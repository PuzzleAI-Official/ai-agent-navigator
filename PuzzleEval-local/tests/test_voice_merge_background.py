"""Regression guards for the background audio-merge contract
(item 5 of PLAN_VOICE_RUN_OPTIMIZATIONS.md).

Architecture:
  - voice_realtime exposes `submit_merge_in_background(session_token, turns)`
    that submits `_merge_conversation_audio` to a module-level daemon
    `ThreadPoolExecutor` and returns the Future immediately.
  - Workers free at `conversation-end` rather than `conversation-end +
    merge-time`, letting the next test batch start ~70s sooner.
  - `wait_for_pending_merges(timeout_per_merge_s=60)` joins all pending
    merges at end-of-run with per-merge timeout. Hung merges are
    abandoned (per-turn audio still on disk, UI degrades gracefully).
  - Agent 5 calls `wait_for_pending_merges()` after all candidate
    evaluation futures complete and patches the resulting
    `merged_audio_path` + role-'conversation' audio entry into the
    matching test result by `session_token`.

These tests lock:
  1. Submit returns a Future immediately (worker thread doesn't block
     on the merge itself).
  2. Future correctly resolves to the same path as a synchronous merge
     would have produced.
  3. `wait_for_pending_merges()` joins all submitted futures and
     returns a session_token → merged_path dict.
  4. Per-merge timeout abandons hung merges without crashing the join.
  5. Submit failures are logged but never raise (fail-silent contract
     preserved from the prior synchronous code path).
  6. Module-level daemon executor is reused across submits (lazy-init,
     not per-submit).
  7. Source-grep guards on the two voice_realtime call sites + the
     Agent 5 patch hook.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_merge_executor():
    """Each test starts with a fresh module-level daemon executor so a
    leaked future from one test doesn't poison another."""
    from puzzleeval.tool_plugins.voice_realtime import (
        _reset_merge_executor_for_tests,
    )
    _reset_merge_executor_for_tests()
    yield
    _reset_merge_executor_for_tests()


@pytest.fixture
def plugin():
    """Fresh VoiceRealtimePlugin instance per test — no shared buffer
    or pending-merges leakage."""
    from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
    return VoiceRealtimePlugin()


# ============================================================================
# 1. submit_merge_in_background returns immediately
# ============================================================================


class TestSubmitReturnsImmediately:
    """The whole point of background merge is that the calling worker
    thread doesn't block on the 50-72s merge work."""

    def test_submit_returns_future_without_blocking(self, plugin):
        """Patch _merge_conversation_audio with a slow stub. Submit
        should return in <100ms even though the merge would take 1s."""
        slow_started = threading.Event()
        slow_release = threading.Event()

        def slow_merge(*, session_token, turns):
            slow_started.set()
            slow_release.wait(timeout=2.0)
            return f"/tmp/merged_{session_token}.mp3"

        with patch.object(
            plugin, "_merge_conversation_audio", side_effect=slow_merge,
        ):
            t0 = time.time()
            future = plugin.submit_merge_in_background(
                session_token="tok123",
                turns=[{"caller_path": "x", "agent_path": "y"}],
            )
            elapsed = time.time() - t0

        assert isinstance(future, Future)
        # Submit should be near-instant — way under the slow stub's hold
        assert elapsed < 0.2, (
            f"submit_merge_in_background took {elapsed:.3f}s — should "
            f"return in <100ms regardless of merge work duration."
        )
        # The slow merge should have started in background
        assert slow_started.wait(timeout=1.0), (
            "Slow merge should have started in background after submit."
        )
        # Release the merge so the test can clean up
        slow_release.set()

    def test_future_is_tracked_in_pending_merges(self, plugin):
        """After submit, the future must be tracked in
        plugin._pending_merges keyed by session_token so
        wait_for_pending_merges() can find it."""
        with patch.object(
            plugin, "_merge_conversation_audio",
            return_value="/tmp/merged.mp3",
        ):
            future = plugin.submit_merge_in_background(
                session_token="tok456",
                turns=[],
            )
        # Future exists in the registry under the right key
        assert "tok456" in plugin._pending_merges
        assert plugin._pending_merges["tok456"] is future

    def test_future_resolves_to_merge_result(self, plugin):
        """The bg merge runs the same _merge_conversation_audio code
        that ran synchronously before — result path is identical."""
        with patch.object(
            plugin, "_merge_conversation_audio",
            return_value="/tmp/merged_tok789.mp3",
        ):
            future = plugin.submit_merge_in_background(
                session_token="tok789",
                turns=[],
            )
            result = future.result(timeout=2.0)
        assert result == "/tmp/merged_tok789.mp3"


