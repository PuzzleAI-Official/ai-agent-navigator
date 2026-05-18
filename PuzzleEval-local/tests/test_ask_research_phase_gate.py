"""Regression guards for the scoped ask_research knowledge-gap gate.`r`n`r`nask_research is for declared planned research tasks, failure-packet gaps,`r`nand concrete FIELD NEEDED / WHY debug gaps. Broad provider discovery is`r`nblocked because it wastes turns and bypasses durable synthesis.`r`n`r`nAD-007: this is a safety-critical architecture contract, so enforcement`r`nlives in deterministic code and the tool description teaches the shape at`r`ntool-selection time.`r`n"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class TestAskResearchToolDescriptionTeachesScopeGate:
    """The tool description seen by Claude must explicitly warn that
    the tool refuses broad discovery. Making the
    contract visible at tool-selection time is as important as
    enforcing it at dispatch time â€” if the model picks the tool in
    wrong shape and gets refused, the refusal tokens were still wasted."""

    def _tool(self) -> dict:
        from puzzleeval.agents.implement_test_env import ASK_RESEARCH_TOOL
        return ASK_RESEARCH_TOOL

    def test_description_states_phase_gate(self):
        desc = self._tool()["description"]
        assert "DEFAULT KNOWLEDGE-GAP GATE" in desc, (
            "Tool description must surface the knowledge-gap gate at "
            "tool-selection time, not just at dispatch time."
        )
        assert "_agent_state/research_plan.json" in desc
        assert "FIELD NEEDED" in desc
        assert "WHY" in desc
        assert "task_id" in desc

    def test_description_explains_why(self):
        desc = self._tool()["description"]
        # The explanation must mention the context-inheritance rationale
        assert "mental model" in desc.lower() or "inherit" in desc.lower()

    def test_description_gives_phase_1_alternative(self):
        desc = self._tool()["description"]
        # Tell Claude what to do instead for multi-gap research.
        assert "research_tasks" in desc and "official docs entrypoint" in desc
        assert "Planned research pattern" in desc or "planned" in desc.lower()


class TestDispatchDeterministicBlock:
    """Source-grep guards: the dispatch block in implement_test_env.py
    must check research_plan.json existence BEFORE enriching or invoking the
    sub-agent. Safety contract enforced in code (AD-007), not prompts."""

    def _source(self) -> str:
        """Phase 5 Step 3: dispatch block lives in build_loop.py now.
        Combined source so source-grep finds the literals wherever they
        landed across the move."""
        impl = (
            ROOT / "puzzleeval" / "agents" / "implement_test_env.py"
        ).read_text(encoding="utf-8")
        build_loop = (
            ROOT / "puzzleeval" / "agents" / "agent5" / "build_loop.py"
        ).read_text(encoding="utf-8")
        return impl + "\n# === build_loop.py ===\n" + build_loop

    def test_dispatch_checks_research_plan_exists_before_calling_sub_agent(self):
        src = self._source()
        # The gate must exist near the ask_research dispatch.
        # We look for the sentinel phrase that identifies the refusal path.
        assert "ask_research BLOCKED" in src, (
            "Dispatch must contain a scoped refusal branch. Without "
            "deterministic enforcement, Claude can still invoke "
            "ask_research before planning even with the prompt warning."
        )
        assert "RESEARCH_WORKERS_ENABLED" in src
        assert "validate_ask_research_scope" in src
        assert "FIELD NEEDED" in src

    def test_dispatch_logs_plan_block_as_warning(self):
        src = self._source()
        # Observability: the block should log so ops can grep for it
        assert "ask_research_scope_gate" in src

    def test_refusal_message_points_to_research_plan(self):
        src = self._source()
        # The refusal text must tell the builder what to do INSTEAD
        assert "research_plan.json" in src
        assert "FIELD NEEDED" in src
        assert "debug gap" in src

    def test_refusal_returns_tool_result_not_raises(self):
        """The gate must return a tool_result (so Claude sees the
        refusal + can adjust) rather than crashing the run."""
        src = self._source()
        # Look for the continue statement after appending the refusal
        # â€” this is how the gate gracefully declines without tearing
        # down the build loop.
        block_start = src.find("ask_research BLOCKED")
        assert block_start != -1
        # Within the next ~3000 chars (the refusal block), there must
        # be a `continue` to break out of the tool-dispatch branch.
        refusal_block = src[block_start:block_start + 3500]
        assert "continue" in refusal_block, (
                "Scoped refusal must `continue` (tool_result appended) "
            "rather than raise â€” the build loop keeps going with the "
            "refusal as the tool's output."
        )


class TestAskResearchGateKeepsScopedBehavior:
    """The default gate blocks broad discovery and keeps scoped research available."""

    def _source(self) -> str:
        """Combined source: implement_test_env.py + dispatch_helpers.py
        + build_loop.py.

        Phase 4 Path B Step 3: enrichment moved to
        ``agent5/dispatch_helpers.enrich_research_question``.
        Phase 5 Step 3: ``_build_single_harness`` body moved to
        ``agent5/build_loop.build_single_harness``.
        Combined source covers both moves.
        """
        impl = (
            ROOT / "puzzleeval" / "agents" / "implement_test_env.py"
        ).read_text(encoding="utf-8")
        dispatch = (
            ROOT / "puzzleeval" / "agents" / "agent5" / "dispatch_helpers.py"
        ).read_text(encoding="utf-8")
        build_loop = (
            ROOT / "puzzleeval" / "agents" / "agent5" / "build_loop.py"
        ).read_text(encoding="utf-8")
        return (
            impl
            + "\n# === dispatch_helpers.py ===\n" + dispatch
            + "\n# === build_loop.py ===\n" + build_loop
        )

    # Enrichment behavior is covered directly by TestEnrichResearchQuestion.
    # Behavior covered by: tests/test_dispatch_helpers.py::TestEnrichResearchQuestion

    def test_ask_research_tool_still_registered(self):
        """Despite the gate, ask_research must still be in the tool
        list - builders with scoped gaps still need it."""
        from puzzleeval.agents.implement_test_env import (
            ASK_RESEARCH_TOOL, CUSTOM_TOOL_NAMES,
        )
        assert ASK_RESEARCH_TOOL["name"] == "ask_research"
        assert "ask_research" in CUSTOM_TOOL_NAMES


class TestAskResearchGateAppliesUniformlyAcrossProviders:
    """Generality check: the gate is provider-agnostic. It fires
    based on research_plan.json existence by default, not on candidate name. Nothing
    in the dispatch block hardcodes ElevenLabs / OpenAI / voice /
    any specific modality."""

    def test_gate_has_no_provider_hardcoding(self):
        # Phase 5 Step 3: refusal block lives in build_loop.py now.
        impl = (
            ROOT / "puzzleeval" / "agents" / "implement_test_env.py"
        ).read_text(encoding="utf-8")
        build_loop = (
            ROOT / "puzzleeval" / "agents" / "agent5" / "build_loop.py"
        ).read_text(encoding="utf-8")
        src = impl + "\n# === build_loop.py ===\n" + build_loop
        # Pull the planned-research refusal block (bounded by the BLOCKED
        # sentinel and the `continue` statement that ends it).
        start = src.find("ask_research BLOCKED")
        assert start != -1
        # Look ~2500 chars ahead for the `continue` statement
        block = src[start:start + 2500]
        # Must not mention specific provider names â€” language is
        # provider-agnostic
        forbidden_hardcodes = (
            "ElevenLabs", "OpenAI", "Mindee", "Veryfi", "Anthropic",
            "voice", "OCR", "plumbing", "Twilio",
        )
        for name in forbidden_hardcodes:
            assert name not in block, (
                f"ask_research refusal block mentions specific provider/modality "
                f"{name!r} â€” the gate must be provider/modality-agnostic."
            )

