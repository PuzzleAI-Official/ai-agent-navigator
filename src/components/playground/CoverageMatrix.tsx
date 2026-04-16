// ============================================================================
// CoverageMatrix — Phase 4 candidates × scopes matrix view
// ============================================================================
// Renders a tabular candidates × scopes grid above/beside the per-scope
// CandidateCard stacks. Rows = candidates (sorted by how many scopes they
// cover, descending, so all-in-ones rise to the top; tiebreak by relevance).
// Columns = blueprint scopes (in blueprint order). Cells reveal coverage
// state at a glance:
//
//   empty cell     — candidate does NOT cover that scope
//   amber ⦿        — candidate CLAIMS that scope (Agent 2 guess)
//   emerald ✓      — that scope is VERIFIED (Phase 6.5)
//   amber ⦿*       — same as claimed, but with test results (future phases)
//
// The matrix is the honest presentation layer for "one vendor vs best-per-
// step" — a user who values vendor consolidation sees which all-in-ones
// cover every scope; a user who values best-of-breed sees who wins each
// column independently.
//
// Only renders when:
//   - blueprint has >= 2 scopes, AND
//   - at least one candidate has a non-empty covers_step_ids.
// Single-scope runs don't need a matrix (one column = already a list).
// ============================================================================

import { motion } from "framer-motion";
import type { PipelineCandidate, WorkflowStep } from "@/types/pipeline";
import { perScopeUnitCost } from "@/lib/pricing";

interface Props {
  candidates: PipelineCandidate[];
  workflowSteps: WorkflowStep[];
}

