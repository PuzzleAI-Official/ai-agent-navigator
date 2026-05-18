// ============================================================================
// Pricing helpers — Phase 5a frontend mirror of puzzleeval/pricing.py
// ============================================================================
// Port of the Python helpers so the frontend can compute per-scope costs,
// monthly estimates, and short summaries without a round-trip to the
// backend. Kept intentionally literal with the Python implementation so
// behavior stays in sync — any change here should land in pricing.py too.
//
// All functions accept `null | undefined` breakdowns gracefully — the
// selected-candidate verification may not have populated pricing for every candidate,
// so every call site needs a null-safe path.
// ============================================================================

import type { PricingBreakdown, PricingTier } from "@/types/pipeline";

export function estimateMonthlyCost(
  breakdown: PricingBreakdown | null | undefined,
  monthlyVolume: number
): number {
  if (!breakdown || breakdown.tiers.length === 0) return 0;
  if (monthlyVolume < 0) {
    throw new Error(`monthlyVolume must be >= 0, got ${monthlyVolume}`);
  }

  const candidates: number[] = [];
  for (const tier of breakdown.tiers) {
    const cost = tierCostAt(tier, monthlyVolume);
    if (cost !== null) candidates.push(cost);
  }
  if (candidates.length > 0) return Math.min(...candidates);

  // Every tier capped below volume with no overage → best-effort ceiling.
  return Math.max(...breakdown.tiers.map((t) => t.monthly_cost_usd));
}

function tierCostAt(tier: PricingTier, volume: number): number | null {
  const base = tier.monthly_cost_usd;
  const included = tier.included_units ?? null;
  const overage = tier.overage_cost_per_unit_usd ?? null;

  if (included === null && overage === null) return base;
  if (included === null && overage !== null) return base + volume * overage;
  if (included !== null) {
    if (volume <= included) return base;
    if (overage !== null) return base + (volume - included) * overage;
    return null;
  }
  return base;
}

export function perScopeUnitCost(
  breakdown: PricingBreakdown | null | undefined,
  scopeId: string
): number | null {
  if (!breakdown) return null;
  if (scopeId in breakdown.per_scope_unit_cost) {
    return breakdown.per_scope_unit_cost[scopeId];
  }
  for (const tier of breakdown.tiers) {
    if (tier.overage_cost_per_unit_usd != null) {
      return tier.overage_cost_per_unit_usd;
    }
  }
  return null;
}

export function cheapestMeaningfulTier(
  breakdown: PricingBreakdown | null | undefined
): PricingTier | null {
  if (!breakdown || breakdown.tiers.length === 0) return null;
  for (const tier of breakdown.tiers) {
    if (
      tier.monthly_cost_usd > 0 ||
      (tier.overage_cost_per_unit_usd ?? 0) > 0
    ) {
      return tier;
    }
  }
  return breakdown.tiers[0];
}

function singularUnit(unitName?: string | null): string {
  if (!unitName) return "unit";
  if (unitName.endsWith("ies") && unitName.length > 3) {
    return unitName.slice(0, -3) + "y";
  }
  if (unitName.endsWith("s") && !unitName.endsWith("ss")) {
    return unitName.slice(0, -1);
  }
  return unitName;
}

export function formatTierShort(tier: PricingTier): string {
  const parts: string[] = [`$${trimNumber(tier.monthly_cost_usd)}/mo`];
  if (tier.included_units != null && tier.unit_name) {
    parts.push(
      `${tier.included_units.toLocaleString()} ${tier.unit_name} included`
    );
  } else if (tier.included_units != null) {
    parts.push(`${tier.included_units.toLocaleString()} units included`);
  }
  if (tier.overage_cost_per_unit_usd != null) {
    const u = singularUnit(tier.unit_name);
    parts.push(`$${trimNumber(tier.overage_cost_per_unit_usd)}/${u} overage`);
  }
  return parts.join(" · ");
}

export function formatBreakdownShort(
  breakdown: PricingBreakdown | null | undefined
): string {
  const tier = cheapestMeaningfulTier(breakdown);
  if (!tier || !breakdown) return "Pricing unknown";
  const truePayg =
    breakdown.pay_as_you_go &&
    breakdown.tiers.length === 1 &&
    tier.monthly_cost_usd === 0 &&
    tier.overage_cost_per_unit_usd != null;
  if (truePayg) {
    const u = singularUnit(tier.unit_name);
    return `$${trimNumber(tier.overage_cost_per_unit_usd!)}/${u} (pay-as-you-go)`;
  }
  return `From $${trimNumber(tier.monthly_cost_usd)}/mo`;
}

/**
 * Trim trailing zeros from a number for display. 29.0 → "29", 0.005 → "0.005".
 * Matches Python's `:g` format behavior closely enough for UI strings.
 */
function trimNumber(n: number): string {
  if (Number.isInteger(n)) return String(n);
  return String(parseFloat(n.toPrecision(6)));
}
