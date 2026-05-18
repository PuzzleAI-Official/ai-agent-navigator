import importlib
import json
from types import SimpleNamespace
from pathlib import Path


def test_phase6_debug_flags_surface_in_snapshot():
    from puzzleeval import config

    snapshot = config.migration_flags_snapshot()

    assert snapshot["PUZZLEEVAL_FAILURE_PACKET_DEBUG_ENABLED"] is True
    assert snapshot["PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED"] is True


def test_failure_packet_derives_mechanical_tags_not_semantic_routes():
    from puzzleeval.agents.agent5.failure_packets import (
        classify_failure,
        recommended_action,
    )

    docs_gap = classify_failure("docs missing official endpoint reference")
    interaction = classify_failure(
        "live_test saw keepalive ping-only events and no agent response",
        command="python live_test.py",
    )
    credentials = classify_failure("401 unauthorized: invalid api key")
    code_bug = classify_failure("Traceback (most recent call last): KeyError")

    assert docs_gap == "unknown"
    assert recommended_action(docs_gap) == "inspect_evidence"
    assert interaction in {"stream_no_output", "unknown"}
    assert recommended_action(interaction) == "inspect_evidence"
    assert credentials in {"credentials_rejected", "http_401"}
    assert recommended_action(credentials) == "inspect_evidence"
    assert code_bug in {"command_failed", "unknown"}
    assert recommended_action(code_bug) == "inspect_evidence"


def test_failure_packet_artifact_shape_can_use_llm_review(tmp_path: Path):
    from puzzleeval.agents.agent5.failure_packets import write_failure_packet

    packet = write_failure_packet(
        tmp_path,
        turn=11,
        candidate_name="Example",
        failure_source="tool:write_file",
        exit_code=1,
        result_text=(
            "Error: research_synthesis.json failed Phase 3 research artifact "
            "validation. chosen_api_surface must name an endpoint URL."
        ),
        diagnostic_reviewer=lambda _packet: {
            "observed_failure": "research_synthesis.json was rejected",
            "confidence": "high",
            "likely_root_cause": "The artifact is missing required structured fields.",
            "uncertainty": "",
            "next_diagnostic_or_patch": "Rewrite the artifact with the missing endpoint field.",
            "research_would_change_implementation": False,
            "evidence": ["write_file was rejected and persisted=false"],
        },
    )

    assert "artifact_validation_failed" in packet["mechanical_tags"]
    assert packet["diagnosis"]["likely_root_cause"] == "The artifact is missing required structured fields."
    assert packet["llm_diagnostic_review"]["confidence"] == "high"


def test_meaningful_failure_filter_skips_trivial_tool_errors():
    from puzzleeval.agents.agent5.failure_packets import is_meaningful_failure_source

    assert not is_meaningful_failure_source(
        tool_name="read_file",
        exit_code=1,
        result_text="Error: 'notes.txt' does not exist in sandbox.",
        failure_source="tool:read_file",
    )
    assert is_meaningful_failure_source(
        tool_name="run_code",
        exit_code=1,
        result_text="Traceback (most recent call last): KeyError",
        command="python live_test.py",
        failure_source="run_code",
    )


def test_offline_smoke_env_masks_credentials(monkeypatch):
    from puzzleeval.agents.agent5.build_loop import _offline_smoke_env

    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    env = _offline_smoke_env({"ELEVENLABS_API_KEY": "candidate-secret"})

    assert env["PUZZLEEVAL_SMOKE_OFFLINE"] == "1"
    assert env["PUZZLEEVAL_PROVIDER_CALLS_DISABLED"] == "1"
    assert env["OPENAI_API_KEY"] == ""
    assert env["ELEVENLABS_API_KEY"] == ""


def test_anthropic_message_sanitizer_reorders_interleaved_tool_result_text():
    import pytest

    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent5.api_call import (
        sanitize_messages_for_anthropic,
        validate_tool_result_pairing,
    )

    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "write_file", "input": {}},
                {"type": "tool_use", "id": "tu_2", "name": "write_file", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "first failed", "is_error": True},
                {"type": "text", "text": "Structured failure packet written"},
                {"type": "tool_result", "tool_use_id": "tu_2", "content": "second failed", "is_error": True},
            ],
        },
    ]

    repaired = sanitize_messages_for_anthropic(
        messages,
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        trace_id="trace",
        candidate_name="Example",
    )

    assert repaired >= 1
    content = messages[1]["content"]
    assert [block.get("tool_use_id") for block in content[:2]] == ["tu_1", "tu_2"]
    assert content[2]["type"] == "text"
    assert validate_tool_result_pairing(messages) == []


