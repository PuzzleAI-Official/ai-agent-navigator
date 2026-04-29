"""End-to-end wiring regression guards for the agentic conversational path.

Before this pass, five separate wiring gaps silently dropped
persona/goal/rubric between Agent 3's emission and the plugin's
evaluation:
  1. Agent 5's evaluator dispatch didn't forward them
  2. plugin_tool_runner's EvalContext had no fields for them
  3. voice_realtime.evaluate_output didn't accept them
  4. voice_realtime._evaluate_conversation didn't accept or forward them
  5. TestCaseResult never received rubric_verdict + transcript from the
     plugin's verdict.detail

This module locks each wiring seam so a future refactor can't silently
regress the pipeline back to "agentic emission + scripted evaluation"
(the worst-of-both-worlds state that would make real runs misleading).

Covers:
  - EvalContext carries the agentic kit
  - evaluate_with_tool_runner signature accepts it
  - Direct-invoke forwards it + returns verdict_detail
  - voice_realtime.evaluate_output accepts it
  - voice_realtime._evaluate_conversation accepts it
  - conversation_simulator.evaluate_output accepts it via **kwargs
  - Agent 5 source grep confirms the 3 call sites pass it
  - _promote_verdict_to_tcr populates rubric_verdict + transcript
  - Hybrid mode is rejected at the schema layer
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class TestEvalContextThreading:
    """EvalContext must carry agentic fields — they're the bridge from
    Agent 5 to the plugin's drive_conversation."""

    def test_eval_context_has_agentic_fields(self):
        from puzzleeval.plugin_tool_runner import EvalContext
        ctx = EvalContext(response=None, expected={}, criteria=[], harness_runner=None)
        for f in ("persona", "goal", "constraints", "rubric",
                  "max_turns", "evaluation_mode", "trace_id"):
            assert hasattr(ctx, f), f"EvalContext missing agentic field {f!r}"

    def test_eval_context_defaults_are_scripted_compatible(self):
        """Defaults must not trigger agentic mode — legacy tests without
        agentic kwargs still go through scripted path."""
        from puzzleeval.plugin_tool_runner import EvalContext
        ctx = EvalContext(response=None, expected={}, criteria=[], harness_runner=None)
        assert ctx.persona is None
        assert ctx.goal is None
        assert ctx.rubric == []
        assert ctx.evaluation_mode == "auto"

    def test_evaluate_with_tool_runner_signature_has_agentic_kwargs(self):
        from puzzleeval.plugin_tool_runner import evaluate_with_tool_runner
        sig = inspect.signature(evaluate_with_tool_runner)
        for kw in ("persona", "goal", "constraints", "rubric",
                   "max_turns", "evaluation_mode"):
            assert kw in sig.parameters, (
                f"evaluate_with_tool_runner signature missing {kw!r} — "
                f"the kwarg must flow from Agent 5 into EvalContext."
            )


class TestToolRunnerVerdictCarriesDetail:
    """ToolRunnerVerdict must carry verdict_detail so _promote_verdict_
    to_tcr can populate rubric_verdict + transcript on TestCaseResult."""

    def test_verdict_detail_field_present(self):
        from puzzleeval.plugin_tool_runner import ToolRunnerVerdict
        v = ToolRunnerVerdict(
            passed=True, score=0.9, reasoning="ok",
            tools_invoked=[], artifacts=[],
            cost_usd=0.0, iterations=0,
        )
        assert hasattr(v, "verdict_detail")
        assert v.verdict_detail == {}

    def test_verdict_detail_round_trip(self):
        from puzzleeval.plugin_tool_runner import ToolRunnerVerdict
        v = ToolRunnerVerdict(
            passed=True, score=0.8, reasoning="",
            tools_invoked=[], artifacts=[], cost_usd=0.0, iterations=0,
            verdict_detail={"rubric_verdict": {"overall_score": 0.8, "passed": True,
                                                "criterion_scores": [],
                                                "conversation_summary": "clean"}},
        )
        assert v.verdict_detail["rubric_verdict"]["overall_score"] == 0.8


