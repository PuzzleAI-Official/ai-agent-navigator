# Full Agent 4/5 Architecture Recovery Plan

## Summary

We will rebuild the Agent 4 to Agent 5 backbone around one principle:

Agent 4 finds verified API documentation entrypoints and lightweight report metadata. Agent 5 owns research strategy, implementation planning, build, debug, and evidence. Contracts verify outcomes and safety; they must not force one implementation shape.

This plan keeps the decisions already agreed in prior reviews, including completion truth, runtime/report truth, fixture consistency, provider health, frontend/audio truth, failure packets, efficiency telemetry, stale-policy cleanup, and contract simplification. It also incorporates the latest feedback:

- Keep `objective.md` orchestrator-owned. Agent 5 should not author the immutable success criteria.
- Narrow Agent 4, but do not make it blind. Agent 4 still extracts auth/access/pricing metadata for reports and frontend cards.
- Keep the research-agent direction. Agent 5 designs research; focused research workers execute scoped tasks in parallel.
- Runtime primitives are binary: `single_call` and `persistent_worker`. Streaming/duplex/session semantics live in `interaction_pattern.known_family` inside `implementation_plan.json`. Runtime owns process lifetime; Agent 5 owns how streaming works.
- Generate failure packets for meaningful failures, not every trivial command failure.
- Validate with mocks always; run budget-capped real API smoke when credentials are available. Mocks catch architecture regressions; real API smoke catches provider quirks. Both layers exist; mocks are the blocker, real API smoke is opportunistic.
- Cleanup is continuous and release-blocking for touched policy, not a giant final-only refactor.
- New artifacts must survive an artifact-budget test: they must drive action selection, acceptance, telemetry, or user-facing evidence.
- Playbooks contain outcome contracts only; no implementation guidance. Pretrained model knowledge handles HOW; playbooks define WHAT must be true.
- Every phase ships behind a default-on feature flag. Reverting one phase = one env var. Flags are centralized and surfaced in the runtime snapshot.
- Sunset rules for legacy artifacts are phase-based or outcome-based, not fixed calendar dates. Calendar dates are aspirational targets, not hard deadlines.
- Agent 5 has a truthful early-exit path (`abandon_candidate.json`) when docs are missing, the API is incompatible, the provider is blocked, or credentials are unavailable. Burning turns until budget exhaustion is not the right answer for these cases.

## Out of Scope for v1

These are deliberately NOT in this plan. Anyone proposing one of them mid-execution gets pointed at this section.

