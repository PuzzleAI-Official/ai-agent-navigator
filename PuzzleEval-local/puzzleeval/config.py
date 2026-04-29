# ============================================================================
# Configuration — Centralized settings loaded from environment variables
# ============================================================================
# WHY ENVIRONMENT VARIABLES?
#   - API keys should NEVER be hardcoded in source code (security risk).
#   - Different environments (dev, staging, prod) need different settings.
#   - Environment variables are the industry standard for configuration.
#
# HOW TO SET THEM:
#   Linux/Mac:  export ANTHROPIC_API_KEY="sk-ant-..."
#   Windows:    set ANTHROPIC_API_KEY=sk-ant-...
#   Or use a .env file (but never commit it to git).
#
# EVERY agent in the pipeline imports from this file, so changes here
# affect the entire system.
# ============================================================================

import os


# ---------------------------------------------------------------------------
# Anthropic API Key
# ---------------------------------------------------------------------------
# Your secret key for calling Claude's API. Get one at
# https://console.anthropic.com/. Put it in puzzleeval-api/.env or export
# it in your shell.
#
# Read at import but deliberately NOT enforced here — raising at module
# import time would break anything that wants to inspect / register /
# list parts of the library without actually calling Claude (plugin
# registry, plugin status reporter, unit tests). Call
# `require_anthropic_key()` at the top of any function that's about to
# instantiate an Anthropic client — it raises a clear EnvironmentError
# when the key is missing.
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


