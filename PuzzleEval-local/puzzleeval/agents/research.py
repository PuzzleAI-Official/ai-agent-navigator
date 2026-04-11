# ============================================================================
# Agent 2: Research Agent
# ============================================================================
# PURPOSE:
#   Find 5-7 candidate AI services with public APIs that match the user's
#   sub-tasks. Uses Anthropic's web search and web fetch server tools with
#   dynamic filtering to find real, current, well-known services.
#
# DESIGN: Two-step pure function — search the web, then structure the results.
#
# WHY TWO STEPS?
#   Step 1 uses web_search + web_fetch server tools via client.messages.create().
#   These are "server tools" — the API executes searches/fetches internally and
#   returns mixed content blocks (text + tool_use + tool_result). We can't use
#   client.messages.parse() with output_format here because structured output
#   conflicts with the server tool content blocks.
#
#   Step 2 takes the text findings from Step 1 and passes them through
#   client.messages.parse() with output_format=Agent2Result to get guaranteed
#   structured JSON. This is cheap (~500 tokens in, ~1000 out) since it's
#   just reformatting already-found data.
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  CORE LINES GUIDE                                               │
# │                                                                 │
# │  If you want to understand ONLY the main logic (skip logging,   │
# │  error handling, cost tracking), read these lines:              │
# │                                                                 │
# │  1. RESEARCH_SYSTEM_PROMPT    — instructions for web research   │
# │  2. STRUCTURE_SYSTEM_PROMPT   — instructions for JSON output    │
# │  3. _build_research_message() — assembles the search request    │
# │  4. _extract_text_from_response() — pulls text from mixed blocks│
# │  5. run_research_agent()      — THE MAIN FUNCTION               │
# │     Inside it, the core is just 6 lines:                        │
# │       a. client = anthropic.Anthropic(...)                      │
# │       b. research_msg = _build_research_message(input)          │
# │       c. research_response = client.messages.create(            │
# │            tools=[web_search])                  ★ STEP 1        │
# │       d. findings = _extract_text_from_response(response)       │
# │       e. structured = client.messages.parse(                    │
# │            output_format=Agent2Result, ...)      ★ STEP 2       │
# │       f. return structured.parsed_output                        │
# │                                                                 │
# │  Everything else is logging, error handling, and cost tracking  │
# │  — necessary for production but not for understanding the flow. │
# └─────────────────────────────────────────────────────────────────┘
# ============================================================================

import time

import anthropic

from puzzleeval.config import (
    ANTHROPIC_API_KEY,
    DEFAULT_MODEL,
    RESEARCH_MODEL,
    WEB_SEARCH_PRICE_PER_SEARCH,
)
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import Agent2Input, Agent2Result, Candidate, UserUnderstandingOutput


# ============================================================================
# [CORE] System Prompt — Step 1: Web Research
# ============================================================================
# STRATEGY: "Survey the landscape, then pick the best fits"
#
#   1. Search for 1-2 comparison/roundup articles for the user's capabilities
#   2. From the search results, identify ALL players mentioned across articles
#   3. Pick the 5-7 best fits for THIS user's specific sub-tasks
#
# WHY THIS WORKS:
#   Comparison articles are curated by humans who know the space. A single
#   search for "best document OCR APIs 2026" surfaces the 10-15 real players
#   in one shot. We then use Claude's judgment (search results + training
#   knowledge) to pick the ones that best match the user's sub-tasks.
#
# WHY NO WEB FETCH?
#   Web fetch loads full page content into context (~5-25K tokens per page).
#   That content stays in context for ALL subsequent iterations of the
#   server-side tool loop — the #1 cause of token explosion.
#   Search results give us enough to IDENTIFY candidates.
#   Validation (checking real API docs) is Agent 4's job, not ours.
#
# DYNAMIC FILTERING (web_search_20260209):
#   Even without web fetch, dynamic filtering helps. When a search returns
#   10 results with encrypted_content, Claude can write code to keep only
#   the relevant results and discard the rest BEFORE they accumulate in
#   context. This reduces token cost per search iteration.
#   Requires Sonnet 4.6 or Opus 4.6 (code execution is auto-injected).
# ============================================================================

