# ============================================================================
# Billing Service (Phase 2: Service Tier Scaffold)
# ============================================================================
# Thin gating + observability layer for the eventual user-facing PuzzleAI plans:
#   - free       : unlimited search (Agents 1-3); testing (Agents 4-5) blocked
#                  unless explicitly allowed for trials
#   - paid       : credit-based testing — buy credits, spend on Agents 4/5
#   - enterprise : everything in paid + monitoring/observability endpoints
#
# Design constraint (carries through every line in this module):
#   When PUZZLEEVAL_BILLING_ENFORCED=0 (the DEFAULT), this module is a NO-OP
#   for blocking purposes — every gate call records usage for observability
#   but never raises. CLI users are unaffected. Backend users on the legacy
#   "everything is free" model are unaffected. The day product wants to flip
#   billing on, set the env var to 1; nothing else needs to change.
#
# What this module does NOT do (deliberately):
#   - No persistence. Credit ledger is per-RunState, in-memory. Real billing
#     will need a database; that lives behind this same interface later.
#   - No payment integration. Stripe / etc. lives one layer up.
#   - No multi-tenant auth. user_id is a stub until Phase 6 user picking
#     adds first-class identity.
#   - No usage-by-day rollups. Phase 10 bench scripts can compute these from
#     pipeline_summary.json metadata; we don't need a separate ledger.
#
# Phase fingerprint:
#   pipeline_summary.json:metadata.credits_consumed
#   pipeline_summary.json:metadata.plan_gates_triggered
#   per-call logs include {"plan": ..., "agent": ..., "credits_after": ...}
#
# Diagnostic flag:
#   PUZZLEEVAL_BILLING_ENFORCED=0 (default) -> no-op, only tracks usage
#   PUZZLEEVAL_BILLING_ENFORCED=1           -> raises HTTPException(402) on gate fail
# ============================================================================

import logging
import os
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    # Avoid circular import at module load — RunState imports from here would
    # be ugly. Use TYPE_CHECKING to get the hint without the runtime cost.
    from services.run_manager import RunState

logger = logging.getLogger("puzzleeval_api.billing")


# ---------------------------------------------------------------------------
# Configuration knobs (env-driven)
# ---------------------------------------------------------------------------
# Single master switch: when 0, every gate is a no-op that just tracks usage.
# When 1, gates raise HTTPException(402) on insufficient credits or
# unauthorized features. Default 0 so existing deployments don't break.
BILLING_ENFORCED: bool = os.environ.get("PUZZLEEVAL_BILLING_ENFORCED", "0") == "1"


# Per-agent credit cost. Agents 1-3 are "search" (free in every plan).
# Agent 4 (screening) costs 1 credit per run (single API call surface).
# Agent 5 (build + test) is the expensive one — averages 4 candidates × ~$1.25
# each in our existing benchmarks, so we anchor 5 credits ≈ $5 of API spend.
# These map "credits" to "rough USD" 1:1 for now; the conversion can shift
# without callers caring as long as require_credits() stays the only gate.
CREDIT_COST_PER_AGENT: dict[str, int] = {
    "agent_1": 0,
    "agent_2": 0,
    "agent_3": 0,
    "agent_3f": 0,
    "agent_4": 1,
    "agent_5": 5,
}


# Plan -> features the plan can use. "search" gates Agents 1-3 (free for all).
# "testing" gates Agents 4-5. "monitoring" gates the future enterprise-only
# observability endpoints (stubbed in routes/monitoring.py).
PLAN_FEATURE_MATRIX: dict[str, dict[str, bool]] = {
    "free": {
        "search": True,
        "testing": False,
        "monitoring": False,
    },
    "paid": {
        "search": True,
        "testing": True,
        "monitoring": False,
    },
    "enterprise": {
        "search": True,
        "testing": True,
        "monitoring": True,
    },
}


# Plan -> starting credit balance. None = unlimited (no credit gating; only
# the feature matrix gates). Free tier gets unlimited search credits because
# search costs nothing. Paid tier starts with 100 credits (~$100 of API spend);
# real product will refresh / let users buy more — that lives at the route
# layer, not here. Enterprise is unlimited (billed differently).
PLAN_STARTING_CREDITS: dict[str, int | None] = {
    "free": None,         # unlimited (but only "search" features allowed)
    "paid": 100,
    "enterprise": None,   # unlimited
}


# Map agent_name -> required feature so gating can check both "do you have
# the feature on this plan?" and "do you have credits?" in one call.
AGENT_REQUIRED_FEATURE: dict[str, str] = {
    "agent_1": "search",
    "agent_2": "search",
    "agent_3": "search",
    "agent_3f": "search",
    "agent_4": "testing",
    "agent_5": "testing",
}


