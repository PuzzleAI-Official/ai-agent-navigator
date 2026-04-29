"""Outbound delivery verification plugin — mock SMTP/Slack/SMS receivers.

Outbound message senders (email automation, Slack notifier, Twilio SMS,
PagerDuty alerting) succeed when the message ACTUALLY LANDS in the
intended channel — not just when the API returned 200. Today's pipeline
can only check the latter (HTTP success). To test "did the email
actually arrive in the inbox?" we run destination simulators locally:

  - **smtp**: a real SMTP server bound to ``127.0.0.1:PORT`` that
    accepts MAIL/RCPT/DATA and stores everything received in memory.
    Configurable via ``SMTP_HOST=127.0.0.1 SMTP_PORT=2525`` env vars
    that the candidate harness reads.
  - **slack**: an HTTP server that accepts ``POST /webhooks/<token>``
    or ``POST /api/chat.postMessage`` shapes. Records messages by
    channel.
  - **sms**: an HTTP server that accepts Twilio's ``POST
    /2010-04-01/Accounts/<sid>/Messages.json`` form-encoded shape.
    Records by To/From.

Each receiver is a separate background thread. The plugin's
``synthesize_input()`` returns the connection details the candidate
harness should use; ``evaluate_output()`` inspects what landed.

This plugin is the first-class fix for the "Outbound end-to-end
delivery verification" sub-scenario (1-2 days of work in the original
gap analysis — shipped here in one pass).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socketserver
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message as EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

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

SMTP_BIND_HOST = os.environ.get("PUZZLEEVAL_SMTP_BIND", "127.0.0.1")
SMTP_PORT = int(os.environ.get("PUZZLEEVAL_SMTP_PORT", "2525"))
SLACK_PORT = int(os.environ.get("PUZZLEEVAL_SLACK_MOCK_PORT", "8766"))
SMS_PORT = int(os.environ.get("PUZZLEEVAL_SMS_MOCK_PORT", "8767"))
HTTP_BIND_HOST = os.environ.get("PUZZLEEVAL_OUTBOUND_BIND", "127.0.0.1")
MAX_BUFFER = int(os.environ.get("PUZZLEEVAL_OUTBOUND_MAX_BUFFER", "1000"))
# Per-line max for SMTP (RFC 5321 says 1000 octets including CRLF; we go
# generously to 8 KB to accommodate long headers without breaking real mail).
SMTP_MAX_LINE_BYTES = int(os.environ.get("PUZZLEEVAL_SMTP_MAX_LINE_BYTES", "8192"))
# Total DATA cap per message — even a benign attachment-laden email rarely
# exceeds this. 25 MiB matches Gmail's attachment ceiling.
SMTP_MAX_DATA_BYTES = int(os.environ.get("PUZZLEEVAL_SMTP_MAX_DATA_BYTES", str(25 * 1024 * 1024)))
# HTTP body cap (channel + sms receivers). Same protection as webhook receiver.
HTTP_MAX_BODY_BYTES = int(os.environ.get("PUZZLEEVAL_OUTBOUND_HTTP_MAX_BODY", str(1 << 20)))


# ---------------------------------------------------------------------------
# Captured-message data classes
# ---------------------------------------------------------------------------


@dataclass
class CapturedEmail:
    from_addr: str
    to_addrs: list[str]
    subject: str
    body_text: str
    body_html: str | None
    received_at: float
    raw_bytes: bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class CapturedChannelMessage:
    """Slack / Discord / Teams shape — channel-keyed text."""
    channel: str
    text: str
    sender: str
    received_at: float
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapturedSMS:
    to: str
    from_: str
    body: str
    received_at: float
    raw_payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SMTP receiver (pure socket implementation — works in 3.12+ where smtpd
# is removed)
# ---------------------------------------------------------------------------


class _SMTPRequestHandler(socketserver.StreamRequestHandler):
    """Hand-rolled SMTP transaction handler.

    Implements the minimum verbs to satisfy any standard mail client:
    HELO/EHLO, MAIL FROM, RCPT TO, DATA, QUIT, RSET, NOOP. No auth, no
    TLS — we're a capture endpoint, not a relay.

    This intentionally re-implements smtpd because:
      - ``smtpd``/``asyncore`` were removed from stdlib in 3.12
      - Adding ``aiosmtpd`` would mean a third-party dep just for tests
      - SMTP is small enough that 80 lines do the job

    Per RFC 5321: line endings are CRLF. End-of-DATA is ``<CRLF>.<CRLF>``;
    a leading ``.`` on a data line is doubled by the sender and unescaped
    by us (transparency rule).
    """

    timeout = 30  # seconds — kill stuck connections

    def _send(self, line: str) -> None:
        try:
            self.wfile.write((line + "\r\n").encode("utf-8", "replace"))
            self.wfile.flush()
        except OSError:
            pass

    def _read_line(self) -> str | None:
        """Read one line, capped at SMTP_MAX_LINE_BYTES.

        Without the cap a malicious sender could stream a single line
        forever and OOM the server. We pass the cap to ``readline()`` so
        the rfile (BufferedReader) returns at most that many bytes; if
        the line was longer, subsequent reads continue from where we
        stopped — but for our capture-and-discard-on-overflow semantics
        we treat the result as one line and let the caller's DATA size
        cap (SMTP_MAX_DATA_BYTES) close the door if abuse continues.
        """
        try:
            raw = self.rfile.readline(SMTP_MAX_LINE_BYTES)
        except OSError:
            return None
        if not raw:
            return None
        return raw.rstrip(b"\r\n").decode("utf-8", "replace")

    def handle(self) -> None:
        plugin: OutboundDeliveryPlugin = self.server.plugin  # type: ignore[attr-defined]
        self._send("220 puzzleeval.local SMTP capture ready")
        mail_from = ""
        rcpt_tos: list[str] = []
        data_buf: list[bytes] = []
        data_buf_bytes = 0
        in_data = False
        data_overflowed = False

        while True:
            line = self._read_line()
            if line is None:
                break
            if in_data:
                if line == ".":
                    if data_overflowed:
                        # Reject the message rather than silently truncate;
                        # senders see a clear 552 and we don't pollute the
                        # inbox with partial captures.
                        self._send(
                            f"552 message exceeded {SMTP_MAX_DATA_BYTES} byte cap"
                        )
                    else:
                        raw = ("\r\n".join(self._undot_data(data_buf))).encode(
                            "utf-8", "replace",
                        )
                        self._capture(plugin, mail_from, rcpt_tos, raw)
                        self._send("250 OK message received")
                    mail_from, rcpt_tos, data_buf, in_data = "", [], [], False
                    data_buf_bytes = 0
                    data_overflowed = False
                    continue
                # Transparency: leading "." in client data line is escaped.
                processed = line[1:] if line.startswith(".") else line
                # Bound the in-memory buffer to defend against an attacker
                # streaming an unbounded body. Once we hit the cap we stop
                # appending but keep reading lines until the terminator so
                # the connection state machine reaches QUIT cleanly.
                if not data_overflowed:
                    data_buf.append(processed)
                    data_buf_bytes += len(processed) + 2  # +2 for CRLF
                    if data_buf_bytes >= SMTP_MAX_DATA_BYTES:
                        data_overflowed = True
                        data_buf = data_buf[:0]  # release memory
                continue

            cmd, _, arg = line.partition(" ")
            cmd_upper = cmd.upper()
            if cmd_upper in ("HELO", "EHLO"):
                self._send("250 puzzleeval.local")
            elif cmd_upper == "MAIL":
                mail_from = _extract_addr(arg.partition(":")[2])
                self._send("250 OK")
            elif cmd_upper == "RCPT":
                rcpt = _extract_addr(arg.partition(":")[2])
                if rcpt:
                    rcpt_tos.append(rcpt)
                    self._send("250 OK")
                else:
                    self._send("501 syntax error in RCPT")
            elif cmd_upper == "DATA":
                if not mail_from or not rcpt_tos:
                    self._send("503 need MAIL and RCPT first")
                    continue
                self._send("354 end with <CRLF>.<CRLF>")
                in_data = True
                data_buf = []
            elif cmd_upper == "RSET":
                mail_from, rcpt_tos, data_buf, in_data = "", [], [], False
                self._send("250 OK")
            elif cmd_upper == "NOOP":
                self._send("250 OK")
            elif cmd_upper == "QUIT":
                self._send("221 bye")
                break
            else:
                self._send("502 not implemented")

    @staticmethod
    def _undot_data(lines: list[str]) -> list[str]:
        return list(lines)

    @staticmethod
    def _capture(
        plugin: OutboundDeliveryPlugin, mail_from: str,
        rcpt_tos: list[str], data: bytes,
    ) -> None:
        try:
            parsed = message_from_bytes(data) if data else EmailMessage()
            subject = parsed.get("Subject", "")
            text_body, html_body = _extract_email_bodies(parsed)
            headers = {k: v for k, v in parsed.items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("SMTP message parse failed: %s", exc)
            subject, text_body, html_body, headers = "", data.decode("utf-8", "replace"), None, {}
        captured = CapturedEmail(
            from_addr=mail_from, to_addrs=list(rcpt_tos), subject=subject,
            body_text=text_body, body_html=html_body,
            received_at=time.time(), raw_bytes=data, headers=headers,
        )
        plugin._record_email(captured)


def _extract_addr(token: str) -> str:
    """Strip <...> and whitespace from MAIL FROM / RCPT TO arguments."""
    s = token.strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
    return s.strip()


class _ThreadedSMTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """One thread per inbound SMTP connection. Daemonized."""
    allow_reuse_address = True
    daemon_threads = True
    plugin: OutboundDeliveryPlugin


def _extract_email_bodies(msg: EmailMessage) -> tuple[str, str | None]:
    """Return (text body, html body or None) from a parsed email."""
    text, html = "", None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            try:
                decoded = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:  # noqa: BLE001
                decoded = payload.decode("utf-8", "replace")
            if ctype == "text/plain" and not text:
                text = decoded
            elif ctype == "text/html" and not html:
                html = decoded
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
        elif isinstance(payload, str):
            text = payload
    return text, html


# ---------------------------------------------------------------------------
# HTTP receiver (Slack / SMS)
# ---------------------------------------------------------------------------


class _ChannelHTTPHandler(BaseHTTPRequestHandler):
    """Records POST bodies to either a Slack-shaped or SMS-shaped buffer.

    Routing rule:
      - path starts with ``/sms``  → Twilio-shaped form payload
      - everything else            → Slack-shaped JSON payload
    """

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("outbound mock %s %s", self.client_address, fmt % args)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        try:
            return self.rfile.read(min(length, HTTP_MAX_BODY_BYTES))
        except OSError:
            return b""

    def _is_sms_path(self) -> bool:
        path = urlparse(self.path).path.lower()
        return path.startswith("/sms") or "/messages" in path

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        plugin: OutboundDeliveryPlugin = self.server.plugin  # type: ignore[attr-defined]
        if self._is_sms_path():
            self._handle_sms(plugin, body)
        else:
            self._handle_channel(plugin, body)

    def _handle_sms(self, plugin: OutboundDeliveryPlugin, body: bytes) -> None:
        try:
            text = body.decode("utf-8", "replace")
            qs = parse_qs(text, keep_blank_values=True)
            to = qs.get("To", [""])[0]
            from_ = qs.get("From", [""])[0]
            msg_body = qs.get("Body", [""])[0]
        except Exception:  # noqa: BLE001
            to, from_, msg_body = "", "", body.decode("utf-8", "replace")
        captured = CapturedSMS(
            to=to, from_=from_, body=msg_body, received_at=time.time(),
            raw_payload={"raw": body.decode("utf-8", "replace")},
        )
        plugin._record_sms(captured)
        # Return Twilio-ish JSON ack
        ack = json.dumps({
            "sid": f"SM{secrets.token_hex(16)}", "status": "queued",
            "to": to, "from": from_, "body": msg_body,
        }).encode()
        self._respond_json(ack)

    def _handle_channel(self, plugin: OutboundDeliveryPlugin, body: bytes) -> None:
        text = body.decode("utf-8", "replace")
        # Try JSON first, fall back to text
        payload: dict[str, Any] = {}
        msg_text = text
        channel = "general"
        sender = "candidate"
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                payload = parsed
                msg_text = str(payload.get("text") or payload.get("message") or text)
                channel = str(payload.get("channel") or "general")
                sender = str(payload.get("username") or payload.get("sender") or "candidate")
        except (ValueError, TypeError):
            pass
        captured = CapturedChannelMessage(
            channel=channel, text=msg_text, sender=sender,
            received_at=time.time(), raw_payload=payload,
        )
        plugin._record_channel(captured)
        self._respond_json(b'{"ok":true}')

    def _respond_json(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ThreadedHTTPServer(HTTPServer):
    # Same restart-resilience as the SMTP server above + the webhook /
    # voice plugins. Without this, a uvicorn --reload binds the channel /
    # SMS receivers to ephemeral ports on the second start.
    allow_reuse_address = True
    plugin: OutboundDeliveryPlugin


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


@dataclass
class _ServerHandle:
    thread: threading.Thread
    shutdown: Any  # callable
    bind_host: str
    port: int


class OutboundDeliveryPlugin(ToolPlugin):
    """Verifies outbound delivery for email, channel messages, and SMS."""

    name = "outbound_delivery"

    def __init__(self) -> None:
        self._emails: deque[CapturedEmail] = deque(maxlen=MAX_BUFFER)
        self._channels: deque[CapturedChannelMessage] = deque(maxlen=MAX_BUFFER)
        self._sms: deque[CapturedSMS] = deque(maxlen=MAX_BUFFER)
        self._lock = threading.Lock()
        self._smtp_handle: _ServerHandle | None = None
        self._channel_handle: _ServerHandle | None = None
        self._sms_handle: _ServerHandle | None = None
        self._server_lock = threading.Lock()
        self._asyncore_loop_running = False

    # -- Plugin contract ----------------------------------------------------

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            input_types=[],
            output_types=["action", "outbound_message"],
            synthesizes_input=True,
            evaluates_output=True,
            requires_credentials=[],
            notes=(
                "Local mock receivers for email (SMTP), channel messages "
                "(Slack/Discord/Teams shapes), and SMS (Twilio shape). "
                "Candidate harness is configured to send to localhost; "
                "we capture and verify delivery."
            ),
        )

    def is_available(self) -> tuple[bool, str]:
        return True, ""

    # -- Server lifecycle ---------------------------------------------------

    def _ensure_smtp(self) -> _ServerHandle:
        """Lazily start the SMTP capture server."""
        with self._server_lock:
            if self._smtp_handle is not None:
                return self._smtp_handle
            last_err: Exception | None = None
            for try_port in (SMTP_PORT, 0):
                try:
                    server = _ThreadedSMTPServer(
                        (SMTP_BIND_HOST, try_port), _SMTPRequestHandler,
                    )
                    break
                except OSError as exc:
                    last_err = exc
                    continue
            else:
                raise RuntimeError(f"failed to bind SMTP receiver: {last_err}")
            server.plugin = self
            actual_port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever, name="puzzleeval-smtp", daemon=True,
            )
            thread.start()

            def _shutdown(_s=server) -> None:
                try:
                    _s.shutdown()
                    _s.server_close()
                except Exception:  # noqa: BLE001
                    pass

            self._smtp_handle = _ServerHandle(
                thread=thread, shutdown=_shutdown,
                bind_host=SMTP_BIND_HOST, port=actual_port,
            )
            logger.info(
                "SMTP receiver listening on %s:%s", SMTP_BIND_HOST, actual_port,
            )
            return self._smtp_handle

    def _ensure_http(self, channel_kind: str) -> _ServerHandle:
        """Start either the channel or sms HTTP receiver."""
        with self._server_lock:
            if channel_kind == "channel":
                if self._channel_handle is not None:
                    return self._channel_handle
                target_port = SLACK_PORT
            else:
                if self._sms_handle is not None:
                    return self._sms_handle
                target_port = SMS_PORT

            for try_port in (target_port, 0):
                try:
                    server = _ThreadedHTTPServer(
                        (HTTP_BIND_HOST, try_port), _ChannelHTTPHandler,
                    )
                    break
                except OSError as exc:
                    last_err = exc
                    continue
            else:
                raise RuntimeError(
                    f"failed to bind outbound {channel_kind} receiver: {last_err}"
                )
            server.plugin = self
            actual_port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"puzzleeval-outbound-{channel_kind}", daemon=True,
            )
            thread.start()

            def _shutdown(_s=server) -> None:
                try:
                    _s.shutdown()
                    _s.server_close()
                except Exception:  # noqa: BLE001
                    pass

            handle = _ServerHandle(
                thread=thread, shutdown=_shutdown,
                bind_host=HTTP_BIND_HOST, port=actual_port,
            )
            if channel_kind == "channel":
                self._channel_handle = handle
            else:
                self._sms_handle = handle
            logger.info(
                "outbound %s receiver listening on %s:%s",
                channel_kind, HTTP_BIND_HOST, actual_port,
            )
            return handle

    def shutdown(self) -> None:
        """Stop all receivers cleanly."""
        with self._server_lock:
            for handle_attr in ("_smtp_handle", "_channel_handle", "_sms_handle"):
                handle: _ServerHandle | None = getattr(self, handle_attr)
                if handle is None:
                    continue
                try:
                    handle.shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("shutdown error %s: %s", handle_attr, exc)
                setattr(self, handle_attr, None)

    # -- Recording ----------------------------------------------------------

    def _record_email(self, c: CapturedEmail) -> None:
        with self._lock:
            self._emails.append(c)

    def _record_channel(self, c: CapturedChannelMessage) -> None:
        with self._lock:
            self._channels.append(c)

    def _record_sms(self, c: CapturedSMS) -> None:
        with self._lock:
            self._sms.append(c)

    # -- Buffer access ------------------------------------------------------

    def emails_received(
        self, *, to: str | None = None, since: float | None = None,
    ) -> list[CapturedEmail]:
        with self._lock:
            items = list(self._emails)
        out = []
        for c in items:
            if since is not None and c.received_at < since:
                continue
            if to is not None and to not in c.to_addrs:
                continue
            out.append(c)
        return out

    def channel_messages(
        self, *, channel: str | None = None, since: float | None = None,
    ) -> list[CapturedChannelMessage]:
        with self._lock:
            items = list(self._channels)
        out = []
        for c in items:
            if since is not None and c.received_at < since:
                continue
            if channel is not None and c.channel != channel:
                continue
            out.append(c)
        return out

    def sms_messages(
        self, *, to: str | None = None, since: float | None = None,
    ) -> list[CapturedSMS]:
        with self._lock:
            items = list(self._sms)
        out = []
        for c in items:
            if since is not None and c.received_at < since:
                continue
            if to is not None and c.to != to:
                continue
            out.append(c)
        return out

    def clear(self) -> None:
        with self._lock:
            self._emails.clear()
            self._channels.clear()
            self._sms.clear()

    # -- Synthesis ----------------------------------------------------------

    def synthesize_input(
        self, *, scope_role: str, ground_truth_hint: str | None = None,
        **kwargs: Any,
    ) -> SynthesisResult:
        """Stand up a destination and return how the candidate should reach it.

        ``kwargs.channel`` selects: "email" | "slack" | "sms" (default
        derived from scope_role).
        ``kwargs.recipient`` overrides the destination address.
        """
        channel = (kwargs.get("channel") or _infer_channel(scope_role)).lower()
        token = secrets.token_hex(8)
        message = (
            ground_truth_hint
            or kwargs.get("message")
            or f"Test outbound delivery — token {token}"
        )
        instructions: dict[str, Any] = {"token": token, "channel": channel}
        ground_truth: dict[str, Any] = {
            "token": token, "channel": channel,
            "expected_text_substring": message[:80],
        }

        if channel == "email":
            handle = self._ensure_smtp()
            recipient = kwargs.get("recipient") or f"test-{token}@puzzleeval.local"
            sender = kwargs.get("sender") or "agent@puzzleeval.local"
            instructions.update({
                "smtp_host": handle.bind_host, "smtp_port": handle.port,
                "smtp_user": "", "smtp_password": "", "smtp_use_tls": False,
                "from_addr": sender, "to_addr": recipient,
                "subject": f"PuzzleEval test {token}", "body_text": message,
                "instructions": (
                    f"Send an email via SMTP to {recipient} from {sender}. "
                    f"Use host {handle.bind_host}:{handle.port} (no auth, no TLS)."
                ),
            })
            ground_truth["expected_recipient"] = recipient
        elif channel == "sms":
            handle = self._ensure_http("sms")
            number = kwargs.get("recipient") or "+15555550199"
            instructions.update({
                "sms_endpoint": (
                    f"http://{handle.bind_host}:{handle.port}"
                    f"/sms/2010-04-01/Accounts/AC_TEST/Messages.json"
                ),
                "to": number, "from": "+15555550100", "body": message,
                "auth": "form-encoded; mock accepts no credentials",
                "instructions": (
                    f"POST a Twilio-style form-encoded message to the "
                    f"sms_endpoint with To, From, Body. Mock returns 200 + JSON."
                ),
            })
            ground_truth["expected_recipient"] = number
        else:  # slack / channel default
            handle = self._ensure_http("channel")
            channel_name = kwargs.get("recipient") or "#test-channel"
            instructions.update({
                "webhook_url": (
                    f"http://{handle.bind_host}:{handle.port}/webhooks/{token}"
                ),
                "channel": channel_name, "text": message,
                "instructions": (
                    f"POST JSON {{channel,text,username}} to webhook_url. "
                    f"Channel: {channel_name}. Mock returns 200 + JSON."
                ),
            })
            ground_truth["expected_channel"] = channel_name
            ground_truth["channel"] = "slack"  # disambiguate

        return SynthesisResult(
            file_path=None,
            inline_data=instructions,
            ground_truth=ground_truth,
            notes=f"outbound {channel} mock receiver ready",
        )

    # -- Evaluation ---------------------------------------------------------

    def evaluate_output(
        self, *, response: Any, expected: Any, criteria: list[dict] | None = None,
        **kwargs: Any,
    ) -> EvaluationResult:
        """Score by inspecting which messages landed in the receiver."""
        if not isinstance(expected, dict):
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning="expected payload missing",
                fallback_reason="malformed_expected",
            )
        channel = (expected.get("channel") or "").lower()
        substr = (expected.get("expected_text_substring") or "").strip().lower()
        since = None
        if isinstance(response, dict):
            since = response.get("since")

        if channel == "email":
            recipient = expected.get("expected_recipient")
            received = self.emails_received(to=recipient, since=since)
            return self._score_received(
                received, substr,
                preview=lambda c: f"subject={c.subject!r} body={c.body_text[:80]!r}",
                kind="email",
            )
        if channel == "sms":
            recipient = expected.get("expected_recipient")
            received = self.sms_messages(to=recipient, since=since)
            return self._score_received(
                received, substr,
                preview=lambda c: f"to={c.to!r} body={c.body[:80]!r}",
                kind="sms",
                body_attr="body",
            )
        # default = slack/channel
        target = expected.get("expected_channel")
        received = self.channel_messages(channel=target, since=since)
        return self._score_received(
            received, substr,
            preview=lambda c: f"channel={c.channel!r} text={c.text[:80]!r}",
            kind="channel",
            body_attr="text",
        )

    @staticmethod
    def _score_received(
        received: list[Any], substr: str,
        *, preview: Any, kind: str, body_attr: str = "body_text",
    ) -> EvaluationResult:
        if not received:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning=f"no {kind} messages captured",
                detail={"kind": kind, "captured_count": 0},
            )
        last = received[-1]
        body = (getattr(last, body_attr, "") or "").lower()
        score = 0.6
        if substr and substr in body:
            score += 0.4
            reasoning = f"{kind} delivered AND text matched"
        elif substr:
            reasoning = f"{kind} delivered but expected substring missing"
            score += 0.0
        else:
            reasoning = f"{kind} delivered (no substring expected)"
            score += 0.4
        return EvaluationResult(
            passed=score >= 0.6, score=min(1.0, score),
            reasoning=reasoning,
            detail={
                "kind": kind, "captured_count": len(received),
                "preview": preview(last),
            },
        )


def _infer_channel(scope_role: str) -> str:
    role = (scope_role or "").lower()
    if any(k in role for k in ("email", "smtp", "mail")):
        return "email"
    if any(k in role for k in ("sms", "text_message", "twilio")):
        return "sms"
    return "slack"


# ---------------------------------------------------------------------------
# Module-level instance + registration
# ---------------------------------------------------------------------------


_PLUGIN: OutboundDeliveryPlugin | None = None


def get_plugin_instance() -> OutboundDeliveryPlugin:
    global _PLUGIN
    if _PLUGIN is None:
        _PLUGIN = OutboundDeliveryPlugin()
    return _PLUGIN


register_plugin(get_plugin_instance())


__all__ = [
    "CapturedChannelMessage",
    "CapturedEmail",
    "CapturedSMS",
    "OutboundDeliveryPlugin",
    "get_plugin_instance",
]