export function CoverageMatrix({ candidates, workflowSteps }: Props) {
  // Guardrails: skip rendering when the matrix wouldn't be informative.
  if (workflowSteps.length < 2) return null;
  const anyCoverage = candidates.some((c) => c.covers_step_ids.length > 0);
  if (!anyCoverage) return null;

  // Sort candidates for a readable matrix:
  //   primary: coverage count descending (all-in-ones at top)
  //   secondary: relevance_score descending (higher match wins within group)
  //   tertiary: name (stable alphabetical)
  const sorted = [...candidates].sort((a, b) => {
    const da = a.covers_step_ids.length;
    const db = b.covers_step_ids.length;
    if (da !== db) return db - da;
    const ra = a.relevance_score || 0;
    const rb = b.relevance_score || 0;
    if (ra !== rb) return rb - ra;
    return a.name.localeCompare(b.name);
  });

  // Per-scope coverage counts for the footer row ("how many candidates
  // cover each scope"). Surfaces thin scopes immediately.
  const perScopeCounts: Record<string, number> = {};
  for (const step of workflowSteps) perScopeCounts[step.id] = 0;
  for (const c of candidates) {
    for (const sid of c.covers_step_ids) {
      if (sid in perScopeCounts) perScopeCounts[sid] += 1;
    }
  }

  return (
    <motion.section
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      className="px-6 lg:px-8 pt-5 pb-4 border-b border-white/[0.06] bg-white/[0.015]"
      data-testid="coverage-matrix"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-grotesk font-semibold uppercase tracking-[0.1em] text-white/45">
            Coverage Matrix
          </span>
          <span className="text-[10px] font-mono text-white/30">
            {sorted.length} candidate{sorted.length === 1 ? "" : "s"} ×{" "}
            {workflowSteps.length} scopes
          </span>
        </div>
        <div className="flex items-center gap-3 text-[9px] font-mono text-white/35">
          <LegendItem color="amber" label="claimed" />
          <LegendItem color="emerald" label="verified" />
          <LegendItem color="white" label="not covered" mute />
        </div>
      </div>

      {/* Horizontally scrollable so wide DAGs don't overflow the panel. */}
      <div className="overflow-x-auto scrollbar-thin">
        <table className="min-w-full border-separate border-spacing-0 text-[11px]">
          <thead>
            <tr>
              <th className="sticky left-0 z-10 bg-[#0a0a0b] text-left font-grotesk font-medium uppercase tracking-[0.08em] text-[10px] text-white/35 px-2 py-1.5 border-b border-white/[0.08] min-w-[180px]">
                Candidate
              </th>
              {workflowSteps.map((step) => (
                <th
                  key={step.id}
                  className="text-center font-grotesk font-medium uppercase tracking-[0.06em] text-[10px] text-white/40 px-2 py-1.5 border-b border-white/[0.08] min-w-[80px]"
                  title={`${step.id} · ${step.description}`}
                >
                  <div className="text-blue-300/70">{step.role}</div>
                  <div className="text-[8px] text-white/20 font-mono mt-0.5">
                    {step.id}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((candidate, rowIdx) => (
              <tr
                key={candidate.name}
                className="group"
                data-testid={`coverage-row-${candidate.name}`}
              >
                <td
                  className={`sticky left-0 z-10 bg-[#0a0a0b] px-2 py-2 border-b border-white/[0.04] ${
                    rowIdx === 0 ? "" : ""
                  }`}
                >
                  <div className="flex flex-col">
                    <span className="text-white/80 font-grotesk font-medium">
                      {candidate.name}
                    </span>
                    <span className="text-[9px] text-white/25 font-sans">
                      {candidate.provider} ·{" "}
                      {Math.round((candidate.relevance_score || 0) * 100)}% fit
                    </span>
                  </div>
                </td>
                {workflowSteps.map((step) => {
                  const covered = candidate.covers_step_ids.includes(step.id);
                  const conf = candidate.coverage_confidence[step.id];
                  // Phase 5: per-scope unit cost overlay. Shown underneath
                  // the coverage dot when pricing varies by scope AND this
                  // candidate covers this scope. Null-safe — when
                  // pricing_breakdown is missing (pre-Phase-6.5) the
                  // overlay is silent.
                  const perScope = covered
                    ? perScopeUnitCost(candidate.pricing_breakdown ?? null, step.id)
                    : null;
                  return (
                    <td
                      key={step.id}
                      className="text-center px-2 py-2 border-b border-white/[0.04]"
                      data-testid={`coverage-cell-${candidate.name}-${step.id}`}
                    >
                      <div className="flex flex-col items-center gap-0.5">
                        <CoverageCell covered={covered} confidence={conf} />
                        {perScope !== null && (
                          <span
                            className="text-[9px] font-mono text-white/35"
                            title={`Per-${step.role || step.id} unit cost`}
                            data-testid={`coverage-cell-cost-${candidate.name}-${step.id}`}
                          >
                            ${_formatUnitCost(perScope)}
                          </span>
                        )}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
            {/* Footer row: per-scope tally. */}
            <tr>
              <td className="sticky left-0 z-10 bg-[#0a0a0b] px-2 py-1.5 text-[9px] font-grotesk uppercase tracking-[0.08em] text-white/35">
                Coverage depth
              </td>
              {workflowSteps.map((step) => {
                const count = perScopeCounts[step.id];
                const thin = count > 0 && count < 3;
                const empty = count === 0;
                return (
                  <td
                    key={step.id}
                    className="text-center px-2 py-1.5 text-[10px] font-mono"
                  >
                    <span
                      className={
                        empty
                          ? "text-red-400/70"
                          : thin
                          ? "text-amber-400/70"
                          : "text-emerald-400/70"
                      }
                      title={
                        empty
                          ? "No candidate claims this scope — Phase 6.5 has nothing to verify here"
                          : thin
                          ? `${count} candidate(s) — thin pool, aim for >=3`
                          : `${count} candidate(s) claim coverage`
                      }
                    >
                      {count}
                    </span>
                  </td>
                );
              })}
            </tr>
          </tbody>
        </table>
      </div>
    </motion.section>
  );
}

function CoverageCell({
  covered,
  confidence,
}: {
  covered: boolean;
  confidence: "claimed" | "verified" | undefined;
}) {
  if (!covered) {
    return (
      <span
        className="inline-block w-3 h-[1px] bg-white/[0.08]"
        aria-label="not covered"
      />
    );
  }
  if (confidence === "verified") {
    return (
      <span
        className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-emerald-400/10 border border-emerald-400/30 text-emerald-300/90 text-[10px]"
        aria-label="verified coverage"
      >
        ✓
      </span>
    );
  }
  // claimed (or missing confidence, which we treat as claimed)
  return (
    <span
      className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-amber-400/10 border border-amber-400/25 text-amber-300/80 text-[11px] leading-none"
      aria-label="claimed coverage"
    >
      ⦿
    </span>
  );
}

function LegendItem({
  color,
  label,
  mute,
}: {
  color: "amber" | "emerald" | "white";
  label: string;
  mute?: boolean;
}) {
  const dot =
    color === "amber"
      ? "bg-amber-400/70"
      : color === "emerald"
      ? "bg-emerald-400/80"
      : "bg-white/30";
  return (
    <span className={`inline-flex items-center gap-1 ${mute ? "opacity-60" : ""}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

/**
 * Trim trailing zeros for a unit-cost display: 0.01 → "0.01", 0.005 → "0.005".
 * Matches Python's `:g` format closely enough for inline cell labels.
 */
function _formatUnitCost(n: number): string {
  if (Number.isInteger(n)) return String(n);
  return String(parseFloat(n.toPrecision(4)));
}
