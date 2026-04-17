export type Stage = "conversation" | "pipeline" | "selection" | "results";

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
  depends_on: string[];             // ids of upstream steps — DAG authoritative
  parallel_group?: string | null;   // UI hint: sibling steps in one fan-out cluster
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

// Phase 4: every candidate carries a coverage set over blueprint step IDs.
// Values are "claimed" by Agent 2 dual search; Phase 6.5 upgrades confirmed
// scopes to "verified" or drops them from covers_step_ids.
export type CoverageConfidence = "claimed" | "verified";

// ---------------------------------------------------------------------------
// PricingBreakdown — structured pricing for a candidate.
// ---------------------------------------------------------------------------
// Mirrors puzzleeval/schemas.py::PricingBreakdown + PricingTier. Agent 2
// never fills this; Phase 6.5's 4B extraction during deep-verify populates
// it. Every field is optional so the UI renders gracefully when the
// extractor couldn't infer a field (e.g. sparse docs).
// ---------------------------------------------------------------------------

export type PricingConfidence = "high" | "medium" | "low";

export interface PricingTier {
  name: string;
  monthly_cost_usd: number;
  included_units?: number | null;
  unit_name?: string | null;
  overage_cost_per_unit_usd?: number | null;
  notes?: string | null;
}

export interface PricingBreakdown {
  tiers: PricingTier[];
  free_tier_monthly_units?: number | null;
  pay_as_you_go: boolean;
  billing_granularity: string;              // "monthly" | "per_call" | "annual_commit" | "hybrid"
  per_scope_unit_cost: Record<string, number>;  // keyed by step_id; empty when pricing is uniform
  sources: string[];
  confidence: PricingConfidence;
  notes?: string | null;
}

export interface PipelineCandidate {
  name: string;
  provider: string;
  description: string;
  relevance_score: number;
  adoption_difficulty: "easy" | "medium" | "hard";
  claimed_capabilities: string[];
  // Dual-search coverage (Phase 4)
  covers_step_ids: string[];                                  // blueprint step IDs this candidate claims to cover
  coverage_confidence: Record<string, CoverageConfidence>;    // keyed by step_id; "claimed" until deep-verify upgrades to "verified"
  // Structured pricing populated by the deep-verify 4B extraction step
  pricing_breakdown?: PricingBreakdown | null;                // null when the candidate hasn't been deep-verified yet
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

// ---------------------------------------------------------------------------
// Phase 9: Per-scope test execution
// ---------------------------------------------------------------------------

export interface ScopeTestRun {
  scope_id: string;
  scope_role: string;
  candidate_results: Array<{
    candidate_name: string;
    provider: string;
    pass_rate: number;
    tests_passed: number;
    tests_failed: number;
    tests_errored: number;
    avg_latency_ms: number;
    total_cost_usd: number;
  }>;
  test_case_count: number;
}

export interface RunStateOut {
  run_id: string;
  trace_id: string;
  status: string;
  stage: string;
  cost_usd: number;
  quota: Quota;
}

// ---------------------------------------------------------------------------
// Phase 6: User candidate selection types
// ---------------------------------------------------------------------------

export interface UserAddedCandidate {
  name: string;
  provider: string;
  api_docs_url?: string | null;
  notes?: string | null;
  covers_step_ids: string[];
  source?: string;
}

export interface SelectCandidatesRequest {
  scope_picks: Record<string, string[]>;    // scope_id -> candidate names
  add: UserAddedCandidate[];
}

export interface SelectCandidatesResponse {
  accepted_count: number;
  scope_coverage: Record<string, number>;   // scope_id -> count picked
}

// Rejection entries shown by RejectionSummary after the deep-verify pass.
// Populated from `candidate_rejected` SSE events.
export interface RejectionEntry {
  name: string;
  provider: string;
  scope_id: string;
  reason: "docs_unreachable" | "enterprise_only" | "deprecated" | "no_api" | "coverage_removed_at_scope" | "verify_error";
  attempt_notes: string;
}

// Phase 9 convenience alias used by ResultsComparison
export type ScopeCandidateResult = ScopeTestRun["candidate_results"][number];
