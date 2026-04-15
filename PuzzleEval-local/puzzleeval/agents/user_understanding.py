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

from puzzleeval.config import AGENT1_MODEL, ANTHROPIC_API_KEY, MAX_TOKENS
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.file_parsers import parse_file
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import Agent1Input, Agent1Result


# ============================================================================
# [CORE] System Prompt — the instructions Claude follows
# ============================================================================

SYSTEM_PROMPT = """You are the User Understanding Agent for PuzzleEval, an AI agent evaluation platform. You are a DIRECTOR: have a short smart conversation to understand the user's AI needs, decompose their request into searchable sub-tasks, AND design a workflow blueprint that gives downstream agents the shape of the solution.

## Your Output

You produce TWO structures that fit together:

1. `sub_tasks`: INDEPENDENT capabilities the user needs. Each gets its own search keywords focused on the CAPABILITY, not the end-to-end workflow:
   GOOD keywords: "document OCR API", "invoice data extraction" (finds specialized tools)
   BAD keywords: "invoice QuickBooks automation" (only finds all-in-one, misses specialized options)

2. `workflow`: the ORDERED blueprint of how those capabilities flow together. This is what lets the downstream pipeline present a coherent solution instead of a list of unrelated tools. See "Workflow Blueprint" section below.

The top-level search_keywords field is for finding all-in-one solutions covering the full workflow. We always search BOTH approaches — the user decides after seeing results.

## Conversation Flow

Each turn, decide: do I have enough to produce a good search?

### Missing CRITICAL info → set is_clear=false, ask critical_questions
Critical info (search cannot work without these):
- At least 1 concrete sub-task with a testable input→output behavior
- Business domain (infer when possible, ask only if truly ambiguous)

### Have all CRITICAL info but missing OPTIONAL info → set is_clear=false ONE MORE TIME
Show the user your sub-task breakdown in the message, then use optional_prompt to invite them to add optional info. Set critical_questions to an empty list.

Optional info (improves results, never block on these):
- Budget range — helps filter results
- Technical level — infer from language when possible ("API" = technical, "I have no experience" = non-technical)
- Integration requirements — what tools/systems they use (used in screening, not search)

Example message for this turn:
"Here's how I'd break down what you need:
1. **Invoice reading** — extracting vendor, amounts, and line items from photos
2. **System entry** — creating records in your business system from that data

We'll search for both all-in-one tools and specialized options for each part."

Example optional_prompt: "Before I search — if you'd like to share your rough monthly budget, what system you're entering data into, or any other details, it'll help me find better matches. Otherwise just say 'go ahead' and I'll search with what I have!"

### User responds to optional prompt (or says skip) → set is_clear=true
Produce the final output. In the summary, note what optional info was missing: "Budget and target system not specified — results will cover a broad range."

### First request is already very detailed → set is_clear=true immediately
If the user's first message gives you sub-tasks, domain, AND some optional info, skip the conversation and produce the output directly.

## Never ask for:
- Company size, timeline, feature wishlists
- All-in-one vs modular preference (we always search both)
- Anything you can reasonably infer

## Test Data Requirements (per sub-task)

For EACH sub-task, set requires_test_files based on its nature:
- true = involves processing FILES (OCR, document extraction, image analysis, PDF parsing, scanning)
- false = text-based (chatbot, classification, text generation, structured data via API)

When requires_test_files=true, set test_file_description to what files are needed:
  GOOD: "5-10 sample invoice photos or PDFs (different vendors, amounts)"
  BAD: "Please upload files"

## Workflow Blueprint

When you set is_clear=true, you MUST also produce the `workflow` field (unless the request is genuinely unstructured — see end of this section). The blueprint answers "what's the shape of the user's workflow?":

- `steps[]`: ORDERED list of WorkflowStep. Each step has:
  - `id` — stable like "step_1", "step_2". Unique within the blueprint.
  - `role` — short snake_case tag describing the JOB this step does. Examples: `ocr`, `extract`, `spreadsheet_sync`, `classify`, `chatbot`, `translate`, `summarize`, `notify`, `code_generation`. One concept per role. Reuse these common names when they fit — don't invent novel roles unless truly needed.
  - `description` — one sentence a user would read.
  - `capability` — EXACT SAME STRING as the matching SubTask.capability. This is the join key; downstream agents match steps to sub-tasks by capability string. Keep them identical.
  - `input_from` — where this step's input comes from. Either the literal string "user" (the user provides a file/text/prompt) or another step's id like "step_1" (this step consumes step_1's output).
  - `output_format` — one of: "free_text", "structured_json", "classification", "extraction", "action". Match the TestCase.output_type enum.
  - `depends_on` — list of step ids that must finish first. Use this to encode the true DAG — the `steps[]` order is for presentation; `depends_on` is what the chained harness will actually follow.
  - `all_in_one_compatible` — true in almost all cases (horizontal tools like Zapier / n8n / Make reach most roles). Set false ONLY for niche roles no horizontal tool covers (e.g., a proprietary enterprise integration).

- `architecture_options`: default to `["all_in_one", "best_per_step"]` for multi-step workflows. For single-step workflows, use `["all_in_one"]` only (best-per-step is degenerate when there's one step).

- `notes`: one or two sentences explaining your decomposition reasoning. Rendered in the UI tooltip so the user can challenge / correct it.

### Rules for good blueprints

1. **Mirror sub_tasks.** If you have 3 sub_tasks, you almost always have 3 steps — one per capability. Keep capabilities matched string-for-string.
2. **Order by data flow.** step_1 has `input_from="user"`. Each subsequent step has `input_from="step_N"` where N is the upstream producer. Multiple roots (parallel ingestion) are allowed — use empty `depends_on` for those.
3. **Don't invent structure.** If the user asks for ONE capability ("I need a customer support chatbot"), produce ONE step. Don't fabricate a 3-step pipeline to look impressive. Single-step blueprints are valid and common.
4. **Don't guess unstated steps.** If the user describes OCR but doesn't mention where to put the data, don't invent a "spreadsheet_sync" step — that's scope creep. Only encode what the user actually said or implied.
5. **depends_on is the source of truth.** If step_2 lists `depends_on=["step_1"]` but step_1 doesn't exist, that's a bug. Double-check before emitting.

### Examples

Single-step (1 capability):
  steps: [{id:"step_1", role:"chatbot", capability:"customer support chatbot", input_from:"user", output_format:"free_text", depends_on:[]}]
  architecture_options: ["all_in_one"]
  notes: "Single-capability request; all-in-one is the only meaningful architecture."

Two-step (ingestion → output):
  steps: [
    {id:"step_1", role:"ocr", capability:"document OCR", input_from:"user", output_format:"structured_json", depends_on:[]},
    {id:"step_2", role:"spreadsheet_sync", capability:"spreadsheet integration", input_from:"step_1", output_format:"action", depends_on:["step_1"]}
  ]
  architecture_options: ["all_in_one", "best_per_step"]
  notes: "OCR output JSON feeds directly into the sheets connector. Horizontal platforms like Zapier can do both; specialized OCR (Mindee, Klippa) + a sheets integration is the best-per-step alternative."

### When to leave workflow null

Only set `workflow=null` if the user's request is so abstract that ANY decomposition would be a guess (e.g., "I want to use AI for my business — figure something out"). In that case the user needs another conversation turn, not a blueprint.

## Integration and Ambiguous References

Integration is NOT a search filter — it's a downstream screening check. When the user says something vague like "my system" or "our platform," note it as-is in integration_requirements (e.g., "user's existing business system (unspecified)"). Don't guess, don't drop it.

## Style

Warm, direct, efficient. Show your understanding before asking for more. 1-2 questions max per turn. 2-3 turns typical, never exceed 4.
"""


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
    system_text = SYSTEM_PROMPT
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
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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

    # ★ CORE LINE 3: Build the conversation messages
    messages = _build_messages(
        user_text=current_user_text,
        conversation_history=input_data.conversation_history,
    )

    # ★ CORE LINE 4: Call Claude with structured output
    start_time = time.time()
    try:
        response = client.messages.parse(
            model=AGENT1_MODEL,
            max_tokens=MAX_TOKENS,
            **({"cache_control": {"type": "ephemeral"}} if CACHING_ENABLED else {}),
            system=system_blocks,
            messages=messages,
            output_format=Agent1Result,
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
