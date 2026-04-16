# ============================================================================
# Tests for Pipeline Run Manager
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_pipeline.py -v
#
# Tests that PipelineRun correctly saves intermediate outputs, tracks
# validation results, and produces accurate pipeline summaries.
# Uses a temp directory to avoid polluting the project with test artifacts.
# ============================================================================

import json
import tempfile
from pathlib import Path

import pytest

from puzzleeval.pipeline import PipelineRun
from puzzleeval.schemas import (
    Agent1Input,
    Agent1Result,
    Constraints,
    SubTask,
    UserUnderstandingOutput,
)
from puzzleeval.validators import ValidationResult


# ============================================================================
# Fixtures
# ============================================================================

def _make_agent1_input() -> Agent1Input:
    return Agent1Input(
        user_text="I need AI for invoices",
        trace_id="test-pipeline-001",
    )


def _make_agent1_result() -> Agent1Result:
    return Agent1Result(
        is_clear=True,
        result=UserUnderstandingOutput(
            summary="User needs invoice processing AI",
            sub_tasks=[
                SubTask(
                    description="Extract data from invoices",
                    capability="document OCR",
                    search_keywords=["invoice OCR API"],
                ),
            ],
            search_strategy="both",
            domain="accounting",
            search_keywords=["invoice AI"],
            constraints=Constraints(),
            workflow_summary=None,
        ),
    )


# ============================================================================
# Tests
# ============================================================================

class TestPipelineRun:

    def test_creates_run_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-001", output_dir=tmpdir)
            assert (Path(tmpdir) / "test-trace-001").is_dir()

    def test_saves_agent_input_and_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-002", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1",
                _make_agent1_input(),
                _make_agent1_result(),
                duration_ms=3200,
                cost_usd=0.05,
            )

            run_dir = Path(tmpdir) / "test-trace-002"
            assert (run_dir / "agent_1_input.json").exists()
            assert (run_dir / "agent_1_output.json").exists()

            # Verify input is valid JSON
            input_data = json.loads((run_dir / "agent_1_input.json").read_text())
            assert input_data["user_text"] == "I need AI for invoices"

            # Verify output is valid JSON
            output_data = json.loads((run_dir / "agent_1_output.json").read_text())
            assert output_data["is_clear"] is True

    def test_saves_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-003", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=3200,
            )

            validation = ValidationResult(
                passed=True, errors=[], warnings=["Domain is vague"],
            )
            run.save_validation("agent_1", validation)

            run_dir = Path(tmpdir) / "test-trace-003"
            assert (run_dir / "agent_1_validation.json").exists()

            v_data = json.loads((run_dir / "agent_1_validation.json").read_text())
            assert v_data["passed"] is True
            assert "Domain is vague" in v_data["warnings"]

    def test_validation_failure_updates_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-004", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=3200,
            )

            validation = ValidationResult(
                passed=False, errors=["No sub-tasks"], warnings=[],
            )
            run.save_validation("agent_1", validation)

            assert run.agents[0].status == "validation_failed"

    def test_finalize_creates_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-005", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=3200, cost_usd=0.05,
            )
            run.save_validation("agent_1", ValidationResult(passed=True))

            summary = run.finalize()

            run_dir = Path(tmpdir) / "test-trace-005"
            assert (run_dir / "pipeline_summary.json").exists()

            assert summary["trace_id"] == "test-trace-005"
            assert summary["status"] == "completed"
            assert summary["total_cost_usd"] == 0.05
            assert len(summary["agents"]) == 1
            assert summary["agents"][0]["name"] == "agent_1"

    def test_completed_with_warnings_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-006", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=3200, cost_usd=0.05,
            )
            run.save_validation("agent_1", ValidationResult(
                passed=True, warnings=["Domain is vague"],
            ))

            summary = run.finalize()
            assert summary["status"] == "completed_with_warnings"

    def test_failed_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-007", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=3200, cost_usd=0.05,
            )
            run.fail("agent_2", "Rate limit exceeded")

            summary = run.finalize()
            assert summary["status"] == "failed"
            assert summary["failed_at"] == "agent_2"
            assert len(summary["agents"]) == 2
            assert summary["agents"][1]["error"] == "Rate limit exceeded"

    def test_validation_failed_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-008", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=3200,
            )
            run.save_validation("agent_1", ValidationResult(
                passed=False, errors=["No sub-tasks"],
            ))

            summary = run.finalize()
            assert summary["status"] == "validation_failed"

    def test_total_cost_sums_agents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-trace-009", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=3200, cost_usd=0.05,
            )
            run.save_agent_result(
                "agent_2", _make_agent1_input(), _make_agent1_result(),
                duration_ms=12000, cost_usd=0.35,
            )

            summary = run.finalize()
            assert summary["total_cost_usd"] == 0.4

    def test_multiple_runs_dont_interfere(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run1 = PipelineRun("run-A", output_dir=tmpdir)
            run2 = PipelineRun("run-B", output_dir=tmpdir)

            run1.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=1000,
            )
            run2.save_agent_result(
                "agent_1", _make_agent1_input(), _make_agent1_result(),
                duration_ms=2000,
            )

            assert (Path(tmpdir) / "run-A" / "agent_1_output.json").exists()
            assert (Path(tmpdir) / "run-B" / "agent_1_output.json").exists()


