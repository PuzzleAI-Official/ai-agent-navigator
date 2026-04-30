"""Regression guards for the agent-system-prompt grounding fix.

Real-run observation (2026-04-22, run d7d08eba): Agent 3's prompt was
ambiguous about where the agent's system prompt goes (`input_data`
vs `input_context.instructions`). The Agent 5 runner closure only
reads `input_context.instructions` — so instructions misplaced in
`input_data` are silently lost, the candidate agent gets a generic
"You are a helpful voice_agent" fallback, and every rubric score
on domain-specific criteria (accuracy_no_hallucination, policy_
compliance, scope_adherence) lands near-zero because the agent was
never told the scope rules.

This module locks in:
  1. Agent 3 prompt mandates `input_context.instructions` as the
     SINGLE canonical location for the agent's system prompt.
  2. Agent 3 prompt tells the model NOT to put instructions in
     `input_data` for conversational tests.
  3. The PER-MODALITY FIELD MATRIX explicitly enumerates which
     fields apply to which modality.
  4. Validator warns when conversational tests lack
     input_context.instructions.
  5. Validator warns when non-conversational tests populate
     persona/rubric (signal of confused modality routing).
  6. rubric_judge accepts agent_system_prompt and includes it in
     the judge's system prompt as ground truth for scope/policy.
  7. End-to-end threading: Agent 5 → plugin.evaluate_output →
     rubric_judge receives the instructions.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class TestAgent3PromptUnambiguous:
    """Agent 3 prompt must make input_context.instructions the single
    canonical place for the agent's system prompt."""

    def _prompt(self) -> str:
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        return SYSTEM_PROMPT

    def test_has_modality_field_matrix(self):
        p = self._prompt()
        assert "PER-MODALITY FIELD MATRIX" in p, (
            "Agent 3 prompt must include a per-modality field matrix "
            "so Agent 3 has an unambiguous reference for which fields "
            "apply to which input_type."
        )

    def test_matrix_lists_all_major_modality_groups(self):
        p = self._prompt()
        for group in (
            "Conversational multi-turn",
            "Conversational single-turn",
            "Document / OCR",
            "Image / vision",
            "Code generation",
            "Audio input / transcription",
            "Webhook / inbound",
            "Outbound messaging",
        ):
            assert group in p, f"Matrix missing modality group: {group!r}"

    def test_matrix_emphasizes_two_different_llms_concept(self):
        """The matrix prose must explicitly call out that persona and
        input_context.instructions are two distinct LLM system prompts
        for two different sides of the conversation. Past ambiguity
        led to 500-class errors. Phase 2D compressed the wording but
        the contract remains."""
        p = self._prompt()
        # New compressed phrasing: "Two LLM system prompts, two sides".
        # Either the new phrasing OR the legacy CAPS form satisfies the
        # contract — both make the same teaching point.
        has_concept = (
            "Two LLM system prompts, two sides" in p
            or "TWO DIFFERENT LLM system prompts" in p
        )
        assert has_concept, (
            "Prompt must explicitly call out the two-distinct-prompts "
            "concept (persona is the simulator side, "
            "input_context.instructions is the agent side)."
        )

    def test_input_context_instructions_single_source_rule(self):
        """The Phase 2A consolidation removed the verbose CAPS-shouted
        'THIS IS THE ONLY PLACE' framing in favor of a principle-based
        co-located rule. Phase 2D further compressed it. Verify the
        single-source contract is still clearly stated."""
        p = self._prompt()
        # Phase 2D compressed "SINGLE source" → "Single source" in the
        # bullet header. Either form satisfies the contract.
        assert "Single source" in p or "SINGLE source" in p, (
            "Prompt must declare input_context.instructions as the "
            "single canonical location for the agent's system prompt."
        )
        # The principle: not in input_data, not in persona, not in
        # expected_output. Verify the negation is preserved (the
        # compressed wording lists them inline rather than across
        # multiple sentences).
        assert "`input_data`" in p
        assert "`persona`" in p
        assert "`expected_output`" in p

    def test_voice_conversation_input_data_is_placeholder_only(self):
        """The voice_conversation section's input_data guidance MUST
        warn against putting `instructions` in input_data — that was
        the original bug. Input_data is a placeholder carrying only
        `shape`."""
        p = self._prompt()
        import re
        match = re.search(
            r"### When input_type == \"voice_conversation\".*?(?=### )",
            p, re.DOTALL,
        )
        assert match, "voice_conversation section not found"
        section = match.group(0)
        # Positive: the section must tell Agent 3 NOT to put
        # instructions in input_data. Phase 2D compressed
        # "Do NOT include `instructions` here" → "Do NOT put
        # `instructions` here". Either form satisfies the contract.
        has_warning = (
            ("Do NOT put" in section or "Do NOT include" in section)
            and "instructions" in section
        )
        assert has_warning, (
            "voice_conversation section must explicitly tell Agent 3 "
            "not to put `instructions` in input_data."
        )
        # The voice_conversation section must reference the matrix or
        # the input_context.instructions co-located rule as the
        # canonical location for the agent's system prompt.
        assert (
            "input_context.instructions" in section
            or "matrix above" in section
        )

    def test_single_voice_turn_section_not_duplicated(self):
        p = self._prompt()
        cnt = p.count('### When input_type == "voice_turn"')
        assert cnt == 1, f"voice_turn section should appear ONCE, got {cnt}"

    def test_forbidden_cross_modality_fields_rule(self):
        """Phase 2D renamed 'Forbidden cross-modality field usage' to
        'Cross-modality field usage notes' (the matrix is now the
        authoritative rule; the section is clarifications). The
        underlying contract — flagging conversational fields on
        non-conversational tests — must still be present."""
        p = self._prompt()
        assert (
            "Cross-modality field usage" in p
            or "Forbidden cross-modality field usage" in p
        ), "Cross-modality clarifications section is missing."
        # The contract: persona/goal/constraints/rubric/max_turns
        # populated on a non-conversational test must trigger a
        # validator warning. Verify the warning is described.
        assert "non-conversational" in p.lower()
        assert "warn" in p.lower()

    def test_non_conversational_input_context_rule(self):
        """Phase 2A consolidated the REQUIRED-vs-FORBIDDEN asymmetry
        into a single co-located rule. Verify the non-conversational
        contract (no instructions for non-LLM-agent modalities) is
        still stated clearly."""
        p = self._prompt()
        # Must teach: non-conversational modalities have no agent
        # system prompt → input_context.instructions is not populated.
        assert "non-conversational" in p.lower()
        # Must reference the metadata-only pattern for those modalities.
        assert "metadata only" in p or "per-test metadata" in p


