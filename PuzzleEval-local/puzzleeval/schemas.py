# ============================================================================
# Pydantic Schemas — Data Contracts for Agent 1
# ============================================================================
# These models serve THREE purposes:
#   1. DATA VALIDATION — catch bad data before it causes downstream problems
#   2. API CONTRACT — the JSON shapes frontend devs code against
#   3. STRUCTURED OUTPUT SCHEMA — Claude is constrained to match these exactly
#
# The `description` strings on each field are read by Claude when generating
# structured output. Good descriptions = better results.
# ============================================================================

from typing import Any

from pydantic import BaseModel, Field


# ============================================================================
# Agent 1 Input Schema
# ============================================================================

class Agent1Input(BaseModel):
    """What gets passed INTO Agent 1. Created by the CLI or API, NOT by Claude."""

    user_text: str = Field(
        description="The user's natural language description of their AI needs"
    )

    workflow_file_path: str | None = Field(
        default=None,
        description="Optional file path to the user's uploaded workflow document"
    )

    trace_id: str = Field(
        description="UUID for correlating logs across the entire pipeline"
    )

    # For backwards compatibility — simple single-string follow-up
    additional_context: str | None = Field(
        default=None,
        description="User's answers to clarifying questions from a previous round"
    )

    # Full conversation history for multi-turn conversations.
    # The CLI/API builds this by collecting messages from previous turns.
    # Format: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    conversation_history: list[dict[str, Any]] | None = Field(
        default=None,
        description="Previous conversation turns for multi-turn context"
    )


# ============================================================================
# Agent 1 Output Schemas
# ============================================================================

class SubTask(BaseModel):
    """
    A single capability/sub-task extracted from the user's request.

    WHY SUB-TASKS?
    Users describe their needs as one big request ("read invoices and put
    them in QuickBooks"). But the best AI solution might be a combination
    of specialized tools — one for OCR, one for QuickBooks sync. By
    decomposing into sub-tasks, the Research Agent can search for the best
    tool PER capability, not just all-in-one solutions.

    Example decomposition of "read invoices and put in QuickBooks":
      Sub-task 1: "Extract structured data from invoice photos" (capability: OCR/document extraction)
      Sub-task 2: "Create bill entries in QuickBooks from structured data" (capability: accounting integration)
    """

    # What this sub-task does — described as an input→output behavior
    description: str = Field(
        description=(
            "A concrete, testable task described as input→output behavior. "
            "Example: 'Given a photo of a paper invoice, extract vendor name, "
            "line items, and total amount into structured JSON'"
        )
    )

    # The underlying capability needed (used for searching)
    capability: str = Field(
        description=(
            "The AI capability category needed for this sub-task. Examples: "
            "'document OCR', 'text generation', 'data extraction', "
            "'API integration', 'image understanding', 'classification'"
        )
    )

    # Search keywords specific to THIS sub-task
    search_keywords: list[str] = Field(
        description=(
            "2-4 search keywords for finding AI solutions for this specific "
            "sub-task. Focus on the capability, not the end-to-end workflow. "
            "Example: for invoice OCR, use 'document OCR API', 'invoice data "
            "extraction', 'receipt scanning AI' — NOT 'invoice QuickBooks automation'"
        )
    )

    # Whether this sub-task requires real files for testing
    requires_test_files: bool = Field(
        default=False,
        description=(
            "True when this sub-task involves processing files: OCR, document "
            "extraction, image analysis, PDF parsing, spreadsheet processing. "
            "False for text-based tasks: chatbot, classification, text generation, "
            "structured data processing via API."
        )
    )

    test_file_description: str | None = Field(
        default=None,
        description=(
            "When requires_test_files is True, describes what files the user "
            "should provide for THIS sub-task. Be specific: "
            "'5-10 sample invoice photos or PDFs'. Null when requires_test_files is False."
        )
    )


class Constraints(BaseModel):
    """User's constraints. All fields optional."""

    budget_range: str | None = Field(
        default=None,
        description="Monthly budget range, e.g., '$50-200/mo', 'Free', or null if not specified"
    )

    must_have_features: list[str] = Field(
        default_factory=list,
        description="Non-negotiable features the AI solution must support"
    )

    # Integration requirements are tracked here but NOT used as search filters.
    # The Research Agent searches by capability. The Screening Agent (Agent 4)
    # later checks if candidates can integrate with these systems.
    integration_requirements: list[str] = Field(
        default_factory=list,
        description=(
            "External systems the solution must eventually work with. "
            "NOTE: These are checked during screening, not during search — "
            "a tool that can't directly integrate may still be usable via API glue."
        )
    )

    # User's technical level — affects whether modular solutions are viable
    technical_level: str | None = Field(
        default=None,
        description=(
            "User's technical capability: 'non-technical' (needs turnkey), "
            "'some-technical' (can connect APIs with guidance), "
            "'technical' (can build custom integrations). "
            "Null if not determined."
        )
    )


# ============================================================================
# Phase 3: WorkflowBlueprint — Agent 1 as director
# ============================================================================
# SubTask (above) answers "what capabilities does the user need?".
# WorkflowBlueprint answers "what's the shape of the user's workflow?" —
# ordering, data flow, role assignment, architecture options.
#
# Downstream phases use this structure:
#   Phase 4 (Agent 2 dual search) groups candidates by step role
#   Phase 6 (user selection UI) renders a step-chain diagram
#   Phase 7 (Agent 5 selection) guarantees ≥1 candidate per step
#   Phase 9 (workflow-chaining harnesses) wires step[n].run → step[n+1].run
#
# Optional — when Agent 1 can't produce a meaningful blueprint (or for old
# saved runs pre-Phase-3), `workflow=None` triggers today's flat-sub-tasks
# behavior everywhere downstream.
# ============================================================================

class WorkflowStep(BaseModel):
    """One step in the user's workflow. Ordered chain; `depends_on` encodes the DAG."""

    id: str = Field(
        description=(
            "Stable ID for this step, e.g. 'step_1'. Used by other steps' "
            "depends_on, by Phase 4's candidates_by_step grouping, and by "
            "Phase 9's workflow harness wiring. Must be unique within the blueprint."
        )
    )

    role: str = Field(
        description=(
            "Short role tag that describes the job this step does. Examples: "
            "'ocr', 'extract', 'spreadsheet_sync', 'classify', 'chatbot', "
            "'translate', 'summarize'. Phase 4's search pass uses this to "
            "group candidates. Prefer snake_case, single concept per role."
        )
    )

    description: str = Field(
        description=(
            "One-sentence human description of what this step does for the user. "
            "Rendered in the frontend WorkflowDiagram and the selection UI."
        )
    )

    capability: str = Field(
        description=(
            "Matches the `capability` string on the corresponding SubTask. "
            "This is the join key — same string on SubTask.capability and "
            "WorkflowStep.capability links the two so downstream agents can "
            "cross-reference search keywords without needing a separate ID field."
        )
    )

    input_from: str | None = Field(
        default=None,
        description=(
            "Where this step's input comes from. Either 'user' (first step — "
            "user provides a file/text/prompt) or another step's id like 'step_1' "
            "(this step consumes step_1's output). None means standalone."
        )
    )

    output_format: str = Field(
        default="structured_json",
        description=(
            "Shape of this step's output. Matches the TestCase.output_type "
            "enum: 'free_text' | 'structured_json' | 'classification' | "
            "'extraction' | 'action'. Phase 9's harness uses this to decide "
            "whether to serialize the output before passing to the next step."
        )
    )

    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of steps that must complete before this one starts. Encodes "
            "the DAG. Empty list = no dependencies (step can run first). "
            "Two steps whose `depends_on` lists don't reference each other "
            "are implicitly parallel — the runtime can execute them "
            "concurrently. Cycles are forbidden; the Agent 1 validator "
            "rejects them as errors."
        )
    )

    parallel_group: str | None = Field(
        default=None,
        description=(
            "Optional tag that clusters this step with siblings in the same "
            "intentional fan-out (e.g. three enrichment steps that all read "
            "step_1's output). Purely a layout hint for the frontend "
            "WorkflowDiagram — steps sharing the same non-null tag render "
            "side-by-side in one visual group. DAG semantics are still "
            "governed entirely by `depends_on`; `parallel_group` never "
            "changes execution order. Leave null for linear chains and "
            "single-step blueprints."
        )
    )

    all_in_one_compatible: bool = Field(
        default=True,
        description=(
            "True if a single all-in-one tool could cover this step (almost "
            "always true — most roles have all-in-one alternatives). Phase 4 "
            "uses this to decide whether to INCLUDE this step in the all-in-one "
            "search pass. False only for niche steps (e.g. proprietary "
            "integration) that no horizontal tool reaches."
        )
    )


class WorkflowBlueprint(BaseModel):
    """The ordered shape of the user's workflow.

    Authored by Agent 1 alongside sub_tasks in the same LLM call. Downstream
    phases branch on `len(steps) == 1` for trivial single-capability requests
    (e.g., "I need a chatbot") and treat those as today; multi-step blueprints
    unlock Phase 4's dual search, Phase 6's selection UI, and Phase 9's
    workflow-chaining harnesses.
    """

    steps: list[WorkflowStep] = Field(
        description=(
            "Ordered list of workflow steps. Order reflects the intended "
            "execution sequence (but `depends_on` is authoritative for actual "
            "DAG topology). Minimum 1 step."
        )
    )

    architecture_options: list[str] = Field(
        default_factory=lambda: ["all_in_one", "best_per_step"],
        description=(
            "Which architectural approaches make sense for this workflow. "
            "Almost always both: a single all-in-one tool AND a best-per-step "
            "composition. The user picks after seeing real candidates. Empty "
            "or single-element list for edge cases (e.g., workflow has one "
            "step -- all_in_one IS best_per_step, no distinction)."
        )
    )

    notes: str = Field(
        default="",
        description=(
            "Agent 1's reasoning about how the workflow is structured. "
            "Rendered in the UI tooltip on the WorkflowDiagram so the user "
            "understands why we decomposed it this way and can correct it "
            "in the Phase 6 selection UI if needed."
        )
    )


