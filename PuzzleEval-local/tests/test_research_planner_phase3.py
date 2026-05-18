from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest


def _valid_plan() -> dict:
    return {
        "schema_version": 1,
        "docs_entrypoint": "https://docs.example.test/realtime",
        "objective_summary": "Build a realtime voice harness for the provided tests.",
        "research_tasks": [
            {
                "id": "auth",
                "question": "How does auth work for the realtime surface?",
                "where_to_look": ["https://docs.example.test/realtime/auth"],
                "evidence_required": ["source URL", "exact auth header or token flow"],
                "why_needed_for_build": "harness.py must load and send credentials correctly.",
            },
            {
                "id": "events",
                "question": "Which event marks output completion?",
                "where_to_look": ["https://docs.example.test/realtime/events"],
                "evidence_required": ["source URL", "completion event name"],
                "why_needed_for_build": "live_test.py needs a deterministic stop condition.",
            },
        ],
        "stop_condition": "Enough evidence to write implementation_plan.json without guessing.",
    }


def _valid_synthesis() -> dict:
    return {
        "schema_version": 1,
        "findings_used": ["research_findings/auth.json"],
        "facts": [
            {
                "claim": "Realtime auth uses an Authorization bearer token.",
                "source": "research_findings/auth.json",
            }
        ],
        "assumptions": [],
        "open_risks": [],
        "provider_doc_map": [
            {
                "topic": "authentication",
                "url": "https://docs.example.test/realtime/auth",
                "status": "current",
                "facts": ["Realtime auth uses an Authorization bearer token."],
                "used_for": ["credential_model", "request_headers"],
            }
        ],
        "chosen_api_surface": {
            "endpoint_url": "https://api.example.test/realtime",
            "method": "POST",
            "why_selected": "It is the documented realtime surface for the task.",
        },
        "credential_model": {
            "env_vars": ["EXAMPLE_API_KEY"],
            "auth_method": "bearer_token",
            "header": "Authorization: Bearer ${EXAMPLE_API_KEY}",
        },
        "request_response_contract": {
            "request_schema": {"text": "string"},
            "response_schema": {"output": "string"},
            "input_mapping": {"text": "payload.text"},
            "output_mapping": {"output": "response.output"},
        },
        "input_compatibility": {
            "plain_text": {"supported": True, "mapping": "payload.text"},
            "file_upload": {"supported": False, "reason": "text endpoint only"},
        },
        "routing_table": [
            {"condition": "plain text", "route": "POST /realtime", "code_pattern": "json={'text': ...}"}
        ],
        "working_examples": [
            {
                "source": "research_findings/auth.json",
                "language": "curl",
                "example": "curl -H 'Authorization: Bearer ...' https://api.example.test/realtime",
            }
        ],
        "errors_and_limits": {
            "auth_errors": ["401 when bearer token is missing"],
            "rate_limits": ["429 when quota is exceeded"],
        },
        "sdk_package": {"package": "none", "used": False, "reason": "raw HTTP is documented"},
        "build_brief": {
            "endpoint_auth": "POST https://api.example.test/realtime with bearer auth.",
            "request_response_shape": "JSON request text field, JSON response output field.",
            "input_output_mapping": "payload.text maps to response.output.",
            "state_continuity": "none; each request is independent.",
            "completion_signal": "HTTP response body returned.",
            "errors_limits": "401 missing bearer token; 429 quota.",
            "source_pointers": ["research_findings/auth.json"],
        },
        "interaction_constraints": [],
        "dead_or_deprecated_docs": [],
        "unresolved_questions": [],
        "facts_used_for_implementation_plan": [
            {
                "claim": "Realtime auth uses an Authorization bearer token.",
                "source": "research_findings/auth.json",
                "plan_field": "credential_model",
            }
        ],
        "proceed_to_implementation_plan": True,
    }


def test_research_plan_validator_rejects_broad_or_empty_plan():
    from puzzleeval.agents.agent5.research_plan import validate_research_plan_text

    verdict = validate_research_plan_text(json.dumps({
        "schema_version": 1,
        "docs_entrypoint": "",
        "objective_summary": "",
        "research_tasks": [],
        "stop_condition": "",
    }))

    assert verdict.ok is False
    assert any("research_tasks" in issue for issue in verdict.issues)
    assert any("docs_entrypoint" in issue for issue in verdict.issues)


