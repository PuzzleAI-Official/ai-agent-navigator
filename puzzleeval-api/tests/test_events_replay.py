# ============================================================================
# Tests for GET /runs/{run_id}/events — history-replay broadcast bus
# ============================================================================
# Regression guard for TWO related bugs:
#
# 1. **Single-consumer queue split** — EventBus used to be a shared
#    queue.Queue where every event was consumed by exactly ONE
#    subscriber. When the frontend's EventSource reconnected (network
#    blip, React Strict Mode double-mount, page reload), the new
#    subscriber only received events emitted AFTER its subscribe
#    call; earlier events went to the dead first connection. Symptom:
#    SelectionPanel never rendered because `candidates_found` and
#    `selection_required` had already been consumed.
#
# 2. **No history / replay** — even with a broadcast bus, a fresh
#    page reload during the pause would start with an empty event
#    stream. The frontend's state map (`workflow`, `candidates`,
#    `stage`) stayed at defaults until the next live event.
#
# Fix: EventBus is now a broadcast bus with a shared history buffer.
# Every emit fans out to every current subscriber AND is appended to
# history. New subscribers are seeded with the full history before
# entering the live loop. Result: SelectionPanel renders correctly on
# every reload, reconnect, or Strict-Mode double-mount.
# ============================================================================

import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from main import app
from services.run_manager import run_manager, RunState


client = TestClient(app)


