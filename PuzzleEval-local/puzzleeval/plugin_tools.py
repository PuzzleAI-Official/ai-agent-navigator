"""Adapter that exposes PuzzleEval plugins as Claude-callable Anthropic tools.

The deterministic modality dispatch in `_run_tests_for_candidate` handles
80%+ of test cases — when `(input_type, output_type)` clearly maps to a
single plugin (audio → transcription, code → code_execution, etc.), we
score in a reproducible way without involving Claude.

The remaining ~20% are AMBIGUOUS modalities:
  - `output_type="free_text"` but the response IS code (and we want to run it)
  - `output_type="structured_json"` but the JSON contains an audio URL we
    could transcribe as a sanity check
  - User-supplied custom criteria that name a tool by capability

For those cases, this module exposes the 5 plugins as Anthropic tools
that Claude can call from inside the LLM-judge batch evaluation. Claude
gets to decide whether ANY plugin would help, picks one, sees its
verdict, and incorporates it into the final score reasoning.

Reproducibility note: Claude-picked dispatch is non-deterministic by
construction. We use this path ONLY for cases the deterministic
dispatcher already declined. Same test run twice WILL produce
deterministic scores for the modality-clear majority; will produce
slightly variable scores for the modality-ambiguous fallback minority.
That trade-off is correct: ambiguous cases benefit from judgment more
than they need pinpoint reproducibility.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from puzzleeval.tool_plugins import (
    EvaluationResult,
    ToolPlugin,
    list_plugins,
)

logger = logging.getLogger(__name__)


def build_plugin_tool_definitions(
    plugins: list[ToolPlugin] | None = None,
    only_evaluators: bool = True,
) -> list[dict[str, Any]]:
    """Convert registered plugins into Anthropic tool definitions.

    Each tool's name follows ``score_with_<plugin_name>`` so Claude sees
    the intent immediately. Input schema is the SAME minimal shape for
    every plugin (response, expected, criteria, optional language) — the
    dispatcher routes based on the tool name, not the schema.

    Returns only plugins that:
      - Are currently `is_available()` (no missing credentials)
      - Have `evaluates_output=True` when ``only_evaluators=True`` (default)

    The returned list is suitable to pass directly as ``tools=[...]`` in
    a `client.messages.create` / `client.messages.parse` call.
    """
    out: list[dict[str, Any]] = []
    for p in plugins if plugins is not None else list_plugins():
        caps = p.capabilities()
        if only_evaluators and not caps.evaluates_output:
            continue
        ok, _ = p.is_available()
        if not ok:
            continue
        description = (
            f"Score a candidate's response using the {p.name} plugin. "
            f"Handles input_types={caps.input_types or '(any)'}, "
            f"output_types={caps.output_types or '(any)'}. "
            f"{caps.notes}"
        )
        out.append({
            "name": f"score_with_{p.name}",
            "description": description.strip(),
            "input_schema": {
                "type": "object",
                "properties": {
                    "response": {
                        "description": (
                            "The candidate API's raw response. May be a string, "
                            "URL, or JSON object — the plugin parses it."
                        ),
                        # Anthropic accepts string here; Claude will JSON-stringify
                        # complex objects when needed.
                        "type": "string",
                    },
                    "expected": {
                        "description": (
                            "The expected output / ground truth from the test "
                            "case. For code, JSON-encoded {expected_function, "
                            "test_inputs, test_outputs}. For audio, expected "
                            "transcript text. For images, natural-language "
                            "description."
                        ),
                        "type": "string",
                    },
                    "language": {
                        "description": (
                            "Only for code_execution: the programming language "
                            "(python, javascript, typescript, go, rust, bash). "
                            "Inferred from response when omitted."
                        ),
                        "type": "string",
                    },
                },
                "required": ["response", "expected"],
            },
        })
    return out


def dispatch_plugin_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    plugins: list[ToolPlugin] | None = None,
) -> dict[str, Any]:
    """Run a Claude-invoked plugin tool and return a JSON-serializable result.

    ``tool_name`` must be of the form ``score_with_<plugin_name>``. Returns:
      - On success: a dict with passed/score/reasoning + the plugin name
      - On unknown tool name: ``{"error": "unknown plugin tool"}``
      - On plugin crash: ``{"error": "<exception class>: <message>"}``
      - On `fallback_reason` (plugin couldn't run): forwards the reason

    The dispatcher catches every exception so the surrounding LLM judge
    can incorporate the failure into its reasoning instead of crashing.
    """
    if not tool_name.startswith("score_with_"):
        return {"error": f"unknown plugin tool: {tool_name}"}
    plugin_name = tool_name[len("score_with_"):]
    pool = plugins if plugins is not None else list_plugins()
    plugin = next((p for p in pool if p.name == plugin_name), None)
    if plugin is None:
        return {"error": f"plugin '{plugin_name}' not registered"}
    # Claude passes complex tool inputs as JSON strings (the schema declares
    # `type: string`). Decode them so the plugin sees the dict / list it
    # expects. String-only payloads fall through unchanged.
    def _maybe_decode(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not (stripped.startswith(("{", "[")) and stripped.endswith(("}", "]"))):
            return value
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value

    response_arg = _maybe_decode(tool_input.get("response", ""))
    expected_arg = _maybe_decode(tool_input.get("expected", ""))
    try:
        verdict: EvaluationResult = plugin.evaluate_output(
            response=response_arg,
            expected=expected_arg,
            criteria=[],
            language=tool_input.get("language"),
        )
    except TypeError:
        # Some plugins (vision) ignore unknown kwargs; others raise. Retry
        # without the language kwarg for compatibility.
        try:
            verdict = plugin.evaluate_output(
                response=response_arg,
                expected=expected_arg,
                criteria=[],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin %s crashed: %s", plugin_name, exc)
            return {"error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin %s crashed: %s", plugin_name, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
    payload: dict[str, Any] = {
        "plugin": plugin_name,
        "passed": verdict.passed,
        "score": verdict.score,
        "reasoning": verdict.reasoning[:500],
    }
    if verdict.fallback_reason:
        payload["fallback_reason"] = verdict.fallback_reason
    if verdict.detail:
        # Trim potentially-large detail dicts to stay under tool_result size limits
        try:
            payload["detail"] = json.loads(json.dumps(verdict.detail, default=str))
        except (TypeError, ValueError):
            payload["detail"] = {"_note": "detail not JSON-serializable"}
    return payload


def serialize_for_tool_result(payload: dict[str, Any]) -> str:
    """Stringify the dispatch result for inclusion in a tool_result block."""
    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = [
    "build_plugin_tool_definitions",
    "dispatch_plugin_tool",
    "serialize_for_tool_result",
]
