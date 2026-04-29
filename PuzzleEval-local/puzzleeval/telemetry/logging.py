"""Structured JSON logging — shared by ALL agents.

Promoted from ``puzzleeval/logging_setup.py`` as part of the cross-cutting
telemetry consolidation. The legacy import path remains as a thin shim.

WHY STRUCTURED LOGGING?
  Normal logs look like: "2024-01-15 INFO: Agent completed successfully"
  Structured logs look like: {"timestamp": "...", "agent_name": "...",
                              "tokens_in": 150, ...}

  Structured (JSON) logs are machine-readable. This means:
    - Cloud services (CloudWatch, Datadog, Grafana) can parse them automatically
    - You can filter/search by any field (e.g., "show me all errors from agent_1")
    - You can build dashboards tracking token usage, costs, latency over time
    - No migration needed later — JSON-lines format works everywhere

HOW IT WORKS:
  1. We subclass Python's built-in logging.Formatter to output JSON
  2. Each agent calls get_logger("agent_name") to get its own logger
  3. Logs go to stderr (never stdout) — stdout is reserved for agent output
  4. Extra fields (tokens, latency, cost) are passed via Python's `extra` dict

NEW IN telemetry consolidation:
  ``record_llm_call(ctx, model, response, latency_ms)`` — the canonical
  cross-cutting entry point that takes a ``TelemetryContext``, computes
  cost via the canonical pricing tables, and emits the structured log
  entry. Replaces the per-agent ``log_llm_call(logger, response, ...)``
  pattern (legacy entry point preserved for back-compat).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from puzzleeval.telemetry.context import TelemetryContext
from puzzleeval.telemetry.cost import calculate_call_cost
from puzzleeval.telemetry.pricing_tables import MODEL_PRICING


# Re-export config defaults so legacy callers don't need to chase the new path.
LOG_LEVEL = os.environ.get("PUZZLEEVAL_LOG_LEVEL", "INFO")
LOG_OUTPUT_PATH = os.environ.get("PUZZLEEVAL_LOG_PATH", None)


class StructuredJsonFormatter(logging.Formatter):
    """Convert every log record into a single-line JSON object.

    A log record is Python's internal representation of one log message.
    It contains the log level, message, timestamp, and any extra fields
    we attach (like tokens_in, latency_ms, etc.).
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "agent_name": record.name,
            "message": record.getMessage(),
        }

        # Optional fields — only included if the caller passed them via
        # the `extra` parameter. Keeps logs clean (no null spam).
        optional_fields = [
            "operation",
            "trace_id",
            "run_id",
            "candidate_id",
            "agent_id",
            "latency_ms",
            "tokens_in",
            "tokens_out",
            "cost_usd",
            "error",
            "error_type",
            "model",
            "stop_reason",
            "cache_creation_tokens",
            "cache_read_tokens",
            "phase",
            "turn",
        ]

        for field in optional_fields:
            value = getattr(record, field, None)
            if value is not None:
                log_entry[field] = value

        # Anything in record.__dict__ that we haven't already captured AND
        # was passed via `extra={...}` is included. Skips Python's built-in
        # LogRecord attributes (covered by ``_LOGRECORD_BUILTIN_ATTRS``).
        for key, value in record.__dict__.items():
            if (
                key not in _LOGRECORD_BUILTIN_ATTRS
                and key not in log_entry
                and not key.startswith("_")
            ):
                if value is not None:
                    log_entry[key] = value

        return json.dumps(log_entry, ensure_ascii=False, default=str)


# Set of LogRecord attributes that Python's logging module sets internally.
# Anything outside this set passed via ``extra={...}`` is user-provided
# and should appear in the structured output.
_LOGRECORD_BUILTIN_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


