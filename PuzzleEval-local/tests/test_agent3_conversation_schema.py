"""Schema + prompt tests for the agentic conversational Agent 3 contract.

Locks in:
  - Persona / RubricCriterion / RubricVerdict JSON roundtrip
  - TestCase with new conversational fields parses cleanly + back-compat
    for old TestCase (no new fields set)
  - Agent 3 prompt teaches agentic persona+goal+rubric contract
  - Validator warns (not errors) on agentic-mode tests missing persona/
    goal/rubric — migration-friendly
  - Validator flags invalid evaluation_mode / rubric weights / max_turns
  - Rubric structural checks detect malformed weights
"""

from __future__ import annotations

import pytest

from puzzleeval.schemas import (
    Agent3Result,
    Constraints,
    ConversationTurn,
    InfoStatus,
    JudgementCriterion,
    Persona,
    RubricCriterion,
    RubricScore,
    RubricVerdict,
    SimulatorTurn,
    SubTask,
    TestCase,
    UserUnderstandingOutput,
)
from puzzleeval.validators import validate_agent3_output


# ---------------------------------------------------------------------------
# Schema round-trip + back-compat
# ---------------------------------------------------------------------------


class TestSchemaRoundtrip:
    """New models must JSON-roundtrip cleanly (used for persistence +
    SSE payload serialization)."""

    def test_persona_roundtrip(self):
        p = Persona(
            name="Dr. Chen", demographics="ICU director, 3am shift",
            emotional_state="worried", tech_level="technical",
            speaking_style="precise, impatient",
        )
        d = p.model_dump()
        p2 = Persona.model_validate(d)
        assert p2.name == "Dr. Chen"
        assert p2.tech_level == "technical"

    def test_rubric_criterion_roundtrip_with_critical_gate(self):
        c = RubricCriterion(
            name="accuracy", description="no hallucination",
            weight=0.4, critical=True, min_passing_score=0.6,
        )
        d = c.model_dump()
        c2 = RubricCriterion.model_validate(d)
        assert c2.critical is True
        assert c2.min_passing_score == 0.6

    def test_rubric_verdict_with_scores_roundtrip(self):
        v = RubricVerdict(
            overall_score=0.75, passed=True,
            criterion_scores=[
                RubricScore(
                    criterion_name="goal", score=0.8,
                    reasoning="booked", evidence_turn_indices=[2, 4],
                ),
            ],
            conversation_summary="Clean booking",
            critical_failures=[],
            cost_usd=0.015,
        )
        d = v.model_dump()
        v2 = RubricVerdict.model_validate(d)
        assert v2.overall_score == 0.75
        assert v2.criterion_scores[0].evidence_turn_indices == [2, 4]

    def test_testcase_back_compat_no_new_fields(self):
        """Old TestCase with ONLY the original fields must parse cleanly
        — the new conversational fields all default to None/empty/auto."""
        tc = TestCase(
            id="tc-001", sub_task_ref="ocr", scenario="sample",
            input_type="document_content", input_data="x",
            output_type="structured_json", expected_output="{}",
            judgement_criteria=[
                JudgementCriterion(
                    criterion="c1", eval_type="exact_match", weight=1.0,
                ),
            ],
            difficulty="easy", tags=["happy_path"],
        )
        assert tc.persona is None
        assert tc.goal is None
        assert tc.rubric == []
        assert tc.constraints == []
        assert tc.max_turns == 6
        assert tc.evaluation_mode == "auto"

    def test_testcase_agentic_full_roundtrip(self):
        tc = TestCase(
            id="tc-002", sub_task_ref="voice_agent",
            scenario="Maria's water heater", input_type="voice_conversation",
            input_data='{"shape":"twilio"}',
            output_type="voice_turn", expected_output='{"shape":"twilio"}',
            judgement_criteria=[
                JudgementCriterion(
                    criterion="c1", eval_type="subjective_quality",
                    weight=1.0,
                ),
            ],
            difficulty="medium", tags=["happy_path"],
            persona=Persona(
                name="Maria", demographics="homeowner",
                emotional_state="stressed",
            ),
            goal="book tonight",
            constraints=["stay focused on plumbing"],
            rubric=[
                RubricCriterion(
                    name="goal_completion", description="booked?",
                    weight=0.5,
                ),
                RubricCriterion(
                    name="accuracy", description="no hallucination",
                    weight=0.5, critical=True, min_passing_score=0.5,
                ),
            ],
            max_turns=5,
            evaluation_mode="agentic",
        )
        d = tc.model_dump()
        tc2 = TestCase.model_validate(d)
        assert tc2.persona.name == "Maria"
        assert tc2.evaluation_mode == "agentic"
        assert len(tc2.rubric) == 2
        assert tc2.rubric[1].critical is True


# ---------------------------------------------------------------------------
# Agent 3 prompt contract
# ---------------------------------------------------------------------------


class TestAgent3PromptAgentic:
    """Agent 3's prompt must teach the new agentic contract clearly so
    the model generates persona + goal + rubric test cases, not static
    scripts."""

    def test_prompt_teaches_agentic_fields(self):
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        for field in ("persona", "goal", "constraints", "rubric",
                      "max_turns", "evaluation_mode"):
            assert field in SYSTEM_PROMPT

    def test_prompt_includes_standard_rubric_criteria(self):
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        # Core 6 criterion names all templated
        for name in (
            "goal_completion", "accuracy_no_hallucination",
            "info_gathering", "appropriate_tone",
            "policy_compliance", "scope_adherence",
        ):
            assert name in SYSTEM_PROMPT

    def test_prompt_explicit_critical_gate_guidance(self):
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        # "critical" appears with meaningful context
        assert "critical" in SYSTEM_PROMPT.lower()
        assert "min_passing_score" in SYSTEM_PROMPT

    def test_prompt_keeps_legacy_scripted_callout(self):
        """Back-compat: for the migration window, the scripted-mode path
        must still be documented so tests can opt into it."""
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        assert "LEGACY scripted mode" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Validator — new conversational-field checks
# ---------------------------------------------------------------------------


