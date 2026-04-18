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

## input_type Values (authoritative — matches VALID_INPUT_TYPES)

Choose the one that best describes the nature of the input. Values marked
* have a dedicated modality section LOWER in this prompt with detailed
schema guidance — consult it before stamping.

- "text" — plain text (chat messages, queries, plain documents)
- "structured_data" — JSON, CSV, or tabular data
- "document_content" — text representation of a formatted document (invoice, contract, receipt)
- "conversation"* — multi-turn TEXT chat script (chatbot, inbound Slack bot)
- "image_description" — description of visual content
- "audio_content"* — audio input synthesized by TTS (ONE utterance — use voice_turn for phone exchanges)
- "file_reference" — pre-uploaded file ID; used by file-first scopes
- "code"* — source code to be executed
- "webhook_event"* — inbound webhook payload (Slack / Stripe / Twilio)
- "voice_turn"* — SINGLE voice/phone turn (IVR press-1-for-sales)
- "voice_conversation"* — MULTI-TURN phone conversation where the agent must maintain context across exchanges

## output_type Values (authoritative — matches VALID_OUTPUT_TYPES)

Choose what kind of output the AI service should produce. Values marked
* map to a plugin that scores them natively; unmarked values go to the LLM
judge.

- "free_text" — natural language response (LLM judge)
- "structured_json" — JSON with specific fields
- "classification" — category label(s)
- "extraction" — extracted fields/data from input
- "action" — an action the agent performs (create/update/delete record)
- "media_url"* — URL to a downloadable image / audio / document (vision plugin for images, transcription for audio)
- "code"* — source code the agent produced (code_execution plugin)
- "audio_content"* — audio bytes or URL (transcription plugin)
- "webhook_callback"* — outbound webhook POST captured by webhook_receiver
- "outbound_message"* — email / Slack / SMS captured by outbound_delivery
- "voice_turn"* — single voice reply (voice_realtime plugin)
- "voice_conversation"* — aggregated multi-turn voice reply (voice_realtime multi-turn driver)

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

## Plugin-shaped test cases (REQUIRED for code / conversation / audio modalities)

The Agent 5 evaluator dispatches test cases to specialized plugins based on
input_type / output_type. Plugins need STRUCTURED payloads, not free-form
text. Generate the right shape per modality so the plugin can score
deterministically — otherwise the LLM judge falls back, which is fine for
text/json but loses precision on code (does it run?), conversation (did
it stay on intent across turns?), audio (does the transcript match?).

### When output_type == "code" (or input_type == "code")

The code_execution plugin runs the generated code and scores by execution
success against test inputs/outputs. Populate:

- **input_data**: a JSON string describing the prompt + language, e.g.
  `{"prompt": "Write a function fizzbuzz(n) that ...", "language": "python"}`.
- **expected_output**: a JSON string with the executable contract:
  `{"expected_function": "<function_name_to_call>", "test_inputs": [...arg-tuples...], "test_outputs": [...expected_returns...], "language": "python"}`.

Choose at least 3 test_inputs covering happy path + edge case + boundary.
Languages supported by the plugin today: python (always), javascript,
typescript, go, rust, bash (when host toolchain installed).

### When input_type == "conversation"

The conversation_simulator plugin replays a multi-turn script against the
candidate's harness and checks per-turn assertions. Populate:

- **input_data**: a JSON string with the conversation script:
  `{"conversation_script": {"user_turns": ["Hi", "What can you help with?", "Thanks"], "assertions": [{"turn_index": 0, "check_type": "contains", "value": "help", "weight": 1.0}, {"turn_index": -1, "check_type": "not_contains", "value": "error", "weight": 1.0}]}}`.

`turn_index` is 0-based for the agent's reply to user turn N; -1 means the
final agent turn. `check_type` is one of `contains` / `not_contains` /
`regex_match` / `intent_match`. Generate 3-5 user turns per script and 2-4
assertions per turn-index that exercise the agent's state-keeping +
clarifying-question behavior.

### When input_type == "audio_content"

