# PuzzleEval â€” Architecture

> System design reference. For AI-assistant working notes (decisions,
> conventions, safety contracts) see `CLAUDE.md`.

## What it is

An AI-agent evaluation platform. Users describe what they need AI to do
in plain English; we find relevant AI services, build thin API-client
harnesses for them, run synthetic tests through each, and return a
verdict on **Performance Â· Speed Â· Price**. Target users: SMBs that
can't evaluate AI options themselves.

**V0 scope:** API-enabled providers only. Browser-automation paths
(Selenium/Playwright) are V1.

---

## The pipeline

Five sequential agents + a deterministic report assembler. Agents 2 + 3
run in parallel; Agent 5 fans out across N candidates.

```
USER INPUT (text + optional files)
        â”‚
        â–¼
   [Agent 1: User Understanding]      Parse intent â†’ WorkflowBlueprint
        â”‚
        â”œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
        â–¼              â–¼
   [Agent 2: Research]  [Agent 3 / 3F: Synthetic Tests]   â† PARALLEL
        â”‚                       â”‚
        â–¼                       â”‚
   [Agent 4: Screening]          â”‚
        â”‚                       â”‚
        â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                   â–¼
        [Agent 5: Build + Test + Evaluate Ã— N]   â† PARALLEL per candidate
                   â”‚
                   â–¼
        [report.py: EvaluationReport]   â† deterministic, no LLM
                   â”‚
                   â–¼
              UI / JSON
```

### Agent 1 â€” User Understanding

**Module:** `puzzleeval/agents/agent1/core.py` + `templates/system_prompt.md`

Parses natural language + optional uploaded files (PDF / DOCX / CSV /
images via Claude vision) into a structured `Agent1Result`:

- `sub_tasks[]` â€” each with `requires_test_files` boolean (drives 3 vs 3F routing)
- `domain`, `constraints` (technical_level, integration_requirements, monthly_volume)
- `workflow: WorkflowBlueprint | None` â€” DAG of steps (Phase 3 expansion)
- `is_clear: bool` â€” gates pipeline start

Conversational: max 4 turns. Asks clarifying questions until concrete
sub-tasks emerge.

### Agent 2 â€” Research

**Module:** `puzzleeval/agents/agent2/core.py`

Two-step stateless function. Step 1 uses Anthropic's server-side
`web_search` to find candidate AI services; Step 2 structures into
`Agent2Result` (5â€“7 candidates with `relevance_score`,
`adoption_difficulty`, `covers_step_ids`).

**Selection strategy:** Score â†’ Weight â†’ Rank. Each candidate scored on
3 dimensions (capability fit, adoption fit, use case fit), weighted by
user context. The `relevance_score` is the composite user-fit score â€”
NOT general capability rank.

**Dual-search mode** when blueprint has â‰¥2 scopes: one all-in-one
horizontal search PLUS per-scope specialist searches. Each candidate
declares `covers_step_ids` (which scopes it covers).

### Agent 3 / 3F â€” Synthetic Tests

**Modules:** `puzzleeval/agents/agent3/core.py` (text mode) and
`puzzleeval/agents/agent3f/core.py` (file mode)

Generate test cases with ground truth + weighted judgement criteria.
Routing:

- `requires_test_files=false` â†’ Agent 3 generates synthetic text inputs
- `requires_test_files=true` â†’ Agent 3F reads user files via Claude vision and creates one test case per file

Both produce `Agent3Result`. **Coverage matrix:** each sub-task tested
across 6 dimensions (`happy_path`, `input_variation`, `edge_case`,
`scale`, `domain_specific`, `error_resilience`).

For agentic conversational tests (voice, chatbot), test cases include
`persona`, `goal`, `constraints`, and a weighted `rubric` instead of a
static turn script. The `user_simulator.py` and `rubric_judge.py`
modules consume this.

### Agent 4 â€” Screening + Deep Verify

**Module:** `puzzleeval/agents/agent4/core.py` + `puzzleeval/deep_verify_runner.py`

Verifies each candidate has real, publicly accessible API access. N
parallel per-candidate verification calls (ThreadPoolExecutor) + 1
structuring call. Per-candidate isolation prevents the token-accumulation
explosion that broke earlier designs.

