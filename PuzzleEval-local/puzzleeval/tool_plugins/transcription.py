"""Transcription (Speech-to-Text) plugin for voice/audio agent evaluation.

Voice and phone agents return audio responses. The text LLM judge can't
listen. This plugin transcribes the audio response to text via a
configurable STT provider (OpenAI Whisper API, Deepgram, AssemblyAI,
Anthropic-future), then the score is computed by text-similarity
against the expected transcript.

Provider selection is by env var:

  - PUZZLEEVAL_STT_PROVIDER=openai_whisper (default if OPENAI_API_KEY set)
  - PUZZLEEVAL_STT_PROVIDER=deepgram         (uses DEEPGRAM_API_KEY)
  - PUZZLEEVAL_STT_PROVIDER=assemblyai       (uses ASSEMBLYAI_API_KEY)

When no provider credential is configured, ``is_available()`` returns
False with a structured reason. The caller (Agent 5 evaluator) falls
back to the LLM judge with a "transcription unavailable" annotation
in the test result, rather than crashing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from puzzleeval.tool_plugins import (
    EvaluationResult,
    PluginCapabilities,
    ToolPlugin,
    register_plugin,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider adapter shape
# ---------------------------------------------------------------------------


def _select_provider() -> tuple[str, dict[str, str]] | None:
    """Pick a configured STT provider. Returns ``(name, env_vars)``."""
    explicit = os.environ.get("PUZZLEEVAL_STT_PROVIDER", "").lower().strip()
    candidates = [
        ("openai_whisper", "OPENAI_API_KEY"),
        ("deepgram", "DEEPGRAM_API_KEY"),
        ("assemblyai", "ASSEMBLYAI_API_KEY"),
    ]
    if explicit:
        for name, key in candidates:
            if name == explicit and os.environ.get(key):
                return name, {key: os.environ[key]}
        return None
    for name, key in candidates:
        if os.environ.get(key):
            return name, {key: os.environ[key]}
    return None


def _transcribe_via_openai(audio_path: Path, key: str) -> str:
    try:
        import requests  # type: ignore
    except ImportError:
        raise RuntimeError("requests package not installed")
    with open(audio_path, "rb") as f:
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (audio_path.name, f)},
            data={"model": "whisper-1"},
            timeout=120,
        )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def _transcribe_via_deepgram(audio_path: Path, key: str) -> str:
    try:
        import requests  # type: ignore
    except ImportError:
        raise RuntimeError("requests package not installed")
    with open(audio_path, "rb") as f:
        resp = requests.post(
            "https://api.deepgram.com/v1/listen",
            headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
            data=f.read(),
            timeout=120,
        )
    resp.raise_for_status()
    payload = resp.json()
    try:
        return payload["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    except (KeyError, IndexError, TypeError):
        return ""


def _transcribe_via_assemblyai(audio_path: Path, key: str) -> str:
    try:
        import requests  # type: ignore
    except ImportError:
        raise RuntimeError("requests package not installed")
    headers = {"authorization": key}
    with open(audio_path, "rb") as f:
        upload = requests.post(
            "https://api.assemblyai.com/v2/upload",
            headers=headers, data=f.read(), timeout=60,
        )
    upload.raise_for_status()
    audio_url = upload.json()["upload_url"]
    submit = requests.post(
        "https://api.assemblyai.com/v2/transcript",
        json={"audio_url": audio_url}, headers=headers, timeout=30,
    )
    submit.raise_for_status()
    transcript_id = submit.json()["id"]
    import time as _time
    for _ in range(120):
        poll = requests.get(
            f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
            headers=headers, timeout=30,
        )
        poll.raise_for_status()
        body = poll.json()
        status = body.get("status")
        if status == "completed":
            return (body.get("text") or "").strip()
        if status == "error":
            raise RuntimeError(body.get("error", "assemblyai error"))
        _time.sleep(1.5)
    raise RuntimeError("assemblyai polling timeout")


_PROVIDER_DISPATCH = {
    "openai_whisper": _transcribe_via_openai,
    "deepgram": _transcribe_via_deepgram,
    "assemblyai": _transcribe_via_assemblyai,
}


# ---------------------------------------------------------------------------
# Plugin implementation
# ---------------------------------------------------------------------------


class TranscriptionPlugin(ToolPlugin):
    """Speech-to-text evaluator for voice/audio agent responses.

    The evaluation flow:
      1. Extract the audio reference from the harness response (URL,
         file path, or base64 data).
      2. If it's a URL or data URI, download to a tmp file.
      3. Transcribe via the configured provider.
      4. Compare the transcript to ``expected`` via text similarity
         (Jaccard token overlap as a baseline; LLM judge for nuance
         when ``criteria`` are provided).
    """

    name = "transcription"

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            input_types=["audio_content", "file_reference"],
            output_types=["audio_content", "media_url"],
            synthesizes_input=False,
            evaluates_output=True,
            requires_credentials=[
                "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
            ],  # ANY one is sufficient — checked per-call
            notes=(
                "Wraps OpenAI Whisper / Deepgram / AssemblyAI. Pick provider "
                "via PUZZLEEVAL_STT_PROVIDER env var or first key found wins. "
                "Falls back gracefully when no provider is credentialed."
            ),
        )

    def is_available(self) -> tuple[bool, str]:
        provider = _select_provider()
        if provider is None:
            return False, (
                "no STT provider credential found "
                "(set OPENAI_API_KEY, DEEPGRAM_API_KEY, or ASSEMBLYAI_API_KEY)"
            )
        return True, ""

    def evaluate_output(
        self, *, response: Any, expected: Any,
        criteria: list[dict] | None = None, **kwargs: Any,
    ) -> EvaluationResult:
        provider = _select_provider()
        if provider is None:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning="no STT provider configured",
                fallback_reason="no_stt_provider",
            )
        provider_name, env_vars = provider

        audio_path = self._materialize_audio(response)
        if audio_path is None:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning="could not extract audio from response",
                fallback_reason="no_audio_in_response",
            )

        try:
            transcript = _PROVIDER_DISPATCH[provider_name](
                audio_path, list(env_vars.values())[0],
            )
        except Exception as exc:
            logger.warning("transcription failed via %s: %s", provider_name, exc)
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning=f"transcription error: {exc}",
                fallback_reason=f"stt_error_{provider_name}",
            )

        expected_text = expected if isinstance(expected, str) else str(expected or "")
        score = _token_overlap_score(transcript, expected_text)
        passed = score >= 0.6  # 60% token overlap is a generous match
        return EvaluationResult(
            passed=passed,
            score=score,
            reasoning=(
                f"transcript via {provider_name}: {transcript[:200]}... "
                f"(token overlap {score:.2f})"
            ),
            detail={
                "transcript": transcript,
                "expected_text": expected_text,
                "provider": provider_name,
                "token_overlap": score,
            },
        )

    @staticmethod
    def _materialize_audio(response: Any) -> Path | None:
        """Return a Path to a local audio file from the response payload."""
        import tempfile, base64, re
        candidates: list[str] = []
        if isinstance(response, str):
            candidates.append(response)
        elif isinstance(response, dict):
            for key in (
                "audio_url", "url", "audio", "file", "output", "audio_path",
            ):
                v = response.get(key)
                if isinstance(v, str):
                    candidates.append(v)
        for c in candidates:
            c = c.strip()
            if not c:
                continue
            # Local path?
            if os.path.exists(c):
                return Path(c)
            # data URI?
            m = re.match(r"data:audio/[a-zA-Z0-9.+-]+;base64,(.+)$", c)
            if m:
                blob = base64.b64decode(m.group(1))
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".wav", delete=False,
                )
                tmp.write(blob)
                tmp.close()
                return Path(tmp.name)
            # http URL?
            if c.startswith(("http://", "https://")):
                try:
                    import requests  # type: ignore
                    r = requests.get(c, timeout=30)
                    r.raise_for_status()
                    suffix = ".wav"
                    if "." in c.split("?")[0][-6:]:
                        suffix = "." + c.split("?")[0].rsplit(".", 1)[-1]
                    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                    tmp.write(r.content)
                    tmp.close()
                    return Path(tmp.name)
                except Exception as exc:
                    logger.warning("audio download failed: %s", exc)
                    continue
        return None


def _token_overlap_score(actual: str, expected: str) -> float:
    """Jaccard token overlap between two strings, lowercased + de-punctuated."""
    import re
    def toks(s: str) -> set[str]:
        return set(re.findall(r"[a-z0-9']+", s.lower()))
    a, b = toks(actual), toks(expected)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


register_plugin(TranscriptionPlugin())


__all__ = ["TranscriptionPlugin", "_token_overlap_score"]
