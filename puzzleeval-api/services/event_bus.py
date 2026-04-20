import asyncio
import json
import queue
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class SSEEvent:
    event_type: str
    data: dict[str, Any]

    def format(self) -> str:
        return json.dumps({"type": self.event_type, "data": self.data})


class EventBus:
    """Per-run broadcast event bus for SSE streaming.

    **Critical semantics change (fixes silent-split bug):** Earlier this
    class was a single ``queue.Queue`` shared across all subscribers —
    which meant every emitted event was consumed by exactly ONE subscriber
    (whichever called ``get()`` first). When the frontend's EventSource
    reconnected (network blip, React Strict Mode double-mount, page
    reload) two GET /events requests raced on the same queue and the
    events got split between them. Result: the frontend's active
    connection would receive only half the events and state handlers
    for ``candidates_found`` / ``selection_required`` silently dropped,
    leaving the SelectionPanel unable to render even though the backend
    was paused correctly.

    The fix: EventBus now maintains a per-subscriber queue plus a
    shared history buffer. Every emit is broadcast to every currently
    subscribed queue, AND appended to history. New subscribers are
    seeded with the full history so they get a complete replay on
    connect. ``queue.Queue`` is kept (not asyncio.Queue) because
    Agent 5's progress callbacks still fire from ThreadPoolExecutor
    threads and need thread-safe push.
    """

    def __init__(self):
        self._subscribers: list[queue.Queue[SSEEvent | None]] = []
        self._history: list[SSEEvent] = []
        self._closed = False
        self._lock = threading.Lock()

    def emit(self, event_type: str, data: dict[str, Any] | None = None):
        """Thread-safe: append to history and fan out to all subscribers."""
        if self._closed:
            return
        event = SSEEvent(event_type, data or {})
        with self._lock:
            self._history.append(event)
            subs_snapshot = list(self._subscribers)
        for sub_q in subs_snapshot:
            try:
                sub_q.put_nowait(event)
            except Exception:
                # A dead subscriber queue should never break emit —
                # unsubscription is handled when subscribe() exits.
                pass

    def close(self):
        """Signal end of stream to every subscriber."""
        with self._lock:
            self._closed = True
            subs_snapshot = list(self._subscribers)
        for sub_q in subs_snapshot:
            try:
                sub_q.put_nowait(None)
            except Exception:
                pass

    async def subscribe(self):
        """Async generator yielding every event emitted since the run
        started, plus every future event until close() is called.

        History replay first — so a late-connect client catches up on
        everything it missed before streaming live events. Each
        subscriber has its own queue so broadcast fan-out never splits
        events across connections.
        """
        sub_q: queue.Queue[SSEEvent | None] = queue.Queue()
        with self._lock:
            # Seed with full history so the new subscriber gets the
            # complete story. This is what makes SelectionPanel work
            # across page reloads + SSE reconnects: all the state-
            # setting events (workflow_blueprint, candidates_found,
            # selection_required) are delivered even when the original
            # emits happened long before this subscribe() call.
            for past in self._history:
                sub_q.put_nowait(past)
            if self._closed:
                sub_q.put_nowait(None)
            self._subscribers.append(sub_q)
        try:
            while True:
                # Use to_thread to avoid blocking the event loop on queue.get()
                event = await asyncio.to_thread(sub_q.get)
                if event is None:
                    break
                yield event
        finally:
            with self._lock:
                try:
                    self._subscribers.remove(sub_q)
                except ValueError:
                    pass