# ============================================================================
# Test Plan — Agent 1 as test director
# ============================================================================
# Agent 1 designs the test plan alongside the workflow. Instead of Agent 3/3F
# independently guessing what tests to generate, they execute THIS plan.
# Each scope gets an explicit spec: what mode (file/synthetic), what input
# shape, what output shape, and example data so downstream steps get
# correctly-shaped test inputs.
# ============================================================================


class ScopeTestSpec(BaseModel):
    """Per-scope test specification authored by Agent 1."""

    scope_id: str = Field(
        description="Matches WorkflowStep.id — the scope this spec is for"
    )

    test_mode: str = Field(
        description=(
            "How to generate test cases for this scope. One of: "
            "'file_based' (requires user-uploaded files — Agent 3F), "
            "'synthetic_text' (Agent 3 generates text-based test data), "
            "'synthetic_structured' (Agent 3 generates structured JSON test data). "
            "Determined by Agent 1 based on the sub-task nature and whether "
            "this scope processes files or structured data."
        )
    )

    input_type: str = Field(
        description=(
            "The input_type that test cases for this scope MUST use. "
            "One of: 'text', 'structured_data', 'document_content', "
            "'conversation', 'image_description'. Agent 3/3F must set "
            "this on every test case for this scope."
        )
    )

    output_type: str = Field(
        description=(
            "The output_type that test cases for this scope MUST use. "
            "Matches the WorkflowStep.output_format for this scope. "
            "One of: 'free_text', 'structured_json', 'classification', "
            "'extraction', 'action'."
        )
    )

    input_description: str = Field(
        description=(
            "Human-readable description of what a good test input looks "
            "like for this scope. Example: 'A photo or PDF of a real "
            "invoice with vendor name, line items, amounts, and dates.'"
        )
    )

    expected_output_description: str = Field(
        description=(
            "Human-readable description of the ideal output. Example: "
            "'Structured JSON with vendor_name, line_items[], total, "
            "tax, date fields extracted accurately.'"
        )
    )

    sample_input: str = Field(
        description=(
            "ONE concrete example of what input_data should look like. "
            "For file-based: a text description of the file content. "
            "For downstream steps: a simulated upstream output. "
            "Agent 3 uses this as a template for generating variations."
        )
    )

    sample_output: str = Field(
        description=(
            "ONE concrete example of expected_output. Agent 3 uses this "
            "to understand the shape and content of ideal responses."
        )
    )

    test_count_target: int = Field(
        default=7,
        description=(
            "How many test cases to generate for this scope. "
            "Default 7 (middle of 5-8 range). Agent 1 may increase for "
            "complex scopes or decrease for simple ones."
        )
    )

    upstream_output_shape: str | None = Field(
        default=None,
        description=(
            "For downstream steps (input_from != 'user'): describes the "
            "shape of the upstream step's output so Agent 3 can generate "
            "test inputs that SIMULATE what the upstream step produces. "
            "Example: '{\"vendor_name\": \"...\", \"line_items\": [...], "
            "\"total\": 0.00}'. Null for root steps (input_from='user')."
        )
    )

    requires_user_files: bool = Field(
        default=False,
        description=(
            "True when this scope ideally tests with real user files "
            "(OCR, document parsing, image analysis). Matches "
            "SubTask.requires_test_files. When true and files are "
            "provided, Agent 3F runs for this scope. When true but no "
            "files provided, Agent 3 generates synthetic proxies with "
            "file_required=True flagged."
        )
    )

    file_description: str | None = Field(
        default=None,
        description=(
            "When requires_user_files is True, describes what files the "
            "user should provide. Example: '5-10 sample invoice photos "
            "or PDFs (different vendors, amounts)'. Null when False."
        )
    )

    evaluation_focus: list[str] = Field(
        default_factory=lambda: ["accuracy", "completeness"],
        description=(
            "What the judgement criteria should emphasize for this scope. "
            "Examples: 'accuracy' (correct extraction), 'completeness' "
            "(all fields present), 'format_compliance' (valid JSON), "
            "'latency' (response time matters), 'error_handling' "
            "(graceful degradation on bad input)."
        )
    )


class TestPlan(BaseModel):
    """
    Agent 1's centralized test plan. Consumed by Agent 3/3F.

    Instead of Agent 3/3F independently guessing what tests to generate,
    they execute THIS plan. The plan ensures:
    1. Every scope gets the right test mode (file vs synthetic)
    2. Input/output types match the workflow architecture exactly
    3. Downstream steps get test inputs shaped like upstream outputs
    4. Test count allocation is proportional to scope complexity
    5. Evaluation criteria are scope-appropriate
    """

    scope_specs: list[ScopeTestSpec] = Field(
        description=(
            "One spec per scope in the workflow. Order matches "
            "WorkflowBlueprint.steps order."
        )
    )

    total_test_target: int = Field(
        description="Sum of all scope_specs[].test_count_target."
    )

    notes: str = Field(
        default="",
        description=(
            "Agent 1's reasoning about the test strategy — why certain "
            "scopes get more tests, what evaluation dimensions matter most."
        )
    )


class UserUnderstandingOutput(BaseModel):
    """
    The FULL structured output when Agent 1 has enough information.
    Feeds into Agent 2 (Research) and Agent 3 (Synthetic Tests).
    """

    summary: str = Field(
        description="One clear sentence restating what the user needs AI to do"
    )

    # Sub-tasks: the decomposed capabilities needed
    sub_tasks: list[SubTask] = Field(
        description=(
            "The user's request broken down into independent capability areas. "
            "Each sub-task can potentially be handled by a different AI tool. "
            "Minimum 1 sub-task."
        )
    )

    # Search strategy is ALWAYS "both" — we search for all-in-one AND modular
    # solutions, then present both approaches in the final report. The user
    # decides AFTER seeing real results, not before.
    # This field is kept for downstream compatibility but always set to "both".
    search_strategy: str = Field(
        default="both",
        description="Always 'both' — Research Agent searches all-in-one AND modular approaches"
    )

    domain: str = Field(
        description="The business domain, e.g., 'accounting', 'e-commerce', 'healthcare'"
    )

    # Top-level keywords for all-in-one search (used when search_strategy
    # includes all-in-one). Sub-task-level keywords are in each SubTask.
    search_keywords: list[str] = Field(
        description=(
            "4-8 keywords for finding all-in-one solutions that cover the "
            "full workflow. Only meaningful when search_strategy is "
            "'all_in_one' or 'both'."
        )
    )

    constraints: Constraints = Field(
        description="User's constraints on budget, features, integrations, and technical level"
    )

    workflow_summary: str | None = Field(
        default=None,
        description="Summary of the uploaded workflow file, or null if no file"
    )

    # ── Phase 3: the ordered workflow blueprint ──
    # Optional so:
    #   1. Saved artifacts from before Phase 3 still parse (default None).
    #   2. Agent 1 can legitimately return None for edge cases where the
    #      workflow structure is genuinely unknowable from the user input.
    # Downstream phases should branch on `workflow is not None` — when None,
    # they fall back to the flat `sub_tasks` list (legacy behavior).
    workflow: WorkflowBlueprint | None = Field(
        default=None,
        description=(
            "The ordered structure of the user's workflow — steps, roles, data "
            "flow, architecture options. Authored by Agent 1 alongside sub_tasks. "
            "Downstream phases (Phase 4 dual search, Phase 6 selection UI, "
            "Phase 9 chained harnesses) consume this. None means the workflow "
            "couldn't be determined or this output predates Phase 3."
        )
    )

    # ── Test Plan: Agent 1 as test director ──
    # Agent 1 specifies per-scope test specs so Agent 3/3F execute a plan
    # instead of guessing independently. Optional for backward compat.
    test_plan: TestPlan | None = Field(
        default=None,
        description=(
            "Per-scope test specifications authored by Agent 1 alongside "
            "the workflow blueprint. Agent 3/3F consume this to generate "
            "correctly-shaped test data for each scope. None means Agent "
            "3/3F fall back to independent generation (pre-TestPlan behavior)."
        )
    )

    # NOTE: Test file requirements are now PER SUB-TASK, not global.
    # Each SubTask has its own requires_test_files and test_file_description.
    # This allows mixed evaluations (e.g., OCR needs files + chatbot is text-only).


class InfoStatus(BaseModel):
    """
    Tracks what information has been collected vs what's still needed.

    This is the systematic categorization of required vs optional info.
    The agent updates this on every turn so the CLI/frontend can:
      - Show users what's been captured
      - Know exactly why the agent is asking more questions
      - Display optional fields as "you can also tell us about..."

    CRITICAL = must have before we can search. Without these, the Research
    Agent will return garbage results. Agent MUST ask for these.

    OPTIONAL = improves results but doesn't block the search. Agent should
    invite the user to provide these but never demand them.
    """

    # ── CRITICAL FIELDS (block search without these) ──

    has_concrete_subtasks: bool = Field(
        description="True if at least 1 sub-task with testable input→output behavior has been identified"
    )

    has_domain: bool = Field(
        description="True if the business domain/industry has been identified or can be inferred"
    )

    # ── OPTIONAL FIELDS (improve results, don't block) ──

    has_budget: bool = Field(
        default=False,
        description="True if the user mentioned a budget range"
    )

    has_technical_level: bool = Field(
        default=False,
        description="True if we know the user's technical capability (inferred or stated)"
    )

    has_integration_requirements: bool = Field(
        default=False,
        description="True if the user mentioned specific tools/platforms they need to integrate with"
    )

    has_workflow_file: bool = Field(
        default=False,
        description="True if the user uploaded a workflow document"
    )


