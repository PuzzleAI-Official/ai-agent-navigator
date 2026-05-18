from __future__ import annotations

import json
from pathlib import Path

import pytest


def _candidate():
    from puzzleeval.schemas import ScreenedCandidate

    return ScreenedCandidate(
        name="Example Realtime",
        provider="Example",
        description="Realtime voice API",
        pricing_model="usage-based",
        pricing_details=None,
        claimed_capabilities=["voice conversation"],
        relevance_score=0.9,
        adoption_difficulty="medium",
        relevant_subtasks=["answer calls"],
        source="https://example.test",
        verified_api_docs_url="https://developers.example.test/realtime",
        auth_method="api_key",
        api_access_method="free_tier",
        confirmed_capabilities=["voice conversation"],
        rate_limit_info=None,
        data_format_notes="WebSocket JSON events with audio deltas",
        screening_notes="Fetched realtime guide successfully.",
    )


def test_docs_entrypoint_synthesizes_agent4_docs_authorization(tmp_path: Path):
    from puzzleeval.docs_entrypoint import synthesize_docs_entrypoint

    (tmp_path / "fetched_docs_0.txt").write_text(
        "# Fetched from: https://developers.example.test/realtime\n\n"
        "POST /realtime\nAuthorization: Bearer ...",
        encoding="utf-8",
    )

    payload = synthesize_docs_entrypoint(_candidate(), tmp_path)

    assert payload["schema_version"] == 1
    assert payload["docs_verdict"] == "verified_docs"
    assert payload["evidence_status"] == "fetched_current_api_docs"
    assert payload["primary_docs_entrypoint"] == "https://developers.example.test/realtime"
    assert payload["official_domain"] == "developers.example.test"
    assert payload["auth_method"] == "api_key"
    assert payload["api_access_method"] == "free_tier"
    assert payload["confidence"] == "high"
    assert payload["capability_hints"] == ["voice conversation"]
    assert not any("selected_endpoint" in item for item in payload["evidence"])


def test_docs_entrypoint_preserves_open_access_method(tmp_path: Path):
    from puzzleeval.docs_entrypoint import synthesize_docs_entrypoint

    candidate = _candidate().model_copy(update={"api_access_method": "open"})
    (tmp_path / "fetched_docs_0.txt").write_text(
        "# Fetched from: https://developers.example.test/realtime\n\n"
        "POST /realtime\nAuthorization: Bearer ...",
        encoding="utf-8",
    )

    payload = synthesize_docs_entrypoint(candidate, tmp_path)

    assert payload["api_access_method"] == "open"


def test_docs_entrypoint_blocks_automatic_build_without_verified_docs(tmp_path: Path):
    from puzzleeval.docs_entrypoint import (
        docs_entrypoint_allows_automatic_build,
        synthesize_docs_entrypoint,
    )

    candidate = _candidate().model_copy(update={"verified_api_docs_url": ""})
    payload = synthesize_docs_entrypoint(candidate, tmp_path)

    assert payload["docs_verdict"] == "no_verified_docs"
    assert payload["primary_docs_entrypoint"] == ""
    assert docs_entrypoint_allows_automatic_build(candidate, tmp_path) is False


def test_orchestrator_selection_uses_docs_entrypoint_gate():
    selection_source = (Path("puzzleeval") / "selection.py").read_text(
        encoding="utf-8"
    )
    agent5_source = (
        Path("puzzleeval") / "agents" / "implement_test_env.py"
    ).read_text(encoding="utf-8")

    assert "select_agent5_build_candidates" in selection_source
    assert "docs_entrypoint_allows_automatic_build" in selection_source
    assert "blocked_missing_docs" in selection_source
    assert "agent5_build_candidate_contract_failed" in agent5_source


def test_builder_prompt_does_not_force_file_only_thin_client_shape():
    prompt = (
        Path("puzzleeval")
        / "agents"
        / "agent5"
        / "templates"
        / "builder_system_prompt.md"
    ).read_text(encoding="utf-8")

    assert "focused provider adapter" in prompt
    assert "persistent_worker" in prompt
    assert "Your harness is a THIN API CLIENT" not in prompt
    assert "Open the test file" not in prompt
    assert "Do NOT parse, extract, format, or transform" not in prompt
    assert "Keep it simple -- no classes" not in prompt