def test_write_file_validates_research_plan_and_synthesis(tmp_path: Path):
    from puzzleeval.agents.agent5.tools import summarize_build_state, write_file

    rejected = write_file(
        {"filename": "_agent_state/research_plan.json", "content": "{}"},
        tmp_path,
    )
    assert "research_plan.json failed Phase 3" in rejected

    accepted = write_file(
        {
            "filename": "_agent_state/research_plan.json",
            "content": json.dumps(_valid_plan()),
        },
        tmp_path,
    )
    assert accepted.startswith("Written")

    synthesis = write_file(
        {
            "filename": "_agent_state/research_synthesis.json",
            "content": json.dumps(_valid_synthesis()),
        },
        tmp_path,
    )
    assert synthesis.startswith("Written")

    summary = summarize_build_state({}, tmp_path)
    assert "planned_research" in summary
    assert "research_task_ids" in summary
    assert "synthesis_validator" in summary
    assert "provider_doc_map_topics" in summary


def test_research_synthesis_requires_provider_understanding_sections():
    from puzzleeval.agents.agent5.research_plan import validate_research_synthesis_text

    old_minimal_shape = {
        "schema_version": 1,
        "findings_used": ["research_findings/auth.json"],
        "facts": [
            {
                "claim": "Auth uses a bearer token.",
                "source": "research_findings/auth.json",
            }
        ],
        "assumptions": [],
        "open_risks": [],
        "proceed_to_implementation_plan": True,
    }

    verdict = validate_research_synthesis_text(json.dumps(old_minimal_shape))

    assert verdict.ok is False
    assert any("provider_doc_map" in issue for issue in verdict.issues)
    assert any("chosen_api_surface" in issue for issue in verdict.issues)
    assert any("request_response_contract" in issue for issue in verdict.issues)
    assert any("input_compatibility" in issue for issue in verdict.issues)
    assert any("routing_table" in issue for issue in verdict.issues)
    assert any("build_brief" in issue for issue in verdict.issues)


def test_research_worker_budget_text_matches_worker_budget():
    source = Path("puzzleeval/agents/implement_test_env.py").read_text(encoding="utf-8")

    assert "3 searches + 3 fetches" not in source
    assert "2 web_search" in source
    assert "2 web_fetch" in source


def test_research_workers_flag_zero_demotes_validation(monkeypatch, tmp_path: Path, caplog):
    import puzzleeval.config as cfg
    from puzzleeval.agents.agent5.tools import write_file

    caplog.set_level("INFO")
    monkeypatch.setenv("PUZZLEEVAL_RESEARCH_WORKERS_ENABLED", "0")
    importlib.reload(cfg)
    try:
        result = write_file(
            {"filename": "_agent_state/research_plan.json", "content": "{}"},
            tmp_path,
        )
    finally:
        monkeypatch.setenv("PUZZLEEVAL_RESEARCH_WORKERS_ENABLED", "1")
        importlib.reload(cfg)

    assert result.startswith("Written")
    assert any(
        record.__dict__.get("operation") == "research_workers_validator_skipped"
        for record in caplog.records
    )


def test_research_batch_runs_tasks_and_skips_terminal_urls(tmp_path: Path):
    from puzzleeval.agents.agent5.research_memory import mark_terminal_research_url
    from puzzleeval.agents.agent5.research_plan import run_research_batch

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    plan = _valid_plan()
    plan["research_tasks"][0]["where_to_look"].append("https://docs.example.test/dead")
    (state_dir / "research_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    mark_terminal_research_url(
        tmp_path,
        url="https://docs.example.test/dead",
        reason="url_not_allowed",
        turn=0,
    )

    seen_where_to_look: dict[str, list[str]] = {}

    def researcher(task: dict) -> dict:
        time.sleep(0.05)
        seen_where_to_look[task["id"]] = task["where_to_look"]
        return {
            "schema_version": 1,
            "task_id": task["id"],
            "answer": f"Answer for {task['id']}",
            "confidence": "high",
            "sources": [
                {
                    "url": "https://docs.example.test/realtime",
                    "claim": "Official docs support this answer.",
                }
            ],
            "blocked_or_dead_urls": [],
            "unresolved_questions": [],
            "notes_for_builder": [],
        }

    started = time.monotonic()
    findings = run_research_batch(tmp_path, researcher, max_workers=2)
    elapsed = time.monotonic() - started

    assert [item["task_id"] for item in findings] == ["auth", "events"]
    assert "https://docs.example.test/dead" not in seen_where_to_look["auth"]
    assert elapsed < 0.12
    assert (state_dir / "research_findings" / "auth.json").exists()


