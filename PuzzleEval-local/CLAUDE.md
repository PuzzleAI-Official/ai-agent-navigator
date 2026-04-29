# PuzzleEval — Project Context for AI Assistants

> Read this FIRST. This file is for AI assistants working in this
> codebase: how to read it, how to make changes safely, what
> architectural decisions are load-bearing, and what's known to be
> broken or rough.
>
> **For system design** (the agents, the modules, the SSE catalog,
> data flow) → `ARCHITECTURE.md`.
>
> **For Agent 5 deep-dive** → `puzzleeval/agents/agent5/ARCHITECTURE.md`.
>
> **For backend / FastAPI specifics** → `puzzleeval-api/BACKEND_ARCHITECTURE.md`.
>
> **For security model + cloud-deferred items** → `SECURITY.md`.

---

## What this file is and is not (read this BEFORE editing it)

This file is the **AI-assistant orientation guide** for the PuzzleEval
codebase. Future AI sessions read this to understand current state,
conventions, and load-bearing decisions before touching code. The bar
for what belongs here is *"would a competent engineer joining the
project today need this to avoid breaking something?"*

**This file IS:**
- A map of where things live (file purpose, directory roles).
- A list of durable architectural decisions (AD-XXX entries — additive over time, but additions must be real *decisions*, not tactical fixes).
- A list of active deterministic safety contracts (the AD-007 enforcement table — concrete, greppable, current).
- A short list of real outstanding gaps (OT-XXX — only what's actually broken or rough today, not historical or speculative).
- The conventions for testing + diagnostic flags + how to ship changes safely.

**This file is NOT:**
- A changelog. Past sessions' rationale belongs in git commit messages and PR descriptions. **Never add "NEW-XX" or "Phase N" session-by-session change blocks.**
- A trace ledger. Real-run trace IDs from specific sessions are evidence in commits, not durable context.
- A speculation board. Don't add "we might want to..." or "consider X someday." If it's not real and current, it doesn't belong.
- A duplicate of `ARCHITECTURE.md`. System design lives there. This file is *how to work in the codebase*, not *what the codebase is*.
- A duplicate of agent prompts or markdown playbooks. Those files own their own contracts.

**When updating this file, the rules are:**

1. **Replace, don't append.** If a section becomes wrong, edit it in place. Don't add a "(updated 2026-XX-XX: now actually works like Y)" note next to a stale paragraph.
2. **Each line earns its place.** If you can delete a sentence and a future engineer wouldn't miss it, delete it. Terse over thorough; specific over vague.
3. **Cite by file:line, not by session.** "`agent5/build_loop.py:1217`" is durable. "`NEW-AI capability fix 2`" is not.
4. **Add an AD-XXX only when a real architectural decision is made.** Decisions have alternatives weighed and a revisit trigger. Tactical fixes go in code comments + tests.
5. **Add an OT-XXX only when something is genuinely broken or rough today.** Remove the entry when it's fixed — don't leave a "FIXED in NEW-Z" stub.
6. **Don't rename or restructure existing AD-XXX / OT-XXX numbers.** Future readers grep for them; numbers are stable identifiers.
7. **If you're tempted to add a section called "Recent changes" / "Lessons learned" / "Open questions" / "Things to consider" — DON'T.** That's how this file became 6,000 lines of changelog last time. Git history is the changelog. PR descriptions are the lessons.

The file should stay under ~700 lines. If you're about to push past that, something needs to be deleted, not added.

---

## What PuzzleEval is

An AI-agent evaluation platform. Users describe what they need AI to do
in plain English. We find relevant AI services, build thin API-client
harnesses for them, run synthetic tests through each, and return a
verdict on **Performance · Speed · Price** — nothing else.

Target users: SMBs (small/medium businesses) overwhelmed by AI options,
without the technical ability to evaluate them.

V0 scope: API-enabled providers only. V1: browser-automation paths.

---

## Where to find things

Top-level layout:

```
ai-agent-navigator/                # repo root (NOT a Python package)
├── src/                           # React/TypeScript frontend
├── puzzleeval-api/                # FastAPI backend
└── PuzzleEval-local/
    ├── puzzleeval/                # the Python package (~41K LoC)
    ├── tests/                     # 1,744 unit tests
    ├── scripts/                   # diagnostic + smoke-test scripts
    └── runs/                      # gitignored pipeline outputs
```

Tech stack: Python 3.11+, Pydantic v2, Anthropic SDK (Opus 4.7 / Sonnet
4.6 / Haiku 4.5), structured JSON logging, FastAPI + SSE, React + Vite.

### `puzzleeval/agents/` — the 5 agents + Agent 5 internals

Each user-facing agent lives in its own package with `core.py` (Python
logic) + `templates/*.md` (prompts). Agent 5 has 14 sub-modules because
its build loop is the largest piece of orchestration in the codebase.
Five legacy single-file paths exist as back-compat shims (AD-010).

| Path | Purpose |
|---|---|
| `agent1/core.py` + `templates/system_prompt.md` | Conversational user-understanding agent. Parses natural language + uploaded files (PDF / DOCX / CSV / image via Claude vision) into `Agent1Result` (sub-tasks, domain, constraints, optional `WorkflowBlueprint`). |
| `agent2/core.py` + `templates/research_system.md` + `structure_system.md` | Two-step research agent. Step 1 uses server-side `web_search` to find candidates; Step 2 structures them into `Agent2Result` with `relevance_score`, `adoption_difficulty`, `covers_step_ids`. |
| `agent3/core.py` + `templates/system_prompt.md` | Synthetic-test generator (text mode). Creates test cases with ground truth + weighted judgement criteria across 6 coverage dimensions. |
| `agent3f/core.py` + `templates/system_prompt.md` | Synthetic-test generator (file mode). One test case per user-uploaded file; ground truth extracted from file content via Claude vision. |
| `agent4/core.py` + `templates/verification_system.md` + `structure_system.md` | Per-candidate API verification + Phase 6.5 deep verify. Produces `Agent4Result` with `validated_candidates`, `rejected_candidates`, `BuildReadinessChecklist` per candidate. |
| `implement_test_env.py` | Back-compat forwarding shims for Agent 5 (4,708 LoC, mostly delegation to `agent5/*`). Direct imports from this file should resolve to canonical homes; new code should NOT add to it. |
| `user_understanding.py`, `research.py`, `synthetic_tests.py`, `synthetic_tests_file.py`, `screening.py` | Legacy single-file paths (24-line shims). Each mirrors EVERY public + private attribute from the canonical `agent<N>/core.py`. Sunset-dated per AD-010. |

**Inside `agent5/` (the build loop):**

| File | Purpose |
|---|---|
| `__init__.py` | Public API surface for the agent5 package. |
| `build_loop.py` | The orchestration spine (~1,700 LoC). Owns `BuildContext` (frozen) + `BuildLoopState` (mutable) dataclasses + `build_single_harness(ctx)` entry point. The autonomous tool-use loop that drives ONE candidate's harness build. |
| `api_call.py` | Anthropic API call boundary. Retry-with-backoff, prompt-too-long (PTL) recovery via context compaction, rate-limit backoff, model fallback (Opus → Sonnet → Haiku). `BuilderAPICallContext` frozen dataclass; `APICallOutcome` tagged union (Success/Failure). |
| `dispatch_helpers.py` | Pure predicates + formatters used by the dispatch loop: `detect_smoke_pass`, `detect_harness_signal`, `detect_phase_transition`, `detect_tool_result_error`, `classify_tool_result_error`, `should_inject_reassessment`, `build_reassessment_message`, `enrich_research_question`. Pure functions, no I/O — each one is testable in isolation. |
| `turn_blocks.py` | Per-turn response-block iteration. Strips orphan server tool_use blocks (max_tokens truncation defense), builds the per-turn `turn_log` dict, emits the SSE `agent_activity` build_turn payload. |
| `sandbox.py` | venv creation + management (per-sandbox lock for race-safety, `python -m venv` with pip-bootstrap fallback for Windows+anaconda), `VENV_PREINSTALL_MANIFEST` (6 packages every harness needs), credential resolution from `provider_registry.json` (cross-provider union by name substring), env-var injection, test-file staging. |
| `tools.py` | Local-dispatch implementations of the custom tools the builder calls: `write_file`, `patch_file` (string-replace editing), `read_file`, `run_code` (subprocess with timeout + venv isolation). Returns `(result_text, exit_code)` tuples for is_error classification. |
| `prompts.py` | Builder system prompt loader from `templates/builder_system_prompt.md` + placeholder rendering: `__OS_TYPE__`, `__OS_SPECIFIC_RULES__`, `__CONTRACT_BLOCK__`. Single render entry point used by both real builds and tests. |
| `playbooks.py` | Capability playbook composition. Reads `capability_playbooks/*.md` via the contracts loader; composes the conditional contract block injected into the system prompt for THIS test case's modalities. |
| `costing.py` | Per-turn cost calculation: input + output tokens, cache create/read, advisor server-tool, web_search/web_fetch server-tool. Pricing tables sourced from `puzzleeval/telemetry/pricing_tables.py`. |
| `verification.py` | Post-`HARNESS_COMPLETE` deterministic gate. Confirms `harness.py` exists and has the required contract (`run(input_data)` callable, returns the canonical 7-key dict). |
| `research_subagent.py` | The `ask_research` sub-agent for Phase 2+ debugging only. Spawns a fresh research context (separate from builder) with web_search + web_fetch. **Phase 1 gate:** refuses with a redirect message when `api_spec.txt` doesn't exist yet — forces builder to do its own primary discovery. |
| `initial_message.py` | Builder's first user message. Composes: atlas context (Agent 4's `BuildReadinessChecklist`), sandbox contents listing, prefetched docs preview, scope-role hints, modality context, FAST-PATH instructions when checklist is complete. |
| `execution.py` | Post-build test execution (~830 LoC). `execute_all_tests`, `execute_single_test` (subprocess driver with bytes round-trip via b64 sentinels), `execute_test_with_session_retry`, `run_single_test_with_rate_limit`, `compute_aggregate_metrics`. Multi-call modality pre-call skip lives here. |
| `evaluation.py` | LLM judge for test results (~350 LoC). `evaluate_with_llm` (Sonnet with adaptive thinking, raw API response truncated to 15K chars), `build_evaluation_prompt`, `evaluate_mechanical` (legacy fallback path; not used in current flow), `compute_weighted_score`. |
| `conversation_log.py` | Per-turn `conversation_log.json` persistence. Save-incremental-after-every-turn so operators can `cat conversation_log.json` mid-build for debugging. |
| `templates/builder_system_prompt.md` | Agent 5 builder system prompt — the load-bearing 55KB prompt loaded once at module init (memoized via `lru_cache`). |
| `ARCHITECTURE.md` | Agent 5 internals deep-dive (separate from the top-level ARCHITECTURE.md). |