def test_docs_entrypoint_write_read_and_tool_gate(tmp_path: Path):
    from puzzleeval.agents.agent5.tools import write_file
    from puzzleeval.docs_entrypoint import read_docs_entrypoint, write_docs_entrypoint

    (tmp_path / "fetched_docs_0.txt").write_text(
        "# Fetched from: https://developers.example.test/realtime\n\n"
        "POST /realtime\nAuthorization: Bearer ...",
        encoding="utf-8",
    )
    written = write_docs_entrypoint(_candidate(), tmp_path)
    loaded = read_docs_entrypoint(tmp_path)

    assert loaded is not None
    assert loaded["primary_docs_entrypoint"] == written["primary_docs_entrypoint"]
    denied = write_file(
        {"filename": "_agent_state/docs_entrypoint.json", "content": "{}"},
        tmp_path,
    )
    assert "orchestrator-owned" in denied


def test_research_inputs_prefers_docs_entrypoint_and_handoff(tmp_path: Path):
    from puzzleeval.agents.agent5.initial_message import format_research_inputs_block
    from puzzleeval.docs_entrypoint import write_docs_entrypoint
    from puzzleeval.research_handoff import write_research_handoff

    candidate = _candidate()
    (tmp_path / "fetched_docs_0.txt").write_text(
        "# Fetched from: https://developers.example.test/realtime\n\n"
        "POST /realtime\nAuthorization: Bearer ...",
        encoding="utf-8",
    )
    write_docs_entrypoint(candidate, tmp_path)
    write_research_handoff(candidate, tmp_path)

    rendered = format_research_inputs_block(candidate, tmp_path)

    assert "1. `_agent_state/docs_entrypoint.json`" in rendered
    assert "### Docs entrypoint" in rendered
    assert "### Compact handoff index" in rendered
    assert rendered.index("### Docs entrypoint") < rendered.index("### Compact handoff index")


def test_failure_packet_is_written_and_surfaced(tmp_path: Path):
    from puzzleeval.agents.agent5.failure_packets import (
        read_latest_failure_packet,
        summarize_failure_packets,
        write_failure_packet,
    )
    from puzzleeval.agents.agent5.tools import summarize_build_state

    conversation_log = [
        {
            "turn": 1,
            "tool_results": [
                {"tool": "write_file", "wrote_path": "harness.py"},
                {"tool": "patch_file", "patch_path": "live_test.py"},
            ],
        },
        {
            "turn": 2,
            "tool_results": [
                {
                    "tool": "run_code",
                    "is_error": True,
                    "command": "python live_test.py",
                    "result": "AssertionError: missing conversation_history",
                }
            ],
        },
    ]
    packet = write_failure_packet(
        tmp_path,
        turn=2,
        candidate_name="Example",
        failure_source="run_code",
        command="python live_test.py",
        exit_code=1,
        result_text="AssertionError: missing conversation_history\n[Exit code: 1]",
        conversation_log=conversation_log,
    )

    assert "command_failed" in packet["mechanical_tags"]
    assert "conversation_history" in json.dumps(packet["diagnosis"])
    assert "harness.py" in packet["files_changed_since_last_test"]
    assert read_latest_failure_packet(tmp_path)["turn"] == 2
    assert summarize_failure_packets(tmp_path)["count"] == 1
    summary = summarize_build_state({}, tmp_path)
    assert "latest_failure_packet" in summary
    assert "mechanical_tags" in summary


def test_failure_packet_filename_sanitizes_logical_source(tmp_path: Path):
    from puzzleeval.agents.agent5.failure_packets import write_failure_packet

    packet = write_failure_packet(
        tmp_path,
        turn=23,
        candidate_name="Example",
        failure_source="tool:write_file",
        result_text="Error: extension not allowed",
        conversation_log=[],
    )

    packet_path = tmp_path / packet["path"]
    assert packet["failure_source"] == "tool:write_file"
    assert packet_path.name == "turn_023_tool_write_file.json"
    assert packet_path.exists()
    assert packet_path.stat().st_size > 0
    assert not (tmp_path / "_agent_state" / "failure_packets" / "turn_023_tool").exists()


def test_failure_packet_nested_paths_are_readable_but_not_writable(tmp_path: Path):
    from puzzleeval.agents.agent5.failure_packets import write_failure_packet
    from puzzleeval.agents.agent5.tools import read_file, write_file

    packet = write_failure_packet(
        tmp_path,
        turn=5,
        candidate_name="Example",
        failure_source="completion_gate",
        result_text="reflection missing",
        conversation_log=[],
    )

    content = read_file({"filename": packet["path"]}, tmp_path)
    assert '"failure_source": "completion_gate"' in content

    denied = write_file(
        {"filename": packet["path"], "content": "{}"},
        tmp_path,
    )
    assert "orchestrator-owned" in denied