RESEARCH_SYSTEM_PROMPT = """You are the Research Agent for PuzzleEval — a product expert who finds AI solutions tailored to each user's specific situation.

You are NOT finding the best services in the world. You are finding the best services FOR THIS USER — their background, technical ability, domain, and use case.

## Phase 1: Search
Do 1-2 searches to find comparison/roundup articles listing the market players.
- Search: "best [main capability] API tools 2026" or "[capability] API comparison"
- If the user has a second distinct sub-task, do ONE more search for that capability
- STOP after 2-3 searches. Do not search for individual tools.

## Phase 2: Collect Candidate Pool
From search results, list EVERY tool/service mentioned. Don't filter yet — this is your raw pool (typically 15-30 candidates).

## Phase 3: Score Each Candidate (the critical step)

For EVERY candidate in your pool, score on three dimensions (0-10):

### Dimension 1: Capability Fit (0-10)
How well does this service handle the user's specific sub-tasks?
- 9-10: Covers all sub-tasks with production-quality, purpose-built features
- 6-8: Covers most sub-tasks well, may need minor workarounds
- 3-5: Covers some sub-tasks, significant gaps or limitations
- 1-2: Barely relevant, would require heavy customization

### Dimension 2: Adoption Fit (0-10)
How realistic is it for THIS SPECIFIC USER to get from zero to a working integration?
Consider: their technical level, what setup the service requires, documentation quality, SDK availability.
- 9-10: This user could be up and running in under an hour (signup → key → first API call)
- 6-8: Manageable for this user with some learning, clear docs available
- 3-5: Significant effort for this user, requires skills they may not have
- 1-2: This user would need to hire someone to set it up

### Dimension 3: Use Case Fit (0-10)
Is this service designed for someone like this user, in their domain, solving their kind of problem?
- 9-10: Built specifically for this use case and user profile (e.g., invoice tool for accountants)
- 6-8: General-purpose but commonly used for this use case
- 3-5: Can technically do it but designed for a different audience/use case
- 1-2: Enterprise/developer infrastructure tool being repurposed

## Phase 4: Weight the Dimensions for THIS User

Based on the user's context, decide how much each dimension matters:

Example weights:
- Non-technical small business owner: Adoption 40%, Use Case 35%, Capability 25%
  (they need something they can actually use, even if it's not the most powerful)
- Senior engineer building a pipeline: Capability 50%, Use Case 30%, Adoption 20%
  (they need raw power and can handle any setup)
- Freelancer with some tech skills: Use Case 40%, Capability 30%, Adoption 30%
  (domain fit matters most for their workflow)

State your chosen weights and WHY they fit this user.

## Phase 5: Rank and Select Top 5-7

Composite score = (capability × w1) + (adoption × w2) + (use_case × w3)

Show the scoring table. Select the top 5-7 by composite score.

Hard requirements (candidates must have):
- Public API (V0 scope)
- At least 4 different providers in the final 5-7

## Output Format

1. **Candidate pool**: List ALL tools found in search results
2. **Weights**: Your chosen weights + reasoning for this user
3. **Scoring table**: Every candidate scored on 3 dimensions + composite
4. **Selected 5-7**: The top candidates with: name, provider, description, API docs URL, pricing, sub-tasks covered, and WHY this candidate fits this user
5. **Coverage analysis**: Which sub-tasks are well-covered vs underserved
"""


# ============================================================================
# [CORE] System Prompt — Step 2: Structure the Findings
# ============================================================================
# This prompt takes the raw text findings from Step 1 and formats them
# into our Pydantic schema. It's a simple formatting/scoring task —
# all the hard work (searching, fetching, validating) was done in Step 1.
# ============================================================================

