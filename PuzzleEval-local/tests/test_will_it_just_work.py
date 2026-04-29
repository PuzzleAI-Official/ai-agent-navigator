"""End-to-end "if I set the keys, does it just work?" sanity checks.

These tests guard the practical user contract: drop keys in .env, run
the pipeline, plugins dispatch correctly. No real API calls — just
verifies the wiring is sound.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Dependency manifest — the bits that need to be installable for things
# to "just work" after `pip install -e .`
# ---------------------------------------------------------------------------


class TestDependenciesDeclared:
    def test_python_dotenv_listed_in_pyproject(self):
        """Without python-dotenv, the CLI's auto-loader silently no-ops
        and users have to manually export every plugin credential."""
        text = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert "python-dotenv" in text

    def test_requests_listed_in_pyproject(self):
        """Plugins (transcription, tts, vision_judge for image fetch,
        openapi_harness for spec fetch) all need requests."""
        text = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert "requests" in text

    def test_python_dotenv_actually_importable(self):
        """If pip didn't install it, this surfaces the gap immediately
        rather than letting the silent-skip path hide the problem."""
        try:
            import dotenv  # noqa: F401
        except ImportError:
            pytest.fail(
                "python-dotenv not installed. Run `pip install -e .` from "
                "PuzzleEval-local/ to pick up the new dependency."
            )

    def test_requests_actually_importable(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            pytest.fail(
                "requests not installed. Run `pip install -e .` from "
                "PuzzleEval-local/."
            )


# ---------------------------------------------------------------------------
# VALID_OUTPUT_TYPES expansion — the bug that hid plugin dispatch
# ---------------------------------------------------------------------------


class TestOutputTypeEnumExpansion:
    def test_code_is_a_valid_output_type(self):
        from puzzleeval.validators import VALID_OUTPUT_TYPES
        assert "code" in VALID_OUTPUT_TYPES, (
            "Without 'code' in VALID_OUTPUT_TYPES, Agent 1 can't declare "
            "code-generation scopes; modality detector falls back to LLM "
            "judging instead of dispatching to the code_execution plugin."
        )

    def test_audio_content_is_a_valid_output_type(self):
        from puzzleeval.validators import VALID_OUTPUT_TYPES
        assert "audio_content" in VALID_OUTPUT_TYPES, (
            "Without 'audio_content' in VALID_OUTPUT_TYPES, voice-agent "
            "scopes can't declare audio outputs; transcription plugin "
            "never gets dispatched for evaluation."
        )

    def test_legacy_output_types_still_valid(self):
        """The expansion is additive — none of the previously-valid types
        should have been removed."""
        from puzzleeval.validators import VALID_OUTPUT_TYPES
        for legacy in ("free_text", "structured_json", "classification",
                       "extraction", "action", "media_url"):
            assert legacy in VALID_OUTPUT_TYPES

    def test_code_and_audio_output_types_in_valid_set(self):
        """The actual fix: VALID_OUTPUT_TYPES now includes code + audio_content
        so test cases declaring those modalities are no longer rejected."""
        from puzzleeval.validators import VALID_OUTPUT_TYPES
        assert "code" in VALID_OUTPUT_TYPES
        assert "audio_content" in VALID_OUTPUT_TYPES


# ---------------------------------------------------------------------------
# End-to-end dispatch sanity — given valid output types, does the
# modality detector actually pick the right plugin?
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    def test_code_output_picks_code_execution_plugin(self):
        from puzzleeval.modality import detect_for_test_case
        reqs = detect_for_test_case(input_type="code", output_type="code")
        names = {p.name for p in reqs.output_evaluators}
        assert "code_execution" in names

    def test_audio_output_picks_transcription_plugin(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test")  # makes transcription available
        from puzzleeval.modality import detect_for_test_case
        reqs = detect_for_test_case(
            input_type="audio_content", output_type="audio_content",
        )
        names = {p.name for p in reqs.output_evaluators}
        assert "transcription" in names


# ---------------------------------------------------------------------------
# Agent 1 prompt teaches the new types
# ---------------------------------------------------------------------------


class TestAgent1KnowsNewOutputTypes:
    def test_prompt_lists_code_output_format(self):
        from puzzleeval.agents.user_understanding import SYSTEM_PROMPT
        # The prompt's modality table should mention `code` (quoted or in a
        # table cell — both forms are documented-ish).
        assert "code" in SYSTEM_PROMPT

    def test_prompt_lists_audio_content_output_format(self):
        from puzzleeval.agents.user_understanding import SYSTEM_PROMPT
        # Reworded from the old one-paragraph enum list into a modality
        # table; the enum value still appears literally.
        assert "audio_content" in SYSTEM_PROMPT

    def test_prompt_explains_dispatch_consequences(self):
        from puzzleeval.agents.user_understanding import SYSTEM_PROMPT
        # Agent 1 should know that picking the right output_format enables
        # the right tool plugin (otherwise LLM judge fallback)
        assert "code_execution" in SYSTEM_PROMPT or "tool plugin" in SYSTEM_PROMPT.lower()

    def test_prompt_has_modality_table_with_all_11_enum_values(self):
        """Every enum in VALID_OUTPUT_TYPES must appear in the modality table
        so Agent 1 has explicit guidance for each — previously only 6 of 11
        got worked-example coverage."""
        from puzzleeval.agents.user_understanding import SYSTEM_PROMPT
        from puzzleeval.validators import VALID_OUTPUT_TYPES
        for enum_value in VALID_OUTPUT_TYPES:
            assert enum_value in SYSTEM_PROMPT, (
                f"output_format enum `{enum_value}` missing from Agent 1 prompt"
            )

    def test_prompt_has_non_ocr_worked_examples(self):
        """The audit flagged that every worked example was OCR/document —
        chatbot + outbound email, inbound Slack webhook, code-gen, and voice
        each need explicit example blueprints."""
        from puzzleeval.agents.user_understanding import SYSTEM_PROMPT
        # Chatbot + outbound email
        assert "outbound_message" in SYSTEM_PROMPT
        assert "email_notifier" in SYSTEM_PROMPT or "email confirmations" in SYSTEM_PROMPT
        # Inbound Slack webhook
        assert "webhook_callback" in SYSTEM_PROMPT
        assert "slack_mention_responder" in SYSTEM_PROMPT or "Slack bot" in SYSTEM_PROMPT
        # Code generation
        assert "code_generation" in SYSTEM_PROMPT or "python code generation" in SYSTEM_PROMPT
        # Voice / phone agent
        assert "voice_turn" in SYSTEM_PROMPT
        assert "voice_agent" in SYSTEM_PROMPT or "voice IVR" in SYSTEM_PROMPT
