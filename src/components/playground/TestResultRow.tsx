import { useState } from "react";
import { motion } from "framer-motion";
import type { TestResult } from "@/types/pipeline";

interface Props {
  result: TestResult;
}

export function TestResultRow({ result }: Props) {
  const [expanded, setExpanded] = useState(false);
  const score = Math.round(result.weighted_score * 100);

  return (
    <div className="border-b border-[#2c2c2e]/50 last:border-0">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 px-3 py-2.5 hover:bg-[#1c1c1e]/50 transition-colors text-left"
      >
        <span className={`text-[11px] font-mono ${result.passed ? "text-[hsl(220,14%,62%)]" : "text-[#ff453a]"}`}>
          {result.passed ? "✓" : "✗"}
        </span>
        <span className="text-[12px] text-[#a1a1a6] flex-1 truncate font-sans">{result.test_case_id}</span>
        <span className="text-[13px] font-mono text-[#f5f5f7]">
          {score}<span className="text-[#48484a]">/100</span>
        </span>
        <span className="text-[11px] font-mono text-[#6e6e73]">{Math.round(result.latency_ms)}ms</span>
        <span className="text-[#48484a] text-[10px]">{expanded ? "▾" : "▸"}</span>
      </button>

      {expanded && result.criteria_scores.length > 0 && (
        <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} className="px-3 pb-3">
          <div className="ml-6 pl-3 border-l border-[#2c2c2e] space-y-2">
            {result.criteria_scores.map((cs, i) => (
              <div key={i} className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className={`text-[10px] font-mono ${cs.passed ? "text-[hsl(220,14%,62%)]" : "text-[#ff453a]"}`}>
                    {cs.passed ? "✓" : "✗"}
                  </span>
                  <span className="text-[12px] text-[#a1a1a6] flex-1 font-sans">{cs.criterion}</span>
                  <span className="text-[11px] font-mono text-[#6e6e73]">{Math.round(cs.score * 10)}/10</span>
                </div>
                {cs.reasoning && (
                  <p className="ml-5 text-[11px] text-[#6e6e73] leading-[1.5] font-sans italic">{cs.reasoning}</p>
                )}
              </div>
            ))}
          </div>
        </motion.div>
      )}
    </div>
  );
}
