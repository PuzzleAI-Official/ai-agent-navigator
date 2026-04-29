# ============================================================================
# Tests for puzzleeval/pricing.py
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_pricing.py -v
#
# Pure function tests — no mocking needed. Every test constructs a
# `PricingBreakdown` fixture in-line and asserts the expected output.
# Covers: estimate_monthly_cost, per_scope_unit_cost,
# monthly_cost_for_scope_map, cheapest_meaningful_tier, format_*.
# ============================================================================

import pytest

from puzzleeval.pricing import (
    cheapest_meaningful_tier,
    estimate_monthly_cost,
    format_breakdown_short,
    format_tier_short,
    monthly_cost_for_scope_map,
    per_scope_unit_cost,
)
from puzzleeval.schemas import PricingBreakdown, PricingTier


# ---------------------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------------------

def _flat_tier(name="Pro", cost=29.0, included=None, unit=None, overage=None):
    return PricingTier(
        name=name,
        monthly_cost_usd=cost,
        included_units=included,
        unit_name=unit,
        overage_cost_per_unit_usd=overage,
    )


def _freemium_breakdown():
    """Classic OCR freemium: 250 free pages, then $0.02/page."""
    return PricingBreakdown(
        tiers=[
            _flat_tier(name="Free", cost=0.0, included=250, unit="pages", overage=0.02),
            _flat_tier(name="Starter", cost=29.0, included=2500, unit="pages", overage=0.015),
            _flat_tier(name="Pro", cost=99.0, included=10000, unit="pages", overage=0.01),
        ],
        free_tier_monthly_units=250,
        pay_as_you_go=False,
        billing_granularity="monthly",
        sources=["https://example.com/pricing"],
        confidence="high",
    )


def _payg_breakdown():
    """Pure pay-as-you-go: $0 base, $0.01 per call."""
    return PricingBreakdown(
        tiers=[
            _flat_tier(name="PAYG", cost=0.0, included=None, unit="calls", overage=0.01),
        ],
        pay_as_you_go=True,
        billing_granularity="per_call",
        sources=["https://example.com/pricing"],
        confidence="high",
    )


def _flat_rate_breakdown():
    """Single flat-rate enterprise tier, no metering."""
    return PricingBreakdown(
        tiers=[
            _flat_tier(name="Flat", cost=499.0, included=None, unit=None, overage=None),
        ],
        pay_as_you_go=False,
        billing_granularity="monthly",
        sources=["https://example.com/pricing"],
        confidence="medium",
    )


def _capped_no_overage_breakdown():
    """Starter caps at 1000 calls with NO overage (hard cap); Pro has no cap."""
    return PricingBreakdown(
        tiers=[
            _flat_tier(name="Starter", cost=19.0, included=1000, unit="calls", overage=None),
            _flat_tier(name="Pro", cost=99.0, included=10000, unit="calls", overage=0.005),
        ],
        pay_as_you_go=False,
        billing_granularity="monthly",
        sources=["https://example.com/pricing"],
        confidence="high",
    )


def _per_scope_breakdown():
    """Zapier-style: OCR scope $0.10/page, sync scope $0.005/event."""
    return PricingBreakdown(
        tiers=[
            _flat_tier(name="Starter", cost=29.0, included=None, unit="events", overage=0.005),
        ],
        pay_as_you_go=True,
        billing_granularity="monthly",
        per_scope_unit_cost={"step_1": 0.10, "step_2": 0.005},
        sources=["https://zapier.com/pricing"],
        confidence="medium",
    )


# ---------------------------------------------------------------------------
# estimate_monthly_cost
# ---------------------------------------------------------------------------


