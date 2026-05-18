"""Orchestrator-owned runtime state for Agent 5 builds.

The orchestrator writes ``_agent_state/runtime_state.json`` after every
turn. The file is the AGENT's ground-truth view of where it is — agent
reads it via ``read_file('_agent_state/runtime_state.json')`` and
grounds its mental model against it.

This is the STATE pillar of the autonomy architecture (per Codex's
"orchestrator owns state" critique — the agent's self-written state
must NEVER become the source of truth, only orchestrator state can).

The file is WRITE-PROTECTED — Agent 5 attempting to write it via
``write_file`` is rejected at the tool dispatch boundary
(see ``puzzleeval.agents.agent5.tools``).

PR 3 future use: the orchestrator's directive-suppression check compares
this file (truth) against ``_agent_state/agent_observations.json``
(agent's claim). Suppress redirect only when BOTH agree. If they
disagree, fire the directive (defensive default — agent's mental model
is wrong, correct it).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from puzzleeval.agents.agent5.build_loop import BuildLoopState
    from puzzleeval.schemas import Agent5Input, ScreenedCandidate


RUNTIME_STATE_FILENAME = "runtime_state.json"
RUNTIME_SNAPSHOT_FILENAME = "runtime_snapshot.json"
"""Canonical filename inside the ``_agent_state/`` directory."""


def _agent_state_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / "_agent_state"


def _runtime_state_path(sandbox_dir: Path) -> Path:
    return _agent_state_dir(sandbox_dir) / RUNTIME_STATE_FILENAME


def _runtime_snapshot_path(sandbox_dir: Path) -> Path:
    return _agent_state_dir(sandbox_dir) / RUNTIME_SNAPSHOT_FILENAME


# Filenames the orchestrator considers when computing files_present /
# files_pending. The list is intentionally focused on the canonical
# build artifacts the agent must produce; we do NOT enumerate every
# possible file (e.g., test_inputs/, fetched_docs_*) to keep the
# state file readable + bounded.
_TRACKED_REQUIRED_FILES = (
    "_agent_state/research_plan.json",
    "_agent_state/research_synthesis.json",
    "_agent_state/implementation_plan.json",
    "requirements.txt",
    "harness.py",
    "smoke_test.py",
    "live_test.py",
)


def _scan_files_present(sandbox_dir: Path) -> tuple[list[str], list[str]]:
    """Return ``(files_present, files_pending)`` from the canonical set.

    Pure function — reads the filesystem only. Does NOT mutate.
    """
    present: list[str] = []
    pending: list[str] = []
    for fname in _TRACKED_REQUIRED_FILES:
        if (sandbox_dir / fname).exists():
            present.append(fname)
        else:
            pending.append(fname)
    return present, pending


def derive_current_phase(
    sandbox_dir: Path,
    state: "BuildLoopState",
) -> str:
    """Compute the current phase from observable state.

    The phases mirror the builder's 4-phase choreography:
      - phase_1_research: active build gate not satisfied
      - phase_2_build: build gate satisfied, harness.py absent
      - phase_3_verify: harness.py present, smoke not yet passed
      - phase_4_deliver: smoke passed (or verification_passed)

    Pure function. No mutation.
    """
    build_gate_satisfied = bool(
        getattr(
            state,
            "implementation_plan_accepted",
            getattr(state, "build_gate_accepted", False),
        )
    )

    if not build_gate_satisfied:
        return "phase_1_research"
    if not (sandbox_dir / "harness.py").exists():
        return "phase_2_build"
    if not state.smoke_ever_passed:
        return "phase_3_verify"
    return "phase_4_deliver"


def init_runtime_state(
    sandbox_dir: Path,
    candidate: "ScreenedCandidate",
    input_data: "Agent5Input",
    effective_max_turns: int,
    effective_max_budget_usd: float,
    initial_model: str,
) -> dict[str, Any]:
    """Write the initial ``runtime_state.json`` for a fresh build.

    Called from the build_loop setup BEFORE turn 0. Returns the dict
    that was written (so callers can log a snapshot if needed).

    The directory ``sandbox_dir / "_agent_state"`` is created if missing.
    """
    from puzzleeval import config

    state_dir = _agent_state_dir(sandbox_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    files_present, files_pending = _scan_files_present(sandbox_dir)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "candidate_name": candidate.name,
        "trace_id": input_data.trace_id,
        "current_phase": "phase_1_research",
        "phase_entered_at_turn": 0,
        "current_turn": 0,
        "current_model": initial_model,
        "lead_model": config.AGENT5_BUILDER_MODEL,
        "research_worker_model": config.RESEARCH_MODEL,
        "effective_max_turns": effective_max_turns,
        "effective_max_budget_usd": effective_max_budget_usd,
        "accumulated_cost_usd": 0.0,
        "files_present": files_present,
        "files_pending": files_pending,
        "smoke_test_status": "not_run",
        "live_test_status": "not_run",
        "live_passed_at_turn": None,
        "last_live_test_output": None,
        "completion_gate_status": "not_signaled",
        "completion_gate_issues": [],
        "external_provider_status": None,
        "errors_encountered_this_turn": [],
        "errors_history": [],
        "directives_fired": [],
        "context_compactions": [],
        "verification_attempts": 0,
        "verification_passed": False,
        "implementation_plan_accepted": False,
        "migration_flags": config.migration_flags_snapshot(),
        "last_updated_t_abs": time.time(),
    }
    _runtime_state_path(sandbox_dir).write_text(
        json.dumps(payload, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    return payload


def write_runtime_snapshot(sandbox_dir: Path) -> dict[str, Any]:
    """Persist active code/config facts for stale-runtime diagnosis.

    Real runs can use an already-running backend process. This snapshot lets
    postmortems prove which code path and gate flags were active for a build
    instead of assuming the edited source on disk was loaded.
    """
    from puzzleeval import config
    from puzzleeval.agents.agent5 import tools

    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:  # noqa: BLE001 - snapshot is best effort
        git_sha = None

    payload: dict[str, Any] = {
        "schema_version": 1,
        "git_sha": git_sha,
        "runtime_state_module": str(Path(__file__).resolve()),
        "runtime_state_module_path": str(Path(__file__).resolve()),
        "tools_module": str(Path(tools.__file__).resolve()),
        "tools_module_path": str(Path(tools.__file__).resolve()),
        "allowed_extensions": sorted(tools.ALLOWED_EXTENSIONS),
        "orchestrator_owned_artifacts": sorted(tools.ORCHESTRATOR_OWNED_ARTIFACTS),
        "gate_flags": {
            "forensics_coverage": config.GATE_FORENSICS_COVERAGE_ENABLED,
            "autonomy_artifacts": config.GATE_AUTONOMY_ARTIFACTS_ENABLED,
            "reflection_phase_3": config.GATE_REFLECTION_PHASE_3_ENABLED,
            "context_compaction": config.CONTEXT_COMPACTION_AT_BUILD_GATE_ENABLED,
            "persistent_harness_runner": config.PERSISTENT_HARNESS_RUNNER_ENABLED,
            "build_gate_compaction": config.CONTEXT_COMPACTION_AT_BUILD_GATE_ENABLED,
        },
        "migration_flags": config.migration_flags_snapshot(),
        "config": {
            "agent5_builder_model": config.AGENT5_BUILDER_MODEL,
            "research_worker_model": config.RESEARCH_MODEL,
            "agent5_max_verification_retries": config.AGENT5_MAX_VERIFICATION_RETRIES,
            "agent5_max_turns": config.AGENT5_MAX_TURNS,
            "agent5_max_turns_voice": config.AGENT5_MAX_TURNS_VOICE,
        },
        "written_t_abs": time.time(),
    }
    state_dir = _agent_state_dir(sandbox_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    _runtime_snapshot_path(sandbox_dir).write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return payload


def update_runtime_state(
    sandbox_dir: Path,
    state: "BuildLoopState",
    *,
    current_model: str,
    smoke_test_status: str | None = None,
    live_test_status: str | None = None,
    live_passed_at_turn: int | None = None,
    last_live_test_output: str | None = None,
    completion_gate_status: str | None = None,
    completion_gate_issues: list[str] | None = None,
    external_provider_status: str | None = None,
    errors_encountered_this_turn: list[dict[str, Any]] | None = None,
    directive_fired: dict[str, Any] | None = None,
    context_compaction_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update ``runtime_state.json`` with current state.

    Called from the build_loop AFTER each turn's tool dispatch completes,
    so files_present reflects what the just-completed turn actually wrote.

    Args:
        sandbox_dir: The candidate's sandbox.
        state: The current BuildLoopState.
        current_model: Model used in the just-completed turn.
        smoke_test_status: One of "not_run", "passing", "failing"; pass
            None to leave unchanged from the prior write.
        live_test_status: Same shape as smoke_test_status.
        completion_gate_status: Human-readable completion-gate status to
            persist for operators and final report assembly.
        errors_encountered_this_turn: List of error dicts observed in
            this turn. Pass empty list to clear; pass None to leave
            unchanged. Each entry should have "category" + "message" at
            minimum.
        directive_fired: When the orchestrator fired a directive THIS
            turn, pass the entry dict so it's
            appended to ``directives_fired``. None means no directive.
        context_compaction_event: Same shape as directive_fired but for
            context-compaction events.

    Returns:
        The dict that was written.
    """
    state_path = _runtime_state_path(sandbox_dir)

    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    else:
        existing = {}

    files_present, files_pending = _scan_files_present(sandbox_dir)
    current_phase = derive_current_phase(sandbox_dir, state)

    # Phase-entered-at-turn tracking: bump only when we transition.
    prior_phase = existing.get("current_phase")
    if prior_phase != current_phase:
        phase_entered_at_turn = state.turn
    else:
        phase_entered_at_turn = existing.get("phase_entered_at_turn", state.turn)

    payload: dict[str, Any] = {
        **existing,
        "current_phase": current_phase,
        "phase_entered_at_turn": phase_entered_at_turn,
        "current_turn": state.turn,
        "current_model": current_model,
        "accumulated_cost_usd": round(state.accumulated_cost, 4),
        "files_present": files_present,
        "files_pending": files_pending,
        "verification_attempts": state.verification_attempts,
        "verification_passed": state.verification_passed,
        "implementation_plan_accepted": bool(
            getattr(
                state,
                "implementation_plan_accepted",
                getattr(state, "build_gate_accepted", False),
            )
        ),
        "smoke_ever_passed": state.smoke_ever_passed,
        "smoke_passed_at_turn": state.smoke_passed_at_turn,
        "consecutive_errors": state.consecutive_errors,
        "total_reassessments": state.total_reassessments,
        "last_updated_t_abs": time.time(),
    }

    if smoke_test_status is not None:
        payload["smoke_test_status"] = smoke_test_status
    elif state.smoke_ever_passed:
        payload["smoke_test_status"] = "passing"
    if live_test_status is not None:
        payload["live_test_status"] = live_test_status
    elif state.verification_passed:
        payload["live_test_status"] = "passing"
    if live_passed_at_turn is not None:
        payload["live_passed_at_turn"] = live_passed_at_turn
    if last_live_test_output is not None:
        payload["last_live_test_output"] = last_live_test_output[-2000:]
    if completion_gate_status is not None:
        payload["completion_gate_status"] = completion_gate_status
    if completion_gate_issues is not None:
        payload["completion_gate_issues"] = list(completion_gate_issues)[-20:]
    if external_provider_status is not None:
        payload["external_provider_status"] = external_provider_status
    if errors_encountered_this_turn is not None:
        payload["errors_encountered_this_turn"] = errors_encountered_this_turn
        # Roll into history
        history = list(existing.get("errors_history", []))
        for err in errors_encountered_this_turn:
            history.append({"turn": state.turn, **err})
        # Cap history to last 50 entries to keep file size bounded
        payload["errors_history"] = history[-50:]
    if directive_fired is not None:
        directives = list(existing.get("directives_fired", []))
        directives.append({"turn": state.turn, **directive_fired})
        payload["directives_fired"] = directives[-20:]
    if context_compaction_event is not None:
        compactions = list(existing.get("context_compactions", []))
        compactions.append({"turn": state.turn, **context_compaction_event})
        payload["context_compactions"] = compactions[-10:]

    # Atomic write — write to temp file then replace, so a partial-write
    # crash doesn't leave the file in a half-parsed state the agent sees.
    tmp_path = state_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    tmp_path.replace(state_path)
    return payload