def setup_logging() -> None:
    """Configure the root logger for the entire application.

    Call this ONCE at startup (in cli.py or api.py), before any agent runs.

    What it does:
      1. Sets the log level (INFO by default, configurable via PUZZLEEVAL_LOG_LEVEL)
      2. Adds a stderr handler with our JSON formatter
      3. Optionally adds a file handler (if PUZZLEEVAL_LOG_PATH is set)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    # Remove any existing handlers to avoid duplicate logs if setup_logging()
    # is called more than once (e.g., in tests).
    root_logger.handlers.clear()

    formatter = StructuredJsonFormatter()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root_logger.addHandler(stderr_handler)

    if LOG_OUTPUT_PATH:
        file_handler = logging.FileHandler(LOG_OUTPUT_PATH)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def get_logger(agent_name: str) -> logging.Logger:
    """Get a named logger for a specific agent.

    Each agent calls this with its own name:
        logger = get_logger("user_understanding")
        logger.info("Starting analysis", extra={"trace_id": "abc-123"})
    """
    return logging.getLogger(agent_name)


def generate_trace_id() -> str:
    """Generate a unique trace ID for one user request.

    This ID is passed through ALL agents in the pipeline so you can find
    every log related to a single evaluation by searching for the trace_id.
    """
    return str(uuid.uuid4())


def log_llm_call(
    logger: logging.Logger,
    response: Any,
    model: str,
    trace_id: str,
    start_time: float,
    operation: str = "llm_call",
) -> float:
    """Legacy entry point — log an LLM call and return cost.

    PREFER ``record_llm_call(ctx, model, response, latency_ms)`` for new
    code. This function is preserved for back-compat with all existing
    call sites.

    Args:
        logger: The agent's logger instance.
        response: The raw Anthropic API response object (has .usage attribute).
        model: Model name string (e.g., "claude-sonnet-4-6").
        trace_id: The request's trace ID for log correlation.
        start_time: time.time() value from BEFORE the API call started.
        operation: Description of what this call was for.

    Returns:
        Estimated cost in USD.
    """
    latency_ms = round((time.time() - start_time) * 1000, 2)

    tokens_in = response.usage.input_tokens
    tokens_out = response.usage.output_tokens
    cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0

    cost_usd = calculate_call_cost(response, model)

    logger.info(
        "LLM call completed",
        extra={
            "operation": operation,
            "trace_id": trace_id,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cache_creation_tokens": cache_creation if cache_creation else None,
            "cache_read_tokens": cache_read if cache_read else None,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "stop_reason": getattr(response, "stop_reason", None),
        },
    )

    return cost_usd


def record_llm_call(
    ctx: TelemetryContext,
    *,
    model: str,
    response: Any,
    latency_ms: float | None = None,
    operation: str | None = None,
) -> float:
    """Canonical cross-cutting entry point for logging an LLM call.

    Replaces ``log_llm_call`` for new code. Reads context from
    ``TelemetryContext`` (no positional logger / trace_id args), computes
    cost from the canonical pricing tables, emits a structured log entry.

    Args:
        ctx: TelemetryContext bundle (trace_id, agent_id, run_id, candidate_id).
        model: Model name string.
        response: Anthropic API response (must have .usage).
        latency_ms: Optional latency override (use this when measuring with
                    track_time externally; otherwise we don't know the
                    start-time and emit None).
        operation: Optional operation override (defaults to ctx.operation).

    Returns:
        Estimated cost in USD.
    """
    logger = get_logger(ctx.agent_id)
    cost_usd = calculate_call_cost(response, model)

    tokens_in = response.usage.input_tokens
    tokens_out = response.usage.output_tokens
    cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0

    log_extra = ctx.to_log_extra()
    log_extra.update({
        "operation": operation or ctx.operation or "llm_call",
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_creation_tokens": cache_creation if cache_creation else None,
        "cache_read_tokens": cache_read if cache_read else None,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "stop_reason": getattr(response, "stop_reason", None),
    })

    logger.info("LLM call completed", extra=log_extra)
    return cost_usd


__all__ = [
    "StructuredJsonFormatter",
    "generate_trace_id",
    "get_logger",
    "log_llm_call",
    "record_llm_call",
    "setup_logging",
]
