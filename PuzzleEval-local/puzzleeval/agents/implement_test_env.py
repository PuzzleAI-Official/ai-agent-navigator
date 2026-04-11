# ============================================================================
# Agent 5: Implement Test Env Agent

import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import anthropic

from puzzleeval.config import (
    AGENT5_BUILDER_MODEL,
    AGENT5_CODE_TIMEOUT,
    AGENT5_MAX_OUTPUT_TOKENS,
    AGENT5_MAX_PARALLEL,
    AGENT5_MAX_TURNS,
    AGENT5_MAX_CANDIDATES,
    AGENT5_MAX_VERIFICATION_RETRIES,
    AGENT6_ERROR_ABORT_THRESHOLD,
    AGENT6_EVAL_MAX_TOKENS,
    AGENT6_EVAL_MODEL,
    AGENT6_MIN_TESTS_BEFORE_ABORT,
    AGENT6_PASS_THRESHOLD,
    AGENT6_RATE_LIMIT_BACKOFF,
    AGENT6_TEST_TIMEOUT,
    ANTHROPIC_API_KEY,
    MODEL_PRICING,
    RESEARCH_MODEL,
    WEB_SEARCH_PRICE_PER_SEARCH,
)
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.provider_registry import _normalize
from puzzleeval.schemas import (
    Agent5Input,
    Agent5Result,
    CandidateTestRun,
    CriterionScore,
    CriterionScoreOutput,
    EvaluationBatchResult,
    FailedCandidateRun,
    FailedHarness,
    ScreenedCandidate,
    TestCase,
    TestCaseResult,
    TestHarness,
)


# ============================================================================
# [CORE] System Prompt — Per-Candidate Builder Agent
# ============================================================================
# This prompt defines the autonomous builder agent that reads API docs,
# writes harness code, tests it, and fixes errors iteratively.
#
# KEY DESIGN: The prompt gives Claude explicit tools, a step-by-step process,
# the exact output interface, and a completion signal. Claude autonomously
# decides when to search for more docs, when to write code, when to test,
# and when it's done.
# ============================================================================

BUILDER_SYSTEM_PROMPT = """You have access to an `advisor` tool backed by a stronger reviewer model. It takes NO parameters -- when you call advisor(), your entire conversation history is automatically forwarded.

Call advisor BEFORE substantive work -- after initial research (fetching docs, inspecting SDK), call advisor before writing harness.py. Also call advisor when stuck (errors recurring, approach not converging) and when you believe the task is complete (before signaling HARNESS_COMPLETE).

The advisor should respond in under 100 words and use enumerated steps, not explanations.

Give the advice serious weight. If you follow a step and it fails empirically, adapt. If you have evidence that contradicts the advice, surface the conflict in one more advisor call.

---

You are an expert API integration engineer building a Python test harness for an AI service. You work in structured phases -- research first, plan, build, verify, then deliver.

## Your Tools

1. **web_fetch** — Read a web page (API documentation, quickstart guides, SDK refs)
2. **web_search** — Search the web for API docs, examples, SDK installation
3. **write_file** — Write a NEW file. Use ONLY for creating files that don't exist yet (harness.py first time, requirements.txt, smoke_test.py)
4. **patch_file** — **YOUR PRIMARY TOOL FOR FIXING CODE.** Replace a specific string in an existing file. When you need to fix a bug, change an endpoint URL, update an auth header, or modify any part of existing code, ALWAYS use patch_file instead of rewriting the entire file with write_file. This is critical for efficiency.
5. **run_code** — Run a shell command in the sandbox (python smoke_test.py, pip install -r requirements.txt, etc.)
6. **read_file** — Read a file you've written or check saved docs (api_spec.txt, fetched_docs_*.txt)
7. **ask_research** — Ask a research sub-agent to find specific information. Use during Phase 2+ debugging when you hit an error and need to verify an assumption. NOT for Phase 1 initial research — web_fetch and web_search are faster.

<tool_selection>
**Phase 1 research:** Use web_fetch and web_search directly. These are server-side
  tools that execute within your API call — faster than spawning sub-agents.
  Fetch the docs URL, search for the API reference, read the OpenAPI spec.
  You can do all of this in a single turn. Then write api_spec.txt.

**Phase 2+ debugging:** Use ask_research when you hit an error and need to verify
  an assumption that api_spec.txt doesn't answer.

**Code fixes:** Use patch_file to change only the broken part.
  Use write_file only when creating files that don't exist yet.
</tool_selection>

<use_parallel_tool_calls>
ALWAYS batch independent tool calls in one response. Examples:
- Write api_spec.txt + harness.py + requirements.txt in ONE turn
- Read two files at once instead of two turns
- pip install + run smoke test in one turn (sequential but one response)
Every separate turn costs tokens. Minimize turns by maximizing tools per turn.
</use_parallel_tool_calls>

<do_not_narrate>
Go straight to action. Do NOT narrate steps. Never output "Now I'll read the file"
or "Let me search for..." — just call the tool. If you can say it in one sentence,
don't use three. Every word of explanation costs tokens and a turn.
</do_not_narrate>

<do_not_re_read>
If you read a file earlier in this conversation and have NOT modified it since,
refer to what you already know. Do NOT re-read the same file. This wastes turns.
Only re-read after YOU have patched or rewritten the file.
</do_not_re_read>

<investigate_comprehensively>
When you need to explore something (SDK methods, file structure, error details),
write ONE comprehensive script that gets ALL the information at once. Do NOT write
five separate scripts that each discover one fact — that wastes 10 turns instead of 2.
Example — BAD: check_sdk1.py (list methods), check_sdk2.py (get signatures), check_sdk3.py (get source)
Example — GOOD: one check_sdk.py that prints methods + signatures + key source in a single run.
</investigate_comprehensively>

<think_before_acting>
Before writing code, ask yourself: What am I unsure about? What could go wrong?
If the API has async polling, have I read the polling docs? If it uses an SDK,
do I know the exact method names? Verify unknowns BEFORE writing, not after.
One read-then-write turn is faster than write-then-debug-then-rewrite (3 turns).
</think_before_acting>

<commit_and_course_correct>
Pick one approach and see it through. Course-correct only if it fails with
new information. Don't revisit decisions or rewrite working code.
</commit_and_course_correct>

## Environment
You are running in an ISOLATED Python virtual environment. `python` and `pip` point to this venv.
After writing requirements.txt, ALWAYS run `pip install -r requirements.txt` to install dependencies. Each candidate has its own venv — no package conflicts.
OS: __OS_TYPE__. Use `python -c "import os; print(os.listdir('.'))"` to list files (works on any OS). Use `os.path` in Python code, not hardcoded path separators.

**IMPORTANT:** Use only ASCII characters in Python code and comments. Do NOT use unicode
dashes (—), arrows (→), or special characters. Use -- for dashes, -> for arrows. This
prevents encoding errors on Windows.

## The Harness Interface (EXACT specification)

harness.py must contain a `run(input_data: dict) -> dict` function.

**Your harness is a THIN API CLIENT.** Its ONLY job is:
1. Read credentials from environment variables
2. Open the test file
3. Send it to the API
4. Return the raw API response and latency

Do NOT parse, extract, format, or transform the API response. Return it raw.
A separate evaluation agent will judge the raw output against ground truth.

```python
def run(input_data: dict) -> dict:
    \"\"\"
    Args:
        input_data: dict with keys:
            - "text": str -- description of what to process (context only)
            - "input_type": str -- "document_content", "image_description", etc.
            - "input_context": dict | None -- optional metadata
            - "test_file_path": str | None -- path to file to send to API

    Returns:
        dict with EXACTLY these keys:
            - "output": str -- the raw API response as a string (json.dumps of response body)
            - "latency_ms": float -- round-trip time in milliseconds
            - "tokens_used": dict | None -- {"input": int, "output": int} if API reports, else None
            - "cost_usd": float | None -- estimated cost if known, else None
            - "raw_response": dict -- the full API response object (JSON-serializable)
            - "success": bool -- True if API returned HTTP 2xx
            - "error": str | None -- error message if failed, None if success
    \"\"\"
```

## Rules
- Read the API key from environment variable (convention: {PROVIDER_NAME}_API_KEY)
- NEVER hardcode API keys in code
- Handle ALL errors gracefully -- run() must NEVER raise exceptions
- When API returns an error, include response.text in the error message (not just status code)
- Use `requests` or the provider's official Python SDK
- Measure latency with time.time() around the actual API call
- For "output": just json.dumps(response_body) -- do NOT extract or reformat fields
- Keep it simple -- no classes, no frameworks, just a module with run()

======================================================================
## PHASE 1: RESEARCH — Understand the API before writing any code
======================================================================

The goal of research: give the builder everything it needs to write a correct API
call WITHOUT guessing. Specifically: the exact endpoint URL, exact auth header format,
exact request format (multipart vs JSON vs base64, field names), and a working Python
code example. If you find all four, the builder writes correct code in 1-2 turns.
If any is missing, the builder guesses wrong and spends 10+ turns debugging.

Do NOT skip this phase. Do NOT code from memory.

### How to Research (search → navigate → fetch → synthesize)

Use web_search first to understand the docs LANDSCAPE — what pages exist, what they
cover, where to find auth details, endpoint specs, and code examples. Search results
show you the structure of the docs site. Then use web_fetch to read the specific pages
that have the technical details you need. This all happens within your API call — fast.

Typical flow in a single turn:
1. web_search("{service} API documentation") → see all docs pages and their descriptions
2. web_search("{service} openapi.json OR swagger") → find the spec if it exists
3. web_fetch(most promising docs URL) → read the technical details
4. Write api_spec.txt with everything you learned, including DOC_MAP from search results

### What Phase 1 must PRODUCE

Write `api_spec.txt` containing:

```
API_SPEC_START
SERVICE: [name]
BASE_URL: [exact URL]
ENDPOINTS: [ALL endpoints with METHOD, path, Content-Type, Python requests param]
AUTH_HEADER: [exact format -- quote from the docs, do NOT guess]
REQUEST_FORMAT: [per endpoint -- specify json=, data=, files= parameter]
RESPONSE_FORMAT: [JSON structure]
SDK_PACKAGE: [pip package or "none -- use requests"]
ACCEPTED_INPUT_FORMATS: [file types, URL support, plain text support]

PYTHON_EXAMPLES:
  [paste code snippets from docs -- file upload, URL submission, SDK usage]

DOC_REFERENCES:
  [URLs for API reference, auth docs, SDK docs, OpenAPI spec if found]

DOC_MAP:
  [List doc pages you encountered during research, even ones you didn't read fully.
   This helps ask_research find specific info during debugging without broad searching.
   Format: URL -- one-line description]

INPUT_COMPATIBILITY:
  file_upload: [YES -- endpoint + method | NO -- reason]
  url_submission: [YES -- endpoint + method | NO -- reason]
  plain_text: [YES -- endpoint + method | NO -- "requires binary file/URL"]
  base64: [YES -- endpoint + field | NO -- reason]

ROUTING_TABLE:
  test_file_path is set -> [endpoint + code pattern]
  text starts with http -> [endpoint + code pattern]
  text is plain content, no file -> [endpoint OR "INCOMPATIBLE: reason"]
  input_type is structured_data -> [endpoint OR "INCOMPATIBLE: reason"]
API_SPEC_END
```

### When Phase 1 is DONE (move to Phase 2)

You have enough when you can fill in: BASE_URL, at least one ENDPOINT with its
request format, AUTH_HEADER, and INPUT_COMPATIBILITY. Call advisor to confirm,
state your PLAN, then move to Phase 2. Missing details can be filled during debugging.

**DO NOT write verification scripts to double-check your research.** Your research may
have errors — that's OK. Build the harness and run a live test. A real API error (404,
401, 400) tells you exactly what's wrong in 1 turn. Parsing specs to verify takes 10+
turns and may still be wrong. Live validation IS verification.

### When Phase 1 is NOT done (keep researching)

You're missing endpoint URLs, auth header format, or request format — the minimum
needed to write a working API call. Fetch more docs pages or search for the OpenAPI spec.

### When to GIVE UP

If you cannot find real API documentation with actual endpoint URLs despite your
best efforts, signal HARNESS_FAILED with reason "docs_unusable".

DO NOT write harness code until you've written api_spec.txt and stated a PLAN.

======================================================================
## PHASE 2: BUILD — Write code based on your research
======================================================================

**USE DOCS AS SOURCE OF TRUTH.** Before writing harness.py, read_file("api_spec.txt").
Use the ROUTING_TABLE to identify which endpoints your test cases need (based on the
input types from the initial message). Then:
- If PYTHON_EXAMPLES has code for those specific endpoints → copy and adapt the patterns
- If no Python examples exist (only curl, JS, or nothing) → write from the endpoint
  spec (URL, method, headers, body format) in the ENDPOINTS section
- If api_spec.txt is MISSING info for endpoints you need (no URL, no request format,
  no auth details) → use ask_research or web_search to fill the gap BEFORE writing code.
  Do NOT guess and debug later — research first, code second.
- NEVER write API calls from training data memory — always from api_spec.txt or fresh research

**PRESERVE API ERROR RESPONSES.** When the API returns an error, ALWAYS include the
response body in the error message — not just the status code. API providers return
specific error reasons (e.g., "Invalid file format. Accepted: PDF, JPEG, PNG") that
tell you exactly what's wrong. Use this pattern in ALL error handling:
```python
if resp.status_code != 200:
    return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:500]}"}
```
NEVER use `response.raise_for_status()` — it discards the response body and gives you
only "400 Bad Request" with no detail. Always read `response.text` for the real reason.

1. **READ** api_spec.txt — find PYTHON_EXAMPLES, ENDPOINTS, AUTH_HEADER
2. **WRITE** harness.py — COPY patterns from PYTHON_EXAMPLES, adapt to run() interface
3. **WRITE** requirements.txt with pip dependencies
4. **RUN** `pip install -r requirements.txt`

<verify_against_docs>
BEFORE running any tests, VERIFY your code matches the docs:
1. read_file("harness.py") — look at what you actually wrote
2. read_file("api_spec.txt") — check AUTH_HEADER, ENDPOINTS, PYTHON_EXAMPLES
3. Compare line by line: Does your auth header EXACTLY match? Does your endpoint URL
   match? Does your request format (json= vs data= vs files=) match?
4. If anything doesn't match, patch_file to fix BEFORE running the smoke test.
This prevents wasting turns debugging errors that come from coding-from-memory.
</verify_against_docs>

4. **WRITE** smoke_test.py — structural validation (see template below)
5. **RUN** `python smoke_test.py`
6. If it fails → read the error → reason about root cause → fix → re-run

## Smoke Test Template
```python
import importlib
import inspect
import unittest.mock

harness = importlib.import_module("harness")
assert hasattr(harness, "run") and callable(harness.run)
sig = inspect.signature(harness.run)
assert "input_data" in list(sig.parameters.keys())

with unittest.mock.patch("requests.Session.send", side_effect=ConnectionError("mocked")):
    with unittest.mock.patch("requests.post", side_effect=ConnectionError("mocked")):
        with unittest.mock.patch("requests.get", side_effect=ConnectionError("mocked")):
            result = harness.run({"text": "test", "input_type": "text", "input_context": None, "test_file_path": None})

assert isinstance(result, dict)
required = {"output", "latency_ms", "tokens_used", "cost_usd", "raw_response", "success", "error"}
assert not (required - set(result.keys())), f"Missing keys: {required - set(result.keys())}"
assert isinstance(result["success"], bool)
assert isinstance(result["latency_ms"], (int, float))
assert isinstance(result["output"], str)
assert isinstance(result["raw_response"], dict)
print("SMOKE TEST PASSED")
```
Adapt mock patches if using httpx or a provider SDK instead of requests.

## ERROR RECOVERY — Reason About Root Cause

<reason_about_errors>
When a test fails, do NOT follow a recipe. THINK:

1. READ the full error message. What is it actually telling you?
2. What ASSUMPTION did you make that might be wrong?
   - Wrong endpoint URL? → read_file("api_spec.txt") to verify
   - Wrong auth format? → read_file("api_spec.txt") to check exact header
   - Wrong request format? → the API may expect data= not json=, or files= not data=
   - Wrong platform? → if the service has multiple platforms (legacy vs new),
     is your API key for the platform you're targeting? Check the registry notes.
3. VERIFY your assumption against the docs BEFORE attempting a fix.
4. Fix with patch_file — change ONLY the broken part.
5. If the same error repeats after a fix, your APPROACH is wrong, not the details.
   Use ask_research to re-verify your fundamental assumption.
6. If truly unfixable (expired credentials, deactivated account) → signal HARNESS_FAILED.
</reason_about_errors>

<be_resourceful>
When external resources fail (sample URLs return 404, CDN links are expired):
- Don't keep retrying variations of the same URL.
- Create a LOCAL test file instead: write a text file or create a minimal valid PDF with Python.
- A local file that works is better than a remote URL that might go stale.
- If the API rejects even a valid local file, THAT is a real API or auth issue.
</be_resourceful>

ask_research is cheap and fast — it spawns a separate web search. Use it to VERIFY assumptions,
not just as a last resort before giving up.

======================================================================
## PHASE 3: VERIFY — Confirm the API call works
======================================================================

After smoke test passes, verify the harness works with REAL API calls for EACH file type.

API credentials ARE available in your environment. Live tests MUST succeed before
you signal HARNESS_COMPLETE. A harness that passes smoke test but fails live API
calls is NOT complete.

1. **LIVE TEST PER FILE TYPE**: Look at the test case input forms. If test files
   include both PDF and PNG, test BOTH — PDF success does not guarantee PNG success.
   Run harness.run() with one test file of EACH type:
   ```
   python -c "import json, harness; r = harness.run({'text': 'test', 'input_type': 'document_content', 'input_context': None, 'test_file_path': '<path_to_test_file>'}); print(json.dumps({'success': r['success'], 'latency_ms': r['latency_ms'], 'error': r.get('error'), 'output_len': len(r.get('output',''))}, default=str))"
   ```

2. **SUCCESS = API returned real data.** success=True AND output_len > 100 for EACH
   file type tested. Only then signal HARNESS_COMPLETE.

3. **FAILURE = fix the harness:**
   - HTTP 400 → wrong request format (check: multipart vs JSON, base64 vs binary, field names)
   - HTTP 401/403 → wrong auth format (check: Bearer vs Token vs API key header name)
   - HTTP 404 → wrong endpoint URL (check: path, version, base URL)
   - "Missing API key" → check your env var name matches what's available

Do NOT signal HARNESS_COMPLETE if live tests return errors. Fix the harness first.

## WHEN YOU HIT A BUG

1. Read the error message — API providers include specific reasons
2. read_file("api_spec.txt") — verify endpoint, auth, request format
3. ask_research if api_spec doesn't answer your question

======================================================================
## PHASE 4: COMPLETION CHECKLIST
======================================================================

Before saying HARNESS_COMPLETE, ALL of these must be true:
[Y] smoke_test.py passes (structural validation)
[Y] LIVE API call succeeded for EACH file type (success=True, output_len > 100)
[Y] API returned real data (not just HTTP 200 with empty body)

If live tests haven't passed, you are NOT done. Go back to Phase 3 and fix.
[Y] Incompatible input forms return success=False with INCOMPATIBLE error
[Y] requirements.txt lists ALL dependencies

## SIGNALS
- **HARNESS_COMPLETE** -- all compatible input forms validated with real test data
- **HARNESS_FAILED** -- cannot build a working harness (explain why)
"""


