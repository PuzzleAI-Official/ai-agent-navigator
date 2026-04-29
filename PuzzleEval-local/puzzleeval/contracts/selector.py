"""Multi-layer contract selection — the core algorithm.

This is the answer to the user's design question: how do we ensure the
right contracts are loaded for any given task, with no missed contracts
and no unneeded contracts?

The mechanism: 4 deterministic layers, each with a falsifiable property.

  Layer 1: Always-On      — structurally required (platform, safety)
  Layer 2: Deterministic  — typed predicate match against TaskContext
  Layer 3: Coverage       — assert all required contracts are selected
  Layer 4: Conflict       — drop mutually-exclusive lower-priority

Properties guaranteed:
  * Soundness: every selected contract has a true predicate or is always-on.
  * Completeness (narrowed): for tasks matching at least one
    CoverageRequirement, Layer 3 reports gaps. Plus an enum-classification
    test in tests/test_coverage_completeness.py guards against the
    "developer added a new modality but forgot to add a CoverageRequirement"
    failure mode.
  * Consistency: Layer 4 prevents conflicting contracts from co-existing.
  * Determinism: same TaskContext → byte-identical SelectionResult.

LLM-ROUTED SELECTION (Tier 2) was REMOVED in Round 5 (Codex pushback —
speculative architecture without a consumer). When/if a real use case
emerges (e.g., 30+ contracts with overlapping triggers where deterministic
match is genuinely ambiguous), add it back as a separate layer driven
by per-contract `selection_mode="llm_routed"` declarations rather than
a count threshold. ALLOWED_SELECTION_MODES rejects 'llm_routed' today
so authors can't accidentally ship contracts pointing at vapor.

Property tests live in ``tests/test_contract_selection_properties.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from puzzleeval.contracts.coverage import (
    ContractCoverageReport,
    validate_coverage,
)
from puzzleeval.contracts.errors import ContractCoverageError
from puzzleeval.contracts.loader import Contract, ContractRegistry
from puzzleeval.contracts.selector_spec import SelectorSpec, matches as spec_matches
from puzzleeval.contracts.task_context import TaskContext


# ---------------------------------------------------------------------------
# SelectionResult — structured output of select_contracts_for_task.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Conflict:
    """One conflict detected by the conflict-resolution layer."""
    a: str
    b: str


@dataclass(frozen=True)
class SelectionResult:
    """Output of contract selection.

    Fields:
        contracts: tuple of selected Contract objects, sorted by descending
                   priority (highest priority first → injected earliest in
                   prompt rendering).
        coverage: ContractCoverageReport from the coverage layer.
        conflicts_resolved: list of Conflict entries the conflict layer resolved.
        layer_telemetry: per-layer attribution — which contract_ids each
                         layer contributed. Useful for debugging "why was
                         this contract loaded?".
        task_context: copy of the input TaskContext for back-reference.
    """

    contracts: tuple[Contract, ...]
    coverage: ContractCoverageReport
    conflicts_resolved: tuple[Conflict, ...]
    layer_telemetry: dict[str, list[str]]
    task_context: TaskContext

    @property
    def contract_ids(self) -> frozenset[str]:
        return frozenset(c.metadata.id for c in self.contracts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def select_contracts_for_task(
    task: TaskContext,
    *,
    strict: bool = False,
    registry: ContractRegistry | None = None,
) -> SelectionResult:
    """Select the contracts to inject for ``task``.

    100% deterministic: same TaskContext + same registry → same result.

    Args:
        task: TaskContext describing what the agent is doing.
        strict: When True, coverage gaps raise ContractCoverageError.
                When False (default), gaps are reported in the result but
                the caller decides what to do.
        registry: Override the cached registry (for tests). None → use cached.

    Returns:
        SelectionResult with contracts (sorted by priority), coverage report,
        conflicts resolved, and per-layer telemetry.

    Raises:
        ContractCoverageError when strict=True and coverage gaps exist.
    """
    if registry is None:
        registry = ContractRegistry.cached()

    layer_telemetry: dict[str, list[str]] = {
        "layer_1_always_on": [],
        "layer_2_deterministic": [],
    }

    # Layer 1: Always-On
    layer_1 = _select_layer_1_always_on(registry, task)
    layer_telemetry["layer_1_always_on"] = [c.metadata.id for c in layer_1]

    # Layer 2: Deterministic
    layer_2 = _select_layer_2_deterministic(registry, task)
    layer_telemetry["layer_2_deterministic"] = [c.metadata.id for c in layer_2]

    # Dedupe + sort by priority (descending → high-priority first)
    selected = _dedupe_preserving_first(layer_1 + layer_2)
    selected.sort(key=lambda c: -c.metadata.priority)

    # Layer 3: Coverage validation
    selected_ids = frozenset(c.metadata.id for c in selected)
    coverage = validate_coverage(task, selected_ids)
    if coverage.has_gaps and strict:
        raise ContractCoverageError(
            task_description=task.describe(),
            missing=sorted(coverage.missing),
            selected=sorted(selected_ids),
        )

    # Layer 4: Conflict detection + resolution
    selected, conflicts = _resolve_conflicts(selected, registry.conflict_graph())

    return SelectionResult(
        contracts=tuple(selected),
        coverage=coverage,
        conflicts_resolved=tuple(conflicts),
        layer_telemetry=layer_telemetry,
        task_context=task,
    )


def compose_contract_block(
    task: TaskContext,
    *,
    strict: bool = False,
    separator: str = "\n\n",
    registry: ContractRegistry | None = None,
) -> str:
    """Convenience: select + compose into a single string for prompt injection.

    Equivalent to:
      result = select_contracts_for_task(task, ...)
      return separator.join(c.body for c in result.contracts)

    Returns empty string when no contracts match — the caller's prompt
    template should still render cleanly with an empty contract block.
    """
    result = select_contracts_for_task(task, strict=strict, registry=registry)
    if not result.contracts:
        return ""
    return separator.join(c.body for c in result.contracts)


# ---------------------------------------------------------------------------
# Layer implementations
# ---------------------------------------------------------------------------

def _select_layer_1_always_on(
    registry: ContractRegistry, task: TaskContext,
) -> list[Contract]:
    """Layer 1 — load always-on contracts that match the agent + platform."""
    out: list[Contract] = []
    for contract in registry.always_on(agent_id=task.agent_id):
        # Always-on contracts may still have a platform predicate
        # (e.g., platform_windows.md is always_on but only for Windows).
        platforms = contract.metadata.selectors.trigger_platforms
        if platforms and task.platform not in platforms:
            continue
        out.append(contract)
    return out


def _select_layer_2_deterministic(
    registry: ContractRegistry, task: TaskContext,
) -> list[Contract]:
    """Layer 2 — deterministic schema-driven match against SelectorSpec."""
    out: list[Contract] = []
    for contract in registry.deterministic(agent_id=task.agent_id):
        spec = SelectorSpec(
            trigger_types=frozenset(contract.metadata.selectors.trigger_types),
            trigger_platforms=frozenset(contract.metadata.selectors.trigger_platforms),
            trigger_candidate_metadata=dict(contract.metadata.selectors.trigger_candidate_metadata),
            require_all=contract.metadata.selectors.require_all,
        )
        if spec_matches(spec, task):
            out.append(contract)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dedupe_preserving_first(contracts: list[Contract]) -> list[Contract]:
    """Remove duplicates while preserving order of first occurrence."""
    seen: set[str] = set()
    out: list[Contract] = []
    for contract in contracts:
        cid = contract.metadata.id
        if cid in seen:
            continue
        seen.add(cid)
        out.append(contract)
    return out


def _resolve_conflicts(
    selected: list[Contract],
    conflict_graph: dict[str, frozenset[str]],
) -> tuple[list[Contract], list[Conflict]]:
    """Drop lower-priority contracts when mutually exclusive.

    Returns (resolved_selected, conflicts_recorded).
    """
    selected_ids = {c.metadata.id for c in selected}
    by_id = {c.metadata.id: c for c in selected}

    conflicts_found: list[Conflict] = []
    drop: set[str] = set()

    seen_pairs: set[frozenset[str]] = set()
    for cid in list(selected_ids):
        for excluded in conflict_graph.get(cid, frozenset()):
            if excluded not in selected_ids:
                continue
            pair = frozenset({cid, excluded})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            a = by_id[cid]
            b = by_id[excluded]
            # Drop the lower-priority one. Tie-breaker: alphabetical id.
            if a.metadata.priority < b.metadata.priority:
                loser = a.metadata.id
            elif a.metadata.priority > b.metadata.priority:
                loser = b.metadata.id
            else:
                loser = max(a.metadata.id, b.metadata.id)
            drop.add(loser)
            # Record the conflict in stable (a, b) order
            ordered = sorted({cid, excluded})
            conflicts_found.append(Conflict(a=ordered[0], b=ordered[1]))

    if not drop:
        return (selected, conflicts_found)

    return (
        [c for c in selected if c.metadata.id not in drop],
        conflicts_found,
    )


__all__ = [
    "Conflict",
    "SelectionResult",
    "compose_contract_block",
    "select_contracts_for_task",
]
