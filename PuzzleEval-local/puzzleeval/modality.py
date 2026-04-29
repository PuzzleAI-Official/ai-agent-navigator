"""Modality detection — picks the right tool plugins for a test case.

Given a TestCase (or a candidate's atlas), this module returns the set
of plugins required to evaluate it correctly. The selection is fully
data-driven from the schema enums:

  - ``input_type`` ∈ {text, structured_data, document_content,
                       conversation, image_description, audio_content,
                       file_reference, code}
  - ``output_type`` ∈ {free_text, structured_json, classification,
                        extraction, action, media_url, code, audio_content}

The detector queries the plugin registry for every plugin claiming to
handle the test case's input/output types. The caller (Agent 5
evaluator) tries plugins in the returned order and falls back to the
LLM judge when no plugin produces a usable verdict.

Modality detection NEVER hardcodes a capability ("if scope_role ==
'voice' then transcription"). It branches purely on the SCHEMA enums
that Agent 1's TestPlan and Agent 3's TestCase already carry. Adding a
new modality means: register a plugin that declares the relevant
input_type/output_type, and the detector picks it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from puzzleeval.tool_plugins import (
    ToolPlugin,
    find_plugins_for_input_type,
    find_plugins_for_output_type,
)


@dataclass
class ModalityRequirements:
    """Plugins required to fully exercise + evaluate one test case.

    ``input_synthesizers``: plugins that can MANUFACTURE input for this
        modality when the test case has none on disk (e.g. TTS for an
        audio_content scope when the user has no recordings).
    ``output_evaluators``: plugins that can SCORE the harness response
        (transcription for audio responses, code_execution for code,
        vision for images, conversation_simulator for multi-turn).
    ``unavailable``: plugins that match by capability but failed
        ``is_available()`` (missing credential / toolchain). Surfaced
        so the caller can warn the user "to evaluate audio responses,
        set OPENAI_API_KEY for the transcription plugin."
    """
    input_synthesizers: list[ToolPlugin] = field(default_factory=list)
    output_evaluators: list[ToolPlugin] = field(default_factory=list)
    unavailable: list[tuple[str, str]] = field(default_factory=list)  # (plugin name, reason)


def detect_for_test_case(
    *, input_type: str, output_type: str,
) -> ModalityRequirements:
    """Pick plugins for a single (input_type, output_type) pair."""
    requirements = ModalityRequirements()

    seen_synth: set[str] = set()
    for plugin in find_plugins_for_input_type(input_type):
        caps = plugin.capabilities()
        if not caps.synthesizes_input:
            continue
        if plugin.name in seen_synth:
            continue
        seen_synth.add(plugin.name)
        ok, reason = plugin.is_available()
        if ok:
            requirements.input_synthesizers.append(plugin)
        else:
            requirements.unavailable.append((plugin.name, f"input synth: {reason}"))

    seen_eval: set[str] = set()
    for plugin in find_plugins_for_output_type(output_type):
        caps = plugin.capabilities()
        if not caps.evaluates_output:
            continue
        if plugin.name in seen_eval:
            continue
        seen_eval.add(plugin.name)
        ok, reason = plugin.is_available()
        if ok:
            requirements.output_evaluators.append(plugin)
        else:
            requirements.unavailable.append((plugin.name, f"output eval: {reason}"))

    return requirements


def detect_for_test_plan(test_plan: Any) -> dict[str, ModalityRequirements]:
    """Walk a TestPlan and return per-scope ModalityRequirements.

    Returns empty dict when ``test_plan`` is None or carries no scope_specs.
    """
    out: dict[str, ModalityRequirements] = {}
    if test_plan is None:
        return out
    specs = getattr(test_plan, "scope_specs", None) or []
    for spec in specs:
        scope_id = getattr(spec, "scope_id", "")
        if not scope_id:
            continue
        out[scope_id] = detect_for_test_case(
            input_type=getattr(spec, "input_type", ""),
            output_type=getattr(spec, "output_type", ""),
        )
    return out


def summarize_unavailable(
    requirements_by_scope: dict[str, ModalityRequirements],
) -> list[str]:
    """Roll up unique 'plugin X needs credential Y' messages so the
    pipeline summary can flag them once instead of per-scope."""
    unique: dict[str, str] = {}
    for reqs in requirements_by_scope.values():
        for plugin_name, reason in reqs.unavailable:
            unique.setdefault(plugin_name, reason)
    return [f"{name}: {reason}" for name, reason in sorted(unique.items())]


__all__ = [
    "ModalityRequirements",
    "detect_for_test_case",
    "detect_for_test_plan",
    "summarize_unavailable",
]
