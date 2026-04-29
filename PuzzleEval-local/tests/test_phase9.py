# ============================================================================
# Tests for Phase 9 — Per-scope test execution
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_phase9.py -v
#
# Tests cover:
#   - ScopeTestRun schema round-trip
#   - Agent5Result.scope_runs backward-compat (empty default)
#   - scope_routing.group_tests_by_scope
#   - scope_routing.build_scope_runs
#   - scope_routing.dedup_tools_for_build
#   - Config SCOPE_TEST_MODE flag
# ============================================================================

import json

import pytest

from puzzleeval.schemas import (
    Agent5Result,
    CandidateTestRun,
    ScopeTestRun,
    TestCaseResult,
    WorkflowBlueprint,
    WorkflowStep,
    TestCase,
    JudgementCriterion,
)
from puzzleeval.scope_routing import (
    build_scope_runs,
    dedup_tools_for_build,
    group_tests_by_scope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _blueprint(steps):
    return WorkflowBlueprint(
        steps=[
            WorkflowStep(
                id=s["id"], role=s["role"],
                description=s["desc"], capability=s["cap"],
                input_from="user" if i == 0 else f"step_{i}",
                output_format="structured_json",
                depends_on=[] if i == 0 else [f"step_{i}"],
            )
            for i, s in enumerate(steps)
        ],
    )


def _candidate_run(name, provider="Prov", pass_rate=0.8, sub_task_refs=None):
    """Minimal CandidateTestRun with test_results tagged by sub_task_ref."""
    results = []
    for ref in (sub_task_refs or ["default"]):
        results.append(TestCaseResult(
            test_case_id=f"tc-{name}-{ref}",
            sub_task_ref=ref,
            input_sent={"text": "test"},
            output_received="ok",
            raw_response={"status": "ok"},
            latency_ms=100.0,
            success=True,
            criteria_scores=[],
            weighted_score=pass_rate,
            passed=pass_rate >= 0.5,
        ))
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    return CandidateTestRun(
        candidate_name=name,
        provider=provider,
        harness_dir=f"/tmp/{name}",
        status="completed",
        test_results=results,
        total_tests=total,
        tests_passed=passed,
        tests_failed=total - passed,
        tests_errored=0,
        tests_skipped=0,
        success_rate=1.0,
        pass_rate=pass_rate,
        avg_latency_ms=100.0,
        p95_latency_ms=120.0,
        total_cost_usd=0.01,
        evaluation_cost_usd=0.0,
        execution_duration_ms=500.0,
    )


def _test_case(tc_id, sub_task_ref):
    return TestCase(
        id=tc_id,
        sub_task_ref=sub_task_ref,
        scenario="test scenario",
        input_type="text",
        input_data="test input",
        output_type="structured_json",
        expected_output='{"key": "value"}',
        judgement_criteria=[
            JudgementCriterion(criterion="accuracy", weight=0.5, eval_type="exact_match"),
            JudgementCriterion(criterion="format", weight=0.5, eval_type="format_compliance"),
        ],
        difficulty="easy",
        tags=["happy_path"],
    )


# ---------------------------------------------------------------------------
# ScopeTestRun schema tests
# ---------------------------------------------------------------------------

class TestScopeTestRunSchema:

    def test_round_trip(self):
        scope_run = ScopeTestRun(
            scope_id="step_1",
            scope_role="ocr",
            candidate_results=[
                _candidate_run("Mindee", sub_task_refs=["Extract data"]),
            ],
            test_case_count=5,
        )
        j = scope_run.model_dump_json()
        back = ScopeTestRun.model_validate_json(j)
        assert back.scope_id == "step_1"
        assert back.scope_role == "ocr"
        assert len(back.candidate_results) == 1
        assert back.test_case_count == 5

    def test_agent5_result_scope_runs_default_empty(self):
        result = Agent5Result(
            harnesses=[],
            failed_harnesses=[],
            total_candidates_attempted=0,
            total_build_cost_usd=0.0,
            build_summary="test",
        )
        assert result.scope_runs == []

    def test_agent5_result_with_scope_runs(self):
        result = Agent5Result(
            harnesses=[],
            failed_harnesses=[],
            total_candidates_attempted=0,
            total_build_cost_usd=0.0,
            build_summary="test",
            scope_runs=[
                ScopeTestRun(
                    scope_id="step_1",
                    scope_role="ocr",
                    test_case_count=3,
                ),
                ScopeTestRun(
                    scope_id="step_2",
                    scope_role="sync",
                    test_case_count=2,
                ),
            ],
        )
        assert len(result.scope_runs) == 2
        assert result.scope_runs[0].scope_id == "step_1"
        assert result.scope_runs[1].scope_role == "sync"


# ---------------------------------------------------------------------------
# group_tests_by_scope tests
# ---------------------------------------------------------------------------

class TestGroupTestsByScope:

    def test_no_blueprint_groups_flat(self):
        tcs = [_test_case("tc1", "Extract data"), _test_case("tc2", "Sync data")]
        grouped = group_tests_by_scope(tcs, None)
        assert "_flat" in grouped
        assert len(grouped["_flat"]) == 2

    def test_single_scope_all_tests_assigned(self):
        bp = _blueprint([{"id": "step_1", "role": "ocr", "desc": "Extract data from invoices", "cap": "document OCR"}])
        tcs = [
            _test_case("tc1", "Extract data from invoices"),
            _test_case("tc2", "Extract structured data from invoice photos"),
        ]
        grouped = group_tests_by_scope(tcs, bp)
        assert "step_1" in grouped
        assert len(grouped["step_1"]) == 2

    def test_multi_scope_routes_correctly(self):
        bp = _blueprint([
            {"id": "step_1", "role": "ocr", "desc": "Extract data from invoices", "cap": "document OCR"},
            {"id": "step_2", "role": "sync", "desc": "Sync to QuickBooks accounting", "cap": "accounting integration"},
        ])
        tcs = [
            _test_case("tc1", "Extract data from invoices"),
            _test_case("tc2", "Sync to QuickBooks accounting"),
        ]
        grouped = group_tests_by_scope(tcs, bp)
        assert len(grouped.get("step_1", [])) >= 1
        assert len(grouped.get("step_2", [])) >= 1


# ---------------------------------------------------------------------------
# build_scope_runs tests
# ---------------------------------------------------------------------------

class TestBuildScopeRuns:

    def test_single_scope_wraps_all(self):
        bp = _blueprint([{"id": "step_1", "role": "ocr", "desc": "OCR", "cap": "document OCR"}])
        runs = [_candidate_run("Mindee", pass_rate=0.9), _candidate_run("Veryfi", pass_rate=0.7)]
        scope_runs = build_scope_runs(runs, bp)
        assert len(scope_runs) == 1
        assert scope_runs[0].scope_id == "step_1"
        assert len(scope_runs[0].candidate_results) == 2
        # Sorted by pass_rate descending
        assert scope_runs[0].candidate_results[0].candidate_name == "Mindee"

    def test_no_blueprint_wraps_flat(self):
        runs = [_candidate_run("Mindee")]
        scope_runs = build_scope_runs(runs, None)
        assert len(scope_runs) == 1
        assert scope_runs[0].scope_id == "step_1"  # default

    def test_multi_scope_splits_by_sub_task_ref(self):
        bp = _blueprint([
            {"id": "step_1", "role": "ocr", "desc": "Extract data from invoices", "cap": "document OCR"},
            {"id": "step_2", "role": "sync", "desc": "Sync to QuickBooks accounting", "cap": "accounting integration"},
        ])
        runs = [
            _candidate_run("Mindee", pass_rate=0.9, sub_task_refs=["Extract data from invoices"]),
            _candidate_run("Zapier", pass_rate=0.7, sub_task_refs=[
                "Extract data from invoices",
                "Sync to QuickBooks accounting",
            ]),
        ]
        scope_runs = build_scope_runs(runs, bp)
        # Should have at least 1 scope run
        assert len(scope_runs) >= 1
        # Mindee should appear in OCR scope
        ocr_scope = next((sr for sr in scope_runs if sr.scope_id == "step_1"), None)
        assert ocr_scope is not None
        ocr_names = [cr.candidate_name for cr in ocr_scope.candidate_results]
        assert "Mindee" in ocr_names

    def test_empty_candidate_runs_returns_empty(self):
        bp = _blueprint([{"id": "step_1", "role": "ocr", "desc": "OCR", "cap": "document OCR"}])
        scope_runs = build_scope_runs([], bp)
        assert len(scope_runs) == 0 or scope_runs[0].test_case_count == 0


# ---------------------------------------------------------------------------
# dedup_tools_for_build tests
# ---------------------------------------------------------------------------

class TestDedupToolsForBuild:

    def test_dedup_across_scopes(self):
        selections = {
            "step_1": ["Mindee", "Zapier", "Veryfi"],
            "step_2": ["Zapier", "Make"],       # Zapier appears in both
            "step_3": ["Zapier", "Nanonets"],    # Zapier again
        }
        unique = dedup_tools_for_build(selections)
        assert len(unique) == 5  # Mindee, Zapier, Veryfi, Make, Nanonets
        assert unique.count("Zapier") == 1  # deduplicated

    def test_preserves_first_seen_order(self):
        selections = {
            "step_1": ["B", "A"],
            "step_2": ["C", "A"],  # A already seen
        }
        unique = dedup_tools_for_build(selections)
        assert unique == ["B", "A", "C"]

    def test_empty_selections(self):
        assert dedup_tools_for_build({}) == []

    def test_case_insensitive_dedup(self):
        selections = {
            "step_1": ["Mindee"],
            "step_2": ["mindee"],
        }
        unique = dedup_tools_for_build(selections)
        assert len(unique) == 1


# ---------------------------------------------------------------------------
# Config flag test
# ---------------------------------------------------------------------------

class TestScopeTestModeConfig:

    def test_default_enabled(self):
        from puzzleeval.config import SCOPE_TEST_MODE
        assert SCOPE_TEST_MODE is True