class TestValidatorEnforcesInstructionsOnConversational:
    """Validator must warn when a conversational test case lacks
    input_context.instructions — the missing-instructions path is
    silently-broken (candidate gets generic fallback)."""

    def _make_tc(self, **overrides):
        from puzzleeval.schemas import (
            TestCase, JudgementCriterion, Persona, RubricCriterion,
        )
        defaults = dict(
            id="tc-001", sub_task_ref="voice_agent",
            scenario="test", input_type="voice_conversation",
            input_data='{"shape":"twilio"}',
            output_type="voice_conversation", expected_output="{}",
            judgement_criteria=[
                JudgementCriterion(
                    criterion="c", eval_type="subjective_quality",
                    weight=1.0,
                ),
            ],
            difficulty="medium", tags=["happy_path"],
            persona=Persona(
                name="M", demographics="h", emotional_state="s",
            ),
            goal="book appointment",
            rubric=[
                RubricCriterion(name="goal_completion",
                                description="g", weight=1.0),
            ],
            evaluation_mode="agentic",
        )
        defaults.update(overrides)
        return TestCase(**defaults)

    def _validate(self, tc):
        from puzzleeval.validators import validate_agent3_output
        from puzzleeval.schemas import (
            Agent3Result, UserUnderstandingOutput, SubTask, Constraints,
        )
        a1 = UserUnderstandingOutput(
            summary="voice agent",
            sub_tasks=[SubTask(
                description="voice_agent",
                capability="voice_agent",
                requires_test_files=False,
                search_keywords=["voice"], search_strategy="both",
            )],
            domain="home services",
            search_keywords=["voice"],
            constraints=Constraints(),
            workflow_summary="voice",
        )
        return validate_agent3_output(
            Agent3Result(
                test_cases=[tc],
                generation_notes="",
                coverage_summary={"voice_agent": 1},
            ),
            a1,
        )

    def test_warns_on_missing_input_context_instructions(self):
        tc = self._make_tc(input_context={})  # no instructions
        result = self._validate(tc)
        assert any(
            "no input_context.instructions" in w for w in result.warnings
        ), (
            "Validator must warn when a conversational test has no "
            "input_context.instructions — this is the exact silent-"
            "failure path we're guarding against."
        )

    def test_accepts_populated_instructions(self):
        tc = self._make_tc(input_context={
            "instructions": "You are Vera, a plumbing dispatcher…",
        })
        result = self._validate(tc)
        assert not any(
            "no input_context.instructions" in w for w in result.warnings
        )

    def test_accepts_any_alias(self):
        """The alias list (instructions/system_prompt/system/brief/
        agent_prompt) must ALL count as valid — harnesses check these
        in the same order."""
        from puzzleeval.validators import validate_agent3_output
        for alias in ("system_prompt", "system", "brief", "agent_prompt"):
            tc = self._make_tc(input_context={alias: "You are Vera…"})
            result = self._validate(tc)
            assert not any(
                "no input_context.instructions" in w for w in result.warnings
            ), f"Alias {alias!r} should satisfy the check"


