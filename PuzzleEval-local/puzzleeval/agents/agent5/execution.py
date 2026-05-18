"""Test execution for Agent 5.

Phase 6.1 — extracts the test-execution pipeline from
``implement_test_env.py`` into a focused module.

Public surface:
  * ``execute_all_tests(harness, test_cases, credentials, ...)`` — the
    top-level entry point, runs every test case for one candidate's
    harness with parallelism + retries + per-test cost tracking.
  * ``execute_single_test(...)`` — runs ONE test case by spawning a
    subprocess against the sandbox venv. Returns ``TestCaseResult``.
  * ``execute_test_with_session_retry(...)`` — wraps single-test exec
    with concurrent-session retry (for plugins that hit per-session
    quota errors).
  * ``run_single_test_with_rate_limit(...)`` — wraps with rate-limit
    backoff.
  * ``compute_aggregate_metrics(test_results)`` — pure aggregator that
    turns per-test results into the candidate-level summary.
  * ``adapt_test_input(test_case, harness)`` — pure adapter.
  * ``inflate_b64_sentinels(obj)`` — JSON border bytes round-trip
    (paired with the encoder in implement_test_env's exec script).
  * ``needs_plugin_synthesis(tc)`` / ``synthesize_test_input_via_plugin(...)``
    — plugin-driven synthesis path for modalities where Agent 3's
    text input doesn't suffice (audio, file uploads, etc.).

AD-007: this module is pure backbone. Markdown contracts don't gate
behavior here.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from puzzleeval.config import (
    AGENT6_ERROR_ABORT_THRESHOLD,
    AGENT6_MIN_TESTS_BEFORE_ABORT,
    AGENT6_RATE_LIMIT_BACKOFF,
    AGENT6_SESSION_MAX_RETRIES,
    AGENT6_SESSION_RETRY_BACKOFF_BASE,
    AGENT6_TEST_TIMEOUT,
    AGENT6_CONVERSATION_TIMEOUT,
)
from puzzleeval.rate_limiter import GlobalProviderLimiter
from puzzleeval.schemas import (
    TestCase,
    TestCaseResult,
    TestHarness,
)


def adapt_test_input(test_case: TestCase, harness: TestHarness) -> dict:
    """Map a test case to the harness.run() input format."""
    return {
        "text": test_case.input_data,
        "input_type": test_case.input_type,
        "input_context": test_case.input_context,
        "test_file_path": test_case.test_file_path,
    }


def inflate_b64_sentinels(obj: Any) -> Any:
    """Walk a JSON-decoded structure and re-inflate `{"_b64": "..."}`
    sentinels (written by `_execute_single_test`'s exec_script) back
    into real ``bytes``. Round-trip partner of the ``_bytes_safe``
    encoder. Operates in place on dicts/lists; pure-Python recursion
    keeps the implementation independent of any third-party encoder.

    Real-run signal (voice_dual_7): a harness that returned raw MP3
    bytes in ``raw_response.audio_bytes`` had those bytes silently
    stringified as ``"b'\\xff\\xfb...'"`` (Python repr) by the prior
    ``json.dump(..., default=str)`` path. The voice plugin's
    `isinstance(audio_bytes, bytes)` check then failed, no agent
    audio was saved, and the merged conversation file ended up
    caller-only. Fix is a transparent two-stage encoder around the
    JSON serialization border.
    """
    if isinstance(obj, dict):
        # Sentinel: a dict with exactly one key "_b64" holding a base64 string.
        if (
            len(obj) == 1
            and "_b64" in obj
            and isinstance(obj["_b64"], str)
        ):
            try:
                import base64 as _b64
                return _b64.b64decode(obj["_b64"], validate=False)
            except (ValueError, TypeError):
                # Malformed sentinel — leave the raw dict in place so
                # the consumer can decide what to do.
                return obj
        return {k: inflate_b64_sentinels(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [inflate_b64_sentinels(v) for v in obj]
    return obj


def _bytes_safe_for_json(obj: Any) -> Any:
    """JSON-border partner for ``inflate_b64_sentinels``.

    Persistent harness sessions exchange one JSON line per turn. Keep the
    same bytes contract as the legacy file-based subprocess path so audio
    payloads and binary provider responses do not degrade to Python repr
    strings.
    """
    if isinstance(obj, (bytes, bytearray)):
        import base64
        return {"_b64": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, dict):
        return {k: _bytes_safe_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bytes_safe_for_json(v) for v in obj]
    if isinstance(obj, tuple):
        return [_bytes_safe_for_json(v) for v in obj]
    return obj


_PERSISTENT_WORKER_SCRIPT = r'''
import base64
import contextlib
import json
import sys
import time
import traceback

_PROTOCOL_STDOUT = sys.stdout


def _write(obj):
    _PROTOCOL_STDOUT.write(json.dumps(obj, default=str) + "\n")
    _PROTOCOL_STDOUT.flush()


def _bytes_safe(obj):
    if isinstance(obj, (bytes, bytearray)):
        return {"_b64": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, dict):
        return {k: _bytes_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bytes_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_bytes_safe(v) for v in obj]
    return obj


def _inflate(obj):
    if isinstance(obj, dict):
        if len(obj) == 1 and isinstance(obj.get("_b64"), str):
            try:
                return base64.b64decode(obj["_b64"], validate=False)
            except Exception:
                return obj
        return {k: _inflate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_inflate(v) for v in obj]
    return obj


def _cleanup_state(state):
    closed = 0
    for value in list(state.values()):
        close = getattr(value, "close", None)
        if callable(close):
            try:
                close()
                closed += 1
            except Exception:
                traceback.print_exc(file=sys.stderr)
    return closed


try:
    sys.path.insert(0, ".")
    with contextlib.redirect_stdout(sys.stderr):
        import harness
except Exception as exc:
    _write({
        "type": "error",
        "error_type": "import_error",
        "error": str(exc),
        "traceback": traceback.format_exc(limit=8),
    })
    sys.exit(2)


session_state = {}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except Exception as exc:
        _write({"type": "error", "error_type": "bad_command", "error": str(exc)})
        continue

    cmd_type = cmd.get("type")
    if cmd_type in ("close", "shutdown"):
        closed = _cleanup_state(session_state)
        _write({"type": "closed", "closed_resources": closed})
        break

    if cmd_type != "turn":
        _write({
            "type": "error",
            "error_type": "unknown_command",
            "error": str(cmd_type),
        })
        continue

    payload = _inflate(cmd.get("payload") or {})
    if not isinstance(payload, dict):
        payload = {"payload": payload}

    incoming_state = payload.get("session_state")
    if isinstance(incoming_state, dict):
        session_state.update(incoming_state)
    payload["session_state"] = session_state

    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = harness.run(payload)
        latency_ms = (time.perf_counter() - started) * 1000.0
        if isinstance(result, dict):
            new_state = result.get("session_state")
            if isinstance(new_state, dict):
                session_state.update(new_state)
            result_for_parent = dict(result)
            # In persistent mode, session_state is worker-owned. Returning
            # it would stringify non-serializable provider handles such as
            # WebSockets and then poison the parent-side state dict.
            result_for_parent.pop("session_state", None)
            result_for_parent.setdefault("latency_ms", latency_ms)
        else:
            result_for_parent = {
                "success": False,
                "output": "",
                "latency_ms": latency_ms,
                "raw_response": {},
                "error": "harness.run returned non-dict result",
            }
        _write({"type": "result", "result": _bytes_safe(result_for_parent)})
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        _write({
            "type": "result",
            "result": {
                "success": False,
                "output": "",
                "latency_ms": latency_ms,
                "tokens_used": None,
                "cost_usd": None,
                "raw_response": {"error_type": type(exc).__name__},
                "error": str(exc),
            },
        })
'''


def _sandbox_python_executable(sandbox_dir: Path) -> str:
    """Prefer the sandbox venv interpreter; fall back to current Python."""
    if sys.platform == "win32":
        candidate = sandbox_dir / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = sandbox_dir / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


class PersistentHarnessSession:
    """One long-lived harness subprocess for one multi-turn conversation."""

    def __init__(
        self,
        sandbox_dir: Path,
        credentials: dict[str, str] | None,
        *,
        turn_timeout: int,
        conversation_timeout: int = AGENT6_CONVERSATION_TIMEOUT,
        logger: logging.Logger | None = None,
        trace_id: str = "no-trace",
        candidate_name: str = "",
    ) -> None:
        self.sandbox_dir = sandbox_dir
        self.credentials = credentials
        self.turn_timeout = turn_timeout
        self.conversation_timeout = conversation_timeout
        self.logger = logger or logging.getLogger(__name__)
        self.trace_id = trace_id
        self.candidate_name = candidate_name
        self.started_at = time.monotonic()
        self._lock = threading.Lock()
        self._stdout_queue: "queue.Queue[str]" = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._closed = False
        self._proc = self._start_worker()

    def _start_worker(self) -> subprocess.Popen:
        from puzzleeval.agents.agent5.sandbox import build_sandbox_env

        env = build_sandbox_env(self.sandbox_dir, self.credentials)
        proc = subprocess.Popen(
            [_sandbox_python_executable(self.sandbox_dir), "-u", "-c", _PERSISTENT_WORKER_SCRIPT],
            cwd=str(self.sandbox_dir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(
            target=self._read_stdout,
            args=(proc,),
            name=f"puzzleeval-persistent-stdout-{self.candidate_name[:16]}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(proc,),
            name=f"puzzleeval-persistent-stderr-{self.candidate_name[:16]}",
            daemon=True,
        ).start()
        return proc

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self._stdout_queue.put(line)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip())

    def _stderr_excerpt(self) -> str:
        return "\n".join(self._stderr_tail)[-1200:]

    def _remaining_conversation_seconds(self) -> float:
        return max(0.0, self.conversation_timeout - (time.monotonic() - self.started_at))

    def _failure(self, error: str, error_type: str) -> dict:
        return {
            "output": "",
            "latency_ms": 0.0,
            "tokens_used": None,
            "cost_usd": None,
            "raw_response": {"error_type": error_type},
            "success": False,
            "error": error,
        }

    def turn(self, payload: dict) -> dict:
        """Execute one turn inside the persistent worker."""
        with self._lock:
            if self._closed:
                return self._failure("persistent harness worker is closed", "worker_closed")
            remaining = self._remaining_conversation_seconds()
            if remaining <= 0:
                self.kill()
                return self._failure(
                    f"Persistent harness conversation timed out after {self.conversation_timeout}s",
                    "conversation_timeout",
                )
            if self._proc.poll() is not None:
                return self._failure(
                    f"Persistent harness worker died before turn; stderr: {self._stderr_excerpt()}",
                    "worker_died",
                )

            command = {
                "type": "turn",
                "payload": _bytes_safe_for_json(payload),
            }
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(command, default=str) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                return self._failure(
                    f"Persistent harness worker pipe broke: {exc}; stderr: {self._stderr_excerpt()}",
                    "worker_died",
                )

            deadline = max(0.1, min(float(self.turn_timeout), remaining))
            end_at = time.monotonic() + deadline
            while True:
                wait = max(0.0, end_at - time.monotonic())
                if wait <= 0:
                    self.kill()
                    return self._failure(
                        f"Persistent harness worker timed out after {deadline:.1f}s",
                        "worker_timeout",
                    )
                try:
                    line = self._stdout_queue.get(timeout=min(wait, 0.2))
                except queue.Empty:
                    if self._proc.poll() is not None:
                        return self._failure(
                            f"Persistent harness worker died during turn; stderr: {self._stderr_excerpt()}",
                            "worker_died",
                        )
                    continue
                if self._proc.poll() is not None and not line:
                    return self._failure(
                        f"Persistent harness worker died during turn; stderr: {self._stderr_excerpt()}",
                        "worker_died",
                    )
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._stderr_tail.append(f"[stdout non-json] {line.rstrip()}")
                    continue
                if message.get("type") == "result":
                    result = inflate_b64_sentinels(message.get("result") or {})
                    if isinstance(result, dict):
                        return result
                    return self._failure("Persistent worker returned non-dict result", "bad_result")
                return self._failure(
                    f"Persistent harness worker error: {message.get('error')}; stderr: {self._stderr_excerpt()}",
                    str(message.get("error_type") or "worker_error"),
                )

    def close(self) -> None:
        """Ask the worker to close resources and exit; kill on non-response."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._proc.poll() is None:
                try:
                    assert self._proc.stdin is not None
                    self._proc.stdin.write(json.dumps({"type": "close"}) + "\n")
                    self._proc.stdin.flush()
                    try:
                        self._stdout_queue.get(timeout=5)
                    except queue.Empty:
                        pass
                    self._proc.wait(timeout=5)
                except Exception:
                    self.kill()

    def kill(self) -> None:
        self._closed = True
        if self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def __enter__(self) -> "PersistentHarnessSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def execute_single_test(
    sandbox_dir: Path,
    adapted_input: dict,
    credentials: dict[str, str] | None,
    timeout: int,
) -> dict:
    """
    Execute one test case via subprocess in the harness's venv.
    Returns the harness result dict or an error dict. Never raises.

    NEW-AM v6 — RACE FIX (real-run trace a4860e94, 2026-04-25):
    Per-call unique input/output filenames eliminate the cross-test
    contamination caused by parallel test executions sharing
    `_test_input.json` + `_test_output.json` in the candidate's sandbox.

    Pre-fix evidence: tc-001 (persona=James Whitfield) transcript showed
    agent saying "Maria" + tc-002's phone number 555-918-4422. The pre-
    fix used SHARED filenames `_test_input.json` and `_test_output.json`
    in `sandbox_dir`. With AGENT6_PER_CANDIDATE_SESSION_PARALLELISM=6,
    multiple test workers raced on the same files:

      Worker tc-001: writes `{persona: James, audio_url: <James audio>}`
      Worker tc-002: writes `{persona: Maria, audio_url: <Maria audio>}`
                     (overwrites tc-001's input)
      Subprocess for tc-001: reads `_test_input.json` → gets MARIA's
                              payload → sends Maria's audio to provider
      Result returns to tc-001's runner → transcript shows Maria's
      content attributed to tc-001

    Post-fix: each call gets a unique filename via
    `secrets.token_hex(8)`. Subprocesses read/write per-call paths
    via env vars `PUZZLEEVAL_TEST_INPUT_PATH` / `PUZZLEEVAL_TEST_
    OUTPUT_PATH` (cleaner than positional args; tolerated by harnesses
    that don't read them).
    """

    # Phase 6 lazy imports — implement_test_env helpers we depend on.
    from puzzleeval.agents.implement_test_env import (
        _build_sandbox_env,
    )

    import secrets
    error_result = {
        "output": "",
        "latency_ms": 0.0,
        "tokens_used": None,
        "cost_usd": None,
        "raw_response": {},
        "success": False,
        "error": None,
    }

    # Per-call unique filenames — race-safe under parallel execution.
    call_id = secrets.token_hex(8)
    input_filename = f"_test_input_{call_id}.json"
    output_filename = f"_test_output_{call_id}.json"
    input_path = sandbox_dir / input_filename
    output_path = sandbox_dir / output_filename

    try:
        input_path.write_text(json.dumps(adapted_input), encoding="utf-8")
    except Exception as e:
        return {**error_result, "error": f"Failed to write test input: {e}"}

    # Bytes-safe round-trip: harnesses can put raw `bytes` (audio MP3,
    # binary blobs) into raw_response. JSON can't carry bytes natively;
    # the previous `default=str` path stringified them as Python repr
    # (`"b'\\xff\\xfb...'"`) which the plugin couldn't decode → voice
    # harnesses' agent audio was silently lost (only caller audio
    # survived; the "merged conversation" file ended up caller-only).
    # Real-run signal: voice_dual_7 produced 5 caller MP3s + 1 merged
    # MP3 that was actually 5 caller voices stitched together with NO
    # agent audio.
    #
    # General fix: walk the result before dumping; replace every bytes
    # value with the sentinel `{"_b64": "<base64-utf8>"}`. Caller side
    # walks the loaded JSON and re-inflates sentinels back to bytes.
    # Transparent to harnesses (they keep returning bytes) and to the
    # plugin (it gets bytes back). Belt-and-braces: the plugin now also
    # accepts the b64 string directly, so older harnesses that
    # base64-encoded themselves still work.
    #
    # NEW-AM v6: filenames are now templated via the `_INPUT_FILE_NAME`
    # / `_OUTPUT_FILE_NAME` placeholders so each subprocess reads/writes
    # its own per-call file (eliminates the parallel-test race on
    # _test_input.json that bled tc-002's persona into tc-001).
    exec_script = (
        'import sys, json, base64\n'
        'sys.path.insert(0, ".")\n'
        'import harness\n'
        '\n'
        'def _bytes_safe(obj):\n'
        '    if isinstance(obj, (bytes, bytearray)):\n'
        '        return {"_b64": base64.b64encode(bytes(obj)).decode("ascii")}\n'
        '    if isinstance(obj, dict):\n'
        '        return {k: _bytes_safe(v) for k, v in obj.items()}\n'
        '    if isinstance(obj, list):\n'
        '        return [_bytes_safe(v) for v in obj]\n'
        '    if isinstance(obj, tuple):\n'
        '        return [_bytes_safe(v) for v in obj]\n'
        '    return obj\n'
        '\n'
        'input_data = json.loads(open("' + input_filename + '", encoding="utf-8").read())\n'
        'result = harness.run(input_data)\n'
        'safe = _bytes_safe(result)\n'
        'with open("' + output_filename + '", "w", encoding="utf-8") as f:\n'
        '    json.dump(safe, f, default=str)\n'
    )

    env = _build_sandbox_env(sandbox_dir, credentials)

    try:
        if output_path.exists():
            output_path.unlink()

        proc = subprocess.run(
            [_sandbox_python_executable(sandbox_dir), "-c", exec_script],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(sandbox_dir),
            env=env,
        )

        if output_path.exists():
            try:
                result = json.loads(output_path.read_text(encoding="utf-8"))
                # Inverse of `_bytes_safe` in the exec_script: walk the
                # JSON tree and re-inflate `{"_b64": "..."}` sentinels
                # back to real bytes so downstream consumers (the voice
                # plugin's audio writer, anything else that needs raw
                # bytes) get the data in its native shape.
                result = inflate_b64_sentinels(result)
                return {
                    "output": str(result.get("output", "")),
                    "latency_ms": float(result.get("latency_ms", 0.0)),
                    "tokens_used": result.get("tokens_used"),
                    "cost_usd": result.get("cost_usd"),
                    "raw_response": result.get("raw_response", {}),
                    "success": bool(result.get("success", False)),
                    "error": result.get("error"),
                }
            except (json.JSONDecodeError, Exception) as e:
                return {**error_result, "error": f"Failed to parse harness output: {e}"}

        stderr = (proc.stderr or "")[:500]
        return {**error_result, "error": f"Harness crashed: {stderr}"}

    except subprocess.TimeoutExpired:
        return {**error_result, "error": f"Test timed out after {timeout}s"}
    except Exception as e:
        return {**error_result, "error": f"Execution error: {e}"}
    finally:
        for p in (input_path, output_path):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def execute_test_with_session_retry(
    sandbox_dir: Path,
    payload: dict,
    credentials: dict[str, str] | None,
    timeout: int,
    logger: logging.Logger,
    trace_id: str,
    candidate_name: str,
) -> dict:
    """Execute a single harness call, retrying on concurrent-session-cap
    errors with exponential backoff.

    The problem this solves: when per-candidate session parallelism
    (default 3) exceeds a provider's concurrent-session cap (free tier
    often = 1), some harness calls fail with "maximum concurrent
    sessions exceeded" or similar. The right fallback is to WAIT for
    a prior session to release, not to mark the test as errored.

    Semantics:
      - First call: immediate, no wait
      - On success or non-session error: return immediately (no retry)
      - On session-cap error: sleep exponential(attempt) + retry, up to
        AGENT6_SESSION_MAX_RETRIES times
      - Final failure after N retries: return the last error result

    Thread safety: no shared state — safe to call from ThreadPoolExecutor
    workers. Each concurrent worker that hits the cap will independently
    back off and retry, which naturally serializes multiple concurrent
    offenders through the provider's session slot availability.

    Distinct from RATE_LIMIT retry (faster backoff, different reset
    timescale) — session caps release on conversation-completion
    timescales while rate limits reset in 1-60 seconds.
    """

    # Phase 6 lazy imports — implement_test_env helpers we depend on.
    from puzzleeval.agents.implement_test_env import _is_concurrent_session_error

    result = execute_single_test(sandbox_dir, payload, credentials, timeout)

    for attempt in range(1, AGENT6_SESSION_MAX_RETRIES + 1):
        if result.get("success"):
            return result
        error_text = result.get("error")
        if not _is_concurrent_session_error(error_text):
            # Non-session errors (auth failure, timeout, schema issue)
            # are handled elsewhere. Don't retry them here.
            return result

        wait_time = AGENT6_SESSION_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
        logger.info(
            f"concurrent session cap hit for {candidate_name}, "
            f"attempt {attempt}/{AGENT6_SESSION_MAX_RETRIES}, "
            f"waiting {wait_time}s for a slot to free up",
            extra={
                "operation": "session_cap_retry",
                "trace_id": trace_id,
                "candidate_name": candidate_name,
                "attempt": attempt,
                "wait_seconds": wait_time,
                "error_fragment": str(error_text)[:200] if error_text else "",
            },
        )
        time.sleep(wait_time)
        result = execute_single_test(sandbox_dir, payload, credentials, timeout)

    # After all retries, if still failing with session error, log + return
    if not result.get("success") and _is_concurrent_session_error(result.get("error")):
        logger.warning(
            f"concurrent session cap still blocking after "
            f"{AGENT6_SESSION_MAX_RETRIES} retries for {candidate_name}; "
            f"accepting the failure",
            extra={
                "operation": "session_cap_retry_exhausted",
                "trace_id": trace_id,
                "candidate_name": candidate_name,
            },
        )
    return result


