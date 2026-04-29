"""Text-to-Speech plugin — synthesizes audio test inputs for voice agents.

When evaluating a voice or phone agent, the test runner needs to send
realistic audio INPUT to the API (recorded utterances, IVR prompts,
multi-speaker conversations). When the user has no sample recordings
on disk, this plugin generates them via a configurable TTS provider.

Provider selection (env vars):
  - PUZZLEEVAL_TTS_PROVIDER=openai_tts   (uses OPENAI_API_KEY, model `tts-1`)
  - PUZZLEEVAL_TTS_PROVIDER=elevenlabs    (uses ELEVENLABS_API_KEY)

Returned ``SynthesisResult.file_path`` is the absolute path to a .wav
or .mp3 file the test runner can upload via multipart. The
``ground_truth`` carries the exact text spoken (so the
TranscriptionPlugin's eval can compare against it later).
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from puzzleeval.tool_plugins import (
    PluginCapabilities,
    SynthesisResult,
    ToolPlugin,
    register_plugin,
)

logger = logging.getLogger(__name__)


_TTS_PROVIDER_KEYS: list[tuple[str, str]] = [
    ("openai_tts", "OPENAI_API_KEY"),
    ("elevenlabs", "ELEVENLABS_API_KEY"),
]


def _iter_tts_providers() -> list[tuple[str, str]]:
    """All credentialed TTS providers in priority order.

    When ``PUZZLEEVAL_TTS_PROVIDER`` is set (and credentialed), it comes
    FIRST. All other credentialed providers come after so the caller can
    fail over from a broken key to a working one without user
    intervention. Without an explicit preference, iteration follows the
    natural order in ``_TTS_PROVIDER_KEYS`` (OpenAI first — more widely
    available, tighter SLA — then ElevenLabs).

    Returns a list of ``(provider_name, api_key)`` tuples, possibly empty.
    """
    explicit = os.environ.get("PUZZLEEVAL_TTS_PROVIDER", "").lower().strip()
    credentialed = [
        (name, os.environ[key])
        for name, key in _TTS_PROVIDER_KEYS
        if os.environ.get(key)
    ]
    if explicit:
        explicit_entries = [e for e in credentialed if e[0] == explicit]
        other_entries = [e for e in credentialed if e[0] != explicit]
        return explicit_entries + other_entries
    return credentialed


def _select_tts_provider() -> tuple[str, str] | None:
    """First credentialed TTS provider (or None). Back-compat shim.

    Tests + external callers that don't need the failover list still
    work. New code should use ``_iter_tts_providers`` to enable
    graceful failover when the primary provider returns 401 / rate
    limits / etc.
    """
    providers = _iter_tts_providers()
    return providers[0] if providers else None


def _synthesize_via_openai(text: str, key: str, voice: str = "alloy") -> bytes:
    try:
        import requests  # type: ignore
    except ImportError:
        raise RuntimeError("requests package not installed")
    resp = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": "tts-1", "input": text, "voice": voice, "format": "wav"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def _synthesize_via_elevenlabs(text: str, key: str, voice_id: str | None = None) -> bytes:
    try:
        import requests  # type: ignore
    except ImportError:
        raise RuntimeError("requests package not installed")
    voice_id = voice_id or os.environ.get(
        "PUZZLEEVAL_ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM",  # default Rachel
    )
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_monolingual_v1"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


_PROVIDER_DISPATCH = {
    "openai_tts": _synthesize_via_openai,
    "elevenlabs": _synthesize_via_elevenlabs,
}


class TTSPlugin(ToolPlugin):
    """Text-to-speech synthesizer for voice/phone agent test inputs."""

    name = "tts"

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            input_types=[],  # this plugin doesn't EVALUATE inputs
            output_types=["audio_content", "media_url"],
            synthesizes_input=True,
            evaluates_output=False,
            requires_credentials=["OPENAI_API_KEY", "ELEVENLABS_API_KEY"],
            notes=(
                "Synthesizes audio test inputs. ANY of the provider keys above "
                "is sufficient. The text spoken is preserved as ground truth "
                "for the transcription plugin's downstream evaluation."
            ),
        )

    def is_available(self) -> tuple[bool, str]:
        if _select_tts_provider() is None:
            return False, (
                "no TTS provider credential found "
                "(set OPENAI_API_KEY or ELEVENLABS_API_KEY)"
            )
        return True, ""

    def synthesize_input(
        self, *, scope_role: str, ground_truth_hint: str | None = None,
        **kwargs: Any,
    ) -> SynthesisResult:
        providers = _iter_tts_providers()
        if not providers:
            return SynthesisResult(
                file_path=None,
                ground_truth={"reason": "no_tts_provider"},
                notes="TTS provider not credentialed; cannot synthesize audio",
            )

        # Choose what to say. The ground_truth_hint is the canonical text
        # the test runner expects the agent to understand. When no hint
        # is supplied we use a domain-neutral utterance.
        text = ground_truth_hint or self._utterance_for_role(scope_role)

        # Failover chain: try each credentialed provider in priority order.
        # A 401 / 429 / 5xx from the primary (e.g. expired ElevenLabs key)
        # shouldn't collapse the whole voice pipeline when OpenAI TTS is
        # also available. Only return failure when EVERY provider errors.
        errors: list[str] = []
        for provider_name, key in providers:
            try:
                audio_bytes = _PROVIDER_DISPATCH[provider_name](text, key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "TTS synthesis failed via %s: %s — trying next provider",
                    provider_name, exc,
                )
                errors.append(f"{provider_name}: {exc}")
                continue
            suffix = ".wav" if provider_name == "openai_tts" else ".mp3"
            tmp = tempfile.NamedTemporaryFile(
                prefix="puzzleeval_tts_", suffix=suffix, delete=False,
            )
            tmp.write(audio_bytes)
            tmp.close()
            notes = f"audio synthesized via {provider_name}; expected transcript = {text!r}"
            if errors:
                notes += f" (after failover from: {'; '.join(errors)})"
            return SynthesisResult(
                file_path=tmp.name,
                ground_truth={
                    "text": text, "voice_provider": provider_name,
                    "providers_tried": [p[0] for p in providers[:len(errors) + 1]],
                },
                notes=notes,
            )

        # All providers failed.
        return SynthesisResult(
            file_path=None,
            ground_truth={"text": text, "tts_errors": errors},
            notes=f"TTS synthesis failed on all providers: {'; '.join(errors)}",
        )

    @staticmethod
    def _utterance_for_role(role: str) -> str:
        """Pick a domain-neutral seed utterance.

        Avoids capability-specific carveouts: the same phrase tests any
        voice agent's general comprehension. Override per-test-case by
        passing ``ground_truth_hint`` explicitly.
        """
        return (
            "Hello. I'd like to ask a quick question and I would appreciate "
            "a brief answer. Thank you."
        )


register_plugin(TTSPlugin())


__all__ = ["TTSPlugin"]