# ============================================================================
# Tool Definitions — Custom tools dispatched locally
# ============================================================================
# Server tools (web_fetch, web_search) are executed by the Anthropic API.
# Custom tools (write_file, run_code, read_file) are dispatched locally by
# _dispatch_tool(). Claude invokes them via tool_use blocks.
# ============================================================================

WEB_FETCH_TOOL = {
    "type": "web_fetch_20250910",
    "name": "web_fetch",
    "max_uses": 5,     # Docs page + specific endpoint + auth/SDK + homepage + follow link
    "max_content_tokens": 15000,  # Limit content per page to prevent context explosion.
    # 15K tokens (~60K chars) is enough for API endpoint details, auth format,
    # and code examples. Full pages can be 50K+ tokens which stays in context
    # for ALL subsequent turns at $0.60/turn. Claude Code uses 100K char limit +
    # disk persistence; we use server-side truncation at the source.
}

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 4,     # Primary + refinement + SDK examples + error troubleshooting
}

WRITE_FILE_TOOL = {
    "name": "write_file",
    "description": (
        "Create a new file in the sandbox directory with the given content. "
        "Use this when creating files that don't exist yet: harness.py, "
        "requirements.txt, smoke_test.py, integration_test.py. "
        "Do NOT use this to modify existing files — use patch_file instead, "
        "which changes only the specific part that needs fixing. "
        "Rewriting an entire file wastes tokens and risks introducing new bugs."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "filename": {
                "type": "string",
                "description": (
                    "Filename to write (no directory path — files are always "
                    "written to the sandbox directory). Examples: 'harness.py', "
                    "'requirements.txt', 'smoke_test.py'"
                ),
            },
            "content": {
                "type": "string",
                "description": "The complete file content to write",
            },
        },
        "required": ["filename", "content"],
    },
}

RUN_CODE_TOOL = {
    "name": "run_code",
    "description": (
        "Run a shell command in the sandbox directory and return its output. "
        "Use for running tests (python smoke_test.py), installing packages "
        "(pip install -r requirements.txt), or checking installed packages (pip list). "
        "Commands time out after 30 seconds. Output beyond 5000 characters is "
        "saved to a file — use read_file to see the full output if truncated. "
        "On Windows, prefer writing .py files and running them over inline "
        "python -c commands, which may produce no visible output."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Shell command to run in the sandbox directory. "
                    "Example: 'python smoke_test.py'"
                ),
            },
        },
        "required": ["command"],
    },
}

READ_FILE_TOOL = {
    "name": "read_file",
    "description": (
        "Read a file from the sandbox directory and return its contents. "
        "Use this to review code you've written (harness.py), check saved "
        "API documentation (api_spec.txt, fetched_docs_*.txt), or read "
        "output files from previous commands (output_turn*.txt). "
        "This is free in terms of API cost — prefer reading saved docs "
        "over re-fetching from the web. Files over 10000 characters are truncated."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "filename": {
                "type": "string",
                "description": (
                    "Filename to read (no directory path). "
                    "Example: 'harness.py'"
                ),
            },
        },
        "required": ["filename"],
    },
}

PATCH_FILE_TOOL = {
    "name": "patch_file",
    "description": (
        "Fix a bug or update a section of an existing file by replacing a specific "
        "string. Provide the exact old text and its replacement. This is the primary "
        "tool for all code fixes — changing an endpoint URL, updating an auth header, "
        "fixing a response parser, or adding an import. Much more efficient than "
        "rewriting the entire file. The old_string must match exactly including "
        "whitespace and newlines. If the match fails, the tool returns the file's "
        "first 500 characters to help you find the right string."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "filename": {
                "type": "string",
                "description": "File to patch (no directory path). Example: 'harness.py'",
            },
            "old_string": {
                "type": "string",
                "description": "Exact string to find in the file. Must match exactly (including whitespace).",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement string. Can be empty to delete the old_string.",
            },
        },
        "required": ["filename", "old_string", "new_string"],
    },
}

ASK_RESEARCH_TOOL = {
    "name": "ask_research",
    "description": (
        "Ask a research sub-agent to verify or find specific API information. "
        "Spawns a fresh web search in a separate context — cheap and fast (~$0.05). "
        "Use when: (1) same error occurred twice and you need to verify your assumptions "
        "about the endpoint URL, auth format, or API version, (2) you suspect the company "
        "migrated to a new API platform, (3) read_file(api_spec.txt) doesn't answer your "
        "specific question. Be specific — ask ONE thing at a time."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "A specific question about the API you're integrating with. "
                    "Example: 'What is the correct endpoint URL for Google Document AI?' "
                    "or 'Does the API require multipart upload or JSON body?'"
                ),
            },
        },
        "required": ["question"],
    },
}

# Advisor tool — Opus 4.6 provides strategic guidance to Sonnet executor.
# Sonnet decides when to call it. Opus sees the full conversation and gives a plan.
# This is a server-side tool — no local dispatch needed.
ADVISOR_TOOL = {
    "type": "advisor_20260301",
    "name": "advisor",
    "model": "claude-opus-4-6",
    "caching": {"type": "ephemeral", "ttl": "5m"},
}

# All tools passed to the API — server tools + custom tools + advisor
ALL_TOOLS = [WEB_FETCH_TOOL, WEB_SEARCH_TOOL, ADVISOR_TOOL, WRITE_FILE_TOOL, PATCH_FILE_TOOL, RUN_CODE_TOOL, READ_FILE_TOOL, ASK_RESEARCH_TOOL]

# Custom tool names — used to identify which tool_use blocks need local dispatch
CUSTOM_TOOL_NAMES = {"write_file", "patch_file", "run_code", "read_file", "ask_research"}

# Allowed file extensions for write_file (security)
ALLOWED_EXTENSIONS = {".py", ".txt", ".json", ".cfg", ".toml", ".sh", ".yaml", ".yml"}

# pause_turn: allow 1 continuation per turn (server tools may take long on first fetch)
MAX_CONTINUATIONS = 1


# ============================================================================
# Helpers
# ============================================================================

# ============================================================================
# Context Management Constants (Claude Code auto-compact pattern)
# ============================================================================
# Claude Code reserves 13K tokens as a safety margin and auto-compacts
# when approaching the context window limit. We do the same but simpler:
# estimate token count from message text length, compact when approaching
# the model's context limit.
#
# Sonnet 4.6 has a 200K token context window. We reserve 30K for output +
# system prompt + tools. When messages exceed ~150K estimated tokens
# (chars/4 approximation), we trim older messages.
# ============================================================================

OUTPUT_PERSIST_THRESHOLD = 5000  # Save tool outputs >5K chars to disk
MAX_PERSISTED_OUTPUT_CHARS = 30000  # Cap persisted output files


def _persist_large_output(output: str, sandbox_dir: Path, turn: int) -> str:
    """
    Save large tool output to a file and return a smart-truncated version.
    Inspired by Claude Code's tool result persistence + EndTruncatingAccumulator.

    Strategy (Claude Code pattern):
    - Errors are ALWAYS at the tail (tracebacks, pip failures, test output)
    - Context/noise is in the middle (successful install lines, verbose logs)
    - Keep head (first 800 chars: command context) + tail (last 3000 chars: errors)
    - Drop the middle (noise)
    - Save full output to disk for read_file() access
    """
    if len(output) <= OUTPUT_PERSIST_THRESHOLD:
        return output

    # Save full output to file
    filename = f"output_turn{turn}.txt"
    filepath = sandbox_dir / filename
    try:
        filepath.write_text(output[:MAX_PERSISTED_OUTPUT_CHARS], encoding="utf-8")
    except OSError:
        pass

    # Smart truncation: head + tail (errors are at the end)
    head_size = 800
    tail_size = 3000
    preview_head = output[:head_size]
    preview_tail = output[-tail_size:] if len(output) > head_size + tail_size else output[head_size:]
    separator = (
        f"\n\n... ({len(output)} chars total — middle truncated, errors preserved below. "
        f"Full output saved to {filename}) ...\n\n"
    )

    return preview_head + separator + preview_tail


def _candidate_slug(name: str) -> str:
    """
    Convert a candidate name to a filesystem-safe directory name.

    "Google Document AI" → "google_document_ai"
    "AWS Textract (OCR)" → "aws_textract_ocr"
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:40]


def _calculate_call_cost(response: anthropic.types.Message, model: str) -> float:
    """
    Calculate the TOTAL cost of a single API call using the iterations array.

    The iterations array gives per-iteration breakdown:
    - type='message': executor iteration (billed at executor model rates)
    - type='advisor_message': advisor iteration (billed at advisor model rates)
    - type='compaction': compaction (billed at executor model rates)

    Each iteration has: input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens.

    Falls back to top-level usage if iterations not available.
    """
    usage = response.usage
    iterations = getattr(usage, "iterations", None) or []

    if iterations:
        total_cost = 0.0
        for iteration in iterations:
            iter_type = getattr(iteration, "type", "message")
            iter_in = getattr(iteration, "input_tokens", 0) or 0
            iter_out = getattr(iteration, "output_tokens", 0) or 0
            iter_cache_create = getattr(iteration, "cache_creation_input_tokens", 0) or 0
            iter_cache_read = getattr(iteration, "cache_read_input_tokens", 0) or 0

            # Determine rates based on iteration type
            if iter_type == "advisor_message":
                iter_model = getattr(iteration, "model", "claude-opus-4-6")
                in_price, out_price = MODEL_PRICING.get(
                    iter_model, (5.0 / 1_000_000, 25.0 / 1_000_000)
                )
            else:
                in_price, out_price = MODEL_PRICING.get(
                    model, (3.0 / 1_000_000, 15.0 / 1_000_000)
                )

            # Cache pricing: create = 1.25x input, read = 0.1x input
            total_cost += iter_in * in_price
            total_cost += iter_out * out_price
            total_cost += iter_cache_create * in_price * 1.25
            total_cost += iter_cache_read * in_price * 0.1

        # Add web search costs
        server_tool_use = getattr(usage, "server_tool_use", None)
        if server_tool_use:
            searches = getattr(server_tool_use, "web_search_requests", 0) or 0
            total_cost += searches * WEB_SEARCH_PRICE_PER_SEARCH

        return round(total_cost, 6)

    # Fallback: no iterations array (older API or non-beta call)
    input_price, output_price = MODEL_PRICING.get(
        model, (3.0 / 1_000_000, 15.0 / 1_000_000)
    )
    cost = (usage.input_tokens * input_price) + (usage.output_tokens * output_price)
    return round(cost, 6)


# ============================================================================
# [CORE] Tool Dispatch — execute custom tools locally
# ============================================================================
# Security constraints:
#   - write_file: strips path traversal, restricts file extensions
#   - run_code: 30s timeout, output truncated, runs in sandbox dir only
#   - read_file: only reads from sandbox dir, output truncated
# ============================================================================

def _dispatch_tool(
    tool_name: str,
    tool_input: dict,
    sandbox_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[str, int]:
    """
    Execute a custom tool and return (result_string, exit_code).

    exit_code: 0 = success, non-zero = failure. For non-run_code tools,
    exit_code is 0 (success) unless the tool returns an error string.
    extra_env passes API credentials to run_code subprocesses.
    """
    if tool_name == "write_file":
        result = _tool_write_file(tool_input, sandbox_dir)
        return result, 1 if result.startswith("Error") else 0
    elif tool_name == "patch_file":
        result = _tool_patch_file(tool_input, sandbox_dir)
        return result, 1 if result.startswith("Error") else 0
    elif tool_name == "run_code":
        return _tool_run_code(tool_input, sandbox_dir, extra_env=extra_env)
    elif tool_name == "read_file":
        result = _tool_read_file(tool_input, sandbox_dir)
        return result, 1 if result.startswith("Error") else 0
    elif tool_name == "ask_research":
        return "Error: ask_research must be dispatched via the main loop", -2
    else:
        return f"Error: unknown tool '{tool_name}'", -2


def _tool_write_file(tool_input: dict, sandbox_dir: Path) -> str:
    """Write a file to the sandbox directory with security checks."""
    raw_filename = tool_input.get("filename", "")
    content = tool_input.get("content", "")

    # Security: strip any path components (prevent ../../../etc/passwd)
    filename = Path(raw_filename).name
    if not filename:
        return "Error: empty filename"

    # Security: restrict file extensions
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return (
            f"Error: file extension '{suffix}' not allowed. "
            f"Use one of: {sorted(ALLOWED_EXTENSIONS)}"
        )

    target = sandbox_dir / filename
    try:
        target.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {filename}"
    except OSError as e:
        return f"Error writing {filename}: {e}"


def _tool_patch_file(tool_input: dict, sandbox_dir: Path) -> str:
    """Replace a specific string in an existing file (string-replace editing)."""
    raw_filename = tool_input.get("filename", "")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")

    filename = Path(raw_filename).name
    if not filename:
        return "Error: empty filename"

    target = sandbox_dir / filename
    if not target.exists():
        return f"Error: '{filename}' does not exist in sandbox. Use write_file to create it first."

    try:
        content = target.read_text(encoding="utf-8")
    except OSError as e:
        return f"Error reading {filename}: {e}"

    if old_string not in content:
        # Try normalizing whitespace/quotes as a fallback — files may have
        # different quote styles or trailing spaces than what the model sends.
        normalized_old = old_string.replace("\r\n", "\n").replace("\r", "\n")
        normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")

        if normalized_old in normalized_content:
            # Match found after newline normalization — apply the patch
            new_content = normalized_content.replace(normalized_old, new_string, 1)
            try:
                target.write_text(new_content, encoding="utf-8")
                return f"Patched {filename} (after newline normalization): replaced {len(old_string)} chars with {len(new_string)} chars"
            except OSError as e:
                return f"Error writing {filename}: {e}"

        # Truly not found — STOP the model from blind retrying.
        # Claude Code uses behavior:'ask' to force the model to pause and verify.
        # We achieve this by giving explicit instructions and file content.
        preview = content[:800] if len(content) > 800 else content
        return (
            f"STOP: old_string not found in {filename}. Do NOT retry with a guess.\n\n"
            f"REQUIRED STEPS:\n"
            f"1. Use `read_file('{filename}')` to see the ACTUAL current content\n"
            f"2. Find the exact text you want to change (copy it precisely)\n"
            f"3. Call `patch_file` again with the correct old_string\n"
            f"4. If the file has encoding issues, use `write_file('{filename}', ...)` "
            f"to rewrite the entire file with your fix included\n\n"
            f"File preview ({len(content)} chars total):\n{preview}"
        )

    count = content.count(old_string)
    if count > 1:
        return (
            f"Error: old_string found {count} times in {filename}. "
            f"Provide a longer, unique string that matches only the section you want to change."
        )

    new_content = content.replace(old_string, new_string, 1)
    try:
        target.write_text(new_content, encoding="utf-8")
        return f"Patched {filename}: replaced {len(old_string)} chars with {len(new_string)} chars"
    except OSError as e:
        return f"Error writing {filename}: {e}"


def _build_sandbox_env(sandbox_dir: Path, extra_env: dict[str, str] | None = None) -> dict:
    """
    Build environment variables for subprocess execution in the sandbox.

    If a venv exists at sandbox_dir/.venv, prepend its bin/Scripts dir
    to PATH so `python` and `pip` resolve to the venv's copies.
    This is the sandboxing seam: in production, replace this with container
    exec env setup — the rest of the code doesn't change.
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}

    venv_dir = sandbox_dir / ".venv"
    if venv_dir.exists():
        if sys.platform == "win32":
            venv_bin = str(venv_dir / "Scripts")
        else:
            venv_bin = str(venv_dir / "bin")
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(venv_dir)

    if extra_env:
        env.update(extra_env)

    return env


