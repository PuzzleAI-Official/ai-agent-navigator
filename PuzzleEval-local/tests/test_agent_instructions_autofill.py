"""Regression guards for the deterministic input_context.instructions auto-fill.

Real-run trace f9de380b (2026-04-22): despite the Agent 3 prompt rewrite
making `input_context.instructions` the single canonical place for the
agent's system prompt, Claude emitted 5/5 conversational test cases with
empty input_context. The generic runner-level fallback kicked in, tanking
rubric scores for all domain-specific criteria.

AD-007: safety-critical contracts belong in deterministic code, not
prompts. `_ensure_agent_instructions_on_conversational` is the
deterministic enforcement point:
  - Primary: copy from scope_spec.agent_instructions (Agent 1's field)
  - Fallback: derive from workflow step description + domain + sample_output
  - Idempotent: preserves existing instructions
  - Scoped: only affects conversational test cases
"""

from __future__ import annotations

import pytest

from puzzleeval.agents.synthetic_tests import (
    _derive_instructions_from_scope,
    _ensure_agent_instructions_on_conversational,
    _find_scope_spec_for,
    _find_workflow_step_for,
    _has_agent_instructions,
)
from puzzleeval.schemas import (
    Agent3Result,
    Constraints,
    JudgementCriterion,
    Persona,
    RubricCriterion,
    ScopeTestSpec,
    SubTask,
    TestCase,
    TestPlan,
    UserUnderstandingOutput,
    WorkflowBlueprint,
    WorkflowStep,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_conversational_tc(**overrides) -> TestCase:
    defaults = dict(
        id="tc-001",
        sub_task_ref="voice",
        scenario="scenario",
        scope_id="step_1",
        input_type="voice_conversation",
        input_data='{"shape":"twilio"}',
        output_type="voice_conversation",
        expected_output="{}",
        judgement_criteria=[
            JudgementCriterion(
                criterion="c", eval_type="subjective_quality", weight=1.0,
            ),
        ],
        difficulty="medium",
        tags=["happy_path"],
        persona=Persona(name="M", demographics="h", emotional_state="s"),
        goal="book appointment",
        rubric=[
            RubricCriterion(name="goal", description="g", weight=1.0),
        ],
        input_context={},
    )
    defaults.update(overrides)
    return TestCase(**defaults)


def _make_a1_output(*, agent_instructions: str | None = None) -> UserUnderstandingOutput:
    return UserUnderstandingOutput(
        summary="Voice agent for Acme Plumbing in Los Angeles",
        sub_tasks=[
            SubTask(
                description="voice_agent", capability="voice_agent",
                requires_test_files=False,
                search_keywords=["voice"], search_strategy="both",
            ),
        ],
        domain="home services / plumbing dispatch",
        search_keywords=["voice"],
        constraints=Constraints(),
        workflow_summary="voice",
        workflow=WorkflowBlueprint(
            steps=[
                WorkflowStep(
                    id="step_1", role="voice_agent",
                    description=(
                        "Answer inbound plumbing calls, diagnose the issue, "
                        "quote from the pricing menu, collect address + "
                        "preferred time, confirm booking or transfer."
                    ),
                    capability="voice_agent", input_from="user",
                    output_format="voice_turn",
                ),
            ],
        ),
        test_plan=TestPlan(
            scope_specs=[
                ScopeTestSpec(
                    scope_id="step_1",
                    test_mode="synthetic_structured",
                    input_type="voice_conversation",
                    output_type="voice_turn",
                    input_description="voice test input",
                    expected_output_description=(
                        "Agent engages naturally, collects info, "
                        "quotes from menu, confirms."
                    ),
                    sample_input="caller asks about water heater",
                    sample_output=(
                        "Agent quotes $80 diagnostic + $450-$850 for "
                        "water heater replacement, confirms LA metro."
                    ),
                    test_count_target=5,
                    reference_mode="exemplar",
                    agent_instructions=agent_instructions,
                ),
            ],
            total_test_target=5,
        ),
    )


# ---------------------------------------------------------------------------
# Core auto-fill behavior
# ---------------------------------------------------------------------------


class TestAutoFillFromScopeSpec:
    """PRIMARY path: Agent 1's scope_spec.agent_instructions flows into
    every conversational test case's input_context.instructions."""

    def test_scope_spec_agent_instructions_propagated_to_all_conversational_tests(self):
        tc = _make_conversational_tc()
        a1 = _make_a1_output(
            agent_instructions=(
                "You are Vera, a 24/7 plumbing dispatcher for Acme Plumbing "
                "(Los Angeles metro only). Pricing: $80 diagnostic, "
                "$450-$850 water heater replacement, +$50 after 8pm."
            ),
        )
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"voice": 1},
        )
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )
        assert _has_agent_instructions(tc.input_context)
        assert "Vera" in tc.input_context["instructions"]
        assert "Acme Plumbing" in tc.input_context["instructions"]
        assert "$80 diagnostic" in tc.input_context["instructions"]

    def test_multiple_tests_get_same_instructions(self):
        """All tests for the SAME scope get the SAME instructions —
        fair comparison requires equal system prompt across candidates."""
        tc1 = _make_conversational_tc(id="tc-001")
        tc2 = _make_conversational_tc(id="tc-002")
        tc3 = _make_conversational_tc(id="tc-003")
        a1 = _make_a1_output(
            agent_instructions="You are Vera, plumbing dispatcher.",
        )
        result = Agent3Result(
            test_cases=[tc1, tc2, tc3], generation_notes="",
            coverage_summary={"voice": 3},
        )
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )
        assert (
            tc1.input_context["instructions"]
            == tc2.input_context["instructions"]
            == tc3.input_context["instructions"]
        )
        assert "Vera" in tc1.input_context["instructions"]


