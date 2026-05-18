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
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
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
    # Stable pointer to the merged full-conversation recording when present.
    # The same path is also included in audio_paths with role="conversation"
    # so older frontend code keeps working.
    merged_audio_path: str | None = None
    # Rubric verdict from the agentic conversational eval path. When
    # present, the frontend renders an expandable "Rubric breakdown" card
    # showing per-criterion scores + critical failures + summary. Null
    # for non-conversational tests and for scripted-path conversations.
    # Shape: dict mirroring puzzleeval.schemas.RubricVerdict.model_dump().
    rubric_verdict: dict | None = None
    judge_failed: bool = False
    judge_failure_reason: str | None = None
    # Per-turn transcript for conversational tests (agentic mode).
    # Each entry: ``{turn_index, role: 'user'|'agent', text, meta}``.
    # Empty for non-conversational and legacy scripted-path tests.
    transcript: list[dict] = field(default_factory=list)


@dataclass
class CandidateReport:
    """Per-candidate summary with evidence + cost projection.

    For candidates whose harness FAILED to build (adversarial probes
    flagged it NOT READY, or the build loop emitted FailedHarness),
    the report still surfaces an entry with ``build_succeeded=False``,
    ``critical_failures`` listing what blocked the harness, and
    ``forensics_tail`` showing the last events the harness emitted
    before failure. This prevents the silent-absence failure mode where
    a user-named candidate (e.g., ElevenLabs in real-run trace
    6e0c9563) gets built but excluded from the report entirely.
    """
    name: str
    provider: str
    rank: int  # 1 = best; sorted by pass_rate, then score, then cost/latency
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
    test_evidence: list[TestEvidence] = field(default_factory=list)
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    # Build-status disclosure (Issue #6 fix from real-run trace 6e0c9563):
    # When `build_succeeded=False`, the candidate's harness failed
    # adversarial validation OR the build loop emitted FailedHarness.
    # The frontend surfaces these in a dedicated "Builds attempted but
    # failed validation" section so the user understands WHY a named
    # candidate doesn't have test results.
    build_succeeded: bool = True
    # Phase 6: a candidate can be truthfully abandoned when provider/account
    # state or API incompatibility means more build-loop turns would only
    # burn budget. This is distinct from gate rejection or harness error.
    abandoned: bool = False
    abandon_reason: str | None = None
    # When build_succeeded=False, lists the adversarial-probe critical
    # failures (e.g., "empty_input: timeout: ...", "concurrency:
    # outcomes=['crash','crash','crash']"). Empty for successful builds.
    critical_failures: list[str] = field(default_factory=list)
    # Last 50 events from harness_forensics.jsonl (post-mortem evidence).
    # Each entry is a JSONL-decoded dict matching the canonical event
    # taxonomy. Empty when the forensics file is missing OR the build
    # succeeded (the operator already has the per-test transcripts).
    forensics_tail: list[dict] = field(default_factory=list)


@dataclass
class CoverageReport:
    """How the workflow blueprint mapped to tested candidates."""
    blueprint_step_ids: list[str]
    covered_step_ids: list[str]
    missing_step_ids: list[str]
    coverage_percent: float


@dataclass
class EfficiencySummary:
    """User-facing explanation of where Agent 5 spent time and money."""

    migration_flags: dict[str, bool]
    totals: dict[str, Any]
    turns_by_phase: dict[str, int]
    cost_by_phase_usd: dict[str, float]
    latency_by_phase_ms: dict[str, float]
    research: dict[str, Any]
    blocked_fetches: dict[str, Any]
    artifact_overhead: dict[str, Any]
    failure_packets: dict[str, Any]
    provider_health: dict[str, Any]
    abandoned_candidates: dict[str, Any]
    candidates: list[dict] = field(default_factory=list)


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
    efficiency_summary: EfficiencySummary | None = None
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


