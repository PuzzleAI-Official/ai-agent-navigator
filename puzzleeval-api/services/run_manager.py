import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from services.event_bus import EventBus


@dataclass
class RunState:
    run_id: str
    trace_id: str
    # Status values: "created", "agent1_conversation", "pipeline_running",
    # "awaiting_candidate_selection" (Phase 6 pause),
    # "completed", "failed", "cancelled".
    status: str = "created"
    cancel_requested: bool = False
    # Phase 6: asyncio.Event-based cancellation signal, used alongside
    # `cancel_requested` for clean `await` paths (cancel_requested remains
    # for sync polling code that hasn't migrated to event-driven waits).
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

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

    # ── Phase 2: Service tier scaffold ──
    # ``plan`` selects the user-facing tier (free / paid / enterprise).
    # ``credits_remaining`` is the in-memory ledger for paid-tier credit
    # gating; None means unlimited (free or enterprise — see billing.py).
    # ``credits_consumed`` and ``plan_gates_triggered`` are the run-level
    # observability counters that surface in pipeline_summary.json metadata.
    # All four are no-ops when PUZZLEEVAL_BILLING_ENFORCED=0 (the default).
    plan: str = "free"
    credits_remaining: Optional[int] = None
    credits_consumed: int = 0
    plan_gates_triggered: int = 0

    # ── Phase 6: User Candidate Selection ──
    # After Agent 2 emits its candidate pool, the pipeline pauses and
    # awaits `selection_ready`. The route handler
    # /runs/{id}/select-candidates validates the picks, writes them to
    # `user_scope_picks` + `user_added_candidates`, and sets the event.
    # `selection_required_emitted_at` is recorded as an ISO timestamp
    # when the pause begins — used by ops tooling to measure pause
    # duration and by `pipeline_summary.json:metadata` as a Phase 6
    # fingerprint.
    selection_ready: asyncio.Event = field(default_factory=asyncio.Event)
    user_scope_picks: Optional[dict[str, list[str]]] = None
    user_added_candidates: list[dict] = field(default_factory=list)
    selection_required_emitted_at: Optional[str] = None
    user_selection_applied: bool = False

    # SSE
    event_bus: EventBus = field(default_factory=EventBus)

    # Background task
    pipeline_task: Optional[asyncio.Task] = None


class RunManager:
    """In-memory store for active runs."""

    def __init__(self):
        self._runs: dict[str, RunState] = {}

    def create_run(
        self,
        text: str,
        agent_modes: dict[str, str] | None = None,
        plan: str = "free",
    ) -> RunState:
        run_id = str(uuid.uuid4())[:8]
        trace_id = str(uuid.uuid4())
        # Lazy import: billing imports run_manager via TYPE_CHECKING to avoid
        # the cycle. Importing here at call time keeps the module graph clean.
        from services.billing import starting_credits_for
        state = RunState(
            run_id=run_id,
            trace_id=trace_id,
            user_text=text,
            plan=plan,
            credits_remaining=starting_credits_for(plan),
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
        # Phase 6: also signal the asyncio.Event so the pause in
        # pipeline_runner wakes up immediately instead of sleeping
        # until the selection_ready event fires.
        state.cancel_event.set()
        return True

    def list_runs(self) -> list[str]:
        return list(self._runs.keys())


# Singleton
run_manager = RunManager()
