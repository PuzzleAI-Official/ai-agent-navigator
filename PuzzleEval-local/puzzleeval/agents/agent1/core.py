# ============================================================================
# Agent 1: User Understanding Agent
# ============================================================================
# PURPOSE:
#   Parse the user's natural language request into structured data that
#   ALL downstream agents can consume.
#
# DESIGN: Pure function — input data in, structured result out.
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  CORE LINES GUIDE                                               │
# │                                                                 │
# │  If you want to understand ONLY the main logic (skip logging,   │
# │  error handling, caching), read these lines:                    │
# │                                                                 │
# │  1. SYSTEM_PROMPT (line ~30)     — instructions to Claude       │
# │  2. _build_system_blocks()       — assembles system message     │
# │  3. _build_messages()            — assembles conversation       │
# │  4. run_user_understanding_agent — THE MAIN FUNCTION            │
# │     Inside it, the core is just 5 lines:                        │
# │       a. client = anthropic.Anthropic(...)                      │
# │       b. system_blocks = _build_system_blocks(file_content)     │
# │       c. messages = _build_messages(user_text, history)         │
# │       d. response = client.messages.parse(...)                  │
# │       e. return response.parsed_output                          │
# │                                                                 │
# │  Everything else is logging, error handling, or file parsing    │
# │  — necessary for production but not for understanding the flow. │
# └─────────────────────────────────────────────────────────────────┘
# ============================================================================

import time
from typing import Any

import anthropic

from puzzleeval.config import (
    AGENT1_MODEL,
    ANTHROPIC_API_KEY,
    MAX_TOKENS,
    output_config_for_request,
)
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.file_parsers import parse_file
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import Agent1Input, Agent1Result

from functools import lru_cache
from importlib import resources


# ============================================================================
# [CORE] System Prompt — the instructions Claude follows
# ============================================================================

@lru_cache(maxsize=1)
def _load_system_prompt() -> str:
    """Load SYSTEM_PROMPT from templates/system_prompt.md (Phase 7)."""
    return resources.files("puzzleeval.agents.agent1").joinpath(
        "templates", "system_prompt.md",
    ).read_text(encoding="utf-8")


SYSTEM_PROMPT = _load_system_prompt()


# Caching is DISABLED for Agent 1 (human-in-the-loop, slow turns).
# Enable for agents with rapid-fire calls (Agent 5/6/7).
# See AGENT1_SKILL.md and README_AGENT1.md for full rationale.
CACHING_ENABLED = False


# ============================================================================
# [CORE] Build the system message sent to Claude
# ============================================================================

