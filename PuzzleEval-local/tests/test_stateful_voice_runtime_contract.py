from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from puzzleeval.tool_plugins import (
    HARNESS_EXECUTION_PERSISTENT_WORKER,
    HARNESS_EXECUTION_SINGLE_CALL,
    get_plugin,
)
from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin


def test_voice_realtime_declares_persistent_execution_mode():
    caps = get_plugin("voice_realtime").capabilities()
    assert caps.requires_harness_runner is True
    assert caps.harness_execution_mode == HARNESS_EXECUTION_PERSISTENT_WORKER


def test_conversation_simulator_uses_single_call_runtime_primitive():
    caps = get_plugin("conversation_simulator").capabilities()
    assert caps.requires_harness_runner is True
    assert caps.harness_execution_mode == HARNESS_EXECUTION_SINGLE_CALL


def test_persistent_runner_policy_uses_execution_mode(monkeypatch):
    pytest.importorskip("anthropic")
    from puzzleeval.agents import implement_test_env

    monkeypatch.setattr(implement_test_env, "PERSISTENT_HARNESS_RUNNER_ENABLED", True)

    voice_caps = get_plugin("voice_realtime").capabilities()
    chat_caps = get_plugin("conversation_simulator").capabilities()

    assert implement_test_env._should_start_persistent_harness_session(
        voice_caps,
        "voice_conversation",
        "voice_conversation",
    )
    assert not implement_test_env._should_start_persistent_harness_session(
        chat_caps,
        "conversation",
        "free_text",
    )


def test_tool_runner_fallback_detects_only_persistent_modality_owners(monkeypatch):
    pytest.importorskip("anthropic")
    from puzzleeval.agents import implement_test_env

    monkeypatch.setattr(implement_test_env, "PERSISTENT_HARNESS_RUNNER_ENABLED", True)

    assert implement_test_env._has_persistent_harness_owner(
        "voice_conversation",
        "voice_conversation",
    )
    assert not implement_test_env._has_persistent_harness_owner(
        "conversation",
        "free_text",
    )


def test_voice_turn_payload_includes_safe_context_and_history(monkeypatch, tmp_path):
    plugin = VoiceRealtimePlugin()
    plugin.set_session_dir(tmp_path)
    monkeypatch.setattr(
        VoiceRealtimePlugin,
        "_synthesize_caller_audio",
        lambda self, text, token: str(tmp_path / f"caller_{token}.mp3"),
    )
    monkeypatch.setattr(
        VoiceRealtimePlugin,
        "public_url_for_audio",
        lambda self, token: f"http://localhost/audio/{token}",
    )
    monkeypatch.setattr(
        VoiceRealtimePlugin,
        "submit_merge_in_background",
        lambda self, *, session_token, turns: None,
    )

    payloads = []

    def harness_runner(payload):
        payloads.append(payload)
        turn_index = payload["turn_index"]
        return {
            "success": True,
            "output": "Thanks for calling Bean & Brew" if turn_index == 0 else "I have your name from earlier.",
            "raw_response": {},
            "latency_ms": 10,
        }

    verdict = plugin.evaluate_output(
        response={},
        expected={
            "turns": [
                {"user_text": "Hi", "expected_agent_contains": "thanks"},
                {"user_text": "My name is Dean", "expected_agent_contains": "name"},
            ],
            "input_context": {"instructions": "Greet once, then keep context."},
            "rubric": "must-not-leak",
        },
        harness_runner=harness_runner,
    )

    assert verdict.passed is True
    assert len(payloads) == 2
    assert payloads[0]["input_context"]["instructions"] == "Greet once, then keep context."
    assert payloads[1]["input_context"]["instructions"] == "Greet once, then keep context."
    assert payloads[1]["conversation_history"] == [
        {"role": "user", "content": "Hi", "turn_index": 0},
        {"role": "assistant", "content": "Thanks for calling Bean & Brew", "turn_index": 0},
        {"role": "user", "content": "My name is Dean", "turn_index": 1},
    ]
    assert "rubric" not in payloads[0]
    assert "expected" not in payloads[0]


