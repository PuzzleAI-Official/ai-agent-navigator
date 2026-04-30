"""Research sub-agent for Agent 5 builder.

When the builder hits a gap (unknown endpoint shape, undocumented edge
case, vendor migration), it invokes the ``ask_research`` tool. That
tool dispatches to ``run_targeted_research`` here, which spins up a
fresh Sonnet sub-agent with web_search + web_fetch tools and a focused
research-question prompt. The sub-agent returns a string answer + cost.

Phase 3.2 of the architecture cleanup — extracted from
``puzzleeval/agents/implement_test_env.py``. The legacy names
``_run_targeted_research`` and ``_ask_research_template_adherence``
are preserved as one-line shims in the legacy module for back-compat
with source-grep tests + external callers.

Module ownership boundaries:
  * This module owns the SUB-AGENT call path (model selection, tool
    config, retry/continuation handling, cost accounting).
  * It does NOT own the dispatching from the build loop's
    ``ask_research`` tool — that's still in ``implement_test_env.py``
    until Phase 5 moves the build loop.
  * It does NOT own the ``ask_research_template_adherence`` LOGGER
    that sits next to the dispatch site — that's a build-loop
    concern.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import anthropic

from puzzleeval.agent_preamble import with_preamble
from puzzleeval.agents.agent5.sandbox import candidate_slug
from puzzleeval.config import (
    RESEARCH_MODEL,
    WEB_SEARCH_PRICE_PER_SEARCH,
    output_config_for_request,
)
from puzzleeval.telemetry.cost import calculate_call_cost
from puzzleeval.telemetry.logging import log_llm_call

if TYPE_CHECKING:
    import logging


# ---------------------------------------------------------------------------
# Template-adherence inspection (NEW-AK §Q3)
# ---------------------------------------------------------------------------
# When the builder invokes ask_research, the question SHOULD follow the
# direction-pointing template (CANDIDATE / ENDPOINT / KNOWN / FIELD NEEDED /
# WHY) so the sub-agent has full context. We don't HARD-validate (false
# rejects on benign rephrasings hurt UX); we LOG adherence so we can see
# drift in production.
_ASK_RESEARCH_TEMPLATE_FIELDS: tuple[str, ...] = (
    "CANDIDATE",
    "ENDPOINT",
    "KNOWN",
    "FIELD NEEDED",
    "WHY",
)


def ask_research_template_adherence(question: str) -> dict[str, object]:
    """Inspect an ask_research question for template adherence.

    Pure function. No side effects. Returns a structured report shaped
    for direct logging:

        {
            "fields_present": ["CANDIDATE", "WHY"],
            "fields_missing": ["ENDPOINT", "KNOWN", "FIELD NEEDED"],
            "adherence_ratio": 0.4,
            "fully_adherent": False,
        }
    """
    upper = (question or "").upper()
    present = [f for f in _ASK_RESEARCH_TEMPLATE_FIELDS if f in upper]
    missing = [f for f in _ASK_RESEARCH_TEMPLATE_FIELDS if f not in upper]
    return {
        "fields_present": present,
        "fields_missing": missing,
        "adherence_ratio": (
            len(present) / len(_ASK_RESEARCH_TEMPLATE_FIELDS)
        ),
        "fully_adherent": len(missing) == 0,
    }


# ---------------------------------------------------------------------------
# Research sub-agent system prompt
# ---------------------------------------------------------------------------
# Constant defined here (canonical home) and re-exported via shim from
# implement_test_env.py for back-compat with tests that grep for the name.
TARGETED_RESEARCH_SYSTEM = (
    "You are a peer integration engineer helping a builder agent debug a "
    "specific API. You've been handed FULL CONTEXT already: the provider "
    "name, docs URL, auth method, what the builder knows (api_spec.txt "
    "excerpt), what they've tried (recent error output, harness code), "
    "and the specific question they need answered.\n\n"
    "DO NOT re-derive what's already in the context. Don't restate the "
    "endpoint base URL or auth method — the builder already has those. "
    "Your job is to find what's MISSING, WRONG, or NON-OBVIOUS.\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "ROUTE BY REGIME — self-classify from the context you were given:\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "REGIME A — TARGETED (question names a specific thing):\n"
    "  Signals: question mentions a specific flag, endpoint, error code, "
    "config parameter, or HTTP status. Context has a rich api_spec with "
    "clear base URL + auth.\n"
    "  Strategy: ONE precise search — `{provider} {specific_feature}` — "
    "fetch the most authoritative result (official docs preferred), "
    "return the answer + source URL. Stop. Do NOT burn remaining budget.\n"
    "  Example: 'What enables override_permissions for ElevenLabs "
    "Conversational AI agent?' → one search → fetch their agent-config "
    "docs → cite the exact flag + docs URL → done.\n\n"
    "REGIME B — EXPLORATORY (question is open-ended OR context is thin):\n"
    "  Signals: question asks WHY something fails without a clear "
    "hypothesis, or api_spec excerpt is minimal, or the symptom could "
    "have many causes (WebSocket close codes, silent failures, "
    "intermittent issues).\n"
    "  Strategy: DIVERSIFY across search angles — do NOT repeat the "
    "same query. Spread budget across 3-4 DIFFERENT sources:\n"
    "    1. Official docs: `{provider} {feature}`\n"
    "    2. GitHub SDK issues: `site:github.com {provider} {symptom}`\n"
    "    3. Community (StackOverflow / Reddit / forum): "
    "`{provider} {error_pattern} site:stackoverflow.com`\n"
    "    4. Archived docs (for deprecated endpoints / migrations): "
    "`{provider} {feature} site:web.archive.org`\n"
    "  Fetch 1-2 most promising results across different sources. "
    "Synthesize findings. If the provider migrated (new domain / v2 API), "
    "report BOTH old and new endpoints explicitly.\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "HONEST 3-TIER OUTPUT FORMAT (both regimes):\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Pick exactly ONE of three tiers based on what your research found.\n"
    "The tier names are load-bearing — the builder routes on them.\n\n"
    "TIER 1 — `ANSWER`: you found a confident, source-backed answer.\n"
    "  `ANSWER: <direct actionable answer — specific flag, endpoint, "
    "fix, code snippet if relevant>`\n"
    "  `SOURCE: <URL — prefer official docs; cite multiple if cross-"
    "referenced>`\n"
    "  `CONFIDENCE: high|medium (based on source authority + specificity)`\n\n"
    "TIER 2 — `REASONABLE_GUESS`: you couldn't find an authoritative "
    "answer but the context (api_spec, recent error, similar APIs) "
    "supports a likely value. Builder treats this as 'code with this; "
    "live errors confirm or refute.' Honest middle ground between a "
    "fabricated ANSWER and a giving-up NOT_FOUND.\n"
    "  `REASONABLE_GUESS: <likely value with reasoning>`\n"
    "  `BASIS: <what context supports this — analogous API, error "
    "pattern, partial doc fragment>`\n"
    "  `CONFIDENCE: low (unverified — builder should validate "
    "empirically)`\n\n"
    "TIER 3 — `NOT_FOUND`: genuinely uncertain. No confident answer, "
    "no defensible guess. Builder will leave the field as TODO and "
    "let live errors guide.\n"
    "  `NOT_FOUND: searched: <queries tried>; checked: "
    "<sources checked>`\n"
    "  `RECOMMENDED NEXT STEP: <what the builder should try "
    "empirically, e.g., 'probe the WebSocket close code and check the "
    "frame payload'>`\n"
    "  `CONFIDENCE: none`\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "CRITICAL RULES:\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "1. NEVER fabricate a confident-sounding answer in TIER 1. If "
    "you're uncertain, use TIER 2 (REASONABLE_GUESS) — that's exactly "
    "what it's for. Hallucinating an endpoint URL or config flag with "
    "TIER 1 confidence is WORSE than admitting uncertainty: the "
    "builder will waste turns acting on a wrong answer believed to be "
    "authoritative.\n"
    "2. The TIER 2 path lets you contribute partial information when "
    "research finds context but no authoritative confirmation. Use it "
    "when you have a defensible reasoning chain; don't downgrade to "
    "TIER 3 if you actually have signal.\n"
    "3. If you find conflicting info, prefer the most recent source "
    "(2025-2026) and report the conflict in your TIER 1 answer.\n"
    "4. If the company rebranded or migrated, report BOTH old and new "
    "endpoints — builder may have credentials for only one.\n"
    "5. Keep output concise — builder has limited context. 300-800 "
    "words ideal. Front-load the tier-prefixed first line; supporting "
    "detail below."
)


# ---------------------------------------------------------------------------
# Phase 2C.4 — output-tier classifier (Codex C8 prep for G-Aux2 promotion)
# ---------------------------------------------------------------------------

# The three tiers the prompt teaches. Builders/callers use the classifier
# below to route based on which tier the sub-agent emitted. Currently
# advisory; future G-Aux2 promotion would attach Pydantic-schema validation.

RESEARCH_SUBAGENT_TIERS: tuple[str, ...] = ("ANSWER", "REASONABLE_GUESS", "NOT_FOUND")


def classify_research_output(text: str) -> str:
    """Classify a research-subagent response into one of the three tiers.

    Returns one of the strings in RESEARCH_SUBAGENT_TIERS or "UNKNOWN"
    if no tier prefix is detectable. The classifier is permissive about
    surrounding whitespace + colon-vs-no-colon to handle minor LLM
    formatting variation.

    Looks for the FIRST line that begins (after stripping) with one of
    the tier names. ANSWER is the most specific (not REASONABLE_GUESS
    nor NOT_FOUND), so we check in order: REASONABLE_GUESS first
    (longest), then NOT_FOUND, then ANSWER.
    """
    if not text:
        return "UNKNOWN"
    for line in text.splitlines():
        stripped = line.lstrip().lstrip("`").lstrip("*").lstrip()
        # Order matters: REASONABLE_GUESS must be checked before ANSWER
        # (which is a substring of nothing here, but defensive ordering).
        for tier in ("REASONABLE_GUESS", "NOT_FOUND", "ANSWER"):
            if stripped.startswith(tier + ":") or stripped.startswith(tier + " "):
                return tier
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# The sub-agent call
# ---------------------------------------------------------------------------

def _extract_text_from_response(response: anthropic.types.Message) -> str:
    """Pull all text content from a response with mixed content blocks.

    Local copy (the legacy implement_test_env._extract_text_from_response
    is still the canonical home for the build loop). Sub-agent response
    extraction is identical so we keep this private here to avoid an
    import cycle during Phase 3-5.
    """
    text_parts = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
    return "\n\n".join(text_parts)


def run_targeted_research(
    client: anthropic.Anthropic,
    question: str,
    candidate_name: str,
    logger: "logging.Logger",
    trace_id: str,
) -> tuple[str, float]:
    """Run a focused research sub-agent to answer a specific API question.

    Called mid-loop when the builder invokes ``ask_research``. Uses
    fresh context (no accumulated build noise) with web_search +
    web_fetch tools, capped at 2 each (post-NEW-AM v5 budget).

    Returns ``(answer_text, cost_usd)``. Total cost includes server-tool
    web_search line items charged at WEB_SEARCH_PRICE_PER_SEARCH.

    Tool budget: 2 web_search + 2 web_fetch per call. Real-run measurement
    (trace a4860e94) showed budget 3+3 led to 8-min sub-agent runs that
    were 60% of the entire build wall-clock; budget 2+2 caps the sub-
    agent at ~2-3 min and forces concise answers. The build agent can
    always call ask_research again with a refined question — cheaper
    than one massive 8-min call.
    """
    candidate_label = candidate_slug(candidate_name)

    logger.info(
        f"Targeted research for {candidate_name}: {question[:100]}",
        extra={
            "operation": f"targeted_research_{candidate_label}",
            "trace_id": trace_id,
        },
    )

    total_cost = 0.0
    messages = [{
        "role": "user",
        "content": (
            f"## Research Question\n\n{question}\n\n"
            "Search the web and report your findings with exact details."
        ),
    }]

    # Allow 1 continuation for pause_turn
    for continuation in range(2):
        call_start = time.time()
        try:
            _kwargs_research_sub: dict[str, object] = {}
            _ocfg = output_config_for_request()
            if _ocfg:
                _kwargs_research_sub["output_config"] = _ocfg
            response = client.beta.messages.create(
                model=RESEARCH_MODEL,
                max_tokens=4096,
                betas=["context-management-2025-06-27"],
                system=[{"type": "text", "text": with_preamble(TARGETED_RESEARCH_SYSTEM)}],
                messages=messages,
                tools=[
                    {"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
                    {"type": "web_fetch_20250910", "name": "web_fetch",
                     "max_uses": 2, "max_content_tokens": 10000},
                ],
                thinking={"type": "adaptive"},
                **_kwargs_research_sub,
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError,
                anthropic.APIStatusError, anthropic.BadRequestError) as e:
            return f"Research failed: {e}", total_cost

        log_llm_call(
            logger=logger, response=response, model=RESEARCH_MODEL,
            trace_id=trace_id, start_time=call_start,
            operation=f"targeted_research_{candidate_label}_cont{continuation}",
        )

        call_cost = calculate_call_cost(response, RESEARCH_MODEL)
        total_cost += call_cost

        server_tool_use = getattr(response.usage, "server_tool_use", None)
        if server_tool_use:
            searches = getattr(server_tool_use, "web_search_requests", 0) or 0
            total_cost += searches * WEB_SEARCH_PRICE_PER_SEARCH

        if response.stop_reason == "pause_turn":
            messages = [
                messages[0],
                {"role": "assistant", "content": response.content},
            ]
            continue

        text = _extract_text_from_response(response)
        if text:
            logger.info(
                f"Targeted research complete for {candidate_name}",
                extra={
                    "operation": f"targeted_research_complete_{candidate_label}",
                    "trace_id": trace_id,
                    "cost": total_cost,
                },
            )
            return text, total_cost

    return "Research exhausted continuations without producing an answer.", total_cost


__all__ = [
    "RESEARCH_SUBAGENT_TIERS",
    "TARGETED_RESEARCH_SYSTEM",
    "ask_research_template_adherence",
    "classify_research_output",
    "run_targeted_research",
]
