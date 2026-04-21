# ============================================================================
# Agent 4: Screening Agent
# ============================================================================
# PURPOSE:
#   Verify that each candidate from Agent 2 has REAL, publicly accessible
#   API access. Enrich validated candidates with information Agent 5 needs
#   to build test harnesses (auth method, verified docs URL, data formats).
#
# DESIGN: Per-candidate isolated verification (PARALLEL) + one structuring call.
#
# WHY PER-CANDIDATE ISOLATION?
#   The most reliable way to verify API access is to fetch the actual API
#   documentation page — not guess from search snippets. But web_fetch in
#   a single API call across 7 candidates causes token accumulation (the
#   exact problem that burned us in Agent 2 at $10/run). Solution: one API
#   call per candidate, each with its own isolated context.
#
# WHY PARALLEL?
#   Each candidate's verification is completely independent — no shared
#   state, no cross-candidate context. Running them in parallel via
#   ThreadPoolExecutor cuts wall-clock time from ~2-3 minutes (sequential)
#   to ~20-30 seconds (parallel). The Anthropic sync client is thread-safe
#   for independent API calls.
#
# HOW IT WORKS:
#   For each candidate (5-7 PARALLEL calls via ThreadPoolExecutor):
#     1. web_fetch the api_docs_url (if Agent 2 provided one)
#     2. If fetch fails → web_search for "{name} API docs" as fallback
#     3. Visit product website as last resort
#     4. Claude reads actual page content, determines PASS/REJECT
#     5. Returns text findings with enrichment data
#
#   Then one final client.messages.parse() call structures all findings
#   into Agent4Result.
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  CORE LINES GUIDE                                               │
# │                                                                 │
# │  If you want to understand ONLY the main logic (skip logging,   │
# │  error handling, cost tracking), read these lines:              │
# │                                                                 │
# │  1. VERIFICATION_SYSTEM_PROMPT — per-candidate verification      │
# │  2. STRUCTURE_SYSTEM_PROMPT    — final JSON structuring          │
# │  3. _build_candidate_message() — assembles per-candidate request │
# │  4. _extract_text_from_response() — pulls text from mixed blocks │
# │  5. _verify_single_candidate() — ONE candidate, ONE API call     │
# │  6. run_screening_agent()      — THE MAIN FUNCTION               │
# │     Inside it, the core is:                                      │
# │       a. client = anthropic.Anthropic(...)                       │
# │       b. ThreadPoolExecutor → parallel per-candidate:             │
# │            finding = _verify_single_candidate(...)  ★ VERIFY     │
# │       c. structured = client.messages.parse(                     │
# │            output_format=Agent4Result, ...)          ★ STRUCTURE  │
# │       d. return structured.parsed_output                         │
# │                                                                 │
# │  Everything else is logging, error handling, and cost tracking  │
# │  — necessary for production but not for understanding the flow. │
# └─────────────────────────────────────────────────────────────────┘
# ============================================================================

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from puzzleeval.config import (
    ANTHROPIC_API_KEY,
    DEFAULT_MODEL,
    ENABLE_FETCH_FALLBACK,
    SCREENING_MODEL,
    WEB_SEARCH_PRICE_PER_SEARCH,
)
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import (
    Agent4Input,
    Agent4Result,
    Candidate,
)
from puzzleeval.web_fetch_fallback import (
    count_actionable_problems,
    extract_blocked_fetches,
    extract_unusable_pages,
    summarize_blocks_for_log,
)


# ============================================================================
# [CORE] System Prompt — Per-Candidate Verification
# ============================================================================
# This prompt runs ONCE PER CANDIDATE in an isolated API call. Claude
# fetches/searches for the candidate's API docs and determines whether
# real, publicly accessible API access exists.
#
# ACCURACY IS THE #1 GOAL:
#   - False positive (passing a no-API candidate) → Agent 5 wastes time
#   - False negative (rejecting a real-API candidate) → we miss a contender
#   Both are bad. The prompt gives Claude explicit signals for each.
#
# WHY web_fetch + web_search (not just search)?
#   - web_fetch reads the ACTUAL page content — ground truth
#   - web_search is the fallback when the URL fails or is missing
#   - Two chances to find real docs prevents false negatives
# ============================================================================

