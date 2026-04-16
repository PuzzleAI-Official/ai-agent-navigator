import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { PipelineCandidate, WorkflowStep } from "@/types/pipeline";
import { TestResultRow } from "./TestResultRow";
import { CoverageBadge } from "./CoverageBadge";
import { PricingBlock } from "./PricingBlock";

interface Props {
  candidate: PipelineCandidate;
  index: number;
  /**
   * Phase 4: when the run has a multi-scope blueprint, pass it so the card
   * can render a "covers K/N scopes" badge with role names. Omit for
   * single-scope / legacy runs — no badge shown.
   */
  workflowSteps?: WorkflowStep[];
}

export function CandidateCard({ candidate: c, index, workflowSteps }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [logExpanded, setLogExpanded] = useState(false);

  const isBuilding = c.harness_status === "building";
  const isBuilt = c.harness_status === "built";
  const isTesting = c.test_status === "running";
  const isDone = c.test_status === "completed";
  const isFailed = c.harness_status === "failed";
  const isActive = isBuilding || isTesting;
  const isPending = c.harness_status === "pending" && !c.auth_method;
  const isVerifiedOnly = c.auth_method && c.harness_status === "pending";

  const [descExpanded, setDescExpanded] = useState(false);

  // Early discovery stage (Agent 2 only) — compact row
  if (isPending) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, height: 0, marginBottom: 0 }}
        transition={{ delay: index * 0.04, duration: 0.25 }}
        className="rounded-xl bg-white/[0.02] border border-white/[0.05] hover:bg-white/[0.04] hover:border-white/[0.08] transition-all group overflow-hidden"
      >
        <div className="px-4 py-3.5 flex items-start gap-3.5">
          {/* Provider icon */}
          <div className="w-5 h-5 shrink-0 mt-0.5">
            <svg viewBox="0 0 20 20" fill="none" className="w-5 h-5">
              <path d="M10 2L3 6v8l7 4 7-4V6l-7-4z" stroke="rgba(255,255,255,0.15)" strokeWidth="1.2" strokeLinejoin="round" />
              <path d="M10 2v8m0 0l7-4m-7 4l-7-4m7 12v-8m7 0v8m-14-8v8" stroke="rgba(255,255,255,0.08)" strokeWidth="0.8" />
              <circle cx="10" cy="10" r="2" fill="rgba(255,255,255,0.12)" />
            </svg>
          </div>

          {/* Name + description */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="font-grotesk font-semibold text-[13px] text-white/85 truncate">
                {c.name}
              </h3>
              <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded shrink-0 ${
                c.adoption_difficulty === "easy"
                  ? "text-emerald-400/50 bg-emerald-400/5 border border-emerald-400/8"
                  : c.adoption_difficulty === "hard"
                  ? "text-amber-400/50 bg-amber-400/5 border border-amber-400/8"
                  : "text-white/25 bg-white/[0.03] border border-white/[0.05]"
              }`}>
                {c.adoption_difficulty}
              </span>
            </div>
            <p
              onClick={(e) => { e.stopPropagation(); if (c.description) setDescExpanded(!descExpanded); }}
              className={`text-[11px] text-white/30 font-sans mt-0.5 ${
                descExpanded ? "" : "truncate"
              } ${c.description ? "cursor-pointer hover:text-white/40" : ""}`}
            >
              {c.provider}
              {c.description && ` · ${c.description}`}
            </p>
          </div>

          {/* Scanning indicator */}
          <div className="flex items-center gap-1.5 shrink-0 mt-1">
            <motion.div
              className="w-1 h-1 rounded-full bg-blue-400/40"
              animate={{ opacity: [0.2, 0.8, 0.2] }}
              transition={{ duration: 2, repeat: Infinity, delay: index * 0.15 }}
            />
            <span className="text-[9px] text-white/20 font-mono">screening</span>
          </div>
        </div>

        {/* Phase 4: coverage badge — shown when the run has a blueprint
            and the candidate claims at least one scope. The badge surfaces
            the "covers K/N scopes" summary + claimed/verified status so the
            user sees coverage breadth at a glance, even in compact rows. */}
        {workflowSteps && workflowSteps.length > 0 && c.covers_step_ids.length > 0 && (
          <div className="px-4 pb-2">
            <CoverageBadge
              candidate={c}
              workflowSteps={workflowSteps}
              compact
            />
          </div>
        )}

        {/* Subtle bottom accent line */}
        <div className="h-[1px] bg-gradient-to-r from-transparent via-white/[0.04] to-transparent" />
      </motion.div>
    );
  }

  // Full card for verified / building / testing / done candidates
  return (
    <motion.div
      initial={{ opacity: 0, x: 16 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -16, height: 0 }}
      transition={{ delay: index * 0.06 }}
      className={`rounded-xl overflow-hidden transition-all ${
        isActive
          ? "bg-blue-500/[0.04] border border-blue-400/15"
          : isDone || isBuilt
          ? "bg-emerald-500/[0.04] border border-emerald-400/15"
          : "bg-white/[0.02] border border-white/[0.06]"
      }`}
    >
      <div className="p-4 lg:p-5">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <div
              className={`w-2 h-2 shrink-0 rounded-sm ${
                isActive
                  ? "bg-blue-400/80 animate-pulse"
                  : isDone
                  ? "bg-emerald-400/60"
                  : isFailed
                  ? "bg-red-400/80"
                  : "bg-white/15"
              }`}
              style={{ transform: "rotate(45deg)" }}
            />
            <div>
              <h3 className="font-grotesk font-semibold text-[14px] text-white/90">
                {c.name}
              </h3>
              <span className="text-[12px] text-white/35 font-sans">
                {c.provider} · {Math.round(c.relevance_score * 100)}% match
              </span>
            </div>
          </div>

          <div className="flex items-center gap-1.5">
            {c.auth_method && (
              <span className="text-[10px] font-grotesk tracking-[0.03em] px-2 py-0.5 rounded bg-emerald-400/8 text-emerald-400/70 border border-emerald-400/15">
                Verified
              </span>
            )}
            {isBuilt && (
              <span className="text-[10px] font-grotesk tracking-[0.03em] px-2 py-0.5 rounded bg-blue-400/8 text-blue-300/70 border border-blue-400/15">
                Built
              </span>
            )}
            {isBuilding && (
              <span className="text-[11px] font-sans text-blue-400/70 animate-pulse">Building...</span>
            )}
            {isTesting && (
              <span className="text-[11px] font-sans text-blue-400/70 animate-pulse">Testing...</span>
            )}
            {isFailed && (
              <span className="text-[11px] font-sans text-red-400/80">Failed</span>
            )}
          </div>
        </div>

        {/* Auth + access info */}
        {c.auth_method && !isDone && (
          <div className="mt-2.5 flex items-center gap-2">
            <span className="text-[10px] font-mono text-white/25 px-1.5 py-0.5 rounded bg-white/[0.03] border border-white/[0.05]">
              {c.auth_method}
            </span>
            {c.api_access_method && (
              <span className="text-[10px] font-mono text-white/25 px-1.5 py-0.5 rounded bg-white/[0.03] border border-white/[0.05]">
                {c.api_access_method}
              </span>
            )}
          </div>
        )}

        {c.confirmed_capabilities.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {c.confirmed_capabilities.slice(0, 3).map((cap, i) => (
              <span key={i} className="text-[10px] px-2 py-0.5 rounded bg-white/[0.03] border border-white/[0.05] text-white/40 font-sans">
                {cap}
              </span>
            ))}
            {c.confirmed_capabilities.length > 3 && (
              <span className="text-[10px] text-white/20">+{c.confirmed_capabilities.length - 3}</span>
            )}
          </div>
        )}

        {/* Phase 4: coverage badge for built/tested cards. Lives in its
            own row so role chips don't crowd the capability chips above. */}
        {workflowSteps && workflowSteps.length > 0 && c.covers_step_ids.length > 0 && (
          <div className="mt-3">
            <CoverageBadge candidate={c} workflowSteps={workflowSteps} />
          </div>
        )}

        {/* Phase 5: structured pricing block — null-safe. Invisible until
            Phase 6.5's 4B extraction populates pricing_breakdown. When
            data arrives, shows "From $X/mo" with expandable tier details
            and source links. */}
        {c.pricing_breakdown && (
          <div className="mt-3">
            <PricingBlock breakdown={c.pricing_breakdown} coversStepIds={c.covers_step_ids} workflowSteps={workflowSteps} />
          </div>
        )}

        {/* Build/test progress log — shown during building and testing */}
        {(isBuilding || isTesting) && c.buildLog.length > 0 && (
          <div className="mt-3">
            {/* Latest entry with fade */}
            <AnimatePresence mode="wait">
              <motion.div
                key={c.buildLog[c.buildLog.length - 1]}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.25 }}
                className="flex items-start gap-2 px-2.5 py-1.5 rounded bg-white/[0.03] border border-white/[0.04]"
              >
                <motion.span
                  className="w-1.5 h-1.5 mt-1.5 rounded-full bg-blue-400/60 shrink-0"
                  animate={{ opacity: [0.3, 1, 0.3] }}
                  transition={{ duration: 1.5, repeat: Infinity }}
                />
                <span className="text-[11px] text-white/70 font-sans leading-[1.5]">
                  {c.buildLog[c.buildLog.length - 1]}
                </span>
              </motion.div>
            </AnimatePresence>

            {/* Expand to see full log */}
            {c.buildLog.length > 1 && (
              <button
                onClick={() => setLogExpanded(!logExpanded)}
                className="mt-1.5 text-[10px] text-white/20 hover:text-white/40 transition-colors font-mono flex items-center gap-1"
              >
                <span>{logExpanded ? "▾" : "▸"}</span>
                {c.buildLog.length - 1} earlier
              </button>
            )}

            {logExpanded && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                className="mt-1 space-y-0.5 max-h-32 overflow-y-auto scrollbar-thin"
              >
                {c.buildLog.slice(0, -1).map((msg, i) => (
                  <div key={i} className="flex items-start gap-2 px-2.5 py-1">
                    <span className="text-[10px] text-white/15 mt-[1px] font-mono w-3 text-center">›</span>
                    <span className="text-[11px] text-white/30 font-sans">{msg}</span>
                  </div>
                ))}
              </motion.div>
            )}
          </div>
        )}

        {isBuilt && c.build_turns != null && (
          <div className="mt-2.5 text-[11px] text-white/30 font-mono">
            {c.build_turns} turns · {((c.build_cost_usd ?? 0) * 20).toFixed(2)} credits
          </div>
        )}

        {isDone && c.overall_score != null && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            className="mt-4 pt-4 border-t border-white/[0.06]"
          >
            <div className="grid grid-cols-3 gap-4">
              <div>
                <span className="text-[10px] font-grotesk tracking-[0.06em] text-white/35 block mb-1">Performance</span>
                <span className="font-display text-[22px] text-white/90">{Math.round(c.overall_score * 100)}</span>
                <span className="text-[11px] text-white/20 ml-0.5">/100</span>
              </div>
              <div>
                <span className="text-[10px] font-grotesk tracking-[0.06em] text-white/35 block mb-1">Speed</span>
                <span className="font-mono text-[18px] text-white/90">{Math.round(c.avg_latency_ms ?? 0)}</span>
                <span className="text-[11px] text-white/20 ml-0.5">ms</span>
              </div>
              <div>
                <span className="text-[10px] font-grotesk tracking-[0.06em] text-white/35 block mb-1">Cost</span>
                <span className="font-mono text-[18px] text-white/90">${(c.build_cost_usd ?? 0).toFixed(2)}</span>
              </div>
            </div>

            <div className="mt-3 flex items-center gap-2">
              <div className="flex-1 h-[2px] bg-white/[0.06] rounded-full overflow-hidden">
                <motion.div
                  className="h-full bg-gradient-to-r from-blue-500/50 to-blue-400/70"
                  initial={{ width: 0 }}
                  animate={{ width: `${Math.round(c.overall_score * 100)}%` }}
                  transition={{ duration: 1, delay: 0.2 }}
                />
              </div>
              <span className="text-[10px] font-mono text-white/30">
                {c.tests_passed}/{(c.tests_passed ?? 0) + (c.tests_failed ?? 0)} passed
              </span>
            </div>

            {c.test_results.length > 0 && (
              <button
                onClick={() => setExpanded(!expanded)}
                className="mt-3 text-[11px] font-sans text-blue-400/60 hover:text-blue-300/80 transition-colors flex items-center gap-1"
              >
                {expanded ? "Hide" : "View"} test details
                <span className="text-[9px]">{expanded ? "▾" : "▸"}</span>
              </button>
            )}
          </motion.div>
        )}
      </div>

      {expanded && c.test_results.length > 0 && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          className="border-t border-white/[0.06] bg-black/20"
        >
          {c.test_results.map((tr) => (
            <TestResultRow key={tr.test_case_id} result={tr} />
          ))}
        </motion.div>
      )}
    </motion.div>
  );
}