class TestDerivationFallback:
    """FALLBACK path: when Agent 1 also misses scope_spec.agent_instructions,
    derive from workflow step description + domain + sample_output."""

    def test_derivation_uses_workflow_step_description(self):
        tc = _make_conversational_tc()
        # agent_instructions NOT set → triggers fallback
        a1 = _make_a1_output(agent_instructions=None)
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"voice": 1},
        )
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )
        assert _has_agent_instructions(tc.input_context)
        instr = tc.input_context["instructions"]
        # Pulls role + description + domain + sample_output
        assert "voice_agent" in instr
        assert "home services" in instr
        assert "Answer inbound plumbing calls" in instr

    def test_derivation_embeds_sample_output_details(self):
        """Sample_output carries business-specific details (pricing, etc.)
        that Agent 1 captured from the user's input. Derivation MUST
        surface these so the agent has ground-truth rules."""
        tc = _make_conversational_tc()
        a1 = _make_a1_output(agent_instructions=None)
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"voice": 1},
        )
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )
        instr = tc.input_context["instructions"]
        # Sample output mentioned $80 + $450-$850 — those should flow
        # through so the agent knows the pricing menu.
        assert "$80" in instr or "diagnostic" in instr
        assert "$450" in instr or "water heater" in instr

    def test_derivation_standalone_function(self):
        """The deriver is usable independently — given raw inputs, it
        emits a coherent system prompt without needing the full Agent 1
        object graph."""
        spec = ScopeTestSpec(
            scope_id="step_1", test_mode="synthetic_structured",
            input_type="voice_conversation", output_type="voice_turn",
            input_description="x", expected_output_description="y",
            sample_input="caller says hello",
            sample_output="Agent books a $100 appointment",
            test_count_target=3,
        )
        step = WorkflowStep(
            id="step_1", role="customer_service",
            description="Handle customer calls",
            capability="customer_service", input_from="user",
            output_format="voice_turn",
        )
        instr = _derive_instructions_from_scope(
            spec, step, "retail", "Customer service for online store",
        )
        assert "customer_service" in instr
        assert "retail" in instr
        assert "Handle customer calls" in instr
        assert "$100" in instr or "books" in instr


class TestNonConversationalUntouched:
    """Auto-fill must be scoped to conversational modalities. OCR,
    vision, code, webhook, outbound tests MUST NOT get instructions."""

    @pytest.mark.parametrize("input_type,output_type", [
        ("document_content", "structured_json"),
        ("image_description", "classification"),
        ("code", "code"),
        ("webhook_event", "webhook_callback"),
        ("text", "free_text"),
    ])
    def test_non_conversational_never_gets_instructions(
        self, input_type, output_type,
    ):
        tc = TestCase(
            id="tc-nc", sub_task_ref="x", scenario="s",
            input_type=input_type, input_data="x",
            output_type=output_type, expected_output="{}",
            judgement_criteria=[
                JudgementCriterion(
                    criterion="c", eval_type="exact_match", weight=1.0,
                ),
            ],
            difficulty="easy", tags=["happy_path"],
            input_context={},
        )
        a1 = _make_a1_output(
            agent_instructions="You are Vera, a plumbing dispatcher.",
        )
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"x": 1},
        )
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )
        assert not _has_agent_instructions(tc.input_context)


class TestIdempotence:
    """Running auto-fill twice (e.g., after top-up adds more tests)
    must preserve existing instructions. Only empty input_context gets
    touched."""

    def test_existing_instructions_preserved(self):
        tc = _make_conversational_tc(
            input_context={"instructions": "Custom hand-crafted prompt"},
        )
        a1 = _make_a1_output(agent_instructions="SHOULD NOT OVERWRITE")
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"voice": 1},
        )
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )
        assert tc.input_context["instructions"] == "Custom hand-crafted prompt"

    def test_any_alias_counts_as_already_populated(self):
        """If input_context has instructions under ANY of the 5 accepted
        aliases, we shouldn't overwrite."""
        for alias in ("system_prompt", "system", "brief", "agent_prompt"):
            tc = _make_conversational_tc(
                input_context={alias: f"Custom via {alias}"},
            )
            a1 = _make_a1_output(agent_instructions="different string")
            result = Agent3Result(
                test_cases=[tc], generation_notes="",
                coverage_summary={"voice": 1},
            )
            _ensure_agent_instructions_on_conversational(
                result, a1, trace_id="t",
            )
            # Original alias value preserved
            assert tc.input_context[alias] == f"Custom via {alias}"

    def test_running_twice_no_change(self):
        """Second call after first auto-fill: no-op (all tests already
        have instructions)."""
        tc = _make_conversational_tc()
        a1 = _make_a1_output(
            agent_instructions="You are Vera, plumbing dispatcher.",
        )
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"voice": 1},
        )
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )
        first_value = tc.input_context["instructions"]
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )
        assert tc.input_context["instructions"] == first_value


