# PuzzleEval Gap Analysis — Full 30-Gap Report

> Generated 2026-04-16 from exhaustive pipeline trace across 6 diverse task types.
> Each gap was evaluated against "is this a bandaid or a general solution?" criterion.
>
> **Status (rolled up through the resilience pass):** 14 gaps shipped
> (6, 9, 10, 11, 12, 13, 14, 16, 18, 20, 21, 25, 26, 29, 30 + the three
> inbound/outbound/voice scenarios). 4 gaps are CLOUD-DEFERRED (4, 5,
> 17, 24 — require a publicly-reachable callback URL). The remaining
> Claude-Code-parity gaps (mid-turn cancellation, Agent 5 model
> fallback, incremental token streaming, Agent 2 per-scope parallelism,
> in-run web_fetch cache, idempotency keys + DRY_RUN propagation,
> provider-quirk registry, AWS SigV4 / OAuth authorization_code /
> mTLS auth) are tracked in `POST_ROADMAP_ENHANCEMENTS.md` §22. See that
> doc for the authoritative per-gap change log and file-level deltas.

## Methodology

Six real-world tasks were traced through every agent (1 through 5) and every pipeline phase (1 through 10):

1. **OCR + Accounting** — file-based + API integration, multi-scope
2. **Chatbot** — pure text, single-scope
3. **Image Generation** — generative output, single-scope
4. **Multi-language Translation** — text-in text-out, fan-out DAG
5. **Audio Transcription + Summarization** — audio file input, multi-scope
6. **Real-time Data Pipeline** — streaming/webhook, multi-scope

---

## FIXED GAPS (9 gaps — all shipped, tested, general solutions)

### Gap 10 — TestCase has no scope_id; routing relies on fuzzy match
- **Severity:** HIGH
- **Tasks affected:** All multi-scope
- **Agent:** Agent 3 + scope_routing.py
- **What was wrong:** Test cases only had `sub_task_ref` (a description string). Routing to scopes used word-overlap matching — fragile, breaks when descriptions are rephrased.
- **Fix:** Added `scope_id: str | None` to `TestCase` schema. When a TestPlan exists, Agent 3 populates `scope_id` from `ScopeTestSpec.scope_id`. `group_tests_by_scope()` and `build_scope_runs()` now use `scope_id` as a deterministic PRIMARY key; fuzzy matching is the FALLBACK for legacy test cases.
- **Why this is general, not a bandaid:** The `scope_id` field is a first-class schema addition that works for ANY domain. It doesn't hardcode any domain-specific logic — it's a join key between the TestPlan and the test cases.
- **Files:** `schemas.py`, `scope_routing.py`

### Gap 3 — File-required tests silently skipped when no files provided
- **Severity:** CRITICAL
- **Tasks affected:** OCR + Accounting, Audio Transcription
- **Agent:** Agent 5 (Build + Test)
- **What was wrong:** When `file_required=True` and `test_file_path=None`, Agent 5's test execution skipped the test entirely with `skip_reason="INCOMPATIBLE"`. Result: OCR and audio scopes got ZERO test results when users didn't provide files.
- **Fix:** File-required tests now RUN with text `input_data` as fallback. Many APIs accept both file uploads AND text input. If the API returns INCOMPATIBLE, the result is recorded as a normal failure (counted, visible) instead of being silently skipped. Successful fallback results are annotated with "Tested with synthetic text input."
- **Why this is general, not a bandaid:** The fix doesn't hardcode "OCR" or "audio" — it changes the behavior for ALL `file_required=True` tests. The harness gets a chance to try; the API decides if it can handle text-only input. This works for any file-based domain.
- **Files:** `implement_test_env.py`, `schemas.py` (description updates)

### Gap 1 — No output_type for media URLs or binary returns
- **Severity:** CRITICAL
- **Tasks affected:** Image Generation, Audio Generation, Real-time Data
- **Agent:** Agent 1 (schemas)
- **What was wrong:** `output_type` only allowed 5 values. Image generation APIs return URLs/base64, not text. Forcing `"action"` was semantically wrong and confused downstream evaluation.
- **Fix:** Added `"media_url"` to `VALID_OUTPUT_TYPES`. APIs that return image URLs, audio URLs, or file download links now have an honest type.
- **Why this is general, not a bandaid:** `"media_url"` covers ANY API that returns a reference to generated content — images, audio, video, PDFs, archives. Not domain-specific.
- **Files:** `validators.py`

