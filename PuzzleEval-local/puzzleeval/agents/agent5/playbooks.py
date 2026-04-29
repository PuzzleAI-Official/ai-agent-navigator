"""Deterministic capability playbook loading for Agent 5.

These are local PuzzleEval playbooks, not native Anthropic Skills. The
router is deterministic: test-case modality selects the playbooks before
the builder model sees the prompt, while safety-critical enforcement stays
in Python code.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Mapping


VOICE_MODALITIES = frozenset({
    "conversation",
    "voice_conversation",
    "voice_turn",
    "audio_content",
})

STREAMING_RESPONSE_SHAPES = frozenset({
    "voice_conversation",
    "voice_turn",
    "audio_content",
    "conversation",
    "code",
})


@dataclass(frozen=True)
class CapabilityPlaybook:
    """A local prompt playbook plus its deterministic routing predicate."""

    id: str
    filename: str
    trigger_types: frozenset[str]

    def applies_to(self, test_cases: list[Any] | tuple[Any, ...] | None) -> bool:
        return _any_test_case_type_in(test_cases, self.trigger_types)


PLAYBOOK_ORDER: tuple[CapabilityPlaybook, ...] = (
    CapabilityPlaybook(
        id="voice",
        filename="voice.md",
        trigger_types=VOICE_MODALITIES,
    ),
    CapabilityPlaybook(
        id="streaming_response",
        filename="streaming_response.md",
        trigger_types=STREAMING_RESPONSE_SHAPES,
    ),
    CapabilityPlaybook(
        id="live_test_voice",
        filename="live_test_voice.md",
        trigger_types=STREAMING_RESPONSE_SHAPES,
    ),
)


def selected_playbook_ids(test_cases: list[Any] | tuple[Any, ...] | None) -> list[str]:
    """Return playbook ids selected for these test cases, in injection order."""

    if not test_cases:
        return []
    return [playbook.id for playbook in PLAYBOOK_ORDER if playbook.applies_to(test_cases)]


def compose_capability_playbooks(
    test_cases: list[Any] | tuple[Any, ...] | None,
    *,
    fallbacks: Mapping[str, str] | None = None,
) -> str:
    """Load and join the selected playbooks for ``test_cases``.

    ``fallbacks`` lets the legacy compatibility module preserve behavior if a
    packaged markdown file is missing during local development.
    """

    if not test_cases:
        return ""

    fallback_map = fallbacks or {}
    parts: list[str] = []
    for playbook in PLAYBOOK_ORDER:
        if not playbook.applies_to(test_cases):
            continue
        text = load_playbook_text(
            playbook.id,
            fallback=fallback_map.get(playbook.id, ""),
        )
        if text:
            parts.append(text)
    return "\n\n".join(parts)


@lru_cache(maxsize=None)
def _load_packaged_playbook_text(playbook_id: str) -> str:
    """Read a playbook from package data by id.

    As of the Phase 0 contract system migration, every capability playbook
    file carries a YAML frontmatter block. We strip it here so callers
    receive the body only — Claude must never see the metadata.
    """

    playbook = _playbook_by_id(playbook_id)
    raw_text = (
        resources.files("puzzleeval")
        .joinpath("capability_playbooks", playbook.filename)
        .read_text(encoding="utf-8")
    )
    # Strip YAML frontmatter via the canonical contracts loader so behavior
    # stays in lockstep with the new selector.
    from puzzleeval.contracts.loader import parse_frontmatter

    _, body = parse_frontmatter(raw_text, source=playbook.filename)
    return body if body else raw_text


def load_playbook_text(playbook_id: str, *, fallback: str = "") -> str:
    """Read a cached playbook from package data by id."""

    try:
        return _load_packaged_playbook_text(playbook_id)
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        return fallback


def _playbook_by_id(playbook_id: str) -> CapabilityPlaybook:
    for playbook in PLAYBOOK_ORDER:
        if playbook.id == playbook_id:
            return playbook
    raise KeyError(f"Unknown Agent 5 capability playbook: {playbook_id}")


def _any_test_case_type_in(
    test_cases: list[Any] | tuple[Any, ...] | None,
    type_names: frozenset[str],
) -> bool:
    if not test_cases:
        return False
    for tc in test_cases:
        input_type = (
            getattr(tc, "input_type", None)
            or (tc.get("input_type") if isinstance(tc, dict) else None)
        )
        output_type = (
            getattr(tc, "output_type", None)
            or (tc.get("output_type") if isinstance(tc, dict) else None)
        )
        if input_type in type_names or output_type in type_names:
            return True
    return False
