"""Time tracking — context manager + Timer object.

Standardized way to capture elapsed wall-clock time around a block.
Replaces the ad-hoc ``start = time.time(); ...; latency_ms = ...``
pattern scattered through agents.

Usage:

    with track_time("agent_5_build_turn") as timer:
        response = client.beta.messages.create(...)
    # timer.elapsed_ms now holds the wall-clock duration

The ``operation`` arg labels the timer for telemetry — when integrated
with ``record_llm_call``, the operation name surfaces as a metric label.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass
class Timer:
    """Wall-clock timer.

    Created by ``track_time``. Inside the context, ``elapsed_ms`` is 0
    (the timer hasn't stopped yet); after exiting, ``elapsed_ms`` holds
    the final duration in milliseconds.

    The timer also exposes ``elapsed_seconds`` for callers that need
    fractional seconds without doing the division themselves.
    """

    operation: str
    start_time: float
    end_time: float | None = None

    @property
    def elapsed_ms(self) -> float:
        """Elapsed time in milliseconds (0 if timer hasn't stopped yet).

        Computed from start_time → end_time once the context exits.
        Inside the context, returns elapsed-so-far for live tracking.
        """
        end = self.end_time if self.end_time is not None else time.time()
        return round((end - self.start_time) * 1000, 2)

    @property
    def elapsed_seconds(self) -> float:
        """Elapsed time in seconds."""
        return round(self.elapsed_ms / 1000, 4)

    @property
    def stopped(self) -> bool:
        """True if the context has exited."""
        return self.end_time is not None


@contextmanager
def track_time(operation: str) -> Iterator[Timer]:
    """Context manager that captures wall-clock time around a block.

    Args:
        operation: Label for the timer. Surfaces in telemetry metric
                   labels and structured logs.

    Yields:
        Timer object. Read ``timer.elapsed_ms`` after the block exits.
    """
    timer = Timer(operation=operation, start_time=time.time())
    try:
        yield timer
    finally:
        timer.end_time = time.time()


__all__ = ["Timer", "track_time"]
