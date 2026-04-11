# ============================================================================
# Agent 3F: File-Based Test Cases Agent
# ============================================================================
# PURPOSE:
#   Generate test cases using USER-UPLOADED files as test inputs.
#   Reads each file (via Claude vision for images/PDFs, text extraction
#   for DOCX/CSV/TXT), generates ground truth (expected_output) and
#   weighted judgement criteria based on what's actually in the files.
#
# WHEN TO USE:
#   When Agent 1 sets requires_test_files=true AND the user provides files.
#   For text-only evaluations, use Agent 3 (synthetic_tests.py) instead.
#
# DESIGN: Single-step pure function — one structured output call.
#   Files are read using file_parsers.py (same utilities as Agent 1)
#   and sent to Claude as content blocks. Claude reads the files and
#   generates test cases with ground truth.
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  CORE LINES GUIDE                                               │
# │                                                                 │
# │  1. SYSTEM_PROMPT             — instructions for file analysis  │
# │  2. _build_file_message()     — assembles files + context       │
# │  3. run_file_tests_agent()    — THE MAIN FUNCTION               │
# │     Inside it, the core is just 4 lines:                        │
# │       a. client = anthropic.Anthropic(...)                      │
# │       b. content_blocks = _build_file_message(input)            │
# │       c. response = client.messages.parse(                      │
# │            output_format=Agent3Result)           ★ SINGLE CALL  │
# │       d. return response.parsed_output                          │
# └─────────────────────────────────────────────────────────────────┘
# ============================================================================

import os
import time

import anthropic

from puzzleeval.config import (
    ANTHROPIC_API_KEY,
    DEFAULT_MODEL,
)
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentFileParseError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.file_parsers import parse_file
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import Agent3Input, Agent3Result, UserUnderstandingOutput


# ============================================================================
# [CORE] System Prompt — File-Based Test Case Generation
# ============================================================================
# Different from Agent 3's text prompt: here Claude is READING real files
# and generating ground truth based on what it actually sees. It's acting
# as the "oracle" — the most capable model establishing what the correct
# output should be.
# ============================================================================

SYSTEM_PROMPT = """You are the File-Based Test Cases Agent for PuzzleEval. Your job is to read the user's uploaded files, create ground truth, and define evaluation criteria.

## Your Task

The user has uploaded real files. For EACH file, create exactly ONE test case:
1. READ the file carefully — you are the "ground truth oracle"
2. GENERATE expected_output (ground truth) based on what you actually see in the file
3. CREATE weighted judgement criteria for evaluating AI service output against your ground truth
4. Set test_file_path to the file path provided — this is the file that will be sent to API providers
5. Set input_data to a TEXT DESCRIPTION of the file contents (for downstream evaluation context)

Do NOT generate synthetic text test cases. Only create test cases from the actual uploaded files.
The number of test cases must equal the number of uploaded files — one test case per file.

## How Many Test Cases

Exactly one per uploaded file. 3 files = 3 test cases. No more, no less.

## Coverage Matrix

Tag each test case with applicable dimensions:
- "happy_path" — standard, clean input
- "input_variation" — unusual format or style
- "edge_case" — boundary conditions
- "scale" — large files or many items
- "domain_specific" — industry-specific content
- "error_resilience" — poor quality, damaged, or incomplete

## Difficulty Assessment

- "easy" — clean, standard format, clear text
- "medium" — some complexity (mixed formats, industry jargon)
- "hard" — challenging (handwritten, damaged, complex layout)

## input_type Values

- "document_content" — invoices, contracts, receipts, documents
- "image_description" — photos, diagrams, screenshots
- "structured_data" — spreadsheets, CSV data
- "text" — plain text documents

## output_type Values

- "extraction" — extract specific fields/data
- "structured_json" — produce structured JSON output
- "classification" — categorize the content
- "free_text" — generate natural language analysis

## Judgement Criteria Rules

- 2-5 criteria per test case, weights sum to ~1.0
- eval_type options: "exact_match", "semantic_similarity", "contains_key_info", "format_compliance", "subjective_quality"
- Be SPECIFIC. "Must extract vendor name as 'Acme Corp'" not "must be correct"
- For numerical values, use exact_match with the precise number
- For text fields that may vary slightly, use semantic_similarity

## Coverage Summary

In coverage_summary, count ALL test cases (file-based + synthetic) per sub-task. This field MUST NOT be empty — map EACH sub-task description (use the EXACT description string) to its integer test case count. Example: {"Extract data from invoices": 7, "Create entries in QuickBooks": 5}.
"""


# ============================================================================
# Constants
# ============================================================================

CACHING_ENABLED = False
GENERATION_MAX_TOKENS = 16384


# ============================================================================
# [CORE] Build the file message with content blocks
# ============================================================================

