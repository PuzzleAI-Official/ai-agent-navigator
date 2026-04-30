"""Regression tests for the curated field-alias dictionaries.

Per Codex C8 in the Phase 2 plan: alias dictionaries can become hidden
scoring loopholes. Adding a new alias requires the regression test to
assert at least 3 known false-positive pairs are NOT matched. This test
file enforces that discipline at CI time.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Invoice field aliases
# ---------------------------------------------------------------------------


class TestInvoiceFieldAliases:
    def test_canonical_resolution(self):
        from puzzleeval.field_aliases_invoices import lookup_canonical

        assert lookup_canonical("vendor_name") == "vendor_name"
        assert lookup_canonical("seller_name") == "vendor_name"
        assert lookup_canonical("supplier_name") == "vendor_name"
        assert lookup_canonical("merchant_name") == "vendor_name"

        assert lookup_canonical("total_amount") == "total_amount"
        assert lookup_canonical("invoice_amount") == "total_amount"
        assert lookup_canonical("grand_total") == "total_amount"

    def test_case_insensitive(self):
        from puzzleeval.field_aliases_invoices import lookup_canonical

        assert lookup_canonical("VENDOR_NAME") == "vendor_name"
        assert lookup_canonical("Seller_Name") == "vendor_name"
        assert lookup_canonical("  supplier_name  ") == "vendor_name"

    def test_unknown_field_returns_none(self):
        from puzzleeval.field_aliases_invoices import lookup_canonical

        assert lookup_canonical("foo_bar_baz") is None
        assert lookup_canonical(None) is None
        assert lookup_canonical("") is None

    def test_equivalents_within_canonical_group(self):
        from puzzleeval.field_aliases_invoices import are_equivalent

        # Same canonical → equivalent.
        assert are_equivalent("vendor_name", "seller_name") is True
        assert are_equivalent("supplier_name", "merchant_name") is True
        assert are_equivalent("total_amount", "grand_total") is True

    def test_known_false_positive_pairs_not_matched(self):
        """Per Codex C8 -- the loophole-prevention discipline.

        Each pair in INVOICE_FALSE_POSITIVE_PAIRS MUST resolve to
        DIFFERENT canonicals (or one of them MUST resolve to None).
        Adding an alias above without keeping this test green is
        forbidden -- the test is the gate.
        """
        from puzzleeval.field_aliases_invoices import (
            INVOICE_FALSE_POSITIVE_PAIRS,
            are_equivalent,
        )

        # Mandatory: at least 3 pairs (per the discipline).
        assert len(INVOICE_FALSE_POSITIVE_PAIRS) >= 3, (
            "Adding aliases requires extending INVOICE_FALSE_POSITIVE_PAIRS "
            "with the false-positive cases that are NOT supposed to match."
        )

        for left, right in INVOICE_FALSE_POSITIVE_PAIRS:
            assert not are_equivalent(left, right), (
                f"FALSE POSITIVE: {left!r} should NOT be equivalent to {right!r} "
                f"per the curated alias discipline. Either remove the alias that "
                f"created this false equivalence, or remove the false-positive "
                f"pair if it's no longer applicable."
            )

    def test_canonical_includes_itself(self):
        """Every canonical name must appear in its own equivalents set."""
        from puzzleeval.field_aliases_invoices import INVOICE_FIELD_ALIASES

        for canonical, equivalents in INVOICE_FIELD_ALIASES.items():
            assert canonical in equivalents, (
                f"Canonical {canonical!r} must be in its own equivalents set "
                f"so lookup_canonical({canonical!r}) returns {canonical!r}."
            )

    def test_no_overlap_between_canonical_groups(self):
        """Each name appears in at most one canonical group.

        Overlap would create ambiguity: lookup_canonical(name) would
        depend on dict iteration order. The test pins the discipline.
        """
        from puzzleeval.field_aliases_invoices import INVOICE_FIELD_ALIASES

        all_names: list[str] = []
        for equivalents in INVOICE_FIELD_ALIASES.values():
            all_names.extend(equivalents)
        duplicates = {n for n in all_names if all_names.count(n) > 1}
        assert duplicates == set(), (
            f"Field names appearing in multiple canonical groups: {duplicates}. "
            f"Each name belongs to exactly one canonical group."
        )


# ---------------------------------------------------------------------------
# `relevant_subtasks` deprecation helper
# ---------------------------------------------------------------------------


class TestRelevantSubtasksDeprecationHelper:
    def _make_candidate(self, **overrides):
        from puzzleeval.schemas import Candidate

        defaults = dict(
            name="X",
            provider="X",
            description="d",
            api_available=True,
            api_docs_url=None,
            pricing_model="free-tier",
            pricing_details=None,
            claimed_capabilities=["c"],
            relevance_score=0.5,
            adoption_difficulty="easy",
            relevant_subtasks=[],
            source="https://example.com",
            covers_step_ids=[],
        )
        defaults.update(overrides)
        return Candidate(**defaults)

    def test_falls_back_to_covers_when_relevant_subtasks_empty(self):
        from puzzleeval.schemas import read_subtask_or_scope_refs

        c = self._make_candidate(
            relevant_subtasks=[], covers_step_ids=["step_1", "step_2"]
        )
        assert read_subtask_or_scope_refs(c) == ["step_1", "step_2"]

    def test_uses_relevant_subtasks_when_populated(self):
        from puzzleeval.schemas import read_subtask_or_scope_refs

        c = self._make_candidate(
            relevant_subtasks=["old description"], covers_step_ids=["step_1"]
        )
        # Legacy semantics: relevant_subtasks wins when populated.
        assert read_subtask_or_scope_refs(c) == ["old description"]

    def test_returns_empty_when_both_empty(self):
        from puzzleeval.schemas import read_subtask_or_scope_refs

        c = self._make_candidate(relevant_subtasks=[], covers_step_ids=[])
        assert read_subtask_or_scope_refs(c) == []

    def test_telemetry_emits_only_once_per_process(self, caplog):
        """The `deprecated_field_read` log line fires AT MOST once per
        process so production logs aren't spammed when the field is
        read N times across thousands of candidates."""
        import logging

        # Reset the once-per-process gate so we can observe the log.
        from puzzleeval.schemas import _DEPRECATED_FIELD_LOG_ONCE, read_subtask_or_scope_refs

        _DEPRECATED_FIELD_LOG_ONCE.discard("relevant_subtasks")

        c = self._make_candidate(relevant_subtasks=["x"])

        with caplog.at_level(logging.INFO, logger="puzzleeval.schemas"):
            read_subtask_or_scope_refs(c)
            read_subtask_or_scope_refs(c)
            read_subtask_or_scope_refs(c)

        records = [
            r
            for r in caplog.records
            if getattr(r, "operation", "") == "deprecated_field_read"
        ]
        assert len(records) == 1, (
            f"Expected exactly 1 deprecated_field_read log (once-per-process), "
            f"got {len(records)}."
        )
        rec = records[0]
        assert getattr(rec, "field_name", None) == "relevant_subtasks"
        assert getattr(rec, "candidate_name", None) == "X"
