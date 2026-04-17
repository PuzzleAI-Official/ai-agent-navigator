# PuzzleEval Agent Refinement — Phased Implementation Roadmap

> ## Status as of current session (read this first)
>
> **All 9 phases from the original plan (1, 1.5, 2, 3, 4, 5, 6, 6.5, 7, 8) have shipped.** The document below is preserved as the design record of each phase's rationale and schema changes; it is NOT a plan — it is history.
>
> After the phased plan shipped, **two additional capability passes** landed in the same repo:
>
> 1. **Cross-modality tool plugin + hybrid evaluator pass** — `puzzleeval/tool_plugins/` ecosystem (code_execution, vision, transcription, tts, conversation_simulator, webhook_receiver, outbound_delivery, voice_realtime), modality dispatcher, test-data sufficiency analyzer, programmatic tool calling for Agent 5, adaptive-thinking effort tiers, hybrid evaluator for ambiguous modalities. Tracked in `PuzzleEval-local/POST_ROADMAP_ENHANCEMENTS.md`.
>
> 2. **Production-resilience audit pass** (current session) — central Anthropic client factory with timeout + retries + model fallback ladder (`anthropic_client.py`), structured-output grammar fallback (`structured_output.py`), cost circuit-breaker (`budget.py` + `RunState.record_cost`), FastAPI lifespan handler for plugin teardown, upload + SMTP DoS caps, empty-env-var shadow fix, `coverage_gap` SSE event, final `EvaluationReport` assembler (`report.py`) + `GET /runs/{id}/report`, frontend SSE auto-reconnect, live `cost_update` meter, `EvaluationReportCard` UI. Detailed in the same `POST_ROADMAP_ENHANCEMENTS.md` under "Production audit + resilience pass".
>
> **Current test count:** 816 core + 30 API + 39 generalizability = **885 tests passing**. TypeScript clean. Vite build clean.
>
> **Current production code size (excluding tests):** ~40,900 LoC (26,600 Python core + 2,344 FastAPI + 12,018 frontend TypeScript).
>
> **Known Claude-Code-parity gaps still open** (none architectural; all are wiring):
> 1. Agent 5 mid-turn cancellation — `cancel_event` stops at agent boundaries, not inside the 25-turn builder loop
> 2. Agent 5 model fallback — `call_with_model_fallback()` helper exists but is only wired into Agent 1
> 3. Live `agent_thinking` streaming — extended-thinking blocks are produced but not surfaced to SSE
> 4. Incremental token streaming — Agent 5 uses blocking `messages.create()`, not `stream=True`
> 5. Agent 2 per-scope parallelism — one serial research call for N-scope blueprints
> 6. In-run web_fetch URL cache — same doc re-fetched per candidate
> 7. Idempotency keys + DRY_RUN propagation on candidate writes
> 8. Structured provider-quirk registry (Stripe-Version, OpenAI-Beta, anthropic-version)
> 9. AWS SigV4 / OAuth2 authorization_code / mTLS auth patterns in `api_patterns.py`
>
> These are documented in detail in `POST_ROADMAP_ENHANCEMENTS.md`. Closing them is ~1 focused day of work — none require architectural changes.

---

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

### D. The scope is the unit of test, not the chain (pivot)

Per-scope testing — for each scope in the workflow DAG, test the top-K candidates INDEPENDENTLY at that scope. No full-chain integration testing. Agent 1's DAG architecture is the integration surface the user sees; results are per-scope scores; user composes their final solution mentally. Rationale: combinatorial explosion (K^N chained combinations) is unbounded AND has integration-impedance failure modes (JSON-shape mismatches between tools). Per-scope data is more actionable and bounded linear in N.

New schemas: `ScopeTestRun` (per-scope result bundle); `Candidate.covers_step_ids: frozenset[str]` as a first-class field (authoritative: Agent 2 seeds as "claimed", Phase 6.5 verifies and upgrades to "verified" or removes). Build-time tool deduplication: one harness per unique tool regardless of how many scopes it covers. Single-scope blueprints behave exactly as today's single-candidate testing. DROPPED: `WorkflowCombination`, `WorkflowHarness`, `WorkflowTestRun` — these were the old chained-harness design before the pivot.

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

## Phase 3 — Workflow Architecture Design (Agent 1 as architect / DAG) (todo #5)

**Status:** FULLY SHIPPED (linear form + DAG expansion, 2026-04-15). All items below marked [shipped].

**Goal:** Agent 1 designs the user's workflow as a proper DAG **architecture** — steps with ordering, parallelism (fan-out / fan-in), and role assignment. The architecture is what the user sees first; downstream phases test top-K candidates INDEPENDENTLY at each scope in the architecture. No full-chain integration test is built — the per-scope results are the deliverable (see Phase 9 rewrite).

**Why the pivot to DAG + per-scope testing:** Full-chain integration testing is unbounded (K^N combinatorial) AND has real integration-impedance problems (JSON-shape mismatches between tools, error-propagation). Per-scope testing is bounded (linear in N) AND gives users more actionable information — "Mindee scores 0.95 at OCR" vs "this specific 5-tool chain scored 0.83 as a pipeline." Agent 1's DAG architecture design IS the product's integration surface; the user composes final workflows mentally from per-scope data.

**Why third:** every subsequent phase consumes the architecture. Defining it first prevents thrash.

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add `WorkflowStep`, `WorkflowBlueprint`; add `workflow: WorkflowBlueprint | None` to `UserUnderstandingOutput`. [shipped]
- Modify: `PuzzleEval-local/puzzleeval/agents/user_understanding.py` — extend `SYSTEM_PROMPT` with workflow-decomposition instructions; `sub_tasks[]` IDs and `WorkflowStep.id`s must align so cross-references work. [shipped] (DAG expansion 2026-04-15: parallelism + fan-out/fan-in + acyclic rules, two new worked examples)
- Modify: `PuzzleEval-local/puzzleeval/validators.py` (`validate_agent1_output`) — check ID uniqueness + `depends_on` references. [shipped] (DAG expansion 2026-04-15: DFS cycle detection + unreachable-step warnings + no-root detection)
- Modify: `puzzleeval-api/models/api_models.py` — surface in `RunStateOut`. [shipped]
- Modify: `puzzleeval-api/services/pipeline_runner.py` — emit new SSE event `workflow_blueprint` after Agent 1 completes. [shipped]
- Modify: `src/types/pipeline.ts` — `WorkflowStep`, `WorkflowBlueprint`. [shipped] (DAG expansion: `parallel_group?: string \| null` added to WorkflowStep)
- New: `src/components/playground/WorkflowDiagram.tsx` — step-chain visual rendered above candidate list. [shipped] (DAG expansion 2026-04-15: rewritten for topological-layer layout with SVG edges + parallel_group clusters + ResizeObserver-based edge re-layout)
- Modify: `src/hooks/usePipelineRun.ts` — handle `workflow_blueprint` event, store in state. [shipped]

**[DAG] Completed work (2026-04-15):**
- **Agent 1 prompt — DAG authoring.** System prompt rule block added: parallelism is default not opt-in, explicit fan-in merge steps, acyclic. Two worked DAG examples embedded (fan-out+fan-in enrichment, two-root parallel ingestion).
- **Schema — `parallel_group: str | None`** on WorkflowStep. Layout-only; `depends_on` remains authoritative. Mirrored in `src/types/pipeline.ts`.
- **Validator — acyclic + unreachable.** DFS-based cycle detection (reports cycle path in the error message); unreachable-step detection via forward walk from roots (warning-level); no-root multi-step blueprint detected as cycle error.
- **Frontend DAG rendering.** Layers via longest-path-from-roots; per-layer columns; steps in the same `parallel_group` render inside a dashed container tagged with the group name; SVG cubic-Bezier edges measured via `useLayoutEffect` + ResizeObserver. Header shows "N steps · DAG" when any layer has >1 node. ~200 LoC; no external graph library.

**[DAG] Deferred to Phase 6:**
- Coverage-highlight interaction (hovering a candidate in SelectionPanel tints covered DAG nodes) — lands with SelectionPanel in Phase 6.
- dagre / elkjs polish for graphs with >10 nodes — revisit only if bench shows Agent 1 producing unusually large blueprints.

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

**Model switch:** Agent 1 promoted to Opus 4.7 (`AGENT1_MODEL` in `config.py`). Opus's planning reasoning is what lets Agent 1 reliably emit consistent multi-step blueprints. Cost: ~$0.05 → ~$0.10 per Agent 1 evaluation; ~1% of total pipeline cost.

**Tests:** 25 new unit tests total (10 schema + 15 validator, incl. DAG expansion). Baseline now **255 green** (236 PuzzleEval + 19 billing; was 246 before DAG expansion). `tsc` clean, Vite production build clean. Frontend originally verified live in preview with a mock 2-step linear blueprint; DAG rendering path exercised via unit tests + production build (live DAG preview scheduled alongside Phase 6 SelectionPanel work that consumes it).

**Complexity:** M.

---

## Phase 4 — Agent 2 dual search + ranked candidate pool (todo #7)

**Status:** FULLY SHIPPED (2026-04-15). Schema + prompts + tool config + validator + SSE + frontend + tests + docs all in. Behavior lives behind `PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED` (default on).

**Goal:** Agent 2 searches BOTH for all-in-one tools AND for best-per-step tools (one search per blueprint scope). Output is a RANKED candidate pool with claimed `covers_step_ids`, scored per scope. **No verification happens here.** That's deferred to Phase 6.5 (deep verify, drop-on-reject) so we only pay deep-research cost on candidates that will actually be tested. The pool is what feeds the UI selection panel and the programmatic top-K selector.

Coverage is arbitrary: a provider may cover 1, 2, 3, or all N scopes; there is no special "multi-step" category — everything is just a coverage set. Specialists and all-in-ones compete equally at every scope they claim. All coverage info here is `coverage_confidence: "claimed"` — only Phase 6.5's deep verify can upgrade it to `"verified"`.