VERIFICATION_SYSTEM_PROMPT = """You are verifying whether a single AI service has real, publicly accessible API access. Your goal is to FIND the real API documentation page if it exists. You have 3 web searches and 3 web fetches — use them strategically.

## IMPORTANT: You are finding the API docs page, not studying the API
Confirm: "Do real, public API docs exist, and WHERE?" Once you find the URL with clear evidence (endpoints, auth docs, SDK installs), STOP and report. But do NOT give up early — exhaust your search strategies before concluding no API exists.

## Strategy: Progressive search, then fetch to confirm

### Step 1: SEARCH — standard queries (always do this first)
SEARCH for "{service name} API documentation" or "{service name} developer API".
Read the search results carefully. Look for:
- Search results pointing to developer portals, API references, SDK pages
- Snippets showing endpoint URLs (POST /v1/...), auth methods, SDK install commands
- URLs like developers.example.com, docs.example.com/api, example.com/api-reference

If search results CLEARLY show real API documentation exists (you can see endpoint references, auth docs, or SDK pages in the snippets) → PASS immediately. Do NOT fetch — you already have enough evidence.

### Step 2: SEARCH — capability-specific queries (if Step 1 inconclusive)
Try ALTERNATIVE search queries that match how the service labels its API:
- "{service name} REST API" or "{service name} OCR API" (use the specific capability from the candidate description)
- "{service name} API reference endpoints"
Many services label their API under a feature name (e.g., "DocuClipper OCR API" instead of "DocuClipper API documentation"). This search catches those.

### Step 3: SEARCH — site-scoped query (if Steps 1-2 inconclusive)
Search WITHIN the service's domain to find any API-related page:
- "site:{domain} API" or "site:{domain} developer documentation"
This forces the search engine to find pages on the service's own site that mention "API", even if those pages are buried deep in navigation or not well-linked.

### Step 4: FETCH — progressive exploration (use remaining fetches as needed)
Use your web fetches strategically based on what searches found:

**Fetch priority 1:** The most promising API docs URL from search results — fetch it to confirm it contains real documentation.

**Fetch priority 2:** The product's main website (Source URL below). Examine the page THOROUGHLY:
- Check the MAIN NAVIGATION BAR and FOOTER for "Docs", "Developers", "API", "Integrations" links
- Check DROPDOWN MENUS under "Products", "Features", "Solutions", or "Platform" — APIs are often nested under feature categories (e.g., Features → OCR API, Tools & Integrations → API)
- Look for URLs containing "/api", "/developers", "/docs", "/reference", or "/integration" in ANY link on the page
- Note any URL that could plausibly lead to API documentation

**Fetch priority 3:** FOLLOW the most promising API-related link you found on the homepage. This is critical — if you see a "OCR API" link under Features, or a "Developers" link in the footer, FETCH that URL to confirm it leads to real API docs. This step catches APIs hidden behind navigation that searches missed.

## What Real API Docs Look Like (PASS signals)
- REST/GraphQL endpoint references (POST /v1/..., GET /api/...)
- Authentication documentation (API key setup, OAuth flow, bearer tokens)
- SDK installation instructions (pip install, npm install)
- Request/response code examples
- OpenAPI/Swagger specification links

## What Fake/Inaccessible APIs Look Like (REJECT signals)
- "Contact Sales" or "Request a Demo" as the ONLY way to get access
- "Enterprise only" with no self-service tier at all
- Marketing landing pages with no technical content anywhere on the entire site
- "Coming soon" or "Beta — request access" with no public docs
- The product founder or official sources explicitly confirm no API exists

## CRITICAL: Evidence-Based Determination
If you found ANY of the following evidence that an API exists, you MUST PASS — even if you could not access the specific docs page:
- Marketing pages mentioning "REST API", "API access", or "developer API"
- Pricing tiers that include "API access" as a feature
- A docs URL that exists but returned a temporary error (5xx, timeout, Cloudflare block)
- SDK packages on PyPI/npm (pip install {service-name})
- GitHub repos with official client libraries
- Search results referencing API endpoints, even if the linked page was inaccessible

In these cases: PASS with VERIFIED_DOCS_URL set to the best URL you found (even if you couldn't fully access it). Add detailed NOTES explaining what evidence you found and what Agent 5 should investigate further.

Only REJECT when there is genuinely ZERO evidence of any API existing across ALL your search and fetch attempts, OR when authoritative sources confirm no API exists.

## Accuracy Rules
- If you find real API docs with endpoints and auth documentation → PASS. Do NOT reject.
- If you find EVIDENCE of an API but can't access the docs page → PASS with notes. Agent 5 has its own web tools and will investigate further.
- Only REJECT when zero evidence exists after exhausting all strategies.
- When uncertain, ALWAYS PASS with notes. A false pass costs nothing (Agent 5 will catch it). A false reject loses a valid candidate forever.
- Never reject based on pricing alone — paid APIs are still accessible.

## Output Format
Write your findings as structured text with these exact labels:

CANDIDATE: {name}
DETERMINATION: PASS or REJECT
STRATEGIES_TRIED: Which steps you used and what happened at each step
EVIDENCE: What specific content confirmed API access (e.g., "Search results show REST API reference at docs.example.com with POST /v1/analyze endpoint")
AUTH_METHOD: api_key / oauth2 / bearer_token / basic_auth / no_auth / unknown
ACCESS_METHOD: free_signup / free_tier / trial / sandbox / open / paid_only
VERIFIED_DOCS_URL: The URL where real API docs were confirmed (or the best candidate URL if evidence exists but page was inaccessible)
CONFIRMED_CAPABILITIES: Comma-separated list of capabilities found in docs
RATE_LIMITS: Any rate limit info found (or "not_found")
DATA_FORMATS: What input/output formats the API accepts (or "not_found")
NOTES: Any caveats, uncertainty, or additional context for Agent 5
"""


