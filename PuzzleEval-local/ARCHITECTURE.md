# PuzzleEval — V0 Agent Architecture

> **What is PuzzleEval?**
> An AI agent evaluation platform. Users describe what they need AI to do.
> We find relevant AI solutions, test them with synthetic data, and return
> a clear verdict: **Performance**, **Speed**, **Price**. Nothing else.

> **V0 Scope:** API-enabled AI providers only.
> **Future V1:** Browser-automation for providers without APIs (Selenium/Playwright-based agent interaction).

---

## The Pipeline at a Glance

```
USER INPUT (natural language + optional test files)
     │
     ▼
┌──────────────────────┐
│  1. USER UNDERSTANDING│
│     AGENT             │
│                       │
│  sets requires_test_  │
│  files = true/false   │
└─────────┬────────────┘
          │
          ├──────────────────────────────────────────────┐
          │                                              │
          │                              requires_test_files?
          │                                    │
          ▼                          ┌─────────┴─────────┐
┌──────────────────┐           false │                   │ true
│ 2. RESEARCH      │                ▼                   ▼
│    AGENT         │     ┌───────────────────┐ ┌────────────────────┐
│                  │     │ 3. SYNTHETIC TEST │ │ 3F. FILE-BASED     │
│ finds 5-7        │     │    AGENT          │ │     TEST AGENT     │
│ candidate AI     │     │                   │ │                    │
│ services         │     │ generates text-   │ │ reads user files,  │
│                  │     │ based synthetic   │ │ generates ground   │
│                  │     │ test data         │ │ truth from content │
└────────┬─────────┘     └────────┬──────────┘ └────────┬───────────┘
         │                        │                     │
         │                        └──────────┬──────────┘
         │                                   │
         ▼                          Agent3Result (same schema)
┌──────────────────┐                         │
│ 4. SCREENING     │                         │
│    AGENT         │                         │
│                  │                         │
│ validates API    │                         │
│ access, kills    │                         │
│ bad candidates   │                         │
└────────┬─────────┘                         │
         │ (3-5 validated candidates)        │
         │                                   │
         ▼                                   │
┌──────────────────┐                         │
│ 5. IMPLEMENT     │ ← one instance          │
│    TEST ENV      │   per candidate         │
│    AGENT (×N)    │                         │
│                  │                         │
│ builds thin API  │                         │
│ client harness   │                         │
│ per provider,    │                         │
│ runs all test    │                         │
│ cases in parallel│                         │
│ LLM judge evals  │                         │
└────────┬─────────┘                         │
         │                                   │
         ▼                                   │
┌──────────────────────────────────────────────┐
│ 7. ANALYZE AGENT (×N, independent per product)│
│                                              │
│ AI-as-judge: evaluates each product's output │
│ against ground truth INDEPENDENTLY           │
│ (no cross-product context to avoid bias)     │
└─────────────────┬────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────┐
│ 8. RANKING & AGGREGATION AGENT               │
│                                              │
│ compares all scored results, normalizes      │
│ scores, eliminates ordering bias, picks top 3│
└─────────────────┬────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────┐
│ 9. SUMMARIZATION REPORT                      │
│                                              │
│ Final output to user:                        │
│ Top 3 providers ranked by                    │
│ Performance | Speed | Price                  │
└──────────────────────────────────────────────┘
```

---

## Agent-by-Agent Specification

### Agent 1: User Understanding Agent

**Purpose:** Parse the user's natural language request into structured data that all downstream agents can consume.

**Input:**
- User's text description (required)
- Uploaded workflow file (optional: PDF, DOCX, CSV, images)

**Output — structured JSON:**
```json
{
  "summary": "One-sentence restatement of what the user needs",
  "sub_tasks": [
    {
      "description": "specific task as input→output behavior",
      "capability": "document OCR",
      "search_keywords": ["keyword1", "keyword2"],
      "requires_test_files": true,
      "test_file_description": "5-10 sample invoice photos or PDFs"
    }
  ],
  "domain": "e-commerce | healthcare | finance | marketing | ...",
  "search_keywords": ["keyword1", "keyword2", "keyword3"],
  "constraints": {
    "budget_range": "$50-200/mo | null",
    "must_have_features": ["feature1"],
    "integration_requirements": ["Shopify", "Slack", "..."],
    "technical_level": "non-technical | some-technical | technical"
  },
  "workflow_summary": "parsed summary of uploaded workflow file, or null"
}
```

**Key design decisions:**
- This agent MUST extract concrete, testable sub-tasks — not vague goals.
  "Handle customer support" is too vague.
  "Respond to product availability questions using a product catalog CSV" is testable.
- The keywords output is specifically crafted to help the Research Agent search effectively.
- If the user's request is too vague, this agent should generate clarifying questions back to the user BEFORE proceeding.

---

### Agent 2: Research Agent

**Purpose:** Find the 5-7 best-fit AI services for THIS specific user — not the best in the world, but the best for their background, technical ability, and use case.