def test_agentic_voice_releases_worker_and_submits_merge_before_judge(monkeypatch, tmp_path):
    from puzzleeval.schemas import (
        Persona,
        RubricCriterion,
        RubricScore,
        RubricVerdict,
        SimulatorTurn,
    )
    import puzzleeval.rubric_judge as rubric_judge
    import puzzleeval.user_simulator as user_simulator

    plugin = VoiceRealtimePlugin()
    plugin.set_session_dir(tmp_path)
    events: list[str] = []

    monkeypatch.setattr(
        VoiceRealtimePlugin,
        "_synthesize_caller_audio",
        lambda self, text, token: str(tmp_path / f"caller_{token}.mp3"),
    )
    monkeypatch.setattr(
        VoiceRealtimePlugin,
        "public_url_for_audio",
        lambda self, token: f"http://localhost/audio/{token}",
    )

    def fake_merge(self, *, session_token, turns):
        events.append("merge")
        return None

    def fake_release():
        events.append("release")

    def fake_progress(event_type, payload):
        events.append(event_type)

    def fake_simulator(**kwargs):
        return SimulatorTurn(
            text="I need help with my order.",
            end_conversation=False,
            end_reason="ongoing",
            cost_usd=0.001,
        )

    def fake_judge(**kwargs):
        events.append("judge")
        assert "merge" in events
        assert "release" in events
        return RubricVerdict(
            overall_score=1.0,
            passed=True,
            criterion_scores=[
                RubricScore(
                    criterion_name="goal_completion",
                    score=1.0,
                    reasoning="Handled the request.",
                    evidence_turn_indices=[1],
                )
            ],
            conversation_summary="Agent helped the caller.",
            critical_failures=[],
            cost_usd=0.002,
        )

    monkeypatch.setattr(VoiceRealtimePlugin, "submit_merge_in_background", fake_merge)
    monkeypatch.setattr(user_simulator, "generate_next_user_turn", fake_simulator)
    monkeypatch.setattr(rubric_judge, "judge_conversation", fake_judge)

    result = plugin.drive_conversation(
        agent_responder=lambda turn_idx, audio_url, state: {"text": "I can help with that."},
        persona=Persona(
            name="Dean",
            demographics="busy customer",
            emotional_state="neutral",
        ),
        goal="Get help with an order",
        constraints=[],
        rubric=[
            RubricCriterion(
                name="goal_completion",
                description="The agent helps the caller.",
                weight=1.0,
            )
        ],
        max_turns=1,
        evaluation_mode="agentic",
        trace_id="test-trace",
        progress_callback=fake_progress,
        release_harness_session=fake_release,
    )

    assert result["overall_passed"] is True
    assert events.count("merge") == 1
    assert events.index("merge") < events.index("judge")
    assert events.index("release") < events.index("judge")
    assert "merge_submitted" in events
    assert "provider_turn_completed" in events
    assert "rubric_judge_started" in events
    assert "rubric_judge_completed" in events


def test_rubric_judge_deterministic_precheck_skips_llm_for_blank_agent_turns():
    from puzzleeval.rubric_judge import judge_conversation
    from puzzleeval.schemas import ConversationTurn, Persona, RubricCriterion

    verdict = judge_conversation(
        transcript=[
            ConversationTurn(turn_index=0, role="user", text="Hello"),
            ConversationTurn(turn_index=1, role="agent", text=""),
        ],
        persona=Persona(
            name="Dean",
            demographics="busy customer",
            emotional_state="neutral",
        ),
        goal="Get help",
        rubric=[
            RubricCriterion(
                name="goal_completion",
                description="The agent helps the caller.",
                weight=1.0,
                critical=True,
                min_passing_score=0.5,
            )
        ],
    )

    assert verdict.passed is False
    assert verdict.cost_usd == 0.0
    assert verdict.critical_failures == ["goal_completion"]
    assert verdict.criterion_scores[0].score == 0.0