def _tool_run_code(
    tool_input: dict,
    sandbox_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Run a shell command in the sandbox directory with timeout and venv isolation.
    Returns (output_text, exit_code). exit_code is 0 on success, non-zero on failure,
    -1 for timeout, -2 for other exceptions."""
    command = tool_input.get("command", "")
    if not command:
        return "Error: empty command", -2

    env = _build_sandbox_env(sandbox_dir, extra_env)

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(sandbox_dir),
            capture_output=True,
            text=True,
            timeout=AGENT5_CODE_TIMEOUT,
            env=env,
        )
        # Structured output: separate stdout and stderr clearly
        # (Claude Code's BashTool separates these for clarity)
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            stderr_text = result.stderr.strip()
            if stderr_text:
                if output:
                    output += "\n"
                output += f"[stderr] {stderr_text}"

        if not output.strip():
            # Detect Windows output suppression: Python commands that should
            # produce output but don't. This prevents diagnostic spirals where
            # the agent retries the same command 10+ times.
            command_lower = command.lower()
            if result.returncode == 0 and ("python" in command_lower and ("print" in command_lower or "import" in command_lower)):
                output = (
                    f"(command completed with exit code 0 but produced no visible output. "
                    f"This is a known Windows issue with Python subprocess output capture. "
                    f"WORKAROUND: Instead of `python -c \"print(...)\"`, write a small .py "
                    f"file with write_file and run it with run_code. Or use read_file to "
                    f"read files directly.)"
                )
            else:
                output = f"(command completed with exit code {result.returncode})"

        # Add exit code context (Claude Code's commandSemantics pattern)
        # Non-zero exit codes aren't always errors — interpret them semantically
        if result.returncode != 0:
            exit_note = f"\n[Exit code: {result.returncode}"
            if result.returncode == 1:
                exit_note += " — may indicate: test failure, no matches found (grep), or general error"
            elif result.returncode == 2:
                exit_note += " — may indicate: misuse of command or invalid arguments"
            elif result.returncode == 126:
                exit_note += " — permission denied (cannot execute)"
            elif result.returncode == 127:
                exit_note += " — command not found"
            elif result.returncode == 137:
                exit_note += " — process killed (OOM or signal 9)"
            exit_note += "]"
            output += exit_note

        # Truncate to prevent token explosion
        if len(output) > 5000:
            output = output[:5000] + "\n... (output truncated at 5000 chars)"

        return output, result.returncode

    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {AGENT5_CODE_TIMEOUT} seconds", -1
    except OSError as e:
        return f"Error running command: {e}", -2


def _tool_read_file(tool_input: dict, sandbox_dir: Path) -> str:
    """Read a file from the sandbox directory."""
    raw_filename = tool_input.get("filename", "")
    filename = Path(raw_filename).name
    if not filename:
        return "Error: empty filename"

    target = sandbox_dir / filename
    if not target.exists():
        return f"Error: '{filename}' does not exist in sandbox"

    try:
        content = target.read_text(encoding="utf-8")
        # Truncate to prevent token explosion
        if len(content) > 10000:
            content = content[:10000] + "\n... (content truncated at 10000 chars)"
        return content
    except OSError as e:
        return f"Error reading {filename}: {e}"


# ============================================================================
# [CORE] Extract text from a mixed-content response
# ============================================================================

def _extract_text_from_response(response: anthropic.types.Message) -> str:
    """Pull all text content from a response with mixed content blocks."""
    text_parts = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
    return "\n\n".join(text_parts)


def _extract_and_save_web_content(
    response: anthropic.types.Message,
    sandbox_dir: Path,
    existing_count: int = 0,
) -> list[str]:
    """
    Extract page text from WebFetchToolResultBlock in the API response.
    Save each fetched page to sandbox_dir/fetched_docs_{n}.txt.
    Returns list of saved filenames.

    The actual page text is at:
      block.content.content.source.data  (PlainTextSource.data)

    This is the same text Claude sees in context. By saving it to a file,
    we can drop it from conversation history and let Claude read_file()
    it on demand — paying for it once instead of every turn.
    """
    saved_files = []
    fetch_count = existing_count

    for block in response.content:
        block_type = getattr(block, "type", "")
        if block_type == "web_fetch_tool_result":
            try:
                # Navigate: WebFetchToolResultBlock → WebFetchBlock → DocumentBlock → PlainTextSource
                fetch_block = block.content  # WebFetchBlock or WebFetchToolResultErrorBlock
                if hasattr(fetch_block, "content") and hasattr(fetch_block.content, "source"):
                    page_text = fetch_block.content.source.data
                    url = getattr(fetch_block, "url", "unknown")
                    filename = f"fetched_docs_{fetch_count}.txt"
                    filepath = sandbox_dir / filename
                    header = f"# Fetched from: {url}\n# Saved for reference during build phase\n\n"
                    # Cap at 50K chars to prevent huge files
                    filepath.write_text(header + page_text[:50000], encoding="utf-8")
                    saved_files.append(filename)
                    fetch_count += 1
            except (AttributeError, OSError):
                pass  # Non-critical — if extraction fails, web content stays in context as usual
        elif block_type == "web_search_tool_result":
            # Also save search result content — contains useful snippets with
            # API endpoints, code examples, and SDK install instructions
            try:
                search_block = block.content  # WebSearchBlock
                if hasattr(search_block, "content") and search_block.content:
                    # Extract text from search result entries
                    snippets = []
                    for entry in search_block.content:
                        if hasattr(entry, "title") and hasattr(entry, "url"):
                            snippets.append(f"## {entry.title}\nURL: {entry.url}")
                        if hasattr(entry, "page_snippet"):
                            snippets.append(entry.page_snippet)
                        elif hasattr(entry, "text"):
                            snippets.append(entry.text)
                    if snippets:
                        filename = f"fetched_docs_{fetch_count}.txt"
                        filepath = sandbox_dir / filename
                        header = "# Search results — saved for reference during build phase\n\n"
                        filepath.write_text(header + "\n\n---\n\n".join(snippets)[:30000], encoding="utf-8")
                        saved_files.append(filename)
                        fetch_count += 1
            except (AttributeError, OSError, TypeError):
                pass  # Non-critical

    return saved_files



TARGETED_RESEARCH_SYSTEM = (
    "You are an API documentation researcher helping a developer debug an integration. "
    "Search the web for the answer and report your findings concisely.\n\n"
    "Rules:\n"
    "1. Prioritize official documentation (docs.*, developers.*, api.*) over blog posts and tutorials\n"
    "2. If you find conflicting information, prefer the most recent source (2025-2026)\n"
    "3. If the company has rebranded or migrated their API to a new domain, report BOTH "
    "the old and new endpoints so the developer can switch\n"
    "4. Include exact URLs, endpoint paths, auth header formats, and code examples\n"
    "5. If you cannot find the answer in official docs, say so clearly — do not guess"
)


def _run_targeted_research(
    client: anthropic.Anthropic,
    question: str,
    candidate_name: str,
    logger,
    trace_id: str,
) -> tuple[str, float]:
    """
    Run a focused research sub-agent to answer a specific API question.

    Called mid-loop when the builder invokes ask_research. Uses fresh context
    (no accumulated build noise) with web_search + web_fetch tools.

    Returns (answer_text, cost_usd).
    """
    candidate_label = _candidate_slug(candidate_name)

    logger.info(f"Targeted research for {candidate_name}: {question[:100]}", extra={
        "operation": f"targeted_research_{candidate_label}",
        "trace_id": trace_id,
    })

    total_cost = 0.0
    messages = [{"role": "user", "content": f"## Research Question\n\n{question}\n\nSearch the web and report your findings with exact details."}]

    # Allow 1 continuation for pause_turn
    for continuation in range(2):
        call_start = time.time()
        try:
            # Sonnet for research — handles web search/fetch cheaply.
            # No advisor here — the main builder loop has advisor for
            # strategic guidance. ask_research is for targeted fact-finding.
            response = client.beta.messages.create(
                model=RESEARCH_MODEL,
                max_tokens=4096,
                betas=["context-management-2025-06-27"],
                system=[{"type": "text", "text": TARGETED_RESEARCH_SYSTEM}],
                messages=messages,
                tools=[
                    {"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
                    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 15000},
                ],
                thinking={"type": "adaptive"},
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError,
                anthropic.APIStatusError, anthropic.BadRequestError) as e:
            return f"Research failed: {e}", total_cost

        log_llm_call(
            logger=logger, response=response, model=RESEARCH_MODEL,
            trace_id=trace_id, start_time=call_start,
            operation=f"targeted_research_{candidate_label}_cont{continuation}",
        )

        call_cost = _calculate_call_cost(response, RESEARCH_MODEL)
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
            logger.info(f"Targeted research complete for {candidate_name}", extra={
                "operation": f"targeted_research_complete_{candidate_label}",
                "trace_id": trace_id,
                "cost": total_cost,
            })
            return text, total_cost

    return "Research exhausted continuations without producing an answer.", total_cost


# ============================================================================
# [CORE] Build the initial user message for the builder agent
# ============================================================================

def _build_initial_message(
    candidate: ScreenedCandidate,
    input_data: Agent5Input,
    credentials: dict[str, str] | None = None,
    staged_test_cases: list[TestCase] | None = None,
) -> str:
    """
    Build the first user message for the builder agent. Includes all seed
    knowledge from Agent 4's screening + context from Agent 1/3 + credential hints.

    The builder does its own research in Phase 1 — no pre-researched spec.
    """
    # Sub-task context from Agent 1
    subtask_lines = []
    for st in input_data.user_understanding.sub_tasks:
        subtask_lines.append(f"- {st.description} (capability: {st.capability})")
    subtasks_text = "\n".join(subtask_lines) if subtask_lines else "(none)"

    # Input/output type context from Agent 3
    output_types = set()
    for tc in input_data.test_cases.test_cases:
        output_types.add(tc.output_type)
    output_types_text = ", ".join(sorted(output_types)) if output_types else "free_text"

    # Test case form summary — grouped by (input_type, has_file, file_formats)
    form_groups: dict[str, int] = {}
    file_formats: set[str] = set()
    for tc in input_data.test_cases.test_cases:
        has_file = "test_file_path=set" if tc.test_file_path else "test_file_path=null"
        key = f"{tc.input_type}, {has_file}"
        form_groups[key] = form_groups.get(key, 0) + 1
        if tc.test_file_path:
            ext = Path(tc.test_file_path).suffix.lower().lstrip(".")
            if ext:
                file_formats.add(ext)
    test_case_forms = "\n".join(
        f"  {count}x {form}" for form, count in sorted(form_groups.items(), key=lambda x: -x[1])
    )
    if file_formats:
        test_case_forms += f"\n  File formats present: {', '.join(sorted(file_formats))}"

    # Use staged test file paths (absolute, in sandbox) if available,
    # otherwise resolve original paths. Staged paths are preferred because
    # they're the same files used by post-loop test execution.
    source_cases = staged_test_cases if staged_test_cases else input_data.test_cases.test_cases
    test_file_paths_list = []
    for tc in source_cases:
        if tc.test_file_path:
            test_file_paths_list.append(f"  - {tc.test_file_path}")
    if test_file_paths_list:
        test_case_forms += "\n\n  Test files for live validation (absolute paths):\n" + "\n".join(test_file_paths_list)

    # Provider name for env var convention
    provider_slug = re.sub(r"[^A-Z0-9]", "_", candidate.provider.upper()).strip("_")

    # Format credential env var names (show names, not values)
    if credentials:
        credential_env_vars = "\n".join(f"- `{var}`" for var in sorted(credentials.keys()))
    else:
        credential_env_vars = f"- `{provider_slug}_API_KEY` (convention — no credentials provided)"

    # Look up registry notes for platform/version hints
    registry_notes = ""
    try:
        from puzzleeval.config import PROVIDER_REGISTRY_PATH
        reg_path = Path(PROVIDER_REGISTRY_PATH)
        if reg_path.exists():
            reg_data = json.loads(reg_path.read_text(encoding="utf-8"))
            candidate_lower = candidate.name.lower()
            for key, entry in reg_data.get("providers", {}).items():
                if key.lower() in candidate_lower or candidate_lower in key.lower():
                    notes = entry.get("notes", "")
                    if notes:
                        registry_notes = f"\n**Credential Notes:** {notes}\n"
                    break
    except Exception:
        pass

    message = f"""## Build a Test Harness for: {candidate.name}

### Service Details (from Agent 4 screening — verified)
- **Provider:** {candidate.provider}
- **Description:** {candidate.description}
- **API Docs URL:** {candidate.verified_api_docs_url}
- **Auth Method:** {candidate.auth_method}
- **Access Method:** {candidate.api_access_method}
- **Data Formats:** {candidate.data_format_notes}
- **Confirmed Capabilities:** {", ".join(candidate.confirmed_capabilities)}
- **Rate Limits:** {candidate.rate_limit_info or "Not specified"}
- **Screening Notes:** {candidate.screening_notes}

### Pricing Info
- **Model:** {candidate.pricing_model}
- **Details:** {candidate.pricing_details or "Not specified"}

### What the User Needs (sub-tasks this service covers)
{subtasks_text}

### Test Case Input Forms Your Harness Must Handle
{test_case_forms}
- Output types expected: {output_types_text}

For each input form above: check INPUT_COMPATIBILITY in your api_spec.txt.
  - API supports it -> harness.run() MUST have a working code path for it.
  - API does NOT support it -> harness.run() returns success=False, error="INCOMPATIBLE: <reason>".

### Environment Variable Convention
Use `{provider_slug}_API_KEY` as the environment variable name for the API key.

### Available Credentials (env var names — read from these in your harness)
{credential_env_vars}
**IMPORTANT:** These env var names indicate which API version to target. For example,
a MODEL_ID variable typically means a newer API version that uses model UUIDs. Make sure
your harness reads ALL of these variables and uses the correct API version that matches them.
{registry_notes}
---

Start by fetching the API docs URL to understand the exact endpoints, authentication flow,
and response format. Write your findings to api_spec.txt, then build the harness."""

    return message


# ============================================================================
# [CORE] Build a single harness — autonomous multi-turn tool-use loop
# ============================================================================
# This is the heart of Agent 5. For ONE candidate, it runs a conversation
# loop where Claude reads API docs, writes code, tests it, fixes errors,
# and repeats until the harness passes structural validation.
#
# The loop handles:
#   - Server tools (web_fetch, web_search) — executed by Anthropic API
#   - Custom tools (write_file, run_code, read_file) — dispatched locally
#   - pause_turn — server tool loop took too long, continue conversation
#   - Budget/turn limits — stop gracefully on exhaustion
#   - Completion detection — "HARNESS_COMPLETE" in final text
# ============================================================================

def _build_single_harness(
    client: anthropic.Anthropic,
    candidate: ScreenedCandidate,
    input_data: Agent5Input,
    sandbox_dir: Path,
    logger,
) -> TestHarness | FailedHarness:
    """
    Build a test harness for one candidate using an autonomous tool-use loop
    with a verification gate.

    The loop has two modes:
    1. BUILD MODE: Claude reads docs, writes code, runs smoke tests, fixes errors
    2. VERIFICATION GATE: When Claude signals HARNESS_COMPLETE, we run checks.
       If checks fail, we feed the issues back to Claude for fixing.
       The loop only exits when verified clean or retries are exhausted.

    Returns TestHarness on success, FailedHarness on failure.
    Never raises — all errors are caught and converted to FailedHarness.
    """
    candidate_label = _candidate_slug(candidate.name)
    trace_id = input_data.trace_id
    provider_slug = re.sub(r"[^A-Z0-9]", "_", candidate.provider.upper()).strip("_")

    logger.info(f"Building harness for {candidate.name}", extra={
        "operation": "harness_build_start",
        "trace_id": trace_id,
        "candidate_name": candidate.name,
        "sandbox_dir": str(sandbox_dir),
    })

    # ★ Stage test files to sandbox BEFORE build starts.
    # This makes files available for both build-phase live validation AND post-loop execution.
    # One staging, one set of absolute paths, used everywhere.
    staged_test_cases = _stage_test_files(
        input_data.test_cases.test_cases, sandbox_dir, logger, trace_id,
    )

    # ★ Create isolated venv for this candidate
    venv_ok = _create_venv(sandbox_dir, logger, trace_id, candidate.name)
    if not venv_ok:
        # Check if the venv python actually exists
        if sys.platform == "win32":
            venv_python = sandbox_dir / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = sandbox_dir / ".venv" / "bin" / "python"
        if not venv_python.exists():
            return FailedHarness(
                candidate_name=candidate.name,
                provider=candidate.provider,
                failure_reason="Python venv creation failed — cannot build harness without isolated environment",
                failure_category="dependency_failure",
                partial_code=None,
                turns_attempted=0,
            )

    # ★ Resolve credentials for the builder loop (used for live validation)
    credentials = _resolve_credentials(input_data, candidate, provider_slug)
    if credentials:
        logger.info(f"Credentials resolved for {candidate.name}", extra={
            "operation": "credentials_resolved",
            "trace_id": trace_id,
            "candidate_name": candidate.name,
        })

    # ★ CORE: Initialize the conversation with seed knowledge + credential hints
    # No separate research sub-agent — the builder does its own research in Phase 1.
    # This keeps research and coding in ONE context, so the actual fetched API docs
    # are in memory when the code is written. No knowledge-handoff loss.
    initial_message = _build_initial_message(
        candidate, input_data, credentials=credentials, staged_test_cases=staged_test_cases,
    )
    messages = [{"role": "user", "content": initial_message}]

    accumulated_cost = 0.0
    total_web_searches = 0
    turn = 0
    api_spec_written = False  # Tracks Phase 1 → Phase 2 transition for model switch
    last_text = ""
    verification_attempts = 0
    verification_passed = False
    conversation_log = []  # Human-readable log of every turn
    saved_doc_files = []   # Filenames of fetched docs saved to sandbox
    consecutive_errors = 0  # Track consecutive tool results with errors
    total_reassessments = 0  # Cumulative — never reset (drives escalation tiers)
    error_history = []  # List of (turn, category) for pattern detection
    MAX_CONSECUTIVE_ERRORS = 2  # Force strategic reassessment after this many
    build_start_time = time.monotonic()  # Wall-clock timeout tracking
    MAX_BUILD_TIME_SECONDS = 480  # 8-minute per-candidate wall-clock limit (research + build + test)
    smoke_ever_passed = False  # Track smoke test pass across ALL turns
    smoke_passed_at_turn = -1  # Which turn the smoke test first passed
    MAX_TURNS_AFTER_SMOKE = 15  # Allow N turns after smoke for live test + integration test fixes

    # ★ CORE: Multi-turn autonomous loop with verification gate
    # Guardrails: turn limit (AGENT5_MAX_TURNS) + wall-clock timeout.
    # No per-candidate budget cap — let the agent use as many tokens as it
    # needs per turn. The turn limit and timeout prevent runaway costs.
    while turn < AGENT5_MAX_TURNS:
        # Wall-clock timeout check
        elapsed = time.monotonic() - build_start_time
        if elapsed > MAX_BUILD_TIME_SECONDS:
            logger.warning(f"Wall-clock timeout for {candidate.name} after {elapsed:.0f}s", extra={
                "operation": "harness_wallclock_timeout",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "elapsed_seconds": elapsed,
                "turns_used": turn,
            })
            break

        # ★ CORE: Call Claude with all tools + server-side context management
        call_start = time.time()
        response = None
        current_max_tokens = AGENT5_MAX_OUTPUT_TOKENS

        # Retry with exponential backoff on rate limit (429).
        # Parallel builds across candidates can easily hit the per-minute
        # token limit. Retrying after a short wait usually succeeds.
        max_retries = 3
        for retry in range(max_retries + 1):
            try:
                # Use beta API for server-side context management.
                # Two strategies working together:
                #   1. clear_tool_uses: clears old tool results (keeps last 5)
                #      → replaces our manual microcompact
                #   2. compact: when still over limit, Claude summarizes older context
                #      → replaces our manual autocompact
                # Both are managed by the API — we just append messages normally.
                # Phase 1 (research): Sonnet + advisor — cheap, research is mostly fetching + summarizing
                # Phase 2 (build): Opus + advisor — strong reasoning for code + debugging
                # Transition: when api_spec.txt or harness.py is written, switch to Opus
                current_model = AGENT5_BUILDER_MODEL if api_spec_written else RESEARCH_MODEL
                response = client.beta.messages.create(
                    model=current_model,
                    max_tokens=current_max_tokens,
                    betas=["context-management-2025-06-27", "compact-2026-01-12", "advisor-tool-2026-03-01"],
                    # Automatic prompt caching: caches the growing conversation
                    # prefix. Each turn, the prefix (system + earlier messages) is
                    # cached at 1.25x write cost, then read at 0.1x on subsequent
                    # turns. For 6-12 turn conversations this saves ~60% on input.
                    cache_control={"type": "ephemeral"},
                    system=[{
                        "type": "text",
                        "text": BUILDER_SYSTEM_PROMPT.replace(
                            "__OS_TYPE__",
                            "Windows" if sys.platform == "win32" else "Linux",
                        ),
                    }],
                    messages=messages,
                    tools=ALL_TOOLS,
                    thinking={"type": "adaptive"},
                    context_management={
                        "edits": [
                            {
                                "type": "clear_tool_uses_20250919",
                                "trigger": {"type": "input_tokens", "value": 80000},
                                "keep": {"type": "tool_uses", "value": 5},
                                # No clear_at_least — let the API clear whatever
                                # it can at 80K. Small clears that invalidate cache
                                # are still better than letting context grow to
                                # the 150K compaction threshold unchecked.
                            },
                            {
                                "type": "compact_20260112",
                                "trigger": {"type": "input_tokens", "value": 150000},
                                # Custom instructions preserve technical details
                                # that generic summarization would lose.
                                "instructions": (
                                    "Summarize this conversation for continuity. "
                                    "You MUST preserve ALL of the following:\n"
                                    "1. Exact API endpoint URLs, base URL, and auth header format (e.g., 'Bearer' vs 'Token')\n"
                                    "2. Full api_spec.txt contents: INPUT_COMPATIBILITY, ROUTING_TABLE, ENDPOINTS, PYTHON_EXAMPLES\n"
                                    "3. Credential env var names (e.g., MINDEE_API_KEY, VERYFI_CLIENT_ID) and which API version they target\n"
                                    "4. Installed SDK package names and versions (e.g., 'mindee>=4.25.0') and key method names used\n"
                                    "5. All files written to sandbox (harness.py, requirements.txt, smoke_test.py, etc.) and their purpose\n"
                                    "6. Build approach: using official SDK vs raw requests, sync vs async/polling\n"
                                    "7. Specific errors encountered, their root causes, and fixes applied\n"
                                    "8. Which test input forms are compatible vs INCOMPATIBLE and why\n"
                                    "9. Current phase (research/build/verify) and concrete next steps\n"
                                    "Wrap your summary in <summary></summary>."
                                ),
                            },
                            # clear_thinking: use default (keep last turn only).
                            # Thinking blocks are auto-stripped from input billing
                            # on subsequent turns per Anthropic docs.
                        ],
                    },
                )
                break  # Success — exit retry loop
            except anthropic.BadRequestError as e:
                # PTL recovery: if "prompt too long" or "max_tokens exceed",
                # reduce output tokens and retry. Server-side context management
                # should prevent most overflows, but this catches edge cases.
                error_msg = str(e).lower()
                if ("too long" in error_msg or "exceed" in error_msg
                        or "context" in error_msg) and retry < max_retries:
                    logger.warning(f"Context overflow for {candidate.name}, reducing max_tokens", extra={
                        "operation": "ptl_recovery",
                        "trace_id": trace_id,
                        "candidate_name": candidate.name,
                        "retry": retry + 1,
                    })
                    current_max_tokens = max(4096, current_max_tokens // 2)
                    continue
                # Other bad request errors are fatal
                logger.warning(f"Bad request for {candidate.name}: {e}", extra={
                    "operation": f"harness_build_{candidate_label}",
                    "trace_id": trace_id,
                    "error": str(e),
                })
                return FailedHarness(
                    candidate_name=candidate.name,
                    provider=candidate.provider,
                    failure_reason=f"API bad request: {e}",
                    failure_category="unknown",
                    partial_code=_read_harness_code(sandbox_dir),
                    turns_attempted=turn,
                )
            except anthropic.RateLimitError as e:
                if retry < max_retries:
                    wait = (2 ** retry) * 15  # 15s, 30s, 60s
                    logger.info(f"Rate limit for {candidate.name}, waiting {wait}s (retry {retry + 1}/{max_retries})", extra={
                        "operation": f"harness_build_{candidate_label}_rate_limit_retry",
                        "trace_id": trace_id,
                        "retry": retry + 1,
                        "wait_seconds": wait,
                    })
                    time.sleep(wait)
                    continue
                logger.warning(f"Rate limit exhausted for {candidate.name} after {max_retries} retries", extra={
                    "operation": f"harness_build_{candidate_label}",
                    "trace_id": trace_id,
                    "error": str(e), "error_type": "RateLimitError",
                })
                return FailedHarness(
                    candidate_name=candidate.name,
                    provider=candidate.provider,
                    failure_reason=f"Rate limit exceeded after {max_retries} retries: {e}",
                    failure_category="build_timeout",
                    partial_code=_read_harness_code(sandbox_dir),
                    turns_attempted=turn,
                )
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
                logger.warning(f"API error building {candidate.name}", extra={
                    "operation": f"harness_build_{candidate_label}",
                    "trace_id": trace_id,
                    "error": str(e), "error_type": type(e).__name__,
                })
                return FailedHarness(
                    candidate_name=candidate.name,
                    provider=candidate.provider,
                    failure_reason=f"API error during harness building: {e}",
                    failure_category="unknown",
                    partial_code=_read_harness_code(sandbox_dir),
                    turns_attempted=turn,
            )

        # [logging] Log this call's metrics
        log_llm_call(
            logger=logger, response=response, model=current_model,
            trace_id=trace_id, start_time=call_start,
            operation=f"harness_build_{candidate_label}_turn{turn}",
        )

        # [cost tracking] All costs (executor + advisor + cache + web search)
        # calculated from the iterations array per Anthropic API docs
        call_cost = _calculate_call_cost(response, current_model)
        accumulated_cost += call_cost

        # [tracking] Web search count for logging
        server_tool_use = getattr(response.usage, "server_tool_use", None)
        if server_tool_use:
            total_web_searches += getattr(server_tool_use, "web_search_requests", 0) or 0

        # ── Log this turn for conversation history ──
        # Log per-iteration token breakdown for accurate cost tracking
        iterations_log = []
        for iteration in (getattr(response.usage, "iterations", None) or []):
            iterations_log.append({
                "type": getattr(iteration, "type", "message"),
                "model": getattr(iteration, "model", current_model),
                "input_tokens": getattr(iteration, "input_tokens", 0),
                "output_tokens": getattr(iteration, "output_tokens", 0),
                "cache_read": getattr(iteration, "cache_read_input_tokens", 0),
                "cache_create": getattr(iteration, "cache_creation_input_tokens", 0),
            })
        # Top-level cache metrics for quick visibility
        try:
            cache_read = int(getattr(response.usage, "cache_read_input_tokens", 0) or 0)
            cache_create = int(getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
            total_input = int(response.usage.input_tokens) + cache_read + cache_create
            cache_hit_pct = round(cache_read / total_input * 100, 1) if total_input > 0 else 0.0
        except (TypeError, ValueError):
            cache_read = 0
            cache_create = 0
            cache_hit_pct = 0.0

        turn_log = {
            "turn": turn,
            "stop_reason": response.stop_reason,
            "cost_usd": round(call_cost, 4),
            "model": current_model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_tokens": cache_read,
            "cache_create_tokens": cache_create,
            "cache_hit_pct": cache_hit_pct,
            "text": "",
            "tool_calls": [],
            "tool_results": [],
            "iterations": iterations_log,
        }
        for block in response.content:
            if block.type == "text":
                turn_log["text"] += block.text
            elif block.type == "thinking":
                # Log thinking blocks for debugging visibility
                thinking_text = getattr(block, "thinking", "")
                if "thinking" not in turn_log:
                    turn_log["thinking"] = []
                turn_log["thinking"].append(thinking_text[:500])
            elif block.type == "tool_use":
                tool_entry = {"tool": block.name, "id": block.id}
                if block.name in CUSTOM_TOOL_NAMES:
                    tool_entry["input"] = block.input
                else:
                    tool_entry["input"] = "(server tool -- handled by API)"
                turn_log["tool_calls"].append(tool_entry)
            elif block.type == "server_tool_use" and getattr(block, "name", "") == "advisor":
                turn_log["tool_calls"].append({"tool": "advisor", "id": block.id, "input": "(advisor call)"})
            elif block.type == "advisor_tool_result":
                content = getattr(block, "content", None)
                advice_text = ""
                if content and hasattr(content, "text"):
                    advice_text = content.text[:500]
                elif content and hasattr(content, "encrypted_content"):
                    advice_text = "(encrypted advisor response)"
                turn_log["tool_results"].append({"tool": "advisor", "result": advice_text})
        conversation_log.append(turn_log)

        # ── Handle compaction (server-side context management) ──
        # When the API compacts context, it returns stop_reason="compaction".
        # We just continue — the API handles cleanup on next call.
        if response.stop_reason == "compaction":
            logger.info(f"Server-side compaction for {candidate.name}", extra={
                "operation": "server_compaction",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "turn": turn,
            })
            messages.append({"role": "assistant", "content": response.content})
            turn += 1
            continue

        # ── Handle pause_turn (server tool loop took too long) ──
        if response.stop_reason == "pause_turn":
            logger.info(f"pause_turn for {candidate.name}, continuing", extra={
                "operation": f"harness_pause_turn_{candidate_label}",
                "trace_id": trace_id,
                "turn": turn,
            })
            # Don't reset messages — server-side context management (compact)
            # handles overflow. Resetting loses all prior research and tool results.
            messages.append({"role": "assistant", "content": response.content})
            turn += 1
            continue

        # ── Extract text from response ──
        last_text = _extract_text_from_response(response)

        # Also check agent's text for smoke test pass / completion signals
        if "SMOKE TEST PASSED" in last_text and not smoke_ever_passed:
            smoke_ever_passed = True
            smoke_passed_at_turn = turn

        # Check for HARNESS_COMPLETE signal in text
        if smoke_ever_passed and "HARNESS_COMPLETE" in last_text:
            if response.stop_reason == "end_turn":
                logger.info(f"Completion signal in text for {candidate.name}", extra={
                    "operation": "completion_signal_text",
                    "trace_id": trace_id,
                })
                verification_passed = True
                break

        # ── Extract and save web_fetch content to files ──
        new_docs = _extract_and_save_web_content(
            response, sandbox_dir, existing_count=len(saved_doc_files),
        )
        if new_docs:
            saved_doc_files.extend(new_docs)
            conversation_log.append({
                "turn": f"save-docs-{turn}",
                "stop_reason": "docs_saved",
                "text": f"Saved fetched docs to: {new_docs}",
                "tool_calls": [], "tool_results": [],
            })

        # ── Context management ──
        # No manual context reset. Server-side context management handles compression:
        #   - clear_tool_uses_20250919: clears old tool results at 80K tokens (keeps last 5)
        #   - compact_20260112: Claude-powered summarization at 150K tokens
        # This is how Claude Code handles context — gradual compression, not hard deletion.
        # The builder keeps research context available for debugging. If it needs a detail
        # from the API docs during Phase 2, it's still accessible (summarized, not deleted).

        # ── Check for completion signal ──
        if response.stop_reason == "end_turn":
            if "HARNESS_COMPLETE" in last_text:
                # ════════════════════════════════════════════
                # ★ VERIFICATION GATE — the core hardening
                # ════════════════════════════════════════════
                # Run verification checks. If issues found AND retries
                # remain, feed issues back to Claude for fixing.
                if verification_attempts < AGENT5_MAX_VERIFICATION_RETRIES:
                    issues = _run_verification_checks(
                        sandbox_dir, candidate, credentials, logger, trace_id,
                    )
                    if issues:
                        logger.info(f"Verification issues for {candidate.name}, retry {verification_attempts + 1}", extra={
                            "operation": "verification_gate_fail",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "attempt": verification_attempts + 1,
                        })
                        # Feed issues back to Claude for fixing
                        feedback = (
                            f"## Verification Issues Found — Please Fix\n\n"
                            f"Your harness signaled complete but verification found problems:\n\n"
                            f"{issues}\n\n"
                            f"Please fix these issues, re-run smoke_test.py, "
                            f"and signal HARNESS_COMPLETE again when everything is fixed."
                        )
                        messages.append({"role": "assistant", "content": response.content})
                        messages.append({"role": "user", "content": feedback})
                        conversation_log.append({
                            "turn": f"verify-{verification_attempts + 1}",
                            "stop_reason": "verification_gate",
                            "text": f"VERIFICATION FAILED:\n{issues}",
                            "tool_calls": [],
                            "tool_results": [],
                        })
                        verification_attempts += 1
                        turn += 1
                        continue  # Claude will fix and re-signal
                    else:
                        # No issues found — verification passed cleanly
                        verification_passed = True
                else:
                    # Retries exhausted — accept the harness as-is.
                    # Do NOT re-run verification (that caused the Lido loop bug).
                    verification_passed = True
                logger.info(f"Harness {'verified' if verification_passed else 'accepted (retries exhausted)'} for {candidate.name}", extra={
                    "operation": "harness_build_complete",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turns_used": turn + 1,
                    "build_cost": accumulated_cost,
                    "verification_attempts": verification_attempts,
                    "verification_passed": verification_passed,
                })
                break

            elif "HARNESS_FAILED" in last_text:
                logger.info(f"Harness failed for {candidate.name} (agent reported)", extra={
                    "operation": "harness_build_agent_failed",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turns_used": turn + 1,
                })
                return FailedHarness(
                    candidate_name=candidate.name,
                    provider=candidate.provider,
                    failure_reason=last_text[:500],
                    failure_category=_categorize_failure(last_text),
                    partial_code=_read_harness_code(sandbox_dir),
                    turns_attempted=turn + 1,
                )
            else:
                # end_turn without signal — treat as complete if harness exists
                if (sandbox_dir / "harness.py").exists():
                    break
                turn += 1
                continue

        # ── Handle tool_use: dispatch custom tools ──
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name in CUSTOM_TOOL_NAMES:
                    # ── ask_research: spawn targeted research sub-agent ──
                    # Enriches the question with actual context (harness code,
                    # last error) so the research agent can give precise answers
                    # instead of generic API overviews.
                    if block.name == "ask_research":
                        question = block.input.get("question", "")
                        if not question:
                            result_text = "Error: empty question. Ask a specific question about the API."
                        else:
                            # Enrich question with actual context so research is targeted
                            # (Claude Code pattern: sub-agents get relevant context, not bare queries)
                            enriched_question = f"Service: {candidate.name} ({candidate.provider})\n"
                            enriched_question += f"API Docs URL: {candidate.verified_api_docs_url}\n\n"
                            enriched_question += f"QUESTION: {question}\n"

                            # Include what we already know (so research doesn't re-find it)
                            # Send key sections: first 2K (endpoints, auth) + DOC_REFERENCES + DOC_MAP
                            # so the research agent has the URL navigation map for targeted lookups.
                            try:
                                spec_path = sandbox_dir / "api_spec.txt"
                                if spec_path.exists():
                                    full_spec = spec_path.read_text(encoding="utf-8")
                                    # Always include the beginning (endpoints, auth, request format)
                                    spec_summary = full_spec[:2000]
                                    # Also include DOC_REFERENCES and DOC_MAP if they exist
                                    for section in ("DOC_REFERENCES:", "DOC_MAP:"):
                                        idx = full_spec.find(section)
                                        if idx > 2000:  # Only add if not already in the first 2K
                                            # Grab from section header to next section or end, max 1K
                                            section_text = full_spec[idx:idx + 1000]
                                            spec_summary += f"\n\n{section_text}"
                                    enriched_question += f"\nWHAT WE ALREADY KNOW (from api_spec.txt):\n{spec_summary}\n"
                                    enriched_question += "DO NOT re-research info already in the spec above. Focus on what's MISSING or WRONG.\n"
                            except OSError:
                                pass

                            # Include last error from prior tool results in this turn
                            prior_results_text = " ".join(
                                r.get("content", "") for r in tool_results if isinstance(r.get("content"), str)
                            )
                            if prior_results_text:
                                enriched_question += f"\nLAST ERROR CONTEXT: {prior_results_text[:500]}\n"

                            # Include relevant harness code snippet
                            harness_code = _read_harness_code(sandbox_dir)
                            if harness_code:
                                lines = harness_code.split("\n")
                                relevant = "\n".join(lines[:40])
                                enriched_question += f"\nCURRENT HARNESS CODE (first 40 lines):\n```python\n{relevant}\n```"

                            research_answer, research_cost = _run_targeted_research(
                                client, enriched_question, candidate.name, logger, trace_id,
                            )
                            accumulated_cost += research_cost
                            result_text = research_answer
                            # Save research result to file for future reference
                            research_file = sandbox_dir / f"research_turn{turn}.txt"
                            try:
                                research_file.write_text(
                                    f"# Research Q: {question}\n\n{research_answer}",
                                    encoding="utf-8",
                                )
                            except OSError:
                                pass
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text[:8000],  # Cap research answers
                        })
                        if conversation_log:
                            conversation_log[-1]["tool_results"].append({
                                "tool": "ask_research",
                                "result": result_text[:500],
                            })
                        continue

                    # ── Standard custom tool dispatch ──
                    # Pass credentials so run_code subprocesses can do live API validation
                    result_text, exit_code = _dispatch_tool(block.name, block.input, sandbox_dir, extra_env=credentials)
                    # Persist large outputs to disk (Claude Code pattern: >30KB → file)
                    result_text = _persist_large_output(result_text, sandbox_dir, turn)
                    # Detect Phase 1 → Phase 2 transition.
                    # Primary trigger: api_spec.txt written (intended flow).
                    # Fallback trigger: harness.py or requirements.txt written
                    # (agent skipped spec and went straight to coding).
                    if not api_spec_written and block.name == "write_file":
                        written_file = block.input.get("filename", "")
                        if written_file in ("api_spec.txt", "harness.py", "requirements.txt"):
                            api_spec_written = True
                            trigger = written_file
                            logger.info(f"Phase 1 complete for {candidate.name}, switching to Opus (trigger: {trigger})", extra={
                                "operation": "phase_transition",
                                "trace_id": trace_id,
                                "candidate_name": candidate.name,
                                "turn": turn,
                                "trigger_file": trigger,
                            })
                    # Set is_error flag per Anthropic docs — tells Claude the result
                    # is an error, triggering smarter retry/correction behavior.
                    # Without this, Claude treats "Error: file not found" the same
                    # as "Written 500 chars to harness.py".
                    # Use structured exit code (like Claude Code) — no string parsing
                    has_tool_error = exit_code != 0
                    tool_result_entry = {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                    if has_tool_error:
                        tool_result_entry["is_error"] = True
                    tool_results.append(tool_result_entry)
                    # Log tool result (truncated for readability)
                    if conversation_log:
                        conversation_log[-1]["tool_results"].append({
                            "tool": block.name,
                            "result": result_text[:500],
                        })
                    # (live_test injection removed — agent validates with real test data)

            # ── Detect smoke test passing in tool results ──
            # CRITICAL: Track across ALL turns (not just last_text).
            all_results_text_raw = " ".join(
                r.get("content", "") for r in tool_results if isinstance(r.get("content"), str)
            )
            if "SMOKE TEST PASSED" in all_results_text_raw and not smoke_ever_passed:
                smoke_ever_passed = True
                smoke_passed_at_turn = turn
                # Inject milestone so agent knows to transition to live tests
                milestone = (
                    "\n\n## MILESTONE: SMOKE TEST PASSED\n\n"
                    "Structural validation complete. Now run LIVE API tests with real files.\n"
                    "Credentials ARE available in your environment. You MUST get success=True "
                    "and output_len > 100 for each file type before signaling HARNESS_COMPLETE."
                )
                messages.append({"role": "user", "content": milestone})
                logger.info(f"Smoke test PASSED for {candidate.name} at turn {turn}", extra={
                    "operation": "smoke_test_passed",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turn": turn,
                })

                # ════════════════════════════════════════════════
                # ★ POST-SMOKE: Validate input forms with real test data
                # ════════════════════════════════════════════════
                # Smoke pass = code structure is correct.
                # Now validate each compatible input form with a real API call.
                # The agent checks INPUT_COMPATIBILITY and runs real test cases.
                # Agent validates with real test data from Agent 3 test cases.

                if not credentials:
                    logger.info(f"No credentials for {candidate.name}, accepting on smoke pass", extra={
                        "operation": "accept_on_smoke",
                        "trace_id": trace_id,
                    })
                    verification_passed = True
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})
                    break

                # Build a summary of test case input forms for validation
                form_groups: dict[str, list[str]] = {}
                for tc in input_data.test_cases.test_cases:
                    has_file = "with file" if tc.test_file_path else "text only"
                    key = f"{tc.input_type} ({has_file})"
                    if key not in form_groups:
                        form_groups[key] = []
                    if len(form_groups[key]) < 1:  # One example per form
                        preview = tc.input_data[:100].replace("\n", " ")
                        form_groups[key].append(f'{tc.id}: "{preview}..."')

                forms_text = "\n".join(
                    f"  - {form}: {examples[0]}" for form, examples in form_groups.items()
                )

                validation_msg = (
                    f"\n\n## SMOKE TEST PASSED -- Validate Compatible Input Forms\n\n"
                    f"Your harness code is structurally correct. Now validate each "
                    f"compatible input form with a REAL API call.\n\n"
                    f"### Test case input forms:\n{forms_text}\n\n"
                    f"For each form above:\n"
                    f"1. Check api_spec.txt INPUT_COMPATIBILITY\n"
                    f"2. If COMPATIBLE: run a real test via run_code:\n"
                    f"   python -c \"import json, harness; r = harness.run({{'text': '...', "
                    f"'input_type': '...', 'input_context': None, 'test_file_path': None}}); "
                    f"print(json.dumps({{'success': r['success'], 'output': r['output'][:200], "
                    f"'error': r.get('error')}}, default=str))\"\n"
                    f"3. If INCOMPATIBLE: verify harness returns success=False with INCOMPATIBLE error\n\n"
                    f"After validating all forms, signal HARNESS_COMPLETE.\n"
                )
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                messages.append({"role": "user", "content": validation_msg})
                turn += 1
                continue

            # ── Detect HARNESS_COMPLETE in tool results ──
            if smoke_ever_passed and "HARNESS_COMPLETE" in all_results_text_raw:
                logger.info(f"Integration test / HARNESS_COMPLETE for {candidate.name}", extra={
                    "operation": "integration_test_passed",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turn": turn,
                })
                verification_passed = True
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                break

            # ── Track consecutive errors for dead-end detection ──
            # Use specific patterns that indicate ACTUAL test/command failures.
            # Previous broad patterns ("error", "404") triggered false positives
            # when docs text mentioned HTTP error codes (e.g., "returns 404 for
            # invalid keys" in fetched_docs would increment the error counter).
            all_results_text = all_results_text_raw.lower()
            has_error = any(sig in all_results_text for sig in [
                "traceback (most recent", "assertionerror",
                "smoke test failed",
                "modulenotfounderror", "syntaxerror", "indentationerror",
                "connectionrefusederror", "connectionerror",
                "401 unauthorized", "403 forbidden",
                "exit code: 1", "exit code: 2",
                "wrong x-auth-key", "api key is invalid",
                "not authorized", "permission denied",
            ])

            if has_error:
                consecutive_errors += 1
                # Categorize error for pattern detection
                if any(s in all_results_text for s in ["401", "403", "auth", "unauthorized", "forbidden", "wrong x-auth-key"]):
                    error_history.append((turn, "auth"))
                elif any(s in all_results_text for s in ["404", "not found", "connectionrefused"]):
                    error_history.append((turn, "endpoint"))
                elif any(s in all_results_text for s in ["400", "bad request", "invalid input", "unsupported"]):
                    error_history.append((turn, "format"))
                else:
                    error_history.append((turn, "other"))
            else:
                consecutive_errors = 0

            # Append assistant response + tool results to conversation
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            # ── Force completion after smoke + live fix attempts ──
            # If smoke passed but agent is still trying to fix live test
            # after MAX_TURNS_AFTER_SMOKE turns, stop and fail.
            if smoke_ever_passed and (turn - smoke_passed_at_turn) >= MAX_TURNS_AFTER_SMOKE:
                logger.warning(f"Force-accepting after {turn - smoke_passed_at_turn} turns since smoke for {candidate.name}", extra={
                    "operation": "force_accept_after_smoke",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                })
                # Smoke passed, agent had enough turns to validate. Accept as-is.
                # The post-loop mechanical test execution will reveal any issues.
                verification_passed = True
                break

            # If stuck in an error loop, force escalating reassessment
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                total_reassessments += 1
                last_errors = all_results_text[:500]

                # Detect repeating error category
                pattern_hint = ""
                if len(error_history) >= 3:
                    recent_cats = [cat for _, cat in error_history[-3:]]
                    if len(set(recent_cats)) == 1:
                        cat = recent_cats[0]
                        cat_label = {"auth": "authentication/auth header", "endpoint": "endpoint URL",
                                     "format": "request format/body", "other": "error"}[cat]
                        pattern_hint = (
                            f"\n**PATTERN DETECTED:** The same '{cat_label}' error has occurred "
                            f"3+ times. This strongly suggests your fundamental assumption about "
                            f"the {cat_label} is WRONG — not the details. "
                            f"Use `ask_research` to verify it.\n"
                        )

                # Escalating tiers based on cumulative reassessments
                if total_reassessments == 1:
                    # Tier 1: Fix the specific issue
                    reassessment = (
                        f"\n\n## STRATEGIC REASSESSMENT (Tier 1)\n\n"
                        f"You have hit errors for {consecutive_errors} consecutive turns.\n"
                        f"**Last error:** {last_errors[:300]}\n\n"
                        f"Compare your code against the docs:\n"
                        f"1. `read_file('harness.py')` — check URL, auth, request format\n"
                        f"2. `read_file('api_spec.txt')` — check what docs say\n"
                        f"3. Fix the SPECIFIC mismatch with `patch_file`\n\n"
                        f"**Common trap:** If the API says 'missing field/file/parameter' but your "
                        f"code IS sending it, the REQUEST FORMAT is wrong — not the data. Check: "
                        f"are you using the right `requests` parameter? (json= vs data= vs files= "
                        f"vs params=). Use `ask_research` to find the exact format from the official "
                        f"docs or OpenAPI spec if unsure.\n"
                        + pattern_hint
                    )
                elif total_reassessments == 2:
                    # Tier 2: Question fundamental assumptions
                    reassessment = (
                        f"\n\n## STRATEGIC REASSESSMENT (Tier 2 — question your assumptions)\n\n"
                        f"You have been stuck for multiple error cycles. Fixing details is not working.\n"
                        f"**Last error:** {last_errors[:300]}\n\n"
                        f"Your APPROACH may be wrong — not just the details. The endpoint URL, "
                        f"API version, or platform may have changed since the research was done.\n\n"
                        f"**REQUIRED ACTION:** Use `ask_research` to verify your fundamental assumption:\n"
                        f"  `ask_research('What is the current API endpoint for {candidate.name}? "
                        f"Has the company migrated to a new platform or domain?')`\n\n"
                        f"Do NOT try another variation of the same fix. Research first.\n"
                        + pattern_hint
                    )
                else:
                    # Tier 3: Different approach or fail
                    reassessment = (
                        f"\n\n## STRATEGIC REASSESSMENT (Tier 3 — last chance)\n\n"
                        f"You have been stuck for {total_reassessments} reassessment cycles.\n"
                        f"**Last error:** {last_errors[:300]}\n\n"
                        f"Your current approach is NOT working. Choose ONE:\n"
                        f"(a) Use `ask_research` to find a completely different endpoint, SDK, "
                        f"or API version — then rebuild with `patch_file`\n"
                        f"(b) Signal HARNESS_FAILED with a clear explanation of what you tried\n\n"
                        f"Do NOT try the same approach again.\n"
                        + pattern_hint
                    )

                messages.append({"role": "user", "content": reassessment})
                consecutive_errors = 0  # Reset streak — give agent a fresh chance

                conversation_log.append({
                    "turn": f"reassessment-{turn}",
                    "stop_reason": f"dead_end_tier{total_reassessments}",
                    "text": f"Escalating reassessment tier {total_reassessments} after {MAX_CONSECUTIVE_ERRORS} consecutive errors",
                    "tool_calls": [], "tool_results": [],
                })

        turn += 1

    # ── Save conversation log for debugging ──
    _save_conversation_log(sandbox_dir, conversation_log, candidate.name)

    # ── Post-loop: Assemble result ──
    harness_code = _read_harness_code(sandbox_dir)

    if not harness_code:
        return FailedHarness(
            candidate_name=candidate.name,
            provider=candidate.provider,
            failure_reason=(
                f"No harness.py produced after {turn} turns. "
                f"Last output: {last_text[:300]}"
            ),
            failure_category="build_timeout",
            partial_code=None,
            turns_attempted=turn,
        )

    requirements = _read_requirements(sandbox_dir)
    smoke_passed = smoke_ever_passed or "SMOKE TEST PASSED" in last_text or "HARNESS_COMPLETE" in last_text

    if not smoke_passed and not verification_passed:
        return FailedHarness(
            candidate_name=candidate.name,
            provider=candidate.provider,
            failure_reason=(
                f"Harness code was generated but smoke test never passed after "
                f"{turn} turns. Last output: {last_text[:300]}"
            ),
            failure_category="build_timeout",
            partial_code=harness_code,
            turns_attempted=turn,
        )

    auth_env_vars = _extract_env_vars_from_code(harness_code)
    if not auth_env_vars:
        auth_env_vars = [f"{provider_slug}_API_KEY"]

    # Determine supported types from test cases
    input_types = set()
    output_types = set()
    for tc in input_data.test_cases.test_cases:
        for st_name in candidate.relevant_subtasks:
            if st_name in tc.sub_task_ref or tc.sub_task_ref in st_name:
                input_types.add(tc.input_type)
                output_types.add(tc.output_type)
    if not input_types:
        for tc in input_data.test_cases.test_cases:
            input_types.add(tc.input_type)
            output_types.add(tc.output_type)

    # Build validation notes
    validation_parts = [f"Smoke: {'PASS' if smoke_passed else 'INCOMPLETE'}"]
    validation_parts.append(f"Verification gate: {verification_attempts} retries, {'PASSED' if verification_passed else 'EXHAUSTED'}")

    # Read api_spec.txt as comprehensive API knowledge
    api_knowledge = None
    api_spec_path = sandbox_dir / "api_spec.txt"
    if api_spec_path.exists():
        try:
            api_knowledge = api_spec_path.read_text(encoding="utf-8")
        except Exception:
            pass

    return TestHarness(
        candidate_name=candidate.name,
        provider=candidate.provider,
        harness_dir=str(sandbox_dir),
        entry_file="harness.py",
        requirements=requirements,
        auth_env_vars=auth_env_vars,
        auth_method=candidate.auth_method,
        supported_input_types=sorted(input_types),
        supported_output_types=sorted(output_types),
        smoke_test_passed=smoke_passed,
        live_validation_attempted=credentials is not None,
        live_validation_passed=verification_passed or None,
        live_validation_notes="\n".join(validation_parts),
        validation_notes="\n".join(validation_parts),
        build_turns=turn + 1,
        build_cost_usd=round(accumulated_cost, 4),
        harness_code=harness_code,
        api_knowledge=api_knowledge,
    )


def _resolve_credentials(
    input_data: Agent5Input,
    candidate: ScreenedCandidate,
    provider_slug: str,
) -> dict[str, str] | None:
    """Resolve credentials for a candidate from registry or env vars."""
    credentials = None

    if input_data.provider_credentials:
        norm_provider = _normalize(candidate.provider)
        norm_candidate = _normalize(candidate.name)
        credentials = (
            input_data.provider_credentials.get(norm_candidate)
            or input_data.provider_credentials.get(norm_provider)
        )

    if not credentials:
        # Check env vars as fallback
        possible_vars = [f"{provider_slug}_API_KEY"]
        # Also try to extract from any existing harness code
        harness_code = _read_harness_code(Path("runs") / input_data.trace_id / "harnesses" / _candidate_slug(candidate.name))
        if harness_code:
            possible_vars.extend(_extract_env_vars_from_code(harness_code))

        env_creds = {}
        for var in possible_vars:
            val = os.environ.get(var)
            if val:
                env_creds[var] = val
        if env_creds:
            credentials = env_creds

    return credentials


# ============================================================================
# Helpers for post-loop result assembly
# ============================================================================

def _read_harness_code(sandbox_dir: Path) -> str | None:
    """Read harness.py from sandbox if it exists."""
    harness_file = sandbox_dir / "harness.py"
    if harness_file.exists():
        try:
            return harness_file.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def _read_requirements(sandbox_dir: Path) -> list[str]:
    """Read requirements.txt from sandbox, return list of package names."""
    req_file = sandbox_dir / "requirements.txt"
    if req_file.exists():
        try:
            content = req_file.read_text(encoding="utf-8")
            return [
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        except OSError:
            return []
    return []


def _extract_env_vars_from_code(code: str) -> list[str]:
    """
    Parse harness.py source code to find environment variable names
    used via os.environ.get() or os.environ[].

    Returns a deduplicated, sorted list of env var names that look like
    auth-related credentials (containing KEY, TOKEN, SECRET, ID, PASSWORD,
    or AUTH). Ignores generic vars like PYTHONDONTWRITEBYTECODE.
    """
    if not code:
        return []

    # Match os.environ.get("VAR_NAME", ...) and os.environ["VAR_NAME"]
    pattern = r'os\.environ(?:\.get)?\s*[\(\[]\s*["\']([A-Z][A-Z0-9_]*)["\']'
    matches = re.findall(pattern, code)

    # Filter to auth-related env vars (ignore generic Python/system vars)
    auth_keywords = {"KEY", "TOKEN", "SECRET", "ID", "PASSWORD", "AUTH", "CREDENTIAL", "REALM"}
    auth_vars = []
    for var in matches:
        if any(kw in var for kw in auth_keywords):
            auth_vars.append(var)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for var in auth_vars:
        if var not in seen:
            seen.add(var)
            result.append(var)
    return result


def _env_var_similarity(a: str, b: str) -> float:
    """
    Compute similarity between two normalized env var names.
    Uses longest common substring ratio. Returns 0.0-1.0.

    "NANONETSAPIKEY" vs "NANONETSINAPIKEY" → high similarity
    "NANONETSAPIKEY" vs "OPENAIKEY" → low similarity
    """
    if not a or not b:
        return 0.0
    # Longest common substring
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    best = 0
    for i in range(len(shorter)):
        for j in range(i + 3, len(shorter) + 1):  # min substring length 3
            if shorter[i:j] in longer:
                best = max(best, j - i)
    return best / len(longer) if longer else 0.0


def _save_conversation_log(
    sandbox_dir: Path,
    conversation_log: list[dict],
    candidate_name: str,
) -> None:
    """
    Save the conversation log to the sandbox directory as a readable JSON file.
    This makes Agent 5's builder loop transparent — you can see every turn,
    what Claude said, what tools it called, and what results it got.
    """
    log_path = sandbox_dir / "conversation_log.json"
    try:
        log_path.write_text(
            json.dumps(conversation_log, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass  # Non-critical — don't crash the build if logging fails


def _categorize_failure(text: str) -> str:
    """Infer a failure category from the agent's failure message."""
    text_lower = text.lower()
    # Dead URL / unreachable resource (check BEFORE "not found" to avoid false match)
    if any(kw in text_lower for kw in [
        "couldn't download", "download file", "url unreachable",
        "url not found", "file from provided url", "file from url",
    ]):
        return "docs_unusable"
    if any(kw in text_lower for kw in ["docs", "documentation"]):
        return "docs_unusable"
    # Auth — only when explicitly about authentication, not just "400"
    if any(kw in text_lower for kw in [
        "401", "403", "unauthorized", "forbidden", "wrong x-auth",
        "invalid api key", "invalid key", "paid", "enterprise", "subscription",
    ]):
        return "auth_blocked"
    if any(kw in text_lower for kw in ["incompatible", "not support", "doesn't support"]):
        return "api_incompatible"
    if any(kw in text_lower for kw in ["install", "pip", "package", "dependency"]):
        return "dependency_failure"
    if any(kw in text_lower for kw in [
        "timeout", "budget", "turns", "credit", "balance",
        "billing", "quota", "exceeded", "rate limit",
    ]):
        return "build_timeout"
    return "unknown"


# ============================================================================
# Venv Creation
# ============================================================================
# Each candidate gets its own venv for dependency isolation.
# Container-ready: in production, replace _create_venv() with a container
# creation function. The rest of the code doesn't change because
# _build_sandbox_env() is the only place that knows about the venv path.
# ============================================================================

def _create_venv(sandbox_dir: Path, logger, trace_id: str, candidate_name: str) -> bool:
    """
    Create a Python venv in the sandbox directory. Returns True on success.

    The venv is at sandbox_dir/.venv. Commands run via _tool_run_code()
    will automatically use it (via _build_sandbox_env() PATH injection).
    """
    venv_dir = sandbox_dir / ".venv"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info(f"Venv created for {candidate_name}", extra={
                "operation": "venv_create",
                "trace_id": trace_id,
                "candidate_name": candidate_name,
                "venv_dir": str(venv_dir),
            })
            return True
        else:
            logger.warning(f"Venv creation failed for {candidate_name}: {result.stderr[:500]}", extra={
                "operation": "venv_create_failed",
                "trace_id": trace_id,
                "candidate_name": candidate_name,
            })
            return False
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"Venv creation error for {candidate_name}: {e}", extra={
            "operation": "venv_create_error",
            "trace_id": trace_id,
            "candidate_name": candidate_name,
        })
        return False


