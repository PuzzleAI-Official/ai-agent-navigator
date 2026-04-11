# ============================================================================
# Agent 3: Synthetic Test Cases Agent
# ============================================================================
# PURPOSE:
#   Generate realistic test case specifications that will be used to evaluate
#   ALL AI candidates fairly. Test cases are generated INDEPENDENTLY from
#   candidate services — this prevents bias.
#
# DESIGN: Single-step pure function — one structured output call.
#
# WHY SINGLE STEP (vs Agent 2's two-step)?
#   Agent 2 needs two steps because server tools (web_search) produce mixed
#   content blocks that can't be combined with structured output. Agent 3
#   doesn't use any server tools — it's purely generative from Agent 1's
#   output. A single client.messages.parse() call with output_format is
#   sufficient and cheaper.
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  CORE LINES GUIDE                                               │
# │                                                                 │
# │  If you want to understand ONLY the main logic (skip logging,   │
# │  error handling, cost tracking), read these lines:              │
# │                                                                 │
# │  1. SYSTEM_PROMPT             — instructions for test generation│
# │  2. _build_generation_message() — assembles the request         │
# │  3. run_synthetic_tests_agent()  — THE MAIN FUNCTION            │
# │     Inside it, the core is just 4 lines:                        │
# │       a. client = anthropic.Anthropic(...)                      │
# │       b. message = _build_generation_message(input)             │
# │       c. response = client.messages.parse(                      │
# │            output_format=Agent3Result)           ★ SINGLE CALL  │
# │       d. return response.parsed_output                          │
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
)
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import Agent3Input, Agent3Result, UserUnderstandingOutput


# ============================================================================
# [CORE] System Prompt — Test Case Generation
# ============================================================================
# This prompt instructs Claude to generate test cases that:
#   1. Cover ALL sub-tasks from Agent 1's output
#   2. Spread across 6 testing dimensions (coverage matrix)
#   3. Include weighted judgement criteria for Agent 7
#   4. Scale count dynamically based on sub-task complexity
#
# The coverage matrix ensures the user never feels undertested. Each sub-task
# gets tested across happy path, input variations, edge cases, scale,
# domain-specific scenarios, and error resilience.
# ============================================================================

SYSTEM_PROMPT = """You are the Synthetic Test Cases Agent for PuzzleEval. Your job is to generate realistic, comprehensive test case specifications that will fairly evaluate AI services.

## How Many Test Cases to Generate

Scale with sub-task count:
- Base: 5-8 test cases per sub-task
- If workflow_summary is provided: add 2-3 extra cases per sub-task grounded in that real data
- Minimum: 10 total test cases
- Maximum: 50 total test cases

## Coverage Matrix (CRITICAL)

Each sub-task MUST be tested across these 6 dimensions. Generate at least 1 test case per dimension per sub-task:

1. **happy_path** — Standard, clean, ideal input. The baseline.
2. **input_variation** — Different formats, styles, or structures of valid input.
3. **edge_case** — Boundary conditions, unusual values, ambiguous input.
4. **scale** — Single item vs batch, small vs large input.
5. **domain_specific** — Industry-specific scenarios tied to the user's domain.
6. **error_resilience** — Bad, partial, or corrupted input. How gracefully does the AI fail?

Tag each test case with the dimensions it covers (a test can cover multiple).

## Difficulty Spread (per sub-task)

- ~30% easy (happy_path, straightforward)
- ~50% medium (realistic complexity, input variations)
- ~20% hard (edge cases, error resilience, tricky scenarios)

## Input Data Rules

- input_data MUST contain the ACTUAL test content (not a description of what to generate)
- For text-based tests: write the actual text (customer email, chat message, query, document text)
- Make data REALISTIC and domain-appropriate — use plausible names, numbers, dates
- Vary the data across test cases — don't reuse the same names/values
- test_file_path is always null in text mode — user-uploaded files are handled separately

## input_type Values

Choose the one that best describes the nature of the input:
- "text" — plain text (chat messages, queries, plain documents)
- "structured_data" — JSON, CSV, or tabular data
- "document_content" — text representation of a formatted document (invoice, contract, receipt)
- "conversation" — multi-turn conversation context
- "image_description" — description of visual content

## output_type Values

Choose what kind of output the AI service should produce:
- "free_text" — natural language response
- "structured_json" — JSON with specific fields
- "classification" — category label(s)
- "extraction" — extracted fields/data from input
- "action" — an action to perform

## Judgement Criteria Rules

Each test case must have 2-5 weighted criteria. Rules:
- Weights must sum to approximately 1.0
- Use the most appropriate eval_type for each criterion:
  - "exact_match" — for specific values (numbers, names, dates)
  - "semantic_similarity" — for meaning-equivalent text
  - "contains_key_info" — for responses that must include specific facts
  - "format_compliance" — for structural requirements (valid JSON, correct schema)
  - "subjective_quality" — for tone, helpfulness, completeness
- Be SPECIFIC. "Good response" is too vague. "Must mention the 30-day return policy" is testable.

## Output

Generate the complete test suite with:
- generation_notes explaining your coverage reasoning
- coverage_summary: a dict mapping EACH sub-task description (use the EXACT description string from the sub-tasks above) to the number of test cases generated for it. Example: {"Extract data from invoices": 7, "Create QuickBooks entries": 5}. This field MUST NOT be empty — every sub-task must appear as a key with its integer count.
"""


# ============================================================================
# Constants
# ============================================================================

# Caching is DISABLED for Agent 3 (single-shot, no conversation loop).
CACHING_ENABLED = False

