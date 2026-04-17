import asyncio
import json
import queue
from dataclasses import dataclass
from typing import Any


@dataclass
class SSEEvent:
    event_type: str
    data: dict[str, Any]

    def format(self) -> str:
        return json.dumps({"type": self.event_type, "data": self.data})


class EventBus:
    """Per-run event queue for SSE streaming.

    Uses stdlib queue.Queue (thread-safe) instead of asyncio.Queue
    because Agent 5's progress callbacks fire from ThreadPoolExecutor threads.
    """

    def __init__(self):
        self._queue: queue.Queue[SSEEvent | None] = queue.Queue()
        self._closed = False

    def emit(self, event_type: str, data: dict[str, Any] | None = None):
        """Thread-safe: can be called from any thread (including ThreadPoolExecutor)."""
        if not self._closed:
            self._queue.put_nowait(SSEEvent(event_type, data or {}))

    def close(self):
        """Signal end of stream."""
        self._closed = True
        self._queue.put_nowait(None)

    async def subscribe(self):
        """Async generator that yields events. Polls the thread-safe queue
        from the async event loop without blocking."""
        while True:
            # Use to_thread to avoid blocking the event loop on queue.get()
            event = await asyncio.to_thread(self._queue.get)
            if event is None:
                break
            yield event
