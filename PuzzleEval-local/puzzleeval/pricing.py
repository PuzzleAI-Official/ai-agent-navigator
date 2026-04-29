# ============================================================================
# Pricing Helpers — Phase 5 cost calculators
# ============================================================================
# Pure functions that read a `PricingBreakdown` and compute useful numbers
# for the selection UI and final results:
#
#   estimate_monthly_cost(breakdown, monthly_volume) -> float
#       The "how much will this actually cost me per month at X volume"
#       question. Picks the cheapest tier that fits `monthly_volume`,
#       adds overage if applicable. Works for flat-rate, freemium, and
#       pay-as-you-go shapes.
#
#   per_scope_unit_cost(breakdown, scope_id) -> float | None
#       The per-scope unit cost used by the CoverageMatrix UI when
#       pricing varies across scopes the candidate covers. When
#       `per_scope_unit_cost[scope_id]` is set, return it. Otherwise
#       fall back to the cheapest tier's `overage_cost_per_unit_usd`.
#       Returns None when no per-unit rate exists (flat-rate only).
#
# NO API CALLS. NO I/O. Deterministic functions on structured data —
# easy to test, easy to embed in scoring loops.
#
# Used by:
#   - Phase 7 selection (pricing_fit dimension of the per-scope scoring fn)
#   - Phase 9 results table (per-candidate monthly cost estimate)
#   - Phase 4 CoverageMatrix frontend (per-scope cost overlay)
# ============================================================================

from __future__ import annotations

from puzzleeval.schemas import PricingBreakdown, PricingTier


def estimate_monthly_cost(
    breakdown: PricingBreakdown,
    monthly_volume: int,
) -> float:
    """
    Estimate monthly cost at a given usage volume (units/month).

    Algorithm:
      1. If breakdown has no tiers → return 0.0 (should never happen; Phase
         6.5's 4B extraction enforces >=1 tier).
      2. For each tier (iterated cheapest-first), compute the effective
         cost at `monthly_volume`:
             included_units >= monthly_volume → pay only the base cost
             else                             → base + (vol - included) * overage
         Tiers without `overage_cost_per_unit_usd` and with
         `included_units < monthly_volume` are skipped (can't serve this
         volume — next tier up).
      3. Return the MINIMUM effective cost across all tiers that can serve
         the volume. This lets the calculator account for "Starter covers
         1K calls, Pro covers 10K calls" cases where the cheapest tier
         that fits depends on volume.
      4. If NO tier can serve the volume (every capped tier has
         included_units < volume AND no overage), return the base cost of
         the most expensive tier as a best-effort ceiling. Emitters should
         surface this as "contact sales" in the UI.

    All costs in USD. Volume must be >= 0.
    """
    if not breakdown.tiers:
        return 0.0
    if monthly_volume < 0:
        raise ValueError(f"monthly_volume must be >= 0, got {monthly_volume}")

    candidates: list[float] = []
    for tier in breakdown.tiers:
        cost = _tier_cost_at(tier, monthly_volume)
        if cost is not None:
            candidates.append(cost)

    if candidates:
        return min(candidates)

    # Fallback — every tier either caps below volume with no overage, or
    # the breakdown has malformed tiers. Return the highest base cost as
    # an upper-bound best-effort.
    return max(tier.monthly_cost_usd for tier in breakdown.tiers)


def _tier_cost_at(tier: PricingTier, volume: int) -> float | None:
    """
    Cost of serving `volume` units under a single tier. Returns None when
    the tier cannot serve this volume (capped without overage).
    """
    base = tier.monthly_cost_usd
    included = tier.included_units
    overage = tier.overage_cost_per_unit_usd

    # Unlimited or unmetered tier → base covers everything.
    if included is None and overage is None:
        return base

    # Pure pay-as-you-go (no included allowance but has overage rate):
    # every unit from zero costs overage.
    if included is None and overage is not None:
        return base + volume * overage

    # Freemium or tier with included allowance:
    if included is not None:
        if volume <= included:
            return base
        if overage is not None:
            return base + (volume - included) * overage
        # Capped tier with no overage — can't serve this volume.
        return None

    return base


def per_scope_unit_cost(
    breakdown: PricingBreakdown,
    scope_id: str,
) -> float | None:
    """
    Per-unit cost for a specific scope.

    Lookup order:
      1. breakdown.per_scope_unit_cost[scope_id] if set — takes precedence.
         This is the explicit "OCR charges per page, sync charges per event"
         case.
      2. Otherwise fall back to the cheapest tier's
         `overage_cost_per_unit_usd`. Most providers have uniform per-unit
         pricing and this "cheapest overage" is what a light user pays per
         unit.
      3. None when no per-unit rate is knowable (flat-rate only with no
         metering and no per_scope override).
    """
    # Precedence 1: explicit per-scope override.
    if scope_id in breakdown.per_scope_unit_cost:
        return breakdown.per_scope_unit_cost[scope_id]

    # Precedence 2: cheapest tier's overage (tiers are cheapest-first).
    for tier in breakdown.tiers:
        if tier.overage_cost_per_unit_usd is not None:
            return tier.overage_cost_per_unit_usd

    # Precedence 3: no per-unit rate.
    return None


