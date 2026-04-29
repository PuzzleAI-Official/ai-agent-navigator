"""Architecture guards for the Agent 5 cleanup boundary."""

from __future__ import annotations

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
    assert "Voice harness return-shape contract" in rendered
    assert "Streaming / multi-event response collection" in rendered
    assert "Voice / multi-turn live-test requirements" in rendered


def test_agent5_builder_prompt_template_is_loaded_from_package():
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


def test_agent5_playbook_router_does_not_pollute_rest_cases():
    from puzzleeval.agents.agent5.playbooks import (
        compose_capability_playbooks,
        selected_playbook_ids,
    )

    rest_cases = [{"input_type": "file", "output_type": "structured_json"}]
    assert selected_playbook_ids(rest_cases) == []
    assert compose_capability_playbooks(rest_cases) == ""


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
