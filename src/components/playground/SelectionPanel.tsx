// ============================================================================
// SelectionPanel — Phase 6 per-scope candidate picking
// ============================================================================
// Shown when the pipeline pauses with `selection_required` SSE event.
// One column per scope. Each column lists candidates that claimed
// coverage at that scope (from Agent 2's dual search). User toggles
// keep/remove checkboxes per candidate per scope. An "Add provider"
// form at the bottom lets users inject custom providers with explicit
// scope coverage. Submit button fires the `submitSelection` handler
// which POSTs to /runs/{id}/select-candidates.
//
// Coverage confidence dots: amber ⦿ = claimed (Agent 2 search-only),
// emerald ✓ = verified (Phase 6.5 — won't appear until then).
//
// All candidates default to SELECTED (keep=true). Users deselect what
// they don't want tested — opt-out, not opt-in, because most Agent 2
// candidates are relevant and "keep all" is the expected happy path.
// ============================================================================

import { useState, useMemo, useCallback } from "react";
import { motion } from "framer-motion";
import type {
  PipelineCandidate,
  WorkflowStep,
  UserAddedCandidate,
} from "@/types/pipeline";

interface Props {
  candidates: PipelineCandidate[];
  workflowSteps: WorkflowStep[];
  perScopeCandidates: Record<string, string[]>;
  /**
   * Phase 7's top-K programmatic picks per scope, sent alongside
   * ``perScopeCandidates`` in the ``selection_required`` SSE payload. The
   * panel pre-checks these (not all candidates) so the user starts from
   * the smart default — a weighted rank across user_picked / credentials
   * / relevance / docs / pricing_fit. Falls back to all-checked when the
   * backend doesn't provide picks (e.g. legacy pipelines, feature flag off).
   */
  defaultPicks?: Record<string, string[]>;
  onSubmit: (
    scopePicks: Record<string, string[]>,
    userAdded: UserAddedCandidate[]
  ) => void;
  isSubmitting: boolean;
}

