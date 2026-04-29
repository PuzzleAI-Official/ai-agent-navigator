"""Custom tool dispatch for Agent 5 builder sandboxes.

This module owns the deterministic, production-side tool execution surface.
The legacy ``implement_test_env`` module keeps wrapper names for existing
imports, but new code should call here.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from puzzleeval.agents.agent5.dispatch_helpers import (
    SCAFFOLD_FILENAMES,
    is_forbidden_meta_filename,
    is_introspection_script_name,
    is_phase1_scaffold_violation,
)


_logger = logging.getLogger(__name__)


CUSTOM_TOOL_NAMES = {"write_file", "patch_file", "run_code", "read_file", "ask_research"}
ALLOWED_EXTENSIONS = {".py", ".txt", ".json", ".cfg", ".toml", ".sh", ".yaml", ".yml"}


def dispatch_tool(
    tool_name: str,
    tool_input: dict,
    sandbox_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    read_state: dict[str, float] | None = None,
    code_timeout_s: int,
    allowed_extensions: set[str] | frozenset[str] = ALLOWED_EXTENSIONS,
    phase_state: dict | None = None,
) -> tuple[str, int]:
    """Execute an Agent 5 custom tool and return ``(result, exit_code)``.

    ``phase_state`` is an optional dict carrying per-build context that
    feeds the write_file gates (B1, B2, B3). Recognized keys:
      * ``api_spec_written`` (bool): flips True after the builder writes
        or patches api_spec.txt; gates B3 (scaffold-block) on this.
      * ``candidate_slug`` (str | None): for structured logging of gate
        fires.
      * ``trace_id`` (str | None): same.
    Legacy callers that pass no ``phase_state`` get the original behavior
    (gates effectively off, since they default to permissive).
    """

    if tool_name == "write_file":
        result = write_file(
            tool_input,
            sandbox_dir,
            read_state=read_state,
            allowed_extensions=allowed_extensions,
            phase_state=phase_state,
        )
        return result, 1 if result.startswith("Error") else 0
    if tool_name == "patch_file":
        result = patch_file(tool_input, sandbox_dir, read_state=read_state)
        return result, 1 if result.startswith("Error") else 0
    if tool_name == "run_code":
        return run_code(
            tool_input,
            sandbox_dir,
            extra_env=extra_env,
            code_timeout_s=code_timeout_s,
        )
    if tool_name == "read_file":
        result = read_file(tool_input, sandbox_dir, read_state=read_state)
        return result, 1 if result.startswith("Error") else 0
    if tool_name == "ask_research":
        return "Error: ask_research must be dispatched via the main loop", -2
    return f"Error: unknown tool '{tool_name}'", -2


def _log_gate_fired(
    *,
    gate_name: str,
    gate_filename: str,
    severity: str,
    rejected: bool,
    phase_state: dict | None,
) -> None:
    """Emit a structured `gate_fired` log line for false-positive detection.

    Field name `gate_filename` (not `filename`) avoids a clash with
    LogRecord's built-in `filename` attribute (the source file name).
    """

    state = phase_state or {}
    _logger.warning(
        "gate_fired",
        extra={
            "operation": "gate_fired",
            "gate_name": gate_name,
            "gate_filename": gate_filename,
            "severity": severity,
            "rejected": rejected,
            "candidate_slug": state.get("candidate_slug"),
            "trace_id": state.get("trace_id"),
            "api_spec_written": state.get("api_spec_written"),
        },
    )


def write_file(
    tool_input: dict,
    sandbox_dir: Path,
    *,
    read_state: dict[str, float] | None = None,
    allowed_extensions: set[str] | frozenset[str] = ALLOWED_EXTENSIONS,
    phase_state: dict | None = None,
) -> str:
    """Write a file to the sandbox directory with path and extension checks.

    Phase B gates (each with env-var bypass via puzzleeval.config):

    * **B1 — Forbidden meta-filenames** (REJECT_TOOL_CALL): exact-match
      against `FORBIDDEN_META_FILENAMES` (case-insensitive). The builder
      receives a tool error and adapts (rename to a canonical file or
      patch the content into api_spec.txt).
    * **B2 — Introspection-script warn** (WARN-only): logs but allows
      writes of `inspect_*.py / check_*.py / explore_*.py / probe_*.py`
      when harness.py does not yet exist. Observability for fragmented-
      probing antipattern; doesn't block.
    * **B3 — Phase-1 scaffold block** (REJECT_TOOL_CALL): rejects writes
      of harness.py / smoke_test.py / live_test.py / requirements.txt
      while `phase_state['api_spec_written']` is False. Phase-keyed (NOT
      model-keyed) so model-fallback ladders can't trigger false rejects.
    """

    # Lazy-import config so test fixtures that flip env vars at module load
    # see the updated values.
    from puzzleeval import config as cfg

    raw_filename = tool_input.get("filename", "")
    content = tool_input.get("content", "")

    filename = Path(raw_filename).name
    if not filename:
        return "Error: empty filename"

    suffix = Path(filename).suffix.lower()
    if suffix not in allowed_extensions:
        return (
            f"Error: file extension '{suffix}' not allowed. "
            f"Use one of: {sorted(allowed_extensions)}"
        )

    # ── Gate B1: forbidden meta-filenames ─────────────────────────────
    if cfg.GATE_FORBIDDEN_FILENAMES_ENABLED and is_forbidden_meta_filename(filename):
        _log_gate_fired(
            gate_name="forbidden_meta_filename",
            gate_filename=filename,
            severity="REJECT_TOOL_CALL",
            rejected=True,
            phase_state=phase_state,
        )
        return (
            f"Error: '{filename}' is a meta/state-tracking file and is not "
            f"allowed in the sandbox. The canonical files are: api_spec.txt, "
            f"harness.py, requirements.txt, smoke_test.py, live_test.py. "
            f"If you need to record a finding, patch_file('api_spec.txt', ...) "
            f"with the relevant detail — that's your durable memory and "
            f"survives context compaction."
        )

    # ── Gate B3: Phase-1 scaffold-block ───────────────────────────────
    if cfg.GATE_PHASE1_SCAFFOLD_BLOCK_ENABLED and phase_state is not None:
        api_spec_written = bool(phase_state.get("api_spec_written", True))
        if is_phase1_scaffold_violation(filename, api_spec_written=api_spec_written):
            _log_gate_fired(
                gate_name="phase1_scaffold_block",
                gate_filename=filename,
                severity="REJECT_TOOL_CALL",
                rejected=True,
                phase_state=phase_state,
            )
            return (
                f"Error: cannot write '{filename}' in Phase 1. The api_spec.txt "
                f"hasn't been written or patched yet — that's the trigger for "
                f"the model switch to Opus. Either write_file('api_spec.txt', "
                f"...) for a fresh spec, or patch_file('api_spec.txt', ...) "
                f"if a pre-rendered spec is already in the sandbox. Phase 2 "
                f"scaffolds (harness.py / smoke_test.py / live_test.py / "
                f"requirements.txt) get written after that single patch fires "
                f"the switch."
            )

    # ── Gate B2: introspection-script warn (log + allow) ──────────────
    if cfg.GATE_INTROSPECTION_WARN_ENABLED and is_introspection_script_name(filename):
        harness_exists = (sandbox_dir / "harness.py").exists()
        if not harness_exists:
            _log_gate_fired(
                gate_name="introspection_warn",
                gate_filename=filename,
                severity="WARN",
                rejected=False,
                phase_state=phase_state,
            )

    target = sandbox_dir / filename
    try:
        target.write_text(content, encoding="utf-8")
        if read_state is not None:
            try:
                read_state[filename] = target.stat().st_mtime
            except OSError:
                pass
        return f"Written {len(content)} chars to {filename}"
    except OSError as exc:
        return f"Error writing {filename}: {exc}"


def patch_file(
    tool_input: dict,
    sandbox_dir: Path,
    *,
    read_state: dict[str, float] | None = None,
) -> str:
    """Replace a specific string in an existing sandbox file."""

    raw_filename = tool_input.get("filename", "")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")

    filename = Path(raw_filename).name
    if not filename:
        return "Error: empty filename"

    target = sandbox_dir / filename
    if not target.exists():
        return f"Error: '{filename}' does not exist in sandbox. Use write_file to create it first."

    if read_state is not None:
        try:
            current_mtime = target.stat().st_mtime
        except OSError:
            current_mtime = None
        last_read = read_state.get(filename)
        if last_read is None:
            return (
                f"STOP: '{filename}' has not been read yet in this build. "
                f"Call read_file('{filename}') FIRST so your patch plan is "
                f"based on the current file contents. This gate prevents the "
                f"iterative-micro-patch waste pattern (multiple consecutive "
                f"patches without re-reading -> blind fixes -> cumulative cost). "
                f"After reading, return with a comprehensive patch that "
                f"addresses every issue you identified."
            )
        if current_mtime is not None and last_read < current_mtime - 0.5:
            return (
                f"STOP: '{filename}' was modified after your last read_file call. "
                f"Your patch plan may be based on stale contents. Call "
                f"read_file('{filename}') AGAIN to see the current state, then "
                f"plan a comprehensive patch."
            )

    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Error reading {filename}: {exc}"

    if old_string not in content:
        normalized_old = old_string.replace("\r\n", "\n").replace("\r", "\n")
        normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")

        if normalized_old in normalized_content:
            new_content = normalized_content.replace(normalized_old, new_string, 1)
            try:
                target.write_text(new_content, encoding="utf-8")
                return (
                    f"Patched {filename} (after newline normalization): "
                    f"replaced {len(old_string)} chars with {len(new_string)} chars"
                )
            except OSError as exc:
                return f"Error writing {filename}: {exc}"

        preview = content[:800] if len(content) > 800 else content
        return (
            f"STOP: old_string not found in {filename}. Do NOT retry with a guess.\n\n"
            f"REQUIRED STEPS:\n"
            f"1. Use `read_file('{filename}')` to see the ACTUAL current content\n"
            f"2. Find the exact text you want to change (copy it precisely)\n"
            f"3. Call `patch_file` again with the correct old_string\n"
            f"4. If the file has encoding issues, use `write_file('{filename}', ...)` "
            f"to rewrite the entire file with your fix included\n\n"
            f"File preview ({len(content)} chars total):\n{preview}"
        )

    count = content.count(old_string)
    if count > 1:
        return (
            f"Error: old_string found {count} times in {filename}. "
            f"Provide a longer, unique string that matches only the section you want to change."
        )

    new_content = content.replace(old_string, new_string, 1)
    try:
        target.write_text(new_content, encoding="utf-8")
        if read_state is not None:
            try:
                read_state[filename] = target.stat().st_mtime
            except OSError:
                pass
        return f"Patched {filename}: replaced {len(old_string)} chars with {len(new_string)} chars"
    except OSError as exc:
        return f"Error writing {filename}: {exc}"


def build_sandbox_env(
    sandbox_dir: Path,
    extra_env: dict[str, str] | None = None,
    *,
    platform: str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build subprocess environment variables for sandbox execution."""

    host_platform = platform or sys.platform
    env = {
        **(base_env or os.environ),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }

    venv_dir = sandbox_dir / ".venv"
    if venv_dir.exists():
        venv_bin = str(venv_dir / ("Scripts" if host_platform == "win32" else "bin"))
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(venv_dir)

    if extra_env:
        env.update(extra_env)

    return env