class ClarifyingResponse(BaseModel):
    """
    Returned when the agent needs more information.

    Separates critical questions (MUST answer) from optional prompts
    (nice to have). The frontend can present these differently — e.g.,
    critical questions as required fields, optional as expandable hints.
    """

    message: str = Field(
        description=(
            "A conversational message that: (1) acknowledges what was understood, "
            "(2) shows the sub-task breakdown if identified, and (3) naturally "
            "leads into the questions. Should feel like a smart consultant."
        )
    )

    critical_questions: list[str] = Field(
        description=(
            "1-2 questions for CRITICAL missing information that blocks the search. "
            "These correspond to InfoStatus fields that are False. "
            "If has_concrete_subtasks is False, ask what specific tasks they need. "
            "If has_domain is False, ask what kind of business/work this is for."
        )
    )

    optional_prompt: str | None = Field(
        default=None,
        description=(
            "A single, casual invitation for optional info. Example: "
            "'If you'd like, you can also share your rough budget and any "
            "tools you currently use (like Shopify, QuickBooks, etc.) — "
            "this helps us narrow down the best options, but it's totally fine "
            "to skip this.' Set to null if there's nothing useful to optionally collect."
        )
    )

    partial_understanding: str = Field(
        description="What the agent understood so far — preserves context for next turn"
    )

    info_status: InfoStatus = Field(
        description="Systematic tracking of what information has been collected so far"
    )


class Agent1Result(BaseModel):
    """
    Branching wrapper: either a full result OR follow-up questions.

    is_clear=True  → result is populated (ready for downstream agents)
    is_clear=False → clarification_needed is populated (need more conversation)
    """

    is_clear: bool = Field(
        description=(
            "True if the agent has enough information to produce a high-quality "
            "search that will find the right AI solutions. False if more "
            "conversation would meaningfully improve the search results."
        )
    )

    result: UserUnderstandingOutput | None = Field(
        default=None,
        description="Full structured output — only populated when is_clear is True"
    )

    clarification_needed: ClarifyingResponse | None = Field(
        default=None,
        description="Follow-up questions — only populated when is_clear is False"
    )

    cost_usd: float = Field(
        default=0.0,
        description="Total API cost for this agent run",
    )


# ============================================================================
# Agent 2 Input Schema
# ============================================================================

class Agent2Input(BaseModel):
    """
    What gets passed INTO Agent 2. Created by the pipeline orchestrator
    after Agent 1 produces a final UserUnderstandingOutput.

    The orchestrator takes Agent 1's result (when is_clear=True) and wraps
    it here along with the trace_id for log correlation.
    """

    user_understanding: UserUnderstandingOutput = Field(
        description="Agent 1's final structured output — the parsed user request"
    )

    trace_id: str = Field(
        description="UUID for correlating logs across the entire pipeline"
    )


# ============================================================================
# Phase 5: PricingBreakdown — structured pricing (populated by Phase 6.5)
# ============================================================================
# Agent 2's loose `pricing_model` / `pricing_details` strings are fine for
# surveying the landscape but useless for "how much will this actually cost
# me per month for X volume?" — that question needs structured tiers,
# overage costs, and per-scope-unit rates when the provider charges
# differently across scopes (e.g. OCR per page vs sync per event).
#
# Phase 6.5's 4B extraction populates this during deep-verify, at the
# same time it extracts endpoints — no separate research call. For
# candidates that never reach deep-verify (rejected, or user never picked
# them), pricing_breakdown stays None and downstream falls back to the
# legacy pricing_model / pricing_details strings.
#
# The `sources` field is load-bearing: the UI surfaces it so users can
# verify pricing themselves, and Phase 6.5's 4B prompt is instructed to
# only populate a tier when it has a source URL backing it.
# ============================================================================


class PricingTier(BaseModel):
    """One pricing tier. Tiers are ordered cheapest-first in a PricingBreakdown."""

    name: str = Field(
        description='Human-readable tier name, e.g. "Free", "Starter", "Pro", "Enterprise"'
    )

    monthly_cost_usd: float = Field(
        description=(
            "Flat monthly base cost in USD for this tier (0 for free / "
            "pay-as-you-go tiers). Does NOT include per-unit overage."
        )
    )

    included_units: int | None = Field(
        default=None,
        description=(
            "Units included in the monthly base cost. Null when the tier "
            "is pure pay-as-you-go (no included allowance)."
        ),
    )

    unit_name: str | None = Field(
        default=None,
        description=(
            'The unit being metered: "pages", "calls", "tokens", "events", '
            '"documents". Null when the tier is flat-rate with no metering.'
        ),
    )

    overage_cost_per_unit_usd: float | None = Field(
        default=None,
        description=(
            "Cost per unit once included_units is exhausted (or per unit "
            "from zero for pay-as-you-go tiers). Null when the tier has "
            "no overage / hard-cap behavior."
        ),
    )

    notes: str | None = Field(
        default=None,
        description=(
            "Freeform caveats: annual-only pricing, regional discounts, "
            "volume commitments. Rendered in the UI tier tooltip."
        ),
    )


class PricingBreakdown(BaseModel):
    """
    Structured pricing for a candidate. Populated by Phase 6.5's 4B
    extraction turn-phase; null for candidates never deep-verified.
    """

    tiers: list[PricingTier] = Field(
        description=(
            "Pricing tiers, ordered CHEAPEST FIRST. Must have at least one "
            "entry — a candidate that offers only 'enterprise contact sales' "
            "should be REJECTED in Phase 6.5 as enterprise_only, not have an "
            "empty tier list here."
        )
    )

    free_tier_monthly_units: int | None = Field(
        default=None,
        description=(
            "If the candidate has a genuinely free tier (no trial, no "
            "credit card, self-service signup), the monthly unit allowance "
            "of that tier. Null when no free tier exists. Redundant with "
            "tiers[0].included_units when tiers[0].monthly_cost_usd == 0, "
            "but callout'd as a top-level field because Phase 7's selection "
            "scoring uses it as a fast yes/no signal."
        ),
    )

    pay_as_you_go: bool = Field(
        default=False,
        description=(
            "True when the candidate supports true pay-as-you-go with no "
            "monthly minimum (paying only for what you use). Different from "
            "free-tier — a paid tier at $0 base + $0.01/call IS pay-as-you-go."
        ),
    )

    billing_granularity: str = Field(
        default="monthly",
        description=(
            "How the provider bills: 'monthly' (most common), 'per_call' "
            "(pure PAYG with no statement cycle), 'annual_commit' (requires "
            "up-front year), 'hybrid' (mix of monthly + per-call overage)."
        ),
    )

    per_scope_unit_cost: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-scope-id unit cost when the provider charges DIFFERENTLY "
            "across the scopes this candidate covers. Example: Zapier "
            "covering OCR scope at $0.10/page and sync scope at $0.005/event "
            "would set {'step_1': 0.10, 'step_2': 0.005}. Empty dict = "
            "uniform pricing (fall back to tiers[*].overage_cost_per_unit_usd). "
            "Keys must be a subset of Candidate.covers_step_ids — validator "
            "catches drift."
        ),
    )

    sources: list[str] = Field(
        description=(
            "Canonical URLs that back this pricing. Must have at least one "
            "entry — usually the provider's /pricing page, sometimes the "
            "API docs page when pricing is inlined. Surfaced in the UI so "
            "users can sanity-check the numbers themselves."
        )
    )

    confidence: str = Field(
        default="medium",
        description=(
            "Extractor's confidence in the accuracy of the parsed tiers. "
            'Values: "high" (pricing page parsed cleanly with explicit tier '
            'table), "medium" (tiers inferred from marketing copy or partial '
            'docs), "low" (couldn\'t find a pricing page; numbers are guesses '
            "from roundup articles). A run with many 'low' entries is a "
            "signal that Phase 6.5's 4B pricing hunt is underperforming."
        ),
    )

    notes: str | None = Field(
        default=None,
        description=(
            "Freeform caveats affecting overall pricing: volume commits, "
            "annual-only discounts, region-specific pricing, hidden fees."
        ),
    )


# ============================================================================
# Phase 6: UserAddedCandidate — candidates submitted via the SelectionPanel
# ============================================================================
# After Agent 2 produces the candidate pool and the pipeline pauses, the
# user can optionally add providers that Agent 2 didn't surface — usually
# tools the user already knows, private/niche APIs, or in-house services.
# The `covers_step_ids` are explicit (user declares which scopes they
# want this provider tested at). source="user_provided" so downstream
# logs can distinguish search-found from user-added candidates.
# ============================================================================

class UserAddedCandidate(BaseModel):
    """A user-supplied candidate submitted during the Phase 6 pause."""

    name: str = Field(
        description="Service/product name, e.g. 'MyInternalOCR', 'Acme Doc API'"
    )

    provider: str = Field(
        description="Company or organization behind the service"
    )

    api_docs_url: str | None = Field(
        default=None,
        description=(
            "Optional URL to API docs. When provided, Phase 6.5's deep-verify "
            "loop starts here. When null, Phase 6.5 does its own discovery "
            "pass via web_search."
        ),
    )

    notes: str | None = Field(
        default=None,
        description=(
            "Free-form user note. Surfaced in the UI tooltip so downstream "
            "agents know what the user had in mind."
        ),
    )

    covers_step_ids: list[str] = Field(
        description=(
            "Blueprint step IDs this provider covers — REQUIRED. At least "
            "one scope must be specified. Every ID must correspond to a "
            "step in the current blueprint; the select-candidates route "
            "validates this."
        )
    )

    source: str = Field(
        default="user_provided",
        description=(
            "Provenance tag. Always 'user_provided' for UserAddedCandidate; "
            "kept as a field so the Candidate record emitted by "
            "inject_user_candidates carries it through unchanged."
        ),
    )


# ============================================================================
# Agent 2 Output Schemas
# ============================================================================

