"""Tests for the Claude-driven plugin dispatch path.

Covers ``puzzleeval/plugin_tool_runner.py``:

- ScoreVerdict schema enforcement.
- @beta_tool wrapping of plugins (name, description, input_schema).
- allowed_callers + defer_loading injection via to_dict patches.
- tool_search_tool + code_execution server-tool inclusion under flags.
- Agent 5 dispatch strategy selection (tool_runner | deterministic | hybrid).

The tool_runner calls themselves are mocked — these tests assert the
WIRING (what tools get built, what the dispatch selects) without
hitting the live Anthropic API.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# ScoreVerdict — structured output contract
# ---------------------------------------------------------------------------


def test_score_verdict_schema_validates():
    """The Pydantic model must accept valid verdicts and reject invalid ones."""
    from puzzleeval.plugin_tool_runner import ScoreVerdict

    ok = ScoreVerdict(passed=True, score=0.95, reasoning="good match")
    assert ok.passed is True
    assert ok.score == 0.95

    # score must be 0..1
    with pytest.raises(Exception):
        ScoreVerdict(passed=True, score=1.5, reasoning="out of range")
    with pytest.raises(Exception):
        ScoreVerdict(passed=True, score=-0.1, reasoning="negative")


def test_score_verdict_has_description_fields():
    """tool_runner's output_format path needs field descriptions so Claude
    knows what to emit. Regression guard against accidentally stripping them."""
    from puzzleeval.plugin_tool_runner import ScoreVerdict
    fields = ScoreVerdict.model_fields
    for name in ("passed", "score", "reasoning"):
        assert fields[name].description, f"field {name} missing description"


# ---------------------------------------------------------------------------
# Eligibility selection — which plugins get exposed as tools
# ---------------------------------------------------------------------------


def test_eligible_plugins_filters_unavailable():
    """Plugins whose is_available() returns False must NOT appear in the
    tool list, so Claude doesn't waste iterations calling broken plugins."""
    from puzzleeval.plugin_tool_runner import eligible_plugins

    plugins = eligible_plugins(input_type=None, output_type=None)
    for p in plugins:
        ok, _reason = p.is_available()
        assert ok, f"plugin {p.name} is unavailable but still in eligible list"


def test_eligible_plugins_matches_input_modality():
    """An audio_content test case should surface audio-capable plugins."""
    from puzzleeval.plugin_tool_runner import eligible_plugins

    audio_plugins = eligible_plugins(
        input_type="audio_content", output_type="audio_content",
    )
    names = {p.name for p in audio_plugins}
    # transcription evaluates audio_content output; voice_realtime claims it too.
    # At least one of them must surface.
    assert names & {"transcription", "voice_realtime"}


def test_eligible_plugins_multi_modal_either_match():
    """When a test case has input_type=A / output_type=B and a plugin claims
    EITHER, it should be eligible. This is the generalization that closes
    the ambiguous-enum-tiebreak gap from deterministic dispatch."""
    from puzzleeval.plugin_tool_runner import eligible_plugins

    # A plugin that only claims input_types should still surface when
    # the output_type matches something else in the plugin catalog.
    # Specifically, webhook_receiver claims output_types=["webhook_callback"]
    # but with output_type=free_text we still want conversation_simulator.
    plugins = eligible_plugins(
        input_type="conversation", output_type="free_text",
    )
    names = {p.name for p in plugins}
    assert "conversation_simulator" in names


# ---------------------------------------------------------------------------
# Tool list assembly — @beta_tool + server tools
# ---------------------------------------------------------------------------


def test_build_tool_list_wraps_each_plugin_as_beta_tool():
    """Every eligible plugin should become exactly one BetaFunctionTool
    with a unique name prefixed score_with_<plugin>."""
    from puzzleeval.plugin_tool_runner import (
        EvalContext, build_tool_list, eligible_plugins,
    )

    plugins = eligible_plugins(input_type="conversation", output_type="free_text")
    ctx = EvalContext(response="", expected="", criteria=[], harness_runner=None)
    tools = build_tool_list(
        ctx, plugins=plugins,
        allow_code_execution=False, use_tool_search=False,
    )
    # Each plugin → one tool; no server tools added.
    assert len(tools) == len(plugins)
    names = {t.to_dict()["name"] for t in tools}
    assert all(n.startswith("score_with_") for n in names)


