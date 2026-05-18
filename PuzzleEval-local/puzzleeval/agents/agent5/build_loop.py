"""Agent 5 build loop â€” the orchestration spine.

Phase 5 destination â€” this module owns ``build_single_harness(ctx)``,
the autonomous tool-use loop that drives ONE candidate's harness
build. The legacy entry point ``_build_single_harness`` in
``implement_test_env.py`` becomes a thin shim that constructs
``BuildContext`` and delegates here.

Shape of the move:
  * Step 1 (this file at first commit): ``BuildContext`` +
    ``BuildLoopState`` dataclasses + helper unit tests. The dataclasses
    are unused until Step 3.
  * Step 2: ``_initialize_loop_state(ctx, setup)`` extraction.
  * Step 3: ``build_single_harness(ctx)`` lands here, replacing the
    legacy function body.

AD-007: this module is pure backbone. Markdown contracts don't gate
anything here â€” retry math, state mutation, dispatch flow are all
deterministic Python.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import anthropic
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal test envs
    from puzzleeval.anthropic_client import anthropic  # type: ignore

from puzzleeval.agents.agent5.sandbox import candidate_slug
from puzzleeval.logging_setup import log_llm_call
from puzzleeval.schemas import (
    Agent5Input,
    FailedHarness,
    ScreenedCandidate,
    TestHarness,
)
from puzzleeval.web_fetch_fallback import (
    build_fallback_message,
    count_actionable_problems,
    extract_blocked_fetches,
    extract_unusable_pages,
    maybe_apply_rate_limit_backoff,
    summarize_blocks_for_log,
)


def _build_phase_name(*, build_gate_accepted: bool, smoke_ever_passed: bool) -> str:
    if smoke_ever_passed:
        return "validating"
    return "building" if build_gate_accepted else "researching"


_SECRET_ENV_TOKENS: tuple[str, ...] = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
)


def _is_smoke_test_command(command: str) -> bool:
    lowered = (command or "").lower().replace("\\", "/")
    return "smoke_test.py" in lowered


def _offline_smoke_env(credentials: dict[str, str] | None) -> dict[str, str]:
    """Return env overrides that make smoke tests offline/mechanical only."""

    masked = {
        key: ""
        for key in os.environ
        if any(token in key.upper() for token in _SECRET_ENV_TOKENS)
    }
    for key in (credentials or {}):
        masked[str(key)] = ""
    masked["PUZZLEEVAL_SMOKE_OFFLINE"] = "1"
    masked["PUZZLEEVAL_PROVIDER_CALLS_DISABLED"] = "1"
    return masked


def _representative_probe_status(sandbox_dir: Path) -> str:
    path = sandbox_dir / "_agent_state" / "representative_probe_evidence.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("status") or "")


def _select_agent5_model(
    *,
    research_model: str,
    builder_model: str,
) -> str:
    """Select the Agent 5 lead model.

    Agent 5 now uses the builder model from turn 0 for research planning,
    synthesis, implementation planning, build, and debug. ``research_model``
    remains the bounded worker model for planned research and ask_research.
    """

    return builder_model


def _append_build_progress_event(sandbox_dir: Path, payload: dict[str, Any]) -> None:
    """Best-effort JSONL progress breadcrumb for long in-flight turns."""
    try:
        state_dir = sandbox_dir / "_agent_state"
        state_dir.mkdir(exist_ok=True)
        event = {"t_abs": time.time(), **payload}
        with (state_dir / "build_progress.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return


def _completion_gate_issue_key(issues: str | list[str]) -> str:
    """Coarse stable retry bucket for completion-gate failures.

    Retry budgets are per gate issue class, not a global sticky counter. This
    lets a build recover from a real early failure (for example missing
    conversation_history), then pass later after it fixes a different issue
    such as reflection headings.
    """
    text = " ".join(str(i) for i in issues) if isinstance(issues, list) else str(issues)
    lowered = text.lower()
    if "reflection" in lowered:
        reflection_absent = (
            "reflection_phase_3.md is missing" in lowered
            or "reflection_phase_3.md missing" in lowered
            or "reflection file is missing" in lowered
            or "reflection is missing" in lowered
            or "reflection_phase_3.md not found" in lowered
            or "does not exist" in lowered
            or "not found" in lowered
        )
        reflection_present_but_weak = (
            "missing_sections" in lowered
            or "invalid_citations" in lowered
            or "lacks substantive evidence" in lowered
            or "judge_fail" in lowered
            or "borderline" in lowered
            or "present" in lowered
        )
        if reflection_absent and not reflection_present_but_weak:
            return "reflection_missing"
        return "reflection_evidence"
    if "voice" in lowered or "conversation_history" in lowered or "persistent" in lowered:
        return "voice_live_contract"
    if "forensics" in lowered or "_forensics" in lowered or "traced_op" in lowered:
        return "forensics_coverage"
    if "harness.py" in lowered or "structural" in lowered:
        return "structural"
    return "completion_gate_other"


def _start_turn_heartbeat(
    *,
    progress_callback: Callable[[str, dict], None] | None,
    sandbox_dir: Path,
    candidate_name: str,
    turn: int,
    max_turns: int,
    phase: str,
    model: str,
    interval_seconds: float,
) -> Callable[[], None]:
    """Emit liveness events while a single model/tool turn is in flight.

    Anthropic server-side web_fetch / web_search / advisor calls happen inside
    one blocking SDK call, so the normal per-turn event only arrives after the
    expensive work finishes. A daemon heartbeat gives the UI and postmortem log
    a truthful "still waiting on this turn" signal without changing behavior.
    """
    if interval_seconds <= 0:
        return lambda: None

    start = time.monotonic()
    stop = threading.Event()

    def _emit(event_type: str, elapsed_ms: int) -> None:
        payload = {
            "candidate_name": candidate_name,
            "turn": turn + 1,
            "max_turns": max_turns,
            "phase": phase,
            "model": model,
            "elapsed_ms": elapsed_ms,
        }
        _append_build_progress_event(sandbox_dir, {"event": event_type, **payload})
        if progress_callback is not None:
            try:
                progress_callback(event_type, payload)
            except Exception:
                # Progress callbacks are diagnostic; never let UI/SSE plumbing
                # perturb the harness build.
                pass

    def _run() -> None:
        while not stop.wait(interval_seconds):
            _emit("build_turn_heartbeat", int((time.monotonic() - start) * 1000))

    _emit("build_turn_started", 0)
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    def _stop() -> None:
        stop.set()
        thread.join(timeout=0.2)

    return _stop


def _start_tool_heartbeat(
    *,
    progress_callback: Callable[[str, dict], None] | None,
    sandbox_dir: Path,
    candidate_name: str,
    turn: int,
    phase: str,
    tool_name: str,
    tool_summary: str,
    interval_seconds: float,
) -> tuple[Callable[[], None], Callable[[dict[str, Any]], None]]:
    """Emit liveness events while a potentially blocking custom tool runs."""
    if interval_seconds <= 0:
        return (lambda: None, lambda _extra: None)

    start = time.monotonic()
    stop = threading.Event()

    def _payload(event_type: str, elapsed_ms: int, extra: dict[str, Any] | None = None) -> dict:
        payload = {
            "candidate_name": candidate_name,
            "turn": turn + 1,
            "phase": phase,
            "tool": tool_name,
            "tool_summary": tool_summary[:200],
            "elapsed_ms": elapsed_ms,
        }
        if extra:
            payload.update(extra)
        _append_build_progress_event(sandbox_dir, {"event": event_type, **payload})
        return payload

    def _emit(event_type: str, extra: dict[str, Any] | None = None) -> None:
        payload = _payload(event_type, int((time.monotonic() - start) * 1000), extra)
        if progress_callback is not None:
            try:
                progress_callback(event_type, payload)
            except Exception:
                pass

    def _run() -> None:
        while not stop.wait(interval_seconds):
            _emit("build_tool_heartbeat")

    _emit("build_tool_started", {"elapsed_ms": 0})
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    def _stop() -> None:
        stop.set()
        thread.join(timeout=0.2)

    def _complete(extra: dict[str, Any]) -> None:
        _emit("build_tool_completed", extra)

    return _stop, _complete


def _tool_summary_for_progress(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        if tool_name == "run_code":
            return str(tool_input.get("command") or tool_input.get("code") or "")
        if tool_name == "ask_research":
            return str(tool_input.get("question") or "")
        return str(
            tool_input.get("filename")
            or tool_input.get("path")
            or tool_input.get("file_path")
            or ""
        )
    return str(tool_input or "")


def _tool_targets_relative_path(block: Any, relative_path: str) -> bool:
    """True when a write/patch tool targets the sandbox-relative path."""

    if getattr(block, "name", None) not in {"write_file", "patch_file"}:
        return False
    data = getattr(block, "input", None)
    if not isinstance(data, dict):
        return False
    raw = str(data.get("filename") or data.get("path") or "")
    normalized = raw.replace("\\", "/").lstrip("/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == relative_path


# Deterministic build-gate directive. The accepted implementation plan is the
# single transition from research/planning into scaffold writes.
IMPLEMENTATION_PLAN_DIRECTIVE = (
    "_agent_state/implementation_plan.json is accepted. You are in the build "
    "phase. Your IMMEDIATE next response MUST call write_file in one same-turn batch "
    "for: requirements.txt, harness.py, smoke_test.py, live_test.py. Do NOT "
    "narrate the transition. Just call the tools."
)


_CONCURRENCY_SAFE_LOCAL_TOOLS = frozenset({
    "read_file",
    "read_file_range",
    "read_forensics",
    "summarize_forensics",
})
_LOCAL_TOOL_NAMES_FOR_PAIRING = frozenset({
    "write_file",
    "patch_file",
    "run_code",
    "read_file",
    "read_file_range",
    "ask_research",
    "read_forensics",
    "summarize_forensics",
    "summarize_build_state",
})


@dataclass
class _ScheduledLocalToolResult:
    content: str
    exit_code: int
    elapsed_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


def _is_concurrency_safe_local_tool(block: Any) -> bool:
    return (
        getattr(block, "type", None) == "tool_use"
        and getattr(block, "name", None) in _CONCURRENCY_SAFE_LOCAL_TOOLS
    )


def _assistant_content_has_local_tool_uses(content_blocks: list[Any]) -> bool:
    """True when appending a user restoration message would break pairing."""

    for block in content_blocks:
        if isinstance(block, dict):
            block_type = block.get("type")
            block_name = block.get("name")
        else:
            block_type = getattr(block, "type", None)
            block_name = getattr(block, "name", None)
        if block_type == "tool_use" and block_name in _LOCAL_TOOL_NAMES_FOR_PAIRING:
            return True
    return False


def _collect_concurrency_safe_batch(content_blocks: list[Any], start_index: int) -> list[Any]:
    batch: list[Any] = []
    for block in content_blocks[start_index:]:
        if not _is_concurrency_safe_local_tool(block):
            break
        batch.append(block)
    return batch


def _execute_concurrency_safe_tool_batch(
    *,
    blocks: list[Any],
    sandbox_dir: Path,
    credentials: dict[str, str],
    read_state: dict[str, Any],
    phase_state: dict[str, Any],
    code_timeout_s: int,
    max_workers: int = 4,
    dispatch_fn: Callable[..., Any] | None = None,
) -> dict[str, _ScheduledLocalToolResult]:
    """Run one consecutive read-only/local-inspection tool batch concurrently."""

    if dispatch_fn is None:
        from puzzleeval.agents.agent5.tools import dispatch_tool_result

        dispatch_fn = dispatch_tool_result

    def _run(block: Any) -> tuple[str, _ScheduledLocalToolResult]:
        started = time.time()
        try:
            result = dispatch_fn(
                block.name,
                block.input,
                sandbox_dir,
                extra_env=credentials,
                read_state=read_state,
                code_timeout_s=code_timeout_s,
                phase_state=phase_state,
            )
            content = getattr(result, "content", str(result))
            exit_code = int(getattr(result, "exit_code", 0))
            metadata = dict(getattr(result, "metadata", {}) or {})
        except Exception as exc:  # noqa: BLE001 - every tool_use needs a result
            content = f"Error: {block.name} failed during parallel read-only dispatch: {type(exc).__name__}: {str(exc)[:500]}"
            exit_code = -2
            metadata = {"dispatch_exception": type(exc).__name__}
        elapsed_ms = int((time.time() - started) * 1000)
        return str(block.id), _ScheduledLocalToolResult(
            content=content,
            exit_code=exit_code,
            elapsed_ms=elapsed_ms,
            metadata=metadata,
        )

    worker_count = max(1, min(max_workers, len(blocks)))
    if worker_count == 1:
        tool_id, result = _run(blocks[0])
        return {tool_id: result}

    results: dict[str, _ScheduledLocalToolResult] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_run, block) for block in blocks]
        for future in as_completed(futures):
            tool_id, result = future.result()
            results[tool_id] = result
    return results


def _tool_result_evidence_hash(entry: dict[str, Any]) -> str:
    payload = {
        "tool": entry.get("tool"),
        "exit_code": entry.get("exit_code"),
        "command": str(entry.get("command") or "")[:300],
        "result": str(entry.get("result") or "")[-1200:],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _classify_durable_turn_progress(
    *,
    response_content: list[Any],
    tool_result_logs: list[dict[str, Any]],
    build_gate_transitioned: bool,
    smoke_just_passed: bool,
    live_just_passed: bool,
    seen_failure_evidence_hashes: set[str],
) -> tuple[bool, list[str], list[str]]:
    """Return durable progress and low-value activity signals for one turn."""

    durable: set[str] = set()
    low_value: set[str] = set()

    for block in response_content:
        if getattr(block, "type", None) == "server_tool_use":
            durable.add("server_research")

    if build_gate_transitioned:
        durable.add("accepted_implementation_plan")
    if smoke_just_passed:
        durable.add("smoke_passed")
    if live_just_passed:
        durable.add("live_passed")

    for entry in tool_result_logs:
        tool = str(entry.get("tool") or "")
        is_error = bool(entry.get("is_error"))
        if is_error:
            evidence_hash = _tool_result_evidence_hash(entry)
            if evidence_hash not in seen_failure_evidence_hashes:
                seen_failure_evidence_hashes.add(evidence_hash)
                durable.add("new_failure_evidence")
            else:
                low_value.add("repeated_failure_evidence")
            continue

        if tool in {"write_file", "patch_file"}:
            persisted = entry.get("persisted")
            if persisted is False:
                low_value.add("rejected_artifact_write")
                continue
            code_diag = entry.get("code_diagnostics")
            if isinstance(code_diag, dict) and int(code_diag.get("new_count") or 0) > 0:
                durable.add("new_code_diagnostics")
            path = str(
                entry.get("wrote_path")
                or entry.get("patch_path")
                or entry.get("attempted_path")
                or ""
            )
            if path.endswith("_agent_state/research_synthesis.json"):
                durable.add("accepted_research_synthesis")
            elif path.endswith("_agent_state/implementation_plan.json"):
                durable.add("accepted_implementation_plan_artifact")
            elif path:
                durable.add("code_or_artifact_changed")
            else:
                durable.add("code_or_artifact_changed")
            continue

        if tool == "run_code":
            command = str(entry.get("command") or "").lower()
            result = str(entry.get("result") or "").lower()
            if any(token in command for token in ("test", "smoke", "live", "probe", "pytest")):
                durable.add("test_or_probe_evidence")
            elif any(token in result for token in ("smoke test passed", "harness_complete", "probe")):
                durable.add("test_or_probe_evidence")
            else:
                low_value.add("non_test_run_code")
            continue

        if tool == "ask_research":
            durable.add("new_research_finding")
            continue

        if entry.get("deduped_read"):
            low_value.add("deduped_read_stub")
        elif tool in _CONCURRENCY_SAFE_LOCAL_TOOLS:
            low_value.add("read_only_inspection")

    return bool(durable), sorted(durable), sorted(low_value)


def _normalized_tool_target(block: Any) -> str:
    tool_input = getattr(block, "input", None)
    if not isinstance(tool_input, dict):
        return ""
    raw = tool_input.get("filename") or tool_input.get("path") or tool_input.get("file_path") or ""
    return str(raw).replace("\\", "/").lstrip("./")


def _detect_implementation_plan_transition(
    block: Any,
    *,
    implementation_plan_accepted: bool,
    exit_code: int,
) -> tuple[bool, str]:
    """Return True when a successful plan write accepts the active build gate."""

    if implementation_plan_accepted or exit_code != 0:
        return False, ""
    if getattr(block, "name", "") not in {"write_file", "patch_file"}:
        return False, ""
    target = _normalized_tool_target(block)
    if target == "_agent_state/implementation_plan.json":
        return True, f"{block.name}:{target}"
    return False, ""


def _is_implementation_plan_write_attempt(block: Any) -> bool:
    if getattr(block, "name", "") not in {"write_file", "patch_file"}:
        return False
    return _normalized_tool_target(block) == "_agent_state/implementation_plan.json"


def _successful_python_mutation_path(
    block: Any,
    *,
    result_text: str,
    exit_code: int,
) -> str:
    """Return a sandbox-relative .py path after a successful write/patch."""

    if exit_code != 0:
        return ""
    tool_name = getattr(block, "name", "")
    if tool_name == "write_file":
        if not str(result_text).startswith("Written "):
            return ""
    elif tool_name == "patch_file":
        if not str(result_text).startswith("Patched "):
            return ""
    else:
        return ""
    target = _normalized_tool_target(block)
    return target if target.endswith(".py") else ""


# ---------------------------------------------------------------------------
# BuildContext â€” frozen inputs the legacy entry point provides.
# ---------------------------------------------------------------------------


def _run_planned_research_workers(
    *,
    client: Any,
    sandbox_dir: Path,
    candidate: ScreenedCandidate,
    logger: Any,
    trace_id: str,
    targeted_research: Callable[[Any, str, str, Any, str], tuple[str, float]],
    max_workers: int = 4,
) -> tuple[str, float]:
    """Execute unresolved tasks from research_plan.json once per plan hash."""

    from puzzleeval.agents.agent5.research_plan import (
        research_finding_from_answer,
        run_research_batch_once,
    )

    cost_lock = threading.Lock()
    total_cost = 0.0
    context_packet = _planned_research_context_packet(sandbox_dir)

    def researcher(task: dict[str, Any]) -> dict[str, Any]:
        nonlocal total_cost
        task_id = str(task.get("id") or "task")
        where = task.get("where_to_look") or []
        evidence = task.get("evidence_required") or []
        question = (
            "PLANNED_RESEARCH_TASK\n"
            f"TASK_ID: {task_id}\n"
            f"CANDIDATE: {candidate.name}\n"
            f"PROVIDER: {candidate.provider}\n"
            f"DOCS_ENTRYPOINT: {candidate.verified_api_docs_url or '(from research_plan.json)'}\n"
            f"QUESTION: {task.get('question') or ''}\n"
            f"WHERE_TO_LOOK: {json.dumps(where, ensure_ascii=False)}\n"
            f"EVIDENCE_REQUIRED: {json.dumps(evidence, ensure_ascii=False)}\n"
            f"WHY: {task.get('why_needed_for_build') or ''}\n\n"
            f"{context_packet}"
            "Return the narrow answer with source URLs. If the official docs "
            "do not answer it, say NOT_FOUND and name what was checked."
        )
        answer, cost = targeted_research(
            client,
            question,
            candidate.name,
            logger,
            trace_id,
        )
        with cost_lock:
            total_cost += cost
        return research_finding_from_answer(
            task=task,
            question=question,
            answer=answer,
            source="planned_research_worker",
        )

    execution = run_research_batch_once(
        sandbox_dir,
        researcher,
        max_workers=max_workers,
    )
    logger.info(
        "Planned research worker execution %s for %s",
        execution.status,
        candidate.name,
        extra={
            "operation": "planned_research_workers_executed",
            "trace_id": trace_id,
            "candidate_name": candidate.name,
            "status": execution.status,
            "pending_task_ids": execution.pending_task_ids,
            "completed_task_ids": execution.completed_task_ids,
            "finding_count": len(execution.findings),
            "marker_path": execution.marker_path,
        },
    )
    return execution.to_result_text(), total_cost


def _read_limited_text(path: Path, limit: int) -> str:
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:limit].strip()


def _planned_research_context_packet(sandbox_dir: Path) -> str:
    """Return bounded durable context for planned research workers."""

    sections: list[str] = []
    for rel, limit in (
        ("_agent_state/objective.md", 1200),
        ("_agent_state/docs_entrypoint.json", 1500),
        ("_agent_state/research_handoff.json", 1800),
        ("_agent_state/research_plan.json", 2200),
        ("_agent_state/research_synthesis.json", 2500),
        ("_agent_state/implementation_plan.json", 1800),
        ("_agent_state/latest_failure_packet.json", 1500),
    ):
        text = _read_limited_text(sandbox_dir / rel, limit)
        if text:
            sections.append(f"## {rel}\n{text}")

    findings_dir = sandbox_dir / "_agent_state" / "research_findings"
    try:
        finding_paths = sorted(findings_dir.glob("*.json"))[:5] if findings_dir.exists() else []
    except OSError:
        finding_paths = []
    finding_sections: list[str] = []
    for path in finding_paths:
        text = _read_limited_text(path, 900)
        if text:
            finding_sections.append(f"### {path.name}\n{text}")
    if finding_sections:
        sections.append("## _agent_state/research_findings\n" + "\n".join(finding_sections))

    if not sections:
        return ""
    return (
        "WHOLE_PICTURE_CONTEXT (bounded durable Agent 5 artifacts):\n"
        + "\n\n".join(sections)
        + "\n\nDo not re-research confirmed facts above. Answer only the "
        "planned task's missing implementation-changing facts.\n\n"
    )


@dataclass(frozen=True)
class BuildContext:
    """Frozen inputs for ONE candidate's build.

    Constructed once at the entry point (legacy
    ``_build_single_harness(client, candidate, input_data, sandbox_dir,
    logger, progress_callback=None)``); never mutated.

    Derived fields (``trace_id``, ``candidate_label``) are populated in
    ``__post_init__`` so callers don't recompute them from the same
    inputs across helpers.

    Note: post-setup outputs (``credentials``, ``staged_test_cases``,
    ``provider_slug``) live on
    ``BuildSetupSuccess`` (already extracted in Phase 4.2). They are
    NOT on BuildContext because they aren't available at construction
    time â€” the caller must run setup first to obtain them. Helpers
    requiring both shapes take ``ctx: BuildContext`` AND
    ``setup: BuildSetupSuccess`` separately.
    """

    # The 6 legacy positional/keyword inputs
    client: Any  # anthropic.Anthropic
    candidate: Any  # ScreenedCandidate
    input_data: Any  # Agent5Input
    sandbox_dir: Path
    logger: Any  # logging.Logger
    progress_callback: Callable[[str, dict], None] | None = None

    # Derived in __post_init__ â€” read-only after construction
    trace_id: str = field(init=False)
    candidate_label: str = field(init=False)

    def __post_init__(self) -> None:
        # Frozen dataclass â€” must use object.__setattr__ to populate
        # derived fields. This is the documented escape hatch for
        # __post_init__ on frozen dataclasses.
        object.__setattr__(self, "trace_id", self.input_data.trace_id)
        object.__setattr__(
            self, "candidate_label", candidate_slug(self.candidate.name)
        )


# ---------------------------------------------------------------------------
# BuildLoopState â€” mutable per-build state. Replaces ~22 loop locals.
# ---------------------------------------------------------------------------


@dataclass
class BuildLoopState:
    """Mutable per-build state â€” the ~22 locals the loop body reads/writes.

    Each field's default matches the inline ``var = default`` line it
    replaces in ``_build_single_harness``. The loop body uses
    ``state.X = ...`` instead of bare ``X = ...``. Behavior is identical;
    only the access syntax changes.

    NOT included (intentional):
      * Per-iteration short-lived locals (``call_start``,
        ``call_latency_ms``, ``last_errors``, ``pattern_hint``,
        ``current_max_tokens``, etc.). They live + die within one loop
        body iteration; promoting them to BuildLoopState would pollute
        the dataclass.
      * Setup-derived constants (``credentials``, ``staged_test_cases``,
        ``trace_id``, ``candidate_label``, etc.). Those live on
        BuildContext / BuildSetupSuccess.
    """

    # â”€â”€ Turn counters + accumulators â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    turn: int = 0
    accumulated_cost: float = 0.0
    total_web_searches: int = 0
    candidate_web_fetch_blocks: int = 0  # Cumulative recoverable web_fetch blocks across turns

    # â”€â”€ The conversation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    messages: list = field(default_factory=list)
    conversation_log: list = field(default_factory=list)  # Human-readable per-turn log

    # â”€â”€ Research â†’ build transition flag â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    build_gate_accepted: bool = False
    implementation_plan_accepted: bool = False

    # â”€â”€ Per-turn ephemeral (cleared at top of each iteration) â”€â”€â”€â”€â”€â”€
    last_text: str = ""

    # â”€â”€ Verification gate state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    verification_attempts: int = 0
    verification_passed: bool = False

    # â”€â”€ Smoke test pass tracking (across ALL turns) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    smoke_ever_passed: bool = False
    smoke_passed_at_turn: int = -1  # -1 = never passed yet

    # â”€â”€ Dead-end detection / reassessment â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    consecutive_errors: int = 0  # Resets on successful turn OR after reassessment
    total_reassessments: int = 0  # Cumulative â€” never resets (drives escalation tiers)
    error_history: list[tuple[int, str]] = field(default_factory=list)  # (turn, category)

    # â”€â”€ Progress / nudge tracking â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    progress_ring: list[bool] = field(default_factory=list)  # Last N turns' "made progress" signal
    diminishing_nudge_sent: bool = False
    turn_budget_nudge_sent: bool = False
    approaches_tried: list[str] = field(default_factory=list)
    patch_fragmentation_nudged_files: set[str] = field(default_factory=set)

    # â”€â”€ Tool state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    build_read_state: dict[str, float] = field(default_factory=dict)  # filename â†’ last_read_mtime
    saved_doc_files: list[str] = field(default_factory=list)  # fetched_docs_N.txt index

    # â”€â”€ Wall-clock timeout tracking â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    build_start_time: float = 0.0  # time.monotonic() at build start; 0.0 means uninitialized


# ---------------------------------------------------------------------------
# Phase 5 Step 2 helpers â€” _initialize_loop_state + _should_inject_turn_budget_nudge
# ---------------------------------------------------------------------------


def _initialize_loop_state(ctx: BuildContext, setup: Any) -> BuildLoopState:
    """Build the initial BuildLoopState from ctx + setup output.

    Replaces the inline state-init block in
    ``_build_single_harness`` that ran AFTER ``_setup_sandbox_and_credentials``
    returned. Each assignment here matches a corresponding inline
    ``var = default`` line â€” behavior is identical.

    Args:
        ctx: Frozen BuildContext (the 6 entry-point inputs + derived
            trace_id / candidate_label).
        setup: BuildSetupSuccess-shaped object with ``credentials``,
            ``staged_test_cases``, etc. (Imported
            lazily to avoid an import cycle â€” implement_test_env owns
            BuildSetupSuccess today; this helper just reads its
            attributes.)

    Returns:
        BuildLoopState seeded with:
          * The first user message (the build's initial prompt) in
            ``messages``.
          * ``saved_doc_files`` populated from prefetched docs already
            on disk (Agent 4 â†’ Agent 5 handoff).
          * ``build_start_time`` set to ``time.monotonic()``.
          * Every other field at its dataclass default.

    Pure-ish: imports + filesystem read (counting prefetched docs) +
    one logger call. No mutation of ctx or setup.
    """
    import time

    from puzzleeval.web_doc_cache import count_existing_fetched_docs

    # Lazy import â€” `_build_initial_message` lives in implement_test_env
    # today (Phase 4 has not yet extracted it). Importing at call time
    # avoids a build_loop â†’ implement_test_env import cycle.
    from puzzleeval.agents.implement_test_env import _build_initial_message

    state = BuildLoopState()

    # Seed messages from the initial-message builder.
    initial_message = _build_initial_message(
        ctx.candidate,
        ctx.input_data,
        credentials=setup.credentials,
        staged_test_cases=setup.staged_test_cases,
        sandbox_dir=ctx.sandbox_dir,
    )
    state.messages.append({"role": "user", "content": initial_message})

    # Seed prefetched-docs index from sandbox dir (Agent 4 â†’ 5 handoff).
    prefetched_count = count_existing_fetched_docs(ctx.sandbox_dir)
    if prefetched_count:
        state.saved_doc_files = [
            f"fetched_docs_{i}.txt" for i in range(prefetched_count)
        ]
        ctx.logger.info(
            f"Seeded Agent 5 with {prefetched_count} prefetched docs from Agent 4",
            extra={
                "operation": "agent5_doc_handoff_seed",
                "trace_id": ctx.trace_id,
                "candidate_name": ctx.candidate.name,
                "prefetched_count": prefetched_count,
                "prefetched_files": state.saved_doc_files,
            },
        )

    # Wall-clock start.
    state.build_start_time = time.monotonic()

    return state


def _should_inject_turn_budget_nudge(
    state: BuildLoopState, max_turns: int,
) -> bool:
    """Predicate for the wrap-up nudge â€” fires when â‰¤3 turns remain
    AND smoke hasn't passed AND the conversation has started AND the
    nudge hasn't fired yet.

    Pattern stolen from Claude Code's ``getBudgetContinuationMessage``
    (query/tokenBudget.ts). Without this the builder can get stuck
    polishing on turn 23 and run out without producing a verdict.

    Pure function. Caller is responsible for actually appending the
    nudge message AND setting ``state.turn_budget_nudge_sent = True``
    after injection.
    """
    turns_remaining = max_turns - state.turn
    return (
        turns_remaining <= 3
        and not state.turn_budget_nudge_sent
        and not state.smoke_ever_passed
        and bool(state.messages)
    )


# ---------------------------------------------------------------------------
# Phase 5 Step 3 â€” build_single_harness (the orchestration spine).
# ---------------------------------------------------------------------------


def build_single_harness(
    client: anthropic.Anthropic,
    candidate: ScreenedCandidate,
    input_data: Agent5Input,
    sandbox_dir: Path,
    logger,
    progress_callback: "Callable[[str, dict], None] | None" = None,
) -> TestHarness | FailedHarness:
    """
    Build a test harness for one candidate using an autonomous tool-use loop
    with a verification gate.

    The loop has two modes:
    1. BUILD MODE: Claude reads docs, writes code, runs smoke tests, fixes errors
    2. VERIFICATION GATE: When Claude signals HARNESS_COMPLETE, we run checks.
       If checks fail, we feed the issues back to Claude for fixing.
       The loop only exits when verified clean or retries are exhausted.

    Returns TestHarness on success, FailedHarness on failure.
    Never raises â€” all errors are caught and converted to FailedHarness.
    """

    # Phase 5 Step 3 â€” lazy imports of helpers that still live in
    # ``puzzleeval.agents.implement_test_env``. Lazy because:
    # implement_test_env imports build_loop at module bottom for the
    # legacy shim; importing back at MODULE TOP here would cycle. By
    # the time build_single_harness is CALLED, implement_test_env has
    # finished loading, so these imports resolve cleanly.
    from puzzleeval.config import (
        AGENT5_BUILDER_MODEL,
        AGENT5_CODE_TIMEOUT,
        AGENT5_MAX_BUDGET_PER_CANDIDATE,
        AGENT5_MAX_BUDGET_PER_CANDIDATE_VOICE,
        AGENT5_MAX_OUTPUT_TOKENS,
        AGENT5_MAX_TURNS,
        AGENT5_MAX_TURNS_VOICE,
        AGENT5_MAX_VERIFICATION_RETRIES,
        AGENT5_PROGRESS_HEARTBEAT_SECONDS,
        ENABLE_FETCH_FALLBACK,
        GATE_FORENSICS_COVERAGE_ENABLED,
        REPRESENTATIVE_PROBE_GATE_ENABLED,
        RESEARCH_MODEL,
    )
    from puzzleeval.agents.agent5.autonomy_directives import (
        BUILD_PLAN_INIT_DIRECTIVE,
        BUILD_PLAN_STALENESS_NUDGE,
        EVENT_BUILD_PLAN_INIT_DIRECTIVE_FIRED,
        EVENT_BUILD_PLAN_STALE_AT_TRIGGER,
        EVENT_REFLECTION_PHASE_3_DIRECTIVE_FIRED,
        REFLECTION_PHASE_3_DIRECTIVE,
    )
    from puzzleeval.agents.agent5.context_compaction import (
        COMPACTION_EVENT_NAME,
        compact_for_build_gate,
        restore_after_server_compaction,
    )
    from puzzleeval.agents.agent5.tools import (
        dispatch_tool_result,
        invalidate_read_dedup_state,
    )
    from puzzleeval.agents.agent5.conversation_log import save_conversation_log
    from puzzleeval.agents.agent5.playbooks import VOICE_MODALITIES
    from puzzleeval.agents.agent5.runtime_state import update_runtime_state
    from puzzleeval.agents.agent5.verification import (
        verify_forensics_coverage,
        verify_reflection_complete,
        verify_voice_live_test_contract,
    )
    from puzzleeval.agents.agent5.representative_probe import (
        run_representative_probe_gate,
    )
    from puzzleeval.agents.implement_test_env import (
        ALL_TOOLS,
        BUILDER_SYSTEM_PROMPT,
        CUSTOM_TOOL_NAMES,
        _apply_message_cache_breakpoint,
        _ask_research_template_adherence,
        _build_initial_message,
        _build_tools_with_programmatic,
        _calculate_call_cost,
        _categorize_failure,
        _compact_research_tool_results,
        _detect_patch_fragmentation_pattern,
        _extract_and_save_web_content,
        _extract_text_from_response,
        _finalize_build_result,
        _persist_large_output,
        _read_harness_code,
        _render_builder_prompt,
        _run_targeted_research,
        _run_verification_checks,
        _setup_sandbox_and_credentials,
        _tool_patch_file,
        _with_builder_appendix,
        _with_shared_preamble,
    )

    # Phase 4.2: setup logic extracted to _setup_sandbox_and_credentials.
    # Returns BuildSetupSuccess on success, or TestHarness/FailedHarness
    # for early-return paths (OpenAPI fastpath success, venv failure).
    _setup = _setup_sandbox_and_credentials(
        candidate, input_data, sandbox_dir, logger, progress_callback,
    )
    if isinstance(_setup, (TestHarness, FailedHarness)):
        return _setup
    candidate_label = _setup.candidate_label
    trace_id = _setup.trace_id
    provider_slug = _setup.provider_slug
    staged_test_cases = _setup.staged_test_cases
    credentials = _setup.credentials

    # Stage compact research handoff before the initial message is rendered
    # so the builder sees it on turn 0 even when Agent 4 did not already
    # write the file (legacy/cache/direct Agent 5 paths).
    try:
        from puzzleeval.research_handoff import write_research_handoff

        write_research_handoff(candidate, sandbox_dir)
    except Exception as exc:  # noqa: BLE001 - handoff is advisory
        logger.warning(
            "Agent 5 pre-message research handoff staging failed",
            extra={
                "operation": "agent5_research_handoff_premessage_failed",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "error_type": type(exc).__name__,
                "error_msg": str(exc)[:300],
            },
        )

    # â˜… CORE: Initialize the conversation with seed knowledge + credential hints.
    # Agent 5 owns both research and build, but research is durable now:
    # turn-0 context points at docs_entrypoint/research_handoff, planned
    # research workers persist findings, and the builder consolidates those
    # facts into research_synthesis.json before implementation_plan.json.
    initial_message = _build_initial_message(
        candidate, input_data, credentials=credentials,
        staged_test_cases=staged_test_cases, sandbox_dir=sandbox_dir,
    )
    messages = [{"role": "user", "content": initial_message}]
    # Exposed to the per-turn builder loop so `_render_builder_prompt` can
    # inject the right modality-specific contract (voice harness shape,
    # etc.) based on THIS candidate's test-case modalities.
    test_cases_for_builder = staged_test_cases

    accumulated_cost = 0.0
    total_web_searches = 0
    turn = 0
    # â”€â”€ Research â†’ build transition tracker â”€â”€
    # The build gate accepts only a valid _agent_state/implementation_plan.json.
    build_gate_accepted = False
    implementation_plan_accepted = False
    implementation_plan_validation_failures = 0
    last_text = ""
    verification_attempts = 0
    verification_passed = False
    completion_gate_status = "not_signaled"
    completion_gate_issues: list[str] = []
    completion_gate_issue_retries: dict[str, int] = {}
    conversation_log = []  # Human-readable log of every turn
    # Seed saved_doc_files from the sandbox dir â€” Agent 4's verification
    # turn persists `fetched_docs_*.txt` here before we ever start, so the
    # builder's STEP 1 can `read_file` instead of `web_fetch` the same URL
    # a second time. If Agent 4 found nothing (or the feature flag was
    # off), this is the empty list and Agent 5 behaves exactly as before.
    # See puzzleeval/web_doc_cache.py for the handoff protocol.
    from puzzleeval.web_doc_cache import count_existing_fetched_docs
    _prefetched_count = count_existing_fetched_docs(sandbox_dir)
    saved_doc_files = [
        f"fetched_docs_{i}.txt" for i in range(_prefetched_count)
    ]
    if saved_doc_files:
        logger.info(
            f"Seeded Agent 5 with {_prefetched_count} prefetched docs from Agent 4",
            extra={
                "operation": "agent5_doc_handoff_seed",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "prefetched_count": _prefetched_count,
                "prefetched_files": saved_doc_files,
            },
        )
    consecutive_errors = 0  # Track consecutive tool results with errors
    total_reassessments = 0  # Cumulative â€” never reset (drives escalation tiers)
    error_history = []  # List of (turn, category) for pattern detection
    # MAX_CONSECUTIVE_ERRORS: 3 (was 2) â€” one extra try before strategic
    # pivot. Some first-fix attempts legitimately fail (stale docs, wrong
    # version), and 2 triggered pivots on legitimately-fixable bugs.
    MAX_CONSECUTIVE_ERRORS = 3
    build_start_time = time.monotonic()  # Wall-clock timeout tracking
    # MAX_BUILD_TIME_SECONDS: 900 (was 480) â€” complex voice / WebSocket /
    # multi-endpoint builds legitimately take 12-15 min. Under 8 min the
    # loop was killing mid-Phase-2 debug cycles on ElevenLabs + similar.
    MAX_BUILD_TIME_SECONDS = 900
    candidate_web_fetch_blocks = 0  # Cumulative recoverable web_fetch blocks across turns (Phase 1 hardening)
    # Gate B4 â€” Pre-plan research budget. Counts TURNS (not calls) where
    # the builder used web_search / web_fetch / ask_research while
    # the build gate was not yet satisfied. After exceeding budget, inject
    # a one-shot user message before the next API call telling the builder
    # to commit research_synthesis.json and implementation_plan.json. Soft
    # enforcement; the builder decides whether to comply or call advisor for
    # a tier-up.
    prebuild_research_turns = 0
    prebuild_budget_message_injected = False
    empty_fetch_counts: dict[str, int] = {}
    smoke_ever_passed = False  # Track optional offline smoke checks across ALL turns
    smoke_passed_at_turn = -1  # Which turn the optional smoke check first passed
    live_ever_passed = False  # Track live_test.py success independent of gate acceptance
    live_passed_at_turn = -1
    last_live_test_output = ""
    # Gate C â€” patch-fragmentation nudge history. Each filename is
    # added AFTER the nudge fires for that file, so a single file can
    # be nudged AT MOST once per build. Re-fires on DIFFERENT files
    # (e.g., nibbling on harness.py then nibbling on live_test.py
    # triggers two nudges, which is correct â€” two distinct lessons).
    patch_fragmentation_nudged_files: set[str] = set()
    scaffold_batch_nudge_sent = False
    # --- Adaptive progress tracking (Claude Code diminishing-returns pattern) ---
    # `progress_ring`: last N turns' durable-progress signal. A turn counts
    # only if it produced accepted artifacts, code changes, new research,
    # fresh failure/test/probe evidence, or completion evidence. Mere tool
    # activity (rereads, rejected writes, repeated failures) does not reset it.
    from puzzleeval.config import (
        AGENT5_DIMINISHING_RETURNS_WINDOW,
        AGENT5_MAX_REASSESSMENT_TIERS,
        AGENT5_PATCH_FRAGMENT_NUDGE_ENABLED,
        AGENT5_PATCH_FRAGMENT_TOKEN_CEILING,
    )
    progress_ring: list[bool] = []
    seen_failure_evidence_hashes: set[str] = set()
    diminishing_nudge_sent = False
    # `approaches_tried`: per-candidate list of pivots taken. Cited in
    # reassessment prompt so the builder knows what NOT to try again.
    approaches_tried: list[str] = []
    # Read-before-patch gate state (Claude Code parity). Per-build dict
    # mapping ``filename â†’ read metadata``. Populated by read_file +
    # write_file; checked by patch_file. Exact read keys support unchanged-read
    # stubs and are invalidated after compaction. Prevents the
    # iterative-micro-patch waste pattern observed in trace 28cb2648
    # (5 consecutive patches to harness.py = $1.67 burned on blind fixes).
    # See `_tool_patch_file` docstring for the full gate design.
    build_read_state: dict[str, Any] = {}

    # â˜… CORE: Multi-turn autonomous loop with verification gate
    # Guardrails: turn limit (AGENT5_MAX_TURNS) + wall-clock timeout.
    # No per-candidate budget cap â€” let the agent use as many tokens as it
    # needs per turn. The turn limit and timeout prevent runaway costs.
    # Turn-budget nudge state â€” when the builder has â‰¤3 turns remaining
    # AND hasn't signaled HARNESS_COMPLETE/FAILED yet, inject a wrap-up
    # reminder into the next user message. Stolen from Claude Code's
    # getBudgetContinuationMessage pattern (query/tokenBudget.ts). Without
    # this, Agent 5 can get stuck polishing on turn 23 and run out without
    # ever producing a final verdict. Fired once per candidate.
    turn_budget_nudge_sent = False

    # Voice harnesses get a higher build budget. Voice is intrinsically
    # harder than REST (multi-turn WebSocket state, async events, real-time
    # TTS/STT) â€” real-run trace 6e0c9563 had ElevenLabs abandon a deeper
    # fix at turn 13/40 because the default 40-turn cap ran out before debug
    # iterations completed. Per-modality logic in build_loop is the
    # documented exception to AD-001/AD-003 because budget is meta-control
    # over the agent itself, not modality-specific behavior.
    is_voice_build = any(
        (getattr(tc, "input_type", None) in VOICE_MODALITIES)
        or (getattr(tc, "output_type", None) in VOICE_MODALITIES)
        for tc in staged_test_cases
    )
    effective_max_turns = AGENT5_MAX_TURNS_VOICE if is_voice_build else AGENT5_MAX_TURNS
    effective_max_budget_usd = (
        AGENT5_MAX_BUDGET_PER_CANDIDATE_VOICE if is_voice_build
        else AGENT5_MAX_BUDGET_PER_CANDIDATE
    )
    if is_voice_build and effective_max_turns != AGENT5_MAX_TURNS:
        logger.info(
            "Voice-modality build: using effective_max_turns=%d (default %d) "
            "for %s",
            effective_max_turns, AGENT5_MAX_TURNS, candidate.name,
            extra={
                "operation": "voice_build_budget_applied",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "effective_max_turns": effective_max_turns,
                "default_max_turns": AGENT5_MAX_TURNS,
            },
        )

    # â”€â”€ PR 1: stage autonomy artifacts (objective.md + runtime_state.json) â”€â”€
    # See plan: GoalÂ·PlanningÂ·StateÂ·Reflection. Orchestrator-owned files
    # land in ``_agent_state/`` BEFORE turn 0 so the agent's first read
    # has authoritative truth. Failure here is logged but doesn't abort
    # the build â€” the legacy reactive path still works without these files.
    from puzzleeval import config as _cfg

    def _current_loop_model() -> str:
        return _select_agent5_model(
            research_model=RESEARCH_MODEL,
            builder_model=AGENT5_BUILDER_MODEL,
        )

    def _failure_packet_reviewer(packet: dict) -> dict | None:
        if not getattr(_cfg, "FAILURE_PACKET_LLM_REVIEW_ENABLED", False):
            return None
        try:
            from puzzleeval.agents.agent5.failure_packets import review_failure_packet_with_llm

            return review_failure_packet_with_llm(
                client,
                packet,
                model=AGENT5_BUILDER_MODEL,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics cannot break builds
            logger.warning(
                "failure packet LLM review failed",
                extra={
                    "operation": "failure_packet_llm_review_failed",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                },
            )
            return None

    def _flush_observability_snapshot(
        *,
        current_model: str | None = None,
        final_completion_status: str | None = None,
        final_completion_issues: list[str] | None = None,
    ) -> None:
        """Best-effort runtime/log flush for normal and fatal exits."""

        if _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED:
            try:
                class _FlushRuntimeState:
                    pass

                _frs = _FlushRuntimeState()
                _frs.turn = turn
                _frs.accumulated_cost = accumulated_cost
                _frs.build_gate_accepted = build_gate_accepted
                _frs.implementation_plan_accepted = implementation_plan_accepted
                _frs.verification_attempts = verification_attempts
                _frs.verification_passed = verification_passed
                _frs.smoke_ever_passed = smoke_ever_passed
                _frs.smoke_passed_at_turn = smoke_passed_at_turn
                _frs.consecutive_errors = consecutive_errors
                _frs.total_reassessments = total_reassessments
                update_runtime_state(
                    sandbox_dir=sandbox_dir,
                    state=_frs,
                    current_model=current_model or _current_loop_model(),
                    smoke_test_status=("passing" if smoke_ever_passed else "not_run"),
                    live_test_status=(
                        "passing" if live_ever_passed
                        else "failing" if last_live_test_output else "not_run"
                    ),
                    live_passed_at_turn=live_passed_at_turn if live_ever_passed else None,
                    last_live_test_output=last_live_test_output or None,
                    completion_gate_status=final_completion_status or completion_gate_status,
                    completion_gate_issues=final_completion_issues or completion_gate_issues,
                )
            except OSError:
                pass
        try:
            save_conversation_log(sandbox_dir, conversation_log, candidate.name)
        except OSError:
            pass

    if _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED:
        from puzzleeval.agents.agent5.playbooks import selected_playbook_ids
        from puzzleeval.agents.agent5.sandbox import stage_agent_state
        import sys as _sys
        _platform_label = (
            "windows" if _sys.platform == "win32"
            else "macos" if _sys.platform == "darwin"
            else "linux"
        )
        try:
            _modality_playbook_ids = selected_playbook_ids(staged_test_cases)
        except Exception:  # noqa: BLE001 â€” playbook composition is advisory here
            _modality_playbook_ids = []
        _staged_ok = stage_agent_state(
            sandbox_dir=sandbox_dir,
            candidate=candidate,
            input_data=input_data,
            modality_playbook_ids=_modality_playbook_ids,
            effective_max_turns=effective_max_turns,
            effective_max_budget_usd=effective_max_budget_usd,
            platform=_platform_label,
            initial_model=AGENT5_BUILDER_MODEL,
        )
        logger.info(
            "Autonomy artifacts staged for %s: %s",
            candidate.name, "ok" if _staged_ok else "failed",
            extra={
                "operation": "autonomy_artifacts_staged",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "ok": _staged_ok,
                "modality_playbook_ids": _modality_playbook_ids,
                "platform": _platform_label,
            },
        )
        try:
            from puzzleeval.agents.agent5 import runtime_state as _runtime_state
            snapshot = _runtime_state.write_runtime_snapshot(sandbox_dir)
            if ".md" not in snapshot.get("allowed_extensions", []):
                completion_gate_status = "failed_runtime_config"
                completion_gate_issues.append(
                    "Active Agent 5 runtime does not allow .md writes; "
                    "_agent_state/reflection_phase_3.md cannot be created."
                )
                _append_build_progress_event(sandbox_dir, {
                    "event": "runtime_config_error",
                    "candidate_name": candidate.name,
                    "issue": completion_gate_issues[-1],
                    "allowed_extensions": snapshot.get("allowed_extensions", []),
                })
        except OSError:
            pass

    # â”€â”€ PR 1: track autonomy-directive firing state across the loop â”€â”€
    # These flags ensure each one-shot directive fires AT MOST once per
    # build. Build-plan trigger telemetry uses last_build_plan_mtime to
    # detect staleness at trigger points (build gate, smoke pass, etc.).
    research_handoff: dict[str, Any] = {}
    try:
        from puzzleeval.agents.agent5.build_decisions import append_build_decision
        from puzzleeval.research_handoff import read_research_handoff

        research_handoff = read_research_handoff(sandbox_dir) or {}
        append_build_decision(
            sandbox_dir,
            event="research_handoff_available",
            turn=-1,
            candidate_name=candidate.name,
            canonical_docs_url_count=len(research_handoff.get("canonical_docs_urls") or []),
            discovered_docs_url_count=len(research_handoff.get("discovered_docs_urls") or []),
            prefetched_doc_count=len(research_handoff.get("prefetched_doc_files") or []),
            unresolved_question_count=len(research_handoff.get("unresolved_questions") or []),
        )
        _append_build_progress_event(sandbox_dir, {
            "event": "research_handoff_available",
            "candidate_name": candidate.name,
            "canonical_docs_url_count": len(research_handoff.get("canonical_docs_urls") or []),
            "discovered_docs_url_count": len(research_handoff.get("discovered_docs_urls") or []),
            "prefetched_doc_count": len(research_handoff.get("prefetched_doc_files") or []),
            "unresolved_question_count": len(research_handoff.get("unresolved_questions") or []),
        })
    except Exception as exc:  # noqa: BLE001 - handoff is advisory
        logger.warning(
            "Agent 5 research handoff staging failed",
            extra={
                "operation": "agent5_research_handoff_stage_failed",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "error_type": type(exc).__name__,
                "error_msg": str(exc)[:300],
            },
        )

    build_plan_init_directive_sent = False
    reflection_phase_3_directive_sent = False
    last_build_plan_mtime = 0.0  # 0.0 = never written

    def _current_completion_gate_issues(*, attempt: int) -> str | None:
        """Return the current completion-gate issue, or None when passable.

        This is deliberately current-state-only: it re-reads the sandbox on
        every call and does not consult historical retry status. Retry budgets
        only decide whether to spend another repair turn; they must never make
        an earlier, already-fixed issue poison final acceptance.
        """

        if completion_gate_status == "failed_runtime_config" and completion_gate_issues:
            return completion_gate_issues[-1]

        if getattr(_cfg, "CODE_DIAGNOSTICS_ENABLED", True):
            try:
                from puzzleeval.agents.agent5.code_diagnostics import (
                    SCAFFOLD_PYTHON_FILES,
                    completion_blocking_diagnostics,
                    run_python_diagnostics,
                )

                run_python_diagnostics(
                    sandbox_dir,
                    SCAFFOLD_PYTHON_FILES,
                    turn=turn,
                    source_tool="completion_gate",
                )
                syntax_diagnostics = completion_blocking_diagnostics(sandbox_dir)
                if syntax_diagnostics:
                    formatted = "; ".join(
                        f"{item.get('path')}:{item.get('line')}:{item.get('column')} "
                        f"{item.get('message')}"
                        for item in syntax_diagnostics[:6]
                    )
                    return (
                        "Python syntax diagnostics are still active for required "
                        f"scaffold files: {formatted}. Fix these mechanical syntax "
                        "errors before signaling HARNESS_COMPLETE."
                    )
            except Exception as exc:  # noqa: BLE001 - gate remains fail-open
                logger.warning(
                    "Code diagnostics completion gate failed open",
                    extra={
                        "operation": "code_diagnostics_completion_gate_failed_open",
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "turn": turn,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300],
                    },
                )

        issues = _run_verification_checks(
            sandbox_dir, candidate, credentials, logger, trace_id,
        )
        if not issues and GATE_FORENSICS_COVERAGE_ENABLED:
            forensics_issue = verify_forensics_coverage(sandbox_dir)
            if forensics_issue is not None:
                logger.warning(
                    "gate_fired",
                    extra={
                        "operation": "gate_fired",
                        "gate_name": "forensics_coverage",
                        "severity": "WARN",
                        "rejected": True,
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "attempt": attempt,
                    },
                )
                issues = forensics_issue
        if not issues and is_voice_build:
            live_contract_issue = verify_voice_live_test_contract(
                sandbox_dir,
                client=client,
                judge_model=AGENT5_BUILDER_MODEL,
                llm_review_enabled=getattr(
                    _cfg,
                    "VOICE_LIVE_SEMANTIC_REVIEW_ENABLED",
                    False,
                ),
            )
            if live_contract_issue is not None:
                logger.warning(
                    "gate_fired",
                    extra={
                        "operation": "gate_fired",
                        "gate_name": "voice_live_test_contract",
                        "severity": "WARN",
                        "rejected": True,
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "attempt": attempt,
                    },
                )
                issues = live_contract_issue
        if not issues and REPRESENTATIVE_PROBE_GATE_ENABLED and implementation_plan_accepted:
            representative_issue = run_representative_probe_gate(
                client=client,
                sandbox_dir=sandbox_dir,
                candidate=candidate,
                input_data=input_data,
                staged_test_cases=staged_test_cases,
                credentials=credentials,
                logger=logger,
                trace_id=trace_id,
                progress_callback=progress_callback,
            )
            if representative_issue is not None:
                logger.warning(
                    "gate_fired",
                    extra={
                        "operation": "gate_fired",
                        "gate_name": "representative_probe",
                        "severity": "WARN",
                        "rejected": True,
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "attempt": attempt,
                    },
                )
                issues = representative_issue
        representative_status = _representative_probe_status(sandbox_dir)
        if (
            not issues
            and _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED
            and _cfg.GATE_REFLECTION_PHASE_3_ENABLED
            and representative_status not in {"passed", "external_block"}
        ):
            reflection_issue = verify_reflection_complete(
                sandbox_dir,
                candidate,
                client=client,
                judge_model=_cfg.REFLECTION_LLM_JUDGE_MODEL,
                llm_judge_enabled=_cfg.REFLECTION_LLM_JUDGE_ENABLED,
                logger=logger,
                trace_id=trace_id,
            )
            if reflection_issue is not None:
                logger.info(
                    "Reflection gate current-state issue for %s (attempt %d)",
                    candidate.name, attempt,
                    extra={
                        "operation": "reflection_gate_retry",
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "attempt": attempt,
                    },
                )
                issues = reflection_issue
        elif (
            not issues
            and _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED
            and _cfg.GATE_REFLECTION_PHASE_3_ENABLED
            and representative_status in {"passed", "external_block"}
        ):
            logger.info(
                "Reflection gate skipped because representative probe is the production-equivalence proof",
                extra={
                    "operation": "reflection_gate_skipped_representative_probe",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "attempt": attempt,
                    "representative_probe_status": representative_status,
                },
            )
        return str(issues) if issues else None

    def _tools_for_current_turn() -> list[dict[str, Any]]:
        """Return server/custom tools with research exploration capped by handoff.

        If Agent 4 already gave Agent 5 canonical URLs, prefetched docs, or a
        pre-rendered spec, Phase-1 research should be gap-directed rather than
        broad server-tool exploration. The server tools do not let us veto a
        specific URL before execution, so the production control is to shrink
        the maximum server-tool fanout for that turn.
        """

        tools = copy.deepcopy(ALL_TOOLS)
        has_routing_source = bool(
            (research_handoff.get("prefetched_doc_files") or [])
            or (research_handoff.get("canonical_docs_urls") or [])
            or saved_doc_files
        )
        unresolved_count = len(research_handoff.get("unresolved_questions") or [])
        if build_gate_accepted or not has_routing_source:
            return _build_tools_with_programmatic(tools)
        for tool in tools:
            if tool.get("name") == "web_fetch":
                tool["max_uses"] = 1 if unresolved_count <= 1 else 2
                tool["max_content_tokens"] = min(int(tool.get("max_content_tokens", 10000)), 6000)
            elif tool.get("name") == "web_search":
                tool["max_uses"] = 1
        return _build_tools_with_programmatic(tools)

    while turn < effective_max_turns:
        # Turn-budget nudge â€” fire once when <=3 turns remain.
        turns_remaining = effective_max_turns - turn
        if (
            turns_remaining <= 3
            and not turn_budget_nudge_sent
            and not smoke_ever_passed
            and messages  # only when there IS a conversation to nudge
        ):
            messages.append({
                "role": "user",
                "content": (
                    f"TURN BUDGET ADVISORY: {turns_remaining} turns remaining "
                    f"(of {effective_max_turns}). You must now EITHER: (a) "
                    "finish the current attempt + signal HARNESS_COMPLETE "
                    "if the smoke test + live test will pass, OR (b) signal "
                    "HARNESS_FAILED with a specific failure_reason. Do NOT "
                    "start a new research pass or a third refactor. Pick one "
                    "and commit."
                ),
            })
            turn_budget_nudge_sent = True
            logger.info(
                "Turn-budget nudge injected for %s at turn %d",
                candidate.name, turn,
                extra={"operation": "turn_budget_nudge", "trace_id": trace_id,
                       "candidate_name": candidate.name,
                       "turns_remaining": turns_remaining},
            )

        # Wall-clock timeout check
        elapsed = time.monotonic() - build_start_time
        if elapsed > MAX_BUILD_TIME_SECONDS:
            logger.warning(f"Wall-clock timeout for {candidate.name} after {elapsed:.0f}s", extra={
                "operation": "harness_wallclock_timeout",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "elapsed_seconds": elapsed,
                "turns_used": turn,
            })
            completion_gate_status = "failed_wallclock_timeout"
            completion_gate_issues.append(
                f"Build exceeded {MAX_BUILD_TIME_SECONDS}s wall-clock budget before the completion gate passed."
            )
            break

        # â”€â”€ PR 1: refresh runtime_state.json for the agent to read this turn â”€â”€
        # Orchestrator-owned authoritative state. Updated at the TOP of each
        # iteration so the agent's first read this turn sees the prior turn's
        # effects (files written, smoke status, errors). The state file is
        # write-protected against the agent at the tool-dispatch boundary.
        if _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED:
            # Build a minimal in-flight ``BuildLoopState``-like object out of
            # loop locals. The standalone build_loop hasn't migrated to the
            # ``BuildLoopState`` dataclass for live state yet (migration is
            # tracked separately); this duck-typed shim keeps the call site
            # honest without a refactor.
            class _InFlightState:
                pass
            _ifs = _InFlightState()
            _ifs.turn = turn
            _ifs.accumulated_cost = accumulated_cost
            _ifs.build_gate_accepted = build_gate_accepted
            _ifs.implementation_plan_accepted = implementation_plan_accepted
            _ifs.verification_attempts = verification_attempts
            _ifs.verification_passed = verification_passed
            _ifs.smoke_ever_passed = smoke_ever_passed
            _ifs.smoke_passed_at_turn = smoke_passed_at_turn
            _ifs.consecutive_errors = consecutive_errors
            _ifs.total_reassessments = total_reassessments
            try:
                update_runtime_state(
                    sandbox_dir=sandbox_dir,
                    state=_ifs,
                    current_model=_current_loop_model(),
                    completion_gate_status=completion_gate_status,
                )
            except OSError:
                pass  # Soft â€” runtime_state staleness is observable via mtime in tests.

        # â˜… CORE: Call Claude with all tools + server-side context management.
        #
        # Phase 4 Path B Step 1: the API call boundary (retry loop + PTL
        # recovery + rate-limit backoff + APIConnectionError handling) is
        # extracted to ``puzzleeval.agents.agent5.api_call.make_builder_api_call``.
        # This call site provides the loop-local state in a typed
        # ``BuilderAPICallContext`` and pattern-matches on the ``APICallOutcome``
        # tagged union. The orchestration spine (turn counter, accumulated
        # cost, response handling) stays here in the loop.
        #
        # Agent 5 lead model stays fixed from turn 0. The active build gate
        # still controls which scaffold actions are legal.
        from puzzleeval.agents.agent5.api_call import (
            APICallFailure,
            BuilderAPICallContext,
            make_builder_api_call,
        )
        from puzzleeval.config import output_config_for_request

        call_start = time.time()
        current_model = _current_loop_model()
        current_phase = _build_phase_name(
            build_gate_accepted=build_gate_accepted,
            smoke_ever_passed=smoke_ever_passed,
        )
        stop_heartbeat = _start_turn_heartbeat(
            progress_callback=progress_callback,
            sandbox_dir=sandbox_dir,
            candidate_name=candidate.name,
            turn=turn,
            max_turns=effective_max_turns,
            phase=current_phase,
            model=current_model,
            interval_seconds=AGENT5_PROGRESS_HEARTBEAT_SECONDS,
        )

        try:
            api_outcome = make_builder_api_call(BuilderAPICallContext(
                client=client,
                current_model=current_model,
                current_max_tokens=AGENT5_MAX_OUTPUT_TOKENS,
                messages=messages,
                system_text=_with_shared_preamble(
                    _with_builder_appendix(
                        _render_builder_prompt(
                            BUILDER_SYSTEM_PROMPT,
                            test_cases=test_cases_for_builder,
                        )
                    )
                ),
                tools=_tools_for_current_turn(),
                output_config=output_config_for_request(),
                max_retries=3,
                candidate=candidate,
                candidate_label=candidate_label,
                sandbox_dir=sandbox_dir,
                trace_id=trace_id,
                turn=turn,
                accumulated_cost=accumulated_cost,
                candidate_web_fetch_blocks=candidate_web_fetch_blocks,
                read_harness_code=_read_harness_code,
                apply_message_cache_breakpoint=_apply_message_cache_breakpoint,
                logger=logger,
            ))
        finally:
            stop_heartbeat()
        if isinstance(api_outcome, APICallFailure):
            completion_gate_status = "build_failed_infrastructure"
            if not completion_gate_issues:
                completion_gate_issues.append(api_outcome.failed_harness.failure_reason[:500])
            _append_build_progress_event(sandbox_dir, {
                "event": "build_turn_failed",
                "candidate_name": candidate.name,
                "turn": turn + 1,
                "max_turns": effective_max_turns,
                "phase": current_phase,
                "model": current_model,
                "elapsed_ms": int((time.time() - call_start) * 1000),
                "failure_reason": api_outcome.failed_harness.failure_reason[:300],
            })
            _append_build_progress_event(sandbox_dir, {
                "event": "completion_gate_final",
                "candidate_name": candidate.name,
                "turn": turn,
                "status": completion_gate_status,
                "verification_passed": False,
                "issues": completion_gate_issues[-10:],
            })
            _flush_observability_snapshot(
                current_model=current_model,
                final_completion_status=completion_gate_status,
                final_completion_issues=completion_gate_issues,
            )
            return api_outcome.failed_harness
        response = api_outcome.response

        # [logging] Log this call's metrics
        log_llm_call(
            logger=logger, response=response, model=current_model,
            trace_id=trace_id, start_time=call_start,
            operation=f"harness_build_{candidate_label}_turn{turn}",
        )

        # [observability] Capture per-turn latency so conversation_log
        # has wall-clock data alongside token/cost data. log_llm_call
        # captures latency to stderr structured logs but doesn't write
        # to the persisted turn dict; without this we have no way to
        # correlate "this turn cost $X" with "this turn took Ys" when
        # diagnosing perf issues post-hoc.
        call_latency_ms = round((time.time() - call_start) * 1000, 2)

        # [cost tracking] All costs (executor + advisor + cache + web search)
        # calculated from the iterations array per Anthropic API docs
        call_cost = _calculate_call_cost(response, current_model)
        accumulated_cost += call_cost
        _append_build_progress_event(sandbox_dir, {
            "event": "build_turn_completed",
            "candidate_name": candidate.name,
            "turn": turn + 1,
            "max_turns": effective_max_turns,
            "phase": current_phase,
            "model": current_model,
            "elapsed_ms": int(call_latency_ms),
            "cost_usd": round(call_cost, 4),
            "cumulative_cost_usd": round(accumulated_cost, 4),
        })

        # [tracking] Web search count for logging
        server_tool_use = getattr(response.usage, "server_tool_use", None)
        if server_tool_use:
            total_web_searches += getattr(server_tool_use, "web_search_requests", 0) or 0

        # â”€â”€ Log this turn for conversation history â”€â”€
        # Phase 4 Path B: turn_log construction extracted to
        # agent5.turn_blocks.build_initial_turn_log. The dict is
        # subsequently mutated by the response.content iteration below
        # (text accumulation + tool_calls / tool_results appends).
        from puzzleeval.agents.agent5.turn_blocks import build_initial_turn_log
        turn_log = build_initial_turn_log(
            response,
            turn=turn,
            current_model=current_model,
            call_cost=call_cost,
            call_latency_ms=call_latency_ms,
        )

        server_tool_inputs_by_id: dict[str, object] = {}
        for block in response.content:
            if block.type == "text":
                turn_log["text"] += block.text
            elif block.type == "thinking":
                # Log thinking blocks for debugging visibility
                thinking_text = getattr(block, "thinking", "")
                if "thinking" not in turn_log:
                    turn_log["thinking"] = []
                turn_log["thinking"].append(thinking_text[:500])
            elif block.type == "server_tool_use" and getattr(block, "name", "") in {
                "web_fetch", "web_search", "advisor",
            }:
                raw_input = getattr(block, "input", None) or {}
                if hasattr(raw_input, "model_dump"):
                    raw_input = raw_input.model_dump()
                elif hasattr(raw_input, "dict"):
                    raw_input = raw_input.dict()
                if isinstance(raw_input, dict):
                    server_tool_inputs_by_id[getattr(block, "id", "")] = dict(raw_input)
                    logged_input = {
                        k: (str(v)[:500] if isinstance(v, str) else v)
                        for k, v in raw_input.items()
                    }
                elif getattr(block, "name", "") == "advisor":
                    logged_input = "(advisor call)"
                else:
                    logged_input = "(server tool - non-dict input)"
                turn_log["tool_calls"].append({
                    "tool": getattr(block, "name", ""),
                    "id": getattr(block, "id", ""),
                    "input": logged_input,
                })
            elif block.type == "tool_use":
                tool_entry = {"tool": block.name, "id": block.id}
                if block.name in CUSTOM_TOOL_NAMES:
                    tool_entry["input"] = block.input
                else:
                    # [observability] Capture server-tool inputs (web_fetch URL,
                    # web_search query) instead of dropping them â€” the URL or
                    # query is exactly the diagnostic info that lets the
                    # operator see WHAT Claude is researching this turn.
                    # Pre-NEW-AM the input was replaced with a generic
                    # placeholder string and the URL/query was thrown away,
                    # making mid-build debugging impossible.
                    raw_input = getattr(block, "input", None) or {}
                    if isinstance(raw_input, dict):
                        tool_entry["input"] = {
                            k: (str(v)[:300] if isinstance(v, str) else v)
                            for k, v in raw_input.items()
                        }
                    else:
                        tool_entry["input"] = "(server tool â€” non-dict input)"
                turn_log["tool_calls"].append(tool_entry)
            elif block.type == "advisor_tool_result":
                content = getattr(block, "content", None)
                advice_text = ""
                if content and hasattr(content, "text"):
                    advice_text = content.text[:500]
                elif content and hasattr(content, "encrypted_content"):
                    advice_text = "(encrypted advisor response)"
                turn_log["tool_results"].append({"tool": "advisor", "result": advice_text})
            # [observability] Capture web_fetch / web_search server-tool RESULTS
            # so the operator can see WHAT Claude actually found this turn.
            # Pre-NEW-AM these block types passed through silently â€” we knew
            # Claude called web_fetch but had no idea what page came back
            # (which is critical when debugging "why did Claude pick the
            # wrong endpoint?"). Truncate aggressively (500 chars per blob)
            # to keep the conversation_log readable; the full fetched docs
            # are persisted separately as fetched_docs_*.txt.
            elif block.type == "web_fetch_tool_result":
                tool_use_id = getattr(block, "tool_use_id", "") or ""
                requested_input = server_tool_inputs_by_id.get(tool_use_id, {})
                requested_url = (
                    requested_input.get("url", "")
                    if isinstance(requested_input, dict)
                    else ""
                )
                fetched_url = ""
                fetched_preview = ""
                fetched_chars = 0
                error_code = ""
                error_message = ""
                status = "empty"
                content = getattr(block, "content", None)
                if content is not None:
                    if getattr(content, "type", "") == "web_fetch_tool_result_error":
                        error_code = getattr(content, "error_code", "") or ""
                        error_message = (
                            getattr(content, "error_message", "")
                            or getattr(content, "message", "")
                            or ""
                        )
                        status = "error"
                    else:
                        inner = getattr(content, "content", None)
                        if inner is not None:
                            fetched_url = getattr(inner, "url", "") or ""
                            source = getattr(inner, "source", None)
                            if source is not None:
                                data = getattr(source, "data", "") or ""
                                fetched_chars = len(data)
                                fetched_preview = data[:500]
                                status = "success" if fetched_chars > 0 else "empty"
                turn_log["tool_results"].append({
                    "tool": "web_fetch",
                    "id": tool_use_id,
                    "url": fetched_url or requested_url,
                    "requested_url": requested_url,
                    "chars_returned": fetched_chars,
                    "status": status,
                    "error_code": error_code,
                    "error_message": error_message[:300],
                    "preview": fetched_preview,
                })
            elif block.type == "web_search_tool_result":
                tool_use_id = getattr(block, "tool_use_id", "") or ""
                requested_input = server_tool_inputs_by_id.get(tool_use_id, {})
                query = (
                    requested_input.get("query", "")
                    if isinstance(requested_input, dict)
                    else ""
                )
                content = getattr(block, "content", None)
                results_summary = []
                result_count = 0
                if isinstance(content, list):
                    result_count = len(content)
                    for r in content[:5]:  # capture top 5 hits
                        title = getattr(r, "title", "") or (r.get("title", "") if isinstance(r, dict) else "")
                        url = getattr(r, "url", "") or (r.get("url", "") if isinstance(r, dict) else "")
                        snippet = (
                            getattr(r, "page_snippet", "")
                            or getattr(r, "snippet", "")
                            or (r.get("page_snippet", "") if isinstance(r, dict) else "")
                            or (r.get("snippet", "") if isinstance(r, dict) else "")
                        )
                        results_summary.append({
                            "title": title[:120],
                            "url": url[:200],
                            "snippet": str(snippet)[:240],
                        })
                turn_log["tool_results"].append({
                    "tool": "web_search",
                    "id": tool_use_id,
                    "query": query,
                    "result_count": result_count,
                    "top_results": results_summary,
                })
        conversation_log.append(turn_log)

        # Phase 3: remember terminal research URLs immediately after the
        # server-tool results are logged. This cannot prevent wasted work
        # inside the current model call, but it prevents later turns from
        # retrying the same blocked/empty URL family without evidence.
        try:
            from puzzleeval.agents.agent5.build_decisions import append_build_decision
            from puzzleeval.agents.agent5.research_memory import (
                mark_terminal_research_url,
                terminal_reason_for_fetch_result,
            )

            for _research_result in turn_log.get("tool_results") or []:
                reason = terminal_reason_for_fetch_result(_research_result)
                if not reason:
                    continue
                url = (
                    _research_result.get("url")
                    or _research_result.get("requested_url")
                    or ""
                )
                entry = mark_terminal_research_url(
                    sandbox_dir,
                    url=str(url),
                    reason=reason,
                    turn=turn,
                    status=str(_research_result.get("status") or ""),
                    error_code=str(_research_result.get("error_code") or ""),
                    source="web_fetch",
                )
                if entry:
                    append_build_decision(
                        sandbox_dir,
                        event="research_url_marked_terminal",
                        turn=turn,
                        candidate_name=candidate.name,
                        url=entry.get("url"),
                        reason=reason,
                        attempts=entry.get("attempts"),
                    )
                    _append_build_progress_event(sandbox_dir, {
                        "event": "research_url_marked_terminal",
                        "candidate_name": candidate.name,
                        "turn": turn,
                        "url": entry.get("url"),
                        "reason": reason,
                        "attempts": entry.get("attempts"),
                    })
        except Exception:  # noqa: BLE001 - research memory is advisory
            pass

        # [observability] Incremental conversation_log.json save â€” written
        # after EVERY turn instead of only at end-of-build. Lets the
        # operator `cat conversation_log.json` mid-build to see exactly
        # what Claude did each turn (text emitted, tools called, URLs
        # fetched, search queries). Previously the file only existed
        # AFTER the build finished â€” useless for debugging a stuck build.
        # Best-effort: failures here MUST NOT break the build loop.
        try:
            (sandbox_dir / "conversation_log.json").write_text(
                json.dumps(conversation_log, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass

        # Per-turn progress callback â€” let the frontend show live build progress.
        #
        # Phase 4 Path B extraction: rich SSE payload construction
        # (phase computation, tool_calls_detail summarization, text_preview)
        # moved to ``puzzleeval.agents.agent5.turn_blocks.emit_build_turn_progress``.
        # This call site provides the loop-local state (turn, model,
        # accumulators) that the helper composes into the event.
        from puzzleeval.agents.agent5.turn_blocks import emit_build_turn_progress
        emit_build_turn_progress(
            progress_callback,
            candidate_name=candidate.name,
            turn=turn,
            max_turns=effective_max_turns,
            build_gate_accepted=build_gate_accepted,
            smoke_ever_passed=smoke_ever_passed,
            current_model=current_model,
            call_cost=call_cost,
            accumulated_cost=accumulated_cost,
            call_latency_ms=call_latency_ms,
            # cache_read/cache_create live on turn_log under the
            # *_tokens names (build_initial_turn_log populated them).
            cache_read=turn_log.get("cache_read_tokens", 0),
            cache_create=turn_log.get("cache_create_tokens", 0),
            response=response,
            turn_log=turn_log,
        )

        # â”€â”€ Handle compaction (server-side context management) â”€â”€
        # When the API compacts context, it returns stop_reason="compaction".
        # We just continue â€” the API handles cleanup on next call.
        if response.stop_reason == "compaction":
            invalidate_read_dedup_state(build_read_state)
            logger.info(f"Server-side compaction for {candidate.name}", extra={
                "operation": "server_compaction",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "turn": turn,
            })
            messages.append({"role": "assistant", "content": response.content})
            if not _assistant_content_has_local_tool_uses(list(response.content or [])):
                try:
                    messages.append(restore_after_server_compaction(sandbox_dir))
                    logger.info(
                        "Restored bounded artifact packet after server-side compaction for %s",
                        candidate.name,
                        extra={
                            "operation": "server_compaction_artifact_restoration",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - restoration is fail-open
                    logger.warning(
                        "Server-side compaction restoration failed open",
                        extra={
                            "operation": "server_compaction_artifact_restoration_failed",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:300],
                        },
                    )
            turn += 1
            continue

        # â”€â”€ Universal orphan-server-tool-use scrubber â”€â”€
        # Real-run signal (traces voice_debug_4 + voice_debug_5): the API
        # occasionally returns content with a `server_tool_use` block lacking
        # its matching `_tool_result` (max_tokens truncation, server-side
        # races). Appending to history would 400 the next API call. Strip
        # orphans BEFORE appending, on EVERY turn.
        #
        # Phase 4.1: detection + cleanup logic moved to
        # ``puzzleeval.agents.agent5.turn_blocks.strip_orphan_server_tool_uses``.
        # Logging + control-flow fallback stay here in the loop.
        from puzzleeval.agents.agent5.turn_blocks import strip_orphan_server_tool_uses
        _cleaned, _orphan_ids, _mutation_ok = strip_orphan_server_tool_uses(response)
        if _orphan_ids:
            logger.warning(
                f"Stripping {len(_orphan_ids)} orphan server tool_use block(s) "
                f"for {candidate.name} at turn {turn} (stop_reason={response.stop_reason})",
                extra={
                    "operation": "orphan_server_tool_strip",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turn": turn,
                    "stop_reason": response.stop_reason,
                    "orphan_count": len(_orphan_ids),
                },
            )
            if not _mutation_ok:
                # Could not mutate response.content (frozen response object).
                # Treat this turn as a pause: append the cleaned content
                # locally + nudge, increment turn, continue loop.
                messages.append({"role": "assistant", "content": _cleaned})
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "Server-side research was truncated. Summarize "
                            "what you have and proceed â€” do not re-issue "
                            "the same search."
                        ),
                    }],
                })
                turn += 1
                continue

        # â”€â”€ Handle pause_turn (server tool loop took too long) â”€â”€
        if response.stop_reason == "pause_turn":
            logger.info(f"pause_turn for {candidate.name}, continuing", extra={
                "operation": f"harness_pause_turn_{candidate_label}",
                "trace_id": trace_id,
                "turn": turn,
            })
            # Don't reset messages â€” server-side context management (compact)
            # handles overflow. Resetting loses all prior research and tool results.
            messages.append({"role": "assistant", "content": response.content})
            turn += 1
            continue

        # â”€â”€ Extract text from response â”€â”€
        last_text = _extract_text_from_response(response)

        # Also check agent's text for smoke test pass / completion signals.
        # Phase 4 Path B Step 2: detection extracted to dispatch_helpers
        # (pure predicates, see agent5/dispatch_helpers.py).
        from puzzleeval.agents.agent5.dispatch_helpers import (
            detect_harness_signal,
            detect_smoke_pass,
        )
        if detect_smoke_pass(last_text) and not smoke_ever_passed:
            smoke_ever_passed = True
            smoke_passed_at_turn = turn

        # â”€â”€ Pre-HARNESS_COMPLETE reflection telemetry (early observation) â”€â”€
        # Records whether reflection_phase_3.md was present at the moment
        # the agent emitted HARNESS_COMPLETE â€” informational; the actual
        # gate runs inside the verification sequence below. The
        # directive-sent flag is set ONLY by the gate path so the
        # template injection logic in the retry branch works correctly.
        if (
            _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED
            and detect_harness_signal(last_text) == "complete"
        ):
            _reflection_path = sandbox_dir / "_agent_state" / "reflection_phase_3.md"
            logger.info(
                "Reflection phase 3 early telemetry for %s at turn %d (reflection_present=%s)",
                candidate.name, turn, _reflection_path.exists(),
                extra={
                    "operation": "reflection_phase_3_early_telemetry",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turn": turn,
                    "reflection_present": _reflection_path.exists(),
                },
            )

        # Do not accept HARNESS_COMPLETE here. The unified completion
        # gate below runs structural, forensics, and reflection checks.
        if smoke_ever_passed and detect_harness_signal(last_text) == "complete":
            logger.info(
                "Completion signal observed for %s; routing through verification gate",
                candidate.name,
                extra={
                    "operation": "completion_signal_text_observed",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turn": turn,
                },
            )

        # â”€â”€ Extract and save web_fetch content to files â”€â”€
        empty_fetch_urls: list[str] = []
        for _tr in turn_log.get("tool_results") or []:
            if not isinstance(_tr, dict) or _tr.get("tool") != "web_fetch":
                continue
            if int(_tr.get("chars_returned", 0) or 0) > 0:
                continue
            _url = str(_tr.get("url") or "").strip() or "<unknown-url>"
            empty_fetch_counts[_url] = empty_fetch_counts.get(_url, 0) + 1
            empty_fetch_urls.append(_url)
        if (
            response.stop_reason == "end_turn"
            and empty_fetch_urls
            and detect_harness_signal(last_text) not in {"complete", "failed"}
        ):
            logger.info(
                "Empty web_fetch result steering for %s at turn %d: %s",
                candidate.name, turn, empty_fetch_urls[:3],
                extra={
                    "operation": "agent5_empty_fetch_steering",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turn": turn,
                    "empty_fetch_urls": empty_fetch_urls[:5],
                },
            )
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": (
                    "Research efficiency guard: the previous web_fetch returned "
                    "no usable content for "
                    f"{', '.join(empty_fetch_urls[:3])}. Do NOT retry the same "
                    "URL/query family. Switch to official docs search snippets, "
                    "SDK examples, cached fetched_docs_*.txt, or commit the "
                    "known facts to research_synthesis.json and "
                    "implementation_plan.json with non-blocking risks."
                ),
            })
            turn += 1
            continue

        new_docs = _extract_and_save_web_content(
            response, sandbox_dir, existing_count=len(saved_doc_files),
        )
        if new_docs:
            saved_doc_files.extend(new_docs)
            conversation_log.append({
                "turn": f"save-docs-{turn}",
                "stop_reason": "docs_saved",
                "text": f"Saved fetched docs to: {new_docs}",
                "tool_calls": [], "tool_results": [],
            })

        # â”€â”€ Context management â”€â”€
        # No manual context reset. Server-side context management handles compression:
        #   - clear_tool_uses_20250919: clears old tool results at 80K tokens (keeps last 5)
        #   - compact_20260112: Claude-powered summarization at 150K tokens
        # This is how Claude Code handles context â€” gradual compression, not hard deletion.
        # The builder keeps research context available for debugging. If it needs a detail
        # from the API docs during build/debug, it remains accessible through artifacts.

        # â”€â”€ Check for completion signal â”€â”€
        # Phase 4 Path B Step 2: signal detection via dispatch_helpers.
        _signal = detect_harness_signal(last_text)
        if response.stop_reason == "end_turn":
            if _signal == "complete":
                completion_gate_status = "running"
                _append_build_progress_event(sandbox_dir, {
                    "event": "completion_gate_attempt",
                    "candidate_name": candidate.name,
                    "turn": turn,
                    "attempt": verification_attempts + 1,
                    "max_repair_retries": AGENT5_MAX_VERIFICATION_RETRIES,
                })
                # â”€â”€ PR 1 (deferred wiring): build_plan staleness at pre-HARNESS_COMPLETE â”€â”€
                # The agent is about to claim completion. If they haven't
                # touched build_plan.md since the last trigger, that's a
                # signal the artifact wasn't operational this build â€”
                # nudge them once. Soft-tier: telemetry + one nudge, no
                # block on completion.
                if (
                    _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED
                    and _cfg.AUTONOMY_BUILD_PLAN_DIRECTIVES_ENABLED
                ):
                    _bp_path = sandbox_dir / "_agent_state" / "build_plan.md"
                    _bp_mtime = 0.0
                    if _bp_path.exists():
                        try:
                            _bp_mtime = _bp_path.stat().st_mtime
                        except OSError:
                            pass
                    _bp_stale = _bp_mtime <= last_build_plan_mtime
                    logger.info(
                        "Build plan check at pre-HARNESS_COMPLETE for %s: stale=%s",
                        candidate.name, _bp_stale,
                        extra={
                            "operation": (
                                EVENT_BUILD_PLAN_STALE_AT_TRIGGER if _bp_stale
                                else "autonomy_build_plan_updated_at_trigger"
                            ),
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "trigger": "pre-HARNESS_COMPLETE",
                        },
                    )
                    last_build_plan_mtime = _bp_mtime

                # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
                # â˜… VERIFICATION GATE â€” the core hardening
                # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
                # Run the current-state gate. This helper re-reads the
                # sandbox and intentionally ignores historical issues.
                issues = _current_completion_gate_issues(
                    attempt=verification_attempts + 1,
                )
                inject_reflection_directive_with_feedback = (
                    bool(issues)
                    and "reflection" in str(issues).lower()
                    and not reflection_phase_3_directive_sent
                )
                if issues:
                    issue_key = _completion_gate_issue_key(issues)
                    issue_retry_count = completion_gate_issue_retries.get(issue_key, 0)
                    if issue_retry_count >= AGENT5_MAX_VERIFICATION_RETRIES:
                        verification_passed = False
                        completion_gate_status = "failed_retries_exhausted"
                        completion_gate_issues.append(
                            f"HARNESS_COMPLETE still fails gate issue {issue_key!r} "
                            f"after {issue_retry_count} repair retries: {issues}"
                        )
                        try:
                            from puzzleeval.agents.agent5.build_decisions import append_build_decision
                            from puzzleeval.agents.agent5.failure_packets import write_failure_packet

                            packet = write_failure_packet(
                                sandbox_dir,
                                turn=turn,
                                candidate_name=candidate.name,
                                failure_source="completion_gate",
                                command="HARNESS_COMPLETE",
                                exit_code=None,
                                result_text=str(issues),
                                issues=[str(issues)],
                                conversation_log=conversation_log,
                                diagnostic_reviewer=_failure_packet_reviewer,
                            )
                            append_build_decision(
                                sandbox_dir,
                                event="completion_gate_failed",
                                turn=turn,
                                candidate_name=candidate.name,
                                issue_key=issue_key,
                                status=completion_gate_status,
                                packet_path=packet.get("path"),
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        _append_build_progress_event(sandbox_dir, {
                            "event": "completion_gate_failed",
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "attempt": verification_attempts + 1,
                            "issue_key": issue_key,
                            "issue_retry_count": issue_retry_count,
                            "status": completion_gate_status,
                            "issues": [str(issues)],
                        })
                        logger.warning(
                            "Completion gate failed after retry exhaustion for %s",
                            candidate.name,
                            extra={
                                "operation": "completion_gate_failed",
                                "trace_id": trace_id,
                                "candidate_name": candidate.name,
                                "attempt": verification_attempts + 1,
                                "completion_gate_status": completion_gate_status,
                            },
                        )
                        break
                    completion_gate_status = "retrying"
                    completion_gate_issues.append(str(issues))
                    try:
                        from puzzleeval.agents.agent5.build_decisions import append_build_decision
                        from puzzleeval.agents.agent5.failure_packets import write_failure_packet

                        packet = write_failure_packet(
                            sandbox_dir,
                            turn=turn,
                            candidate_name=candidate.name,
                            failure_source="completion_gate",
                            command="HARNESS_COMPLETE",
                            exit_code=None,
                            result_text=str(issues),
                            issues=[str(issues)],
                            conversation_log=conversation_log,
                            diagnostic_reviewer=_failure_packet_reviewer,
                        )
                        append_build_decision(
                            sandbox_dir,
                            event="completion_gate_failed",
                            turn=turn,
                            candidate_name=candidate.name,
                            issue_key=issue_key,
                            status=completion_gate_status,
                            packet_path=packet.get("path"),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    _append_build_progress_event(sandbox_dir, {
                        "event": "completion_gate_failed",
                        "candidate_name": candidate.name,
                        "turn": turn,
                        "attempt": verification_attempts + 1,
                        "issue_key": issue_key,
                        "issue_retry_count": issue_retry_count,
                        "status": completion_gate_status,
                        "issues": [str(issues)],
                    })
                    logger.info(f"Verification issues for {candidate.name}, retry {verification_attempts + 1}", extra={
                        "operation": "verification_gate_fail",
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "attempt": verification_attempts + 1,
                    })
                    # Feed issues back to Claude for fixing
                    feedback = (
                        f"## Verification Issues Found â€” Please Fix\n\n"
                        f"Your harness signaled complete but verification found problems:\n\n"
                        f"{issues}\n\n"
                        f"Please debug from the specific gate evidence above, patch the final harness "
                        f"or artifact that explains it, run the cheapest relevant check/probe, "
                        f"and signal HARNESS_COMPLETE again when the production-equivalence evidence is fixed."
                    )
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": feedback})
                    # When the reflection gate fired AND the directive
                    # template hasn't been shown yet, append it as a
                    # second user message so the agent has the canonical
                    # section structure on hand. Subsequent retries see
                    # only the brief feedback (avoids prompt bloat).
                    if inject_reflection_directive_with_feedback:
                        messages.append({
                            "role": "user",
                            "content": [{
                                "type": "text",
                                "text": REFLECTION_PHASE_3_DIRECTIVE,
                            }],
                        })
                        reflection_phase_3_directive_sent = True
                        logger.info(
                            "Reflection phase 3 directive template injected for %s",
                            candidate.name,
                            extra={
                                "operation": EVENT_REFLECTION_PHASE_3_DIRECTIVE_FIRED,
                                "trace_id": trace_id,
                                "candidate_name": candidate.name,
                                "turn": turn,
                            },
                        )
                        if _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED:
                            try:
                                class _ReflectionDirectiveState:
                                    pass
                                _rds = _ReflectionDirectiveState()
                                _rds.turn = turn
                                _rds.accumulated_cost = accumulated_cost
                                _rds.build_gate_accepted = build_gate_accepted
                                _rds.implementation_plan_accepted = implementation_plan_accepted
                                _rds.verification_attempts = verification_attempts
                                _rds.verification_passed = verification_passed
                                _rds.smoke_ever_passed = smoke_ever_passed
                                _rds.smoke_passed_at_turn = smoke_passed_at_turn
                                _rds.consecutive_errors = consecutive_errors
                                _rds.total_reassessments = total_reassessments
                                update_runtime_state(
                                    sandbox_dir=sandbox_dir,
                                    state=_rds,
                                    current_model=_current_loop_model(),
                                    completion_gate_status=completion_gate_status,
                                    directive_fired={
                                        "directive": "REFLECTION_PHASE_3_DIRECTIVE",
                                        "reason": "completion_gate_retry",
                                    },
                                )
                            except OSError:
                                pass
                    conversation_log.append({
                        "turn": f"verify-{verification_attempts + 1}",
                        "stop_reason": "verification_gate",
                        "text": f"VERIFICATION FAILED:\n{issues}",
                        "tool_calls": [],
                        "tool_results": [],
                        "issue_key": issue_key,
                    })
                    completion_gate_issue_retries[issue_key] = issue_retry_count + 1
                    verification_attempts += 1
                    turn += 1
                    continue  # Claude will fix and re-signal
                else:
                    # No issues found â€” verification passed cleanly
                    verification_passed = True
                    completion_gate_status = "passed"
                    completion_gate_issues.clear()
                    try:
                        from puzzleeval.agents.agent5.build_decisions import append_build_decision

                        append_build_decision(
                            sandbox_dir,
                            event="completion_gate_passed",
                            turn=turn,
                            candidate_name=candidate.name,
                            status=completion_gate_status,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    _append_build_progress_event(sandbox_dir, {
                        "event": "completion_gate_passed",
                        "candidate_name": candidate.name,
                        "turn": turn,
                        "attempt": verification_attempts + 1,
                        "status": completion_gate_status,
                    })
                    if _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED:
                        try:
                            class _VerificationPassState:
                                pass
                            _vps = _VerificationPassState()
                            _vps.turn = turn
                            _vps.accumulated_cost = accumulated_cost
                            _vps.build_gate_accepted = build_gate_accepted
                            _vps.implementation_plan_accepted = implementation_plan_accepted
                            _vps.verification_attempts = verification_attempts
                            _vps.verification_passed = verification_passed
                            _vps.smoke_ever_passed = smoke_ever_passed
                            _vps.smoke_passed_at_turn = smoke_passed_at_turn
                            _vps.consecutive_errors = consecutive_errors
                            _vps.total_reassessments = total_reassessments
                            update_runtime_state(
                                sandbox_dir=sandbox_dir,
                                state=_vps,
                                current_model=_current_loop_model(),
                                live_test_status="passing",
                                completion_gate_status=completion_gate_status,
                            )
                        except OSError:
                            pass
                logger.info(f"Harness {'verified' if verification_passed else 'rejected by completion gate'} for {candidate.name}", extra={
                    "operation": "harness_build_complete",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turns_used": turn + 1,
                    "build_cost": accumulated_cost,
                    "verification_attempts": verification_attempts,
                    "verification_passed": verification_passed,
                    "completion_gate_status": completion_gate_status,
                })
                break

            elif _signal == "failed":
                logger.info(f"Harness failed for {candidate.name} (agent reported)", extra={
                    "operation": "harness_build_agent_failed",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turns_used": turn + 1,
                })
                return FailedHarness(
                    candidate_name=candidate.name,
                    provider=candidate.provider,
                    failure_reason=last_text[:500],
                    failure_category=_categorize_failure(last_text),
                    partial_code=_read_harness_code(sandbox_dir),
                    turns_attempted=turn + 1,
                    web_fetch_blocks=candidate_web_fetch_blocks,
                    build_cost_usd=round(accumulated_cost, 4),
                    harness_dir=str(sandbox_dir),
                    completion_gate_status="agent_reported_failed",
                    completion_gate_issues=[last_text[:500]],
                )
            else:
                # end_turn without signal is never completion. A generated
                # harness is necessary but not sufficient: the unified gate
                # must see an explicit HARNESS_COMPLETE and run structural,
                # forensics, and reflection checks.
                if (sandbox_dir / "harness.py").exists():
                    completion_gate_status = "awaiting_explicit_completion"
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({
                        "role": "user",
                        "content": (
                            "harness.py exists, but this build is not accepted until you "
                            "run the required smoke/live validation, write the required "
                            "reflection evidence, and explicitly signal HARNESS_COMPLETE. "
                            "Do not rely on file existence as completion."
                        ),
                    })
                    turn += 1
                    continue
                # Append the assistant response so messages[] actually
                # changes between iterations. Without this, identical
                # prompts cycle and the model is locked into whatever
                # pattern its first response landed on (real-run trace
                # 749b09b1, 2026-04-28: cache_read=88,763 stayed
                # IDENTICAL across 33 turns of the builder narrating`r`n                # instead of acting - $1.65 wasted on
                # cache-replay). Appending the response means at
                # minimum the model now sees its own prior text in
                # history on the next iteration.
                # Pinned by tests/test_build_loop_behavior.py::TestEndTurnNoSignalAppendsAssistant.
                messages.append({"role": "assistant", "content": response.content})
                turn += 1
                continue

        # â”€â”€ Handle tool_use: dispatch custom tools â”€â”€
        if response.stop_reason == "tool_use":
            tool_results = []
            research_synthesis_rejected_this_turn = False
            # Capture pre-transition state so build-gate compaction and
            # progress telemetry fire exactly once on the accepted plan turn.
            build_gate_was_accepted = build_gate_accepted
            content_blocks = list(response.content)
            custom_tool_count = sum(
                1 for b in content_blocks
                if getattr(b, "type", None) == "tool_use"
                and getattr(b, "name", None) in CUSTOM_TOOL_NAMES
            )
            scheduled_local_results: dict[str, _ScheduledLocalToolResult] = {}
            scheduled_local_batch_ids: set[str] = set()
            for block_index, block in enumerate(content_blocks):
                if block.type == "tool_use" and block.name in CUSTOM_TOOL_NAMES:
                    # â”€â”€ ask_research: spawn targeted research sub-agent â”€â”€
                    # Enriches the question with actual context (harness code,
                    # last error) so the research agent can give precise answers
                    # instead of generic API overviews.
                    if block.name == "ask_research":
                        question = block.input.get("question", "")
                        requested_research_task_id = block.input.get("task_id", "")
                        latest_failure_packet = None
                        failure_packet_context = ""
                        if _cfg.FAILURE_PACKET_DEBUG_ENABLED:
                            try:
                                from puzzleeval.agents.agent5.failure_packets import (
                                    build_failure_packet_research_context,
                                    debug_research_attempts_for_packet,
                                    diagnosis_requests_research,
                                    read_latest_failure_packet,
                                )

                                latest_failure_packet = read_latest_failure_packet(sandbox_dir)
                                if latest_failure_packet and diagnosis_requests_research(latest_failure_packet):
                                    attempts = debug_research_attempts_for_packet(
                                        sandbox_dir,
                                        latest_failure_packet,
                                    )
                                    if attempts >= 1:
                                        result_text = (
                                            "ask_research BLOCKED: a research worker has already "
                                            "been fired for the latest failure packet "
                                            f"({latest_failure_packet.get('path')}). Patch code, "
                                            "revise implementation_plan.json, or produce a new "
                                            "failure packet before delegating again."
                                        )
                                        tool_results.append({
                                            "type": "tool_result",
                                            "tool_use_id": block.id,
                                            "content": result_text[:8000],
                                            "is_error": True,
                                        })
                                        if conversation_log:
                                            conversation_log[-1]["tool_results"].append({
                                                "tool": "ask_research",
                                                "result": "BLOCKED (research already fired for latest failure packet)",
                                            })
                                        continue
                                    failure_packet_context = build_failure_packet_research_context(
                                        latest_failure_packet
                                    )
                            except Exception:  # noqa: BLE001 - guard must not break tool dispatch
                                latest_failure_packet = None
                                failure_packet_context = ""
                        # â”€â”€ SCOPED RESEARCH GATE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                        # ask_research is a scoped helper for planned task IDs,
                        # failure-packet gaps, or concrete FIELD NEEDED/WHY
                        # debug gaps. Broad provider discovery remains blocked
                        # by the research-plan validator, not by a phase file.
                        failure_packet_research_allowed = bool(
                            latest_failure_packet and failure_packet_context
                        )

                        if not question:
                            result_text = "Error: empty question. Ask a specific question about the API."
                        else:
                            if _cfg.RESEARCH_WORKERS_ENABLED:
                                try:
                                    from puzzleeval.agents.agent5.research_plan import (
                                        validate_ask_research_scope,
                                    )

                                    scope = validate_ask_research_scope(
                                        sandbox_dir,
                                        question=question,
                                        task_id=requested_research_task_id,
                                        failure_packet_research_allowed=failure_packet_research_allowed,
                                    )
                                    scope_error = ""
                                except Exception as exc:  # noqa: BLE001 - fail closed but explain
                                    scope = None
                                    scope_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                                if scope is None or not scope.allowed:
                                    reason = (
                                        scope.reason if scope is not None
                                        else f"scope verifier failed: {scope_error}"
                                    )
                                    result_text = (
                                        "ask_research BLOCKED: planned research is enabled, "
                                        "but this request is not tied to an unresolved planned "
                                        f"task or concrete debug gap. Reason: {reason}.\n\n"
                                        "Use one of these routes:\n"
                                        "  1. Add or name `task_id` from "
                                        "_agent_state/research_plan.json for unresolved planned research.\n"
                                        "  2. For build/debug drift, ask with FIELD NEEDED and WHY so "
                            "the answer can be recorded as an unplanned debug gap.\n"
                                        "  3. If latest_failure_packet.json diagnosis says research would change "
                                        "implementation, ask the scoped research_question once, then revise "
                                        "code or implementation_plan.json."
                                    )
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": result_text[:8000],
                                        "is_error": True,
                                    })
                                    logger.warning(
                                        "ask_research blocked by scope gate",
                                        extra={
                                            "operation": "ask_research_scope_gate",
                                            "trace_id": trace_id,
                                            "candidate_name": candidate.name,
                                            "turn": turn,
                                            "scope_reason": reason,
                                            "requested_task_id": requested_research_task_id,
                                        },
                                    )
                                    if conversation_log:
                                        conversation_log[-1]["tool_results"].append({
                                            "tool": "ask_research",
                                            "result": f"BLOCKED ({reason})",
                                        })
                                    continue
                            # â”€â”€ SOFT TEMPLATE ADHERENCE LOG (Plan Â§Q3 hybrid) â”€â”€
                            # Inspect the question for the
                            # CANDIDATE/ENDPOINT/KNOWN/FIELD NEEDED/WHY
                            # template fields. We don't reject malformed
                            # calls â€” Opus follows the template reliably
                            # enough that hard validation would produce
                            # false-rejects on benign rephrasings. Logging
                            # only gives us telemetry to detect drift if
                            # adherence ever degrades in production.
                            adherence = _ask_research_template_adherence(question)
                            if not adherence["fully_adherent"]:
                                logger.info(
                                    f"ask_research template adherence: "
                                    f"{len(adherence['fields_present'])}/5 fields "
                                    f"for {candidate.name} at turn {turn} "
                                    f"(missing: {adherence['fields_missing']})",
                                    extra={
                                        "operation": "ask_research_template_adherence",
                                        "trace_id": trace_id,
                                        "candidate_name": candidate.name,
                                        "turn": turn,
                                        "fully_adherent": False,
                                        "adherence_ratio": adherence["adherence_ratio"],
                                        "fields_present": adherence["fields_present"],
                                        "fields_missing": adherence["fields_missing"],
                                    },
                                )
                            else:
                                logger.info(
                                    f"ask_research fully template-adherent for "
                                    f"{candidate.name} at turn {turn}",
                                    extra={
                                        "operation": "ask_research_template_adherence",
                                        "trace_id": trace_id,
                                        "candidate_name": candidate.name,
                                        "turn": turn,
                                        "fully_adherent": True,
                                        "adherence_ratio": 1.0,
                                    },
                                )

                            # Phase 4 Path B Step 3: enrichment extracted to
                            # dispatch_helpers.enrich_research_question.
                            # Caller still owns: prior_results_text aggregation
                            # (depends on tool_results local), harness code
                            # read, and the actual sub-agent invocation.
                            from puzzleeval.agents.agent5.dispatch_helpers import (
                                enrich_research_question,
                            )
                            prior_results_text = " ".join(
                                r.get("content", "") for r in tool_results
                                if isinstance(r.get("content"), str)
                            )
                            enriched_question = enrich_research_question(
                                question=question,
                                candidate_name=candidate.name,
                                candidate_provider=candidate.provider,
                                candidate_docs_url=candidate.verified_api_docs_url,
                                sandbox_dir=sandbox_dir,
                                prior_results_text=prior_results_text,
                                harness_code=_read_harness_code(sandbox_dir),
                            )
                            if failure_packet_context:
                                enriched_question += failure_packet_context
                            if research_handoff:
                                source_state = {
                                    "canonical_docs_urls": (research_handoff.get("canonical_docs_urls") or [])[:5],
                                    "discovered_docs_urls": (research_handoff.get("discovered_docs_urls") or [])[:5],
                                    "dead_or_blocked_urls": (research_handoff.get("dead_or_blocked_urls") or [])[:8],
                                    "unresolved_questions": (research_handoff.get("unresolved_questions") or [])[:5],
                                }
                                enriched_question += (
                                    "\n\n## Source Routing State\n"
                                    "Use canonical_docs_urls first. Treat discovered_docs_urls as unverified "
                                    "fallbacks. Do not retry dead_or_blocked_urls unless your search finds a "
                                    f"new official replacement.\n{json.dumps(source_state, indent=2, ensure_ascii=False)}"
                                )
                            stop_tool_heartbeat, complete_tool = _start_tool_heartbeat(
                                progress_callback=progress_callback,
                                sandbox_dir=sandbox_dir,
                                candidate_name=candidate.name,
                                turn=turn,
                                phase=_build_phase_name(
                                    build_gate_accepted=build_gate_accepted,
                                    smoke_ever_passed=smoke_ever_passed,
                                ),
                                tool_name="ask_research",
                                tool_summary=question,
                                interval_seconds=AGENT5_PROGRESS_HEARTBEAT_SECONDS,
                            )
                            tool_started = time.time()
                            research_cost = 0.0
                            try:
                                if latest_failure_packet and failure_packet_context:
                                    try:
                                        from puzzleeval.agents.agent5.failure_packets import (
                                            record_debug_research_attempt,
                                        )

                                        record_debug_research_attempt(
                                            sandbox_dir,
                                            packet=latest_failure_packet,
                                            question=question,
                                            turn=turn,
                                        )
                                    except Exception:  # noqa: BLE001 - diagnostics only
                                        pass
                                research_answer, research_cost = _run_targeted_research(
                                    client, enriched_question, candidate.name, logger, trace_id,
                                )
                            finally:
                                stop_tool_heartbeat()
                            research_elapsed_ms = int((time.time() - tool_started) * 1000)
                            complete_tool({
                                "elapsed_ms": research_elapsed_ms,
                                "cost_usd": round(research_cost, 4),
                            })
                            accumulated_cost += research_cost
                            result_text = research_answer
                            # Save research result to file for future reference
                            research_file = sandbox_dir / f"research_turn{turn}.txt"
                            try:
                                research_file.write_text(
                                    f"# Research Q: {question}\n\n{research_answer}",
                                    encoding="utf-8",
                                )
                            except OSError:
                                pass
                            try:
                                from puzzleeval.agents.agent5.research_plan import (
                                    record_inline_research_finding,
                                )

                                record_inline_research_finding(
                                    sandbox_dir,
                                    question=question,
                                    answer=research_answer,
                                    turn=turn,
                                )
                            except Exception:  # noqa: BLE001 - research memory is advisory
                                pass
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text[:8000],  # Cap research answers
                        })
                        if conversation_log:
                            conversation_log[-1]["tool_results"].append({
                                "tool": "ask_research",
                                "result": result_text[:500],
                                "tool_elapsed_ms": research_elapsed_ms if not phase1_block and question else 0,
                                "cost_usd": round(research_cost, 4) if not phase1_block and question else 0.0,
                            })
                        continue

                    # â”€â”€ Standard custom tool dispatch â”€â”€
                    # Pass credentials so run_code subprocesses can do live API validation.
                    # Pass build_read_state so the patch_file gate enforces
                    # read-before-patch discipline across the build loop.
                    # Pass phase_state so the write_file gates (B1, B2, B3)
                    # can phase-key their decisions on build_gate_accepted.
                    phase_state = {
                        "build_gate_accepted": build_gate_accepted,
                        "implementation_plan_accepted": implementation_plan_accepted,
                        "candidate_slug": candidate.name,
                        "trace_id": trace_id,
                    }
                    block_command_text = ""
                    if block.name == "run_code" and isinstance(block.input, dict):
                        block_command_text = str(
                            block.input.get("command")
                            or block.input.get("code")
                            or ""
                        )
                    tool_extra_env = (
                        _offline_smoke_env(credentials)
                        if block.name == "run_code" and _is_smoke_test_command(block_command_text)
                        else credentials
                    )
                    tool_metadata: dict[str, Any] = {}
                    block_id = str(block.id)
                    if _is_concurrency_safe_local_tool(block):
                        if block_id not in scheduled_local_results:
                            batch = _collect_concurrency_safe_batch(content_blocks, block_index)
                            if len(batch) > 1:
                                scheduled_local_batch_ids.update(str(b.id) for b in batch)
                                _append_build_progress_event(sandbox_dir, {
                                    "event": "local_tool_batch_started",
                                    "candidate_name": candidate.name,
                                    "turn": turn,
                                    "batch_type": "concurrency_safe_read_only",
                                    "tool_count": len(batch),
                                    "tools": [str(getattr(b, "name", "")) for b in batch],
                                })
                            batch_results = _execute_concurrency_safe_tool_batch(
                                blocks=batch,
                                sandbox_dir=sandbox_dir,
                                credentials=credentials,
                                read_state=build_read_state,
                                phase_state=phase_state,
                                code_timeout_s=AGENT5_CODE_TIMEOUT,
                                dispatch_fn=dispatch_tool_result,
                            )
                            scheduled_local_results.update(batch_results)
                            if len(batch) > 1:
                                _append_build_progress_event(sandbox_dir, {
                                    "event": "local_tool_batch_completed",
                                    "candidate_name": candidate.name,
                                    "turn": turn,
                                    "batch_type": "concurrency_safe_read_only",
                                    "tool_count": len(batch),
                                    "tools": [str(getattr(b, "name", "")) for b in batch],
                                })
                                _append_build_progress_event(sandbox_dir, {
                                    "event": "runtime_parallel_tool_batch",
                                    "candidate_name": candidate.name,
                                    "turn": turn,
                                    "batch_type": "concurrency_safe_read_only",
                                    "tool_count": len(batch),
                                    "tools": [str(getattr(b, "name", "")) for b in batch],
                                })
                        scheduled = scheduled_local_results[block_id]
                        result_text = scheduled.content
                        exit_code = scheduled.exit_code
                        tool_elapsed_ms = scheduled.elapsed_ms
                        tool_metadata = scheduled.metadata
                    else:
                        if custom_tool_count > 1:
                            _append_build_progress_event(sandbox_dir, {
                                "event": "serial_tool_batch_reason",
                                "candidate_name": candidate.name,
                                "turn": turn,
                                "tool": str(block.name),
                                "reason": "side_effectful_or_stateful",
                            })
                        use_tool_heartbeat = block.name == "run_code"
                        stop_tool_heartbeat: Callable[[], None] = lambda: None
                        complete_tool: Callable[[dict[str, Any]], None] = lambda _extra: None
                        tool_started = time.time()
                        if use_tool_heartbeat:
                            stop_tool_heartbeat, complete_tool = _start_tool_heartbeat(
                                progress_callback=progress_callback,
                                sandbox_dir=sandbox_dir,
                                candidate_name=candidate.name,
                                turn=turn,
                                phase=_build_phase_name(
                                    build_gate_accepted=build_gate_accepted,
                                    smoke_ever_passed=smoke_ever_passed,
                                ),
                                tool_name=block.name,
                                tool_summary=_tool_summary_for_progress(block.name, block.input),
                                interval_seconds=AGENT5_PROGRESS_HEARTBEAT_SECONDS,
                            )
                        try:
                            dispatched = dispatch_tool_result(
                                block.name, block.input, sandbox_dir,
                                extra_env=tool_extra_env, read_state=build_read_state,
                                code_timeout_s=AGENT5_CODE_TIMEOUT,
                                phase_state=phase_state,
                            )
                            result_text = dispatched.content
                            exit_code = dispatched.exit_code
                            tool_metadata = dispatched.metadata
                        finally:
                            stop_tool_heartbeat()
                        tool_elapsed_ms = int((time.time() - tool_started) * 1000)
                        if use_tool_heartbeat:
                            complete_tool({
                                "elapsed_ms": tool_elapsed_ms,
                                "exit_code": exit_code,
                                "is_error": exit_code != 0,
                            })
                    if block_id in scheduled_local_batch_ids:
                        tool_metadata = {**tool_metadata, "runtime_parallel_batch": True}
                    # Persist large outputs to disk (Claude Code pattern: >30KB -> file)
                    result_text = _persist_large_output(result_text, sandbox_dir, turn)
                    if (
                        exit_code == 0
                        and _cfg.RESEARCH_WORKERS_ENABLED
                        and _tool_targets_relative_path(
                            block,
                            "_agent_state/research_plan.json",
                        )
                    ):
                        try:
                            planned_result, planned_cost = _run_planned_research_workers(
                                client=client,
                                sandbox_dir=sandbox_dir,
                                candidate=candidate,
                                logger=logger,
                                trace_id=trace_id,
                                targeted_research=_run_targeted_research,
                            )
                            accumulated_cost += planned_cost
                            result_text = (
                                f"{result_text}\n\n{planned_result}\n"
                                f"Planned research cost: ${planned_cost:.4f}."
                            )
                        except Exception as exc:  # noqa: BLE001 - builder must see failure
                            exit_code = 1
                            result_text = (
                                f"{result_text}\n\n"
                                "Error: planned research workers failed after "
                                "research_plan.json was accepted: "
                                f"{type(exc).__name__}: {str(exc)[:500]}. "
                                "Read _agent_state/research_plan.json, revise only "
                                "the failed scoped task if needed, or continue with "
                                "documented NOT_FOUND evidence if the provider docs "
                                "are unavailable."
                            )
                            logger.warning(
                                "Planned research workers failed for %s",
                                candidate.name,
                                extra={
                                    "operation": "planned_research_workers_failed",
                                    "trace_id": trace_id,
                                    "candidate_name": candidate.name,
                                    "error_type": type(exc).__name__,
                                    "error_msg": str(exc)[:500],
                                },
                            )
                    diagnostics_result = None
                    mutation_path = _successful_python_mutation_path(
                        block,
                        result_text=result_text,
                        exit_code=exit_code,
                    )
                    if (
                        mutation_path
                        and getattr(_cfg, "CODE_DIAGNOSTICS_ENABLED", True)
                    ):
                        try:
                            from puzzleeval.agents.agent5.code_diagnostics import (
                                format_new_diagnostics_for_tool_result,
                                run_python_diagnostics,
                            )

                            diagnostics_result = run_python_diagnostics(
                                sandbox_dir,
                                [mutation_path],
                                turn=turn,
                                source_tool=str(block.name),
                            )
                            diagnostic_text = format_new_diagnostics_for_tool_result(
                                diagnostics_result
                            )
                            if diagnostic_text:
                                result_text = f"{result_text}{diagnostic_text}"
                            tool_metadata["code_diagnostics"] = {
                                "checked_paths": diagnostics_result.checked_paths,
                                "new_count": len(diagnostics_result.new_diagnostics),
                                "active_count": len(diagnostics_result.active_diagnostics),
                                "cleared_paths": diagnostics_result.cleared_paths,
                                "registry_path": diagnostics_result.registry_path,
                                "error": diagnostics_result.error,
                            }
                            if diagnostics_result.error:
                                logger.warning(
                                    "Python diagnostics failed open",
                                    extra={
                                        "operation": "code_diagnostics_failed_open",
                                        "trace_id": trace_id,
                                        "candidate_name": candidate.name,
                                        "turn": turn,
                                        "tool_name": str(block.name),
                                        "path": mutation_path,
                                        "error": diagnostics_result.error[:300],
                                    },
                                )
                            if (
                                diagnostics_result.new_diagnostics
                                or diagnostics_result.cleared_paths
                            ):
                                _append_build_progress_event(sandbox_dir, {
                                    "event": "code_diagnostics_updated",
                                    "candidate_name": candidate.name,
                                    "turn": turn,
                                    "source_tool": str(block.name),
                                    "checked_paths": diagnostics_result.checked_paths,
                                    "new_count": len(diagnostics_result.new_diagnostics),
                                    "active_count": len(diagnostics_result.active_diagnostics),
                                    "cleared_paths": diagnostics_result.cleared_paths,
                                })
                        except Exception as exc:  # noqa: BLE001 - diagnostics fail open
                            logger.warning(
                                "Python diagnostics failed open",
                                extra={
                                    "operation": "code_diagnostics_failed_open",
                                    "trace_id": trace_id,
                                    "candidate_name": candidate.name,
                                    "turn": turn,
                                    "tool_name": str(block.name),
                                    "path": mutation_path,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc)[:300],
                                },
                            )
                    # Detect the build-gate transition. The only supported
                    # transition is a successful implementation_plan.json
                    # write/patch.
                    transition_triggered, trigger = _detect_implementation_plan_transition(
                        block,
                        implementation_plan_accepted=implementation_plan_accepted,
                        exit_code=exit_code,
                    )
                    if transition_triggered:
                        implementation_plan_accepted = True
                        build_gate_accepted = True
                        try:
                            from puzzleeval.agents.agent5.build_decisions import append_build_decision

                            append_build_decision(
                                sandbox_dir,
                                event="implementation_plan_accepted",
                                turn=turn,
                                candidate_name=candidate.name,
                                trigger=trigger,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        logger.info(f"Build gate accepted for {candidate.name}; build actions are now allowed (trigger: {trigger})", extra={
                            "operation": "phase_transition",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "trigger_file": trigger,
                        })
                        # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                        # Build-gate research-output compaction
                        # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                        # Bound bulky research tool_result blobs after the
                        # implementation plan accepts. Durable findings and
                        # synthesis stay on disk; the lead model reads them
                        # via read_file/summarize_build_state when needed.
                        try:
                            compacted = _compact_research_tool_results(
                                messages,
                            )
                            if compacted:
                                invalidate_read_dedup_state(build_read_state)
                                logger.info(
                                    f"Compacted {compacted} research tool_result block(s) "
                                    f"at build gate for {candidate.name}",
                                    extra={
                                        "operation": "research_compaction",
                                        "trace_id": trace_id,
                                        "candidate_name": candidate.name,
                                        "turn": turn,
                                        "blocks_compacted": compacted,
                                    },
                                )
                        except Exception as exc:  # noqa: BLE001
                            # Compaction failure is non-fatal â€” at
                            # worst we pay the original tax. Don't
                            # let a malformed message structure
                            # crash the build.
                            logger.warning(
                                f"Research compaction failed for {candidate.name}: "
                                f"{type(exc).__name__}: {str(exc)[:200]}",
                                extra={
                                    "operation": "research_compaction_failed",
                                    "trace_id": trace_id,
                                    "candidate_name": candidate.name,
                                },
                            )
                    # Set is_error flag per Anthropic docs â€” tells Claude the result
                    # is an error, triggering smarter retry/correction behavior.
                    # Without this, Claude treats "Error: file not found" the same
                    # as "Written 500 chars to harness.py".
                    # Use structured exit code (like Claude Code) â€” no string parsing
                    has_tool_error = exit_code != 0
                    tool_result_entry = {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                    if has_tool_error:
                        tool_result_entry["is_error"] = True
                    tool_results.append(tool_result_entry)
                    # [observability] Log tool result with rich diagnostic
                    # context: exit_code (was dropped on the floor), full
                    # 2000-char tail (so tracebacks fit â€” 500 chars cut
                    # error messages mid-line), and per-tool metadata
                    # (write_file content size, patch_file diff hint).
                    # Without this, debugging "why did smoke_test.py fail?"
                    # required re-reading sandbox files manually because
                    # the truncated 500-char tool result hid the actual
                    # Python traceback.
                    if conversation_log:
                        result_entry = {
                            "tool": block.name,
                            "exit_code": exit_code,
                            "is_error": has_tool_error,
                            "result": result_text[-2000:],  # Tail, not head â€” errors at end
                            "result_length": len(result_text),
                            "tool_elapsed_ms": tool_elapsed_ms,
                        }
                        if tool_metadata:
                            result_entry.update(tool_metadata)
                        # Per-tool diagnostic enrichment
                        if block.name == "write_file":
                            content = block.input.get("content", "") if isinstance(block.input, dict) else ""
                            attempted_path = block.input.get("filename", block.input.get("path", "")) if isinstance(block.input, dict) else ""
                            result_entry["attempted_path"] = attempted_path
                            result_entry["attempted_chars"] = len(content)
                            result_entry["attempted_preview"] = content[:200]
                            result_entry["attempted_write"] = True
                            result_entry["persisted"] = not has_tool_error
                            if not has_tool_error:
                                result_entry["wrote_path"] = attempted_path
                                result_entry["wrote_chars"] = len(content)
                                result_entry["wrote_preview"] = content[:200]
                        elif block.name == "patch_file":
                            old_str = block.input.get("old_string", "") if isinstance(block.input, dict) else ""
                            new_str = block.input.get("new_string", "") if isinstance(block.input, dict) else ""
                            attempted_path = block.input.get("filename", block.input.get("path", "")) if isinstance(block.input, dict) else ""
                            result_entry["attempted_path"] = attempted_path
                            result_entry["attempted_patch"] = True
                            result_entry["persisted"] = not has_tool_error
                            if not has_tool_error:
                                result_entry["patch_path"] = attempted_path
                            result_entry["patch_diff_chars"] = len(new_str) - len(old_str)
                            result_entry["patch_old_preview"] = old_str[:120]
                            result_entry["patch_new_preview"] = new_str[:120]
                        elif block.name == "run_code":
                            cmd = block.input.get("command") or block.input.get("code") or "" if isinstance(block.input, dict) else ""
                            result_entry["command"] = str(cmd)[:300]
                        conversation_log[-1]["tool_results"].append(result_entry)
                    if (
                        exit_code == 0
                        and _cfg.ABANDON_CANDIDATE_ENABLED
                        and _tool_targets_relative_path(
                            block,
                            "_agent_state/abandon_candidate.json",
                        )
                    ):
                        try:
                            from puzzleeval.agents.agent5.abandon_candidate import (
                                failed_harness_category_for_reason,
                                read_abandon_candidate,
                            )

                            abandon = read_abandon_candidate(sandbox_dir)
                        except Exception:  # noqa: BLE001 - invalid artifact just continues loop
                            abandon = None
                        if abandon:
                            reason = str(abandon.get("reason") or "unknown")
                            summary = str(
                                abandon.get("summary")
                                or abandon.get("explanation")
                                or ""
                            ).strip()
                            failure_reason = (
                                f"Abandoned: {reason}. {summary}"
                            )[:1000]
                            logger.warning(
                                "Agent 5 abandoned candidate %s: %s",
                                candidate.name,
                                reason,
                                extra={
                                    "operation": "candidate_abandoned",
                                    "trace_id": trace_id,
                                    "candidate_name": candidate.name,
                                    "turn": turn,
                                    "reason": reason,
                                },
                            )
                            _flush_observability_snapshot(
                                current_model=_current_loop_model(),
                                final_completion_status="abandoned",
                                final_completion_issues=[failure_reason],
                            )
                            return FailedHarness(
                                candidate_name=candidate.name,
                                provider=candidate.provider,
                                failure_reason=failure_reason,
                                failure_category=failed_harness_category_for_reason(reason),
                                partial_code=_read_harness_code(sandbox_dir),
                                turns_attempted=turn + 1,
                                web_fetch_blocks=candidate_web_fetch_blocks,
                                build_cost_usd=round(accumulated_cost, 4),
                                harness_dir=str(sandbox_dir),
                                completion_gate_status="abandoned",
                                completion_gate_issues=[failure_reason],
                            )
                    if has_tool_error:
                        if (
                            block.name == "write_file"
                            and _tool_targets_relative_path(
                                block,
                                "_agent_state/research_synthesis.json",
                            )
                            and "research_synthesis.json failed" in result_text
                        ):
                            research_synthesis_rejected_this_turn = True
                        is_implementation_plan_validation_error = (
                            _is_implementation_plan_write_attempt(block)
                            and (
                                "implementation-plan validation" in result_text
                                or "implementation_plan.json failed" in result_text
                            )
                        )
                        if (
                            is_implementation_plan_validation_error
                        ):
                            implementation_plan_validation_failures += 1
                            if (
                                implementation_plan_validation_failures
                                > _cfg.IMPLEMENTATION_PLAN_MAX_REVISIONS
                            ):
                                failure_reason = (
                                    "implementation_plan.json failed validation "
                                    f"{implementation_plan_validation_failures} time(s), "
                                    f"exceeding the revision budget of "
                                    f"{_cfg.IMPLEMENTATION_PLAN_MAX_REVISIONS}. "
                                    f"Last error: {result_text[:500]}"
                                )
                                logger.warning(
                                    "Implementation plan revision budget exhausted for %s",
                                    candidate.name,
                                    extra={
                                        "operation": "implementation_plan_invalid",
                                        "trace_id": trace_id,
                                        "candidate_name": candidate.name,
                                        "turn": turn,
                                        "revision_failures": implementation_plan_validation_failures,
                                        "max_revisions": _cfg.IMPLEMENTATION_PLAN_MAX_REVISIONS,
                                    },
                                )
                                _flush_observability_snapshot(
                                    current_model=_current_loop_model(),
                                    final_completion_status="implementation_plan_invalid",
                                    final_completion_issues=[failure_reason],
                                )
                                return FailedHarness(
                                    candidate_name=candidate.name,
                                    provider=candidate.provider,
                                    failure_reason=failure_reason,
                                    failure_category="implementation_plan_invalid",
                                    partial_code=_read_harness_code(sandbox_dir),
                                    turns_attempted=turn + 1,
                                    web_fetch_blocks=candidate_web_fetch_blocks,
                                    build_cost_usd=round(accumulated_cost, 4),
                                    harness_dir=str(sandbox_dir),
                                    completion_gate_status="implementation_plan_invalid",
                                    completion_gate_issues=[failure_reason],
                                )
                        command_text = ""
                        if block.name == "run_code" and isinstance(block.input, dict):
                            command_text = str(
                                block.input.get("command")
                                or block.input.get("code")
                                or ""
                            )
                        failure_source = (
                            "run_code" if block.name == "run_code"
                            else f"tool:{block.name}"
                        )
                        if (
                            research_synthesis_rejected_this_turn
                            and _is_implementation_plan_write_attempt(block)
                            and "cannot accept implementation_plan.json before" in result_text
                        ):
                            # Dependent artifact write in the same parallel
                            # batch. The actionable packet belongs to the
                            # rejected synthesis artifact; do not make the
                            # builder debug the consequence as a new root cause.
                            continue
                        try:
                            from puzzleeval.agents.agent5.build_decisions import append_build_decision
                            from puzzleeval.agents.agent5.failure_packets import (
                                is_meaningful_failure_source,
                                write_failure_packet,
                            )
                            if not (
                                _cfg.FAILURE_PACKET_DEBUG_ENABLED
                                and is_meaningful_failure_source(
                                    tool_name=block.name,
                                    exit_code=exit_code,
                                    result_text=result_text,
                                    command=command_text[:500],
                                    failure_source=failure_source,
                                )
                            ):
                                continue
                            packet = write_failure_packet(
                                sandbox_dir,
                                turn=turn,
                                candidate_name=candidate.name,
                                failure_source=failure_source,
                                command=command_text[:500],
                                exit_code=exit_code,
                                result_text=result_text,
                                conversation_log=conversation_log,
                                diagnostic_reviewer=_failure_packet_reviewer,
                            )
                            append_build_decision(
                                sandbox_dir,
                                event="failure_packet_created",
                                turn=turn,
                                candidate_name=candidate.name,
                                failure_source=failure_source,
                                mechanical_tags=packet.get("mechanical_tags"),
                                diagnosis=(packet.get("diagnosis") or {}).get("observed_failure")
                                if isinstance(packet.get("diagnosis"), dict)
                                else None,
                                packet_path=packet.get("path"),
                            )
                            _append_build_progress_event(sandbox_dir, {
                                "event": "failure_packet_created",
                                "candidate_name": candidate.name,
                                "turn": turn,
                                "failure_source": failure_source,
                                "mechanical_tags": packet.get("mechanical_tags"),
                                "diagnosis": (packet.get("diagnosis") or {}).get("observed_failure")
                                if isinstance(packet.get("diagnosis"), dict)
                                else None,
                                "packet_path": packet.get("path"),
                            })
                            diagnosis = packet.get("diagnosis") if isinstance(packet.get("diagnosis"), dict) else {}
                            packet_note = (
                                "\n\nStructured failure packet written: "
                                f"{packet.get('path', '_agent_state/latest_failure_packet.json')} "
                                f"(tags={', '.join(packet.get('mechanical_tags') or ['untagged'])}; "
                                f"observed={diagnosis.get('observed_failure', 'see packet')}; "
                                f"next={diagnosis.get('next_diagnostic_or_patch', 'inspect evidence')}). "
                                "Call summarize_build_state() before patching if you need "
                                "the structured failure context."
                            )
                            tool_result_entry["content"] = str(
                                tool_result_entry.get("content") or ""
                            ) + packet_note
                        except Exception as exc:  # noqa: BLE001 - diagnostics must not break builds
                            logger.warning(
                                "failure packet creation failed",
                                extra={
                                    "operation": "failure_packet_create_failed",
                                    "trace_id": trace_id,
                                    "candidate_name": candidate.name,
                                    "turn": turn,
                                    "tool_name": block.name,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc)[:300],
                                },
                            )
                    # (live_test injection removed â€” agent validates with real test data)

            # â”€â”€ Detect smoke test passing in tool results â”€â”€
            # CRITICAL: Track across ALL turns (not just last_text).
            expected_tool_results = [
                (block.id, block.name)
                for block in response.content
                if getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) in CUSTOM_TOOL_NAMES
            ]
            returned_tool_ids = {
                r.get("tool_use_id")
                for r in tool_results
                if isinstance(r, dict) and r.get("type") == "tool_result"
            }
            for missing_id, missing_name in expected_tool_results:
                if missing_id in returned_tool_ids:
                    continue
                fallback = (
                    f"{missing_name} produced no tool_result. PuzzleEval "
                    "inserted this error placeholder so the builder can "
                    "recover instead of sending an invalid Anthropic message."
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": missing_id,
                    "content": fallback,
                    "is_error": True,
                })
                logger.warning(
                    "custom tool produced no tool_result",
                    extra={
                        "operation": "agent5_missing_tool_result_filled",
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "tool_name": missing_name,
                        "turn": turn,
                    },
                )
                if conversation_log:
                    conversation_log[-1]["tool_results"].append({
                        "tool": missing_name,
                        "exit_code": -1,
                        "is_error": True,
                        "result": fallback,
                        "result_length": len(fallback),
                    })

            all_results_text_raw = " ".join(
                r.get("content", "") for r in tool_results if isinstance(r.get("content"), str)
            )
            if conversation_log:
                for _tr in conversation_log[-1].get("tool_results", []):
                    if not isinstance(_tr, dict) or _tr.get("tool") != "run_code":
                        continue
                    _cmd = str(_tr.get("command") or "")
                    _result = str(_tr.get("result") or "")
                    if "live_test.py" in _cmd:
                        last_live_test_output = _result[-2000:]
                        _live_exit_code = int(_tr.get("exit_code", 1) or 0)
                        if _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED:
                            try:
                                class _LiveState:
                                    pass
                                _ls = _LiveState()
                                _ls.turn = turn
                                _ls.accumulated_cost = accumulated_cost
                                _ls.build_gate_accepted = build_gate_accepted
                                _ls.implementation_plan_accepted = implementation_plan_accepted
                                _ls.verification_attempts = verification_attempts
                                _ls.verification_passed = verification_passed
                                _ls.smoke_ever_passed = smoke_ever_passed
                                _ls.smoke_passed_at_turn = smoke_passed_at_turn
                                _ls.consecutive_errors = consecutive_errors
                                _ls.total_reassessments = total_reassessments
                                update_runtime_state(
                                    sandbox_dir=sandbox_dir,
                                    state=_ls,
                                    current_model=_current_loop_model(),
                                    live_test_status=("passing" if _live_exit_code == 0 else "failing"),
                                    live_passed_at_turn=turn if _live_exit_code == 0 else (
                                        live_passed_at_turn if live_ever_passed else None
                                    ),
                                    last_live_test_output=last_live_test_output or None,
                                    completion_gate_status=completion_gate_status,
                                )
                            except OSError:
                                pass
                        if _live_exit_code == 0 and not live_ever_passed:
                            live_ever_passed = True
                            live_passed_at_turn = turn
                            try:
                                from puzzleeval.agents.agent5.build_decisions import append_build_decision

                                append_build_decision(
                                    sandbox_dir,
                                    event="live_test_passed",
                                    turn=turn,
                                    candidate_name=candidate.name,
                                    command=_cmd[:300],
                                )
                            except Exception:  # noqa: BLE001
                                pass
                            _append_build_progress_event(sandbox_dir, {
                                "event": "live_test_passed",
                                "candidate_name": candidate.name,
                                "turn": turn,
                                "command": _cmd[:300],
                            })
            if detect_smoke_pass(all_results_text_raw) and not smoke_ever_passed:
                smoke_ever_passed = True
                smoke_passed_at_turn = turn
                try:
                    from puzzleeval.agents.agent5.build_decisions import append_build_decision

                    append_build_decision(
                        sandbox_dir,
                        event="smoke_test_passed",
                        turn=turn,
                        candidate_name=candidate.name,
                    )
                except Exception:  # noqa: BLE001
                    pass
                logger.info(f"Offline smoke check passed for {candidate.name} at turn {turn}", extra={
                    "operation": "smoke_test_passed",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turn": turn,
                })
                if _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED:
                    try:
                        class _SmokeState:
                            pass
                        _ss = _SmokeState()
                        _ss.turn = turn
                        _ss.accumulated_cost = accumulated_cost
                        _ss.build_gate_accepted = build_gate_accepted
                        _ss.implementation_plan_accepted = implementation_plan_accepted
                        _ss.verification_attempts = verification_attempts
                        _ss.verification_passed = verification_passed
                        _ss.smoke_ever_passed = smoke_ever_passed
                        _ss.smoke_passed_at_turn = smoke_passed_at_turn
                        _ss.consecutive_errors = consecutive_errors
                        _ss.total_reassessments = total_reassessments
                        update_runtime_state(
                            sandbox_dir=sandbox_dir,
                            state=_ss,
                            current_model=_current_loop_model(),
                            smoke_test_status="passing",
                            completion_gate_status=completion_gate_status,
                        )
                    except OSError:
                        pass

            # â”€â”€ Detect HARNESS_COMPLETE in tool results â”€â”€
            # Route through the unified verification gate by asking the
            # agent to re-emit HARNESS_COMPLETE in assistant text. We MUST
            # append the assistant response + tool_results FIRST so the
            # next API call sees a consistent message history (every
            # tool_use block in the assistant turn must be paired with a
            # tool_result in the following user turn â€” Anthropic API
            # invariant).
            if detect_harness_signal(all_results_text_raw) == "complete":
                logger.info(
                    "HARNESS_COMPLETE appeared in tool output for %s; requesting assistant-text completion",
                    candidate.name,
                    extra={
                        "operation": "tool_result_completion_signal_observed",
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "turn": turn,
                    },
                )
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                messages.append({
                    "role": "user",
                    "content": (
                        "A tool result contained HARNESS_COMPLETE. If the harness "
                        "is ready, signal HARNESS_COMPLETE in your next assistant "
                        "message. The unified verification gate will then run "
                        "structural, forensics, and reflection checks."
                    ),
                })
                turn += 1
                continue

            # â”€â”€ Track consecutive errors for dead-end detection â”€â”€
            # Phase 4 Path B Step 2: error detection + classification
            # extracted to dispatch_helpers (pure predicates over text).
            # ``all_results_text`` (lowered) preserved as a local â€” it's
            # used downstream by the reassessment-message builder.
            from puzzleeval.agents.agent5.dispatch_helpers import (
                classify_tool_result_error,
                detect_tool_result_error,
            )
            all_results_text = all_results_text_raw.lower()
            has_error = detect_tool_result_error(all_results_text_raw)

            if has_error:
                consecutive_errors += 1
                error_history.append((turn, classify_tool_result_error(all_results_text_raw)))
            else:
                consecutive_errors = 0

            # â”€â”€ Phase 1 + 1.5 hardening: detect useless web_fetch results â”€â”€
            # Two failure surfaces share one recovery path:
            #   Phase 1   â€” HTTP-level errors (403/Cloudflare, 429, 5xx)
            #   Phase 1.5 â€” content-level uselessness (SPA shells, auth walls,
            #               soft 404s, marketing pages with no API signals)
            # Anthropic's web_fetch can't customize user-agent and can't run JS,
            # so the recovery for both is the same: PIVOT to web_search snippets,
            # GitHub SDK repos, alternate URLs, or archive.org. We append unified
            # guidance to the tool_results so the model sees it next turn.
            # See puzzleeval/web_fetch_fallback.py for both classifiers.
            if ENABLE_FETCH_FALLBACK:
                blocked = extract_blocked_fetches(response)
                unusable = extract_unusable_pages(response)
                actionable = count_actionable_problems(blocked, unusable)
                if actionable:
                    candidate_web_fetch_blocks += actionable
                    logger.info(
                        f"Web fetch problems for {candidate.name} at turn {turn}",
                        extra={
                            "operation": "agent5_fetch_blocks",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            **summarize_blocks_for_log(blocked, unusable),
                        },
                    )
                    # Backoff for 429s before next turn (no-op when no rate limit).
                    maybe_apply_rate_limit_backoff(blocked)
                    # Append unified guidance as a text block alongside the
                    # tool_results so the model sees it on its next turn.
                    guidance = build_fallback_message(blocked, unusable)
                    if guidance:
                        tool_results.append({"type": "text", "text": guidance})

            # Append assistant response + tool results to conversation
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            # Same-turn scaffold behavior: after implementation_plan.json is
            # accepted, the independent scaffold files are usually known
            # together. If the builder starts writing them one file at a time,
            # nudge once toward batching instead of spending four Opus turns.
            if implementation_plan_accepted and not scaffold_batch_nudge_sent:
                scaffold_files = {"requirements.txt", "harness.py", "smoke_test.py", "live_test.py"}
                written_this_turn = {
                    str(getattr(block, "input", {}).get("filename", "") or "")
                    for block in response.content
                    if getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "write_file"
                }
                scaffold_written = {Path(name).name for name in written_this_turn} & scaffold_files
                missing_scaffold = sorted(
                    name for name in scaffold_files if not (sandbox_dir / name).exists()
                )
                if scaffold_written and missing_scaffold:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Scaffold batching nudge: you wrote "
                            f"{', '.join(sorted(scaffold_written))}, but "
                            f"{', '.join(missing_scaffold)} are still missing. "
                            "If their content is already determined by the accepted "
                            "implementation_plan.json, "
                            "emit the remaining write_file calls in one same-turn batch. "
                            "Sequence only when a later file truly depends on observed test output."
                        ),
                    })
                    scaffold_batch_nudge_sent = True
                    try:
                        from puzzleeval.agents.agent5.build_decisions import append_build_decision

                        append_build_decision(
                            sandbox_dir,
                            event="scaffold_batching_nudge",
                            turn=turn,
                            candidate_name=candidate.name,
                            written_this_turn=sorted(scaffold_written),
                            missing_scaffold=missing_scaffold,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    _append_build_progress_event(sandbox_dir, {
                        "event": "scaffold_batching_nudge",
                        "candidate_name": candidate.name,
                        "turn": turn,
                        "written_this_turn": sorted(scaffold_written),
                        "missing_scaffold": missing_scaffold,
                    })
                    logger.info(
                        "Scaffold batching nudge sent for %s at turn %d",
                        candidate.name, turn,
                        extra={
                            "operation": "scaffold_batching_nudge",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "written_this_turn": sorted(scaffold_written),
                            "missing_scaffold": missing_scaffold,
                        },
                    )

            # â”€â”€ PR 1: build_plan init directive at turn-0 â†’ turn-1 boundary â”€â”€
            # Disabled by default. If explicitly enabled for experiments, it
            # asks the agent to write _agent_state/build_plan.md as the first
            # tool call of turn 1. Production behavior stages a passive seed
            # and avoids plan-maintenance turn tax.
            if (
                _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED
                and _cfg.AUTONOMY_BUILD_PLAN_DIRECTIVES_ENABLED
                and turn == 0
                and not build_gate_accepted
                and not build_plan_init_directive_sent
            ):
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": BUILD_PLAN_INIT_DIRECTIVE}],
                })
                build_plan_init_directive_sent = True
                logger.info(
                    "Build_plan init directive fired for %s at turn 0 boundary",
                    candidate.name,
                    extra={
                        "operation": EVENT_BUILD_PLAN_INIT_DIRECTIVE_FIRED,
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "turn": turn,
                    },
                )
                try:
                    class _BuildPlanDirectiveState:
                        pass
                    _bpds = _BuildPlanDirectiveState()
                    _bpds.turn = turn
                    _bpds.accumulated_cost = accumulated_cost
                    _bpds.build_gate_accepted = build_gate_accepted
                    _bpds.implementation_plan_accepted = implementation_plan_accepted
                    _bpds.verification_attempts = verification_attempts
                    _bpds.verification_passed = verification_passed
                    _bpds.smoke_ever_passed = smoke_ever_passed
                    _bpds.smoke_passed_at_turn = smoke_passed_at_turn
                    _bpds.consecutive_errors = consecutive_errors
                    _bpds.total_reassessments = total_reassessments
                    update_runtime_state(
                        sandbox_dir=sandbox_dir,
                        state=_bpds,
                        current_model=_current_loop_model(),
                        completion_gate_status=completion_gate_status,
                        directive_fired={
                            "directive": "BUILD_PLAN_INIT_DIRECTIVE",
                            "reason": "turn_0_boundary",
                        },
                    )
                except OSError:
                    pass

            # â”€â”€ Build-gate forcing directive (or artifact compaction) â”€â”€
            # When build_gate_accepted JUST flipped True this turn, the
            # implementation plan has been accepted and scaffold/code writes
            # become legal. Context compaction is preferred because it grounds
            # the next turn in durable artifacts; otherwise a concise directive
            # tells the builder to start the vertical slice immediately.
            if build_gate_accepted and not build_gate_was_accepted:
                # PR 3 â€” agreement check. Suppress when the agent's most
                # recent phase observation matches phase_2_build.
                _agreement_verdict = "no_observation"
                _agent_phase_note: str | None = None
                if (
                    _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED
                    and _cfg.DIRECTIVE_SUPPRESS_ON_AGREEMENT_ENABLED
                ):
                    from puzzleeval.agents.agent5.runtime_state import (
                        evaluate_agent_phase_agreement,
                    )
                    _agreement_verdict, _agent_phase_note = evaluate_agent_phase_agreement(
                        sandbox_dir, orchestrator_phase="phase_2_build",
                    )

                # PR 3 telemetry: log the verdict before any action, so
                # operators can see the agreement/disagreement rate over
                # a release cycle (the load-bearing data point for
                # promoting suppression from soft-default to hard-default).
                if _agreement_verdict == "disagreed":
                    logger.warning(
                        "Agent phase disagreement at build gate "
                        "for %s â€” orchestrator says phase_2_build, agent says: %r",
                        candidate.name, (_agent_phase_note or "")[:160],
                        extra={
                            "operation": "agent_observation_phase_disagreement",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "orchestrator_phase": "phase_2_build",
                            "agent_phase_note": (_agent_phase_note or "")[:160],
                        },
                    )
                elif _agreement_verdict == "no_observation":
                    logger.info(
                        "No agent phase observation for %s at transition â€” "
                        "using transition backstop (defensive default)",
                        candidate.name,
                        extra={
                            "operation": "transition_backstop_no_agent_observation",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                        },
                    )

                if _agreement_verdict == "agreed":
                    logger.info(
                        "Directive suppressed at build gate "
                        "for %s â€” agent observed phase_2_build",
                        candidate.name,
                        extra={
                            "operation": "directive_suppressed_agent_observed",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "agent_phase_note": (_agent_phase_note or "")[:160],
                        },
                    )
                if _cfg.CONTEXT_COMPACTION_AT_BUILD_GATE_ENABLED:
                    # The build gate accepted after the normal top-of-loop
                    # runtime_state refresh. Refresh immediately before
                    # composing the canonical packet so it reflects the build gate.
                    try:
                        class _TransitionState:
                            pass
                        _ts = _TransitionState()
                        _ts.turn = turn
                        _ts.accumulated_cost = accumulated_cost
                        _ts.build_gate_accepted = True
                        _ts.implementation_plan_accepted = implementation_plan_accepted
                        _ts.verification_attempts = verification_attempts
                        _ts.verification_passed = verification_passed
                        _ts.smoke_ever_passed = smoke_ever_passed
                        _ts.smoke_passed_at_turn = smoke_passed_at_turn
                        _ts.consecutive_errors = consecutive_errors
                        _ts.total_reassessments = total_reassessments
                        update_runtime_state(
                            sandbox_dir=sandbox_dir,
                            state=_ts,
                            current_model=AGENT5_BUILDER_MODEL,
                            completion_gate_status=completion_gate_status,
                        )
                    except OSError:
                        pass
                    # Capture the pre-compaction message count for logging,
                    # then replace messages with the compacted single user
                    # message. The next API call's payload is now bounded:
                    # the system prompt + ONE user message + tool result the
                    # next turn produces.
                    _pre_compaction_count = len(messages)
                    messages.clear()
                    messages.extend(compact_for_build_gate(sandbox_dir))
                    invalidate_read_dedup_state(build_read_state)
                    logger.info(
                        "Context compacted at build gate for %s "
                        "(was %d messages, now 1)",
                        candidate.name, _pre_compaction_count,
                        extra={
                            "operation": COMPACTION_EVENT_NAME,
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "pre_compaction_message_count": _pre_compaction_count,
                        },
                    )
                    try:
                        update_runtime_state(
                            sandbox_dir=sandbox_dir,
                            state=_ts,
                            current_model=AGENT5_BUILDER_MODEL,
                            completion_gate_status=completion_gate_status,
                            context_compaction_event={
                                "operation": COMPACTION_EVENT_NAME,
                                "pre_message_count": _pre_compaction_count,
                                "post_message_count": len(messages),
                                "reason": (
                                    "implementation_plan_accepted_build_gate"
                                ),
                            },
                        )
                    except OSError:
                        pass
                elif _agreement_verdict != "agreed":
                    directive_text = IMPLEMENTATION_PLAN_DIRECTIVE
                    messages.append({
                        "role": "user",
                        "content": [{"type": "text", "text": directive_text}],
                    })
                    try:
                        class _DirectiveState:
                            pass
                        _ds = _DirectiveState()
                        _ds.turn = turn
                        _ds.accumulated_cost = accumulated_cost
                        _ds.build_gate_accepted = True
                        _ds.implementation_plan_accepted = implementation_plan_accepted
                        _ds.verification_attempts = verification_attempts
                        _ds.verification_passed = verification_passed
                        _ds.smoke_ever_passed = smoke_ever_passed
                        _ds.smoke_passed_at_turn = smoke_passed_at_turn
                        _ds.consecutive_errors = consecutive_errors
                        _ds.total_reassessments = total_reassessments
                        update_runtime_state(
                            sandbox_dir=sandbox_dir,
                            state=_ds,
                            current_model=AGENT5_BUILDER_MODEL,
                            completion_gate_status=completion_gate_status,
                            directive_fired={
                                "directive": "IMPLEMENTATION_PLAN_DIRECTIVE",
                                "reason": "implementation_plan_accepted",
                            },
                        )
                    except OSError:
                        pass

            _smoke_just_passed = (
                smoke_ever_passed and smoke_passed_at_turn == turn
            )

            # â”€â”€ PR 1 (deferred wiring): build_plan staleness at trigger â”€â”€
            # When a meaningful state-change trigger fires this turn
            # (implementation plan accepted, scaffold writes landed, smoke passed),
            # we check whether the agent updated _agent_state/build_plan.md.
            # Soft-tier â€” telemetry per trigger + ONE nudge per turn when
            # stale. The plan stays "operational rather than ornamental"
            # because the orchestrator pings the agent at exactly the moments
            # a real plan would be revised.
            if (
                _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED
                and _cfg.AUTONOMY_BUILD_PLAN_DIRECTIVES_ENABLED
            ):
                from puzzleeval.agents.agent5.dispatch_helpers import (
                    detect_build_plan_triggers,
                )
                _bp_triggers = detect_build_plan_triggers(
                    response.content,
                    build_gate_was_accepted=build_gate_was_accepted,
                    build_gate_now_accepted=build_gate_accepted,
                    smoke_passed_this_turn=_smoke_just_passed,
                    harness_complete_signaled=False,  # text-based path covers this
                )
                if _bp_triggers:
                    _bp_path = sandbox_dir / "_agent_state" / "build_plan.md"
                    _bp_mtime = 0.0
                    if _bp_path.exists():
                        try:
                            _bp_mtime = _bp_path.stat().st_mtime
                        except OSError:
                            pass
                    _bp_stale = _bp_mtime <= last_build_plan_mtime
                    for _trigger_label in _bp_triggers:
                        logger.info(
                            "Build plan %s at trigger for %s: %s",
                            "stale" if _bp_stale else "updated",
                            candidate.name, _trigger_label,
                            extra={
                                "operation": (
                                    EVENT_BUILD_PLAN_STALE_AT_TRIGGER
                                    if _bp_stale
                                    else "autonomy_build_plan_updated_at_trigger"
                                ),
                                "trace_id": trace_id,
                                "candidate_name": candidate.name,
                                "turn": turn,
                                "trigger": _trigger_label,
                            },
                        )
                    # Inject one nudge per turn naming the most recent
                    # trigger. Suppress when the build gate transitioned
                    # this turn because the directive or compaction already
                    # gave the agent build-shaped direction.
                    _build_gate_just_transitioned = (
                        build_gate_accepted and not build_gate_was_accepted
                    )
                    if _bp_stale and not _build_gate_just_transitioned:
                        messages.append({
                            "role": "user",
                            "content": [{
                                "type": "text",
                                "text": BUILD_PLAN_STALENESS_NUDGE.format(
                                    trigger=_bp_triggers[-1],
                                ),
                            }],
                        })
                    last_build_plan_mtime = _bp_mtime

            # â”€â”€ Gate B4: pre-plan research budget â”€â”€
            # AD-007 soft enforcement. Counts turns (not calls) where the
            # builder used web_search/web_fetch/ask_research while still
            # in the research phase. After exceeding budget, inject a single
            # user message asking the builder to commit research synthesis and
            # implementation_plan.json. The builder adapts (writes a plan with
            # non-blocking risks)
            # OR calls advisor for a tier-up â€” never halts the build.
            from puzzleeval import config as _cfg
            from puzzleeval.agents.agent5.dispatch_helpers import (
                turn_used_prebuild_research,
            )
            if (
                _cfg.GATE_PREBUILD_RESEARCH_BUDGET_ENABLED
                and not build_gate_accepted
                and not prebuild_budget_message_injected
            ):
                if turn_used_prebuild_research(
                    response.content,
                    response.usage,
                    build_gate_artifact_exists=(
                        sandbox_dir / "_agent_state" / "implementation_plan.json"
                    ).exists(),
                ):
                    prebuild_research_turns += 1
                budget = max(1, int(_cfg.GATE_PREBUILD_RESEARCH_BUDGET))
                if prebuild_research_turns >= budget:
                    budget_msg = (
                        f"Pre-build research budget reached ({prebuild_research_turns} "
                        f"of {budget} research turns used before the build gate). "
                        f"Stop broad research and commit: write or patch "
                        f"_agent_state/research_synthesis.json and "
                        f"_agent_state/implementation_plan.json with cited facts, "
                        f"explicit assumptions, and non-blocking risks. If a "
                        f"required implementation-plan field is still missing, "
                        f"make one targeted fetch/ask_research call or abandon "
                        f"with evidence rather than looping."
                    )
                    messages.append({
                        "role": "user",
                        "content": [{"type": "text", "text": budget_msg}],
                    })
                    prebuild_budget_message_injected = True
                    logger.warning(
                        "gate_fired",
                        extra={
                            "operation": "gate_fired",
                            "gate_name": "prebuild_research_budget",
                            "severity": "INJECT_USER_MESSAGE",
                            "rejected": False,
                            "candidate_slug": candidate.name,
                            "trace_id": trace_id,
                            "prebuild_research_turns": prebuild_research_turns,
                            "budget": budget,
                        },
                    )

            # â”€â”€ Adaptive progress tracking â”€â”€
            # Claude Code's diminishing-return pattern measures whether a
            # continuation is still buying useful state. PuzzleEval uses
            # durable artifact/evidence signals rather than mere tool activity.
            turn_tool_results = (
                conversation_log[-1].get("tool_results") or []
                if conversation_log else []
            )
            turn_made_progress, progress_signals, low_value_signals = (
                _classify_durable_turn_progress(
                    response_content=list(response.content),
                    tool_result_logs=[
                        tr for tr in turn_tool_results
                        if isinstance(tr, dict)
                    ],
                    build_gate_transitioned=(
                        build_gate_accepted and not build_gate_was_accepted
                    ),
                    smoke_just_passed=_smoke_just_passed,
                    live_just_passed=(
                        live_ever_passed and live_passed_at_turn == turn
                    ),
                    seen_failure_evidence_hashes=seen_failure_evidence_hashes,
                )
            )
            _append_build_progress_event(sandbox_dir, {
                "event": "turn_progress_ledger",
                "progress_version": 2,
                "candidate_name": candidate.name,
                "turn": turn,
                "phase": _build_phase_name(
                    build_gate_accepted=build_gate_accepted,
                    smoke_ever_passed=smoke_ever_passed,
                ),
                "made_progress": turn_made_progress,
                "signals": progress_signals,
                "low_value_signals": low_value_signals,
            })
            progress_ring.append(turn_made_progress)
            if len(progress_ring) > AGENT5_DIMINISHING_RETURNS_WINDOW:
                progress_ring.pop(0)
            # If N consecutive no-progress turns, inject one state-grounded
            # nudge. This is SOFT: no rejection, no forced branch.
            if (
                len(progress_ring) >= AGENT5_DIMINISHING_RETURNS_WINDOW
                and not any(progress_ring)
                and not diminishing_nudge_sent
            ):
                messages.append({
                    "role": "user",
                    "content": (
                        "## Progress nudge\n\n"
                        f"The last {AGENT5_DIMINISHING_RETURNS_WINDOW} turns produced no durable "
                        "artifact, new research finding, test/probe evidence, or failure evidence. "
                        "Current phase: "
                        f"{_build_phase_name(build_gate_accepted=build_gate_accepted, smoke_ever_passed=smoke_ever_passed)}. "
                        "Use the accepted artifacts and latest evidence to choose one productive next action: "
                        "synthesize, write/repair the implementation plan, scaffold, run a test/probe, patch, "
                        "ask one scoped research gap, or abandon with evidence. If continued reading/research is "
                        "truly necessary, name the missing fact and why it changes implementation.\n"
                    ),
                })
                diminishing_nudge_sent = True
                logger.info(
                    f"Progress nudge sent for {candidate.name} at turn {turn}",
                    extra={"operation": "progress_nudge",
                           "trace_id": trace_id,
                           "candidate_name": candidate.name},
                )

            # â”€â”€ Gate C â€” patch-fragmentation runtime nudge â”€â”€
            # When the builder serial-nibbles at the same file with two
            # small patches back-to-back, offer (softly) that parallel
            # patches land a multi-edit fix in ONE turn instead of N.
            # NOT a hard stop; genuine iterate-and-verify cycles continue
            # unchanged. At-most-once per file per build (see state set
            # above) so it can't spam the loop on legitimate long debug
            # sessions.
            if AGENT5_PATCH_FRAGMENT_NUDGE_ENABLED and AGENT5_PATCH_FRAGMENT_TOKEN_CEILING > 0:
                fragmented_file = _detect_patch_fragmentation_pattern(
                    conversation_log,
                    patch_fragmentation_nudged_files,
                    output_token_ceiling=AGENT5_PATCH_FRAGMENT_TOKEN_CEILING,
                )
                if fragmented_file:
                    messages.append({
                        "role": "user",
                        "content": (
                            f"## Patch-fragmentation observation\n\n"
                            f"The last 2 patches to `{fragmented_file}` were "
                            f"small and sequential. If you already know 3+ "
                            f"more edits to `{fragmented_file}` for the same "
                            f"underlying bug, emit them as same-turn "
                            f"`patch_file` calls in one assistant response - each "
                            f"round-trip costs a full conversation replay. "
                            f"If you're genuinely iterating (each patch "
                            f"depends on seeing the last one's effect, "
                            f"e.g., you need to run the relevant check/probe between "
                            f"edits), keep going sequentially - this nudge "
                            f"is advisory, not a mandate. This is the only "
                            f"nudge you'll see for `{fragmented_file}` "
                            f"this build.\n"
                        ),
                    })
                    patch_fragmentation_nudged_files.add(fragmented_file)
                    logger.info(
                        f"Patch-fragmentation nudge sent for {candidate.name} "
                        f"on file={fragmented_file} at turn {turn}",
                        extra={
                            "operation": "patch_fragmentation_nudge",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "filename": fragmented_file,
                            "turn": turn,
                        },
                    )

            # If stuck in an error loop, force escalating reassessment.
            # Hard cap on reassessment tiers â€” after N escalations without
            # recovery, accept defeat and emit FailedHarness cleanly.
            if total_reassessments >= AGENT5_MAX_REASSESSMENT_TIERS:
                current_issues = _current_completion_gate_issues(
                    attempt=verification_attempts + 1,
                )
                if current_issues is None:
                    verification_passed = True
                    completion_gate_status = "passed"
                    completion_gate_issues.clear()
                    _append_build_progress_event(sandbox_dir, {
                        "event": "completion_gate_passed",
                        "candidate_name": candidate.name,
                        "turn": turn,
                        "attempt": verification_attempts + 1,
                        "status": completion_gate_status,
                        "reason": "reassessment_cap_current_state_pass",
                    })
                    break
                logger.warning(
                    f"Agent 5: max reassessment tiers ({AGENT5_MAX_REASSESSMENT_TIERS}) "
                    f"reached for {candidate.name} â€” accepting failure cleanly",
                    extra={"operation": "reassessment_cap_reached",
                           "trace_id": trace_id,
                           "candidate_name": candidate.name,
                           "approaches_tried": approaches_tried},
                )
                completion_gate_status = "failed_reassessment_cap"
                completion_gate_issues = [
                    f"Max reassessment tiers ({AGENT5_MAX_REASSESSMENT_TIERS}) reached. "
                    f"Current gate issue: {current_issues}"
                ]
                break
            # Phase 4 Path B Step 2: reassessment trigger predicate +
            # message builder extracted to dispatch_helpers (pure
            # functions). Loop owns the COUNTER MUTATIONS (increment,
            # reset to 0) and the message append; the helpers compute
            # the bool gate + the message string.
            from puzzleeval.agents.agent5.dispatch_helpers import (
                build_reassessment_message,
                should_inject_reassessment,
            )
            if should_inject_reassessment(consecutive_errors, MAX_CONSECUTIVE_ERRORS):
                total_reassessments += 1
                last_errors = all_results_text[:500]
                # Record this reassessment as an "approach tried" so the
                # next tier's prompt can cite what NOT to repeat.
                _last_cat = error_history[-1][1] if error_history else "unknown"
                approaches_tried.append(
                    f"tier_{total_reassessments}_{_last_cat}"
                )
                reassessment = build_reassessment_message(
                    consecutive_errors=consecutive_errors,
                    last_errors=last_errors,
                    error_history=error_history,
                    total_reassessments=total_reassessments,
                    approaches_tried=approaches_tried,
                )
                messages.append({"role": "user", "content": reassessment})
                consecutive_errors = 0  # Reset streak â€” give agent a fresh chance

                conversation_log.append({
                    "turn": f"reassessment-{turn}",
                    "stop_reason": f"dead_end_tier{total_reassessments}",
                    "text": f"Escalating reassessment tier {total_reassessments} after {MAX_CONSECUTIVE_ERRORS} consecutive errors",
                    "tool_calls": [], "tool_results": [],
                })

        turn += 1

    if not verification_passed:
        if completion_gate_status in {"not_signaled", "awaiting_explicit_completion", "running", "retrying"}:
            completion_gate_status = (
                "failed_max_turns" if turn >= effective_max_turns else "failed_incomplete"
            )
        if not completion_gate_issues:
            completion_gate_issues.append(
                "Build loop ended before the unified completion gate passed."
            )

    _append_build_progress_event(sandbox_dir, {
        "event": "completion_gate_final",
        "candidate_name": candidate.name,
        "turn": turn,
        "status": completion_gate_status,
        "verification_passed": verification_passed,
        "issues": completion_gate_issues[-10:],
    })

    if _cfg.GATE_AUTONOMY_ARTIFACTS_ENABLED:
        try:
            class _FinalRuntimeState:
                pass
            _frs = _FinalRuntimeState()
            _frs.turn = turn
            _frs.accumulated_cost = accumulated_cost
            _frs.build_gate_accepted = build_gate_accepted
            _frs.implementation_plan_accepted = implementation_plan_accepted
            _frs.verification_attempts = verification_attempts
            _frs.verification_passed = verification_passed
            _frs.smoke_ever_passed = smoke_ever_passed
            _frs.smoke_passed_at_turn = smoke_passed_at_turn
            _frs.consecutive_errors = consecutive_errors
            _frs.total_reassessments = total_reassessments
            update_runtime_state(
                sandbox_dir=sandbox_dir,
                state=_frs,
                current_model=_current_loop_model(),
                smoke_test_status=("passing" if smoke_ever_passed else "not_run"),
                live_test_status=(
                    "passing" if live_ever_passed
                    else "failing" if smoke_ever_passed
                    else "not_run"
                ),
                live_passed_at_turn=live_passed_at_turn if live_ever_passed else None,
                last_live_test_output=last_live_test_output or None,
                completion_gate_status=completion_gate_status,
                completion_gate_issues=completion_gate_issues,
            )
        except OSError:
            pass

    # Phase 4.4: post-loop result assembly extracted to _finalize_build_result.
    return _finalize_build_result(
        candidate,
        input_data,
        sandbox_dir,
        conversation_log=conversation_log,
        credentials=credentials,
        provider_slug=provider_slug,
        turn=turn,
        last_text=last_text,
        accumulated_cost=accumulated_cost,
        candidate_web_fetch_blocks=candidate_web_fetch_blocks,
        smoke_ever_passed=smoke_ever_passed,
        verification_attempts=verification_attempts,
        verification_passed=verification_passed,
        completion_gate_status=completion_gate_status,
        completion_gate_issues=completion_gate_issues,
    )


__all__ = [
    "BuildContext",
    "BuildLoopState",
    "_initialize_loop_state",
    "_should_inject_turn_budget_nudge",
    "build_single_harness",
]
