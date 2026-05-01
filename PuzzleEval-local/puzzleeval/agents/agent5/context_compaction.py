"""Context compaction at the Sonnet → Opus model transition.

Direct fix for the 749b09b1 narrative-inertia failure: Opus inheriting
Sonnet's exit narration ("Phase 1 complete — handing off to Phase 2
(Opus)") and looping 33 turns without realizing IT IS Opus.

A single ``PHASE2_DIRECTIVE`` user-message injection cannot reliably
override embedded narrative bias when the prior context is large
(observed: cache_read=88,763 tokens stuck identical across 33 turns).
The fix is to REPLACE the conversation history with a compact, canonical
state packet at the model-transition point. Opus then starts a fresh
conversation that points at on-disk artifacts (objective.md,
runtime_state.json, build_plan.md, api_spec.txt) for the prior context.

Cost: ~$0.10-0.30 per build (one Opus call without cache hit). Saved
per occurrence: ~$1.65 (the observed 33-turn waste). Net win even if
the compaction only helps 1 build in 30.

Scoped to the Sonnet → Opus transition ONLY. Other phase boundaries
keep their existing prompt-injection mechanism (compacting at every
boundary would be wasteful — narrative inertia is a model-transition
phenomenon, not a phase-boundary one).

Bypass: ``PUZZLEEVAL_CONTEXT_COMPACTION_AT_MODEL_TRANSITION=0`` falls
back to the legacy PHASE2_DIRECTIVE-only path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Marker the next API call site logs so we can audit when compaction
# fired. Kept here so callers don't recompose the string.
COMPACTION_EVENT_NAME = "context_compacted_at_model_transition"


def _agent_state_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / "_agent_state"


def _read_optional(path: Path, max_chars: int = 4000) -> str:
    """Read a file if it exists, truncating long content. Returns empty
    string when missing or unreadable."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"
    return text


def _extract_outstanding_success_criteria(objective_md: str) -> str:
    """Pull the SUCCESS CRITERIA section out of objective.md.

    Returns the raw section text. Doesn't try to be clever about
    "outstanding" vs "satisfied" — at the Phase 1 → Phase 2 transition
    NONE of the criteria are yet satisfied (the harness doesn't exist
    yet). Showing the full SUCCESS CRITERIA list orients Opus toward
    what the build is graded against.
    """
    if not objective_md:
        return ""
    # Find the section header
    marker = "## SUCCESS CRITERIA"
    idx = objective_md.find(marker)
    if idx < 0:
        return ""
    # Find the start of the next ## section (or end of file)
    end_marker = "\n## "
    rest = objective_md[idx + len(marker):]
    end_idx = rest.find(end_marker)
    section = rest if end_idx < 0 else rest[:end_idx]
    return section.strip()


def _extract_phase_2_todos(build_plan_md: str) -> str:
    """Pull the Phase 2 — Build section out of build_plan.md.

    Returns the raw section text or empty when build_plan.md is absent
    or doesn't yet have a Phase 2 section.
    """
    if not build_plan_md:
        return ""
    marker = "## Phase 2"
    idx = build_plan_md.find(marker)
    if idx < 0:
        return ""
    end_marker = "\n## "
    rest = build_plan_md[idx + len(marker):]
    end_idx = rest.find(end_marker)
    section = rest if end_idx < 0 else rest[:end_idx]
    return section.strip()


def compose_canonical_state_packet(sandbox_dir: Path) -> str:
    """Build the compacted user-message content for the model transition.

    Reads from the on-disk artifacts (orchestrator-owned) so the message
    reflects current truth, not stale conversation history.

    Pure function — reads files, returns string. No mutation.
    """
    objective_md = _read_optional(_agent_state_dir(sandbox_dir) / "objective.md")
    build_plan_md = _read_optional(_agent_state_dir(sandbox_dir) / "build_plan.md")
    runtime_state_path = _agent_state_dir(sandbox_dir) / "runtime_state.json"
    runtime_state_payload: dict[str, Any] = {}
    if runtime_state_path.exists():
        try:
            runtime_state_payload = json.loads(
                runtime_state_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            runtime_state_payload = {}

    success_criteria = _extract_outstanding_success_criteria(objective_md)
    phase_2_todos = _extract_phase_2_todos(build_plan_md)

    files_present = runtime_state_payload.get("files_present", [])
    files_pending = runtime_state_payload.get("files_pending", [])

    # The packet is intentionally direct + imperative. Opus's first
    # response after compaction sees this as the most recent user input
    # — overriding any narrative bias that would otherwise lead it to
    # emit "handing off to Opus" exit text.
    parts: list[str] = [
        "**You are now Phase 2 builder (Opus). You ARE the builder — do not narrate "
        "any handoff or transition. Write the scaffold files now.**",
        "",
        "## Where you stand",
        "",
        f"- Current phase: {runtime_state_payload.get('current_phase', 'phase_2_build')}",
        f"- Current turn: {runtime_state_payload.get('current_turn', '?')}",
        f"- Files already in your sandbox: {', '.join(files_present) if files_present else '(none beyond initial setup)'}",
        f"- Files still to write: {', '.join(files_pending) if files_pending else '(none)'}",
        "",
        "## Read these for full context",
        "",
        "Your prior research is on disk. Do NOT try to recall it from memory:",
        "",
        "- `read_file('api_spec.txt')` — the spec you (Sonnet) wrote in Phase 1.",
        "  This has the auth method, endpoints, request/response shapes.",
        "- `read_file('_agent_state/objective.md')` — what you must deliver.",
        "- `read_file('_agent_state/runtime_state.json')` — your authoritative state.",
        "- `read_file('_agent_state/build_plan.md')` — your Phase 2 todos.",
    ]

    if success_criteria:
        parts.extend([
            "",
            "## SUCCESS CRITERIA from objective.md (your build is graded against these)",
            "",
            success_criteria,
        ])

    if phase_2_todos:
        parts.extend([
            "",
            "## Phase 2 todos from build_plan.md",
            "",
            phase_2_todos,
        ])

    parts.extend([
        "",
        "## Next required action",
        "",
        "Your IMMEDIATE next response MUST call `write_file` (in parallel) for:",
        "",
        "- `requirements.txt`",
        "- `harness.py`",
        "- `smoke_test.py`",
        "- `live_test.py`",
        "",
        "Read api_spec.txt and _agent_state/build_plan.md first. If the plan "
        "needs a Phase 2 update, patch it before scaffold writes. Then issue "
        "the four parallel write_file calls. Do NOT acknowledge this message "
        "in narration. Do NOT describe a handoff. You ARE Opus. You ARE the "
        "builder. Just call the tools.",
    ])

    return "\n".join(parts)


def compact_for_model_transition(
    sandbox_dir: Path,
) -> list[dict[str, Any]]:
    """Return a fresh ``messages`` list with one canonical user message.

    Caller replaces ``state.messages`` with this return value at the
    Sonnet → Opus transition point. The system prompt is passed
    separately in ``client.messages.create(system=...)`` and is NOT
    affected by this compaction.

    The returned list always has exactly one entry — a user message
    containing the compacted state packet.
    """
    content = compose_canonical_state_packet(sandbox_dir)
    return [{"role": "user", "content": content}]


__all__ = [
    "COMPACTION_EVENT_NAME",
    "compose_canonical_state_packet",
    "compact_for_model_transition",
]