class Candidate(BaseModel):
    """
    A single AI service/product found during research.

    WHY THESE FIELDS?
    Different downstream agents need different fields:
      - Screening Agent (Agent 4) needs api_available, api_docs_url, and
        claimed_capabilities to validate candidates quickly.
      - Implement Test Env Agent (Agent 5) needs api_docs_url to read docs
        and build a test harness.
      - Ranking Agent (Agent 8) needs pricing_model and pricing_details
        to calculate the Price score.
      - relevant_subtasks tells us which parts of the user's request this
        candidate covers — important for coverage analysis.

    WHY relevance_score?
    This is the Research Agent's own confidence that this service can do
    the job. It's used for prioritization when the Screening Agent needs
    to decide which candidates to investigate first. It's NOT shown to the
    user — the user sees Performance/Speed/Price scores from Agent 8.
    """

    name: str = Field(
        description="Service/product name, e.g., 'Google Document AI', 'Pinecone'"
    )

    provider: str = Field(
        description="Company or organization behind the service, e.g., 'Google', 'Pinecone Inc.'"
    )

    description: str = Field(
        description=(
            "1-2 sentence description of what this service does, focused on "
            "the capabilities relevant to the user's sub-tasks"
        )
    )

    api_available: bool = Field(
        description=(
            "True if the service has a publicly accessible API. "
            "In V0, this should always be True — candidates without APIs "
            "should not be included."
        )
    )

    api_docs_url: str | None = Field(
        default=None,
        description=(
            "URL to the service's API documentation or developer portal. "
            "Used by Agent 5 to read docs and build test harnesses. "
            "Null only if docs URL couldn't be confirmed via web fetch."
        )
    )

    pricing_model: str = Field(
        description=(
            "How the service charges: 'per-token', 'per-request', 'per-page', "
            "'monthly', 'usage-based', 'free-tier', or 'freemium'. "
            "This is the PRIMARY pricing model — some services have multiple."
        )
    )

    pricing_details: str | None = Field(
        default=None,
        description=(
            "Human-readable pricing info, e.g., '$0.01 per 1K tokens', "
            "'Free up to 1000 pages/mo, then $0.01/page', '$99/mo for 10K requests'. "
            "Null if pricing couldn't be determined from search results."
        )
    )

    claimed_capabilities: list[str] = Field(
        description=(
            "Capabilities this service claims to have, relevant to the user's "
            "sub-tasks. Example: ['document OCR', 'table extraction', 'handwriting recognition']. "
            "These are claims from the provider — the Screening Agent verifies them."
        )
    )

    relevance_score: float = Field(
        description=(
            "0.0 to 1.0 — the Research Agent's confidence that this candidate "
            "can handle the user's sub-tasks. Based on: capability match, "
            "API maturity, pricing fit, and coverage breadth. "
            "Used for prioritization, not shown to the user."
        )
    )

    adoption_difficulty: str = Field(
        description=(
            "How hard it is for the user to adopt this service, considering "
            "their technical level and the service's setup requirements. "
            "One of: "
            "'easy' (signup → API key → simple REST calls, good docs with "
            "quickstart examples, no cloud infrastructure needed), "
            "'medium' (requires OAuth setup, SDK installation with moderate "
            "configuration, or a managed platform account with some setup), "
            "'hard' (requires cloud provider account, IAM/service accounts, "
            "region configuration, resource provisioning, or significant "
            "infrastructure knowledge). "
            "Scored relative to the user's technical level from constraints."
        )
    )

    relevant_subtasks: list[str] = Field(
        description=(
            "Which of the user's sub-task descriptions this candidate covers. "
            "Use the exact sub-task description strings from UserUnderstandingOutput. "
            "A candidate may cover one sub-task (specialized) or many (all-in-one)."
        )
    )

    source: str = Field(
        description=(
            "Where this candidate was found. Typically a URL from web search "
            "results (e.g., 'https://cloud.google.com/document-ai'). "
            "Use 'training knowledge' only if the candidate was known to the "
            "model without web search confirmation."
        )
    )

    # ── Phase 4: arbitrary-coverage scope sets ──
    # These two fields are the contract between Agent 2 (dual search) and
    # Phase 7 (per-scope top-K selection) / Phase 6.5 (deep verify). They
    # replace the old concept of "workflow_role" / simple-grouping — every
    # candidate is just a coverage SET, arbitrary in shape.
    covers_step_ids: frozenset[str] = Field(
        default_factory=frozenset,
        description=(
            "Blueprint step IDs this candidate CLAIMS to cover. Populated "
            "by Agent 2 from search snippets — specialists surfaced in a "
            "per-scope search default to {that_one_step_id}; all-in-ones "
            "surfaced in the horizontal survey get their full claimed "
            "coverage (e.g. {'step_1', 'step_2', 'step_3'}). Coverage is "
            "ARBITRARY — no special multi-step category — a provider may "
            "cover 1, 2, or all N scopes and competes equally at every "
            "scope it claims. Dedup happens by candidate name: a tool "
            "surfaced in multiple searches merges its claimed scope sets. "
            "Empty frozenset for legacy flat flow (no blueprint, or dual "
            "search disabled). Phase 6.5's Agent 4 deep-verify is "
            "AUTHORITATIVE — it can remove unverifiable scopes from this "
            "set and upgrade verified ones in coverage_confidence."
        ),
    )
    coverage_confidence: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-scope-id confidence tag. Values: 'claimed' (Agent 2's "
            "initial guess from search snippets) or 'verified' (Phase "
            "6.5 confirmed from docs). Keys align with covers_step_ids. "
            "All Agent 2 output is 'claimed'; the UI shows a small "
            "unverified dot next to such scopes so the user sees what "
            "still has to be validated. Empty dict mirrors empty "
            "covers_step_ids (legacy flat flow)."
        ),
    )

    # ── Phase 5: structured pricing (populated by Phase 6.5's 4B extraction) ──
    # Agent 2 never fills this — it's None until Phase 6.5 deep-verifies the
    # candidate. The legacy `pricing_model` / `pricing_details` strings above
    # remain authoritative for candidates that never reach deep-verify.
    pricing_breakdown: PricingBreakdown | None = Field(
        default=None,
        description=(
            "Structured pricing populated by Phase 6.5's 4B extraction. "
            "None for Agent 2 output and for any candidate that never "
            "reaches deep-verify (rejected, or user never picked them). "
            "When present, takes precedence over pricing_model / "
            "pricing_details for per-scope cost summaries and monthly "
            "budget estimates."
        ),
    )


class Agent2Result(BaseModel):
    """
    Agent 2's complete output. Consumed by Agent 4 (Screening).

    WHY 5-7 CANDIDATES?
    The Screening Agent will reject candidates that fail quick validation
    (no API access, capabilities don't match, rate limits too low). By
    overshooting to 5-7, we ensure 3-5 candidates survive screening —
    enough for meaningful comparison in the testing phases.
    """

    candidates: list[Candidate] = Field(
        description=(
            "5-7 candidate AI services found during research. "
            "Must include at least 4 different providers for diversity. "
            "Should include both all-in-one and specialized candidates."
        )
    )

    search_approach: str = Field(
        description=(
            "Brief summary of how the research was conducted: what was searched, "
            "how many searches/fetches were performed, which comparison articles "
            "were most useful. For debugging and transparency."
        )
    )

    coverage_notes: str = Field(
        description=(
            "Analysis of sub-task coverage: which sub-tasks have strong candidate "
            "options, which are underserved, and any gaps. Example: "
            "'Document OCR has 5 strong candidates. QuickBooks integration has "
            "fewer direct options — may need API glue between an OCR tool and "
            "QuickBooks API.'"
        )
    )

    cost_usd: float = Field(
        default=0.0,
        description="Total API cost including web search fees",
    )


# ============================================================================
# Agent 3 Input Schema
# ============================================================================

class Agent3Input(BaseModel):
    """
    What gets passed INTO Agent 3. Created by the pipeline orchestrator
    after Agent 1 produces a final UserUnderstandingOutput.

    Agent 3 consumes the SAME Agent 1 output as Agent 2, and runs in
    PARALLEL with Agent 2. Agent 2 finds candidates; Agent 3 generates
    test cases. Neither depends on the other.

    TWO MODES:
      - Text mode (test_file_paths is None/empty): generates synthetic
        text test cases. Used when the evaluation is text-based.
      - File mode (test_file_paths has paths): reads user-uploaded files,
        generates ground truth and criteria for each. Used when the
        evaluation involves document/image processing.
    """

    user_understanding: UserUnderstandingOutput = Field(
        description="Agent 1's final structured output — the parsed user request"
    )

    trace_id: str = Field(
        description="UUID for correlating logs across the entire pipeline"
    )

    test_file_paths: list[str] | None = Field(
        default=None,
        description=(
            "Paths to user-uploaded test files (invoices, documents, images, "
            "spreadsheets). When provided, Agent 3 reads these files and "
            "generates test cases using them as inputs. When None or empty, "
            "Agent 3 generates synthetic text-based test data."
        )
    )


# ============================================================================
# Agent 3 Output Schemas
# ============================================================================

class JudgementCriterion(BaseModel):
    """
    A single criterion for judging a test case result.

    WHY WEIGHTED CRITERIA?
    Different aspects of a response matter differently. For an invoice OCR
    test, extracting the vendor name correctly (weight=0.3) matters more
    than having valid JSON formatting (weight=0.1). Weights let Agent 7
    compute a meaningful quality score instead of treating all criteria equally.

    WHY eval_type?
    Agent 7 needs to know HOW to judge, not just WHAT to judge. "Must
    extract vendor name" could mean exact string match, or semantic
    similarity (e.g., "Acme Corp" vs "ACME CORPORATION"). The eval_type
    tells Agent 7 which comparison method to use, making scoring consistent
    and reproducible across all evaluations.
    """

    criterion: str = Field(
        description=(
            "A specific, measurable criterion for judging the output. "
            "Example: 'Must correctly extract the vendor name', "
            "'Response tone must be professional and empathetic'"
        )
    )

    weight: float = Field(
        description=(
            "Importance weight from 0.0 to 1.0. Weights across all criteria "
            "in a test case should sum to approximately 1.0. Higher weight "
            "= more impact on the test case's quality score."
        )
    )

    eval_type: str = Field(
        description=(
            "How Agent 7 should evaluate this criterion. One of: "
            "'exact_match' (output must match ground truth closely), "
            "'semantic_similarity' (output conveys the same meaning), "
            "'contains_key_info' (output includes specific key facts/fields), "
            "'format_compliance' (output matches expected format like valid JSON), "
            "'subjective_quality' (quality judgment on tone, helpfulness, completeness)"
        )
    )


