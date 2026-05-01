"""Helper-level tests for puzzleeval.agents.agent5.api_call (Phase 4 Path B Step 1).

These tests pin the contract of ``make_builder_api_call`` in isolation —
no full pipeline, no Agent 5 input setup. They mock the Anthropic client
at the boundary and assert what the helper does with each exception
class. Behavior-pinning tests in test_build_loop_behavior.py cover the
end-to-end integration.

Why both layers exist:
  * Helper unit tests (this file) — fast, precise per-branch coverage.
  * Behavior-pinning tests (test_build_loop_behavior.py) — cover the
    full call site wiring (caller passes the right context, handles
    APICallSuccess/APICallFailure correctly).

Together they form defense-in-depth around the API-call boundary
extraction.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

from puzzleeval.agents.agent5.api_call import (
    APICallFailure,
    APICallSuccess,
    BUILDER_BETAS,
    BuilderAPICallContext,
    PTL_MARKERS,
    _build_context_management_edits,
    _is_ptl_error,
    make_builder_api_call,
    sanitize_messages_for_anthropic,
)
from puzzleeval.schemas import FailedHarness


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_rate_limit(message="rate limit"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req)
    return anthropic.RateLimitError(
        message, response=resp, body={"error": {"message": message}}
    )


def _make_bad_request(message):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.BadRequestError(
        message, response=resp, body={"error": {"message": message}}
    )


def _make_status_error(message="500 internal"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(500, request=req)
    return anthropic.APIStatusError(
        message, response=resp, body={"error": {"message": message}}
    )


def _make_connection_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=req)


def _make_candidate(name="TestCand", provider="TestProvider"):
    """Minimal ScreenedCandidate-like mock — only needs .name + .provider."""
    cand = MagicMock()
    cand.name = name
    cand.provider = provider
    return cand


def _make_ctx(
    *,
    client=None,
    max_retries=3,
    current_max_tokens=8192,
    sandbox_dir=None,
    apply_breakpoint=None,
    read_harness=None,
):
    """Build a BuilderAPICallContext with sensible defaults for tests."""
    if client is None:
        client = MagicMock()
    if sandbox_dir is None:
        sandbox_dir = Path("/tmp/test_sandbox_dummy")
    if apply_breakpoint is None:
        apply_breakpoint = MagicMock()
    if read_harness is None:
        read_harness = MagicMock(return_value=None)
    return BuilderAPICallContext(
        client=client,
        current_model="claude-sonnet-4-6",
        current_max_tokens=current_max_tokens,
        messages=[{"role": "user", "content": "hello"}],
        system_text="you are a builder",
        tools=[],
        output_config=None,
        max_retries=max_retries,
        candidate=_make_candidate(),
        candidate_label="testcand",
        sandbox_dir=sandbox_dir,
        trace_id="test-trace-001",
        turn=0,
        accumulated_cost=0.5,
        candidate_web_fetch_blocks=2,
        read_harness_code=read_harness,
        apply_message_cache_breakpoint=apply_breakpoint,
        logger=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Pure-function tests: _is_ptl_error + PTL_MARKERS contract
# ---------------------------------------------------------------------------


class TestIsPtlError:
    """The PTL detector is a pure function. Test each marker AND the
    false-positive cases together."""

    @pytest.mark.parametrize("message", [
        "Prompt is too long",
        "PROMPT IS TOO LONG",
        "prompt too long",
        "Maximum context length exceeded",
        "max_tokens exceeded",
        "context_length_exceeded",
    ])
    def test_recognizes_known_ptl_markers_case_insensitive(self, message):
        exc = _make_bad_request(message)
        assert _is_ptl_error(exc), f"Should recognize {message!r} as PTL"

    @pytest.mark.parametrize("message", [
        # Real false-positive from real_debug_3 trace: a 400 mentioning
        # 'context_management' alone must NOT trigger PTL retry.
        "Invalid context_management.edits[0]: schema validation failed",
        "Schema validation error on context-management",
        "Tool input validation failed",
        "model not found",
        "permission denied",
    ])
    def test_rejects_non_ptl_400s(self, message):
        exc = _make_bad_request(message)
        assert not _is_ptl_error(exc), f"Should NOT classify {message!r} as PTL"

    def test_ptl_markers_is_immutable_tuple(self):
        """Frozen at module level so accidental .append() can't drift."""
        assert isinstance(PTL_MARKERS, tuple)
        assert len(PTL_MARKERS) == 5  # locked count


# ---------------------------------------------------------------------------
# Dataclass shape contracts
# ---------------------------------------------------------------------------


class TestDataclassShapes:
    """Frozen dataclasses prevent accidental mutation across the helper
    boundary. Outcome union is the explicit success/failure signal."""

    def test_context_is_frozen(self):
        ctx = _make_ctx()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.current_model = "different-model"

    def test_success_is_frozen(self):
        outcome = APICallSuccess(response=MagicMock())
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.response = "different"

    def test_failure_is_frozen(self):
        fh = FailedHarness(
            candidate_name="x", provider="y", failure_reason="z",
            failure_category="unknown", partial_code=None,
            turns_attempted=0, web_fetch_blocks=0, build_cost_usd=0.0,
        )
        outcome = APICallFailure(failed_harness=fh)
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.failed_harness = "different"


