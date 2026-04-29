"""Tests for puzzleeval.structured_output.parse_with_fallback.

The fallback wrapper is the durable fix for Anthropic's
compiled-grammar size + timeout caps on large structured outputs.
These tests use stubs (no real API calls) to verify:

1. Strict path success → return value passes through unchanged.
2. Strict path raises non-grammar 400 → re-raised.
3. Strict path raises grammar 400 → fallback path runs and validates JSON.
4. Fallback drops thinking + output_config.
5. Fallback unwraps over-nested model output ({"input":{...}} variant).
6. Fallback raises clear error when no tool_use block is returned.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from pydantic import BaseModel, Field

from puzzleeval.structured_output import (
    StructuredOutputFallbackError,
    parse_with_fallback,
)


# ---------------------------------------------------------------------------
# Test schema (intentionally small — these are unit tests, not API tests)
# ---------------------------------------------------------------------------


class _SmallResult(BaseModel):
    is_clear: bool = Field(description="boolean")
    note: str = Field(default="", description="optional note")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_grammar_400(message: str) -> anthropic.BadRequestError:
    """Build a real BadRequestError (the SDK rejects synthetic constructions)."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(
        400, request=req,
        json={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )
    return anthropic.BadRequestError(message=message, response=resp, body=None)


def _make_unrelated_400() -> anthropic.BadRequestError:
    return _make_grammar_400("invalid model parameter")


def _stub_tool_use_response(input_dict: dict) -> Any:
    """Build a fake messages.create() response carrying one tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "emit_result"
    block.input = input_dict
    raw = MagicMock()
    raw.content = [block]
    raw.stop_reason = "tool_use"
    raw.usage = MagicMock(input_tokens=10, output_tokens=20)
    raw.id = "msg_test"
    raw.model = "claude-test"
    return raw


# ---------------------------------------------------------------------------
# Strict path
# ---------------------------------------------------------------------------


def test_strict_path_success_pass_through():
    expected_response = MagicMock(parsed_output=_SmallResult(is_clear=True))
    client = MagicMock()
    client.messages.parse.return_value = expected_response
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[],
        output_format=_SmallResult, extra={"thinking": {"type": "adaptive"}},
    )
    assert out is expected_response
    client.messages.parse.assert_called_once()
    client.messages.create.assert_not_called()


def test_unrelated_400_reraised():
    client = MagicMock()
    client.messages.parse.side_effect = _make_unrelated_400()
    with pytest.raises(anthropic.BadRequestError):
        parse_with_fallback(
            client=client, model="x", max_tokens=100,
            system=[], messages=[], output_format=_SmallResult,
        )
    client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# Fallback triggers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "The compiled grammar is too large, which would cause performance issues.",
    "Grammar compilation timed out.",
    "compiled grammar is too large",
    "Grammar Compilation failed",
])
def test_fallback_triggers_on_known_grammar_errors(message):
    client = MagicMock()
    client.messages.parse.side_effect = _make_grammar_400(message)
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": True, "note": "ok"}
    )
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert out.parsed_output.is_clear is True
    client.messages.create.assert_called_once()


def test_fallback_validates_returned_json_through_pydantic():
    client = MagicMock()
    client.messages.parse.side_effect = _make_grammar_400("compiled grammar is too large")
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": False, "note": "needs more info"}
    )
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert isinstance(out.parsed_output, _SmallResult)
    assert out.parsed_output.note == "needs more info"


def test_fallback_drops_thinking_and_output_config():
    """The non-strict fallback path must strip thinking + output_config —
    Anthropic forbids combining them with tool_choice forcing a tool."""
    client = MagicMock()
    client.messages.parse.side_effect = _make_grammar_400("compiled grammar is too large")
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": True}
    )
    parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
        extra={
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "cache_control": {"type": "ephemeral"},
        },
    )
    create_kwargs = client.messages.create.call_args.kwargs
    assert "thinking" not in create_kwargs
    assert "output_config" not in create_kwargs
    # cache_control still passes through
    assert create_kwargs.get("cache_control") == {"type": "ephemeral"}
    # tool_choice forces our tool
    assert create_kwargs["tool_choice"] == {"type": "tool", "name": "emit_result"}


# ---------------------------------------------------------------------------
# Defensive unwrap
# ---------------------------------------------------------------------------


def test_fallback_unwraps_over_nested_input_under_input_key():
    """Some model outputs wrap the result under {"input": {...}} — unwrap."""
    client = MagicMock()
    client.messages.parse.side_effect = _make_grammar_400("compiled grammar is too large")
    client.messages.create.return_value = _stub_tool_use_response(
        {"input": {"is_clear": True, "note": "wrapped"}}
    )
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert out.parsed_output.is_clear is True
    assert out.parsed_output.note == "wrapped"


def test_fallback_unwraps_under_arguments_key():
    client = MagicMock()
    client.messages.parse.side_effect = _make_grammar_400("compiled grammar is too large")
    client.messages.create.return_value = _stub_tool_use_response(
        {"arguments": {"is_clear": True, "note": "another wrap"}}
    )
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert out.parsed_output.note == "another wrap"


def test_fallback_does_not_unwrap_when_top_level_is_correct():
    """Don't unwrap when the dict already has the required schema fields."""
    client = MagicMock()
    client.messages.parse.side_effect = _make_grammar_400("compiled grammar is too large")
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": True, "note": "x"}
    )
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert out.parsed_output.note == "x"