class TestVoiceRealtimeAcceptsAgenticKwargs:
    """voice_realtime.evaluate_output + _evaluate_conversation must
    accept and forward persona/goal/rubric — last hop before
    drive_conversation."""

    def test_evaluate_output_signature(self):
        from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
        sig = inspect.signature(VoiceRealtimePlugin.evaluate_output)
        for kw in ("persona", "goal", "constraints", "rubric",
                   "max_turns", "evaluation_mode", "trace_id"):
            assert kw in sig.parameters, (
                f"voice_realtime.evaluate_output missing {kw!r}"
            )

    def test_evaluate_conversation_signature(self):
        from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
        sig = inspect.signature(VoiceRealtimePlugin._evaluate_conversation)
        for kw in ("persona", "goal", "constraints", "rubric",
                   "max_turns", "evaluation_mode", "trace_id"):
            assert kw in sig.parameters


class TestAgent5CallSitesForwardAgenticKwargs:
    """Source-grep guards: the three call sites in implement_test_env.py
    that invoke plugin evaluation must forward persona/goal/rubric."""

    def _read_agent5_src(self) -> str:
        return (
            ROOT / "puzzleeval" / "agents" / "implement_test_env.py"
        ).read_text(encoding="utf-8")

    def test_evaluator_dispatch_passes_persona(self):
        src = self._read_agent5_src()
        # The deterministic evaluator dispatch call site must extract
        # persona from the TestCase and pass it to evaluate_output.
        assert 'persona=getattr(tc_eval, "persona", None)' in src, (
            "Agent 5's deterministic evaluator dispatch must pass "
            "tc_eval.persona to evaluate_output. Without this, the "
            "plugin's agentic path never fires and every conversational "
            "test silently falls back to scripted."
        )

    def test_tool_runner_call_passes_agentic_context(self):
        src = self._read_agent5_src()
        # The tool_runner call site must also pass persona etc.
        assert 'persona=getattr(tc_eval, "persona", None)' in src
        # Both call sites should show up — count them to catch
        # accidental deletion of either.
        assert src.count('persona=getattr(tc_eval, "persona", None)') >= 2, (
            "Expected persona forwarding in BOTH the deterministic "
            "dispatch AND the tool_runner path. Missing one means "
            "half the evaluation strategies ignore agentic fields."
        )

    def test_promote_verdict_to_tcr_populates_rubric_and_transcript(self):
        src = self._read_agent5_src()
        # The promotion function must pull rubric_verdict + transcript
        # out of verdict_detail and set them on TestCaseResult. Without
        # this, the frontend never sees the rubric breakdown.
        assert "tcr_target.rubric_verdict" in src, (
            "_promote_verdict_to_tcr must assign to tcr_target.rubric_"
            "verdict so the evaluation report surfaces the judge's "
            "per-criterion scores."
        )
        assert "tcr_target.transcript" in src, (
            "_promote_verdict_to_tcr must assign to tcr_target.transcript"
            " so the UI can render the full conversation."
        )

    def test_promote_verdict_to_tcr_accepts_verdict_detail(self):
        from puzzleeval.agents.implement_test_env import _build_single_harness
        # _promote_verdict_to_tcr is a nested closure — we can't
        # introspect its signature directly. Instead grep for the
        # param name in source.
        src = (
            ROOT / "puzzleeval" / "agents" / "implement_test_env.py"
        ).read_text(encoding="utf-8")
        assert "verdict_detail: dict | None = None" in src


