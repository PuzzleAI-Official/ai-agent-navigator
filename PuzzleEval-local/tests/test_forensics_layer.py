"""Tests for the harness forensics layer.

Covers:
  * The `_forensics.py` shim that gets auto-injected into every sandbox:
    log() + traced_op() public API; credential redaction; rate limiting;
    JSONL persistence; thread error capture; stack-dump-on-hang config.
  * The `stage_forensics_shim()` helper in agent5/sandbox.py.
  * The `verify_forensics_coverage()` semantic AST gate in
    agent5/verification.py.

Per AD-007: prompts teach, code enforces. The forensics layer is the
code enforcement of the OBSERVABILITY CONTRACT taught in the builder
system prompt.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shim staging
# ---------------------------------------------------------------------------


class TestStageForensicsShim:
    def test_writes_forensics_py_into_sandbox_root(self):
        from puzzleeval.agents.agent5.sandbox import (
            stage_forensics_shim,
            FORENSICS_SHIM_CONTENT,
        )
        with tempfile.TemporaryDirectory() as d:
            sb = Path(d)
            ok = stage_forensics_shim(sb)
            assert ok is True
            target = sb / "_forensics.py"
            assert target.exists()
            assert target.read_text(encoding="utf-8") == FORENSICS_SHIM_CONTENT

    def test_idempotent_on_repeated_calls(self):
        from puzzleeval.agents.agent5.sandbox import stage_forensics_shim
        with tempfile.TemporaryDirectory() as d:
            sb = Path(d)
            assert stage_forensics_shim(sb) is True
            assert stage_forensics_shim(sb) is True
            assert (sb / "_forensics.py").exists()

    def test_creates_parent_dir_if_missing(self):
        from puzzleeval.agents.agent5.sandbox import stage_forensics_shim
        with tempfile.TemporaryDirectory() as d:
            sb = Path(d) / "deeply" / "nested"
            assert stage_forensics_shim(sb) is True
            assert (sb / "_forensics.py").exists()


# ---------------------------------------------------------------------------
# Shim is valid Python
# ---------------------------------------------------------------------------


class TestShimIsValidPython:
    def test_shim_parses(self):
        import ast
        from puzzleeval.agents.agent5.sandbox import FORENSICS_SHIM_CONTENT
        # Should not raise
        ast.parse(FORENSICS_SHIM_CONTENT)

    def test_shim_exposes_public_api(self):
        import ast
        from puzzleeval.agents.agent5.sandbox import FORENSICS_SHIM_CONTENT
        tree = ast.parse(FORENSICS_SHIM_CONTENT)
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        # Universal API
        assert "log" in names
        assert "traced_op" in names
        assert "log_thread_start" in names
        assert "log_thread_error" in names


# ---------------------------------------------------------------------------
# Shim run-time behavior — exercised in a real subprocess
# ---------------------------------------------------------------------------


def _run_shim_with_user_code(tmpdir: Path, user_code: str, env_extra: dict | None = None) -> tuple[str, str, int]:
    """Stage the shim into tmpdir, write user_code as runner.py, exec it.

    Returns (stdout, stderr, returncode).
    """
    from puzzleeval.agents.agent5.sandbox import stage_forensics_shim
    stage_forensics_shim(tmpdir)
    runner = tmpdir / "runner.py"
    runner.write_text(user_code, encoding="utf-8")
    import os
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(runner)],
        cwd=str(tmpdir),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _read_log(tmpdir: Path) -> list[dict]:
    log = tmpdir / "harness_forensics.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


class TestShimRuntimeBehavior:
    def test_log_writes_jsonl_with_canonical_fields(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stdout, stderr, rc = _run_shim_with_user_code(tmp, """
from _forensics import log
log("custom_event", op="test", provider="example", value=42)
""")
            assert rc == 0, f"stderr: {stderr}"
            events = _read_log(tmp)
            # harness_start (auto) + custom_event + harness_exit (auto)
            event_names = [e.get("event") for e in events]
            assert "harness_start" in event_names
            assert "custom_event" in event_names
            assert "harness_exit" in event_names
            custom = next(e for e in events if e.get("event") == "custom_event")
            assert custom["op"] == "test"
            assert custom["provider"] == "example"
            assert custom["value"] == 42
            assert "t_ms" in custom
            assert "t_abs" in custom

    def test_traced_op_emits_start_and_done(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stdout, stderr, rc = _run_shim_with_user_code(tmp, """
from _forensics import traced_op
with traced_op("create_session", provider="elevenlabs"):
    pass