# ---------------------------------------------------------------------------
# Fallback errors
# ---------------------------------------------------------------------------


def test_fallback_raises_when_no_tool_use_block():
    client = MagicMock()
    client.messages.parse.side_effect = _make_grammar_400("compiled grammar is too large")
    raw = MagicMock()
    raw.content = []  # no tool_use block
    raw.stop_reason = "end_turn"
    client.messages.create.return_value = raw
    with pytest.raises(StructuredOutputFallbackError) as ei:
        parse_with_fallback(
            client=client, model="x", max_tokens=100,
            system=[], messages=[], output_format=_SmallResult,
        )
    assert "no `emit_result` tool_use block" in str(ei.value)


def test_fallback_coerces_frozenset_repr_strings_to_lists():
    """The model sometimes emits Python ``frozenset({'x','y'})`` strings for
    fields the schema declares as arrays/sets. Pydantic then iterates the
    string char-by-char, silently producing garbage. The coercion pass
    catches this before validation."""
    from puzzleeval.structured_output import _coerce_repr_strings_to_lists
    out = _coerce_repr_strings_to_lists(
        {"covers_step_ids": "frozenset({'step_1', 'step_2'})"}
    )
    assert out == {"covers_step_ids": ["step_1", "step_2"]}


def test_fallback_coerces_set_repr_strings():
    from puzzleeval.structured_output import _coerce_repr_strings_to_lists
    out = _coerce_repr_strings_to_lists({"tags": "set({'a','b'})"})
    assert out == {"tags": ["a", "b"]}


def test_fallback_coerces_empty_collection_reprs():
    from puzzleeval.structured_output import _coerce_repr_strings_to_lists
    assert _coerce_repr_strings_to_lists({"x": "frozenset()"}) == {"x": []}
    assert _coerce_repr_strings_to_lists({"x": "set()"}) == {"x": []}


def test_fallback_coerce_walks_nested_lists():
    from puzzleeval.structured_output import _coerce_repr_strings_to_lists
    out = _coerce_repr_strings_to_lists(
        {"items": [{"covers": "frozenset({'a'})"}]}
    )
    assert out == {"items": [{"covers": ["a"]}]}


def test_fallback_coerce_passes_through_normal_values():
    from puzzleeval.structured_output import _coerce_repr_strings_to_lists
    inp = {"name": "Veryfi", "tags": ["ocr", "invoice"], "count": 7}
    assert _coerce_repr_strings_to_lists(inp) == inp


