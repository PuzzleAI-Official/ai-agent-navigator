# ============================================================================
# Structured JSON Logging — Shared by ALL agents
# ============================================================================
# WHY STRUCTURED LOGGING?
#   Normal logs look like: "2024-01-15 INFO: Agent completed successfully"
#   Structured logs look like: {"timestamp": "...", "agent_name": "...", "tokens_in": 150, ...}
#
#   Structured (JSON) logs are machine-readable. This means:
#     - Cloud services (CloudWatch, Datadog, Grafana) can parse them automatically
#     - You can filter/search by any field (e.g., "show me all errors from agent_1")
#     - You can build dashboards tracking token usage, costs, latency over time
#     - No migration needed later — JSON-lines format works everywhere
#
# HOW IT WORKS:
#   1. We subclass Python's built-in logging.Formatter to output JSON
#   2. Each agent calls get_logger("agent_name") to get its own logger
#   3. Logs go to stderr (never stdout) — stdout is reserved for agent output
#   4. Extra fields (tokens, latency, cost) are passed via Python's `extra` dict
#
# LATER MIGRATION TO CLOUD:
#   To send logs to CloudWatch/Datadog/etc., you have two options:
#     Option A: Set PUZZLEEVAL_LOG_PATH to a file, point a log agent at that file
#     Option B: Run the process and pipe stderr to a log collector
#   Either way, ZERO code changes needed — the JSON format is already compatible.
# ============================================================================

import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from puzzleeval.config import (
    LOG_LEVEL,
    LOG_OUTPUT_PATH,
    MODEL_PRICING,
    CACHE_WRITE_MULTIPLIER_5M,
    CACHE_READ_MULTIPLIER,
)


# ============================================================================
# Custom JSON Formatter
# ============================================================================
# Python's logging module uses "Formatters" to control how log messages look.
# The default formatter produces plain text lines. We override it to produce
# JSON objects — one per line (called "JSON-lines" or "JSONL" format).
# ============================================================================

class StructuredJsonFormatter(logging.Formatter):
    """
    Converts every log record into a single-line JSON object.

    A log record is Python's internal representation of one log message.
    It contains the log level, message, timestamp, and any extra fields
    we attach (like tokens_in, latency_ms, etc.).
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Called by Python's logging system for every log message.
        We build a dictionary with all the fields we want, then serialize to JSON.

        Args:
            record: Python's internal log record object. Contains:
                    - record.levelname: "INFO", "ERROR", etc.
                    - record.getMessage(): the log message text
                    - record.name: the logger name (our agent_name)
                    - Any extra fields we passed via the `extra` parameter
        """
        # Build the base log entry with fields that are ALWAYS present
        log_entry = {
            # ISO 8601 timestamp with timezone — the universal standard for logs.
            # Example: "2024-01-15T10:30:45.123456+00:00"
            "timestamp": datetime.now(timezone.utc).isoformat(),

            # Log severity level: DEBUG, INFO, WARNING, ERROR, CRITICAL
            "level": record.levelname,

            # Which agent produced this log. Set when calling get_logger("name").
            "agent_name": record.name,

            # The human-readable log message (e.g., "LLM call completed")
            "message": record.getMessage(),
        }

        # ---------------------------------------------------------------
        # Optional fields — only included if the caller passed them via
        # the `extra` parameter. This keeps logs clean (no null spam).
        #
        # Example of passing extra fields:
        #   logger.info("LLM call done", extra={"tokens_in": 150, "latency_ms": 1200})
        # ---------------------------------------------------------------
        optional_fields = [
            "operation",     # What operation was being performed (e.g., "llm_call", "file_parse")
            "trace_id",      # UUID linking all logs for one user request across agents
            "latency_ms",    # How long the operation took in milliseconds
            "tokens_in",     # Number of input tokens sent to Claude
            "tokens_out",    # Number of output tokens Claude generated
            "cost_usd",      # Estimated cost of this API call in USD
            "error",         # Error message if something went wrong
            "error_type",    # Exception class name (e.g., "AgentRateLimitError")
            "model",         # Which Claude model was used
            "stop_reason",   # Why Claude stopped generating (e.g., "end_turn", "max_tokens")
            "cache_creation_tokens",  # Tokens written to cache (one-time 25% premium)
            "cache_read_tokens",      # Tokens read from cache (90% cheaper than regular input)
        ]

        for field in optional_fields:
            # getattr checks if the log record has this field attached.
            # If the caller passed extra={"tokens_in": 150}, then
            # getattr(record, "tokens_in") returns 150.
            value = getattr(record, field, None)
            if value is not None:
                log_entry[field] = value

        # Serialize to a single JSON line. ensure_ascii=False allows Unicode
        # characters (accents, CJK, etc.) to pass through without escaping.
        return json.dumps(log_entry, ensure_ascii=False)


# ============================================================================
# Logger Setup Functions
# ============================================================================