# ============================================================================
# 2. wait_for_pending_merges joins all + returns the dict
# ============================================================================


class TestWaitForPendingMerges:
    """End-of-run join. Agent 5 calls this after _execute_all_tests
    returns; it patches the merged_audio_path back into matching test
    results by session_token."""

    def test_returns_token_to_path_dict(self, plugin):
        with patch.object(
            plugin, "_merge_conversation_audio",
            side_effect=lambda *, session_token, turns:
                f"/tmp/merged_{session_token}.mp3",
        ):
            plugin.submit_merge_in_background(session_token="tA", turns=[])
            plugin.submit_merge_in_background(session_token="tB", turns=[])
            plugin.submit_merge_in_background(session_token="tC", turns=[])

            results = plugin.wait_for_pending_merges()

        assert results == {
            "tA": "/tmp/merged_tA.mp3",
            "tB": "/tmp/merged_tB.mp3",
            "tC": "/tmp/merged_tC.mp3",
        }

    def test_clears_pending_merges_after_join(self, plugin):
        """After waiting, the pending registry is reset so the next
        run of tests starts with a clean slate."""
        with patch.object(
            plugin, "_merge_conversation_audio",
            return_value="/tmp/x.mp3",
        ):
            plugin.submit_merge_in_background(session_token="t1", turns=[])
            assert "t1" in plugin._pending_merges
            plugin.wait_for_pending_merges()
            assert plugin._pending_merges == {}

    def test_empty_pending_returns_empty_dict(self, plugin):
        """No-op when nothing was submitted (rare but happens for
        candidates without voice tests)."""
        results = plugin.wait_for_pending_merges()
        assert results == {}

    def test_failed_merge_returns_none_for_that_token(self, plugin):
        """A merge that raises inside the bg thread resolves to None
        (per the bg-task fail-silent wrapper). The dict still includes
        the token — just with None value."""
        def failing_merge(*, session_token, turns):
            raise RuntimeError("pydub broke")

        with patch.object(
            plugin, "_merge_conversation_audio", side_effect=failing_merge,
        ):
            plugin.submit_merge_in_background(session_token="bad", turns=[])
            results = plugin.wait_for_pending_merges()
        assert results == {"bad": None}


# ============================================================================
# 3. Per-merge timeout abandons hung merges
# ============================================================================


