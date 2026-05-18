"""Tests for ``puzzleeval.agents.agent5.objective_synthesis``.

Covers the orchestrator-side pure function that generates
``_agent_state/objective.md`` from upstream Agent 1-4 outputs.
This is PR 1 of the Goal/Planning/State/Reflection plan.
"""

from __future__ import annotations

import pytest

from puzzleeval.agents.agent5 import objective_synthesis
from puzzleeval.schemas import (
    Agent3Result,
    Agent5Input,
    Constraints,
    JudgementCriterion,
    ScreenedCandidate,
    SubTask,
    TestCase,
    UserUnderstandingOutput,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user_understanding() -> UserUnderstandingOutput:
    return UserUnderstandingOutput(
        summary="Compare voice agents for inbound plumbing calls",
        sub_tasks=[
            SubTask(
                description="Receive inbound call",
                capability="voice intake",
                search_keywords=["voice agent", "inbound call"],
            ),
            SubTask(
                description="Respond in natural voice",
                capability="text-to-speech",
                search_keywords=["voice synthesis"],
            ),
        ],
        search_strategy="both",
        domain="home_services",
        search_keywords=["voice agent inbound calls"],
        constraints=Constraints(
            budget_range="$50-200/mo",
            must_have_features=["multi-turn"],
            integration_requirements=[],
            technical_level="non-technical",
        ),
    )


def _make_candidate(name: str = "ElevenLabs ConvAI") -> ScreenedCandidate:
    return ScreenedCandidate(
        name=name,
        provider="ElevenLabs",
        description="Conversational AI voice platform",
        pricing_model="per-second",
        pricing_details="$0.10 per second",
        claimed_capabilities=["voice synthesis", "conversation"],
        relevance_score=0.95,
        adoption_difficulty="medium",
        relevant_subtasks=["Receive inbound call", "Respond in natural voice"],
        source="https://elevenlabs.io",
        verified_api_docs_url="https://elevenlabs.io/docs/conversational-ai",
        auth_method="api_key",
        api_access_method="paid_signup",
        confirmed_capabilities=["voice synthesis"],
        data_format_notes="WebSocket + HTTP",
        screening_notes="Verified docs URL",
    )


def _make_test_cases() -> Agent3Result:
    cases = [
        TestCase(
            id=f"tc-{i:03d}",
            sub_task_ref="Receive inbound call",
            scenario=f"Caller {i}",
            input_type="voice_conversation",
            output_type="audio_content",
            input_data=f"Hello scenario {i}",
            expected_output="Acknowledgment",
            difficulty="medium",
            tags=["happy_path"],
            judgement_criteria=[
                JudgementCriterion(
                    criterion="Must respond clearly",
                    weight=1.0,
                    eval_type="rubric_score",
                ),
            ],
        )
        for i in range(1, 6)
    ]
    return Agent3Result(
        test_cases=cases,
        generation_notes="5 voice scenarios",
        coverage_summary={"Receive inbound call": 5},
    )


def _make_input() -> Agent5Input:
    return Agent5Input(
        validated_candidates=[_make_candidate()],
        user_understanding=_make_user_understanding(),
        test_cases=_make_test_cases(),
        trace_id="test-trace",
    )


# ---------------------------------------------------------------------------
# Determinism + content invariants
# ---------------------------------------------------------------------------


class TestSynthesizeObjectiveDeterministic:
    """Same inputs → same output, every time. No model variance."""

    def test_same_inputs_produce_same_output(self):
        candidate = _make_candidate()
        input_data = _make_input()
        kwargs = {
            "modality_playbook_ids": ["voice", "streaming_response"],
            "effective_max_turns": 65,
            "effective_max_budget_usd": 5.0,
            "platform": "windows",
        }
        a = objective_synthesis.synthesize_objective(
            candidate=candidate, input_data=input_data, **kwargs
        )
        b = objective_synthesis.synthesize_objective(
            candidate=candidate, input_data=input_data, **kwargs
        )
        assert a == b


class TestObjectiveSections:
    """Every required section header appears in the output."""

    def setup_method(self):
        self.md = objective_synthesis.synthesize_objective(
            candidate=_make_candidate(),
            input_data=_make_input(),
            modality_playbook_ids=["voice", "streaming_response"],
            effective_max_turns=65,
            effective_max_budget_usd=5.0,
            platform="windows",
        )

    def test_all_section_headers_present(self):
        for header in objective_synthesis.expected_section_headers():
            assert header in self.md, f"Missing section header: {header}"

    def test_deliverable_includes_candidate_name(self):
        assert "ElevenLabs ConvAI" in self.md

    def test_deliverable_includes_user_summary(self):
        assert "Compare voice agents for inbound plumbing calls" in self.md

    def test_deliverable_lists_relevant_subtasks(self):
        assert "Receive inbound call" in self.md
        assert "Respond in natural voice" in self.md

    def test_success_criteria_enumerates_six_adversarial_probes(self):
        for probe_name, _ in objective_synthesis.ADVERSARIAL_PROBES:
            assert f"**{probe_name}**" in self.md, f"Probe missing: {probe_name}"

    def test_success_criteria_includes_test_count(self):
        # 5 test cases in the fixture
        assert "All 5 test cases" in self.md

    def test_constraints_includes_modality_playbooks(self):
        assert "voice" in self.md
        assert "streaming_response" in self.md

    def test_constraints_includes_budget(self):
        assert "65 turns" in self.md
        assert "$5.00" in self.md

    def test_constraints_includes_platform(self):
        assert "windows" in self.md

    def test_constraints_includes_auth_method(self):
        assert "api_key" in self.md

    def test_constraints_includes_verified_docs_url(self):
        assert "https://elevenlabs.io/docs/conversational-ai" in self.md

    def test_out_of_scope_lists_standard_items(self):
        assert "State leakage across independent test cases" in self.md
        assert "conversation/session-local state is allowed" in self.md
        assert "Retry logic" in self.md

    def test_candidate_notes_section_points_to_observations(self):
        # The objective is read-only; agent-authored notes belong in
        # agent_observations.json.
        assert "## CANDIDATE NOTES" in self.md
        assert "agent_observations.json" in self.md


class TestObjectiveSourceAudit:
    def test_audit_accepts_synthesized_objective(self):
        candidate = _make_candidate()
        input_data = _make_input()
        md = objective_synthesis.synthesize_objective(
            candidate=candidate,
            input_data=input_data,
            modality_playbook_ids=["voice", "streaming_response"],
            effective_max_turns=65,
            effective_max_budget_usd=5.0,
            platform="windows",
        )

        verdict = objective_synthesis.audit_objective_source_consistency(
            md,
            candidate=candidate,
            input_data=input_data,
            modality_playbook_ids=["voice", "streaming_response"],
            platform="windows",
        )

        assert verdict.ok is True
        assert verdict.issues == []

    def test_audit_rejects_objective_with_wrong_candidate(self):
        candidate = _make_candidate()
        input_data = _make_input()
        md = objective_synthesis.synthesize_objective(
            candidate=candidate,
            input_data=input_data,
            modality_playbook_ids=["voice"],
            effective_max_turns=65,
            effective_max_budget_usd=5.0,
            platform="windows",
        ).replace(candidate.name, "Wrong Candidate")

        verdict = objective_synthesis.audit_objective_source_consistency(
            md,
            candidate=candidate,
            input_data=input_data,
            modality_playbook_ids=["voice"],
            platform="windows",
        )

        assert verdict.ok is False
        assert any("candidate name" in issue for issue in verdict.issues)


class TestObjectiveSubtaskFallback:
    """When candidate.relevant_subtasks is empty, fall back to user's sub_tasks."""

    def test_empty_relevant_subtasks_uses_user_subtasks(self):
        cand = _make_candidate()
        cand_data = cand.model_dump()
        cand_data["relevant_subtasks"] = []
        cand_empty = ScreenedCandidate(**cand_data)
        md = objective_synthesis.synthesize_objective(
            candidate=cand_empty,
            input_data=_make_input(),
            modality_playbook_ids=[],
            effective_max_turns=40,
            effective_max_budget_usd=3.0,
            platform="linux",
        )
        # Falls back to the user_understanding sub_task descriptions
        assert "Receive inbound call" in md
        assert "Respond in natural voice" in md


class TestExpectedSectionHeadersStable:
    """The list of headers is a contract — tests catch if someone renames a section."""

    def test_returns_five_headers(self):
        headers = objective_synthesis.expected_section_headers()
        assert len(headers) == 5
        assert all(h.startswith("## ") for h in headers)
