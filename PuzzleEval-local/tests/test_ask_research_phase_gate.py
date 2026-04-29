"""Regression guards for the ask_research Phase-1 gate.

Real-run trace f9de380b-69c0 (2026-04-22): ElevenLabs build called
ask_research TWICE before api_spec.txt was written. Both calls returned
text but the sub-agent had NO context to inherit (no spec excerpt, no
harness code, no prior errors) — so the "research" was generic API
discovery that defeats the whole context-inheritance design.

Architectural principle: ask_research is for FILLING GAPS in an
existing api_spec.txt during Phase 2+ debugging. Primary discovery
must be done by the builder itself via web_search + web_fetch so it
develops its own mental model of the API. The sub-agent's value is
targeted help WITH context; without context it has nothing to offer
that web_search wouldn't offer more cheaply.

AD-007: this is a safety-critical architectural contract, so
enforcement lives in deterministic code (the dispatch block refuses
the call), not just in prompt rules. This file locks both layers:
  - Deterministic: ask_research dispatch checks api_spec.txt exists
  - Prompt: ASK_RESEARCH_TOOL.description warns of the Phase-1 block
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class TestAskResearchToolDescriptionTeachesPhaseGate:
    """The tool description seen by Claude must explicitly warn that
    the tool refuses to run until api_spec.txt exists. Making the
    contract visible at tool-selection time is as important as
    enforcing it at dispatch time — if the model picks the tool in
    Phase 1 and gets refused, the refusal tokens were still wasted."""

    def _tool(self) -> dict:
        from puzzleeval.agents.implement_test_env import ASK_RESEARCH_TOOL
        return ASK_RESEARCH_TOOL

    def test_description_states_phase_gate(self):
        desc = self._tool()["description"]
        assert "PHASE-1 BLOCKED" in desc or "Phase 2+" in desc, (
            "Tool description must surface the Phase-1 gate at "
            "tool-selection time, not just at dispatch time."
        )
        assert "api_spec.txt" in desc

    def test_description_explains_why(self):
        desc = self._tool()["description"]
        # The explanation must mention the context-inheritance rationale
        assert "mental model" in desc.lower() or "inherit" in desc.lower()

    def test_description_gives_phase_1_alternative(self):
        desc = self._tool()["description"]
        # Tell Claude what to do instead in Phase 1
        assert "web_search" in desc and "web_fetch" in desc
        assert "Phase 1 pattern" in desc or "initial" in desc.lower()


class TestDispatchDeterministicBlock:
    """Source-grep guards: the dispatch block in implement_test_env.py
    must check api_spec.txt existence BEFORE enriching or invoking the
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

    def test_dispatch_checks_spec_exists_before_calling_sub_agent(self):
        src = self._source()
        # The gate must exist near the ask_research dispatch.
        # We look for the sentinel phrase that identifies the refusal path.
        assert "ask_research REFUSED" in src, (
            "Dispatch must contain a Phase-1 refusal branch. Without "
            "deterministic enforcement, Claude can still invoke "
            "ask_research in Phase 1 even with the prompt warning."
        )
        # Must be tied to api_spec.txt existence check
        assert 'spec_path.exists()' in src

    def test_dispatch_logs_phase1_block_as_warning(self):
        src = self._source()
        # Observability: the block should log so ops can grep for it
        assert '"operation": "ask_research_phase1_gate"' in src

    def test_refusal_message_points_to_web_search_fetch(self):
        src = self._source()
        # The refusal text must tell the builder what to do INSTEAD
        assert "WHAT TO DO INSTEAD" in src or "Phase 1 research pattern" in src
        assert "web_search" in src and "web_fetch" in src

    def test_refusal_returns_tool_result_not_raises(self):
        """The gate must return a tool_result (so Claude sees the
        refusal + can adjust) rather than crashing the run."""
        src = self._source()
        # Look for the continue statement after appending the refusal
        # — this is how the gate gracefully declines without tearing
        # down the build loop.
        block_start = src.find("ask_research REFUSED")
        assert block_start != -1
        # Within the next ~3000 chars (the refusal block), there must
        # be a `continue` to break out of the tool-dispatch branch.
        refusal_block = src[block_start:block_start + 3500]
        assert "continue" in refusal_block, (
            "Phase-1 refusal must `continue` (tool_result appended) "
            "rather than raise — the build loop keeps going with the "
            "refusal as the tool's output."
        )


class TestPhase1GateDoesNotRegressPhase2Behavior:
    """The gate must only fire when api_spec.txt is MISSING. Phase 2
    callers (with spec on disk) must proceed through normal enrichment
    exactly as before."""

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

    # Phase 8: deleted source-grep test `test_enrichment_path_still_runs_post_spec`.
    # Behavior covered by: tests/test_dispatch_helpers.py::TestEnrichResearchQuestion

    def test_ask_research_tool_still_registered(self):
        """Despite the gate, ask_research must still be in the tool
        list — builders that pass Phase 1 correctly still need it."""
        from puzzleeval.agents.implement_test_env import (
            ASK_RESEARCH_TOOL, CUSTOM_TOOL_NAMES,
        )
        assert ASK_RESEARCH_TOOL["name"] == "ask_research"
        assert "ask_research" in CUSTOM_TOOL_NAMES


class TestPhase1GateAppliesUniformlyAcrossProviders:
    """Generality check: the gate is provider-agnostic. It fires
    based on api_spec.txt existence, not on candidate name. Nothing
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
        # Pull the Phase-1 refusal block (bounded by the REFUSED
        # sentinel and the `continue` statement that ends it)
        start = src.find("ask_research REFUSED")
        assert start != -1
        # Look ~2500 chars ahead for the `continue` statement
        block = src[start:start + 2500]
        # Must not mention specific provider names — language is
        # provider-agnostic
        forbidden_hardcodes = (
            "ElevenLabs", "OpenAI", "Mindee", "Veryfi", "Anthropic",
            "voice", "OCR", "plumbing", "Twilio",
        )
        for name in forbidden_hardcodes:
            assert name not in block, (
                f"Phase-1 refusal block mentions specific provider/modality "
                f"{name!r} — the gate must be provider/modality-agnostic."
            )