""")
            assert rc == 0, f"stderr: {stderr}"
            events = _read_log(tmp)
            # traced_op emits op_start/op_done with op=<name> field;
            # canonical names like session_create_start come from explicit
            # log() calls, not from traced_op (the wrapper pattern is
            # generic; the taxonomy is for direct log() events).
            op_starts = [e for e in events if e.get("event") == "op_start"]
            op_dones = [e for e in events if e.get("event") == "op_done"]
            assert len(op_starts) == 1 and op_starts[0]["op"] == "create_session"
            assert len(op_dones) == 1
            done = op_dones[0]
            assert "duration_ms" in done
            assert done["op"] == "create_session"
            assert done["provider"] == "elevenlabs"

    def test_traced_op_emits_error_on_exception(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stdout, stderr, rc = _run_shim_with_user_code(tmp, """
from _forensics import traced_op
try:
    with traced_op("create_session", provider="x"):
        raise ValueError("nope")
except ValueError:
    pass
""")
            assert rc == 0, f"stderr: {stderr}"
            events = _read_log(tmp)
            errors = [e for e in events
                      if e.get("event") == "op_error"
                      and e.get("op") == "create_session"]
            assert len(errors) == 1
            err = errors[0]
            assert err["error_type"] == "ValueError"
            assert "nope" in err["error"]
            assert "duration_ms" in err

    def test_traced_op_does_not_swallow_exception(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stdout, stderr, rc = _run_shim_with_user_code(tmp, """
from _forensics import traced_op
with traced_op("op1", provider="x"):
    raise RuntimeError("propagates")
""")
            # Exception must propagate; rc should be nonzero
            assert rc != 0
            assert "RuntimeError" in stderr

    def test_thread_error_captured_automatically(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stdout, stderr, rc = _run_shim_with_user_code(tmp, """
import _forensics  # auto-installs threading hook
import threading, time
def crash():
    raise RuntimeError("thread-died")
t = threading.Thread(target=crash, name="reader")
t.start()
t.join()
""")
            # Main thread didn't crash; rc=0
            assert rc == 0
            events = _read_log(tmp)
            thread_errors = [e for e in events if e.get("event") == "thread_error"]
            assert len(thread_errors) == 1
            te = thread_errors[0]
            assert te["name"] == "reader"
            assert te["error_type"] == "RuntimeError"
            assert "thread-died" in te["error"]


# ---------------------------------------------------------------------------
# Credential redaction — MANDATORY before any field hits disk
# ---------------------------------------------------------------------------


class TestCredentialRedaction:
    @pytest.mark.parametrize("url,must_not_contain", [
        ("https://api.example.com/?token=secret123", "secret123"),
        ("https://api.example.com/?signature=ABC", "ABC"),
        ("https://api.example.com/?api_key=KEY", "KEY"),
        ("https://api.example.com/?access_token=AT", "AT"),
        ("https://api.example.com/?secret=VAL", "VAL"),
        ("https://api.example.com/?password=PWD", "PWD"),
        ("https://api.example.com/?key=K1", "K1"),
        ("https://api.example.com/path?id_token=IDT", "IDT"),
    ])
    def test_url_query_credentials_redacted(self, url, must_not_contain):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            user_code = f"""
from _forensics import log
log("test_url", url={url!r})
"""
            stdout, stderr, rc = _run_shim_with_user_code(tmp, user_code)
            assert rc == 0, f"stderr: {stderr}"
            log_text = (tmp / "harness_forensics.jsonl").read_text(encoding="utf-8")
            assert must_not_contain not in log_text, (
                f"Credential leaked in log: {must_not_contain!r} found in URL {url!r}"
            )
            assert "[REDACTED]" in log_text

    def test_authorization_header_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stdout, stderr, rc = _run_shim_with_user_code(tmp, """
from _forensics import log
log("test_headers", headers={"Authorization": "Bearer SECRET_TOKEN_12345",
                              "Content-Type": "application/json"})
""")
            assert rc == 0, f"stderr: {stderr}"
            log_text = (tmp / "harness_forensics.jsonl").read_text(encoding="utf-8")
            assert "SECRET_TOKEN_12345" not in log_text
            assert "[REDACTED]" in log_text
            # Non-credential header preserved
            assert "Content-Type" in log_text or "application/json" in log_text

    def test_xi_api_key_header_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stdout, stderr, rc = _run_shim_with_user_code(tmp, """