STRUCTURE_SYSTEM_PROMPT = """You are a data structuring assistant. Take the research findings (which include a scoring table) and structure them into the exact JSON format required.

## How to Map Scores to Schema Fields

The research findings include dimensional scores (capability, adoption, use case) and a composite score for each selected candidate. Map these to the schema as follows:

### relevance_score (0.0 to 1.0)
Use the COMPOSITE user-fit score from the scoring table, normalized to 0-1 scale.
- Composite 8-10 → relevance_score 0.8-1.0
- Composite 6-7.9 → relevance_score 0.6-0.79
- Composite 4-5.9 → relevance_score 0.4-0.59
- Below 4 → relevance_score below 0.4

This score now represents "fit for this specific user" not "general capability."

### adoption_difficulty
Derive from the ADOPTION FIT dimensional score:
- Adoption score 7-10 → "easy"
- Adoption score 4-6 → "medium"
- Adoption score 1-3 → "hard"

This is an objective description of setup complexity, useful for the final report.

## Field Guidelines

- api_available: Should be True for all candidates (V0 scope)
- api_docs_url: Use the URL from the research findings, or null if unconfirmed
- pricing_model: "per-token", "per-request", "per-page", "monthly", "usage-based", "free-tier", or "freemium"
- relevant_subtasks: Use the EXACT sub-task description strings from the user's request
- source: URL where the candidate was found during research

## Coverage Notes
Summarize which sub-tasks are well-covered vs underserved. Note the dimension weights used and why.
"""


# ============================================================================
# Tool Configuration — Web Search Only (basic version)
# ============================================================================
# SEARCH ONLY, NO FETCH. Search results include encrypted_content which
# gives Claude substantial page content — enough to identify candidates.
# Full page fetching (web_fetch) is reserved for Agent 4 (Screening).
#
# WHY BASIC (web_search_20250305) instead of dynamic filtering (20260209)?
#   With our conservative setup (3 searches, no fetch), search results are
#   only ~5-7K tokens. Dynamic filtering adds ~8 extra server-side code
#   execution iterations to filter that small payload — the overhead costs
#   MORE than the savings. Tested: basic = ~$0.10-0.15, filtering = ~$0.38.
#
#   WHEN TO SWITCH TO DYNAMIC FILTERING:
#   If we increase max_uses beyond 5, or add web_fetch back, switch to
#   web_search_20260209 + RESEARCH_MODEL (Sonnet 4.6). Dynamic filtering
#   pays off when there's heavy content to filter.
#
# Basic search works on ALL models, so Step 1 can use DEFAULT_MODEL
# (Sonnet 4.5) — same as the rest of the pipeline.
# ============================================================================

# ---------------------------------------------------------------------------
# Web Search: $10/1000 searches = $0.01 each
# max_uses=3: one comparison search + 1-2 targeted follow-ups.
# ---------------------------------------------------------------------------
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,
}


# Caching is DISABLED for Agent 2 (single-shot, no conversation loop).
# There's no repeated context to cache across turns.
CACHING_ENABLED = False

# Max tokens for each step.
# Step 1 is kept modest — Claude should summarize candidates concisely,
# not write essays. Lower max_tokens also signals "be brief."
RESEARCH_MAX_TOKENS = 5000  # Actual output ~3,100 tokens; 60% headroom
STRUCTURE_MAX_TOKENS = 4096

# ---------------------------------------------------------------------------
# pause_turn Safety Valve
# ---------------------------------------------------------------------------
# When the server-side tool loop gets too long, the API returns with
# stop_reason="pause_turn". We MUST continue (re-send full context) or
# accept partial results. We allow at most 1 continuation — with tight
# max_uses, the API should almost always finish in one call.
#
# WHY 1? Each continuation re-sends the ENTIRE context (all previous
# search results + fetch results). Even one continuation doubles the
# token cost. With max_uses=4 search + 3 fetch, one call is usually enough.
# ---------------------------------------------------------------------------
MAX_CONTINUATIONS = 1


