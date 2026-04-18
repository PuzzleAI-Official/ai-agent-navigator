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
# AGENT5_CODE_TIMEOUT = baseline subprocess timeout in seconds.
# 120s suits sync APIs and most async-polling jobs. Long-running operations
# (video encoding, ML training, large-batch processing, async jobs with
# documented SLA > 2 min) need more — use the *_LONG values below, or let
# the Agent 5 builder scale dynamically per candidate based on
# `interaction_model.async_polling` / `batch_file` flags from the atlas.
AGENT5_CODE_TIMEOUT = int(os.environ.get("PUZZLEEVAL_AGENT5_CODE_TIMEOUT", "120"))
# When the candidate's atlas reports async_polling OR batch_file, scale the
# timeout to this value. Defaults to 10 minutes — covers video encoding,
# ML model inference queues, batch document processing, large file uploads.
AGENT5_CODE_TIMEOUT_LONG = int(
    os.environ.get("PUZZLEEVAL_AGENT5_CODE_TIMEOUT_LONG", "600")
)
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
#   high   - default; always think, deep reasoning on complex tasks
#   xhigh  - deeper exploration, available on Opus 4.7
#   max    - no constraint on thinking depth, available on Opus 4.7+
#
# When unset (empty string), no effort is sent and the API uses its model
# default (high on adaptive-capable models).
# ---------------------------------------------------------------------------
EFFORT = os.environ.get("PUZZLEEVAL_EFFORT", "high").lower().strip()
_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max", ""}
if EFFORT not in _VALID_EFFORTS:
    # Unknown value — log and reset to default so downstream API calls don't
    # fail with a 400. We don't raise because config import shouldn't crash.
    import sys as _sys
    print(
        f"warning: PUZZLEEVAL_EFFORT={EFFORT!r} not in "
        f"{sorted(_VALID_EFFORTS)} — defaulting to 'high'",
        file=_sys.stderr,
    )
    EFFORT = "high"


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


MIN_CACHEABLE_TOKENS = {
    "claude-opus-4-7": 4096,
    "claude-opus-4-5": 4096,
    "claude-opus-4-1": 1024,
    "claude-sonnet-4-6": 2048,
    "claude-sonnet-4-5-20250929": 1024,
    "claude-haiku-4-5-20251001": 4096,
}


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
