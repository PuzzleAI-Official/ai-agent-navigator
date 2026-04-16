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

## Architecture Alignment (CRITICAL for multi-scope workflows)

When sub-tasks include `[Architecture]` annotations:
- `output_format` tells you EXACTLY what output_type to use for that sub-task's test cases. Use it directly:
  - output_format=structured_json → output_type="structured_json"
  - output_format=free_text → output_type="free_text"
  - output_format=classification → output_type="classification"
  - output_format=extraction → output_type="extraction"
  - output_format=action → output_type="action"
- `input_source` tells you what feeds this step:
  - "user provides input directly" → input_type matches the sub-task's nature (text, document_content, conversation, etc.)
  - "receives output from step_N" → input_type should be "structured_data" or "text" depending on step_N's output_format. The test case input_data should SIMULATE what the upstream step would produce (e.g., if step_1 is OCR with output_format=structured_json, then step_2's input_data should be a realistic JSON object with extracted invoice fields, NOT a raw invoice image)
- `requires_test_files=True` means the sub-task ideally tests with real files. When generating synthetic tests for a file-based sub-task, use input_type="document_content" and write realistic text representations of what the file would contain.
- `step_id` is the scope identifier. Set sub_task_ref to the sub-task's EXACT description string (as before), but be aware this test case will be routed to candidates covering that step_id during Phase 9 testing.

DO NOT override these architectural constraints with your own judgment about what the output type should be. Agent 1 designed the workflow; Agent 3 generates tests that MATCH the design.

## Generative / Non-Text Output Domains (Image / Audio / Video Generation)

When a sub-task involves GENERATING non-text output (images, audio, video, code artifacts):
- input_type: always "text" (the generation prompt IS text)
- output_type: use "action" (the API performs generation and returns a result reference)
- input_data: write a REALISTIC, detailed generation prompt. Examples:
  - Image: "A watercolor painting of a golden retriever puppy playing in autumn leaves, soft lighting, warm color palette, high detail"
  - Audio: "Generate a 30-second jazz piano loop at 120 BPM in C major, suitable for a coffee shop ambiance"
  - Code: "Write a Python function that implements binary search on a sorted list, with type hints and docstring"
- expected_output: DESCRIBE the ideal result in text form (since the output itself is non-textual):
  - Image: "JSON response with image_url field pointing to a generated image showing a golden retriever puppy in an autumn scene with warm watercolor style"
  - Audio: "JSON response with audio_url field, 30-second duration, jazz piano genre"
  - Code: "A Python function named binary_search with correct type hints, O(log n) complexity"
- judgement_criteria: use these eval_types:
  - "contains_key_info" — API returned a valid result reference (URL, base64, file path)
  - "format_compliance" — response structure matches the API's documented format
  - "subjective_quality" — the generated content matches the prompt's intent (for LLM judge evaluation of the text description of the output)

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

def _format_test_plan(test_plan) -> str:
    """
    Render Agent 1's TestPlan as authoritative per-scope generation specs.

    When present, this OVERRIDES the general architecture annotations —
    Agent 3 follows these specs exactly instead of guessing.
    """
    if not test_plan or not getattr(test_plan, "scope_specs", None):
        return ""

    lines = [
        "## TEST PLAN (AUTHORITATIVE — from Agent 1)",
        "",
        "Agent 1 designed these per-scope test specifications. Follow them EXACTLY.",
        f"Total target: {test_plan.total_test_target} test cases.",
        "",
    ]

    for spec in test_plan.scope_specs:
        lines.append(f"### Scope: {spec.scope_id}")
        lines.append(f"  test_mode: {spec.test_mode}")
        lines.append(f"  input_type: {spec.input_type} (USE THIS — do not override)")
        lines.append(f"  output_type: {spec.output_type} (USE THIS — do not override)")
        lines.append(f"  test_count_target: {spec.test_count_target}")
        lines.append(f"  requires_user_files: {spec.requires_user_files}")
        lines.append(f"  evaluation_focus: {', '.join(spec.evaluation_focus)}")
        lines.append(f"  input_description: {spec.input_description}")
        lines.append(f"  expected_output_description: {spec.expected_output_description}")
        lines.append(f"")
        lines.append(f"  SAMPLE INPUT (use as template for variations):")
        lines.append(f"  {spec.sample_input}")
        lines.append(f"")
        lines.append(f"  SAMPLE OUTPUT (use as template for expected_output):")
        lines.append(f"  {spec.sample_output}")

        if spec.upstream_output_shape:
            lines.append(f"")
            lines.append(f"  UPSTREAM OUTPUT SHAPE (this scope receives data shaped like this):")
            lines.append(f"  {spec.upstream_output_shape}")
            lines.append(f"  Your input_data for this scope MUST match this shape — simulate upstream output.")

        if spec.file_description:
            lines.append(f"  File description: {spec.file_description}")
        lines.append("")

    if test_plan.notes:
        lines.append(f"Test plan notes: {test_plan.notes}")

    return "\n".join(lines)


