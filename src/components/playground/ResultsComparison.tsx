// ============================================================================
// ResultsComparison — Phase 9 per-scope table view with legacy fallback
// ============================================================================
// When `scopeRuns` are provided (Phase 9 multi-scope runs), renders one
// section per scope: a header with scope_id, role badge, and test count,
// followed by a table of candidates at that scope sorted by pass_rate
// descending. Columns: Candidate, Pass Rate (bar), Score, Avg Latency, Cost.
//
// When NO scopeRuns are provided (legacy single-scope), falls back to the
// original flat card layout with ResultCard + Recommendation.
// ============================================================================

import { useState } from "react";
import { motion } from "framer-motion";
import type {
  PipelineCandidate,
  ScopeTestRun,
  WorkflowStep,
} from "@/types/pipeline";
import { TestResultRow } from "./TestResultRow";

interface Props {
  candidates: PipelineCandidate[];
  scopeRuns?: ScopeTestRun[];
  workflowSteps?: WorkflowStep[];
}

export function ResultsComparison({ candidates, scopeRuns, workflowSteps }: Props) {
  // Phase 9: per-scope table view when scope data is available
  if (scopeRuns && scopeRuns.length > 0) {
    return (
      <div data-testid="results-comparison" className="p-6 lg:p-8">
        <div className="mb-8">
          <h2 className="font-display text-[22px] text-[#f5f5f7] tracking-[-0.01em]">
            Evaluation Results
          </h2>
          <p className="text-[13px] text-[#6e6e73] mt-1 font-sans">
            {scopeRuns.length} scope{scopeRuns.length === 1 ? "" : "s"} evaluated
            {" "}&middot; per-scope breakdown
          </p>
        </div>

        <div className="space-y-8">
          {scopeRuns.map((scope, idx) => (
            <ScopeSection
              key={scope.scope_id}
              scope={scope}
              index={idx}
              workflowSteps={workflowSteps}
            />
          ))}
        </div>
      </div>
    );
  }

  // Legacy single-scope fallback — original flat card layout
  return <LegacyResults candidates={candidates} />;
}

// ---------------------------------------------------------------------------
// Phase 9: Per-scope section with candidate table
// ---------------------------------------------------------------------------

function ScopeSection({
  scope,
  index,
  workflowSteps,
}: {
  scope: ScopeTestRun;
  index: number;
  workflowSteps?: WorkflowStep[];
}) {
  const sorted = [...scope.candidate_results].sort(
    (a, b) => b.pass_rate - a.pass_rate,
  );

  // Look up the workflow step description if available
  const step = workflowSteps?.find((s) => s.id === scope.scope_id);

  return (
    <motion.section
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.08, duration: 0.3 }}
      data-testid={`scope-section-${scope.scope_id}`}
    >
      {/* Scope header */}
      <div className="flex items-center gap-3 mb-3">
        <h3 className="font-grotesk font-semibold text-[15px] text-white/90">
          {scope.scope_id}
        </h3>
        <span className="text-[10px] font-grotesk tracking-[0.04em] px-2 py-0.5 rounded bg-blue-400/10 text-blue-300/80 border border-blue-400/15">
          {scope.scope_role}
        </span>
        <span className="text-[11px] font-mono text-white/30">
          {scope.test_case_count} test{scope.test_case_count === 1 ? "" : "s"}
        </span>
      </div>

      {step?.description && (
        <p className="text-[11px] text-white/30 font-sans mb-3 leading-[1.5]">
          {step.description}
        </p>
      )}

      {/* Candidate table */}
      <div className="overflow-x-auto scrollbar-thin rounded-lg border border-white/[0.06] bg-white/[0.015]">
        <table className="min-w-full border-separate border-spacing-0 text-[12px]">
          <thead>
            <tr>
              <th className="text-left font-grotesk font-medium uppercase tracking-[0.08em] text-[10px] text-white/35 px-4 py-2.5 border-b border-white/[0.08] min-w-[180px]">
                Candidate
              </th>
              <th className="text-left font-grotesk font-medium uppercase tracking-[0.08em] text-[10px] text-white/35 px-4 py-2.5 border-b border-white/[0.08] min-w-[200px]">
                Pass Rate
              </th>
              <th className="text-right font-grotesk font-medium uppercase tracking-[0.08em] text-[10px] text-white/35 px-4 py-2.5 border-b border-white/[0.08]">
                Score
              </th>
              <th className="text-right font-grotesk font-medium uppercase tracking-[0.08em] text-[10px] text-white/35 px-4 py-2.5 border-b border-white/[0.08]">
                Avg Latency
              </th>
              <th className="text-right font-grotesk font-medium uppercase tracking-[0.08em] text-[10px] text-white/35 px-4 py-2.5 border-b border-white/[0.08]">
                Cost
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((cr, rowIdx) => (
              <ScopeCandidateRow
                key={cr.candidate_name}
                result={cr}
                rank={rowIdx + 1}
                isBest={rowIdx === 0}
              />
            ))}
          </tbody>
        </table>
      </div>
    </motion.section>
  );
}