# ============================================================================
# [CORE] System Prompt — Final Structuring
# ============================================================================
# Takes ALL per-candidate findings (concatenated text) and structures them
# into Agent4Result. Simple formatting/scoring task — all the hard work
# (fetching, searching, verifying) was done in the per-candidate calls.
# ============================================================================

STRUCTURE_SYSTEM_PROMPT = """You are a data structuring assistant. Take the screening findings for each candidate and structure them into the exact JSON format required.

## Rules

1. Every candidate must appear in EITHER validated_candidates OR rejected_candidates — none should be dropped.
2. For validated candidates (DETERMINATION: PASS):
   - ALL enrichment fields must be populated (verified_api_docs_url, auth_method, etc.)
   - confirmed_capabilities should come from the EVIDENCE, not just repeat claimed capabilities
   - screening_notes should summarize the evidence trail
   - **relevance_score MUST be copied EXACTLY from the Original Candidate Data — do NOT change it. It is Agent 2's score, not yours to modify.**
   - **adoption_difficulty MUST be copied EXACTLY from the Original Candidate Data.**
3. For rejected candidates (DETERMINATION: REJECT):
   - rejection_reason should be specific and evidence-based
   - rejection_category must be one of the allowed values
4. total_candidates_screened must equal len(validated) + len(rejected)

## Field Guidelines

auth_method: One of "api_key", "oauth2", "bearer_token", "basic_auth", "no_auth", "unknown"
api_access_method: One of "free_signup", "free_tier", "trial", "sandbox", "open", "paid_only"
rejection_category: One of "no_api_access", "no_public_docs", "capability_mismatch", "rate_limit_insufficient", "no_free_tier", "enterprise_only", "deprecated", "region_restricted"

## Screening Summary
Write a brief overview: how many candidates were screened, how many passed, how many rejected, and any notable patterns (e.g., "3 of 5 candidates have free tiers suitable for testing").
"""


# ============================================================================
# Tool Configuration — Per-Candidate (search-first, fetch-when-needed)
# ============================================================================
# COST OPTIMIZATION: Search results (~5-7K tokens) are much cheaper than
# web_fetch (~10-140K tokens per page). For well-known services (Google,
# AWS, etc.), search snippets clearly show API docs exist — no need to
# fetch the full 140K-token API reference just to answer "does it exist?"
#
#   web_search=3: Standard query + capability-specific + site-scoped
#   web_fetch=3:  Docs page + homepage + follow promising link
#
# Each call is isolated — one candidate's context doesn't affect another.
# ============================================================================

# Tool versions: basic 20250910 + 20250305. Real-run experience with
# the 20260209 dynamic-filtering pair surfaced multiple operational
# regressions (400 container_id errors, sandbox spin-up latency,
# cross-agent propagation complexity) that outweighed the filtering
# benefit for Agent 4's single-call-per-candidate verify pattern.
# See research.py top-of-file docstring for the full trace evidence.
WEB_FETCH_TOOL = {
    "type": "web_fetch_20250910",
    "name": "web_fetch",
    "max_uses": 3,     # Docs page + homepage + follow promising link
    "max_content_tokens": 10000,  # Agent 4 verifies docs exist — 10K covers endpoints + auth + examples
}

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,     # Standard query + capability-specific + site-scoped
}


# Caching is DISABLED for Agent 4 — per-candidate calls have different
# content each time, so there's no repeated context to cache.

