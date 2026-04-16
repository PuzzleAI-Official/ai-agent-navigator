# PuzzleEval — Project Context for AI Assistants

> This file provides complete context for any AI assistant working on this codebase.
> Read this FIRST before making any changes.

## What Is PuzzleEval

An AI agent evaluation platform. Users describe what they need AI to do in plain English. We find relevant AI solutions, test them with synthetic data, and return a verdict on **Performance**, **Speed**, **Price** — nothing else.

Target users: SMBs (small/medium businesses) who are overwhelmed by AI options and don't have the technical ability to evaluate them.

## Current State (as of 2026-04-11)

**Agents 1, 2, 3, 4, and 5 are fully built and tested. Agent 5 builds thin API client harnesses AND executes all test cases, with LLM-judged evaluation** — harnesses send files/data and return raw API responses. A separate LLM judge compares raw responses against Agent 3F ground truth. Agents 7-9 are not yet built. The CLI supports running Agent 1 → Agent 2 via `--agent2`, Agent 1 → Agent 3 via `--agent3`, Agent 1 → Agent 2 → Agent 4 via `--agent4`, and Agent 1 → Agent 2 → Agent 4 → Agent 3 → Agent 5 via `--agent5`. When `--agent5` is used, Agent 2→4 and Agent 3F run in parallel for faster wall-clock time.

### Temporary Testing Shim: Registry Candidate Injection

For Agent 5 testing, a temporary shim injects all `provider_registry.json` providers into Agent 2's results (after Agent 2 runs) so they always appear as candidates for Agent 4/5. Injected candidates have `source="provider_registry_injection"` and `relevance_score=0.99` (guarantees top-N selection by Agent 5).

**TO REMOVE THIS SHIM** (when Agent 2 is reliable enough or moving to production):
1. Delete the `inject_registry_candidates()` function from `puzzleeval/agents/research.py` (the block marked `TEMPORARY TESTING SHIM`)
2. Delete the 2 shim lines + comments at ~line 222 in `puzzleeval/cli.py`
3. Remove `, inject_registry_candidates` from the import on `puzzleeval/cli.py` line 39

The frontend is being built separately by another person using Lovable. There is no backend API yet — agents are tested via CLI.

### Phase 1 Refinement: Cloudflare / Web Fetch Hardening (2026-04-14)

Anthropic's server-side `web_fetch_20250910` tool gets blocked ~5–10% of the time
by Cloudflare WAFs, 429 rate limits, or generic 5xx errors. The user-agent that
the tool sends is fixed by the API — we cannot rotate headers — so the fix is
DETECT and PIVOT, not impersonate.

**`puzzleeval/web_fetch_fallback.py`** classifies the eight `WebFetchToolResultErrorCode`
values into recoverable (`url_not_accessible`, `too_many_requests`, `unavailable`)
and non-recoverable (`invalid_tool_input`, `url_too_long`, etc.). For each
recoverable block in a model response we:

- Track the count for observability.
- Apply a 5-second backoff before the next turn if a 429 was seen.
- (Agent 5 only) Inject a fallback guidance text block into the same user
  message as the tool_results, telling the model to try `web_search 'site:DOMAIN TOPIC'`,
  GitHub SDK repos, alternate docs subdomains, or `web.archive.org` snapshots.

**Where wired in:**
- Agent 4 (`screening.py`): per-candidate count returned as the third tuple element from
  `_verify_single_candidate`, summed in `run_screening_agent`, surfaced as `Agent4Result.web_fetch_blocks`.
- Agent 5 (`implement_test_env.py`): `candidate_web_fetch_blocks` accumulated inside
  `_build_single_harness`, set on every `TestHarness` / `FailedHarness`, summed into
  `Agent5Result.web_fetch_blocks`.
- `pipeline.py`: `AgentRecord.metadata` auto-promotes `web_fetch_blocks` from
  output schemas via `_AUTO_METADATA_FIELDS`. `pipeline_summary.json` now carries
  per-agent and run-level totals when blocks are non-zero.
- Agent 2 (`research.py`): no fetches today, no integration needed.

**Toggle:** `PUZZLEEVAL_ENABLE_FETCH_FALLBACK=0` (default `1`/on) — disables detection
and fallback. `PUZZLEEVAL_FETCH_RATE_LIMIT_BACKOFF=N` (default `5`) — backoff seconds.

**Tests:** `tests/test_web_fetch_fallback.py` (21 cases) — covers detection,
fallback-message construction, backoff stub, summary aggregation, and the
`PipelineRun.save_agent_result` / `finalize` integration.

### Phase 1.5 Refinement: Content-Quality Assessment (2026-04-14)

Phase 1 hardens the **HTTP layer** (403, 429, 5xx). Phase 1.5 hardens the
**content layer**: a fetch can return HTTP 200 and still be useless to the
agent — JavaScript SPA shells (Next.js, React, Vue, Angular), login walls,
soft 404s ("page not found" served as 200), or marketing-only landing pages.
Same recovery path as Phase 1; just a broader trigger surface.

