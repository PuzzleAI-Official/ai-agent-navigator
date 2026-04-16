from pydantic import BaseModel, Field
from typing import Literal, Optional


# Phase 2: Service tier scaffold. Three plans for now; new ones added in
# billing.py's matrices automatically become legal here via the Literal union.
Plan = Literal["free", "paid", "enterprise"]


class CreateRunRequest(BaseModel):
    text: str
    file_ids: list[str] = Field(default_factory=list)
    agent_modes: dict[str, str] = Field(
        default_factory=lambda: {
            "agent1": "mock",
            "agent2": "mock",
            "agent3": "mock",
            "agent4": "mock",
            "agent5": "mock",
        },
        description="Per-agent mode: 'mock' (replay saved data, $0) or 'real' (call Claude API)",
    )
    plan: Plan = Field(
        default="free",
        description=(
            "User-facing PuzzleAI plan. Drives credit allocation and feature "
            "gating in services/billing.py. Defaults to 'free' so existing "
            "callers are unchanged."
        ),
    )


class CreateRunResponse(BaseModel):
    run_id: str
    trace_id: str


class ChatRequest(BaseModel):
    message: str
    file_ids: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    is_clear: bool
    assistant_message: str
    clarifying_questions: list[str] = Field(default_factory=list)
    pipeline_started: bool = False


class UploadFilesResponse(BaseModel):
    file_ids: list[str]
    filenames: list[str]


class CancelRunResponse(BaseModel):
    cancelled: bool


class CriterionScoreOut(BaseModel):
    criterion: str
    score: float
    passed: bool
    reasoning: str


class TestResultOut(BaseModel):
    test_case_id: str
    passed: bool
    weighted_score: float
    latency_ms: float
    criteria_scores: list[CriterionScoreOut] = Field(default_factory=list)


class CandidateOut(BaseModel):
    name: str
    provider: str
    description: str = ""
    relevance_score: float = 0.0
    adoption_difficulty: str = "medium"
    claimed_capabilities: list[str] = Field(default_factory=list)
    confirmed_capabilities: list[str] = Field(default_factory=list)
    auth_method: Optional[str] = None
    api_access_method: Optional[str] = None
    verified_api_docs_url: Optional[str] = None
    harness_status: str = "pending"
    test_status: str = "pending"
    # Test results (populated after Agent 5)
    overall_score: Optional[float] = None
    pass_rate: Optional[float] = None
    avg_latency_ms: Optional[float] = None
    total_cost_usd: Optional[float] = None
    tests_passed: Optional[int] = None
    tests_failed: Optional[int] = None
    test_results: list[TestResultOut] = Field(default_factory=list)
    build_cost_usd: Optional[float] = None
    build_turns: Optional[int] = None


class PipelineProgressOut(BaseModel):
    current_agent: Optional[str] = None
    agents_completed: list[str] = Field(default_factory=list)
    harnesses_building: list[str] = Field(default_factory=list)
    harnesses_completed: list[str] = Field(default_factory=list)
    harnesses_failed: list[str] = Field(default_factory=list)
    test_progress: float = 0.0


class Quota(BaseModel):
    """Phase 2: Service-tier snapshot returned with every RunStateOut.

    The frontend renders this as a badge in the playground header so the
    user always sees their current plan + remaining credits. ``billing_enforced``
    tells the UI whether to display credit-cost warnings as advisory
    (enforced=False, the default) or as hard blockers (enforced=True).
    """

    plan: Plan = "free"
    credits_remaining: Optional[int] = Field(
        default=None,
        description="None = unlimited (free or enterprise). Integer = paid balance.",
    )
    credits_consumed: int = 0
    tier_features: dict[str, bool] = Field(
        default_factory=dict,
        description="Feature -> bool from billing.PLAN_FEATURE_MATRIX. UI uses this to disable buttons not in plan.",
    )
    billing_enforced: bool = Field(
        default=False,
        description="Whether the current PUZZLEEVAL_BILLING_ENFORCED flag is on. UI surfaces credit warnings when True.",
    )


# ============================================================================
# Phase 6: Candidate Selection (per-scope picks + user-added providers)
# ============================================================================

class UserAddedCandidateIn(BaseModel):
    """User-supplied candidate from the SelectionPanel's 'Add provider' form."""
    name: str
    provider: str
    api_docs_url: Optional[str] = None
    notes: Optional[str] = None
    covers_step_ids: list[str] = Field(
        description="Blueprint step IDs this provider covers. Required, at least one."
    )
    source: str = "user_provided"


class SelectCandidatesRequest(BaseModel):
    """POST body for /runs/{id}/select-candidates."""
    scope_picks: dict[str, list[str]] = Field(
        description=(
            "Per-scope picks: scope_id -> list of candidate names the user "
            "wants tested at that scope. A candidate can appear under multiple "
            "scope_ids if it covers multiple scopes."
        )
    )
    add: list[UserAddedCandidateIn] = Field(
        default_factory=list,
        description="Candidates the user added via the SelectionPanel form.",
    )


class SelectCandidatesResponse(BaseModel):
    """Response from /runs/{id}/select-candidates."""
    accepted_count: int = Field(
        description="Total unique (candidate, scope) pairs accepted."
    )
    scope_coverage: dict[str, int] = Field(
        description="scope_id -> how many candidates were picked at that scope."
    )


class RunStateOut(BaseModel):
    run_id: str
    trace_id: str
    status: str
    stage: str
    candidates: list[CandidateOut] = Field(default_factory=list)
    cost_usd: float = 0.0
    pipeline_progress: PipelineProgressOut = Field(default_factory=PipelineProgressOut)
    quota: Quota = Field(default_factory=Quota)
