import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from services.event_bus import SSEEvent
from services.run_manager import run_manager

router = APIRouter()


@router.get("/runs/{run_id}/events")
async def stream_events(run_id: str):
    state = run_manager.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_generator():
        # Replay cached state-setting payloads to any late subscriber.
        #
        # EventBus is an in-memory queue.Queue with no replay buffer — an
        # event is consumed by at most one subscriber call. If the user
        # (a) loaded the page while the pipeline was already past an
        # event, or (b) reconnected after a network blip / SSE drop,
        # the original `workflow_blueprint` / `candidates_found` /
        # `selection_required` emits are gone and the frontend state
        # never populates, producing "SelectionPanel never appeared" or
        # "No progress for 2m — pipeline may be stuck" banners even
        # though the backend is genuinely paused at
        # `awaiting_candidate_selection`.
        #
        # Order matters: workflow_blueprint before candidates_found
        # before selection_required — matches the natural order the
        # frontend hook expects to process them, and the
        # SelectionPanel render condition is
        # ``stage === "selection" && workflow`` so `workflow` needs
        # to set first.
        if state.cached_workflow_blueprint:
            replay = SSEEvent(
                event_type="workflow_blueprint",
                data=state.cached_workflow_blueprint,
            )
            yield {"event": replay.event_type, "data": replay.format()}

        if state.cached_candidates_payload:
            replay = SSEEvent(
                event_type="candidates_found",
                data=state.cached_candidates_payload,
            )
            yield {"event": replay.event_type, "data": replay.format()}

        if (
            state.status == "awaiting_candidate_selection"
            and state.pending_selection_payload
        ):
            replay = SSEEvent(
                event_type="selection_required",
                data=state.pending_selection_payload,
            )
            yield {"event": replay.event_type, "data": replay.format()}

        async for event in state.event_bus.subscribe():
            yield {
                "event": event.event_type,
                "data": event.format(),
            }

    return EventSourceResponse(event_generator())