# ============================================================================
# [CORE] Build the research request message
# ============================================================================
# Serializes Agent 1's output into a clear, structured text prompt that
# tells Claude exactly what to search for. Each sub-task gets its own
# section with search keywords, making it easy for Claude to plan its
# search queries.
# ============================================================================

def _build_research_message(user_understanding: UserUnderstandingOutput) -> str:
    """
    Convert Agent 1's structured output into a research request message.

    The message is formatted to make Claude's job easy:
    - Summary at the top for context
    - Each sub-task listed with its capability and search keywords
    - Top-level keywords for all-in-one solution searches
    - Constraints that might affect which candidates are viable
    """
    # ── CORE: Build the sub-tasks section ──
    subtask_lines = []
    for i, st in enumerate(user_understanding.sub_tasks, 1):
        subtask_lines.append(
            f"{i}. {st.description}\n"
            f"   Capability: {st.capability}\n"
            f"   Search keywords: {', '.join(st.search_keywords)}"
        )
    subtasks_text = "\n".join(subtask_lines)

    # ── CORE: Build the constraints section ──
    constraints = user_understanding.constraints
    budget = constraints.budget_range or "Not specified"
    tech_level = constraints.technical_level or "Not specified"
    integrations = (
        ", ".join(constraints.integration_requirements)
        if constraints.integration_requirements
        else "None specified"
    )

    # ── CORE: Build technical level guidance ──
    tech_guidance = _tech_level_guidance(tech_level)

    # ── CORE: Assemble the full message ──
    message = f"""## What the User Needs
{user_understanding.summary}

## Who This User Is
- **Technical level:** {tech_level}
- **Domain:** {user_understanding.domain}
- **Budget:** {budget}
- **Integration requirements:** {integrations}
- **Summary:** {user_understanding.summary}
{tech_guidance}

## Sub-Tasks to Find Solutions For
{subtasks_text}

## Top-Level Search Keywords (for all-in-one solutions)
{', '.join(user_understanding.search_keywords)}

## Workflow Context
{user_understanding.workflow_summary or "No workflow document provided."}

---

Find 5-7 AI services with public APIs that are the best fit for THIS user — not the best in the world, but the best for their specific background, technical ability, and use case."""

    return message


def _tech_level_guidance(tech_level: str) -> str:
    """Generate contextual guidance (not rules) based on the user's technical level."""
    if tech_level == "non-technical":
        return (
            "This person is not technical. Think about what that means for adoption: "
            "they'll likely need simple signup flows, API key auth, clear REST examples, "
            "and good documentation. Services requiring cloud infrastructure setup "
            "(IAM roles, service accounts, region configuration) will be much harder "
            "for them — consider whether simpler alternatives exist that do the same thing."
        )
    elif tech_level == "some-technical":
        return (
            "This person has some technical ability — they can follow documentation, "
            "install SDKs, and handle moderate configuration. OAuth flows and platform "
            "accounts are manageable if well-documented."
        )
    elif tech_level == "technical":
        return (
            "This person is technically proficient. Complex setups, cloud-native "
            "services, and infrastructure-heavy solutions are fine. They can evaluate "
            "on capability and scale rather than ease of setup."
        )
    return (
        "Technical level not specified. Use the user's description and domain to "
        "infer what level of complexity is appropriate. When in doubt, include "
        "a mix — some simple options and some advanced ones."
    )


# ============================================================================
# [CORE] Extract text from a mixed-content response
# ============================================================================
# When Claude uses server tools (web_search, web_fetch), the response
# contains mixed content blocks:
#   - "text" blocks: Claude's own writing (search reasoning, candidate descriptions)
#   - "server_tool_use" blocks: search/fetch tool invocations
#   - "web_search_tool_result" blocks: search results (encrypted content)
#   - "web_fetch_tool_result" blocks: fetched page content
#   - "code_execution_tool_result" blocks: dynamic filtering code output
#
# We only need the text blocks — those contain Claude's analysis and
# candidate descriptions that we'll structure in Step 2.
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
# [CORE] Main function — this is the entry point
# ============================================================================
#
# THE CORE LOGIC IS 6 LINES (marked with ★ below). Everything else is
# logging, error handling, and cost tracking — necessary for production
# but not for understanding what Agent 2 does.
#
# TWO API CALLS:
#   Step 1: client.messages.create() with web tools → raw text findings
#   Step 2: client.messages.parse() with output_format → structured JSON
#
# ============================================================================