def _read_forensics_tail(
    harness_dir: Any, max_events: int = 50,
) -> list[dict]:
    """Read the last N events from a harness's harness_forensics.jsonl.

    Returns a list of decoded JSON objects (one per event). Empty list when
    the harness_dir is missing/None, the file doesn't exist, or read fails —
    never raises, never blocks report assembly.

    Used by the failed-build-candidate surfacing logic to attach post-mortem
    evidence to the report so the user can see exactly what the harness was
    doing before it failed validation.
    """
    if not harness_dir:
        return []
    try:
        from pathlib import Path
        log_path = Path(str(harness_dir)) / "harness_forensics.jsonl"
        if not log_path.exists():
            return []
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    tail = lines[-max_events:]
    out: list[dict] = []
    import json
    for line in tail:
        try:
            ev = json.loads(line)
            if isinstance(ev, dict):
                out.append(ev)
        except (ValueError, json.JSONDecodeError):
            continue
    return out


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
    for raw_tr in test_results:
        tr = _test_result_to_dict(raw_tr)
        score = float(tr.get("weighted_score") or tr.get("score") or 0.0)
        # Normalize audio_paths: accept list[dict] with {role, path} keys.
        # Silently drop any malformed entries — evidence is best-effort and
        # a broken entry shouldn't break the whole report.
        audio_paths, merged_audio_path = _normalize_audio_artifacts(tr)
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
        judge_failed = bool(
            tr.get("judge_failed")
            or tr.get("rubric_judge_failed")
            or (
                rubric_dict is None
                and bool(transcript_list)
                and (
                    tr.get("evaluation_mode") in {"agentic_conversation", "voice_realtime"}
                    or tr.get("rubric_error")
                    or tr.get("judge_error")
                    or tr.get("rubric_failure_reason")
                )
            )
        )
        judge_failure_reason = None
        if judge_failed:
            judge_failure_reason = str(
                tr.get("judge_failure_reason")
                or tr.get("rubric_failure_reason")
                or tr.get("rubric_error")
                or tr.get("judge_error")
                or "rubric verdict was unavailable for this conversational result"
            )[:300]
        ev = TestEvidence(
            test_case_id=str(tr.get("test_case_id") or tr.get("id") or ""),
            scenario=str(tr.get("scenario") or "")[:120],
            passed=_test_result_passed(tr),
            score=score,
            reasoning_excerpt=str(
                tr.get("reasoning") or tr.get("error") or ""
            )[:240],
            audio_paths=audio_paths,
            merged_audio_path=merged_audio_path,
            rubric_verdict=rubric_dict,
            judge_failed=judge_failed,
            judge_failure_reason=judge_failure_reason,
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


def _test_result_passed(tr: dict) -> bool:
    """Return the authoritative pass/fail bit for a test result.

    `success` means the harness/API call returned a syntactically usable
    response. `passed` means the evaluated output satisfied the test. Reports
    must use `passed` when present; otherwise a successful-but-low-quality API
    call is incorrectly counted as a passing test.
    """
    if "passed" in tr:
        return bool(tr.get("passed"))
    return bool(tr.get("success", False))


def _test_result_to_dict(tr: Any) -> dict:
    if isinstance(tr, dict):
        return tr
    if hasattr(tr, "model_dump"):
        return tr.model_dump()
    if hasattr(tr, "__dict__"):
        return dict(tr.__dict__)
    return {}


def _normalize_audio_artifacts(tr: dict) -> tuple[list[dict], str | None]:
    """Normalize audio artifacts and ensure merged conversation audio is first."""
    raw_audio = tr.get("audio_paths") or []
    audio_paths: list[dict] = []
    if isinstance(raw_audio, list):
        for a in raw_audio:
            if isinstance(a, dict) and "path" in a:
                clean = {
                    "role": str(a.get("role") or "unknown"),
                    "path": str(a["path"]),
                }
                if a.get("token"):
                    clean["token"] = str(a["token"])
                audio_paths.append(clean)

    merged_audio_path = tr.get("merged_audio_path")
    if merged_audio_path:
        merged_audio_path = str(merged_audio_path)
        if not any(
            a.get("role") == "conversation" and a.get("path") == merged_audio_path
            for a in audio_paths
        ):
            audio_paths.insert(0, {
                "role": "conversation",
                "path": merged_audio_path,
            })
    return audio_paths, merged_audio_path if merged_audio_path else None


def _collect_numeric_fields(obj: Any, keys: set[str], out: list[float], *, depth: int = 0) -> None:
    if depth > 4 or len(out) >= 200:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys:
                try:
                    numeric = float(value)
                    if numeric > 0:
                        out.append(numeric)
                except (TypeError, ValueError):
                    pass
            elif isinstance(value, (dict, list)):
                _collect_numeric_fields(value, keys, out, depth=depth + 1)
    elif isinstance(obj, list):
        for item in obj[:200]:
            if isinstance(item, (dict, list)):
                _collect_numeric_fields(item, keys, out, depth=depth + 1)


def _derive_avg_latency_ms(candidate_run: Any, test_results: list[Any]) -> float | None:
    """Compute truthful average latency from candidate/test/turn evidence."""

    raw_avg = _safe_get(candidate_run, "avg_latency_ms", None)
    try:
        raw_avg_f = float(raw_avg)
        if raw_avg_f > 0:
            return raw_avg_f
    except (TypeError, ValueError):
        pass

    latencies: list[float] = []
    for raw_tr in test_results:
        tr = _test_result_to_dict(raw_tr)
        for key in ("latency_ms", "duration_ms", "elapsed_ms"):
            try:
                value = float(tr.get(key))
                if value > 0:
                    latencies.append(value)
            except (TypeError, ValueError):
                pass
        for nested_key in ("raw_response", "details", "verdict_detail", "turns", "transcript"):
            nested = tr.get(nested_key)
            if nested is not None:
                _collect_numeric_fields(
                    nested,
                    {"latency_ms", "duration_ms", "elapsed_ms", "turn_latency_ms"},
                    latencies,
                )
    if not latencies:
        return None
    return sum(latencies) / len(latencies)


def _read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _add_counter(target: dict[str, int], key: Any, count: int = 1) -> None:
    normalized = str(key or "unknown")
    target[normalized] = target.get(normalized, 0) + count


def _candidate_efficiency_sources(agent5_result: Any | None) -> list[dict]:
    if not agent5_result:
        return []
    sources: list[dict] = []
    for harness in (_safe_get(agent5_result, "harnesses", []) or []):
        sources.append({
            "candidate_name": str(_safe_get(harness, "candidate_name", "") or ""),
            "harness_dir": _safe_get(harness, "harness_dir", None),
            "source": "built",
            "schema_turns": _safe_get(harness, "build_turns", None),
            "schema_cost_usd": _safe_get(harness, "build_cost_usd", None),
            "abandoned_reason": None,
        })
    for failed in (_safe_get(agent5_result, "failed_harnesses", []) or []):
        failure_category = str(_safe_get(failed, "failure_category", "") or "")
        failure_reason = str(_safe_get(failed, "failure_reason", "") or "")
        gate_status = str(_safe_get(failed, "completion_gate_status", "") or "")
        abandoned_reason = (
            failure_category
            if (
                gate_status == "abandoned"
                or failure_reason.lower().startswith("abandoned:")
            )
            else None
        )
        sources.append({
            "candidate_name": str(_safe_get(failed, "candidate_name", "") or ""),
            "harness_dir": _safe_get(failed, "harness_dir", None),
            "source": "failed",
            "schema_turns": _safe_get(failed, "turns_attempted", None),
            "schema_cost_usd": _safe_get(failed, "build_cost_usd", None),
            "failure_category": failure_category,
            "abandoned_reason": abandoned_reason,
        })
    return sources


def _build_efficiency_summary(
    agent5_result: Any | None,
    *,
    total_cost_usd: float,
) -> EfficiencySummary | None:
    from puzzleeval import config

    if not config.EFFICIENCY_SUMMARY_ENABLED:
        return None

    migration_flags = config.migration_flags_snapshot()
    sources = _candidate_efficiency_sources(agent5_result)
    turns_by_phase: dict[str, int] = {}
    cost_by_phase_usd: dict[str, float] = {}
    latency_by_phase_ms: dict[str, float] = {}
    provider_statuses: dict[str, int] = {}
    abandoned_reasons: dict[str, int] = {}
    failure_categories: dict[str, int] = {}
    candidates_summary: list[dict] = []
    research_totals = {
        "turns": 0,
        "web_fetch_results": 0,
        "empty_web_fetch_results": 0,
        "web_fetch_errors": 0,
        "web_search_results": 0,
        "advisor_results": 0,
        "debug_research_attempts": 0,
        "cache_read_tokens": 0,
        "total_billed_input": 0,
        "turns_with_cache_write": 0,
        "turns_with_cache_read_only": 0,
    }
    blocked_fetches = {
        "web_fetch_blocks": _safe_int(_safe_get(agent5_result, "web_fetch_blocks", 0)),
        "empty_web_fetch_results": 0,
        "web_fetch_errors": 0,
        "empty_web_fetch_by_url": {},
        "web_fetch_error_codes": {},
    }
    artifact_overhead = {
        "artifact_only_turns": 0,
        "text_only_turns": 0,
        "diagnostic_script_turns": 0,
        "scaffold_write_turns": 0,
        "single_file_scaffold_turns": 0,
        "parallel_tool_turns": 0,
        "total_tool_turns": 0,
    }
    failure_packets = {"count": 0, "by_category": {}}
    total_turns_observed = 0

    for item in sources:
        harness_dir_raw = item.get("harness_dir")
        harness_dir = Path(str(harness_dir_raw)) if harness_dir_raw else None
        summary = (
            _read_json_file(harness_dir / "conversation_summary.json")
            if harness_dir else {}
        )
        runtime_state = (
            _read_json_file(harness_dir / "_agent_state" / "runtime_state.json")
            if harness_dir else {}
        )
        agent_state_dir = harness_dir / "_agent_state" if harness_dir else None
        debug_ledger = (
            _read_json_file(agent_state_dir / "debug_research_ledger.json")
            if agent_state_dir else {}
        )
        debug_attempts = len(debug_ledger.get("attempts") or [])

        phase_buckets = summary.get("build_phases")
        if isinstance(phase_buckets, dict) and phase_buckets:
            for phase, bucket in phase_buckets.items():
                if not isinstance(bucket, dict):
                    continue
                phase_key = str(phase)
                turns_by_phase[phase_key] = (
                    turns_by_phase.get(phase_key, 0)
                    + _safe_int(bucket.get("turns"))
                )
                cost_by_phase_usd[phase_key] = (
                    cost_by_phase_usd.get(phase_key, 0.0)
                    + _safe_float(bucket.get("cost_usd"))
                )
                latency_by_phase_ms[phase_key] = (
                    latency_by_phase_ms.get(phase_key, 0.0)
                    + _safe_float(bucket.get("latency_ms"))
                )
        else:
            fallback_turns = _safe_int(item.get("schema_turns"))
            fallback_cost = _safe_float(item.get("schema_cost_usd"))
            if fallback_turns:
                turns_by_phase["unknown"] = turns_by_phase.get("unknown", 0) + fallback_turns
                cost_by_phase_usd["unknown"] = cost_by_phase_usd.get("unknown", 0.0) + fallback_cost

        aggregate = summary.get("aggregate") if isinstance(summary.get("aggregate"), dict) else {}
        total_turns = _safe_int(summary.get("total_turns"), _safe_int(item.get("schema_turns")))
        total_turns_observed += total_turns
        research = (
            summary.get("efficiency_analysis", {}).get("research", {})
            if isinstance(summary.get("efficiency_analysis"), dict)
            else {}
        )
        if isinstance(research, dict):
            for key in (
                "turns",
                "web_fetch_results",
                "empty_web_fetch_results",
                "web_fetch_errors",
                "web_search_results",
                "advisor_results",
            ):
                research_totals[key] += _safe_int(research.get(key))
            blocked_fetches["empty_web_fetch_results"] += _safe_int(
                research.get("empty_web_fetch_results")
            )
            blocked_fetches["web_fetch_errors"] += _safe_int(
                research.get("web_fetch_errors")
            )
            for url, count in (research.get("empty_web_fetch_by_url") or {}).items():
                _add_counter(blocked_fetches["empty_web_fetch_by_url"], url, _safe_int(count, 1))
            for code, count in (research.get("web_fetch_error_codes") or {}).items():
                _add_counter(blocked_fetches["web_fetch_error_codes"], code, _safe_int(count, 1))

        research_totals["debug_research_attempts"] += debug_attempts
        research_totals["cache_read_tokens"] += _safe_int(aggregate.get("cache_read_tokens"))
        research_totals["total_billed_input"] += _safe_int(aggregate.get("total_billed_input"))
        cache = summary.get("cache_analysis") if isinstance(summary.get("cache_analysis"), dict) else {}
        research_totals["turns_with_cache_write"] += _safe_int(cache.get("turns_with_cache_write"))
        research_totals["turns_with_cache_read_only"] += _safe_int(cache.get("turns_with_cache_read_only"))

        efficiency = summary.get("efficiency_analysis")
        if isinstance(efficiency, dict):
            waste = efficiency.get("waste_signals") or {}
            if isinstance(waste, dict):
                for key in (
                    "artifact_only_turns",
                    "text_only_turns",
                    "diagnostic_script_turns",
                ):
                    artifact_overhead[key] += _safe_int(waste.get(key))
            for key in (
                "scaffold_write_turns",
                "single_file_scaffold_turns",
                "parallel_tool_turns",
                "total_tool_turns",
            ):
                artifact_overhead[key] += _safe_int(efficiency.get(key))

        packet_summary = summary.get("failure_packets")
        if isinstance(packet_summary, dict):
            failure_packets["count"] += _safe_int(packet_summary.get("count"))
            for category, count in (packet_summary.get("by_category") or {}).items():
                _add_counter(failure_packets["by_category"], category, _safe_int(count, 1))

        provider_status = runtime_state.get("external_provider_status")
        if provider_status:
            _add_counter(provider_statuses, provider_status)
        abandoned_reason = item.get("abandoned_reason")
        if abandoned_reason:
            _add_counter(abandoned_reasons, abandoned_reason)
        failure_category = item.get("failure_category")
        if failure_category:
            _add_counter(failure_categories, failure_category)

        candidates_summary.append({
            "candidate_name": item.get("candidate_name") or "unknown",
            "source": item.get("source"),
            "turns": total_turns,
            "build_cost_usd": round(_safe_float(item.get("schema_cost_usd")), 4),
            "build_phases": phase_buckets if isinstance(phase_buckets, dict) else {},
            "cache_hit_pct": round(_safe_float(aggregate.get("cache_hit_pct")), 2),
            "research_turns": _safe_int(research.get("turns")) if isinstance(research, dict) else 0,
            "debug_research_attempts": debug_attempts,
            "failure_packets": packet_summary if isinstance(packet_summary, dict) else {"count": 0},
            "provider_status": provider_status,
            "abandoned_reason": abandoned_reason,
        })

    total_billed = research_totals["total_billed_input"]
    research_totals["cache_hit_pct"] = (
        round(100.0 * research_totals["cache_read_tokens"] / total_billed, 2)
        if total_billed > 0 else 0.0
    )
    cost_by_phase_usd = {
        phase: round(cost, 4) for phase, cost in cost_by_phase_usd.items()
    }
    latency_by_phase_ms = {
        phase: round(latency, 2) for phase, latency in latency_by_phase_ms.items()
    }
    total_build_cost = _safe_float(_safe_get(agent5_result, "total_build_cost_usd", 0.0))
    total_test_cost = _safe_float(_safe_get(agent5_result, "total_test_cost_usd", 0.0))
    raw_total_cost = _safe_float(total_cost_usd) or round(total_build_cost + total_test_cost, 6)

    return EfficiencySummary(
        migration_flags=migration_flags,
        totals={
            "candidates_attempted": len(sources),
            "total_turns_observed": total_turns_observed,
            "total_cost_usd": round(raw_total_cost, 6),
            "agent5_build_cost_usd": round(total_build_cost, 6),
            "agent5_test_cost_usd": round(total_test_cost, 6),
        },
        turns_by_phase=turns_by_phase,
        cost_by_phase_usd=cost_by_phase_usd,
        latency_by_phase_ms=latency_by_phase_ms,
        research=research_totals,
        blocked_fetches=blocked_fetches,
        artifact_overhead=artifact_overhead,
        failure_packets=failure_packets,
        provider_health={
            "status_counts": provider_statuses,
            "failure_categories": failure_categories,
        },
        abandoned_candidates={
            "count": sum(abandoned_reasons.values()),
            "by_reason": abandoned_reasons,
        },
        candidates=candidates_summary,
    )


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

    # Sort by outcome truth first. A 4/5 passing candidate should not lose
    # to a 2/5 candidate because of a small average-score delta.
    def _ranking_key(run: dict) -> tuple[float, float, float, float]:
        trs = _safe_get(run, "test_results", []) or []
        total = len(trs)
        passed = sum(1 for tr in trs if _test_result_passed(_test_result_to_dict(tr)))
        pass_rate_key = (passed / total) if total else 0.0
        score_key = float(_safe_get(run, "overall_score", None) or 0.0)
        cost_key = float(_safe_get(run, "cost_usd", None) or 0.0)
        latency = _derive_avg_latency_ms(run, trs) or 0.0
        return (pass_rate_key, score_key, -cost_key, -latency)
    sorted_runs = sorted(candidate_runs, key=_ranking_key, reverse=True)

    for rank_idx, cr in enumerate(sorted_runs):
        name = str(_safe_get(cr, "candidate_name", "") or "")
        provider = str(_safe_get(cr, "provider", "") or "")
        test_results = _safe_get(cr, "test_results", []) or []
        passed_count = sum(
            1 for tr in test_results
            if _test_result_passed(_test_result_to_dict(tr))
        )
        total_count = len(test_results)
        pass_rate = (passed_count / total_count) if total_count else 0.0
        overall_score = float(_safe_get(cr, "overall_score", None) or 0.0)
        avg_latency = _derive_avg_latency_ms(cr, test_results)
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
        test_evidence = sorted(
            failures + successes,
            key=lambda ev: (ev.test_case_id, not ev.passed),
        )
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
            test_evidence=test_evidence,
            pros=pros, cons=cons,
            coverage_gaps=[],
        ))

    # ── Failed-build candidate surfacing (Issue #6) ──────────────────────
    # When a harness builds but fails adversarial validation (e.g.,
    # ElevenLabs ConvAI's reader-thread bug from real-run trace 6e0c9563),
    # OR the build loop emits FailedHarness, the candidate is silently
    # dropped from candidate_runs[]. Without this block, the user-named
    # candidate disappears from the report with no explanation.
    #
    # Surface these candidates with build_succeeded=False, the
    # critical_failures list, and forensics_tail (the last 50 events
    # from harness_forensics.jsonl). They appear AFTER the ranked
    # successful builds so they don't compete on score; the frontend
    # renders them in a dedicated "build failed" section.
    already_reported = {rep.name.lower() for rep in candidate_reports}
    if agent5_result:
        # Adversarial-failed harnesses: built but harness_ready=False
        for h in (_safe_get(agent5_result, "harnesses", []) or []):
            h_name = str(_safe_get(h, "candidate_name", "") or "")
            if not h_name or h_name.lower() in already_reported:
                continue
            adv = _safe_get(h, "adversarial_report", {}) or {}
            # Convert adversarial_report to dict if it's an object
            if not isinstance(adv, dict):
                try:
                    adv = adv.__dict__
                except Exception:
                    adv = {}
            harness_ready = bool(adv.get("harness_ready", True))
            if harness_ready:
                continue  # built fine, just not in candidate_runs for some other reason
            critical_failures = list(adv.get("critical_failures", []) or [])
            forensics_tail = _read_forensics_tail(_safe_get(h, "harness_dir", None))
            candidate_reports.append(CandidateReport(
                name=h_name,
                provider=str(_safe_get(h, "provider", "") or ""),
                rank=len(candidate_reports) + 1,
                overall_score=0.0,
                pass_rate=0.0,
                passed_count=0,
                total_count=0,
                avg_latency_ms=None,
                cost_usd_per_call=None,
                monthly_cost_projection_usd=None,
                auth_method=str(_safe_get(h, "auth_method", "") or "unknown"),
                requirements=list(_safe_get(h, "requirements", []) or []),
                auth_env_vars=list(_safe_get(h, "auth_env_vars", []) or []),
                sandbox_used=False,
                build_succeeded=False,
                critical_failures=critical_failures,
                forensics_tail=forensics_tail,
            ))
            already_reported.add(h_name.lower())
            advisories.append(
                f"{h_name}: harness built but failed adversarial validation "
                f"({len(critical_failures)} critical issue(s)). See the "
                f"candidate's Forensics block for the last events before failure."
            )
        # Outright failed harnesses: never built or smoke-test never passed
        for fh in (_safe_get(agent5_result, "failed_harnesses", []) or []):
            f_name = str(_safe_get(fh, "candidate_name", "") or "")
            if not f_name or f_name.lower() in already_reported:
                continue
            failure_reason = str(_safe_get(fh, "failure_reason", "") or "unknown")
            failure_category = str(_safe_get(fh, "failure_category", "") or "")
            gate_status = _safe_get(fh, "completion_gate_status", None)
            gate_issues = list(_safe_get(fh, "completion_gate_issues", []) or [])
            abandoned_categories = {
                "api_incompatible",
                "credentials_unavailable",
                "docs_missing",
                "provider_blocked",
                "quota_exhausted",
                "test/fixture_mismatch_unfixable",
            }
            is_abandoned = (
                gate_status == "abandoned"
                or failure_category in abandoned_categories
                or failure_reason.lower().startswith("abandoned:")
            )
            if is_abandoned:
                abandon_reason = failure_category or "unknown"
                critical = [f"abandoned={abandon_reason}"]
                if failure_reason:
                    critical.append(failure_reason)
            else:
                abandon_reason = None
                critical = [failure_reason] if failure_reason else []
                if failure_category:
                    critical.append(f"failure_category={failure_category}")
                if gate_status:
                    critical.append(f"completion_gate_status={gate_status}")
                critical.extend(str(issue) for issue in gate_issues[:5])
            forensics_tail = _read_forensics_tail(_safe_get(fh, "harness_dir", None))
            candidate_reports.append(CandidateReport(
                name=f_name,
                provider=str(_safe_get(fh, "provider", "") or ""),
                rank=len(candidate_reports) + 1,
                overall_score=0.0,
                pass_rate=0.0,
                passed_count=0,
                total_count=0,
                avg_latency_ms=None,
                cost_usd_per_call=None,
                monthly_cost_projection_usd=None,
                auth_method="unknown",
                requirements=[],
                auth_env_vars=[],
                sandbox_used=False,
                build_succeeded=False,
                abandoned=is_abandoned,
                abandon_reason=abandon_reason,
                critical_failures=critical,
                forensics_tail=forensics_tail,
            ))
            already_reported.add(f_name.lower())
            if is_abandoned:
                advisories.append(
                    f"{f_name}: abandoned ({abandon_reason}) after evidence showed "
                    "more build-loop turns would not repair the candidate."
                )

    # Per-scope winners: best (highest overall_score) candidate that
    # actually covers each scope.
    eligible_reports = [
        rep for rep in candidate_reports
        if rep.build_succeeded and rep.total_count > 0
    ]
    winners_by_scope: dict[str, str] = {}
    for sid in blueprint_steps:
        best_name: str | None = None
        best_key: tuple[float, float] = (-1.0, -1.0)
        for rep in eligible_reports:
            cand = next(
                (c for c in candidates if str(_safe_get(c, "name", "")).lower() == rep.name.lower()),
                None,
            )
            if cand and sid in (_safe_get(cand, "covers_step_ids", []) or []):
                rep_key = (rep.pass_rate, rep.overall_score)
                if rep_key > best_key:
                    best_key = rep_key
                    best_name = rep.name
        if best_name:
            winners_by_scope[sid] = best_name

    overall_winner = eligible_reports[0].name if eligible_reports else None
    if not eligible_reports:
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
        efficiency_summary=_build_efficiency_summary(
            agent5_result,
            total_cost_usd=total_cost_usd,
        ),
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
    "EfficiencySummary",
    "EvaluationReport",
    "TestEvidence",
    "assemble_report",
    "report_to_dict",
]
