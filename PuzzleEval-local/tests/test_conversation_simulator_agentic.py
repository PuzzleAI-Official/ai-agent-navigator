"""Tests for conversation_simulator.evaluate_output agentic-mode path.

Parallels the voice_realtime_agentic test suite but for text chatbot
conversations — no TTS/STT, pure text round-trips.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.tool_plugins import get_plugin
from puzzleeval.schemas import (
    Persona,
    RubricCriterion,
    RubricScore,
    RubricVerdict,
    SimulatorTurn,
)


def make_persona():
    return Persona(
        name="Maria",
        demographics="45yo customer",
        emotional_state="curious",
    )


def make_rubric():
    return [
        RubricCriterion(
            name="goal_completion", description="got answer?",
            weight=0.7, critical=False,
        ),
        RubricCriterion(
            name="accuracy", description="no wrong facts",
            weight=0.3, critical=True, min_passing_score=0.5,
        ),
    ]


def make_harness_runner(agent_response: str):
    """Build a harness_runner that returns `agent_response` as the output
    for any payload."""
    def _runner(payload):
        return {
            "success": True,
            "output": agent_response,
            "latency_ms": 100,
            "raw_response": {"choices": [{"message": {"content": agent_response}}]},
        }
    return _runner


@pytest.fixture
def plugin():
    return get_plugin("conversation_simulator")


class TestAutoDetection:
    def test_scripted_mode_when_no_agentic_fields(self, plugin):
        """Legacy script in `expected` — no agentic kwargs → scripted path.
        Existing conversation_simulator callers don't break."""
        script = {
            "conversation_script": {
                "user_turns": ["Hi", "Thanks"],
                "assertions": [
                    {"turn_index": 0, "check_type": "contains",
                     "value": "hello", "weight": 1.0},
                ],
            },
        }
        result = plugin.evaluate_output(
            response={},
            expected=script,
            harness_runner=make_harness_runner("Hello there!"),
        )
        # Scripted path: substring match hit
        assert result.passed is True
        assert result.detail["evaluation_mode"] == "scripted"
        assert "rubric_verdict" not in result.detail

    def test_agentic_mode_when_persona_goal_rubric_present(self, plugin):
        fake_sim_turn = SimulatorTurn(
            text="Hi, what are your hours?",
            end_conversation=True,
            end_reason="goal_achieved",
            cost_usd=0.002,
        )
        fake_verdict = RubricVerdict(
            overall_score=0.9, passed=True,
            criterion_scores=[
                RubricScore(criterion_name="goal_completion", score=0.95, reasoning=""),
                RubricScore(criterion_name="accuracy", score=0.8, reasoning=""),
            ],
            conversation_summary="Quick info exchange",
            cost_usd=0.015,
        )
        with patch(
            "puzzleeval.user_simulator.generate_next_user_turn",
            return_value=fake_sim_turn,
        ), patch(
            "puzzleeval.rubric_judge.judge_conversation",
            return_value=fake_verdict,
        ):
            result = plugin.evaluate_output(
                response={}, expected={},
                harness_runner=make_harness_runner("We're open 9-5"),
                persona=make_persona(),
                goal="get hours",
                rubric=make_rubric(),
                max_turns=4,
            )
        assert result.passed is True
        assert result.score == 0.9
        assert result.detail["evaluation_mode"] == "agentic"
        assert result.detail["rubric_verdict"] is not None
        assert result.detail["sim_end_reason"] == "goal_achieved"


class TestAgenticCriticalGate:
    def test_critical_failure_blocks_pass_even_with_high_overall(self, plugin):
        """End-to-end check: judge emits passed=True with no critical
        failures, but the deterministic post-processor finds a critical
        miss and vetoes."""
        fake_sim_turn = SimulatorTurn(
            text="", end_conversation=True, end_reason="goal_achieved", cost_usd=0.0,
        )
        # Judge returns passed=True but scores accuracy below 0.5 critical threshold
        fake_verdict = RubricVerdict(
            overall_score=0.73,
            passed=True,  # WRONG — finalize_verdict will override
            criterion_scores=[
                RubricScore(
                    criterion_name="goal_completion", score=1.0,
                    reasoning="answered",
                ),
                RubricScore(
                    criterion_name="accuracy", score=0.1,
                    reasoning="invented hours",
                ),
            ],
            conversation_summary="Answered but made up hours",
            critical_failures=[],  # judge forgot
            cost_usd=0.015,
        )
        # Note: the plugin's agentic path calls judge_conversation which
        # does finalize_verdict internally. We can't easily mock at the
        # judge level AND still exercise finalize. Instead mock
        # judge_conversation with a PRE-finalized verdict (passed=False)
        # to simulate the real behavior.
        from puzzleeval.rubric_judge import _finalize_verdict
        finalized = _finalize_verdict(
            fake_verdict, make_rubric(), cost_usd=0.015,
        )
        with patch(
            "puzzleeval.user_simulator.generate_next_user_turn",
            return_value=fake_sim_turn,
        ), patch(
            "puzzleeval.rubric_judge.judge_conversation",
            return_value=finalized,
        ):
            result = plugin.evaluate_output(
                response={}, expected={},
                harness_runner=make_harness_runner("We're open 9-5"),
                persona=make_persona(),
                goal="get hours",
                rubric=make_rubric(),
                max_turns=2,
            )
        # Critical-gate enforcement flipped passed False
        assert result.passed is False
        # Detail carries the critical_failures list
        assert "accuracy" in result.detail["rubric_verdict"]["critical_failures"]


class TestFallback:
    def test_agentic_mode_without_runner_returns_clean_error(self, plugin):
        result = plugin.evaluate_output(
            response={}, expected={},
            persona=make_persona(),
            goal="test",
            rubric=make_rubric(),
            evaluation_mode="agentic",
            # no harness_runner
        )
        assert result.passed is False
        assert "no harness_runner" in result.reasoning.lower()

    def test_explicit_agentic_without_kit_falls_back_to_scripted(self, plugin, caplog):
        """Back-compat: evaluation_mode='agentic' requested but missing
        persona/goal/rubric → fall back to scripted without crashing."""
        import logging
        caplog.set_level(logging.WARNING)
        script = {
            "conversation_script": {
                "user_turns": ["Hi"],
                "assertions": [
                    {"turn_index": 0, "check_type": "contains",
                     "value": "hi", "weight": 1.0},
                ],
            },
        }
        result = plugin.evaluate_output(
            response={}, expected=script,
            harness_runner=make_harness_runner("hi!"),
            evaluation_mode="agentic",
            # Missing persona/goal/rubric
        )
        # Fell back to scripted and still produced a result
        assert result.detail.get("evaluation_mode") == "scripted"
        # Warning was logged
        assert any(
            "incomplete" in r.message
            for r in caplog.records
        )
