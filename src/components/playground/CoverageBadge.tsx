// ============================================================================
// CoverageBadge — Phase 4 scope coverage surface on CandidateCard
// ============================================================================
// Shows "covers K/N scopes: ocr, extract, sync" for any candidate whose
// `covers_step_ids` is non-empty AND whose run has a blueprint.
//
// Each listed scope is rendered as a small chip with:
//   - the scope's role name (from the blueprint) for human readability
//   - a status dot: amber for "claimed" (Agent 2's guess from search snippets),
//     emerald for "verified" (confirmed from docs during selected-candidate verification).
//
// The compact variant (used inside the early-discovery row) drops the
// leading "covers" label and the role names — just shows a "K/N ✓" count
// + a mini legend — so the row stays one line tall.
//
// The full variant (used on the post-verify card) shows the role chips
// inline with status dots and an optional "unverified" hint below.
// ============================================================================

import type { CoverageConfidence, PipelineCandidate, WorkflowStep } from "@/types/pipeline";

interface Props {
  candidate: PipelineCandidate;
  workflowSteps: WorkflowStep[];
  /**
   * When true, render the condensed form suitable for the early-discovery
   * row (no role chips, tighter padding). Defaults to false (full form).
   */
  compact?: boolean;
}

export function CoverageBadge({ candidate, workflowSteps, compact }: Props) {
  if (candidate.covers_step_ids.length === 0 || workflowSteps.length === 0) {
    return null;
  }

  // Look up each covered step's role so the chip is human-readable.
  // Preserve blueprint order (steps[]), not the candidate's covers order,
  // so cards render in the same scope sequence Agent 1 designed.
  const stepsById = new Map(workflowSteps.map((s) => [s.id, s]));
  const coveredInOrder = workflowSteps.filter((s) =>
    candidate.covers_step_ids.includes(s.id)
  );

  const totalScopes = workflowSteps.length;
  const coveredCount = candidate.covers_step_ids.length;

  // Aggregate confidence across all covered scopes. If any scope is still
  // "claimed", the badge as a whole is "claimed"-tinted (amber). When
  // every scope is "verified" (verification complete), the badge is emerald.
  const anyClaimed = Object.values(candidate.coverage_confidence).some(
    (conf) => conf === "claimed"
  );
  const anyVerified = Object.values(candidate.coverage_confidence).some(
    (conf) => conf === "verified"
  );
  const aggStatus: CoverageConfidence =
    anyClaimed || !anyVerified ? "claimed" : "verified";

  if (compact) {
    return (
      <div
        className="inline-flex items-center gap-1.5 text-[9px] font-mono text-white/35"
        title={_buildTooltip(candidate, coveredInOrder)}
        data-testid="coverage-badge-compact"
      >
        <span className="inline-block w-1 h-1 rounded-full"
          style={{
            background: aggStatus === "verified"
              ? "rgba(16, 185, 129, 0.75)"
              : "rgba(245, 158, 11, 0.7)",
          }}
        />
        <span>
          covers {coveredCount}/{totalScopes} scope{totalScopes === 1 ? "" : "s"}
        </span>
      </div>
    );
  }

  return (
    <div
      className="flex flex-col gap-1.5"
      data-testid="coverage-badge"
      title={_buildTooltip(candidate, coveredInOrder)}
    >
      <div className="flex items-center gap-1.5 text-[10px] font-grotesk uppercase tracking-[0.08em] text-white/35">
        <span>
          covers {coveredCount}/{totalScopes}
        </span>
        {aggStatus === "claimed" && (
          <span className="text-[9px] text-amber-400/60 font-mono normal-case tracking-normal">
            · claimed, awaiting verify
          </span>
        )}
        {aggStatus === "verified" && (
          <span className="text-[9px] text-emerald-400/70 font-mono normal-case tracking-normal">
            · verified
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {coveredInOrder.map((step) => {
          const conf = candidate.coverage_confidence[step.id] ?? "claimed";
          const isVerified = conf === "verified";
          return (
            <span
              key={step.id}
              className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded border font-sans ${
                isVerified
                  ? "text-emerald-300/80 bg-emerald-400/5 border-emerald-400/15"
                  : "text-amber-200/75 bg-amber-400/5 border-amber-400/15"
              }`}
              title={`${step.id} · ${step.role} · ${conf}`}
              data-testid={`coverage-chip-${step.id}`}
            >
              <span
                className="w-1 h-1 rounded-full"
                style={{
                  background: isVerified
                    ? "rgba(16, 185, 129, 0.9)"
                    : "rgba(245, 158, 11, 0.85)",
                }}
              />
              {step.role}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function _buildTooltip(
  candidate: PipelineCandidate,
  coveredSteps: WorkflowStep[]
): string {
  const lines = coveredSteps.map((s) => {
    const conf = candidate.coverage_confidence[s.id] ?? "claimed";
    return `${s.id} (${s.role}): ${conf}`;
  });
  return `Coverage for ${candidate.name}\n${lines.join("\n")}`;
}