**Verification strategy:** progressive search-first, fetch only when
ambiguous. Search standard â†’ search capability-specific â†’ search
site-scoped â†’ fetch progressive (docs URL â†’ homepage â†’ most promising
link). 3 web searches + 3 web fetches per candidate (`max_uses=3`
each).


**Deep verify:** each candidate goes through a docs-entrypoint verification
loop that confirms the canonical landing page plus lightweight auth, access,
and pricing metadata. Agent 4 does not author the build plan; it verifies
whether the candidate is safe to hand to Agent 5.

**Evidence-based PASS rule:** when ANY evidence of an API exists, PASS
with notes. Only reject on definitively bad evidence (deprecated, no
public API, enterprise-only). Three rejection states: `Verified Pass`,
`Verified Reject`, `Inconclusive`. System failures (timeouts, parser
errors) NEVER reject â€” they pass through with a sentinel checklist.

### Agent 5 â€” Build + Test + Evaluate

**Module:** `puzzleeval/agents/agent5/build_loop.py` (orchestration spine)
+ siblings (sandbox, execution, evaluation, etc.)

For each validated candidate, builds a thin API-client harness and runs
all test cases through it.

**The harness contract:** `harness.run(input_data) -> raw_response`. The
harness sends input, returns the raw API response â€” it does NOT parse,
extract, or interpret. All intelligence is in the LLM judge.

**The flow per candidate (one agent, one context):**

1. **RESEARCH STRATEGY** (Opus lead). Read `objective.md`,
   `docs_entrypoint.json`, `test_case_manifest.json`, and candidate context.
   Write `_agent_state/research_plan.json` only when there are independent
   provider facts worth delegating.
2. **SCOPED RESEARCH WORKERS** (Sonnet workers). Planned tasks and
   scoped `ask_research` debug gaps answer concrete missing facts and persist
   durable findings. The lead consolidates them into `research_synthesis.json`
   and `research_build_brief.json`.
3. **IMPLEMENTATION PLAN** (Opus lead). Write
   `_agent_state/implementation_plan.json`, including objective coverage,
   chosen runtime primitive, actual input families, and representative probe
   strategy. This accepted plan is the build gate.
4. **BUILD** (Opus lead). Write `harness.py`, `requirements.txt`,
   `smoke_test.py`, and `live_test.py`; run smoke/live/self-checks; debug
   from failure packets, forensics, and scoped research when a missing fact
   would change the implementation.
5. **VALIDATE**. Run production-equivalent representative probes through the
   same evaluator/plugin adapter used by final evaluation, then run the full
   test suite with credentials injected into the sandbox.
4. **HARNESS_COMPLETE** signal â†’ verification gate (deterministic
   checks for `harness.py` + `live_test.py` existence and contract).
5. **POST-LOOP TEST EXECUTION** (Python, parallel across candidates).
   Run all `Agent3Result.test_cases` through `harness.run()`. Multi-call
   modalities (voice, chatbot) skip the pre-call entirely â€” the plugin
   owns every harness invocation.
6. **EVALUATION.** All criteria go to `evaluate_with_llm`. Raw API
   response (truncated at 15K chars) compared against ground truth +
   weighted judgement criteria. No mechanical eval (no `exact_match`,
   no `format_compliance`).

**Sub-modules under `agent5/`:**

| Module | Role |
|---|---|
| `build_loop.py` | The orchestration spine. ~1,700 LoC. Owns `BuildContext` + `BuildLoopState` dataclasses. |
| `api_call.py` | Anthropic API call boundary (retries, PTL recovery, rate-limit backoff, model fallback). |
| `dispatch_helpers.py` | Pure predicates + formatters: smoke detection, harness signal detection, phase transition trigger, error classification, reassessment message builder. |
| `turn_blocks.py` | Per-turn block iteration: orphan server-tool scrubber, turn_log dict builder, SSE progress emitter. |
| `sandbox.py` | venv creation, credential resolution, env var injection, test file staging. |
| `tools.py` | Custom tool implementations (write_file, patch_file, read_file, run_code) â€” local dispatch. |
| `prompts.py` | Builder system prompt loader + OS/modality placeholder rendering. |
| `playbooks.py` | Capability playbook composition (voice + streaming + live test) for the prompt. |
| `costing.py` | Per-turn cost calculation (token + cache + advisor + server-tool prices). |
| `verification.py` | Post-HARNESS_COMPLETE deterministic gate. |
| `research_subagent.py` | `ask_research` sub-agent for Phase 2+ debugging (NOT for Phase 1 initial discovery â€” gated). |
| `initial_message.py` | Builder's first user-message construction (atlas context, sandbox contents, prefetched docs). |
| `execution.py` | Post-loop test execution: per-test, session-retry, rate-limit retry, aggregate metrics. |
| `evaluation.py` | LLM judge: `evaluate_with_llm`, `build_evaluation_prompt`, `compute_weighted_score`. |
| `conversation_log.py` | Per-turn conversation log persistence. |