class TestHybridModeIsDropped:
    """Hybrid was a diagnostic A/B feature the user doesn't need —
    scripted is strictly worse than agentic for multi-turn so there's
    nothing to compare. Lock that hybrid stays rejected at the schema
    layer and removed from plugin code."""

    def test_hybrid_rejected_at_schema(self):
        from puzzleeval.schemas import TestCase, JudgementCriterion
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            TestCase(
                id="x", sub_task_ref="y", scenario="z",
                input_type="voice_conversation",
                input_data="{}", output_type="voice_turn", expected_output="{}",
                judgement_criteria=[
                    JudgementCriterion(
                        criterion="c", eval_type="subjective_quality",
                        weight=1.0,
                    ),
                ],
                difficulty="easy", tags=["happy_path"],
                evaluation_mode="hybrid",
            )

    def test_hybrid_paths_removed_from_voice_realtime(self):
        src = (
            ROOT / "puzzleeval" / "tool_plugins" / "voice_realtime.py"
        ).read_text(encoding="utf-8")
        # Code-level hybrid branches gone; only comment-level mentions
        # tolerated for historical context.
        assert 'evaluation_mode == "hybrid"' not in src
        assert 'effective_mode == "hybrid"' not in src
        assert '("agentic", "hybrid")' not in src

    def test_hybrid_paths_removed_from_conversation_simulator(self):
        src = (
            ROOT / "puzzleeval" / "tool_plugins" / "conversation_simulator.py"
        ).read_text(encoding="utf-8")
        assert 'effective_mode == "hybrid"' not in src
        assert 'effective_mode in ("agentic", "hybrid")' not in src
        assert 'hybrid_scripted' not in src


class TestAgent1ConversationalTestCountRule:
    """Agent 1 prompt must teach that conversational scopes use N
    AGENTIC CONVERSATIONS, not 'N synthetic scripts'. This is what
    makes the test plan coherent with the agentic evaluation path."""

    def _read_agent1_src(self) -> str:
        return ((
            ROOT / "puzzleeval" /"agents" / "agent1" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent1" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))

    def test_prompt_distinguishes_conversational_from_dimension_count(self):
        src = self._read_agent1_src()
        # Explicit rule that conversational scopes have a different
        # test_count_target derivation than the 6-dimensions formula
        assert "conversation" in src.lower()
        assert "AGENTIC CONVERSATION" in src.upper() or "agentic conversation" in src.lower()

    def test_prompt_has_conversational_worked_example(self):
        src = self._read_agent1_src()
        # The example test_plan for voice agent with agentic conversational scope
        assert '"voice_conversation"' in src
        # Demonstrates reference_mode=exemplar for conversational
        assert 'reference_mode' in src and 'exemplar' in src

    def test_prompt_explicitly_warns_against_synthetic_scripts_framing(self):
        """The prompt must EXPLICITLY tell Agent 1 not to describe
        conversational tests as 'synthetic scripts' in notes — that
        framing confuses Agent 3 downstream."""
        src = self._read_agent1_src()
        # Either a positive "LIVE AGENTIC CONVERSATIONS" instruction or
        # a negative "NEVER describe ... as synthetic scripts" instruction
        # must appear in the rules block.
        assert (
            "agentic conversations" in src.lower()
            and "synthetic scripts" in src.lower()
        ), "Agent 1 prompt must contrast agentic-conversations vs synthetic-scripts framing explicitly"


class TestAgent3ForcesAgenticForConversational:
    """Agent 3 prompt must make agentic the REQUIRED path for
    conversational tests — not just the default."""

    def _read_agent3_src(self) -> str:
        return ((
            ROOT / "puzzleeval" /"agents" / "agent3" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent3" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))

    def test_prompt_mandates_agentic_for_conversational(self):
        src = self._read_agent3_src()
        assert "MUST use the AGENTIC path" in src, (
            "Agent 3 prompt must mandate the agentic path for "
            "conversational tests, not merely 'prefer' it."
        )

    def test_prompt_warns_against_emitting_static_script(self):
        src = self._read_agent3_src()
        assert "DO NOT emit" in src and "conversation_script" in src

    def test_prompt_explicit_evaluation_mode_agentic(self):
        src = self._read_agent3_src()
        # The field guidance must say set evaluation_mode='agentic'
        # explicitly (not just leave as 'auto') so the validator warns
        # on missing fields rather than silently falling back.
        assert 'set to `"agentic"` explicitly' in src

    def test_legacy_scripted_mode_callout_still_present(self):
        """Back-compat: the LEGACY scripted section must still be
        documented so existing fixtures don't confuse Agent 3."""
        src = self._read_agent3_src()
        assert "LEGACY scripted mode" in src
