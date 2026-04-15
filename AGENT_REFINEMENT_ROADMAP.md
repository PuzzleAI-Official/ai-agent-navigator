# PuzzleEval Agent Refinement — Phased Implementation Roadmap

## Context

PuzzleEval is an AI agent evaluation platform with a 5-agent pipeline (User Understanding → Research → Test Cases → Screening → Build & Test) wrapped by a FastAPI backend (`puzzleeval-api/`) with SSE streaming and a Vite/React frontend (`src/`). Today the pipeline runs end-to-end automatically once Agent 1 says `is_clear=true` — there is no user pause, no workflow-aware research, no API-tier handling, and no benchmark across domains beyond invoice OCR.

This roadmap addresses 10 refinement todos surfaced from the user's Trello board. Order is chosen so each phase consumes upstream changes and unblocks the next. Every phase ships behind a feature flag so the previous behavior remains one env-var away. The 231 existing unit tests must keep passing throughout.

The user's strategic clarifications:

- **"Agent 1 user picks"** is actually about Agent 2 — after research returns candidates, the user must be able to add, remove, and edit providers; user-added providers become top-priority test targets (replacing the current `inject_registry_candidates()` shim).
- **Service tier division** = user-facing PuzzleAI plans (Free / Paid / Enterprise). Free = unlimited search; Paid = credit-based testing; Enterprise = monitoring + observability. The current round must architect this so the future product flip is a flag-flip, not a refactor — implement it now if feasible.
- **Multi-objective tasking** = decompose the user's demand into a workflow blueprint (ordered scope-of-work steps), redesign Agent 2 to handle "all-in-one vs best-per-step" research, and have Agent 5 build harnesses that execute the workflow end-to-end.

## Cross-Cutting Architectural Decisions

Five decisions shape every phase below.

### A. Workflow blueprint lives inside Agent 1's output

Add `WorkflowBlueprint` (steps, ordering, all-in-one vs best-per-step option) as a new optional field on `UserUnderstandingOutput` (`PuzzleEval-local/puzzleeval/schemas.py:159`). Agent 1's existing `client.messages.parse()` call emits it alongside `sub_tasks[]`. Do NOT add a separate Agent 1.5 — it would double the only synchronous-conversational latency and split a single semantic concern (decomposition) across two agents.

### B. SSE pause-for-user gate

Pipeline pauses with `RunState.status = "awaiting_candidate_selection"` and `state.selection_ready: asyncio.Event`. Backend emits new SSE event `selection_required`; frontend renders a selection panel; user POSTs to new endpoint `POST /pzapi/runs/{run_id}/select-candidates`; endpoint sets the event, pipeline continues. Agent 4 (screening) runs AFTER user picks so we only screen what the user actually wants tested. CLI honors the same pause via stdin prompt (skip with `--no-interactive`).

### C. Service tier scaffold (non-enforcing by default)

Thin `puzzleeval-api/services/billing.py` module with `BILLING_ENFORCED` env flag (default `False`). Adds `plan: str = "free"` and `credits_remaining: int | None` to `RunState`. Decorator `require_credits(state, n)` is a no-op until the flag flips. `RunStateOut` gains a `Quota` block; frontend renders a `<QuotaBadge>`. Provider-tier (free/paid/enterprise on the upstream API) and service-tier (PuzzleAI plan) are kept distinct.

### D. Workflow harness = the unit of test

`WorkflowHarness.run(input)` chains per-step harnesses (`step1.run() → step2.run()`). Each step is built with the existing `_build_single_harness()` machinery — no new agent. New schemas: `WorkflowCombination` (one column of the test matrix, e.g. `{step_1: Mindee, step_2: Zapier}`), `WorkflowHarness`, `WorkflowTestRun`. Single-step blueprints behave exactly as today (1-element chain, no code branching).

### E. Generalizability benchmark = opt-in pytest + bench script

New `tests/generalizability/` directory marked `@pytest.mark.generalizability` (skipped by default). Six initial domain configs: OCR, chatbot, classification, translation, summarization, data extraction. Cassette-replay (vcrpy) for cheap regression CI; `--live` flag for refreshing cassettes. New `bench/run_benchmark.py` for human-readable reports.

---

## Phase 1 — Cloudflare hardening (todo #9)

**Goal:** Cut `web_fetch` failure rate from ~5–10 % to <1 % with detection + fallback for 403 / 429 / Cloudflare blocks. Anthropic's server-side `web_fetch_20250910` does not let us customize user-agent, so the fix is detect-and-pivot, not UA rotation.

**Why first:** every later phase fetches more docs (Agent 2 dual search, Agent 4 re-screening user adds, Agent 5 deep-dive). Reliability is the multiplier.

**Files:**
- New: `PuzzleEval-local/puzzleeval/web_fetch_fallback.py` — `should_retry_on_block()` helper used by Agents 2 / 4 / 5.
- Modify: `PuzzleEval-local/puzzleeval/agents/screening.py` (web_fetch loop), `implement_test_env.py` (the `_dispatch_tool` path that handles `web_fetch_tool_result` error blocks ~line 2063), `research.py` (no fetch but logs blocks).
- Modify: `PuzzleEval-local/puzzleeval/pipeline.py` — surface `web_fetch_blocks` count in `pipeline_summary.json`.

**Logic:** detect `web_fetch_tool_result` blocks with `is_error: True` and error text containing `403` / `429` / `cloudflare` / `blocked`. Inject a tool_result that tells the model to try `web_search "site:{domain} {query}"` or fall back to a saved-doc / GitHub-SDK search. 5-second backoff on 429.

**Schema changes:** none (logging only).

**Tests:** `tests/test_web_fetch_fallback.py` (~8 cases) — mock Anthropic responses with `is_error: True`, assert retry path executes search fallback.