class TestCase(BaseModel):
    """
    A single test case specification.

    DESIGN PRINCIPLES:
    1. TEXT OR FILE — input_data is always text (synthetic or extracted
       from user-uploaded files). test_file_path references the actual
       file when the test uses user-provided data.
    2. UNIVERSAL — works with any AI service. input_type and output_type
       tell downstream agents how to adapt the data, not what service to use.
    3. TAGGED FOR COVERAGE — tags mark which testing dimension this case
       covers (happy_path, edge_case, etc.) so we can verify completeness.
    """

    id: str = Field(
        description="Unique test case ID, e.g., 'tc-001', 'tc-002'"
    )

    sub_task_ref: str = Field(
        description=(
            "Which sub-task this tests — use the EXACT description string "
            "from UserUnderstandingOutput.sub_tasks[].description"
        )
    )

    scope_id: str | None = Field(
        default=None,
        description=(
            "When a TestPlan exists, the scope (WorkflowStep.id) this test "
            "case targets. Populated by Agent 3 from ScopeTestSpec.scope_id. "
            "When set, scope_routing.group_tests_by_scope uses this as a "
            "deterministic primary key instead of fuzzy sub_task_ref matching. "
            "None for pre-TestPlan test cases (legacy flow)."
        ),
    )

    scenario: str = Field(
        description=(
            "Human-readable description of the test scenario. "
            "Example: 'A standard printed invoice from a US vendor with 3 line items'"
        )
    )

    # ── Input specification (canonical text format) ──

    input_type: str = Field(
        description=(
            "The nature of the input data. Tells Agent 5 how to present it "
            "to each service. One of: "
            "'text' (plain text — chat message, query, document text), "
            "'structured_data' (JSON/CSV data to process), "
            "'document_content' (text representation of a document — invoice, contract, receipt), "
            "'conversation' (multi-turn conversation context), "
            "'image_description' (description of what an image contains)"
        )
    )

    input_data: str = Field(
        description=(
            "The actual test input content, always as text. "
            "For text-based tests: the synthetic input (chat message, query, data). "
            "For file-based tests: text description of the file content "
            "(extracted by reading the user's uploaded file). Used as context "
            "for Agent 7 judging."
        )
    )

    input_context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional metadata about the input. Examples: "
            "{'language': 'en', 'document_format': 'invoice', 'page_count': 1}, "
            "{'conversation_turns': 3, 'user_sentiment': 'frustrated'}"
        )
    )

    # ── User-provided test file (when applicable) ──

    test_file_path: str | None = Field(
        default=None,
        description=(
            "Path to user-uploaded file used as test input. Set when the "
            "test uses a real file provided by the user (invoice photo, PDF, "
            "spreadsheet, etc.). Null for synthetic text-based tests. "
            "Agent 5 sends this file to the AI service being tested."
        )
    )

    file_required: bool = Field(
        default=False,
        description=(
            "True when this test case ideally needs a file but none was provided. "
            "Set by the CLI when sub-tasks have requires_test_files=true but no "
            "--test-files were given. Agent 5 runs these with text input_data "
            "as fallback — if the API returns INCOMPATIBLE, the result is "
            "recorded as a normal failure (not skipped)."
        )
    )

    # ── Expected output ──

    output_type: str = Field(
        description=(
            "What kind of output to expect from the AI service. One of: "
            "'free_text' (natural language response), "
            "'structured_json' (JSON matching a specific schema), "
            "'classification' (category label or labels), "
            "'extraction' (extracted fields from input), "
            "'action' (action to perform — e.g., create record, send message)"
        )
    )

    expected_output: str = Field(
        description=(
            "The ground truth / ideal response. For structured outputs, this "
            "is the expected JSON. For free text, this is an ideal response "
            "that Agent 7 compares against using the judgement criteria."
        )
    )

    # ── Judgement ──

    judgement_criteria: list[JudgementCriterion] = Field(
        description=(
            "Specific, weighted criteria for Agent 7 to judge the output. "
            "Must have at least 2 criteria. Weights should sum to ~1.0."
        )
    )

    difficulty: str = Field(
        description="Test difficulty: 'easy' (happy path), 'medium' (realistic), 'hard' (edge case)"
    )

    tags: list[str] = Field(
        description=(
            "Coverage dimension tags. At least one of: "
            "'happy_path', 'input_variation', 'edge_case', 'scale', "
            "'domain_specific', 'error_resilience'. A test case can have "
            "multiple tags."
        )
    )


class Agent3Result(BaseModel):
    """
    Agent 3's complete output. Contains test cases ready for
    Agent 5 (Integration) to run against candidate AI services.

    WHY DYNAMIC COUNT?
    Fixed counts (like 20) undertest complex requests and overtest simple
    ones. We scale with sub-task count: 5-8 cases per sub-task, with
    bonus cases when workflow data is available for grounding.
    """

    test_cases: list[TestCase] = Field(
        description=(
            "Complete list of test cases. Target: 5-8 per sub-task. "
            "Minimum 10 total, maximum 50 total. Each sub-task must be "
            "covered across multiple difficulty levels and testing dimensions."
        )
    )

    generation_notes: str = Field(
        description=(
            "How the test cases were generated: reasoning about coverage, "
            "which dimensions were prioritized, any gaps or limitations. "
            "For debugging and transparency."
        )
    )

    coverage_summary: dict[str, int] = Field(
        description=(
            "Number of test cases per sub-task. Keys are the exact sub-task "
            "description strings from UserUnderstandingOutput. Values are counts. "
            "Used to verify every sub-task has adequate coverage."
        )
    )

    cost_usd: float = Field(
        default=0.0,
        description="Total API cost for test generation",
    )


# ============================================================================
# Agent 4 Input Schema
# ============================================================================

class Agent4Input(BaseModel):
    """
    What gets passed INTO Agent 4. Created by the pipeline orchestrator
    after Agent 2 produces candidate results.

    Agent 4 takes BOTH Agent 2's candidates AND Agent 1's user understanding.
    It needs the candidates to screen, and the user understanding to check
    capability match against what the user actually needs.
    """

    candidates: Agent2Result = Field(
        description="Agent 2's full output — the candidate list plus research context"
    )

    user_understanding: UserUnderstandingOutput = Field(
        description="Agent 1's final structured output — the parsed user request"
    )

    trace_id: str = Field(
        description="UUID for correlating logs across the entire pipeline"
    )


# ============================================================================
# Agent 4 Output Schemas
# ============================================================================

