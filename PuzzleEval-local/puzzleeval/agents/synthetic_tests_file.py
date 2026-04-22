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

The user has uploaded real files. You are the "ground truth oracle" — READ each file carefully and GENERATE test cases that exercise the scope's capability against real content.

## The two-phase process

**Phase 1 — Content inventory.** Before generating tests, walk every file and classify:
  - `matches_scope` — file content is genuinely testable at this scope (e.g. an invoice for an OCR scope)
  - `off_topic` — file doesn't match the scope's expected domain (e.g. a wedding photo for an OCR-invoice scope). Do NOT generate tests from these; include them in `coverage_summary.off_topic_files` so the caller can warn the user.
  - `covered_dimensions` — which canonical coverage dimensions this file naturally exercises based on its actual content. A file can cover multiple dimensions at once (e.g., a damaged handwritten invoice covers `happy_path` + `edge_case` + `error_resilience` simultaneously). Pick from: `happy_path`, `input_variation`, `edge_case`, `scale`, `domain_specific`, `error_resilience`.

**Phase 2 — Test count follows INPUT count, not coverage count.** This is the core rule:

**EMIT EXACTLY ONE TestCase PER UNIQUE INPUT.** For file-based scopes, "unique input" means unique source file (content-wise, not filename-wise — near-duplicates collapse). For any test, the `judgement_criteria` list packs ALL dimensions that input naturally exercises; the `tags` list names those same dimensions.

**Why this is the general rule, not an optimization:**

Each TestCase triggers exactly one harness invocation — one paid API call against one input. The TestCase `judgement_criteria` list already supports multi-dimensional grading via multiple criteria with distinct `eval_type` + `weight`. So coverage expands by adding CRITERIA to one test, not by duplicating the input across multiple tests.

Multiple tests against the same input don't add information in any modality:
  - Deterministic APIs (OCR, structured extraction, classification) — same input gives the same response, so the second test just grades criteria against the same output the first test already produced. Pure API-call waste.
  - Non-deterministic APIs (generative, chat, temperature>0) — the architecture doesn't compare responses ACROSS tests; each test's criteria are evaluated only against that test's own response. Running the same input N times doesn't sample variance into criteria scoring, so you learn nothing the first run didn't already tell you, while paying N× the cost. If variance is what you want to test, that's a separate concern (repeated runs of the SAME test with statistical assertions) which no TestCase shape currently supports.

So: one TestCase per unique input, in every modality that reads this prompt. Don't optimize; just don't duplicate.

**Sizing a test's criteria list (this is where coverage now lives):**
  - 2-3 criteria if the input is simple (one or two dimensions visible)
  - 4-6 criteria if the input is rich (3+ dimensions visible — e.g., happy_path fields + edge_case vendor-name ambiguity + input_variation column layout)
  - Weights sum to ~1.0 within each test
  - Each criterion's `eval_type` targets the specific thing being checked (exact_match for numeric totals, semantic_similarity for free-text fields, contains_key_info for line-item presence, format_compliance for date/phone formats)
  - Tags reflect the UNION of dimensions the criteria collectively cover

**Near-duplicate inputs:** if you see multiple user files that look essentially identical (same template, same layout, different data), emit ONE TestCase using one representative and list the duplicate set in `coverage_summary.near_duplicates`. The caller warns the user they uploaded redundant files.

**Coverage gap reporting:** if the user's files don't cover all relevant dimensions (e.g., all invoices are clean — no `error_resilience` input), list the uncovered dimensions in `coverage_summary.gaps`. Text-mode Agent 3 — which runs in parallel and generates SYNTHETIC tests with DIFFERENT inputs — will cover those gaps. Your job is not to fabricate fake files; your job is to honestly report what the user's real files cover and what they don't.

**Test count arithmetic:** target `test_count = unique_file_count - off_topic_files - near_duplicate_redundancy`. With 5 user files where 1 is off-topic and 2 are near-duplicates of another, you emit 3 TestCases. Not 3×k for some k>1.

Historical note: an earlier version of this prompt encouraged "2-3 tests per medium-variety file, 3-5 per high-variety" as a way to reward coverage. This confused TEST count (inputs processed) with CRITERION count (dimensions graded) and produced runs where 3 user files generated 8 TestCases — 5 of them byte-identical file copies against the same candidate API, wasting 62% of the per-run API budget for zero extra information. The current rule separates those concerns.

## Coverage Matrix

Tag each test case with applicable dimensions from the canonical set (single source of truth lives at puzzleeval.config.CANONICAL_COVERAGE_DIMENSIONS):
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

## input_type Values (file-based scopes — common subset)

File-based test cases most often use these five values. The full
VALID_INPUT_TYPES enum supports more (voice_turn, voice_conversation,
webhook_event, etc.) — use them when a user-uploaded audio/webhook-payload
file is the primary test input (e.g., uploaded WAV for a phone-agent scope).

- "document_content" — invoices, contracts, receipts, documents
- "image_description" — photos, diagrams, screenshots
- "structured_data" — spreadsheets, CSV data
- "text" — plain text documents
- "audio_content" — uploaded audio files (WAV, MP3); use for transcription scopes
- "file_reference" — catch-all when the uploaded file is a binary blob the
  harness treats as a reference (ZIP, MP4, archive) rather than content
- "voice_turn" / "voice_conversation" — when the user uploaded sample audio
  for a voice/phone scope; emit matching scripts in the single-turn or
  multi-turn shape respectively

## output_type Values (authoritative — matches VALID_OUTPUT_TYPES)

Common file-based outputs:
- "extraction" — extract specific fields/data
- "structured_json" — produce structured JSON output
- "classification" — categorize the content
- "free_text" — generate natural language analysis

