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

from functools import lru_cache
from importlib import resources

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

@lru_cache(maxsize=1)
def _load_verification_system_prompt() -> str:
    """Load VERIFICATION_SYSTEM_PROMPT from templates/verification_system.md (Phase 7)."""
    return resources.files("puzzleeval.agents.agent4").joinpath(
        "templates", "verification_system.md",
    ).read_text(encoding="utf-8")


VERIFICATION_SYSTEM_PROMPT = _load_verification_system_prompt()


# ============================================================================
# [CORE] System Prompt — Final Structuring
# ============================================================================
# Takes ALL per-candidate findings (concatenated text) and structures them
# into Agent4Result. Simple formatting/scoring task — all the hard work
# (fetching, searching, verifying) was done in the per-candidate calls.
# ============================================================================

@lru_cache(maxsize=1)
def _load_structure_system_prompt() -> str:
    """Load STRUCTURE_SYSTEM_PROMPT from templates/structure_system.md (Phase 7)."""
    return resources.files("puzzleeval.agents.agent4").joinpath(
        "templates", "structure_system.md",
    ).read_text(encoding="utf-8")


STRUCTURE_SYSTEM_PROMPT = _load_structure_system_prompt()


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
# Verification produces TWO outputs per candidate: (1) findings text
# (~500-1000 tokens — PASS/REJECT, evidence, auth_method, etc.) and
# (2) the BUILD_READINESS_CHECKLIST JSON block (~600-900 tokens — ten
# fields plus provider_surface). Adaptive thinking blocks add another
# ~1000-3000 tokens of reasoning between tool calls.
#
# Token budget history:
#   - 4096: original — JSON block reliably truncated (trace 73a9d605
#     every candidate fell to system_failure sentinel)
#   - 12288 (NEW-AK): mostly worked but failed on rich-docs candidates
#     (real-run trace 4427591c, 2026-04-25: OpenAI Realtime SIP had
#     so much rich data — 9 capabilities, 5 user_selectable_params,
#     full interaction_model, detailed pricing/rate_limits — that the
#     findings + structured fields consumed the budget, leaving the
#     fenced JSON block truncated. Sentinel checklist returned →
#     no pre-render fast path → Agent 5 had to do full Sonnet research)
#   - 16384 (NEW-AM): bumped to address the rich-docs failure mode.
#     Adds <$0.05 per candidate when budget actually used (Sonnet
#     output rate $15/MTok × 4K extra tokens = $0.06 worst case),
#     $0 otherwise. Still well below Sonnet's 64K per-call cap.
VERIFICATION_MAX_TOKENS = 16384
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

# ============================================================================
# [CORE] Build-readiness checklist extraction
# ============================================================================
# The verification prompt asks Claude to emit two blocks per candidate:
#   1. Findings text (PASS/REJECT + EVIDENCE / AUTH_METHOD / etc.)
#   2. A fenced ```json BUILD_READINESS_CHECKLIST block holding the
#      structured handoff to Agent 5.
#
# We pull the checklist deterministically from the FINDINGS text (per-
# candidate) rather than relying on the structuring LLM to copy it
# field-for-field. The structuring LLM is great at filling enums and
# rephrasing prose, but it occasionally drops nested JSON when the
# total output approaches the grammar-budget cap. Parsing the JSON
# block here gives us a guaranteed checklist on every Verified Pass.
#
# Per AD-007: contract enforcement lives in deterministic code, not
# prompts. Both layers exist (prompt teaches the format; parser
# enforces it), defense in depth.
# ============================================================================

import json as _json
import re as _re_screening

# Matches the fenced JSON block. We allow either a language hint
# ("json") or just the label, and we accept variations in spacing /
# casing of the label (Claude sometimes title-cases or uses spaces).
_CHECKLIST_FENCE_PATTERN = _re_screening.compile(
    r"```(?:json\s*)?BUILD_READINESS_CHECKLIST\s*\n(.*?)\n```",
    _re_screening.DOTALL | _re_screening.IGNORECASE,
)


