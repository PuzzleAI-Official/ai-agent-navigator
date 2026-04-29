"""DEPRECATED — use ``puzzleeval.telemetry.logging`` instead.

This module is a thin shim preserved for back-compat. The canonical
location for structured logging utilities is
``puzzleeval.telemetry.logging`` (renamed to avoid colliding with the
stdlib ``logging`` module — import as
``from puzzleeval.telemetry import logging as obs_logging`` if needed).

The public API is also re-exported from ``puzzleeval.telemetry`` directly:

    from puzzleeval.telemetry import (
        StructuredJsonFormatter,
        generate_trace_id,
        get_logger,
        log_llm_call,
        record_llm_call,
        setup_logging,
    )

Deprecation removal: 2026-10-27 (6 months after this shim landed).
"""

from __future__ import annotations

from puzzleeval.telemetry.logging import (
    LOG_LEVEL,
    LOG_OUTPUT_PATH,
    StructuredJsonFormatter,
    generate_trace_id,
    get_logger,
    log_llm_call,
    record_llm_call,
    setup_logging,
)

__all__ = [
    "LOG_LEVEL",
    "LOG_OUTPUT_PATH",
    "StructuredJsonFormatter",
    "generate_trace_id",
    "get_logger",
    "log_llm_call",
    "record_llm_call",
    "setup_logging",
]