class ScreenedCandidate(BaseModel):
    """
    A candidate that PASSED screening — verified to have real, publicly
    accessible API documentation.

    WHY A NEW MODEL (not extending Candidate)?
    ScreenedCandidate is the contract between Agent 4 and Agent 5. Agent 5
    needs GUARANTEED enrichment fields (verified docs URL, auth method, data
    formats) to build test harnesses without guessing. Making these fields
    non-optional in a separate model enforces this contract at the schema level.

    The enrichment fields give Agent 5 a head start — it knows the auth
    method, the working docs URL, and what formats to expect BEFORE it
    starts reading API docs itself.
    """

    # ── Carried forward from Candidate (Agent 2) ──

    name: str = Field(
        description="Service/product name, e.g., 'Google Document AI', 'Pinecone'"
    )

    provider: str = Field(
        description="Company or organization behind the service"
    )

    description: str = Field(
        description="1-2 sentence description of what this service does"
    )

    pricing_model: str = Field(
        description=(
            "How the service charges: 'per-token', 'per-request', 'per-page', "
            "'monthly', 'usage-based', 'free-tier', or 'freemium'"
        )
    )

    pricing_details: str | None = Field(
        default=None,
        description="Human-readable pricing info, e.g., '$0.01 per 1K tokens'"
    )

    claimed_capabilities: list[str] = Field(
        description="Capabilities claimed by the provider (from Agent 2 research)"
    )

    relevance_score: float = Field(
        description="0.0 to 1.0 — Agent 2's confidence score for this candidate"
    )

    adoption_difficulty: str = Field(
        description=(
            "Adoption difficulty from Agent 2: 'easy', 'medium', or 'hard'. "
            "Carried forward for Agent 8 ranking and Agent 9 reporting."
        )
    )

    relevant_subtasks: list[str] = Field(
        description="Which of the user's sub-task descriptions this candidate covers"
    )

    source: str = Field(
        description="Where this candidate was originally found (URL or 'training knowledge')"
    )

    # ── NEW: Enrichment fields from screening (for Agent 5) ──

    verified_api_docs_url: str = Field(
        description=(
            "The URL to API documentation that was CONFIRMED to exist and contain "
            "real API documentation (endpoints, authentication, SDKs). This URL "
            "was verified by fetching or searching during screening. Agent 5 uses "
            "this as its starting point for building test harnesses."
        )
    )

    auth_method: str = Field(
        description=(
            "How the API authenticates requests, determined from actual API "
            "documentation found during screening. One of: 'api_key', 'oauth2', "
            "'bearer_token', 'basic_auth', 'no_auth', 'unknown'. Agent 5 uses "
            "this to set up test environment authentication."
        )
    )

    api_access_method: str = Field(
        description=(
            "How a developer gets access to use this API. One of: "
            "'free_signup' (create account, get key immediately), "
            "'free_tier' (usage-limited free access), "
            "'trial' (time-limited trial), "
            "'sandbox' (test environment available), "
            "'open' (no signup needed), "
            "'paid_only' (requires payment upfront). "
            "Agent 5 uses this to determine if it can build a working test harness."
        )
    )

    confirmed_capabilities: list[str] = Field(
        description=(
            "Capabilities verified from actual API documentation or developer "
            "guides found during web fetch/search. These are NOT just the claims "
            "from Agent 2 repeated — they are capabilities confirmed to exist in "
            "the API docs. Should map to the user's sub-tasks where possible."
        )
    )

    rate_limit_info: str | None = Field(
        default=None,
        description=(
            "Rate limit information found in the API documentation. "
            "Example: '100 requests/minute on free tier, 1000/minute on paid'. "
            "Null if no rate limit info was found in docs."
        )
    )

    data_format_notes: str = Field(
        description=(
            "What input/output formats the API accepts, as determined from "
            "documentation. Example: 'Accepts JPEG/PNG/PDF via multipart upload, "
            "returns JSON with extracted fields', 'REST API accepting JSON request "
            "bodies, returns JSON responses'. Agent 5 uses this to build the test "
            "harness adapter."
        )
    )

    screening_notes: str = Field(
        description=(
            "How the screening determination was made — what evidence was found, "
            "what URL was fetched, what content confirmed API access. Provides an "
            "audit trail for the pass decision."
        )
    )

    # ── Phase 6.5: deep-verify enrichments ──
    api_spec_path: str | None = Field(
        default=None,
        description=(
            "Absolute path to api_spec.txt produced by Phase 6.5 deep-verify. "
            "Agent 5 reads this at build time instead of re-researching. "
            "None for candidates not deep-verified (rejected or never selected)."
        ),
    )

    covers_step_ids: frozenset[str] = Field(
        default_factory=frozenset,
        description=(
            "Blueprint step IDs this candidate covers. Phase 6.5 OVERWRITES "
            "with verified truth — scopes that couldn't be verified in 4C "
            "are REMOVED from this set. Empty frozenset for legacy flow."
        ),
    )

    coverage_confidence: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-scope confidence: 'claimed' (from Agent 2) or 'verified' "
            "(confirmed by Phase 6.5's 4C). Keys align with covers_step_ids."
        ),
    )

    # ── Phase 5: structured pricing (populated by Phase 6.5's 4B extraction) ──
    # When Phase 6.5 lands, 4B extracts pricing at the same time as endpoints
    # (same provider domain, often same page). Until 6.5 is live this stays
    # None on every ScreenedCandidate — the field exists so consumers don't
    # have to null-check against a missing attribute, only against a None
    # value.
    pricing_breakdown: PricingBreakdown | None = Field(
        default=None,
        description=(
            "Structured pricing. Populated by Phase 6.5's 4B extraction "
            "turn-phase at the same time as endpoints. None when the "
            "pricing page couldn't be found or parsed, when Phase 6.5 "
            "hasn't shipped yet, or when the candidate predates Phase 5."
        ),
    )


class RejectedCandidate(BaseModel):
    """
    A candidate that FAILED screening — no verified public API access,
    or other disqualifying issue found.

    WHY rejection_category?
    Structured rejection reasons let downstream systems (API, frontend)
    present useful feedback without parsing free-text strings. They also
    enable analytics on WHY candidates fail (e.g., "80% of rejections
    are enterprise-only services — Research Agent should deprioritize those").
    """

    name: str = Field(
        description="Service/product name that was rejected"
    )

    provider: str = Field(
        description="Company or organization behind the rejected service"
    )

    rejection_reason: str = Field(
        description=(
            "Human-readable explanation of why this candidate was rejected. "
            "Should be specific: 'API documentation page returns 404 and web "
            "search found no alternative developer docs' rather than 'no API'."
        )
    )

    rejection_category: str = Field(
        description=(
            "Structured rejection reason. One of: "
            "'no_api_access' (no callable API exists), "
            "'no_public_docs' (API may exist but docs are not publicly accessible), "
            "'capability_mismatch' (API exists but doesn't handle the user's use cases), "
            "'rate_limit_insufficient' (rate limits too low for ~20 test cases), "
            "'no_free_tier' (requires paid access with no trial/sandbox option), "
            "'enterprise_only' (only available through enterprise sales process), "
            "'deprecated' (API is deprecated or being sunset), "
            "'region_restricted' (API not available in required regions)"
        )
    )


class FailedToVerify(BaseModel):
    """Phase 6.5: a candidate that failed deep-verify for a specific scope."""
    name: str = Field(description="Service/product name that failed verification")
    provider: str = Field(description="Company behind the service")
    scope_id: str = Field(description="Which scope slot this verification attempt was for")
    reason: str = Field(
        description=(
            "Rejection category. One of: 'docs_unreachable', 'enterprise_only', "
            "'deprecated', 'no_api', 'coverage_removed_at_scope', 'verify_error'"
        )
    )
    attempt_notes: str = Field(
        default="",
        description="Agent 4's trail of search attempts for audit trail"
    )


class Agent4Result(BaseModel):
    """
    Agent 4's complete output. Consumed by Agent 5 (Implement Test Env).

    WHY total_candidates_screened?
    Cross-check field — validated + rejected should sum to this number.
    If they don't match, a candidate was silently dropped (bug). The
    validator catches this.
    """

    validated_candidates: list[ScreenedCandidate] = Field(
        description=(
            "Candidates that passed screening — verified to have publicly "
            "accessible API documentation. Target: 3-5 candidates. Each has "
            "enrichment fields (auth method, docs URL, data formats) that "
            "give Agent 5 a head start building test harnesses."
        )
    )

    rejected_candidates: list[RejectedCandidate] = Field(
        description=(
            "Candidates that failed screening with specific rejection reasons. "
            "Every candidate from Agent 2 must appear in either validated or "
            "rejected — none should be silently dropped."
        )
    )

    screening_summary: str = Field(
        description=(
            "Overview of the screening process: how many candidates were checked, "
            "what verification method was used (web fetch, web search), and a "
            "brief assessment of the overall candidate quality."
        )
    )

    total_candidates_screened: int = Field(
        description=(
            "Total number of candidates that were screened. Must equal "
            "len(validated_candidates) + len(rejected_candidates). Used as "
            "a cross-check to catch silently dropped candidates."
        )
    )

    cost_usd: float = Field(
        default=0.0,
        description="Total API cost including per-candidate verification and web search fees",
    )

    web_fetch_blocks: int = Field(
        default=0,
        description=(
            "Number of web_fetch attempts that did not produce usable content "
            "across all per-candidate verifications. Combines two failure "
            "surfaces: (1) HTTP-level errors — 403/Cloudflare/429/unavailable "
            "(Phase 1) — and (2) content-level failures — SPA shells, auth "
            "walls, soft 404s, marketing pages with no API signals "
            "(Phase 1.5). Surfaced for observability so the team can spot "
            "providers whose docs are consistently unreachable or unrenderable. "
            "See puzzleeval/web_fetch_fallback.py for both classifiers."
        ),
    )

    failed_to_verify: list[FailedToVerify] = Field(
        default_factory=list,
        description=(
            "Phase 6.5: candidates that failed deep-verify, with per-scope "
            "rejection reasons. Empty for legacy shallow verification."
        ),
    )

    scope_selections: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Phase 7: per-scope verified candidate mapping that Phase 9 "
            "consumes. Keys are scope/step IDs, values are lists of "
            "candidate names selected for that scope. Empty for legacy flow."
        ),
    )


# ============================================================================
# Agent 5 Input/Output Schemas
# ============================================================================
# Agent 5 (Implement Test Env) builds a working Python test harness for each
# validated candidate. It runs N instances in parallel — one per candidate.
#
# Each builder agent is an autonomous tool-use loop: it reads the candidate's
# API docs, writes harness code, runs smoke tests, fixes errors, and repeats
# until the harness passes structural validation.
#
# The harness exposes a standardized `run(input_data) -> dict` interface that
# Agent 5 (Integration) calls to execute test cases. Every harness must
# return the SAME dict shape for fair comparison.
# ============================================================================


class Agent5Input(BaseModel):
    """
    What gets passed INTO each Agent 5 run.

    Contains the validated candidates from Agent 4, plus context from Agent 1
    (user understanding) and Agent 3 (test case formats) so the builder agent
    knows what input/output types to support in the harness.
    """

    validated_candidates: list[ScreenedCandidate] = Field(
        description=(
            "Candidates that passed Agent 4 screening. Each has verified API "
            "docs URL, auth method, data format notes, and confirmed capabilities. "
            "Agent 5 builds one test harness per candidate."
        )
    )

    user_understanding: UserUnderstandingOutput = Field(
        description=(
            "Agent 1's parsed understanding of the user's request. Provides "
            "context on what the user needs (sub-tasks, domain, technical level) "
            "so the builder agent can write harnesses that handle the right "
            "input/output types."
        )
    )

    test_cases: Agent3Result = Field(
        description=(
            "Agent 3's generated test cases. The builder agent uses this to "
            "understand what input_types (text, structured_data, document_content) "
            "and output_types (free_text, structured_json, extraction) the "
            "harness needs to support."
        )
    )

    trace_id: str = Field(
        description="UUID for log correlation across the entire pipeline"
    )

    provider_credentials: dict[str, dict[str, str]] | None = Field(
        default=None,
        description=(
            "Centralized provider credentials from the provider registry. "
            "Maps normalized provider/candidate names to env var dicts. "
            "Used for live API validation during harness building. "
            "None if no registry is configured."
        )
    )


