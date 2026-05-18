"""Implementation-plan validation for ``implementation_plan.json``.

The objective remains orchestrator-owned in ``objective.md``. Agent 5 can
interpret it in ``implementation_plan.json``, but the plan must not drop or
weaken system-defined success criteria. Phase 4 extends the same gate with
required build-readiness fields; the accepted implementation plan is the active
build boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


IMPLEMENTATION_PLAN_RELATIVE_PATH = "_agent_state/implementation_plan.json"

_WEAKENING_STATUSES = {
    "dropped",
    "ignore",
    "ignored",
    "omit",
    "omitted",
    "out_of_scope",
    "remove",
    "removed",
    "skip",
    "skipped",
    "weaken",
    "weakened",
}


@dataclass(frozen=True)
class ObjectiveCriterion:
    criterion_id: str
    text: str


@dataclass(frozen=True)
class ObjectiveCoverageValidation:
    ok: bool
    issues: list[str] = field(default_factory=list)
    required_count: int = 0
    covered_count: int = 0
    criteria_results: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": list(self.issues),
            "required_count": self.required_count,
            "covered_count": self.covered_count,
            "criteria_results": dict(self.criteria_results),
        }


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _success_criteria_section(objective_md: str) -> str:
    marker = "## SUCCESS CRITERIA"
    start = objective_md.find(marker)
    if start == -1:
        return ""
    rest = objective_md[start + len(marker):]
    next_section = rest.find("\n## ")
    return rest[:next_section] if next_section != -1 else rest


def _clean_criterion_line(line: str) -> str:
    text = line.strip()
    text = re.sub(r"^- \[ \]\s*", "", text)
    text = re.sub(r"^-\s*", "", text)
    text = re.sub(r"^[-*]\s*", "", text)
    text = re.sub(r"^\*\*([^*]+)\*\*:\s*", r"\1: ", text)
    return text.strip()


def extract_objective_success_criteria(objective_md: str) -> list[str]:
    """Extract top-level objective success criteria from ``objective.md``."""

    return [criterion.text for criterion in extract_objective_success_criteria_with_ids(objective_md)]


def extract_objective_success_criteria_with_ids(objective_md: str) -> list[ObjectiveCriterion]:
    """Extract top-level objective criteria with stable IDs.

    IDs are deterministic within ``objective.md`` and intentionally independent
    from the criterion wording. Agent 5 can reference ``OBJ-1``/``OBJ-2``
    without copying exact text, while the objective remains orchestrator-owned.
    """

    section = _success_criteria_section(objective_md)
    criteria: list[ObjectiveCriterion] = []
    for raw_line in section.splitlines():
        if not raw_line.startswith("- [ ] "):
            continue
        cleaned = _clean_criterion_line(raw_line)
        if cleaned:
            criteria.append(ObjectiveCriterion(f"OBJ-{len(criteria) + 1}", cleaned))
    return criteria


def _coverage_entries(plan: dict[str, Any]) -> list[dict[str, Any]]:
    coverage = plan.get("objective_coverage")
    if not isinstance(coverage, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in coverage:
        if isinstance(item, dict):
            entries.append(item)
        elif isinstance(item, str):
            entries.append({"objective_ref": item, "status": "covered"})
    return entries


def _entry_text(entry: dict[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "objective_ref",
        "criterion",
        "success_criterion",
        "plan",
        "approach",
        "evidence",
        "notes",
    ):
        value = entry.get(key)
        if isinstance(value, list):
            values.extend(str(v) for v in value)
        elif value is not None:
            values.append(str(value))
    return "\n".join(values)


def _normalize_criterion_id(value: Any) -> str:
    text = str(value or "").strip().upper().replace("_", "-")
    match = re.fullmatch(r"(?:OBJ-?)?(\d+)", text)
    if match:
        return f"OBJ-{int(match.group(1))}"
    return text


def _entry_criterion_ids(entry: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("criterion_id", "objective_id", "objective_ref_id", "id"):
        value = entry.get(key)
        if value is not None:
            ids.add(_normalize_criterion_id(value))
    covers = entry.get("covers") or entry.get("criteria") or entry.get("criterion_ids")
    if isinstance(covers, list):
        ids.update(_normalize_criterion_id(item) for item in covers)
    elif covers is not None:
        ids.add(_normalize_criterion_id(covers))
    ids.discard("")
    return ids


def _entry_has_substance(entry: dict[str, Any]) -> bool:
    for key in ("approach", "plan", "evidence", "notes", "objective_ref", "criterion", "success_criterion"):
        value = entry.get(key)
        if isinstance(value, list):
            text = " ".join(str(v) for v in value)
        elif value is None:
            text = ""
        else:
            text = str(value)
        normalized = _normalize(text)
        if normalized and normalized not in {"n a", "na", "none", "todo", "tbd", "unknown", "placeholder"}:
            return True
    return False


def _entry_is_weakening(entry: dict[str, Any]) -> bool:
    for key in ("status", "decision", "coverage_status"):
        status = _normalize(str(entry.get(key, ""))).replace(" ", "_")
        if status in _WEAKENING_STATUSES:
            return True
    text = _normalize(_entry_text(entry)).replace(" ", "_")
    return any(token in text for token in _WEAKENING_STATUSES)


def _criterion_is_covered(criterion: str, entries: list[dict[str, Any]]) -> bool:
    criterion_norm = _normalize(criterion)
    if not criterion_norm:
        return False

    for entry in entries:
        text_norm = _normalize(_entry_text(entry))
        if not text_norm:
            continue
        if criterion_norm in text_norm or text_norm in criterion_norm:
            return True
        # A stable prefix is enough for long criteria where the plan quotes a
        # concise objective_ref, e.g. "harness.py exposes run(input_data)".
        prefix = " ".join(criterion_norm.split()[:5])
        if prefix and prefix in text_norm:
            return True
    return False


def _objective_criterion_is_covered(
    criterion: ObjectiveCriterion,
    entries: list[dict[str, Any]],
) -> bool:
    for entry in entries:
        if _entry_is_weakening(entry) or not _entry_has_substance(entry):
            continue
        if criterion.criterion_id in _entry_criterion_ids(entry):
            return True
        # Compatibility window: existing plans/tests may still quote objective
        # text. Keep this as fallback only; new plans should use criterion IDs.
        if _criterion_is_covered(criterion.text, [entry]):
            return True
    return False


def _criterion_preview(criterion: ObjectiveCriterion) -> str:
    text = " ".join(criterion.text.split())
    if len(text) > 90:
        text = text[:87].rstrip() + "..."
    return f"{criterion.criterion_id} {text}"


_PLACEHOLDER_TEXT = {
    "",
    "n/a",
    "na",
    "none",
    "not applicable",
    "placeholder",
    "tbd",
    "todo",
    "unknown",
    "unsure",
    "to be determined",
    "fill in later",
}

_KNOWN_INTERACTION_FAMILIES = {
    "request_response",
    "async_job",
    "webhook",
    "serialized_conversation",
    "persistent_session",
    "continuous_stream",
    "browser_session",
    "file_batch",
    "other",
}


def _flatten_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        pieces: list[str] = []
        for key, item in value.items():
            pieces.append(str(key))
            pieces.extend(_flatten_text(item))
        return pieces
    if isinstance(value, list):
        pieces = []
        for item in value:
            pieces.extend(_flatten_text(item))
        return pieces
    if value is None:
        return []
    return [str(value)]


def _meaningful_text(value: Any) -> bool:
    text = _normalize(" ".join(_flatten_text(value)))
    return text not in _PLACEHOLDER_TEXT


def _surface_item_has_callable_target(item: Any) -> bool:
    if isinstance(item, dict):
        target_keys = {
            "endpoint",
            "endpoint_url",
            "function",
            "method",
            "path",
            "route",
            "sdk_method",
            "url",
        }
        return any(_meaningful_text(item.get(key)) for key in target_keys)
    if isinstance(item, str):
        text = item.strip()
        if not _meaningful_text(text):
            return False
        return any(marker in text for marker in ("http://", "https://", "/", ".", "::", "("))
    return False


def _credential_env_vars(value: Any) -> list[str]:
    if isinstance(value, dict):
        raw = value.get("env_vars")
        if raw is None:
            return [str(k) for k in value.keys() if _meaningful_text(k)]
    else:
        raw = None
    if isinstance(raw, dict):
        return [str(k) for k, v in raw.items() if _meaningful_text(k) and _meaningful_text(v)]
    if isinstance(raw, list):
        names: list[str] = []
        for item in raw:
            if isinstance(item, str) and _meaningful_text(item):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("env_var") or item.get("key")
                if _meaningful_text(name):
                    names.append(str(name))
        return names
    if isinstance(raw, str) and _meaningful_text(raw):
        return [raw]
    return []


def _validate_required_plan_fields(plan: dict[str, Any], issues: list[str]) -> dict[str, str]:
    """Validate the Phase 4 build-gate criteria beyond objective coverage."""

    results: dict[str, str] = {}

    chosen_api_surface = plan.get("chosen_api_surface")
    if isinstance(chosen_api_surface, list):
        has_surface = bool(chosen_api_surface) and any(
            _surface_item_has_callable_target(item) for item in chosen_api_surface
        )
    else:
        has_surface = _surface_item_has_callable_target(chosen_api_surface)
    if has_surface:
        results["chosen_api_surface"] = "passed"
    else:
        results["chosen_api_surface"] = "failed"
        issues.append(
            "chosen_api_surface must name at least one endpoint URL, route, method, or SDK method"
        )

    credential_loading = plan.get("credential_loading")
    env_vars = _credential_env_vars(credential_loading)
    if env_vars:
        results["credential_loading"] = "passed"
    else:
        results["credential_loading"] = "failed"
        issues.append("credential_loading.env_vars must list the staged credential env vars")

    interaction_pattern = plan.get("interaction_pattern")
    if not isinstance(interaction_pattern, dict):
        interaction_pattern = {}
        issues.append("interaction_pattern must be an object")

    family = _normalize(str(interaction_pattern.get("known_family", ""))).replace(" ", "_")
    if family in _KNOWN_INTERACTION_FAMILIES:
        results["interaction_pattern.known_family"] = "passed"
    else:
        results["interaction_pattern.known_family"] = "failed"
        issues.append(
            "interaction_pattern.known_family must be one of the supported families or 'other'"
        )

    state_owner_norm = _normalize(str(interaction_pattern.get("state_owner", ""))).replace(" ", "_")
    if state_owner_norm and state_owner_norm not in {"provider_server", "harness_process"}:
        issues.append(
            "interaction_pattern.state_owner must be 'provider_server' or 'harness_process'"
        )

    for field_name in ("state_owner", "input_clocking", "output_completion_signal"):
        if not _meaningful_text(interaction_pattern.get(field_name)):
            issues.append(f"interaction_pattern.{field_name} must be non-placeholder")

    if family == "other" and not _meaningful_text(interaction_pattern.get("why_this_pattern")):
        issues.append(
            "interaction_pattern.why_this_pattern must explain the provider-specific shape when known_family is 'other'"
        )

    if all(
        _meaningful_text(interaction_pattern.get(field_name))
        for field_name in ("state_owner", "input_clocking", "output_completion_signal")
    ) and state_owner_norm in {"provider_server", "harness_process"} and (
        family != "other" or _meaningful_text(interaction_pattern.get("why_this_pattern"))
    ):
        results["interaction_pattern.substance"] = "passed"
    else:
        results["interaction_pattern.substance"] = "failed"

    live_test_strategy = plan.get("live_test_strategy")
    live_text = _normalize(" ".join(_flatten_text(live_test_strategy)))
    has_live_strategy = (
        _meaningful_text(live_test_strategy)
        and "production" in live_text
        and any(token in live_text for token in ("task", "test case", "objective", "fixture"))
    )
    if has_live_strategy:
        results["live_test_strategy"] = "passed"
    else:
        results["live_test_strategy"] = "failed"
        issues.append(
            "live_test_strategy must explain both production-equivalence and task-equivalence"
        )

    open_questions = interaction_pattern.get("open_questions", [])
    blocking_questions: list[str] = []
    if open_questions is None:
        open_questions = []
    if not isinstance(open_questions, list):
        issues.append("interaction_pattern.open_questions must be a list")
    else:
        for question in open_questions:
            if isinstance(question, dict) and question.get("blocking") is True:
                blocking_questions.append(str(question.get("question") or "unnamed question"))
    if blocking_questions:
        results["interaction_pattern.open_questions"] = "failed"
        issues.append(
            "interaction_pattern.open_questions contains blocking questions: "
            + "; ".join(blocking_questions[:5])
        )
    else:
        results["interaction_pattern.open_questions"] = "passed"

    if plan.get("ready_to_build") is True:
        results["ready_to_build"] = "passed"
    else:
        results["ready_to_build"] = "failed"
        issues.append("ready_to_build must be true before build scaffolds can start")

    return results


def validate_implementation_plan(
    plan: dict[str, Any],
    objective_md: str,
    *,
    check_objective_coverage: bool = True,
    check_required_fields: bool = True,
) -> ObjectiveCoverageValidation:
    issues: list[str] = []
    criteria_results: dict[str, str] = {}
    criteria = (
        extract_objective_success_criteria_with_ids(objective_md)
        if check_objective_coverage
        else []
    )
    covered: list[ObjectiveCriterion] = []

    if check_objective_coverage:
        if not criteria:
            issues.append("objective.md has no parseable SUCCESS CRITERIA section")

        entries = _coverage_entries(plan)
        if not entries:
            issues.append("implementation_plan.json must include non-empty objective_coverage")
        else:
            weakening_entries = [entry for entry in entries if _entry_is_weakening(entry)]
            if weakening_entries:
                issues.append("objective_coverage contains dropped/weakened/skipped criteria")

            covered = [
                criterion
                for criterion in criteria
                if _objective_criterion_is_covered(criterion, entries)
            ]
            missing = [criterion for criterion in criteria if criterion not in covered]
            if missing:
                preview = "; ".join(_criterion_preview(criterion) for criterion in missing[:5])
                issues.append(f"objective_coverage missing {len(missing)} success criteria: {preview}")
            for criterion in criteria:
                criteria_results[f"objective_coverage.{criterion.criterion_id}"] = (
                    "passed" if criterion in covered else "failed"
                )
        criteria_results["objective_coverage"] = (
            "passed" if criteria and len(covered) == len(criteria) else "failed"
        )

    if check_required_fields:
        criteria_results.update(_validate_required_plan_fields(plan, issues))

    return ObjectiveCoverageValidation(
        ok=not issues,
        issues=issues,
        required_count=len(criteria),
        covered_count=len(covered),
        criteria_results=criteria_results,
    )


def validate_implementation_plan_text(
    content: str,
    *,
    objective_md: str,
    check_objective_coverage: bool = True,
    check_required_fields: bool = True,
) -> ObjectiveCoverageValidation:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        return ObjectiveCoverageValidation(
            ok=False,
            issues=[f"implementation_plan.json is invalid JSON: {exc.msg}"],
        )
    if not isinstance(raw, dict):
        return ObjectiveCoverageValidation(
            ok=False,
            issues=["implementation_plan.json must be a JSON object"],
        )
    return validate_implementation_plan(
        raw,
        objective_md,
        check_objective_coverage=check_objective_coverage,
        check_required_fields=check_required_fields,
    )


def validate_implementation_plan_file(
    sandbox_dir: Path,
    *,
    check_objective_coverage: bool = True,
    check_required_fields: bool = True,
) -> ObjectiveCoverageValidation:
    plan_path = sandbox_dir / IMPLEMENTATION_PLAN_RELATIVE_PATH
    objective_path = sandbox_dir / "_agent_state" / "objective.md"
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ObjectiveCoverageValidation(
            ok=False,
            issues=[f"missing implementation plan file: {exc}"],
        )
    objective_md = ""
    if check_objective_coverage:
        try:
            objective_md = objective_path.read_text(encoding="utf-8")
        except OSError as exc:
            return ObjectiveCoverageValidation(
                ok=False,
                issues=[f"missing objective file: {exc}"],
            )
    return validate_implementation_plan_text(
        plan_text,
        objective_md=objective_md,
        check_objective_coverage=check_objective_coverage,
        check_required_fields=check_required_fields,
    )


__all__ = [
    "IMPLEMENTATION_PLAN_RELATIVE_PATH",
    "ObjectiveCriterion",
    "ObjectiveCoverageValidation",
    "extract_objective_success_criteria",
    "extract_objective_success_criteria_with_ids",
    "validate_implementation_plan",
    "validate_implementation_plan_file",
    "validate_implementation_plan_text",
]