# Max tokens per call type.
# Verification is concise — Claude reads a page and writes ~500-1000 tokens of findings.
# Structuring formats all findings into JSON (~2000-3000 tokens output).
VERIFICATION_MAX_TOKENS = 4096
# Structuring must output ALL validated/rejected candidates with rich
# enrichment fields (16 fields per ScreenedCandidate × up to 7 candidates).
# 4096 tokens is too small — causes truncated JSON. 16384 gives plenty of
# room even for 7 fully-populated candidates.
STRUCTURE_MAX_TOKENS = 16384

# ---------------------------------------------------------------------------
# pause_turn Safety Valve
# ---------------------------------------------------------------------------
# Per-candidate calls can use up to 3 searches + 3 fetches (6 tool uses),
# so the server-side loop may need more time. Allow 2 continuations.
# ---------------------------------------------------------------------------
MAX_CONTINUATIONS = 2

# ---------------------------------------------------------------------------
# Parallel Verification
# ---------------------------------------------------------------------------
# Each candidate is verified independently — no shared state, no cross-
# candidate context. We run them in parallel using ThreadPoolExecutor.
#
# MAX_PARALLEL_VERIFICATIONS controls how many candidates are verified
# simultaneously. Higher = faster but more concurrent API calls.
# Set to 7 (max candidates from Agent 2) so all run at once by default.
# Reduce if you hit rate limits with your API tier.
#
# Override with: export PUZZLEEVAL_MAX_PARALLEL_SCREENING=3
# ---------------------------------------------------------------------------
MAX_PARALLEL_VERIFICATIONS = int(
    os.environ.get("PUZZLEEVAL_MAX_PARALLEL_SCREENING", "7")
)


# ============================================================================
# [CORE] Build the per-candidate verification message
# ============================================================================
# Creates the user message for one candidate's verification call. Includes
# the candidate's details from Agent 2 and the user's sub-tasks for
# capability matching.
# ============================================================================

def _build_candidate_message(
    candidate: Candidate,
    input_data: Agent4Input,
) -> str:
    """
    Build the user message for verifying a single candidate.

    Includes:
    - Candidate name, provider, claimed capabilities, API docs URL
    - The user's sub-tasks (so Claude can check capability match)
    """
    # ── CORE: Format the sub-tasks for capability matching ──
    subtask_lines = []
    for i, st in enumerate(input_data.user_understanding.sub_tasks, 1):
        subtask_lines.append(f"{i}. {st.description} (capability: {st.capability})")
    subtasks_text = "\n".join(subtask_lines)

    # ── CORE: Assemble the message ──
    message = f"""## Candidate to Verify
Name: {candidate.name}
Provider: {candidate.provider}
Description: {candidate.description}
API Docs URL (from research, unverified): {candidate.api_docs_url or "None provided"}
Source URL (product website): {candidate.source}
Claimed Capabilities: {", ".join(candidate.claimed_capabilities)}
Covers Sub-Tasks: {", ".join(candidate.relevant_subtasks)}

## User's Sub-Tasks (what the AI needs to do)
{subtasks_text}

---

Verify this candidate's API accessibility using the 3-strategy approach. Report your findings using the exact output format specified."""

    return message


# ============================================================================
# [CORE] Extract text from a mixed-content response
# ============================================================================
# Identical pattern to Agent 2's _extract_text_from_response().
# When Claude uses server tools (web_fetch, web_search), the response
# contains mixed content blocks. We only need Claude's text analysis.
# ============================================================================

def _extract_text_from_response(response: anthropic.types.Message) -> str:
    """
    Pull all text content from a response that may contain mixed
    content blocks (text + tool use + tool results).

    Returns a single string of Claude's text output, skipping all
    tool-related blocks.
    """
    text_parts = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
    return "\n\n".join(text_parts)


# ============================================================================
# [CORE] Verify a single candidate — one isolated API call
# ============================================================================
# This is the heart of Agent 4. Each candidate gets its own API call
# with web_fetch + web_search tools. Context is isolated — one candidate's
# huge docs page doesn't affect another candidate's verification.
#
# Returns the text findings for this candidate, or a failure message
# if the API call itself fails (the candidate gets rejected gracefully).
# ============================================================================

