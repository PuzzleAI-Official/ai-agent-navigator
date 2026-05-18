from __future__ import annotations

import importlib
import json
from pathlib import Path
import re

from puzzleeval.agents.agent5.runtime_policy import (
    RUNTIME_PERSISTENT_WORKER,
    RUNTIME_SINGLE_CALL,
    select_runtime_primitive,
)
from puzzleeval.tool_plugins import (
    HARNESS_EXECUTION_MULTI_TURN_PERSISTENT,
    HARNESS_EXECUTION_MULTI_TURN_SERIALIZED,
    HARNESS_EXECUTION_PERSISTENT_WORKER,
    HARNESS_EXECUTION_SINGLE_CALL,
    get_plugin,
)

PLAYBOOK_DIR = Path(__file__).resolve().parents[1] / "puzzleeval" / "capability_playbooks"


def _write_plan(tmp_path: Path, *, state_owner: str, known_family: str = "continuous_stream") -> None:
    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "implementation_plan.json").write_text(
        json.dumps({
            "schema_version": 1,
            "interaction_pattern": {
                "known_family": known_family,
                "state_owner": state_owner,
                "input_clocking": "one user turn per payload",
                "output_completion_signal": "provider completion event",
            },
        }),
        encoding="utf-8",
    )


def test_legacy_plugin_mode_constants_are_binary_shims():
    assert HARNESS_EXECUTION_SINGLE_CALL == "single_call"
    assert HARNESS_EXECUTION_PERSISTENT_WORKER == "persistent_worker"
    assert HARNESS_EXECUTION_MULTI_TURN_SERIALIZED == HARNESS_EXECUTION_SINGLE_CALL
    assert HARNESS_EXECUTION_MULTI_TURN_PERSISTENT == HARNESS_EXECUTION_PERSISTENT_WORKER


def test_runtime_selection_uses_harness_process_plan_state(tmp_path: Path):
    _write_plan(tmp_path, state_owner="harness_process")

    decision = select_runtime_primitive(sandbox_dir=tmp_path, persistent_worker_enabled=True)

    assert decision.mode == RUNTIME_PERSISTENT_WORKER
    assert decision.source == "implementation_plan"
    assert decision.degraded is False


def test_runtime_selection_uses_single_call_for_provider_server_state(tmp_path: Path):
    _write_plan(tmp_path, state_owner="provider_server", known_family="serialized_conversation")

    decision = select_runtime_primitive(sandbox_dir=tmp_path, persistent_worker_enabled=True)

    assert decision.mode == RUNTIME_SINGLE_CALL
    assert decision.source == "implementation_plan"


def test_implementation_plan_state_owner_overrides_plugin_fallback(tmp_path: Path):
    voice_caps = get_plugin("voice_realtime").capabilities()
    chat_caps = get_plugin("conversation_simulator").capabilities()

    _write_plan(tmp_path, state_owner="provider_server", known_family="serialized_conversation")
    voice_decision = select_runtime_primitive(
        sandbox_dir=tmp_path,
        caps=voice_caps,
        persistent_worker_enabled=True,
    )

    assert voice_decision.mode == RUNTIME_SINGLE_CALL
    assert voice_decision.source == "implementation_plan"

    _write_plan(tmp_path, state_owner="harness_process", known_family="continuous_stream")
    chat_decision = select_runtime_primitive(
        sandbox_dir=tmp_path,
        caps=chat_caps,
        persistent_worker_enabled=True,
    )

    assert chat_decision.mode == RUNTIME_PERSISTENT_WORKER
    assert chat_decision.source == "implementation_plan"


def test_runtime_selection_flag_off_degrades_harness_process_to_single_call(tmp_path: Path):
    _write_plan(tmp_path, state_owner="harness_process")

    decision = select_runtime_primitive(sandbox_dir=tmp_path, persistent_worker_enabled=False)

    assert decision.mode == RUNTIME_SINGLE_CALL
    assert decision.degraded is True
    assert "PERSISTENT_WORKER_RUNTIME" in decision.reason


