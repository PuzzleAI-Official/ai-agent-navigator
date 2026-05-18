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

try:
    import anthropic
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal test envs
    from puzzleeval.anthropic_client import anthropic  # type: ignore

from functools import lru_cache
from importlib import resources

from puzzleeval.config import (
    ANTHROPIC_API_KEY,
    DEFAULT_MODEL,
    RESEARCH_DUAL_SEARCH_ENABLED,
    RESEARCH_MODEL,
    WEB_SEARCH_PRICE_PER_SEARCH,
)
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import (
    Agent2Input,
    Agent2Result,
    Candidate,
    UserAddedCandidate,
    UserUnderstandingOutput,
    WorkflowBlueprint,
)


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
# WHY NOT DYNAMIC FILTERING (20260209)?
#   The 20260209 web tools auto-inject code_execution for dynamic
#   filtering. In real runs this caused: (a) 3-5 min latency per Agent 2
#   call waiting on sandbox spin-up, (b) 400 "container_id required"
#   errors that cascade across sub-agents, (c) Agent 2's non-beta
#   messages.create hangs silently when `container` is passed. Basic
#   web_search_20250305 has none of those failure modes and Agent 2's
#   small search payload doesn't benefit from filtering anyway.
# ============================================================================

@lru_cache(maxsize=1)
def _load_research_system_prompt() -> str:
    """Load RESEARCH_SYSTEM_PROMPT from templates/research_system.md (Phase 7)."""
    return resources.files("puzzleeval.agents.agent2").joinpath(
        "templates", "research_system.md",
    ).read_text(encoding="utf-8")


RESEARCH_SYSTEM_PROMPT = _load_research_system_prompt()


# ============================================================================
# [CORE] System Prompt — Step 2: Structure the Findings
# ============================================================================
# This prompt takes the raw text findings from Step 1 and formats them
# into our Pydantic schema. It's a simple formatting/scoring task —
# all the hard work (searching, fetching, validating) was done in Step 1.
# ============================================================================

@lru_cache(maxsize=1)
def _load_structure_system_prompt() -> str:
    """Load STRUCTURE_SYSTEM_PROMPT from templates/structure_system.md (Phase 7)."""
    return resources.files("puzzleeval.agents.agent2").joinpath(
        "templates", "structure_system.md",
    ).read_text(encoding="utf-8")


STRUCTURE_SYSTEM_PROMPT = _load_structure_system_prompt()


# ============================================================================
# Tool Configuration — Web Search Only (basic version)
# ============================================================================
# SEARCH ONLY, NO FETCH. Search results include encrypted_content which
# gives Claude substantial page content — enough to identify candidates.
# Full page fetching (web_fetch) is reserved for Agent 4 (Screening).
#
# Tool version: web_search_20250305 (basic, GA).
#
# Why not the 20260209 dynamic-filtering version? Real-run traces
# d3b49875 + 4068e872 showed the 20260209 pair introduces real
# operational gaps that outweighed the ~11% quality lift / ~24% token
# savings on our workload:
#   1. The auto-injected code_execution sandbox requires `container_id`
#      threading across every turn in a conversation. Missing threading
#      on ANY sub-agent (main builder, Agent 4 verify, ask_research)
#      produced a 400 "container_id is required when there are pending
#      tool uses generated by code execution with tools" — an error
#      class that didn't exist before the upgrade.
#   2. Agent 2's non-beta `client.messages.create` silently hangs the
#      socket when `container=<id>` is passed — 8+ min stall with zero
#      observable progress on real runs.
#   3. Dynamic filtering adds 3-5 min of sandbox-compile latency per
#      Agent 2 search call, which inflates wall-clock for the entire
#      pipeline.
# Basic search works on every model with no new failure modes.
# ============================================================================

# ---------------------------------------------------------------------------
# Web Search: $10/1000 searches = $0.01 each
#
# Single-pass (legacy / 1-scope / no blueprint): max_uses=3 — one comparison
# search + 1-2 targeted follow-ups.
#
# Dual search (Phase 4, N>=2 scope blueprint): max_uses=N+1 — one all-in-one
# survey search + one per-scope search per blueprint step. Capped at
# DUAL_SEARCH_MAX_USES_CEILING so cost stays bounded for unusually large
# blueprints (typical N is 2-5; a 10-scope workflow would still cap at 8).
# ---------------------------------------------------------------------------
SINGLE_SEARCH_MAX_USES = 3
DUAL_SEARCH_MAX_USES_CEILING = 8


