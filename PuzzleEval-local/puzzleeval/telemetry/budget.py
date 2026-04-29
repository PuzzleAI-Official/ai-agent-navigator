"""Per-run cost budget — circuit-breaker for runaway pipelines.

Every PuzzleEval run accumulates Anthropic + plugin-provider cost across
Agents 1-5 plus tool-plugin calls (TTS, transcription, vision, etc.). A
pathological Agent 5 builder loop with deep adaptive thinking + many
turns + 4 candidates can climb past $10 quickly; a bug in the loop
(infinite retry, cache miss storm) could go higher. Without a cap, the
user pays for our bugs.

This module exposes a tiny ``RunBudget`` interface threaded through the
pipeline. Each agent (and each plugin invocation) calls
``budget.spend(usd, reason)`` after a billable action; the budget object
raises ``BudgetExceededError`` when the running total crosses the cap.

The cap is set at run-construction time (FastAPI ``run_manager`` reads
``PUZZLEEVAL_MAX_RUN_COST_USD``) and exposed as ``state.budget``. Code
paths that don't have access to the run state (CLI standalone, unit
tests) get a permissive default.

Why a class, not a global counter:
  * Multiple concurrent runs in the same process need isolated budgets.
  * Cancellation, status, and cost-event surfacing all want a single
    object to subscribe to.
  * Pure data — no global state, no thread-locals.

This module was promoted from ``puzzleeval/budget.py`` to
``puzzleeval/telemetry/budget.py`` as part of the cross-cutting
telemetry consolidation. The legacy import path remains as a thin shim.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


DEFAULT_MAX_RUN_COST_USD = float(
    os.environ.get("PUZZLEEVAL_MAX_RUN_COST_USD", "25.0")
)


class BudgetExceededError(Exception):
    """Raised when a run's accumulated cost crosses its configured cap.

    Caught by the pipeline runner and converted into a clear user-facing
    ``pipeline_failed`` event with `reason="budget_exceeded"`. NEVER
    swallow this — the user explicitly opted into the cap.
    """

    def __init__(self, *, spent: float, cap: float, last_reason: str = "") -> None:
        self.spent = spent
        self.cap = cap
        self.last_reason = last_reason
        super().__init__(
            f"Run budget exceeded: spent ${spent:.4f} of ${cap:.2f} cap"
            + (f" (last: {last_reason})" if last_reason else "")
        )


@dataclass
class _SpendRecord:
    """One billable event."""
    amount_usd: float
    reason: str
    cumulative_usd: float


@dataclass
class RunBudget:
    """Thread-safe cost accumulator with a hard cap.

    Use ``RunBudget.unlimited()`` for code paths that genuinely don't
    care (one-off CLI invocations, unit tests).
    """

    cap_usd: float = DEFAULT_MAX_RUN_COST_USD
    spent_usd: float = 0.0
    history: list[_SpendRecord] = field(default_factory=list)
    on_spend: Callable[[float, str, float], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def unlimited(cls) -> "RunBudget":
        """Budget with no cap. Use only for non-production paths."""
        return cls(cap_usd=float("inf"))

    def spend(self, amount_usd: float, reason: str = "") -> None:
        """Record a billable event. Raises ``BudgetExceededError`` over cap.

        ``amount_usd`` may be 0.0 (free operation logged for transparency).
        Negative amounts are clamped to 0 — this is a budget tracker, not
        a refund processor.
        """
        if amount_usd < 0:
            amount_usd = 0.0
        with self._lock:
            self.spent_usd += amount_usd
            cumulative = self.spent_usd
            self.history.append(_SpendRecord(
                amount_usd=amount_usd, reason=reason,
                cumulative_usd=cumulative,
            ))
            over_cap = cumulative > self.cap_usd
            cb = self.on_spend
        if cb is not None:
            try:
                cb(amount_usd, reason, cumulative)
            except Exception as exc:  # noqa: BLE001
                logger.warning("RunBudget on_spend callback raised: %s", exc)
        if over_cap and self.cap_usd != float("inf"):
            raise BudgetExceededError(
                spent=cumulative, cap=self.cap_usd, last_reason=reason,
            )

    def remaining(self) -> float:
        """Remaining budget; +inf for unlimited, 0 when exceeded."""
        with self._lock:
            return max(0.0, self.cap_usd - self.spent_usd)

    def utilization(self) -> float:
        """Spent / cap ratio; 0.0 for unlimited (no meaningful ratio)."""
        if self.cap_usd == float("inf") or self.cap_usd == 0:
            return 0.0
        with self._lock:
            return self.spent_usd / self.cap_usd

    def snapshot(self) -> dict:
        """JSON-friendly summary for SSE events / pipeline_summary.

        Computes utilization inline (instead of calling self.utilization())
        so we don't try to re-acquire the lock — threading.Lock is not
        re-entrant, and a re-acquire deadlocks the calling thread.
        """
        with self._lock:
            spent = self.spent_usd
            cap = self.cap_usd
            unlimited = cap == float("inf")
            utilization = (spent / cap) if (not unlimited and cap > 0) else 0.0
            return {
                "spent_usd": round(spent, 6),
                "cap_usd": None if unlimited else round(cap, 2),
                "remaining_usd": (
                    None if unlimited else round(max(0.0, cap - spent), 6)
                ),
                "utilization": round(utilization, 4),
                "event_count": len(self.history),
            }


__all__ = [
    "BudgetExceededError",
    "DEFAULT_MAX_RUN_COST_USD",
    "RunBudget",
]
