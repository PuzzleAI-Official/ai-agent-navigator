from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from services.run_manager import run_manager

router = APIRouter()


@router.get("/runs/{run_id}/events")
async def stream_events(run_id: str):
    state = run_manager.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_generator():
        # EventBus.subscribe() is a broadcast bus — every subscriber
        # receives every event, and new subscribers are seeded with the
        # full run history. This handles:
        #   (a) page reload during pause → history replay rehydrates
        #       workflow / candidates / selection state cleanly
        #   (b) SSE network blip reconnect → history catches up the
        #       gap, then live events resume
        #   (c) React Strict Mode double-mount → both subscribers get
        #       the full event stream (no split-brain)
        #
        # The defense-in-depth cached_* fields on RunState
        # (workflow_blueprint / candidates / selection_payload) remain
        # on the state for programmatic access by other routes, but the
        # events endpoint no longer needs an explicit replay — the
        # bus handles it.
        async for event in state.event_bus.subscribe():
            yield {
                "event": event.event_type,
                "data": event.format(),
            }

    return EventSourceResponse(event_generator())