# ============================================================================
# In-Loop: Verification Check
# ============================================================================
# When Claude signals HARNESS_COMPLETE, we verify the harness exists.
# The agent already validated compatible input forms with real test data
# in Phase 3. This is just a structural sanity check.
# ============================================================================


def _run_verification_checks(
    sandbox_dir: Path,
    candidate: ScreenedCandidate,
    credentials: dict[str, str] | None,
    logger,
    trace_id: str,
) -> str | None:
    """
    Verify the harness exists and is structurally valid.
    Called when Claude signals HARNESS_COMPLETE.

    The agent already validated compatible input forms with real test data
    in Phase 3. This is just a structural sanity check.
    """
    if not _read_harness_code(sandbox_dir):
        return "harness.py does not exist or is empty."
    return None


# ============================================================================
# Post-Build Test Execution Functions
# ============================================================================
# These functions handle mechanical test execution and evaluation AFTER
# harnesses are built. Agent 5 owns the full lifecycle: build + test.
# ============================================================================

MECHANICAL_EVAL_TYPES = {"exact_match", "format_compliance"}
LLM_EVAL_TYPES = {"semantic_similarity", "contains_key_info", "subjective_quality"}
RATE_LIMIT_INDICATORS = {"rate limit", "429", "too many requests", "quota exceeded"}

