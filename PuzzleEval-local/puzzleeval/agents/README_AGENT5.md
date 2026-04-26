# Agent 5: Build + Test Agent — Complete Walkthrough

> **Status (2026-04-11):** 4/4 builds passing, 3/4 test execution pass (working_test_6).
> Cost: ~$4.40/run for 4 candidates. 7-9 turns per candidate, 289s total.
> Agent 5 handles BOTH harness building AND test execution natively.

> This document explains Agent 5's architecture, design decisions, and lessons learned.
> Agent 5 evolved through extensive iteration from 0/4 to stable 4/4. The key insight:
> removing knowledge boundaries (research→builder merge, builder→tester merge) is
> always better than building infrastructure to bridge them.

---

## Current Architecture (2026-04-10)

### What Agent 5 Does Now
1. **Builds** a thin Python API client harness for each AI service candidate (sends file/text, returns raw response)
2. **Validates** the harness with live API calls (credentials injected into build sandbox)
3. **Executes** ALL test cases in parallel across candidates
4. **Evaluates** results using an LLM judge comparing raw API response vs ground truth (no mechanical eval)

The harness is intentionally thin — it sends input to the API and returns the raw response. All intelligence for evaluating results lives in the LLM judge, not the harness code. This makes harnesses simpler to build and more robust.

Steps 3-4 are owned by Agent 5 because:
- Agent 5 already has full API understanding from research
- Separating test execution into a different agent would create a knowledge boundary (misclassifications, wasted cost)
- The test execution itself is mechanical (no LLM needed for running a for-loop); evaluation uses one LLM call per test case

### The Flow Per Candidate

```
Phase 1 (Sonnet 4.6): Server-side web_search/web_fetch for API docs → write api_spec.txt
  ↓ [model switch when api_spec.txt written]
Phase 2 (Opus 4.7): Build thin API client harness.py + smoke test (ask_research for debugging)
  ↓ [smoke passes → milestone message]
Phase 3 (Opus 4.7): Live API validation required (credentials + test files staged in sandbox)
  ↓ [HARNESS_COMPLETE]
Post-loop (Python, parallel across candidates): Run ALL test cases → LLM judge eval (raw response truncated to 15K) → aggregate metrics
```