def _format_workflow_architecture(workflow) -> str:
    """
    Render the workflow DAG as a compact architecture summary for Agent 3.

    Shows the data flow between steps so Agent 3 knows:
    - What each step produces (output_format)
    - What feeds each step (input_from)
    - The dependency chain (what runs before what)

    Returns empty string when no workflow exists (single-scope / legacy).
    """
    if not workflow or not getattr(workflow, "steps", None):
        return ""
    lines = ["## Workflow Architecture (from Agent 1)"]
    lines.append("Data flows through these steps in order. Test cases for each step must")
    lines.append("match its expected input/output format.\n")
    for step in workflow.steps:
        deps = f" (after {', '.join(step.depends_on)})" if step.depends_on else " (first step)"
        lines.append(
            f"  {step.id} [{step.role}]{deps}\n"
            f"    Input: {step.input_from or 'user'} -> Output: {step.output_format}\n"
            f"    \"{step.description}\""
        )
    lines.append("")
    lines.append(
        "For downstream steps (input_from != user), your test case input_data should "
        "SIMULATE what the upstream step would produce. Example: if step_1 is OCR "
        "producing structured_json, then step_2's test cases should use a realistic "
        "JSON object with extracted fields as input_data, NOT a raw document."
    )
    return "\n".join(lines)


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
    # When Agent 1 produced a workflow blueprint, enrich each sub-task
    # with the corresponding step's architectural constraints so Agent 3
    # generates tests with the EXACT input/output types the architecture
    # expects. Without this, Agent 3 guesses independently and may
    # produce tests misaligned with the workflow design.
    blueprint = user_understanding.workflow
    step_by_cap: dict[str, "WorkflowStep"] = {}
    if blueprint and blueprint.steps:
        from puzzleeval.schemas import WorkflowStep
        for step in blueprint.steps:
            step_by_cap[step.capability.strip().lower()] = step

    subtask_lines = []
    for i, st in enumerate(user_understanding.sub_tasks, 1):
        lines = [
            f"{i}. {st.description}",
            f"   Capability: {st.capability}",
            f"   Search keywords: {', '.join(st.search_keywords)}",
        ]

        # Match this sub-task to a workflow step via capability
        matched_step = step_by_cap.get(st.capability.strip().lower())
        if matched_step:
            lines.append(f"   [Architecture] step_id={matched_step.id}, role={matched_step.role}")
            lines.append(f"   [Architecture] output_format={matched_step.output_format}")
            input_desc = (
                "user provides input directly"
                if matched_step.input_from == "user" or matched_step.input_from is None
                else f"receives output from {matched_step.input_from}"
            )
            lines.append(f"   [Architecture] input_source={input_desc}")
            if st.requires_test_files:
                lines.append(f"   [Architecture] requires_test_files=True (file-based input)")
            else:
                lines.append(f"   [Architecture] requires_test_files=False (text/synthetic input)")

        subtask_lines.append("\n".join(lines))
    subtasks_text = "\n\n".join(subtask_lines)

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

    # ── Test Plan integration ──
    # When Agent 1 produced a test_plan, include it as the primary
    # instruction for what to generate. This replaces independent guessing
    # with directed execution. When no test_plan exists, fall back to the
    # architecture annotations + general instructions.
    test_plan_section = _format_test_plan(user_understanding.test_plan)

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

{_format_workflow_architecture(user_understanding.workflow)}

{test_plan_section}

## Target
Generate approximately {target_total} test cases total ({base_per_subtask + workflow_bonus} per sub-task).
Ensure every sub-task is covered across all 6 testing dimensions.
Use IDs starting from tc-001.

IMPORTANT: When a Test Plan is provided above, it is AUTHORITATIVE — follow the per-scope specs exactly (input_type, output_type, sample_input shape, test_count_target). When [Architecture] annotations appear but no Test Plan, use them to set output_type and design input_data that matches the workflow's data flow."""

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
