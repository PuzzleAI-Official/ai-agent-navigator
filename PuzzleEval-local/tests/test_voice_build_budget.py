"""Tests for voice-modality build budget bumps.

When a candidate's test cases use a voice modality (voice_conversation,
voice_turn, audio_content), Agent 5 uses higher turn + dollar caps:
  * AGENT5_MAX_TURNS_VOICE (default 65) instead of AGENT5_MAX_TURNS (40)
  * AGENT5_MAX_BUDGET_PER_CANDIDATE_VOICE ($5) instead of $3

Voice harnesses are intrinsically harder than REST (multi-turn WebSocket
state, async events, real-time TTS/STT) and benefit from extra debug
headroom. Real-run trace 6e0c9563 had ElevenLabs abandon a deeper fix
at turn 13/40 — this is the documented exception to AD-001/AD-003 because
budget is meta-control over the agent itself, not modality behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Config flags exist + have correct defaults
# ---------------------------------------------------------------------------


class TestConfigFlags:
    def test_voice_max_turns_default_65(self):
        from puzzleeval.config import AGENT5_MAX_TURNS_VOICE
        assert AGENT5_MAX_TURNS_VOICE == 65

    def test_voice_budget_default_5_usd(self):
        from puzzleeval.config import AGENT5_MAX_BUDGET_PER_CANDIDATE_VOICE
        assert AGENT5_MAX_BUDGET_PER_CANDIDATE_VOICE == 5.0

    def test_voice_caps_higher_than_default(self):
        """Sanity: voice caps must be >= the non-voice defaults so the
        bump is meaningful. If they're ever flipped, this test fails."""
        from puzzleeval.config import (
            AGENT5_MAX_TURNS,
            AGENT5_MAX_TURNS_VOICE,
            AGENT5_MAX_BUDGET_PER_CANDIDATE,
            AGENT5_MAX_BUDGET_PER_CANDIDATE_VOICE,
        )
        assert AGENT5_MAX_TURNS_VOICE > AGENT5_MAX_TURNS
        assert AGENT5_MAX_BUDGET_PER_CANDIDATE_VOICE > AGENT5_MAX_BUDGET_PER_CANDIDATE


# ---------------------------------------------------------------------------
# Voice-modality detection predicate (exercised inline in build_loop)
# ---------------------------------------------------------------------------


class TestVoiceModalityDetection:
    def test_voice_modalities_frozenset_is_canonical(self):
        from puzzleeval.agents.agent5.playbooks import VOICE_MODALITIES
        # Single source of truth — must contain at least these three
        assert "voice_conversation" in VOICE_MODALITIES
        assert "voice_turn" in VOICE_MODALITIES
        assert "audio_content" in VOICE_MODALITIES

    @pytest.mark.parametrize("input_type,output_type,expected", [
        ("voice_conversation", "voice_conversation", True),
        ("voice_turn", "audio_content", True),
        ("text", "voice_conversation", True),  # output is voice
        ("voice_turn", "free_text", True),     # input is voice
        ("text", "free_text", False),
        ("document_content", "structured_json", False),
        ("conversation", "free_text", False),  # text chat, NOT voice
    ])
    def test_predicate_matches_input_or_output(self, input_type, output_type, expected):
        """The predicate (used inline in build_loop) returns True if
        EITHER the test case's input_type OR output_type is a voice
        modality. Mirrors the actual logic at line 552 of build_loop."""
        from puzzleeval.agents.agent5.playbooks import VOICE_MODALITIES
        tc = SimpleNamespace(input_type=input_type, output_type=output_type)
        is_voice = (
            (tc.input_type in VOICE_MODALITIES)
            or (tc.output_type in VOICE_MODALITIES)
        )
        assert is_voice is expected

    def test_any_voice_test_in_set_triggers_bump(self):
        """A mixed-modality candidate (some voice, some text test cases)
        still gets the voice bump as long as ANY test case is voice."""
        from puzzleeval.agents.agent5.playbooks import VOICE_MODALITIES
        test_cases = [
            SimpleNamespace(input_type="text", output_type="free_text"),
            SimpleNamespace(input_type="voice_conversation", output_type="voice_conversation"),
        ]
        is_voice_build = any(
            (getattr(tc, "input_type", None) in VOICE_MODALITIES)
            or (getattr(tc, "output_type", None) in VOICE_MODALITIES)
            for tc in test_cases
        )
        assert is_voice_build is True


# ---------------------------------------------------------------------------
# Env-var override path
# ---------------------------------------------------------------------------


class TestEnvVarOverride:
    def test_max_turns_voice_overridable(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_AGENT5_MAX_TURNS_VOICE", "100")
        import importlib, puzzleeval.config
        importlib.reload(puzzleeval.config)
        assert puzzleeval.config.AGENT5_MAX_TURNS_VOICE == 100

    def test_budget_voice_overridable(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_AGENT5_BUDGET_PER_CANDIDATE_VOICE", "8.5")
        import importlib, puzzleeval.config
        importlib.reload(puzzleeval.config)
        assert puzzleeval.config.AGENT5_MAX_BUDGET_PER_CANDIDATE_VOICE == 8.5
