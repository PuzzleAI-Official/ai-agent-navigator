export type Stage = "conversation" | "pipeline" | "results";

// ---------------------------------------------------------------------------
// Phase 3: WorkflowBlueprint — Agent 1 as director
// ---------------------------------------------------------------------------
// Mirrors puzzleeval/schemas.py. Agent 1 produces this alongside sub_tasks
// when is_clear=true; downstream phases branch on `workflow !== null`.
// The WorkflowDiagram component renders it as a horizontal step chain above
// the candidate list.
// ---------------------------------------------------------------------------

export interface WorkflowStep {
  id: string;                       // "step_1", "step_2" — unique within blueprint
  role: string;                     // "ocr" | "extract" | "spreadsheet_sync" | ...
  description: string;              // one-sentence user-facing description
  capability: string;               // matches SubTask.capability (join key)
  input_from: string | null;        // "user" | "step_1" | null
  output_format: string;            // "free_text" | "structured_json" | ...
  depends_on: string[];             // ids of upstream steps
  all_in_one_compatible: boolean;
}

export interface WorkflowBlueprint {
  steps: WorkflowStep[];            // ordered
  architecture_options: string[];   // ["all_in_one", "best_per_step"]
  notes: string;                    // Agent 1's reasoning for UI tooltip
}

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

// ---------------------------------------------------------------------------
// Phase 2: Service tier scaffold
// ---------------------------------------------------------------------------
// Plan / Quota types mirror puzzleeval-api/models/api_models.py. The QuotaBadge
// component reads `Quota` from RunStateOut and renders plan + remaining credits.
// `billing_enforced=false` (the default) means the UI shows credit numbers as
// advisory only; flipping the backend env flag to enforce makes them blocking.
// ---------------------------------------------------------------------------

export type Plan = "free" | "paid" | "enterprise";

export interface Quota {
  plan: Plan;
  credits_remaining: number | null; // null = unlimited (free or enterprise)
  credits_consumed: number;
  tier_features: Record<string, boolean>;
  billing_enforced: boolean;
}

export interface RunStateOut {
  run_id: string;
  trace_id: string;
  status: string;
  stage: string;
  cost_usd: number;
  quota: Quota;
}