### `puzzleeval/tool_plugins/` — the 8 modality plugins

All plugins auto-register at import time via the package `__init__.py`.
The dispatcher in `puzzleeval/modality.py` queries the registry by
`(input_type, output_type)` enums.

| File | Purpose |
|---|---|
| `code_execution.py` | Sandboxed runner for code-generation candidates. Synthesizes test inputs (e.g., FizzBuzz seeds); evaluates by exit code + output match. Languages: Python / JS / TS / Go / Rust / Bash. |
| `vision.py` | Wraps `puzzleeval/vision_judge.py` as a registered plugin. Scores image responses against expected output via Claude vision. Evaluation only. |
| `transcription.py` | Speech-to-text for voice/audio response evaluation. Provider chain: OpenAI Whisper → Deepgram → AssemblyAI (first credentialed wins). Evaluation only. |
| `tts.py` | Text-to-speech for voice agent test inputs. Provider chain: OpenAI TTS → ElevenLabs (failover on 429 / 401 / 5xx). Synthesis only. |
| `conversation_simulator.py` | Multi-turn scripted conversation runner for chatbot / inbound agents. Replays user-turn scripts with per-turn assertions; adapts to message / conversation / history payload shapes. Both synthesis (default scripts) and evaluation. Owns its own N-turn loop (`requires_harness_runner=True`). |
| `voice_realtime.py` | Local audio loopback for voice/phone agents. Synthesizes caller audio (TTS), exposes at `/audio/<token>` on 127.0.0.1:8768, drives N-turn conversations through the harness, scores per-turn substring assertions, merges per-turn audio into a `conversation_<token>.mp3` (pydub normalization primary; byte-concat with ID3-strip fallback). Thread-local `_session_dir` keeps parallel candidates isolated (AD-004). Owns the multi-turn loop (`requires_harness_runner=True`). |
| `webhook_receiver.py` | Inbound HTTP capture for webhook agents (Slack `app_mention`, Intercom widget, Stripe events, Twilio SMS, GitHub webhooks, generic). Binds 127.0.0.1:8765 lazily, returns provider-shaped JSON acks, tracks captured POSTs by token. Both synthesis (callback URL + envelope) and evaluation (substring match against captured payload). |
| `outbound_delivery.py` | Mock receivers for outbound message senders. SMTP (port 2525, RFC 5321 line-by-line state machine), Slack-shaped HTTP (port 8766), Twilio-shaped form-encoded SMS HTTP (port 8767). Verifies messages actually landed in the intended channel — not just that the API returned 200. DoS caps: 8 KB per line / 25 MiB total DATA / 1 MiB per HTTP body. |

**Plugin registry contract** (in `tool_plugins/__init__.py`):
- Re-registering the SAME instance under an existing name is a no-op (idempotent).
- Registering a DIFFERENT instance under an existing name logs a warning, OR raises with `PUZZLEEVAL_STRICT_PLUGIN_REGISTRY=1`.
- All HTTP servers bind `127.0.0.1` (not `0.0.0.0`) and set `allow_reuse_address=True`.

### `puzzleeval/contracts/` — runtime gate framework

The contract framework loads markdown playbooks, selects which apply
to a given task, validates coverage, and detects conflicts. Per AD-007,
runtime gates are Python-only — markdown frontmatter is descriptive
metadata, not control.