def test_debug_research_ledger_allows_one_worker_per_failure_packet(tmp_path: Path):
    from puzzleeval.agents.agent5.failure_packets import (
        build_failure_packet_research_context,
        debug_research_attempts_for_packet,
        record_debug_research_attempt,
        write_failure_packet,
    )

    packet = write_failure_packet(
        tmp_path,
        turn=3,
        candidate_name="Example",
        failure_source="run_code",
        command="python live_test.py",
        exit_code=1,
        result_text="docs missing official streaming endpoint reference",
    )

    assert debug_research_attempts_for_packet(tmp_path, packet) == 0
    context = build_failure_packet_research_context(packet)
    assert "Attached Failure Packet" in context
    assert "mechanical_tags" in context
    assert "diagnosis" in context

    record_debug_research_attempt(
        tmp_path,
        packet=packet,
        question="Find the official streaming endpoint.",
        turn=4,
    )

    assert debug_research_attempts_for_packet(tmp_path, packet) == 1


def test_failure_packet_diagnoses_stream_timeout_after_output(tmp_path: Path):
    from puzzleeval.agents.agent5.failure_packets import write_failure_packet

    (tmp_path / "harness_forensics.jsonl").write_text(
        "\n".join([
            json.dumps({"event": "stream_event", "dir": "recv", "type": "audio", "t_ms": 1000}),
            json.dumps({"event": "stream_event", "dir": "recv", "type": "agent_response", "t_ms": 1100}),
            json.dumps({"event": "stream_event", "dir": "recv", "type": "ping", "t_ms": 2000}),
            json.dumps({"event": "stream_event", "dir": "send", "type": "pong", "t_ms": 2001}),
        ]),
        encoding="utf-8",
    )

    packet = write_failure_packet(
        tmp_path,
        turn=5,
        candidate_name="Example",
        failure_source="run_code",
        command="python live_test.py",
        exit_code=-1,
        result_text="Error: command timed out after 60 seconds",
    )

    assert "stream_output_then_control" in packet["mechanical_tags"]
    assert packet["forensics_summary"]["stream_summary"]["timeout_relation"] == (
        "timeout_after_output_then_control"
    )
    assert "completion/termination detection" in packet["diagnosis"]["likely_root_cause"]


def test_failure_packet_diagnoses_control_only_stream_timeout_as_research_gap(tmp_path: Path):
    from puzzleeval.agents.agent5.failure_packets import write_failure_packet

    (tmp_path / "harness_forensics.jsonl").write_text(
        "\n".join([
            json.dumps({"event": "stream_event", "dir": "recv", "type": "metadata", "t_ms": 500}),
            json.dumps({"event": "stream_event", "dir": "recv", "type": "ping", "t_ms": 1500}),
            json.dumps({"event": "stream_event", "dir": "send", "type": "pong", "t_ms": 1501}),
        ]),
        encoding="utf-8",
    )

    packet = write_failure_packet(
        tmp_path,
        turn=6,
        candidate_name="Example",
        failure_source="run_code",
        command="python live_test.py",
        exit_code=-1,
        result_text="Error: command timed out after 60 seconds",
    )

    assert "stream_no_output" in packet["mechanical_tags"]
    assert "stream_control_only_timeout" in packet["mechanical_tags"]
    assert packet["forensics_summary"]["stream_summary"]["output_event_count"] == 0
    assert packet["forensics_summary"]["stream_summary"]["control_event_count"] >= 1
    assert packet["diagnosis"]["research_would_change_implementation"] is True


def test_failure_packet_tags_representative_probe_empty_success(tmp_path: Path):
    state = tmp_path / "_agent_state"
    state.mkdir()
    (state / "representative_probe_evidence.json").write_text(
        json.dumps({
            "status": "failed",
            "selected_test_ids": ["tc-1"],
            "results": [{
                "test_case_id": "tc-1",
                "family_key": "input=voice_conversation|output=audio_content|mode=agentic",
                "status": "failed",
                "input_observation": {
                    "requires_observable_output": True,
                    "empty_success_without_output_evidence": True,
                },
                "payloads_supplied_to_harness": [{"has_audio": True}],
                "raw_result": {"success": True, "output_chars": 0, "audio_path_count": 0},
                "verdict": {"passed": False, "reasoning": "no substantive output"},
            }],
        }),
        encoding="utf-8",
    )
    from puzzleeval.agents.agent5.failure_packets import write_failure_packet

    packet = write_failure_packet(
        tmp_path,
        turn=7,
        candidate_name="Example",
        failure_source="completion_gate",
        command="HARNESS_COMPLETE",
        result_text="representative_probe failed",
        issues=["representative_probe failed"],
    )

    assert "representative_probe_failed" in packet["mechanical_tags"]
    assert "empty_success_output" in packet["mechanical_tags"]
    assert "success without usable output" in packet["diagnosis"]["observed_failure"]


