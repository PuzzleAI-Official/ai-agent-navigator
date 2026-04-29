"""DEPRECATED — use ``puzzleeval.telemetry.budget`` instead.

This module is a thin shim preserved for back-compat. The canonical
location for run-budget tracking is ``puzzleeval.telemetry.budget``.
Existing imports continue to work; new code should use the canonical
path.

Deprecation removal: 2026-10-27 (6 months after this shim landed).
"""

from __future__ import annotations

from puzzleeval.telemetry.budget import (
    BudgetExceededError,
    DEFAULT_MAX_RUN_COST_USD,
    RunBudget,
)

__all__ = [
    "BudgetExceededError",
    "DEFAULT_MAX_RUN_COST_USD",
    "RunBudget",
]