### Model Strategy
- **Sonnet 4.6** for Phase 1 research (server-side web_search/web_fetch, cheap, I/O-heavy)
- **Opus 4.7** for Phase 2-3 build/validate (needs strong reasoning)
- **Opus 4.7 Advisor** available in all phases via `advisor-tool-2026-03-01`
- **Sonnet 4.6** for `ask_research` (Phase 2+ debugging, targeted web search, doesn't need Opus)

### Key Design Principles (Lessons Learned)
1. **Thin API client, not full parser.** The harness sends requests and returns raw responses. The LLM judge handles all evaluation intelligence.
2. **No knowledge boundaries.** Research and coding in ONE context. No separate spec-to-code translation.
3. **Let the agent do what it's good at.** Research, code, debug. Let infrastructure do loops and metrics.
4. **Behavioral instructions over prescriptive rules.** Shape reasoning patterns, don't write recipes.
5. **Live validation IS verification.** If the live API call works, accept the harness. No separate verification scripts or cosmetic code review.
6. **Comprehensive research output.** api_spec.txt includes INPUT_COMPATIBILITY, ROUTING_TABLE, WORKING_EXAMPLE (any language — Python / curl / JS / Go / raw HTTP), DOC_REFERENCES, DOC_MAP, API_LIMITATIONS — everything downstream needs.
7. **Context engineering.** `max_content_tokens: 15000` on web_fetch, server-side `clear_tool_uses` + `compact`, automatic prompt caching (83-86% hit rate).
8. **Accurate cost tracking.** Uses `response.usage.iterations[]` to track executor vs advisor costs separately.
9. **Credentials flow to build sandbox.** Test files staged before build, credentials injected into sandbox env vars. The builder agent can run live API calls during Phase 3.
10. **Exit code based is_error.** Error detection uses exit codes (not string matching) to set `is_error: true` on tool results.

### Behavioral Instructions (Claude Code-Inspired)
- `<use_parallel_tool_calls>` — batch independent tool calls in one turn
- `<do_not_narrate>` — act, don't explain each step
- `<do_not_re_read>` — don't re-read unchanged files
- `<investigate_comprehensively>` — one comprehensive script, not five minimal ones
- `<think_before_acting>` — verify unknowns before writing code
- `<verify_against_docs>` — read code back and compare to api_spec before running
- `<reason_about_errors>` — reason about root cause, don't follow recipes
- `<be_resourceful>` — create local files when URLs fail

### What Was Removed (and Why)
- **Research sub-agent:** Merged into builder's Phase 1. Eliminates knowledge handoff loss.
- **live_test_logic.py / live_test.py:** Replaced by Phase 3 live API validation with credentials injected into build sandbox.
- **_inject_live_test_script / _run_live_validation:** Removed entirely. The agent validates with real API calls.
- **Manual context reset (PLAN detection):** Replaced by server-side context management. No fragile keyword matching.
- **Cosmetic verification gate:** Live validation IS verification. No separate verification scripts or auth method label matching.
- **Per-candidate budget cap:** Removed. Turn limit + wall-clock timeout are the guardrails.
- **SDK inspection step:** Removed. Caused 6-7 turn spirals. Agent discovers SDK methods organically through errors (1 turn to fix).
- **Mechanical evaluation:** Removed. All evaluation uses LLM judge comparing raw response vs ground truth. No exact_match/format_compliance mechanical scoring.
- **String-matching error detection:** Replaced by exit code based `is_error`. Exit codes are more reliable than scanning output for keywords.

---

---

## Lessons Learned (Hard-Won, Read Before Modifying Agent 5)

These are specific failure cases from the 2026-04-10 development session. Each cost real money and debugging time. Future developers: read these BEFORE making changes.

### 1. Knowledge Boundaries Kill Performance

**The evidence:** When research ran as a separate sub-agent, the builder received a text spec and generated code from TRAINING DATA instead of from the spec. Mindee's spec said `AUTH_HEADER: Authorization: {api_key}` (no prefix). The builder wrote `Authorization: Token {key}` — a common pattern from training data, not what the spec said.

**The fix:** Merge research into the builder. Same context that reads the docs also writes the code. Zero interpretation gap.

**The principle:** Every time you split an agent into two agents with a handoff, you create a knowledge boundary. The receiving agent re-interprets the handoff document through its training priors, which can override what the document actually says. This was observed with research→builder handoffs (auth format wrong) and with separate test execution (misclassified Mindee as 7/14 compatible when it was 0/14).

### 2. SDK Inspection Causes More Harm Than Good

**The evidence:** Across 8 runs, the 2 runs WITH SDK inspection scripts (check_sdk1.py through check_sdk7.py) FAILED. The 6 runs WITHOUT SDK inspection SUCCEEDED. The best Mindee run (12 turns, $1.04) had zero SDK scripts.

**Why:** Complex SDKs (Mindee's V2 has ClientV2 → InferenceParameters → InferenceResponse → InferenceResult → InferenceFields) lead to sequential discovery spirals. The agent writes a script, discovers one class, writes another script to explore it, discovers another class, etc. 6-7 turns consumed before any harness code is written.

**The fix:** Remove SDK inspection from Phase 1. Let the agent discover method names organically through errors. If `process_document_from_base64()` doesn't exist, the ImportError tells the agent in 1 turn, not 7.

### 3. Cosmetic Verification Breaks Working Code

**The case:** Nanonets harness passed smoke test and live API test (success=True, 4028ms latency). The verification gate found "Bearer token code exists but auth_method is basic_auth" — a cosmetic mismatch. Fed this back to the agent. The agent spent 5 turns removing the Bearer code, broke something during patching, and timed out.

**The fix:** Verification gate only checks: does harness.py exist? Does the live API call work? If both yes, ACCEPT. No cosmetic code review.

### 4. Non-Determinism Is Real — Same Input, Different Output

**The data:**
| Run | Config | Mindee | Klippa | Nanonets | Veryfi |
|-----|--------|--------|--------|----------|--------|
| Run 1 | Sonnet merged | PASS 12t | FAIL | PASS 17t | FAIL |
| Run 2 | Sonnet + fixes | FAIL 25t | PASS 12t | PASS 11t | PASS 26t |
| Run 3 | Sonnet + advisor | PASS 24t | PASS 9t | PASS 9t | FAIL 25t |
| Run 4 | Opus + advisor | FAIL 25t | PASS 5t | PASS 8t | PASS 15t |
| Run 5 | Sonnet→Opus | PASS 4t | PASS 11t | PASS 6t | PASS 6t |

Same input every time. Different results. One failed run does NOT mean the code is broken. Three consecutive successes is the real signal.

### 5. Dead Sample URLs Waste 5+ Turns

**The case:** Veryfi's sample receipt URL (`cdn.veryfi.com/receipts/...`) returned 403. The agent tried 5 URL variations (different CDN paths, GitHub links) — all dead. Eventually created a local test file, but it was 333 bytes (below Veryfi's 250-byte minimum).

**The fix:** `<be_resourceful>` instruction tells the agent to create local files instead of retrying URLs. The agent that followed this (Working_test_4) switched to `w3.org/WAI/WCAG21/Techniques/pdf/img/table-word.jpg` on the first failure — 1 turn instead of 5.

### 6. Cost Tracking: Use iterations[], Not Top-Level Usage

**The bug:** `response.usage.input_tokens` only reflects the FIRST executor iteration. With the advisor tool, there are multiple iterations:
```json
"iterations": [
  {"type": "message", "input_tokens": 988, "output_tokens": 79},
  {"type": "advisor_message", "model": "claude-opus-4-7", "input_tokens": 1915, "output_tokens": 93},
  {"type": "message", "input_tokens": 1092, "output_tokens": 29}
]
```
Advisor tokens are NOT in top-level totals and are billed at Opus rates. Without reading `iterations[]`, cost tracking underreports by 30-50%.

### 7. Context Explosion: web_fetch Content Stays in Context

**The case:** Veryfi's turns 1-7 had 119K input tokens each because web-fetched docs (encrypted content blocks in response.content) stayed in the conversation. Cost: $0.60/turn × 7 turns = $4.20 wasted.

**The fix:** `max_content_tokens: 15000` on web_fetch. Truncates at the server before content enters context. All real API docs fit in 15K tokens — the rest is HTML navigation, sidebars, JavaScript artifacts. Combined with server-side `clear_tool_uses` + `compact`, context stays under 60K.

### 8. PLAN Detection Was Fragile

**The case:** The context reset checked for literal `"PLAN:"` in the model's text. The model wrote `**PLAN**:` (markdown bold). The colon was after the bold markers. Context reset never fired. 118K tokens stayed in context for 7 turns.

**The fix:** Removed keyword-based context reset entirely. Server-side context management handles compression automatically. No fragile string matching.

### 9. The Agent Follows Behavioral Instructions ~70% of the Time

**The reality:** `<investigate_comprehensively>` says "write ONE script, not five." The agent follows this in most runs but occasionally writes 7 sequential scripts anyway. `<do_not_narrate>` says "don't explain each step." The agent sometimes narrates anyway.

**The mitigation:** Opus follows instructions more reliably than Sonnet. The advisor tool provides a "second opinion" that reinforces good practices. But 100% compliance is not achievable through prompting alone. The code handles the 30% failure case through turn limits, wall-clock timeouts, and the post-loop mechanical execution (which doesn't depend on the agent following instructions).

### 10. Platform Mismatch: Credential → Wrong API Endpoint

**The case:** Klippa migrated from `custom-ocr.klippa.com` (legacy, `X-Auth-Key` header) to `dochorizon.klippa.com` (new, `x-api-key` header). The API key in `provider_registry.json` was for DocHorizon. The builder chose the legacy platform (simpler endpoints) and got 401 errors for 17 turns.

**The fix:** Added `notes` field to provider_registry.json entries: "Key is for DocHorizon platform (dochorizon.klippa.com, auth header: x-api-key)." The initial message passes these notes to the agent. Builder reads the notes and targets the correct platform.

### 11. Credentials Must Flow to Build Sandbox (working_test_6)

**The case:** Early implementations resolved credentials but did not inject them into the sandbox environment. The builder agent could not run live API calls during Phase 3 because the env vars were not set in the subprocess.

**The fix:** Credentials are injected as environment variables into the sandbox before the build starts. Test files are also staged in the sandbox directory pre-build, not just post-loop. This enables Phase 3 live validation with real data.

### 12. `response.parsed_output` Not `.parsed` (working_test_6)

**The case:** Code used `response.parsed` to access structured output from `client.messages.parse()`. The correct attribute is `response.parsed_output`. This caused AttributeError on every structured output call.

**The fix:** Changed all references to `response.parsed_output`. A subtle API difference that caused hard-to-diagnose failures.

### 13. Live Validation IS Verification (working_test_6)

**The case:** Earlier designs had separate verification scripts that checked auth method labels, placeholder URLs, and ran programmatic checks. These caused false failures (Nanonets: passing harness rejected for "Bearer token code exists but auth_method is basic_auth").

**The fix:** Removed all separate verification logic. If the live API call succeeds, the harness works. Period. Don't write verification scripts — let the agent prove correctness by running the actual API call.

### 14. Don't Write Verification Scripts (working_test_6)

**The case:** The agent sometimes wrote elaborate verification scripts to check its own code instead of just running the live API call. These scripts consumed 3-5 turns and never caught real bugs.

**The fix:** The system prompt explicitly says: don't write separate verification scripts. Run the harness with real data. If it works, signal HARNESS_COMPLETE.

---

## Detailed Documentation (Original Design Notes Below)

> **NOTE:** The sections below document the ORIGINAL design and its evolution.
> Some details (three-agent system, live test injection, PLAN-based context reset)
> are no longer current. Refer to the "Current Architecture" section above for the
> current design. The original notes are preserved for historical context.

---

## Table of Contents

1. [What Agent 5 Does](#1-what-agent-5-does)
2. [Pipeline Position — Where Agent 5 Fits](#2-pipeline-position--where-agent-5-fits)
3. [Architecture Overview](#3-architecture-overview)
4. [The Parallel Execution Model](#4-the-parallel-execution-model)
5. [The Autonomous Builder Loop](#5-the-autonomous-builder-loop)
6. [Tool Dispatch Architecture](#6-tool-dispatch-architecture)
7. [The System Prompt — BUILDER_SYSTEM_PROMPT](#7-the-system-prompt--builder_system_prompt)
8. [The Harness Interface Contract](#8-the-harness-interface-contract)
9. [The Smoke Test Validation Flow](#9-the-smoke-test-validation-flow)
10. [The Verification Gate](#10-the-verification-gate)
11. [Venv Sandboxing](#11-venv-sandboxing)
12. [Provider Registry and Live Validation](#12-provider-registry-and-live-validation)
13. [The Seed Message — _build_initial_message()](#13-the-seed-message--_build_initial_message)
14. [Function-by-Function Reference](#14-function-by-function-reference)
15. [Security Model](#15-security-model)
16. [Schemas — Input, Output, and Everything Between](#16-schemas--input-output-and-everything-between)
17. [Configuration Knobs](#17-configuration-knobs)
18. [Cost Model](#18-cost-model)
19. [Error Handling Matrix](#19-error-handling-matrix)
20. [Comparison to Agent 4](#20-comparison-to-agent-4)
21. [Output Quality Validator](#21-output-quality-validator)
22. [CLI Wiring](#22-cli-wiring)
23. [Test Coverage Map](#23-test-coverage-map)
24. [Cloud Scaling Path](#24-cloud-scaling-path)
25. [Lessons Learned](#25-lessons-learned)

---

## 1. What Agent 5 Does

Agent 5 is the **build step** of the PuzzleEval pipeline. Its single job:

**Take each validated AI service candidate from Agent 4 and build a thin
Python API client that sends input to the service and returns the raw response.**

The harness is a thin API client with a `run(input_data) -> dict` function.
It does NOT parse, extract, or interpret results — that is the LLM judge's job.
Every harness — whether it wraps Google Document AI, AWS Textract, or a
small startup's REST API — returns the SAME dict shape with the raw API response.
This is what makes fair comparison possible in the LLM judge evaluation.

Input: A list of `ScreenedCandidate` objects (verified API docs, auth methods,
data formats) from Agent 4, plus context from Agent 1 (user needs), Agent 3
(test case types), and optionally provider credentials from the provider
registry.

Output: An `Agent5Result` containing:
- `harnesses` — successfully built test harnesses (one per candidate)
- `failed_harnesses` — candidates where building failed (with structured reasons)
- Cost, timing, and a summary

---

## 2. Pipeline Position — Where Agent 5 Fits

```
                              THE PUZZLEEVAL PIPELINE
  ============================================================================

  User Input
       |
       v
  +------------------+
  |  Agent 1          |     User Understanding
  |  (Parsing)        |     "I need invoice OCR for accounting"
  +------------------+          |
       |                        |
       +----------+-------------+
       |          |
       v          v
  +----------+  +----------+
  | Agent 2  |  | Agent 3  |     Agent 2: Research (web search for candidates)
  | Research |  | Synth    |     Agent 3: Synthetic test case generation
  | (web)    |  | Tests    |     These run IN PARALLEL
  +----------+  +----------+
       |             |
       v             |
  +----------+       |
  | Agent 4  |       |           Agent 4: Screen candidates (verify API docs)
  | Screening|       |
  +----------+       |
       |             |
       +------+------+
              |
              v
  +============================+
  ||   AGENT 5                ||     <-- YOU ARE HERE
  ||   Implement Test Env     ||
  ||                          ||     Builds N test harnesses in parallel.
  ||   N parallel builders    ||     Each builder is an autonomous tool-use
  ||   One per candidate      ||     loop with a verification gate that
  ||   Venv-isolated          ||     catches and fixes issues in-loop.
  +============================+
              |
              v
  +------------------+
  |  Agent 7          |     Analyze: Score each service's outputs
  |  (Judge)          |     Isolated per-candidate to avoid bias
  +------------------+
              |
              v
  +------------------+
  |  Agent 8          |     Ranking: Compare scores across services
  +------------------+
              |
              v
  +------------------+
  |  Agent 9          |     Report: Generate final verdict for user
  +------------------+
```

### Data Flow Into Agent 5

```
  +------------------+     validated_candidates     +====================+
  |    Agent 4       | --------------------------->  ||                  ||
  |    (Screening)   |     (ScreenedCandidate[])     ||    Agent 5      ||
  +------------------+                               ||                  ||
                                                     ||  Agent5Input     ||
  +------------------+     user_understanding        ||  {               ||
  |    Agent 1       | --------------------------->  ||    validated_    ||
  |    (Parsing)     |     (UserUnderstandingOutput) ||    candidates   ||
  +------------------+                               ||    user_under-  ||
                                                     ||    standing     ||
  +------------------+     test_cases                ||    test_cases   ||
  |    Agent 3       | --------------------------->  ||    trace_id     ||
  |    (Synth Tests) |     (Agent3Result)            ||    provider_    ||
  +------------------+                               ||    credentials  ||
                                                     ||  }              ||
  +------------------+     provider_credentials      ||                  ||
  | Provider Registry| --------------------------->  ||                  ||
  | (JSON / env)     |     (dict or None)            ||                  ||
  +------------------+                               +====================+
```

### Data Flow Out of Agent 5

```
  +====================+
  ||    Agent 5        ||
  ||                   || ---> Agent5Result
  ||                   ||        |
  +====================+         |
                                 |
        +------------------------+------------------------+
        |                                                 |
        v                                                 v
   harnesses: [TestHarness, ...]               failed_harnesses: [FailedHarness, ...]
        |                                                 |
        |  Each TestHarness contains:                     |  Each FailedHarness contains:
        |    - harness_code (Python source)               |    - failure_reason (human-readable)
        |    - harness_dir (filesystem path)              |    - failure_category (structured)
        |    - requirements (pip packages)                |    - partial_code (if any)
        |    - auth_env_vars (API key names)              |    - turns_attempted
        |    - supported_input_types                      |
        |    - supported_output_types                     |
        |    - smoke_test_passed (bool)                   |
        |    - live_validation_attempted (bool)           |
        |    - live_validation_passed (bool | None)       |
        |    - live_validation_notes (str | None)         |
        |    - validation_notes (str)                     |
        |                                                 |
        v                                                 v
   Post-loop test runner imports               Diagnostic info for
   and calls harness.run(input_data)           pipeline reporting
```

---

## 3. Architecture Overview

Agent 5 is architecturally the most complex agent in the pipeline so far. While
Agents 1-3 each make 1-2 API calls, and Agent 4 makes N parallel single-shot
calls, Agent 5 makes N parallel MULTI-TURN CHAINS with an in-loop verification
gate. Each chain is an autonomous agent that can use tools, iterate, fix its
own mistakes, and be programmatically checked before finalization.

```
  +=======================================================================+
  ||                    AGENT 5 ARCHITECTURE                             ||
  +=======================================================================+
  ||                                                                     ||
  ||  run_implement_test_env_agent(input_data)                           ||
  ||    |                                                                ||
  ||    |  1. Create Anthropic client                                    ||
  ||    |  2. Select top AGENT5_MAX_CANDIDATES by user-fit score         ||
  ||    |  3. Create sandbox directories (runs/{trace_id}/harnesses/)    ||
  ||    |                                                                ||
  ||    +-- ThreadPoolExecutor(max_workers=5) --------------------------+||
  ||    |   |                                                           |||
  ||    |   |  Thread 1          Thread 2          Thread N             |||
  ||    |   |  +-----------+    +-----------+    +-----------+         |||
  ||    |   |  | _create_  |    | _create_  |    | _create_  |         |||
  ||    |   |  | venv()    |    | venv()    |    | venv()    |         |||
  ||    |   |  | _resolve_ |    | _resolve_ |    | _resolve_ |         |||
  ||    |   |  | creds()   |    | creds()   |    | creds()   |         |||
  ||    |   |  | _inject_  |    | _inject_  |    | _inject_  |         |||
  ||    |   |  | live_test |    | live_test |    | live_test |         |||
  ||    |   |  |           |    |           |    |           |         |||
  ||    |   |  | Builder   |    | Builder   |    | Builder   |         |||
  ||    |   |  | Loop +    |    | Loop +    |    | Loop +    |         |||
  ||    |   |  | Verify    |    | Verify    |    | Verify    |         |||
  ||    |   |  | Gate      |    | Gate      |    | Gate      |         |||
  ||    |   |  |           |    |           |    |           |         |||
  ||    |   |  | Own msgs  |    | Own msgs  |    | Own msgs  |         |||
  ||    |   |  | Own venv  |    | Own venv  |    | Own venv  |         |||
  ||    |   |  | Own sandbox|   | Own sandbox|   | Own sandbox|        |||
  ||    |   |  | Own budget |    | Own budget |    | Own budget |       |||
  ||    |   |  +-----------+    +-----------+    +-----------+         |||
  ||    |   |  | TestHarness|   | FailedH.  |   | TestHarness|         |||
  ||    |   |  | or Failed  |    | or Test   |    | or Failed  |        |||
  ||    |   +--+------------+----+-----------+----+-----------+--------+||
  ||    |                                                                ||
  ||    |  3. Collect results (as_completed)                             ||
  ||    |  4. Sort into harnesses[] and failed_harnesses[]               ||
  ||    |  5. Build summary string                                       ||
  ||    |  6. Return Agent5Result (NO structuring LLM call)              ||
  ||    |                                                                ||
  +=======================================================================+
```

### Key Architectural Choice: No Structuring LLM Call

Agents 2 and 4 both use a final structuring LLM call to convert raw text
findings into structured JSON. Agent 5 does NOT need this. The builder loop
produces artifacts (files on disk) and metadata (turns, cost) programmatically.
The orchestrator assembles `Agent5Result` directly from these artifacts. This
saves one API call per run (~$0.10 and 5 seconds).

---

## 4. The Parallel Execution Model

```
  TIME ----->

  Thread 1: |--build_single_harness(Google Document AI)---------------|
            |  [venv] [creds] [live_test inject]                     |
            |  [fetch docs] [write harness] [test] [fix] [verify] OK |

  Thread 2: |--build_single_harness(AWS Textract)-----------|
            |  [venv] [fetch docs] [write harness] [test] OK |

  Thread 3: |--build_single_harness(Mindee)--------------------------------------|
            |  [venv] [creds] [live_test inject]                                 |
            |  [fetch docs] [search SDK] [write] [test] [fix] [verify] [fix] OK |

  Thread 4: |--build_single_harness(Veryfi)-----|
            |  [venv] [fetch docs] [FAILED: 403] |

  Thread 5: |--build_single_harness(Rossum)---------------------|
            |  [venv] [fetch docs] [write] [test] [verify] OK  |

                                                                          DONE
  Orchestrator: collect results as_completed -----------> Agent5Result
  Total wall-clock: max(Thread 1..5) = Thread 3 duration
```

### Why ThreadPoolExecutor (Not asyncio)

The Anthropic sync client is thread-safe for independent API calls. Each thread
makes its own HTTP requests with its own context. `ThreadPoolExecutor` is the
simplest correct solution. Asyncio would add complexity (event loop, async
client, awaits) without meaningful benefit — the bottleneck is I/O (API calls),
and threads handle I/O-bound parallelism just fine.

### Why Per-Candidate Isolation

This is the SAME lesson from Agent 4, amplified. In Agent 4, each candidate's
web fetch content (~10-140K tokens) was isolated to prevent cross-candidate
token accumulation. In Agent 5, each candidate's multi-turn conversation is
isolated. A candidate with complex API docs and 8 fix cycles doesn't inflate
the context for a candidate with simple docs and 3 turns.

Without isolation, if all candidates shared one conversation:
- Turn 1: Candidate A docs (40K tokens)
- Turn 2: Candidate A code (2K tokens) -> context is 42K
- Turn 3: Candidate B docs (80K tokens) -> context is 122K
- Turn 4: Candidate B code (2K tokens) -> context is 124K
- ...and so on, exploding to 300K+ tokens

With isolation, each candidate's peak context is typically 30-80K tokens.

### Candidate Selection

Before building, `run_implement_test_env_agent()` sorts all validated candidates
by `relevance_score` (the user-fit composite score from Agent 2) in descending
order and takes the top `AGENT5_MAX_CANDIDATES` (default 4). This avoids
building harnesses for low-scoring candidates that would not make the final
comparison.

### Worker Count

```
AGENT5_MAX_PARALLEL = 5 (default, configurable)
AGENT5_MAX_CANDIDATES = 4 (default, configurable)
actual_workers = min(AGENT5_MAX_PARALLEL, len(selected_candidates))
```

Default 5 workers means all selected candidates build simultaneously for
typical pipeline runs. Reduce via `PUZZLEEVAL_AGENT5_MAX_PARALLEL` if hitting
API rate limits on a lower tier.

---

## 5. The Autonomous Builder Loop

This is the heart of Agent 5. For ONE candidate, `_build_single_harness()`
runs a multi-turn conversation loop where Claude autonomously reads API docs,
writes code, tests it, and fixes errors — with a verification gate that
catches issues before finalization.

```
  _build_single_harness(client, candidate, input_data, sandbox_dir, logger)
  ===========================================================================

  Pre-loop setup:
    _create_venv(sandbox_dir)                    # Isolated Python env
    credentials = _resolve_credentials(...)       # From registry or env
    if credentials:
        _inject_live_test_script(sandbox_dir)    # Write live_test.py

  Initialize:
    messages = [user: _build_initial_message(candidate, input_data)]
    accumulated_cost = 0.0
    turn = 0
    verification_attempts = 0
    saved_doc_files = []          # Filenames of fetched docs saved to sandbox
    research_complete = False     # True after PLAN detected → context reset
    conversation_log = []         # Human-readable log of every turn

                 +----------------------------------------------+
                 |                                              |
                 v                                              |
         +---------------+                                     |
         | Budget check  |                                     |
         | turn < MAX?   |                                     |
         +------+--------+                                     |
                |                                              |
                | yes                                          |
                v                                              |
  +----------------------------+                               |
  | client.messages.create(    |                               |
  |   model=AGENT5_BUILDER_MODEL,                              |
  |   system=BUILDER_SYSTEM_PROMPT,                            |
  |   messages=messages,       |                               |
  |   tools=ALL_TOOLS,         |                               |
  | )                          |                               |
  +----------------------------+                               |
                |                                              |
                v                                              |
  +----------------------------+                               |
  | Log metrics, track cost    |                               |
  +----------------------------+                               |
                |                                              |
     +----------+-----------+-----------+                      |
     |          |           |           |                      |
     v          v           v           v                      |
  pause_turn  end_turn   end_turn    tool_use                  |
  (server     w/COMPLETE w/FAILED    (Claude                   |
  tools       -> VERIFY  -> return   wants to                  |
  took too    GATE       FailedH.    use a tool)               |
  long)                                 |                      |
     |          |           |           v                      |
     |          |           |   +-------------------+          |
     |          v           |   | For each tool_use |          |
     |    +-----------+     |   | block in response:|          |
     |    | VERIFY    |     |   |                   |          |
     |    | GATE      |     |   | Server tool?      |          |
     |    |           |     |   |   -> Handled by   |          |
     |    | _run_     |     |   |      Anthropic API|          |
     |    | verifica- |     |   |                   |          |
     |    | tion_     |     |   | Custom tool?      |          |
     |    | checks()  |     |   |   -> _dispatch_   |          |
     |    |           |     |   |      tool()       |          |
     |    | Issues?   |     |   +-------------------+          |
     |    | +retries? |     |           |                      |
     |    +-----+-----+    |           v                      |
     |    |yes  |no        |   +-------------------+          |
     |    |     |          |   | Append to msgs:   |          |
     |    v     v          |   |  assistant: resp   |         |
     |  Feed   Break       |   |  user: tool_results|         |
     |  issues loop        |   +-------------------+          |
     |  back   (verified   |           |                      |
     |  to     or retries  |           +----------------------+
     |  Claude exhausted)  |
     |    |                |
     v    +----------------+
  Reset msgs
  continue
  loop --------+
               |
               v
        Return TestHarness (with verification status)
```

### The Five Stop Reasons

| stop_reason | What it means | What Agent 5 does |
|---|---|---|
| `end_turn` + "HARNESS_COMPLETE" | Claude finished building | Run verification gate. If clean or retries exhausted, break. If issues + retries remain, feed back and continue. |
| `end_turn` + "HARNESS_FAILED" | Claude gave up after trying | Return `FailedHarness` with categorized reason |
| `end_turn` + no signal | Claude stopped mid-work (rare) | If harness.py exists on disk, treat as complete; otherwise continue |
| `tool_use` | Claude wants to use a tool | Dispatch custom tools locally, append results, continue loop |
| `pause_turn` | Server tool execution took too long | Reset messages to initial + last response, continue |

### Dead-End Detection

The builder loop tracks consecutive error turns via a `consecutive_errors`
counter. When tool results contain error signals (error, traceback, 401, 404,
500, etc.) for `MAX_CONSECUTIVE_ERRORS` (3) consecutive turns, the loop injects
a STRATEGIC REASSESSMENT message:

```
You have encountered errors for 3 consecutive turns. STOP and make a decision:

Option A: You understand the root cause and have a DIFFERENT approach to try.
Option B: This problem requires something you cannot do. Signal HARNESS_FAILED.

Do NOT repeat the same approach. Either pivot or fail gracefully.
```

The counter resets after the reassessment, giving Claude a fresh chance. This
prevents 7-turn debugging spirals on unsolvable problems (deprecated endpoints,
requires manual dashboard configuration, paid account setup).

### OS Detection

The system prompt dynamically includes `OS: Windows` or `OS: Linux` based on
`sys.platform`. This tells the builder agent to use cross-platform commands
(e.g., `python -c "import os; print(os.listdir('.'))"` instead of `ls`) and
`os.path` in Python code instead of hardcoded path separators.

### Typical Build Sequence

For a well-documented API service with credentials available, a typical build
takes 6-10 turns:

```
  Turn 1: Claude calls web_fetch on the API docs URL
          -> Reads the documentation, understands endpoints and auth
          -> States plan: "PLAN: I will call [endpoint] with [auth]..."
          -> [CONTEXT COMPRESSION] _extract_and_save_web_content() saves
             fetched docs to fetched_docs_0.txt
          -> [CONTEXT RESET] "PLAN:" detected → messages reset to:
             initial seed + plan text + doc file references.
             Accumulated web_fetch content (~30-50K tokens) DROPPED.

  Turn 2: Claude calls write_file("harness.py", ...)
          -> Writes the initial harness code based on plan
          Claude calls write_file("requirements.txt", ...)
          -> Lists the pip dependencies

  Turn 3: Claude calls run_code("pip install -r requirements.txt")
          -> Installs dependencies in the candidate's venv

  Turn 4: Claude calls write_file("smoke_test.py", ...)
          -> Writes the structural validation test

  Turn 5: Claude calls run_code("python smoke_test.py")
          -> Runs the smoke test, sees output

  Turn 6: (if test failed) Claude calls read_file("fetched_docs_0.txt")
          -> Checks saved API docs for correct field names
          Claude calls write_file("harness.py", ...)
          -> Fixes the code based on docs + error message

  Turn 7: Claude calls run_code("python smoke_test.py")
          -> SMOKE TEST PASSED
          -> Milestone message injected: "Smoke test passed. Proceed to live validation."

  Turn 8: Claude runs live API call with test file (credentials in sandbox env)
          -> Checks results. If failure, fixes and re-runs.

  Turn 9: Claude responds with "HARNESS_COMPLETE" summary
          -> Live validation passed = harness accepted
          -> If live validation failed: feedback sent to Claude, loop continues

  Post-loop: conversation_log.json saved to sandbox directory
```

For complex or poorly documented APIs, it may take 10-15 turns with additional
web searches, SDK explorations, verification retries, and multiple fix cycles.

---

## 6. Tool Dispatch Architecture

Agent 5 uses TWO categories of tools: server tools (executed by Anthropic's API)
and custom tools (executed locally by our code).

```
  +==================================================================+
  ||                    TOOL ARCHITECTURE                            ||
  +==================================================================+
  ||                                                                ||
  ||  Claude's Response                                             ||
  ||  +----------------------------------------------------------+  ||
  ||  | content: [                                                |  ||
  ||  |   {type: "text", text: "I'll fetch the docs..."},        |  ||
  ||  |   {type: "tool_use", name: "web_fetch", ...},  <-- SERVER|  ||
  ||  |   {type: "tool_use", name: "write_file", ...}, <-- CUSTOM|  ||
  ||  |   {type: "tool_use", name: "run_code", ...},   <-- CUSTOM|  ||
  ||  | ]                                                         |  ||
  ||  +----------------------------------------------------------+  ||
  ||                          |                                     ||
  ||           +--------------+---------------+                     ||
  ||           |                              |                     ||
  ||           v                              v                     ||
  ||  +------------------+          +--------------------+          ||
  ||  | SERVER TOOLS     |          | CUSTOM TOOLS       |          ||
  ||  |                  |          |                    |          ||
  ||  | web_fetch        |          | write_file         |          ||
  ||  | web_search       |          | run_code           |          ||
  ||  |                  |          | read_file           |          ||
  ||  | Executed by      |          |                    |          ||
  ||  | Anthropic API    |          | Executed locally   |          ||
  ||  | server-side.     |          | by _dispatch_tool()|          ||
  ||  | Results injected |          | in the sandbox dir |          ||
  ||  | into context     |          | with venv isolation|          ||
  ||  | automatically.   |          |                    |          ||
  ||  |                  |          | Results sent back  |          ||
  ||  | We never see the |          | as tool_result     |          ||
  ||  | raw results.     |          | content blocks.    |          ||
  ||  +------------------+          +--------------------+          ||
  ||                                          |                     ||
  ||                                          v                     ||
  ||                                +--------------------+          ||
  ||                                | _dispatch_tool()   |          ||
  ||                                |   |                |          ||
  ||                                |   +-> write_file   |          ||
  ||                                |   |   - Strips path|          ||
  ||                                |   |   - Checks ext |          ||
  ||                                |   |   - Writes to  |          ||
  ||                                |   |     sandbox    |          ||
  ||                                |   |                |          ||
  ||                                |   +-> run_code     |          ||
  ||                                |   |   - 120s timeout|         ||
  ||                                |   |   - 5000 char  |          ||
  ||                                |   |     truncation |          ||
  ||                                |   |   - Runs in    |          ||
  ||                                |   |     venv env   |          ||
  ||                                |   |                |          ||
  ||                                |   +-> read_file    |          ||
  ||                                |       - Sandbox    |          ||
  ||                                |         only       |          ||
  ||                                |       - 10000 char |          ||
  ||                                |         truncation |          ||
  ||                                +--------------------+          ||
  +==================================================================+
```

### Tool Configuration

| Tool | Type | Version | max_uses | Purpose |
|---|---|---|---|---|
| web_fetch | Server | 20250910 | 5 | Read API docs, quickstart guides, SDK refs |
| web_search | Server | 20250305 | 4 | Search for docs, examples, SDK installation |
| advisor | Server | advisor-tool-2026-03-01 | unlimited | Consult Opus 4.7 for strategic guidance |
| write_file | Custom | n/a | unlimited | Write harness.py, requirements.txt, smoke_test.py |
| patch_file | Custom | n/a | unlimited | String-replace editing on existing files (efficient bug fixing) |
| run_code | Custom | n/a | unlimited | Run smoke tests, pip installs, import tests (venv-isolated, 120s timeout) |
| read_file | Custom | n/a | unlimited | Review code, check outputs, read saved API docs |
| ask_research | Custom (sub-agent) | n/a | unlimited | Targeted web research for Phase 2+ debugging |

### Why web_fetch Gets 3 Uses (Reduced from 4)

Context compression means fetched docs are saved to `fetched_docs_*.txt` files
in the sandbox. Claude can `read_file()` them instead of re-fetching. A typical
sequence:

1. Fetch the main API docs URL (from Agent 4) — saved to `fetched_docs_0.txt`
2. Fetch a quickstart or getting started page — saved to `fetched_docs_1.txt`
3. Fetch a specific endpoint reference if needed — saved to `fetched_docs_2.txt`

During the build and verify phases, Claude reads these saved files instead of
re-fetching, which eliminates the need for a 4th web_fetch use.

### Why web_search Gets 2 Uses (Reduced from 3)

With docs saved locally, Claude can find most answers via `read_file()` on
the saved docs. Web search is reserved for:

1. Fallback when the API docs URL from Agent 4 is a landing page
2. Searching for a specific error message during build/verify phase

---

## 7. The System Prompt — BUILDER_SYSTEM_PROMPT

The system prompt is the longest prompt in the pipeline so far. It defines the
autonomous builder agent's behavior using a 4-phase structure. Every section
exists for a specific reason.

### The 4-Phase Structure

The prompt is organized into four sequential phases with explicit headers:

**PHASE 1: RESEARCH** — Understand the API before writing any code

1. Use server-side web_search and web_fetch to find and read API docs
2. Read carefully: note the exact base URL, endpoint path, auth method and header format, request format, response JSON structure
3. If the docs page is a landing page, SEARCH for quickstart/examples
4. State your plan in text BEFORE writing code: "PLAN: I will call [endpoint URL] with [auth method]..."

**Why the explicit plan requirement?** Without it, Claude sometimes writes code
from training data memory instead of the actual docs it just fetched. The plan
forces Claude to commit to specific endpoint URLs and auth formats before
coding, making it easy to verify against the docs.

**PHASE 2: BUILD** — Write code based on research

1. WRITE harness.py implementing run() based on Phase 1 plan
2. WRITE requirements.txt with pip dependencies
3. RUN `pip install -r requirements.txt` (installs into the candidate's venv)
4. WRITE smoke_test.py using the structural validation template
5. RUN `python smoke_test.py`
6. If it fails, read the error, fix the code, re-run, repeat

**PHASE 3: VALIDATE** — Live API validation required

1. **LIVE VALIDATION**: Credentials are injected into the build sandbox as environment variables. Test files are staged in the sandbox before the build starts. The agent runs real API calls to validate the harness works end-to-end.
2. If the live call fails (401 = wrong auth, 404 = wrong endpoint, 400 = wrong request format), fix the harness and re-run. `ask_research` is available for debugging.

**Why credentials in the sandbox?** Credentials flow to the build sandbox so the agent can validate with real API calls during building, not just post-loop. Test files are also staged before the build so the agent can use them for live validation. Live validation IS verification — no separate verification scripts needed.

**PHASE 4: COMPLETION CHECKLIST** — Before signaling HARNESS_COMPLETE

Eight items Claude must mentally verify:
1. Endpoint URL in code matches the API docs fetched (not from training data)
2. Auth header format matches docs exactly
3. Request body structure matches docs
4. Response parsing handles the actual JSON structure from docs
5. Error handling catches all exceptions, returns success=False, never raises
6. smoke_test.py passes
7. live_test.py passes (if it exists) OR live_test.py does not exist
8. requirements.txt lists ALL dependencies

If ANY item fails, Claude must go back and fix it. Do NOT signal complete with
known issues.

### Other Prompt Sections

**"## Your Tools"** — Lists all 5 tools with their purposes. Claude needs
to know what tools it has to plan its approach.

**"## Environment"** — Tells Claude it is running in an ISOLATED Python
virtual environment. `python` and `pip` point to this venv. After writing
requirements.txt, ALWAYS run `pip install -r requirements.txt`. Each candidate
has its own venv.

**"## The Harness Interface (EXACT specification)"** — The exact `run(input_data: dict) -> dict` contract. The most critical section.

**"## Rules"** — Seven non-negotiable constraints (API key from env var, never
hardcode keys, handle ALL errors, use requests/httpx/official SDK, measure
latency, keep it simple, include docstring).

**"## Smoke Test Template"** — A complete Python smoke test that Claude should
adapt. Ensures every harness is validated against the SAME criteria.

**"## WHEN YOU HIT A BUG — Reference Chain"** — Teaches Claude to check fixes
in cheapest-first order: (1) check PLAN notes (already in context), (2)
`read_file("fetched_docs_0.txt")` (local file read), (3) `web_search` for the
specific error (snippet tokens only), (4) `web_fetch` a specific URL (last
resort). This prevents Claude from immediately re-fetching pages when the
answer is already saved locally.

**"## SIGNALS"** — Two magic strings: `HARNESS_COMPLETE` and `HARNESS_FAILED`.
Detected by the builder loop to determine when to stop or when the
verification gate should run.

---

## 8. The Harness Interface Contract

Every harness exposes the same interface. This is the contract between
Agent 5's builder loop and its post-loop test runner.

### Input: `run(input_data: dict) -> dict`

```
  input_data = {
      "text": str,              # The input text/prompt to send to the API

      "input_type": str,        # One of: "text", "structured_data",
                                # "document_content", "conversation",
                                # "image_description"

      "input_context": dict|None,  # Optional metadata for the test case

      "test_file_path": str|None,  # Path to a test file (for file-based tests)
  }
```

### Output: the return dict

```
  return {
      "output": str,            # The service's response text
      "latency_ms": float,      # Round-trip time in milliseconds
      "tokens_used": dict|None, # {"input": int, "output": int}
      "cost_usd": float|None,   # Estimated cost per call
      "raw_response": dict,     # The full API response (JSON-serializable)
      "success": bool,          # True if the API call completed successfully
      "error": str|None,        # Error message if success=False
  }
```

### Why This Specific Shape?

Each field serves a downstream consumer:

| Field | Used by | Purpose |
|---|---|---|
| `output` | Agent 7 (Judge) | Compared against expected output for scoring |
| `latency_ms` | Agent 8 (Ranking) | Speed dimension of the evaluation |
| `tokens_used` | Agent 8 (Ranking) | Efficiency metric |
| `cost_usd` | Agent 8 (Ranking) | Price dimension of the evaluation |
| `raw_response` | Agent 9 (Report) | Evidence trail for the user |
| `success` | Test Runner (post-loop) | Determines if the test case ran or errored |
| `error` | Test Runner (post-loop) | Diagnostics for failed test cases |

---

## 9. The Smoke Test Validation Flow

The smoke test is the first quality gate. It validates STRUCTURE, not
correctness. It does NOT make real API calls.

```
  +---------------------------------------------------------+
  |                 SMOKE TEST FLOW                          |
  +---------------------------------------------------------+
  |                                                         |
  |  Claude writes smoke_test.py to sandbox                 |
  |  Claude calls: run_code("python smoke_test.py")         |
  |  (runs in the candidate's venv)                         |
  |                                                         |
  |  smoke_test.py:                                         |
  |    1. import harness         -- Can it import?           |
  |    2. assert hasattr(harness, "run")  -- Has run()?     |
  |    3. assert callable(harness.run)    -- Is callable?   |
  |    4. Check run() signature  -- Takes input_data?       |
  |    5. Mock ALL HTTP          -- No real API calls       |
  |       (requests.Session.send, requests.post,            |
  |        requests.get all return ConnectionError)         |
  |    6. Call harness.run({...}) -- Run with mock           |
  |    7. Check return is dict   -- Correct type?           |
  |    8. Check required keys    -- All 7 keys present?     |
  |    9. Check key types        -- bool, float, str, dict? |
  |   10. print("SMOKE TEST PASSED")                        |
  |                                                         |
  |  OUTCOMES:                                              |
  |                                                         |
  |  PASSED:                                                |
  |    stdout: "SMOKE TEST PASSED"                          |
  |    -> Claude proceeds to Phase 3 (Verify)               |
  |                                                         |
  |  FAILED:                                                |
  |    stderr: error details                                |
  |    -> Claude sees the error, fixes harness.py           |
  |    -> Writes updated file, runs smoke test again        |
  |    -> Repeats until passed or turns/budget exhausted    |
  +---------------------------------------------------------+
```

### Why Structural Validation Only?

The smoke test mocks all HTTP so no real API calls are made. This means:

1. No API key needed during building (keys are only needed for live validation)
2. No risk of burning API credits during the build phase
3. No dependency on the external service being available
4. Tests run instantly (no network latency)
5. Deterministic — same result every time

What we ARE validating: imports, function signature, return dict shape, key types.

What we are NOT validating: endpoint URLs, auth headers, response parsing,
real API behavior. The verification gate and live test handle these.

---

## 10. The Verification Gate

The verification gate is the core hardening that distinguishes the current
Agent 5 from the initial implementation. It runs INSIDE the builder loop,
not after it.

### How It Works

When Claude includes "HARNESS_COMPLETE" in its response:

```
  1. _run_verification_checks(sandbox_dir, candidate, credentials, logger, trace_id)
     |
     +-- Check 1: Code-level sanity
     |   - Scan harness.py for placeholder URLs (example.com, localhost, httpbin.org, etc.)
     |   - Check auth method consistency (bearer_token in schema but Basic in code?)
     |   - Verify def run() exists
     |
     +-- Check 2: Live API validation (if credentials available)
         - Run _run_live_validation() which executes live_test.py
         - Parse the LIVE_RESULT JSON output
         - success=True or soft error (400 = endpoint+auth work) -> PASS
         - Hard failure (401, 404, connection refused) -> FAIL

  2. If issues found AND verification_attempts < MAX_VERIFICATION_RETRIES (2):
     -> Format issues as a feedback message
     -> Append to conversation: assistant response + user feedback
     -> verification_attempts += 1
     -> Continue loop (Claude fixes and re-signals)

  3. If no issues OR retries exhausted:
     -> Break loop
     -> verification_passed = True if clean, False if retries exhausted
```

### Why Inside the Loop?

The previous design ran verification after the loop completed. Problem: by
that point, Claude's conversation context was gone. If verification found
that the endpoint URL was wrong, there was no way to ask Claude to fix it.

With in-loop verification, Claude still has the API docs in context. The
feedback message tells Claude exactly what is wrong. Claude fixes the code,
re-runs tests, and re-signals HARNESS_COMPLETE. The verification gate runs
again. This self-healing cycle catches and fixes most issues automatically.

### What Gets Checked

| Check | What it catches | Severity |
|---|---|---|
| Placeholder URLs (example.com, localhost, etc.) | Hallucinated endpoints | High — code will never work |
| Auth method mismatch (bearer vs basic vs api_key) | Wrong auth header format | High — 401 on every call |
| Missing `def run(` | Fundamentally broken harness | Critical |
| Live test failure (401) | Wrong auth header or key format | High |
| Live test failure (404) | Wrong endpoint URL | High |
| Live test failure (400) | PASS — endpoint exists, auth works | N/A |
| Live test failure (connection error) | Completely wrong URL | High |

---

## 11. Venv Sandboxing

Each candidate gets its own Python virtual environment for dependency isolation.

### _create_venv()

Called at the start of `_build_single_harness()`, before the builder loop.

```python
def _create_venv(sandbox_dir, logger, trace_id, candidate_name) -> bool:
    # Creates sandbox_dir/.venv/ using sys.executable -m venv
    # Returns True on success, False on failure
    # 120-second timeout for slow venv creation
```

### _build_sandbox_env()

Called by `_tool_run_code()` every time a shell command is executed.

```python
def _build_sandbox_env(sandbox_dir, extra_env=None) -> dict:
    # If sandbox_dir/.venv exists:
    #   Prepend .venv/bin (Unix) or .venv/Scripts (Windows) to PATH
    #   Set VIRTUAL_ENV env var
    # Always sets PYTHONDONTWRITEBYTECODE=1
    # Merges extra_env (used for credential injection)
```

This means `python` and `pip` in `run_code` commands resolve to the venv's
copies, not the system Python. Each candidate's `pip install` affects only
its own venv.

### Container Swap Point

This is the designed seam for production. Replace `_create_venv()` with a
container creation function (Docker, E2B, Modal). The rest of the code does
not change because `_build_sandbox_env()` is the ONLY place that knows about
the venv path. Change that one function to configure container exec environment
setup, and the entire codebase works with containers instead of venvs.

---

## 12. Provider Registry and Live Validation

### Provider Registry (`puzzleeval/provider_registry.py`)

A centralized JSON registry for API keys. Replaces the need for users to set
environment variables for every provider manually.

**Schema:**
```json
{
  "providers": {
    "mindee": {
      "env_vars": {"MINDEE_API_KEY": "sk-..."},
      "tier": "free",
      "monthly_limit": 250,
      "usage_this_month": 0
    },
    "aws_textract": {
      "env_vars": {
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "AWS_DEFAULT_REGION": "us-east-1"
      },
      "tier": "free_tier",
      "monthly_limit": 1000,
      "usage_this_month": 0
    }
  }
}
```

**Key classes:**
- `ProviderEntry` — one provider's credentials and metadata
- `ProviderRegistry` — container for all provider credentials
- `load_registry(path)` — loads JSON file, returns empty registry if missing
- `get_credentials(registry, provider_name, candidate_name, auth_env_vars)` — 4-tier lookup
- `get_all_credentials(registry, candidates)` — bulk lookup for all candidates

**Matching strategy (4-tier fallback):**
1. Exact match on normalized candidate name
2. Exact match on normalized provider name
3. Partial/substring match
4. Fallback to `os.environ` for the expected env var names

**Design for future swap:** The interface is designed so it can be replaced
with AWS Secrets Manager, HashiCorp Vault, or GCP Secret Manager without
changing callers. The `get_credentials()` function signature stays the same.

### Credential Resolution in Agent 5

`_resolve_credentials()` in `implement_test_env.py`:

1. Check `input_data.provider_credentials` (populated by CLI from registry)
2. Check `os.environ` for expected env var names (e.g., `{PROVIDER}_API_KEY`)
3. Returns `dict[str, str]` mapping env var names to values, or `None`

### Live Test Injection

When credentials are available, `_inject_live_test_script()` writes
`live_test.py` to the sandbox BEFORE the builder loop starts:

```python
LIVE_TEST_TEMPLATE = '''
import json, os, sys
{env_setup}  # os.environ['PROVIDER_API_KEY'] = 'sk-...'
sys.path.insert(0, ".")
import harness
result = harness.run({"text": "test input", "input_type": "text", ...})
print("LIVE_RESULT:" + json.dumps(result, default=str))
'''
```

The builder agent's system prompt tells it to check for `live_test.py` and
run it during Phase 3 (Verify). The verification gate also runs it via
`_run_live_validation()`.

### Live Validation Result Parsing

`_run_live_validation()` parses the `LIVE_RESULT:` JSON output:

| Outcome | Meaning | Result |
|---|---|---|
| `success=True` | API call worked | `(True, "Live API call succeeded")` |
| `success=False` + soft error (invalid input, bad request) | Endpoint exists, auth works | `(True, "API responded with input validation error")` |
| `success=False` + hard error (401, 404, connection refused) | Real problem | `(False, "Live API call failed: ...")` |
| No `LIVE_RESULT:` in output | Script crashed or timed out | `(False, "No parseable result")` |
| No credentials | No live test possible | `(None, "No credentials available")` |

---

## 13. The Seed Message — _build_initial_message()

The first user message in the builder conversation gives Claude everything
it needs to start building. It aggregates information from three upstream
agents.

### What Gets Injected

From **Agent 4** (ScreenedCandidate):
- Service name, provider, description
- Verified API docs URL
- Auth method (api_key, oauth2, etc.)
- Access method (free_tier, trial, etc.)
- Data format notes
- Confirmed capabilities
- Rate limit info
- Screening notes
- Pricing model and details

From **Agent 1** (UserUnderstandingOutput):
- Sub-task descriptions and capabilities (what the user needs)

From **Agent 3** (Agent3Result):
- Input types the harness must handle (text, structured_data, etc.)
- Output types expected (free_text, extraction, etc.)

**Provider slug** for env var convention:
- "Google" becomes `GOOGLE_API_KEY`
- "Amazon Web Services" becomes `AMAZON_WEB_SERVICES_API_KEY`

### Why Include Agent 1/3 Context?

The builder agent needs to know:
- What input types to support (text only? or also document_content?)
- What output types to produce (free_text? structured_json? extraction?)
- What the user actually needs (so it can focus the harness on relevant capabilities)

Without this context, Claude might build a harness for the wrong endpoint
(e.g., building an image captioning harness when the user needs document OCR).

---

## 14. Function-by-Function Reference

### `run_implement_test_env_agent(input_data: Agent5Input) -> Agent5Result`

**The main entry point.** Called by `cli.py` or (future) FastAPI endpoint.

- Creates the Anthropic client
- Creates sandbox directories under `runs/{trace_id}/harnesses/`
- Launches parallel builds via `ThreadPoolExecutor`
- Collects results as they complete
- Assembles `Agent5Result` programmatically (no LLM call)
- Returns the result

### `_build_single_harness(client, candidate, input_data, sandbox_dir, logger) -> TestHarness | FailedHarness`

**The core builder loop for ONE candidate.** This is the most complex function
in the codebase. It:

1. Creates an isolated venv via `_create_venv()`
2. Resolves credentials via `_resolve_credentials()`
3. Injects `live_test.py` via `_inject_live_test_script()` if credentials available
4. Runs a multi-turn conversation loop with tool use
5. When Claude signals HARNESS_COMPLETE, runs the verification gate
6. Feeds issues back to Claude for fixing (up to `AGENT5_MAX_VERIFICATION_RETRIES` times)
7. Returns `TestHarness` on success, `FailedHarness` on failure

Never raises — all errors become `FailedHarness`.

### `_create_venv(sandbox_dir, logger, trace_id, candidate_name) -> bool`

**Creates a Python venv in the sandbox.** At `sandbox_dir/.venv`. 120-second
timeout. Returns True on success, False on failure. The venv is the sandboxing
seam — swap this with container creation for production.

### `_build_sandbox_env(sandbox_dir, extra_env=None) -> dict`

**Builds environment variables for subprocess execution.** Prepends venv's
`bin/` or `Scripts/` to PATH so `python` and `pip` resolve to the venv. Merges
extra_env (used for credential injection during live tests).

### `_resolve_credentials(input_data, candidate, provider_slug) -> dict | None`

**Resolves credentials from registry or env vars.** Checks
`input_data.provider_credentials` first (populated by CLI from registry),
then falls back to `os.environ` for expected env var names.

### `_inject_live_test_script(sandbox_dir, credentials) -> bool`

**Writes `live_test.py` to the sandbox.** Called BEFORE the builder loop when
credentials are available. Bakes credentials into the script so Claude can
run it during Phase 3.

### `_run_verification_checks(sandbox_dir, candidate, credentials, logger, trace_id) -> str | None`

**Runs all verification checks.** Called inside the loop when Claude signals
HARNESS_COMPLETE. Returns issue description string if problems found, None if
clean. Checks: placeholder URLs, auth method consistency, `def run(` exists,
live API validation.

### `_run_live_validation(sandbox_dir, credentials, candidate, logger, trace_id) -> tuple[bool | None, str]`

**Runs `live_test.py` and parses the result.** Returns (passed, notes).
Distinguishes success, soft errors (endpoint exists, auth works), hard failures,
and cases where no credentials are available.

### `_build_initial_message(candidate, input_data) -> str`

**Builds the first user message.** Aggregates seed knowledge from Agents 1, 3,
and 4 into a formatted markdown message.

### `_dispatch_tool(tool_name, tool_input, sandbox_dir) -> str`

**Routes custom tool calls to their handlers.** Only handles tools in
`CUSTOM_TOOL_NAMES` = {"write_file", "run_code", "read_file"}.

### `_tool_write_file(tool_input, sandbox_dir) -> str`

**Writes a file to the sandbox directory.** Security: strips path components,
restricts extensions to `ALLOWED_EXTENSIONS`.

### `_tool_run_code(tool_input, sandbox_dir, extra_env=None) -> str`

**Runs a shell command in the sandbox.** Uses venv-aware environment from
`_build_sandbox_env()`. Timeout: `AGENT5_CODE_TIMEOUT` (120s, for async APIs).
Output truncated to 5000 chars. Exit code determines `is_error: true` on tool results.

### `_tool_read_file(tool_input, sandbox_dir) -> str`

**Reads a file from the sandbox.** Sandbox-only, path traversal stripped,
output truncated to 10000 chars.

### `_extract_text_from_response(response) -> str`

**Pulls all text content blocks from a response.** Responses can contain
mixed content (text + tool_use blocks). Extracts only the text.

### `_extract_and_save_web_content(response, sandbox_dir, existing_count=0) -> list[str]`

**Extracts page text from WebFetchToolResultBlock in the API response and saves
to `fetched_docs_{n}.txt` files.** Returns list of saved filenames. The page
text is extracted from `block.content.content.source.data` (PlainTextSource).
This is the same text Claude sees in context. By saving it to a file, the
context compression can drop it from conversation history and let Claude
`read_file()` it on demand.

### `_save_conversation_log(sandbox_dir, conversation_log, candidate_name) -> None`

**Saves the conversation log to `conversation_log.json` in the sandbox.**
Records every turn: Claude's text, tool calls, tool results, verification
gate outcomes, context resets, and cost per turn. Non-critical: if saving
fails, the build continues.

### `_env_var_similarity(a, b) -> float`

**Computes similarity between two normalized env var names using longest common
substring ratio.** Used by `_inject_live_test_script()` to match registry env
var names to harness env var names when they differ slightly (e.g.,
`NANONETS_API_KEY` vs `NANONETS__INC_API_KEY`).

### `_candidate_slug(name) -> str`

**Converts a candidate name to a filesystem-safe slug.**
"Google Document AI" becomes `google_document_ai`. Truncated to 40 chars.

### `_calculate_call_cost(response, model) -> float`

**Calculates the cost of a single API call.** Uses `MODEL_PRICING` from config.

### `_read_harness_code(sandbox_dir) -> str | None`

**Reads `harness.py` from the sandbox if it exists.**

### `_read_requirements(sandbox_dir) -> list[str]`

**Reads `requirements.txt` from the sandbox.** Returns list of package names.

### `_extract_env_vars_from_code(code) -> list[str]`

**Parses harness.py to find auth-related env var names.** Finds `os.environ.get()`
and `os.environ[]` patterns, filters to auth-related vars (KEY, TOKEN, SECRET,
etc.).

### `_categorize_failure(text) -> str`

**Infers a structured failure category from the agent's failure message.**

| Keywords in text | Category |
|---|---|
| docs, documentation, 404, 403, not found | `docs_unusable` |
| auth, key, paid, enterprise, subscription | `auth_blocked` |
| incompatible, not support, doesn't support | `api_incompatible` |
| install, pip, package, dependency | `dependency_failure` |
| timeout, budget, turns, credit, balance, billing, quota, exceeded, rate limit | `build_timeout` |
| (none match) | `unknown` |

---

## 15. Security Model

Agent 5 is the first agent in the pipeline that executes code on the local
machine. Security is critical.

### Threat Model

The system prompt instructs Claude to write and execute Python code. Claude
might (through hallucination or adversarial prompt injection via malicious
API docs pages) attempt to:

1. Write files outside the sandbox directory
2. Execute arbitrary system commands
3. Read sensitive files from the host system
4. Write executable binaries
5. Exfiltrate data via the code it writes

### Defense: write_file

```
  Input: {"filename": "../../../etc/passwd", "content": "evil"}
                               |
                               v
  Step 1: Path(raw_filename).name  -->  "passwd"
          (strips ALL path components, including ../)

  Step 2: Check extension: "" not in ALLOWED_EXTENSIONS -> REJECTED

  ALLOWED_EXTENSIONS = {".py", ".txt", ".json", ".cfg", ".toml", ".sh", ".yaml", ".yml"}

  Result: "Error: file extension '' not allowed."
```

### Defense: run_code

```
  Constraints:
    - cwd = sandbox_dir (cannot escape via cd)
    - timeout = 120 seconds (increased for async APIs that need polling)
    - output truncated to 5000 chars (prevents token explosion)
    - shell=True (required for pip, python commands)
    - PYTHONDONTWRITEBYTECODE=1 (prevents .pyc littering)
    - Venv isolation: python/pip resolve to candidate's venv, not system
```

### Defense: read_file

```
  Input: {"filename": "../../../etc/shadow"}
  Step 1: Path(raw_filename).name  -->  "shadow"
  Step 2: target = sandbox_dir / "shadow"
  Step 3: target.exists() -> False (no such file in sandbox)
  Result: "Error: 'shadow' does not exist in sandbox"
```

### Defense: venv isolation

Each candidate's packages are installed in its own venv. One candidate's
malicious dependency cannot affect another candidate's build.

### What Is NOT Prevented (and Why It Is Acceptable)

- Network access from the sandbox (code can make HTTP calls — necessary for API integration)
- System command execution (shell=True allows arbitrary commands — needed for pip/python)
- Resource exhaustion beyond the 120s timeout

The security model is "defense in depth" — enough for the current CLI-only
testing phase. For production/cloud deployment, replace venvs with containers
that add network isolation and filesystem restrictions (see Cloud Scaling Path).

---

## 16. Schemas — Input, Output, and Everything Between

### Agent5Input

```
  Agent5Input
  +---------------------------------------------+
  | validated_candidates: list[ScreenedCandidate]|  From Agent 4
  | user_understanding: UserUnderstandingOutput  |  From Agent 1
  | test_cases: Agent3Result                     |  From Agent 3
  | trace_id: str                                |  UUID for logging
  | provider_credentials: dict | None            |  From provider registry
  +---------------------------------------------+
```

Why does Agent 5 need data from three upstream agents plus the registry?

- **Agent 4 candidates**: What to build harnesses for (the primary input)
- **Agent 1 understanding**: What input/output types the user needs (context)
- **Agent 3 test cases**: What input_type/output_type values the harness must support
- **Provider credentials**: API keys for live validation during building

### TestHarness

The success case. Contains everything the post-loop test runner needs to import and run the harness.

| Field | Type | Purpose |
|---|---|---|
| candidate_name | str | Identifies which service |
| provider | str | Company behind the service |
| harness_dir | str | Absolute path to sandbox dir |
| entry_file | str | Always "harness.py" |
| requirements | list[str] | Pip packages to install |
| auth_env_vars | list[str] | Env var names for auth (e.g., GOOGLE_API_KEY) |
| auth_method | str | api_key, oauth2, bearer_token, etc. |
| supported_input_types | list[str] | What input_type values the harness handles |
| supported_output_types | list[str] | What output_type values the harness produces |
| smoke_test_passed | bool | Did the structural smoke test pass? |
| live_validation_attempted | bool | Was a live API call attempted? |
| live_validation_passed | bool or None | Did the live API call succeed? None if not attempted |
| live_validation_notes | str or None | Details of the live validation result |
| validation_notes | str | Smoke test output, verification gate results, and build notes |
| build_turns | int | How many API calls it took |
| build_cost_usd | float | Total cost of building this harness |
| harness_code | str | Complete Python source of harness.py |

Why both `harness_dir` AND `harness_code`?

- `harness_dir` is the local path for the test runner to import from (local execution)
- `harness_code` is the portable source (for cloud deployment, database storage,
  or shipping to a container)

### FailedHarness

The failure case. Contains structured failure info for diagnostics and backfill.

| Field | Type | Purpose |
|---|---|---|
| candidate_name | str | Which service failed |
| provider | str | Company behind the service |
| failure_reason | str | Human-readable explanation |
| failure_category | str | Structured category (see below) |
| partial_code | str or None | Last harness code if any was written |
| turns_attempted | int | How many turns before giving up |

The six failure categories:

| Category | Meaning | Example |
|---|---|---|
| `docs_unusable` | API docs inaccessible or too vague | "docs.example.com returned 403" |
| `auth_blocked` | Cannot authenticate without paid account | "Requires enterprise subscription" |
| `api_incompatible` | API exists but wrong capabilities | "API only supports image, not document" |
| `build_timeout` | Hit max turns or budget | "Exceeded 15 turns without passing smoke test" |
| `dependency_failure` | Required packages cannot install | "google-cloud-documentai requires Python 3.12" |
| `unknown` | Unexpected failure | "Segfault in native extension" |

### Agent5Result

| Field | Type | Purpose |
|---|---|---|
| harnesses | list[TestHarness] | Successfully built harnesses |
| failed_harnesses | list[FailedHarness] | Candidates that failed |
| total_candidates_attempted | int | Cross-check: must = len(harnesses) + len(failed) |
| total_build_cost_usd | float | Sum of all build costs |
| build_summary | str | Human-readable summary |

---

## 17. Configuration Knobs

All Agent 5 settings are in `puzzleeval/config.py`. Each has an environment
variable override.

### AGENT5_BUILDER_MODEL

```
Default: RESEARCH_MODEL (Sonnet 4.6)
Env var: PUZZLEEVAL_BUILDER_MODEL
```

The model used for the builder agent. Sonnet 4.6 was chosen because:
- Same price as Sonnet 4.5 ($3/$15 per MTok)
- Handles web content (fetched API docs) better than 4.5
- Good at code generation (the primary task)

### AGENT5_MAX_TURNS

```
Default: 15
Env var: PUZZLEEVAL_AGENT5_MAX_TURNS
```

Maximum conversation turns per candidate. 15 is enough for:
- 2-3 turns reading docs (web_fetch + possibly web_search)
- 1 turn writing initial code + requirements
- 1 turn installing deps
- 2-3 fix cycles (test, fail, fix, test)
- 1-2 verification retries
- Margin for complex APIs with unusual auth or response formats

### AGENT5_MAX_BUDGET_PER_CANDIDATE

```
Default: $3.00
Env var: PUZZLEEVAL_AGENT5_BUDGET_PER_CANDIDATE
```

Maximum spend per candidate. Checked at the start of each turn. If accumulated
cost exceeds this, the loop stops and returns whatever it has.

### AGENT5_MAX_BUDGET_TOTAL

```
Default: $20.00
Env var: PUZZLEEVAL_AGENT5_BUDGET_TOTAL
```

Not enforced at runtime (checked by the validator post-hoc). If total cost
exceeds this, the validator emits a warning.

### AGENT5_MAX_PARALLEL

```
Default: 5
Env var: PUZZLEEVAL_AGENT5_MAX_PARALLEL
```

Maximum number of candidates building simultaneously.

### AGENT5_CODE_TIMEOUT

```
Default: 120 seconds
Env var: PUZZLEEVAL_AGENT5_CODE_TIMEOUT
```

Timeout for `run_code` tool execution. Increased from 30s to 120s to accommodate
async APIs that require polling (e.g., submit document, wait for processing, retrieve result).

### AGENT5_MAX_OUTPUT_TOKENS

```
Default: 8192
Env var: PUZZLEEVAL_AGENT5_MAX_TOKENS
```

Maximum tokens in Claude's response per turn. Higher than the default 4096
because code generation produces more tokens.

### AGENT5_MAX_VERIFICATION_RETRIES

```
Default: 2
Env var: PUZZLEEVAL_AGENT5_MAX_VERIFICATION_RETRIES
```

Maximum times the verification gate can feed issues back to Claude for fixing.
With 2 retries, the verification gate runs up to 3 times (initial check + 2
retries). Setting to 0 disables the verification gate (immediate exit on
HARNESS_COMPLETE).

### AGENT5_MAX_CANDIDATES

```
Default: 4
Env var: PUZZLEEVAL_AGENT5_MAX_CANDIDATES
```

Top N candidates by user-fit score (`relevance_score`). Not all validated
candidates get harnesses built. Default 4 gives 3 for comparison + 1 buffer
for build failures. `run_implement_test_env_agent()` sorts candidates by
`relevance_score` (descending) and takes the top N.

### Prompt Caching

```
Type: ephemeral (5-minute TTL)
Applied to: BUILDER_SYSTEM_PROMPT (system message)
```

The system prompt is identical across all turns of a candidate's build loop.
With ephemeral caching, the first turn pays full price for the system prompt
(~3.5K tokens), and subsequent turns read it from cache at 0.10x cost. Since
turns within a candidate's build happen every few seconds, the 5-min TTL is
sufficient. Different candidates running in parallel get their own cache entries.

### PROVIDER_REGISTRY_PATH

```
Default: "provider_registry.json"
Env var: PUZZLEEVAL_PROVIDER_REGISTRY
```

Path to the centralized API key registry file. If the file does not exist,
the pipeline falls back to checking environment variables.

---

## 18. Cost Model

### Where Tokens Go (After Context Compression)

```
  Per-Candidate Cost Breakdown (Typical: 6-10 turns, $0.40-0.60)
  ================================================================

  Turn 1: Fetch API docs via web_fetch + state PLAN
    Input:  ~3K (system prompt, cached after first turn) + ~1K (seed message)
    Output: ~500 (text planning + tool_use block)
    + web_fetch content: ~10-50K (injected server-side)
    Subtotal: ~$0.05-0.20
    → Docs saved to fetched_docs_0.txt
    → PLAN detected → CONTEXT RESET: drop web content from messages

  Turn 2: Write harness.py + requirements.txt
    Input:  ~4K (seed + plan + doc file refs) — NOT growing from web content
    Output: ~2K (tool_use blocks with Python code)
    Subtotal: ~$0.02-0.05

  Turn 3: pip install + write smoke_test.py
    Input:  ~6K (stable — no web content accumulation)
    Output: ~1K (run_code + write_file blocks)
    Subtotal: ~$0.02-0.05

  Turn 4-5: Run smoke test, fix if needed
    Input:  ~8K (stable context + code + error output)
    Output: ~1-2K (rewritten code)
    Subtotal: ~$0.05-0.10

  Turn 6: Self-review + read_file(fetched_docs_0.txt) + live test
    Input:  ~10K (stable context + doc file content from read_file)
    Output: ~500 (review text + run_code block)
    Subtotal: ~$0.03-0.05

  Turn 7: HARNESS_COMPLETE + verification gate
    Input:  ~10K
    Output: ~200 (summary text)
    Subtotal: ~$0.03-0.05

  Turn 8-9: Verification retry (if needed)
    Input:  ~12K (stable context + verification feedback)
    Output: ~1-2K (fixed code + re-run tests)
    Subtotal: ~$0.05-0.10

  Web search costs: 1-2 searches * $0.01 = $0.01-0.02
  Prompt cache savings: ~$0.02-0.04 (system prompt cached across 6-10 turns)

  TOTAL PER CANDIDATE: ~$0.80-1.50 (typical, 7-9 turns with live validation)

  TOTAL FOR 4 CANDIDATES (AGENT5_MAX_CANDIDATES=4): ~$4.00-5.00
  working_test_6 actual: $4.40 for 4 candidates, 289s, 7-9 turns each
```

### Full Pipeline Cost (Agents 1-5)

```
  Agent 1 (3-turn conversation):     ~$0.05
  Agent 2 (research + structure):    ~$0.35
  Agent 3F (file-based tests):       ~$0.06
  Agent 4 (screening):               ~$0.90
  Agent 5 (4 harnesses + test exec): ~$4.00-5.00
  ──────────────────────────────────────────
  TOTAL:                              ~$5-6
```

Agent 5 is still the most expensive agent but context compression cut its
cost by roughly half. The cost scales linearly with candidate count. With
AGENT5_MAX_CANDIDATES=4 (down from building all validated), total cost is
further reduced.

### Cost Optimization Levers

1. **Context compression (DONE)**: Extract web_fetch content to files, reset
   conversation after research phase. Already implemented — saves ~50%.

2. **Prompt caching (DONE)**: System prompt cached with ephemeral/5-min TTL.
   Saves ~$0.003 per turn, ~$0.02-0.04 per candidate.

3. **Reduced tool limits (DONE)**: web_fetch 3 (was 4), web_search 2 (was 3).
   Claude uses saved docs via read_file instead of re-fetching.

4. **Reduce candidates via stricter Agent 4 screening**: 3 candidates instead
   of 5 saves ~$1-1.50 total.

5. **Lower max_turns from 15 to 10**: Catches runaway builds earlier. Risk:
   complex APIs may fail that would have succeeded in 12 turns.

6. **Reduce verification retries from 2 to 1**: Saves ~$0.05-0.15 per candidate
   when verification finds issues. Risk: some fixable issues may not get fixed.

---

## 19. Error Handling Matrix

Agent 5 follows the principle: **never crash the pipeline**. Every error is
caught and converted to a `FailedHarness` or logged as a warning.

### Per-Candidate Errors (in _build_single_harness)

| Error | Cause | Handling | Result |
|---|---|---|---|
| `anthropic.RateLimitError` | API rate limit hit | Retry with exponential backoff (15s, 30s, 60s). After 3 retries, stop build | `FailedHarness(category="build_timeout")` after retries exhausted |
| `anthropic.APIConnectionError` | Network failure to Anthropic | Log warning, stop build | `FailedHarness(category="unknown")` |
| `anthropic.APIStatusError` | Anthropic API error (5xx) | Log warning, stop build | `FailedHarness(category="unknown")` |
| Budget exceeded | `accumulated_cost >= MAX_BUDGET` | Log warning, break loop | `TestHarness` if harness.py exists, else `FailedHarness` |
| Max turns exceeded | `turn >= MAX_TURNS` | Break loop | `TestHarness` if harness.py exists, else `FailedHarness` |
| "HARNESS_FAILED" signal | Claude gives up | Return immediately | `FailedHarness` with categorized reason |
| `pause_turn` | Server tools took too long | Reset messages, continue | Loop continues |
| `end_turn` without signal | Claude stopped unexpectedly | Check for harness.py on disk | `TestHarness` if exists, else continue loop |
| Verification gate failure | Issues found in harness | Feed back to Claude | Loop continues (up to MAX_RETRIES) |

### Thread-Level Errors (in run_implement_test_env_agent)

| Error | Cause | Handling | Result |
|---|---|---|---|
| Unexpected exception from thread | Bug in _build_single_harness | Catch in as_completed, log error | `FailedHarness(category="unknown")` |

### Tool Execution Errors (in _dispatch_tool)

| Error | Cause | Handling | Result |
|---|---|---|---|
| Empty filename | Claude passed empty filename | Return error string | Claude sees error, adjusts |
| Bad extension | Claude tried to write .exe | Return error string | Claude sees error, uses .py |
| `subprocess.TimeoutExpired` | run_code exceeded 120s | Return timeout message | Claude sees error, simplifies code |
| `OSError` in write/read | Permission or disk error | Return error string | Claude sees error, adjusts |
| Unknown tool name | Bug in tool routing | Return error string | Claude sees error |

### Key Design: Errors Flow Back to Claude

For tool execution errors, the error message is returned as a `tool_result`
content block. Claude SEES the error and can react. This self-healing loop
is why the autonomous agent pattern works for code generation.

---

## 20. Comparison to Agent 4

Agent 5 follows the same parallelism pattern as Agent 4, but the per-candidate
work is fundamentally different.

| Aspect | Agent 4 (Screening) | Agent 5 (Test Env) |
|---|---|---|
| **Per-candidate work** | Single-shot: search + maybe fetch + verdict | Multi-turn: read docs, write code, test, verify, fix, repeat |
| **API calls per candidate** | 1 (one client.messages.create) | 5-15 (multi-turn conversation loop with verification) |
| **Tools** | web_search + web_fetch (server only) | web_search + web_fetch (server) + write_file + run_code + read_file (custom) |
| **Local side effects** | None (purely informational) | Creates venv, files on disk (harness.py, requirements.txt, smoke_test.py, live_test.py) |
| **Verification** | N/A (single-shot) | In-loop verification gate with retries |
| **Output determination** | Claude writes a text verdict, structuring call formats it | Programmatic: check harness.py on disk, read smoke test output, verification status |
| **Structuring call** | Yes (1 final LLM call to create Agent4Result) | No (Agent5Result assembled programmatically) |
| **Isolation** | Per-candidate context, no shared state | Per-candidate context + per-candidate venv + per-candidate sandbox dir |
| **Cost per candidate** | ~$0.10-0.15 | ~$0.40-0.60 (with context compression) |
| **Typical wall-clock** | 15-30 seconds | 60-180 seconds |
| **Parallelism** | ThreadPoolExecutor, identical | ThreadPoolExecutor, identical |
| **Error handling** | Returns REJECT verdict | Returns FailedHarness with categorized reason |

---

## 21. Output Quality Validator

`validate_agent5_output()` in `puzzleeval/validators.py` checks structural
correctness of the Agent5Result.

### Checks Performed

| Check | Severity | What It Catches |
|---|---|---|
| `harnesses + failed = total_candidates_attempted` | ERROR | Silently dropped candidates (bug) |
| At least 1 harness succeeded | ERROR | Pipeline cannot continue with zero harnesses |
| Each harness has non-empty `harness_code` | ERROR | Empty harness is useless |
| Each harness has non-empty `entry_file` | ERROR | Test runner needs this to import |
| Each harness has non-empty `harness_dir` | ERROR | Test runner needs this path |
| `harness_dir` exists on disk | WARNING | Directory may have been cleaned up |
| Each harness has `auth_env_vars` | WARNING | Test runner won't know how to authenticate |
| Each harness passed smoke test | WARNING | Harness may not work correctly |
| Each harness has requirements | WARNING | May be missing dependencies |
| Live validation attempted but failed | WARNING | Harness may not work with real API |
| No harnesses were live-validated | WARNING | No provider credentials available |
| Each failed harness has non-empty `failure_reason` | ERROR | No diagnostic info |
| Each failed harness has valid `failure_category` | ERROR | Invalid enum value |
| Every Agent 4 candidate appears in results | WARNING | Silently dropped candidate |
| Total cost within budget | WARNING | Budget exceeded |
| Only 1 harness built (not enough for comparison) | WARNING | Need 2+ for meaningful comparison |

### What the Validator Does NOT Check

- Whether `harness_code` is valid Python (the smoke test handles this)
- Whether the API endpoint URLs in the code are correct (the verification gate + post-loop test execution handle this)
- Whether the harness actually works with real API calls (post-loop test execution handles this)
- Semantic capability matching (same philosophy as Agent 4's validator)

---

## 22. CLI Wiring

Agent 5 is triggered by the `--agent5` flag in `cli.py`.

```bash
python -m puzzleeval.cli --text "I need invoice OCR" --agent5 --pretty
```

### What --agent5 Implies

- `--agent5` implies `--agent4` (Agent 5 needs screened candidates)
- `--agent4` implies `--agent2` (Agent 4 needs researched candidates)
- Agent 3 is also run (Agent 5 needs test case format context)

### Pipeline Execution Order Under --agent5

```
  1. Agent 1 conversation loop (interactive)
  2. Agent 2 research + Agent 3F file-based tests (in PARALLEL when --agent5 is used)
  3. Agent 4 screening (parallel per-candidate verification)
  4. Load provider registry, resolve credentials for all candidates
  5. Agent 5 implement test env (parallel per-candidate building with verification)
```

Agent 2 and Agent 3F now run in parallel when `--agent5` is used. Neither depends on the
other (Agent 2 needs Agent 1 output for research, Agent 3F needs Agent 1 output + test files),
so they can execute concurrently. Agent 5 waits for both to complete.

### Agent5Input Assembly in CLI

```python
# Load provider registry
registry = load_registry()
provider_creds = get_all_credentials(registry, agent4_result.validated_candidates)

agent5_input = Agent5Input(
    validated_candidates=agent4_result.validated_candidates,
    user_understanding=result.result,   # Agent 1's output
    test_cases=agent3_result,           # Agent 3's output
    trace_id=trace_id,
    provider_credentials=provider_creds, # From registry or None
)
```

### Output

The final JSON printed to stdout is `Agent5Result.model_dump_json()`.
All intermediate outputs, validation results, and logs are saved to
`runs/{trace_id}/`.

### Timing Estimate

```
  Agent 1: 5-30 seconds (depends on conversation turns)
  Agent 2 + Agent 3F: 30-60 seconds (in parallel)
  Agent 4: 60-120 seconds
  Agent 5: 250-350 seconds (~4-6 minutes, includes live validation + test execution)
  ────────────────────────────
  Total:   ~6-10 minutes
  working_test_6 actual: 289s for Agent 5 phase
```

---

## 23. Test Coverage Map

Tests are in `tests/test_agent5.py`. Run with:

```bash
ANTHROPIC_API_KEY=dummy python -m pytest tests/test_agent5.py -v
```

**52 tests total** covering schemas, helpers, validators, integration,
provider registry, venv/sandbox, and verification checks.

### Schema Tests (TestSchemas class — 5 tests)

| Test | What It Verifies |
|---|---|
| `test_agent5_input_valid` | Agent5Input constructs with valid data (including provider_credentials) |
| `test_test_harness_all_fields` | TestHarness accepts all fields including live_validation_* and harness_code |
| `test_failed_harness_all_fields` | FailedHarness accepts all fields |
| `test_failed_harness_with_partial_code` | FailedHarness accepts partial_code (optional field) |
| `test_agent5_result_valid` | Agent5Result constructs with harnesses and failed_harnesses |

### Helper Function Tests (TestHelpers class — 16 tests)

| Test | What It Verifies |
|---|---|
| `test_candidate_slug_basic` | "Google Document AI" -> "google_document_ai" |
| `test_candidate_slug_special_chars` | "AWS Textract (OCR)" -> "aws_textract_ocr" |
| `test_candidate_slug_long_name` | Names over 100 chars are truncated to 40 |
| `test_candidate_slug_unicode` | Unicode characters are handled gracefully |
| `test_dispatch_write_file` | write_file creates file in sandbox with correct content |
| `test_dispatch_write_file_rejects_path_traversal` | "../../../etc/passwd" does not escape sandbox |
| `test_dispatch_write_file_rejects_bad_extension` | ".exe" files are rejected |
| `test_dispatch_run_code` | Shell commands run in sandbox and return output |
| `test_dispatch_run_code_timeout` | Commands exceeding timeout produce error message |
| `test_dispatch_run_code_truncates_output` | Output over 5000 chars is truncated |
| `test_dispatch_read_file` | Files in sandbox can be read |
| `test_dispatch_read_file_missing` | Missing files produce error message |
| `test_categorize_failure_docs` | "API docs returned 404" -> "docs_unusable" |
| `test_categorize_failure_auth` | "Requires paid subscription key" -> "auth_blocked" |
| `test_categorize_failure_credit_balance` | "credit balance" -> "build_timeout" |
| `test_categorize_failure_rate_limit` | "rate limit" -> "build_timeout" |
| `test_categorize_failure_unknown` | Unrecognized text -> "unknown" |
| `test_extract_env_vars_from_code` | Finds auth-related env vars in source code |
| `test_extract_env_vars_empty_code` | Empty code returns empty list |
| `test_calculate_call_cost` | Cost calculation is mathematically correct |

### Validator Tests (TestValidator class — 8 tests)

| Test | What It Verifies |
|---|---|
| `test_valid_result_passes` | Well-formed result with 2 harnesses passes validation |
| `test_zero_harnesses_is_error` | Zero successful harnesses is a blocking error |
| `test_count_mismatch_is_error` | harnesses + failed != total is a blocking error |
| `test_missing_candidate_is_warning` | Agent 4 candidate not in results produces warning |
| `test_empty_harness_code_is_error` | Empty harness_code is a blocking error |
| `test_invalid_failure_category_is_error` | Invalid category string is a blocking error |
| `test_single_harness_is_warning` | Only 1 harness is a non-blocking warning |
| `test_smoke_test_not_passed_is_warning` | smoke_test_passed=False is a warning |

### Integration Tests (TestIntegration class — 4 tests)

| Test | What It Verifies |
|---|---|
| `test_successful_build` | With mocked API (2 turns: write + complete), produces TestHarness |
| `test_failed_build_returns_failed_harness` | Agent that gives up produces FailedHarness, not crash |
| `test_rate_limit_produces_failed_harness` | RateLimitError produces FailedHarness, not pipeline crash |
| `test_max_turns_exceeded` | Exceeding max turns produces FailedHarness |

### Provider Registry Tests (TestProviderRegistry class — 5 tests)

| Test | What It Verifies |
|---|---|
| `test_load_missing_file` | Missing registry file returns empty registry |
| `test_load_valid_registry` | Valid JSON file loads correctly with all fields |
| `test_get_credentials_match` | Provider name matches and returns credentials |
| `test_get_credentials_no_match` | Unknown provider returns None |
| `test_get_credentials_env_fallback` | Falls back to os.environ when registry has no match |

### Venv and Sandbox Tests (TestVenvAndSandbox class — 3 tests)

| Test | What It Verifies |
|---|---|
| `test_create_venv_success` | Venv creation succeeds and .venv directory exists |
| `test_sandbox_env_uses_venv` | _build_sandbox_env() prepends venv bin to PATH |
| `test_sandbox_env_works_without_venv` | _build_sandbox_env() works without a venv (fallback) |

### Verification Check Tests (TestVerificationChecks class — 7 tests)

| Test | What It Verifies |
|---|---|
| `test_clean_harness_passes` | Well-formed harness with no placeholder URLs passes |
| `test_placeholder_url_detected` | harness.py containing "example.com" is flagged |
| `test_auth_mismatch_detected` | bearer_token candidate with Basic auth in code is flagged |
| `test_missing_harness_detected` | Missing harness.py returns critical error |
| `test_no_credentials_skipped` | No credentials skips live validation cleanly |
| `test_live_validation_with_credentials_runs_script` | Live test runs when credentials available |
| `test_live_validation_soft_error_passes` | 400 "invalid input" counts as PASS (endpoint exists) |

### What Is NOT Tested (and Why)

- **Real API calls**: Tests use mocked Anthropic client. Real API calls are
  expensive ($1-2 per run) and non-deterministic.

- **web_fetch/web_search behavior**: Server tools are executed by Anthropic's
  API, not our code. We cannot mock server-side behavior.

- **End-to-end pipeline**: Testing Agent 1 through Agent 5 end-to-end requires
  real API calls. This is tested manually and costs ~$3-4 per run (after
  context compression optimization and AGENT5_MAX_CANDIDATES=4).

---

## 24. Cloud Scaling Path

Agent 5 is designed to scale from local CLI to cloud containers. The venv
sandboxing is the designed seam for this transition.

```
  STAGE 1: Current (CLI)
  ================================================
  +----------------+
  | Your Laptop    |
  |                |
  | ThreadPool     |
  | +---+ +---+   |
  | |T1 | |T2 |   |  Threads share one process
  | |venv| |venv|  |  Per-candidate venvs on local disk
  | +---+ +---+   |  30s timeout via subprocess
  | +---+ +---+   |  Provider registry from local JSON
  | |T3 | |T4 |   |
  | |venv| |venv|  |
  | +---+ +---+   |
  +----------------+


  STAGE 2: FastAPI Server
  ================================================
  +------------------+
  | ECS / Cloud Run  |
  |                  |
  | FastAPI          |
  | ThreadPool       |  Same code, thin API wrapper
  | Per-candidate    |  Venvs on container disk
  | venvs            |  Provider registry from Secrets Manager
  +------------------+


  STAGE 3: Distributed Workers (per-candidate)
  ================================================
                    +--------------------+
                    | Orchestrator       |
                    | (FastAPI / Lambda) |
                    +--------+-----------+
                             |
              +--------------+--------------+
              |              |              |
              v              v              v
  +------------+  +------------+  +------------+
  | Worker 1   |  | Worker 2   |  | Worker N   |
  | (Lambda /  |  | (Lambda /  |  | (Lambda /  |
  | Cloud Run) |  | Cloud Run) |  | Cloud Run) |
  | Own venv   |  | Own venv   |  | Own venv   |
  | Own sandbox|  | Own sandbox|  | Own sandbox|
  +------------+  +------------+  +------------+


  STAGE 4: Container-Isolated Sandboxes
  ================================================
  Replace _create_venv() with container creation.
  _build_sandbox_env() configures container exec env.
  The rest of the code does not change.

  +==============+
  | Container    |  Each candidate gets its own
  | - harness.py |  ephemeral container with:
  | - smoke_test |  - No outbound network (except API)
  | - live_test  |  - Read-only filesystem (except /sandbox)
  | - 30s timeout|  - CPU/memory limits
  +==============+  - Auto-destroyed after build
```

### What Makes Scaling Easy

1. `_build_single_harness()` is stateless and self-contained
2. No shared state between candidates
3. `TestHarness` contains `harness_code` as a string (portable, no path dependency)
4. Each candidate's venv/sandbox is independent
5. Budget tracking is per-candidate (no global shared budget counter)
6. `_create_venv()` and `_build_sandbox_env()` are the only functions that know about venv paths — the designed swap point for containers
7. Provider registry interface (`get_credentials()`) is designed for secrets manager swap

---

## 25. Lessons Learned

These lessons were learned during development, testing, and the hardening phase.
They cost real money.

### 1. Multi-Turn Loops Need Budget Guards

Without `AGENT5_MAX_BUDGET_PER_CANDIDATE`, a candidate with complex API docs
could spiral to 20+ turns as Claude endlessly tries to fix obscure errors.
$3 per candidate is generous but bounded.

### 2. Smoke Tests Must Mock HTTP, Not Just Assert Structure

Early smoke tests only checked `hasattr(harness, "run")`. Claude would write
harnesses that import fine but crash on `run()` because they make immediate
API calls during initialization. The mock-HTTP smoke test catches this by
ensuring run() works even when the network is unavailable.

### 3. write_file Needs Extension Restrictions

Without extension checking, Claude occasionally tried to write `.bat` or
`.sh` scripts to "set up the environment." Restricting to known safe
extensions prevents this.

### 4. run_code Output MUST Be Truncated

One candidate's smoke test produced a 50KB error traceback (deeply nested
SDK import failure). Without truncation, this would have consumed 12K+ tokens
of context on the next turn. The 5000-char limit prevents token explosion.

### 5. No Structuring Call Needed

Agent5Result is assembled programmatically from build artifacts. No final
LLM call needed. Saves ~$0.10 and 5 seconds per run.

### 6. pause_turn Must Reset Messages

When `pause_turn` fires during web_fetch, the context includes incomplete
server tool execution. The loop resets to the initial message plus the last
response, giving Claude a clean restart point.

### 7. "HARNESS_COMPLETE" Is Better Than Checking harness.py

An earlier design checked for `harness.py` on disk as the completion signal.
Problem: Claude sometimes writes an incomplete harness.py, then keeps working.
The explicit signal means Claude has verified the smoke test passed and
considers the harness done. Checking the file is a fallback for edge cases.

### 8. Categorized Failures Enable Automation

`_categorize_failure()` maps free-text failure messages to structured
categories, enabling automated backfill requests and dashboard analytics.

### 9. The Verification Gate Must Be Inside the Loop (Not Post-Loop)

**This was the core hardening insight.** The initial design ran verification
after the loop completed. Problem: Claude's conversation context was gone by
then. If the verification found the endpoint URL was wrong, there was no way
to ask Claude to fix it.

Moving verification inside the loop means:
- Claude still has the API docs in context
- The feedback message tells Claude exactly what is wrong
- Claude can fix the code, re-run tests, and re-signal
- The verification gate runs again
- Most issues are fixed automatically in 1-2 retries

### 10. Live Test Injection Before the Loop Is Better Than Post-Loop

Early design ran live validation only after the loop. The current design
injects `live_test.py` before the loop starts, so:
- Claude discovers it during Phase 3 and runs it itself
- If the live test fails, Claude reads the error and fixes the harness
- The fix happens while the API docs are still in Claude's context
- The verification gate provides a second check

### 11. Venv Isolation Prevents Cross-Candidate Package Conflicts

Early testing without venvs caused intermittent failures when two candidates
required conflicting package versions. Per-candidate venvs eliminate this.
The venv creation adds ~5 seconds per candidate but prevents hard-to-debug
dependency conflicts.

### 12. Context Compression Is the Single Biggest Cost Lever

Web_fetch content (~10-50K tokens per page) stays in the conversation for every
subsequent turn, causing context to grow linearly with turn count. By extracting
page text to `fetched_docs_*.txt` files and resetting the conversation after the
research phase, per-candidate cost dropped by ~50%. The key insight: Claude only
needs the full docs during research. During build/verify, it can `read_file()`
specific sections on demand.

### 13. "PLAN:" Is a Reliable Phase Boundary for Context Reset

Claude reliably writes "PLAN:" before transitioning from research to build
(the system prompt explicitly requires it). This makes it a safe trigger for
the context reset. If PLAN is never written (rare edge case), no reset happens
and the loop runs at the old cost — safe degradation, not a crash.

### 14. Rate Limit Retries Prevent Unnecessary Failures

Parallel builds across candidates regularly hit Anthropic's per-minute token
limits. Adding exponential backoff (15s, 30s, 60s) with 3 retries lets the
rate limit window reset. Most rate limits are resolved by the first retry.
Without retries, parallel builds had a ~30% chance of at least one candidate
failing due to rate limits.

### 15. Conversation Logs Are Invaluable for Debugging

Saving `conversation_log.json` to each candidate's sandbox makes build failures
transparent without re-running the pipeline ($2-5 per run). The log shows
exactly what Claude saw, what tools it called, and what went wrong. This has
been the fastest way to diagnose and fix prompt issues.

### 16. Incomplete Harnesses Must Be FailedHarness, Not TestHarness

Early implementation returned `TestHarness` whenever `harness.py` existed on
disk, even if the smoke test never passed. This silently passed broken harnesses
to downstream execution. Now, if the smoke test never passed AND the verification gate never
ran, the result is explicitly a `FailedHarness` with category `build_timeout`.

### 17. PLAN + tool_use in Same Turn Needs Special Handling

When Claude writes PLAN text AND calls write_file in the same response
(common: research + immediately start writing code), the context reset must
dispatch the custom tools silently WITHOUT appending `response.content` back
to messages — that would re-add the web content being dropped. The tools are
dispatched, their results summarized, and the fresh message chain starts with
only the plan + seed knowledge + tool result summaries.

### 18. Re-inject live_test.py Every Time harness.py Is Written

The initial injection (before the loop) does not have harness.py yet, so it
cannot map env var names. After each `write_file("harness.py")`, the loop
calls `_inject_live_test_script()` again with fuzzy env var name mapping
(`_env_var_similarity()`) to handle mismatches like `VERYFI_INC_API_KEY` vs
`VERYFI_API_KEY`.

### 19. Dead-End Detection Prevents Debugging Spirals

Without it, Claude could spend 7 turns trying to fix an unfixable problem
(deprecated endpoint, requires manual dashboard setup, credit balance issues).
The consecutive error counter + strategic reassessment message forces Claude
to either pivot to a different approach or signal HARNESS_FAILED after 3
consecutive error turns. This saves ~$0.30-0.50 per dead-end candidate.

### 20. Not All Validated Candidates Need Harnesses

AGENT5_MAX_CANDIDATES (default 4) selects the top N by user-fit score from
Agent 2. Building 6 harnesses at $0.50 each when we only need 3 for comparison
wastes money. The 4th candidate is buffer for build failures.

---

## Key Files Reference

| File | What to Read |
|---|---|
| `puzzleeval/agents/implement_test_env.py` | Core agent implementation with verification gate, context compression, dead-end detection |
| `puzzleeval/schemas.py` (after Agent 4 schemas) | Agent5Input, TestHarness, FailedHarness, Agent5Result |
| `puzzleeval/config.py` (AGENT5_* settings) | All AGENT5_* settings + PROVIDER_REGISTRY_PATH |
| `puzzleeval/provider_registry.py` | Centralized API key management |
| `puzzleeval/validators.py` (validate_agent5_output) | Agent 5 output quality validator |
| `puzzleeval/cli.py` | --agent5 pipeline wiring + provider registry loading |
| `tests/test_agent5.py` | 52 unit and integration tests |