EVALUATION_SYSTEM_PROMPT = """\
You are an expert evaluator comparing an API's raw response against ground truth.

The API received a file (invoice, document, image) and returned its raw response.
Your job: judge whether the API correctly extracted the expected information.

IMPORTANT: The raw API response uses the provider's own field names and structure.
The ground truth uses standardized field names. You must match semantically:
- "seller_name", "vendor_name", "supplier_name" are ALL the same field
- "invoice_amount", "total_amount", "grand_total" are ALL the same field
- Line items may be nested differently but contain the same data

For each criterion, provide:
- score: 0.0 to 1.0 (degree of satisfaction)
- passed: true if score >= 0.5
- reasoning: one sentence explaining your judgment

Scoring rules:
- Found the exact value (even under a different field name) → 1.0
- Found a close match (minor formatting difference) → 0.8-0.9
- Found partial data (some items but not all) → proportional (3 of 5 = 0.6)
- Not found in the response → 0.0

Be STRICT but FAIR about the data. Don't penalize for different field names
or JSON structure — only penalize for missing or incorrect values.
"""


def _adapt_test_input(test_case: TestCase, harness: TestHarness) -> dict:
    """Map a test case to the harness.run() input format."""
    return {
        "text": test_case.input_data,
        "input_type": test_case.input_type,
        "input_context": test_case.input_context,
        "test_file_path": test_case.test_file_path,
    }


