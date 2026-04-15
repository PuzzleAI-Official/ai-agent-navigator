# ============================================================================
# Monitoring Routes (Phase 2 stub — enterprise tier surface)
# ============================================================================
# These endpoints are placeholders for the eventual enterprise-only
# observability service: ongoing monitoring of a deployed AI agent's
# performance against the Agent 3 / 3F test cases, drift detection,
# alert thresholds, etc. The routes exist NOW so the URL surface is
# committed and the billing gate is wired through; the actual handlers
# will land in a later phase (post-Phase 9, when the workflow harness
# is available to re-run on a schedule).
#
# Behavior today:
#   - GET /monitoring/{run_id}/status
#   - POST /monitoring/{run_id}/check
# Both check that the run's plan == "enterprise" via billing.plan_allows().
# Anything else returns 402 Payment Required regardless of
# PUZZLEEVAL_BILLING_ENFORCED — these endpoints are enterprise-only by
# design, not credit-gated.
#
# When the monitoring system is built out, replace the body with real logic
# but keep the gate at the top of each route.
# ============================================================================

from fastapi import APIRouter, HTTPException

from services.billing import plan_allows
from services.run_manager import run_manager

router = APIRouter()


def _require_enterprise(run_id: str):
    """Internal: 404 if no run, 402 if not on enterprise plan. Used by all routes."""
    state = run_manager.get_run(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not plan_allows(state.plan, "monitoring"):
        raise HTTPException(
            status_code=402,
            detail=(
                f"Monitoring is an enterprise-only feature. Current plan: "
                f"'{state.plan}'. Contact sales to upgrade."
            ),
        )
    return state


@router.get("/monitoring/{run_id}/status")
async def monitoring_status(run_id: str):
    """Stub: ongoing monitoring health for a deployed harness.

    Returns a placeholder payload until the monitoring backend is built.
    The 402 gate ensures only enterprise plans can probe this surface.
    """
    state = _require_enterprise(run_id)
    return {
        "run_id": state.run_id,
        "monitoring_enabled": False,
        "status": "not_yet_implemented",
        "message": (
            "Enterprise monitoring backend is not yet built. This endpoint "
            "is reserved for future implementation."
        ),
    }


@router.post("/monitoring/{run_id}/check")
async def monitoring_check(run_id: str):
    """Stub: trigger an on-demand re-evaluation of the run's harness.

    Will eventually re-execute Agent 3 test cases against the live API and
    flag drift. For now it returns 501 with the same enterprise gate.
    """
    _require_enterprise(run_id)
    raise HTTPException(
        status_code=501,
        detail="On-demand monitoring check is not yet implemented.",
    )
