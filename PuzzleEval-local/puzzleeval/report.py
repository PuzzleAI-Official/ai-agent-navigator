"""Final evaluation report assembler.

After Agent 5 finishes, the user expects a structured report — for each
tested candidate: pass/fail per test, criterion scores, ranking,
qualitative reasoning, monthly cost projection, adoption guidance,
known failure modes, sandbox/DRY_RUN disclosure. Before this module
that "report" was just the raw ``agent_5_output.json`` artifact and a
templated client-side recommendation sentence.

This module produces a structured ``EvaluationReport`` from the four
Agent outputs (1, 2, 4, 5) plus the run's ``Constraints`` (so monthly
cost projections use the user's actual stated volume). The same
function is invoked by:

  * The pipeline runner at ``pipeline_completed`` time so the report is
    persisted to ``runs/<trace_id>/evaluation_report.json``.
  * The ``GET /runs/{run_id}/report`` endpoint so the frontend (or a
    user CLI) can fetch it on demand.
  * A future CLI ``puzzleeval report <run_id>`` for printable output.

The report is pure data — no Claude calls, no network. It assembles
existing per-test results into a comparison view, computes deterministic
rankings, and projects monthly cost via the existing
``puzzleeval.pricing`` module.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report data classes
# ---------------------------------------------------------------------------


@dataclass
class TestEvidence:
    """One test result quoted in the report — used as evidence for claims."""
    test_case_id: str
    scenario: str
    passed: bool
    score: float
    reasoning_excerpt: str = ""
    # Playable audio artifacts captured for this test case (voice_realtime
    # caller + agent WAVs, multi-turn conversation segments, etc.). Each
    # entry: ``{role: 'caller' | 'agent' | ..., path: '<abs path>'}``.
    # Empty list for non-voice tests. Frontend renders these as in-browser
    # playback controls in the evidence panel.
    audio_paths: list[dict] = field(default_factory=list)
    # Rubric verdict from the agentic conversational eval path. When
    # present, the frontend renders an expandable "Rubric breakdown" card
    # showing per-criterion scores + critical failures + summary. Null
    # for non-conversational tests and for scripted-path conversations.
    # Shape: dict mirroring puzzleeval.schemas.RubricVerdict.model_dump().
    rubric_verdict: dict | None = None
    # Per-turn transcript for conversational tests (agentic mode).
    # Each entry: ``{turn_index, role: 'user'|'agent', text, meta}``.
    # Empty for non-conversational and legacy scripted-path tests.
    transcript: list[dict] = field(default_factory=list)


@dataclass
class CandidateReport:
    """Per-candidate summary with evidence + cost projection."""
    name: str
    provider: str
    rank: int  # 1 = best; sorted by overall_score desc, ties broken by cost
    overall_score: float  # 0.0–1.0
    pass_rate: float
    passed_count: int
    total_count: int
    avg_latency_ms: float | None
    cost_usd_per_call: float | None
    monthly_cost_projection_usd: float | None
    auth_method: str
    requirements: list[str]
    auth_env_vars: list[str]
    sandbox_used: bool
    failure_evidence: list[TestEvidence] = field(default_factory=list)
    success_evidence: list[TestEvidence] = field(default_factory=list)
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    """How the workflow blueprint mapped to tested candidates."""
    blueprint_step_ids: list[str]
    covered_step_ids: list[str]
    missing_step_ids: list[str]
    coverage_percent: float


@dataclass
class EvaluationReport:
    """The user-facing final report. JSON-serializable via asdict()."""
    run_id: str
    trace_id: str
    user_summary: str
    domain: str
    monthly_volume: int | None
    total_cost_usd: float
    candidate_count: int
    test_count: int
    coverage: CoverageReport
    winners_by_scope: dict[str, str]  # scope_id → best candidate name
    overall_winner: str | None
    candidate_reports: list[CandidateReport]
    advisories: list[str] = field(default_factory=list)
    # Phase 9 per-scope breakdown from Agent 5. When populated, the
    # frontend's ResultsComparison renders one section per scope instead
    # of the legacy flat card layout. Serialized as list[dict] — the
    # ScopeTestRun Pydantic model's dump shape. Empty when Agent 5 ran
    # without multi-scope routing (single-scope workflows).
    scope_runs: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _safe_get(obj: Any, key: str, default: Any = None) -> Any:
    """Get key from dict OR attribute from object — both UO + dump_dict shapes."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _project_monthly_cost(
    pricing_breakdown: Any, monthly_volume: int | None,
) -> float | None:
    """Best-effort monthly cost projection via puzzleeval.pricing.

    Returns None when we don't have enough information (no breakdown OR
    no monthly_volume). Never raises — pricing helpers are best-effort.
    """
    if monthly_volume is None or monthly_volume <= 0 or pricing_breakdown is None:
        return None
    try:
        from puzzleeval.pricing import estimate_monthly_cost
        return float(estimate_monthly_cost(pricing_breakdown, monthly_volume))
    except Exception as exc:  # noqa: BLE001
        logger.debug("monthly cost projection failed: %s", exc)
        return None