# ============================================================================
# Aggregate helpers — used by the frontend CoverageMatrix and results table.
# ============================================================================


def monthly_cost_for_scope_map(
    breakdown: PricingBreakdown,
    scope_volumes: dict[str, int],
) -> float:
    """
    Sum of per-scope costs when the user provides a per-scope volume map.
    For scopes without a per-unit rate, falls back to the cheapest tier's
    base cost (so a flat-rate tool doesn't show as free).

    `scope_volumes` example: {"step_1": 500, "step_2": 10000} — the user
    expects 500 OCR pages and 10k sync events per month.
    """
    if not scope_volumes:
        return 0.0
    total = 0.0
    # Base cost applied once — it's a monthly flat fee regardless of scopes.
    base = breakdown.tiers[0].monthly_cost_usd if breakdown.tiers else 0.0
    total += base
    for scope_id, volume in scope_volumes.items():
        rate = per_scope_unit_cost(breakdown, scope_id)
        if rate is None:
            # Flat-rate tool — base already counted, no per-scope add.
            continue
        total += volume * rate
    return total


def cheapest_meaningful_tier(
    breakdown: PricingBreakdown,
) -> PricingTier | None:
    """
    The cheapest tier that isn't a $0 free-tier-without-overage. Useful
    for comparing candidates' "starting prices" — a tool with only a $0
    free tier shouldn't visually beat one with a $5/mo real tier when
    the user's volume exceeds the free allowance anyway.
    """
    if not breakdown.tiers:
        return None
    for tier in breakdown.tiers:
        if tier.monthly_cost_usd > 0 or tier.overage_cost_per_unit_usd:
            return tier
    # Every tier is a $0 no-overage tier — return the first one.
    return breakdown.tiers[0]


def _singular_unit(unit_name: str | None) -> str:
    """
    Best-effort singularization for UI strings. "$0.005/call overage" reads
    better than "$0.005/calls overage". Deliberately simple — doesn't need
    to be perfect, just sound natural for the common units (pages, calls,
    events, documents, tokens).
    """
    if not unit_name:
        return "unit"
    if unit_name.endswith("ies") and len(unit_name) > 3:
        return unit_name[:-3] + "y"  # queries -> query
    if unit_name.endswith("s") and not unit_name.endswith("ss"):
        return unit_name[:-1]  # pages -> page, calls -> call
    return unit_name


def format_tier_short(tier: PricingTier) -> str:
    """
    One-line summary suitable for a UI tooltip: "$29/mo · 5000 calls included
    · $0.005/call overage".
    """
    parts: list[str] = [f"${tier.monthly_cost_usd:g}/mo"]
    if tier.included_units is not None and tier.unit_name:
        parts.append(f"{tier.included_units:,} {tier.unit_name} included")
    elif tier.included_units is not None:
        parts.append(f"{tier.included_units:,} units included")
    if tier.overage_cost_per_unit_usd is not None:
        unit = _singular_unit(tier.unit_name)
        parts.append(f"${tier.overage_cost_per_unit_usd:g}/{unit} overage")
    return " · ".join(parts)


def format_breakdown_short(breakdown: PricingBreakdown) -> str:
    """
    Short "starting at $X/mo" summary for display under a candidate's name.
    Picks cheapest_meaningful_tier. Distinguishes TRUE pay-as-you-go (the
    breakdown.pay_as_you_go flag is set AND there's only one tier) from
    freemium (multiple tiers starting with a $0 + overage option).
    """
    tier = cheapest_meaningful_tier(breakdown)
    if tier is None:
        return "Pricing unknown"
    # True pay-as-you-go — single tier, $0 base, per-unit overage. Shows
    # unit cost since "From $0/mo" would be misleading.
    true_payg = (
        breakdown.pay_as_you_go
        and len(breakdown.tiers) == 1
        and tier.monthly_cost_usd == 0
        and tier.overage_cost_per_unit_usd is not None
    )
    if true_payg:
        unit = _singular_unit(tier.unit_name)
        return f"${tier.overage_cost_per_unit_usd:g}/{unit} (pay-as-you-go)"
    return f"From ${tier.monthly_cost_usd:g}/mo"


__all__ = (
    "estimate_monthly_cost",
    "per_scope_unit_cost",
    "monthly_cost_for_scope_map",
    "cheapest_meaningful_tier",
    "format_tier_short",
    "format_breakdown_short",
)