def test_completion_issue_key_changes_after_reflection_file_exists():
    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent5.build_loop import _completion_gate_issue_key

    assert (
        _completion_gate_issue_key("_agent_state/reflection_phase_3.md is missing")
        == "reflection_missing"
    )
    assert (
        _completion_gate_issue_key(
            "_agent_state/reflection_phase_3.md is present but lacks substantive evidence: "
            "missing_sections=5"
        )
        == "reflection_evidence"
    )


def test_voice_live_contract_rejects_business_fixture_contradictions(tmp_path: Path):
    from puzzleeval.agents.agent5.verification import verify_voice_live_test_contract

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "objective.md").write_text(
        "# Objective\nBuild a Bean & Brew voice ordering harness.",
        encoding="utf-8",
    )
    (state_dir / "business_fixture.json").write_text(
        json.dumps({
            "schema_version": 1,
            "synthetic": False,
            "canonical_facts": [
                "Menu: Latte $5.50, Drip Coffee $3.00, Espresso $3.25, Croissant $4.00, Muffin $3.50",
                "Hours: Mon-Sat 7am-7pm, closed Sundays",
            ],
        }),
        encoding="utf-8",
    )
    (state_dir / "runtime_state.json").write_text(
        json.dumps({
            "last_live_test_output": "\n".join([
                json.dumps({
                    "turn_index": 0,
                    "success": True,
                    "transcript": "Pickup is available Sunday for cold brew with oat milk.",
                    "audio_path": "turn0.wav",
                }),
                json.dumps({
                    "turn_index": 1,
                    "success": True,
                    "transcript": "We are open 6am-7pm daily.",
                    "audio_path": "turn1.wav",
                }),
            ]),
        }),
        encoding="utf-8",
    )
    (tmp_path / "live_test.py").write_text(
        """
def test_live():
    turns = [
        {"turn_index": 0, "input_context": {"instructions": "Bean & Brew customer support"}, "conversation_history": [], "text": "hello"},
        {"turn_index": 1, "input_context": {"instructions": "Bean & Brew customer support"}, "conversation_history": [{"role": "user", "content": "hello"}], "text": "again"},
    ]
""",
        encoding="utf-8",
    )

    result = verify_voice_live_test_contract(tmp_path)
    assert result is not None
    assert "business_fixture consistency" in result
    assert "catalog item/add-on" in result


def test_voice_live_contract_rejects_tone_only_audio(tmp_path: Path):
    from puzzleeval.agents.agent5.verification import verify_voice_live_test_contract

    class _Block:
        text = json.dumps({
            "decision": "block",
            "confidence": "high",
            "issues": ["caller input provenance is tone audio and transcripts do not prove current-turn speech"],
            "evidence": ["turn 0 produced a generic greeting despite task speech requirement"],
            "rationale": "The live test passed transport but not task-equivalent spoken input.",
        })

    class _Messages:
        def create(self, **_kwargs):
            return type("Resp", (), {"content": [_Block()]})()

    class _Client:
        messages = _Messages()

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "objective.md").write_text(
        "# Objective\nBuild a Bean & Brew voice ordering harness.",
        encoding="utf-8",
    )
    (state_dir / "runtime_state.json").write_text(
        json.dumps({
            "last_live_test_output": "\n".join([
                json.dumps({
                    "turn_index": 0,
                    "success": True,
                    "transcript": "Hi there, how can I help?",
                    "audio_path": "turn0.wav",
                }),
                json.dumps({
                    "turn_index": 1,
                    "success": True,
                    "transcript": "We open at 7am Saturday.",
                    "audio_path": "turn1.wav",
                }),
            ]),
        }),
        encoding="utf-8",
    )
    (tmp_path / "live_test.py").write_text(
        """
import math
import struct
import wave

def make_caller_wav(path):
    with wave.open(path, "wb") as wf:
        for i in range(1000):
            wf.writeframes(struct.pack("<h", int(math.sin(i) * 1000)))

def test_live():
    turns = [
        {"turn_index": 0, "input_context": {"instructions": "Bean & Brew"}, "conversation_history": [], "test_file_path": "tone0.wav"},
        {"turn_index": 1, "input_context": {"instructions": "Bean & Brew"}, "conversation_history": [{"role": "user", "content": "hours"}], "test_file_path": "tone1.wav"},
    ]
""",
        encoding="utf-8",
    )

    result = verify_voice_live_test_contract(
        tmp_path,
        client=_Client(),
        judge_model="claude-opus-4-7",
        llm_review_enabled=True,
    )
    assert result is not None
    assert "semantic evidence" in result
    assert "tone audio" in result


