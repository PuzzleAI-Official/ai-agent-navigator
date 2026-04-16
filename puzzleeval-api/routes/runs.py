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
    SelectCandidatesRequest,
    SelectCandidatesResponse,
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
        "awaiting_candidate_selection": "selection",  # Phase 6
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


@router.post("/runs/{run_id}/select-candidates", response_model=SelectCandidatesResponse)
async def select_candidates(run_id: str, req: SelectCandidatesRequest):
    """
    Phase 6: submit per-scope candidate picks + optional user-added providers.

    Validates:
      - Run exists and is in awaiting_candidate_selection state.
      - Selection hasn't already been submitted (idempotency guard).
      - At least one scope has picks.
      - Every scope_id in scope_picks matches the blueprint.
      - Every user-added candidate's covers_step_ids references valid scopes.
      - Every picked candidate name exists in Agent 2's results OR the add list.

    On success: records picks on RunState, signals selection_ready, pipeline
    resumes. Returns acceptance counts.
    """
    state = run_manager.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    if state.status != "awaiting_candidate_selection":
        raise HTTPException(
            status_code=400,
            detail=f"Run is not awaiting selection; current status={state.status}",
        )

    if state.selection_ready.is_set():
        raise HTTPException(
            status_code=400,
            detail="Selection already submitted for this run",
        )

    # Must have at least one scope with at least one pick
    if not req.scope_picks or not any(req.scope_picks.values()):
        raise HTTPException(
            status_code=400,
            detail="At least one scope must have candidate picks",
        )

    # Validate scope_ids against the blueprint
    blueprint_step_ids: set[str] = set()
    try:
        raw_result = (state.agent1_result or {}).get("result", {})
        raw_workflow = raw_result.get("workflow")
        if raw_workflow:
            for step in raw_workflow.get("steps", []):
                blueprint_step_ids.add(step.get("id", ""))
    except (AttributeError, TypeError):
        pass

    if blueprint_step_ids:
        for scope_id in req.scope_picks:
            if scope_id not in blueprint_step_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown scope_id in picks: '{scope_id}'",
                )
        for ua in req.add:
            if not ua.covers_step_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"User-added candidate '{ua.name}' has no covers_step_ids",
                )
            for scope_id in ua.covers_step_ids:
                if scope_id not in blueprint_step_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"User-added candidate '{ua.name}' references "
                            f"unknown scope_id: '{scope_id}'"
                        ),
                    )

    # Validate picked candidate names exist in Agent 2 results or add list
    a2_names = set()
    for c in (state.agent2_result or {}).get("candidates", []):
        a2_names.add(c.get("name", "").strip().lower())
    user_added_names = {ua.name.strip().lower() for ua in req.add}
    all_known = a2_names | user_added_names

    for scope_id, names in req.scope_picks.items():
        for name in names:
            if name.strip().lower() not in all_known:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Picked candidate '{name}' at scope '{scope_id}' "
                        f"not in Agent 2 results or user-added list"
                    ),
                )

    # Record picks on RunState
    state.user_scope_picks = req.scope_picks
    state.user_added_candidates = [ua.model_dump() for ua in req.add]

    # Compute acceptance stats
    total_pairs = sum(len(names) for names in req.scope_picks.values())
    scope_coverage = {sid: len(names) for sid, names in req.scope_picks.items()}

    # Signal the pipeline to resume
    state.selection_ready.set()

    return SelectCandidatesResponse(
        accepted_count=total_pairs,
        scope_coverage=scope_coverage,
    )


@router.delete("/runs/{run_id}", response_model=CancelRunResponse)
async def cancel_run(run_id: str):
    success = run_manager.cancel_run(run_id)
    if not success:
        raise HTTPException(status_code=404, detail="Run not found")
    return CancelRunResponse(cancelled=True)
