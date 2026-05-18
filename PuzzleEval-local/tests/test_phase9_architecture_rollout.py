from __future__ import annotations

import json
from pathlib import Path

from puzzleeval.agents.agent5.rollout_validation import (
    MOCK_API_INCOMPATIBLE,
    MOCK_CONTINUOUS_STREAM,
    MOCK_CREDENTIALS_UNAVAILABLE,
    MOCK_DEPRECATED_DOCS,
    MOCK_MISSING_DOCS,
    MOCK_PERSISTENT_SESSION,
    MOCK_PROVIDER_BLOCKED,
    MOCK_REST_REQUEST_RESPONSE,
    MOCK_SERIALIZED_CONVERSATION,
    REQUIRED_CONTROL_FLAGS,
    REQUIRED_MOCK_SCENARIOS,
    collect_mock_scenario_result,
    validate_phase9_rollout,
    write_rollout_validation_artifact,
)


SUCCESS_CRITERION = "Harness returns task-equivalent output for mock scenario"


def _state_dir(sandbox: Path) -> Path:
    state_dir = sandbox / "_agent_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_docs_entrypoint(
    sandbox: Path,
    *,
    verdict: str = "verified_docs",
    deprecated: bool = False,
) -> None:
    _write_json(
        _state_dir(sandbox) / "docs_entrypoint.json",
        {
            "schema_version": 1,
            "candidate": sandbox.name,
            "docs_verdict": verdict,
            "evidence_status": (
                "fetched_current_api_docs" if verdict == "verified_docs" else "missing_fetch_verified_docs"
            ),
            "verification_method": "web_fetch" if verdict == "verified_docs" else "none",
            "primary_docs_entrypoint": (
                "https://docs.example.test/api" if verdict == "verified_docs" else ""
            ),
            "official_domain": "docs.example.test" if verdict == "verified_docs" else "",
            "alternate_entrypoints": [],
            "deprecated_or_blocked_urls": (
                [{"url": "https://old.example.test/docs", "reason": "deprecated"}]
                if deprecated
                else []
            ),
            "evidence": [
                {
                    "source_url": "https://docs.example.test/api",
                    "claim": "official docs",
                    "evidence_type": "fetched_page",
                }
            ],
            "capability_hints": [],
            "auth_method": "api_key",
            "api_access_method": "sandbox",
            "pricing_summary": "mock",
            "confidence": "high" if verdict == "verified_docs" else "low",
        },
    )


def _write_research_artifacts(sandbox: Path) -> None:
    state_dir = _state_dir(sandbox)
    _write_json(
        state_dir / "research_plan.json",
        {
            "schema_version": 1,
            "docs_entrypoint": "https://docs.example.test/api",
            "objective_summary": "Exercise the mock provider.",
            "research_tasks": [
                {
                    "id": "auth",
                    "question": "How does mock auth work?",
                    "where_to_look": ["https://docs.example.test/api/auth"],
                    "evidence_required": ["header format"],
                    "why_needed_for_build": "Changes credential loading in harness.py.",
                }
            ],
            "stop_condition": "Enough evidence to write implementation_plan.json.",
        },
    )
    _write_json(
        state_dir / "research_synthesis.json",
        {
            "schema_version": 1,
            "findings_used": ["_agent_state/research_findings/auth.json"],
            "facts": [
                {
                    "claim": "Mock API uses X-Mock-Key.",
                    "source": "_agent_state/research_findings/auth.json",
                }
            ],
            "assumptions": [],
            "open_risks": [],
            "provider_doc_map": [
                {
                    "topic": "mock auth",
                    "url": "https://docs.example.test/api/auth",
                    "status": "current",
                    "facts": ["Mock API uses X-Mock-Key."],
                    "used_for": ["credential_model", "request_headers"],
                }
            ],
            "chosen_api_surface": {
                "endpoint_url": "https://api.example.test/v1/run",
                "method": "POST",
                "why_selected": "It exercises the mock rollout path.",
            },
            "credential_model": {
                "env_vars": ["MOCK_API_KEY"],
                "auth_method": "api_key",
                "header": "X-Mock-Key: ${MOCK_API_KEY}",
            },
            "request_response_contract": {
                "request_schema": {"text": "string"},
                "response_schema": {"result": "string"},
                "input_mapping": {"text": "payload.text"},
                "output_mapping": {"output": "response.result"},
            },
            "input_compatibility": {
                "plain_text": {"supported": True, "mapping": "payload.text"},
                "file_upload": {"supported": False, "reason": "mock text API"},
            },
            "routing_table": [
                {"condition": "plain text", "route": "POST /v1/run", "code_pattern": "json={'text': ...}"}
            ],
            "working_examples": [
                {"source": "_agent_state/research_findings/auth.json", "language": "curl", "example": "curl https://api.example.test/v1/run"}
            ],
            "errors_and_limits": {
                "auth_errors": ["401 missing X-Mock-Key"],
                "rate_limits": ["429 quota exceeded"],
            },
            "sdk_package": {"package": "none", "used": False, "reason": "mock raw HTTP"},
            "build_brief": {
                "endpoint_auth": "POST /v1/run with X-Mock-Key.",
                "request_response_shape": "text request, result response.",
                "input_output_mapping": "payload.text maps to response.result.",
                "state_continuity": "none.",
                "completion_signal": "HTTP response returned.",
                "errors_limits": "401 missing key; 429 quota.",
                "source_pointers": ["_agent_state/research_findings/auth.json"],
            },
            "interaction_constraints": [],
            "dead_or_deprecated_docs": [],
            "unresolved_questions": [],
            "facts_used_for_implementation_plan": [
                {
                    "claim": "Mock API uses X-Mock-Key.",
                    "source": "_agent_state/research_findings/auth.json",
                    "plan_field": "credential_model",
                }
            ],
            "proceed_to_implementation_plan": True,
        },
    )