def test_voice_live_contract_does_not_require_runner_recipe_phrase(tmp_path: Path):
    from puzzleeval.agents.agent5.verification import verify_voice_live_test_contract

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "runtime_state.json").write_text(
        json.dumps({
            "last_live_test_output": "\n".join([
                json.dumps({
                    "turn_index": 0,
                    "success": True,
                    "transcript": "We open at 7am Saturday.",
                    "audio_path": "turn0.wav",
                }),
                json.dumps({
                    "turn_index": 1,
                    "success": True,
                    "transcript": "Your latte and muffin pickup order is started.",
                    "audio_path": "turn1.wav",
                }),
            ]),
        }),
        encoding="utf-8",
    )
    (tmp_path / "live_test.py").write_text(
        """
def test_live():
    turns = [
        {"turn_index": 0, "input_context": {"instructions": "Bean & Brew"}, "conversation_history": [], "text": "hours"},
        {"turn_index": 1, "input_context": {"instructions": "Bean & Brew"}, "conversation_history": [{"role": "user", "content": "hours"}], "text": "order"},
    ]
    assert turns
""",
        encoding="utf-8",
    )

    assert verify_voice_live_test_contract(tmp_path) is None


def test_validator_audit_blocks_source_text_semantic_live_checks():
    verification_src = (
        Path(__file__).parent.parent
        / "puzzleeval"
        / "agents"
        / "agent5"
        / "verification.py"
    ).read_text(encoding="utf-8")

    assert "persistent subprocess" not in verification_src
    assert "validate_text_against_business_fixture(" not in verification_src
    assert "generic_phrases" not in verification_src


def test_research_handoff_synthesizes_compact_agent4_context(tmp_path: Path):
    from puzzleeval.agents.agent5.initial_message import (
        format_research_handoff_block,
        format_research_inputs_block,
    )
    from puzzleeval.research_handoff import read_research_handoff, write_research_handoff

    (tmp_path / "fetched_docs_0.txt").write_text(
        "# Fetched from: https://developers.example.test/realtime\n\nbody",
        encoding="utf-8",
    )
    handoff = write_research_handoff(_candidate(), tmp_path)

    assert "https://developers.example.test/realtime" in handoff["canonical_docs_urls"]
    assert handoff["discovered_docs_urls"] == []
    assert any(
        item["status"] in {"confirmed_source", "fetched_current"}
        and item["discovered_url"] == "https://developers.example.test/realtime"
        for item in handoff["docs_resolution"]
    )
    assert "fetched_docs_0.txt" in handoff["prefetched_doc_files"]
    assert handoff["sdk_or_transport"] == "websocket"
    assert handoff["unresolved_questions"] == []
    assert read_research_handoff(tmp_path)["candidate"] == "Example Realtime"

    rendered = format_research_handoff_block(tmp_path)
    assert "Compact Research Handoff" in rendered
    assert "fetched_docs_0.txt" in rendered
    assert "_agent_state/research_handoff.json" in rendered

    consolidated = format_research_inputs_block(_candidate(), tmp_path)
    assert "Research Inputs (Agent 4 -> Agent 5)" in consolidated
    assert "Compact handoff index" in consolidated
    assert "Raw evidence files" in consolidated
    assert "BUILD-READINESS CHECKLIST" not in consolidated
    assert "Prefetched Documentation Inventory" not in consolidated