def _build_web_search_tool(blueprint: WorkflowBlueprint | None) -> dict:
    """
    Build the web_search server-tool config. When a multi-scope blueprint is
    present AND dual search is enabled, cap max_uses at N+1 (one survey + one
    per scope). Otherwise fall back to SINGLE_SEARCH_MAX_USES.
    """
    step_count = len(blueprint.steps) if blueprint else 0
    if RESEARCH_DUAL_SEARCH_ENABLED and step_count >= 2:
        max_uses = min(step_count + 1, DUAL_SEARCH_MAX_USES_CEILING)
    else:
        max_uses = SINGLE_SEARCH_MAX_USES
    return {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_uses,
    }


# Caching is DISABLED for Agent 2 (single-shot, no conversation loop).
# There's no repeated context to cache across turns.
CACHING_ENABLED = False

# Max tokens for each step.
# Step 1 is kept modest — Claude should summarize candidates concisely,
# not write essays. Lower max_tokens also signals "be brief."
RESEARCH_MAX_TOKENS = 12000  # Adaptive thinking + 4 web_searches + text synthesis;
                              # 5000 was too tight (one observed real run hit
                              # stop_reason=max_tokens with 8 tool_use blocks
                              # and zero text findings — the loop was still
                              # mid-search when budget ran out).
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
    - Workflow blueprint (Phase 3) — scope list that drives Phase 4 dual search
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

    # ── Phase 4: Workflow Blueprint section ──
    # Surfaces the scope list to the research prompt so Claude knows to do
    # dual search (survey + one per scope) and to populate covers_step_ids
    # per candidate. If no blueprint exists, show a single-scope fallback
    # note so the agent takes the legacy path.
    blueprint_section = _format_blueprint_section(user_understanding.workflow)
    scope_pool_instruction = _scope_pool_instruction(user_understanding.workflow)

    # Architecture note: atlas-cache hints removed. Agent 4 no longer
    # produces an atlas; we don't preload provider hints from memdir.
    # Agent 2 does its own web_search each run.
    cached_hints_block = ""

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

{blueprint_section}

## Top-Level Search Keywords (for all-in-one solutions)
{', '.join(user_understanding.search_keywords)}

## Workflow Context
{user_understanding.workflow_summary or "No workflow document provided."}

---

