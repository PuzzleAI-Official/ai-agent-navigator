import { useState, useCallback, useRef, useEffect } from "react";
import type {
  Stage,
  Message,
  PipelineCandidate,
  PipelineProgress,
  SSEEventData,
  AgentModes,
  WorkflowBlueprint,
  CoverageConfidence,
  SelectCandidatesRequest,
  UserAddedCandidate,
  RejectionEntry,
} from "@/types/pipeline";
import type { ActivityEntry, PipelineNodeState } from "@/types/activity";
import { AGENT_LABELS } from "@/types/activity";
import {
  createRun,
  sendMessage as apiSendMessage,
  uploadFiles,
  cancelRun as apiCancelRun,
  selectCandidates as apiSelectCandidates,
  subscribeToEvents,
} from "@/services/api";

const WELCOME_MESSAGE: Message = {
  id: 0,
  role: "assistant",
  content:
    "Welcome to PuzzleAI. Describe the workflow or task you need an AI solution for, and I'll find the best candidates for you.",
};

const DEFAULT_MODES: AgentModes = {
  agent1: "real",
  agent2: "real",
  agent3: "real",
  agent4: "real",
  agent5: "real",
};

// Agent 1 is no longer "completed" by the time pipeline_started fires.
// Under the split-Agent-1 design (intent classifier in chat handler →
// heavy planner runs inside the pipeline), Agent 1 is the FIRST agent
// the pipeline view should highlight as active. The backend emits
// agent_started: agent_1 ("Designing workflow architecture") immediately
// after pipeline_started; the existing agent_started SSE handler then
// flips this node from pending → active.
const INITIAL_NODES: PipelineNodeState[] = [
  { agentId: "agent_1", label: "Design", status: "pending" },
  { agentId: "agent_2", label: "Research", status: "pending" },
  { agentId: "agent_3", label: "Test Cases", status: "pending" },
  { agentId: "agent_4", label: "Screen", status: "pending" },
  { agentId: "agent_5", label: "Build + Test", status: "pending" },
];

let activityCounter = 0;

// Monotonic counter for message IDs.
//
// Earlier the hook used ``id: Date.now()`` as the React key for every
// chat message it appended. With the new broadcast-history SSE bus, a
// fresh subscriber receives multiple state-setting events (pipeline_started,
// workflow_blueprint, test_data_sufficiency, coverage_gap, evaluation_report)
// in the same tick — all Date.now() calls collide on the same ms. React
// 18 then fires "Encountered two children with the same key" and SILENTLY
// omits one of the duplicates, which was the root cause of
// SelectionPanel never rendering: the stage-setting event landed in a
// duplicate-key branch and React dropped it.
//
// A monotonic counter is O(1) and collision-free forever.
let messageIdCounter = 1;
function nextMessageId(): number {
  return messageIdCounter++;
}

function makeActivity(
  agentId: string,
  type: ActivityEntry["type"],
  summary: string,
  extra?: Partial<ActivityEntry>
): ActivityEntry {
  return {
    id: `act-${++activityCounter}`,
    timestamp: Date.now(),
    agentId,
    agentLabel: AGENT_LABELS[agentId] || agentId,
    type,
    summary,
    status: "info",
    ...extra,
  };
}

function activityDedupeKey(entry: Pick<ActivityEntry, "agentId" | "type" | "summary" | "candidateName" | "status">): string {
  return [
    entry.agentId,
    entry.type,
    entry.candidateName ?? "",
    entry.status ?? "",
    entry.summary,
  ].join("::");
}

