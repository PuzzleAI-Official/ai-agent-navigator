"""Tests for the four-feature pass:

  1. Effort parameter config + output_config_for_request helper
  2. Adaptive thinking applied with effort across all multi-turn agent calls
  3. Hybrid evaluator: plugin tool definitions, dispatcher, end-to-end
  4. Programmatic tool calling: tool list adapter for Agent 5 builder
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Effort parameter
# ---------------------------------------------------------------------------


class TestEffortConfig:
    def test_default_effort_is_medium(self, monkeypatch):
        # Lowered from `high` to `medium` in
        # PLAN_VOICE_RUN_OPTIMIZATIONS.md item 4. Empirical evidence:
        # `high` was burning budget on routine tool-execution turns
        # without producing visible work. `medium` is a FLOOR — adaptive
        # thinking auto-tunes UPWARD on complex decisions, so hard
        # decisions still get the depth they need.
        monkeypatch.delenv("PUZZLEEVAL_EFFORT", raising=False)
        # Re-import config to pick up the cleared env
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.EFFORT == "medium"

    def test_effort_override_via_env(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_EFFORT", "high")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.EFFORT == "high"

    def test_invalid_effort_falls_back_to_medium(self, monkeypatch, capsys):
        monkeypatch.setenv("PUZZLEEVAL_EFFORT", "ludicrous_speed")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.EFFORT == "medium"
        captured = capsys.readouterr()
        assert "ludicrous_speed" in captured.err

    def test_output_config_returns_dict_when_set(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_EFFORT", "xhigh")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        result = cfg.output_config_for_request()
        assert result == {"effort": "xhigh"}

    def test_output_config_returns_none_when_unset(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_EFFORT", "")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.output_config_for_request() is None

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_all_documented_effort_levels_accepted(self, monkeypatch, level):
        monkeypatch.setenv("PUZZLEEVAL_EFFORT", level)
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.EFFORT == level
        assert cfg.output_config_for_request() == {"effort": level}


# ---------------------------------------------------------------------------
# 2. Adaptive thinking audit (presence + effort wiring)
# ---------------------------------------------------------------------------


class TestAdaptiveThinkingAudit:
    AGENTS_DIR = Path(__file__).parent.parent / "puzzleeval" / "agents"
    OTHER = Path(__file__).parent.parent / "puzzleeval"

    def _read(self, relpath: str) -> str:
        if relpath.startswith("agents/"):
            return (self.AGENTS_DIR / relpath[len("agents/"):]).read_text(encoding="utf-8")
        return (self.OTHER / relpath).read_text(encoding="utf-8")

    @pytest.mark.parametrize("relpath", [
        "agents/agent2/core.py",       # Agent 2 research
        "agents/agent4/core.py",      # Agent 4 verify
        # Agent 5 builder: thinking={"type": "adaptive"} moved to
        # agent5/api_call.py in Phase 4 Path B Step 1. Test the
        # canonical owner; behavior is also covered by
        # tests/test_api_call.py::TestSuccessPath.
        "agents/agent5/api_call.py",
    ])
    def test_multi_turn_agents_use_adaptive_thinking(self, relpath):
        text = self._read(relpath)
        assert 'thinking={"type": "adaptive"}' in text, (
            f"{relpath} should enable adaptive thinking on its multi-turn loop"
        )

    @pytest.mark.parametrize("relpath", [
        "agents/agent2/core.py",
        "agents/agent4/core.py",
        # Phase 6.2: output_config_for_request() now lives in
        # agent5/evaluation.py (the evaluator's adaptive-thinking
        # wiring). build_loop.py also passes ctx.output_config from
        # the loop caller into api_call.py. Either location satisfies
        # the contract; assert against evaluation.py as the canonical
        # owner of Agent 5's effort-propagation today.
        "agents/agent5/evaluation.py",
    ])
    def test_multi_turn_agents_apply_effort_via_helper(self, relpath):
        """Each adaptive-thinking call site should invoke
        output_config_for_request() so PUZZLEEVAL_EFFORT propagates."""
        text = self._read(relpath)
        assert "output_config_for_request" in text, (
            f"{relpath} must call output_config_for_request() so the effort "
            f"knob reaches the API"
        )


# ---------------------------------------------------------------------------
# 3. Hybrid evaluator
# ---------------------------------------------------------------------------


class TestPluginTools:
    def test_build_definitions_only_evaluators_by_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        from puzzleeval.plugin_tools import build_plugin_tool_definitions
        defs = build_plugin_tool_definitions()
        names = {d["name"] for d in defs}
        # All evaluator plugins should appear
        assert "score_with_code_execution" in names
        assert "score_with_vision" in names
        assert "score_with_transcription" in names
        assert "score_with_conversation_simulator" in names
        # tts is synthesizes-only — NOT an evaluator — should be excluded
        assert "score_with_tts" not in names

    def test_build_definitions_skips_unavailable_plugins(self, monkeypatch):
        for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY",
                  "ASSEMBLYAI_API_KEY"):
            monkeypatch.delenv(v, raising=False)
        from puzzleeval.plugin_tools import build_plugin_tool_definitions
        defs = build_plugin_tool_definitions()
        names = {d["name"] for d in defs}
        # transcription needs a key → unavailable → excluded
        assert "score_with_transcription" not in names
        # vision needs ANTHROPIC_API_KEY → unavailable → excluded
        assert "score_with_vision" not in names
        # code_execution + conversation_simulator have no creds → always included
        assert "score_with_code_execution" in names
        assert "score_with_conversation_simulator" in names

    def test_dispatcher_unknown_tool_returns_error(self):
        from puzzleeval.plugin_tools import dispatch_plugin_tool
        result = dispatch_plugin_tool("score_with_nonexistent", {})
        assert "error" in result

    def test_dispatcher_invalid_prefix_returns_error(self):
        from puzzleeval.plugin_tools import dispatch_plugin_tool
        result = dispatch_plugin_tool("not_a_score_tool", {})
        assert "error" in result

    def test_dispatcher_runs_code_execution_plugin(self):
        from puzzleeval.plugin_tools import dispatch_plugin_tool
        code = (
            "def fizzbuzz(n):\n"
            "    out = []\n"
            "    for i in range(1, n+1):\n"
            "        if i%15==0: out.append('FizzBuzz')\n"
            "        elif i%3==0: out.append('Fizz')\n"
            "        elif i%5==0: out.append('Buzz')\n"
            "        else: out.append(str(i))\n"
            "    return out\n"
        )
        expected_json = json.dumps({
            "expected_function": "fizzbuzz",
            "test_inputs": [3], "test_outputs": [["1", "2", "Fizz"]],
        })
        result = dispatch_plugin_tool(
            "score_with_code_execution",
            {"response": code, "expected": expected_json, "language": "python"},
        )
        assert result.get("plugin") == "code_execution"
        assert result.get("score") == 1.0
        assert result.get("passed") is True

    def test_serialize_returns_compact_json(self):
        from puzzleeval.plugin_tools import serialize_for_tool_result
        s = serialize_for_tool_result({"plugin": "x", "score": 0.5})
        assert "plugin" in s
        assert "0.5" in s


class TestHybridEvaluator:
    def test_verdict_extraction_from_json_block(self):
        from puzzleeval.hybrid_evaluator import _extract_verdict
        text = (
            "Some reasoning here.\n\n"
            '```json\n{"passed": true, "score": 0.9, "reasoning": "ok"}\n```'
        )
        v = _extract_verdict(text)
        assert v is not None
        assert v.passed is True
        assert v.score == 0.9
        assert v.reasoning == "ok"

    def test_verdict_extraction_clamps_score(self):
        from puzzleeval.hybrid_evaluator import _extract_verdict
        v = _extract_verdict('```json\n{"passed": false, "score": 1.7, "reasoning": ""}\n```')
        assert v.score == 1.0

    def test_verdict_extraction_handles_unparseable(self):
        from puzzleeval.hybrid_evaluator import _extract_verdict
        v = _extract_verdict("just prose, no json block")
        assert v is None

    def test_evaluator_returns_fallback_when_no_plugins(self, monkeypatch):
        # Strip every plugin credential
        for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY",
                  "ASSEMBLYAI_API_KEY", "ELEVENLABS_API_KEY"):
            monkeypatch.delenv(v, raising=False)
        # code_execution + conversation_simulator have no creds so they're
        # always available; this test verifies the fallback path when
        # build_plugin_tool_definitions returns an empty list.
        with patch(
            "puzzleeval.hybrid_evaluator.build_plugin_tool_definitions",
            return_value=[],
        ):
            from puzzleeval.hybrid_evaluator import (
                evaluate_with_claude_picked_plugins,
            )
            verdict = evaluate_with_claude_picked_plugins(
                response="x", expected="y",
                client=MagicMock(),
            )
            assert verdict.fallback_reason == "no_plugins_available"

    def test_evaluator_parses_terminal_verdict_block(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        # Mock client returns a single end_turn response with a verdict JSON
        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = (
            "Looks like a clean text-vs-text comparison; no plugin needed.\n\n"
            '```json\n{"passed": true, "score": 0.85, "reasoning": "match"}\n```'
        )
        mock_response.content = [text_block]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        from puzzleeval.hybrid_evaluator import (
            evaluate_with_claude_picked_plugins,
        )
        verdict = evaluate_with_claude_picked_plugins(
            response="hello", expected="hello",
            client=mock_client,
        )
        assert verdict.passed is True
        assert verdict.score == 0.85
        assert verdict.fallback_reason is None

    def test_evaluator_records_tools_called(self, monkeypatch):
        """Multi-iteration loop: first response calls a plugin tool, then
        end_turn with the verdict. tools_called should record the plugin name."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        # First response: tool_use
        first = MagicMock()
        first.stop_reason = "tool_use"
        text1 = MagicMock(); text1.type = "text"; text1.text = "Let me run this."
        tool_use = MagicMock()
        tool_use.type = "tool_use"
        tool_use.name = "score_with_code_execution"
        tool_use.id = "toolu_1"
        tool_use.input = {"response": "def f(): return 1", "expected": "{}"}
        first.content = [text1, tool_use]
        # Second response: end_turn with verdict
        second = MagicMock()
        second.stop_reason = "end_turn"
        text2 = MagicMock(); text2.type = "text"
        text2.text = '```json\n{"passed": false, "score": 0.0, "reasoning": "did not run"}\n```'
        second.content = [text2]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [first, second]

        from puzzleeval.hybrid_evaluator import (
            evaluate_with_claude_picked_plugins,
        )
        verdict = evaluate_with_claude_picked_plugins(
            response="x", expected="y", client=mock_client,
        )
        assert "score_with_code_execution" in verdict.tools_called