def test_rubric_judge_uses_bounded_client_when_client_omitted(monkeypatch):
    from puzzleeval import rubric_judge
    from puzzleeval.config import (
        RUBRIC_JUDGE_MAX_RETRIES,
        RUBRIC_JUDGE_TIMEOUT_S,
    )
    from puzzleeval.schemas import (
        ConversationTurn,
        Persona,
        RubricCriterion,
        RubricScore,
        RubricVerdict,
    )

    calls: dict[str, object] = {}
    fake_client = SimpleNamespace()

    verdict = RubricVerdict(
        overall_score=1.0,
        passed=True,
        criterion_scores=[
            RubricScore(
                criterion_name="goal_completion",
                score=1.0,
                reasoning="Handled the request.",
                evidence_turn_indices=[1],
            )
        ],
        conversation_summary="Agent helped the caller.",
        critical_failures=[],
        cost_usd=0.0,
    )

    def fake_build_client(**kwargs):
        calls.update(kwargs)
        return fake_client

    def fake_parse_with_fallback(**kwargs):
        assert kwargs["client"] is fake_client
        return SimpleNamespace(
            parsed_output=verdict,
            content=[],
            stop_reason="end_turn",
        )

    monkeypatch.setattr(rubric_judge, "build_client", fake_build_client)
    monkeypatch.setattr(rubric_judge, "parse_with_fallback", fake_parse_with_fallback)
    monkeypatch.setattr(rubric_judge, "log_llm_call", lambda *args, **kwargs: 0.003)

    result = rubric_judge.judge_conversation(
        transcript=[
            ConversationTurn(turn_index=0, role="user", text="I need help."),
            ConversationTurn(turn_index=1, role="agent", text="I can help."),
        ],
        persona=Persona(
            name="Dean",
            demographics="busy customer",
            emotional_state="neutral",
        ),
        goal="Get help",
        rubric=[
            RubricCriterion(
                name="goal_completion",
                description="The agent helps the caller.",
                weight=1.0,
            )
        ],
    )

    assert result.passed is True
    assert calls["timeout"] == RUBRIC_JUDGE_TIMEOUT_S
    assert calls["max_retries"] == RUBRIC_JUDGE_MAX_RETRIES


def test_agent6_dispatch_has_whole_test_timeout_contract():
    import inspect
    from puzzleeval.agents import implement_test_env
    from puzzleeval.config import AGENT6_WHOLE_TEST_TIMEOUT

    src = inspect.getsource(implement_test_env.run_implement_test_env_agent)
    assert AGENT6_WHOLE_TEST_TIMEOUT > 0
    assert "AGENT6_WHOLE_TEST_TIMEOUT" in src
    assert "test_case_timeout" in src
    assert "whole_test_timeout" in src


def test_session_continuity_gate_flags_late_session_create(tmp_path):
    from puzzleeval.agents.agent5.verification import (
        verify_session_continuity_from_forensics,
    )

    log = tmp_path / "harness_forensics.jsonl"
    log.write_text(
        "\n".join([
            '{"event":"session_create","turn_index":0,"session_token":"abc"}',
            '{"event":"session_create","turn_index":1,"session_token":"abc"}',
        ]),
        encoding="utf-8",
    )

    warning = verify_session_continuity_from_forensics(tmp_path, session_token="abc")
    assert warning is not None
    assert "later turn" in warning


def test_session_continuity_gate_accepts_create_then_reuse(tmp_path):
    from puzzleeval.agents.agent5.verification import (
        verify_session_continuity_from_forensics,
    )

    log = tmp_path / "harness_forensics.jsonl"
    log.write_text(
        "\n".join([
            '{"event":"session_create","turn_index":0,"session_token":"abc"}',
            '{"event":"session_reuse","turn_index":1,"session_token":"abc"}',
        ]),
        encoding="utf-8",
    )

    assert verify_session_continuity_from_forensics(tmp_path, session_token="abc") is None


