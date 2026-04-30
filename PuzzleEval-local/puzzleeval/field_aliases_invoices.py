"""Domain-scoped curated field aliases for invoice-extraction evaluation.

Per Codex C8 in the Phase 2 plan: alias dictionaries can become hidden
scoring loopholes (vendor_name == supplier_name is fine; vendor_name ==
vendor_phone is NOT). Mitigations:

  1. **Domain-scoped.** This file is invoices-only. Voice transcripts,
     image generation, code completion, etc. each get their own
     `field_aliases_<domain>.py` when needed. NO global alias dict.
  2. **Mandatory false-positive regression tests.** Adding a new alias
     to `INVOICE_FIELD_ALIASES` requires extending
     `INVOICE_FALSE_POSITIVE_PAIRS` and the test in
     `tests/test_phase2_aliases.py` to assert at least 3 known false-
     positive pairs are NOT matched. The test enforces this discipline
     at CI time.
  3. **Conservative additions.** Each alias must be a true semantic
     equivalent in the invoice domain (the same business concept, just
     a different vendor's API field name). When in doubt, leave it out
     and let Claude's LLM judge handle it.

The data file is loaded by `puzzleeval/agents/agent5/evaluation.py`
(future programmatic alias matching) and referenced as authoritative
source-of-truth in the EVALUATION_SYSTEM_PROMPT. The prompt teaches
the principle ('semantic equivalents score as matches') and points
operators at this file for the actual list -- editing the alias list
becomes a code change with a test gate, not a prompt edit.
"""

from __future__ import annotations


# Canonical → equivalents. Each canonical name maps to a frozenset of
# names that are SEMANTICALLY EQUIVALENT in the invoice domain. The
# canonical name is included in its own equivalents so lookup works
# uniformly: the equivalents set always contains the canonical itself.
INVOICE_FIELD_ALIASES: dict[str, frozenset[str]] = {
    "vendor_name": frozenset(
        {"vendor_name", "seller_name", "supplier_name", "merchant_name", "issuer_name"}
    ),
    "total_amount": frozenset(
        {"total_amount", "invoice_amount", "grand_total", "amount_due", "total_due"}
    ),
    "tax_amount": frozenset(
        {"tax_amount", "tax_total", "vat", "sales_tax", "gst"}
    ),
    "invoice_date": frozenset(
        {"invoice_date", "issue_date", "billing_date", "date_issued"}
    ),
    "due_date": frozenset(
        {"due_date", "payment_due", "payment_due_date"}
    ),
    "invoice_number": frozenset(
        {"invoice_number", "invoice_id", "invoice_no", "doc_number"}
    ),
}


# Known FALSE-POSITIVE pairs that MUST NOT be matched. Adding an alias
# above without updating this list (and the regression test) is forbidden.
# Each entry is a (left, right) pair where the values are NOT equivalents
# despite sharing words or structure.
INVOICE_FALSE_POSITIVE_PAIRS: tuple[tuple[str, str], ...] = (
    # Different facts about the same vendor are not the same field.
    ("vendor_name", "vendor_phone"),
    ("vendor_name", "vendor_address"),
    # Different totals are different facts.
    ("total_amount", "tax_amount"),
    # Different date semantics.
    ("invoice_date", "due_date"),
    ("issue_date", "payment_due_date"),
    # Different numbers (number-of-something vs identifier).
    ("invoice_number", "invoice_amount"),
)


def lookup_canonical(field_name: str | None) -> str | None:
    """Return the canonical name for a field if it has one, else None.

    `vendor_name`, `seller_name`, `supplier_name` all return `'vendor_name'`.
    `vendor_phone` returns None (no canonical alias group for that field).

    Used by future programmatic evaluation paths to compare two field
    names for semantic equivalence: both having the same canonical
    means they're equivalents.
    """
    if not field_name:
        return None
    name = field_name.lower().strip()
    for canonical, equivalents in INVOICE_FIELD_ALIASES.items():
        if name in equivalents:
            return canonical
    return None


def are_equivalent(name_a: str | None, name_b: str | None) -> bool:
    """Return True iff `name_a` and `name_b` resolve to the same canonical.

    `vendor_name` and `seller_name` are equivalent; `vendor_name` and
    `vendor_phone` are NOT.

    Returns False if either name has no canonical (defensive: no
    canonical → not in the curated alias table → treat as distinct).
    """
    a = lookup_canonical(name_a)
    b = lookup_canonical(name_b)
    if a is None or b is None:
        return False
    return a == b


__all__ = [
    "INVOICE_FIELD_ALIASES",
    "INVOICE_FALSE_POSITIVE_PAIRS",
    "lookup_canonical",
    "are_equivalent",
]
