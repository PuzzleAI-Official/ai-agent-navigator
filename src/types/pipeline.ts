export type Stage = "conversation" | "pipeline" | "results";

export interface Attachment {
  name: string;
  size: number;
  type: string;
  file?: File; // actual File object for upload
}

export interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  attachments?: Attachment[];
}

export interface CriterionScore {
  criterion: string;
  score: number;
  passed: boolean;
  reasoning: string;
}

export interface TestResult {
  test_case_id: string;
  passed: boolean;
  weighted_score: number;
  latency_ms: number;
  criteria_scores: CriterionScore[];
}

export interface PipelineCandidate {
  name: string;
  provider: string;
  description: string;
  relevance_score: number;
  adoption_difficulty: "easy" | "medium" | "hard";
  claimed_capabilities: string[];
  // Agent 4 enrichment
  confirmed_capabilities: string[];
  auth_method?: string;
  api_access_method?: string;
  verified_api_docs_url?: string;
  // Agent 5 build
  harness_status: "pending" | "building" | "built" | "failed";
  build_cost_usd?: number;
  build_turns?: number;
  // Agent 5 test results
  test_status: "pending" | "running" | "completed" | "failed";
  overall_score?: number;
  pass_rate?: number;
  avg_latency_ms?: number;
  total_cost_usd?: number;
  tests_passed?: number;
  tests_failed?: number;
  test_results: TestResult[];
  // Per-candidate build/test progress messages
  buildLog: string[];
}

export interface PipelineProgress {
  current_agent: string | null;
  agents_completed: string[];
  harnesses_building: string[];
  harnesses_completed: string[];
  harnesses_failed: string[];
  test_progress: number;
}

export interface SSEEventData {
  type: string;
  data: Record<string, unknown>;
}

export interface AgentModes {
  agent1: "mock" | "real";
  agent2: "mock" | "real";
  agent3: "mock" | "real";
  agent4: "mock" | "real";
  agent5: "mock" | "real";
}
