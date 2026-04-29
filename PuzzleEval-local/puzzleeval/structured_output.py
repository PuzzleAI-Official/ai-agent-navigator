"""Strict-grammar structured output with non-strict tool-call fallback.

Anthropic's ``messages.parse(output_format=...)`` compiles the Pydantic
model into a token-level constrained-decoding grammar — this is the
fast, guaranteed-valid path. The API enforces an upper bound on
compiled grammar size AND a compilation timeout. Several PuzzleEval
agents emit very large structured outputs (Agent 1's
``UserUnderstandingOutput`` + ``WorkflowBlueprint`` + ``TestPlan`` is
60+ fields across 9 nested types; Agent 2's research result is similar
in shape) and bump up against either ceiling.

This module exposes ``parse_with_fallback()``: try the strict path
first; on the specific 400 errors that signal grammar over-budget, fall
back to ``messages.create()`` with a NON-strict tool whose
``input_schema`` is the same JSON Schema. The model emits JSON freely
(no grammar constraint, no size limit), we extract the ``tool_use``
block's ``input`` dict, and validate it through the same Pydantic
model. The returned shim object exposes ``parsed_output``,
``stop_reason``, ``usage``, ``id``, ``model``, ``content`` so callers
don't need to branch.

The fallback is opt-in per call site — ``parse_with_fallback`` is a
drop-in replacement for ``client.messages.parse``. Callers don't need
to handle the fallback themselves.

Two API constraints the fallback path must work around:
  1. ``tool_choice`` forcing a specific tool is incompatible with
     ``thinking`` (adaptive or extended). The fallback drops thinking.
  2. ``output_config`` is a strict-mode-only field. The fallback drops
     it.

These are graceful degradations: when the strict path is healthy, you
get adaptive thinking + effort tier. When the schema is too big, you
get the result without those reasoning aids — better than failing
hard.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable

import anthropic
from pydantic import TypeAdapter

logger = logging.getLogger(__name__)


# Module-level sleep indirection. Under pytest, `_sleep` becomes a no-op
# so the retry-backoff tests don't wait through real wall-clock seconds.
# Production keeps the real sleep so transient outages get the full
# exponential backoff window (4/8/16/32s). The indirection is a single
# function rather than scattered sleep calls so tests can also
# monkeypatch `_sleep` directly (`puzzleeval.structured_output._sleep`)
# when they need deterministic per-test behavior.
#
# `PYTEST_CURRENT_TEST` is set per-test-run by pytest — we check it
# at CALL time (not import time) because the env var lands AFTER
# modules load. This is the one-line difference between "tests take
# 30s each on retry" and "instant".


def _sleep(delay: float) -> None:
    """Sleep wrapper — auto-zero under pytest for fast test runs."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    time.sleep(delay)


# Substrings that identify the grammar-over-budget 400 errors. Anthropic
# returns several distinct messages for related failure modes; match all of
# them so we don't have to chase new wordings.
_GRAMMAR_FALLBACK_TRIGGERS = (
    "compiled grammar",            # "The compiled grammar is too large..."
    "grammar is too large",        # variant wording
    "grammar compilation timed out",  # "Grammar compilation timed out."
    "grammar compilation",         # any other compilation-phase failure
)


# ── Transient-error retry policy (5xx / overloaded) ──
#
# Anthropic's SDK retries 5xx a few times internally (default max_retries=3)
# with short backoff — that covers most transient blips but NOT extended
# outage windows where every retry within a ~10s span hits the same 500.
# Real observed failure (2026-04-22): Agent 3 hit `req_011CaKNwVbkGYkwRQPPL8QvT`
# with a 500 during test generation; SDK's 3 internal retries all failed
# inside the outage window, propagating the error and killing the run.
#
# Layered fix: after the SDK's built-in retries exhaust, we catch the
# exception and retry at OUR layer with longer exponential backoff +
# jitter, covering 30-90s outage windows. This is the same belt-and-
# braces pattern as rate-limit handling in call_with_model_fallback.
#
# Errors we retry at our layer:
#   - InternalServerError (500)
#   - ServiceUnavailableError ("overloaded", 529 when present)
#   - APIStatusError with status_code in 500..599
#   - APIConnectionError (network blip after SDK gave up)
# Errors we do NOT retry (raise immediately):
#   - BadRequestError (400) — caller request shape is wrong
#   - AuthenticationError (401)
#   - PermissionDeniedError (403)
#   - NotFoundError (404)
#   - UnprocessableEntityError (422)
#   - RateLimitError (429) — handled at model-fallback layer
_TRANSIENT_RETRY_MAX_ATTEMPTS = 4   # after SDK's 3 = 7 total attempts
_TRANSIENT_RETRY_BASE_DELAY_S = 4.0  # 4, 8, 16, 32s with jitter
_TRANSIENT_RETRY_JITTER_S = 1.5