def run_single_test_with_rate_limit(
    tc: TestCase,
    sandbox_dir: Path,
    harness: TestHarness,
    credentials: dict[str, str] | None,
    logger: logging.Logger,
    trace_id: str,
    rate_limiter: "GlobalProviderLimiter | None",
    upstream_provider: str | None,
) -> dict:
    """Execute a single single-turn test case with rate-limit discipline.

    Extracted from the original sequential loop body so both the
    sequential and parallel paths share the same rate-limit +
    rate-limit-retry semantics. Returns the harness result dict
    (same shape as ``_execute_single_test``).

    Thread-safety: this function has no shared mutable state. The
    rate_limiter has its own internal lock (per GlobalProviderLimiter
    contract). Subprocess spawning via ``_execute_single_test`` is
    thread-safe — each test gets its own subprocess PID.
    """

    # Phase 6 lazy imports — implement_test_env helpers we depend on.
    from puzzleeval.agents.implement_test_env import (
        _adaptive_test_timeout,
        _is_rate_limit_error,
    )

    adapted = adapt_test_input(tc, harness)

    # Gap 13 + 25: pace calls to respect per-candidate + upstream limits
    if rate_limiter is not None:
        waited = rate_limiter.acquire(harness.candidate_name, upstream_provider)
        if waited > 0.5:
            logger.info(
                f"rate_limiter waited {waited:.2f}s for {harness.candidate_name}",
                extra={
                    "operation": "rate_limit_wait",
                    "trace_id": trace_id,
                    "candidate_name": harness.candidate_name,
                    "wait_seconds": waited,
                    "upstream_provider": upstream_provider,
                },
            )

    # Session-retry wrapper handles concurrent-session-cap errors
    # (distinct from rate limits; different backoff tuning). If the
    # cap isn't hit, this is a pass-through single call.
    result = execute_test_with_session_retry(
        sandbox_dir, adapted, credentials,
        _adaptive_test_timeout(harness),
        logger, trace_id, harness.candidate_name,
    )

    if not result["success"] and _is_rate_limit_error(result.get("error")):
        logger.info(
            f"Rate limit hit for {harness.candidate_name}, backing off {AGENT6_RATE_LIMIT_BACKOFF}s",
            extra={"operation": "rate_limit_backoff", "trace_id": trace_id},
        )
        time.sleep(AGENT6_RATE_LIMIT_BACKOFF)
        if rate_limiter is not None:
            rate_limiter.acquire(harness.candidate_name, upstream_provider)
        result = execute_test_with_session_retry(
            sandbox_dir, adapted, credentials, AGENT6_TEST_TIMEOUT,
            logger, trace_id, harness.candidate_name,
        )

    return result