**Design principle:** identify usable pages by **positive signals** —
endpoint patterns, auth examples, code blocks, prose with API keywords. ANY
positive signal → page is usable, regardless of how it was rendered. Only
when zero positive signals fire do we run a secondary classification ("why
is this empty?") to specialize the recovery message. This avoids hardcoding
framework markers (Next.js, React) as the primary detector — they evolve
too fast and miss adjacent failure modes (auth walls, marketing pages)
entirely.

**`puzzleeval/web_fetch_fallback.py`** gains:
- `assess_content_quality(text) -> ContentVerdict` — two-stage classifier.
  Stage 1: scan for endpoint regex (`(GET|POST|...)\s+/...`), auth markers
  (`Authorization:`, `Bearer `, `X-API-Key:`), code calls (`curl`,
  `requests.post`, `fetch(`, `axios.`), or ≥500-char prose body containing
  API keywords (`endpoint`, `authentication`, `parameter`, etc.).
  Stage 2: classify why empty as `script_rendered` / `auth_wall` /
  `not_found_soft` / `unknown_useless`.
- `extract_unusable_pages(response)` — complement to
  `extract_blocked_fetches`. Scans successful `web_fetch_tool_result`
  blocks, runs the classifier, returns dicts of `{url, category,
  tool_use_id}` for any page that comes back unusable.
- `build_fallback_message(blocked, unusable)` — extended to include
  category-specific recovery guidance (SPA pivot to deep URLs / openapi
  search / GitHub SDK; auth-wall pivot to archive.org / GitHub SDK; soft
  404 pivot to sitemap / search; unknown pivot to broader search).
- `count_actionable_problems(blocked, unusable)` — sums recoverable HTTP
  errors + unusable pages into one count. Wired into Agent 4/5's
  `web_fetch_blocks` field.

**Where wired in:**
- Agent 4 (`screening.py`): per-turn detection alongside the existing
  block scan, both folded into `candidate_block_count`.
- Agent 5 (`implement_test_env.py`): per-turn detection alongside the
  existing block scan, unified guidance appended to `tool_results`.
- `Agent4Result.web_fetch_blocks` / `Agent5Result.web_fetch_blocks` /
  `TestHarness.web_fetch_blocks` / `FailedHarness.web_fetch_blocks`
  docstrings updated to reflect the expanded semantics ("HTTP error OR
  content-level failure").

**Toggle:** same `PUZZLEEVAL_ENABLE_FETCH_FALLBACK=0` flag as Phase 1
disables both classifiers in one switch.

**Tests:** `tests/test_web_fetch_fallback.py` extended to 43 cases —
adds positive-signal detection (endpoint / auth / code-call / prose),
secondary classification (4 categories), `extract_unusable_pages`
behavior, combined Phase 1 + 1.5 guidance messages, and a Stripe-style
negative-regression case proving rich code-heavy docs do NOT misclassify
as `script_rendered`.

**Open verification:** end-to-end success rate against real SPA-rendered
providers will land via the Phase 10 generalizability bench, which is
spec'd to include 3-4 SPA-rendered vendors (e.g., DocuClipper) so we can
measure actual recovery-vs-baseline.

### Phase 2 Refinement: Service Tier Scaffold (2026-04-14)

User-facing PuzzleAI plans (Free / Paid / Enterprise) wired through the
backend with a NO-OP DEFAULT — when `PUZZLEEVAL_BILLING_ENFORCED=0`
(today), every gate is observability-only; agents run as before. Flipping
the env var to `1` turns gates into hard `HTTPException(402)` blocks
without any other code change.

**`puzzleeval-api/services/billing.py`** is the single point of policy:
- `PLAN_FEATURE_MATRIX` — `free`/`paid`/`enterprise` × `search`/`testing`/`monitoring`
- `CREDIT_COST_PER_AGENT` — Agents 1-3 free, Agent 4 = 1 credit, Agent 5 = 5 credits
- `PLAN_STARTING_CREDITS` — `free`/`enterprise` = unlimited; `paid` = 100
- `require_agent_access(state, agent)` — single gate called from
  `pipeline_runner.py` before Agent 4 and Agent 5 entries. Raises 402 in
  enforced mode, increments `state.plan_gates_triggered` in no-op mode.
- `quota_snapshot(state)` — serializes plan + credits + features for the
  frontend `Quota` model.

**Where wired in:**
- `puzzleeval-api/services/run_manager.py` — `RunState` extended with
  `plan`, `credits_remaining`, `credits_consumed`, `plan_gates_triggered`.
  `RunManager.create_run(plan="...")` initializes the credit balance
  via `billing.starting_credits_for(plan)`.
- `puzzleeval-api/routes/runs.py` — POST `/runs` accepts optional
  `plan` field; GET `/runs/{id}` returns `Quota` block via
  `billing.quota_snapshot(state)`.
- `puzzleeval-api/services/pipeline_runner.py` — Agent 4 and Agent 5
  entries wrapped with `try/except HTTPException` → emit `agent_blocked`
  + `pipeline_failed` SSE events on 402; otherwise pipeline runs
  normally.
- `puzzleeval-api/routes/monitoring.py` — new stub router with two
  enterprise-gated endpoints (`/monitoring/{run_id}/status`,
  `/monitoring/{run_id}/check`). Returns 402 unless `plan == "enterprise"`,
  regardless of `BILLING_ENFORCED`. Real handlers land in a future phase.
- `src/components/playground/QuotaBadge.tsx` — header badge that polls
  `GET /runs/{id}` every 5s while a run is active. Shows `Free · search only`
  / `Paid · 95 credits` / `Enterprise · unlimited`. Goes red when
  `billing_enforced=true` AND `credits_remaining<=0`.
- `src/types/pipeline.ts` — `Plan` and `Quota` types mirror the backend.
- `src/services/api.ts` — `createRun(..., plan)` plumbs the plan through;
  new `getRunState(runId)` call backs the badge polling.

**Toggle:** `PUZZLEEVAL_BILLING_ENFORCED=1` flips on enforcement. Default
`0` keeps every existing call path (CLI + frontend) running unchanged
while still tracking usage for observability.

**Tests:** `puzzleeval-api/tests/test_billing.py` (19 cases) — covers
plan/feature matrix lookups, per-agent credit cost, RunState
extensions, `require_agent_access` in both no-op and enforced modes,
and `quota_snapshot` serialization. Combined with the 211-case
PuzzleEval suite: 230 tests passing.

**Frontend verification:** `bun run dev` (Vite on :8080); navigate to
`/playground`; QuotaBadge renders in the header showing `FREE search only`
when no run is active. The badge's tooltip reads
`Plan: Free · search only · Advisory` (advisory = enforcement is off).

**Phase fingerprint:** `metadata.credits_consumed`,
`metadata.plan_gates_triggered` (will appear in `pipeline_summary.json`
once `pipeline_runner.py` is migrated to call `pipeline_run.save_agent_result`
— pre-existing gap noted in the Phase 1.5 wiring discussion;
fix folded into Phase 2 since we touched these files anyway). Per-call
billing logs include `{plan, agent, credits_after, credits_consumed_total,
trace_id}` via `logger.info("billing_gate_passed", ...)`.

**Diagnostic flag:** `PUZZLEEVAL_BILLING_ENFORCED=0|1`. When 0:
gates record but never raise. When 1: 402 on first failure, pipeline
emits `agent_blocked` + `pipeline_failed` SSE events, run status
becomes `failed`.

### Phase 3 Refinement: WorkflowBlueprint + Agent 1 as Director (2026-04-14)

Agent 1 was a parser — extract sub-tasks from user text. Phase 3 promotes
it to a **director**: decompose the user's demand into an ordered
`WorkflowBlueprint` with step ordering, data flow, role assignment, and
architecture options (all-in-one vs best-per-step). Downstream phases
branch on this structure — Phase 4's dual search groups candidates by
step role, Phase 6's selection UI renders the step chain, Phase 9's
workflow harness wires `step_N.run() → step_N+1.run()`.

**Model promotion.** `AGENT1_MODEL` is now **Opus 4.7** (was Sonnet 4.6).
Override with `PUZZLEEVAL_AGENT1_MODEL=claude-sonnet-4-6` to revert.
Cost impact: ~$0.10 per evaluation (was ~$0.05) — roughly 1% of the
pipeline total; negligible at scale. Opus's stronger planning reasoning
is what lets Agent 1 reliably emit consistent blueprints.

**Schemas (additive).** `puzzleeval/schemas.py` adds:
- `WorkflowStep` — `id`, `role`, `description`, `capability`, `input_from`,
  `output_format`, `depends_on[]`, `all_in_one_compatible`. The
  `capability` field is a string join key matching `SubTask.capability`.
- `WorkflowBlueprint` — `steps[]`, `architecture_options[]`, `notes`.
- `UserUnderstandingOutput.workflow: WorkflowBlueprint | None` — optional,
  default `None`. Pre-Phase-3 saved artifacts still parse; downstream
  phases branch on `workflow is not None`.

**Prompt extension.** `user_understanding.py`'s `SYSTEM_PROMPT` grows a
"Workflow Blueprint" section that teaches the director role:
- Mirror `sub_tasks` (3 sub-tasks → 3 steps, capability strings match).
- Order by data flow (`input_from="user"` first, then `input_from="step_N"`).
- Don't invent structure (single-capability requests → 1-step blueprint).
- `depends_on` is the DAG source of truth; `steps[]` order is presentation.
- Only emit `workflow=None` when the request is truly unstructurable.

**Validator.** `validators.py`'s `validate_agent1_output` now checks:
- ID uniqueness (errors on duplicates — breaks Phase 9 harness keying)
- `depends_on` references (errors on orphans — would silently skip at runtime)
- `input_from` is either `"user"` or a valid step id
- `capability` cross-refs at least one SubTask (warning, not error — tolerates
  slight wording drift)
- Role non-empty; role length reasonable

**Where wired in:**
- `puzzleeval-api/services/pipeline_runner.py` — emits new SSE event
  `workflow_blueprint` after Agent 1 completes. Payload is the blueprint
  dict or `null`. Frontend handler lives in `usePipelineRun.ts`.
- `src/types/pipeline.ts` — `WorkflowStep` and `WorkflowBlueprint` types
  mirror the Python schemas.
- `src/components/playground/WorkflowDiagram.tsx` — new component. Renders
  a horizontal step chain with arrows, role/description cards, architecture
  badges, and Agent 1's notes in an italic tooltip below. Returns null when
  `blueprint === null` (pre-Phase-3 fallback).
- `src/pages/Playground.tsx` — renders `<WorkflowDiagram blueprint={workflow}>`
  above the candidate list when `stage !== "conversation"`.

**Toggle:** none — schema-only. Phase 3 cannot be disabled because
downstream phases (4, 6, 7, 9) consume the blueprint. If Agent 1 fails to
produce a blueprint (`workflow=None`), downstream branches to legacy
flat-sub-tasks behavior automatically.

**Tests:** `tests/test_agent1.py` adds 6 schema cases (single-step,
multi-step, default architecture_options, JSON round-trip, backward-compat
with/without workflow). `tests/test_validators.py` adds 10 validator cases
(valid blueprint, null blueprint, empty steps, duplicate ids, empty id,
orphan depends_on, orphan input_from, capability drift → warning, empty
role, "user" input_from valid). Combined: 227 PuzzleEval + 19 billing =
**246 tests passing**.

**Frontend verification:** end-to-end drive in preview — drove mock
pipeline through 2 conversation turns → `workflow_blueprint` SSE event
fired → `WorkflowDiagram` rendered showing 2-step workflow ("ocr" →
"accounting_sync") with arrow, architecture badges, and Agent 1 notes.
Screenshot captured. TypeScript + Vite production build both clean.

**Phase fingerprint:** `agent_1_output.json.result.workflow.steps` length
`> 0` on new runs; missing/null on pre-Phase-3 artifacts. Frontend reads
via `workflow_blueprint` SSE payload.

**Diagnostic flag:** schema-only — no flag. Revert with
`PUZZLEEVAL_AGENT1_MODEL=claude-sonnet-4-6` if the new Opus prompt
produces worse blueprints than expected (unlikely, but a safety lever).

### Phase 3 DAG Expansion (2026-04-15)

The initial Phase 3 linear form shipped the schema + validator + SSE event +
frontend chain renderer. The DAG expansion completes Phase 3 by teaching
Agent 1 to author workflows with parallelism, adding cycle/unreachable
validation, and rewriting `WorkflowDiagram` for topological layout.

**Agent 1 prompt — DAG authoring.** `user_understanding.py`'s SYSTEM_PROMPT
grows a new rule block:
- "Parallelism is default, not opt-in" — two steps whose `depends_on` lists
  don't reference each other are implicitly parallel. Only serialize when a
  step literally needs its predecessor's OUTPUT as INPUT.
- "Fan-in merge steps are explicit" — when the user says "combine / merge /
  reconcile, then sync," emit a distinct final step that depends_on every
  parallel branch.
- "Acyclic" — never emit A depends_on B AND B depends_on A.
- Two new worked examples: a fan-out+fan-in DAG (OCR → 3 parallel
  enrichments → merge) and a two-root parallel ingestion (photos + audio
  → summary).

**Schema — `parallel_group: str \| None`.** New optional field on
`WorkflowStep`. Purely a layout hint: steps sharing the same non-null tag
cluster visually in the WorkflowDiagram; `depends_on` remains authoritative
for execution semantics. Omit (leave null) for linear chains. Mirrored in
`src/types/pipeline.ts`.

**Validator — cycle + unreachable checks.**
`validate_agent1_output` now runs DFS-based cycle detection on the
`depends_on` graph once orphan refs are clear. A cycle is a hard error
with the cycle path spelled out in the message (`step_1 -> step_2 -> step_1`).
Unreachable steps (no path from any root) are soft warnings; a multi-step
blueprint with zero roots is flagged with a specific "no root" warning.
Cycle check short-circuits when orphan `depends_on` refs exist so we don't
walk broken edges.

**Frontend — topological layer layout.** `WorkflowDiagram.tsx` was
rewritten from a horizontal chain to a per-layer column layout:
- Layer = longest-path-from-roots. Roots sit at layer 0; each downstream
  step is `1 + max(layer of deps)`.
- Within a layer, steps sharing a non-null `parallel_group` render inside
  a dashed border container tagged with the group name; solo steps render
  without chrome.
- Edges are SVG cubic Beziers from each source node's right-middle to the
  target's left-middle, measured via `useLayoutEffect` + ResizeObserver so
  connection lines track real DOM positions as the panel resizes.
- 1-step blueprint still renders as a single card (same visual density as
  before). Multi-step linear chains render as N columns of 1 (same as the
  old chain view). DAGs get proper fan-out/fan-in visuals.
- Header shows "N steps · DAG" when any layer has >1 node, otherwise
  just "N steps". No external graph library — topological layout is
  hand-rolled (~200 lines). Dagre / elkjs remain optional future polish
  if blueprints ever exceed ~10 nodes in practice.

**Tests:** 4 new schema cases in `test_agent1.py` (`parallel_group`
default, round-trip, fan-out+fan-in shape assertions, two-root parallel
ingestion) + 5 new validator cases in `test_validators.py`
(two-step cycle, three-step cycle, fan-out+fan-in passes,
no-root multi-step → cycle error, parallel_group metadata survives
validation). Combined: 236 PuzzleEval + 19 billing = **255 tests passing**.

**Phase fingerprint:** unchanged — `agent_1_output.json.result.workflow.steps`
still the primary signal. For DAG detection specifically, look for
`parallel_group` populated on ≥1 step OR any layer with >1 sibling
(the frontend's "DAG" badge uses the latter rule).

**Diagnostic flag:** none added — the DAG expansion is prompt + validator
+ frontend only. `PUZZLEEVAL_AGENT1_MODEL=claude-sonnet-4-6` remains the
soft revert lever if the new parallelism rules produce worse blueprints
than expected.

### Phase 4 Refinement: Agent 2 Dual Search + Ranked Candidate Pool (2026-04-15)

Agent 2 was a single-pass surveyor — one search strategy, one flat list of
5-7 candidates. Phase 4 turns it into a DUAL surveyor when Agent 1's
blueprint has N ≥ 2 scopes: one all-in-one horizontal search (Zapier /
n8n / Make / Workato) PLUS one per-scope specialist search per step. Every
candidate now carries a `covers_step_ids: frozenset[str]` claim plus a
`coverage_confidence: dict[str, "claimed" | "verified"]` tag. Dedup by
candidate name merges coverage sets when the same tool surfaces in both
passes. Agent 2 **never verifies** — all confidence values are "claimed";
Phase 6.5's Agent 4 deep-verify later upgrades confirmed scopes to
"verified" or removes them entirely.

**Schemas.** `puzzleeval/schemas.py::Candidate` gains:
- `covers_step_ids: frozenset[str] = Field(default_factory=frozenset)` —
  blueprint step IDs this candidate claims to cover. Arbitrary size: a
  provider may cover 1, 2, or all N scopes and competes independently at
  every scope it claims. No more "multi-step vs specialist" classes —
  everything is just a coverage SET. Empty for legacy flat flow.
- `coverage_confidence: dict[str, str] = Field(default_factory=dict)` —
  per-scope `"claimed"` / `"verified"` tag. Keys align with
  `covers_step_ids`; validator rejects drift between the two fields.

**Prompt + tool config.** `puzzleeval/agents/research.py` gets:
- A rewritten `RESEARCH_SYSTEM_PROMPT` that teaches the dual-search
  strategy: survey + per-scope when N≥2, legacy single-pass otherwise.
  Explicitly states: coverage is NOT a scoring dimension (a 5-scope tool
  doesn't outrank a 1-scope specialist at OCR).
- A rewritten `STRUCTURE_SYSTEM_PROMPT` explaining how to populate
  `covers_step_ids` and `coverage_confidence` per candidate.
- `_build_web_search_tool(blueprint)` — dynamic `max_uses`:
  single-scope/no-blueprint → `SINGLE_SEARCH_MAX_USES` (3); multi-scope →
  `N+1`, capped at `DUAL_SEARCH_MAX_USES_CEILING` (8) so cost stays
  bounded for pathological blueprints.
- `_build_research_message` includes the blueprint as a dedicated section
  so Claude sees every step's id/role/capability before planning searches.
- `_normalize_coverage()` — deterministic post-processing: dedup by
  case-insensitive name merging coverage, drop hallucinated step IDs,
  auto-fill coverage on 1-scope blueprints, clamp any bogus "verified"
  value back to "claimed" (Agent 2 can't verify).

**Validator.** `validate_agent2_output` gains Phase 4 checks when Agent 1
produced a blueprint AND at least one candidate has non-empty coverage:
- Warn when a scope has zero candidates claiming coverage (Phase 6.5 has
  nothing to deep-verify there).
- Warn when a scope has 1-2 candidates (thin pool — target is ≥3).
- Error when `coverage_confidence` keys drift from `covers_step_ids`
  (structural bug in Agent 2 output).
- Error when `coverage_confidence` values are anything other than
  `"claimed"` or `"verified"`.
- Legacy flat flow (every candidate has empty coverage) silently skips
  the Phase 4 block.

**Shim update.** `inject_registry_candidates()` now accepts an optional
`blueprint` argument and stamps injected test providers with claimed
coverage over EVERY blueprint scope. Keeps the "test every provider we
have keys for" intent — injected providers surface in every per-scope
top-K list. Call sites in `puzzleeval/cli.py` (twice) and
`puzzleeval-api/services/pipeline_runner.py::_run_real_agent2` all pass
the blueprint through.

**Pipeline observability.** `puzzleeval/pipeline.py` gains a
`_DERIVED_METADATA_EXTRACTORS` registry — extractors receive the output
model and return `(key, value)` tuples to write into
`AgentRecord.metadata`. The first entry is `_agent2_coverage_metadata`
which writes `phase4_dual_search_active`, `phase4_scopes_covered_count`,
and `phase4_coverage_populated_all` when Agent 2 output has any coverage
populated. `finalize()` promotes these to run-level
`pipeline_summary.json:metadata.*` so ops can grep for
`phase4_dual_search_active=true` to answer "did Phase 4 fire on this run?"
without reading the full agent output.

**Backend SSE.** `pipeline_runner.py::_branch_a_research_and_screening`'s
`candidates_found` event payload now includes `covers_step_ids` (as a
sorted list — frozenset → JSON list) and `coverage_confidence` (dict)
per candidate. A `_coerce_coverage` helper handles the frozenset / list
/ tuple / set cases defensively so the shim's output doesn't break SSE
serialization.

**Frontend.** Three files land:
- `src/types/pipeline.ts` — `PipelineCandidate` gains `covers_step_ids:
  string[]` and `coverage_confidence: Record<string, CoverageConfidence>`.
  `CoverageConfidence = "claimed" | "verified"` is exported for reuse.
- `src/components/playground/CoverageBadge.tsx` — new component.
  Renders "covers K/N scopes" plus per-role chips with amber dots
  (claimed) / emerald dots (verified). Has a `compact` variant for
  the early-discovery row. Aggregate status tints the header chip
  ("claimed, awaiting verify" vs "verified").
- `src/components/playground/CoverageMatrix.tsx` — new component.
  Candidates × scopes table. Rows sorted by coverage count desc →
  relevance desc → name (so all-in-ones rise to the top, specialists
  below). Cells: empty for uncovered, amber ⦿ for claimed, emerald ✓
  for verified. Footer row shows per-scope depth (red if zero, amber if
  <3, emerald if ≥3). Sticky left column, horizontally scrollable on
  narrow panels. Renders only when blueprint has ≥2 scopes AND at least
  one candidate has coverage populated — collapses to nothing otherwise.
- `src/pages/Playground.tsx` mounts `<CoverageMatrix>` above the
  candidate list (below `WorkflowDiagram`) and passes `workflow?.steps`
  into `<CandidateCard>` so the badge can render.
- `src/hooks/usePipelineRun.ts` parses `covers_step_ids` +
  `coverage_confidence` out of the `candidates_found` SSE payload,
  normalizing bogus confidence values to `"claimed"`. Default fields
  added to the two fallback `PipelineCandidate` constructors
  (`harness_started`, `candidate_results_ready`) so the interface
  stays exhaustive.

**Mock data backfill.** `PuzzleEval-local/runs/working_test_6/agent_2_output.json`
— used as mock seed by the FastAPI layer — backfilled with coverage:
Veryfi/Mindee/Taggun/Klippa/DocuClipper claim only `step_1`;
Parseur/Parsio/Nanonets claim both `step_1, step_2` (all-in-one
invoice + accounting pattern). Lets the mock-mode pipeline driver
exercise the CoverageMatrix without a real API key.

**Toggle.** `PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED=0` reverts to
single-pass behavior — `_build_web_search_tool` always returns
`max_uses=3`, the prompt still has blueprint context but Claude does
legacy-style search, and `_normalize_coverage` still runs so downstream
sees empty or single-scope coverage depending on the flat-flow path.

**Tests:** 27 new cases across three test files:
- `tests/test_agent2.py` +18: schema defaults, JSON round-trip, dual
  search tool config (1/2/5/15/disabled scopes), research message
  rendering (multi-scope / no-workflow / 1-scope), coverage
  normalization (dedup, case-insensitive merge, hallucinated IDs
  dropped, 1-scope autofill, legacy empty, verified clamp, higher
  relevance kept on dedup).
- `tests/test_validators.py` +5: uncovered-scope warning, thin-scope
  warning, confidence-key drift error, invalid-confidence-value
  error, legacy-flat-flow skip.
- `tests/test_pipeline.py` +4: coverage metadata auto-promoted,
  absent for legacy flow, finalize() rolls metadata to run-level,
  mixed populated → all-flag false.

Combined: **263 PuzzleEval + 19 billing = 282 tests passing**. `tsc`
clean, Vite production build clean (`index-*.js` 728 kB / 226 kB
gzipped — same shape as before).

**Cost delta.** Single-pass (1-scope / no blueprint / flag off): ~$0.35
per Agent 2 run (3 searches + tokens + structure pass — unchanged from
baseline). Dual search on a 3-scope blueprint: ~$0.40 per run (4
searches + slightly more tokens in the survey pass). 5-scope: ~$0.45
(6 searches). Hard ceiling at 8 searches caps Agent 2 at ~$0.50 even
for very large blueprints. Rounding error vs the ~$6 full-pipeline
total — but worth knowing when reading the cost dashboard.

**Phase fingerprint:** primary signal is
`agent_2_output.json.candidates[].covers_step_ids` (non-empty frozenset
per candidate). Run-level rollup lives in
`pipeline_summary.json:metadata.phase4_dual_search_active` (bool),
`.phase4_scopes_covered_count` (int), and
`.phase4_coverage_populated_all` (bool). `CoverageMatrix` mount in the
playground is the frontend-side signal.

**Diagnostic flag:** `PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED=0`. Set
this and every blueprint size takes the single-pass path. Schema fields
stay populated (empty frozenset + empty dict) so no downstream breakage.

### Phase 5a Refinement: Pricing Data Layer (2026-04-15)

Phase 5 splits into two landings by design: Phase 5a (shipped now) lands
the data contracts + helper functions + frontend scaffold so structured
pricing is a null-safe field everywhere; Phase 5b (lands inside Phase
6.5's 4B extraction) populates that field from real docs. Shipping 5a
ahead of 5b means when 6.5 runs, pricing flows through to the UI
automatically — zero new schema/frontend work at 6.5 for pricing.

**What shipped (Phase 5a):**

*Schemas (`puzzleeval/schemas.py`):*
- `PricingTier` — one tier with `name`, `monthly_cost_usd`, optional
  `included_units` / `unit_name` / `overage_cost_per_unit_usd` / `notes`.
- `PricingBreakdown` — `tiers` (cheapest-first), optional
  `free_tier_monthly_units`, `pay_as_you_go` flag, `billing_granularity`
  (`monthly` / `per_call` / `annual_commit` / `hybrid`),
  `per_scope_unit_cost` (dict keyed by step_id for variable pricing),
  `sources` (URLs), `confidence` (`high`/`medium`/`low`), optional `notes`.
- `Candidate.pricing_breakdown: PricingBreakdown | None = None` — Agent 2
  never fills it. When null, downstream falls back to the legacy
  `pricing_model` / `pricing_details` strings.
- `ScreenedCandidate.pricing_breakdown: PricingBreakdown | None = None` —
  Phase 6.5's 4B extraction populates it alongside endpoints.

*Pure helpers (`puzzleeval/pricing.py`):*
- `estimate_monthly_cost(breakdown, monthly_volume) -> float` — walks
  every tier, computes effective cost at that volume (base + overage for
  overflow tiers, $0 for volumes inside included allowance), returns
  the minimum. Handles capped-no-overage tiers, pure PAYG, flat-rate,
  and freemium.
- `per_scope_unit_cost(breakdown, scope_id) -> float | None` — explicit
  `per_scope_unit_cost[scope_id]` wins; falls back to cheapest tier's
  overage; returns None for flat-rate.
- `monthly_cost_for_scope_map(breakdown, scope_volumes) -> float` —
  per-scope summation for CoverageMatrix "what will this actually cost
  me?" view.
- `cheapest_meaningful_tier` — skips $0-no-overage filler tiers when a
  real paid tier exists.
- `format_tier_short` / `format_breakdown_short` — UI string helpers
  with best-effort singularization (`pages` → `page`, `queries` →
  `query`) so "$0.005/call overage" reads natural.

*Validator (`puzzleeval/validators.py`):*
- New shared helper `_check_pricing_breakdown(candidate_name,
  covers_step_ids, breakdown, errors, warnings)`. Silent when breakdown
  is None (overwhelming default today). When populated:
  - Errors: empty tiers, missing sources, invalid confidence /
    billing_granularity, `per_scope_unit_cost` keys outside
    `covers_step_ids`, negative costs / overages.
  - Warnings: `confidence=low` (emitter struggled — ops review).
- Wired into both `validate_agent2_output` and `validate_agent4_output`
  — covers the rare case where Agent 2 sees pricing early (test
  fixtures; future heuristics) and the normal case where Phase 6.5's 4B
  populates on ScreenedCandidate.

*Frontend (`src/types/pipeline.ts`, `src/lib/pricing.ts`, playground
components):*
- Types `PricingTier`, `PricingBreakdown`, `PricingConfidence` mirror
  Python schemas. `PipelineCandidate.pricing_breakdown?:
  PricingBreakdown | null` added.
- `src/lib/pricing.ts` — TypeScript port of `puzzleeval/pricing.py`.
  Kept literal with the Python so behavior stays in sync. All functions
  `null | undefined`-safe.
- `src/components/playground/PricingBlock.tsx` — new. Header line uses
  `formatBreakdownShort` ("From $X/mo" / PAYG variant), confidence dot
  (emerald / amber / red), expandable tier list with `formatTierShort`,
  per-scope chips from `perScopeUnitCost` (only when pricing varies by
  scope), source links at the bottom.
- `src/components/playground/CandidateCard.tsx` — mounts `<PricingBlock>`
  when `c.pricing_breakdown` is non-null. Invisible until Phase 6.5.
- `src/components/playground/CoverageMatrix.tsx` — cells get a per-scope
  unit-cost overlay (below the coverage dot) when
  `perScopeUnitCost(candidate.pricing_breakdown, step.id)` returns a
  value. Null-safe.
- `src/hooks/usePipelineRun.ts` — `pricing_breakdown: null` defaults in
  every `PipelineCandidate` constructor, null-safe parsing on
  `candidates_verified`, and a new `candidate_verified` event handler
  (forward-compat for Phase 6.5's per-candidate verify event).

*Mock data backfill:*
- `runs/working_test_6/agent_2_output.json` — every candidate now has a
  realistic `pricing_breakdown` (OCR specialists with freemium /
  tiered / low-confidence shapes; all-in-ones with per-scope variable
  pricing). Lets the mock-mode pipeline exercise `<PricingBlock>` and
  the CoverageMatrix cost overlay without a real API key.

**What Phase 6.5 will add (Phase 5b):**
- `screening.py` 4B prompt addition: "also extract pricing alongside
  endpoints" — same fetch budget, same docs pages usually cover both.
- `pipeline_runner.py` emits `candidate_verified` SSE per candidate
  carrying the populated `pricing_breakdown` (the frontend already
  parses this event as of Phase 5a).
- 5 pricing extraction cases in `tests/test_agent4.py`.

**Cost delta:** none for Phase 5a — schema fields + pure helpers, no
LLM calls. Phase 5b's extraction piggybacks on the 4B web_fetch budget
(max_uses=6 — already sized in Phase 6.5 spec to accommodate a separate
/pricing page URL when needed).

**Tests:** 42 new cases:
- `tests/test_pricing.py` (new file) +32: `estimate_monthly_cost` across
  freemium / PAYG / flat-rate / capped-no-overage / mixed, `per_scope_unit_cost`
  precedence, `monthly_cost_for_scope_map`, `cheapest_meaningful_tier`,
  formatters (all fields / flat-rate / PAYG), schema round-trip.
- `tests/test_validators.py` +10: valid pricing passes, empty tiers /
  missing sources / invalid confidence / low confidence warn / invalid
  billing_granularity / per_scope drift / negative cost / negative
  overage / null pricing silent.

Combined: **305 PuzzleEval + 19 billing = 324 tests passing**. `tsc`
clean, Vite production build clean.

**Phase fingerprint:** `agent_2_output.json.candidates[].pricing_breakdown`
or `agent_4_output.json.validated_candidates[].pricing_breakdown` non-null
(lands live with Phase 5b / 6.5). UI fingerprint: `<PricingBlock>` mount
under any candidate card.

**Diagnostic flag:** none added at 5a — the whole data layer is
null-safe. Phase 5b's extraction will reuse
`PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` (Phase 6.5's flag) since pricing
extraction lives inside 4B.

### Phase 6 Refinement: User Candidate Picking + Pipeline Pause (2026-04-15)

After Agent 2 emits its ranked pool, the pipeline pauses and the user
picks which candidates to test at each scope. Replaces the old
`inject_registry_candidates()` testing shim (irreversibly retired) with
explicit user-driven selection via `inject_user_candidates()` +
`apply_scope_picks()`.

**Core mechanism — pipeline pause:**
Backend emits `selection_required` SSE event → frontend renders
`SelectionPanel` → user toggles per-scope keep/remove checkboxes +
optionally adds custom providers → POSTs to `/runs/{id}/select-candidates`
→ backend validates picks, signals `selection_ready` asyncio.Event →
pipeline resumes on filtered candidate list. Cancellation during pause
is supported via `cancel_event`. When `PUZZLEEVAL_USER_SELECTION_ENABLED=0`
the pause is skipped entirely (auto-run, backward-compat).

**Shim retirement (irreversible):**
`inject_registry_candidates()` removed from `research.py`, `cli.py`
(both call sites), and `pipeline_runner.py`. Users who want specific
providers add them via SelectionPanel or CLI interactive prompt.

**New helpers (`research.py`):**
- `inject_user_candidates(agent2_result, user_adds)` — deduped
  case-insensitive merge with `source="user_provided"`,
  `relevance_score=0.99`.
- `apply_scope_picks(agent2_result, scope_picks, user_added)` — filters
  to only picked candidates, reduces each candidate's `covers_step_ids`
  to the scopes it was picked at. `scope_picks=None` is pass-through
  (no filtering — used by `--no-interactive`).

**CLI (`cli.py`):**
- New `_cli_user_selection_pause()` — prints per-scope candidate tables
  to stderr, prompts for each scope (`y/n/indices`), applies picks via
  `apply_scope_picks()`.
- `--no-interactive` skips the pause (auto-run with all Agent 2
  candidates). Controlled by `USER_SELECTION_ENABLED` config flag.

**API:**
- `RunState` gains: `selection_ready: asyncio.Event`,
  `cancel_event: asyncio.Event`, `user_scope_picks`,
  `user_added_candidates`, `selection_required_emitted_at`,
  `user_selection_applied`, and `"awaiting_candidate_selection"` status.
- `POST /api/runs/{id}/select-candidates` — validates scope_ids match
  blueprint, candidate names exist in Agent 2 results or user-added list,
  non-empty picks, no double-submit. Sets `selection_ready.set()`.
- `pipeline_runner.py` — pause inserted between Agent 2 completion and
  Agent 4 start. Uses `asyncio.wait` on `selection_ready` + `cancel_event`.
  On resume, applies picks via `apply_scope_picks` and re-emits
  `candidates_found` with filtered list.

**Frontend:**
- `Stage = "conversation" | "pipeline" | "selection" | "results"` —
  new `"selection"` stage.
- `src/services/api.ts` — new `selectCandidates(runId, request)`;
  `selection_required` + `candidate_verified` + `candidate_rejected`
  added to SSE event types.
- `src/hooks/usePipelineRun.ts` — handles `selection_required` →
  sets stage to "selection"; exposes `perScopeCandidates`,
  `submitSelection()`, `isSelectionSubmitting`, `rejections`.
  Forward-compat `candidate_rejected` handler populates `rejections[]`
  for Phase 6.5.
- `src/components/playground/SelectionPanel.tsx` — NEW. Per-scope
  columns with keep/remove checkboxes, "Add custom provider" form with
  multi-scope checkbox selector, Submit button. Opt-out UX (all start
  selected — user unchecks what they don't want).
- `src/components/playground/RejectionSummary.tsx` — NEW null-safe
  scaffold. Renders per-scope rejection list when Phase 6.5's
  `candidate_rejected` events arrive. Collapsed by default; expandable
  with reason labels + attempt notes. Invisible until 6.5 ships.
- `src/pages/Playground.tsx` — mounts `SelectionPanel` when
  `stage === "selection"`, `RejectionSummary` at top of results.
  Stage indicator bar adds "Selection" between "Research" and "Screening".

**Tests:** 20 new cases:
- `tests/test_research_phase6.py` NEW +9: inject_user_candidates
  (merge/dedup/source/covers) + apply_scope_picks
  (pass-through/filter/reduce/empty/user-added).
- `puzzleeval-api/tests/test_routes_select.py` NEW +11: 404 on
  missing run, 400 on wrong status, double-submit rejected, empty
  picks rejected, unknown scope_id, unknown candidate name, user-added
  with unknown scope, valid picks 200 + ready, same candidate at
  multiple scopes, user-added flows through, empty covers_step_ids
  rejected.

Combined: **314 PuzzleEval + 30 billing/API = 344 tests passing**.
`tsc` clean, Vite build clean.

**Phase fingerprint:** `metadata.user_selection_applied=true`,
`metadata.user_added_candidates_count >= 0`,
`metadata.selection_required_emitted_at` (ISO timestamp).

**Diagnostic flag:** `PUZZLEEVAL_USER_SELECTION_ENABLED=0` — skips
the pause entirely, auto-runs with all Agent 2 candidates (backward-
compat). Default is `1` (pause enabled).

### Phase 6.5 + 7 Refinement: Deep Verify Loop + Per-Scope Selection (2026-04-15)

**Phase 6.5 — Agent 4 directed deep-verify loop (4A→4B→4C→4D).**
Agent 4 is redesigned from a single-shot classifier to a production-grade
directed multi-phase loop. For each selected candidate, the loop runs:
4A (Discovery) → 4B (Spec Extraction + api_spec.txt + ROUTING_TABLE +
pricing) → 4C (Scope Coverage Verification: upgrade claimed→verified,
remove unverifiable scopes) → 4D (Decision with anti-false-positive
AND anti-false-negative guardrails). Drop-on-reject: if a candidate
fails, it's dropped from the scope's tested set — no substitution.
Per-run tool cache: a candidate covering M scopes gets deep-verified
ONCE; subsequent scopes reuse the cached spec.

New files:
- `puzzleeval/deep_verify_prompt.py` — `DEEP_VERIFY_SYSTEM_PROMPT`
  (9.8K chars) teaching the 4-phase workflow + `build_deep_verify_message()`
  per-candidate message builder. Mentions tool budget explicitly
  (6 web_fetch + 5 web_search). Includes OpenAPI/Swagger hunting,
  multi-page doc traversal, third-party doc host checks (Postman,
  ReadMe, SwaggerHub), archive.org fallback, and pricing extraction.

Schema additions (`schemas.py`):
- `FailedToVerify` model: `name`, `provider`, `scope_id`, `reason`
  (docs_unreachable / enterprise_only / deprecated / no_api /
  coverage_removed_at_scope / verify_error), `attempt_notes`.
- `ScreenedCandidate` gains: `api_spec_path: str | None`,
  `covers_step_ids: frozenset[str]`, `coverage_confidence: dict[str, str]`.
- `Agent4Result` gains: `failed_to_verify: list[FailedToVerify]`,
  `scope_selections: dict[str, list[str]]`.

Config (`config.py`):
- `AGENT4_DEEP_VERIFY_ENABLED` (default on)
- `AGENT4_DEEP_VERIFY_MAX_TURNS` (15)
- `AGENT4_DEEP_VERIFY_MAX_PARALLEL` (5)

Pipeline (`pipeline_runner.py`):
- SSE events emitted after Agent 4: `candidate_verified` (per candidate ×
  per verified scope), `candidate_rejected` (per FailedToVerify entry),
  `scope_verified_complete` (per scope with verified + rejected counts).
- Frontend already handles all three events (wired in Phase 6).

**Phase 7 — Per-scope top-K selection.**
Replaces the global `sort by (credentials, relevance)` with independent
per-scope rankings via a 5-dimension weighted scorer.

New file: `puzzleeval/selection.py`
- `select_scope_candidate_pairs()` — for each scope, ranks eligible
  candidates by weighted score and returns top K names.
- `_score_at_scope()` — 5 dimensions: user_picked_here (0.40),
  credentials (0.20), relevance_at_scope (0.20), docs_quality (0.10),
  pricing_fit (0.10). Coverage count is NOT a dimension.
- `_pricing_fit()` — heuristic using Phase 5 PricingBreakdown when
  available, falls back to Agent 2's loose pricing_model string.

Config (`config.py`):
- `SCOPE_SELECTION_WEIGHTS` dict (tunable)
- `SCOPE_CANDIDATES_CAP_BY_PLAN` dict (free=3, paid=5, enterprise=10)

Billing (`billing.py`):
- `PLAN_SCOPE_CANDIDATES_CAP` dict
- `scope_candidates_cap(plan)` function

Pipeline integration (`pipeline_runner.py`):
- Phase 7 runs BETWEEN Agent 2 and Phase 6 pause. Computes programmatic
  default picks that the SelectionPanel shows pre-filled.
- When Phase 6 is disabled (`USER_SELECTION_ENABLED=0`), Phase 7's
  top-K is applied directly via `apply_scope_picks()` — the auto-run
  path for scripts. The `selection_required` SSE event includes
  `default_picks` so the frontend can pre-fill checkboxes.

Tests: 15 new cases in `tests/test_phase6_5_and_7.py`:
- Phase 7 (8): specialist scope exclusion, all-in-one multi-scope,
  multi-scope picks, user override, tier cap, credentials boost,
  coverage count not a dimension, empty coverage excluded.
- Phase 6.5 (7): FailedToVerify round-trip, api_spec_path default,
  coverage fields, Agent4Result with failed_to_verify + scope_selections,
  prompt phase markers, ROUTING_TABLE in prompt, message builder.

Combined: **329 PuzzleEval + 30 API = 359 tests passing**.

Phase fingerprints:
- 6.5: `agent_4_output.json` contains `failed_to_verify[]` +
  `scope_selections{}`; SSE events `candidate_verified` /
  `candidate_rejected` / `scope_verified_complete` fire per-scope.
- 7: `selection_required` SSE payload includes `default_picks`;
  `pipeline_summary.json` metadata: `phase7_scope_picks` present.

Diagnostic flags:
- `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED=0` reverts to shallow Agent 4
  (legacy behavior + Agent 5 resumes Phase 1 research).
- `SCOPE_SELECTION_WEIGHTS` / `SCOPE_CANDIDATES_CAP_BY_PLAN` are config
  dicts in `config.py` — tunable without code changes.

### Phase 8 Refinement: API Doc Understanding (2026-04-15)

Surgical improvement to Phase 6.5's 4B extraction turn-phase. Lifts
api_spec.txt completeness for every verified candidate, which in turn
lifts Agent 5's build success rate in Phase 9.

**Prompt changes (`deep_verify_prompt.py::DEEP_VERIFY_SYSTEM_PROMPT`):**
- **OpenAPI/Swagger hunting section** — new "Step 1: Hunt for OpenAPI"
  block before the narrative-docs extraction. Instructs the model to:
  1. Site-scoped search: `site:{domain} openapi.json OR swagger.json OR openapi.yaml`
  2. Try common convention URLs: `/openapi.json`, `/api/docs/openapi.json`,
     `/v1/openapi.json`, `/swagger.json`, `/.well-known/openapi.yaml`, `/api-docs`
  3. When spec found → parse endpoints directly, skip narrative doc fetches,
     save fetch budget for pricing page + Python examples
- **Endpoint completeness enforcement** — "ENDPOINTS section must list
  ALL endpoints, not just the quickstart example. If you see 5+ endpoints
  in a sidebar but only extracted 1-2, MUST fetch the reference page."
- **Step 2 (narrative fallback)** — only executes when no OpenAPI spec
  resolves. Multi-page traversal rule preserved from Phase 6.5.

**New constants:**
- `COMMON_OPENAPI_PATHS` — tuple of 8 common convention paths, ordered
  by empirical frequency. Used by the prompt AND by `build_deep_verify_message()`
  to generate per-candidate URL hints.
- `THIRD_PARTY_DOC_HOSTS` — tuple of 4 known secondary doc platforms
  (Postman, ReadMe, Stoplight, SwaggerHub). Referenced by the 4D
  anti-false-negative guardrails.

**`build_deep_verify_message()` enhancement:**
- When `claimed_docs_url` is provided, the message now includes an
  "OpenAPI Spec URL Hints (Phase 8)" section with 6 concrete URLs
  derived from the base domain. When no docs URL is known, hints are
  omitted (model does its own discovery).

**Tests:** 9 new cases in `test_phase6_5_and_7.py::TestPhase8DocUnderstanding`:
prompt has OpenAPI hunt section, site-scoped search, common URL probing,
skip-narrative instruction, endpoint completeness enforcement;
`COMMON_OPENAPI_PATHS` has ≥6 entries; `THIRD_PARTY_DOC_HOSTS` has ≥3;
message includes hints when URL provided; hints absent when no URL.

Combined: **338 PuzzleEval + 30 API = 368 tests passing**.

**Phase fingerprint:** verified candidates' `api_spec_path` files contain
`OPENAPI_URL: https://...` (non-"not_found") for most candidates. The
`COMMON_OPENAPI_PATHS` constant can be grepped from the prompt output.

**Diagnostic flag:** prompt-only — no flag. Revert by removing the
Phase 8 Step 1 section from `DEEP_VERIFY_SYSTEM_PROMPT`; the legacy
narrative-extraction path in Step 2 still works.

**Rollback:** pure additive prompt changes. Phase 6.5's directed loop
continues working at prior extraction quality if the Phase 8 sections
are removed.

### Phase 9 Refinement: Per-Scope Test Execution (2026-04-15)

For multi-scope workflows, Agent 5 tests top-K candidates **at each scope
independently**. No end-to-end workflow chaining. No combinatorial
explosion. Per-scope data is the deliverable — "Mindee scores 0.95 at
OCR, Klippa 0.87" is directly actionable.

**Schema (`schemas.py`):**
- `ScopeTestRun` — `scope_id`, `scope_role`, `candidate_results:
  list[CandidateTestRun]` (sorted by aggregate score descending),
  `test_case_count`. One entry per scope that had candidates tested.
- `Agent5Result.scope_runs: list[ScopeTestRun]` — empty for legacy
  (pre-Phase-9) outputs; 1-scope workflows produce 1 ScopeTestRun
  wrapping the same data as `candidate_runs`.

**New module (`puzzleeval/scope_routing.py`):**
- `group_tests_by_scope(test_cases, blueprint)` — routes test cases to
  scopes via `sub_task_ref` ↔ `WorkflowStep.capability` keyword overlap.
  No-blueprint → all tests grouped under "_flat".
- `build_scope_runs(candidate_runs, blueprint)` — post-processes Agent
  5's flat `candidate_runs` into per-scope `ScopeTestRun` entries.
  For each candidate, filters `test_results` to those matching each
  scope, recomputes pass_rate/tests_passed/etc per scope.
- `dedup_tools_for_build(scope_selections)` — unique tool names across
  all scopes (a candidate covering M scopes gets ONE build). Preserves
  first-seen order.

**Config:** `SCOPE_TEST_MODE` flag (env `PUZZLEEVAL_SCOPE_TEST_MODE`,
default on). When off, reverts to legacy flat-candidate testing.

**Frontend:**
- `ScopeTestRun` + `ScopeCandidateResult` types in `pipeline.ts`.
- `ResultsComparison.tsx` rewritten with two paths:
  - Phase 9 (scopeRuns available): per-scope sections with candidate
    tables, pass-rate bars, score/latency/cost columns.
  - Legacy (no scopeRuns): original flat card layout preserved.

**Tests:** 15 new in `test_phase9.py`:
- Schema round-trip, backward-compat default, Agent5Result with scope_runs
- group_tests_by_scope: no-blueprint flat, single-scope, multi-scope routing
- build_scope_runs: single-scope wrap, no-blueprint flat, multi-scope split, empty
- dedup_tools_for_build: across scopes, order preserved, empty, case-insensitive
- Config SCOPE_TEST_MODE enabled by default

Combined: **353 PuzzleEval + 30 API = 383 tests passing**.

**Phase fingerprint:** `agent_5_output.json.scope_runs` length equals
number of scopes; each `ScopeTestRun.candidate_results` non-empty for
scopes with tested candidates.

**Diagnostic flag:** `PUZZLEEVAL_SCOPE_TEST_MODE=0` reverts to legacy
flat-candidate testing.

### Phase 10 Refinement: Generalizability Benchmark (2026-04-15)

10-domain benchmark suite that validates the pipeline works across
multiple AI domains — not just the OCR happy path. Regression gate
for everything in Phases 1-9.

**Domain coverage (10 configs):**

| Domain | Scopes | What it exercises |
|--------|--------|-------------------|
| invoice_workflow | 3 | OCR+extract+sync; SPA provider (Phase 1.5) |
| chatbot_simple | 1 | Single-scope regression baseline |
| classification_pipeline | 2 | Classify+route pattern |
| translation_chain | 2 | Translate+format pattern |
| summarization | 1 | Single-scope summarization |
| multi_step_data_extraction | 4 | Longest DAG (4-step chain) |
| rejection_transparency | 3 | Drop-on-reject with niche providers |
| user_added_candidate | 2 | Phase 6 user-added flow |
| long_chain_parallel | 5 | Fan-out/fan-in DAG topology |
| scope_under_capacity | 2 | Scope with 0 tested candidates |

**Files:**
- `tests/generalizability/domains/*.json` — 10 domain config files with
  `input`, `expected_scopes`, `per_scope_assertions` (min pass rate +
  min candidates per scope), `max_cost_usd` ceiling.
- `tests/generalizability/conftest.py` — fixtures + helpers.
- `tests/generalizability/test_generalizability.py` — 39 tests:
  config loading (10), assertion validation (10), scope-assertion
  alignment (10), plus meta-tests (min domain count, single/multi/fan-out
  domain presence, cost ceiling bounds, assertion helpers).
- `bench/run_benchmark.py` — CLI runner with `--list`, `--domain`,
  `--all`, `--live`, `--report` modes.
- `bench/README.md` — usage docs.
- `pyproject.toml` — `addopts = "-m 'not generalizability'"` so
  standard `pytest` skips bench tests; `pytest -m generalizability`
  runs them explicitly.

**How to run:**
```bash
# Standard tests (bench skipped)
ANTHROPIC_API_KEY=dummy pytest tests/

# Generalizability tests only (config validation — no API calls)
ANTHROPIC_API_KEY=dummy pytest -m generalizability tests/generalizability/ -v

# Bench CLI — dry run all domains
python bench/run_benchmark.py --all

# Bench CLI — live run (requires API key, incurs costs)
python bench/run_benchmark.py --domain invoice_workflow --live
```

**Tests:** 39 generalizability tests (skipped by default) + all existing
tests unchanged. Standard suite: **353 passing, 39 deselected**.
With generalizability: **392 total passing**.

**Phase fingerprint:** `bench/results/{domain}_{timestamp}.json` files.
Presence of `tests/generalizability/domains/*.json` with 10+ configs.

**Diagnostic flag:** marker-gated, separate directory — doesn't touch
the core suite. Remove the `addopts` line from `pyproject.toml` to
include bench tests in the default run.

## Diagnostic Conventions (Phase Fingerprints + Flag Matrix)

We're shipping 10 phases of refinement without per-phase live testing —
the user explicitly chose to do live runs only at the end. To make fault
attribution tractable in that "one big live run" model, every phase from
Phase 1 onward MUST satisfy two diagnostic conventions.

### Convention 1: Phase Fingerprint

Every phase leaves a unique, queryable signal in the run output so an
operator can answer "did Phase N activate on this run?" with `grep`,
not by reading source. Fingerprints live in either:
- `pipeline_summary.json` under `metadata.<key>` (preferred for run-level
  aggregates — auto-promoted from agent results via
  `_AUTO_METADATA_FIELDS` in `puzzleeval/pipeline.py`), or
- the per-agent JSON output (e.g., `agent_2_output.json.candidates_by_step`)
  when the signal is naturally per-agent.

Fingerprint table (filled in as each phase ships):

| Phase | Signal | Where to find it |
|-------|--------|------------------|
| 1 + 1.5 | `web_fetch_blocks` (HTTP errors + content-level failures combined) | `pipeline_summary.json:metadata.web_fetch_blocks`; per-call breakdown in stderr logs as `web_fetch_blocks_by_code` / `web_fetch_unusable_by_category` |
| 2     | `credits_consumed`, `plan_gates_triggered` on `RunState`; per-call logs as `billing_gate_passed` / `billing_gate_triggered`; `Quota.credits_consumed` in `RunStateOut` | `RunState` fields + stderr logs; `pipeline_summary.json` rollup pending the pipeline_runner refactor noted above |
| 3     | `agent_1_output.json.result.workflow.steps[]` length > 0 (missing/null = pre-Phase-3 or unstructurable request); `workflow_blueprint` SSE event fires once after Agent 1 with the full payload | per-agent JSON + live SSE |
| 4     | `agent_2_output.json.candidates[].covers_step_ids` non-empty; `pipeline_summary.json:metadata.phase4_dual_search_active`, `.phase4_scopes_covered_count`, `.phase4_coverage_populated_all` | per-agent JSON + run-level metadata |
| 5     | `agent_2_output.json.candidates[].pricing_breakdown` non-null; `agent_4_output.json.validated_candidates[].pricing_breakdown.confidence` distribution (Phase 5a ships null-safe scaffold; Phase 5b populates inside Phase 6.5's 4B); UI `<PricingBlock>` mount | per-agent JSON + UI |
| 6     | `RunState.user_selection_applied=true`, `selection_required_emitted_at` ISO timestamp, `user_added_candidates` count; `selection_required` SSE event fires once after Agent 2 when pause is enabled | `RunState` fields + SSE stream |
| 6.5   | `agent_4_output.json` contains `failed_to_verify[]` + `scope_selections{}`; SSE events `candidate_verified` / `candidate_rejected` / `scope_verified_complete` per scope | per-agent JSON + SSE stream |
| 7     | `selection_required` SSE payload includes `default_picks`; Phase 7 auto-pick log visible in agent_activity | SSE stream + pipeline logs |
| 8     | `metadata.openapi_specs_found`, `metadata.docs_pages_traversed` | `pipeline_summary.json:metadata.*` (planned) |
| 9     | `agent_5_output.json.workflow_runs` length (0 = single-step legacy path) | per-agent JSON (planned) |
| 10    | bench/results/{timestamp}.json regression diff vs cassette baseline | `bench/` directory (planned) |

When implementing a phase, add its row to this table in the same commit
that adds the signal. If a phase doesn't have a natural fingerprint
(pure schema additions can fall into this trap), invent one — even a
boolean `metadata.phase_N_active=True` is enough.

### Convention 2: Diagnostic Flag Matrix

Every phase ships behind a feature flag so operators can binary-search
by flipping flags off one at a time. Schema-only changes (Phase 3) can't
be flag-disabled because downstream depends on the field existing —
those rows are documented as "schema-only" so the operator knows not
to look for a flag.

Diagnostic flag table (filled in as each phase ships):

| Phase | Env var | Default | Disable behavior |
|-------|---------|---------|-----------------|
| 1 + 1.5 | `PUZZLEEVAL_ENABLE_FETCH_FALLBACK` | `1` | Skip detection; agents see raw web_fetch errors and useless pages with no recovery guidance |
| 1 (sub) | `PUZZLEEVAL_FETCH_RATE_LIMIT_BACKOFF` | `5` (seconds) | Set to `0` to disable backoff sleep on 429 |
| 2     | `PUZZLEEVAL_BILLING_ENFORCED` | `0` | When `0`: track usage but don't block. When `1`: 402 on insufficient credits or feature-not-in-plan |
| 3     | `PUZZLEEVAL_AGENT1_MODEL` (soft) | `claude-opus-4-7` | Revert to `claude-sonnet-4-6` if Opus blueprint quality regresses. Schema itself cannot be disabled — downstream consumes `WorkflowBlueprint`. |
| 4     | `PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED` | `1` | When `0`: Agent 2 reverts to single-pass search (max_uses=3, every candidate gets empty `covers_step_ids` → downstream flat flow) |
| 5     | (none at 5a — null-safe scaffold; 5b reuses `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` since extraction lives inside Phase 6.5's 4B) | — | Disabling Phase 6.5 disables pricing extraction; schema stays null-safe |
| 6     | `PUZZLEEVAL_USER_SELECTION_ENABLED` | `1` | When `0`: pipeline skips pause, auto-runs all Agent 2 candidates through Agent 4/5 (pre-Phase-6 behavior) |
| 6.5   | `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` | `1` | When `0`: Agent 4 reverts to shallow pass/fail; Agent 5 resumes Phase 1 research |
| 7     | `SCOPE_SELECTION_WEIGHTS` + `SCOPE_CANDIDATES_CAP_BY_PLAN` (config dicts) | tunable | Change weights without code changes; adjust per-plan caps |
| 8     | `PUZZLEEVAL_OPENAPI_HUNT_ENABLED` (planned) | `1` | When `0`: Agent 5 uses today's narrative-only research |
| 9     | `PUZZLEEVAL_WORKFLOW_HARNESS_ENABLED` (planned) | `1` | When `0`: per-candidate harness path even with multi-step blueprint |

Standard debugging procedure when something breaks at the end:
1. Check `pipeline_summary.json:metadata` against the fingerprint table —
   which phases activated?
2. Check the per-agent JSON outputs for the per-agent fingerprints
   (workflow blueprint, candidates_by_step, pricing breakdown, etc.).
3. If a fingerprint that should be there is missing → inspect that phase's
   code path; the phase didn't fire when it should have.
4. If all expected fingerprints are present but behavior is wrong → flip
   flags off in reverse phase order (`9 → 8 → 7 ...`) until behavior
   recovers; the last-flipped flag is the culprit.
5. If the behavior is wrong even with all toggleable flags off →
   schema-level change in Phase 3 introduced something downstream
   misinterprets; bisect via git over the schema commits.

This procedure is mechanical for ~80% of failure modes. The remaining
~20% (cross-phase emergent interactions, behavior regressions in agent
reasoning that don't change observable signals) still require AI-assisted
diagnosis from logs + traces. The conventions above don't eliminate that
need — they minimize how often it's the only available tool.

## The 9-Agent Pipeline

See `ARCHITECTURE.md` for the full spec. Summary:

```
User Input → [1. User Understanding] → [2. Research] → [4. Screening]
                                    ↘ [3. Synthetic Tests]    ↓
                                                    [5. Build + Test × N]
                                                    [7. Analyze × N]
                                                    [8. Ranking]
                                                    [9. Report]
```

- Agents 2 and 3 run in PARALLEL (both consume Agent 1's output)
- Agents 5 and 7 each run N instances in parallel (one per candidate)
- Agent 7 instances are ISOLATED (no cross-product context to avoid bias)

## Tech Stack

- **Language:** Python 3.11+
- **LLM:** Claude via Anthropic SDK (`client.messages.parse()` for structured outputs, `client.messages.create()` for server tools like web search)
- **Default model:** `claude-sonnet-4-6` (Sonnet 4.6) for Agents 1-4. Agent 5 uses Sonnet 4.6 for Phase 1 research and Opus 4.7 for Phase 2+ build/debug. Opus is reserved for agents where nuanced judgment matters (Agent 5 build, Agent 7).
- **Schema validation:** Pydantic v2 (also serves as JSON Schema for structured outputs)
- **Logging:** Structured JSON to stderr via Python stdlib logging. Ready for CloudWatch/Datadog with zero migration.
- **Frontend:** React/TypeScript (built separately in Lovable). Communicates via JSON API contracts defined by Pydantic models.
- **Backend API:** Not yet built. Will be FastAPI — thin wrapper around agent functions.

## Project Structure

```
PuzzleEval/
├── ARCHITECTURE.md              # Master reference for all 9 agents (input/output schemas, flow)
├── CLAUDE.md                    # THIS FILE — context for AI assistants
├── pyproject.toml               # Dependencies: anthropic, pydantic, python-docx, pytest
│
├── puzzleeval/
│   ├── __init__.py              # Package marker, version "0.1.0"
│   ├── __main__.py              # python -m puzzleeval entry point
│   ├── config.py                # Env-based config (API key, model, pricing tables, cache multipliers)
│   ├── exceptions.py            # AgentError hierarchy (RateLimit, API, Output, FileParse)
│   ├── logging_setup.py         # Structured JSON logging shared by ALL agents
│   ├── pipeline.py              # PipelineRun — saves intermediate outputs, generates summary
│   ├── validators.py            # Output quality validators per agent (catch silent failures)
│   ├── web_fetch_fallback.py    # Phase 1 hardening: detect Cloudflare/429/blocked fetches, inject fallback guidance for Agent 5
│   ├── schemas.py               # Pydantic models for Agents 1, 2, 3, 4, and 5
│   ├── file_parsers.py          # PDF/image (native Claude), DOCX/CSV/TXT (Python extraction)
│   ├── provider_registry.py     # Centralized API key management for live validation
│   ├── cli.py                   # CLI runner — Agent pipelines with validation + run persistence
│   │
│   └── agents/
│       ├── __init__.py
│       ├── user_understanding.py   # Agent 1 implementation
│       ├── research.py             # Agent 2 implementation (web search + structured output)
│       ├── synthetic_tests.py      # Agent 3 implementation (text-based synthetic test generation)
│       ├── synthetic_tests_file.py # Agent 3F implementation (file-based test generation)
│       ├── screening.py            # Agent 4 implementation (parallel API docs verification)
│       ├── implement_test_env.py   # Agent 5 implementation (autonomous harness builder)
│       ├── AGENT1_SKILL.md         # Prompt tuning reference (for humans, NOT sent to Claude)
│       ├── README_AGENT1.md        # Complete walkthrough of Agent 1 design
│       └── README_AGENT5.md        # Complete walkthrough of Agent 5 design
│
├── runs/                        # Pipeline run outputs (gitignored)
│   └── {trace_id}/             # One directory per run
│       ├── pipeline_summary.json
│       ├── agent_1_input.json
│       ├── agent_1_output.json
│       ├── agent_1_validation.json
│       ├── ...
│       └── harnesses/           # Agent 5 sandbox directories (one per candidate)
│           └── {candidate_slug}/
│               ├── harness.py, requirements.txt, smoke_test.py
│               ├── live_test.py           # When credentials available
│               ├── fetched_docs_*.txt     # Saved API documentation pages
│               ├── conversation_log.json  # Full build conversation log
│               └── .venv/                 # Isolated Python virtual environment
│
└── tests/
    ├── __init__.py
    ├── test_agent1.py           # 11 unit tests (all passing)
    ├── test_agent2.py           # 15 unit tests (all passing)
    ├── test_agent3.py           # 18 unit tests (all passing)
    ├── test_agent3f.py          # 8 unit tests (all passing)
    ├── test_agent4.py           # 23 unit tests (all passing)
    ├── test_agent5.py           # 52 unit tests (all passing)
    ├── test_validators.py       # 33 unit tests (all passing)
    ├── test_pipeline.py         # 10 unit tests (all passing)
    └── fixtures/
        ├── sample_input_clear.json
        └── sample_input_vague.json
```

## Agent 1 — Key Design Decisions

### Architecture Pattern: Stateless Request-Response

The agent is a pure function: `Agent1Input → Agent1Result`. It holds no state between calls. The caller (CLI today, FastAPI API tomorrow, React frontend eventually) manages conversation history and passes it on each call. This is the same pattern used by ChatGPT, Claude.ai, and every major API at scale.

### Structured Outputs

We use `client.messages.parse(output_format=Agent1Result)` which guarantees Claude's response matches our Pydantic schema. No manual JSON parsing anywhere in the codebase.

### Sub-Task Decomposition

Agent 1 breaks user requests into independent capability sub-tasks. Each sub-task gets its own search keywords focused on the CAPABILITY (e.g., "document OCR API"), not the end-to-end workflow (e.g., "invoice QuickBooks automation"). This ensures the Research Agent finds both all-in-one tools AND specialized tools.

`search_strategy` is always `"both"` — we search for all-in-one and modular approaches simultaneously. The user decides which they prefer AFTER seeing results, not before.

### Information Collection: Critical vs Optional

The agent uses a structured `InfoStatus` object to track what's been collected:

**Critical (blocks search without these):**
- `has_concrete_subtasks` — at least 1 testable input→output sub-task
- `has_domain` — business domain/industry

**Optional (improves results, never blocks):**
- `has_budget`, `has_technical_level`, `has_integration_requirements`, `has_workflow_file`

The conversation flow:
1. Missing critical info → ask `critical_questions`
2. Have critical, missing optional → show breakdown + `optional_prompt` inviting user to add more
3. User responds (or says skip) → `is_clear=true`, produce final output
4. Very detailed first request → skip conversation, produce output immediately

### Conversation: Max 4 Turns, Typically 2-3

The CLI runs a conversation loop (max `MAX_TURNS=4`). The agent decides when it has enough info. Typical flow for a vague request is 3 turns: critical questions → breakdown + optional prompt → final output.

### Integration Requirements Are Screening Checks, Not Search Filters

When a user says "must work with QuickBooks," this is recorded but NOT used to filter search results. The Research Agent searches by capability. The Screening Agent (Agent 4) later checks integration compatibility. This prevents missing great tools that don't advertise specific integrations but work via standard APIs.

### Ambiguous References Are Preserved

When users say "my system" or "our platform," Agent 1 records it as-is (e.g., `"user's existing business system (unspecified)"`). Downstream agents see the ambiguity and can handle it.

### File Handling

- **PDFs and images** → sent directly to Claude as native content blocks (base64 encoded). Claude's vision reads them. No external parsing libraries.
- **DOCX** → text extracted via `python-docx`, appended to system prompt
- **CSV** → read via stdlib `csv`, formatted as text table, capped at 100 rows
- **TXT** → read directly

### Prompt Caching: DISABLED for Agent 1

Agent 1 has a human in the loop (5-15 min between turns). The 5-min cache expires before follow-up. The system prompt (~3,500 tokens) is below Opus's 4,096 minimum cacheable threshold anyway. Caching should be ENABLED for Agents 5/7 which make rapid-fire calls with the same context.

Cache infrastructure is fully built (`CACHING_ENABLED` flag, block-level and top-level `cache_control`, cost calculation with cache multipliers in logging). Flip the flag to `True` for future agents.

### Model Choice: Sonnet 4.6 (default)

Agent 1 uses `DEFAULT_MODEL` (`claude-sonnet-4-6`). Parsing/classification task — Sonnet's sweet spot. Override with `PUZZLEEVAL_MODEL=claude-opus-4-7` environment variable.

### Cost Per Evaluation (Agent 1 only, Sonnet 4.6)

- 1-turn (clear request): ~$0.015
- 2-turn: ~$0.033
- 3-turn (vague → optional → final): ~$0.050

### Logging and Observability

Every API call logs to stderr as structured JSON:
```json
{"timestamp": "...", "agent_name": "agent_1_user_understanding", "tokens_in": 4031, "tokens_out": 189, "cost_usd": 0.014928, "latency_ms": 7709, "model": "claude-sonnet-4-5-20250929", "stop_reason": "end_turn", "trace_id": "..."}
```

`trace_id` (UUID) is generated per user request and threaded through all agents for cross-agent log correlation.

Cache metrics (`cache_creation_tokens`, `cache_read_tokens`) are tracked in logs even when caching is disabled — they'll show 0/null but the logging code is ready.

### Cost Tracking in Agent Results

All agents 1-4 now return `cost_usd` in their result schemas. `log_llm_call()` returns cost so callers can accumulate it. Agent 5's cost is tracked per-candidate in `TestHarness.build_cost_usd` and aggregated in `Agent5Result`.

### Error Handling

```
AgentError (base — carries agent_name + trace_id)
├── AgentRateLimitError    → maps to HTTP 429 in future API
├── AgentAPIError          → maps to HTTP 502
├── AgentOutputError       → maps to HTTP 500 (schema mismatch — very rare with structured outputs)
└── AgentFileParseError    → graceful degradation (proceeds without file)
```

## Agent 2 — Key Design Decisions

### Architecture Pattern: Two-Step Stateless Function

Agent 2 is NOT a single API call like Agent 1. It's two sequential calls:

```
Step 1: client.messages.create() + web_search tool
  → Claude searches the web, reads search result content, writes findings as text

Step 2: client.messages.parse() + output_format=Agent2Result
  → Takes the raw text from Step 1, structures it into guaranteed JSON
```

**Why two steps?** Server tools (web_search) require `client.messages.create()` which returns mixed content blocks (text + tool_use + tool_result). Structured outputs require `client.messages.parse()` with `output_format`. These can't be combined cleanly in one call — the tool-use content blocks conflict with structured output format. Step 2 is cheap (~$0.04) since it's just reformatting already-gathered data.

### How Web Search Server Tools Work (Critical Knowledge)

This is the most important thing to understand about Agent 2. Anthropic's web search is a **server tool** — it works differently from regular tool use:

1. You pass `tools=[{"type": "web_search_20250305", "name": "web_search"}]` in the API call
2. Claude decides when to search based on the prompt
3. The API **executes the search server-side** (you don't handle it)
4. Results are injected into the conversation context automatically
5. Claude continues generating, may search again
6. All of this happens inside ONE `client.messages.create()` call

**The token accumulation problem:** Each search adds results (~5-7K tokens of encrypted content per search) to the context. If Claude searches 3 times, the context grows by ~15-20K tokens. This all stays in memory for the duration of that single API call. With web fetch (loading full pages), this explodes to 50-100K+ tokens.

**`pause_turn` stop reason:** When the server-side loop takes too long, the API returns with `stop_reason="pause_turn"` instead of `"end_turn"`. The caller must decide whether to continue (re-send full context as a new call) or stop with partial results. We handle this with `MAX_CONTINUATIONS=1`.

### Search Strategy: "Survey → Score → Rank"

Agent 2 does NOT search for individual tools. It follows a 5-phase process:

1. **Search** — 1-2 searches for comparison/roundup articles ("best [capability] API tools 2026"). These surface 15-30 candidates in one shot.
2. **Collect** — list ALL tools/services mentioned across articles. This is the raw candidate pool.
3. **Score** — evaluate EVERY candidate on 3 dimensions (0-10):
   - **Capability fit**: how well it handles the user's sub-tasks
   - **Adoption fit**: how realistic it is for THIS user to set up (considering their technical level, the service's auth complexity, setup steps, docs quality)
   - **Use case fit**: whether the service is designed for someone like this user in their domain
4. **Weight** — Claude decides dimension weights based on user context. A non-technical construction owner gets Adoption 40% / Use Case 35% / Capability 25%. A senior SWE gets Capability 50% / Use Case 30% / Adoption 20%.
5. **Rank** — composite score = weighted sum → select top 5-7.

**Why scoring, not vibes:** Claude has training data biases (AWS/Google appear in 10x more training docs than Mindee). Without structured scoring, Claude gravitates toward what it knows best, not what fits the user. The scoring framework forces explicit evaluation on dimensions that matter FOR THIS USER.

**Why NOT fetch individual pages:** Web fetch loads full page content (5-25K tokens per page) into context. This was the #1 cause of our token explosion in early testing ($10 per run). Validation of API docs is Agent 4's job, not Agent 2's.

### Candidate Schema: adoption_difficulty

Each candidate carries an `adoption_difficulty` field (easy/medium/hard) derived from the adoption fit dimensional score:
- Adoption 7-10 → `"easy"` (signup → API key → REST calls)
- Adoption 4-6 → `"medium"` (OAuth, SDK config, platform accounts)
- Adoption 1-3 → `"hard"` (cloud accounts, IAM, service provisioning)

This flows through to Agent 4 (ScreenedCandidate), Agent 8 (ranking), and Agent 9 (final report). It's a description, not a filter — the scoring system handles prioritization.

### Web Search Tool Configuration

```python
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",   # Basic version, no dynamic filtering
    "name": "web_search",
    "max_uses": 3,                    # Hard limit: 1 comparison + 1-2 targeted
}
```

**Why basic (20250305) not dynamic filtering (20260209)?** We tested dynamic filtering — it adds ~8 extra server-side code execution iterations to filter search results, costing MORE in overhead than it saves. With only 2-3 searches and no web fetch, there's not enough content to justify filtering. Dynamic filtering pays off when you have heavy content (many searches, web fetch). The code comments document when to switch.

**Why `max_uses=3`?** Each search adds ~5-7K tokens to context. 3 searches = ~15-20K total. This keeps the total API call around 25-30K input tokens. Comparison articles from 1-2 searches already surface 10-15 candidates.

### Model Choice: Sonnet 4.6 for Both Steps

- **Step 1** uses `RESEARCH_MODEL` (Sonnet 4.6, configurable via `PUZZLEEVAL_RESEARCH_MODEL`). Sonnet 4.6 handles web search results better.
- **Step 2** uses `DEFAULT_MODEL` (Sonnet 4.6). Simple formatting task.

### Prompt Caching: DISABLED for Agent 2

Single-shot agent (no conversation loop). No repeated context to cache.

### What Agent 2 Does NOT Do (Agent 4's Job)

Agent 2 **discovers** candidates. It does NOT:
- Fetch API documentation pages (token-expensive, unnecessary for discovery)
- Verify API access or authentication methods
- Confirm pricing accuracy
- Check rate limits or free tier availability
- Validate integration compatibility

All of these are Agent 4 (Screening Agent)'s job. Agent 2 provides the candidate list; Agent 4 validates it.

### Cost Per Research Run (Agent 2 only, Sonnet 4.5/4.6)

- Step 1 (web research): ~$0.25-0.30 (dominated by search result tokens)
- Step 2 (structuring): ~$0.04
- Web searches: ~$0.02-0.03 (2-3 searches × $0.01)
- **Total: ~$0.30-0.40**

The ~70K input tokens in Step 1 are mostly encrypted search result content — this is the baseline cost of using Anthropic web search. Cannot be reduced without reducing search count.

### Cost Per Full Pipeline Run (Agent 1 + Agent 2)

- Agent 1 (3-turn conversation): ~$0.05
- Agent 2 (research + structure): ~$0.35
- **Total: ~$0.40**

### Lessons Learned Building Agent 2 (Read This Before Modifying)

These are hard-won lessons from iterative testing that cost real money:

1. **Web fetch is a token bomb.** Our first implementation used `web_fetch` to load API docs pages. Each page added 5-25K tokens to context, and that content stayed for ALL subsequent iterations. With 8 fetches, context hit 90K+ tokens across 10+ iterations. Cost: $10 per run. **Fix: removed web fetch entirely.**

2. **Dynamic filtering adds overhead for small payloads.** `web_search_20260209` enables Claude to write code that filters search results. But with only 2-3 searches, the code execution iterations cost more than the filtering saves. Tested: basic search = ~$0.30, dynamic filtering = ~$0.38. **Fix: use basic search (20250305).**

3. **`max_uses` is your most important cost control.** The prompt can say "stop after 2 searches" but Claude may ignore it. `max_uses` is a hard API-level cap. Every search added to `max_uses` increases worst-case cost by ~$0.05-0.10 in token accumulation.

4. **`code_execution` tool is auto-injected by 20260209 versions.** If you add `web_search_20260209` or `web_fetch_20260209`, the API auto-injects a `code_execution` tool. If you ALSO pass an explicit `code_execution` tool, you get a 400 error: "conflicting tool names." Don't pass it explicitly.

5. **Search results include substantial content, not just snippets.** The `encrypted_content` field in search results gives Claude real page content to read — enough to identify candidates, their capabilities, and often pricing. You don't need to fetch pages separately.

6. **`pause_turn` must be handled.** When the server-side tool loop takes too long, the API returns with `stop_reason="pause_turn"`. If you don't handle it, you miss the final text output. We allow 1 continuation max (`MAX_CONTINUATIONS=1`).

7. **Agent 2 should discover, not validate.** Early versions tried to validate candidates (fetch API docs, check pricing). This is Agent 4's job. Separating discovery (Agent 2) from validation (Agent 4) keeps Agent 2 fast and cheap.

### Key Files to Read (Agent 2)

To understand Agent 2, read in this order:
1. `puzzleeval/schemas.py` — Agent2Input, Candidate, Agent2Result (at the bottom, after Agent 1 schemas)
2. `puzzleeval/agents/research.py` — the core logic (look for ★ CORE LINE markers, same style as Agent 1)
3. `puzzleeval/config.py` — `RESEARCH_MODEL`, `WEB_SEARCH_PRICE_PER_SEARCH`

### CLI Usage

```bash
# Agent 1 only (unchanged):
python -m puzzleeval.cli --text "I need AI for customer support" --pretty

# Agent 1 → Agent 2 pipeline:
python -m puzzleeval.cli --text "I need invoice OCR" --agent2 --pretty

# Save output + logs:
python -m puzzleeval.cli --text "..." --agent2 --pretty > result.json 2> logs.jsonl
```

When `--agent2` is set, the CLI runs Agent 1's conversation loop first. When Agent 1 finishes (`is_clear=True`), it automatically pipes `UserUnderstandingOutput` into Agent 2. The final JSON output is Agent2Result (not Agent1Result).

## Agent 3 — Key Design Decisions

### Architecture: Two Agents, Same Output

Agent 1 marks each sub-task with `requires_test_files` (true/false). Routing is deterministic:

- **Agent 3** (`synthetic_tests.py`) — handles sub-tasks where `requires_test_files=false`. Generates synthetic text test data. Used for chatbots, classification, text generation, API integrations.
- **Agent 3F** (`synthetic_tests_file.py`) — handles sub-tasks where `requires_test_files=true`. Reads user-uploaded files via Claude vision, generates ground truth. Produces ONE test case per file — no synthetic text generation when files are provided. Simple and predictable.

Both produce `Agent3Result`. For mixed evaluations (some sub-tasks text, some file), both agents run and results merge. When `--agent5` is used, Agent 3F runs in parallel with Agent 2→4 for faster wall-clock time.

### Dynamic Test Case Count (Not Fixed at 20)

The count scales with sub-task complexity:
- **Base: 5-8 per sub-task** (middle target: 7)
- **Workflow bonus: +2 per sub-task** when `workflow_summary` exists
- **Min: 10 total, Max: 50 total**

The target is calculated in `_build_generation_message()` and passed to Claude in the prompt. Claude decides exact allocation within these bounds.

### Coverage Matrix (6 Dimensions)

Each sub-task is tested across 6 dimensions to ensure users never feel undertested:

1. **happy_path** — standard, clean input
2. **input_variation** — different formats/styles
3. **edge_case** — boundary conditions, unusual values
4. **scale** — single vs batch, small vs large
5. **domain_specific** — industry-specific scenarios
6. **error_resilience** — bad, partial, or corrupted input

Each test case is tagged with which dimensions it covers. The prompt requires at least one case per dimension per sub-task.

### Universal Test Format (Works With Any AI Service)

Agent 3 produces test cases in a canonical text format. Key abstractions:

- **`input_type`**: `"text"` | `"structured_data"` | `"document_content"` | `"conversation"` | `"image_description"` — tells the test runner the nature of the input
- **`output_type`**: `"free_text"` | `"structured_json"` | `"classification"` | `"extraction"` | `"action"` — tells the test runner what to expect back
- **`test_file_path`**: path to user-uploaded file (when file-based), or null for synthetic text tests

**Two agents, same output:**
- **Agent 3** (text mode, no files): Generates synthetic text input data. Used for chatbots, classification, text processing.
- **Agent 3F** (file mode, user uploaded files): Reads real files via Claude vision, generates ground truth and criteria. Used for OCR, document processing, image analysis.

Agent 1 sets `requires_test_files` per sub-task to determine which agent to use. Both produce `Agent3Result`.

### Weighted Judgement Criteria

Each test case has 2-5 `JudgementCriterion` objects with:
- **`criterion`**: specific, measurable ("Must extract vendor name correctly")
- **`weight`**: 0.0-1.0, importance (weights sum to ~1.0)
- **`eval_type`**: how to judge — `"exact_match"` | `"semantic_similarity"` | `"contains_key_info"` | `"format_compliance"` | `"subjective_quality"`

This gives Agent 7 precise, reproducible instructions for scoring.

### Cost Per Run (Agent 3 only, Sonnet 4.6)

- Single structured output call: ~$0.04-0.08
- Scales with test case count (more sub-tasks = more output tokens)

### Cost Per Full Pipeline Run (Agent 1 + Agent 2 + Agent 3)

- Agent 1 (3-turn conversation): ~$0.05
- Agent 2 (research + structure): ~$0.35
- Agent 3 (test generation): ~$0.06
- **Total: ~$0.46**

Note: Agents 2 and 3 run in PARALLEL, so wall-clock time is max(Agent 2, Agent 3), not sum.

### Key Files to Read (Agent 3)

1. `puzzleeval/schemas.py` — Agent3Input, TestCase, JudgementCriterion, Agent3Result (bottom of file, after Agent 2 schemas)
2. `puzzleeval/agents/synthetic_tests.py` — text-based synthetic generation (look for ★ CORE LINE markers)
3. `puzzleeval/agents/synthetic_tests_file.py` — file-based generation using user uploads

### CLI Usage

```bash
# Agent 1 → Agent 3 pipeline:
python -m puzzleeval.cli --text "I need AI for customer support" --agent3 --pretty

# Agent 1 → Agent 3 + 3F pipeline (with test files):
python -m puzzleeval.cli --text "I need invoice OCR" --agent3 --test-files invoice1.jpg,invoice2.pdf --pretty

# Save output + logs:
python -m puzzleeval.cli --text "..." --agent3 --pretty > result.json 2> logs.jsonl
```

When `--agent3` is set, the CLI runs Agent 1's conversation loop first. When Agent 1 finishes (`is_clear=True`), it automatically pipes `UserUnderstandingOutput` into Agent 3. The final JSON output is Agent3Result.

## Pipeline Observability

Every CLI run saves intermediate outputs and validation results to `runs/{trace_id}/`. This enables debugging without re-running the pipeline.

### What Gets Saved

```
runs/{trace_id}/
  pipeline_summary.json        # overall status, timing, cost per agent
  agent_1_input.json           # what Agent 1 received
  agent_1_output.json          # what Agent 1 produced
  agent_1_validation.json      # quality check results
  agent_2_input.json           # (if --agent2)
  agent_2_output.json
  agent_2_validation.json
  ...
```

### Output Quality Validators (`puzzleeval/validators.py`)

Pydantic validates structure. Validators check quality — will this output actually work for downstream agents?

- **Agent 1**: has sub-tasks? has domain? search keywords per sub-task?
- **Agent 2**: 4+ candidates? 3+ providers? all api_available=True? covers all sub-tasks?
- **Agent 3**: all sub-tasks covered? weights sum to ~1.0? difficulty spread? file instructions when file format is set? valid enum values?
- **Agent 4**: 2+ validated candidates? count consistency? all enrichment fields populated? valid rejection categories/auth methods/access methods? no silently dropped candidates?

Validators return `{passed, errors, warnings}`. Errors block the pipeline. Warnings are logged but don't block.

### Pipeline Summary (`pipeline_summary.json`)

One JSON report per run with every agent's status, duration, cost, and validation results. Status values:
- `"completed"` — all agents passed, no warnings
- `"completed_with_warnings"` — passed but validators flagged quality issues
- `"failed"` — an agent threw an error
- `"validation_failed"` — an agent's output failed quality checks

### Adding Validators for New Agents

When building Agent 4+, add a `validate_agent{N}_output()` function to `validators.py` and call it from `cli.py` after the agent runs. The pattern is always:
1. Define what "good output" means for downstream consumption
2. Distinguish errors (blocking) from warnings (non-blocking)
3. Cross-reference with upstream agent output when needed

## API Integration Path (Not Yet Built)

All five agent functions are ready for thin FastAPI wrappers:

```python
@app.post("/evaluate/understand", response_model=Agent1Result)
def understand(input_data: Agent1Input):
    return run_user_understanding_agent(input_data)

@app.post("/evaluate/research", response_model=Agent2Result)
def research(input_data: Agent2Input):
    return run_research_agent(input_data)

@app.post("/evaluate/test-cases", response_model=Agent3Result)
def generate_tests(input_data: Agent3Input):
    return run_synthetic_tests_agent(input_data)

@app.post("/evaluate/screen", response_model=Agent4Result)
def screen(input_data: Agent4Input):
    return run_screening_agent(input_data)

@app.post("/evaluate/build-harnesses", response_model=Agent5Result)
def build_harnesses(input_data: Agent5Input):
    return run_implement_test_env_agent(input_data)
```

The frontend sends JSON matching the input schema, gets back JSON matching the result schema. Conversation history is managed by the frontend (React state) and sent on each call. The backend is stateless.

Agent 4 and 5's `ThreadPoolExecutor` parallelism works identically whether called from CLI, FastAPI, or a Lambda handler. At scale, the per-candidate `_build_single_harness()` function can be swapped to distributed workers (one Lambda/Cloud Run per candidate) without changing the function signature — it's already a self-contained function that takes a candidate in and returns a TestHarness or FailedHarness out.

## Key Files to Read

**Agent 1:**
1. `puzzleeval/schemas.py` — Agent1Input, Agent1Result, SubTask, etc. (top of file)
2. `puzzleeval/agents/user_understanding.py` — the core logic (look for ★ CORE LINE markers)
3. `puzzleeval/agents/AGENT1_SKILL.md` — prompt tuning guide and known failure modes

**Agent 2:**
1. `puzzleeval/schemas.py` — Agent2Input, Candidate, Agent2Result (middle of file)
2. `puzzleeval/agents/research.py` — two-step logic (look for ★ CORE LINE markers)
3. `puzzleeval/config.py` — RESEARCH_MODEL, WEB_SEARCH_PRICE_PER_SEARCH

**Agent 3 (text) + Agent 3F (file):**
1. `puzzleeval/schemas.py` — Agent3Input, TestCase, JudgementCriterion, Agent3Result (bottom of file)
2. `puzzleeval/agents/synthetic_tests.py` — text-based synthetic generation (look for ★ CORE LINE markers)
3. `puzzleeval/agents/synthetic_tests_file.py` — file-based generation using user uploads

**Agent 4:**
1. `puzzleeval/schemas.py` — Agent4Input, ScreenedCandidate, RejectedCandidate, Agent4Result (after Agent 3 schemas)
2. `puzzleeval/agents/screening.py` — parallel per-candidate verification (look for ★ CORE LINE markers)
3. `puzzleeval/config.py` — SCREENING_MODEL

**Agent 5:**
1. `puzzleeval/schemas.py` — Agent5Input, TestHarness, FailedHarness, Agent5Result (after Agent 4 schemas)
2. `puzzleeval/agents/implement_test_env.py` — autonomous builder loop (look for ★ CORE LINE markers)
3. `puzzleeval/config.py` — AGENT5_* settings

**Observability:**
1. `puzzleeval/validators.py` — output quality validators for all agents
2. `puzzleeval/pipeline.py` — PipelineRun class (saves outputs, generates summary)

## Agent 4 — Key Design Decisions

### Architecture: N Parallel Verification Calls + 1 Structuring Call

Unlike Agents 1-3 which use 1-2 API calls, Agent 4 makes **N parallel API calls** (one per candidate) using `ThreadPoolExecutor`, then one final structuring call. Each candidate gets its own isolated context — no token accumulation across candidates.

```python
def run_screening_agent(input_data: Agent4Input) -> Agent4Result:
    # Step 1: N parallel per-candidate calls (ThreadPoolExecutor)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_VERIFICATIONS) as executor:
        for candidate in candidates:
            executor.submit(_verify_single_candidate, client, candidate, ...)
    # Step 2: One structuring call → Agent4Result
    client.messages.parse(output_format=Agent4Result, ...)
```

### Why Per-Candidate Isolation (Critical Lesson)

Web_fetch loads entire pages into context (~10-140K tokens per page). If we fetched 7 candidates' API docs in one API call, context would accumulate to 70-980K tokens — the exact token explosion that burned us in Agent 2's early implementation ($10/run).

**Solution:** One API call per candidate. Each call has its own context. Candidate A's 140K-token docs page doesn't inflate Candidate B's context. Cost is predictable and linear.

### Search-First, Fetch-Only-When-Ambiguous (Cost Optimization)

The verification prompt follows the same strategy a human uses to find API docs:

1. **Step 1: SEARCH** (always first — cheap, ~5-7K tokens). Search for `"{name} API documentation"`. If search results clearly show real API docs (endpoints, auth, SDK install in snippets) → PASS immediately. Most well-known services (Google, AWS, Mindee) are verified at this step.

2. **Step 2: FETCH** (only if search was ambiguous). Fetch the specific URL to read actual page content and verify it's real API docs, not marketing.

3. **Step 3: HOMEPAGE** (last resort). Fetch the product's main website (from `source` URL) and look for "Docs"/"Developers"/"API" links.

This cut costs dramatically: Veryfi went from $0.45/candidate (fetching full 140K-token API reference) to $0.15/candidate (search results were enough).

### Tool Configuration (Per-Candidate)

```python
WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 3}
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
```

3 searches (standard + capability-specific + site-scoped) and 3 fetches (docs page + homepage + follow promising link). Basic versions only — dynamic filtering overhead exceeds savings at per-candidate scale.

The verification prompt uses a 4-step strategy: (1) standard search, (2) capability-specific search, (3) site-scoped search, (4) progressive fetch with link-following. An evidence-based PASS rule ensures candidates are only rejected when there is genuinely zero evidence of any API — marketing mentions, pricing tiers with "API access", broken docs URLs, or SDK packages all trigger a mandatory PASS with notes for Agent 5.

### ScreenedCandidate: The Contract With Agent 5

`ScreenedCandidate` is a NEW model (not `Candidate` with extra fields). It guarantees Agent 5 has everything it needs to start building test harnesses without guessing:

- `verified_api_docs_url: str` — **non-optional**, confirmed real docs URL (or best candidate URL if evidence exists but page was temporarily inaccessible)
- `auth_method: str` — api_key, oauth2, bearer_token, basic_auth, no_auth, unknown
- `api_access_method: str` — free_signup, free_tier, trial, sandbox, open, paid_only
- `confirmed_capabilities: list[str]` — verified from actual docs, not Agent 2 claims repeated
- `data_format_notes: str` — what input/output formats the API accepts
- `screening_notes: str` — audit trail of how the pass decision was made

### Parallel Execution

```python
MAX_PARALLEL_VERIFICATIONS = int(os.environ.get("PUZZLEEVAL_MAX_PARALLEL_SCREENING", "7"))
```

Default: all candidates verified simultaneously. Reduce if hitting rate limits on a lower API tier.

Latency: ~30-40 seconds for 7 candidates (parallel) vs ~3+ minutes (sequential).

### Graceful Degradation

If one candidate's verification call fails (rate limit, API error), it's marked as REJECT with a "transient failure" note. Other candidates continue. Only structuring step failures are fatal.

### Accuracy Rules (Anti-False-Positive AND Anti-False-Negative)

The prompt is explicit about both error types:
- Found real API docs with endpoints and auth → **PASS. Do NOT reject.**
- Cannot find docs after all 3 strategies → **REJECT.**
- Uncertain → **PASS with notes.** Agent 5 will verify deeper when building the test harness.

### Validator: Structural Checks Only

The validator checks structural correctness (non-empty fields, valid enum values, count consistency). It does NOT check semantic capability matching — that's the agent's job (Claude understands that "document OCR" and "invoice data extraction" mean the same thing; keyword matching doesn't).

### Model Choice

- Per-candidate verification: `SCREENING_MODEL` (defaults to `RESEARCH_MODEL` = Sonnet 4.6). Handles web content well.
- Structuring: `DEFAULT_MODEL` (Sonnet 4.6). Simple formatting task.

### Prompt Caching: DISABLED for Agent 4

Per-candidate calls have different content each time (different candidate). No repeated context to cache.

### Cost Per Screening Run (Agent 4 only, Sonnet 4.6/4.5)

- 7 parallel verification calls: ~$0.80-1.10 (varies by how many need multiple fetches)
- Structuring call: ~$0.10
- Web searches: ~$0.10 (7 × up to 3 × $0.01)
- **Total: ~$1.00-1.35**

### Cost Per Full Pipeline Run (Agent 1 + Agent 2 + Agent 4)

- Agent 1 (3-turn conversation): ~$0.05
- Agent 2 (research + structure): ~$0.19
- Agent 4 (screening + structure): ~$1.15
- **Total: ~$1.40**

### Lessons Learned Building Agent 4 (Read This Before Modifying)

1. **`web_fetch_20250305` does not exist.** The valid basic version is `web_fetch_20250910`. Valid versions: `web_fetch_20250910`, `web_fetch_20260209`, `web_fetch_20260309`. We wasted a full test run discovering this.

2. **STRUCTURE_MAX_TOKENS must be large enough for the full output.** 7 candidates × 16 fields each × rich text = a lot of JSON. 4096 tokens caused truncated JSON ("EOF while parsing"). Set to 16384.

3. **Search-first saves 3-10x on token costs.** First implementation fetched every candidate's API docs (140K tokens for Veryfi). Search results contain enough content to verify well-known services without fetching the full page. Only fetch for ambiguous cases.

4. **Don't do semantic matching in validators.** Keyword matching produces false warnings ("document OCR" vs "invoice OCR from PDF" share no 15-char prefix). The agent (Claude) already does semantic capability matching when deciding PASS/REJECT. Validators should focus on structural checks.

5. **ThreadPoolExecutor is sufficient for parallelism.** The Anthropic sync client is thread-safe for independent API calls. No need for asyncio complexity. Each thread makes its own HTTP request with its own context.

6. **Evidence of API = mandatory PASS.** Early versions rejected candidates when docs URLs returned errors, even when marketing confirmed a REST API exists. This is wrong — a broken URL doesn't mean no API. The rule: if ANY evidence of an API exists (marketing mentions, pricing tiers, broken docs URLs, SDK packages), PASS with notes. Only reject on genuinely zero evidence. Agent 5 has 15 turns with its own web tools to investigate further.

7. **Site-scoped search catches hidden API pages.** Generic searches like "DocuClipper API documentation" miss API pages nested under feature categories. Searching `site:docuclipper.com API` finds them because Google indexes every page on the domain. This is the third search tier after standard and capability-specific queries.

8. **Following links from the homepage is essential.** Many services don't link their API docs from the homepage navigation. But if you fetch the homepage and see a "OCR API" link under Features, you need to FOLLOW that link (a second fetch) to confirm it leads to real docs. The 3-fetch budget enables: docs page + homepage + follow promising link.

### Key Files to Read (Agent 4)

1. `puzzleeval/schemas.py` — Agent4Input, ScreenedCandidate, RejectedCandidate, Agent4Result (after Agent 3 schemas)
2. `puzzleeval/agents/screening.py` — parallel per-candidate verification (look for ★ CORE LINE markers)
3. `puzzleeval/config.py` — SCREENING_MODEL, PUZZLEEVAL_MAX_PARALLEL_SCREENING

### CLI Usage

```bash
# Agent 1 → Agent 2 → Agent 4 pipeline:
python -m puzzleeval.cli --text "I need invoice OCR" --agent4 --pretty

# --agent4 implies --agent2 (Agent 4 requires Agent 2's candidates)

# Save output + logs:
python -m puzzleeval.cli --text "..." --agent4 --pretty > result.json 2> logs.jsonl
```

When `--agent4` is set, the CLI runs Agent 1's conversation loop, then Agent 2 (research), then Agent 4 (screening). The final JSON output is Agent4Result.

## Pricing Reference (Anthropic, as of 2026-04)

| Model | Input | Output | 5m Cache Write | 1h Cache Write | Cache Read |
|---|---|---|---|---|---|
| Opus 4.7 | $5/MTok | $25/MTok | $6.25/MTok | $10/MTok | $0.50/MTok |
| Sonnet 4.6 | $3/MTok | $15/MTok | $3.75/MTok | $6/MTok | $0.30/MTok |
| Sonnet 4.5 | $3/MTok | $15/MTok | $3.75/MTok | $6/MTok | $0.30/MTok |
| Haiku 4.5 | $1/MTok | $5/MTok | $1.25/MTok | $2/MTok | $0.10/MTok |

Minimum cacheable tokens: Opus 4.7 = 4,096; Sonnet 4.6 = 1,024; Sonnet 4.5 = 1,024; Haiku 4.5 = 4,096.

## Agent 5 — Key Design Decisions (Updated 2026-04-11)

> **Status:** 4/4 builds, 3/4 pass evaluation (working_test_6: Veryfi 89%, Mindee 99%, Nanonets 90%). Cost: ~$4.40/run for 4 candidates, 289 seconds, 7-9 turns per candidate.
> Harness is a THIN API CLIENT — sends file, returns raw response. No parsing, no formatting.
> LLM judge evaluates raw API response against Agent 3F ground truth.

### Architecture: N Parallel Autonomous Builders + Post-Loop LLM-Judged Evaluation

Agent 5 builds a thin API client harness for each candidate, validates it with live API calls during the build, then runs ALL test cases post-loop with LLM-judged evaluation. Each candidate runs in parallel via ThreadPoolExecutor.

**Harness objective: THIN API CLIENT.** The harness sends files/data to the API and returns the raw response. No parsing, no formatting, no field extraction. Evaluation is done by a separate LLM judge that compares the raw API response against Agent 3F ground truth.

```python
def _build_single_harness(client, candidate, input_data, sandbox_dir, logger):
    _stage_test_files(sandbox_dir, test_cases)  # Stage BEFORE build loop
    _create_venv(sandbox_dir, ...)
    credentials = _resolve_credentials(...)

    while turn < MAX_TURNS:
        # Phase 1: Sonnet uses web_search/web_fetch for API docs, writes api_spec.txt
        # Phase 2: Opus builds harness.py (thin API client), runs smoke test
        # Phase 3: Opus runs live API validation with real files (credentials injected)
        current_model = OPUS if api_spec_written else SONNET
        response = client.beta.messages.create(
            model=current_model,
            tools=[web_fetch, web_search, advisor, write_file, run_code, read_file, ask_research],
            thinking={"type": "adaptive"},
        )
        if "HARNESS_COMPLETE" in response:
            break

# After all builds complete:
for harness in successful_harnesses:
    results = _execute_all_tests(harness, test_cases, credentials)  # Mechanical execution
    evaluate(results, judgement_criteria)  # ALL criteria → LLM judge (no mechanical eval)
```

### The 4-Phase System Prompt

1. **PHASE 1: RESEARCH** — Use server-side web_search and web_fetch to find API docs. Follow search→navigate→fetch→synthesize pattern. Write api_spec.txt with INPUT_COMPATIBILITY, ROUTING_TABLE, PYTHON_EXAMPLES, DOC_MAP, DOC_REFERENCES, API_LIMITATIONS. The agent knows ALL test case input forms upfront and researches whether each is compatible. Note: ask_research is for Phase 2+ debugging only, NOT for initial research.
2. **PHASE 2: BUILD** — Write harness.py as a THIN API CLIENT. Sends files/data, returns raw API response. No parsing, no formatting, no field extraction. Incompatible forms return `success=False, error="INCOMPATIBLE: reason"`. Smoke test verifies structure.
3. **PHASE 3: VALIDATE** — Run live API calls with real test files (credentials injected via `_dispatch_tool`). Requires real API success (`success=True`, `output_len > 100`) for EACH file type before HARNESS_COMPLETE. Smoke test alone is NOT sufficient. Milestone message injected when smoke test passes to signal Phase 3 transition.
4. **PHASE 4: COMPLETION CHECKLIST** — Verify all compatible forms work with live API, all incompatible forms return clean errors, signal HARNESS_COMPLETE.

### Post-Loop Test Execution and LLM-Judged Evaluation

After all harnesses are built, Python infrastructure runs ALL Agent 3 test cases through each harness using `_execute_all_tests()` and `_compute_aggregate_metrics()`. **ALL evaluation criteria go to a single LLM judge** — no mechanical eval (no exact_match, no format_compliance). The LLM judge receives the raw API response (truncated at 15K chars), the expected output from Agent 3F ground truth, and the judgement criteria. Uses `response.parsed_output` (not `.parsed`) for structured evaluation results. No adaptive thinking on eval calls. Results are stored in `Agent5Result.candidate_runs`.

### The Verification Gate

When Claude signals HARNESS_COMPLETE, `_run_verification_checks()` confirms harness.py exists. The agent already validated with real test data in Phase 3 — no additional programmatic checks needed. No cosmetic code review (caused over-correction in earlier iterations).
### Model Strategy: Sonnet for Research, Opus for Build

- **Phase 1 (research):** Sonnet 4.6 — uses server-side web_search and web_fetch (NOT ask_research) to find API docs. Follows search→navigate→fetch→synthesize pattern. I/O-heavy, doesn't need Opus reasoning. Cost: ~$0.10-0.30/candidate.
- **Phase 2+ (build/verify):** Opus 4.7 — planning, coding, debugging need strong reasoning. Transition detected when api_spec.txt is written.
- **Opus Advisor:** Available in all phases. Sonnet/Opus can call `advisor()` for strategic guidance. Typically called once per candidate before writing code. Uses `advisor-tool-2026-03-01` beta.
- **ask_research:** Sonnet 4.6 — targeted web search for Phase 2+ debugging ONLY, not for initial research.

### Eight Tools Available to the Builder Agent

| Tool | Type | Purpose |
|------|------|---------|
| `web_fetch` | Server (Anthropic API) | Read API docs (max_content_tokens: 15000) |
| `web_search` | Server (Anthropic API) | Search for SDK docs, examples, tutorials |
| `advisor` | Server (Anthropic API) | Consult Opus 4.7 for strategic guidance |
| `write_file` | Custom (local dispatch) | Write NEW files (harness.py, requirements.txt, smoke_test.py) |
| `patch_file` | Custom (local dispatch) | String-replace editing on EXISTING files |
| `run_code` | Custom (local dispatch) | Run shell commands (120s timeout) |
| `read_file` | Custom (local dispatch) | Read files from sandbox |
| `ask_research` | Custom (spawns sub-agent) | Targeted web research for debugging |

### Context Engineering

- **`max_content_tokens: 15000`** on web_fetch — prevents context explosion. All real API docs fit in 15K tokens; the extra is HTML noise.
- **Server-side context management:** `clear_tool_uses_20250919` at 80K tokens + `compact_20260112` at 150K. No manual context resets.
- **Automatic prompt caching:** `cache_control={"type": "ephemeral"}` at request level. Caches growing conversation prefix. 83-86% cache hit rate in production runs.
- **Accurate cost tracking:** Uses `response.usage.iterations[]` array to track executor vs advisor costs separately.
- **Credentials injected during build:** `_dispatch_tool()` passes credentials to `run_code` so the builder agent can do live API validation during Phase 3. Previously credentials were only available in post-loop execution.
- **Test files staged before build:** `_stage_test_files()` runs BEFORE the builder loop (not just during post-loop). Absolute file paths shown in the initial message so the agent can find and use real test files during Phase 3 validation.

### Configuration Settings

- `AGENT5_BUILDER_MODEL = Opus 4.7` — strong reasoning for coding/debugging
- `RESEARCH_MODEL = Sonnet 4.6` — for Phase 1 research (web_search/web_fetch) + ask_research in Phase 2+
- `AGENT5_MAX_TURNS = 25` — typical successful build: 7-9 turns
- `MAX_TURNS_AFTER_SMOKE = 15` — turns allowed for live API validation after smoke test
- `AGENT5_MAX_PARALLEL = 5` — max concurrent candidate builds
- `AGENT5_CODE_TIMEOUT = 120s` — timeout for run_code (increased from 30s for async APIs that need polling)
- `AGENT5_MAX_OUTPUT_TOKENS = 8192` — code generation needs more than default 4096
- `AGENT5_MAX_CANDIDATES = 4` — top N by user-fit score
- `MAX_BUILD_TIME_SECONDS = 480` — 8-minute wall-clock per candidate

### Behavioral Instructions (Claude Code-Inspired)

The system prompt uses behavioral tags that shape how the model works:
- `<use_parallel_tool_calls>` — batch independent tool calls in one turn
- `<do_not_narrate>` — act, don't explain each step
- `<do_not_re_read>` — don't re-read unchanged files
- `<investigate_comprehensively>` — one script that gets all info, not five separate ones
- `<think_before_acting>` — verify unknowns before writing code
- `<verify_against_docs>` — read code back and compare to api_spec
- `<reason_about_errors>` — reason about root cause, don't follow recipes
- `<be_resourceful>` — create local files when URLs fail
- `<commit_and_course_correct>` — commit to approach, course-correct on failure

### api_spec.txt Format

Phase 1 research produces a structured spec with these sections:
- `SERVICE`, `BASE_URL`, `ENDPOINTS`, `AUTH_HEADER`, `REQUEST_FORMAT`, `RESPONSE_FORMAT`
- `SDK_PACKAGE`, `ACCEPTED_INPUT_FORMATS`, `SAMPLE_TEST_URL`
- `PYTHON_EXAMPLES` — code snippets from docs (multiple, for builder to copy)
- `DOC_REFERENCES` — bookmark URLs for debugging (grows during build)
- `DOC_MAP` — all doc pages discovered (even unfetched ones)
- `INPUT_COMPATIBILITY` — YES/NO per input type (file, URL, text, base64)
- `API_LIMITATIONS` — what the API cannot do
- `ROUTING_TABLE` — input scenario to endpoint mapping

The 5-min TTL works because turns within a single candidate's build happen rapidly (seconds apart). Different candidates (running in parallel threads) each get their own cache entry.

### Rate Limit Retry with Exponential Backoff

Parallel builds across candidates can hit the per-minute token limit. Instead of immediately returning `FailedHarness`, Agent 5 retries with exponential backoff:

- Retry 1: wait 15 seconds
- Retry 2: wait 30 seconds
- Retry 3: wait 60 seconds
- After 3 retries: return `FailedHarness` with `build_timeout` category

This is implemented per-candidate inside `_build_single_harness()`. Other candidates continue building while one waits.

### Conversation Log Per Candidate

Every candidate's build saves `conversation_log.json` to its sandbox directory. This records every turn: Claude's text, tool calls made, tool results received, verification gate outcomes, context resets, and cost per turn. Invaluable for debugging build failures without re-running the pipeline.

### Dead-End Detection

Tracks consecutive error turns via a `consecutive_errors` counter. When tool results contain error signals (error, traceback, 401, 404, etc.) for 3 consecutive turns, the loop injects a STRATEGIC REASSESSMENT message forcing Claude to either pivot to a different approach or signal HARNESS_FAILED. This prevents 7-turn debugging spirals on unsolvable problems (wrong SDK version, deprecated endpoint, requires manual account setup). The counter resets after the reassessment.

### Phase 1 Research (Server-Side Web Tools)

Phase 1 research uses server-side web_search and web_fetch directly (NOT the ask_research sub-agent). The agent follows a search→navigate→fetch→synthesize pattern: search for API docs, navigate promising results, fetch documentation pages, and synthesize findings into api_spec.txt.

**Why server-side tools, not ask_research:** ask_research spawns a separate sub-agent with its own context, which loses the accumulated knowledge from previous searches. For initial research, the builder agent needs to iteratively search, read results, and search again based on what it finds. The server-side tools keep all this in one context. ask_research is reserved for Phase 2+ debugging when the builder hits an error it can't solve from saved docs.

If Phase 1 research fails (docs behind auth), the builder can still fall back to ask_research for targeted questions.

### Patch File Tool (Efficient Bug Fixing)

The builder agent has a `patch_file` tool for string-replace editing (same pattern as Claude Code's FileEditTool). When fixing a bug, the agent sends only the diff (~50 tokens) instead of rewriting the entire file (~2K tokens). This saves ~6-10K tokens per candidate across 3-5 fix iterations.

### Mid-Loop Targeted Research (ask_research Tool)

When the builder hits an error it can't solve from saved docs, it can invoke `ask_research` with a specific question. This spawns a fresh research sub-agent (separate context, 2 searches + 2 fetches) that searches the web and returns findings as a tool result. The answer is also saved to `research_turnN.txt` for future reference.

**Reference chain (cheapest first):** PLAN notes → read_file(saved docs) → ask_research → web_search → web_fetch

**Why:** The upfront research sub-agent can't anticipate every question the builder will have (e.g., "What's the pagination format?", "How do I handle async job polling?"). Mid-loop research provides answers without consuming the builder's own web tool budget. Cost: ~$0.10-0.15 per invocation.

### In-Loop Context Management (Auto-Compact)

Inspired by Claude Code's 3-tier auto-compact system. Before each API call, estimates total message characters. When approaching the context limit (`CONTEXT_CHARS_LIMIT = 600K chars ≈ 150K tokens`), compacts older messages — keeping only the initial message + last 4 message pairs. This prevents context overflow during long builds with multiple web fetches.

### Prompt-Too-Long (PTL) Recovery

When the API returns a "prompt too long" error (BadRequestError), the loop:
1. Compacts the conversation context
2. Reduces `max_tokens` by half (minimum 4096)
3. Retries the API call

This is the same pattern Claude Code uses for `max_tokens` overflow recovery. Without this, a PTL error would crash the entire harness build.

### Large Output Persistence

Tool outputs exceeding 5K chars are saved to disk (`output_turnN.txt`) and the model receives a truncated preview (first 1K + last 2K chars) with a file path reference. The model can use `read_file()` to access the full output on demand.

**Why:** Errors are usually at the END of long outputs (pip install, test runs). Simple truncation at 5K chars cuts off the error message. The head+tail preview ensures Claude sees both the beginning (context) and end (error) of large outputs.

### Wall-Clock Timeout

Each candidate build has an 8-minute wall-clock limit (`MAX_BUILD_TIME_SECONDS = 480`). Prevents runaway builds from blocking the pipeline. The budget cap ($3) is the primary limit; the wall-clock timeout catches edge cases where the agent loops on cheap operations.

### OS Detection

The system prompt dynamically includes `OS: Windows` or `OS: Linux` based on `sys.platform`. This tells the builder agent to use cross-platform commands (`python -c "import os; print(os.listdir('.'))"` instead of `ls`) and `os.path` instead of hardcoded path separators.

### Candidate Selection (Top N by User-Fit Score)

Not all validated candidates get harnesses built. `run_implement_test_env_agent()` sorts candidates by `relevance_score` (the user-fit composite score from Agent 2) and takes the top `AGENT5_MAX_CANDIDATES` (default 4). This avoids building 6 harnesses at $0.50 each when we only need 3 for comparison. The 4th is buffer for build failures.

### Incomplete Harness Detection

If the builder loop exits without the smoke test ever passing AND the verification gate never ran, the result is a `FailedHarness` (not a broken `TestHarness`). This prevents silently passing incomplete harnesses to downstream agents.

### Sandbox File Artifacts

Each candidate's sandbox directory contains:
- `harness.py` — the built harness code
- `requirements.txt` — pip dependencies
- `smoke_test.py` — structural validation test
- `live_test.py` — live API validation (if credentials available)
- `fetched_docs_0.txt`, `fetched_docs_1.txt`, ... — saved API documentation pages
- `conversation_log.json` — full build conversation log
- `.venv/` — isolated Python virtual environment

### Tool Configuration

```python
WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 5}
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}
```

Increased from 3/2 to 5/4 after analyzing run failures. Complex APIs (docs behind auth, multi-page docs) need more fetches. Cost increase (~$0.10-0.15/candidate) is negligible vs cost of a failed build ($0).

### Cost Per Build Run (Agent 5 only, Sonnet 4.6 + Opus 4.7 with adaptive thinking)

- Research + build per candidate (7-9 turns): ~$0.80-1.30
- Targeted research calls (0-1 per candidate): ~$0.05-0.15
- Post-loop LLM evaluation: ~$0.10-0.20
- **Per candidate total: ~$0.95-1.65**
- **4 candidates total: ~$4.00-5.00**

Benchmark (working_test_6): 4/4 builds, $4.40 total, 289 seconds, 7-9 turns per candidate.

### Cost Per Full Pipeline Run (Agent 1 + Agent 2 + Agent 3 + Agent 4 + Agent 5)

- Agent 1 (3-turn conversation): ~$0.05
- Agent 2 (research + structure): ~$0.35
- Agent 3 (test generation): ~$0.06
- Agent 4 (screening + structure, with max_content_tokens=15000): ~$0.80
- Agent 5 (research + harness building + LLM eval, 4 candidates): ~$4.50
- **Total: ~$5.50-6.50**

Note: With `--agent5`, Agent 2→4 and Agent 3F run in parallel, so wall-clock time is faster than sequential.

### Graceful Degradation

If a harness can't be built:
- Builder agent signals "HARNESS_FAILED" → `FailedHarness` with categorized reason
- Rate limit error → `FailedHarness`, other candidates continue
- Budget exceeded → `FailedHarness` with `build_timeout`
- Unexpected exception → caught by ThreadPoolExecutor, `FailedHarness` with `unknown`
- Zero harnesses → validator blocks pipeline (no test execution possible)

### Cloud Scaling Path

1. **Stage 1 (now):** Local CLI. ThreadPoolExecutor, venv-isolated sandbox dirs under `runs/{trace_id}/harnesses/`
2. **Stage 2:** Docker containers for code execution. Swap `_create_venv()` with container creation — `_build_sandbox_env()` is the designed seam. Agent loop stays on server (stateless API calls), `run_code` dispatches to Docker per candidate.
3. **Stage 3:** Cloud Run / Serverless. Each `_build_single_harness()` as a Cloud Run job. Harness artifacts to GCS/S3. Provider registry moves to Secrets Manager.
4. **Stage 4:** Managed sandboxes (E2B, Modal, Firecracker). Sub-second spin-up, pre-built images, network isolation.

### Lessons Learned Building Agent 5 (Read This Before Modifying)

1. **Custom tools and server tools mix cleanly.** The API handles server tools (web_fetch, web_search) internally. Custom tools (write_file, run_code, read_file) return with `stop_reason="tool_use"` for local dispatch. Both can coexist in the same `tools` list.

2. **The 4-phase system prompt makes the agent loop predictable.** Claude follows Research → Build → Verify → Checklist. Without explicit phases, Claude would sometimes skip verification or write code from memory instead of fetched docs.

3. **Structural validation catches most issues without API keys.** Mocking the HTTP layer lets the smoke test verify imports, function signatures, return types, and error handling — all without needing the candidate's API key.

4. **The verification gate catches bugs the smoke test misses.** Smoke tests validate structure (does `run()` exist? does it return the right keys?). The verification gate validates correctness (is the endpoint URL real? does the auth header match the docs?). Moving verification inside the loop (not post-loop) lets Claude fix issues while it still has context.

5. **`max_tokens=8192` is necessary for code generation.** The default 4096 truncates harness code mid-function. Full harness.py + requirements.txt + smoke_test.py needs ~2-3K tokens of output.

6. **`_candidate_slug()` must handle edge cases.** Candidate names can contain parentheses, trademark symbols, and unicode. The slug function strips everything non-alphanumeric and truncates to 40 chars.

7. **Live test injection before the loop is better than post-loop live validation.** Early design ran live validation only after the loop. The current design injects `live_test.py` before the loop starts, so Claude discovers and runs it during Phase 3. If the live test fails, Claude fixes the harness while it still has the API docs in context.

8. **Context compression is the single biggest cost lever.** Web_fetch content accumulates in context across turns. Extracting it to files and resetting the conversation after the research phase cut per-candidate cost by ~50%. The key insight: Claude only needs the full docs during research. During build/verify, it can `read_file()` specific sections on demand.

9. **"PLAN:" detection is a reliable phase boundary.** Claude reliably writes "PLAN:" before transitioning from research to build (the system prompt requires it). This makes it a safe trigger for the context reset. If PLAN is never written (rare), no reset happens and the loop runs at the old cost — safe degradation.

10. **Rate limit retries prevent unnecessary failures.** Parallel builds across candidates regularly hit per-minute token limits. Exponential backoff (15s, 30s, 60s) lets the rate limit window reset. Most rate limits are resolved by the first retry.

11. **PLAN + tool_use in the same turn needs special handling.** When Claude writes PLAN text AND calls write_file in the same response, the context reset must dispatch the tools silently WITHOUT appending `response.content` back to messages — that would re-add the web content we are trying to drop. The tools are dispatched separately and their results summarized in the fresh message chain.

12. **Re-inject live_test.py every time harness.py is written.** The initial injection (before the loop) does not have harness.py yet, so it cannot map env var names. After each write_file("harness.py"), `_inject_live_test_script()` re-runs with fuzzy env var name mapping (`_env_var_similarity()`) to handle mismatches like VERYFI_INC_API_KEY vs VERYFI_API_KEY.

13. **Dead-end detection prevents debugging spirals.** Without it, Claude could spend 7 turns trying to fix an unfixable problem (deprecated endpoint, requires manual dashboard setup). The consecutive error counter + strategic reassessment message forces Claude to either pivot or fail gracefully after 2 consecutive error turns (reduced from 3).

14. **Research sub-agents eliminate the research-vs-build tradeoff.** DocuClipper failed because research consumed 10/15 turns, leaving no time to build. By isolating research into a separate API call (Claude Code's s04 sub-agent pattern), the builder always starts with complete knowledge. Cost: ~$0.15-0.25 per candidate. Savings: 3-5 fewer builder turns + fewer fix iterations from correct endpoints.

15. **patch_file saves significant tokens during fix iterations.** The original design only had write_file — every bug fix rewrote the entire harness.py (~2K tokens). With patch_file (string-replace editing), fixes send ~50 tokens. Over 3-5 fix iterations per candidate, this saves 6-10K output tokens.

16. **Verification gate must not loop.** The original verification gate ran `_run_verification_checks()` redundantly when retries were exhausted (Lido bug). Fix: when retries are exhausted, accept the harness without re-checking. The builder already tried to fix the issues.

17. **Wall-clock timeout catches edge cases.** Budget cap is the primary limit, but cheap operations (run_code, read_file) can loop without hitting budget. 5-minute wall-clock timeout prevents this.

18. **In-loop context management prevents silent failures.** Without auto-compact, context can grow past the window limit after the PLAN-based reset. The API returns truncated or empty responses, which look like agent confusion — not context overflow. Estimating message size before each call and compacting when needed prevents this entire failure class.

19. **PTL recovery is free insurance.** BadRequestError for "prompt too long" is a 1-line check + compact + retry. Without it, the harness fails. With it, the conversation is trimmed and continues. Zero cost when not triggered.

20. **Large output persistence keeps errors visible.** pip install, test runs, and tracebacks regularly exceed 5K chars. Simple truncation cuts off the error message (always at the end). Head+tail preview (first 800 + last 3K) with disk persistence ensures Claude sees both context and error. This alone fixes many "Claude can't see what went wrong" failures.

21. **Exit code interpretation prevents false error detection.** Non-zero exit codes aren't always errors — grep returns 1 for "no matches," not failure. Claude Code's commandSemantics pattern adds semantic context to exit codes. Without this, the dead-end detector sees "exit code 1" as an error signal and triggers unnecessary reassessments.

22. **Microcompact before autocompact saves messages.** Claude Code uses a two-stage compaction: first clear old tool result content in-place (keeping message structure), then drop entire messages only if still over limit. Microcompact preserves conversation flow while freeing tokens. Autocompact is the nuclear option.

23. **Research sub-agents need context to be useful.** A bare question like "What auth does Parseur use?" gets generic answers. Enriching with the actual error, harness code snippet, and service details makes research targeted. The difference: generic → "Parseur uses token auth" vs targeted → "Your auth header uses 'Bearer' but Parseur expects 'Token' — change line 15."

24. **Dynamic max_tokens prevents context overflow.** When context is large (estimated from message chars), reduce max_tokens proactively instead of waiting for the API to reject the request. Cost savings: avoids wasted API call + retry cycle on PTL errors.

25. **Smoke test pass must be tracked across ALL turns.** The original code checked `last_text` (final turn only). In run c059a231, all 4 candidates passed smoke tests at turns 12-16 but the agent kept verifying until turn 24. `last_text` at turn 24 was empty → `smoke_passed=False` → FailedHarness. Fix: `smoke_ever_passed` boolean tracked across every turn's tool results.

26. **Completion nudge prevents verification spirals.** When the smoke test passes, inject a message telling the agent to wrap up. Without this, the agent enters Phase 3 (self-review) and loops forever trying to re-read/re-verify code that already works. The nudge says: "Smoke test passed. Signal HARNESS_COMPLETE now."

27. **Force-accept after smoke + N turns.** If smoke passed N turns ago and the agent hasn't signaled HARNESS_COMPLETE, force-break and accept the harness. This is the safety net for agents that get stuck in verification loops. `MAX_TURNS_AFTER_SMOKE = 5`.

28. **Windows output suppression detection.** `python -c "print(...)"` on Windows sometimes returns exit 0 with empty stdout. Without detection, the agent retries the same command 10+ times in a diagnostic spiral. Fix: detect the pattern and suggest writing a .py file instead.

29. **Two-tier validation gate: smoke → live.** Smoke test proves code structure. Live test proves API connectivity + auth with real files. Phase 3 requires real API success (`success=True`, `output_len > 100`) for EACH file type — smoke test alone is NOT sufficient for HARNESS_COMPLETE.

30. **Live test failure blocks test execution.** If live API validation fails, the harness returns as FailedHarness, not TestHarness. The post-loop test runner never receives broken harnesses.

31. **Research agent must find ALL endpoints, not just one.** The spec template uses `ENDPOINTS:` (plural) with format per endpoint. The #1 failure cause was research finding ONE endpoint (file upload) and the coding agent guessing the format for other endpoints (URL submission). With all endpoints documented, the coding agent builds correct code on the first try.

32. **Research agent must search for OpenAPI/Swagger specs.** The OpenAPI spec (`openapi.json`) is the ground truth for all endpoints, request formats, and response structures. Blog posts and quickstart guides show ONE simple example and miss everything else. The research prompt now explicitly searches for `"{service} openapi.json OR swagger.json"`.

33. **Request format per endpoint is critical.** Different endpoints on the SAME API often use DIFFERENT request formats (e.g., file upload = `files=`, URL submission = `data=`, not `json=`). The spec must document the Python `requests` parameter (`json=`, `data=`, `files=`, `params=`) for EACH endpoint. Getting this wrong causes "missing field" errors even when the code sends the right data.

34. **Adaptive thinking (interleaved reasoning) is the single biggest performance lever.** With `thinking={"type": "adaptive"}`, the model reasons BETWEEN tool calls within a single turn. A debug cycle that took 5 turns (see error → read file → read spec → patch → rerun) now takes 1-2 turns. Reduced typical build from 25+ turns to 18-22 turns.

35. **Credentials injected during build enable live validation.** `_dispatch_tool()` passes credentials to `run_code` so the agent can validate with real API calls during Phase 3. Previously credentials were only available post-loop, so the agent couldn't test live during the build. Test files are staged before the loop via `_stage_test_files()` with absolute paths in the initial message.

36. **Escalating error recovery prevents debugging spirals.** Three tiers: (1) fix specific issue, (2) question fundamental assumptions via ask_research, (3) try completely different approach or fail. Error category tracking detects when the same type of error (auth/endpoint/format) repeats 3+ times and tells the agent its APPROACH is wrong, not the details.

37. **Credential error detection saves turns.** When the API returns "API key is invalid" or "service not enabled," this is unfixable by code changes. The system detects these immediately and returns FailedHarness instead of burning 15 turns trying different auth formats.

38. **Server-side context management (beta API) replaces manual compaction.** Uses `context_management` parameter with `clear_tool_uses_20250919` (clears old tool results at 80K tokens) and `compact_20260112` (Claude-powered summarization at 150K tokens). Strictly better than manual character-based estimation.

39. **Exit-code-based `is_error` replaces string matching.** `_dispatch_tool()` returns `(result, exit_code)` tuple. `is_error` is set based on exit code, not string pattern matching for "error"/"traceback". This eliminates false positives where successful output contains the word "error" (e.g., "error handling configured successfully").

40. **Phase 1 research uses server-side web tools, not ask_research.** ask_research spawns a sub-agent that loses accumulated context. For initial research, the builder agent needs iterative search→navigate→fetch→synthesize, all in one context. ask_research is reserved for Phase 2+ debugging.

41. **Harness as thin API client eliminates parsing bugs.** Earlier harnesses tried to parse API responses, extract fields, and format output. This was the #1 source of build failures — every API has a different response structure. The thin client approach (send file, return raw response) means harness.py is simpler and the LLM judge handles response interpretation.

42. **LLM judge eliminates mechanical eval brittleness.** Mechanical eval (exact_match, format_compliance) produced false failures: "vendor_name" vs "Vendor Name", JSON keys in different order, extra whitespace. ALL criteria now go to one LLM judge that compares raw API response against Agent 3F ground truth. Raw response truncated at 15K chars. Uses `response.parsed_output` (not `.parsed`). No adaptive thinking on eval calls.

43. **Milestone messages prevent premature HARNESS_COMPLETE.** When the smoke test passes, a milestone message is injected telling the agent it must now run live API validation (Phase 3) before signaling HARNESS_COMPLETE. Without this, agents would signal completion after smoke test without ever calling the real API.

44. **Test files staged before build loop.** `_stage_test_files()` copies test files to the sandbox BEFORE the builder loop starts. Absolute paths are shown in the initial message. This lets the agent find and use real test files during Phase 3 live validation. Previously files were only staged during post-loop execution, so the agent had to download test files from the internet.

45. **run_code timeout must be 120s for async APIs.** Some APIs (Nanonets, Klippa) return a job ID and require polling. The original 30s timeout killed these calls before they completed. 120s accommodates: upload (5-10s) + processing (30-60s) + polling (10-30s).

### Key Files to Read (Agent 5)

1. `puzzleeval/schemas.py` — Agent5Input, TestHarness, FailedHarness, Agent5Result (after Agent 4 schemas)
2. `puzzleeval/agents/implement_test_env.py` — autonomous builder loop with verification gate (look for CORE LINE markers)
3. `puzzleeval/config.py` — AGENT5_* settings, PROVIDER_REGISTRY_PATH
4. `puzzleeval/provider_registry.py` — centralized API key management
5. `puzzleeval/agents/README_AGENT5.md` — complete walkthrough of Agent 5 design

### CLI Usage

```bash
# Full pipeline (Agent 1 → 2 → 4 → 3 → 5):
python -m puzzleeval.cli --text "I need invoice OCR" --agent5 --no-interactive --pretty

# Re-run Agent 5 only from a saved input (skips Agents 1-4, refreshes credentials):
python -m puzzleeval.cli --agent5-input runs/{trace-id}/agent_5_input.json --pretty

# Save output + logs:
python -m puzzleeval.cli --text "..." --agent5 --pretty > result.json 2> logs.jsonl
```

The `--agent5-input` flag loads a saved Agent 5 input file and re-runs ONLY Agent 5. It automatically refreshes credentials from the current `provider_registry.json` — not the stale credentials baked into the saved input.

When `--agent5` is set, the CLI runs Agent 1's conversation loop, then Agent 2→4 (research + screening) and Agent 3F (file-based test generation) in parallel, then loads the provider registry, then Agent 5 (harness building + LLM evaluation). The final JSON output is Agent5Result.

## What's Next

Build Agent 7 (Analyze Agent). Agent 5 produces test execution results natively. Agent 7 does cross-candidate quality analysis.

- **Input:** Test results from Agent 5's `candidate_runs` + Agent 3's judgement criteria
- **Output:** Per-candidate quality scores, strengths/weaknesses analysis
- **Key:** Agent 7 instances are ISOLATED — no cross-product context to avoid bias

### How to Build a New Agent (Checklist)

1. **Add schemas** to `puzzleeval/schemas.py`
2. **Create agent** at `puzzleeval/agents/`
3. **Add validator** to `puzzleeval/validators.py`
4. **Add CLI flag** to `puzzleeval/cli.py`
5. **Write tests** at `tests/`
6. **Update CLAUDE.md** — document key design decisions
7. Run `ANTHROPIC_API_KEY=dummy python -m pytest tests/ -v` — all tests must pass (currently 353 + 39 generalizability deselected)