def test_build_tool_list_includes_tool_search_above_threshold():
    """When use_tool_search=True, the catalog adds tool_search_tool_bm25
    and every plugin tool has defer_loading=True."""
    from puzzleeval.plugin_tool_runner import (
        EvalContext, build_tool_list, eligible_plugins,
    )

    plugins = eligible_plugins(input_type=None, output_type=None)
    ctx = EvalContext(response="", expected="", criteria=[], harness_runner=None)
    tools = build_tool_list(
        ctx, plugins=plugins,
        allow_code_execution=False, use_tool_search=True,
    )
    # All plugin tools get defer_loading=True.
    plugin_tools = [t for t in tools if hasattr(t, "to_dict")]
    for t in plugin_tools:
        d = t.to_dict()
        assert d.get("defer_loading") is True, f"{d['name']} missing defer_loading"
    # tool_search_tool_bm25 server tool appended.
    server_names = [
        t.get("name") for t in tools if isinstance(t, dict)
    ]
    assert "tool_search_tool_bm25" in server_names


def test_build_tool_list_includes_code_execution_when_enabled():
    """When allow_code_execution=True, plugin tools get allowed_callers and
    the code_execution server tool is added."""
    from puzzleeval.plugin_tool_runner import (
        EvalContext, build_tool_list, eligible_plugins,
    )

    plugins = eligible_plugins(input_type="conversation", output_type="free_text")
    ctx = EvalContext(response="", expected="", criteria=[], harness_runner=None)
    tools = build_tool_list(
        ctx, plugins=plugins,
        allow_code_execution=True, use_tool_search=False,
    )
    plugin_tools = [t for t in tools if hasattr(t, "to_dict")]
    for t in plugin_tools:
        d = t.to_dict()
        assert d.get("allowed_callers") == ["direct", "code_execution_20260120"], (
            f"{d['name']} missing/mis-set allowed_callers"
        )
    server_names = [t.get("name") for t in tools if isinstance(t, dict)]
    assert "code_execution" in server_names


def test_build_tool_list_combines_search_and_code_execution():
    """Both flags on → plugin tools have defer_loading + allowed_callers,
    AND both server tools present."""
    from puzzleeval.plugin_tool_runner import (
        EvalContext, build_tool_list, eligible_plugins,
    )

    plugins = eligible_plugins(input_type=None, output_type=None)
    ctx = EvalContext(response="", expected="", criteria=[], harness_runner=None)
    tools = build_tool_list(
        ctx, plugins=plugins,
        allow_code_execution=True, use_tool_search=True,
    )
    plugin_tools = [t for t in tools if hasattr(t, "to_dict")]
    for t in plugin_tools:
        d = t.to_dict()
        assert d.get("defer_loading") is True
        assert d.get("allowed_callers") == ["direct", "code_execution_20260120"]
    server_names = [t.get("name") for t in tools if isinstance(t, dict)]
    assert "tool_search_tool_bm25" in server_names
    assert "code_execution" in server_names


# ---------------------------------------------------------------------------
# Plugin tool invocation — closure captures context
# ---------------------------------------------------------------------------


