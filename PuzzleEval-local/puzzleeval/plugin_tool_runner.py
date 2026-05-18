"""Claude-driven plugin dispatch via `client.beta.messages.tool_runner`.

Replaces the legacy enum-based deterministic dispatcher (``modality.py``)
and the hand-rolled tool-use loop (``hybrid_evaluator.py``). Each registered
tool plugin is exposed to Claude as an ``@beta_tool`` function. Claude
reads the test case + response, picks the right plugin(s), chains them
across iterations when multiple are needed, and emits a structured
``ScoreVerdict`` as the final message.

This is the architecturally-correct path per the Anthropic docs
(https://platform.claude.com/docs/en/agents-and-tools/tool-use):

  * Tool discovery at scale → ``tool_search_tool_bm25_20251119`` +
    ``defer_loading=True`` when the plugin count exceeds
    ``EVAL_TOOL_SEARCH_THRESHOLD``. Tools marked deferred don't appear
    in the system-prompt prefix; Claude searches for them on demand.
    Prompt caching stays intact across turns.

  * Multi-tool chaining → with
    ``PUZZLEEVAL_EVAL_PROGRAMMATIC_CHAINING=1``, plugin tools are marked
    ``allowed_callers=["direct", "code_execution_20260120"]`` and a
    ``code_execution_20260120`` server tool is added. Claude can write
    one Python script that calls multiple plugins inside a single
    container — intermediate tool results do NOT enter the model's
    context window.

  * Structured verdict → ``output_format=ScoreVerdict`` forces the final
    message into a Pydantic model. No regex-parsing a JSON block out of
    free text.

Coverage gaps this module closes vs the legacy deterministic path:
  1. "Deterministic picked 1 but 3 tools were needed" — Claude invokes
     tools across iterations until it has enough info.
  2. "Agent 3 mis-labeled the modality enum" — Claude reads the actual
     response content, not just the label.
  3. "Two plugins both claim (input_type, output_type); tie-break was
     order-dependent" — Claude picks based on description + content.
  4. "Multi-modal response (audio + image + code)" — Claude calls
     transcription + vision + code_execution, aggregates.
  5. "Novel plugin added" — just register it, Claude finds it via
     description match (or ``tool_search_tool`` at scale). No modality
     enum updates anywhere.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    import anthropic
    from anthropic import beta_tool
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal test envs
    from puzzleeval.anthropic_client import anthropic  # type: ignore
    beta_tool = anthropic.beta_tool  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - fallback stub lacks package metadata
    from puzzleeval.anthropic_client import anthropic  # type: ignore
    beta_tool = anthropic.beta_tool  # type: ignore[attr-defined]
from pydantic import BaseModel, Field

from puzzleeval.agent_preamble import with_preamble
from puzzleeval.config import (
    CONVERSATION_DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    EVAL_MAX_ITERATIONS,
    EVAL_PROGRAMMATIC_CHAINING_ENABLED,
    EVAL_TOOL_SEARCH_THRESHOLD,
    output_config_for_request,
)
from puzzleeval.tool_plugins import ToolPlugin, list_plugins

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured verdict — enforced via output_format on tool_runner
# ---------------------------------------------------------------------------


class ScoreVerdict(BaseModel):
    """Final structured verdict Claude produces after invoking plugins.

    ``tool_runner`` enforces this schema via ``output_format=ScoreVerdict``;
    no regex-parsing of JSON blocks from free text. The final iteration's
    ``parsed`` attribute is this exact model.
    """

    passed: bool = Field(
        description=(
            "Whether the response met the criteria overall. "
            "True when the weighted score is >= the pass threshold "
            "AND no criterion with significant weight failed catastrophically."
        ),
    )
    score: float = Field(
        description=(
            "Overall weighted score from 0.0 to 1.0. Aggregates "
            "per-criterion signals from the plugins invoked plus any "
            "direct inspection you did."
        ),
        ge=0.0,
        le=1.0,
    )
    reasoning: str = Field(
        description=(
            "One-paragraph explanation citing evidence from each tool "
            "result you relied on. Be terse; the reasoning is for the "
            "evaluation report, not for the model."
        ),
    )


# ---------------------------------------------------------------------------
# Per-test context — closure-captured by the plugin @beta_tool wrappers
# ---------------------------------------------------------------------------


@dataclass
class _CapturedPluginVerdict:
    """Snapshot of one plugin's EvaluationResult captured inside the
    tool_runner loop. Used to promote plugin scoring directly into a
    ToolRunnerVerdict when Claude's final structured output is missing.
    """
    plugin_name: str
    passed: bool
    score: float
    reasoning: str
    fallback_reason: str | None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalContext:
    """Per-test context threaded through every plugin tool the runner invokes.

    The response / expected / criteria / harness_runner are the same
    across every tool invocation for a given test case, so the plugin
    tools receive them via closure rather than as tool arguments. Claude
    only needs to decide WHICH plugin to call, not to re-state the test
    case context each time.

    ``plugin_artifacts`` accumulates audio paths and similar playback
    evidence from plugins that implement ``artifacts_for_token``.
    ``tools_invoked`` records call order for the per-test telemetry
    that lands in ``TestCaseResult.tools_used``.
    """

    response: Any
    expected: Any
    criteria: list[dict]
    harness_runner: Callable[[dict], dict] | None
    plugin_artifacts: list[dict] = field(default_factory=list)
    tools_invoked: list[str] = field(default_factory=list)
    # Every plugin verdict from every tool invocation, newest last.
    # Used as a fallback when Claude's tool_runner finishes without
    # emitting a structured ``ScoreVerdict`` — in that case we still
    # have conclusive plugin scoring (e.g., voice_realtime's
    # drive_conversation ran 5 turns cleanly, produced per-turn
    # evidence signals, and computed overall_score) that we want to
    # promote directly instead of collapsing to LLM-judge fallback.
    plugin_verdicts: list["_CapturedPluginVerdict"] = field(default_factory=list)
    # ── Agentic conversational eval context ──
    # Populated when the TestCase carries persona+goal+rubric (new
    # agentic path). Plugins that own multi-turn modalities
    # (voice_realtime, conversation_simulator) detect these and route
    # to user_simulator + rubric_judge instead of the scripted loop.
    # None / empty for non-conversational tests — plugins ignore.
    persona: Any = None          # puzzleeval.schemas.Persona | None
    goal: str | None = None
    constraints: list[str] = field(default_factory=list)
    rubric: list = field(default_factory=list)  # list[RubricCriterion]
    max_turns: int = CONVERSATION_DEFAULT_MAX_TURNS
    evaluation_mode: str = "auto"
    trace_id: str = "no-trace"
    # The candidate agent's configured system prompt + any per-test
    # metadata (language, format, region, …). Mirrors TestCase.input_
    # context. Plugins that care (voice_realtime, conversation_
    # simulator) extract `instructions` (or an alias) and pass it to
    # rubric_judge so scope/policy scoring has ground truth. Empty
    # dict is safe for non-conversational tests.
    input_context: dict = field(default_factory=dict)
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None
    release_harness_session: Callable[[], None] | None = None


# ---------------------------------------------------------------------------
# Plugin → @beta_tool wrapper factory
# ---------------------------------------------------------------------------


def _verdict_to_dict_str(verdict: Any, plugin_name: str, dimensions: list[str]) -> str:
    """Serialize a plugin's EvaluationResult into the JSON string tool_runner
    feeds back to Claude as the tool_result content.

    Keeps the shape stable so Claude's aggregation logic can trust it:
    ``{passed, score, reasoning, fallback_reason, dimensions_scored}``.
    """
    return json.dumps(
        {
            "plugin": plugin_name,
            "passed": bool(getattr(verdict, "passed", False)),
            "score": float(getattr(verdict, "score", 0.0) or 0.0),
            "reasoning": str(getattr(verdict, "reasoning", ""))[:1000],
            "fallback_reason": getattr(verdict, "fallback_reason", None),
            "dimensions_scored": dimensions,
        },
        default=str,
    )


def _build_plugin_tool(
    plugin: ToolPlugin, ctx: EvalContext, *, allow_code_execution: bool,
):
    """Wrap one plugin's ``evaluate_output`` as a ``@beta_tool`` function.

    The function is given a unique name (``score_with_<plugin>``), its
    docstring is synthesized from the plugin's capabilities so Claude
    can decide when to call it, and its implementation captures the
    ``EvalContext`` via closure.

    When ``allow_code_execution=True``, the tool's serialized dict is
    patched to include ``allowed_callers=["direct", "code_execution_20260120"]``
    so Claude can also invoke it from inside a code_execution container.
    """
    plugin_name = plugin.name
    caps = plugin.capabilities()
    output_types = ", ".join(caps.output_types) if caps.output_types else "any"
    input_types = ", ".join(caps.input_types) if caps.input_types else "any"
    notes = (caps.notes or "").strip()

    description = (
        f"Score the test response using the '{plugin_name}' plugin.\n\n"
        f"When to use: the response matches the plugin's modality — "
        f"input_types=[{input_types}], output_types=[{output_types}]. "
        f"{notes}\n\n"
        f"Arguments: none (context is bound to this test case).\n"
        f"Returns: JSON {{plugin, passed, score (0-1), reasoning, "
        f"fallback_reason, dimensions_scored}}. "
        f"If ``fallback_reason`` is non-null the plugin couldn't handle "
        f"this response — call a different plugin or reason directly."
    )

    # The function body captures `plugin`, `ctx`, and `caps` via closure.
    # `__name__` + `__doc__` are overwritten so beta_tool extracts
    # per-plugin metadata. Arity is zero — Claude only decides WHETHER
    # to call this tool, not with what arguments.
    def _impl() -> str:
        """Placeholder — overwritten below."""
        ctx.tools_invoked.append(plugin_name)
        try:
            # Forward agentic conversational context. Plugins that
            # don't consume these fields ignore them via **kwargs.
            # Conversational plugins (voice_realtime, conversation_
            # simulator) inspect persona+goal+rubric to route to their
            # agentic drive loop. See plugin's evaluate_output docstring
            # for full contract.
            verdict = plugin.evaluate_output(
                response=ctx.response,
                expected=ctx.expected,
                criteria=ctx.criteria,
                harness_runner=ctx.harness_runner,
                persona=ctx.persona,
                goal=ctx.goal,
                constraints=ctx.constraints,
                rubric=ctx.rubric,
                max_turns=ctx.max_turns,
                evaluation_mode=ctx.evaluation_mode,
                trace_id=ctx.trace_id,
                input_context=ctx.input_context,
                semantic_review_required=True,
                progress_callback=ctx.progress_callback,
                release_harness_session=ctx.release_harness_session,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plugin %s crashed inside tool_runner: %s", plugin_name, exc,
            )
            return json.dumps(
                {
                    "plugin": plugin_name,
                    "passed": False,
                    "score": 0.0,
                    "reasoning": "",
                    "fallback_reason": f"plugin_crash:{type(exc).__name__}",
                    "dimensions_scored": [],
                }
            )
        # Pull artifacts (audio paths etc.) if the plugin provides them.
        # Priority order, all general-purpose:
        #   1. verdict.detail.audio_paths — the plugin already did the
        #      work of building the list (multi-turn drive_conversation
        #      returns per-turn artifacts here). Use directly.
        #   2. plugin.artifacts_for_token_prefix(session_token) — for
        #      multi-turn sessions where each turn uses a sub-token
        #      `<session>-t<idx>`.
        #   3. plugin.artifacts_for_token(token) — legacy single-turn.
        detail_paths = None
        if hasattr(verdict, "detail") and isinstance(verdict.detail, dict):
            detail_paths = verdict.detail.get("audio_paths")
        if isinstance(detail_paths, list) and detail_paths:
            for item in detail_paths:
                if isinstance(item, dict) and item.get("path"):
                    ctx.plugin_artifacts.append(item)
        elif hasattr(plugin, "artifacts_for_token") or hasattr(plugin, "artifacts_for_token_prefix"):
            token = None
            exp = ctx.expected
            if isinstance(exp, dict):
                token = exp.get("token") or exp.get("session_token")
            # voice_realtime's drive_conversation synthesizes its own
            # session_token on the fly; the EvaluationResult's detail
            # dict carries it back so artifacts can still be recovered
            # even when the test case didn't pre-seed one.
            if not token and hasattr(verdict, "detail") and isinstance(verdict.detail, dict):
                token = verdict.detail.get("session_token")
            if token:
                try:
                    if hasattr(plugin, "artifacts_for_token_prefix"):
                        arts = plugin.artifacts_for_token_prefix(token)
                    else:
                        arts = plugin.artifacts_for_token(token)
                    if arts:
                        ctx.plugin_artifacts.extend(arts)
                except Exception:  # noqa: BLE001
                    pass
        # Capture the plugin's own verdict so we can promote it to a
        # ToolRunnerVerdict if Claude's structured output never lands.
        # Real-run signal (voice_debug_6): the plugin drove 5 turns
        # cleanly, produced passed/score/reasoning, but tool_runner
        # short-circuited to "no_structured_verdict" → every voice
        # test fell back to single-turn LLM judge that couldn't even
        # see the transcript. Capturing here lets us self-recover.
        ctx.plugin_verdicts.append(_CapturedPluginVerdict(
            plugin_name=plugin_name,
            passed=bool(getattr(verdict, "passed", False)),
            score=float(getattr(verdict, "score", 0.0) or 0.0),
            reasoning=str(getattr(verdict, "reasoning", ""))[:4000],
            fallback_reason=getattr(verdict, "fallback_reason", None),
            detail=dict(getattr(verdict, "detail", {}) or {}),
        ))
        return _verdict_to_dict_str(verdict, plugin_name, list(caps.output_types))

    _impl.__name__ = f"score_with_{plugin_name}"
    _impl.__doc__ = description

    tool = beta_tool(_impl)

    if allow_code_execution:
        # Inject allowed_callers into the serialized dict. BetaFunctionTool
        # doesn't expose this natively but tool_runner calls .to_dict() to
        # assemble the API request, so patching the dict flows through.
        original_to_dict = tool.to_dict

        def _patched_to_dict() -> dict:
            d = dict(original_to_dict())
            d["allowed_callers"] = ["direct", "code_execution_20260120"]
            return d

        tool.to_dict = _patched_to_dict  # type: ignore[method-assign]
    return tool


# ---------------------------------------------------------------------------
# Eligibility — which plugins get exposed as tools for a given test case
# ---------------------------------------------------------------------------


def eligible_plugins(
    input_type: str | None = None,
    output_type: str | None = None,
    *,
    only_available: bool = True,
) -> list[ToolPlugin]:
    """Return plugins that claim the test case's modalities AND are ready.

    Unlike the legacy deterministic dispatcher, we return ALL plugins that
    match (not just the first). Claude picks; we only filter out plugins
    that aren't available (missing credentials / system deps) because
    exposing an unavailable plugin just wastes an iteration.

    When both ``input_type`` and ``output_type`` are None, we return every
    plugin — useful when Agent 3's schema stamp is wrong or for scenarios
    like adversarial tests where the "right" plugin isn't encoded in the
    enums.
    """
    picked: list[ToolPlugin] = []
    for plugin in list_plugins():
        caps = plugin.capabilities()
        if not caps.evaluates_output:
            continue
        # Modality match semantics (simplified after audit):
        #   - Both input_type AND output_type provided: plugin matches if
        #     EITHER hits. Lets a plugin claiming only input_types still
        #     surface when the test's output side is what it scores, and
        #     vice versa. This is the "any signal is enough" stance Claude
        #     should have when choosing among descriptions.
        #   - Only one side provided: that side must match.
        #   - Neither provided: plugin is eligible.
        if input_type is not None and output_type is not None:
            modality_hit = (
                input_type in caps.input_types
                or output_type in caps.output_types
            )
        elif input_type is not None:
            modality_hit = input_type in caps.input_types
        elif output_type is not None:
            modality_hit = output_type in caps.output_types
        else:
            modality_hit = True
        if not modality_hit:
            continue
        if only_available:
            ok, _reason = plugin.is_available()
            if not ok:
                continue
        picked.append(plugin)
    return picked


# ---------------------------------------------------------------------------
# System prompt for the evaluator
# ---------------------------------------------------------------------------


EVAL_SYSTEM_PROMPT = """\
You are scoring a candidate API's response to a test case. Plugins are \
available as tools — call as many as make sense, in whatever order best \
builds evidence for your verdict. Each plugin returns JSON with a \
``passed``/``score``/``reasoning`` block plus a ``fallback_reason`` you \
should respect.

