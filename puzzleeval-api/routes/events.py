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
        async for event in state.event_bus.subscribe():
            yield {
                "event": event.event_type,
                "data": event.format(),
            }

    return EventSourceResponse(event_generator())
