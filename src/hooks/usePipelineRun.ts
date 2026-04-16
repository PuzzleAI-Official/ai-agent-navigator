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

const INITIAL_NODES: PipelineNodeState[] = [
  { agentId: "agent_1", label: "Understand", status: "completed" },
  { agentId: "agent_2", label: "Research", status: "pending" },
  { agentId: "agent_3", label: "Test Cases", status: "pending" },
  { agentId: "agent_4", label: "Screen", status: "pending" },
  { agentId: "agent_5", label: "Build + Test", status: "pending" },
];

let activityCounter = 0;

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
  // Phase 3: workflow blueprint emitted by Agent 1 — null until the backend
  // fires `workflow_blueprint`. The WorkflowDiagram reads this and renders
  // a step chain; when null it renders nothing (legacy pre-Phase-3 flow).
  const [workflow, setWorkflow] = useState<WorkflowBlueprint | null>(null);

  // Phase 6: per-scope candidate selection state. Populated when the
  // pipeline pauses with `selection_required` SSE. Consumed by the
  // SelectionPanel component which lets the user keep/remove candidates
  // per scope and add custom providers.
  const [perScopeCandidates, setPerScopeCandidates] = useState<
    Record<string, string[]>
  >({});
  const [isSelectionSubmitting, setIsSelectionSubmitting] = useState(false);

  // Phase 6.5 forward-compat: rejection entries after deep-verify.
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
      setActivityEntries((prev) => [...prev, makeActivity(agentId, type, summary, extra)]);
    },
    []
  );

  // SSE event handler
  const handleSSEEvent = useCallback(
    (event: SSEEventData) => {
      const { type, data } = event;

      switch (type) {
        case "pipeline_started":
          setStage("pipeline");
          setPipelineNodes([...INITIAL_NODES]);
          break;

        case "selection_required": {
          // Phase 6: pipeline paused after Agent 2 — show SelectionPanel.
          // Payload: per_scope_candidates (scope_id -> candidate name list)
          setStage("selection");
          const psc = data.per_scope_candidates as Record<string, string[]> | undefined;
          setPerScopeCandidates(psc ?? {});
          addActivity("pipeline", "info", "Waiting for your candidate selection...", {
            status: "progress",
          });
          break;
        }

        case "candidate_rejected": {
          // Phase 6.5 forward-compat: per-candidate rejection after deep-verify.
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

        case "workflow_blueprint": {
          // Phase 3: Agent 1's director output. Payload may be null for
          // pre-Phase-3 mock artifacts — WorkflowDiagram handles null by
          // rendering nothing, matching legacy behavior.
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
                id: Date.now(),
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
                  ? { ...c, buildLog: [...c.buildLog, message] }
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
              // Phase 4: dual-search coverage fields. Backend sends a list of
              // step IDs and a dict of step_id -> "claimed"/"verified". When
              // absent (pre-Phase-4 mock artifacts, or legacy single-pass
              // search) default to empty — the UI falls back to the pre-Phase-4
              // flat view with no coverage badges.
              const coversRaw = c.covers_step_ids;
              const covers = Array.isArray(coversRaw)
                ? coversRaw.filter((x): x is string => typeof x === "string")
                : [];
              const confRaw = (c.coverage_confidence as Record<string, string> | undefined) || {};
              const coverageConfidence: Record<string, CoverageConfidence> = {};
              for (const [sid, val] of Object.entries(confRaw)) {
                coverageConfidence[sid] = val === "verified" ? "verified" : "claimed";
              }
              // Phase 5: pricing_breakdown is null here (Agent 2 never fills
              // it); Phase 6.5 populates via a separate `candidate_verified`
              // event. Default to null so the PricingBlock stays hidden
              // until then.
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
            let updated = prev.filter((c) => !rejected.includes(c.name));
            return updated.map((c) => {
              const match = validated.find((v) => v.name === c.name);
              if (!match) return c;
              // Phase 5 forward-compat: once Phase 6.5's 4B extraction
              // populates pricing_breakdown server-side, it travels on
              // this SSE payload (or its successor `candidate_verified`
              // per-candidate event). Parse it null-safely so the field
              // stays null when Phase 6.5 hasn't shipped yet.
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
          // Phase 6.5's directed 4A→4B→4C→4D loop. Carries pricing_breakdown
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
              id: Date.now(),
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
              id: Date.now(),
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
              id: Date.now(),
              role: "assistant" as const,
              content: `Pipeline failed: ${(data.error as string) || "Unknown error"}`,
            },
          ]);
          break;

        case "done":
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
          id: Date.now(),
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
          { id: Date.now() + 1, role: "assistant", content: assistantContent },
        ]);

        if (response.pipeline_started) {
          unsubscribeRef.current?.();
          const unsub = subscribeToEvents(currentRunId, handleSSEEvent, () => {});
          unsubscribeRef.current = unsub;
        }
      } catch (err) {
        setMessages((prev) => [
          ...prev,
          {
            id: Date.now(),
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
            id: Date.now(),
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
    isSelectionSubmitting,
    submitSelection,
    // Phase 6.5 forward-compat: rejection entries from deep-verify
    rejections,
  };
}