**Why fourth:** consumes #3 DAG; produces the ranked/coverage-claimed pool that #7 picks top-K from, #6 lets the user override, #6.5 deep-verifies (drops rejections), #9 tests.

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/agents/research.py` — rewrite the search loop (dual mode when blueprint has >1 step). **Agent 2 does NOT fetch docs, does NOT verify, does NOT call Agent 4.** Just searches, ranks, extracts candidate names + provider + claimed coverage + claimed docs_url from search snippets.
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add `covers_step_ids: frozenset[str]` and `coverage_confidence: dict[str, str]` to `Candidate`. All `coverage_confidence` values are `"claimed"` at this phase (Phase 6.5 upgrades confirmed ones to `"verified"`). Drop the old `workflow_role` / simple-grouping concept — arbitrary-coverage sets replace it.
- Modify: `PuzzleEval-local/puzzleeval/validators.py` — warn if any scope has fewer than 3 candidates covering it (low diversity at that scope); warn if the union of all candidates' coverage doesn't cover every blueprint scope. A thin pool here is valid — Phase 6.5 still tries; it just means the scope may end up with fewer verified candidates after drops.
- Modify: `puzzleeval-api/services/pipeline_runner.py` — `candidates_found` SSE payload includes `covers_step_ids` per candidate (frontend builds per-scope views from this). Pipeline continues past Phase 4 directly to Phase 7 (programmatic top-K) / Phase 6 (user picks); no intermediate verification pass.
- Modify: `src/types/pipeline.ts` — `Candidate.covers_step_ids: string[]` and `coverage_confidence: Record<string, "verified" | "claimed">`.
- Modify: `src/components/playground/CandidateCard.tsx` — render coverage badge ("covers 3/5 scopes: ocr, extract, sync"). Visual hint when `coverage_confidence[scope] === "claimed"` (small "unverified" dot) so user sees what's still to be validated.
- New: `src/components/playground/CoverageMatrix.tsx` (PART OF Phase 4, not deferred) — the candidates-×-scopes matrix view. Shown alongside per-scope tables once candidates arrive. Empty cells for scopes a candidate doesn't cover; dim cells for "claimed but not yet verified".

**Schema:**

```python
class Candidate(BaseModel):
    ...
    covers_step_ids: frozenset[str] = Field(
        default_factory=frozenset,
        description=(
            "Scope IDs this candidate covers. Initial guess from Agent 2's "
            "dual search — specialists from per-step search default to the "
            "single step they were found for; all-in-ones from the "
            "horizontal search claim their full stated coverage. Agent 4.5 "
            "verifies and overwrites this field as authoritative."
        ),
    )
    coverage_confidence: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-scope-id: 'claimed' (Agent 2's initial guess from search "
            "snippets) or 'verified' (Agent 4.5 confirmed from docs). "
            "UI annotates low-confidence scope assignments."
        ),
    )
```

**Logic:**
- 1-scope blueprint → behave as today (single search pass). All candidates have `covers_step_ids = {"step_1"}` and `coverage_confidence = {"step_1": "claimed"}`.
- N-scope blueprint → 1 all-in-one search + N per-scope searches (cap at `max_uses=N+1`). Per-scope candidates default to `covers_step_ids = {that_one_scope}`. All-in-one candidates set `covers_step_ids` from the search result's claimed coverage.
- Dedup by candidate name: if Mindee surfaces in both the all-in-one pass (claiming 3 scopes) AND the per-step-1 pass, merge under one record with `covers_step_ids = {"step_1", "step_2", "step_3"}` and note in coverage_confidence.

**Tests:** `tests/test_agent2.py` adds 5 cases (1-scope → single-element covers sets; 3-scope → candidates have varied coverage sets; all-in-one dedup; coverage_confidence defaults to "claimed"; validator warns on low-diversity scopes).

**Verification:** `python -m puzzleeval.cli --text "Photos of invoices, extract data, push to Google Sheets" --agent2 --pretty` → each candidate has a `covers_step_ids` set; specialists have 1-element sets, all-in-ones have full-length sets. Frontend renders per-scope CandidateCard sections PLUS a coverage matrix for the whole candidate pool.

**Rollback:** `RESEARCH_DUAL_SEARCH_ENABLED=False` reverts to single-pass behavior (all candidates get empty `covers_step_ids`, downstream falls back to legacy flat flow).

**Phase fingerprint:** `agent_2_output.json.candidates[].covers_step_ids` is populated (non-empty frozenset per candidate). `CoverageMatrix` component mounted on playground.

**Complexity:** L.

---

## Phase 5 — Pricing details (folds into Phase 6.5's deep verify) (todo #4)

**Status:** PHASE 5a SHIPPED (2026-04-15) — schemas + helpers + frontend scaffold + tests + docs. Phase 5b (prompt addition + SSE event emission) defers to Phase 6.5 since it lives inside 6.5's 4B extraction turn-phase.

**Goal:** Each deep-verified candidate carries a structured `PricingBreakdown` (free tier units, paid tiers, overage cost, per-scope-unit cost where it varies, billing granularity, sources, confidence). Used by the per-scope selection UI in #6 and the per-scope cost summary in final results.

**Why it moved:** previously folded into Phase 4.5 (verify-all). Now Phase 4.5 is gone; pricing folds into **Phase 6.5's Phase 4B extraction** (the "Spec Extraction" turn-phase inside the deep-verify loop). Agent 4 extracts pricing at the same time as endpoints — they're on the same provider domain, often on the same page. We never extract pricing for candidates that won't be tested.

**Why it's LIGHT now:** no new agent call. Phase 5 is a prompt addition to Phase 6.5's 4B extraction: pull pricing along with endpoints. `web_fetch.max_uses` already raised to 6 in Phase 6.5 to accommodate the pricing page if it's on a separate URL.

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add `pricing_breakdown: PricingBreakdown | None` to `Candidate` and `ScreenedCandidate`.
- Modify: `PuzzleEval-local/puzzleeval/agents/screening.py` — Phase 6.5's Phase 4B prompt gains a "also extract pricing" section. Extraction produces the `PricingBreakdown` dict alongside `ENDPOINTS[]`.
- New: `PuzzleEval-local/puzzleeval/pricing.py` — `estimate_monthly_cost(breakdown, monthly_volume) -> float`; also `per_scope_unit_cost(breakdown, scope_id) -> float | None` for the per-scope cost display that variable-coverage presentation requires.
- Modify: `puzzleeval-api/services/pipeline_runner.py` — `candidate_verified` SSE event (from Phase 6.5) includes pricing payload.
- Modify: `src/types/pipeline.ts` — add `PricingBreakdown`, `PricingTier`.
- Modify: `src/components/playground/CandidateCard.tsx` — render pricing block; tooltip with full tier list.
- Modify: `src/components/playground/CoverageMatrix.tsx` — cells show cost per scope when pricing varies by scope (e.g. OCR tools charging per page vs sync tools charging per event).

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
    per_scope_unit_cost: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-scope-id unit cost when the provider charges differently "
            "across scopes (e.g. $0.10/page for OCR scope, $0.005/event "
            "for sync scope). Empty dict = uniform pricing across all "
            "scopes this candidate covers."
        ),
    )
    sources: list[str]                         # confirming URLs
    confidence: str                            # "high" | "medium" | "low"
    notes: str | None = None
```

**Tests:** `tests/test_agent4.py` (already being extended in Phase 6.5) adds 5 more pricing cases covering extraction. New `tests/test_pricing.py` covers `estimate_monthly_cost` and `per_scope_unit_cost` (uniform falls back to tier cost; per-scope map takes precedence).

**Verification:** `python -m puzzleeval.cli --text "I need invoice OCR" --agent5 --pretty` — every deep-verified candidate in `scope_selections[*]` has `pricing_breakdown` populated. CoverageMatrix cells show per-scope costs where applicable.

**Rollback:** field is optional; when null, downstream falls back to Agent 2's free-form `pricing_model` string.

