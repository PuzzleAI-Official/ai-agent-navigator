# ============================================================================
# Pipeline Run Manager — Intermediate Output Persistence & Summary
# ============================================================================
# Manages a single pipeline run: saves agent inputs/outputs to disk,
# tracks timing and cost, runs validators, and produces a final summary.
#
# USAGE:
#   run = PipelineRun(trace_id)
#   run.save_agent_result("agent_1", input_data, result, duration_ms=3200)
#   run.save_validation("agent_1", validation_result)
#   run.finalize()  # writes pipeline_summary.json
#
# OUTPUT:
#   runs/{trace_id}/
#     agent_1_input.json
#     agent_1_output.json
#     agent_1_validation.json
#     pipeline_summary.json
#
# This is a LIGHTWEIGHT wrapper — it doesn't change how agents work.
# The CLI/orchestrator calls save_agent_result() after each agent.
# ============================================================================

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from puzzleeval.validators import ValidationResult


class AgentRecord(BaseModel):
    """Record of a single agent's execution within a pipeline run.

    The optional ``metadata`` dict carries observability signals that don't
    fit the structured ``status`` / ``cost`` / ``validation`` slots — for
    example ``web_fetch_blocks`` (Phase 1 Cloudflare hardening). Populated
    automatically by ``save_agent_result`` from known fields on the output
    schema; keep additions documented and key names stable so downstream
    tooling (dashboards, CI checks) can rely on them.
    """
    name: str
    status: str             # "success", "failed", "validation_failed"
    duration_ms: float
    cost_usd: float | None = None
    validation: ValidationResult | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# Output-schema field names that get auto-promoted into AgentRecord.metadata
# whenever ``save_agent_result`` is called. Stable list — extend deliberately,
# do not pull in arbitrary fields (keeps pipeline_summary.json readable).
_AUTO_METADATA_FIELDS: tuple[str, ...] = ("web_fetch_blocks",)


# Derived-metadata extractors. Each callable receives the output model and
# returns (key, value) tuples to write into AgentRecord.metadata, or ()
# when the field isn't relevant for this output. Keep each extractor
# side-effect free and cheap. New phases add entries here to surface
# phase-specific fingerprints without touching call sites.
def _agent2_coverage_metadata(output: BaseModel) -> tuple[tuple[str, Any], ...]:
    """
    Phase 4: Agent 2 dual search fingerprint — how many candidates claim
    coverage, how many scopes they collectively cover, and whether every
    candidate has a non-empty covers_step_ids set. Silent on non-Agent-2
    outputs (returns ()).
    """
    candidates = getattr(output, "candidates", None)
    if candidates is None:
        return ()
    any_populated = any(
        bool(getattr(c, "covers_step_ids", frozenset())) for c in candidates
    )
    if not any_populated:
        return ()  # Legacy flat flow — skip the Phase 4 block entirely.
    all_populated = all(
        bool(getattr(c, "covers_step_ids", frozenset())) for c in candidates
    )
    all_scopes: set[str] = set()
    for c in candidates:
        all_scopes.update(getattr(c, "covers_step_ids", frozenset()))
    return (
        ("phase4_dual_search_active", True),
        ("phase4_scopes_covered_count", len(all_scopes)),
        ("phase4_coverage_populated_all", all_populated),
    )


_DERIVED_METADATA_EXTRACTORS: tuple = (_agent2_coverage_metadata,)