Other VALID_OUTPUT_TYPES values you may stamp when the scope calls for them:
"action", "media_url", "code", "audio_content", "webhook_callback",
"outbound_message", "voice_turn", "voice_conversation".

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

def _transcribe_audio_for_ground_truth(file_path: str, logger) -> str:
    """Use the transcription plugin to extract spoken text from an audio file.

    Falls back to a clear "transcription unavailable — no STT provider
    configured" message when no provider is credentialed; Agent 3F's LLM
    then knows the file exists but its content can't be inspected, and
    will generate test cases with that constraint in mind (e.g. "test
    that the API accepts the file and returns SOMETHING; ground truth
    can't be verified without STT").
    """
    try:
        from puzzleeval.tool_plugins import get_plugin
        plugin = get_plugin("transcription")
        if plugin is None:
            return "(transcription plugin not registered)"
        ok, reason = plugin.is_available()
        if not ok:
            return (
                f"(audio transcription unavailable — {reason}; "
                "ground truth cannot be inspected from the file alone)"
            )
        # Drive the plugin's evaluate path with a dummy "expected" so it
        # just transcribes — the helper exists for evaluation but we reuse
        # its STT call as a free-standing ground-truth extractor.
        from puzzleeval.tool_plugins.transcription import (
            _PROVIDER_DISPATCH, _select_provider,
        )
        provider = _select_provider()
        if provider is None:
            return "(no STT provider configured)"
        provider_name, env_vars = provider
        from pathlib import Path as _P
        try:
            transcript = _PROVIDER_DISPATCH[provider_name](
                _P(file_path), list(env_vars.values())[0],
            )
        except Exception as exc:
            logger.warning(
                f"audio ground-truth STT failed for {file_path}: {exc}",
                extra={"operation": "agent3f_stt_failure"},
            )
            return f"(STT failed: {exc})"
        return transcript or "(transcription returned empty text)"
    except Exception as exc:
        logger.warning(
            f"audio ground-truth helper crashed for {file_path}: {exc}",
            extra={"operation": "agent3f_stt_helper_crash"},
        )
        return f"(transcription helper error: {exc})"


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
    AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".aiff"}
    for i, file_path in enumerate(file_paths):
        filename = os.path.basename(file_path)
        ext = os.path.splitext(filename)[1].lower()

        # Add a label for each file
        content_blocks.append({
            "type": "text",
            "text": f"\n--- File {i + 1}: {filename} (path: {file_path}) ---\n",
        })

        # Audio files: vision can't listen. Use the transcription plugin to
        # extract the spoken content as ground truth. When no STT provider
        # is credentialed the plugin returns a fallback message and we tell
        # the LLM judge what's missing — no crash, no silent skip.
        if ext in AUDIO_EXT:
            transcript_text = _transcribe_audio_for_ground_truth(file_path, logger)
            content_blocks.append({
                "type": "text",
                "text": (
                    f"This is an AUDIO file. Spoken content (transcribed):\n"
                    f"{transcript_text}\n\n"
                    f"Generate test cases referencing this file via "
                    f"test_file_path. Use the transcript above as ground "
                    f"truth — expected_output should reflect what the API "
                    f"is supposed to do with this spoken content."
                ),
            })
            continue

        try:
            parsed = parse_file(file_path)

            if isinstance(parsed, dict):
                # Binary file (PDF/image) → native content block
                content_blocks.append(parsed)
            else:
                # Text file (DOCX/CSV/TXT) or pass-through (audio/video/binary
                # other than the audio extensions handled above) → text block
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

    Behavior when no files are provided:
      Falls back to text-only synthetic generation via Agent 3 instead of
      raising. This handles the common case where a user describes a
      file-based capability ("transcribe audio recordings") but doesn't
      have sample files on disk to upload. The downstream test execution
      gets text-described test cases that exercise the API's input-form
      tolerance (and the API itself decides whether it can serve text
      input — see Gap 3 fallback in implement_test_env.py).
    """
    if not input_data.test_file_paths:
        # Graceful degradation: synthesize text-based test cases instead of
        # raising. This is the general-purpose fallback for "user wants
        # file-based testing but has no files" — the test runner will
        # surface "INCOMPATIBLE: file required" as a real failure when
        # the API genuinely needs a file, which is the right signal.
        from puzzleeval.agents.synthetic_tests import run_synthetic_tests_agent
        logger = get_logger("agent_3f_file_tests")
        logger.info(
            "Agent 3F: no files provided — degrading to Agent 3 text-only synthesis",
            extra={"operation": "agent_3f_text_only_fallback", "trace_id": input_data.trace_id},
        )
        return run_synthetic_tests_agent(input_data)

    # ★ CORE LINE 1: Create the API client
    # Central factory — 120 s timeout + max_retries=3 (see anthropic_client.py).
    from puzzleeval.anthropic_client import build_client
    client = build_client(api_key=ANTHROPIC_API_KEY)

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
    # parse_with_fallback handles grammar-budget rejections — same pattern
    # as Agents 1/2/3/4. Agent3Result on file-based generation tends to be
    # even larger (one TestCase per file × multiple weighted criteria).
    start_time = time.time()
    try:
        from puzzleeval.agent_preamble import with_preamble
        from puzzleeval.structured_output import parse_with_fallback
        response = parse_with_fallback(
            client=client,
            model=DEFAULT_MODEL,
            max_tokens=GENERATION_MAX_TOKENS,
            system=[{"type": "text", "text": with_preamble(SYSTEM_PROMPT)}],
            messages=[{"role": "user", "content": content_blocks}],
            output_format=Agent3Result,
            extra={},
            trace_id=input_data.trace_id,
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
