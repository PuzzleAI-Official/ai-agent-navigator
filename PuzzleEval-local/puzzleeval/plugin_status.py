"""Single source of truth for "which plugins are wired and ready right now."

Answers, in one place:

  - Which plugins are registered? (always all 5 — auto-imported at package load)
  - Which agents call which plugins?  (synthesis vs evaluation)
  - What credential does each plugin need? Is it set right now?
  - What configuration advice should we surface to the user?

Used by:
  - `pipeline_summary.json` builder to surface missing-credential advisories
  - The CLI `--plugins` flag to print the readiness matrix
  - Agent 5's evaluator + synthesis paths to skip plugins gracefully when
    not credentialed (they fall back to LLM judging)

The reporter never executes plugins or makes API calls — it inspects
declared capabilities, consults the env, and returns a structured snapshot.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from puzzleeval.tool_plugins import (
    ToolPlugin,
    list_plugins,
)


# ---------------------------------------------------------------------------
# Wiring map — which agents actually consume each plugin in production today.
# Updated when new dispatch paths land. Used by the status table to make
# "registered but unwired" plugins visible (a foot-gun otherwise).
# ---------------------------------------------------------------------------

# Synthesis: an agent calls plugin.synthesize_input() to produce a test input.
# Evaluation: an agent calls plugin.evaluate_output() to score a response.
# "Builder context": Agent 5 mentions the plugin's name in its system prompt
#   so the model knows the contract. (Doesn't actually invoke.)

PLUGIN_WIRING: dict[str, dict[str, list[str]]] = {
    "code_execution": {
        "synthesis": ["agent_5_test_input_synthesis"],
        "evaluation": ["agent_5_evaluator_dispatch"],
        "builder_context": ["agent_5_builder_initial_message"],
    },
    "vision": {
        "synthesis": [],
        "evaluation": ["agent_5_evaluator_dispatch"],
        "builder_context": ["agent_5_builder_initial_message"],
    },
    "transcription": {
        "synthesis": [],
        "evaluation": ["agent_5_evaluator_dispatch"],
        "builder_context": ["agent_5_builder_initial_message"],
    },
    "tts": {
        "synthesis": ["agent_5_test_input_synthesis"],
        "evaluation": [],
        "builder_context": ["agent_5_builder_initial_message"],
    },
    "conversation_simulator": {
        "synthesis": ["agent_5_test_input_synthesis"],
        "evaluation": ["agent_5_evaluator_dispatch"],
        "builder_context": ["agent_5_builder_initial_message"],
    },
    "webhook_receiver": {
        "synthesis": ["agent_5_test_input_synthesis"],
        "evaluation": ["agent_5_evaluator_dispatch"],
        "builder_context": ["agent_5_builder_initial_message"],
    },
    "outbound_delivery": {
        "synthesis": ["agent_5_test_input_synthesis"],
        "evaluation": ["agent_5_evaluator_dispatch"],
        "builder_context": ["agent_5_builder_initial_message"],
    },
    "voice_realtime": {
        "synthesis": ["agent_5_test_input_synthesis"],
        "evaluation": ["agent_5_evaluator_dispatch"],
        "builder_context": ["agent_5_builder_initial_message"],
    },
}


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------


@dataclass
class CredentialStatus:
    """Per-credential availability snapshot for one plugin."""
    name: str
    is_set: bool
    advice: str = ""  # human-readable advice when missing


@dataclass
class PluginStatus:
    """One row of the readiness matrix."""
    name: str
    is_available: bool
    unavailable_reason: str
    capabilities_input_types: list[str]
    capabilities_output_types: list[str]
    synthesizes_input: bool
    evaluates_output: bool
    declared_credentials: list[CredentialStatus]
    wired_for_synthesis_in: list[str]
    wired_for_evaluation_in: list[str]
    wired_for_builder_context_in: list[str]


def _credential_advice(plugin_name: str, var: str) -> str:
    """Plugin-specific advice for setting a missing credential."""
    advice_map = {
        ("transcription", "OPENAI_API_KEY"): "Get a key at platform.openai.com — STT via Whisper costs ~$0.006/min",
        ("transcription", "DEEPGRAM_API_KEY"): "Get a key at deepgram.com — free tier covers ~12k minutes/year",
        ("transcription", "ASSEMBLYAI_API_KEY"): "Get a key at assemblyai.com — free tier covers ~5 hr/month",
        ("tts", "OPENAI_API_KEY"): "OpenAI tts-1 costs $15/1M chars; 'voice' env override: PUZZLEEVAL_TTS_VOICE",
        ("tts", "ELEVENLABS_API_KEY"): "ElevenLabs free tier ~10k chars/month; voice ID env: PUZZLEEVAL_ELEVENLABS_VOICE_ID",
        ("vision", "ANTHROPIC_API_KEY"): "Same Anthropic key the rest of PuzzleEval uses; should already be set",
    }
    return advice_map.get(
        (plugin_name, var),
        f"Set the env var {var} to enable this plugin.",
    )


def snapshot_plugin(plugin: ToolPlugin) -> PluginStatus:
    caps = plugin.capabilities()
    available, reason = plugin.is_available()

    # Build credential rows. ANY-of semantics: plugins like transcription
    # accept any one of several keys.
    cred_rows: list[CredentialStatus] = []
    for var in caps.requires_credentials:
        is_set = bool(os.environ.get(var))
        cred_rows.append(CredentialStatus(
            name=var,
            is_set=is_set,
            advice=_credential_advice(plugin.name, var) if not is_set else "",
        ))

    wiring = PLUGIN_WIRING.get(plugin.name, {})
    return PluginStatus(
        name=plugin.name,
        is_available=available,
        unavailable_reason=reason,
        capabilities_input_types=caps.input_types,
        capabilities_output_types=caps.output_types,
        synthesizes_input=caps.synthesizes_input,
        evaluates_output=caps.evaluates_output,
        declared_credentials=cred_rows,
        wired_for_synthesis_in=wiring.get("synthesis", []),
        wired_for_evaluation_in=wiring.get("evaluation", []),
        wired_for_builder_context_in=wiring.get("builder_context", []),
    )


def snapshot_all() -> list[PluginStatus]:
    """Snapshot every registered plugin."""
    return [snapshot_plugin(p) for p in list_plugins()]


def to_dict(status_list: list[PluginStatus]) -> dict[str, Any]:
    """Serialize the snapshot for pipeline_summary.json."""
    return {
        "plugins": [asdict(s) for s in status_list],
        "summary": {
            "registered_count": len(status_list),
            "available_count": sum(1 for s in status_list if s.is_available),
            "wired_for_evaluation_count": sum(
                1 for s in status_list if s.wired_for_evaluation_in
            ),
            "wired_for_synthesis_count": sum(
                1 for s in status_list if s.wired_for_synthesis_in
            ),
        },
    }


# ---------------------------------------------------------------------------
# Pretty-print for the CLI / log
# ---------------------------------------------------------------------------


def format_text_table(status_list: list[PluginStatus]) -> str:
    """Compact human-readable table for CLI output."""
    lines = []
    lines.append("PLUGIN STATUS — readiness matrix")
    lines.append("=" * 70)
    for s in status_list:
        status_marker = "[READY]" if s.is_available else "[BLOCKED]"
        lines.append(f"{status_marker} {s.name}")
        lines.append(f"  inputs:  {', '.join(s.capabilities_input_types) or '(none)'}")
        lines.append(f"  outputs: {', '.join(s.capabilities_output_types) or '(none)'}")
        if not s.is_available:
            lines.append(f"  blocked: {s.unavailable_reason}")
        cred_lines = []
        for c in s.declared_credentials:
            mark = "[set]" if c.is_set else "[unset]"
            cred_lines.append(f"    {mark} {c.name}")
            if c.advice and not c.is_set:
                cred_lines.append(f"        -> {c.advice}")
        if cred_lines:
            lines.append("  credentials (any one suffices when listed together):")
            lines.extend(cred_lines)
        lines.append(
            f"  wired into: synthesis={s.wired_for_synthesis_in or 'none'}; "
            f"evaluation={s.wired_for_evaluation_in or 'none'}"
        )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Advisories — one-line strings for pipeline_summary.metadata
# ---------------------------------------------------------------------------


def collect_advisories(status_list: list[PluginStatus] | None = None) -> list[str]:
    """Return one-line "set X to enable Y" advisories for missing credentials.

    Deduped per plugin: if transcription accepts any of 3 keys and none are
    set, returns ONE line listing all three (not three separate lines).
    """
    if status_list is None:
        status_list = snapshot_all()
    out: list[str] = []
    for s in status_list:
        if s.is_available:
            continue
        if not s.declared_credentials:
            continue
        keys = [c.name for c in s.declared_credentials if not c.is_set]
        if not keys:
            continue
        if len(keys) == 1:
            advice = s.declared_credentials[0].advice
            out.append(f"plugin '{s.name}' inactive — set {keys[0]} ({advice})")
        else:
            out.append(
                f"plugin '{s.name}' inactive — set ANY of {keys} "
                f"to enable {', '.join(s.capabilities_output_types) or 'this modality'}"
            )
    return out


__all__ = [
    "CredentialStatus",
    "PLUGIN_WIRING",
    "PluginStatus",
    "collect_advisories",
    "format_text_table",
    "snapshot_all",
    "snapshot_plugin",
    "to_dict",
]