| File | Purpose |
|---|---|
| `__init__.py` | Public API surface: `TaskContext`, `select_contracts_for_task`, `compose_contract_block`. |
| `task_context.py` | `TaskContext` frozen dataclass — the typed input to selection. Carries `agent_id`, `phase`, `platform`, `test_cases`, `candidate`. Hashable for `lru_cache`. Strongly typed so contracts can declare predicates against KNOWN field names. |
| `selector_spec.py` | `SelectorSpec` Pydantic model — typed predicate against `TaskContext`. `matches(spec, task) -> bool` is a pure function: same inputs always produce the same boolean. No I/O. |
| `selector.py` | The 4-layer selection algorithm: (1) Always-On, (2) Deterministic predicate match, (3) Coverage validation, (4) Conflict detection + resolution by priority. Each layer has a falsifiable property (soundness / completeness / consistency / determinism). |
| `loader.py` | Walks `capability_playbooks/*.md` at first call (memoized via `lru_cache`). Parses YAML frontmatter, validates against `ContractMetadata` schema, builds `Contract` objects. Hot-reload bypass via `PUZZLEEVAL_PLAYBOOK_HOT_RELOAD=1` for dev. |
| `metadata.py` | `ContractMetadata` Pydantic schema — the data contract between playbook authors and the runtime. Fields: `id`, `version`, `title`, `category` (modality / platform / interaction), `description`, `selectors`, `criticality` (`required` | `optional`), `priority`, `applies_to_agents`, `paired_gates`, `mutually_exclusive_with`. |
| `coverage.py` | Per-task-type coverage requirements. Falsifiable contract for "right contracts loaded": after Layer 2 runs, Layer 3 walks `COVERAGE_REQUIREMENTS` and asserts every required contract appears in the selected set. Strict mode raises `ContractCoverageError`; default mode logs a `contract_coverage_gap` event. |
| `runtime_gates.py` | THE source of truth for which gates run at runtime. `ALWAYS_ON_GATES` registry + base `ContractGate` class. **AD-007 boundary:** the `paired_gates` field in markdown is descriptive only — it does NOT control which gates execute. Adding a new gate = add a class here. |
| `errors.py` | `ContractError` base + `ContractLoadError` (frontmatter parse/validation failures, hard-error at startup) + `ContractCoverageError` (strict-mode coverage gap). |

### `puzzleeval/capability_playbooks/` — runtime LLM prompt content

These are MARKDOWN files but **product behavior, not docs.** They get
concatenated into Agent 5's system prompt at runtime via
`importlib.resources`. Each file has YAML frontmatter (parsed by
`contracts/metadata.py`) + a markdown body (the actual prompt content).

**Do NOT comment-clean these files.** Treat them as code.

| File | When loaded into the prompt |
|---|---|
| `voice.md` | Test cases include `voice_conversation`, `voice_turn`, or `audio_content`. Teaches the harness builder the two valid `raw_response` return shapes (Shape A: inline `audio_bytes`; Shape B: on-disk `audio_path`). Forbids alternate keys. Paired with `VoiceHarnessGate` runtime gate. |
| `streaming_response.md` | Test cases include `voice_conversation`, `voice_turn`, `audio_content`, `conversation`, or `code` (any streaming-shape modality). Teaches the canonical error-timeout + reset-on-event collection pattern for streaming responses (WebSocket events, SSE chunks, audio chunks, polled job results). Anti-bandaid: principle-based, no provider-specific names or magic timeout numbers. |
| `live_test_voice.md` | Test cases include `voice_conversation`, `voice_turn`, or `conversation`. Teaches the harness builder the live-test session-state contract: drive 2+ turns through the EXACT production payload shape (`audio_url`, `turn_index`, `session_state`, `input_context`). Skipping causes silent production failures smoke tests don't catch. |
| `platform_windows.md` | `sys.platform == "win32"`. Always-on for Windows builds. Unix→Windows command translations (no `tail`, `head`, piped `grep`, `&`, `&&`); the `python -c` silent-output trap warning. |
| `platform_linux.md` | `sys.platform == "linux"`. Always-on for Linux builds. Standard POSIX guidance (minimal — most builders already know GNU coreutils). |
| `platform_macos.md` | `sys.platform == "darwin"`. Always-on for macOS builds. BSD vs GNU flag-syntax differences (`sed -i ''`, `grep -P` not portable). |

---

## How the codebase is organized

**The pipeline (5 agents + report assembler).** See `ARCHITECTURE.md` §
"The pipeline" for the full data flow. Short version:

1. **Agent 1** parses user intent → `WorkflowBlueprint`.
2. **Agent 2** finds candidates via web search → 5–7 candidates.
3. **Agent 3 / 3F** generates synthetic test cases (text or file mode).
4. **Agent 4** verifies API access + builds a `BuildReadinessChecklist`.
5. **Agent 5** builds a thin API-client harness per candidate, runs all
   tests, evaluates with an LLM judge.
6. **`report.py`** assembles the deterministic `EvaluationReport`.

Agents 2 and 3 run in parallel. Agent 5 fans out across N candidates.

**Each agent lives in `puzzleeval/agents/agent<N>/`** with `core.py` +
`templates/*.md`. Agent 5 is bigger and has 14 sub-modules under
`agent5/`. The 5 legacy single-file paths (`research.py`, `screening.py`,
etc.) are back-compat shims — see AD-010 below.

**Plugins** (`puzzleeval/tool_plugins/*.py`) auto-register at import
time. The dispatcher in `puzzleeval/modality.py` routes test cases to
plugins by `(input_type, output_type)` enums — no hardcoded
`if scope_role == X` branches anywhere.

---

## Architectural Decisions

These are durable. If you find yourself about to contradict one, pause
and check whether the conditions that motivated the decision have
changed.

### AD-001: Stay unified — no per-modality pipeline fork

One agent pipeline handles every modality (voice, OCR, vision, code-gen,
webhook, streaming, chatbot-text). Do NOT fork into "voice pipeline /
vision pipeline" each with its own Agent 1–5.

**Why.** Agents 1–4 are 100% modality-agnostic (they operate on enum
fields and `WorkflowBlueprint` shape). Forking would 4× the prompt
maintenance with zero gain for those agents. Real users describe
mixed-modality problems ("OCR invoices THEN call the vendor") that a
forked architecture would force them to split — breaking the product's
core value. Cross-modality comparison (a product requirement) becomes
impossible with forked pipelines.

**Revisit if:** a modality emerges with fundamentally different Agent
1→2 semantics or scoring philosophy plugins can't carry, OR a user
contract differs at the surface above agents (e.g., HIPAA-audited
healthcare).

### AD-002: Agent 5 uses conditional in-prompt gating

Agent 5's builder prompt stays in `agent5/templates/builder_system_prompt.md`
with conditional injection for modality-specific sections (driven by
`agent5/playbooks.py` + `capability_playbooks/*.md`). Do NOT migrate to
native Anthropic Agent Skills, do NOT build a generic playbook-router
on top of what's there.

**Why.** Three options were evaluated:

| Option | Verdict |
|---|---|
| (A) Native Agent Skills | Rejected — requires `container` + code-execution beta. User API keys would flow through Anthropic's sandbox. Incompatible with our local-subprocess credential model. |
| (B) Playbook files + deterministic router | Already partially built (`contracts/` + `capability_playbooks/`). Sufficient for current scope. |
| (C) Conditional in-prompt gating | The pattern actually used — extends the existing `MULTI-CALL HARNESS CONTRACT` injection. Minimal infrastructure. |

**Revisit (B → A) if:** PuzzleEval pivots to SaaS (credentials flow
through our infrastructure anyway), OR Anthropic ships a "skills as
system-prompt injection" mode without `container`.

### AD-003: Plugins own modality-specific orchestration

Any modality that needs N harness invocations per test (multi-turn
voice, multi-turn chatbot, streaming with re-subscribe) is owned by a
PLUGIN with `requires_harness_runner=True`. The plugin drives its own
N-turn loop; the harness stays single-turn.

**Why.** Keeps Agent 5's builder prompt + harness shape uniform across
modalities. Adding a new multi-call modality is one plugin file
(self-contained) — NOT a prompt overhaul. `tool_plugins/voice_realtime.py`
is the reference implementation. See `ARCHITECTURE.md` §
"Multi-call modality ownership" for the three patterns this enables
(direct-invoke fast path, plugin verdict promotion, multi-call pre-call
skip).

### AD-004: Audio artifacts live under the run directory, not %TEMP%

Every audio file (caller MP3, agent MP3, merged conversation) lives
under `runs/<trace_id>/harnesses/<candidate_slug>/voice/`.
`tool_plugins/voice_realtime.py` keeps `_session_dir` thread-local so
parallel candidates write to their own folders.

**Why.** The backend audio-streaming route (`/runs/audio?path=...`) has
a containment check — without per-run paths, the frontend silently
fails. User expectation is "every run is a self-contained directory I
can zip and share."

**Cloud-scale seam:** `set_session_dir()` accepts any Path-compatible
object. Swap in an S3/GCS Path-shim for cloud without touching call sites.

### AD-005: Skills and MCP are NOT interchangeable

Skills are for PROCEDURAL KNOWLEDGE (markdown guidance, optional bundled
scripts). MCP is for TOOL/SERVICE INTERFACES (function-call RPCs).
Orthogonal. When adding a new capability, route as follows:

| Need | Answer |
|---|---|
| Teach the agent a contract (how to call X API family) | Conditional in-prompt section (AD-002) |
| Expose a custom callable (e.g., `sample_audio_check`) | Custom tool registered at `client.messages.create(tools=[...])` |
| Expose a set of external service tools | MCP connector (remote or stdio) |
| Bundle domain procedural knowledge for Anthropic-hosted agents | Native Agent Skills (only when AD-002 revisit triggers fire) |

### AD-006: Bytes across the JSON border via b64 sentinels

Any harness that returns `bytes` in `raw_response.*` (audio_bytes,
binary blobs, anything) is round-tripped via `{"_b64": "<base64>"}`
sentinels. The encode happens in the subprocess driver
(`agent5/execution.py`'s exec_script); the decode happens via
`_inflate_b64_sentinels` before the plugin sees the dict. Harnesses
never need to know.

**Why.** The prior `json.dump(..., default=str)` path silently
stringified bytes as Python repr — silent data loss surface. The
sentinel is the stable seam for any future modality that ships binary
data (audio, images, file blobs).

### AD-007: Safety-critical contracts live in deterministic code, not prompts

When Claude's adherence to a prompt rule determines whether a runtime
contract holds (vs. being a soft quality hint), push the enforcement
into OUR code where adherence is guaranteed. **Prompts teach patterns;
deterministic runtime gates enforce contracts.**

**Why.** The product previously chased the same symptom (zero agent
audio) across four sessions with different root causes. Each "fix"
was a new prompt rule that the next run violated differently. The
pattern broke when defaults moved into runner closures — code we own,
not prompts we hope Claude follows.

**Operational rule.** When adding a contract that the product depends
on at runtime, ask: *"can Claude violating the prompt-level
description of this contract produce a silent failure?"* If yes, add
a deterministic enforcement point (verification gate, runner closure,
validator). Keep the prompt rule too — both layers. Defense in depth.

**Active enforcement points:**

| Contract | Where enforced | Why |
|---|---|---|
| Default `input_context` injection | `agent5/build_loop.py` runner closure | Voice harnesses 400 without `input_context.instructions`; runner-level merge guarantees one is always present. |
| Phase-1 → Phase-2 transition forces tool call | `agent5/build_loop.py::PHASE2_DIRECTIVE` | Opus inheriting Sonnet's "handing off to Opus" exit narration would otherwise loop in text-only narration. The directive is a deterministic user-message injection at the api_spec_written transition. |
| `ask_research` Phase-1 gate | `agent5/build_loop.py` | Refuses with a redirect message if api_spec.txt doesn't exist yet. Forces builder to do its own primary discovery via web_search/web_fetch. |
| Web-tool revert guards | `tests/test_prod_audit_fixes.py::TestNoContainerThreadingAfterRevert` | CI fails if anyone re-introduces 20260209 container threading without the full migration (see AD-008). |
| Plugin-registry duplicate guard | `tool_plugins/__init__.py::register_plugin` | Warns on conflict; raises with `PUZZLEEVAL_STRICT_PLUGIN_REGISTRY=1`. |
| Path containment for audio streaming | `puzzleeval-api/routes/runs.py:serve_run_audio` | Allowlist of run roots; `Path.resolve().relative_to(root)` check. No traversal holes. |
| Path traversal in file uploads | `puzzleeval-api/routes/files.py` | Strips directory components, scrubs shell metachars, verifies `dest.resolve()` stays inside upload dir. |
| Bytes round-trip across JSON border | `agent5/execution.py` exec_script + `_inflate_b64_sentinels` | AD-006 — silent data loss surface eliminated. |
| Forbidden meta-filenames (B1) | `agent5/tools.py::write_file` | Rejects writes of `notes.md/.txt`, `status.txt`, `progress.md`, `state.md`, `memory.txt`, `plan.md`, `todo.md`. Soft (REJECT_TOOL_CALL — build continues, agent adapts). Bypass: `PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES=0`. |
| Phase-1 scaffold-block (B3) | `agent5/tools.py::write_file` | Rejects writes of `harness.py/smoke_test.py/live_test.py/requirements.txt` while `phase_state['api_spec_written']` is False. Phase-keyed (NOT model-keyed), so model-fallback ladders never produce false rejects. Bypass: `PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK=0`. |
| Pre-spec research budget (B4) | `agent5/build_loop.py` (post-turn user-message injection) | Counts turns (not calls) where the builder used web_search/web_fetch/ask_research while `api_spec_written=False`. After exceeding budget (default 2), injects a one-shot user message asking the builder to commit api_spec.txt. Soft — agent decides next move. Bypass: `PUZZLEEVAL_GATE_PRESPEC_RESEARCH_BUDGET=0`. |
| Agent 2 per-scope-floor (G-A2) | `schemas.py::Agent2Result._gate_a2_per_scope_floor` (Pydantic `model_validator(mode='after')`) | WARN-tier. Emits `gate_fired` when any scope has fewer than 3 candidates whose `covers_step_ids` includes that scope. Surfaces under-coverage to operator without blocking the pipeline. Bypass: `PUZZLEEVAL_GATE_AGENT2_SCOPE_FLOOR=0`. NEVER raises. |
| TestCase instructions-asymmetry (G-A3) | `schemas.py::TestCase._gate_a3_instructions_asymmetry` (Pydantic `model_validator(mode='after')`) | WARN-tier. Capability-predicate-driven via `puzzleeval/capability_predicates.py::supports_user_instructions(input_type)` — new modalities update the predicate, not the validator. Emits `gate_fired` when a non-conversational test case populates `input_context.instructions` OR a conversational one omits it. Bypass: `PUZZLEEVAL_GATE_TESTCASE_INSTRUCTIONS_ASYMMETRY=0`. NEVER raises. Promotion to REJECT_TOOL_CALL gated on 2 release cycles of zero false positives. |
| Verified-Pass needs non-negotiables (G-A4) | `validators.py::validate_checklist_for_verified_pass` (standalone fn called by Agent 4 at verdict-assignment) | WARN-tier. Returns the names of non-negotiable BuildReadinessChecklist fields that are still `unknown` when Verified Pass is granted; emits `gate_fired` log. Surfaces false-pass risk for operator review. Bypass: `PUZZLEEVAL_GATE_CHECKLIST_VERIFIED_PASS=0`. NEVER raises. |

### AD-008: Web-tool version selection prioritizes behavioral stability

Stay on `web_fetch_20250910` + `web_search_20250305` (basic versions),
not the 20260209 dynamic-filtering pair.

**Why.** The 20260209 pair introduces failure classes the basic
versions don't have: 400 "container_id is required" cascades across
every sub-agent using the tools; non-beta `messages.create()` silently
hangs on `container` kwarg; dynamic filtering's sandbox spin-up adds
3–5 min per real research call; the theoretical 24% input-token
savings doesn't materialize on our discovery workload (Agent 2 only
runs 2–3 searches per request).

