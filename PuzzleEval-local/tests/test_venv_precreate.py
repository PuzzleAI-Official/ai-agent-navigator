"""Regression guards for venv pre-creation (PLAN_VOICE_RUN_OPTIMIZATIONS.md item 3).

Architecture:
  - `precreate_venvs_for_candidates` runs venv setup + pip install in
    parallel via ThreadPoolExecutor for the SELECTED candidates only.
  - Hooked from `puzzleeval-api/services/pipeline_runner.py::_kick_off_venv_precreate`
    AFTER selection submission, BEFORE Agent 4 starts. Runs as a
    background asyncio task — does not block the event loop.
  - `_create_venv` short-circuits via the per-sandbox lock when it
    sees an existing venv python interpreter, so when Agent 5 calls
    `_create_venv` later it finds the pre-created venv and returns
    immediately.

These tests lock:
  1. Pre-create runs ONLY for the selected candidates passed to it
     (not all Agent 2 candidates).
  2. Pre-create runs in parallel (ThreadPoolExecutor, max_workers > 1).
  3. Agent 5's `_create_venv` short-circuits when the venv already
     exists (avoids duplicate work + pip install).

Reference: PLAN_VOICE_RUN_OPTIMIZATIONS.md item 3.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ============================================================================
# Helpers
# ============================================================================


@pytest.fixture
def quiet_logger():
    """A logger that doesn't pollute the test output but captures records."""
    logger = logging.getLogger("test_venv_precreate")
    logger.setLevel(logging.DEBUG)
    return logger


@pytest.fixture(autouse=True)
def _reset_venv_locks():
    """Each test starts with an empty per-sandbox lock registry so locks
    from prior tests don't leak across cases."""
    from puzzleeval.agents.implement_test_env import (
        _VENV_CREATE_LOCKS,
        _VENV_CREATE_LOCKS_GUARD,
    )
    with _VENV_CREATE_LOCKS_GUARD:
        _VENV_CREATE_LOCKS.clear()
    yield
    with _VENV_CREATE_LOCKS_GUARD:
        _VENV_CREATE_LOCKS.clear()


def _venv_python_path(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _seed_existing_venv(sandbox_dir: Path) -> Path:
    """Create a fake venv-python file so `_create_venv`'s short-circuit
    check finds it. Doesn't actually contain a working python — the
    test only verifies the short-circuit path is taken."""
    venv_dir = sandbox_dir / ".venv"
    py = _venv_python_path(venv_dir)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"#!fake python\n")
    return py


# ============================================================================
# 1. precreate_venvs_for_candidates runs ONLY for the names passed in
# ============================================================================