export function usePipelineRun(agentModes: AgentModes = DEFAULT_MODES) {
  const [stage, setStage] = useState<Stage>("conversation");
  const [runId, setRunId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([WELCOME_MESSAGE]);
  const [candidates, setCandidates] = useState<PipelineCandidate[]>([]);
  const [pipelineProgress, setPipelineProgress] = useState<PipelineProgress>({
    current_agent: null,
    agents_completed: [],
    harnesses_building: [],
    harnesses_completed: [],
    harnesses_failed: [],
    test_progress: 0,
  });
  const [costAccumulator, setCostAccumulator] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [activityEntries, setActivityEntries] = useState<ActivityEntry[]>([]);
  const [pipelineNodes, setPipelineNodes] = useState<PipelineNodeState[]>([]);
  // Final structured EvaluationReport delivered via the `evaluation_report`
  // SSE event at pipeline_completed time. Same shape as GET /runs/{id}/report.
  // Null until the report is ready; consumed by the results panel.
  const [evaluationReport, setEvaluationReport] = useState<Record<string, unknown> | null>(null);
  // SSE connection status — drives the "reconnecting…" banner in the UI.
  const [sseStatus, setSseStatus] = useState<"connecting" | "open" | "reconnecting" | "closed">("connecting");
  // Wall-clock of last SSE event received. The UI uses this to surface a
  // "no progress for Xm — pipeline may be stuck" warning when nothing has
  // arrived for a while during a long agent phase.
  const [lastEventAt, setLastEventAt] = useState<number>(Date.now());
  // Workflow blueprint emitted by Agent 1 — null until the backend fires
  // `workflow_blueprint`. The WorkflowDiagram reads this and renders a
  // step chain; when null it renders nothing.
  const [workflow, setWorkflow] = useState<WorkflowBlueprint | null>(null);

  // Phase 6: per-scope candidate selection state. Populated when the
  // pipeline pauses with `selection_required` SSE. Consumed by the
  // SelectionPanel component which lets the user keep/remove candidates
  // per scope and add custom providers.
  const [perScopeCandidates, setPerScopeCandidates] = useState<
    Record<string, string[]>
  >({});
  // Phase 7 default picks — the top-K per scope ranked by the weighted
  // scorer (user_picked / credentials / relevance / docs / pricing_fit).
  // SelectionPanel pre-checks these instead of "all candidates." Empty
  // on resume until selection_required fires.
  const [defaultPicks, setDefaultPicks] = useState<
    Record<string, string[]>
  >({});
  const [isSelectionSubmitting, setIsSelectionSubmitting] = useState(false);

  // Per-candidate rejection entries from selected-candidate verification.
  // Populated by `candidate_rejected` SSE events; consumed by the
  // null-safe RejectionSummary component. Empty until 6.5 ships.
  const [rejections, setRejections] = useState<RejectionEntry[]>([]);

  const unsubscribeRef = useRef<(() => void) | null>(null);
  const runIdRef = useRef<string | null>(null);

  useEffect(() => {
    return () => {
      unsubscribeRef.current?.();
    };
  }, []);

  const addActivity = useCallback(
    (agentId: string, type: ActivityEntry["type"], summary: string, extra?: Partial<ActivityEntry>) => {
      const next = makeActivity(agentId, type, summary, extra);
      const nextKey = activityDedupeKey(next);
      setActivityEntries((prev) => {
        const last = prev[prev.length - 1];
        if (last && activityDedupeKey(last) === nextKey) {
          return [
            ...prev.slice(0, -1),
            { ...last, timestamp: Date.now(), detail: next.detail ?? last.detail },
          ];
        }
        return [...prev, next];
      });
    },
    []
  );

  // SSE event handler
  const handleSSEEvent = useCallback(
    (event: SSEEventData) => {
      const { type, data } = event;
      // Bump the wall-clock so the stuck-pipeline warning resets — every
      // event proves the backend is alive. Done first so even handlers
      // that throw still update the heartbeat.
      setLastEventAt(Date.now());

      switch (type) {
        case "pipeline_started":
          setStage("pipeline");
          setPipelineNodes([...INITIAL_NODES]);
          break;

        case "selection_required": {
          // Phase 6: pipeline paused after Agent 2 — show SelectionPanel.
          // Payload: per_scope_candidates (scope_id -> candidate name list)
          //          + default_picks (scope_id -> top-K per Phase 7 ranker)
          // SelectionPanel uses default_picks to pre-check Phase 7's smart
          // choice instead of all candidates.
          setStage("selection");
          const psc = data.per_scope_candidates as Record<string, string[]> | undefined;
          const dp = data.default_picks as Record<string, string[]> | undefined;
          setPerScopeCandidates(psc ?? {});
          setDefaultPicks(dp ?? {});
          addActivity("pipeline", "info", "Waiting for your candidate selection...", {
            status: "progress",
          });
          break;
        }

        case "test_cases_ready": {
          // Agent 3 finished — surface the count so users see progress
          // between screening and harness building (the long phase).
          const tcount = (data.count as number) ?? 0;
          addActivity(
            "agent_3",
            "info",
            `Test cases generated: ${tcount}`,
            { status: "success" },
          );
          break;
        }

        case "agent_blocked": {
          // Billing gate denied this agent. User needs the specific reason
          // (plan feature missing / credits insufficient) to know what to
          // do — generic "pipeline_failed" alone is unhelpful.
          const agent = (data.agent as string) || "agent";
          const reason = (data.reason as string) || "billing gate";
          const plan = (data.plan as string) || "";
          addActivity(
            agent,
            "error",
            `Blocked: ${reason}${plan ? ` (plan: ${plan})` : ""}`,
            { status: "error" },
          );
          break;
        }

        case "scope_verified_complete": {
          // Per-scope aggregate from selected-candidate verification.
          const scopeId = (data.scope_id as string) || "scope";
          const v = (data.verified_count as number) ?? 0;
          const r = (data.rejected_count as number) ?? 0;
          addActivity(
            "agent_4",
            "info",
            `${scopeId}: ${v} verified, ${r} rejected`,
            { status: "success" },
          );
          break;
        }

        case "candidate_rejected": {
          // Per-candidate rejection from selected-candidate verification.
          const rName = data.candidate_name as string;
          const rScopeId = data.scope_id as string;
          const rReason = data.reason as RejectionEntry["reason"];
          const rNotes = (data.attempt_notes as string) || "";
          const rProvider = (data.provider as string) || "";
          if (rName && rScopeId && rReason) {
            setRejections((prev) => [
              ...prev,
              {
                name: rName,
                provider: rProvider,
                scope_id: rScopeId,
                reason: rReason,
                attempt_notes: rNotes,
              },
            ]);
          }
          break;
        }

        case "cost_update": {
          // Live cost-meter update emitted incrementally from agents that
          // run for several minutes (Agent 5 builder loop in particular).
          // Without this handler the user only sees cost climb at agent
          // boundaries, which feels like the system is silent during the
          // longest phase. Payload: {total_cost_usd, agent_name?, delta?}.
          const total = data.total_cost_usd as number | undefined;
          if (typeof total === "number") {
            setCostAccumulator(total);
          } else {
            const delta = data.delta as number | undefined;
            if (typeof delta === "number") {
              setCostAccumulator((prev) => prev + delta);
            }
          }
          break;
        }

        case "coverage_gap": {
          // Backend warns the user when Agent 2 found 0 candidates OR when
          // some workflow scopes have no covering candidates. Surface as a
          // chat note so the user can BROADEN their request or add specific
          // providers via the SelectionPanel — without this the pipeline
          // would silently complete with no actual results, which reads as
          // "the system broke."
          const userMsg = data.user_message as string | undefined;
          const missing = data.missing_scopes as string[] | undefined;
          const candidateCount = data.candidate_count as number | undefined;
          const lines: string[] = [];
          lines.push(
            candidateCount === 0
              ? "**Coverage gap:** No candidates were found for your request."
              : `**Coverage gap:** ${candidateCount ?? "?"} candidate(s) found, but ${missing?.length ?? 0} workflow scope(s) have no coverage.`,
          );
          if (missing && missing.length > 0) {
            lines.push(`Missing scopes: ${missing.join(", ")}`);
          }
          if (userMsg) lines.push(userMsg);
          setMessages((prev) => [
            ...prev,
            {
              id: nextMessageId(),
              role: "assistant" as const,
              content: lines.join("\n\n"),
            },
          ]);
          break;
        }

        case "evaluation_report": {
          // Final structured report assembled at pipeline_completed time.
          // Payload is the EvaluationReport dict (see puzzleeval/report.py).
          // We store it in component state so the results page can render
          // a rich comparison view; the frontend can also fetch it via
          // GET /runs/{id}/report at any point.
          // (State store wired below — see setEvaluationReport.)
          setEvaluationReport(data as Record<string, unknown>);
          const winner = (data as { overall_winner?: string }).overall_winner;
          const candidateCount = (data as { candidate_count?: number }).candidate_count ?? 0;
          if (winner) {
            setMessages((prev) => [
              ...prev,
              {
                id: nextMessageId(),
                role: "assistant" as const,
                content: `**Evaluation report ready.** ${candidateCount} candidate(s) tested, winner: **${winner}**. Open the results panel for the full breakdown.`,
              },
            ]);
          }
          break;
        }

        case "test_data_sufficiency": {
          // Per-file-requiring-scope verdict from puzzleeval/test_data_sufficiency.py.
          // Payload shape:
          //   {
          //     summary: { by_action, min_confidence, advisories[],
          //                request_messages[{scope_id,message}], needs_user_action },
          //     verdicts: [{
          //       scope_id, action: "ready"|"augment"|"synthesize"|"request_more"|"degrade",
          //       reason, advisories[], request_message, degraded_confidence,
          //       plugin_for_augment, file_count
          //     }, ...]
          //   }
          // The frontend turns it into a chat note so the user sees what the
          // pipeline plans to do with their data BEFORE Agent 3F/3 runs.
          // request_more verdicts are surfaced loud — those need user action.
          const summary = data.summary as Record<string, unknown> | undefined;
          const verdicts = data.verdicts as Array<Record<string, unknown>> | undefined;
          if (verdicts && verdicts.length > 0) {
            const lines = verdicts.map((v) => {
              const action = String(v.action || "").toUpperCase();
              return `- **${v.scope_id}**: ${action} — ${v.reason}`;
            });
            const advisoryCount = (summary?.advisories as unknown[] | undefined)?.length ?? 0;
            const needsAction = Boolean(summary?.needs_user_action);
            const tail = needsAction
              ? "\n\n**Action needed:** one or more scopes need more / different sample files."
              : advisoryCount > 0
                ? `\n\n${advisoryCount} advisory note${advisoryCount === 1 ? "" : "s"} attached.`
                : "";
            setMessages((prev) => [
              ...prev,
              {
                id: nextMessageId(),
                role: "assistant" as const,
                content: `**Test data sufficiency check:**\n${lines.join("\n")}${tail}`,
              },
            ]);
          }
          break;
        }

        case "workflow_blueprint": {
          // Agent 1's director output. Payload may be null on mock runs
          // that don't emit a blueprint — WorkflowDiagram treats null as
          // "render nothing".
          const bp = data.workflow as WorkflowBlueprint | null | undefined;
          setWorkflow(bp ?? null);

          // TestPlan summary → chat message so user sees the architecture
          // before research starts (non-blocking UX improvement).
          const tp = data.test_plan as Record<string, unknown> | null | undefined;
          if (bp && bp.steps && bp.steps.length > 0) {
            const stepSummary = bp.steps
              .map((s) => `**${s.id}** (${s.role}): ${s.description}`)
              .join("\n");
            let planNote = "";
            if (tp && Array.isArray(tp.scope_specs)) {
              const specs = tp.scope_specs as Array<Record<string, unknown>>;
              planNote = "\n\n**Test Plan:**\n" + specs
                .map((s) => `- ${s.scope_id}: ${s.test_mode}, ${s.test_count_target} tests`)
                .join("\n");
            }
            setMessages((prev) => [
              ...prev,
              {
                id: nextMessageId(),
                role: "assistant" as const,
                content: `I've designed a ${bp.steps.length}-scope workflow:\n\n${stepSummary}${planNote}\n\nNow searching for the best AI solutions for each scope...`,
              },
            ]);
          }
          break;
        }

        case "agent_started": {
          const agentId = data.agent as string;
          const agentName = data.name as string;
          setPipelineProgress((prev) => ({
            ...prev,
            current_agent: agentName,
          }));
          setPipelineNodes((prev) =>
            prev.map((n) => (n.agentId === agentId ? { ...n, status: "active" as const } : n))
          );
          addActivity(agentId, "agent_start", agentName);
          break;
        }

        case "agent_completed": {
          const agentId = data.agent as string;
          const cost = data.cost_usd as number | undefined;
          setPipelineProgress((prev) => ({
            ...prev,
            agents_completed: [...prev.agents_completed, agentId],
            current_agent: null,
          }));
          setPipelineNodes((prev) =>
            prev.map((n) => (n.agentId === agentId ? { ...n, status: "completed" as const, cost } : n))
          );
          if (cost) setCostAccumulator((prev) => prev + cost);
          break;
        }

        case "agent_activity": {
          const agentId = data.agent as string;
          const message = data.message as string;
          const candidateName = data.candidate_name as string | undefined;
          const status = (data.status as ActivityEntry["status"]) || "progress";

          // Always add to activity feed
          addActivity(agentId, "info", message, { candidateName, status });

          // Agent 5 per-candidate messages ALSO go to the candidate's buildLog (shown on cards)
          if (agentId === "agent_5" && candidateName) {
            setCandidates((prev) =>
              prev.map((c) =>
                c.name === candidateName
                  ? {
                      ...c,
                      buildLog:
                        c.buildLog[c.buildLog.length - 1] === message
                          ? c.buildLog
                          : [...c.buildLog, message],
                    }
                  : c
              )
            );
          }
          break;
        }

        case "agent_thinking": {
          const agentId = data.agent as string;
          addActivity(agentId, "thinking", "Agent reasoning...", {
            detail: data.thinking as string,
            candidateName: data.candidate_name as string | undefined,
          });
          break;
        }

        case "candidates_selected": {
          const selected = data.selected as string[];
          setCandidates((prev) => prev.filter((c) => selected.includes(c.name)));
          break;
        }

        case "candidates_found": {
          const rawCandidates = data.candidates as Array<Record<string, unknown>>;
          setCandidates(
            rawCandidates.map((c) => {
              // Dual-search coverage fields. Backend sends a list of step IDs
              // and a dict of step_id -> "claimed"/"verified". Default to
              // empty on mock-mode payloads that don't populate coverage —
              // the UI falls back to a flat view with no coverage badges.
              const coversRaw = c.covers_step_ids;
              const covers = Array.isArray(coversRaw)
                ? coversRaw.filter((x): x is string => typeof x === "string")
                : [];
              const confRaw = (c.coverage_confidence as Record<string, string> | undefined) || {};
              const coverageConfidence: Record<string, CoverageConfidence> = {};
              for (const [sid, val] of Object.entries(confRaw)) {
                coverageConfidence[sid] = val === "verified" ? "verified" : "claimed";
              }
              // pricing_breakdown is null here — Agent 2 never populates it.
              // Selected-candidate verification emits a `candidate_verified` event
              // with the real pricing, and PricingBlock stays hidden until
              // that fires.
              return {
                name: (c.name as string) || "",
                provider: (c.provider as string) || "",
                description: (c.description as string) || "",
                relevance_score: (c.relevance_score as number) || 0,
                adoption_difficulty: (c.adoption_difficulty as "easy" | "medium" | "hard") || "medium",
                claimed_capabilities: (c.claimed_capabilities as string[]) || [],
                covers_step_ids: covers,
                coverage_confidence: coverageConfidence,
                pricing_breakdown: null,
                confirmed_capabilities: [],
                harness_status: "pending" as const,
                test_status: "pending" as const,
                test_results: [],
                buildLog: [],
              };
            })
          );
          break;
        }

        case "candidates_verified": {
          const validated = data.validated as Array<Record<string, unknown>>;
          const rejected = data.rejected as string[];
          setCandidates((prev) => {
            const updated = prev.filter((c) => !rejected.includes(c.name));
            return updated.map((c) => {
              const match = validated.find((v) => v.name === c.name);
              if (!match) return c;
              // pricing_breakdown travels on this SSE payload when selected-candidate
              // 4B extraction filled it in. Parse null-safely in case the
              // field is absent (e.g. mock-mode runs or legacy artifacts).
              const maybePricing = match.pricing_breakdown;
              const pricing = maybePricing && typeof maybePricing === "object"
                ? (maybePricing as unknown as typeof c.pricing_breakdown)
                : null;
              return {
                ...c,
                description: (match.description as string) || c.description,
                confirmed_capabilities: (match.confirmed_capabilities as string[]) || c.confirmed_capabilities,
                auth_method: match.auth_method as string,
                api_access_method: match.api_access_method as string,
                verified_api_docs_url: match.verified_api_docs_url as string,
                pricing_breakdown: pricing,
              };
            });
          });
          break;
        }

        case "candidate_verified": {
          // Phase 5 + 6.5: per-candidate verification event emitted during
          // Selected-candidate verification/research loop. Carries pricing_breakdown
          // populated by 4B extraction. Null-safe here — when 6.5 hasn't
          // shipped this event never fires and candidates keep the default
          // null pricing.
          const name = data.candidate_name as string;
          const pricing = data.pricing_breakdown as Record<string, unknown> | null | undefined;
          if (!name) break;
          setCandidates((prev) =>
            prev.map((c) => {
              if (c.name !== name) return c;
              return {
                ...c,
                pricing_breakdown:
                  pricing && typeof pricing === "object"
                    ? (pricing as unknown as typeof c.pricing_breakdown)
                    : c.pricing_breakdown ?? null,
              };
            })
          );
          break;
        }

        case "harness_started": {
          const hName = data.candidate_name as string;
          setCandidates((prev) => {
            const exists = prev.some((c) => c.name === hName);
            if (exists) {
              return prev.map((c) =>
                c.name === hName ? { ...c, harness_status: "building" as const } : c
              );
            }
            // Candidate not in list yet (Agent 5 selected it but candidates_selected hasn't filtered yet)
            // Add it with building status
            return [
              ...prev,
              {
                name: hName,
                provider: "",
                description: "",
                relevance_score: 0,
                adoption_difficulty: "medium" as const,
                claimed_capabilities: [],
                covers_step_ids: [],
                coverage_confidence: {},
                pricing_breakdown: null,
                confirmed_capabilities: [],
                harness_status: "building" as const,
                test_status: "pending" as const,
                test_results: [],
                buildLog: [],
              },
            ];
          });
          setPipelineProgress((prev) => ({
            ...prev,
            harnesses_building: [...prev.harnesses_building, hName],
          }));
          break;
        }

        case "harness_completed":
          setCandidates((prev) =>
            prev.map((c) =>
              c.name === data.candidate_name
                ? {
                    ...c,
                    harness_status: "built" as const,
                    build_cost_usd: data.build_cost_usd as number,
                    build_turns: data.build_turns as number,
                  }
                : c
            )
          );
          setPipelineProgress((prev) => ({
            ...prev,
            harnesses_building: prev.harnesses_building.filter((n) => n !== data.candidate_name),
            harnesses_completed: [...prev.harnesses_completed, data.candidate_name as string],
          }));
          break;

        case "harness_failed":
          setCandidates((prev) =>
            prev.map((c) =>
              c.name === data.candidate_name ? { ...c, harness_status: "failed" as const } : c
            )
          );
          setPipelineProgress((prev) => ({
            ...prev,
            harnesses_building: prev.harnesses_building.filter((n) => n !== data.candidate_name),
            harnesses_failed: [...prev.harnesses_failed, data.candidate_name as string],
          }));
          break;

        case "test_execution_started":
          setCandidates((prev) =>
            prev.map((c) =>
              c.name === data.candidate_name ? { ...c, test_status: "running" as const } : c
            )
          );
          break;

        case "test_result":
          setCandidates((prev) =>
            prev.map((c) => {
              if (c.name !== data.candidate_name) return c;
              return {
                ...c,
                test_results: [
                  ...c.test_results,
                  {
                    test_case_id: data.test_case_id as string,
                    passed: data.passed as boolean,
                    weighted_score: data.weighted_score as number,
                    latency_ms: data.latency_ms as number,
                    criteria_scores:
                      (data.criteria_scores as Array<{
                        criterion: string;
                        score: number;
                        passed: boolean;
                        reasoning: string;
                      }>) || [],
                  },
                ],
              };
            })
          );
          break;

        case "candidate_results_ready": {
          const crName = data.candidate_name as string;
          setCandidates((prev) => {
            const exists = prev.some((c) => c.name === crName);
            const resultData = {
              test_status: "completed" as const,
              overall_score: (data.overall_score as number) ?? (data.pass_rate as number),
              pass_rate: data.pass_rate as number,
              avg_latency_ms: data.avg_latency_ms as number,
              total_cost_usd: data.total_cost_usd as number,
              tests_passed: data.tests_passed as number,
              tests_failed: data.tests_failed as number,
            };
            if (exists) {
              return prev.map((c) =>
                c.name === crName ? { ...c, ...resultData } : c
              );
            }
            // Candidate not in list — add it with results
            return [
              ...prev,
              {
                name: crName,
                provider: (data.provider as string) || "",
                description: "",
                relevance_score: 0,
                adoption_difficulty: "medium" as const,
                claimed_capabilities: [],
                covers_step_ids: [],
                coverage_confidence: {},
                pricing_breakdown: null,
                confirmed_capabilities: [],
                harness_status: "built" as const,
                test_results: [],
                buildLog: [],
                ...resultData,
              },
            ];
          });
          break;
        }

        case "report_generating":
          addActivity("report", "info", "Generating evaluation report...", { status: "progress" });
          break;

        case "pipeline_completed":
          setStage("results");
          if (data.total_cost_usd) setCostAccumulator(data.total_cost_usd as number);
          addActivity("pipeline", "pipeline_complete", `Pipeline complete — ${data.summary || ""}`, {
            status: "success",
          });
          setMessages((prev) => [
            ...prev,
            {
              id: nextMessageId(),
              role: "assistant" as const,
              content: `Evaluation complete! ${(data.summary as string) || "Check the results panel for detailed scores."}`,
            },
          ]);
          break;

        case "pipeline_cancelled":
          setStage("results");
          addActivity("pipeline", "pipeline_complete", "Pipeline cancelled", { status: "failure" });
          setMessages((prev) => [
            ...prev,
            {
              id: nextMessageId(),
              role: "assistant" as const,
              content: "Run cancelled. Partial results are shown in the panel.",
            },
          ]);
          break;

        case "pipeline_failed":
          setStage("results");
          addActivity("pipeline", "pipeline_complete", `Pipeline failed: ${data.error || ""}`, {
            status: "failure",
          });
          setMessages((prev) => [
            ...prev,
            {
              id: nextMessageId(),
              role: "assistant" as const,
              content: `Pipeline failed: ${(data.error as string) || "Unknown error"}`,
            },
          ]);
          break;

        case "done":
          // Terminal event — backend has closed the event bus. Call the
          // subscribe cleanup so the EventSource shuts down cleanly
          // instead of firing onerror → reconnect → onerror → reconnect
          // in an infinite loop against a closed stream. That loop is
          // what produces the persistent "Reconnecting to backend…"
          // banner after the run completes (pipeline was done, but the
          // EventSource kept flapping).
          unsubscribeRef.current?.();
          unsubscribeRef.current = null;
          setSseStatus("closed");
          break;
      }
    },
    [addActivity]
  );

  const handleSend = useCallback(
    async (text: string, pendingFiles: File[]) => {
      if (!text.trim() && pendingFiles.length === 0) return;
      setIsLoading(true);

      try {
        let currentRunId = runIdRef.current;
        let fileIds: string[] = [];

        if (!currentRunId) {
          const run = await createRun(text, [], agentModes);
          currentRunId = run.run_id;
          setRunId(run.run_id);
          runIdRef.current = run.run_id;
        }

        if (pendingFiles.length > 0) {
          const uploadResult = await uploadFiles(currentRunId, pendingFiles);
          fileIds = uploadResult.file_ids;
        }

        const userMessage: Message = {
          id: nextMessageId(),
          role: "user",
          content: text || `Uploaded ${pendingFiles.length} file(s)`,
          attachments: pendingFiles.map((f) => ({
            name: f.name,
            size: f.size,
            type: f.type,
          })),
        };
        setMessages((prev) => [...prev, userMessage]);

        const response = await apiSendMessage(currentRunId, text, fileIds);

        const assistantContent = response.assistant_message;

        setMessages((prev) => [
          ...prev,
          { id: nextMessageId(), role: "assistant", content: assistantContent },
        ]);

        if (response.pipeline_started) {
          unsubscribeRef.current?.();
          // Pass the SSE status callback so the UI can render reconnect
          // banners. Without it, a network blip silently kills the stream
          // and the user sees a hung "loading" state.
          const unsub = subscribeToEvents(
            currentRunId,
            handleSSEEvent,
            () => {},
            (status) => setSseStatus(status),
          );
          unsubscribeRef.current = unsub;
        }
      } catch (err) {
        setMessages((prev) => [
          ...prev,
          {
            id: nextMessageId(),
            role: "assistant",
            content: `Error: ${err instanceof Error ? err.message : "Something went wrong"}`,
          },
        ]);
      } finally {
        setIsLoading(false);
      }
    },
    [agentModes, handleSSEEvent]
  );

  const handleCancel = useCallback(async () => {
    const id = runIdRef.current;
    if (!id) return;
    try {
      await apiCancelRun(id);
    } catch {
      // best effort
    }
  }, []);

  // Phase 6: submit the user's per-scope candidate picks + optional
  // user-added providers. Called by SelectionPanel.
  const submitSelection = useCallback(
    async (
      scopePicks: Record<string, string[]>,
      userAdded: UserAddedCandidate[] = []
    ) => {
      const id = runIdRef.current;
      if (!id) return;
      setIsSelectionSubmitting(true);
      try {
        const req: SelectCandidatesRequest = {
          scope_picks: scopePicks,
          add: userAdded,
        };
        await apiSelectCandidates(id, req);
        // Selection submitted — pipeline resumes automatically. The backend
        // will re-emit `candidates_found` with the filtered set, and the
        // stage will transition back to "pipeline" when events resume.
        setStage("pipeline");
        addActivity("pipeline", "info", "Selection submitted — pipeline resuming...", {
          status: "success",
        });
      } catch (err) {
        setMessages((prev) => [
          ...prev,
          {
            id: nextMessageId(),
            role: "assistant" as const,
            content: `Selection failed: ${err instanceof Error ? err.message : "Unknown error"}. Please try again.`,
          },
        ]);
      } finally {
        setIsSelectionSubmitting(false);
      }
    },
    [addActivity]
  );

  return {
    stage,
    messages,
    candidates,
    pipelineProgress,
    costAccumulator,
    isLoading,
    handleSend,
    handleCancel,
    activityEntries,
    pipelineNodes,
    runId, // Phase 2: needed by QuotaBadge to poll GET /runs/{id} for current quota
    workflow, // Phase 3: blueprint from Agent 1, consumed by WorkflowDiagram
    // Phase 6: selection state + handlers
    perScopeCandidates,
    defaultPicks, // Phase 7 top-K pre-checked in SelectionPanel
    isSelectionSubmitting,
    submitSelection,
    // Rejection entries from selected-candidate verification
    rejections,
    // Robustness pass: SSE status + heartbeat + final report
    sseStatus,
    lastEventAt,
    evaluationReport,
  };
}