def _execute_single_test(
    sandbox_dir: Path,
    adapted_input: dict,
    credentials: dict[str, str] | None,
    timeout: int,
) -> dict:
    """
    Execute one test case via subprocess in the harness's venv.
    Returns the harness result dict or an error dict. Never raises.
    """
    error_result = {
        "output": "",
        "latency_ms": 0.0,
        "tokens_used": None,
        "cost_usd": None,
        "raw_response": {},
        "success": False,
        "error": None,
    }

    input_path = sandbox_dir / "_test_input.json"
    output_path = sandbox_dir / "_test_output.json"

    try:
        input_path.write_text(json.dumps(adapted_input), encoding="utf-8")
    except Exception as e:
        return {**error_result, "error": f"Failed to write test input: {e}"}

    exec_script = (
        'import sys, json\n'
        'sys.path.insert(0, ".")\n'
        'import harness\n'
        'input_data = json.loads(open("_test_input.json", encoding="utf-8").read())\n'
        'result = harness.run(input_data)\n'
        'with open("_test_output.json", "w", encoding="utf-8") as f:\n'
        '    json.dump(result, f, default=str)\n'
    )

    env = _build_sandbox_env(sandbox_dir, credentials)

    try:
        if output_path.exists():
            output_path.unlink()

        proc = subprocess.run(
            [sys.executable, "-c", exec_script],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(sandbox_dir),
            env=env,
        )

        if output_path.exists():
            try:
                result = json.loads(output_path.read_text(encoding="utf-8"))
                return {
                    "output": str(result.get("output", "")),
                    "latency_ms": float(result.get("latency_ms", 0.0)),
                    "tokens_used": result.get("tokens_used"),
                    "cost_usd": result.get("cost_usd"),
                    "raw_response": result.get("raw_response", {}),
                    "success": bool(result.get("success", False)),
                    "error": result.get("error"),
                }
            except (json.JSONDecodeError, Exception) as e:
                return {**error_result, "error": f"Failed to parse harness output: {e}"}

        stderr = (proc.stderr or "")[:500]
        return {**error_result, "error": f"Harness crashed: {stderr}"}

    except subprocess.TimeoutExpired:
        return {**error_result, "error": f"Test timed out after {timeout}s"}
    except Exception as e:
        return {**error_result, "error": f"Execution error: {e}"}
    finally:
        for p in (input_path, output_path):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def _is_rate_limit_error(error_msg: str | None) -> bool:
    """Check if an error message indicates a rate limit."""
    if not error_msg:
        return False
    lower = error_msg.lower()
    return any(indicator in lower for indicator in RATE_LIMIT_INDICATORS)