def _verify_single_candidate(
    client: anthropic.Anthropic,
    candidate: Candidate,
    input_data: Agent4Input,
    logger,
) -> tuple[str, float, int]:
    """
    Verify one candidate's API accessibility in an isolated API call.

    Returns a tuple of (text_findings, cost_usd, web_fetch_blocks).
    text_findings is PASS/REJECT + enrichment data.
    cost_usd is the total cost of API calls + web searches for this candidate.
    web_fetch_blocks is the count of recoverable fetch blocks seen during this
    verification (Cloudflare/403/429/unavailable). Used for observability.
    If the API call itself fails, returns a REJECT finding with 0 cost and 0 blocks.
    """
    # ★ CORE: Build the verification request
    candidate_message = _build_candidate_message(candidate, input_data)

    # [logging] Start timing
    verify_start = time.time()
    candidate_label = candidate.name.replace(" ", "_").lower()[:30]
    total_web_searches = 0
    candidate_cost = 0.0
    candidate_block_count = 0

    # ★ CORE: Call Claude with web_fetch + web_search tools (isolated context)
    messages = [{"role": "user", "content": candidate_message}]
    response = None

    for continuation in range(MAX_CONTINUATIONS + 1):
        try:
            from puzzleeval.agent_preamble import with_preamble
            from puzzleeval.config import output_config_for_request
            _kwargs_verify: dict[str, object] = {}
            _ocfg = output_config_for_request()
            if _ocfg:
                _kwargs_verify["output_config"] = _ocfg
            # [critical] `context_management` is a beta-gated Anthropic API
            # parameter. The regular `client.messages.create` endpoint does
            # not accept it and returns HTTP 400 "context_management extra
            # inputs not permitted". We MUST route through
            # `client.beta.messages.create` with the `context-management-
            # 2025-06-27` beta header, matching how `deep_verify_runner.py`
            # wires the same feature. Prior code passed `context_management`
            # via `extra_body=`, which made the API reject EVERY verification
            # call — every candidate surfaced as "transient API error" and
            # got mass-rejected with `no_public_docs` even though their docs
            # were reachable. See CLAUDE.md OT-012 investigation.
            response = client.beta.messages.create(
                model=SCREENING_MODEL,
                max_tokens=VERIFICATION_MAX_TOKENS,
                betas=["context-management-2025-06-27"],
                system=[{"type": "text", "text": with_preamble(VERIFICATION_SYSTEM_PROMPT)}],
                messages=messages,
                tools=[WEB_FETCH_TOOL, WEB_SEARCH_TOOL],
                # Adaptive thinking — Sonnet reasons about which search to run next
                # and how to interpret pages between tool calls. Same pattern Agent 5
                # uses; lifts per-candidate verification quality measurably.
                thinking={"type": "adaptive"},
                # Server-side context management — clears old tool results when
                # context grows past 80K tokens (clear_tool_uses_20250919),
                # summarizes at 150K (compact_20260112). Mirrors Agent 5's
                # in-loop strategy. Without this, multi-page-fetch verification
                # of complex APIs can blow the context window mid-loop.
                context_management={
                    "edits": [
                        {
                            "type": "clear_tool_uses_20250919",
                            "trigger": {"type": "input_tokens", "value": 80000},
                        }
                    ]
                },
                **_kwargs_verify,
            )

        # [error handling] Per-candidate failure is graceful — don't kill the pipeline
        except anthropic.RateLimitError as e:
            logger.warning(f"Rate limit during verification of {candidate.name}", extra={
                "operation": f"screening_verify_{candidate_label}",
                "trace_id": input_data.trace_id,
                "error": str(e), "error_type": "RateLimitError",
            })
            return (
                f"CANDIDATE: {candidate.name}\n"
                f"DETERMINATION: REJECT\n"
                f"EVIDENCE: API verification failed due to rate limit — could not fetch docs\n"
                f"NOTES: Rate limit error during screening. This is a transient failure, "
                f"not a definitive rejection. Consider re-running.\n",
                candidate_cost,
                candidate_block_count,
            )
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            logger.warning(f"API error during verification of {candidate.name}", extra={
                "operation": f"screening_verify_{candidate_label}",
                "trace_id": input_data.trace_id,
                "error": str(e), "error_type": type(e).__name__,
            })
            return (
                f"CANDIDATE: {candidate.name}\n"
                f"DETERMINATION: REJECT\n"
                f"EVIDENCE: API verification failed due to API error — could not fetch docs\n"
                f"NOTES: API error during screening: {e}. This is a transient failure.\n",
                candidate_cost,
                candidate_block_count,
            )

        # [logging] Log this call's metrics
        iter_cost = log_llm_call(
            logger=logger, response=response, model=SCREENING_MODEL,
            trace_id=input_data.trace_id, start_time=verify_start,
            operation=f"screening_verify_{candidate_label}_iter{continuation}",
        )
        candidate_cost += iter_cost

        # [cost tracking] Accumulate web search usage
        server_tool_use = getattr(response.usage, "server_tool_use", None)
        if server_tool_use:
            total_web_searches += getattr(server_tool_use, "web_search_requests", 0) or 0

        # [Phase 1 + 1.5 hardening] Detect both HTTP-level blocks and
        # content-level useless pages. Agent 4 makes a single call per
        # candidate (no multi-turn loop), so we cannot inject fallback
        # guidance mid-call — the model has already produced its findings.
        # We just count both classes of failures for observability so the
        # team can spot providers whose docs are WAF-gated OR JS-rendered.
        if ENABLE_FETCH_FALLBACK:
            blocked = extract_blocked_fetches(response)
            unusable = extract_unusable_pages(response)
            actionable = count_actionable_problems(blocked, unusable)
            if actionable:
                candidate_block_count += actionable
                logger.info(
                    f"Web fetch problems for {candidate.name}",
                    extra={
                        "operation": f"screening_fetch_blocks_{candidate_label}",
                        "trace_id": input_data.trace_id,
                        "candidate_name": candidate.name,
                        **summarize_blocks_for_log(blocked, unusable),
                    },
                )

        # [pause_turn] If the API finished, break out of the loop
        if response.stop_reason != "pause_turn":
            break

        # [pause_turn] Continue by re-sending with assistant's partial response
        logger.info(f"pause_turn for {candidate.name}, continuing", extra={
            "operation": f"screening_pause_turn_{candidate_label}",
            "trace_id": input_data.trace_id,
            "continuation": continuation + 1,
        })
        messages = [
            {"role": "user", "content": candidate_message},
            {"role": "assistant", "content": response.content},
        ]

    # [cost tracking] Log web search usage for this candidate
    if total_web_searches > 0:
        logger.info(f"Web search usage for {candidate.name}", extra={
            "operation": f"screening_web_usage_{candidate_label}",
            "trace_id": input_data.trace_id,
            "web_search_requests": total_web_searches,
            "web_search_cost_usd": round(total_web_searches * WEB_SEARCH_PRICE_PER_SEARCH, 4),
        })

    # ★ CORE: Extract text findings from the response
    findings = _extract_text_from_response(response)

    if not findings.strip():
        logger.warning(f"No text output for {candidate.name}", extra={
            "operation": f"screening_verify_{candidate_label}",
            "trace_id": input_data.trace_id,
            "stop_reason": response.stop_reason,
        })
        return (
            f"CANDIDATE: {candidate.name}\n"
            f"DETERMINATION: REJECT\n"
            f"EVIDENCE: Verification produced no output — could not determine API access\n"
            f"NOTES: Empty response from verification call.\n",
            candidate_cost + total_web_searches * WEB_SEARCH_PRICE_PER_SEARCH,
            candidate_block_count,
        )

    # [cost tracking] Add web search fees to this candidate's cost
    candidate_cost += total_web_searches * WEB_SEARCH_PRICE_PER_SEARCH

    logger.info(f"Verified {candidate.name}", extra={
        "operation": f"screening_verify_{candidate_label}",
        "trace_id": input_data.trace_id,
        "findings_length": len(findings),
        "web_fetch_blocks": candidate_block_count,
    })

    return findings, candidate_cost, candidate_block_count