def run_code(
    tool_input: dict,
    sandbox_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    code_timeout_s: int,
) -> tuple[str, int]:
    """Run a shell command in the sandbox directory."""

    command = tool_input.get("command", "")
    if not command:
        return "Error: empty command", -2

    env = build_sandbox_env(sandbox_dir, extra_env)

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(sandbox_dir),
            capture_output=True,
            text=True,
            timeout=code_timeout_s,
            env=env,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            stderr_text = result.stderr.strip()
            if stderr_text:
                if output:
                    output += "\n"
                output += f"[stderr] {stderr_text}"

        if not output.strip():
            command_lower = command.lower()
            if (
                result.returncode == 0
                and "python" in command_lower
                and ("print" in command_lower or "import" in command_lower)
            ):
                output = (
                    "(command completed with exit code 0 but produced no visible output. "
                    "This is a known Windows issue with Python subprocess output capture. "
                    "WORKAROUND: Instead of `python -c \"print(...)\"`, write a small .py "
                    "file with write_file and run it with run_code. Or use read_file to "
                    "read files directly.)"
                )
            else:
                output = f"(command completed with exit code {result.returncode})"

        if result.returncode != 0:
            exit_note = f"\n[Exit code: {result.returncode}"
            if result.returncode == 1:
                exit_note += " - may indicate: test failure, no matches found (grep), or general error"
            elif result.returncode == 2:
                exit_note += " - may indicate: misuse of command or invalid arguments"
            elif result.returncode == 126:
                exit_note += " - permission denied (cannot execute)"
            elif result.returncode == 127:
                exit_note += " - command not found"
            elif result.returncode == 137:
                exit_note += " - process killed (OOM or signal 9)"
            exit_note += "]"
            output += exit_note

        if len(output) > 5000:
            output = output[:5000] + "\n... (output truncated at 5000 chars)"

        return output, result.returncode

    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {code_timeout_s} seconds", -1
    except OSError as exc:
        return f"Error running command: {exc}", -2


def read_file(
    tool_input: dict,
    sandbox_dir: Path,
    *,
    read_state: dict[str, float] | None = None,
) -> str:
    """Read a file from the sandbox directory."""

    raw_filename = tool_input.get("filename", "")
    filename = Path(raw_filename).name
    if not filename:
        return "Error: empty filename"

    target = sandbox_dir / filename
    if not target.exists():
        return f"Error: '{filename}' does not exist in sandbox"

    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > 10000:
            content = content[:10000] + "\n... (content truncated at 10000 chars)"
        if read_state is not None:
            try:
                read_state[filename] = target.stat().st_mtime
            except OSError:
                pass
        return content
    except OSError as exc:
        return f"Error reading {filename}: {exc}"