def _extract_test_evidence(
    test_results: list[dict], *, max_items: int | None = None,
) -> tuple[list[TestEvidence], list[TestEvidence]]:
    """Pull passes + failures from a candidate's test runs.

    Returns ``(failures, successes)`` truncated to ``max_items`` each.
    Failures are sorted lowest-score-first (most diagnostic) and
    successes highest-first.

    When ``max_items`` is None (the normal call path from
    ``assemble_report``), the cap comes from
    ``REPORT_MAX_EVIDENCE_PER_KIND`` in config — default 50. That's
    high enough that almost every real run shows ALL test results
    (the frontend expands them inside <details> accordions), while
    still preventing pathological runs from producing multi-MB
    payloads. Pass an explicit integer to override per-call (used by
    some tests to lock the old "3 representative items" contract for
    legacy-behavior regression checks).
    """
    if max_items is None:
        # Deferred import — avoids circular-import risk if config
        # later grows a dependency on report.py.
        from puzzleeval.config import REPORT_MAX_EVIDENCE_PER_KIND
        max_items = REPORT_MAX_EVIDENCE_PER_KIND
    passed = []
    failed = []
    for tr in test_results:
        score = float(tr.get("weighted_score") or tr.get("score") or 0.0)
        # Normalize audio_paths: accept list[dict] with {role, path} keys.
        # Silently drop any malformed entries — evidence is best-effort and
        # a broken entry shouldn't break the whole report.
        raw_audio = tr.get("audio_paths") or []
        audio_paths: list[dict] = []
        if isinstance(raw_audio, list):
            for a in raw_audio:
                if isinstance(a, dict) and "path" in a:
                    audio_paths.append({
                        "role": str(a.get("role") or "unknown"),
                        "path": str(a["path"]),
                    })
        # Normalize rubric_verdict + transcript (agentic conversational
        # path). Both are optional — dict shapes come straight from
        # TestCaseResult.rubric_verdict.model_dump() and .transcript
        # entries (ConversationTurn.model_dump()). Invalid shapes are
        # silently dropped; evidence is best-effort.
        raw_rubric = tr.get("rubric_verdict")
        rubric_dict: dict | None = raw_rubric if isinstance(raw_rubric, dict) else None
        raw_transcript = tr.get("transcript") or []
        transcript_list: list[dict] = []
        if isinstance(raw_transcript, list):
            for t in raw_transcript:
                if isinstance(t, dict) and "role" in t and "text" in t:
                    transcript_list.append({
                        "turn_index": int(t.get("turn_index", 0) or 0),
                        "role": str(t.get("role", "unknown")),
                        "text": str(t.get("text") or "")[:2000],
                        "meta": t.get("meta") or {},
                    })
        ev = TestEvidence(
            test_case_id=str(tr.get("test_case_id") or tr.get("id") or ""),
            scenario=str(tr.get("scenario") or "")[:120],
            passed=bool(tr.get("success") or tr.get("passed", False)),
            score=score,
            reasoning_excerpt=str(
                tr.get("reasoning") or tr.get("error") or ""
            )[:240],
            audio_paths=audio_paths,
            rubric_verdict=rubric_dict,
            transcript=transcript_list,
        )
        (passed if ev.passed else failed).append(ev)
    failed.sort(key=lambda e: e.score)
    passed.sort(key=lambda e: -e.score)
    return failed[:max_items], passed[:max_items]


def _derive_pros_cons(
    *, pass_rate: float, monthly_cost: float | None,
    auth_method: str, sandbox_used: bool, score: float,
) -> tuple[list[str], list[str]]:
    """Deterministic pros/cons strings from score + metadata. No LLM."""
    pros: list[str] = []
    cons: list[str] = []
    if score >= 0.9:
        pros.append(f"Top-tier overall score ({score:.0%}) on the test battery")
    elif score >= 0.75:
        pros.append(f"Solid overall score ({score:.0%}) on the test battery")
    elif score < 0.5:
        cons.append(f"Below-half overall score ({score:.0%}) — major gaps")
    if pass_rate >= 0.9:
        pros.append(f"Passed {pass_rate:.0%} of test cases")
    elif pass_rate < 0.6:
        cons.append(f"Only passed {pass_rate:.0%} of test cases")
    if monthly_cost is not None and monthly_cost < 50:
        pros.append(f"Projected monthly cost ${monthly_cost:.2f}")
    elif monthly_cost is not None and monthly_cost > 500:
        cons.append(f"Projected monthly cost ${monthly_cost:.2f}")
    if auth_method == "no_auth":
        pros.append("No authentication required — easiest adoption")
    elif auth_method == "oauth2":
        cons.append("OAuth2 setup required — non-trivial integration effort")
    if sandbox_used:
        cons.append(
            "Tested in sandbox / DRY_RUN mode — production behavior may differ"
        )
    return pros, cons