export function SelectionPanel({
  candidates,
  workflowSteps,
  perScopeCandidates,
  defaultPicks,
  onSubmit,
  isSubmitting,
}: Props) {
  // Initial selections: Phase 7's default_picks when provided (top-K from
  // the weighted scorer), else fall back to "all candidates checked" so
  // legacy pipelines behave as before.
  const [scopeSelections, setScopeSelections] = useState<Record<string, Set<string>>>(() => {
    const initial: Record<string, Set<string>> = {};
    for (const [scopeId, names] of Object.entries(perScopeCandidates)) {
      const picks = defaultPicks?.[scopeId];
      if (picks && picks.length > 0) {
        // Intersect Phase 7 picks with the available candidate list so we
        // don't pre-check a name the user can't see.
        const allowed = new Set(names);
        initial[scopeId] = new Set(picks.filter((p) => allowed.has(p)));
        // If the intersection is empty (picks went stale) fall back to all.
        if (initial[scopeId].size === 0) {
          initial[scopeId] = new Set(names);
        }
      } else {
        initial[scopeId] = new Set(names);
      }
    }
    return initial;
  });

  // State: user-added providers
  const [userAdded, setUserAdded] = useState<UserAddedCandidate[]>([]);
  const [addFormOpen, setAddFormOpen] = useState(false);
  const [addName, setAddName] = useState("");
  const [addProvider, setAddProvider] = useState("");
  const [addDocsUrl, setAddDocsUrl] = useState("");
  const [addNotes, setAddNotes] = useState("");
  const [addScopes, setAddScopes] = useState<Set<string>>(new Set());

  // Candidate lookup for display info
  const candidateByName = useMemo(() => {
    const map = new Map<string, PipelineCandidate>();
    for (const c of candidates) map.set(c.name, c);
    return map;
  }, [candidates]);

  const toggleCandidate = useCallback(
    (scopeId: string, candidateName: string) => {
      setScopeSelections((prev) => {
        const next = { ...prev };
        const set = new Set(prev[scopeId] || []);
        if (set.has(candidateName)) {
          set.delete(candidateName);
        } else {
          set.add(candidateName);
        }
        next[scopeId] = set;
        return next;
      });
    },
    []
  );

  const handleAddProvider = useCallback(() => {
    if (!addName.trim() || addScopes.size === 0) return;
    const ua: UserAddedCandidate = {
      name: addName.trim(),
      provider: addProvider.trim() || addName.trim(),
      api_docs_url: addDocsUrl.trim() || null,
      notes: addNotes.trim() || null,
      covers_step_ids: Array.from(addScopes),
    };
    setUserAdded((prev) => [...prev, ua]);
    // Also add to scope selections so the submit includes them
    setScopeSelections((prev) => {
      const next = { ...prev };
      for (const sid of addScopes) {
        const set = new Set(prev[sid] || []);
        set.add(ua.name);
        next[sid] = set;
      }
      return next;
    });
    // Reset form
    setAddName("");
    setAddProvider("");
    setAddDocsUrl("");
    setAddNotes("");
    setAddScopes(new Set());
    setAddFormOpen(false);
  }, [addName, addProvider, addDocsUrl, addNotes, addScopes]);

  const handleSubmit = useCallback(() => {
    const scopePicks: Record<string, string[]> = {};
    for (const [scopeId, nameSet] of Object.entries(scopeSelections)) {
      scopePicks[scopeId] = Array.from(nameSet);
    }
    onSubmit(scopePicks, userAdded);
  }, [scopeSelections, userAdded, onSubmit]);

  // Summary stats
  const totalSelected = new Set(
    Object.values(scopeSelections).flatMap((s) => Array.from(s))
  ).size;
  const totalAvailable = new Set(
    Object.values(perScopeCandidates).flat()
  ).size;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="p-6 lg:p-8"
      data-testid="selection-panel"
    >
      <div className="flex items-center justify-between mb-5">
        <div>
          <h2 className="font-display text-[18px] text-white/90 tracking-[-0.01em]">
            Select Candidates
          </h2>
          <p className="text-[12px] text-white/30 mt-0.5 font-sans">
            Choose which candidates to test at each scope. Uncheck to remove.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[11px] font-mono text-white/40">
            {totalSelected}/{totalAvailable} selected
          </span>
          <button
            onClick={handleSubmit}
            disabled={isSubmitting || totalSelected === 0}
            className="px-4 py-2 rounded-lg bg-blue-500/20 text-blue-300/90 text-[12px] font-grotesk font-semibold uppercase tracking-[0.06em] border border-blue-400/25 hover:bg-blue-500/30 hover:border-blue-400/40 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
            data-testid="selection-submit"
          >
            {isSubmitting ? "Submitting..." : "Start Testing"}
          </button>
        </div>
      </div>

      {/* Per-scope columns */}
      <div className="flex gap-4 overflow-x-auto scrollbar-thin pb-2">
        {workflowSteps.map((step) => {
          const candidatesAtScope = perScopeCandidates[step.id] || [];
          const selectedAtScope = scopeSelections[step.id] || new Set();
          return (
            <div
              key={step.id}
              className="min-w-[220px] max-w-[280px] shrink-0 rounded-lg border border-white/[0.08] bg-white/[0.02] overflow-hidden"
              data-testid={`selection-scope-${step.id}`}
            >
              {/* Scope header */}
              <div className="px-3 py-2 border-b border-white/[0.06] bg-white/[0.03]">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-grotesk font-semibold uppercase tracking-[0.08em] text-blue-300/80">
                    {step.role}
                  </span>
                  <span className="text-[9px] font-mono text-white/30">
                    {selectedAtScope.size}/{candidatesAtScope.length}
                  </span>
                </div>
                <div className="text-[10px] text-white/25 font-mono mt-0.5">
                  {step.id}
                </div>
              </div>

              {/* Candidate list */}
              <div className="px-2 py-2 space-y-1">
                {candidatesAtScope.length === 0 && (
                  <p className="text-[11px] text-white/20 italic px-1 py-2">
                    No candidates cover this scope
                  </p>
                )}
                {candidatesAtScope.map((name) => {
                  const isSelected = selectedAtScope.has(name);
                  const cand = candidateByName.get(name);
                  const confidence = cand?.coverage_confidence?.[step.id];
                  return (
                    <label
                      key={name}
                      className={`flex items-start gap-2 px-2 py-1.5 rounded cursor-pointer transition-colors ${
                        isSelected
                          ? "bg-blue-500/[0.06] hover:bg-blue-500/[0.1]"
                          : "bg-white/[0.01] hover:bg-white/[0.03] opacity-50"
                      }`}
                      data-testid={`selection-item-${step.id}-${name}`}
                    >
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => toggleCandidate(step.id, name)}
                        className="mt-0.5 accent-blue-400"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span
                            className="w-1.5 h-1.5 rounded-full shrink-0"
                            style={{
                              background:
                                confidence === "verified"
                                  ? "rgba(16,185,129,0.8)"
                                  : "rgba(245,158,11,0.7)",
                            }}
                          />
                          <span className="text-[11px] text-white/80 font-sans truncate">
                            {name}
                          </span>
                        </div>
                        {cand && (
                          <span className="text-[9px] text-white/25 font-sans mt-0.5 block truncate">
                            {cand.provider} ·{" "}
                            {Math.round((cand.relevance_score || 0) * 100)}%
                          </span>
                        )}
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>

      {/* Add custom provider form */}
      <div className="mt-5">
        {!addFormOpen ? (
          <button
            onClick={() => setAddFormOpen(true)}
            className="text-[11px] font-grotesk text-blue-400/60 hover:text-blue-300/80 transition-colors flex items-center gap-1.5"
            data-testid="selection-add-toggle"
          >
            <span className="text-[14px]">+</span> Add custom provider
          </button>
        ) : (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            className="p-4 rounded-lg border border-white/[0.08] bg-white/[0.02] space-y-3"
            data-testid="selection-add-form"
          >
            <div className="text-[11px] font-grotesk font-semibold uppercase tracking-[0.08em] text-white/50 mb-2">
              Add Custom Provider
            </div>
            <div className="grid grid-cols-2 gap-2">
              <input
                placeholder="Provider name *"
                value={addName}
                onChange={(e) => setAddName(e.target.value)}
                className="col-span-1 px-3 py-2 text-[12px] bg-white/[0.04] border border-white/[0.08] rounded text-white/80 placeholder:text-white/20 outline-none focus:border-blue-400/30"
              />
              <input
                placeholder="Company name"
                value={addProvider}
                onChange={(e) => setAddProvider(e.target.value)}
                className="col-span-1 px-3 py-2 text-[12px] bg-white/[0.04] border border-white/[0.08] rounded text-white/80 placeholder:text-white/20 outline-none focus:border-blue-400/30"
              />
            </div>
            <input
              placeholder="API docs URL (optional)"
              value={addDocsUrl}
              onChange={(e) => setAddDocsUrl(e.target.value)}
              className="w-full px-3 py-2 text-[12px] bg-white/[0.04] border border-white/[0.08] rounded text-white/80 placeholder:text-white/20 outline-none focus:border-blue-400/30"
            />
            <input
              placeholder="Notes (optional)"
              value={addNotes}
              onChange={(e) => setAddNotes(e.target.value)}
              className="w-full px-3 py-2 text-[12px] bg-white/[0.04] border border-white/[0.08] rounded text-white/80 placeholder:text-white/20 outline-none focus:border-blue-400/30"
            />
            <div>
              <div className="text-[10px] font-grotesk uppercase tracking-[0.08em] text-white/35 mb-1.5">
                Covers scopes *
              </div>
              <div className="flex flex-wrap gap-2">
                {workflowSteps.map((step) => {
                  const isChecked = addScopes.has(step.id);
                  return (
                    <label
                      key={step.id}
                      className={`inline-flex items-center gap-1.5 px-2 py-1 rounded text-[11px] cursor-pointer transition-colors ${
                        isChecked
                          ? "bg-blue-500/15 text-blue-300/80 border border-blue-400/25"
                          : "bg-white/[0.03] text-white/40 border border-white/[0.06]"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => {
                          setAddScopes((prev) => {
                            const next = new Set(prev);
                            if (next.has(step.id)) next.delete(step.id);
                            else next.add(step.id);
                            return next;
                          });
                        }}
                        className="sr-only"
                      />
                      {step.role}
                    </label>
                  );
                })}
              </div>
            </div>
            <div className="flex items-center gap-2 pt-1">
              <button
                onClick={handleAddProvider}
                disabled={!addName.trim() || addScopes.size === 0}
                className="px-3 py-1.5 rounded bg-emerald-500/15 text-emerald-300/80 text-[11px] font-grotesk font-semibold border border-emerald-400/20 hover:bg-emerald-500/25 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
              >
                Add
              </button>
              <button
                onClick={() => setAddFormOpen(false)}
                className="px-3 py-1.5 text-[11px] text-white/30 hover:text-white/50 transition-colors"
              >
                Cancel
              </button>
            </div>
          </motion.div>
        )}

        {/* Show user-added providers */}
        {userAdded.length > 0 && (
          <div className="mt-3 space-y-1">
            {userAdded.map((ua, i) => (
              <div
                key={`${ua.name}-${i}`}
                className="flex items-center gap-2 px-3 py-1.5 rounded bg-emerald-500/[0.04] border border-emerald-400/10 text-[11px]"
                data-testid={`user-added-${ua.name}`}
              >
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400/60 shrink-0" />
                <span className="text-white/70 font-sans">{ua.name}</span>
                <span className="text-white/25 font-mono text-[9px]">
                  covers {ua.covers_step_ids.join(", ")}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </motion.div>
  );
}
