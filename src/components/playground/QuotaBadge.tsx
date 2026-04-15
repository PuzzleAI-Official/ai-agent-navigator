// ============================================================================
// QuotaBadge — Phase 2 service-tier surface in the playground header
// ============================================================================
// Shows the user's plan + remaining credits. Polls GET /pzapi/runs/{id} on a
// slow interval while a run is active so the count stays current as Agents
// 4/5 consume credits.
//
// Display states:
//   - No run yet         -> "Free · Search unlimited" (default state)
//   - Free + run active  -> "Free · Search unlimited" (testing not in plan)
//   - Paid + credits >0  -> "Paid · 95 credits"
//   - Paid + credits <=0 -> "Paid · 0 credits" in red (when billing_enforced)
//   - Enterprise         -> "Enterprise · Unlimited"
//
// Honest caveats (matters for the diagnostic story in CLAUDE.md):
//   - When billing_enforced=false (today's default), the badge is informational
//     only — credits go negative, badge stays green, agents still run. The
//     value is observability, not gating.
//   - Polling is slow (5s) because credit changes happen at agent transitions
//     (Agent 4 entry, Agent 5 entry) — sub-second resolution doesn't help.
// ============================================================================

import { useEffect, useState } from "react";

import { getRunState } from "@/services/api";
import type { Quota } from "@/types/pipeline";

const POLL_INTERVAL_MS = 5000;

interface QuotaBadgeProps {
  /** Active run id; when null the badge shows the default "Free" state. */
  runId: string | null;
  /** Whether to actively poll; pass false when the run is in `results` stage. */
  active?: boolean;
}

const DEFAULT_QUOTA: Quota = {
  plan: "free",
  credits_remaining: null,
  credits_consumed: 0,
  tier_features: { search: true, testing: false, monitoring: false },
  billing_enforced: false,
};

export function QuotaBadge({ runId, active = true }: QuotaBadgeProps) {
  const [quota, setQuota] = useState<Quota>(DEFAULT_QUOTA);
  const [error, setError] = useState<boolean>(false);

  // Poll run state while active to refresh credits after gate transitions.
  useEffect(() => {
    if (!runId || !active) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const state = await getRunState(runId);
        if (!cancelled && state.quota) {
          setQuota(state.quota);
          setError(false);
        }
      } catch {
        if (!cancelled) setError(true);
      }
    };

    poll(); // immediate fetch so the badge updates right after createRun
    const id = window.setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [runId, active]);

  const planLabel = quota.plan.charAt(0).toUpperCase() + quota.plan.slice(1);
  const creditsLabel = formatCredits(quota);
  const isLow =
    quota.billing_enforced &&
    quota.credits_remaining !== null &&
    quota.credits_remaining <= 0;

  return (
    <div
      className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md border ${
        isLow
          ? "bg-red-500/10 border-red-400/30 text-red-300"
          : "bg-white/[0.04] border-white/[0.06] text-white/55"
      }`}
      title={
        error
          ? "Could not fetch quota — backend may be unreachable"
          : `Plan: ${planLabel} · ${creditsLabel}` +
            (quota.billing_enforced ? " · Enforced" : " · Advisory")
      }
    >
      <svg
        width="12"
        height="12"
        viewBox="0 0 16 16"
        fill="none"
        className={isLow ? "text-red-400/80" : "text-white/35"}
      >
        <rect x="2" y="4" width="12" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.2" />
        <path d="M2 7h12" stroke="currentColor" strokeWidth="1.2" />
      </svg>
      <span className="text-[11px] font-grotesk font-medium uppercase tracking-[0.06em]">
        {planLabel}
      </span>
      <span className="text-[10px] font-mono opacity-70">{creditsLabel}</span>
    </div>
  );
}

function formatCredits(q: Quota): string {
  if (q.credits_remaining === null) {
    // Unlimited (free or enterprise) — distinguish by which feature is gated.
    return q.tier_features.testing ? "unlimited" : "search only";
  }
  return `${q.credits_remaining} credits`;
}