### Gap 2 — No input_type for audio content
- **Severity:** HIGH
- **Tasks affected:** Audio Transcription + Summarization
- **Agent:** Agent 1 (schemas)
- **What was wrong:** `input_type` had no value for audio. Transcription tasks had to use `"document_content"` (wrong) or `"text"` (more wrong).
- **Fix:** Added `"audio_content"` and `"file_reference"` to `VALID_INPUT_TYPES`. Audio transcription uses `"audio_content"`; generic file uploads use `"file_reference"`.
- **Why this is general, not a bandaid:** `"audio_content"` and `"file_reference"` are semantic categories, not provider-specific. They work for Deepgram, AssemblyAI, Whisper, any future audio API, and any file-upload API.
- **Files:** `validators.py`

### Gap 15 — No validator: workflow present but test_plan null
- **Severity:** MEDIUM
- **Tasks affected:** All
- **Agent:** Agent 1 (validator)
- **What was wrong:** Agent 1 could produce a workflow blueprint but forget the test_plan. Agent 3 would silently fall back to independent generation, producing test cases misaligned with the architecture.
- **Fix:** Validator now warns when `workflow is not None and test_plan is None`.
- **Why this is general:** Structural consistency check — catches any case where the LLM output is incomplete.
- **Files:** `validators.py`

### Gap 27 — total_test_target sum not validated
- **Severity:** LOW
- **Tasks affected:** All
- **Agent:** Agent 1 (validator)
- **What was wrong:** `TestPlan.total_test_target` could disagree with the sum of per-scope targets. Agent 3 would see conflicting signals.
- **Fix:** Validator warns when the sum doesn't match.
- **Why this is general:** Arithmetic consistency check.
- **Files:** `validators.py`

### Gap 28 — Scope spec count vs step count not validated
- **Severity:** MEDIUM
- **Tasks affected:** All
- **Agent:** Agent 1 (validator)
- **What was wrong:** Agent 1 could produce a 3-step workflow but only 2 scope_specs, silently leaving one scope untested.
- **Fix:** Validator errors when scope_specs don't cover all workflow steps, warns when there are extra specs.
- **Why this is general:** Set-alignment check between two data structures.
- **Files:** `validators.py`

### Gap 7 — Identical capabilities on parallel steps break routing
- **Severity:** HIGH
- **Tasks affected:** Multi-language Translation
- **Agent:** Agent 1 (validator)
- **What was wrong:** Three translation steps with `capability="text translation"` caused Agent 3's fuzzy routing to assign ALL test cases to the first step. Spanish, French, and German tests blended together.
- **Fix:** Validator warns when multiple steps share the same capability string. Agent 1's prompt already teaches unique capabilities per step ("English to Spanish translation" vs "text translation").
- **Why this is general:** Detects ANY case of duplicate capabilities, not just translation.
- **Files:** `validators.py`

### Gap 8 — Scope routing fails with identical capabilities (companion to Gap 7)
- **Severity:** HIGH
- **Tasks affected:** Multi-language Translation
- **Agent:** Agent 3 + scope_routing.py
- **What was wrong:** Fuzzy keyword matching in `group_tests_by_scope()` couldn't distinguish steps with identical capabilities.
- **Fix:** Fixed by Gap 10's `scope_id` field — deterministic routing bypasses fuzzy matching entirely when scope_id is populated.
- **Why this is general:** The scope_id fix works for ALL routing ambiguity, not just translation.
- **Files:** Fixed by Gap 10's changes

---

## UNFIXED GAPS — Documented for Future Work (21 gaps)

### Priority 1: CRITICAL (pipeline-breaking for specific domains)