# ---------------------------------------------------------------------------
# Context management edits ordering contract (real-run e21f6077)
# ---------------------------------------------------------------------------


class TestContextManagementEdits:
    """The edits list MUST start with clear_thinking_20251015 — the API
    rejects any other ordering with a misleading error. This is the
    regression guard ``test_clear_thinking_edit_is_first_in_edits_list``
    promises in production."""

    def test_clear_thinking_is_first(self):
        edits = _build_context_management_edits()
        assert edits[0]["type"] == "clear_thinking_20251015"

    def test_clear_thinking_has_keep_all(self):
        edits = _build_context_management_edits()
        assert edits[0]["keep"] == "all"

    def test_includes_three_edit_types(self):
        edits = _build_context_management_edits()
        types = [e["type"] for e in edits]
        assert "clear_thinking_20251015" in types
        assert "clear_tool_uses_20250919" in types
        assert "compact_20260112" in types

    def test_clear_tool_uses_excludes_high_value_tools(self):
        edits = _build_context_management_edits()
        clear_edit = next(e for e in edits if e["type"] == "clear_tool_uses_20250919")
        # write_file/patch_file/advisor must NEVER be cleared — Claude
        # needs to see its edit history + advisor verdicts are rare.
        for tool in ("write_file", "patch_file", "advisor"):
            assert tool in clear_edit["exclude_tools"], (
                f"{tool} must be excluded from clear_tool_uses (high-value)"
            )


# ---------------------------------------------------------------------------
# Happy-path: API call returns a response → APICallSuccess
# ---------------------------------------------------------------------------


class TestSuccessPath:
    """When call_with_model_fallback returns a response, helper returns
    APICallSuccess(response). No retries, no failure path."""

    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_success_returns_api_call_success(self, mock_fallback):
        mock_response = MagicMock()
        mock_fallback.return_value = mock_response
        ctx = _make_ctx()
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallSuccess)
        assert outcome.response is mock_response

    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_success_called_with_primary_model_and_betas(self, mock_fallback):
        mock_fallback.return_value = MagicMock()
        ctx = _make_ctx()
        make_builder_api_call(ctx)
        # call_with_model_fallback was called with primary_model + trace_id
        kw = mock_fallback.call_args.kwargs
        assert kw["primary_model"] == "claude-sonnet-4-6"
        assert kw["trace_id"] == "test-trace-001"
        assert "TestCand" in kw["operation_label"]


class TestMessageSanitizer:
    def test_drops_empty_text_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "   "},
            {"role": "user", "content": [{"type": "text", "text": ""}]},
        ]
        logger = MagicMock()
        repaired = sanitize_messages_for_anthropic(
            messages,
            logger=logger,
            trace_id="trace",
            candidate_name="Candidate",
        )
        assert repaired == 2
        assert messages == [{"role": "user", "content": "hello"}]
        logger.warning.assert_called_once()

    def test_repairs_empty_tool_result_content(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": ""},
                ],
            }
        ]
        repaired = sanitize_messages_for_anthropic(
            messages,
            logger=MagicMock(),
            trace_id="trace",
            candidate_name="Candidate",
        )
        assert repaired == 1
        block = messages[0]["content"][0]
        assert block["tool_use_id"] == "toolu_1"
        assert "tool returned no content" in block["content"]
        assert block["is_error"] is True


# ---------------------------------------------------------------------------
# PTL recovery branch
# ---------------------------------------------------------------------------


class TestPtlRecovery:
    """When call_with_model_fallback raises a BadRequestError matching a
    PTL marker, helper halves max_tokens and retries (up to max_retries
    times). After exhaustion, returns APICallFailure(unknown)."""

    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_ptl_then_success_recovers(self, mock_fallback):
        mock_response = MagicMock()
        mock_fallback.side_effect = [_make_bad_request("prompt is too long"), mock_response]
        ctx = _make_ctx(current_max_tokens=8192)
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallSuccess)
        assert mock_fallback.call_count == 2

    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_ptl_exhaustion_returns_failure_unknown(self, mock_fallback):
        # max_retries=2 → 3 attempts total, all PTL → fail
        mock_fallback.side_effect = [_make_bad_request("max_tokens")] * 4
        ctx = _make_ctx(max_retries=2)
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallFailure)
        assert outcome.failed_harness.failure_category == "unknown"
        assert "API bad request" in outcome.failed_harness.failure_reason

    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_non_ptl_400_fails_immediately(self, mock_fallback):
        # A 400 NOT matching any PTL marker → fatal on first attempt
        mock_fallback.side_effect = _make_bad_request(
            "Schema validation failed on context_management"
        )
        ctx = _make_ctx()
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallFailure)
        assert outcome.failed_harness.failure_category == "unknown"
        # No retry: only ONE call made
        assert mock_fallback.call_count == 1

    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_failed_harness_carries_partial_code_from_callback(self, mock_fallback):
        """Per the dataclass contract, partial_code comes from the caller-
        injected read_harness_code function. Verify the wiring."""
        mock_fallback.side_effect = _make_bad_request("schema invalid")
        read_harness = MagicMock(return_value="def run(): pass\n")
        ctx = _make_ctx(read_harness=read_harness)
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallFailure)
        assert outcome.failed_harness.partial_code == "def run(): pass\n"