from _forensics import log
log("test", headers={"xi-api-key": "ELEVENLABS_REAL_KEY"})
""")
            assert rc == 0, f"stderr: {stderr}"
            log_text = (tmp / "harness_forensics.jsonl").read_text(encoding="utf-8")
            assert "ELEVENLABS_REAL_KEY" not in log_text


# ---------------------------------------------------------------------------
# Verification gate (semantic AST checks)
# ---------------------------------------------------------------------------


def _scenario(harness_src: str) -> str | None:
    """Run verify_forensics_coverage against a synthetic harness."""
    from puzzleeval.agents.agent5.verification import verify_forensics_coverage
    with tempfile.TemporaryDirectory() as d:
        sb = Path(d)
        (sb / "harness.py").write_text(harness_src, encoding="utf-8")
        return verify_forensics_coverage(sb)


class TestVerifyForensicsCoverage:
    def test_empty_harness_rejected(self):
        result = _scenario("")
        assert result is not None
        assert "empty" in result.lower() or "missing" in result.lower()

    def test_syntax_error_rejected(self):
        result = _scenario("def foo(\n  # never closed")
        assert result is not None
        assert "syntax" in result.lower()

    def test_missing_forensics_import_rejected(self):
        result = _scenario("import requests\nrequests.get('https://x')")
        assert result is not None
        assert "_forensics" in result

    def test_forensics_imported_after_network_lib_rejected(self):
        result = _scenario(
            "import requests\nfrom _forensics import log, traced_op\nrequests.get('x')"
        )
        assert result is not None
        assert "FIRST" in result or "before" in result.lower()

    def test_basic_correct_passes(self):
        result = _scenario(
            "from _forensics import log, traced_op\n"
            "import requests\n"
            "requests.get('https://x')\n"
        )
        assert result is None

    def test_openai_sdk_without_traced_op_rejected(self):
        result = _scenario(
            "from _forensics import log\n"
            "import openai\n"
            "openai.ChatCompletion.create()\n"
        )
        assert result is not None
        assert "openai" in result.lower()
        assert "traced_op" in result

    def test_openai_sdk_with_traced_op_passes(self):
        result = _scenario(
            "from _forensics import log, traced_op\n"
            "import openai\n"
            "with traced_op('chat', provider='openai'):\n"
            "    openai.ChatCompletion.create()\n"
        )
        assert result is None

    def test_streaming_harness_without_session_lifecycle_rejected(self):
        result = _scenario(
            "from _forensics import log\n"
            "import websocket\n"
            "import threading\n"
            "ws = websocket.create_connection('x')\n"
            "Thread = threading.Thread\n"
            "Thread(target=lambda: None).start()\n"
        )
        assert result is not None
        assert "session" in result.lower() or "stream" in result.lower()

    def test_streaming_harness_with_full_lifecycle_passes(self):
        result = _scenario(
            "from _forensics import log, traced_op\n"
            "import websocket\n"
            "import threading\n"
            "with traced_op('session_create', provider='x'):\n"
            "    ws = websocket.create_connection('x')\n"
            "with traced_op('stream', provider='x'):\n"
            "    pass\n"
        )
        assert result is None


# ---------------------------------------------------------------------------
# Faulthandler env-var configuration
# ---------------------------------------------------------------------------


class TestFaulthandlerConfig:
    def test_default_45s_timeout(self):
        # Just verify the env-var interpolation works; we don't actually
        # wait 45s in unit tests.
        from puzzleeval.config import HARNESS_FAULTHANDLER_TIMEOUT
        assert HARNESS_FAULTHANDLER_TIMEOUT == 45

    def test_zero_disables(self, monkeypatch):
        # The shim reads the env var at import time inside the subprocess,
        # so we test that the constant resolves correctly here.
        monkeypatch.setenv("PUZZLEEVAL_HARNESS_FAULTHANDLER_TIMEOUT", "0")
        # Re-import config to pick up the change
        import importlib, puzzleeval.config
        importlib.reload(puzzleeval.config)
        assert puzzleeval.config.HARNESS_FAULTHANDLER_TIMEOUT == 0


# ---------------------------------------------------------------------------
# Auto-hooks fail-soft (no crash when libraries missing)
# ---------------------------------------------------------------------------


class TestAutoHooksFailSoft:
    def test_shim_imports_cleanly_with_only_stdlib(self):
        """The shim's universal API must work even if every optional
        library import fails."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stdout, stderr, rc = _run_shim_with_user_code(tmp, """
from _forensics import log, traced_op
log("works")
with traced_op("op1"):
    pass
""")
            assert rc == 0, f"stderr: {stderr}"
            events = _read_log(tmp)
            assert len(events) >= 3  # harness_start + works + op_done + harness_exit