class TestResilience:
    """Auto-fill must never crash the pipeline. Missing scope_id,
    missing workflow, missing test_plan — all safely no-op."""

    def test_missing_scope_id_noop(self):
        tc = _make_conversational_tc(scope_id=None)
        a1 = _make_a1_output(agent_instructions="…")
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"voice": 1},
        )
        # Should not crash. May or may not fill — we accept either.
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )

    def test_missing_workflow_noop(self):
        tc = _make_conversational_tc()
        a1 = _make_a1_output()
        a1.workflow = None
        a1.test_plan.scope_specs[0].agent_instructions = None
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"voice": 1},
        )
        # No scope_spec instructions + no workflow → derivation returns
        # minimal instruction but doesn't crash. TC may or may not have
        # instructions depending on what the deriver produced.
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )

    def test_missing_test_plan_noop(self):
        tc = _make_conversational_tc()
        a1 = _make_a1_output()
        a1.test_plan = None
        result = Agent3Result(
            test_cases=[tc], generation_notes="",
            coverage_summary={"voice": 1},
        )
        # No test_plan → no scope_spec → no primary source. Deriver
        # may still run on workflow alone. Must not crash.
        _ensure_agent_instructions_on_conversational(
            result, a1, trace_id="t",
        )


class TestScopeTestSpecSchema:
    """Lock the schema addition — agent_instructions field is present
    on ScopeTestSpec with a None default so pre-agent_instructions runs
    parse cleanly."""

    def test_scope_test_spec_has_agent_instructions_field(self):
        spec = ScopeTestSpec(
            scope_id="step_1", test_mode="synthetic_structured",
            input_type="voice_conversation", output_type="voice_turn",
            input_description="x", expected_output_description="y",
            sample_input="a", sample_output="b",
            test_count_target=3,
        )
        assert hasattr(spec, "agent_instructions")
        assert spec.agent_instructions is None  # default

    def test_scope_test_spec_accepts_agent_instructions(self):
        spec = ScopeTestSpec(
            scope_id="step_1", test_mode="synthetic_structured",
            input_type="voice_conversation", output_type="voice_turn",
            input_description="x", expected_output_description="y",
            sample_input="a", sample_output="b",
            test_count_target=3,
            agent_instructions="You are Vera…",
        )
        assert spec.agent_instructions == "You are Vera…"

    def test_scope_test_spec_roundtrip_preserves_instructions(self):
        spec = ScopeTestSpec(
            scope_id="step_1", test_mode="synthetic_structured",
            input_type="voice_conversation", output_type="voice_turn",
            input_description="x", expected_output_description="y",
            sample_input="a", sample_output="b",
            test_count_target=3,
            agent_instructions="You are Vera…",
        )
        d = spec.model_dump()
        spec2 = ScopeTestSpec.model_validate(d)
        assert spec2.agent_instructions == "You are Vera…"


class TestAgent1PromptTeaches:
    """Agent 1 prompt must teach the new agent_instructions field so
    Agent 1 populates it directly (primary path). Without this, Agent 3's
    derivation fallback becomes the PRIMARY path, which is less reliable."""

    def _read_agent1_prompt(self) -> str:
        from puzzleeval.agents.user_understanding import SYSTEM_PROMPT
        return SYSTEM_PROMPT

    def test_prompt_mentions_agent_instructions_field(self):
        p = self._read_agent1_prompt()
        assert "agent_instructions" in p

    def test_prompt_has_agent_instructions_rule(self):
        p = self._read_agent1_prompt()
        # Rule 8 in the numbered rules list
        assert (
            "`agent_instructions` is REQUIRED" in p
            or "agent_instructions is REQUIRED" in p.lower()
        )

    def test_prompt_shows_worked_example_with_populated_field(self):
        """The test_plan example for voice agent MUST show a populated
        agent_instructions field so Agent 1 has a concrete template
        that demonstrates including business details (name, domain,
        pricing menu, service area)."""
        p = self._read_agent1_prompt()
        assert '"agent_instructions":' in p
        # The example should contain concrete business details.
        # "Acme Plumbing" is the named business; the example should
        # also include pricing details so Agent 1 sees the template
        # for encoding menu/policy info.
        assert "Acme Plumbing" in p, (
            "Agent 1 prompt must show a worked example with a "
            "concrete business name so Claude learns to embed user-"
            "specific details in agent_instructions."
        )
        assert "$80" in p or "diagnostic" in p, (
            "Example must show pricing details so Agent 1 learns to "
            "weave the user's pricing menu into agent_instructions."
        )
        assert "LA metro" in p or "Los Angeles" in p, (
            "Example must show service-area details so Agent 1 learns "
            "to weave the user's service area into agent_instructions."
        )
