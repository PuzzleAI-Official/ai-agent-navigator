"""Behavior-pinning tests for the Agent 5 build loop (Phase 4 Path B prep).

These tests lock the OUTPUT and STATE-EVOLUTION contracts of
``_build_single_harness`` so that subsequent dispatcher refactors
(Phase 4 Path B) cannot silently regress behavior.

Pattern — based on the existing tests/test_agent5.py::TestIntegration:
  1. Mock anthropic.Anthropic to return a controlled response sequence
  2. Run the full Agent 5 pipeline (which calls _build_single_harness internally)
  3. Assert the contract: result shape, state evolution, side effects

Why these tests are the right safety net for Path B:
  * They exercise the function END-TO-END (not just inspect.getsource)
  * They lock measurable contracts (turn count, cost, smoke flag)
  * They run in <2 seconds (mocked client + tmp_path filesystem)
  * They catch dispatcher refactor bugs before production sees them

What these tests do NOT cover (acknowledged gaps):
  * Real Anthropic API behavior (rate limits, partial responses)
  * Real subprocess execution (smoke tests, pip install)
  * Real Agent 5 build full lifecycle (would need ~$5 in API costs per run)

When the dispatcher is extracted in Path B, ALL these tests must pass.
A failure means the refactor changed observable behavior.
"""

from __future__ import annotations

import importlib

from unittest.mock import MagicMock, patch

import pytest


# Reuse fixtures from test_agent5.py — they're already battle-tested.
from tests.test_agent5 import (
    _make_screened_candidate,
    _make_user_understanding,
    _make_test_cases,
)
from puzzleeval.schemas import Agent5Input


@pytest.fixture(autouse=True)
def _disable_phase1_scaffold_gate_for_legacy_mocks(monkeypatch):
    """Pre-existing build-loop tests use minimal mocks that write harness.py
    directly without first writing api_spec.txt. They predate gate B3 and
    test OTHER invariants (output shape, cost accumulation, conversation
    log, verification gate, retry behavior).

    Gate B3's correctness is verified separately in
    `tests/test_agent5_write_file_gates.py`. Here we disable it via the
    documented env-var bypass so these legacy mock flows continue to
    exercise the build loop's higher-level contracts.

    These tests also do not cover real venv creation or dependency
    installation. Stubbing venv setup keeps the suite focused on build-loop
    state evolution instead of paying ~4-10 seconds per mocked candidate on
    Windows/Anaconda.
    """
    monkeypatch.setenv("PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK", "0")
    monkeypatch.setenv("PUZZLEEVAL_GATE_FORENSICS_COVERAGE", "0")
    monkeypatch.setenv("PUZZLEEVAL_VENV_PREINSTALL", "0")
    # PR 1 (autonomy artifacts) — these tests pin the LEGACY build-loop
    # behavior (PHASE2_DIRECTIVE injection, message accumulation through
    # the model transition). The autonomy layer is verified separately by
    # tests/test_autonomy_*.py. Disable both flags here so legacy contracts
    # remain pinned. Tests that explicitly want the new behavior re-enable
    # them via their own monkeypatch.
    monkeypatch.setenv("PUZZLEEVAL_GATE_AUTONOMY_ARTIFACTS", "0")
    monkeypatch.setenv("PUZZLEEVAL_CONTEXT_COMPACTION_AT_MODEL_TRANSITION", "0")
    import puzzleeval.config as cfg
    importlib.reload(cfg)
    from puzzleeval.agents import implement_test_env as ite
    from puzzleeval.agents.agent5 import sandbox as agent5_sandbox

    def _fast_create_venv(*args, **kwargs):
        return True

    monkeypatch.setattr(ite, "_create_venv", _fast_create_venv)
    monkeypatch.setattr(agent5_sandbox, "create_venv", _fast_create_venv)
    yield
    # Reset for the next test (autouse fixture re-runs each test).


def _make_mock_response(content_blocks, stop_reason="end_turn",
                        input_tokens=5000, output_tokens=1000):
    """Create a mock API response with usage stats."""
    mock = MagicMock()
    mock.content = content_blocks
    mock.stop_reason = stop_reason
    mock.usage.input_tokens = input_tokens
    mock.usage.output_tokens = output_tokens
    mock.usage.cache_creation_input_tokens = 0
    mock.usage.cache_read_input_tokens = 0
    mock.usage.server_tool_use = None
    mock.usage.iterations = None
    return mock


def _make_text_block(text):
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(name, input_data, tool_id="tool_1"):
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_data
    block.id = tool_id
    return block


def _make_input_with_one_candidate(name="TestService"):
    """Build a one-candidate Agent5Input with a UNIQUE trace_id per call.

    Why unique: Agent 5 builds write to ``runs/<trace_id>/harnesses/<slug>/``
    and the per-sandbox venv lock dict is module-level (R2 invariant).
    Without unique trace_ids, a second test invocation finds the
    pre-existing sandbox + harness.py from the previous run, the
    OpenAPI fastpath sees pre-existing files, and behavior diverges.
    UUID-based trace_id guarantees fresh sandbox per test.
    """
    import uuid
    return Agent5Input(
        validated_candidates=[_make_screened_candidate(name)],
        user_understanding=_make_user_understanding(),
        test_cases=_make_test_cases(),
        trace_id=f"behavior-pin-{name.lower()}-{uuid.uuid4().hex[:8]}",
    )


# ---------------------------------------------------------------------------
# Contract 1: Output shape — TestHarness vs FailedHarness
# ---------------------------------------------------------------------------


class TestOutputShapeContract:
    """The build loop must return TestHarness on success, FailedHarness on
    failure. The dispatcher refactor MUST preserve this binary outcome."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_successful_build_returns_test_harness_with_smoke_passed(
        self, mock_anthropic_cls,
    ):
        """A build that writes harness.py + signals HARNESS_COMPLETE
        must return TestHarness with smoke_test_passed=True."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Turn 1: write harness.py (returns tool_use)
        # Turn 2: signals HARNESS_COMPLETE
        responses = [
            _make_mock_response(
                [
                    _make_text_block("Building."),
                    _make_tool_use_block("write_file", {
                        "filename": "harness.py",
                        "content": (
                            'def run(input_data):\n'
                            '    return {"output": "x", "latency_ms": 1, '
                            '"tokens_used": None, "cost_usd": None, '
                            '"raw_response": {}, "success": True, "error": None}\n'
                        ),
                    }, "tu_1"),
                ],
                stop_reason="tool_use",
            ),
            _make_mock_response(
                [_make_text_block("SMOKE TEST PASSED\nHARNESS_COMPLETE")],
                stop_reason="end_turn",
            ),
        ]
        mock_client.beta.messages.create.side_effect = responses

        result = run_implement_test_env_agent(_make_input_with_one_candidate())

        assert len(result.harnesses) == 1
        assert len(result.failed_harnesses) == 0
        h = result.harnesses[0]
        assert h.candidate_name == "TestService"
        assert h.smoke_test_passed is True
        # build_turns is 1-based: 2 turns elapsed → build_turns = 2
        assert h.build_turns >= 2
        # Cost was accumulated from both turns
        assert h.build_cost_usd > 0

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_harness_failed_signal_returns_failed_harness(
        self, mock_anthropic_cls,
    ):
        """Builder explicitly signaling HARNESS_FAILED must produce
        FailedHarness, not crash. Reason field captures the agent's text."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        fail_response = _make_mock_response(
            [_make_text_block(
                "Could not build. API requires enterprise account. HARNESS_FAILED"
            )],
            stop_reason="end_turn",
        )
        mock_client.beta.messages.create.return_value = fail_response

        result = run_implement_test_env_agent(_make_input_with_one_candidate("BadService"))

        assert len(result.harnesses) == 0
        assert len(result.failed_harnesses) == 1
        f = result.failed_harnesses[0]
        assert f.candidate_name == "BadService"
        # Failure reason captures part of the agent's last text
        assert f.failure_reason  # non-empty


# ---------------------------------------------------------------------------
# Contract 2: Cost accumulation across turns
# ---------------------------------------------------------------------------


class TestCostAccumulationContract:
    """accumulated_cost must grow monotonically across turns and end up
    in the final TestHarness.build_cost_usd. Dispatcher refactor must
    preserve the per-turn cost addition."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_multi_turn_build_accumulates_cost(self, mock_anthropic_cls):
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # 3 turns, each with non-trivial token counts
        responses = [
            _make_mock_response(
                [_make_text_block("turn 1 thinking")],
                stop_reason="end_turn",
                input_tokens=2000, output_tokens=500,
            ),
            _make_mock_response(
                [
                    _make_text_block("writing harness"),
                    _make_tool_use_block("write_file", {
                        "filename": "harness.py",
                        "content": (
                            'def run(input_data):\n'
                            '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                            '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                        ),
                    }, "tu_1"),
                ],
                stop_reason="tool_use",
                input_tokens=3000, output_tokens=1500,
            ),
            _make_mock_response(
                [_make_text_block("SMOKE TEST PASSED\nHARNESS_COMPLETE")],
                stop_reason="end_turn",
                input_tokens=2500, output_tokens=200,
            ),
        ]
        mock_client.beta.messages.create.side_effect = responses

        result = run_implement_test_env_agent(_make_input_with_one_candidate("CostAccum"))

        assert len(result.harnesses) == 1
        # Cost is positive and reflects all 3 turns of token usage
        assert result.harnesses[0].build_cost_usd > 0
        # Total run cost includes this candidate
        assert result.total_build_cost_usd >= result.harnesses[0].build_cost_usd


