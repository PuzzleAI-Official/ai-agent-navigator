"""Custom tool dispatch for Agent 5 builder sandboxes.

This module owns the deterministic, production-side tool execution surface.
The legacy ``implement_test_env`` module keeps wrapper names for existing
imports, but new code should call here.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from puzzleeval.agents.agent5.dispatch_helpers import (
    SCAFFOLD_FILENAMES,
    is_forbidden_meta_filename,
    is_introspection_script_name,
    is_phase1_scaffold_violation,
)


_logger = logging.getLogger(__name__)


CUSTOM_TOOL_NAMES = {
    "write_file",
    "patch_file",
    "run_code",
    "read_file",
    "read_file_range",
    "ask_research",
    "read_forensics",
    "summarize_forensics",
    "summarize_build_state",
}
ALLOWED_EXTENSIONS = {".py", ".txt", ".json", ".cfg", ".toml", ".sh", ".yaml", ".yml", ".md"}

FILE_UNCHANGED_STUB = (
    "File unchanged since last read. The content from the earlier read_file "
    "tool_result in this conversation is still current; use read_file_range "
    "if you need exact lines."
)
_FULL_READ_KEY = "full"


@dataclass
class ToolDispatchResult:
    content: str
    exit_code: int
    metadata: dict[str, Any] = field(default_factory=dict)


# Subdirectories the agent may read from + write into (a-la-carte allowlist
# rather than full subdir support — keep blast radius small). Today the
# only entry is ``_agent_state/`` for the autonomy artifacts (PR 1 of the
# Goal/Planning/State/Reflection plan). New entries must justify why
# subdir access is required vs sandbox-root.
_ALLOWED_SUBDIRS: frozenset[str] = frozenset({"_agent_state"})
_ALLOWED_NESTED_READONLY_PREFIXES: frozenset[str] = frozenset({
    "_agent_state/failure_packets",
    "_agent_state/research_findings",
})


# Files inside ``_agent_state/`` that are ORCHESTRATOR-OWNED and must not
# be writable by the agent. ``write_file`` rejects with a clear error
# message; ``patch_file`` rejects identically. Reading these is allowed.
ORCHESTRATOR_OWNED_ARTIFACTS: frozenset[str] = frozenset({
    "_agent_state/business_fixture.json",
    "_agent_state/build_decisions.jsonl",
    "_agent_state/code_diagnostics.json",
    "_agent_state/debug_research_ledger.json",
    "_agent_state/latest_failure_packet.json",
    "_agent_state/objective.md",
    "_agent_state/post_compaction_snapshot.json",
    "_agent_state/docs_entrypoint.json",
    "_agent_state/research_handoff.json",
    "_agent_state/research_findings_index.json",
    "_agent_state/research_terminal_urls.json",
    "_agent_state/runtime_snapshot.json",
    "_agent_state/runtime_state.json",
})


def _is_orchestrator_owned_artifact(relative_path: str) -> bool:
    """Return True when ``relative_path`` is read-only to Agent 5."""

    normalized = relative_path.replace("\\", "/")
    if normalized in ORCHESTRATOR_OWNED_ARTIFACTS:
        return True
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in _ALLOWED_NESTED_READONLY_PREFIXES
    )


def _resolve_sandbox_filename(raw_filename: str) -> str | None:
    """Return the sandbox-relative path for a tool argument.

    Handles two cases:
      * Plain filename (no directory) → returned as-is (the historical
        behavior of ``Path(raw_filename).name`` with no information lost
        when the input was already a bare filename).
      * Path with one allowlisted subdirectory prefix (e.g.
        ``_agent_state/build_plan.md``) → returned as a forward-slashed
        relative path so callers can use it for both filesystem ops and
        log fields.

    Returns None when the input has a directory prefix that is NOT on
    the allowlist (caller produces a clear error). Empty strings also
    yield None.
    """
    raw_filename = (raw_filename or "").strip()
    if not raw_filename:
        return None

    # Reject absolute paths early. Check before any normalization so an
    # input like "/etc/passwd" or "C:\\foo" doesn't slip through.
    if raw_filename.startswith(("/", "\\")) or (len(raw_filename) > 2 and raw_filename[1] == ":"):
        return None

    # Normalize separators (Windows + POSIX) and strip a SINGLE leading
    # "./" if present. We deliberately do NOT use ``Path.lstrip("./")``
    # which would also munch a leading ".." (lstrip strips any of the
    # given characters from the left, eating "../foo" → "foo").
    normalized = raw_filename.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    parts = [p for p in normalized.split("/") if p]
    if not parts:
        return None

    # Reject ".." anywhere in the path (traversal).
    if any(p == ".." for p in parts):
        return None

    if len(parts) == 1:
        return parts[0]

    if len(parts) == 2 and parts[0] in _ALLOWED_SUBDIRS:
        return f"{parts[0]}/{parts[1]}"

    if len(parts) == 3:
        candidate = f"{parts[0]}/{parts[1]}"
        if candidate in _ALLOWED_NESTED_READONLY_PREFIXES:
            return f"{candidate}/{parts[2]}"

    # Other multi-level paths or unknown subdirs are rejected.
    return None


def _read_state_mtime(value) -> float | None:
    """Extract mtime from old float or new metadata read_state entries."""
    if isinstance(value, dict):
        value = value.get("mtime")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_state_source(value) -> str:
    if isinstance(value, dict):
        source = value.get("source")
        if isinstance(source, str):
            return source
    return "read"


def _read_state_keys(value) -> set[str]:
    if not isinstance(value, dict):
        return set()
    keys = value.get("read_keys")
    if isinstance(keys, list):
        return {str(k) for k in keys}
    if isinstance(keys, set):
        return {str(k) for k in keys}
    return set()


def _read_state_has_exact_read(value, mtime: float, read_key: str) -> bool:
    last_mtime = _read_state_mtime(value)
    if last_mtime is None or last_mtime != mtime:
        return False
    if _read_state_source(value) != "read":
        return False
    return read_key in _read_state_keys(value)


def _mark_read_state(
    read_state: dict | None,
    relative_path: str,
    mtime: float,
    source: str,
    *,
    read_key: str | None = None,
) -> None:
    if read_state is not None:
        entry: dict[str, Any] = {"mtime": mtime, "source": source}
        if source == "read" and read_key:
            previous = read_state.get(relative_path)
            keys: set[str] = set()
            if (
                isinstance(previous, dict)
                and _read_state_source(previous) == "read"
                and _read_state_mtime(previous) == mtime
            ):
                keys = _read_state_keys(previous)
            keys.add(read_key)
            entry["read_keys"] = sorted(keys)
        read_state[relative_path] = entry


def invalidate_read_dedup_state(read_state: dict | None) -> None:
    """Remove exact-read dedup keys while preserving patch safety metadata."""

    if read_state is None:
        return
    for relative_path, value in list(read_state.items()):
        if isinstance(value, dict):
            next_value = dict(value)
            next_value.pop("read_keys", None)
            next_value["dedup_invalidated"] = True
            read_state[relative_path] = next_value


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
      * ``implementation_plan_accepted`` (bool): active build gate.
      * ``candidate_slug`` (str | None): for structured logging of gate
        fires.
      * ``trace_id`` (str | None): same.
    Legacy callers that pass no ``phase_state`` get the original behavior
    (gates effectively off, since they default to permissive).
    """

    result = dispatch_tool_result(
        tool_name,
        tool_input,
        sandbox_dir,
        extra_env=extra_env,
        read_state=read_state,
        code_timeout_s=code_timeout_s,
        allowed_extensions=allowed_extensions,
        phase_state=phase_state,
    )
    return result.content, result.exit_code


