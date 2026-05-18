"""Regression guards for the current Agent 5 research/build prompt contract."""

from __future__ import annotations

from pathlib import Path

from puzzleeval.agents.implement_test_env import (
    BUILDER_SYSTEM_PROMPT,
    _ask_research_template_adherence,
    _format_prefetched_docs_block,
    _usefulness_signal,
)


class TestResearchPromptContract:
    def test_phase_1_keeps_research_plan_synthesis_plan_order(self):
        prompt = BUILDER_SYSTEM_PROMPT
        plan = prompt.find("_agent_state/research_plan.json")
        synthesis = prompt.find("_agent_state/research_synthesis.json")
        implementation = prompt.find("_agent_state/implementation_plan.json")
        assert 0 < plan < synthesis < implementation

    def test_phase_1_uses_docs_entrypoint_and_not_retired_spec_or_checklist(self):
        prompt = BUILDER_SYSTEM_PROMPT
        phase_start = prompt.find("## PHASE 1")
        phase_end = prompt.find("## PHASE 2")
        phase1 = prompt[phase_start:phase_end]
        assert "docs_entrypoint.json" in phase1
        assert "research_handoff.json" in phase1
        assert "api_spec.txt" not in phase1
        assert "BuildReadinessChecklist" not in phase1

    def test_ask_research_template_is_scoped(self):
        prompt = BUILDER_SYSTEM_PROMPT
        idx = prompt.find("CANDIDATE:")
        section = prompt[idx: idx + 900]
        for field in ("CANDIDATE:", "ENDPOINT:", "KNOWN:", "FIELD NEEDED:", "WHY:"):
            assert field in section
        assert "broad provider discovery" in prompt

    def test_build_phase_requires_vertical_slice_after_plan(self):
        prompt = BUILDER_SYSTEM_PROMPT
        phase2 = prompt[prompt.find("## PHASE 2"):]
        assert "WRITE harness.py" in phase2
        assert "python smoke_test.py" in phase2
        assert "WRITE harness.py BEFORE ANY INSPECTION SCRIPTS" in phase2

    def test_readiness_criteria_not_old_checklist(self):
        prompt = BUILDER_SYSTEM_PROMPT
        assert "Implementation-plan readiness criteria" in prompt
        assert "Implementation-plan readiness checklist" not in prompt
        assert "Agent 4 checklist" not in prompt


class TestPrefetchedDocsBlock:
    def test_empty_string_when_sandbox_dir_is_none(self):
        assert _format_prefetched_docs_block(None) == ""

    def test_renders_ranked_prefetched_docs_without_stale_policy(self, tmp_path: Path):
        (tmp_path / "fetched_docs_0.txt").write_text(
            "# Fetched from: https://docs.example.test\n\nPOST /v1/demo\nAuthorization: Bearer",
            encoding="utf-8",
        )
        rendered = _format_prefetched_docs_block(tmp_path)
        assert "fetched_docs_0.txt" in rendered
        assert "docs-entrypoint artifact is the authorization source" in rendered
        assert "checklist" not in rendered.lower()

    def test_usefulness_signal_is_ranking_hint(self):
        rich = "```python\nx=1\n```\nPOST /v1/demo\nAuthorization: Bearer"
        plain = "Welcome to our docs"
        assert _usefulness_signal(rich) > _usefulness_signal(plain)


class TestAskResearchTemplateTelemetry:
    def test_adherence_counts_named_fields(self):
        question = (
            "CANDIDATE: Example\nENDPOINT: POST /v1/demo\nKNOWN: auth exists\n"
            "FIELD NEEDED: response schema\nWHY: parser depends on it"
        )
        result = _ask_research_template_adherence(question)
        assert result["fully_adherent"] is True
        assert len(result["fields_present"]) >= 5
        assert result["fields_missing"] == []
