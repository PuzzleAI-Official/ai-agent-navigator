// ============================================================================
// WorkflowDiagram — Phase 3 DAG surface for Agent 1's director output
// ============================================================================
// Renders the workflow blueprint as a topological-layer DAG above the
// candidate list. Each `depends_on` edge becomes an SVG arrow between
// nodes. Steps with no dependencies form the leftmost layer; downstream
// steps cascade right. Parallel siblings (same layer AND same
// `parallel_group`) cluster in a soft container so fan-outs read as
// intentional groups.
//
// Layering: longest-path from roots. A step's layer is
// `1 + max(layer of each depends_on dep)`, with empty `depends_on`
// steps at layer 0. Cycles should never reach us — Agent 1's validator
// blocks them — but the layering fallback treats any re-entry as layer 0
// so we still render SOMETHING rather than crashing.
//
// Edges: after layout we measure each node's bounding box against the
// SVG container and draw an arrow from the source's right-middle to the
// target's left-middle. ResizeObserver triggers a redraw on width changes
// (e.g., when the panel grows as candidate cards arrive beneath).
//
// Behavior:
//   - `blueprint === null` → renders nothing (pre-Phase-3 fallback).
//   - `blueprint.steps.length === 1` → single node, no edges (still shown
//     so the user can sanity-check Agent 1's interpretation).
//   - Multi-step linear chain → N layers of 1 node each (same visual
//     density as the old horizontal chain).
//   - Multi-step DAG → proper columns, parallel clusters, SVG edges.
// ============================================================================

import { motion } from "framer-motion";
import { useLayoutEffect, useMemo, useRef, useState } from "react";

import type { WorkflowBlueprint, WorkflowStep } from "@/types/pipeline";

interface WorkflowDiagramProps {
  blueprint: WorkflowBlueprint | null;
}

// ----------------------------------------------------------------------------
// Layout computation
// ----------------------------------------------------------------------------

interface LayerCluster {
  parallelGroup: string | null;
  steps: WorkflowStep[];
}

interface Layout {
  layers: LayerCluster[][]; // layerIdx -> clusters -> steps
  edges: Array<{ from: string; to: string }>;
  stepLayer: Map<string, number>;
  stepIndex: Map<string, number>; // presentation order (for node numbering)
}

function computeLayout(steps: WorkflowStep[]): Layout {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const layerCache = new Map<string, number>();
  const visiting = new Set<string>();

  function layerOf(id: string): number {
    const cached = layerCache.get(id);
    if (cached !== undefined) return cached;
    // Cycle fallback — shouldn't happen (validator blocks) but if an
    // invalid blueprint slipped through we still want to render.
    if (visiting.has(id)) return 0;
    const step = byId.get(id);
    if (!step) return 0;
    if (!step.depends_on || step.depends_on.length === 0) {
      layerCache.set(id, 0);
      return 0;
    }
    visiting.add(id);
    let max = 0;
    for (const dep of step.depends_on) {
      const l = layerOf(dep);
      if (l + 1 > max) max = l + 1;
    }
    visiting.delete(id);
    layerCache.set(id, max);
    return max;
  }

  const stepLayer = new Map<string, number>();
  for (const s of steps) stepLayer.set(s.id, layerOf(s.id));

  // Group steps by (layer, parallel_group). Within a layer, steps that
  // share the same non-null parallel_group cluster together; everything
  // else gets its own single-step cluster. Presentation order within a
  // cluster follows the original `steps[]` order for stability.
  const maxLayer = Math.max(0, ...Array.from(stepLayer.values()));
  const layers: LayerCluster[][] = Array.from({ length: maxLayer + 1 }, () => []);

  const clusterKey = (layer: number, group: string | null | undefined): string =>
    group ? `${layer}::${group}` : "";
  const clusterByKey = new Map<string, LayerCluster>();

  for (const s of steps) {
    const layer = stepLayer.get(s.id) ?? 0;
    const group = s.parallel_group ?? null;
    const key = clusterKey(layer, group);
    if (key) {
      const existing = clusterByKey.get(key);
      if (existing) {
        existing.steps.push(s);
        continue;
      }
      const cluster: LayerCluster = { parallelGroup: group, steps: [s] };
      clusterByKey.set(key, cluster);
      layers[layer].push(cluster);
    } else {
      layers[layer].push({ parallelGroup: null, steps: [s] });
    }
  }

  // Edges: one per depends_on entry (authoritative DAG). Deduped.
  const edges: Array<{ from: string; to: string }> = [];
  const seen = new Set<string>();
  for (const s of steps) {
    for (const dep of s.depends_on ?? []) {
      const k = `${dep}->${s.id}`;
      if (seen.has(k)) continue;
      seen.add(k);
      edges.push({ from: dep, to: s.id });
    }
  }

  // Presentation index = original steps[] order. Used only for "#1 / #2
  // / #3" badges on nodes so the user has a stable reference number.
  const stepIndex = new Map<string, number>();
  steps.forEach((s, i) => stepIndex.set(s.id, i));

  return { layers, edges, stepLayer, stepIndex };
}

// ----------------------------------------------------------------------------
// Component
// ----------------------------------------------------------------------------

