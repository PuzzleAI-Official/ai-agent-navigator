"""DEPRECATED — use ``puzzleeval.telemetry.cost`` instead.

This module is a thin shim preserved for back-compat. The canonical
location for LLM call cost calculation is ``puzzleeval.telemetry.cost``.

Deprecation removal: 2026-10-27 (6 months after this shim landed).
"""

from __future__ import annotations

from puzzleeval.telemetry.cost import (
    DEFAULT_ADVISOR_PRICING,
    DEFAULT_EXECUTOR_PRICING,
    calculate_call_cost,
)

__all__ = [
    "DEFAULT_ADVISOR_PRICING",
    "DEFAULT_EXECUTOR_PRICING",
    "calculate_call_cost",
]