class TestHybridEvalConfig:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("PUZZLEEVAL_HYBRID_EVAL_ENABLED", raising=False)
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.HYBRID_EVAL_ENABLED is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_HYBRID_EVAL_ENABLED", "1")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.HYBRID_EVAL_ENABLED is True


# ---------------------------------------------------------------------------
# 4. Programmatic tool calling
# ---------------------------------------------------------------------------


class TestProgrammaticToolsAdapter:
    def test_disabled_returns_unchanged(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_PROGRAMMATIC_TOOLS", "0")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        from puzzleeval.agents import implement_test_env
        importlib.reload(implement_test_env)
        from puzzleeval.agents.implement_test_env import (
            ALL_TOOLS, _build_tools_with_programmatic,
        )
        adapted = _build_tools_with_programmatic(ALL_TOOLS)
        assert adapted is ALL_TOOLS

    def test_enabled_adds_code_execution_tool(self, monkeypatch):
        """When PROGRAMMATIC_TOOLS_ENABLED=1, the adapter must add an
        explicit `code_execution_20260120` tool to the list. The basic
        20250910 / 20250305 web tools do NOT auto-inject code_execution,
        so the explicit declaration is what enables programmatic plugin
        chaining via `allowed_callers=["direct","code_execution_20260120"]`.

        (We briefly ran with 20260209 web tools + no explicit code_execution
        — the auto-injection worked but caused cascade failures across
        sub-agents. Reverted.)"""
        monkeypatch.setenv("PUZZLEEVAL_PROGRAMMATIC_TOOLS", "1")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        from puzzleeval.agents import implement_test_env
        importlib.reload(implement_test_env)
        from puzzleeval.agents.implement_test_env import (
            ALL_TOOLS, _build_tools_with_programmatic,
        )
        adapted = _build_tools_with_programmatic(ALL_TOOLS)
        types = [t.get("type") for t in adapted]
        assert "code_execution_20260120" in types, (
            "Programmatic mode must add explicit code_execution_20260120 "
            "so plugin tools can be chained from inside the sandbox."
        )
        # Sanity: confirm basic web tools (post-revert) are present.
        assert "web_fetch_20250910" in types
        assert "web_search_20250305" in types

    def test_enabled_marks_custom_tools_with_allowed_callers(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_PROGRAMMATIC_TOOLS", "1")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        from puzzleeval.agents import implement_test_env
        importlib.reload(implement_test_env)
        from puzzleeval.agents.implement_test_env import (
            ALL_TOOLS, _build_tools_with_programmatic, CUSTOM_TOOL_NAMES,
        )
        adapted = _build_tools_with_programmatic(ALL_TOOLS)
        for tool in adapted:
            if tool.get("name") in CUSTOM_TOOL_NAMES:
                callers = tool.get("allowed_callers", [])
                assert "direct" in callers
                assert "code_execution_20260120" in callers, (
                    f"custom tool {tool.get('name')} should be programmatically callable"
                )

    def test_enabled_leaves_server_tools_direct_only(self, monkeypatch):
        """Server tools (web_fetch, web_search, advisor) can't be called from
        code_execution per the docs — they execute server-side already."""
        monkeypatch.setenv("PUZZLEEVAL_PROGRAMMATIC_TOOLS", "1")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        from puzzleeval.agents import implement_test_env
        importlib.reload(implement_test_env)
        from puzzleeval.agents.implement_test_env import (
            ALL_TOOLS, _build_tools_with_programmatic,
        )
        adapted = _build_tools_with_programmatic(ALL_TOOLS)
        for tool in adapted:
            ttype = tool.get("type", "")
            if ttype.startswith(("web_fetch_", "web_search_", "advisor")):
                # Server tools should NOT have allowed_callers added
                assert "allowed_callers" not in tool, (
                    f"server tool {ttype} should remain direct-only"
                )


class TestProgrammaticToolsConfig:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("PUZZLEEVAL_PROGRAMMATIC_TOOLS", raising=False)
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.PROGRAMMATIC_TOOLS_ENABLED is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_PROGRAMMATIC_TOOLS", "1")
        import importlib
        from puzzleeval import config as cfg
        importlib.reload(cfg)
        assert cfg.PROGRAMMATIC_TOOLS_ENABLED is True
