from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path


OBJECTIVE_MD = """# Objective

## DELIVERABLE

Build the harness.

## SUCCESS CRITERIA (system-defined; do not modify)

- [ ] harness.py imports and exposes run(input_data)
- [ ] live_test.py passes against the production payload shape
- [ ] All 2 test cases from agent_3_test_cases.json produce success=True with correct output shape
- [ ] User-fit: the harness OBSERVABLY solves the sub-tasks listed in DELIVERABLE

## CONSTRAINTS (system-defined)

- Budget: max 40 turns
"""


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "objective.md").write_text(OBJECTIVE_MD, encoding="utf-8")
    return state_dir


def _stage_valid_research_synthesis(tmp_path: Path) -> None:
    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "research_synthesis.json").write_text(
        json.dumps({
            "schema_version": 1,
            "findings_used": ["research_findings/auth.json"],
            "facts": [
                {
                    "claim": "Example API uses bearer auth.",
                    "source": "research_findings/auth.json",
                }
            ],
            "assumptions": [],
            "open_risks": [],
            "provider_doc_map": [
                {
                    "topic": "auth",
                    "url": "https://docs.example.test/auth",
                    "status": "current",
                    "facts": ["Example API uses bearer auth."],
                    "used_for": ["credential_model"],
                }
            ],
            "chosen_api_surface": {"endpoint_url": "https://api.example.test/v1/respond"},
            "credential_model": {"env_vars": ["EXAMPLE_API_KEY"], "auth_method": "bearer"},
            "request_response_contract": {
                "request_schema": {"prompt": "string"},
                "response_schema": {"answer": "string"},
            },
            "input_compatibility": {"text": {"supported": True}},
            "routing_table": [{"condition": "text", "route": "POST /v1/respond"}],
            "working_examples": [
                {
                    "source": "research_findings/auth.json",
                    "language": "curl",
                    "example": "curl https://api.example.test/v1/respond",
                }
            ],
            "errors_and_limits": {"auth_errors": ["401 on missing bearer token"]},
            "sdk_package": {"package": "none", "used": False},
            "build_brief": {
                "endpoint_auth": "POST /v1/respond with bearer auth.",
                "request_response_shape": "prompt request, answer response.",
                "input_output_mapping": "text input to prompt, answer to output.",
                "state_continuity": "none.",
                "completion_signal": "HTTP response returned.",
                "errors_limits": "401 missing bearer token.",
                "source_pointers": ["research_findings/auth.json"],
            },
            "interaction_constraints": [],
            "dead_or_deprecated_docs": [],
            "unresolved_questions": [],
            "facts_used_for_implementation_plan": [
                {
                    "claim": "Example API uses bearer auth.",
                    "source": "research_findings/auth.json",
                    "plan_field": "credential_loading",
                }
            ],
            "proceed_to_implementation_plan": True,
        }),
        encoding="utf-8",
    )


def _valid_plan() -> dict:
    return {
        "schema_version": 1,
        "summary": "Candidate-specific implementation interpretation.",
        "objective_coverage": [
            {
                "objective_ref": "harness.py imports and exposes run(input_data)",
                "status": "planned",
                "approach": "harness.py will expose run(input_data) and import cleanly.",
            },
            {
                "objective_ref": "live_test.py passes against the production payload shape",
                "status": "planned",
                "approach": "live_test.py will use the provider payload shape from docs.",
            },
            {
                "objective_ref": "All 2 test cases from agent_3_test_cases.json produce success=True with correct output shape",
                "status": "planned",
                "approach": "Harness output schema will map each test case to success=True.",
            },
            {
                "objective_ref": "User-fit: the harness OBSERVABLY solves the sub-tasks listed in DELIVERABLE",
                "status": "planned",
                "approach": "Returned fields expose user-visible evidence for comparison.",
            },
        ],
        "chosen_api_surface": [
            {
                "endpoint_url": "https://api.example.test/v1/respond",
                "method": "POST",
            }
        ],
        "credential_loading": {"env_vars": ["EXAMPLE_API_KEY"]},
        "input_mapping": {"prompt": "test case input"},
        "output_mapping": {"answer": "provider response text"},
        "interaction_pattern": {
            "pattern_name": "single HTTP request per evaluation turn",
            "known_family": "request_response",
            "why_this_pattern": "The provider receives one JSON request and returns one JSON response.",
            "state_owner": "provider_server",
            "input_clocking": "one request is sent for each Agent 3 test case",
            "output_completion_signal": "HTTP 200 response body is received",
            "cleanup_required": False,
            "open_questions": [],
        },
        "live_test_strategy": {
            "production_equivalence": "live_test.py sends the same production HTTP payload shape as harness.py.",
            "task_equivalence": "live_test.py runs a representative Agent 3 task payload and checks success/output shape.",
        },
        "cleanup_strategy": {"resources": "no persistent local resources"},
        "remaining_risks": [],
        "ready_to_build": True,
    }


def test_extracts_top_level_success_criteria():
    from puzzleeval.agents.agent5.objective_validator import (
        extract_objective_success_criteria,
    )

    criteria = extract_objective_success_criteria(OBJECTIVE_MD)

    assert criteria == [
        "harness.py imports and exposes run(input_data)",
        "live_test.py passes against the production payload shape",
        "All 2 test cases from agent_3_test_cases.json produce success=True with correct output shape",
        "User-fit: the harness OBSERVABLY solves the sub-tasks listed in DELIVERABLE",
    ]


