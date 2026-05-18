import { useRef, useEffect, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { ActivityEntry } from "@/types/activity";
import { AGENT_LABELS } from "@/types/activity";

interface Props {
  entries: ActivityEntry[];
}

interface Section {
  agentId: string;
  label: string;
  candidateLabel?: string;
  cost?: number;
  entries: ActivityEntry[];
  isComplete: boolean;
}

export function ActivityFeed({ entries }: Props) {
  const [manualExpanded, setManualExpanded] = useState<Set<number>>(new Set());
  const feedRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);

  // Smart scroll: only auto-scroll if user is already at the bottom
  const handleScroll = useCallback(() => {
    const el = feedRef.current;
    if (!el) return;
    const threshold = 60;
    isAtBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  }, []);

  useEffect(() => {
    if (isAtBottomRef.current && feedRef.current) {
      feedRef.current.scrollTo({ top: feedRef.current.scrollHeight, behavior: "smooth" });
    }
  }, [entries.length]);

  // Group entries by agent sections
  const sections: Section[] = [];
  let currentSection: Section | null = null;

  for (const entry of entries) {
    if (
      entry.type === "agent_start" ||
      !currentSection ||
      currentSection.agentId !== entry.agentId
    ) {
      currentSection = {
        agentId: entry.agentId,
        label: entry.agentLabel || AGENT_LABELS[entry.agentId] || entry.agentId,
        candidateLabel:
          entry.candidateName && entry.agentId === "agent_5" ? entry.candidateName : undefined,
        entries: [],
        isComplete: false,
      };
      sections.push(currentSection);
    }
    currentSection.entries.push(entry);
    if (entry.type === "agent_complete" || entry.type === "pipeline_complete") {
      currentSection.isComplete = true;
      if (entry.cost != null) currentSection.cost = entry.cost;
    }
  }

  // Filter out empty sections and Agent 5 per-candidate entries (shown on cards instead)
  const filteredSections = sections.filter((s) => {
    const nonStart = s.entries.filter((e) => e.type !== "agent_start");
    if (nonStart.length === 0) return false; // Remove empty sections
    return true;
  });

  const toggleSection = (index: number) => {
    setManualExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  // ALL sections collapsed by default. Only manually expanded.
  const isExpanded = (index: number): boolean => manualExpanded.has(index);
  const displaySections = filteredSections;

  return (
    <div className="flex flex-col h-full">
      <div
        ref={feedRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto px-3 py-3 space-y-1.5 scrollbar-thin"
      >
        {displaySections.map((section, si) => {
          const visibleEntries = section.entries.filter((e) => e.type !== "agent_start");
          const expanded = isExpanded(si);
          const isActive = si === displaySections.length - 1 && !section.isComplete;
          // Get the latest entry for the single-line display
          const latestEntry = visibleEntries[visibleEntries.length - 1];
          const summaryEntry = [...visibleEntries].reverse().find(
            (e) => e.status === "success" || e.status === "failure"
          );
          const displayEntry = section.isComplete ? summaryEntry : latestEntry;

          return (
            <motion.div
              key={`${section.agentId}-${si}`}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2 }}
              className={`rounded-xl overflow-hidden backdrop-blur-sm transition-all ${
                isActive
                  ? "bg-white/[0.04] border border-white/[0.08]"
                  : "bg-white/[0.02] border border-white/[0.04]"
              }`}
            >
              {/* Section header */}
              <button
                onClick={() => toggleSection(si)}
                className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-white/[0.03] transition-colors"
              >
                <div className="flex items-center gap-2.5">
                  <span className="text-[11px] text-white/20 font-mono">
                    {expanded ? "▾" : "▸"}
                  </span>
                  <span className="text-[12px] font-grotesk font-medium tracking-[0.02em] text-white/70">
                    {section.label}
                    {section.candidateLabel && (
                      <span className="text-white/30 ml-1">— {section.candidateLabel}</span>
                    )}
                  </span>
                  {section.isComplete && (
                    <span className="text-[11px] text-emerald-400/80">✓</span>
                  )}
                  {isActive && (
                    <motion.span
                      className="w-1.5 h-1.5 rounded-full bg-blue-400/70"
                      animate={{ opacity: [0.3, 1, 0.3] }}
                      transition={{ duration: 1.5, repeat: Infinity }}
                    />
                  )}
                </div>
                <div className="flex items-center gap-2">
                  {!expanded && visibleEntries.length > 1 && (
                    <span className="text-[10px] text-white/15 font-mono">
                      {visibleEntries.length}
                    </span>
                  )}
                  {section.cost != null && (
                    <span className="text-[10px] font-mono text-white/30">
                      ${section.cost.toFixed(2)}
                    </span>
                  )}
                </div>
              </button>

              {/* Collapsed: show only 1 latest entry with fade */}
              {!expanded && displayEntry && (
                <div className="px-4 pb-3">
                  <AnimatePresence mode="wait">
                    <motion.div
                      key={displayEntry.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -4 }}
                      transition={{ duration: 0.3 }}
                      className="flex items-start gap-2.5"
                    >
                      <span className={`text-[12px] shrink-0 mt-[1px] font-mono w-3 text-center ${
                        displayEntry.status === "success" ? "text-emerald-400/80"
                          : displayEntry.status === "failure" ? "text-red-400/80"
                          : "text-white/40"
                      }`}>
                        {displayEntry.status === "success" ? "✓"
                          : displayEntry.status === "failure" ? "✗"
                          : "›"}
                      </span>
                      <span className="text-[13px] leading-[1.5] font-sans text-white/90">
                        {displayEntry.summary}
                      </span>
                    </motion.div>
                  </AnimatePresence>
                </div>
              )}

              {/* Expanded: show all entries */}
              {expanded && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.2 }}
                  className="px-3 pb-3 border-t border-white/[0.04]"
                >
                  {visibleEntries.map((entry) => (
                    <motion.div
                      key={entry.id}
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      className="flex items-start gap-2.5 py-1 px-1"
                    >
                      <span className={`text-[12px] shrink-0 mt-[1px] font-mono w-3 text-center ${
                        entry.status === "success" ? "text-emerald-400/80"
                          : entry.status === "failure" ? "text-red-400/80"
                          : "text-white/40"
                      }`}>
                        {entry.status === "success" ? "✓"
                          : entry.status === "failure" ? "✗"
                          : "›"}
                      </span>
                      <span className="text-[13px] leading-[1.5] font-sans text-white/90">
                        {entry.summary}
                      </span>
                    </motion.div>
                  ))}
                </motion.div>
              )}
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}
