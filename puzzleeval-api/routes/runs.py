import asyncio

from fastapi import APIRouter, HTTPException

from models.api_models import (
    CancelRunResponse,
    CandidateOut,
    CreateRunRequest,
    CreateRunResponse,
    PipelineProgressOut,
    Quota,
    RunStateOut,
)
from services.billing import quota_snapshot
from services.run_manager import run_manager
from services.pipeline_runner import run_pipeline

router = APIRouter()


@router.post("/runs", response_model=CreateRunResponse)
async def create_run(req: CreateRunRequest):
    # Phase 2: pass plan through to RunManager so the right credit balance
    # gets initialized via billing.starting_credits_for(plan).
    state = run_manager.create_run(req.text, req.agent_modes, plan=req.plan)
    return CreateRunResponse(run_id=state.run_id, trace_id=state.trace_id)


@router.get("/runs/{run_id}", response_model=RunStateOut)
async def get_run(run_id: str):
    state = run_manager.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    # Determine stage from status
    stage_map = {
        "created": "conversation",
        "agent1_conversation": "conversation",
        "pipeline_running": "pipeline",
        "completed": "results",
        "failed": "results",
        "cancelled": "results",
    }

    return RunStateOut(
        run_id=state.run_id,
        trace_id=state.trace_id,
        status=state.status,
        stage=stage_map.get(state.status, "conversation"),
        cost_usd=state.total_cost_usd,
        quota=Quota(**quota_snapshot(state)),
    )


@router.delete("/runs/{run_id}", response_model=CancelRunResponse)
async def cancel_run(run_id: str):
    success = run_manager.cancel_run(run_id)
    if not success:
        raise HTTPException(status_code=404, detail="Run not found")
    return CancelRunResponse(cancelled=True)
