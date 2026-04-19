# ============================================================================
# Tests for GET /runs/{run_id}/events — selection_required replay
# ============================================================================
# Regression guard for the Phase-6 race where a page reload or SSE
# reconnect during the candidate-selection pause meant the original
# `selection_required` SSE event was gone (EventBus is a no-replay
# queue.Queue) and the frontend's SelectionPanel never rendered — the
# user saw "No progress for 2m — pipeline may be stuck" even though
# the backend was genuinely paused at `awaiting_candidate_selection`.
#
# Fix: cache the selection payload on `RunState.pending_selection_payload`
# when the pause begins, clear it when selection is applied. The
# /events endpoint replays it as a synthetic SSE event to any
# subscriber that connects while the run is still paused.
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
    """Parse an SSE body into (event_type, data) tuples.

    Only looks at ``event:`` + ``data:`` pairs; ignores pings /
    heartbeats. Stops at the first empty event so tests don't hang
    waiting for the stream to close.
    """
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


def test_events_replay_selection_required_when_paused():
    """A subscriber connecting while run is awaiting_selection gets the payload.

    Simulates the "page reload during selection pause" race: backend has
    already emitted `selection_required` and paused; the original
    subscriber is gone. A fresh GET /events call MUST receive a
    replayed `selection_required` event so the frontend can restore
    SelectionPanel state.
    """
    state = RunState(
        run_id="replay-test-1",
        trace_id="trace-replay-1",
        status="awaiting_candidate_selection",
    )
    state.pending_selection_payload = {
        "per_scope_candidates": {"step_1": ["OpenAI", "ElevenLabs"]},
        "default_picks": {"step_1": ["OpenAI"]},
        "total_candidates": 2,
    }
    # Close the event bus so the subscribe() loop terminates cleanly
    # once the replay has been yielded. Without this the async
    # generator would park on queue.get() forever.
    state.event_bus.close()
    run_manager._runs[state.run_id] = state

    try:
        # TestClient streams the SSE body synchronously until the stream
        # completes — EventBus.close() drains it right after the replay.
        response = client.get(f"/api/runs/{state.run_id}/events")
        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        sel_events = [e for e in events if e[0] == "selection_required"]
        assert len(sel_events) == 1, (
            f"Expected exactly one replayed selection_required, got {len(sel_events)} "
            f"in events {events}"
        )
        payload = sel_events[0][1].get("data", {})
        assert payload["per_scope_candidates"] == {"step_1": ["OpenAI", "ElevenLabs"]}
        assert payload["default_picks"] == {"step_1": ["OpenAI"]}
        assert payload["total_candidates"] == 2
    finally:
        run_manager._runs.pop(state.run_id, None)


def test_events_no_replay_when_run_is_not_paused():
    """When run isn't at awaiting_selection, no synthetic replay fires.

    Guards against stale replays — e.g. if pending_selection_payload
    gets left on state by accident after the pipeline resumed, we should
    NOT spam new subscribers with a stale selection_required.
    """
    state = RunState(
        run_id="replay-test-2",
        trace_id="trace-replay-2",
        status="pipeline_running",  # post-selection, normal running
    )
    # Deliberately leave a stale payload to prove the status gate works.
    state.pending_selection_payload = {
        "per_scope_candidates": {"step_1": ["Stale"]},
        "default_picks": {},
        "total_candidates": 1,
    }
    state.event_bus.close()
    run_manager._runs[state.run_id] = state

    try:
        response = client.get(f"/api/runs/{state.run_id}/events")
        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        sel_events = [e for e in events if e[0] == "selection_required"]
        assert sel_events == [], (
            f"Run is pipeline_running; replay must NOT fire. Got: {sel_events}"
        )
    finally:
        run_manager._runs.pop(state.run_id, None)


def test_events_no_replay_when_payload_cleared():
    """After selection is applied, payload is cleared and no replay fires."""
    state = RunState(
        run_id="replay-test-3",
        trace_id="trace-replay-3",
        status="awaiting_candidate_selection",
    )
    state.pending_selection_payload = None  # cleared as if selection applied
    state.event_bus.close()
    run_manager._runs[state.run_id] = state

    try:
        response = client.get(f"/api/runs/{state.run_id}/events")
        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        sel_events = [e for e in events if e[0] == "selection_required"]
        assert sel_events == [], (
            f"Payload is None; replay must NOT fire. Got: {sel_events}"
        )
    finally:
        run_manager._runs.pop(state.run_id, None)


def test_events_404_on_unknown_run():
    response = client.get("/api/runs/does-not-exist/events")
    assert response.status_code == 404


def test_events_replay_workflow_and_candidates():
    """Late subscriber gets workflow_blueprint + candidates_found + selection_required
    in order so the frontend can hydrate Playground state top-to-bottom.

    SelectionPanel's render condition is
    ``stage === "selection" && workflow`` — without the workflow replay
    the panel silently refuses to render even though the stage is set
    correctly. Same for CandidateCard which needs `candidates` state.
    """
    state = RunState(
        run_id="replay-test-4",
        trace_id="trace-replay-4",
        status="awaiting_candidate_selection",
    )
    state.cached_workflow_blueprint = {
        "workflow": {"steps": [{"id": "step_1", "role": "voice_agent"}]},
        "test_plan": {"scope_specs": []},
    }
    state.cached_candidates_payload = {
        "candidates": [
            {"name": "OpenAI", "provider": "OpenAI", "covers_step_ids": ["step_1"]},
            {"name": "ElevenLabs", "provider": "ElevenLabs", "covers_step_ids": ["step_1"]},
        ]
    }
    state.pending_selection_payload = {
        "per_scope_candidates": {"step_1": ["OpenAI", "ElevenLabs"]},
        "default_picks": {"step_1": ["OpenAI"]},
        "total_candidates": 2,
    }
    state.event_bus.close()
    run_manager._runs[state.run_id] = state

    try:
        response = client.get(f"/api/runs/{state.run_id}/events")
        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        event_types = [e[0] for e in events]
        # Order matters: workflow first, then candidates, then selection.
        assert event_types[:3] == [
            "workflow_blueprint",
            "candidates_found",
            "selection_required",
        ], f"Replay order wrong: {event_types}"
    finally:
        run_manager._runs.pop(state.run_id, None)
