"""Tests for ``puzzleeval.agents.agent5.runtime_state``.

The orchestrator owns ``_agent_state/runtime_state.json``. Agent reads it
via the ``read_file`` tool but is rejected by the tool-dispatch gate
when it tries to write. Tests cover:

  * init_runtime_state writes a valid initial JSON.
  * update_runtime_state computes phase from observable state.
  * The atomic-write path replaces the file cleanly (no half-write).
  * Errors and directives accumulate with bounded history.
  * read_runtime_state returns None on missing/corrupt file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from puzzleeval.agents.agent5 import runtime_state as rt
from puzzleeval.schemas import (
    Agent3Result,
    Agent5Input,
    Constraints,
    JudgementCriterion,
    ScreenedCandidate,
    SubTask,
    TestCase,
    UserUnderstandingOutput,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_input() -> Agent5Input:
    user = UserUnderstandingOutput(
        summary="x", sub_tasks=[SubTask(description="x", capability="x", search_keywords=["x"])],
        search_strategy="both", domain="test", search_keywords=["x"],
        constraints=Constraints(),
    )
    tc = TestCase(
        id="tc-001", sub_task_ref="x", scenario="s",
        input_type="text", output_type="extraction",
        input_data="i", expected_output="o",
        difficulty="easy", tags=[],
        judgement_criteria=[JudgementCriterion(criterion="c", weight=1.0, eval_type="rubric_score")],
    )
    return Agent5Input(
        validated_candidates=[_make_candidate()],
        user_understanding=user,
        test_cases=Agent3Result(test_cases=[tc], generation_notes="n", coverage_summary={"x": 1}),
        trace_id="trace-runtime",
    )


def _make_candidate() -> ScreenedCandidate:
    return ScreenedCandidate(
        name="TestSvc", provider="TestCo", description="d", pricing_model="per-token",
        claimed_capabilities=["x"], relevance_score=0.8, adoption_difficulty="easy",
        relevant_subtasks=["x"], source="https://x.com",
        verified_api_docs_url="https://docs.x.com", auth_method="api_key",
        api_access_method="free_tier", confirmed_capabilities=["x"],
        data_format_notes="JSON", screening_notes="ok",
    )


class _FakeState:
    """Minimal duck-typed BuildLoopState replacement for tests."""
    def __init__(self, **kw):
        self.turn = kw.get("turn", 0)
        self.accumulated_cost = kw.get("accumulated_cost", 0.0)
        accepted = kw.get(
            "implementation_plan_accepted",
            kw.get("build_gate_accepted", False),
        )
        self.implementation_plan_accepted = accepted
        self.build_gate_accepted = accepted
        self.verification_attempts = kw.get("verification_attempts", 0)
        self.verification_passed = kw.get("verification_passed", False)
        self.smoke_ever_passed = kw.get("smoke_ever_passed", False)
        self.smoke_passed_at_turn = kw.get("smoke_passed_at_turn", -1)
        self.consecutive_errors = kw.get("consecutive_errors", 0)
        self.total_reassessments = kw.get("total_reassessments", 0)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


class TestInitRuntimeState:
    def test_creates_agent_state_directory(self, tmp_path: Path):
        rt.init_runtime_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            initial_model="claude-opus-4-7",
        )
        assert (tmp_path / "_agent_state").is_dir()
        assert (tmp_path / "_agent_state" / "runtime_state.json").is_file()

    def test_initial_state_fields(self, tmp_path: Path):
        rt.init_runtime_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            initial_model="claude-opus-4-7",
        )
        data = json.loads((tmp_path / "_agent_state" / "runtime_state.json").read_text())
        assert data["candidate_name"] == "TestSvc"
        assert data["trace_id"] == "trace-runtime"
        assert data["current_phase"] == "phase_1_research"
        assert data["current_turn"] == 0
        assert data["current_model"] == "claude-opus-4-7"
        assert data["effective_max_turns"] == 40
        assert data["effective_max_budget_usd"] == 3.0
        assert data["smoke_test_status"] == "not_run"
        assert data["migration_flags"]["PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED"] is True
        assert data["migration_flags"]["PUZZLEEVAL_RESEARCH_WORKERS_ENABLED"] is True
        assert data["migration_flags"]["PUZZLEEVAL_PERSISTENT_WORKER_RUNTIME_ENABLED"] is True
        assert data["migration_flags"]["PUZZLEEVAL_FAILURE_PACKET_DEBUG_ENABLED"] is True
        assert data["migration_flags"]["PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED"] is True
        assert data["migration_flags"]["PUZZLEEVAL_EFFICIENCY_SUMMARY_ENABLED"] is True
        assert data["implementation_plan_accepted"] is False
        assert data["live_test_status"] == "not_run"
        assert data["files_present"] == []
        # Default architecture tracks research/plan artifacts plus scaffold files.
        assert "harness.py" in data["files_pending"]
        assert "_agent_state/research_plan.json" in data["files_pending"]
        assert "_agent_state/research_synthesis.json" in data["files_pending"]
        assert "_agent_state/implementation_plan.json" in data["files_pending"]


# ---------------------------------------------------------------------------
# Phase derivation
# ---------------------------------------------------------------------------


class TestDeriveCurrentPhase:
    def test_phase_1_before_implementation_plan_acceptance(self, tmp_path: Path):
        state = _FakeState(implementation_plan_accepted=False)
        assert rt.derive_current_phase(tmp_path, state) == "phase_1_research"

    def test_phase_1_when_plan_file_exists_but_not_accepted(self, tmp_path: Path):
        state_dir = tmp_path / "_agent_state"
        state_dir.mkdir()
        (state_dir / "implementation_plan.json").write_text("{}", encoding="utf-8")
        state = _FakeState(implementation_plan_accepted=False)
        assert rt.derive_current_phase(tmp_path, state) == "phase_1_research"

    def test_phase_2_when_implementation_plan_accepted(self, tmp_path: Path):
        state = _FakeState(implementation_plan_accepted=True)
        assert rt.derive_current_phase(tmp_path, state) == "phase_2_build"

    def test_phase_3_when_harness_exists_but_smoke_not_passed(self, tmp_path: Path):
        (tmp_path / "harness.py").write_text("pass")
        state = _FakeState(implementation_plan_accepted=True, smoke_ever_passed=False)
        assert rt.derive_current_phase(tmp_path, state) == "phase_3_verify"

    def test_phase_4_when_smoke_passed(self, tmp_path: Path):
        (tmp_path / "harness.py").write_text("pass")
        state = _FakeState(implementation_plan_accepted=True, smoke_ever_passed=True)
        assert rt.derive_current_phase(tmp_path, state) == "phase_4_deliver"


# ---------------------------------------------------------------------------
# Update + atomic write
# ---------------------------------------------------------------------------


class TestUpdateRuntimeState:
    def test_update_advances_turn_counter(self, tmp_path: Path):
        rt.init_runtime_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            initial_model="claude-opus-4-7",
        )
        state = _FakeState(turn=5, accumulated_cost=0.42)
        rt.update_runtime_state(
            sandbox_dir=tmp_path,
            state=state,
            current_model="claude-opus-4-7",
        )
        data = rt.read_runtime_state(tmp_path)
        assert data is not None
        assert data["current_turn"] == 5
        assert data["accumulated_cost_usd"] == pytest.approx(0.42)

    def test_phase_entered_at_turn_bumps_on_transition(self, tmp_path: Path):
        rt.init_runtime_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            initial_model="claude-opus-4-7",
        )
        # First update at turn 3, still in phase 1
        rt.update_runtime_state(
            sandbox_dir=tmp_path,
            state=_FakeState(turn=3, implementation_plan_accepted=False),
            current_model="claude-opus-4-7",
        )
        a = rt.read_runtime_state(tmp_path)
        assert a["current_phase"] == "phase_1_research"
        assert a["phase_entered_at_turn"] == 0  # No transition yet

        # Now accept the implementation plan and update at turn 5.
        rt.update_runtime_state(
            sandbox_dir=tmp_path,
            state=_FakeState(turn=5, implementation_plan_accepted=True),
            current_model="claude-opus-4-7",
        )
        b = rt.read_runtime_state(tmp_path)
        assert b["current_phase"] == "phase_2_build"
        assert b["phase_entered_at_turn"] == 5  # Bumped on transition

    def test_status_fields_follow_passed_state(self, tmp_path: Path):
        rt.init_runtime_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            initial_model="claude-opus-4-7",
        )
        rt.update_runtime_state(
            sandbox_dir=tmp_path,
            state=_FakeState(
                turn=9,
                implementation_plan_accepted=True,
                smoke_ever_passed=True,
                smoke_passed_at_turn=8,
                verification_passed=True,
            ),
            current_model="claude-opus-4-7",
        )
        data = rt.read_runtime_state(tmp_path)
        assert data["smoke_test_status"] == "passing"
        assert data["live_test_status"] == "passing"

    def test_live_status_can_pass_while_completion_gate_fails(self, tmp_path: Path):
        rt.init_runtime_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            initial_model="claude-opus-4-7",
        )
        rt.update_runtime_state(
            sandbox_dir=tmp_path,
            state=_FakeState(
                turn=14,
                implementation_plan_accepted=True,
                smoke_ever_passed=True,
                smoke_passed_at_turn=8,
                verification_passed=False,
            ),
            current_model="claude-opus-4-7",
            live_test_status="passing",
            live_passed_at_turn=13,
            last_live_test_output="LIVE TEST PASSED\nsuccess=True",
            completion_gate_status="failed",
            completion_gate_issues=["reflection_missing"],
        )
        data = rt.read_runtime_state(tmp_path)
        assert data["live_test_status"] == "passing"
        assert data["live_passed_at_turn"] == 13
        assert "LIVE TEST PASSED" in data["last_live_test_output"]
        assert data["completion_gate_status"] == "failed"
        assert data["completion_gate_issues"] == ["reflection_missing"]
        assert data["verification_passed"] is False

    def test_errors_history_accumulates_and_caps(self, tmp_path: Path):
        rt.init_runtime_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            initial_model="claude-opus-4-7",
        )
        # Push 60 errors; history should cap at 50
        for i in range(60):
            rt.update_runtime_state(
                sandbox_dir=tmp_path,
                state=_FakeState(turn=i),
                current_model="claude-opus-4-7",
                errors_encountered_this_turn=[
                    {"category": "auth", "message": f"err {i}"},
                ],
            )
        data = rt.read_runtime_state(tmp_path)
        assert len(data["errors_history"]) == 50
        # The most recent should be err 59
        assert data["errors_history"][-1]["message"] == "err 59"

    def test_directives_fired_accumulates_and_caps(self, tmp_path: Path):
        rt.init_runtime_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            initial_model="claude-opus-4-7",
        )
        for i in range(25):
            rt.update_runtime_state(
                sandbox_dir=tmp_path,
                state=_FakeState(turn=i),
                current_model="claude-opus-4-7",
                directive_fired={"directive": "build_gate_compaction", "reason": "test"},
            )
        data = rt.read_runtime_state(tmp_path)
        assert len(data["directives_fired"]) == 20  # capped


class TestRuntimeSnapshot:
    def test_snapshot_records_active_tool_contract(self, tmp_path: Path):
        snapshot = rt.write_runtime_snapshot(tmp_path)
        path = tmp_path / "_agent_state" / "runtime_snapshot.json"
        assert path.is_file()
        assert ".md" in snapshot["allowed_extensions"]
        assert "_agent_state/runtime_state.json" in snapshot["orchestrator_owned_artifacts"]
        assert "tools_module_path" in snapshot
        assert "gate_flags" in snapshot


class TestReadRuntimeState:
    def test_missing_file_returns_none(self, tmp_path: Path):
        assert rt.read_runtime_state(tmp_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path):
        state_dir = tmp_path / "_agent_state"
        state_dir.mkdir()
        (state_dir / "runtime_state.json").write_text("{not valid json")
        assert rt.read_runtime_state(tmp_path) is None


class TestReadAgentObservations:
    def test_missing_file_returns_none(self, tmp_path: Path):
        assert rt.read_agent_observations(tmp_path) is None

    def test_returns_dict_when_present(self, tmp_path: Path):
        state_dir = tmp_path / "_agent_state"
        state_dir.mkdir()
        (state_dir / "agent_observations.json").write_text(
            json.dumps({"observations": [{"turn": 1, "category": "phase", "note": "n"}]})
        )
        data = rt.read_agent_observations(tmp_path)
        assert data is not None
        assert "observations" in data
        assert len(data["observations"]) == 1