def _build_system_blocks(
    file_content: str | dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Assemble the system message: prompt text + optional file content.
    Text files (DOCX/CSV/TXT) are appended to the prompt.
    Binary files (PDF/image) are added as separate content blocks.
    """
    # ── CORE: Build prompt text, append text-based file if present ──
    # Cross-cutting rules apply to every agent — see agent_preamble.py
    from puzzleeval.agent_preamble import with_preamble
    system_text = with_preamble(SYSTEM_PROMPT)
    if isinstance(file_content, str):
        system_text += (
            "\n\n## Uploaded Workflow Document\n"
            "The user has provided the following workflow document. "
            "Use this to better understand their current process and extract "
            "more accurate use cases.\n\n"
            f"{file_content}"
        )

    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system_text}
    ]

    # ── CORE: Add PDF/image as a native content block ──
    if isinstance(file_content, dict):
        file_block = {**file_content}
        if CACHING_ENABLED:
            file_block["cache_control"] = {"type": "ephemeral"}
        blocks.append(file_block)

    return blocks


# ============================================================================
# [CORE] Build the conversation messages sent to Claude
# ============================================================================

def _build_messages(
    user_text: str,
    conversation_history: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    Assemble the messages array: previous turns + new user message.
    Previous turns are included as-is for context.
    """
    messages: list[dict[str, Any]] = []

    # ── CORE: Include previous turns so Claude sees full conversation ──
    if conversation_history:
        messages.extend(conversation_history)

    # ── CORE: Add the new user message ──
    messages.append({"role": "user", "content": user_text})

    return messages


# ============================================================================
# [CORE] Main function — this is the entry point
# ============================================================================
#
# THE CORE LOGIC IS 5 LINES (marked with ★ below). Everything else is
# logging, error handling, and file parsing — necessary for production
# but not for understanding what Agent 1 does.
#
# ============================================================================


def run_user_understanding_agent(input_data: Agent1Input) -> Agent1Result:
    """
    Run Agent 1. Takes user's request, returns structured understanding
    or clarifying questions.
    """
    # ★ CORE LINE 1: Create the API client
    # Central factory: 120 s timeout + max_retries=3 (5xx + connection drops).
    # Without this every agent shipped its own bare-default client and a
    # single flaky TCP socket would hang the run for 10 minutes.
    from puzzleeval.anthropic_client import build_client
    client = build_client(api_key=ANTHROPIC_API_KEY)

    # [logging] Set up logger for this agent
    logger = get_logger("agent_1_user_understanding")
    logger.info("Agent 1 started", extra={
        "operation": "agent_start", "trace_id": input_data.trace_id,
    })

    # [file handling] Parse uploaded workflow file if provided
    file_content: str | dict[str, Any] | None = None
    if input_data.workflow_file_path:
        file_parse_start = time.time()
        try:
            file_content = parse_file(input_data.workflow_file_path)
            logger.info("Workflow file parsed", extra={
                "operation": "file_parse",
                "trace_id": input_data.trace_id,
                "latency_ms": round((time.time() - file_parse_start) * 1000, 2),
            })
        except Exception as e:
            logger.warning(f"File parsing failed, proceeding without file: {e}", extra={
                "operation": "file_parse", "trace_id": input_data.trace_id,
                "error": str(e), "error_type": type(e).__name__,
            })
            file_content = None

    # ★ CORE LINE 2: Build the system message (prompt + file)
    system_blocks = _build_system_blocks(file_content)

    # [backwards compat] Combine additional_context if no conversation_history
    current_user_text = input_data.user_text
    if input_data.additional_context and not input_data.conversation_history:
        current_user_text += f"\n\nAdditional context:\n{input_data.additional_context}"

    # Surface proceed_with_partial_info as an explicit operator instruction.
    # When True, the caller (CLI --no-interactive, FastAPI auto-run path)
    # has signaled "no human-in-the-loop will answer follow-up questions."
    # Agent 1 must respect critical-vs-optional: if the user's message has
    # enough for has_concrete_subtasks + has_domain, produce a complete
    # result with sensible defaults for optional fields (budget=None,
    # technical_level="some-technical", integrations=[]). If critical
    # info is genuinely missing, it should still return is_clear=False
    # and explain what's missing in clarification_needed.message — the
    # caller will then surface that as a user error, not hang.
    if getattr(input_data, "proceed_with_partial_info", False):
        current_user_text += (
            "\n\n---\n"
            "OPERATOR DIRECTIVE: proceed_with_partial_info=True. No human "
            "is available to answer follow-up questions on this turn. "
            "Apply this rule:\n"
            "- If the user's message + any uploaded files give you enough "
            "  for BOTH critical info fields (has_concrete_subtasks=True "
            "  AND has_domain=True), set is_clear=True, populate a complete "
            "  UserUnderstandingOutput using reasonable defaults for any "
            "  OPTIONAL fields the user didn't specify (budget=null, "
            "  technical_level='some-technical' if unclear, "
            "  integrations=[] if none mentioned, monthly_volume=null "
            "  if no hint). Build the full workflow + test_plan. Do NOT "
            "  block on optional info.\n"
            "- If a critical field is genuinely missing (e.g. the user "
            "  wrote one vague sentence), still return is_clear=False "
            "  with clarification_needed.message explaining what minimum "
            "  info you need. The caller will surface that as an error, "
            "  not hang."
        )

    # ★ CORE LINE 3: Build the conversation messages
    messages = _build_messages(
        user_text=current_user_text,
        conversation_history=input_data.conversation_history,
    )

    # ★ CORE LINE 4: Call Claude with structured output
    # Adaptive thinking + effort tier are wired in for Agent 1's director role
    # — decomposing user demands into a WorkflowBlueprint is a planning task
    # that benefits from extended reasoning. `output_config.effort` defaults to
    # `high` (or whatever PUZZLEEVAL_EFFORT is set to). Set
    # `PUZZLEEVAL_EFFORT=xhigh` for the deepest planning on Opus 4.7.
    #
    # The strict-grammar path (messages.parse + output_format) compiles the
    # Pydantic schema into a token-level constraint grammar — fast and
    # guaranteed-valid, but Anthropic enforces a max grammar size. Agent1Result
    # has 9 nested types and 60+ fields; once Phase 9's TestPlan is included
    # the compiled grammar exceeds the API limit. _call_with_fallback() runs
    # the strict path first and, on the specific 400 "compiled grammar too
    # large" error, falls back to messages.create() with a NON-strict tool
    # whose input is the same JSON Schema. The model emits JSON freely; we
    # validate the JSON through the Pydantic model post-hoc. Same Pydantic
    # output object reaches the rest of the pipeline either way.
    start_time = time.time()
    _ocfg = output_config_for_request()
    _extra: dict[str, Any] = {"thinking": {"type": "adaptive"}}
    if _ocfg is not None:
        _extra["output_config"] = _ocfg
    if CACHING_ENABLED:
        _extra["cache_control"] = {"type": "ephemeral"}
    try:
        from puzzleeval.structured_output import parse_with_fallback
        response = parse_with_fallback(
            client=client,
            model=AGENT1_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            messages=messages,
            output_format=Agent1Result,
            extra=_extra,
            trace_id=input_data.trace_id,
        )

    # [error handling] Different error types for different retry strategies
    except anthropic.RateLimitError as e:
        logger.error("Rate limit hit", extra={
            "operation": "llm_call", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "RateLimitError",
        })
        raise AgentRateLimitError(
            message=f"Rate limit exceeded: {e}",
            agent_name="user_understanding", trace_id=input_data.trace_id,
        )
    except anthropic.APIConnectionError as e:
        logger.error("API connection failed", extra={
            "operation": "llm_call", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIConnectionError",
        })
        raise AgentAPIError(
            message=f"Failed to connect to Anthropic API: {e}",
            agent_name="user_understanding", trace_id=input_data.trace_id,
        )
    except anthropic.APIStatusError as e:
        logger.error("API error", extra={
            "operation": "llm_call", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIStatusError",
        })
        raise AgentAPIError(
            message=f"Anthropic API error: {e}",
            agent_name="user_understanding", trace_id=input_data.trace_id,
        )

    # [logging] Capture tokens, cost, latency
    call_cost = log_llm_call(
        logger=logger, response=response, model=AGENT1_MODEL,
        trace_id=input_data.trace_id, start_time=start_time,
        operation="user_understanding",
    )

    # ★ CORE LINE 5: Return the parsed result
    result = response.parsed_output

    # [error handling] Defensive check for truncated/refused responses
    if result is None:
        logger.error("Parsed output is None", extra={
            "operation": "output_validation", "trace_id": input_data.trace_id,
            "error": "parsed_output is None", "stop_reason": response.stop_reason,
        })
        raise AgentOutputError(
            message=f"Claude returned no parsed output. stop_reason={response.stop_reason}",
            agent_name="user_understanding", trace_id=input_data.trace_id,
        )

    # [cost tracking] Set the cost for this single API call on the result.
    # Agent 1 is called once per conversation turn; the CLI accumulates cost
    # across turns by summing result.cost_usd from each turn.
    result.cost_usd = call_cost

    logger.info("Agent 1 completed", extra={
        "operation": "agent_complete", "trace_id": input_data.trace_id,
    })

    return result