def test_plugin_beta_tool_invokes_plugin_evaluate_output():
    """When Claude calls the beta_tool, the plugin's evaluate_output runs
    with the closure-captured context. Verdict fields flow into the
    JSON string returned to the runner."""
    from puzzleeval.plugin_tool_runner import EvalContext, _build_plugin_tool
    from puzzleeval.tool_plugins import EvaluationResult, ToolPlugin, PluginCapabilities

    class FakePlugin(ToolPlugin):
        name = "fake_scorer"
        def capabilities(self) -> PluginCapabilities:
            return PluginCapabilities(
                input_types=["text"], output_types=["free_text"],
                synthesizes_input=False, evaluates_output=True,
                notes="test double",
            )
        def is_available(self):
            return True, ""
        def evaluate_output(self, *, response, expected, criteria=None, **kwargs):
            return EvaluationResult(
                passed=True, score=0.82,
                reasoning=f"response={response!s} expected={expected!s}",
            )

    ctx = EvalContext(
        response="hello world",
        expected="hello world",
        criteria=[{"criterion": "greets", "weight": 1.0}],
        harness_runner=None,
    )
    tool = _build_plugin_tool(FakePlugin(), ctx, allow_code_execution=False)
    # Tool name should carry the plugin identity.
    d = tool.to_dict()
    assert d["name"] == "score_with_fake_scorer"
    assert "test double" in d["description"]

    # Invoke the underlying func — simulates tool_runner dispatch.
    result_str = tool.call({})
    payload = json.loads(result_str)
    assert payload["plugin"] == "fake_scorer"
    assert payload["passed"] is True
    assert payload["score"] == 0.82
    assert "hello world" in payload["reasoning"]
    # ctx.tools_invoked should record the call.
    assert ctx.tools_invoked == ["fake_scorer"]


def test_plugin_beta_tool_handles_plugin_crash():
    """If the plugin raises, the wrapper returns a fallback_reason instead
    of propagating — tool_runner needs a parseable tool_result to continue."""
    from puzzleeval.plugin_tool_runner import EvalContext, _build_plugin_tool
    from puzzleeval.tool_plugins import ToolPlugin, PluginCapabilities

    class CrashPlugin(ToolPlugin):
        name = "crasher"
        def capabilities(self):
            return PluginCapabilities(
                input_types=["text"], output_types=["free_text"],
                evaluates_output=True,
            )
        def is_available(self):
            return True, ""
        def evaluate_output(self, *, response, expected, criteria=None, **kwargs):
            raise RuntimeError("boom")

    ctx = EvalContext(response="", expected="", criteria=[], harness_runner=None)
    tool = _build_plugin_tool(CrashPlugin(), ctx, allow_code_execution=False)
    result_str = tool.call({})
    payload = json.loads(result_str)
    assert payload["passed"] is False
    assert payload["fallback_reason"].startswith("plugin_crash:")


def test_plugin_beta_tool_captures_artifacts():
    """When a plugin implements artifacts_for_token and expected carries
    a token, the wrapper pulls the artifacts into ctx.plugin_artifacts
    so the caller can surface them in TestCaseResult.audio_paths."""
    from puzzleeval.plugin_tool_runner import EvalContext, _build_plugin_tool
    from puzzleeval.tool_plugins import EvaluationResult, ToolPlugin, PluginCapabilities

    class ArtifactPlugin(ToolPlugin):
        name = "artifact_producer"
        def capabilities(self):
            return PluginCapabilities(
                input_types=["voice_turn"], output_types=["voice_turn"],
                evaluates_output=True,
            )
        def is_available(self):
            return True, ""
        def evaluate_output(self, *, response, expected, criteria=None, **kwargs):
            return EvaluationResult(passed=True, score=1.0, reasoning="ok")
        def artifacts_for_token(self, token: str):
            return [{"role": "caller", "path": f"/tmp/{token}.wav"}]

    ctx = EvalContext(
        response="",
        expected={"token": "abc123"},
        criteria=[],
        harness_runner=None,
    )
    tool = _build_plugin_tool(ArtifactPlugin(), ctx, allow_code_execution=False)
    tool.call({})
    assert ctx.plugin_artifacts == [{"role": "caller", "path": "/tmp/abc123.wav"}]


# ---------------------------------------------------------------------------
# evaluate_with_tool_runner — end-to-end with mocked client
# ---------------------------------------------------------------------------


def _make_runner_with_verdict(verdict_obj):
    """Build a fake iterable that yields one message whose ``parsed`` is the
    supplied ScoreVerdict (mimics tool_runner's final message)."""
    msg = MagicMock()
    msg.parsed = verdict_obj
    msg.usage = MagicMock(input_tokens=1200, output_tokens=150)
    return iter([msg])


