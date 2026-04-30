"""Phase 2C.3 tests — vision_judge tiered fallback cap (Codex C6).

The cap helper applies tier-based maximum scores when vision can't
verify image content; downstream consumers that fall back to a
text-only judge can apply the cap so 'URL present' isn't scored as
a full pass.

Tier table (per Codex C6):
  no_evidence:        0.3
  url_only:           0.5
  url_plus_structural: 0.7
  url_plus_secondary: 0.9
  None (no cap):      no change
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# apply_fallback_cap
# ---------------------------------------------------------------------------


class TestApplyFallbackCap:
    def test_none_cap_passes_score_through(self):
        from puzzleeval.vision_judge import apply_fallback_cap

        assert apply_fallback_cap(0.95, None) == 0.95
        assert apply_fallback_cap(0.0, None) == 0.0

    def test_no_evidence_caps_at_0_3(self):
        from puzzleeval.vision_judge import apply_fallback_cap

        assert apply_fallback_cap(0.9, "no_evidence") == 0.3
        assert apply_fallback_cap(1.0, "no_evidence") == 0.3
        # Already below cap → unchanged.
        assert apply_fallback_cap(0.1, "no_evidence") == 0.1

    def test_url_only_caps_at_0_5(self):
        from puzzleeval.vision_judge import apply_fallback_cap

        assert apply_fallback_cap(0.9, "url_only") == 0.5
        assert apply_fallback_cap(0.4, "url_only") == 0.4

    def test_url_plus_structural_caps_at_0_7(self):
        from puzzleeval.vision_judge import apply_fallback_cap

        assert apply_fallback_cap(0.95, "url_plus_structural") == 0.7
        assert apply_fallback_cap(0.6, "url_plus_structural") == 0.6

    def test_url_plus_secondary_caps_at_0_9(self):
        from puzzleeval.vision_judge import apply_fallback_cap

        assert apply_fallback_cap(1.0, "url_plus_secondary") == 0.9
        assert apply_fallback_cap(0.85, "url_plus_secondary") == 0.85

    def test_unknown_cap_reason_passes_through(self):
        """Defensive: unknown labels don't accidentally cap legitimate
        scores. If a cap_reason is malformed, prefer no cap over wrong
        cap."""
        from puzzleeval.vision_judge import apply_fallback_cap

        assert apply_fallback_cap(0.95, "future_unknown_tier") == 0.95
        assert apply_fallback_cap(0.95, "") == 0.95


# ---------------------------------------------------------------------------
# VisionVerdict fallback fields
# ---------------------------------------------------------------------------


class TestVisionVerdictFallbackFields:
    def test_default_fields_no_fallback(self):
        from puzzleeval.vision_judge import VisionVerdict

        v = VisionVerdict(
            passed=True, score=0.9, reasoning="ok",
            criterion_scores={"c1": 0.9},
        )
        assert v.vision_fallback_used is False
        assert v.cap_reason is None
        assert v.fallback_reason is None

    def test_set_fallback_fields_explicitly(self):
        from puzzleeval.vision_judge import VisionVerdict

        v = VisionVerdict(
            passed=False, score=0.0, reasoning="",
            criterion_scores={},
            fallback_reason="download_failed",
            vision_fallback_used=True,
            cap_reason="url_only",
        )
        assert v.vision_fallback_used is True
        assert v.cap_reason == "url_only"
        assert v.fallback_reason == "download_failed"

    def test_apply_cap_to_verdict_score(self):
        """Caller-flow integration: a downstream evaluator that falls
        back to a text-only score after vision returned a fallback can
        cap the new score using the verdict's cap_reason."""
        from puzzleeval.vision_judge import VisionVerdict, apply_fallback_cap

        vision = VisionVerdict(
            passed=False, score=0.0, reasoning="",
            criterion_scores={},
            fallback_reason="download_failed",
            vision_fallback_used=True,
            cap_reason="url_only",
        )
        # Text fallback judges 0.95 (URL looks plausible, response shape ok).
        text_score = 0.95
        capped = apply_fallback_cap(text_score, vision.cap_reason)
        assert capped == 0.5, (
            "URL-only cap should bound the text-fallback score at 0.5 "
            "even when the text judge would have given a higher score."
        )