def test_fallback_shim_response_exposes_expected_attrs():
    """Callers read .parsed_output, .stop_reason, .usage — all must be present."""
    client = MagicMock()
    client.messages.parse.side_effect = _make_grammar_400("compiled grammar is too large")
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": True}
    )
    out = parse_with_fallback(
        client=client, model="claude-test", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert hasattr(out, "parsed_output")
    assert hasattr(out, "stop_reason")
    assert hasattr(out, "usage")
    assert hasattr(out, "id")
    assert hasattr(out, "model")
    assert hasattr(out, "content")
    assert out.stop_reason == "tool_use"
    assert out.usage is not None


# ---------------------------------------------------------------------------
# Transient 5xx / overload retry policy
#
# Real-run regression (2026-04-22, req_011CaKNwVbkGYkwRQPPL8QvT): Agent 3
# test generation crashed with an Anthropic 500 InternalServerError, the
# SDK's built-in 3 retries all fell inside the outage window, and the
# pipeline died. Fix: parse_with_fallback wraps both the strict parse AND
# the non-strict fallback create calls in an exponential-backoff retry
# that survives 30-90s outage windows.
# ---------------------------------------------------------------------------


def _make_500() -> anthropic.InternalServerError:
    """Build a real InternalServerError. SDK rejects bare constructions."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(
        500, request=req,
        json={"type": "error", "error": {"type": "api_error",
                                         "message": "Internal server error"}},
    )
    return anthropic.InternalServerError(
        message="Internal server error", response=resp, body=None,
    )


def test_strict_path_retries_transient_500_then_succeeds(monkeypatch):
    """When Anthropic returns 500 and then succeeds on retry, the caller
    never sees the 500 — it's absorbed by the retry loop."""
    # Skip real time.sleep to keep the test fast
    import puzzleeval.structured_output as so
    # Patch the module-level _sleep indirection (more reliable than
    # patching time.sleep directly — the function exists specifically
    # so tests can override backoff behavior).
    monkeypatch.setattr(so, "_sleep", lambda *_a, **_k: None)

    expected_response = MagicMock(parsed_output=_SmallResult(is_clear=True))
    client = MagicMock()
    # First two calls 500, third succeeds
    client.messages.parse.side_effect = [
        _make_500(),
        _make_500(),
        expected_response,
    ]
    out = parse_with_fallback(
        client=client, model="claude-test", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert out is expected_response
    assert client.messages.parse.call_count == 3


def test_strict_path_gives_up_after_max_transient_retries(monkeypatch):
    """If every attempt hits 500, the last 500 propagates — caller can
    surface a clean error (Agent 3 wraps it as AgentAPIError)."""
    import puzzleeval.structured_output as so
    # Patch the module-level _sleep indirection (more reliable than
    # patching time.sleep directly — the function exists specifically
    # so tests can override backoff behavior).
    monkeypatch.setattr(so, "_sleep", lambda *_a, **_k: None)

    client = MagicMock()
    # All 4 attempts 500 — caller gets the last 500
    client.messages.parse.side_effect = [_make_500() for _ in range(10)]
    with pytest.raises(anthropic.InternalServerError):
        parse_with_fallback(
            client=client, model="claude-test", max_tokens=100,
            system=[], messages=[], output_format=_SmallResult,
        )
    # Exactly _TRANSIENT_RETRY_MAX_ATTEMPTS calls
    assert client.messages.parse.call_count == so._TRANSIENT_RETRY_MAX_ATTEMPTS


def test_non_transient_400_not_retried():
    """A caller-side 400 (non-grammar) is re-raised immediately — no
    point retrying a bad request."""
    client = MagicMock()
    client.messages.parse.side_effect = _make_unrelated_400()
    with pytest.raises(anthropic.BadRequestError):
        parse_with_fallback(
            client=client, model="claude-test", max_tokens=100,
            system=[], messages=[], output_format=_SmallResult,
        )
    # Only one attempt — we don't retry 400s
    assert client.messages.parse.call_count == 1


def test_rate_limit_not_retried_by_this_layer(monkeypatch):
    """429 is handled by call_with_model_fallback, not this layer.
    parse_with_fallback must pass rate limits through untouched so the
    model-fallback ladder can catch them."""
    import puzzleeval.structured_output as so
    # Patch the module-level _sleep indirection (more reliable than
    # patching time.sleep directly — the function exists specifically
    # so tests can override backoff behavior).
    monkeypatch.setattr(so, "_sleep", lambda *_a, **_k: None)

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(
        429, request=req,
        json={"type": "error", "error": {"type": "rate_limit_error",
                                         "message": "throttled"}},
    )
    rate_limit_err = anthropic.RateLimitError(
        message="throttled", response=resp, body=None,
    )
    client = MagicMock()
    client.messages.parse.side_effect = rate_limit_err
    with pytest.raises(anthropic.RateLimitError):
        parse_with_fallback(
            client=client, model="claude-test", max_tokens=100,
            system=[], messages=[], output_format=_SmallResult,
        )
    # Single attempt — not retried at this layer
    assert client.messages.parse.call_count == 1


def test_fallback_path_also_retries_500(monkeypatch):
    """The non-strict fallback path (after a grammar-triggered 400) is
    ALSO wrapped in transient retry — Anthropic could 500 on either path
    independently, and we shouldn't crash the pipeline after recovering
    from the grammar issue."""
    import puzzleeval.structured_output as so
    # Patch the module-level _sleep indirection (more reliable than
    # patching time.sleep directly — the function exists specifically
    # so tests can override backoff behavior).
    monkeypatch.setattr(so, "_sleep", lambda *_a, **_k: None)

    client = MagicMock()
    # Strict path: grammar 400 → triggers fallback
    client.messages.parse.side_effect = _make_grammar_400("grammar is too large")
    # Fallback path: 500 once, then success
    client.messages.create.side_effect = [
        _make_500(),
        _stub_tool_use_response({"is_clear": True}),
    ]
    out = parse_with_fallback(
        client=client, model="claude-test", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert out.parsed_output.is_clear is True
    assert client.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# prefer_non_strict — skip the doomed strict attempt for known-large schemas
#
# Real-run regression (trace 73a9d605, 2026-04-25): every Agent 2 + Agent 4
# run hit the strict-grammar 400, logged the fallback warning, then ran the
# non-strict path successfully. The strict attempt costs ~30-60s + one wasted
# API call per run. Agents whose schemas are known-too-large can pass
# `prefer_non_strict=True` to skip the doomed strict attempt and go directly
# to the non-strict path. Agents 2 + 4 set this; Agents 1, 3, and the
# Agent 5 evaluator keep the default (False) since their schemas fit strict.
# ---------------------------------------------------------------------------


def test_prefer_non_strict_true_skips_strict_attempt():
    """When prefer_non_strict=True, the strict messages.parse() must NOT be
    called at all — go directly to non-strict tool path."""
    client = MagicMock()
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": True, "note": "from non-strict"}
    )
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
        prefer_non_strict=True,
    )
    # Strict path NEVER attempted
    client.messages.parse.assert_not_called()
    # Non-strict path ran exactly once
    client.messages.create.assert_called_once()
    # Output shape unchanged from the lazy-fallback path
    assert out.parsed_output.is_clear is True
    assert out.parsed_output.note == "from non-strict"


def test_prefer_non_strict_default_false_preserves_legacy_behavior():
    """Default (omitted/False) must keep the original behavior: try strict
    first, fall back only on grammar 400. Agents 1/3/5 rely on this."""
    expected_response = MagicMock(parsed_output=_SmallResult(is_clear=True))
    client = MagicMock()
    client.messages.parse.return_value = expected_response
    # Call without the flag — should match legacy strict-first behavior
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
    )
    assert out is expected_response
    client.messages.parse.assert_called_once()
    client.messages.create.assert_not_called()


