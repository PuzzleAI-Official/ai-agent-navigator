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
# [CORE] System Prompt — loaded from templates/system_prompt.md (Phase 7)
# ============================================================================
# Different from Agent 3's text prompt: here Claude is READING real files
# and generating ground truth based on what it actually sees. It's acting
# as the "oracle" — the most capable model establishing what the correct
# output should be.
# ============================================================================
from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=1)
def _load_system_prompt() -> str:
    """Load the SYSTEM_PROMPT body from templates/system_prompt.md.

    Phase 7: prompt content extracted from inline triple-quoted Python
    string into a markdown file for easier editing/diffing. Loaded once
    per process via lru_cache; the cost is one resources.read_text call
    at module import time.
    """
    return resources.files("puzzleeval.agents.agent3f").joinpath(
        "templates", "system_prompt.md",
    ).read_text(encoding="utf-8")


SYSTEM_PROMPT = _load_system_prompt()


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