**Revisit only if** Anthropic (1) fixes the non-beta `messages.create`
hang on `container`, OR (2) documents a clean "use without container
threading" mode for read-only tool users, OR (3) ships a workload
where the savings meaningfully outweigh latency.

**Revisit procedure.** Read `tests/test_prod_audit_fixes.py::TestNoContainerThreadingAfterRevert`
first — it enumerates every code site needing container-ID threading
on re-upgrade. Implement the full migration in one pass: thread
`container_id` through Agent 5 builder, Agent 4 verify continuations,
ask_research sub-agent. Move Agent 2 to `client.beta.messages.create`.
Audit every new sub-agent for container propagation.

### AD-009: Modality-contract injection is the skills-loader predecessor

Modality-specific build rules (voice harness contract, future
vision/code-gen contracts) live in `capability_playbooks/*.md` files
loaded conditionally via `agent5/playbooks.py` based on test cases'
input/output types. The structural shape already matches what a
skills loader would look like — placeholder + selector + content.

When AD-002 revisit triggers fire, the migration is purely *where the
strings come from* (already on disk as markdown today; would become
Anthropic-uploaded skill IDs). Call sites don't change.

### AD-010: Back-compat shims are documented, sunset-dated, and never silently removed

Five legacy single-file agent paths exist as comprehensive re-export
shims:

- `puzzleeval/agents/user_understanding.py` → `agent1/core.py`
- `puzzleeval/agents/research.py` → `agent2/core.py`
- `puzzleeval/agents/synthetic_tests.py` → `agent3/core.py`
- `puzzleeval/agents/synthetic_tests_file.py` → `agent3f/core.py`
- `puzzleeval/agents/screening.py` → `agent4/core.py`

Each is a 24-line shim that mirrors EVERY module attribute (public +
private) from the canonical core.py. They preserve back-compat for
external callers that imported via the old paths.

**Honor sunsets explicitly.** When the time comes to remove them, do it
in a separate PR with a clear deprecation notice. Don't auto-delete on
the basis of "no in-tree caller" alone — the cleanup audit
(`scripts/audit_dead_code.py`-style work) MUST scan every directory
that imports the package, including siblings (`puzzleeval-api/`).
Real-world example: a previous cleanup deleted `puzzleeval/selection.py`
based on no in-tree caller, then mock-pipeline-E2E caught a sibling
import in `puzzleeval-api/services/pipeline_runner.py:632`. The verification
gate caught it before merge — exactly its job.

---

## Where to make changes

This section is the navigation map: "I want to do X — which files do I
touch, in what order, with what verification?"

The codebase is split into single-purpose modules. That makes the
codebase clean but makes navigation non-obvious. Use this section as a
lookup. **Do not improvise file placement** — guess wrong and you'll
either duplicate logic across canonical and legacy paths or skip a
deterministic gate that AD-007 requires.

### Quick lookup — common changes