def read_runtime_state(sandbox_dir: Path) -> dict[str, Any] | None:
    """Read and parse ``runtime_state.json``. Returns None if absent or invalid.

    Used by the orchestrator (NOT by the agent — the agent reads via
    ``read_file`` tool to keep the access path uniform).
    """
    state_path = _runtime_state_path(sandbox_dir)
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_agent_observations(sandbox_dir: Path) -> dict[str, Any] | None:
    """Read and parse the agent's optional ``agent_observations.json``.

    Used by PR 3's agreement-check logic. Never authoritative — only
    advisory. Returns None when the agent hasn't written one yet.
    """
    obs_path = _agent_state_dir(sandbox_dir) / "agent_observations.json"
    if not obs_path.exists():
        return None
    try:
        return json.loads(obs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# PR 3 — agreement check between orchestrator truth and agent observation
# ---------------------------------------------------------------------------
# The orchestrator's authoritative state lives in BuildLoopState (in-memory)
# + ``runtime_state.json`` (disk mirror). The agent's claimed mental model
# lives in ``agent_observations.json`` (advisory, never authoritative).
#
# Before firing a redundant build-gate directive, the
# orchestrator can check: "does the agent already know we're at this
# phase?" If YES, suppress the directive (telemetry: agreed). If NO or
# UNSURE, fire the directive (telemetry: disagreed / no_observation).
#
# This is the load-bearing demotion of prompt-injection state-management
# to a backstop (PR 3). When the agent reliably writes phase observations,
# most directive injections become no-ops — saving cost AND avoiding the
# narrative-inertia failure mode where a redundant injection competes
# with prior conversation context.


_PHASE_ALIASES = {
    "phase_1_research": ("phase_1_research", "phase 1", "phase_1", "phase1", "research"),
    "phase_2_build": ("phase_2_build", "phase 2", "phase_2", "phase2", "build", "scaffold"),
    "phase_3_verify": ("phase_3_verify", "phase 3", "phase_3", "phase3", "verify", "live test"),
    "phase_4_deliver": ("phase_4_deliver", "phase 4", "phase_4", "phase4", "deliver", "harness_complete", "complete"),
}


def evaluate_agent_phase_agreement(
    sandbox_dir: Path,
    orchestrator_phase: str,
) -> tuple[str, str | None]:
    """Return ``(verdict, agent_note)`` comparing agent observation to
    orchestrator's current phase belief.

    Verdict is one of:
        ``"agreed"``         — agent's most recent phase observation
                               matches the orchestrator's current phase.
        ``"disagreed"``      — agent's most recent phase observation
                               disagrees (e.g. agent thinks phase_1 but
                               orchestrator transitioned to phase_2).
        ``"no_observation"`` — agent_observations.json is missing OR has
                               no observation with ``category == "phase"``.

    ``agent_note`` is the most recent phase observation's ``note`` field
    (or None when no_observation).

    Pure function — reads agent_observations.json only. Never raises.

    Recognized agent observation shapes:
        {"category": "phase", "phase": "phase_2_build", "note": "..."}  ← preferred
        {"category": "phase", "note": "I'm in phase 2 now"}             ← fallback parses the note

    Unknown phases are treated as ``no_observation`` (defensive default
    — orchestrator fires the directive when unsure).
    """
    # Defensive: if the orchestrator phase isn't recognized, we can't
    # meaningfully evaluate agreement. Return no_observation so callers
    # default to firing the directive (the safer choice when unsure).
    if orchestrator_phase not in _PHASE_ALIASES:
        return ("no_observation", None)

    obs = read_agent_observations(sandbox_dir)
    if not isinstance(obs, dict):
        return ("no_observation", None)
    observations = obs.get("observations")
    if not isinstance(observations, list):
        return ("no_observation", None)

    phase_obs = [
        o for o in observations
        if isinstance(o, dict) and o.get("category") == "phase"
    ]
    if not phase_obs:
        return ("no_observation", None)

    latest = phase_obs[-1]
    agent_note = str(latest.get("note", "")) or None

    # Preferred: structured ``phase`` field
    structured_phase = str(latest.get("phase", "")).strip()
    if structured_phase:
        if structured_phase == orchestrator_phase:
            return ("agreed", agent_note)
        return ("disagreed", agent_note)

    # Fallback: parse the note for phase aliases
    note_lower = (agent_note or "").lower()
    expected_aliases = _PHASE_ALIASES.get(orchestrator_phase, ())
    for alias in expected_aliases:
        if alias in note_lower:
            return ("agreed", agent_note)
    # If the note mentions a DIFFERENT phase, we can flag disagreement.
    for phase, aliases in _PHASE_ALIASES.items():
        if phase == orchestrator_phase:
            continue
        for alias in aliases:
            if alias in note_lower:
                return ("disagreed", agent_note)
    # Note doesn't mention any phase explicitly — treat as no_observation
    return ("no_observation", agent_note)


__all__ = [
    "RUNTIME_STATE_FILENAME",
    "derive_current_phase",
    "evaluate_agent_phase_agreement",
    "init_runtime_state",
    "write_runtime_snapshot",
    "update_runtime_state",
    "read_runtime_state",
    "read_agent_observations",
]
