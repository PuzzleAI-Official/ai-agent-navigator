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
2. **PHASE 2: BUILD** (Opus 4.6) — Write harness.py as a thin API client for all compatible input forms. Incompatible forms return `success=False, error="INCOMPATIBLE"`. Smoke test verifies structure. `ask_research` available for debugging.
3. **PHASE 3: VALIDATE** (Opus 4.6) — Live API validation required. Credentials are injected into the build sandbox. Run real API calls with test files (staged before build). Fix failures with full API context.
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
