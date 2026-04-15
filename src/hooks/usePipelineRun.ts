import { useState, useCallback, useRef, useEffect } from "react";
import type {
  Stage,
  Message,
  PipelineCandidate,
  PipelineProgress,
  SSEEventData,
  AgentModes,
  WorkflowBlueprint,
} from "@/types/pipeline";
import type { ActivityEntry, PipelineNodeState } from "@/types/activity";
import { AGENT_LABELS } from "@/types/activity";
import {
  createRun,
  sendMessage as apiSendMessage,
  uploadFiles,
  cancelRun as apiCancelRun,
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

        case "workflow_blueprint": {
          // Phase 3: Agent 1's director output. Payload may be null for
          // pre-Phase-3 mock artifacts — WorkflowDiagram handles null by
          // rendering nothing, matching legacy behavior.
          const bp = data.workflow as WorkflowBlueprint | null | undefined;
          setWorkflow(bp ?? null);
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
            rawCandidates.map((c) => ({
              name: (c.name as string) || "",
              provider: (c.provider as string) || "",
              description: (c.description as string) || "",
              relevance_score: (c.relevance_score as number) || 0,
              adoption_difficulty: (c.adoption_difficulty as "easy" | "medium" | "hard") || "medium",
              claimed_capabilities: (c.claimed_capabilities as string[]) || [],
              confirmed_capabilities: [],
              harness_status: "pending" as const,
              test_status: "pending" as const,
              test_results: [],
              buildLog: [],
            }))
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
              return {
                ...c,
                description: (match.description as string) || c.description,
                confirmed_capabilities: (match.confirmed_capabilities as string[]) || c.confirmed_capabilities,
                auth_method: match.auth_method as string,
                api_access_method: match.api_access_method as string,
                verified_api_docs_url: match.verified_api_docs_url as string,
              };
            });
          });
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
  };
}