# ---------------------------------------------------------------------------
# Main assembler
# ---------------------------------------------------------------------------


def assemble_report(
    *,
    run_id: str,
    trace_id: str,
    agent1_result: Any,  # dict or UserUnderstandingOutput
    agent2_result: Any | None,  # dict-shaped Agent2Result
    agent4_result: Any | None,  # dict-shaped Agent4Result
    agent5_result: Any | None,  # dict-shaped Agent5Result
    total_cost_usd: float = 0.0,
) -> EvaluationReport:
    """Assemble the final report. Tolerates partial inputs.

    When an upstream agent returned None or empty (e.g. coverage_gap fired
    after Agent 2), the report still assembles with informative
    advisories rather than crashing.
    """
    advisories: list[str] = []

    # Pull user context from Agent 1
    uu = (
        _safe_get(agent1_result, "result")
        if isinstance(agent1_result, dict) and "result" in agent1_result
        else agent1_result
    )
    user_summary = str(_safe_get(uu, "summary", "") or "")
    domain = str(_safe_get(uu, "domain", "") or "unknown")
    constraints = _safe_get(uu, "constraints", None)
    monthly_volume = _safe_get(constraints, "monthly_volume", None)

    workflow = _safe_get(uu, "workflow", None)
    blueprint_steps = []
    if workflow:
        steps = _safe_get(workflow, "steps", []) or []
        blueprint_steps = [str(_safe_get(s, "id", "")) for s in steps]

    # Compute coverage from Agent 2
    candidates = (
        _safe_get(agent2_result, "candidates", []) if agent2_result else []
    ) or []
    covered_scopes: set[str] = set()
    for c in candidates:
        for sid in (_safe_get(c, "covers_step_ids", []) or []):
            covered_scopes.add(str(sid))
    missing_scopes = [s for s in blueprint_steps if s not in covered_scopes]
    coverage_percent = (
        len([s for s in blueprint_steps if s in covered_scopes])
        / len(blueprint_steps)
        if blueprint_steps else 1.0
    )
    coverage = CoverageReport(
        blueprint_step_ids=list(blueprint_steps),
        covered_step_ids=sorted(covered_scopes),
        missing_step_ids=missing_scopes,
        coverage_percent=round(coverage_percent, 4),
    )
    if missing_scopes:
        advisories.append(
            f"{len(missing_scopes)} workflow step(s) had no candidates: "
            f"{', '.join(missing_scopes)}. Consider broader research keywords "
            f"or adding specific providers manually."
        )

    # Per-candidate reports from Agent 5
    candidate_reports: list[CandidateReport] = []
    candidate_runs = (
        _safe_get(agent5_result, "candidate_runs", [])
        if agent5_result else []
    ) or []

    # Build a quick lookup of pricing_breakdown from Agent 4 (verified) and
    # Agent 2 (claimed) for monthly cost projection.
    pricing_by_name: dict[str, Any] = {}
    if agent4_result:
        for sc in (_safe_get(agent4_result, "validated_candidates", []) or []):
            pricing_by_name[str(_safe_get(sc, "candidate_name", "")).lower()] = (
                _safe_get(sc, "pricing_breakdown", None)
            )
    for c in candidates:
        nm = str(_safe_get(c, "name", "")).lower()
        if nm and nm not in pricing_by_name:
            pricing_by_name[nm] = _safe_get(c, "pricing_breakdown", None)

    # Sort candidate runs by score (desc) for ranking
    def _score_key(run: dict) -> float:
        return float(_safe_get(run, "overall_score", None) or 0.0)
    sorted_runs = sorted(candidate_runs, key=_score_key, reverse=True)

    for rank_idx, cr in enumerate(sorted_runs):
        name = str(_safe_get(cr, "candidate_name", "") or "")
        provider = str(_safe_get(cr, "provider", "") or "")
        test_results = _safe_get(cr, "test_results", []) or []
        passed_count = sum(
            1 for tr in test_results
            if bool(_safe_get(tr, "success", False) or _safe_get(tr, "passed", False))
        )
        total_count = len(test_results)
        pass_rate = (passed_count / total_count) if total_count else 0.0
        overall_score = float(_safe_get(cr, "overall_score", None) or 0.0)
        avg_latency = _safe_get(cr, "avg_latency_ms", None)
        cost_per_call = _safe_get(cr, "cost_usd", None)
        if cost_per_call is not None and total_count:
            cost_per_call = float(cost_per_call) / total_count
        sandbox_used = bool(
            _safe_get(cr, "tested_in_sandbox", False)
            or _safe_get(cr, "sandbox_used", False)
        )
        pricing = pricing_by_name.get(name.lower())
        monthly_cost = _project_monthly_cost(pricing, monthly_volume)

        failures, successes = _extract_test_evidence(test_results)
        pros, cons = _derive_pros_cons(
            pass_rate=pass_rate, monthly_cost=monthly_cost,
            auth_method=str(_safe_get(cr, "auth_method", "") or "unknown"),
            sandbox_used=sandbox_used, score=overall_score,
        )

        candidate_reports.append(CandidateReport(
            name=name, provider=provider, rank=rank_idx + 1,
            overall_score=round(overall_score, 4),
            pass_rate=round(pass_rate, 4),
            passed_count=passed_count, total_count=total_count,
            avg_latency_ms=(
                round(float(avg_latency), 2) if avg_latency is not None else None
            ),
            cost_usd_per_call=(
                round(float(cost_per_call), 6) if cost_per_call is not None else None
            ),
            monthly_cost_projection_usd=(
                round(monthly_cost, 2) if monthly_cost is not None else None
            ),
            auth_method=str(_safe_get(cr, "auth_method", "") or "unknown"),
            requirements=list(_safe_get(cr, "requirements", []) or []),
            auth_env_vars=list(_safe_get(cr, "auth_env_vars", []) or []),
            sandbox_used=sandbox_used,
            failure_evidence=failures,
            success_evidence=successes,
            pros=pros, cons=cons,
            coverage_gaps=[],
        ))

    # Per-scope winners: best (highest overall_score) candidate that
    # actually covers each scope.
    winners_by_scope: dict[str, str] = {}
    for sid in blueprint_steps:
        best_name: str | None = None
        best_score = -1.0
        for rep in candidate_reports:
            cand = next(
                (c for c in candidates if str(_safe_get(c, "name", "")).lower() == rep.name.lower()),
                None,
            )
            if cand and sid in (_safe_get(cand, "covers_step_ids", []) or []):
                if rep.overall_score > best_score:
                    best_score = rep.overall_score
                    best_name = rep.name
        if best_name:
            winners_by_scope[sid] = best_name

    overall_winner = candidate_reports[0].name if candidate_reports else None
    if not candidate_reports:
        advisories.append(
            "No harnesses successfully ran — every selected candidate failed "
            "to build, validate, or run. Check pipeline failure events for "
            "the specific blocker per candidate."
        )

    return EvaluationReport(
        run_id=run_id, trace_id=trace_id,
        user_summary=user_summary, domain=domain,
        monthly_volume=monthly_volume,
        total_cost_usd=round(float(total_cost_usd), 6),
        candidate_count=len(candidate_reports),
        test_count=sum(c.total_count for c in candidate_reports),
        coverage=coverage,
        winners_by_scope=winners_by_scope,
        overall_winner=overall_winner,
        candidate_reports=candidate_reports,
        advisories=advisories,
        scope_runs=_extract_scope_runs(agent5_result),
    )


def _extract_scope_runs(agent5_result: Any | None) -> list[dict]:
    """Pull Agent 5's ``scope_runs`` (Phase 9) into a JSON-ready list.

    Agent5Result's ``scope_runs`` field is a list of ScopeTestRun
    Pydantic models — each has ``scope_id``, ``scope_role``,
    ``candidate_results`` (list[CandidateTestRun]), ``test_case_count``,
    ``evaluation_mode``. Either Pydantic or dict shape is possible
    depending on serialization path; handle both.
    """
    if not agent5_result:
        return []
    raw = _safe_get(agent5_result, "scope_runs", None)
    if not raw:
        return []
    out: list[dict] = []
    for entry in raw:
        if hasattr(entry, "model_dump"):
            out.append(entry.model_dump(mode="json"))
        elif isinstance(entry, dict):
            out.append(entry)
    return out


def report_to_dict(report: EvaluationReport) -> dict:
    """Convert to JSON-friendly dict (handles nested dataclasses)."""
    return asdict(report)


__all__ = [
    "CandidateReport",
    "CoverageReport",
    "EvaluationReport",
    "TestEvidence",
    "assemble_report",
    "report_to_dict",
]
