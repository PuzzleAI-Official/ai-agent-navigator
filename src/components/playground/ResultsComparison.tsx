import { useState } from "react";
import { motion } from "framer-motion";
import type { PipelineCandidate } from "@/types/pipeline";
import { TestResultRow } from "./TestResultRow";

interface Props {
  candidates: PipelineCandidate[];
}

export function ResultsComparison({ candidates }: Props) {
  const sorted = [...candidates]
    .filter((c) => c.test_status === "completed" && c.overall_score != null)
    .sort((a, b) => (b.overall_score ?? 0) - (a.overall_score ?? 0));

  if (sorted.length === 0) return null;

  return (
    <div className="p-6 lg:p-8">
      <div className="mb-8">
        <h2 className="font-display text-[22px] text-[#f5f5f7] tracking-[-0.01em]">
          Evaluation Results
        </h2>
        <p className="text-[13px] text-[#6e6e73] mt-1 font-sans">
          {sorted.length} candidates tested · ranked by performance
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

      {/* Score — Instrument Serif */}
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