# ---------------------------------------------------------------------------
# Contract 3: Conversation log shape and turn counting
# ---------------------------------------------------------------------------


class TestConversationLogContract:
    """conversation_log.json must contain one dict per build turn with
    documented fields. The dispatcher refactor's _record_turn_telemetry
    helper must preserve this exact shape."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_conversation_log_has_one_entry_per_turn(
        self, mock_anthropic_cls, tmp_path,
    ):
        """After a 2-turn build, conversation_log.json should have at
        least 2 entries (one per real API turn)."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        import json

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        responses = [
            _make_mock_response(
                [
                    _make_text_block("writing harness"),
                    _make_tool_use_block("write_file", {
                        "filename": "harness.py",
                        "content": (
                            'def run(input_data):\n'
                            '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                            '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                        ),
                    }, "tu_1"),
                ],
                stop_reason="tool_use",
            ),
            _make_mock_response(
                [_make_text_block("SMOKE TEST PASSED\nHARNESS_COMPLETE")],
                stop_reason="end_turn",
            ),
        ]
        mock_client.beta.messages.create.side_effect = responses

        result = run_implement_test_env_agent(_make_input_with_one_candidate("LogShape"))

        # The harness sandbox dir contains conversation_log.json
        assert len(result.harnesses) == 1
        from pathlib import Path
        sandbox_dir = Path(result.harnesses[0].harness_dir)
        log_path = sandbox_dir / "conversation_log.json"
        assert log_path.exists(), f"conversation_log.json not written to {sandbox_dir}"

        log_data = json.loads(log_path.read_text(encoding="utf-8"))
        assert isinstance(log_data, list)
        # At least 2 entries (one per real turn — ignoring bookkeeping rows)
        real_turns = [e for e in log_data if isinstance(e.get("turn"), int)]
        assert len(real_turns) >= 2, (
            f"Expected at least 2 real-turn entries; got {len(real_turns)}"
        )

        # Each real turn has the canonical fields
        for entry in real_turns:
            assert "turn" in entry
            assert "input_tokens" in entry
            assert "output_tokens" in entry
            assert "cost_usd" in entry
            assert "model" in entry
            assert "stop_reason" in entry
            # latency_ms added in NEW-AM
            assert "latency_ms" in entry


# ---------------------------------------------------------------------------
# Contract 4: HARNESS_COMPLETE → verification gate → exit
# ---------------------------------------------------------------------------


class TestVerificationGateContract:
    """The HARNESS_COMPLETE signal triggers verification. If verification
    passes (harness.py exists), the loop exits with TestHarness. If
    harness.py doesn't exist (model lied about completing), the loop
    must NOT exit — it must continue with feedback."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_complete_signal_with_harness_writes_test_harness(
        self, mock_anthropic_cls,
    ):
        """HARNESS_COMPLETE + harness.py written → TestHarness output.
        This is the happy path that locks the verification-gate behavior."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Single combined turn: write harness + signal complete
        combined = _make_mock_response(
            [
                _make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_1"),
            ],
            stop_reason="tool_use",
        )
        complete = _make_mock_response(
            [_make_text_block("SMOKE TEST PASSED\nHARNESS_COMPLETE")],
            stop_reason="end_turn",
        )
        mock_client.beta.messages.create.side_effect = [combined, complete]

        result = run_implement_test_env_agent(_make_input_with_one_candidate("VerifyGate"))

        # Success: TestHarness produced
        assert len(result.harnesses) == 1
        h = result.harnesses[0]
        # smoke_test_passed reflects either real smoke run OR
        # SMOKE TEST PASSED in last_text OR HARNESS_COMPLETE in last_text
        assert h.smoke_test_passed is True

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_complete_signal_routes_through_reflection_gate_when_autonomy_enabled(
        self, mock_anthropic_cls, monkeypatch,
    ):
        """SMOKE TEST PASSED + HARNESS_COMPLETE must not bypass the
        reflection gate when autonomy artifacts are enabled."""
        monkeypatch.setenv("PUZZLEEVAL_GATE_AUTONOMY_ARTIFACTS", "1")
        monkeypatch.setenv("PUZZLEEVAL_GATE_REFLECTION_PHASE_3", "1")
        monkeypatch.setenv("PUZZLEEVAL_GATE_FORENSICS_COVERAGE", "0")
        import puzzleeval.config as cfg
        importlib.reload(cfg)

        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        write_harness = _make_mock_response(
            [
                _make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_1"),
            ],
            stop_reason="tool_use",
        )
        complete_without_reflection = _make_mock_response(
            [_make_text_block("SMOKE TEST PASSED\nHARNESS_COMPLETE")],
            stop_reason="end_turn",
        )
        failed_after_retry = _make_mock_response(
            [_make_text_block("HARNESS_FAILED")],
            stop_reason="end_turn",
        )
        mock_client.beta.messages.create.side_effect = [
            write_harness,
            complete_without_reflection,
            failed_after_retry,
        ]

        result = run_implement_test_env_agent(
            _make_input_with_one_candidate("ReflectionGateE2E")
        )

        assert len(result.harnesses) == 0
        assert len(result.failed_harnesses) == 1

        third_call_messages = mock_client.beta.messages.create.call_args_list[2].kwargs["messages"]
        all_user_text = ""
        for msg in third_call_messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                all_user_text += content
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict):
                        all_user_text += blk.get("text", "")
        assert "reflection_phase_3.md" in all_user_text
        assert "Verification Issues" in all_user_text


# ---------------------------------------------------------------------------
# Contract 5: Failure-mode classification
# ---------------------------------------------------------------------------


class TestFailureCategoryContract:
    """When the build fails, FailedHarness.failure_category must reflect
    the real cause. The dispatcher's _categorize_failure path must be
    preserved."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_no_harness_after_loop_yields_build_timeout(
        self, mock_anthropic_cls,
    ):
        """Loop runs to completion without producing harness.py →
        FailedHarness with failure_category='build_timeout'."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Agent never writes harness.py and never signals
        # HARNESS_COMPLETE / HARNESS_FAILED — just some text every turn
        chatty = _make_mock_response(
            [_make_text_block("thinking about this api...")],
            stop_reason="end_turn",
        )
        # Make every API call return the same chatty response
        mock_client.beta.messages.create.return_value = chatty

        result = run_implement_test_env_agent(_make_input_with_one_candidate("Timeout"))

        # No harness produced → failed_harnesses
        # Note: depending on exact loop guards, this may end via wallclock
        # or turn limit. Either way the contract is the same.
        assert len(result.harnesses) == 0
        assert len(result.failed_harnesses) >= 1
        # Most likely failure category is build_timeout
        f = result.failed_harnesses[0]
        # failure_category should be SOMETHING from the documented enum
        assert f.failure_category in {
            "build_timeout", "auth_blocked", "api_incompatible",
            "dependency_failure", "docs_unusable", "unknown",
        }