class TestValidatorWarnsConversationalFieldsOnNonConversational:
    """Converse: if Agent 3 populated persona/rubric for an OCR test,
    that's a sign of confused modality routing. Validator flags it."""

    def _make_ocr_tc_with_stray(self, **overrides):
        from puzzleeval.schemas import (
            TestCase, JudgementCriterion, Persona, RubricCriterion,
        )
        defaults = dict(
            id="tc-ocr", sub_task_ref="ocr",
            scenario="OCR test", input_type="document_content",
            input_data="invoice text", output_type="structured_json",
            expected_output='{"vendor":"x"}',
            judgement_criteria=[
                JudgementCriterion(
                    criterion="c", eval_type="exact_match", weight=1.0,
                ),
            ],
            difficulty="easy", tags=["happy_path"],
            # Stray conversational fields:
            persona=Persona(name="M", demographics="h",
                            emotional_state="s"),
            goal="extract invoice data",
            rubric=[RubricCriterion(name="x", description="y",
                                     weight=1.0)],
        )
        defaults.update(overrides)
        return TestCase(**defaults)

    def test_warns_when_persona_rubric_on_ocr(self):
        tc = self._make_ocr_tc_with_stray()
        from puzzleeval.validators import validate_agent3_output
        from puzzleeval.schemas import (
            Agent3Result, UserUnderstandingOutput, SubTask, Constraints,
        )
        a1 = UserUnderstandingOutput(
            summary="OCR invoices",
            sub_tasks=[SubTask(
                description="ocr",
                capability="ocr",
                requires_test_files=False,
                search_keywords=["ocr"], search_strategy="both",
            )],
            domain="accounting", search_keywords=["ocr"],
            constraints=Constraints(), workflow_summary="ocr",
        )
        result = validate_agent3_output(
            Agent3Result(
                test_cases=[tc], generation_notes="",
                coverage_summary={"ocr": 1},
            ),
            a1,
        )
        assert any(
            "conversational-only field" in w for w in result.warnings
        ), (
            "Validator must warn when conversational fields are "
            "populated on non-conversational tests — that's a "
            "signal of confused modality routing."
        )

    def test_warns_on_stray_instructions_for_ocr(self):
        """input_context.instructions on an OCR test is also
        suspicious — the candidate isn't an LLM agent."""
        from puzzleeval.schemas import (
            TestCase, JudgementCriterion,
        )
        tc = TestCase(
            id="tc-ocr-2", sub_task_ref="ocr", scenario="ocr",
            input_type="document_content", input_data="x",
            output_type="structured_json", expected_output="{}",
            judgement_criteria=[
                JudgementCriterion(
                    criterion="c", eval_type="exact_match", weight=1.0,
                ),
            ],
            difficulty="easy", tags=["happy_path"],
            input_context={"instructions": "You are Vera…"},  # wrong!
        )
        from puzzleeval.validators import validate_agent3_output
        from puzzleeval.schemas import (
            Agent3Result, UserUnderstandingOutput, SubTask, Constraints,
        )
        a1 = UserUnderstandingOutput(
            summary="OCR",
            sub_tasks=[SubTask(
                description="ocr", capability="ocr",
                requires_test_files=False,
                search_keywords=["ocr"], search_strategy="both",
            )],
            domain="accounting", search_keywords=["ocr"],
            constraints=Constraints(), workflow_summary="ocr",
        )
        result = validate_agent3_output(
            Agent3Result(
                test_cases=[tc], generation_notes="",
                coverage_summary={"ocr": 1},
            ),
            a1,
        )
        assert any(
            "input_context.instructions but isn't a" in w for w in result.warnings
        )