class TestPreCreateOnlyRunsForSelectedCandidates:
    """The hook must be a `selected-only` operation — Agent 2 typically
    yields 5-10 candidates, the user picks 2-3, and ONLY those should
    get venvs. Pre-creating venvs for non-selected candidates would
    waste 60-120s of disk + time per skip.
    """

    def test_only_named_candidates_get_venv_create_calls(
        self, tmp_path, quiet_logger,
    ):
        """When called with names ['A', 'B'], only A and B's sandbox
        dirs should see _create_venv invocations — not 'C' or 'D' that
        also exist in some hypothetical Agent 2 pool."""
        from puzzleeval.agents.implement_test_env import (
            precreate_venvs_for_candidates,
        )

        called_names: list[str] = []
        called_lock = threading.Lock()

        def fake_create(sandbox_dir, logger, trace_id, name):
            with called_lock:
                called_names.append(name)
            return True

        with patch(
            "puzzleeval.agents.agent5.sandbox.create_venv",
            side_effect=fake_create,
        ):
            results = precreate_venvs_for_candidates(
                candidate_names=["Selected A", "Selected B"],
                trace_id="trace-test",
                logger=quiet_logger,
                runs_root=tmp_path,
            )

        assert sorted(called_names) == ["Selected A", "Selected B"]
        assert results == {"Selected A": True, "Selected B": True}

    def test_empty_candidate_list_is_noop(self, tmp_path, quiet_logger):
        """Defensive — empty list returns empty dict without spawning
        an executor (avoids `max_workers=0` edge case)."""
        from puzzleeval.agents.implement_test_env import (
            precreate_venvs_for_candidates,
        )

        with patch(
            "puzzleeval.agents.agent5.sandbox.create_venv",
        ) as mock_create:
            results = precreate_venvs_for_candidates(
                candidate_names=[],
                trace_id="trace-test",
                logger=quiet_logger,
                runs_root=tmp_path,
            )

        assert results == {}
        mock_create.assert_not_called()

    def test_failures_logged_not_raised(self, tmp_path, quiet_logger, caplog):
        """When _create_venv fails (returns False) for one candidate,
        the helper records False in the result dict but doesn't raise.
        Agent 5's _create_venv falls back to in-build venv create."""
        from puzzleeval.agents.implement_test_env import (
            precreate_venvs_for_candidates,
        )

        def fake_create(sandbox_dir, logger, trace_id, name):
            return name == "Good"  # Only "Good" succeeds

        with patch(
            "puzzleeval.agents.agent5.sandbox.create_venv",
            side_effect=fake_create,
        ):
            results = precreate_venvs_for_candidates(
                candidate_names=["Good", "Bad"],
                trace_id="trace-test",
                logger=quiet_logger,
                runs_root=tmp_path,
            )

        assert results == {"Good": True, "Bad": False}

    def test_exception_inside_create_does_not_propagate(
        self, tmp_path, quiet_logger,
    ):
        """If _create_venv raises (not just returns False), the helper
        catches it and records False — fire-and-forget background task
        must NEVER crash the event loop."""
        from puzzleeval.agents.implement_test_env import (
            precreate_venvs_for_candidates,
        )

        def fake_create(sandbox_dir, logger, trace_id, name):
            raise OSError("disk full")

        with patch(
            "puzzleeval.agents.agent5.sandbox.create_venv",
            side_effect=fake_create,
        ):
            # Must not raise
            results = precreate_venvs_for_candidates(
                candidate_names=["X"],
                trace_id="trace-test",
                logger=quiet_logger,
                runs_root=tmp_path,
            )
        assert results == {"X": False}


# ============================================================================
# 2. precreate runs in parallel (ThreadPoolExecutor)
# ============================================================================


class TestPreCreateRunsInParallel:
    """The whole point of pre-creation is to run venv setups
    concurrently with Agent 4. Sequential pre-creation of N candidates
    would take N × 90s; parallel takes max(per-candidate setup) ≈ 90-130s.
    """

    def test_parallel_execution_overlaps_in_time(
        self, tmp_path, quiet_logger,
    ):
        """Two slow venv creates should overlap in time, not stack.
        With 2 candidates × 0.3s each, parallel takes ~0.3s; sequential
        would take ~0.6s. We allow generous slack but assert the wall-
        clock is closer to one-create-time than two."""
        from puzzleeval.agents.implement_test_env import (
            precreate_venvs_for_candidates,
        )

        def slow_create(sandbox_dir, logger, trace_id, name):
            time.sleep(0.3)
            return True

        start = time.time()
        with patch(
            "puzzleeval.agents.agent5.sandbox.create_venv",
            side_effect=slow_create,
        ):
            precreate_venvs_for_candidates(
                candidate_names=["A", "B"],
                trace_id="trace-test",
                logger=quiet_logger,
                runs_root=tmp_path,
            )
        elapsed = time.time() - start

        # Sequential = 0.6s, parallel ≈ 0.3s + executor overhead.
        # Generous bound: must be < 0.55s (well under the sequential
        # ceiling), proving overlap.
        assert elapsed < 0.55, (
            f"Pre-create wall-clock {elapsed:.3f}s suggests sequential "
            f"execution (expected ~0.3s for 2-parallel)."
        )

    def test_max_workers_caps_at_5_or_candidate_count(
        self, tmp_path, quiet_logger,
    ):
        """The executor sizing should be `min(len(candidates), 5)` by
        default — protects against fork-bomb when a pathological
        scenario yields 50+ selected candidates (rare but possible)."""
        from puzzleeval.agents.implement_test_env import (
            precreate_venvs_for_candidates,
        )

        # Track concurrent-worker peak
        active = 0
        peak = 0
        lock = threading.Lock()

        def tracking_create(sandbox_dir, logger, trace_id, name):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return True

        with patch(
            "puzzleeval.agents.agent5.sandbox.create_venv",
            side_effect=tracking_create,
        ):
            # 10 candidates — default max_workers caps at 5
            precreate_venvs_for_candidates(
                candidate_names=[f"cand-{i}" for i in range(10)],
                trace_id="trace-test",
                logger=quiet_logger,
                runs_root=tmp_path,
            )

        assert peak <= 5, f"Peak concurrency {peak} exceeded the cap of 5"
        # And it WAS parallel (not 1 — would mean sequential)
        assert peak >= 2

    def test_max_workers_override_respected(
        self, tmp_path, quiet_logger,
    ):
        """Caller can override the worker cap explicitly."""
        from puzzleeval.agents.implement_test_env import (
            precreate_venvs_for_candidates,
        )

        active = 0
        peak = 0
        lock = threading.Lock()

        def tracking_create(sandbox_dir, logger, trace_id, name):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return True

        with patch(
            "puzzleeval.agents.agent5.sandbox.create_venv",
            side_effect=tracking_create,
        ):
            precreate_venvs_for_candidates(
                candidate_names=[f"cand-{i}" for i in range(5)],
                trace_id="trace-test",
                logger=quiet_logger,
                runs_root=tmp_path,
                max_workers=2,
            )

        assert peak <= 2