**Verification:** `python -m puzzleeval.cli --text "I need invoice OCR" --agent4 --pretty` against a Cloudflare-gated vendor; expect no `FailedHarness` with `docs_unusable` citing 403/cf. Watch `web_fetch_blocks` counter in `pipeline_summary.json`.

**Rollback:** `ENABLE_FETCH_FALLBACK=False` env flag (default True).

**Phase fingerprint:** `pipeline_summary.json:metadata.web_fetch_blocks` (>0 = phase fired); per-call breakdown in stderr logs as `web_fetch_blocks_by_code`. Per-agent: `Agent4Result.web_fetch_blocks`, `Agent5Result.web_fetch_blocks`, `TestHarness.web_fetch_blocks`, `FailedHarness.web_fetch_blocks` (all default 0; non-zero = phase activated for that record).

**Diagnostic flag:** `PUZZLEEVAL_ENABLE_FETCH_FALLBACK=0` disables both detection and fallback message injection. `PUZZLEEVAL_FETCH_RATE_LIMIT_BACKOFF=N` controls 429 backoff seconds (default 5; set 0 to disable).

**Complexity:** S.

---

## Phase 1.5 — Content-quality assessment (extends Phase 1)

**Goal:** Generalize the fetch-recovery loop from "HTTP-error pivot" to "useless-content pivot." A page that returns HTTP 200 but is a JavaScript SPA shell, login wall, soft 404, or marketing-only landing page is just as useless to Agent 5 as a 403 — and the recovery action is the same. Build a framework-agnostic classifier that decides "did this fetch produce content the agent can use?" and route negative answers through the existing `build_fallback_message()` machinery.

**Why this exists as a separate phase from Phase 1:** Phase 1 hardened the HTTP layer. This hardens the content layer. Same module, same fallback path, broader trigger surface. Listed separately so the bench in Phase 10 can measure each independently.

**Why before Phase 2:** small extension of code we just shipped, low rollback risk, immediately compounds with Phase 8's doc-understanding work. Doesn't block Phase 2; can land in either order, but cheaper to do while Phase 1 context is fresh.

**Design principle (carries through every implementation choice):** identify usable pages by **positive** signals — endpoint patterns, auth examples, code blocks, prose with API keywords. ANY positive signal → page is usable, regardless of how it was rendered. Only when zero positive signals fire do we run a secondary classification ("why is this empty?") to give the recovery message a specific reason. This avoids hardcoding framework markers (Next.js, React, Vue, Angular) as the primary detector — they evolve too fast and miss adjacent failure modes (login walls, marketing pages) entirely.

**Files to modify:**
- `PuzzleEval-local/puzzleeval/web_fetch_fallback.py` — add `assess_content_quality()`, `extract_unusable_pages()`, helper constants (`_ENDPOINT_PATTERN`, `_AUTH_HEADER_MARKERS`, `_CODE_CALL_MARKERS`, `_API_HEADER_KEYWORDS`, `_SPA_MARKERS`, `_AUTH_WALL_MARKERS`, `_NOT_FOUND_MARKERS`). Extend `build_fallback_message()` to accept content verdicts. Extend `summarize_blocks_for_log()` to include unusable counts.
- `PuzzleEval-local/puzzleeval/agents/screening.py` — call `extract_unusable_pages` per turn, fold into `candidate_block_count` (Agent 4 single-shot, observability only).
- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — call `extract_unusable_pages` immediately after `extract_blocked_fetches`, fold the count into `candidate_web_fetch_blocks`, append the unified guidance to `tool_results`.
- `PuzzleEval-local/puzzleeval/schemas.py` — update `web_fetch_blocks` docstring on `Agent4Result`, `Agent5Result`, `TestHarness`, `FailedHarness` to reflect the expanded semantics ("HTTP error OR content-level failure").
- No frontend changes.

**Schema changes:** none. The existing `web_fetch_blocks` field absorbs both error types — operators get one number to watch. The breakdown lives in the per-call log entries.

**Detector behavior (deterministic, no LLM call):**

```
assess_content_quality(text) -> ContentVerdict

Stage 1 — positive signals (any one fires → usable):
  - Endpoint signature regex: (GET|POST|PUT|DELETE|PATCH|HEAD)\s+/[\w/{}\-:.]+
  - Auth marker substring: "Authorization:" / "X-API-Key:" / "Bearer " / "OAuth"
  - Code call marker: "curl ", "requests.get/post", "fetch(", "axios.", etc.
  - Prose floor: ≥500 chars after stripping <script>/<style>/tags AND
    contains at least one of: "endpoint", "request", "response",
    "parameter", "authentication", "rate limit", "api reference"

Stage 2 — classify why empty (secondary, only if stage 1 fails):
  - script_rendered: contains __NEXT_DATA__ / __NUXT__ / data-reactroot /
                     ng-version / data-sveltekit / NEXT_REDIRECT
  - auth_wall:       contains "sign in to view" / "log in to continue" /
                     <input type="password">
  - not_found_soft:  prose < 500 chars + contains "page not found" /
                     "404 not found" / "couldn't find"
  - unknown_useless: none of the above
```

The recovery message added to the next turn names the category so the model knows what to try (e.g., for `script_rendered` → "the page is client-rendered; search for deeper URLs that may be statically generated"; for `auth_wall` → "the docs require login; try GitHub SDK or web_search snippets which may have indexed before the login wall went up").

**Tests:** `tests/test_web_fetch_fallback.py` adds ~10 cases:
- `test_assess_usable_via_endpoint_signature`
- `test_assess_usable_via_auth_marker`
- `test_assess_usable_via_code_call`
- `test_assess_usable_via_prose_with_api_keywords`
- `test_assess_classifies_spa_shell`
- `test_assess_classifies_auth_wall`
- `test_assess_classifies_soft_404`
- `test_assess_classifies_unknown_useless`
- `test_extract_unusable_pages_skips_error_blocks`
- `test_extract_unusable_pages_finds_url_via_server_tool_use`
- `test_build_fallback_message_includes_content_verdicts`
- `test_stripe_style_rich_doc_passes_as_usable` (negative regression — must NOT misclassify rich API docs)