class TestJudgeReceivesAgentSystemPrompt:
    """rubric_judge.judge_conversation must accept + use
    agent_system_prompt. End-to-end: the plugin extracts from
    expected.input_context → passes through drive_conversation →
    reaches the judge."""

    def test_judge_signature_has_agent_system_prompt(self):
        from puzzleeval.rubric_judge import judge_conversation
        sig = inspect.signature(judge_conversation)
        assert "agent_system_prompt" in sig.parameters

    def test_prompt_builder_emits_instructions_block_when_populated(self):
        from puzzleeval.rubric_judge import _format_agent_instructions_block
        block = _format_agent_instructions_block(
            "You are Vera, a plumbing dispatcher."
        )
        assert "Vera" in block
        # Ground-truth framing must be present — the judge uses this
        # as the authoritative contract for scope/policy scoring.
        assert "canonical contract" in block or "ground truth" in block.lower()

    def test_prompt_builder_emits_neutral_block_when_empty(self):
        """When no instructions are configured, the judge should NOT
        penalize scope violations against guessed rules — the block
        must explicitly de-ground in that case."""
        from puzzleeval.rubric_judge import _format_agent_instructions_block
        for empty in (None, "", "   "):
            block = _format_agent_instructions_block(empty)
            assert "No system prompt was provided" in block
            # The neutral block must tell the judge explicitly NOT to
            # penalize scope violations against guessed rules. The
            # literal "not penalize" / "don't penalize" phrasing is
            # what surfaces that instruction — either works.
            lower = block.lower()
            has_de_grounding = (
                "not penalize" in lower
                or "don't penalize" in lower
                or "do not penalize" in lower
            )
            assert has_de_grounding, (
                f"Neutral block must explicitly tell the judge not to "
                f"penalize scope violations that weren't defined. Got: "
                f"{block!r}"
            )

    def test_build_system_prompt_contains_agent_instructions_header(self):
        from puzzleeval.rubric_judge import _build_system_prompt
        from puzzleeval.schemas import Persona, RubricCriterion
        sp = _build_system_prompt(
            persona=Persona(name="M", demographics="h",
                             emotional_state="s"),
            goal="book appointment",
            rubric=[RubricCriterion(name="x", description="y",
                                     weight=1.0)],
            transcript=[],
            candidate_role="plumbing dispatcher",
            agent_system_prompt="You are Vera, a plumbing dispatcher.",
        )
        assert "AGENT'S OFFICIAL INSTRUCTIONS" in sp
        assert "Vera" in sp


class TestEndToEndWiringThroughPlugin:
    """input_context must flow from Agent 5 → evaluate_output →
    drive_conversation → judge. Any dropped hop here means Fix #2
    is silently broken."""

    def _read_source(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_agent5_dispatch_passes_input_context(self):
        src = self._read_source("puzzleeval/agents/implement_test_env.py")
        # Both dispatch paths must pass input_context
        assert 'input_context=getattr(tc_eval, "input_context", None) or {}' in src
        count = src.count('input_context=getattr(tc_eval, "input_context", None)')
        assert count >= 2, (
            f"Expected input_context forwarding in BOTH deterministic "
            f"AND tool_runner dispatch paths. Got {count} references."
        )

    def test_eval_context_has_input_context_field(self):
        from puzzleeval.plugin_tool_runner import EvalContext
        ctx = EvalContext(response=None, expected={}, criteria=[],
                          harness_runner=None)
        assert hasattr(ctx, "input_context")
        assert ctx.input_context == {}

    def test_evaluate_with_tool_runner_signature(self):
        from puzzleeval.plugin_tool_runner import evaluate_with_tool_runner
        sig = inspect.signature(evaluate_with_tool_runner)
        assert "input_context" in sig.parameters

    def test_voice_realtime_evaluate_output_signature(self):
        from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
        sig = inspect.signature(VoiceRealtimePlugin.evaluate_output)
        assert "input_context" in sig.parameters

    def test_voice_realtime_drive_conversation_has_agent_system_prompt(self):
        from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
        sig = inspect.signature(VoiceRealtimePlugin.drive_conversation)
        assert "agent_system_prompt" in sig.parameters

    def test_conversation_simulator_passes_agent_system_prompt_to_judge(self):
        src = self._read_source(
            "puzzleeval/tool_plugins/conversation_simulator.py"
        )
        # _evaluate_agentic must extract + forward agent_system_prompt
        assert "agent_system_prompt=agent_system_prompt" in src
