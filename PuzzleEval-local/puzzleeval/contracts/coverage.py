"""Coverage requirements — falsifiable contract for "right contracts loaded".

This module is the answer to the architectural question: how do we know
the right contracts are loaded for any given task, and how do we know
none were missed?

The answer: every (task signature pattern) declares what contracts it
REQUIRES. After Layer 2's deterministic match runs, Layer 4 walks the
requirements and asserts every required contract appears in the selected
set. Strict mode raises ``ContractCoverageError``; default mode logs a
``contract_coverage_gap`` telemetry event.

Operators can grep telemetry for any production gap. The CI guard
``tests/test_coverage_completeness.py`` asserts every documented modality
enum value has at least one CoverageRequirement entry — preventing the
"forgot to declare coverage" failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from puzzleeval.contracts.task_context import TaskContext


@dataclass(frozen=True)
class TaskSignature:
    """Pattern that identifies a CLASS of tasks needing the same contracts.

    A signature is a SET of conditions; a TaskContext matches a signature
    if it satisfies ALL of the signature's conditions. (None values mean
    "don't care" for that dimension.)

    Fields:
        agent_id: Match this exact agent_id. None = any.
        input_type_in: Task's input_types must intersect this set. None = any.
        output_type_in: Task's output_types must intersect this set. None = any.
        platform_in: Task's platform must be in this set. None = any.
        candidate_side_effects_in: Candidate's side_effects must be in this
                                    set. None = any.
    """

    agent_id: str | None = None
    input_type_in: frozenset[str] | None = None
    output_type_in: frozenset[str] | None = None
    platform_in: frozenset[str] | None = None
    candidate_side_effects_in: frozenset[str] | None = None

    def matches(self, task: TaskContext) -> bool:
        """True if this task satisfies all of the signature's conditions."""
        if self.agent_id is not None and task.agent_id != self.agent_id:
            return False
        if self.input_type_in is not None:
            if not (self.input_type_in & task.input_types):
                return False
        if self.output_type_in is not None:
            if not (self.output_type_in & task.output_types):
                return False
        if self.platform_in is not None:
            if task.platform not in self.platform_in:
                return False
        if self.candidate_side_effects_in is not None:
            cand_se = task.candidate_metadata.get("side_effects")
            if cand_se not in self.candidate_side_effects_in:
                return False
        return True


@dataclass(frozen=True)
class CoverageRequirement:
    """For tasks matching this signature, these contracts MUST be selected.

    Fields:
        signature: TaskSignature — the pattern of tasks this requirement applies to.
        required_contract_ids: frozenset of contract IDs that must appear
                               in SelectionResult.contracts.
        description: Human-readable rationale shown in coverage-gap errors.
    """

    signature: TaskSignature
    required_contract_ids: frozenset[str]
    description: str


# ---------------------------------------------------------------------------
# The canonical requirements table.
# ---------------------------------------------------------------------------
# Adding a new modality / interaction / safety contract? Add a
# CoverageRequirement here that captures when the contract is required.
# Without an entry here, Layer 4 can't catch missing-required-contract bugs.
#
# Each entry pairs a TaskSignature (when does this requirement apply?)
# with a set of required contract IDs (what must be selected?).
#
# The CI guard `tests/test_coverage_completeness.py` (Phase 0) asserts:
#   - Every contract declared in the registry has at least one
#     CoverageRequirement that requires it (no orphan contracts).
#   - Every modality enum value mentioned in puzzleeval/validators.py
#     appears in at least one CoverageRequirement (no orphan modalities).
# ---------------------------------------------------------------------------
COVERAGE_REQUIREMENTS: tuple[CoverageRequirement, ...] = (
    # Voice modality on Agent 5 needs the full voice contract trio.
    CoverageRequirement(
        signature=TaskSignature(
            agent_id="agent_5",
            input_type_in=frozenset({"voice_conversation", "voice_turn", "audio_content"}),
        ),
        required_contract_ids=frozenset({"voice", "streaming_response", "live_test_voice"}),
        description=(
            "Voice tests need return-shape contract (voice.md), streaming "
            "response collection (streaming_response.md), and live test "
            "session contract (live_test_voice.md)"
        ),
    ),
    # Voice modality where output is voice — same trio.
    CoverageRequirement(
        signature=TaskSignature(
            agent_id="agent_5",
            output_type_in=frozenset({"voice_conversation", "voice_turn", "audio_content"}),
        ),
        required_contract_ids=frozenset({"voice", "streaming_response", "live_test_voice"}),
        description=(
            "Voice OUTPUT tests need the same trio as voice input tests — "
            "harness must produce audio in the supported shapes."
        ),
    ),
    # Conversation modality (text) needs streaming_response since it's
    # multi-turn but doesn't need voice-specific contracts.
    CoverageRequirement(
        signature=TaskSignature(
            agent_id="agent_5",
            input_type_in=frozenset({"conversation"}),
        ),
        required_contract_ids=frozenset({"streaming_response"}),
        description=(
            "Multi-turn text conversation tests need streaming response "
            "collection contract for response collection patterns."
        ),
    ),
    # Code modality — streaming_response covers the streaming code-gen pattern.
    CoverageRequirement(
        signature=TaskSignature(
            agent_id="agent_5",
            input_type_in=frozenset({"code"}),
            output_type_in=frozenset({"code"}),
        ),
        required_contract_ids=frozenset({"streaming_response"}),
        description=(
            "Code-generation tests need streaming response collection "
            "contract (modern code-gen APIs stream tokens)."
        ),
    ),
    # Windows builds always need the Windows platform contract.
    CoverageRequirement(
        signature=TaskSignature(
            agent_id="agent_5",
            platform_in=frozenset({"win32"}),
        ),
        required_contract_ids=frozenset({"platform_windows"}),
        description=(
            "Windows builds need OS-specific command translation guidance "
            "(Unix→Windows shell command differences)."
        ),
    ),
)


@dataclass(frozen=True)
class ContractCoverageReport:
    """Result of Layer 4 coverage validation.

    Fields:
        applicable_requirements: which requirements matched the task.
        missing: contract IDs that were required but not selected.
        selected: contract IDs that WERE selected (for debug context).
        has_gaps: True iff `missing` is non-empty.
    """

    applicable_requirements: tuple[CoverageRequirement, ...]
    missing: frozenset[str]
    selected: frozenset[str]

    @property
    def has_gaps(self) -> bool:
        return bool(self.missing)


def applicable_requirements(task: TaskContext) -> tuple[CoverageRequirement, ...]:
    """Return all CoverageRequirement entries that match this task."""
    return tuple(req for req in COVERAGE_REQUIREMENTS if req.signature.matches(task))


def validate_coverage(task: TaskContext, selected_contract_ids: frozenset[str]) -> ContractCoverageReport:
    """Check that all applicable requirements are satisfied.

    Args:
        task: The TaskContext being evaluated.
        selected_contract_ids: Set of contract IDs that the selector picked.

    Returns:
        ContractCoverageReport. Caller decides how to handle gaps (raise vs warn).
    """
    applicable = applicable_requirements(task)
    required: set[str] = set()
    for req in applicable:
        required.update(req.required_contract_ids)
    missing = frozenset(required - selected_contract_ids)
    return ContractCoverageReport(
        applicable_requirements=applicable,
        missing=missing,
        selected=selected_contract_ids,
    )


__all__ = [
    "COVERAGE_REQUIREMENTS",
    "ContractCoverageReport",
    "CoverageRequirement",
    "TaskSignature",
    "applicable_requirements",
    "validate_coverage",
]