- Cross-candidate parallel research (one candidate's research informing another's plan within the same run).
- Browser/file-batch/async-job/webhook runtime primitives. Add only when a real candidate forces the need.
- Cross-run learning (build N learns from build N-1 patterns; persisted across runs).
- Dynamic gate tuning based on telemetry (gates self-adjust thresholds from observed data).
- Multi-judge ensembles for evaluation (more than one judge model per test case).
- Provider-specific specialized adapters beyond the two runtime primitives.
- Candidate-metadata-derived business fixture hallucination. Canonical fixtures from user/Agent 1 facts and test-generation needs remain in scope; the out-of-scope behavior is inventing fixture facts from provider/candidate metadata alone.
- Persistent agent memory across runs (Agent 5 remembering prior candidates' build approaches).

If any of these prove necessary during execution, they get a separate plan and a separate phase.

## Current Problems This Plan Addresses

1. Agent 4 has mixed obligations.
   - Its prompt says "find where the API docs are," but the current system also turns Agent 4 output into build-readiness and `api_spec.txt` scaffolding.
   - This lets incomplete verification masquerade as implementation truth.

2. Agent 5 phase mechanics over-shape behavior.
   - `api_spec.txt` is currently a phase boundary.
   - Phase 2 directives and scaffold nudges can push the agent into coding before it has chosen the right interaction pattern.
   - The old Phase-1 `ask_research` block prevents Agent 5 from delegating scoped initial research.

3. Contracts sometimes force implementation shape instead of verifying outcomes.
   - Voice/live-test guidance has historically pushed a per-turn `harness.run()` shape.
   - That shape fits some APIs but harmed continuous duplex providers.

4. Research is expensive and not reliably objective-directed.
   - Runs showed long server-tool turns, repeated or blocked URLs, and provider docs rediscovery.
   - Agent 5 needs planned, scoped research workers and URL/result caching.

5. Debugging still needs a stronger root-cause loop.
   - Failure packets exist but should become the primary debug surface for meaningful failures.
   - The builder should classify the failure before patching.

6. The codebase has stale and conflicting policies.
   - Agent 5 policy lives across prompts, playbooks, runtime gates, `implement_test_env.py`, tools, tests, and shims.
   - Future work must remove or explicitly shim obsolete control paths.

## Target Architecture

### Agent 4: Docs Entrypoint Verifier

Agent 4's job is to:

- decide whether public API docs exist;
- find the best official docs landing page;
- identify deprecated, blocked, or misleading URLs;
- provide evidence and confidence;
- extract lightweight report metadata: auth method, access method, pricing summary, capability hints.

Agent 4 must not produce an authoritative build plan.

New artifact:

```json
{
  "schema_version": 1,
  "candidate": "...",
  "docs_verdict": "verified_docs | no_verified_docs | rejected",
  "primary_docs_entrypoint": "...",
  "official_domain": "...",
  "alternate_entrypoints": [],
  "deprecated_or_blocked_urls": [
    {"url": "...", "reason": "deprecated | blocked | 403 | url_not_allowed | empty_spa | auth_wall"}
  ],
  "evidence": [
    {"source_url": "...", "claim": "...", "evidence_type": "fetched_page | search_snippet | official_nav"}
  ],
  "capability_hints": [],
  "auth_method": "api_key | oauth | bearer | basic | no_auth | unknown",
  "api_access_method": "free_signup | free_tier | trial | sandbox | paid_only | unknown",
  "pricing_summary": "",
  "confidence": "high | medium | low"
}
```

Default rule: no verified docs entrypoint, no automatic paid Agent 5 build.

Compatibility:

- Existing `ScreenedCandidate` fields may remain during migration.
- `BuildReadinessChecklist` remains report/debug telemetry only.
- Checklist-to-`api_spec.txt` pre-render is disabled by default and must not drive Agent 5 phase state.

### Agent 5: Research, Implementation, Build, Debug

Agent 5 starts from:

- orchestrator-owned `_agent_state/objective.md`;
- Agent 4 `docs_entrypoint.json`;
- Agent 3 test cases and business fixture;
- credentials metadata;
- provider/report metadata;
- prefetched docs/cache where available.

Agent 5 should produce and use:

```text
_agent_state/research_plan.json
_agent_state/research_findings/*.json
_agent_state/research_synthesis.json
_agent_state/implementation_plan.json
_agent_state/failure_packets/*.json
_agent_state/build_decisions.jsonl
_agent_state/runtime_state.json
```

`_agent_state/objective.md` stays orchestrator-owned and immutable to Agent 5. Agent 5 can interpret it in `implementation_plan.json`, but cannot replace or weaken it.

### Agent 5 Flow

1. Read objective, docs entrypoint, tests, fixture, and starting files.
2. Write `research_plan.json`.
3. Orchestrator runs focused research workers in parallel.
4. Agent 5 writes `research_synthesis.json`.
5. Agent 5 writes `implementation_plan.json`.
6. Orchestrator validates implementation plan against objective and required fields.
7. Agent 5 builds harness files.
8. Smoke/live tests run.
9. Meaningful failures produce failure packets.
10. Agent 5 classifies failure before patching.
11. Completion gate verifies current files, reflection, forensics, runtime state, and report truth.

## Public/Internal Interfaces

### `docs_entrypoint.json`

Owner: Agent 4 / orchestrator.

Purpose:

- Decide whether Agent 5 should build.
- Provide official docs starting point.
- Preserve lightweight frontend/report metadata.
- Record deprecated or blocked URLs so Agent 5 does not retry them blindly.

Must not:

- Claim implementation readiness.
- Force endpoint choice.
- Generate `api_spec.txt`.

### `objective.md`

Owner: orchestrator.

Purpose:

- Immutable success criteria.
- User/test/fixture/candidate constraints.
- Completion evidence target.

Agent 5 may read it and cite it. Agent 5 may not write or patch it.

### `research_plan.json`

Owner: Agent 5.

Purpose:

- Tell the orchestrator which research workers to launch.
- Split research by objective-specific information gaps.
- Avoid broad rediscovery.

Minimum shape:

```json
{
  "schema_version": 1,
  "docs_entrypoint": "...",
  "objective_summary": "...",
  "research_tasks": [
    {
      "id": "auth",
      "question": "How does authentication work for this API surface?",
      "where_to_look": ["official docs entrypoint or known child pages"],
      "evidence_required": ["source URL", "exact header/token/session flow"],
      "why_needed_for_build": "..."
    }
  ],
  "stop_condition": "Enough evidence to write implementation_plan.json without guessing required behavior."
}
```

### `research_findings/*.json`

Owner: research workers.

Purpose:

- Scoped evidence collection.
- Direct answer with citations.
- Confidence and unresolved questions.

Minimum shape:

```json
{
  "schema_version": 1,
  "task_id": "...",
  "answer": "...",
  "confidence": "high | medium | low | not_found",
  "sources": [{"url": "...", "claim": "..."}],
  "blocked_or_dead_urls": [],
  "unresolved_questions": [],
  "notes_for_builder": []
}
```

### `research_synthesis.json`

Owner: Agent 5.

Purpose:

- Merge research worker outputs into one build understanding.
- Distinguish facts, assumptions, and open risks.
- Decide whether to proceed to implementation planning.

### `implementation_plan.json`

Owner: Agent 5.

Purpose:

- Replace `api_spec.txt` as the build gate.
- Explain how the harness will satisfy the objective.
- Capture interaction pattern without forcing a hard lifecycle enum.

Minimum shape:

```json
{
  "schema_version": 1,
  "objective_coverage": [],
  "chosen_api_surface": [],
  "credential_loading": {},
  "input_mapping": {},
  "output_mapping": {},
  "interaction_pattern": {
    "pattern_name": "...",
    "known_family": "request_response | async_job | webhook | serialized_conversation | persistent_session | continuous_stream | browser_session | file_batch | other",
    "why_this_pattern": "...",
    "state_owner": "...",
    "input_clocking": "...",
    "output_completion_signal": "...",
    "cleanup_required": true,
    "open_questions": [
      {"question": "...", "blocking": false}
    ]
  },
  "live_test_strategy": {},
  "cleanup_strategy": {},
  "remaining_risks": [],
  "ready_to_build": true
}
```

Validation criteria (orchestrator-enforced, runs after Agent 5 writes the plan):

1. `objective_coverage` references every success criterion ID from `objective.md`. Missing IDs are a hard failure.
2. `chosen_api_surface` is non-empty. At least one endpoint URL or SDK method must be specified.
3. `credential_loading.env_vars` covers every required credential staged for the candidate. Missing required env vars are a hard failure. Extra env vars are allowed only when marked optional or explained; unexplained extras are a warning unless they create credential ambiguity.
4. `interaction_pattern.known_family` is set. If it is `other`, `why_this_pattern` must be non-placeholder and must explain state ownership, input clocking, and output completion in provider-specific terms. Do not rely on a raw length threshold as proof of substance.
5. `live_test_strategy` is concrete: not `TBD`, not empty, and must name how production-equivalence and task-equivalence will be proven. Reject placeholder prose and one-sentence filler; do not accept length alone as evidence.
6. `interaction_pattern.open_questions`: no entry with `"blocking": true`. Non-blocking unknowns are allowed without limit; blocking unknowns mean the plan is not ready.
7. `ready_to_build` must be `true`. Setting it to `false` returns the plan to Agent 5 for revision.

Validator returns the list of failed criteria. Agent 5 gets configurable revision attempts (default 1, settable via `PUZZLEEVAL_IMPLEMENTATION_PLAN_MAX_REVISIONS`) before the build is blocked. After max revisions, the candidate is marked failed with `failure_category: implementation_plan_invalid`.

### `abandon_candidate.json`

Owner: Agent 5 (proposes), orchestrator (validates and accepts).

Purpose:

- Truthful early exit when continuing the build would be dishonest or wasteful.
- Avoid burning turn/budget caps on candidates that cannot succeed in this run.

Minimum shape:

```json
{
  "schema_version": 1,
  "candidate": "...",
  "reason": "provider_blocked | api_incompatible | docs_missing | credentials_unavailable | quota_exhausted",
  "evidence": [
    {"source": "research_findings/auth.json | live_test stderr | docs_entrypoint.json", "claim": "..."}
  ],
  "recommended_action": "skip_for_this_run | retry_when_credentials_available | re-research_after_24h",
  "turns_consumed_at_abandon": 0,
  "cost_at_abandon_usd": 0.0
}
```

Orchestrator validation:

- `reason` must match one of the enum values.
- `evidence` is non-empty and references real artifacts in the sandbox or research findings.
- Evidence must be external to Agent 5 self-assertion. Acceptable evidence includes `docs_entrypoint.json`, provider health output, credential checks, research worker `not_found` findings, quota/auth responses, fixture/objective validation failures, or repeated same-category failure packets.
- The runtime accepts the abandonment, records it in `runtime_state.json`, marks the candidate as failed-with-classification (NOT failed-without-explanation), and continues the run with remaining candidates.

Frontend rendering:

- Candidate card shows "Abandoned: <reason>" instead of "Build failed."
- Final report distinguishes abandoned candidates from gate-rejected candidates in efficiency summary.
- Audit log records who abandoned (Agent 5 self-classification) vs orchestrator-stopped (provider health check failed).

### Failure Packets

Owner: orchestrator/runtime.

Generate packets for:

- smoke test failures;
- live test failures;
- completion gate rejection;
- provider blocked classification;
- timeouts;
- repeated same-category command failures;
- failure after first patch did not recover.

Do not generate packets for every trivial failed command.

Failure packets should include:

- source command/gate;
- stdout/stderr tail;
- traceback;
- runtime state excerpt;
- changed files since last test;
- forensics summary;
- suspected category;
- recommended next action.

## Key Changes By Subsystem

### Agent 4

- Add `docs_entrypoint.json` generation.
- Update prompt to verify docs entrypoint and extract lightweight metadata.
- Stop asking Agent 4 to produce build-readiness as the primary artifact.
- Keep old checklist fields only for compatibility/report/debug.
- Reject or mark no-build when docs entrypoint is not verified.
- Preserve pricing/auth/access metadata for frontend and reports.

### Agent 5 Initial State

- Keep orchestrator-owned objective.
- Stage docs entrypoint, fixture, test cases, credentials, cached docs, and runtime state.
- Initial message should describe the new flow:
  - read objective;
  - write research plan;
  - synthesize research;
  - write implementation plan;
  - build.
- Remove language that makes `api_spec.txt` the first required artifact.

### Research

- Add planned research batch runner.
- Agent 5 designs research tasks.
- Research workers execute scoped tasks in parallel.
- Add shared URL/result cache across candidates and turns.
- Record terminal URLs.
- Keep current `ask_research` only for targeted debug gaps until the new path fully replaces it.

### Implementation Plan Gate

- Replace `api_spec_written` as the active build phase gate.
- Build begins only after `implementation_plan.json` passes validation.
- `api_spec.txt` may be created as a helper file, but it cannot trigger phase transition.
- Remove forced Sonnet to Opus transition tied to `api_spec.txt`.

### Contracts and Playbooks

Operating principle: playbooks contain **outcome contracts only**. Pretrained model knowledge handles HOW; playbooks define WHAT must be true. Implementation prose ("use this collect_response pattern", "wrap in try/except like X", "the WebSocket should reconnect with backoff") gets removed. Provider-neutral schema fragments are allowed in playbooks only when they define the shape of an outcome contract, such as `interaction_pattern.known_family`; they must not become implementation examples.

Keep contracts focused on:

- outcome correctness;
- production equivalence;
- task equivalence;
- evidence;
- cleanup/resource safety;
- report truth.

Rewrite contracts that prescribe:

- one voice runner shape;
- one live-test implementation recipe;
- one scaffold order;
- one research phase artifact;
- specific event-handling code patterns;
- specific timeout/retry recipes.

Voice/conversation playbooks should ONLY ask outcome questions:

- What interaction pattern does the provider require?
- Who owns state?
- How is user input clocked?
- How is output completion detected?
- How is cleanup performed?
- How does the live test prove the same behavior production evaluation uses?

Anti-pattern (will be removed during refactor):

- Code snippets showing "the right way" to handle WebSocket events.
- Numbered implementation steps ("1. open connection, 2. send session.update, 3. ...").
- Specific timeout values or retry counts as part of the contract.
- Provider-named recipes ("for ElevenLabs use X, for OpenAI Realtime use Y").

Expected outcome: playbook files shrink by roughly 60-80%. Surviving content is outcome contracts + a short "what to verify" checklist. Anything teaching the agent HOW to write code gets deleted. Provider-neutral schema fragments may stay only when they define contract shape; provider-specific examples move out or get deleted.

### Runtime Primitives

Runtime primitives are **binary**: they encode the choice the agent cannot make on its own - does the harness process die between turns or stay alive.

Ship two primitives only:

- **`single_call`** - subprocess-per-call (today's behavior). Default for stateless providers (REST, OCR, code-gen, etc.).
- **`persistent_worker`** - one process per conversation, JSON over stdin/stdout for per-turn requests. Required for any provider where in-memory state (WebSocket, conversation_id, cookies, persistent SDK clients) must survive across turns.

`continuous_stream`, `serialized_conversation`, `persistent_session`, and similar streaming/session semantics are NOT separate runtime primitives. They are values for `interaction_pattern.known_family` inside `implementation_plan.json`. The agent uses pretrained knowledge to write the streaming code; the runtime only decides whether the worker process is single-shot or persistent.

Why this split:

- Runtime owns **process lifetime** (the thing the agent cannot decide for itself; it's a system architecture choice).
- Agent 5 owns **how streaming works** (event names, completion signals, audio chunk handling - pretrained knowledge handles this for known providers; research_findings handles it for unknown ones).

Adapter selection rule:

- `interaction_pattern.state_owner == "provider_server"` -> `single_call` is fine (server holds state via `conversation_id` or session_token).
- `interaction_pattern.state_owner == "harness_process"` -> `persistent_worker` required (in-memory state must survive across turns).

Async/webhook/browser/file-batch primitives are explicitly NOT shipped in v1 (see Out of Scope). They go behind their own future plans when a real candidate forces the need.

### Debug Loop

Agent 5 must classify meaningful failures before patching:

- code bug;
- missing docs info;
- wrong docs interpretation;
- wrong interaction pattern;
- provider/account blocked;
- test/fixture mismatch;
- credentials unavailable;
- API genuinely incompatible with the use case.

Routing:

- docs gap -> targeted research (`ask_research_gap` with the failure packet attached).
- wrong interaction pattern -> revise `implementation_plan.json` before patching code.
- provider blocked -> write `abandon_candidate.json` with `reason: provider_blocked` and stop the build.
- credentials unavailable -> write `abandon_candidate.json` with `reason: credentials_unavailable` and stop.
- API incompatible (research finds the API genuinely cannot do what objective requires) -> write `abandon_candidate.json` with `reason: api_incompatible` and stop.
- code bug -> patch code.
- test mismatch -> fix test/live-test contract or fixture (within Agent 5's authority); if the contradiction is in immutable objective/fixture, abandon with `reason: test/fixture_mismatch_unfixable`.

Agent 5 has the authority to abandon. The orchestrator validates the abandonment evidence and accepts it, marking the candidate as failed-with-classification (NOT failed-without-explanation). Burning turn budget on a candidate that should be abandoned is itself a failure mode.

### Runtime and Report Truth

Keep and tighten:

- unified completion gate;
- independent smoke/live/completion statuses;
- final runtime-state flush on all exit paths;
- no winner if all candidates fail;
- judge failures surfaced explicitly;
- full conversation audio visible in frontend;
- build efficiency summary.

### Agent 3 / Fixture Consistency

This remains part of the recovery plan because Agent 5 cannot build a truthful harness if tests contradict the fixture.

- Generate or stage canonical `business_fixture.json` before Agent 3 test generation when domain facts are needed.
- Agent 3 tests, objective, live tests, rubrics, and reports must reference the same fixture.
- Contradictory tests should be rejected or warned:
  - unavailable item but rubric demands successful order;
  - missing price but expected total required;
  - closed day but successful appointment expected.

### Frontend and Evaluation UX

Keep previously planned user-visible fixes:

- merged conversation audio appears on final voice result cards;
- progress messages are deduped;
- long-running test progress has meaningful events;
- report shows why a build took long;
- judge failures are explicit;
- no winner when all candidates fail.

## Cleanup Policy

Cleanup is a release-blocking part of the project.

Canonical ownership:

- Agent 4 prompt: docs entrypoint and lightweight access facts.
- Agent 5 prompt: objective/research/build/debug workflow.
- Research worker prompt: scoped evidence collection.
- Playbooks: outcome expectations and capability reminders.
- Verification modules: hard acceptance gates.
- Tool modules: file/tool policy.
- Runtime state modules: orchestrator truth.
- `implement_test_env.py`: compatibility glue only.

Every touched legacy policy must be classified as:

- `keep`: still matches the new architecture.
- `rewrite`: useful intent, wrong implementation or wording.
- `demote`: keep as telemetry/advisory, not control flow.
- `delete`: actively harmful, obsolete, or duplicated.
- `shim`: retained only for backwards compatibility.

Conflict rules:

- If old code enforces rejected behavior, delete or feature-disable it with the replacement.
- If old tests assert stale behavior, rewrite them to assert the new contract.
- If old helpers are still imported, convert them to thin shims and add source-grep tests proving the canonical owner.
- If two modules define the same policy, pick one owner and make the other import from it.
- If prompt and deterministic code disagree, deterministic code wins and prompt is corrected.
- If an artifact is not used for action selection, acceptance, telemetry, or user-facing evidence, delete or demote it.

Artifact budget:

- New artifacts must affect action selection, acceptance, telemetry, or user-facing evidence.
- Target: architecture artifacts should stay below 25 percent of build turns.
- If an artifact is not read by the orchestrator, final report, or Agent 5's next decision, delete or demote it.

### Migration Flags

Every phase ships behind a default-on env-var flag. Reverting one phase does not revert the architecture; it just flips one switch and a degraded emergency rollback path takes over.

Flag `0` branches are not a second supported architecture. They exist to keep the product operable while the phase is being rolled out, and they should either run cleanly in legacy mode or fail with an explicit degraded/unsupported classification. They must not become permanent parallel control paths.

Flag definitions live centrally in `puzzleeval/config.py` (the existing pattern for AD-007 gates). They are surfaced in `runtime_state.json` under a new `migration_flags` field so operators can see at a glance which paths a given run took.

Phase-to-flag mapping:

- Phase 1 - `PUZZLEEVAL_AGENT4_DOCS_ENTRYPOINT_ENABLED` (default `1`). When `0`, Agent 4 falls back to the degraded legacy `BuildReadinessChecklist`-as-build-truth path.
- Phase 2 - `PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED` (default `1`). When `0`, the implementation-plan-vs-objective coverage check is skipped.
- Phase 3 - `PUZZLEEVAL_RESEARCH_WORKERS_ENABLED` (default `1`). When `0`, Agent 5 falls back to the degraded legacy in-loop `ask_research` + web tools path.
- Phase 4 - `PUZZLEEVAL_IMPLEMENTATION_PLAN_GATE_ENABLED` (default `1`). When `0`, the build loop reverts to the degraded legacy `api_spec_written` phase boundary.
- Phase 5 - `PUZZLEEVAL_PERSISTENT_WORKER_RUNTIME_ENABLED` (default `1`). When `0`, all candidates use single-call subprocess execution (today's behavior); voice harnesses lose continuity and should be marked degraded.
- Phase 6 - `PUZZLEEVAL_FAILURE_PACKET_DEBUG_ENABLED` (default `1`). When `0`, failures fall back to plain stderr text in the conversation log and debug classification is degraded.
- Phase 6 - `PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED` (default `1`). When `0`, the early-exit path is disabled and Agent 5 must fail through the normal turn/budget caps.
- Phase 7 - `PUZZLEEVAL_EFFICIENCY_SUMMARY_ENABLED` (default `1`). When `0`, the per-phase telemetry rollup is suppressed; raw cost meter only.

Each flag must:

- Default to `1` (enabled).
- Be readable at runtime; the degraded legacy path remains importable until the phase's sunset rule fires.
- Be logged in the `migration_flags` field of `runtime_state.json` at run start.
- Be referenced in the corresponding phase's deliverables and acceptance criteria.

After a phase's sunset rule fires (see below), the flag's `0` branch is deleted and the flag itself is removed.

### Sunset Rules

Sunset rules are **phase-based or outcome-based**, not fixed calendar dates. Calendar dates are aspirational targets for planning, not hard deadlines that produce broken behavior if missed.

Each demoted artifact gets a sunset rule of one of these forms:

- **Phase-based:** "Removed in Phase N." Concrete and predictable.
- **Outcome-based:** "Removed after K successful runs on the new flow with no regressions." Tied to evidence, not the calendar.
- **Both:** target Phase + outcome guard, whichever comes later.

Active sunset rules:

| Artifact / policy | Sunset rule | Target date (aspirational) |
|---|---|---|
| `api_spec.txt` as phase gate | Phase 4 (replaced by `implementation_plan.json` gate) | 2026-06-30 |
| `api_spec.txt` as helper file | Indefinite; reassess at Phase 9 retrospective | n/a |
| `BuildReadinessChecklist` as build truth | Phase 1 (demoted to telemetry) | 2026-06-15 |
| `BuildReadinessChecklist` shim | Removed after 2 successful runs on new docs-entrypoint flow | 2026-08-31 |
| `build_plan.md` directive | Phase 8 (deleted) | 2026-09-30 |
| `agent_observations.json` directive-suppression | Phase 8 (deleted) | 2026-09-30 |
| Phase 2 directive (`PHASE2_DIRECTIVE`) | Phase 4 (removed when `api_spec.txt` gate is removed) | 2026-06-30 |
| Phase-1 `ask_research` block | Phase 3 (replaced by research planner) | 2026-07-15 |
| Voice playbook per-turn implementation prose | Phase 5 (rewritten as outcome contract) | 2026-08-01 |
| Streaming playbook implementation recipes | Phase 5 (rewritten as outcome contract) | 2026-08-01 |
| Legacy helpers in `implement_test_env.py` | Outcome: removed when `agent5/*` modules cover all callers + 1 successful real-API run | n/a |

If a target date passes without the sunset firing, the project owner reviews. The rule does not auto-relax; the deadline-missed signal is what triggers a deliberate decision.

## Stale Policy Inventory To Resolve

These policies must be inventoried and resolved during migration:

- Agent 4 `BuildReadinessChecklist` prompt/schema/tests.
- Agent 4 `research_handoff.json`.
- checklist-to-`api_spec.txt` pre-render.
- `api_spec_written` phase transition.
- Phase 2 directive.
- context compaction tied to `api_spec.txt`.
- Phase-1 `ask_research` block.
- `build_plan.md` directive and staleness logic.
- `agent_observations.json` directive-suppression concept.
- system-generated objective wording that implies Agent 5 cannot interpret objective.
- voice playbook per-turn semantics.
- live-test voice runner prescription.
- streaming playbook implementation recipes.
- duplicated tool extension policies.
- duplicated completion/verification paths.
- legacy helpers in `implement_test_env.py`.
- tests that assert stale architecture behavior.

## Implementation Phases

### Phase 0: Inventory and Baseline

Deliverables:

- Policy inventory markdown or JSON.
- Baseline metrics from latest runs:
  - research time/cost;
  - build turns;
  - failure categories;
  - gate failures;
  - provider blocks;
  - audio/report truth;
  - duplicate policy paths.
- Stale test list.
- Artifact list with owner and survival reason.

Acceptance:

- Every Agent 4/5 policy source is classified.
- No behavior migration starts without the inventory.

### Phase 1: Agent 4 Entrypoint Refactor

Feature flag: `PUZZLEEVAL_AGENT4_DOCS_ENTRYPOINT_ENABLED` (default `1`). When `0`, Agent 4 keeps emitting `BuildReadinessChecklist` as the primary build artifact.

Deliverables:

- `docs_entrypoint.json` artifact.
- Agent 4 prompt/schema update.
- Lightweight metadata retained for reports.
- Verified-docs gating for automatic Agent 5 build.
- Checklist demoted from build truth.
- Flag wired in `puzzleeval/config.py` and surfaced under `runtime_state.json.migration_flags`.

Acceptance:

- Verified official docs proceeds.
- Missing docs blocks automatic build.
- Deprecated docs are recorded and not primary.
- Auth/access/pricing metadata still appears in report data.
- With flag `0`, the legacy `BuildReadinessChecklist`-as-build-truth path still runs end to end.

### Phase 2: Objective Contract Stabilization

Feature flag: `PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED` (default `1`). When `0`, the implementation-plan-vs-objective coverage check is skipped; Agent 5's plan goes straight to the build step.

Deliverables:

- Keep orchestrator-owned `objective.md`.
- Agent 5 candidate-specific interpretation moves to `implementation_plan.json`.
- Validator checks implementation plan/objective coverage.
- Flag wired in `puzzleeval/config.py` and surfaced under `runtime_state.json.migration_flags`.

Acceptance:

- Agent 5 cannot weaken immutable success criteria.
- Objective remains useful for reflection and live-test task-equivalence checks.
- With flag `0`, plans missing `objective_coverage` entries still proceed to build (legacy behavior); telemetry records the skipped check.

### Phase 3: Research Planner and Workers

Feature flag: `PUZZLEEVAL_RESEARCH_WORKERS_ENABLED` (default `1`). When `0`, Agent 5 falls back to the legacy in-loop `ask_research` + web tools path; the Phase-1 `ask_research` block is restored.

Deliverables:

- `research_plan.json`.
- Research batch runner.
- `research_findings/*.json`.
- `research_synthesis.json`.
- URL/result cache.
- Terminal URL memory integration.
- Flag wired in `puzzleeval/config.py` and surfaced under `runtime_state.json.migration_flags`.

Acceptance:

- Research plan precedes coding.
- Independent research tasks can run in parallel.
- Research workers cite official docs.
- Repeated blocked/terminal URLs are not refetched.
- Old broad Phase-1 research behavior is disabled once replacement passes tests.
- With flag `0`, the legacy in-loop `ask_research` + web tools path still works end to end.

### Phase 4: Implementation Plan Gate

Feature flag: `PUZZLEEVAL_IMPLEMENTATION_PLAN_GATE_ENABLED` (default `1`). When `0`, the build loop reverts to `api_spec_written` as the phase boundary; `implementation_plan.json` is still authored but is not gating.

Deliverables:

- `implementation_plan.json`.
- Validation for required implementation-plan fields (the 7 criteria from the artifact spec).
- Build loop uses implementation plan, not `api_spec.txt`, as build gate.
- `api_spec.txt` becomes optional helper/compat artifact.
- `PUZZLEEVAL_IMPLEMENTATION_PLAN_MAX_REVISIONS` env var (default 1) for revision budget.
- Flag wired in `puzzleeval/config.py` and surfaced under `runtime_state.json.migration_flags`.

Acceptance:

- No active branch flips build phase solely because `api_spec.txt` changed.
- Build cannot start without an accepted implementation plan.
- Unknown provider shapes can use `known_family: other` with explanation.
- With flag `0`, builds gated only by `api_spec_written` still work end to end.

### Phase 5: Contract Rewrite and Runtime Primitives

Feature flag: `PUZZLEEVAL_PERSISTENT_WORKER_RUNTIME_ENABLED` (default `1`). When `0`, all candidates use single-call subprocess execution (today's behavior); voice harnesses lose continuity.

Deliverables:

- Voice/live/streaming playbooks rewritten as outcome contracts (no implementation prose, no implementation code snippets, no provider-named recipes; target ~60-80% size reduction per the Contracts and Playbooks section). Provider-neutral schema fragments are allowed when they define contract shape, not implementation behavior.
- Adapter-aware live-test validation.
- Runtime primitives - **binary**:
  - `single_call` (subprocess-per-call; today's behavior; default for stateless providers);
  - `persistent_worker` (one process per conversation; JSON over stdin/stdout; required when `interaction_pattern.state_owner == "harness_process"`).
- Streaming/duplex/session semantics handled inside `interaction_pattern.known_family` in `implementation_plan.json` (continuous_stream, serialized_conversation, persistent_session, etc. are interaction patterns, NOT runtime primitives).
- Adapter selection rule wired: `state_owner == "provider_server"` -> `single_call` OK; `state_owner == "harness_process"` -> `persistent_worker` required.
- Flag wired in `puzzleeval/config.py` and surfaced under `runtime_state.json.migration_flags`.

Acceptance:

- Voice providers are not forced into per-turn runtime.
- Live test proves production equivalence and task equivalence.
- Continuous duplex providers can be represented inside `persistent_worker` without per-turn squeezing.
- Playbooks contain no implementation code snippets, no numbered implementation steps, and no provider-named recipes. Grep tests must distinguish contract-schema snippets from HOW-to-code snippets.
- With flag `0`, all candidates run in `single_call` mode and voice harnesses degrade to the legacy per-turn shape.

### Phase 6: Debug Loop Upgrade

Feature flags:

- `PUZZLEEVAL_FAILURE_PACKET_DEBUG_ENABLED` (default `1`). When `0`, failures fall back to plain stderr text in the conversation log; classification is skipped.
- `PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED` (default `1`). When `0`, the early-exit path is disabled and Agent 5 must fail through the normal turn/budget caps.

Deliverables:

- Meaningful-failure packet policy.
- Failure classification (the 8 categories from the Debug Loop section).
- Debug routing rules.
- Provider-block stop behavior.
- `abandon_candidate.json` artifact + orchestrator validator.
- Frontend rendering for abandoned-vs-failed candidates.
- Both flags wired in `puzzleeval/config.py` and surfaced under `runtime_state.json.migration_flags`.

Acceptance:

- Agent 5 reads failure packet before patching meaningful failures.
- Missing docs info routes to targeted research.
- Wrong interaction pattern routes to implementation-plan revision.
- Provider blocked stops debug loops truthfully.
- Agent 5 can write `abandon_candidate.json` for `provider_blocked`, `credentials_unavailable`, `api_incompatible`, `docs_missing`, `test/fixture_mismatch_unfixable`.
- Final report distinguishes abandoned candidates from gate-rejected candidates.
- With `PUZZLEEVAL_FAILURE_PACKET_DEBUG_ENABLED=0`, the legacy stderr-only failure path still works.
- With `PUZZLEEVAL_ABANDON_CANDIDATE_ENABLED=0`, Agent 5 fails through normal turn caps with no early exit.

### Phase 7: Report, Frontend, and Efficiency

Feature flag: `PUZZLEEVAL_EFFICIENCY_SUMMARY_ENABLED` (default `1`). When `0`, the per-phase telemetry rollup is suppressed; raw cost meter only.

Deliverables:

- Full conversation audio visible per voice test.
- Deduped frontend progress.
- Long-running progress events.
- Efficiency summary:
  - turns by phase;
  - cost by phase;
  - latency by phase;
  - research attempts/cache hits;
  - blocked/empty fetches;
  - artifact overhead;
  - failure packet count/categories;
  - provider health status;
  - abandoned candidate count/reasons (from Phase 6).
- Migration flag block in efficiency summary header (which flags fired in this run).
- Flag wired in `puzzleeval/config.py` and surfaced under `runtime_state.json.migration_flags`.

Acceptance:

- User can understand why a build took long without raw logs.
- All-failed run has no winner.
- Judge failures are explicit.
- Voice audio is playable when merged audio exists.
- Migration flag state is visible in the run report.
- With flag `0`, the run still completes; only the per-phase rollup is missing.

### Phase 8: Stale Policy Audit

Cleanup happens in every earlier phase. Phase 8 is the final audit and removal gate.

Remove or disable obsolete control paths:

- checklist-to-`api_spec.txt` pre-render by default;
- `api_spec_written` as active phase boundary;
- Phase 2 directive;
- Sonnet to Opus transition triggered by `api_spec.txt`;
- Phase-1 `ask_research` refusal logic;
- build-plan initialization/staleness nudges;
- directive suppression based on agent-authored observations;
- hardcoded voice per-turn live-test requirement;
- completion fast paths that bypass unified gate;
- duplicate write/patch/read extension allowlists;
- duplicated runtime/report truth logic in legacy modules.

Rewrite stale tests:

- `test_ask_research_phase_gate.py` asserts planned research replaces Phase-1 blocking.
- Agent 4 handoff tests assert docs-entrypoint truth, not checklist authority.
- Agent 5 architecture tests assert implementation-plan gating, not `api_spec.txt` gating.
- Voice contract tests assert adapter/outcome equivalence, not one per-turn implementation.
- Cleanup tests assert `implement_test_env.py` is shim-only.

Acceptance:

- No active phase gate depends on `api_spec.txt`.
- No active build path treats Agent 4 checklist as build truth.
- No active gate requires `build_plan.md` or `agent_observations.json`.
- No active prompt says to write `api_spec.txt` early and let live errors teach before research synthesis.
- No active playbook says all voice providers must use per-turn `harness.run()` semantics.
- No active module defines local tool extension policy when `agent5.tools` is canonical.

### Phase 9: Validation and Rollout

Mocks are the blocker. Real API smoke is opportunistic - runs when credentials are available, never the gate that holds rollout.

Mock providers (one per `interaction_pattern.known_family` value that v1 supports, plus failure-routing scenarios):

- REST request/response (`single_call`, state on provider server).
- Serialized conversation (`single_call`, server-held conversation_id).
- Persistent session (`persistent_worker`, harness-held session state).
- Continuous stream (`persistent_worker`, harness-held WebSocket state).
- Provider blocked (exercises abandon_candidate `provider_blocked` path).
- Credentials unavailable (exercises abandon_candidate `credentials_unavailable` path).
- API incompatible (exercises abandon_candidate `api_incompatible` path).
- Deprecated docs (exercises Agent 4 docs verdict + alternate entrypoint).
- Missing docs (exercises Agent 4 `no_verified_docs` block).

Mocks for `async_job`, `webhook`, `browser_session`, and `file_batch` are NOT in v1 (see Out of Scope). They get added when their respective primitives are introduced.

Real API smoke (opportunistic):

- Budget-capped runs against providers with credentials staged in the local env.
- Skipped without failing rollout when credentials are absent or quota is exhausted.
- Run results recorded in the efficiency summary; failures don't block rollout unless they reproduce in mocks.

Acceptance:

- All mock scenarios pass on the v1 path.
- Agent 4 verified docs entrypoint exists for verified-docs scenarios.
- Agent 5 objective coverage is validated.
- Research plan/synthesis present in every successful build.
- Implementation plan present and accepted in every successful build.
- No old phase gate controls build.
- Runtime/report truth tests pass.
- Phase 8 stale-policy tests pass.
- Real API smoke is attempted when credentials are present; absence of credentials is recorded but does not fail the rollout.
- Each migration flag has been exercised in both `1` and `0` settings via mocks (rollback safety).

## Test Plan

### Agent 4

- Verified docs produce `docs_entrypoint.json`.
- Missing docs blocks automatic Agent 5 build.
- Deprecated docs are recorded but not primary.
- Auth/access/pricing metadata is preserved.
- Checklist no longer acts as build truth.

### Agent 5 Research

- Research plan precedes coding.
- Research workers run scoped tasks.
- Findings cite official docs.
- URL cache prevents duplicate fetches.
- Terminal URL memory prevents retries.
- Research synthesis distinguishes facts, assumptions, and unresolved questions.

### Agent 5 Implementation

- Implementation plan precedes `harness.py`.
- Interaction pattern is open-ended.
- Build starts only after implementation-plan validation.
- `api_spec.txt` cannot trigger phase transition.

### Contracts

- Voice is not forced into per-turn runtime.
- Live tests prove task and production equivalence.
- Completion gate checks current state only.
- Reflection remains evidence-based.
- Provider blocked stops debug loops truthfully.

### Playbooks (outcome-only enforcement)

- Voice/conversation/streaming playbooks contain no implementation code snippets (grep `\`\`\`python|\`\`\`javascript` against playbook files; expected: 0 matches). JSON/schema snippets are allowed in playbooks only when provider-neutral and used to define outcome-contract shape; tests should reject JSON snippets that contain provider names, endpoint/event recipes, timeout/retry numbers, or step-by-step implementation instructions.
- Playbooks contain no numbered implementation steps ("1. open connection, 2. send..."); regex check for ordered-list HOW patterns.
- Playbooks contain no specific timeout/retry numeric recipes (regex for `timeout=\d+`, `retries=\d+` in playbook prose).
- Playbooks contain no provider-named recipes ("for ElevenLabs use X, for OpenAI Realtime use Y"); grep against known provider name list.
- Each surviving playbook section maps to at least one outcome-contract question (auth/state-owner/clocking/completion/cleanup/equivalence). Sections that don't are deletion candidates.
- Playbook line count after rewrite is 60-80% smaller than the pre-rewrite baseline (Phase 0 baseline metric).

### Abandon candidate path

- Agent 5 can write `abandon_candidate.json` with each enum reason: `provider_blocked`, `api_incompatible`, `docs_missing`, `credentials_unavailable`, `quota_exhausted`, `test/fixture_mismatch_unfixable`.
- Orchestrator validator rejects abandon packets with empty `evidence`, Agent-5-only self-assertion, missing `reason`, unknown reason values, or evidence that does not resolve to real artifacts/provider responses.
- Orchestrator records abandonment in `runtime_state.json` and continues with remaining candidates.
- Frontend renders "Abandoned: <reason>" instead of "Build failed" for abandoned candidates.
- Final report distinguishes abandoned vs gate-rejected vs failed-with-error in the efficiency summary.
- Abandoned candidates do not produce a winner in all-failed reports.
- A candidate that should have abandoned but kept building until budget exhaustion shows up as a regression test failure (mock: provider returns 403 from turn 1; agent should abandon, not retry indefinitely).

### Migration flag rollback

- For each `PUZZLEEVAL_*_ENABLED` flag, a regression test runs the same scenario with the flag at `0` and confirms the degraded rollback path either produces a working legacy result or fails with an explicit degraded/unsupported classification. The test must not turn the legacy path into a permanent first-class architecture.
- `runtime_state.json.migration_flags` records flag state at run start; assertion checks all 8 flags are present.
- Flipping a flag mid-run is not supported; tests confirm the flag is read once at run start (not re-read per turn).
- Removing a flag's `0` branch (after sunset) requires updating the corresponding test to remove the rollback case; CI fails if a sunset-target flag still has rollback test coverage past its target date.

### Implementation plan gate

- Plan missing any `objective_coverage` ID is rejected.
- Plan with empty `chosen_api_surface` is rejected.
- Plan missing required `credential_loading.env_vars` is rejected; optional extras are allowed only when explicitly marked optional or explained.
- Plan with `interaction_pattern.known_family: other` and placeholder `why_this_pattern` is rejected; concrete reasoning about state, clocking, and completion is required.
- Plan with `live_test_strategy` of `TBD`, empty, placeholder prose, or no production/task-equivalence explanation is rejected.
- Plan with any `open_questions[].blocking == true` is rejected.
- Plan with `ready_to_build: false` is returned for revision.
- After `PUZZLEEVAL_IMPLEMENTATION_PLAN_MAX_REVISIONS` revisions (default 1), candidate is failed with `failure_category: implementation_plan_invalid`.
- With `PUZZLEEVAL_OBJECTIVE_VALIDATOR_ENABLED=0`, the coverage check is skipped and the plan goes straight to build (legacy compatibility).

### Fixture and Evaluation

- Agent 3 tests use canonical business fixture.
- Live tests do not contradict fixture facts.
- Rubrics do not demand success for impossible/off-menu scenarios.
- Judge failures are surfaced explicitly.

### Frontend and Reports

- Merged voice audio appears in final cards.
- Duplicate progress messages are deduped.
- All-failed reports have no winner.
- Efficiency summary renders without raw log inspection.

### Cleanup Regression

- Old pre-render cannot silently override new flow.
- Old Phase 2 directive cannot fire.
- Old build-plan directive remains disabled or deleted.
- Tool/file policies have one source of truth.
- `implement_test_env.py` no longer owns active policy.
- Stale tests are rewritten instead of preserved.

### Regression

- OCR, scraping, REST, code, and non-voice conversations still work.
- Existing frontend reports render.
- Audio merge/playback still works.
- Failure packets and runtime summaries still write on fatal errors.

## Handoff Instructions For Future AI Sessions

1. Start with Phase 0. Do not begin code migration before producing the policy inventory.
2. Work phase by phase. Do not create new artifacts without adding their owner, survival reason, and reader.
3. Every new phase ships behind a default-on `PUZZLEEVAL_*_ENABLED` flag. Wire the flag, log it under `runtime_state.json.migration_flags`, and add a regression test for the `0` branch in the same PR. No phase ships without its flag.
4. When replacing a policy, remove or shim the old one in the same phase. The replacement and the demotion happen together - never leave both control paths active without a flag selecting between them.
5. Rewrite stale tests. Do not keep tests that assert rejected architecture behavior.
6. Prefer small canonical modules over duplicating logic in `implement_test_env.py`.
7. Keep `objective.md` orchestrator-owned. Agent 5 interprets it in `implementation_plan.json`; Agent 5 never writes or weakens it.
8. Do not make Agent 4's lightweight metadata into build truth. `docs_entrypoint.json` is build authorization; `BuildReadinessChecklist` is telemetry.
9. Do not force all providers into a fixed adapter list. Runtime primitives are binary (`single_call` and `persistent_worker`). Streaming/duplex/session semantics live in `interaction_pattern.known_family` inside `implementation_plan.json`. If a future candidate genuinely needs an `async_job`/`webhook`/`browser_session`/`file_batch` runtime, that's a separate plan, not a quiet expansion of this one.
10. Mocks are the rollout blocker; real API smoke is opportunistic. Run real API smoke when credentials are staged. Skip without failing rollout when they aren't. Don't gate phase promotion on a flaky paid endpoint.
11. Use the abandon path when continuing would be dishonest: provider blocked, credentials unavailable, API incompatible with the use case, docs missing past the research budget, fixture/objective contradiction unfixable. Burning turns until budget exhaustion is itself a failure.
12. Playbooks are outcome contracts only. Do not add implementation code snippets, numbered implementation steps, specific timeout/retry recipes, or provider-named recipes back into a playbook during a fix. Provider-neutral schema fragments are allowed only when they define the contract shape. The grep/content tests will fail on HOW prose. Pretrained model knowledge handles HOW.
13. Implementation plans get a configurable revision budget (`PUZZLEEVAL_IMPLEMENTATION_PLAN_MAX_REVISIONS`, default 1). Tune it for the candidate class if needed; do not silently allow unlimited revisions.
14. Sunset rules are phase-based or outcome-based. Aspirational target dates exist for planning but missing one does not auto-remove anything. A missed target triggers a deliberate review, not a silent rollback.
15. Report artifact overhead and delete artifacts that do not earn their keep. The artifact-budget test (must drive action selection, acceptance, telemetry, or evidence; <=25% of build turns) applies to every new artifact this plan introduces.
16. Anything from "Out of Scope for v1" stays out of v1. If a real candidate forces the need, write a separate plan and a separate phase; don't smuggle it in under cleanup.

## Assumptions and Defaults

- Agent 4 verified docs are required for automatic Agent 5 build.
- Orchestrator owns immutable objective criteria.
- Agent 5 owns research strategy and implementation plan.
- Initial research uses planned parallel workers, not ad hoc `ask_research`.
- Contracts verify outcomes, evidence, and safety, not implementation recipes.
- `api_spec.txt` remains temporarily for compatibility but loses phase-gate authority.
- Cleanup is part of migration, not optional refactoring.
- Stale conflicting policy must be removed or made an explicit shim.
