"""Runtime primitive selection for Agent 5 harness execution.

Phase 5 keeps runtime primitives binary:

* ``single_call``: subprocess per harness invocation.
* ``persistent_worker``: one JSON-over-stdin/stdout worker per conversation.

Streaming, duplex, serialized conversation, and provider session semantics live
in ``implementation_plan.json`` under ``interaction_pattern``. This module only
decides process lifetime.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUNTIME_SINGLE_CALL = "single_call"
RUNTIME_PERSISTENT_WORKER = "persistent_worker"

_LEGACY_PERSISTENT_VALUES = {"multi_turn_persistent", RUNTIME_PERSISTENT_WORKER}
_LEGACY_SINGLE_CALL_VALUES = {
    "",
    "multi_turn_serialized",
    RUNTIME_SINGLE_CALL,
}


@dataclass(frozen=True)
class RuntimePrimitiveDecision:
    mode: str
    state_owner: str = ""
    known_family: str = ""
    source: str = "default"
    degraded: bool = False
    reason: str = ""


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _read_implementation_plan(sandbox_dir: Path | None) -> dict[str, Any] | None:
    if sandbox_dir is None:
        return None
    plan_path = sandbox_dir / "_agent_state" / "implementation_plan.json"
    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _mode_from_capabilities(caps: Any | None) -> str:
    raw = _normalize(getattr(caps, "harness_execution_mode", RUNTIME_SINGLE_CALL))
    if raw in _LEGACY_PERSISTENT_VALUES:
        return RUNTIME_PERSISTENT_WORKER
    if raw in _LEGACY_SINGLE_CALL_VALUES:
        return RUNTIME_SINGLE_CALL
    return RUNTIME_SINGLE_CALL


def select_runtime_primitive(
    *,
    sandbox_dir: Path | None = None,
    caps: Any | None = None,
    persistent_worker_enabled: bool = True,
) -> RuntimePrimitiveDecision:
    """Select the binary runtime primitive for a plugin-driven conversation."""

    plan = _read_implementation_plan(sandbox_dir)
    interaction = plan.get("interaction_pattern") if isinstance(plan, dict) else None
    interaction = interaction if isinstance(interaction, dict) else {}
    state_owner = _normalize(interaction.get("state_owner"))
    known_family = _normalize(interaction.get("known_family"))

    if state_owner == "harness_process":
        if persistent_worker_enabled:
            return RuntimePrimitiveDecision(
                mode=RUNTIME_PERSISTENT_WORKER,
                state_owner=state_owner,
                known_family=known_family,
                source="implementation_plan",
                reason="harness_process state requires a persistent worker",
            )
        return RuntimePrimitiveDecision(
            mode=RUNTIME_SINGLE_CALL,
            state_owner=state_owner,
            known_family=known_family,
            source="implementation_plan",
            degraded=True,
            reason=(
                "PUZZLEEVAL_PERSISTENT_WORKER_RUNTIME_ENABLED=0; "
                "harness_process state degrades to single_call"
            ),
        )

    if state_owner == "provider_server":
        return RuntimePrimitiveDecision(
            mode=RUNTIME_SINGLE_CALL,
            state_owner=state_owner,
            known_family=known_family,
            source="implementation_plan",
            reason="provider_server owns state; single_call is sufficient",
        )

    capability_mode = _mode_from_capabilities(caps)
    if capability_mode == RUNTIME_PERSISTENT_WORKER and not persistent_worker_enabled:
        return RuntimePrimitiveDecision(
            mode=RUNTIME_SINGLE_CALL,
            source="plugin_capability",
            degraded=True,
            reason=(
                "PUZZLEEVAL_PERSISTENT_WORKER_RUNTIME_ENABLED=0; "
                "plugin persistent-worker request degrades to single_call"
            ),
        )
    if capability_mode == RUNTIME_PERSISTENT_WORKER:
        return RuntimePrimitiveDecision(
            mode=RUNTIME_PERSISTENT_WORKER,
            source="plugin_capability",
            reason="compatibility fallback: plugin requests persistent_worker",
        )
    return RuntimePrimitiveDecision(
        mode=RUNTIME_SINGLE_CALL,
        source="default",
        reason="no harness-owned state declared",
    )


__all__ = [
    "RUNTIME_PERSISTENT_WORKER",
    "RUNTIME_SINGLE_CALL",
    "RuntimePrimitiveDecision",
    "select_runtime_primitive",
]
