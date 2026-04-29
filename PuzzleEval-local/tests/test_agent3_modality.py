"""Tests for Agent 3 modality awareness + Agent 3F audio handling.

Covers:
  - Agent 3 system prompt teaches plugin-shaped test generation
    (code → structured execution contract; conversation → script;
    audio → spoken text as ground truth; media_url → description).
  - Agent 3F audio file path: transcription plugin called for STT
    ground-truth; degrades gracefully when no provider credentialed.
  - CLI auto-loads .env so plugin keys are available without manual export.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Agent 3 prompt — modality awareness
# ---------------------------------------------------------------------------


class TestAgent3PromptModality:
    def test_prompt_documents_code_modality_shape(self):
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        assert "code_execution" in SYSTEM_PROMPT
        assert "expected_function" in SYSTEM_PROMPT
        assert "test_inputs" in SYSTEM_PROMPT
        assert "test_outputs" in SYSTEM_PROMPT

    def test_prompt_documents_conversation_modality_shape(self):
        """After the agentic-conversational refactor, the PRIMARY contract
        for `conversation` / `voice_conversation` tests is persona +
        goal + rubric, NOT a static script. Legacy scripted shape is
        preserved for back-compat but is explicitly marked LEGACY.
        This test locks BOTH: the new agentic fields must be taught,
        AND the scripted-legacy fallback callout must still be present."""
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT

        # Agentic contract — the primary/default path
        for agentic_field in (
            "persona", "goal", "constraints", "rubric",
            "max_turns", "evaluation_mode",
        ):
            assert agentic_field in SYSTEM_PROMPT, (
                f"Agent 3 prompt must teach the agentic {agentic_field!r} "
                f"field for conversational tests."
            )
        # Core rubric criteria by name
        for rubric_crit in (
            "goal_completion", "accuracy_no_hallucination",
            "info_gathering", "appropriate_tone", "policy_compliance",
        ):
            assert rubric_crit in SYSTEM_PROMPT, (
                f"Standard rubric criterion {rubric_crit!r} should be "
                f"templated for Agent 3."
            )
        # Critical-gate semantics must be taught
        assert "critical" in SYSTEM_PROMPT.lower()
        # Legacy-scripted fallback callout for back-compat
        assert "LEGACY scripted mode" in SYSTEM_PROMPT, (
            "Prompt must keep a legacy-scripted-mode callout so tests "
            "migrating from the old path have a clear fallback. "
            "evaluation_mode='scripted' is the explicit opt-in."
        )
        # Scripted-mode shape still mentioned for that fallback path
        for scripted_field in ("conversation_script", "user_turns"):
            assert scripted_field in SYSTEM_PROMPT
        # All four legacy assertion check types still documented
        for ct in ("contains", "not_contains", "regex_match", "intent_match"):
            assert ct in SYSTEM_PROMPT

    def test_prompt_documents_audio_modality_shape(self):
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        assert "audio_content" in SYSTEM_PROMPT
        assert "tts" in SYSTEM_PROMPT.lower() or "TTS" in SYSTEM_PROMPT
        assert "spoken text" in SYSTEM_PROMPT.lower() or "exact spoken" in SYSTEM_PROMPT.lower()

    def test_prompt_documents_media_url_modality(self):
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        assert "media_url" in SYSTEM_PROMPT
        assert "vision" in SYSTEM_PROMPT.lower()

    def test_prompt_lists_supported_languages_for_code(self):
        from puzzleeval.agents.synthetic_tests import SYSTEM_PROMPT
        # Languages the code_execution plugin supports
        for lang in ("python", "javascript", "go", "rust", "bash"):
            assert lang in SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# Agent 3F audio handling
# ---------------------------------------------------------------------------


class TestAgent3FAudioPath:
    def test_audio_extension_routes_to_transcription_helper(self, tmp_path, monkeypatch):
        """The audio branch must call _transcribe_audio_for_ground_truth and
        feed the transcript into the LLM message rather than choking on a
        format the vision endpoint can't handle."""
        # Strip every STT credential so the helper goes down the
        # "no provider" path — keeps the test deterministic and offline.
        for v in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
                  "PUZZLEEVAL_STT_PROVIDER"):
            monkeypatch.delenv(v, raising=False)

        from puzzleeval.agents.agent3f.core import _build_file_message
        from puzzleeval.schemas import (
            Constraints, SubTask, UserUnderstandingOutput,
        )

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"RIFF....fake wav header")

        uo = UserUnderstandingOutput(
            user_text="x", domain="voice", summary="x",
            sub_tasks=[SubTask(
                description="transcribe call", capability="transcribe",
                requires_test_files=True, search_keywords=["s"],
                search_strategy="both",
            )],
            constraints=Constraints(),
            integration_requirements=[], is_clear=True,
            search_keywords=["voice"],
            workflow=None, test_plan=None,
        )
        blocks = _build_file_message(uo, [str(audio_file)])
        # The audio block should carry a structured "transcribed audio" body
        joined = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
        assert "AUDIO file" in joined
        assert "test.wav" in joined
        # Without a credential, the transcript section should explain why
        assert ("transcription unavailable" in joined.lower()
                or "no STT provider" in joined
                or "transcription plugin" in joined.lower())

    def test_audio_helper_returns_transcript_when_provider_succeeds(
        self, tmp_path, monkeypatch,
    ):
        """When STT succeeds, the helper hands the transcript through."""
        monkeypatch.setenv("OPENAI_API_KEY", "test_key")
        from puzzleeval.agents.synthetic_tests_file import (
            _transcribe_audio_for_ground_truth,
        )
        import logging
        audio_file = tmp_path / "x.wav"
        audio_file.write_bytes(b"RIFF...fake")

        # Mock the actual STT dispatch so we don't hit the real OpenAI API
        with patch(
            "puzzleeval.tool_plugins.transcription._PROVIDER_DISPATCH",
            {"openai_whisper": lambda p, k: "Hello, I need help."},
        ):
            transcript = _transcribe_audio_for_ground_truth(
                str(audio_file), logging.getLogger("test"),
            )
        assert "Hello, I need help" in transcript

    def test_audio_helper_handles_stt_crash_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test_key")
        from puzzleeval.agents.synthetic_tests_file import (
            _transcribe_audio_for_ground_truth,
        )
        import logging
        audio_file = tmp_path / "x.wav"
        audio_file.write_bytes(b"RIFF...fake")

        def _crash(*args, **kwargs):
            raise RuntimeError("network down")
        with patch(
            "puzzleeval.tool_plugins.transcription._PROVIDER_DISPATCH",
            {"openai_whisper": _crash},
        ):
            result = _transcribe_audio_for_ground_truth(
                str(audio_file), logging.getLogger("test"),
            )
        assert "STT failed" in result or "network down" in result

    def test_non_audio_extensions_skip_transcription_path(self, tmp_path):
        """PDFs / images / DOCX still route to parse_file as before."""
        from puzzleeval.agents.agent3f.core import _build_file_message
        from puzzleeval.schemas import (
            Constraints, SubTask, UserUnderstandingOutput,
        )

        txt_file = tmp_path / "doc.txt"
        txt_file.write_text("hello world", encoding="utf-8")

        uo = UserUnderstandingOutput(
            user_text="x", domain="docs", summary="x",
            sub_tasks=[SubTask(
                description="parse text", capability="text",
                requires_test_files=True, search_keywords=["s"],
                search_strategy="both",
            )],
            constraints=Constraints(),
            integration_requirements=[], is_clear=True,
            search_keywords=["text"],
            workflow=None, test_plan=None,
        )
        blocks = _build_file_message(uo, [str(txt_file)])
        joined = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
        assert "AUDIO file" not in joined  # didn't hit the audio branch
        assert "hello world" in joined  # actual file content passed through