# ---------------------------------------------------------------------------
# Rate-limit branch
# ---------------------------------------------------------------------------


class TestRateLimitRetry:
    """Rate limit → exponential backoff [15, 30, 60] → exhaustion =
    APICallFailure(build_timeout)."""

    @patch("puzzleeval.agents.agent5.api_call.time.sleep")
    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_one_shot_recovery(self, mock_fallback, mock_sleep):
        mock_fallback.side_effect = [_make_rate_limit(), MagicMock()]
        ctx = _make_ctx()
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallSuccess)
        # First retry sleeps 15 seconds
        mock_sleep.assert_called_once_with(15)

    @patch("puzzleeval.agents.agent5.api_call.time.sleep")
    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_exponential_backoff_15_30_60(self, mock_fallback, mock_sleep):
        # max_retries=3 → 4 total attempts. The first 3 sleeps fire
        # before retries 1, 2, 3. The 4th attempt (retry index 3) hits
        # the `if retry < max_retries` False branch → no sleep, fatal.
        mock_fallback.side_effect = [_make_rate_limit()] * 4
        ctx = _make_ctx(max_retries=3)
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallFailure)
        sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_args == [15, 30, 60]

    @patch("puzzleeval.agents.agent5.api_call.time.sleep")
    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_exhaustion_returns_build_timeout(self, mock_fallback, mock_sleep):
        mock_fallback.side_effect = [_make_rate_limit("rate limit hit")] * 4
        ctx = _make_ctx(max_retries=3)
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallFailure)
        assert outcome.failed_harness.failure_category == "build_timeout"
        assert "rate limit" in outcome.failed_harness.failure_reason.lower()


# ---------------------------------------------------------------------------
# APIConnectionError + APIStatusError branch
# ---------------------------------------------------------------------------


class TestApiConnectionAndStatusErrors:
    """Connection or status errors → fatal on first attempt (no retry).
    failure_category is "unknown"."""

    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_api_connection_error_fails_immediately(self, mock_fallback):
        mock_fallback.side_effect = _make_connection_error()
        ctx = _make_ctx()
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallFailure)
        assert outcome.failed_harness.failure_category == "unknown"
        assert mock_fallback.call_count == 1

    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_api_status_error_fails_immediately(self, mock_fallback):
        mock_fallback.side_effect = _make_status_error("502 bad gateway")
        ctx = _make_ctx()
        outcome = make_builder_api_call(ctx)
        assert isinstance(outcome, APICallFailure)
        assert outcome.failed_harness.failure_category == "unknown"


# ---------------------------------------------------------------------------
# Message cache breakpoint mutation contract
# ---------------------------------------------------------------------------


class TestMessageCacheBreakpoint:
    """The caller-injected breakpoint mutator MUST be called once per API
    attempt (when CACHE_MESSAGES_ENABLED). Pin both the call AND the
    argument identity (it must mutate the actual ctx.messages list)."""

    @patch("puzzleeval.agents.agent5.api_call.CACHE_MESSAGES_ENABLED", True)
    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_breakpoint_called_when_cache_enabled(self, mock_fallback):
        mock_fallback.return_value = MagicMock()
        breakpoint_fn = MagicMock()
        ctx = _make_ctx(apply_breakpoint=breakpoint_fn)
        make_builder_api_call(ctx)
        breakpoint_fn.assert_called_once_with(ctx.messages)

    @patch("puzzleeval.agents.agent5.api_call.CACHE_MESSAGES_ENABLED", False)
    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    def test_breakpoint_skipped_when_cache_disabled(self, mock_fallback):
        mock_fallback.return_value = MagicMock()
        breakpoint_fn = MagicMock()
        ctx = _make_ctx(apply_breakpoint=breakpoint_fn)
        make_builder_api_call(ctx)
        breakpoint_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Beta features locked
# ---------------------------------------------------------------------------


class TestBuilderBetas:
    """The Anthropic betas Agent 5 needs are pinned at module level so
    accidentally dropping one (e.g., advisor-tool-2026-03-01) breaks
    visibly + the regression guard catches it."""

    def test_required_betas_present(self):
        for beta in (
            "context-management-2025-06-27",
            "compact-2026-01-12",
            "advisor-tool-2026-03-01",
        ):
            assert beta in BUILDER_BETAS, f"{beta} must remain in BUILDER_BETAS"