def _extract_checklist_from_findings(
    findings_text: str,
    candidate_name: str,
) -> "BuildReadinessChecklist":
    """Parse the BUILD_READINESS_CHECKLIST JSON block out of one
    candidate's findings text and return a validated checklist.

    Falls back to `default_unknown_checklist` (system_failure sentinel)
    on any of: no fence found, malformed JSON, Pydantic validation
    failure. The fallback is intentional — per the three-state rejection
    model, system failures NEVER reject the candidate; instead Agent 5
    receives the sentinel and falls back to full-research mode.

    The reason string captures WHY the sentinel was emitted so Agent 5
    can see it and adjust expectations.
    """
    from puzzleeval.schemas import (
        BuildReadinessChecklist,
        default_unknown_checklist,
    )

    match = _CHECKLIST_FENCE_PATTERN.search(findings_text or "")
    if not match:
        return default_unknown_checklist(
            reason=(
                f"agent 4 produced no BUILD_READINESS_CHECKLIST block "
                f"for {candidate_name}"
            ),
        )

    raw_json = match.group(1).strip()
    try:
        parsed = _json.loads(raw_json)
    except _json.JSONDecodeError as exc:
        return default_unknown_checklist(
            reason=(
                f"BUILD_READINESS_CHECKLIST JSON for {candidate_name} "
                f"failed to parse: {exc}"
            ),
        )

    # Ensure populated_by defaults to "agent_4" when the LLM omitted it
    # (common — the JSON template doesn't include the meta field).
    parsed.setdefault("populated_by", "agent_4")

    try:
        checklist = BuildReadinessChecklist.model_validate(parsed)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError catch-all
        return default_unknown_checklist(
            reason=(
                f"BUILD_READINESS_CHECKLIST for {candidate_name} failed "
                f"schema validation: {type(exc).__name__}: {str(exc)[:200]}"
            ),
        )

    # Stamp last_updated_at to "now" if the LLM didn't supply one.
    if not checklist.last_updated_at:
        from datetime import datetime, timezone
        checklist.last_updated_at = datetime.now(timezone.utc).isoformat()

    return checklist