def test_prefer_non_strict_true_drops_thinking_and_output_config():
    """The non-strict path is incompatible with thinking + output_config
    (Anthropic forbids combining them with forced tool_choice). The
    prefer_non_strict path must apply the same stripping the lazy
    fallback already does, so callers can pass these kwargs unconditionally
    without branching on the flag."""
    client = MagicMock()
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": True}
    )
    parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
        extra={
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "cache_control": {"type": "ephemeral"},
        },
        prefer_non_strict=True,
    )
    create_kwargs = client.messages.create.call_args.kwargs
    assert "thinking" not in create_kwargs
    assert "output_config" not in create_kwargs
    # cache_control still passes through (compatible with tool_choice)
    assert create_kwargs.get("cache_control") == {"type": "ephemeral"}
    # tool_choice still forces our tool
    assert create_kwargs["tool_choice"] == {"type": "tool", "name": "emit_result"}


def test_prefer_non_strict_true_returns_same_shim_shape_as_lazy_fallback():
    """The output shim from the eager non-strict path must have the same
    attributes as the lazy-fallback path produces. Callers read
    .parsed_output / .stop_reason / .usage / .id / .model / .content
    without branching on which path produced it."""
    client = MagicMock()
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": True, "note": "ok"}
    )
    out = parse_with_fallback(
        client=client, model="claude-test", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
        prefer_non_strict=True,
    )
    assert hasattr(out, "parsed_output")
    assert hasattr(out, "stop_reason")
    assert hasattr(out, "usage")
    assert hasattr(out, "id")
    assert hasattr(out, "model")
    assert hasattr(out, "content")
    assert out.stop_reason == "tool_use"
    assert out.usage is not None
    assert isinstance(out.parsed_output, _SmallResult)


