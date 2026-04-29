"""Regression guards for concurrent-session-cap retry.

When per-candidate parallelism exceeds a provider's concurrent-session
cap (common on free-tier voice APIs: ElevenLabs ConvAI, OpenAI
Realtime), some harness calls fail with "maximum concurrent sessions
exceeded" or similar. The right fallback is to WAIT for a prior
session to release, not to mark the test as errored.

Contract locked here (AD-007: safety contract in code, not prompts):
  1. The CONCURRENT_SESSION_INDICATORS set covers the common phrasings
     so _is_concurrent_session_error correctly detects the class.
  2. _execute_test_with_session_retry wraps _execute_single_test with
     exponential backoff retry on session-cap errors only (not other
     failures).
  3. Single-turn path (_run_single_test_with_rate_limit) uses the
     session-retry wrapper.
  4. Multi-turn path (runner closures in _run_deterministic and
     _run_tool_runner) use the session-retry wrapper too — plugin's
     drive_conversation transparently gets waited-and-retried per turn.
  5. Retry logic: detects session errors, sleeps exponential(attempt)
     × base seconds, retries up to MAX_RETRIES times, then returns the
     last error.
  6. Non-session errors pass through immediately — no retry.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Phase 6.1 note: execution helpers moved to agent5/execution.py.
# Phase 5 note: _build_single_harness moved to agent5/build_loop.py.
# Combined source so source-grep tests find the literals at their
# canonical owners.
IMPL_SRC = (
    (ROOT / "puzzleeval" / "agents" / "implement_test_env.py").read_text(encoding="utf-8")
    + "\n# === build_loop.py boundary ===\n"
    + (ROOT / "puzzleeval" / "agents" / "agent5" / "build_loop.py").read_text(encoding="utf-8")
    + "\n# === execution.py boundary ===\n"
    + (ROOT / "puzzleeval" / "agents" / "agent5" / "execution.py").read_text(encoding="utf-8")
)
CONFIG_SRC = (ROOT / "puzzleeval" / "config.py").read_text(encoding="utf-8")


# ============================================================================
# Config knobs exist with sensible defaults
# ============================================================================


class TestSessionRetryConfig:

    def test_backoff_base_defaults_to_5_seconds(self):
        from puzzleeval.config import AGENT6_SESSION_RETRY_BACKOFF_BASE
        # 5s is tuned for typical voice-conversation duration (15-30s).
        # With exponential backoff (5 → 10 → 20), by the 3rd retry a
        # prior session has almost certainly released. Shorter base
        # risks tight-looping against a persistent cap.
        assert AGENT6_SESSION_RETRY_BACKOFF_BASE == 5

    def test_max_retries_defaults_to_3(self):
        from puzzleeval.config import AGENT6_SESSION_MAX_RETRIES
        # 3 retries with exponential 5s base = up to 35s total wait
        # (5 + 10 + 20). Enough for a full conversation to release.
        assert AGENT6_SESSION_MAX_RETRIES == 3

    def test_knobs_are_env_overridable(self):
        assert "PUZZLEEVAL_AGENT6_SESSION_RETRY_BACKOFF_BASE" in CONFIG_SRC
        assert "PUZZLEEVAL_AGENT6_SESSION_MAX_RETRIES" in CONFIG_SRC

    def test_doc_explains_retry_rationale(self):
        # Ops needs to know why this is distinct from rate-limit retry.
        # Use rfind to locate the actual config-line occurrence — other
        # parts of the file mention AGENT6_SESSION_RETRY_BACKOFF in
        # cross-references (e.g., the parallelism-knob comment block
        # explains how session-cap errors trigger this retry path).
        block_start = CONFIG_SRC.rfind("AGENT6_SESSION_RETRY_BACKOFF_BASE")
        assert block_start != -1
        # Walk back ~2500 chars to find the block's doc comment
        context = CONFIG_SRC[max(0, block_start - 2500): block_start + 300]
        assert "concurrent session" in context.lower()
        # Must distinguish from rate limits
        assert "rate limit" in context.lower() or "RATE LIMIT" in context


# ============================================================================
# _is_concurrent_session_error helper
# ============================================================================


class TestConcurrentSessionErrorDetection:

    def test_detects_common_english_phrasings(self):
        from puzzleeval.agents.implement_test_env import _is_concurrent_session_error
        positives = [
            "Error: maximum concurrent sessions exceeded",
            "HTTP 429: too many concurrent requests",
            "concurrent call limit reached for this API key",
            "Max sessions: 1 per key, please wait",
            "You have reached the maximum number of concurrent calls",
            "A session is already active for this account",
            "concurrent connections limit exceeded",
        ]
        for msg in positives:
            assert _is_concurrent_session_error(msg), (
                f"Expected {msg!r} to match a concurrent-session pattern; "
                f"if it doesn't, add the relevant token to "
                f"CONCURRENT_SESSION_INDICATORS."
            )

    def test_rejects_non_session_errors(self):
        from puzzleeval.agents.implement_test_env import _is_concurrent_session_error
        negatives = [
            "rate limit exceeded",
            "401 unauthorized",
            "connection refused",
            "invalid api key",
            "timeout waiting for response",
            "",
            None,
        ]
        for msg in negatives:
            assert not _is_concurrent_session_error(msg), (
                f"Expected {msg!r} NOT to match a concurrent-session pattern."
            )

    def test_indicator_set_has_enough_coverage(self):
        # Too few indicators risks missing real provider error texts.
        # Too many risks false positives. Keep a reasonable floor.
        from puzzleeval.agents.implement_test_env import CONCURRENT_SESSION_INDICATORS
        assert len(CONCURRENT_SESSION_INDICATORS) >= 10, (
            f"CONCURRENT_SESSION_INDICATORS has only "
            f"{len(CONCURRENT_SESSION_INDICATORS)} entries — need enough "
            f"to cover provider phrasings for WebSocket voice, "
            f"conversation platforms, and HTTP session APIs."
        )


# ============================================================================
# _execute_test_with_session_retry behavior
# ============================================================================


class TestSessionRetryWrapper:
    """Behavioral tests of the retry wrapper itself — no subprocess
    spawning, just mock _execute_single_test."""

    def _call(self, mock_results):
        """Helper: call the wrapper with a mocked execute_single_test
        that returns results in sequence. Returns (final_result,
        call_count, sleep_times).

        Phase 6.1 note: ``execute_single_test`` and
        ``execute_test_with_session_retry`` moved to
        ``puzzleeval.agents.agent5.execution``. ``implement_test_env``
        keeps re-export aliases for back-compat, but the call site
        INSIDE ``execute_test_with_session_retry`` references the
        canonical name. Patch the canonical owner.
        """
        import puzzleeval.agents.agent5.execution as execution
        from pathlib import Path as _P

        sleep_times = []
        call_count = {"n": 0}

        def _fake_execute(*args, **kwargs):
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(mock_results):
                return mock_results[idx]
            return mock_results[-1]

        def _fake_sleep(seconds):
            sleep_times.append(seconds)

        import logging
        logger = logging.getLogger("test")

        with patch.object(execution, "execute_single_test", side_effect=_fake_execute):
            with patch.object(execution.time, "sleep", side_effect=_fake_sleep):
                final = execution.execute_test_with_session_retry(
                    _P("/tmp/fake"), {}, None, 60,
                    logger, "trace-test", "TestCandidate",
                )
        return final, call_count["n"], sleep_times

    def test_success_on_first_call_no_retry(self):
        final, calls, sleeps = self._call([
            {"success": True, "error": None},
        ])
        assert final["success"] is True
        assert calls == 1
        assert sleeps == []

    def test_non_session_error_no_retry(self):
        """Non-session errors (auth failure, timeout) should pass
        through without retry — they'll be handled by other code
        paths (rate-limit retry, LLM judge fallback, etc.)."""
        final, calls, sleeps = self._call([
            {"success": False, "error": "401 unauthorized"},
        ])
        assert final["success"] is False
        assert "401" in final["error"]
        assert calls == 1, "Should not retry on non-session errors"
        assert sleeps == [], "No sleep on non-session errors"

    def test_session_error_retries_with_exponential_backoff(self):
        """Classic case: two session-cap failures then success."""
        final, calls, sleeps = self._call([
            {"success": False, "error": "maximum concurrent sessions exceeded"},
            {"success": False, "error": "concurrent call limit reached"},
            {"success": True, "error": None, "output": "works now"},
        ])
        assert final["success"] is True
        assert calls == 3
        # Exponential: 5, 10 — stops after 2nd retry succeeds
        assert sleeps == [5, 10]

    def test_max_retries_then_gives_up(self):
        """If session cap persists through all retries, return the
        last error — don't loop forever."""
        persistent_error = {
            "success": False,
            "error": "maximum concurrent sessions exceeded",
        }
        final, calls, sleeps = self._call([persistent_error] * 10)
        # 1 initial call + MAX_RETRIES retries = 4 calls
        assert calls == 4
        # Slept 3 times: 5, 10, 20
        assert sleeps == [5, 10, 20]
        assert final["success"] is False

    def test_switches_from_session_error_to_non_session_error(self):
        """Edge: first call fails with session cap (retry triggers),
        second call fails with different error (no further retry)."""
        final, calls, sleeps = self._call([
            {"success": False, "error": "concurrent sessions exceeded"},
            {"success": False, "error": "401 unauthorized"},  # different class
        ])
        assert final["success"] is False
        assert "401" in final["error"]
        assert calls == 2
        assert sleeps == [5]  # one retry attempted, then returned


