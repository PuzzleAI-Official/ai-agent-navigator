"""Voice real-time plugin â€” local audio loopback for voice/phone agents.

Real-world voice agents speak the WebRTC / SIP / Twilio Voice / Vonage
Voice protocols. End-to-end testing of those flows needs a publicly-
reachable phone number, a SIP endpoint, or a TURN server â€” those belong
in the cloud-deferred bucket. **What we CAN do locally** is the next
best thing: an audio-loopback harness that mimics a one-turn voice
exchange entirely on 127.0.0.1.

How it works:
  1. **Synthesize** an audio file (via TTS plugin) carrying the
     "caller's" utterance.
  2. **Expose** an HTTP endpoint that serves the audio when the
     candidate harness fetches the recording URL (mirrors how Twilio /
     Vonage deliver recordings).
  3. **Receive** the candidate's TwiML / NCCO / generic JSON response
     OR an audio-blob upload of what the agent says back.
  4. **Evaluate** the response: if it's structured TwiML/NCCO, parse
     and check for ``<Say>``/``<Play>`` actions plus expected text; if
     it's an audio blob, call the transcription plugin to STT it and
     score against the expected response.

Limitations called out clearly:
  - This is a **simulated** turn, not a real bidirectional call. No
    DTMF, no interruptions, no codec negotiation. Use it to test
    intent + response shape; use the cloud telephony adapter (separate
    work) for full WebRTC/SIP fidelity.
  - The transcription dependency means evaluating audio responses needs
    OPENAI_API_KEY / DEEPGRAM_API_KEY / ASSEMBLYAI_API_KEY (any one).
    Without those we still capture the audio blob and report ``unscored``;
    the LLM judge can take over with a fallback advisory.

Public URL handling: same pattern as ``webhook_receiver`` â€” when
``PUZZLEEVAL_TUNNEL_URL`` is set, the served URLs use the public host so
candidates running offsite can fetch recordings. Default is 127.0.0.1.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, urlparse

from puzzleeval.tool_plugins import (
    EvaluationResult,
    HARNESS_EXECUTION_PERSISTENT_WORKER,
    PluginCapabilities,
    SynthesisResult,
    ToolPlugin,
    register_plugin,
)

if TYPE_CHECKING:
    from puzzleeval.schemas import Persona, RubricCriterion

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_VOICE_PORT_ENV = os.environ.get("PUZZLEEVAL_VOICE_PORT")
VOICE_PORT = int(_VOICE_PORT_ENV) if _VOICE_PORT_ENV else 0
VOICE_PORT_EXPLICIT = _VOICE_PORT_ENV is not None
VOICE_BIND = os.environ.get("PUZZLEEVAL_VOICE_BIND", "127.0.0.1")
TUNNEL_URL = os.environ.get("PUZZLEEVAL_TUNNEL_URL", "").rstrip("/")
MAX_AUDIO_BYTES = int(os.environ.get("PUZZLEEVAL_VOICE_MAX_AUDIO", str(25 * 1024 * 1024)))  # 25 MiB
MAX_BUFFER = int(os.environ.get("PUZZLEEVAL_VOICE_MAX_BUFFER", "200"))


# ---------------------------------------------------------------------------
# Captured turn data class
# ---------------------------------------------------------------------------


@dataclass
class CapturedVoiceTurn:
    """One inbound response to a voice prompt.

    Either ``twiml_text`` is populated (response was XML), ``ncco_json``
    is populated (Vonage NCCO array), ``response_json`` is populated
    (generic JSON), or ``audio_path`` is populated (binary audio blob
    saved to disk).
    """
    token: str
    received_at: float
    twiml_text: str | None = None
    ncco_json: list[Any] | None = None
    response_json: Any = None
    audio_path: str | None = None
    audio_bytes_len: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    raw_body_preview: str = ""


# ---------------------------------------------------------------------------
# HTTP handler â€” serves audio + receives responses
# ---------------------------------------------------------------------------


class _VoiceHandler(BaseHTTPRequestHandler):
    """Routes:
      - GET  /audio/<token>            â†’ serve synthesized caller audio
      - POST /voice/<token>            â†’ receive candidate's response
      - POST /voice/<token>/recording  â†’ receive audio blob from candidate
    """

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("voice mock %s %s", self.client_address, fmt % args)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        try:
            return self.rfile.read(min(length, MAX_AUDIO_BYTES))
        except OSError:
            return b""

    def _path_token(self) -> tuple[str, str]:
        """Return (kind, token) where kind is 'audio'|'voice'|'recording'|''."""
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) >= 2 and parts[0] == "audio":
            return "audio", parts[1]
        if len(parts) >= 2 and parts[0] == "voice":
            kind = "recording" if (len(parts) >= 3 and parts[2] == "recording") else "voice"
            return kind, parts[1]
        return "", ""

    def do_GET(self) -> None:  # noqa: N802
        plugin: VoiceRealtimePlugin = self.server.plugin  # type: ignore[attr-defined]
        kind, token = self._path_token()
        if kind == "audio":
            audio_path = plugin._get_audio_for_token(token)
            if not audio_path or not os.path.exists(audio_path):
                self._respond(404, "audio not found")
                return
            try:
                with open(audio_path, "rb") as f:
                    data = f.read()
            except OSError as exc:
                logger.warning("audio read failed: %s", exc)
                self._respond(500, "read error")
                return
            self.send_response(200)
            ctype = _guess_audio_content_type(audio_path)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._respond(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        plugin: VoiceRealtimePlugin = self.server.plugin  # type: ignore[attr-defined]
        kind, token = self._path_token()
        if not token:
            self._respond(400, "missing token")
            return
        body = self._read_body()
        ctype = (self.headers.get("Content-Type") or "").lower()
        captured = CapturedVoiceTurn(
            token=token, received_at=time.time(),
            headers={k: v for k, v in self.headers.items()},
            raw_body_preview=body[:512].decode("utf-8", "replace"),
            audio_bytes_len=len(body),
        )
        if kind == "recording" or "audio/" in ctype:
            # Save audio blob to a tempfile under the plugin's session dir
            audio_path = plugin._save_audio_blob(token, body, ctype)
            captured.audio_path = audio_path
        elif "xml" in ctype or body.lstrip().startswith(b"<"):
            captured.twiml_text = body.decode("utf-8", "replace")
        else:
            # Try JSON, then form-encoded
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
                # Vonage NCCO is a top-level list
                if isinstance(parsed, list):
                    captured.ncco_json = parsed
                else:
                    captured.response_json = parsed
            except (ValueError, TypeError):
                try:
                    captured.response_json = parse_qs(
                        body.decode("utf-8", "replace"), keep_blank_values=True,
                    )
                except Exception:  # noqa: BLE001
                    captured.response_json = None
        plugin._record(captured)
        # Echo simple TwiML 200 so candidates expecting voice protocol ack are happy
        self._respond(200, '<?xml version="1.0" encoding="UTF-8"?>\n<Response/>',
                      content_type="application/xml")

    def _respond(
        self, code: int, body: str, content_type: str = "text/plain",
    ) -> None:
        body_bytes = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


def _guess_audio_content_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
        ".m4a": "audio/mp4", ".webm": "audio/webm", ".flac": "audio/flac",
    }.get(ext, "application/octet-stream")


class _ThreadedHTTPServer(HTTPServer):
    # Do not share a fixed loopback port by default. On Windows,
    # SO_REUSEADDR can allow two local servers to bind the same port,
    # which lets one run's voice loopback answer another run's tokens.
    allow_reuse_address = False
    plugin: VoiceRealtimePlugin


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


@dataclass
class _ServerHandle:
    thread: threading.Thread
    shutdown: Any
    bind_host: str
    port: int


# ---------------------------------------------------------------------------
# Background audio-merge executor
# ---------------------------------------------------------------------------
# Module-level daemon thread pool â€” lazy-initialized on first submit so
# tests / CLI probes that don't trigger merges pay zero cost. Daemon=True
# so the process can exit cleanly without waiting on hung pydub.
#
# Pool size = 8: covers parallelism=6 + 2 slack for overflow.
# Per-merge timeout (env-overridable) prevents one hung merge from freezing
# the end-of-run report assembly.
from concurrent.futures import Future, ThreadPoolExecutor

_MERGE_EXECUTOR: "ThreadPoolExecutor | None" = None
_MERGE_EXECUTOR_LOCK = threading.Lock()
_MERGE_POOL_SIZE = int(os.environ.get("PUZZLEEVAL_VOICE_MERGE_POOL_SIZE", "8"))
# Per-merge timeout for the end-of-run wait. pydub decode + concat +
# re-encode of 17 MP3s typically takes 50-72s; 60s is a tight but safe
# ceiling. If a merge is still running after 60s we abandon it (the
# per-turn audio files are still on disk so the UI degrades gracefully
# to playing them individually).
_MERGE_PER_FUTURE_TIMEOUT_S = float(
    os.environ.get("PUZZLEEVAL_VOICE_MERGE_TIMEOUT_S", "60")
)


def _get_merge_executor() -> "ThreadPoolExecutor":
    """Lazy-init the module-level merge daemon thread pool."""
    global _MERGE_EXECUTOR
    if _MERGE_EXECUTOR is not None:
        return _MERGE_EXECUTOR
    with _MERGE_EXECUTOR_LOCK:
        if _MERGE_EXECUTOR is None:
            _MERGE_EXECUTOR = ThreadPoolExecutor(
                max_workers=max(1, _MERGE_POOL_SIZE),
                thread_name_prefix="voice-merge",
            )
    return _MERGE_EXECUTOR


def _reset_merge_executor_for_tests() -> None:
    """Test-only hook to tear down the module-level merge executor.

    Pytest fixtures that exercise the merge path call this between tests
    so a leaked future from one test can't poison another. Production
    code never calls this â€” the executor lives for the process lifetime
    and exits cleanly on interpreter shutdown via daemon threads.
    """
    global _MERGE_EXECUTOR
    with _MERGE_EXECUTOR_LOCK:
        if _MERGE_EXECUTOR is not None:
            _MERGE_EXECUTOR.shutdown(wait=False, cancel_futures=True)
            _MERGE_EXECUTOR = None


class VoiceRealtimePlugin(ToolPlugin):
    """Local-loopback voice agent harness."""

    name = "voice_realtime"

    def __init__(self) -> None:
        self._buffer: deque[CapturedVoiceTurn] = deque(maxlen=MAX_BUFFER)
        self._lock = threading.Lock()
        self._token_to_audio: dict[str, str] = {}
        self._server_handle: _ServerHandle | None = None
        self._server_lock = threading.Lock()
        # Default session dir is %TEMP% (safe fallback when the plugin
        # is imported in isolation â€” tests, CLI probes, etc.). Agent 5
        # overrides via ``set_session_dir()`` before executing tests so
        # audio artifacts land in ``runs/<trace_id>/harnesses/<slug>/voice/``
        # alongside harness.py / conversation_log.json / fetched_docs_*.
        # The cloud-scale seam: swap the Path for a StorageBackend protocol
        # (S3/GCS/Azure-Blob) without touching any call sites â€” every read
        # + write goes through the resolved session_dir.
        self._default_session_dir = (
            Path(tempfile.gettempdir()) / "puzzleeval_voice"
        )
        # Per-thread session dir override. The plugin is a SINGLETON
        # registered globally, but Agent 5 evaluates candidates in
        # parallel (one ThreadPoolExecutor worker per candidate), and
        # each worker calls set_session_dir() with its own per-candidate
        # path. Without thread-local isolation the LAST set_session_dir
        # call wins for both workers â†’ both candidates' audio lands in
        # one folder. Real-run signal (voice_dual_6 + voice_dual_7):
        # OpenAI's audio paths showed `elevenlabs_voice_stack/voice/`
        # because ElevenLabs's worker happened to set_session_dir
        # second. Thread-local fixes this without changing call sites.
        self._tl = threading.local()
        # Track which audio paths belong to which token so the evaluator
        # can surface them in the TestCaseResult. Keyed by token; each
        # entry is a list of (role, path) pairs â€” role = "caller" | "agent"
        # so the UI can render them in order.
        self._token_to_artifacts: dict[str, list[tuple[str, str]]] = {}
        # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Background audio-merge infrastructure.
        # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Audio merge (decode N MP3s + sample-rate normalize + concat +
        # re-encode via pydub) takes 50-72s per test in real runs. With
        # parallelism=6 default and â‰¥7 tests, batch 1's per-worker sync
        # merge gates batch 2's start by ~70s â€” workers don't free until
        # `conv + merge` per test. Submitting the merge to a daemon pool
        # frees workers at `conv` only, letting batch 2's conversations
        # start ~70s sooner.
        #
        # Daemon thread pool (`_MERGE_EXECUTOR`) is module-level and
        # lazy-initialized on first submit. Daemon=True so the process
        # can exit cleanly without waiting on hung pydub. Pool size = 8
        # (covers parallelism=6 + 2 slack for overflow).
        #
        # `_pending_merges` tracks (session_token, future) so
        # `wait_for_pending_merges()` can join everything at end-of-run.
        # End-of-run wait has a per-merge timeout (default 30s) so a
        # single hung merge doesn't freeze the report assembly.
        self._pending_merges: dict[str, "Future[str | None]"] = {}
        self._pending_merges_lock = threading.Lock()
        try:
            self._default_session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("voice session dir create failed: %s", exc)

    @property
    def _session_dir(self) -> Path:
        """Per-thread session dir if set, else the global default."""
        return getattr(self._tl, "session_dir", None) or self._default_session_dir

    @_session_dir.setter
    def _session_dir(self, value: Path) -> None:
        # Back-compat: legacy code that assigned ``self._session_dir = p``
        # still works AND now sets per-thread state automatically.
        self._tl.session_dir = value

    def set_session_dir(self, session_dir: Path | str) -> None:
        """Redirect audio persistence to a caller-supplied directory.

        Agent 5 calls this before running tests so every caller + agent
        audio file lands in ``runs/<trace_id>/harnesses/<slug>/voice/``.
        The directory is created if missing.

        Per-thread state: each parallel candidate evaluator sets its own
        dir; reads from `self._session_dir` resolve against the calling
        thread's value, falling back to the global default for threads
        that never called set_session_dir (single-process tests, CLI
        probes). This keeps two candidates' audio in DIFFERENT folders
        when Agent 5 runs them in parallel.

        Cloud-scale seam: the argument is typed as Path but any
        Path-compatible object that implements mkdir + the / operator
        works. To back onto S3/GCS, wrap the storage client in a
        PathLike shim and pass it here.
        """
        p = Path(session_dir)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("voice session dir override failed: %s", exc)
            return
        self._tl.session_dir = p

    def artifacts_for_token(self, token: str) -> list[dict[str, str]]:
        """Return all audio artifacts saved for a token, in order.

        Each entry: ``{"role": "caller" | "agent", "path": "..."}``.
        Used by Agent 5 to populate ``TestCaseResult.audio_paths`` so
        the evaluation report can surface playable links.
        """
        with self._lock:
            items = list(self._token_to_artifacts.get(token, []))
        return [{"role": role, "path": path} for role, path in items]

    def _record_artifact(self, token: str, role: str, path: str) -> None:
        """Track an audio file for later surfacing in the report."""
        with self._lock:
            self._token_to_artifacts.setdefault(token, []).append((role, path))

    # -- Plugin contract ----------------------------------------------------

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            # NOTE: deliberately omit `audio_content` from input_types â€” that
            # modality is owned by the TTS plugin's synthesis path. We declare
            # only voice_turn (single turn) + voice_conversation (multi-turn
            # script) so we don't shadow TTS for ordinary voice-input tests.
            input_types=["voice_turn", "voice_conversation"],
            output_types=["voice_turn", "voice_conversation", "audio_content"],
            synthesizes_input=True,
            evaluates_output=True,
            requires_credentials=[],
            # Multi-turn (voice_conversation) drives the harness per turn
            # via harness_runner. Single-turn (voice_turn) doesn't need it
            # but the capability flag is plugin-level, not per-test. Agent 5
            # passes a runner whenever it's available â€” the plugin ignores
            # it for single-turn tests. Same pattern as conversation_simulator.
            requires_harness_runner=True,
            harness_execution_mode=HARNESS_EXECUTION_PERSISTENT_WORKER,
            # Voice harnesses provision a billable provider session per
            # call (ElevenLabs ConvAI agent, OpenAI Realtime session,
            # Twilio call, etc.). The adversarial verifier reads this
            # flag to skip stateless probes that would create N billable
            # sessions in seconds and hit provider rate limits.
            provisions_remote_session_per_call=True,
            notes=(
                "Local audio-loopback for voice/phone agents. Single-turn: "
                "serves a caller utterance, captures one response. Multi-turn: "
                "drives a scripted N-turn conversation, scoring each turn "
                "independently. STT-based evaluation requires the transcription "
                "plugin (OPENAI_API_KEY, DEEPGRAM_API_KEY, or ASSEMBLYAI_API_KEY)."
            ),
        )

    def is_available(self) -> tuple[bool, str]:
        return True, ""

    # -- Server lifecycle ---------------------------------------------------

    def _ensure_server(self) -> _ServerHandle:
        with self._server_lock:
            if self._server_handle is not None:
                return self._server_handle
            last_err: Exception | None = None
            candidate_ports = (VOICE_PORT,) if VOICE_PORT_EXPLICIT else (0,)
            for try_port in candidate_ports:
                try:
                    server = _ThreadedHTTPServer((VOICE_BIND, try_port), _VoiceHandler)
                    break
                except OSError as exc:
                    last_err = exc
                    continue
            else:
                raise RuntimeError(f"failed to bind voice receiver: {last_err}")
            server.plugin = self
            actual_port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever, name="puzzleeval-voice", daemon=True,
            )
            thread.start()

            def _shutdown(_s=server) -> None:
                try:
                    _s.shutdown()
                    _s.server_close()
                except Exception:  # noqa: BLE001
                    pass

            self._server_handle = _ServerHandle(
                thread=thread, shutdown=_shutdown,
                bind_host=VOICE_BIND, port=actual_port,
            )
            logger.info("voice receiver listening on %s:%s", VOICE_BIND, actual_port)
            return self._server_handle

    def shutdown(self) -> None:
        with self._server_lock:
            if self._server_handle is None:
                return
            try:
                self._server_handle.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning("voice shutdown error: %s", exc)
            self._server_handle = None

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Background audio merge
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def submit_merge_in_background(
        self,
        *,
        session_token: str,
        turns: list[dict[str, Any]],
    ) -> "Future[str | None]":
        """Submit a `_merge_conversation_audio` job to the daemon pool.

        Returns the Future immediately so the test worker thread can free
        up while the merge runs concurrently. Workers no longer block on
        the 50-72s pydub decode + concat + re-encode â€” they return after
        the conversation completes.

        The returned future is also tracked in `self._pending_merges`
        keyed by session_token so `wait_for_pending_merges()` can join
        everything at end-of-run before the report is assembled.

        Captures `self._tl.session_dir` AT SUBMIT TIME (not at execute
        time) so the background thread sees the SUBMITTING worker's
        per-thread session_dir, not the bg thread's (which would fall
        back to the global default and write merges to %TEMP%).
        """
        with self._pending_merges_lock:
            existing = self._pending_merges.get(session_token)
            if existing is not None:
                return existing

        # Capture submitting worker's per-thread session_dir; restore
        # inside the background thread before _merge_conversation_audio
        # reads `self._session_dir`.
        captured_session_dir = self._session_dir

        def _do_merge() -> "str | None":
            # Restore the caller's per-thread session_dir on this bg thread.
            try:
                self._tl.session_dir = captured_session_dir
            except Exception:  # noqa: BLE001 â€” never crash the bg task
                pass
            try:
                return self._merge_conversation_audio(
                    session_token=session_token,
                    turns=turns,
                )
            except Exception as exc:  # noqa: BLE001 â€” bg task never raises
                logger.info(
                    "background conversation merge failed (non-fatal): %s",
                    exc,
                    extra={
                        "operation": "voice_merge_failed_bg",
                        "session_token": session_token,
                    },
                )
                return None

        future = _get_merge_executor().submit(_do_merge)
        with self._pending_merges_lock:
            self._pending_merges[session_token] = future
        return future

    def wait_for_pending_merges(
        self,
        timeout_per_merge_s: float | None = None,
    ) -> dict[str, "str | None"]:
        """Block until every pending background merge resolves (or times out).

        Called by Agent 5's report-assembly path after `_execute_all_tests`
        returns. Each future is given up to `timeout_per_merge_s` seconds
        (default `_MERGE_PER_FUTURE_TIMEOUT_S` = 60s); a merge that
        exceeds the timeout is abandoned (the per-turn audio files are
        still on disk, so the UI degrades gracefully to playing them
        individually).

        Returns a dict mapping session_token â†’ merged_audio_path (or None
        when the merge failed / timed out / returned no path). Callers
        patch the merged_path back into the corresponding TestCaseResult
        before persisting the run.

        Clears `self._pending_merges` after joining so subsequent runs
        of the plugin (rare in production, common in tests) start with
        a clean slate.
        """
        timeout = (
            timeout_per_merge_s
            if timeout_per_merge_s is not None
            else _MERGE_PER_FUTURE_TIMEOUT_S
        )
        with self._pending_merges_lock:
            pending = dict(self._pending_merges)
            self._pending_merges.clear()
        if not pending:
            return {}

        results: dict[str, str | None] = {}
        for token, future in pending.items():
            try:
                results[token] = future.result(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 â€” TimeoutError + any
                # Merge timed out OR raised. Either way the per-turn
                # files are on disk; the UI can still play them. Log and
                # record None so the caller knows there's no merged path.
                logger.warning(
                    "background merge for %s did not complete within %ss "
                    "(%s) â€” leaving per-turn audio in place",
                    token, timeout, type(exc).__name__,
                    extra={
                        "operation": "voice_merge_timeout_bg",
                        "session_token": token,
                        "error_type": type(exc).__name__,
                    },
                )
                results[token] = None
                # Don't try to cancel â€” pydub may be holding C-level
                # subprocess handles. Leave the future to complete or
                # die on its own; daemon thread keeps the process exit
                # clean.
        return results

    # -- Buffer access ------------------------------------------------------

    def _record(self, turn: CapturedVoiceTurn) -> None:
        with self._lock:
            self._buffer.append(turn)

    def turns_for_token(
        self, token: str, since: float | None = None,
    ) -> list[CapturedVoiceTurn]:
        with self._lock:
            items = list(self._buffer)
        out = []
        for c in items:
            if c.token != token:
                continue
            if since is not None and c.received_at < since:
                continue
            out.append(c)
        return out

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._token_to_audio.clear()
            self._token_to_artifacts.clear()

    # -- Audio mgmt --------------------------------------------------------

    def _get_audio_for_token(self, token: str) -> str | None:
        with self._lock:
            return self._token_to_audio.get(token)

    def _save_audio_blob(self, token: str, body: bytes, ctype: str) -> str:
        ext = ".bin"
        for type_substr, e in [
            ("wav", ".wav"), ("mpeg", ".mp3"), ("mp4", ".m4a"),
            ("ogg", ".ogg"), ("webm", ".webm"), ("flac", ".flac"),
        ]:
            if type_substr in ctype:
                ext = e
                break
        out_path = self._session_dir / f"response_{token}_{secrets.token_hex(4)}{ext}"
        try:
            with open(out_path, "wb") as f:
                f.write(body)
        except OSError as exc:
            logger.warning("audio blob save failed: %s", exc)
        # Track the agent's response audio so the report can link it.
        self._record_artifact(token, "agent", str(out_path))
        return str(out_path)

    def public_url_for_audio(self, token: str) -> str:
        handle = self._ensure_server()
        if TUNNEL_URL:
            return f"{TUNNEL_URL}/audio/{token}"
        return f"http://{handle.bind_host}:{handle.port}/audio/{token}"

    def public_url_for_callback(self, token: str) -> str:
        handle = self._ensure_server()
        if TUNNEL_URL:
            return f"{TUNNEL_URL}/voice/{token}"
        return f"http://{handle.bind_host}:{handle.port}/voice/{token}"

    # -- Multi-turn conversation driver ------------------------------------

    def drive_conversation(
        self,
        *,
        script: list[dict[str, Any]] | None = None,
        agent_responder,
        scope_role: str = "voice_agent",
        shape: str = "generic",
        # Agentic-mode kwargs (optional â€” preserves back-compat).
        # When populated AND evaluation_mode resolves to 'agentic',
        # drive_conversation uses user_simulator + rubric_judge instead of
        # the script iterator. See puzzleeval.user_simulator /
        # puzzleeval.rubric_judge for the per-module contracts.
        persona: "Persona | None" = None,
        goal: str | None = None,
        constraints: list[str] | None = None,
        rubric: "list[RubricCriterion] | None" = None,
        max_turns: int | None = None,
        evaluation_mode: str = "auto",
        anthropic_client=None,
        trace_id: str = "no-trace",
        agent_system_prompt: str | None = None,
        semantic_review_required: bool = False,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        release_harness_session: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Drive a multi-turn voice conversation (scripted OR agentic).

        This is the core multi-turn primitive. The PLUGIN owns the turn
        state machine â€” the caller (Agent 5's harness template OR a
        direct Python driver) only needs to supply a callable that takes
        one caller audio URL + session context and returns the agent's
        response (audio bytes, TwiML, NCCO, or JSON). The plugin handles
        TTS, serving, capturing, STT, per-turn scoring, artifact tracking.

        Two modes:
            **Scripted**: iterate `script` list, record deterministic evidence signals, and apply semantic review when the production evaluator requires it. Fires when no persona+goal+rubric is provided OR evaluation_mode='scripted'.

            **Agentic** (new): replace the script iterator with a
            user_simulator that generates each user turn reactively based
            on the agent's previous response; after the loop, rubric_judge
            scores the full transcript. Fires when persona+goal+rubric are
            provided AND evaluation_mode in {'agentic', 'auto'}.

        Mode resolution (per-call):
          1. Global override via PUZZLEEVAL_CONVERSATION_EVAL_MODE takes
             precedence if set to a non-'auto' value
          2. Otherwise the caller's `evaluation_mode` arg is consulted
          3. 'auto' + persona/goal/rubric all present â†’ agentic
          4. 'auto' + any of persona/goal/rubric missing â†’ scripted
          5. Explicit 'agentic' without all three â†’ falls back to scripted
             with a warning log (never crashes)

        Cloud-scale seam: replace the in-process HTTP server with a
        service (same routes) and swap local tempfile for S3 via
        ``set_session_dir()``. The contract for ``agent_responder`` does
        not change â€” cloud harnesses and local harnesses call this the
        same way.

        Args:
            script: ordered list of turns for scripted mode, each:
                ``{"user_text": "hi I need to reschedule",
                   "expected_agent_contains": "which appointment",
                   "expected_agent_text": "optional"}``
                Ignored in agentic mode.
            agent_responder: callable ``(turn_index, user_audio_url,
                session_state) -> AgentResponse``. The responder makes
                ONE call to the candidate's API, passing the caller audio.
                ``session_state`` is a mutable dict the responder can use
                to carry provider-specific state (conversation_id,
                session_token, cookies) across turns. Return must be a
                dict with at least one of: ``audio_bytes``,
                ``audio_content_type``, ``text``, ``twiml``, ``json``.
            scope_role: passed through to TTS for caller voice selection.
            shape: 'twilio' | 'vonage' | 'generic' â€” affects callback
                instructions, mirrors single-turn synthesize_input.
            persona: Persona for agentic mode â€” see user_simulator.
            goal: what the simulated user wants.
            constraints: simulator-side behavior rules.
            rubric: RubricCriterion list for post-conversation judging.
            max_turns: hard cap on turn count in agentic mode.
            evaluation_mode: 'auto' | 'agentic' | 'scripted'.
            anthropic_client: for simulator + judge. Built if None.
            trace_id: correlation ID for structured logs.

        Returns a dict with:
            ``session_token`` â€” grouping key for all turns' artifacts
            ``turns`` â€” per-turn results (same shape as scripted mode)
            ``overall_passed`` â€” True iff every turn passed (scripted) OR
                rubric_verdict.passed (agentic)
            ``overall_score`` â€” mean per-turn (scripted) OR overall_score
                from rubric_verdict (agentic)
            ``audio_paths`` â€” all caller + agent artifacts
            ``merged_audio_path`` â€” merged conversation file if available
            ``evaluation_mode`` â€” which mode actually ran ('agentic' |
                'scripted')
            ``rubric_verdict`` (agentic only) â€” RubricVerdict as dict
            ``transcript`` (agentic only) â€” list[ConversationTurn as dict]
            ``simulator_cost_usd`` (agentic only) â€” sum of simulator calls
            ``judge_cost_usd`` (agentic only) â€” single judge call cost
            ``sim_end_reason`` (agentic only) â€” 'goal_achieved' | etc.
        """
        # Global mode override â€” PUZZLEEVAL_CONVERSATION_EVAL_MODE wins
        # when set to a non-'auto' value. Lets operators force a mode
        # across an entire run for diagnostic A/B comparison without
        # touching per-test TestCase fields.
        try:
            from puzzleeval.config import CONVERSATION_EVAL_MODE as _GLOBAL_MODE
        except Exception:  # pragma: no cover â€” config edge
            _GLOBAL_MODE = "auto"
        effective_mode = _GLOBAL_MODE if _GLOBAL_MODE != "auto" else evaluation_mode

        # Auto-detect: if persona+goal+rubric all present â†’ agentic; else scripted.
        if effective_mode == "auto":
            if persona is not None and goal and rubric:
                effective_mode = "agentic"
            else:
                effective_mode = "scripted"

        # Explicit agentic without full kit â†’ fall back to scripted
        # with a warning. Prevents a missing field from crashing â€” tests
        # may intentionally leave one out to exercise the fallback.
        if effective_mode == "agentic" and not (
            persona is not None and goal and rubric
        ):
            logger.warning(
                "drive_conversation: evaluation_mode='agentic' requested "
                "but persona/goal/rubric incomplete â€” falling back to "
                "scripted.",
                extra={"operation": "agentic_mode_fallback",
                       "trace_id": trace_id},
            )
            effective_mode = "scripted"

        # â”€â”€ Dispatch to agentic path when selected â”€â”€
        if effective_mode == "agentic":
            return self._drive_conversation_agentic(
                persona=persona,                   # type: ignore[arg-type]
                goal=goal,                         # type: ignore[arg-type]
                constraints=constraints or [],
                rubric=rubric,                     # type: ignore[arg-type]
                max_turns=max_turns,
                agent_responder=agent_responder,
                scope_role=scope_role,
                shape=shape,
                evaluation_mode=effective_mode,
                anthropic_client=anthropic_client,
                trace_id=trace_id,
                agent_system_prompt=agent_system_prompt,
                progress_callback=progress_callback,
                release_harness_session=release_harness_session,
            )

        # Scripted path: deterministic checks collect evidence; production
        # evaluator calls also run semantic review before accepting pass/fail.
        session_token = secrets.token_hex(16)
        session_state: dict[str, Any] = {
            "shape": shape,
            "scope_role": scope_role,
            "session_token": session_token,
        }
        per_turn: list[dict[str, Any]] = []
        all_scores: list[float] = []
        all_passed = True

        for idx, turn in enumerate(script or []):
            # 1. Synthesize the caller's audio for this turn. The plugin
            # reuses its existing TTS path; one new token per turn so each
            # audio file has a unique URL for debug-ability and playback.
            turn_token = f"{session_token}-t{idx}"
            user_text = str(turn.get("user_text") or turn.get("text") or "")
            if not user_text:
                per_turn.append({
                    "turn_index": idx, "skipped": True,
                    "reason": "empty user_text in script turn",
                })
                all_passed = False
                continue

            caller_path = self._synthesize_caller_audio(user_text, turn_token)
            with self._lock:
                self._token_to_audio[turn_token] = caller_path
            caller_url = self.public_url_for_audio(turn_token)
            session_state.setdefault("conversation_history", []).append({
                "role": "user",
                "content": user_text,
                "turn_index": idx,
            })

            # 2. Invoke the harness's agent_responder for THIS turn. It
            # reads session_state (conversation_id etc.) and mutates it
            # on return so the next turn has continuity. The plugin does
            # not interpret state â€” the harness template knows its API.
            try:
                resp = agent_responder(idx, caller_url, session_state) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_responder crashed at turn %d: %s", idx, exc)
                per_turn.append({
                    "turn_index": idx,
                    "caller_path": caller_path,
                    "caller_text": user_text,
                    "agent_path": None,
                    "agent_text": "",
                    "passed": False,
                    "score": 0.0,
                    "reasoning": f"agent_responder exception: {exc}",
                    "expected_contains": turn.get("expected_agent_contains") or "",
                })
                all_passed = False
                all_scores.append(0.0)
                continue

            # 3. Extract agent text from whatever shape came back. Use
            # the same resolution order as single-turn evaluate_output
            # so new response shapes automatically work here too.
            agent_text, agent_path = self._extract_agent_text_and_path(
                resp, turn_token,
            )

            # 4. Score this turn into evidence signals. These cheap checks
            # help explain what happened, but production callers set
            # semantic_review_required so an evidence-grounded judge owns
            # final scripted conversation pass/fail.
            expected_contains = str(turn.get("expected_agent_contains") or "").lower().strip()
            expected_exact = str(turn.get("expected_agent_text") or "").lower().strip()
            reply_lower = agent_text.lower()
            passed = False
            score = 0.0
            reasoning = ""
            if not agent_text:
                reasoning = "agent produced no text (no audio, no structured response)"
            elif expected_contains and expected_contains in reply_lower:
                passed = True
                score = 1.0
                reasoning = f"response contains '{expected_contains}'"
            elif expected_exact and expected_exact == reply_lower:
                passed = True
                score = 1.0
                reasoning = "response exactly matches expected text"
            elif expected_contains:
                # Partial credit when SOME words match â€” helps rank
                # candidates that are close but not perfect.
                hit_words = [
                    w for w in expected_contains.split()
                    if len(w) > 3 and w in reply_lower
                ]
                if hit_words:
                    score = min(0.6, 0.1 * len(hit_words))
                    reasoning = f"partial match; hit words: {hit_words}"
                else:
                    reasoning = f"response did not contain '{expected_contains}'"
            else:
                # No expected text given â€” presence of any reply is pass.
                passed = True
                score = 0.7
                reasoning = "no expected_agent_contains â€” treating presence as pass"

            per_turn.append({
                "turn_index": idx,
                "caller_path": caller_path,
                "caller_text": user_text,
                "agent_path": agent_path,
                "agent_text": agent_text[:800],
                "passed": passed,
                "score": score,
                "reasoning": reasoning,
                "expected_contains": expected_contains,
            })
            all_scores.append(score)
            session_state.setdefault("conversation_history", []).append({
                "role": "assistant",
                "content": agent_text[:2000],
                "turn_index": idx,
            })
            if not passed:
                all_passed = False

        semantic_review = None
        if semantic_review_required and per_turn:
            from puzzleeval.semantic_review import review_scripted_conversation_semantics

            transcript: list[dict[str, Any]] = []
            assertions: list[dict[str, Any]] = []
            for turn in per_turn:
                idx = int(turn.get("turn_index") or 0)
                caller_text = str(turn.get("caller_text") or "")
                agent_text = str(turn.get("agent_text") or "")
                if caller_text:
                    transcript.append({
                        "turn_index": len(transcript),
                        "role": "user",
                        "text": caller_text,
                        "meta": {"script_turn_index": idx},
                    })
                if agent_text:
                    transcript.append({
                        "turn_index": len(transcript),
                        "role": "agent",
                        "text": agent_text,
                        "meta": {"script_turn_index": idx},
                    })
                expected = str(turn.get("expected_contains") or "").strip()
                if expected:
                    assertions.append({
                        "turn_index": idx,
                        "check_type": "contains",
                        "value": expected,
                        "weight": 1.0,
                    })
            if assertions:
                semantic_review = review_scripted_conversation_semantics(
                    transcript=transcript,
                    assertions=assertions,
                    candidate_role=scope_role,
                    agent_system_prompt=agent_system_prompt or "",
                    client=anthropic_client,
                    trace_id=trace_id,
                )
                if semantic_review.get("available"):
                    all_passed = bool(semantic_review.get("passed"))
                    all_scores = [float(semantic_review.get("score") or 0.0)]

        overall_score = sum(all_scores) / len(all_scores) if all_scores else 0.0
        artifacts = self.artifacts_for_token_prefix(session_token)

        # Submit conversation merge to background daemon pool. pydub
        # decode + sample-rate normalize + concat + re-encode of N
        # MP3s is 50-72s of CPU
        # work; running it synchronously here would gate next-batch
        # start by `merge_time` per worker. Background submit frees
        # workers at conversation-end. Agent 5's
        # `voice_plugin.wait_for_pending_merges()` after
        # `_execute_all_tests` patches `merged_audio_path` and the
        # role='conversation' artifact entry into the result
        # retroactively. General across shapes (twilio/vonage/generic).
        try:
            self.submit_merge_in_background(
                session_token=session_token,
                turns=per_turn,
            )
        except Exception as exc:  # noqa: BLE001 â€” submit must never crash
            logger.info(
                "conversation merge submit failed (non-fatal): %s", exc,
                extra={"operation": "voice_merge_submit_failed",
                       "session_token": session_token},
            )
        merged_path: str | None = None

        return {
            "session_token": session_token,
            "turns": per_turn,
            "overall_passed": all_passed,
            "overall_score": overall_score,
            "audio_paths": artifacts,
            "merged_audio_path": merged_path,
            "evaluation_mode": "scripted",
            "semantic_review": semantic_review,
        }

    # ------------------------------------------------------------------
    # Agentic drive loop â€” simulator-driven turns + rubric-judged outcome
    # ------------------------------------------------------------------
    def _drive_conversation_agentic(
        self,
        *,
        persona: "Persona",
        goal: str,
        constraints: list[str],
        rubric: "list[RubricCriterion]",
        max_turns: int | None,
        agent_responder,
        scope_role: str,
        shape: str,
        evaluation_mode: str,
        anthropic_client,
        trace_id: str,
        agent_system_prompt: str | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        release_harness_session: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Internal agentic driver â€” called from drive_conversation when
        effective_mode resolves to 'agentic'.

        Turn-by-turn flow:
          1. user_simulator.generate_next_user_turn(history, persona, goal, ...)
             â†’ returns SimulatorTurn with text + end_conversation + end_reason
          2. If end_conversation: append final user turn, break loop
          3. Synthesize caller audio from user text (existing TTS path)
          4. agent_responder(turn_index, caller_url, session_state)
             â†’ returns AgentResponse dict
          5. Extract agent text from response (shared helper with scripted)
          6. Append ConversationTurn(role='agent') to history
          7. Loop until end_conversation OR max_turns

        After loop: rubric_judge scores the full transcript. Rubric verdict
        + cost accounting + transcript all go into the return dict.

        Max-turn resolution: min(test.max_turns, CONVERSATION_MAX_TURNS_CEILING).
        """
        from puzzleeval.config import (
            CONVERSATION_DEFAULT_MAX_TURNS,
            CONVERSATION_MAX_TURNS_CEILING,
        )
        from puzzleeval.user_simulator import generate_next_user_turn
        from puzzleeval.rubric_judge import judge_conversation
        from puzzleeval.schemas import ConversationTurn

        resolved_max = min(
            max_turns if max_turns is not None else CONVERSATION_DEFAULT_MAX_TURNS,
            CONVERSATION_MAX_TURNS_CEILING,
        )
        session_token = secrets.token_hex(16)
        session_state: dict[str, Any] = {
            "shape": shape,
            "scope_role": scope_role,
            "session_token": session_token,
        }
        per_turn_artifacts: list[dict[str, Any]] = []
        transcript: list[ConversationTurn] = []
        simulator_cost = 0.0
        sim_end_reason: str = "ongoing"
        drive_started_at = time.monotonic()

        def _emit_progress(event_type: str, **payload: Any) -> None:
            event_payload = {
                "trace_id": trace_id,
                "session_token": session_token,
                "evaluation_mode": evaluation_mode,
                "persona_name": persona.name,
                "max_turns": resolved_max,
            }
            event_payload.update(payload)
            if progress_callback is not None:
                try:
                    progress_callback(event_type, event_payload)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "voice progress callback failed",
                        extra={
                            "operation": "voice_progress_callback_failed",
                            "trace_id": trace_id,
                            "event_type": event_type,
                        },
                    )
            logger.info(
                "voice evaluation progress: %s",
                event_type,
                extra={"operation": event_type, **event_payload},
            )

        logger.info(
            "agentic drive_conversation starting",
            extra={
                "operation": "agentic_drive_start",
                "trace_id": trace_id,
                "scope_role": scope_role,
                "max_turns": resolved_max,
                "persona_name": persona.name,
            },
        )

        turn_idx = 0
        while turn_idx < resolved_max:
            # â”€â”€ 1. Generate the next user turn via simulator â”€â”€
            try:
                _emit_progress("simulator_turn_started", turn_index=turn_idx)
                sim_turn = generate_next_user_turn(
                    history=transcript,
                    persona=persona,
                    goal=goal,
                    constraints=constraints,
                    turn_index=turn_idx,
                    max_turns=resolved_max,
                    client=anthropic_client,
                    trace_id=trace_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "simulator crashed at turn %d: %s", turn_idx, exc,
                    extra={"operation": "simulator_crash",
                           "trace_id": trace_id},
                )
                # Treat simulator crash as abandonment â€” break cleanly so
                # the judge can still score whatever transcript we have.
                sim_end_reason = "abandoned"
                break

            simulator_cost += sim_turn.cost_usd
            _emit_progress(
                "simulator_turn_completed",
                turn_index=turn_idx,
                end_conversation=bool(sim_turn.end_conversation),
                end_reason=sim_turn.end_reason,
                text_chars=len(sim_turn.text or ""),
                cost_usd=round(sim_turn.cost_usd, 6),
            )

            # Append the simulator's utterance to the transcript. Even
            # when end_conversation=True with empty text, we append an
            # empty user turn so the judge knows the caller hung up on
            # this turn (useful for scoring goal_completion when agent
            # caused the abandonment).
            user_text = (sim_turn.text or "").strip()
            transcript.append(ConversationTurn(
                turn_index=turn_idx * 2,       # interleaved indexing: user=even, agent=odd
                role="user",
                text=user_text,
                meta={
                    "end_reason": sim_turn.end_reason,
                    "simulator_cost_usd": sim_turn.cost_usd,
                },
            ))

            # Simulator-driven end: break BEFORE calling agent (user hung up).
            if sim_turn.end_conversation:
                sim_end_reason = sim_turn.end_reason
                logger.info(
                    "simulator ended conversation: %s", sim_end_reason,
                    extra={"operation": "simulator_ended",
                           "trace_id": trace_id,
                           "reason": sim_end_reason,
                           "turn_index": turn_idx},
                )
                break

            # User text empty but not end_conversation â†’ can't feed empty
            # to TTS. Treat as abandonment.
            if not user_text:
                sim_end_reason = "abandoned"
                break

            # â”€â”€ 2. Synthesize caller audio + serve â”€â”€
            turn_token = f"{session_token}-t{turn_idx}"
            caller_path = self._synthesize_caller_audio(user_text, turn_token)
            with self._lock:
                self._token_to_audio[turn_token] = caller_path
            caller_url = self.public_url_for_audio(turn_token)

            # â”€â”€ 3. Invoke the agent responder â”€â”€
            session_state.setdefault("conversation_history", []).append({
                "role": "user",
                "content": user_text,
                "turn_index": turn_idx,
            })
            try:
                provider_started = time.monotonic()
                resp = agent_responder(turn_idx, caller_url, session_state) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent_responder crashed at turn %d: %s",
                    turn_idx, exc,
                    extra={"operation": "agent_responder_crash",
                           "trace_id": trace_id},
                )
                # Append a synthetic agent turn recording the failure so
                # the judge sees the agent failed â€” this should tank
                # accuracy/goal_completion criteria appropriately.
                transcript.append(ConversationTurn(
                    turn_index=turn_idx * 2 + 1,
                    role="agent",
                    text=f"[AGENT ERROR: {exc}]",
                    meta={"error": str(exc)},
                ))
                sim_end_reason = "agent_failed"
                break

            # â”€â”€ 4. Extract agent text + path â”€â”€
            agent_text, agent_path = self._extract_agent_text_and_path(
                resp, turn_token,
            )
            _emit_progress(
                "provider_turn_completed",
                turn_index=turn_idx,
                latency_ms=round((time.monotonic() - provider_started) * 1000, 1),
                has_audio=bool(agent_path),
                has_text=bool(agent_text),
                agent_text_chars=len(agent_text or ""),
            )

            # Append agent turn to transcript
            transcript.append(ConversationTurn(
                turn_index=turn_idx * 2 + 1,
                role="agent",
                text=agent_text[:2000] if agent_text else "",  # cap to avoid prompt bloat
                meta={"agent_path": agent_path} if agent_path else {},
            ))
            session_state.setdefault("conversation_history", []).append({
                "role": "assistant",
                "content": agent_text[:2000] if agent_text else "",
                "turn_index": turn_idx,
            })

            # Track per-turn artifacts for return (parallels scripted path)
            per_turn_artifacts.append({
                "turn_index": turn_idx,
                "caller_path": caller_path,
                "caller_text": user_text,
                "agent_path": agent_path,
                "agent_text": agent_text[:800] if agent_text else "",
            })

            turn_idx += 1

        # If the loop ran max turns without end_conversation, record it
        if sim_end_reason == "ongoing" and turn_idx >= resolved_max:
            sim_end_reason = "max_turns"

        # Conversation capture is complete. Submit the merge and release
        # any persistent provider worker before the rubric judge starts so
        # playback and provider cleanup are not blocked by LLM latency.
        artifacts = self.artifacts_for_token_prefix(session_token)
        try:
            self.submit_merge_in_background(
                session_token=session_token,
                turns=per_turn_artifacts,
            )
            _emit_progress(
                "merge_submitted",
                turn_count=len(per_turn_artifacts),
                artifact_count=len(artifacts),
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "conversation merge submit failed (non-fatal): %s", exc,
                extra={"operation": "voice_merge_submit_failed",
                       "session_token": session_token},
            )
        if release_harness_session is not None:
            try:
                release_harness_session()
                _emit_progress("provider_session_released")
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "persistent harness release failed after transcript capture: %s",
                    exc,
                    extra={"operation": "persistent_harness_release_failed",
                           "trace_id": trace_id,
                           "session_token": session_token},
                )

        # â”€â”€ 5. Judge the full transcript â”€â”€
        judge_cost = 0.0
        rubric_verdict = None
        judge_failed = False
        judge_failure_reason: str | None = None
        try:
            _emit_progress(
                "rubric_judge_started",
                transcript_turn_count=len(transcript),
            )
            rubric_verdict = judge_conversation(
                transcript=transcript,
                persona=persona,
                goal=goal,
                rubric=rubric,
                candidate_role=scope_role,
                agent_system_prompt=agent_system_prompt,
                # Use rubric_judge's bounded client policy instead of the
                # broader simulator/research client. The provider session has
                # already been released; a slow judge should fail this test,
                # not occupy a worker with the general SDK timeout budget.
                client=None,
                trace_id=trace_id,
            )
            judge_cost = rubric_verdict.cost_usd
            _emit_progress(
                "rubric_judge_completed",
                passed=bool(rubric_verdict.passed),
                overall_score=float(rubric_verdict.overall_score),
                cost_usd=round(judge_cost, 6),
            )
        except Exception as exc:  # noqa: BLE001
            judge_failed = True
            judge_failure_reason = f"{type(exc).__name__}: {exc}"
            _emit_progress(
                "rubric_judge_completed",
                passed=False,
                judge_failed=True,
                error_type=type(exc).__name__,
            )
            logger.warning(
                "rubric_judge crashed: %s", exc,
                extra={"operation": "rubric_judge_crash",
                       "trace_id": trace_id},
            )
        # `merged_audio_path` and the role='conversation' artifact entry are
        # populated retroactively by Agent 5's `wait_for_pending_merges()`
        # call after all tests finish. The merge was submitted immediately
        # after transcript capture, before judge_conversation, so playback is
        # not blocked by LLM judge latency.
        merged_path: str | None = None

        # â”€â”€ 6. Assemble return dict â”€â”€
        # The agentic path's per_turn entries intentionally lack passed/
        # score/reasoning fields â€” those are rubric-judge-scope, not
        # per-turn. Callers that need per-turn pass/fail should read
        # rubric_verdict.criterion_scores with their evidence_turn_indices.
        overall_score = (
            rubric_verdict.overall_score if rubric_verdict is not None else 0.0
        )
        overall_passed = (
            rubric_verdict.passed if rubric_verdict is not None else False
        )

        result: dict[str, Any] = {
            "session_token": session_token,
            "turns": per_turn_artifacts,
            "overall_passed": overall_passed,
            "overall_score": overall_score,
            "audio_paths": artifacts,
            "merged_audio_path": merged_path,
            "evaluation_mode": evaluation_mode,
            "rubric_verdict": rubric_verdict.model_dump() if rubric_verdict else None,
            "judge_failed": judge_failed,
            "judge_failure_reason": judge_failure_reason,
            "transcript": [t.model_dump() for t in transcript],
            "simulator_cost_usd": round(simulator_cost, 6),
            "judge_cost_usd": round(judge_cost, 6),
            "total_duration_s": round(time.monotonic() - drive_started_at, 3),
            "sim_end_reason": sim_end_reason,
        }

        return result

    def _merge_conversation_audio(
        self, *, session_token: str, turns: list[dict[str, Any]],
    ) -> str | None:
        """Concatenate every caller+agent audio file for a session into
        a single playback file.

        Two implementations, picked by capability detection:

        * **pydub path (preferred when pydub + ffmpeg are available):**
          decode every per-turn file into an ``AudioSegment``,
          resample/rechannel to a uniform format (the first segment's
          frame rate + channels â€” usually the caller TTS), concatenate,
          and export. Produces a uniformly-encoded output that browsers
          and players handle correctly even when caller and agent files
          had different sample rates.

        * **Byte-concat fallback (when pydub/ffmpeg unavailable):** the
          historical pure-Python path. Strips ID3 tags so MP3 frames
          stitch reasonably, but does NOT reconcile sample-rate
          differences â€” players that strict-decode based on the first
          frame's sample rate will glitch at boundaries.

        Real-run signal (trace 28cb2648): caller TTS produced MP3 at
        44.1 kHz, OpenAI Realtime â†’ pydub MP3 produced agent files at
        24 kHz, byte-concat resulted in a file that played the first
        segment correctly then paused at the first sample-rate
        transition. The pydub path resamples both to 44.1 kHz before
        concat â€” playback is now uniform.

        Output written to ``<session_dir>/conversation_<token>.<ext>``
        alongside the per-turn files so the RunState-scoped path
        containment check in the backend's audio streamer still passes.
        """
        ordered_paths: list[Path] = []
        for turn in turns:
            c = turn.get("caller_path")
            a = turn.get("agent_path")
            if c:
                ordered_paths.append(Path(c))
            if a:
                ordered_paths.append(Path(a))
        if len(ordered_paths) < 2:
            return None
        # All files must exist + share one extension.
        exts = {p.suffix.lower() for p in ordered_paths if p.exists()}
        if len(exts) != 1:
            return None
        ext = exts.pop()
        if ext not in {".mp3", ".wav", ".ogg", ".m4a", ".webm", ".mpga", ".mp4"}:
            return None
        existing = [p for p in ordered_paths if p.exists()]
        if len(existing) < 2:
            return None
        # Write next to the first file â€” keeps the run-dir containment
        # guarantee the backend audio streamer relies on.
        out_dir = existing[0].parent
        out_path = out_dir / f"conversation_{session_token[:16]}{ext}"

        # Try pydub-based merge first (handles sample-rate mismatches).
        merged_via_pydub = self._try_merge_via_pydub(existing, out_path, ext)
        if merged_via_pydub:
            return str(out_path)

        # Fallback: pure byte-concat with ID3 tag stripping. Works when
        # all per-turn files share encoding (rare in voice runs since
        # caller TTS and agent audio usually differ). Logged so we know
        # we took the degraded path.
        logger.info(
            "conversation merge falling back to byte-concat (pydub/ffmpeg "
            "not available); sample-rate mismatches may glitch playback",
            extra={"operation": "voice_merge_byte_concat_fallback"},
        )
        try:
            with open(out_path, "wb") as out_f:
                for i, p in enumerate(existing):
                    body = p.read_bytes()
                    # ID3-tag-aware concat. Pure byte-concat of MP3s
                    # produces a file the OS sees as NÃ—file_size bytes
                    # but most players honor the FIRST file's ID3v2
                    # duration tag and stop after the first segment.
                    # Strip ID3v2 from segments 2..N (keep the first's
                    # tag for codec init) and ID3v1 trailers from all
                    # segments so they don't appear mid-stream.
                    if ext == ".mp3":
                        if i > 0:
                            body = _strip_id3v2_header(body)
                        body = _strip_id3v1_trailer(body)
                    out_f.write(body)
        except OSError as exc:
            logger.info(
                "conversation merge write failed: %s", exc,
                extra={"operation": "voice_merge_write_failed"},
            )
            return None
        return str(out_path)

    def _try_merge_via_pydub(
        self,
        ordered_paths: list[Path],
        out_path: Path,
        ext: str,
    ) -> bool:
        """Decode per-turn audio, normalize to a uniform format, and
        export the merged file. Returns True on success; False when
        pydub or ffmpeg isn't available (caller falls back to byte-
        concat) or when any decode/export step raises.

        Normalization target: the FIRST segment's frame rate + channel
        count. Caller TTS is usually segment 0 â€” preserving its sample
        rate keeps speech intelligibility intact while bringing the
        agent's lower-rate audio up to match. Equally fine to pick a
        fixed target (e.g., 44100 mono); the first-segment heuristic
        avoids hard-coding while staying deterministic.
        """
        try:
            from pydub import AudioSegment  # type: ignore
        except Exception:
            return False
        try:
            segments = []
            for p in ordered_paths:
                seg = AudioSegment.from_file(str(p))
                segments.append(seg)
            if not segments:
                return False
            target_fr = segments[0].frame_rate
            target_ch = segments[0].channels
            normalized = [
                s.set_frame_rate(target_fr).set_channels(target_ch)
                for s in segments
            ]
            combined = normalized[0]
            for s in normalized[1:]:
                combined = combined + s
            export_format = ext.lstrip(".")
            # pydub uses "mp3" for export, not "mpga" / "mp4". Map.
            if export_format in ("mpga",):
                export_format = "mp3"
            elif export_format in ("mp4", "m4a"):
                export_format = "ipod"  # pydub's name for m4a/AAC export
            combined.export(str(out_path), format=export_format)
            return True
        except Exception as exc:  # noqa: BLE001
            # Any decode/encode failure â†’ fall back. Logged at info
            # because the byte-concat path still produces something.
            logger.info(
                "pydub merge failed (%s) â€” falling back to byte-concat",
                exc,
                extra={"operation": "voice_merge_pydub_failed"},
            )
            return False

    def _evaluate_conversation(
        self,
        *,
        script: list[dict[str, Any]],
        expected: dict[str, Any],
        harness_runner,
        # â”€â”€ Agentic conversational eval (new) â”€â”€
        # When persona/goal/rubric are populated, drive_conversation
        # routes to the simulator-driven path. When empty and a
        # `script` is provided, drive_conversation uses the legacy
        # scripted path. Both routes converge back through
        # drive_conversation â†’ ``EvaluationResult`` with the same shape.
        persona: Any = None,
        goal: str | None = None,
        constraints: list[str] | None = None,
        rubric: list | None = None,
        max_turns: int = 4,
        evaluation_mode: str = "auto",
        trace_id: str = "no-trace",
        semantic_review_required: bool = False,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        release_harness_session: Callable[[], None] | None = None,
    ) -> EvaluationResult:
        """Multi-turn path for evaluate_output (agentic OR scripted).

        Builds an agent_responder closure that wraps ``harness_runner``:
        for each turn, the plugin constructs a payload (caller audio URL
        + per-turn context), the runner invokes the candidate's
        single-turn harness.run(), the plugin normalizes the response
        into the shape ``drive_conversation`` expects, and continues.

        This is the bridge that lets the plugin OWN multi-turn
        orchestration while the harness stays single-turn. No new
        harness template branch, no new Agent 4 authority field.

        Agentic kwargs are forwarded to drive_conversation â€” when
        populated, drive_conversation swaps the static script
        iterator for user_simulator + rubric_judge. When absent,
        drive_conversation uses the legacy scripted path unchanged.
        """
        if harness_runner is None:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning=(
                    "voice_conversation requires harness_runner; Agent 5 "
                    "must inject one for plugins with requires_harness_runner"
                ),
                fallback_reason="no_runner",
            )

        session_state: dict[str, Any] = expected.get("session_state") or {}

        # Extract the agent's system prompt from expected (if provided
        # via the test's input_context) so the rubric judge can use it
        # as ground truth for scope/policy scoring. Alias list mirrors
        # the harness-side fallback order from AD-007:
        #   instructions / system_prompt / system / brief / agent_prompt
        # Returns empty string when none is found â€” judge falls back to
        # general-plausibility scoring.
        agent_system_prompt: str = ""
        if isinstance(expected, dict):
            # Prefer a nested input_context (the Agent 5 runner injects
            # merged input_context here when it threads through).
            ic = expected.get("input_context")
            if isinstance(ic, dict):
                for _alias in ("instructions", "system_prompt", "system",
                               "brief", "agent_prompt"):
                    _val = ic.get(_alias)
                    if isinstance(_val, str) and _val.strip():
                        agent_system_prompt = _val.strip()
                        break
            # Fallback: check expected.instructions directly (some
            # callers flatten the key onto expected).
            if not agent_system_prompt:
                _val = expected.get("instructions")
                if isinstance(_val, str) and _val.strip():
                    agent_system_prompt = _val.strip()

        def _responder(turn_idx: int, user_audio_url: str, state: dict[str, Any]):
            # Per-turn payload handed to harness.run(). The harness is
            # single-turn: takes an audio URL + optional session state,
            # returns {success, output, raw_response, ...}. Carrying
            # ``session_state`` through is how multi-turn state (cookie,
            # conversation_id, session token) flows â€” the harness
            # template reads + writes it without knowing the plugin
            # owns the outer loop.
            payload = {
                "audio_url": user_audio_url,
                "caller_audio_url": user_audio_url,
                "turn_index": turn_idx,
                "session_state": state,
                "input_context": expected.get("input_context") if isinstance(expected, dict) else {},
                "conversation_history": list(state.get("conversation_history") or []),
            }
            try:
                turn_result = harness_runner(payload) or {}
            except Exception as exc:  # noqa: BLE001
                return {"text": "", "error": f"runner crash: {exc}"}
            if not isinstance(turn_result, dict):
                return {"text": ""}
            # If the harness updated session_state, propagate it back.
            new_state = turn_result.get("session_state")
            if isinstance(new_state, dict):
                state.update(new_state)
            raw = turn_result.get("raw_response") or {}
            # Normalize into drive_conversation's responder contract.
            # Harnesses typically return ``output`` as a text string plus
            # ``raw_response`` with provider-specific fields. Map the
            # common shapes without hardcoding any provider.
            if isinstance(raw, dict):
                if raw.get("audio_bytes"):
                    # content_type priority: explicit field â†’ derive from
                    # `audio_format` â†’ fall through to None.
                    # For RAW PCM (pcm16 / pcm_s16le / etc â€” no container
                    # header), wrap the bytes in a WAV/RIFF header here so
                    # the saved file is actually playable. Browsers and
                    # `<audio>` tags can't decode raw PCM; they need the
                    # container. Real-run signal (OpenAI Realtime): the
                    # harness returned audio_format="pcm16" at 24kHz mono,
                    # and the saved `.wav` files had NO header â€” so click-
                    # to-play did nothing AND the merger couldn't stitch
                    # them (mixed .mp3 caller + .wav agent = merge rejects).
                    fmt = (raw.get("audio_format") or "").lower().strip()
                    ct = raw.get("audio_content_type")
                    audio_bytes_out = raw["audio_bytes"]
                    # Normalize b64-string payloads to bytes BEFORE any
                    # format-aware processing. Harnesses that cross the
                    # subprocess JSON border typically base64-encode bytes
                    # themselves (our OpenAI Realtime harness does
                    # `base64.b64encode(pcm).decode("ascii")` at line ~173).
                    # pydub's AudioSegment accepts a string at construction
                    # time, THEN fails deep inside `.export()` with
                    # "memoryview: a bytes-like object is required, not
                    # 'str'" â€” the exception gets caught by the `try:`
                    # wrappers below, `encoded` comes back None, and
                    # `_wrap_pcm16_as_wav` throws the same TypeError. The
                    # net result: the saved agent audio drops entirely
                    # (no response_*.wav / no response_*.mp3 on disk),
                    # `agent_text` stays empty, every turn scores 0.
                    # Decoding ONCE here covers both _encode_pcm16_to_mp3
                    # AND the wrap-as-wav fallback AND the downstream
                    # `_extract_agent_text_and_path` call â€” the pipeline
                    # no longer has a string-flavored PCM branch.
                    if isinstance(audio_bytes_out, str) and len(audio_bytes_out) >= 100:
                        try:
                            import base64 as _b64
                            decoded = _b64.b64decode(audio_bytes_out, validate=False)
                            if decoded:
                                audio_bytes_out = decoded
                        except (ValueError, TypeError) as exc:
                            logger.warning(
                                "audio_bytes b64-decode failed (falling through "
                                "to string-handling): %s", exc,
                            )
                    if fmt in ("pcm", "pcm16", "pcm_s16le", "l16", "raw_pcm16"):
                        sr = int(raw.get("audio_sample_rate") or 24000)
                        ch = int(raw.get("audio_channels") or 1)
                        # Prefer MP3 (matches caller TTS files â†’ merger can
                        # concat uniformly, browsers play directly). Fall
                        # back to WAV-wrap when pydub/ffmpeg unavailable.
                        encoded = _encode_pcm16_to_mp3(
                            audio_bytes_out, sample_rate=sr, channels=ch,
                        )
                        if encoded:
                            audio_bytes_out = encoded
                            ct = "audio/mpeg"
                        else:
                            try:
                                audio_bytes_out = _wrap_pcm16_as_wav(
                                    audio_bytes_out, sample_rate=sr, channels=ch,
                                )
                                ct = "audio/wav"
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "PCM16 WAV-wrap failed (saving raw): %s", exc,
                                )
                    if not ct:
                        ct = {
                            "mp3":  "audio/mpeg",
                            "mpeg": "audio/mpeg",
                            "mpga": "audio/mpeg",
                            "wav":  "audio/wav",
                            "wave": "audio/wav",
                            "ogg":  "audio/ogg",
                            "opus": "audio/ogg",
                            "m4a":  "audio/mp4",
                            "mp4":  "audio/mp4",
                            "webm": "audio/webm",
                            "flac": "audio/flac",
                        }.get(fmt)
                    return {
                        "audio_bytes": audio_bytes_out,
                        "audio_content_type": ct,
                    }
                # Harness-on-disk return shape: some harnesses save audio
                # to a local temp file and return just the PATH in
                # raw_response (keeps the JSON payload small, avoids
                # cross-process bytes marshaling). Any harness using
                # AudioSegment.export("path.wav") or tempfile naturally
                # falls into this pattern. Read the file here into
                # audio_bytes and continue down the bytes path so the
                # downstream `_save_audio_blob` sees a uniform contract.
                # content_type inferred from the extension; falls back to
                # WAV when the extension is unknown.
                audio_path = raw.get("audio_path")
                if isinstance(audio_path, str) and audio_path:
                    try:
                        import os as _os
                        if _os.path.exists(audio_path):
                            with open(audio_path, "rb") as _f:
                                _audio_on_disk = _f.read()
                            if _audio_on_disk:
                                _ext = _os.path.splitext(audio_path)[1].lower().lstrip(".")
                                _ct = {
                                    "mp3": "audio/mpeg", "mpeg": "audio/mpeg",
                                    "wav": "audio/wav", "wave": "audio/wav",
                                    "ogg": "audio/ogg", "opus": "audio/ogg",
                                    "m4a": "audio/mp4", "mp4": "audio/mp4",
                                    "webm": "audio/webm", "flac": "audio/flac",
                                }.get(_ext, "audio/wav")
                                return {
                                    "audio_bytes": _audio_on_disk,
                                    "audio_content_type": _ct,
                                }
                    except (OSError, IOError) as _exc:
                        logger.warning(
                            "Failed to read harness-returned audio_path %s: %s",
                            audio_path, _exc,
                        )
                if raw.get("twiml"):
                    return {"twiml": raw["twiml"]}
                if raw.get("ncco"):
                    return {"ncco": raw["ncco"]}
            out = turn_result.get("output")
            if isinstance(out, str) and out:
                return {"text": out}
            if isinstance(out, dict):
                return {"json": out}
            return {"text": ""}

        run = self.drive_conversation(
            script=script,
            agent_responder=_responder,
            shape=str(expected.get("shape") or "generic"),
            scope_role=str(expected.get("scope_role") or "voice_agent"),
            # Forward the agentic kit â€” drive_conversation auto-detects
            # agentic vs scripted based on which fields are populated.
            persona=persona,
            goal=goal,
            constraints=list(constraints or []),
            rubric=list(rubric or []),
            max_turns=max_turns,
            evaluation_mode=evaluation_mode,
            trace_id=trace_id,
            # Ground the rubric judge: pass the actual system prompt
            # the candidate agent was configured with so it scores
            # scope/policy against the REAL contract, not a guess.
            # Empty string when no instructions were provided.
            agent_system_prompt=agent_system_prompt,
            semantic_review_required=semantic_review_required,
            progress_callback=progress_callback,
            release_harness_session=release_harness_session,
        )
        # The agentic path populates additional keys; pass them through
        # in `detail` so _promote_verdict_to_tcr can surface rubric_
        # verdict + transcript on the TestCaseResult.
        #
        # reasoning precedence: agentic path emits a conversation_summary
        # inside rubric_verdict (more informative than turn counts) â€”
        # use it when available.
        rubric_verdict = run.get("rubric_verdict")
        transcript = run.get("transcript") or []
        reasoning = (
            (rubric_verdict or {}).get("conversation_summary")
            if isinstance(rubric_verdict, dict)
            else None
        )
        if not reasoning:
            reasoning = (
                f"{len(run['turns'])} turns; "
                f"{sum(1 for t in run['turns'] if t.get('passed'))} passed; "
                f"session_token={run['session_token'][:12]}â€¦"
            )
        detail: dict[str, Any] = {
            "session_token": run["session_token"],
            "turns": run["turns"],
            "audio_paths": run["audio_paths"],
            "evaluation_mode": run.get("evaluation_mode", "scripted"),
        }
        if rubric_verdict is not None:
            detail["rubric_verdict"] = rubric_verdict
        if run.get("judge_failed"):
            detail["judge_failed"] = True
            detail["judge_failure_reason"] = run.get("judge_failure_reason")
        if transcript:
            detail["transcript"] = transcript
        if "simulator_cost_usd" in run:
            detail["simulator_cost_usd"] = run["simulator_cost_usd"]
        if "judge_cost_usd" in run:
            detail["judge_cost_usd"] = run["judge_cost_usd"]
        if "total_duration_s" in run:
            detail["total_duration_s"] = run["total_duration_s"]
        if "sim_end_reason" in run:
            detail["sim_end_reason"] = run["sim_end_reason"]
        return EvaluationResult(
            passed=run["overall_passed"],
            score=run["overall_score"],
            reasoning=reasoning,
            detail=detail,
        )

    def artifacts_for_token_prefix(self, prefix: str) -> list[dict[str, str]]:
        """All artifacts whose token starts with ``prefix``.

        Used by the multi-turn driver to pull every caller + agent audio
        file from a session (each turn uses a per-turn sub-token of the
        form ``<session>-t<idx>``).
        """
        out: list[dict[str, str]] = []
        with self._lock:
            for tok, items in self._token_to_artifacts.items():
                if tok == prefix or tok.startswith(f"{prefix}-"):
                    for role, path in items:
                        out.append({"role": role, "path": path, "token": tok})
        return out

    def _extract_agent_text_and_path(
        self, resp: dict[str, Any], turn_token: str,
    ) -> tuple[str, str | None]:
        """Normalize an agent_responder return into (text, saved_audio_path).

        Accepts the same shapes ``evaluate_output`` handles. Delegates
        shape parsing to the existing single-turn helpers (``_extract_agent_text``
        + ``_try_transcribe``) via a synthetic CapturedVoiceTurn so the
        multi-turn path doesn't diverge from the single-turn contract.
        """
        # 1. Audio bytes â†’ save, then STT via the transcription plugin.
        # Accept both raw bytes (in-process callers) AND base64 strings
        # (subprocess-bridged harnesses that explicitly base64-encoded
        # for JSON safety, OR the new `{"_b64": "..."}` round-trip when
        # decoded by Agent 5's `_inflate_b64_sentinels`). Real-run
        # signal: voice_dual_7's harness returned raw bytes which the
        # Agent 5 dispatcher used to stringify via `default=str` â€”
        # leaving the plugin with a useless `"b'\\xff...'"` string and
        # no way to save agent audio. The `_inflate_b64_sentinels` fix
        # at the dispatcher restores bytes; this branch is the
        # belt-and-braces for older harnesses that base64-encoded
        # themselves.
        audio_bytes = resp.get("audio_bytes")
        agent_path: str | None = None
        if isinstance(audio_bytes, str) and audio_bytes:
            # Base64 fast path: try decoding, fall through silently if
            # the string was something else (e.g., a transcript).
            try:
                import base64 as _b64
                # Only treat as base64 when it's plausibly long enough
                # to be audio AND decodes cleanly. Avoids decoding
                # short text strings the harness happens to put here.
                if len(audio_bytes) >= 100:
                    decoded = _b64.b64decode(audio_bytes, validate=False)
                    if decoded:
                        audio_bytes = decoded
            except (ValueError, TypeError):
                pass
        if isinstance(audio_bytes, (bytes, bytearray)) and audio_bytes:
            ctype = str(resp.get("audio_content_type") or "audio/wav")
            agent_path = self._save_audio_blob(turn_token, bytes(audio_bytes), ctype)
            text, _src = _try_transcribe(agent_path)
            return text, agent_path

        # 2. All other shapes: build a CapturedVoiceTurn the single-turn
        # helpers understand. Zero-duplication â€” new response shapes added
        # to _extract_agent_text automatically flow into multi-turn.
        synthetic = CapturedVoiceTurn(
            token=turn_token, received_at=time.time(),
        )
        if resp.get("text"):
            synthetic.response_json = {"text": str(resp["text"])}
        elif resp.get("twiml"):
            synthetic.twiml_text = str(resp["twiml"])
        elif resp.get("ncco"):
            ncco_val = resp["ncco"]
            synthetic.ncco_json = ncco_val if isinstance(ncco_val, list) else [ncco_val]
        elif resp.get("json"):
            synthetic.response_json = resp["json"]
        text, _src = _extract_agent_text(synthetic)
        return text, None

    # -- Synthesis ----------------------------------------------------------

    def synthesize_input(
        self, *, scope_role: str, ground_truth_hint: str | None = None,
        **kwargs: Any,
    ) -> SynthesisResult:
        """Set up one inbound voice turn for the candidate to handle.

        ``ground_truth_hint`` becomes the spoken text the "caller" says.
        ``kwargs.shape``: 'twilio' | 'vonage' | 'generic' (default).
        ``kwargs.expected_response``: text the agent SHOULD say back.
        """
        token = secrets.token_hex(16)
        spoken_text = (
            ground_truth_hint
            or kwargs.get("message")
            or f"Test caller utterance for {scope_role or 'voice agent'}"
        )
        expected_response = kwargs.get("expected_response") or ""
        shape = (kwargs.get("shape") or "generic").lower().strip()

        # Synthesize audio via the TTS plugin if available; otherwise fall back
        # to writing a placeholder text file so the URL is still serveable.
        audio_path = self._synthesize_caller_audio(spoken_text, token)
        with self._lock:
            self._token_to_audio[token] = audio_path

        audio_url = self.public_url_for_audio(token)
        callback_url = self.public_url_for_callback(token)
        recording_url = f"{callback_url}/recording"

        instructions: dict[str, Any] = {
            "token": token, "shape": shape,
            "audio_url": audio_url,
            "callback_url": callback_url,
            "recording_url": recording_url,
            "spoken_text": spoken_text,
            "expected_response": expected_response,
            "instructions": _instructions_for_shape(shape),
        }
        ground_truth = {
            "token": token, "shape": shape,
            "spoken_text": spoken_text,
            "expected_response": expected_response,
            "expected_text_substring": (expected_response or spoken_text)[:80],
        }
        return SynthesisResult(
            file_path=audio_path,
            inline_data=instructions,
            ground_truth=ground_truth,
            notes=(
                f"voice loopback ready; caller audio at {audio_url}, "
                f"agent should POST response to {callback_url}"
            ),
        )

    def _synthesize_caller_audio(self, text: str, token: str) -> str:
        """Best-effort audio synthesis via the TTS plugin.

        Falls back to a text placeholder file when no TTS provider key
        is configured, so the rest of the flow still works (the
        candidate gets a fetchable URL â€” it just contains text not
        audio, and STT-based eval will note that).
        """
        try:
            from puzzleeval.tool_plugins import get_plugin
            tts = get_plugin("tts")
            if tts is not None:
                ok, _ = tts.is_available()
                if ok:
                    result = tts.synthesize_input(
                        scope_role="voice_caller",
                        ground_truth_hint=text,
                    )
                    if result.file_path and os.path.exists(result.file_path):
                        # NORMALIZE to .mp3 in the session dir regardless
                        # of what extension the TTS provider emitted.
                        #
                        # Why: real-run trace 0c7f085f (2026-04-23) showed
                        # OpenAI candidate had 10 caller_*.mp3 + 3
                        # caller_*.wav files â€” the .wav ones came from
                        # the TTS fallover path (triggered by ElevenLabs
                        # 429s mid-conversation). The conversation
                        # merger refuses mixed extensions â€” result: 3 of
                        # 5 OpenAI tests had NO merged conversation.mp3
                        # file, so the UI couldn't render the full call
                        # and the report looked incomplete.
                        #
                        # General fix â€” the TTS pipeline now guarantees
                        # ONE caller audio format (.mp3) regardless of
                        # which provider produced the underlying bytes.
                        # Applies to every TTS provider, every fallover
                        # scenario, every modality. Transcoding via
                        # pydub preserves audio content; byte-identical
                        # copy when source is already MP3.
                        try:
                            import shutil
                            src = result.file_path
                            src_ext = os.path.splitext(src)[1].lower()
                            TARGET_EXT = ".mp3"
                            dst = self._session_dir / f"caller_{token}{TARGET_EXT}"

                            if src_ext == TARGET_EXT:
                                # Already MP3 â€” direct copy (fast path)
                                shutil.copyfile(src, dst)
                            else:
                                # Transcode to MP3 for merger compatibility
                                try:
                                    from pydub import AudioSegment
                                    seg = AudioSegment.from_file(src)
                                    seg.export(str(dst), format="mp3")
                                    logger.info(
                                        f"TTS caller audio normalized {src_ext}â†’.mp3 for merger compatibility "
                                        f"(source provider may have been via fallover)",
                                        extra={"operation": "tts_format_normalize",
                                               "src_ext": src_ext},
                                    )
                                except Exception as transcode_err:
                                    # pydub/ffmpeg not available OR
                                    # transcode failed. Fall back to
                                    # copying source extension â€” this
                                    # preserves legacy behavior rather
                                    # than losing the turn entirely.
                                    dst = self._session_dir / f"caller_{token}{src_ext or '.wav'}"
                                    shutil.copyfile(src, dst)
                                    logger.warning(
                                        f"TTS transcode to .mp3 failed ({transcode_err}); "
                                        f"falling back to source extension {src_ext!r}. "
                                        f"Conversation merge may skip this test if other turns are .mp3.",
                                        extra={"operation": "tts_format_normalize_failed"},
                                    )

                            self._record_artifact(token, "caller", str(dst))
                            return str(dst)
                        except OSError as exc:
                            logger.debug("caller audio copy failed: %s", exc)
                            # Fall through to the text placeholder below.
                            self._record_artifact(token, "caller", result.file_path)
                            return result.file_path
        except Exception as exc:  # noqa: BLE001
            logger.debug("TTS synthesis fallback: %s", exc)
        # Fallback: text file. Real TTS requires a provider key.
        out = self._session_dir / f"caller_{token}.txt"
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        self._record_artifact(token, "caller", str(out))
        return str(out)

    # -- Evaluation ---------------------------------------------------------

    def evaluate_output(
        self, *, response: Any, expected: Any, criteria: list[dict] | None = None,
        harness_runner=None,
        # â”€â”€ Agentic conversational eval context â”€â”€
        # When `persona` + `goal` + `rubric` are populated, the plugin
        # routes to the agentic path (user_simulator + rubric_judge)
        # even without a scripted script in `expected`. This is the
        # primary contract for new multi-turn voice tests.
        persona: Any = None,
        goal: str | None = None,
        constraints: list[str] | None = None,
        rubric: list | None = None,
        max_turns: int = 4,
        evaluation_mode: str = "auto",
        trace_id: str = "no-trace",
        # Candidate agent's system prompt (+ per-test metadata).
        # Mirrors TestCase.input_context. When populated with an
        # `instructions` alias, the rubric judge uses it as ground
        # truth for scope/policy scoring. See _format_agent_instructions_
        # block in rubric_judge.py.
        input_context: dict | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        release_harness_session: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> EvaluationResult:
        """Score the candidate's voice response.

        Three routing decisions from (persona/goal/rubric, expected):

        1. **Agentic multi-turn** (persona + goal + rubric present, OR
           evaluation_mode='agentic'): drives the conversation via
           user_simulator per turn, scores full transcript via
           rubric_judge at the end. No script required. This is the
           DEFAULT PATH for new voice_conversation tests.

        2. **Scripted multi-turn** (`expected` contains a `conversation_script` OR `turns` array): drive_conversation iterates the script, records deterministic evidence signals, and uses semantic review when the production evaluator requires it.

        3. **Single-turn**: if a token (no script, no agentic kit) is
           in expected, looks at captured turns from the in-process HTTP
           server and records deterministic evidence signals.

        Dispatch is data-driven from the schema â€” no case-specific
        if-statements in Agent 5 or the harness template.
        """
        # Agentic path takes priority when persona/goal/rubric are
        # populated. This is intentional: if Agent 3 emitted the
        # agentic kit, that's what the user asked for â€” don't let a
        # legacy `conversation_script` key in `expected` silently
        # sidestep it. The plugin's agentic drive handles the multi-
        # turn voice flow end-to-end.
        # Merge the caller-provided input_context into the expected dict
        # so _evaluate_conversation (and the agent_system_prompt extraction
        # block inside it) sees a single unified source. The caller's
        # kwarg takes precedence over any input_context already in
        # `expected` â€” Agent 5 knows the authoritative TestCase.input_context.
        expected_with_ctx = dict(expected) if isinstance(expected, dict) else {}
        if input_context:
            # Merge existing + override; caller's wins on key conflict.
            merged_ic = dict(expected_with_ctx.get("input_context") or {})
            merged_ic.update(input_context)
            expected_with_ctx["input_context"] = merged_ic

        has_agentic_kit = (
            persona is not None and goal and rubric
        )
        if has_agentic_kit or evaluation_mode == "agentic":
            return self._evaluate_conversation(
                script=_extract_conversation_script(expected) or [],
                expected=expected_with_ctx,
                harness_runner=harness_runner,
                persona=persona,
                goal=goal,
                constraints=constraints or [],
                rubric=rubric or [],
                max_turns=max_turns,
                evaluation_mode=evaluation_mode,
                trace_id=trace_id,
                semantic_review_required=bool(kwargs.get("semantic_review_required")),
                progress_callback=progress_callback,
                release_harness_session=release_harness_session,
            )

        script = _extract_conversation_script(expected)
        if script is not None:
            return self._evaluate_conversation(
                script=script,
                expected=expected_with_ctx,
                harness_runner=harness_runner,
                persona=persona,
                goal=goal,
                constraints=constraints or [],
                rubric=rubric or [],
                max_turns=max_turns,
                evaluation_mode=evaluation_mode,
                trace_id=trace_id,
                semantic_review_required=bool(kwargs.get("semantic_review_required")),
                progress_callback=progress_callback,
                release_harness_session=release_harness_session,
            )

        if not isinstance(expected, dict) or not expected.get("token"):
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning="expected payload missing token",
                fallback_reason="malformed_expected",
            )
        token = str(expected["token"])
        since = response.get("since") if isinstance(response, dict) else None
        turns = self.turns_for_token(token, since=since)
        if not turns:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning=f"no voice turns captured for token {token}",
                detail={"token": token, "captured_count": 0},
            )
        last = turns[-1]
        expected_text = (expected.get("expected_response") or "").strip()
        substr = (expected.get("expected_text_substring") or "").strip().lower()

        agent_text, source = _extract_agent_text(last)

        if not agent_text and last.audio_path:
            agent_text, transcription_note = _try_transcribe(last.audio_path)
            source = source or f"audioâ†’{transcription_note}"

        if not agent_text:
            return EvaluationResult(
                passed=False, score=0.3,
                reasoning=(
                    f"voice turn captured but no recoverable text "
                    f"(source={source or 'none'})"
                ),
                detail={
                    "token": token, "captured_count": len(turns),
                    "source": source, "audio_bytes": last.audio_bytes_len,
                },
            )

        agent_text_lc = agent_text.lower()
        score = 0.5  # captured + extracted text
        bits = [f"text extracted via {source or 'unknown'}"]
        if expected_text and expected_text.lower() in agent_text_lc:
            score += 0.5
            bits.append("expected response substring matched")
        elif substr and substr in agent_text_lc:
            score += 0.4
            bits.append("expected substring matched (partial)")
        elif expected_text or substr:
            bits.append("expected text not found in agent response")
        else:
            score += 0.4  # nothing expected; full credit on receipt+extract
        return EvaluationResult(
            passed=score >= 0.6, score=min(1.0, score),
            reasoning="; ".join(bits),
            detail={
                "token": token, "captured_count": len(turns),
                "source": source,
                "agent_text_preview": agent_text[:200],
                "expected_substring": substr,
            },
        )


def _extract_conversation_script(expected: Any) -> list[dict[str, Any]] | None:
    """Return a normalized N-turn script from the expected payload, or None.

    Accepts several shapes so Agent 3 can emit whichever is natural:
      - ``{"conversation_script": [{"user_text": "...", "expected_agent_contains": "..."}, ...]}``
      - ``{"script": [...]}`` (shorter alias)
      - ``{"turns": [...]}`` (most compact)
      - a raw list of turn dicts

    Returns None when no list-of-dicts with at least one turn is present â€”
    callers fall through to single-turn evaluation. This keeps
    ``evaluate_output`` free of case-specific branches: the presence of a
    script in the data is the only signal that matters.
    """
    if isinstance(expected, dict):
        for key in ("conversation_script", "script", "turns"):
            val = expected.get(key)
            if isinstance(val, list) and val and all(isinstance(t, dict) for t in val):
                return val
        # Some callers stuff the script into ``expected_response`` as a JSON string.
        maybe = expected.get("expected_response")
        if isinstance(maybe, str) and maybe.strip().startswith("["):
            try:
                import json as _json
                parsed = _json.loads(maybe)
                if isinstance(parsed, list) and all(isinstance(t, dict) for t in parsed):
                    return parsed
            except (TypeError, ValueError):
                pass
    if isinstance(expected, list) and expected and all(isinstance(t, dict) for t in expected):
        return expected
    if isinstance(expected, str):
        stripped = expected.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            # Accept BOTH shapes from a JSON-stringified expected_output:
            #   "[{\"user_text\": ...}, ...]"       â€” raw list
            #   "{\"conversation_script\": [...]}"   â€” dict wrapper
            # Real-run signal (trace voice_debug_3): Agent 3 / hand-
            # crafted inputs serialize expected_output as a dict-shaped
            # JSON string because TestCase.expected_output is typed
            # `str`. The old extractor only parsed list-shaped strings,
            # so conversation_script never reached drive_conversation
            # â†’ every turn collapsed to single-turn evaluation â†’ score
            # 0.1 regardless of agent quality. Parse once, then recurse
            # into the standard dict / list branches.
            try:
                import json as _json
                parsed = _json.loads(stripped)
            except (TypeError, ValueError):
                return None
            return _extract_conversation_script(parsed)
    return None


def _wrap_pcm16_as_wav(pcm: bytes, *, sample_rate: int, channels: int) -> bytes:
    """Prepend a RIFF/WAV header to raw PCM16 little-endian audio so the
    result is a playable .wav file.

    Pure stdlib â€” uses ``wave`` module. Sample width is hard-coded to 2
    bytes (16-bit signed LE) which matches every realtime-audio API that
    returns ``audio_format="pcm16"`` (OpenAI Realtime, ElevenLabs Realtime,
    most telephony providers). Callers pass sample_rate + channels from the
    harness's ``audio_sample_rate`` / ``audio_channels`` fields.

    Browsers + ``<audio>`` tags cannot play naked PCM â€” they require the
    container header this function adds. Without this, saved agent audio
    files were silent / unseekable / unplayable.
    """
    import io as _io
    import wave as _wave
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(max(1, int(channels)))
        w.setsampwidth(2)  # 16-bit
        w.setframerate(int(sample_rate))
        w.writeframes(pcm)
    return buf.getvalue()


def _encode_pcm16_to_mp3(pcm: bytes, *, sample_rate: int, channels: int) -> bytes | None:
    """Transcode raw PCM16-LE to MP3 bytes via pydub/ffmpeg. Returns None
    when pydub or ffmpeg isn't available, letting callers fall back to WAV.

    Why MP3 matters: the caller-side TTS produces MP3. If the agent side is
    .wav, the merger refuses mixed extensions and returns None â€” the user
    ends up with a "full call" file that's just caller audio stitched
    together (no agent voice). Encoding agent PCM16 â†’ MP3 here means every
    artifact in voice/ shares the .mp3 extension, the merger concatenates
    cleanly with ID3 stripping, and browsers play the result.

    Pure in-memory pipeline: PCM16 bytes â†’ pydub AudioSegment (raw PCM
    kwargs) â†’ export to mp3 â†’ return bytes. No temp files.
    """
    try:
        from pydub import AudioSegment  # type: ignore
    except Exception:
        return None
    try:
        seg = AudioSegment(
            data=pcm,
            sample_width=2,
            frame_rate=int(sample_rate),
            channels=max(1, int(channels)),
        )
        import io as _io
        out = _io.BytesIO()
        seg.export(out, format="mp3")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.debug("PCM16 â†’ MP3 encode failed: %s", exc)
        return None


def _strip_id3v2_header(body: bytes) -> bytes:
    """Strip an ID3v2 tag from the front of an MP3 byte stream.

    Pure-Python implementation (no mutagen / pydub dependency). The
    tag layout per id3.org/id3v2.4.0-structure:
      bytes 0-2   "ID3"
      byte  3-4   version (major, revision)
      byte  5     flags
      bytes 6-9   synchsafe size â€” each byte's high bit is unused, so
                  the real size is (b6<<21)|(b7<<14)|(b8<<7)|b9.
    Total tag length = 10 + size. If the leading 3 bytes aren't "ID3",
    return the body unchanged.

    Real-run signal (voice_v3 conversation file): 4 MP3 segments
    concat'd byte-wise produced a 290 KB file most players (Windows
    Media Player, QuickTime, default browser <audio>) treated as just
    the first segment because the leading ID3v2 tag declared a 3-second
    duration. Stripping subsequent files' tags + ID3v1 trailers makes
    the concat stream parse as a continuous frame sequence.
    """
    if len(body) < 10 or body[:3] != b"ID3":
        return body
    # Synchsafe-decode the 4-byte size at offset 6..10.
    b = body[6:10]
    # Each byte uses only the low 7 bits; high bit is always 0.
    if any(x & 0x80 for x in b):
        # Malformed â€” leave as-is rather than risk truncating frames.
        return body
    size = (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]
    cut = 10 + size
    if cut >= len(body):
        # Tag claims to be larger than the file â€” refuse to truncate.
        return body
    return body[cut:]


def _strip_id3v1_trailer(body: bytes) -> bytes:
    """Strip an ID3v1 tag (128 bytes at end of file) if present.

    Detection: last 128 bytes start with "TAG". When concatenating
    MP3s, leftover ID3v1 trailers in the middle of the stream confuse
    decoders into stopping early.
    """
    if len(body) >= 128 and body[-128:-125] == b"TAG":
        return body[:-128]
    return body


def _instructions_for_shape(shape: str) -> str:
    if shape == "twilio":
        return (
            "Twilio Voice flow: fetch caller audio from `audio_url` "
            "(or use it as the `Recording` URL in your TwiML <Gather>). "
            "Respond with TwiML XML to `callback_url`. Use <Say> for "
            "synthesized text or <Play> with a URL for audio."
        )
    if shape == "vonage":
        return (
            "Vonage Voice flow: fetch caller audio from `audio_url`. "
            "Respond with NCCO JSON array to `callback_url`. Use a "
            "`talk` action with `text` for TTS or `stream` for audio."
        )
    return (
        "Generic voice flow: fetch the caller's audio at `audio_url` "
        "(WAV/MP3). Respond by POSTing JSON {response_text} to "
        "`callback_url`, OR upload the agent's response audio blob to "
        "`recording_url` (audio/wav, audio/mpeg, etc.)."
    )


# ---------------------------------------------------------------------------
# Response extraction helpers
# ---------------------------------------------------------------------------


_TWIML_TEXT_RE = re.compile(
    r"<\s*(?:Say|Play|Gather|Pause)[^>]*>([^<]*)</\s*(?:Say|Play|Gather|Pause)\s*>",
    re.IGNORECASE,
)


def _extract_agent_text(turn: CapturedVoiceTurn) -> tuple[str, str]:
    """Return (extracted_text, source_label)."""
    if turn.twiml_text:
        matches = _TWIML_TEXT_RE.findall(turn.twiml_text)
        if matches:
            return " ".join(m.strip() for m in matches if m.strip()), "twiml"
        # Some TwiML uses self-closing <Play>URL â€” just return raw stripped
        stripped = re.sub(r"<[^>]+>", " ", turn.twiml_text).strip()
        return stripped, "twiml-raw"
    if turn.ncco_json:
        bits: list[str] = []
        for action in turn.ncco_json:
            if not isinstance(action, dict):
                continue
            if action.get("action") == "talk":
                text = action.get("text") or ""
                if text:
                    bits.append(str(text))
            elif action.get("action") == "stream":
                bits.append(f"[audio_stream:{action.get('streamUrl')}]")
        if bits:
            return " ".join(bits), "ncco"
    if turn.response_json is not None:
        if isinstance(turn.response_json, dict):
            for key in ("response_text", "text", "message", "reply", "content"):
                v = turn.response_json.get(key)
                if isinstance(v, str) and v.strip():
                    return v, f"json.{key}"
        if isinstance(turn.response_json, str) and turn.response_json.strip():
            return turn.response_json, "json-string"
    return "", ""


def _try_transcribe(audio_path: str) -> tuple[str, str]:
    """Best-effort STT via the transcription plugin."""
    try:
        from puzzleeval.tool_plugins import get_plugin
        plugin = get_plugin("transcription")
        if plugin is None:
            return "", "no_transcription_plugin"
        ok, _ = plugin.is_available()
        if not ok:
            return "", "no_transcription_credential"
        # Treat the audio file like a candidate response and reuse the
        # plugin's evaluate_output path purely to extract a transcript;
        # we pass empty expected so it returns the raw text in detail.
        result = plugin.evaluate_output(
            response=audio_path, expected="", criteria=[],
        )
        # Different transcription plugin builds may put the transcript in
        # different keys. We try the common ones.
        if isinstance(result.detail, dict):
            for key in ("transcript", "text", "stt_text", "response_text"):
                v = result.detail.get(key)
                if isinstance(v, str) and v.strip():
                    return v, "transcription_plugin"
        if result.reasoning:
            return result.reasoning, "transcription_reasoning"
    except Exception as exc:  # noqa: BLE001
        logger.debug("voice transcription fallback: %s", exc)
        return "", f"transcription_error:{type(exc).__name__}"
    return "", "transcription_no_text"


# ---------------------------------------------------------------------------
# Module-level instance + registration
# ---------------------------------------------------------------------------


_PLUGIN: VoiceRealtimePlugin | None = None


def get_plugin_instance() -> VoiceRealtimePlugin:
    global _PLUGIN
    if _PLUGIN is None:
        _PLUGIN = VoiceRealtimePlugin()
    return _PLUGIN


register_plugin(get_plugin_instance())


__all__ = [
    "CapturedVoiceTurn",
    "VoiceRealtimePlugin",
    "get_plugin_instance",
]

