"""Orchestrator-derived build decisions for Agent 5.

This file is intentionally tiny: it records facts the orchestrator
observed, not agent-authored plans. The goal is to preserve the useful
part of "planning/state awareness" without forcing the model to maintain
write-only markdown artifacts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


BUILD_DECISIONS_FILENAME = "build_decisions.jsonl"


def _agent_state_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / "_agent_state"


def append_build_decision(
    sandbox_dir: Path,
    *,
    event: str,
    turn: int | None = None,
    candidate_name: str | None = None,
    **payload: Any,
) -> None:
    """Append one compact JSONL decision event.

    Best-effort by design: observability must never break a build.
    """

    try:
        state_dir = _agent_state_dir(sandbox_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "t_abs": time.time(),
            "event": event,
            **({"turn": turn} if turn is not None else {}),
            **({"candidate_name": candidate_name} if candidate_name else {}),
            **payload,
        }
        with (state_dir / BUILD_DECISIONS_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return


def read_recent_build_decisions(sandbox_dir: Path, limit: int = 8) -> list[dict[str, Any]]:
    """Read the most recent decision events."""

    path = _agent_state_dir(sandbox_dir) / BUILD_DECISIONS_FILENAME
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(0, limit):]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


__all__ = [
    "BUILD_DECISIONS_FILENAME",
    "append_build_decision",
    "read_recent_build_decisions",
]
