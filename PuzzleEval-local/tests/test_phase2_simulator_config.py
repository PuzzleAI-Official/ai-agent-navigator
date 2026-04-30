"""Phase 2C.5 tests — SimulatorConfig + extend_to_completion semantics.

Resolves the cross-prompt conflict (F-Aux1) between rubric_judge
(scores goal completion on a 0-1 spectrum, expects multi-step
resolution) and user_simulator (default ends call on goal-met).

The new TestCase.simulator_config sub-field carries:
  - extend_to_completion: bool — whether to continue past goal-met
  - max_extension_turns: int — cap on extension turns

Default behavior (extend_to_completion=False) preserves legacy
"end-on-goal" semantics — no regression for existing fixtures.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# SimulatorConfig schema
# ---------------------------------------------------------------------------


class TestSimulatorConfigSchema:
    def test_defaults_preserve_legacy_behavior(self):
        from puzzleeval.schemas import SimulatorConfig

        cfg = SimulatorConfig()
        # Default extend_to_completion=False preserves the legacy
        # "end-on-goal" semantics so existing test fixtures are
        # unaffected.
        assert cfg.extend_to_completion is False
        assert cfg.max_extension_turns == 3

    def test_explicit_extend_mode(self):
        from puzzleeval.schemas import SimulatorConfig

        cfg = SimulatorConfig(extend_to_completion=True, max_extension_turns=5)
        assert cfg.extend_to_completion is True
        assert cfg.max_extension_turns == 5

    def test_max_extension_turns_clamped_to_safe_range(self):
        """A value above 10 or below 0 is invalid (Pydantic enforces
        ge=0, le=10)."""
        import pytest
        from pydantic import ValidationError

        from puzzleeval.schemas import SimulatorConfig

        with pytest.raises(ValidationError):
            SimulatorConfig(extend_to_completion=True, max_extension_turns=20)
        with pytest.raises(ValidationError):
            SimulatorConfig(extend_to_completion=True, max_extension_turns=-1)


# ---------------------------------------------------------------------------
# TestCase integration
# ---------------------------------------------------------------------------


class TestTestCaseSimulatorConfigField:
    def _make_voice_test_case(self, **overrides):
        from puzzleeval.schemas import (
            JudgementCriterion,
            Persona,
            RubricCriterion,
            TestCase,
        )

        defaults = dict(
            id="tc-test",
            sub_task_ref="x",
            scenario="test",
            input_type="voice_conversation",
            input_data="{}",
            output_type="text",
            expected_output="x",
            input_context={"instructions": "You are a test agent."},
            judgement_criteria=[
                JudgementCriterion(
                    criterion="c", eval_type="subjective_quality", weight=1.0,
                )
            ],
            difficulty="medium",
            tags=["happy_path"],
            persona=Persona(name="M", demographics="h", emotional_state="s"),
            goal="test",
            rubric=[
                RubricCriterion(name="goal_completion", description="g", weight=1.0)
            ],
            evaluation_mode="agentic",
        )
        defaults.update(overrides)
        return TestCase(**defaults)

    def test_default_simulator_config_is_none(self):
        tc = self._make_voice_test_case()
        assert tc.simulator_config is None

    def test_can_set_simulator_config_with_extend_mode(self):
        from puzzleeval.schemas import SimulatorConfig

        tc = self._make_voice_test_case(
            simulator_config=SimulatorConfig(
                extend_to_completion=True, max_extension_turns=4,
            ),
        )
        assert tc.simulator_config is not None
        assert tc.simulator_config.extend_to_completion is True
        assert tc.simulator_config.max_extension_turns == 4


# ---------------------------------------------------------------------------
# user_simulator prompt swap
# ---------------------------------------------------------------------------


class TestUserSimulatorPromptSwap:
    def _make_persona(self):
        from puzzleeval.schemas import Persona

        return Persona(name="Test", demographics="adult", emotional_state="calm")

    def test_default_prompt_has_never_go_past_goal_rule(self):
        from puzzleeval.user_simulator import _build_system_prompt

        prompt = _build_system_prompt(
            persona=self._make_persona(), goal="x", constraints=[],
        )
        assert "NEVER go past your goal" in prompt, (
            "Default behavior must preserve the legacy end-on-goal rule."
        )

    def test_extend_to_completion_swaps_the_rule(self):
        from puzzleeval.user_simulator import _build_system_prompt

        prompt = _build_system_prompt(
            persona=self._make_persona(),
            goal="x",
            constraints=[],
            extend_to_completion=True,
            max_extension_turns=3,
        )
        # Old rule must be GONE.
        assert "NEVER go past your goal" not in prompt
        # New extension rule must be PRESENT.
        assert "After your primary goal is met" in prompt
        assert "up to 3 more" in prompt

    def test_max_extension_turns_value_is_substituted(self):
        from puzzleeval.user_simulator import _build_system_prompt

        prompt = _build_system_prompt(
            persona=self._make_persona(),
            goal="x",
            constraints=[],
            extend_to_completion=True,
            max_extension_turns=5,
        )
        assert "up to 5 more" in prompt

    def test_extend_mode_keeps_other_safety_rules(self):
        """The extension swap should NOT touch other 'NEVER' clauses
        (character-breaking, AI-revelation, etc.)."""
        from puzzleeval.user_simulator import _build_system_prompt

        prompt = _build_system_prompt(
            persona=self._make_persona(),
            goal="x",
            constraints=[],
            extend_to_completion=True,
            max_extension_turns=3,
        )
        # Character-integrity rules MUST persist.
        assert 'NEVER reveal you\'re a test, a simulation, an AI' in prompt
        assert 'NEVER say "as an AI"' in prompt
