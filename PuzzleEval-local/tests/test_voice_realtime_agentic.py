"""Tests for voice_realtime.drive_conversation agentic-mode path.

Locks in:
  - Scripted-mode backward compatibility (no agentic kwargs → legacy path)
  - Auto-detect: persona+goal+rubric populated → agentic path taken
  - Hybrid mode runs BOTH paths and attaches scripted result
  - Fallback: agentic requested but missing persona/goal/rubric → scripted
  - Simulator crash is survivable (transcript still judged)
  - Return dict carries rubric_verdict + transcript + evaluation_mode keys
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
from puzzleeval.schemas import (
    ConversationTurn,
    Persona,
    RubricCriterion,
    RubricScore,
    RubricVerdict,
    SimulatorTurn,
)


def make_persona():
    return Persona(
        name="Maria",
        demographics="45yo homeowner",
        emotional_state="stressed",
    )


def make_rubric():
    return [
        RubricCriterion(
            name="goal_completion", description="booked?",
            weight=0.6, critical=False,
        ),
        RubricCriterion(
            name="accuracy", description="no hallucination",
            weight=0.4, critical=True, min_passing_score=0.5,
        ),
    ]


def make_responder_returning_text(agent_text: str):
    """Build an agent_responder callable that returns a fixed text
    response regardless of input."""
    def _responder(turn_idx, caller_url, state):
        return {"text": agent_text}
    return _responder


def _noop_synth(self, text, token):
    """Replace _synthesize_caller_audio so no TTS is hit during tests —
    returns a deterministic path string without writing anything."""
    return f"/tmp/fake_caller_{token}.wav"


def _noop_public_url(self, token):
    return f"http://localhost:0/audio/{token}"


def _noop_merge(self, *, session_token, turns):
    return None


@pytest.fixture
def plugin(monkeypatch):
    """VoiceRealtimePlugin with TTS + public URL + merge patched out —
    pure in-process testing of the drive loop."""
    p = VoiceRealtimePlugin()
    monkeypatch.setattr(
        VoiceRealtimePlugin, "_synthesize_caller_audio", _noop_synth,
    )
    monkeypatch.setattr(
        VoiceRealtimePlugin, "public_url_for_audio", _noop_public_url,
    )
    monkeypatch.setattr(
        VoiceRealtimePlugin, "_merge_conversation_audio", _noop_merge,
    )
    # Avoid starting the real HTTP server
    monkeypatch.setattr(
        VoiceRealtimePlugin, "artifacts_for_token_prefix",
        lambda self, token: [],
    )
    yield p
    p.clear()


# ---------------------------------------------------------------------------


class TestModeAutoDetection:
    """Default evaluation_mode='auto' — plugin picks based on provided fields."""

    def test_scripted_path_when_no_agentic_fields(self, plugin):
        """Back-compat: providing script WITHOUT persona/goal/rubric must
        route through the legacy scripted loop. Existing callers don't
        break."""
        result = plugin.drive_conversation(
            script=[{"user_text": "Hi", "expected_agent_contains": "hello"}],
            agent_responder=make_responder_returning_text("Hello there"),
            scope_role="voice",
            shape="generic",
        )
        assert result["evaluation_mode"] == "scripted"
        assert "rubric_verdict" not in result
        assert result["turns"][0]["passed"] is True
        assert result["turns"][0]["score"] == 1.0

    def test_agentic_path_when_all_fields_present(self, plugin):
        """persona + goal + rubric populated → auto-routes to agentic."""
        # Mock the simulator + judge so no API calls fire
        fake_sim_turn = SimulatorTurn(
            text="Hi, my water heater is leaking.",
            end_conversation=True,
            end_reason="goal_achieved",
            cost_usd=0.002,
        )
        fake_verdict = RubricVerdict(
            overall_score=0.85, passed=True,
            criterion_scores=[
                RubricScore(criterion_name="goal_completion", score=0.9, reasoning="quick"),
                RubricScore(criterion_name="accuracy", score=0.8, reasoning="ok"),
            ],
            conversation_summary="Quick booking.",
            cost_usd=0.015,
        )
        with patch(
            "puzzleeval.user_simulator.generate_next_user_turn",
            return_value=fake_sim_turn,
        ), patch(
            "puzzleeval.rubric_judge.judge_conversation",
            return_value=fake_verdict,
        ):
            result = plugin.drive_conversation(
                agent_responder=make_responder_returning_text("Sorry to hear."),
                persona=make_persona(),
                goal="book emergency appointment",
                constraints=[],
                rubric=make_rubric(),
                max_turns=4,
                trace_id="test-agentic-1",
            )
        assert result["evaluation_mode"] == "agentic"
        assert result["rubric_verdict"] is not None
        assert result["rubric_verdict"]["overall_score"] == 0.85
        assert result["transcript"]  # non-empty
        # The simulator ended on turn 0 with goal_achieved
        assert result["sim_end_reason"] == "goal_achieved"


class TestExplicitModeRequest:
    def test_agentic_requested_without_kit_falls_back_to_scripted(self, plugin, caplog):
        """Missing persona → warn + fall back to scripted. No crash."""
        import logging
        caplog.set_level(logging.WARNING)
        result = plugin.drive_conversation(
            script=[{"user_text": "Hi", "expected_agent_contains": "hi"}],
            agent_responder=make_responder_returning_text("hi"),
            evaluation_mode="agentic",
            # Missing persona/goal/rubric
        )
        assert result["evaluation_mode"] == "scripted"
        assert any(
            "persona/goal/rubric incomplete" in r.message
            for r in caplog.records
        )

    def test_hybrid_mode_no_longer_accepted(self, plugin):
        """Hybrid mode was dropped — evaluation_mode='hybrid' is rejected
        at the schema layer; plugins treat unknown values as scripted
        fallback. Lock this so hybrid can't silently reappear."""
        # Schema-layer rejection: Pydantic Literal blocks it.
        from puzzleeval.schemas import TestCase, JudgementCriterion
        import pytest as _pt
        with _pt.raises(Exception):
            TestCase(
                id="tc", sub_task_ref="x", scenario="s",
                input_type="voice_conversation",
                input_data="{}", output_type="voice_turn",
                expected_output="{}",
                judgement_criteria=[
                    JudgementCriterion(
                        criterion="c", eval_type="subjective_quality",
                        weight=1.0,
                    ),
                ],
                difficulty="medium", tags=["happy_path"],
                evaluation_mode="hybrid",
            )