def _write_implementation_plan(
    sandbox: Path,
    *,
    known_family: str,
    state_owner: str,
) -> None:
    state_dir = _state_dir(sandbox)
    (state_dir / "objective.md").write_text(
        "## SUCCESS CRITERIA\n"
        f"- [ ] {SUCCESS_CRITERION}\n",
        encoding="utf-8",
    )
    _write_json(
        state_dir / "implementation_plan.json",
        {
            "schema_version": 1,
            "objective_coverage": [
                {
                    "objective_ref": SUCCESS_CRITERION,
                    "status": "covered",
                    "evidence": "smoke_test.py and live_test.py cover the mock.",
                }
            ],
            "chosen_api_surface": [
                {
                    "endpoint_url": "https://api.example.test/v1/run",
                    "method": "POST",
                }
            ],
            "credential_loading": {"env_vars": ["MOCK_API_KEY"]},
            "input_mapping": {"text": "payload.text"},
            "output_mapping": {"output": "response.result"},
            "interaction_pattern": {
                "pattern_name": known_family,
                "known_family": known_family,
                "why_this_pattern": "Mock scenario exercises rollout routing.",
                "state_owner": state_owner,
                "input_clocking": "one user turn per request",
                "output_completion_signal": "mock completion event",
                "cleanup_required": state_owner == "harness_process",
                "open_questions": [],
            },
            "live_test_strategy": {
                "production_equivalence": "Uses the same payload shape as production.",
                "task_equivalence": "Asserts the objective/test case output.",
            },
            "cleanup_strategy": {"close": "worker process if persistent"},
            "remaining_risks": [],
            "ready_to_build": True,
        },
    )


def _write_success_scenario(
    sandbox: Path,
    *,
    known_family: str,
    state_owner: str,
    deprecated: bool = False,
) -> None:
    _write_docs_entrypoint(sandbox, deprecated=deprecated)
    _write_research_artifacts(sandbox)
    _write_implementation_plan(
        sandbox,
        known_family=known_family,
        state_owner=state_owner,
    )


def _write_abandon_scenario(sandbox: Path, *, reason: str) -> None:
    state_dir = _state_dir(sandbox)
    _write_docs_entrypoint(sandbox)
    _write_json(
        state_dir / "provider_response.json",
        {"status": "blocked", "reason": reason},
    )
    _write_json(
        state_dir / "abandon_candidate.json",
        {
            "schema_version": 1,
            "candidate": sandbox.name,
            "reason": reason,
            "summary": "Mock scenario proves continued patching is not honest.",
            "evidence": [{"artifact": "_agent_state/provider_response.json"}],
            "recommended_action": "skip_for_this_run",
        },
    )


def _complete_flag_exercises() -> list[dict]:
    return [
        {
            "flag": flag,
            "enabled_status": "passed",
            "disabled_status": "degraded",
            "disabled_classification": "explicit degraded control path",
        }
        for flag in REQUIRED_CONTROL_FLAGS
    ]