**Input:**
- Structured output from User Understanding Agent (includes user's technical level, domain, constraints)

**Output:**
```json
{
  "candidates": [
    {
      "name": "ServiceName",
      "provider": "Company",
      "description": "what it claims to do",
      "api_available": true,
      "api_docs_url": "https://...",
      "pricing_model": "per-token | per-request | monthly",
      "claimed_capabilities": ["capability1", "capability2"],
      "relevance_score": 0.85,
      "adoption_difficulty": "easy | medium | hard",
      "source": "where we found this"
    }
  ]
}
```

**Architecture:** Two-step stateless function. Step 1: web search + Score→Weight→Rank selection. Step 2: structure into JSON.

**Selection strategy: Score → Weight → Rank**
1. **Search** comparison articles → collect 15-30 candidates (the pool)
2. **Score** every candidate on 3 dimensions (0-10): capability fit, adoption fit, use case fit
3. **Weight** dimensions based on user context (e.g., non-technical user: adoption 40%, use case 35%, capability 25%)
4. **Rank** by composite score → select top 5-7

**Key design decisions:**
- Overshoot to 5-7 candidates because the Screening Agent will kill some.
- Only include candidates where `api_available: true` (V0 scope).
- `relevance_score` is the composite user-fit score (not general capability). Higher = better fit for THIS user.
- `adoption_difficulty` describes objective setup complexity (easy/medium/hard). Derived from the adoption fit dimension score.
- Dimensional scoring forces explicit evaluation instead of selection by training-data familiarity bias. Claude must justify each candidate's scores.
- No hard rules like "never recommend X to Y users." Contextual reasoning within the scoring framework handles personalization.
- Cost: ~$0.30-0.40 per run (unchanged — same number of API calls).

---

### Agent 3: Synthetic Test Cases Agent (Text Mode)

**Purpose:** Generate synthetic text-based test cases with ground truth and weighted judgement criteria. Used when the evaluation does NOT require file-based testing (chatbots, text classification, text generation, API integrations).

**When to use:** Agent 1 sets `requires_test_files=false`.

**Input:**
- Structured output from User Understanding Agent (use cases, domain, workflow summary)

**Output:** `Agent3Result` (same schema as Agent 3F — see below)

---

### Agent 3F: File-Based Test Cases Agent (File Mode)

**Purpose:** Generate test cases using real user-uploaded files as test inputs. Reads each file via Claude vision (images/PDFs) or text extraction (DOCX/CSV/TXT), generates ground truth (expected_output) based on what it actually sees, and creates weighted judgement criteria. Acts as the "ground truth oracle."

**Simplified design:** One test case per file. No synthetic text generation. Each file becomes exactly one test case with ground truth extracted from the actual file content. This keeps the test suite focused on real data.

**When to use:** Agent 1 sets `requires_test_files=true` AND user provides files.

**Input:**
- Structured output from User Understanding Agent
- List of file paths provided by the user

**How users provide test files:**

The orchestrator (CLI or frontend) is responsible for collecting files from the user. The flow:
1. Agent 1 completes with `requires_test_files=true` and `test_file_description` (e.g., "5-10 sample invoice photos or PDFs")
2. The orchestrator shows the user `test_file_description` and asks for files
3. If user provides files → route to Agent 3F
4. If user declines ("skip") → fall back to Agent 3 (text mode, best-effort synthetic data)

In the CLI, this is an interactive prompt. In the frontend, this would be a file upload UI.

---

### Agent 3 / 3F Shared Output

Both agents produce the same `Agent3Result` schema:

```json
{
  "test_cases": [
    {
      "id": "tc-001",
      "sub_task_ref": "Extract structured data from invoice photos",
      "scenario": "A standard printed invoice from a US vendor with 3 line items",
      "input_type": "document_content",
      "input_data": "Invoice #4521\nVendor: Acme Corp\nDate: 2026-03-15\n...",
      "input_context": {"language": "en", "document_format": "invoice"},
      "test_file_path": "/uploads/invoice_001.pdf",
      "output_type": "extraction",
      "expected_output": "{\"vendor\": \"Acme Corp\", \"total\": 1250.00}",
      "judgement_criteria": [
        {"criterion": "Must extract vendor name", "weight": 0.3, "eval_type": "exact_match"},
        {"criterion": "Must extract total amount", "weight": 0.3, "eval_type": "exact_match"},
        {"criterion": "Must extract all line items", "weight": 0.3, "eval_type": "contains_key_info"},
        {"criterion": "JSON must be valid", "weight": 0.1, "eval_type": "format_compliance"}
      ],
      "difficulty": "easy",
      "tags": ["happy_path", "us_vendor"]
    }
  ],
  "generation_notes": "Generated 28 test cases across 4 sub-tasks...",
  "coverage_summary": {"Extract structured data from invoice photos": 8, "...": 7}
}
```

**Key differences:**
- Agent 3 (text): `test_file_path` is always null, `input_data` is synthetic text
- Agent 3F (file): `test_file_path` points to user file, `input_data` is text description extracted from the file

**Shared design decisions:**
- Test cases are generated INDEPENDENTLY from candidate services — this prevents bias.
- **Dynamic count** — scales with sub-task complexity: 5-8 cases per sub-task, +2-3 with workflow data. Min 10, max 50.
- **Coverage matrix** — each sub-task tested across 6 dimensions: happy_path, input_variation, edge_case, scale, domain_specific, error_resilience.
- **Universal format** — `input_type`/`output_type` abstraction works with any AI service. Agent 5's test runner adapts the data to each service's API.
- **Weighted judgement criteria** — each criterion has a weight (importance) and eval_type (how to judge), making Agent 7's scoring consistent and reproducible.

---

### Agent 4: Screening Agent — IMPLEMENTED

**Status:** Fully built and tested (23 unit tests). See `CLAUDE.md` for full design decisions.

**Purpose:** Verify that each candidate has REAL, publicly accessible API access. Enrich validated candidates with information Agent 5 needs (auth method, verified docs URL, data formats).

**Input:**
- `Agent4Input`: Agent 2's `Agent2Result` (5-7 candidates) + Agent 1's `UserUnderstandingOutput` + `trace_id`

**Output:**
```json
{
  "validated_candidates": [
    {
      "name": "Mindee Invoice OCR API",
      "provider": "Mindee",
      "verified_api_docs_url": "https://developers.mindee.com/docs/invoice-ocr",
      "auth_method": "api_key",
      "api_access_method": "free_signup",
      "confirmed_capabilities": ["Invoice OCR from PDF/JPG/PNG", "Line-item extraction"],
      "data_format_notes": "Input: PDF/JPG/PNG via binary upload. Output: Structured JSON.",
      "screening_notes": "Search confirmed REST endpoints at api.mindee.net/v1...",
      "...other Candidate fields carried forward..."
    }
  ],
  "rejected_candidates": [
    {
      "name": "EnterpriseOnlyOCR",
      "provider": "BigCorp",
      "rejection_reason": "No public API documentation found. Only 'Contact Sales' page.",
      "rejection_category": "enterprise_only"
    }
  ],
  "screening_summary": "Screened 7 candidates. 5 passed, 2 rejected.",
  "total_candidates_screened": 7
}
```

**Architecture:** N parallel per-candidate API calls (ThreadPoolExecutor) + 1 structuring call.

**Verification strategy (4-step progressive search + fetch with link-following):**
1. **SEARCH standard** — `"{name} API documentation"` — if search results clearly show real API docs → PASS
2. **SEARCH capability-specific** — `"{name} OCR API"` or `"{name} REST API"` — catches APIs labeled under feature names
3. **SEARCH site-scoped** — `site:{domain} API` — finds any API-related page on the service's own domain
4. **FETCH progressive** — (a) most promising docs URL from search, (b) product homepage to explore navigation, (c) follow the most promising API-related link found on the homepage

**Evidence-based PASS rule:** If ANY evidence of an API exists (marketing mentions, pricing tiers, broken docs URLs, SDK packages), Agent 4 MUST PASS with notes. Only reject when genuinely zero evidence exists. A false pass costs nothing (Agent 5 catches it); a false reject loses a valid candidate.

**Key design decisions:**
- Per-candidate isolation prevents token accumulation (the lesson from Agent 2's $10/run disaster)
- Search-first cuts per-candidate cost from $0.45 to $0.15 (avoids fetching full API references)
- When uncertain, PASS with notes — Agent 5 will verify deeper when building the test harness
- `ScreenedCandidate` is a separate model from `Candidate` — guarantees Agent 5 gets non-optional enrichment fields
- Semantic capability matching is done by the agent (Claude), NOT the validator (keyword matching produces false warnings)
- 3 web searches + 3 web fetches per candidate (max_uses=3 each)
- Target: pass 3-5 candidates through. Cost: ~$1.00-1.35 per screening run

---

### Agent 5: Implement Test Env Agent (×N)

**Purpose:** For each validated candidate, build a thin Python API client harness, validate it with live API calls, then execute all test cases and evaluate results with an LLM judge.

**Input:**
- `Agent5Input`: validated candidates from Agent 4, user understanding from Agent 1, test cases from Agent 3, trace_id, provider credentials

**Output:**
- `Agent5Result` containing:
  - `harnesses: list[TestHarness]` — built harnesses with code, requirements, auth, api_knowledge
  - `failed_harnesses: list[FailedHarness]` — candidates that failed building
  - `candidate_runs: list[CandidateTestRun]` — test execution results per harness
  - `failed_test_runs: list[FailedCandidateRun]` — candidates where test execution failed
  - Build + test cost summaries

**Architecture: One agent per candidate (research + build in one context) + post-loop parallel test execution with LLM judge.**

The builder agent does its own research in Phase 1 (no separate research sub-agent — eliminates knowledge handoff loss). After building, Python infrastructure runs all test cases in parallel across candidates. Evaluation uses an LLM judge comparing raw API responses against ground truth — no mechanical eval.

**The harness objective:** A thin API client that sends input (file or text) to the service and returns the raw response. The harness does NOT parse, extract, or interpret results — that is the LLM judge's job.

**The flow per candidate:**
1. **PHASE 1: RESEARCH** (Sonnet 4.6) — Server-side web_search and web_fetch to find API docs. Write api_spec.txt with INPUT_COMPATIBILITY, ROUTING_TABLE, PYTHON_EXAMPLES, DOC_MAP, DOC_REFERENCES. Agent knows ALL test case input forms upfront.
2. **PHASE 2: BUILD** (Opus 4.7) — Write harness.py as a thin API client for all compatible input forms. Incompatible forms return `success=False, error="INCOMPATIBLE"`. Smoke test verifies structure. `ask_research` available for debugging.
3. **PHASE 3: VALIDATE** (Opus 4.7) — Live API validation required. Credentials are injected into the build sandbox. Run real API calls with test files (staged before build). Fix failures with full API context.
4. **HARNESS_COMPLETE** — Signal completion. Milestone message on smoke test pass.
5. **POST-LOOP** (Python, parallel across candidates) — Run ALL test cases through harness.run(). Evaluate with LLM judge: raw API response (truncated to 15K chars) compared against ground truth and judgement criteria. Produce CandidateTestRun with aggregate metrics.

**Key design decisions:**
- **Thin API client, not full parser:** The harness sends requests and returns raw responses. All intelligence is in the LLM judge, not the harness code.
- **One agent, one context:** Research and building happen in the same conversation. The model writes code while the actual API docs are still in context. No knowledge handoff loss.
- **Opus advisor tool:** Sonnet/Opus can call `advisor()` for strategic guidance. Called ~1x per candidate before writing code.
- **Context engineering:** `max_content_tokens: 15000` on web_fetch prevents context explosion. Server-side `clear_tool_uses` + `compact` for automatic management. Automatic prompt caching (83-86% hit rate).
- **Behavioral instructions:** Claude Code-inspired patterns (`<do_not_narrate>`, `<investigate_comprehensively>`, `<verify_against_docs>`, etc.)
- **No cosmetic verification:** If smoke + live test pass, accept the harness. Live validation IS verification.
- Cost: ~$1.00-1.50 per candidate for build, ~$0.10-0.50 for test execution

**Subprocess seam + bytes round-trip (NEW-AH):**
Harness execution is via `subprocess.run(venv_python, "-c", driver_script, ...)`. The `driver_script` is an inline Python snippet constructed by `_execute_single_test`:

```python
# Conceptual — see implement_test_env.py for the full encoder
def _bytes_safe(obj):
    if isinstance(obj, (bytes, bytearray)):
        return {"_b64": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, dict):
        return {k: _bytes_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bytes_safe(v) for v in obj]
    return obj

result = harness.run(input_data)
safe = _bytes_safe(result)
json.dump(safe, open("_test_output.json", "w"), default=str)
```

On the read side, Agent 5's `_inflate_b64_sentinels` walks the JSON tree and re-inflates `{"_b64": "..."}` → real bytes before returning to the plugin. This is the stable seam for every harness that ships binary data (audio, images, file blobs) across the JSON border.

**Credential resolution** (`_resolve_candidate_credentials`): union of every registered provider whose normalized key appears anywhere in the candidate's searchable surface (name + provider + validation_notes). Enables cross-provider harnesses like "ElevenLabs Voice Stack" to get ElevenLabs + OpenAI + Anthropic keys from one lookup. No breaking schema change — still reads from `Agent5Input.provider_credentials` dict. See `tool_plugins/voice_realtime.py` docstring for the cross-provider recipe.

**Multi-call modality pre-call skip** (NEW-AH): test cases with `input_type ∈ {conversation, voice_conversation, voice_turn}` or `output_type ∈ {voice_turn, voice_conversation}` bypass the initial harness pre-call. See "Multi-call modality ownership" under the plugin ecosystem section below — the plugin owns every real harness invocation.

---

### Agent 7: Analyze Agent (×N, Independent)

**Purpose:** AI-as-judge. Evaluate the quality of each candidate's outputs against ground truth.

**Input:**
- Test results for ONE provider (from Agent 5's test execution)
- Ground truth + judgement criteria (from Synthetic Test Cases Agent)

**Output:**
```json
{
  "provider": "ServiceName",
  "scores": [
    {
      "test_case_id": "tc-001",
      "criteria_scores": {
        "Must correctly identify the product mentioned": { "pass": true, "confidence": 0.95 },
        "Must provide accurate availability info": { "pass": true, "confidence": 0.88 },
        "Tone must be professional": { "pass": true, "confidence": 0.92 }
      },
      "overall_pass": true,
      "quality_score": 0.92
    }
  ],
  "aggregate_quality_score": 0.87
}
```

**Key design decisions:**
- **CRITICAL: Each provider is judged in a SEPARATE agent call with NO knowledge of other providers' results.** This eliminates ordering bias and cross-contamination.
- Scoring is criteria-based (did it meet each specific criterion?) not vibes-based.
- Confidence scores let the Ranking Agent weigh high-confidence results more.
- The judge prompt must be carefully designed to be consistent across all evaluations.

---

### Agent 8: Ranking & Aggregation Agent

**Purpose:** Combine all independent analysis results into a final comparative ranking.

**Input:**
- All Analyze Agent outputs (quality scores per provider)
- All Agent 5 test execution outputs (speed + cost metrics per provider)

**Output:**
```json
{
  "rankings": [
    {
      "rank": 1,
      "provider": "ServiceName",
      "scores": {
        "performance": 87,
        "speed": 92,
        "price": 74
      },
      "overall_match": 84,
      "highlights": "Best at X, weakest at Y",
      "estimated_monthly_cost": "$45-80"
    }
  ]
}
```

**How the 3 scores are calculated:**
- **Performance** (0-100): Derived from Analyze Agent's quality scores. What % of use cases did this provider handle correctly and well?
- **Speed** (0-100): Normalized from average latency. Fastest candidate = 100, others scaled relative.
- **Price** (0-100): Normalized from total cost per test suite. Cheapest = 100, others scaled relative. Extrapolated to estimated monthly cost based on user's expected volume.

**Key design decisions:**
- Normalization is relative to the candidate pool (not absolute) — so scores are always comparative.
- Top 3 are selected for the final report.
- Ties are broken by performance first, then speed, then price.

---

### Agent 9: Summarization Report

**Purpose:** Generate the final user-facing report.

**This is not an agent — it's a rendering step.** Takes the Ranking Agent's structured output and presents it in the UI.

**What the user sees:**
- Their original request summarized in one sentence
- Top 3 providers, each showing:
  - Provider name + one-line description
  - 3 score bars: Performance, Speed, Price (each 0-100)
  - Overall match percentage
  - Expandable details: test breakdown, specific strengths/weaknesses, estimated monthly cost
  - Link to provider's website
- Option to view side-by-side comparison
- Option to refine and re-run

---

## Data Flow Summary (for Claude Code reference)

```
UserInput (+ optional test files)
  → [Agent 1] → UserProfile (JSON)
      → [Agent 2] → CandidateList (JSON)         ← PARALLEL with Agent 3/3F
      → [Agent 3 OR 3F] → TestSuite (JSON)       ← PARALLEL with Agent 2
         ↑ Agent 3 if text-only (requires_test_files=false)
         ↑ Agent 3F if user provides files (requires_test_files=true)
          → [Agent 4] → ValidatedCandidates (JSON)
              → [Agent 5 ×N] → TestHarness[] + TestResults[]  ← PARALLEL per candidate
                  → [Agent 7 ×N] → AnalysisScores[]  ← PARALLEL per candidate, ISOLATED
                          → [Agent 8] → FinalRankings (JSON)
                              → [Render] → User Report (UI)
```

**Parallelization points:**
- Agent 2 (Research) and Agent 3/3F (Test Cases) run in PARALLEL — when `--agent5` is used, Agent 2 and Agent 3F run concurrently since neither depends on the other
- Agent 5 (Build) runs N instances in PARALLEL (one per candidate)
- Agent 5 post-loop test execution runs in PARALLEL across candidates
- Agent 7 (Analyze) runs N instances in PARALLEL, each ISOLATED

**Sequential dependencies:**
- Agent 4 must wait for Agent 2
- Agent 5 must wait for BOTH Agent 4 AND Agent 3/3F
- Agent 7 must wait for Agent 5
- Agent 8 must wait for ALL Agent 7 instances

---

## V1 Roadmap (not in scope for V0)

- **Browser Automation Path:** For AI services without APIs, use Playwright/Selenium-based agents to interact with web UIs directly. The Implement Test Env Agent would spawn a browser agent instead of writing API integration code.
- **Agent-to-Agent Hiring:** Programmatic interface where an AI agent (not a human) submits evaluation requests via API. Same pipeline, different input interface.
- **Continuous Monitoring:** Re-run evaluations periodically to catch provider regressions.
- **User Data Privacy:** Sandboxed test environments, data encryption, no real user data in test cases.

---

# Current module index (beyond the 5 agents)

The agents describe *what the pipeline does*; the modules below describe *how it stays resilient, accurate, and honest* while doing it. Every module listed is production code exercised by the 885-test suite and the FastAPI runner.

## Core infrastructure

| Module | Lines | Role |
|---|---:|---|
| `puzzleeval/anthropic_client.py` | ~160 | Central client factory — every agent builds its Anthropic client here (120 s timeout, `max_retries=3`, Opus→Sonnet→Haiku fallback ladder via `call_with_model_fallback()`). Replaces the previous pattern of each agent instantiating its own bare-default client. |
| `puzzleeval/structured_output.py` | ~260 | `parse_with_fallback()` — drop-in replacement for `client.messages.parse(output_format=...)` that degrades to a non-strict tool-call path when Anthropic's compiled-grammar size/timeout limit hits. Defensive coercion of Python-repr array strings. Wired into all 6 structured-output call sites. |
| `puzzleeval/budget.py` | ~150 | `RunBudget` cost circuit-breaker. Threadsafe accumulator with a hard USD cap (default $25 via `PUZZLEEVAL_MAX_RUN_COST_USD`). Raises `BudgetExceededError` when crossed. Surfaced as HTTP 402 at the chat endpoint and as `pipeline_failed reason="budget_exceeded"` at the pipeline boundary. |
| `puzzleeval/config.py` | ~700 | Every env-var configurable knob lives here. Effort tier (`PUZZLEEVAL_EFFORT`), model selection, caching toggles, feature flags for hybrid eval / programmatic tools / adaptive thinking. |
| `puzzleeval/schemas.py` | ~2,800 | Every agent boundary's Pydantic contract. Recent migration: `Candidate.covers_step_ids` and `ScreenedCandidate.covers_step_ids` are now `list[str]` (not `frozenset[str]`) — JSON Schema has no native frozenset type, and the old choice forced the non-strict fallback path on every run. |
| `puzzleeval/validators.py` | ~1,100 | Canonical enum definitions (`VALID_INPUT_TYPES` / `VALID_OUTPUT_TYPES`) and structural validation for every Pydantic output. Single source of truth — schemas' field `description`s reference this module by name to avoid drift. |
| `puzzleeval/exceptions.py` | — | `AgentRateLimitError`, `AgentAPIError`, `AgentOutputError`, `AgentFileParseError`. Each classifies a recoverable vs fatal condition so the pipeline runner can emit the right SSE event. |

## Accuracy & reporting

| Module | Role |
|---|---|
| `puzzleeval/test_data_sufficiency.py` | First-class verdict per scope: `READY` / `AUGMENT` / `SYNTHESIZE` / `REQUEST_MORE` / `DEGRADE`. Walks the file inventory, detects wrong-extension uploads, variety risk (all files share a prefix), sub-minimum counts. Emitted as `test_data_sufficiency` SSE event. |
| `puzzleeval/report.py` | Final `EvaluationReport` assembler — deterministic ranking, per-scope winners, evidence rows (top-3 failures + top-3 successes per candidate), monthly cost projection (uses `pricing.py` + user's `monthly_volume`), pros/cons heuristics, sandbox-disclosure flags, coverage-gap advisories. Persisted to `runs/<trace>/evaluation_report.json` + emitted as SSE + served at `GET /runs/{id}/report`. |
| `puzzleeval/modality.py` | Dispatcher. Given a test case's `(input_type, output_type)`, queries the plugin registry for all plugins claiming to handle that modality. Fully data-driven — no `if scope_role == X` branches anywhere. |
| `puzzleeval/hybrid_evaluator.py` | Opt-in (`PUZZLEEVAL_HYBRID_EVAL_ENABLED=1`) second-look pass for ambiguous modalities: exposes all plugins as Claude-callable tools, lets the model pick one. Only runs when the deterministic dispatch yields no plugin. |
| `puzzleeval/pricing.py` | `estimate_monthly_cost(breakdown, monthly_volume)` — projects a candidate's claimed pricing breakdown against the user's stated volume. Called by the report assembler. |
| `puzzleeval/plugin_tools.py` | `build_plugin_tool_definitions()` + `dispatch_plugin_tool()` — exposes plugins as Anthropic-callable tool schemas for the hybrid evaluator. |
| `puzzleeval/plugin_status.py` | `PLUGIN_WIRING` registry + `snapshot_plugin()` — where each plugin is actually wired (synthesis, evaluation, builder context) plus per-credential advice when a plugin is missing keys. |

## Research & verification

| Module | Role |
|---|---|
| `puzzleeval/deep_verify_runner.py` | Phase 6.5 per-candidate deep-verify loop (4A→4B→4C→4D). Reads docs, writes provider atlas + spec to `memdir/`, confirms scope-by-scope which claimed coverage is real. |
| `puzzleeval/deep_verify_prompt.py` | The deep-verify system prompts. |
| `puzzleeval/provider_atlas.py` | Structured atlas of a provider's API surface (endpoints, auth, rate limits, sandbox). Cross-run reusable. |
| `puzzleeval/memdir.py` | Per-category cross-run memory (`~/.puzzleeval/memdir/<category>/<provider>.md`). 14-day TTL, frontmatter + body. Used for api_specs, quirks, atlas. |
| `puzzleeval/manual_atlas.py` | When Phase 6.5 can't auto-build an atlas, `manual_atlas_from_llm()` falls back to structured output from a plain research call. |
| `puzzleeval/openapi_harness.py` | When a provider publishes an OpenAPI spec, we use it directly instead of asking Agent 5 to reconstruct. |
| `puzzleeval/web_fetch_fallback.py` | Plain HTTPS GET when Anthropic's `web_fetch` tool returns nothing (some sites block it). |
| `puzzleeval/adversarial_verifier.py` | Probes candidate harnesses with empty / max / malformed / idempotency / concurrency / auth-error inputs. |

## Orchestration

| Module | Role |
|---|---|
| `puzzleeval/pipeline.py` | CLI-side pipeline orchestrator. Mirrors `puzzleeval-api/services/pipeline_runner.py` but for standalone CLI runs. |
| `puzzleeval/cli.py` | `python -m puzzleeval.cli` entrypoint — conversational Agent 1 loop + pipeline execution. |
| `puzzleeval/selection.py` | Phase 7 per-scope top-K selection — picks the default candidate set the UI shows in the SelectionPanel. |
| `puzzleeval/scope_routing.py` | Per-scope test routing — maps tests to candidates via blueprint scope IDs. |
| `puzzleeval/rate_limiter.py` | Per-provider concurrency + request-rate caps. Used inside Agent 5's test execution pool. |
| `puzzleeval/file_parsers.py` | PDF / DOCX / CSV / TXT / image parsing for Agent 1 and Agent 3F. |
| `puzzleeval/agent_preamble.py` | Shared preamble text every agent's system prompt gets prefixed with. |
| `puzzleeval/logging_setup.py` | Structured JSON logging with cost/latency/token extras. |
| `puzzleeval/provider_registry.py` | Read-only accessor for the user's `provider_registry.json` credential store. Tolerates missing/corrupt files (returns empty registry, logs a warning). |

---

# Tool plugin ecosystem (8 plugins)

The plugin architecture lets each modality plug in input synthesis + output evaluation without touching Agent 5. Plugins auto-register at import time via `puzzleeval/tool_plugins/__init__.py`; the modality detector queries the registry by capability.

| Plugin | Required credential(s) | Synthesizes | Evaluates | Role |
|---|---|:---:|:---:|---|
| `code_execution` | (none) | ✓ | ✓ | Runs generated code in a sandbox; scores by exit code + output match |
| `vision` | `ANTHROPIC_API_KEY` | — | ✓ | Scores image responses via Claude vision |
| `transcription` | `OPENAI_API_KEY` OR `DEEPGRAM_API_KEY` OR `ASSEMBLYAI_API_KEY` | — | ✓ | STTs audio responses, scores transcript against expected |
| `tts` | `OPENAI_API_KEY` OR `ELEVENLABS_API_KEY` | ✓ | — | Synthesizes audio test inputs for voice agents |
| `conversation_simulator` | (none) | ✓ | ✓ | Multi-turn scripted conversations with per-turn assertions |
| `webhook_receiver` (NEW) | (none — local) | ✓ | ✓ | Captures inbound HTTP callbacks (Slack / Intercom / Stripe / Twilio / GitHub / generic shapes). Binds 127.0.0.1:8765 lazily. `PUZZLEEVAL_TUNNEL_URL` for offsite candidates. |
| `outbound_delivery` (NEW) | (none — local) | ✓ | ✓ | Mock SMTP (port 2525), Slack-webhook HTTP (8766), SMS-Twilio HTTP (8767). Verifies messages actually landed. |
| `voice_realtime` (NEW) | (none; STT needs transcription key, TTS needs `OPENAI_API_KEY` or `ELEVENLABS_API_KEY`) | ✓ | ✓ | Local audio loopback — serves synthesized caller audio, captures TwiML / NCCO / JSON / audio-blob responses. **Multi-turn**: owns `drive_conversation` (N caller+agent turns, per-turn substring scoring, full-conversation MP3 merge with ID3-tag stripping). **Thread-local session_dir** so parallel candidates write to their own `runs/<trace>/harnesses/<slug>/voice/` folder. Audio artifacts round-trip through the Agent-5 `{"_b64": "..."}` sentinel — harnesses return raw bytes, plugin gets raw bytes, JSON border is transparent. |

**Registry guards:** `register_plugin()` warns on duplicate-name conflicts (or raises with `PUZZLEEVAL_STRICT_PLUGIN_REGISTRY=1`). Plugin bind-addresses default to `127.0.0.1` for security. All HTTP servers set `allow_reuse_address=True` so uvicorn restarts rebind cleanly. DoS caps: SMTP per-line 8 KB / total DATA 25 MiB, HTTP body 1 MiB.

**Multi-call modality ownership (NEW-AG / NEW-AH):**

Multi-turn tests (voice, chatbot) are owned by a PLUGIN, not by Agent 5 directly. The plugin's `evaluate_output(response, expected, criteria, harness_runner=...)` receives the harness runner callable and owns the N-turn loop. The single-turn harness is the same shape regardless of modality. Three patterns are set in stone:

1. **Direct-invoke fast path** (`plugin_tool_runner.py`): when a plugin has `requires_harness_runner=True` AND its modality enums match the test's input_type/output_type, it's invoked DIRECTLY before Claude's tool-picker runs. Eliminates the non-determinism observed in voice_dual_4 where Claude sometimes picked voice_realtime and sometimes skipped it. Priority-sorted (most specific `output_type` match wins). Log operation: `tool_runner_direct_invoke_owner`.

2. **Plugin verdict promotion** (`plugin_tool_runner.py`): when Claude's tool_runner finishes WITHOUT emitting a structured `ScoreVerdict`, the LAST conclusive plugin verdict is promoted directly instead of collapsing to LLM-judge single-turn fallback. Plugin scoring IS authoritative. Log operation: `tool_runner_promote_plugin_verdict`.

3. **Multi-call pre-call skip** (`_execute_all_tests`): test cases with `input_type ∈ {conversation, voice_conversation, voice_turn}` or `output_type ∈ {voice_turn, voice_conversation}` skip the initial single-turn `_execute_single_test` call. The plugin's drive-loop owns every real harness invocation. Without this, strict harnesses (ElevenLabs Voice Stack) would return `success=False` on the bogus pre-call payload (no `audio_url` / `turn_index`) and skip the plugin path entirely. Log operation: `multi_call_pre_call_skipped`.

**Voice audio stack** (`tool_plugins/voice_realtime.py`):

- `synthesize_input()` → TTS caller audio via `tts` plugin chain, serves at `/audio/<token>` on 127.0.0.1:8768.
- `drive_conversation(script, agent_responder)` → per-turn loop: synth caller → invoke responder → extract agent response (audio_bytes / text / twiml / ncco / json) → score per-turn assertion.
- `_merge_conversation_audio(session_token, turns)` → stitches caller+agent files in dialogue order into `conversation_<token>.<ext>`. MP3-specific: strips ID3v2 tag from segments 2…N (first segment keeps it for codec init), strips ID3v1 trailers from every segment. Same-extension required; mixed types bail with `None` and per-turn files remain available.
- `artifacts_for_token_prefix(session)` → returns all caller + agent + merged files registered under a session token (each turn uses sub-token `<session>-t<idx>`).

**Audio extension / content-type derivation** (`_responder`): when the harness returns `raw_response = {"audio_bytes": <bytes>, "audio_format": "mp3"}` without an explicit `audio_content_type`, the plugin derives `audio/mpeg` from the format field. Otherwise `_save_audio_blob` would default to `.wav` → corrupted MP3 playback + mixed-extension merge failure.

**Backend audio streaming** (`puzzleeval-api/routes/runs.py:serve_run_audio`):

`GET /runs/audio?path=<absolute_path>` streams audio through with a containment check. Allowed roots (expanded in NEW-AG):
1. `puzzleeval-api/runs/` (backend-driven pipeline runs)
2. `PuzzleEval-local/runs/` (CLI-driven dev runs — auto-detected via sibling-dir lookup)
3. Any comma-separated path in `PUZZLEEVAL_EXTRA_RUNS_ROOTS` env var.

Paths outside every allowed root → HTTP 403. Containment uses `Path.resolve().relative_to(root)` — no path-traversal holes.

**Frontend audio rendering** (`src/components/playground/EvaluationReportCard.tsx::AudioPathsBlock`):

Role-based style table (`ROLE_STYLE`): `conversation` → violet "Full call" badge, full-width control; `caller` → blue, compact; `agent` → emerald, compact. The merged `role=conversation` clip always renders first and widest. Adding a new role is a single-entry map update.

---

# Resilience infrastructure

Everything in this section is why a real run won't silently corrupt or hang.

### `.env` autoload (`puzzleeval/__init__.py:_autoload_dotenv`)
- Walks from CWD + package location to find `.env` or `puzzleeval-api/.env`.
- **Empty-string shadow defense:** if a key declared in the `.env` file is already in `os.environ` as an empty/whitespace-only value, that entry is evicted before `load_dotenv` runs — so `ANTHROPIC_API_KEY=` (CI placeholder) doesn't silently shadow the real value. Real non-empty shell values still win (`override=False`).

### Structured-output fallback (`puzzleeval/structured_output.py`)
- Strict path first: `client.messages.parse(output_format=PydanticModel)`.
- On "compiled grammar too large" / "Grammar compilation timed out" 400s, fall through to `client.messages.create()` with a non-strict tool whose `input_schema` is the same JSON Schema.
- Defensive post-processing: unwrap `{"input": {...}}` over-nesting, coerce Python-repr strings (`"frozenset({'x'})"`) back to real arrays before Pydantic validation.
- Wired into Agents 1, 2, 3, 3F, 4, and 5's LLM evaluator.

### Budget circuit-breaker (`puzzleeval/budget.py` + `RunState.record_cost()`)
- Every cost-recording site (agent completions, Agent 1 chat turns, aggregate) calls `state.record_cost(usd, reason)`.
- Raises `BudgetExceededError` when the running total crosses the cap.
- Chat endpoint surfaces HTTP 402; pipeline runner surfaces `pipeline_failed reason="budget_exceeded"` with spent/cap/recovery hint.

### Plugin lifecycle (`puzzleeval-api/main.py:lifespan`)
- On startup: touches the plugin registry so every plugin imports/registers.
- On shutdown: calls `shutdown()` on every plugin that exposes one — HTTP/SMTP servers close cleanly across `uvicorn --reload` and redeploys.

### Upload + SMTP DoS caps
- `routes/files.py:_read_with_cap` — stream-reads with `MAX_UPLOAD_BYTES_PER_FILE` (100 MiB default), aborts with HTTP 413.
- `outbound_delivery.py` — `SMTP_MAX_LINE_BYTES` (8 KB) + `SMTP_MAX_DATA_BYTES` (25 MiB).

### Frontend SSE resilience (`src/services/api.ts:subscribeToEvents`)
- Exponential backoff reconnect (1s → 2s → ... → 30s cap).
- Preserves `lastEventId` across reconnects.
- Surfaces `connecting/open/reconnecting/closed` status to the UI.
- "No progress for Xm" banner in `Playground.tsx` when `stage === "pipeline"` and `Date.now() - lastEventAt > 2min`.

---

# SSE event catalog

Every event the backend emits, and which UI component consumes it.

| Event | Payload | Consumer |
|---|---|---|
| `pipeline_started` | `{trace_id}` | sets `stage="pipeline"` |
| `workflow_blueprint` | `{workflow, test_plan}` | `WorkflowDiagram`, chat message |
| `test_data_sufficiency` | `{summary, verdicts[]}` | chat message — READY / AUGMENT / SYNTHESIZE / REQUEST_MORE / DEGRADE per scope |
| `agent_started` | `{agent, name}` | pipeline nodes + activity feed |
| `agent_activity` | `{agent, message, status}` | activity feed |
| `agent_thinking` | reserved | (subscribed; emission deferred — known gap) |
| `agent_completed` | `{agent, cost_usd}` | pipeline nodes |
| `agent_blocked` | `{agent, reason}` | Phase 2 billing gate |
| `candidates_found` | `{candidates[]}` | `CandidateCard` + `CoverageMatrix` |
| `coverage_gap` (NEW) | `{candidate_count, missing_scopes, user_message}` | chat message — fires when zero candidates or any scope has no coverage |
| `selection_required` | `{run_id, trace_id, ...}` | `SelectionPanel` pause |
| `candidates_selected` | `{scope_picks, user_added}` | post-selection resume |
| `candidate_verified` | `{candidate, scope_id, ...}` | Phase 6.5 per-scope deep-verify result |
| `candidate_rejected` | `{candidate, reason}` | Phase 6.5 |
| `scope_verified_complete` | `{scope_id, verified, rejected}` | Phase 6.5 summary |
| `candidates_verified` | `{validated, rejected}` | Agent 4 complete |
| `test_cases_ready` | `{test_count}` | Agent 3 complete |
| `harness_started` | `{candidate}` | Agent 5 per-candidate |
| `harness_completed` | `{candidate}` | Agent 5 per-candidate |
| `harness_failed` | `{candidate, reason}` | Agent 5 per-candidate |
| `test_execution_started` | `{candidate, test_count}` | Agent 5 |
| `test_result` | `{candidate, test_case_id, passed, score}` | Agent 5 per-test |
| `candidate_results_ready` | `{candidate, scores}` | Agent 5 per-candidate |
| `cost_update` (NEW) | `{source, delta_usd, total_cost_usd, budget: {spent_usd, cap_usd, remaining_usd, utilization}}` | live cost meter in `usePipelineRun` |
| `report_generating` | `{}` | shows "Generating evaluation report..." |
| `evaluation_report` (NEW) | full `EvaluationReport` dict | `EvaluationReportCard` renders winner + per-scope + evidence |
| `pipeline_completed` | `{total_cost_usd, budget, summary}` | sets `stage="results"` |
| `pipeline_failed` | `{error, reason?, spent_usd?, cap_usd?, recovery?}` | error banner; `reason="budget_exceeded"` gets special UI |
| `pipeline_cancelled` | `{}` | cancel confirmation |
| `done` | `{}` | closes SSE stream |

---

# Honest gap list (what's NOT production-grade yet)

These are acknowledged gaps from the Claude-Code-parity audit. None are fundamental; all are wiring exercises.

1. **Agent 5 cancellation** — `state.cancel_requested` is checked at agent boundaries but not inside `_build_single_harness`'s 25-turn loop. An 8-minute build is uninterruptible once started.
2. **Agent 5 model fallback** — `call_with_model_fallback()` is the reusable helper but Agent 5 still hard-fails on persistent Opus 4.7 rate-limits (only uses SDK-level retries).
3. **Live `agent_thinking` streaming** — extended-thinking blocks exist in responses but are never extracted to SSE. User sees silent spinners during Opus planning.
4. **Incremental token-level streaming** — Agent 5 builder calls are blocking `messages.create()`, not `stream=True`. Cost/progress only updates at turn boundaries (~30-60 s).
5. **Agent 2 per-scope parallelism** — one serial research call for N-scope blueprints. Wall-clock cost, not correctness.
6. **In-run web_fetch URL cache** — same doc fetched per-candidate pays per-candidate.
7. **Idempotency keys on writes** — a retried Stripe/Slack write could create duplicates in the real provider's account.
8. **DRY_RUN propagation into harness generation** — `side_effects=creates_records` scopes could leak test data unless the candidate publishes a sandbox URL.
9. **Provider-quirk registry** — Stripe-Version, OpenAI-Beta, anthropic-version headers aren't in a structured registry; Agent 5 re-discovers from docs each build.
10. **AWS SigV4 / OAuth2 authorization_code / mTLS auth patterns** — not in `api_patterns.py`; rare auth flows will fall back to generic HTTP patterns and likely fail.

These are tracked in `POST_ROADMAP_ENHANCEMENTS.md`.