def _is_transient_server_error(exc: Exception) -> bool:
    """Return True when an Anthropic SDK exception is a transient 5xx /
    overload / connection blip worth retrying with backoff.

    Rate limits are deliberately excluded — those have their own model-
    fallback ladder in call_with_model_fallback. Caller-side errors
    (400, 401, 403, 404, 422) are excluded so we fail loudly on real
    bugs rather than waste budget on doomed retries.
    """
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.InternalServerError):
        return True
    # Anthropic SDK exposes "overloaded" as APIStatusError with status 529
    # in newer versions; also handle the generic status-code branch for
    # any 5xx we didn't anticipate.
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is not None and 500 <= status <= 599:
            return True
        # Some SDK versions classify overloaded under APIStatusError
        # with a different status attribute path.
        msg = str(exc).lower()
        if "overloaded" in msg or "server error" in msg or "internal server" in msg:
            return True
    return False


def _retry_transient(
    fn: Callable[[], Any],
    *,
    operation_label: str,
    trace_id: str,
    max_attempts: int = _TRANSIENT_RETRY_MAX_ATTEMPTS,
) -> Any:
    """Retry ``fn()`` on transient 5xx/connection errors with exponential
    backoff + jitter. Re-raises on non-transient errors or when attempts
    are exhausted.

    ``fn`` is expected to be a zero-arg closure wrapping the actual API
    call. Each attempt invokes it fresh — the SDK's internal retry also
    fires, so total attempts = max_attempts × SDK_max_retries.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — re-raise non-transient below
            if not _is_transient_server_error(exc):
                raise
            last_exc = exc
            if attempt == max_attempts - 1:
                # Last attempt — re-raise so caller can surface a clean
                # error message. No point sleeping on the way out.
                break
            delay = (
                _TRANSIENT_RETRY_BASE_DELAY_S * (2 ** attempt)
                + random.uniform(0, _TRANSIENT_RETRY_JITTER_S)
            )
            logger.warning(
                "%s: transient Anthropic error (attempt %d/%d), retrying "
                "in %.1fs — %s",
                operation_label, attempt + 1, max_attempts, delay,
                str(exc)[:200],
                extra={
                    "operation": "structured_output_transient_retry",
                    "trace_id": trace_id,
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "delay_s": delay,
                    "error_type": type(exc).__name__,
                },
            )
            _sleep(delay)
    assert last_exc is not None
    raise last_exc


class StructuredOutputFallbackError(Exception):
    """Raised when the non-strict fallback path also fails to produce output."""


def _should_fall_back(exc: anthropic.BadRequestError) -> bool:
    msg = str(exc).lower()
    return any(trigger in msg for trigger in _GRAMMAR_FALLBACK_TRIGGERS)


_REPR_PATTERNS_TO_LIST = (
    ("frozenset(", ")"),
    ("set(", ")"),
)


def _coerce_repr_strings_to_lists(node: Any) -> Any:
    """Walk the parsed JSON and rescue Python repr strings into proper JSON.

    Without strict-grammar constraints the model occasionally emits a Python
    repr like ``"frozenset({'step_1'})"`` for a field that the schema declares
    as an array (Pydantic ``frozenset[str]``, ``set[str]``, etc.). Pydantic
    will then ITERATE the string character by character, producing a frozenset
    of single chars — silently broken downstream consumers. Detect these
    patterns and convert them to real JSON arrays so Pydantic's validator gets
    the right shape.

    Pure data transformation: dicts walk recursively, lists walk recursively,
    everything else passes through unchanged.
    """
    if isinstance(node, dict):
        return {k: _coerce_repr_strings_to_lists(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_coerce_repr_strings_to_lists(v) for v in node]
    if isinstance(node, str):
        s = node.strip()
        for prefix, suffix in _REPR_PATTERNS_TO_LIST:
            if s.startswith(prefix) and s.endswith(suffix):
                inner = s[len(prefix):-len(suffix)].strip()
                # Strip the inner braces if present: "{'step_1','step_2'}" → "'step_1','step_2'"
                if inner.startswith("{") and inner.endswith("}"):
                    inner = inner[1:-1]
                if inner.startswith("[") and inner.endswith("]"):
                    inner = inner[1:-1]
                # Best-effort split + clean of items.
                parts = [
                    p.strip().strip("'").strip('"')
                    for p in inner.split(",")
                    if p.strip()
                ]
                return parts
        # Empty repr: "frozenset()" or "set()" → []
        if s in ("frozenset()", "set()", "[]", "{}"):
            return []
    return node


def _unwrap_overnested_input(
    tool_input: dict[str, Any], schema_required: set[str], trace_id: str,
) -> dict[str, Any]:
    """Defensive unwrap when the model nests the result under an extra key.

    Some model outputs wrap the structured result under an ``input`` /
    ``result`` / ``arguments`` / ``data`` key despite the tool's
    ``input_schema`` declaring top-level fields. Detect this case
    (single-key dict whose sole value contains all required schema keys)
    and unwrap. No-op when the model emits correctly.
    """
    if not (
        isinstance(tool_input, dict)
        and len(tool_input) == 1
        and schema_required
    ):
        return tool_input
    sole_key = next(iter(tool_input))
    sole_val = tool_input[sole_key]
    if (
        sole_key not in schema_required
        and isinstance(sole_val, dict)
        and schema_required.issubset(sole_val.keys())
    ):
        logger.warning(
            "structured_output: unwrapping over-nested tool input from key %r",
            sole_key,
            extra={
                "operation": "structured_output_fallback_unwrap",
                "trace_id": trace_id,
            },
        )
        return sole_val
    return tool_input


class _ShimResponse:
    """Mirrors the ``messages.parse`` response shape so callers don't branch."""
    parsed_output: Any = None
    stop_reason: str = "tool_use"
    usage: Any = None
    id: str | None = None
    model: str = ""
    content: list[Any] = []


