"""Capability predicates — generalizable boundaries for code gates.

Per the Phase 2 plan's generalizability principle (Section 2 / Codex C2):
gates keyed on a capability predicate survive new modalities; gates keyed
on enumerated sets like ``input_type in {voice_*, audio_content, conversation}``
are brittle and need to be revised for every new modality.

When a new modality is added (e.g., ``video_conversation``,
``multimodal_chat``), update the predicate here once. Every gate that
queries the predicate gets the new behavior automatically. Gates do
NOT enumerate modalities directly.

This module is data + pure functions. No I/O, no side effects.
"""

from __future__ import annotations


# Modalities where the candidate is an LLM-backed agent that consumes a
# system-prompt-style instructions field. Adding a new modality? Update
# this set, not every gate that queries it.
_USER_INSTRUCTIONS_MODALITIES: frozenset[str] = frozenset(
    {
        "conversation",
        "voice_conversation",
        "voice_turn",
        "chat",
    }
)


def supports_user_instructions(input_type: str | None) -> bool:
    """True iff a TestCase with this input_type SHOULD populate
    `input_context.instructions` (the LLM-agent system prompt).

    Used by gate G-A3 (Agent 3 instructions-asymmetry validator) to decide
    whether populating ``input_context.instructions`` is correct (when
    True) or a violation (when False).

    OOD recovery: a future modality that legitimately needs instructions
    on a non-conversational TestCase: add the new modality to
    ``_USER_INSTRUCTIONS_MODALITIES`` here. The validator updates
    automatically. No code change needed in the validator itself.
    """
    if not input_type:
        return False
    return input_type in _USER_INSTRUCTIONS_MODALITIES


__all__ = [
    "supports_user_instructions",
]