class TestHarness(BaseModel):
    """
    A successfully built test harness for one candidate.

    The harness is a Python module with a `run(input_data: dict) -> dict`
    function that calls the candidate's API and returns standardized results.

    WHY harness_code AND harness_dir?
    harness_dir is the local filesystem path (for Agent 5 to import/run).
    harness_code is the actual Python source (for portability — when the
    harness needs to be shipped to a cloud container or stored in a database,
    the code travels with the result, not tied to a local path).
    """

    candidate_name: str = Field(
        description="Service/product name, e.g., 'Google Document AI'"
    )

    provider: str = Field(
        description="Company or organization behind the service"
    )

    harness_dir: str = Field(
        description=(
            "Absolute path to the sandbox directory containing the harness "
            "code (harness.py, requirements.txt, smoke_test.py). Agent 5 "
            "uses this to import and run the harness."
        )
    )

    entry_file: str = Field(
        description="Filename of the main harness module, always 'harness.py'"
    )

    requirements: list[str] = Field(
        description=(
            "Python packages the harness needs, e.g., ['requests', "
            "'google-cloud-documentai']. Agent 5 installs these before "
            "running tests."
        )
    )

    auth_env_vars: list[str] = Field(
        description=(
            "Environment variable names the harness reads for authentication, "
            "e.g., ['GOOGLE_DOCAI_API_KEY']. Agent 5 must ensure these are "
            "set before running tests. Convention: {PROVIDER}_API_KEY."
        )
    )

    auth_method: str = Field(
        description=(
            "How the API authenticates, carried from ScreenedCandidate. "
            "One of: 'api_key', 'oauth2', 'bearer_token', 'basic_auth', "
            "'no_auth', 'unknown'."
        )
    )

    supported_input_types: list[str] = Field(
        description=(
            "Input types the harness can handle, matching Agent 3's test case "
            "input_type values: 'text', 'structured_data', 'document_content', "
            "'conversation', 'image_description'."
        )
    )

    supported_output_types: list[str] = Field(
        description=(
            "Output types the harness produces, matching Agent 3's test case "
            "output_type values: 'free_text', 'structured_json', "
            "'classification', 'extraction', 'action'."
        )
    )

    smoke_test_passed: bool = Field(
        description=(
            "True if the structural smoke test passed — harness imports, "
            "run() exists with correct signature, returns correct dict shape "
            "with mocked HTTP. This does NOT mean real API calls work."
        )
    )

    live_validation_attempted: bool = Field(
        default=False,
        description=(
            "True if a live API call was attempted during building. "
            "Requires credentials from the provider registry or env vars. "
            "If False, the harness was only validated structurally."
        )
    )

    live_validation_passed: bool | None = Field(
        default=None,
        description=(
            "True if the live API call returned a valid response (even an "
            "error like 'invalid input' counts — it proves the endpoint "
            "exists and auth works). False if auth failed or endpoint not "
            "found. None if live validation was not attempted."
        )
    )

    live_validation_notes: str | None = Field(
        default=None,
        description=(
            "Details of the live validation: what was sent, what came back, "
            "and why it passed or failed. None if not attempted."
        )
    )

    validation_notes: str = Field(
        description=(
            "What validation was performed and what happened. Includes "
            "smoke test output, docs verification results, and any issues "
            "encountered during building."
        )
    )

    build_turns: int = Field(
        description=(
            "How many agent loop iterations (API calls) it took to build "
            "this harness. Typical: 4-8. Max: 15."
        )
    )

    build_cost_usd: float = Field(
        description=(
            "Estimated cost (USD) of building this harness — sum of all "
            "API call costs during the builder loop."
        )
    )

    harness_code: str = Field(
        description=(
            "The complete Python source code of harness.py. Stored here "
            "for portability — when deploying to cloud containers, the code "
            "travels with the result instead of depending on local paths."
        )
    )

    api_knowledge: str | None = Field(
        default=None,
        description=(
            "Comprehensive API understanding from Agent 5's research sub-agent. "
            "Contains the full api_spec with INPUT_COMPATIBILITY matrix, "
            "API_LIMITATIONS, ROUTING_TABLE, and DOC_MAP sections. Agent 5 "
            "reads this directly for test classification — no file I/O or "
            "re-research needed. None if research phase was skipped."
        )
    )

    web_fetch_blocks: int = Field(
        default=0,
        description=(
            "Number of web_fetch attempts that did not produce usable content "
            "during this candidate's build loop. Combines HTTP-level errors "
            "(Cloudflare/403/429/unavailable — Phase 1) and content-level "
            "failures (SPA shells, auth walls, soft 404s — Phase 1.5). "
            "Aggregated into Agent5Result.web_fetch_blocks for run-level "
            "observability."
        ),
    )


class FailedHarness(BaseModel):
    """
    A candidate where harness building failed.

    WHY failure_category?
    Structured failure reasons enable:
    1. Automated backfill requests to Agent 2 (Agent 5's job)
    2. Analytics on why builds fail (e.g., "60% fail due to unusable docs")
    3. Frontend display of actionable failure info
    """

    candidate_name: str = Field(
        description="Service/product name that failed"
    )

    provider: str = Field(
        description="Company or organization behind the failed service"
    )

    failure_reason: str = Field(
        description=(
            "Human-readable explanation of why the harness could not be built. "
            "Should be specific: 'API docs at docs.example.com returned 403 "
            "and no SDK quickstart could be found' rather than 'build failed'."
        )
    )

    failure_category: str = Field(
        description=(
            "Structured failure reason. One of: "
            "'docs_unusable' (API docs too vague, inaccessible, or incomplete), "
            "'auth_blocked' (cannot set up auth without paid account/manual approval), "
            "'api_incompatible' (API exists but doesn't support needed operations), "
            "'build_timeout' (exceeded max turns or budget without passing smoke test), "
            "'dependency_failure' (required packages cannot be installed), "
            "'unknown' (unexpected failure not fitting other categories)"
        )
    )

    partial_code: str | None = Field(
        default=None,
        description=(
            "Last version of the harness code if any was generated before "
            "failure. Useful for debugging and for manual completion."
        )
    )

    turns_attempted: int = Field(
        description="How many agent loop iterations were tried before giving up"
    )

    web_fetch_blocks: int = Field(
        default=0,
        description=(
            "Number of web_fetch attempts that did not produce usable content "
            "before this build was abandoned. Combines HTTP-level errors "
            "(Cloudflare/403/429/unavailable — Phase 1) and content-level "
            "failures (SPA shells, auth walls, soft 404s — Phase 1.5). "
            "Aggregated into Agent5Result.web_fetch_blocks for run-level "
            "observability."
        ),
    )


class ScopeTestRun(BaseModel):
    """Phase 9: test results for one scope in a multi-scope workflow."""
    scope_id: str = Field(description="e.g. 'step_1'")
    scope_role: str = Field(description="e.g. 'ocr' — from WorkflowStep.role")
    candidate_results: list["CandidateTestRun"] = Field(
        default_factory=list,
        description="Ordered by aggregate score descending at this scope"
    )
    test_case_count: int = Field(
        default=0,
        description="How many Agent 3/3F test cases ran at this scope"
    )


class Agent5Result(BaseModel):
    """
    Agent 5's complete output. Consumed by Agent 5 (Integration).

    WHY total_candidates_attempted?
    Cross-check field — harnesses + failed_harnesses should sum to this
    number. If they don't match, a candidate was silently dropped (bug).
    The validator catches this.
    """

    harnesses: list[TestHarness] = Field(
        description=(
            "Successfully built test harnesses, one per candidate. Each "
            "harness exposes a standardized run(input_data) -> dict interface "
            "that Agent 5 calls to execute test cases."
        )
    )

    failed_harnesses: list[FailedHarness] = Field(
        description=(
            "Candidates where harness building failed. Every validated "
            "candidate from Agent 4 must appear in either harnesses or "
            "failed_harnesses — none should be silently dropped."
        )
    )

    total_candidates_attempted: int = Field(
        description=(
            "Total number of candidates that were attempted. Must equal "
            "len(harnesses) + len(failed_harnesses). Used as a cross-check "
            "to catch silently dropped candidates."
        )
    )

    total_build_cost_usd: float = Field(
        description=(
            "Sum of all build costs across all candidates (both successful "
            "and failed). Includes API call tokens + web search costs."
        )
    )

    build_summary: str = Field(
        description=(
            "Overview of the build process: how many succeeded, how many "
            "failed, common failure reasons, and total time/cost."
        )
    )

    # ── Test execution results (merged from Agent 5) ──
    # These fields are populated when Agent 5 runs test cases after building.
    # Default values ensure backward compatibility with existing consumers.

    candidate_runs: list["CandidateTestRun"] = Field(
        default_factory=list,
        description=(
            "Test execution results for each successful harness. "
            "Contains per-test-case results, aggregate metrics, and "
            "evaluation scores. Empty if test execution was skipped."
        )
    )

    failed_test_runs: list["FailedCandidateRun"] = Field(
        default_factory=list,
        description=(
            "Candidates where test execution failed (setup error or "
            ">50%% error rate). Empty if test execution was skipped."
        )
    )

    total_test_cases: int = Field(
        default=0,
        description="Number of test cases from Agent 3"
    )

    total_test_cost_usd: float = Field(
        default=0.0,
        description="Sum of all candidate API costs + evaluation costs during testing"
    )

    test_execution_summary: str = Field(
        default="",
        description=(
            "Summary of test execution: pass rates, costs, incompatible counts. "
            "Empty string if test execution was skipped."
        )
    )

    web_fetch_blocks: int = Field(
        default=0,
        description=(
            "Number of web_fetch attempts that did not produce usable content "
            "summed across every candidate's build loop. Combines HTTP-level "
            "errors (403/Cloudflare/429/unavailable — Phase 1) and "
            "content-level failures (SPA shells, auth walls, soft 404s — "
            "Phase 1.5). Surfaced for observability so the team can spot "
            "providers whose docs are consistently unreachable or "
            "unrenderable. See puzzleeval/web_fetch_fallback.py for both "
            "classifiers."
        ),
    )

    scope_runs: list[ScopeTestRun] = Field(
        default_factory=list,
        description=(
            "Phase 9: one entry per scope that had at least one candidate "
            "tested. A 1-scope workflow produces 1 ScopeTestRun whose "
            "candidate_results matches the legacy candidate_runs list. "
            "Multi-scope workflows produce N entries. Empty for legacy "
            "flow (pre-Phase-9 outputs)."
        ),
    )