def _run_non_strict(
    *,
    client: anthropic.Anthropic,
    model: str,
    max_tokens: int,
    system: Any,
    messages: list[dict[str, Any]],
    output_format: type,
    extra: dict[str, Any],
    trace_id: str,
    fallback_tool_name: str,
) -> Any:
    """Run the non-strict tool path and return a shim mirroring messages.parse.

    Extracted from the body of `parse_with_fallback` so callers that
    KNOW their schema overflows the strict-grammar budget can skip the
    doomed strict attempt entirely (via `prefer_non_strict=True`).

    Identical behavior to the prior fallback path: build a non-strict
    tool from the Pydantic schema, force tool_choice, parse the
    `tool_use` block, Pydantic-validate after applying the over-nesting
    + repr-string defenses.
    """
    schema = TypeAdapter(output_format).json_schema()
    tool_def = {
        "name": fallback_tool_name,
        "description": (
            "Emit the final structured result. Call this tool exactly once. "
            "The arguments object IS the result — top-level keys must be the "
            "schema's top-level fields, do not nest the result under another key. "
            "EVERY array field must be a real JSON array (e.g. [\"step_1\"]), "
            "never a string like \"frozenset({'step_1'})\" or \"['step_1']\". "
            "EVERY object field must be a real JSON object, never a string."
        ),
        "input_schema": schema,
    }
    # Strip strict-mode-only fields that are incompatible with forced tool_choice.
    fallback_extra = {
        k: v for k, v in extra.items()
        if k not in ("output_config", "thinking")
    }
    # Wrap the non-strict path in the transient-5xx retry — Anthropic
    # blips happen on this path too.
    def _call():
        return client.messages.create(
            model=model, max_tokens=max_tokens,
            system=system, messages=messages,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": fallback_tool_name},
            **fallback_extra,
        )
    raw = _retry_transient(
        _call,
        operation_label="structured_output.non_strict",
        trace_id=trace_id,
    )

    tool_input: dict[str, Any] | None = None
    for block in (raw.content or []):
        if (
            getattr(block, "type", "") == "tool_use"
            and getattr(block, "name", "") == fallback_tool_name
        ):
            tool_input = block.input  # type: ignore[assignment]
            break
    if tool_input is None:
        raise StructuredOutputFallbackError(
            f"non-strict path: model returned no `{fallback_tool_name}` "
            f"tool_use block. stop_reason={getattr(raw, 'stop_reason', None)}"
        )

    schema_required = set(schema.get("required") or [])
    tool_input = _unwrap_overnested_input(tool_input, schema_required, trace_id)
    # Defense against Python repr strings in array-typed fields. Cheap pass,
    # no-op when the model emits properly.
    tool_input = _coerce_repr_strings_to_lists(tool_input)
    # Pydantic validation — final line of defense against malformed model
    # output that the over-nest unwrap + repr-string coerce didn't catch
    # (wrong field types, missing required fields, structural mismatches).
    # Wrap in try/except so the caller sees a `StructuredOutputFallbackError`
    # (the existing AgentOutputError mapping in research.py / screening.py
    # already handles this exception type) instead of an uncaught
    # `pydantic.ValidationError` that would crash the agent. Defense in
    # depth — the strict-grammar path prevents this at the API level when
    # available; the non-strict path needs us to enforce it post-hoc.
    try:
        parsed = TypeAdapter(output_format).validate_python(tool_input)
    except Exception as exc:  # noqa: BLE001 — pydantic.ValidationError is the common case
        # Truncate the error message — pydantic's full validation report
        # can be 1000+ chars listing every field issue. The first 500
        # chars give the caller enough to diagnose without flooding logs.
        raise StructuredOutputFallbackError(
            f"non-strict path: pydantic validation failed for "
            f"{output_format.__name__}: {type(exc).__name__}: "
            f"{str(exc)[:500]}"
        ) from exc

    shim = _ShimResponse()
    shim.parsed_output = parsed
    shim.stop_reason = getattr(raw, "stop_reason", "tool_use")
    shim.usage = getattr(raw, "usage", None)
    shim.id = getattr(raw, "id", None)
    shim.model = getattr(raw, "model", model)
    shim.content = getattr(raw, "content", [])
    return shim


