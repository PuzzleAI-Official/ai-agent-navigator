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

    # Cost tracking + circuit breaker.
    #
    # ``total_cost_usd`` is the running dollar total; ``budget`` is the
    # circuit-breaker that raises ``BudgetExceededError`` when the total
    # climbs above ``PUZZLEEVAL_MAX_RUN_COST_USD`` (default $25).
    #
    # Every place that accumulates cost MUST call ``state.record_cost(amt, reason)``
    # so the budget gets updated alongside the plain total — that single
    # helper guarantees the budget stays in sync no matter which agent
    # reports cost. The alternative (each caller updating both fields) is
    # fragile and one missed site defeats the breaker.
    total_cost_usd: float = 0.0
    budget: Any = None  # puzzleeval.budget.RunBudget; lazily attached in __post_init__

    def __post_init__(self) -> None:
        # Lazy import to avoid circulars — puzzleeval.budget is pure Python,
        # no FastAPI imports.
        if self.budget is None:
            from puzzleeval.budget import RunBudget
            self.budget = RunBudget()

    def record_cost(self, amount_usd: float, reason: str = "") -> None:
        """Record a billable event against BOTH the plain total and the
        budget circuit-breaker. Raises ``BudgetExceededError`` when the cap
        is crossed — callers should catch at the pipeline boundary and
        emit ``pipeline_failed`` with ``reason="budget_exceeded"``.
        """
        if amount_usd <= 0:
            return
        self.total_cost_usd += amount_usd
        self.budget.spend(amount_usd, reason)

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

    # Cached copy of the `selection_required` SSE payload so a late SSE
    # subscriber (page reload during the pause, network blip reconnect)
    # can reconstruct the selection state. Without this the EventBus has
    # no replay — `selection_required` fires once, and any consumer that
    # wasn't subscribed when it fired misses it forever, which looks to
    # the user like "SelectionPanel never appeared" even though the
    # backend is genuinely paused at `awaiting_candidate_selection`.
    pending_selection_payload: Optional[dict] = None

    # Cached workflow_blueprint payload — replayed on late SSE connects
    # so the frontend's `workflow` state rehydrates, which is required
    # for SelectionPanel to render (Playground conditions on
    # `stage === "selection" && workflow`). Without this, a user who
    # reloads the tab during the pause lands on a run that knows it's
    # paused but has no workflow shape to render the SelectionPanel
    # columns against.
    cached_workflow_blueprint: Optional[dict] = None

    # Cached candidates_found payload — replayed on late SSE connects so
    # the frontend's `candidates` list (consumed by SelectionPanel and
    # CandidateCard) rehydrates. The alternative of re-deriving from
    # Agent 2 output dicts couples the frontend to the Agent 2 schema;
    # caching the SSE payload keeps the frontend unchanged.
    cached_candidates_payload: Optional[dict] = None

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
