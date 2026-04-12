import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from services.event_bus import EventBus


@dataclass
class RunState:
    run_id: str
    trace_id: str
    status: str = "created"  # created, agent1_conversation, pipeline_running, completed, failed, cancelled
    cancel_requested: bool = False

    # Agent modes
    agent_modes: dict[str, str] = field(default_factory=lambda: {
        "agent1": "mock", "agent2": "mock", "agent3": "mock",
        "agent4": "mock", "agent5": "mock",
    })

    # Agent 1 conversation state
    conversation_history: list[dict] = field(default_factory=list)
    agent1_result: Optional[dict] = None
    user_text: str = ""
    current_turn: int = 0

    # Pipeline results (populated as agents complete)
    agent2_result: Optional[dict] = None
    agent3_result: Optional[dict] = None
    agent4_result: Optional[dict] = None
    agent5_result: Optional[dict] = None

    # File management
    uploaded_files: list[dict] = field(default_factory=list)
    file_id_to_path: dict[str, str] = field(default_factory=dict)

    # Cost tracking
    total_cost_usd: float = 0.0

    # SSE
    event_bus: EventBus = field(default_factory=EventBus)

    # Background task
    pipeline_task: Optional[asyncio.Task] = None


class RunManager:
    """In-memory store for active runs."""

    def __init__(self):
        self._runs: dict[str, RunState] = {}

    def create_run(self, text: str, agent_modes: dict[str, str] | None = None) -> RunState:
        run_id = str(uuid.uuid4())[:8]
        trace_id = str(uuid.uuid4())
        state = RunState(
            run_id=run_id,
            trace_id=trace_id,
            user_text=text,
        )
        if agent_modes:
            state.agent_modes = agent_modes
        self._runs[run_id] = state
        return state

    def get_run(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def cancel_run(self, run_id: str) -> bool:
        state = self._runs.get(run_id)
        if not state:
            return False
        state.cancel_requested = True
        return True

    def list_runs(self) -> list[str]:
        return list(self._runs.keys())


# Singleton
run_manager = RunManager()