def setup_logging() -> None:
    """
    Configure the root logger for the entire application.
    Call this ONCE at startup (in cli.py or api.py), before any agent runs.

    What it does:
      1. Sets the log level (INFO by default, configurable via PUZZLEEVAL_LOG_LEVEL)
      2. Adds a stderr handler with our JSON formatter
      3. Optionally adds a file handler (if PUZZLEEVAL_LOG_PATH is set)
    """
    # Get the root logger — all other loggers inherit from this one.
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    # Remove any existing handlers to avoid duplicate logs if setup_logging()
    # is called more than once (e.g., in tests).
    root_logger.handlers.clear()

    # Create our JSON formatter
    formatter = StructuredJsonFormatter()

    # -------------------------------------------------------------------
    # Handler 1: stderr (always active)
    # -------------------------------------------------------------------
    # WHY STDERR?
    # In Unix/Linux, every program has two output streams:
    #   stdout (file descriptor 1) — for the program's actual output (data)
    #   stderr (file descriptor 2) — for diagnostic/log messages
    #
    # By sending logs to stderr and agent JSON output to stdout, you can:
    #   python -m puzzleeval.cli --text "..." > output.json 2> logs.jsonl
    #                                          ^^^^^^^^^^^^   ^^^^^^^^^^^
    #                                          agent result   structured logs
    # -------------------------------------------------------------------
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root_logger.addHandler(stderr_handler)

    # -------------------------------------------------------------------
    # Handler 2: file (only if PUZZLEEVAL_LOG_PATH is set)
    # -------------------------------------------------------------------
    # This writes the same JSON logs to a file. Useful for:
    #   - Persistent log storage
    #   - Cloud log agents that watch files (e.g., Datadog Agent, CloudWatch Agent)
    # -------------------------------------------------------------------
    if LOG_OUTPUT_PATH:
        file_handler = logging.FileHandler(LOG_OUTPUT_PATH)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def get_logger(agent_name: str) -> logging.Logger:
    """
    Get a named logger for a specific agent.

    Each agent calls this with its own name:
        logger = get_logger("user_understanding")
        logger.info("Starting analysis", extra={"trace_id": "abc-123"})

    The agent_name appears in every log line as the "agent_name" field,
    making it easy to filter logs by agent in your monitoring tools.

    Args:
        agent_name: A short, descriptive name like "user_understanding",
                    "research", "screening", etc.

    Returns:
        A Python logging.Logger instance configured with our JSON formatter.
    """
    return logging.getLogger(agent_name)


# ============================================================================
# Convenience Functions
# ============================================================================

def generate_trace_id() -> str:
    """
    Generate a unique trace ID for one user request.

    This ID is passed through ALL agents in the pipeline so you can find
    every log related to a single evaluation by searching for the trace_id.

    Example trace_id: "a7b3c1d2-e4f5-6789-0abc-def123456789"

    In production, you'd typically generate this in the API layer when a
    request comes in, and thread it through every agent call.
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
    """
    Log the details of a Claude API call in a standardized format.

    This is called after every LLM API call to capture:
      - Token usage (input + output)
      - Estimated cost in USD
      - Latency in milliseconds
      - Which model was used
      - Why Claude stopped generating

    Args:
        logger:     The agent's logger instance
        response:   The raw Anthropic API response object (has .usage attribute)
        model:      Model name string (e.g., "claude-sonnet-4-5-20250929")
        trace_id:   The request's trace ID for log correlation
        start_time: The time.time() value from BEFORE the API call started
        operation:  Description of what this call was for (default: "llm_call")
    """
    # Calculate how long the API call took
    latency_ms = round((time.time() - start_time) * 1000, 2)

    # Extract token counts from the response.
    # The Anthropic SDK response has a .usage object with these fields.
    tokens_in = response.usage.input_tokens
    tokens_out = response.usage.output_tokens

    # Extract cache metrics — these tell us how much we're saving.
    # cache_creation_input_tokens: tokens that were cached for the first time
    #   (costs 25% MORE than regular input — a one-time investment)
    # cache_read_input_tokens: tokens read from cache instead of reprocessed
    #   (costs 90% LESS than regular input — this is the payoff)
    cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0

    # Calculate cost using our pricing table, now accounting for cache pricing.
    # If the model isn't in our table, we use 0 (unknown cost is better than crashing).
    input_price, output_price = MODEL_PRICING.get(model, (0, 0))

    # Cost breakdown:
    #   Regular input tokens:  1.00x base input price
    #   Cache write tokens:    1.25x base input price (for 5-min default TTL)
    #   Cache read tokens:     0.10x base input price (90% discount, the payoff)
    #   Output tokens:         full output price
    #
    # NOTE: input_tokens from the API is the count of tokens NOT served from
    # cache. cache_creation + cache_read are separate counts.
    # Total tokens processed = input_tokens + cache_creation + cache_read
    #
    # NOTE: This uses the 5-min write multiplier. If an agent uses 1h TTL,
    # the actual cost is higher (2x instead of 1.25x). The log gives an
    # approximate lower bound — check cache_creation_tokens to calculate
    # the exact cost if using 1h TTL.
    cost_usd = round(
        tokens_in * input_price                                    # Regular input
        + cache_creation * input_price * CACHE_WRITE_MULTIPLIER_5M # Cache write (1.25x)
        + cache_read * input_price * CACHE_READ_MULTIPLIER         # Cache read (0.1x)
        + tokens_out * output_price,                               # Output
        6,
    )

    # Log everything in one structured entry
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
            "stop_reason": response.stop_reason,
        },
    )

    return cost_usd
