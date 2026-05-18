// ============================================================================
// RejectionSummary - per-scope rejection counts from selected-candidate verification
// ============================================================================
// Shows per-scope rejection counts + expandable detail after selected-candidate
// verification. Null-safe: renders nothing when `rejections` is empty
// (e.g. every selected candidate passed verification).
//
// Data arrives via `candidate_rejected` SSE events:
//   - Collapsed header: "2 rejections across 3 scopes"
//   - Expanded: per-scope grouped list with reason category + one-line
//     explanation so the user sees exactly why a pick failed verification
//     and wasn't tested (no silent substitution).
// ============================================================================

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { RejectionEntry, WorkflowStep } from "@/types/pipeline";

interface Props {
  rejections: RejectionEntry[];
  workflowSteps?: WorkflowStep[];
}

const REASON_LABELS: Record<RejectionEntry["reason"], string> = {
  docs_unreachable: "Docs unreachable",
  enterprise_only: "Enterprise only",
  deprecated: "Deprecated",
  no_api: "No public API",
  coverage_removed_at_scope: "Coverage unverified",
  verify_error: "Verify error",
};

export function RejectionSummary({ rejections, workflowSteps }: Props) {
  const [expanded, setExpanded] = useState(false);

  if (!rejections || rejections.length === 0) return null;

  // Group by scope for the expanded view
  const byScope = new Map<string, RejectionEntry[]>();
  for (const r of rejections) {
    const list = byScope.get(r.scope_id) || [];
    list.push(r);
    byScope.set(r.scope_id, list);
  }

  const stepById = new Map(
    (workflowSteps ?? []).map((s) => [s.id, s])
  );

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      className="mx-6 lg:mx-8 mt-4 rounded-lg border border-amber-400/15 bg-amber-500/[0.03] overflow-hidden"
      data-testid="rejection-summary"
    >
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full px-4 py-2.5 flex items-center justify-between text-left"
        data-testid="rejection-summary-toggle"
      >
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 bg-amber-400/60 rounded-full shrink-0" />
          <span className="text-[12px] font-grotesk text-amber-200/80">
            {rejections.length} rejection{rejections.length === 1 ? "" : "s"} across{" "}
            {byScope.size} scope{byScope.size === 1 ? "" : "s"}
          </span>
        </div>
        <span className="text-[10px] text-white/30">{expanded ? "▾" : "▸"}</span>
      </button>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="border-t border-amber-400/10 px-4 py-3 space-y-3"
          >
            {Array.from(byScope.entries()).map(([scopeId, entries]) => {
              const step = stepById.get(scopeId);
              return (
                <div key={scopeId} data-testid={`rejection-scope-${scopeId}`}>
                  <div className="text-[10px] font-grotesk font-semibold uppercase tracking-[0.08em] text-white/40 mb-1">
                    {step ? `${step.role} (${scopeId})` : scopeId}
                  </div>
                  {entries.map((r, i) => (
                    <div
                      key={`${r.name}-${i}`}
                      className="flex items-start gap-2 px-2 py-1.5 text-[11px]"
                      data-testid={`rejection-entry-${r.name}`}
                    >
                      <span className="w-1 h-1 mt-1.5 rounded-full bg-amber-400/50 shrink-0" />
                      <div className="min-w-0">
                        <span className="text-white/70 font-sans">{r.name}</span>
                        <span className="text-white/25 mx-1">—</span>
                        <span className="text-amber-300/70 font-mono text-[10px]">
                          {REASON_LABELS[r.reason] || r.reason}
                        </span>
                        {r.attempt_notes && (
                          <p className="text-[10px] text-white/25 mt-0.5 leading-[1.5]">
                            {r.attempt_notes}
                          </p>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              );
            })}
            <p className="text-[10px] text-white/20 italic pt-1">
              Rejected candidates were dropped — no substitution. The scopes
              they were picked at test with fewer candidates.
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
