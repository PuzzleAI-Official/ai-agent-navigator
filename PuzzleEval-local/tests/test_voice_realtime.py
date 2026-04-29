"""Tests for the voice_realtime plugin.

Spins up the voice loopback server, simulates a candidate fetching the
caller audio + posting back a TwiML / NCCO / JSON response, then
exercises the evaluator's text-extraction paths.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

import pytest

from puzzleeval.tool_plugins import get_plugin
from puzzleeval.tool_plugins.voice_realtime import (
    CapturedVoiceTurn,
    VoiceRealtimePlugin,
    get_plugin_instance,
    _extract_agent_text,
)


@pytest.fixture
def plugin():
    p = get_plugin_instance()
    p.clear()
    yield p
    p.clear()


def _http_post(url: str, body: bytes, content_type: str) -> int:
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _http_get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


# ---------------------------------------------------------------------------
# Plugin contract
# ---------------------------------------------------------------------------


def test_plugin_registers():
    assert get_plugin("voice_realtime") is not None


def test_capabilities(plugin):
    caps = plugin.capabilities()
    assert "voice_turn" in caps.input_types
    assert "voice_turn" in caps.output_types
    assert caps.synthesizes_input is True
    assert caps.evaluates_output is True


def test_is_available_true(plugin):
    ok, _ = plugin.is_available()
    assert ok is True


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def test_synthesize_returns_audio_and_callback_urls(plugin):
    res = plugin.synthesize_input(
        scope_role="voice_agent",
        ground_truth_hint="Hello, what are your hours?",
        expected_response="We are open 9 to 5",
    )
    info = res.inline_data
    assert "/audio/" in info["audio_url"]
    assert "/voice/" in info["callback_url"]
    assert info["spoken_text"] == "Hello, what are your hours?"
    assert res.ground_truth["expected_text_substring"]


def test_synthesize_twilio_shape_instructions(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="x", shape="twilio",
    )
    assert "Twilio" in res.inline_data["instructions"]


def test_synthesize_vonage_shape_instructions(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="x", shape="vonage",
    )
    assert "Vonage" in res.inline_data["instructions"]


def test_synthesize_audio_file_serveable(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="placeholder text",
    )
    code, body = _http_get(res.inline_data["audio_url"])
    assert code == 200
    assert len(body) > 0


# ---------------------------------------------------------------------------
# Capture + evaluation
# ---------------------------------------------------------------------------


def test_twiml_response_extracted_and_scored(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="What's your refund policy?",
        expected_response="Refunds within 30 days",
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Say>Refunds within 30 days, no questions.</Say></Response>'
    )
    status = _http_post(
        res.inline_data["callback_url"], twiml.encode(), "application/xml",
    )
    assert status == 200
    verdict = plugin.evaluate_output(response={}, expected=res.ground_truth)
    assert verdict.passed is True
    assert verdict.score >= 0.6
    assert verdict.detail["source"] == "twiml"


def test_ncco_response_extracted(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="any topic",
        expected_response="hello there",
        shape="vonage",
    )
    ncco = [{"action": "talk", "text": "hello there from the agent"}]
    status = _http_post(
        res.inline_data["callback_url"],
        json.dumps(ncco).encode(), "application/json",
    )
    assert status == 200
    verdict = plugin.evaluate_output(response={}, expected=res.ground_truth)
    assert verdict.passed is True
    assert verdict.detail["source"] == "ncco"


def test_generic_json_response_extracted(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="weather?",
        expected_response="seventy degrees",
    )
    body = json.dumps({"response_text": "It is seventy degrees today"}).encode()
    _http_post(res.inline_data["callback_url"], body, "application/json")
    verdict = plugin.evaluate_output(response={}, expected=res.ground_truth)
    assert verdict.passed is True
    assert "json" in verdict.detail["source"]


def test_no_response_returns_score_zero(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="x", expected_response="y",
    )
    verdict = plugin.evaluate_output(response={}, expected=res.ground_truth)
    assert verdict.passed is False
    assert verdict.score == 0.0


def test_no_expected_text_still_credits_capture(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="x", expected_response="",
    )
    res.ground_truth["expected_text_substring"] = ""
    body = json.dumps({"text": "anything"}).encode()
    _http_post(res.inline_data["callback_url"], body, "application/json")
    verdict = plugin.evaluate_output(response={}, expected=res.ground_truth)
    assert verdict.passed is True


def test_audio_blob_path_recorded(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="x",
    )
    token = res.ground_truth["token"]
    recording_url = res.inline_data["recording_url"]
    fake_audio = b"\x00" * 100
    status = _http_post(recording_url, fake_audio, "audio/wav")
    assert status == 200
    turns = plugin.turns_for_token(token)
    assert len(turns) == 1
    assert turns[0].audio_path
    assert turns[0].audio_bytes_len == 100


def test_evaluator_rejects_malformed_expected(plugin):
    v = plugin.evaluate_output(response={}, expected="not a dict")
    assert v.passed is False
    assert v.fallback_reason == "malformed_expected"


def test_evaluator_partial_credit_on_unmatched_text(plugin):
    res = plugin.synthesize_input(
        scope_role="voice", ground_truth_hint="x",
        expected_response="zebra",
    )
    body = json.dumps({"text": "elephant"}).encode()
    _http_post(res.inline_data["callback_url"], body, "application/json")
    verdict = plugin.evaluate_output(response={}, expected=res.ground_truth)
    # Captured + extracted text → 0.5; substring missed → no bonus
    assert verdict.score == pytest.approx(0.5, abs=1e-3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_extract_agent_text_handles_self_closing_play():
    turn = CapturedVoiceTurn(
        token="t", received_at=0,
        twiml_text=(
            '<Response><Play>https://example.com/audio.mp3</Play></Response>'
        ),
    )
    text, source = _extract_agent_text(turn)
    assert "example.com" in text
    assert source == "twiml"


def test_extract_agent_text_returns_empty_for_nothing():
    turn = CapturedVoiceTurn(token="t", received_at=0)
    text, source = _extract_agent_text(turn)
    assert text == ""
    assert source == ""