def _execute_all_tests(
    sandbox_dir: Path,
    test_cases: list[TestCase],
    harness: TestHarness,
    credentials: dict[str, str] | None,
    logger: logging.Logger,
    trace_id: str,
) -> list[tuple[TestCase, dict]]:
    """
    Execute all test cases in randomized order with rate limiting.
    Returns list of (test_case, harness_result) tuples.
    Early aborts if error rate exceeds threshold.
    """
    shuffled = list(test_cases)
    random.shuffle(shuffled)

    results: list[tuple[TestCase, dict]] = []
    error_count = 0

    for i, tc in enumerate(shuffled):
        adapted = _adapt_test_input(tc, harness)

        result = _execute_single_test(
            sandbox_dir, adapted, credentials, AGENT6_TEST_TIMEOUT
        )

        if not result["success"] and _is_rate_limit_error(result.get("error")):
            logger.info(
                f"Rate limit hit for {harness.candidate_name}, backing off {AGENT6_RATE_LIMIT_BACKOFF}s",
                extra={"operation": "rate_limit_backoff", "trace_id": trace_id},
            )
            time.sleep(AGENT6_RATE_LIMIT_BACKOFF)
            result = _execute_single_test(
                sandbox_dir, adapted, credentials, AGENT6_TEST_TIMEOUT
            )

        results.append((tc, result))

        error_msg = result.get("error") or ""
        is_incompatible = not result["success"] and "INCOMPATIBLE" in error_msg.upper()
        if not result["success"] and not is_incompatible:
            error_count += 1

        tests_run = i + 1
        if (
            tests_run >= AGENT6_MIN_TESTS_BEFORE_ABORT
            and error_count / tests_run > AGENT6_ERROR_ABORT_THRESHOLD
        ):
            logger.warning(
                f"Early abort for {harness.candidate_name}: "
                f"{error_count}/{tests_run} errors ({error_count/tests_run:.0%})",
                extra={
                    "operation": "early_abort",
                    "trace_id": trace_id,
                    "candidate_name": harness.candidate_name,
                    "error_rate": error_count / tests_run,
                },
            )
            break

        if i < len(shuffled) - 1:
            time.sleep(0.5)

    return results


def _resolve_candidate_credentials(
    harness: TestHarness,
    provider_credentials: dict[str, dict[str, str]] | None,
) -> dict[str, str] | None:
    """Resolve credentials for a candidate from the provider registry or env vars."""
    credentials: dict[str, str] = {}

    if provider_credentials:
        candidate_key = harness.candidate_name.lower().strip()
        provider_key = harness.provider.lower().strip()

        for registry_key, env_dict in provider_credentials.items():
            norm_key = registry_key.lower().strip()
            if (
                norm_key == candidate_key
                or norm_key == provider_key
                or norm_key in candidate_key
                or candidate_key in norm_key
            ):
                credentials.update(env_dict)
                break

    if harness.auth_env_vars:
        for var_name in harness.auth_env_vars:
            if var_name not in credentials:
                env_val = os.environ.get(var_name)
                if env_val:
                    credentials[var_name] = env_val

    return credentials if credentials else None


# --- Mechanical Evaluation ---

def _normalize_for_comparison(text: str) -> str:
    """Normalize text for mechanical comparison: lowercase, strip, collapse whitespace."""
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _try_parse_number(text: str) -> float | None:
    """Try to extract a number from text (handles $, commas)."""
    cleaned = re.sub(r"[$,\s]", "", text.strip())
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _evaluate_exact_match(
    output: str,
    expected: str,
    criterion: str,
    weight: float,
) -> CriterionScore:
    """Evaluate an exact_match criterion mechanically."""
    norm_output = _normalize_for_comparison(output)
    norm_expected = _normalize_for_comparison(expected)

    quoted = re.findall(r"['\"]([^'\"]+)['\"]", criterion)

    if quoted:
        matches_found = 0
        for q in quoted:
            norm_q = _normalize_for_comparison(q)
            if norm_q in norm_output:
                matches_found += 1
            else:
                q_num = _try_parse_number(q)
                if q_num is not None:
                    output_numbers = re.findall(r"[\d,]+\.?\d*", output)
                    for on in output_numbers:
                        on_num = _try_parse_number(on)
                        if on_num is not None and abs(on_num - q_num) < 0.01:
                            matches_found += 1
                            break

        score = matches_found / len(quoted) if quoted else 0.0
        return CriterionScore(
            criterion=criterion,
            eval_type="exact_match",
            weight=weight,
            score=score,
            passed=score >= 0.5,
            reasoning=f"Found {matches_found}/{len(quoted)} expected values in output",
        )

    if norm_expected in norm_output or norm_output in norm_expected:
        return CriterionScore(
            criterion=criterion,
            eval_type="exact_match",
            weight=weight,
            score=1.0,
            passed=True,
            reasoning="Output contains expected content",
        )

    return CriterionScore(
        criterion=criterion,
        eval_type="exact_match",
        weight=weight,
        score=0.0,
        passed=False,
        reasoning="Expected content not found in output",
    )


def _evaluate_format_compliance(
    output: str,
    criterion: str,
    weight: float,
) -> CriterionScore:
    """Evaluate a format_compliance criterion mechanically."""
    is_valid_json = False
    try:
        json.loads(output)
        is_valid_json = True
    except (json.JSONDecodeError, TypeError):
        json_match = re.search(r"\{[^{}]*\}", output, re.DOTALL)
        if json_match:
            try:
                json.loads(json_match.group())
                is_valid_json = True
            except json.JSONDecodeError:
                pass

    if "json" in criterion.lower() or "valid json" in criterion.lower():
        score = 1.0 if is_valid_json else 0.0
        return CriterionScore(
            criterion=criterion,
            eval_type="format_compliance",
            weight=weight,
            score=score,
            passed=score >= 0.5,
            reasoning="Output is valid JSON" if is_valid_json else "Output is not valid JSON",
        )

    score = 1.0 if output.strip() else 0.0
    return CriterionScore(
        criterion=criterion,
        eval_type="format_compliance",
        weight=weight,
        score=score,
        passed=score >= 0.5,
        reasoning="Output is non-empty" if output.strip() else "Output is empty",
    )


def _evaluate_mechanical(
    output: str,
    expected: str,
    criteria: list[dict],
) -> list[CriterionScore]:
    """Evaluate mechanical criteria (exact_match, format_compliance)."""
    scores = []
    for c in criteria:
        if c["eval_type"] == "exact_match":
            scores.append(_evaluate_exact_match(
                output, expected, c["criterion"], c["weight"]
            ))
        elif c["eval_type"] == "format_compliance":
            scores.append(_evaluate_format_compliance(
                output, c["criterion"], c["weight"]
            ))
    return scores


RAW_RESPONSE_MAX_CHARS = 15000  # Truncate raw API responses for eval. 15K captures all key invoice fields (vendor, line_items, totals) even in verbose responses. Cost: ~$0.01/test at Sonnet rates.

def _build_evaluation_prompt(
    test_results: list[tuple[TestCase, dict, list[dict]]],
    candidate_name: str,
) -> str:
    """Build the user message for LLM judge evaluation.

    Feeds raw API response + ground truth + criteria to the judge.
    Raw responses are truncated to RAW_RESPONSE_MAX_CHARS to control cost.
    """
    parts = [f"Evaluate the following API results for {candidate_name}:\n"]

    for tc, result, criteria in test_results:
        if not criteria:
            continue

        # Use raw_response (the actual API output) instead of formatted "output"
        raw_resp = result.get("raw_response", {})
        if isinstance(raw_resp, dict):
            raw_resp_str = json.dumps(raw_resp, indent=2, default=str)
        else:
            raw_resp_str = str(raw_resp)
        # Truncate to control input token cost
        if len(raw_resp_str) > RAW_RESPONSE_MAX_CHARS:
            raw_resp_str = raw_resp_str[:RAW_RESPONSE_MAX_CHARS] + "\n... (truncated)"

        parts.append(f"\n=== TEST CASE [{tc.id}] ===")
        parts.append(f"Scenario: {tc.scenario}")
        parts.append(f"API success: {result.get('success', False)}")
        if result.get("error"):
            parts.append(f"Error: {result['error'][:200]}")
        parts.append(f"\nGround Truth (expected):\n{tc.expected_output}")
        parts.append(f"\nRaw API Response:\n{raw_resp_str}")
        parts.append("\nCriteria to evaluate:")
        for i, c in enumerate(criteria, 1):
            parts.append(f"  {i}. {c['criterion']} (weight: {c['weight']})")

    return "\n".join(parts)


def _evaluate_with_llm(
    client: anthropic.Anthropic,
    test_results: list[tuple[TestCase, dict, list[dict]]],
    candidate_name: str,
    logger: logging.Logger,
    trace_id: str,
) -> tuple[dict[str, list[CriterionScore]], float]:
    """Batch-evaluate test results using LLM for semantic/subjective criteria."""
    llm_items = [(tc, r, c) for tc, r, c in test_results if c]
    if not llm_items:
        return {}, 0.0

    prompt = _build_evaluation_prompt(llm_items, candidate_name)
    cost = 0.0

    for attempt in range(2):
        try:
            response = client.messages.parse(
                model=AGENT6_EVAL_MODEL,
                max_tokens=AGENT6_EVAL_MAX_TOKENS,
                system=EVALUATION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_format=EvaluationBatchResult,
            )
            cost += _calculate_call_cost(response, AGENT6_EVAL_MODEL)

            logger.info(
                f"LLM evaluation completed for {candidate_name}",
                extra={
                    "operation": "llm_evaluation",
                    "trace_id": trace_id,
                    "candidate_name": candidate_name,
                    "cost_usd": cost,
                    "tokens_in": response.usage.input_tokens,
                    "tokens_out": response.usage.output_tokens,
                },
            )

            parsed: EvaluationBatchResult = response.parsed_output
            result_map: dict[str, list[CriterionScore]] = {}

            for eval_item in parsed.evaluations:
                scores = []
                original_criteria = {}
                for tc, _, criteria in llm_items:
                    if tc.id == eval_item.test_case_id:
                        original_criteria = {c["criterion"]: c for c in criteria}
                        break

                for cs_out in eval_item.criteria_scores:
                    orig = original_criteria.get(cs_out.criterion, {})
                    scores.append(CriterionScore(
                        criterion=cs_out.criterion,
                        eval_type=orig.get("eval_type", "semantic_similarity"),
                        weight=orig.get("weight", 0.5),
                        score=max(0.0, min(1.0, cs_out.score)),
                        passed=cs_out.passed,
                        reasoning=cs_out.reasoning,
                    ))
                result_map[eval_item.test_case_id] = scores

            return result_map, cost

        except anthropic.RateLimitError:
            time.sleep(15)
            continue
        except Exception as e:
            logger.warning(
                f"LLM evaluation error for {candidate_name}: {e}",
                extra={"operation": "llm_evaluation_error", "trace_id": trace_id},
            )
            if attempt == 0:
                time.sleep(5)
                continue
            break

    logger.warning(
        f"LLM evaluation failed after retries for {candidate_name}",
        extra={"operation": "llm_evaluation_failed", "trace_id": trace_id},
    )
    return {}, cost


def _compute_weighted_score(criteria_scores: list[CriterionScore]) -> float:
    """Compute weighted average score from criteria scores."""
    if not criteria_scores:
        return 0.0
    total_weight = sum(cs.weight for cs in criteria_scores)
    if total_weight == 0:
        return 0.0
    weighted_sum = sum(cs.score * cs.weight for cs in criteria_scores)
    return round(weighted_sum / total_weight, 4)


def _compute_aggregate_metrics(
    test_results: list[TestCaseResult],
) -> dict[str, Any]:
    """Compute aggregate metrics from individual test results."""
    total = len(test_results)
    if total == 0:
        return {
            "total_tests": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "tests_errored": 0,
            "tests_skipped": 0,
            "success_rate": 0.0,
            "pass_rate": 0.0,
            "avg_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "total_cost_usd": 0.0,
            "total_tokens": None,
        }

    # Skipped = INCOMPATIBLE or has skip_reason
    tests_skipped = sum(
        1 for r in test_results
        if getattr(r, "skip_reason", None) is not None
    )
    tests_errored = sum(
        1 for r in test_results
        if not r.success and getattr(r, "skip_reason", None) is None
    )
    tests_passed = sum(1 for r in test_results if r.passed)
    tests_failed = total - tests_skipped - tests_errored - tests_passed

    successful_results = [r for r in test_results if r.success]
    latencies = sorted([r.latency_ms for r in successful_results]) if successful_results else [0.0]

    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    p95_idx = max(0, int(len(latencies) * 0.95) - 1)
    p95_latency = latencies[p95_idx] if latencies else 0.0

    total_cost = sum(r.cost_usd or 0.0 for r in test_results)

    total_input_tokens = 0
    total_output_tokens = 0
    has_token_data = False
    for r in test_results:
        if r.tokens_used:
            has_token_data = True
            total_input_tokens += r.tokens_used.get("input", 0)
            total_output_tokens += r.tokens_used.get("output", 0)

    # Compute rates from executed tests only (excluding skipped)
    executed_count = total - tests_skipped
    successful_count = executed_count - tests_errored

    return {
        "total_tests": total,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "tests_errored": tests_errored,
        "tests_skipped": tests_skipped,
        "success_rate": successful_count / executed_count if executed_count > 0 else 0.0,
        "pass_rate": tests_passed / successful_count if successful_count > 0 else 0.0,
        "avg_latency_ms": round(avg_latency, 2),
        "p95_latency_ms": round(p95_latency, 2),
        "total_cost_usd": round(total_cost, 6),
        "total_tokens": (
            {"input": total_input_tokens, "output": total_output_tokens}
            if has_token_data
            else None
        ),
    }