# ============================================================================
# 3. Agent 5's _create_venv short-circuits when venv already exists
# ============================================================================


class TestCreateVenvShortCircuits:
    """When pre-creation has already produced a venv, Agent 5's call to
    `_create_venv` (synchronous, from the build loop) must skip the
    work — no duplicate `python -m venv` invocation, no duplicate pip
    install, no race with the background pre-create."""

    def test_short_circuits_when_venv_python_exists(
        self, tmp_path, quiet_logger,
    ):
        """Pre-seed a fake venv python file; verify _create_venv returns
        True without invoking subprocess.run."""
        from puzzleeval.agents.implement_test_env import _create_venv

        sandbox_dir = tmp_path / "harnesses" / "test_cand"
        sandbox_dir.mkdir(parents=True)
        _seed_existing_venv(sandbox_dir)

        from unittest.mock import MagicMock
        pip_ok = MagicMock(returncode=0, stderr="", stdout="pip 24.0")
        with patch(
            "puzzleeval.agents.implement_test_env.subprocess.run",
            return_value=pip_ok,
        ) as mock_run:
            ok = _create_venv(sandbox_dir, quiet_logger, "trace-test", "TestCand")

        assert ok is True
        venv_create_calls = [
            call for call in mock_run.call_args_list
            if "-m" in call.args[0] and "venv" in call.args[0]
        ]
        assert not venv_create_calls, (
            "Short-circuit failed — _create_venv ran python -m venv "
            "even though the venv python already exists."
        )

    def test_repairs_partial_venv_when_create_command_fails(
        self, tmp_path, quiet_logger,
    ):
        """Windows+Anaconda can return non-zero from `python -m venv` while
        still leaving a usable python.exe behind. If ensurepip can repair
        pip, create_venv should treat the partial venv as usable instead of
        surfacing an infrastructure false failure."""
        from puzzleeval.agents.implement_test_env import _create_venv

        sandbox_dir = tmp_path / "harnesses" / "partial_cand"
        sandbox_dir.mkdir(parents=True)

        from unittest.mock import MagicMock

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.stderr = ""
            result.stdout = ""
            if "-m" in cmd and "venv" in cmd:
                _seed_existing_venv(sandbox_dir)
                result.returncode = 1
                result.stderr = "ensurepip failed during venv creation"
            elif "-m" in cmd and "pip" in cmd:
                result.returncode = 0
                result.stdout = "pip 24.0"
            elif "-m" in cmd and "ensurepip" in cmd:
                result.returncode = 0
                result.stdout = "Successfully installed pip"
            else:
                result.returncode = 0
            return result

        with patch(
            "puzzleeval.agents.implement_test_env.subprocess.run",
            side_effect=fake_run,
        ), patch.dict(
            "os.environ", {"PUZZLEEVAL_VENV_PREINSTALL": "0"},
        ):
            ok = _create_venv(sandbox_dir, quiet_logger, "trace-test", "Partial")

        assert ok is True

    def test_creates_fresh_venv_when_absent(
        self, tmp_path, quiet_logger,
    ):
        """Without a pre-existing venv, _create_venv runs subprocess.run
        as before — the short-circuit must NOT skip work for the
        fall-through case."""
        from puzzleeval.agents.implement_test_env import _create_venv

        sandbox_dir = tmp_path / "harnesses" / "test_cand_2"
        sandbox_dir.mkdir(parents=True)

        # Mock subprocess.run to "succeed" without actually creating a venv
        from unittest.mock import MagicMock
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""

        with patch(
            "puzzleeval.agents.implement_test_env.subprocess.run",
            return_value=fake_result,
        ) as mock_run, patch.dict(
            "os.environ", {"PUZZLEEVAL_VENV_PREINSTALL": "0"},
        ):
            ok = _create_venv(sandbox_dir, quiet_logger, "trace-test", "TestCand2")

        assert ok is True
        # Subprocess WAS called (the venv create command)
        assert mock_run.called

    def test_per_sandbox_lock_serializes_concurrent_callers(
        self, tmp_path, quiet_logger,
    ):
        """When pre-create AND Agent 5 race for the same sandbox, the
        lock must serialize them so only ONE actually runs the create.
        Validates the design that prevents concurrent pip install on
        the same venv (a real failure mode at file-system level on
        Windows)."""
        from puzzleeval.agents.implement_test_env import _create_venv

        sandbox_dir = tmp_path / "harnesses" / "race_cand"
        sandbox_dir.mkdir(parents=True)

        creation_count = 0
        creation_lock = threading.Lock()

        def fake_subprocess(*args, **kwargs):
            nonlocal creation_count
            # Count ONLY actual venv-create invocations. NEW-AM added a
            # `python -m pip --version` verification call after creation
            # (Windows+anaconda pip-bootstrap fix); that's an additional
            # subprocess.run we don't want to mistake for a duplicate
            # creation. The race-safety contract is: only ONE thread
            # actually invokes `python -m venv <dir>`, the other
            # short-circuits via the per-sandbox lock.
            cmd = args[0] if args else kwargs.get("args", [])
            is_venv_create = (
                isinstance(cmd, list) and len(cmd) >= 3
                and "-m" in cmd and "venv" in cmd
            )
            if is_venv_create:
                with creation_lock:
                    creation_count += 1
                # Simulate the venv being "created" by writing the python
                # file so the SECOND caller's short-circuit fires.
                _seed_existing_venv(sandbox_dir)
                time.sleep(0.05)  # Hold the lock briefly
            from unittest.mock import MagicMock
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = "pip 24.0"  # Make pip --version "succeed"
            return r

        with patch(
            "puzzleeval.agents.implement_test_env.subprocess.run",
            side_effect=fake_subprocess,
        ), patch.dict(
            "os.environ", {"PUZZLEEVAL_VENV_PREINSTALL": "0"},
        ):
            # Spawn two threads racing for the same sandbox
            results: list[bool] = []
            results_lock = threading.Lock()

            def caller():
                ok = _create_venv(sandbox_dir, quiet_logger, "trace-test", "Race")
                with results_lock:
                    results.append(ok)

            t1 = threading.Thread(target=caller)
            t2 = threading.Thread(target=caller)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        assert results == [True, True], "Both callers should succeed"
        assert creation_count == 1, (
            f"Per-sandbox lock failed — got {creation_count} venv-create "
            f"subprocess calls when there should have been exactly 1."
        )


