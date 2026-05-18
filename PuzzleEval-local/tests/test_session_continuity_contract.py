"""Regression guards for Phase 5 voice continuity policy.

The voice playbook used to prescribe a specific session_state recipe. Phase 5
keeps continuity as an outcome contract and lets implementation_plan.json own
the state-owner decision. These tests pin that boundary instead of re-freezing
the old recipe.
"""

from __future__ import annotations


def _contract() -> str:
    from puzzleeval.agents.implement_test_env import _VOICE_HARNESS_CONTRACT

    return _VOICE_HARNESS_CONTRACT


def test_voice_contract_assigns_continuity_to_state_owner_not_recipe():
    contract = _contract()

    assert "implementation plan declares who owns conversational state" in contract.lower()
    assert "provider_server" in contract
    assert "harness_process" in contract
    assert "persistent_worker" in contract
    assert "production-equivalent" in contract


def test_voice_contract_defines_output_audio_evidence_only():
    contract = _contract()

    assert "raw_response.audio_bytes" in contract
    assert "raw_response.audio_path" in contract
    assert "transcript" in contract.lower()
    assert "text alone is not audio evidence" in contract.lower()


def test_voice_contract_keeps_legacy_audio_aliases_forbidden():
    contract = _contract()

    for forbidden in ("audio_url", "audio_data", "audio_file"):
        assert forbidden in contract
    assert "do not satisfy the contract" in contract.lower()


def test_voice_contract_no_longer_teaches_session_state_how_to():
    contract = _contract()

    forbidden_recipe_phrases = (
        "HARNESS owns provider continuity",
        "plugin handles session_state threading",
        "turn_index=0",
        "turn_index>0",
        "fresh WebSocket",
        "open WebSocket once",
        "store handle in session_state",
        "REUSE on turn 1",
        "introduces itself every turn",
    )
    for phrase in forbidden_recipe_phrases:
        assert phrase not in contract


def test_voice_contract_preserves_reportable_continuity_evidence():
    contract = _contract().lower()

    assert "per-turn agent audio" in contract
    assert "multi-turn transcripts" in contract
    assert "session creation on the first turn" in contract
    assert "reuse" in contract or "continuity evidence" in contract
