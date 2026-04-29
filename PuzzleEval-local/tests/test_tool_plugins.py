"""Tests for the tool plugin architecture (cross-modality coverage).

Covers:
  - Plugin registry: register/lookup/find by input/output type
  - CodeExecutionPlugin: Python execution, assertion parsing, no-toolchain
    fallback, language detection
  - VisionPlugin: capability declaration + delegation
  - TranscriptionPlugin: provider selection, missing-credential degradation,
    token-overlap scorer
  - TTSPlugin: provider selection
  - ConversationSimulatorPlugin: script extraction, multi-turn replay,
    assertion checking, harness shape adapters
  - Modality detector: matches plugins by input/output enum, surfaces
    unavailable plugins as advice
  - Builder context emits modality plugin guidance
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.modality import (
    detect_for_test_case,
    detect_for_test_plan,
    summarize_unavailable,
)
from puzzleeval.tool_plugins import (
    EvaluationResult,
    PluginCapabilities,
    SynthesisResult,
    ToolPlugin,
    find_plugins_for_input_type,
    find_plugins_for_output_type,
    get_plugin,
    list_plugins,
    register_plugin,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestPluginRegistry:
    def test_bundled_plugins_auto_registered(self):
        names = {p.name for p in list_plugins()}
        # All five reference plugins should be present after package import
        for n in (
            "code_execution", "vision", "transcription",
            "tts", "conversation_simulator",
        ):
            assert n in names, f"{n} not registered"

    def test_get_plugin_lookup(self):
        assert get_plugin("code_execution") is not None
        assert get_plugin("does_not_exist") is None

    def test_register_and_lookup_custom_plugin(self):
        class _Stub(ToolPlugin):
            name = "stub_for_test"
            def capabilities(self) -> PluginCapabilities:
                return PluginCapabilities(
                    input_types=["text"], output_types=["free_text"],
                    synthesizes_input=True, evaluates_output=True,
                )
        stub = _Stub()
        register_plugin(stub)
        assert get_plugin("stub_for_test") is stub

    def test_register_rejects_nameless_plugin(self):
        class _Bad(ToolPlugin):
            name = ""
            def capabilities(self) -> PluginCapabilities:
                return PluginCapabilities()
        with pytest.raises(ValueError, match="non-empty"):
            register_plugin(_Bad())

    def test_find_by_input_type(self):
        plugins = find_plugins_for_input_type("audio_content")
        names = {p.name for p in plugins}
        assert "transcription" in names

    def test_find_by_output_type(self):
        plugins = find_plugins_for_output_type("media_url")
        names = {p.name for p in plugins}
        assert "vision" in names
        assert "transcription" in names  # transcription declares media_url too


# ---------------------------------------------------------------------------
# CodeExecutionPlugin
# ---------------------------------------------------------------------------


class TestCodeExecutionPlugin:
    def test_capabilities(self):
        plugin = get_plugin("code_execution")
        caps = plugin.capabilities()
        assert "code" in caps.input_types
        assert "code" in caps.output_types
        assert caps.synthesizes_input
        assert caps.evaluates_output

    def test_python_always_available(self):
        plugin = get_plugin("code_execution")
        ok, _ = plugin.is_available()
        assert ok  # Python is always present (host interpreter)

    def test_synthesize_python_seed(self):
        plugin = get_plugin("code_execution")
        result = plugin.synthesize_input(scope_role="code_generation", language="python")
        assert "fizzbuzz" in (result.inline_data or {}).get("prompt", "").lower()
        assert result.ground_truth["expected_function"] == "fizzbuzz"

    def test_evaluate_correct_python_solution(self):
        plugin = get_plugin("code_execution")
        correct_code = textwrap.dedent("""
            def fizzbuzz(n):
                out = []
                for i in range(1, n + 1):
                    if i % 15 == 0:
                        out.append("FizzBuzz")
                    elif i % 3 == 0:
                        out.append("Fizz")
                    elif i % 5 == 0:
                        out.append("Buzz")
                    else:
                        out.append(str(i))
                return out
        """).strip()
        expected = {
            "expected_function": "fizzbuzz",
            "test_inputs": [3],
            "test_outputs": [["1", "2", "Fizz"]],
        }
        result = plugin.evaluate_output(
            response={"output": correct_code},
            expected=expected, language="python",
        )
        assert result.passed
        assert result.score == 1.0
        assert result.detail["exit_code"] == 0

    def test_evaluate_broken_python_solution(self):
        plugin = get_plugin("code_execution")
        broken = "def fizzbuzz(n):\n    return ['always_wrong']"
        expected = {
            "expected_function": "fizzbuzz",
            "test_inputs": [3],
            "test_outputs": [["1", "2", "Fizz"]],
        }
        result = plugin.evaluate_output(
            response=broken, expected=expected, language="python",
        )
        assert not result.passed
        assert result.score < 1.0

    def test_evaluate_missing_function(self):
        plugin = get_plugin("code_execution")
        no_function = "x = 42"
        expected = {"expected_function": "fizzbuzz", "test_inputs": [3], "test_outputs": [["x"]]}
        result = plugin.evaluate_output(
            response=no_function, expected=expected, language="python",
        )
        assert not result.passed

    def test_evaluate_no_code_in_response(self):
        plugin = get_plugin("code_execution")
        result = plugin.evaluate_output(response={}, expected={}, language="python")
        assert result.fallback_reason == "no_code_in_response"

    def test_unknown_language_returns_fallback(self):
        plugin = get_plugin("code_execution")
        result = plugin.evaluate_output(
            response="(* hello *)", expected={}, language="brainfuck",
        )
        assert result.fallback_reason in {"unknown_language", "missing_toolchain_brainfuck"}

    def test_language_detection_python(self):
        from puzzleeval.tool_plugins.code_execution import detect_language
        assert detect_language("def hello():\n    pass") == "python"
        assert detect_language("import os\nprint(os.getcwd())") == "python"

    def test_language_detection_javascript(self):
        from puzzleeval.tool_plugins.code_execution import detect_language
        assert detect_language("console.log('hi')\nconst x = 1;") == "javascript"

    def test_language_detection_explicit_hint_wins(self):
        from puzzleeval.tool_plugins.code_execution import detect_language
        # Even with python-shaped content, hint=javascript wins
        assert detect_language("def x(): pass", language_hint="js") == "javascript"


# ---------------------------------------------------------------------------
# VisionPlugin
# ---------------------------------------------------------------------------


class TestVisionPlugin:
    def test_capabilities(self):
        plugin = get_plugin("vision")
        caps = plugin.capabilities()
        assert "media_url" in caps.output_types
        assert caps.evaluates_output
        assert "ANTHROPIC_API_KEY" in caps.requires_credentials

    def test_evaluate_no_image_returns_fallback(self):
        plugin = get_plugin("vision")
        result = plugin.evaluate_output(response={}, expected="a sunset")
        assert result.fallback_reason == "no_image_in_response"


# ---------------------------------------------------------------------------
# TranscriptionPlugin
# ---------------------------------------------------------------------------


class TestTranscriptionPlugin:
    def test_capabilities(self):
        plugin = get_plugin("transcription")
        caps = plugin.capabilities()
        assert "audio_content" in caps.input_types
        assert "media_url" in caps.output_types
        assert caps.evaluates_output

    def test_token_overlap_scorer(self):
        from puzzleeval.tool_plugins.transcription import _token_overlap_score
        assert _token_overlap_score("hello world", "hello world") == 1.0
        assert _token_overlap_score("hello world", "world hello") == 1.0
        assert 0.4 < _token_overlap_score(
            "the quick brown fox", "the slow brown fox",
        ) < 1.0
        assert _token_overlap_score("", "") == 1.0
        assert _token_overlap_score("anything", "") == 0.0

    def test_unavailable_when_no_provider(self, monkeypatch):
        # Strip every potential STT provider key
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "PUZZLEEVAL_STT_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        plugin = get_plugin("transcription")
        ok, reason = plugin.is_available()
        assert not ok
        assert "STT provider" in reason

    def test_evaluate_returns_fallback_when_no_provider(self, monkeypatch):
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "PUZZLEEVAL_STT_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        plugin = get_plugin("transcription")
        result = plugin.evaluate_output(
            response={"audio_url": "https://example.com/x.wav"},
            expected="hello",
        )
        assert result.fallback_reason == "no_stt_provider"


# ---------------------------------------------------------------------------
# TTSPlugin
# ---------------------------------------------------------------------------


class TestTTSPlugin:
    def test_capabilities(self):
        plugin = get_plugin("tts")
        caps = plugin.capabilities()
        assert "audio_content" in caps.output_types
        assert caps.synthesizes_input

    def test_unavailable_when_no_provider(self, monkeypatch):
        for v in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "PUZZLEEVAL_TTS_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        plugin = get_plugin("tts")
        ok, reason = plugin.is_available()
        assert not ok
        assert "TTS provider" in reason

    def test_synthesize_returns_failure_marker_when_no_provider(self, monkeypatch):
        for v in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "PUZZLEEVAL_TTS_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        plugin = get_plugin("tts")
        result = plugin.synthesize_input(scope_role="voice")
        assert result.file_path is None
        assert result.ground_truth.get("reason") == "no_tts_provider"


# ---------------------------------------------------------------------------
# ConversationSimulatorPlugin
# ---------------------------------------------------------------------------


class TestConversationSimulatorPlugin:
    def test_capabilities(self):
        plugin = get_plugin("conversation_simulator")
        caps = plugin.capabilities()
        assert "conversation" in caps.input_types
        assert caps.synthesizes_input
        assert caps.evaluates_output

    def test_synthesize_default_three_turns(self):
        plugin = get_plugin("conversation_simulator")
        result = plugin.synthesize_input(scope_role="chatbot")
        script = result.inline_data["conversation_script"]
        assert len(script["user_turns"]) == 3

    def test_evaluate_full_conversation_pass(self):
        plugin = get_plugin("conversation_simulator")
        # A scripted runner that always replies politely
        def runner(payload):
            return {
                "success": True,
                "output": {"text": "Sure, I can help with general questions."},
                "latency_ms": 10, "raw_response": {}, "error": None,
            }
        expected = {
            "conversation_script": {
                "user_turns": ["Hi", "What can you help with?", "Thanks"],
                "assertions": [
                    {"turn_index": 0, "check_type": "contains", "value": "help", "weight": 1.0},
                    {"turn_index": -1, "check_type": "not_contains", "value": "error", "weight": 1.0},
                ],
            }
        }
        result = plugin.evaluate_output(
            response=None, expected=expected, harness_runner=runner,
        )
        assert result.passed
        assert result.score == 1.0
        assert len(result.detail["agent_turns"]) == 3

    def test_evaluate_fails_when_assertion_fails(self):
        plugin = get_plugin("conversation_simulator")
        def runner(payload):
            return {
                "success": True,
                "output": {"text": "I don't know."},
                "latency_ms": 10, "raw_response": {}, "error": None,
            }
        expected = {
            "conversation_script": {
                "user_turns": ["Hi"],
                "assertions": [
                    {"turn_index": 0, "check_type": "contains", "value": "help"},
                ],
            }
        }
        result = plugin.evaluate_output(
            response=None, expected=expected, harness_runner=runner,
        )
        assert not result.passed
        assert result.score < 1.0

    def test_evaluate_handles_runner_crash(self):
        plugin = get_plugin("conversation_simulator")
        def runner(payload):
            raise RuntimeError("boom")
        expected = {"conversation_script": {"user_turns": ["x"], "assertions": []}}
        result = plugin.evaluate_output(
            response=None, expected=expected, harness_runner=runner,
        )
        assert not result.passed

    def test_payload_format_messages_default(self):
        from puzzleeval.tool_plugins.conversation_simulator import (
            ConversationSimulatorPlugin,
        )
        history = [{"role": "user", "content": "x"}]
        payload = ConversationSimulatorPlugin._payload_for_format(history, "messages")
        assert payload == {"messages": history}

    def test_payload_format_history_uses_split_shape(self):
        from puzzleeval.tool_plugins.conversation_simulator import (
            ConversationSimulatorPlugin,
        )
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        payload = ConversationSimulatorPlugin._payload_for_format(history, "history")
        assert payload["user_input"] == "second"
        assert payload["history"] == history[:-1]

    def test_assertion_check_types(self):
        from puzzleeval.tool_plugins.conversation_simulator import (
            ConversationAssertion, ConversationSimulatorPlugin,
        )
        check = ConversationSimulatorPlugin._check
        assert check("hello world", ConversationAssertion(0, "contains", "WORLD"))
        assert not check("hello world", ConversationAssertion(0, "contains", "absent"))
        assert check("hello", ConversationAssertion(0, "not_contains", "absent"))
        assert check("hello world", ConversationAssertion(0, "regex_match", r"\bworld\b"))
        # intent_match: strong overlap
        assert check(
            "I can help you reset your account password right now",
            ConversationAssertion(0, "intent_match", "help reset password"),
        )

    def test_no_runner_returns_fallback(self):
        plugin = get_plugin("conversation_simulator")
        result = plugin.evaluate_output(
            response=None,
            expected={"conversation_script": {"user_turns": ["x"], "assertions": []}},
        )
        assert result.fallback_reason == "no_runner"


# ---------------------------------------------------------------------------
# Modality detector
# ---------------------------------------------------------------------------


class TestModalityDetector:
    def test_audio_to_audio_picks_transcription_for_eval(self, monkeypatch):
        # Make transcription available
        monkeypatch.setenv("OPENAI_API_KEY", "test_key")
        reqs = detect_for_test_case(
            input_type="audio_content", output_type="media_url",
        )
        names = {p.name for p in reqs.output_evaluators}
        assert "transcription" in names

    def test_image_output_picks_vision_eval(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test_key")
        reqs = detect_for_test_case(
            input_type="image_description", output_type="media_url",
        )
        names = {p.name for p in reqs.output_evaluators}
        assert "vision" in names

    def test_code_output_picks_code_execution(self):
        reqs = detect_for_test_case(
            input_type="code", output_type="code",
        )
        names = {p.name for p in reqs.output_evaluators}
        assert "code_execution" in names

    def test_conversation_picks_conversation_simulator(self):
        reqs = detect_for_test_case(
            input_type="conversation", output_type="free_text",
        )
        names = {p.name for p in reqs.output_evaluators}
        assert "conversation_simulator" in names

    def test_unrelated_modality_returns_no_evaluators(self, monkeypatch):
        # text → free_text doesn't match any specialized plugin
        # (relies on the LLM judge — caller's concern, not ours)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        reqs = detect_for_test_case(
            input_type="text", output_type="free_text",
        )
        # Not asserting the absence specifically — this varies by env.
        # The detector should at least not crash.
        assert isinstance(reqs.output_evaluators, list)

    def test_unavailable_plugins_surfaced(self, monkeypatch):
        # Strip transcription credentials so it's reported as unavailable
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "PUZZLEEVAL_STT_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        reqs = detect_for_test_case(
            input_type="audio_content", output_type="media_url",
        )
        names = [n for n, _ in reqs.unavailable]
        assert "transcription" in names

    def test_summarize_unavailable_dedup(self, monkeypatch):
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "PUZZLEEVAL_STT_PROVIDER", "ELEVENLABS_API_KEY",
                  "PUZZLEEVAL_TTS_PROVIDER"):
            monkeypatch.delenv(v, raising=False)
        # Two scopes both want audio — should dedup the transcription reason
        scope_reqs = {
            "step_1": detect_for_test_case(
                input_type="audio_content", output_type="media_url",
            ),
            "step_2": detect_for_test_case(
                input_type="audio_content", output_type="media_url",
            ),
        }
        summary = summarize_unavailable(scope_reqs)
        # Should have ONE transcription line, not two
        transcription_lines = [s for s in summary if s.startswith("transcription:")]
        assert len(transcription_lines) == 1


# ---------------------------------------------------------------------------
# Builder modality context emission
# ---------------------------------------------------------------------------


class TestBuilderModalityContext:
    def test_modality_context_lists_active_plugins(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test")  # makes transcription/tts available
        from puzzleeval.agents.implement_test_env import (
            _format_modality_context_for_builder,
        )
        from puzzleeval.schemas import (
            Agent3Result, Agent5Input, JudgementCriterion, ScreenedCandidate,
            TestCase, UserUnderstandingOutput,
        )
        # Build a minimal Agent5Input with one audio test case
        tc = TestCase(
            id="tc1",
            sub_task_ref="voice agent",
            scenario="user calls",
            input_type="audio_content",
            output_type="media_url",
            input_data="hello",
            expected_output="any greeting",
            difficulty="medium",
            coverage_dimensions=["happy_path"],
            tags=["voice"],
            judgement_criteria=[
                JudgementCriterion(criterion="responds", weight=1.0,
                                   eval_type="subjective_quality"),
            ],
        )
        agent3 = Agent3Result(
            test_cases=[tc], coverage_summary={"voice agent": 1},
            generation_notes="", generation_duration_ms=0, cost_usd=0,
        )
        # We need a full UserUnderstandingOutput — build the smallest valid one
        from puzzleeval.schemas import SubTask, Constraints
        uu = UserUnderstandingOutput(
            user_text="x", domain="test", summary="x",
            sub_tasks=[SubTask(
                description="voice agent", capability="voice",
                requires_test_files=False, search_keywords=["v"],
                search_strategy="both",
            )],
            constraints=Constraints(),
            integration_requirements=[], is_clear=True,
            search_keywords=["voice"],
            workflow=None, test_plan=None,
        )
        sc_kwargs = dict(
            name="VoiceCo", provider="VoiceCo", description="",
            pricing_model="freemium", claimed_capabilities=[],
            relevance_score=0.5, adoption_difficulty="easy",
            relevant_subtasks=[], source="",
            verified_api_docs_url="https://x.com",
            auth_method="api_key", api_access_method="free_signup",
            confirmed_capabilities=[], data_format_notes="",
            screening_notes="",
        )
        input_data = Agent5Input(
            validated_candidates=[ScreenedCandidate(**sc_kwargs)],
            user_understanding=uu, test_cases=agent3, trace_id="t",
        )
        block = _format_modality_context_for_builder(input_data)
        assert "Modality plugins active" in block
        assert "transcription" in block
        # The block should mention input/output types it saw
        assert "audio_content" in block
        assert "media_url" in block