def test_plugin_capability_is_only_compatibility_fallback_without_plan():
    voice_caps = get_plugin("voice_realtime").capabilities()
    chat_caps = get_plugin("conversation_simulator").capabilities()

    voice_decision = select_runtime_primitive(caps=voice_caps, persistent_worker_enabled=True)
    chat_decision = select_runtime_primitive(caps=chat_caps, persistent_worker_enabled=True)

    assert voice_decision.mode == RUNTIME_PERSISTENT_WORKER
    assert voice_decision.source == "plugin_capability"
    assert chat_decision.mode == RUNTIME_SINGLE_CALL


def test_implement_test_env_policy_reads_phase5_flag(monkeypatch):
    from puzzleeval.agents import implement_test_env

    monkeypatch.setenv("PUZZLEEVAL_PERSISTENT_WORKER_RUNTIME_ENABLED", "0")
    import puzzleeval.config as cfg

    importlib.reload(cfg)
    importlib.reload(implement_test_env)

    voice_caps = get_plugin("voice_realtime").capabilities()
    assert not implement_test_env._should_start_persistent_harness_session(
        voice_caps,
        "voice_conversation",
        "voice_conversation",
    )

    monkeypatch.setenv("PUZZLEEVAL_PERSISTENT_WORKER_RUNTIME_ENABLED", "1")
    importlib.reload(cfg)
    importlib.reload(implement_test_env)


def test_implement_test_env_uses_plan_state_owner_for_voice_runtime(tmp_path: Path, monkeypatch):
    from puzzleeval.agents import implement_test_env

    monkeypatch.setattr(implement_test_env, "PERSISTENT_HARNESS_RUNNER_ENABLED", True)
    monkeypatch.setattr(implement_test_env, "PERSISTENT_WORKER_RUNTIME_ENABLED", True)
    voice_caps = get_plugin("voice_realtime").capabilities()

    _write_plan(tmp_path, state_owner="provider_server", known_family="serialized_conversation")
    assert not implement_test_env._should_start_persistent_harness_session(
        voice_caps,
        "voice_conversation",
        "voice_conversation",
        sandbox_dir=tmp_path,
    )

    _write_plan(tmp_path, state_owner="harness_process", known_family="continuous_stream")
    assert implement_test_env._should_start_persistent_harness_session(
        voice_caps,
        "voice_conversation",
        "voice_conversation",
        sandbox_dir=tmp_path,
    )


def test_active_prompt_demotes_old_multi_call_runtime_recipe():
    src = (
        PLAYBOOK_DIR.parents[0] / "agents" / "implement_test_env.py"
    ).read_text(encoding="utf-8")

    assert "CONVERSATION RUNTIME AND EVIDENCE NOTE" in src
    assert "MULTI-CALL HARNESS CONTRACT" not in src
    assert "implementation_plan.json must" in src
    assert "state_owner=provider_server" in src
    assert "state_owner=harness_process" in src


def test_phase5_playbooks_are_outcome_contracts_not_implementation_recipes():
    texts = {
        name: (PLAYBOOK_DIR / name).read_text(encoding="utf-8")
        for name in ("voice.md", "streaming_response.md", "live_test_voice.md")
    }
    combined = "\n".join(texts.values())

    assert "```python" not in combined
    assert "```javascript" not in combined
    assert "PersistentLiveRunner" not in combined
    assert "subprocess.Popen" not in combined
    assert "harness.run({" not in combined
    for provider_name in ("ElevenLabs", "OpenAI Realtime", "Vapi", "Retell", "Bland"):
        assert provider_name not in combined
    assert not re.search(r"(?m)^\s*\d+\.\s+(open|send|wait|receive|call|run)\b", combined.lower())
    assert not re.search(r"\btimeout\s*=\s*\d+|\bretries\s*=\s*\d+", combined.lower())


def test_phase5_playbooks_still_define_outcome_evidence():
    voice = (PLAYBOOK_DIR / "voice.md").read_text(encoding="utf-8")
    streaming = (PLAYBOOK_DIR / "streaming_response.md").read_text(encoding="utf-8")
    live = (PLAYBOOK_DIR / "live_test_voice.md").read_text(encoding="utf-8")

    assert "audio_bytes" in voice
    assert "audio_path" in voice
    assert "persistent_worker" in voice
    assert "output_completion_signal" in streaming
    assert "state_owner" in streaming
    assert "production-equivalence evidence" in live.lower()
    assert "task-equivalence evidence" in live.lower()
