"""Coverage completeness — every documented modality has a CoverageRequirement.

Codex pushback B: the previous claim "Layer 4 reports any missing required
contract" was overstated — Layer 4 only catches gaps where a CoverageRequirement
EXISTS for that task pattern. If a developer adds a new modality enum value
without adding a CoverageRequirement, no gap is reported, and the contract
system silently degrades for that modality.

This test enforces the implicit invariant: every input/output type enum value
in `puzzleeval.validators.VALID_INPUT_TYPES` and `VALID_OUTPUT_TYPES` must
either:

  1. Appear in at least one CoverageRequirement signature, OR
  2. Be explicitly marked in NO_COVERAGE_REQUIRED below as "this type
     intentionally has no required contracts."

Adding a new modality enum WITHOUT updating this test fails CI loudly, NOT
silently — converting "human remembers" into a test gate.

The narrowed correctness claim becomes:
  "For every (agent_id, modality) pair documented in CoverageRequirement
  signatures OR explicitly allowlisted as no-coverage, Layer 4 reports any
  missing required contract."
"""

from __future__ import annotations

import pytest

from puzzleeval.contracts.coverage import COVERAGE_REQUIREMENTS
from puzzleeval.validators import VALID_INPUT_TYPES, VALID_OUTPUT_TYPES


# ---------------------------------------------------------------------------
# Allowlist: modalities that intentionally have NO required contracts.
# ---------------------------------------------------------------------------
# Adding to this list is a deliberate decision: it asserts "tasks of this
# modality work correctly with no modality-specific contract beyond the
# always-on platform contracts." Document the rationale in the comment.
# ---------------------------------------------------------------------------

NO_COVERAGE_REQUIRED_INPUT_TYPES: frozenset[str] = frozenset({
    # OCR / document processing — currently passes through to plugin-side
    # evaluation without modality-specific harness teaching beyond the
    # base BUILDER_SYSTEM_PROMPT. If OCR ever needs a contract (e.g.,
    # multi-page batching guidance), add a CoverageRequirement.
    "text",
    "structured_data",
    "document_content",
    "image_description",
    "file_reference",
    # webhook_event needs no contract today because the webhook_receiver
    # plugin handles all the protocol shape internally — the harness sees
    # a normalized payload via the plugin's evaluate_output path.
    "webhook_event",
})

NO_COVERAGE_REQUIRED_OUTPUT_TYPES: frozenset[str] = frozenset({
    # OCR / general output types — see input rationale above.
    "free_text",
    "structured_json",
    "classification",
    "extraction",
    "action",
    "media_url",
    # NOTE: 'audio_content' is NOT here — it appears in the voice OUTPUT
    # CoverageRequirement (see coverage.py). Voice harness output tests
    # require the voice + streaming + live_test contracts.
    # Outbound delivery handled entirely by the outbound_delivery plugin's
    # mock receivers — no harness-side teaching needed.
    "webhook_callback",
    "outbound_message",
})


def _all_input_types_in_coverage_requirements() -> set[str]:
    """Collect every input_type referenced in any CoverageRequirement."""
    out: set[str] = set()
    for req in COVERAGE_REQUIREMENTS:
        if req.signature.input_type_in:
            out.update(req.signature.input_type_in)
    return out


def _all_output_types_in_coverage_requirements() -> set[str]:
    """Collect every output_type referenced in any CoverageRequirement."""
    out: set[str] = set()
    for req in COVERAGE_REQUIREMENTS:
        if req.signature.output_type_in:
            out.update(req.signature.output_type_in)
    return out


class TestEnumToCoverageCompleteness:
    """Every documented modality must be classified as either:
    (a) requiring at least one contract via CoverageRequirement, OR
    (b) explicitly allowlisted in NO_COVERAGE_REQUIRED_* above.

    Adding a new enum value without doing either fails CI — the test
    converts the "human remembers to add a CoverageRequirement" failure
    mode from silent to loud.
    """

    def test_every_input_type_classified(self):
        in_requirements = _all_input_types_in_coverage_requirements()
        unclassified = (
            VALID_INPUT_TYPES
            - in_requirements
            - NO_COVERAGE_REQUIRED_INPUT_TYPES
        )
        assert not unclassified, (
            f"New input_type enum value(s) {sorted(unclassified)} are "
            "not classified — either:\n"
            "  - Add a CoverageRequirement that lists them in "
            "input_type_in, OR\n"
            "  - Add them to NO_COVERAGE_REQUIRED_INPUT_TYPES with a "
            "comment explaining why no contract is needed.\n"
            "Without one of these, contract selection silently degrades "
            "for tests of this modality."
        )

    def test_every_output_type_classified(self):
        in_requirements = _all_output_types_in_coverage_requirements()
        unclassified = (
            VALID_OUTPUT_TYPES
            - in_requirements
            - NO_COVERAGE_REQUIRED_OUTPUT_TYPES
        )
        assert not unclassified, (
            f"New output_type enum value(s) {sorted(unclassified)} are "
            "not classified — either:\n"
            "  - Add a CoverageRequirement that lists them in "
            "output_type_in, OR\n"
            "  - Add them to NO_COVERAGE_REQUIRED_OUTPUT_TYPES with a "
            "comment explaining why no contract is needed.\n"
            "Without one of these, contract selection silently degrades "
            "for tests of this modality."
        )

    def test_allowlist_does_not_overlap_requirements(self):
        """A type can't be BOTH in a CoverageRequirement AND allowlisted as
        no-coverage. That's contradictory — pick one."""
        in_requirements_input = _all_input_types_in_coverage_requirements()
        overlap_input = NO_COVERAGE_REQUIRED_INPUT_TYPES & in_requirements_input
        assert not overlap_input, (
            f"Input types {sorted(overlap_input)} appear in BOTH a "
            "CoverageRequirement AND NO_COVERAGE_REQUIRED_INPUT_TYPES. "
            "These declarations are contradictory."
        )

        in_requirements_output = _all_output_types_in_coverage_requirements()
        overlap_output = NO_COVERAGE_REQUIRED_OUTPUT_TYPES & in_requirements_output
        assert not overlap_output, (
            f"Output types {sorted(overlap_output)} appear in BOTH a "
            "CoverageRequirement AND NO_COVERAGE_REQUIRED_OUTPUT_TYPES."
        )