# ============================================================================
# 4. Pipeline runner hook is wired correctly (source-grep guard)
# ============================================================================


class TestPipelineRunnerHookWired:
    """Source-grep regression guards for the pipeline_runner.py hook —
    if a future refactor drops these calls, venv pre-creation silently
    stops happening and we lose 60-120s per voice run."""

    def test_pipeline_runner_imports_precreate_helper(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2] / "puzzleeval-api" /
            "services" / "pipeline_runner.py"
        ).read_text(encoding="utf-8")
        assert "precreate_venvs_for_candidates" in src
        assert "_kick_off_venv_precreate" in src

    def test_pipeline_runner_kicks_off_after_user_selection(self):
        """The hook must fire AFTER `state.user_selection_applied = True`
        — pre-creating before selection is applied would target the
        WRONG (unfiltered) candidate list."""
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2] / "puzzleeval-api" /
            "services" / "pipeline_runner.py"
        ).read_text(encoding="utf-8")
        # Find the user-selection branch
        idx_apply = src.find("state.user_selection_applied = True")
        assert idx_apply != -1, "user_selection_applied flag missing"
        # The kickoff must appear after this point in the file
        idx_kickoff = src.find(
            "_kick_off_venv_precreate", idx_apply,
        )
        assert idx_kickoff != -1, (
            "venv pre-create hook must fire AFTER user_selection_applied — "
            "the search starting from user_selection_applied found nothing."
        )

    def test_pipeline_runner_passes_only_filtered_candidates(self):
        """The hook must pass the POST-FILTER candidate list, not the
        pre-filter (Agent 2's full pool). The variable used is
        `filtered_candidates` (from `state.agent2_result.get('candidates',
        [])` AFTER `apply_scope_picks`) — the same source the
        candidates_found re-emit uses."""
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2] / "puzzleeval-api" /
            "services" / "pipeline_runner.py"
        ).read_text(encoding="utf-8")
        # Find the user-selection branch's hook call
        idx = src.find("_kick_off_venv_precreate(state, filtered_candidates")
        assert idx != -1, (
            "User-selection branch must pass `filtered_candidates` "
            "(post-Phase-6 filter) to the pre-create hook."
        )
        call_tail = src[idx:src.find("\n", idx)]
        assert "runs_root=runs_dir" in call_tail, (
            "Pre-create must use the same explicit runs root as Agent 4/5 "
            "so venvs land in the candidate sandbox Agent 5 will use."
        )