def test_research_handoff_uses_docs_resolver_for_known_migrations(tmp_path: Path):
    from types import SimpleNamespace
    from puzzleeval.research_handoff import synthesize_research_handoff

    candidate = SimpleNamespace(
        name="OpenAI Realtime",
        verified_api_docs_url="https://platform.openai.com/docs/guides/realtime",
        auth_method="api_key",
        data_format_notes="WebSocket realtime audio events",
        screening_notes="Use realtime docs.",
    )
    handoff = synthesize_research_handoff(candidate, tmp_path)

    assert "https://platform.openai.com/docs/guides/realtime" not in handoff["canonical_docs_urls"]
    assert "https://developers.openai.com/api/docs/guides/realtime-websocket" in handoff["canonical_docs_urls"]
    assert "https://developers.openai.com/api/docs/guides/realtime-conversations" in handoff["canonical_docs_urls"]
    assert handoff["dead_or_blocked_urls"][0]["reason"] == "legacy_docs_migrated"
    assert handoff["docs_resolution"][0]["status"] == "deprecated_replaced"

    candidate.verified_api_docs_url = "https://platform.openai.com/docs/guides/realtime-websocket"
    handoff = synthesize_research_handoff(candidate, tmp_path)
    assert "https://platform.openai.com/docs/guides/realtime-websocket" not in handoff["canonical_docs_urls"]
    assert "https://developers.openai.com/api/docs/guides/realtime-websocket" in handoff["canonical_docs_urls"]


def test_research_handoff_does_not_treat_snippet_only_docs_field_as_canonical(tmp_path: Path):
    from types import SimpleNamespace
    from puzzleeval.research_handoff import synthesize_research_handoff

    candidate = SimpleNamespace(
        name="Snippet Only API",
        verified_api_docs_url="https://docs.snippet-only.test/api",
        auth_method="api_key",
        data_format_notes="REST JSON",
        screening_notes="search result looked promising but no fetch was saved",
    )

    handoff = synthesize_research_handoff(candidate, tmp_path)

    assert handoff["canonical_docs_urls"] == []
    assert handoff["discovered_docs_urls"] == ["https://docs.snippet-only.test/api"]
    assert handoff["docs_resolution"][0]["status"] == "discovered_unverified"


def test_terminal_research_url_memory_updates_handoff_and_summary(tmp_path: Path):
    from puzzleeval.agents.agent5.research_memory import (
        mark_terminal_research_url,
        summarize_terminal_research_urls,
        terminal_reason_for_fetch_result,
    )
    from puzzleeval.agents.agent5.tools import summarize_build_state
    from puzzleeval.research_handoff import write_research_handoff

    write_research_handoff(_candidate(), tmp_path)
    result = {
        "tool": "web_fetch",
        "url": "https://platform.example.test/docs/realtime",
        "status": "error",
        "error_code": "url_not_allowed",
        "error_message": "URL not allowed",
    }
    reason = terminal_reason_for_fetch_result(result)
    assert reason == "url_not_allowed"

    entry = mark_terminal_research_url(
        tmp_path,
        url=result["url"],
        reason=reason,
        turn=3,
        status=result["status"],
        error_code=result["error_code"],
    )
    assert entry["attempts"] == 1
    terminal_summary = summarize_terminal_research_urls(tmp_path)
    assert terminal_summary["count"] == 1

    handoff = json.loads(
        (tmp_path / "_agent_state" / "research_handoff.json").read_text(encoding="utf-8")
    )
    assert handoff["dead_or_blocked_urls"][0]["reason"] == "url_not_allowed"
    build_state = summarize_build_state({}, tmp_path)
    assert "terminal_research_urls" in build_state
    assert "url_not_allowed" in build_state


def test_conversation_summary_includes_failure_and_research_memory(tmp_path: Path):
    from puzzleeval.agents.agent5.conversation_log import save_conversation_log
    from puzzleeval.agents.agent5.failure_packets import write_failure_packet
    from puzzleeval.agents.agent5.research_memory import mark_terminal_research_url

    write_failure_packet(
        tmp_path,
        turn=1,
        candidate_name="Example",
        failure_source="completion_gate",
        command="HARNESS_COMPLETE",
        exit_code=None,
        result_text="reflection missing",
        issues=["reflection missing"],
        conversation_log=[],
    )
    mark_terminal_research_url(
        tmp_path,
        url="https://platform.example.test/docs",
        reason="403_or_waf",
        turn=0,
        status="error",
        error_code="403",
    )
    save_conversation_log(
        tmp_path,
        [{"turn": 0, "tool_calls": [], "tool_results": [], "latency_ms": 10}],
        "Example",
    )
    summary = json.loads((tmp_path / "conversation_summary.json").read_text(encoding="utf-8"))
    assert summary["failure_packets"]["count"] == 1
    assert summary["terminal_research_urls"]["count"] == 1