class PipelineRun:
    """
    Manages a single pipeline run — saves intermediate outputs and
    produces a summary report.

    Args:
        trace_id: UUID for this pipeline run
        output_dir: base directory for all runs (default: "runs")
    """

    def __init__(self, trace_id: str, output_dir: str = "runs"):
        self.trace_id = trace_id
        self.run_dir = Path(output_dir) / trace_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now(timezone.utc)
        self.agents: list[AgentRecord] = []
        self.failed_at: str | None = None

    def save_agent_result(
        self,
        agent_name: str,
        input_data: BaseModel,
        output_data: BaseModel,
        duration_ms: float,
        cost_usd: float | None = None,
    ) -> None:
        """
        Save an agent's input and output to the run directory.

        Writes two files:
          - {agent_name}_input.json
          - {agent_name}_output.json

        Also records the agent in the summary, auto-promoting any fields
        listed in ``_AUTO_METADATA_FIELDS`` (currently ``web_fetch_blocks``)
        from the output schema into ``AgentRecord.metadata`` so they appear
        in ``pipeline_summary.json`` without needing per-agent plumbing.
        """
        # Save input
        input_path = self.run_dir / f"{agent_name}_input.json"
        input_path.write_text(
            input_data.model_dump_json(indent=2), encoding="utf-8"
        )

        # Save output
        output_path = self.run_dir / f"{agent_name}_output.json"
        output_path.write_text(
            output_data.model_dump_json(indent=2), encoding="utf-8"
        )

        # Auto-promote known observability fields into the summary record.
        metadata: dict[str, Any] = {}
        for field_name in _AUTO_METADATA_FIELDS:
            value = getattr(output_data, field_name, None)
            if value is not None and value != 0:
                metadata[field_name] = value

        # Derived fingerprints — richer signals that aren't just a scalar
        # field on the output model. Each extractor is silent for outputs
        # it doesn't apply to. Keeps phase-specific fingerprints close to
        # their schemas without bloating the call sites.
        for extractor in _DERIVED_METADATA_EXTRACTORS:
            try:
                for key, value in extractor(output_data):
                    metadata[key] = value
            except Exception:
                # Never let a fingerprint helper break save_agent_result.
                continue

        # Record in summary
        self.agents.append(AgentRecord(
            name=agent_name,
            status="success",
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            metadata=metadata,
        ))

    def save_validation(
        self,
        agent_name: str,
        validation: ValidationResult,
    ) -> None:
        """
        Save validation results for an agent and update its record.

        Writes {agent_name}_validation.json and updates the agent's
        status if validation failed.
        """
        # Save validation result
        validation_path = self.run_dir / f"{agent_name}_validation.json"
        validation_path.write_text(
            validation.model_dump_json(indent=2), encoding="utf-8"
        )

        # Update the agent record
        for record in self.agents:
            if record.name == agent_name:
                record.validation = validation
                if not validation.passed:
                    record.status = "validation_failed"
                break

    def fail(self, agent_name: str, error: str) -> None:
        """
        Record that an agent failed with an error.

        Called when an agent throws an exception. The error message
        is saved in the summary for debugging.
        """
        self.failed_at = agent_name
        self.agents.append(AgentRecord(
            name=agent_name,
            status="failed",
            duration_ms=0,
            error=error,
        ))

    def finalize(self) -> dict:
        """
        Write the pipeline summary to disk and return it.

        Returns a dict with:
          - trace_id, timing, total cost
          - per-agent status, timing, cost, validation
          - overall pipeline status
        """
        completed_at = datetime.now(timezone.utc)
        total_duration_ms = round(
            (completed_at - self.started_at).total_seconds() * 1000, 2
        )
        total_cost = sum(
            r.cost_usd for r in self.agents if r.cost_usd is not None
        )

        # Determine overall status
        if self.failed_at:
            status = "failed"
        elif any(r.status == "validation_failed" for r in self.agents):
            status = "validation_failed"
        elif any(
            r.validation and r.validation.warnings
            for r in self.agents
        ):
            status = "completed_with_warnings"
        else:
            status = "completed"

        # Aggregate agent-level metadata to the run level for quick scanning.
        # Currently: web_fetch_blocks. Add new keys deliberately to keep this
        # block small and easy to check in CI.
        run_metadata: dict[str, Any] = {}
        for field_name in _AUTO_METADATA_FIELDS:
            total = sum(r.metadata.get(field_name, 0) for r in self.agents)
            if total:
                run_metadata[field_name] = total

        # Derived fingerprints — promote any that landed on at least one
        # agent. Booleans promote as OR; ints promote as max (the last /
        # highest agent wins). This keeps pipeline_summary.json queryable
        # for "did Phase N fire on this run?" without a shape-per-phase.
        for key in (
            "phase4_dual_search_active",
            "phase4_scopes_covered_count",
            "phase4_coverage_populated_all",
        ):
            values = [
                r.metadata[key] for r in self.agents if key in r.metadata
            ]
            if not values:
                continue
            if isinstance(values[0], bool):
                run_metadata[key] = any(values)
            elif isinstance(values[0], int):
                run_metadata[key] = max(values)
            else:
                run_metadata[key] = values[-1]

        agent_dicts: list[dict[str, Any]] = []
        for r in self.agents:
            d = r.model_dump(exclude_none=True)
            # Drop empty metadata dicts so the summary stays readable.
            if not d.get("metadata"):
                d.pop("metadata", None)
            agent_dicts.append(d)

        summary = {
            "trace_id": self.trace_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "total_duration_ms": total_duration_ms,
            "total_cost_usd": round(total_cost, 6),
            "status": status,
            "failed_at": self.failed_at,
            "agents": agent_dicts,
        }
        if run_metadata:
            summary["metadata"] = run_metadata

        # Write summary
        summary_path = self.run_dir / "pipeline_summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return summary
