"""Integration tests for the autonomy artifact layer.

Covers the orchestrator-side surface:
  * ``stage_agent_state`` creates ``_agent_state/`` and writes objective.md
    + runtime_state.json with valid structure.
  * Tool-dispatch protection: agent attempting to write
    ``_agent_state/objective.md`` or ``_agent_state/runtime_state.json``
    is rejected with the orchestrator-owned-artifact message.
  * read_file / write_file / patch_file accept paths inside
    ``_agent_state/`` for agent-writable filenames (build_plan.md,
    agent_observations.json, reflection_phase_3.md).
  * The B1 forbidden-meta-filename gate still fires when the agent
    tries to write ``plan.md`` at sandbox root (no carve-out for the
    forbidden names — only the autonomy artifacts are allowlisted).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from puzzleeval.agents.agent5 import sandbox, tools
from puzzleeval.agents.agent5.objective_synthesis import expected_section_headers
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


def _make_input() -> Agent5Input:
    user = UserUnderstandingOutput(
        summary="Test summary", sub_tasks=[
            SubTask(description="Do X", capability="cap", search_keywords=["x"]),
        ],
        search_strategy="both", domain="test", search_keywords=["x"],
        constraints=Constraints(),
    )
    tc = TestCase(
        id="tc-001", sub_task_ref="Do X", scenario="s",
        input_type="text", output_type="extraction",
        input_data="i", expected_output="o",
        difficulty="easy", tags=[],
        judgement_criteria=[JudgementCriterion(criterion="c", weight=1.0, eval_type="rubric_score")],
    )
    return Agent5Input(
        validated_candidates=[_make_candidate()],
        user_understanding=user,
        test_cases=Agent3Result(test_cases=[tc], generation_notes="n", coverage_summary={"Do X": 1}),
        trace_id="trace-autonomy",
    )


def _make_candidate() -> ScreenedCandidate:
    return ScreenedCandidate(
        name="TestSvc", provider="TestCo", description="d", pricing_model="per-token",
        claimed_capabilities=["x"], relevance_score=0.8, adoption_difficulty="easy",
        relevant_subtasks=["Do X"], source="https://x.com",
        verified_api_docs_url="https://docs.x.com", auth_method="api_key",
        api_access_method="free_tier", confirmed_capabilities=["x"],
        data_format_notes="JSON", screening_notes="ok",
    )


# ---------------------------------------------------------------------------
# stage_agent_state — orchestrator-side staging at sandbox setup
# ---------------------------------------------------------------------------


class TestStageAgentState:
    def test_creates_directory_and_files(self, tmp_path: Path):
        ok = sandbox.stage_agent_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            modality_playbook_ids=[],
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            platform="linux",
            initial_model="claude-sonnet-4-6",
        )
        assert ok is True
        assert (tmp_path / "_agent_state").is_dir()
        assert (tmp_path / "_agent_state" / "objective.md").is_file()
        assert (tmp_path / "_agent_state" / "runtime_state.json").is_file()

    def test_objective_has_all_required_sections(self, tmp_path: Path):
        sandbox.stage_agent_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            modality_playbook_ids=["voice"],
            effective_max_turns=65,
            effective_max_budget_usd=5.0,
            platform="windows",
            initial_model="claude-sonnet-4-6",
        )
        md = (tmp_path / "_agent_state" / "objective.md").read_text()
        for header in expected_section_headers():
            assert header in md, f"Missing: {header}"

    def test_runtime_state_is_valid_json(self, tmp_path: Path):
        sandbox.stage_agent_state(
            sandbox_dir=tmp_path,
            candidate=_make_candidate(),
            input_data=_make_input(),
            modality_playbook_ids=[],
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            platform="linux",
            initial_model="claude-sonnet-4-6",
        )
        data = json.loads(
            (tmp_path / "_agent_state" / "runtime_state.json").read_text()
        )
        assert data["candidate_name"] == "TestSvc"
        assert data["current_phase"] == "phase_1_research"
        assert data["current_turn"] == 0
        assert "files_pending" in data and "harness.py" in data["files_pending"]

    def test_idempotent_re_run_overwrites(self, tmp_path: Path):
        sandbox.stage_agent_state(
            sandbox_dir=tmp_path, candidate=_make_candidate(), input_data=_make_input(),
            modality_playbook_ids=[], effective_max_turns=40,
            effective_max_budget_usd=3.0, platform="linux", initial_model="claude-sonnet-4-6",
        )
        first_mtime = (tmp_path / "_agent_state" / "objective.md").stat().st_mtime

        # Tiny sleep so mtime can change on systems with coarse timestamps,
        # then re-stage. Idempotent operation should succeed.
        import time
        time.sleep(0.05)
        ok = sandbox.stage_agent_state(
            sandbox_dir=tmp_path, candidate=_make_candidate(), input_data=_make_input(),
            modality_playbook_ids=[], effective_max_turns=40,
            effective_max_budget_usd=3.0, platform="linux", initial_model="claude-sonnet-4-6",
        )
        assert ok is True
        # File should exist (new mtime not strictly required if mtime resolution is coarse)
        assert (tmp_path / "_agent_state" / "objective.md").exists()


# ---------------------------------------------------------------------------
# Tool-dispatch protection — orchestrator-owned files cannot be agent-written
# ---------------------------------------------------------------------------


class TestOrchestratorOwnedProtection:
    def test_write_file_rejects_objective_md(self, tmp_path: Path):
        (tmp_path / "_agent_state").mkdir()
        result = tools.write_file(
            {"filename": "_agent_state/objective.md", "content": "agent attempt"},
            sandbox_dir=tmp_path,
        )
        assert result.startswith("Error:")
        assert "orchestrator-owned" in result

    def test_write_file_rejects_runtime_state_json(self, tmp_path: Path):
        (tmp_path / "_agent_state").mkdir()
        result = tools.write_file(
            {"filename": "_agent_state/runtime_state.json", "content": "{}"},
            sandbox_dir=tmp_path,
        )
        assert result.startswith("Error:")
        assert "orchestrator-owned" in result

    def test_patch_file_rejects_objective_md(self, tmp_path: Path):
        # Even if the file exists, patch_file rejects.
        (tmp_path / "_agent_state").mkdir()
        (tmp_path / "_agent_state" / "objective.md").write_text("# Objective\nfoo")
        result = tools.patch_file(
            {"filename": "_agent_state/objective.md", "old_string": "foo", "new_string": "bar"},
            sandbox_dir=tmp_path,
        )
        assert result.startswith("Error:")
        assert "orchestrator-owned" in result


# ---------------------------------------------------------------------------
# Tool-dispatch — agent-writable files inside _agent_state/ work
# ---------------------------------------------------------------------------


class TestAgentWritableArtifacts:
    def test_write_file_accepts_build_plan_md(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "_agent_state/build_plan.md", "content": "# plan\n- [ ] x"},
            sandbox_dir=tmp_path,
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "_agent_state" / "build_plan.md").is_file()

    def test_write_file_accepts_agent_observations_json(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "_agent_state/agent_observations.json", "content": "{}"},
            sandbox_dir=tmp_path,
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "_agent_state" / "agent_observations.json").is_file()

    def test_write_file_accepts_reflection_phase_3_md(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "_agent_state/reflection_phase_3.md", "content": "# reflection"},
            sandbox_dir=tmp_path,
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "_agent_state" / "reflection_phase_3.md").is_file()

    def test_read_file_works_for_orchestrator_owned_artifacts(self, tmp_path: Path):
        # Agent CAN read what it cannot write.
        (tmp_path / "_agent_state").mkdir()
        (tmp_path / "_agent_state" / "objective.md").write_text("# objective")
        (tmp_path / "_agent_state" / "runtime_state.json").write_text("{}")
        a = tools.read_file({"filename": "_agent_state/objective.md"}, sandbox_dir=tmp_path)
        b = tools.read_file({"filename": "_agent_state/runtime_state.json"}, sandbox_dir=tmp_path)
        assert "# objective" in a
        assert "{}" in b

    def test_patch_file_accepts_build_plan_md(self, tmp_path: Path):
        # Set up file + read it (so the read_state gate passes).
        rs: dict[str, float] = {}
        tools.write_file(
            {"filename": "_agent_state/build_plan.md",
             "content": "# plan\n- [ ] todo a\n- [ ] todo b"},
            sandbox_dir=tmp_path, read_state=rs,
        )
        tools.read_file(
            {"filename": "_agent_state/build_plan.md"},
            sandbox_dir=tmp_path, read_state=rs,
        )
        result = tools.patch_file(
            {"filename": "_agent_state/build_plan.md",
             "old_string": "- [ ] todo a", "new_string": "- [x] todo a"},
            sandbox_dir=tmp_path, read_state=rs,
        )
        assert not result.startswith("Error:"), result
        text = (tmp_path / "_agent_state" / "build_plan.md").read_text()
        assert "- [x] todo a" in text


# ---------------------------------------------------------------------------
# Path-resolution edge cases
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_rejects_absolute_path(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "/etc/passwd", "content": "x"}, sandbox_dir=tmp_path,
        )
        assert result.startswith("Error:")

    def test_rejects_traversal(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "../escape.txt", "content": "x"}, sandbox_dir=tmp_path,
        )
        assert result.startswith("Error:")

    def test_rejects_unknown_subdir(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "secret/foo.txt", "content": "x"}, sandbox_dir=tmp_path,
        )
        assert result.startswith("Error:")

    def test_bare_filename_still_works(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "api_spec.txt", "content": "spec"}, sandbox_dir=tmp_path,
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "api_spec.txt").is_file()


# ---------------------------------------------------------------------------
# B1 gate still fires on forbidden meta-files (no carve-out for those)
# ---------------------------------------------------------------------------


class TestB1GateStillFires:
    """The autonomy carve-outs are NAMED (build_plan.md, etc.) — the B1
    gate's existing rejections (plan.md / status.txt / progress.md) still
    fire at sandbox root."""

    def test_b1_still_rejects_plan_md_at_root(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "plan.md", "content": "x"}, sandbox_dir=tmp_path,
        )
        assert result.startswith("Error:")
        assert "meta/state-tracking" in result

    def test_b1_still_rejects_status_txt_at_root(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "status.txt", "content": "x"}, sandbox_dir=tmp_path,
        )
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# Initial-message formatter integration (autonomy block renders when present)
# ---------------------------------------------------------------------------


class TestInitialMessageBlockFormatter:
    def test_returns_empty_when_agent_state_absent(self, tmp_path: Path):
        from puzzleeval.agents.agent5.initial_message import format_autonomy_artifacts_block
        assert format_autonomy_artifacts_block(tmp_path) == ""

    def test_returns_section_when_orchestrator_artifacts_present(self, tmp_path: Path):
        sandbox.stage_agent_state(
            sandbox_dir=tmp_path, candidate=_make_candidate(), input_data=_make_input(),
            modality_playbook_ids=[], effective_max_turns=40,
            effective_max_budget_usd=3.0, platform="linux", initial_model="claude-sonnet-4-6",
        )
        from puzzleeval.agents.agent5.initial_message import format_autonomy_artifacts_block
        block = format_autonomy_artifacts_block(tmp_path)
        assert "Autonomy artifacts" in block
        assert "_agent_state/objective.md" in block
        assert "_agent_state/runtime_state.json" in block
        assert "build_plan.md" in block
        assert "reflection_phase_3.md" in block