def test_evaluate_with_tool_runner_extracts_verdict():
    """Happy path: runner yields a message with a parsed ScoreVerdict;
    evaluator returns the corresponding ToolRunnerVerdict."""
    from puzzleeval.plugin_tool_runner import (
        ScoreVerdict, evaluate_with_tool_runner,
    )

    fake_client = MagicMock()
    fake_client.beta.messages.tool_runner.return_value = _make_runner_with_verdict(
        ScoreVerdict(passed=True, score=0.9, reasoning="clear match")
    )
    verdict = evaluate_with_tool_runner(
        client=fake_client,
        response="hello",
        expected="hi",
        criteria=[{"criterion": "greeting", "weight": 1.0}],
        test_scenario="basic greeting",
    )
    assert verdict.passed is True
    assert verdict.score == 0.9
    assert verdict.reasoning == "clear match"
    assert verdict.fallback_reason is None
    assert verdict.iterations == 1
    # Cost estimate should be non-zero given the usage stub.
    assert verdict.cost_usd > 0


def test_evaluate_with_tool_runner_returns_fallback_on_no_verdict():
    """When runner iterates without producing a structured verdict, we
    return fallback_reason='no_structured_verdict' — caller falls back."""
    from puzzleeval.plugin_tool_runner import evaluate_with_tool_runner

    fake_client = MagicMock()
    empty_msg = MagicMock()
    empty_msg.parsed = None
    empty_msg.usage = None
    fake_client.beta.messages.tool_runner.return_value = iter([empty_msg])
    verdict = evaluate_with_tool_runner(
        client=fake_client,
        response="x", expected="y", criteria=[],
    )
    assert verdict.fallback_reason == "no_structured_verdict"
    assert verdict.passed is False


def test_evaluate_with_tool_runner_handles_api_error():
    """APIError inside tool_runner must be caught and surfaced as a
    fallback_reason — never propagated (would crash the pipeline)."""
    import anthropic
    from puzzleeval.plugin_tool_runner import evaluate_with_tool_runner

    fake_client = MagicMock()
    fake_client.beta.messages.tool_runner.side_effect = anthropic.APIError(
        "boom", request=MagicMock(), body=None,
    )
    verdict = evaluate_with_tool_runner(
        client=fake_client, response="x", expected="y", criteria=[],
    )
    assert verdict.fallback_reason and verdict.fallback_reason.startswith("api_error:")
    assert verdict.passed is False


def test_evaluate_with_tool_runner_passes_code_execution_beta_when_enabled():
    """When EVAL_PROGRAMMATIC_CHAINING_ENABLED=True, tool_runner is invoked
    with the beta header for code_execution so Claude can chain plugins."""
    from puzzleeval.plugin_tool_runner import (
        ScoreVerdict, evaluate_with_tool_runner,
    )

    fake_client = MagicMock()
    fake_client.beta.messages.tool_runner.return_value = _make_runner_with_verdict(
        ScoreVerdict(passed=True, score=1.0, reasoning="ok")
    )
    with patch(
        "puzzleeval.plugin_tool_runner.EVAL_PROGRAMMATIC_CHAINING_ENABLED", True,
    ):
        evaluate_with_tool_runner(
            client=fake_client,
            response="x", expected="y", criteria=[],
        )
    kwargs = fake_client.beta.messages.tool_runner.call_args.kwargs
    betas = kwargs.get("betas", [])
    # Exact literal — prevents regression of the "wrong beta version" bug
    # (audit C1). `code_execution_20260120` tool TYPE requires
    # `code-execution-2025-08-25` beta HEADER per Anthropic docs.
    assert "code-execution-2025-08-25" in betas, (
        f"Expected exact beta 'code-execution-2025-08-25', got {betas}. "
        "Wrong header => every tool_runner call returns invalid_beta 400."
    )


