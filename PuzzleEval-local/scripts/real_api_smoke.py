"""Real-API smoke tests — prove every cred, every plugin, every provider
works BEFORE spending money on a full pipeline run.

Each test makes ONE cheap real API call (< $0.01 total spend) and
verifies the round-trip. If any test fails, fix that before the real
run. If all pass, the full pipeline will hit real APIs successfully.

Scope:
  - Anthropic: Haiku hello-world (validates ANTHROPIC_API_KEY + SDK)
  - OpenAI Chat: tiny gpt-4o-mini prompt (validates OPENAI_API_KEY)
  - OpenAI TTS: 1-word synth -> WAV (validates TTS plugin path end to end)
  - OpenAI Whisper: transcribe that WAV back (validates STT plugin path)
  - ElevenLabs TTS: 1-word synth (validates ElevenLabs TTS plugin path)
  - voice_realtime loopback: 1 caller WAV synthesized + served + captured
  - Plugin is_available() matrix: every plugin reports ready

  - Provider registry: substring match surfaces creds for representative
    candidate names ("OpenAI Realtime", "ElevenLabs Conversational AI",
    "Mindee OCR") -- proves Agent 5's harness will get its env vars.

Exit 0 = all pass, real run is safe. Exit 1 = at least one failure.

Usage:
    cd PuzzleEval-local
    python scripts/real_api_smoke.py
    python scripts/real_api_smoke.py --skip anthropic openai  # skip named tests
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Force package import so .env + provider_registry propagation runs.
import puzzleeval  # noqa: F401

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def icon(status: str) -> str:
    return {"pass": f"{GREEN}[OK]{RESET}", "fail": f"{RED}[XX]{RESET}",
            "skip": f"{YELLOW}[..]{RESET}"}[status]


RESULTS: list[tuple[str, str, str]] = []  # (name, status, detail)


def run_test(name: str, fn, skip_list: list[str]) -> bool:
    short = name.lower().replace(" ", "_").split(":")[0]
    if any(s in short for s in skip_list):
        print(f"  {icon('skip')} {name}: user-requested skip")
        RESULTS.append((name, "skip", ""))
        return True
    try:
        detail = fn() or ""
        print(f"  {icon('pass')} {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"      {line}")
        RESULTS.append((name, "pass", detail))
        return True
    except AssertionError as exc:
        print(f"  {icon('fail')} {name}: {exc}")
        RESULTS.append((name, "fail", str(exc)))
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  {icon('fail')} {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=2)
        RESULTS.append((name, "fail", f"{type(exc).__name__}: {exc}"))
        return False


# ---------------------------------------------------------------------------
# 1. Anthropic — $0.0001 hello-world proves ANTHROPIC_API_KEY + SDK
# ---------------------------------------------------------------------------

def test_anthropic_haiku():
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": "Reply with just: OK"}],
    )
    text = "".join(
        getattr(b, "text", "") for b in (resp.content or [])
    )
    assert "OK" in text.upper(), f"unexpected response: {text!r}"
    return (f"tokens: in={resp.usage.input_tokens} "
            f"out={resp.usage.output_tokens}")


# ---------------------------------------------------------------------------
# 2. OpenAI Chat — validates OPENAI_API_KEY for candidate harnesses
# ---------------------------------------------------------------------------

def test_openai_chat():
    """Proves OPENAI_API_KEY works. Uses `requests` directly instead of
    the openai SDK because Agent 5's generated harness has full freedom
    over its requirements.txt — we only need to validate the KEY, not
    that a specific SDK is installed in THIS environment."""
    import requests
    key = os.environ.get("OPENAI_API_KEY", "")
    assert key, "OPENAI_API_KEY not in env (registry propagation failed?)"
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini", "max_tokens": 10,
              "messages": [{"role": "user", "content": "Reply with just: OK"}]},
        timeout=30,
    )
    assert resp.status_code == 200, (
        f"status={resp.status_code} body={resp.text[:300]}"
    )
    body = resp.json()
    text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    assert "OK" in text.upper(), f"unexpected response: {text!r}"
    usage = body.get("usage", {})
    return (f"tokens: in={usage.get('prompt_tokens')} "
            f"out={usage.get('completion_tokens')}")


# ---------------------------------------------------------------------------
# 3. OpenAI TTS via plugin — validates the tts plugin end to end
# ---------------------------------------------------------------------------

def test_openai_tts_plugin():
    prior_provider = os.environ.get("PUZZLEEVAL_TTS_PROVIDER")
    os.environ["PUZZLEEVAL_TTS_PROVIDER"] = "openai_tts"
    try:
        from puzzleeval.tool_plugins.tts import TTSPlugin
        plugin = TTSPlugin()
        ok, reason = plugin.is_available()
        assert ok, f"is_available: {reason}"
        result = plugin.synthesize_input(
            scope_role="voice_caller",
            ground_truth_hint="Testing",
        )
        assert result.file_path, "no file_path returned"
        p = Path(result.file_path)
        assert p.exists(), f"file does not exist: {p}"
        size = p.stat().st_size
        assert size > 100, f"file suspiciously small ({size} bytes)"
        return f"synthesized {size} bytes via openai_tts at {p.name}"
    finally:
        if prior_provider is None:
            os.environ.pop("PUZZLEEVAL_TTS_PROVIDER", None)
        else:
            os.environ["PUZZLEEVAL_TTS_PROVIDER"] = prior_provider


# ---------------------------------------------------------------------------
# 4. ElevenLabs TTS via plugin — ensures fallback provider works
# ---------------------------------------------------------------------------

def test_elevenlabs_tts_plugin():
    if not os.environ.get("ELEVENLABS_API_KEY"):
        raise AssertionError(
            "ELEVENLABS_API_KEY not in environment — registry propagation failed"
        )
    prior_provider = os.environ.get("PUZZLEEVAL_TTS_PROVIDER")
    os.environ["PUZZLEEVAL_TTS_PROVIDER"] = "elevenlabs"
    try:
        from puzzleeval.tool_plugins.tts import TTSPlugin
        plugin = TTSPlugin()
        ok, reason = plugin.is_available()
        assert ok, f"is_available: {reason}"
        result = plugin.synthesize_input(
            scope_role="voice_caller",
            ground_truth_hint="Testing",
        )
        assert result.file_path, "no file_path returned"
        p = Path(result.file_path)
        assert p.exists(), f"file does not exist: {p}"
        size = p.stat().st_size
        assert size > 100, f"file suspiciously small ({size} bytes)"
        return f"synthesized {size} bytes via elevenlabs at {p.name}"
    finally:
        if prior_provider is None:
            os.environ.pop("PUZZLEEVAL_TTS_PROVIDER", None)
        else:
            os.environ["PUZZLEEVAL_TTS_PROVIDER"] = prior_provider


# ---------------------------------------------------------------------------
# 5. OpenAI Whisper STT — validates transcription plugin end to end
# ---------------------------------------------------------------------------

def test_openai_whisper_stt():
    # Synthesize a known phrase, then transcribe it. The roundtrip proves
    # both ends of the voice plugin chain.
    from puzzleeval.tool_plugins.tts import TTSPlugin
    from puzzleeval.tool_plugins.transcription import TranscriptionPlugin
    prior_stt = os.environ.get("PUZZLEEVAL_STT_PROVIDER")
    prior_tts = os.environ.get("PUZZLEEVAL_TTS_PROVIDER")
    os.environ["PUZZLEEVAL_STT_PROVIDER"] = "openai_whisper"
    os.environ["PUZZLEEVAL_TTS_PROVIDER"] = "openai_tts"
    try:
        tts = TTSPlugin()
        stt = TranscriptionPlugin()
        ok, reason = stt.is_available()
        assert ok, f"transcription is_available: {reason}"
        # Synthesize
        synth = tts.synthesize_input(
            scope_role="voice_caller",
            ground_truth_hint="The quick brown fox jumps",
        )
        assert synth.file_path and Path(synth.file_path).exists()
        # Transcribe the file. The transcription plugin's evaluate_output
        # accepts {audio_path: ...} and returns transcript in .detail.
        verdict = stt.evaluate_output(
            response={"audio_path": synth.file_path},
            expected="The quick brown fox jumps",
            criteria=[],
        )
        transcript = (verdict.detail or {}).get("transcript", "")
        assert transcript, f"no transcript returned, reasoning={verdict.reasoning!r}"
        # Basic sanity: at least 2 of the 5 key words appear. Whisper is
        # forgiving on punctuation/case; we don't demand exact match.
        tl = transcript.lower()
        hits = sum(w in tl for w in ["quick", "brown", "fox", "jumps", "the"])
        assert hits >= 3, f"Whisper transcript too different: {transcript!r}"
        return f"transcript: {transcript!r}"
    finally:
        if prior_stt is None:
            os.environ.pop("PUZZLEEVAL_STT_PROVIDER", None)
        else:
            os.environ["PUZZLEEVAL_STT_PROVIDER"] = prior_stt
        if prior_tts is None:
            os.environ.pop("PUZZLEEVAL_TTS_PROVIDER", None)
        else:
            os.environ["PUZZLEEVAL_TTS_PROVIDER"] = prior_tts


# ---------------------------------------------------------------------------
# 6. voice_realtime loopback — one-turn synth + serve + STT roundtrip
# ---------------------------------------------------------------------------

def test_voice_realtime_oneturn():
    """Drive voice_realtime through its drive_conversation path locally.
    No external provider call; validates that the plugin's internal audio
    pipeline (TTS -> serve -> STT) is intact under registry credentials."""
    import urllib.request
    from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
    plugin = VoiceRealtimePlugin()
    try:
        with tempfile.TemporaryDirectory() as td:
            plugin.set_session_dir(Path(td))
            # Fake agent responder that returns text directly (simulates
            # what a REST harness would).
            call_counts = [0]

            def responder(turn_idx, user_audio_url, state):
                call_counts[0] += 1
                # Verify the caller audio URL is reachable.
                try:
                    with urllib.request.urlopen(user_audio_url, timeout=5) as r:
                        body = r.read()
                    assert len(body) > 100, (
                        f"caller audio blob suspiciously small: {len(body)}"
                    )
                except Exception as exc:  # noqa: BLE001
                    raise AssertionError(f"caller URL fetch failed: {exc}")
                return {"text": "Confirming your order."}

            script = [{
                "user_text": "Hi, I'd like to confirm my order please.",
                "expected_agent_contains": "confirming",
            }]
            run = plugin.drive_conversation(
                script=script, agent_responder=responder,
            )
            assert run["overall_passed"], (
                f"loopback did not pass: {run['turns']}"
            )
            assert call_counts[0] == 1
            assert len(run["audio_paths"]) >= 1, (
                f"no audio artifacts: {run['audio_paths']}"
            )
            return (f"loopback OK -- {len(run['audio_paths'])} artifacts, "
                    f"score={run['overall_score']}")
    finally:
        plugin.shutdown()


# ---------------------------------------------------------------------------
# 7. Provider registry substring match — proves candidates get creds
# ---------------------------------------------------------------------------

def test_provider_registry_lookup():
    from puzzleeval.provider_registry import load_registry, get_credentials
    reg = load_registry()
    assert not reg.is_empty(), "registry is empty"
    # Verify substring match for the cases we care about in the voice scenario.
    checks = [
        ("OpenAI",               "OpenAI Realtime",           "OPENAI_API_KEY"),
        ("OpenAI",               "OpenAI Chat Completions",   "OPENAI_API_KEY"),
        ("OpenAI",               "gpt-4o-mini",               "OPENAI_API_KEY"),
        ("ElevenLabs",           "ElevenLabs Conversational AI", "ELEVENLABS_API_KEY"),
        ("ElevenLabs",           "ElevenLabs TTS Stream",     "ELEVENLABS_API_KEY"),
        ("Mindee SAS",           "Mindee",                    "MINDEE_API_KEY"),
    ]
    missing = []
    for provider, candidate, required_key in checks:
        creds = get_credentials(reg, provider, candidate)
        if not creds or required_key not in creds:
            missing.append(f"{provider}/{candidate!r} -> {creds}")
    assert not missing, "substring match failures:\n  " + "\n  ".join(missing)
    return f"{len(checks)} candidate-name lookups all resolved to correct env vars"


# ---------------------------------------------------------------------------
# 8. Plugin readiness after registry propagation
# ---------------------------------------------------------------------------

def test_plugin_readiness_via_registry():
    """With only registry (no ad-hoc shell exports), every plugin we
    expect for the voice scenario must be available."""
    from puzzleeval.tool_plugins import list_plugins
    must_be_available = {"tts", "transcription", "voice_realtime",
                         "conversation_simulator", "code_execution",
                         "webhook_receiver", "outbound_delivery", "vision"}
    lines = []
    missing = []
    for p in list_plugins():
        ok, reason = p.is_available()
        lines.append(f"  {p.name}: {'OK' if ok else 'UNAVAILABLE ' + reason!r}")
        if p.name in must_be_available and not ok:
            missing.append(f"{p.name}: {reason}")
    assert not missing, "plugins unavailable:\n  " + "\n  ".join(missing)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TESTS = [
    ("anthropic: haiku hello-world",               test_anthropic_haiku),
    ("openai: chat completions minimal",           test_openai_chat),
    ("provider_registry: substring lookup",        test_provider_registry_lookup),
    ("plugin readiness via registry",              test_plugin_readiness_via_registry),
    ("tts plugin: openai_tts synthesis",           test_openai_tts_plugin),
    ("tts plugin: elevenlabs synthesis",           test_elevenlabs_tts_plugin),
    ("transcription plugin: openai_whisper STT",   test_openai_whisper_stt),
    ("voice_realtime: one-turn loopback",          test_voice_realtime_oneturn),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip", nargs="*", default=[],
                        help="skip tests containing any of these substrings")
    args = parser.parse_args()

    print(f"\n{BOLD}Real-API smoke tests{RESET}")
    print(f"Working dir: {ROOT}")
    print(f"Skip list:   {args.skip or '(none)'}\n")

    passes, fails, skipped = 0, 0, 0
    for name, fn in TESTS:
        if run_test(name, fn, args.skip):
            if RESULTS[-1][1] == "skip":
                skipped += 1
            else:
                passes += 1
        else:
            fails += 1

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}Summary:{RESET} {passes} pass / {fails} fail / {skipped} skip")
    if fails:
        print(f"\n{RED}{BOLD}FAILURES FOUND -- fix before real run:{RESET}")
        for n, s, d in RESULTS:
            if s == "fail":
                print(f"  [XX] {n}: {d}")
        sys.exit(1)
    else:
        print(f"\n{GREEN}{BOLD}ALL PASSED -- real run is cleared.{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
