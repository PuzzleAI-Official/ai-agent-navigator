"""Tests for puzzleeval.report — final EvaluationReport assembler.

Pure unit tests with synthetic Agent 1-5 outputs. Verifies the assembler
tolerates partial inputs, computes ranking deterministically, and emits
useful advisories for empty / mis-shapen runs.
"""

from __future__ import annotations

import pytest

from puzzleeval.report import (
    CandidateReport,
    CoverageReport,
    EvaluationReport,
    TestEvidence,
    assemble_report,
    report_to_dict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_uo(monthly_volume=None, blueprint_steps=None):
    """Synthetic Agent 1 result dict shape (UserUnderstandingOutput)."""
    workflow = None
    if blueprint_steps:
        workflow = type("WF", (), {
            "steps": [type("S", (), {"id": s})() for s in blueprint_steps],
        })()
    constraints = type("C", (), {"monthly_volume": monthly_volume})()
    return type("UO", (), {
        "summary": "OCR construction invoices",
        "domain": "construction",
        "constraints": constraints,
        "workflow": workflow,
    })()


def _make_agent2(candidate_data):
    return {"candidates": candidate_data, "cost_usd": 0.05}


def _make_agent5(runs):
    return {"candidate_runs": runs, "total_build_cost_usd": 0.10, "total_test_cost_usd": 0.20}


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_full_coverage_when_every_step_has_candidate():
    uo = _make_uo(blueprint_steps=["step_1", "step_2"])
    agent2 = _make_agent2([
        {"name": "A", "covers_step_ids": ["step_1", "step_2"]},
    ])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo, agent2_result=agent2,
        agent4_result=None, agent5_result=None,
    )
    assert report.coverage.coverage_percent == 1.0
    assert report.coverage.missing_step_ids == []


def test_partial_coverage_emits_advisory():
    uo = _make_uo(blueprint_steps=["step_1", "step_2", "step_3"])
    agent2 = _make_agent2([
        {"name": "A", "covers_step_ids": ["step_1"]},
    ])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo, agent2_result=agent2,
        agent4_result=None, agent5_result=None,
    )
    assert report.coverage.coverage_percent < 1.0
    assert "step_2" in report.coverage.missing_step_ids
    assert "step_3" in report.coverage.missing_step_ids
    assert any("workflow step(s) had no candidates" in a for a in report.advisories)


def test_empty_blueprint_treated_as_full_coverage():
    """When the user has no blueprint (single-scope legacy flow),
    coverage_percent is 1.0 and there are no missing steps."""
    uo = _make_uo(blueprint_steps=None)
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo, agent2_result=_make_agent2([]),
        agent4_result=None, agent5_result=None,
    )
    assert report.coverage.coverage_percent == 1.0


# ---------------------------------------------------------------------------
# Ranking + per-candidate
# ---------------------------------------------------------------------------


def test_ranking_by_overall_score_desc():
    uo = _make_uo(blueprint_steps=["step_1"])
    agent2 = _make_agent2([
        {"name": "A", "covers_step_ids": ["step_1"]},
        {"name": "B", "covers_step_ids": ["step_1"]},
        {"name": "C", "covers_step_ids": ["step_1"]},
    ])
    agent5 = _make_agent5([
        {"candidate_name": "A", "overall_score": 0.6, "test_results": []},
        {"candidate_name": "B", "overall_score": 0.9, "test_results": []},
        {"candidate_name": "C", "overall_score": 0.75, "test_results": []},
    ])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo, agent2_result=agent2,
        agent4_result=None, agent5_result=agent5,
    )
    names_by_rank = [c.name for c in report.candidate_reports]
    assert names_by_rank == ["B", "C", "A"]
    assert report.overall_winner == "B"


def test_winners_by_scope_picks_best_covering_candidate():
    uo = _make_uo(blueprint_steps=["step_1", "step_2"])
    # A covers step_1 only (high score); B covers both (lower score)
    agent2 = _make_agent2([
        {"name": "A", "covers_step_ids": ["step_1"]},
        {"name": "B", "covers_step_ids": ["step_1", "step_2"]},
    ])
    agent5 = _make_agent5([
        {"candidate_name": "A", "overall_score": 0.9, "test_results": []},
        {"candidate_name": "B", "overall_score": 0.7, "test_results": []},
    ])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo, agent2_result=agent2,
        agent4_result=None, agent5_result=agent5,
    )
    assert report.winners_by_scope["step_1"] == "A"  # higher score, covers
    assert report.winners_by_scope["step_2"] == "B"  # only one that covers


def test_pass_rate_and_evidence_extracted():
    uo = _make_uo(blueprint_steps=["step_1"])
    agent5 = _make_agent5([{
        "candidate_name": "A",
        "overall_score": 0.7,
        "test_results": [
            {"test_case_id": "t1", "scenario": "happy", "success": True, "weighted_score": 1.0},
            {"test_case_id": "t2", "scenario": "edge", "success": True, "weighted_score": 0.8},
            {"test_case_id": "t3", "scenario": "fail", "success": False, "weighted_score": 0.2, "reasoning": "wrong field"},
        ],
    }])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo, agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None, agent5_result=agent5,
    )
    cr = report.candidate_reports[0]
    assert cr.passed_count == 2
    assert cr.total_count == 3
    assert cr.pass_rate == pytest.approx(2/3, abs=1e-3)
    assert any(ev.test_case_id == "t3" for ev in cr.failure_evidence)
    assert any(ev.test_case_id == "t1" for ev in cr.success_evidence)


