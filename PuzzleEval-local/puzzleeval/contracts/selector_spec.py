"""SelectorSpec — typed predicate against TaskContext.

A contract's ``selectors`` field (in frontmatter) is parsed into one of
these. ``matches(spec, task)`` is a pure function — same inputs always
produce the same boolean.

Property: predicate evaluation is total and pure. No I/O, no side effects,
no exceptions on well-formed input. Every (TaskContext, SelectorSpec)
pair produces a deterministic bool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from puzzleeval.contracts.task_context import TaskContext


@dataclass(frozen=True)
class SelectorSpec:
    """Predicate against TaskContext fields.

    A contract is selected (in Tier 1 deterministic mode) when this
    spec's ``matches(task)`` returns True.

    Empty spec (no predicates) → never matches via deterministic mode.
    Such a contract must use ``selection_mode="always_on"`` or add a concrete
    predicate to be selected.

    Fields:
        trigger_types: frozenset of strings to match against
                       ``task.input_types | task.output_types``. The match
                       is "ANY trigger appears in the task's types".
        trigger_platforms: frozenset of platform strings (``"win32"``,
                           ``"darwin"``, ``"linux"``). The match is
                           ``task.platform in trigger_platforms``.
        trigger_candidate_metadata: dict mapping candidate-allowlist field
                                    name → required value. The match is
                                    ``task.candidate_metadata[k] == v`` for
                                    ALL listed (k, v) pairs.
        require_all: When True, ALL declared predicate dimensions must
                     match (AND). When False (default), ANY declared
                     predicate matching is sufficient (OR).
    """

    trigger_types: frozenset[str] = frozenset()
    trigger_platforms: frozenset[str] = frozenset()
    trigger_candidate_metadata: Mapping[str, str] = field(default_factory=dict)
    require_all: bool = False

    def has_any_predicate(self) -> bool:
        """True if at least one selection dimension is declared."""
        return bool(
            self.trigger_types
            or self.trigger_platforms
            or self.trigger_candidate_metadata
        )


def matches(spec: SelectorSpec, task: TaskContext) -> bool:
    """Evaluate ``spec`` against ``task``.

    Returns True if the task matches the spec under the spec's
    require_all/any policy. Returns False for an empty spec — empty
    selectors never match in deterministic mode.

    Pure function: deterministic, side-effect-free, total on well-formed
    inputs.
    """
    matches: list[bool] = []

    if spec.trigger_types:
        task_types = task.input_types | task.output_types
        matches.append(bool(spec.trigger_types & task_types))

    if spec.trigger_platforms:
        matches.append(task.platform in spec.trigger_platforms)

    if spec.trigger_candidate_metadata:
        candidate_meta = task.candidate_metadata
        # ALL listed (k, v) pairs must match — this is an AND within the
        # candidate-metadata predicate, regardless of require_all.
        # require_all controls how this dimension combines with the OTHERS,
        # not how predicates within this dimension combine.
        all_matched = all(
            candidate_meta.get(k) == v
            for k, v in spec.trigger_candidate_metadata.items()
        )
        matches.append(all_matched)

    if not matches:
        # No predicates declared — never selects via deterministic mode.
        return False

    if spec.require_all:
        return all(matches)
    return any(matches)


__all__ = ["SelectorSpec", "matches"]