**Verification:** unit tests cover the classifier exhaustively. End-to-end verification arrives via Phase 10 bench: include 3-4 SPA-rendered providers (DocuClipper, Vercel-hosted vendors), measure the rate of "fetched HTML returned 200 but agent couldn't extract spec" — this should drop ~30-50% versus baseline.

**Rollback:** detection lives behind the same `ENABLE_FETCH_FALLBACK` flag as Phase 1. Setting it `False` reverts both the HTTP-error and content-quality detectors in one switch.

**Phase fingerprint:** Same `pipeline_summary.json:metadata.web_fetch_blocks` counter as Phase 1 (semantics expanded to include content-level failures). Per-call telemetry in stderr logs adds `web_fetch_unusable_total` and `web_fetch_unusable_by_category` keys (categories: `script_rendered` / `auth_wall` / `not_found_soft` / `unknown_useless`) — the presence of these keys (vs absent in Phase 1-only logs) signals Phase 1.5 activated.

**Diagnostic flag:** Shares `PUZZLEEVAL_ENABLE_FETCH_FALLBACK` with Phase 1 — single flip disables both classifiers.

**Complexity:** S (small extension of an existing module; ~150 lines of code + tests).

---

## Phase 2 — Service tier scaffold (todo #10)

**Goal:** Put plan/credit gating machinery in place so turning on billing in production is a flag-flip, not a refactor. Default mode = no-op (only tracks usage for observability).

**Why second:** every new endpoint added in later phases (`select-candidates`, future monitoring) should be tier-aware from day one.

**Files:**
- New: `puzzleeval-api/services/billing.py` — `BILLING_ENFORCED` flag, `credit_cost_for_agent(agent)`, `require_credits(state, n)`, `plan_allows(plan, feature)`.
- Modify: `puzzleeval-api/services/run_manager.py:13` — add `plan: str = "free"` and `credits_remaining: int | None` to `RunState`.
- Modify: `puzzleeval-api/routes/runs.py:20` — accept optional `plan` in `CreateRunRequest`.
- Modify: `puzzleeval-api/models/api_models.py` — add `plan` to `CreateRunRequest`, add `Quota` model surfaced inside `RunStateOut`.
- Modify: `puzzleeval-api/services/pipeline_runner.py:312` — wrap Agent 4 / Agent 5 entries with `billing.require_credits(state, n)`.
- New: `src/components/playground/QuotaBadge.tsx` — renders plan + remaining credits in `Playground.tsx` header.
- Modify: `src/types/pipeline.ts`, `src/services/api.ts` — add `Plan` and `Quota` types, accept `plan` in `createRun`.
- Stub: `puzzleeval-api/routes/monitoring.py` — returns 402 unless `plan == "enterprise"` (placeholder for future).

**Schema sketch:**

```python
# puzzleeval-api/models/api_models.py
class Quota(BaseModel):
    plan: str = "free"
    credits_remaining: int | None = None  # None = unlimited
    credits_consumed: int = 0
    tier_features: dict[str, bool] = Field(default_factory=dict)

class RunStateOut(BaseModel):
    ...
    quota: Quota = Field(default_factory=Quota)
```

**Plan matrix (initial):**
- `free` → `{search: True, testing: False, monitoring: False}`
- `paid` → `{search: True, testing: True, monitoring: False}`
- `enterprise` → all True

**Tests:** `puzzleeval-api/tests/test_billing.py` (~8 cases): no-op default; raises 402 when enforced + insufficient; matrix coverage; CreateRun accepts plan; RunStateOut surfaces quota.

**Verification:** `curl -X POST /pzapi/runs -d '{"text":"...","plan":"free"}'` runs Agents 1–3 free. With `PUZZLEEVAL_BILLING_ENFORCED=1`, Agent 4 returns HTTP 402. With flag off, behavior identical to today. Frontend QuotaBadge renders `FREE search only` in the playground header (verified live in preview, screenshot stored).

**Rollback:** flag-flip; CLI never touches billing.

**Phase fingerprint:** `RunState.credits_consumed`, `RunState.plan_gates_triggered`; per-call stderr logs as `billing_gate_passed` (success) and `billing_gate_triggered` (block) with `{plan, agent, credits_after, reason, enforced, trace_id}`; frontend reads via `Quota.credits_consumed` in `RunStateOut`. (Run-level rollup into `pipeline_summary.json:metadata.*` lands when `pipeline_runner.py` is migrated to call `pipeline_run.save_agent_result()` — pre-existing wiring gap, will be closed in this phase or Phase 6 latest.)

**Diagnostic flag:** `PUZZLEEVAL_BILLING_ENFORCED=0` (default) → no-op tracking only. `=1` → 402 on insufficient credits or feature-not-in-plan; pipeline emits `agent_blocked` + `pipeline_failed` SSE events; run status becomes `failed`.

**Complexity:** M.

---

## Phase 3 — WorkflowBlueprint in Agent 1 (todo #5)

**Goal:** Agent 1 emits an ordered `WorkflowBlueprint` describing the user's full scope of work (sub-task → role → ordering → input/output formats → "all_in_one vs best_per_step" architecture options). Downstream agents branch on this.