def test_planned_research_once_writes_marker_and_avoids_rerun(tmp_path: Path):
    from puzzleeval.agents.agent5.research_plan import (
        RESEARCH_EXECUTION_MARKER_RELATIVE_PATH,
        run_research_batch_once,
    )

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "research_plan.json").write_text(json.dumps(_valid_plan()), encoding="utf-8")
    calls: list[str] = []

    def researcher(task: dict) -> dict:
        calls.append(task["id"])
        return {
            "schema_version": 1,
            "task_id": task["id"],
            "answer": f"Answer for {task['id']}",
            "confidence": "high",
            "sources": [
                {
                    "url": "https://docs.example.test/realtime",
                    "claim": "Official docs support this answer.",
                }
            ],
            "blocked_or_dead_urls": [],
            "unresolved_questions": [],
            "notes_for_builder": [],
        }

    first = run_research_batch_once(tmp_path, researcher, max_workers=2)
    second = run_research_batch_once(tmp_path, researcher, max_workers=2)

    assert first.status == "ran"
    assert second.status == "already_complete"
    assert sorted(calls) == ["auth", "events"]
    assert (tmp_path / RESEARCH_EXECUTION_MARKER_RELATIVE_PATH).exists()
    index = json.loads((state_dir / "research_findings_index.json").read_text(encoding="utf-8"))
    assert index["finding_count"] == 2
    assert [item["task_id"] for item in index["findings"]] == ["auth", "events"]
    assert len(index["findings"][0]["direct_answer"]) < 1300


def test_research_batch_owns_task_identity(tmp_path: Path):
    from puzzleeval.agents.agent5.research_plan import run_research_batch_once

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "research_plan.json").write_text(json.dumps(_valid_plan()), encoding="utf-8")

    def researcher(task: dict) -> dict:
        return {
            "schema_version": 1,
            "task_id": "stale-id-from-worker",
            "answer": f"Answer for {task['id']}",
            "confidence": "high",
            "sources": [
                {
                    "url": "https://docs.example.test/realtime",
                    "claim": "Official docs support this answer.",
                }
            ],
            "blocked_or_dead_urls": [],
            "unresolved_questions": [],
            "notes_for_builder": [],
        }

    first = run_research_batch_once(tmp_path, researcher, max_workers=2)
    second = run_research_batch_once(tmp_path, researcher, max_workers=2)

    assert [item["task_id"] for item in first.findings] == ["auth", "events"]
    assert second.status == "already_complete"
    assert json.loads((state_dir / "research_findings" / "auth.json").read_text())["task_id"] == "auth"


def test_ask_research_scope_requires_task_or_concrete_debug_gap(tmp_path: Path):
    from puzzleeval.agents.agent5.research_plan import validate_ask_research_scope

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "research_plan.json").write_text(json.dumps(_valid_plan()), encoding="utf-8")

    broad = validate_ask_research_scope(
        tmp_path,
        question="Tell me about the provider API",
    )
    planned = validate_ask_research_scope(
        tmp_path,
        question="How does auth work?",
        task_id="auth",
    )
    debug_gap = validate_ask_research_scope(
        tmp_path,
        question=(
            "CANDIDATE: Example\n"
            "FIELD NEEDED: exact retry-after header\n"
            "WHY: this changes the error handler"
        ),
    )

    assert broad.allowed is False
    assert broad.route == "unscoped_question"
    assert planned.allowed is True
    assert planned.route == "planned_task"
    assert debug_gap.allowed is True
    assert debug_gap.route == "unplanned_debug_gap"


def test_ask_research_scope_allows_concrete_debug_gap_without_plan(tmp_path: Path):
    from puzzleeval.agents.agent5.research_plan import validate_ask_research_scope

    broad = validate_ask_research_scope(
        tmp_path,
        question="Research the provider API.",
    )
    scoped = validate_ask_research_scope(
        tmp_path,
        question=(
            "CANDIDATE: Example\n"
            "KNOWN: synthesis covers auth but not stream completion\n"
            "FIELD NEEDED: exact stream completion event after output\n"
            "WHY: this changes the live_test completion detector"
        ),
    )

    assert broad.allowed is False
    assert broad.route == "unscoped_question"
    assert scoped.allowed is True
    assert scoped.route == "unplanned_debug_gap"