class TestEstimateMonthlyCost:

    def test_freemium_within_free_tier(self):
        bd = _freemium_breakdown()
        # 100 pages is under the 250-page free allowance → $0.
        assert estimate_monthly_cost(bd, monthly_volume=100) == 0.0

    def test_freemium_at_free_tier_boundary(self):
        bd = _freemium_breakdown()
        assert estimate_monthly_cost(bd, monthly_volume=250) == 0.0

    def test_freemium_over_free_tier_uses_overage_on_free(self):
        # 251 pages → cheapest option is Free + 1 page overage @ $0.02 = $0.02
        # (vs Starter base $29). So picks Free tier.
        bd = _freemium_breakdown()
        assert estimate_monthly_cost(bd, monthly_volume=251) == pytest.approx(0.02)

    def test_freemium_switch_to_starter_when_cheaper(self):
        # At very high overage volume on Free, Starter becomes cheaper.
        # 2000 pages:
        #   Free:    0 + 1750 * 0.02 = $35.00
        #   Starter: 29 + 0 = $29.00  (1750 within 2500 included)
        #   Pro:     99 = $99.00
        # Cheapest = Starter = $29.00
        bd = _freemium_breakdown()
        assert estimate_monthly_cost(bd, monthly_volume=2000) == 29.0

    def test_freemium_very_high_volume_uses_pro(self):
        # 50000 pages:
        #   Free:    0 + 49750 * 0.02 = $995.00
        #   Starter: 29 + 47500 * 0.015 = $29 + $712.50 = $741.50
        #   Pro:     99 + 40000 * 0.01 = $99 + $400 = $499
        # Cheapest = Pro
        bd = _freemium_breakdown()
        assert estimate_monthly_cost(bd, monthly_volume=50000) == pytest.approx(499.0)

    def test_payg_scales_linearly(self):
        bd = _payg_breakdown()
        assert estimate_monthly_cost(bd, monthly_volume=0) == 0.0
        assert estimate_monthly_cost(bd, monthly_volume=1000) == pytest.approx(10.0)
        assert estimate_monthly_cost(bd, monthly_volume=100_000) == pytest.approx(1000.0)

    def test_flat_rate_constant_at_any_volume(self):
        bd = _flat_rate_breakdown()
        for vol in (0, 100, 10_000, 1_000_000):
            assert estimate_monthly_cost(bd, monthly_volume=vol) == 499.0

    def test_capped_tier_skipped_when_volume_exceeds_cap(self):
        # 5000 calls exceeds Starter's 1000 cap (no overage) → must use Pro.
        # Pro: 99 + 0 * 0.005 = $99 (5000 within 10000 included).
        bd = _capped_no_overage_breakdown()
        assert estimate_monthly_cost(bd, monthly_volume=5000) == 99.0

    def test_capped_tier_chosen_when_volume_fits(self):
        # 500 calls fits Starter's cap → $19.
        bd = _capped_no_overage_breakdown()
        assert estimate_monthly_cost(bd, monthly_volume=500) == 19.0

    def test_volume_beyond_all_tiers_returns_best_effort_ceiling(self):
        # A breakdown with ONLY capped tiers that can't serve a huge volume.
        bd = PricingBreakdown(
            tiers=[
                _flat_tier(name="Small", cost=10.0, included=100, overage=None),
                _flat_tier(name="Medium", cost=50.0, included=1000, overage=None),
            ],
            sources=["https://example.com/pricing"],
            confidence="low",
        )
        # 1M volume → no tier can serve → returns highest base ($50).
        result = estimate_monthly_cost(bd, monthly_volume=1_000_000)
        assert result == 50.0

    def test_empty_tiers_returns_zero(self):
        # Defensive — Phase 6.5 shouldn't emit this, but helper handles it.
        bd = PricingBreakdown(
            tiers=[],
            sources=["https://example.com/pricing"],
            confidence="low",
        )
        assert estimate_monthly_cost(bd, monthly_volume=100) == 0.0

    def test_negative_volume_raises(self):
        bd = _freemium_breakdown()
        with pytest.raises(ValueError, match=">= 0"):
            estimate_monthly_cost(bd, monthly_volume=-1)


# ---------------------------------------------------------------------------
# per_scope_unit_cost
# ---------------------------------------------------------------------------


class TestPerScopeUnitCost:

    def test_explicit_per_scope_override_wins(self):
        bd = _per_scope_breakdown()
        assert per_scope_unit_cost(bd, "step_1") == 0.10
        assert per_scope_unit_cost(bd, "step_2") == 0.005

    def test_falls_back_to_cheapest_tier_overage(self):
        # Freemium: cheapest overage is Free tier's $0.02/page.
        bd = _freemium_breakdown()
        # No per_scope override → falls back to first tier's overage.
        assert per_scope_unit_cost(bd, "step_1") == 0.02

    def test_returns_none_for_flat_rate(self):
        # No overage on any tier → None (flat-rate, can't compute per-unit).
        bd = _flat_rate_breakdown()
        assert per_scope_unit_cost(bd, "step_1") is None

    def test_per_scope_override_wins_even_over_tier_overage(self):
        # If both are set, per_scope_unit_cost takes precedence.
        bd = _freemium_breakdown()
        bd.per_scope_unit_cost = {"step_1": 0.005}  # override cheaper than $0.02
        assert per_scope_unit_cost(bd, "step_1") == 0.005
        # Scope not in override → back to tier-based fallback.
        assert per_scope_unit_cost(bd, "step_2") == 0.02


# ---------------------------------------------------------------------------
# monthly_cost_for_scope_map
# ---------------------------------------------------------------------------


class TestMonthlyCostForScopeMap:

    def test_per_scope_map_sums_correctly(self):
        # 500 step_1 * $0.10 + 10000 step_2 * $0.005 = $50 + $50 = $100
        # Plus base $29 = $129.
        bd = _per_scope_breakdown()
        result = monthly_cost_for_scope_map(bd, {"step_1": 500, "step_2": 10000})
        assert result == pytest.approx(129.0)

    def test_empty_scope_map_is_zero(self):
        bd = _per_scope_breakdown()
        assert monthly_cost_for_scope_map(bd, {}) == 0.0

    def test_flat_rate_ignores_scope_volumes(self):
        # Flat rate tool → base cost only, regardless of scope volumes.
        bd = _flat_rate_breakdown()
        result = monthly_cost_for_scope_map(bd, {"step_1": 1000, "step_2": 99999})
        assert result == 499.0  # flat rate, no per-scope add