// ---------------------------------------------------------------------------
// Individual candidate row inside a scope table
// ---------------------------------------------------------------------------

function ScopeCandidateRow({
  result,
  rank,
  isBest,
}: {
  result: ScopeCandidateResult;
  rank: number;
  isBest: boolean;
}) {
  const passPercent = Math.round(result.pass_rate * 100);
  const scoreDisplay = Math.round(result.pass_rate * 100);

  return (
    <tr
      className={`group transition-colors ${
        isBest ? "bg-emerald-500/[0.04]" : "hover:bg-white/[0.02]"
      }`}
      data-testid={`scope-candidate-row-${result.candidate_name}`}
    >
      {/* Candidate name */}
      <td className="px-4 py-3 border-b border-white/[0.04]">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-white/20 w-4">
            #{rank}
          </span>
          <span
            className={`font-grotesk font-medium text-[13px] ${
              isBest ? "text-emerald-300/90" : "text-white/80"
            }`}
          >
            {result.candidate_name}
          </span>
          {isBest && (
            <span className="text-[9px] font-grotesk tracking-[0.04em] px-1.5 py-0.5 rounded bg-emerald-400/10 text-emerald-300/70 border border-emerald-400/15">
              Best
            </span>
          )}
        </div>
      </td>

      {/* Pass rate with bar */}
      <td className="px-4 py-3 border-b border-white/[0.04]">
        <div className="flex items-center gap-3">
          <div className="flex-1 h-[6px] bg-white/[0.06] rounded-full overflow-hidden max-w-[120px]">
            <motion.div
              className={`h-full rounded-full ${
                passPercent >= 80
                  ? "bg-emerald-400/70"
                  : passPercent >= 50
                  ? "bg-amber-400/70"
                  : "bg-red-400/70"
              }`}
              initial={{ width: 0 }}
              animate={{ width: `${passPercent}%` }}
              transition={{ duration: 0.8, delay: 0.1 }}
            />
          </div>
          <span className="font-mono text-[12px] text-white/70 w-10 text-right tabular-nums">
            {passPercent}%
          </span>
          <span className="text-[10px] text-white/25 font-mono">
            {result.tests_passed}/{(result.tests_passed + result.tests_failed + result.tests_errored)}
          </span>
        </div>
      </td>

      {/* Score */}
      <td className="px-4 py-3 border-b border-white/[0.04] text-right">
        <span className="font-mono text-[13px] text-white/75 tabular-nums">
          {scoreDisplay}
        </span>
        <span className="text-[10px] text-white/25 ml-0.5">/100</span>
      </td>

      {/* Avg latency */}
      <td className="px-4 py-3 border-b border-white/[0.04] text-right">
        <span className="font-mono text-[13px] text-white/75 tabular-nums">
          {Math.round(result.avg_latency_ms)}
        </span>
        <span className="text-[10px] text-white/25 ml-0.5">ms</span>
      </td>

      {/* Cost */}
      <td className="px-4 py-3 border-b border-white/[0.04] text-right">
        <span className="font-mono text-[13px] text-white/75">
          ${result.total_cost_usd.toFixed(4)}
        </span>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Legacy single-scope fallback — preserves original flat card layout
// ---------------------------------------------------------------------------

function LegacyResults({ candidates }: { candidates: PipelineCandidate[] }) {
  const sorted = [...candidates]
    .filter((c) => c.test_status === "completed" && c.overall_score != null)
    .sort((a, b) => (b.overall_score ?? 0) - (a.overall_score ?? 0));

  if (sorted.length === 0) return null;

  return (
    <div data-testid="results-comparison" className="p-6 lg:p-8">
      <div className="mb-8">
        <h2 className="font-display text-[22px] text-[#f5f5f7] tracking-[-0.01em]">
          Evaluation Results
        </h2>
        <p className="text-[13px] text-[#6e6e73] mt-1 font-sans">
          {sorted.length} candidates tested &middot; ranked by performance
        </p>
      </div>

      {/* Side-by-side cards */}
      <div className="flex gap-4 overflow-x-auto pb-4 scrollbar-thin">
        {sorted.map((c, i) => (
          <ResultCard key={c.name} candidate={c} rank={i + 1} />
        ))}
      </div>

      {/* Recommendation */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.4 }}
        className="mt-8 rounded-lg border border-[hsl(220,14%,56%)]/15 bg-[hsl(220,14%,56%)]/[0.04] p-6"
      >
        <h3 className="font-grotesk font-medium text-[13px] tracking-[0.04em] text-[hsl(220,14%,62%)] mb-3">
          Recommendation
        </h3>
        <p className="text-[14px] text-[#a1a1a6] leading-[1.7] font-sans">
          Based on your workflow requirements,{" "}
          <span className="text-[#f5f5f7] font-medium">{sorted[0]?.name}</span>{" "}
          delivers the best performance score
          {sorted.length > 1 && (
            <>
              . Consider{" "}
              <span className="text-[#f5f5f7] font-medium">
                {[...sorted].sort((a, b) => (a.avg_latency_ms ?? 999) - (b.avg_latency_ms ?? 999))[0]?.name}
              </span>{" "}
              if latency is your primary concern
            </>
          )}
          .
        </p>
      </motion.div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Legacy ResultCard — original card component for single-scope fallback
// ---------------------------------------------------------------------------

function ResultCard({ candidate: c, rank }: { candidate: PipelineCandidate; rank: number }) {
  const [showTests, setShowTests] = useState(false);
  const score = Math.round((c.overall_score ?? 0) * 100);
  const isBest = rank === 1;

  return (
    <motion.div
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: rank * 0.12 }}
      className={`min-w-[280px] max-w-[320px] flex-shrink-0 rounded-lg border flex flex-col ${
        isBest
          ? "border-[hsl(220,14%,56%)]/30 bg-[hsl(220,14%,56%)]/[0.04]"
          : "border-[#2c2c2e] bg-[#1c1c1e]"
      }`}
    >
      {/* Header */}
      <div className="p-5 border-b border-[#2c2c2e]/50">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            {isBest && (
              <span className="text-[10px] font-grotesk tracking-[0.04em] px-2 py-0.5 rounded bg-[hsl(220,14%,56%)]/15 text-[hsl(220,14%,62%)] border border-[hsl(220,14%,56%)]/20">
                Best
              </span>
            )}
            <span className="text-[10px] font-mono text-[#48484a]">#{rank}</span>
          </div>
          {c.auth_method && (
            <span className="text-[9px] font-mono text-[#6e6e73] px-1.5 py-0.5 rounded bg-[#232325] border border-[#2c2c2e]">
              {c.auth_method}
            </span>
          )}
        </div>
        <h3 className="font-grotesk font-semibold text-[16px] text-[#f5f5f7] leading-tight">
          {c.name}
        </h3>
        <span className="text-[12px] text-[#6e6e73] font-sans">{c.provider}</span>
      </div>

      {/* Score */}
      <div className="p-5 flex flex-col items-center border-b border-[#2c2c2e]/50">
        <span className="font-display text-[48px] text-[#f5f5f7] leading-none tracking-[-0.02em]">
          {score}
        </span>
        <span className="text-[12px] text-[#6e6e73] mt-1.5 font-sans">/100 performance</span>
        <div className="w-full mt-4 h-[2px] bg-[#2c2c2e] rounded-full overflow-hidden">
          <motion.div
            className={`h-full rounded-full ${
              isBest
                ? "bg-gradient-to-r from-[hsl(220,14%,45%)] to-[hsl(220,20%,68%)]"
                : "bg-gradient-to-r from-[hsl(220,14%,40%)] to-[hsl(220,14%,58%)]"
            }`}
            initial={{ width: 0 }}
            animate={{ width: `${score}%` }}
            transition={{ duration: 1, delay: 0.4 }}
          />
        </div>
      </div>

      {/* Metrics */}
      <div className="p-5 grid grid-cols-3 gap-3 border-b border-[#2c2c2e]/50">
        <div className="text-center">
          <span className="text-[10px] font-grotesk tracking-[0.06em] text-[#6e6e73] block mb-1">Speed</span>
          <span className="font-mono text-[15px] text-[#f5f5f7]">{Math.round(c.avg_latency_ms ?? 0)}</span>
          <span className="text-[10px] text-[#48484a]">ms</span>
        </div>
        <div className="text-center">
          <span className="text-[10px] font-grotesk tracking-[0.06em] text-[#6e6e73] block mb-1">Cost</span>
          <span className="font-mono text-[15px] text-[#f5f5f7]">${(c.build_cost_usd ?? 0).toFixed(2)}</span>
        </div>
        <div className="text-center">
          <span className="text-[10px] font-grotesk tracking-[0.06em] text-[#6e6e73] block mb-1">Passed</span>
          <span className="font-mono text-[15px] text-[#f5f5f7]">
            {c.tests_passed}/{(c.tests_passed ?? 0) + (c.tests_failed ?? 0)}
          </span>
        </div>
      </div>

      {/* Description */}
      {c.description && (
        <div className="px-5 py-4 border-b border-[#2c2c2e]/50">
          <p className="text-[11px] text-white/40 leading-[1.6] font-sans line-clamp-3">
            {c.description}
          </p>
        </div>
      )}

      {/* Test details */}
      {c.test_results.length > 0 && (
        <div className="p-4">
          <button
            onClick={() => setShowTests(!showTests)}
            className="w-full text-[11px] font-sans text-[hsl(220,14%,56%)] hover:text-[hsl(220,14%,66%)] transition-colors flex items-center justify-center gap-1"
          >
            {showTests ? "Hide" : "View"} test details
            <span className="text-[9px]">{showTests ? "▾" : "▸"}</span>
          </button>
          {showTests && (
            <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} className="mt-3 border-t border-[#2c2c2e]">
              {c.test_results.map((tr) => (
                <TestResultRow key={tr.test_case_id} result={tr} />
              ))}
            </motion.div>
          )}
        </div>
      )}
    </motion.div>
  );
}
