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
  * The model-fallback ladder via ``call_with_model_fallback`` (Opus →
    Sonnet → Haiku on persistent 429 at the primary model).
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

import anthropic

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
         120K). Keeps last 3 tool uses; never clears write_file /
         patch_file / advisor.
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
            "exclude_tools": [
                "write_file", "patch_file", "advisor",
            ],
        },
        {
            "type": "compact_20260112",
            "trigger": {"type": "input_tokens", "value": 150000},
            "instructions": (
                "Summarize this conversation for continuity. "
                "You MUST preserve ALL of the following:\n"
                "1. Exact API endpoint URLs, base URL, and auth header format (e.g., 'Bearer' vs 'Token')\n"
                "2. Full api_spec.txt contents: INPUT_COMPATIBILITY, ROUTING_TABLE, ENDPOINTS, WORKING_EXAMPLE\n"
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
                    block["content"] = (
                        "(tool returned no content; PuzzleEval inserted this "
                        "diagnostic placeholder so the Anthropic message "
                        "contract remains valid)"
                    )
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


def sanitize_messages_for_anthropic(
    messages: list,
    *,
    logger: Any,
    trace_id: str,
    candidate_name: str,
) -> int:
    """Remove/repair empty message content before an Anthropic API call.

    This is a defensive API-boundary invariant, not a provider-specific patch:
    no tool, server result, context compaction, or future helper may pass empty
    user/assistant text to Anthropic. Returns the number of repaired/dropped
    messages for telemetry.
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

    if repaired:
        messages[:] = sanitized
        logger.warning(
            "sanitized empty builder message content before Anthropic call",
            extra={
                "operation": "agent5_message_sanitized",
                "trace_id": trace_id,
                "candidate_name": candidate_name,
                "repaired_messages": repaired,
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

            # Wrap in model-fallback ladder: on persistent 429 at
            # ``current_model`` (Opus 4.7 by default), degrade to Sonnet
            # 4.6 → Haiku 4.5 rather than hard-failing after the SDK's
            # 3 retries. ``call_with_model_fallback`` re-raises non-rate-
            # limit errors unchanged so the PTL recovery branch below
            # still fires.
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
]
