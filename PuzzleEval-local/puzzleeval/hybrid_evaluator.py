"""Hybrid LLM-judge + Claude-picked-plugins evaluator.

The deterministic dispatch in `_run_tests_for_candidate` handles cases
where `(input_type, output_type)` clearly maps to a single plugin. For
the remaining ambiguous-modality cases, the existing LLM judge runs
without any plugin assistance.

This module ships an OPT-IN third path: invoke Claude with the SAME
plugins exposed as Anthropic-callable tools, plus adaptive thinking +
effort. Claude inspects the test response, decides whether ANY plugin
would sharpen its verdict, calls one if useful, then produces a final
structured score.

Reproducibility: this path is non-deterministic by construction (Claude
picks tools per request). Use it for cases where ambiguity makes
deterministic dispatch impossible — NOT as the primary path. Default
off; opt in via `PUZZLEEVAL_HYBRID_EVAL_ENABLED=1`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import anthropic

from puzzleeval.agent_preamble import with_preamble
from puzzleeval.config import (
    DEFAULT_MODEL,
    output_config_for_request,
)
from puzzleeval.plugin_tools import (
    build_plugin_tool_definitions,
    dispatch_plugin_tool,
    serialize_for_tool_result,
)

logger = logging.getLogger(__name__)


_HYBRID_SYSTEM = """\
You are scoring a candidate API's response against an expected output. \
Five plugin tools are available — call one if and only if it sharpens \
your verdict. Examples of when calling a plugin helps:

- The response looks like code → `score_with_code_execution` to run it
- The response carries an audio URL → `score_with_transcription` to STT
- The response is an image URL → `score_with_vision` to inspect it
- The expected output is a multi-turn script → `score_with_conversation_simulator`

If the response is plainly text vs text, score it directly without \
calling any plugin. After your reasoning, end with a JSON block in this \
exact shape (no prose after):

```json
{"passed": true, "score": 0.0, "reasoning": "<one paragraph>"}
```

`passed` is a boolean. `score` is a float 0.0-1.0. Be terse — the \
reasoning is for the report, not for the model.
"""


_VERDICT_PATTERN = re.compile(
    r"```json\s*\n(.*?)\n```",
    re.DOTALL,
)


@dataclass
class HybridVerdict:
    passed: bool
    score: float
    reasoning: str
    tools_called: list[str] = field(default_factory=list)
    fallback_reason: str | None = None
    raw_text: str = ""


def _extract_verdict(text: str) -> HybridVerdict | None:
    match = _VERDICT_PATTERN.search(text)
    blob = match.group(1) if match else text.strip()
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        score = float(parsed.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    return HybridVerdict(
        passed=bool(parsed.get("passed", score >= 0.5)),
        score=score,
        reasoning=str(parsed.get("reasoning", "")).strip()[:1000],
        raw_text=text,
    )


def evaluate_with_claude_picked_plugins(
    *,
    response: Any,
    expected: Any,
    criteria: list[dict] | None = None,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
    max_tool_iterations: int = 4,
) -> HybridVerdict:
    """Score a response using Claude with all available plugin tools exposed.

    Loops until Claude stops calling tools (or hits ``max_tool_iterations``).
    Returns a HybridVerdict with the final score + the names of plugins
    Claude actually invoked.

    On any error (API failure, empty response, unparseable verdict),
    returns a HybridVerdict with ``fallback_reason`` set so the caller
    can decide to retry, fall back to the LLM judge, or surface the
    error in the test result.
    """
    if client is None:
        from puzzleeval.anthropic_client import build_client
        client = build_client()
    if model is None:
        model = DEFAULT_MODEL

    plugin_tools = build_plugin_tool_definitions(only_evaluators=True)
    if not plugin_tools:
        return HybridVerdict(
            passed=False, score=0.0, reasoning="",
            fallback_reason="no_plugins_available",
        )

    response_str = response if isinstance(response, str) else json.dumps(response, default=str)[:4000]
    expected_str = expected if isinstance(expected, str) else json.dumps(expected, default=str)[:4000]
    criteria_str = "\n".join(
        f"- {c.get('criterion','(unnamed)')} (weight {c.get('weight',0):.2f})"
        for c in (criteria or [])
    ) or "- Overall fidelity to expected output (weight 1.00)"

    user_prompt = (
        "Test response (from candidate API):\n"
        f"{response_str}\n\n"
        "Expected output (ground truth):\n"
        f"{expected_str}\n\n"
        f"Criteria to weigh:\n{criteria_str}\n"
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    tools_called: list[str] = []

    extra_kwargs: dict[str, object] = {}
    ocfg = output_config_for_request()
    if ocfg:
        extra_kwargs["output_config"] = ocfg

    last_text = ""
    for iteration in range(max_tool_iterations + 1):
        try:
            api_resp = client.messages.create(
                model=model,
                max_tokens=2048,
                system=[{"type": "text", "text": with_preamble(_HYBRID_SYSTEM)}],
                messages=messages,
                tools=plugin_tools,
                thinking={"type": "adaptive"},
                **extra_kwargs,
            )
        except anthropic.APIError as exc:
            logger.warning("hybrid eval API error: %s", exc)
            return HybridVerdict(
                passed=False, score=0.0, reasoning="",
                tools_called=tools_called,
                fallback_reason=f"api_error: {type(exc).__name__}",
            )

        # Collect text + tool_use blocks
        text_parts: list[str] = []
        tool_uses: list[Any] = []
        for block in api_resp.content or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                tool_uses.append(block)
        last_text = "\n".join(text_parts).strip()

        # If no tool calls, model is done
        if not tool_uses or api_resp.stop_reason in {"end_turn", "stop_sequence"}:
            break

        # Append assistant message + tool results
        messages.append({"role": "assistant", "content": api_resp.content})
        tool_results: list[dict[str, Any]] = []
        for tu in tool_uses:
            tool_name = getattr(tu, "name", "")
            tool_input = getattr(tu, "input", {}) or {}
            tools_called.append(tool_name)
            try:
                payload = dispatch_plugin_tool(tool_name, tool_input)
            except Exception as exc:  # noqa: BLE001
                payload = {"error": f"dispatch crashed: {exc}"}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": getattr(tu, "id", ""),
                "content": serialize_for_tool_result(payload),
            })
        messages.append({"role": "user", "content": tool_results})

    # Extract the final verdict from last_text
    verdict = _extract_verdict(last_text)
    if verdict is None:
        return HybridVerdict(
            passed=False, score=0.0, reasoning=last_text[:500],
            tools_called=tools_called,
            fallback_reason="verdict_unparseable",
            raw_text=last_text,
        )
    verdict.tools_called = tools_called
    return verdict


__all__ = [
    "HybridVerdict",
    "evaluate_with_claude_picked_plugins",
]