# Max tokens for the generation call. Test cases with detailed criteria
# and realistic input data produce substantial output.
GENERATION_MAX_TOKENS = 16384


# ============================================================================
# [CORE] Build the generation request message
# ============================================================================
# Serializes Agent 1's output into a structured prompt that tells Claude
# exactly what sub-tasks to generate tests for, with domain context and
# any workflow data to ground the synthetic content.
# ============================================================================

def _build_generation_message(user_understanding: UserUnderstandingOutput) -> str:
    """
    Convert Agent 1's structured output into a test generation request.

    Includes:
    - Summary and domain for context
    - Each sub-task with capability and keywords
    - Constraints that affect test design
    - Workflow summary for grounding (if available)
    - Calculated target case count
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

    # ── Calculate target test case count ──
    num_subtasks = len(user_understanding.sub_tasks)
    base_per_subtask = 7  # middle of 5-8 range
    workflow_bonus = 2 if user_understanding.workflow_summary else 0
    target_total = num_subtasks * (base_per_subtask + workflow_bonus)
    target_total = max(10, min(50, target_total))  # clamp to [10, 50]

    # ── CORE: Build constraints section ──
    constraints = user_understanding.constraints
    budget = constraints.budget_range or "Not specified"
    tech_level = constraints.technical_level or "Not specified"
    integrations = (
        ", ".join(constraints.integration_requirements)
        if constraints.integration_requirements
        else "None specified"
    )

    # ── CORE: Assemble the full message ──
    message = f"""## What the User Needs
{user_understanding.summary}

## Domain
{user_understanding.domain}

## Sub-Tasks to Generate Test Cases For
{subtasks_text}

## Constraints (affect test design)
- Budget: {budget}
- Technical level: {tech_level}
- Integration requirements: {integrations}

## Workflow Context
{user_understanding.workflow_summary or "No workflow document provided."}

## Target
Generate approximately {target_total} test cases total ({base_per_subtask + workflow_bonus} per sub-task).
Ensure every sub-task is covered across all 6 testing dimensions.
Use IDs starting from tc-001."""

    return message


# ============================================================================
# [CORE] Main function — this is the entry point
# ============================================================================
#
# THE CORE LOGIC IS 4 LINES (marked with ★ below). Everything else is
# logging, error handling, and cost tracking — necessary for production
# but not for understanding what Agent 3 does.
#
# SINGLE API CALL:
#   client.messages.parse() with output_format=Agent3Result
#   → guaranteed structured JSON matching our Pydantic schema
#
# ============================================================================

def run_synthetic_tests_agent(input_data: Agent3Input) -> Agent3Result:
    """
    Run Agent 3. Takes Agent 1's structured understanding, generates
    comprehensive test case specifications with ground truth and
    judgement criteria.

    Single-step process:
      client.messages.parse() with output_format=Agent3Result
    """
    # ★ CORE LINE 1: Create the API client
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # [logging] Set up logger for this agent
    logger = get_logger("agent_3_synthetic_tests")
    logger.info("Agent 3 started", extra={
        "operation": "agent_start", "trace_id": input_data.trace_id,
    })

    # ★ CORE LINE 2: Build the generation request message
    generation_message = _build_generation_message(input_data.user_understanding)

    # ======================================================================
    # Generate test cases via structured output
    # ======================================================================

    # ★ CORE LINE 3: Call Claude with structured output
    start_time = time.time()
    try:
        response = client.messages.parse(
            model=DEFAULT_MODEL,
            max_tokens=GENERATION_MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": generation_message}],
            output_format=Agent3Result,
        )

    # [error handling] Same pattern as Agent 1 and Agent 2
    except anthropic.RateLimitError as e:
        logger.error("Rate limit hit", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "RateLimitError",
        })
        raise AgentRateLimitError(
            message=f"Rate limit exceeded during test generation: {e}",
            agent_name="synthetic_tests", trace_id=input_data.trace_id,
        )
    except anthropic.APIConnectionError as e:
        logger.error("API connection failed", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIConnectionError",
        })
        raise AgentAPIError(
            message=f"Failed to connect to Anthropic API during test generation: {e}",
            agent_name="synthetic_tests", trace_id=input_data.trace_id,
        )
    except anthropic.APIStatusError as e:
        logger.error("API error", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIStatusError",
        })
        raise AgentAPIError(
            message=f"Anthropic API error during test generation: {e}",
            agent_name="synthetic_tests", trace_id=input_data.trace_id,
        )

    # [logging] Log call metrics
    call_cost = log_llm_call(
        logger=logger, response=response, model=DEFAULT_MODEL,
        trace_id=input_data.trace_id, start_time=start_time,
        operation="synthetic_tests_generate",
    )

    # ★ CORE LINE 4: Return the parsed result
    result = response.parsed_output

    # [error handling] Defensive check for truncated/refused responses
    if result is None:
        logger.error("Parsed output is None", extra={
            "operation": "output_validation", "trace_id": input_data.trace_id,
            "error": "parsed_output is None",
            "stop_reason": response.stop_reason,
        })
        raise AgentOutputError(
            message=(
                f"Test generation returned no parsed output. "
                f"stop_reason={response.stop_reason}"
            ),
            agent_name="synthetic_tests", trace_id=input_data.trace_id,
        )

    # [cost tracking] Set cost on the result
    result.cost_usd = call_cost

    logger.info("Agent 3 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "test_case_count": len(result.test_cases),
        "subtasks_covered": len(result.coverage_summary),
    })

    return result
