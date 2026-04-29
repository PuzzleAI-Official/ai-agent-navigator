"""Cross-cutting observability for PuzzleEval.

This package is the canonical owner of:
  * Structured JSON logging (was: puzzleeval/logging_setup.py)
  * Cost / token accounting (was: puzzleeval/agents/agent5/costing.py)
  * Run budget tracking (was: puzzleeval/budget.py)
  * Pricing tables (was: puzzleeval/config.py:MODEL_PRICING)
  * Time tracking (NEW: track_time context manager)
  * TelemetryContext (NEW: bundles trace_id, agent_id, run_id, candidate_id)

Public API — every agent imports from here:

    from puzzleeval import telemetry as obs

    ctx = obs.TelemetryContext(
        trace_id="...", agent_id="agent_5", run_id="...", candidate_id="...",
    )

    with obs.track_time("agent_5_build_turn") as timer:
        response = client.beta.messages.create(...)

    obs.record_llm_call(
        ctx,
        model="claude-opus-4-7",
        response=response,
        latency_ms=timer.elapsed_ms,
    )

The legacy import paths (``puzzleeval.logging_setup``, ``puzzleeval.budget``,
``puzzleeval.agents.agent5.costing``) survive as thin shims that re-export
from this package. Existing call sites continue to work unchanged.
"""

from __future__ import annotations

from puzzleeval.telemetry.budget import (
    BudgetExceededError,
    DEFAULT_MAX_RUN_COST_USD,
    RunBudget,
)
from puzzleeval.telemetry.context import TelemetryContext
from puzzleeval.telemetry.cost import (
    DEFAULT_ADVISOR_PRICING,
    DEFAULT_EXECUTOR_PRICING,
    calculate_call_cost,
    estimate_cost,
)
from puzzleeval.telemetry.logging import (
    StructuredJsonFormatter,
    generate_trace_id,
    get_logger,
    log_llm_call,
    record_llm_call,
    setup_logging,
)
from puzzleeval.telemetry.pricing_tables import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER_1H,
    CACHE_WRITE_MULTIPLIER_5M,
    MODEL_PRICING,
    WEB_SEARCH_PRICE_PER_SEARCH,
    lookup_pricing,
)
from puzzleeval.telemetry.timing import Timer, track_time

__all__ = [
    # Context
    "TelemetryContext",
    # Logging
    "StructuredJsonFormatter",
    "generate_trace_id",
    "get_logger",
    "log_llm_call",
    "record_llm_call",
    "setup_logging",
    # Cost
    "calculate_call_cost",
    "estimate_cost",
    "DEFAULT_ADVISOR_PRICING",
    "DEFAULT_EXECUTOR_PRICING",
    # Pricing tables
    "MODEL_PRICING",
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER_1H",
    "CACHE_WRITE_MULTIPLIER_5M",
    "WEB_SEARCH_PRICE_PER_SEARCH",
    "lookup_pricing",
    # Budget
    "BudgetExceededError",
    "DEFAULT_MAX_RUN_COST_USD",
    "RunBudget",
    # Timing
    "Timer",
    "track_time",
]