**Phase fingerprint:** `ScreenedCandidate.pricing_breakdown` non-null for deep-verified candidates; `pricing_breakdown.confidence` distribution (most should be "high" or "medium"; "low" count warns if Phase 6.5's 4B is struggling to find pricing pages).

**Complexity:** S (additive prompt inside Phase 6.5's 4B, not a new agent call).

---

## Phase 6 — User candidate picking + pipeline pause (todo #1, also retires the shim)

**Status:** FULLY SHIPPED (2026-04-15). Pipeline pause + SelectionPanel + select-candidates endpoint + inject_user_candidates + apply_scope_picks + shim retired + CLI interactive + RejectionSummary scaffold + tests + docs.

**Goal:** After Agent 2 emits ranked candidates with CLAIMED coverage sets, pipeline pauses. UI shows candidates **organized per scope** (not as combinations). User picks per-scope — "at scope 1, include these 4 candidates; at scope 2, include these 3 and add my custom one." User-added providers specify which `covers_step_ids` they serve. Replaces `inject_registry_candidates()`.

**Sequencing with Phase 6.5:** this phase pauses BEFORE deep-verify. At pick time, coverage is unverified (`coverage_confidence: "claimed"` on every scope). After the user resumes, Phase 6.5 deep-verifies their picks (drop-on-reject). The UI post-verify shows:
- ✅ verified candidates (green badge transitions claimed → verified)
- ⚠️ rejections (user's pick failed deep-verify; candidate dropped from tested set — no substitution; scope tests with fewer candidates)
- ❌ scope under-capacity (if many candidates rejected, scope may have only 1-2 tested — surfaced as prominent warning so user can add alternatives)

**Why sixth:** consumes DAG (#3), ranked pool with claimed coverage (#4), tier scaffold (#2 — endpoint should be tier-aware from day one). Feeds Phase 6.5 (deep verify, drop-on-reject) which feeds Phase 9 (per-scope test execution).

**Files:**
- Modify: `puzzleeval-api/services/pipeline_runner.py` — after Agent 4.5 produces validated candidates with verified `covers_step_ids`, emit `selection_required` with per-scope groupings. Await `state.selection_ready`. Then Agent 5 runs on the user-approved per-scope lists.
- Modify: `puzzleeval-api/services/run_manager.py` — add `awaiting_candidate_selection` to status enum; add `selection_ready: asyncio.Event`, `user_scope_picks: dict[str, list[str]] | None` (scope_id → list of candidate names to test at that scope).
- Modify: `puzzleeval-api/routes/runs.py` — new endpoint `POST /pzapi/runs/{run_id}/select-candidates`.
- Modify: `puzzleeval-api/models/api_models.py` — `SelectCandidatesRequest`, `SelectCandidatesResponse`, `UserAddedCandidate`.
- **Shim removal (irreversible):**
  - `PuzzleEval-local/puzzleeval/cli.py` — remove `, inject_registry_candidates` from import; delete the shim invocation.
  - `puzzleeval-api/services/pipeline_runner.py` — delete the matching import + invocation.
  - `PuzzleEval-local/puzzleeval/agents/research.py` — delete the shim function + comments.
- Add: `PuzzleEval-local/puzzleeval/agents/research.py` — new `inject_user_candidates(agent2_result, user_adds: list[UserAddedCandidate]) -> Agent2Result` with `source="user_provided"`, `relevance_score=0.99`, and explicit `covers_step_ids` supplied by the user.
- Modify: `src/hooks/usePipelineRun.ts` — new stage `"selection"`, new event handler.
- New: `src/components/playground/SelectionPanel.tsx` — **per-scope columns**. One column per WorkflowStep. Each column lists candidates covering that scope, pre-sorted by `relevance_score_at_scope`. Each candidate has keep/remove checkbox. Claimed-coverage dots visible next to each candidate (dim) — they turn green after Phase 6.5 verification. Above the columns: the WorkflowDiagram with coverage-highlight interaction (hovering a candidate tints the scopes it covers). Below the columns: "Add custom provider" form (name, docs URL, which scopes it covers — multi-checkbox).
- Modify: `src/components/playground/CoverageMatrix.tsx` — reuse from Phase 4; in selection mode, cells become clickable (click to toggle "test this candidate at this scope").
- New: `src/components/playground/RejectionSummary.tsx` — collapsed summary panel shown after Phase 6.5 completes. Renders per-scope rejection counts: "Scope 1: 5 selected, 3 tested, 2 rejected — click to expand." Expanded: list of rejected candidates with reason category and one-line explanation (TaxAI — enterprise_only; DocAI — docs_unreachable). Respects UX choice (explicit transparency about what failed; no silent substitution).
- Modify: `src/services/api.ts` — `selectCandidates(runId, picks)` with new shape.
- Modify: `src/types/pipeline.ts` — `Stage` adds `"selection"`; new `UserAddedCandidate` with `covers_step_ids: string[]`; `SelectCandidatesRequest` shape updated.

**Schema:**

```python
# puzzleeval-api/models/api_models.py
class UserAddedCandidate(BaseModel):
    name: str
    provider: str
    api_docs_url: str | None = None
    notes: str | None = None
    covers_step_ids: list[str]                 # REQUIRED — which scopes the user's custom provider serves
    source: str = "user_provided"

class SelectCandidatesRequest(BaseModel):
    # Per-scope picks: scope_id -> list of candidate names the user wants
    # tested at that scope. A candidate can appear under multiple scope_ids
    # if it covers multiple scopes (e.g. Zapier under step_1, step_2, step_3).
    scope_picks: dict[str, list[str]]
    # Candidates the user added
    add: list[UserAddedCandidate]

class SelectCandidatesResponse(BaseModel):
    accepted_count: int                        # total unique (candidate, scope) pairs
    scope_coverage: dict[str, int]             # scope_id -> how many candidates picked at that scope
```

**Pipeline pause shape:**

```python
async def _branch_a_research():
    ...run Agent 2 + Agent 4.5...
    emit("candidates_found", {
        "candidates": [...],
        "workflow": blueprint,
        "per_scope_candidates": {step_id: [candidate_name, ...] for step_id in blueprint.step_ids}
    })
    emit("selection_required", {...})
    state.status = "awaiting_candidate_selection"
    await state.selection_ready.wait()
    # Apply user picks: build the final (scope_id -> candidate_list) map
    a4_result = apply_scope_picks(a4_result, state.user_scope_picks, user_added=state.user_added)
    ...continue to Agent 5 (per-scope testing)...
```

Cancellation: `asyncio.wait([selection_ready, cancel_requested], return_when=FIRST_COMPLETED)` so the run cleanly aborts mid-pause.

CLI: interactive default prints per-scope candidate tables and prompts "keep all at scope 1? (y/edit)" etc.; `--no-interactive` keeps auto-run (top-K per scope, no user intervention).

**Tests:** new `puzzleeval-api/tests/test_routes_select.py` (~10 cases): POST without run → 404; valid per-scope picks → 200 + resume; double-fire rejected; user-added candidate with valid covers_step_ids flows to Agent 5; user-added with `covers_step_ids` referencing an unknown step → 400; cancel during pause → clean shutdown; empty scope_picks → 400 (at least one scope must have picks); same candidate picked at multiple scopes → accepted (tested at each). `tests/test_research.py` adds 4 cases for `inject_user_candidates` (merge, order, dedup by name, respects user-supplied covers_step_ids).

**Verification:** end-to-end browser flow — type multi-scope request → pause on SelectionPanel → edit per-scope picks + add a custom provider → resume → Agent 5 tests only at the picked (candidate, scope) pairs. CLI interactive path. All 246+ existing tests still pass after shim removal (audit `test_pipeline.py` and `test_agent5.py` for any direct shim references and switch fixtures to `inject_user_candidates`).

**Rollback:** `PUZZLEEVAL_USER_SELECTION_ENABLED=False` skips the wait (auto-runs with Phase 7's programmatic per-scope top-K). Shim removal is irreversible — but `inject_user_candidates` provides the same mechanism under user control.

**Phase fingerprint:** `metadata.user_selection_applied=true` in pipeline_summary; `metadata.user_added_candidates_count >= 0`; `metadata.selection_required_emitted_at` timestamp.

**Diagnostic flag:** `PUZZLEEVAL_USER_SELECTION_ENABLED` — flip to disable the pause (auto-runs with Phase 7's selection).

**Complexity:** L.

---

## Phase 6.5 — Deep verify, drop-on-reject (was Phase 4.5) (new)

**Status:** SHIPPED (2026-04-15). Schema + deep-verify prompt + selection module + pipeline wiring + SSE events + tests + docs. The directed 4A→4B→4C→4D prompt is in `deep_verify_prompt.py`; the per-scope selector is in `selection.py`; pipeline_runner emits `candidate_verified` / `candidate_rejected` / `scope_verified_complete` per-scope. The Phase 1.5 content-quality + Phase 1 web-fetch fallback integrate into the 4A discovery phase. Phase 5b pricing extraction is built into the 4B spec-extraction prompt.

**Goal:** For each (scope, selected-candidate) pair produced by Phase 6 (user picks) + Phase 7 (programmatic top-K), run a production-grade deep-verify pass on **each unique tool** in the selection. Produce `api_spec.txt` per candidate that passes; **on rejection, drop the candidate from that scope's tested set** — no backfill, no substitution. A scope selected with K=5 candidates where 2 reject ends up testing 3. The 2 rejections surface explicitly in the results panel as a rejection list with reasons. This is the agent redesign that fixes today's "shallow Agent 4 sometimes produces useless HTTP" problem.

**Why drop instead of fallback/substitution:** dropping is simpler, cheaper, and honestly more informative — "TaxAI claimed a public API but actually requires sales contact" is real market signal the user should see, not something to conceal behind a silent substitution. User agency is preserved: a user who explicitly picked TaxAI is told why it failed, not second-guessed with TaxPro. Predictable cost and latency: fixed K deep-verifies per scope, no indeterminate "how many backfills this run." If a user needs guaranteed N confident passes, they over-select in Phase 6 (picking 7 to get ~5 verified).

**Why it moved here (from old Phase 4.5):** under the old placement, we deep-verified EVERY validated candidate — expensive waste on candidates that were never going to be tested. Moving it downstream of Phase 6/7 means we pay deep-research cost ONLY on candidates we'll actually build and test. Tool deduplication: a candidate covering M scopes gets deep-verified ONCE even though it occupies M scope slots. For a 3-scope paid-tier run (15 scope slots), typically ~10-12 unique tools deep-verified.

**What "deep verify" means — the 4-phase directed loop per candidate:**

Agent 4 stops being a single-shot classifier and becomes a directed multi-phase loop, same overall shape as Agent 5's builder but pointed at DISCOVERY + EXTRACTION:

```
Phase 4A — Discovery (3-4 turns, ~$0.10)
  1. Try Agent 2's claimed docs_url. If resolves to real content → 4B.
  2. If 404 / SPA shell / Cloudflare / login wall / marketing only:
     a. web_search 'site:<domain> API documentation'
     b. web_search 'site:<domain> developers'
     c. web_search '<provider> REST API endpoints'
     d. web_search 'github <provider> SDK' (SDK repos link to canonical docs)
  3. If still nothing conclusive:
     a. web_search '<provider> API pricing' (pricing pages often link docs)
     b. archive.org check for historical docs
  4. If nothing found → proceed to 4D with REJECT(docs_unreachable)

Phase 4B — Spec Extraction (3-4 turns, ~$0.20)
  Extract into api_spec.txt sections:
    BASE_URL, AUTH_HEADER (exact format), ENDPOINTS[] (path+method+request_format per),
    RESPONSE_FORMAT, SDK_PACKAGE, ACCEPTED_INPUT_FORMATS, PYTHON_EXAMPLES (verbatim),
    PRICING_BREAKDOWN (or link to pricing page fetched here), OPENAPI_URL,
    ACCESS_METHOD enum (free_tier | free_signup | trial | paid_only | sales_contact_only)
    SIGNUP_URL (self-service signup page if ACCESS_METHOD != sales_contact_only)

  ROUTING_TABLE (CRITICAL — added so Agent 5 picks the right endpoint per scope):
    For each scope_id the candidate (claims to) cover, list endpoints that
    serve it, ranked most-specific first. Example for a provider that has
    both generic OCR and specialized invoice parsing:

      ROUTING_TABLE:
        scope=invoice_parsing:
          - endpoint: POST /v1/invoices/parse    (primary — purpose-built for invoice fields)
          - endpoint: POST /v1/documents/extract (fallback — generic doc extraction)
        scope=general_ocr:
          - endpoint: POST /v1/ocr               (primary — specialized OCR)
          - endpoint: POST /v1/documents/extract (fallback — same generic)

    Primary = endpoint whose docs explicitly target this scope's role.
    Fallback = generic endpoint that COULD serve the scope with lower fit.
    Phase 9's Agent 5 builder reads this table and uses primary endpoints
    unless they fail at runtime — only then falls back to the fallback.

Phase 4C — Scope Coverage Verification (1-2 turns, ~$0.05)
  For each scope the candidate CLAIMED to cover:
    Does the API have endpoints that plausibly serve this scope?
    Downgrade "claimed" scopes with no matching endpoint → remove from covers_step_ids
    Upgrade confirmed "claimed" → "verified" in coverage_confidence
    Populate ROUTING_TABLE[scope_id] with the ranked endpoint list for this scope.
    If a scope has only FALLBACK endpoints (no specialized one), flag
    coverage_confidence[scope_id] = "verified_generic" (distinct from full "verified").

Phase 4D — Decision with guardrails (0 turns, deterministic on gathered evidence)
  PASS conditions (all must be true):
    - Got to real API docs with endpoint signatures (from 4A)
    - ACCESS_METHOD is NOT sales_contact_only AND SIGNUP_URL resolves
      (not just marketing "contact us" page — must be a real signup flow)
    - At least one scope coverage claim verified in 4C
    - ROUTING_TABLE populated with ≥1 endpoint per verified scope

  REJECT categories:
    - docs_unreachable    (exhausted 4A search variants + archive + SDK fallback)
    - enterprise_only     (API exists but requires sales contact for a key,
                           OR signup flow is not self-service / has waitlist
                           that blocks immediate access)
    - deprecated          (docs indicate sunset or platform migration)
    - no_api              (verified only UI / file-export / no programmatic API)

  Anti-false-negative guardrails (MUST run before emitting REJECT):
    - One required ask_research sub-agent call: "Find the current API docs for <provider>."
      If sub-agent surfaces a URL Phase 4A didn't try, restart 4B from it.
    - Archive.org historical snapshot as alt source.
    - GitHub SDK with ≥500 stars counts as evidence that API exists.
    - Check common 3rd-party doc hosts: Postman public workspaces
      (postman.com/<provider>), ReadMe Hub (<provider>.readme.io),
      Stoplight (<provider>.stoplight.io), SwaggerHub (app.swaggerhub.com/apis/<provider>).

  Anti-false-positive guardrails (MUST pass before emitting PASS):
    - Docs URL loaded REAL content (passes Phase 1.5 content-quality check,
      not an SPA shell with no fetched content).
    - Marketing copy ≠ docs. "We offer APIs for enterprise customers" with
      no docs link is NOT a pass.
    - Auth method MUST be programmatic (api_key / oauth / bearer), not
      "contact us" or "negotiated".
    - Signup URL must be REACHABLE (HTTP 200 or documented redirect to a
      signup form) — not a 404 "coming soon" placeholder.

Per-candidate cost: ~$0.35-0.50 successful verify (slightly higher than
original $0.35-0.45 due to ROUTING_TABLE population + 3rd-party host checks);
~$0.20-0.30 for reject (short-circuits).
```

**The deep-verify pool (drop-on-reject, no backfill):**

```python
async def verify_scope_pool(scope_id, selected_candidates, run_tool_cache):
    """
    Deep-verify every candidate in the scope's top-K selection in parallel.
    Candidates that pass go to `verified`; rejections go to `rejected` with
    categorized reasons. No backfill. Verified count may be less than K if
    any candidates rejected; scope tests with however many passed.
    """
    verified = []
    rejected = []

    # Separate fresh-to-verify from already-cached (tool covers multiple scopes)
    to_verify_fresh = []
    for c in selected_candidates:
        if c.name in run_tool_cache:
            cached = run_tool_cache[c.name]
            # Cached globally but check whether THIS scope's coverage was verified
            if scope_id in cached.covers_step_ids:
                verified.append(cached.view_for_scope(scope_id))
            else:
                # Cached spec exists but 4C didn't confirm this scope earlier;
                # run just 4C for this scope against the cached spec
                scope_check = await verify_scope_against_cached_spec(cached, scope_id)
                if scope_check.verified:
                    cached = cached.with_scope_verified(scope_id)
                    run_tool_cache[c.name] = cached
                    verified.append(cached.view_for_scope(scope_id))
                else:
                    rejected.append(FailedToVerify(
                        name=c.name, provider=c.provider, scope_id=scope_id,
                        reason="coverage_removed_at_scope",
                        attempt_notes=scope_check.notes,
                    ))
        else:
            to_verify_fresh.append(c)

    # Fresh deep-verify: all of them in parallel (ThreadPoolExecutor)
    results = await asyncio.gather(*[
        agent_4_deep_verify(c, scope_id) for c in to_verify_fresh
    ], return_exceptions=True)

    for c, result in zip(to_verify_fresh, results):
        if isinstance(result, Exception):
            rejected.append(FailedToVerify(
                name=c.name, provider=c.provider, scope_id=scope_id,
                reason="verify_error", attempt_notes=str(result)[:200],
            ))
            continue
        if result.verdict == "PASS" and scope_id in result.verified_scopes:
            run_tool_cache[c.name] = result
            verified.append(result.view_for_scope(scope_id))
        else:
            # Two rejection modes: candidate failed overall, OR candidate
            # passed overall but THIS scope's coverage was removed in 4C
            reason = result.reject_category if result.verdict == "REJECT" else "coverage_removed_at_scope"
            rejected.append(FailedToVerify(
                name=c.name, provider=c.provider, scope_id=scope_id,
                reason=reason, attempt_notes=result.notes,
            ))

    # Note: len(verified) may be < len(selected_candidates). Intentional.
    # Scope proceeds to Phase 9 testing with however many candidates passed.
    return verified, rejected
```

Per scope: exactly K deep-verifies max (no backfill attempts). Global tool cache (run-scoped) prevents double-verify when a multi-scope candidate occupies slots in several scope queues — the first scope that selects provider X pays the verify cost; subsequent scopes reuse the cached api_spec, with lazy per-scope 4C re-check if scope wasn't verified in the earlier pass.

**Per-run cache scoping (not process-global):**

The "global tool cache" above is **global within a single pipeline run**, not across the FastAPI process. Concretely: the cache lives on `RunState` (keyed by `run_id`), populated by Phase 6.5 for that specific run, cleared when the run finalizes. Two concurrent runs (user A doing invoice OCR, user B doing chatbot selection) each have their own cache. Zapier verified for run A is NOT reused for run B — different user-understanding context means different coverage claims and different scope sets to verify. Cross-run cache reuse is a cloud-migration concern (see Cloud Migration Touchpoints section) — on single-host local dev it's fine to re-verify across runs.

**Cancellation semantics during Phase 6.5:**

User cancellation while deep-verify is in flight needs a clean path:
- Phase 6.5 is implemented as `ThreadPoolExecutor` with per-candidate futures (same shape as today's Agent 4 parallelism).
- `RunState.cancel_requested` is checked between each future completion. When set:
  - In-flight futures are allowed to complete (Python's GIL + thread-safe Anthropic client means we can't safely kill mid-request) — but their results are discarded.
  - No new deep-verify futures are submitted.
  - `pipeline_cancelled` SSE event emitted with `cancelled_at: "phase_6.5_deep_verify"` and a count of `candidates_verified_before_cancel`.
  - Cached partial verifications are DISCARDED (not carried to a resumed run).
- Budget impact of cancellation: up to K in-flight candidates' worth of deep-verify cost is already committed at cancel time. User sees what was spent in the `cost_usd` field of the cancellation event.

This is intentional — abrupt resumption is a hard problem we don't need to solve. If a user wants to re-run, they pay for a fresh verify pass. Simple, predictable.

**Files to modify:**
- Modify: `PuzzleEval-local/puzzleeval/agents/screening.py` — rewrite Agent 4 as the 4A→4B→4C→4D directed loop. Raise `max_uses` on `web_fetch` to 6 and `web_search` to 5 (quality over frugality for the candidates we actually care about).
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add `api_spec_path: str | None` to `ScreenedCandidate`; add new `FailedToVerify` model with `name`, `provider`, `scope_id`, `reason` (enum), `attempt_notes`.
- Modify: `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — **drop Phase 1 research entirely**. Agent 5 reads `api_spec.txt` from sandbox at Phase 2 start. `ask_research` remains for mid-build gap-filling.
- Modify: `puzzleeval-api/services/pipeline_runner.py` — add new SSE events:
  - `candidate_verified` {candidate_name, scope_id, attempt_rank}
  - `candidate_rejected` {candidate_name, scope_id, reason}
  - `scope_verified_complete` {scope_id, verified_count, rejected_count}
- Modify: `src/components/playground/ResultsComparison.tsx` — integrate `RejectionSummary` at the top of the per-scope results area. Per-scope tables render only verified/tested candidates; rejections surface in the summary panel with reason category + one-line explanation.
- Modify: `src/types/pipeline.ts` — new `FailedToVerify` type, `Candidate.coverage_confidence` upgrades to verified.

**Schema additions:**

```python
class ScreenedCandidate(BaseModel):
    ...
    api_spec_path: str | None = Field(
        default=None,
        description=(
            "Absolute path to api_spec.txt produced by Phase 6.5 deep-verify. "
            "Agent 5 reads this at build time instead of re-researching. "
            "None for candidates not deep-verified (rejected or never selected)."
        ),
    )
    # covers_step_ids inherited from Candidate; Phase 6.5 OVERWRITES with
    # verified truth. coverage_confidence upgrades claimed→verified per scope.
    # Scopes that claimed but couldn't be verified are REMOVED from covers_step_ids.

class FailedToVerify(BaseModel):
    name: str
    provider: str
    scope_id: str                        # which scope slot this was attempted for
    reason: Literal[
        "docs_unreachable",
        "enterprise_only",
        "deprecated",
        "no_api",
        "coverage_removed_at_scope",     # candidate passed overall but not for THIS scope
        "verify_error",                  # transient error during deep-verify (timeout, rate limit)
    ]
    attempt_notes: str                   # Agent 4's trail of search attempts for audit
    # (No attempted_at_rank — drop-on-reject means rank isn't meaningful;
    # there's no fallback queue where position mattered.)
```

**Config:**
- `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` (default True) — if False, Agent 4 reverts to shallow pass/fail (legacy behavior + no api_spec + no fallback, Agent 5 resumes Phase 1 research)

**Cost math (3-scope paid-tier workflow, K=5 per scope):**
- Unique tools in ranked pool across all scopes: ~20 (candidates + all-in-ones)
- Unique tools selected (top-K + any user-added): ~12 (after dedup)
- Rejection rate assumption: 20% → scopes end up with ~4 tested (instead of 5); no extra verifies, just fewer passes
- Deep verify: 15 attempts × $0.40 avg = ~$6
- Agent 5 build (no Phase 1): 12 tools × $1.25 = $15 (vs $10 for 4 tools before Phase 9)
- Tests: 15 scope-tool pairs × $0.50 = $7.50
- **Total: ~$29 for 3-scope.** Compared to ~$12 today for a 4-tool single-scope: the pivot buys MORE testing (15 pairs vs 4) at 2.5× the cost, bounded linearly.

**Agent 5 simplification (implicit side effect):**
- `BUILDER_SYSTEM_PROMPT` loses Phase 1 research instructions (keeps a short "how to read api_spec.txt" orientation section). Phase 2 starts with "api_spec.txt is in your sandbox — read it first, then build harness.py".
- Typical build turns drop from 7-9 to 4-6 (Phase 1 was 2-3 of those turns).
- Cost per build: ~$1.25 → ~$0.95.

**Tests:**
- `tests/test_agent4.py` extends with ~15 new cases covering the 4A→4B→4C→4D loop:
  - 4A finds docs via site-scoped search when claimed URL is 404
  - 4A uses archive.org when all live options fail
  - 4A uses GitHub SDK as alt doc source
  - 4B produces api_spec.txt with required sections
  - 4B handles auth-gated docs pages
  - 4C downgrades unverifiable coverage claims
  - 4C upgrades confirmed claims to "verified"
  - 4D PASS requires all three conditions
  - 4D anti-false-positive: marketing pages rejected
  - 4D anti-false-negative: ask_research fallback catches missed URLs
  - Full pipeline: rejected candidate is dropped, scope tests with verified-only subset (no substitution)
  - Drop-on-reject: scope with 2 of 5 rejected tests with 3; result panel shows 2 rejections + reasons
  - Global tool cache: multi-scope candidate verified once
  - FailedToVerify telemetry populated with correct reason enum values
  - api_spec_path set only for PASS candidates

- `tests/test_agent5.py` updates ~3 existing cases to assume Phase 1 is skipped (read api_spec fixture, no search mocking).

**Verification:**
- CLI end-to-end: `python -m puzzleeval.cli --text "..." --agent5 --pretty` — output shows `scope_selections` with `api_spec_path` set on each; `failed_to_verify` list populated for any rejections; run completes with `scope_runs` showing tests for verified candidates only.
- Disk artifacts: `runs/{trace_id}/harnesses/{slug}/api_spec.txt` exists for each verified candidate; file contains all Phase 4B sections.
- Bench (Phase 10): include a domain where known candidates have auth-gated docs; verify that Agent 4's ask_research fallback or archive.org fallback activates and the candidate passes anyway.

**Rollback:** `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED=False` reverts Agent 4 to today's shallow pass/fail verification + Agent 5 resumes Phase 1 research. No api_spec.txt, no FailedToVerify telemetry.

**Phase fingerprint:** `scope_selections[scope_id][*].api_spec_path` non-null for verified candidates; `failed_to_verify` list populated with structured rejections; SSE events `candidate_verified` / `candidate_rejected` / `scope_verified_complete` observable in the stream; `pipeline_summary.json` metadata: `deep_verify_attempts` (total, = sum of K across scopes), `deep_verify_rejections` (count), `scopes_under_capacity` (count of scopes where verified < selected K).

**Diagnostic flag:** `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` — atomic on/off for Phase 6.5 (also re-enables Agent 5 Phase 1 when False so the pipeline stays functional).

**Complexity:** L (directed Agent 4 loop is the biggest code addition; drop-on-reject + global cache is ~60 lines, simpler than fallback would have been; removing Agent 5 Phase 1 is additive simplification).

---

## Phase 7 — Per-scope top-K selection (todo #2)

**Status:** SHIPPED (2026-04-15). `puzzleeval/selection.py` module + config constants + billing `scope_candidates_cap()` + pipeline_runner wiring + tests.

**Goal:** Replace the global `(_has_credentials, relevance_score)` sort with **independent per-scope top-K selection**. For each scope in the workflow, rank candidates whose CLAIMED `covers_step_ids` includes that scope, take top K (tier-aware). A candidate that covers M scopes competes in M scope rankings independently — it can make top-K at every scope it covers, some, or none. Coverage count is NOT a scoring input (don't privilege "broader" tools); coverage is a presentation detail.

**Runtime ordering:** Phase 7 runs on **Agent 2's output only** — CLAIMED coverage, no deep verification yet. Phase 6 user picks OVERRIDE Phase 7's output (if the user provides per-scope picks, those replace Phase 7's programmatic defaults). **Phase 7's output is the FINAL top-K per scope** — exactly K candidates per scope go to Phase 6.5 for deep-verify. There's no fallback queue; Phase 7 does not maintain a ranked pool beyond K for later substitution. If a user wants guaranteed N confident passes, they over-select in Phase 6 (e.g. pick 7 candidates to account for expected ~20% reject rate).

**Why seventh:** consumes the DAG (#3) for scope list; consumes Agent 2's ranked pool (#4). Feeds Phase 6 (initial UI defaults) and Phase 6.5 (deep-verify). Scope-independent rankings keep Phase 6.5's logic simple — just parallel-verify the selected K candidates, no ordering needed at verify time.

**Auto-run path when user picks aren't present:** `PUZZLEEVAL_USER_SELECTION_ENABLED=False` sends no user scope_picks; Phase 7's programmatic top-K is the selection, passed directly to Phase 6.5 for deep-verify. That's the legacy auto-run path for scripts.

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — replace `_select_candidates` with `_select_scope_candidate_pairs`. Returns `dict[scope_id, list[ScreenedCandidate]]`.
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add optional `user_constraints` (budget cap) to `Agent5Input`.
- Modify: `PuzzleEval-local/puzzleeval/config.py` — `AGENT5_SCOPE_SELECTION_WEIGHTS` dict, `SCOPE_CANDIDATES_CAP_BY_PLAN` dict (tier-aware).
- Modify: `puzzleeval-api/services/billing.py` — expose `scope_candidates_cap(plan)` helper so Agent 5 can size K correctly.

**Scoring logic (per-scope):**

```python
def _score_at_scope(c: ScreenedCandidate, scope_id: str, has_creds: bool,
                   user_picked_here: bool, user_budget: float | None) -> float:
    # Scope eligibility check: if c doesn't cover this scope, not eligible at all.
    if scope_id not in c.covers_step_ids:
        return 0.0  # excluded

    w = AGENT5_SCOPE_SELECTION_WEIGHTS
    score  = w["user_picked_here"] * (1.0 if user_picked_here else 0.0)
    score += w["credentials"]      * (1.0 if has_creds else 0.0)
    score += w["relevance_at_scope"] * _per_scope_relevance(c, scope_id)   # how well does c serve THIS scope, not overall
    score += w["docs_quality"]     * _docs_quality_score(c)
    score += w["pricing_fit"]      * _pricing_fit_score(c, scope_id, user_budget)
    # NB: no coverage_count term — a multi-scope tool doesn't get a bonus here.
    return score
```

Default weights: `user_picked_here=0.40, credentials=0.20, relevance_at_scope=0.20, docs_quality=0.10, pricing_fit=0.10`.

Per-scope cap (tier-aware, from `SCOPE_CANDIDATES_CAP_BY_PLAN`):
- `free`: 3 per scope
- `paid`: 5 per scope
- `enterprise`: 10 per scope

Total tests per run = `K × N` (K per-scope cap × N scopes in the blueprint). Bounded, linear in workflow size. For a 5-scope paid run: 25 tests max.

**Important: dedup for build.** A candidate selected at M scopes still gets ONE harness build (Agent 5 Phase 9 runs test cases from each selected scope against the same harness). See Phase 9's tool-deduplication logic.

**Presentation rule:** even if a candidate never makes top-K at any scope (e.g., ranks 8th at every scope), show it in the **CoverageMatrix** (Phase 4) for completeness — lets users who value "one vendor for everything" see the honest trade-off. DON'T show it in per-scope tables.

**Tests:** `tests/test_agent5.py` adds 8 cases:
- Specialist covering 1 scope competes only at that scope
- All-in-one covering 5 scopes appears in all 5 scope rankings
- Same candidate can be picked at multiple scopes
- User_picked_here dominates at the scope it was picked (but not at other scopes)
- Tier cap enforcement (free=3, paid=5, enterprise=10)
- Non-contiguous coverage (covers scopes 1, 3, 5 but not 2, 4)
- Coverage count is not a scoring input (5-scope tool doesn't outrank 1-scope specialist at OCR)
- Rejected candidates (coverage=∅) don't appear anywhere

**Verification:** `python -m puzzleeval.cli --text "..." --agent5 --no-interactive` — output has `scope_selections: dict[scope_id, [candidate_name, ...]]` with size bounded by K per scope.

**Rollback:** `AGENT5_SCOPE_SELECTION_WEIGHTS={"relevance_at_scope": 1.0}` plus low cap reverts to simple per-scope top-N by relevance.

**Phase fingerprint:** `agent_5_output.json.scope_selections` present; size bounded by K × N; no candidate appears with an empty covers-scope intersection.

**Complexity:** M.

---

## Phase 8 — API doc understanding (retargeted to Phase 6.5) (todo #3)

**Status:** SHIPPED (2026-04-15). OpenAPI hunting, common-URL probing, site-scoped search, skip-narrative optimization, endpoint completeness enforcement, COMMON_OPENAPI_PATHS + THIRD_PARTY_DOC_HOSTS constants, build_deep_verify_message URL hints. 9 tests.

**Goal:** Make Phase 6.5's **Phase 4B — Spec Extraction** turn-phase explicitly target OpenAPI / Swagger specs, traverse multi-page docs, and pivot to GitHub SDK repos when docs are auth-gated. Same prompt improvements as originally scoped for Agent 5 Phase 1 — they now land in the directed 4A→4B→4C→4D loop's extraction step.

**Why eighth:** independent surgical improvement to Phase 6.5's deep-verify quality. Lifts api_spec.txt completeness for every verified candidate, which in turn lifts Agent 5's build success rate in Phase 9. Lands after Phase 6.5's directed-loop structure is in place — we're sharpening the extraction turn-phase specifically.

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/agents/screening.py` — Phase 6.5's 4B prompt gains:
  1. First attempt: `web_search` `site:{domain} openapi.json OR swagger.json OR .well-known/openapi.yaml`.
  2. Second: try `web_fetch` on `/openapi.json`, `/api/docs/openapi.json`, `/v1/openapi.json`, `/swagger.json` (common conventions).
  3. If spec found, parse endpoints directly into `api_spec.txt` without reading narrative docs.
  4. Multi-page docs: if `web_fetch` result contains "Next: X" or "See also: Y", model MUST fetch ≥2 more pages before completing. Enforce: "ENDPOINTS section must list ALL endpoints, not just quickstart."
  5. Auth-gated docs: tie into Phase 4A's GitHub-SDK and archive.org fallback — instead of re-doing that work inside 4B, 4B consumes 4A's discovered alt-source URL if present.
  6. Required `api_spec.txt` field: `OPENAPI_URL: <url or "not found">`.

**Tests:** `tests/test_agent4.py` adds 4 cassette-based cases targeting the extraction phase specifically (spec found on first try; spec missing → endpoints parsed from narrative docs; multi-page combine; auth-gated → SDK-as-source path).

**Verification:** live run inspects `runs/{trace_id}/harnesses/{slug}/api_spec.txt` produced by Phase 6.5 — has `OPENAPI_URL` line, has more endpoints than before. Phase 10 bench passes for translation / summarization domains that previously tripped on doc quality.

**Rollback:** prompt-only change; revert the 4B system-prompt additions. Phase 6.5's loop still works at prior extraction quality.

**Phase fingerprint:** verified candidates' `api_spec_path` files contain `OPENAPI_URL: https://...` lines (non-"not found") for most candidates. Count of `OPENAPI_URL: not found` should be <20% of verified candidates.

**Complexity:** S.

---

## Phase 9 — Per-scope test execution (todo #6)

**Status:** SHIPPED (2026-04-15). ScopeTestRun schema + scope_routing.py (group_tests_by_scope, build_scope_runs, dedup_tools_for_build) + ResultsComparison rewrite + SCOPE_TEST_MODE flag + 15 tests.

**Goal:** For a multi-scope workflow, Agent 5 tests the top-K candidates **at each scope independently**. No end-to-end workflow chaining. No combinatorial combination matrix. No integration impedance headaches. The user sees per-scope rankings (and a candidates × scopes matrix view) — enough to compose their own solution mentally.

**Why we dropped workflow chaining:** combinatorial cost explosion (K^N builds), integration-impedance failures (tool A's JSON doesn't match tool B's input shape), and the realization that **per-scope data is what real buyers want**. Knowing "Mindee scores 0.95 at OCR, Klippa 0.87, Zapier 0.75" is directly actionable; knowing "the Mindee+Zapier+Sheets chain scored 0.83 as a pipeline" requires more interpretation and may not generalize if the user picks different tools later. See earlier discussion in Phase 3's "Why the pivot to DAG + per-scope testing".

**Why ninth:** biggest implementation change in the roadmap, but far simpler than the old chained-harness design. Needs DAG (#3), coverage sets (#4), Agent 4.5's api_spec (#4.5), pricing (#5), user picks per scope (#6), per-scope top-K (#7).

**Files:**
- Modify: `PuzzleEval-local/puzzleeval/schemas.py` — add `ScopeTestRun`; extend `Agent5Result` with `scope_runs: list[ScopeTestRun]`. Drop the old `workflow_harnesses` / `WorkflowCombination` / `WorkflowHarness` / `WorkflowTestRun` from the roadmap entirely.
- Modify: `PuzzleEval-local/puzzleeval/agents/implement_test_env.py`:
  - `_execute_all_tests` becomes `_execute_scope_tests` — routes test cases by `test_case.sub_task_ref → scope_id ∈ candidate.covers_step_ids`.
  - Build-time tool deduplication: collect unique tools across the `scope_selections` map; build ONE harness per unique tool (not one per scope-tool pair).
  - For each unique tool, run test cases for EVERY scope in `tool.covers_step_ids ∩ selected_scopes`.
  - Aggregate results by scope; emit `scope_runs`.
  - **Routing-aware harness build (CRITICAL, from Phase 6.5's ROUTING_TABLE):**
    - Builder system prompt gains a section: "ROUTING_TABLE in your api_spec.txt lists, for each scope this candidate serves, a ranked list of endpoints. ALWAYS use the PRIMARY (first) endpoint for each scope. Only fall back to the secondary endpoint if the primary returns 404 / 410 / deprecated response during live validation."
    - The harness is **multi-route** when a tool covers multiple scopes: `run(input_data, scope_id)` dispatches to the right endpoint based on `scope_id`. Example (Zapier covering OCR + extract + sync): `run(doc, scope="ocr")` → `POST /ocr`; `run(doc, scope="extract")` → `POST /extract`; `run(data, scope="sync")` → `POST /webhook`.
    - Smoke test verifies: for EACH scope the harness claims, calling `run(sample, scope=scope_id)` produces a structured response (not a 404 / generic error).
    - Post-build validation gate: compare actual endpoints the harness calls against ROUTING_TABLE[scope_id]. If the harness hardcoded a generic fallback when a specialized primary was available → flag for rebuild. Don't let Agent 5 quietly use the wrong endpoint.
- Modify: `puzzleeval-api/services/pipeline_runner.py` — new SSE events `scope_test_started` (fired per-scope) and `scope_test_result` (fired per candidate-at-scope result).
- Modify: `src/types/pipeline.ts` — add `ScopeTestRun`, drop `WorkflowCombination` / `WorkflowTestResult`.
- **Rewrite** `src/components/playground/ResultsComparison.tsx` — two primary views:
  1. **Per-scope tables** (primary) — one section per scope, top-K candidates with score / cost / coverage-badge / test results.
  2. **Coverage matrix** (secondary, reuse from Phase 4) — candidates × scopes, cells show test scores where tested, cells show "not covered" where the tool doesn't serve that scope.
- Modify: `src/components/playground/WorkflowDiagram.tsx` — extend with coverage-highlight interaction: selecting a candidate row in a per-scope table tints the DAG nodes that candidate covers. Multi-select shows union of coverage.

**Schema:**

```python
class ScopeTestRun(BaseModel):
    scope_id: str                         # e.g. "step_1"
    scope_role: str                       # e.g. "ocr" — from WorkflowStep.role
    candidate_results: list[CandidateTestRun]   # ordered by aggregate score at this scope
    test_case_count: int                  # how many Agent 3/3F test cases ran at this scope

class Agent5Result(BaseModel):
    ...
    scope_runs: list[ScopeTestRun]        # NEW — one per scope that had at least one candidate tested
    # existing per-candidate fields KEPT for backward compat with single-step flows
    # (a 1-scope workflow just produces 1 ScopeTestRun with the same tools as candidate_runs)
```

**Execution logic (Phase 9's outer loop):**

```python
def run_implement_test_env_agent(input_data):
    blueprint = input_data.user_understanding.workflow
    scope_selections = _select_scope_candidate_pairs(...)  # Phase 7 output
    # scope_selections: dict[scope_id, list[ScreenedCandidate]]

    # Dedup: one harness per unique tool across all scopes
    unique_tools = {c.name: c for candidates in scope_selections.values() for c in candidates}

    # Build harnesses in parallel — API spec already in sandbox from Agent 4.5
    with ThreadPoolExecutor(max_workers=AGENT5_MAX_PARALLEL) as executor:
        harnesses_by_tool_name = {
            name: executor.submit(_build_single_harness, c, ...)  # no Phase 1 research; reads api_spec.txt
            for name, c in unique_tools.items()
        }
    # => N unique tools built, not K × N scope-tool pairs

    # Per-scope test execution
    scope_runs = []
    for scope_id, candidates_at_scope in scope_selections.items():
        scope_results = []
        for candidate in candidates_at_scope:
            harness = harnesses_by_tool_name[candidate.name]
            scope_tests = [tc for tc in input_data.test_cases.test_cases
                          if tc.sub_task_ref == scope_id]
            # Harness.run() dispatches to the ROUTING_TABLE[scope_id] primary
            # endpoint. One harness, N scopes' worth of routes inside it.
            candidate_run = _run_tool_at_scope(harness, candidate, scope_id, scope_tests)
            scope_results.append(candidate_run)
        scope_runs.append(ScopeTestRun(
            scope_id=scope_id,
            scope_role=next(s.role for s in blueprint.steps if s.id == scope_id),
            candidate_results=sorted(scope_results, key=lambda r: -r.aggregate_score),
            test_case_count=len(scope_tests),
        ))

    return Agent5Result(harnesses=[...], scope_runs=scope_runs, ...)
```

**Why the routing-aware harness matters (user's "no OCR for invoice parsing" concern):**

Without ROUTING_TABLE, Agent 5 might write a Zapier harness that calls `/webhook` for every scope — a generic catch-all endpoint. With ROUTING_TABLE, Agent 5 writes a Zapier harness that dispatches per scope: `/invoices/parse` for invoice scope, `/webhook` for sync scope, etc. The test results now actually measure how good Zapier is AT THAT SPECIFIC SCOPE, not how good Zapier is as an undifferentiated endpoint. This is the single biggest quality lever for per-scope testing to produce meaningful comparisons.

**Cost math (target budget):**

For 5-scope workflow, paid tier K=5 per scope:
- Unique tools across scope selections: ~10-14 (specialists + multi-scope tools + all-in-ones; dedup reduces from 25 slots to ~12 tools)
- Harness builds: 12 × $0.95 = ~$12 (with Phase 6.5's api_spec; Agent 5 skips Phase 1)
- Per-scope tests: 25 scope-tool pairs × $0.50 = ~$13
- **Total: ~$25 for a 5-scope workflow.** Linear in unique tool count; linear in scope count × K. Not exponential.

For 3-scope workflow: ~$15.
For 1-scope workflow: ~$6 (4 tools × $0.95 build + 4 × $0.50 test; matches today's behavior).

**Tests:** `tests/test_agent5.py` adds 10 cases replacing the workflow-chain cases:
- Single-scope workflow → results match legacy 1-scope behavior
- 3-scope workflow, all specialists → 3 ScopeTestRuns with disjoint candidates
- 3-scope workflow, 1 all-in-one tool → all-in-one appears in all 3 ScopeTestRuns
- Tool deduplication: multi-scope tool builds ONCE, tests N times
- Coverage-routed test execution: scope_1 test cases never run against scope_2-only tool
- Non-contiguous coverage: tool covering scopes 1, 3 appears in scope 1 and scope 3 runs only
- Per-scope aggregate scoring: same tool can score differently at different scopes
- Result ordering within scope: sorted by aggregate_score descending
- No scope chaining: `scope_runs[i]` doesn't reference `scope_runs[i+1]` outputs
- Budget cap enforcement: respects `SCOPE_CANDIDATES_CAP_BY_PLAN`

**Verification:** `python -m puzzleeval.cli --text "3-step workflow description" --agent5 --no-interactive --pretty` → output has `scope_runs` list with length = number of scopes; each contains `candidate_results` list ≤ K. UI ResultsComparison renders per-scope tables + coverage matrix. No `workflow_harnesses` artifact.

**Rollback:** `PUZZLEEVAL_SCOPE_TEST_MODE=False` reverts to today's single-scope behavior (builds for all candidates, no scope routing — equivalent to 1-scope blueprint path). Schema additions are optional; legacy consumers reading only `harnesses` still work.

**Phase fingerprint:** `agent_5_output.json.scope_runs` length equals number of scopes in the blueprint. Each `ScopeTestRun.candidate_results` non-empty for scopes that had selected candidates. `unique_tools_built` = count of deduplicated harnesses (logged as metadata) — typically much less than K × N.

**Diagnostic flag:** `PUZZLEEVAL_SCOPE_TEST_MODE` — flip False to revert to legacy flat-candidate testing.

**Complexity:** M. (Was XL when it was workflow-chaining — dropping chaining cuts the complexity roughly in half.)

---

## Phase 10 — Generalizability benchmark (todo #8)

**Status:** SHIPPED (2026-04-16). 10-domain benchmark framework + 39 tests + bench runner + domain configs + pyproject marker gating. All 10 domains validate cleanly in dry-run mode.

**Goal:** A 6-domain benchmark suite (OCR, chatbot, classification, translation, summarization, data extraction) that runs end-to-end and asserts quality + cost budgets. Regression gate for everything above.

**Why last:** validates that the previous 9 phases didn't narrow PuzzleEval to the OCR happy path.

**Files:**
- New: `PuzzleEval-local/tests/generalizability/` with `__init__.py`, `conftest.py` (markers + fixtures), `test_generalizability.py` (parametrized over domains), `domains/{ocr,chatbot,classification,translation,summarization,data_extraction}.json`.
- New: `PuzzleEval-local/bench/run_benchmark.py` — CLI runner (`--domain ocr --live` records new cassette; `--domain ocr` replays).
- New: `PuzzleEval-local/bench/cassettes/{domain}/`, `bench/results/`, `bench/README.md`.
- Modify: `PuzzleEval-local/pyproject.toml` — add `vcrpy>=6.0` to dev deps.
- New: `pytest.ini` config block — `markers = generalizability: long-running cross-domain benchmarks`; default invocation excludes (`-m "not generalizability"`).
- Optional: `.github/workflows/benchmark.yml` — weekly cassette-replay run.

**Domain config sketch (per-scope assertions):**

```json
{
  "name": "invoice_workflow",
  "input": "Photos of invoices, extract line items, then sync to Google Sheets.",
  "expected_scopes": ["ocr", "extract", "spreadsheet_sync"],
  "min_candidates_per_scope": 3,
  "max_cost_usd": 30.0,
  "per_scope_assertions": {
    "ocr": {"min_top_pass_rate": 0.85, "min_candidates_tested": 3},
    "extract": {"min_top_pass_rate": 0.80, "min_candidates_tested": 2},
    "spreadsheet_sync": {"min_top_pass_rate": 0.75, "min_candidates_tested": 2}
  },
  "spa_providers_expected": 1
}
```

**Test shape:**

```python
@pytest.mark.generalizability
@pytest.mark.parametrize("domain", [
    "invoice_workflow", "chatbot_simple", "classification_pipeline",
    "translation_chain", "summarization", "multi_step_data_extraction",
])
def test_domain_per_scope(domain):
    config = json.load(open(f"domains/{domain}.json"))
    with vcr_cassette(f"cassettes/{domain}.yaml"):
        result = run_full_pipeline(config["input"])

    # Blueprint assertions
    assert result.workflow is not None
    scopes = {s.role for s in result.workflow.steps}
    assert scopes >= set(config["expected_scopes"])

    # Per-scope assertions
    for scope_role, expected in config["per_scope_assertions"].items():
        scope_run = next(r for r in result.scope_runs if r.scope_role == scope_role)
        assert len(scope_run.candidate_results) >= expected["min_candidates_tested"]
        top_pass_rate = scope_run.candidate_results[0].pass_rate
        assert top_pass_rate >= expected["min_top_pass_rate"]

    # Cost ceiling (bounded-testing property from Phase 9)
    assert result.total_cost_usd <= config["max_cost_usd"]
```

**Domain mix (exercises the specific pipeline behaviors that matter):**

*Base domains (cover the common cases):*
- `invoice_workflow` (3 scopes, OCR + extract + sync, SPA provider: DocuClipper) — exercises Phase 1.5 content-quality detection
- `chatbot_simple` (1 scope) — single-scope regression baseline
- `classification_pipeline` (2 scopes: classify + route)
- `translation_chain` (2 scopes: translate + format)
- `summarization` (1 scope)
- `multi_step_data_extraction` (4 scopes, exercises longest DAG)

*Edge-case domains (exercise pipeline recovery paths) — NEW in this revision:*
- `rejection_transparency` (3 scopes, one scope's pool seeded with 2 known-to-reject candidates at ranks 1-2, valid at rank 3) — asserts scope ends up with 1 tested candidate + 2 `FailedToVerify` entries with correct reason categories; `RejectionSummary` UI surfaces both rejections; NO substitution happens
- `user_added_candidate` (2 scopes, one with `add: [{name: "MyCustomAPI", covers_step_ids: ["step_1"]}]` supplied via `SelectCandidatesRequest`) — asserts user-added provider flows through Phase 6.5 deep-verify; if rejected, scope tests with fewer candidates (no substitution); rejection reason shown in summary
- `long_chain_parallel` (5 scopes with fan-out/fan-in DAG: step_1 → [step_2a, step_2b, step_2c] → step_3 → step_4) — asserts DAG topology preserved, parallel scopes verified/tested independently, no serialization
- `scope_under_capacity` (2 scopes, pool for scope_2 entirely synthetic-reject candidates) — asserts scope_2 ends with 0 `candidate_results`, UI warning surfaced in output, run still completes (not failed)

**Tests:** 10 domain tests + 5 meta-tests (config loads, cassette replay deterministic, missing cassette → skip with warning, per-scope assertion helper, coverage-matrix completeness check, rejection-summary renders correctly).

**Verification:** `pytest -m generalizability` — all 10 pass with cassettes. Default `pytest` keeps the 246+-test green path. `python bench/run_benchmark.py --domain invoice_workflow` prints a human report (per-scope top-3, unique tools built, total cost, per-scope rejections, time).

**Rollback:** marker-gated, separate dir — does not touch the core suite.

**Complexity:** L.

---

## Recommended Execution Order

Phases ordered by **build/ship sequence** (not runtime). Runtime sequence is below the table.

| # | Phase | Original todo | Why here |
|---|-------|---------------|----------|
| 1 | Cloudflare hardening | #9 | Reliability multiplier for every later phase that fetches more docs. Small, self-contained, low-risk. **Shipped.** |
| 1.5 | Content-quality assessment | (extends #9) | Generalizes #1's recovery loop from HTTP errors to any useless-content fetch (SPA shells, auth walls, soft 404s, marketing pages). **Shipped.** |
| 2 | Service tier scaffold | #10 | Has to exist before any new endpoint. No-op default → cannot break anything. **Shipped.** |
| 3 | Workflow Architecture (DAG) | #5 | Agent 1 as director: DAG-shaped blueprint. Every downstream phase consumes it. **Fully shipped (linear + DAG expansion).** |
| 4 | Agent 2 dual search + ranked pool | #7 | Search + rank only — NO verification. Claimed `covers_step_ids`. Cheap pool that feeds selection. **Fully shipped.** |
| 5 | Pricing details | #4 | Folds into Phase 6.5's 4B extraction. Additive prompt + schema; no new agent call. **Phase 5a shipped (schema + helpers + frontend); 5b — prompt + SSE — lands with Phase 6.5.** |
| 6 | User picks per scope + pause | #1 | Per-scope SelectionPanel (pre-verify UI). Replaces shim. **Fully shipped.** |
| 6.5 | Deep verify, drop-on-reject | (new, replaces old #4.5) | **This is the Agent 4 redesign.** Directed 4A→4B→4C→4D loop per candidate, runs ONLY on selected candidates (top-K + user picks), produces api_spec.txt, drops rejections from the scope's tested set (no substitution). **Shipped.** |
| 7 | Per-scope top-K selection | #2 | Scope-independent ranking over Agent 2's output (pre-verify). Feeds Phase 6 defaults; final K per scope goes directly to Phase 6.5. No ranked pool maintained beyond K. **Shipped.** |
| 8 | API doc understanding (retargeted to Phase 6.5) | #3 | Prompt improvements (OpenAPI hunting, multi-page, GitHub SDK fallback) land in Phase 6.5's 4B extraction. **Shipped.** |
| 9 | Per-scope test execution | #6 | **Big simplification from original chained-harness plan.** No workflow chaining. Bounded linear cost. **Shipped.** |
| 10 | Generalizability benchmark | #8 | Regression gate. Per-scope assertions; multi-scope workflows + SPA providers. **Shipped.** |

**Runtime sequence (what actually happens per pipeline run):**

```
Agent 1 (conversation + DAG blueprint)
  → Phase 4: Agent 2 search + rank (no verify)
  → Phase 7: programmatic top-K per scope (default picks for Phase 6)
  → Phase 6: user overrides picks per scope (or skips via --no-interactive)
  → Phase 6.5: deep verify selected candidates, drop on reject (no substitution)
      └─ extracts api_spec.txt per verified candidate
      └─ extracts pricing (Phase 5 prompt) along the way
  → Phase 9: Agent 5 builds harness (no Phase 1 — reads api_spec) + runs per-scope tests
  → Results: per-scope tables + coverage matrix + rejection summary (per-scope)
```

---

## Global Constraints

- Each phase ships behind a feature flag where possible: `PUZZLEEVAL_ENABLE_FETCH_FALLBACK`, `PUZZLEEVAL_BILLING_ENFORCED`, `PUZZLEEVAL_AGENT1_MODEL` (soft revert), `RESEARCH_DUAL_SEARCH_ENABLED`, `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` (new in Phase 6.5), `PUZZLEEVAL_USER_SELECTION_ENABLED`, `PUZZLEEVAL_SCOPE_TEST_MODE`. Defaults chosen so behavior matches the new phase; flipping False restores prior behavior.
- All schema additions are optional (`= None` or `Field(default_factory=...)`) so saved snapshots in `runs/` keep loading.
- CLI (`python -m puzzleeval.cli ... --agent5`) stays functional through every phase. `--no-interactive` skips the Phase 6 user pause and uses Phase 7's programmatic per-scope top-K, which then feeds Phase 6.5 deep-verify unchanged.
- Shim removal in Phase 6 is irreversible — but `inject_user_candidates()` preserves the priority-promotion mechanism under user control.
- 246+ existing unit tests must remain green at the end of every phase. New tests are additive.
- **`covers_step_ids: frozenset[str]` is a first-class field from Phase 4 onward.** Agent 2 sets initial (all "claimed"); Phase 6.5 verifies authoritatively and can downgrade claimed-but-unsupported scopes. Downstream selection and testing MUST route through it.
- **Deep verify runs on selected candidates only, not on the whole pool.** Phase 6.5's "select-then-verify, drop-on-reject" pattern is what keeps cost bounded AND predictable at per-scope-matrix scale. Phase 4 stays CHEAP (search only, no fetch); expensive work concentrates on candidates we'll actually test. No fallback queue, no backfill budget — fixed K deep-verifies per scope.
- **`api_spec.txt` is the handoff artifact between Phase 6.5 and Phase 9.** Agent 5 skips Phase 1 research entirely — the spec is already on disk when it starts Phase 2 build. If the spec is missing, the candidate was rejected upstream and should not appear in `scope_selections`.
- **Drop-on-reject transparency:** when a user's pick fails deep-verify, the candidate is dropped from that scope's tested set — no silent substitution. The `RejectionSummary` panel explicitly lists per-scope rejections with reason categories. Users never silently get a different tool than what they picked; they either see their pick tested OR they see it explicitly rejected with why.
- **No workflow chaining built.** Per-scope data is what the user decomposes mentally. If future product requirements demand chaining, add a Phase 11 rather than resurrect the old Phase 9 design.

## Critical Files (most touched across the roadmap)

- `PuzzleEval-local/puzzleeval/schemas.py` — every phase except #1 and #10. Gains `covers_step_ids` / `coverage_confidence` (Phase 4); `api_spec_path` / `FailedToVerify` (Phase 6.5); `ScopeTestRun` (Phase 9).
- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — phases #1, #7, #9. Agent 5 gets SIMPLER in Phase 6.5 (Phase 1 research removed); SIMPLER in Phase 9 (no chaining).
- `PuzzleEval-local/puzzleeval/agents/screening.py` — phases #1, #6.5 (**biggest rewrite: directed 4A→4B→4C→4D loop**), #5 (pricing prompt additions), #8 (doc-understanding prompt improvements).
- `PuzzleEval-local/puzzleeval/agents/research.py` — phases #4 (search+rank+claimed coverage), #6 (inject_user_candidates).
- `puzzleeval-api/services/pipeline_runner.py` — phases #2, #3, #4, #6, #6.5, #9. New SSE events in Phase 6.5: `candidate_verified`, `candidate_rejected`, `scope_verified_complete`.
- `puzzleeval-api/services/run_manager.py` — phases #2, #6.
- `src/hooks/usePipelineRun.ts` — phases #3, #4, #6, #6.5, #9.
- `src/components/playground/` — phases #2 (QuotaBadge), #3 (WorkflowDiagram with DAG rendering), #4 (CoverageMatrix, CandidateCard with claimed-badge), #6 (SelectionPanel with per-scope columns), #6.5 (RejectionSummary), #9 (ResultsComparison with per-scope tables).

## Cloud Migration Touchpoints (DEFERRED — not blocking local dev)

**Status: documentation only. No implementation work in this roadmap.** Current target is a single-laptop local-host backend. When we eventually migrate to multi-worker cloud infrastructure, these are the components that need to be externalized. This section exists so we don't forget.

| Component | Local (today) | Cloud target | Why it matters |
|---|---|---|---|
| Per-candidate parallelism (Agent 4.5, Agent 5) | `ThreadPoolExecutor` in the FastAPI process | Celery / Cloud Run Jobs / AWS Lambda per candidate | `_build_single_harness` and Phase 6.5's deep-verify are already isolated; swap executor. No prompt or schema changes. |
| Harness sandbox artifacts | `puzzleeval-api/runs/{trace_id}/harnesses/{slug}/` on local disk | S3 / GCS bucket with path identical to current | `_save_json` and `read_file` tool calls need a provider shim. Prompts and api_spec format unchanged. |
| Venv isolation | Python `.venv/` per candidate sandbox | Docker / Firecracker / managed sandboxes (E2B, Modal) | `_build_sandbox_env` + `_create_venv` are the designed seam. Harness Python source is portable. |
| `RunState` in-memory | Python dict on FastAPI singleton | Redis / Postgres with run_id keys | Phase 2 billing scaffold already anticipated this. `RunState` is a plain dataclass; swap the store. |
| **Global tool cache (Phase 6.5)** | Python dict on `RunState` (per-run scoped) | Redis / DynamoDB with `{run_id}:{candidate_name}` keys, TTL 1h | **Gap**: multi-worker concurrent deep-verifies must share the cache so tool X isn't re-researched by worker A after worker B already cached it. Required for cloud. |
| **SSE event bus** | `asyncio.Queue` in FastAPI process | Redis Pub/Sub / NATS / WebSocket gateway | **Gap**: frontend connection may hit worker A but Phase 6.5 events are emitted from worker B. Need a pub/sub to route events to whichever worker is holding the client SSE stream. Required for cloud. |
| Provider credentials | `provider_registry.json` on local disk | AWS Secrets Manager / HashiCorp Vault / GCP Secret Manager | Already designed as a seam in `provider_registry.py`. `get_credentials()` swaps its backend. |

**What "local-first" means for this roadmap:**

1. Phase 6.5's global tool cache lives on `RunState.tool_cache: dict[str, VerifiedCandidate]`. Per-run scoped; no cross-run leakage. Fine for a single laptop.
2. SSE events use the existing `EventBus` (asyncio queue) inside one process. Works as long as the FastAPI process stays up.
3. Concurrent runs: two users hitting the same laptop-hosted backend work, but they don't share any state. Rate limits against Anthropic API could throttle concurrent runs — acceptable.
4. If the FastAPI process restarts mid-run, the run is lost (no persistence). Acceptable for local dev; cloud target would checkpoint to Redis/Postgres.

**When we migrate to cloud, the work is:**
1. Replace `asyncio.Queue` with Redis Pub/Sub for event bus
2. Move `RunState.tool_cache` and `RunState.event_bus` to external stores
3. Replace `ThreadPoolExecutor` with Celery (or Cloud Run Jobs) for per-candidate work
4. Move sandbox artifacts to S3/GCS
5. Swap provider registry to Secrets Manager
6. Add run checkpointing to Postgres

None of this affects the agent logic, prompts, or schemas. The interfaces stay identical; only the storage and orchestration backends swap.

## Known Limits and Deferred Edge Cases (DOCUMENTED — not blocking)

**Status: documentation only.** These are cases the current roadmap does NOT solve. We've chosen to accept them as "known limits" rather than expand scope. Re-evaluate after Phase 10 bench reveals which are painful in practice.

| Limit | Impact | Mitigation (user-facing workaround) | Revisit trigger |
|---|---|---|---|
| **Non-English research coverage** | Agent 2 and Agent 4.5 do English-only searches; misses providers whose docs are primarily in Chinese/Japanese/German/etc. | User can add candidates manually via Phase 6 if they know the provider name. | Internationalization becomes a product requirement. |
| **Very vague user requests** | Agent 1 emits `workflow=None`; downstream phases fall back to legacy flat-sub-task flow. Not all downstream paths are tested for `workflow=None`. | Agent 1 asks clarifying questions instead of designing a bad DAG. | Unclear requests become a support burden. |
| **Agent 1 design-quality validation** | Validator catches structural bugs (cycles, orphan refs) but NOT semantic bugs ("should be 2 scopes, Agent 1 made 7"). | User can manually edit via Phase 6 selection (remove scopes they don't care about). Future: add a "does this decomposition match user intent" LLM-judge gate. | Bench reveals systematic over/under-decomposition by Agent 1. |
| **Test-quality validation** | No check that Agent 3's synthetic tests are discriminating (a bad tool might pass bad tests). | Agent 3F (file-based) uses real user data, sidesteps this. Synthetic mode trusts Agent 3's prompt. | Agent 3 produces obvious false positives in practice. |
| **Pipeline cost cap / pre-run estimate** | No budget gate before expensive runs. User could accidentally kick off a $50 5-scope workflow. | Phase 2 billing scaffolds plan-level caps but not per-run warnings. | Actually gets triggered in production by a real user. |
| **Concurrent multi-user scaling** | Single-host FastAPI handles one user at a time gracefully, multiple concurrent users OK but no isolation guarantees. | Run the backend per-user if scale demands. | Move off single-laptop hosting. |
| **Very long workflows (>7 scopes)** | Architecture is linear in N but UI / cost / latency grow. 7+ scope DAG is probably >$100/run with K=5 per scope. | Tier-aware caps can lower K for long workflows. | User requests this shape. |
| **Regional / industry-specific providers** | Agent 2 finds mainstream providers; misses regional incumbents (e.g., EU-only OCR, Japan-only accounting). | User-added candidates. | Bench shows systematic miss in a domain. |
| **Cross-run cache reuse** | Same provider researched fresh on each run even if another run just verified it. Wasteful but safe (no stale data). | None. Accept the cost. | Multi-run sessions become common. |
| **Fine-grained cost attribution** | `total_cost_usd` is aggregate per run. No breakdown by which scope / which agent / which candidate cost how much. | `pipeline_summary.json` has per-agent cost; adequate for debugging. | Billing needs per-candidate costs. |
| **Credential lifecycle** | Provider credentials loaded at run start; if a key rotates mid-run, failures not recoverable. | Run cancellation + re-run with refreshed creds. | Mid-run credential rotation becomes a real case. |
| **Duplicate provider names** | Two providers with the same display name collide in the global tool cache (e.g. two vendors both called "DocAI"). | Cache key includes provider URL domain as tiebreaker — covers most cases. Not perfect. | First real collision encountered. |
| **Rate-limit cascade** | 10 parallel Phase 6.5 verifies all hit the Anthropic per-minute token limit simultaneously. Phase 1 per-call backoff helps but fleet-level throttling doesn't exist. | Phase 1 rate-limit retry gives ~15-30 sec reprieve; usually enough. | Actually trips in production. |
| **Drop-on-reject variable scope size** | Scope selected with K=5 candidates may end up testing only 3 or 4 if some reject deep-verify. User sees uneven sample sizes across scopes (scope 1 has 5 tested, scope 2 has 3). | User who needs guaranteed N confident passes over-selects in Phase 6 (pick 7 to get ~5 verified). `RejectionSummary` panel shows exactly which candidates failed and why so the user can choose to add alternatives manually. | User feedback shows the uneven sample sizes confuse decision-making. |

## End-to-End Verification (after all 10 phases)

1. `pytest` — 246+ baseline tests + ~60 new tests added across phases all pass.
2. `pytest -m generalizability` — all 6 domain benchmarks pass via cassette replay with per-scope assertions.
3. CLI: `python -m puzzleeval.cli --text "Take photos of invoices, verify tax IDs in parallel, categorize them, then sync to QuickBooks" --agent5 --no-interactive --pretty` — output JSON shows:
   - `workflow.steps` with DAG topology (fan-out + fan-in via `depends_on`)
   - Each `candidate` has non-empty `covers_step_ids`
   - `validated_candidates[].api_spec_path` populated (Phase 6.5)
   - `pricing_breakdown` on each validated candidate (Phase 5)
   - `scope_selections` map (Phase 7) with top-K per scope
   - `scope_runs` (Phase 9) with per-scope results; no `workflow_harnesses` / `workflow_runs` artifacts
4. UI: `bun run dev` + start `puzzleeval-api`. End-to-end browser flow:
   - Multi-scope workflow description → WorkflowDiagram renders DAG (not just horizontal chain)
   - Candidates appear with coverage badges
   - SelectionPanel shows per-scope columns; user edits picks
   - CoverageMatrix renders candidates × scopes
   - Pipeline resumes → ResultsComparison shows per-scope tables + coverage matrix; NO combination matrix
   - QuotaBadge in header reflects plan + credits
5. Live run with `PUZZLEEVAL_BILLING_ENFORCED=1` and `plan="free"` correctly returns 402 at Agent 4 entry; `plan="paid"` proceeds with credits decremented per Agent 4.5 + Agent 9 work.