# ---------------------------------------------------------------------------
# Public API — read helpers
# ---------------------------------------------------------------------------

def credit_cost_for_agent(agent: str) -> int:
    """How many credits invoking this agent costs. Unknown agents default to 0."""
    return CREDIT_COST_PER_AGENT.get(agent, 0)


def plan_allows(plan: str, feature: str) -> bool:
    """Does this plan allow this feature? Unknown plan or feature returns False."""
    return PLAN_FEATURE_MATRIX.get(plan, {}).get(feature, False)


def starting_credits_for(plan: str) -> int | None:
    """Initial credit balance for a new run on this plan. None = unlimited."""
    return PLAN_STARTING_CREDITS.get(plan)


def required_feature_for(agent: str) -> str | None:
    """Which feature flag this agent needs. None = no feature requirement."""
    return AGENT_REQUIRED_FEATURE.get(agent)


def is_unlimited(state: "RunState") -> bool:
    """True if the run's plan has no credit cap (free unlimited / enterprise)."""
    return state.credits_remaining is None


# ---------------------------------------------------------------------------
# Public API — gating
# ---------------------------------------------------------------------------

def require_agent_access(state: "RunState", agent: str) -> None:
    """Single entry point used by pipeline_runner before invoking an agent.

    Performs two checks in order:
      1. Feature gate — does ``state.plan`` even allow this feature class?
         (e.g. "free" plan can search but not test.)
      2. Credit gate — does the run have enough credits left for this agent?
         Skipped when ``credits_remaining is None`` (unlimited plans).

    On success, decrements credits (when applicable) and increments the
    ``credits_consumed`` counter. On failure:
      - If BILLING_ENFORCED: raises HTTPException(402) — the route layer
        translates this into an SSE failure event for the caller.
      - If not enforced: increments ``plan_gates_triggered`` and returns
        normally so the legacy behavior (everything runs) is preserved.

    Either way, the gate counters end up in pipeline_summary.json under
    metadata.credits_consumed / metadata.plan_gates_triggered for
    operator observability.
    """
    feature = required_feature_for(agent)
    cost = credit_cost_for_agent(agent)

    # ---- Feature gate ----
    if feature is not None and not plan_allows(state.plan, feature):
        _record_gate_trigger(state, agent, reason=f"feature_not_in_plan:{feature}")
        if BILLING_ENFORCED:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Feature '{feature}' is not available on the '{state.plan}' "
                    f"plan. Upgrade to access {agent}."
                ),
            )
        # Non-enforced mode: log and continue (legacy behavior).
        return

    # ---- Credit gate (skipped for unlimited plans) ----
    if state.credits_remaining is not None and cost > 0:
        if state.credits_remaining < cost:
            _record_gate_trigger(state, agent, reason="insufficient_credits")
            if BILLING_ENFORCED:
                raise HTTPException(
                    status_code=402,
                    detail=(
                        f"Insufficient credits for {agent}: need {cost}, have "
                        f"{state.credits_remaining}. Top up your balance."
                    ),
                )
            # Non-enforced: track but allow.
            return

        # Successful gate — decrement balance and record consumption.
        state.credits_remaining -= cost

    state.credits_consumed += cost
    logger.info(
        "billing_gate_passed",
        extra={
            "agent": agent,
            "plan": state.plan,
            "credits_cost": cost,
            "credits_after": state.credits_remaining,
            "credits_consumed_total": state.credits_consumed,
            "trace_id": getattr(state, "trace_id", None),
        },
    )


def _record_gate_trigger(state: "RunState", agent: str, reason: str) -> None:
    """Internal: log a gate hit and bump the trigger counter."""
    state.plan_gates_triggered += 1
    log_fn = logger.warning if BILLING_ENFORCED else logger.info
    log_fn(
        "billing_gate_triggered",
        extra={
            "agent": agent,
            "plan": state.plan,
            "reason": reason,
            "enforced": BILLING_ENFORCED,
            "credits_remaining": state.credits_remaining,
            "trace_id": getattr(state, "trace_id", None),
        },
    )


# ---------------------------------------------------------------------------
# Snapshot for API responses (Quota model in api_models.py reads this)
# ---------------------------------------------------------------------------

def quota_snapshot(state: "RunState") -> dict:
    """Build the ``Quota`` payload exposed to the frontend via RunStateOut.

    Lives here (not on RunState) so the plan/feature matrix stays in one
    place — RunState is a pure data carrier.
    """
    return {
        "plan": state.plan,
        "credits_remaining": state.credits_remaining,
        "credits_consumed": state.credits_consumed,
        "tier_features": dict(PLAN_FEATURE_MATRIX.get(state.plan, {})),
        "billing_enforced": BILLING_ENFORCED,
    }