def _parse_sse_events(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event_type, data) tuples."""
    events: list[tuple[str, dict]] = []
    current_event: str | None = None
    for raw in text.splitlines():
        if raw.startswith("event:"):
            current_event = raw.split(":", 1)[1].strip()
        elif raw.startswith("data:") and current_event is not None:
            payload = raw.split(":", 1)[1].strip()
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = {}
            events.append((current_event, parsed))
            current_event = None
    return events


def test_events_history_replay_on_late_connect():
    """Emit events BEFORE subscribing, then subscribe. The subscriber
    must receive the full history in order — this is what lets the
    frontend rehydrate SelectionPanel on page reload during a pause.
    """
    state = RunState(run_id="hist-1", trace_id="trace-hist-1")
    state.event_bus.emit("workflow_blueprint", {
        "workflow": {"steps": [{"id": "step_1", "role": "voice_agent"}]},
        "test_plan": {},
    })
    state.event_bus.emit("candidates_found", {
        "candidates": [{"name": "OpenAI", "provider": "OpenAI"}]
    })
    state.event_bus.emit("selection_required", {
        "per_scope_candidates": {"step_1": ["OpenAI"]},
        "default_picks": {"step_1": ["OpenAI"]},
        "total_candidates": 1,
    })
    # Close so the SSE generator terminates after history replay.
    state.event_bus.close()
    run_manager._runs[state.run_id] = state

    try:
        response = client.get(f"/api/runs/{state.run_id}/events")
        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        types = [t for t, _ in events]
        # All three history events must be delivered, in emit order.
        assert "workflow_blueprint" in types, f"missing in {types}"
        assert "candidates_found" in types, f"missing in {types}"
        assert "selection_required" in types, f"missing in {types}"
        assert types.index("workflow_blueprint") < types.index("candidates_found") < types.index("selection_required")

        sel_payload = [p for t, p in events if t == "selection_required"][0]["data"]
        assert sel_payload["per_scope_candidates"] == {"step_1": ["OpenAI"]}
    finally:
        run_manager._runs.pop(state.run_id, None)


def test_events_broadcast_to_multiple_subscribers():
    """Two concurrent SSE connections both receive every event.

    Without broadcast semantics, a single queue.Queue would split events
    across the two subscribers and each would see roughly half. This
    guards the regression where the frontend's primary + reconnect
    EventSource competed for the same queue.
    """
    state = RunState(run_id="bcast-1", trace_id="trace-bcast-1")
    state.event_bus.emit("workflow_blueprint", {"workflow": {"steps": []}, "test_plan": {}})
    state.event_bus.emit("candidates_found", {"candidates": []})
    state.event_bus.close()
    run_manager._runs[state.run_id] = state

    try:
        r1 = client.get(f"/api/runs/{state.run_id}/events")
        r2 = client.get(f"/api/runs/{state.run_id}/events")
        assert r1.status_code == 200 and r2.status_code == 200
        events1 = [t for t, _ in _parse_sse_events(r1.text)]
        events2 = [t for t, _ in _parse_sse_events(r2.text)]
        # Both subscribers must see BOTH events — broadcast, not split.
        assert "workflow_blueprint" in events1 and "candidates_found" in events1
        assert "workflow_blueprint" in events2 and "candidates_found" in events2
    finally:
        run_manager._runs.pop(state.run_id, None)


def test_events_404_on_unknown_run():
    response = client.get("/api/runs/does-not-exist/events")
    assert response.status_code == 404


def test_events_order_preserved():
    """Events must be delivered in the exact order they were emitted.

    Order matters for the frontend: `workflow_blueprint` must arrive
    before `selection_required` because SelectionPanel's render
    condition is `stage === "selection" && workflow` — reversing the
    order would fire a setStage("selection") before `workflow` is
    populated, and the panel would briefly fail to render.
    """
    state = RunState(run_id="order-1", trace_id="trace-order-1")
    state.event_bus.emit("pipeline_started", {"trace_id": "x"})
    state.event_bus.emit("agent_started", {"agent": "agent_1"})
    state.event_bus.emit("workflow_blueprint", {"workflow": {"steps": []}, "test_plan": {}})
    state.event_bus.emit("agent_started", {"agent": "agent_2"})
    state.event_bus.emit("candidates_found", {"candidates": []})
    state.event_bus.emit("selection_required", {
        "per_scope_candidates": {}, "default_picks": {}, "total_candidates": 0,
    })
    state.event_bus.close()
    run_manager._runs[state.run_id] = state

    try:
        response = client.get(f"/api/runs/{state.run_id}/events")
        events = _parse_sse_events(response.text)
        names = [t for t, _ in events]
        expected_order = [
            "pipeline_started",
            "agent_started",
            "workflow_blueprint",
            "agent_started",
            "candidates_found",
            "selection_required",
        ]
        assert names[:len(expected_order)] == expected_order
    finally:
        run_manager._runs.pop(state.run_id, None)


def test_audio_route_serves_files_under_allowed_runs_root(tmp_path, monkeypatch):
    """Regression for the frontend audio playback bug where
    ``GET /runs/audio?path=...`` returned 404 "Run not found" because
    FastAPI was matching the ``/runs/{run_id}`` catch-all first.

    Fix validated here: a real audio file under the allowed runs tree
    streams back with ``audio/mpeg`` content-type, not an error body.
    """
    # Use an extra root so we can point it at tmp_path deterministically.
    fake_audio = tmp_path / "some-run" / "harnesses" / "cand" / "voice" / "caller-t0.mp3"
    fake_audio.parent.mkdir(parents=True)
    # Minimal valid-ish MP3 header so FileResponse serves bytes.
    fake_audio.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)

    monkeypatch.setenv("PUZZLEEVAL_EXTRA_RUNS_ROOTS", str(tmp_path))

    response = client.get(
        "/api/runs/audio",
        params={"path": str(fake_audio)},
    )
    assert response.status_code == 200, (
        f"Audio route must return 200 for files under allowed roots. "
        f"Got {response.status_code}: {response.text[:200]}"
    )
    assert response.headers["content-type"].startswith("audio/"), response.headers
    # Must NOT have been matched by /runs/{run_id} handler.
    assert b"Run not found" not in response.content


def test_audio_route_rejects_paths_outside_allowed_roots(tmp_path):
    """Containment check — ``/runs/audio?path=/etc/passwd`` must 403."""
    outside = tmp_path / "escape.mp3"
    outside.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 10)
    # Do NOT set PUZZLEEVAL_EXTRA_RUNS_ROOTS so tmp_path is NOT in the
    # allowlist.
    response = client.get("/api/runs/audio", params={"path": str(outside)})
    assert response.status_code == 403, (
        f"Audio route must reject paths outside allowed roots. "
        f"Got {response.status_code}: {response.text[:200]}"
    )


def test_emit_after_close_is_silently_dropped():
    """Closed bus doesn't crash on further emits; new subscribers see
    whatever was already in history plus the terminator."""
    state = RunState(run_id="closed-1", trace_id="trace-closed-1")
    state.event_bus.emit("before_close", {"x": 1})
    state.event_bus.close()
    state.event_bus.emit("after_close", {"x": 2})  # must not raise
    run_manager._runs[state.run_id] = state

    try:
        response = client.get(f"/api/runs/{state.run_id}/events")
        events = _parse_sse_events(response.text)
        names = [t for t, _ in events]
        assert "before_close" in names
        assert "after_close" not in names
    finally:
        run_manager._runs.pop(state.run_id, None)
