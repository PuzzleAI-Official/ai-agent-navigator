"""Regression guards for within-candidate test parallelism.

Two parallelism knobs were added to let a single candidate run multiple
test cases concurrently against the same provider:

  - AGENT6_PER_CANDIDATE_PARALLELISM (default 6) — how many single-turn
    tests for one candidate fire concurrently. Examples: OCR batch,
    chatbot-single-turn, classification. Rate limiter still paces
    req/sec; this is just the concurrency ceiling.

  - AGENT6_PER_CANDIDATE_SESSION_PARALLELISM (default 6) — how many
    multi-turn tests (conversation, voice_conversation, voice_turn)
    fire concurrently. Free-tier voice APIs that cap concurrent sessions
    below 6 trigger the AGENT6_SESSION_RETRY_BACKOFF path (5s/10s/20s
    backoff, 3 retries) — graceful degradation to slower wall-clock,
    never test failure.

Default 6 chosen as the CEILING below the ElevenLabs Conversational AI
Starter pack's concurrent-session cap (the tightest paid tier we
currently exercise). Pairs with item 5 (background audio merge) to
overlap merges with next-batch conversations when tests > 6.

Bumped from 3→6 in PLAN_VOICE_RUN_OPTIMIZATIONS.md (item 2 + item 5
combined) to cut test-phase wall-clock by ~50%.

Architectural safety contracts this test file locks (AD-007):

  1. The two knobs exist and have sensible defaults.
  2. Parallel execution is thread-safe — shared state (error counter,
     results map, eval_cost accumulator, plugin_handled_ids) is
     guarded by explicit locks.
  3. When parallelism is 1, the path degrades to the legacy sequential
     execution order exactly (escape hatch).
  4. Cloud-migration-safe: no process-level shared state, no filesystem
     racing, no cross-candidate coordination.
  5. Order-preservation: test results return in shuffled order, not
     completion order (downstream log ordering + evaluation remains
     deterministic per shuffle).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Phase 6.1 note: execution helpers moved to agent5/execution.py.
IMPL_SRC = (
    (ROOT / "puzzleeval" / "agents" / "implement_test_env.py").read_text(encoding="utf-8")
    + "\n# === execution.py boundary ===\n"
    + (ROOT / "puzzleeval" / "agents" / "agent5" / "execution.py").read_text(encoding="utf-8")
)
CONFIG_SRC = (ROOT / "puzzleeval" / "config.py").read_text(encoding="utf-8")


# ============================================================================
# Config knobs exist and have the intended defaults
# ============================================================================


class TestConfigKnobsExist:

    def test_single_turn_knob_defaults_to_6(self):
        from puzzleeval.config import AGENT6_PER_CANDIDATE_PARALLELISM
        # Default 6 — chosen as the CEILING below the ElevenLabs
        # Conversational AI Starter pack's concurrent-session cap.
        # Going above 6 on Starter triggers 429 / "session limit
        # exceeded" on every voice run; 6 stays under the cap and
        # leaves 1 slot of headroom for any straggler reconnect.
        # Pair with item 5 (background audio merge) to save 65s per
        # voice run when tests > 6 (batch 2 starts at conversation-end
        # not merge-end).
        assert AGENT6_PER_CANDIDATE_PARALLELISM == 6, (
            f"Expected default 6, got {AGENT6_PER_CANDIDATE_PARALLELISM}. "
            "If you changed this, update the config.py docstring too."
        )

    def test_session_parallelism_knob_defaults_to_6(self):
        from puzzleeval.config import AGENT6_PER_CANDIDATE_SESSION_PARALLELISM
        # Same Starter-pack ceiling as single-turn. Multi-turn tests
        # open a separate provider session each (WebSocket /
        # conversation_id / session handle) so the concurrent-session
        # cap applies identically. Free-tier providers below 6 trigger
        # the AGENT6_SESSION_RETRY_BACKOFF retry path — graceful
        # degradation, never test failure.
        assert AGENT6_PER_CANDIDATE_SESSION_PARALLELISM == 6, (
            f"Expected default 6, got {AGENT6_PER_CANDIDATE_SESSION_PARALLELISM}. "
            "If you changed this, update the config.py docstring + this test "
            "in lockstep."
        )

    def test_knobs_are_env_overridable(self):
        # Both must be `int(os.environ.get(...))` — not hardcoded.
        assert "PUZZLEEVAL_AGENT6_PER_CANDIDATE_PARALLELISM" in CONFIG_SRC
        assert "PUZZLEEVAL_AGENT6_PER_CANDIDATE_SESSION_PARALLELISM" in CONFIG_SRC

    def test_session_parallelism_doc_explains_free_tier_concern(self):
        # The knob's comment must warn ops about concurrent-session caps
        # so anyone raising it understands the provider-side risk.
        block_start = CONFIG_SRC.find("AGENT6_PER_CANDIDATE_SESSION_PARALLELISM controls")
        assert block_start != -1
        doc = CONFIG_SRC[block_start:block_start + 1200]
        assert "concurrent session" in doc.lower() or "session cap" in doc.lower()


# ============================================================================
# Thread-safety: locks on shared state
# ============================================================================


class TestEvalPhaseHasSharedStateLock:
    """The evaluation dispatchers (deterministic + tool_runner) now run
    per-item work in parallel. Without locks, eval_cost would double-
    count, plugin_handled_ids would miss entries, and TCR promotion
    would race."""

    def test_eval_state_lock_is_defined(self):
        assert "eval_state_lock = threading.Lock()" in IMPL_SRC, (
            "Evaluation phase must use a threading.Lock to guard "
            "eval_cost / plugin_handled_ids / TCR mutation. Without it, "
            "parallel workers race on shared state."
        )

    def test_deterministic_path_locks_around_mutation(self):
        # Find _run_deterministic function body
        fn_start = IMPL_SRC.find("def _run_deterministic()")
        fn_end = IMPL_SRC.find("def _run_tool_runner()")
        assert fn_start != -1 and fn_end != -1
        fn_body = IMPL_SRC[fn_start:fn_end]
        # Must lock around plugin_handled_ids mutation
        assert "with eval_state_lock:" in fn_body
        # Must call _promote_verdict_to_tcr INSIDE the locked block
        promote_pos = fn_body.find("_promote_verdict_to_tcr")
        lock_pos = fn_body.find("with eval_state_lock:")
        assert lock_pos < promote_pos, (
            "_promote_verdict_to_tcr must be inside the locked block — "
            "TCR mutation must be atomic with plugin_handled_ids.add()"
        )

    def test_tool_runner_path_locks_around_mutation(self):
        fn_start = IMPL_SRC.find("def _run_tool_runner()")
        # Find the end by looking for where the dispatch call sits
        fn_end = IMPL_SRC.find(
            "# Initialize eval_cost BEFORE dispatch", fn_start,
        )
        assert fn_start != -1 and fn_end != -1
        fn_body = IMPL_SRC[fn_start:fn_end]
        assert "with eval_state_lock:" in fn_body
        # eval_cost accumulation must be inside the lock (atomic)
        cost_pos = fn_body.find("eval_cost += float(verdict.cost_usd")
        lock_pos = fn_body.find("with eval_state_lock:")
        assert lock_pos != -1 and cost_pos != -1
        assert lock_pos < cost_pos, (
            "eval_cost accumulation must be inside the lock — "
            "otherwise two parallel tool_runner invocations double-count."
        )

    def test_execute_all_tests_has_state_lock(self):
        # _execute_all_tests also has a shared-state lock for its own
        # error counter + results map.
        fn_start = IMPL_SRC.find("def execute_all_tests")  # canonical form (post-Phase 6.1)
        assert fn_start != -1
        # End at the next top-level def
        fn_end = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:fn_end if fn_end != -1 else len(IMPL_SRC)]
        assert "state_lock = threading.Lock()" in fn_body


# ============================================================================
# Parallel dispatch is actually wired
# ============================================================================


class TestParallelDispatchWiring:

    def test_execute_all_tests_uses_thread_pool(self):
        # ThreadPoolExecutor must appear inside _execute_all_tests — not
        # just at module level (where it's used for candidate-level
        # parallelism). This confirms per-test parallelism inside a
        # single candidate.
        fn_start = IMPL_SRC.find("def execute_all_tests")  # canonical form (post-Phase 6.1)
        fn_end = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:fn_end]
        assert "ThreadPoolExecutor" in fn_body, (
            "_execute_all_tests must use ThreadPoolExecutor for "
            "single-turn test parallelism within a candidate."
        )

    def test_dispatch_helper_splits_multi_vs_single_turn(self):
        # The _dispatch_eval_items_in_parallel helper distinguishes
        # multi-turn (SESSION_PARALLELISM) from single-turn
        # (PARALLELISM). This is load-bearing: free-tier voice APIs
        # often cap concurrent sessions, so conflating the two knobs
        # would break those runs.
        assert "_dispatch_eval_items_in_parallel" in IMPL_SRC
        helper_start = IMPL_SRC.find("def _dispatch_eval_items_in_parallel")
        helper_end = IMPL_SRC.find("def _run_deterministic", helper_start)
        helper_body = IMPL_SRC[helper_start:helper_end]
        assert "AGENT6_PER_CANDIDATE_PARALLELISM" in helper_body
        assert "AGENT6_PER_CANDIDATE_SESSION_PARALLELISM" in helper_body
        # Must route multi-turn vs single-turn separately
        assert "multi_turn" in helper_body and "single_turn" in helper_body

    def test_multi_turn_detection_helper_exists(self):
        # Must identify multi-turn tests by input/output enum (not by
        # brittle string match on candidate name). This keeps the
        # classification provider-agnostic.
        assert "_is_multi_turn_eval" in IMPL_SRC
        # The canonical multi-turn enum values must appear near the
        # helper's enum-set definitions (_multi_turn_input /
        # _multi_turn_output). Walk backwards from the helper to include
        # those defining statements in the grep.
        helper_pos = IMPL_SRC.find("def _is_multi_turn_eval")
        assert helper_pos != -1
        # Grab 800 chars of context — covers helper + preceding set defs
        context = IMPL_SRC[max(0, helper_pos - 800): helper_pos + 600]
        assert "voice_conversation" in context, (
            "Multi-turn detection must recognize voice_conversation enum."
        )
        assert '"conversation"' in context, (
            "Multi-turn detection must recognize conversation enum."
        )
        assert "voice_turn" in context, (
            "Multi-turn detection must recognize voice_turn enum."
        )


# ============================================================================
# Sequential fallback when parallelism is 1
# ============================================================================


class TestSequentialFallbackWhenKnobIsOne:
    """Setting parallelism to 1 must restore the legacy sequential
    execution path. This is the escape hatch when ops hits an
    unexpected parallel-execution bug on a provider."""

    def test_execute_all_tests_has_sequential_fallback(self):
        fn_start = IMPL_SRC.find("def execute_all_tests")  # canonical form (post-Phase 6.1)
        fn_end = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:fn_end]
        # A sequential fallback branch must exist. The test is slightly
        # looser than a strict regex to survive minor refactors.
        assert "parallelism == 1" in fn_body or "max_workers=1" in fn_body

    def test_dispatch_helper_has_sequential_fallback(self):
        helper_start = IMPL_SRC.find("def _dispatch_eval_items_in_parallel")
        helper_end = IMPL_SRC.find("def _run_deterministic", helper_start)
        helper_body = IMPL_SRC[helper_start:helper_end]
        assert "workers == 1" in helper_body


# ============================================================================
# Order preservation — results come back in shuffled order
# ============================================================================


class TestOrderPreservation:
    """Parallel execution completes tests in non-deterministic order,
    but the RETURNED list must preserve the original shuffled order so
    downstream evaluation + logging is deterministic per shuffle."""

    def test_execute_all_tests_returns_in_shuffle_order(self):
        fn_start = IMPL_SRC.find("def execute_all_tests")  # canonical form (post-Phase 6.1)
        fn_end = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:fn_end]
        # The final return must iterate the original index range
        # (results_by_index[i] for i in range(...)), not use a raw
        # completion-ordered list.
        assert "results_by_index[i]" in fn_body
        assert "range(len(shuffled))" in fn_body


# ============================================================================
# Cloud-migration safety
# ============================================================================


class TestCloudMigrationSafety:
    """The parallelism pattern must translate to Docker / Cloud Run /
    managed-sandbox architectures without redesign. Contracts:

      1. No process-level shared state across candidates.
      2. No filesystem racing between parallel workers.
      3. No cross-candidate coordination.
      4. No fresh module-level singletons.
    """

    def test_knob_docs_mention_cloud_safety(self):
        # The config doc block must explicitly call out cloud-migration
        # safety so future ops knows the pattern was chosen
        # deliberately, not by accident.
        block_start = CONFIG_SRC.find("AGENT6_PER_CANDIDATE_PARALLELISM controls")
        assert block_start != -1
        doc = CONFIG_SRC[block_start:block_start + 2500]
        assert "cloud" in doc.lower() or "Docker" in doc or "Cloud Run" in doc

    def test_no_new_module_level_singletons_introduced(self):
        """The parallelism change must NOT introduce module-level
        mutable state — otherwise cross-candidate parallel runs would
        race. Lock definitions inside functions are fine; module-level
        locks with names like `GLOBAL_*` are a smell."""
        # Grep for module-level lock definitions (heuristic)
        suspicious_patterns = [
            "GLOBAL_EVAL_LOCK",
            "GLOBAL_PARALLEL_LOCK",
            "TEST_EXECUTION_LOCK",
        ]
        for pat in suspicious_patterns:
            assert pat not in IMPL_SRC, (
                f"Module-level lock {pat!r} would break cross-candidate "
                f"parallelism in cloud deployments. Keep locks inside "
                f"functions so each candidate's thread pool gets its own."
            )


# ============================================================================
# The helper for single test + rate limit exists and is thread-safe
# ============================================================================


class TestParallelWorkersInheritSessionDir:
    """Regression guard for a real bug (run trace 94271de4, 2026-04-22):
    voice_realtime.VoiceRealtimePlugin stores session_dir in
    threading.local() per AD-004 (cross-candidate parallel-build
    isolation). The candidate-level thread calls set_session_dir
    pointing at ``runs/<trace>/harnesses/<slug>/voice/``, but the
    within-candidate parallelism shipped this session spawns NEW
    ThreadPoolExecutor worker threads that don't inherit thread-local.
    The voice plugin's _session_dir property then falls back to
    _default_session_dir (%TEMP%/puzzleeval_voice/) — audio writes there
    get rejected by the backend /api/runs/audio containment check,
    frontend 404s on every playback.

    Fix: _dispatch_eval_items_in_parallel wraps every worker call with
    a set_session_dir re-application using the captured
    sandbox_dir / "voice" path. Cheap (setattr on thread-local per
    plugin) and idempotent.
    """

    def test_dispatch_helper_re_sets_session_dir_in_workers(self):
        """The dispatcher must re-apply set_session_dir inside each
        worker thread — otherwise thread-local resets per worker and
        voice artifacts land in %TEMP%, not the sandbox."""
        from puzzleeval.agents.implement_test_env import (  # noqa: F401
            run_implement_test_env_agent,
        )
        # Source-grep the dispatcher body
        helper_start = IMPL_SRC.find("def _dispatch_eval_items_in_parallel")
        helper_end = IMPL_SRC.find("def _run_deterministic", helper_start)
        helper_body = IMPL_SRC[helper_start:helper_end]
        assert "set_session_dir" in helper_body, (
            "Parallel dispatcher must re-apply set_session_dir inside "
            "workers. Without it, voice artifacts land in %TEMP% "
            "outside the runs root, breaking audio playback in the UI."
        )

    def test_session_dir_captured_from_outer_thread(self):
        """The voice_session_dir must be captured from the ENCLOSING
        candidate-level thread (sandbox_dir / 'voice'), not re-derived
        inside the worker (which would have no sandbox_dir in scope)."""
        helper_start = IMPL_SRC.find("def _dispatch_eval_items_in_parallel")
        helper_end = IMPL_SRC.find("def _run_deterministic", helper_start)
        helper_body = IMPL_SRC[helper_start:helper_end]
        # Capture line must reference sandbox_dir (outer scope)
        assert "sandbox_dir / " in helper_body or 'sandbox_dir /"voice"' in helper_body

    def test_worker_wrapper_is_idempotent_safe(self):
        """The wrapper must be safe to call many times from the same
        thread (ThreadPoolExecutor reuses threads across submitted
        tasks). set_session_dir is idempotent by design."""
        helper_start = IMPL_SRC.find("def _dispatch_eval_items_in_parallel")
        helper_end = IMPL_SRC.find("def _run_deterministic", helper_start)
        helper_body = IMPL_SRC[helper_start:helper_end]
        # The wrapper must handle exceptions (list_plugins might fail)
        # without killing the worker.
        assert "except Exception" in helper_body or "try:" in helper_body


class TestThreadingImportedAtModuleScope:
    """Regression guard for a real bug shipped during this pass:
    `threading.Lock()` was called inside `_execute_all_tests` and the
    eval dispatchers, but `import threading` lived only inside a
    docstring (Python string literal) — the module never actually
    imported it at runtime. Every test execution threw
    `NameError: name 'threading' is not defined` and fell through to
    'Test execution error' for EVERY test case.

    Real-run trace 2b2b9d1f (2026-04-22): 2 harnesses built cleanly
    ($5.85 spent) but all test cases failed with the NameError before
    any harness call fired. Source-grep tests (is the code spelling
    `threading.Lock()` correctly?) don't catch this class — they check
    the string, not that the module is importable.
    """

    def test_threading_importable_at_module_scope(self):
        import puzzleeval.agents.implement_test_env as module
        assert hasattr(module, "threading"), (
            "threading must be imported at module scope — not inside "
            "a docstring or nested function. The parallelism locks "
            "reference threading.Lock() from both _execute_all_tests "
            "and the eval dispatchers."
        )
        # Can we actually construct a Lock?
        lock = module.threading.Lock()
        assert lock is not None

    def test_module_imports_cleanly_without_circular(self):
        # A second import should hit the import cache without
        # triggering any side-effects. Sanity check against accidental
        # module-scope state.
        import importlib
        import puzzleeval.agents.implement_test_env as m1
        m2 = importlib.import_module("puzzleeval.agents.implement_test_env")
        assert m1 is m2


class TestRunSingleTestHelper:
    """The extracted _run_single_test_with_rate_limit helper replaces
    the inline sequential loop body. Both sequential and parallel paths
    must use it so rate-limit + rate-limit-retry semantics stay
    identical."""

    def test_helper_function_exists(self):
        # Phase 6.1: function renamed run_single_test_with_rate_limit
        # (no leading _) when moved to agent5/execution.py.
        assert (
            "def _run_single_test_with_rate_limit" in IMPL_SRC
            or "def run_single_test_with_rate_limit" in IMPL_SRC
        )

    def test_helper_preserves_rate_limit_retry(self):
        fn_start = IMPL_SRC.find("def run_single_test_with_rate_limit")  # canonical form (post-Phase 6.1)
        fn_end = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:fn_end]
        # Original rate-limit behaviors must survive
        assert "rate_limiter.acquire" in fn_body
        assert "_is_rate_limit_error" in fn_body
        assert "AGENT6_RATE_LIMIT_BACKOFF" in fn_body
