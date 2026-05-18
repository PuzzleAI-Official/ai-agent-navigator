"""Agent 5 planned-research artifacts and validators.

Phase 3 of the Agent 4/5 recovery plan moves initial research from
ad-hoc in-loop ask_research calls to an explicit Agent 5-owned plan. The
orchestrator can then run focused workers and hand scoped findings back
to the builder without making Agent 4 an implementation planner.
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


RESEARCH_PLAN_RELATIVE_PATH = "_agent_state/research_plan.json"
RESEARCH_FINDINGS_DIR_RELATIVE = "_agent_state/research_findings"
RESEARCH_SYNTHESIS_RELATIVE_PATH = "_agent_state/research_synthesis.json"
RESEARCH_EXECUTION_MARKER_RELATIVE_PATH = "_agent_state/research_plan_execution.json"
RESEARCH_FINDINGS_INDEX_RELATIVE_PATH = "_agent_state/research_findings_index.json"
RESEARCH_BUILD_BRIEF_RELATIVE_PATH = "_agent_state/research_build_brief.json"

ALLOWED_CONFIDENCE = {"high", "medium", "low", "not_found"}
ALLOWED_DOC_MAP_STATUSES = {"current", "alternate", "deprecated", "blocked", "missing", "unknown"}

_SOURCE_URL_RE = re.compile(r"https?://[^\s<>)\"']+")


@dataclass(frozen=True)
class ResearchArtifactValidation:
    ok: bool
    issues: list[str]
    task_count: int = 0
    citation_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "task_count": self.task_count,
            "citation_count": self.citation_count,
        }


@dataclass(frozen=True)
class AskResearchScopeVerdict:
    allowed: bool
    route: str
    reason: str
    task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "route": self.route,
            "reason": self.reason,
            "task_id": self.task_id,
        }


@dataclass(frozen=True)
class PlannedResearchExecution:
    status: str
    plan_hash: str
    pending_task_ids: list[str]
    completed_task_ids: list[str]
    findings: list[dict[str, Any]]
    marker_path: str

    def to_result_text(self) -> str:
        if self.status == "already_complete":
            return (
                "Planned research workers already completed for this "
                f"research_plan.json hash ({len(self.completed_task_ids)} task(s)). "
                "Read _agent_state/research_findings/*.json and write "
                "_agent_state/research_synthesis.json before implementation."
            )
        task_list = ", ".join(self.pending_task_ids) if self.pending_task_ids else "(none)"
        return (
            "Planned research workers completed for "
            f"{len(self.findings)} task(s): {task_list}. Findings were written under "
            "_agent_state/research_findings/. Read them and synthesize durable "
            "provider understanding in _agent_state/research_synthesis.json before "
            "writing implementation_plan.json."
        )


def _load_json_object(content: str, *, artifact_name: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, [f"{artifact_name} must be valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, [f"{artifact_name} must be a JSON object"]
    return data, []


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_string_list(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, str) and item.strip() for item in value)


def _source_locator(source: dict[str, Any]) -> str:
    for key in ("url", "source_url", "artifact"):
        text = str(source.get(key) or "").strip()
        if text:
            return text
    return ""


def _surface_item_has_target(item: Any) -> bool:
    if isinstance(item, str):
        return bool(item.strip())
    if not isinstance(item, dict):
        return False
    for key in ("endpoint_url", "url", "route", "path", "sdk_method", "method_name"):
        if _nonempty_text(item.get(key)):
            return True
    return False


def _meaningful_value(value: Any) -> bool:
    if _nonempty_text(value):
        return True
    if _nonempty_string_list(value):
        return True
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return value is not None and str(value).strip() not in {"", "None"}


def _explicit_none(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {
            "none",
            "none documented",
            "not applicable",
            "n/a",
            "no sdk",
            "no published example",
        }
    if isinstance(value, dict):
        return any(
            bool(value.get(key))
            for key in (
                "none",
                "not_applicable",
                "not_applicable_reason",
                "no_sdk",
                "no_published_example",
            )
        )
    return False


def _has_structured_or_explicit_none(data: dict[str, Any], key: str) -> bool:
    if key not in data:
        return False
    value = data.get(key)
    if _explicit_none(value):
        return True
    if isinstance(value, dict):
        return bool(value) and any(_meaningful_value(v) for v in value.values())
    if isinstance(value, list):
        return bool(value) and any(_meaningful_value(item) for item in value)
    return _nonempty_text(value)


def _validate_build_brief(data: dict[str, Any], issues: list[str]) -> None:
    brief = data.get("build_brief")
    if not isinstance(brief, dict):
        issues.append("build_brief must be an object authored from research findings")
        return

    for field_name in (
        "endpoint_auth",
        "request_response_shape",
        "input_output_mapping",
        "state_continuity",
        "completion_signal",
        "errors_limits",
        "source_pointers",
    ):
        value = brief.get(field_name)
        if not (_explicit_none(value) or _meaningful_value(value)):
            issues.append(
                f"build_brief.{field_name} must be populated or explicitly marked none/not_applicable"
            )


def _has_any_meaningful_key(data: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(_meaningful_value(data.get(key)) for key in keys)


def sanitize_research_task_id(task_id: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", (task_id or "").strip()).strip("_")
    return (text or "task")[:80]


def research_plan_path(sandbox_dir: Path) -> Path:
    return sandbox_dir / RESEARCH_PLAN_RELATIVE_PATH


def research_findings_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / RESEARCH_FINDINGS_DIR_RELATIVE


def research_synthesis_path(sandbox_dir: Path) -> Path:
    return sandbox_dir / RESEARCH_SYNTHESIS_RELATIVE_PATH


def research_execution_marker_path(sandbox_dir: Path) -> Path:
    return sandbox_dir / RESEARCH_EXECUTION_MARKER_RELATIVE_PATH


def validate_research_plan_text(content: str) -> ResearchArtifactValidation:
    data, issues = _load_json_object(content, artifact_name="research_plan.json")
    if data is None:
        return ResearchArtifactValidation(False, issues)

    if data.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    if not _nonempty_text(data.get("docs_entrypoint")):
        issues.append("docs_entrypoint must name the official docs entrypoint or explain why none exists")
    if not _nonempty_text(data.get("objective_summary")):
        issues.append("objective_summary must summarize the candidate-specific objective")
    if not _nonempty_text(data.get("stop_condition")):
        issues.append("stop_condition must say when enough evidence exists to proceed")

    tasks = data.get("research_tasks")
    if not isinstance(tasks, list) or not tasks:
        issues.append("research_tasks must be a non-empty list")
        return ResearchArtifactValidation(False, issues)

    seen_ids: set[str] = set()
    for index, task in enumerate(tasks):
        label = f"research_tasks[{index}]"
        if not isinstance(task, dict):
            issues.append(f"{label} must be an object")
            continue
        task_id = task.get("id")
        if not _nonempty_text(task_id):
            issues.append(f"{label}.id must be non-empty")
        else:
            normalized_id = str(task_id).strip()
            if normalized_id in seen_ids:
                issues.append(f"{label}.id duplicates {normalized_id!r}")
            seen_ids.add(normalized_id)
        if not _nonempty_text(task.get("question")):
            issues.append(f"{label}.question must be non-empty")
        if not _nonempty_string_list(task.get("where_to_look")):
            issues.append(f"{label}.where_to_look must include at least one source hint")
        if not _nonempty_string_list(task.get("evidence_required")):
            issues.append(f"{label}.evidence_required must include required evidence")
        if not _nonempty_text(task.get("why_needed_for_build")):
            issues.append(f"{label}.why_needed_for_build must be non-empty")

    return ResearchArtifactValidation(not issues, issues, task_count=len(tasks))


def validate_research_finding_text(
    content: str,
    *,
    official_domain: str | None = None,
) -> ResearchArtifactValidation:
    data, issues = _load_json_object(content, artifact_name="research_findings/*.json")
    if data is None:
        return ResearchArtifactValidation(False, issues)

    if data.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    if not _nonempty_text(data.get("task_id")):
        issues.append("task_id must be non-empty")
    if not _nonempty_text(data.get("answer")):
        issues.append("answer must be non-empty")
    confidence = data.get("confidence")
    if confidence not in ALLOWED_CONFIDENCE:
        issues.append(f"confidence must be one of {sorted(ALLOWED_CONFIDENCE)}")

    sources = data.get("sources")
    if sources is None:
        sources = []
    if not isinstance(sources, list):
        issues.append("sources must be a list")
        sources = []

    citation_count = 0
    official_hits = 0
    domain = (official_domain or "").strip().lower()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            issues.append(f"sources[{index}] must be an object")
            continue
        locator = _source_locator(source)
        claim = str(source.get("claim") or "").strip()
        if not locator:
            issues.append(f"sources[{index}] must include non-empty url or artifact")
        if not claim:
            issues.append(f"sources[{index}].claim must be non-empty")
        if locator and claim:
            citation_count += 1
            if domain and locator.startswith(("http://", "https://")) and domain in locator.lower():
                official_hits += 1

    if confidence != "not_found" and citation_count == 0:
        issues.append("findings with confidence other than not_found must include citations")
    if domain and confidence != "not_found" and official_hits == 0:
        issues.append(f"findings must cite official docs domain {domain!r} when available")

    for key in ("blocked_or_dead_urls", "unresolved_questions", "notes_for_builder"):
        if key in data and not isinstance(data.get(key), list):
            issues.append(f"{key} must be a list")

    return ResearchArtifactValidation(not issues, issues, citation_count=citation_count)


def validate_research_synthesis_text(content: str) -> ResearchArtifactValidation:
    data, issues = _load_json_object(content, artifact_name="research_synthesis.json")
    if data is None:
        return ResearchArtifactValidation(False, issues)

    if data.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    if not _nonempty_string_list(data.get("findings_used")):
        issues.append("findings_used must reference at least one research finding")
    if not isinstance(data.get("facts"), list) or not data.get("facts"):
        issues.append("facts must be a non-empty list of cited build facts")
    if not isinstance(data.get("assumptions", []), list):
        issues.append("assumptions must be a list")
    if not isinstance(data.get("open_risks", []), list):
        issues.append("open_risks must be a list")
    if not isinstance(data.get("proceed_to_implementation_plan"), bool):
        issues.append("proceed_to_implementation_plan must be a boolean")

    citation_count = 0
    for index, fact in enumerate(data.get("facts") or []):
        if not isinstance(fact, dict):
            issues.append(f"facts[{index}] must be an object")
            continue
        if not _nonempty_text(fact.get("claim")):
            issues.append(f"facts[{index}].claim must be non-empty")
        if not _nonempty_text(fact.get("source")):
            issues.append(f"facts[{index}].source must cite a finding or URL")
        else:
            citation_count += 1

    doc_map = data.get("provider_doc_map")
    if not isinstance(doc_map, list) or not doc_map:
        issues.append("provider_doc_map must be a non-empty list of researched docs/topics")
    else:
        for index, entry in enumerate(doc_map):
            label = f"provider_doc_map[{index}]"
            if not isinstance(entry, dict):
                issues.append(f"{label} must be an object")
                continue
            status = str(entry.get("status") or "").strip()
            if not _nonempty_text(entry.get("topic")):
                issues.append(f"{label}.topic must be non-empty")
            if status not in ALLOWED_DOC_MAP_STATUSES:
                issues.append(f"{label}.status must be one of {sorted(ALLOWED_DOC_MAP_STATUSES)}")
            locator = _source_locator(entry)
            if status in {"current", "alternate"} and not locator:
                issues.append(f"{label} current/alternate entries must cite url or artifact")
            if status in {"current", "alternate"} and not _nonempty_string_list(entry.get("facts")):
                issues.append(f"{label}.facts must include researched facts for current docs")
            if status in {"current", "alternate"} and not _nonempty_string_list(entry.get("used_for")):
                issues.append(f"{label}.used_for must say how the docs affect the harness")
            if locator:
                citation_count += 1

    chosen_surface = data.get("chosen_api_surface")
    surface_items = chosen_surface if isinstance(chosen_surface, list) else [chosen_surface]
    if not any(_surface_item_has_target(item) for item in surface_items):
        issues.append("chosen_api_surface must name an endpoint URL, route, path, or SDK method")

    credential_model = data.get("credential_model")
    if not isinstance(credential_model, dict):
        issues.append("credential_model must be an object")
    elif not (
        _nonempty_string_list(credential_model.get("env_vars"))
        or _has_any_meaningful_key(
            credential_model,
            ("auth_method", "header", "token_flow", "session_flow", "credential_source"),
        )
    ):
        issues.append("credential_model must name env_vars or the auth/session flow")

    request_response = data.get("request_response_contract")
    if not isinstance(request_response, dict):
        issues.append("request_response_contract must be an object")
    else:
        has_request = _has_any_meaningful_key(
            request_response,
            ("request_body", "request_schema", "request_fields", "input_mapping", "payload", "parameters"),
        )
        has_response = _has_any_meaningful_key(
            request_response,
            ("response_body", "response_schema", "response_fields", "output_mapping", "completion_signal"),
        )
        if not has_request:
            issues.append("request_response_contract must describe request shape or input mapping")
        if not has_response:
            issues.append("request_response_contract must describe response shape or output mapping")

    for key in ("interaction_constraints", "dead_or_deprecated_docs", "unresolved_questions"):
        if not isinstance(data.get(key), list):
            issues.append(f"{key} must be a list")

    for key in (
        "input_compatibility",
        "routing_table",
        "working_examples",
        "errors_and_limits",
        "sdk_package",
    ):
        if not _has_structured_or_explicit_none(data, key):
            issues.append(
                f"{key} must be populated in research_synthesis.json or explicitly marked none/not_applicable"
            )

    _validate_build_brief(data, issues)

    plan_facts = data.get("facts_used_for_implementation_plan")
    if not isinstance(plan_facts, list) or not plan_facts:
        issues.append("facts_used_for_implementation_plan must be a non-empty list")
    else:
        for index, fact in enumerate(plan_facts):
            label = f"facts_used_for_implementation_plan[{index}]"
            if not isinstance(fact, dict):
                issues.append(f"{label} must be an object")
                continue
            if not (_nonempty_text(fact.get("claim")) or _nonempty_text(fact.get("fact"))):
                issues.append(f"{label}.claim must be non-empty")
            if not _nonempty_text(fact.get("source")):
                issues.append(f"{label}.source must cite a finding or URL")
            else:
                citation_count += 1
            if not (_nonempty_text(fact.get("plan_field")) or _nonempty_text(fact.get("used_for"))):
                issues.append(f"{label}.plan_field or used_for must name the implementation-plan field it supports")

    return ResearchArtifactValidation(not issues, issues, citation_count=citation_count)


def _research_tier(answer: str) -> str:
    for line in (answer or "").splitlines():
        stripped = line.strip().lstrip("`").lstrip("*").strip().upper()
        if stripped.startswith("REASONABLE_GUESS"):
            return "REASONABLE_GUESS"
        if stripped.startswith("NOT_FOUND"):
            return "NOT_FOUND"
        if stripped.startswith("ANSWER"):
            return "ANSWER"
    return "UNKNOWN"


def _extract_source_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _SOURCE_URL_RE.findall(text or ""):
        url = match.rstrip(".,;:]")
        if url not in urls:
            urls.append(url)
    return urls


def research_finding_from_answer(
    *,
    task: dict[str, Any],
    question: str,
    answer: str,
    source: str,
    turn: int | None = None,
) -> dict[str, Any]:
    task_id = sanitize_research_task_id(str(task.get("id") or task.get("task_id") or "task"))
    tier = _research_tier(answer)
    urls = _extract_source_urls(answer)
    sources = [
        {"url": url, "claim": "Source cited by research answer."}
        for url in urls[:8]
    ]
    if not sources:
        sources.append({
            "artifact": f"research_{task_id}.txt",
            "claim": "Uncited research answer preserved for synthesis review.",
        })

    confidence = "medium"
    if tier == "ANSWER":
        confidence = "high" if urls else "low"
    elif tier == "REASONABLE_GUESS":
        confidence = "low"
    elif tier == "NOT_FOUND":
        confidence = "not_found"

    finding: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "question": str(question or task.get("question") or "").strip(),
        "answer": str(answer or "").strip(),
        "confidence": confidence,
        "sources": sources,
        "blocked_or_dead_urls": [],
        "unresolved_questions": [str(question or task.get("question") or "").strip()] if tier == "NOT_FOUND" else [],
        "notes_for_builder": [
            "Promote only cited facts or explicit assumptions into research_synthesis.json."
        ],
        "source": source,
    }
    if turn is not None:
        finding["turn"] = turn
    return finding


def record_inline_research_finding(
    sandbox_dir: Path,
    *,
    question: str,
    answer: str,
    turn: int,
) -> Path | None:
    """Persist an in-loop ``ask_research`` answer as structured evidence.

    Planned workers already write ``research_findings/*.json``. This helper
    keeps later debug research out of the ephemeral context window by mirroring
    it into the same evidence lane, while leaving Agent 5 responsible for
    promoting it into ``research_synthesis.json``.
    """

    if not str(answer or "").strip():
        return None
    directory = research_findings_dir(sandbox_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    task_id = sanitize_research_task_id(f"ask_research_turn_{turn}")
    finding = research_finding_from_answer(
        task={"id": task_id},
        question=question,
        answer=answer,
        source="ask_research",
        turn=turn,
    )
    debug_gap_key = _debug_gap_key_from_question(question)
    if debug_gap_key:
        finding["debug_gap_key"] = debug_gap_key
    if finding.get("sources") and isinstance(finding["sources"][0], dict):
        if "artifact" in finding["sources"][0]:
            finding["sources"][0]["artifact"] = f"research_turn{turn}.txt"
    finding["notes_for_builder"] = [
        "Inline ask_research result; promote only cited facts or explicit assumptions into research_synthesis.json."
    ]
    finding["written_t_abs"] = time.time()
    validation = validate_research_finding_text(json.dumps(finding, ensure_ascii=False, default=str))
    finding["validation"] = validation.to_dict()

    path = directory / f"{task_id}.json"
    try:
        path.write_text(
            json.dumps(finding, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        write_research_findings_index(sandbox_dir)
        write_research_build_brief(sandbox_dir)
    except OSError:
        return None
    return path


def read_research_plan(sandbox_dir: Path) -> dict[str, Any] | None:
    path = research_plan_path(sandbox_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_research_findings(sandbox_dir: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    directory = research_findings_dir(sandbox_dir)
    if not directory.exists():
        return findings
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data.setdefault("path", str(Path(RESEARCH_FINDINGS_DIR_RELATIVE) / path.name).replace("\\", "/"))
            findings.append(data)
    return findings


def research_findings_index_path(sandbox_dir: Path) -> Path:
    return sandbox_dir / RESEARCH_FINDINGS_INDEX_RELATIVE_PATH


def research_build_brief_path(sandbox_dir: Path) -> Path:
    return sandbox_dir / RESEARCH_BUILD_BRIEF_RELATIVE_PATH


def _finding_index_item(finding: dict[str, Any]) -> dict[str, Any]:
    sources = finding.get("sources") if isinstance(finding.get("sources"), list) else []
    source_refs: list[str] = []
    for source in sources[:5]:
        if not isinstance(source, dict):
            continue
        locator = _source_locator(source)
        if locator:
            source_refs.append(locator)
    return {
        "task_id": finding.get("task_id"),
        "path": finding.get("path"),
        "question": str(finding.get("question") or "")[:500],
        "direct_answer": str(finding.get("answer") or "")[:1200],
        "confidence": finding.get("confidence"),
        "sources": source_refs,
        "unresolved_questions": finding.get("unresolved_questions") or [],
        "notes_for_builder": finding.get("notes_for_builder") or [],
    }


def write_research_findings_index(sandbox_dir: Path) -> Path:
    """Write a compact synthesis-ready index over durable findings."""

    findings = list_research_findings(sandbox_dir)
    payload = {
        "schema_version": 1,
        "written_t_abs": time.time(),
        "finding_count": len(findings),
        "findings": [_finding_index_item(finding) for finding in findings],
        "guidance": (
            "Use this index for synthesis triage; read full finding JSON only "
            "when a listed answer/source is insufficient."
        ),
    }
    path = research_findings_index_path(sandbox_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path


def _brief_item_from_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Return only implementation-useful facts from a full finding."""

    index_item = _finding_index_item(finding)
    categories = {
        "endpoint_auth": [],
        "request_response_shape": [],
        "input_output_mapping": [],
        "state_or_continuity": [],
        "stream_or_completion_signal": [],
        "errors_limits": [],
    }
    for line in str(finding.get("answer") or "").splitlines():
        text = line.strip()
        if not text:
            continue
        lower = text.lower()
        bucket = None
        if any(token in lower for token in ("endpoint", "base url", "auth", "api key", "bearer", "header")):
            bucket = "endpoint_auth"
        elif any(token in lower for token in ("stream", "websocket", "sse", "event", "done", "completion")):
            bucket = "stream_or_completion_signal"
        elif any(token in lower for token in ("request", "response", "json", "schema", "field", "content-type")):
            bucket = "request_response_shape"
        elif any(token in lower for token in ("input", "output", "audio", "file", "multipart", "mapping")):
            bucket = "input_output_mapping"
        elif any(token in lower for token in ("session", "conversation", "state", "history", "context")):
            bucket = "state_or_continuity"
        elif any(token in lower for token in ("error", "limit", "quota", "rate", "timeout", "close code")):
            bucket = "errors_limits"
        if bucket and len(categories[bucket]) < 4:
            categories[bucket].append(text[:350])
    return {
        "task_id": index_item.get("task_id"),
        "path": index_item.get("path"),
        "direct_answer": index_item.get("direct_answer"),
        "implementation_fact_buckets": categories,
        "confidence": index_item.get("confidence"),
        "sources": index_item.get("sources"),
        "unresolved_questions": index_item.get("unresolved_questions"),
        "notes_for_builder": index_item.get("notes_for_builder"),
    }


def write_research_build_brief(sandbox_dir: Path) -> Path:
    """Write an action-ready implementation brief over research findings."""

    findings = list_research_findings(sandbox_dir)
    payload = {
        "schema_version": 1,
        "written_t_abs": time.time(),
        "finding_count": len(findings),
        "brief": [_brief_item_from_finding(finding) for finding in findings],
        "guidance": (
            "Start synthesis from this brief. Open full research_findings JSON "
            "only when a direct answer/source here is insufficient for a "
            "specific implementation decision."
        ),
    }
    path = research_build_brief_path(sandbox_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path


def _plan_hash(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _task_hash(task: dict[str, Any]) -> str:
    canonical = json.dumps(task, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _valid_completed_task_ids(sandbox_dir: Path) -> set[str]:
    completed: set[str] = set()
    for finding in list_research_findings(sandbox_dir):
        task_id = sanitize_research_task_id(str(finding.get("task_id") or ""))
        validation = finding.get("validation")
        if isinstance(validation, dict) and validation.get("ok") is False:
            continue
        if task_id:
            completed.add(task_id)
    return completed


def _debug_gap_key_from_question(question: str) -> str:
    """Return a stable key for unplanned debug-gap research requests."""

    match = re.search(r"(?im)^\s*FIELD NEEDED\s*:\s*(.+?)\s*$", question or "")
    if match:
        key_text = match.group(1)
    else:
        key_text = str(question or "")[:300]
    return re.sub(r"\s+", " ", key_text.strip().lower())


def _completed_debug_gap_keys(sandbox_dir: Path) -> set[str]:
    keys: set[str] = set()
    for finding in list_research_findings(sandbox_dir):
        if str(finding.get("source") or "") != "ask_research":
            continue
        validation = finding.get("validation")
        if isinstance(validation, dict) and validation.get("ok") is False:
            continue
        key = str(finding.get("debug_gap_key") or "").strip()
        if not key:
            key = _debug_gap_key_from_question(str(finding.get("question") or ""))
        if key:
            keys.add(key)
    return keys


def validate_ask_research_scope(
    sandbox_dir: Path,
    *,
    question: str,
    task_id: str | None = None,
    failure_packet_research_allowed: bool = False,
) -> AskResearchScopeVerdict:
    """Return whether an ask_research call is scoped enough to run.

    The default path should use planned research workers when several
    independent gaps are known. This verifier keeps ad-hoc ask_research useful
    for explicit planned task IDs or concrete debug gaps, while blocking broad
    provider-discovery questions.
    """

    if failure_packet_research_allowed:
        return AskResearchScopeVerdict(
            True,
            "failure_packet",
            "latest failure packet diagnosis says scoped research would change implementation",
            None,
        )

    has_field_needed = bool(re.search(r"(?im)^\s*FIELD NEEDED\s*:", question or ""))
    has_why = bool(re.search(r"(?im)^\s*WHY\s*:", question or ""))
    if has_field_needed and has_why:
        debug_gap_key = _debug_gap_key_from_question(question or "")
        if debug_gap_key and debug_gap_key in _completed_debug_gap_keys(sandbox_dir):
            return AskResearchScopeVerdict(
                False,
                "debug_gap_already_researched",
                f"debug gap {debug_gap_key!r} already has an ask_research finding",
                None,
            )
        return AskResearchScopeVerdict(
            True,
            "unplanned_debug_gap",
            "question declares FIELD NEEDED and WHY; record result as an unplanned debug gap",
            None,
        )

    plan = read_research_plan(sandbox_dir)
    requested_task_id = (
        sanitize_research_task_id(str(task_id))
        if str(task_id or "").strip()
        else ""
    )
    if not requested_task_id:
        match = re.search(r"\b(?:task_id|research_task)\s*[:=]\s*([A-Za-z0-9_-]+)", question or "", re.I)
        if match:
            requested_task_id = sanitize_research_task_id(match.group(1))

    if requested_task_id:
        if not plan:
            return AskResearchScopeVerdict(
                False,
                "missing_research_plan",
                "_agent_state/research_plan.json does not exist for requested task_id",
                requested_task_id,
            )
        plan_tasks = plan.get("research_tasks") if isinstance(plan, dict) else []
        tasks = [task for task in (plan_tasks or []) if isinstance(task, dict)]
        task_by_id = {
            sanitize_research_task_id(str(task.get("id") or "")): task
            for task in tasks
            if str(task.get("id") or "").strip()
        }
        completed = _valid_completed_task_ids(sandbox_dir)

        if requested_task_id not in task_by_id:
            return AskResearchScopeVerdict(
                False,
                "unknown_task_id",
                f"task_id {requested_task_id!r} is not declared in research_plan.json",
                requested_task_id,
            )
        if requested_task_id in completed:
            return AskResearchScopeVerdict(
                False,
                "planned_task_already_completed",
                f"research task {requested_task_id!r} already has a finding",
                requested_task_id,
            )
        return AskResearchScopeVerdict(
            True,
            "planned_task",
            f"research task {requested_task_id!r} is declared and unresolved",
            requested_task_id,
        )

    return AskResearchScopeVerdict(
        False,
        "unscoped_question",
        "ask_research must name a declared task_id or a concrete FIELD NEEDED/WHY debug gap",
        None,
    )


def _terminal_url_set(sandbox_dir: Path) -> set[str]:
    try:
        from puzzleeval.agents.agent5.research_memory import read_terminal_research_urls

        memory = read_terminal_research_urls(sandbox_dir)
    except Exception:  # noqa: BLE001 - memory is advisory
        return set()
    urls = memory.get("urls") if isinstance(memory, dict) else {}
    if not isinstance(urls, dict):
        return set()
    return {str(url).rstrip("/") for url in urls}


def _active_where_to_look(items: Iterable[Any], terminal_urls: set[str]) -> list[str]:
    active: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        if text.rstrip("/") in terminal_urls:
            continue
        active.append(text)
    return active


ResearchWorker = Callable[[dict[str, Any]], dict[str, Any]]


def run_research_batch(
    sandbox_dir: Path,
    researcher: ResearchWorker,
    *,
    max_workers: int = 4,
    task_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Run planned research tasks in parallel using ``researcher``.

    The callable is injected so unit tests and production callers can use
    different research engines. The runner owns fan-out, terminal-URL
    suppression, finding-file writes, and validation of returned findings.
    """

    plan = read_research_plan(sandbox_dir)
    if not plan:
        raise FileNotFoundError(RESEARCH_PLAN_RELATIVE_PATH)
    tasks = plan.get("research_tasks") or []
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("research_plan.json has no research_tasks")

    terminal_urls = _terminal_url_set(sandbox_dir)
    directory = research_findings_dir(sandbox_dir)
    directory.mkdir(parents=True, exist_ok=True)

    official_domain = ""
    docs_entrypoint = str(plan.get("docs_entrypoint") or "")
    if "://" in docs_entrypoint:
        official_domain = docs_entrypoint.split("://", 1)[1].split("/", 1)[0]

    prepared_tasks: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        normalized_task_id = sanitize_research_task_id(str(task.get("id") or "task"))
        if task_ids is not None and normalized_task_id not in task_ids:
            continue
        prepared = dict(task)
        prepared["where_to_look"] = _active_where_to_look(
            prepared.get("where_to_look") or [],
            terminal_urls,
        )
        prepared_tasks.append(prepared)

    results: list[dict[str, Any]] = []
    if not prepared_tasks:
        write_research_findings_index(sandbox_dir)
        write_research_build_brief(sandbox_dir)
        return results
    worker_count = max(1, min(max_workers, len(prepared_tasks)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_task = {executor.submit(researcher, task): task for task in prepared_tasks}
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            task_id = sanitize_research_task_id(str(task.get("id") or "task"))
            finding = future.result()
            if not isinstance(finding, dict):
                finding = {
                    "schema_version": 1,
                    "task_id": task_id,
                    "answer": str(finding),
                    "confidence": "low",
                    "sources": [],
                    "blocked_or_dead_urls": [],
                    "unresolved_questions": [],
                    "notes_for_builder": ["Researcher returned a non-object result."],
                }
            finding.setdefault("schema_version", 1)
            # The runner owns task identity. A worker may return a generic
            # object or accidentally echo a stale id; persisting that mismatch
            # would make completion markers drift and can rerun the same task.
            finding["task_id"] = task_id
            finding.setdefault("blocked_or_dead_urls", [])
            finding.setdefault("unresolved_questions", [])
            finding.setdefault("notes_for_builder", [])
            validation = validate_research_finding_text(
                json.dumps(finding, ensure_ascii=False, default=str),
                official_domain=official_domain or None,
            )
            finding["validation"] = validation.to_dict()
            if not validation.ok:
                raise ValueError(
                    f"research finding {task_id!r} failed validation: "
                    + "; ".join(validation.issues)
                )
            finding["written_t_abs"] = time.time()
            (directory / f"{task_id}.json").write_text(
                json.dumps(finding, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            results.append(finding)

    write_research_findings_index(sandbox_dir)
    write_research_build_brief(sandbox_dir)
    return sorted(results, key=lambda item: str(item.get("task_id") or ""))


def run_research_batch_once(
    sandbox_dir: Path,
    researcher: ResearchWorker,
    *,
    max_workers: int = 4,
) -> PlannedResearchExecution:
    """Run only the planned research tasks that have not completed for this plan.

    A marker file records the plan hash and per-task hashes so unchanged plans
    do not keep spawning research workers every time the builder retries the
    same write. If a task changes under the same ID, that task runs again.
    """

    plan = read_research_plan(sandbox_dir)
    if not plan:
        raise FileNotFoundError(RESEARCH_PLAN_RELATIVE_PATH)
    plan_hash = _plan_hash(plan)
    tasks = [task for task in (plan.get("research_tasks") or []) if isinstance(task, dict)]
    task_hashes = {
        sanitize_research_task_id(str(task.get("id") or "task")): _task_hash(task)
        for task in tasks
    }
    completed = _valid_completed_task_ids(sandbox_dir)
    marker_path = research_execution_marker_path(sandbox_dir)
    marker = _read_json_object(marker_path) or {}
    previous_hashes = marker.get("task_hashes") if isinstance(marker.get("task_hashes"), dict) else {}

    pending_ids = [
        task_id for task_id, task_hash in task_hashes.items()
        if task_id not in completed or previous_hashes.get(task_id) != task_hash
    ]
    if not pending_ids:
        return PlannedResearchExecution(
            status="already_complete",
            plan_hash=plan_hash,
            pending_task_ids=[],
            completed_task_ids=sorted(completed),
            findings=[],
            marker_path=RESEARCH_EXECUTION_MARKER_RELATIVE_PATH,
        )

    findings = run_research_batch(
        sandbox_dir,
        researcher,
        max_workers=max_workers,
        task_ids=set(pending_ids),
    )
    completed_after = _valid_completed_task_ids(sandbox_dir)
    marker_payload = {
        "schema_version": 1,
        "plan_hash": plan_hash,
        "task_hashes": task_hashes,
        "completed_task_ids": sorted(completed_after),
        "last_pending_task_ids": pending_ids,
        "last_run_t_abs": time.time(),
    }
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(marker_payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return PlannedResearchExecution(
        status="ran",
        plan_hash=plan_hash,
        pending_task_ids=pending_ids,
        completed_task_ids=sorted(completed_after),
        findings=findings,
        marker_path=RESEARCH_EXECUTION_MARKER_RELATIVE_PATH,
    )


__all__ = [
    "AskResearchScopeVerdict",
    "RESEARCH_BUILD_BRIEF_RELATIVE_PATH",
    "RESEARCH_FINDINGS_DIR_RELATIVE",
    "RESEARCH_FINDINGS_INDEX_RELATIVE_PATH",
    "RESEARCH_EXECUTION_MARKER_RELATIVE_PATH",
    "RESEARCH_PLAN_RELATIVE_PATH",
    "RESEARCH_SYNTHESIS_RELATIVE_PATH",
    "PlannedResearchExecution",
    "ResearchArtifactValidation",
    "list_research_findings",
    "read_research_plan",
    "research_execution_marker_path",
    "research_build_brief_path",
    "research_findings_index_path",
    "research_findings_dir",
    "research_plan_path",
    "research_synthesis_path",
    "research_finding_from_answer",
    "record_inline_research_finding",
    "run_research_batch",
    "run_research_batch_once",
    "sanitize_research_task_id",
    "validate_ask_research_scope",
    "validate_research_finding_text",
    "validate_research_plan_text",
    "validate_research_synthesis_text",
    "write_research_findings_index",
    "write_research_build_brief",
]