def run_research_agent(input_data: Agent2Input) -> Agent2Result:
    """
    Run Agent 2. Takes Agent 1's structured understanding, searches the web
    for matching AI services, returns structured candidate list.

    Two-step process:
      Step 1: Web research using search + fetch tools with dynamic filtering
      Step 2: Structure the findings into Agent2Result via structured output
    """
    # ★ CORE LINE 1: Create the API client
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # [logging] Set up logger for this agent
    logger = get_logger("agent_2_research")
    logger.info("Agent 2 started", extra={
        "operation": "agent_start", "trace_id": input_data.trace_id,
    })

    # ★ CORE LINE 2: Build the research request message
    research_message = _build_research_message(input_data.user_understanding)

    # ======================================================================
    # STEP 1: Web Research (search-only + pause_turn handling)
    # ======================================================================
    # HOW THIS WORKS (same as Claude.ai):
    #   We give Claude web_search (search only, no page fetching).
    #   Claude does 1-3 searches, reads result snippets, combines with
    #   training knowledge, and writes up candidate descriptions.
    #
    # WHY pause_turn HANDLING:
    #   If the server-side loop takes too long, the API returns with
    #   stop_reason="pause_turn". We allow 1 continuation max.
    #   With only 3 searches allowed, this rarely triggers.
    # ======================================================================

    # ★ CORE LINE 3: Call Claude with web search + fetch tools
    step1_start = time.time()
    messages = [{"role": "user", "content": research_message}]
    research_response = None
    total_web_searches = 0
    total_cost = 0.0

    # [pause_turn] Allow limited continuations if the API pauses mid-research
    for continuation in range(MAX_CONTINUATIONS + 1):
        try:
            research_response = client.messages.create(
                model=RESEARCH_MODEL,  # Sonnet 4.6 for better search quality
                max_tokens=RESEARCH_MAX_TOKENS,
                system=[{"type": "text", "text": RESEARCH_SYSTEM_PROMPT}],
                messages=messages,
                tools=[WEB_SEARCH_TOOL],
            )

        # [error handling] Same pattern as Agent 1 — different error types
        # for different retry strategies
        except anthropic.RateLimitError as e:
            logger.error("Rate limit hit (Step 1: research)", extra={
                "operation": "llm_call_research", "trace_id": input_data.trace_id,
                "error": str(e), "error_type": "RateLimitError",
            })
            raise AgentRateLimitError(
                message=f"Rate limit exceeded during web research: {e}",
                agent_name="research", trace_id=input_data.trace_id,
            )
        except anthropic.APIConnectionError as e:
            logger.error("API connection failed (Step 1: research)", extra={
                "operation": "llm_call_research", "trace_id": input_data.trace_id,
                "error": str(e), "error_type": "APIConnectionError",
            })
            raise AgentAPIError(
                message=f"Failed to connect to Anthropic API during research: {e}",
                agent_name="research", trace_id=input_data.trace_id,
            )
        except anthropic.APIStatusError as e:
            logger.error("API error (Step 1: research)", extra={
                "operation": "llm_call_research", "trace_id": input_data.trace_id,
                "error": str(e), "error_type": "APIStatusError",
            })
            raise AgentAPIError(
                message=f"Anthropic API error during research: {e}",
                agent_name="research", trace_id=input_data.trace_id,
            )

        # [logging] Log this call's metrics
        step1_call_cost = log_llm_call(
            logger=logger, response=research_response, model=RESEARCH_MODEL,
            trace_id=input_data.trace_id, start_time=step1_start,
            operation=f"research_web_search_iter{continuation}",
        )
        total_cost += step1_call_cost

        # [cost tracking] Accumulate web search usage across continuations
        server_tool_use = getattr(research_response.usage, "server_tool_use", None)
        if server_tool_use:
            total_web_searches += getattr(server_tool_use, "web_search_requests", 0) or 0

        # [pause_turn] If the API finished, break out of the loop
        if research_response.stop_reason != "pause_turn":
            break

        # [pause_turn] API paused — continue by re-sending with assistant's partial response
        logger.info("pause_turn received, continuing research", extra={
            "operation": "pause_turn_continue",
            "trace_id": input_data.trace_id,
            "continuation": continuation + 1,
        })
        messages = [
            {"role": "user", "content": research_message},
            {"role": "assistant", "content": research_response.content},
        ]

    # [cost tracking] Log total web search usage across all continuations
    if total_web_searches > 0:
        logger.info("Web tool usage (total)", extra={
            "operation": "web_tool_usage",
            "trace_id": input_data.trace_id,
            "web_search_requests": total_web_searches,
            "web_search_cost_usd": round(total_web_searches * WEB_SEARCH_PRICE_PER_SEARCH, 4),
        })

    # ★ CORE LINE 4: Extract text findings from the mixed response
    findings_text = _extract_text_from_response(research_response)

    # [error handling] If Step 1 produced no text, something went wrong
    if not findings_text.strip():
        logger.error("Research produced no text output", extra={
            "operation": "research_extraction", "trace_id": input_data.trace_id,
            "stop_reason": research_response.stop_reason,
            "content_block_count": len(research_response.content),
        })
        raise AgentOutputError(
            message=(
                f"Web research returned no text findings. "
                f"stop_reason={research_response.stop_reason}, "
                f"content_blocks={len(research_response.content)}"
            ),
            agent_name="research", trace_id=input_data.trace_id,
        )

    logger.info("Research findings extracted", extra={
        "operation": "research_extraction",
        "trace_id": input_data.trace_id,
        "findings_length": len(findings_text),
    })

    # ======================================================================
    # STEP 2: Structure the Findings
    # ======================================================================
    # Takes the raw text findings from Step 1 and passes them through
    # structured output to get guaranteed JSON matching our Pydantic schema.
    # This is cheap — just reformatting data that's already been gathered.
    # Uses DEFAULT_MODEL (Sonnet 4.5) since this is a simple formatting task.
    # ======================================================================

    # ★ CORE LINE 5: Call Claude with structured output to format findings
    step2_start = time.time()
    try:
        structure_response = client.messages.parse(
            model=DEFAULT_MODEL,
            max_tokens=STRUCTURE_MAX_TOKENS,
            system=[{"type": "text", "text": STRUCTURE_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": findings_text}],
            output_format=Agent2Result,
        )

    # [error handling] Same error pattern for Step 2
    except anthropic.RateLimitError as e:
        logger.error("Rate limit hit (Step 2: structuring)", extra={
            "operation": "llm_call_structure", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "RateLimitError",
        })
        raise AgentRateLimitError(
            message=f"Rate limit exceeded during structuring: {e}",
            agent_name="research", trace_id=input_data.trace_id,
        )
    except anthropic.APIConnectionError as e:
        logger.error("API connection failed (Step 2: structuring)", extra={
            "operation": "llm_call_structure", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIConnectionError",
        })
        raise AgentAPIError(
            message=f"Failed to connect to Anthropic API during structuring: {e}",
            agent_name="research", trace_id=input_data.trace_id,
        )
    except anthropic.APIStatusError as e:
        logger.error("API error (Step 2: structuring)", extra={
            "operation": "llm_call_structure", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIStatusError",
        })
        raise AgentAPIError(
            message=f"Anthropic API error during structuring: {e}",
            agent_name="research", trace_id=input_data.trace_id,
        )

    # [logging] Log Step 2 call metrics
    step2_cost = log_llm_call(
        logger=logger, response=structure_response, model=DEFAULT_MODEL,
        trace_id=input_data.trace_id, start_time=step2_start,
        operation="research_structure",
    )
    total_cost += step2_cost

    # [cost tracking] Add web search fees
    web_search_cost = total_web_searches * WEB_SEARCH_PRICE_PER_SEARCH
    total_cost += web_search_cost

    # ★ CORE LINE 6: Return the parsed result
    result = structure_response.parsed_output

    # [error handling] Defensive check for truncated/refused responses
    if result is None:
        logger.error("Parsed output is None (Step 2: structuring)", extra={
            "operation": "output_validation", "trace_id": input_data.trace_id,
            "error": "parsed_output is None",
            "stop_reason": structure_response.stop_reason,
        })
        raise AgentOutputError(
            message=(
                f"Structuring step returned no parsed output. "
                f"stop_reason={structure_response.stop_reason}"
            ),
            agent_name="research", trace_id=input_data.trace_id,
        )

    # [cost tracking] Set total cost on the result
    result.cost_usd = round(total_cost, 6)

    logger.info("Agent 2 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "candidate_count": len(result.candidates),
    })

    return result


# ============================================================================
# TEMPORARY TESTING SHIM — Registry Candidate Injection
# ============================================================================
# Injects any provider_registry.json providers missing from Agent 2's results
# so that Agent 4/5 always see all registered providers. This ensures test
# coverage for all providers we have API keys for, even if Agent 2's web
# search doesn't discover them.
#
# TO REMOVE THIS SHIM:
#   1. Delete this entire function (inject_registry_candidates)
#   2. Delete the call in cli.py (search for "inject_registry_candidates")
#   3. Remove "Candidate" from the import at the top of this file
#   That's it — no other code references this function.
# ============================================================================


def inject_registry_candidates(
    agent2_result: Agent2Result,
    registry_path: str | None = None,
) -> Agent2Result:
    """
    Append any provider_registry providers missing from agent2_result.candidates.

    Matching is case-insensitive on provider name. Injected candidates get
    source="provider_registry_injection" so they're clearly identifiable.

    Returns a NEW Agent2Result (does not mutate the original).
    """
    import json
    from pathlib import Path
    from puzzleeval.config import PROVIDER_REGISTRY_PATH

    reg_path = Path(registry_path or PROVIDER_REGISTRY_PATH)
    if not reg_path.exists():
        return agent2_result

    try:
        raw = json.loads(reg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return agent2_result

    registry_providers = raw.get("providers", {})
    if not registry_providers:
        return agent2_result

    # Build a set of normalized names already present in Agent 2's results
    existing_names = set()
    for c in agent2_result.candidates:
        existing_names.add(c.name.lower().strip())
        existing_names.add(c.provider.lower().strip())

    injected = []
    for provider_key, _data in registry_providers.items():
        normalized_key = provider_key.lower().strip()
        if normalized_key in existing_names:
            continue

        injected.append(Candidate(
            name=provider_key,
            provider=provider_key,
            description=(
                f"{provider_key} — injected from provider_registry.json for "
                f"testing. Agent 4 will verify API docs."
            ),
            api_available=True,
            api_docs_url=None,
            pricing_model="unknown",
            pricing_details=None,
            claimed_capabilities=["document processing", "data extraction"],
            relevance_score=0.99,  # TESTING SHIM — guarantees top-N selection by Agent 5
            adoption_difficulty="medium",
            relevant_subtasks=[],
            source="provider_registry_injection",
        ))

    if not injected:
        return agent2_result

    return Agent2Result(
        candidates=agent2_result.candidates + injected,
        search_approach=(
            agent2_result.search_approach
            + f" [+{len(injected)} injected from provider_registry]"
        ),
        coverage_notes=agent2_result.coverage_notes,
    )