# ============================================================================
# Wiring: single-turn path uses the wrapper
# ============================================================================


class TestSingleTurnPathUsesWrapper:

    def test_run_single_test_with_rate_limit_calls_wrapper(self):
        # Phase 6.1: function renamed run_single_test_with_rate_limit
        # at canonical owner agent5/execution.py.
        fn_start = IMPL_SRC.find("def run_single_test_with_rate_limit")
        if fn_start == -1:
            fn_start = IMPL_SRC.find("def _run_single_test_with_rate_limit")
        assert fn_start != -1, "run_single_test_with_rate_limit must exist"
        fn_end = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:fn_end]
        assert ("_execute_test_with_session_retry" in fn_body or "execute_test_with_session_retry(" in fn_body), (
            "run_single_test_with_rate_limit must delegate to the "
            "session-retry wrapper so single-turn tests benefit from "
            "wait-and-retry on concurrent-session caps."
        )


# ============================================================================
# Wiring: multi-turn path uses the wrapper in both strategies
# ============================================================================


class TestMultiTurnPathUsesWrapper:

    def test_deterministic_runner_uses_wrapper(self):
        # Find _run_deterministic, look for _execute_test_with_session_
        # retry inside the _runner closure body.
        det_start = IMPL_SRC.find("def _run_deterministic()")
        det_end = IMPL_SRC.find("def _run_tool_runner()")
        assert det_start != -1 and det_end != -1
        det_body = IMPL_SRC[det_start:det_end]
        assert ("_execute_test_with_session_retry" in det_body or "execute_test_with_session_retry(" in det_body), (
            "The deterministic dispatcher's _runner closure must use "
            "the session-retry wrapper so multi-turn voice conversations "
            "transparently wait for a slot on session-cap errors."
        )

    def test_tool_runner_uses_wrapper(self):
        tr_start = IMPL_SRC.find("def _run_tool_runner()")
        # End at the next top-level section marker
        tr_end = IMPL_SRC.find("# Initialize eval_cost BEFORE dispatch", tr_start)
        assert tr_start != -1 and tr_end != -1
        tr_body = IMPL_SRC[tr_start:tr_end]
        assert ("_execute_test_with_session_retry" in tr_body or "execute_test_with_session_retry(" in tr_body), (
            "The tool_runner dispatcher's _runner closure must use the "
            "session-retry wrapper so Claude-driven plugin evaluations "
            "get the same session-cap resilience."
        )