def test_summarize_forensics_separates_output_from_control_events(tmp_path: Path):
    from puzzleeval.agents.agent5.tools import summarize_forensics

    (tmp_path / "harness_forensics.jsonl").write_text(
        "\n".join([
            json.dumps({"event": "stream_event", "dir": "recv", "type": "audio", "t_ms": 100}),
            json.dumps({"event": "stream_event", "dir": "recv", "type": "ping", "t_ms": 200}),
            json.dumps({"event": "stream_event", "dir": "send", "type": "pong", "t_ms": 201}),
        ]),
        encoding="utf-8",
    )

    summary = summarize_forensics({}, tmp_path)

    assert "stream_events: output=1, control=2" in summary
    assert "last_meaningful_output_event" in summary
    assert "last_control_event" in summary


def test_abandon_candidate_validator_requires_external_evidence(tmp_path: Path):
    from puzzleeval.agents.agent5.abandon_candidate import (
        validate_abandon_candidate_text,
    )

    self_assertion = json.dumps({
        "reason": "provider_blocked",
        "summary": "The provider account is blocked and further patching cannot fix it.",
        "evidence": [{"note": "I think this is blocked"}],
    })
    assert not validate_abandon_candidate_text(
        self_assertion,
        sandbox_dir=tmp_path,
    ).ok

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "latest_failure_packet.json").write_text("{}", encoding="utf-8")
    anchored = json.dumps({
        "reason": "provider_blocked",
        "summary": "The provider rejected access, so the candidate cannot be repaired by code patches.",
        "evidence": ["_agent_state/latest_failure_packet.json"],
    })

    assert validate_abandon_candidate_text(anchored, sandbox_dir=tmp_path).ok


def test_write_file_validates_abandon_candidate_and_flag_zero_blocks(
    tmp_path: Path,
    monkeypatch,
):
    from puzzleeval import config
    from puzzleeval.agents.agent5.tools import write_file

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "latest_failure_packet.json").write_text("{}", encoding="utf-8")
    content = json.dumps({
        "reason": "credentials_unavailable",
        "summary": "The provider rejected credentials, so more code patches cannot create valid access.",
        "evidence": ["_agent_state/latest_failure_packet.json"],
    })

    result = write_file(
        {"filename": "_agent_state/abandon_candidate.json", "content": content},
        tmp_path,
        phase_state={"api_spec_written": True, "implementation_plan_accepted": True},
    )
    assert result.startswith("Written ")

    monkeypatch.setenv("PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED", "0")
    importlib.reload(config)
    try:
        blocked = write_file(
            {"filename": "_agent_state/abandon_candidate.json", "content": content},
            tmp_path,
            phase_state={"api_spec_written": True, "implementation_plan_accepted": True},
        )
        assert "PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED=0" in blocked
    finally:
        monkeypatch.delenv("PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED", raising=False)
        importlib.reload(config)


def test_abandon_target_helper_accepts_only_exact_agent_state_path():
    from types import SimpleNamespace

    import pytest

    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent5.build_loop import _tool_targets_relative_path

    assert _tool_targets_relative_path(
        SimpleNamespace(
            name="write_file",
            input={"filename": "_agent_state/abandon_candidate.json"},
        ),
        "_agent_state/abandon_candidate.json",
    )
    assert _tool_targets_relative_path(
        SimpleNamespace(
            name="patch_file",
            input={"path": "\\_agent_state\\abandon_candidate.json"},
        ),
        "_agent_state/abandon_candidate.json",
    )
    assert not _tool_targets_relative_path(
        SimpleNamespace(name="read_file", input={"filename": "_agent_state/abandon_candidate.json"}),
        "_agent_state/abandon_candidate.json",
    )
    assert not _tool_targets_relative_path(
        SimpleNamespace(name="write_file", input={"filename": "abandon_candidate.json"}),
        "_agent_state/abandon_candidate.json",
    )