# ---------------------------------------------------------------------------
# Contract 6: Total candidates accounting
# ---------------------------------------------------------------------------


class TestTotalCandidatesContract:
    """total_candidates_attempted must match input cardinality regardless
    of success/failure split."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_total_candidates_equals_input_count(self, mock_anthropic_cls):
        """When 1 candidate is passed in, total_candidates_attempted is 1."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Builder produces a complete harness in 1 turn
        complete = _make_mock_response(
            [
                _make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_1"),
                _make_text_block("HARNESS_COMPLETE"),
            ],
            stop_reason="end_turn",
        )
        mock_client.beta.messages.create.return_value = complete

        result = run_implement_test_env_agent(_make_input_with_one_candidate("CountCheck"))

        assert result.total_candidates_attempted == 1
        # Sum of harnesses + failed should equal total
        assert (len(result.harnesses) + len(result.failed_harnesses)
                == result.total_candidates_attempted)


# ===========================================================================
# Phase 4 Path B Step 0 — baseline behavior tests for high-risk extractions.
#
# These tests pin the contracts that ``make_builder_api_call`` (Block A
# extraction) and the dispatch-loop pure helpers (Block B extraction) MUST
# preserve. They land FIRST against the unchanged code; if any fails here,
# classify as ``wrong-expectation`` (test is broken, fix the test) or
# ``latent-bug`` (production code was wrong, STOP and ask).
#
# Each test asserts OBSERVABLE behavior end-to-end through the full
# ``run_implement_test_env_agent`` entry point, mocking the Anthropic client
# at the ``client.beta.messages.create`` boundary.
# ===========================================================================


# ----- Helpers for constructing typed Anthropic exceptions ------------------

def _make_rate_limit_error(message="Rate limit exceeded"):
    """Build a real anthropic.RateLimitError so the typed except clause fires."""
    import anthropic
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req)
    return anthropic.RateLimitError(
        message, response=resp, body={"error": {"message": message}}
    )


def _make_bad_request_error(message):
    """Build a real anthropic.BadRequestError. The error MESSAGE drives PTL
    detection — pass exact strings the production code's ``is_ptl`` check
    looks for to exercise that branch."""
    import anthropic
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.BadRequestError(
        message, response=resp, body={"error": {"message": message}}
    )


def _make_tool_use_response(filename, content="x = 1\n"):
    """A response that calls write_file with the given filename. Useful
    fixture for phase-transition + dispatch tests."""
    return _make_mock_response(
        [_make_tool_use_block("write_file", {
            "filename": filename, "content": content,
        }, "tu_x")],
        stop_reason="tool_use",
    )


def _make_text_response(text, stop_reason="end_turn"):
    return _make_mock_response([_make_text_block(text)], stop_reason=stop_reason)


def _capture_messages_side_effect(responses):
    """Build a side_effect callable that snapshots the messages list at
    each API call and returns sequential responses.

    Why this exists: when you do
    ``mock_client.beta.messages.create.side_effect = [resp1, resp2, ...]``
    and later read ``call_args_list[0].kwargs["messages"]``, MagicMock
    captures a REFERENCE to the messages list — not a snapshot. The
    build loop mutates the same list in place across iterations, so by
    the time you assert, every call_args entry points to the SAME final
    list state. To verify per-call message growth, you have to snapshot
    explicitly at the moment of the call.

    Returns: (side_effect_callable, captured_snapshots_list).
    The list grows as calls happen; each entry is a deep-enough copy
    of the messages list at that call's moment (list-of-dict shallow
    copy is sufficient because we only inspect lengths and roles).
    """
    captured: list[list[dict]] = []
    iter_responses = iter(responses)

    def side_effect(*args, **kwargs):
        msgs = kwargs.get("messages")
        if msgs is None and args:
            msgs = args[0]
        # Snapshot: copy the OUTER list so subsequent appends don't
        # retroactively grow this snapshot. The per-message dicts can
        # stay aliased — we don't mutate them, only count them.
        captured.append(list(msgs or []))
        return next(iter_responses)

    return side_effect, captured


# ===========================================================================
# Block A safety net — API call retry + PTL recovery + rate-limit backoff
# ===========================================================================


