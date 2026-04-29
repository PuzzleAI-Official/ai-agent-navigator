"""Tests for the outbound_delivery plugin.

Spins up SMTP / channel HTTP / SMS HTTP receivers on ephemeral local
ports, simulates a "candidate harness" sending a message, then checks
the plugin's evaluator returns the right verdict.
"""

from __future__ import annotations

import json
import smtplib
import time
import urllib.parse
import urllib.request
import urllib.error
from email.mime.text import MIMEText

import pytest

from puzzleeval.tool_plugins import get_plugin
from puzzleeval.tool_plugins.outbound_delivery import (
    OutboundDeliveryPlugin,
    get_plugin_instance,
)


@pytest.fixture
def plugin():
    p = get_plugin_instance()
    p.clear()
    yield p
    p.clear()


def _http_post(url: str, body: bytes, content_type: str = "application/json") -> int:
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _send_smtp(host: str, port: int, sender: str, recipient: str, subject: str, body: str) -> None:
    msg = MIMEText(body)
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    with smtplib.SMTP(host, port, timeout=5) as smtp:
        smtp.sendmail(sender, [recipient], msg.as_string())


# ---------------------------------------------------------------------------
# Plugin contract
# ---------------------------------------------------------------------------


def test_plugin_registers():
    assert get_plugin("outbound_delivery") is not None


def test_capabilities(plugin):
    caps = plugin.capabilities()
    assert "action" in caps.output_types
    assert "outbound_message" in caps.output_types
    assert caps.synthesizes_input is True
    assert caps.evaluates_output is True


# ---------------------------------------------------------------------------
# Email (SMTP) end-to-end
# ---------------------------------------------------------------------------


def test_smtp_synthesize_returns_connection_details(plugin):
    result = plugin.synthesize_input(
        scope_role="email_notifier", ground_truth_hint="welcome message",
        channel="email",
    )
    assert result.inline_data["smtp_host"]
    assert isinstance(result.inline_data["smtp_port"], int)
    assert result.ground_truth["channel"] == "email"
    assert "@" in result.ground_truth["expected_recipient"]


def test_smtp_message_captured_and_evaluated(plugin):
    result = plugin.synthesize_input(
        scope_role="email_notifier",
        ground_truth_hint="welcome to PuzzleEval",
        channel="email",
    )
    info = result.inline_data
    _send_smtp(
        info["smtp_host"], info["smtp_port"],
        sender=info["from_addr"], recipient=info["to_addr"],
        subject=info["subject"], body="welcome to PuzzleEval and have fun",
    )
    # Give SMTP server a tick to finish writing
    time.sleep(0.2)
    captured = plugin.emails_received(to=info["to_addr"])
    assert len(captured) == 1
    assert "welcome to PuzzleEval" in captured[0].body_text

    verdict = plugin.evaluate_output(
        response={}, expected=result.ground_truth,
    )
    assert verdict.passed is True
    assert verdict.score >= 0.6


def test_smtp_evaluator_fails_when_nothing_sent(plugin):
    result = plugin.synthesize_input(
        scope_role="email_notifier",
        ground_truth_hint="never sent", channel="email",
    )
    verdict = plugin.evaluate_output(
        response={}, expected=result.ground_truth,
    )
    assert verdict.passed is False
    assert verdict.score == 0.0


# ---------------------------------------------------------------------------
# Channel (Slack) end-to-end
# ---------------------------------------------------------------------------


def test_channel_synthesize_returns_webhook_url(plugin):
    result = plugin.synthesize_input(
        scope_role="slack_notifier", ground_truth_hint="deploy succeeded",
        channel="slack", recipient="#deploys",
    )
    assert "webhook_url" in result.inline_data
    assert "/webhooks/" in result.inline_data["webhook_url"]


def test_channel_message_captured_and_evaluated(plugin):
    result = plugin.synthesize_input(
        scope_role="slack_notifier", ground_truth_hint="deploy succeeded",
        channel="slack", recipient="#deploys",
    )
    body = json.dumps({
        "channel": "#deploys",
        "text": "deploy succeeded at 14:30",
        "username": "deploybot",
    }).encode()
    status = _http_post(result.inline_data["webhook_url"], body)
    assert status == 200
    captured = plugin.channel_messages(channel="#deploys")
    assert len(captured) == 1
    verdict = plugin.evaluate_output(
        response={}, expected=result.ground_truth,
    )
    assert verdict.passed is True


# ---------------------------------------------------------------------------
# SMS (Twilio shape) end-to-end
# ---------------------------------------------------------------------------


def test_sms_synthesize_returns_endpoint(plugin):
    result = plugin.synthesize_input(
        scope_role="sms_alerts", ground_truth_hint="alert text",
        channel="sms", recipient="+15555550100",
    )
    assert "sms_endpoint" in result.inline_data
    assert "/Messages.json" in result.inline_data["sms_endpoint"]


def test_sms_message_captured_and_evaluated(plugin):
    result = plugin.synthesize_input(
        scope_role="sms_alerts", ground_truth_hint="alert text 9001",
        channel="sms", recipient="+15555550100",
    )
    payload = urllib.parse.urlencode({
        "To": "+15555550100", "From": "+15555550101",
        "Body": "alert text 9001 fired",
    }).encode()
    status = _http_post(
        result.inline_data["sms_endpoint"], payload,
        content_type="application/x-www-form-urlencoded",
    )
    assert status == 200
    sms = plugin.sms_messages(to="+15555550100")
    assert len(sms) == 1
    assert "9001" in sms[0].body
    verdict = plugin.evaluate_output(
        response={}, expected=result.ground_truth,
    )
    assert verdict.passed is True


def test_sms_recipient_filter(plugin):
    result = plugin.synthesize_input(
        scope_role="sms", ground_truth_hint="x",
        channel="sms", recipient="+15555550111",
    )
    payload = urllib.parse.urlencode({
        "To": "+15555550999", "From": "+15555550000", "Body": "wrong number",
    }).encode()
    _http_post(
        result.inline_data["sms_endpoint"], payload,
        content_type="application/x-www-form-urlencoded",
    )
    # Recipient filter excludes wrong number
    matched = plugin.sms_messages(to="+15555550111")
    assert matched == []
    all_msgs = plugin.sms_messages()
    assert len(all_msgs) == 1


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_clear_resets_all_buffers(plugin):
    res = plugin.synthesize_input(
        scope_role="email", ground_truth_hint="x", channel="email",
    )
    _send_smtp(
        res.inline_data["smtp_host"], res.inline_data["smtp_port"],
        res.inline_data["from_addr"], res.inline_data["to_addr"],
        "test", "body",
    )
    time.sleep(0.1)
    assert len(plugin.emails_received()) >= 1
    plugin.clear()
    assert plugin.emails_received() == []
    assert plugin.channel_messages() == []
    assert plugin.sms_messages() == []


def test_evaluator_rejects_malformed_expected(plugin):
    v = plugin.evaluate_output(response={}, expected="not a dict")
    assert v.passed is False
    assert v.fallback_reason == "malformed_expected"