### Report assembly (post-pipeline)

**Module:** `puzzleeval/report.py`

Pure-data report generation â€” no LLM calls, no network. Produces an
`EvaluationReport`:

- Per-candidate ranking by overall score
- Per-scope winners
- Top 3 failure + top 3 success evidence rows per candidate
- Monthly cost projection (uses `pricing.py` + user's `monthly_volume`)
- Pros/cons heuristics
- Sandbox-disclosure flags
- Coverage-gap advisories

Persisted to `runs/<trace>/evaluation_report.json`, emitted as
`evaluation_report` SSE event, served at `GET /runs/{id}/report`.

---

## Module index

### Core

| Module | Role |
|---|---|
| `anthropic_client.py` | Central client factory. 120 s timeout, `max_retries=3`, Opusâ†’Sonnetâ†’Haiku fallback ladder via `call_with_model_fallback()`. Every agent builds its client here. |
| `config.py` | Every env-var configurable knob: model selection, effort tier, feature flags, timeouts, parallelism caps. |
| `schemas.py` | All agent boundary Pydantic contracts. Single source of truth â€” `validators.py` enums are referenced by `description=` fields to avoid drift. |
| `validators.py` | Canonical enums (`VALID_INPUT_TYPES`, `VALID_OUTPUT_TYPES`, etc.) + structural validation. |
| `exceptions.py` | `AgentRateLimitError`, `AgentAPIError`, `AgentOutputError`, `AgentFileParseError`. Each maps to a specific SSE failure event. |
| `structured_output.py` | `parse_with_fallback()` â€” strict-grammar path first, fall through to non-strict tool-call path on Anthropic's grammar-size 400. Defensive coercion of Python-repr array strings. Wired into all 6 structured-output call sites. |
| `budget.py` | `RunBudget` cost circuit breaker. Threadsafe. Hard USD cap (default $25 via `PUZZLEEVAL_MAX_RUN_COST_USD`). Raises `BudgetExceededError`. |
| `agent_preamble.py` | Shared system-prompt prefix for every agent. |
| `logging_setup.py` | Structured JSON logging with cost/latency/token extras. CloudWatch/Datadog-compatible. |
| `provider_registry.py` | Read-only accessor for `provider_registry.json` credential store. |
| `rate_limiter.py` | Per-provider concurrency + request-rate caps used in Agent 5's test execution pool. |
| `file_parsers.py` | PDF / DOCX / CSV / TXT / image parsing for Agent 1 and Agent 3F. |

### Pipeline orchestration

| Module | Role |
|---|---|
| `pipeline.py` | CLI-side pipeline orchestrator. Mirrors `puzzleeval-api/services/pipeline_runner.py` for standalone runs. |
| `cli.py` | `python -m puzzleeval.cli` entry point. Conversational Agent 1 loop + pipeline execution. |
| `selection.py` | Phase 7 per-scope top-K candidate selection (consumed by `puzzleeval-api/services/pipeline_runner.py` for SelectionPanel default picks). |
| `scope_routing.py` | Per-scope test routing. Maps tests to candidates via blueprint scope IDs. |

### Research & verification

| Module | Role |
|---|---|
| `deep_verify_runner.py` | Phase 6.5 per-candidate deep-verify loop (4Aâ†’4Bâ†’4Câ†’4D). |
| `deep_verify_prompt.py` | Deep-verify system prompt + per-candidate message builder. |
| `provider_atlas.py` | Structured atlas of a provider's API surface (endpoints, auth, rate limits, sandbox). Cross-run reusable. |
| `memdir.py` | Per-category cross-run memory at `~/.puzzleeval/memdir/<category>/<provider>.md`. 14-day TTL. Used for api_specs, quirks, atlas. |
| `manual_atlas.py` | Fallback structured-output atlas builder when Phase 6.5 can't auto-build one. |
| `openapi_harness.py` | When a provider publishes an OpenAPI spec, generate harness mechanically â€” skip Agent 5 reconstruction. |
| `web_fetch_fallback.py` | HTTPS GET when Anthropic's `web_fetch` returns nothing (CF/WAF, 5xx, content-uselessness). Two-stage classifier (HTTP errors + content-level uselessness). |
| `web_doc_cache.py` | Per-run cache of fetched docs to avoid re-fetching across candidates. |
| `adversarial_verifier.py` | Six principle-based probes (empty / max / malformed / idempotency / concurrency / auth_error). Pre-flight gate before Agent 3 cases run. |

### Accuracy & reporting

| Module | Role |
|---|---|
| `report.py` | Final `EvaluationReport` assembler (see above). |
| `pricing.py` | `estimate_monthly_cost(breakdown, monthly_volume)` â€” projects pricing against user's stated volume. |
| `test_data_sufficiency.py` | First-class verdict per scope: `READY` / `AUGMENT` / `SYNTHESIZE` / `REQUEST_MORE` / `DEGRADE`. Detects wrong-extension uploads, single-source variety risk. Emitted as `test_data_sufficiency` SSE event. |
| `modality.py` | Plugin dispatcher. Given `(input_type, output_type)`, queries the plugin registry. Fully data-driven. |
| `plugin_status.py` | `PLUGIN_WIRING` registry + `snapshot_plugin()` â€” where each plugin is wired (synthesis, evaluation, builder context) + per-credential advice when keys missing. |
| `plugin_tools.py` | `build_plugin_tool_definitions()` + `dispatch_plugin_tool()` â€” exposes plugins as Anthropic-callable tool schemas. |
| `plugin_tool_runner.py` | Claude-driven plugin dispatch via `tool_runner` (NEW-AA). Plugin-verdict promotion + direct-invoke owner fast path for multi-call modalities. |
| `hybrid_evaluator.py` | Opt-in (`PUZZLEEVAL_HYBRID_EVAL_ENABLED=1`) second-look when deterministic dispatch yields no plugin. |
| `rubric_judge.py` | Sonnet-based transcript-level scoring for agentic conversational tests. Critical-gate rubric semantics enforced deterministically. |
| `vision_judge.py` | Claude visionâ€“based scoring for image responses. |
| `user_simulator.py` | Haiku-driven user turns for agentic conversation tests (reactive, persona-aware). |

### Contracts (the runtime gate framework)

| Module | Role |
|---|---|
| `contracts/loader.py` | Loads playbooks from `capability_playbooks/*.md` with frontmatter. |
| `contracts/metadata.py` | Pydantic `ContractMetadata` schema. |
| `contracts/selector.py` + `selector_spec.py` | Tier-1 deterministic + Tier-2 LLM-routed playbook selection. |
| `contracts/task_context.py` | Frozen `TaskContext` (agent_id, phase, platform, test_cases, candidate). Hashable for `lru_cache`. |
| `contracts/coverage.py` | Per-task-type coverage requirements. Falsifiable contract for "right contracts loaded". |
| `contracts/runtime_gates.py` | `ALWAYS_ON_GATES` registry + base `ContractGate`. Per AD-007: gates are Python, not configurable from markdown. |
| `contracts/errors.py` | `ContractCoverageError`, `ContractLoadError`. |

### Telemetry

| Module | Role |
|---|---|
| `telemetry/cost.py` | Cost estimation per model + cache scenario. Canonical pricing tables. |
| `telemetry/budget.py` | `RunBudget` (the same circuit breaker, owned here). |
| `telemetry/timing.py` | `track_time` context manager. |
| `telemetry/logging.py` | Structured JSON log emitter with full context. |
| `telemetry/context.py` | `TelemetryContext(trace_id, agent_id, run_id, candidate_id)`. |
| `telemetry/pricing_tables.py` | Per-model pricing + min-cacheable-tokens. |

### Capability playbooks (markdown loaded at runtime)

`capability_playbooks/` contains markdown files loaded into Agent 5's
system prompt when test cases match the playbook's `selectors`.

| File | Purpose |
|---|---|
| `voice.md` | Voice harness return-shape contract (Shape A: inline bytes; Shape B: on-disk path). |
| `streaming_response.md` | Error-timeout + reset-on-event collection pattern for streaming APIs. |
| `live_test_voice.md` | Live test session-state contract for voice. |
| `platform_windows.md`, `platform_linux.md`, `platform_macos.md` | OS-specific shell + Python command guidance. |

These are **product behavior, not docs** â€” they get concatenated into
the LLM's system prompt at runtime via `importlib.resources`.

---

## Plugin ecosystem

The plugin architecture lets each modality plug in input synthesis +
output evaluation without touching Agent 5. Plugins auto-register at
import time via `puzzleeval/tool_plugins/__init__.py`; `modality.py`
dispatches by capability.

### The 8 plugins

| Plugin | Required credential(s) | Synthesizes | Evaluates | Role |
|---|---|:---:|:---:|---|
| `code_execution` | (none) | âœ“ | âœ“ | Runs generated code in sandbox; scores by exit code + output match |
| `vision` | `ANTHROPIC_API_KEY` | â€” | âœ“ | Scores image responses via Claude vision |
| `transcription` | `OPENAI_API_KEY` OR `DEEPGRAM_API_KEY` OR `ASSEMBLYAI_API_KEY` | â€” | âœ“ | STTs audio responses, scores transcript |
| `tts` | `OPENAI_API_KEY` OR `ELEVENLABS_API_KEY` | âœ“ | â€” | Synthesizes audio test inputs for voice agents (provider-failover chain) |
| `conversation_simulator` | (none) | âœ“ | âœ“ | Multi-turn scripted conversations with per-turn assertions |
| `webhook_receiver` | (none â€” local) | âœ“ | âœ“ | Captures inbound HTTP callbacks (Slack/Intercom/Stripe/Twilio/GitHub/generic). Binds 127.0.0.1:8765 lazily. |
| `outbound_delivery` | (none â€” local) | âœ“ | âœ“ | Mock SMTP (port 2525), Slack-webhook HTTP (8766), SMS HTTP (8767). |
| `voice_realtime` | (none directly; STT needs transcription key, TTS needs `OPENAI_API_KEY` or `ELEVENLABS_API_KEY`) | âœ“ | âœ“ | Local audio loopback. **Multi-turn**: owns `drive_conversation` (N caller+agent turns, per-turn substring scoring, MP3 merge with ID3 strip). Thread-local `session_dir` for parallel candidates. |

### Multi-call modality ownership

Multi-turn tests (voice, chatbot) are owned by a PLUGIN, not by Agent 5
directly. The plugin's `evaluate_output(response, expected, criteria,
harness_runner=...)` receives the harness runner callable and owns the
N-turn loop. The single-turn harness shape stays the same regardless
of modality. Three patterns are set in stone:

1. **Direct-invoke fast path** (`plugin_tool_runner.py`): when a plugin
   has `requires_harness_runner=True` AND its modality enums match the
   test's input/output types, it's invoked DIRECTLY before Claude's
   tool-picker runs. Eliminates the non-determinism observed in earlier
   voice runs where Claude sometimes picked the plugin and sometimes
   skipped it. Priority-sorted (most specific output_type match wins).
2. **Plugin verdict promotion** (`plugin_tool_runner.py`): when Claude's
   tool_runner finishes WITHOUT emitting a structured `ScoreVerdict`,
   the LAST conclusive plugin verdict is promoted directly instead of
   collapsing to LLM-judge single-turn fallback. Plugin scoring IS
   authoritative.
3. **Multi-call pre-call skip** (`agent5/execution.py`): test cases
   with `input_type âˆˆ {conversation, voice_conversation, voice_turn}`
   or `output_type âˆˆ {voice_turn, voice_conversation}` skip the initial
   single-turn `_execute_single_test` call. The plugin's drive-loop
   owns every real harness invocation.

### Voice audio stack (`voice_realtime.py`)

- `synthesize_input()` â†’ TTS caller audio via `tts` plugin chain,
  serves at `/audio/<token>` on an isolated local loopback port
  (`PUZZLEEVAL_VOICE_PORT` may pin one for operator-managed tunnels).
- `drive_conversation(script, agent_responder)` â†’ per-turn loop:
  synth caller â†’ invoke responder â†’ extract agent response â†’ score
  per-turn assertion.
- `_merge_conversation_audio(session_token, turns)` stitches caller
  + agent files into `conversation_<token>.<ext>` in dialogue order.
  Two paths: **pydub normalization** (primary) resamples to first
  segment's `frame_rate + channels`, exports uniformly-encoded MP3.
  **Byte-concat with ID3 strip** (fallback when pydub/ffmpeg
  unavailable) strips ID3v2 from segments 2â€¦N.

**Three harness return shapes supported:**

- **Shape A (inline bytes):** `raw_response.audio_bytes: <bytes>`.
- **Shape A-variant (base64 string):** `raw_response.audio_bytes: "<b64 str>"`.
  Decoded at the top of the pcm16 branch before pydub/wave touch it.
- **Shape B (on-disk file path):** `raw_response.audio_path: "/tmp/x.wav"`.
  Plugin reads the file, derives content_type from extension.

The contract is in `capability_playbooks/voice.md` â€” it's loaded into
the builder system prompt when voice test cases are present.

### Registry guards

`register_plugin()` warns on duplicate-name conflicts (or raises with
`PUZZLEEVAL_STRICT_PLUGIN_REGISTRY=1`). All plugin HTTP servers bind
`127.0.0.1` for security. Voice loopback uses an isolated port by default
so overlapping runs cannot consume each other's tokens. DoS caps: SMTP
per-line 8 KB / total DATA 25 MiB,
HTTP body 1 MiB.

---

## SSE event catalog

Every event the backend emits, and which UI component consumes it.

| Event | Payload | Consumer |
|---|---|---|
| `pipeline_started` | `{trace_id}` | sets `stage="pipeline"` |
| `workflow_blueprint` | `{workflow, test_plan}` | `WorkflowDiagram`, chat message |
| `test_data_sufficiency` | `{summary, verdicts[]}` | chat message â€” per-scope verdict |
| `agent_started` | `{agent, name}` | pipeline nodes + activity feed |
| `agent_activity` | `{agent, message, status}` | activity feed |
| `agent_thinking` | reserved | (subscribed; emission deferred) |
| `agent_completed` | `{agent, cost_usd}` | pipeline nodes |
| `agent_blocked` | `{agent, reason}` | billing-gate denial |
| `candidates_found` | `{candidates[]}` | `CandidateCard` + `CoverageMatrix` |
| `coverage_gap` | `{candidate_count, missing_scopes, user_message}` | chat warning |
| `selection_required` | `{run_id, per_scope_candidates, default_picks, total_candidates}` | `SelectionPanel` pause |
| `candidates_selected` | `{scope_picks, user_added}` | post-selection resume |
| `candidate_verified` | `{candidate, scope_id, ...}` | per-scope deep-verify result |
| `candidate_rejected` | `{candidate, reason}` | rejection ledger |
| `scope_verified_complete` | `{scope_id, verified, rejected}` | summary |
| `candidates_verified` | `{validated, rejected}` | Agent 4 complete |
| `test_cases_ready` | `{test_count}` | Agent 3 complete |
| `harness_started` | `{candidate}` | Agent 5 per-candidate |
| `harness_completed` | `{candidate, build_turns, build_cost_usd, success}` | Agent 5 per-candidate |
| `harness_failed` | `{candidate, reason}` | Agent 5 per-candidate |
| `test_execution_started` | `{candidate}` | Agent 5 |
| `test_result` | `{candidate, test_case_id, passed, score}` | Agent 5 per-test |
| `candidate_results_ready` | `{candidate, scores}` | Agent 5 per-candidate |
| `cost_update` | `{source, delta_usd, total_cost_usd, budget: {spent_usd, cap_usd, remaining_usd, utilization}}` | live cost meter |
| `report_generating` | `{}` | "Generating evaluation report..." |
| `evaluation_report` | full `EvaluationReport` dict | `EvaluationReportCard` |
| `pipeline_completed` | `{total_cost_usd, budget, summary}` | sets `stage="results"` |
| `pipeline_failed` | `{error, reason?, spent_usd?, cap_usd?, recovery?}` | error banner; `reason="budget_exceeded"` gets special UI |
| `pipeline_cancelled` | `{}` | cancel confirmation |
| `done` | `{}` | closes SSE stream |

---

## Resilience infrastructure

### `.env` autoload (`puzzleeval/__init__.py:_autoload_dotenv`)

Walks from CWD + package location to find `.env` or
`puzzleeval-api/.env`. **Empty-string shadow defense:** if a key in the
`.env` is already in `os.environ` as empty/whitespace, that entry is
evicted before `load_dotenv` runs â€” so `ANTHROPIC_API_KEY=` (CI
placeholder) doesn't silently shadow the real value.

### Provider-registry â†’ environ propagation

`ProviderRegistry.iter_env_vars()` + `sync_to_environ(override=False)`
propagate registry credentials into `os.environ`. Auto-runs at package
init. Single source of truth for credentials â€” `.env` optional.

### Structured-output fallback

`structured_output.parse_with_fallback()` â€” strict path
(`client.messages.parse(output_format=...)`) first, fall through to
`client.messages.create()` non-strict tool on Anthropic's
"compiled grammar too large" 400. Defensive coercion of Python-repr
arrays + over-nested `{"input": ...}`. Wired into Agents 1, 2, 3, 3F,
4, and 5's LLM evaluator.

`prefer_non_strict=True` available for callers that know the schema
won't fit (Agent 2, Agent 4) â€” skips the doomed strict attempt.

### Budget circuit breaker

Every cost-recording site (agent completions, Agent 1 chat turns,
aggregate) calls `state.record_cost(usd, reason)`. Crosses the cap â†’
`BudgetExceededError`. Chat endpoint: HTTP 402. Pipeline runner:
`pipeline_failed reason="budget_exceeded"` with spent/cap/recovery hint.

### Plugin lifecycle (`puzzleeval-api/main.py:lifespan`)

On startup: touches the plugin registry so every plugin imports/registers.
On shutdown: calls `shutdown()` on every plugin that exposes one â€” HTTP
and SMTP servers close cleanly across `uvicorn --reload`.

### Upload + SMTP DoS caps

- `routes/files.py:_read_with_cap` â€” stream-reads with
  `MAX_UPLOAD_BYTES_PER_FILE` (100 MiB default), aborts with HTTP 413.
- `outbound_delivery.py` â€” `SMTP_MAX_LINE_BYTES` (8 KB) +
  `SMTP_MAX_DATA_BYTES` (25 MiB).
- `webhook_receiver.py` â€” `HTTP_MAX_BODY_BYTES` (1 MiB).

### Path traversal protection

`POST /files` strips directory components via `PurePosixPath/PureWindowsPath.name`,
scrubs shell-metachars, verifies `dest.resolve()` stays inside
`run_upload_dir.resolve()` before writing.

### Frontend SSE auto-reconnect

`src/services/api.ts:subscribeToEvents` wraps EventSource with
exponential backoff (1 s â†’ 2 s â†’ ... â†’ 30 s cap), preserves
`lastEventId` across reconnects, surfaces `connecting / open /
reconnecting / closed` to the UI. "No progress for Xm" banner in
`Playground.tsx` when `stage === "pipeline"` and
`Date.now() - lastEventAt > 2 min`.

### Path resolution for backend audio streaming

`GET /runs/audio?path=<absolute_path>` (in `puzzleeval-api/routes/runs.py`)
streams audio with a containment check. Allowed roots:

1. `puzzleeval-api/runs/` (backend-driven pipeline runs)
2. `PuzzleEval-local/runs/` (CLI-driven dev runs â€” auto-detected)
3. Any comma-separated path in `PUZZLEEVAL_EXTRA_RUNS_ROOTS` env var.

Containment via `Path.resolve().relative_to(root)` â€” no traversal holes.

---

## Frontend integration

| Component (path under `src/`) | Role |
|---|---|
| `pages/Playground.tsx` | Top-level page: chat panel + pipeline stage indicator + results |
| `hooks/usePipelineRun.ts` | SSE subscription + state machine (`stage = conversation | pipeline | selection | results`) |
| `services/api.ts` | REST + SSE client. `subscribeToEvents` with auto-reconnect |
| `components/playground/WorkflowDiagram.tsx` | Renders Agent 1's `WorkflowBlueprint` as a topological-layer DAG |
| `components/playground/CandidateCard.tsx` | Per-candidate summary with `<PricingBlock>` when populated |
| `components/playground/CoverageMatrix.tsx` | Candidates Ã— scopes table when blueprint has â‰¥2 scopes |
| `components/playground/SelectionPanel.tsx` | Per-scope keep/remove checkboxes + "Add custom provider" form |
| `components/playground/EvaluationReportCard.tsx` | Final report with `RubricBreakdownBlock`, `AudioPathsBlock`, `<PricingBlock>` |
| `components/playground/QuotaBadge.tsx` | Header badge polling `GET /runs/{id}` for credits + plan status |
| `components/playground/ResultsComparison.tsx` | Per-scope candidate tables for multi-scope workflows |
| `components/playground/RejectionSummary.tsx` | Per-scope rejection reasons (Phase 6.5 deep verify) |

Stage transitions (`hooks/usePipelineRun.ts`):
`conversation` (Agent 1 chatting) â†’ `pipeline` (Agents 2â€“4 running) â†’
`selection` (SelectionPanel pause) â†’ `pipeline` (Agents 4 deep verify
+ 5 build/test) â†’ `results` (`EvaluationReportCard`).

Rendering on all-rejected runs: `Playground.tsx` renders unconditionally
at `stage === "results"` with empty-state banner â€” rejections still
visible, advisories still shown.

---

## Project layout

```
ai-agent-navigator/                     # repo root (NOT a Python package)
â”œâ”€â”€ src/                                # React/TypeScript frontend
â”‚   â”œâ”€â”€ pages/, components/, hooks/, services/, types/
â”œâ”€â”€ puzzleeval-api/                     # FastAPI backend
â”‚   â”œâ”€â”€ main.py                         # app + lifespan
â”‚   â”œâ”€â”€ routes/                         # REST + SSE endpoints
â”‚   â”œâ”€â”€ services/                       # pipeline_runner, run_manager, billing, event_bus
â”‚   â”œâ”€â”€ models/                         # API Pydantic models
â”‚   â””â”€â”€ tests/                          # 37 backend tests
â””â”€â”€ PuzzleEval-local/                   # Python package + tests + scripts
    â”œâ”€â”€ ARCHITECTURE.md                 # this file
    â”œâ”€â”€ CLAUDE.md                       # AI-assistant working notes
    â”œâ”€â”€ pyproject.toml
    â”œâ”€â”€ puzzleeval/                     # the Python package (~41K LoC)
    â”‚   â”œâ”€â”€ agents/                     # 5 agent packages + legacy shims
    â”‚   â”‚   â”œâ”€â”€ agent1/, agent2/, agent3/, agent3f/, agent4/
    â”‚   â”‚   â”‚   â””â”€â”€ core.py + templates/*.md
    â”‚   â”‚   â”œâ”€â”€ agent5/                 # 14 modules + templates/
    â”‚   â”‚   â”œâ”€â”€ implement_test_env.py   # forwarding shims for back-compat
    â”‚   â”‚   â””â”€â”€ (legacy single-file shims for back-compat)
    â”‚   â”œâ”€â”€ tool_plugins/               # 8 plugins
    â”‚   â”œâ”€â”€ contracts/                  # runtime gate framework
    â”‚   â”œâ”€â”€ telemetry/                  # cost / budget / logging / timing
    â”‚   â”œâ”€â”€ capability_playbooks/       # runtime LLM-prompt content
    â”‚   â””â”€â”€ (other modules â€” see Module index)
    â”œâ”€â”€ tests/                          # 1,744 unit tests
    â”œâ”€â”€ scripts/                        # mock_pipeline_direct.py, preflight_check.py, real_api_smoke.py
    â””â”€â”€ runs/                           # gitignored pipeline outputs
```

---

## Tool-version pinning

| Tool | Version | Rationale |
|---|---|---|
| `web_fetch` | `20250910` | Reverted from 20260209 after real-run failures (container_id cascades, sandbox latency, non-beta hangs). See AD-008 in CLAUDE.md. |
| `web_search` | `20250305` | Same revert. |
| `code_execution` | `20260120` | Required in `_build_tools_with_programmatic` â€” basic web tools don't auto-inject. |
| `advisor` | `20260301` | Opus-backed advisor called by Sonnet executor. |
| `tool_search_tool_bm25` | `20251119` | Tool discovery at scale. |
| `clear_tool_uses` | `20250919` | Server-side context-mgmt edit at 80K tokens. |
| `compact` | `20260112` | Server-side summarize at 150K tokens. |
| `clear_thinking` | `20251015` | Prunes accumulated extended-thinking blocks. Fires before clear_tool_uses. |