def test_prefer_non_strict_true_retries_500_on_non_strict_path(monkeypatch):
    """The transient-5xx retry must apply on the eager non-strict path
    too — Anthropic can blip on this path just like on the lazy fallback."""
    import puzzleeval.structured_output as so
    monkeypatch.setattr(so, "_sleep", lambda *_a, **_k: None)

    client = MagicMock()
    client.messages.create.side_effect = [
        _make_500(),
        _stub_tool_use_response({"is_clear": True}),
    ]
    out = parse_with_fallback(
        client=client, model="claude-test", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
        prefer_non_strict=True,
    )
    assert out.parsed_output.is_clear is True
    # Strict still NEVER attempted
    client.messages.parse.assert_not_called()
    # Non-strict path: one 500 retry + success
    assert client.messages.create.call_count == 2


def test_agent_2_call_site_uses_prefer_non_strict():
    """Source-grep regression guard: research.py's Agent 2 structuring
    call site must pass prefer_non_strict=True. If a future refactor
    drops this flag, every Agent 2 run silently regresses to paying
    the doomed strict attempt cost.

    `rfind` is intentional — the file's docstrings reference
    `output_format=Agent2Result` in the CORE LINES GUIDE comment block,
    but the actual call site is the LAST occurrence (in the structuring
    step's call to parse_with_fallback).
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "puzzleeval" / "agents" /
           "agent2" / "core.py").read_text(encoding="utf-8")
    idx = src.rfind("output_format=Agent2Result")
    assert idx != -1, "Agent 2 structuring call site moved or renamed"
    nearby = src[idx:idx + 500]
    assert "prefer_non_strict=True" in nearby, (
        "Agent 2 structuring call must set prefer_non_strict=True — "
        "Agent2Result reliably overflows the compiled-grammar budget."
    )


def test_agent_4_call_site_uses_prefer_non_strict():
    """Source-grep regression guard for Agent 4 — same reasoning as
    Agent 2. Agent4Result with the BuildReadinessChecklist additions
    is even larger. `rfind` is intentional — see the Agent 2 test."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "puzzleeval" / "agents" /
           "agent4" / "core.py").read_text(encoding="utf-8")
    idx = src.rfind("output_format=Agent4Result")
    assert idx != -1, "Agent 4 structuring call site moved or renamed"
    nearby = src[idx:idx + 500]
    assert "prefer_non_strict=True" in nearby, (
        "Agent 4 structuring call must set prefer_non_strict=True — "
        "Agent4Result + BuildReadinessChecklist overflows the compiled-grammar "
        "budget."
    )


