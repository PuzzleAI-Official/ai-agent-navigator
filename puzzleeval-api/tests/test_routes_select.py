# ============================================================================
# Tests for POST /runs/{run_id}/select-candidates (Phase 6)
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_routes_select.py -v
#
# Tests the Phase 6 select-candidates endpoint validation: correct status
# required, at least one scope has picks, scope_ids match blueprint,
# user-added candidates validated, double-submit rejected, unknown
# candidate names rejected, and the success path.
# ============================================================================

import asyncio
import pytest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient


# We need to set ANTHROPIC_API_KEY before importing the app
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from main import app
from services.run_manager import run_manager, RunState


client = TestClient(app)


def _make_run_in_selection_state(run_id: str = "test-sel") -> RunState:
    """Create a RunState parked in awaiting_candidate_selection with mock Agent 2 data."""
    state = RunState(
        run_id=run_id,
        trace_id="trace-sel",
        status="awaiting_candidate_selection",
        agent1_result={
            "is_clear": True,
            "result": {
                "workflow": {
                    "steps": [
                        {"id": "step_1", "role": "ocr", "description": "OCR"},
                        {"id": "step_2", "role": "sync", "description": "Sync"},
                    ],
                    "architecture_options": ["all_in_one", "best_per_step"],
                    "notes": "test",
                },
                "sub_tasks": [],
                "summary": "test",
                "domain": "test",
                "search_keywords": [],
                "constraints": {},
            },
        },
        agent2_result={
            "candidates": [
                {"name": "Mindee", "provider": "Mindee", "covers_step_ids": ["step_1"], "coverage_confidence": {"step_1": "claimed"}},
                {"name": "Zapier", "provider": "Zapier", "covers_step_ids": ["step_1", "step_2"], "coverage_confidence": {"step_1": "claimed", "step_2": "claimed"}},
                {"name": "Make", "provider": "Make", "covers_step_ids": ["step_2"], "coverage_confidence": {"step_2": "claimed"}},
            ],
            "search_approach": "test",
            "coverage_notes": "test",
        },
    )
    run_manager._runs[run_id] = state
    return state


def _cleanup(run_id: str = "test-sel"):
    run_manager._runs.pop(run_id, None)