# ---------------------------------------------------------------------------
# CLI auto-load .env
# ---------------------------------------------------------------------------


class TestCliAutoloadDotenv:
    def test_helper_loads_env_from_temp_dotenv(self, tmp_path, monkeypatch):
        """When a .env file exists in (or above) cwd, the CLI helper loads
        it without overriding existing env vars."""
        from puzzleeval.cli import _autoload_dotenv

        # Create a .env in tmp_path and chdir there
        env_file = tmp_path / ".env"
        env_file.write_text(
            "PUZZLEEVAL_AUTOLOAD_TEST_KEY=loaded_from_file\n", encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PUZZLEEVAL_AUTOLOAD_TEST_KEY", raising=False)

        _autoload_dotenv()
        assert os.environ.get("PUZZLEEVAL_AUTOLOAD_TEST_KEY") == "loaded_from_file"

    def test_helper_does_not_override_existing_env(self, tmp_path, monkeypatch):
        from puzzleeval.cli import _autoload_dotenv

        env_file = tmp_path / ".env"
        env_file.write_text(
            "PUZZLEEVAL_AUTOLOAD_TEST_KEY=from_file\n", encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PUZZLEEVAL_AUTOLOAD_TEST_KEY", "from_shell")

        _autoload_dotenv()
        # Shell env wins
        assert os.environ["PUZZLEEVAL_AUTOLOAD_TEST_KEY"] == "from_shell"

    def test_helper_silent_when_no_dotenv_present(self, tmp_path, monkeypatch):
        """No .env file anywhere upward → no crash, no log noise."""
        from puzzleeval.cli import _autoload_dotenv
        monkeypatch.chdir(tmp_path)
        # Should not raise
        _autoload_dotenv()