# ---------------------------------------------------------------------------
# cheapest_meaningful_tier
# ---------------------------------------------------------------------------


class TestCheapestMeaningfulTier:

    def test_skips_zero_cost_no_overage_tier(self):
        # Hypothetical: "Free forever" tier with no overage (unlimited free)
        # alongside a paid tier. cheapest_meaningful returns the PAID tier.
        bd = PricingBreakdown(
            tiers=[
                _flat_tier(name="Free", cost=0.0, included=None, overage=None),
                _flat_tier(name="Pro", cost=29.0, included=10000, overage=0.01),
            ],
            sources=["https://example.com/pricing"],
            confidence="high",
        )
        tier = cheapest_meaningful_tier(bd)
        assert tier is not None
        assert tier.name == "Pro"

    def test_includes_free_with_overage(self):
        # Free tier WITH overage → "meaningful" (user hitting overage pays).
        bd = _freemium_breakdown()
        tier = cheapest_meaningful_tier(bd)
        assert tier.name == "Free"

    def test_all_zero_no_overage_returns_first(self):
        bd = PricingBreakdown(
            tiers=[
                _flat_tier(name="Free1", cost=0.0, overage=None),
                _flat_tier(name="Free2", cost=0.0, overage=None),
            ],
            sources=["https://example.com/pricing"],
            confidence="low",
        )
        tier = cheapest_meaningful_tier(bd)
        assert tier.name == "Free1"

    def test_empty_returns_none(self):
        bd = PricingBreakdown(
            tiers=[], sources=["url"], confidence="low",
        )
        assert cheapest_meaningful_tier(bd) is None


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class TestFormatters:

    def test_format_tier_short_all_fields(self):
        tier = _flat_tier(name="Pro", cost=29, included=5000, unit="calls", overage=0.005)
        s = format_tier_short(tier)
        assert "$29/mo" in s
        assert "5,000 calls included" in s
        assert "$0.005/call overage" in s

    def test_format_tier_short_flat_rate(self):
        tier = _flat_tier(name="Flat", cost=499, included=None, unit=None, overage=None)
        s = format_tier_short(tier)
        assert s == "$499/mo"

    def test_format_tier_short_payg(self):
        tier = _flat_tier(name="PAYG", cost=0.0, included=None, unit="calls", overage=0.01)
        s = format_tier_short(tier)
        assert "$0/mo" in s
        assert "$0.01/call overage" in s

    def test_format_breakdown_short_paid_tier(self):
        bd = _freemium_breakdown()
        s = format_breakdown_short(bd)
        # Free tier with overage IS meaningful → "From $0/mo"
        assert "From $0/mo" == s

    def test_format_breakdown_short_payg(self):
        bd = _payg_breakdown()
        s = format_breakdown_short(bd)
        assert "pay-as-you-go" in s
        assert "$0.01/call" in s

    def test_format_breakdown_short_empty(self):
        bd = PricingBreakdown(tiers=[], sources=["url"], confidence="low")
        assert format_breakdown_short(bd) == "Pricing unknown"


# ---------------------------------------------------------------------------
# Schema defaults / round-trip (Phase 5a)
# ---------------------------------------------------------------------------


class TestSchemaDefaults:
    """Verify PricingBreakdown / PricingTier serialize cleanly."""

    def test_breakdown_round_trip(self):
        bd = _freemium_breakdown()
        j = bd.model_dump_json()
        back = PricingBreakdown.model_validate_json(j)
        assert len(back.tiers) == 3
        assert back.tiers[0].name == "Free"
        assert back.free_tier_monthly_units == 250
        assert back.confidence == "high"

    def test_candidate_pricing_breakdown_defaults_to_none(self):
        from puzzleeval.schemas import Candidate

        c = Candidate(
            name="X", provider="X", description="d",
            api_available=True, api_docs_url=None,
            pricing_model="usage-based", pricing_details=None,
            claimed_capabilities=["c"], relevance_score=0.5,
            adoption_difficulty="easy", relevant_subtasks=[], source="t",
        )
        assert c.pricing_breakdown is None

    def test_candidate_pricing_breakdown_accepts_value(self):
        from puzzleeval.schemas import Candidate

        c = Candidate(
            name="X", provider="X", description="d",
            api_available=True, api_docs_url=None,
            pricing_model="usage-based", pricing_details=None,
            claimed_capabilities=["c"], relevance_score=0.5,
            adoption_difficulty="easy", relevant_subtasks=[], source="t",
            pricing_breakdown=_freemium_breakdown(),
        )
        assert c.pricing_breakdown is not None
        assert len(c.pricing_breakdown.tiers) == 3