def test_evaluate_with_tool_runner_uses_tool_search_above_threshold():
    """When plugin count >= threshold, the tools list includes
    tool_search_tool_bm25 AND plugins are deferred."""
    from puzzleeval.plugin_tool_runner import (
        ScoreVerdict, evaluate_with_tool_runner,
    )

    fake_client = MagicMock()
    fake_client.beta.messages.tool_runner.return_value = _make_runner_with_verdict(
        ScoreVerdict(passed=True, score=0.8, reasoning="ok")
    )
    # Force threshold to 1 so the real plugin catalog triggers it.
    with patch("puzzleeval.plugin_tool_runner.EVAL_TOOL_SEARCH_THRESHOLD", 1):
        evaluate_with_tool_runner(
            client=fake_client,
            response="x", expected="y", criteria=[],
        )
    kwargs = fake_client.beta.messages.tool_runner.call_args.kwargs
    tools = list(kwargs["tools"])
    # A tool_search server tool should appear.
    server_names = [t.get("name") for t in tools if isinstance(t, dict)]
    assert "tool_search_tool_bm25" in server_names


# ---------------------------------------------------------------------------
# Agent 5 dispatch strategy — source-grep regression guards
# ---------------------------------------------------------------------------


def test_agent5_reads_eval_strategy_from_config():
    """Agent 5's eval dispatch must consult EVAL_STRATEGY; no hardcoded
    'deterministic' behavior."""
    src = (ROOT / "puzzleeval" / "agents" / "implement_test_env.py").read_text(
        encoding="utf-8"
    )
    assert "EVAL_STRATEGY" in src
    assert "_run_tool_runner" in src
    assert "_run_deterministic" in src


def test_agent5_tool_runner_is_default_path():
    """The default strategy must be tool_runner, not deterministic — the
    whole point of this migration was to make Claude-driven the primary."""
    from puzzleeval.config import EVAL_STRATEGY
    # Env override is legal; under test without override, default is tool_runner.
    # Accept the overridden value if the user set it, else assert default.
    if "PUZZLEEVAL_EVAL_STRATEGY" not in os.environ:
        assert EVAL_STRATEGY == "tool_runner"


def test_config_exports_eval_knobs():
    """Every new env knob must be importable from puzzleeval.config so
    ops can tune without editing source."""
    from puzzleeval.config import (
        EVAL_STRATEGY,
        EVAL_TOOL_SEARCH_THRESHOLD,
        EVAL_MAX_ITERATIONS,
        EVAL_PROGRAMMATIC_CHAINING_ENABLED,
    )
    assert EVAL_STRATEGY in {"tool_runner", "deterministic", "hybrid"}
    assert EVAL_TOOL_SEARCH_THRESHOLD >= 0
    assert EVAL_MAX_ITERATIONS >= 1
    assert isinstance(EVAL_PROGRAMMATIC_CHAINING_ENABLED, bool)


def test_system_prompt_has_multi_tool_guidance():
    """Claude must be told to chain tools when multiple modalities are
    present — this is the prompt-level fix for the 'need 3 tools, got 1' gap."""
    from puzzleeval.plugin_tool_runner import EVAL_SYSTEM_PROMPT
    assert "multiple modalities" in EVAL_SYSTEM_PROMPT.lower() or "multiple" in EVAL_SYSTEM_PROMPT.lower()
    # And the "don't stop after one tool" language.
    assert "stop" in EVAL_SYSTEM_PROMPT.lower()
    # And the structured-output contract.
    assert "ScoreVerdict" in EVAL_SYSTEM_PROMPT


def test_tool_descriptions_include_plugin_modalities():
    """Each plugin tool's description must tell Claude what modalities it
    handles — otherwise picking is random. Guards against blank descriptions."""
    from puzzleeval.plugin_tool_runner import (
        EvalContext, build_tool_list, eligible_plugins,
    )

    plugins = eligible_plugins(input_type=None, output_type=None)
    ctx = EvalContext(response="", expected="", criteria=[], harness_runner=None)
    tools = build_tool_list(
        ctx, plugins=plugins,
        allow_code_execution=False, use_tool_search=False,
    )
    for t in tools:
        if not hasattr(t, "to_dict"):
            continue
        d = t.to_dict()
        desc = d["description"]
        # Must include both input_types and output_types hint so Claude
        # can match against the response shape.
        assert "input_types=[" in desc, f"{d['name']} description missing input_types"
        assert "output_types=[" in desc, f"{d['name']} description missing output_types"
        # Must describe the return shape so Claude knows how to parse.
        assert "Returns" in desc
