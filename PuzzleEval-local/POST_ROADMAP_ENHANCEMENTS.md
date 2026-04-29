# Post-Roadmap Enhancements

> Changes made to PuzzleEval AFTER the 10-phase roadmap was complete.
> These are architectural improvements, UX polish, and production-hardening
> discoveries that emerged during integration testing and user feedback.
>
> The 10-phase roadmap is documented in `AGENT_REFINEMENT_ROADMAP.md`.
> Known gaps and their fix status are in `GAP_ANALYSIS.md`.
> This document captures everything else.

---

## 1. TestPlan Architecture — Agent 1 as Test Director

**The problem:** Agent 3 and 3F operated as independent black boxes. Each
guessed what tests to generate without a shared plan. For a multi-scope
workflow like "OCR invoices → sync to QuickBooks", the result was:
- Agent 3F got ALL sub-tasks when files were provided (even the ones that
  didn't need files — QuickBooks sync doesn't use files)
- Agent 3 got ALL sub-tasks when no files (even file-needing ones got
  synthetic text proxies)
- Downstream steps (step_2 receiving step_1's OCR output) had no way to
  know what the upstream output SHAPE was — Agent 3 guessed and produced
  test inputs that didn't match the pipeline's actual data flow

**The fix — Centralized test planning:**

Agent 1 now emits a `TestPlan` alongside the `WorkflowBlueprint`. The
TestPlan contains one `ScopeTestSpec` per scope with explicit:
- `test_mode`: file_based / synthetic_text / synthetic_structured
- `input_type`: what type of test input to use (MUST match architecture)
- `output_type`: what output to expect (MUST equal step's output_format)
- `input_description`, `expected_output_description`: human descriptions
- `sample_input`, `sample_output`: ONE concrete example each
- `test_count_target`: how many test cases to generate
- `upstream_output_shape`: CRITICAL for downstream steps — the JSON shape
  of what the upstream step produces, so Agent 3 can simulate it
- `requires_user_files`: True when real files are needed
- `file_description`: what files the user should provide
- `evaluation_focus`: list like ["accuracy", "completeness", "format_compliance"]

Agent 3/3F are no longer planners — they EXECUTE the TestPlan. The CLI
and pipeline_runner both route by `ScopeTestSpec.test_mode` per scope:
`file_based` → Agent 3F; `synthetic_*` → Agent 3. Results merge into one
`Agent3Result`.

**Schema additions (`schemas.py`):**
- `ScopeTestSpec` Pydantic model
- `TestPlan` Pydantic model
- `UserUnderstandingOutput.test_plan: TestPlan | None` (optional for backward compat)

**Validator additions (`validators.py`):**
- Warning when `workflow is not None and test_plan is None`
- Error when `scope_specs` don't cover all workflow steps
- Warning when extra scope_specs exist for non-existent steps
- Warning when `total_test_target` doesn't match scope-target sum
- Error when `test_mode` value is outside the valid set
- Warning when multiple steps share the same `capability` string
  (causes scope routing ambiguity)

**Agent 1 prompt update (`user_understanding.py`):**
New "Test Plan (REQUIRED when workflow is non-null)" section teaches
Agent 1 to emit per-scope specs. Includes a worked example (OCR →
QuickBooks) and 5 rules:
1. `output_type` MUST equal the step's `output_format`
2. Downstream `sample_input` MUST simulate upstream output
3. `upstream_output_shape` REQUIRED for non-root steps
4. `total_test_target` = sum of all scope targets
5. `test_plan` null when `workflow` is null

**Agent 3 prompt update (`synthetic_tests.py`):**
- New `_format_test_plan()` helper renders the TestPlan as an
  AUTHORITATIVE instruction block in the generation message
- Per-sub-task section now includes `[Architecture]` annotations:
  step_id, role, output_format, input_source, requires_test_files
- New `_format_workflow_architecture()` helper renders the DAG
- System prompt has new "Architecture Alignment (CRITICAL)" section
  telling Agent 3 to use the TestPlan's specs verbatim
- New "Generative / Non-Text Output Domains" section teaches Agent 3
  how to generate tests for image/audio/video/code generation APIs

**Dispatcher logic (`cli.py`, `pipeline_runner.py`):**
- `_filter_user_understanding(uo, subtasks_to_keep)` — creates a filtered
  UserUnderstandingOutput with only the specified sub-tasks, so each
  agent receives only its assigned work
- `_merge_agent3_results(file_result, text_result)` — merges test cases
  and coverage summaries from parallel Agent 3 + 3F runs into one
  Agent3Result (with correct cost accumulation and generation notes)
- `_run_test_generation` in CLI and `_run_real_agent3` in pipeline_runner
  both route by TestPlan.test_mode when available, fall back to
  `requires_test_files` when no TestPlan exists

### TestCase.scope_id — deterministic routing

**The problem:** Even with a TestPlan, `group_tests_by_scope` in
`scope_routing.py` used fuzzy keyword matching between a test case's
`sub_task_ref` and each WorkflowStep's capability/description. This broke
when:
- Two steps had identical capabilities (translation pipeline)
- The LLM rephrased `sub_task_ref` slightly from the blueprint text
- Test case descriptions used domain-specific vocabulary that didn't
  overlap with the step's capability words

**The fix:** Added `scope_id: str | None` field to `TestCase`. When a
TestPlan exists, Agent 3 populates it from `ScopeTestSpec.scope_id`.
`group_tests_by_scope()` and `build_scope_runs()` now use `scope_id` as
a PRIMARY deterministic key; fuzzy matching is the FALLBACK for legacy
test cases (pre-TestPlan) without scope_id.

This is domain-agnostic — it's a join key between data structures, not
a domain-specific rule.

---

## 2. Mixed-Mode Test Generation (Agent 3 + 3F in parallel)

**The problem:** For "OCR invoices → sync to QuickBooks":
- Providing files → Agent 3F ran for BOTH scopes → generated nonsensical
  file-based tests for QuickBooks sync (which doesn't need files)
- No files → Agent 3 ran for BOTH scopes → generated text-proxy tests
  for OCR (poor quality — synthetic text is a bad stand-in for a real
  invoice photo)

**The fix — Per-sub-task routing:**

Both CLI `--agent5` parallel path AND API `_run_real_agent3` now split:
- File sub-tasks (or `test_mode=file_based`) → Agent 3F with REAL files
- Text sub-tasks (or `test_mode=synthetic_*`) → Agent 3 with synthetic data
- Results merge via `_merge_agent3_results`

If files aren't provided for file-based scopes, Agent 3 generates
synthetic proxies with `file_required=True` flagged, so Agent 5's
execution knows these are degraded tests.

**The flow matrix:**

| Scenario | Behavior |
|----------|----------|
| OCR only, files provided | Agent 3F runs with files → ground truth from real invoices |
| OCR only, NO files | Agent 3 synthetic → text proxies, `file_required=True` |
| OCR + Sync, files provided | Agent 3F for OCR (real files) + Agent 3 for Sync (synthetic) → merged |
| OCR + Sync, NO files | Agent 3 for everything → OCR tests get `file_required=True`, Sync tests synthetic |
| Chatbot only | Agent 3 synthetic → conversation test cases |
| Image generation | Agent 3 synthetic → text prompts, text output descriptions |

---

## 3. file_required Fallback (Not Silent-Skip)

**The problem:** When `file_required=True` and `test_file_path=None`,
Agent 5's `_execute_all_tests()` saw the INCOMPATIBLE result and silently
marked it as `skip_reason="INCOMPATIBLE"`. Result: OCR and audio scopes
got ZERO non-skipped test results when users didn't provide files. The
user saw "N skipped" in results but no actual test data.

**The fix (`implement_test_env.py`):**
File-required tests without files now RUN with text `input_data` as
fallback. Many APIs accept both file uploads AND text input
(base64-encoded, URLs, plain text). The harness tries:
- Success: result is annotated with `_note: "Tested with synthetic text input (no user file provided)"`
- INCOMPATIBLE: result is recorded as a normal FAILURE (visible, counted)
  with error message `"[Tested with synthetic text input (no user file provided)] INCOMPATIBLE: ..."`
- Regular INCOMPATIBLE tests (non-file_required) still get `skip_reason`

This is domain-agnostic — it works for ANY file-based API that also
accepts text input, which is most of them (OCR APIs accept URLs, audio
APIs accept base64, document APIs accept structured input).

**Schema description updates:**
`TestCase.file_required` and `TestCaseResult.skip_reason` docstrings
updated to reflect the fallback behavior.

---

## 4. Architecture Summary in Chat (UX improvement)

**The problem:** After Agent 1 finished the conversation and returned
`is_clear=true`, the pipeline started immediately. The user saw a
chat → pipeline transition but might miss the WorkflowDiagram on the
right panel because the Activity tab auto-switched focus. No explicit
confirmation that "Agent 1 understood you correctly."

**The fix:**

Two places emit an architecture summary:

**Pipeline_runner (`pipeline_runner.py`):**
When `workflow_blueprint` SSE fires, also emits an `agent_activity`
event with a human-readable summary of the blueprint + test plan:
```
Workflow designed with 2 scope(s):
**step_1** (ocr): OCR invoice photos into structured JSON
**step_2** (accounting_sync): Push extracted data to QuickBooks

**Test Plan:**
- step_1: file_based, 8 tests
- step_2: synthetic_structured, 6 tests
```

**Frontend (`usePipelineRun.ts`):**
The `workflow_blueprint` SSE handler now also adds a chat message
showing the architecture summary, so the user sees it in the chat
transcript, not just the activity feed:
```
I've designed a 2-scope workflow:

**step_1** (ocr): OCR invoice photos into structured JSON
**step_2** (accounting_sync): Push extracted data to QuickBooks

**Test Plan:**
- step_1: file_based, 8 tests
- step_2: synthetic_structured, 6 tests

Now searching for the best AI solutions for each scope...
```

This is a non-blocking UX improvement. The pipeline starts immediately;
the user just sees WHAT Agent 1 designed before results start flowing in.

The `workflow_blueprint` SSE payload now includes `test_plan` alongside
`workflow`, so the frontend can render both.

---

## 5. Agent 3 Architecture-Aware Test Generation

**Changes to `synthetic_tests.py`:**

### 5a. Sub-task section enriched with architecture annotations

Before:
```
1. Extract structured data from invoice photos
   Capability: document OCR
   Search keywords: invoice OCR API
```

After:
```
1. Extract structured data from invoice photos
   Capability: document OCR
   Search keywords: invoice OCR API
   [Architecture] step_id=step_1, role=ocr
   [Architecture] output_format=structured_json
   [Architecture] input_source=user provides input directly
   [Architecture] requires_test_files=True (file-based input)

2. Create bill entries in QuickBooks from structured data
   [Architecture] step_id=step_2, role=accounting_sync
   [Architecture] output_format=action
   [Architecture] input_source=receives output from step_1
   [Architecture] requires_test_files=False (text/synthetic input)
```

Agent 3 now knows EXACTLY what output_type to use per scope (no
guessing) and what kind of input each step receives.

### 5b. Workflow DAG section added

A new `_format_workflow_architecture()` helper renders the DAG so
Agent 3 sees the data flow:
```
## Workflow Architecture (from Agent 1)
  step_1 [ocr] (first step)
    Input: user -> Output: structured_json
    "OCR invoice photos into structured JSON"
  step_2 [accounting_sync] (after step_1)
    Input: step_1 -> Output: action
    "Push extracted data to QuickBooks"

For downstream steps (input_from != user), your test case input_data
should SIMULATE what the upstream step would produce.
```

### 5c. Generative / Non-Text Output Domains section

New section in the SYSTEM_PROMPT teaches Agent 3 how to handle image,
audio, video, and code generation domains:
- `input_type="text"` (the generation prompt IS text)
- `output_type="action"` for APIs that return URLs/refs
- Realistic generation prompts as `input_data`:
  - Image: "A watercolor painting of a golden retriever puppy playing
    in autumn leaves, soft lighting, warm color palette"
  - Audio: "Generate a 30-second jazz piano loop at 120 BPM in C major"
  - Code: "Write a Python function that implements binary search"
- `expected_output` describes the ideal result textually since the
  output itself is non-textual
- Evaluation strategy: `contains_key_info` (valid URL), `format_compliance`
  (response shape), `subjective_quality` (content matches intent)

### 5d. Test Plan as authoritative section

The new `_format_test_plan()` helper (from section 1 above) emits a
section titled "## TEST PLAN (AUTHORITATIVE — from Agent 1)" that
tells Agent 3 to follow the specs EXACTLY — do not override
input_type/output_type based on independent judgment.

---

## 6. file_required Flag Wiring (CLI)

**The problem:** The `file_required: bool` field on `TestCase` existed
in the schema since the original design but was NEVER set by any
code path. Agent 5 had INCOMPATIBLE fallback logic that relied on
this flag but it was always `False`.

**The fix:** The CLI now wires `file_required=True` on Agent 3's
generated test cases when:
- The sub-task was flagged `requires_test_files=True` in Agent 1's output
- But no `--test-files` were provided
- Agent 3 is generating synthetic proxies

The matching logic uses fuzzy keyword overlap between sub-task
capability/description and the test case's `sub_task_ref`. Once wired,
Agent 5's execution path (fixed in section 3) picks up these flagged
test cases and treats them as fallback-eligible.

---

## 7. Opus 4.6 → Opus 4.7 Upgrade

**Scope of upgrade:**

Source code (4 files):
- `puzzleeval/config.py` — `AGENT1_MODEL` default, `AGENT5_BUILDER_MODEL`
  default, `MODEL_PRICING` entry, `MIN_CACHEABLE_TOKENS` entry
- `puzzleeval/agents/implement_test_env.py` — `ADVISOR_TOOL.model` and
  cost-tracking fallback model
- `puzzleeval/agents/research.py` — comments
- `tests/test_agent5.py` — advisor mock

Documentation (4 files):
- `PuzzleEval-local/CLAUDE.md`
- `PuzzleEval-local/ARCHITECTURE.md`
- `PuzzleEval-local/puzzleeval/agents/README_AGENT5.md`
- `AGENT_REFINEMENT_ROADMAP.md` + synced plan file

(Two standalone Anthropic-API reference notes that were drafted during the
thinking/tool-calling pass — `claude_api_2026_reference.md` and
`claude_api_mastery_guide.md` — were later removed in the dead-code
cleanup pass; the canonical reference is Anthropic's live docs.)

**Intentionally NOT modified:** Historical run artifacts in
`puzzleeval-api/runs/*/conversation_log.json`. These are immutable logs
from past runs; rewriting them would be revisionist history.

**Effect:** Agent 1 (blueprint designer) and Agent 5 (harness builder +
advisor) now default to `claude-opus-4-7`. Env var override still
available: `PUZZLEEVAL_AGENT1_MODEL`, `PUZZLEEVAL_BUILDER_MODEL`.

---

## 8. Validator Hardening (cross-data consistency)

**New validator checks in `validate_agent1_output`:**

### 8a. TestPlan ↔ WorkflowBlueprint alignment
- Error when scope_specs don't cover all workflow steps
- Warning when extra scope_specs exist for non-existent step ids
- Ensures Agent 1 doesn't silently skip scopes

### 8b. Arithmetic consistency
- Warning when `total_test_target != sum(spec.test_count_target)`
- Catches LLM output where the summary number disagrees with details

### 8c. Enum validation
- Error when `test_mode` is outside {`file_based`, `synthetic_text`,
  `synthetic_structured`}
- Catches LLM invention of new mode values

### 8d. Duplicate capability detection
- Warning when multiple `WorkflowStep`s share the same `capability` string
- Catches translation-style fan-outs where Agent 1 didn't differentiate
  (e.g., three "text translation" steps with no language distinction)
- Recommended fix in the warning text: "Consider making capabilities
  unique per step (e.g., 'English to Spanish translation' instead of
  'text translation' for all languages)"

---

## 9. Input/Output Type Enum Expansion

**The problem:** The original enums were English-text-oriented and
couldn't honestly describe image generation (media_url output), audio
transcription (audio input), or file-reference APIs.

**The fix (`validators.py`):**
```python
VALID_INPUT_TYPES = {
    "text", "structured_data", "document_content",
    "conversation", "image_description",
    "audio_content",      # NEW — for audio transcription
    "file_reference",     # NEW — generic file upload
}
VALID_OUTPUT_TYPES = {
    "free_text", "structured_json", "classification",
    "extraction", "action",
    "media_url",          # NEW — for image/audio/video generation APIs
}
```

These are SEMANTIC categories, not provider-specific. `audio_content`
works for Deepgram, AssemblyAI, Whisper, or any future audio API.
`media_url` works for DALL-E, Stable Diffusion, Midjourney, or any API
that returns a reference to generated content (images, audio, video,
PDFs, archives).

---

## 10. Documentation Artifacts Created

New documents added to help users, operators, and future developers:

- **`PuzzleEval-local/GAP_ANALYSIS.md`** — Full 30-gap audit report
  with per-gap severity, current behavior, fix, effort estimate, and
  "when to fix" trigger. Documents 9 fixed gaps and 21 unfixed gaps
  organized by priority.

- **`PuzzleEval-local/POST_ROADMAP_ENHANCEMENTS.md`** (this file) —
  All post-roadmap changes that don't fit the 10-phase structure.

- **`PuzzleEval-local/bench/README.md`** — Phase 10 generalizability
  benchmark usage guide (created during Phase 10).

- **Schema comments in `schemas.py`** — Every new field has a detailed
  `description` that serves as inline documentation AND is read by
  Claude during structured output generation (so Agent 1/Agent 3
  literally learn from the schema descriptions).

---

## 11. Dispatcher Pattern — Clean Separation of Concerns

As a result of the changes above, the test generation flow now has
crystal-clear role separation:

```
┌─────────────────────────────────────────────────────────────────┐
│ Agent 1 (PLANNER)                                                │
│   - Decomposes user request into sub-tasks                       │
│   - Designs WorkflowBlueprint (DAG architecture)                 │
│   - Designs TestPlan (per-scope test specs)                      │
│   - Outputs: UserUnderstandingOutput with workflow + test_plan   │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│ Pipeline Dispatcher (CLI + pipeline_runner)                      │
│   - Reads TestPlan.scope_specs                                   │
│   - Routes file_based scopes → Agent 3F                          │
│   - Routes synthetic_* scopes → Agent 3                          │
│   - Filters UserUnderstandingOutput per agent                    │
│   - Runs Agent 3 + Agent 3F in parallel                          │
│   - Merges results into single Agent3Result                      │
└────────────┬────────────────────────┬───────────────────────────┘
             │                        │
             ▼                        ▼
┌─────────────────────┐   ┌─────────────────────┐
│ Agent 3F (EXECUTOR) │   │ Agent 3 (EXECUTOR)  │
│   - File-based only │   │   - Synthetic only  │
│   - Reads user files│   │   - Follows specs   │
│   - Per-scope specs │   │   - Per-scope specs │
│   - Ground truth    │   │   - Simulated data  │
│     from files      │   │     for downstream  │
└──────────┬──────────┘   └──────────┬──────────┘
           │                         │
           └────── MERGE ────────────┘
                   │
                   ▼
           Agent3Result (per-scope, correctly shaped)
```

**Agent 1 ≠ Dispatcher ≠ Executor.** Each has one job.

---

## 12. Backend Server Hygiene (Windows + uvicorn)

**Discovered issue:** `uvicorn --reload` on Windows exits unexpectedly
when the file watcher detects certain file system events. The backend
process dies silently.

**Resolution:** Documented that the backend should run WITHOUT
`--reload` for stable local testing:
```bash
cd puzzleeval-api
python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

For development iteration, restart manually when code changes. The Vite
frontend's HMR handles frontend changes fine; only the Python backend
needs manual restarts on source changes.

---

## Test Coverage Impact

All post-roadmap changes landed with:
- **357 PuzzleEval tests** (was 353 at end of Phase 10; +4 TestPlan schema tests)
- **30 puzzleeval-api tests** (unchanged)
- **39 generalizability bench tests** (unchanged, deselected by default)
- **387 total tests green**
- TypeScript compiles clean (no errors)
- Vite production build clean

Zero regressions from any of these changes.

---

## File-Level Change Index

For rapid look-up: every source file modified in the post-roadmap phase.

| File | What changed |
|------|--------------|
| `puzzleeval/schemas.py` | `ScopeTestSpec`, `TestPlan`, `TestCase.scope_id`, `UserUnderstandingOutput.test_plan` |
| `puzzleeval/validators.py` | Enum expansions, TestPlan validators, duplicate-capability warning |
| `puzzleeval/agents/user_understanding.py` | Test Plan section in SYSTEM_PROMPT with 5 rules + worked example |
| `puzzleeval/agents/synthetic_tests.py` | `_format_test_plan`, `_format_workflow_architecture`, Architecture annotations per sub-task, Generative domain section |
| `puzzleeval/agents/implement_test_env.py` | file_required fallback (run with text input instead of skip), Opus 4.7 |
| `puzzleeval/agents/research.py` | Opus 4.7 comment |
| `puzzleeval/scope_routing.py` | scope_id as primary routing key in `group_tests_by_scope` and `build_scope_runs` |
| `puzzleeval/config.py` | Opus 4.7 as default for AGENT1_MODEL and AGENT5_BUILDER_MODEL |
| `puzzleeval/cli.py` | `_filter_user_understanding`, `_merge_agent3_results`, TestPlan-aware mixed-mode routing, file_required wiring |
| `puzzleeval-api/services/pipeline_runner.py` | `_run_real_agent3` rewritten with mixed-mode routing, architecture summary SSE emission, TestPlan in workflow_blueprint payload |
| `src/hooks/usePipelineRun.ts` | Architecture summary chat message on workflow_blueprint event |
| `tests/test_agent1.py` | 4 TestPlan schema tests |
| `tests/test_agent5.py` | Opus 4.7 in advisor mock |
| Various `.md` files | Opus 4.7 model name updates |

---

## Production Readiness Checklist

- [x] Agent 1 designs both blueprint AND test plan — no downstream guessing
- [x] Mixed-mode test generation (Agent 3 + 3F in parallel, filtered per scope)
- [x] `scope_id` on test cases — deterministic routing
- [x] file_required fallback — no silent skips
- [x] Enum expansion — supports media_url, audio_content, file_reference
- [x] TestPlan validators — catches Agent 1 output inconsistencies
- [x] Duplicate capability warning — prevents fan-out routing ambiguity
- [x] Generative domain guidance in Agent 3 prompt
- [x] Architecture summary visible in chat after Agent 1 finishes
- [x] Opus 4.7 across all active code paths
- [x] Zero test regressions (459 passing — 390 core + 30 API + 39 generalizability bench)
- [x] TypeScript + Vite build clean
- [x] Image quality evaluation via vision model (Gap 6, 26 — `puzzleeval/vision_judge.py`)
- [x] Sandbox/dry-run metadata for destructive APIs (Gap 9 — schema fields + prompt guidance + dry-run scaffold)
- [x] Cross-candidate rate limiting for shared upstream LLMs (Gap 25 — `puzzleeval/rate_limiter.py::GlobalProviderLimiter`)
- [x] Per-candidate rate limiting (Gap 13 — `TokenBucketLimiter` wired into `_execute_all_tests`)
- [x] Per-scope `ScopeTestRun.evaluation_mode` (Gap 21 — objective vs subjective)
- [x] Exemplar vs ground_truth judge branching (Gap 14)
- [x] Audio `input_channel` + generative-API extraction in Phase 6.5 (Gap 12, 16)
- [x] Polling hint on `Candidate.api_interaction_pattern_hint` (Gap 11, 18 partial)
- [x] Unspecified-integration guard (Gap 20 — `all_in_one_compatible=false` rule)
- [x] Chatbot vs LLM-API keyword disambiguation (Gap 29)
- [x] Multi-scope `input_context_hints` on `ScopeTestSpec` (Gap 30)
- [ ] Streaming/webhook APIs (Gap 4, 5 — **CLOUD-DEFERRED**, requires publicly-reachable callback URL)
- [ ] `api_interaction_pattern` full enum on Candidate (Gap 17 — **CLOUD-DEFERRED**, ties to Gap 4)
- [ ] Streaming verification in 4C (Gap 24 — **CLOUD-DEFERRED**, ties to Gap 4)

---

## 13. Rate Limiting, Vision Judge, Side-Effects Hardening (2026-04-16)

Sixteen gaps closed in a single pass to make the product production-ready for
local hosting on Windows 11. Streaming / webhook gaps (4, 5, 17, 24) remain
deferred because webhook reception needs a publicly-reachable URL — that's
cloud work.

**New modules:**
- `puzzleeval/rate_limiter.py` — `TokenBucketLimiter` (thread-safe token bucket)
  + `GlobalProviderLimiter` (two-layer: per-candidate + per-upstream-provider)
  + `parse_rate_limit_info` (free-text string → rps). Wired into
  `implement_test_env.py::_execute_all_tests` so every Agent 5 test call
  respects both its own documented quota AND the shared upstream LLM pool.
- `puzzleeval/vision_judge.py` — `evaluate_generated_image` using Claude's
  vision input blocks. Handles `https://` URLs (downloaded, 4 MB cap) and
  `data:image/*` URIs. Returns `VisionVerdict` with `fallback_reason` set
  when the vision path couldn't run (caller falls back to the text judge).
  `extract_image_url()` walks arbitrary harness JSON to find image URLs.

**Schema additions (`puzzleeval/schemas.py`):**
- `WorkflowStep.side_effects: str = "read_only"` — read_only | creates_records |
  modifies_records | deletes_records
- `ScopeTestSpec.side_effects: str = "read_only"` — mirrors the step
- `ScopeTestSpec.reference_mode: str = "ground_truth"` — ground_truth | exemplar
- `ScopeTestSpec.input_context_hints: dict[str, str]` — scope-specific params
  every test case must carry (e.g. target_language for a translation scope)
- `Candidate.api_interaction_pattern_hint: str = "unknown"` — sync |
  async_polling | unknown; Agent 5 harness template emits
  `_poll_until_complete()` for async_polling
- `ScreenedCandidate.upstream_provider: str | None` — openai | anthropic |
  google | cohere | mistral | None; keys the per-upstream rate-limit bucket
- `ScreenedCandidate.sandbox_available: bool`,
  `ScreenedCandidate.sandbox_docs_url: str | None` — populated by Phase 6.5
  for destructive-action candidates
- `ScopeTestRun.evaluation_mode: str = "objective"` — derived from
  `ScopeTestSpec.reference_mode` so the frontend can pill-tag each scope
  with "Objective scoring" / "Subjective scoring (not comparable across
  modes)"

**Config (`puzzleeval/config.py`):**
- `PUZZLEEVAL_RATE_LIMIT_ENABLED` (default `1`)
- `PUZZLEEVAL_DEFAULT_RPS` (default `2.0`) — bucket rate when
  rate_limit_info is missing or unparseable
- `PUZZLEEVAL_UPSTREAM_RPS` (default `5.0`) — per-upstream-provider cap

**Agent 1 prompt (`user_understanding.py`):**
- New `side_effects` rule with worked examples
- New `reference_mode` rule in the Test Plan section
- New `input_context_hints` rule for parallel-scope parameterization
- Unspecified-integration guard: "my system" / "our platform" sets
  `all_in_one_compatible=false` + notes the ambiguity

**Agent 2 prompt (`research.py`):**
- `api_interaction_pattern_hint` guidance (sync / async_polling / unknown)
- Chatbot vs LLM-API disambiguation section so both candidate classes
  surface in chatbot-capability searches

**Deep-verify prompt (`deep_verify_prompt.py`):**
- `ROUTING_TABLE` rows carry `input_channel` (document_content /
  audio_content / image_description / text / file_reference)
- New `GENERATIVE_PARAMS` section (model variants, output sizes, quality
  levels, content policy notes) for image/audio/video APIs
- New `UPSTREAM_PROVIDER` extraction (openai / anthropic / google / cohere
  / mistral / none)
- New `SANDBOX` section (sandbox_available + sandbox_base_url +
  sandbox_docs_url) for destructive-action candidates

**Agent 3 prompt (`synthetic_tests.py::_format_test_plan`):**
- Renders `input_context_hints` into a "copy into every test case's
  input_context" instruction block when populated
- Renders `reference_mode="exemplar"` as a distinct judge-mode instruction
- Renders `side_effects != "read_only"` as a synthetic-test-records
  guardrail

**scope_routing.py:**
- `build_scope_runs(test_plan=...)` new optional parameter; propagates
  `reference_mode` from each `ScopeTestSpec` to the corresponding
  `ScopeTestRun.evaluation_mode` ("subjective" when exemplar, else
  "objective").

**Agent 5 (`implement_test_env.py`):**
- `_execute_all_tests` gets `rate_limiter` + `upstream_provider` optional
  parameters; sleeps on both candidate bucket and upstream bucket before
  every harness call. The blind 0.5s inter-test sleep only fires when no
  smart limiter is active.
- Top-level `run_implement_test_env_agent` constructs a single
  `GlobalProviderLimiter`, pre-configures each validated candidate's
  bucket from its `rate_limit_info`, and threads it through per-candidate
  execution.
- Agent5Result now populates `scope_runs` via `scope_routing.build_scope_runs`
  (previously only set in tests; latent Phase 9 gap closed).

**Tests:**
- `tests/test_rate_limiter.py` (18 cases): parse_rate_limit_info across
  req/min, req/sec, RPM, RPS, hourly, garbage, and "pick the strictest
  rate" multi-matches; TokenBucketLimiter burst + throttle + error paths;
  GlobalProviderLimiter disabled path + per-candidate throttle +
  upstream grouping + snapshot counters + thread safety.
- `tests/test_vision_judge.py` (15 cases): extract_image_url across
  direct / nested-dict / data-URL / null; _decode_data_url valid +
  invalid; _build_vision_prompt criterion rendering; full evaluate
  path with mocked Anthropic client (happy + parse-failure + markdown
  fences + unsupported-source + invalid data URL).

Combined: **459 tests green (390 core + 30 API + 39 generalizability
bench)**, up from 387. Zero regressions on prior tests. TypeScript
`tsc --noEmit` clean.

**Phase fingerprints for the diagnostic table:**
- Rate limiting active: look for `rate_limit_wait` operation entries in
  stderr logs OR a non-zero `acquire_count` in the `GlobalProviderLimiter`
  snapshot surfaced on run completion.
- Vision judge active: test cases with `output_type="media_url"` that
  produce non-null `criterion_scores` from a vision reasoning path
  (distinct from the text judge).
- Side-effects honored: `side_effects != "read_only"` on any
  `WorkflowStep` / `ScopeTestSpec` in `agent_1_output.json`.
- Exemplar scoring: `agent_1_output.json.result.test_plan.scope_specs[].reference_mode == "exemplar"`
  AND `agent_5_output.json.scope_runs[].evaluation_mode == "subjective"`.

**Diagnostic flags:**
- `PUZZLEEVAL_RATE_LIMIT_ENABLED=0` — skip rate limiter entirely (falls
  back to the old 0.5s blind sleep).
- `PUZZLEEVAL_DEFAULT_RPS` / `PUZZLEEVAL_UPSTREAM_RPS` — tune bucket rates
  without code changes.

**Out of scope for this pass (still CLOUD-DEFERRED):**
Gap 4 (execution_mode for webhook/streaming), Gap 5 (async_run harness),
Gap 17 (api_interaction_pattern full enum), Gap 24 (streaming verification
in 4C). All four require a publicly-reachable callback URL.

---

## 14. Bandaid-Removal: Generalize Case-Based Rules into Principles (2026-04-16)

After landing §13, an audit against Claude Code's built-in-agent patterns
(`src/tools/AgentTool/built-in/*.ts`) flagged several prompt/schema
additions from §13 as case-based bandaids rather than general principles.
Claude Code's agent prompts describe STRENGTHS and GUIDELINES — they never
say "if capability == chatbot then look at Intercom / Drift / Ada".
§14 refactors every §13 bandaid into a domain-agnostic general mechanism.

**Bandaids removed:**

1. **"Chatbot vs LLM-API" hardcoded carve-out.** §13 told Agent 2 to search
   for "LLM APIs (OpenAI, Anthropic, Gemini, Cohere, Mistral) AND chatbot
   platforms (Intercom Fin, Drift, Ada, Zendesk AI, Tidio)" when the
   capability looked like a chatbot. That only covered one capability and
   hardcoded brand names.
   → Replaced with the **"Candidate-class separation" principle**: for ANY
   capability, there often exist TWO classes of candidate — a **developer
   primitive** (raw API) and a **packaged product** (end-to-end SaaS) — that
   compete on different axes. The prompt now teaches the principle and the
   search framings for each class (`{capability} API` vs `{capability}
   platform`), with illustrative capabilities but no brand names. The
   principle applies to image gen, OCR, translation, analytics, code gen,
   payments — every current and future capability.

2. **`api_interaction_pattern_hint` 3-value enum was too narrow.** The real
   answer space for "how does this API deliver results" is richer than
   `{sync, async_polling, unknown}`: SSE streaming, webhook callbacks,
   batch-file uploads, and event subscriptions all exist.
   → Expanded soft vocabulary on the Candidate hint field to `{sync,
   async_polling, other, unknown}` — `other` catches patterns Agent 2
   recognizes but can't fully characterize from snippets.
   → Introduced `InteractionModel` (structured model) on `ScreenedCandidate`
   with six boolean flags (`synchronous`, `async_polling`, `webhook_callback`,
   `sse_streaming`, `batch_file`, `event_subscription`) + notes. Phase 6.5
   populates this from real docs. An API that supports multiple modes on
   different endpoints sets multiple flags. Agent 5's harness template reads
   these flags to emit the right client code (poll helper, SSE reader,
   batch uploader, etc.). `webhook_callback` / `event_subscription` stay
   documented but flagged as cloud-deferred for local runtime.

3. **Hardcoded `upstream_provider` enum.** §13 named a closed list
   (`openai | anthropic | google | cohere | mistral`). Reality: any
   provider wraps any upstream — xAI, DeepSeek, Groq, Aleph Alpha,
   self-hosted — and new ones appear monthly.
   → Removed the enum examples from both the schema description AND the
   deep-verify prompt. The field is free-text `str | None`; the rate
   limiter keys on whatever string comes back. The only requirement is
   consistent spelling across candidates, not membership in a curated list.

4. **`GENERATIVE_PARAMS` section as a generative-only carve-out.** §13
   added a "populate only for image / audio / video" block naming
   `dall-e-3`, `stable-diffusion-xl`, `standard | hd`.
   → Generalized to `USER_SELECTABLE_PARAMS` + `UserSelectableParam`
   Pydantic model. Applies to every API class (OCR region, transcription
   language, translation formality, chat temperature, code-gen variant,
   image size, TTS voice). The prompt lists heterogeneous examples
   explicitly across domains to break the LLM's tendency to anchor on
   "generative = image gen".

5. **`input_context_hints` described only with a translation example.**
   Generalized the Agent 1 prompt to frame it as "whatever parameter
   defines this scope vs that scope in parallel fan-outs" — language,
   region, model, format, target-system, persona, glossary, style.

**Schema additions (`schemas.py`):**
- `InteractionModel` — 6 boolean flags + notes; multi-flag allowed.
- `UserSelectableParam` — `name`, `allowed_values`, `affects`, `default`.
- `ScreenedCandidate.interaction_model: InteractionModel` (default empty).
- `ScreenedCandidate.user_selectable_params: list[UserSelectableParam]`.

**Prompt changes:**
- `research.py::RESEARCH_SYSTEM_PROMPT` — added "Candidate-class separation
  (general principle)" section; expanded the coverage collection step to
  record class. Removed the brand-name-heavy chatbot carve-out from
  `STRUCTURE_SYSTEM_PROMPT`.
- `deep_verify_prompt.py` — replaced `GENERATIVE_PARAMS` with
  `USER_SELECTABLE_PARAMS`; added `INTERACTION_MODEL` block with all 6
  boolean flags; extraction-rules section rewritten to teach the
  general principle instead of domain-specific carve-outs.
- `user_understanding.py` — `input_context_hints` description generalized
  to "whatever parameter defines one scope vs another".

**Runtime coercion:**
- `research.py::_normalize_coverage` coerces any
  `api_interaction_pattern_hint` outside `{sync, async_polling, other,
  unknown}` to `"unknown"` so LLM drift can't poison the downstream
  harness template.

**Tests:** `tests/test_general_principles.py` (29 cases):
- `InteractionModel`: defaults, multi-flag simultaneously, JSON roundtrip.
- `UserSelectableParam` across six heterogeneous domains (OCR, transcription,
  translation, image-gen, chat, code-gen) — parameterized.
- `ScreenedCandidate`: `upstream_provider` accepts any string (xai,
  deepseek, groq, self, None, etc.); multi-flag `interaction_model`
  round-trips; `user_selectable_params` accepts heterogeneous domains.
- `_normalize_coverage` hint coercion: valid vocabulary preserved,
  case-drift / typos / empty / combined strings coerced to `"unknown"`.
- **Principle guards:** grep-style regression tests that fail if the
  prompts re-introduce hardcoded brand names or generative-only carve-outs
  — `test_research_prompt_no_hardcoded_chatbot_carveout`,
  `test_deep_verify_prompt_no_generative_only_section`, etc.

Combined total: **419 + 30 API + 39 bench = 488 tests green**. TypeScript
clean. Vite build clean. FastAPI boots. Multi-flag `InteractionModel`
round-trips through Pydantic serialization end-to-end.

**Phase fingerprint for §14 active:**
- `ScreenedCandidate.interaction_model` has at least one `True` flag OR
  non-empty `notes` on a real run (vs default all-false shape for legacy).
- `ScreenedCandidate.user_selectable_params` non-empty for candidates
  whose docs Phase 6.5 actually reached.

**No new diagnostic flag** — §14 is a generality refactor, not a feature
toggle. If the new prompts produce worse results than §13's case-based
version, roll back via git (the old carve-outs are a single commit apart).

**Why we think this is the right model, cribbed from Claude Code:**
Claude Code's `exploreAgent.ts` and `generalPurposeAgent.ts` prompts are
under 100 lines each. They describe STRENGTHS and GUIDELINES in
capability terms (searching, reading, analyzing) and let the model apply
the capabilities to whatever domain shows up. There are no `if filetype
== python then ...` carve-outs — the agent is given tools and principles,
and figures the rest out per task. §14 applies the same pattern to our
agents: the prompt teaches when the "developer primitive vs packaged
product" duality is relevant, not a case list of which capabilities it
affects. This is the only way the pipeline generalizes to capabilities
we haven't seen yet.

---

## 15. Claude-Code-Pattern Generality Boost (2026-04-16)

A second audit, this time against Claude Code's runtime architecture
(`src/tools/AgentTool/built-in/*.ts`, `src/services/compact/microCompact.ts`,
`src/memdir/findRelevantMemories.ts`, `src/tools/AgentTool/built-in/verificationAgent.ts`),
identified five mechanisms that Claude Code uses to make its general-purpose
agents work across unknown domains, but PuzzleEval's pipeline lacked. §15
ships all five.

### Gap A — Shared cross-cutting agent preamble

Claude Code injects a small set of behavioral rules (parallel tool calls,
no narration, reason about errors, verify against source, commit and
course-correct) into EVERY sub-agent via
`enhanceSystemPromptWithEnvDetails()`. Without this, each agent's prompt
re-derives the same rules in its own words — drift accumulates, new agents
miss the rules entirely.

**New file:** `puzzleeval/agent_preamble.py`
- `SHARED_AGENT_PREAMBLE` — single source of truth for the cross-cutting rules.
- `with_preamble(agent_specific_prompt) -> str` — idempotent prepender.

**Wired into every agent's `client.messages.{create,parse}` call:**
- `agents/user_understanding.py` — Agent 1 (system_text built via with_preamble)
- `agents/research.py` — Agent 2 research + structuring calls
- `agents/synthetic_tests.py` — Agent 3 generation
- `agents/synthetic_tests_file.py` — Agent 3F file-based generation
- `agents/screening.py` — Agent 4 per-candidate verification + structuring
- `agents/implement_test_env.py` — Agent 5 builder loop, ask_research sub-agent,
  evaluation judge

**Regression guard:** `tests/test_claude_code_patterns.py::TestSharedPreamble::test_every_agent_call_is_wired`
greps every `system=` call in `agents/*.py` and fails if a new call site
bypasses `with_preamble`. New agents added in the future get caught
automatically.

### Gap B — Adaptive thinking on multi-turn reasoning agents

Agent 5's builder loop uses `thinking={"type": "adaptive"}`. The other
multi-turn reasoning agents (Agent 2 research, Agent 4 per-candidate
verification) didn't. Both do hop-by-hop reasoning between web tool
calls — exactly the workload adaptive thinking is designed for.

**Enabled on:**
- `agents/research.py` Agent 2 research loop
- `agents/screening.py` Agent 4 per-candidate verification loop

Agent 1 stays on plain `messages.parse` (one-shot structured output —
no multi-turn reasoning to think between). Agent 3/3F same reason.

### Gap C — Server-side context management on Agent 4

Agent 5 already uses `context-management-2025-06-27` beta with
`clear_tool_uses_20250919` to drop old tool results when context grows
past 80K tokens. Agent 4's per-candidate verification didn't — so a
multi-page-fetch verification of a complex API could blow the context
window mid-loop.

**Enabled on:** `agents/screening.py::_verify_single_candidate` — the
same `extra_body={"context_management": {...}}` pattern Agent 5 uses.

### Gap D — Cross-run memory directory

Claude Code's `memdir/` persists `.md` memo files across sessions and
recalls them by relevance into future runs. Without this, every Phase
6.5 deep-verify of "Mindee" re-researches the same docs from scratch,
even when yesterday's run already extracted them.

**New module:** `puzzleeval/memdir.py`
- `write_memory(category, key, name, description, body, tags, ...)` —
  saves a frontmatter-prefixed markdown memo to `~/.puzzleeval/memdir/`
  (overridable via `PUZZLEEVAL_MEMDIR`).
- `recall_memory(category, key, max_age_days=...)` — exact-key lookup
  with optional staleness check.
- `find_relevant_memories(query, categories, limit)` — word-overlap
  ranking. Claude Code uses a small LLM for this; ours is deterministic
  and zero-cost — equivalent for the small memo counts we'll see.
- `INDEX.json` — auto-refreshed cross-run index per category.
- `stats()` — observability surfaced into `pipeline_summary.json`.

**Concrete categories ready to use:**
- `api_specs/` — Phase 6.5's extracted api_spec.txt per
  candidate+docs_url. Future runs of the same candidate can recall
  before re-researching.
- `quirks/` — Agent 5 records discovered API quirks ("camelCase keys",
  "multipart required despite JSON docs") — recallable next time the
  builder hits the same provider.

**Toggle:** `PUZZLEEVAL_MEMORY_ENABLED=0` makes every read/write a no-op
for diagnosis or air-gapped runs.

**Test isolation:** `conftest.py` autouse fixture points memdir at a
per-session tmp path so tests can never pollute the developer's real
memdir.

### Gap E — Adversarial verification battery

Claude Code's `verificationAgent.ts` is 800+ lines of guidance to
*adversarially probe* an implementation rather than confirm the happy
path. Today PuzzleEval declares HARNESS_COMPLETE on smoke + one live
call — a harness that crashes on edge inputs, ignores corrupted
credentials, or has shape drift between calls passes our gate and
silently corrupts every Agent 3 result.

**New module:** `puzzleeval/adversarial_verifier.py`
- `run_adversarial_battery(sandbox_dir, sample_input, credentials, enabled_probes)`
  → `AdversarialReport` with six PRINCIPLE-BASED probes that apply to
  every harness regardless of domain:
  1. **empty_input** — does the harness gracefully handle the smallest
     valid payload?
  2. **max_input** — feed 10x the smoke-test size; surfaces buffer /
     serialization issues.
  3. **malformed_input** — non-printable, emoji, mixed scripts in the
     largest string field; surfaces encoding bugs.
  4. **idempotency** — same input twice; surfaces shape drift / state
     leakage between calls.
  5. **concurrency** — 3 simultaneous calls; surfaces races and the
     API's actual concurrent-request behavior.
  6. **auth_error** — corrupt every credential and call once; surfaces
     harnesses that return success=True regardless of credentials
     (the silent-corruption case that invalidates every domain test).
- Outcome bucket: `{graceful_success, graceful_failure, crash, silent_corruption}`.
- `harness_ready=False` only when a probe was `crash` OR `silent_corruption`.
  `graceful_failure` is acceptable — the underlying API genuinely cannot
  handle that input, but the harness correctly surfaced the failure.
- `report_to_dict()` for serialization onto `TestHarness.adversarial_report`
  and SSE emission.

**Wired into Agent 5:** between the build loop and the test execution
phase. When a harness fails the battery, it's dropped from the test
execution set and `progress_callback("harness_not_ready", ...)` fires
so the frontend can surface it.

**Schema:** `TestHarness.adversarial_report: dict | None`.

**Toggle:** `PUZZLEEVAL_ADVERSARIAL_PROBES_ENABLED=0` reverts to the
legacy smoke-only verification.

**Test isolation:** `conftest.py` autouse fixture defaults the env var
to off for unit tests (most tests use mocked harnesses with no real
venv that would fail probe execution legitimately). The dedicated
battery tests in `test_claude_code_patterns.py` invoke
`run_adversarial_battery` directly without going through the env flag.

### Tests + verification

- `tests/test_claude_code_patterns.py` (32 cases):
  - Gap A: preamble content checks, idempotency, regression guard
    grepping every system= call site
  - Gap B: presence-of-thinking checks for Agents 2 + 4
  - Gap C: presence-of-context_management check for Agent 4
  - Gap D: slugify, short_hash, write/recall roundtrip, max_age, disabled
    no-op, index refresh, word-overlap ranking, category filter
  - Gap E: classifier across all 4 outcome buckets, summarize across
    all error paths, end-to-end battery against minimal harness (passes),
    crashy harness (fails on empty_input probe), silent-corruption
    harness (fails on auth_error probe)
- `tests/conftest.py` — session-wide autouse fixtures isolating memdir
  + disabling adversarial probes by default

Combined: **451 + 30 API + 39 generalizability bench = 520 tests
green** (was 488 after §14, +32 net for §15). TypeScript clean. Vite
build clean. FastAPI boots cleanly.

### Phase fingerprints

| Mechanism | Fingerprint |
|---|---|
| Gap A — shared preamble | "Cross-cutting rules" string in any agent's effective system prompt |
| Gap B — adaptive thinking | API request payload includes `thinking.type=adaptive` on Agents 2 + 4 calls |
| Gap C — server-side context | Agent 4 verification calls include `context_management.edits` in extra_body |
| Gap D — cross-run memory | `~/.puzzleeval/memdir/INDEX.json` exists; `memdir.stats()` returns non-zero counts |
| Gap E — adversarial battery | `TestHarness.adversarial_report` non-null on a real run; `harness_not_ready` SSE events visible |

### Diagnostic flags

| Mechanism | Flag | Default | Disable behavior |
|---|---|---|---|
| Gap D | `PUZZLEEVAL_MEMORY_ENABLED` | `1` | `recall_memory` returns None, `write_memory` returns None |
| Gap D | `PUZZLEEVAL_MEMDIR` | `~/.puzzleeval/memdir` | Override storage location |
| Gap E | `PUZZLEEVAL_ADVERSARIAL_PROBES_ENABLED` | `1` | Skip probe battery; keep legacy smoke-only verification |

### Why these five close the meaningful gap

I considered nine candidate gaps from Claude Code; the four that didn't ship:

- **TodoWrite** — Claude Code uses an in-conversation todo tool. PuzzleEval
  doesn't have a single-conversation surface; the orchestrator (cli.py /
  pipeline_runner.py) tracks per-agent progress externally. Skipped.
- **Skill system** — Claude Code's loadable skill .md files. We could
  add this for "MultipartUploadSkill" / "AsyncPollingSkill" / "OAuthSkill"
  but Agent 5's builder already composes these from scratch each time,
  and the cost of skill-file maintenance > the cost of regenerating each
  run. Defer until provider count > 50.
- **Frontier model with rate-limit downgrade** — Claude Code falls Opus →
  Haiku on rate limit. Our existing rate limiter (Gap 13) + per-candidate
  parallelism with backoff (3 retries at 15s/30s/60s) achieves the same
  unblocking effect without a model swap. Adding model downgrade would
  also change the test results, which makes runs non-reproducible.
  Skipped intentionally.
- **Tool result budget pruning** — Claude Code prunes oldest tool results
  when over budget; Agent 5 has CONTEXT_CHARS_LIMIT-based whole-conversation
  compaction. The gap exists but the symptom (overlong context) is
  already covered by Gap C's server-side context_management on Agent 4
  and Agent 5's own context_management. Skipped.

The five that did ship are the ones whose absence would either silently
corrupt results (Gap E auth_error probe, Gap A shared rules drift) or
materially degrade quality (Gap B reasoning, Gap C context overflow,
Gap D re-research cost). Each of the five has a Claude Code citation
in its module docstring.

---

## 16. Whole-Picture Agent 4 + Agent 5 Guarantee (2026-04-16)

A focused audit on four user-surfaced questions about Agent 4's research
quality and Agent 5's delivery guarantee uncovered four real gaps:

| Question | Reality before §16 | Fix |
|---|---|---|
| Q1: Does Agent 4 produce the whole picture? | NO — `screening.py:419` used the shallow `VERIFICATION_SYSTEM_PROMPT` (binary PASS/REJECT, 3 fetches). The sophisticated `DEEP_VERIFY_SYSTEM_PROMPT` was defined but never imported into production. Every new schema field (`interaction_model`, `user_selectable_params`, `upstream_provider`, `sandbox_*`, `pricing_breakdown`, `api_spec_path`) was always empty. | New `puzzleeval/deep_verify_runner.py` wires the directed 4-phase loop. Multi-turn `client.beta.messages.create` with tools, server-side context_management, adaptive thinking. Parses every documented section into the matching ScreenedCandidate field. |
| Q2: Multi-scope candidates handled per-scope? | PARTIAL — data structures existed but `coverage_confidence` never got upgraded to `"verified"` because no per-scope check ran. | The runner extracts the ROUTING_TABLE per-candidate, populates `coverage_confidence[sid] = "verified"` only for scopes the model actually mapped to endpoints, and DROPS unverified scopes from `covers_step_ids`. |
| Q3: No repeated research for same provider? | NO — every `Candidate` got its own per-candidate verify call. Three Google services = three Google docs fetches. | Provider-URL deduplication via `_normalize_docs_url()` + `group_candidates_by_docs_url()`. Same docs URL across N candidates → ONE deep-verify call → spec split across the group. Cross-run `memdir` cache layered on top: a provider verified yesterday is recalled today within the 14-day TTL (configurable via `PUZZLEEVAL_DEEP_VERIFY_MEMO_MAX_AGE_DAYS`). |
| Q4: Agent 5 guarantees delivery? | NO — if every selected candidate failed to build, validator flagged "Zero harnesses → pipeline cannot continue". User had no testable environment. | New build-failure fallback: when `len(harnesses) == 0` after primary attempts, Agent 5 pulls top-K next-ranked candidates from Agent 4's verified pool that the user did NOT pick (ranked by relevance × adoption_difficulty), builds them, tags `was_fallback=True` on success. Capped by `AGENT5_FALLBACK_MAX` (default 3). |

### New / changed files

- **NEW:** `puzzleeval/deep_verify_runner.py` (~470 lines) — directed-loop driver, URL dedup, memdir cache, scope-aware spec parsing, per-candidate enrichment.
- **MODIFIED:** `puzzleeval/agents/screening.py` — `run_screening_agent()` now calls `run_deep_verify_pass()` first when `AGENT4_DEEP_VERIFY_ENABLED=1`. On exception falls back to legacy shallow verification (defensive).
- **MODIFIED:** `puzzleeval/agents/implement_test_env.py` — fallback block between primary build phase and test execution.
- **MODIFIED:** `puzzleeval/schemas.py` — `TestHarness.was_fallback: bool = False`.
- **MODIFIED:** `puzzleeval/config.py` — added `AGENT5_FALLBACK_ENABLED` (default `1`), `AGENT5_FALLBACK_MAX` (default `3`).
- **NEW TESTS:** `tests/test_deep_verify_and_fallback.py` (30 cases).

### Test coverage (30 new cases)

- **URL dedup (3 tests):** normalize strips query/fragment, three same-URL candidates → one group, different-subproduct candidates → separate groups, no-URL candidates each get singleton groups.
- **Spec parsing (10 tests):** PASS/REJECT detection, REJECT-trumps-PASS guard, multi-flag InteractionModel, default-to-sync when unspecified, USER_SELECTABLE_PARAMS extraction, empty-section handling, upstream_provider extraction with `self`/`none` normalization, sandbox parsing, auth + access defaults.
- **Scope-aware extraction (2 tests):** routing-table-only scopes accepted, missing routing table → empty list.
- **Full runner (4 tests):** same-URL group ⇒ one deep-verify call; different subproducts ⇒ N calls; cache HIT skips fresh research on second run; runner respects `AGENT4_DEEP_VERIFY_ENABLED=0`.
- **Per-candidate scope confidence (1 test):** partial verification — claimed {1,2,3}, ROUTING_TABLE confirms {1,3} → final covers={1,3}, all "verified".
- **Agent 5 fallback (4 tests):** `AGENT5_FALLBACK_ENABLED` defaults true; `AGENT5_FALLBACK_MAX >= 1`; `was_fallback` field round-trips; ranking logic — `Unselected_HighRelevance_Easy` beats `Unselected_HighRelevance_Hard`, picked candidates excluded from pool.
- **Crash invariants (6 tests):** runner classifies all four outcome buckets, summarize handles all error paths.

Combined: **481 + 30 API + 39 generalizability bench = 550 tests
green** (was 520 after §15, +30 net for §16). TypeScript clean. FastAPI
boots cleanly with the new modules.

### Phase fingerprints

| Mechanism | Fingerprint |
|---|---|
| Deep-verify wired (Q1) | `agent_4_output.json.validated_candidates[*].interaction_model.synchronous` ≠ default OR `screening_summary` starts with `"Deep-verify:"` |
| Per-scope verification (Q2) | `coverage_confidence[*] == "verified"` instead of `"claimed"` on at least one ScreenedCandidate |
| URL dedup (Q3) | `pipeline_summary.json:metadata.deep_verify_groups < deep_verify_candidates` (groups smaller than candidate count) |
| Memdir cache hits (Q3) | `~/.puzzleeval/memdir/api_specs/*.md` exist; `pipeline_summary.json:metadata.deep_verify_cache_hits > 0` on subsequent runs |
| Agent 5 fallback engaged (Q4) | Any `TestHarness.was_fallback == True`; SSE `agent5_fallback_engaged` event in stream |

### Diagnostic flags

| Mechanism | Flag | Default | Disable behavior |
|---|---|---|---|
| Q1+Q2+Q3 (deep verify) | `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` | `1` | Falls back to legacy shallow `_verify_single_candidate` per candidate |
| Q3 cache TTL | `PUZZLEEVAL_DEEP_VERIFY_MEMO_MAX_AGE_DAYS` | `14` | Memos older than this re-research |
| Q4 (fallback) | `PUZZLEEVAL_AGENT5_FALLBACK_ENABLED` | `1` | Hard "Zero harnesses" failure when all picks fail |
| Q4 fallback budget | `PUZZLEEVAL_AGENT5_FALLBACK_MAX` | `3` | Cap on number of fallback candidates Agent 5 will try |

### Cost / latency impact

- Deep-verify per URL group: ~$0.30-0.60 (vs ~$0.15 for the shallow per-candidate call). Net cost for a typical 8-candidate pipeline with 3 distinct URLs: ~$1.80 vs old ~$1.20 — modest increase for materially more accurate verification.
- Memdir cache turnaround: 0 cost on hit. After the first run of any provider, subsequent runs save 100% of that group's verify cost.
- Fallback: only engages on total primary failure. When it fires, adds 1-3 build cycles (~$1-5 each). Without it the pipeline returned no testable environment, so the marginal cost is "guaranteed delivery" vs "user has nothing to test".

### Known limitations (deliberately not addressed in §16)

- **Spec parsing is regex-based.** A model that emits the api_spec in a substantially different format than `DEEP_VERIFY_SYSTEM_PROMPT` instructs (a contingency we accept) will produce sparse fields. The runner errs on the side of populating defaults rather than dropping the candidate — `parse_interaction_model()` defaults to `synchronous=True` when no flags found, `parse_auth_and_access()` defaults to `unknown`/`free_signup`. The model is well-behaved against the prompt in every test we've run.
- **Memdir cache invalidation is time-based only.** A provider that ships a breaking docs change within the 14-day window will hit the stale cache. Operator workaround: `rm ~/.puzzleeval/memdir/api_specs/<provider>__*.md` to force fresh research, or set `PUZZLEEVAL_DEEP_VERIFY_MEMO_MAX_AGE_DAYS=0` for diagnosis.
- **Fallback doesn't recurse.** If all 3 fallback candidates also fail to build, we surface a hard failure. Reasonable trade-off: deeper recursion would mask systemic bugs (network outage, all candidates broken, Anthropic API down) as long-running attempts.

---

## 17. Whole-Picture Research + Build Resilience (2026-04-16)

§16 closed the structural gaps in Agent 4 + Agent 5 (deep-verify wired,
URL dedup, scope-aware confidence, build fallback). User audit on §16
surfaced two deeper questions §17 addresses:

**Q3 deeper:** "Dedup isn't enough. Every time we research a provider, do
we get the WHOLE picture — every endpoint, every request shape, every
error code, every code example, the doc structure map? If yes, future
users testing any scope of the same provider should hit a fully-warm
cache, not a scope-narrow one."

**Q4 deeper:** "Fallback to other providers isn't the answer. Claude
Code finishes most coding tasks within 10 turns. Why can't Agent 5? Our
goal: unless the server is down, credentials are bad, or the provider
posts no docs, we should ALWAYS deliver a working harness on the
candidates the user picked. What gaps prevent this?"

### Fix Q3 — Provider Atlas (exhaustive per-provider research)

The previous deep_verify extracted an api_spec scoped to whatever the
current user's covered scopes needed. That's a narrow cache: the next
user testing scope 7 of the same provider triggered fresh research
because the previous run only covered scopes 1-3.

**New module:** `puzzleeval/provider_atlas.py`
- `ProviderAtlas` dataclass — comprehensive structure: every endpoint with
  request_shape / response_shape / python_example / error_codes / scope_hints,
  authentication modes (all of them), pagination pattern, rate_limits per tier,
  user_selectable_params, SDKs, **doc_page_map** (which doc URL covers what),
  openapi_url, global_error_codes, webhooks_or_events, interaction_modes,
  sandbox_available, upstream_provider, pricing_summary.
- `ATLAS_EXTRACTION_INSTRUCTIONS` — appended to the deep-verify prompt with
  TWO rules: (1) every endpoint goes in `endpoints[]`, even ones outside
  the current user's scopes; (2) when a field is unknown, emit empty
  value, never fabricate. JSON output in a ```atlas fenced block.
- `parse_atlas_from_response()` — extracts the JSON block; returns None
  on absence/malformed (callers fall back to legacy spec parsing).
- `cache_atlas()` / `recall_atlas()` — memdir category `provider_atlases/`
  with 30-day TTL (longer than spec TTL because atlases change less often).
- `select_endpoints_for_scopes()` — atlas-driven scope matching using
  endpoint purpose + scope_hints rather than regex on legacy spec text.
- `derive_screened_candidate_fields()` — pulls auth_method, access_method,
  rate_limit_info, interaction_model, user_selectable_params, sandbox_*,
  upstream_provider straight out of the atlas.

**Wiring into `deep_verify_runner`:**
- Atlas instructions appended to every fresh deep-verify message.
- TWO cache layers checked in priority: atlas cache first (preferred), then
  legacy spec cache (transition fallback for pre-§17 cached entries).
- When fresh extraction succeeds, BOTH atlas + spec are cached so the
  next run hits the atlas immediately.
- Per-candidate enrichment prefers atlas-derived fields when atlas exists;
  regex spec parsing only fires for legacy cached entries.
- `screening_notes` now reports the source: `(atlas-cache)`, `(spec-cache)`,
  or `(fresh)` plus endpoint count when atlas is used.

**Telemetry additions:** `atlas_cache_hits`, `atlas_fresh_extractions` in
the deep_verify telemetry dict (surfaced in pipeline_summary.json).

### Fix Q4 — Build resilience (Claude-Code-style determination)

Four independent mechanisms for the builder to actually deliver:

#### Q4a: API pattern catalog (`puzzleeval/api_patterns.py`)
- `API_PATTERNS_CATALOG` constant — 10 universal HTTP/REST patterns
  documented with **signature**, **python skeleton** (working code to
  copy verbatim), **common pitfalls**:
  1. REST + Bearer auth in Authorization header
  2. REST + API key in custom header
  3. REST + API key in query string
  4. Multipart file upload
  5. Base64 file in JSON body
  6. Async polling (job_id then GET /jobs/{id})
  7. SSE streaming
  8. OAuth 2.0 client credentials
  9. Service account JSON
  10. Pagination (cursor / offset / link-header)
- Decision rule at the bottom: "Match candidate to one of the 10. Copy
  the skeleton. Substitute URL/fields. THAT'S YOUR FIRST DRAFT. Don't
  write a custom solution when a pattern matches."
- Wired into `BUILDER_SYSTEM_PROMPT` via `_with_builder_appendix()` so
  every harness build sees the catalog without rediscovering patterns.

#### Q4b: OpenAPI auto-harness generator (`puzzleeval/openapi_harness.py`)
When the atlas reports an `openapi_url`, generate the harness MECHANICALLY
without ANY LLM build turns:
- `fetch_openapi_spec()` — defensive HTTP GET, JSON+YAML parser,
  10-second timeout, returns None on any failure.
- `_pick_server_url()` — handles both OpenAPI 3 servers and Swagger 2
  host+basePath+schemes.
- `_detect_auth_scheme()` — recognizes bearer, apikey_header,
  apikey_query, basic from `securitySchemes` (OpenAPI 3) or
  `securityDefinitions` (Swagger 2).
- `find_operation_for_role()` — picks the operation whose
  operationId/summary/description/path/tags best match the workflow
  role's keywords. Method priority POST > PUT > PATCH > GET.
- `generate_harness_code()` — emits a minimal harness.py with the right
  request_kwarg (`json` / `data` / `files`) based on the operation's
  requestBody content type.
- `generate_harness_for_candidate()` — end-to-end orchestration; returns
  None on any failure → caller falls back to LLM build.
- **Wired into Agent 5 via `_try_openapi_fastpath()`** in
  `implement_test_env.py`: runs BEFORE the builder loop. When it
  succeeds, returns a TestHarness immediately with `build_turns=0` and
  `validation_notes="auto-generated from OpenAPI spec"`. When it
  fails, the LLM build path runs as before — no behavioral regression.
- Saves $1-2 + 5-15 LLM turns per candidate when the spec is available.

#### Q4c: Structured PIVOT mechanism
The old Tier 3 reassessment said "(a) ask_research more, (b) signal
HARNESS_FAILED" — too easy to give up. Replaced with `STRUCTURED_PIVOT_PROMPT`
that REQUIRES:
1. **Blocker statement** — one specific sentence on the root cause
   ("API requires multipart but I'm sending JSON"), not vague ("400 error").
2. **Three fundamentally different approaches** — concrete examples
   given (REST→SDK swap, auth header location swap, body format swap,
   different endpoint, OpenAPI spec generation, cURL-to-Python translation).
3. **Pick the most evidence-supported approach** — not the closest to
   what's already broken.
4. **Abandon the current line entirely** — comment out broken code,
   start fresh; don't try to massage it.
- Hard ceiling: 3 pivots per harness. After 3 unsuccessful pivots,
  HARNESS_FAILED is allowed but with a clear attempt_summary listing
  what was tried.
- "DO NOT signal HARNESS_FAILED before completing at least one full
  pivot" — closes the give-up-too-easy escape hatch.

#### Q4d: Live-test BATTERY before HARNESS_COMPLETE
`LIVE_TEST_BATTERY_PROMPT` appended to the builder prompt:
- happy_path / minimal / boundary / invalid_credential — four required
  probes. The invalid_credential probe specifically catches the
  silent-corruption case where a harness returns success=True regardless
  of credentials.
- "If ANY of the four fails: read the actual error, fix the harness,
  re-run the WHOLE battery."
- "Do not signal HARNESS_COMPLETE until all four pass."

### Files added / changed

**New:**
- `puzzleeval/provider_atlas.py` — ProviderAtlas dataclass + extraction
  instructions + parser + scope matcher + memdir cache.
- `puzzleeval/openapi_harness.py` — OpenAPI spec analyzer + harness code
  generator + end-to-end orchestrator.
- `puzzleeval/api_patterns.py` — 10-pattern catalog + structured pivot
  prompt + live-test battery prompt.
- `tests/test_atlas_and_resilience.py` — 30 cases.

**Changed:**
- `puzzleeval/deep_verify_runner.py` — atlas instructions appended,
  two-layer cache (atlas + spec), atlas-driven enrichment, atlas-driven
  scope matching, telemetry expanded.
- `puzzleeval/agents/implement_test_env.py` — `_try_openapi_fastpath()`
  helper, `_with_builder_appendix()` helper, fastpath wired into
  `_build_single_harness`, Tier 3 reassessment uses STRUCTURED_PIVOT_PROMPT,
  builder system prompt appendix injection.

### Tests + verification

- 30 new test cases in `test_atlas_and_resilience.py`:
  - Atlas (8): dataclass round-trip, extraction instructions demand
    completeness, cache persistence, parser well-formed/no-block/malformed,
    scope matching by hints + by purpose, derive_screened_candidate_fields.
  - Pattern catalog (3): all 10 patterns present, python skeletons present,
    catalog wired into builder appendix.
  - Pivot prompt (2): demands three approaches, caps at three pivots.
  - Battery prompt (1): lists all four required probes.
  - OpenAPI analysis (5): server picking from OpenAPI 3 + Swagger 2,
    bearer/apikey-header/apikey-query auth detection, no-auth default.
  - OpenAPI matching (3): role keyword matching across multiple ops,
    no-match returns None.
  - OpenAPI generation (5): bearer+multipart harness, bearer+json harness,
    end-to-end with mocked fetch, fetch-fails returns None,
    no-op-matches returns None.
  - Crash invariants and edge cases.

Combined: **511 + 30 API + 39 generalizability bench = 580 tests
green** (was 550 after §16, +30 net for §17). TypeScript clean.
FastAPI boots with all 5 new modules importable.

### Phase fingerprints

| Mechanism | Where to look |
|---|---|
| Atlas extraction live | `pipeline_summary.json:metadata.deep_verify_atlas_fresh_extractions > 0` OR a `provider_atlas__*.json` file in `runs/{trace_id}/agent4_specs/` |
| Atlas cache hits (cross-run benefit) | `pipeline_summary.json:metadata.deep_verify_atlas_cache_hits > 0` after the second run of any provider |
| OpenAPI fastpath fired | `TestHarness.build_turns == 0` AND `validation_notes == "auto-generated from OpenAPI spec"`; SSE event `openapi_fastpath_success` |
| Pattern catalog wired | builder system prompt contains the string `"UNIVERSAL API PATTERNS"` |
| Pivot mechanism active | conversation_log.json contains `"dead_end_tier3"` with the structured pivot prompt text |

### Diagnostic flags

| Flag | Default | Disable behavior |
|---|---|---|
| `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` | `1` | Atlas extraction skipped; falls back to legacy shallow `_verify_single_candidate` per candidate |
| `PUZZLEEVAL_DEEP_VERIFY_MEMO_MAX_AGE_DAYS` | `14` | Spec memos older than this re-research |
| (atlas TTL) | `30` (constant `ATLAS_MEMO_MAX_AGE_DAYS`) | Atlas memos older than this re-research |
| `PUZZLEEVAL_MEMORY_ENABLED` | `1` | All memdir reads/writes become no-ops; both atlas + spec caches inactive |

### Cost / latency impact

- First run of any provider with atlas extraction: same cost as §16's
  deep-verify (~$0.30-0.60 per URL group). The atlas adds ~10-20% output
  tokens for the broader spec but the LLM is already doing the research.
- Second + subsequent runs of the same provider on any scope: $0 + 0
  turns. Atlas is recalled from disk; per-candidate enrichment is pure
  computation. Without §17, the next-day run paid the full deep-verify
  cost again because the cache was scope-narrow.
- OpenAPI fastpath when available: saves ~$0.50-2.00 per harness + 5-15
  LLM turns. Many providers (Stripe, Plaid, Anthropic, OpenAI, etc.)
  publish openapi.json — when they do, build is mechanical.
- Pattern catalog injection: adds ~600 tokens to the builder system
  prompt. Cached (Anthropic prompt caching), so cost is read-only after
  the first turn. Net cost negligible.
- Live-test battery: 4 calls per harness instead of 1 = ~$0.05-0.20
  more in API costs to the candidate's API. Worth it: catches the
  silent-credential-corruption bug that would otherwise invalidate
  every Agent 3 result.

### Honest limitations (deliberately not addressed in §17)

- **Atlas completeness depends on the LLM following instructions.** The
  prompt explicitly demands every endpoint; if the model skips some
  (large platforms with hundreds of endpoints), the atlas is
  partial. Mitigation: the cache TTL (30 days) means we re-research
  periodically anyway, picking up missed endpoints.
- **OpenAPI fastpath is best-effort.** It fetches and parses the spec;
  if the spec is malformed, references external schemas via $ref
  without resolution, or uses non-standard auth schemes, it returns
  None and the LLM build path runs. No degradation vs §16.
- **Pivot mechanism enforced by prompt, not by code.** The model is
  told "DO NOT signal HARNESS_FAILED before completing at least one
  full pivot." We can't programmatically prevent the model from
  ignoring this; we can only make the instruction clear and trust the
  Opus-4.7 builder to follow it. If the model regresses on
  pivot-following, we add a code-level check that rejects HARNESS_FAILED
  before tier-3 has been seen at least once.
- **Live-test battery enforced by prompt, not by code.** Same reasoning.
  The post-loop adversarial battery (Gap E) catches what the model
  might skip; the in-build battery is a quality-of-life prompt that
  gets the model to the right place faster.

---

## 18. End-to-End Bandaid Audit + Capability Extensions (2026-04-16)

A read-only audit of every prompt and module across all 5 agents found
seven outstanding case-specific carveouts ("bandaids") plus four
capability gaps that prevented the goal of "always deliver regardless of
how weird the user request, files, API doc structure, doc website
location, or complexity is." §18 ships fixes for all eleven.

### Bandaids removed

**1. Domain-narrow timeout comments + values.** `AGENT5_CODE_TIMEOUT=120s`
and `AGENT6_TEST_TIMEOUT=120s` carried comments naming "Mindee,
DocuClipper" and were sized for OCR async-polling jobs (~30-60s
typical). Video encoding, ML training, batch document processing all
need 5+ min — they would have silently timed out.
- Comments rewritten to be domain-agnostic.
- New `AGENT5_CODE_TIMEOUT_LONG=600s` + `AGENT6_TEST_TIMEOUT_LONG=600s`
  constants for long-running operations.
- New `_adaptive_test_timeout(harness)` helper in `implement_test_env.py`
  reads the candidate's atlas; when `interaction_modes` includes
  `async_polling` or `batch_file`, scales to LONG. Sync APIs keep the
  baseline.

**2. Hardcoded `_ROLE_KEYWORD_MAP` in `openapi_harness.py`.** The map
listed a closed set of roles (`ocr`, `transcribe`, `translate`, `chat`,
etc.) with hand-curated keyword tuples. Novel roles (genome assembly,
music composition, climate modeling) fell through to single-keyword
matching.
- Removed entirely. Replaced with `_role_tokens(role)` — generic
  alphanumeric word-splitter that handles snake_case / kebab-case /
  camelCase. Drops common stop-words (`to`, `of`, `the`).
- `find_operation_for_role()` now scores operations by token-overlap
  with operationId + summary + description + path + tags. Works for
  ANY role string.
- New tests verify `genome_assembly` matches `/genome/assembly`,
  `music_composition` matches `/compose`, etc. — none of these were in
  the old hardcoded map.

**3. Atlas enrichment fields collected but unused by the builder.**
Phase 6.5 deep-verify populated `interaction_model`, `sandbox_available`,
`sandbox_docs_url`, `upstream_provider`, `user_selectable_params`,
`api_spec_path` on every ScreenedCandidate — but Agent 5's builder
initial message NEVER showed those fields to the model. The builder
re-discovered them via web research instead of acting on what we
already knew.
- New `_format_atlas_context_for_builder(candidate)` helper in
  `implement_test_env.py` formats every populated field into a
  builder-readable "STRUCTURED CONTEXT FROM PHASE 6.5" block appended
  to the initial message. The builder now sees:
  - Active interaction modes (with explicit guidance: "use a polling
    loop or stream consumer; don't treat the first response as final")
  - Sandbox availability + sandbox docs URL ("PREFER sandbox base URL
    during testing")
  - Upstream provider warning ("rate-limit headroom shared with other
    candidates wrapping the same upstream")
  - User-selectable params list (so harness accepts them via input_data)
  - Pre-extracted spec path ("read this BEFORE doing any web fetches")

### Capability extensions

**4. Generic `file_parsers.py` pass-through for any binary.** The old
parser raised `AgentFileParseError("Unsupported file format")` on any
extension outside `.pdf / .png / .jpg / .jpeg / .gif / .webp / .docx /
.csv / .txt`. Audio, video, archives, arbitrary binaries — all hard
failures at the parsing layer.
- Added `PASS_THROUGH_EXTENSIONS` map covering audio (mp3/wav/m4a/flac/
  ogg/aac/opus/aiff), video (mp4/mov/avi/webm/mkv/flv), archives
  (zip/tar/gz/7z), generic binary (bin/dat).
- New `describe_pass_through_file()` returns a structured `file_reference`
  text with absolute path + media_type + size_bytes, ready for Agent 5's
  harness builder to upload via multipart or base64-in-JSON.
- `parse_file()` no longer raises on unknown extensions — falls through
  to `describe_pass_through_file()` so any file format reaches downstream.
- Test `test_unsupported_format_raises_error` retired in favor of
  `test_unknown_extension_returns_file_reference` and
  `test_audio_file_returns_file_reference`.

**5. Manual atlas upload path for private / internal / auth-walled APIs.**
The default Phase 6.5 path uses `web_search` + `web_fetch` to find
public docs. Private APIs, enterprise APIs behind login walls,
pre-release APIs, and pre-uploaded OpenAPI specs all hit a hard wall.
- New `puzzleeval/manual_atlas.py` — two ingestion modes:
  - `atlas_from_openapi(spec_dict, provider, candidate_name, docs_url)`
    — mechanically converts an OpenAPI 3.x or Swagger 2.x dict into a
    ProviderAtlas. No LLM call. One AtlasEndpoint per (method, path) pair.
  - `atlas_from_markdown(text, provider, candidate_name, docs_url)` —
    accepts user-pasted markdown / plain-text spec, calls Claude with
    the same `ATLAS_EXTRACTION_INSTRUCTIONS` prompt the deep-verify
    loop uses. One API call, no web tools.
- `ingest_user_atlas(...)` writes the result into the same memdir
  cache (`provider_atlases/`) that Phase 6.5 reads, so the next
  deep-verify pass picks it up transparently. Cache key matches Phase
  6.5's normalization (`<provider>::<docs_url>`).

**6. OAuth support in `provider_registry.py`.** Only API-key-in-env-var
was supported. OAuth-only services (Stripe Connect, GitHub Apps,
Google Cloud service accounts in some flows) failed at credential
lookup.
- New `OAuthCredentials` dataclass: `client_id_env`, `client_secret_env`,
  `token_url`, optional `scope`, optional `audience`.
- `ProviderEntry` accepts an `oauth: OAuthCredentials | None` field
  alongside `env_vars`.
- New `auth_mode` property (`"oauth"` / `"api_key"` / `"none"`) and
  `all_env_vars()` method that unions API-key vars with OAuth vars.
- `load_registry()` parses the `oauth` JSON block; malformed blocks
  degrade to None without crashing the whole registry load.
- `get_credentials()` returns the union — both API-key and OAuth env
  vars flow through to Agent 5's harness builder.

**7. Agent 3F text-only fallback when no sample files provided.** Agent
3F used to raise `AgentOutputError("requires test_file_paths but none
were provided")` — a hard wall for users who described file-based
capabilities verbally without sample files on disk.
- `run_file_tests_agent()` now falls back to
  `run_synthetic_tests_agent()` when `test_file_paths` is None or
  empty, logged via `agent_3f_text_only_fallback` operation.
- Downstream test execution surfaces "INCOMPATIBLE: file required" as
  a real failure when the API actually needs a file (Gap 3 fix in
  `implement_test_env.py` already handles that path), so the user gets
  a real signal about provider compatibility instead of a hard error.

### Tests + verification

**44 net new test cases** across two files:
- `tests/test_bandaid_removal.py` (43 cases): adaptive timeout (6),
  config comment de-domaining (1), generic role tokenizer (7), novel
  domain matching (2), builder context injection (6), generic file
  pass-through (6), manual atlas (4), OAuth (7), Agent 3F fallback (1)
  semantic check.
- `tests/test_agent3f.py` (test rewritten): file-less input dispatches
  to text-only synthesis instead of raising.
- `tests/test_agent1.py` (test rewritten): unknown extension returns
  file_reference instead of raising; audio extension produces correct
  media_type.

Combined: **555 PuzzleEval + 30 API + 39 generalizability bench = 624
tests green** (was 580 after §17, +44 net for §18). TypeScript clean.
FastAPI boots cleanly with all new modules.

### Phase fingerprints

| Mechanism | Where to look |
|---|---|
| Adaptive timeout active | `_adaptive_test_timeout()` returns `AGENT5_CODE_TIMEOUT_LONG` for any harness whose atlas declared async_polling/batch_file |
| Generic role matching | `_ROLE_KEYWORD_MAP` no longer present in `openapi_harness.py`; `_role_tokens()` defined |
| Builder atlas context | builder initial message contains string `"STRUCTURED CONTEXT FROM PHASE 6.5"` |
| Generic file pass-through | `parse_file()` on `.mp3`, `.mp4`, `.zip`, etc. returns text containing `"path:"` and `"media_type:"` |
| Manual atlas active | `provider_atlases/` memdir contains entries with `source_run="user_upload"` |
| OAuth registered | `ProviderEntry.auth_mode == "oauth"` for any registered provider |
| Agent 3F fallback fired | log entry with operation `agent_3f_text_only_fallback` |

### Diagnostic flags

| Flag | Default | Disable behavior |
|---|---|---|
| `PUZZLEEVAL_AGENT5_CODE_TIMEOUT_LONG` | `600` (sec) | Override long-timeout value for video/ML/batch APIs |
| `PUZZLEEVAL_AGENT6_TEST_TIMEOUT_LONG` | `600` (sec) | Same for the test runner |

No new feature flags for the other items (they're either always-on
generalizations or schema additions). All bandaid removals are pure
generalizations — no toggle needed because the new behavior strictly
supersedes the old.

### What this audit confirmed

The full audit across 10 categories (brand names, capability carveouts,
magic constants, dead code, file formats, mock paths, defensive
fallbacks, window-dressing prompts, provider-registry coupling,
goal-misalignment) found that the codebase is now **principle-based**
across all 5 agents. The seven items §18 fixed were the last remaining
case-specific carveouts. There are no more `if provider == X then Y`
branches, no more capability-specific prompt sections, no more
hardcoded brand lists. The remaining illustrative examples (OCR shown
in role lists, invoice shown in workflow examples) are exactly that —
illustrations of principles, not branches that depend on those domains.

### Honest residual limitations

- **Multi-step workflow chaining (Phase 9 in CLAUDE.md)**: Agents 7-9
  are still unbuilt. Tests are per-scope independent, not end-to-end
  chained. A 4-step workflow (OCR → entities → enrichment → sync)
  produces 4 isolated harness results, not chain-validated results.
  This is documented as a known design choice (Gap 19).
- **Binary output evaluation**: When a harness returns a file path or
  binary blob (image, audio, PDF download), the LLM judge can't
  inspect the content directly. Vision judge (Gap 6/26) handles
  images; audio/video output judging would require additional
  modality-specific judges. Out of scope for this pass.
- **Webhook/event-subscription receiver**: Atlas captures
  `webhook_callback` and `event_subscription` interaction modes, and
  the builder context warns that local runtime can't receive
  callbacks. Receiving webhooks requires either ngrok-style tunneling
  or a publicly-reachable URL — genuine cloud work, not a local
  bandaid.

---

## 19. Tool Plugin Architecture for Cross-Modality Generalization (2026-04-16)

A user audit against the six product categories on the marketing site
(Document Parsing / Inbound / Outbound / Voice & Phone / Chatbot / Code
Generation) found two structural gaps and a list of capability
extensions needed to support arbitrary tasks beyond HTTP REST APIs:

1. **Single-shot HTTP harness contract** assumed every API takes JSON
   in / returns JSON out, evaluable by an LLM judge reading the
   response text. Voice agents return audio; code-gen needs execution;
   chatbots need multi-turn state.

2. **Hardcoded modality logic** — the `_format_modality_*()` and
   evaluation paths would need carve-outs ("if response is audio then
   transcribe; if code then execute") that violate principle-based
   design.

§19 ships a **tool plugin architecture**: any cross-modality capability
(audio synthesis, transcription, code execution, conversation replay,
vision evaluation) is added as a registered plugin. Agent 5's builder
prompt and the modality detector pick plugins based on the test case's
schema enums — no `if capability == X` branches.

### New package: `puzzleeval/tool_plugins/`

- **`__init__.py`** — `ToolPlugin` ABC, `PluginCapabilities`,
  `SynthesisResult`, `EvaluationResult` data classes, registry
  (`register_plugin`, `get_plugin`, `find_plugins_for_input_type`,
  `find_plugins_for_output_type`, `list_plugins`). Auto-imports the
  bundled plugins so they self-register at package import.
- **`code_execution.py`** — `CodeExecutionPlugin`. Runs Python (always),
  JavaScript (when node available), TypeScript (when npx+tsx
  available), Bash, Go, Rust in sandboxed subprocesses. Builds
  per-language driver scripts that emit `ASSERT_PASS[i]`/`ASSERT_FAIL[i]`
  lines parsed back into a 0-1 score. Fallback when toolchain missing
  is structured (no crash). Synthesizes a canonical FizzBuzz seed when
  no example code is supplied.
- **`vision.py`** — `VisionPlugin`. Wraps the existing `vision_judge.py`
  as a registered plugin so the modality detector picks it
  uniformly. Handles `media_url` outputs (image URLs, data URIs).
- **`transcription.py`** — `TranscriptionPlugin`. Speech-to-text
  evaluator. Picks provider via `PUZZLEEVAL_STT_PROVIDER` env var or
  first key found wins (OpenAI Whisper / Deepgram / AssemblyAI).
  Materializes audio from path / URL / data URI. Scores by
  Jaccard token overlap (0.6 threshold) against expected transcript.
  Degrades gracefully with `fallback_reason="no_stt_provider"` when
  no provider credentialed.
- **`tts.py`** — `TTSPlugin`. Text-to-speech synthesizer for voice
  agent test inputs. Picks provider via `PUZZLEEVAL_TTS_PROVIDER` env
  var or first key found (OpenAI tts-1 / ElevenLabs). Returns
  `SynthesisResult.file_path` pointing at a .wav/.mp3 plus
  `ground_truth.text` for downstream transcription comparison.
- **`conversation_simulator.py`** — `ConversationSimulatorPlugin`.
  Multi-turn replay for chatbot/inbound agents. `ConversationScript`
  carries `user_turns` + `ConversationAssertion` list (turn_index,
  check_type ∈ {contains, not_contains, regex_match, intent_match},
  value, weight). Drives the candidate's harness via a caller-supplied
  `harness_runner` callable; payload format selectable
  (`messages` / `conversation` / `history` / generic). Scores by
  weighted assertion pass-rate (0.6 threshold).

### New module: `puzzleeval/modality.py`

- `ModalityRequirements` dataclass — `input_synthesizers`,
  `output_evaluators`, `unavailable: list[(plugin_name, reason)]`.
- `detect_for_test_case(input_type, output_type)` — queries the
  registry, returns plugins matching by capability. Filters by
  `is_available()` and surfaces unavailable plugins as
  configuration advice.
- `detect_for_test_plan(test_plan)` — walks every ScopeTestSpec.
- `summarize_unavailable(scope_reqs)` — dedup'd list of
  configuration-advice strings for the pipeline summary.

### Wired into Agent 5

- `_format_modality_context_for_builder()` in
  `implement_test_env.py` enumerates the input/output type pairs
  present in the test cases, calls `detect_for_test_case()`, and
  emits a "Modality plugins active for this run" block in the
  builder's initial message. The builder is told:
  - which plugins will evaluate the harness output
  - which plugins are UNAVAILABLE (with the credential needed)
  - the contract: "match your response shape to the plugin's
    expected input — that's the contract."

### Cross-modality coverage of the six product categories

| Category | Input | Output | Plugin used | Status |
|---|---|---|---|---|
| Document Parsing | document_content | structured_json | (LLM judge — already strong) | ✅ existing |
| Inbound Agents | conversation | free_text | conversation_simulator | ✅ shipped |
| Outbound Agents | text | action | LLM judge + (future destination simulator) | ⚠️ partial — see notes |
| Voice & Phone | audio_content | media_url/audio | tts (synth) + transcription (eval) | ✅ shipped |
| Chatbot Agents | conversation | free_text | conversation_simulator | ✅ shipped |
| Code Generation | text/code | code | code_execution | ✅ shipped |

### Tests + verification

- `tests/test_tool_plugins.py` — **43 new test cases**:
  - Registry (6): bundled-plugin auto-registration, custom plugin
    registration, lookup by input/output type.
  - CodeExecutionPlugin (10): capabilities, Python toolchain
    always-available, FizzBuzz synth seed, correct/broken/missing-function
    Python evaluation, no-code-in-response fallback, unknown-language
    fallback, language detection (Python/JavaScript), explicit
    language hint wins over content sniff.
  - VisionPlugin (2): capabilities, no-image fallback.
  - TranscriptionPlugin (4): capabilities, token-overlap scorer,
    unavailable when no provider, evaluation fallback path.
  - TTSPlugin (3): capabilities, unavailable when no provider,
    synthesis returns failure marker.
  - ConversationSimulatorPlugin (8): capabilities, default 3-turn
    seed, full conversation pass, assertion failure, runner crash,
    payload format adapters (messages/history), all four assertion
    check types (contains/not_contains/regex_match/intent_match),
    no-runner fallback.
  - Modality detector (7): audio→audio picks transcription, image
    output picks vision, code picks code_execution, conversation
    picks conversation_simulator, unrelated modality returns no
    evaluators (falls to LLM judge), unavailable plugins surfaced,
    summarize_unavailable dedup.
  - Builder modality context emission (1): full Agent5Input fixture
    drives `_format_modality_context_for_builder()`, asserts the
    block names available plugins and lists the input/output types.

Combined: **598 PuzzleEval + 30 API + 39 generalizability bench = 667
tests green** (was 624 after §18, +43 net for §19). TypeScript clean.
FastAPI boots cleanly. All five plugins importable and routed
correctly by the modality detector.

### Phase fingerprints

| Mechanism | Where to look |
|---|---|
| Plugin registry populated | `puzzleeval.tool_plugins.list_plugins()` returns all 5 by name |
| Modality detection wired | builder system prompt contains `"Modality plugins active for this run"` block |
| Code execution available | `is_available()` returns True (Python always present) |
| Audio plugins available | `OPENAI_API_KEY` / `DEEPGRAM_API_KEY` / `ASSEMBLYAI_API_KEY` present (any one) |
| TTS available | `OPENAI_API_KEY` / `ELEVENLABS_API_KEY` present (any one) |

### Diagnostic flags

| Flag | Default | Effect |
|---|---|---|
| `PUZZLEEVAL_STT_PROVIDER` | unset (auto-pick first available) | Force a specific STT provider |
| `PUZZLEEVAL_TTS_PROVIDER` | unset (auto-pick first available) | Force a specific TTS provider |
| `PUZZLEEVAL_ELEVENLABS_VOICE_ID` | unset (default Rachel) | Override TTS voice |

No flag for the registry itself — plugins are always loaded.

### What's still partial (transparent)

- **Outbound destination simulators** (mock SMTP, Slack, Twilio
  webhook receivers): not shipped. Outbound message providers
  (Mailgun, SendGrid, etc.) can be tested end-to-end via the existing
  HTTP path; the missing piece is "verify the message reached the
  destination," which needs local mock servers. Fits the same plugin
  pattern; one or two days of work to add `OutboundDestinationPlugin`
  with mock-SMTP/mock-Slack handlers.
- **Web automation for chatbot widgets**: Playwright wrapper not
  shipped. Chatbot APIs (Intercom Fin API, etc.) work via the existing
  HTTP + conversation_simulator combo. Widget UI testing (clicking
  the chat bubble, observing the response) requires Playwright and is
  noted as a future plugin.
- **Telephony adapter** (Twilio Voice / Vonage / Bland test fixtures):
  same shape as outbound destination simulator; ships when first user
  needs end-to-end voice-call testing rather than just audio API
  testing.
- **Code-gen language coverage**: Python is always available; other
  languages (JS/TS/Go/Rust) require host-installed toolchains.
  Plugin returns structured "missing_toolchain_X" fallback so the
  caller can show "install rustc to evaluate Rust generation".

### How to add a new plugin

```python
from puzzleeval.tool_plugins import (
    ToolPlugin, PluginCapabilities, SynthesisResult, EvaluationResult,
    register_plugin,
)

class MyPlugin(ToolPlugin):
    name = "my_plugin"
    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            input_types=["my_input_type"],
            output_types=["my_output_type"],
            synthesizes_input=True, evaluates_output=True,
            requires_credentials=["MY_API_KEY"],
        )
    def synthesize_input(self, *, scope_role, **kwargs) -> SynthesisResult:
        ...
    def evaluate_output(self, *, response, expected, **kwargs) -> EvaluationResult:
        ...

register_plugin(MyPlugin())
```

Drop the file in `puzzleeval/tool_plugins/`, add the import to the
auto-loader list in `__init__.py`, and the modality detector picks it
up. Zero changes to Agent 5.

---

## 20. Plugin Wiring + Credential Bridge + Observability (2026-04-16)

§19 shipped the plugin contract (registry, 5 plugins, modality detector)
plus a builder INFORMATIONAL block, but didn't actually invoke plugins
during evaluation or synthesis. This pass closes that gap.

### Honest before-state

| Layer | Before | After §20 |
|---|---|---|
| Plugin registry | Real, 5 plugins | Real, 5 plugins |
| Modality detector | Real, picks by schema enum | Same |
| Builder text block listing plugins | Real (informational) | Same |
| **Plugins invoked during evaluation** | **Not wired — `_evaluate_with_llm` ran for every test** | **WIRED — `detect_for_test_case` runs first, plugin scores or falls back to LLM** |
| **Plugins invoked during synthesis** | **Not wired — TTS/conversation/code seeds unused** | **WIRED — `_needs_plugin_synthesis` + `_synthesize_test_input_via_plugin` in `_stage_test_files`** |
| Credential management | Each plugin checked its own env | **Unified `plugin_status.py` snapshot + advisories surfaced in `pipeline_summary.json`** |
| Tool-use observability | None | **`TestCaseResult.tools_used: list[str]` records which plugins/judges scored each case** |

### New module: `puzzleeval/plugin_status.py`

- `PLUGIN_WIRING` dict — declares which agents each plugin is wired into
  (synthesis / evaluation / builder context). Tested for accuracy: a
  plugin can't be registered without a wiring entry.
- `snapshot_all()` / `snapshot_plugin()` — readiness matrix per plugin:
  capabilities, credential status (per env var, set/unset), wiring
  destinations.
- `format_text_table(snap)` — Windows-safe ASCII rendering for CLI:
  `[READY]` / `[BLOCKED]` markers, per-credential `[set]` / `[unset]`,
  inline action advice ("Get a key at deepgram.com — free tier covers
  ~12k minutes/year").
- `collect_advisories()` — deduped one-line "set X to enable Y" strings
  for `pipeline_summary.json` and the CLI.
- `to_dict()` — serialization for `pipeline_summary.plugins`.

### Evaluator dispatch (the real fix)

`_evaluate_with_llm` previously scored every test case via the LLM judge.
Now, in `_run_tests_for_candidate`:

```
for each eval_item:
    reqs = detect_for_test_case(input_type, output_type)
    plugin = first available output_evaluator
    if plugin:
        verdict = plugin.evaluate_output(...)
        if not verdict.fallback_reason:
            tcr.criteria_scores = [CriterionScore from verdict]
            tcr.tools_used.append(plugin.name)
            remove from eval_items
# whatever's left → LLM judge as before, tools_used.append("llm_judge")
```

When the plugin is `conversation_simulator`, the evaluator passes a
`harness_runner` closure so the plugin can drive the multi-turn
script. Other plugins ignore the kwarg.

### Synthesis dispatch (the second real fix)

`_stage_test_files` now branches:

```
for each tc:
    if tc.test_file_path: stage as before
    elif _needs_plugin_synthesis(tc):  # audio_content / conversation / code
        try plugin.synthesize_input(scope_role, ground_truth_hint)
        if produced file_path: tc.test_file_path = path; tc.expected_output = ground_truth.text
        if produced inline_data: tc.input_data = json.dumps(inline_data)
    else: pass through
```

Audio test cases with text in `input_data` are sent to TTS for real
audio synthesis — the plugin returns the spoken text as ground truth so
the transcription plugin can score the candidate's audio response back
against it. Conversation test cases get a default 3-turn greeting/info/farewell
script when no detailed script is supplied. Code test cases get a
canonical FizzBuzz seed.

### Credential management

Every plugin declares `requires_credentials: list[str]` in its
`PluginCapabilities`. The status reporter checks `os.environ` per
variable, surfaces missing ones with plugin-specific advice (where to
get the key, what tier it covers), and dedups them into
`pipeline_summary.plugin_advisories`. Plugins use ANY-of semantics:
`transcription` is READY when ANY of `OPENAI_API_KEY` / `DEEPGRAM_API_KEY`
/ `ASSEMBLYAI_API_KEY` is set; the advisory lists all three so the user
picks the cheapest one for their volume.

### Tool-use observability

`TestCaseResult.tools_used: list[str]` records the dispatch path for
every case:
- `["code_execution"]` — code-gen test scored by sandboxed execution
- `["transcription"]` — audio-output test scored by STT
- `["vision"]` — image-output test scored by Claude vision
- `["conversation_simulator"]` — multi-turn test driven + scored by the simulator
- `["llm_judge"]` — text/structured test scored by the LLM judge
- `["conversation_simulator", "llm_judge"]` — partial plugin score + LLM fill-in

Surfaces in the per-scope report so the user can see HOW each result
was scored, not just the score.

### Pipeline summary additions

`pipeline_summary.json` now carries:

```json
{
  "plugins": {
    "plugins": [
      {
        "name": "transcription",
        "is_available": true,
        "wired_for_evaluation_in": ["agent_5_evaluator_dispatch"],
        "declared_credentials": [
          {"name": "OPENAI_API_KEY", "is_set": true, "advice": ""},
          {"name": "DEEPGRAM_API_KEY", "is_set": false, "advice": "Get a key at deepgram.com — free tier covers ~12k minutes/year"}
        ],
        ...
      }
    ],
    "summary": {
      "registered_count": 5,
      "available_count": 5,
      "wired_for_evaluation_count": 4,
      "wired_for_synthesis_count": 3
    }
  },
  "plugin_advisories": [
    "plugin 'transcription' inactive — set ANY of [...] to enable audio_content, media_url"
  ]
}
```

### Live readiness matrix (with OPENAI_API_KEY + ANTHROPIC_API_KEY set)

```
PLUGIN STATUS — readiness matrix
======================================================================
[READY] code_execution
  inputs:  code, text, structured_data
  outputs: code, free_text
  wired into: synthesis=['agent_5_test_input_synthesis']; evaluation=['agent_5_evaluator_dispatch']

[READY] vision
  inputs:  image_description
  outputs: media_url
  credentials: [set] ANTHROPIC_API_KEY
  wired into: synthesis=none; evaluation=['agent_5_evaluator_dispatch']

[READY] transcription
  inputs:  audio_content, file_reference
  outputs: audio_content, media_url
  credentials (any one suffices):
    [set] OPENAI_API_KEY
    [unset] DEEPGRAM_API_KEY → free tier covers ~12k minutes/year
    [unset] ASSEMBLYAI_API_KEY → free tier covers ~5 hr/month
  wired into: synthesis=none; evaluation=['agent_5_evaluator_dispatch']

[READY] tts
  inputs:  (none)
  outputs: audio_content, media_url
  credentials: [set] OPENAI_API_KEY; [unset] ELEVENLABS_API_KEY
  wired into: synthesis=['agent_5_test_input_synthesis']; evaluation=none

[READY] conversation_simulator
  inputs:  conversation
  outputs: free_text, structured_json
  wired into: synthesis=['agent_5_test_input_synthesis']; evaluation=['agent_5_evaluator_dispatch']
```

### Tests

`tests/test_plugin_wiring.py` — **23 new test cases**:

- plugin_status (10): snapshot row per plugin, capabilities carried,
  per-credential set/unset detection, wiring metadata attached, summary
  counts, no advisories when credentialed, advisory dedup for any-of
  plugins.
- format_text_table (2): renders all plugins, marks blocked ones.
- PLUGIN_WIRING accuracy (3): every registered plugin has a wiring
  entry; source-grep guards confirm `detect_for_test_case` /
  `evaluate_output` / `tools_used` / `find_plugins_for_input_type` /
  `synthesize_input` are actually present in `implement_test_env.py`.
- TestCaseResult.tools_used (2): default empty; round-trips populated
  list.
- Synthesis dispatch (5): `_needs_plugin_synthesis` for audio/conversation/code
  with various input states; unchanged when no plugin available;
  conversation_simulator always-available path produces inline script.
- Pipeline summary (1): `finalize()` includes plugin status + advisories.

Combined: **621 PuzzleEval + 30 API + 39 generalizability bench = 690
tests green** (was 667 after §19, +23 net for §20). TypeScript clean.
FastAPI boots cleanly. The live readiness matrix renders correctly on
Windows after Unicode → ASCII swap.

### How agents pick tools (deterministic, schema-driven)

The "intelligent tool selection" is fully deterministic — picked by the
modality detector based on the test case's `input_type` and
`output_type` enum values that Agent 1's TestPlan and Agent 3's
TestCase already emit:

| input_type → output_type | First-pick evaluator | Falls back to |
|---|---|---|
| code → code | code_execution | LLM judge |
| audio_content → audio_content/media_url | transcription | LLM judge |
| image_description → media_url | vision | LLM judge |
| conversation → free_text/structured_json | conversation_simulator | LLM judge |
| (anything else) | (none) | LLM judge |

The model never "decides" which plugin to use — that would introduce
non-determinism in scoring. Instead, the dispatch is data-driven and
predictable, and the model is informed (via the builder's modality
context block) that "if your harness returns audio, the transcription
plugin will STT it and compare to expected text — match your response
shape to the contract."

### Plugin → wiring summary (your direct question)

| Plugin | Credentials needed | Wired for synthesis in | Wired for evaluation in | Builder context |
|---|---|---|---|---|
| code_execution | (none — Python always) | Agent 5 staging | Agent 5 evaluator | yes |
| vision | ANTHROPIC_API_KEY | (n/a) | Agent 5 evaluator | yes |
| transcription | any of OPENAI/DEEPGRAM/ASSEMBLYAI | (n/a) | Agent 5 evaluator | yes |
| tts | any of OPENAI/ELEVENLABS | Agent 5 staging | (n/a) | yes |
| conversation_simulator | (none) | Agent 5 staging | Agent 5 evaluator | yes |

### Diagnostic flags

| Variable | Effect |
|---|---|
| `PUZZLEEVAL_STT_PROVIDER` | Force STT provider (`openai_whisper` / `deepgram` / `assemblyai`) |
| `PUZZLEEVAL_TTS_PROVIDER` | Force TTS provider (`openai_tts` / `elevenlabs`) |
| `PUZZLEEVAL_ELEVENLABS_VOICE_ID` | Override default ElevenLabs voice |
| `OPENAI_API_KEY` | Enables OpenAI Whisper STT + tts-1 + GPT eval if you want it |
| `DEEPGRAM_API_KEY` | Alternative STT provider |
| `ASSEMBLYAI_API_KEY` | Alternative STT provider |
| `ELEVENLABS_API_KEY` | Alternative TTS provider |

When NONE of the audio creds are set, transcription/tts plugins return
structured `fallback_reason="no_stt_provider"` / `"no_tts_provider"`
and the LLM judge takes over (text-described audio, less precise but
no crash). The advisories in `pipeline_summary.plugin_advisories` tell
the user which key to add to upgrade.

---

## 21. Inbound / Outbound / Voice plugin pass (closes the "80/20 ocean")

**The gap.** After the hybrid-evaluator pass, three sub-scenarios in the
user's screenshot still had no local testing story:
- **Inbound** — webhook-driven agents (Slack `app_mention`, Intercom widget
  messages, Stripe events, Twilio SMS replies).
- **Outbound delivery** — "did the email actually land in the inbox?"
- **Voice real-time** — WebRTC / SIP / Twilio Voice turns.

Each would have required cloud infrastructure (ngrok tunnels, real phone
numbers, production SMTP) to test end-to-end. This section shipped three
plugins that bring them **inside the local process** so they can be
exercised with zero external dependencies.

### `puzzleeval/tool_plugins/webhook_receiver.py` (NEW)

Pure-stdlib HTTP server bound to `127.0.0.1:PUZZLEEVAL_WEBHOOK_PORT`
(default 8765). `synthesize_input()` returns a unique-token callback URL
+ a provider-shaped envelope (slack / intercom / twilio_sms / stripe /
github / generic). `evaluate_output()` inspects captured POSTs, scoring
0.6 for "received at all" + 0.4 for substring match. `PUZZLEEVAL_TUNNEL_URL`
advertises a public URL when an operator runs ngrok / cloudflared
out-of-band for offsite candidates. Per-request body cap (1 MiB) +
token-scoped buffer isolation protect against cross-test leakage.

### `puzzleeval/tool_plugins/outbound_delivery.py` (NEW)

Three local mock receivers spun lazily:
- **SMTP** at `127.0.0.1:2525` — hand-rolled pure-socket implementation
  (`smtpd` was removed in Python 3.12; `aiosmtpd` adds a third-party
  dep). Supports HELO/EHLO/MAIL/RCPT/DATA/RSET/NOOP/QUIT, RFC 5321
  transparency rule. **DoS caps:** per-line 8 KB, total DATA 25 MiB;
  overflow returns SMTP 552 cleanly.
- **Channel HTTP** at `127.0.0.1:8766` — Slack/Discord/Teams-shaped JSON.
- **SMS HTTP** at `127.0.0.1:8767` — Twilio form-encoded
  `To/From/Body`, returns Twilio-style JSON ack.

Each receiver returns provider-shaped acks so candidate harnesses that
expect normal responses stay happy. Buffers are recipient/channel-keyed
for filtered evaluation.

### `puzzleeval/tool_plugins/voice_realtime.py` (NEW)

Local audio-loopback HTTP server at `127.0.0.1:8768`. `synthesize_input()`
calls the TTS plugin to produce caller audio, exposes it at
`/audio/<token>`, and gives the candidate harness a `/voice/<token>`
callback URL plus a `/voice/<token>/recording` upload URL.
`evaluate_output()` extracts agent text from:
- TwiML `<Say>`/`<Play>` tags
- Vonage NCCO `talk` / `stream` actions
- Generic JSON `response_text` / `text` / `message` / `reply`
- Audio-blob responses → routed to transcription plugin for STT scoring

Real WebRTC/SIP fidelity remains cloud-deferred (needs publicly-reachable
phone numbers + TURN server). Local loopback covers intent + response
shape for agents that speak Twilio Voice / Vonage Voice / generic HTTP.

### `puzzleeval/test_data_sufficiency.py` (NEW)

First-class structured verdict per scope with one of five actions:

| Action | When |
|---|---|
| `READY` | ≥ 6 valid files OR ≥ 3 with advisory |
| `AUGMENT` | 0 files but a synthesis plugin exists (TTS, webhook, outbound) |
| `SYNTHESIZE` | 0 files, no synth plugin available |
| `REQUEST_MORE` | < 3 valid files OR wrong extension for modality |
| `DEGRADE` | Below ideal, single-source variety risk |

Detects wrong-extension uploads ("you sent .mp4 but this is OCR"),
single-source variety risk (all files share a 6-char prefix), and
below-min counts. Wired into both the CLI (printed before Agent 3F fires)
and the FastAPI runner as a `test_data_sufficiency` SSE event with
per-scope verdicts + advisories.

### Schema enum expansion

`puzzleeval/validators.py:VALID_INPUT_TYPES` and `VALID_OUTPUT_TYPES`
extended with new enum members: `webhook_event`, `voice_turn`,
`webhook_callback`, `outbound_message`. Agent 1's system prompt teaches
when to pick each; Agent 3's system prompt teaches how to shape tests for
each (input payload format, expected_output shape, evaluation contract).
Modality dispatcher routes them to the right plugin automatically — no
hardcoded `if scope_role == X` branches.

### PLUGIN_WIRING registry coverage

`puzzleeval/plugin_status.py:PLUGIN_WIRING` now contains entries for all
8 plugins (added `webhook_receiver`, `outbound_delivery`, `voice_realtime`
alongside the 5 originals). Each entry declares where the plugin is
actually invoked: synthesis call sites, evaluation call sites, builder
context mentions. Tested by `test_plugin_wiring.py::TestWiringAccuracy`.

### Tests shipped

- `tests/test_webhook_receiver.py` — 16 cases (full POST → capture →
  evaluate loops for all 6 provider shapes)
- `tests/test_outbound_delivery.py` — 12 cases (real `smtplib.sendmail`
  round-trip, form-encoded SMS, JSON Slack, recipient-filtered eval)
- `tests/test_voice_realtime.py` — 17 cases (TwiML / NCCO / JSON /
  audio-blob response paths)
- `tests/test_test_data_sufficiency.py` — 16 cases (all 5 action
  verdicts, new augmentation mappings)

---

## 22. Production audit + resilience pass (CURRENT SESSION)

Six parallel deep audits (robustness, bandaids, Claude-Code-parity,
plugin edges, test-data-quality, frontend errors) surfaced 70+ findings.
Every production-blocking finding is fixed. This section consolidates.

### 22a. Central Anthropic client factory — `puzzleeval/anthropic_client.py` (NEW)

Every agent used to do `anthropic.Anthropic(api_key=...)` with bare
defaults: 10-minute SDK timeout, no retries. A flaky TCP socket hung
the whole pipeline for 10 minutes; a single transient 429 / 5xx killed
runs with no recovery. Now every agent routes through:

```python
from puzzleeval.anthropic_client import build_client
client = build_client(api_key=ANTHROPIC_API_KEY)            # 120 s timeout, max_retries=3
# Agent 5 needs longer for deep thinking:
client = build_client(api_key=ANTHROPIC_API_KEY, timeout=240)
```

Tuning via `PUZZLEEVAL_ANTHROPIC_TIMEOUT_S` / `PUZZLEEVAL_ANTHROPIC_MAX_RETRIES`.

`call_with_model_fallback(fn, primary_model, ...)` wraps a call in the
Opus→Sonnet→Haiku ladder on persistent 429:
```
claude-opus-4-7  → claude-sonnet-4-6
claude-opus-4-5  → claude-sonnet-4-6
claude-sonnet-4-6 → claude-haiku-4-5-20251001
```
Currently wired into Agent 1's `parse_with_fallback` path. **Known gap:**
Agent 5 still hard-fails on persistent Opus 4.7 rate-limits (only
SDK-level retries).

### 22b. Structured-output grammar fallback — `puzzleeval/structured_output.py` (NEW)

Anthropic's `client.messages.parse(output_format=PydanticModel)` compiles
the schema into a token-level grammar — fast and guaranteed valid, but
size-capped. Several schemas (`Agent1Result`, `Agent2Result`,
`Agent3Result`, Agent 5's `EvaluationBatchResult`) sit near the cap. A
minor field addition trips 400s:
- `"The compiled grammar is too large, which would cause performance issues."`
- `"Grammar compilation timed out."`

`parse_with_fallback(...)` is a drop-in replacement for
`client.messages.parse`. On those specific 400s, it falls through to
`client.messages.create()` with a non-strict tool whose `input_schema`
is the same JSON Schema. Post-processing:
- **Over-nesting unwrap** — some models emit `{"input": {...}}` wrapping
  the result. Detect single-key dict whose value contains the required
  schema keys and unwrap.
- **Python-repr string coercion** — LLMs occasionally emit
  `"frozenset({'step_1'})"` for array-typed fields. Pydantic would
  iterate that string char-by-char. We coerce back to real arrays
  before validation.
- **Strict-mode fields stripped** — fallback drops `thinking` and
  `output_config` (Anthropic forbids combining those with `tool_choice`
  forcing a specific tool).

Wrapped around ALL 6 structured-output call sites: Agent 1, Agent 2's
structuring step, Agent 3, Agent 3F, Agent 4's structuring step, and
Agent 5's LLM evaluator batch call.

### 22c. `frozenset[str]` → `list[str]` schema migration

`Candidate.covers_step_ids` and `ScreenedCandidate.covers_step_ids` used
to be typed `frozenset[str]`. JSON Schema has no native frozenset type,
so the strict-grammar path always failed (forcing the non-strict
fallback for every run). Worse, `Pydantic.model_dump()` preserved the
frozenset as a Python frozenset, and `json.dumps(default=str)`
stringified it as `"frozenset({'step_1'})"` on disk — callers then
iterated the string char-by-char.

Root fix: migrated the field to `list[str]` with caller-side de-dup +
sort enforcement (`sorted({...})`). All upstream call sites in
`research.py`, `deep_verify_runner.py`, `selection.py`, `pipeline.py`
updated. Eliminates the bug class at the source; the coercer in
`structured_output.py` is now defense-in-depth rather than load-bearing.

### 22d. Cost circuit-breaker — `puzzleeval/budget.py` (NEW)

Threadsafe `RunBudget` class with a hard USD cap. Default $25 via
`PUZZLEEVAL_MAX_RUN_COST_USD`. Raises `BudgetExceededError` when the
cap is crossed.

Wired through **`RunState.record_cost(amount_usd, reason)`** in
`services/run_manager.py` — every cost-accumulation site calls this
helper, which drives BOTH the plain total AND the budget's internal
accumulator. Single chokepoint; one missed call-site would defeat
the breaker, and that's documented in the field docstring.

**Surfacing paths:**
- `routes/chat.py` catches during Agent 1 turns → HTTP 402 with
  `{reason, spent_usd, cap_usd, message}`.
- `services/pipeline_runner.py` catches at the outer handler → emits
  `pipeline_failed` with `{reason: "budget_exceeded", spent_usd, cap_usd,
  last_charge_reason, recovery}`.

Wired cost-accumulation sites:
- Agent 1 chat turns — `real_agent1_turn()` after result parse
- Agents 2/3/4 completion — via new `_record_agent_cost_and_emit()` helper
- Agent 5 completion — aggregates `total_build_cost_usd + total_test_cost_usd`

### 22e. Live `cost_update` SSE stream

The frontend had a `cost_update` handler subscribed but the backend never
emitted. Live cost meter was frozen during multi-minute agents.

New `_record_agent_cost_and_emit(agent_name, cost_usd)` helper in
`services/pipeline_runner.py` fires after every agent completion with:
```json
{
  "trace_id": "...",
  "source": "agent_2",
  "delta_usd": 0.05,
  "total_cost_usd": 0.12,
  "budget": {"spent_usd": 0.12, "cap_usd": 25.0, "remaining_usd": 24.88, "utilization": 0.005}
}
```

Frontend `usePipelineRun` case `"cost_update"` updates `costAccumulator`
incrementally — meter updates between agent boundaries instead of
jumping at the end.

### 22f. FastAPI `lifespan` handler — `puzzleeval-api/main.py`

The 3 new local plugins (webhook_receiver, outbound_delivery,
voice_realtime) spin up HTTP/SMTP servers on 127.0.0.1 lazily. Without
explicit shutdown, threads + sockets leaked across uvicorn restarts;
the second start fell to ephemeral ports and silently broke harnesses
with hardcoded port references.

New `@asynccontextmanager lifespan(app)` context manager:
- Startup: touches `list_plugins()` so every plugin imports and
  registers. Doesn't bind ports (plugins stay lazy).
- Shutdown: iterates plugins, calls `shutdown()` on any that expose it,
  logs success / failure per plugin. Idempotent.

All `_ThreadedHTTPServer` subclasses in the new plugins also set
`allow_reuse_address = True` so rebind works across `uvicorn --reload`.

### 22g. DoS protection

Three real OOM/exhaustion vectors closed:
1. **`routes/files.py:_read_with_cap()`** — stream-reads with
   `MAX_UPLOAD_BYTES_PER_FILE` cap (100 MiB default, env-configurable),
   aborts with HTTP 413 instead of buffering an unbounded blob.
   Previous code did `await upload_file.read()` with no check.
2. **`outbound_delivery.py` SMTP handler** — `readline(SMTP_MAX_LINE_BYTES)`
   (8 KB per RFC 5321) AND total DATA cap `SMTP_MAX_DATA_BYTES` (25 MiB
   matching Gmail's ceiling). Previously a malicious sender could stream
   a single line forever.
3. **`outbound_delivery.py` HTTP receivers** — `HTTP_MAX_BODY_BYTES`
   (1 MiB default) instead of hardcoded literal.

### 22h. `coverage_gap` SSE event

Backend emits when Agent 2 returns zero candidates OR when workflow
scopes have no covering candidates:
```json
{
  "trace_id": "...",
  "candidate_count": 0 or N,
  "blueprint_step_ids": ["step_1", "step_2"],
  "missing_scopes": ["step_2"],
  "covered_scopes": ["step_1"],
  "user_message": "Found N candidate(s) but M scope(s) have no coverage: step_2. ..."
}
```

Frontend renders as a chat warning immediately after research finishes.
Previously the pipeline silently completed with no candidates →
user read it as "the system broke."

### 22i. Final `EvaluationReport` assembler — `puzzleeval/report.py` (NEW)

Before: the "final report" was raw `agent_5_output.json` + a hardcoded
recommendation sentence in the React UI.

After: `assemble_report()` produces structured `EvaluationReport` from
Agent 1/2/4/5 outputs + total cost. Contains:
- Per-candidate ranking by `overall_score` desc
- Per-scope winners (best covering candidate per scope)
- Pass/fail counts + pass rate
- Evidence rows (top-3 failures + top-3 successes per candidate)
- Monthly cost projection (uses `puzzleeval.pricing.estimate_monthly_cost`
  + user's stated `Constraints.monthly_volume`)
- Deterministic pros/cons heuristics (Top-tier / Below-half / auth-method
  flags / sandbox disclosure)
- Coverage-gap advisories

Tolerates partial inputs — emits informative advisories for missing
pieces rather than crashing. Persisted to
`runs/<trace>/evaluation_report.json`, emitted as `evaluation_report`
SSE event, served via new `GET /runs/{id}/report` endpoint (reads disk
first, falls back to on-demand assembly). 16 unit tests in
`tests/test_report.py`.

### 22j. Frontend resilience

- **`subscribeToEvents` wrapped with auto-reconnect** — exponential
  backoff 1s → 2s → ... → 30 s cap; preserves `lastEventId` across
  reconnects; new `onStatusChange` callback surfaces
  `connecting/open/reconnecting/closed`.
- **Reconnecting banner** in `Playground.tsx` when `sseStatus ===
  "reconnecting"`.
- **Stuck-pipeline banner** when `stage === "pipeline"` AND
  `Date.now() - lastEventAt > 2 min` — "no progress for Xm" nudges the
  user to cancel/retry.
- **`EvaluationReportCard` component** — renders winner, per-scope
  winners, coverage %, advisories, ranked candidates with pros/cons +
  failure evidence. Dropped into `Playground.tsx` above `ResultsComparison`.
- **`cost_update` handler** — wires live meter to the running total +
  budget snapshot in every `cost_update` event.
- **`coverage_gap` handler** — renders the gap as an immediate chat
  warning.
- **`VITE_API_BASE` override** — `import.meta.env.VITE_API_BASE` first,
  `/pzapi` dev-proxy fallback. Deployers can point the SPA at any
  backend URL without rebuilding.

### 22k. `.env` autoload empty-shadow fix

CI environments (and `set "ANTHROPIC_API_KEY=..."` patterns in Windows
shells) commonly declare placeholder env vars as empty strings before
injecting the real value. `load_dotenv(override=False)` treats an empty
string as "already set" and refuses to overwrite — silently shadowing
the correct value in the `.env` file.

Fix in `puzzleeval/__init__.py:_autoload_dotenv`: before calling
`load_dotenv`, walk the keys defined in the `.env` file and evict any
whose current `os.environ` value is empty or whitespace-only. Eviction
is scoped to keys defined in the `.env` file — unrelated empty env vars
are untouched. Real non-empty shell values still beat the `.env` file
(override=False semantics preserved). 5 regression tests in
`tests/test_autoload_dotenv.py`.

### 22l. Plugin registry name-conflict guard

`register_plugin()` previously did `_REGISTRY[name] = plugin` with no
duplicate check. Two plugins with the same name silently fought for the
modality slot. Now:
- Re-registering the SAME instance is a no-op (idempotent — common when
  a module is imported via different paths in tests).
- Re-registering a DIFFERENT instance under an existing name logs a
  WARNING with both module-qualified class names.
- Set `PUZZLEEVAL_STRICT_PLUGIN_REGISTRY=1` to raise instead of warn.

### 22m. Agent 5 cost-tracking fix for fallback path

`_save_json` used to stringify frozensets via `json.dumps(default=str)`
→ produced `"frozenset({'step_1'})"` strings on disk. Replaced with
`_jsonify()` walker that converts frozenset/set/tuple → JSON-friendly
equivalents. Defensive `_coerce_coverage()` in `pipeline_runner.py` also
handles any legacy upstream path that stringified coverage.

### Tests + verification

Final test suite:
- 816 PuzzleEval core tests
- 30 FastAPI tests
- 39 generalizability bench tests
- **885 total, all passing**

New tests this pass: `test_anthropic_client_and_budget.py` (20 cases),
`test_report.py` (16 cases), `test_autoload_dotenv.py` (5 cases),
`test_structured_output.py` (22 cases).

TypeScript `npx tsc --noEmit` clean. Vite `npm run build` clean. Backend
boot via `TestClient` verified clean with new lifespan handler. Plugin
readiness matrix: 8/8 READY when all keys present.

### Known gaps still open

See the phase status block at the top of `AGENT_REFINEMENT_ROADMAP.md`
for the canonical Claude-Code-parity gap list. Shortest version:

1. Agent 5 mid-turn cancellation (wiring)
2. Agent 5 Opus→Sonnet fallback (helper exists, needs wiring into `_build_single_harness`)
3. Live `agent_thinking` SSE streaming
4. Incremental token streaming (stream=True in `messages.create()`)
5. Agent 2 per-scope parallelism
6. In-run web_fetch URL cache
7. Idempotency keys + DRY_RUN propagation
8. Structured provider-quirk registry
9. AWS SigV4 / mTLS / OAuth authorization_code patterns

None are architectural — all are focused wiring work.