**Why third:** every subsequent phase consumes the blueprint. Defining it first prevents thrash.

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add `WorkflowStep`, `WorkflowBlueprint`; add `workflow: WorkflowBlueprint | None` to `UserUnderstandingOutput` (line 159).
- Modify: `PuzzleEval-local/puzzleeval/agents/user_understanding.py` — extend `SYSTEM_PROMPT` with workflow-decomposition instructions; `sub_tasks[]` IDs and `WorkflowStep.id`s must align so cross-references work.
- Modify: `PuzzleEval-local/puzzleeval/validators.py:135` (`validate_agent1_output`) — check ID uniqueness + `depends_on` references.
- Modify: `puzzleeval-api/models/api_models.py` — surface in `RunStateOut`.
- Modify: `puzzleeval-api/services/pipeline_runner.py` — emit new SSE event `workflow_blueprint` after Agent 1 completes.
- Modify: `src/types/pipeline.ts` — `WorkflowStep`, `WorkflowBlueprint`.
- New: `src/components/playground/WorkflowDiagram.tsx` — horizontal step-chain visual rendered above candidate list.
- Modify: `src/hooks/usePipelineRun.ts` — handle `workflow_blueprint` event, store in state.

**Schema:**

```python
class WorkflowStep(BaseModel):
    id: str                       # "step_1"
    role: str                     # "ocr", "extract", "spreadsheet"
    description: str
    capability: str               # matches SubTask.capability
    input_from: str | None = None # id of prior step or "user"
    output_format: str            # "structured_json", "free_text", ...
    depends_on: list[str] = Field(default_factory=list)
    all_in_one_compatible: bool = True

class WorkflowBlueprint(BaseModel):
    steps: list[WorkflowStep]
    architecture_options: list[str]   # ["all_in_one", "best_per_step"]
    notes: str
```

`workflow: WorkflowBlueprint | None = None` on `UserUnderstandingOutput` (default None for backward-compat with saved snapshots).

**Tests:** `tests/test_agent1.py` adds 4 cases (1-step request → 1-step blueprint; multi-step → correct `depends_on`; sub-task ↔ step ID alignment; both architecture options always present). `tests/test_validators.py` adds ID-uniqueness and orphan-depends_on cases.

**Verification:** `python -m puzzleeval.cli --text "OCR invoices then sync them to QuickBooks" --pretty` produces JSON with 2 steps, second step's `depends_on=["step_1"]`. UI shows the new diagram.

**Rollback:** field is optional; revert system-prompt only if quality regresses (schema stays).

**Phase fingerprint:** `agent_1_output.json.result.workflow.steps[]` length > 0. Live: `workflow_blueprint` SSE event fires once after Agent 1 with payload = full blueprint (or `null`). Validator catches structural bugs (duplicate ids, orphan `depends_on`, orphan `input_from`) as errors — warning for capability drift.

**Diagnostic flag:** soft revert only — `PUZZLEEVAL_AGENT1_MODEL=claude-sonnet-4-6` reverts Agent 1 to the pre-Phase-3 model if Opus blueprints regress. Schema itself cannot be disabled (downstream consumes it); `workflow=None` + no-ops downstream is the legacy fallback.

**Model switch:** Agent 1 promoted to Opus 4.6 (`AGENT1_MODEL` in `config.py`). Opus's planning reasoning is what lets Agent 1 reliably emit consistent multi-step blueprints. Cost: ~$0.05 → ~$0.10 per Agent 1 evaluation; ~1% of total pipeline cost.

**Tests:** 16 new unit tests (6 schema + 10 validator). Baseline now 246 green (227 PuzzleEval + 19 billing). `tsc` clean, Vite production build clean. Frontend verified live in preview: mock pipeline drove through conversation turns, `workflow_blueprint` SSE event rendered the 2-step `WorkflowDiagram` above the candidate list.

**Complexity:** M.

---

## Phase 4 — Agent 2 dual search + role-aware results (todo #7)

**Goal:** Agent 2 searches BOTH for all-in-one tools AND for best-per-step tools (one search per blueprint step). Output is grouped by which workflow step each candidate covers.

**Why fourth:** consumes #3 blueprint; produces role-grouped output the selection UI in #6 will render and the smart selector in #7 will use.

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/agents/research.py` — rewrite the search loop (dual mode when blueprint has >1 step).
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add `workflow_role: str = "all_in_one"` and `covers_step_ids: list[str]` to `Candidate`; add `candidates_by_step: dict[str, list[str]]` and `all_in_one_candidates: list[str]` to `Agent2Result`.
- Modify: `PuzzleEval-local/puzzleeval/validators.py:191` — warn if any step has zero candidates.
- Modify: `puzzleeval-api/services/pipeline_runner.py:196-232` — `candidates_found` SSE payload includes grouping.
- Modify: `src/types/pipeline.ts` — extend `Candidate` and `PipelineProgress`.
- Modify: `src/components/playground/CandidateCard.tsx` — already per-candidate, now grouped into role sections.
- New: `src/components/playground/WorkflowRoleSection.tsx` — wraps cards per step.

**Logic:**
- 1-step blueprint → behave as today (single search pass).
- N-step blueprint → 1 all-in-one search + N per-step searches (cap at `max_uses=5`). Per-step searches use the step's `capability` + step-specific keywords.
- Per-step candidates score only on that step's fit; all-in-one candidates score on aggregate.
- Dedup: a service that covers multiple steps appears once with `workflow_role="all_in_one"` AND populated `covers_step_ids`.

**Tests:** `tests/test_agent2.py` adds 4 cases (1-step → no grouping; 2-step → 2 keys in `candidates_by_step`; all-in-one appears in every step's coverage; validator warns on empty step).

**Verification:** `python -m puzzleeval.cli --text "Photos of invoices, extract data, push to Google Sheets" --agent2 --pretty` shows `candidates_by_step: {step_1: [...OCR...], step_2: [...sheets...]}` plus an `all_in_one_candidates` list. UI renders three sections.

**Rollback:** `RESEARCH_DUAL_SEARCH_ENABLED=False` reverts to single-pass behavior.

**Complexity:** L.

---

## Phase 5 — Pricing details research + calculations (todo #4)

**Goal:** Each validated candidate carries a structured `PricingBreakdown` (free tier units, paid tiers, overage cost, billing granularity, sources, confidence). Used by the selection UI in #6, the smart selector in #7, and the eventual final report.

**Why fifth:** consumes role grouping from #4 (per-role pricing aggregation); needed by #6 (selection UI shows pricing) and #7 (Agent 5 selection considers cost fit).

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/schemas.py:369` — add `pricing_breakdown: PricingBreakdown | None` to `Candidate`.
- Modify: `PuzzleEval-local/puzzleeval/schemas.py:827` — same field on `ScreenedCandidate`.
- Modify: `PuzzleEval-local/puzzleeval/agents/screening.py` — extend the per-candidate prompt to extract pricing structure on the same fetch (raise `web_fetch.max_uses` from 3 to 4 for a dedicated pricing-page fetch).
- New: `PuzzleEval-local/puzzleeval/pricing.py` — `estimate_monthly_cost(breakdown, monthly_volume) -> float`.
- Modify: `puzzleeval-api/services/pipeline_runner.py` — emit `candidates_verified` with pricing.
- Modify: `src/types/pipeline.ts` — add `PricingBreakdown`, `PricingTier`.
- Modify: `src/components/playground/CandidateCard.tsx` — render pricing block; tooltip with full tier list.