def _minimal_agent1_output():
    return UserUnderstandingOutput(
        summary="Handle inbound plumbing dispatch calls",
        sub_tasks=[
            SubTask(
                description="handle inbound phone calls",
                capability="voice_agent",
                requires_test_files=False,
                search_keywords=["voice agent", "phone dispatch"],
                search_strategy="both",
            ),
        ],
        domain="home services",
        search_keywords=["plumbing", "voice"],
        constraints=Constraints(),
        workflow_summary="phone dispatch",
    )


def _make_conversational_test_case(
    *, evaluation_mode="agentic",
    persona=None, goal=None, rubric=None, max_turns=6,
) -> TestCase:
    return TestCase(
        id="tc-001", sub_task_ref="handle inbound phone calls",
        scenario="Maria test", input_type="voice_conversation",
        input_data='{"shape":"twilio"}',
        output_type="voice_turn", expected_output='{"shape":"twilio"}',
        judgement_criteria=[
            JudgementCriterion(
                criterion="c1", eval_type="subjective_quality", weight=1.0,
            ),
        ],
        difficulty="medium", tags=["happy_path"],
        persona=persona, goal=goal, rubric=rubric or [],
        max_turns=max_turns, evaluation_mode=evaluation_mode,
    )


class TestValidatorAgenticFields:
    def test_agentic_without_persona_warns_but_doesnt_error(self):
        """Migration-friendly: missing persona → warning (plugin falls
        back to scripted), not a hard error."""
        tc = _make_conversational_test_case(
            evaluation_mode="agentic",
            persona=None, goal="book appointment",
            rubric=[RubricCriterion(name="goal", description="...", weight=1.0)],
        )
        result = validate_agent3_output(
            Agent3Result(
                test_cases=[tc],
                generation_notes="",
                coverage_summary={"handle inbound phone calls": 1},
            ),
            _minimal_agent1_output(),
        )
        # Warning present — but passed is True if sufficiency floor ok.
        assert any("no persona" in w for w in result.warnings)

    def test_agentic_without_goal_warns(self):
        tc = _make_conversational_test_case(
            persona=Persona(
                name="Maria", demographics="homeowner",
                emotional_state="stressed",
            ),
            goal=None,
            rubric=[RubricCriterion(name="goal", description="...", weight=1.0)],
        )
        result = validate_agent3_output(
            Agent3Result(
                test_cases=[tc], generation_notes="",
                coverage_summary={"handle inbound phone calls": 1},
            ),
            _minimal_agent1_output(),
        )
        assert any("no goal" in w for w in result.warnings)

    def test_rubric_weights_wildly_skewed_warns(self):
        tc = _make_conversational_test_case(
            persona=Persona(
                name="Maria", demographics="h",
                emotional_state="s",
            ),
            goal="book",
            rubric=[
                RubricCriterion(name="a", description="...", weight=0.9),
                RubricCriterion(name="b", description="...", weight=0.9),
                RubricCriterion(name="c", description="...", weight=0.9),
            ],  # sum 2.7 — judge will normalize but this is suspicious
        )
        result = validate_agent3_output(
            Agent3Result(
                test_cases=[tc], generation_notes="",
                coverage_summary={"handle inbound phone calls": 1},
            ),
            _minimal_agent1_output(),
        )
        assert any("weights sum" in w for w in result.warnings)

    def test_invalid_evaluation_mode_is_caught(self):
        """Pydantic's Literal type catches invalid evaluation_mode at
        TestCase construction — the validator's check is belt-and-
        braces defense-in-depth for cases where a TestCase is built via
        model_construct() (skipping validation). Lock BOTH layers."""
        import pydantic
        # Layer 1: Pydantic Literal blocks construction of invalid values.
        with pytest.raises(pydantic.ValidationError) as exc_info:
            _make_conversational_test_case(
                evaluation_mode="ballistic",
                persona=Persona(
                    name="M", demographics="h", emotional_state="s",
                ),
                goal="t",
                rubric=[RubricCriterion(name="a", description="...", weight=1.0)],
            )
        assert "evaluation_mode" in str(exc_info.value)

        # Layer 2: if someone bypasses validation via model_construct,
        # the runtime validator still catches the invalid mode.
        tc_bypass = TestCase.model_construct(
            id="tc-bad", sub_task_ref="handle inbound phone calls",
            scenario="s", input_type="voice_conversation",
            input_data="{}", output_type="voice_turn", expected_output="{}",
            judgement_criteria=[
                JudgementCriterion(
                    criterion="c1", eval_type="subjective_quality", weight=1.0,
                ),
            ],
            difficulty="medium", tags=["happy_path"],
            persona=Persona(name="M", demographics="h", emotional_state="s"),
            goal="test",
            rubric=[RubricCriterion(name="a", description="...", weight=1.0)],
            max_turns=5,
            evaluation_mode="ballistic",  # bypass Pydantic via construct
        )
        result = validate_agent3_output(
            Agent3Result(
                test_cases=[tc_bypass], generation_notes="",
                coverage_summary={"handle inbound phone calls": 1},
            ),
            _minimal_agent1_output(),
        )
        assert any("invalid evaluation_mode" in e for e in result.errors)
        assert result.passed is False