class TestFailureSurvival:
    """The agentic path must survive simulator crashes and responder
    crashes without tearing down the run. The judge scores whatever
    transcript we collected."""

    def test_simulator_crash_still_yields_verdict(self, plugin):
        fake_verdict = RubricVerdict(
            overall_score=0.0, passed=False,
            criterion_scores=[],
            conversation_summary="No conversation happened.",
            cost_usd=0.0,
        )

        def _raising_sim(*args, **kwargs):
            raise RuntimeError("simulator died")

        with patch(
            "puzzleeval.user_simulator.generate_next_user_turn",
            side_effect=_raising_sim,
        ), patch(
            "puzzleeval.rubric_judge.judge_conversation",
            return_value=fake_verdict,
        ):
            result = plugin.drive_conversation(
                agent_responder=make_responder_returning_text("hi"),
                persona=make_persona(),
                goal="test",
                rubric=make_rubric(),
                max_turns=3,
                evaluation_mode="agentic",
            )
        # sim_end_reason was 'abandoned' due to crash
        assert result["sim_end_reason"] == "abandoned"
        # transcript is empty but the judge was still called
        assert result["rubric_verdict"] is not None

    def test_max_turns_cap_prevents_runaway(self, plugin):
        """Even if simulator never emits END_CALL, max_turns breaks the
        loop cleanly."""
        fake_sim_turn = SimulatorTurn(
            text="keep going",
            end_conversation=False,  # never ends
            end_reason="ongoing",
            cost_usd=0.002,
        )
        fake_verdict = RubricVerdict(
            overall_score=0.5, passed=False,
            criterion_scores=[],
            conversation_summary="capped",
            cost_usd=0.015,
        )
        with patch(
            "puzzleeval.user_simulator.generate_next_user_turn",
            return_value=fake_sim_turn,
        ), patch(
            "puzzleeval.rubric_judge.judge_conversation",
            return_value=fake_verdict,
        ):
            result = plugin.drive_conversation(
                agent_responder=make_responder_returning_text("reply"),
                persona=make_persona(),
                goal="test",
                rubric=make_rubric(),
                max_turns=3,  # hard cap
                evaluation_mode="agentic",
            )
        # Simulator returned 'ongoing' but plugin capped at max_turns
        assert result["sim_end_reason"] == "max_turns"
        # Turn count matches the cap
        assert len(result["turns"]) == 3