**Schema:**

```python
class PricingTier(BaseModel):
    name: str                                  # "Free", "Starter", "Pro"
    monthly_cost_usd: float
    included_units: int | None = None          # e.g. 250 pages/mo
    unit_name: str | None = None               # "pages", "calls", "tokens"
    overage_cost_per_unit_usd: float | None = None
    notes: str | None = None

class PricingBreakdown(BaseModel):
    tiers: list[PricingTier]                   # cheapest first
    free_tier_monthly_units: int | None = None
    pay_as_you_go: bool = False
    billing_granularity: str                   # "monthly", "per_call", "annual_commit"
    sources: list[str]                         # confirming URLs
    confidence: str                            # "high" | "medium" | "low"
    notes: str | None = None
```

**Tests:** `tests/test_agent4.py` adds 5 pricing cases. New `tests/test_pricing.py` covers `estimate_monthly_cost` (free under limit, paid over limit, overage).

**Verification:** `python -m puzzleeval.cli --text "I need invoice OCR" --agent4 --pretty` — every validated candidate has `pricing_breakdown` populated. Stub call `estimate_monthly_cost(breakdown, 1000)` returns expected dollar figure.

**Rollback:** field is optional; old text-string `pricing_details` (today) preserved as fallback.

**Complexity:** M.

---

## Phase 6 — User candidate picking + pipeline pause (todo #1, also retires the shim)

**Goal:** After Agent 2 emits candidates, pipeline pauses. UI lets user keep / remove / add providers. User-added providers are forced top-priority (mirroring the current shim, but user-driven). Replaces `inject_registry_candidates()`.