def _attach_checklists_to_result(
    result: "Agent4Result",
    findings_by_name: dict[str, str],
    logger,
    trace_id: str,
) -> None:
    """Patch each ScreenedCandidate with its extracted checklist.

    Mutates `result.validated_candidates` in place. For candidates whose
    findings text is missing or has no parseable checklist block, the
    sentinel from `default_unknown_checklist` is attached so the field
    is never None on a validated candidate (downstream Agent 5 contract:
    if checklist is None, treat as full-research; if checklist is a
    sentinel, also treat as full-research but with a richer reason
    string for diagnostics).

    Logs one INFO line per candidate summarizing the outcome
    (confirmed-count / total) plus a WARNING when the sentinel had to
    be used.
    """
    from puzzleeval.schemas import BUILD_READINESS_FIELDS

    for cand in result.validated_candidates:
        findings = findings_by_name.get(cand.name, "")
        checklist = _extract_checklist_from_findings(findings, cand.name)
        cand.checklist = checklist

        confirmed = sum(
            1 for n in BUILD_READINESS_FIELDS
            if getattr(checklist, n).status == "confirmed"
        )
        if checklist.populated_by == "system_failure":
            logger.warning(
                f"Sentinel checklist attached for {cand.name}: "
                f"{checklist.endpoint_path.reasoning}",
                extra={
                    "operation": "screening_checklist_sentinel",
                    "trace_id": trace_id,
                    "candidate_name": cand.name,
                    "reason": checklist.endpoint_path.reasoning,
                    "verified_pass": False,
                },
            )
        else:
            logger.info(
                f"Checklist attached for {cand.name}: "
                f"{confirmed}/{len(BUILD_READINESS_FIELDS)} fields confirmed, "
                f"verified_pass={checklist.is_verified_pass()}",
                extra={
                    "operation": "screening_checklist_attached",
                    "trace_id": trace_id,
                    "candidate_name": cand.name,
                    "confirmed_count": confirmed,
                    "total_fields": len(BUILD_READINESS_FIELDS),
                    "verified_pass": checklist.is_verified_pass(),
                    "has_provider_surface": checklist.has_provider_surface(),
                    "selected_endpoint": checklist.selected_endpoint,
                },
            )


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

        # [doc handoff] Persist web_fetch / web_search content to the
        # candidate's future sandbox directory so Agent 5's Phase-1 STEP 1
        # reads local files instead of re-fetching the same URLs. Saves
        # ~3-5s per candidate and immunizes Agent 5 from CF/WAF blocks
        # that hit Agent 4's second verifier pass. See
        # puzzleeval/web_doc_cache.py for the full handoff rationale.
        #
        # We resolve the sandbox dir via `candidate_sandbox_dir()` — the
        # single source of truth both agents agree on. If the dir doesn't
        # exist yet (it shouldn't — Agent 5 creates it later), the helper
        # mkdirs it. Agent 5's `harness_base.mkdir(parents=True,
        # exist_ok=True)` tolerates the dir being pre-existing.
        #
        # The helper swallows per-block extraction errors: one malformed
        # web_fetch_tool_result doesn't poison the screening call. If
        # nothing is extractable (e.g., CF blocked the fetch so the block
        # is an error shape), saved_now is empty — no logging noise.
        try:
            from puzzleeval.web_doc_cache import (
                candidate_sandbox_dir,
                save_web_fetches_to_sandbox,
            )
            _handoff_dir = candidate_sandbox_dir(
                input_data.trace_id, candidate.name
            )
            _existing = sum(
                1 for p in _handoff_dir.iterdir()
                if _handoff_dir.exists()
                and p.is_file()
                and p.name.startswith("fetched_docs_")
                and p.name.endswith(".txt")
            ) if _handoff_dir.exists() else 0
            # Diagnostic: what block types does this response carry?
            # This tells us at a glance whether Agent 4's verification
            # used web_fetch (saveable), web_search-only (saveable
            # snippets), or just text reasoning (nothing to save).
            # Previously ran silently — first real run (trace 2b2b9d1f)
            # showed ZERO saved files and ZERO logs so we had no way to
            # diagnose. Now every attempt logs BEFORE + AFTER.
            block_types: dict[str, int] = {}
            for block in getattr(response, "content", []) or []:
                bt = getattr(block, "type", "unknown")
                block_types[bt] = block_types.get(bt, 0) + 1
            saved_now = save_web_fetches_to_sandbox(
                response, _handoff_dir, existing_count=_existing,
            )
            logger.info(
                f"Agent 5 doc handoff attempted for {candidate.name}: "
                f"saved {len(saved_now)} file(s), "
                f"existing {_existing}, block_types={block_types}",
                extra={
                    "operation": f"screening_doc_handoff_{candidate_label}",
                    "trace_id": input_data.trace_id,
                    "candidate_name": candidate.name,
                    "handoff_dir": str(_handoff_dir),
                    "files_saved": saved_now,
                    "existing_count": _existing,
                    "block_types": block_types,
                    "saved_count": len(saved_now),
                },
            )
        except Exception as exc:  # noqa: BLE001 — doc handoff must not
            # break verification. Promoted to WARNING (was debug) so
            # real-run diagnostics don't get filtered out by uvicorn's
            # default INFO log level. If the save fails (disk full,
            # perms, malformed response), verification continues
            # unaffected; Agent 5 just re-fetches as before.
            logger.warning(
                f"Doc handoff FAILED for {candidate.name}: {type(exc).__name__}: {exc}",
                extra={
                    "operation": f"screening_doc_handoff_failed_{candidate_label}",
                    "trace_id": input_data.trace_id,
                    "candidate_name": candidate.name,
                    "error_type": type(exc).__name__,
                    "error_msg": str(exc)[:300],
                },
            )

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
    # enrichment fields (now also BuildReadinessChecklist with 10 nested
    # FieldStatus + EndpointSummary[]) and reliably exceeds Anthropic's
    # compiled-grammar size cap.
    #
    # ``prefer_non_strict=True``: skips the doomed strict attempt that
    # always 400s on this schema. Saves ~30-60s + one wasted API call
    # per Agent 4 run. Non-strict path produces an identical shim
    # object; downstream consumers don't branch.
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
            prefer_non_strict=True,
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
    except Exception as e:
        # StructuredOutputFallbackError (no tool_use block — non-strict
        # path equivalent of the strict-path "parsed_output is None"
        # check below). Surface as AgentOutputError so the caller
        # contract is unchanged. Imported lazily to avoid an import
        # cycle through structured_output.
        from puzzleeval.structured_output import StructuredOutputFallbackError
        if isinstance(e, StructuredOutputFallbackError):
            logger.error(
                "Structured output fallback failed (structuring step)",
                extra={
                    "operation": "output_validation",
                    "trace_id": input_data.trace_id,
                    "error": str(e),
                },
            )
            raise AgentOutputError(
                message=f"Screening structuring returned no parsed output. {e}",
                agent_name="screening", trace_id=input_data.trace_id,
            )
        raise

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

    # ─────────────────────────────────────────────────────────────────
    # CHECKLIST POST-PROCESS (deterministic, AD-007 defense-in-depth)
    # ─────────────────────────────────────────────────────────────────
    # The structuring LLM is asked to copy each candidate's
    # BUILD_READINESS_CHECKLIST JSON block from findings into
    # ScreenedCandidate.checklist. To make the contract robust against
    # transcription drift (the structuring step occasionally drops
    # nested JSON when output approaches the grammar-budget cap), we
    # OVERWRITE the checklist deterministically from the per-candidate
    # findings text. Single source of truth: each candidate's findings
    # block produced by _verify_single_candidate.
    #
    # When the JSON block is missing or malformed for a given
    # candidate, _extract_checklist_from_findings returns the sentinel
    # (default_unknown_checklist with populated_by="system_failure").
    # The candidate is NEVER rejected for this — Agent 5 handles the
    # sentinel by falling back to full-research mode (per the
    # three-state rejection model).
    findings_by_name = {
        candidates[i].name: findings_by_index[i]
        for i in range(len(candidates))
    }
    _attach_checklists_to_result(
        result, findings_by_name, logger, input_data.trace_id,
    )

    # ─────────────────────────────────────────────────────────────────
    # CHECKLIST OBSERVABILITY METRICS
    # ─────────────────────────────────────────────────────────────────
    # Aggregate counters surface "did Agent 4 deliver real checklists
    # this run, or did it sentinel-out?" without grepping per-candidate
    # logs. These flow up via pipeline metadata for run-level rollup.
    verified_pass_count = sum(
        1 for c in result.validated_candidates
        if c.checklist is not None and c.checklist.is_verified_pass()
    )
    inconclusive_count = sum(
        1 for c in result.validated_candidates
        if c.checklist is not None
        and not c.checklist.is_verified_pass()
        and c.checklist.populated_by != "system_failure"
    )
    sentinel_count = sum(
        1 for c in result.validated_candidates
        if c.checklist is not None
        and c.checklist.populated_by == "system_failure"
    )

    logger.info("Agent 4 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "validated_count": len(result.validated_candidates),
        "rejected_count": len(result.rejected_candidates),
        "web_fetch_blocks": total_web_fetch_blocks,
        "checklist_verified_pass_count": verified_pass_count,
        "checklist_inconclusive_count": inconclusive_count,
        "checklist_sentinel_count": sentinel_count,
    })

    return result