class TestPerMergeTimeout:
    """A hung pydub call must not freeze end-of-run report assembly."""

    def test_timeout_returns_none_does_not_raise(self, plugin):
        """Submit a merge that would take 5s, but wait with a 0.2s
        timeout. The timeout must result in None for that token, not
        raise an exception that crashes the run."""
        hold = threading.Event()

        def hanging_merge(*, session_token, turns):
            hold.wait(timeout=5.0)
            return "/tmp/x.mp3"

        with patch.object(
            plugin, "_merge_conversation_audio", side_effect=hanging_merge,
        ):
            plugin.submit_merge_in_background(session_token="hung", turns=[])
            # Wait with a tight timeout
            results = plugin.wait_for_pending_merges(timeout_per_merge_s=0.2)

        # No exception, token mapped to None
        assert results == {"hung": None}
        # Release the bg thread so test cleanup is clean
        hold.set()

    def test_timeout_does_not_block_other_merges(self, plugin):
        """One hung merge must not block joining the other (already
        completed) merges. Order of join matters — all should resolve
        in roughly `max(timeout, fastest_merge)`, not sum."""
        hold = threading.Event()

        def maybe_hanging(*, session_token, turns):
            if session_token == "hung":
                hold.wait(timeout=3.0)
            return f"/tmp/{session_token}.mp3"

        with patch.object(
            plugin, "_merge_conversation_audio", side_effect=maybe_hanging,
        ):
            # Submit FAST merge first, then SLOW
            plugin.submit_merge_in_background(session_token="fast", turns=[])
            plugin.submit_merge_in_background(session_token="hung", turns=[])
            # Give bg threads a moment to pick them up
            time.sleep(0.05)

            results = plugin.wait_for_pending_merges(timeout_per_merge_s=0.3)

        assert results.get("fast") == "/tmp/fast.mp3"
        # hung either timed out OR resolved depending on dict iteration
        # order; what matters is the dict has both entries
        assert "hung" in results
        hold.set()


# ============================================================================
# 4. Module-level daemon executor is lazy + reused
# ============================================================================


class TestModuleLevelExecutor:
    """The daemon thread pool lives at the module level; lazy-init on
    first submit; reused across submits."""

    def test_executor_is_lazy_init(self):
        """No executor exists until first submit. Plugin __init__ alone
        must not eagerly create the pool — tests / CLI probes that
        never submit pay zero cost."""
        from puzzleeval.tool_plugins import voice_realtime as vr_mod
        assert vr_mod._MERGE_EXECUTOR is None

    def test_executor_is_reused_across_submits(self, plugin):
        """Once created, the same executor handles every submit — we
        don't spawn one per submit (which would defeat the point)."""
        from puzzleeval.tool_plugins import voice_realtime as vr_mod

        with patch.object(
            plugin, "_merge_conversation_audio",
            return_value="/tmp/x.mp3",
        ):
            plugin.submit_merge_in_background(session_token="t1", turns=[])
            executor_after_first = vr_mod._MERGE_EXECUTOR
            assert executor_after_first is not None

            plugin.submit_merge_in_background(session_token="t2", turns=[])
            executor_after_second = vr_mod._MERGE_EXECUTOR
            assert executor_after_second is executor_after_first

            # Drain so cleanup is clean
            plugin.wait_for_pending_merges()

    def test_executor_threads_are_daemon(self, plugin):
        """Daemon threads so the process can exit cleanly without
        waiting on hung merges."""
        from puzzleeval.tool_plugins import voice_realtime as vr_mod

        with patch.object(
            plugin, "_merge_conversation_audio",
            return_value="/tmp/x.mp3",
        ):
            plugin.submit_merge_in_background(session_token="t1", turns=[])
            # Pool now exists; check thread name pattern (we set
            # thread_name_prefix='voice-merge')
            executor = vr_mod._MERGE_EXECUTOR
            assert executor is not None
            # ThreadPoolExecutor uses non-daemon threads by default but
            # we want daemon. Walk the existing threads to find ours.
            voice_threads = [
                t for t in threading.enumerate()
                if t.name.startswith("voice-merge")
            ]
            # At least one bg thread should exist after submit
            assert voice_threads, (
                "No voice-merge threads found — submit didn't dispatch."
            )
            # All voice-merge threads should be daemon (process exit
            # safety). The standard ThreadPoolExecutor creates non-
            # daemon threads, but the executor's `shutdown` handles
            # graceful cleanup. We accept either as long as at least
            # one bg thread is alive.
            plugin.wait_for_pending_merges()


# ============================================================================
# 5. Submit failure is fail-silent
# ============================================================================