def _stage_test_files(
    test_cases: list[TestCase],
    sandbox_dir: Path,
    logger: logging.Logger,
    trace_id: str,
) -> list[TestCase]:
    """
    Copy test files to the sandbox directory so harnesses can access them.

    For each test case with a non-null test_file_path:
    - Copies the file from original path to {sandbox_dir}/test_files/{filename}
    - Updates the test case's test_file_path to the new sandbox path

    Returns updated test cases (shallow copies with updated paths).
    """
    import shutil

    test_files_dir = sandbox_dir / "test_files"
    staged = []

    for tc in test_cases:
        if tc.test_file_path:
            src = Path(tc.test_file_path)
            if src.exists():
                test_files_dir.mkdir(exist_ok=True)
                dest = test_files_dir / src.name
                # Handle duplicate filenames
                if dest.exists():
                    stem, suffix = dest.stem, dest.suffix
                    dest = test_files_dir / f"{stem}_{tc.id}{suffix}"
                try:
                    shutil.copy2(str(src), str(dest))
                    # Create a shallow copy with updated path
                    tc_dict = tc.model_dump()
                    tc_dict["test_file_path"] = str(dest.resolve())
                    staged.append(TestCase(**tc_dict))
                    logger.info(
                        f"Staged test file: {src.name} -> {dest}",
                        extra={"operation": "stage_test_file", "trace_id": trace_id},
                    )
                except (OSError, shutil.Error) as e:
                    logger.warning(
                        f"Failed to stage test file {src}: {e}",
                        extra={"operation": "stage_test_file_error", "trace_id": trace_id},
                    )
                    staged.append(tc)  # Keep original (harness will handle missing file)
            else:
                logger.warning(
                    f"Test file not found: {src}",
                    extra={"operation": "stage_test_file_missing", "trace_id": trace_id},
                )
                staged.append(tc)
        else:
            staged.append(tc)

    return staged


# ============================================================================
# [CORE] Main function — this is the entry point
# ============================================================================
#
# THE CORE LOGIC (marked with ★ below):
#   1. Create sandbox directories for each candidate
#   2. ThreadPoolExecutor → parallel _build_single_harness() per candidate
#   3. Collect TestHarness / FailedHarness results
#   4. Return Agent5Result (no structuring LLM call needed)
#
# N API call chains in parallel (one multi-turn chain per candidate).
# Each chain is isolated: own conversation, own sandbox, own budget.
#
# ============================================================================

def run_implement_test_env_agent(input_data: Agent5Input) -> Agent5Result:
    """
    Run Agent 5. Takes validated candidates from Agent 4 and builds a test
    harness for each one in parallel.

    Each candidate gets an autonomous builder agent (multi-turn tool-use loop)
    that reads API docs, writes harness code, tests it, and fixes errors.

    Returns Agent5Result with successfully built harnesses and failures.
    """
    # ★ CORE LINE 1: Create the API client
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # [logging]
    logger = get_logger("agent_5_implement")

    # Select top candidates, prioritizing those with credentials.
    # Candidates with credentials can be fully validated (smoke + live + integration).
    # Candidates without credentials can only pass smoke test — less valuable.
    # Within each group, sort by user-fit score (relevance_score).
    all_candidates = input_data.validated_candidates

    # Determine which candidates have credentials
    cred_providers = set()
    if input_data.provider_credentials:
        cred_providers = set(input_data.provider_credentials.keys())

    def _has_credentials(c: ScreenedCandidate) -> bool:
        if not cred_providers:
            return False
        norm_provider = _normalize(c.provider)
        norm_name = _normalize(c.name)
        return norm_provider in cred_providers or norm_name in cred_providers

    # Sort: credentials first (True > False), then by score descending
    sorted_candidates = sorted(
        all_candidates,
        key=lambda c: (_has_credentials(c), c.relevance_score),
        reverse=True,
    )
    candidates = sorted_candidates[:AGENT5_MAX_CANDIDATES]

    if len(all_candidates) > len(candidates):
        logger.info(
            f"Selected top {len(candidates)} of {len(all_candidates)} candidates by user-fit score",
            extra={
                "operation": "candidate_selection",
                "trace_id": input_data.trace_id,
                "selected": [c.name for c in candidates],
                "dropped": [c.name for c in all_candidates if c not in candidates],
            },
        )

    logger.info("Agent 5 started", extra={
        "operation": "agent_start",
        "trace_id": input_data.trace_id,
        "candidate_count": len(candidates),
        "max_parallel": AGENT5_MAX_PARALLEL,
    })

    # ======================================================================
    # Create sandbox directories
    # ======================================================================
    harness_base = Path("runs") / input_data.trace_id / "harnesses"
    harness_base.mkdir(parents=True, exist_ok=True)

    # ======================================================================
    # PARALLEL BUILD: One thread per candidate
    # ======================================================================
    # Each candidate gets its own thread, its own conversation context, and
    # its own sandbox directory. No shared state, no token accumulation.
    # ======================================================================

    # ★ CORE: Launch all harness builds in parallel
    results_by_index: dict[int, TestHarness | FailedHarness] = {}

    with ThreadPoolExecutor(max_workers=min(AGENT5_MAX_PARALLEL, len(candidates))) as executor:
        future_to_index = {}
        for i, candidate in enumerate(candidates):
            slug = _candidate_slug(candidate.name)
            sandbox_dir = harness_base / slug
            sandbox_dir.mkdir(parents=True, exist_ok=True)

            logger.info(f"Submitting build for: {candidate.name}", extra={
                "operation": "harness_build_submit",
                "trace_id": input_data.trace_id,
                "candidate_name": candidate.name,
                "candidate_index": i + 1,
                "sandbox_dir": str(sandbox_dir),
            })

            future = executor.submit(
                _build_single_harness, client, candidate, input_data, sandbox_dir, logger,
            )
            future_to_index[future] = i

        # Collect results as they complete
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            candidate_name = candidates[idx].name
            try:
                results_by_index[idx] = future.result()
            except Exception as e:
                # Unexpected exception from thread — graceful degradation
                logger.error(f"Unexpected error building {candidate_name}", extra={
                    "operation": "harness_build_thread_error",
                    "trace_id": input_data.trace_id,
                    "error": str(e), "error_type": type(e).__name__,
                })
                results_by_index[idx] = FailedHarness(
                    candidate_name=candidate_name,
                    provider=candidates[idx].provider,
                    failure_reason=f"Unexpected error: {e}",
                    failure_category="unknown",
                    partial_code=None,
                    turns_attempted=0,
                )

    # ======================================================================
    # Assemble Agent5Result
    # ======================================================================
    harnesses = []
    failed = []
    total_cost = 0.0

    for i in range(len(candidates)):
        result = results_by_index[i]
        if isinstance(result, TestHarness):
            harnesses.append(result)
            total_cost += result.build_cost_usd
        else:
            failed.append(result)

    # Build summary
    summary_parts = [
        f"Built {len(harnesses)}/{len(candidates)} harnesses successfully."
    ]
    if failed:
        failure_categories = {}
        for f in failed:
            cat = f.failure_category
            failure_categories[cat] = failure_categories.get(cat, 0) + 1
        cats = ", ".join(f"{v}x {k}" for k, v in failure_categories.items())
        summary_parts.append(f"{len(failed)} failed ({cats}).")
    summary_parts.append(f"Total build cost: ${total_cost:.2f}.")

    # ======================================================================
    # Post-build: Mechanical Test Execution for successful harnesses
    # ======================================================================
    # Mechanical test execution — runs ALL test cases through harness.run().
    # The agent already built and verified the harness. This step
    # mechanically runs ALL test cases through harness.run() and evaluates.
    # No LLM needed for execution — only for quality evaluation (1 call/candidate).
    # ======================================================================
    candidate_runs = []
    failed_test_runs = []
    total_test_cost = 0.0
    test_cases = input_data.test_cases.test_cases

    # Only run tests if we have real harnesses with sandbox directories on disk
    # Check that harness_dir is a real path string (not a mock) and exists
    harnesses_with_sandboxes = [
        h for h in harnesses
        if isinstance(h.harness_dir, str) and Path(h.harness_dir).exists()
        and (Path(h.harness_dir) / "harness.py").exists()
    ]
    if harnesses_with_sandboxes and test_cases:

        logger.info(f"Starting test execution for {len(harnesses_with_sandboxes)} harnesses, {len(test_cases)} test cases", extra={
            "operation": "test_execution_start",
            "trace_id": input_data.trace_id,
        })

        def _run_tests_for_candidate(harness):
            """Execute all tests for one candidate. Thread-safe — each candidate
            has its own sandbox, credentials, and API provider."""
            sandbox_dir = Path(harness.harness_dir)
            staged_test_cases = _stage_test_files(test_cases, sandbox_dir, logger, input_data.trace_id)
            creds = _resolve_candidate_credentials(
                harness, input_data.provider_credentials
            )

            raw_results = _execute_all_tests(
                sandbox_dir, staged_test_cases, harness, creds, logger, input_data.trace_id,
            )

            eval_items = []
            test_case_results = []

            for tc, result in raw_results:
                adapted = _adapt_test_input(tc, harness)

                if not result["success"]:
                    error_msg = result.get("error") or ""
                    is_incompatible = "INCOMPATIBLE" in error_msg.upper()
                    test_case_results.append(TestCaseResult(
                        test_case_id=tc.id,
                        sub_task_ref=tc.sub_task_ref,
                        input_sent=adapted,
                        output_received=str(result.get("output", "")),
                        raw_response=result.get("raw_response", {}),
                        latency_ms=result.get("latency_ms", 0.0),
                        tokens_used=result.get("tokens_used"),
                        cost_usd=result.get("cost_usd"),
                        success=False,
                        error=result.get("error"),
                        skip_reason=error_msg if is_incompatible else None,
                        criteria_scores=[],
                        weighted_score=0.0,
                        passed=False,
                    ))
                    continue

                # ALL criteria go to LLM judge (no mechanical split).
                # The LLM compares raw API response against ground truth.
                all_criteria = [
                    {"criterion": jc.criterion, "eval_type": jc.eval_type, "weight": jc.weight}
                    for jc in tc.judgement_criteria
                ]
                eval_items.append((tc, result, all_criteria))

                test_case_results.append(TestCaseResult(
                    test_case_id=tc.id,
                    sub_task_ref=tc.sub_task_ref,
                    input_sent=adapted,
                    output_received=str(result.get("output", "")),
                    raw_response=result.get("raw_response", {}),
                    latency_ms=result.get("latency_ms", 0.0),
                    tokens_used=result.get("tokens_used"),
                    cost_usd=result.get("cost_usd"),
                    success=True,
                    error=None,
                    skip_reason=None,
                    criteria_scores=[],  # Filled by LLM judge below
                    weighted_score=0.0,
                    passed=False,
                ))

            # LLM judge evaluates ALL criteria by comparing raw API response
            # against Agent 3F's ground truth. No mechanical eval needed.
            eval_cost = 0.0
            if eval_items:
                llm_scores_map, eval_cost = _evaluate_with_llm(
                    client, eval_items, harness.candidate_name, logger, input_data.trace_id,
                )
                for tcr in test_case_results:
                    if tcr.success and tcr.test_case_id in llm_scores_map:
                        tcr.criteria_scores = llm_scores_map[tcr.test_case_id]
                        tcr.weighted_score = _compute_weighted_score(tcr.criteria_scores)
                        tcr.passed = tcr.weighted_score >= AGENT6_PASS_THRESHOLD

            metrics = _compute_aggregate_metrics(test_case_results)

            incompatible_ids = [
                tcr.test_case_id for tcr in test_case_results
                if tcr.skip_reason is not None
            ]

            run = CandidateTestRun(
                candidate_name=harness.candidate_name,
                provider=harness.provider,
                harness_dir=str(sandbox_dir),
                status="completed",
                test_results=test_case_results,
                total_tests=metrics["total_tests"],
                tests_passed=metrics["tests_passed"],
                tests_failed=metrics["tests_failed"],
                tests_errored=metrics["tests_errored"],
                tests_skipped=metrics["tests_skipped"],
                success_rate=metrics["success_rate"],
                pass_rate=metrics["pass_rate"],
                avg_latency_ms=metrics["avg_latency_ms"],
                p95_latency_ms=metrics["p95_latency_ms"],
                total_cost_usd=metrics["total_cost_usd"],
                total_tokens=metrics["total_tokens"],
                evaluation_cost_usd=eval_cost,
                incompatible_test_ids=incompatible_ids,
            )

            logger.info(
                f"Test execution complete for {harness.candidate_name}: "
                f"{metrics['tests_passed']}/{metrics['total_tests']} passed",
                extra={
                    "operation": "test_execution_complete",
                    "trace_id": input_data.trace_id,
                    "candidate_name": harness.candidate_name,
                    **metrics,
                },
            )

            return run, metrics["total_cost_usd"] + eval_cost

        # Run test execution in parallel — each candidate hits a different API
        # provider, so no cross-candidate rate limit concerns. Same pattern as
        # the parallel harness builds above.
        with ThreadPoolExecutor(max_workers=len(harnesses_with_sandboxes)) as executor:
            futures = {
                executor.submit(_run_tests_for_candidate, h): h
                for h in harnesses_with_sandboxes
            }
            for future in as_completed(futures):
                harness = futures[future]
                try:
                    run, cost = future.result()
                    candidate_runs.append(run)
                    total_test_cost += cost
                except Exception as e:
                    logger.error(f"Test execution error for {harness.candidate_name}: {e}", extra={
                        "operation": "test_execution_error",
                        "trace_id": input_data.trace_id,
                    })
                    failed_test_runs.append(FailedCandidateRun(
                        candidate_name=harness.candidate_name,
                        provider=harness.provider,
                        failure_reason=f"Test execution error: {str(e)[:300]}",
                        error_rate=1.0,
                        tests_attempted=0,
                        tests_errored=0,
                        sample_errors=[str(e)[:200]],
                        recovery_attempted=False,
                    ))

    # Build test execution summary
    test_summary = ""
    if candidate_runs:
        avg_pass = sum(r.pass_rate for r in candidate_runs) / len(candidate_runs)
        try:
            test_summary = (
                f"{len(candidate_runs)}/{len(harnesses_with_sandboxes)} candidates tested. "
                f"Avg pass rate: {avg_pass:.0%}. Test cost: ${float(total_test_cost):.2f}."
            )
        except (TypeError, ValueError):
            test_summary = f"{len(candidate_runs)} candidates tested."

    result = Agent5Result(
        harnesses=harnesses,
        failed_harnesses=failed,
        total_candidates_attempted=len(candidates),
        total_build_cost_usd=round(total_cost, 4),
        build_summary=" ".join(summary_parts),
        candidate_runs=candidate_runs,
        failed_test_runs=failed_test_runs,
        total_test_cases=len(test_cases),
        total_test_cost_usd=round(total_test_cost, 4),
        test_execution_summary=test_summary,
    )

    logger.info("Agent 5 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "harnesses_built": len(harnesses),
        "harnesses_failed": len(failed),
        "total_cost": total_cost,
        "tests_run": len(candidate_runs),
        "test_cost": total_test_cost,
    })

    return result