def test_extracts_stable_success_criterion_ids():
    from puzzleeval.agents.agent5.objective_validator import (
        extract_objective_success_criteria_with_ids,
    )

    criteria = extract_objective_success_criteria_with_ids(OBJECTIVE_MD)

    assert [(item.criterion_id, item.text) for item in criteria[:2]] == [
        ("OBJ-1", "harness.py imports and exposes run(input_data)"),
        ("OBJ-2", "live_test.py passes against the production payload shape"),
    ]


def test_validator_accepts_criterion_ids_without_copying_objective_text():
    from puzzleeval.agents.agent5.objective_validator import (
        validate_implementation_plan_text,
    )

    plan = _valid_plan()
    plan["objective_coverage"] = [
        {
            "criterion_id": "OBJ-1",
            "status": "planned",
            "approach": "Import harness.py and verify it exposes run(input_data).",
        },
        {
            "criterion_id": "OBJ-2",
            "status": "planned",
            "approach": "Exercise the credentialed production request shape in live validation.",
        },
        {
            "criterion_id": "OBJ-3",
            "status": "planned",
            "approach": "Drive each selected fixture through harness.run and assert the normalized output.",
        },
        {
            "criterion_id": "OBJ-4",
            "status": "planned",
            "approach": "Return observable task evidence rather than a bare provider success flag.",
        },
    ]

    verdict = validate_implementation_plan_text(
        json.dumps(plan),
        objective_md=OBJECTIVE_MD,
    )

    assert verdict.ok is True
    assert verdict.criteria_results["objective_coverage.OBJ-1"] == "passed"


def test_validator_accepts_paraphrased_coverage_when_ids_are_present():
    from puzzleeval.agents.agent5.objective_validator import (
        validate_implementation_plan_text,
    )

    plan = _valid_plan()
    plan["objective_coverage"] = [
        {"covers": ["OBJ-1"], "status": "planned", "approach": "Execute the local build probe successfully."},
        {"covers": ["OBJ-2"], "status": "planned", "approach": "Confirm the real credentialed request path."},
        {"covers": ["OBJ-3"], "status": "planned", "approach": "Replay both canonical fixtures through the adapter."},
        {"covers": ["OBJ-4"], "status": "planned", "approach": "Emit evidence that matches the requested user task."},
    ]

    verdict = validate_implementation_plan_text(
        json.dumps(plan),
        objective_md=OBJECTIVE_MD,
    )

    assert verdict.ok is True


def test_validator_rejects_missing_objective_coverage():
    from puzzleeval.agents.agent5.objective_validator import (
        validate_implementation_plan_text,
    )

    verdict = validate_implementation_plan_text(
        json.dumps({"schema_version": 1, "summary": "too thin"}),
        objective_md=OBJECTIVE_MD,
    )

    assert verdict.ok is False
    assert "objective_coverage" in verdict.issues[0]


def test_write_file_rejects_plan_that_weakens_objective(tmp_path: Path, monkeypatch):
    from puzzleeval.agents.agent5 import tools

    monkeypatch.setenv("PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED", "1")
    import puzzleeval.config as cfg
    importlib.reload(cfg)
    _state_dir(tmp_path)
    _stage_valid_research_synthesis(tmp_path)

    result = tools.write_file(
        {
            "filename": "_agent_state/implementation_plan.json",
            "content": json.dumps({
                "schema_version": 1,
                "objective_coverage": [
                    {"objective_ref": "harness.py imports and exposes run(input_data)", "status": "skipped"}
                ],
                "chosen_api_surface": [{"endpoint_url": "https://api.example.test/v1/respond"}],
                "credential_loading": {"env_vars": ["EXAMPLE_API_KEY"]},
                "interaction_pattern": {
                    "known_family": "request_response",
                    "state_owner": "provider_server",
                    "input_clocking": "one request per test",
                    "output_completion_signal": "HTTP response body",
                    "open_questions": [],
                },
                "live_test_strategy": {
                    "production_equivalence": "same production payload",
                    "task_equivalence": "same task payload",
                },
                "ready_to_build": True,
            }),
        },
        tmp_path,
    )

    assert result.startswith("Error:"), result
    assert "objective_coverage" in result
    assert not (tmp_path / "_agent_state" / "implementation_plan.json").exists()


def test_write_file_accepts_valid_implementation_plan(tmp_path: Path, monkeypatch):
    from puzzleeval.agents.agent5 import tools

    monkeypatch.setenv("PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED", "1")
    import puzzleeval.config as cfg
    importlib.reload(cfg)
    _state_dir(tmp_path)
    _stage_valid_research_synthesis(tmp_path)

    result = tools.write_file(
        {
            "filename": "_agent_state/implementation_plan.json",
            "content": json.dumps(_valid_plan()),
        },
        tmp_path,
    )

    assert not result.startswith("Error:"), result
    assert (tmp_path / "_agent_state" / "implementation_plan.json").is_file()


def test_flag_off_allows_missing_coverage_and_logs_skip(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    from puzzleeval.agents.agent5 import tools

    monkeypatch.setenv("PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED", "0")
    import puzzleeval.config as cfg
    importlib.reload(cfg)
    _state_dir(tmp_path)
    _stage_valid_research_synthesis(tmp_path)
    plan = _valid_plan()
    plan["objective_coverage"] = []

    with caplog.at_level(logging.INFO, logger="puzzleeval.agents.agent5.tools"):
        result = tools.write_file(
            {
                "filename": "_agent_state/implementation_plan.json",
                "content": json.dumps(plan),
            },
            tmp_path,
        )

    assert not result.startswith("Error:"), result
    assert any(
        record.__dict__.get("operation") == "objective_validator_skipped"
        for record in caplog.records
    )

    monkeypatch.setenv("PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED", "1")
    importlib.reload(cfg)
