"""TelemetryContext — bundles per-call observability state.

A single dataclass that travels with every LLM call and structured log.
Replaces the previous pattern of passing ``trace_id``, ``agent_name``,
``run_id``, ``candidate_id`` as separate kwargs to every logging call.

Frozen + hashable so it can serve as a lru_cache key when needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class TelemetryContext:
    """Per-call observability context.

    Threaded through every LLM call so log entries, metrics, and cost
    accounting share a consistent context shape.

    Fields:
        trace_id: UUID linking all logs for one user request across agents.
                  Generated once per request via ``generate_trace_id()``.
        agent_id: Stable agent identifier (e.g., ``"agent_5"``,
                  ``"agent_5.build_loop"``). Used as the logger name AND
                  the metric label.
        run_id: Optional run UUID (FastAPI ``RunState.run_id``). Distinguishes
                concurrent runs in the same process. None for CLI invocations.
        candidate_id: Optional candidate name (e.g., ``"OpenAI Realtime"``)
                      when the call is per-candidate. None for pipeline-level
                      calls (Agent 1, Agent 2, etc.).
        operation: Optional operation tag (e.g., ``"build_turn"``,
                   ``"verification"``, ``"evaluation"``). Surfaces in metric
                   labels for fine-grained filtering.
        extra: Catch-all dict for context fields the caller wants to thread
               through but that don't justify their own field.
    """

    trace_id: str
    agent_id: str
    run_id: str | None = None
    candidate_id: str | None = None
    operation: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def with_operation(self, operation: str) -> "TelemetryContext":
        """Return a copy with ``operation`` overridden.

        Useful for narrowing context to a specific phase without mutating
        the original (which is frozen anyway).
        """
        return replace(self, operation=operation)

    def with_candidate(self, candidate_id: str) -> "TelemetryContext":
        """Return a copy with ``candidate_id`` set/overridden."""
        return replace(self, candidate_id=candidate_id)

    def with_extra(self, **fields: Any) -> "TelemetryContext":
        """Return a copy with ``extra`` merged with new fields."""
        return replace(self, extra={**self.extra, **fields})

    def to_log_extra(self) -> dict[str, Any]:
        """Convert to the ``extra={...}`` dict format expected by logging.

        Renames ``agent_id`` → ``agent_name`` to preserve the existing log
        schema. Drops None values so log entries stay clean.
        """
        out: dict[str, Any] = {"trace_id": self.trace_id}
        if self.run_id is not None:
            out["run_id"] = self.run_id
        if self.candidate_id is not None:
            out["candidate_id"] = self.candidate_id
        if self.operation is not None:
            out["operation"] = self.operation
        for k, v in self.extra.items():
            if v is not None:
                out[k] = v
        return out


__all__ = ["TelemetryContext"]
