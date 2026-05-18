"""Evidence-first failure packets for Agent 5 debugging.

Failure packets are a durable debugging surface, not a semantic router.  The
deterministic layer records facts the code can prove: command, exit status,
stdout/stderr tails, changed files, forensics, diagnostics, representative
probe evidence, and mechanical tags.  Ambiguous root-cause diagnosis is a
bounded LLM review that returns natural language in a stable JSON shape.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Callable


FAILURE_PACKET_DIRNAME = "failure_packets"
DEBUG_RESEARCH_LEDGER_FILENAME = "debug_research_ledger.json"

MECHANICAL_TAG_ORDER: tuple[str, ...] = (
    "missing_file",
    "invalid_json",
    "artifact_validation_failed",
    "write_rejected",
    "syntax_error",
    "command_failed",
    "timeout",
    "credentials_missing",
    "credentials_rejected",
    "http_401",
    "http_403",
    "http_429",
    "provider_blocked_mechanical",
    "quota_or_billing",
    "representative_probe_failed",
    "empty_success_output",
    "stream_no_output",
    "stream_output_then_control",
    "stream_control_only_timeout",
    "tool_protocol_error",
)
MECHANICAL_FAILURE_TAGS = frozenset(MECHANICAL_TAG_ORDER)

_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):(.*?)(?:\n\[Exit code:|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_SAFE_PACKET_PART_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _agent_state_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / "_agent_state"


def _packet_dir(sandbox_dir: Path) -> Path:
    return _agent_state_dir(sandbox_dir) / FAILURE_PACKET_DIRNAME


def _safe_packet_part(value: str) -> str:
    safe = _SAFE_PACKET_PART_RE.sub("_", str(value or "unknown")).strip("._-")
    return safe[:80] or "unknown"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _tail(text: str, limit: int = 2000) -> str:
    return (text or "")[-limit:]


def _extract_streams(result_text: str) -> tuple[str, str, str]:
    text = result_text or ""
    stderr = ""
    stdout = text
    marker = "[stderr]"
    if marker in text:
        stdout, stderr = text.split(marker, 1)
        stderr = stderr.strip()
    match = _TRACEBACK_RE.search(text)
    tb = match.group(0).strip() if match else ""
    return _tail(stdout.strip()), _tail(stderr), _tail(tb)


def _changed_files_since_last_test(conversation_log: list[dict[str, Any]] | None) -> list[str]:
    if not conversation_log:
        return []
    changed: list[str] = []
    for turn in reversed(conversation_log[:-1]):
        for result in turn.get("tool_results") or []:
            if not isinstance(result, dict):
                continue
            if result.get("is_error") or result.get("persisted") is False:
                continue
            tool = result.get("tool")
            if tool == "run_code":
                return list(reversed(dict.fromkeys(reversed(changed))))
            path = result.get("wrote_path") or result.get("patch_path")
            if tool in {"write_file", "patch_file"} and path:
                changed.append(str(path))
        for call in turn.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            tool = call.get("tool")
            data = call.get("input") if isinstance(call.get("input"), dict) else {}
            path = data.get("filename") or data.get("path") if isinstance(data, dict) else ""
            if tool in {"write_file", "patch_file"} and path:
                changed.append(str(path))
    return list(reversed(dict.fromkeys(reversed(changed))))


def _runtime_excerpt(sandbox_dir: Path) -> dict[str, Any]:
    path = _agent_state_dir(sandbox_dir) / "runtime_state.json"
    if not path.exists():
        return {}
    try:
        runtime = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    keys = (
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
    )
    return {k: runtime.get(k) for k in keys if k in runtime}


_STREAM_OUTPUT_TOKENS = (
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
_STREAM_CONTROL_TOKENS = (
    "ping",
    "pong",
    "heartbeat",
    "keepalive",
    "keep_alive",
    "metadata",
    "ack",
)
_STREAM_LIFECYCLE_TOKENS = (
    "open",
    "close",
    "closed",
    "connect",
    "disconnect",
    "session",
    "initiation",
    "op_done",
)
_STREAM_ERROR_TOKENS = (
    "error",
    "exception",
    "failed",
    "failure",
    "unauthorized",
    "forbidden",
)


def _event_label(event: dict[str, Any]) -> str:
    return str(
        event.get("type")
        or event.get("event_type")
        or event.get("event")
        or event.get("op")
        or ""
    ).lower()


def _event_text(event: dict[str, Any]) -> str:
    try:
        return json.dumps(event, ensure_ascii=False, default=str).lower()
    except TypeError:
        return str(event).lower()


def _is_stream_event(event: dict[str, Any]) -> bool:
    label = _event_label(event)
    return (
        event.get("event") == "stream_event"
        or (
            str(event.get("kind") or "").lower() in {"ws", "stream", "event"}
            and any(token in label for token in (*_STREAM_OUTPUT_TOKENS, *_STREAM_CONTROL_TOKENS))
        )
    )


def _stream_event_class(event: dict[str, Any]) -> str:
    label = _event_label(event)
    text = _event_text(event)
    if any(token in label or token in text for token in _STREAM_ERROR_TOKENS):
        return "error"
    if any(token in label for token in _STREAM_OUTPUT_TOKENS):
        return "output"
    if any(token in label for token in _STREAM_CONTROL_TOKENS):
        return "control"
    if any(token in label for token in _STREAM_LIFECYCLE_TOKENS):
        return "lifecycle"
    return "unknown"


def _event_time_ms(event: dict[str, Any]) -> float | None:
    for key in ("t_ms", "duration_ms"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _compact_forensics_event(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    keys = ("event", "kind", "dir", "type", "event_type", "op", "t_ms", "duration_ms", "bytes")
    compact = {key: event.get(key) for key in keys if key in event}
    if event.get("error"):
        compact["error"] = str(event.get("error"))[:300]
    if event.get("message"):
        compact["message"] = str(event.get("message"))[:300]
    return compact


def _timeout_relation(
    *,
    timeout_observed: bool,
    stream_events: list[dict[str, Any]],
    output_events: list[dict[str, Any]],
    control_events: list[dict[str, Any]],
) -> str:
    if not timeout_observed:
        return "not_timeout"
    if not stream_events:
        return "timeout_without_stream_evidence"
    if not output_events:
        return "timeout_after_only_control" if control_events else "timeout_before_output"
    last_output_index = max(stream_events.index(event) for event in output_events)
    tail_classes = {
        _stream_event_class(event)
        for event in stream_events[last_output_index + 1:]
        if _stream_event_class(event) not in {"lifecycle", "unknown"}
    }
    if tail_classes <= {"control"}:
        return "timeout_after_output_then_control"
    return "timeout_after_output_with_mixed_events"


def _forensics_summary(sandbox_dir: Path, *, timeout_observed: bool = False) -> dict[str, Any]:
    path = sandbox_dir / "harness_forensics.jsonl"
    if not path.exists():
        return {"status": "missing"}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"status": "unreadable", "error": str(exc)[:200]}
    events: list[dict[str, Any]] = []
    for line in lines[-200:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    counts = Counter(str(e.get("event", "<missing>")) for e in events)
    stream_events = [event for event in events if _is_stream_event(event)]
    class_counts = Counter(_stream_event_class(event) for event in stream_events)
    output_events = [event for event in stream_events if _stream_event_class(event) == "output"]
    control_events = [event for event in stream_events if _stream_event_class(event) == "control"]
    stream_start = next((_event_time_ms(event) for event in stream_events if _event_time_ms(event) is not None), None)
    stream_end = next(
        (_event_time_ms(event) for event in reversed(stream_events) if _event_time_ms(event) is not None),
        None,
    )
    stream_duration_ms = (
        round(stream_end - stream_start, 2)
        if stream_start is not None and stream_end is not None and stream_end >= stream_start
        else None
    )
    forensics_timeout = any(
        "timeout" in _event_text(event) or "timed out" in _event_text(event)
        for event in events[-50:]
    )
    timeout_observed = bool(timeout_observed or forensics_timeout)
    recent_errors = [
        {
            "event": e.get("event"),
            "error": str(e.get("error") or e.get("message") or "")[:300],
            "turn_index": e.get("turn_index"),
        }
        for e in events
        if "error" in str(e.get("event", "")).lower()
        or e.get("error")
        or "quota" in json.dumps(e, default=str).lower()
        or "credit" in json.dumps(e, default=str).lower()
    ][-5:]
    return {
        "status": "ok",
        "event_count": len(events),
        "top_events": dict(counts.most_common(8)),
        "recent_errors": recent_errors,
        "stream_summary": {
            "stream_event_count": len(stream_events),
            "output_event_count": len(output_events),
            "control_event_count": len(control_events),
            "lifecycle_event_count": class_counts.get("lifecycle", 0),
            "error_event_count": class_counts.get("error", 0),
            "unknown_event_count": class_counts.get("unknown", 0),
            "last_meaningful_output_event": _compact_forensics_event(output_events[-1] if output_events else None),
            "last_control_event": _compact_forensics_event(control_events[-1] if control_events else None),
            "stream_duration_ms": stream_duration_ms,
            "timeout_observed": timeout_observed,
            "timeout_relation": _timeout_relation(
                timeout_observed=timeout_observed,
                stream_events=stream_events,
                output_events=output_events,
                control_events=control_events,
            ),
        },
    }


def _timeout_observed(result_text: str, issues: list[str], command: str) -> bool:
    text = "\n".join([result_text or "", command or "", *issues]).lower()
    return any(marker in text for marker in ("timeout", "timed out", "deadline"))


def _representative_probe_excerpt(sandbox_dir: Path) -> dict[str, Any]:
    path = _agent_state_dir(sandbox_dir) / "representative_probe_evidence.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unreadable", "path": "_agent_state/representative_probe_evidence.json"}
    results = data.get("results") if isinstance(data, dict) else []
    compact_results: list[dict[str, Any]] = []
    for item in (results or [])[:8]:
        if not isinstance(item, dict):
            continue
        payloads = item.get("payloads_supplied_to_harness") or []
        raw_result = item.get("raw_result") if isinstance(item.get("raw_result"), dict) else {}
        verdict = item.get("verdict") if isinstance(item.get("verdict"), dict) else {}
        compact_results.append({
            "test_case_id": item.get("test_case_id"),
            "family_key": item.get("family_key"),
            "test_case": item.get("test_case") if isinstance(item.get("test_case"), dict) else {},
            "status": item.get("status"),
            "external_block_reason": item.get("external_block_reason"),
            "input_observation": item.get("input_observation")
            if isinstance(item.get("input_observation"), dict)
            else {},
            "payload_count": len(payloads) if isinstance(payloads, list) else 0,
            "payloads_supplied_to_harness": payloads[:3] if isinstance(payloads, list) else [],
            "raw_result": {
                "success": raw_result.get("success"),
                "error": raw_result.get("error"),
                "output_chars": raw_result.get("output_chars"),
                "audio_path_count": raw_result.get("audio_path_count"),
                "has_merged_audio": raw_result.get("has_merged_audio"),
            },
            "verdict": {
                "passed": verdict.get("passed"),
                "score": verdict.get("score"),
                "fallback_reason": verdict.get("fallback_reason"),
                "reasoning": str(verdict.get("reasoning") or "")[:800],
                "artifact_count": verdict.get("artifact_count"),
            },
        })
    return {
        "path": "_agent_state/representative_probe_evidence.json",
        "status": data.get("status") if isinstance(data, dict) else None,
        "gate_issue": data.get("gate_issue") if isinstance(data, dict) else None,
        "selected_test_ids": data.get("selected_test_ids") if isinstance(data, dict) else None,
        "results": compact_results,
    }


def _code_diagnostics_summary(sandbox_dir: Path, changed_files: list[str]) -> dict[str, Any]:
    try:
        from puzzleeval.agents.agent5.code_diagnostics import (
            SCAFFOLD_PYTHON_FILES,
            active_code_diagnostics,
            read_code_diagnostics,
        )

        registry = read_code_diagnostics(sandbox_dir)
        relevant_paths = {
            str(path).replace("\\", "/")
            for path in changed_files
            if str(path).replace("\\", "/").endswith(".py")
        }
        relevant_paths.update(SCAFFOLD_PYTHON_FILES)
        relevant = active_code_diagnostics(registry, paths=relevant_paths)
        active = active_code_diagnostics(registry)
        return {
            "registry_path": "_agent_state/code_diagnostics.json",
            "active_count": len(active),
            "relevant_count": len(relevant),
            "relevant_diagnostics": relevant[:8],
        }
    except Exception:  # noqa: BLE001 - diagnostics must not block packets
        return {}


def _add_tag(tags: set[str], tag: str) -> None:
    if tag in MECHANICAL_FAILURE_TAGS:
        tags.add(tag)


def _mechanical_tags(
    *,
    result_text: str,
    command: str,
    failure_source: str,
    exit_code: int | None,
    issues: list[str],
    forensics_summary: dict[str, Any],
    representative_probe: dict[str, Any],
    code_diagnostics: dict[str, Any],
) -> list[str]:
    text = "\n".join([failure_source or "", command or "", result_text or "", *issues]).lower()
    tags: set[str] = set()
    if exit_code not in (None, 0):
        _add_tag(tags, "command_failed")
    if "no such file" in text or "does not exist in sandbox" in text or "missing file" in text:
        _add_tag(tags, "missing_file")
    if "invalid json" in text or "jsondecodeerror" in text:
        _add_tag(tags, "invalid_json")
    if any(marker in text for marker in (
        "failed phase 3 research artifact validation",
        "failed implementation-plan validation",
        "implementation_plan.json failed validation",
        "research_synthesis.json failed",
        "cannot accept implementation_plan.json before",
        "artifact validation",
    )):
        _add_tag(tags, "artifact_validation_failed")
    if "write_file" in failure_source and ("rejected" in text or "error:" in text):
        _add_tag(tags, "write_rejected")
    if "syntaxerror" in text or int(code_diagnostics.get("relevant_count") or 0) > 0:
        _add_tag(tags, "syntax_error")
    if "401" in text or "unauthorized" in text:
        _add_tag(tags, "http_401")
        _add_tag(tags, "credentials_rejected")
    if "403" in text or "forbidden" in text:
        _add_tag(tags, "http_403")
        _add_tag(tags, "provider_blocked_mechanical")
    if "429" in text or "rate limit" in text:
        _add_tag(tags, "http_429")
    if "missing api key" in text or "api_key not set" in text or "api key not set" in text:
        _add_tag(tags, "credentials_missing")
    if any(marker in text for marker in ("quota", "billing", "credit", "payment required", "out of credits")):
        _add_tag(tags, "quota_or_billing")
    if any(marker in text for marker in ("timeout", "timed out", "deadline")):
        _add_tag(tags, "timeout")
    if "tool_use ids were found without tool_result" in text or "tool protocol" in text:
        _add_tag(tags, "tool_protocol_error")
    if str(representative_probe.get("status") or "") == "failed" or "representative_probe" in text:
        _add_tag(tags, "representative_probe_failed")
    for item in representative_probe.get("results") or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("raw_result") if isinstance(item.get("raw_result"), dict) else {}
        if raw.get("success") is True and int(raw.get("output_chars") or 0) == 0 and int(raw.get("audio_path_count") or 0) == 0:
            _add_tag(tags, "empty_success_output")
        observation = item.get("input_observation") if isinstance(item.get("input_observation"), dict) else {}
        if observation.get("empty_success_without_output_evidence") is True:
            _add_tag(tags, "empty_success_output")
    stream = forensics_summary.get("stream_summary") if isinstance(forensics_summary, dict) else {}
    if isinstance(stream, dict):
        relation = str(stream.get("timeout_relation") or "")
        if int(stream.get("output_event_count") or 0) == 0 and int(stream.get("stream_event_count") or 0) > 0:
            _add_tag(tags, "stream_no_output")
        if relation == "timeout_after_output_then_control":
            _add_tag(tags, "stream_output_then_control")
        if relation in {"timeout_after_only_control", "timeout_before_output"}:
            _add_tag(tags, "stream_control_only_timeout")
    return [tag for tag in MECHANICAL_TAG_ORDER if tag in tags]


def classify_failure(result_text: str, *, command: str = "", failure_source: str = "") -> str:
    """Compatibility helper returning the first mechanical tag, not a semantic class."""

    tags = _mechanical_tags(
        result_text=result_text,
        command=command,
        failure_source=failure_source,
        exit_code=None,
        issues=[],
        forensics_summary={},
        representative_probe={},
        code_diagnostics={},
    )
    return tags[0] if tags else "unknown"


def recommended_action(_category: str) -> str:
    """Compatibility helper; builder-facing packets now use natural-language diagnosis."""

    return "inspect_evidence"


def is_meaningful_failure_source(
    *,
    tool_name: str,
    exit_code: int | None,
    result_text: str,
    command: str = "",
    failure_source: str = "",
) -> bool:
    if exit_code == 0:
        return False
    if tool_name == "run_code":
        return True
    if tool_name in {"read_file", "read_file_range"}:
        return False
    tag = classify_failure(result_text, command=command, failure_source=failure_source)
    if tag != "unknown":
        return True
    lowered = (result_text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "implementation_plan.json failed",
            "completion gate",
            "harness_complete",
            "live_test.py",
            "smoke_test.py",
            "representative_probe",
        )
    )


def action_requires_abandon(action: str) -> bool:
    return str(action or "").startswith("abandon_candidate:")


def action_requires_research(action: str) -> bool:
    return "research" in str(action or "").lower()


def diagnosis_requests_research(packet: dict[str, Any] | None) -> bool:
    diagnosis = (packet or {}).get("diagnosis")
    return bool(
        isinstance(diagnosis, dict)
        and diagnosis.get("research_would_change_implementation") is True
        and str(diagnosis.get("research_question") or "").strip()
    )


def _debug_research_ledger_path(sandbox_dir: Path) -> Path:
    return _agent_state_dir(sandbox_dir) / DEBUG_RESEARCH_LEDGER_FILENAME


def _read_debug_research_ledger(sandbox_dir: Path) -> list[dict[str, Any]]:
    path = _debug_research_ledger_path(sandbox_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("attempts") if isinstance(data, dict) else data
    return [item for item in (items or []) if isinstance(item, dict)]


def debug_research_attempts_for_packet(sandbox_dir: Path, packet: dict[str, Any] | None) -> int:
    if not packet:
        return 0
    packet_path = str(packet.get("path") or "")
    return sum(
        1 for item in _read_debug_research_ledger(sandbox_dir)
        if item.get("packet_path") == packet_path
    )


def record_debug_research_attempt(
    sandbox_dir: Path,
    *,
    packet: dict[str, Any],
    question: str,
    turn: int,
) -> dict[str, Any]:
    attempts = _read_debug_research_ledger(sandbox_dir)
    diagnosis = packet.get("diagnosis") if isinstance(packet.get("diagnosis"), dict) else {}
    entry = {
        "t_abs": time.time(),
        "turn": turn,
        "packet_path": packet.get("path"),
        "mechanical_tags": packet.get("mechanical_tags"),
        "research_question": diagnosis.get("research_question"),
        "question": str(question or "")[:500],
    }
    attempts.append(entry)
    path = _debug_research_ledger_path(sandbox_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, {"attempts": attempts[-50:]})
    return entry


def build_failure_packet_research_context(packet: dict[str, Any]) -> str:
    fields = {
        "path": packet.get("path"),
        "failure_source": packet.get("failure_source"),
        "command": packet.get("command"),
        "mechanical_tags": packet.get("mechanical_tags"),
        "diagnosis": packet.get("diagnosis"),
        "stderr_tail": packet.get("stderr_tail"),
        "issues": packet.get("issues"),
        "evidence_envelope": packet.get("evidence_envelope"),
        "forensics_summary": packet.get("forensics_summary"),
        "representative_probe": packet.get("representative_probe"),
    }
    return (
        "\n\n## Attached Failure Packet\n"
        "Answer only the scoped docs/provider gap named in diagnosis.research_question "
        "or in FIELD NEEDED/WHY. Do not broaden into general provider research.\n"
        + json.dumps(fields, indent=2, ensure_ascii=False, default=str)[:5000]
    )


FailureDiagnosticReviewer = Callable[[dict[str, Any]], dict[str, Any] | None]


def build_failure_diagnostic_prompt(packet: dict[str, Any]) -> str:
    compact = {
        "failure_source": packet.get("failure_source"),
        "command": packet.get("command"),
        "exit_code": packet.get("exit_code"),
        "mechanical_tags": packet.get("mechanical_tags"),
        "issues": packet.get("issues"),
        "evidence_envelope": packet.get("evidence_envelope"),
        "forensics_summary": packet.get("forensics_summary"),
        "representative_probe": packet.get("representative_probe"),
        "runtime_state_excerpt": packet.get("runtime_state_excerpt"),
        "stdout_tail": packet.get("stdout_tail"),
        "stderr_tail": packet.get("stderr_tail"),
    }
    return (
        "You are reviewing one failed Agent 5 build step. Use only the evidence "
        "in this packet. Do not choose from a category list and do not invent "
        "provider-specific rules. Explain the likely root cause in natural "
        "language and name the next useful diagnostic or patch.\n\n"
        "Return strict JSON with keys:\n"
        "- observed_failure: one short sentence describing what failed\n"
        "- evidence: array of 1-6 packet-grounded facts\n"
        "- likely_root_cause: natural-language hypothesis, not an enum\n"
        "- uncertainty: what is still unknown or ambiguous\n"
        "- next_diagnostic_or_patch: concrete next action in natural language\n"
        "- research_would_change_implementation: boolean\n"
        "- research_question: optional scoped question if research would help\n"
        "- confidence: low, medium, or high\n\n"
        "Packet:\n"
        + json.dumps(compact, indent=2, ensure_ascii=False, default=str)[:9000]
    )


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


def _json_object_from_text(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    candidates = [stripped]
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def normalize_diagnostic_review(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    data = review.get("diagnosis") if isinstance(review.get("diagnosis"), dict) else review
    if not isinstance(data, dict):
        return None
    evidence_raw = data.get("evidence")
    evidence = [
        str(item).strip()[:400]
        for item in (evidence_raw if isinstance(evidence_raw, list) else [])
        if str(item).strip()
    ][:6]
    confidence = str(data.get("confidence") or "low").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    diagnosis = {
        "observed_failure": str(data.get("observed_failure") or "")[:700],
        "evidence": evidence,
        "likely_root_cause": str(data.get("likely_root_cause") or data.get("rationale") or "")[:1000],
        "uncertainty": str(data.get("uncertainty") or "")[:700],
        "next_diagnostic_or_patch": str(data.get("next_diagnostic_or_patch") or data.get("next_action") or "")[:1000],
        "research_would_change_implementation": bool(data.get("research_would_change_implementation")),
        "research_question": str(data.get("research_question") or "")[:700],
        "confidence": confidence,
    }
    if not diagnosis["observed_failure"] and not diagnosis["likely_root_cause"]:
        return None
    return diagnosis


def _default_diagnosis(packet: dict[str, Any]) -> dict[str, Any]:
    tags = set(packet.get("mechanical_tags") or [])
    evidence: list[str] = []
    if packet.get("command"):
        evidence.append(f"Command: {packet.get('command')}")
    if packet.get("exit_code") not in (None, 0):
        evidence.append(f"Exit code: {packet.get('exit_code')}")
    stdout_tail = str(packet.get("stdout_tail") or "").strip()
    stderr_tail = str(packet.get("stderr_tail") or "").strip()
    if stdout_tail:
        evidence.append(f"stdout_tail: {stdout_tail[-300:]}")
    if stderr_tail:
        evidence.append(f"stderr_tail: {stderr_tail[-300:]}")
    for issue in packet.get("issues") or []:
        evidence.append(str(issue)[:300])
    stream = ((packet.get("forensics_summary") or {}).get("stream_summary") or {})
    if stream:
        evidence.append(
            "Stream events: "
            f"output={stream.get('output_event_count')}, "
            f"control={stream.get('control_event_count')}, "
            f"timeout_relation={stream.get('timeout_relation')}"
        )
    rep = packet.get("representative_probe") or {}
    if rep.get("status"):
        evidence.append(f"Representative probe status: {rep.get('status')}")

    observed = "The build step failed and needs evidence-grounded debugging."
    root = "Inspect the evidence envelope, then patch the smallest harness or artifact issue that explains it."
    next_step = "Inspect latest_failure_packet.json, relevant test/probe evidence, and changed files before patching."
    research = False
    question = ""
    uncertainty = "No LLM diagnostic review was available; this is a deterministic evidence summary."

    if "representative_probe_failed" in tags and "empty_success_output" in tags:
        observed = "The representative probe failed because the harness reported success without usable output."
        root = "The harness likely has a false-success path or is not routing the representative test input to the provider/output parser."
        next_step = "Patch the final harness so the representative test input reaches the provider path and empty output cannot return success=True."
    elif "stream_no_output" in tags:
        observed = "The stream produced no output-bearing events before the failure."
        root = "The harness may be using the wrong interaction path, missing a send/input step, or lacking a provider fact needed for this stream."
        next_step = "Inspect stream forensics and payload evidence; ask one scoped research gap only if the provider interaction fact is missing."
        research = True
        question = "What provider interaction step or event indicates output for the failing stream path?"
    elif "stream_output_then_control" in tags:
        observed = "Output-bearing stream events arrived, then only control traffic continued until timeout."
        root = "The harness may have a completion/termination detection bug rather than a provider-output problem."
        next_step = "Patch completion detection using the observed output and lifecycle/control events."
    elif "artifact_validation_failed" in tags:
        observed = "An orchestrator artifact was rejected by schema/contract validation."
        root = "The artifact shape is mechanically invalid or missing required fields."
        next_step = "Rewrite or patch the rejected artifact according to the validation message and actual persisted state."
    elif "syntax_error" in tags:
        observed = "Python syntax diagnostics are active for generated scaffold code."
        root = "The generated Python file has a mechanical syntax error."
        next_step = "Patch the syntax error before running more semantic provider tests."
    elif "credentials_missing" in tags or "credentials_rejected" in tags:
        observed = "The provider call could not authenticate."
        root = "Credentials are missing, rejected, or mapped to the wrong environment/header."
        next_step = "Verify env var mapping and auth header shape from implementation_plan/research_synthesis; abandon only on proven external credential block."

    return {
        "observed_failure": observed,
        "evidence": evidence[:6],
        "likely_root_cause": root,
        "uncertainty": uncertainty,
        "next_diagnostic_or_patch": next_step,
        "research_would_change_implementation": research,
        "research_question": question,
        "confidence": "low",
    }


def review_failure_packet_with_llm(
    client: Any,
    packet: dict[str, Any],
    *,
    model: str,
    max_tokens: int = 900,
) -> dict[str, Any] | None:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=(
            "You are a PuzzleEval failure-diagnosis reviewer. Be concise, "
            "evidence-grounded, and general across providers. Do not use a "
            "closed semantic category taxonomy."
        ),
        messages=[{"role": "user", "content": build_failure_diagnostic_prompt(packet)}],
    )
    return normalize_diagnostic_review(_json_object_from_text(_response_text(response)))


def write_failure_packet(
    sandbox_dir: Path,
    *,
    turn: int,
    candidate_name: str,
    failure_source: str,
    result_text: str,
    command: str = "",
    exit_code: int | None = None,
    issues: list[str] | str | None = None,
    conversation_log: list[dict[str, Any]] | None = None,
    diagnostic_reviewer: FailureDiagnosticReviewer | None = None,
) -> dict[str, Any]:
    stdout_tail, stderr_tail, traceback_tail = _extract_streams(result_text)
    issue_list = (
        [str(i) for i in issues]
        if isinstance(issues, list)
        else ([str(issues)] if issues else [])
    )
    changed_files = _changed_files_since_last_test(conversation_log)
    runtime_excerpt = _runtime_excerpt(sandbox_dir)
    timeout_seen = _timeout_observed(result_text, issue_list, command)
    forensics = _forensics_summary(sandbox_dir, timeout_observed=timeout_seen)
    representative_probe = _representative_probe_excerpt(sandbox_dir)
    code_diag = _code_diagnostics_summary(sandbox_dir, changed_files)
    tags = _mechanical_tags(
        result_text=result_text,
        command=command,
        failure_source=failure_source,
        exit_code=exit_code,
        issues=issue_list,
        forensics_summary=forensics,
        representative_probe=representative_probe,
        code_diagnostics=code_diag,
    )
    packet = {
        "schema_version": 2,
        "t_abs": time.time(),
        "turn": turn,
        "candidate": candidate_name,
        "failure_source": failure_source,
        "command": command,
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "traceback_tail": traceback_tail,
        "issues": issue_list,
        "files_changed_since_last_test": changed_files,
        "runtime_state_excerpt": runtime_excerpt,
        "forensics_summary": forensics,
        "representative_probe": representative_probe,
        "code_diagnostics": code_diag,
        "mechanical_tags": tags,
        "evidence_envelope": {
            "command": command,
            "exit_code": exit_code,
            "timeout_observed": timeout_seen,
            "changed_files_since_last_test": changed_files,
            "runtime_phase": runtime_excerpt.get("current_phase"),
            "stdout_tail": stdout_tail[-1200:],
            "stderr_tail": stderr_tail[-1200:],
            "stream_summary": forensics.get("stream_summary"),
            "representative_probe": representative_probe,
            "code_diagnostics": code_diag,
        },
    }
    diagnosis = None
    if diagnostic_reviewer is not None:
        try:
            diagnosis = normalize_diagnostic_review(diagnostic_reviewer(packet))
        except Exception:  # noqa: BLE001 - diagnostics cannot break builds
            diagnosis = None
            packet["llm_diagnostic_review_error"] = traceback.format_exc(limit=2)[-500:]
    packet["diagnosis"] = diagnosis or _default_diagnosis(packet)
    if diagnosis is not None:
        packet["llm_diagnostic_review"] = diagnosis

    try:
        out_dir = _packet_dir(sandbox_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_source = _safe_packet_part(failure_source)
        path = out_dir / f"turn_{turn:03d}_{safe_source}.json"
        packet["path"] = str(path.relative_to(sandbox_dir))
        _atomic_write_json(path, packet)
        latest = _agent_state_dir(sandbox_dir) / "latest_failure_packet.json"
        _atomic_write_json(latest, packet)
    except OSError:
        packet["write_error"] = traceback.format_exc(limit=2)[-500:]
    return packet


def read_latest_failure_packet(sandbox_dir: Path) -> dict[str, Any] | None:
    path = _agent_state_dir(sandbox_dir) / "latest_failure_packet.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def summarize_failure_packets(sandbox_dir: Path) -> dict[str, Any]:
    out_dir = _packet_dir(sandbox_dir)
    if not out_dir.exists():
        return {"count": 0, "by_mechanical_tag": {}, "latest": None}
    packets: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            packets.append(data)
    by_tag: Counter[str] = Counter()
    for packet in packets:
        for tag in packet.get("mechanical_tags") or ["untagged"]:
            by_tag[str(tag)] += 1
    latest = packets[-1] if packets else None
    latest_diagnosis = latest.get("diagnosis") if isinstance((latest or {}).get("diagnosis"), dict) else {}
    return {
        "count": len(packets),
        "by_mechanical_tag": dict(by_tag),
        "latest": {
            "turn": latest.get("turn"),
            "failure_source": latest.get("failure_source"),
            "mechanical_tags": latest.get("mechanical_tags"),
            "observed_failure": latest_diagnosis.get("observed_failure"),
            "likely_root_cause": latest_diagnosis.get("likely_root_cause"),
            "next_diagnostic_or_patch": latest_diagnosis.get("next_diagnostic_or_patch"),
            "research_would_change_implementation": latest_diagnosis.get("research_would_change_implementation"),
            "path": latest.get("path"),
        } if latest else None,
    }


__all__ = [
    "DEBUG_RESEARCH_LEDGER_FILENAME",
    "FAILURE_PACKET_DIRNAME",
    "MECHANICAL_FAILURE_TAGS",
    "action_requires_abandon",
    "action_requires_research",
    "build_failure_diagnostic_prompt",
    "build_failure_packet_research_context",
    "classify_failure",
    "debug_research_attempts_for_packet",
    "diagnosis_requests_research",
    "is_meaningful_failure_source",
    "normalize_diagnostic_review",
    "record_debug_research_attempt",
    "recommended_action",
    "read_latest_failure_packet",
    "review_failure_packet_with_llm",
    "summarize_failure_packets",
    "write_failure_packet",
]
