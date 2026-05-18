"""Phase 9 rollout validation for the Agent 4/5 recovery architecture.

Mocks are the release blocker. This module defines the required v1 mock
scenario matrix and validates a rollout run summary against the architecture
contracts:

* verified-docs scenarios have docs entrypoints, research artifacts,
  accepted implementation plans, and the expected binary runtime primitive;
* blocked/unavailable/incompatible scenarios exercise truthful abandonment or
  docs-entrypoint blocking;
* runtime safety controls have explicit enabled/default-path coverage;
* real API smoke is recorded opportunistically without becoming the gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from puzzleeval.agents.agent5.abandon_candidate import read_abandon_candidate
from puzzleeval.agents.agent5.objective_validator import validate_implementation_plan_file
from puzzleeval.agents.agent5.research_plan import (
    validate_research_plan_text,
    validate_research_synthesis_text,
)
from puzzleeval.agents.agent5.runtime_policy import (
    RUNTIME_PERSISTENT_WORKER,
    RUNTIME_SINGLE_CALL,
    select_runtime_primitive,
)
from puzzleeval.docs_entrypoint import read_docs_entrypoint


MOCK_REST_REQUEST_RESPONSE = "rest_request_response"
MOCK_SERIALIZED_CONVERSATION = "serialized_conversation"
MOCK_PERSISTENT_SESSION = "persistent_session"
MOCK_CONTINUOUS_STREAM = "continuous_stream"
MOCK_PROVIDER_BLOCKED = "provider_blocked"
MOCK_CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
MOCK_API_INCOMPATIBLE = "api_incompatible"
MOCK_DEPRECATED_DOCS = "deprecated_docs"
MOCK_MISSING_DOCS = "missing_docs"


REQUIRED_CONTROL_FLAGS: tuple[str, ...] = (
    "PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED",
    "PUZZLEEVAL_RESEARCH_WORKERS_ENABLED",
    "PUZZLEEVAL_PERSISTENT_WORKER_RUNTIME_ENABLED",
    "PUZZLEEVAL_FAILURE_PACKET_DEBUG_ENABLED",
    "PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED",
    "PUZZLEEVAL_EFFICIENCY_SUMMARY_ENABLED",
)


@dataclass(frozen=True)
class MockScenarioRequirement:
    scenario_id: str
    expected_status: str = "passed"
    expected_docs_verdict: str | None = "verified_docs"
    expected_known_family: str | None = None
    expected_state_owner: str | None = None
    expected_runtime: str | None = None
    requires_docs_entrypoint: bool = True
    requires_research_artifacts: bool = True
    requires_implementation_plan: bool = True
    expected_abandon_reason: str | None = None
    expected_automatic_build_allowed: bool | None = True
    min_deprecated_or_blocked_urls: int = 0


REQUIRED_MOCK_SCENARIOS: tuple[MockScenarioRequirement, ...] = (
    MockScenarioRequirement(
        scenario_id=MOCK_REST_REQUEST_RESPONSE,
        expected_known_family="request_response",
        expected_state_owner="provider_server",
        expected_runtime=RUNTIME_SINGLE_CALL,
    ),
    MockScenarioRequirement(
        scenario_id=MOCK_SERIALIZED_CONVERSATION,
        expected_known_family="serialized_conversation",
        expected_state_owner="provider_server",
        expected_runtime=RUNTIME_SINGLE_CALL,
    ),
    MockScenarioRequirement(
        scenario_id=MOCK_PERSISTENT_SESSION,
        expected_known_family="persistent_session",
        expected_state_owner="harness_process",
        expected_runtime=RUNTIME_PERSISTENT_WORKER,
    ),
    MockScenarioRequirement(
        scenario_id=MOCK_CONTINUOUS_STREAM,
        expected_known_family="continuous_stream",
        expected_state_owner="harness_process",
        expected_runtime=RUNTIME_PERSISTENT_WORKER,
    ),
    MockScenarioRequirement(
        scenario_id=MOCK_PROVIDER_BLOCKED,
        requires_research_artifacts=False,
        requires_implementation_plan=False,
        expected_abandon_reason="provider_blocked",
    ),
    MockScenarioRequirement(
        scenario_id=MOCK_CREDENTIALS_UNAVAILABLE,
        requires_research_artifacts=False,
        requires_implementation_plan=False,
        expected_abandon_reason="credentials_unavailable",
    ),
    MockScenarioRequirement(
        scenario_id=MOCK_API_INCOMPATIBLE,
        requires_research_artifacts=False,
        requires_implementation_plan=False,
        expected_abandon_reason="api_incompatible",
    ),
    MockScenarioRequirement(
        scenario_id=MOCK_DEPRECATED_DOCS,
        expected_known_family="request_response",
        expected_state_owner="provider_server",
        expected_runtime=RUNTIME_SINGLE_CALL,
        min_deprecated_or_blocked_urls=1,
    ),
    MockScenarioRequirement(
        scenario_id=MOCK_MISSING_DOCS,
        expected_docs_verdict="no_verified_docs",
        requires_research_artifacts=False,
        requires_implementation_plan=False,
        expected_automatic_build_allowed=False,
    ),
)


@dataclass(frozen=True)
class RolloutValidationVerdict:
    ok: bool
    issues: list[str] = field(default_factory=list)
    scenario_count: int = 0
    flag_count: int = 0
    real_api_smoke_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": list(self.issues),
            "scenario_count": self.scenario_count,
            "flag_count": self.flag_count,
            "real_api_smoke_count": self.real_api_smoke_count,
        }


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _validate_json_file(path: Path, validator: Any) -> bool:
    try:
        return bool(validator(path.read_text(encoding="utf-8")).ok)
    except OSError:
        return False


def collect_mock_scenario_result(
    scenario_id: str,
    sandbox_dir: Path,
    *,
    status: str = "passed",
) -> dict[str, Any]:
    """Summarize one mock scenario sandbox for Phase 9 rollout validation."""

    state_dir = sandbox_dir / "_agent_state"
    docs_entrypoint = read_docs_entrypoint(sandbox_dir) or {}
    docs_verdict = str(docs_entrypoint.get("docs_verdict") or "")
    primary_docs = str(docs_entrypoint.get("primary_docs_entrypoint") or "")
    deprecated_or_blocked = docs_entrypoint.get("deprecated_or_blocked_urls") or []

    plan = _read_json_object(state_dir / "implementation_plan.json") or {}
    interaction = plan.get("interaction_pattern") if isinstance(plan, dict) else {}
    interaction = interaction if isinstance(interaction, dict) else {}
    runtime = select_runtime_primitive(
        sandbox_dir=sandbox_dir,
        persistent_worker_enabled=True,
    )

    abandon = read_abandon_candidate(sandbox_dir)
    implementation_plan_verdict = validate_implementation_plan_file(sandbox_dir)

    return {
        "scenario_id": scenario_id,
        "status": status,
        "has_docs_entrypoint": bool(docs_entrypoint),
        "docs_verdict": docs_verdict,
        "primary_docs_entrypoint": primary_docs,
        "automatic_build_allowed": (
            docs_verdict == "verified_docs"
            and docs_entrypoint.get("evidence_status") == "fetched_current_api_docs"
            and bool(primary_docs)
        ),
        "deprecated_or_blocked_urls_count": (
            len(deprecated_or_blocked) if isinstance(deprecated_or_blocked, list) else 0
        ),
        "has_research_plan": (state_dir / "research_plan.json").exists(),
        "research_plan_ok": _validate_json_file(
            state_dir / "research_plan.json",
            validate_research_plan_text,
        ),
        "has_research_synthesis": (state_dir / "research_synthesis.json").exists(),
        "research_synthesis_ok": _validate_json_file(
            state_dir / "research_synthesis.json",
            validate_research_synthesis_text,
        ),
        "has_implementation_plan": bool(plan),
        "implementation_plan_ok": implementation_plan_verdict.ok,
        "known_family": str(interaction.get("known_family") or ""),
        "state_owner": str(interaction.get("state_owner") or ""),
        "runtime_primitive": runtime.mode,
        "runtime_degraded": runtime.degraded,
        "abandon_reason": str((abandon or {}).get("reason") or ""),
        "abandon_evidence_count": len((abandon or {}).get("evidence") or []),
    }


def _index_by_id(items: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item.get(key) or "")
        if item_id:
            indexed[item_id] = item
    return indexed


def _validate_scenario(
    requirement: MockScenarioRequirement,
    result: dict[str, Any],
) -> list[str]:
    sid = requirement.scenario_id
    issues: list[str] = []

    if result.get("status") != requirement.expected_status:
        issues.append(
            f"{sid}: status must be {requirement.expected_status!r}, "
            f"got {result.get('status')!r}"
        )
    if requirement.requires_docs_entrypoint and not result.get("has_docs_entrypoint"):
        issues.append(f"{sid}: docs_entrypoint.json is required")
    if (
        requirement.expected_docs_verdict is not None
        and result.get("docs_verdict") != requirement.expected_docs_verdict
    ):
        issues.append(
            f"{sid}: docs_verdict must be {requirement.expected_docs_verdict!r}, "
            f"got {result.get('docs_verdict')!r}"
        )
    if (
        requirement.expected_automatic_build_allowed is not None
        and bool(result.get("automatic_build_allowed"))
        != requirement.expected_automatic_build_allowed
    ):
        issues.append(
            f"{sid}: automatic_build_allowed must be "
            f"{requirement.expected_automatic_build_allowed}"
        )
    if (
        int(result.get("deprecated_or_blocked_urls_count") or 0)
        < requirement.min_deprecated_or_blocked_urls
    ):
        issues.append(
            f"{sid}: deprecated_or_blocked_urls_count must be at least "
            f"{requirement.min_deprecated_or_blocked_urls}"
        )
    if requirement.requires_research_artifacts:
        if not result.get("research_plan_ok"):
            issues.append(f"{sid}: research_plan.json must validate")
        if not result.get("research_synthesis_ok"):
            issues.append(f"{sid}: research_synthesis.json must validate")
    if requirement.requires_implementation_plan and not result.get("implementation_plan_ok"):
        issues.append(f"{sid}: implementation_plan.json must validate")
    for field_name, expected in (
        ("known_family", requirement.expected_known_family),
        ("state_owner", requirement.expected_state_owner),
        ("runtime_primitive", requirement.expected_runtime),
    ):
        if expected is not None and result.get(field_name) != expected:
            issues.append(
                f"{sid}: {field_name} must be {expected!r}, "
                f"got {result.get(field_name)!r}"
            )
    if requirement.expected_abandon_reason is not None:
        if result.get("abandon_reason") != requirement.expected_abandon_reason:
            issues.append(
                f"{sid}: abandon_reason must be "
                f"{requirement.expected_abandon_reason!r}"
            )
        if int(result.get("abandon_evidence_count") or 0) < 1:
            issues.append(f"{sid}: abandon_candidate.json must include evidence")

    return issues


def _validate_flag_exercises(flag_exercises: Iterable[dict[str, Any]]) -> list[str]:
    by_flag = _index_by_id(flag_exercises, "flag")
    issues: list[str] = []
    for flag in REQUIRED_CONTROL_FLAGS:
        exercise = by_flag.get(flag)
        if not exercise:
            issues.append(f"{flag}: control exercise is missing")
            continue
        if exercise.get("enabled_status") != "passed":
            issues.append(f"{flag}: enabled_status must be 'passed'")
        disabled_status = str(exercise.get("disabled_status") or "")
        if disabled_status == "passed":
            continue
        if disabled_status not in {"degraded", "unsupported"}:
            issues.append(
                f"{flag}: disabled_status must be passed, degraded, or unsupported"
            )
        if not str(exercise.get("disabled_classification") or "").strip():
            issues.append(
                f"{flag}: disabled path must include explicit classification"
            )
    return issues


def _validate_real_api_smoke(smoke_attempts: Iterable[dict[str, Any]]) -> list[str]:
    attempts = list(smoke_attempts)
    if not attempts:
        return ["real_api_smoke: absence or attempt status must be recorded"]

    issues: list[str] = []
    for index, attempt in enumerate(attempts):
        label = f"real_api_smoke[{index}]"
        status = str(attempt.get("status") or "")
        credentials_present = bool(attempt.get("credentials_present"))
        if credentials_present:
            if status not in {"passed", "failed_recorded"}:
                issues.append(
                    f"{label}: credentials are present, so smoke must be "
                    "attempted and recorded as passed or failed_recorded"
                )
        else:
            if status != "skipped":
                issues.append(f"{label}: missing credentials should be recorded as skipped")
        if status in {"skipped", "failed_recorded"} and not str(attempt.get("reason") or "").strip():
            issues.append(f"{label}: {status} smoke entry must include a reason")
        if bool(attempt.get("reproduced_in_mocks")):
            issues.append(f"{label}: real API failure reproduced in mocks blocks rollout")
    return issues


def validate_phase9_rollout(
    *,
    mock_scenarios: Iterable[dict[str, Any]],
    flag_exercises: Iterable[dict[str, Any]],
    real_api_smoke: Iterable[dict[str, Any]],
) -> RolloutValidationVerdict:
    """Validate the Phase 9 release evidence matrix."""

    scenario_list = list(mock_scenarios)
    flag_list = list(flag_exercises)
    smoke_list = list(real_api_smoke)
    by_scenario = _index_by_id(scenario_list, "scenario_id")

    issues: list[str] = []
    for requirement in REQUIRED_MOCK_SCENARIOS:
        result = by_scenario.get(requirement.scenario_id)
        if not result:
            issues.append(f"{requirement.scenario_id}: mock scenario is missing")
            continue
        issues.extend(_validate_scenario(requirement, result))

    issues.extend(_validate_flag_exercises(flag_list))
    issues.extend(_validate_real_api_smoke(smoke_list))

    return RolloutValidationVerdict(
        ok=not issues,
        issues=issues,
        scenario_count=len(scenario_list),
        flag_count=len(flag_list),
        real_api_smoke_count=len(smoke_list),
    )


def write_rollout_validation_artifact(
    sandbox_dir: Path,
    verdict: RolloutValidationVerdict,
) -> Path:
    """Write the Phase 9 rollout verdict for CI/report consumers."""

    state_dir = sandbox_dir / "_agent_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "phase9_rollout_validation.json"
    path.write_text(
        json.dumps(verdict.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


__all__ = [
    "MOCK_API_INCOMPATIBLE",
    "MOCK_CONTINUOUS_STREAM",
    "MOCK_CREDENTIALS_UNAVAILABLE",
    "MOCK_DEPRECATED_DOCS",
    "MOCK_MISSING_DOCS",
    "MOCK_PERSISTENT_SESSION",
    "MOCK_PROVIDER_BLOCKED",
    "MOCK_REST_REQUEST_RESPONSE",
    "MOCK_SERIALIZED_CONVERSATION",
    "REQUIRED_CONTROL_FLAGS",
    "REQUIRED_MOCK_SCENARIOS",
    "MockScenarioRequirement",
    "RolloutValidationVerdict",
    "collect_mock_scenario_result",
    "validate_phase9_rollout",
    "write_rollout_validation_artifact",
]
