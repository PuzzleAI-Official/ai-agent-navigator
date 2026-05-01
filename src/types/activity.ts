export type ActivityEntryType =
  | "agent_start"
  | "agent_complete"
  | "search_action"
  | "candidate_found"
  | "candidate_verified"
  | "candidate_rejected"
  | "harness_building"
  | "harness_built"
  | "harness_failed"
  | "test_running"
  | "test_result"
  | "pipeline_complete"
  | "thinking"
  | "info";

export interface ActivityEntry {
  id: string;
  timestamp: number;
  agentId: string;
  agentLabel: string;
  type: ActivityEntryType;
  summary: string;
  detail?: string;
  candidateName?: string;
  status?: "success" | "failure" | "progress" | "info";
  cost?: number;
}

export type DetailLevel = "summary" | "detailed";

export type PipelineNodeStatus = "pending" | "active" | "completed" | "failed";

export interface PipelineNodeState {
  agentId: string;
  label: string;
  status: PipelineNodeStatus;
  cost?: number;
  duration?: number;
  summary?: string;
}

export const AGENT_LABELS: Record<string, string> = {
  agent_1: "Design",
  agent_2: "Research",
  agent_3: "Test Cases",
  agent_4: "Screening",
  agent_5: "Build + Test",
};

export const AGENT_ICONS: Record<string, string> = {
  agent_1: "💬",
  agent_2: "🔍",
  agent_3: "🧪",
  agent_4: "🛡",
  agent_5_build: "🔧",
  agent_5_test: "📊",
};