def parse_with_fallback(
    *,
    client: anthropic.Anthropic,
    model: str,
    max_tokens: int,
    system: Any,
    messages: list[dict[str, Any]],
    output_format: type,
    extra: dict[str, Any] | None = None,
    trace_id: str = "",
    fallback_tool_name: str = "emit_result",
    prefer_non_strict: bool = False,
) -> Any:
    """Strict-grammar parse with non-strict tool fallback on grammar errors.

    Drop-in replacement for ``client.messages.parse``. Forwards every
    argument the strict path accepts via ``extra`` — pass thinking,
    output_config, cache_control, tools, etc. through there. Returns an
    object exposing ``parsed_output``, ``stop_reason``, ``usage``,
    ``id``, ``model``, ``content`` regardless of which path produced it.

    ``prefer_non_strict`` (default False): when True, skips the strict
    attempt entirely and goes straight to the non-strict tool path.
    Use this for schemas KNOWN to overflow Anthropic's compiled-grammar
    budget — currently `Agent2Result` and `Agent4Result` (the latter
    with the BuildReadinessChecklist additions). Saves ~30-60s + one
    doomed API call per invocation by skipping the strict attempt that
    will reliably 400 with "compiled grammar is too large." The
    non-strict path produces an identical shim object, so callers don't
    branch on the choice. Default unchanged: agents whose schemas fit
    strict (Agent 1, Agent 3, Agent 5 evaluator) benefit from strict's
    free correctness guarantees.
    """
    extra = dict(extra or {})

    # ── Fast path: caller knows the schema overflows; skip strict ──
    if prefer_non_strict:
        return _run_non_strict(
            client=client, model=model, max_tokens=max_tokens,
            system=system, messages=messages,
            output_format=output_format, extra=extra,
            trace_id=trace_id, fallback_tool_name=fallback_tool_name,
        )

    # Wrap the strict parse in a transient-error retry loop. This is the
    # critical path for Agents 1/2/3/4/5 structured output — when
    # Anthropic has a blip, we don't want a single 500 to kill the
    # whole pipeline (real-run regression observed 2026-04-22).
    def _strict_call():
        return client.messages.parse(
            model=model, max_tokens=max_tokens,
            system=system, messages=messages,
            output_format=output_format,
            **extra,
        )
    try:
        return _retry_transient(
            _strict_call,
            operation_label="structured_output.strict_parse",
            trace_id=trace_id,
        )
    except anthropic.BadRequestError as exc:
        if not _should_fall_back(exc):
            raise
        logger.warning(
            "structured_output: strict-grammar path failed, "
            "falling back to non-strict tool — %s",
            str(exc)[:200],
            extra={
                "operation": "structured_output_fallback",
                "trace_id": trace_id,
            },
        )

    # ── Non-strict fallback path (after strict 400'd) ──
    return _run_non_strict(
        client=client, model=model, max_tokens=max_tokens,
        system=system, messages=messages,
        output_format=output_format, extra=extra,
        trace_id=trace_id, fallback_tool_name=fallback_tool_name,
    )


__all__ = [
    "StructuredOutputFallbackError",
    "parse_with_fallback",
]