# ============================================================================
# 5. Phase 2 R2 invariant: module-level state identity across modules
# ============================================================================


class TestVenvLockDictSingletonAcrossModules:
    """R2 invariant from the refactor plan's risk register.

    After Phase 2 extracted venv code from implement_test_env.py to
    agent5/sandbox.py, the lock dict (_VENV_CREATE_LOCKS) became
    module-level state in sandbox.py. The legacy implement_test_env.py
    re-exports the SAME dict object — it does NOT redeclare it.

    If a future refactor accidentally creates a SECOND dict (e.g., by
    declaring `_VENV_CREATE_LOCKS = {}` in implement_test_env.py
    instead of importing from sandbox), two threads racing for the
    same sandbox dir would each acquire a DIFFERENT lock — venv
    creation would NOT be serialized — concurrent pip install would
    corrupt the venv.

    These tests catch that regression at CI time, not at run time.
    """

    def test_venv_create_locks_dict_is_singleton(self):
        from puzzleeval.agents import implement_test_env as ite
        from puzzleeval.agents.agent5 import sandbox

        assert ite._VENV_CREATE_LOCKS is sandbox._VENV_CREATE_LOCKS, (
            "R2 INVARIANT VIOLATED: _VENV_CREATE_LOCKS must be the SAME "
            "dict object across implement_test_env and agent5.sandbox. "
            "If you see this fail, somebody redeclared the dict instead "
            "of re-importing it. Fix: in implement_test_env.py, the "
            "name should be `from puzzleeval.agents.agent5.sandbox "
            "import _VENV_CREATE_LOCKS`, NOT `_VENV_CREATE_LOCKS = {}`."
        )

    def test_venv_create_locks_guard_is_singleton(self):
        from puzzleeval.agents import implement_test_env as ite
        from puzzleeval.agents.agent5 import sandbox

        assert ite._VENV_CREATE_LOCKS_GUARD is sandbox._VENV_CREATE_LOCKS_GUARD

    def test_lock_for_same_path_returns_same_object(self):
        """acquire_venv_lock(p) and acquire_venv_lock(p) for the same path
        return the SAME lock object — both via legacy and canonical paths."""
        from pathlib import Path
        from puzzleeval.agents import implement_test_env as ite
        from puzzleeval.agents.agent5 import sandbox

        sb = Path("runs/test_lock_singleton/cand_x").resolve()
        lock_legacy = ite._venv_lock_for(sb)
        lock_canonical = sandbox.acquire_venv_lock(sb)
        assert lock_legacy is lock_canonical, (
            "Lock for the same sandbox path must be the same object "
            "regardless of import path."
        )
        # And calling twice returns the cached lock (not a new one)
        lock_again = sandbox.acquire_venv_lock(sb)
        assert lock_again is lock_canonical

    def test_venv_preinstall_manifest_is_singleton(self):
        from puzzleeval.agents import implement_test_env as ite
        from puzzleeval.agents.agent5 import sandbox

        assert ite.VENV_PREINSTALL_MANIFEST is sandbox.VENV_PREINSTALL_MANIFEST
