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
# This is your secret key for calling Claude's API.
# Get one at: https://console.anthropic.com/
# The agent will fail immediately with a clear error if this is not set.
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not ANTHROPIC_API_KEY:
    raise EnvironmentError(
        "ANTHROPIC_API_KEY environment variable is not set. "
        "Get your API key from https://console.anthropic.com/ and set it:\n"
        "  export ANTHROPIC_API_KEY='sk-ant-...'"
    )


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
# Agent 1 Model (promoted in Phase 3)
# ---------------------------------------------------------------------------
# Before Phase 3, Agent 1 was a parser — extract sub-tasks from user text.
# Sonnet 4.6 was sufficient. Phase 3 promotes Agent 1 to a DIRECTOR role:
# decompose the user's demand into an ordered WorkflowBlueprint with
# step ordering, data flow, role assignment, and architecture options
# (all-in-one vs best-per-step). This requires real planning reasoning —
# the kind of task Opus 4.7 is materially better at than Sonnet.
#
# Cost impact: Agent 1 runs in ~1-3 turns with ~4K in + ~0.5-1K out per turn.
# Sonnet -> Opus roughly doubles the Agent 1 cost from ~$0.05 to ~$0.10 per
# evaluation. At the pipeline scale (~$6 total), this is a ~1% rounding error.
#
# Override with: export PUZZLEEVAL_AGENT1_MODEL="claude-sonnet-4-6" to revert.
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
# Model Pricing (USD per token)
# ---------------------------------------------------------------------------
# Used to calculate cost estimates in logs. These prices are from Anthropic's
# pricing page — update them if pricing changes.
# Format: { "model_name": (input_price_per_token, output_price_per_token) }
# ---------------------------------------------------------------------------
MODEL_PRICING = {
    # Opus 4.7: $5 / 1M input, $25 / 1M output
    "claude-opus-4-7": (5.0 / 1_000_000, 25.0 / 1_000_000),
    # Opus 4.5: $5 / 1M input, $25 / 1M output
    "claude-opus-4-5": (5.0 / 1_000_000, 25.0 / 1_000_000),
    # Opus 4.1: $15 / 1M input, $75 / 1M output
    "claude-opus-4-1": (15.0 / 1_000_000, 75.0 / 1_000_000),
    # Sonnet 4.6: $3 / 1M input, $15 / 1M output
    "claude-sonnet-4-6": (3.0 / 1_000_000, 15.0 / 1_000_000),
    # Sonnet 4.5: $3 / 1M input, $15 / 1M output
    "claude-sonnet-4-5-20250929": (3.0 / 1_000_000, 15.0 / 1_000_000),
    # Haiku 4.5: $1 / 1M input, $5 / 1M output
    "claude-haiku-4-5-20251001": (1.0 / 1_000_000, 5.0 / 1_000_000),
}


# ---------------------------------------------------------------------------
# Cache Pricing Multipliers
# ---------------------------------------------------------------------------
# Prompt caching has different write costs depending on TTL:
#   5-min TTL: 1.25x base input price for writes
#   1-hour TTL: 2.00x base input price for writes
#   Cache reads (hits): 0.10x base input price (same for both TTLs)
#
# Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
# ---------------------------------------------------------------------------
CACHE_WRITE_MULTIPLIER_5M = 1.25
CACHE_WRITE_MULTIPLIER_1H = 2.00
CACHE_READ_MULTIPLIER = 0.10


# ---------------------------------------------------------------------------
# Minimum Cacheable Tokens
# ---------------------------------------------------------------------------
# The API silently ignores cache_control if the prefix is below this threshold.
# No error is raised — the request just runs at full price without caching.
#
# This means: our ~800-token system prompt ALONE won't be cached on Opus 4.7.
# Caching only kicks in when total cached prefix (system + file + history)
# exceeds the threshold. A user who uploads a PDF will easily hit it.
# A user with just a short text prompt may not — and that's fine, the cost
# of ~800 uncached tokens is negligible.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Web Search Pricing
# ---------------------------------------------------------------------------
# Anthropic charges $10 per 1,000 web searches = $0.01 per search.
# This is in ADDITION to standard token costs for search-generated content.
# Web fetch has NO additional cost — just standard token costs.
# Used in logging to calculate total cost per Agent 2 run.
# ---------------------------------------------------------------------------
WEB_SEARCH_PRICE_PER_SEARCH = 0.01


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
# Agent 4 Deep Verify (Phase 6.5)
# ---------------------------------------------------------------------------
AGENT4_DEEP_VERIFY_ENABLED = (
    os.environ.get("PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED", "1") != "0"
)
AGENT4_DEEP_VERIFY_MAX_TURNS = int(
    os.environ.get("PUZZLEEVAL_AGENT4_DEEP_VERIFY_MAX_TURNS", "15")
)
AGENT4_DEEP_VERIFY_MAX_PARALLEL = int(
    os.environ.get("PUZZLEEVAL_AGENT4_DEEP_VERIFY_MAX_PARALLEL", "5")
)


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
AGENT5_MAX_TURNS = int(os.environ.get("PUZZLEEVAL_AGENT5_MAX_TURNS", "25"))
AGENT5_MAX_BUDGET_PER_CANDIDATE = float(
    os.environ.get("PUZZLEEVAL_AGENT5_BUDGET_PER_CANDIDATE", "3.0")
)
AGENT5_MAX_BUDGET_TOTAL = float(
    os.environ.get("PUZZLEEVAL_AGENT5_BUDGET_TOTAL", "20.0")
)
AGENT5_MAX_PARALLEL = int(os.environ.get("PUZZLEEVAL_AGENT5_MAX_PARALLEL", "5"))
AGENT5_CODE_TIMEOUT = int(os.environ.get("PUZZLEEVAL_AGENT5_CODE_TIMEOUT", "120"))  # 120s for async APIs that poll (Mindee, DocuClipper)
AGENT5_MAX_OUTPUT_TOKENS = int(
    os.environ.get("PUZZLEEVAL_AGENT5_MAX_TOKENS", "8192")
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
AGENT6_TEST_TIMEOUT = int(
    os.environ.get("PUZZLEEVAL_AGENT6_TEST_TIMEOUT", "120")
)  # Seconds per test case — some OCR APIs poll for up to 120s
AGENT6_RATE_LIMIT_BACKOFF = int(
    os.environ.get("PUZZLEEVAL_AGENT6_RATE_LIMIT_BACKOFF", "3")
)  # Seconds to wait on rate limit before retry
AGENT6_EVAL_MAX_TOKENS = int(
    os.environ.get("PUZZLEEVAL_AGENT6_EVAL_MAX_TOKENS", "4096")
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


MIN_CACHEABLE_TOKENS = {
    "claude-opus-4-7": 4096,
    "claude-opus-4-5": 4096,
    "claude-opus-4-1": 1024,
    "claude-sonnet-4-6": 2048,
    "claude-sonnet-4-5-20250929": 1024,
    "claude-haiku-4-5-20251001": 4096,
}
