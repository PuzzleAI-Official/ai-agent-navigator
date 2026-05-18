"""
Phase 7: Per-scope top-K candidate selection.

Replaces the global `sort by (credentials, relevance)` with independent
per-scope rankings. For each scope in the workflow, candidates whose
`covers_step_ids` includes that scope are scored and ranked. Top K
(tier-aware) are selected per scope.

A candidate covering M scopes competes in M scope rankings independently.
Coverage count is NOT a scoring input — broader coverage doesn't get a
bonus. Each scope's competition is standalone.

Used by:
  - pipeline_runner.py: provides default per-scope picks for the
    SelectionPanel (Phase 6) and the auto-run path (--no-interactive)
  - selected-candidate verification: exactly the selected K candidates per
    scope go to docs/access verification and Agent 5 research
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from puzzleeval.provider_registry import _normalize
from puzzleeval.schemas import Candidate, ScreenedCandidate


# Default weights — tunable via SCOPE_SELECTION_WEIGHTS config.
DEFAULT_WEIGHTS = {
    "user_picked_here": 0.40,
    "credentials": 0.20,
    "relevance_at_scope": 0.20,
    "docs_quality": 0.10,
    "pricing_fit": 0.10,
}


@dataclass(frozen=True)
class BuildCandidateSelection:
    """Orchestrator-owned Agent 5 build list plus audit details."""

    selected: list[ScreenedCandidate]
    blocked_missing_docs: list[ScreenedCandidate]
    blocked_missing_credentials: list[ScreenedCandidate]
    dropped_by_rank: list[ScreenedCandidate]
    audit_payload: dict[str, Any]

    def event_payload(self) -> dict[str, Any]:
        return {
            "selected": [c.name for c in self.selected],
            "blocked_missing_docs": [c.name for c in self.blocked_missing_docs],
            "blocked_missing_credentials": [
                c.name for c in self.blocked_missing_credentials
            ],
            "dropped_by_rank": [c.name for c in self.dropped_by_rank],
        }


def select_scope_candidate_pairs(
    candidates: list[Candidate] | list[ScreenedCandidate],
    blueprint_step_ids: list[str],
    cap_per_scope: int = 5,
    user_scope_picks: dict[str, list[str]] | None = None,
    credentials_available: set[str] | None = None,
    user_budget: float | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, list[str]]:
    """
    For each scope, rank eligible candidates and return top K names.

    Args:
        candidates: Agent 2's candidate pool (with covers_step_ids).
        blueprint_step_ids: ordered list of step IDs from the blueprint.
        cap_per_scope: max candidates per scope (tier-aware).
        user_scope_picks: if provided by Phase 6, OVERRIDES programmatic
            selection. Pass-through: returned as-is (Phase 6 picks are
            authoritative over Phase 7 defaults).
        credentials_available: set of normalized candidate names for
            which we have API keys (from provider_registry).
        user_budget: optional monthly budget in USD for pricing_fit.
        weights: scoring weights; defaults to DEFAULT_WEIGHTS.

    Returns:
        dict mapping scope_id -> list of candidate names (top K per scope).
    """
    # Phase 6 overrides Phase 7 — user picks are authoritative.
    if user_scope_picks is not None:
        return user_scope_picks

    w = weights or DEFAULT_WEIGHTS
    creds = credentials_available or set()

    result: dict[str, list[str]] = {}
    for scope_id in blueprint_step_ids:
        # Filter to candidates that cover this scope
        eligible = [
            c for c in candidates
            if scope_id in (c.covers_step_ids or [])
        ]
        if not eligible:
            result[scope_id] = []
            continue

        # Score each candidate at this scope
        scored: list[tuple[float, str]] = []
        for c in eligible:
            score = _score_at_scope(c, scope_id, creds, w, user_budget)
            scored.append((score, c.name))

        # Sort descending by score, take top K
        scored.sort(key=lambda x: (-x[0], x[1]))  # tiebreak: alphabetical
        result[scope_id] = [name for _, name in scored[:cap_per_scope]]

    return result


def candidate_has_build_credentials(
    candidate: ScreenedCandidate,
    provider_credentials: dict[str, dict[str, str]] | None,
) -> bool:
    """Return whether the candidate can be called in a live build."""

    if (getattr(candidate, "auth_method", "") or "").lower() == "no_auth":
        return True

    norm_provider = _normalize(getattr(candidate, "provider", "") or "")
    norm_candidate = _normalize(getattr(candidate, "name", "") or "")
    credential_keys = {
        _normalize(str(key))
        for key in (provider_credentials or {}).keys()
        if str(key or "").strip()
    }
    for key in credential_keys:
        if (
            key == norm_provider
            or key == norm_candidate
            or key in norm_provider
            or norm_provider in key
            or key in norm_candidate
            or norm_candidate in key
        ):
            return True

    # Direct Agent 5 tests/CLI runs can still provide explicit env vars on the
    # candidate. The orchestrator normally resolves these into
    # provider_credentials before Agent 5 starts.
    import os

    for var in getattr(candidate, "auth_env_vars", []) or []:
        if os.environ.get(str(var)):
            return True
    return False


def select_agent5_build_candidates(
    validated_candidates: list[ScreenedCandidate],
    *,
    trace_id: str,
    runs_root: str | Path | None,
    provider_credentials: dict[str, dict[str, str]] | None,
    max_candidates: int,
) -> BuildCandidateSelection:
    """Select the final Agent 5 build list from Agent 4 validated candidates."""

    from puzzleeval.docs_entrypoint import (
        docs_entrypoint_allows_automatic_build,
        resolve_docs_entrypoint_for_candidate,
    )
    from puzzleeval.web_doc_cache import candidate_sandbox_dir

    docs_ready: list[ScreenedCandidate] = []
    blocked_missing_docs: list[ScreenedCandidate] = []
    blocked_docs_details: list[dict[str, Any]] = []

    for candidate in validated_candidates:
        sandbox_dir = candidate_sandbox_dir(
            trace_id,
            candidate.name,
            runs_root=runs_root,
        )
        docs_payload = resolve_docs_entrypoint_for_candidate(candidate, sandbox_dir)
        docs_ok = docs_entrypoint_allows_automatic_build(candidate, sandbox_dir)
        if docs_ok:
            docs_ready.append(candidate)
        else:
            blocked_missing_docs.append(candidate)
            blocked_docs_details.append({
                "name": candidate.name,
                "provider": candidate.provider,
                "docs_verdict": docs_payload.get("docs_verdict"),
                "evidence_status": docs_payload.get("evidence_status"),
                "primary_docs_entrypoint": docs_payload.get("primary_docs_entrypoint"),
            })

    credentialed: list[ScreenedCandidate] = []
    blocked_missing_credentials: list[ScreenedCandidate] = []
    for candidate in docs_ready:
        if candidate_has_build_credentials(candidate, provider_credentials):
            credentialed.append(candidate)
        else:
            blocked_missing_credentials.append(candidate)

    ranked = sorted(
        credentialed,
        key=lambda item: (
            -float(getattr(item, "relevance_score", 0) or 0),
            item.name,
        ),
    )
    selected = ranked[: max(0, max_candidates)]
    dropped_by_rank = ranked[max(0, max_candidates):]

    audit_payload = {
        "schema_version": 1,
        "trace_id": trace_id,
        "selection_owner": "orchestrator",
        "source": "agent4_validated_candidates",
        "max_candidates": max_candidates,
        "input_candidates": [
            {"name": c.name, "provider": c.provider}
            for c in validated_candidates
        ],
        "selected": [
            {
                "name": c.name,
                "provider": c.provider,
                "relevance_score": c.relevance_score,
            }
            for c in selected
        ],
        "blocked_missing_docs": blocked_docs_details,
        "blocked_missing_credentials": [
            {"name": c.name, "provider": c.provider, "auth_method": c.auth_method}
            for c in blocked_missing_credentials
        ],
        "dropped_by_rank": [
            {
                "name": c.name,
                "provider": c.provider,
                "relevance_score": c.relevance_score,
            }
            for c in dropped_by_rank
        ],
    }

    return BuildCandidateSelection(
        selected=selected,
        blocked_missing_docs=blocked_missing_docs,
        blocked_missing_credentials=blocked_missing_credentials,
        dropped_by_rank=dropped_by_rank,
        audit_payload=audit_payload,
    )


def _score_at_scope(
    c,  # Candidate or ScreenedCandidate
    scope_id: str,
    credentials: set[str],
    weights: dict[str, float],
    user_budget: float | None,
) -> float:
    """
    Compute a weighted fitness score for a single candidate at a single scope.

    Dimensions:
      - user_picked_here: 1.0 if the user explicitly selected this candidate
        at this scope (always 0.0 in programmatic path; Phase 6 overrides
        bypass this function entirely).
      - credentials: 1.0 if we have API keys for this provider.
      - relevance_at_scope: the candidate's relevance_score (Agent 2's
        composite user-fit score — same at every scope since Agent 2 doesn't
        score per-scope yet).
      - docs_quality: 1.0 if api_docs_url is populated, 0.5 otherwise.
      - pricing_fit: 1.0 if free tier available or budget accommodates, 0.3 otherwise.

    Coverage count is NOT a dimension — a 5-scope tool doesn't outrank
    a 1-scope specialist at OCR. Each scope's competition is standalone.
    """
    w = weights
    name_key = c.name.strip().lower()

    # user_picked_here: always 0 in programmatic path (Phase 6 overrides bypass)
    score = w.get("user_picked_here", 0) * 0.0

    # credentials
    has_creds = name_key in credentials or getattr(c, "provider", "").strip().lower() in credentials
    score += w.get("credentials", 0) * (1.0 if has_creds else 0.0)

    # relevance_at_scope — use the overall relevance_score (Agent 2 doesn't
    # produce per-scope relevance yet; future phases could add it)
    rel = getattr(c, "relevance_score", 0.5)
    score += w.get("relevance_at_scope", 0) * rel

    # docs_quality
    has_docs = bool(getattr(c, "api_docs_url", None))
    score += w.get("docs_quality", 0) * (1.0 if has_docs else 0.5)

    # pricing_fit
    pricing_score = _pricing_fit(c, user_budget)
    score += w.get("pricing_fit", 0) * pricing_score

    return score


def _pricing_fit(c, user_budget: float | None) -> float:
    """
    Heuristic pricing fit. 1.0 = definitely fits, 0.0 = definitely doesn't.

    Uses the lightweight pricing fields from Agent 2 (pricing_model,
    pricing_details). When Phase 5's PricingBreakdown is populated,
    switches to structured tier analysis.
    """
    # Phase 5 structured pricing available?
    breakdown = getattr(c, "pricing_breakdown", None)
    if breakdown is not None:
        # Has free tier -> good fit
        if getattr(breakdown, "free_tier_monthly_units", None):
            return 1.0
        # Pay-as-you-go -> good fit (user pays only for what they use)
        if getattr(breakdown, "pay_as_you_go", False):
            return 0.9
        # Has tiers -> check against budget if available
        tiers = getattr(breakdown, "tiers", [])
        if tiers and user_budget is not None:
            cheapest = min(getattr(t, "monthly_cost_usd", float("inf")) for t in tiers)
            return 1.0 if cheapest <= user_budget else 0.3
        return 0.6  # has pricing info but can't evaluate fit

    # Fallback to Agent 2's loose pricing_model string
    model = getattr(c, "pricing_model", "unknown").lower()
    if model in ("free-tier", "freemium"):
        return 1.0
    if model in ("usage-based", "per-request", "per-page", "per-token"):
        return 0.8  # pay-as-you-go-ish
    if model == "monthly":
        return 0.6
    return 0.5  # unknown