def execute_all_tests(
    sandbox_dir: Path,
    test_cases: list[TestCase],
    harness: TestHarness,
    credentials: dict[str, str] | None,
    logger: logging.Logger,
    trace_id: str,
    rate_limiter: "GlobalProviderLimiter | None" = None,
    upstream_provider: str | None = None,
) -> list[tuple[TestCase, dict]]:
    """
    Execute all test cases in randomized order with rate limiting.
    Returns list of (test_case, harness_result) tuples.
    Early aborts if error rate exceeds threshold.

    Within a single candidate, tests are partitioned by modality:
      - Multi-turn modalities (conversation, voice_conversation,
        voice_turn) run SEQUENTIALLY. Each is owned by a plugin's
        drive-loop with session_state that must progress monotonically.
      - Single-turn modalities (everything else) run in PARALLEL via
        ThreadPoolExecutor with max_workers =
        AGENT6_PER_CANDIDATE_PARALLELISM (default 3). This cuts wall-
        clock 2-3× on OCR / chatbot / classification evals without
        requiring any cloud-migration redesign — it's the same
        thread-in-container pattern that translates directly to
        Docker / Cloud Run / managed-sandbox deployments.

    When ``rate_limiter`` is provided (Gap 13/25), each test call waits on
    the per-candidate bucket AND the per-upstream bucket before firing,
    even in the parallel path. The limiter is its own pacing layer — the
    parallelism knob is just the concurrency ceiling.
    """
    shuffled = list(test_cases)
    random.shuffle(shuffled)

    # Multi-call modalities (voice_conversation, voice_turn, conversation)
    # are owned by a plugin's drive-loop (voice_realtime / conversation_
    # simulator). Pre-calling harness.run() once with the test's bare
    # adapted payload is wasteful AND actively harmful: the harness has
    # no audio_url / turn_index / session_state until the plugin's
    # responder builds them per turn, so strict harnesses correctly
    # return success=False on the pre-call ("missing audio_url"), which
    # records as a real test error and skips the entire plugin path.
    # voice_dual_6 caught exactly this: the lenient OpenAI harness
    # tolerated the bogus pre-call and got 6 audio artifacts via the
    # plugin; the strict ElevenLabs harness rejected it and ended up
    # with 0 audio_paths + 0 evaluation. Skip the pre-call for these
    # modalities and let the plugin own every harness invocation —
    # the REAL work for multi-turn tests happens later in the
    # evaluation phase (plugin.evaluate_output → drive_conversation).
    multi_call_input_types = {"conversation", "voice_conversation", "voice_turn"}
    multi_call_output_types = {"voice_conversation", "voice_turn"}

    def _is_multi_call(_tc: "TestCase") -> bool:
        return (
            (_tc.input_type or "") in multi_call_input_types
            or (_tc.output_type or "") in multi_call_output_types
        )

    # ──────────────────────────────────────────────────────────────────
    # Phase A: instant placeholders for multi-turn tests. No network
    # work happens here — the plugin's drive_conversation runs during
    # evaluation. Still index these by their original shuffle position
    # so we can stitch results in order at the end.
    # ──────────────────────────────────────────────────────────────────
    results_by_index: dict[int, tuple[TestCase, dict]] = {}
    for i, tc in enumerate(shuffled):
        if _is_multi_call(tc):
            placeholder = {
                "output": "",
                "latency_ms": 0.0,
                "tokens_used": None,
                "cost_usd": None,
                "raw_response": {
                    "_skipped_pre_call_for_multi_call_modality": True,
                    "input_type": tc.input_type,
                    "output_type": tc.output_type,
                },
                "success": True,
                "error": None,
            }
            logger.info(
                f"skipping pre-call for {harness.candidate_name} on {tc.id} "
                f"(multi-call modality {tc.input_type}/{tc.output_type}); "
                f"plugin owns the loop",
                extra={
                    "operation": "multi_call_pre_call_skipped",
                    "trace_id": trace_id,
                    "candidate_name": harness.candidate_name,
                    "test_case_id": tc.id,
                    "input_type": tc.input_type,
                    "output_type": tc.output_type,
                },
            )
            results_by_index[i] = (tc, placeholder)

    # Shared mutable state across parallel workers. The lock guards
    # results_by_index + error_state so parallel threads don't race
    # on ordering or early-abort signaling.
    state_lock = threading.Lock()
    error_state = {"count": 0, "aborted": False}

    def _run_one_single_turn(i: int, tc: TestCase) -> None:
        """Worker body for one single-turn test case.

        Runs harness with rate-limit + rate-limit-retry, records the
        result in ``results_by_index[i]``. Updates error count + flips
        the abort flag if the error-rate threshold is crossed.

        Safe to call from a ThreadPoolExecutor worker: the rate_limiter
        has internal locking, _execute_single_test is subprocess-based
        (no shared Python state), and all mutation of closure state
        happens under ``state_lock``.
        """
        # Check abort flag before starting expensive work
        with state_lock:
            if error_state["aborted"]:
                return

        result = run_single_test_with_rate_limit(
            tc, sandbox_dir, harness, credentials, logger, trace_id,
            rate_limiter, upstream_provider,
        )

        with state_lock:
            results_by_index[i] = (tc, result)

            error_msg = result.get("error") or ""
            is_incompatible = (
                not result["success"]
                and "INCOMPATIBLE" in error_msg.upper()
            )
            if not result["success"] and not is_incompatible:
                error_state["count"] += 1

            # Early-abort check: scanned across ALL tests completed so
            # far (including multi-turn placeholders that "succeeded"
            # trivially). The original sequential code used (i + 1) but
            # with parallel completion order that's no longer stable.
            # Using total_done = len(results_by_index) matches the
            # intent ("fraction of tests seen so far that errored").
            total_done = len(results_by_index)
            if (
                total_done >= AGENT6_MIN_TESTS_BEFORE_ABORT
                and error_state["count"] / total_done > AGENT6_ERROR_ABORT_THRESHOLD
            ):
                if not error_state["aborted"]:
                    logger.warning(
                        f"Early abort for {harness.candidate_name}: "
                        f"{error_state['count']}/{total_done} errors "
                        f"({error_state['count']/total_done:.0%})",
                        extra={
                            "operation": "early_abort",
                            "trace_id": trace_id,
                            "candidate_name": harness.candidate_name,
                            "error_rate": error_state["count"] / total_done,
                        },
                    )
                error_state["aborted"] = True

    # ──────────────────────────────────────────────────────────────────
    # Phase B: single-turn tests run in parallel via ThreadPoolExecutor.
    # Multi-turn tests are already placeholder'd above; this phase only
    # runs harness.run() for test cases that need a real API call at
    # this stage. Rate limiter still paces within-bucket + per-upstream
    # — parallelism is just the concurrency ceiling.
    # ──────────────────────────────────────────────────────────────────
    single_turn_work = [
        (i, tc) for i, tc in enumerate(shuffled) if not _is_multi_call(tc)
    ]

    from puzzleeval.config import AGENT6_PER_CANDIDATE_PARALLELISM
    parallelism = max(1, AGENT6_PER_CANDIDATE_PARALLELISM)

    if parallelism == 1 or len(single_turn_work) <= 1:
        # Sequential fallback — preserves legacy behavior when the
        # knob is flipped to 1, and avoids ThreadPoolExecutor setup
        # cost when there's nothing to parallelize.
        for i, tc in single_turn_work:
            _run_one_single_turn(i, tc)
            # Only apply the blind 0.5s sleep when no smart limiter
            # is active AND we're in the legacy sequential path.
            if rate_limiter is None:
                time.sleep(0.5)
    else:
        workers = min(parallelism, len(single_turn_work))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"puzzleeval-tests-{harness.candidate_name[:20]}",
        ) as executor:
            # Submit all; as_completed to react to errors immediately
            futures = [
                executor.submit(_run_one_single_turn, i, tc)
                for i, tc in single_turn_work
            ]
            for fut in as_completed(futures):
                # Propagate unexpected exceptions (not normal harness
                # failures — those land in result["error"] without
                # raising). This matches the sequential path, which
                # had no try/except around _execute_single_test.
                fut.result()

    # Return results in the original shuffled order so downstream
    # iteration is deterministic per shuffle (same order reporting,
    # cleaner eval logs).
    return [
        results_by_index[i]
        for i in range(len(shuffled))
        if i in results_by_index
    ]


