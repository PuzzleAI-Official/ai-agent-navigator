"""Architecture guards for the Agent 5 cleanup boundary."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent


def test_agent5_playbook_router_selects_capabilities_without_legacy_import():
    from puzzleeval.agents.agent5.playbooks import (
        compose_capability_playbooks,
        selected_playbook_ids,
    )

    voice_cases = [{"input_type": "voice_conversation", "output_type": "voice_turn"}]
    assert selected_playbook_ids(voice_cases) == [
        "voice",
        "streaming_response",
        "live_test_voice",
    ]
    rendered = compose_capability_playbooks(voice_cases)
    assert "Voice Harness Outcome Contract" in rendered
    assert "Streaming Response Outcome Contract" in rendered
    assert "Voice Live-Test Outcome Contract" in rendered


def test_agent5_builder_prompt_template_is_loaded_from_package():
    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent5.prompts import load_builder_system_prompt
    from puzzleeval.agents.implement_test_env import BUILDER_SYSTEM_PROMPT

    template = load_builder_system_prompt()
    assert template == BUILDER_SYSTEM_PROMPT
    assert "PHASE 1: RESEARCH" in template
    # Phase 1.B + 1.C: legacy __MODALITY_CONTRACT__ + __OS_SPECIFIC_RULES__
    # placeholders collapsed into a single trailing __CONTRACT_BLOCK__ for
    # cache-prefix optimization. The dual-placeholder layout is gone from
    # the canonical template.
    assert "__CONTRACT_BLOCK__" in template
    assert "__MODALITY_CONTRACT__" not in template
    assert "__OS_SPECIFIC_RULES__" not in template


def test_phase8_builder_prompt_retires_api_spec_and_checklist():
    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent5.prompts import load_builder_system_prompt

    template = load_builder_system_prompt()
    forbidden = (
        "how to ship api_spec.txt",
        "Then write api_spec.txt",
        "VERY NEXT write_file MUST be harness.py",
        "first response to api_spec.txt",
        "let live calls speak",
        "Phase 2's live test is more informative",
        "API SPEC (Phase D)",
        "selected_endpoint from BuildReadinessChecklist",
        "always from api_spec.txt",
    )
    for phrase in forbidden:
        assert phrase not in template

    assert "Implementation-plan readiness criteria" in template
    assert "Degraded rollback helper notes" not in template
    assert "IMPLEMENTATION PLAN SUMMARY" in template
    assert "api_spec.txt" not in template
    assert "BuildReadinessChecklist" not in template


def test_phase8_default_policy_flags_disable_stale_active_paths():
    from puzzleeval import config as cfg

    assert cfg.RESEARCH_WORKERS_ENABLED is True
    assert cfg.AUTONOMY_BUILD_PLAN_DIRECTIVES_ENABLED is False
    assert cfg.DIRECTIVE_SUPPRESS_ON_AGREEMENT_ENABLED is False
    assert not hasattr(cfg, "IMPLEMENTATION_PLAN_GATE_ENABLED")
    assert not hasattr(cfg, "AGENT4_DOCS_ENTRYPOINT_ENABLED")


def _build_selection_candidate(
    name: str,
    *,
    provider: str,
    score: float,
    docs_url: str = "https://docs.example.test/api",
    auth_method: str = "api_key",
):
    from puzzleeval.schemas import ScreenedCandidate

    return ScreenedCandidate(
        name=name,
        provider=provider,
        description=f"{name} API",
        pricing_model="usage-based",
        pricing_details=None,
        claimed_capabilities=["test"],
        relevance_score=score,
        adoption_difficulty="medium",
        relevant_subtasks=["test"],
        source="https://example.test",
        verified_api_docs_url=docs_url,
        auth_method=auth_method,
        api_access_method="free_tier",
        confirmed_capabilities=["test"],
        rate_limit_info=None,
        data_format_notes="JSON request/response.",
        screening_notes="Verified docs available.",
    )


def _stage_fetch_verified_docs(tmp_path: Path, trace_id: str, candidate: ScreenedCandidate) -> None:
    from puzzleeval.web_doc_cache import candidate_sandbox_dir

    sandbox = candidate_sandbox_dir(trace_id, candidate.name, runs_root=tmp_path)
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "fetched_docs_0.txt").write_text(
        f"# Fetched from: {candidate.verified_api_docs_url}\n"
        "# Evidence status: fetched_current_api_docs\n\n"
        "POST /v1/respond\nAuthorization: Bearer $KEY\nJSON response schema.",
        encoding="utf-8",
    )


def test_orchestrator_selection_blocks_missing_docs_and_credentials(tmp_path: Path):
    from puzzleeval.selection import select_agent5_build_candidates

    selected = _build_selection_candidate(
        "Alpha API", provider="Alpha", score=0.9
    )
    missing_docs = _build_selection_candidate(
        "No Docs API", provider="NoDocs", score=1.0, docs_url=""
    )
    missing_creds = _build_selection_candidate(
        "No Creds API", provider="NoCreds", score=0.8
    )
    trace_id = "trace-selection"
    _stage_fetch_verified_docs(tmp_path, trace_id, selected)
    _stage_fetch_verified_docs(tmp_path, trace_id, missing_creds)

    result = select_agent5_build_candidates(
        [missing_docs, missing_creds, selected],
        trace_id=trace_id,
        runs_root=tmp_path,
        provider_credentials={"alpha": {"ALPHA_API_KEY": "secret"}},
        max_candidates=4,
    )

    assert [c.name for c in result.selected] == ["Alpha API"]
    assert [c.name for c in result.blocked_missing_docs] == ["No Docs API"]
    assert [c.name for c in result.blocked_missing_credentials] == ["No Creds API"]
    assert result.event_payload() == {
        "selected": ["Alpha API"],
        "blocked_missing_docs": ["No Docs API"],
        "blocked_missing_credentials": ["No Creds API"],
        "dropped_by_rank": [],
    }
    assert result.audit_payload["selection_owner"] == "orchestrator"


def test_orchestrator_selection_sorts_and_applies_max_candidates(tmp_path: Path):
    from puzzleeval.selection import select_agent5_build_candidates

    beta = _build_selection_candidate("Beta API", provider="Beta", score=0.75)
    alpha = _build_selection_candidate("Alpha API", provider="Alpha", score=0.95)
    gamma = _build_selection_candidate("Gamma API", provider="Gamma", score=0.60)
    trace_id = "trace-rank"
    for candidate in (beta, alpha, gamma):
        _stage_fetch_verified_docs(tmp_path, trace_id, candidate)

    result = select_agent5_build_candidates(
        [beta, gamma, alpha],
        trace_id=trace_id,
        runs_root=tmp_path,
        provider_credentials={
            "alpha": {"KEY": "a"},
            "beta": {"KEY": "b"},
            "gamma": {"KEY": "g"},
        },
        max_candidates=2,
    )

    assert [c.name for c in result.selected] == ["Alpha API", "Beta API"]
    assert [c.name for c in result.dropped_by_rank] == ["Gamma API"]


def test_agent5_model_selection_uses_builder_model_from_turn_zero():
    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent5.build_loop import _select_agent5_model

    assert _select_agent5_model(
        research_model="sonnet",
        builder_model="opus",
    ) == "opus"
    assert _select_agent5_model(
        research_model="sonnet",
        builder_model="opus",
    ) == "opus"


def test_research_plan_hook_accepts_dot_prefixed_agent_state_path():
    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent5.build_loop import _tool_targets_relative_path

    block = SimpleNamespace(
        name="write_file",
        input={"filename": "./_agent_state/research_plan.json"},
    )

    assert _tool_targets_relative_path(block, "_agent_state/research_plan.json") is True


def test_planned_research_workers_receive_durable_whole_picture_context(tmp_path: Path):
    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent5.build_loop import _planned_research_context_packet

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "objective.md").write_text("Objective for Provider X", encoding="utf-8")
    (state_dir / "docs_entrypoint.json").write_text(
        '{"url":"https://docs.example.test"}',
        encoding="utf-8",
    )
    (state_dir / "research_handoff.json").write_text(
        '{"canonical_docs_urls":["https://docs.example.test"]}',
        encoding="utf-8",
    )
    (state_dir / "research_synthesis.json").write_text(
        '{"facts":[{"claim":"Use bearer auth"}]}',
        encoding="utf-8",
    )

    packet = _planned_research_context_packet(tmp_path)

    assert "WHOLE_PICTURE_CONTEXT" in packet
    assert "_agent_state/objective.md" in packet
    assert "_agent_state/docs_entrypoint.json" in packet
    assert "_agent_state/research_handoff.json" in packet
    assert "_agent_state/research_synthesis.json" in packet
    assert "Do not re-research confirmed facts" in packet


def test_phase8_implement_test_env_uses_canonical_tool_extension_policy():
    impl_src = (ROOT / "puzzleeval" / "agents" / "implement_test_env.py").read_text(
        encoding="utf-8"
    )

    assert "ALLOWED_EXTENSIONS = _agent5_tools.ALLOWED_EXTENSIONS" in impl_src
    assert "ALLOWED_EXTENSIONS = {" not in impl_src


def test_agent5_playbook_router_does_not_pollute_rest_cases():
    from puzzleeval.agents.agent5.playbooks import (
        compose_capability_playbooks,
        selected_playbook_ids,
    )

    rest_cases = [{"input_type": "file", "output_type": "structured_json"}]
    assert selected_playbook_ids(rest_cases) == []
    assert compose_capability_playbooks(rest_cases) == ""


def test_plain_text_conversation_does_not_load_voice_or_live_test_voice():
    """Per coverage.py:130-139, plain text `conversation` needs streaming_response only.

    Pre-fix bug: voice.md and live_test_voice.md both listed `conversation`
    in their trigger_types, so chatbot/text-conversation candidates loaded the
    full voice playbook stack — adding ~3,300 tokens of irrelevant audio
    contracts to a text-only build's prompt.

    This test pins the corrected selector behavior.
    """
    from puzzleeval.agents.agent5.playbooks import (
        compose_capability_playbooks,
        selected_playbook_ids,
    )

    chatbot_cases = [{"input_type": "conversation", "output_type": "conversation"}]
    selected = selected_playbook_ids(chatbot_cases)
    assert "voice" not in selected, (
        "Plain text conversation must not load voice playbook — voice teaches "
        "audio_bytes/audio_path return shapes that don't apply to text agents."
    )
    assert "live_test_voice" not in selected, (
        "Plain text conversation must not load live_test_voice playbook — its "
        "template asserts on agent audio bytes which don't exist in text builds."
    )
    assert "streaming_response" in selected, (
        "Plain text conversation DOES need streaming_response (token streaming)."
    )

    rendered = compose_capability_playbooks(chatbot_cases)
    assert "Voice Harness Outcome Contract" not in rendered
    assert "Voice Live-Test Outcome Contract" not in rendered
    assert "Streaming Response Outcome Contract" in rendered


def test_agent5_playbook_reads_are_cached():
    from puzzleeval.agents.agent5 import playbooks

    playbooks._load_packaged_playbook_text.cache_clear()
    with patch.object(playbooks.resources, "files", wraps=playbooks.resources.files) as files:
        first = playbooks.load_playbook_text("voice")
        second = playbooks.load_playbook_text("voice")

    assert first == second
    assert files.call_count == 1


def test_agent5_costing_module_preserves_cache_and_web_search_accounting():
    from puzzleeval.agents.agent5.costing import calculate_call_cost

    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=10000,
        cache_read_input_tokens=50000,
        iterations=None,
        server_tool_use=SimpleNamespace(web_search_requests=2),
    )
    response = SimpleNamespace(usage=usage)
    cost = calculate_call_cost(
        response,
        "claude-sonnet-4-6",
        model_pricing={"claude-sonnet-4-6": (3.0 / 1_000_000, 15.0 / 1_000_000)},
        web_search_price_per_search=0.01,
    )
    assert cost == pytest.approx(0.083, rel=1e-3)


def test_agent5_tool_dispatch_lives_in_extracted_module(tmp_path):
    from puzzleeval.agents.agent5.tools import dispatch_tool

    result, exit_code = dispatch_tool(
        "write_file",
        {"filename": "harness.py", "content": "print('ok')"},
        tmp_path,
        code_timeout_s=1,
    )
    assert exit_code == 0
    assert "Written" in result
    assert (tmp_path / "harness.py").read_text(encoding="utf-8") == "print('ok')"


def test_agent5_tool_patch_gate_lives_in_extracted_module(tmp_path):
    from puzzleeval.agents.agent5.tools import patch_file, read_file, write_file

    read_state = {}
    write_file({"filename": "harness.py", "content": "VALUE = 1"}, tmp_path)
    blocked = patch_file(
        {"filename": "harness.py", "old_string": "1", "new_string": "2"},
        tmp_path,
        read_state=read_state,
    )
    assert "has not been read yet" in blocked

    read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
    patched = patch_file(
        {"filename": "harness.py", "old_string": "1", "new_string": "2"},
        tmp_path,
        read_state=read_state,
    )
    assert "Patched" in patched
    assert (tmp_path / "harness.py").read_text(encoding="utf-8") == "VALUE = 2"


def test_agent5_read_state_invalidates_after_write_and_patch(tmp_path):
    from puzzleeval.agents.agent5.tools import patch_file, read_file, write_file

    read_state = {}
    write_file(
        {"filename": "harness.py", "content": "VALUE = 1"},
        tmp_path,
        read_state=read_state,
    )

    first = read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
    assert "VALUE = 1" in first

    write_file(
        {"filename": "harness.py", "content": "VALUE = 2"},
        tmp_path,
        read_state=read_state,
    )
    after_write = read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
    assert "VALUE = 2" in after_write
    assert "VALUE = 1" not in after_write
    assert "File unchanged since last read" not in after_write

    patched = patch_file(
        {"filename": "harness.py", "old_string": "2", "new_string": "3"},
        tmp_path,
        read_state=read_state,
    )
    assert "Patched" in patched
    after_patch = read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
    assert "VALUE = 3" in after_patch
    assert "VALUE = 2" not in after_patch
    assert "File unchanged since last read" not in after_patch


def test_agent5_exact_unchanged_reads_return_stub_and_compaction_invalidates(tmp_path):
    from puzzleeval.agents.agent5.tools import (
        FILE_UNCHANGED_STUB,
        invalidate_read_dedup_state,
        read_file,
        read_file_range,
        write_file,
    )

    read_state = {}
    write_file(
        {"filename": "harness.py", "content": "VALUE = 1\nVALUE = 2\n"},
        tmp_path,
        read_state=read_state,
    )

    first = read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
    second = read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
    assert "VALUE = 1" in first
    assert second == FILE_UNCHANGED_STUB

    invalidate_read_dedup_state(read_state)
    after_compaction = read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
    assert "VALUE = 1" in after_compaction

    range_first = read_file_range(
        {"filename": "harness.py", "start": 1, "end": 1},
        tmp_path,
        read_state=read_state,
    )
    range_second = read_file_range(
        {"filename": "harness.py", "start": 1, "end": 1},
        tmp_path,
        read_state=read_state,
    )
    assert "VALUE = 1" in range_first
    assert range_second == FILE_UNCHANGED_STUB


def test_puzzleeval_source_is_not_git_ignored():
    if shutil.which("git") is None:
        pytest.skip("git is not available")

    source_path = "PuzzleEval-local/puzzleeval/plugin_tool_runner.py"
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", source_path],
        check=False,
    )
    assert result.returncode != 0, f"{source_path} must not be ignored"


def test_generated_runs_remain_git_ignored():
    if shutil.which("git") is None:
        pytest.skip("git is not available")

    generated_path = "PuzzleEval-local/runs/example/harness.py"
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", generated_path],
        check=False,
    )
    assert result.returncode == 0, f"{generated_path} should stay ignored"


def test_api_knowledge_output_prefers_research_artifacts(tmp_path):
    from puzzleeval.agents.implement_test_env import _collect_api_knowledge_for_output

    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir()
    (state_dir / "research_synthesis.json").write_text(
        '{"chosen_api_surface": {"endpoint": "https://docs.example.test/run"}}',
        encoding="utf-8",
    )
    (state_dir / "implementation_plan.json").write_text(
        '{"ready_to_build": true, "interaction_pattern": "single_call"}',
        encoding="utf-8",
    )
    knowledge = _collect_api_knowledge_for_output(tmp_path)

    assert knowledge is not None
    assert "## research_synthesis" in knowledge
    assert "https://docs.example.test/run" in knowledge
    assert "## implementation_plan" in knowledge
    assert "single_call" in knowledge
    assert "api_spec.txt" not in knowledge


def test_openapi_fastpath_uses_current_venv_signature():
    impl_path = REPO_ROOT / "PuzzleEval-local" / "puzzleeval" / "agents" / "implement_test_env.py"
    src = impl_path.read_text(encoding="utf-8")

    assert "_create_venv(sandbox_dir, logger)" not in src
    assert "_create_venv(sandbox_dir, logger, trace_id, candidate.name)" in src


def test_execute_single_test_uses_sandbox_python(monkeypatch, tmp_path: Path):
    from puzzleeval.agents.agent5 import execution

    captured: dict[str, object] = {}
    real_run = execution.subprocess.run

    def fake_python(sandbox_dir: Path) -> str:
        assert sandbox_dir == tmp_path
        return "SANDBOX_PYTHON"

    def fake_run(cmd, **kwargs):
        if not (isinstance(cmd, list) and cmd and cmd[0] == "SANDBOX_PYTHON"):
            return real_run(cmd, **kwargs)
        captured["cmd"] = cmd
        script = cmd[2]
        match = re.search(r'with open\("([^"]+)", "w"', script)
        assert match, script
        output_path = Path(kwargs["cwd"]) / match.group(1)
        output_path.write_text(
            json.dumps({
                "success": True,
                "output": "ok",
                "latency_ms": 1.0,
                "tokens_used": None,
                "cost_usd": None,
                "raw_response": {},
                "error": None,
            }),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(execution, "_sandbox_python_executable", fake_python)
    monkeypatch.setattr(execution.subprocess, "run", fake_run)

    result = execution.execute_single_test(tmp_path, {"text": "hi"}, None, timeout=3)

    assert captured["cmd"][0] == "SANDBOX_PYTHON"
    assert result["success"] is True


def test_resolve_credentials_uses_explicit_runs_root(monkeypatch, tmp_path: Path):
    from puzzleeval.agents.agent5.sandbox import resolve_credentials

    runs_root = tmp_path / "api-runs"
    sandbox_dir = runs_root / "trace-root" / "harnesses" / "rooted_service"
    sandbox_dir.mkdir(parents=True)
    (sandbox_dir / "harness.py").write_text(
        "import os\nAPI_KEY = os.environ['ROOTED_SERVICE_TOKEN']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ROOTED_SERVICE_TOKEN", "secret")

    input_data = SimpleNamespace(
        provider_credentials=None,
        trace_id="trace-root",
        runs_root=str(runs_root),
    )
    candidate = SimpleNamespace(
        provider="Rooted Service",
        name="Rooted Service",
    )

    credentials = resolve_credentials(input_data, candidate, "ROOTED_SERVICE")

    assert credentials == {"ROOTED_SERVICE_TOKEN": "secret"}