def test_session_continuity_gate_understands_traced_op_events(tmp_path):
    from puzzleeval.agents.agent5.verification import (
        verify_session_continuity_from_forensics,
    )

    log = tmp_path / "harness_forensics.jsonl"
    log.write_text(
        "\n".join([
            '{"event":"op_start","op":"session_create","turn_index":0,"session_token":"abc"}',
            '{"event":"op_done","op":"session_create","turn_index":0,"session_token":"abc"}',
            '{"event":"session_reuse","turn_index":1,"session_token":"abc"}',
        ]),
        encoding="utf-8",
    )

    assert verify_session_continuity_from_forensics(tmp_path, session_token="abc") is None


def test_stream_keepalive_diagnostic_flags_keepalive_until_timeout(tmp_path):
    from puzzleeval.agents.agent5.verification import (
        verify_stream_keepalive_only_from_forensics,
    )

    log = tmp_path / "harness_forensics.jsonl"
    log.write_text(
        "\n".join([
            '{"event":"stream_event","dir":"recv","type":"ping","session_token":"abc"}',
            '{"event":"stream_event","dir":"recv","type":"pong","session_token":"abc"}',
            '{"event":"stream_event","dir":"recv","type":"heartbeat","session_token":"abc"}',
            '{"event":"timeout","error":"hard timeout","session_token":"abc"}',
        ]),
        encoding="utf-8",
    )

    warning = verify_stream_keepalive_only_from_forensics(
        tmp_path,
        session_token="abc",
    )
    assert warning is not None
    assert "keepalive" in warning
    assert "timeout" in warning


def test_external_provider_block_classifier_flags_quota_evidence(tmp_path):
    from puzzleeval.agents.agent5.verification import (
        classify_external_provider_block_from_forensics,
    )

    log = tmp_path / "harness_forensics.jsonl"
    log.write_text(
        '{"event":"op_error","op":"session_create","turn_index":0,'
        '"session_token":"abc","error":"quota exceeded: insufficient credits"}',
        encoding="utf-8",
    )

    result = classify_external_provider_block_from_forensics(tmp_path, session_token="abc")
    assert result is not None
    assert result["status"] == "external_provider_blocked"
    assert "quota" in result["reason"] or "provider" in result["reason"]


def test_external_provider_block_classifier_uses_keepalive_diagnostic(tmp_path):
    from puzzleeval.agents.agent5.verification import (
        classify_external_provider_block_from_forensics,
    )

    log = tmp_path / "harness_forensics.jsonl"
    log.write_text(
        "\n".join([
            '{"event":"stream_event","dir":"recv","type":"metadata","session_token":"abc"}',
            '{"event":"stream_event","dir":"recv","type":"ping","session_token":"abc"}',
            '{"event":"stream_event","dir":"recv","type":"pong","session_token":"abc"}',
            '{"event":"timeout","error":"hard timeout","session_token":"abc"}',
        ]),
        encoding="utf-8",
    )

    result = classify_external_provider_block_from_forensics(tmp_path, session_token="abc")
    assert result is not None
    assert result["status"] == "external_provider_blocked"
    assert "keepalive" in result["reason"]


def test_stream_keepalive_diagnostic_ignores_normal_output(tmp_path):
    from puzzleeval.agents.agent5.verification import (
        verify_stream_keepalive_only_from_forensics,
    )

    log = tmp_path / "harness_forensics.jsonl"
    log.write_text(
        "\n".join([
            '{"event":"stream_event","dir":"recv","type":"ping","session_token":"abc"}',
            '{"event":"stream_event","dir":"recv","type":"response.audio.delta","session_token":"abc"}',
            '{"event":"stream_event","dir":"recv","type":"response.done","session_token":"abc"}',
        ]),
        encoding="utf-8",
    )

    assert (
        verify_stream_keepalive_only_from_forensics(tmp_path, session_token="abc")
        is None
    )


def test_live_test_voice_contract_uses_persistent_runner():
    playbook = (
        Path(__file__).resolve().parents[1]
        / "puzzleeval"
        / "capability_playbooks"
        / "live_test_voice.md"
    )
    text = playbook.read_text(encoding="utf-8")

    assert "persistent_worker" in text
    assert "production equivalence" in text.lower()
    assert "harness.run({" not in text