# ============================================================================
# Agent 5 Output Schemas
# ============================================================================

class CriterionScore(BaseModel):
    """
    Result of evaluating one judgement criterion against one test case output.

    WHY BOTH score AND passed?
    score (0.0-1.0) gives granular quality signal for Agent 7's ranking.
    passed (bool) gives a binary verdict for the user-facing pass rate metric.
    Both are needed for different consumers.
    """

    criterion: str = Field(
        description="The criterion text from JudgementCriterion"
    )

    eval_type: str = Field(
        description=(
            "How this criterion was evaluated. One of: 'exact_match', "
            "'semantic_similarity', 'contains_key_info', 'format_compliance', "
            "'subjective_quality'"
        )
    )

    weight: float = Field(
        description="Importance weight from 0.0 to 1.0, from JudgementCriterion"
    )

    score: float = Field(
        description=(
            "Degree of satisfaction from 0.0 (completely fails) to 1.0 "
            "(fully satisfies). For mechanical evaluation: binary 0.0 or 1.0. "
            "For LLM evaluation: continuous scale."
        )
    )

    passed: bool = Field(
        description="True if score >= 0.5. Used for binary pass rate metrics."
    )

    reasoning: str = Field(
        description=(
            "Why this score was given. For mechanical evaluation: 'Exact match: "
            "expected X, found X in output'. For LLM evaluation: model's "
            "explanation of its judgment."
        )
    )


class TestCaseResult(BaseModel):
    """
    Complete result of executing and evaluating one test case against one harness.

    Combines execution metrics (latency, cost, tokens) with quality evaluation
    (criteria scores, weighted score, pass/fail). This is the most granular
    result unit — Agent 7 uses these for cross-candidate quality analysis.
    """

    test_case_id: str = Field(
        description="The test case ID from Agent 3, e.g., 'tc-001'"
    )

    sub_task_ref: str = Field(
        description="Which sub-task this tests, carried from TestCase for grouping"
    )

    input_sent: dict = Field(
        description=(
            "Exact input_data dict passed to harness.run(). Recorded for "
            "debugging and for Agent 7 to see what was actually tested."
        )
    )

    output_received: str = Field(
        description="The harness output['output'] string — what the API returned"
    )

    raw_response: dict = Field(
        description=(
            "The harness output['raw_response'] dict — full API response "
            "for debugging and deep analysis."
        )
    )

    latency_ms: float = Field(
        description="Round-trip API call time in milliseconds, measured by harness"
    )

    tokens_used: dict[str, int] | None = Field(
        default=None,
        description=(
            "Token usage if reported by the API: {'input': N, 'output': M}. "
            "None if the API doesn't report token counts."
        )
    )

    cost_usd: float | None = Field(
        default=None,
        description=(
            "Per-call cost in USD if known from the API response. None if "
            "the API doesn't report costs."
        )
    )

    success: bool = Field(
        description=(
            "True if the API call succeeded (harness returned success=True). "
            "False means the API errored — not a quality judgment."
        )
    )

    error: str | None = Field(
        default=None,
        description="Error message from the harness if success=False, else None"
    )

    skip_reason: str | None = Field(
        default=None,
        description=(
            "Reason this test was skipped (not executed/evaluated). "
            "Set when the harness returns INCOMPATIBLE for a non-file_required "
            "test. file_required tests with no file are NOT skipped — they are "
            "run with text input_data as fallback and recorded as normal "
            "failures if the API cannot handle text-only input."
        )
    )

    criteria_scores: list[CriterionScore] = Field(
        description=(
            "Per-criterion evaluation results. One CriterionScore per "
            "JudgementCriterion from the test case. Empty list if the API "
            "call failed (success=False) — no output to evaluate."
        )
    )

    weighted_score: float = Field(
        description=(
            "Overall quality score from 0.0 to 1.0, computed as the "
            "weighted sum of criteria scores. 0.0 if API call failed."
        )
    )

    passed: bool = Field(
        description=(
            "True if weighted_score >= pass threshold (default 0.5) AND "
            "success=True. A test case can only pass if the API call "
            "succeeded AND the output met quality criteria."
        )
    )


class CandidateTestRun(BaseModel):
    """
    Complete test execution and evaluation results for one candidate.

    One instance per successful harness from Agent 5. Contains all individual
    test results plus aggregate metrics. Agent 7 consumes these for
    cross-candidate quality analysis.
    """

    candidate_name: str = Field(
        description="Service/product name, e.g., 'Klippa', 'Mindee'"
    )

    provider: str = Field(
        description="Company or organization behind the service"
    )

    harness_dir: str = Field(
        description="Path to the sandbox directory used for execution"
    )

    status: str = Field(
        description=(
            "Execution status. 'completed' = all tests ran and were evaluated. "
            "'partial' = some tests ran before early abort. "
            "'failed' = setup failed or >50%% error rate."
        )
    )

    test_results: list[TestCaseResult] = Field(
        description="Individual results for each test case executed"
    )

    # ── Aggregate metrics ──
    total_tests: int = Field(
        description="Total number of test cases attempted"
    )

    tests_passed: int = Field(
        description="Test cases where passed=True (API succeeded AND quality met threshold)"
    )

    tests_failed: int = Field(
        description="Test cases where passed=False but success=True (quality below threshold)"
    )

    tests_errored: int = Field(
        description="Test cases where success=False (API call failed)"
    )

    tests_skipped: int = Field(
        default=0,
        description=(
            "Test cases skipped due to INCOMPATIBLE input type or missing files. "
            "These are not errors — they are legitimate API limitations."
        )
    )

    success_rate: float = Field(
        description="Fraction of tests where API call succeeded: (total - errored) / total"
    )

    pass_rate: float = Field(
        description=(
            "Fraction of successful tests that met quality threshold: "
            "tests_passed / (total - errored). 0.0 if all errored."
        )
    )

    avg_latency_ms: float = Field(
        description="Mean latency across successful test cases"
    )

    p95_latency_ms: float = Field(
        description="95th percentile latency across successful test cases"
    )

    total_cost_usd: float = Field(
        description="Sum of per-test cost_usd from harness (candidate API costs)"
    )

    total_tokens: dict[str, int] | None = Field(
        default=None,
        description="Aggregated token usage: {'input': N, 'output': M}. None if unavailable."
    )

    evaluation_cost_usd: float = Field(
        default=0.0,
        description="Cost of the LLM evaluation call for this candidate"
    )

    refinement_cost_usd: float = Field(
        default=0.0,
        description="Cost of the harness refinement agent for this candidate"
    )

    refinement_changes: str = Field(
        default="none",
        description=(
            "Description of changes made during refinement. 'none' if harness "
            "was already correct. Helps debugging and transparency."
        )
    )

    incompatible_test_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Test case IDs marked as genuinely incompatible with this API. "
            "These are legitimate API limitations, not harness bugs."
        )
    )

    recovery_attempted: bool = Field(
        default=False,
        description="True if the recovery agent was invoked for this candidate"
    )

    recovery_cost_usd: float = Field(
        default=0.0,
        description="Cost of recovery agent calls, if any"
    )

    execution_duration_ms: float = Field(
        default=0.0,
        description="Wall-clock time for this candidate's entire execution"
    )


class FailedCandidateRun(BaseModel):
    """
    A candidate where test execution failed — either setup failure or >50%
    error rate during execution.

    WHY separate from CandidateTestRun?
    Failed candidates have different data: no quality metrics, no evaluation.
    Keeping them separate makes the Agent5Result cleaner for downstream consumers
    (Agent 7 only processes CandidateTestRun, ignores FailedCandidateRun).
    """

    candidate_name: str = Field(
        description="Service/product name that failed"
    )

    provider: str = Field(
        description="Company or organization behind the failed service"
    )

    failure_reason: str = Field(
        description=(
            "Human-readable explanation: 'Setup failed: cannot import harness', "
            "'Error rate 78%% after 9 tests — early abort', etc."
        )
    )

    error_rate: float = Field(
        description="Fraction of tests that returned errors (success=False)"
    )

    tests_attempted: int = Field(
        description="How many tests were run before failure/abort"
    )

    tests_errored: int = Field(
        description="How many tests returned errors"
    )

    sample_errors: list[str] = Field(
        description="First 3 distinct error messages for debugging"
    )

    recovery_attempted: bool = Field(
        description="True if the recovery agent was invoked"
    )


# ── LLM Evaluation Structured Output Models ──
# Used by client.messages.parse() in Agent 5's Phase 3 evaluation call.
# These are NOT part of the pipeline output — they are intermediate models
# for structuring the LLM's evaluation response.

class CriterionScoreOutput(BaseModel):
    """LLM's evaluation of a single criterion."""
    criterion: str = Field(description="The criterion text being evaluated")
    score: float = Field(description="Score from 0.0 to 1.0")
    passed: bool = Field(description="True if score >= 0.5")
    reasoning: str = Field(description="One sentence explaining the judgment")


class TestCaseEvaluation(BaseModel):
    """LLM's evaluation of all criteria for one test case."""
    test_case_id: str = Field(description="The test case ID, e.g., 'tc-001'")
    criteria_scores: list[CriterionScoreOutput] = Field(
        description="Evaluation of each semantic/subjective criterion"
    )


class EvaluationBatchResult(BaseModel):
    """LLM's batch evaluation of all test cases for one candidate."""
    evaluations: list[TestCaseEvaluation] = Field(
        description="One evaluation per test case that had LLM-evaluated criteria"
    )