def compute_aggregate_metrics(
    test_results: list[TestCaseResult],
) -> dict[str, Any]:
    """Compute aggregate metrics from individual test results."""
    total = len(test_results)
    if total == 0:
        return {
            "total_tests": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "tests_errored": 0,
            "tests_skipped": 0,
            "success_rate": 0.0,
            "pass_rate": 0.0,
            "overall_score": 0.0,
            "avg_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "total_cost_usd": 0.0,
            "total_tokens": None,
        }

    # Skipped = INCOMPATIBLE or has skip_reason
    tests_skipped = sum(
        1 for r in test_results
        if getattr(r, "skip_reason", None) is not None
    )
    tests_errored = sum(
        1 for r in test_results
        if not r.success and getattr(r, "skip_reason", None) is None
    )
    tests_passed = sum(1 for r in test_results if r.passed)
    tests_failed = total - tests_skipped - tests_errored - tests_passed

    successful_results = [r for r in test_results if r.success]
    latencies = sorted([r.latency_ms for r in successful_results]) if successful_results else [0.0]

    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    p95_idx = max(0, int(len(latencies) * 0.95) - 1)
    p95_latency = latencies[p95_idx] if latencies else 0.0

    total_cost = sum(r.cost_usd or 0.0 for r in test_results)

    total_input_tokens = 0
    total_output_tokens = 0
    has_token_data = False
    for r in test_results:
        if r.tokens_used:
            has_token_data = True
            total_input_tokens += r.tokens_used.get("input", 0)
            total_output_tokens += r.tokens_used.get("output", 0)

    # Compute rates from executed tests only (excluding skipped)
    executed_count = total - tests_skipped
    successful_count = executed_count - tests_errored

    # OBSERVABILITY FIX (real run 3eb3196a, 2026-04-22): overall_score was
    # computed inline in the SSE callback (`sum(weighted_score) / len`) but
    # never persisted to the schema. Frontend reads `overall_score` from
    # the persisted report → always 0.0 on refresh/reopen. Compute and
    # include here so both live and persisted paths see the same value.
    # Mean of weighted_score across all tests (including failed/errored,
    # which contribute 0). Range [0.0, 1.0]. `pass_rate` is binary-per-
    # test; `overall_score` is degree-of-correctness across criteria.
    overall_score = (
        sum(getattr(r, "weighted_score", 0.0) or 0.0 for r in test_results) / total
        if total > 0 else 0.0
    )

    return {
        "total_tests": total,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "tests_errored": tests_errored,
        "tests_skipped": tests_skipped,
        "success_rate": successful_count / executed_count if executed_count > 0 else 0.0,
        "pass_rate": tests_passed / successful_count if successful_count > 0 else 0.0,
        "overall_score": round(overall_score, 4),
        "avg_latency_ms": round(avg_latency, 2),
        "p95_latency_ms": round(p95_latency, 2),
        "total_cost_usd": round(total_cost, 6),
        "total_tokens": (
            {"input": total_input_tokens, "output": total_output_tokens}
            if has_token_data
            else None
        ),
    }