{cached_hints_block}{scope_pool_instruction}"""

    return message


def _format_blueprint_section(workflow: WorkflowBlueprint | None) -> str:
    """
    Render Agent 1's WorkflowBlueprint as a scope table for the research prompt.
    Empty string when no blueprint exists (legacy flow).
    """
    if workflow is None or not workflow.steps:
        return "## Workflow Blueprint\nNone provided — treat the request as a single-scope search and leave covers_step_ids empty for every candidate."

    step_lines = []
    for step in workflow.steps:
        deps = f", depends_on={list(step.depends_on)}" if step.depends_on else ""
        step_lines.append(
            f"- `{step.id}` (role={step.role}, capability=\"{step.capability}\", "
            f"input_from={step.input_from or 'none'}{deps}): {step.description}"
        )
    return (
        "## Workflow Blueprint (Phase 4 dual search)\n"
        + "\n".join(step_lines)
        + "\n\nCover these step_ids in covers_step_ids per candidate. "
        f"Total scopes: {len(workflow.steps)}. Architecture options Agent 1 considered: "
        f"{', '.join(workflow.architecture_options) or 'all_in_one, best_per_step'}."
    )


def _scope_pool_instruction(workflow: WorkflowBlueprint | None) -> str:
    """
    Tail instruction that tells Claude how many candidates to aim for based
    on blueprint size. N=1 → today's 5-7 behavior. N>=2 → larger pool.
    """
    n = len(workflow.steps) if workflow else 0
    if not RESEARCH_DUAL_SEARCH_ENABLED or n < 2:
        return (
            "Find 5-7 AI services with public APIs that are the best fit for THIS "
            "user — not the best in the world, but the best for their specific "
            "background, technical ability, and use case."
        )
    return (
        f"This is a {n}-scope workflow. Run dual search: ONE all-in-one survey + "
        f"ONE per-scope search per step ({n + 1} searches total, capped). Produce "
        f"8-12 candidates TOTAL with per-scope diversity — aim for 3+ candidates "
        f"whose covers_step_ids includes each scope (specialists + any all-in-ones "
        f"that surface at that scope). Populate covers_step_ids and "
        f"coverage_confidence for every candidate."
    )


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


def _salvage_findings_from_tool_uses(response: anthropic.types.Message) -> str:
    """Extract usable context from tool_use queries when no text block is present.

    When the agentic web-search loop runs out of token budget mid-search
    (stop_reason=max_tokens with content blocks dominated by tool_use /
    tool_result), there's no synthesized findings paragraph for Step 2 to
    structure. The OLD behavior was to feed the raw search queries to the
    structurer, which HALLUCINATED candidates from query text to fill the
    5-7 quota — producing plausible-looking fake providers with invented
    api_docs_url values.

    The NEW behavior: we emit the queries as CONTEXT but explicitly instruct
    the structurer to emit ``candidates=[]`` and put the situation in
    ``coverage_notes``. The coverage_gap SSE event will then tell the user
    "no candidates found — try broadening your request." Honest empty result
    beats fabricated candidates.
    """
    bits: list[str] = []
    for block in response.content:
        btype = getattr(block, "type", "")
        if btype == "text":
            txt = getattr(block, "text", "") or ""
            if txt.strip():
                bits.append(txt.strip())
        elif btype == "server_tool_use":
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, dict):
                query = tool_input.get("query") or tool_input.get("url") or ""
                if query:
                    bits.append(f"Web search query: {query}")
    if not bits:
        return ""
    header = (
        "## DEGRADED MODE — research loop terminated before synthesizing findings\n\n"
        "The web research loop hit stop_reason=max_tokens or pause_turn before\n"
        "it could synthesize a findings paragraph. The queries below are all we\n"
        "recovered. Because we have NO verified provider snippets, you MUST NOT\n"
        "fabricate candidates from the query text — that would present invented\n"
        "providers with made-up docs URLs as real options.\n\n"
        "**Instruction to structurer:** emit `candidates=[]` (empty list). In\n"
        "`coverage_notes`, write: \"Research degraded — no candidates verified.\n"
        "User-visible message: web search was interrupted (likely rate-limited\n"
        "or timed out). Retry the run or add candidates manually via the\n"
        "SelectionPanel.\" Set `search_approach='degraded_no_results'`.\n\n"
        "Raw queries the model issued (for diagnostic context only, NOT to\n"
        "be converted into candidates):\n\n"
    )
    return header + "\n".join(f"- {b}" for b in bits)


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
    # Server-tool timeout tier — 420 s (7 min). Agent 2's research pass
    # runs the Anthropic server-side web_search loop which can legitimately
    # take 2-5 minutes on multi-scope blueprints (up to 8 searches × 5-15 s
    # each + Opus reasoning between). The default 120 s collapsed real runs
    # mid-research. See anthropic_client.py::SERVER_TOOL_TIMEOUT_S.
    from puzzleeval.anthropic_client import build_client, SERVER_TOOL_TIMEOUT_S
    client = build_client(
        api_key=ANTHROPIC_API_KEY,
        timeout=SERVER_TOOL_TIMEOUT_S,
    )

    # [logging] Set up logger for this agent
    logger = get_logger("agent_2_research")
    logger.info("Agent 2 started", extra={
        "operation": "agent_start", "trace_id": input_data.trace_id,
    })

    # ★ CORE LINE 2: Build the research request message
    research_message = _build_research_message(input_data.user_understanding)

    # ── Phase 4: dynamic web_search max_uses based on blueprint size ──
    # 1-scope / no blueprint → single-pass (max_uses=3). N>=2 → dual search
    # with max_uses=N+1 (one survey + one per scope, capped at 8). This is
    # the ONLY place the dual-search flag affects tool behavior.
    blueprint = input_data.user_understanding.workflow
    web_search_tool = _build_web_search_tool(blueprint)
    step_ids = [s.id for s in blueprint.steps] if blueprint else []

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
    # NOTE on container_id threading: Agent 5 (builder loop) and Agent 4
    # (per-candidate verify) MUST forward `response.container.id` across
    # turns to prevent the 400 "container_id is required" that ElevenLabs
    # hit in trace d3b49875. Agent 2 does NOT need it — it uses the
    # non-beta `client.messages.create` entrypoint with at most one
    # pause_turn continuation. Passing `container=<id>` into the non-beta
    # messages.create silently hangs the socket (observed: run 31f1af7c
    # stuck 8+ min with zero progress). Since Agent 2 rarely continues,
    # container persistence delivers ~no value here vs Agent 5's 17-turn
    # loop. Keep Agent 2 simple: no container forwarding, no beta endpoint.

    # [pause_turn] Allow limited continuations if the API pauses mid-research
    for continuation in range(MAX_CONTINUATIONS + 1):
        try:
            from puzzleeval.agent_preamble import with_preamble
            from puzzleeval.config import output_config_for_request
            from puzzleeval.anthropic_client import retry_on_transient_5xx
            _kwargs_research: dict[str, object] = {}
            _ocfg = output_config_for_request()
            if _ocfg:
                _kwargs_research["output_config"] = _ocfg
            # Wrap Step 1 in retry_on_transient_5xx so a brief Anthropic 500
            # window (observed: seconds-scale degraded mode) doesn't forfeit
            # the expensive search pass. The SDK's internal retry happens
            # within milliseconds which is too fast to span a real incident.
            def _step1_call():
                return client.messages.create(
                    model=RESEARCH_MODEL,  # Sonnet 4.6 for better search quality
                    max_tokens=RESEARCH_MAX_TOKENS,
                    system=[{"type": "text", "text": with_preamble(RESEARCH_SYSTEM_PROMPT)}],
                    messages=messages,
                    tools=[web_search_tool],
                    # Adaptive thinking — Sonnet reasons between tool calls about which
                    # search to run next + how to interpret results. Same mechanism
                    # Agent 5's builder loop uses; Claude Code uses native thinking
                    # whenever the model is asked to plan multi-step work.
                    thinking={"type": "adaptive"},
                    **_kwargs_research,
                )

            research_response = retry_on_transient_5xx(
                _step1_call,
                trace_id=input_data.trace_id,
                operation_label="research_step1",
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

    # [error handling] If Step 1 produced no text and we genuinely have
    # nothing to structure, fall back gracefully: synthesize a minimal
    # findings paragraph from any tool_use queries the model issued so
    # downstream Step 2 has SOMETHING to structure. This converts a
    # hard pipeline failure (one observed cause: stop_reason=max_tokens
    # mid-tool-loop) into a degraded-but-useful result that surfaces
    # whatever the model managed to research before running out.
    if not findings_text.strip():
        salvaged = _salvage_findings_from_tool_uses(research_response)
        if salvaged.strip():
            logger.warning(
                "Research returned no text — salvaged findings from tool_use queries",
                extra={
                    "operation": "research_salvage", "trace_id": input_data.trace_id,
                    "stop_reason": research_response.stop_reason,
                    "content_block_count": len(research_response.content),
                    "salvaged_chars": len(salvaged),
                },
            )
            findings_text = salvaged
        else:
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
    # Wrapped in parse_with_fallback so that when Agent2Result's compiled
    # grammar exceeds Anthropic's size/timeout budget, we fall back to a
    # non-strict tool-call shape and validate the JSON through Pydantic
    # post-hoc — same final object, no hard failure.
    #
    # ``prefer_non_strict=True``: Agent2Result's schema (list[Candidate]
    # with ~15 fields each, including nested PricingBreakdown +
    # InteractionModel + UserSelectableParam[]) reliably overflows
    # Anthropic's compiled-grammar budget — every Agent 2 run hit the
    # 400 then fell back to non-strict. Skipping the doomed strict
    # attempt saves ~30-60s per run plus one wasted API call. The
    # non-strict path is what we already do successfully; this just
    # stops trying strict first.
    #
    # Retry on transient 5xx via retry_on_transient_5xx: Anthropic
    # occasionally returns HTTP 500 "Internal server error" for seconds at
    # a time, and the SDK's internal max_retries=3 happens within
    # milliseconds — not enough to span a real backend incident. We add
    # a second tier of longer-baked retries (2s → 6s) so Anthropic has
    # time to stabilise rather than forfeit Step 1's ~$0.25 spend on
    # a transient.
    step2_start = time.time()
    try:
        from puzzleeval.structured_output import parse_with_fallback
        from puzzleeval.anthropic_client import retry_on_transient_5xx

        def _step2_call():
            return parse_with_fallback(
                client=client,
                model=DEFAULT_MODEL,
                max_tokens=STRUCTURE_MAX_TOKENS,
                system=[{"type": "text", "text": with_preamble(STRUCTURE_SYSTEM_PROMPT)}],
                messages=[{"role": "user", "content": findings_text}],
                output_format=Agent2Result,
                extra={},
                trace_id=input_data.trace_id,
                prefer_non_strict=True,
            )

        structure_response = retry_on_transient_5xx(
            _step2_call,
            trace_id=input_data.trace_id,
            operation_label="research_structure",
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
        # retry_on_transient_5xx already retried 500/502/503; if we're here
        # the retries were exhausted (or the status was 4xx / other 5xx).
        logger.error("API error (Step 2: structuring)", extra={
            "operation": "llm_call_structure", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIStatusError",
        })
        raise AgentAPIError(
            message=(
                f"Anthropic API error during structuring: {e}. If this is a "
                "5xx transient, re-running the pipeline typically clears it."
            ),
            agent_name="research", trace_id=input_data.trace_id,
        )
    except Exception as e:
        # StructuredOutputFallbackError (no tool_use block, parser couldn't
        # extract a result) is the non-strict-path equivalent of the
        # strict-path "parsed_output is None" check below. Surface as
        # AgentOutputError so the contract for callers is unchanged.
        # Imported lazily to avoid an import cycle through structured_output.
        from puzzleeval.structured_output import StructuredOutputFallbackError
        if isinstance(e, StructuredOutputFallbackError):
            logger.error(
                "Structured output fallback failed (Step 2: structuring)",
                extra={
                    "operation": "output_validation",
                    "trace_id": input_data.trace_id,
                    "error": str(e),
                },
            )
            raise AgentOutputError(
                message=f"Structuring step returned no parsed output. {e}",
                agent_name="research", trace_id=input_data.trace_id,
            )
        raise

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

    # ── Phase 4 post-processing: dedup + coverage normalization ──
    # 1. Dedup by candidate name (case-insensitive). When the same tool
    #    surfaces in both survey + per-scope searches (e.g. Zapier appears
    #    in survey claiming scopes {1,2,3} AND in per-scope-1 search),
    #    the structuring pass may emit two records; we merge here.
    # 2. Enforce coverage_confidence semantics: every scope in
    #    covers_step_ids gets confidence "claimed" (Agent 2 never verifies).
    # 3. Drop scope IDs that aren't in the blueprint (hallucination guard).
    # 4. For single-scope blueprint, ensure every candidate covers step_1
    #    UNLESS covers_step_ids was explicitly empty (legacy flow).
    result.candidates = _normalize_coverage(result.candidates, step_ids)

    # [cost tracking] Set total cost on the result
    result.cost_usd = round(total_cost, 6)

    # [phase fingerprint] Log coverage stats for observability
    if step_ids:
        scope_coverage_counts = {sid: 0 for sid in step_ids}
        for c in result.candidates:
            for sid in c.covers_step_ids:
                if sid in scope_coverage_counts:
                    scope_coverage_counts[sid] += 1
        logger.info("Agent 2 scope coverage", extra={
            "operation": "coverage_stats",
            "trace_id": input_data.trace_id,
            "blueprint_scopes": len(step_ids),
            "scope_coverage_counts": scope_coverage_counts,
            "dual_search_enabled": RESEARCH_DUAL_SEARCH_ENABLED,
            "dual_search_active": RESEARCH_DUAL_SEARCH_ENABLED and len(step_ids) >= 2,
        })

    logger.info("Agent 2 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "candidate_count": len(result.candidates),
    })

    return result


def _normalize_coverage(
    candidates: list[Candidate],
    blueprint_step_ids: list[str],
) -> list[Candidate]:
    """
    Phase 4 post-processing: dedup candidates by name, merge coverage sets
    across duplicates, enforce `coverage_confidence[sid] = "claimed"` for
    every scope in `covers_step_ids`, drop hallucinated step IDs, and
    force-fill single-scope coverage for 1-step blueprints.

    Never shrinks the ranking order — preserves the first occurrence of
    each name and merges coverage FROM later occurrences into the first.
    """
    valid_ids = set(blueprint_step_ids)
    n_scopes = len(blueprint_step_ids)

    # First pass: dedup by normalized name, merging coverage.
    merged: dict[str, Candidate] = {}
    order: list[str] = []
    for c in candidates:
        key = c.name.strip().lower()
        if key in merged:
            # Merge coverage: union covers, union confidence (prefer
            # 'verified' over 'claimed' if both exist — future-proof).
            existing = merged[key]
            # Set-union via temporary set; covers_step_ids is now list[str]
            # (the schema can't carry frozenset because LLM structured output
            # has no native frozenset type — see schemas.py:Candidate).
            new_covers = sorted(set(existing.covers_step_ids) | set(c.covers_step_ids))
            new_conf = dict(existing.coverage_confidence)
            for sid, conf in c.coverage_confidence.items():
                if conf == "verified" or sid not in new_conf:
                    new_conf[sid] = conf
            existing.covers_step_ids = new_covers
            existing.coverage_confidence = new_conf
            # Keep the higher relevance_score of the two (more recent
            # research may re-score the same tool more accurately).
            if c.relevance_score > existing.relevance_score:
                existing.relevance_score = c.relevance_score
        else:
            merged[key] = c
            order.append(key)

    # Second pass: sanitize coverage + interaction-pattern hint on each merged candidate.
    out: list[Candidate] = []
    _valid_hints = {"sync", "async_polling", "other", "unknown"}
    for key in order:
        c = merged[key]
        # Drop hallucinated step IDs that aren't in the blueprint. De-dup +
        # sort so the output is deterministic across runs (callers don't
        # rely on frozenset semantics anymore — schema is list[str]).
        if valid_ids:
            cleaned = sorted({s for s in c.covers_step_ids if s in valid_ids})
        else:
            # No blueprint → legacy flow; covers should be empty.
            cleaned = []
        # 1-scope blueprint: every candidate implicitly covers step_1.
        # (Catches LLM drift where the structuring pass forgot to set it.)
        if n_scopes == 1 and not cleaned:
            cleaned = sorted(blueprint_step_ids)
        c.covers_step_ids = cleaned
        # Normalize confidence: exactly one entry per covered scope,
        # value "claimed" unless already "verified" from a future pass.
        new_conf: dict[str, str] = {}
        for sid in cleaned:
            new_conf[sid] = c.coverage_confidence.get(sid, "claimed")
            # Agent 2 CANNOT produce "verified" — it doesn't fetch docs.
            # Clamp to "claimed" even if the LLM tried to claim otherwise.
            if new_conf[sid] != "claimed":
                new_conf[sid] = "claimed"
        c.coverage_confidence = new_conf
        # Soft-coerce interaction-pattern hint into the documented vocabulary.
        # Any unexpected string from the LLM becomes "unknown" rather than
        # propagating into the harness template as garbage.
        hint = getattr(c, "api_interaction_pattern_hint", "unknown")
        if hint not in _valid_hints:
            c.api_interaction_pattern_hint = "unknown"
        out.append(c)
    return out


# ============================================================================
# Phase 6: User Candidate Injection + Per-Scope Selection
# ============================================================================
# Replaces the retired `inject_registry_candidates` testing shim with two
# explicit, user-driven helpers:
#
#   inject_user_candidates(agent2_result, user_adds) -> Agent2Result
#       Appends user-supplied candidates to the pool. Called by both the
#       API route handler (after the SelectionPanel POST) and the CLI
#       interactive mode. Source is always "user_provided".
#
#   apply_scope_picks(agent2_result, scope_picks, user_added) -> Agent2Result
#       Filters the Agent 2 pool to only candidates the user picked at
#       at least one scope. For kept candidates, reduces covers_step_ids
#       to just the scopes where they were picked (intersection with
#       original coverage for safety). User-added candidates are then
#       injected on top via inject_user_candidates.
#
# Both helpers are pure functions operating on `Agent2Result` — no I/O,
# no logging side effects. All the async/state plumbing lives upstream.
# ============================================================================


def inject_explicit_candidates(
    agent2_result: Agent2Result,
    explicit_names: list[str],
    blueprint_step_ids: list[str] | None = None,
) -> Agent2Result:
    """Auto-inject candidates the user EXPLICITLY mentioned at Agent 1.

    When a user says "compare OpenAI vs ElevenLabs" or "I want to test
    Stripe", Agent 1 writes those names into ``explicit_candidates``.
    Web search is non-deterministic and a brand the user explicitly
    named can easily land outside the top 7 Agent 2 returns — at which
    point the pipeline silently tests random adjacent products and
    the user sees "why didn't you test what I asked for?"

    This helper guarantees every explicit name appears in the candidate
    pool BEFORE the Selection pause, so the user's intent is always
    surfaced. Name matching is case-insensitive substring in both
    directions so "OpenAI" matches both "OpenAI Realtime API" (already
    found by search) and vice versa — we only inject when truly missing.

    Behaviour:
      * If the explicit name is already present (exact or substring) in
        an existing Agent 2 candidate's name/provider — no-op for that
        name. The selection pool already has a representative.
      * If truly missing, inject a synthetic ``Candidate`` with
        ``source="user_explicit"``, ``relevance_score=0.95`` (puts it
        ahead of most Agent 2 finds but behind true user-added via the
        SelectionPanel's "+ Add provider" which uses 0.99), and
        coverage over every blueprint step (we don't know which scope
        the user had in mind, so claim all of them — later
        selected-candidate screening/research will narrow it).

    Returns a NEW Agent2Result. Never mutates the input.
    """
    if not explicit_names:
        return agent2_result

    existing: list[str] = []
    for c in agent2_result.candidates:
        existing.append(c.name.strip().lower())
        existing.append((c.provider or "").strip().lower())

    step_ids = sorted(blueprint_step_ids or [])
    # When there's no blueprint, we still inject but leave covers empty
    # so selected-candidate verification/testing fall back to flat flow.
    covers = step_ids
    confidence = {sid: "claimed" for sid in covers}

    # ── Boost relevance_score on already-present user-explicit candidates ──
    # Real-run signal (trace d3b49875): the user said "Compare OpenAI and
    # ElevenLabs voice stacks" — Agent 2 found both, but ranked them #6
    # (0.61) and #7 (0.445) because the user-fit scoring rewards
    # adoption_difficulty=easy packaged products (ServiceAgent, iVAI, AI
    # Front Desk) for a non-technical user. Those scored 0.84-0.90 and
    # claimed all three default_picks — forcing the user to manually
    # override the selection UI to test what they originally asked for.
    #
    # Fix: whenever an explicit-candidate name substring-matches an
    # already-present candidate, raise that candidate's relevance_score
    # to at least 0.95 (same tier as newly-injected synthetic candidates).
    # Phase 7's per-scope selection uses relevance_score as a dimension,
    # so this ensures the user's named providers land in default_picks.
    # Does NOT invent new candidates or override the user-fit scoring
    # globally — only surfaces what the user explicitly asked for.
    boost_threshold = 0.95
    boosted_names: list[str] = []
    for raw_name in explicit_names:
        norm = raw_name.strip().lower()
        if not norm:
            continue
        # Replace existing candidate objects in-place with boosted copies.
        # We build a new list rather than mutate because Candidate is a
        # Pydantic model — immutable-style replacement is safer.
        replaced: list[Candidate] = []
        for c in agent2_result.candidates:
            name_lc = c.name.strip().lower()
            prov_lc = (c.provider or "").strip().lower()
            matches = (
                (norm in name_lc or name_lc in norm) if name_lc else False
            ) or (
                (norm in prov_lc or prov_lc in norm) if prov_lc else False
            )
            if matches and c.relevance_score < boost_threshold:
                replaced.append(c.model_copy(update={
                    "relevance_score": boost_threshold,
                }))
                boosted_names.append(f"{c.name} ({c.relevance_score:.2f}→{boost_threshold:.2f})")
            else:
                replaced.append(c)
        agent2_result = Agent2Result(
            candidates=replaced,
            search_approach=agent2_result.search_approach,
            coverage_notes=agent2_result.coverage_notes,
            cost_usd=agent2_result.cost_usd,
        )

    new_candidates: list[Candidate] = []
    seen_injections: set[str] = set()
    for raw_name in explicit_names:
        norm = raw_name.strip().lower()
        if not norm:
            continue
        # Substring in either direction: user might say "OpenAI" and
        # Agent 2 found "OpenAI Realtime API", or user says "Stripe
        # Billing" and Agent 2 found just "Stripe" — both count as
        # "already covered".
        already_present = any(
            norm in e or e in norm
            for e in existing if e
        )
        if already_present or norm in seen_injections:
            continue
        seen_injections.add(norm)

        new_candidates.append(Candidate(
            name=raw_name.strip(),
            provider=raw_name.strip(),
            description=(
                f"{raw_name.strip()} — auto-injected because the user "
                "explicitly mentioned this provider in their request. "
                "Selected-candidate screening/research will verify the "
                "public API surface."
            ),
            api_available=True,
            api_docs_url=None,
            pricing_model="unknown",
            pricing_details=None,
            claimed_capabilities=[],
            relevance_score=0.95,
            adoption_difficulty="medium",
            relevant_subtasks=[],
            source="user_explicit",
            covers_step_ids=covers,
            coverage_confidence=confidence,
        ))

    if not new_candidates:
        return agent2_result

    return Agent2Result(
        candidates=agent2_result.candidates + new_candidates,
        search_approach=(
            agent2_result.search_approach
            + f" [+{len(new_candidates)} user-explicit]"
        ),
        coverage_notes=agent2_result.coverage_notes,
        cost_usd=agent2_result.cost_usd,
    )


def inject_user_candidates(
    agent2_result: Agent2Result,
    user_adds: list[UserAddedCandidate],
) -> Agent2Result:
    """
    Append user-supplied candidates to Agent 2's pool.

    - Dedups case-insensitively against existing Agent 2 candidates (a
      user picking a provider Agent 2 already found is silently a no-op
      on the dedup side).
    - Every user-added candidate gets `source="user_provided"`,
      `relevance_score=0.99` (guarantees top-K inclusion at every scope
      it covers), and explicit `covers_step_ids` / `coverage_confidence`
      derived from the user's claim (all "claimed" — selected-candidate
      screening/research verifies later).
    - `api_docs_url` passes through when provided; Agent 4 uses it as
      the starting point for docs-entrypoint/access verification.

    Returns a NEW Agent2Result. Never mutates the input.
    """
    if not user_adds:
        return agent2_result

    existing_names = {
        c.name.strip().lower() for c in agent2_result.candidates
    }
    existing_names.update(
        c.provider.strip().lower() for c in agent2_result.candidates
    )

    new_candidates: list[Candidate] = []
    for ua in user_adds:
        normalized = ua.name.strip().lower()
        if normalized in existing_names:
            # User tried to add a provider Agent 2 already found — skip.
            # Their intent is served by picking the existing entry in the
            # SelectionPanel.
            continue
        # De-dup + sort so coverage stays deterministic regardless of how the
        # caller authored the user-add.
        covers = sorted(set(ua.covers_step_ids))
        confidence = {sid: "claimed" for sid in covers}
        new_candidates.append(Candidate(
            name=ua.name,
            provider=ua.provider,
            description=ua.notes or f"{ua.name} — user-added provider",
            api_available=True,
            api_docs_url=ua.api_docs_url,
            pricing_model="unknown",
            pricing_details=None,
            claimed_capabilities=[],
            relevance_score=0.99,
            adoption_difficulty="medium",
            relevant_subtasks=[],
            source=ua.source,
            covers_step_ids=covers,
            coverage_confidence=confidence,
        ))
        existing_names.add(normalized)

    if not new_candidates:
        return agent2_result

    return Agent2Result(
        candidates=agent2_result.candidates + new_candidates,
        search_approach=(
            agent2_result.search_approach
            + f" [+{len(new_candidates)} user-added]"
        ),
        coverage_notes=agent2_result.coverage_notes,
        cost_usd=agent2_result.cost_usd,
    )


def apply_scope_picks(
    agent2_result: Agent2Result,
    scope_picks: dict[str, list[str]] | None = None,
    user_added: list[UserAddedCandidate] | None = None,
) -> Agent2Result:
    """
    Filter Agent 2's candidate pool to only the candidates the user picked
    at at least one scope; reduce each kept candidate's covers_step_ids to
    just the scopes where it was picked. Then append user-added
    candidates via inject_user_candidates.

    `scope_picks=None` means "pass through" — no filtering, every Agent 2
    candidate survives (used by the CLI --no-interactive path and by the
    API when PUZZLEEVAL_USER_SELECTION_ENABLED=0).

    `scope_picks={}` means "filter everything out" — zero candidates.
    Emit this cautiously; useful for testing but probably an error in
    production.

    Returns a NEW Agent2Result; never mutates the input.
    """
    # Pass-through path
    if scope_picks is None:
        return (
            inject_user_candidates(agent2_result, user_added)
            if user_added
            else agent2_result
        )

    # Invert: candidate_name (normalized) -> set of scope_ids where picked
    picked_scopes: dict[str, set[str]] = {}
    for scope_id, names in scope_picks.items():
        for name in names:
            key = name.strip().lower()
            picked_scopes.setdefault(key, set()).add(scope_id)

    # Filter + reduce
    kept: list[Candidate] = []
    for c in agent2_result.candidates:
        key = c.name.strip().lower()
        if key not in picked_scopes:
            continue
        picked_at = picked_scopes[key]
        # Intersect with original coverage to defend against pick-outside
        # attacks; if the intersection is empty, trust the user's intent
        # and keep their picked scopes (frontend should prevent this but
        # backend stays permissive).
        original = set(c.covers_step_ids)
        if original:
            new_covers = sorted((picked_at & original) or picked_at)
        else:
            new_covers = sorted(picked_at)
        new_conf = {
            sid: c.coverage_confidence.get(sid, "claimed") for sid in new_covers
        }
        c.covers_step_ids = new_covers
        c.coverage_confidence = new_conf
        kept.append(c)

    filtered = Agent2Result(
        candidates=kept,
        search_approach=(
            agent2_result.search_approach
            + f" [user-filtered to {len(kept)} candidates]"
        ),
        coverage_notes=agent2_result.coverage_notes,
        cost_usd=agent2_result.cost_usd,
    )

    if user_added:
        filtered = inject_user_candidates(filtered, user_added)

    return filtered