class TestSelectCandidatesRoute:

    def test_run_not_found_returns_404(self):
        resp = client.post("/api/runs/nonexistent/select-candidates", json={
            "scope_picks": {"step_1": ["Mindee"]},
        })
        assert resp.status_code == 404

    def test_wrong_status_returns_400(self):
        state = _make_run_in_selection_state("wrong-status")
        state.status = "pipeline_running"  # not awaiting selection
        try:
            resp = client.post("/api/runs/wrong-status/select-candidates", json={
                "scope_picks": {"step_1": ["Mindee"]},
            })
            assert resp.status_code == 400
            assert "not awaiting selection" in resp.json()["detail"]
        finally:
            _cleanup("wrong-status")

    def test_double_submit_returns_400(self):
        state = _make_run_in_selection_state("double-submit")
        state.selection_ready.set()  # already submitted
        try:
            resp = client.post("/api/runs/double-submit/select-candidates", json={
                "scope_picks": {"step_1": ["Mindee"]},
            })
            assert resp.status_code == 400
            assert "already submitted" in resp.json()["detail"]
        finally:
            _cleanup("double-submit")

    def test_empty_scope_picks_returns_400(self):
        _make_run_in_selection_state("empty-picks")
        try:
            resp = client.post("/api/runs/empty-picks/select-candidates", json={
                "scope_picks": {},
            })
            assert resp.status_code == 400
            assert "least one scope" in resp.json()["detail"]
        finally:
            _cleanup("empty-picks")

    def test_unknown_scope_id_returns_400(self):
        _make_run_in_selection_state("unknown-scope")
        try:
            resp = client.post("/api/runs/unknown-scope/select-candidates", json={
                "scope_picks": {"step_99": ["Mindee"]},
            })
            assert resp.status_code == 400
            assert "Unknown scope_id" in resp.json()["detail"]
        finally:
            _cleanup("unknown-scope")

    def test_user_added_unknown_scope_returns_400(self):
        _make_run_in_selection_state("ua-bad-scope")
        try:
            resp = client.post("/api/runs/ua-bad-scope/select-candidates", json={
                "scope_picks": {"step_1": ["Mindee"]},
                "add": [{
                    "name": "CustomAPI",
                    "provider": "Custom",
                    "covers_step_ids": ["step_99"],  # not in blueprint
                }],
            })
            assert resp.status_code == 400
            assert "step_99" in resp.json()["detail"]
        finally:
            _cleanup("ua-bad-scope")

    def test_unknown_candidate_name_returns_400(self):
        _make_run_in_selection_state("unknown-cand")
        try:
            resp = client.post("/api/runs/unknown-cand/select-candidates", json={
                "scope_picks": {"step_1": ["NonexistentTool"]},
            })
            assert resp.status_code == 400
            assert "NonexistentTool" in resp.json()["detail"]
        finally:
            _cleanup("unknown-cand")

    def test_valid_picks_return_200_and_set_ready(self):
        state = _make_run_in_selection_state("valid-picks")
        try:
            resp = client.post("/api/runs/valid-picks/select-candidates", json={
                "scope_picks": {
                    "step_1": ["Mindee", "Zapier"],
                    "step_2": ["Zapier", "Make"],
                },
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["accepted_count"] == 4  # 2 at step_1 + 2 at step_2
            assert body["scope_coverage"]["step_1"] == 2
            assert body["scope_coverage"]["step_2"] == 2
            # selection_ready should be set so pipeline resumes
            assert state.selection_ready.is_set()
            assert state.user_scope_picks == {
                "step_1": ["Mindee", "Zapier"],
                "step_2": ["Zapier", "Make"],
            }
        finally:
            _cleanup("valid-picks")

    def test_same_candidate_at_multiple_scopes_accepted(self):
        state = _make_run_in_selection_state("multi-scope")
        try:
            resp = client.post("/api/runs/multi-scope/select-candidates", json={
                "scope_picks": {
                    "step_1": ["Zapier"],
                    "step_2": ["Zapier"],
                },
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["accepted_count"] == 2  # Zapier at each scope = 2 pairs
            assert state.selection_ready.is_set()
        finally:
            _cleanup("multi-scope")

    def test_user_added_candidate_flows_through(self):
        state = _make_run_in_selection_state("with-ua")
        try:
            resp = client.post("/api/runs/with-ua/select-candidates", json={
                "scope_picks": {
                    "step_1": ["Mindee", "MyCustomOCR"],  # includes user-added name
                    "step_2": ["Make"],
                },
                "add": [{
                    "name": "MyCustomOCR",
                    "provider": "Custom Inc",
                    "api_docs_url": "https://custom.io/api",
                    "notes": "Our internal OCR service",
                    "covers_step_ids": ["step_1"],
                }],
            })
            assert resp.status_code == 200
            assert state.selection_ready.is_set()
            assert len(state.user_added_candidates) == 1
            assert state.user_added_candidates[0]["name"] == "MyCustomOCR"
        finally:
            _cleanup("with-ua")

    def test_user_added_with_empty_covers_returns_400(self):
        _make_run_in_selection_state("ua-no-covers")
        try:
            resp = client.post("/api/runs/ua-no-covers/select-candidates", json={
                "scope_picks": {"step_1": ["Mindee"]},
                "add": [{
                    "name": "BadAPI",
                    "provider": "Bad",
                    "covers_step_ids": [],  # empty!
                }],
            })
            assert resp.status_code == 400
            assert "no covers_step_ids" in resp.json()["detail"]
        finally:
            _cleanup("ua-no-covers")