class TestAPICallRetryBehavior:
    """The PTL recovery branch in ``_build_single_harness``'s retry loop
    must:
      * detect ALL 5 PTL marker substrings (matched case-insensitive)
      * halve current_max_tokens on detection (with floor at 4096)
      * retry the API call after halving
      * NOT retry on a non-PTL 400 (e.g., schema validation error)
      * fail with FailedHarness(unknown) when retries exhausted
    """

    def _run_with_bad_request(self, mock_anthropic_cls, error_message):
        """Setup helper: raise BadRequestError then a complete response."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.beta.messages.create.side_effect = [
            _make_bad_request_error(error_message),
            _make_tool_use_response("harness.py",
                'def run(input_data):\n'
                '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ]
        return run_implement_test_env_agent(
            _make_input_with_one_candidate("PTLTest")
        ), mock_client

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_ptl_marker_prompt_is_too_long_triggers_retry(self, mock_anthropic_cls):
        """Marker variant 1 — exact spelling 'prompt is too long'."""
        result, mc = self._run_with_bad_request(mock_anthropic_cls, "prompt is too long")
        # PTL was retried, then the build succeeded
        assert mc.beta.messages.create.call_count >= 2
        assert len(result.harnesses) == 1, (
            f"PTL marker should retry; got {result.failed_harnesses}"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_ptl_marker_prompt_too_long_triggers_retry(self, mock_anthropic_cls):
        """Marker variant 2 — 'prompt too long' (no 'is')."""
        result, mc = self._run_with_bad_request(mock_anthropic_cls, "prompt too long")
        assert mc.beta.messages.create.call_count >= 2
        assert len(result.harnesses) == 1

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_ptl_marker_max_tokens_triggers_retry(self, mock_anthropic_cls):
        """Marker variant 3 — 'max_tokens' (output budget exceeded)."""
        result, mc = self._run_with_bad_request(mock_anthropic_cls, "max_tokens exceeded")
        assert mc.beta.messages.create.call_count >= 2

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_ptl_marker_maximum_context_length_triggers_retry(self, mock_anthropic_cls):
        """Marker variant 4 — 'maximum context length'."""
        result, mc = self._run_with_bad_request(mock_anthropic_cls, "Maximum context length exceeded")
        assert mc.beta.messages.create.call_count >= 2

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_ptl_marker_context_length_exceeded_triggers_retry(self, mock_anthropic_cls):
        """Marker variant 5 — 'context_length_exceeded'."""
        result, mc = self._run_with_bad_request(mock_anthropic_cls, "context_length_exceeded")
        assert mc.beta.messages.create.call_count >= 2

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_non_ptl_400_does_not_retry_returns_failed_harness(self, mock_anthropic_cls):
        """False-positive guard from real_debug_3 trace: a 400 mentioning
        'context_management' (e.g., schema validation error on context
        management edit shape) must NOT be classified as PTL — that bug
        burned through the retry budget in microseconds."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Note: message contains 'context_management' but NOT any PTL marker
        mock_client.beta.messages.create.side_effect = _make_bad_request_error(
            "Invalid context_management.edits[0]: schema validation failed"
        )
        result = run_implement_test_env_agent(
            _make_input_with_one_candidate("NonPTL400")
        )
        # No retry — only 1 API call before fatal return
        assert mock_client.beta.messages.create.call_count == 1
        assert len(result.failed_harnesses) == 1
        assert result.failed_harnesses[0].failure_category == "unknown"

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_ptl_retries_exhausted_returns_failed_harness(self, mock_anthropic_cls):
        """After max_retries+1 PTL exceptions, the loop gives up with
        FailedHarness(unknown). max_retries = 3 → 4 total attempts."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Always raise PTL — never succeed
        mock_client.beta.messages.create.side_effect = _make_bad_request_error(
            "prompt is too long"
        )
        result = run_implement_test_env_agent(
            _make_input_with_one_candidate("PTLExhausted")
        )
        # max_retries=3 + 1 initial = 4 attempts
        assert mock_client.beta.messages.create.call_count == 4
        assert len(result.failed_harnesses) == 1


class TestRateLimitRetryBehavior:
    """The RateLimitError branch must:
      * sleep exponentially (15s, 30s, 60s) between retries
      * retry up to max_retries=3 times
      * succeed if the API recovers within the budget
      * return FailedHarness(build_timeout) when retries exhausted
    """

    @patch("puzzleeval.agents.implement_test_env.time.sleep")
    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_one_shot_rate_limit_recovers(self, mock_anthropic_cls, mock_fallback, mock_sleep):
        """One RateLimitError followed by success → harness builds."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # call_with_model_fallback raises RateLimitError once, then returns success
        success = _make_tool_use_response("harness.py",
            'def run(input_data):\n'
            '    return {"output":"x","latency_ms":1,"tokens_used":None,'
            '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
        )
        complete = _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE")
        mock_fallback.side_effect = [
            _make_rate_limit_error(),
            success,
            complete,
        ]
        result = run_implement_test_env_agent(
            _make_input_with_one_candidate("RateLimitOnce")
        )
        # Recovered cleanly
        assert len(result.harnesses) == 1
        # First sleep = 2^0 * 15 = 15 seconds
        sleep_arg = mock_sleep.call_args_list[0].args[0]
        assert sleep_arg == 15

    @patch("puzzleeval.agents.implement_test_env.time.sleep")
    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_exponential_backoff_schedule_15_30_60(self, mock_anthropic_cls, mock_fallback, mock_sleep):
        """Three RateLimitErrors in a row → sleep(15), sleep(30), sleep(60)."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # 4 RateLimitErrors total (initial + 3 retries) → all exhausted
        # Each retry triggers a sleep. The 4th attempt fails without sleeping
        # (no more retries left). So we expect 3 sleep calls: 15, 30, 60.
        mock_fallback.side_effect = [_make_rate_limit_error()] * 4
        result = run_implement_test_env_agent(
            _make_input_with_one_candidate("RateLimitBackoff")
        )
        assert len(result.failed_harnesses) == 1
        # Verify the backoff schedule precisely
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleep_args == [15, 30, 60], f"Expected [15, 30, 60] got {sleep_args}"

    @patch("puzzleeval.agents.implement_test_env.time.sleep")
    @patch("puzzleeval.agents.agent5.api_call.call_with_model_fallback")
    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_persistent_rate_limit_returns_build_timeout(
        self, mock_anthropic_cls, mock_fallback, mock_sleep
    ):
        """Rate limit on every retry → FailedHarness(build_timeout)."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_fallback.side_effect = [_make_rate_limit_error()] * 4
        result = run_implement_test_env_agent(
            _make_input_with_one_candidate("RateLimitFatal")
        )
        assert len(result.failed_harnesses) == 1
        assert result.failed_harnesses[0].failure_category == "build_timeout"
        # Failure reason mentions rate limit + max_retries
        assert "rate limit" in result.failed_harnesses[0].failure_reason.lower()


# ===========================================================================
# Block B safety net — phase transitions, dispatch gates, error patterns
# ===========================================================================