def test_ask_research_scope_blocks_repeated_unplanned_debug_gap(tmp_path: Path):
    from puzzleeval.agents.agent5.research_plan import (
        record_inline_research_finding,
        validate_ask_research_scope,
    )

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "research_plan.json").write_text(json.dumps(_valid_plan()), encoding="utf-8")
    question = (
        "CANDIDATE: Example\n"
        "FIELD NEEDED: exact retry-after header\n"
        "WHY: this changes the error handler"
    )

    first = validate_ask_research_scope(tmp_path, question=question)
    assert first.allowed is True
    record_inline_research_finding(
        tmp_path,
        question=question,
        answer=(
            "ANSWER: Retry-After is returned on rate limits.\n"
            "SOURCE: https://docs.example.test/realtime/errors\n"
            "CONFIDENCE: high"
        ),
        turn=3,
    )

    second = validate_ask_research_scope(tmp_path, question=question)
    assert second.allowed is False
    assert second.route == "debug_gap_already_researched"


def test_inline_ask_research_is_persisted_as_structured_finding(tmp_path: Path):
    from puzzleeval.agents.agent5.research_plan import record_inline_research_finding

    path = record_inline_research_finding(
        tmp_path,
        question="CANDIDATE: Example\nFIELD NEEDED: auth header",
        answer=(
            "ANSWER: Use Authorization bearer tokens.\n"
            "SOURCE: https://docs.example.test/realtime/auth\n"
            "CONFIDENCE: high"
        ),
        turn=7,
    )

    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["task_id"] == "ask_research_turn_7"
    assert data["confidence"] == "high"
    assert data["debug_gap_key"] == "auth header"
    assert data["validation"]["ok"] is True


def test_inline_ask_research_without_url_still_leaves_reviewable_artifact(tmp_path: Path):
    from puzzleeval.agents.agent5.research_plan import record_inline_research_finding

    path = record_inline_research_finding(
        tmp_path,
        question="CANDIDATE: Example\nFIELD NEEDED: completion signal",
        answer=(
            "REASONABLE_GUESS: The provider likely emits completed.\n"
            "BASIS: The SDK examples use that event.\n"
            "CONFIDENCE: low"
        ),
        turn=8,
    )

    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sources"][0]["artifact"] == "research_turn8.txt"
    assert data["validation"]["ok"] is True


def test_research_findings_are_worker_owned_readonly(tmp_path: Path):
    from puzzleeval.agents.agent5.research_plan import research_findings_dir
    from puzzleeval.agents.agent5.tools import read_file, write_file

    directory = research_findings_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "auth.json").write_text(
        json.dumps({
            "schema_version": 1,
            "task_id": "auth",
            "answer": "Bearer auth.",
            "confidence": "high",
            "sources": [{"url": "https://docs.example.test/auth", "claim": "Bearer auth."}],
        }),
        encoding="utf-8",
    )

    content = read_file({"filename": "_agent_state/research_findings/auth.json"}, tmp_path)
    assert '"task_id": "auth"' in content

    denied = write_file(
        {"filename": "_agent_state/research_findings/auth.json", "content": "{}"},
        tmp_path,
    )
    assert "orchestrator-owned" in denied


def test_ask_research_gate_uses_scoped_knowledge_gap_gate():
    source = (
        Path("puzzleeval")
        / "agents"
        / "agent5"
        / "build_loop.py"
    ).read_text(encoding="utf-8")

    assert "RESEARCH_WORKERS_ENABLED" in source
    assert "ask_research_scope_gate" in source
    assert "validate_ask_research_scope" in source
    assert "_run_planned_research_workers" in source


def test_ask_research_tool_description_surfaces_phase3_gate():
    pytest.importorskip("anthropic")
    from puzzleeval.agents.implement_test_env import ASK_RESEARCH_TOOL

    desc = ASK_RESEARCH_TOOL["description"]
    assert "DEFAULT KNOWLEDGE-GAP GATE" in desc
    assert "_agent_state/research_plan.json" in desc
    assert "FIELD NEEDED" in desc
    assert "Debug gap mode" in desc