# ============================================================================
# [CORE] Main function — this is the entry point
# ============================================================================
#
# THE CORE LOGIC (marked with ★ below):
#   1. ThreadPoolExecutor → parallel _verify_single_candidate() per candidate
#   2. Concatenate all findings
#   3. client.messages.parse(output_format=Agent4Result) → structured JSON
#
# N+1 API calls total: N per-candidate verification (parallel) + 1 structuring.
# Each per-candidate call is isolated (no token accumulation).
#
# ============================================================================

def run_screening_agent(input_data: Agent4Input) -> Agent4Result:
    """
    Run Agent 4. Takes Agent 2's candidates and Agent 1's user understanding,
    verifies API accessibility for each candidate, returns structured results.

    Per-candidate isolation + parallelism:
      Each candidate gets its own API call with web_fetch + web_search,
      running in parallel via ThreadPoolExecutor. Context never accumulates
      across candidates, preventing token explosion. Parallelism cuts
      wall-clock time from ~2-3 min (sequential) to ~20-30 sec.

    Then one final structuring call formats all findings into Agent4Result.
    """
    # ★ CORE LINE 1: Create the API client
    # Server-tool timeout — Agent 4's deep-verify loop runs up to 15
    # turns per candidate, each burning server-side web_fetch (6 max)
    # and web_search (5 max). Legitimate completion can take 2-4 min
    # per candidate on providers with fragmented docs. Default 120 s
    # collapsed real runs mid-verify.
    from puzzleeval.anthropic_client import build_client, SERVER_TOOL_TIMEOUT_S
    client = build_client(
        api_key=ANTHROPIC_API_KEY,
        timeout=SERVER_TOOL_TIMEOUT_S,
    )

    # [logging] Set up logger for this agent
    logger = get_logger("agent_4_screening")
    candidates = input_data.candidates.candidates

    logger.info("Agent 4 started", extra={
        "operation": "agent_start",
        "trace_id": input_data.trace_id,
        "candidate_count": len(candidates),
        "max_parallel": MAX_PARALLEL_VERIFICATIONS,
    })

    # ──────────────────────────────────────────────────────────────────
    # Agent 4's job (as of this pass): verify API existence. Fast.
    #
    # Produces a ScreenedCandidate per candidate with the minimum Agent 5
    # needs to start its own Phase-1 research:
    #   - verified_api_docs_url (starting point)
    #   - auth_method, api_access_method
    #   - screening_notes
    #
    # NO atlas extraction, NO endpoint enumeration, NO cross-run caching.
    # Agent 5 does its own research from scratch against these verified
    # docs — which is the separation of concerns the user asked for:
    # Agent 4 verifies, Agent 5 researches + builds. The earlier attempt
    # to fit both jobs into Agent 4's deep-verify produced rich atlases
    # that Agent 5 couldn't consume efficiently (wrong shape, too much
    # context, missed the specific build-oriented details the builder
    # needed). See the removed ``deep_verify_runner.py`` / ``provider_atlas.py``
    # / ``deep_verify_prompt.py`` modules for the prior architecture.
    # ──────────────────────────────────────────────────────────────────

    # ======================================================================
    # STEP 1: Per-Candidate Verification (N PARALLEL isolated API calls)
    # ======================================================================
    # Each candidate gets its own API call in its own thread. This is the
    # key design: (1) isolated context prevents token accumulation,
    # (2) parallel execution cuts latency to the slowest single candidate.
    #
    # The Anthropic sync client is thread-safe for independent API calls —
    # each thread makes its own HTTP request with its own context.
    # ======================================================================

    # ★ CORE: Launch all candidate verifications in parallel
    # We use a dict to preserve candidate order in the final output.
    findings_by_index: dict[int, str] = {}
    total_verification_cost = 0.0
    total_web_fetch_blocks = 0

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_VERIFICATIONS) as executor:
        # Submit all verification tasks
        future_to_index = {}
        for i, candidate in enumerate(candidates):
            logger.info(f"Submitting verification for: {candidate.name}", extra={
                "operation": "screening_candidate_submit",
                "trace_id": input_data.trace_id,
                "candidate_name": candidate.name,
                "candidate_index": i + 1,
            })
            future = executor.submit(
                _verify_single_candidate, client, candidate, input_data, logger,
            )
            future_to_index[future] = i

        # Collect results as they complete
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            candidate_name = candidates[idx].name
            try:
                findings_text, candidate_cost, candidate_block_count = future.result()
                findings_by_index[idx] = findings_text
                total_verification_cost += candidate_cost
                total_web_fetch_blocks += candidate_block_count
            except Exception as e:
                # Unexpected exception from thread — graceful degradation
                logger.error(f"Unexpected error verifying {candidate_name}", extra={
                    "operation": "screening_verify_thread_error",
                    "trace_id": input_data.trace_id,
                    "error": str(e), "error_type": type(e).__name__,
                })
                findings_by_index[idx] = (
                    f"CANDIDATE: {candidate_name}\n"
                    f"DETERMINATION: REJECT\n"
                    f"EVIDENCE: Verification failed due to unexpected error\n"
                    f"NOTES: {e}\n"
                )

    # Reconstruct findings in original candidate order
    all_findings = [findings_by_index[i] for i in range(len(candidates))]

    # Concatenate all findings for the structuring step
    combined_findings = "\n\n---\n\n".join(all_findings)

    logger.info("All candidates verified, structuring results", extra={
        "operation": "screening_verification_complete",
        "trace_id": input_data.trace_id,
        "total_findings_length": len(combined_findings),
    })

    # ======================================================================
    # STEP 2: Structure the Findings
    # ======================================================================
    # Takes all per-candidate findings and passes through structured output
    # to get guaranteed JSON matching Agent4Result. Uses DEFAULT_MODEL since
    # this is a simple formatting task.
    # ======================================================================

    # ★ CORE: Build the structuring request with candidate context
    # Include original candidate data so the structuring step can carry
    # forward fields like pricing_model, relevance_score, etc.
    candidate_context_lines = []
    for c in candidates:
        candidate_context_lines.append(
            f"- {c.name} (by {c.provider}): "
            f"pricing={c.pricing_model}, relevance={c.relevance_score}, "
            f"source={c.source}, "
            f"relevant_subtasks={c.relevant_subtasks}, "
            f"claimed_capabilities={c.claimed_capabilities}"
        )
    candidate_context = "\n".join(candidate_context_lines)

    structure_message = f"""## Original Candidate Data (from Agent 2)
{candidate_context}

## Screening Findings
{combined_findings}

---

Structure these findings into the required JSON format. Every candidate must appear in either validated_candidates or rejected_candidates."""

    # ★ CORE: Call Claude with structured output to format findings
    # Wrapped in parse_with_fallback for the same grammar-budget reason as
    # Agents 1 / 2 — Agent4Result includes ScreenedCandidate[] with deep
    # enrichment fields and can hit Anthropic's compiled-grammar size cap.
    step2_start = time.time()
    try:
        from puzzleeval.agent_preamble import with_preamble
        from puzzleeval.structured_output import parse_with_fallback
        structure_response = parse_with_fallback(
            client=client,
            model=DEFAULT_MODEL,
            max_tokens=STRUCTURE_MAX_TOKENS,
            system=[{"type": "text", "text": with_preamble(STRUCTURE_SYSTEM_PROMPT)}],
            messages=[{"role": "user", "content": structure_message}],
            output_format=Agent4Result,
            extra={},
            trace_id=input_data.trace_id,
        )

    # [error handling] Structuring failures are fatal — we need the final output
    except anthropic.RateLimitError as e:
        logger.error("Rate limit hit (structuring step)", extra={
            "operation": "llm_call_structure", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "RateLimitError",
        })
        raise AgentRateLimitError(
            message=f"Rate limit exceeded during screening structuring: {e}",
            agent_name="screening", trace_id=input_data.trace_id,
        )
    except anthropic.APIConnectionError as e:
        logger.error("API connection failed (structuring step)", extra={
            "operation": "llm_call_structure", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIConnectionError",
        })
        raise AgentAPIError(
            message=f"Failed to connect to Anthropic API during screening structuring: {e}",
            agent_name="screening", trace_id=input_data.trace_id,
        )
    except anthropic.APIStatusError as e:
        logger.error("API error (structuring step)", extra={
            "operation": "llm_call_structure", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIStatusError",
        })
        raise AgentAPIError(
            message=f"Anthropic API error during screening structuring: {e}",
            agent_name="screening", trace_id=input_data.trace_id,
        )

    # [logging] Log structuring call metrics
    structure_cost = log_llm_call(
        logger=logger, response=structure_response, model=DEFAULT_MODEL,
        trace_id=input_data.trace_id, start_time=step2_start,
        operation="screening_structure",
    )

    # ★ CORE: Return the parsed result
    result = structure_response.parsed_output

    # [error handling] Defensive check for truncated/refused responses
    if result is None:
        logger.error("Parsed output is None (structuring step)", extra={
            "operation": "output_validation", "trace_id": input_data.trace_id,
            "error": "parsed_output is None",
            "stop_reason": structure_response.stop_reason,
        })
        raise AgentOutputError(
            message=(
                f"Screening structuring returned no parsed output. "
                f"stop_reason={structure_response.stop_reason}"
            ),
            agent_name="screening", trace_id=input_data.trace_id,
        )

    # [cost tracking] Set total cost: all verification costs + structuring cost
    result.cost_usd = round(total_verification_cost + structure_cost, 6)

    # [Phase 1 hardening] Surface aggregate web_fetch block count for observability.
    # Helps operators spot providers whose docs are consistently WAF/Cloudflare gated.
    result.web_fetch_blocks = total_web_fetch_blocks

    logger.info("Agent 4 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "validated_count": len(result.validated_candidates),
        "rejected_count": len(result.rejected_candidates),
        "web_fetch_blocks": total_web_fetch_blocks,
    })

    return result
