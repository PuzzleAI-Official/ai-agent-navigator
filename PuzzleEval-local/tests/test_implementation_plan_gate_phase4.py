from __future__ import annotations

import importlib
import json
from pathlib import Path


OBJECTIVE_MD = """# Objective

## SUCCESS CRITERIA (system-defined; do not modify)

- [ ] harness.py imports and exposes run(input_data)
- [ ] live_test.py proves production payload shape
"""


def _state_dir(tmp_path: Path) -> None:
    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "objective.md").write_text(OBJECTIVE_MD, encoding="utf-8")


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
            "chosen_api_surface": {
                "endpoint_url": "https://api.example.test/v1/respond",
                "method": "POST",
            },
            "credential_model": {
                "env_vars": ["EXAMPLE_API_KEY"],
                "auth_method": "bearer",
            },
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
                "request_response_shape": "prompt string request, answer string response.",
                "input_output_mapping": "input text maps to prompt, answer maps to output.",
                "state_continuity": "none; request/response only.",
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


def _valid_plan(*, known_family: str = "request_response") -> dict:
    return {
        "schema_version": 1,
        "objective_coverage": [
            {
                "objective_ref": "harness.py imports and exposes run(input_data)",
                "status": "planned",
                "approach": "harness.py will expose run(input_data) and import cleanly.",
            },
            {
                "objective_ref": "live_test.py proves production payload shape",
                "status": "planned",
                "approach": "live_test.py sends the production request shape.",
            },
        ],
        "chosen_api_surface": [
            {"endpoint_url": "https://api.example.test/v1/respond", "method": "POST"}
        ],
        "credential_loading": {"env_vars": ["EXAMPLE_API_KEY"]},
        "input_mapping": {"prompt": "test case input"},
        "output_mapping": {"answer": "provider response text"},
        "interaction_pattern": {
            "pattern_name": "candidate-specific turn handling",
            "known_family": known_family,
            "why_this_pattern": (
                "The provider keeps request state on its server; the harness sends "
                "one JSON task and waits for the provider response body."
            ),
            "state_owner": "provider_server",
            "input_clocking": "the evaluator sends one task payload per test case",
            "output_completion_signal": "the HTTP response body has been received",
            "cleanup_required": False,
            "open_questions": [],
        },
        "live_test_strategy": {
            "production_equivalence": "live_test.py sends the same production HTTP payload shape as harness.py.",
            "task_equivalence": "live_test.py uses a representative Agent 3 task payload and checks success/output shape.",
        },
        "cleanup_strategy": {"resources": "no persistent local resources"},
        "remaining_risks": [],
        "ready_to_build": True,
    }


def _reload_config(monkeypatch, *, objective_enabled: str = "1"):
    monkeypatch.setenv("PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED", objective_enabled)
    monkeypatch.setenv("PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK", "1")
    import puzzleeval.config as cfg

    importlib.reload(cfg)
    return cfg


def test_validator_rejects_thin_plan_when_phase4_gate_enabled():
    from puzzleeval.agents.agent5.objective_validator import validate_implementation_plan_text

    verdict = validate_implementation_plan_text(
        json.dumps({"schema_version": 1, "objective_coverage": []}),
        objective_md=OBJECTIVE_MD,
        check_objective_coverage=True,
        check_required_fields=True,
    )

    assert verdict.ok is False
    assert any("chosen_api_surface" in issue for issue in verdict.issues)
    assert any("credential_loading.env_vars" in issue for issue in verdict.issues)
    assert any("live_test_strategy" in issue for issue in verdict.issues)
    assert any("ready_to_build" in issue for issue in verdict.issues)


def test_validator_allows_other_family_with_provider_specific_explanation():
    from puzzleeval.agents.agent5.objective_validator import validate_implementation_plan_text

    plan = _valid_plan(known_family="other")
    plan["interaction_pattern"]["why_this_pattern"] = (
        "ExampleAPI does not fit the built-in families because the response is "
        "returned through a provider-owned resumable handle; the harness owns no "
        "session state, clocks one task input per handle creation, and treats the "
        "provider completion field as the output completion signal."
    )

    verdict = validate_implementation_plan_text(
        json.dumps(plan),
        objective_md=OBJECTIVE_MD,
    )

    assert verdict.ok is True, verdict.issues


def test_validator_rejects_unknown_state_owner():
    from puzzleeval.agents.agent5.objective_validator import validate_implementation_plan_text

    plan = _valid_plan()
    plan["interaction_pattern"]["state_owner"] = "runtime_adapter"

    verdict = validate_implementation_plan_text(
        json.dumps(plan),
        objective_md=OBJECTIVE_MD,
    )

    assert verdict.ok is False
    assert any("interaction_pattern.state_owner" in issue for issue in verdict.issues)


def test_scaffolds_blocked_until_implementation_plan_accepted(
    tmp_path: Path,
    monkeypatch,
):
    from puzzleeval.agents.agent5 import tools

    _reload_config(monkeypatch)
    _state_dir(tmp_path)

    result = tools.write_file(
        {"filename": "harness.py", "content": "def run(x): return {}"},
        tmp_path,
        phase_state={
            "implementation_plan_accepted": False,
            "candidate_slug": "phase4",
            "trace_id": "phase4-test",
        },
    )

    assert result.startswith("Error:"), result
    assert "implementation_plan.json has been accepted" in result
    assert not (tmp_path / "harness.py").exists()


def test_accepted_implementation_plan_allows_scaffolds(tmp_path: Path, monkeypatch):
    from puzzleeval.agents.agent5 import tools

    _reload_config(monkeypatch)
    _state_dir(tmp_path)

    result = tools.write_file(
        {"filename": "harness.py", "content": "def run(x): return {}"},
        tmp_path,
        phase_state={
            "implementation_plan_accepted": True,
            "candidate_slug": "phase4",
            "trace_id": "phase4-test",
        },
    )

    assert not result.startswith("Error:"), result
    assert (tmp_path / "harness.py").exists()


def test_implementation_plan_requires_research_synthesis_first(
    tmp_path: Path,
    monkeypatch,
):
    from puzzleeval.agents.agent5 import tools

    _reload_config(monkeypatch)
    _state_dir(tmp_path)

    result = tools.write_file(
        {
            "filename": "_agent_state/implementation_plan.json",
            "content": json.dumps(_valid_plan()),
        },
        tmp_path,
    )

    assert result.startswith("Error:"), result
    assert "research_synthesis.json" in result
    assert not (tmp_path / "_agent_state" / "implementation_plan.json").exists()


def test_valid_implementation_plan_accepts_after_research_synthesis(
    tmp_path: Path,
    monkeypatch,
):
    from puzzleeval.agents.agent5 import tools

    _reload_config(monkeypatch)
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
    assert (tmp_path / "_agent_state" / "implementation_plan.json").exists()