**Why sixth:** consumes blueprint (#3), role grouping (#4), pricing (#5), tier scaffold (#2 — endpoint should be tier-aware from day one).

**Files:**
- Modify: `puzzleeval-api/services/pipeline_runner.py:199-232` — split `_branch_a_research_and_screening()`. After Agent 2, emit `selection_required`, `await state.selection_ready`. Then run Agent 4 with the user-adjusted candidate list.
- Modify: `puzzleeval-api/services/run_manager.py:13` — add `awaiting_candidate_selection` to status enum docstring; add `selection_ready: asyncio.Event`, `user_selected_candidates: list[dict] | None`.
- Modify: `puzzleeval-api/routes/runs.py` — new endpoint `POST /pzapi/runs/{run_id}/select-candidates`.
- Modify: `puzzleeval-api/models/api_models.py` — `SelectCandidatesRequest`, `SelectCandidatesResponse`, `UserAddedCandidate`.
- **Shim removal (irreversible):**
  - `PuzzleEval-local/puzzleeval/cli.py:45` — remove `, inject_registry_candidates` from import.
  - `PuzzleEval-local/puzzleeval/cli.py:356` — delete `a2_result = inject_registry_candidates(a2_result)`.
  - `puzzleeval-api/services/pipeline_runner.py:588` — delete the matching import.
  - `puzzleeval-api/services/pipeline_runner.py:598` — delete `result = inject_registry_candidates(result)`.
  - `PuzzleEval-local/puzzleeval/agents/research.py:650-737` — delete the entire function + comments.
- Add: `PuzzleEval-local/puzzleeval/agents/research.py` — new `inject_user_candidates(agent2_result, user_adds: list[UserAddedCandidate]) -> Agent2Result` with `source="user_provided"`, `relevance_score=0.99`.
- Modify: `src/hooks/usePipelineRun.ts` — new stage `"selection"`, new event handler.
- New: `src/components/playground/SelectionPanel.tsx` — role-grouped cards reused from Phase 4, each with remove-X; "Add custom provider" form (name, docs URL, role).
- Modify: `src/services/api.ts` — `selectCandidates(runId, picks)`.
- Modify: `src/types/pipeline.ts` — `Stage` adds `"selection"`; new `UserCandidate`, `SelectCandidatesRequest`.

**Schema:**

```python
# puzzleeval-api/models/api_models.py
class UserAddedCandidate(BaseModel):
    name: str
    provider: str
    api_docs_url: str | None = None
    notes: str | None = None
    covers_step_ids: list[str] = Field(default_factory=list)
    source: str = "user_provided"

class SelectCandidatesRequest(BaseModel):
    keep: list[str]
    remove: list[str]
    add: list[UserAddedCandidate]

class SelectCandidatesResponse(BaseModel):
    accepted: int
    proceeding_to_screening: list[str]
```

**Pipeline pause shape:**

```python
async def _branch_a_research():
    ...run Agent 2...
    emit("candidates_found", ...)
    emit("selection_required", {"candidates": [...], "workflow": blueprint})
    state.status = "awaiting_candidate_selection"
    await state.selection_ready.wait()  # blocks until POST endpoint fires event
    a2_result = inject_user_candidates(a2_result, state.user_selected_candidates)
    ...continue to Agent 4...
```

Cancellation: `asyncio.wait([selection_ready, cancel_requested], return_when=FIRST_COMPLETED)` so the run cleanly aborts mid-pause.

CLI: interactive default prints candidates and prompts Y/N per-candidate via stdin; `--no-interactive` keeps auto-run for scripts.

**Tests:** new `puzzleeval-api/tests/test_routes_select.py` (~8 cases): POST without run → 404; valid picks → 200 + resume; double-fire rejected; keep+remove+add together; user-added flows to Agent 4 as `source="user_provided"`; cancel during pause → clean shutdown; empty selection → 400; mismatched blueprint step in `add` → 400. `tests/test_research.py` adds 4 cases for `inject_user_candidates` (merge, order, dedup by name, source marking).

**Verification:** end-to-end `curl` flow that drives Agent 1 to completion → SSE receives `selection_required` → POST to `select-candidates` → pipeline continues. CLI interactive prompt path. All 231 existing tests still pass after shim removal (audit `test_pipeline.py` and `test_agent5.py` for any direct shim references and switch fixtures to `inject_user_candidates`).

**Rollback:** `PUZZLEEVAL_USER_SELECTION_ENABLED=False` skips the wait (auto-runs). Shim removal is irreversible — but `inject_user_candidates` provides the same mechanism under user control.

**Complexity:** L.

---

## Phase 7 — Agent 5 smarter candidate / API selection (todo #2)

**Goal:** Replace the simplistic `(_has_credentials, relevance_score)` sort in `_select_candidates` with a multi-factor weighted selection: user picks first, credentials, role coverage across blueprint steps, API doc quality signals, pricing fit.

**Why seventh:** consumes user picks (#6), workflow roles (#3), role grouping (#4), pricing (#5).

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/agents/implement_test_env.py:3166-3226` — rewrite `_select_candidates`.
- Modify: `PuzzleEval-local/puzzleeval/schemas.py:1070` — add optional `user_constraints` (budget cap, max candidates) to `Agent5Input`.
- Modify: `PuzzleEval-local/puzzleeval/config.py` — introduce `AGENT5_SELECTION_WEIGHTS` dict (env-tunable).

**Logic:**

```python
def _compute_build_priority(c: ScreenedCandidate, has_creds: bool, blueprint, user_budget) -> float:
    w = AGENT5_SELECTION_WEIGHTS
    score  = w["user_provided"]   * (1.0 if c.source == "user_provided" else 0.0)
    score += w["credentials"]     * (1.0 if has_creds else 0.0)
    score += w["relevance"]       * c.relevance_score
    score += w["docs_quality"]    * _docs_quality_score(c)
    score += w["pricing_fit"]     * _pricing_fit_score(c, user_budget)
    score += w["role_coverage"]   * _role_coverage_bonus(c, blueprint)
    return score
```

Workflow-aware: if blueprint has N steps, guarantee ≥1 candidate per step in the top-K set before filling slots with all-in-ones. Doc quality signals derive from `verified_api_docs_url` + `screening_notes` patterns (OpenAPI mention +0.3, multiple Python examples +0.2, versioned URL +0.1, no auth-gating mentioned +0.2).

Default weights: `user_provided=0.35, credentials=0.25, relevance=0.15, docs_quality=0.10, pricing_fit=0.10, role_coverage=0.05`. Hard cap remains `AGENT5_MAX_CANDIDATES=4`.

**Tests:** `tests/test_agent5.py` adds 6 cases (user_provided always wins; high docs vs credentials trade-off; 2-step blueprint guarantees 1 per step; pricing fit favors under-budget; deterministic alphabetical tiebreaker; backward-compat fallback to old relevance sort when fields missing).

**Verification:** `python -m puzzleeval.cli --text "..." --agent5 --no-interactive` with selection flag off — top-K respects new weights. Bench test (Phase 10) confirms multi-step workflows always include each role.

**Rollback:** zero out new weights (`AGENT5_SELECTION_WEIGHTS={"relevance": 1.0}`) to restore old behavior.

**Complexity:** M.

---

## Phase 8 — Agent 5 API doc understanding (todo #3)

**Goal:** Phase 1 of Agent 5 explicitly targets OpenAPI / Swagger specs, traverses multi-page docs, and pivots to GitHub SDK repos when docs are auth-gated.

**Why eighth:** independent surgical improvement; lifts #6 build success rate. Lands after #7 (we now trust the selected candidate is well-chosen — invest in teaching it the docs).

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — `BUILDER_SYSTEM_PROMPT` (lines 68–250) and `_build_initial_message()` (~line 1195).
- Modify: same file — `ask_research` sub-agent template hints to find OpenAPI specs.

**Prompt updates:**
1. First attempt: search `site:{domain} openapi.json OR swagger.json OR .well-known/openapi.yaml`.
2. Second: fetch `/openapi.json`, `/api/docs/openapi.json`, `/v1/openapi.json`, `/swagger.json` (common conventions).
3. If spec found, parse endpoints directly into `api_spec.txt` without reading narrative docs.
4. Multi-page docs: if `web_fetch` result contains "Next: X" or "See also: Y", model MUST fetch ≥2 more pages before completing Phase 1. Enforce: "ENDPOINTS section must list ALL endpoints, not just quickstart."
5. Auth-gated docs: try `docs/` or `developer/` variants; if blocked, `ask_research` to find SDK repos on GitHub.
6. Add required `api_spec.txt` section: `OPENAPI_URL: <url or "not found">`.

**Tests:** `tests/test_agent5.py` adds 4 cassette-based cases (spec found on first try; spec missing → search → GitHub SDK fallback; multi-page combine; auth-gated pivot).

**Verification:** live run inspects `runs/{trace_id}/harnesses/{slug}/api_spec.txt` for `OPENAPI_URL` line + more endpoints than before. Bench passes for translation / summarization domains that previously tripped on doc quality.

**Rollback:** prompt-only change; revert the system prompt section.

**Complexity:** S.

---

## Phase 9 — Agent 5 workflow-chaining harnesses (todo #6)

**Goal:** For multi-step blueprints, Agent 5 builds harnesses that EXECUTE the workflow end-to-end. A 2-step "OCR → spreadsheet" workflow chains Mindee's output into Zapier's input. Test results are end-to-end pipeline scores, not isolated sub-task scores.

**Why ninth:** biggest change; needs blueprint (#3), role grouping (#4), smart selection (#7).

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add `WorkflowCombination`, `WorkflowHarness`, `WorkflowTestRun`; extend `Agent5Result:1323` with `workflow_harnesses: list[WorkflowHarness]` and `workflow_runs: list[WorkflowTestRun]`.
- Modify: `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — new `_build_workflow_harness()` that composes per-step harnesses; `_execute_all_tests()` dispatches workflow OR per-candidate based on blueprint step count.
- Modify: `PuzzleEval-local/puzzleeval/agents/synthetic_tests_file.py` — group test cases by workflow output, not per-sub-task, when blueprint is multi-step.
- Modify: `puzzleeval-api/services/pipeline_runner.py` — new SSE event `workflow_test_result`.
- Modify: `src/types/pipeline.ts` — add `WorkflowTestResult`, `WorkflowCombination`.
- Modify: `src/components/playground/ResultsComparison.tsx` — add toggle "View by candidate" vs "View by workflow combination". Workflow mode renders matrix: combos × (quality, cost, latency, pass-rate).

**Schema:**

```python
class WorkflowCombination(BaseModel):
    id: str                              # "combo_1"
    step_assignments: dict[str, str]     # step_id → candidate_name
    is_all_in_one: bool                  # one candidate covers everything
    total_estimated_cost_per_run_usd: float

class WorkflowHarness(BaseModel):
    combination: WorkflowCombination
    harness_dir: str
    entry_file: str                      # "workflow_harness.py"
    per_step_harnesses: dict[str, str]   # step_id → sub-harness path
    smoke_test_passed: bool
    live_validation_passed: bool | None
    build_cost_usd: float

class WorkflowTestRun(BaseModel):
    combination: WorkflowCombination
    test_results: list[TestCaseResult]
    total_cost_usd: float
    avg_latency_ms: float
    pass_rate: float
```

**Outer-loop logic:**
1. 1-step blueprint → today's behavior (per-candidate harness, per-sub-task tests).
2. N-step blueprint → pick top K combinations (default 3): one all-in-one if available, K-1 best-per-step permutations. For each combo, build per-step harnesses in parallel (reuse `_build_single_harness`), then write a thin `workflow_harness.py`:

```python
def run(workflow_input: dict) -> dict:
    step1_result = step_1_harness.run(workflow_input)
    if not step1_result.get("success"):
        return {"success": False, "error": "step_1_failed", "raw": step1_result}
    step2_result = step_2_harness.run({"input": step1_result["output"], ...})
    return {"success": True, "output": step2_result["output"], "intermediate": [step1_result]}
```

3. LLM judge: `raw_response` is the chain's final output; criteria apply to the final output; intermediate outputs surfaced for debugging.
4. Cost accounting: sum per-step costs per workflow run.

**Tests:** `tests/test_agent5.py` adds 6 cases (1-step regression; 2-step chain; step-1 failure → step-2 skipped; all-in-one combo; cost aggregation; latency sums). `tests/test_pipeline.py` adds 2 end-to-end cassette cases.

**Verification:** `python -m puzzleeval.cli --text "OCR invoices then log to a sheet" --agent5 --no-interactive` shows `workflow_harnesses` ≥1 successful chain. UI matrix view renders per-combination quality/cost/latency.

**Rollback:** `WORKFLOW_HARNESS_ENABLED=False` reverts to per-candidate logic even with multi-step blueprints. Schema additions are optional.

**Complexity:** XL.

---

## Phase 10 — Generalizability benchmark (todo #8)

**Goal:** A 6-domain benchmark suite (OCR, chatbot, classification, translation, summarization, data extraction) that runs end-to-end and asserts quality + cost budgets. Regression gate for everything above.

**Why last:** validates that the previous 9 phases didn't narrow PuzzleEval to the OCR happy path.

**Files:**
- New: `PuzzleEval-local/tests/generalizability/` with `__init__.py`, `conftest.py` (markers + fixtures), `test_generalizability.py` (parametrized over domains), `domains/{ocr,chatbot,classification,translation,summarization,data_extraction}.json`.
- New: `PuzzleEval-local/bench/run_benchmark.py` — CLI runner (`--domain ocr --live` records new cassette; `--domain ocr` replays).
- New: `PuzzleEval-local/bench/cassettes/{domain}/`, `bench/results/`, `bench/README.md`.
- Modify: `PuzzleEval-local/pyproject.toml` — add `vcrpy>=6.0` to dev deps.
- New: `pytest.ini` config block — `markers = generalizability: long-running cross-domain benchmarks`; default invocation excludes (`-m "not generalizability"`).
- Optional: `.github/workflows/benchmark.yml` — weekly cassette-replay run.

**Domain config sketch:**

```json
{
  "name": "chatbot",
  "input": "I need a customer support chatbot for my Shopify store.",
  "min_pass_rate": 0.6,
  "max_cost_usd": 6.0,
  "required_roles": ["chatbot"],
  "expected_candidate_count": 3
}
```

**Test shape:**

```python
@pytest.mark.generalizability
@pytest.mark.parametrize("domain", ["ocr", "chatbot", "classification", "translation", "summarization", "data_extraction"])
def test_domain_end_to_end(domain):
    config = json.load(open(f"domains/{domain}.json"))
    with vcr_cassette(f"cassettes/{domain}.yaml"):
        result = run_full_pipeline(config["input"])
    assert result.aggregate_pass_rate >= config["min_pass_rate"]
    assert result.total_cost_usd <= config["max_cost_usd"]
    assert all(role_covered(result, role) for role in config["required_roles"])
```

**Tests:** the 6 domain tests themselves + 4 meta-tests (config loads, cassette replay deterministic, missing cassette → skip with warning, report aggregates correctly).

**Verification:** `pytest -m generalizability` — all 6 pass with cassettes. Default `pytest` keeps the 231-test green path. `python bench/run_benchmark.py --domain ocr` prints a human report (pass rate, cost, per-role coverage, time).

**Rollback:** marker-gated, separate dir — does not touch the core suite.

**Complexity:** L.

---

## Recommended Execution Order

| # | Phase | Original todo | Why here |
|---|-------|---------------|----------|
| 1 | Cloudflare hardening | #9 | Reliability multiplier for every later phase that fetches more docs. Small, self-contained, low-risk. |
| 1.5 | Content-quality assessment | (extends #9) | Generalizes #1's recovery loop from HTTP errors to any useless-content fetch (SPA shells, auth walls, soft 404s, marketing pages). Same module, broader trigger surface. Lands while Phase 1 context is fresh. |
| 2 | Service tier scaffold | #10 | Has to exist before any new endpoint (select-candidates in #6, future monitoring). No-op default → cannot break anything. |
| 3 | WorkflowBlueprint | #5 | Every downstream phase consumes it. Define schema first, prevent thrash. |
| 4 | Agent 2 dual search | #7 | Consumes #3, produces role-grouped output for the selection UI in #6. |
| 5 | Pricing research | #4 | Consumes #4 grouping, feeds the selection UI in #6 and the smart selector in #7. |
| 6 | User picking + pause | #1 | Consumes #3 / #4 / #5; replaces the shim. Highest-visibility UX shift. |
| 7 | Smarter Agent 5 selection | #2 | Consumes user picks + workflow + pricing. |
| 8 | Agent 5 API doc understanding | #3 | Surgical internal improvement; strengthens #9. Inherits the content-quality assessor from #1.5. |
| 9 | Workflow-chaining harnesses | #6 | Biggest change; needs blueprint + selection + smart picking + better doc reading. |
| 10 | Generalizability benchmark | #8 | Regression gate validating the whole stack works beyond OCR. Bench mix should include 3-4 SPA-rendered providers to exercise the #1.5 path. |

---

## Global Constraints

- Each phase ships behind a feature flag: `ENABLE_FETCH_FALLBACK`, `PUZZLEEVAL_BILLING_ENFORCED`, `RESEARCH_DUAL_SEARCH_ENABLED`, `PUZZLEEVAL_USER_SELECTION_ENABLED`, `WORKFLOW_HARNESS_ENABLED`. Defaults chosen so behavior matches the new phase; flipping False restores prior behavior.
- All schema additions are optional (`= None` or `Field(default_factory=...)`) so saved snapshots in `runs/` keep loading.
- CLI (`python -m puzzleeval.cli ... --agent5`) stays functional through every phase. `--no-interactive` skips the Phase 6 user pause.
- Shim removal in Phase 6 is irreversible — but `inject_user_candidates()` preserves the same priority-promotion mechanism under user control.
- 231 existing unit tests must remain green at the end of every phase. New tests are additive.

## Critical Files (most touched across the roadmap)

- `PuzzleEval-local/puzzleeval/schemas.py` — every phase except #1 and #10.
- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — phases #1, #7, #8, #9.
- `PuzzleEval-local/puzzleeval/agents/research.py` — phases #4, #6.
- `puzzleeval-api/services/pipeline_runner.py` — phases #2, #3, #4, #6, #9.
- `puzzleeval-api/services/run_manager.py` — phases #2, #6.
- `src/hooks/usePipelineRun.ts` — phases #3, #4, #6, #9.
- `src/components/playground/` (Playground.tsx + new components) — phases #2, #3, #4, #5, #6, #9.

## End-to-End Verification (after all 10 phases)

1. `pytest` — 231 baseline tests + ~50 new tests added across phases all pass.
2. `pytest -m generalizability` — all 6 domain benchmarks pass via cassette replay.
3. CLI: `python -m puzzleeval.cli --text "Take photos of invoices, extract data, push to QuickBooks" --agent5 --no-interactive --pretty` — output JSON shows: workflow blueprint with 2-3 steps; candidates grouped by step; pricing breakdowns; workflow-chained harnesses; per-combo end-to-end test scores.
4. UI: `bun run dev` + start `puzzleeval-api`. End-to-end browser flow: type a multi-step request → workflow diagram appears → candidates grouped by role → SelectionPanel allows add/remove/edit → pipeline resumes → ResultsComparison shows per-workflow-combination matrix. QuotaBadge in header reflects plan + credits.
5. Live run with `PUZZLEEVAL_BILLING_ENFORCED=1` and `plan="free"` correctly returns 402 at Agent 4 entry; `plan="paid"` proceeds with credits decremented.
