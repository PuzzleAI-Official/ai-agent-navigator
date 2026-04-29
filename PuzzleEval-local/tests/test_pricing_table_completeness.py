"""CI guard — verify every model name referenced in the codebase has pricing.

The plan's L9 hardening calls this out explicitly: a `claude-*` model
name that appears in the codebase but has no entry in MODEL_PRICING
silently zeros out cost calculations for that model. This test catches
the gap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from puzzleeval.telemetry.pricing_tables import MODEL_PRICING


# Source files we scan for model references.
# Excludes test files (which may use fake model names) and docs.
_SCAN_DIRS = ("puzzleeval/",)
_SCAN_EXTENSIONS = (".py",)
_EXCLUDE_PATTERNS = (
    "_test_",  # any module with _test_ in name
    "/tests/",
)

# Match strings that look like Claude model names.
# Pattern: claude-{family}-{version}[-{date}] — covers opus-4-7, sonnet-4-6,
# haiku-4-5-20251001, etc.
_MODEL_PATTERN = re.compile(r'["\']?(claude-(?:opus|sonnet|haiku)-[0-9-]+)["\']?')


def _scan_codebase_for_models() -> set[str]:
    """Return the set of all claude-* model names appearing in source files."""
    repo_root = Path(__file__).resolve().parent.parent
    found: set[str] = set()

    for scan_dir_rel in _SCAN_DIRS:
        scan_dir = repo_root / scan_dir_rel
        if not scan_dir.exists():
            continue
        for path in scan_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in _SCAN_EXTENSIONS:
                continue
            relpath = str(path.relative_to(repo_root)).replace("\\", "/")
            if any(excl in relpath for excl in _EXCLUDE_PATTERNS):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in _MODEL_PATTERN.finditer(text):
                model = match.group(1)
                # Skip names that are too short to be real models
                # (e.g., the regex over-matches `claude-opus-4` without version)
                if model.count("-") < 3:
                    continue
                # Strip trailing hyphens / digits ambiguity
                model = model.rstrip("-")
                # Skip the regex pattern itself
                if "[" in model or "]" in model:
                    continue
                found.add(model)
    return found


class TestPricingTableCompleteness:
    def test_every_referenced_model_has_pricing_entry(self):
        """Every claude-* model name in source must have a MODEL_PRICING entry."""
        referenced = _scan_codebase_for_models()
        # Skip the abstract model name "claude-haiku-4-5" (without date suffix)
        # which appears in CLAUDE.md but isn't a concrete API model name
        # (the API requires the dated form claude-haiku-4-5-20251001).
        # MODEL_PRICING uses the dated form.
        ABSTRACT_NAMES = {"claude-haiku-4-5"}
        referenced -= ABSTRACT_NAMES

        missing = referenced - set(MODEL_PRICING.keys())
        assert not missing, (
            f"Models referenced in code but missing from MODEL_PRICING: "
            f"{sorted(missing)}\n"
            "Add entries to puzzleeval/telemetry/pricing_tables.py:MODEL_PRICING."
        )

    def test_pricing_has_known_models(self):
        """Sanity: the pricing table includes models we definitely use."""
        required_models = {
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        }
        for model in required_models:
            assert model in MODEL_PRICING, (
                f"{model} is core to the pipeline but missing from pricing table"
            )

    def test_all_pricing_tuples_are_positive(self):
        """No model should have zero or negative pricing (data corruption check)."""
        for model, (inp, out) in MODEL_PRICING.items():
            assert inp > 0, f"{model} has zero input pricing"
            assert out > 0, f"{model} has zero output pricing"
            assert inp < 1, f"{model} input pricing too high (likely $/M not $/token)"
            assert out < 1, f"{model} output pricing too high (likely $/M not $/token)"

    def test_output_more_expensive_than_input(self):
        """Anthropic output is always more expensive than input. Sanity check."""
        for model, (inp, out) in MODEL_PRICING.items():
            assert out > inp, (
                f"{model} output ({out}) cheaper than input ({inp}) — "
                "almost certainly a bug in the pricing table."
            )