def dispatch_tool_result(
    tool_name: str,
    tool_input: dict,
    sandbox_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    read_state: dict[str, float] | None = None,
    code_timeout_s: int,
    allowed_extensions: set[str] | frozenset[str] = ALLOWED_EXTENSIONS,
    phase_state: dict | None = None,
) -> ToolDispatchResult:
    """Execute an Agent 5 custom tool with structured metadata."""

    if tool_name == "write_file":
        result = write_file(
            tool_input,
            sandbox_dir,
            read_state=read_state,
            allowed_extensions=allowed_extensions,
            phase_state=phase_state,
        )
        return ToolDispatchResult(result, 1 if result.startswith("Error") else 0)
    if tool_name == "patch_file":
        result = patch_file(tool_input, sandbox_dir, read_state=read_state)
        return ToolDispatchResult(result, 1 if result.startswith("Error") else 0)
    if tool_name == "run_code":
        content, exit_code = run_code(
            tool_input,
            sandbox_dir,
            extra_env=extra_env,
            code_timeout_s=code_timeout_s,
        )
        return ToolDispatchResult(content, exit_code)
    if tool_name == "read_file":
        result, metadata = _read_file_impl(tool_input, sandbox_dir, read_state=read_state)
        return ToolDispatchResult(result, 1 if result.startswith("Error") else 0, metadata)
    if tool_name == "read_file_range":
        result, metadata = _read_file_range_impl(
            tool_input,
            sandbox_dir,
            read_state=read_state,
        )
        return ToolDispatchResult(result, 1 if result.startswith("Error") else 0, metadata)
    if tool_name == "read_forensics":
        result = read_forensics(tool_input, sandbox_dir)
        return ToolDispatchResult(result, 1 if result.startswith("Error") else 0)
    if tool_name == "summarize_forensics":
        result = summarize_forensics(tool_input, sandbox_dir)
        return ToolDispatchResult(result, 1 if result.startswith("Error") else 0)
    if tool_name == "summarize_build_state":
        result = summarize_build_state(tool_input, sandbox_dir)
        return ToolDispatchResult(result, 1 if result.startswith("Error") else 0)
    if tool_name == "ask_research":
        return ToolDispatchResult("Error: ask_research must be dispatched via the main loop", -2)
    return ToolDispatchResult(f"Error: unknown tool '{tool_name}'", -2)


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
            "build_gate_accepted": state.get("build_gate_accepted"),
            "implementation_plan_accepted": state.get("implementation_plan_accepted"),
        },
    )


def _validate_implementation_plan_write(
    *,
    relative_path: str,
    content: str,
    sandbox_dir: Path,
    phase_state: dict | None = None,
) -> str | None:
    """Return an error string when implementation_plan.json fails its gate."""

    from puzzleeval import config as cfg
    from puzzleeval.agents.agent5.objective_validator import (
        IMPLEMENTATION_PLAN_RELATIVE_PATH,
        validate_implementation_plan_text,
    )

    if relative_path.replace("\\", "/") != IMPLEMENTATION_PLAN_RELATIVE_PATH:
        return None

    if cfg.RESEARCH_WORKERS_ENABLED:
        from puzzleeval.agents.agent5.research_plan import (
            RESEARCH_SYNTHESIS_RELATIVE_PATH,
            validate_research_synthesis_text,
        )

        synthesis_path = sandbox_dir / RESEARCH_SYNTHESIS_RELATIVE_PATH
        try:
            synthesis_text = synthesis_path.read_text(encoding="utf-8")
        except OSError as exc:
            return (
                "Error: cannot accept implementation_plan.json before "
                "_agent_state/research_synthesis.json exists and passes "
                f"validation: {exc}. Consolidate planned research findings "
                "into research_synthesis.json first so the build plan is "
                "grounded in durable provider understanding, not transient "
                "context."
            )
        synthesis_verdict = validate_research_synthesis_text(synthesis_text)
        if not synthesis_verdict.ok:
            return (
                "Error: cannot accept implementation_plan.json because "
                "_agent_state/research_synthesis.json is invalid. "
                + "; ".join(synthesis_verdict.issues)
                + ". Fix research_synthesis.json first, then write the "
                "implementation plan."
            )
        try:
            synthesis_payload = json.loads(synthesis_text)
        except json.JSONDecodeError as exc:
            return (
                "Error: cannot accept implementation_plan.json because "
                f"research_synthesis.json is invalid JSON: {exc.msg}"
            )
        if not isinstance(synthesis_payload, dict) or synthesis_payload.get(
            "proceed_to_implementation_plan"
        ) is not True:
            return (
                "Error: cannot accept implementation_plan.json until "
                "research_synthesis.json sets proceed_to_implementation_plan=true. "
                "Record blocking questions or risks in research_synthesis.json "
                "before planning the build."
            )

    if not cfg.OBJECTIVE_VALIDATOR_ENABLED:
        _logger.info(
            "objective validator skipped",
            extra={
                "operation": "objective_validator_skipped",
                "trace_id": (phase_state or {}).get("trace_id"),
                "candidate_slug": (phase_state or {}).get("candidate_slug"),
                "validator_flag": "PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED=0",
            },
        )

    objective_md = ""
    if cfg.OBJECTIVE_VALIDATOR_ENABLED:
        objective_path = sandbox_dir / "_agent_state" / "objective.md"
        try:
            objective_md = objective_path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Error: cannot validate implementation_plan.json because objective.md is unavailable: {exc}"

    verdict = validate_implementation_plan_text(
        content,
        objective_md=objective_md,
        check_objective_coverage=cfg.OBJECTIVE_VALIDATOR_ENABLED,
        check_required_fields=True,
    )
    if verdict.ok:
        _logger.info(
            "implementation plan validator passed",
            extra={
                "operation": "implementation_plan_validator_passed",
                "trace_id": (phase_state or {}).get("trace_id"),
                "candidate_slug": (phase_state or {}).get("candidate_slug"),
                "required_count": verdict.required_count,
                "covered_count": verdict.covered_count,
                "criteria_results": verdict.criteria_results,
            },
        )
        return None

    _log_gate_fired(
        gate_name="implementation_plan_validator",
        gate_filename=relative_path,
        severity="REJECT_TOOL_CALL",
        rejected=True,
        phase_state=phase_state,
    )
    issue_text = "; ".join(verdict.issues)
    return (
        "Error: implementation_plan.json failed implementation-plan validation. "
        f"{issue_text}. Read _agent_state/objective.md, docs_entrypoint.json, "
        "research_synthesis.json, and rewrite implementation_plan.json with "
        "objective_coverage entries referencing objective IDs (OBJ-1, OBJ-2, "
        "...), chosen API surface, credentials, interaction pattern, live-test "
        "strategy, open questions, and ready_to_build=true. Because this tool "
        "call was rejected, the artifact may not exist yet; use write_file "
        "when absent and patch_file only when the file is actually on disk."
    )