#### Gap 4 — No execution_mode for streaming/webhook/polling
- **Severity:** CRITICAL
- **Tasks affected:** Real-time Data Pipeline
- **Agent:** Agent 1 (schema)
- **Current behavior:** `WorkflowStep` assumes request-response. No field for `"webhook"`, `"streaming"`, or `"polling"` execution patterns. "Monitor social media mentions" decomposes into steps but the `monitor` step has no way to express that it's a continuous process.
- **Required fix:** Add `execution_mode: str` to `WorkflowStep` with values `"request_response"` (default), `"polling"`, `"webhook"`, `"streaming"`. This signals to Agent 5 how to build the harness.
- **Effort:** L (architectural change affecting Agent 5's harness contract)
- **When to fix:** When the first user requests a streaming/real-time workflow

#### Gap 5 — Harness is request-response only; no streaming support
- **Severity:** CRITICAL
- **Tasks affected:** Real-time Data Pipeline
- **Agent:** Agent 5 (Build + Test)
- **Current behavior:** `harness.run(input_data) -> dict` is synchronous, single-call. Can't set up webhook listeners, subscribe to streams, or collect events over time.
- **Required fix:** Add `async_run()` or `subscribe_and_collect()` harness method for streaming/webhook APIs.
- **Effort:** L (new harness pattern + test execution flow)
- **When to fix:** When Gap 4 is addressed

### Priority 2: HIGH (degraded quality for specific domains)

#### Gap 6 — LLM judge cannot evaluate generated image quality
- **Severity:** HIGH
- **Tasks affected:** Image Generation
- **Agent:** Agent 3 / Agent 5 evaluation
- **Current behavior:** For image generation, `expected_output` is a text description. LLM judge compares API response (JSON with URL) against this description. Can check structural compliance (URL returned) but NOT whether the generated image actually matches the prompt.
- **Required fix:** For `output_type="media_url"`: (1) harness fetches the URL and verifies valid image data, (2) optionally send image to Claude's vision model for prompt-match evaluation.
- **Effort:** L (vision model integration in evaluation)
- **When to fix:** When image generation becomes a priority use case

#### Gap 9 — No sandbox/dry-run for destructive action APIs
- **Severity:** HIGH
- **Tasks affected:** OCR + Accounting (QuickBooks sync)
- **Agent:** Agent 5 (Build + Test)
- **Current behavior:** "Create bill in QuickBooks" action type has no sandbox concept. Testing creates REAL records.
- **Required fix:** Add `side_effects` field to `WorkflowStep` or `ScopeTestSpec`: `"read_only"`, `"creates_records"`, `"modifies_records"`, `"deletes_records"`. When destructive, harness prefers sandbox/test environments, warns user, uses dry-run mode.
- **Effort:** M
- **When to fix:** Before production launch with action-type APIs

#### Gap 17 — No api_interaction_pattern in Candidate schema
- **Severity:** HIGH
- **Tasks affected:** Real-time Data Pipeline
- **Agent:** Agent 2 (Research)
- **Current behavior:** Agent 2's Candidate has no field to distinguish "REST sync API" from "webhook API" from "streaming API". A candidate with `api_available=True` might only have polling-only access, not real-time.
- **Required fix:** Add `api_interaction_pattern` to Candidate: `"rest_sync"`, `"rest_async_polling"`, `"webhook"`, `"streaming"`, `"mixed"`.
- **Effort:** M
- **When to fix:** With Gap 4 (streaming support)

#### Gap 24 — No streaming/webhook verification in deep-verify 4C
- **Severity:** HIGH
- **Tasks affected:** Real-time Data Pipeline
- **Agent:** Agent 4 (Deep-Verify)
- **Current behavior:** Phase 4C verifies "does this API have endpoints for this scope?" but doesn't check whether those endpoints support real-time/streaming vs batch-only.
- **Required fix:** Add streaming/webhook verification to 4C prompt.
- **Effort:** S
- **When to fix:** With Gap 4 (streaming support)

#### Gap 26 — Image quality unmeasured (no vision evaluation)
- **Severity:** HIGH
- **Tasks affected:** Image Generation
- **Agent:** Agent 5 evaluation
- **Current behavior:** Same as Gap 6. For DALL-E vs Stable Diffusion comparison, the pipeline ranks by response structure + latency + price. Actual image quality is unmeasured.
- **Required fix:** Same as Gap 6 — vision model integration.
- **Effort:** L
- **When to fix:** Same as Gap 6

### Priority 3: MEDIUM (quality degradation, workarounds exist)

#### Gap 11 — No signal for async polling pattern in candidate schema
- **Severity:** MEDIUM
- **Tasks affected:** Audio Transcription, OCR
- **Agent:** Agent 2 (Research)
- **Current behavior:** `data_format_notes` can mention async patterns in free text, but there's no structured field for "this API uses async polling."
- **Required fix:** Add guidance in Agent 2's structuring prompt to flag async patterns.
- **Effort:** S
- **When to fix:** Next prompt-tuning pass

#### Gap 12 — ROUTING_TABLE has no audio file upload row
- **Severity:** MEDIUM
- **Tasks affected:** Audio Transcription, OCR
- **Agent:** Agent 4 (Deep-Verify)
- **Current behavior:** ROUTING_TABLE template maps `document_content -> endpoint`. No row for audio uploads.
- **Required fix:** Add `audio_content -> endpoint` row to the ROUTING_TABLE template in the deep-verify prompt.
- **Effort:** S
- **When to fix:** Next prompt-tuning pass

#### Gap 13 — No per-candidate rate limiting in test execution
- **Severity:** HIGH
- **Tasks affected:** All
- **Agent:** Agent 5 (Build + Test)
- **Current behavior:** Test cases for one candidate run sequentially but there's no rate-limiting delay between calls. Free-tier APIs with 1-2 req/sec limits get hammered.
- **Required fix:** Read `rate_limit_info` from `ScreenedCandidate` and space test calls. Add configurable delay between calls per candidate.
- **Effort:** S
- **When to fix:** Before first live run with free-tier APIs

#### Gap 14 — Subjective eval biased toward single expected_output
- **Severity:** MEDIUM
- **Tasks affected:** Chatbot
- **Agent:** Agent 3 (Test Generation)
- **Current behavior:** Chatbot quality is subjective — two equally good responses may look nothing alike. The LLM judge compares against ONE `expected_output`, biasing toward that specific wording.
- **Required fix:** Add `reference_mode` to `ScopeTestSpec`: `"ground_truth"` (extraction) vs `"exemplar"` (chatbot). LLM judge treats `expected_output` as a reference example for `"exemplar"` mode, not ground truth.
- **Effort:** M
- **When to fix:** When chatbot evaluation quality is a user complaint

#### Gap 16 — Deep-verify has no generative API guidance
- **Severity:** MEDIUM
- **Tasks affected:** Image Generation
- **Agent:** Agent 4 (Deep-Verify)
- **Current behavior:** Spec extraction focuses on document/text APIs. For image generation APIs, generation-specific parameters (model variants, output format, size/quality settings) aren't captured.
- **Required fix:** Add "Generative API" section to deep-verify prompt.
- **Effort:** S
- **When to fix:** Next prompt-tuning pass

#### Gap 18 — No standard polling helper in harness template
- **Severity:** MEDIUM
- **Tasks affected:** Audio Transcription, OCR
- **Agent:** Agent 5 (Build + Test)
- **Current behavior:** Each builder agent invents its own polling loop for async APIs. Some poll too aggressively, some not at all.
- **Required fix:** Provide `poll_until_complete(job_url, headers, max_wait, interval)` in the harness template.
- **Effort:** S
- **When to fix:** Next Agent 5 prompt update

#### Gap 19 — No end-to-end chaining (by design)
- **Severity:** MEDIUM
- **Tasks affected:** Multi-language Translation
- **Agent:** Agent 5 / Results
- **Current behavior:** Scopes are tested independently. The CMS formatting scope never receives real translation output — it gets simulated upstream JSON.
- **Required fix:** Document this clearly in the final report: "Each scope was tested independently. End-to-end testing would require running the chosen tools together." This is a KNOWN DESIGN CHOICE, not a bug.
- **Effort:** S
- **When to fix:** When results report generation is built (Phase 9+ polish)

#### Gap 21 — Pass rates not comparable across eval modes
- **Severity:** MEDIUM
- **Tasks affected:** All
- **Agent:** Results (Phase 9)
- **Current behavior:** A chatbot with 70% pass_rate might be excellent (subjective eval is harsh), while an OCR tool with 70% might be mediocre. Raw numbers without calibration.
- **Required fix:** Add `evaluation_mode` to `ScopeTestRun`: `"objective"` or `"subjective"`. Display prominently. Consider per-mode normalization.
- **Effort:** S
- **When to fix:** Results UI polish pass

#### Gap 22 — Fuzzy scope routing fragile with 2-word overlap
- **Severity:** MEDIUM
- **Tasks affected:** All multi-scope
- **Agent:** scope_routing.py
- **Current behavior:** 2-word overlap threshold can fail when sub_task_ref uses different vocabulary than step capability.
- **Required fix:** ALREADY MITIGATED by Gap 10's `scope_id` fix — fuzzy matching is now FALLBACK only. To further harden: lower threshold to 1-word overlap in fallback path.
- **Effort:** S (already mostly fixed)
- **When to fix:** If fuzzy fallback causes issues in practice

#### Gap 23 — Mixed Agent 3 + 3F merge needs verification
- **Severity:** MEDIUM
- **Tasks affected:** OCR + Accounting
- **Agent:** Agent 3F
- **Current behavior:** Both agents produce tests with potentially overlapping `sub_task_refs`. Merge logic combines test_cases + coverage_summary. Not verified with a dedicated test.
- **Required fix:** Add a test case for the merge scenario in `test_agent3.py`.
- **Effort:** S
- **When to fix:** Next test coverage pass

#### Gap 25 — No cross-candidate rate limiting for shared providers
- **Severity:** MEDIUM
- **Tasks affected:** All (especially chatbot where multiple candidates use OpenAI)
- **Agent:** Agent 5 (Build + Test)
- **Current behavior:** 5 chatbot candidates all using OpenAI's API run in parallel, hitting OpenAI's rate limit. Cascading failures.
- **Required fix:** Group candidates by upstream API provider. Serialize or stagger execution for candidates sharing a provider.
- **Effort:** M
- **When to fix:** When chatbot or LLM-wrapper evaluations become common

#### Gap 30 — Multi-scope harness needs scope-specific input_context
- **Severity:** MEDIUM
- **Tasks affected:** Multi-language Translation
- **Agent:** Agent 5 (Build + Test)
- **Current behavior:** One harness covers 3 translation scopes but test cases don't carry scope-specific parameters (target language) in `input_context`.
- **Required fix:** Ensure `input_context` carries scope-specific parameters. Agent 3's prompt should populate `input_context` from TestPlan specs.
- **Effort:** S
- **When to fix:** Next Agent 3 prompt update

### Priority 4: LOW (cosmetic or edge-case)

#### Gap 20 — Unspecified integration makes action scope untestable
- **Severity:** LOW
- **Tasks affected:** Chatbot
- **Agent:** Agent 1
- **Current behavior:** "Process refund" action step requires knowing WHICH order system the user uses. Without it, candidates for "process refund via API" have no testable target.
- **Required fix:** Agent 1 marks such steps as `all_in_one_compatible=false` and notes the dependency.
- **Effort:** S
- **When to fix:** Prompt tuning pass

#### Gap 29 — Chatbot search keywords don't distinguish LLM vs platform
- **Severity:** LOW
- **Tasks affected:** Chatbot
- **Agent:** Agent 2 (Research)
- **Current behavior:** Search finds both LLM APIs (OpenAI) and chatbot platforms (Intercom). Both are valid but serve different users.
- **Required fix:** Agent 1 generates distinguishing keywords.
- **Effort:** S
- **When to fix:** Prompt tuning pass

---

## Verification: Are the Fixes General Solutions or Bandaids?

| Fix | General? | Evidence |
|-----|----------|----------|
| scope_id on TestCase | **YES** | Domain-agnostic join key. Works for OCR, chatbot, translation, audio, image — any domain. No hardcoded domain logic. |
| file_required fallback | **YES** | Changes behavior for ALL file_required tests. The harness tries; the API decides. No OCR-specific or audio-specific code. |
| media_url output type | **YES** | Covers ANY API returning content references — images, audio, video, PDFs. Not "image_url" (which would be domain-specific). |
| audio_content + file_reference input types | **YES** | Semantic categories, not provider names. Work for any audio API or file-upload API. |
| TestPlan validators | **YES** | Structural consistency checks between data structures. No domain knowledge involved. |
| Duplicate capability warning | **YES** | Detects ANY repeated capability string. Triggered by translation fan-out, but also catches accidental duplicates in OCR pipelines, data extraction chains, etc. |

**None of the fixes reference specific providers, domains, or API patterns.** Every fix operates on schema structure, routing keys, or type enums — domain-agnostic by construction.

---

## Test Coverage

| Suite | Count |
|-------|-------|
| PuzzleEval core | 816 passed |
| puzzleeval-api | 30 passed |
| Generalizability bench | 39 passed (deselected by default) |
| **Total** | **885 tests** |
| TypeScript | clean |
| Vite build | clean |

(Count current as of the production-resilience pass. The historical
snapshot when this gap analysis was first published was 357 core / 426
total; see `POST_ROADMAP_ENHANCEMENTS.md` §§13-22 for the phases that
added the 459 additional tests.)