export function WorkflowDiagram({ blueprint }: WorkflowDiagramProps) {
  // Hooks must run unconditionally; null-check is done before returning JSX.
  const containerRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef<Map<string, HTMLDivElement | null>>(new Map());

  const layout = useMemo<Layout | null>(() => {
    if (!blueprint || blueprint.steps.length === 0) return null;
    return computeLayout(blueprint.steps);
  }, [blueprint]);

  const [edgeSegments, setEdgeSegments] = useState<
    Array<{ id: string; d: string }>
  >([]);
  const [svgSize, setSvgSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });

  useLayoutEffect(() => {
    if (!layout || !containerRef.current) {
      setEdgeSegments([]);
      return;
    }
    const container = containerRef.current;

    const recompute = () => {
      const rect = container.getBoundingClientRect();
      const segments: Array<{ id: string; d: string }> = [];
      for (const edge of layout.edges) {
        const fromEl = nodeRefs.current.get(edge.from);
        const toEl = nodeRefs.current.get(edge.to);
        if (!fromEl || !toEl) continue;
        const fr = fromEl.getBoundingClientRect();
        const tr = toEl.getBoundingClientRect();
        // Source: right edge midpoint. Target: left edge midpoint.
        const x1 = fr.right - rect.left;
        const y1 = fr.top + fr.height / 2 - rect.top;
        const x2 = tr.left - rect.left;
        const y2 = tr.top + tr.height / 2 - rect.top;
        // Cubic Bezier with horizontal control points so edges curve
        // smoothly between columns rather than zig-zagging.
        const dx = Math.max(18, (x2 - x1) / 2);
        const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
        segments.push({ id: `${edge.from}->${edge.to}`, d });
      }
      setEdgeSegments(segments);
      setSvgSize({ w: rect.width, h: rect.height });
    };

    recompute();
    const ro = new ResizeObserver(recompute);
    ro.observe(container);
    // Re-run on node geometry changes too (panel widens as candidates arrive).
    for (const el of nodeRefs.current.values()) {
      if (el) ro.observe(el);
    }
    return () => ro.disconnect();
  }, [layout]);

  if (!blueprint || !layout || layout.layers.length === 0) return null;

  const stepCount = blueprint.steps.length;
  const hasFanOut = layout.layers.some((layer) => layer.length > 1);

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
            {stepCount} step{stepCount === 1 ? "" : "s"}
            {hasFanOut ? " · DAG" : ""}
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

      <div
        ref={containerRef}
        className="relative flex items-stretch gap-6 overflow-x-auto scrollbar-thin pb-2"
        data-testid="workflow-dag"
      >
        {/* SVG overlay for depends_on edges. Absolute so it shares the
            container's coordinate space; pointer-events-none so it never
            swallows clicks on the nodes underneath. */}
        <svg
          className="absolute inset-0 pointer-events-none"
          width={svgSize.w || "100%"}
          height={svgSize.h || "100%"}
          aria-hidden
        >
          <defs>
            <marker
              id="workflow-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
              fill="rgba(255,255,255,0.35)"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" />
            </marker>
          </defs>
          {edgeSegments.map((seg) => (
            <path
              key={seg.id}
              d={seg.d}
              stroke="rgba(255,255,255,0.18)"
              strokeWidth="1.25"
              fill="none"
              markerEnd="url(#workflow-arrow)"
            />
          ))}
        </svg>

        {layout.layers.map((clusters, layerIdx) => (
          <div
            key={`layer-${layerIdx}`}
            className="flex flex-col gap-3 shrink-0 min-w-[180px] max-w-[240px] relative z-[1]"
            data-testid={`workflow-layer-${layerIdx}`}
          >
            {clusters.map((cluster, clusterIdx) => (
              <ClusterContainer key={`c-${layerIdx}-${clusterIdx}`} cluster={cluster}>
                {cluster.steps.map((step) => (
                  <StepCard
                    key={step.id}
                    step={step}
                    index={layout.stepIndex.get(step.id) ?? 0}
                    registerRef={(el) => {
                      if (el) nodeRefs.current.set(step.id, el);
                      else nodeRefs.current.delete(step.id);
                    }}
                  />
                ))}
              </ClusterContainer>
            ))}
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

// ----------------------------------------------------------------------------
// Sub-components
// ----------------------------------------------------------------------------

function ClusterContainer({
  cluster,
  children,
}: {
  cluster: LayerCluster;
  children: React.ReactNode;
}) {
  // Solo steps render without a surrounding chrome; only intentional
  // fan-out clusters (parallel_group set AND >1 step) get the dashed
  // wrapper that signals "these run in parallel as a group."
  if (!cluster.parallelGroup || cluster.steps.length <= 1) {
    return <div className="flex flex-col gap-3">{children}</div>;
  }
  return (
    <div
      className="flex flex-col gap-2 p-2 rounded-md border border-dashed border-white/10 bg-white/[0.015]"
      title={`Parallel group: ${cluster.parallelGroup}`}
    >
      <div className="text-[9px] font-grotesk uppercase tracking-[0.1em] text-white/30 px-0.5">
        {cluster.parallelGroup.replace(/_/g, " ")}
      </div>
      {children}
    </div>
  );
}

function StepCard({
  step,
  index,
  registerRef,
}: {
  step: WorkflowStep;
  index: number;
  registerRef: (el: HTMLDivElement | null) => void;
}) {
  return (
    <div
      ref={registerRef}
      className="px-3 py-2 rounded-md bg-white/[0.04] border border-white/[0.08] hover:border-blue-400/30 transition-colors"
      data-testid={`workflow-step-${step.id}`}
      title={`Capability: ${step.capability}\nInput: ${step.input_from ?? "—"}\nOutput: ${step.output_format}${step.depends_on.length ? `\nDepends on: ${step.depends_on.join(", ")}` : ""}`}
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