def needs_plugin_synthesis(tc: TestCase) -> bool:
    """Test cases for special modalities that didn't ship with file/payload
    can be filled in by the plugin synthesizers (TTS for audio,
    conversation_simulator for chat scripts, code_execution for code seeds)."""
    if tc.test_file_path:
        return False
    if tc.input_data and tc.input_type not in {"audio_content"}:
        # When input_data is already populated, plugin synthesis is only
        # needed for audio (synthesize a real audio file from the text).
        return False
    return tc.input_type in {"audio_content", "conversation", "code"}


def synthesize_test_input_via_plugin(
    tc: TestCase,
    sandbox_dir: Path,
    logger: logging.Logger,
    trace_id: str,
) -> TestCase:
    """Try to synthesize this test case's input via a registered plugin.

    Returns the test case unchanged when no plugin is available or the
    synthesis failed — callers handle missing input downstream
    (e.g., file_required tests already have the Gap 3 fallback).
    """
    from puzzleeval.tool_plugins import find_plugins_for_input_type
    candidates_plugins = [
        p for p in find_plugins_for_input_type(tc.input_type)
        if p.capabilities().synthesizes_input
    ]
    if not candidates_plugins:
        return tc
    # Prefer TTS for audio_content, conversation_simulator for conversation,
    # code_execution for code. Take the first available.
    plugin = None
    for p in candidates_plugins:
        ok, _ = p.is_available()
        if ok:
            plugin = p
            break
    if plugin is None:
        logger.info(
            f"No available synthesizer plugin for {tc.input_type} on {tc.id}",
            extra={"operation": "synthesis_unavailable", "trace_id": trace_id,
                   "candidates": [p.name for p in candidates_plugins]},
        )
        return tc
    # Plugins that produce audio artifacts write to a caller-supplied
    # session dir. Default is %TEMP%, which leaks audio files outside
    # the run. Point them into the sandbox's voice/ subdir so every
    # artifact for this run lives under runs/<trace_id>/harnesses/<slug>/.
    # Cloud-scale seam: swap the voice/ Path for a StorageBackend shim
    # (S3/GCS) and every plugin automatically persists to cloud storage.
    if hasattr(plugin, "set_session_dir"):
        try:
            plugin.set_session_dir(sandbox_dir / "voice")
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"{plugin.name} set_session_dir failed: {exc}",
                extra={"operation": "plugin_session_dir_failed", "trace_id": trace_id},
            )
    try:
        result = plugin.synthesize_input(
            scope_role=tc.sub_task_ref,
            ground_truth_hint=tc.input_data or None,
        )
    except Exception as exc:
        logger.warning(
            f"Plugin {plugin.name} synthesis failed for {tc.id}: {exc}",
            extra={"operation": "synthesis_crash", "trace_id": trace_id},
        )
        return tc
    tc_dict = tc.model_dump()
    if result.file_path:
        tc_dict["test_file_path"] = result.file_path
        # Promote the synthesized file's expected text into expected_output
        # when the caller didn't already set one.
        if not tc.expected_output and "text" in result.ground_truth:
            tc_dict["expected_output"] = result.ground_truth["text"]
    if result.inline_data:
        # Inline payloads (conversation scripts, code prompts) flow through
        # input_data so the harness sees them. We serialize as JSON for
        # transport — the harness will decode based on input_type.
        import json as _json
        try:
            tc_dict["input_data"] = _json.dumps(result.inline_data)
        except (TypeError, ValueError):
            pass
    logger.info(
        f"Plugin {plugin.name} synthesized input for {tc.id}",
        extra={"operation": "synthesis_complete", "trace_id": trace_id,
               "plugin": plugin.name, "file_path": result.file_path,
               "has_inline": bool(result.inline_data)},
    )
    return TestCase(**tc_dict)


__all__ = [
    "adapt_test_input",
    "compute_aggregate_metrics",
    "execute_all_tests",
    "execute_single_test",
    "execute_test_with_session_retry",
    "inflate_b64_sentinels",
    "needs_plugin_synthesis",
    "PersistentHarnessSession",
    "run_single_test_with_rate_limit",
    "synthesize_test_input_via_plugin",
]