# ============================================================================
# Phase 4: Derived metadata fingerprints
# ============================================================================
# Verifies that save_agent_result auto-promotes the Phase 4 coverage
# extractor output into AgentRecord.metadata, and that finalize() aggregates
# the derived fields into run-level metadata for pipeline_summary.json.
# ============================================================================

class TestPhase4DerivedMetadata:
    def _make_agent2_with_coverage(self, scopes_covered=None):
        from puzzleeval.schemas import Agent2Result, Agent2Input, Candidate

        scopes_covered = scopes_covered or {"step_1", "step_2"}
        candidates = [
            Candidate(
                name=f"Svc{i}", provider=f"Prov{i}",
                description="desc", api_available=True, api_docs_url=None,
                pricing_model="usage-based", pricing_details=None,
                claimed_capabilities=["c"], relevance_score=0.8,
                adoption_difficulty="easy", relevant_subtasks=[], source="t",
                covers_step_ids=frozenset(scopes_covered),
                coverage_confidence={s: "claimed" for s in scopes_covered},
            )
            for i in range(3)
        ]
        result = Agent2Result(
            candidates=candidates,
            search_approach="test",
            coverage_notes="test",
        )
        input_data = Agent2Input(
            user_understanding=_make_agent1_result().result,
            trace_id="test-phase4",
        )
        return input_data, result

    def test_coverage_metadata_auto_promoted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("phase4-a", output_dir=tmpdir)
            input_data, result = self._make_agent2_with_coverage()
            run.save_agent_result("agent_2", input_data, result, duration_ms=100)
            assert run.agents[0].metadata["phase4_dual_search_active"] is True
            assert run.agents[0].metadata["phase4_scopes_covered_count"] == 2
            assert run.agents[0].metadata["phase4_coverage_populated_all"] is True

    def test_coverage_metadata_absent_for_legacy_flow(self):
        # Candidates with empty covers_step_ids → no Phase 4 fingerprint.
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("phase4-b", output_dir=tmpdir)
            input_data, result = self._make_agent2_with_coverage()
            # Strip coverage to simulate legacy flow
            for c in result.candidates:
                c.covers_step_ids = frozenset()
                c.coverage_confidence = {}
            run.save_agent_result("agent_2", input_data, result, duration_ms=100)
            assert "phase4_dual_search_active" not in run.agents[0].metadata

    def test_finalize_rolls_coverage_metadata_to_run_level(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("phase4-c", output_dir=tmpdir)
            input_data, result = self._make_agent2_with_coverage(
                {"step_1", "step_2", "step_3"}
            )
            run.save_agent_result("agent_2", input_data, result, duration_ms=100)
            summary = run.finalize()
            assert summary["metadata"]["phase4_dual_search_active"] is True
            assert summary["metadata"]["phase4_scopes_covered_count"] == 3
            assert summary["metadata"]["phase4_coverage_populated_all"] is True

    def test_mixed_populated_yields_false_all_flag(self):
        # One candidate without coverage, two with → all_populated=False.
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("phase4-d", output_dir=tmpdir)
            input_data, result = self._make_agent2_with_coverage()
            result.candidates[0].covers_step_ids = frozenset()
            result.candidates[0].coverage_confidence = {}
            run.save_agent_result("agent_2", input_data, result, duration_ms=100)
            # At least one has coverage → phase4 block activates
            assert run.agents[0].metadata["phase4_dual_search_active"] is True
            # But not all → populated_all=False
            assert run.agents[0].metadata["phase4_coverage_populated_all"] is False