def _complete_mock_scenarios(tmp_path: Path) -> list[dict]:
    scenarios = [
        (
            MOCK_REST_REQUEST_RESPONSE,
            "request_response",
            "provider_server",
            False,
        ),
        (
            MOCK_SERIALIZED_CONVERSATION,
            "serialized_conversation",
            "provider_server",
            False,
        ),
        (
            MOCK_PERSISTENT_SESSION,
            "persistent_session",
            "harness_process",
            False,
        ),
        (
            MOCK_CONTINUOUS_STREAM,
            "continuous_stream",
            "harness_process",
            False,
        ),
        (
            MOCK_DEPRECATED_DOCS,
            "request_response",
            "provider_server",
            True,
        ),
    ]
    results: list[dict] = []
    for scenario_id, known_family, state_owner, deprecated in scenarios:
        sandbox = tmp_path / scenario_id
        _write_success_scenario(
            sandbox,
            known_family=known_family,
            state_owner=state_owner,
            deprecated=deprecated,
        )
        results.append(collect_mock_scenario_result(scenario_id, sandbox))

    for scenario_id, reason in (
        (MOCK_PROVIDER_BLOCKED, "provider_blocked"),
        (MOCK_CREDENTIALS_UNAVAILABLE, "credentials_unavailable"),
        (MOCK_API_INCOMPATIBLE, "api_incompatible"),
    ):
        sandbox = tmp_path / scenario_id
        _write_abandon_scenario(sandbox, reason=reason)
        results.append(collect_mock_scenario_result(scenario_id, sandbox))

    missing_docs_sandbox = tmp_path / MOCK_MISSING_DOCS
    _write_docs_entrypoint(missing_docs_sandbox, verdict="no_verified_docs")
    results.append(collect_mock_scenario_result(MOCK_MISSING_DOCS, missing_docs_sandbox))
    return results


def test_phase9_required_matrix_matches_recovery_plan():
    scenario_ids = {item.scenario_id for item in REQUIRED_MOCK_SCENARIOS}

    assert scenario_ids == {
        MOCK_REST_REQUEST_RESPONSE,
        MOCK_SERIALIZED_CONVERSATION,
        MOCK_PERSISTENT_SESSION,
        MOCK_CONTINUOUS_STREAM,
        MOCK_PROVIDER_BLOCKED,
        MOCK_CREDENTIALS_UNAVAILABLE,
        MOCK_API_INCOMPATIBLE,
        MOCK_DEPRECATED_DOCS,
        MOCK_MISSING_DOCS,
    }
    assert len(REQUIRED_CONTROL_FLAGS) == 6


def test_phase9_rollout_validation_accepts_complete_mock_matrix(tmp_path: Path):
    verdict = validate_phase9_rollout(
        mock_scenarios=_complete_mock_scenarios(tmp_path),
        flag_exercises=_complete_flag_exercises(),
        real_api_smoke=[
            {
                "provider": "mock-only-ci",
                "credentials_present": False,
                "status": "skipped",
                "reason": "No real API credentials are staged in CI.",
            }
        ],
    )

    assert verdict.ok, verdict.issues
    artifact_path = write_rollout_validation_artifact(tmp_path, verdict)
    assert json.loads(artifact_path.read_text(encoding="utf-8"))["ok"] is True


def test_phase9_rollout_validation_rejects_wrong_runtime(tmp_path: Path):
    scenarios = _complete_mock_scenarios(tmp_path)
    for result in scenarios:
        if result["scenario_id"] == MOCK_CONTINUOUS_STREAM:
            result["runtime_primitive"] = "single_call"

    verdict = validate_phase9_rollout(
        mock_scenarios=scenarios,
        flag_exercises=_complete_flag_exercises(),
        real_api_smoke=[
            {
                "provider": "mock-only-ci",
                "credentials_present": False,
                "status": "skipped",
                "reason": "No credentials.",
            }
        ],
    )

    assert not verdict.ok
    assert any("continuous_stream: runtime_primitive" in issue for issue in verdict.issues)


def test_phase9_rollout_validation_rejects_missing_disabled_path_classification(
    tmp_path: Path,
):
    flag_exercises = _complete_flag_exercises()
    flag_exercises[0] = {
        "flag": REQUIRED_CONTROL_FLAGS[0],
        "enabled_status": "passed",
        "disabled_status": "degraded",
    }

    verdict = validate_phase9_rollout(
        mock_scenarios=_complete_mock_scenarios(tmp_path),
        flag_exercises=flag_exercises,
        real_api_smoke=[
            {
                "provider": "mock-only-ci",
                "credentials_present": False,
                "status": "skipped",
                "reason": "No credentials.",
            }
        ],
    )

    assert not verdict.ok
    assert any("disabled path must include explicit classification" in issue for issue in verdict.issues)


def test_phase9_rollout_validation_requires_real_smoke_attempt_when_credentials_exist(
    tmp_path: Path,
):
    verdict = validate_phase9_rollout(
        mock_scenarios=_complete_mock_scenarios(tmp_path),
        flag_exercises=_complete_flag_exercises(),
        real_api_smoke=[
            {
                "provider": "example",
                "credentials_present": True,
                "status": "skipped",
                "reason": "incorrectly skipped",
            }
        ],
    )

    assert not verdict.ok
    assert any("credentials are present" in issue for issue in verdict.issues)
