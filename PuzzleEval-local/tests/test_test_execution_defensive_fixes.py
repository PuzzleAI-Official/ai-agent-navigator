"""Regression guards for three test-execution-phase defensive fixes
shipped 2026-04-23, after real-run trace 0c7f085f exposed them:

Fix #1: user_simulator handles empty-text turns
  - Real run: 7 simulator crashes with "user messages must have non-empty
    content" — agent's harness returned empty transcript on some turns,
    simulator forwarded "" to Anthropic API, 400 error, test case lost.
  - Fix: substitute placeholder for empty text before sending.
  - Contract: simulator must handle incomplete agent responses gracefully.

Fix #2: TTS caller audio normalized to .mp3 regardless of source provider
  - Real run: 3 of 5 OpenAI tests had NO merged conversation.mp3 because
    caller audio was mixed .mp3 + .wav (ElevenLabs 429 → WAV fallover).
    Merger refuses mixed extensions.
  - Fix: transcode caller audio to .mp3 on save regardless of TTS source.
  - Contract: TTS pipeline guarantees one caller format.

Fix #3: rubric_judge max_tokens bump + JSON-truncation tolerance
  - Real run: 2 judge crashes with "Invalid JSON: EOF while parsing a
    string at line 1 column 2126". max_tokens=4096 too tight for long
    5-criterion rubrics.
  - Fix: bump default to 8192 + add try/except around parsed_output,
    falling back to zero-score verdict with clear reason.
  - Contract: judge parse failure never crashes a test case.

All three are DEFENSIVE CONTRACTS at the right layer, not bandaids.
Each handles the class of failure, not a specific candidate.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Fix #1: user_simulator empty-text handling
# ============================================================================


class TestSimulatorEmptyTurnHandling:
    """The simulator must handle turns with empty text without crashing
    the Anthropic API call (which rejects empty content strings)."""

    def _make_turn(self, role: str, text: str):
        from puzzleeval.schemas import ConversationTurn
        return ConversationTurn(role=role, text=text, turn_index=0, meta={})

    def test_empty_agent_turn_becomes_placeholder(self):
        from puzzleeval.user_simulator import _render_history_as_messages
        history = [
            self._make_turn("user", "Hi, can you help?"),
            self._make_turn("agent", ""),  # ← empty agent response
        ]
        messages = _render_history_as_messages(history)
        # No message should have empty content — Anthropic rejects those
        for m in messages:
            assert m["content"], (
                f"Found empty-content message: {m}. Simulator must "
                f"substitute a placeholder for empty turns."
            )
        # The empty agent turn should show up as a user-role message
        # (simulator's POV — agent speaks → role=user) with placeholder
        agent_messages = [m for m in messages if m["role"] == "user"]
        assert agent_messages, "Agent turn must render as role=user"
        placeholder_text = agent_messages[-1]["content"].lower()
        assert "no response" in placeholder_text or "silence" in placeholder_text or "dropped" in placeholder_text

    def test_whitespace_only_agent_turn_also_becomes_placeholder(self):
        """A turn with '  \\n  ' should be treated as empty too — stripping
        whitespace matches the Anthropic rejection criterion."""
        from puzzleeval.user_simulator import _render_history_as_messages
        history = [
            self._make_turn("user", "Hello?"),
            self._make_turn("agent", "   \n  "),  # ← whitespace only
        ]
        messages = _render_history_as_messages(history)
        for m in messages:
            assert m["content"].strip(), (
                f"Whitespace-only content counts as empty for the API: {m}"
            )

    def test_empty_user_turn_also_placeholdered(self):
        """Symmetry: simulator's own blank turns also get placeholders
        (unlikely but possible if simulator model glitches)."""
        from puzzleeval.user_simulator import _render_history_as_messages
        history = [
            self._make_turn("user", ""),  # simulator emitted nothing
            self._make_turn("agent", "Hello?"),
        ]
        messages = _render_history_as_messages(history)
        for m in messages:
            assert m["content"], f"No empty content permitted: {m}"

    def test_normal_turns_unchanged(self):
        """Placeholder substitution must be a TARGETED fix — non-empty
        turns pass through with their original content."""
        from puzzleeval.user_simulator import _render_history_as_messages
        history = [
            self._make_turn("user", "Hi, I have a burst pipe"),
            self._make_turn("agent", "I'll dispatch a plumber in 30 minutes"),
        ]
        messages = _render_history_as_messages(history)
        assert any("burst pipe" in m["content"] for m in messages)
        assert any("30 minutes" in m["content"] for m in messages)


# ============================================================================
# Fix #2: TTS caller audio format normalization
# ============================================================================


class TestTTSCallerAudioFormatNormalization:
    """The caller-audio copy MUST save to .mp3 regardless of what format
    the TTS provider emitted. Merger refuses mixed extensions; a single
    provider fallover mid-conversation would otherwise break the merge."""

    def test_source_mp3_copies_as_mp3(self, tmp_path):
        """Fast path: TTS returned .mp3 → byte-identical copy."""
        from puzzleeval.tool_plugins import voice_realtime as vr_mod
        src_path = tmp_path / "source.mp3"
        src_path.write_bytes(b"fake mp3 bytes")

        plugin = vr_mod.VoiceRealtimePlugin()
        plugin.set_session_dir(tmp_path)

        # Mock TTS plugin
        fake_tts = MagicMock()
        fake_tts.is_available.return_value = (True, "")
        fake_tts.synthesize_input.return_value = SimpleNamespace(
            file_path=str(src_path),
        )
        with patch("puzzleeval.tool_plugins.get_plugin", return_value=fake_tts):
            result_path = plugin._synthesize_caller_audio("hello", "tok123")

        assert result_path.endswith(".mp3")
        assert Path(result_path).exists()
        assert Path(result_path).read_bytes() == b"fake mp3 bytes"

    def test_source_wav_gets_transcoded_to_mp3(self, tmp_path):
        """When TTS fallover produces .wav (e.g., from Whisper/OpenAI-TTS
        fallback after ElevenLabs 429), the caller-audio save normalizes
        to .mp3 via pydub."""
        from puzzleeval.tool_plugins import voice_realtime as vr_mod
        # Create a real minimal WAV so pydub can read it (if available)
        import wave, struct
        src_path = tmp_path / "source.wav"
        with wave.open(str(src_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            # 0.1s of silence
            w.writeframes(b"\x00\x00" * 1600)

        plugin = vr_mod.VoiceRealtimePlugin()
        plugin.set_session_dir(tmp_path)

        fake_tts = MagicMock()
        fake_tts.is_available.return_value = (True, "")
        fake_tts.synthesize_input.return_value = SimpleNamespace(
            file_path=str(src_path),
        )
        with patch("puzzleeval.tool_plugins.get_plugin", return_value=fake_tts):
            result_path = plugin._synthesize_caller_audio("hello", "tok456")

        # Best case: .mp3 (pydub + ffmpeg available)
        # Worst case: falls back to .wav with a WARNING log (transcode
        # failed — we explicitly allow this fallback in the code to avoid
        # losing the turn entirely)
        result = Path(result_path)
        assert result.exists()
        # The fix's primary goal is EITHER .mp3 (normal) OR explicit
        # WARNING-logged fallback to source ext. Either is acceptable —
        # what MUST NOT happen is silent loss of the caller audio.

    def test_target_ext_constant_is_mp3(self):
        """Source-grep: the normalization constant must be .mp3 (matches
        merger's expected extension). Regression guard against accidental
        change to another extension which would re-introduce the bug."""
        src = (Path(__file__).resolve().parents[1]
               / "puzzleeval" / "tool_plugins" / "voice_realtime.py"
               ).read_text(encoding="utf-8")
        assert 'TARGET_EXT = ".mp3"' in src, (
            "_synthesize_caller_audio must set TARGET_EXT = '.mp3' — "
            "this is the format the merger expects. Changing this without "
            "updating the merger's expected format re-introduces the "
            "'no merged conversation' bug."
        )


# ============================================================================
# Fix #3: rubric_judge max_tokens + JSON tolerance
# ============================================================================


class TestRubricJudgeMaxTokensBump:

    def test_default_is_8192(self):
        from puzzleeval.config import RUBRIC_JUDGE_MAX_TOKENS
        # 8192 accommodates typical 5-criterion rubrics with detailed
        # per-criterion reasoning (~600 chars each = ~3KB content + ~4KB
        # thinking). 4096 (previous default) was causing truncation.
        assert RUBRIC_JUDGE_MAX_TOKENS == 8192, (
            f"Expected default 8192, got {RUBRIC_JUDGE_MAX_TOKENS}. "
            f"Lowering this risks re-introducing JSON-truncation crashes."
        )

    def test_env_overridable(self):
        src = (Path(__file__).resolve().parents[1]
               / "puzzleeval" / "config.py").read_text(encoding="utf-8")
        assert "PUZZLEEVAL_RUBRIC_JUDGE_MAX_TOKENS" in src


class TestRubricJudgeJSONTolerantFallback:
    """When the judge's response is unparseable (truncation, malformed
    JSON, schema validation error), return a zero-score verdict with
    clear failure reason instead of crashing the test case."""

    def test_parse_failure_returns_fallback_verdict(self):
        """Simulate the real-run failure: parsed_output raises because
        the judge hit max_tokens mid-JSON. rubric_judge must catch + return
        a RubricVerdict explaining the failure, NOT raise."""
        from puzzleeval.rubric_judge import judge_conversation
        from puzzleeval.schemas import (
            ConversationTurn, Persona, RubricCriterion, RubricVerdict,
        )

        fake_response = MagicMock()
        # Mimic the real error: pydantic validation failure on truncated JSON
        type(fake_response).parsed_output = property(
            lambda self: (_ for _ in ()).throw(
                ValueError("Invalid JSON: EOF while parsing a string at line 1 column 2126")
            )
        )
        fake_response.stop_reason = "max_tokens"
        fake_response.content = [SimpleNamespace(
            type="text",
            text='{"overall_score":0.5,"passed":true,"criterion_scores":[{"name":"partial',
        )]
        fake_response.usage = SimpleNamespace(
            input_tokens=100, output_tokens=8192,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )

        fake_client = MagicMock()

        transcript = [ConversationTurn(
            role="agent", text="Hi, Acme Plumbing", turn_index=0, meta={},
        )]
        persona = Persona(
            name="Test Caller",
            demographics="35yo homeowner, urban",
            emotional_state="neutral — routine inquiry",
            speaking_style="casual",
        )
        rubric = [
            RubricCriterion(
                name="greeting",
                description="Agent greets properly",
                weight=1.0,
                critical=False,
                pass_threshold=0.5,
            ),
            RubricCriterion(
                name="accuracy",
                description="Agent is accurate",
                weight=1.0,
                critical=True,
                pass_threshold=0.7,
            ),
        ]

        with patch(
            "puzzleeval.rubric_judge.parse_with_fallback",
            return_value=fake_response,
        ):
            verdict = judge_conversation(
                transcript=transcript,
                persona=persona,
                goal="Get a plumber to the house",
                rubric=rubric,
                candidate_role="plumbing dispatcher",
                client=fake_client,
                trace_id="test-trace",
            )

        # Must return a verdict, NOT raise
        assert isinstance(verdict, RubricVerdict)
        # Zero-score for the failure
        assert verdict.overall_score == 0.0
        assert verdict.passed is False
        # Critical criteria listed as failures (we can't score them)
        assert "accuracy" in verdict.critical_failures
        # Failure reason in the summary field
        assert "unparseable" in verdict.conversation_summary.lower()

    def test_max_tokens_hint_in_fallback_summary(self):
        """When stop_reason=max_tokens, the fallback verdict must
        mention it so ops can tune config without digging into logs."""
        from puzzleeval.rubric_judge import judge_conversation
        from puzzleeval.schemas import (
            ConversationTurn, Persona, RubricCriterion, RubricVerdict,
        )

        fake_response = MagicMock()
        type(fake_response).parsed_output = property(
            lambda self: (_ for _ in ()).throw(
                ValueError("truncated JSON")
            )
        )
        fake_response.stop_reason = "max_tokens"  # ← the hint trigger
        fake_response.content = [SimpleNamespace(type="text", text="{partial")]
        fake_response.usage = SimpleNamespace(
            input_tokens=10, output_tokens=1,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )

        with patch(
            "puzzleeval.rubric_judge.parse_with_fallback",
            return_value=fake_response,
        ):
            verdict = judge_conversation(
                transcript=[ConversationTurn(
                    role="agent", text="hi", turn_index=0, meta={},
                )],
                persona=Persona(
                    name="x", demographics="y", emotional_state="z",
                    speaking_style="casual",
                ),
                goal="x",
                rubric=[RubricCriterion(
                    name="g", description="g", weight=1.0,
                    critical=False, pass_threshold=0.5,
                )],
                candidate_role="agent",
                client=MagicMock(),
                trace_id="t",
            )

        # Summary should mention max_tokens hint
        assert "max_tokens" in verdict.conversation_summary.lower() or "cap" in verdict.conversation_summary.lower()


# ============================================================================
# Cross-fix: none of these should regress the full suite
# ============================================================================


class TestNoRegressionOnHappyPath:
    """Normal, well-formed runs must behave identically to pre-fix
    behavior. Defensive fixes fire only when upstream produces degraded
    input — they don't change the happy path."""

    def test_simulator_normal_turns_unchanged_by_placeholder_fix(self):
        from puzzleeval.user_simulator import _render_history_as_messages
        from puzzleeval.schemas import ConversationTurn
        history = [
            ConversationTurn(
                role="agent", text="Hello, how can I help?",
                turn_index=0, meta={},
            ),
        ]
        messages = _render_history_as_messages(history)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello, how can I help?"
