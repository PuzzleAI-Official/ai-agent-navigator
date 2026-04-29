"""Tests for the plugin wiring (the part that was missing in the prior pass).

Covers:
  - plugin_status: snapshot, advisory dedup, text table, dict serialization
  - PLUGIN_WIRING declarations match the actual implementations
  - TestCaseResult.tools_used field defaults / round-trip
  - _needs_plugin_synthesis / _synthesize_test_input_via_plugin behavior
  - Pipeline summary surfaces plugin advisories
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.plugin_status import (
    PLUGIN_WIRING,
    PluginStatus,
    collect_advisories,
    format_text_table,
    snapshot_all,
    snapshot_plugin,
    to_dict,
)
from puzzleeval.tool_plugins import get_plugin


# ---------------------------------------------------------------------------
# plugin_status
# ---------------------------------------------------------------------------


class TestPluginStatusSnapshot:
    def test_snapshot_has_one_row_per_registered_plugin(self):
        snap = snapshot_all()
        names = {s.name for s in snap}
        assert names >= {
            "code_execution", "vision", "transcription", "tts",
            "conversation_simulator",
        }

    def test_snapshot_carries_capabilities(self):
        ce = snapshot_plugin(get_plugin("code_execution"))
        assert "code" in ce.capabilities_input_types
        assert ce.synthesizes_input is True
        assert ce.evaluates_output is True

    def test_credential_status_set_when_env_present(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test_value")
        snap = snapshot_plugin(get_plugin("transcription"))
        creds = {c.name: c.is_set for c in snap.declared_credentials}
        assert creds["OPENAI_API_KEY"] is True

    def test_credential_status_unset_when_env_missing(self, monkeypatch):
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY"):
            monkeypatch.delenv(v, raising=False)
        snap = snapshot_plugin(get_plugin("transcription"))
        for c in snap.declared_credentials:
            assert c.is_set is False
            assert c.advice  # Each missing cred has actionable advice

    def test_wiring_metadata_attached(self):
        ce = snapshot_plugin(get_plugin("code_execution"))
        assert "agent_5_evaluator_dispatch" in ce.wired_for_evaluation_in
        assert "agent_5_test_input_synthesis" in ce.wired_for_synthesis_in
        assert "agent_5_builder_initial_message" in ce.wired_for_builder_context_in

    def test_to_dict_includes_summary_counts(self):
        snap = snapshot_all()
        d = to_dict(snap)
        assert d["summary"]["registered_count"] == len(snap)
        assert d["summary"]["wired_for_evaluation_count"] >= 1
        assert d["summary"]["wired_for_synthesis_count"] >= 1


class TestAdvisories:
    def test_no_advisories_when_all_credentialed(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        snap = snapshot_all()
        adv = collect_advisories(snap)
        # transcription + tts + vision should all be available now;
        # code_execution + conversation_simulator have no creds requirement
        # so they're always available
        names_with_advisories = [a.split("'")[1] for a in adv if "'" in a]
        assert "transcription" not in names_with_advisories
        assert "tts" not in names_with_advisories
        assert "vision" not in names_with_advisories

    def test_advisory_lists_all_alternatives_for_any_of_plugins(self, monkeypatch):
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "PUZZLEEVAL_STT_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        snap = snapshot_all()
        adv = collect_advisories(snap)
        transcription_advisories = [a for a in adv if "transcription" in a]
        assert len(transcription_advisories) == 1
        # Should mention all three alternatives
        assert "OPENAI_API_KEY" in transcription_advisories[0]
        assert "DEEPGRAM_API_KEY" in transcription_advisories[0]
        assert "ASSEMBLYAI_API_KEY" in transcription_advisories[0]


class TestTextTable:
    def test_format_renders_all_plugins(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        snap = snapshot_all()
        text = format_text_table(snap)
        assert "PLUGIN STATUS" in text
        for name in ("code_execution", "transcription", "tts", "vision",
                     "conversation_simulator"):
            assert name in text

    def test_format_marks_blocked_plugins(self, monkeypatch):
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "ELEVENLABS_API_KEY"):
            monkeypatch.delenv(v, raising=False)
        snap = snapshot_all()
        text = format_text_table(snap)
        assert "[BLOCKED]" in text
        assert "[READY]" in text


# ---------------------------------------------------------------------------
# PLUGIN_WIRING accuracy guards
# ---------------------------------------------------------------------------


class TestWiringAccuracy:
    def test_every_registered_plugin_has_wiring_entry(self):
        from puzzleeval.tool_plugins import list_plugins
        for plugin in list_plugins():
            assert plugin.name in PLUGIN_WIRING, (
                f"{plugin.name} is registered but has no PLUGIN_WIRING entry. "
                f"Add a row in plugin_status.py so the readiness matrix is honest."
            )

    def test_evaluator_dispatch_actually_imports_into_agent5(self):
        """Source-grep guard: when PLUGIN_WIRING claims a plugin is wired
        into agent_5_evaluator_dispatch, verify the dispatch code actually
        references the plugins package."""
        impl_text = (
            Path(__file__).parent.parent
            / "puzzleeval" / "agents" / "implement_test_env.py"
        ).read_text(encoding="utf-8")
        # The dispatch block uses these calls
        assert "detect_for_test_case" in impl_text
        assert "evaluate_output" in impl_text
        assert "tools_used" in impl_text

    def test_synthesis_path_imports_plugins(self):
        # Phase 6.1 note: synthesize_test_input_via_plugin moved to
        # agent5/execution.py. Combined source covers both files.
        root = Path(__file__).parent.parent
        impl_text = (
            (root / "puzzleeval" / "agents" / "implement_test_env.py").read_text(encoding="utf-8")
            + "\n# === execution.py ===\n"
            + (root / "puzzleeval" / "agents" / "agent5" / "execution.py").read_text(encoding="utf-8")
        )
        assert "find_plugins_for_input_type" in impl_text
        assert "synthesize_input" in impl_text


# ---------------------------------------------------------------------------
# TestCaseResult.tools_used
# ---------------------------------------------------------------------------


class TestToolsUsedField:
    def test_default_empty_list(self):
        from puzzleeval.schemas import TestCaseResult
        tcr = TestCaseResult(
            test_case_id="t1", sub_task_ref="s",
            input_sent={}, output_received="", raw_response={},
            latency_ms=10, tokens_used=None, cost_usd=None,
            success=True, error=None, skip_reason=None,
            criteria_scores=[], weighted_score=0.5, passed=False,
        )
        assert tcr.tools_used == []

    def test_round_trips_populated_list(self):
        from puzzleeval.schemas import TestCaseResult
        tcr = TestCaseResult(
            test_case_id="t1", sub_task_ref="s",
            input_sent={}, output_received="", raw_response={},
            latency_ms=10, tokens_used=None, cost_usd=None,
            success=True, error=None, skip_reason=None,
            criteria_scores=[], weighted_score=0.5, passed=False,
            tools_used=["code_execution", "llm_judge"],
        )
        d = tcr.model_dump()
        assert d["tools_used"] == ["code_execution", "llm_judge"]


# ---------------------------------------------------------------------------
# _needs_plugin_synthesis / _synthesize_test_input_via_plugin
# ---------------------------------------------------------------------------


def _minimal_test_case(**overrides):
    from puzzleeval.schemas import JudgementCriterion, TestCase
    defaults = dict(
        id="tc1", sub_task_ref="x", scenario="x",
        input_type="text", output_type="free_text",
        input_data="hello", expected_output="hi",
        difficulty="easy", coverage_dimensions=["happy_path"],
        tags=[], judgement_criteria=[
            JudgementCriterion(
                criterion="x", weight=1.0, eval_type="subjective_quality",
            )
        ],
    )
    defaults.update(overrides)
    return TestCase(**defaults)


class TestSynthesisDispatch:
    def test_needs_synthesis_when_audio_no_file_no_data(self):
        from puzzleeval.agents.implement_test_env import _needs_plugin_synthesis
        tc = _minimal_test_case(
            input_type="audio_content", input_data="", test_file_path=None,
        )
        assert _needs_plugin_synthesis(tc) is True

    def test_needs_synthesis_when_audio_with_text_data_but_no_file(self):
        """Audio mode with text input_data still benefits from TTS to produce
        a real audio file the harness can upload."""
        from puzzleeval.agents.implement_test_env import _needs_plugin_synthesis
        tc = _minimal_test_case(
            input_type="audio_content",
            input_data="please greet the user",
            test_file_path=None,
        )
        assert _needs_plugin_synthesis(tc) is True

    def test_needs_synthesis_when_conversation_no_data(self):
        from puzzleeval.agents.implement_test_env import _needs_plugin_synthesis
        tc = _minimal_test_case(
            input_type="conversation", input_data="", test_file_path=None,
        )
        assert _needs_plugin_synthesis(tc) is True

    def test_does_not_need_synthesis_when_text_with_data(self):
        from puzzleeval.agents.implement_test_env import _needs_plugin_synthesis
        tc = _minimal_test_case(
            input_type="text", input_data="hello",
        )
        assert _needs_plugin_synthesis(tc) is False

    def test_does_not_need_synthesis_when_file_already_present(self):
        from puzzleeval.agents.implement_test_env import _needs_plugin_synthesis
        tc = _minimal_test_case(
            input_type="audio_content", test_file_path="/tmp/x.wav",
        )
        assert _needs_plugin_synthesis(tc) is False

    def test_synthesizer_returns_unchanged_when_no_plugin_available(
        self, monkeypatch, tmp_path,
    ):
        """When no plugin can synthesize the modality (no credentials), the
        helper returns the test case unchanged so downstream falls back to
        Gap 3's file_required handling."""
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "ELEVENLABS_API_KEY", "PUZZLEEVAL_TTS_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        from puzzleeval.agents.implement_test_env import (
            _synthesize_test_input_via_plugin,
        )
        import logging
        tc = _minimal_test_case(input_type="audio_content")
        out = _synthesize_test_input_via_plugin(
            tc, tmp_path, logging.getLogger("test"), "trace1",
        )
        assert out.test_file_path is None  # Plugin couldn't synthesize

    def test_synthesizer_uses_conversation_simulator_for_conversation_type(
        self, tmp_path,
    ):
        """conversation_simulator has no credential requirement, so
        synthesis should always succeed and produce inline_data."""
        from puzzleeval.agents.implement_test_env import (
            _synthesize_test_input_via_plugin,
        )
        import logging
        tc = _minimal_test_case(input_type="conversation")
        out = _synthesize_test_input_via_plugin(
            tc, tmp_path, logging.getLogger("test"), "trace1",
        )
        # Should have populated input_data with the conversation script JSON
        assert out.input_data
        assert "conversation_script" in out.input_data or "user_turns" in out.input_data


# ---------------------------------------------------------------------------
# Pipeline summary surfaces plugin advisories
# ---------------------------------------------------------------------------


class TestPipelineSummaryAdvisories:
    def test_finalize_includes_plugin_status(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "ELEVENLABS_API_KEY", "PUZZLEEVAL_TTS_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        from puzzleeval.pipeline import PipelineRun
        run = PipelineRun(trace_id="adv-test", output_dir=str(tmp_path))
        summary = run.finalize()
        assert "plugins" in summary
        assert "summary" in summary["plugins"]
        # Without OPENAI/DEEPGRAM/ASSEMBLYAI, transcription should be in advisories
        assert "plugin_advisories" in summary
        text = "\n".join(summary["plugin_advisories"])
        assert "transcription" in text
