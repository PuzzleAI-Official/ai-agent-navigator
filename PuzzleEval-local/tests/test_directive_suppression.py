"""Tests for ``puzzleeval.agents.agent5.runtime_state.evaluate_agent_phase_agreement``.

PR 3 of the autonomy plan: the agreement check between orchestrator
truth and agent observation is the load-bearing primitive for demoting
prompt-injection state-management to a backstop.

Verdict ladder:
    * ``agreed``         — agent's most recent phase observation matches
                           the orchestrator's current phase.
    * ``disagreed``      — agent's most recent phase observation
                           explicitly mentions a DIFFERENT phase.
    * ``no_observation`` — file missing, or no entry with category=phase,
                           or the note doesn't mention any recognizable
                           phase keyword (defensive default — orchestrator
                           fires the directive when unsure).

Recognized observation shapes:
    * ``{"category": "phase", "phase": "phase_2_build", "note": "..."}``
      (preferred — structured ``phase`` field is authoritative)
    * ``{"category": "phase", "note": "I'm in phase 2 now"}``
      (fallback — note text is parsed for phase aliases)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from puzzleeval.agents.agent5 import runtime_state as rt


def _seed_observations(tmp_path: Path, observations: list[dict]) -> None:
    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "agent_observations.json").write_text(
        json.dumps({"observations": observations}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Missing file / structurally-invalid observations
# ---------------------------------------------------------------------------


class TestNoObservation:

    def test_missing_file_returns_no_observation(self, tmp_path: Path):
        verdict, note = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "no_observation"
        assert note is None

    def test_empty_observations_list_returns_no_observation(self, tmp_path: Path):
        _seed_observations(tmp_path, [])
        verdict, note = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "no_observation"
        assert note is None

    def test_observations_without_phase_category_return_no_observation(self, tmp_path: Path):
        _seed_observations(tmp_path, [
            {"turn": 1, "category": "decision", "note": "using websockets-client"},
            {"turn": 2, "category": "decision", "note": "input shape decided"},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "no_observation"

    def test_corrupt_json_returns_no_observation(self, tmp_path: Path):
        state_dir = tmp_path / "_agent_state"
        state_dir.mkdir()
        (state_dir / "agent_observations.json").write_text("{not valid", encoding="utf-8")
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "no_observation"


# ---------------------------------------------------------------------------
# Structured ``phase`` field — preferred shape, authoritative
# ---------------------------------------------------------------------------


class TestStructuredPhaseField:

    def test_matching_phase_field_returns_agreed(self, tmp_path: Path):
        _seed_observations(tmp_path, [
            {"turn": 5, "category": "phase", "phase": "phase_2_build",
             "note": "api_spec.txt exists; entering build"},
        ])
        verdict, note = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "agreed"
        assert "build" in note

    def test_mismatching_phase_field_returns_disagreed(self, tmp_path: Path):
        _seed_observations(tmp_path, [
            {"turn": 1, "category": "phase", "phase": "phase_1_research", "note": "still researching"},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "disagreed"

    def test_most_recent_observation_wins(self, tmp_path: Path):
        _seed_observations(tmp_path, [
            {"turn": 1, "category": "phase", "phase": "phase_1_research", "note": "research"},
            {"turn": 5, "category": "phase", "phase": "phase_2_build", "note": "build"},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "agreed"

    def test_decision_entries_between_phase_entries_dont_disrupt_lookup(self, tmp_path: Path):
        _seed_observations(tmp_path, [
            {"turn": 1, "category": "phase", "phase": "phase_1_research", "note": "n"},
            {"turn": 2, "category": "decision", "note": "websockets-client"},
            {"turn": 4, "category": "phase", "phase": "phase_2_build", "note": "n"},
            {"turn": 5, "category": "decision", "note": "concurrency: per-call session"},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "agreed"


# ---------------------------------------------------------------------------
# Note-only observations — parser fallback
# ---------------------------------------------------------------------------


class TestNoteFallbackParsing:

    @pytest.mark.parametrize("note,target_phase", [
        ("I see api_spec.txt now; transitioning to phase 2 build", "phase_2_build"),
        ("phase_2_build", "phase_2_build"),
        ("Build phase started", "phase_2_build"),
        ("Currently in Phase 1 research", "phase_1_research"),
        ("Phase 3 verify checks running", "phase_3_verify"),
    ])
    def test_note_phrases_parse_to_agreed(self, tmp_path: Path, note: str, target_phase: str):
        _seed_observations(tmp_path, [
            {"turn": 1, "category": "phase", "note": note},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, target_phase)
        assert verdict == "agreed", (
            f"Expected agreement for note={note!r} vs phase={target_phase}"
        )

    def test_note_mentioning_different_phase_returns_disagreed(self, tmp_path: Path):
        _seed_observations(tmp_path, [
            {"turn": 1, "category": "phase", "note": "still in phase 1 research"},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "disagreed"

    def test_note_mentioning_no_phase_returns_no_observation(self, tmp_path: Path):
        _seed_observations(tmp_path, [
            {"turn": 1, "category": "phase", "note": "the SDK uses bearer auth"},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "no_observation"


# ---------------------------------------------------------------------------
# Defensive coverage
# ---------------------------------------------------------------------------


class TestDefensiveDefaults:

    def test_unknown_orchestrator_phase_does_not_crash(self, tmp_path: Path):
        # Future-proof: if the orchestrator's phase enum is extended,
        # the helper should gracefully return no_observation rather than
        # raising or false-agreeing.
        _seed_observations(tmp_path, [
            {"turn": 1, "category": "phase", "note": "in phase 2"},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_99_unknown")
        assert verdict == "no_observation"

    def test_observations_with_non_dict_entry_skipped(self, tmp_path: Path):
        # Defense against malformed JSON the agent might write.
        _seed_observations(tmp_path, [
            "not a dict",
            ["also not a dict"],
            {"turn": 5, "category": "phase", "phase": "phase_2_build", "note": "ok"},
        ])
        verdict, _ = rt.evaluate_agent_phase_agreement(tmp_path, "phase_2_build")
        assert verdict == "agreed"
