"""Anthropic API call boundary for the Agent 5 builder loop.

Phase 4 Path B Step 1 — extracts the ``client.beta.messages.create()``
boundary (~265 LoC) out of ``_build_single_harness``. This module owns:

  * ONE typed entry point: ``make_builder_api_call(ctx) -> APICallOutcome``
  * The retry loop with three typed exception handlers:
      - ``anthropic.BadRequestError`` → PTL (prompt-too-long) recovery:
        match against five PTL marker substrings, halve max_tokens,
        retry. Non-PTL 400s are fatal.
      - ``anthropic.RateLimitError`` → exponential backoff (15s, 30s, 60s),
        then FailedHarness(build_timeout).
      - ``anthropic.APIConnectionError`` / ``anthropic.APIStatusError``
        → FailedHarness(unknown).
  * Strict use of the configured Agent 5 builder model. Builder calls do
    not silently downgrade to the research-worker model; emergency model
    overrides use ``PUZZLEEVAL_BUILDER_MODEL``.
  * The block-level ``cache_control: ephemeral`` system prompt + per-call
    server-side context_management edits (clear_thinking, clear_tool_uses,
    compact).
  * Best-effort message cache breakpoint mutation (``_apply_message_cache_breakpoint``).

What it does NOT own (deliberately):
  * Per-turn telemetry (``turn_blocks.build_initial_turn_log``)
  * Response content iteration (``turn_blocks`` + dispatch loop)
  * SSE progress emission (``turn_blocks.emit_build_turn_progress``)

Why a dataclass for input + a tagged union for output:
  * The original inline block had 15+ caller-visible variables. Passing
    14 kwargs is a code smell — the helper's call site stays painful and
    easy to mis-wire. ``BuilderAPICallContext`` (frozen) collects the
    inputs in one place; future fields land in one place too.
  * The output is binary: a successful response, or a FailedHarness to
    early-return. ``APICallSuccess`` / ``APICallFailure`` make that
    pattern explicit at the call site (caller pattern-matches on the
    type, no implicit None checks).

AD-007 boundary: this module is pure backbone. Retries, PTL detection,
backoff math — all deterministic Python. Markdown contracts don't gate
behavior here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

try:
    import anthropic
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal test envs
    from puzzleeval.anthropic_client import anthropic  # type: ignore

from puzzleeval.anthropic_client import call_with_model_fallback
from puzzleeval.config import (
    CACHE_CLEAR_AT_LEAST_TOKENS,
    CACHE_CLEAR_TOOL_USES_TRIGGER,
    CACHE_MESSAGES_ENABLED,
)
from puzzleeval.schemas import FailedHarness


# Anthropic beta features required by Agent 5's builder calls.
# Pinned here so callers don't have to know which betas opt-in to which
# capability: context_management edits, microcompact, advisor sub-agent.
BUILDER_BETAS: tuple[str, ...] = (
    "context-management-2025-06-27",
    "compact-2026-01-12",
    "advisor-tool-2026-03-01",
)


def _build_context_management_edits() -> list[dict]:
    """Per-call context_management.edits payload.

    ORDERING NOTE (real-run e21f6077, 2026-04-21): the Anthropic schema
    rejects any edits list where ``clear_thinking_20251015`` is not the
    FIRST entry. The error message is misleading
    ("must be the first strategy in `context_management.edits` when
    provided") and not in the public docs we read, but the API enforces
    it hard — both OpenAI and ElevenLabs builds 400'd on turn 1 before
    the builder wrote a single file. Regression-guarded by
    ``test_clear_thinking_edit_is_first_in_edits_list``.

    Three edits, in order:
      1. ``clear_thinking_20251015`` (keep:all) — preserves ALL thinking
         blocks across turns to maximize cache hits on the message prefix.
      2. ``clear_tool_uses_20250919`` — clears stale tool results when
         input tokens cross ``CACHE_CLEAR_TOOL_USES_TRIGGER`` (default
         120K). Keeps last 3 tool uses; never clears locally dispatched
         Agent 5 custom tools or advisor.
      3. ``compact_20260112`` — Claude-powered summarization at 150K
         tokens. Custom instructions preserve API endpoints, auth header
         format, env var names, SDK versions, file purposes, errors +
         fixes — everything Opus needs to continue the build.
    """
    return [
        {
            "type": "clear_thinking_20251015",
            "keep": "all",
        },
        {
            "type": "clear_tool_uses_20250919",
            "trigger": {
                "type": "input_tokens",
                "value": CACHE_CLEAR_TOOL_USES_TRIGGER,
            },
            "keep": {"type": "tool_uses", "value": 3},
            "clear_at_least": {
                "type": "input_tokens",
                "value": CACHE_CLEAR_AT_LEAST_TOKENS,
            },
            "clear_tool_inputs": False,
            # Never clear locally-dispatched custom tools unless the pairing
            # invariant can be preserved server-side. The latest voice run hit
            # an Anthropic 400 when a local run_code tool_use survived but its
            # tool_result had been stripped. Keep web/advisor compaction
            # available, but protect every local custom tool result.
            "exclude_tools": [
                "ask_research",
                "read_file",
                "read_file_range",
                "read_forensics",
                "run_code",
                "summarize_build_state",
                "summarize_forensics",
                "write_file",
                "patch_file",
                "advisor",
            ],
        },
        {
            "type": "compact_20260112",
            "trigger": {"type": "input_tokens", "value": 150000},
            "instructions": (
                "Summarize this conversation for continuity. "
                "You MUST preserve ALL of the following:\n"
                "1. Exact API endpoint URLs, base URL, and auth header format (e.g., 'Bearer' vs 'Token')\n"
                "2. Full research_synthesis.json decisions: input_compatibility, routing_table, chosen_api_surface, working_examples, errors_and_limits\n"
                "3. Credential env var names (e.g., MINDEE_API_KEY, VERYFI_CLIENT_ID) and which API version they target\n"
                "4. Installed SDK package names and versions (e.g., 'mindee>=4.25.0') and key method names used\n"
                "5. All files written to sandbox (harness.py, requirements.txt, smoke_test.py, etc.) and their purpose\n"
                "6. Build approach: using official SDK vs raw requests, sync vs async/polling\n"
                "7. Specific errors encountered, their root causes, and fixes applied\n"
                "8. Which test input forms are compatible vs INCOMPATIBLE and why\n"
                "9. Current phase (research/build/verify) and concrete next steps\n"
                "Wrap your summary in <summary></summary>."
            ),
        },
    ]


# PTL marker substrings — case-insensitive match against ``str(exc).lower()``.
# An earlier version matched any occurrence of "context" which false-positived
# on unrelated 400s (e.g., a schema-validation error whose path included
# "context_management..."). Each false-positive retried instantly, hit the
# same 400, halved max_tokens, and blew through the retry budget in
# microseconds — a real tracer-bullet from trace real_debug_3. Use ONLY
# these five exact substrings. Pinned by ``TestAPICallRetryBehavior`` in
# ``tests/test_build_loop_behavior.py`` (5 positive + 1 false-positive guard).
PTL_MARKERS: tuple[str, ...] = (
    "prompt is too long",
    "prompt too long",
    "maximum context length",
    "max_tokens",
    "context_length_exceeded",
)


def _is_ptl_error(exc: anthropic.BadRequestError) -> bool:
    """Detect prompt-too-long among the variants Anthropic returns.

    Pure function. Tested directly via ``test_api_call.py``.
    """
    error_msg = str(exc).lower()
    return any(marker in error_msg for marker in PTL_MARKERS)


EMPTY_TOOL_RESULT_PLACEHOLDER = (
    "(tool returned no content; PuzzleEval inserted this diagnostic "
    "placeholder so the Anthropic message contract remains valid)"
)

MISSING_TOOL_RESULT_PLACEHOLDER = (
    "(tool result was missing; PuzzleEval inserted this synthetic error "
    "result before the Anthropic API call so every assistant tool_use has "
    "a matching user tool_result)"
)


def _is_empty_content(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def _sanitize_message_content(content: Any) -> tuple[Any, bool, bool]:
    """Return (clean_content, changed, is_empty_message).

    Anthropic rejects messages with empty text content. Tool-result messages are
    special: dropping them can break assistant tool_use pairing, so empty
    tool_result content is replaced with an explicit diagnostic string.
    """
    if isinstance(content, str):
        if content.strip():
            return content, False, False
        return "", True, True

    if not isinstance(content, list):
        return content, False, False

    cleaned: list[Any] = []
    changed = False
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            if block_type == "tool_result":
                result_content = block.get("content")
                if _is_empty_content(result_content):
                    block = dict(block)
                    block["content"] = EMPTY_TOOL_RESULT_PLACEHOLDER
                    block["is_error"] = block.get("is_error", True)
                    changed = True
                cleaned.append(block)
                continue
            if block_type == "text" and str(block.get("text") or "").strip() == "":
                changed = True
                continue
            cleaned.append(block)
            continue

        block_type = getattr(block, "type", None)
        if block_type == "text" and str(getattr(block, "text", "") or "").strip() == "":
            changed = True
            continue
        cleaned.append(block)

    if len(cleaned) != len(content):
        changed = True
    return cleaned, changed, len(cleaned) == 0


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _block_value(block: Any, key: str) -> Any:
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def _content_as_block_list(content: Any) -> list[Any]:
    if isinstance(content, list):
        return list(content)
    if isinstance(content, str):
        if not content.strip():
            return []
        return [{"type": "text", "text": content}]
    if content is None:
        return []
    return [content]


def _assistant_tool_use_ids(msg: dict) -> list[str]:
    if msg.get("role") != "assistant":
        return []
    ids: list[str] = []
    for block in _content_as_block_list(msg.get("content")):
        if _block_type(block) == "tool_use":
            tool_id = _block_value(block, "id")
            if tool_id:
                ids.append(str(tool_id))
    return ids


def _user_tool_result_ids(msg: dict) -> set[str]:
    if msg.get("role") != "user":
        return set()
    ids: set[str] = set()
    for block in _content_as_block_list(msg.get("content")):
        if _block_type(block) == "tool_result":
            tool_id = _block_value(block, "tool_use_id")
            if tool_id:
                ids.add(str(tool_id))
    return ids


def _tool_result_id(block: Any) -> str:
    if _block_type(block) != "tool_result":
        return ""
    tool_id = _block_value(block, "tool_use_id")
    return str(tool_id) if tool_id else ""


def _synthetic_tool_result(tool_use_id: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": f"{MISSING_TOOL_RESULT_PLACEHOLDER}: {tool_use_id}",
        "is_error": True,
    }


def _append_missing_tool_results(msg: dict, missing_ids: list[str]) -> dict:
    updated = dict(msg)
    blocks = _content_as_block_list(updated.get("content"))
    blocks.extend(_synthetic_tool_result(tool_id) for tool_id in missing_ids)
    updated["content"] = blocks
    return updated


def _normalize_tool_result_response(msg: dict, tool_ids: list[str]) -> tuple[dict, int]:
    """Return a user message whose required tool_results are contiguous first.

    Anthropic's contract is stricter than "the ids exist somewhere": the user
    message immediately after an assistant tool-use turn must begin with the
    matching tool_result blocks. Supplemental text is allowed only after those
    results. This normalizer makes that invariant explicit and keeps local
    diagnostics from corrupting the API protocol.
    """

    updated = dict(msg)
    blocks = _content_as_block_list(updated.get("content"))
    by_id: dict[str, Any] = {}
    extras: list[Any] = []
    for block in blocks:
        result_id = _tool_result_id(block)
        if result_id and result_id not in by_id:
            by_id[result_id] = block
        else:
            extras.append(block)

    ordered: list[Any] = []
    repaired = 0
    for tool_id in tool_ids:
        block = by_id.pop(tool_id, None)
        if block is None:
            block = _synthetic_tool_result(tool_id)
            repaired += 1
        ordered.append(block)

    # Preserve unrelated tool_results and text after the required contiguous
    # prefix. They may be useful diagnostics, but they cannot appear before or
    # between required tool_result blocks.
    ordered.extend(by_id.values())
    ordered.extend(extras)
    if ordered != blocks and repaired == 0:
        repaired += 1
    updated["content"] = ordered
    return updated, repaired


def validate_tool_result_pairing(messages: list[Any]) -> list[str]:
    """Return API-boundary tool-result invariant violations.

    This validator is intentionally mechanical: it checks Anthropic's wire
    protocol only, not build semantics. It is used after best-effort
    sanitization so malformed histories are caught locally instead of burning a
    real API call.
    """

    issues: list[str] = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        tool_ids = _assistant_tool_use_ids(msg)
        if not tool_ids:
            continue
        next_msg = messages[index + 1] if index + 1 < len(messages) else None
        if not isinstance(next_msg, dict) or next_msg.get("role") != "user":
            issues.append(f"assistant message {index} has tool_use blocks without following user tool_result message")
            continue
        blocks = _content_as_block_list(next_msg.get("content"))
        prefix_ids = [_tool_result_id(block) for block in blocks[:len(tool_ids)]]
        if prefix_ids != tool_ids:
            issues.append(
                f"assistant message {index} tool_result prefix mismatch: expected {tool_ids}, got {prefix_ids}"
            )
    return issues


def _enforce_tool_result_pairing(messages: list[Any]) -> tuple[list[Any], int]:
    """Ensure assistant tool_use blocks are followed by matching tool_results.

    Anthropic's tool contract is positional: when an assistant message contains
    one or more ``tool_use`` blocks, the immediately following user message must
    contain a non-empty ``tool_result`` block for every ``tool_use.id``. The
    build loop normally creates those pairs, but this API-boundary sanitizer is
    the last line of defense after context compaction, server-tool cleanup, or
    partial tool-dispatch failures.
    """
    repaired = 0
    out: list[Any] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        out.append(msg)
        if not isinstance(msg, dict):
            i += 1
            continue

        tool_ids = _assistant_tool_use_ids(msg)
        if not tool_ids:
            i += 1
            continue

        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        if isinstance(next_msg, dict) and next_msg.get("role") == "user":
            normalized, normalized_repairs = _normalize_tool_result_response(
                next_msg,
                tool_ids,
            )
            messages[i + 1] = normalized
            repaired += normalized_repairs
            i += 1
            continue

        out.append({
            "role": "user",
            "content": [_synthetic_tool_result(tool_id) for tool_id in tool_ids],
        })
        repaired += len(tool_ids)
        i += 1

    return out, repaired


def sanitize_messages_for_anthropic(
    messages: list,
    *,
    logger: Any,
    trace_id: str,
    candidate_name: str,
) -> int:
    """Repair message-shape invariants before an Anthropic API call.

    This is a defensive API-boundary invariant, not a provider-specific patch:
    no tool, server result, context compaction, or future helper may pass empty
    user/assistant text to Anthropic. Also, every assistant ``tool_use`` block
    must be followed immediately by a user ``tool_result`` with the same
    ``tool_use_id`` and non-empty content. Missing results are synthesized;
    empty results are backfilled. Returns the number of repaired/dropped items
    for telemetry.
    """
    repaired = 0
    sanitized: list[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            sanitized.append(msg)
            continue
        content, changed, empty = _sanitize_message_content(msg.get("content"))
        if empty:
            repaired += 1
            continue
        if changed:
            repaired += 1
            msg = dict(msg)
            msg["content"] = content
        sanitized.append(msg)

    sanitized, pairing_repairs = _enforce_tool_result_pairing(sanitized)
    repaired += pairing_repairs
    protocol_issues = validate_tool_result_pairing(sanitized)
    if protocol_issues:
        raise ValueError(
            "Anthropic tool_result protocol invariant failed after sanitization: "
            + "; ".join(protocol_issues[:5])
        )

    if repaired:
        messages[:] = sanitized
        logger.warning(
            "sanitized builder messages before Anthropic call",
            extra={
                "operation": "agent5_message_sanitized",
                "trace_id": trace_id,
                "candidate_name": candidate_name,
                "repaired_items": repaired,
                "tool_pairing_repairs": pairing_repairs,
            },
        )
    return repaired


# ---------------------------------------------------------------------------
# Input + outcome types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuilderAPICallContext:
    """All inputs needed to make ONE builder API call attempt.

    Frozen so the caller can audit immutability across the helper boundary.
    The helper internally manages ``current_max_tokens`` halving across
    retries (reads ctx.current_max_tokens as the initial value).

    Field grouping:
      * Anthropic call inputs: client, current_model, current_max_tokens,
        messages, system_text, tools, output_config
      * Retry budget: max_retries
      * FailedHarness construction context: candidate, candidate_label,
        sandbox_dir, trace_id, turn, accumulated_cost,
        candidate_web_fetch_blocks
      * Observability: logger
    """

    # Anthropic client
    client: Any  # anthropic.Anthropic

    # Per-iteration model + initial output token budget
    current_model: str
    current_max_tokens: int

    # Mutable conversation state. The helper will call
    # ``_apply_message_cache_breakpoint`` on this list when
    # ``CACHE_MESSAGES_ENABLED`` is True (in-place mutation).
    messages: list

    # Pre-rendered prompt: caller has already applied preamble +
    # appendix + ``_render_builder_prompt``. Helper wraps in the
    # ``cache_control: ephemeral`` system block.
    system_text: str

    # Pre-built tools list (caller has already applied
    # ``_build_tools_with_programmatic``).
    tools: list

    # Output config (when present, becomes the ``output_config`` kwarg).
    output_config: dict | None

    # Retry budget — caller controls. Production default: 3.
    max_retries: int

    # FailedHarness construction context
    candidate: Any  # ScreenedCandidate
    candidate_label: str
    sandbox_dir: Path
    trace_id: str
    turn: int
    accumulated_cost: float
    candidate_web_fetch_blocks: int

    # Best-effort fn for partial_code on FailedHarness — caller injects
    # so this module doesn't import the function from
    # ``implement_test_env`` (avoids import cycle).
    read_harness_code: Any  # Callable[[Path], str | None]

    # Best-effort fn for the message cache breakpoint mutation — caller
    # injects (currently lives in ``implement_test_env`` and depends on
    # the loop's local helpers).
    apply_message_cache_breakpoint: Any  # Callable[[list], None]

    # Observability
    logger: Any  # logging.Logger


@dataclass(frozen=True)
class APICallSuccess:
    """The API call returned a response. Caller continues the loop."""
    response: Any  # anthropic.types.Message


@dataclass(frozen=True)
class APICallFailure:
    """The API call failed unrecoverably. Caller returns this FailedHarness."""
    failed_harness: FailedHarness


APICallOutcome = Union[APICallSuccess, APICallFailure]


# ---------------------------------------------------------------------------
# The single entry point
# ---------------------------------------------------------------------------


def make_builder_api_call(ctx: BuilderAPICallContext) -> APICallOutcome:
    """Make ONE Anthropic API call attempt with retry/PTL/rate-limit recovery.

    Returns:
        APICallSuccess(response) — the ``response`` is the
            ``anthropic.types.Message`` from a successful call. Caller
            continues the build loop with this response.
        APICallFailure(failed_harness) — an unrecoverable error occurred
            (PTL exhausted, rate limit exhausted, or non-recoverable API
            error). Caller MUST return this ``failed_harness`` from the
            outer build function — DO NOT retry.

    Side effects:
        * Mutates ``ctx.messages`` in-place via the cache breakpoint
          callback (when ``CACHE_MESSAGES_ENABLED`` is True).
        * Sleeps via ``time.sleep`` on rate-limit retries.
        * Emits ``logger.warning`` / ``logger.info`` for retry events
          and unrecoverable failures.

    The retry loop runs ``range(max_retries + 1)`` attempts. The PTL
    handler halves ``current_max_tokens`` between retries (floor 4096).
    The rate-limit handler waits ``(2 ** retry) * 15`` seconds (15, 30, 60).
    """
    current_max_tokens = ctx.current_max_tokens
    context_edits = _build_context_management_edits()
    candidate_name = ctx.candidate.name

    for retry in range(ctx.max_retries + 1):
        try:
            # Message-level cache breakpoint mutation. The caller-injected
            # callback caches the growing conversation prefix so turn N+1
            # reads turn N's full conversation at 0.1× base input cost.
            # Two active breakpoints total (this + the system block).
            # Gated by env so a future Anthropic regression can be reverted
            # with PUZZLEEVAL_CACHE_MESSAGES_ENABLED=0.
            if CACHE_MESSAGES_ENABLED:
                ctx.apply_message_cache_breakpoint(ctx.messages)
            sanitize_messages_for_anthropic(
                ctx.messages,
                logger=ctx.logger,
                trace_id=ctx.trace_id,
                candidate_name=candidate_name,
            )

            # Call the configured Agent 5 lead model exactly. The generic
            # fallback helper is still used for consistent rate-limit
            # classification, but builder fallback is disabled so Opus-lead
            # ownership cannot silently degrade into a Sonnet build turn.
            kwargs: dict[str, object] = {}
            if ctx.output_config is not None:
                kwargs["output_config"] = ctx.output_config

            response = call_with_model_fallback(
                fn=lambda _m: ctx.client.beta.messages.create(
                    model=_m,
                    max_tokens=current_max_tokens,
                    betas=list(BUILDER_BETAS),
                    # System block carries cache_control: ephemeral so the
                    # ~10.7K-token builder prompt caches across every
                    # turn of a candidate's build (1.25× write turn 1,
                    # 0.1× reads turns 2+).
                    system=[{
                        "type": "text",
                        "text": ctx.system_text,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=ctx.messages,
                    tools=ctx.tools,
                    thinking={"type": "adaptive"},
                    context_management={"edits": context_edits},
                    **kwargs,
                ),
                primary_model=ctx.current_model,
                trace_id=ctx.trace_id,
                operation_label=f"agent5_builder/{candidate_name}",
                allow_fallbacks=False,
            )
            return APICallSuccess(response=response)

        except anthropic.BadRequestError as e:
            # PTL recovery: halve max_tokens and retry. Match only ACTUAL
            # PTL markers (see PTL_MARKERS); a 400 mentioning
            # "context_management" alone must NOT trigger this branch.
            if _is_ptl_error(e) and retry < ctx.max_retries:
                ctx.logger.warning(
                    f"Context overflow for {candidate_name}, reducing max_tokens",
                    extra={
                        "operation": "ptl_recovery",
                        "trace_id": ctx.trace_id,
                        "candidate_name": candidate_name,
                        "retry": retry + 1,
                    },
                )
                current_max_tokens = max(4096, current_max_tokens // 2)
                continue
            # Non-PTL 400 OR PTL retries exhausted — fatal
            ctx.logger.warning(
                f"Bad request for {candidate_name}: {e}",
                extra={
                    "operation": f"harness_build_{ctx.candidate_label}",
                    "trace_id": ctx.trace_id,
                    "error": str(e),
                },
            )
            return APICallFailure(
                failed_harness=FailedHarness(
                    candidate_name=candidate_name,
                    provider=ctx.candidate.provider,
                    failure_reason=f"API bad request: {e}",
                    failure_category="unknown",
                    partial_code=ctx.read_harness_code(ctx.sandbox_dir),
                    turns_attempted=ctx.turn,
                    web_fetch_blocks=ctx.candidate_web_fetch_blocks,
                    build_cost_usd=round(ctx.accumulated_cost, 4),
                    harness_dir=str(ctx.sandbox_dir),
                )
            )

        except anthropic.RateLimitError as e:
            if retry < ctx.max_retries:
                wait = (2 ** retry) * 15  # 15s, 30s, 60s
                ctx.logger.info(
                    f"Rate limit for {candidate_name}, waiting {wait}s "
                    f"(retry {retry + 1}/{ctx.max_retries})",
                    extra={
                        "operation": f"harness_build_{ctx.candidate_label}_rate_limit_retry",
                        "trace_id": ctx.trace_id,
                        "retry": retry + 1,
                        "wait_seconds": wait,
                    },
                )
                time.sleep(wait)
                continue
            ctx.logger.warning(
                f"Rate limit exhausted for {candidate_name} after "
                f"{ctx.max_retries} retries",
                extra={
                    "operation": f"harness_build_{ctx.candidate_label}",
                    "trace_id": ctx.trace_id,
                    "error": str(e),
                    "error_type": "RateLimitError",
                },
            )
            return APICallFailure(
                failed_harness=FailedHarness(
                    candidate_name=candidate_name,
                    provider=ctx.candidate.provider,
                    failure_reason=(
                        f"Rate limit exceeded after {ctx.max_retries} retries: {e}"
                    ),
                    failure_category="build_timeout",
                    partial_code=ctx.read_harness_code(ctx.sandbox_dir),
                    turns_attempted=ctx.turn,
                    web_fetch_blocks=ctx.candidate_web_fetch_blocks,
                    build_cost_usd=round(ctx.accumulated_cost, 4),
                    harness_dir=str(ctx.sandbox_dir),
                )
            )

        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            ctx.logger.warning(
                f"API error building {candidate_name}",
                extra={
                    "operation": f"harness_build_{ctx.candidate_label}",
                    "trace_id": ctx.trace_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return APICallFailure(
                failed_harness=FailedHarness(
                    candidate_name=candidate_name,
                    provider=ctx.candidate.provider,
                    failure_reason=f"API error during harness building: {e}",
                    failure_category="unknown",
                    partial_code=ctx.read_harness_code(ctx.sandbox_dir),
                    turns_attempted=ctx.turn,
                    web_fetch_blocks=ctx.candidate_web_fetch_blocks,
                    build_cost_usd=round(ctx.accumulated_cost, 4),
                    harness_dir=str(ctx.sandbox_dir),
                )
            )

    # Defensive: the loop body either returns or continues; the only way
    # to fall out is range exhaustion without hitting any branch, which
    # is unreachable given the structure above. Surface as a typed error
    # rather than silently returning None.
    raise RuntimeError(
        f"make_builder_api_call: retry loop exited without an outcome "
        f"for {candidate_name} (this should be unreachable)"
    )


__all__ = [
    "APICallFailure",
    "APICallOutcome",
    "APICallSuccess",
    "BUILDER_BETAS",
    "BuilderAPICallContext",
    "PTL_MARKERS",
    "make_builder_api_call",
    "sanitize_messages_for_anthropic",
    "validate_tool_result_pairing",
]