def require_anthropic_key() -> str:
    """Return the Anthropic API key; raise a clear error if unset.

    Call this at the start of any function that's about to instantiate
    `anthropic.Anthropic(...)`. Lazy check by design — we want imports
    to succeed in environments where Claude isn't actually called
    (CLI status commands, plugin readiness inspection, unit tests).
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Get your API key from https://console.anthropic.com/ and "
            "either:\n"
            "  - put it in puzzleeval-api/.env (auto-loaded by CLI + FastAPI)\n"
            "  - or `export ANTHROPIC_API_KEY=sk-ant-...` in your shell"
        )
    return key


# ---------------------------------------------------------------------------
# Model Selection
# ---------------------------------------------------------------------------
# Which Claude model to use. Defaults to claude-sonnet-4-6 — best speed/intelligence
# balance. Used by Agents 1-4 and post-loop evaluation.
# Override with: export PUZZLEEVAL_MODEL="claude-opus-4-7"
#
# Available models (as of 2026):
#   claude-opus-4-7    — most capable, slowest, most expensive (Agent 5 builder)
#   claude-sonnet-4-6  — best speed/intelligence balance (Agents 1-4, research)
#   claude-haiku-4-5   — fastest, cheapest, less capable
# ---------------------------------------------------------------------------
DEFAULT_MODEL = os.environ.get("PUZZLEEVAL_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Agent 1 Model
# ---------------------------------------------------------------------------
# Agent 1 is the DIRECTOR of the pipeline — it decomposes the user's demand
# into an ordered WorkflowBlueprint with step ordering, data flow, role
# assignment, and architecture options (all-in-one vs best-per-step).
# That's a planning task that benefits from Opus 4.7's deeper reasoning.
#
# Cost impact: Agent 1 runs in ~1-3 turns with ~4K in + ~0.5-1K out per turn.
# Opus vs Sonnet adds ~$0.05/evaluation — a ~1% rounding error at pipeline
# scale (~$6 total). Override with PUZZLEEVAL_AGENT1_MODEL if you want to
# run Agent 1 on Sonnet to shave that cost.
# ---------------------------------------------------------------------------
AGENT1_MODEL = os.environ.get("PUZZLEEVAL_AGENT1_MODEL", "claude-opus-4-7")


# ---------------------------------------------------------------------------
# Research Model (Agent 2)
# ---------------------------------------------------------------------------
# Agent 2 uses Sonnet 4.6 for web research. Now the same as DEFAULT_MODEL,
# but kept as a separate constant so web-tool-using agents (2, 4, 5 research)
# can be tuned independently if needed.
# ---------------------------------------------------------------------------
RESEARCH_MODEL = os.environ.get("PUZZLEEVAL_RESEARCH_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Agentic Conversational Evaluation (user_simulator + rubric_judge)
# ---------------------------------------------------------------------------
# Governs the new persona-driven multi-turn evaluation pipeline that
# replaces static turn_script + substring matching for conversational
# tests. See puzzleeval/user_simulator.py and puzzleeval/rubric_judge.py.
#
# Global mode override (per-test TestCase.evaluation_mode wins when
# set — this env var forces a global override across all conversational
# tests in the run):
#   "auto"     → per-test evaluation_mode respected (default)
#   "agentic"  → force agentic path on every conversational test
#   "scripted" → force legacy static-script path (back-compat only —
#                multi-turn conversational evaluation needs agentic;
#                the scripted fallback exists only for legacy tests)
CONVERSATION_EVAL_MODE = os.environ.get(
    "PUZZLEEVAL_CONVERSATION_EVAL_MODE", "auto"
)
# User simulator runs Haiku 4.5 by default — reactive enough for caller
# utterances, 10x cheaper than Sonnet. Override to Sonnet when the
# persona is particularly nuanced (e.g., highly technical customer
# pushing back on agent claims).
USER_SIM_MODEL = os.environ.get(
    "PUZZLEEVAL_USER_SIM_MODEL", "claude-haiku-4-5-20251001"
)
# 0.0 = deterministic, 1.0 = chaotic. 0.3 keeps output reactive but
# stable enough that property-based tests pass across runs.
USER_SIM_TEMPERATURE = float(
    os.environ.get("PUZZLEEVAL_USER_SIM_TEMPERATURE", "0.3")
)
# Typical caller utterance is 30-150 tokens. 512 is a generous cap
# that leaves headroom for compound turns without encouraging monologues.
USER_SIM_MAX_TOKENS = int(
    os.environ.get("PUZZLEEVAL_USER_SIM_MAX_TOKENS", "512")
)
# Rubric judge uses Sonnet 4.6 — quality matters more than cost here;
# one judge call per test versus N simulator calls per test.
RUBRIC_JUDGE_MODEL = os.environ.get(
    "PUZZLEEVAL_RUBRIC_JUDGE_MODEL", "claude-sonnet-4-6"
)
# Judge output is structured (RubricVerdict). 4096 suits typical rubric
# sizes (6 criteria × ~300 tokens reasoning + summary + overhead).
# Rubric judge max_tokens — bumped from 4096 to 8192 (2026-04-23) after
# real-run trace 0c7f085f observed 2 judge crashes with:
#   "Invalid JSON: EOF while parsing a string at line 1 column 2126"
#   "Invalid JSON: EOF while parsing a string at line 1 column 175"
# The judge is emitting JSON-shaped RubricVerdicts; when it hits the
# max_tokens cap mid-string, the partial JSON fails pydantic validation
# and the whole test case loses its rubric verdict. 8192 is enough for
# typical 5-criterion rubrics with detailed per-criterion reasoning
# (~600 chars each × 5 + overhead = ~4KB content, 4KB thinking).
#
# Raise this further if you see recurring JSON-truncation errors for
# a particular rubric shape. Lower to 4096 to measure cost-savings at
# risk of re-introducing truncation.
RUBRIC_JUDGE_MAX_TOKENS = int(
    os.environ.get("PUZZLEEVAL_RUBRIC_JUDGE_MAX_TOKENS", "8192")
)
# Hard ceiling on conversation length regardless of what TestCase.max_turns
# says. Protects worst-case cost envelope: 12 turns × (Haiku + agent call
# + STT) caps out around $0.50-0.80 per test. Tests that set max_turns
# above this get silently clamped down.
CONVERSATION_MAX_TURNS_CEILING = int(
    os.environ.get("PUZZLEEVAL_CONVERSATION_MAX_TURNS_CEILING", "12")
)


# ---------------------------------------------------------------------------
# Screening Model (Agent 4)
# ---------------------------------------------------------------------------
# Agent 4 uses Sonnet 4.6 for web fetch/search verification (same reasons as
# Agent 2: handles web content well, same price as 4.5). Defaults to
# RESEARCH_MODEL so both web-tool-using agents stay in sync. Override with
# PUZZLEEVAL_SCREENING_MODEL if Agent 4 needs independent tuning later.
# ---------------------------------------------------------------------------
SCREENING_MODEL = os.environ.get("PUZZLEEVAL_SCREENING_MODEL", RESEARCH_MODEL)


# ---------------------------------------------------------------------------
# Token Limits
# ---------------------------------------------------------------------------
# Maximum number of tokens Claude can generate in its response.
# 4096 is generous for structured JSON output — most Agent 1 responses
# will be ~500-1000 tokens.
# ---------------------------------------------------------------------------
MAX_TOKENS = int(os.environ.get("PUZZLEEVAL_MAX_TOKENS", "4096"))


# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
# LOG_LEVEL controls how verbose the logs are:
#   DEBUG    — everything, including internal details (noisy)
#   INFO     — normal operations (recommended for development)
#   WARNING  — only potential problems
#   ERROR    — only actual failures
#
# LOG_OUTPUT_PATH: if set, logs also write to this file (in addition to stderr).
# This is useful for later piping to cloud logging services.
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("PUZZLEEVAL_LOG_LEVEL", "INFO")
LOG_OUTPUT_PATH = os.environ.get("PUZZLEEVAL_LOG_PATH", None)


# ---------------------------------------------------------------------------
# Pricing tables — canonical home is `puzzleeval.telemetry.pricing_tables`.
# ---------------------------------------------------------------------------
# Re-exported here so legacy callers (`from puzzleeval.config import
# MODEL_PRICING`) keep working. New code should import directly from
# `puzzleeval.telemetry` or `puzzleeval.telemetry.pricing_tables`.
#
# To update pricing, edit `puzzleeval/telemetry/pricing_tables.py` — that
# is the single source of truth. The CI guard
# `tests/test_pricing_table_completeness.py` asserts every model name
# referenced in the codebase has an entry in the canonical table.
# ---------------------------------------------------------------------------
from puzzleeval.telemetry.pricing_tables import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER_1H,
    CACHE_WRITE_MULTIPLIER_5M,
    MODEL_PRICING,
    WEB_SEARCH_PRICE_PER_SEARCH,
)


# ---------------------------------------------------------------------------
# Agent 2 Dual Search (Phase 4)
# ---------------------------------------------------------------------------
# When Agent 1 produces a multi-scope WorkflowBlueprint, Agent 2 runs DUAL
# search: ONE all-in-one survey search that looks for tools covering the
# entire workflow (Zapier, n8n, etc.) AND ONE per-scope search per step in
# the blueprint (so specialists at each scope surface alongside the
# all-in-ones). Every candidate carries a `covers_step_ids: frozenset[str]`
# claim plus a `coverage_confidence: dict[str, "claimed"]` tag — Agent 2
# never verifies, only records what search snippets claim. Phase 6.5's
# deep-verify upgrades `"claimed"` → `"verified"` per scope or drops the
# scope from `covers_step_ids`.
#
# When disabled: single-pass search (today's behavior); every candidate
# gets an empty `covers_step_ids` and empty `coverage_confidence` so
# downstream falls back to the flat flow.
#
# 1-scope blueprints and blueprint=None runs always take the single-pass
# path regardless of this flag — dual search only activates for N>=2 steps.
# ---------------------------------------------------------------------------
RESEARCH_DUAL_SEARCH_ENABLED = (
    os.environ.get("PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED", "1") != "0"
)


# ---------------------------------------------------------------------------
# User Candidate Selection (Phase 6)
# ---------------------------------------------------------------------------
# After Agent 2 emits a ranked candidate pool with claimed covers_step_ids,
# the pipeline pauses and waits for the user to:
#   1. Pick which candidates to test at each scope (keep/remove per-scope)
#   2. Optionally add custom providers with explicit covers_step_ids
#
# API path: pipeline emits `selection_required` SSE event; frontend renders
# SelectionPanel; user POSTs to `/runs/{id}/select-candidates`; pipeline
# resumes on filtered candidate list.
#
# CLI path: prints per-scope candidate tables to stderr; prompts stdin per
# scope (y = keep all, n = drop all, edit = toggle by index). Skipped when
# `--no-interactive` is set.
#
# When disabled: pause is skipped, pipeline auto-runs with every Agent 2
# candidate tested at every scope it claims to cover (today's behavior
# pre-Phase-6). Useful for scripted nightly runs.
# ---------------------------------------------------------------------------
USER_SELECTION_ENABLED = (
    os.environ.get("PUZZLEEVAL_USER_SELECTION_ENABLED", "1") != "0"
)


# ---------------------------------------------------------------------------
# Agent 4 Deep-Verify flags — REMOVED.
# ---------------------------------------------------------------------------
# The deep-verify path (full atlas extraction in Agent 4) was removed
# along with its supporting modules (``deep_verify_runner.py``,
# ``provider_atlas.py``, ``deep_verify_prompt.py``, ``manual_atlas.py``).
# Rationale: Agent 4's atlas extraction and Agent 5's build-oriented
# research have different goals (structured provider surface vs
# build-ready api_spec.txt). Forcing both into Agent 4 produced
# atlases Agent 5 couldn't consume efficiently, leading to 20+ turn
# build loops where the builder re-read SDK source to rediscover
# what the atlas had in the wrong shape.
#
# Current split:
#   Agent 4: shallow verify (screening.py) — confirm public API exists,
#            emit auth_method + verified_api_docs_url + access_method
#   Agent 5: Phase-1 research (implement_test_env.py BUILDER_SYSTEM_PROMPT)
#            — Sonnet reads docs, writes api_spec.txt, then switches to
#            Opus for build.


# ---------------------------------------------------------------------------
# Per-Scope Candidate Selection (Phase 7)
# ---------------------------------------------------------------------------
SCOPE_SELECTION_WEIGHTS = {
    "user_picked_here": 0.40,
    "credentials": 0.20,
    "relevance_at_scope": 0.20,
    "docs_quality": 0.10,
    "pricing_fit": 0.10,
}
SCOPE_CANDIDATES_CAP_BY_PLAN = {
    "free": 3,
    "paid": 5,
    "enterprise": 10,
}

# ---------------------------------------------------------------------------
# Per-Scope Test Execution Mode (Phase 9)
# ---------------------------------------------------------------------------
SCOPE_TEST_MODE = os.environ.get("PUZZLEEVAL_SCOPE_TEST_MODE", "1") != "0"


# ---------------------------------------------------------------------------
# Web Fetch Fallback (Phase 1: Cloudflare hardening)
# ---------------------------------------------------------------------------
# Anthropic's server-side web_fetch tool sometimes hits 403/Cloudflare blocks
# (url_not_accessible) or 429 rate limits (too_many_requests). The model has
# already seen the error inside its current turn — but we can guide the NEXT
# turn with a fallback message that suggests web_search alternatives,
# GitHub SDK lookups, or alternate docs URLs.
#
# When True (default): detect blocked fetches per turn, log counts, inject
# fallback guidance for Agent 5's next turn, and apply a backoff sleep on 429.
# When False: skip detection entirely (legacy behavior).
#
# Used by puzzleeval/web_fetch_fallback.py and Agents 4 / 5.
# ---------------------------------------------------------------------------
ENABLE_FETCH_FALLBACK = os.environ.get("PUZZLEEVAL_ENABLE_FETCH_FALLBACK", "1") != "0"
FETCH_RATE_LIMIT_BACKOFF_SECONDS = int(
    os.environ.get("PUZZLEEVAL_FETCH_RATE_LIMIT_BACKOFF", "5")
)


# ---------------------------------------------------------------------------
# Agent 5: Implement Test Env Configuration
# ---------------------------------------------------------------------------
# Agent 5 builds test harnesses for each validated candidate. Each candidate
# gets an autonomous builder agent that reads API docs, writes code, tests it,
# and fixes errors iteratively.
#
# AGENT5_BUILDER_MODEL: Sonnet 4.6 (same as research/screening) — handles
#   web content well and produces good code. Same price as Sonnet 4.5.
# AGENT5_MAX_TURNS: 15 is enough for: read docs (2-3) + write code (1) +
#   test + fix cycles (2-3 iterations) with margin for complex APIs.
# AGENT5_MAX_BUDGET_PER_CANDIDATE: $3 covers ~15 turns of web fetch +
#   code generation. Most candidates finish in $1-2.
# AGENT5_MAX_OUTPUT_TOKENS: 8192 — code generation needs more output tokens
#   than the default 4096 (a full harness.py + requirements.txt can be 2-3K tokens).
# ---------------------------------------------------------------------------
AGENT5_BUILDER_MODEL = os.environ.get("PUZZLEEVAL_BUILDER_MODEL", "claude-opus-4-7")
# AGENT5_MAX_TURNS: upper bound on the outer loop. The real stop signal is
# adaptive progress tracking (diminishing returns). This is just the worst-case
# ceiling — legitimately complex builds (voice WebSocket SDK with session
# state, OAuth flows, multi-endpoint pipelines) can legitimately need 25-35
# turns. Prior 25 cap was killing Phase 2 debug cycles on complex SDKs.
AGENT5_MAX_TURNS = int(os.environ.get("PUZZLEEVAL_AGENT5_MAX_TURNS", "40"))
# DIMINISHING_RETURNS_WINDOW: if N consecutive turns produce ZERO file
# writes AND ZERO new research findings, we're stuck — inject a wrap-up
# nudge. Not a hard stop: the agent can still write code to recover. Pattern
# adapted from Claude Code's query/tokenBudget.ts diminishing-returns detector.
AGENT5_DIMINISHING_RETURNS_WINDOW = int(
    os.environ.get("PUZZLEEVAL_AGENT5_DIMINISHING_WINDOW", "3")
)
# MAX_REASSESSMENT_TIERS: hard cap on STRATEGIC_PIVOT escalations. After
# N tier escalations with no progress, the loop accepts that this candidate
# can't be built and emits a FailedHarness cleanly. Without this cap the
# reassessment counter could grow unbounded on pathologically broken APIs.
AGENT5_MAX_REASSESSMENT_TIERS = int(
    os.environ.get("PUZZLEEVAL_AGENT5_MAX_REASSESSMENT_TIERS", "4")
)
# Gate C — patch-fragmentation nudge. When the last 2 real turns were BOTH
# small (<= AGENT5_PATCH_FRAGMENT_TOKEN_CEILING output tokens each) AND BOTH
# applied a single `patch_file` call to the SAME file, inject a one-time
# soft nudge suggesting parallel edits for the next bug on that file.
# Runtime-only (not prompt-static) — fires AT MOST once per file per build
# so it can't spam the loop. Set the flag to "0" to disable the whole
# gate; set the ceiling to "0" to effectively disable it while keeping
# the plumbing hot for A/B tests. Informed by real-run evidence: voice
# builds regularly show 4-5 serial single-line patches to harness.py
# that could have been one parallel turn — ~$0.20-0.40 per build
# lost to round-trip overhead. See plan §4.2.
AGENT5_PATCH_FRAGMENT_NUDGE_ENABLED = (
    os.environ.get("PUZZLEEVAL_AGENT5_PATCH_FRAGMENT_NUDGE", "1") == "1"
)
AGENT5_PATCH_FRAGMENT_TOKEN_CEILING = int(
    os.environ.get("PUZZLEEVAL_AGENT5_PATCH_FRAGMENT_TOKEN_CEILING", "600")
)

# Agent 5 builder write_file gates (B1, B2, B3) — soft-by-default per AD-007.
# Each gate has an env-var bypass so operators can disable in real runs if a
# false-positive blocks legitimate work. All default ON.
#
#   * GATE_FORBIDDEN_FILENAMES — REJECT_TOOL_CALL on meta-files like NOTES.md.
#     Builder receives a tool error in the next turn and adapts (rename to a
#     canonical file or store the content in api_spec.txt).
#   * GATE_INTROSPECTION_WARN — WARN-only (log + allow) when the builder
#     writes inspect_/check_/explore_/probe_*.py BEFORE harness.py exists.
#     Observability for fragmented-probing antipattern; doesn't block.
#   * GATE_PHASE1_SCAFFOLD_BLOCK — REJECT_TOOL_CALL when the builder writes
#     scaffold files (harness.py, smoke_test.py, live_test.py,
#     requirements.txt) while api_spec_written is False. Phase-keyed (NOT
#     model-keyed) so model-fallback ladders can't trigger false rejects.
GATE_FORBIDDEN_FILENAMES_ENABLED = (
    os.environ.get("PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES", "1") != "0"
)
GATE_INTROSPECTION_WARN_ENABLED = (
    os.environ.get("PUZZLEEVAL_GATE_INTROSPECTION_WARN", "1") != "0"
)
GATE_PHASE1_SCAFFOLD_BLOCK_ENABLED = (
    os.environ.get("PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK", "1") != "0"
)

# Gate B4: pre-spec research budget. Counts web_search + web_fetch +
# ask_research uses while api_spec_written is False; injects a user message
# before the next API call when the count crosses the budget. Soft —
# the message lets the builder adapt (write spec with TODOs OR call
# advisor for a tier-up); doesn't halt the build.
GATE_PRESPEC_RESEARCH_BUDGET_ENABLED = (
    os.environ.get("PUZZLEEVAL_GATE_PRESPEC_RESEARCH_BUDGET", "1") != "0"
)
GATE_PRESPEC_RESEARCH_BUDGET = int(
    os.environ.get("PUZZLEEVAL_GATE_PRESPEC_RESEARCH_BUDGET_COUNT", "2")
)
AGENT5_MAX_BUDGET_PER_CANDIDATE = float(
    os.environ.get("PUZZLEEVAL_AGENT5_BUDGET_PER_CANDIDATE", "3.0")
)
AGENT5_MAX_BUDGET_TOTAL = float(
    os.environ.get("PUZZLEEVAL_AGENT5_BUDGET_TOTAL", "20.0")
)
AGENT5_MAX_PARALLEL = int(os.environ.get("PUZZLEEVAL_AGENT5_MAX_PARALLEL", "5"))
AGENT5_CODE_TIMEOUT = int(os.environ.get("PUZZLEEVAL_AGENT5_CODE_TIMEOUT", "120"))
AGENT5_MAX_OUTPUT_TOKENS = int(
    # 24K — raised from 16K after a real-run trace (71734f9d) showed
    # Turn 0 of ElevenLabs hitting stop_reason=max_tokens even with
    # 16K budget, losing $0.68 to response truncation. Root cause:
    # Sonnet 4.6 with adaptive thinking + 3 web_fetch results at 15K
    # max_content_tokens each + write_file emission can legitimately
    # need >16K output tokens on the first Phase 1 turn. Both Sonnet
    # 4.6 and Opus 4.7 support up to 32K output; 24K is the safe
    # middle that prevents truncation without the cost ceiling of 32K.
    # Combined with ``web_fetch.max_content_tokens=10000`` (tightened
    # this pass), the Phase 1 turn always fits comfortably.
    os.environ.get("PUZZLEEVAL_AGENT5_MAX_TOKENS", "24000")
)
AGENT5_MAX_VERIFICATION_RETRIES = int(
    os.environ.get("PUZZLEEVAL_AGENT5_MAX_VERIFICATION_RETRIES", "2")
)
AGENT5_MAX_CANDIDATES = int(
    os.environ.get("PUZZLEEVAL_AGENT5_MAX_CANDIDATES", "4")
)  # Top N by user-fit score: 3 for comparison + 1 buffer for build failures


# ---------------------------------------------------------------------------
# Test Execution Configuration (used by Agent 5 post-build test runner)
# ---------------------------------------------------------------------------
# Agent 5 executes test cases through built harnesses and evaluates results.
# Mostly mechanical (subprocess calls) with one LLM call per candidate for
# quality evaluation.
# ---------------------------------------------------------------------------
AGENT6_EVAL_MODEL = os.environ.get("PUZZLEEVAL_AGENT6_EVAL_MODEL", DEFAULT_MODEL)
# AGENT6_TEST_TIMEOUT = per-test-case subprocess timeout in seconds.
# Same scaling rule as AGENT5_CODE_TIMEOUT: 120s baseline for sync APIs;
# long-running operations (any provider whose atlas reports async_polling
# or batch_file) automatically scale to AGENT6_TEST_TIMEOUT_LONG.
AGENT6_TEST_TIMEOUT = int(
    os.environ.get("PUZZLEEVAL_AGENT6_TEST_TIMEOUT", "120")
)
AGENT6_TEST_TIMEOUT_LONG = int(
    os.environ.get("PUZZLEEVAL_AGENT6_TEST_TIMEOUT_LONG", "600")
)
AGENT6_RATE_LIMIT_BACKOFF = int(
    os.environ.get("PUZZLEEVAL_AGENT6_RATE_LIMIT_BACKOFF", "3")
)  # Seconds to wait on rate limit before retry
AGENT6_EVAL_MAX_TOKENS = int(
    # Real run 045bbd10 (2026-04-21) exposed that 4096 tokens is too
    # small when the evaluator batches 8 test cases AND uses adaptive
    # thinking (thinking tokens are a subset of max_tokens). Adaptive
    # thinking burns ~1-3K tokens; batch JSON for 8 tests × 3-5
    # criteria × ~50 tokens each = 1-2K more. Total needed ~3-5K for
    # JSON alone, which exceeds the remaining budget after thinking.
    # Real symptom: `EOF while parsing a string at column 5471` (JSON
    # truncated mid-string) → retry also truncates → terminal fail
    # → all evaluations return empty → every test scored 0/100.
    # Bumped to 16000 which matches AGENT5_MAX_OUTPUT_TOKENS and
    # gives comfortable headroom. max_tokens is a ceiling — actual
    # usage stays low for small batches, so this has no cost impact
    # when not needed.
    os.environ.get("PUZZLEEVAL_AGENT6_EVAL_MAX_TOKENS", "16000")
)
AGENT6_PASS_THRESHOLD = float(
    os.environ.get("PUZZLEEVAL_AGENT6_PASS_THRESHOLD", "0.5")
)
AGENT6_ERROR_ABORT_THRESHOLD = float(
    os.environ.get("PUZZLEEVAL_AGENT6_ERROR_ABORT_THRESHOLD", "0.5")
)
AGENT6_MIN_TESTS_BEFORE_ABORT = int(
    os.environ.get("PUZZLEEVAL_AGENT6_MIN_TESTS_BEFORE_ABORT", "5")
)

# AGENT6_PER_CANDIDATE_PARALLELISM controls how many SINGLE-TURN test
# cases for ONE candidate run concurrently. Default 6 — chosen as the
# CEILING below the ElevenLabs Conversational AI Starter pack's
# concurrent-session cap (the tightest paid tier we currently exercise).
# Cloud-migration safe: this is ThreadPoolExecutor INSIDE a single
# container (no cross-container coordination), so Docker / Cloud Run /
# managed-sandbox migrations apply the same parallelism per container.
#
# Going above 6 on Starter would trigger ElevenLabs's 429 / "session
# limit exceeded" path on every voice run — the AGENT6_SESSION_RETRY_BACKOFF
# path catches these gracefully but at the cost of 5-35s of retry
# backoff per overflow.
#
# Why 6 is safe AND productive:
#   - For Agent 3's typical 7-test batch, this runs 6 in parallel
#     batch 1 + 1 in batch 2 (plus background audio merge from item 5
#     overlapping batch 2's conversation). Batch 1 wall-clock = max
#     conversation in batch (~135s); batch 2 wall-clock = single test
#     (~135s). Total ≈ 270s vs the prior 390s of 3 sequential batches
#     of 3 — saves ~2 min on the test phase.
#   - For ≥7-test batches, the background audio merge (item 5,
#     voice_realtime._merge_conversation_audio submitted to a daemon
#     pool) frees workers at conversation-end, not merge-end — so
#     batch 2 starts ~70s sooner than sync-merge would allow.
#   - The rate_limiter (puzzleeval/rate_limiter.py) is wired into
#     _execute_all_tests at implement_test_env.py:7617. Every test
#     call goes through `rate_limiter.acquire(candidate, upstream)`
#     BEFORE hitting the provider API. acquire() SLEEPS until a
#     token is available — pure back-pressure, no errors raised.
#   - Default DEFAULT_RPS=2 per candidate means the EFFECTIVE
#     parallelism is capped at ~2 RPS regardless of how many threads
#     are queued. 6-parallel just means we have 6 threads waiting in
#     the bucket queue instead of 3.
#
# AGENT6_PER_CANDIDATE_SESSION_PARALLELISM controls how many MULTI-TURN
# tests (conversation, voice_conversation, voice_turn) run concurrently
# for ONE candidate. Default 6 — same Starter-pack concurrent-session
# ceiling as single-turn. Each multi-turn test opens a separate provider
# session (WebSocket connection / conversation_id / session handle) that
# can run 60-135s. ElevenLabs Starter's concurrent-session cap is 6 per
# workspace; OpenAI Realtime tier-1 handles 6× concurrent WebSockets
# fine.
#
# If a tighter free-tier provider's session cap proves below 6, the
# AGENT6_SESSION_RETRY_BACKOFF retry path absorbs the surge — the
# harness waits for a prior session to release and retries. With
# backoff base=5s and max=3 retries, worst case is slower wall-clock,
# never test failure.
#
# Cloud-migration safety: both knobs scale cleanly across cloud
# deployment stages (Docker per candidate, Cloud Run per candidate,
# managed sandboxes). Each one is ThreadPoolExecutor INSIDE a single
# container / job. No cross-candidate coordination, no process-level
# shared state, no filesystem racing. "Lift and shift" to cloud works
# with no redesign — the same parallelism bounds apply at the
# per-container level whether we run locally or in the cloud.
#
# If you hit "concurrent session exceeded" errors on free-tier voice
# providers AND the retry path proves insufficient, dial back via env:
#   PUZZLEEVAL_AGENT6_PER_CANDIDATE_SESSION_PARALLELISM=3 (or 1 for
#   fully sequential multi-turn). Same env knob exists for single-turn.
# To raise above 6 on a paid-tier upgrade (Pro / Scale on ElevenLabs,
# tier-3+ on OpenAI), set PUZZLEEVAL_AGENT6_PER_CANDIDATE_PARALLELISM=N.
AGENT6_PER_CANDIDATE_PARALLELISM = int(
    os.environ.get("PUZZLEEVAL_AGENT6_PER_CANDIDATE_PARALLELISM", "6")
)
AGENT6_PER_CANDIDATE_SESSION_PARALLELISM = int(
    os.environ.get("PUZZLEEVAL_AGENT6_PER_CANDIDATE_SESSION_PARALLELISM", "6")
)

# AGENT6_SESSION_RETRY_BACKOFF_BASE / MAX_RETRIES — wait-and-retry
# behavior when a provider returns a "concurrent session limit
# exceeded" error.
#
# When per-candidate session parallelism (default 3) runs more tests
# than the provider allows (free tiers commonly cap at 1), the harness
# call fails. Instead of marking the test as errored, we wait for a
# prior session to release and retry. This preserves result quality
# AT THE COST of wall-clock time — but wall-clock was the whole point
# of parallelism, so the user-facing behavior is "parallel if the
# provider supports it, graceful serialization if it doesn't."
#
# Backoff shape: exponential — base × 2^(attempt-1). With base=5 and
# max=3 retries, waits are 5s → 10s → 20s (35s total before giving
# up). Typical multi-turn conversation takes 15-30s, so by the third
# retry a prior session has almost certainly released.
#
# Rate-limit retries (_is_rate_limit_error path) use shorter backoff
# (3s, fixed) because rate limits reset faster. Session caps release
# on conversation-completion timescales, so sleeps are longer.
AGENT6_SESSION_RETRY_BACKOFF_BASE = int(
    os.environ.get("PUZZLEEVAL_AGENT6_SESSION_RETRY_BACKOFF_BASE", "5")
)
AGENT6_SESSION_MAX_RETRIES = int(
    os.environ.get("PUZZLEEVAL_AGENT6_SESSION_MAX_RETRIES", "3")
)

# REPORT_MAX_EVIDENCE_PER_KIND controls how many per-candidate
# TestEvidence entries (failures + successes, separately) get packed
# into the final EvaluationReport. The frontend (EvaluationReportCard)
# renders EVERY entry it receives inside expandable <details> blocks —
# it does NOT paginate — so this cap is the end-to-end visibility
# ceiling.
#
# Earlier default was 3 "representative" failures + 3 successes, which
# was fine when typical evals ran 5-10 tests per candidate. With
# Agent 3 now generating 10-50 tests per run, 3 leaves 70-90% of
# results invisible. Default bumped to 50 — covers 99% of real runs
# entirely, and the frontend's <details> accordion keeps the UI tidy
# even at that size (users expand what they want to inspect).
#
# Raise this if your eval runs typically exceed 50 tests per candidate
# (e.g., for long-running benchmark scenarios). Lower it if payload
# size in the evaluation_report SSE event becomes a problem (each
# TestEvidence is a few KB with transcript + audio_paths).
REPORT_MAX_EVIDENCE_PER_KIND = int(
    os.environ.get("PUZZLEEVAL_REPORT_MAX_EVIDENCE_PER_KIND", "50")
)


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------
# Path to the centralized API key registry file (JSON). Used by Agent 5 for
# live validation and test execution. If the file doesn't exist,
# the pipeline falls back to checking environment variables.
#
# Override with: export PUZZLEEVAL_PROVIDER_REGISTRY="/path/to/keys.json"
# ---------------------------------------------------------------------------
PROVIDER_REGISTRY_PATH = os.environ.get(
    "PUZZLEEVAL_PROVIDER_REGISTRY", "provider_registry.json"
)


# ---------------------------------------------------------------------------
# Rate limiting (Gap 13 + Gap 25)
# ---------------------------------------------------------------------------
# Free-tier providers (Mindee, Veryfi, Klippa, Nanonets) will throttle or
# block a test harness that fires without pacing. Shared upstream LLMs
# (OpenAI, Anthropic) will do the same when several candidates wrap the
# same upstream.
#
# The limiter (puzzleeval/rate_limiter.py) runs in two layers:
#   1. Per-candidate token bucket — respects each candidate's own docs.
#   2. Per-upstream-provider global bucket — groups candidates that wrap
#      the same LLM and caps shared throughput.
#
# Both default to ON. Disable with PUZZLEEVAL_RATE_LIMIT_ENABLED=0 for
# diagnosis (tests will then run as fast as they can and may fail on
# throttled free tiers).
# ---------------------------------------------------------------------------
RATE_LIMIT_ENABLED = os.environ.get(
    "PUZZLEEVAL_RATE_LIMIT_ENABLED", "1"
).lower() not in ("0", "false", "no")

DEFAULT_RPS = float(os.environ.get("PUZZLEEVAL_DEFAULT_RPS", "2.0"))
UPSTREAM_RPS = float(os.environ.get("PUZZLEEVAL_UPSTREAM_RPS", "5.0"))

# ---------------------------------------------------------------------------
# Adversarial verification (Gap E — Claude Code-style verification agent)
# ---------------------------------------------------------------------------
# After Agent 5 builds a harness and signals HARNESS_COMPLETE on smoke +
# one live call, run a battery of adversarial probes (empty input, max
# input, malformed input, idempotency, concurrency, auth-error). When the
# battery surfaces a critical failure (crash or silent corruption), the
# harness is marked NOT READY and Agent 3 test cases skip it.
#
# Disable via PUZZLEEVAL_ADVERSARIAL_PROBES_ENABLED=0 to fall back to the
# legacy behavior (smoke-test-only verification).
# ---------------------------------------------------------------------------
ADVERSARIAL_PROBES_ENABLED = os.environ.get(
    "PUZZLEEVAL_ADVERSARIAL_PROBES_ENABLED", "1"
).lower() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Agent 5 build-failure fallback (Q4 closure)
# ---------------------------------------------------------------------------
# When EVERY selected candidate fails to produce a working harness, the
# pipeline used to surface a hard failure ("Zero harnesses built — pipeline
# cannot continue"). The user is left with no testable environment.
#
# With AGENT5_FALLBACK_ENABLED=1 (default), Agent 5 instead pulls
# AGENT5_FALLBACK_MAX additional candidates from Agent 4's verified pool
# (those NOT in the user's pick list, ranked by relevance + adoption_difficulty)
# and tries to build them. The resulting harnesses are tagged was_fallback=True.
#
# Disable for diagnosis or when reproducibility matters more than guarantee.
# ---------------------------------------------------------------------------
AGENT5_FALLBACK_ENABLED = os.environ.get(
    "PUZZLEEVAL_AGENT5_FALLBACK_ENABLED", "1"
).lower() not in ("0", "false", "no")

AGENT5_FALLBACK_MAX = int(
    os.environ.get("PUZZLEEVAL_AGENT5_FALLBACK_MAX", "3")
)


# ---------------------------------------------------------------------------
# Adaptive thinking effort (Anthropic API output_config.effort)
# ---------------------------------------------------------------------------
# Soft guidance for how much extended thinking Claude does per request.
# Applies to every agent that calls Claude with `thinking={"type": "adaptive"}`.
#
# Levels (from Anthropic docs):
#   low    - skip thinking on simple queries, prioritize latency
#   medium - moderate thinking, may skip for very simple cases
#   high   - always think, deep reasoning on complex tasks
#   xhigh  - deeper exploration, available on Opus 4.7
#   max    - no constraint on thinking depth, available on Opus 4.7+
#
# Default: medium (lowered from `high` in PLAN_VOICE_RUN_OPTIMIZATIONS.md
# item 4). Empirical evidence from real run trace 73a9d605
# (conversation_log.json): Opus build turns under EFFORT=high were
# spending substantial budget on extended thinking that produced no
# visible work — e.g., ElevenLabs T2 Opus turn cost $0.97 with 262
# output tokens, 0 visible text, 0 tools called. Adaptive thinking
# auto-tunes UPWARD when the model hits a complex decision point, so
# `medium` is a FLOOR, not a cap — hard decisions still get the
# reasoning depth they need, routine tool execution doesn't burn
# budget on it.
#
# Saves an estimated 30-60s per slow build (per-turn thinking budget
# halved on routine turns). Compounds across 8-11 turn ElevenLabs-style
# builds.
#
# Override via env to revert: PUZZLEEVAL_EFFORT=high
# When unset (empty string), no effort is sent and the API uses its model
# default.
# ---------------------------------------------------------------------------
EFFORT = os.environ.get("PUZZLEEVAL_EFFORT", "medium").lower().strip()
_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max", ""}
if EFFORT not in _VALID_EFFORTS:
    # Unknown value — log and reset to default so downstream API calls don't
    # fail with a 400. We don't raise because config import shouldn't crash.
    import sys as _sys
    print(
        f"warning: PUZZLEEVAL_EFFORT={EFFORT!r} not in "
        f"{sorted(_VALID_EFFORTS)} — defaulting to 'medium'",
        file=_sys.stderr,
    )
    EFFORT = "medium"


def output_config_for_request() -> dict | None:
    """Build the `output_config` dict to pass to client.messages.create.

    Returns None when EFFORT is unset (don't send the field at all so the
    API's default applies). Otherwise returns ``{"effort": EFFORT}``.
    """
    if not EFFORT:
        return None
    return {"effort": EFFORT}


# ---------------------------------------------------------------------------
# Hybrid evaluator (Claude-picked plugins for ambiguous modalities)
# ---------------------------------------------------------------------------
# When the deterministic dispatch in _run_tests_for_candidate yields no
# plugin (modality is ambiguous, output is generic free_text but might
# benefit from plugin inspection), this flag enables a SECOND-LOOK pass:
# expose all plugins as Claude-callable tools + adaptive thinking, let
# Claude decide whether any plugin would sharpen the verdict.
#
# Off by default — opt in when you want extra precision for ambiguous
# modalities at the cost of non-determinism in the fallback path.
# Modality-clear cases (audio→audio, code→code, etc.) remain deterministic
# regardless of this flag.
# ---------------------------------------------------------------------------
HYBRID_EVAL_ENABLED = os.environ.get(
    "PUZZLEEVAL_HYBRID_EVAL_ENABLED", "0"
).lower() not in ("0", "false", "no", "")


# ---------------------------------------------------------------------------
# Evaluation dispatch strategy (Agent 5 per-test scoring)
# ---------------------------------------------------------------------------
# The path Agent 5 uses to pick which plugin(s) score a given test case.
#
#   "tool_runner" (default) — Plugins are exposed as ``@beta_tool`` functions
#       to Claude via ``client.beta.messages.tool_runner``. Claude reads the
#       test case + response, picks the right plugin(s), chains them across
#       iterations when multiple are needed, and emits a structured
#       ``ScoreVerdict`` as the final message. The SDK handles the tool-use
#       loop and `output_format` enforces the schema.
#
#       This is the architecturally-correct path per Anthropic's docs: it
#       fixes the "deterministic picked 1 but 3 were needed" gap, the
#       "Agent 3 mis-labeled the modality" gap, the "ambiguous enum tiebreak"
#       gap, AND scales cleanly past 30+ plugins via `tool_search_tool`.
#
#       Non-deterministic by design — Claude's selection varies slightly
#       across runs. Borderline scores may wobble by ±0.02; rank ordering
#       stays stable.
#
#   "deterministic" — Legacy enum-based dispatch. For emergency bisection
#       only. modality.py picks one plugin per (input_type, output_type)
#       pair. Reproducible but has the coverage gaps that motivated C.
#
#   "hybrid" — Deterministic first; if the chosen plugin returns
#       ``fallback_reason`` OR no plugin matches, fall back to tool_runner.
#       Middle ground: reproducible for clean cases, intelligent for edges.
# ---------------------------------------------------------------------------
EVAL_STRATEGY = os.environ.get(
    "PUZZLEEVAL_EVAL_STRATEGY", "tool_runner"
).strip().lower()
if EVAL_STRATEGY not in {"tool_runner", "deterministic", "hybrid"}:
    EVAL_STRATEGY = "tool_runner"

# When the plugin count reaches this threshold, we switch from "load all
# tools every call" to "load tool_search_tool + defer_loading on plugins"
# so context stays cheap. Anthropic's published tool-selection accuracy
# threshold sits around 30-50 tools; we switch earlier (conservative).
# Set to 0 to always use tool_search_tool; set to 999 to never use it.
EVAL_TOOL_SEARCH_THRESHOLD = int(
    os.environ.get("PUZZLEEVAL_EVAL_TOOL_SEARCH_THRESHOLD", "15")
)

# Cap on tool_runner iterations per test case. One iteration = one Claude
# API call + any tools it invokes that turn. With chaining, 4 is usually
# enough for the "need N tools" case to converge. Raise for pathological
# multi-modal tests, lower for speed.
EVAL_MAX_ITERATIONS = int(
    os.environ.get("PUZZLEEVAL_EVAL_MAX_ITERATIONS", "5")
)

# When enabled, plugin tools are marked ``allowed_callers=["direct",
# "code_execution_20260120"]`` and a code_execution tool is added so
# Claude can write one Python script that chains multiple plugins in a
# single container — intermediate tool results don't enter the model's
# context. Specifically closes the "need 3 tools, got 1" coverage gap
# without N separate API round-trips. Claude decides per-test whether
# to use programmatic mode; single-tool cases still invoke directly.
# On by default — docs recommend this path for multi-modal scoring.
# Flip to 0 for a pure direct-dispatch evaluation path (emergency
# rollback + regression bisection).
EVAL_PROGRAMMATIC_CHAINING_ENABLED = os.environ.get(
    "PUZZLEEVAL_EVAL_PROGRAMMATIC_CHAINING", "1"
).lower() not in ("0", "false", "no", "")


# ---------------------------------------------------------------------------
# Programmatic tool calling (Agent 5 builder)
# ---------------------------------------------------------------------------
# When enabled, the Agent 5 builder loop adds `code_execution_20260120` to
# its tool list and marks write_file/patch_file/run_code/read_file as
# `allowed_callers=["direct", "code_execution_20260120"]` — Claude can
# write Python that chains tool calls in a single container instead of
# sampling between every call. Estimated savings: 30-50% on builder cost
# and latency for multi-step builds.
#
# Off by default while we soak this on real builds; flip to 1 for the
# faster path. Requires Opus 4.7 or Sonnet 4.6 (which we already use).
# ---------------------------------------------------------------------------
PROGRAMMATIC_TOOLS_ENABLED = os.environ.get(
    "PUZZLEEVAL_PROGRAMMATIC_TOOLS", "0"
).lower() not in ("0", "false", "no", "")


# ---------------------------------------------------------------------------
# Agent 5 message-level prompt caching (cache-the-growing-conversation)
# ---------------------------------------------------------------------------
# When enabled (default), Agent 5's builder loop places a `cache_control`
# breakpoint on the last content block of the last message before each
# `client.beta.messages.create()` call. This caches the growing
# conversation prefix, so turns 2+ read prior messages at 0.1x base cost
# instead of full price.
#
# Docs reference: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
#   "For Multi-Turn Conversations ... use automatic caching" — we use
#   explicit block-level placement which is equivalent and more
#   predictable.
#
# Combined with the existing system-block cache (10.7K tokens) and the
# `clear_at_least: 10000` guard on context_management.edits, this gives
# a 40-60% input-cost reduction on 15-25 turn Agent 5 builds at Opus
# rates with zero risk of paying more than current baseline — worst
# case (every cache miss) matches pre-change cost.
#
# Flip to 0 if a future Anthropic change makes this regressive, or
# during bisection if cache-related errors surface in real runs.
# ---------------------------------------------------------------------------
CACHE_MESSAGES_ENABLED = os.environ.get(
    "PUZZLEEVAL_CACHE_MESSAGES_ENABLED", "1"
).lower() not in ("0", "false", "no", "")


# Minimum cleared-tokens threshold for `clear_tool_uses_20250919` to fire.
# Below this, the cache-invalidation cost from clearing exceeds the
# cache-read savings on the remaining prefix. Per docs: "clear enough
# tokens to make the cache invalidation worthwhile. Use the
# `clear_at_least` parameter." 10K is a conservative breakeven: cache
# write is 1.25x base, cache read is 0.1x base, so clearing saves
# roughly 0.9x per cleared token on future reads; 10K cleared ensures
# we save ~$0.04 at Opus rates which comfortably exceeds the one-time
# rewrite cost.
CACHE_CLEAR_AT_LEAST_TOKENS = int(
    os.environ.get("PUZZLEEVAL_CACHE_CLEAR_AT_LEAST", "10000")
)


# Trigger threshold for `clear_tool_uses_20250919` to fire on Agent 5
# builds. Raised from 80K → 120K based on real-run trace 8ded6706
# (2026-04-25) analysis: at 80K, the edit fires at turn 7-9 in a
# typical 12-turn build, costing ~$0.30 per fire in cache_create
# while only saving ~$0.20 in subsequent cache_reads (only 3-5
# remaining turns benefit from the smaller prefix). Net loss
# ~$0.10-0.15 per build × 2 fires per candidate × 2 candidates =
# ~$0.40-0.60 wasted per voice run.
#
# 120K threshold delays the edit to the END of typical builds where
# it doesn't fire at all (12-turn build's context grows to ~140K so
# rarely crosses 120K). For pathological 25-turn debug-heavy builds
# the edit still fires and provides real value (savings horizon long
# enough to amortize the rewrite cost).
#
# Safety: Opus context window is 200K, `compact_20260112` triggers
# at 150K. 120K → 150K → 200K leaves headroom for both clear_tool_uses
# and the compact safety net.
CACHE_CLEAR_TOOL_USES_TRIGGER = int(
    os.environ.get("PUZZLEEVAL_CACHE_CLEAR_TRIGGER", "120000")
)


# MIN_CACHEABLE_TOKENS — canonical home is puzzleeval.telemetry.pricing_tables.
# Re-exported for back-compat with existing call sites.
from puzzleeval.telemetry.pricing_tables import MIN_CACHEABLE_TOKENS  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Agent 3 test-generation sufficiency policy
# ---------------------------------------------------------------------------
# The "is this enough?" decision used to be spread across three sites
# (Agent 3's prompt, the top-up retry's dimension-gap math, the validator's
# shortfall check) with the 6 dimensions + 70% floor + absolute floor 3
# hardcoded in each. Hoisted here so tuning is a one-line change and the
# three consumers can never drift.
#
# These are POLICY, not bandaids — they define what "sufficient test coverage"
# means for every scope regardless of capability / domain / provider. Lift
# them to env-overridable so product tuning doesn't require code edits.
# ---------------------------------------------------------------------------
CANONICAL_COVERAGE_DIMENSIONS: frozenset[str] = frozenset({
    "happy_path",        # works on typical input
    "input_variation",   # different valid forms of input
    "edge_case",         # boundary conditions, edge values
    "scale",             # large input / concurrency / throughput
    "domain_specific",   # domain knowledge (vocabulary, conventions)
    "error_resilience",  # bad input, partial input, recovery
})

# When Agent 3 produces fewer tests than Agent 1's `test_count_target` for
# a scope, the shortfall is an ERROR if `actual < floor(target * RATIO)`.
# 0.7 is the band below which per-scope precision degrades significantly:
# with the default 6-dimension canonical matrix, 70% coverage means at least
# 4 of 6 dimensions are represented on average.
SUFFICIENCY_FLOOR_RATIO = float(
    os.environ.get("PUZZLEEVAL_SUFFICIENCY_FLOOR_RATIO", "0.7")
)

# Even with a small target (say 2), we want at least this many tests before
# we trust the scope's results. 3 covers happy_path / variation / edge_case
# minimally.
SUFFICIENCY_HARD_FLOOR = int(
    os.environ.get("PUZZLEEVAL_SUFFICIENCY_HARD_FLOOR", "3")
)


# ---------------------------------------------------------------------------
# Agent 5 monthly-volume banding for endpoint selection
# ---------------------------------------------------------------------------
# Agent 5's builder reads `user_understanding.constraints.monthly_volume` to
# pick between atomic and batch endpoints. The bands below convert the raw
# number into qualitative guidance the LLM reasons about. These are
# SMB / mid-market / enterprise heuristics — not per-provider carveouts.
#
# Shape: list of (exclusive_upper_bound, label, guidance_hint). Evaluated
# in order; first band whose threshold the volume falls under wins.
# Final entry has threshold=inf to catch everything above.
# ---------------------------------------------------------------------------
MONTHLY_VOLUME_BANDS: list[tuple[float, str, str]] = [
    (100, "LOW",
     "prefer simple / atomic endpoints; batch is over-engineering"),
    (5_000, "MODERATE",
     "atomic usually fine; consider batch if available + test cases imply bulk"),
    (100_000, "HIGH",
     "prefer batch / bulk endpoints when available"),
    (float("inf"), "VERY HIGH",
     "batch + streaming / async + pagination are load-bearing"),
]


def band_monthly_volume(monthly_volume: int | None) -> tuple[str, str]:
    """Convert a raw monthly_volume into (label, guidance_hint).

    Returns ``("UNSPECIFIED", "assume moderate")`` when ``None``.
    Single source of truth so callers don't reinvent the threshold list.
    """
    if monthly_volume is None:
        return "UNSPECIFIED", "not specified — assume moderate"
    for threshold, label, hint in MONTHLY_VOLUME_BANDS:
        if monthly_volume < threshold:
            return label, hint
    # unreachable — last band has inf threshold
    return MONTHLY_VOLUME_BANDS[-1][1], MONTHLY_VOLUME_BANDS[-1][2]
