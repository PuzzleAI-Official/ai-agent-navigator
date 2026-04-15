// ============================================================================
// WorkflowDiagram — Phase 3 surface for Agent 1's director output
// ============================================================================
// Renders the ordered workflow blueprint as a horizontal step chain, shown
// above the candidate list once the pipeline starts. Each step card shows
// role + description; arrows between cards encode `input_from` / `depends_on`.
//
// Behavior:
//   - `blueprint === null` → renders nothing (pre-Phase-3 fallback).
//   - `blueprint.steps.length === 1` → renders a single card (still useful
//     so the user confirms Agent 1's interpretation).
//   - `blueprint.steps.length >= 2` → renders a horizontal chain with arrows.
//
// Notes tooltip surfaces Agent 1's reasoning so the user can sanity-check
// the decomposition. architecture_options are shown as badges on the
// container for quick visual scanning (all_in_one / best_per_step).
//
// Deliberately light on visual polish — Phase 6 will rework this into an
// editable SelectionPanel. For now we just need the user to SEE the
// workflow so the demo is coherent.
// ============================================================================

import { motion } from "framer-motion";

import type { WorkflowBlueprint, WorkflowStep } from "@/types/pipeline";

interface WorkflowDiagramProps {
  blueprint: WorkflowBlueprint | null;
}

export function WorkflowDiagram({ blueprint }: WorkflowDiagramProps) {
  if (!blueprint || blueprint.steps.length === 0) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      className="px-6 lg:px-8 pt-5 pb-3 border-b border-white/[0.06] bg-white/[0.02]"
      data-testid="workflow-diagram"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-grotesk font-semibold uppercase tracking-[0.1em] text-white/45">
            Workflow
          </span>
          <span
            className="text-[10px] font-mono text-white/30"
            title={blueprint.notes || undefined}
          >
            {blueprint.steps.length} step{blueprint.steps.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {blueprint.architecture_options.map((opt) => (
            <span
              key={opt}
              className="text-[9px] font-grotesk font-medium uppercase tracking-[0.08em] px-2 py-0.5 rounded border border-white/[0.06] text-white/40"
            >
              {opt.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      </div>

      <div className="flex items-stretch gap-2 overflow-x-auto scrollbar-thin pb-1">
        {blueprint.steps.map((step, idx) => (
          <div key={step.id} className="flex items-center gap-2 shrink-0">
            <StepCard step={step} index={idx} />
            {idx < blueprint.steps.length - 1 && <StepArrow />}
          </div>
        ))}
      </div>

      {blueprint.notes && (
        <p className="mt-3 text-[11px] text-white/35 leading-relaxed max-w-3xl italic">
          {blueprint.notes}
        </p>
      )}
    </motion.div>
  );
}

function StepCard({ step, index }: { step: WorkflowStep; index: number }) {
  return (
    <div
      className="min-w-[160px] max-w-[220px] px-3 py-2 rounded-md bg-white/[0.04] border border-white/[0.08] hover:border-blue-400/30 transition-colors"
      title={`Capability: ${step.capability}\nInput: ${step.input_from ?? "—"}\nOutput: ${step.output_format}`}
    >
      <div className="flex items-center gap-1.5 mb-1">
        <span className="text-[9px] font-mono text-white/35">#{index + 1}</span>
        <span className="text-[10px] font-grotesk font-semibold uppercase tracking-[0.08em] text-blue-300/80">
          {step.role}
        </span>
      </div>
      <div className="text-[12px] text-white/75 leading-snug line-clamp-2">
        {step.description}
      </div>
    </div>
  );
}

function StepArrow() {
  return (
    <div className="flex items-center px-1 text-white/25" aria-hidden>
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path
          d="M3 8h10M9 4l4 4-4 4"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
}