def _validate_research_artifact_write(
    *,
    relative_path: str,
    content: str,
    phase_state: dict | None = None,
) -> str | None:
    """Return an error string when Phase 3 research artifacts are invalid."""

    from puzzleeval import config as cfg
    from puzzleeval.agents.agent5.research_plan import (
        RESEARCH_PLAN_RELATIVE_PATH,
        RESEARCH_SYNTHESIS_RELATIVE_PATH,
        validate_research_plan_text,
        validate_research_synthesis_text,
    )

    normalized = relative_path.replace("\\", "/")
    validators = {
        RESEARCH_PLAN_RELATIVE_PATH: (
            "research_plan_validator",
            "research_plan.json",
            validate_research_plan_text,
        ),
        RESEARCH_SYNTHESIS_RELATIVE_PATH: (
            "research_synthesis_validator",
            "research_synthesis.json",
            validate_research_synthesis_text,
        ),
    }
    if normalized not in validators:
        return None

    gate_name, artifact_name, validator = validators[normalized]
    if not cfg.RESEARCH_WORKERS_ENABLED:
        _logger.info(
            "research artifact validator skipped",
            extra={
                "operation": "research_workers_validator_skipped",
                "trace_id": (phase_state or {}).get("trace_id"),
                "candidate_slug": (phase_state or {}).get("candidate_slug"),
                "validator_flag": "PUZZLEEVAL_RESEARCH_WORKERS_ENABLED=0",
                "artifact": artifact_name,
            },
        )
        return None

    verdict = validator(content)
    if verdict.ok:
        _logger.info(
            "research artifact validator passed",
            extra={
                "operation": "research_workers_validator_passed",
                "trace_id": (phase_state or {}).get("trace_id"),
                "candidate_slug": (phase_state or {}).get("candidate_slug"),
                "artifact": artifact_name,
                "task_count": verdict.task_count,
                "citation_count": verdict.citation_count,
            },
        )
        return None

    _log_gate_fired(
        gate_name=gate_name,
        gate_filename=normalized,
        severity="REJECT_TOOL_CALL",
        rejected=True,
        phase_state=phase_state,
    )
    issue_text = "; ".join(verdict.issues)
    return (
        f"Error: {artifact_name} failed Phase 3 research artifact validation. "
        f"{issue_text}. The file was not persisted; rewrite the artifact with "
        "the missing structured fields, or ask one scoped research gap only if "
        "the missing field depends on provider facts not already in findings."
    )