def test_pass_rate_uses_passed_not_success():
    """API success alone is not a passing test; report must honor passed=False."""
    uo = _make_uo(blueprint_steps=["step_1"])
    agent5 = _make_agent5([{
        "candidate_name": "A",
        "overall_score": 0.5,
        "test_results": [
            {
                "test_case_id": "t1",
                "scenario": "API returned but quality failed",
                "success": True,
                "passed": False,
                "weighted_score": 0.2,
            },
        ],
    }])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo,
        agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None,
        agent5_result=agent5,
    )
    cr = report.candidate_reports[0]
    assert cr.passed_count == 0
    assert cr.pass_rate == 0.0
    assert cr.failure_evidence[0].test_case_id == "t1"


def test_merged_audio_path_is_mirrored_into_audio_paths():
    uo = _make_uo(blueprint_steps=["step_1"])
    merged = r"C:\runs\x\harnesses\a\voice\conversation_abc.mp3"
    agent5 = _make_agent5([{
        "candidate_name": "A",
        "overall_score": 1.0,
        "test_results": [
            {
                "test_case_id": "voice-1",
                "scenario": "full call",
                "success": True,
                "passed": True,
                "weighted_score": 1.0,
                "merged_audio_path": merged,
                "audio_paths": [
                    {"role": "caller", "path": r"C:\runs\x\caller_abc-t0.mp3"},
                ],
            },
        ],
    }])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo,
        agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None,
        agent5_result=agent5,
    )
    ev = report.candidate_reports[0].success_evidence[0]
    assert ev.merged_audio_path == merged
    assert ev.audio_paths[0]["role"] == "conversation"
    assert ev.audio_paths[0]["path"] == merged


# ---------------------------------------------------------------------------
# Cost projection
# ---------------------------------------------------------------------------


def test_no_monthly_volume_means_no_projection():
    uo = _make_uo(monthly_volume=None, blueprint_steps=["step_1"])
    agent5 = _make_agent5([{
        "candidate_name": "A", "overall_score": 0.9, "test_results": [],
    }])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo, agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None, agent5_result=agent5,
    )
    assert report.candidate_reports[0].monthly_cost_projection_usd is None


# ---------------------------------------------------------------------------
# Empty / partial input tolerance
# ---------------------------------------------------------------------------


def test_no_agent5_result_emits_advisory():
    uo = _make_uo(blueprint_steps=["step_1"])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo,
        agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None, agent5_result=None,
    )
    assert report.candidate_count == 0
    assert any("No harnesses successfully ran" in a for a in report.advisories)
    assert report.overall_winner is None


def test_empty_agent2_returns_empty_report_no_crash():
    uo = _make_uo(blueprint_steps=["step_1"])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo,
        agent2_result=_make_agent2([]),
        agent4_result=None, agent5_result=None,
    )
    assert report.candidate_count == 0
    assert "step_1" in report.coverage.missing_step_ids


# ---------------------------------------------------------------------------
# Pros/cons heuristics
# ---------------------------------------------------------------------------


def test_high_score_produces_top_tier_pro():
    uo = _make_uo(blueprint_steps=["step_1"])
    agent5 = _make_agent5([{
        "candidate_name": "A", "overall_score": 0.95, "auth_method": "no_auth",
        "test_results": [{"success": True, "weighted_score": 1.0} for _ in range(10)],
    }])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo,
        agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None, agent5_result=agent5,
    )
    cr = report.candidate_reports[0]
    assert any("Top-tier" in p for p in cr.pros)
    assert any("100%" in p for p in cr.pros)
    assert any("No authentication" in p for p in cr.pros)


def test_low_score_produces_cons():
    uo = _make_uo(blueprint_steps=["step_1"])
    agent5 = _make_agent5([{
        "candidate_name": "A", "overall_score": 0.3, "auth_method": "oauth2",
        "test_results": [
            {"success": False, "weighted_score": 0.1} for _ in range(10)
        ],
    }])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo,
        agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None, agent5_result=agent5,
    )
    cr = report.candidate_reports[0]
    assert any("Below-half" in c for c in cr.cons)
    assert any("OAuth2" in c for c in cr.cons)


def test_sandbox_disclosure_in_cons():
    uo = _make_uo(blueprint_steps=["step_1"])
    agent5 = _make_agent5([{
        "candidate_name": "A", "overall_score": 0.85,
        "tested_in_sandbox": True, "test_results": [],
    }])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo,
        agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None, agent5_result=agent5,
    )
    assert any("sandbox" in c.lower() for c in report.candidate_reports[0].cons)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_report_to_dict_is_json_serializable():
    import json
    uo = _make_uo(blueprint_steps=["step_1"])
    agent5 = _make_agent5([{
        "candidate_name": "A", "overall_score": 0.9, "test_results": [],
    }])
    report = assemble_report(
        run_id="r1", trace_id="t1",
        agent1_result=uo,
        agent2_result=_make_agent2([{"name": "A", "covers_step_ids": ["step_1"]}]),
        agent4_result=None, agent5_result=agent5,
    )
    s = json.dumps(report_to_dict(report))
    parsed = json.loads(s)
    assert parsed["overall_winner"] == "A"
    assert parsed["coverage"]["coverage_percent"] == 1.0
