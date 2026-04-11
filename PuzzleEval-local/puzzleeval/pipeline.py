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

from pydantic import BaseModel

from puzzleeval.validators import ValidationResult


class AgentRecord(BaseModel):
    """Record of a single agent's execution within a pipeline run."""
    name: str
    status: str             # "success", "failed", "validation_failed"
    duration_ms: float
    cost_usd: float | None = None
    validation: ValidationResult | None = None
    error: str | None = None


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

        Also records the agent in the summary.
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

        # Record in summary
        self.agents.append(AgentRecord(
            name=agent_name,
            status="success",
            duration_ms=duration_ms,
            cost_usd=cost_usd,
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

        summary = {
            "trace_id": self.trace_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "total_duration_ms": total_duration_ms,
            "total_cost_usd": round(total_cost, 6),
            "status": status,
            "failed_at": self.failed_at,
            "agents": [
                r.model_dump(exclude_none=True) for r in self.agents
            ],
        }

        # Write summary
        summary_path = self.run_dir / "pipeline_summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return summary