Rules:

1. Pick the plugin(s) whose description best matches the RESPONSE content \
and the test's expected output. Enum labels on the test case are hints, \
not gospel — read the response and decide.

2. When the response covers multiple modalities (e.g. a reply with text + \
an audio URL + a generated image), call each matching plugin and \
aggregate. Don't stop after one tool returns a score if other tools \
could score unscored dimensions.

3. If a plugin returns ``fallback_reason``, try a different plugin or \
reason directly. Don't treat a fallback as a final verdict.

4. If no plugin is a good fit, score the response directly against the \
criteria without calling any tool.

5. When you have enough evidence, stop calling tools and emit the final \
structured verdict. Do not narrate; the scoring is the product.

The final message must conform to the ``ScoreVerdict`` schema \
(``passed: bool``, ``score: float 0.0–1.0``, ``reasoning: str``).
"""


# ---------------------------------------------------------------------------
# Main entry: evaluate a single test case through tool_runner
# ---------------------------------------------------------------------------


@dataclass
class ToolRunnerVerdict:
    """Verdict + telemetry returned by ``evaluate_with_tool_runner``.

    Fields:
      ``passed``/``score``/``reasoning`` — the structured ScoreVerdict.
      ``tools_invoked`` — plugin names called during this evaluation, in
        order. Populates ``TestCaseResult.tools_used``.
      ``artifacts`` — ``{role, path}`` dicts from any plugin that exposed
        ``artifacts_for_token``. Populates ``TestCaseResult.audio_paths``.
      ``cost_usd`` — best-effort sum of Anthropic token costs across the
        tool_runner's internal iterations.
      ``iterations`` — how many Claude API calls the runner made.
      ``fallback_reason`` — non-null when the runner couldn't produce a
        structured verdict (all iterations used / parse failure). Callers
        fall back to a simpler path.
    """

    passed: bool
    score: float
    reasoning: str
    tools_invoked: list[str]
    artifacts: list[dict]
    cost_usd: float
    iterations: int
    fallback_reason: str | None = None
    # Full detail dict from the winning plugin verdict (the promoted-
    # directly path stashes this so agentic conversational metadata —
    # rubric_verdict + transcript + simulator_cost_usd + judge_cost_usd
    # — reaches ``_promote_verdict_to_tcr`` and lands on TestCaseResult.
    # Empty dict for non-conversational paths.
    verdict_detail: dict = field(default_factory=dict)


def _estimate_cost(usage_obj: Any, model: str) -> float:
    """Best-effort cost estimate from a usage object.

    Uses the current Sonnet 4.6 pricing as a default floor; Opus is
    roughly 1.7× more expensive but we don't know the exact model that
    served each iteration without inspection. Close enough for a cost
    dashboard; the real number lands via Anthropic's billing.
    """
    try:
        input_tokens = getattr(usage_obj, "input_tokens", 0) or 0
        output_tokens = getattr(usage_obj, "output_tokens", 0) or 0
    except Exception:  # noqa: BLE001
        return 0.0
    in_rate = 5.0 if "opus" in model.lower() else 3.0
    out_rate = 25.0 if "opus" in model.lower() else 15.0
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _compose_user_prompt(
    *,
    response: Any,
    expected: Any,
    criteria: list[dict],
    test_scenario: str,
) -> str:
    """Assemble the single user message fed to the tool_runner.

    Truncates large payloads so the initial prompt stays cheap even when
    a candidate returns a multi-kilobyte response. Claude can always call
    plugins for deeper inspection.
    """
    def _stringify(v: Any, cap: int = 4000) -> str:
        if isinstance(v, str):
            return v[:cap]
        try:
            return json.dumps(v, default=str)[:cap]
        except (TypeError, ValueError):
            return str(v)[:cap]

    response_str = _stringify(response)
    expected_str = _stringify(expected)
    criteria_lines = [
        f"- {c.get('criterion', '(unnamed)')} "
        f"(weight={c.get('weight', 0):.2f}, type={c.get('eval_type', 'subjective_quality')})"
        for c in criteria
    ] or ["- Overall fidelity to expected output (weight 1.00)"]
    criteria_str = "\n".join(criteria_lines)

    return (
        f"Test scenario: {test_scenario or '(not provided)'}\n\n"
        f"Candidate response:\n{response_str}\n\n"
        f"Expected output (ground truth):\n{expected_str}\n\n"
        f"Criteria:\n{criteria_str}\n"
    )


def build_tool_list(
    ctx: EvalContext,
    *,
    plugins: list[ToolPlugin],
    allow_code_execution: bool,
    use_tool_search: bool,
) -> list[Any]:
    """Assemble the tools list passed to ``tool_runner``.

    - Each plugin becomes a ``@beta_tool`` wrapper (from
      ``_build_plugin_tool``).
    - When ``use_tool_search=True``, plugin tools get their dicts
      patched with ``defer_loading=True`` and a
      ``tool_search_tool_bm25_20251119`` server tool is added. This
      keeps the context-prefix free of plugin definitions; Claude
      discovers them on demand. Anthropic's published scaling recipe.
    - When ``allow_code_execution=True``, plugin tools also get
      ``allowed_callers=["direct","code_execution_20260120"]`` (via
      ``_build_plugin_tool``) and the code_execution server tool is
      added so Claude can chain plugins in one Python script — the
      intermediate tool results don't enter the model's context.

    Returns a list mixing BetaFunctionTool objects (for auto-dispatch
    by tool_runner) and server-tool dicts.
    """
    tools: list[Any] = []
    for plugin in plugins:
        tool = _build_plugin_tool(
            plugin, ctx, allow_code_execution=allow_code_execution,
        )
        if use_tool_search:
            # Patch defer_loading into the dict representation.
            prior_to_dict = tool.to_dict

            def _deferred_to_dict(_pd=prior_to_dict) -> dict:
                d = dict(_pd())
                d["defer_loading"] = True
                return d

            tool.to_dict = _deferred_to_dict  # type: ignore[method-assign]
        tools.append(tool)

    if use_tool_search:
        # BM25 variant — natural language queries. The regex variant is
        # available for strict-match cases but BM25 is what we want for
        # "semantically find the right plugin for this response shape."
        tools.append(
            {
                "type": "tool_search_tool_bm25_20251119",
                "name": "tool_search_tool_bm25",
            }
        )

    if allow_code_execution:
        tools.append(
            {
                "type": "code_execution_20260120",
                "name": "code_execution",
            }
        )

    return tools


def evaluate_with_tool_runner(
    *,
    client: anthropic.Anthropic,
    response: Any,
    expected: Any,
    criteria: list[dict] | None,
    test_scenario: str = "",
    harness_runner: Callable[[dict], dict] | None = None,
    input_type: str | None = None,
    output_type: str | None = None,
    model: str | None = None,
    max_iterations: int | None = None,
    trace_id: str | None = None,
    # ── Agentic conversational eval context (optional) ──
    # Forwarded into EvalContext → plugin.evaluate_output. Populated
    # by Agent 5 from TestCase.persona / .goal / .rubric etc. when
    # present; None / empty for non-conversational tests.
    persona: Any = None,
    goal: str | None = None,
    constraints: list[str] | None = None,
    rubric: list | None = None,
    max_turns: int = CONVERSATION_DEFAULT_MAX_TURNS,
    evaluation_mode: str = "auto",
    input_context: dict | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    release_harness_session: Callable[[], None] | None = None,
) -> ToolRunnerVerdict:
    """Score one test case using Claude + plugin tools via ``tool_runner``.

    The entry point called from Agent 5's test-execution loop when
    ``EVAL_STRATEGY in {'tool_runner', 'hybrid'}``. Assembles eligible
    plugin tools, kicks off ``tool_runner``, iterates until Claude
    produces a structured ``ScoreVerdict``, and folds the result into
    the ``TestCaseResult`` shape the existing pipeline expects.

    Never raises for API errors — returns a verdict with a
    ``fallback_reason`` so callers can fall back cleanly.
    """
    criteria = list(criteria or [])
    model = model or DEFAULT_MODEL
    max_iterations = max_iterations or EVAL_MAX_ITERATIONS

    plugins = eligible_plugins(input_type, output_type)
    # When a harness_runner is supplied AND the test's modality genuinely
    # calls for multi-call orchestration (voice or conversation), add any
    # harness-driving plugin Claude might want to pick — even if its enums
    # didn't strictly match. This is a targeted widening, NOT a blanket one:
    # tests with (input_type='text', output_type='free_text') never gain
    # conversation_simulator eligibility just because a runner was threaded
    # through defensively. Prior broad widening invited Claude to mis-pick
    # multi-turn plugins for simple text evals.
    multi_call_modalities = {
        "conversation", "voice_conversation", "voice_turn",
    }
    is_multi_call_test = (
        input_type in multi_call_modalities
        or output_type in multi_call_modalities
    )
    if harness_runner is not None and is_multi_call_test:
        runner_plugins = [
            p for p in list_plugins()
            if p.capabilities().requires_harness_runner
            and p not in plugins
            and p.is_available()[0]
        ]
        plugins.extend(runner_plugins)

    ctx = EvalContext(
        response=response,
        expected=expected,
        criteria=criteria,
        harness_runner=harness_runner,
        persona=persona,
        goal=goal,
        constraints=list(constraints or []),
        rubric=list(rubric or []),
        max_turns=max_turns,
        evaluation_mode=evaluation_mode,
        trace_id=trace_id or "no-trace",
        input_context=dict(input_context or {}),
        progress_callback=progress_callback,
        release_harness_session=release_harness_session,
    )

    # Direct-invoke fast path for unambiguous multi-call modalities.
    # Real-run signal (voice_dual_4): for voice_conversation tests,
    # Claude's tool_runner SOMETIMES picked the voice_realtime plugin
    # (ElevenLabs Voice Stack → 5 turns driven, verdict promoted) and
    # SOMETIMES skipped it entirely (OpenAI Voice Stack → llm_judge
    # fallback, single-turn). Same input, same eligible plugins,
    # different outcomes — non-determinism in tool-picking. When a
    # plugin OWNS the modality (modality enum match + requires_harness_
    # runner=True) AND the test is multi-call AND a runner is
    # available, skip Claude's tool_runner for THIS evaluator and
    # invoke the plugin directly. The plugin's drive_conversation IS
    # the authoritative scoring path; Claude's tool_picker has nothing
    # to add. Falls through to the standard tool_runner only when no
    # such "obvious owner" plugin exists.
    if harness_runner is not None and is_multi_call_test:
        owners = [
            p for p in plugins
            if p.capabilities().requires_harness_runner
            and p.is_available()[0]
            and (
                input_type in p.capabilities().input_types
                or output_type in p.capabilities().output_types
            )
        ]
        # Priority: prefer the plugin whose OUTPUT_TYPES match the test's
        # output_type exactly (most specific). Falls back to input_type
        # match. Without this, the first eligible plugin in registration
        # order wins — e.g., conversation_simulator was getting picked
        # for voice_conversation tests where voice_realtime is the
        # natural owner. Same priority order for any future modality:
        # add a plugin with the matching output_types and it auto-wins.
        def _owner_priority(p):
            caps = p.capabilities()
            return (
                0 if output_type and output_type in caps.output_types else 1,
                0 if input_type and input_type in caps.input_types else 1,
                p.name,
            )
        owners.sort(key=_owner_priority)
        if owners:
            owner = owners[0]
            logger.info(
                "tool_runner: direct-invoking owner plugin '%s' for "
                "multi-call modality (input_type=%s output_type=%s)",
                owner.name, input_type, output_type,
                extra={
                    "operation": "tool_runner_direct_invoke_owner",
                    "trace_id": trace_id,
                    "plugin_name": owner.name,
                },
            )
            try:
                # Direct-invoke path: forward the agentic conversational
                # context the same way the _impl closure does. This is
                # the PRIMARY path for voice_conversation / conversation
                # tests (the earlier voice_realtime + conversation_
                # simulator fast-paths route through here), so if we
                # dropped persona/goal/rubric here, the agentic pipeline
                # would silently fall back to scripted even for
                # properly-emitted Agent 3 tests.
                verdict = owner.evaluate_output(
                    response=ctx.response,
                    expected=ctx.expected,
                    criteria=ctx.criteria,
                    harness_runner=ctx.harness_runner,
                    persona=ctx.persona,
                    goal=ctx.goal,
                    constraints=ctx.constraints,
                    rubric=ctx.rubric,
                    max_turns=ctx.max_turns,
                    evaluation_mode=ctx.evaluation_mode,
                    trace_id=ctx.trace_id,
                    input_context=ctx.input_context,
                    semantic_review_required=True,
                    progress_callback=ctx.progress_callback,
                    release_harness_session=ctx.release_harness_session,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "owner plugin %s crashed during direct invoke: %s",
                    owner.name, exc,
                    extra={"operation": "owner_plugin_crash",
                           "trace_id": trace_id},
                )
                verdict = None
            if verdict is not None and not getattr(verdict, "fallback_reason", None):
                # Pull artifacts via the same priority list as the
                # tool-runner-internal path (verdict.detail.audio_paths
                # → artifacts_for_token_prefix → artifacts_for_token).
                detail = getattr(verdict, "detail", None) or {}
                if isinstance(detail, dict):
                    detail_paths = detail.get("audio_paths")
                    if isinstance(detail_paths, list):
                        for item in detail_paths:
                            if isinstance(item, dict) and item.get("path"):
                                ctx.plugin_artifacts.append(item)
                if not ctx.plugin_artifacts:
                    token = None
                    if isinstance(ctx.expected, dict):
                        token = ctx.expected.get("token") or ctx.expected.get("session_token")
                    if not token and isinstance(detail, dict):
                        token = detail.get("session_token")
                    if token and (
                        hasattr(owner, "artifacts_for_token_prefix")
                        or hasattr(owner, "artifacts_for_token")
                    ):
                        try:
                            if hasattr(owner, "artifacts_for_token_prefix"):
                                arts = owner.artifacts_for_token_prefix(token)
                            else:
                                arts = owner.artifacts_for_token(token)
                            if arts:
                                ctx.plugin_artifacts.extend(arts)
                        except Exception:  # noqa: BLE001
                            pass
                ctx.tools_invoked.append(owner.name)
                return ToolRunnerVerdict(
                    passed=bool(verdict.passed),
                    score=float(verdict.score or 0.0),
                    reasoning=(
                        f"[direct-invoke {owner.name}] "
                        + str(verdict.reasoning)
                    )[:4500],
                    tools_invoked=list(ctx.tools_invoked),
                    artifacts=list(ctx.plugin_artifacts),
                    cost_usd=0.0,
                    iterations=0,
                    fallback_reason=None,
                    # Thread the plugin's full detail dict through so
                    # _promote_verdict_to_tcr can populate rubric_verdict
                    # + transcript on TestCaseResult. Without this the
                    # agentic rubric breakdown never reaches the frontend.
                    verdict_detail=detail if isinstance(detail, dict) else {},
                )
            # Direct invoke either crashed OR returned a fallback_reason.
            # Fall through to Claude's tool_runner so it can still pick
            # an alternative plugin or use llm_judge. We don't lose
            # anything — the failed direct attempt was free.
    use_tool_search = len(plugins) >= EVAL_TOOL_SEARCH_THRESHOLD
    tools = build_tool_list(
        ctx,
        plugins=plugins,
        allow_code_execution=EVAL_PROGRAMMATIC_CHAINING_ENABLED,
        use_tool_search=use_tool_search,
    )

    system_prompt = with_preamble(EVAL_SYSTEM_PROMPT)
    user_prompt = _compose_user_prompt(
        response=response,
        expected=expected,
        criteria=criteria,
        test_scenario=test_scenario,
    )

    extra_kwargs: dict[str, Any] = {}
    ocfg = output_config_for_request()
    if ocfg:
        extra_kwargs["output_config"] = ocfg
    if EVAL_PROGRAMMATIC_CHAINING_ENABLED:
        # code_execution_20260120 (the tool TYPE has date 2026-01-20) requires
        # the `code-execution-2025-08-25` beta family (the HEADER keeps the
        # older date — it's Anthropic's versioning convention where beta
        # families version independently of tool types). Verified against
        # https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool
        # which lists `betas=["code-execution-2025-08-25"]` for the 20260120
        # tool. Wrong value => 400 invalid_beta, every tool_runner call
        # short-circuits to fallback_reason — worst kind of silent failure.
        extra_kwargs["betas"] = ["code-execution-2025-08-25"]

    total_cost = 0.0
    iterations = 0
    final_parsed: ScoreVerdict | None = None
    try:
        runner = client.beta.messages.tool_runner(
            model=model,
            max_tokens=2048,
            system=[{"type": "text", "text": system_prompt}],
            messages=[{"role": "user", "content": user_prompt}],
            tools=tools,
            thinking={"type": "adaptive"},
            max_iterations=max_iterations,
            output_format=ScoreVerdict,
            **extra_kwargs,
        )
        for message in runner:
            iterations += 1
            usage = getattr(message, "usage", None)
            if usage is not None:
                total_cost += _estimate_cost(usage, model)
            # The final message is the structured output; earlier
            # messages are tool_use / tool_result shuffling that the
            # SDK handles internally.
            parsed = getattr(message, "parsed", None)
            if isinstance(parsed, ScoreVerdict):
                final_parsed = parsed
    except anthropic.APIError as exc:
        logger.warning(
            "tool_runner APIError: %s", exc,
            extra={"operation": "tool_runner_api_error", "trace_id": trace_id},
        )
        return ToolRunnerVerdict(
            passed=False, score=0.0,
            reasoning=f"tool_runner API error: {type(exc).__name__}",
            tools_invoked=ctx.tools_invoked,
            artifacts=ctx.plugin_artifacts,
            cost_usd=total_cost,
            iterations=iterations,
            fallback_reason=f"api_error:{type(exc).__name__}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tool_runner unexpected error: %s", exc,
            extra={"operation": "tool_runner_crash", "trace_id": trace_id},
        )
        return ToolRunnerVerdict(
            passed=False, score=0.0,
            reasoning=f"tool_runner crashed: {exc}",
            tools_invoked=ctx.tools_invoked,
            artifacts=ctx.plugin_artifacts,
            cost_usd=total_cost,
            iterations=iterations,
            fallback_reason=f"runner_crash:{type(exc).__name__}",
        )

    if final_parsed is None:
        # Promotion path: when Claude didn't emit a ScoreVerdict but a
        # plugin produced a conclusive verdict during the loop, use that
        # plugin's verdict directly. Plugin scoring IS authoritative for
        # modality-matched evaluations (e.g., voice_realtime's
        # drive_conversation ran all turns and computed overall_score).
        # Forcing Claude to re-emit a structured verdict just to rubber-
        # stamp the plugin wastes a round-trip AND silently loses the
        # per-turn data when the stamp doesn't land.
        #
        # "Conclusive" = a plugin verdict with no fallback_reason AND
        # non-empty reasoning. The LAST such verdict wins (if Claude
        # chained multiple plugins, the terminal one is closest to the
        # decision Claude would have expressed in the ScoreVerdict).
        conclusive = [
            v for v in ctx.plugin_verdicts
            if v.fallback_reason is None and v.reasoning.strip()
        ]
        if conclusive:
            promoted = conclusive[-1]
            logger.info(
                "tool_runner: promoting plugin '%s' verdict "
                "(passed=%s score=%.2f) — Claude didn't emit ScoreVerdict "
                "after %d iterations",
                promoted.plugin_name, promoted.passed, promoted.score,
                iterations,
                extra={
                    "operation": "tool_runner_promote_plugin_verdict",
                    "trace_id": trace_id,
                    "plugin_name": promoted.plugin_name,
                },
            )
            return ToolRunnerVerdict(
                passed=promoted.passed,
                score=promoted.score,
                reasoning=(
                    f"[promoted from {promoted.plugin_name}] "
                    + promoted.reasoning
                ),
                tools_invoked=list(ctx.tools_invoked),
                artifacts=list(ctx.plugin_artifacts),
                cost_usd=total_cost,
                iterations=iterations,
                fallback_reason=None,
                # Promote the captured plugin's detail dict so agentic
                # conversational metadata (rubric_verdict + transcript)
                # still lands on TestCaseResult even when Claude's
                # tool_runner failed to emit a final ScoreVerdict.
                verdict_detail=dict(promoted.detail) if promoted.detail else {},
            )

        # No conclusive plugin verdict either — truly fall through to
        # LLM-judge fallback. Reasoning includes what we tried so the
        # caller can triage.
        tried_note = ""
        if ctx.plugin_verdicts:
            tried_note = (
                " Plugins tried: "
                + ", ".join(
                    f"{v.plugin_name}({v.fallback_reason or 'empty'})"
                    for v in ctx.plugin_verdicts
                )
            )
        return ToolRunnerVerdict(
            passed=False, score=0.0,
            reasoning=(
                "tool_runner finished without producing a structured verdict "
                f"after {iterations} iterations.{tried_note}"
            ),
            tools_invoked=ctx.tools_invoked,
            artifacts=ctx.plugin_artifacts,
            cost_usd=total_cost,
            iterations=iterations,
            fallback_reason="no_structured_verdict",
        )

    return ToolRunnerVerdict(
        passed=final_parsed.passed,
        score=final_parsed.score,
        reasoning=final_parsed.reasoning,
        tools_invoked=list(ctx.tools_invoked),
        artifacts=list(ctx.plugin_artifacts),
        cost_usd=total_cost,
        iterations=iterations,
        fallback_reason=None,
    )


__all__ = [
    "EVAL_SYSTEM_PROMPT",
    "EvalContext",
    "ScoreVerdict",
    "ToolRunnerVerdict",
    "build_tool_list",
    "eligible_plugins",
    "evaluate_with_tool_runner",
]