class TestCriticalityInvariant:
    """Pushback A: required contracts cannot use selection_mode=llm_routed.

    Any contract whose criticality is 'required' must select via
    deterministic Tier 1 (always_on or deterministic mode). The LLM router
    can only ADD optional contracts.
    """

    def test_no_required_contract_is_llm_routed(self):
        from puzzleeval.contracts.loader import discover_contracts
        for contract in discover_contracts():
            if contract.metadata.criticality == "required":
                assert contract.metadata.selection_mode != "llm_routed", (
                    f"Contract {contract.metadata.id!r} is criticality="
                    "required but selection_mode=llm_routed — that "
                    "violates the Tier 1/Tier 2 separation. Required "
                    "contracts must be deterministic; only optional "
                    "contracts can be llm_routed."
                )

    def test_pydantic_validator_rejects_llm_routed_today(self):
        """After Round 5, selection_mode='llm_routed' is REJECTED at parse time.

        Tier 2 (LLM-routed selection) was removed as speculative architecture
        without a consumer. The reserved value can't be used until the framework
        is reintroduced with a real use case.

        This test pins the contract: authors trying to ship llm_routed today
        get a clear validation error, not a silent runtime fall-through.
        """
        from pydantic import ValidationError
        from puzzleeval.contracts.metadata import ContractMetadata
        with pytest.raises(ValidationError) as exc:
            ContractMetadata(
                id="bogus_llm_routed",
                description="x",
                selection_mode="llm_routed",
                criticality="optional",  # even optional doesn't allow llm_routed today
                selectors={"trigger_types": ["voice_conversation"]},
            )
        assert "llm_routed" in str(exc.value).lower() or "selection_mode" in str(exc.value).lower()

    def test_only_two_selection_modes_today(self):
        """ALLOWED_SELECTION_MODES is restricted to always_on + deterministic
        after Round 5 Tier-2 removal."""
        from puzzleeval.contracts.metadata import ALLOWED_SELECTION_MODES
        assert ALLOWED_SELECTION_MODES == frozenset({"always_on", "deterministic"}), (
            "ALLOWED_SELECTION_MODES should ONLY contain always_on + "
            "deterministic. If you've added 'llm_routed' back, also add "
            "Layer 3 implementation, restore tier1/tier2 mode parameter "
            "on select_contracts_for_task, and update the property tests."
        )


class TestNarrowedCorrectnessClaim:
    """Verify the narrowed correctness statement holds.

    Original claim (overstated): "Layer 4 reports any missing required contract."
    Narrowed claim: "For tasks matching at least one CoverageRequirement,
    Layer 4 reports missing required contracts."

    These tests verify the narrowed claim is the actual behavior.
    """

    def test_task_with_no_matching_coverage_requirement_yields_no_gaps(self):
        """When a task pattern doesn't match any CoverageRequirement, the
        coverage report reports 0 gaps — because there are 0 requirements
        to violate. This is the CORRECT behavior for the narrowed claim:
        the system can't report a gap it doesn't know to look for.

        The remediation for this case is the enum-to-coverage test above
        (test_every_input_type_classified) which fails CI when a new enum
        value lacks classification.
        """
        from puzzleeval.contracts import TaskContext
        from puzzleeval.contracts.coverage import (
            applicable_requirements, validate_coverage,
        )

        # Concoct a task that matches no CoverageRequirement (random unused
        # combination) — pretend there's an enum value 'fictional_modality'.
        task = TaskContext(
            agent_id="agent_5",
            phase="build",
            platform="freebsd",  # no platform contract
            test_cases=(),  # no input/output types
        )
        applicable = applicable_requirements(task)
        assert applicable == (), (
            "No CoverageRequirement should match a task with no test_cases "
            "and unknown platform"
        )
        report = validate_coverage(task, frozenset())
        assert not report.has_gaps, (
            "Empty coverage requirements + empty selection = no gaps. "
            "This is correct behavior: gaps can only be reported relative "
            "to a CoverageRequirement that exists."
        )
