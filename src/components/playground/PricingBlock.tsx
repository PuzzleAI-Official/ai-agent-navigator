// ============================================================================
// PricingBlock — structured pricing surface on CandidateCard
// ============================================================================
// Renders the candidate's `pricing_breakdown` as:
//   - a compact header line ("From $29/mo" / "$0.01/call pay-as-you-go" /
//     "Pricing unknown") with a confidence dot (high = emerald, medium =
//     amber, low = red)
//   - an optional collapsed tier list (click "▸ N tiers" to expand)
//   - per-scope cost chips when pricing varies across the scopes this
//     candidate covers (uses per_scope_unit_cost override or falls back
//     to the cheapest tier's overage)
//   - source URLs at the bottom so the user can verify the numbers
//
// Data comes from the deep-verify 4B extraction. CandidateCard only mounts
// this component when pricing_breakdown is non-null.
// ============================================================================

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";

import type {
  PricingBreakdown,
  WorkflowStep,
  PricingConfidence,
} from "@/types/pipeline";
import {
  formatBreakdownShort,
  formatTierShort,
  perScopeUnitCost,
} from "@/lib/pricing";

interface Props {
  breakdown: PricingBreakdown;
  coversStepIds: string[];
  workflowSteps?: WorkflowStep[];
}

export function PricingBlock({ breakdown, coversStepIds, workflowSteps }: Props) {
  const [expanded, setExpanded] = useState(false);

  const shortLine = formatBreakdownShort(breakdown);
  const hasTiers = breakdown.tiers.length > 0;
  const multipleTiers = breakdown.tiers.length > 1;

  // Per-scope cost chips: show only when pricing varies across scopes
  // the candidate covers. If per_scope_unit_cost is empty AND all scopes
  // fall back to the same cheapest-tier overage, the chips are redundant
  // — skip them.
  const perScopeChips = _computePerScopeChips(breakdown, coversStepIds, workflowSteps);

  return (
    <div
      className="rounded-md border border-white/[0.06] bg-white/[0.015] px-3 py-2"
      data-testid="pricing-block"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <ConfidenceDot confidence={breakdown.confidence} />
          <span
            className="text-[12px] font-grotesk text-white/80 truncate"
            title={_tooltipForHeader(breakdown)}
          >
            {shortLine}
          </span>
          {breakdown.pay_as_you_go && breakdown.tiers.length > 1 && (
            <span className="text-[9px] font-mono text-white/30 px-1.5 py-0.5 rounded bg-white/[0.04]">
              PAYG
            </span>
          )}
        </div>
        {hasTiers && multipleTiers && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[10px] font-mono text-white/30 hover:text-white/60 transition-colors shrink-0"
            data-testid="pricing-tier-toggle"
          >
            {expanded ? "▾" : "▸"} {breakdown.tiers.length} tiers
          </button>
        )}
      </div>

      {/* Per-scope cost chips — meaningful only when pricing varies by scope */}
      {perScopeChips.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {perScopeChips.map((chip) => (
            <span
              key={chip.stepId}
              className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded border border-white/[0.08] bg-white/[0.03] text-white/55 font-sans"
              title={`${chip.stepId}${chip.role ? " · " + chip.role : ""} · ${chip.displayCost}`}
              data-testid={`pricing-scope-chip-${chip.stepId}`}
            >
              <span className="text-white/30 font-mono text-[9px]">
                {chip.role || chip.stepId}
              </span>
              <span className="text-white/75 font-mono">{chip.displayCost}</span>
            </span>
          ))}
        </div>
      )}

      <AnimatePresence>
        {expanded && multipleTiers && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="mt-2 space-y-1 overflow-hidden"
          >
            {breakdown.tiers.map((tier, i) => (
              <div
                key={`${tier.name}-${i}`}
                className="flex items-start gap-2 text-[11px] text-white/55 font-sans"
                data-testid={`pricing-tier-${i}`}
              >
                <span className="text-[10px] font-mono text-white/30 mt-0.5 shrink-0 w-14">
                  {tier.name}
                </span>
                <span className="leading-[1.5]">{formatTierShort(tier)}</span>
              </div>
            ))}
            {breakdown.notes && (
              <p className="mt-1 text-[10px] text-white/35 italic font-sans leading-[1.5]">
                {breakdown.notes}
              </p>
            )}
            {breakdown.sources.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1.5 items-center">
                <span className="text-[9px] font-mono text-white/25 uppercase tracking-[0.08em]">
                  sources:
                </span>
                {breakdown.sources.map((src) => (
                  <a
                    key={src}
                    href={src}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-[10px] text-blue-400/60 hover:text-blue-300/80 truncate max-w-[180px] font-mono"
                    title={src}
                  >
                    {_shortenUrl(src)}
                  </a>
                ))}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface PerScopeChip {
  stepId: string;
  role?: string;
  displayCost: string;
}

function _computePerScopeChips(
  breakdown: PricingBreakdown,
  coversStepIds: string[],
  workflowSteps?: WorkflowStep[]
): PerScopeChip[] {
  // Only show per-scope chips when pricing ACTUALLY varies across scopes.
  // If per_scope_unit_cost is populated, those are the chips. Otherwise
  // there's nothing scope-specific to surface.
  const perScope = breakdown.per_scope_unit_cost;
  if (!perScope || Object.keys(perScope).length === 0) return [];

  const stepById = new Map(
    (workflowSteps ?? []).map((s) => [s.id, s])
  );
  const chips: PerScopeChip[] = [];
  for (const stepId of coversStepIds) {
    const rate = perScopeUnitCost(breakdown, stepId);
    if (rate === null) continue;
    const step = stepById.get(stepId);
    chips.push({
      stepId,
      role: step?.role,
      displayCost: `$${_trim(rate)}/unit`,
    });
  }
  return chips;
}

function ConfidenceDot({ confidence }: { confidence: PricingConfidence }) {
  const color =
    confidence === "high"
      ? "bg-emerald-400/80"
      : confidence === "medium"
      ? "bg-amber-400/70"
      : "bg-red-400/70";
  const label =
    confidence === "high"
      ? "High confidence — pricing page parsed cleanly"
      : confidence === "medium"
      ? "Medium confidence — tiers inferred from partial docs"
      : "Low confidence — pricing page not found; numbers are estimates";
  return (
    <span
      className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${color}`}
      title={label}
      data-testid={`pricing-confidence-${confidence}`}
    />
  );
}

function _tooltipForHeader(breakdown: PricingBreakdown): string {
  const lines = [
    `Pricing confidence: ${breakdown.confidence}`,
    `Billing: ${breakdown.billing_granularity}`,
  ];
  if (breakdown.free_tier_monthly_units != null) {
    lines.push(`Free tier: ${breakdown.free_tier_monthly_units.toLocaleString()} units/mo`);
  }
  if (breakdown.pay_as_you_go) lines.push("Pay-as-you-go available");
  return lines.join("\n");
}

function _shortenUrl(url: string): string {
  try {
    const u = new URL(url);
    return u.host + (u.pathname === "/" ? "" : u.pathname);
  } catch {
    return url;
  }
}

function _trim(n: number): string {
  if (Number.isInteger(n)) return String(n);
  return String(parseFloat(n.toPrecision(6)));
}
