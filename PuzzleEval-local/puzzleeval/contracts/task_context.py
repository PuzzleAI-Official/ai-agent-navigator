"""TaskContext — the typed input to contract selection.

A frozen dataclass that carries everything the selector needs to decide
which contracts apply to a task. Strongly typed so contracts can declare
predicates (``SelectorSpec``) against KNOWN field names — no string-keyed
free-form dicts, no surprises.

Design principle: TaskContext is the IMMUTABLE description of a task.
Selection is a pure function of TaskContext + ContractRegistry. Same
inputs → same SelectionResult, byte-identical (Tier 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Mapping


@dataclass(frozen=True)
class TaskContext:
    """Frozen, hashable description of a task that needs contract selection.

    Fields:
        agent_id: Stable agent identifier (e.g., ``"agent_5"``,
                  ``"agent_4"``). Contracts declare ``applies_to_agents``
                  to opt into specific agents.
        phase: Pipeline phase (``"research"``, ``"build"``, ``"validate"``,
               ``"evaluate"``, ``"execute"``). Today's selection doesn't
               branch on phase, but the field is here for forward-compat
               with phase-conditional contracts.
        platform: ``sys.platform`` value (e.g., ``"win32"``, ``"darwin"``,
                  ``"linux"``). Drives platform-conditional contracts
                  (Windows-specific shell rules, etc.).
        test_cases: Tuple of test case objects. Used to derive
                    ``input_types`` and ``output_types`` for modality-conditional
                    contracts. Tuple (not list) so TaskContext stays hashable.
        candidate: Optional ScreenedCandidate-like object. Used for
                   candidate-metadata-conditional contracts (e.g., side_effects,
                   api_interaction_pattern_hint, auth_method). None for
                   pre-Agent-5 phases that don't have a candidate yet.
        run_id: Optional run UUID for logging correlation.
        trace_id: Optional trace UUID for logging correlation.

    Properties (computed lazily, cached):
        input_types: frozenset of ``input_type`` values across test_cases.
        output_types: frozenset of ``output_type`` values across test_cases.
        candidate_metadata: dict of candidate fields explicitly EXPOSED to
                           contract selection (allowlist — see _CANDIDATE_FIELDS).
                           Empty when candidate is None.

    Why allowlist for candidate_metadata: contracts can match against a
    candidate's auth_method, side_effects, etc. We don't want contracts to
    match against arbitrary candidate internals (refactor surface, naming
    drift). Adding a new matchable field requires explicit listing here.
    """

    agent_id: str
    phase: str
    platform: str
    test_cases: tuple[Any, ...] = ()
    candidate: Any | None = None
    run_id: str | None = None
    trace_id: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @cached_property
    def input_types(self) -> frozenset[str]:
        """Frozen set of ``input_type`` values across all test_cases.

        Reads either ``test_case.input_type`` (object attr) or
        ``test_case["input_type"]`` (dict key) — both shapes are common
        across the codebase. Skips test cases without an input_type.
        """
        out: set[str] = set()
        for tc in self.test_cases:
            value = _get_field(tc, "input_type")
            if value:
                out.add(str(value))
        return frozenset(out)

    @cached_property
    def output_types(self) -> frozenset[str]:
        """Frozen set of ``output_type`` values across all test_cases."""
        out: set[str] = set()
        for tc in self.test_cases:
            value = _get_field(tc, "output_type")
            if value:
                out.add(str(value))
        return frozenset(out)

    @cached_property
    def candidate_metadata(self) -> Mapping[str, Any]:
        """Dict of candidate fields explicitly EXPOSED to contract selection.

        ALLOWLIST. Contracts can only match against fields named here.
        Adding a new matchable candidate field requires editing this list
        AND the matching ContractMetadata.selectors.trigger_candidate_metadata
        validation in _loader.py.
        """
        if self.candidate is None:
            return {}
        result: dict[str, Any] = {}
        for field_name in _CANDIDATE_FIELDS_ALLOWLIST:
            value = _get_field(self.candidate, field_name)
            if value is not None:
                result[field_name] = value
        return result

    def describe(self) -> str:
        """Short human-readable description for telemetry / errors."""
        parts = [self.agent_id, self.phase, self.platform]
        if self.input_types:
            parts.append(f"input={sorted(self.input_types)}")
        if self.output_types:
            parts.append(f"output={sorted(self.output_types)}")
        if self.candidate is not None:
            cand_name = _get_field(self.candidate, "name") or "candidate"
            parts.append(str(cand_name))
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Allowlist of candidate fields exposed to contract selection.
# ---------------------------------------------------------------------------
# Adding a field here is a deliberate decision: contracts gain ability to
# match against this field via SelectorSpec.trigger_candidate_metadata.
# Don't add fields whose value is unstable (likely to change without
# coordination) — every contract that matches against the field becomes
# dependent on that stability.
_CANDIDATE_FIELDS_ALLOWLIST: frozenset[str] = frozenset({
    # Authentication / access
    "auth_method",
    "api_access_method",
    # Interaction model — drives streaming/async/websocket contracts
    "api_interaction_pattern_hint",
    # Side effects — drives safety contracts
    "side_effects",
    # Provider / vendor
    "provider",
    "upstream_provider",
})


def _get_field(obj: Any, field_name: str) -> Any:
    """Read a field from object-or-dict.

    Handles both attribute access (Pydantic / dataclass instances) and
    dict access (raw JSON dicts). Returns None when the field is missing.
    """
    if obj is None:
        return None
    if hasattr(obj, field_name):
        return getattr(obj, field_name)
    if isinstance(obj, dict):
        return obj.get(field_name)
    return None


__all__ = ["TaskContext"]
