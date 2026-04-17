import asyncio

from fastapi import APIRouter, HTTPException

from models.api_models import ChatRequest, ChatResponse
from services.run_manager import run_manager
from services.pipeline_runner import mock_agent1_turn, real_agent1_turn, run_pipeline

router = APIRouter()


@router.post("/runs/{run_id}/chat", response_model=ChatResponse)
async def chat(run_id: str, req: ChatRequest):
    state = run_manager.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    state.status = "agent1_conversation"

    # Route to mock or real Agent 1. BudgetExceededError is raised by
    # state.record_cost() when the run crosses its configured cap — catch
    # here and surface a clear HTTP 402 (Payment Required) so the frontend
    # renders a different error path than generic server errors.
    from puzzleeval.budget import BudgetExceededError
    try:
        if state.agent_modes.get("agent1") == "mock":
            result = await mock_agent1_turn(state, req.message)
        else:
            result = await real_agent1_turn(state, req.message)
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "reason": "budget_exceeded",
                "spent_usd": exc.spent,
                "cap_usd": exc.cap,
                "message": (
                    f"Run budget exhausted: ${exc.spent:.4f} of ${exc.cap:.2f} cap. "
                    f"Raise PUZZLEEVAL_MAX_RUN_COST_USD and start a new run."
                ),
            },
        )

    pipeline_started = False

    # If Agent 1 says is_clear, auto-start the pipeline
    if result["is_clear"]:
        state.pipeline_task = asyncio.create_task(run_pipeline(state))
        pipeline_started = True

    return ChatResponse(
        is_clear=result["is_clear"],
        assistant_message=result["assistant_message"],
        clarifying_questions=result.get("clarifying_questions", []),
        pipeline_started=pipeline_started,
    )
