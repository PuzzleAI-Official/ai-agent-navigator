"""Context compaction and bounded artifact restoration for Agent 5.

Agent 5 uses the builder model from turn 0. Compaction is still useful after
research_synthesis.json and implementation_plan.json are accepted because it
grounds the next turn in durable, load-bearing artifacts instead of bulky
research/tool logs.

The active policy is artifact restoration, not ritual rereading: the next turn
gets a compact packet with objective, docs, test manifest, research/build brief,
implementation plan, runtime state, latest failure context, and code diagnostics.
The builder should read deeper files only when a specific detail is missing.

Bypass: ``PUZZLEEVAL_CONTEXT_COMPACTION_AT_BUILD_GATE=0`` disables this
artifact-grounding step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


COMPACTION_EVENT_NAME = "context_compacted_at_build_gate"
SERVER_RESTORATION_EVENT_NAME = "context_restored_after_server_compaction"
SNAPSHOT_RELATIVE_PATH = "_agent_state/post_compaction_snapshot.json"
MAX_RESTORATION_PACKET_CHARS = 12_000
TARGET_RESTORATION_PACKET_CHARS = 10_000


def _agent_state_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / "_agent_state"


def _read_optional(path: Path, max_chars: int = 4000) -> str:
    """Read a file if it exists, truncating long content."""

    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"
    return text


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_outstanding_success_criteria(objective_md: str) -> str:
    """Pull the SUCCESS CRITERIA section out of objective.md."""

    if not objective_md:
        return ""
    marker = "## SUCCESS CRITERIA"
    idx = objective_md.find(marker)
    if idx < 0:
        return ""
    end_marker = "\n## "
    rest = objective_md[idx + len(marker):]
    end_idx = rest.find(end_marker)
    section = rest if end_idx < 0 else rest[:end_idx]
    return section.strip()


def _objective_summary(sandbox_dir: Path) -> dict[str, Any]:
    objective = _read_optional(_agent_state_dir(sandbox_dir) / "objective.md", 5000)
    criteria = _extract_outstanding_success_criteria(objective)
    objective_ids: list[str] = []
    for line in criteria.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [") and "OBJ-" in stripped:
            objective_ids.append(stripped[:120])
    return {
        "path": "_agent_state/objective.md",
        "objective_ids_or_criteria": objective_ids or criteria.splitlines()[:12],
    }


def _docs_entrypoint_summary(sandbox_dir: Path) -> dict[str, Any]:
    data = _read_json(_agent_state_dir(sandbox_dir) / "docs_entrypoint.json")
    prefetched = data.get("prefetched_docs") or data.get("prefetched_doc_files") or {}
    if isinstance(prefetched, dict):
        prefetched_files = list(prefetched.values())[:8]
    elif isinstance(prefetched, list):
        prefetched_files = prefetched[:8]
    else:
        prefetched_files = []
    return {
        "path": "_agent_state/docs_entrypoint.json",
        "evidence_status": data.get("evidence_status"),
        "docs_verdict": data.get("docs_verdict"),
        "primary_docs_entrypoint": data.get("primary_docs_entrypoint")
        or data.get("verified_api_docs_url"),
        "auth_method": data.get("auth_method"),
        "api_access_method": data.get("api_access_method"),
        "prefetched_docs": prefetched_files,
    }


def _test_manifest_summary(sandbox_dir: Path) -> dict[str, Any]:
    data = _read_json(_agent_state_dir(sandbox_dir) / "test_case_manifest.json")
    return {
        "path": "_agent_state/test_case_manifest.json",
        "input_families": data.get("input_families")
        or data.get("families")
        or data.get("input_family_summaries")
        or [],
        "representative_cases": data.get("representative_cases")
        or data.get("representative_test_cases")
        or [],
        "max_turns": data.get("max_turns"),
        "evaluation_mode": data.get("evaluation_mode"),
    }


def _research_build_brief_summary(sandbox_dir: Path) -> dict[str, Any]:
    path = _agent_state_dir(sandbox_dir) / "research_build_brief.json"
    data = _read_json(path)
    if data:
        return {
            "path": "_agent_state/research_build_brief.json",
            "brief": data.get("brief") or data.get("build_brief") or data,
        }
    text = _read_optional(path, 1800)
    return {"path": "_agent_state/research_build_brief.json", "brief": text} if text else {}


def _research_synthesis_summary(sandbox_dir: Path) -> dict[str, Any]:
    data = _read_json(_agent_state_dir(sandbox_dir) / "research_synthesis.json")
    return {
        "path": "_agent_state/research_synthesis.json",
        "build_brief": data.get("build_brief"),
        "chosen_api_surface": data.get("chosen_api_surface"),
        "unresolved_questions": data.get("unresolved_questions") or [],
        "facts_used_count": len(data.get("facts_used_for_implementation_plan") or []),
    }


def _implementation_plan_summary(sandbox_dir: Path) -> dict[str, Any]:
    data = _read_json(_agent_state_dir(sandbox_dir) / "implementation_plan.json")
    return {
        "path": "_agent_state/implementation_plan.json",
        "chosen_surface": data.get("chosen_surface") or data.get("chosen_api_surface"),
        "runtime_pattern": data.get("runtime_pattern") or data.get("interaction_pattern"),
        "file_plan": data.get("file_plan") or data.get("files_to_create"),
        "live_or_probe_strategy": data.get("live_test_strategy")
        or data.get("representative_probe_strategy")
        or data.get("validation_strategy"),
        "objective_coverage": data.get("objective_coverage"),
    }


def _runtime_state_summary(sandbox_dir: Path) -> dict[str, Any]:
    data = _read_json(_agent_state_dir(sandbox_dir) / "runtime_state.json")
    keys = (
        "current_phase",
        "current_turn",
        "files_present",
        "files_pending",
        "lead_model",
        "research_worker_model",
        "completion_gate_status",
        "completion_gate_issues",
        "context_compactions",
    )
    return {"path": "_agent_state/runtime_state.json", **{k: data.get(k) for k in keys}}


def _latest_failure_summary(sandbox_dir: Path) -> dict[str, Any]:
    data = _read_json(_agent_state_dir(sandbox_dir) / "latest_failure_packet.json")
    diagnosis = data.get("diagnosis") if isinstance(data.get("diagnosis"), dict) else {}
    return {
        "path": "_agent_state/latest_failure_packet.json",
        "turn": data.get("turn"),
        "failure_source": data.get("failure_source"),
        "exit_code": data.get("exit_code"),
        "mechanical_tags": data.get("mechanical_tags"),
        "observed_failure": diagnosis.get("observed_failure"),
        "likely_root_cause": diagnosis.get("likely_root_cause"),
        "next_diagnostic_or_patch": diagnosis.get("next_diagnostic_or_patch"),
        "research_would_change_implementation": diagnosis.get("research_would_change_implementation"),
        "issues": data.get("issues"),
        "code_diagnostics": data.get("code_diagnostics"),
    }


def _code_diagnostics_summary(sandbox_dir: Path) -> dict[str, Any]:
    try:
        from puzzleeval.agents.agent5.code_diagnostics import summarize_code_diagnostics

        summary = summarize_code_diagnostics(sandbox_dir)
    except Exception:  # noqa: BLE001
        summary = {}
    return {"path": "_agent_state/code_diagnostics.json", **summary} if summary else {}


def _truncate_json_payload(payload: Any, max_chars: int) -> tuple[str, bool]:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n... truncated at {max_chars} chars", True


def _section(
    title: str,
    payload: Any,
    *,
    max_chars: int,
) -> tuple[str, bool]:
    text, truncated = _truncate_json_payload(payload, max_chars)
    return f"## {title}\n{text}", truncated


def _write_snapshot(
    sandbox_dir: Path,
    *,
    event_name: str,
    turn: Any,
    included_artifacts: list[str],
    omitted_artifacts: list[str],
    truncation_flags: dict[str, bool],
    packet_char_count: int,
) -> None:
    payload = {
        "event_name": event_name,
        "turn": turn,
        "included_artifacts": included_artifacts,
        "omitted_artifacts": omitted_artifacts,
        "truncation_flags": truncation_flags,
        "packet_char_count": packet_char_count,
    }
    path = sandbox_dir / SNAPSHOT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


def compose_canonical_state_packet(
    sandbox_dir: Path,
    *,
    event_name: str = COMPACTION_EVENT_NAME,
) -> str:
    """Build and persist the bounded restored-state packet."""

    summaries: list[tuple[str, dict[str, Any], int]] = [
        ("Objective", _objective_summary(sandbox_dir), 1800),
        ("Docs Entrypoint", _docs_entrypoint_summary(sandbox_dir), 1200),
        ("Test Case Manifest", _test_manifest_summary(sandbox_dir), 1600),
        ("Research Build Brief", _research_build_brief_summary(sandbox_dir), 1800),
        ("Research Synthesis", _research_synthesis_summary(sandbox_dir), 1800),
        ("Implementation Plan", _implementation_plan_summary(sandbox_dir), 1800),
        ("Runtime State", _runtime_state_summary(sandbox_dir), 1200),
        ("Latest Failure Packet", _latest_failure_summary(sandbox_dir), 1200),
        ("Code Diagnostics", _code_diagnostics_summary(sandbox_dir), 1200),
    ]
    included: list[str] = []
    omitted: list[str] = [
        "full fetched docs",
        "full research findings",
        "full conversation logs",
    ]
    truncation: dict[str, bool] = {}
    body_sections: list[str] = []
    for title, payload, max_chars in summaries:
        clean_payload = {k: v for k, v in payload.items() if v not in (None, "", [], {})}
        if not clean_payload:
            continue
        section, was_truncated = _section(title, clean_payload, max_chars=max_chars)
        truncation[title] = was_truncated
        path = clean_payload.get("path")
        if isinstance(path, str):
            included.append(path)
        body_sections.append(section)

    header = "\n".join([
        "**Agent 5 post-compaction restoration packet. You are the builder.**",
        "",
        "Use this restored snapshot as current working context. Read files only "
        "when a specific detail needed for the next action is missing from the "
        "snapshot. After an accepted implementation plan, begin scaffold/build "
        "work without ritual rereads.",
        "",
        "Deeper artifacts are on disk at the paths shown below. This packet never "
        "inlines full fetched docs, full research findings, or conversation logs.",
        "",
    ])
    footer = "\n\n".join([
        "## Next Productive Action",
        "If the implementation plan is accepted and required scaffold files are "
        "pending, write or patch the minimal vertical slice now: requirements.txt, "
        "harness.py, smoke_test.py, and live_test.py. Keep dependent steps serial "
        "(patch, test, diagnose), and use parallel tool calls only for independent "
        "read-only inspection.",
    ])
    packet = header + "\n\n".join(body_sections) + "\n\n" + footer
    if len(packet) > MAX_RESTORATION_PACKET_CHARS:
        truncation["packet"] = True
        packet = (
            packet[:MAX_RESTORATION_PACKET_CHARS]
            + f"\n\n[restoration packet truncated at {MAX_RESTORATION_PACKET_CHARS} chars]"
        )
    else:
        truncation["packet"] = False

    runtime_turn = _runtime_state_summary(sandbox_dir).get("current_turn")
    _write_snapshot(
        sandbox_dir,
        event_name=event_name,
        turn=runtime_turn,
        included_artifacts=included,
        omitted_artifacts=omitted,
        truncation_flags=truncation,
        packet_char_count=len(packet),
    )
    return packet


def compact_for_build_gate(sandbox_dir: Path) -> list[dict[str, Any]]:
    """Return a fresh messages list with one restored-state user message."""

    content = compose_canonical_state_packet(
        sandbox_dir,
        event_name=COMPACTION_EVENT_NAME,
    )
    return [{"role": "user", "content": content}]


def restore_after_server_compaction(sandbox_dir: Path) -> dict[str, Any]:
    """Return one restoration user message after server-side compaction."""

    return {
        "role": "user",
        "content": compose_canonical_state_packet(
            sandbox_dir,
            event_name=SERVER_RESTORATION_EVENT_NAME,
        ),
    }


def compact_for_model_transition(sandbox_dir: Path) -> list[dict[str, Any]]:
    """Compatibility wrapper for old imports."""

    return compact_for_build_gate(sandbox_dir)


__all__ = [
    "COMPACTION_EVENT_NAME",
    "SERVER_RESTORATION_EVENT_NAME",
    "SNAPSHOT_RELATIVE_PATH",
    "compose_canonical_state_packet",
    "compact_for_build_gate",
    "compact_for_model_transition",
    "restore_after_server_compaction",
]
