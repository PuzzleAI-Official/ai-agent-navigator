from __future__ import annotations

import json
from pathlib import Path

from puzzleeval.agents.agent5.representative_probe import (
    representative_probe_evidence_path,
    run_representative_probe_gate,
)
from puzzleeval.agents.agent5.research_plan import (
    research_build_brief_path,
    write_research_build_brief,
)
from puzzleeval.agents.agent5.test_case_manifest import (
    build_test_case_manifest,
    representative_test_cases,
    stage_test_case_manifest,
)
from puzzleeval.schemas import (
    Agent3Result,
    Agent5Input,
    Constraints,
    JudgementCriterion,
    ScreenedCandidate,
    SubTask,
    TestCase,
    UserUnderstandingOutput,
)


def _candidate(auth_method: str = "api_key") -> ScreenedCandidate:
    return ScreenedCandidate(
        name="Example Voice",
        provider="Example",
        description="Example provider",
        pricing_model="usage",
        pricing_details="usage",
        claimed_capabilities=["voice"],
        relevance_score=0.9,
        adoption_difficulty="medium",
        relevant_subtasks=["Handle call"],
        source="https://example.com",
        verified_api_docs_url="https://example.com/docs",
        auth_method=auth_method,
        api_access_method="api_key",
        confirmed_capabilities=["voice"],
        data_format_notes="JSON",
        screening_notes="Verified docs",
    )


def _input(test_cases: list[TestCase]) -> Agent5Input:
    return Agent5Input(
        validated_candidates=[_candidate()],
        user_understanding=UserUnderstandingOutput(
            summary="Evaluate provider",
            sub_tasks=[
                SubTask(
                    description="Handle call",
                    capability="voice",
                    search_keywords=["voice"],
                )
            ],
            search_strategy="web",
            domain="test",
            search_keywords=["voice api"],
            constraints=Constraints(),
        ),
        test_cases=Agent3Result(
            test_cases=test_cases,
            generation_notes="tests",
            coverage_summary={"Handle call": len(test_cases)},
        ),
        trace_id="trace-1",
    )


def _case(
    test_id: str,
    *,
    input_type: str = "voice_conversation",
    output_type: str = "audio_content",
    mode: str = "agentic",
    max_turns: int = 4,
) -> TestCase:
    return TestCase(
        id=test_id,
        sub_task_ref="Handle call",
        scenario=f"Scenario {test_id}",
        input_type=input_type,
        output_type=output_type,
        input_data="caller asks for help",
        expected_output="helpful answer",
        difficulty="medium",
        tags=["happy_path"],
        evaluation_mode=mode,
        max_turns=max_turns,
        judgement_criteria=[
            JudgementCriterion(
                criterion="Responds to the current task",
                weight=1.0,
                eval_type="rubric_score",
            )
        ],
    )


def test_manifest_is_derived_from_real_test_case_families(tmp_path: Path):
    text_case = _case(
        "tc-text",
        input_type="text",
        output_type="free_text",
        mode="auto",
        max_turns=1,
    )
    voice_case = _case("tc-voice", max_turns=5)
    input_data = _input([text_case, voice_case])

    payload = build_test_case_manifest(input_data)

    assert payload["test_count"] == 2
    assert payload["family_count"] == 2
    assert sorted(payload["representative_test_ids"]) == ["tc-text", "tc-voice"]
    assert all("family_key" in item for item in payload["families"])


def test_representative_probe_skips_truthfully_when_credentials_missing(tmp_path: Path):
    tc = _case("tc-voice")
    input_data = _input([tc])
    stage_test_case_manifest(tmp_path, input_data)
    (tmp_path / "harness.py").write_text(
        "def run(input_data):\n"
        "    return {'success': True, 'output': 'ok', 'raw_response': {}, "
        "'latency_ms': 0, 'tokens_used': None, 'cost_usd': None}\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    (tmp_path / "_agent_state" / "implementation_plan.json").write_text(
        json.dumps({"interaction_pattern": {"state_owner": "provider_server"}}),
        encoding="utf-8",
    )

    issue = run_representative_probe_gate(
        client=object(),
        sandbox_dir=tmp_path,
        candidate=_candidate(auth_method="api_key"),
        input_data=input_data,
        staged_test_cases=[tc],
        credentials=None,
        logger=__import__("logging").getLogger("test"),
        trace_id="trace-1",
    )

    evidence = json.loads(representative_probe_evidence_path(tmp_path).read_text(encoding="utf-8"))
    assert issue is None
    assert evidence["status"] == "skipped_no_credentials"
    assert evidence["selected_test_ids"] == ["tc-voice"]


def test_research_build_brief_extracts_action_ready_buckets(tmp_path: Path):
    findings_dir = tmp_path / "_agent_state" / "research_findings"
    findings_dir.mkdir(parents=True)
    (findings_dir / "auth.json").write_text(
        json.dumps({
            "schema_version": 1,
            "task_id": "auth",
            "question": "How does auth and streaming work?",
            "answer": (
                "Use Authorization: Bearer API_KEY header.\n"
                "Request JSON field text carries the prompt.\n"
                "WebSocket emits response.done when completion finishes.\n"
                "429 rate limit errors include retry-after."
            ),
            "confidence": "high",
            "sources": [{"url": "https://example.com/docs"}],
            "unresolved_questions": [],
            "notes_for_builder": [],
        }),
        encoding="utf-8",
    )

    path = write_research_build_brief(tmp_path)
    brief = json.loads(path.read_text(encoding="utf-8"))

    assert path == research_build_brief_path(tmp_path)
    item = brief["brief"][0]
    assert item["task_id"] == "auth"
    assert item["implementation_fact_buckets"]["endpoint_auth"]
    assert item["implementation_fact_buckets"]["stream_or_completion_signal"]