| If you need to... | Touch these files | Verify with |
|---|---|---|
| Add an env-var feature flag | `puzzleeval/config.py` (read it from `os.environ`) → optionally `CLAUDE.md` "Diagnostic flags" table → `tests/test_config.py` | flag round-trips through `parse_with_fallback` etc. |
| Add or modify a Pydantic schema field | `puzzleeval/schemas.py` (the contract) → `puzzleeval/validators.py` (if a new enum value or validation rule) → tests for the agent that emits it | `pytest tests/test_<schema_owner>.py` |
| Change an agent's system prompt | `puzzleeval/agents/agent<N>/templates/<prompt>.md` ONLY — never inline in `core.py` | `pytest tests/test_will_it_just_work.py` |
| Change Agent 1's modality table or worked examples | `agent1/templates/system_prompt.md` → ensure all 11 enum values from `validators.py::VALID_OUTPUT_TYPES` are taught | `pytest tests/test_will_it_just_work.py::TestAgent1KnowsNewOutputTypes` |
| Add a new modality plugin | New file `puzzleeval/tool_plugins/<name>.py` → instance auto-registered at import via `tool_plugins/__init__.py` → ensure modality enums are in `validators.py` | `pytest tests/test_<name>.py` + `tests/test_plugin_wiring.py` |
| Modify how a plugin scores | `puzzleeval/tool_plugins/<plugin>.py::evaluate_output` | `pytest tests/test_<plugin>.py` |
| Add a new SSE event | Three coordinated edits: `puzzleeval-api/services/pipeline_runner.py` (emit) → `src/hooks/usePipelineRun.ts` (handle) → `ARCHITECTURE.md` SSE event catalog (document) | manual: drive a mock pipeline, watch frontend dev tools |
| Add a config-only diagnostic flag | `puzzleeval/config.py` → `CLAUDE.md` "Diagnostic flags" table | unit test that flips the flag |
| Modify the final report shape | `puzzleeval/report.py::EvaluationReport` (Python) → `src/types/pipeline.ts` (TS types) → `src/components/playground/EvaluationReportCard.tsx` (UI) | `pytest tests/test_report.py` + frontend type-check |
| Add an Anthropic API retry/recovery behavior | `puzzleeval/agents/agent5/api_call.py` ONLY (it's the boundary) — never duplicate this logic in other agents | `pytest tests/test_api_call.py` |
| Add a per-agent cost tracking dimension | `puzzleeval/telemetry/cost.py` (canonical home) → if it's Agent 5–specific, also `agent5/costing.py` which composes the per-turn snapshot | `pytest tests/test_telemetry.py` |
| Change which files trigger Phase 1 → Phase 2 model switch | `puzzleeval/agents/agent5/dispatch_helpers.py::TRANSITION_FILES_WRITE` and `detect_phase_transition` | `pytest tests/test_dispatch_helpers.py` + `tests/test_build_loop_behavior.py::TestPhaseTransitionTriggers` |
| Add a tool the builder can call | `puzzleeval/agents/agent5/tools.py` (local dispatch) → register in the tool list in `agent5/build_loop.py` API call kwargs → document in the builder system prompt | `pytest tests/test_build_loop.py` |
| Add a deterministic safety contract (AD-007 pattern) | See "Adding a safety contract" below — the right file depends on the contract's scope |
| Add a runtime LLM-prompt playbook (modality contract) | New `puzzleeval/capability_playbooks/<id>.md` with frontmatter → optionally a paired `ContractGate` in `puzzleeval/contracts/runtime_gates.py` → `puzzleeval/contracts/coverage.py` if it has a coverage requirement | `pytest tests/test_contract_loader.py` + `tests/test_runtime_gates.py` |
| Add a back-compat shim for a moved symbol | The legacy file (e.g., `puzzleeval/agents/research.py`) — mirror via `for _name in dir(_core): globals()[_name] = getattr(_core, _name)` pattern → document sunset in this file under AD-010 | `pytest tests/` (full suite — back-compat is only verified by the importer files passing) |
| Update Agent 5's builder system prompt | `puzzleeval/agents/agent5/templates/builder_system_prompt.md` ONLY — never inline in `agent5/prompts.py` | manual: real-API smoke (`scripts/real_api_smoke.py`) on a small candidate |
| Update a capability playbook (voice/streaming/etc.) | `puzzleeval/capability_playbooks/<id>.md` ONLY — these are PROMPT CONTENT, treat as code | `pytest tests/test_contract_loader.py` |

### Multi-file coordinations

Some changes need 2+ files to land together or the system breaks. Always
land them in one PR.

#### Adding a new SSE event

Five coordinated edits:

1. **Backend emit:** `puzzleeval-api/services/pipeline_runner.py` — call `state.event_bus.emit("<event_name>", payload)` at the right point in the pipeline.
2. **Frontend type:** `src/types/pipeline.ts` — add the event payload's TypeScript type.
3. **Frontend handler:** `src/hooks/usePipelineRun.ts` — add a switch case in the SSE event handler that updates state.
4. **Frontend consumer:** the React component that renders the new state (e.g., `Playground.tsx`, an evidence component).
5. **Docs:** `ARCHITECTURE.md` SSE event catalog table — add the row.

If you skip step 5, the SSE catalog drifts. If you skip step 4, the
event fires but UI does nothing. If you skip step 2, TypeScript breaks.

#### Adding a new agent (or significantly changing one)

Per-agent package convention (AD-010 mirror):

1. New directory `puzzleeval/agents/agent<N>/` with `__init__.py` (re-exports core public names) + `core.py` (Python logic) + `templates/<prompt>.md` (system prompts).
2. `pyproject.toml` `[tool.setuptools.package-data]` — add `"puzzleeval.agents.agent<N>" = ["templates/*.md"]` so the markdown ships in the wheel.
3. Schema additions in `puzzleeval/schemas.py` — input + output Pydantic models.
4. Pipeline orchestration: `puzzleeval/pipeline.py` (CLI side) AND `puzzleeval-api/services/pipeline_runner.py` (backend) — both need to know how to invoke the agent.
5. Tests: new `tests/test_agent<N>.py` modeled on existing test files.
6. CLI flag: `puzzleeval/cli.py` — add a `--agent<N>` argparse entry that routes through the pipeline.
7. ARCHITECTURE.md "The pipeline" diagram + "Agent N" subsection.
8. CLAUDE.md "Where to find things" table — one row.

#### Adding a deterministic safety contract (AD-007)

The decision tree for *which file* depends on what the contract guards:

| Contract scope | Where it lives |
|---|---|
| Builder turn-loop invariant (e.g., "the builder must call write_file after the api_spec transition") | `puzzleeval/agents/agent5/build_loop.py` (in-loop deterministic injection — see `PHASE2_DIRECTIVE` for the pattern) |
| Pre-completion gate ("HARNESS_COMPLETE only accepted when X exists") | `puzzleeval/agents/agent5/verification.py` |
| Tool-dispatch gate (e.g., "ask_research refused in Phase 1") | `puzzleeval/agents/agent5/build_loop.py` (the dispatch branch for that tool) — pattern: refuse with a redirect message that's appended as a tool_result |
| Cross-agent runtime gate (e.g., "voice harnesses must return Shape A or Shape B raw_response") | `puzzleeval/contracts/runtime_gates.py` (the cross-cutting registry; pattern is `ContractGate` subclass added to `ALWAYS_ON_GATES`) |
| Schema-level invariant (e.g., "covers_step_ids must be a subset of blueprint step_ids") | `puzzleeval/validators.py` (validator function called from agent output) |
| Pipeline-runner state invariant ("save state on score-change for explicit-candidate boost") | `puzzleeval-api/services/pipeline_runner.py` (the right phase branch) |
| Backend HTTP contract ("upload size cap", "path containment") | `puzzleeval-api/routes/<route>.py` |

After picking the file, ALSO:
- Add a regression test that asserts the contract is enforced (not just that the prompt teaches it). The test file lives next to the gate file in `tests/`.
- Add a row to `CLAUDE.md` AD-007's "Active enforcement points" table so future AI knows it exists.

#### Updating the final evaluation report

Three coordinated files:

1. **Python:** `puzzleeval/report.py` — modify the `EvaluationReport` dataclass + `assemble_report` logic.
2. **TS types:** `src/types/pipeline.ts` — mirror the Python schema.
3. **UI:** `src/components/playground/EvaluationReportCard.tsx` — render the new field. Sub-blocks live in the same file (`AudioPathsBlock`, `RubricBreakdownBlock`, `PricingBlock`).

### Anti-patterns — where NOT to put code

These are real mistakes future sessions will be tempted to make.

| Tempting to add code in... | Reality | Where it actually goes |
|---|---|---|
| `puzzleeval/agents/implement_test_env.py` | This file is **back-compat shims only.** It's 4,708 LoC of forwarding to canonical homes in `agent5/`. New code added here means the canonical home doesn't get it. | Find the canonical home in `agent5/` (use the table in "Where to find things") |
| `puzzleeval/agents/research.py` (or other legacy single-file paths) | Same — these are 24-line shims. Adding code here breaks the canonical-vs-legacy invariant. | `puzzleeval/agents/agent2/core.py` (the canonical home) |
| Inline prompt strings in `agent<N>/core.py` | Prompts live as `templates/*.md` files (AD-010 convention). Inline strings drift from the templates. | `puzzleeval/agents/agent<N>/templates/<name>.md` |
| `puzzleeval/agents/agent5/build_loop.py` for everything Agent-5–related | `build_loop.py` is the orchestration spine only. Pure helpers go in `dispatch_helpers.py`; tool dispatch goes in `tools.py`; API-call concerns go in `api_call.py`. | The right `agent5/<sub-module>.py` (see the agent5 table in "Where to find things") |
| Comment cleanup in `capability_playbooks/*.md` or `agent<N>/templates/*.md` | These markdown files are **PRODUCT BEHAVIOR, NOT DOCS** — they get concatenated into the LLM prompt at runtime. "Cleaning a stale comment" can change LLM output. | Treat these files as code. Don't comment-clean them. |
| Adding a NEW-XX session changelog entry to CLAUDE.md | Forbidden by the file's own rules — see the top of this file. | Git commit message + PR description. |
| Adding tactical fixes as new AD-XXX entries | AD-XXX entries are for ARCHITECTURAL DECISIONS (with alternatives weighed and revisit triggers). Tactical fixes are code-comment + test material. | Code comment near the fix + a regression test |
| Adding speculative "we might want X" entries to OT-XXX | OT-XXX entries are for genuinely-broken-today items. Speculation goes in design docs (or just doesn't go anywhere — wait until the problem is real). | (nowhere; just don't) |
| Adding the same SSE event handler in two places | Frontend has ONE SSE handler in `src/hooks/usePipelineRun.ts`. If you find yourself wanting to handle the same event in `Playground.tsx` directly, the right move is to expose state from the hook. | Add to the hook's switch statement; consume the state in components. |
| Adding cost tracking ad-hoc in an agent's `core.py` | Cost tracking is centralized in `puzzleeval/telemetry/`. Per-agent cost composition uses those primitives. | `puzzleeval/telemetry/cost.py` (canonical) → agent calls `obs.record_llm_call(ctx, ...)` |
| Adding a new public symbol to `implement_test_env.py` | The whole point of the cleanup was to drain this file. Adding to it is a regression. | The right `agent5/` sub-module |

### When you genuinely don't know where it goes

If a change doesn't fit any of the rows above:

1. **Check whether it's a new architectural decision.** If yes → it should land with an AD-XXX entry justifying the new home. Decisions have alternatives weighed and revisit triggers.
2. **Check whether you're tempted to introduce a new module.** If yes → ask "is this a new responsibility, or am I splitting an existing one?" If splitting, the new module should pull symbols from one existing module — not synthesize across many.
3. **Check whether it's a cross-cutting concern.** If yes → look in `telemetry/`, `contracts/`, or `validators.py` first. These are the existing cross-cutting homes.
4. **If still unclear, write it where the BLAST RADIUS is smallest.** A function that 2 callers use can live in either caller; preferring the smaller of the two minimizes the back-out cost if it gets refactored later.

The cleanup we did was painful precisely because we didn't follow these
rules earlier. Don't add to that debt.

---

## How to work safely

### Test invariants

The full suite runs in ~14 min on this machine:

```bash
ANTHROPIC_API_KEY=dummy python -m pytest tests/ -q          # ~14 min, 1,744 tests
ANTHROPIC_API_KEY=dummy python -m pytest puzzleeval-api/tests/ -q  # ~10 s, 37 tests
ANTHROPIC_API_KEY=dummy python -m pytest -m generalizability tests/generalizability/ -v  # 39 marker-gated
```

**Before merging any agent change**, run:

```bash
ANTHROPIC_API_KEY=dummy python scripts/mock_pipeline_direct.py  # full pipeline E2E, mocked
ANTHROPIC_API_KEY=dummy python scripts/preflight_check.py --no-api  # 84 wiring checks
```

For real-API smoke (cost ~$0.01):

```bash
python scripts/real_api_smoke.py  # 8 checks: Anthropic, OpenAI, ElevenLabs, voice loopback
```

### Diagnostic flags

Every refinement path ships behind a flag so failures are bisectable.
Defaults all-on. To disable a phase, set the env var to `0` and re-run.

| Flag | Default | Effect when disabled |
|---|---|---|
| `PUZZLEEVAL_ENABLE_FETCH_FALLBACK` | `1` | Agents see raw web_fetch errors with no recovery guidance |
| `PUZZLEEVAL_FETCH_RATE_LIMIT_BACKOFF` | `5` (sec) | `0` disables 429 backoff sleep |
| `PUZZLEEVAL_BILLING_ENFORCED` | `0` | When `0`, billing tracks but doesn't block. When `1`, 402 on insufficient credits. |
| `PUZZLEEVAL_USER_SELECTION_ENABLED` | `1` | When `0`, pipeline auto-runs all Agent 2 candidates without the SelectionPanel pause |
| `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` | `1` | When `0`, Agent 4 reverts to shallow pass/fail; Agent 5 must do its own Phase-1 research |
| `PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED` | `1` | When `0`, Agent 2 reverts to single-pass search (no per-scope coverage) |
| `PUZZLEEVAL_SCOPE_TEST_MODE` | `1` | When `0`, legacy flat-candidate testing instead of per-scope rankings |
| `PUZZLEEVAL_HYBRID_EVAL_ENABLED` | `0` | Opt-in. When `1`, Claude picks plugins as tools when deterministic dispatch yields none. |
| `PUZZLEEVAL_VENV_PREINSTALL` | `1` | When `0`, skip pip install of common packages on venv creation (faster but harness's first turn slower) |
| `PUZZLEEVAL_AGENT5_DEEP_VERIFY_ENABLED` | `1` | Same effect as the Agent 4 flag at the Agent 5 boundary |
| `PUZZLEEVAL_ADVERSARIAL_PROBES_ENABLED` | `1` | When `0`, skip the 6-probe adversarial battery before Agent 3 cases |
| `PUZZLEEVAL_TUNNEL_URL` | unset | Set to a public URL (ngrok / cloudflared) to advertise the webhook receiver externally |
| `PUZZLEEVAL_EXTRA_RUNS_ROOTS` | unset | Comma-separated paths added to the audio-streaming allowlist |
| `PUZZLEEVAL_STRICT_PLUGIN_REGISTRY` | unset | When `1`, duplicate `register_plugin()` raises instead of warning |
| `PUZZLEEVAL_MAX_RUN_COST_USD` | `25` | Per-run USD cap; raised → `BudgetExceededError` |
| `PUZZLEEVAL_EFFORT` | `medium` | Reasoning depth: `low` / `medium` / `high` / `xhigh` / `max` |
| `PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES` | `1` | Gate B1. When `0`, write_file does NOT reject meta-files (NOTES.md, STATUS.txt, etc.) |
| `PUZZLEEVAL_GATE_INTROSPECTION_WARN` | `1` | Gate B2 (WARN-only). When `0`, no `gate_fired` log on `inspect_*.py / probe_*.py` writes before harness.py exists. |
| `PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK` | `1` | Gate B3. When `0`, write_file does NOT reject `harness.py / smoke_test.py / live_test.py / requirements.txt` writes during Phase 1 (api_spec_written=False). |
| `PUZZLEEVAL_GATE_PRESPEC_RESEARCH_BUDGET` | `1` | Gate B4. When `0`, no user-message injection when pre-spec research turns cross the budget. |
| `PUZZLEEVAL_GATE_PRESPEC_RESEARCH_BUDGET_COUNT` | `2` | Number of pre-spec research turns allowed before B4 injects the budget-reached message. |
| `PUZZLEEVAL_GATE_AGENT2_SCOPE_FLOOR` | `1` | Phase-2B gate G-A2. When `0`, Agent2Result validator does NOT warn on scopes with fewer than 3 covering candidates. |
| `PUZZLEEVAL_GATE_TESTCASE_INSTRUCTIONS_ASYMMETRY` | `1` | Phase-2B gate G-A3. When `0`, TestCase validator does NOT warn on `input_context.instructions` mismatches with the modality predicate. |
| `PUZZLEEVAL_GATE_CHECKLIST_VERIFIED_PASS` | `1` | Phase-2B gate G-A4. When `0`, `validate_checklist_for_verified_pass` returns empty list (no warn) regardless of unknown non-negotiables. |

Standard debugging procedure when a real run misbehaves:

1. Check `pipeline_summary.json:metadata` for activated phases.
2. Check per-agent JSON outputs for fingerprints (workflow blueprint
   present, candidates have `covers_step_ids`, etc.).
3. Flip flags off in reverse order until behavior recovers; the
   last-flipped flag is the culprit.
4. If behavior is wrong even with all flags off → schema-level
   regression; bisect via git over schema commits.

### Conventions

- **Don't add new code without tests.** Helper-level tests for new
  helpers, behavior-pinning tests for refactors, source-grep tests
  only as Category A (name-preservation guards) with explicit comments.
- **Prefer call-time deprecation, never import-time.** Module-level
  `__getattr__` (PEP 562) is the right pattern for deprecating module
  attributes — fires only on attribute access.
- **Comments tied to defensive code stay; comments tied to refactor
  history go.** If a `# ` comment explains WHY a defensive guard
  exists, keep it. If it explains WHEN something was changed, delete
  it (git blame is the archive).
- **Markdown under `templates/` and `capability_playbooks/` is product
  behavior, not docs.** It gets concatenated into the LLM system
  prompt at runtime. Do NOT comment-clean these files.

---

## Outstanding gaps (real, not historical)

These are honest open items, not aspirational. None block production
for the local-hosted use case.

### OT-001: Substring-match scoring is brittle for natural language

Voice tests' `expected_agent_contains` and chatbot tests'
`expected_response_contains` use literal substring matching. Agent
says "nine" when test asserts `"9"` → false negative. Voice runs
understate quality by ~20–50% depending on phrasing.

**Fix direction.** Per-turn semantic-match fallback in
`tool_plugins/voice_realtime.py::drive_conversation` and
`conversation_simulator.py`. When substring fails, escalate to a
one-shot LLM judge call (~$0.002). ~1 hour of work.

### OT-002: HARNESS_COMPLETE doesn't always test the production call shape

Agent 5's builder writes its own `live_test.py` with whatever payload
shape it likes. The plugin's `drive_conversation` calls `harness.run()`
with a different shape (e.g., `{"audio_url": "..."}` vs the builder's
`{"text": "..."}`). Mitigated by the runner-level default
`input_context` injection (AD-007), but not eliminated.

**Fix direction.** Phase 4 — REAL TEST PROBE. Auto-seed the sandbox
with one Agent 3 test case + a `_production_shape_helper.py` that
builds the exact production-shape payload. Builder MUST run the probe
(deterministic verification gate via `phase_4_passed.txt`) before
HARNESS_COMPLETE.

### OT-003: pydub merger requires ffmpeg on PATH for full quality

When ffmpeg is missing, `_try_merge_via_pydub` falls back to
byte-concat. Byte-concat works only when all per-turn segments share
encoding parameters; mixed sample-rates produce files that pause at
the first transition.

**Fix direction.** Either document ffmpeg as a hard prerequisite in
README, or add a startup check that warns when missing.

### OT-004: Agent 5 cancellation is not propagated into the build loop

`state.cancel_requested` is checked at agent boundaries but not inside
`_build_single_harness`'s 25-turn loop. An 8-minute build is
uninterruptible once started.

**Fix direction.** Thread a `cancel_event` through `BuildContext` and
check it before each `make_builder_api_call`.

### OT-005: Agent 2 per-scope research is sequential

For an N-scope blueprint, Agent 2 makes one serial research call.
Wall-clock cost, not correctness — N parallel calls would cut Agent 2
from ~3 min to ~1 min for a 3-scope blueprint.

**Fix direction.** Per-scope `asyncio.gather`. Existing
`apply_scope_picks` post-processing handles the dedupe.

### OT-006: Live `agent_thinking` SSE streaming not wired

Extended-thinking blocks exist in responses but are never extracted to
SSE. UI shows silent spinners during Opus planning.

**Fix direction.** Subscribe to `agent_thinking` already exists in
`hooks/usePipelineRun.ts`; the emission in `agent5/build_loop.py`
needs to call `progress_callback("agent_thinking", ...)` per
`thinking` block.

### OT-007: Idempotency keys + DRY_RUN propagation in harness writes

A retried Stripe/Slack write could create duplicates in the real
provider's account. `side_effects=creates_records` scopes could leak
test data unless the candidate publishes a sandbox URL.

**Fix direction.** Agent 5 builder prompt redesign + per-candidate
teardown protocol. Real fix; not a bandaid.

---

## Documentation map

| File | Purpose |
|---|---|
| `CLAUDE.md` (this file) | AI-assistant working context: decisions, conventions, gaps |
| `ARCHITECTURE.md` | System design: pipeline, modules, plugins, SSE, frontend, frontend integration |
| `puzzleeval-api/BACKEND_ARCHITECTURE.md` | FastAPI backend specifics — request flow, SSE event system, pipeline orchestration, mock vs real mode |
| `SECURITY.md` | Threat model + what the local sandbox protects against vs what cloud-pivot would require |
| `PLUGIN_KEYS.md` | Credential surfaces + per-plugin key requirements + setup verification |
| `puzzleeval-api/.env.example` | Backend env-var template |
| `pyproject.toml` | Python dependencies + pytest config |
| `README.md` (repo root) | 30-second quickstart |

The Agent 5 sub-modules used to have their own `agent5/ARCHITECTURE.md`;
that file was deleted because its contents were stale (described
post-cleanup work in future tense). Agent 5 internals are documented
in this file's `puzzleeval/agents/agent5/` table under "Where to find
things".

---

(End. The rules for editing this file are at the top — see "What this
file is and is not".)