class TestPhaseTransitionTriggers:
    """Phase 1 → Phase 2 transition (Sonnet → Opus model switch) MUST fire
    on these triggers; any change here is a real-bug-risk regression
    (NEW-AM v7 documented the exact failure mode).
    """

    def _run_with_first_tool_call(self, mock_anthropic_cls, tool_name, filename):
        """Helper: first turn calls tool with filename, second turn completes."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.beta.messages.create.side_effect = [
            _make_mock_response(
                [_make_tool_use_block(tool_name, {
                    "filename": filename,
                    # patch_file needs old_string + new_string; harmless for write_file
                    "old_string": "x", "new_string": "y",
                    "content": "spec content\n",
                }, "tu_1")],
                stop_reason="tool_use",
            ),
            # Second turn writes harness.py + completes (so we always get a valid build)
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_2")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ]
        return run_implement_test_env_agent(
            _make_input_with_one_candidate(f"Trans_{tool_name}_{filename.replace('.', '_')}")
        ), mock_client

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_write_file_api_spec_triggers_transition_to_opus(self, mock_anthropic_cls):
        """write_file('api_spec.txt') flips api_spec_written → next turn uses Opus."""
        from puzzleeval.config import AGENT5_BUILDER_MODEL
        _, mc = self._run_with_first_tool_call(mock_anthropic_cls, "write_file", "api_spec.txt")
        # First call uses Sonnet (research model); second call uses Opus
        first_model = mc.beta.messages.create.call_args_list[0].kwargs["model"]
        second_model = mc.beta.messages.create.call_args_list[1].kwargs["model"]
        assert first_model != AGENT5_BUILDER_MODEL, "First turn should be Sonnet/research"
        assert second_model == AGENT5_BUILDER_MODEL, (
            f"After api_spec.txt write, model should be Opus ({AGENT5_BUILDER_MODEL}); "
            f"got {second_model}"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_patch_file_api_spec_triggers_transition_to_opus(self, mock_anthropic_cls):
        """NEW-AM v7: patch_file('api_spec.txt') is the canonical post-pre-render
        augment-completion signal. WITHOUT this trigger, Sonnet ends up
        writing harness.py (real bug from a4860e94)."""
        from puzzleeval.config import AGENT5_BUILDER_MODEL
        # api_spec.txt must exist for patch_file to work; pre-create it
        # via the pre-render path. For the test, write a minimal spec
        # before the loop runs by patching the sandbox setup.
        # Simpler approach: make patch_file fail on missing file BUT
        # the trigger detection happens BEFORE the dispatch result, so
        # the model still switches. Verify via the model arg.
        _, mc = self._run_with_first_tool_call(mock_anthropic_cls, "patch_file", "api_spec.txt")
        second_model = mc.beta.messages.create.call_args_list[1].kwargs["model"]
        assert second_model == AGENT5_BUILDER_MODEL, (
            f"After patch_file('api_spec.txt'), model must switch to Opus; "
            f"got {second_model}"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_write_file_harness_triggers_transition_with_warning(self, mock_anthropic_cls):
        """write_file('harness.py') in Phase 1 is a Phase 1 prompt violation
        BUT must still flip api_spec_written → switch to Opus on next turn.
        The warning is logged separately (not asserted here; that's a
        log-shape concern)."""
        from puzzleeval.config import AGENT5_BUILDER_MODEL
        _, mc = self._run_with_first_tool_call(mock_anthropic_cls, "write_file", "harness.py")
        second_model = mc.beta.messages.create.call_args_list[1].kwargs["model"]
        assert second_model == AGENT5_BUILDER_MODEL

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_write_file_requirements_triggers_transition_with_warning(self, mock_anthropic_cls):
        """write_file('requirements.txt') in Phase 1 also flips the flag."""
        from puzzleeval.config import AGENT5_BUILDER_MODEL
        _, mc = self._run_with_first_tool_call(
            mock_anthropic_cls, "write_file", "requirements.txt"
        )
        second_model = mc.beta.messages.create.call_args_list[1].kwargs["model"]
        assert second_model == AGENT5_BUILDER_MODEL

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_write_file_unrelated_does_NOT_trigger_transition(self, mock_anthropic_cls):
        """write_file('foo.py') — not one of the 3 trigger filenames →
        api_spec_written stays False → next turn STILL Sonnet/research model.

        Negative-case guard against the trigger list silently broadening."""
        from puzzleeval.config import AGENT5_BUILDER_MODEL
        _, mc = self._run_with_first_tool_call(mock_anthropic_cls, "write_file", "foo.py")
        first_model = mc.beta.messages.create.call_args_list[0].kwargs["model"]
        second_model = mc.beta.messages.create.call_args_list[1].kwargs["model"]
        assert first_model == second_model, (
            f"Unrelated write_file should NOT flip transition; both turns "
            f"should use the SAME model (got {first_model} → {second_model})"
        )
        assert second_model != AGENT5_BUILDER_MODEL


# ---------------------------------------------------------------------------
# Phase 2 forcing directive (real-run trace 749b09b1, 2026-04-28)
# ---------------------------------------------------------------------------
#
# When Sonnet patches/writes api_spec.txt, api_spec_written flips True
# AND PHASE2_DIRECTIVE is appended as a user message so Opus's first
# response after the boundary cannot inherit Sonnet's narrative arc.
#
# Symptom these tests pin against: Opus emitting 33 turns of "handing
# off to Phase 2 (Opus)" without realizing IT IS Opus, because Sonnet's
# exit narration ("Now I have all the information... let me patch...")
# locked it into a "someone else is arriving" frame.
class TestPhase2Directive:
    """The PHASE2_DIRECTIVE forcing message MUST be appended to the
    messages list immediately after the api_spec_written transition
    fires (in the SAME turn as the patch_file/write_file). This is
    AD-007 deterministic enforcement of the "Opus must act, not narrate"
    contract — independent of prompt language adherence."""

    @staticmethod
    def _directive_in_messages(messages: list[dict], directive: str) -> bool:
        """True iff the snapshot has a user-message text block containing
        the directive. Helper to keep test bodies focused on assertions."""
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        if directive in block.get("text", ""):
                            return True
            elif isinstance(content, str) and directive in content:
                return True
        return False

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_directive_appended_after_patch_api_spec(self, mock_anthropic_cls):
        """patch_file('api_spec.txt') triggers transition → next API call's
        messages must end with a user-message containing PHASE2_DIRECTIVE."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        from puzzleeval.agents.agent5.build_loop import PHASE2_DIRECTIVE
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        side_effect, snapshots = _capture_messages_side_effect([
            # T0 (Sonnet): patch api_spec.txt — transition fires
            _make_mock_response(
                [_make_tool_use_block("patch_file", {
                    "filename": "api_spec.txt",
                    "old_string": "x", "new_string": "y",
                }, "tu_1")],
                stop_reason="tool_use",
            ),
            # T1 (Opus): writes harness.py
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_2")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ])
        mock_client.beta.messages.create.side_effect = side_effect
        run_implement_test_env_agent(_make_input_with_one_candidate("PD_PatchSpec"))

        # T0 (the patch turn) must NOT contain the directive yet
        assert not self._directive_in_messages(snapshots[0], PHASE2_DIRECTIVE), (
            "Directive must NOT exist before the transition fires. "
            "T0's messages were captured BEFORE the patch_file ran."
        )
        # T1 (the next turn after transition) MUST contain the directive
        assert self._directive_in_messages(snapshots[1], PHASE2_DIRECTIVE), (
            "PHASE2_DIRECTIVE must appear in T1's messages after T0's "
            "patch_file fired the transition. Without this, Opus inherits "
            "Sonnet's exit narration and stalls (trace 749b09b1)."
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_directive_appended_after_write_api_spec(self, mock_anthropic_cls):
        """write_file('api_spec.txt') (the from-scratch path) also fires
        the transition and must inject the directive."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        from puzzleeval.agents.agent5.build_loop import PHASE2_DIRECTIVE
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        side_effect, snapshots = _capture_messages_side_effect([
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "api_spec.txt",
                    "content": "API_SPEC_START\n",
                }, "tu_1")],
                stop_reason="tool_use",
            ),
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_2")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ])
        mock_client.beta.messages.create.side_effect = side_effect
        run_implement_test_env_agent(_make_input_with_one_candidate("PD_WriteSpec"))

        assert not self._directive_in_messages(snapshots[0], PHASE2_DIRECTIVE)
        assert self._directive_in_messages(snapshots[1], PHASE2_DIRECTIVE), (
            "PHASE2_DIRECTIVE must be injected after write_file('api_spec.txt') "
            "transition, same as patch_file('api_spec.txt')."
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_directive_NOT_appended_on_unrelated_tool_call(self, mock_anthropic_cls):
        """A tool call that does NOT trigger the transition (e.g.,
        read_file or write_file on a non-trigger filename) MUST NOT
        cause the directive to appear. Negative-case guard."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        from puzzleeval.agents.agent5.build_loop import PHASE2_DIRECTIVE
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        side_effect, snapshots = _capture_messages_side_effect([
            _make_mock_response(
                [_make_tool_use_block("read_file", {"filename": "api_spec.txt"}, "tu_1")],
                stop_reason="tool_use",
            ),
            # Now Sonnet writes the spec — transition fires here on T1
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "api_spec.txt",
                    "content": "API_SPEC_START\n",
                }, "tu_2")],
                stop_reason="tool_use",
            ),
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_3")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ])
        mock_client.beta.messages.create.side_effect = side_effect
        run_implement_test_env_agent(_make_input_with_one_candidate("PD_NoTriggerThenTrigger"))

        # T1 (after read_file only — no transition yet) must NOT have directive
        assert not self._directive_in_messages(snapshots[1], PHASE2_DIRECTIVE), (
            "Directive must NOT appear before the api_spec_written "
            "transition. read_file('api_spec.txt') on T0 didn't flip the "
            "flag, so T1's messages should not contain the directive."
        )
        # T2 (after write_file('api_spec.txt') on T1 — transition fires) MUST have directive
        assert self._directive_in_messages(snapshots[2], PHASE2_DIRECTIVE), (
            "After T1's write_file('api_spec.txt') flips the transition, "
            "T2's messages MUST include the directive."
        )

    @staticmethod
    def _count_directive(messages: list[dict], directive: str) -> int:
        """Count occurrences of the directive across all user-message
        text blocks in the snapshot."""
        count = 0
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        if directive in block.get("text", ""):
                            count += 1
            elif isinstance(content, str) and directive in content:
                count += 1
        return count

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_directive_appears_only_once(self, mock_anthropic_cls):
        """If the model patches api_spec.txt again on a later turn (legal
        — augmenting an existing spec), api_spec_was_written is already
        True so the directive must NOT re-fire. Otherwise the directive
        accumulates and leaks tokens on every Phase 2 patch."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        from puzzleeval.agents.agent5.build_loop import PHASE2_DIRECTIVE
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        side_effect, snapshots = _capture_messages_side_effect([
            # T0: write api_spec.txt — transition fires
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "api_spec.txt",
                    "content": "API_SPEC_START\n",
                }, "tu_1")],
                stop_reason="tool_use",
            ),
            # T1: write harness.py
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_2")],
                stop_reason="tool_use",
            ),
            # T2: legal Phase 2 augment of api_spec.txt — must NOT re-inject directive
            _make_mock_response(
                [_make_tool_use_block("patch_file", {
                    "filename": "api_spec.txt",
                    "old_string": "x", "new_string": "y",
                }, "tu_3")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ])
        mock_client.beta.messages.create.side_effect = side_effect
        run_implement_test_env_agent(_make_input_with_one_candidate("PD_OnceOnly"))

        # T3's snapshot — the LAST API call sees the cumulative messages
        # state. Count directive occurrences; must be exactly 1
        # (from T0's transition only).
        directive_count = self._count_directive(snapshots[3], PHASE2_DIRECTIVE)
        assert directive_count == 1, (
            f"Directive must appear EXACTLY ONCE (from T0's transition), "
            f"not re-injected on subsequent Phase 2 patches. Count: {directive_count}"
        )


# ---------------------------------------------------------------------------
# end_turn-no-signal: append assistant response so messages[] changes
# (real-run trace 749b09b1, 2026-04-28)
# ---------------------------------------------------------------------------
class TestEndTurnNoSignalAppendsAssistant:
    """When stop_reason=end_turn, no HARNESS_COMPLETE/FAILED signal, and
    no harness.py on disk, the loop must STILL append the assistant
    response to messages so the next iteration sees a different state.
    Without this, identical prompts cycle (cache_read=88,763 stayed
    IDENTICAL across 33 turns of OpenAI Realtime stall)."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_end_turn_no_signal_grows_messages_list(self, mock_anthropic_cls):
        """Two consecutive end_turn-no-signal turns must produce
        DIFFERENT messages-list snapshots between API calls. Specifically
        len(snapshot_at_T1) > len(snapshot_at_T0)."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        side_effect, snapshots = _capture_messages_side_effect([
            # T0: text-only end_turn (no signal, no harness.py)
            _make_text_response("Thinking about the problem."),
            # T1: text-only end_turn again
            _make_text_response("Still thinking."),
            # T2: finally write api_spec.txt
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "api_spec.txt",
                    "content": "API_SPEC_START\n",
                }, "tu_1")],
                stop_reason="tool_use",
            ),
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_2")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ])
        mock_client.beta.messages.create.side_effect = side_effect
        run_implement_test_env_agent(_make_input_with_one_candidate("ET_GrowMessages"))

        # snapshots[0] = messages at T0 call, snapshots[1] = at T1, etc.
        # After T0's end_turn-no-signal, my patch appends the assistant
        # response → T1 sees +1 message.
        assert len(snapshots[1]) > len(snapshots[0]), (
            f"After T0's end_turn-no-signal, messages list MUST grow "
            f"(by at least the assistant response). Got T0={len(snapshots[0])}, "
            f"T1={len(snapshots[1])}. Without this growth, identical prompts "
            f"cycle and the model is locked into its first response pattern."
        )
        assert len(snapshots[2]) > len(snapshots[1]), (
            f"After T1's second end_turn-no-signal, messages must continue "
            f"to grow. Got T1={len(snapshots[1])}, T2={len(snapshots[2])}."
        )


class TestAskResearchPhaseGate:
    """The ask_research Phase 1 gate is a budget-protection invariant.
    Per Codex feedback, each test asserts state AND side-effects:
      * _run_targeted_research was NOT called (no sub-agent spawned)
      * tool_result content contains the BLOCKED message
      * conversation log records BLOCKED
      * accumulated cost did NOT increase from the (suppressed) call
    """

    @patch("puzzleeval.agents.implement_test_env._run_targeted_research")
    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_ask_research_blocked_before_api_spec_written(
        self, mock_anthropic_cls, mock_research,
    ):
        """Phase 1 (no api_spec.txt yet) → ask_research must be blocked."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Have the agent attempt ask_research first, then complete.
        mock_client.beta.messages.create.side_effect = [
            _make_mock_response(
                [_make_tool_use_block("ask_research", {
                    "question": "What auth does this API use?",
                }, "tu_research")],
                stop_reason="tool_use",
            ),
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_h")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ]
        agent_input = _make_input_with_one_candidate("AskResearchBlocked")
        result = run_implement_test_env_agent(agent_input)

        # ASSERTION 1: sub-agent was NOT spawned (no LLM cost charged)
        mock_research.assert_not_called()

        # ASSERTION 2: tool_result content contains the BLOCKED message.
        # The user message after turn 1 carries the gated tool_result.
        # Find the tool_use_id-matching tool_result and assert its content.
        # call_args_list[1] is the SECOND API call, and `messages` was the kwarg.
        second_call_messages = mock_client.beta.messages.create.call_args_list[1].kwargs["messages"]
        gated_results = []
        for msg in second_call_messages:
            content = msg.get("content")
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("tool_use_id") == "tu_research":
                        gated_results.append(blk.get("content", ""))
        assert gated_results, "No tool_result for tu_research in messages"
        assert any("BLOCKED" in r or "Phase 1" in r for r in gated_results), (
            f"Gated ask_research must contain BLOCKED/Phase 1 message; got {gated_results}"
        )

        # ASSERTION 3: conversation_log recorded the BLOCKED tool_result.
        # This proves the diagnostic log surface stays accurate.
        # The conversation log is saved to disk per turn in the sandbox dir.
        # Walk the runs directory to find the just-created log file (test
        # uses a unique trace_id, so finding any matching log is fine).
        import json
        from pathlib import Path
        runs_dir = Path("runs") / agent_input.trace_id
        gated_log_present = False
        if runs_dir.exists():
            for log_path in runs_dir.glob("**/conversation_log.json"):
                try:
                    log = json.loads(log_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                for entry in log:
                    for tr in entry.get("tool_results", []):
                        if (tr.get("tool") == "ask_research"
                            and "BLOCKED" in str(tr.get("result", ""))):
                            gated_log_present = True
                            break
                    if gated_log_present:
                        break
                if gated_log_present:
                    break
        assert gated_log_present, (
            "conversation_log.json must record ask_research → BLOCKED so "
            "operators can audit Phase 1 gate firings"
        )

        # ASSERTION 4: total cost reflects ONLY the API call cost,
        # NOT a phantom research sub-agent charge. The cost should match
        # what the (mocked) API responses report; no $0.10-0.15 research
        # spike that would show up if the sub-agent ran.
        # Tokens-from-mock are 5K input / 1K output per turn → ~$0.03/turn
        # at Sonnet rates. No way the cost exceeds $0.50 for 3 turns.
        if result.harnesses:
            assert result.harnesses[0].build_cost_usd < 0.50, (
                f"build_cost_usd {result.harnesses[0].build_cost_usd} too high — "
                "ask_research sub-agent appears to have been charged"
            )

    @patch("puzzleeval.agents.implement_test_env._run_targeted_research")
    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_ask_research_allowed_after_api_spec_written(
        self, mock_anthropic_cls, mock_research,
    ):
        """After api_spec.txt is written → ask_research is allowed
        (sub-agent IS spawned). This is the positive twin of the gate."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Sub-agent returns a fake answer + cost
        mock_research.return_value = ("Use Bearer auth.", 0.02)
        mock_client.beta.messages.create.side_effect = [
            # Turn 1: write api_spec.txt (flips the gate)
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "api_spec.txt",
                    "content": "ENDPOINTS:\n  POST /v1/foo\n",
                }, "tu_spec")],
                stop_reason="tool_use",
            ),
            # Turn 2: ask_research (now allowed)
            _make_mock_response(
                [_make_tool_use_block("ask_research", {
                    "question": "What auth?",
                }, "tu_r2")],
                stop_reason="tool_use",
            ),
            # Turn 3: write harness.py
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_h")],
                stop_reason="tool_use",
            ),
            # Turn 4: complete
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ]
        run_implement_test_env_agent(
            _make_input_with_one_candidate("AskResearchAllowed")
        )

        # The gate did NOT fire — sub-agent was called
        assert mock_research.called, (
            "After api_spec.txt is written, ask_research must reach the "
            "sub-agent (no Phase 1 gate)"
        )


class TestPatchFileReadGate:
    """The patch_file read-before-patch gate (lives in agent5/tools.py)
    is exercised through the dispatch loop. Pin both branches so the
    extraction of dispatch-loop helpers can't break this contract."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_patch_without_prior_read_returns_stop_message(self, mock_anthropic_cls):
        """patch_file on a file that exists in the sandbox but was NEVER
        touched by the agent (not write_file'd or read_file'd) → tool_result
        starts with STOP. This pins the gate's first branch (last_read is None).

        Setup: pre-create a file in the sandbox out-of-band (simulating a
        file staged by sandbox setup, e.g., the OpenAPI fastpath api_spec.txt).
        Then have the agent attempt patch_file directly.
        """
        from pathlib import Path
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        from puzzleeval.agents.agent5.sandbox import candidate_slug

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Build the input with a known trace_id so we can pre-stage the file.
        agent_input = _make_input_with_one_candidate("PatchNoRead")
        candidate = agent_input.validated_candidates[0]
        slug = candidate_slug(candidate.name)
        sandbox_dir = (
            Path("runs") / agent_input.trace_id / "harnesses" / slug
        )
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        # Pre-stage spec.txt OUT-OF-BAND — agent never sees its creation
        # via write_file, so build_read_state stays empty for this file.
        (sandbox_dir / "spec.txt").write_text("original content\n", encoding="utf-8")

        mock_client.beta.messages.create.side_effect = [
            # Turn 1: agent tries patch_file directly without reading first
            _make_mock_response(
                [_make_tool_use_block("patch_file", {
                    "filename": "spec.txt",
                    "old_string": "original",
                    "new_string": "patched",
                }, "tu_p")],
                stop_reason="tool_use",
            ),
            # Turn 2: write harness.py + complete (so we get a clean exit)
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_h")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ]
        run_implement_test_env_agent(agent_input)

        # The 2nd API call carries the patch_file's tool_result
        second_call_messages = mock_client.beta.messages.create.call_args_list[1].kwargs["messages"]
        gate_msgs = []
        for msg in second_call_messages:
            content = msg.get("content")
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("tool_use_id") == "tu_p":
                        gate_msgs.append(str(blk.get("content", "")))
        assert gate_msgs, "patch_file tool_result not found"
        # Real production message starts with "STOP: '<file>' has not been read yet"
        assert any("STOP" in m and ("not been read" in m or "FIRST" in m)
                   for m in gate_msgs), (
            f"patch_file gate should reject with STOP message; got {gate_msgs}"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_patch_after_prior_read_succeeds(self, mock_anthropic_cls):
        """patch_file AFTER read_file → patch applies (no STOP message)."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.beta.messages.create.side_effect = [
            # Turn 1: write spec.txt
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "spec.txt", "content": "original\n",
                }, "tu_w")],
                stop_reason="tool_use",
            ),
            # Turn 2: read spec.txt (populates read_state)
            _make_mock_response(
                [_make_tool_use_block("read_file", {
                    "filename": "spec.txt",
                }, "tu_r")],
                stop_reason="tool_use",
            ),
            # Turn 3: patch spec.txt — should succeed
            _make_mock_response(
                [_make_tool_use_block("patch_file", {
                    "filename": "spec.txt",
                    "old_string": "original",
                    "new_string": "patched",
                }, "tu_p")],
                stop_reason="tool_use",
            ),
            # Turn 4: harness.py + complete
            _make_mock_response(
                [_make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'def run(input_data):\n'
                        '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                        '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
                    ),
                }, "tu_h")],
                stop_reason="tool_use",
            ),
            _make_text_response("SMOKE TEST PASSED\nHARNESS_COMPLETE"),
        ]
        run_implement_test_env_agent(
            _make_input_with_one_candidate("PatchAfterRead")
        )

        # The 4th API call carries the patch_file's tool_result
        fourth_call_messages = mock_client.beta.messages.create.call_args_list[3].kwargs["messages"]
        patch_results = []
        for msg in fourth_call_messages:
            content = msg.get("content")
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("tool_use_id") == "tu_p":
                        patch_results.append(str(blk.get("content", "")))
        assert patch_results, "patch_file tool_result not found"
        # Should NOT contain STOP — patch should have succeeded
        for m in patch_results:
            assert "STOP" not in m, (
                f"patch_file after read should NOT trigger gate; got {m!r}"
            )
        # Should contain success indicator (e.g., "Patched")
        assert any("Patched" in m or "replaced" in m for m in patch_results), (
            f"Successful patch should report success; got {patch_results}"
        )


class TestDeadEndDetection:
    """The consecutive_errors counter + reassessment injection are the
    safety net against debugging spirals. MAX_CONSECUTIVE_ERRORS = 3."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_three_consecutive_error_turns_inject_reassessment(self, mock_anthropic_cls):
        """3 turns where tool_results contain error signatures → next turn's
        messages contain a STRATEGIC REASSESSMENT prompt."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Each turn does run_code that fails (we'll see the error sig in
        # the dispatched tool result). The actual subprocess runs in-test
        # — to make this deterministic, use run_code with a command that
        # reliably exits non-zero AND produces 'traceback' or '401' text.
        # Simplest: print a known error string + exit 1.
        # But run_code in tests actually executes; let's use a bash command
        # that prints an error sig AND exits 1.
        # Actually, the safest is: have the model write a file that the
        # NEXT turn's run_code reads (the read prints the file content +
        # exits with non-zero based on its presence).
        # Even simpler: the model just writes a smoke_test.py that
        # produces 'Traceback' on import and run_code executes it.
        err_response = _make_mock_response(
            [_make_tool_use_block("run_code", {
                "command": "python -c \"raise RuntimeError('401 unauthorized')\"",
            }, f"tu_err")],
            stop_reason="tool_use",
        )
        # 4 error turns + 1 final response (HARNESS_FAILED to terminate)
        mock_client.beta.messages.create.side_effect = [
            err_response, err_response, err_response, err_response,
            _make_text_response("HARNESS_FAILED no recovery"),
        ]
        run_implement_test_env_agent(
            _make_input_with_one_candidate("DeadEnd3")
        )

        # After 3 errors, the 4th call's messages should include a
        # STRATEGIC REASSESSMENT user message.
        fourth_call_messages = mock_client.beta.messages.create.call_args_list[3].kwargs["messages"]
        all_user_text = ""
        for msg in fourth_call_messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    all_user_text += content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            all_user_text += blk.get("text", "")
        assert "STRATEGIC REASSESSMENT" in all_user_text, (
            "After 3 consecutive errors, builder must see a STRATEGIC "
            "REASSESSMENT message in next turn's messages"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_error_streak_resets_on_successful_turn(self, mock_anthropic_cls):
        """2 errors → success → 2 more errors → NO reassessment yet
        (counter reset by success). Pin the reset behavior."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        err_response = _make_mock_response(
            [_make_tool_use_block("run_code", {
                "command": "python -c \"raise RuntimeError('401 unauthorized')\"",
            }, "tu_err")],
            stop_reason="tool_use",
        )
        success_response = _make_mock_response(
            [_make_tool_use_block("write_file", {
                "filename": "spec.txt", "content": "ok\n",
            }, "tu_ok")],
            stop_reason="tool_use",
        )
        # Sequence: err, err, SUCCESS (counter resets), err, err, complete
        # Total: 6 turns. After 5th turn (2 errors after success), counter=2,
        # below threshold → NO reassessment in the 6th turn's messages.
        mock_client.beta.messages.create.side_effect = [
            err_response, err_response,
            success_response,  # resets counter
            err_response, err_response,
            _make_text_response("HARNESS_FAILED"),
        ]
        run_implement_test_env_agent(
            _make_input_with_one_candidate("ResetStreak")
        )

        # 6th call's messages should NOT have STRATEGIC REASSESSMENT
        # (only 2 errors after the success — below the 3 threshold)
        sixth_call_messages = mock_client.beta.messages.create.call_args_list[5].kwargs["messages"]
        all_user_text = ""
        for msg in sixth_call_messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    all_user_text += content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            all_user_text += blk.get("text", "")
        assert "STRATEGIC REASSESSMENT" not in all_user_text, (
            "Counter must reset on successful turn; 2 errors after a "
            "success should NOT trigger reassessment"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_reassessment_resets_consecutive_errors_to_zero(self, mock_anthropic_cls):
        """After reassessment fires, consecutive_errors resets so the next
        3 errors can trigger another (escalated) tier. Pin the reset
        AFTER reassessment too."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        err_response = _make_mock_response(
            [_make_tool_use_block("run_code", {
                "command": "python -c \"raise RuntimeError('401 unauthorized')\"",
            }, "tu_err")],
            stop_reason="tool_use",
        )
        # 3 errors → tier 1 reassessment → 3 more errors → tier 2 reassessment
        mock_client.beta.messages.create.side_effect = [
            err_response, err_response, err_response,
            err_response, err_response, err_response,
            _make_text_response("HARNESS_FAILED"),
        ]
        run_implement_test_env_agent(
            _make_input_with_one_candidate("DoubleReassess")
        )

        # 7th call's messages should mention "Tier 2" — proving the second
        # reassessment fired (which means consecutive_errors reset after
        # the first one, otherwise it'd never tier up).
        seventh_call_messages = mock_client.beta.messages.create.call_args_list[6].kwargs["messages"]
        all_user_text = ""
        for msg in seventh_call_messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    all_user_text += content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            all_user_text += blk.get("text", "")
        # Either Tier 2 fired, or the test setup didn't generate enough
        # error turns to escalate. Verify Tier 1 at minimum + Tier 2 marker.
        assert "STRATEGIC REASSESSMENT" in all_user_text
        assert "Tier 2" in all_user_text or "tier 2" in all_user_text.lower(), (
            "Second reassessment cycle must escalate to Tier 2; if it "
            "doesn't, consecutive_errors didn't reset after Tier 1"
        )


class TestVerificationGateNegative:
    """The verification gate (lives in verification.py) requires harness.py
    exists. Pin the negative case: HARNESS_COMPLETE without writing
    harness.py → re-prompt + ultimately FailedHarness."""

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_complete_signal_without_harness_re_prompts_then_fails(
        self, mock_anthropic_cls,
    ):
        """HARNESS_COMPLETE before any harness.py exists → verification
        gate returns the issue message → builder gets a feedback turn.
        After AGENT5_MAX_VERIFICATION_RETRIES exhaustion → FailedHarness."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # The agent keeps signaling complete without writing harness.py.
        # AGENT5_MAX_VERIFICATION_RETRIES=2 → 3 attempts total before accept.
        complete = _make_text_response("HARNESS_COMPLETE")
        # Need enough complete responses to exhaust retries + a final one
        mock_client.beta.messages.create.side_effect = [complete] * 10
        result = run_implement_test_env_agent(
            _make_input_with_one_candidate("VerifyNeg")
        )

        # No harness.py written → FailedHarness expected
        assert len(result.failed_harnesses) == 1, (
            f"HARNESS_COMPLETE without harness.py must produce FailedHarness; "
            f"got harnesses={len(result.harnesses)} failed={len(result.failed_harnesses)}"
        )
        assert result.failed_harnesses[0].failure_category == "build_timeout", (
            "FailedHarness category should be build_timeout when harness.py "
            "is never produced"
        )

        # The 2nd API call's messages should contain the verification
        # feedback ("Verification Issues Found").
        # After the 1st HARNESS_COMPLETE, the gate fails and feedback is
        # injected as the next user message.
        second_call_messages = mock_client.beta.messages.create.call_args_list[1].kwargs["messages"]
        all_user_text = ""
        for msg in second_call_messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    all_user_text += content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            all_user_text += blk.get("text", "")
        assert "Verification Issues" in all_user_text or "harness.py" in all_user_text, (
            "After HARNESS_COMPLETE without harness.py, the next turn's "
            "messages must carry the verification gate feedback"
        )


class TestErrorClassification:
    """The error categorization (auth/endpoint/format/other) drives the
    'PATTERN DETECTED' hint in reassessment messages. Pin each category
    end-to-end so the extraction of classification helpers preserves
    the wiring to reassessment escalation.

    Approach: after 3 consecutive same-category errors, the reassessment
    message contains a 'PATTERN DETECTED' block citing the category label
    (authentication/auth header, endpoint URL, request format/body, error)."""

    def _run_with_n_same_error_turns(self, mock_anthropic_cls, error_command, n=3):
        """Run with n turns producing the same error string."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        err_response = _make_mock_response(
            [_make_tool_use_block("run_code", {
                "command": error_command,
            }, "tu_err")],
            stop_reason="tool_use",
        )
        mock_client.beta.messages.create.side_effect = [err_response] * n + [
            _make_text_response("HARNESS_FAILED"),
        ]
        run_implement_test_env_agent(
            _make_input_with_one_candidate(f"ErrCat_{n}")
        )
        return mock_client

    def _last_user_text(self, mock_client, call_idx):
        """Concatenate all user-role text from the Nth API call's messages."""
        msgs = mock_client.beta.messages.create.call_args_list[call_idx].kwargs["messages"]
        out = ""
        for msg in msgs:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    out += content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            out += blk.get("text", "")
        return out

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_auth_errors_trigger_authentication_pattern(self, mock_anthropic_cls):
        """3 turns with '401 unauthorized' → reassessment cites authentication."""
        mc = self._run_with_n_same_error_turns(
            mock_anthropic_cls,
            "python -c \"raise RuntimeError('401 unauthorized')\"",
            n=3,
        )
        # 4th call carries the reassessment
        text = self._last_user_text(mc, 3)
        assert "STRATEGIC REASSESSMENT" in text
        assert "PATTERN DETECTED" in text, (
            "3 same-category errors must trigger PATTERN DETECTED hint"
        )
        assert "authentication" in text.lower() or "auth header" in text.lower(), (
            f"Auth-category errors must produce auth-specific pattern hint; "
            f"got: {text[-500:]!r}"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_endpoint_errors_via_connectionrefused_trigger_endpoint_pattern(self, mock_anthropic_cls):
        """3 turns with ConnectionRefusedError → endpoint category +
        endpoint URL pattern hint."""
        mc = self._run_with_n_same_error_turns(
            mock_anthropic_cls,
            "python -c \"raise ConnectionRefusedError('connectionrefused 404 not found')\"",
            n=3,
        )
        text = self._last_user_text(mc, 3)
        assert "STRATEGIC REASSESSMENT" in text
        assert "PATTERN DETECTED" in text
        # The ConnectionRefusedError text contains both 'traceback' (has_error)
        # AND 'connectionrefused' (endpoint category)
        assert "endpoint url" in text.lower() or "endpoint" in text.lower(), (
            f"ConnectionRefused should classify as endpoint category; "
            f"got: {text[-500:]!r}"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_format_errors_trigger_format_pattern(self, mock_anthropic_cls):
        """3 turns with '400 bad request' → format category."""
        # The error text needs to trigger has_error AND format classification.
        # has_error includes 'traceback' so a Python error works; classification
        # checks for '400', 'bad request', 'invalid input', 'unsupported'.
        mc = self._run_with_n_same_error_turns(
            mock_anthropic_cls,
            "python -c \"raise ValueError('400 bad request: invalid input')\"",
            n=3,
        )
        text = self._last_user_text(mc, 3)
        assert "STRATEGIC REASSESSMENT" in text
        assert "PATTERN DETECTED" in text
        assert "request format" in text.lower() or "format" in text.lower(), (
            f"400/bad request should classify as format category; "
            f"got: {text[-500:]!r}"
        )

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_other_errors_trigger_other_pattern_or_no_pattern(self, mock_anthropic_cls):
        """3 turns with unrelated traceback (no auth/endpoint/format
        keyword) → 'other' category. Pattern hint cites generic 'error'."""
        mc = self._run_with_n_same_error_turns(
            mock_anthropic_cls,
            "python -c \"raise RuntimeError('unexpected something')\"",
            n=3,
        )
        text = self._last_user_text(mc, 3)
        assert "STRATEGIC REASSESSMENT" in text
        # 'other' category produces pattern hint that just says 'error'
        # (the {"other": "error"} mapping in the production code)
        assert "PATTERN DETECTED" in text, (
            "Even 'other' category should trigger pattern hint after 3 same-cat"
        )