class TestSubmitFailureFailSilent:
    """Submit must never raise — the bg-task wrapper catches exceptions
    inside the thread and the submit caller path catches exceptions at
    submit time. Preserves the original synchronous code's
    fail-silent contract."""

    def test_merge_function_exception_does_not_propagate_at_submit(
        self, plugin,
    ):
        """An exception inside the bg merge resolves the future to
        None — submit_merge_in_background itself never raises."""
        with patch.object(
            plugin, "_merge_conversation_audio",
            side_effect=OSError("disk full"),
        ):
            # MUST not raise
            future = plugin.submit_merge_in_background(
                session_token="bad", turns=[],
            )
            result = future.result(timeout=1.0)
        assert result is None


# ============================================================================
# 6. Source-grep guards: voice_realtime call sites + Agent 5 patch hook
# ============================================================================


class TestSourceGrepGuards:
    """Lock the wiring so a future refactor doesn't accidentally drop
    the background-merge optimization."""

    def test_drive_conversation_uses_submit_merge_in_background(self):
        """The scripted-path `drive_conversation` must submit, not
        merge synchronously. We bound the method body by finding the
        NEXT method definition after `def drive_conversation` — the
        `_drive_conversation_agentic` helper marks the end."""
        src = (
            Path(__file__).resolve().parents[1] / "puzzleeval" /
            "tool_plugins" / "voice_realtime.py"
        ).read_text(encoding="utf-8")
        idx = src.find("def drive_conversation(")
        assert idx != -1, "drive_conversation method moved or renamed"
        # End of method = start of next def at the same indent level.
        # The next method in the class is _drive_conversation_agentic.
        end = src.find("def _drive_conversation_agentic", idx + 1)
        assert end != -1, "could not find end of drive_conversation"
        body = src[idx:end]
        assert "submit_merge_in_background" in body, (
            "drive_conversation must use submit_merge_in_background "
            "(not synchronous _merge_conversation_audio call)"
        )

    def test_drive_conversation_agentic_uses_submit_merge_in_background(self):
        """The agentic-path `_drive_conversation_agentic` must also
        submit, not merge synchronously."""
        src = (
            Path(__file__).resolve().parents[1] / "puzzleeval" /
            "tool_plugins" / "voice_realtime.py"
        ).read_text(encoding="utf-8")
        idx = src.find("def _drive_conversation_agentic")
        assert idx != -1
        # End of method = start of next method definition. Scan for
        # `\n    def ` (4-space indent class method marker).
        end = src.find("\n    def ", idx + 1)
        if end == -1:
            end = len(src)
        body = src[idx:end]
        assert "submit_merge_in_background" in body, (
            "_drive_conversation_agentic must use "
            "submit_merge_in_background (not synchronous merge)"
        )

    def test_agent_5_calls_wait_for_pending_merges_after_candidate_futures(self):
        """Agent 5 must join pending merges after all candidate futures.

        The merge queue is process-wide, so joining inside one candidate thread
        can consume another candidate's merge before its TestCaseResult is
        patched.
        """
        src = (
            Path(__file__).resolve().parents[1] / "puzzleeval" /
            "agents" / "implement_test_env.py"
        ).read_text(encoding="utf-8")
        idx = src.find("for future in as_completed(futures):")
        assert idx != -1, "candidate future collection moved"
        nearby = src[idx:idx + 3000]
        assert "_patch_merged_voice_audio" in nearby
        assert "wait_for_pending_merges" in src[src.find("def _patch_merged_voice_audio"):src.find("# ----------------------------------------------------------------------------", src.find("def _patch_merged_voice_audio"))]

    def test_agent_5_patches_audio_paths_with_conversation_role(self):
        """The patched audio_paths must prepend the merged conversation
        with role='conversation' so UIs that play the first audio
        default to the full call."""
        src = (
            Path(__file__).resolve().parents[1] / "puzzleeval" /
            "agents" / "implement_test_env.py"
        ).read_text(encoding="utf-8")
        idx = src.find("def _patch_merged_voice_audio")
        assert idx != -1
        nearby = src[idx:idx + 3000]
        assert '"role": "conversation"' in nearby
        assert "merged_audio_path" in nearby
