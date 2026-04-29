"""Helper-level tests for puzzleeval.agents.agent5.build_loop (Phase 5 Step 1).

These pin the contracts of BuildContext (frozen) and BuildLoopState
(mutable) before they're wired into ``_build_single_harness`` in Step 3.

End-to-end coverage stays in tests/test_build_loop_behavior.py — those
behavior tests are the safety harness for the Step 3 move. This file
exists to catch dataclass-shape regressions at sub-millisecond cost.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from puzzleeval.agents.agent5.build_loop import BuildContext, BuildLoopState


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_input_data(trace_id="test-trace-001"):
    """Mock Agent5Input with a trace_id (the only field BuildContext reads)."""
    input_data = MagicMock()
    input_data.trace_id = trace_id
    return input_data


def _make_candidate(name="TestCandidate"):
    """Mock ScreenedCandidate with a name (only field BuildContext reads)."""
    cand = MagicMock()
    cand.name = name
    return cand


def _make_ctx(**overrides):
    """Build a BuildContext with sensible defaults; keyword-overridable."""
    defaults = {
        "client": MagicMock(),
        "candidate": _make_candidate(),
        "input_data": _make_input_data(),
        "sandbox_dir": Path("/tmp/sandbox-test"),
        "logger": MagicMock(),
        "progress_callback": None,
    }
    defaults.update(overrides)
    return BuildContext(**defaults)


# ---------------------------------------------------------------------------
# BuildContext — frozen + derived field contracts
# ---------------------------------------------------------------------------


class TestBuildContextFrozenContract:
    def test_construction_with_six_inputs(self):
        ctx = _make_ctx()
        # All 6 inputs accessible
        assert ctx.client is not None
        assert ctx.candidate is not None
        assert ctx.input_data is not None
        assert ctx.sandbox_dir == Path("/tmp/sandbox-test")
        assert ctx.logger is not None
        assert ctx.progress_callback is None  # default

    def test_progress_callback_is_optional(self):
        cb = lambda evt, payload: None  # noqa: E731
        ctx = _make_ctx(progress_callback=cb)
        assert ctx.progress_callback is cb

    def test_is_frozen_no_mutation_allowed(self):
        ctx = _make_ctx()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.client = "different"

    def test_derived_trace_id_from_input_data(self):
        input_data = _make_input_data(trace_id="custom-trace-xyz")
        ctx = _make_ctx(input_data=input_data)
        assert ctx.trace_id == "custom-trace-xyz"

    def test_derived_candidate_label_from_candidate_name(self):
        # candidate_slug strips non-alphanumeric, lowercases. Real
        # behavior locked: "OpenAI Realtime!" → "openai_realtime".
        cand = _make_candidate(name="OpenAI Realtime!")
        ctx = _make_ctx(candidate=cand)
        # Don't assert exact slug — that's candidate_slug's contract,
        # not ours. Just assert it's non-empty + deterministic.
        assert ctx.candidate_label
        assert isinstance(ctx.candidate_label, str)

    def test_derived_fields_cannot_be_overridden_at_construction(self):
        # Frozen dataclass with init=False fields rejects passing them
        # in the constructor.
        with pytest.raises(TypeError):
            BuildContext(
                client=MagicMock(),
                candidate=_make_candidate(),
                input_data=_make_input_data(),
                sandbox_dir=Path("/tmp"),
                logger=MagicMock(),
                trace_id="cannot-set-this",  # init=False — rejected
            )


# ---------------------------------------------------------------------------
# BuildLoopState — defaults + mutability
# ---------------------------------------------------------------------------


class TestBuildLoopStateDefaults:
    """Each field's default must match the inline ``var = default`` line
    it replaces in ``_build_single_harness``. If a default changes,
    behavior could shift silently — pin them."""

    def test_zero_initialized_counters(self):
        state = BuildLoopState()
        assert state.turn == 0
        assert state.accumulated_cost == 0.0
        assert state.total_web_searches == 0
        assert state.candidate_web_fetch_blocks == 0

    def test_empty_collections(self):
        state = BuildLoopState()
        assert state.messages == []
        assert state.conversation_log == []
        assert state.error_history == []
        assert state.progress_ring == []
        assert state.approaches_tried == []
        assert state.saved_doc_files == []
        assert state.patch_fragmentation_nudged_files == set()
        assert state.build_read_state == {}

    def test_phase_transition_flag_starts_false(self):
        state = BuildLoopState()
        assert state.api_spec_written is False

    def test_smoke_pass_flags_default(self):
        state = BuildLoopState()
        assert state.smoke_ever_passed is False
        assert state.smoke_passed_at_turn == -1  # sentinel: never passed

    def test_verification_state_defaults(self):
        state = BuildLoopState()
        assert state.verification_attempts == 0
        assert state.verification_passed is False

    def test_dead_end_state_defaults(self):
        state = BuildLoopState()
        assert state.consecutive_errors == 0
        assert state.total_reassessments == 0

    def test_nudge_flags_default_unsent(self):
        state = BuildLoopState()
        assert state.diminishing_nudge_sent is False
        assert state.turn_budget_nudge_sent is False

    def test_last_text_starts_empty(self):
        state = BuildLoopState()
        assert state.last_text == ""

    def test_build_start_time_default(self):
        # 0.0 = uninitialized. Caller MUST set it via time.monotonic()
        # before the loop begins.
        state = BuildLoopState()
        assert state.build_start_time == 0.0


class TestBuildLoopStateMutability:
    def test_state_is_mutable(self):
        state = BuildLoopState()
        # All fields should be assignable
        state.turn = 5
        assert state.turn == 5
        state.accumulated_cost = 1.234
        assert state.accumulated_cost == 1.234
        state.api_spec_written = True
        assert state.api_spec_written is True

    def test_independent_collections_per_instance(self):
        # default_factory creates a NEW collection per instance —
        # mutating one state's list must NOT leak to another.
        s1 = BuildLoopState()
        s2 = BuildLoopState()
        s1.messages.append({"role": "user", "content": "hi"})
        assert s2.messages == []
        assert s1.messages != s2.messages

    def test_independent_dicts_per_instance(self):
        s1 = BuildLoopState()
        s2 = BuildLoopState()
        s1.build_read_state["foo.py"] = 123.45
        assert "foo.py" not in s2.build_read_state

    def test_independent_sets_per_instance(self):
        s1 = BuildLoopState()
        s2 = BuildLoopState()
        s1.patch_fragmentation_nudged_files.add("harness.py")
        assert "harness.py" not in s2.patch_fragmentation_nudged_files

    def test_error_history_accepts_tuples(self):
        state = BuildLoopState()
        state.error_history.append((3, "auth"))
        state.error_history.append((4, "endpoint"))
        assert state.error_history == [(3, "auth"), (4, "endpoint")]

    def test_messages_grow_through_loop_iterations(self):
        # Simulate the loop appending assistant + user messages each turn
        state = BuildLoopState()
        state.messages.append({"role": "user", "content": "initial"})
        state.messages.append({"role": "assistant", "content": "response 1"})
        state.messages.append({"role": "user", "content": "tool results"})
        assert len(state.messages) == 3


# ---------------------------------------------------------------------------
# _should_inject_turn_budget_nudge — pure predicate
# ---------------------------------------------------------------------------


class TestShouldInjectTurnBudgetNudge:
    """The nudge fires when ALL four conditions hold:
       1. turns_remaining <= 3
       2. nudge has NOT already been sent
       3. smoke has NOT passed yet
       4. messages is non-empty (there's a conversation to nudge)
    """

    def test_fires_at_3_turns_remaining(self):
        from puzzleeval.agents.agent5.build_loop import (
            _should_inject_turn_budget_nudge,
        )
        state = BuildLoopState()
        state.turn = 22  # 22/25 → 3 remaining
        state.messages = [{"role": "user", "content": "x"}]
        assert _should_inject_turn_budget_nudge(state, max_turns=25)

    def test_does_not_fire_with_4_turns_remaining(self):
        from puzzleeval.agents.agent5.build_loop import (
            _should_inject_turn_budget_nudge,
        )
        state = BuildLoopState()
        state.turn = 21  # 21/25 → 4 remaining
        state.messages = [{"role": "user", "content": "x"}]
        assert not _should_inject_turn_budget_nudge(state, max_turns=25)

    def test_does_not_fire_after_already_sent(self):
        from puzzleeval.agents.agent5.build_loop import (
            _should_inject_turn_budget_nudge,
        )
        state = BuildLoopState()
        state.turn = 22
        state.messages = [{"role": "user", "content": "x"}]
        state.turn_budget_nudge_sent = True
        assert not _should_inject_turn_budget_nudge(state, max_turns=25)

    def test_does_not_fire_after_smoke_passed(self):
        from puzzleeval.agents.agent5.build_loop import (
            _should_inject_turn_budget_nudge,
        )
        state = BuildLoopState()
        state.turn = 22
        state.messages = [{"role": "user", "content": "x"}]
        state.smoke_ever_passed = True
        assert not _should_inject_turn_budget_nudge(state, max_turns=25)

    def test_does_not_fire_with_empty_messages(self):
        # No conversation yet → nothing to nudge
        from puzzleeval.agents.agent5.build_loop import (
            _should_inject_turn_budget_nudge,
        )
        state = BuildLoopState()
        state.turn = 22
        # state.messages = [] (default)
        assert not _should_inject_turn_budget_nudge(state, max_turns=25)


# ---------------------------------------------------------------------------
# _initialize_loop_state — state seed contract
# ---------------------------------------------------------------------------


class TestInitializeLoopState:
    """Verify the state-seed function produces equivalent state to the
    inline initialization in ``_build_single_harness``."""

    def _make_setup(self, pre_rendered_spec=None, credentials=None,
                    staged_test_cases=None):
        """Mock BuildSetupSuccess shape — only the attributes
        _initialize_loop_state reads."""
        setup = MagicMock()
        setup.credentials = credentials or {}
        setup.staged_test_cases = staged_test_cases or []
        setup.pre_rendered_spec = pre_rendered_spec
        return setup

    def test_returns_buildloopstate(self, tmp_path, monkeypatch):
        from puzzleeval.agents.agent5.build_loop import (
            _initialize_loop_state,
        )

        # Mock _build_initial_message to avoid pulling in real prompt.
        monkeypatch.setattr(
            "puzzleeval.agents.implement_test_env._build_initial_message",
            lambda *a, **kw: "initial prompt text",
        )
        ctx = _make_ctx(sandbox_dir=tmp_path)
        setup = self._make_setup()
        state = _initialize_loop_state(ctx, setup)
        assert isinstance(state, BuildLoopState)

    def test_seeds_initial_message(self, tmp_path, monkeypatch):
        from puzzleeval.agents.agent5.build_loop import (
            _initialize_loop_state,
        )

        monkeypatch.setattr(
            "puzzleeval.agents.implement_test_env._build_initial_message",
            lambda *a, **kw: "the initial prompt",
        )
        ctx = _make_ctx(sandbox_dir=tmp_path)
        state = _initialize_loop_state(ctx, self._make_setup())
        assert len(state.messages) == 1
        assert state.messages[0]["role"] == "user"
        assert state.messages[0]["content"] == "the initial prompt"

    def test_seeds_prefetched_docs_when_present(
        self, tmp_path, monkeypatch,
    ):
        """Agent 4 saved fetched_docs_{0,1,2}.txt before Agent 5 runs;
        _initialize_loop_state must populate saved_doc_files so STEP 1
        can read them via read_file."""
        from puzzleeval.agents.agent5.build_loop import (
            _initialize_loop_state,
        )

        monkeypatch.setattr(
            "puzzleeval.agents.implement_test_env._build_initial_message",
            lambda *a, **kw: "prompt",
        )
        # Pre-create 3 fetched_docs files in the sandbox.
        for i in range(3):
            (tmp_path / f"fetched_docs_{i}.txt").write_text(f"doc {i}")

        ctx = _make_ctx(sandbox_dir=tmp_path)
        state = _initialize_loop_state(ctx, self._make_setup())
        assert state.saved_doc_files == [
            "fetched_docs_0.txt",
            "fetched_docs_1.txt",
            "fetched_docs_2.txt",
        ]

    def test_no_prefetched_docs_means_empty_list(
        self, tmp_path, monkeypatch,
    ):
        from puzzleeval.agents.agent5.build_loop import (
            _initialize_loop_state,
        )

        monkeypatch.setattr(
            "puzzleeval.agents.implement_test_env._build_initial_message",
            lambda *a, **kw: "prompt",
        )
        ctx = _make_ctx(sandbox_dir=tmp_path)
        state = _initialize_loop_state(ctx, self._make_setup())
        assert state.saved_doc_files == []

    def test_build_start_time_set_to_monotonic(
        self, tmp_path, monkeypatch,
    ):
        from puzzleeval.agents.agent5.build_loop import (
            _initialize_loop_state,
        )

        monkeypatch.setattr(
            "puzzleeval.agents.implement_test_env._build_initial_message",
            lambda *a, **kw: "prompt",
        )
        ctx = _make_ctx(sandbox_dir=tmp_path)
        state = _initialize_loop_state(ctx, self._make_setup())
        # Must be > 0 (monotonic time, post-init)
        assert state.build_start_time > 0.0

    def test_pre_render_log_fires_when_pre_rendered_spec_present(
        self, tmp_path, monkeypatch,
    ):
        from puzzleeval.agents.agent5.build_loop import (
            _initialize_loop_state,
        )

        monkeypatch.setattr(
            "puzzleeval.agents.implement_test_env._build_initial_message",
            lambda *a, **kw: "prompt",
        )
        ctx = _make_ctx(sandbox_dir=tmp_path)
        setup = self._make_setup(pre_rendered_spec="some spec text")
        _initialize_loop_state(ctx, setup)
        # Logger.info called (at least once for pre-render boundary)
        assert ctx.logger.info.called

    def test_other_state_fields_remain_at_defaults(
        self, tmp_path, monkeypatch,
    ):
        """The init function must NOT mutate fields it doesn't own —
        downstream loop logic depends on the documented defaults."""
        from puzzleeval.agents.agent5.build_loop import (
            _initialize_loop_state,
        )

        monkeypatch.setattr(
            "puzzleeval.agents.implement_test_env._build_initial_message",
            lambda *a, **kw: "prompt",
        )
        ctx = _make_ctx(sandbox_dir=tmp_path)
        state = _initialize_loop_state(ctx, self._make_setup())

        # All these are dataclass defaults that the inline code preserved
        assert state.turn == 0
        assert state.accumulated_cost == 0.0
        assert state.api_spec_written is False
        assert state.smoke_ever_passed is False
        assert state.consecutive_errors == 0
        assert state.verification_attempts == 0
        assert state.error_history == []
        assert state.approaches_tried == []
        assert state.build_read_state == {}
        assert state.turn_budget_nudge_sent is False