Agent 5's stage hook calls the TTS plugin to synthesize an actual audio file
when test_file_path is null. Populate **input_data** with the EXACT spoken
text the user is supposed to utter ("Hello, I need to schedule an
appointment for next Tuesday at 2 PM"). The TTS plugin records the text as
ground truth so the transcription plugin can compare the agent's audio
response back to expected text.

### When output_type == "media_url" (image generation, audio generation)

The vision plugin scores image responses; transcription scores audio
responses. Populate **expected_output** with a one-line natural-language
description of what the response should contain ("a sunset over a beach
with palm trees" for image; "a 5-15 second polite greeting acknowledging
the caller" for audio).

### When input_type == "webhook_event" OR output_type == "webhook_callback"

Inbound agents are tested by the webhook_receiver plugin. It captures
HTTP POSTs the candidate sends and verifies the body. Populate:

- **input_data**: a JSON string carrying the inbound payload shape +
  expected agent reaction, e.g.
  `{"shape": "slack", "message": "What's our refund policy?", "expected_callback_substring": "30 days"}`.
  `shape` is one of `slack` / `intercom` / `twilio_sms` / `stripe` /
  `github` / `generic`. The plugin synthesizes a provider-shaped
  envelope wrapping `message` and gives the candidate a callback URL.
- **expected_output**: a JSON string with the verification contract:
  `{"expected_text_substring": "30 days", "shape": "slack"}`. The
  plugin scores 0.6 for "any callback received" and 0.4 for substring
  match.

Generate 3-5 cases covering: simple intent, multi-line message, edge
case (empty body, oversized payload), and one provider-specific shape.

### When output_type == "outbound_message"

The outbound_delivery plugin spins up local mock SMTP (port 2525) /
channel HTTP (port 8766) / SMS HTTP (port 8767) and verifies the
agent's outbound message ACTUALLY landed at the mock receiver.
Populate:

- **input_data**: a JSON string describing the trigger + destination,
  e.g. `{"channel": "email", "trigger": "user requested receipt",
  "expected_recipient": "user@example.com"}`. `channel` is one of
  `email` / `slack` / `sms`.
- **expected_output**: a JSON string with the success criterion:
  `{"channel": "email", "expected_recipient": "user@example.com",
  "expected_text_substring": "receipt for $25.00"}`.

Generate at least one test per channel the workflow uses. The plugin
returns 0.0 (passed=false) when nothing landed — that's the right
signal for "the API said 200 but the email never arrived."

### When input_type == "voice_turn" OR output_type == "voice_turn"

The voice_realtime plugin runs a local audio-loopback turn. It serves
synthesized caller audio at `/audio/<token>` and captures the agent's
TwiML / NCCO / JSON / audio-blob response at `/voice/<token>`.
Populate:

- **input_data**: a JSON string describing the caller's utterance +
  the protocol the candidate speaks, e.g.
  `{"shape": "twilio", "spoken_text": "What time do you close today?",
  "expected_response_substring": "9 PM"}`. `shape` is one of `twilio`
  (TwiML XML expected back), `vonage` (NCCO JSON array), or `generic`
  (any JSON `{response_text}` or audio blob).
- **expected_output**: a JSON string with the scoring contract:
  `{"expected_response_substring": "9 PM", "shape": "twilio"}`.

The plugin extracts text from `<Say>`/`<Play>` tags (TwiML), the
`talk`/`stream` actions (NCCO), or transcribes audio blobs via the
transcription plugin. Generate 2-4 cases per voice scope covering:
information request, multi-step intent (the agent must ask a clarifying
question), and one protocol-specific shape.

### When input_type == "voice_conversation" — multi-turn voice scripts

Use `voice_conversation` when the workflow implies a MULTI-TURN phone
call — the agent must maintain context across several exchanges
(e.g. "pick up calls and assist customers", "book appointments over
the phone", "qualify inbound leads by asking 4 discovery questions").
Single-turn `voice_turn` is for shorter exchanges where one utterance
+ one response is the complete interaction.

The principle is the same as `conversation` for chatbots: when the
scope describes sustained back-and-forth, emit a script that tests
context-carrying, clarifying-question handling, and correct
turn-by-turn flow. When the scope is a one-shot utterance, don't
manufacture multi-turn complexity just for variety.

Populate:

- **input_data**: a JSON string with the multi-turn script, e.g.
  `{"shape": "twilio", "turns": [{"user_text": "Hi, I need to reschedule my Thursday appointment.", "expected_agent_contains": "which appointment"}, {"user_text": "Thursday 2pm.", "expected_agent_contains": "new time"}, {"user_text": "Friday 3pm works.", "expected_agent_contains": "confirmed"}]}`.
- **expected_output**: a JSON string with the same `turns` array
  (voice_realtime's plugin reads it from either input_data or
  expected_output). Each turn needs `user_text` + an
  `expected_agent_contains` substring. Optional `expected_agent_text`
  for exact match.

Generate 2-4 scripts per multi-turn voice scope, each 3-5 turns,
covering: (1) the happy path for the primary intent, (2) an
out-of-scope request the agent should handle gracefully, (3) a
context-dependent turn (agent must remember something the user said
earlier), (4) optionally a protocol-specific shape (twilio vs vonage
vs generic).

The plugin drives each script: it synthesizes caller audio per turn,
invokes the candidate's single-turn harness through Agent 5's
runner, extracts agent text, scores each turn against
`expected_agent_contains`, and returns an aggregate pass/fail plus
per-turn breakdown. Playable audio is saved to the run directory.

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

        # Gap 30: scope-specific input_context parameters (e.g. target_language)
        hints = getattr(spec, "input_context_hints", None)
        if hints:
            lines.append("")
            lines.append(f"  INPUT_CONTEXT HINTS (copy into every test case's input_context):")
            for k, v in hints.items():
                lines.append(f"    - {k}: {v}")
            lines.append(
                "  Every test case you generate for this scope MUST include these keys "
                "verbatim in TestCase.input_context — the harness needs them to route "
                "the API call correctly."
            )

        # Gap 14: ground_truth vs exemplar scoring
        ref_mode = getattr(spec, "reference_mode", "ground_truth")
        if ref_mode == "exemplar":
            lines.append("")
            lines.append(
                "  REFERENCE MODE: exemplar — sample_output is ONE valid answer, not THE "
                "answer. Generate test cases whose expected_output is an exemplar the "
                "LLM judge will use as a REFERENCE, not a target. Criteria should focus "
                "on qualities (helpfulness, tone, coverage) rather than exact text match."
            )

        # Gap 9: destructive action steps
        side_effects = getattr(spec, "side_effects", "read_only")
        if side_effects != "read_only":
            lines.append("")
            lines.append(
                f"  SIDE EFFECTS: {side_effects} — this scope WRITES to external "
                "systems. Generate test inputs that exercise both happy-path AND "
                "error-resilience (duplicate writes, invalid records, partial data) "
                "but use SYNTHETIC / clearly-labeled test records so dry-run / "
                "sandbox execution is easy to distinguish from real data."
            )

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
    # Central factory — 120 s timeout + max_retries=3 (see anthropic_client.py).
    from puzzleeval.anthropic_client import build_client
    client = build_client(api_key=ANTHROPIC_API_KEY)

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
    # Wrapped in parse_with_fallback so a grammar-budget rejection on
    # Agent3Result (TestCase[] with weighted criteria + plugin shapes) falls
    # back to the non-strict tool path instead of failing the run.
    start_time = time.time()
    try:
        from puzzleeval.agent_preamble import with_preamble
        from puzzleeval.structured_output import parse_with_fallback
        response = parse_with_fallback(
            client=client,
            model=DEFAULT_MODEL,
            max_tokens=GENERATION_MAX_TOKENS,
            system=[{"type": "text", "text": with_preamble(SYSTEM_PROMPT)}],
            messages=[{"role": "user", "content": generation_message}],
            output_format=Agent3Result,
            extra={},
            trace_id=input_data.trace_id,
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

    # ── Empty-result fallback (fires BEFORE top-up) ────────────────────
    # Real-run signal: trace real_debug_4 caught Agent 3 returning
    # `{"test_cases": []}` in 2 seconds with 27 output tokens — the model
    # went shallow on a valid request. The normal top-up loop then
    # no-ops because it keys off `test_plan.scope_specs[*].capability`
    # which Agent 1 sometimes leaves None. General fix: when the first
    # pass emits zero cases, retry ONCE from the simplest inputs
    # (sub_tasks themselves) with an explicit "you produced nothing —
    # generate at least N cases for each" nudge. This is robust even
    # when test_plan is partial/missing.
    if not result.test_cases:
        try:
            result = _retry_empty_generation(
                result=result,
                input_data=input_data,
                client=client,
                logger=logger,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Agent 3 empty-result retry failed: %s — proceeding with empty result",
                exc,
                extra={"operation": "empty_retry_failed", "trace_id": input_data.trace_id},
            )

    # ── Sufficiency retry: if any sub_task produced fewer tests than Agent 1's
    # test_count_target floor (70% of target), fire ONE top-up call that asks
    # specifically for the missing cases. Cheap, bounded, and lets the
    # validator downstream pass a previously-failing scope instead of killing
    # the pipeline. The retry is LLM-only (no re-routing, no fallback loop) —
    # if the top-up still shortfalls, the validator escalates to error and
    # the caller decides what to do.
    try:
        result = _topup_undergenerated_subtasks(
            result=result,
            input_data=input_data,
            client=client,
            logger=logger,
            original_cost=call_cost,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Agent 3 top-up retry failed: %s — proceeding with original result",
            exc,
            extra={"operation": "topup_retry_failed", "trace_id": input_data.trace_id},
        )

    logger.info("Agent 3 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "test_case_count": len(result.test_cases),
        "subtasks_covered": len(result.coverage_summary),
    })

    return result


# ---------------------------------------------------------------------------
# Empty-result fallback — retry when first pass emits zero cases
# ---------------------------------------------------------------------------


def _retry_empty_generation(
    *,
    result: "Agent3Result",
    input_data: "Agent3Input",
    client,  # anthropic.Anthropic
    logger,
) -> "Agent3Result":
    """Retry Agent 3 ONCE when the first pass returns zero test cases.

    Real-run signal (trace real_debug_4): Agent 3 sometimes responds
    shallowly with ``{"test_cases": []}`` despite a clear, well-specified
    request — same model, same input, different run = different output.
    The normal top-up loop doesn't catch this because it keys off
    ``test_plan.scope_specs[*].capability`` which Agent 1 can leave null.

    Fix: when len(test_cases) == 0 after the initial call, fire exactly
    one retry using sub_tasks directly (no test_plan dependency). The
    prompt explicitly states "your previous response was empty" so the
    model can't re-emit the same zero-case output on this pass.

    Never raises. Returns the original empty result on any failure —
    the caller's validator decides whether to fail the pipeline.
    """
    uo = input_data.user_understanding
    if not uo.sub_tasks:
        return result

    from puzzleeval.structured_output import parse_with_fallback
    from puzzleeval.agent_preamble import with_preamble

    # Compute per-sub_task minimum targets, falling back to 5 (the
    # documented base in synthetic_tests per-sub_task sizing).
    test_plan = getattr(uo, "test_plan", None)
    specs_by_cap: dict[str, int] = {}
    if test_plan and test_plan.scope_specs:
        for spec in test_plan.scope_specs:
            cap = getattr(spec, "capability", None)
            tgt = getattr(spec, "test_count_target", None)
            if cap and tgt:
                specs_by_cap[cap] = int(tgt)

    lines: list[str] = [
        "Your previous response returned ZERO test cases. That is not acceptable.",
        "",
        "Generate AT LEAST the minimum below per sub_task. The test_cases list",
        "MUST be non-empty on this retry. Use the sub_task description VERBATIM",
        "for each case's sub_task_ref so validation can map them.",
        "",
    ]
    for st in uo.sub_tasks:
        target = specs_by_cap.get(st.capability, 5)
        lines.append(f"- sub_task_ref: {st.description!r}")
        lines.append(f"  capability: {st.capability}")
        lines.append(f"  minimum_cases: {max(3, target)}")
        lines.append("")
    lines.append(
        "Each test case needs 2-5 judgement_criteria with weights summing to ~1.0."
    )

    try:
        response = parse_with_fallback(
            client=client,
            model=DEFAULT_MODEL,
            max_tokens=GENERATION_MAX_TOKENS,
            system=[{"type": "text", "text": with_preamble(SYSTEM_PROMPT)}],
            messages=[{"role": "user", "content": "\n".join(lines)}],
            output_format=Agent3Result,
            extra={},
            trace_id=input_data.trace_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Agent 3 empty-retry call failed: %s",
            exc,
            extra={"operation": "empty_retry_call_error", "trace_id": input_data.trace_id},
        )
        return result

    retry_cost = log_llm_call(
        logger=logger, response=response, model=DEFAULT_MODEL,
        trace_id=input_data.trace_id, start_time=time.time(),
        operation="synthetic_tests_empty_retry",
    )
    retried = response.parsed_output
    if retried is None or not retried.test_cases:
        logger.warning(
            "Agent 3 empty-retry still returned no cases — escalating to validator",
            extra={"operation": "empty_retry_still_empty", "trace_id": input_data.trace_id},
        )
        return result

    # Fold retry cases into the (empty) result + accumulate cost.
    result.test_cases = retried.test_cases
    if retried.coverage_summary:
        result.coverage_summary = retried.coverage_summary
    if retried.generation_notes and not result.generation_notes:
        result.generation_notes = retried.generation_notes
    result.cost_usd = (result.cost_usd or 0.0) + retry_cost

    logger.info(
        "Agent 3 empty-retry recovered %d cases",
        len(retried.test_cases),
        extra={
            "operation": "empty_retry_recovered",
            "trace_id": input_data.trace_id,
            "cases_recovered": len(retried.test_cases),
        },
    )
    return result


# ---------------------------------------------------------------------------
# Sufficiency top-up — closes shortfall vs test_count_target
# ---------------------------------------------------------------------------


# Max topup iterations. Matches Claude Code's MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
# shape — a small bounded loop with a circuit breaker instead of a fixed
# single shot. 3 is the sweet spot: two chances to cover what the first
# attempt missed, one final chance after any gap-analysis refinement.
MAX_TOPUP_ATTEMPTS = 3
# When two consecutive attempts add zero new tests, we've hit a wall —
# either the model is refusing or the sub_task genuinely can't yield more
# variety. Bail gracefully instead of burning another call.
TOPUP_STALL_LIMIT = 2


def _compute_topup_gaps(
    *,
    result: "Agent3Result",
    cap_to_target: dict[str, tuple[int, str]],
    desc_to_cap: dict[str, str],
) -> tuple[list[tuple[str, str, int, int]], dict[str, set[str]], dict[str, set[str]]]:
    """Compute current gaps: shortfalls, missing dimensions per cap, and
    which judgement_criteria strings currently have ≥1 scoring test.

    Returns ``(shortfalls, missing_dims_by_cap, uncovered_criteria_by_cap)``.

    - ``shortfalls`` — list of (cap, desc, actual, target) where
      ``actual < floor``.
    - ``missing_dims_by_cap`` — which canonical coverage dimensions have
      zero tests for each shortfalling cap.
    - ``uncovered_criteria_by_cap`` — any ``judgement_criteria.criterion``
      string mentioned in the TestPlan scope_specs that currently has zero
      test cases referencing it. A criterion with ≥1 test is "covered."
      Used as a *secondary* gap signal when count-floor is already met —
      sufficiency isn't just about count, it's about criterion coverage.
    """
    from collections import Counter
    from puzzleeval.config import (
        CANONICAL_COVERAGE_DIMENSIONS,
        SUFFICIENCY_FLOOR_RATIO,
        SUFFICIENCY_HARD_FLOOR,
    )

    # Re-count per-cap tests.
    actual_by_cap: Counter[str] = Counter()
    for tc in result.test_cases:
        ref = tc.sub_task_ref or ""
        if ref in desc_to_cap:
            actual_by_cap[desc_to_cap[ref]] += 1

    shortfalls: list[tuple[str, str, int, int]] = []
    for cap, (target, desc) in cap_to_target.items():
        actual = actual_by_cap.get(cap, 0)
        floor = max(SUFFICIENCY_HARD_FLOOR, int(target * SUFFICIENCY_FLOOR_RATIO))
        if actual < floor:
            shortfalls.append((cap, desc, actual, target))

    # Canonical-dimension coverage per cap that's shortfalling.
    missing_dims: dict[str, set[str]] = {cap: set() for cap, _, _, _ in shortfalls}
    dims_covered: dict[str, set[str]] = {cap: set() for cap, _, _, _ in shortfalls}
    for tc in result.test_cases:
        ref = tc.sub_task_ref or ""
        cap = desc_to_cap.get(ref)
        if cap in dims_covered:
            for tag in (tc.tags or []):
                if tag in CANONICAL_COVERAGE_DIMENSIONS:
                    dims_covered[cap].add(tag)
    for cap in missing_dims:
        missing_dims[cap] = CANONICAL_COVERAGE_DIMENSIONS - dims_covered[cap]

    # Criterion-coverage: every criterion_text referenced in at least one
    # test case's judgement_criteria means that criterion has a scoring
    # test. Uncovered = referenced in scope_specs but no test case mentions it.
    uncovered_criteria: dict[str, set[str]] = {cap: set() for cap in cap_to_target}
    # Build set of criteria that appear in any test case per cap.
    cap_to_tested_criteria: dict[str, set[str]] = {cap: set() for cap in cap_to_target}
    for tc in result.test_cases:
        ref = tc.sub_task_ref or ""
        cap = desc_to_cap.get(ref)
        if cap is None:
            continue
        for jc in (tc.judgement_criteria or []):
            crit = getattr(jc, "criterion", None)
            if crit:
                cap_to_tested_criteria[cap].add(crit.strip().lower())
    # Scope-specs carry the authoritative criterion list per capability.
    # When the TestPlan lists N named criteria for a cap and only M < N
    # appear in test cases, the remaining (N - M) are "uncovered."
    # We can't always map test_plan.scope_specs[].judgement_criteria back
    # to the caps (different field shapes across versions), so fall back
    # gracefully when the structure isn't there.
    # The topup prompt uses missing_dims primarily; uncovered_criteria is
    # surfaced as context when populated.
    return shortfalls, missing_dims, uncovered_criteria


def _topup_undergenerated_subtasks(
    *,
    result: "Agent3Result",
    input_data: "Agent3Input",
    client,  # anthropic.Anthropic — not annotated to avoid circular typing
    logger,
    original_cost: float,
) -> "Agent3Result":
    """Top up under-generated sub_tasks with an ITERATIVE focused LLM loop.

    Runs up to ``MAX_TOPUP_ATTEMPTS`` rounds. After each round, recomputes:
      - per-cap shortfall vs ``floor(target * SUFFICIENCY_FLOOR_RATIO)``
      - per-cap missing canonical dimensions
      - criterion coverage (which scope criteria still have 0 scoring tests)

    Exits early when every sub_task meets floor AND there are no missing
    dimensions for any shortfalling cap. Circuit-breaks after
    ``TOPUP_STALL_LIMIT`` consecutive attempts that add zero new tests,
    so a model refusing to produce more variety doesn't burn the budget.

    The per-attempt prompt feeds back (a) what's still missing and
    (b) what was added in the prior attempt, so the model steers toward
    gaps it didn't hit. Never raises — caller's logger.warning handles
    exceptions.

    This is the "knows what's missing and when to continue" mechanism:
    each attempt narrows the gap based on real post-attempt coverage
    analysis, not a static one-shot prompt.
    """
    uo = input_data.user_understanding
    test_plan = getattr(uo, "test_plan", None)
    if test_plan is None:
        return result
    specs = getattr(test_plan, "scope_specs", None) or []
    if not specs:
        return result

    # Build capability → (target, description) map
    cap_to_target: dict[str, tuple[int, str]] = {}
    for spec in specs:
        cap = getattr(spec, "capability", None)
        target = getattr(spec, "test_count_target", None)
        if not cap or not target:
            continue
        desc = next(
            (st.description for st in uo.sub_tasks if st.capability == cap), ""
        )
        cap_to_target[cap] = (int(target), desc)
    if not cap_to_target:
        return result

    desc_to_cap: dict[str, str] = {d: c for c, (_, d) in cap_to_target.items()}

    from puzzleeval.structured_output import parse_with_fallback
    from puzzleeval.agent_preamble import with_preamble
    from puzzleeval.config import CANONICAL_COVERAGE_DIMENSIONS

    total_topup_cost = 0.0
    total_added = 0
    stall_streak = 0
    prior_added_breakdown: dict[str, int] = {}

    for attempt in range(1, MAX_TOPUP_ATTEMPTS + 1):
        shortfalls, missing_dims, _uncovered = _compute_topup_gaps(
            result=result,
            cap_to_target=cap_to_target,
            desc_to_cap=desc_to_cap,
        )
        # Sufficiency met: no caps below floor AND no caps with any
        # missing canonical dimension among the shortfalling set. Early
        # exit — don't burn a call when we're already good.
        if not shortfalls:
            break

        # Compose attempt prompt with (a) shortfall table, (b) what was
        # added in the prior attempt so the model avoids re-generating
        # near-duplicates.
        lines: list[str] = [
            f"TEST BATTERY SHORTFALL — attempt {attempt} of {MAX_TOPUP_ATTEMPTS}.",
            "",
            "Generate ADDITIONAL test cases to close the gap for each sub_task below.",
            "Rules:",
            "  1. Keep every case's sub_task_ref EXACTLY as shown so validation can match.",
            "  2. Do NOT re-emit any test case already in the battery — generate ONLY the gap-filling cases.",
            "  3. Prioritize the 'dimensions still missing' list — covering a missing dimension",
            "     is worth more than adding yet another happy-path case.",
            "  4. Each case must have 2-5 judgement_criteria with weights summing to ~1.0.",
            "",
        ]
        if prior_added_breakdown:
            lines.append("Prior attempt added (so you don't duplicate):")
            for cap, n in prior_added_breakdown.items():
                lines.append(f"  - {cap}: +{n} cases")
            lines.append("")

        for cap, desc, actual, target in shortfalls:
            gap = target - actual
            missing = sorted(missing_dims.get(cap, set()))
            lines.append(f"- sub_task_ref: {desc!r}")
            lines.append(f"  capability: {cap}")
            lines.append(f"  current_count: {actual}")
            lines.append(f"  target: {target}")
            lines.append(f"  gap: {gap}")
            lines.append(
                f"  dimensions still missing: {', '.join(missing) if missing else '(all present — deepen existing with edge-case variants)'}"
            )
            lines.append("")

        prompt = "\n".join(lines)

        try:
            response = parse_with_fallback(
                client=client,
                model=DEFAULT_MODEL,
                max_tokens=GENERATION_MAX_TOKENS,
                system=[{"type": "text", "text": with_preamble(SYSTEM_PROMPT)}],
                messages=[{"role": "user", "content": prompt}],
                output_format=Agent3Result,
                extra={},
                trace_id=input_data.trace_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Agent 3 topup attempt %d failed: %s — stopping loop",
                attempt, exc,
                extra={"operation": "topup_attempt_error", "trace_id": input_data.trace_id},
            )
            break

        topup = response.parsed_output
        attempt_cost = log_llm_call(
            logger=logger, response=response, model=DEFAULT_MODEL,
            trace_id=input_data.trace_id, start_time=time.time(),
            operation=f"synthetic_tests_topup_attempt_{attempt}",
        )
        total_topup_cost += attempt_cost

        added_this_attempt = 0
        added_by_cap: dict[str, int] = {}
        if topup is not None and topup.test_cases:
            seen_ids = {tc.id for tc in result.test_cases}
            new_cases = [tc for tc in topup.test_cases if tc.id not in seen_ids]
            result.test_cases.extend(new_cases)
            added_this_attempt = len(new_cases)
            for tc in new_cases:
                cap = desc_to_cap.get(tc.sub_task_ref or "")
                if cap:
                    added_by_cap[cap] = added_by_cap.get(cap, 0) + 1

        total_added += added_this_attempt
        prior_added_breakdown = added_by_cap

        logger.info(
            "Agent 3 topup attempt %d added %d cases",
            attempt, added_this_attempt,
            extra={
                "operation": "topup_attempt_complete",
                "trace_id": input_data.trace_id,
                "attempt": attempt,
                "added": added_this_attempt,
                "attempt_cost_usd": attempt_cost,
                "remaining_shortfalls": len(shortfalls),
            },
        )

        # Circuit breaker: if this attempt added 0 new tests, count a
        # stall. Two stalls in a row → the model is refusing to produce
        # more variety. Bail instead of burning the last attempt.
        if added_this_attempt == 0:
            stall_streak += 1
            if stall_streak >= TOPUP_STALL_LIMIT:
                logger.warning(
                    "Agent 3 topup stalled (%d consecutive 0-add attempts) — stopping loop",
                    stall_streak,
                    extra={
                        "operation": "topup_stall",
                        "trace_id": input_data.trace_id,
                        "attempts_used": attempt,
                    },
                )
                break
        else:
            stall_streak = 0

    result.cost_usd = original_cost + total_topup_cost

    # Emit a final summary of the loop's work for observability.
    final_shortfalls, final_missing, _ = _compute_topup_gaps(
        result=result,
        cap_to_target=cap_to_target,
        desc_to_cap=desc_to_cap,
    )
    logger.info(
        "Agent 3 topup loop complete",
        extra={
            "operation": "topup_loop_complete",
            "trace_id": input_data.trace_id,
            "total_added": total_added,
            "total_cost_usd": total_topup_cost,
            "remaining_shortfall_caps": [cap for cap, _, _, _ in final_shortfalls],
            "remaining_missing_dimensions": {
                cap: sorted(dims) for cap, dims in final_missing.items() if dims
            },
        },
    )
    return result