# ---------------------------------------------------------------------------
# Pydantic ValidationError defense (item Q2 of the optimization pass)
#
# Real-run failure mode: when the model emits a tool_use block whose
# `input` dict has structurally-bad data (wrong field type, missing
# required field) that the over-nest unwrap + repr-string coerce
# defenses don't catch, `TypeAdapter(...).validate_python()` raises
# `pydantic.ValidationError`. Before this fix the error bubbled up
# uncaught, crashing the agent. After this fix it's wrapped as
# `StructuredOutputFallbackError` so the agent's existing
# AgentOutputError mapping (research.py / screening.py) handles it.
# ---------------------------------------------------------------------------


def test_pydantic_validation_failure_raises_structured_output_fallback_error():
    """Wrong field types / missing required fields → wrapped as
    StructuredOutputFallbackError (NOT bare pydantic.ValidationError)."""
    client = MagicMock()
    # tool_input has the wrong type for `is_clear` (str instead of bool)
    # AND a non-existent field. Pydantic's lax mode coerces "true" → True
    # so to GUARANTEE failure we use a value that can't be coerced.
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": "this is a sentence not a bool"}
    )
    with pytest.raises(StructuredOutputFallbackError) as ei:
        parse_with_fallback(
            client=client, model="x", max_tokens=100,
            system=[], messages=[], output_format=_SmallResult,
            prefer_non_strict=True,
        )
    msg = str(ei.value)
    assert "pydantic validation failed" in msg
    assert "_SmallResult" in msg


def test_pydantic_validation_failure_includes_schema_name_in_error():
    """Caller can grep the error to see WHICH schema failed validation —
    helpful when multiple agents share the StructuredOutputFallbackError
    handler."""
    from pydantic import BaseModel as _BM

    class CustomNamedSchema(_BM):
        x: int

    client = MagicMock()
    client.messages.create.return_value = _stub_tool_use_response(
        {"x": "not_an_int_and_not_coercible"}
    )
    with pytest.raises(StructuredOutputFallbackError) as ei:
        parse_with_fallback(
            client=client, model="x", max_tokens=100,
            system=[], messages=[], output_format=CustomNamedSchema,
            prefer_non_strict=True,
        )
    assert "CustomNamedSchema" in str(ei.value)


def test_pydantic_validation_chained_from_original_exception():
    """The original pydantic.ValidationError must be chained as the
    `__cause__` so log captures (and `raise from`) preserve the full
    trace for debugging."""
    client = MagicMock()
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": "bogus_string"}
    )
    try:
        parse_with_fallback(
            client=client, model="x", max_tokens=100,
            system=[], messages=[], output_format=_SmallResult,
            prefer_non_strict=True,
        )
    except StructuredOutputFallbackError as e:
        # Must have a chained cause — `from exc` in the implementation
        assert e.__cause__ is not None
        # The cause is the pydantic ValidationError (or any Exception subclass)
        assert isinstance(e.__cause__, Exception)


def test_well_formed_input_still_parses_through_normally():
    """The Pydantic-defense wrapping must NOT regress the happy path —
    valid input still parses to a real model instance."""
    client = MagicMock()
    client.messages.create.return_value = _stub_tool_use_response(
        {"is_clear": True, "note": "ok"}
    )
    out = parse_with_fallback(
        client=client, model="x", max_tokens=100,
        system=[], messages=[], output_format=_SmallResult,
        prefer_non_strict=True,
    )
    assert isinstance(out.parsed_output, _SmallResult)
    assert out.parsed_output.is_clear is True
    assert out.parsed_output.note == "ok"