# ============================================================================
# Cloud-safety + thread-safety sanity
# ============================================================================


class TestRetryWrapperIsThreadSafe:

    def test_wrapper_has_no_module_level_state(self):
        """The wrapper must not introduce module-level mutable state
        (would break cross-candidate parallelism in cloud)."""
        suspicious = [
            "_session_retry_count = 0",
            "SESSION_RETRIES_IN_PROGRESS",
            "_global_session_wait",
        ]
        for pat in suspicious:
            assert pat not in IMPL_SRC, (
                f"{pat!r} would be module-level state — unsafe for "
                f"cross-candidate parallel runs."
            )

    def test_helper_signature_is_purely_functional(self):
        """No reliance on globals — all inputs must come through args.
        This is what makes it safe to call from N parallel threads."""
        import inspect
        from puzzleeval.agents.implement_test_env import _execute_test_with_session_retry
        sig = inspect.signature(_execute_test_with_session_retry)
        # Verify the wrapper takes all the context it needs via
        # arguments (sandbox_dir, payload, credentials, timeout,
        # logger, trace_id, candidate_name). Any one of these missing
        # would force access to a module-level fallback.
        param_names = set(sig.parameters.keys())
        required = {
            "sandbox_dir", "payload", "credentials", "timeout",
            "logger", "trace_id", "candidate_name",
        }
        missing = required - param_names
        assert not missing, f"Missing required params: {missing}"