def _build_file_message(
    user_understanding: UserUnderstandingOutput,
    file_paths: list[str],
) -> list[dict]:
    """
    Build the message content blocks with files and context.

    Files are parsed using file_parsers.py:
    - PDFs/images → sent as native content blocks (Claude vision reads them)
    - DOCX/CSV/TXT → text extracted and included as text blocks

    Returns a list of content blocks for the API message.
    """
    logger = get_logger("agent_3f_file_tests")

    # Start with the text context
    subtask_lines = []
    for i, st in enumerate(user_understanding.sub_tasks, 1):
        subtask_lines.append(f"{i}. {st.description}")
    subtasks_text = "\n".join(subtask_lines)

    context_text = f"""## What the User Needs
{user_understanding.summary}

## Domain
{user_understanding.domain}

## Sub-Tasks to Generate Test Cases For
{subtasks_text}

## Uploaded Test Files
The user has uploaded {len(file_paths)} file(s) to use as test inputs.
Analyze each file and generate test cases with ground truth.

"""

    content_blocks: list[dict] = [{"type": "text", "text": context_text}]

    # Add each file as a content block
    for i, file_path in enumerate(file_paths):
        filename = os.path.basename(file_path)

        # Add a label for each file
        content_blocks.append({
            "type": "text",
            "text": f"\n--- File {i + 1}: {filename} (path: {file_path}) ---\n",
        })

        try:
            parsed = parse_file(file_path)

            if isinstance(parsed, dict):
                # Binary file (PDF/image) → native content block
                content_blocks.append(parsed)
            else:
                # Text file (DOCX/CSV/TXT) → text content block
                content_blocks.append({
                    "type": "text",
                    "text": f"File content:\n{parsed}",
                })

        except AgentFileParseError as e:
            # Log but don't fail — skip this file and note it
            logger.warning(f"Failed to parse file {filename}: {e}", extra={
                "operation": "file_parse_skip",
                "file_path": file_path,
                "error": str(e),
            })
            content_blocks.append({
                "type": "text",
                "text": f"(Could not read file: {e})",
            })

    # Final instruction
    content_blocks.append({
        "type": "text",
        "text": (
            "\n\nGenerate test cases for ALL files above. Each test case should "
            "reference the file via test_file_path. Generate expected_output "
            "(ground truth) based on what you actually see in each file."
        ),
    })

    return content_blocks


# ============================================================================
# [CORE] Main function — this is the entry point
# ============================================================================

def run_file_tests_agent(input_data: Agent3Input) -> Agent3Result:
    """
    Run Agent 3F. Reads user-uploaded files, generates test cases with
    ground truth and judgement criteria based on file content.

    Requires input_data.test_file_paths to be set and non-empty.
    """
    if not input_data.test_file_paths:
        raise AgentOutputError(
            message="Agent 3F requires test_file_paths but none were provided",
            agent_name="file_tests", trace_id=input_data.trace_id,
        )

    # ★ CORE LINE 1: Create the API client
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # [logging]
    logger = get_logger("agent_3f_file_tests")
    logger.info("Agent 3F started", extra={
        "operation": "agent_start", "trace_id": input_data.trace_id,
        "file_count": len(input_data.test_file_paths),
    })

    # ★ CORE LINE 2: Build file message with content blocks
    content_blocks = _build_file_message(
        input_data.user_understanding,
        input_data.test_file_paths,
    )

    # ★ CORE LINE 3: Call Claude with structured output
    start_time = time.time()
    try:
        response = client.messages.parse(
            model=DEFAULT_MODEL,
            max_tokens=GENERATION_MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": content_blocks}],
            output_format=Agent3Result,
        )

    except anthropic.RateLimitError as e:
        logger.error("Rate limit hit", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "RateLimitError",
        })
        raise AgentRateLimitError(
            message=f"Rate limit exceeded during file test generation: {e}",
            agent_name="file_tests", trace_id=input_data.trace_id,
        )
    except anthropic.APIConnectionError as e:
        logger.error("API connection failed", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIConnectionError",
        })
        raise AgentAPIError(
            message=f"Failed to connect to Anthropic API during file test generation: {e}",
            agent_name="file_tests", trace_id=input_data.trace_id,
        )
    except anthropic.APIStatusError as e:
        logger.error("API error", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIStatusError",
        })
        raise AgentAPIError(
            message=f"Anthropic API error during file test generation: {e}",
            agent_name="file_tests", trace_id=input_data.trace_id,
        )

    # [logging]
    call_cost = log_llm_call(
        logger=logger, response=response, model=DEFAULT_MODEL,
        trace_id=input_data.trace_id, start_time=start_time,
        operation="file_tests_generate",
    )

    # ★ CORE LINE 4: Return the parsed result
    result = response.parsed_output

    if result is None:
        logger.error("Parsed output is None", extra={
            "operation": "output_validation", "trace_id": input_data.trace_id,
            "error": "parsed_output is None",
            "stop_reason": response.stop_reason,
        })
        raise AgentOutputError(
            message=(
                f"File test generation returned no parsed output. "
                f"stop_reason={response.stop_reason}"
            ),
            agent_name="file_tests", trace_id=input_data.trace_id,
        )

    # [cost tracking] Set cost on the result
    result.cost_usd = call_cost

    logger.info("Agent 3F completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "test_case_count": len(result.test_cases),
        "file_count": len(input_data.test_file_paths),
    })

    return result
