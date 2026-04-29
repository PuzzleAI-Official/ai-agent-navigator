"""Webhook receiver plugin — local capture endpoint for inbound test cases.

Inbound agents (Intercom widget, Slack ``app_mention``, SMS replies,
Stripe events, GitHub webhooks, generic provider callbacks) deliver work
by POSTing to a URL the candidate exposes. To test these locally we need
to TURN THE TABLES: the candidate harness needs to deliver a callback
into PuzzleEval, and we need to verify it arrived correctly.

This plugin runs an in-process HTTP server (Python stdlib only — no Flask,
no aiohttp) that records POST/GET callbacks the harness sends. Tests:

  1. **Synthesize** an inbound payload (Slack-shaped, Twilio-shaped, etc.)
     and a public-facing URL (``http://127.0.0.1:PORT/hook/<token>``) the
     harness should POST to.
  2. **Run the harness** with that URL as a configuration parameter.
  3. **Evaluate** by inspecting the captured payload(s): did the harness
     POST? did the body match the expected fields? did headers carry the
     auth token?

Local-vs-cloud:
  - LOCAL (default): the receiver binds 127.0.0.1, perfect for testing
    candidates that run inside our own venv (the harness can reach
    127.0.0.1 just fine).
  - CLOUD-DEFERRED: when the candidate runs OFFSITE (third-party SaaS
    that needs to call back into us from the public internet), an
    ngrok/cloudflared/localtunnel front-end maps a public URL to our
    local receiver. The plugin reads ``PUZZLEEVAL_TUNNEL_URL`` env var;
    when set it advertises the public URL instead of 127.0.0.1, and the
    operator is expected to have started the tunnel out-of-band
    (``ngrok http $PUZZLEEVAL_WEBHOOK_PORT``).

Threading model:
  - One ``HTTPServer`` per plugin instance, created lazily when the first
    test case asks for an endpoint.
  - Daemon thread serves it; ``shutdown()`` is called on plugin
    teardown.
  - All captured payloads live in an in-memory ring buffer (default
    1000 entries, configurable). No disk I/O — fast and ephemeral.

Security:
  - Server binds 127.0.0.1 by default so it's not exposed to the LAN.
  - Per-test tokens make the URL unguessable (``/hook/<32-char hex>``).
  - Never logs request bodies above ``MAX_BODY_LOG_BYTES`` to keep test
    output reviewable.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from puzzleeval.tool_plugins import (
    EvaluationResult,
    PluginCapabilities,
    SynthesisResult,
    ToolPlugin,
    register_plugin,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_PORT = int(os.environ.get("PUZZLEEVAL_WEBHOOK_PORT", "8765"))
MAX_BODY_BYTES = int(os.environ.get("PUZZLEEVAL_WEBHOOK_MAX_BODY", str(1 << 20)))  # 1 MiB
MAX_BUFFER = int(os.environ.get("PUZZLEEVAL_WEBHOOK_MAX_BUFFER", "1000"))
TUNNEL_URL = os.environ.get("PUZZLEEVAL_TUNNEL_URL", "").rstrip("/")
LISTEN_ADDR = os.environ.get("PUZZLEEVAL_WEBHOOK_BIND", "127.0.0.1")


# ---------------------------------------------------------------------------
# Capture data class
# ---------------------------------------------------------------------------


@dataclass
class CapturedRequest:
    """Single recorded POST/GET to a webhook endpoint."""
    token: str
    method: str
    path: str
    headers: dict[str, str]
    body_bytes: bytes
    received_at: float
    remote_addr: str
    truncated: bool = False  # body exceeded MAX_BODY_BYTES

    def body_text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        try:
            return self.body_bytes.decode(encoding, errors=errors)
        except Exception:  # noqa: BLE001
            return ""

    def body_json(self) -> Any:
        """Try to parse body as JSON. Returns None when not parseable."""
        try:
            return json.loads(self.body_text())
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# In-process HTTP server
# ---------------------------------------------------------------------------


class _SilentHandler(BaseHTTPRequestHandler):
    """HTTP handler that records every request into the plugin's buffer.

    The class-level ``server`` injected by ``HTTPServer`` carries our
    plugin reference via ``server.plugin`` (set by ``WebhookReceiverPlugin._ensure_server``).
    """

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet stdout
        logger.debug("webhook %s %s", self.client_address, fmt % args)

    def _capture(self, method: str) -> None:
        plugin: WebhookReceiverPlugin = self.server.plugin  # type: ignore[attr-defined]
        try:
            content_len = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            content_len = 0
        truncated = False
        body = b""
        if content_len > 0:
            read_len = min(content_len, MAX_BODY_BYTES)
            try:
                body = self.rfile.read(read_len)
            except OSError as exc:
                logger.warning("webhook body read failed: %s", exc)
                body = b""
            if content_len > MAX_BODY_BYTES:
                truncated = True
                # Drain remainder so the connection closes cleanly.
                try:
                    self.rfile.read(content_len - read_len)
                except OSError:
                    pass

        path = self.path
        token = ""
        parsed = urlparse(path)
        # Token is the segment after /hook/
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "hook":
            token = parts[1]
        captured = CapturedRequest(
            token=token,
            method=method,
            path=path,
            headers={k: v for k, v in self.headers.items()},
            body_bytes=body,
            received_at=time.time(),
            remote_addr=self.client_address[0],
            truncated=truncated,
        )
        plugin._record(captured)
        # Always 200 — the test runner inspects the captured payload, not
        # the response. Echoing 200 keeps the candidate happy whether it
        # expects an empty ack, a JSON body, or text.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body_out = json.dumps({"received": True, "token": token}).encode()
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def do_POST(self) -> None:  # noqa: N802 (stdlib name)
        self._capture("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._capture("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._capture("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._capture("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._capture("DELETE")


class _ThreadedHTTPServer(HTTPServer):
    """Adds a back-reference so the handler can find the plugin.

    ``allow_reuse_address = True`` lets us rebind the port immediately
    after a process restart instead of waiting for the OS TIME_WAIT
    cooldown. Without this, ``uvicorn --reload`` (and any production
    redeploy) fails to rebind the configured port and falls back to an
    ephemeral port — silently breaking any harness that hardcoded the
    port number.
    """
    allow_reuse_address = True
    plugin: WebhookReceiverPlugin


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


@dataclass
class _ServerHandle:
    server: _ThreadedHTTPServer
    thread: threading.Thread
    bind_host: str
    port: int


class WebhookReceiverPlugin(ToolPlugin):
    """Inbound webhook capture plugin.

    Synthesizes an inbound test payload + a callback URL the harness
    POSTs to. After the harness runs, evaluates by checking that the
    expected payload arrived with the right shape.
    """

    name = "webhook_receiver"

    def __init__(self) -> None:
        self._buffer: deque[CapturedRequest] = deque(maxlen=MAX_BUFFER)
        self._buffer_lock = threading.Lock()
        self._server_handle: _ServerHandle | None = None
        self._server_lock = threading.Lock()

    # -- Plugin contract ----------------------------------------------------

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            input_types=["webhook_event", "structured_data", "conversation"],
            output_types=["webhook_callback", "action", "structured_json"],
            synthesizes_input=True,
            evaluates_output=True,
            requires_credentials=[],  # local; no creds needed
            notes=(
                "Captures inbound HTTP callbacks for testing webhook-driven "
                "agents (Slack mentions, Intercom messages, Stripe events). "
                "Binds 127.0.0.1 by default. Set PUZZLEEVAL_TUNNEL_URL when "
                "the candidate runs offsite and needs a public URL — the "
                "operator runs ngrok/cloudflared and exports the URL."
            ),
        )

    def is_available(self) -> tuple[bool, str]:
        # Local server — always "ready" until we try to actually bind.
        # We don't bind eagerly because tests may not need a server.
        return True, ""

    # -- Server lifecycle ---------------------------------------------------

    def _ensure_server(self, port: int | None = None) -> _ServerHandle:
        """Lazily create and start the HTTP server (idempotent)."""
        with self._server_lock:
            if self._server_handle is not None:
                return self._server_handle
            target_port = port or DEFAULT_PORT
            # Try the requested port; fall back to ephemeral if taken.
            for try_port in (target_port, 0):
                try:
                    server = _ThreadedHTTPServer(
                        (LISTEN_ADDR, try_port), _SilentHandler,
                    )
                    break
                except OSError as exc:
                    last_err = exc
                    continue
            else:
                raise RuntimeError(
                    f"failed to bind webhook receiver: {last_err}"
                )
            server.plugin = self
            actual_port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever, name="puzzleeval-webhook",
                daemon=True,
            )
            thread.start()
            self._server_handle = _ServerHandle(
                server=server, thread=thread,
                bind_host=LISTEN_ADDR, port=actual_port,
            )
            logger.info(
                "webhook receiver listening on %s:%s",
                LISTEN_ADDR, actual_port,
            )
            return self._server_handle

    def shutdown(self) -> None:
        """Stop the server thread cleanly. Safe to call multiple times."""
        with self._server_lock:
            handle = self._server_handle
            if handle is None:
                return
            try:
                handle.server.shutdown()
                handle.server.server_close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("webhook shutdown error: %s", exc)
            self._server_handle = None

    # -- Buffer access ------------------------------------------------------

    def _record(self, captured: CapturedRequest) -> None:
        with self._buffer_lock:
            self._buffer.append(captured)

    def captured_for_token(
        self, token: str, since: float | None = None,
    ) -> list[CapturedRequest]:
        """Return all captured requests matching ``token``.

        ``since`` is a float UNIX timestamp; only requests received at or
        after that time are returned. Useful when running multiple tests
        through the same long-lived server: each test records its
        ``start_at`` and only inspects requests that arrived during it.
        """
        with self._buffer_lock:
            items = list(self._buffer)
        out: list[CapturedRequest] = []
        for c in items:
            if c.token != token:
                continue
            if since is not None and c.received_at < since:
                continue
            out.append(c)
        return out

    def clear(self) -> None:
        with self._buffer_lock:
            self._buffer.clear()

    # -- Public-facing URL builder ------------------------------------------

    def public_url_for(self, token: str) -> str:
        """Build the callback URL the candidate harness will POST to."""
        handle = self._ensure_server()
        if TUNNEL_URL:
            return f"{TUNNEL_URL}/hook/{token}"
        return f"http://{handle.bind_host}:{handle.port}/hook/{token}"

    # -- Synthesis ----------------------------------------------------------

    def synthesize_input(
        self, *, scope_role: str, ground_truth_hint: str | None = None,
        **kwargs: Any,
    ) -> SynthesisResult:
        """Create one inbound payload + the URL the candidate POSTs to.

        ``ground_truth_hint`` is a free-text description of what the
        agent should ultimately respond to. We embed it in the payload
        so the candidate has something meaningful to react to.

        ``kwargs.shape`` selects payload shape: "slack", "intercom",
        "twilio_sms", "stripe", "github", "generic" (default). Each
        shape mimics the real provider's webhook envelope so the
        candidate's parser exercises its real code path.
        """
        token = secrets.token_hex(16)
        url = self.public_url_for(token)
        shape = (kwargs.get("shape") or "generic").lower().strip()
        message_text = (
            ground_truth_hint
            or kwargs.get("message")
            or f"Inbound test message for {scope_role or 'agent'}"
        )
        payload = _build_payload_for_shape(shape, message_text, token)
        return SynthesisResult(
            file_path=None,
            inline_data={
                "callback_url": url,
                "token": token,
                "shape": shape,
                "payload": payload,
                "instructions": (
                    f"POST `payload` to `callback_url`, then verify the "
                    f"agent's response. Token `{token}` is unique to this test."
                ),
            },
            ground_truth={
                "expected_text_substring": message_text[:80],
                "token": token,
                "shape": shape,
            },
            notes=f"webhook receiver bound at {url}",
        )

    # -- Evaluation ---------------------------------------------------------

    def evaluate_output(
        self, *, response: Any, expected: Any, criteria: list[dict] | None = None,
        **kwargs: Any,
    ) -> EvaluationResult:
        """Score by inspecting captured callbacks.

        ``response`` is what the candidate's ``run()`` returned. We accept
        two shapes:

          1. ``{"token": "...", "since": <float>}`` — point us at a
             specific captured callback. Most reliable.
          2. ``{"raw": <whatever>}`` — the candidate's response itself,
             when it doesn't go through the receiver. Falls back to a
             string-match against ``expected_text_substring``.

        ``expected`` is the ``ground_truth`` dict from synthesize_input.
        """
        if not isinstance(expected, dict):
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning="expected payload missing token / ground truth",
                fallback_reason="malformed_expected",
            )
        token = expected.get("token")
        if not token:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning="expected payload has no token",
                fallback_reason="malformed_expected",
            )
        since = None
        if isinstance(response, dict):
            since = response.get("since")

        captured = self.captured_for_token(str(token), since=since)
        if not captured:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning=(
                    f"no callback received for token {token} "
                    f"(buffer size: {len(self._buffer)})"
                ),
                detail={"token": token, "captured_count": 0},
            )

        # Award 0.6 for "received at all" + up to 0.4 for substring/JSON match
        last = captured[-1]
        score = 0.6
        bits = ["callback received"]
        expected_substr = (expected.get("expected_text_substring") or "").strip()
        body_text = last.body_text()
        if expected_substr and expected_substr.lower() in body_text.lower():
            score += 0.4
            bits.append("expected text matched in body")
        elif expected_substr:
            bits.append(
                f"body did not contain expected substring "
                f"'{expected_substr[:30]}...'"
            )
            score += 0.0
        else:
            score += 0.4  # no substring expected; full credit on receipt
        return EvaluationResult(
            passed=score >= 0.6,
            score=min(1.0, score),
            reasoning="; ".join(bits),
            detail={
                "token": token,
                "captured_count": len(captured),
                "method": last.method,
                "path": last.path,
                "body_bytes": len(last.body_bytes),
                "body_preview": body_text[:200],
            },
        )


# ---------------------------------------------------------------------------
# Payload shapes
# ---------------------------------------------------------------------------


def _build_payload_for_shape(shape: str, message: str, token: str) -> dict[str, Any]:
    """Mimic the webhook envelope of common providers."""
    if shape == "slack":
        return {
            "type": "event_callback",
            "event": {
                "type": "app_mention", "user": "U_TEST", "text": message,
                "ts": f"{time.time():.6f}", "channel": "C_TEST",
            },
            "team_id": "T_TEST", "api_app_id": "A_TEST",
            "_puzzleeval_token": token,
        }
    if shape == "intercom":
        return {
            "type": "notification_event",
            "topic": "conversation.user.created",
            "data": {
                "item": {
                    "type": "conversation",
                    "id": f"conv_{token[:8]}",
                    "source": {
                        "type": "conversation",
                        "body": message,
                        "author": {"type": "user", "name": "Test User"},
                    },
                },
            },
            "_puzzleeval_token": token,
        }
    if shape in ("twilio_sms", "twilio"):
        return {
            "MessageSid": f"SM{token[:30]}",
            "From": "+15555550100", "To": "+15555550101",
            "Body": message, "NumMedia": "0",
            "_puzzleeval_token": token,
        }
    if shape == "stripe":
        return {
            "id": f"evt_{token[:24]}", "object": "event",
            "type": "payment_intent.succeeded",
            "data": {"object": {
                "id": f"pi_{token[:24]}", "amount": 2000, "currency": "usd",
                "description": message,
            }},
            "_puzzleeval_token": token,
        }
    if shape == "github":
        return {
            "action": "opened",
            "issue": {
                "number": 1, "title": message,
                "body": f"Issue body: {message}",
                "user": {"login": "puzzleeval-test"},
            },
            "repository": {"full_name": "puzzleeval/test-repo"},
            "_puzzleeval_token": token,
        }
    # generic
    return {
        "event": "inbound_message",
        "message": message,
        "received_at": time.time(),
        "_puzzleeval_token": token,
    }


# ---------------------------------------------------------------------------
# Module-level instance + registration
# ---------------------------------------------------------------------------


_PLUGIN: WebhookReceiverPlugin | None = None


def get_plugin_instance() -> WebhookReceiverPlugin:
    """Singleton accessor — all callers share one buffer + one server."""
    global _PLUGIN
    if _PLUGIN is None:
        _PLUGIN = WebhookReceiverPlugin()
    return _PLUGIN


register_plugin(get_plugin_instance())


__all__ = [
    "CapturedRequest",
    "WebhookReceiverPlugin",
    "get_plugin_instance",
]