def _validate_abandon_candidate_write(
    *,
    relative_path: str,
    content: str,
    sandbox_dir: Path,
) -> str | None:
    """Return an error string when Phase 6 abandonment is invalid."""

    from puzzleeval import config as cfg
    from puzzleeval.agents.agent5.abandon_candidate import (
        ABANDON_CANDIDATE_RELATIVE_PATH,
        validate_abandon_candidate_text,
    )

    normalized = relative_path.replace("\\", "/")
    if normalized != ABANDON_CANDIDATE_RELATIVE_PATH:
        return None
    if not cfg.ABANDON_CANDIDATE_ENABLED:
        return (
            "Error: abandon_candidate.json early exit is disabled by "
            "PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED=0. Continue the normal "
            "debug loop until the build fails through turn or budget caps."
        )

    verdict = validate_abandon_candidate_text(content, sandbox_dir=sandbox_dir)
    if verdict.ok:
        _logger.info(
            "abandon candidate validator passed",
            extra={
                "operation": "abandon_candidate_validator_passed",
                "reason": (verdict.data or {}).get("reason"),
            },
        )
        return None
    issue_text = "; ".join(verdict.issues)
    return (
        "Error: abandon_candidate.json failed Phase 6 validation. "
        f"{issue_text}. Provide a valid reason, a concise summary, and "
        "external evidence such as docs_entrypoint.json, "
        "latest_failure_packet.json, a research finding, or a provider response."
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
      receives a tool error and adapts by using a first-class artifact
      such as research_synthesis.json, implementation_plan.json, or
      reflection evidence.
    * **B2 — Introspection-script warn** (WARN-only): logs but allows
      writes of `inspect_*.py / check_*.py / explore_*.py / probe_*.py`
      when harness.py does not yet exist. Observability for fragmented-
      probing antipattern; doesn't block.
    * **B3 — pre-build scaffold block** (REJECT_TOOL_CALL): rejects writes
      of harness.py / smoke_test.py / live_test.py / requirements.txt
      until _agent_state/implementation_plan.json has been accepted.
    """

    # Lazy-import config so test fixtures that flip env vars at module load
    # see the updated values.
    from puzzleeval import config as cfg

    raw_filename = tool_input.get("filename", "")
    content = tool_input.get("content", "")

    relative_path = _resolve_sandbox_filename(raw_filename)
    if not relative_path:
        return (
            "Error: invalid filename. Use a bare filename (e.g. "
            "'harness.py') OR a path inside an allowlisted subdirectory "
            f"({sorted(_ALLOWED_SUBDIRS)}, e.g. '_agent_state/build_plan.md'). "
            "Absolute paths and `..` traversal are rejected."
        )

    # The base filename (without subdir prefix) drives gate B1/B3 lookups —
    # those gates were designed against bare-name semantics; honoring them
    # uniformly keeps the rejection messages stable when the prefix differs.
    filename = Path(relative_path).name

    suffix = Path(filename).suffix.lower()
    if suffix not in allowed_extensions:
        return (
            f"Error: file extension '{suffix}' not allowed. "
            f"Use one of: {sorted(allowed_extensions)}"
        )

    # ── Orchestrator-owned artifact protection ────────────────────────
    # Files inside _agent_state/ that the orchestrator owns are write-
    # protected. Read access is allowed (read_file works fine); writes
    # are rejected so the agent's mental model can't corrupt the
    # authoritative state the orchestrator maintains.
    if _is_orchestrator_owned_artifact(relative_path):
        _log_gate_fired(
            gate_name="orchestrator_owned_artifact",
            gate_filename=relative_path,
            severity="REJECT_TOOL_CALL",
            rejected=True,
            phase_state=phase_state,
        )
        return (
            f"Error: '{relative_path}' is orchestrator-owned and cannot be "
            f"modified by the agent. The orchestrator updates this file "
            f"directly each turn. You may READ it via read_file to ground "
            f"your mental model, but you cannot WRITE it. Agent-writable "
            f"artifacts in _agent_state/: build_plan.md, "
            f"research_plan.json, research_synthesis.json, "
            f"implementation_plan.json, abandon_candidate.json, "
            f"agent_observations.json, reflection_phase_<n>.md."
        )

    # ── Gate B1: forbidden meta-filenames ─────────────────────────────
    if cfg.GATE_FORBIDDEN_FILENAMES_ENABLED and is_forbidden_meta_filename(filename):
        _log_gate_fired(
            gate_name="forbidden_meta_filename",
            gate_filename=relative_path,
            severity="REJECT_TOOL_CALL",
            rejected=True,
            phase_state=phase_state,
        )
        return (
            f"Error: '{filename}' is a meta/state-tracking file and is not "
            f"allowed in the sandbox. The canonical files are: "
            f"_agent_state/research_plan.json, "
            f"_agent_state/research_synthesis.json, "
            f"_agent_state/implementation_plan.json, "
            f"_agent_state/abandon_candidate.json, "
            f"_agent_state/reflection_phase_<n>.md, harness.py, "
            f"requirements.txt, smoke_test.py, and live_test.py. "
            f"If you need to record a finding, "
            f"write it into research_synthesis.json or implementation_plan.json "
            f"so it drives action selection and survives context compaction."
        )

    # ── Gate B3: Phase-1 scaffold-block ───────────────────────────────
    if cfg.GATE_PHASE1_SCAFFOLD_BLOCK_ENABLED and phase_state is not None:
        build_gate_satisfied = bool(
            phase_state.get("implementation_plan_accepted", False)
        )
        if is_phase1_scaffold_violation(filename, build_gate_accepted=build_gate_satisfied):
            _log_gate_fired(
                gate_name="phase1_scaffold_block",
                gate_filename=relative_path,
                severity="REJECT_TOOL_CALL",
                rejected=True,
                phase_state=phase_state,
            )
            return (
                f"Error: cannot write '{filename}' before "
                "_agent_state/implementation_plan.json has been accepted. "
                "Write a valid implementation plan with objective coverage, "
                "chosen API surface, credentials, interaction pattern, "
                "live-test strategy, no blocking open questions, and "
                "ready_to_build=true."
            )

    # ── Gate B2: introspection-script warn (log + allow) ──────────────
    if cfg.GATE_INTROSPECTION_WARN_ENABLED and is_introspection_script_name(filename):
        harness_exists = (sandbox_dir / "harness.py").exists()
        if not harness_exists:
            _log_gate_fired(
                gate_name="introspection_warn",
                gate_filename=relative_path,
                severity="WARN",
                rejected=False,
                phase_state=phase_state,
            )

    validation_error = _validate_implementation_plan_write(
        relative_path=relative_path,
        content=content,
        sandbox_dir=sandbox_dir,
        phase_state=phase_state,
    )
    if validation_error:
        return validation_error

    validation_error = _validate_research_artifact_write(
        relative_path=relative_path,
        content=content,
        phase_state=phase_state,
    )
    if validation_error:
        return validation_error

    validation_error = _validate_abandon_candidate_write(
        relative_path=relative_path,
        content=content,
        sandbox_dir=sandbox_dir,
    )
    if validation_error:
        return validation_error

    target = sandbox_dir / relative_path
    try:
        # Allowlisted subdirs may not exist yet (sandbox.stage_agent_state
        # creates _agent_state/ at setup, but defense-in-depth: ensure the
        # parent exists before writing).
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if read_state is not None:
            try:
                # A file the agent just wrote is known to the agent, so
                # immediate follow-up patches can proceed without a
                # redundant read. read_file still reads from disk, so this
                # marker cannot return stale pre-write content.
                _mark_read_state(
                    read_state,
                    relative_path,
                    target.stat().st_mtime,
                    "write",
                )
            except OSError:
                pass
        return f"Written {len(content)} chars to {relative_path}"
    except OSError as exc:
        return f"Error writing {relative_path}: {exc}"


def patch_file(
    tool_input: dict,
    sandbox_dir: Path,
    *,
    read_state: dict[str, float] | None = None,
    allowed_extensions: set[str] | frozenset[str] = ALLOWED_EXTENSIONS,
) -> str:
    """Replace a specific string in an existing sandbox file."""

    raw_filename = tool_input.get("filename", "")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")

    relative_path = _resolve_sandbox_filename(raw_filename)
    if not relative_path:
        return (
            "Error: invalid filename. Use a bare filename or a path inside "
            f"an allowlisted subdirectory ({sorted(_ALLOWED_SUBDIRS)})."
        )

    # Orchestrator-owned artifact protection (parallel to write_file).
    if _is_orchestrator_owned_artifact(relative_path):
        return (
            f"Error: '{relative_path}' is orchestrator-owned and cannot be "
            f"patched by the agent. Read it freely via read_file; the "
            f"orchestrator updates it each turn."
        )

    filename = Path(relative_path).name
    suffix = Path(filename).suffix.lower()
    if suffix not in allowed_extensions:
        return (
            f"Error: file extension '{suffix}' not allowed. "
            f"Use one of: {sorted(allowed_extensions)}"
        )

    target = sandbox_dir / relative_path
    if not target.exists():
        return f"Error: '{relative_path}' does not exist in sandbox. Use write_file to create it first."

    if read_state is not None:
        try:
            current_mtime = target.stat().st_mtime
        except OSError:
            current_mtime = None
        last_read = read_state.get(relative_path)
        last_read_mtime = _read_state_mtime(last_read)
        if last_read_mtime is None:
            return (
                f"STOP: '{relative_path}' has not been read yet in this build. "
                f"Call read_file('{relative_path}') FIRST so your patch plan is "
                f"based on the current file contents. This gate prevents the "
                f"iterative-micro-patch waste pattern (multiple consecutive "
                f"patches without re-reading -> blind fixes -> cumulative cost). "
                f"After reading, return with a comprehensive patch that "
                f"addresses every issue you identified."
            )
        if current_mtime is not None and last_read_mtime < current_mtime - 0.5:
            return (
                f"STOP: '{relative_path}' was modified after your last read_file call. "
                f"Your patch plan may be based on stale contents. Call "
                f"read_file('{relative_path}') AGAIN to see the current state, then "
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
            validation_error = _validate_implementation_plan_write(
                relative_path=relative_path,
                content=new_content,
                sandbox_dir=sandbox_dir,
            )
            if validation_error:
                return validation_error
            validation_error = _validate_research_artifact_write(
                relative_path=relative_path,
                content=new_content,
            )
            if validation_error:
                return validation_error
            validation_error = _validate_abandon_candidate_write(
                relative_path=relative_path,
                content=new_content,
                sandbox_dir=sandbox_dir,
            )
            if validation_error:
                return validation_error
            try:
                target.write_text(new_content, encoding="utf-8")
                if read_state is not None:
                    try:
                        _mark_read_state(
                            read_state,
                            relative_path,
                            target.stat().st_mtime,
                            "patch",
                        )
                    except OSError:
                        pass
                return (
                    f"Patched {filename} (after newline normalization): "
                    f"replaced {len(old_string)} chars with {len(new_string)} chars"
                )
            except OSError as exc:
                return f"Error writing {filename}: {exc}"

        preview = content[:800] if len(content) > 800 else content
        return (
            f"STOP: old_string not found in {relative_path}. Do NOT retry with a guess.\n\n"
            f"REQUIRED STEPS:\n"
            f"1. Use `read_file('{relative_path}')` to see the ACTUAL current content\n"
            f"2. Find the exact text you want to change (copy it precisely)\n"
            f"3. Call `patch_file` again with the correct old_string\n"
            f"4. If the file has encoding issues, use `write_file('{relative_path}', ...)` "
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
    validation_error = _validate_implementation_plan_write(
        relative_path=relative_path,
        content=new_content,
        sandbox_dir=sandbox_dir,
    )
    if validation_error:
        return validation_error
    validation_error = _validate_research_artifact_write(
        relative_path=relative_path,
        content=new_content,
    )
    if validation_error:
        return validation_error
    validation_error = _validate_abandon_candidate_write(
        relative_path=relative_path,
        content=new_content,
        sandbox_dir=sandbox_dir,
    )
    if validation_error:
        return validation_error
    try:
        target.write_text(new_content, encoding="utf-8")
        if read_state is not None:
            try:
                # A successful patch updates the observed version so a
                # second same-turn patch doesn't trip the read-before-patch
                # gate. read_file still returns current disk content.
                _mark_read_state(
                    read_state,
                    relative_path,
                    target.stat().st_mtime,
                    "patch",
                )
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
    result, _metadata = _read_file_impl(
        tool_input,
        sandbox_dir,
        read_state=read_state,
    )
    return result


def _read_file_impl(
    tool_input: dict,
    sandbox_dir: Path,
    *,
    read_state: dict[str, float] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Read a file and return Claude-Code-style dedup metadata.

    Supports both bare filenames (``harness.py``) and paths inside an
    allowlisted subdirectory (``_agent_state/objective.md``). Other
    directory prefixes are rejected.
    """

    raw_filename = tool_input.get("filename", "")
    relative_path = _resolve_sandbox_filename(raw_filename)
    if not relative_path:
        return (
            "Error: invalid filename. Use a bare filename or a path inside "
            f"an allowlisted subdirectory ({sorted(_ALLOWED_SUBDIRS)})."
        ), {"deduped_read": False}

    target = sandbox_dir / relative_path
    if not target.exists():
        return f"Error: '{relative_path}' does not exist in sandbox", {
            "deduped_read": False,
            "path": relative_path,
        }

    try:
        current_mtime = target.stat().st_mtime
        if read_state is not None and _read_state_has_exact_read(
            read_state.get(relative_path),
            current_mtime,
            _FULL_READ_KEY,
        ):
            return FILE_UNCHANGED_STUB, {
                "deduped_read": True,
                "path": relative_path,
                "read_key": _FULL_READ_KEY,
                "range": "full",
            }
        content = target.read_text(encoding="utf-8")
        truncated = False
        if len(content) > 10000:
            content = content[:10000] + "\n... (content truncated at 10000 chars)"
            truncated = True
        if read_state is not None:
            try:
                _mark_read_state(
                    read_state,
                    relative_path,
                    current_mtime,
                    "read",
                    read_key=_FULL_READ_KEY,
                )
            except OSError:
                pass
        return content, {
            "deduped_read": False,
            "path": relative_path,
            "read_key": _FULL_READ_KEY,
            "range": "full",
            "truncated": truncated,
        }
    except OSError as exc:
        return f"Error reading {relative_path}: {exc}", {
            "deduped_read": False,
            "path": relative_path,
        }


def read_file_range(
    tool_input: dict,
    sandbox_dir: Path,
    *,
    read_state: dict[str, float] | None = None,
) -> str:
    result, _metadata = _read_file_range_impl(
        tool_input,
        sandbox_dir,
        read_state=read_state,
    )
    return result


def _read_file_range_impl(
    tool_input: dict,
    sandbox_dir: Path,
    *,
    read_state: dict[str, float] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Read a numbered line range from a sandbox file.

    This is the production alternative to helper scripts like
    ``show_lines.py`` or ``tail_file.py``. It keeps large-file inspection
    cheap and gives the builder precise citations for reflection evidence.
    """

    raw_filename = tool_input.get("filename", "")
    relative_path = _resolve_sandbox_filename(raw_filename)
    if not relative_path:
        return (
            "Error: invalid filename. Use a bare filename or a path inside "
            f"an allowlisted subdirectory ({sorted(_ALLOWED_SUBDIRS)})."
        ), {"deduped_read": False}

    try:
        start = int(tool_input.get("start", tool_input.get("start_line", 1)))
        end = int(tool_input.get("end", tool_input.get("end_line", start + 119)))
    except (TypeError, ValueError):
        return "Error: start/end must be integers", {"deduped_read": False}
    start = max(1, start)
    end = max(start, end)
    if end - start + 1 > 300:
        end = start + 299

    target = sandbox_dir / relative_path
    if not target.exists():
        return f"Error: '{relative_path}' does not exist in sandbox", {
            "deduped_read": False,
            "path": relative_path,
        }

    try:
        current_mtime = target.stat().st_mtime
        read_key = f"range:{start}:{end}"
        if read_state is not None and _read_state_has_exact_read(
            read_state.get(relative_path),
            current_mtime,
            read_key,
        ):
            return FILE_UNCHANGED_STUB, {
                "deduped_read": True,
                "path": relative_path,
                "read_key": read_key,
                "range": f"{start}-{end}",
            }
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return f"Error reading {relative_path}: {exc}", {
            "deduped_read": False,
            "path": relative_path,
        }

    if not lines:
        return f"{relative_path} is empty", {
            "deduped_read": False,
            "path": relative_path,
            "read_key": read_key,
            "range": f"{start}-{end}",
        }
    if start > len(lines):
        return (
            f"{relative_path} has {len(lines)} lines; requested start line {start}",
            {
                "deduped_read": False,
                "path": relative_path,
                "read_key": read_key,
                "range": f"{start}-{end}",
            },
        )
    end = min(end, len(lines))
    if read_state is not None:
        _mark_read_state(
            read_state,
            relative_path,
            current_mtime,
            "read",
            read_key=read_key,
        )
    width = len(str(end))
    body = "\n".join(
        f"{idx:>{width}} | {lines[idx - 1]}" for idx in range(start, end + 1)
    )
    return f"{relative_path}:{start}-{end} ({len(lines)} total lines)\n{body}", {
        "deduped_read": False,
        "path": relative_path,
        "read_key": read_key,
        "range": f"{start}-{end}",
    }


def read_forensics(
    tool_input: dict,
    sandbox_dir: Path,
) -> str:
    """Read the last N events from harness_forensics.jsonl.

    The forensics shim auto-injected by sandbox setup writes structured
    JSONL events here whenever the harness runs. After running smoke_test
    or live_test, the builder calls this tool to inspect what happened —
    HTTP/WS calls (auto-instrumented), explicit `traced_op`/`log` events,
    thread errors, and (when present) faulthandler stack dumps.

    Use this INSTEAD of re-running the harness when diagnosing a hang or
    failure: the forensics file already has the evidence.

    Returns the last `last_n` lines of the JSONL file (default 50), one
    JSON event per line. Empty when the harness hasn't run yet.
    """
    last_n = max(1, int(tool_input.get("last_n", 50)))
    log_path = sandbox_dir / "harness_forensics.jsonl"
    if not log_path.exists():
        return (
            "(no forensic log yet — run live_test.py or smoke_test.py first. "
            "The log appears at harness_forensics.jsonl after the harness has "
            "run at least once.)"
        )
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return f"Error reading harness_forensics.jsonl: {exc}"
    if not lines:
        return "(harness_forensics.jsonl is empty)"
    tail = lines[-last_n:]
    return "\n".join(tail)


def _load_forensic_events(sandbox_dir: Path) -> tuple[list[dict], list[str] | None]:
    log_path = sandbox_dir / "harness_forensics.jsonl"
    if not log_path.exists():
        return [], ["(no forensic log yet)"]
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"Error reading harness_forensics.jsonl: {exc}"]
    events: list[dict] = []
    bad = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    notes = [f"ignored {bad} malformed JSONL lines"] if bad else None
    return events, notes


_FORENSICS_OUTPUT_TOKENS = (
    "audio",
    "text",
    "transcript",
    "response",
    "delta",
    "message",
    "agent_response",
    "message_done",
    "response_done",
    "output",
    "content",
)
_FORENSICS_CONTROL_TOKENS = (
    "ping",
    "pong",
    "heartbeat",
    "keepalive",
    "keep_alive",
    "metadata",
    "ack",
)
_FORENSICS_LIFECYCLE_TOKENS = (
    "open",
    "close",
    "closed",
    "connect",
    "disconnect",
    "session",
    "initiation",
    "op_done",
)
_FORENSICS_ERROR_TOKENS = (
    "error",
    "exception",
    "failed",
    "failure",
    "unauthorized",
    "forbidden",
)


def _forensics_event_label(event: dict) -> str:
    return str(
        event.get("type")
        or event.get("event_type")
        or event.get("event")
        or event.get("op")
        or ""
    ).lower()


def _forensics_event_text(event: dict) -> str:
    try:
        return json.dumps(event, ensure_ascii=False, default=str).lower()
    except TypeError:
        return str(event).lower()


def _is_stream_forensics_event(event: dict) -> bool:
    label = _forensics_event_label(event)
    return (
        event.get("event") == "stream_event"
        or (
            str(event.get("kind") or "").lower() in {"ws", "stream", "event"}
            and any(token in label for token in (*_FORENSICS_OUTPUT_TOKENS, *_FORENSICS_CONTROL_TOKENS))
        )
    )


def _stream_forensics_class(event: dict) -> str:
    label = _forensics_event_label(event)
    text = _forensics_event_text(event)
    if any(token in label or token in text for token in _FORENSICS_ERROR_TOKENS):
        return "error"
    if any(token in label for token in _FORENSICS_OUTPUT_TOKENS):
        return "output"
    if any(token in label for token in _FORENSICS_CONTROL_TOKENS):
        return "control"
    if any(token in label for token in _FORENSICS_LIFECYCLE_TOKENS):
        return "lifecycle"
    return "unknown"


def _compact_stream_event(event: dict | None) -> str:
    if not isinstance(event, dict):
        return "(none)"
    keys = ("event", "kind", "dir", "type", "event_type", "op", "t_ms", "duration_ms", "bytes")
    return json.dumps({key: event.get(key) for key in keys if key in event}, sort_keys=True)


def _stream_timeout_relation(filtered: list[dict], output_events: list[dict], control_events: list[dict]) -> str:
    timeout_observed = any(
        "timeout" in _forensics_event_text(event) or "timed out" in _forensics_event_text(event)
        for event in filtered[-50:]
    )
    stream_events = [event for event in filtered if _is_stream_forensics_event(event)]
    if not timeout_observed:
        return "not_timeout"
    if not stream_events:
        return "timeout_without_stream_evidence"
    if not output_events:
        return "timeout_after_only_control" if control_events else "timeout_before_output"
    last_output_index = max(stream_events.index(event) for event in output_events)
    tail_classes = {
        _stream_forensics_class(event)
        for event in stream_events[last_output_index + 1:]
        if _stream_forensics_class(event) not in {"lifecycle", "unknown"}
    }
    if tail_classes <= {"control"}:
        return "timeout_after_output_then_control"
    return "timeout_after_output_with_mixed_events"


def summarize_forensics(tool_input: dict, sandbox_dir: Path) -> str:
    """Return compact event counts and high-signal forensics diagnostics."""

    events, notes = _load_forensic_events(sandbox_dir)
    if not events:
        return "\n".join(notes or ["(harness_forensics.jsonl is empty)"])

    filters = tool_input.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    filtered = events
    for key in ("event", "kind", "op", "turn_index", "session_id"):
        if key in filters and filters[key] is not None:
            expected = str(filters[key])
            filtered = [e for e in filtered if str(e.get(key)) == expected]
    last_n = tool_input.get("last_n")
    if last_n is not None:
        try:
            filtered = filtered[-max(1, int(last_n)):]
        except (TypeError, ValueError):
            pass

    event_counts = Counter(str(e.get("event", "<missing>")) for e in filtered)
    op_counts = Counter(str(e.get("op")) for e in filtered if e.get("op"))
    kind_counts = Counter(str(e.get("kind")) for e in filtered if e.get("kind"))
    errors = [
        e for e in filtered
        if "error" in e or str(e.get("event", "")).endswith("_error")
        or str(e.get("event", "")).lower() in {"thread_error", "op_error"}
    ]
    session_events = [
        e for e in filtered
        if str(e.get("event")) in {"session_create", "session_reuse", "session_reconnect", "session_close"}
    ]
    creates_after_turn0 = [
        e for e in session_events
        if e.get("event") == "session_create"
        and isinstance(e.get("turn_index"), int)
        and e.get("turn_index", 0) > 0
    ]
    stream_events = [e for e in filtered if _is_stream_forensics_event(e)]
    stream_class_counts = Counter(_stream_forensics_class(e) for e in stream_events)
    stream_output = [e for e in stream_events if _stream_forensics_class(e) == "output"]
    keepalive_events = [e for e in stream_events if _stream_forensics_class(e) == "control"]

    def _top(counter: Counter, n: int = 8) -> str:
        if not counter:
            return "(none)"
        return ", ".join(f"{k}={v}" for k, v in counter.most_common(n))

    lines = [
        f"forensics events: total={len(events)} filtered={len(filtered)}",
        f"event_counts: {_top(event_counts)}",
        f"kind_counts: {_top(kind_counts)}",
        f"op_counts: {_top(op_counts)}",
        f"errors: {len(errors)}",
        f"session_events: {len(session_events)}",
        (
            "stream_events: "
            f"output={len(stream_output)}, control={len(keepalive_events)}, "
            f"lifecycle={stream_class_counts.get('lifecycle', 0)}, "
            f"error={stream_class_counts.get('error', 0)}, "
            f"unknown={stream_class_counts.get('unknown', 0)}"
        ),
        f"stream_timeout_relation: {_stream_timeout_relation(filtered, stream_output, keepalive_events)}",
        f"last_meaningful_output_event: {_compact_stream_event(stream_output[-1] if stream_output else None)}",
        f"last_control_event: {_compact_stream_event(keepalive_events[-1] if keepalive_events else None)}",
    ]
    if creates_after_turn0:
        turns = sorted({e.get("turn_index") for e in creates_after_turn0})
        lines.append(
            "WARNING: session_create appeared after turn 0 at turns "
            f"{turns}; this suggests per-turn session resets unless paired with reconnect errors."
        )
    if keepalive_events and not stream_output:
        lines.append(
            "WARNING: log contains control/keepalive stream events but no output-bearing stream events."
        )
    if errors:
        lines.append("recent_errors:")
        for e in errors[-5:]:
            event = e.get("event", "<missing>")
            op = e.get("op", "")
            err = e.get("error") or e.get("message") or e.get("error_type") or ""
            lines.append(f"- {event} {op}: {str(err)[:240]}")
    if session_events:
        lines.append("recent_session_events:")
        for e in session_events[-8:]:
            lines.append(
                "- "
                + json.dumps(
                    {
                        "event": e.get("event"),
                        "turn_index": e.get("turn_index"),
                        "session_id": e.get("session_id"),
                        "reason": e.get("reason"),
                    },
                    sort_keys=True,
                )
            )
    if notes:
        lines.extend(notes)
    return "\n".join(lines)


def summarize_build_state(tool_input: dict, sandbox_dir: Path) -> str:
    """Summarize sandbox files, runtime state, and recent diagnostic evidence."""

    important = [
        "requirements.txt",
        "harness.py",
        "smoke_test.py",
        "live_test.py",
        "harness_forensics.jsonl",
        "_agent_state/objective.md",
        "_agent_state/business_fixture.json",
        "_agent_state/runtime_state.json",
        "_agent_state/runtime_snapshot.json",
        "_agent_state/docs_entrypoint.json",
        "_agent_state/research_handoff.json",
        "_agent_state/research_plan.json",
        "_agent_state/research_synthesis.json",
        "_agent_state/implementation_plan.json",
        "_agent_state/latest_failure_packet.json",
        "_agent_state/code_diagnostics.json",
        "_agent_state/abandon_candidate.json",
        "_agent_state/build_decisions.jsonl",
        "_agent_state/research_terminal_urls.json",
        "_agent_state/implementation_plan.json",
        "_agent_state/reflection_phase_3.md",
        "_agent_state/build_plan.md",
    ]
    lines = ["build_state:"]
    for rel in important:
        path = sandbox_dir / rel
        if not path.exists():
            lines.append(f"- {rel}: missing")
            continue
        try:
            stat = path.stat()
            lines.append(f"- {rel}: {stat.st_size} bytes, mtime={stat.st_mtime:.3f}")
        except OSError as exc:
            lines.append(f"- {rel}: stat error: {exc}")

    runtime_path = sandbox_dir / "_agent_state" / "runtime_state.json"
    if runtime_path.exists():
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            snapshot = {
                key: runtime.get(key)
                for key in (
                    "current_phase",
                    "current_turn",
                    "current_model",
                    "smoke_test_status",
                    "live_test_status",
                    "live_passed_at_turn",
                    "verification_passed",
                    "completion_gate_status",
                    "completion_gate_issues",
                    "external_provider_status",
                    "directives_fired",
                    "context_compactions",
                )
            }
            lines.append("runtime_state:")
            lines.append(json.dumps(snapshot, indent=2, sort_keys=True)[:4000])
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"runtime_state: unreadable ({exc})")

    snapshot_path = sandbox_dir / "_agent_state" / "runtime_snapshot.json"
    if snapshot_path.exists():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            lines.append("runtime_snapshot:")
            lines.append(json.dumps({
                "git_sha": snapshot.get("git_sha"),
                "tools_module": snapshot.get("tools_module"),
                "allowed_extensions": snapshot.get("allowed_extensions"),
                "gate_flags": snapshot.get("gate_flags"),
                "migration_flags": snapshot.get("migration_flags"),
            }, indent=2, sort_keys=True)[:3000])
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"runtime_snapshot: unreadable ({exc})")

    docs_entrypoint_path = sandbox_dir / "_agent_state" / "docs_entrypoint.json"
    if docs_entrypoint_path.exists():
        try:
            docs_entrypoint = json.loads(docs_entrypoint_path.read_text(encoding="utf-8"))
            lines.append("docs_entrypoint:")
            lines.append(json.dumps({
                "docs_verdict": docs_entrypoint.get("docs_verdict"),
                "primary_docs_entrypoint": docs_entrypoint.get("primary_docs_entrypoint"),
                "official_domain": docs_entrypoint.get("official_domain"),
                "alternate_entrypoints": docs_entrypoint.get("alternate_entrypoints", [])[:5],
                "deprecated_or_blocked_urls": docs_entrypoint.get("deprecated_or_blocked_urls", [])[:5],
                "auth_method": docs_entrypoint.get("auth_method"),
                "api_access_method": docs_entrypoint.get("api_access_method"),
                "confidence": docs_entrypoint.get("confidence"),
            }, indent=2, sort_keys=True)[:3000])
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"docs_entrypoint: unreadable ({exc})")

    handoff_path = sandbox_dir / "_agent_state" / "research_handoff.json"
    if handoff_path.exists():
        try:
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            lines.append("research_handoff:")
            lines.append(json.dumps({
                "canonical_docs_urls": handoff.get("canonical_docs_urls", [])[:5],
                "discovered_docs_urls": handoff.get("discovered_docs_urls", [])[:5],
                "prefetched_doc_files": handoff.get("prefetched_doc_files", [])[:8],
                "auth_method": handoff.get("auth_method"),
                "sdk_or_transport": handoff.get("sdk_or_transport"),
                "primary_endpoints": handoff.get("primary_endpoints", [])[:5],
                "docs_resolution": handoff.get("docs_resolution", [])[:5],
                "unresolved_questions": handoff.get("unresolved_questions", [])[:5],
                "dead_or_blocked_urls": handoff.get("dead_or_blocked_urls", [])[:5],
            }, indent=2, sort_keys=True)[:3000])
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"research_handoff: unreadable ({exc})")

    research_plan_path = sandbox_dir / "_agent_state" / "research_plan.json"
    if research_plan_path.exists():
        try:
            from puzzleeval.agents.agent5.research_plan import (
                list_research_findings,
                validate_research_plan_text,
                validate_research_synthesis_text,
            )

            plan_text = research_plan_path.read_text(encoding="utf-8")
            plan = json.loads(plan_text)
            verdict = validate_research_plan_text(plan_text)
            findings = list_research_findings(sandbox_dir)
            synthesis_path = sandbox_dir / "_agent_state" / "research_synthesis.json"
            synthesis_verdict = None
            synthesis_summary = None
            if synthesis_path.exists():
                synthesis = json.loads(synthesis_path.read_text(encoding="utf-8"))
                synthesis_verdict = validate_research_synthesis_text(
                    json.dumps(synthesis, ensure_ascii=False, default=str)
                ).to_dict()
                synthesis_summary = {
                    "provider_doc_map_topics": [
                        item.get("topic")
                        for item in (synthesis.get("provider_doc_map") or [])
                        if isinstance(item, dict)
                    ][:10],
                    "chosen_api_surface": synthesis.get("chosen_api_surface"),
                    "credential_model": synthesis.get("credential_model"),
                    "input_compatibility": synthesis.get("input_compatibility"),
                    "routing_table": synthesis.get("routing_table"),
                    "errors_and_limits": synthesis.get("errors_and_limits"),
                    "sdk_package": synthesis.get("sdk_package"),
                    "unresolved_questions": (synthesis.get("unresolved_questions") or [])[:5],
                }
            lines.append("planned_research:")
            lines.append(json.dumps({
                "plan_validator": verdict.to_dict(),
                "docs_entrypoint": plan.get("docs_entrypoint"),
                "research_task_ids": [
                    task.get("id") for task in (plan.get("research_tasks") or [])
                    if isinstance(task, dict)
                ][:10],
                "finding_count": len(findings),
                "finding_task_ids": [item.get("task_id") for item in findings][:10],
                "synthesis_validator": synthesis_verdict,
                "synthesis_summary": synthesis_summary,
            }, indent=2, sort_keys=True)[:3000])
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"planned_research: unreadable ({exc})")

    implementation_plan_path = sandbox_dir / "_agent_state" / "implementation_plan.json"
    if implementation_plan_path.exists():
        try:
            from puzzleeval.agents.agent5.objective_validator import (
                validate_implementation_plan_file,
            )

            plan = json.loads(implementation_plan_path.read_text(encoding="utf-8"))
            verdict = validate_implementation_plan_file(sandbox_dir)
            lines.append("implementation_plan:")
            lines.append(json.dumps({
                "objective_coverage_count": len(plan.get("objective_coverage") or []),
                "objective_validator": verdict.to_dict(),
            }, indent=2, sort_keys=True)[:3000])
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"implementation_plan: unreadable ({exc})")

    abandon_path = sandbox_dir / "_agent_state" / "abandon_candidate.json"
    if abandon_path.exists():
        try:
            from puzzleeval.agents.agent5.abandon_candidate import (
                validate_abandon_candidate_text,
            )

            abandon_text = abandon_path.read_text(encoding="utf-8")
            verdict = validate_abandon_candidate_text(
                abandon_text,
                sandbox_dir=sandbox_dir,
            )
            data = verdict.data or {}
            lines.append("abandon_candidate:")
            lines.append(json.dumps({
                "validator": verdict.to_dict(),
                "reason": data.get("reason"),
                "summary": str(data.get("summary") or data.get("explanation") or "")[:500],
            }, indent=2, sort_keys=True)[:3000])
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"abandon_candidate: unreadable ({exc})")

    try:
        from puzzleeval.agents.agent5.code_diagnostics import summarize_code_diagnostics

        code_diag = summarize_code_diagnostics(sandbox_dir)
        if code_diag.get("active_count") or code_diag.get("latest_new_count"):
            lines.append("code_diagnostics:")
            lines.append(json.dumps(code_diag, indent=2, sort_keys=True)[:3000])
    except Exception:  # noqa: BLE001 - summary tool must stay best-effort
        pass

    try:
        from puzzleeval.agents.agent5.failure_packets import (
            read_latest_failure_packet,
            summarize_failure_packets,
        )
        packet_summary = summarize_failure_packets(sandbox_dir)
        if packet_summary.get("count"):
            lines.append("failure_packets:")
            lines.append(json.dumps(packet_summary, indent=2, sort_keys=True)[:3000])
        latest_packet = read_latest_failure_packet(sandbox_dir)
        if latest_packet:
            diagnosis = latest_packet.get("diagnosis") if isinstance(latest_packet.get("diagnosis"), dict) else {}
            lines.append("latest_failure_packet:")
            lines.append(json.dumps({
                "turn": latest_packet.get("turn"),
                "failure_source": latest_packet.get("failure_source"),
                "exit_code": latest_packet.get("exit_code"),
                "mechanical_tags": latest_packet.get("mechanical_tags"),
                "observed_failure": diagnosis.get("observed_failure"),
                "likely_root_cause": diagnosis.get("likely_root_cause"),
                "next_diagnostic_or_patch": diagnosis.get("next_diagnostic_or_patch"),
                "research_would_change_implementation": diagnosis.get("research_would_change_implementation"),
                "files_changed_since_last_test": latest_packet.get("files_changed_since_last_test"),
                "issues": latest_packet.get("issues"),
            }, indent=2, sort_keys=True)[:3000])
    except Exception:  # noqa: BLE001 - summary tool must stay best-effort
        pass

    try:
        from puzzleeval.agents.agent5.build_decisions import read_recent_build_decisions
        decisions = read_recent_build_decisions(sandbox_dir, limit=8)
        if decisions:
            lines.append("recent_build_decisions:")
            lines.append(json.dumps(decisions, indent=2, sort_keys=True)[:3000])
    except Exception:  # noqa: BLE001
        pass

    try:
        from puzzleeval.agents.agent5.research_memory import summarize_terminal_research_urls

        terminal_urls = summarize_terminal_research_urls(sandbox_dir)
        if terminal_urls.get("count"):
            lines.append("terminal_research_urls:")
            lines.append(json.dumps(terminal_urls, indent=2, sort_keys=True)[:3000])
    except Exception:  # noqa: BLE001
        pass

    events, notes = _load_forensic_events(sandbox_dir)
    if events:
        event_counts = Counter(str(e.get("event", "<missing>")) for e in events)
        lines.append(
            "forensics_summary: "
            + ", ".join(f"{k}={v}" for k, v in event_counts.most_common(8))
        )
    elif notes:
        lines.extend(notes)
    return "\n".join(lines)
