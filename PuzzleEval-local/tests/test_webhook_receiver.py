"""Tests for the webhook_receiver plugin.

End-to-end via the loopback HTTP server: synthesize → POST as a
"candidate harness" → evaluate. Uses requests when installed, urllib
otherwise (no required test deps).
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

import pytest

from puzzleeval.tool_plugins import get_plugin
from puzzleeval.tool_plugins.webhook_receiver import (
    CapturedRequest,
    WebhookReceiverPlugin,
    get_plugin_instance,
)


@pytest.fixture
def plugin():
    p = get_plugin_instance()
    p.clear()
    yield p
    p.clear()


def _post(url: str, body_bytes: bytes, headers: dict | None = None) -> int:
    req = urllib.request.Request(
        url, data=body_bytes, method="POST",
        headers=headers or {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------------------
# Plugin contract
# ---------------------------------------------------------------------------


def test_plugin_registers():
    assert get_plugin("webhook_receiver") is not None


def test_capabilities_declares_modalities(plugin):
    caps = plugin.capabilities()
    assert "webhook_event" in caps.input_types
    assert "webhook_callback" in caps.output_types
    assert caps.synthesizes_input is True
    assert caps.evaluates_output is True


def test_is_available_true_no_creds(plugin):
    ok, _ = plugin.is_available()
    assert ok is True


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def test_synthesize_returns_url_and_token(plugin):
    result = plugin.synthesize_input(
        scope_role="inbound_chat", ground_truth_hint="Hello agent",
    )
    assert result.inline_data is not None
    assert "callback_url" in result.inline_data
    assert "/hook/" in result.inline_data["callback_url"]
    assert result.ground_truth["token"]


def test_synthesize_slack_shape(plugin):
    result = plugin.synthesize_input(
        scope_role="slack_bot", ground_truth_hint="ping",
        shape="slack",
    )
    payload = result.inline_data["payload"]
    assert payload["type"] == "event_callback"
    assert payload["event"]["type"] == "app_mention"
    assert payload["event"]["text"] == "ping"


def test_synthesize_intercom_shape(plugin):
    result = plugin.synthesize_input(
        scope_role="intercom", ground_truth_hint="customer question",
        shape="intercom",
    )
    payload = result.inline_data["payload"]
    assert payload["topic"] == "conversation.user.created"
    assert "customer question" in payload["data"]["item"]["source"]["body"]


def test_synthesize_twilio_shape(plugin):
    result = plugin.synthesize_input(
        scope_role="sms", ground_truth_hint="reply yes",
        shape="twilio_sms",
    )
    payload = result.inline_data["payload"]
    assert "MessageSid" in payload
    assert payload["Body"] == "reply yes"


def test_synthesize_stripe_shape(plugin):
    result = plugin.synthesize_input(
        scope_role="payments", ground_truth_hint="payment ok",
        shape="stripe",
    )
    payload = result.inline_data["payload"]
    assert payload["type"] == "payment_intent.succeeded"


def test_synthesize_github_shape(plugin):
    result = plugin.synthesize_input(
        scope_role="issue_triage", ground_truth_hint="bug filed",
        shape="github",
    )
    payload = result.inline_data["payload"]
    assert payload["action"] == "opened"
    assert payload["issue"]["title"] == "bug filed"


# ---------------------------------------------------------------------------
# End-to-end capture + evaluate
# ---------------------------------------------------------------------------


def test_post_to_callback_url_is_captured(plugin):
    result = plugin.synthesize_input(
        scope_role="inbound", ground_truth_hint="received message body 42",
    )
    url = result.inline_data["callback_url"]
    token = result.ground_truth["token"]
    body = json.dumps({"text": "received message body 42"}).encode()
    started = time.time()
    status = _post(url, body)
    assert status == 200
    captured = plugin.captured_for_token(token)
    assert len(captured) == 1
    assert captured[0].body_json()["text"] == "received message body 42"
    assert captured[0].received_at >= started


def test_evaluate_passes_when_text_matched(plugin):
    result = plugin.synthesize_input(
        scope_role="inbound", ground_truth_hint="answer is 42",
    )
    url = result.inline_data["callback_url"]
    body = json.dumps({"text": "answer is 42 — confirmed"}).encode()
    _post(url, body)
    verdict = plugin.evaluate_output(
        response={"token": result.ground_truth["token"]},
        expected=result.ground_truth,
    )
    assert verdict.passed is True
    assert verdict.score >= 0.6


def test_evaluate_fails_with_no_callback(plugin):
    verdict = plugin.evaluate_output(
        response={"token": "nonexistent"},
        expected={"token": "nonexistent", "expected_text_substring": "x"},
    )
    assert verdict.passed is False
    assert verdict.score == 0.0
    assert "no callback received" in verdict.reasoning


def test_evaluate_partial_credit_when_received_but_no_match(plugin):
    result = plugin.synthesize_input(
        scope_role="inbound", ground_truth_hint="needle",
    )
    url = result.inline_data["callback_url"]
    body = json.dumps({"text": "haystack"}).encode()
    _post(url, body)
    verdict = plugin.evaluate_output(
        response={"token": result.ground_truth["token"]},
        expected=result.ground_truth,
    )
    # 0.6 for receipt, 0.0 for substring miss
    assert verdict.score == pytest.approx(0.6, abs=1e-3)


def test_evaluate_rejects_malformed_expected(plugin):
    verdict = plugin.evaluate_output(response={}, expected="not a dict")
    assert verdict.passed is False
    assert verdict.fallback_reason == "malformed_expected"


def test_clear_buffer(plugin):
    result = plugin.synthesize_input(scope_role="x", ground_truth_hint="y")
    _post(result.inline_data["callback_url"], b"{}")
    assert len(plugin.captured_for_token(result.ground_truth["token"])) == 1
    plugin.clear()
    assert len(plugin.captured_for_token(result.ground_truth["token"])) == 0


def test_since_filter_excludes_old_captures(plugin):
    result = plugin.synthesize_input(scope_role="x", ground_truth_hint="y")
    url = result.inline_data["callback_url"]
    token = result.ground_truth["token"]
    _post(url, b"{}")
    # Wait, then capture the second
    time.sleep(0.05)
    cutoff = time.time()
    time.sleep(0.05)
    _post(url, b"{}")
    after = plugin.captured_for_token(token, since=cutoff)
    assert len(after) == 1
    all_caps = plugin.captured_for_token(token)
    assert len(all_caps) == 2
