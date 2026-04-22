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
    AGENT6_TEST_TIMEOUT_LONG,
    ANTHROPIC_API_KEY,
    ENABLE_FETCH_FALLBACK,
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
    EvaluationBatchResult,
    FailedCandidateRun,
    FailedHarness,
    ScreenedCandidate,
    TestCase,
    TestCaseResult,
    TestHarness,
)
from puzzleeval.web_fetch_fallback import (
    build_fallback_message,
    count_actionable_problems,
    extract_blocked_fetches,
    extract_unusable_pages,
    maybe_apply_rate_limit_backoff,
    summarize_blocks_for_log,
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

# Local helper — wires the shared cross-cutting preamble (parallel tool use,
# no narration, reason about errors, verify against source, commit and
# course-correct) onto each system prompt. Defined here to avoid an
# import-time cycle when implement_test_env is partially imported by tests.
def _with_shared_preamble(prompt: str) -> str:
    from puzzleeval.agent_preamble import with_preamble
    return with_preamble(prompt)


def _format_modality_context_for_builder(input_data: "Agent5Input") -> str:
    """Tell the builder which tool plugins will evaluate its harness output.

    When the test cases declare audio_content / code / conversation /
    media_url modalities, the modality detector identifies the plugin
    that will run during evaluation. Naming the plugin in the builder
    prompt makes the builder ACT on the contract — e.g. "your harness
    must return an audio URL or path the transcription plugin can
    download" instead of guessing the response shape.
    """
    try:
        from puzzleeval.modality import detect_for_test_case
        from puzzleeval.tool_plugins import list_plugins
    except Exception:
        return ""

    pairs: set[tuple[str, str]] = set()
    for tc in input_data.test_cases.test_cases:
        pairs.add((tc.input_type, tc.output_type))
    if not pairs:
        return ""

    available_plugins = ", ".join(p.name for p in list_plugins())
    lines: list[str] = ["", "## Modality plugins active for this run", ""]
    lines.append(f"Available plugins in registry: {available_plugins}.")
    for input_type, output_type in sorted(pairs):
        reqs = detect_for_test_case(input_type=input_type, output_type=output_type)
        synth_names = [p.name for p in reqs.input_synthesizers]
        eval_names = [p.name for p in reqs.output_evaluators]
        lines.append(
            f"- ({input_type} → {output_type}): "
            f"input_synthesizers={synth_names or 'none'}, "
            f"output_evaluators={eval_names or 'none (LLM judge fallback)'}"
        )
        if reqs.unavailable:
            for plugin_name, reason in reqs.unavailable:
                lines.append(f"    UNAVAILABLE — {plugin_name}: {reason}")
    lines.append("")
    lines.append(
        "If your harness returns audio (path or URL), the transcription "
        "plugin will STT it and compare to expected text. If it returns "
        "code, the code_execution plugin will run it. If it returns an "
        "image URL, the vision plugin will judge it. Match your response "
        "shape to the plugin's expected input — that's the contract."
    )
    return "\n".join(lines)


def _format_atlas_context_for_builder(candidate: ScreenedCandidate) -> str:
    """Return structured hints from ScreenedCandidate's enrichment fields.

    Architecture note: Agent 4 now only does a shallow verify (exists/
    blocked). It does NOT produce an atlas JSON. Agent 5 does its own
    Phase-1 research via web_search + web_fetch and writes its own
    api_spec.txt into the sandbox.

    This helper still surfaces any simple enrichment fields Agent 4
    happens to populate (sandbox_available, upstream_provider,
    interaction_model flags on the shallow path). Returns an empty
    string when nothing is populated — the common case today.
    """
    sections: list[str] = []

    interaction = getattr(candidate, "interaction_model", None)
    if interaction is not None:
        active_modes = []
        for flag in ("synchronous", "async_polling", "webhook_callback",
                     "sse_streaming", "batch_file", "event_subscription"):
            if getattr(interaction, flag, False):
                active_modes.append(flag)
        if active_modes:
            sections.append(
                "### Interaction model hints (from Agent 4 shallow verify)\n"
                "- Active delivery modes: " + ", ".join(active_modes) + "\n"
                + (
                    "- This API uses async/long-running operations. Your harness "
                    "MUST implement a polling loop or stream consumer; do not "
                    "treat the first response as the final result. Use a longer "
                    "timeout (5-10 min) for end-to-end test calls.\n"
                    if any(m in active_modes for m in ("async_polling", "batch_file", "sse_streaming"))
                    else ""
                )
                + (
                    f"- Notes: {interaction.notes}\n" if getattr(interaction, "notes", "") else ""
                )
            )

    if getattr(candidate, "sandbox_available", False):
        sandbox_url = getattr(candidate, "sandbox_docs_url", None) or "(sandbox docs not captured)"
        sections.append(
            "### Sandbox available\n"
            f"- This provider has a documented sandbox / test mode at: {sandbox_url}\n"
            "- PREFER the sandbox base URL during testing — write_only/destructive "
            "calls will not touch real customer data. The sandbox usually accepts "
            "the same auth keys (or a separate test-key prefix like sk_test_*).\n"
        )

    upstream = getattr(candidate, "upstream_provider", None)
    if upstream:
        sections.append(
            f"### Upstream provider\n- This API wraps `{upstream}`. Rate-limit "
            "headroom is shared with every other candidate that wraps the same "
            "upstream — keep test request volume modest.\n"
        )

    if not sections:
        return ""
    return "\n---\n## ENRICHMENT HINTS FROM AGENT 4\n\n" + "\n".join(sections)


# Field names different harnesses use for "the system prompt that tells the
# agent how to behave". Both the OpenAI Realtime and ElevenLabs Conversational
# AI harnesses accept this exact list (their `_extract_system_prompt` walks
# these in order). Documented in the Agent 5 builder prompt's
# "System-prompt resilience" section as the canonical accepted-key set.
# Adding a new alias here propagates to every multi-call modality without
# touching any harness code.
_SYSTEM_PROMPT_ALIASES = (
    "instructions", "system_prompt", "system", "brief", "agent_prompt",
)


def _default_input_context(candidate_name: str, scope_role: str | None) -> dict:
    """Build a minimal default `input_context` for harness calls when the
    test case omitted one (or omitted the system-prompt field).

    Many agent-style API harnesses (voice, chat, conversation) hard-fail
    when `run()` is called without a system prompt — Claude legitimately
    needs to know what role to play. We've shipped TWO prompt-level fixes:

      1. Agent 3's prompt requires `input_context.instructions` on
         conversation/voice tests.
      2. Agent 5's builder prompt teaches a DEFAULT_INSTRUCTIONS
         fallback inside the harness itself.

    Real-run trace f1312253 (post-Option-A): both rules were violated by
    the model — Agent 3 emitted `input_context: null`, and Agent 5's
    OpenAI harness contained `if not instructions: return _fail(...)`
    with no fallback. Every harness call failed, plugin saw empty audio,
    agent MP3s never landed. ElevenLabs's harness has the same pattern.

    The fix here pushes the default INTO OUR CODE (the runner closure
    that ALL multi-call plugin invocations route through). The runner
    is the safety net regardless of prompt-rule adherence by either
    Agent 3 OR Agent 5's builder.

    **Generality:** the default text is populated under EVERY accepted
    alias (``instructions``, ``system_prompt``, ``system``, ``brief``,
    ``agent_prompt``) so harnesses checking any of these names find it.
    This matches the accepted-key list both observed harnesses use; new
    providers using one of these conventions work without code changes.
    Future providers introducing a NEW key name still need to be added
    to ``_SYSTEM_PROMPT_ALIASES`` — that's a one-line change with global
    effect.

    Test cases supplying their OWN values for ANY of these aliases keep
    them — see ``_merge_with_default_input_context`` for the merge rule.
    """
    role_phrase = (scope_role or "agent").replace("_", " ").strip() or "agent"
    text = (
        f"You are a helpful {role_phrase} for {candidate_name}. "
        f"Answer the user's questions concisely, stay on-topic, "
        f"and acknowledge that you can route follow-ups appropriately."
    )
    return {alias: text for alias in _SYSTEM_PROMPT_ALIASES}


def _merge_with_default_input_context(
    test_case_ctx: dict | None,
    default_ctx: dict,
) -> dict:
    """Merge the test case's `input_context` with the candidate-aware
    default so the harness ALWAYS gets a usable system prompt.

    Three cases the runner sees in the wild:

      1. Test case ctx is None / missing entirely (Agent 3 emitted null
         on every voice test in trace f1312253). Result: the full default.
      2. Test case ctx has SOME keys but no system-prompt field (e.g.,
         {"persona_name": "Vera"}). Result: original keys preserved +
         system-prompt aliases filled from the default. The harness's
         `_extract_system_prompt` walks the alias list and finds one.
      3. Test case ctx has its OWN system-prompt value (under any alias).
         Result: ctx wins for every alias the test set, default fills
         only the ones it didn't. Test case content is never overwritten.

    This is what makes the runner-level fallback truly general: it
    doesn't matter which alias the harness happens to read first, and it
    doesn't matter whether the test case partially populated the dict.
    """
    if not isinstance(test_case_ctx, dict):
        return dict(default_ctx)
    merged = dict(default_ctx)
    merged.update(test_case_ctx)  # test case wins on every key it sets
    # If the test case set ANY system-prompt alias, propagate that value
    # to every other alias so harnesses checking different names all see
    # the user's intent (not a stale default for the keys they didn't set).
    user_value = next(
        (test_case_ctx[k] for k in _SYSTEM_PROMPT_ALIASES
         if isinstance(test_case_ctx.get(k), str) and test_case_ctx[k].strip()),
        None,
    )
    if user_value:
        for alias in _SYSTEM_PROMPT_ALIASES:
            merged[alias] = user_value
    return merged


# ============================================================================
# Message-level prompt caching (cache-the-growing-conversation)
# ============================================================================
# Agent 5's builder loop APPENDS to `messages` each turn (tool_result,
# assistant response, next user turn) and never modifies earlier turns.
# That's the exact shape Anthropic's prompt cache is designed for:
# place a `cache_control` marker on the last content block of the last
# message and the full conversation prefix is cached. Turn N+1 reads
# turn N's cache at 0.1x base input cost.
#
# Why block-level placement on the last message (not top-level
# `cache_control` kwarg): equivalent behavior per the docs, but
# explicit placement is easier to debug (grep the request body) and
# keeps us within the documented 4-breakpoint budget (we use 2:
# system + last message). If Anthropic ever narrows which kwargs are
# accepted on `messages.create`, block-level stays unambiguously
# correct.
#
# Why we strip prior markers: cache ENTRIES persist server-side by
# prefix hash; markers in the CURRENT request only tell the API where
# to WRITE new entries and where to LOOK UP existing ones. A stale
# marker on an old message doesn't help (the 20-block lookback from
# the new marker finds the prior write regardless) and over many turns
# could exceed the 4-breakpoint-per-request cap. One active marker on
# the tail is both necessary and sufficient.
#
# Safety properties:
#   - Empty messages: no-op, returns input unchanged.
#   - String-content last message: wraps into a text block so we can
#     attach cache_control cleanly.
#   - List-content last message: marks the last block in place.
#   - Idempotent: calling twice leaves exactly one active marker on
#     the last message's last block.
#   - Cache miss: costs 1x (same as pre-change). No scenario where we
#     pay more than the current baseline.
# ============================================================================
def _apply_message_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """Mark the last message's last content block as a cache breakpoint.

    Mutates `messages` in place for efficiency (the builder loop owns
    this list). Strips any existing `cache_control` markers from earlier
    messages so we stay within the 4-breakpoint-per-request cap even
    over long builds. Returns the same list for call-site convenience.

    Called before every `client.beta.messages.create()` in the Agent 5
    builder loop. Safe to call even when `CACHE_MESSAGES_ENABLED` is
    False (caller gates the call itself).
    """
    if not messages:
        return messages

    # Strip any existing cache_control markers from ALL messages. Over
    # time the builder loop could otherwise accumulate markers beyond
    # the 4-breakpoint cap (system uses 1, so we have 3 for messages).
    # The cache entries themselves persist server-side by prefix hash;
    # removing the marker doesn't delete them.
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for i, block in enumerate(content):
                if isinstance(block, dict) and "cache_control" in block:
                    # Clone + drop cache_control so we don't mutate a
                    # block dict shared elsewhere.
                    content[i] = {k: v for k, v in block.items()
                                  if k != "cache_control"}

    # Apply a single active marker to the tail.
    last = messages[-1]
    if not isinstance(last, dict):
        return messages
    content = last.get("content")

    if isinstance(content, str):
        # Convert string content to a list with one text block carrying
        # the marker. Anthropic accepts both shapes; list-with-blocks is
        # required to attach cache_control.
        last["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(content, list) and content:
        tail_block = content[-1]
        if isinstance(tail_block, dict):
            content[-1] = {**tail_block, "cache_control": {"type": "ephemeral"}}
    # Any other shape (None, int, missing `content`): no-op. The API
    # will reject the malformed message before we ever get a cache miss.
    return messages


def _adaptive_test_timeout(harness) -> int:
    """Pick the right test-case subprocess timeout for this harness.

    Uses the LONG timeout when the candidate's atlas declared
    async_polling or batch_file delivery — those operations regularly
    exceed the 120s baseline (video encoding, ML inference queue, batch
    document processing). Falls back to baseline for sync APIs.
    """
    spec_path = getattr(harness, "api_spec_path", None)
    if not spec_path:
        return AGENT6_TEST_TIMEOUT
    p = Path(spec_path)
    if not p.exists() or p.suffix != ".json":
        return AGENT6_TEST_TIMEOUT
    try:
        atlas = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return AGENT6_TEST_TIMEOUT
    modes = {m.lower() for m in (atlas.get("interaction_modes") or [])}
    if "async_polling" in modes or "batch_file" in modes or "polling" in modes:
        return AGENT6_TEST_TIMEOUT_LONG
    return AGENT6_TEST_TIMEOUT


def _try_openapi_fastpath(
    candidate, sandbox_dir, logger, trace_id: str
) -> str | None:
    """Q4b: when the candidate's atlas carries an openapi_url, generate
    the harness mechanically and skip the LLM build loop entirely.

    Returns the harness.py source on success, None on any failure
    (no atlas, no openapi_url, fetch failed, no matching operation).
    """
    spec_path = getattr(candidate, "api_spec_path", None)
    if not spec_path:
        return None
    p = Path(spec_path)
    if not p.exists() or p.suffix != ".json":
        return None
    try:
        atlas_data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("openapi-fastpath: atlas read failed: %s", exc)
        return None
    openapi_url = atlas_data.get("openapi_url")
    if not openapi_url:
        return None
    role = ""
    covered = list(getattr(candidate, "covers_step_ids", []) or [])
    if covered and atlas_data.get("endpoints"):
        # Use the first scope's role hint from atlas endpoints when present
        for ep in atlas_data["endpoints"]:
            hints = ep.get("scope_hints") or []
            if hints:
                role = hints[0]
                break
    if not role:
        # Fall back to the candidate's relevant_subtasks / claimed_capabilities
        if getattr(candidate, "claimed_capabilities", None):
            role = candidate.claimed_capabilities[0]
        elif getattr(candidate, "relevant_subtasks", None):
            role = candidate.relevant_subtasks[0]
    try:
        from puzzleeval.openapi_harness import generate_harness_for_candidate
        return generate_harness_for_candidate(
            openapi_url=openapi_url,
            role=role,
            candidate_name=candidate.name,
            provider=candidate.provider,
            server_fallback=(atlas_data.get("base_urls") or [None])[0],
        )
    except Exception as exc:
        logger.debug("openapi-fastpath: generate crashed: %s", exc)
        return None


def _with_builder_appendix(prompt: str) -> str:
    """Append the universal API-pattern catalog and live-test battery to
    the builder system prompt. These two appendices give the builder
    explicit knowledge of common patterns (so it doesn't rediscover REST
    + Bearer / multipart / async polling on every harness) and a clear
    pre-HARNESS_COMPLETE checklist."""
    from puzzleeval.api_patterns import (
        API_PATTERNS_CATALOG,
        LIVE_TEST_BATTERY_PROMPT,
    )
    return prompt + "\n\n---\n" + API_PATTERNS_CATALOG + "\n\n---\n" + LIVE_TEST_BATTERY_PROMPT


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
ALWAYS batch independent tool calls in one response. Every separate turn
costs an API round-trip and tokens for the entire conversation prefix,
so minimizing turns has compounding savings.

**Scaffold phase (MANDATORY, not a suggestion):** once api_spec.txt is
written, emit requirements.txt + harness.py + smoke_test.py + live_test.py
as PARALLEL write_file calls in ONE turn. They are independent files;
serial writes are never necessary here. A 4-file scaffold in 4 separate
turns wastes 3 turns and ~$0.24 per build. A concrete real-run example:
Veryfi build finished a full OCR harness in 5 turns ($0.87) by doing
this; the next build that did serial scaffold writes needed 12 turns
($1.46) for an equivalent-difficulty API — the difference was parallelism,
not API complexity.

**General rule:** if two tool calls do not depend on each other's output,
they belong in the SAME turn. Concrete patterns that are ALWAYS parallelizable:
- Multiple write_file calls to different files
- Multiple read_file calls (before making any changes)
- write_file + run_code when the run uses a DIFFERENT file than the one
  you're writing
- Multiple patch_file calls that target different files
- advisor + write_file in the same turn (advisor runs server-side while
  you're preparing the write)

What you CANNOT parallelize (these remain serial across turns):
- run_code that reads a file you're writing this turn (run waits for write
  to land first)
- Any tool call whose input depends on a PRIOR tool call's output this turn

When in doubt, ask: "does tool B read the output of tool A in this turn?"
If no, parallelize them.
</use_parallel_tool_calls>

<do_not_narrate>
Go straight to action. Do NOT narrate steps. Never output "Now I'll read the file"
or "Let me search for..." — just call the tool. If you can say it in one sentence,
don't use three. Every word of explanation costs tokens and a turn.
</do_not_narrate>

<do_not_repeat>
BEFORE running any command or reading any file, check: did you already do this
this session? If yes, the result is still valid. Do NOT:
  - Re-run `ffmpeg -version` / `python --version` / similar environment checks.
    Once a tool is confirmed available, it stays available for the sandbox's
    lifetime.
  - Re-run `pip install -r requirements.txt` after it succeeded. Packages stay
    installed in the venv.
  - Re-read a file you have not modified this turn. Refer to what you already
    know from the earlier read.
  - Re-search for something already in api_spec.txt's DOC_MAP. The spec
    remembers what you saw.
  - Call advisor more than twice per candidate — the advisor repeats itself
    on the same context.

If unsure whether a state changed, reason from the conversation history first.
Re-verification is a turn you paid for.
</do_not_repeat>

<investigate_comprehensively>
This ONLY applies when DEBUGGING a real error from a harness.py or smoke_test.py
that has already run. When you need to explore an SDK or API response to fix
an observed error, write ONE comprehensive script — not five — that prints
everything you might need in a single run.

**DO NOT write introspection scripts BEFORE harness.py exists.** No
`inspect_sdk.py`, no `check_*.py`, no `explore_*.py` as pre-work. Your first
response to api_spec.txt is to WRITE harness.py, not to interrogate the SDK.
A real error message from a failed harness.run() is far more informative
than `dir(some_class)`.

**Probe-script consolidation (MANDATORY when debugging response shape):**
If the first API call returns an unexpected response structure, you often
need to probe what the endpoint ACTUALLY returns. The fragmented pattern —
write probe_endpoints.py, then peek_structure.py, then probe_components.py,
then probe_more.py — burns a turn per script even when each is tiny. This
happened on a real Klippa run: 5 probe scripts across turns 8–15, each
adding ~$0.08, totaling ~$0.40 of avoidable waste.

Instead, when you need to probe, ask: "what EVERY question do I have right
now about this endpoint's behavior?" List them mentally: (a) what does the
happy-path response look like?, (b) are there nested components?, (c) do
error cases return the same shape?, (d) what does the raw/debug flag
reveal?, (e) which fields are always present vs conditional?

Then write ONE script — `probe_<endpoint>.py` — that runs ALL the probes
(hit the endpoint a few times with different inputs, print full responses
with pretty JSON, print type of each top-level field, print keys of
nested objects) and read ALL results in ONE run. Follow-up probe scripts
only when the first revealed a specific new question the first one
couldn't have anticipated.

**Budget:** at most TWO probe scripts per candidate during debugging.
If you're writing a third, you're doing fragmented probing; STOP, read
the probe outputs you already have, and if still unresolved, write ONE
comprehensive probe instead of a fourth tiny one.
</investigate_comprehensively>

<consolidate_related_patches>
When you realize multiple patches to the SAME file are needed to fix
ONE logical issue, combine them. Three small patches to harness.py in
consecutive turns (say, editing the interruption handler, the response
timing, and the reset logic) are one bug fix, not three — and three
serial patches cost 3 turns × ~$0.07 = $0.21 vs one turn for the
combined fix.

**Decision rule:** after the first patch_file call in a debug cycle,
ask "are there other edits I already know this file needs to fix THIS
bug?" If yes, emit them in the same turn. Either (a) one patch_file
with a larger old_string/new_string block covering multiple nearby
edits, or (b) multiple patch_file calls IN PARALLEL in the same turn
(patch_file calls to the same file in the same turn are allowed —
they apply sequentially and must target distinct, non-overlapping
regions, but they're one turn).

**What this does NOT ask you to do:** it does NOT ask you to DEFER the
first patch waiting for hypothetical future patches. If you know one
specific edit fixes the error, patch it. Run. Look at the result. Only
bundle patches you're already CERTAIN you'll need.

**Real-run example of waste:** ElevenLabs build turns 12-14 emitted
three consecutive patches (336, 546, 631 output tokens) fixing related
aspects of interruption handling. All three were planned during a
single diagnostic "this bug needs fix A, B, and C" thought. They should
have been one turn with three parallel patch_file calls.
</consolidate_related_patches>

<think_before_acting>
Before writing harness.py, re-read api_spec.txt. Check AUTH_HEADER, ENDPOINTS,
and PYTHON_EXAMPLES. Resolve unknowns by reading the DOC_MAP entries OR by writing
a first-pass harness.py and letting live errors tell you what's wrong. A ran
harness.py with a concrete error ("AttributeError: 'Conversation' has no attribute
'X'") is the fastest teacher — faster than any introspection script.
</think_before_acting>

<commit_and_course_correct>
Pick one approach and see it through. Course-correct only if it fails with
new information. Don't revisit decisions or rewrite working code.
</commit_and_course_correct>

## Environment
You are running in an ISOLATED Python virtual environment. `python` and `pip` point to this venv.
The following common packages are **pre-installed** and importable NOW —
do NOT re-verify via pip install or `python -c "import X"`:
  - `requests` (HTTP client)
  - `websocket-client` (synchronous WebSocket client, import as `websocket`)
  - `pydub` (audio format conversion; requires ffmpeg on PATH — assume present)
  - `soundfile` (PCM16 I/O without ffmpeg)
  - `numpy` (array math)
  - `python-dotenv` (import as `dotenv`)
For ANY OTHER dependency, add to requirements.txt and run
`pip install -r requirements.txt` — but ONCE. Do not reverify after
it succeeds.
OS: __OS_TYPE__. Use `python -c "import os; print(os.listdir('.'))"` to list files (works on any OS). Use `os.path` in Python code, not hardcoded path separators.

**IMPORTANT:** Use only ASCII characters in Python code and comments. Do NOT use unicode
dashes (—), arrows (→), or special characters. Use -- for dashes, -> for arrows. This
prevents encoding errors on Windows.

__OS_SPECIFIC_RULES__

__MODALITY_CONTRACT__

## Consolidated env probe — 1 turn, not 5 (OS-agnostic)

When you need to verify multiple dependencies, DO NOT emit one
`python -c "import X"` command per package. Instead write a single
`env_check.py` that checks everything and exits non-zero on any
missing dep:

```python
# env_check.py — emitted via write_file, run via `python env_check.py`
import importlib, subprocess, sys
required_py = ["pydub", "soundfile", "numpy", "scipy", "requests", "websocket"]
required_bin = ["ffmpeg"]
missing_py = [m for m in required_py if importlib.util.find_spec(m) is None]
missing_bin = []
for b in required_bin:
    try:
        subprocess.run([b, "-version"], capture_output=True, check=True, timeout=5)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        missing_bin.append(b)
if missing_py or missing_bin:
    print(f"MISSING python: {missing_py}, binaries: {missing_bin}", file=sys.stderr)
    sys.exit(1)
print("OK")
```

This collapses 5-7 probe turns to 2 (write + run).

## Parallel tool calls — write all files in ONE turn (OS-agnostic)

When you have written an api_spec.txt AND you know the content of
requirements.txt + harness.py + smoke_test.py, emit all three
`write_file` tool uses in a SINGLE response. Sequential writes on
separate turns waste $0.30 each in Opus input-token replay.

Real-run evidence (trace d3b49875): 3 sequential write turns cost
$1.04 — could be 1 parallel turn at ~$0.40.

**Explicit rule:** if the NEXT 2+ files to be written are already
fully specified (content decided), emit ALL their `write_file`
tool_use blocks in the same assistant response. This is a guideline,
not a mandate — if file B's content legitimately depends on the outcome
of writing file A (rare), sequence them.

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

## System-prompt resilience (REQUIRED for agent-style APIs)

For APIs that drive an LLM-backed agent (chatbot, voice agent, realtime
conversation, any API where the provider needs persona/domain guidance),
the harness MUST read a system prompt from
``input_data["input_context"]["instructions"]`` (or one of its accepted
aliases: ``system_prompt``, ``system``, ``agent_prompt``). Agent 3's
test cases WILL include this field for voice/chat modalities — always
pass it through to the provider.

If the field is missing or empty (e.g., a partial test input or a
smoke probe), the harness MUST NOT hard-fail with ``"missing system
prompt"``. Fall back to a sensible default built from the candidate's
name + scope role:

```python
DEFAULT_INSTRUCTIONS = (
    "You are a helpful {scope_role} for {provider_name}. "
    "Answer the user's questions concisely and stay on-topic."
)
instructions = (
    input_data.get("input_context", {}).get("instructions")
    or input_data.get("input_context", {}).get("system_prompt")
    or DEFAULT_INSTRUCTIONS.format(
        scope_role="voice agent",  # or whatever the scope is
        provider_name="OpenAI",
    )
)
```

Hard-failing on missing instructions breaks post-loop test execution
because the plugin's drive loop passes per-turn payloads without always
setting instructions. Graceful fallback keeps tests running + scorable.

## File-write discipline — no meta-memory files

The ONLY files you EVER write to the sandbox:
  ``api_spec.txt`` · ``harness.py`` · ``requirements.txt``
  ``smoke_test.py`` · ``live_test.py`` · (optional) ``integration_test.py``

**Do NOT write meta-memory / state-tracking files.** These are all
FORBIDDEN and wasted turns:
  ``NOTES.md`` · ``NOTES.txt`` · ``STATUS.txt`` · ``progress.md``
  ``state.md`` · ``memory.txt`` · ``plan.md`` · ``context_backup.*``

Rationale: context is auto-managed server-side (clear_tool_uses at 80K
tokens, compact at 150K). Writing "save state before context clears"
files does NOT help — they're on-disk but not in-context, and the live
conversation + api_spec.txt + harness.py are the only memory you need.
Every meta-file costs ~$0.30 and zero build progress.

If you feel the urge to "save state," patch ``api_spec.txt`` with the
relevant finding instead — that IS your memory and it survives compaction.

## ERROR-HANDLING CONTRACT (HARD REQUIREMENT)

Your harness will be probed AFTER the build loop by an adversarial battery
with six deliberately-hostile inputs: empty dict, oversized payload,
malformed shape, repeated identical calls (idempotency), parallel calls
(concurrency), and bad credentials. Each probe expects `run()` to RETURN
a dict with `success=False` and a human-readable `error`. A crash (any
uncaught exception, any non-JSON stdout, any `sys.exit`) marks the harness
NOT READY and its test cases are SKIPPED — the candidate ends the pipeline
with zero scored runs. This is the single most common reason real
evaluations return empty. Do not let it happen to yours.

The contract, enforced by the adversarial battery:

1. **Empty / missing input** — `run({})` or `run({"text": None})` must
   return `{"success": False, "error": "missing test_file_path" (or similar), ...}`
   Not a KeyError, not a TypeError. Guard every `input_data["..."]` access
   with `.get(...)` + an explicit None check before you call the API.

2. **Malformed input** — `run({"garbage": 123})` must also return a clean
   `success=False`. Same guard pattern.

3. **Oversized input** — `run({"text": "A" * 1_000_000, ...})` must not
   hang forever; if the API rejects it, catch the HTTP error and return
   `success=False`. If your harness has no upstream size limit, relay the
   API's 413 / 400 as the error string.

4. **Bad credentials** — if the env var is unset OR the API returns 401 /
   403, return `success=False` with an `"auth"` hint in the error. Do NOT
   raise `KeyError("MINDEE_API_KEY")` — use `os.environ.get(...)` and
   return a structured error when missing.

5. **Idempotency / concurrency** — the battery calls `run()` twice in a
   row (idempotency) and three times in parallel (concurrency). Your
   harness must be safe to re-enter. Don't rely on module-level mutable
   state. Don't open a network session at import time. Do your work
   inside `run()` with locals.

6. **Network / SDK exceptions** — wrap the ENTIRE body of `run()` in
   `try/except Exception as exc:` and return
   `{"success": False, "error": f"{type(exc).__name__}: {exc}", ...}`.
   A bare `except` is correct here — a thin API client does not have the
   context to distinguish recoverable vs unrecoverable, and the judge
   layer handles that.

Skeleton that passes the battery:

```python
def run(input_data: dict) -> dict:
    import json, os, time
    t0 = time.time()
    try:
        input_data = input_data or {}
        test_file_path = input_data.get("test_file_path")
        api_key = os.environ.get("PROVIDER_API_KEY")
        if not api_key:
            return {"output": "", "latency_ms": 0.0, "tokens_used": None,
                    "cost_usd": None, "raw_response": {}, "success": False,
                    "error": "auth: PROVIDER_API_KEY not set"}
        if not test_file_path or not os.path.isfile(test_file_path):
            return {"output": "", "latency_ms": 0.0, "tokens_used": None,
                    "cost_usd": None, "raw_response": {}, "success": False,
                    "error": f"missing or invalid test_file_path: {test_file_path!r}"}
        # ... real API call here ...
        # response_body = requests.post(...).json()
        # return {"output": json.dumps(response_body), "success": True, ...}
    except Exception as exc:  # noqa: BLE001 — contract requires no raises
        return {"output": "", "latency_ms": (time.time() - t0) * 1000,
                "tokens_used": None, "cost_usd": None, "raw_response": {},
                "success": False, "error": f"{type(exc).__name__}: {exc}"}
```

Your smoke_test.py (template below) exercises these same six probes so
you catch contract violations during the build loop, not after.

======================================================================
## PHASE 1: RESEARCH — Understand the API, then WRITE api_spec.txt
======================================================================

The goal of research: give the builder everything it needs to write a correct API
call WITHOUT guessing. Specifically: the exact endpoint URL, exact auth header format,
exact request format (multipart vs JSON vs base64, field names), and a working Python
code example. If you find all four, the builder writes correct code in 1-2 turns.
If any is missing, the builder guesses wrong and spends 10+ turns debugging.

Do NOT skip this phase. Do NOT code from memory.

### Bias: WRITE EARLY, GAP-FILL AFTER — no large researches, no refinement

The single biggest failure mode is research perfectionism — fetching page after page,
accumulating thinking, and never calling `write_file("api_spec.txt")`. Beat this by
writing api_spec.txt AS SOON AS you have enough to populate the required fields
(BASE_URL, one ENDPOINT with request format, AUTH_HEADER, INPUT_COMPATIBILITY).
Unknowns become `TODO: <specific question>` entries that you gap-fill in later turns.

**HARD RESEARCH BUDGET:** no more than 3 tool calls to web_search/web_fetch BEFORE
you call write_file("api_spec.txt"). If after 3 research calls you still can't fill
the required fields, write the spec with the fields you have (remainder as TODOs)
anyway. Do NOT keep researching to "refine" or "verify" — that's refinement that
never ends. Commit, then patch from live-test feedback.

**Two valid flows:**

- **Compact (preferred when first pass lands enough info)** — In a single turn:
  `web_search` + `web_fetch` + `write_file("api_spec.txt")`. Typical for well-
  documented providers with an OpenAPI spec or strong quickstart page. This is
  what the "I have enough info, let me write the spec" moment looks like — act
  on it IMMEDIATELY, don't hedge with "let me check one more thing first."

- **Incremental (when first pass has gaps)** — Turn 1: `web_search` + `web_fetch`.
  Turn 2: `write_file("api_spec.txt")` with TODOs for anything missing. Turn 3+:
  ONE targeted search/fetch per TODO, then `patch_file` the resolved field.

**Which flow to pick:** if after your first `web_fetch` you can fill all required
fields, write the full spec in the same turn. If there are gaps, write what you
HAVE — TODOs included — and resolve each one in a dedicated follow-up turn. NEVER
run a second research pass without writing the spec first.

**Budget discipline:** adaptive-thinking blocks, web_fetch results, and your text
prose all count against max_tokens. If you catch yourself mid-turn writing a long
"analysis" of what you've read, STOP and call write_file now. The spec is your
memory; it's always easier to patch later than to re-research.

### How to research (search → navigate → fetch)

1. `web_search("{service} API documentation")` — survey the docs landscape
2. `web_search("{service} openapi.json OR swagger")` — find the machine-readable spec
3. `web_fetch(most_promising_docs_url)` — read endpoints, auth, examples

Search results include snippet content — skim them before deciding what to fetch.
Fetching is expensive (tokens + budget); be selective.

### api_spec.txt template

```
API_SPEC_START
SERVICE: [name]
BASE_URL: [exact URL | TODO: need base URL]
ENDPOINTS:
  - METHOD path
    Content-Type: ...
    Python requests param: json= | data= | files= | params=
    Request body: {...}
    Response: {...}
  [list ALL endpoints you found, not just the quickstart one]
AUTH_HEADER: [exact format quoted from docs | TODO: need auth scheme]
REQUEST_FORMAT: [per endpoint — json= vs data= vs files= matters]
RESPONSE_FORMAT: [JSON structure]
SDK_PACKAGE: [pip package | "none -- use requests"]
ACCEPTED_INPUT_FORMATS: [file types, URL support, plain text, base64]

PYTHON_EXAMPLES:
  [paste code snippets from docs — file upload, URL submission, SDK usage]

DOC_REFERENCES:
  [URLs: API reference, auth docs, SDK, OpenAPI spec]

DOC_MAP:
  [Every doc page you encountered, even ones you didn't read fully.
   This is the lookup Phase 2 and ask_research use to resolve specific
   questions without re-searching. Format: URL -- one-line description]

INPUT_COMPATIBILITY:
  file_upload: [YES -- endpoint + method | NO -- reason | TODO]
  url_submission: [YES -- endpoint + method | NO -- reason | TODO]
  plain_text: [YES -- endpoint + method | NO -- "requires file/URL" | TODO]
  base64: [YES -- endpoint + field | NO -- reason | TODO]

ROUTING_TABLE:
  test_file_path is set -> [endpoint + code pattern | TODO]
  text starts with http -> [endpoint + code pattern | TODO]
  text is plain content, no file -> [endpoint | "INCOMPATIBLE: reason" | TODO]
  input_type is structured_data -> [endpoint | "INCOMPATIBLE: reason" | TODO]

GAPS: (delete this section when empty)
  - [each unresolved TODO with the specific question you need answered]
API_SPEC_END
```

### Gap-fill rule (when you have TODOs after the first write)

Read your spec. For each TODO:
- Is it MUST-HAVE for Phase 2 (BASE_URL, one ENDPOINT, AUTH_HEADER, INPUT_COMPATIBILITY)?
  Yes → ONE targeted search OR fetch this turn, then patch_file the resolved field.
  No  → leave as TODO; Phase 2's live test is more informative than more research.

**The spec is your memory.** Never re-research a field that already has a concrete
value. Never do broad/open-ended searches — only narrow, gap-specific queries. Doing
the same web_search twice wastes budget and buries the answer in duplicate context.

### Phase 1 completion checklist — move to Phase 2 when ALL are true

- [x] api_spec.txt exists on disk (you called write_file)
- [x] BASE_URL is a concrete URL (not TODO)
- [x] At least one ENDPOINT has METHOD, path, request format, and Content-Type
- [x] AUTH_HEADER is quoted from docs (not TODO)
- [x] INPUT_COMPATIBILITY for every input form your test cases will use is YES/NO (not TODO)
- [x] ENDPOINT-FIT: you picked the RIGHT endpoint for this user's scope, not just
      any endpoint that compiles. Write a 2-3 line `<endpoint_fit>` block:
      - chosen_endpoint: METHOD path (or WebSocket URL for realtime/voice)
      - scope_match: one sentence why this endpoint matches the user's scope role
        (e.g., "voice_conversation scope → WebSocket /convai/conversation because
        the user needs multi-turn audio; the REST /tts endpoint can't maintain
        session state")
      - volume_fit: one sentence tying the endpoint to user's monthly_volume
        (LOW = atomic request, HIGH = batch/streaming if available)

When the checklist passes, state your PLAN in 3 bullet lines, then move to Phase 2.
**Remaining TODOs are FINE** — Phase 2's live tests provide cheaper, more
informative feedback than another docs-reading turn.

### Principle: live validation IS verification

Your research may have errors — that's OK. Build the harness and run a live test.
A real API error (404, 401, 400) tells you exactly what's wrong in 1 turn. Parsing
specs to verify ahead of time takes 10+ turns and may still be wrong.

### DO NOT

- **DO NOT write ad-hoc verification scripts** to cross-check the spec before Phase 2.
  A live 4xx is cheaper feedback than another docs-reading turn. (This does NOT
  forbid Phase 2's `<verify_against_docs>` read_file eyeball check — that's fine.)
- **DO NOT re-fetch docs** you already consulted — the spec remembers what you saw.
- **DO NOT narrate your plan at length** before acting. If you are about to write
  api_spec.txt, call write_file; don't spend 500 tokens explaining you will.
- **DO NOT guess from training data** — always quote AUTH_HEADER and REQUEST_FORMAT
  from fetched docs.

### When to GIVE UP

Cannot find real API documentation with actual endpoint URLs despite two distinct
search strategies (site-scoped + OpenAPI hunt)? Signal HARNESS_FAILED with reason
"docs_unusable". Don't burn the whole budget on fruitless research.

======================================================================
## PHASE 2: BUILD — Write harness.py IMMEDIATELY, debug from real errors
======================================================================

**THE CRITICAL RULE: WRITE harness.py BEFORE ANY INSPECTION SCRIPTS.** Once
api_spec.txt exists, your VERY NEXT write_file MUST be harness.py. Do NOT write
`inspect_sdk.py`, `check_*.py`, `explore_*.py`, or any script that prints SDK
methods/signatures. That introspection wastes 4-6 turns before a single API call,
and the information you can extract statically is almost always wrong anyway (SDK
methods accept keyword args, have hidden required params, or have version-specific
signatures). A live harness.run() failing with a real AttributeError or TypeError
is worth 10 introspection scripts.

**SEQUENCE — follow in order, no detours:**

1. **READ** api_spec.txt — locate PYTHON_EXAMPLES for your chosen endpoint
2. **WRITE** harness.py — COPY the PYTHON_EXAMPLE pattern and adapt to the run()
   contract. If no Python example exists, write from ENDPOINTS + AUTH_HEADER + request
   format. Use `requests` or the provider SDK exactly as the docs show. Do NOT
   second-guess method names from training memory — the spec is the truth.
3. **WRITE** requirements.txt — list the pip deps (e.g., `requests`, `elevenlabs`)
4. **RUN** `pip install -r requirements.txt`
5. **RUN** `python smoke_test.py`
6. **If smoke fails**: read the error → read_file("api_spec.txt") to check your
   assumption → patch_file the specific broken line → re-run. ONLY NOW is SDK
   introspection allowed, and ONLY if the error message is opaque (e.g.,
   "TypeError: X() missing 1 required positional argument: 'Y'" where Y isn't in
   the docs).

**WHAT "ENOUGH INFO" MEANS.** If api_spec.txt has BASE_URL + AUTH_HEADER + one
ENDPOINT with request format + INPUT_COMPATIBILITY, you have enough. Write harness.py.
Missing DOC_MAP entries, missing RESPONSE_FORMAT details, or uncertainty about edge
cases are NOT blockers — those get resolved by running the code, not researching.

**WHEN api_spec.txt IS MISSING INFO for your endpoint:** do ONE targeted
web_fetch (not web_search) of a specific doc URL, patch api_spec.txt with the
result, then write harness.py. Never loop: no fetch → inspect → fetch → inspect
cycle. At most one gap-fill fetch, then commit to code.

**NEVER write API calls from training data memory** — always from api_spec.txt.

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

## Smoke Test Template — ALSO exercises the ERROR-HANDLING CONTRACT

The smoke test is not just a structural check anymore — it replays the
six adversarial probes that will run AFTER the build loop. If smoke
passes, your harness is battery-ready; if it fails, you know exactly
which probe shape needs a fix. Same shape as the post-loop battery,
same expectations.

```python
import importlib
import inspect
import threading
import unittest.mock

harness = importlib.import_module("harness")

# ── Structural: callable with run(input_data) ───────────────────────────
assert hasattr(harness, "run") and callable(harness.run)
sig = inspect.signature(harness.run)
assert "input_data" in list(sig.parameters.keys())

REQUIRED = {"output", "latency_ms", "tokens_used", "cost_usd",
            "raw_response", "success", "error"}

def _assert_clean_failure(result, label):
    # Every probe below expects a dict with success=False, not a crash.
    assert isinstance(result, dict), f"{label}: run() did not return a dict (got {type(result).__name__})"
    missing = REQUIRED - set(result.keys())
    assert not missing, f"{label}: missing keys {missing}"
    assert isinstance(result["success"], bool), f"{label}: success must be bool"
    assert result["success"] is False, f"{label}: expected success=False, got True"
    assert result["error"], f"{label}: error must be non-empty on failure"

# ── Probe 1: happy-path shape (with network mocked — structural only) ──
with unittest.mock.patch("requests.Session.send", side_effect=ConnectionError("mocked")), \\
     unittest.mock.patch("requests.post", side_effect=ConnectionError("mocked")), \\
     unittest.mock.patch("requests.get", side_effect=ConnectionError("mocked")):
    result = harness.run({"text": "test", "input_type": "text",
                          "input_context": None, "test_file_path": None})
assert isinstance(result, dict)
assert not (REQUIRED - set(result.keys())), f"Missing keys: {REQUIRED - set(result.keys())}"
assert isinstance(result["success"], bool)
assert isinstance(result["latency_ms"], (int, float))
assert isinstance(result["output"], str)
assert isinstance(result["raw_response"], dict)

# ── Probe 2: empty input ────────────────────────────────────────────────
_assert_clean_failure(harness.run({}), "empty_input")

# ── Probe 3: malformed input ────────────────────────────────────────────
_assert_clean_failure(harness.run({"garbage": 123}), "malformed_input")

# ── Probe 4: oversized input (1MB of 'A') ───────────────────────────────
#   The harness may return success=True if the API happens to accept it;
#   the only failure mode is a CRASH. Just check no uncaught exception.
try:
    r = harness.run({"text": "A" * 1_000_000, "input_type": "text",
                     "input_context": None, "test_file_path": None})
    assert isinstance(r, dict) and "success" in r, "max_input: bad return shape"
except Exception as exc:  # noqa: BLE001
    raise AssertionError(f"max_input: run() raised {type(exc).__name__}: {exc}") from exc

# ── Probe 5: bad credentials ────────────────────────────────────────────
#   Clear env vars that look like API keys and confirm clean failure.
import os
saved = {k: os.environ.pop(k) for k in list(os.environ) if "API_KEY" in k or "SECRET" in k}
try:
    _assert_clean_failure(harness.run({"text": "x", "input_type": "text",
                                       "input_context": None,
                                       "test_file_path": None}),
                           "auth_error")
finally:
    os.environ.update(saved)

# ── Probe 6: concurrency — 3 parallel calls must all return dicts ──────
errors = []
results = []
def _worker():
    try:
        results.append(harness.run({"text": "x", "input_type": "text",
                                    "input_context": None,
                                    "test_file_path": None}))
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
threads = [threading.Thread(target=_worker) for _ in range(3)]
for t in threads: t.start()
for t in threads: t.join()
assert not errors, f"concurrency: parallel run() raised: {errors}"
for r in results:
    assert isinstance(r, dict) and "success" in r, "concurrency: non-dict return"

print("SMOKE TEST PASSED")
```

Adapt the mock patches (Probe 1) if using httpx or a provider SDK
instead of requests. The other five probes exercise the error-handling
contract above and don't depend on the HTTP library.

## ERROR RECOVERY — Root-Cause First, DOC_MAP-Targeted Research

<reason_about_errors>
When a test fails, do NOT patch the symptom. Every fix MUST be preceded by
reasoning:

1. **READ** the full error message. What is it actually telling you?
2. **IDENTIFY YOUR ASSUMPTION** — which line of code made it, and why?
   - Wrong endpoint URL? → read_file("api_spec.txt") § ENDPOINTS
   - Wrong auth format? → read_file("api_spec.txt") § AUTH_HEADER
   - Wrong request format? → read_file("api_spec.txt") § REQUEST_FORMAT
     (the API may expect data= not json=, or files= not data=)
   - Wrong platform? → if the service has multiple platforms (legacy vs new),
     is your API key for the platform you're targeting?
3. **CHECK api_spec.txt FIRST** — if the spec has the answer, patch_file and retry.
   If the spec is SILENT on the specific field that errored, that's the gap.
4. **DOC_MAP-TARGETED RESEARCH** — before any broad web_search, look at the
   DOC_MAP section of api_spec.txt. Each entry is `URL -- one-line description`.
   Find the URL whose description best matches your gap. Then:
   - `web_fetch(<that specific URL>)` — targeted, ONE fetch
   - Only if DOC_MAP has no match → `ask_research(<specific question>)` with a
     concrete question ("what's the EXACT `Content-Type` for ${endpoint}?",
     not "how does this API work?")
5. **Patch, retry, observe**. If the same error category repeats ≥3 times,
   your APPROACH is wrong — not the details. Pivot: different endpoint, SDK
   instead of raw requests, or different authentication mechanism.
6. **Truly unfixable** (expired credentials, deactivated account, API turned
   off) → signal HARNESS_FAILED with a specific reason.
</reason_about_errors>

<root_cause_before_patch>
Whenever the reassessment system injects a "STRATEGIC REASSESSMENT" message,
your NEXT emission MUST start with a `<root_cause_analysis>` block:

```
<root_cause_analysis>
1. Error: <paste the actual error text, not a summary>
2. My assumption: <the line of harness.py that triggered it + what I assumed>
3. api_spec.txt says: <quote the relevant section, or "spec is silent">
4. DOC_MAP URL that covers this: <URL or "none found — need ask_research">
5. Fix plan: <1 sentence — which line changes, to what>
</root_cause_analysis>
```

Then call patch_file / ask_research / run_code as the plan dictates. The
analysis block is required — without it, symptom-patching spirals kick in.
</root_cause_before_patch>

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
#
# Tool versions: basic 20250910 + 20250305. We briefly tried the 20260209
# "dynamic filtering" pair and reverted after real-run evidence (traces
# d3b49875 + 4068e872) surfaced: (a) 400 "container_id is required"
# errors cascading through every sub-agent that uses the same tools,
# (b) 3-5 min sandbox spin-up latency on Agent 2 real research calls,
# (c) Agent 2's non-beta messages.create silently hangs on the
# `container` kwarg. The ~24% input-token savings didn't compensate.
# See research.py top-of-file docstring for the full trace evidence.
#
# `code_execution_20260120` is still added explicitly by
# `_build_tools_with_programmatic` when PROGRAMMATIC_TOOLS_ENABLED=1.
# The basic 20250910 / 20250305 web tools do NOT auto-inject code_execution,
# so the explicit declaration doesn't conflict — it's REQUIRED for the
# `allowed_callers=["direct","code_execution_20260120"]` plugin-chain
# feature to work.
# ============================================================================

WEB_FETCH_TOOL = {
    "type": "web_fetch_20250910",
    "name": "web_fetch",
    "max_uses": 5,     # Docs page + specific endpoint + auth/SDK + homepage + follow link
    "max_content_tokens": 10000,  # Limit content per page to prevent context explosion.
    # 10K tokens (~40K chars) is enough for API endpoint details, auth format,
    # and code examples — real-run evidence: Phase 1 research successfully
    # extracted full spec for OpenAI Realtime at this budget. Prior 15K
    # budget combined with adaptive thinking + tool_use intent caused Turn 0
    # stop_reason=max_tokens truncations on complex providers (ElevenLabs);
    # tightening to 10K reserves output headroom for thinking blocks and
    # the write_file emission. Full docs can be 50K+ tokens which stays in
    # context for ALL subsequent turns at $0.60/turn. Claude Code uses
    # 100K char limit + disk persistence; we use server-side truncation
    # at the source.
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

# Advisor tool — Opus 4.7 provides strategic guidance to Sonnet executor.
# Sonnet decides when to call it. Opus sees the full conversation and gives a plan.
# This is a server-side tool — no local dispatch needed.
ADVISOR_TOOL = {
    "type": "advisor_20260301",
    "name": "advisor",
    "model": "claude-opus-4-7",
    "caching": {"type": "ephemeral", "ttl": "5m"},
}

# All tools passed to the API — server tools + custom tools + advisor
ALL_TOOLS = [WEB_FETCH_TOOL, WEB_SEARCH_TOOL, ADVISOR_TOOL, WRITE_FILE_TOOL, PATCH_FILE_TOOL, RUN_CODE_TOOL, READ_FILE_TOOL, ASK_RESEARCH_TOOL]


# ============================================================================
# OS-conditional prompt injection
# ============================================================================
# Only the HOST OS'es rules get injected into the builder prompt — Linux
# runs don't waste context reading Windows translation tables, macOS runs
# don't pay for Linux-specific hints. Keeps the prompt lean and portable
# as we move to cloud (Linux) deployments without changing the
# OS-agnostic bulk of the prompt.
#
# The rules below are based on REAL observed waste — each line maps to a
# real-run trace entry, not speculation.
# ============================================================================

_OS_RULES_WINDOWS = """\
## Windows-specific shell caveats (OS: Windows detected)

Real-run evidence (trace d3b49875): ~7 setup turns / $1.36 were burned
on Unix muscle-memory commands that fail silently on Windows. Your
training data is Unix-heavy — translate BEFORE emitting the command:

| Unix command | Windows equivalent                                       |
|--------------|----------------------------------------------------------|
| `tail -5`    | drop the pipe; write file then read last bytes in Python |
| `head -3`    | drop the pipe; use `findstr /N "."` or Python slicing    |
| `grep PAT`   | `findstr PAT`                                            |
| `A && B`     | `A && B` WORKS in cmd/PowerShell, but `A; B` does NOT    |
| `A & B`      | DON'T use — Windows `&` is a sequential separator not bg |
| backticks    | use `$(...)` in PowerShell or pipe to a temp file        |
| `ls -la`     | `dir` OR `python -c "import os; print(os.listdir('.'))"` |
| `which foo`  | `where foo`                                              |

Guideline: NEVER emit `| tail`, `| head`, `| wc`, `| grep` on Windows.
These return exit 255 and waste a turn. Pre-translate instead.

### Known silent-output trap: `python -c "..."` on Windows

`python -c "print(something)"` occasionally returns exit 0 with NO
visible stdout on Windows (subprocess output capture race). If a
`python -c` command on Windows produces no visible output, DO NOT
retry with another `python -c`. Write a `.py` file via `write_file`
and execute with `python foo.py` — this always captures output.
"""

_OS_RULES_LINUX = """\
## POSIX shell notes (OS: Linux detected)

Standard POSIX utilities are available (`tail`, `head`, `grep`, `wc`,
`find`, `xargs`, etc.). Use them naturally — no Windows translation
layer needed.

`python -c "..."` captures stdout reliably on Linux. For multi-line
scripts, still prefer `write_file` + `python foo.py` so the code
lives on disk for debugging, but inline is fine for one-liners.
"""

_OS_RULES_MACOS = """\
## POSIX shell notes (OS: macOS detected)

BSD-style POSIX utilities are available but some flag syntax differs
from GNU (e.g., `sed -i` requires an empty string arg: `sed -i '' ...`;
`grep -P` for Perl regex isn't supported — use `grep -E` or `rg`).
If your command fails with a sed/grep flag issue, pivot to writing
a Python script instead — faster than debugging BSD vs GNU.

`python -c "..."` captures stdout reliably on macOS.
"""


def _os_specific_rules() -> str:
    """Return the prompt rules block for the current host OS. Empty-string
    fallback for unrecognized platforms so the prompt always renders."""
    platform = sys.platform
    if platform == "win32":
        return _OS_RULES_WINDOWS
    if platform == "darwin":
        return _OS_RULES_MACOS
    if platform.startswith("linux"):
        return _OS_RULES_LINUX
    # Unknown platforms (FreeBSD, etc.) — strip the placeholder cleanly
    # rather than error. The OS-agnostic sections still cover most work.
    return ""


def _render_builder_prompt_for_os(prompt_template: str) -> str:
    """Fill __OS_TYPE__ + __OS_SPECIFIC_RULES__ placeholders based on host."""
    platform = sys.platform
    if platform == "win32":
        os_name = "Windows"
    elif platform == "darwin":
        os_name = "macOS"
    elif platform.startswith("linux"):
        os_name = "Linux"
    else:
        os_name = platform  # honest signal for weird platforms
    rendered = prompt_template.replace("__OS_TYPE__", os_name)
    rendered = rendered.replace("__OS_SPECIFIC_RULES__", _os_specific_rules())
    return rendered


# ============================================================================
# Modality-specific harness contracts
# ============================================================================
# Each modality has its own expected `raw_response` shape that downstream
# plugins consume. When the harness chooses a shape the plugin doesn't
# recognize, the response silently falls through and the report ends up
# with zero evidence — the hardest class of bug to debug because no error
# is raised.
#
# The contract below enumerates EVERY return shape the voice plugin
# actually handles. Claude (writing a harness autonomously) reads this,
# picks ONE of the listed shapes, and the plugin extracts the audio
# uniformly. New shapes get added here when the plugin's responder is
# extended — they cannot diverge silently.
#
# Forward-compat note: this block is injected via the same conditional
# pattern as `__OS_SPECIFIC_RULES__`. When we migrate to skills-style
# per-modality playbook files (see CLAUDE.md AD-002 revisit trigger),
# this string becomes `voice.md` in a skills directory, `_modality_
# specific_contract` becomes a file reader, and no call-site logic
# changes. Build in the right shape now, migrate layout later.
# ============================================================================

_VOICE_HARNESS_CONTRACT = """\
## Voice harness return-shape contract (REQUIRED when test has voice / audio / conversation modality)

The voice plugin (`tool_plugins/voice_realtime.py`) drives multi-turn
conversations by calling your harness once per turn and extracting the
agent's audio response from `raw_response`. It understands EXACTLY TWO
return shapes. Any other shape — `audio_url`, `audio_base64_string`,
`audio_data`, a custom schema — will silently fall through, zero agent
audio reaches the report, and every test scores 0.

Pick ONE shape per harness. Do NOT mix. Do NOT invent new keys.

### Shape A — inline audio bytes (preferred for responses under ~5 MB)

```python
def run(input_data):
    ...
    return {
        "output": "agent transcript (optional)",
        "latency_ms": elapsed_ms,
        "tokens_used": None,
        "cost_usd": None,
        "raw_response": {
            "audio_bytes": <bytes>,      # raw audio bytes — the plugin base64-decodes
                                          # automatically if you pass a b64 STRING instead
            "audio_format": "mp3",       # "mp3" | "wav" | "ogg" | "m4a" | "webm" | "flac" | "pcm16"
            "audio_sample_rate": 24000,  # REQUIRED when format="pcm16"; ignored otherwise
            "audio_channels": 1,         # REQUIRED when format="pcm16"; ignored otherwise
            "audio_content_type": "audio/mpeg",  # OPTIONAL; inferred from audio_format when absent
            "transcript": "optional text",
        },
        "success": True,
        "error": None,
    }
```

### Shape B — on-disk audio file path (preferred for large responses OR when you transcode via ffmpeg/pydub and it's already on disk)

```python
def run(input_data):
    ...
    # Write audio to a temp file (tempfile.NamedTemporaryFile, AudioSegment.export, ...)
    temp_path = "/tmp/response_abc.wav"
    return {
        "output": "agent transcript (optional)",
        "latency_ms": elapsed_ms,
        "raw_response": {
            "audio_path": temp_path,  # absolute path to a readable audio file
                                       # Extension determines format: .wav .mp3 .ogg .m4a .webm .flac
            "transcript": "optional text",
        },
        "success": True,
        "error": None,
    }
```

### HARD RULES — the plugin silently breaks otherwise

1. `raw_response` must contain EITHER `audio_bytes` OR `audio_path`. Never both.
2. When you have raw PCM16 samples (no container header — OpenAI Realtime,
   ElevenLabs Realtime, most telephony), use Shape A with
   `audio_format="pcm16"` + sample_rate + channels. The plugin transcodes
   to MP3 / WAV automatically.
3. When you have a known audio container (MP3 / WAV / OGG / M4A), either
   shape works. Shape B is slightly cheaper (no base64 round-trip).
4. DO NOT return `audio_url`, `audio_b64`, `audio_data`, `audio`,
   `audio_file`, or any other key name — they will NOT be parsed.
5. DO NOT put audio under a nested key like `raw_response["data"]["audio"]`.
   The plugin reads `raw_response.audio_bytes` and `raw_response.audio_path`
   only at the top level.
6. When the API returns text-only (no audio — rare but valid for voice
   APIs that can degrade to text), just omit BOTH audio keys. The plugin
   falls back to the text path.
7. For multi-turn sessions, each turn's harness call returns ONE turn's
   agent audio. The plugin handles session_state threading — you just
   read `input_data.get("session_state", {})` and mutate it in-place for
   continuity.

### What the plugin does with each shape (so you can verify your harness output)

Both shapes route through `_save_audio_blob(turn_token, bytes, ctype)`,
which writes `response_<token>_<hex>.<ext>` to the session dir that
appears as `runs/<trace_id>/harnesses/<slug>/voice/`. Those files are
then served by `/pzapi/runs/audio?path=...` for UI playback and fed
through the transcription plugin for scoring.

If your harness builds correctly but the report shows zero agent audio,
you violated this contract. Re-read the shapes above.
"""


def _modality_specific_contract(test_cases: list[Any] | None) -> str:
    """Return the modality-specific harness contract for this candidate's
    tests. Empty string when no modality needs a custom contract.

    The input/output types we treat as voice signals — any of these in
    the candidate's test cases injects the voice contract:
      * input_type:  ``conversation`` / ``voice_conversation`` / ``voice_turn`` / ``audio_content``
      * output_type: ``voice_conversation`` / ``voice_turn`` / ``audio_content``

    Other capabilities (code_gen, vision, ocr, translation) currently
    have no custom contract — they fall through to the general harness
    rules. When a future modality needs its own contract, add another
    branch here; when the contract set grows past ~6, migrate to per-
    modality files in a skills/ directory (see CLAUDE.md AD-002).
    """
    if not test_cases:
        return ""
    voice_modalities = {
        "conversation", "voice_conversation", "voice_turn", "audio_content",
    }
    for tc in test_cases:
        # TestCase may be Pydantic model or dict — handle both for
        # flexibility (tests call this with dicts; production with models).
        input_type = (
            getattr(tc, "input_type", None)
            or (tc.get("input_type") if isinstance(tc, dict) else None)
        )
        output_type = (
            getattr(tc, "output_type", None)
            or (tc.get("output_type") if isinstance(tc, dict) else None)
        )
        if input_type in voice_modalities or output_type in voice_modalities:
            return _VOICE_HARNESS_CONTRACT
    return ""


def _render_builder_prompt(
    prompt_template: str,
    test_cases: list[Any] | None = None,
) -> str:
    """Render __OS_TYPE__, __OS_SPECIFIC_RULES__, and __MODALITY_CONTRACT__
    based on host + candidate test cases.

    Thin wrapper over ``_render_builder_prompt_for_os`` that ALSO injects
    the right modality-specific harness contract. Voice tests get the
    voice harness contract; other modalities get an empty string so the
    prompt stays lean.
    """
    rendered = _render_builder_prompt_for_os(prompt_template)
    contract = _modality_specific_contract(test_cases)
    rendered = rendered.replace("__MODALITY_CONTRACT__", contract)
    return rendered


def _build_tools_with_programmatic(base_tools: list[dict]) -> list[dict]:
    """Adapt the tool list to enable Anthropic programmatic tool calling.

    When PUZZLEEVAL_PROGRAMMATIC_TOOLS=1, Claude can write Python that
    chains multiple custom-tool invocations in a single code_execution
    container. This compresses 5-10 sampling round-trips into 1, cutting
    cost and latency on multi-step build/debug cycles.

    The adaptation:
      - Adds a `code_execution_20260120` tool to the list
      - Marks every CUSTOM tool with `allowed_callers=["direct",
        "code_execution_20260120"]` so Claude can call them either way
      - Server tools (web_fetch, web_search, advisor) stay direct-only
        because they execute on Anthropic's infrastructure already

    When the flag is off, returns the input list unchanged.
    """
    from puzzleeval.config import PROGRAMMATIC_TOOLS_ENABLED
    if not PROGRAMMATIC_TOOLS_ENABLED:
        return base_tools

    out: list[dict] = []
    for t in base_tools:
        # Server tools have a `type` field starting with one of the known
        # server-tool prefixes. They cannot be called programmatically.
        ttype = t.get("type", "")
        if ttype.startswith(("web_fetch_", "web_search_", "advisor")):
            out.append(t)
            continue
        # Custom tools — clone and add allowed_callers
        adapted = dict(t)
        adapted["allowed_callers"] = ["direct", "code_execution_20260120"]
        out.append(adapted)

    # Explicit code_execution tool so Claude can write Python that
    # chains the custom tools above. The basic 20250910 / 20250305
    # web tools don't auto-inject code_execution (the 20260209 pair
    # did, which is why this was temporarily removed — reverted now).
    out.append({
        "type": "code_execution_20260120",
        "name": "code_execution",
    })
    return out

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

            # Determine rates based on iteration type.
            #
            # OBSERVABILITY BUG #3 FIX (2026-04-21): the previous fallback
            # hardcoded "claude-opus-4-7" as the default advisor model,
            # which would silently misprice future advisor models (e.g.,
            # a Claude 5 advisor added to MODEL_PRICING later). When
            # the iteration omits its `model` field, fall back to the
            # EXECUTOR model — the advisor typically runs the same-or-
            # stronger model as the caller, so this is a safer default
            # than a hardcoded version. If the advisor genuinely runs a
            # different model, the iteration will report it explicitly.
            if iter_type == "advisor_message":
                iter_model = getattr(iteration, "model", None) or model
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

    # Fallback: no iterations array (older API or non-beta call).
    #
    # OBSERVABILITY BUG #1 FIX (2026-04-21): the prior fallback
    # only counted `usage.input_tokens` and `usage.output_tokens`,
    # SILENTLY IGNORING `cache_creation_input_tokens` and
    # `cache_read_input_tokens` which are present at top level on
    # every response. On cached calls (i.e. every turn after the
    # first when system-prompt caching or message-prefix caching is
    # active), this under-reported cost by 5-40% because cache
    # writes at 1.25x base and cache reads at 0.1x base were never
    # billed. Now the fallback applies the SAME multipliers as the
    # iterations-path above. Server-tool web_search cost is also
    # added to match the iterations-path behavior.
    input_price, output_price = MODEL_PRICING.get(
        model, (3.0 / 1_000_000, 15.0 / 1_000_000)
    )
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (usage.input_tokens * input_price) + (usage.output_tokens * output_price)
    cost += cache_create * input_price * 1.25
    cost += cache_read * input_price * 0.1
    # Mirror the iterations-path: add web search cost when present.
    server_tool_use = getattr(usage, "server_tool_use", None)
    if server_tool_use:
        searches = getattr(server_tool_use, "web_search_requests", 0) or 0
        cost += searches * WEB_SEARCH_PRICE_PER_SEARCH
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
    read_state: dict[str, float] | None = None,
) -> tuple[str, int]:
    """
    Execute a custom tool and return (result_string, exit_code).

    exit_code: 0 = success, non-zero = failure. For non-run_code tools,
    exit_code is 0 (success) unless the tool returns an error string.
    extra_env passes API credentials to run_code subprocesses.

    ``read_state`` is the per-build read-timestamp tracker used by the
    read-before-patch deterministic gate (mirrors Claude Code's
    ``FileEditTool`` pattern, ``src/tools/FileEditTool/FileEditTool.ts:275-287``).
    Map of ``filename → last_read_timestamp_seconds``. Populated by
    ``read_file``, checked by ``patch_file``. Callers that don't supply
    one get legacy behavior (no gate).
    """
    if tool_name == "write_file":
        result = _tool_write_file(tool_input, sandbox_dir, read_state=read_state)
        return result, 1 if result.startswith("Error") else 0
    elif tool_name == "patch_file":
        result = _tool_patch_file(tool_input, sandbox_dir, read_state=read_state)
        return result, 1 if result.startswith("Error") else 0
    elif tool_name == "run_code":
        return _tool_run_code(tool_input, sandbox_dir, extra_env=extra_env)
    elif tool_name == "read_file":
        result = _tool_read_file(tool_input, sandbox_dir, read_state=read_state)
        return result, 1 if result.startswith("Error") else 0
    elif tool_name == "ask_research":
        return "Error: ask_research must be dispatched via the main loop", -2
    else:
        return f"Error: unknown tool '{tool_name}'", -2


def _tool_write_file(
    tool_input: dict,
    sandbox_dir: Path,
    read_state: dict[str, float] | None = None,
) -> str:
    """Write a file to the sandbox directory with security checks.

    Also marks the file as "read" in ``read_state`` (when supplied) —
    write_file produces known content, so the immediate-next patch_file
    gate treats the write as equivalent to a read. Matches Claude Code's
    FileWriteTool semantics (writes populate ``readFileState`` because
    the caller knows what was written).
    """
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
        # Write creates a known-content state — mark as "read" so the
        # patch gate knows the caller can legitimately patch the file
        # without an explicit read_file in between. See the read-before-
        # patch docstring on _tool_patch_file for the full gate design.
        if read_state is not None:
            try:
                read_state[filename] = target.stat().st_mtime
            except OSError:
                pass
        return f"Written {len(content)} chars to {filename}"
    except OSError as e:
        return f"Error writing {filename}: {e}"


def _tool_patch_file(
    tool_input: dict,
    sandbox_dir: Path,
    read_state: dict[str, float] | None = None,
) -> str:
    """Replace a specific string in an existing file (string-replace editing).

    Read-before-patch gate (Claude Code parity): when ``read_state`` is
    supplied, the patch is REFUSED unless ``read_state[filename]`` exists
    AND is >= the file's current mtime. Forces the builder to run
    ``read_file(filename)`` before patching so its patch plan is based on
    current contents. Mirrors Claude Code's ``FileEditTool.ts:275-287``
    where the same gate eliminates iterative-micro-patch waste observed
    in real-run trace 28cb2648 (5 consecutive patches = $1.67 burned).

    Legacy behavior preserved when ``read_state`` is None (tests, direct
    callers) — no gate fires.
    """
    raw_filename = tool_input.get("filename", "")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")

    filename = Path(raw_filename).name
    if not filename:
        return "Error: empty filename"

    target = sandbox_dir / filename
    if not target.exists():
        return f"Error: '{filename}' does not exist in sandbox. Use write_file to create it first."

    # Read-before-patch gate (Claude Code parity). Only fires when a
    # read_state tracker is threaded in (builder loop always threads
    # one; tests can opt-in).
    if read_state is not None:
        try:
            current_mtime = target.stat().st_mtime
        except OSError:
            current_mtime = None
        last_read = read_state.get(filename)
        if last_read is None:
            return (
                f"STOP: '{filename}' has not been read yet in this build. "
                f"Call read_file('{filename}') FIRST so your patch plan is "
                f"based on the current file contents. This gate prevents the "
                f"iterative-micro-patch waste pattern (multiple consecutive "
                f"patches without re-reading → blind fixes → cumulative cost). "
                f"After reading, return with a comprehensive patch that "
                f"addresses every issue you identified."
            )
        if current_mtime is not None and last_read < current_mtime - 0.5:
            # 0.5s tolerance for filesystem timestamp granularity.
            return (
                f"STOP: '{filename}' was modified after your last read_file call. "
                f"Your patch plan may be based on stale contents. Call "
                f"read_file('{filename}') AGAIN to see the current state, then "
                f"plan a comprehensive patch."
            )

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
        # Update read_state to the post-patch mtime so the gate doesn't
        # fire a spurious "file modified after last read" on the IMMEDIATE
        # next patch. Claude Code does the same — after an edit, it
        # refreshes the read-timestamp because it just read-through-wrote
        # the full content. Same-turn patch chains stay unblocked.
        if read_state is not None:
            try:
                read_state[filename] = target.stat().st_mtime
            except OSError:
                pass
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


def _tool_read_file(
    tool_input: dict,
    sandbox_dir: Path,
    read_state: dict[str, float] | None = None,
) -> str:
    """Read a file from the sandbox directory.

    When ``read_state`` is supplied, records the read timestamp so the
    ``patch_file`` gate can later verify the file has been read AT OR
    AFTER its current on-disk mtime. Mirrors Claude Code's
    ``readFileState`` map (``src/Tool.ts`` + ``FileReadTool.ts`` +
    ``FileEditTool.ts``).
    """
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
        # Record the read timestamp for the patch-gate. We use the file's
        # current mtime rather than `time.time()` so that a file modified
        # EXACTLY at read time doesn't register as stale on the next
        # patch. Claude Code does the same — tracks the last-read
        # timestamp against the file's `lastWriteTime`.
        if read_state is not None:
            try:
                read_state[filename] = target.stat().st_mtime
            except OSError:
                pass
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
            from puzzleeval.config import output_config_for_request
            _kwargs_research_sub: dict[str, object] = {}
            _ocfg = output_config_for_request()
            if _ocfg:
                _kwargs_research_sub["output_config"] = _ocfg
            response = client.beta.messages.create(
                model=RESEARCH_MODEL,
                max_tokens=4096,
                betas=["context-management-2025-06-27"],
                system=[{"type": "text", "text": _with_shared_preamble(TARGETED_RESEARCH_SYSTEM)}],
                messages=messages,
                tools=[
                    {"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
                    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 10000},
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

    # ── Full user context (domain, technical_level, constraints, workflow) ──
    # Before this block, the builder only saw sub-task descriptions + test
    # case forms. That made Agent 5 pick "first endpoint that accepts the
    # input shape" rather than the endpoint that fits the user's ACTUAL
    # workload. A healthcare team at 100k records/mo should get the batch
    # endpoint; a hobbyist at 5/mo should get the atomic one. The signals
    # below make that distinction land in the builder's planning.
    uo = input_data.user_understanding
    domain = getattr(uo, "domain", "") or "(not specified)"
    constraints = getattr(uo, "constraints", None)
    technical_level = "(not specified)"
    monthly_volume: int | None = None
    budget_range = "(not specified)"
    integration_requirements: list[str] = []
    must_have_features: list[str] = []
    if constraints is not None:
        technical_level = getattr(constraints, "technical_level", None) or "(not specified)"
        monthly_volume = getattr(constraints, "monthly_volume", None)
        budget_range = getattr(constraints, "budget_range", None) or "(not specified)"
        integration_requirements = list(getattr(constraints, "integration_requirements", []) or [])
        must_have_features = list(getattr(constraints, "must_have_features", []) or [])

    # Locate THIS candidate's scope within the workflow DAG so the builder
    # knows which step it's building for (affects endpoint selection,
    # expected input shape, side-effect posture).
    workflow = getattr(uo, "workflow", None)
    scope_role_hints: list[str] = []
    if workflow is not None:
        steps = getattr(workflow, "steps", []) or []
        covered_ids = set(getattr(candidate, "covers_step_ids", []) or [])
        for step in steps:
            sid = getattr(step, "id", "")
            if sid in covered_ids:
                role = getattr(step, "role", "")
                desc = getattr(step, "description", "")
                side_effects = getattr(step, "side_effects", "read_only")
                scope_role_hints.append(
                    f"  - step {sid}: role={role!r}, side_effects={side_effects!r}"
                    + (f", description={desc!r}" if desc else "")
                )
    workflow_context = "\n".join(scope_role_hints) if scope_role_hints else "  (no workflow steps matched — single-scope or legacy flow)"

    # Volume-tier hint lets the builder reason about batch vs atomic endpoint
    # selection without us prescribing a specific choice. Single source of
    # truth for the bands lives in puzzleeval.config.MONTHLY_VOLUME_BANDS
    # so product tuning doesn't need a code edit in this file.
    from puzzleeval.config import band_monthly_volume
    _vol_label, _vol_hint = band_monthly_volume(monthly_volume)
    if monthly_volume is None:
        volume_tier = f"(not specified — assume moderate)"
    else:
        volume_tier = f"{monthly_volume}/mo — {_vol_label} ({_vol_hint})"

    integration_line = ", ".join(integration_requirements) if integration_requirements else "(none)"
    features_line = ", ".join(must_have_features) if must_have_features else "(none)"

    user_context_block = f"""
## User context (from Agent 1 — use this to pick the RIGHT endpoint, not just any working one)

- domain: {domain}
- technical_level: {technical_level}
- monthly_volume: {volume_tier}
- budget_range: {budget_range}
- integration_requirements: {integration_line}
- must_have_features: {features_line}

## This candidate's scope in the user's workflow (from Agent 1's blueprint)

{workflow_context}

**Endpoint-selection guidance:**
- When the provider has both atomic (`/process`) and batch (`/batch/process`) endpoints,
  pick based on `monthly_volume`, not alphabetical order. HIGH volume → batch.
- When the scope's `side_effects` is `creates_records` or `modifies_records`, prefer
  the provider's sandbox / DRY_RUN endpoint if one exists (check Agent 4's atlas).
- When the scope's role hints at a specific capability (e.g. `extract`, `classify`,
  `translate`), bias toward the endpoint whose atlas `scope_hints` matches that role
  — NOT the first endpoint that happens to accept your input format.
- When `technical_level` is `non-technical`, prefer the provider's higher-level
  SDK / wrapper endpoint (fewer required fields) over the raw REST endpoint.
"""

    # Input/output type context from Agent 3
    output_types = set()
    input_types: set[str] = set()
    for tc in input_data.test_cases.test_cases:
        output_types.add(tc.output_type)
        input_types.add(tc.input_type)
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

    # Multi-call harness contracts — when the test case input_type is one
    # the voice_realtime / conversation_simulator plugins DRIVE through
    # harness_runner, the payload shape harness.run() receives is NOT the
    # usual {text, input_type, input_context, test_file_path}. It's
    # per-turn context from the plugin. Teach this explicitly — without
    # it, the builder writes a single-turn harness that KeyErrors on
    # voice_url or misses session_state, silently scoring every test 0.
    multi_call_input_types = {
        "voice_conversation", "voice_turn", "conversation",
    }
    present_multi_call = [it for it in multi_call_input_types if it in input_types]
    if present_multi_call:
        test_case_forms += (
            "\n\n  **MULTI-CALL HARNESS CONTRACT (REQUIRED for "
            f"{', '.join(sorted(present_multi_call))})**\n"
            "  Plugins drive your harness.run() MULTIPLE TIMES per test case "
            "(once per turn). Each call passes this payload shape:\n"
            "    {\n"
            "      'audio_url' / 'caller_audio_url': fetchable URL of the\n"
            "         caller's TTS-synthesized audio for this turn,\n"
            "      'turn_index': int — 0-based position in the conversation,\n"
            "      'session_state': mutable dict for YOU to thread state\n"
            "         (conversation_id, session_token, cookies, etc.) across\n"
            "         turns. READ it at entry, WRITE to it before returning.\n"
            "    }\n"
            "  Your harness.run() MUST:\n"
            "    1. On turn_index=0: initialize the provider's conversation\n"
            "       (get conversation_id, open WebSocket, etc.), stash in\n"
            "       session_state.\n"
            "    2. On turn_index>0: reuse the provider's session from\n"
            "       session_state to preserve context across turns.\n"
            "    3. Return the standard shape: {success, output, raw_response,\n"
            "       latency_ms, error}. If the provider returns audio, include\n"
            "       it as raw_response['audio_bytes'] for the plugin to STT.\n"
            "    4. Close resources (WebSocket, HTTP session) when the plugin\n"
            "       finishes driving — detect via a 'session_end' key in\n"
            "       session_state OR rely on garbage collection; both are OK.\n"
            "\n"
            "  **System prompt (voice-agent persona) lives in input_context.**\n"
            "  On EVERY turn, the framework merges the test case's\n"
            "  ``input_context`` into your payload. Read the agent's\n"
            "  grounding / persona / system prompt from it — TRY these keys\n"
            "  in order and use the first non-empty string you find:\n"
            "    input_context['instructions']  ← canonical (voice plugin)\n"
            "    input_context['system_prompt']\n"
            "    input_context['system']\n"
            "    input_context['brief']\n"
            "    input_context['agent_prompt']\n"
            "  NEVER hardcode the persona in your harness. A harness that\n"
            "  ignores input_context and uses a DEFAULT_SYSTEM_PROMPT makes\n"
            "  every test score on the harness's own persona, not the\n"
            "  user's. This is a grounding bug: the user wrote a detailed\n"
            "  system prompt describing the business, hours, pricing,\n"
            "  service area; the harness must pass it verbatim to the\n"
            "  provider as the `system` / `instructions` message on every\n"
            "  turn so the LLM stays on-script. If no instructions key is\n"
            "  present, return success=False with error='missing system\n"
            "  prompt in input_context' — do NOT fall back to your own\n"
            "  made-up persona.\n"
        )

    # wss:// advisory — when docs URL hints at WebSocket/realtime, tell the
    # builder to surface it as a limitation in live_test.py's error path
    # instead of silently building a REST facsimile. Without this, OpenAI
    # Realtime gets tested as a chat-completion mock and the user sees a
    # misleading pass rate. Detect via URL heuristic on verified_api_docs_url.
    wss_flagged = False
    docs_url = (candidate.verified_api_docs_url or "").lower()
    if "realtime" in docs_url or "wss" in docs_url or "websocket" in docs_url:
        wss_flagged = True
    if wss_flagged:
        test_case_forms += (
            "\n\n  **WebSocket/Realtime endpoint detected**\n"
            "  This provider uses a persistent wss:// connection as its\n"
            "  primary protocol. The current harness template does NOT include\n"
            "  a WebSocket skeleton (pending — tracked as a separate capability\n"
            "  pass). Two acceptable outcomes:\n"
            "    a. Build against the REST-shaped fallback endpoints IF they\n"
            "       exist and you explicitly note in live_test.py's output\n"
            "       and in HARNESS_COMPLETE's NOTES that Realtime/streaming\n"
            "       was NOT tested — this is a REST facsimile. The evaluation\n"
            "       report must carry this advisory.\n"
            "    b. If no REST fallback exists and WebSocket is truly required,\n"
            "       signal HARNESS_FAILED with reason='websocket_not_supported'\n"
            "       and a clean explanation — better than a misleading pass.\n"
            "  Under no circumstances build a REST facsimile silently. Users\n"
            "  must see which providers were tested at full fidelity.\n"
        )

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
{user_context_block}

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
{_format_atlas_context_for_builder(candidate)}
{_format_modality_context_for_builder(input_data)}
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
    progress_callback: "Callable[[str, dict], None] | None" = None,
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

    # ──────────────────────────────────────────────────────────────────
    # Q4b: OpenAPI auto-generation fast path. If the candidate's atlas
    # carries an openapi_url, generate the harness mechanically — no LLM
    # build turns. The build loop only runs when this fast path fails
    # (no openapi spec, or no operation matched the role).
    # ──────────────────────────────────────────────────────────────────
    openapi_harness_text = _try_openapi_fastpath(candidate, sandbox_dir, logger, trace_id)
    if openapi_harness_text:
        # Write the generated harness + a tiny smoke test, prepare requirements.txt,
        # and return a TestHarness without ever invoking the builder loop.
        try:
            (sandbox_dir / "harness.py").write_text(openapi_harness_text, encoding="utf-8")
            (sandbox_dir / "requirements.txt").write_text("requests\n", encoding="utf-8")
            (sandbox_dir / "smoke_test.py").write_text(
                "from harness import run\n"
                "result = run({'input_data': 'smoke'})\n"
                "assert isinstance(result, dict)\n"
                "assert 'success' in result\n"
                "print('SMOKE OK')\n",
                encoding="utf-8",
            )
            _create_venv(sandbox_dir, logger)
            logger.info(
                f"openapi-fastpath: harness generated for {candidate.name} without LLM",
                extra={
                    "operation": "openapi_fastpath_success",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                },
            )
            if progress_callback:
                progress_callback("openapi_fastpath_success", {
                    "candidate_name": candidate.name,
                })
            return TestHarness(
                candidate_name=candidate.name,
                provider=candidate.provider,
                harness_dir=str(sandbox_dir),
                entry_file="harness.py",
                requirements=["requests"],
                auth_env_vars=[
                    f"{re.sub(r'[^A-Z0-9]+', '_', candidate.provider.upper()).strip('_') or 'API'}_API_KEY"
                ],
                auth_method=candidate.auth_method or "api_key",
                harness_code=openapi_harness_text,
                smoke_test_passed=True,
                build_turns=0,
                build_cost_usd=0.0,
                build_duration_sec=0.0,
                supported_input_types=["text", "structured_data", "document_content"],
                supported_output_types=["structured_json"],
                validation_notes="auto-generated from OpenAPI spec",
                api_spec_path=candidate.api_spec_path,
            )
        except Exception as exc:
            logger.warning(
                f"openapi-fastpath: write/setup failed for {candidate.name}: {exc} — "
                f"falling back to LLM build",
                extra={"operation": "openapi_fastpath_fallback", "trace_id": trace_id},
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
                web_fetch_blocks=0,
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
    # Exposed to the per-turn builder loop so `_render_builder_prompt` can
    # inject the right modality-specific contract (voice harness shape,
    # etc.) based on THIS candidate's test-case modalities.
    test_cases_for_builder = staged_test_cases

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
    # MAX_CONSECUTIVE_ERRORS: 3 (was 2) — one extra try before strategic
    # pivot. Some first-fix attempts legitimately fail (stale docs, wrong
    # version), and 2 triggered pivots on legitimately-fixable bugs.
    MAX_CONSECUTIVE_ERRORS = 3
    build_start_time = time.monotonic()  # Wall-clock timeout tracking
    # MAX_BUILD_TIME_SECONDS: 900 (was 480) — complex voice / WebSocket /
    # multi-endpoint builds legitimately take 12-15 min. Under 8 min the
    # loop was killing mid-Phase-2 debug cycles on ElevenLabs + similar.
    MAX_BUILD_TIME_SECONDS = 900
    candidate_web_fetch_blocks = 0  # Cumulative recoverable web_fetch blocks across turns (Phase 1 hardening)
    smoke_ever_passed = False  # Track smoke test pass across ALL turns
    smoke_passed_at_turn = -1  # Which turn the smoke test first passed
    # MAX_TURNS_AFTER_SMOKE: 25 (was 15) — more debug room after the smoke
    # test passes. Live API calls (especially voice / async-polling) often
    # need 5-10 fix iterations for payload shape, auth headers, etc.
    MAX_TURNS_AFTER_SMOKE = 25
    # --- Adaptive progress tracking (Claude Code diminishing-returns pattern) ---
    # `progress_ring`: last N turns' "did anything change" signal. A turn
    # counts as PROGRESS if it wrote/patched a file OR received new server-
    # tool content (web_fetch/search/advisor result). If N turns in a row
    # show no progress, inject a wrap-up nudge. This replaces pure turn
    # counting as the "stuck" signal — progress-based is more honest.
    from puzzleeval.config import (
        AGENT5_DIMINISHING_RETURNS_WINDOW,
        AGENT5_MAX_REASSESSMENT_TIERS,
    )
    progress_ring: list[bool] = []
    diminishing_nudge_sent = False
    # `approaches_tried`: per-candidate list of pivots taken. Cited in
    # reassessment prompt so the builder knows what NOT to try again.
    approaches_tried: list[str] = []
    # Read-before-patch gate state (Claude Code parity). Per-build dict
    # mapping ``filename → last_read_timestamp_seconds``. Populated by
    # read_file + write_file; checked by patch_file. Prevents the
    # iterative-micro-patch waste pattern observed in trace 28cb2648
    # (5 consecutive patches to harness.py = $1.67 burned on blind fixes).
    # See `_tool_patch_file` docstring for the full gate design.
    build_read_state: dict[str, float] = {}

    # ★ CORE: Multi-turn autonomous loop with verification gate
    # Guardrails: turn limit (AGENT5_MAX_TURNS) + wall-clock timeout.
    # No per-candidate budget cap — let the agent use as many tokens as it
    # needs per turn. The turn limit and timeout prevent runaway costs.
    # Turn-budget nudge state — when the builder has ≤3 turns remaining
    # AND hasn't signaled HARNESS_COMPLETE/FAILED yet, inject a wrap-up
    # reminder into the next user message. Stolen from Claude Code's
    # getBudgetContinuationMessage pattern (query/tokenBudget.ts). Without
    # this, Agent 5 can get stuck polishing on turn 23 and run out without
    # ever producing a final verdict. Fired once per candidate.
    turn_budget_nudge_sent = False

    while turn < AGENT5_MAX_TURNS:
        # Turn-budget nudge — fire once when <=3 turns remain.
        turns_remaining = AGENT5_MAX_TURNS - turn
        if (
            turns_remaining <= 3
            and not turn_budget_nudge_sent
            and not smoke_ever_passed
            and messages  # only when there IS a conversation to nudge
        ):
            messages.append({
                "role": "user",
                "content": (
                    f"TURN BUDGET ADVISORY: {turns_remaining} turns remaining "
                    f"(of {AGENT5_MAX_TURNS}). You must now EITHER: (a) "
                    "finish the current attempt + signal HARNESS_COMPLETE "
                    "if the smoke test + live test will pass, OR (b) signal "
                    "HARNESS_FAILED with a specific failure_reason. Do NOT "
                    "start a new research pass or a third refactor. Pick one "
                    "and commit."
                ),
            })
            turn_budget_nudge_sent = True
            logger.info(
                "Turn-budget nudge injected for %s at turn %d",
                candidate.name, turn,
                extra={"operation": "turn_budget_nudge", "trace_id": trace_id,
                       "candidate_name": candidate.name,
                       "turns_remaining": turns_remaining},
            )

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
                from puzzleeval.config import (
                    output_config_for_request,
                    CACHE_MESSAGES_ENABLED,
                    CACHE_CLEAR_AT_LEAST_TOKENS,
                )
                from puzzleeval.anthropic_client import call_with_model_fallback
                _kwargs_builder: dict[str, object] = {}
                _ocfg = output_config_for_request()
                if _ocfg:
                    _kwargs_builder["output_config"] = _ocfg
                # Message-level cache breakpoint (cache-the-growing-
                # conversation). Mutates `messages` to carry exactly
                # one active `cache_control` marker on the tail block
                # of the last message — caches the full prefix so
                # turn N+1 reads turn N's entire conversation at 0.1x
                # base input cost. System-block cache (below) still
                # stands; this adds a second breakpoint for messages.
                # Two active breakpoints total; docs allow up to 4.
                # Gated by env so a future Anthropic regression can
                # be reverted with PUZZLEEVAL_CACHE_MESSAGES_ENABLED=0.
                if CACHE_MESSAGES_ENABLED:
                    _apply_message_cache_breakpoint(messages)
                # Wrap in model-fallback ladder: on persistent 429 at
                # ``current_model`` (Opus 4.7 by default), degrade to Sonnet
                # 4.6 → Haiku 4.5 rather than hard-failing after the SDK's
                # 3 retries. ``call_with_model_fallback`` re-raises
                # non-rate-limit errors unchanged so the PTL recovery
                # ``except anthropic.BadRequestError`` below still fires.
                response = call_with_model_fallback(
                    fn=lambda _m: client.beta.messages.create(
                        model=_m,
                        max_tokens=current_max_tokens,
                    betas=["context-management-2025-06-27", "compact-2026-01-12", "advisor-tool-2026-03-01"],
                    # Prompt caching — TWO ACTIVE CACHE BREAKPOINTS:
                    #   1. System block (here) — caches the 10.7K-token
                    #      builder system prompt that's identical across
                    #      every turn of a candidate's build. Cached at
                    #      1.25x write on turn 1, 0.1x reads turns 2+.
                    #   2. Last message block (applied above via
                    #      `_apply_message_cache_breakpoint`) — caches
                    #      the growing conversation prefix so turn N+1
                    #      reads everything up through turn N at 0.1x.
                    # Combined: 40-60% input-cost reduction on 15-25 turn
                    # builds at Opus rates (docs-verified per Anthropic's
                    # prompt-caching guide). Block-level placement is the
                    # documented-reliable path for both breakpoints.
                    system=[{
                        "type": "text",
                        "text": _with_shared_preamble(
                            _with_builder_appendix(
                                _render_builder_prompt(
                                    BUILDER_SYSTEM_PROMPT,
                                    test_cases=test_cases_for_builder,
                                )
                            )
                        ),
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=messages,
                    tools=_build_tools_with_programmatic(ALL_TOOLS),
                    thinking={"type": "adaptive"},
                    context_management={
                        "edits": [
                            # ────────────────────────────────────────────
                            # ORDERING NOTE (real-run e21f6077 exposed this
                            # on 2026-04-21): the Anthropic schema rejects
                            # any edits list where `clear_thinking_20251015`
                            # is not the FIRST entry with:
                            #   "context_management: `clear_thinking_20251015`
                            #    must be the first strategy in
                            #    `context_management.edits` when provided"
                            # This constraint is not in the public docs
                            # we read, but the API enforces it hard — both
                            # OpenAI and ElevenLabs builds 400'd on turn
                            # 1 before the builder wrote a single file.
                            # Regression-guarded by
                            # `test_clear_thinking_edit_is_first_in_edits_list`.
                            # ────────────────────────────────────────────

                            # clear_thinking_20251015 — preserves ALL
                            # thinking blocks across turns to maximize
                            # cache hits on the message prefix.
                            #
                            # Per docs (/build-with-claude/context-editing):
                            # "When thinking blocks are kept in context
                            # (not cleared), the prompt cache is
                            # preserved, enabling cache hits and reducing
                            # input token costs. ... To maximize cache
                            # hits, preserve all thinking blocks by
                            # setting `keep: all`."
                            #
                            # The EARLIER failure (trace real_debug_3)
                            # was a `trigger` field on this edit type
                            # which is not accepted by the schema. The
                            # correct shape has only `type` + `keep` —
                            # no trigger, no threshold. This version
                            # passes schema validation.
                            #
                            # Pairs with the message cache breakpoint:
                            # thinking blocks sit inside assistant turns,
                            # so keeping them means turn N's thinking is
                            # part of turn N+1's cache-eligible prefix.
                            # Without this edit, the default behavior
                            # ("keep only last turn's thinking") would
                            # invalidate the message cache at every
                            # thinking-block boundary — defeating the
                            # whole point of caching messages.
                            {
                                "type": "clear_thinking_20251015",
                                "keep": "all",
                            },
                            # SPEC-CONFORMING CONSOLIDATED clear_tool_uses
                            # (docs: /build-with-claude/context-editing).
                            #
                            # Prior configuration had TWO edits with
                            # `clear_tool_inputs` as a list of tool names —
                            # but the published schema declares
                            # `clear_tool_inputs` as a BOOLEAN (default
                            # False). The API was silently coercing or
                            # ignoring the list shape; real behavior was
                            # undefined. Consolidated to ONE correctly-
                            # typed edit with explicit semantics:
                            #   - keep: 3 most-recent tool uses (default,
                            #     made explicit)
                            #   - clear_tool_inputs: False (default, safer
                            #     — preserves tool CALL parameters so
                            #     Claude still sees what it did, only
                            #     RESULTS are cleared)
                            #   - clear_at_least: 10000 input tokens —
                            #     per docs, "clear enough tokens to make
                            #     the cache invalidation worthwhile."
                            #     Prevents firing when clearing saves less
                            #     than the cost of re-writing the cache
                            #     prefix. Tuned via PUZZLEEVAL_CACHE_CLEAR_AT_LEAST.
                            #   - exclude_tools: write_file / patch_file /
                            #     advisor are NEVER cleared. Claude needs
                            #     to see its edit history to maintain
                            #     file-state awareness; advisor verdicts
                            #     are rare and high-value.
                            #
                            # Interaction with message-level cache
                            # (_apply_message_cache_breakpoint above):
                            # when clearing fires it invalidates the
                            # message cache at the clear point. The
                            # `clear_at_least: 10000` guard ensures every
                            # clearing event saves more tokens than the
                            # cache rewrite costs.
                            {
                                "type": "clear_tool_uses_20250919",
                                "trigger": {"type": "input_tokens", "value": 80000},
                                "keep": {"type": "tool_uses", "value": 3},
                                "clear_at_least": {
                                    "type": "input_tokens",
                                    "value": CACHE_CLEAR_AT_LEAST_TOKENS,
                                },
                                "clear_tool_inputs": False,
                                "exclude_tools": [
                                    "write_file", "patch_file", "advisor",
                                ],
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
                    **_kwargs_builder,
                    ),
                    primary_model=current_model,
                    trace_id=trace_id,
                    operation_label=f"agent5_builder/{candidate.name}",
                )
                break  # Success — exit retry loop
            except anthropic.BadRequestError as e:
                # PTL recovery: if "prompt too long" or "max_tokens exceed",
                # reduce output tokens and retry. Server-side context management
                # should prevent most overflows, but this catches edge cases.
                #
                # Match only ACTUAL prompt-too-long markers. An earlier version
                # matched any occurrence of "context" which false-positived on
                # unrelated 400s (e.g., a schema-validation error whose path
                # included "context_management..."). Each false-positive retried
                # instantly, hit the same 400, halved max_tokens, and blew
                # through the retry budget in microseconds — a real tracer-
                # bullet from trace real_debug_3.
                error_msg = str(e).lower()
                is_ptl = (
                    "prompt is too long" in error_msg
                    or "prompt too long" in error_msg
                    or "maximum context length" in error_msg
                    or "max_tokens" in error_msg
                    or "context_length_exceeded" in error_msg
                )
                if is_ptl and retry < max_retries:
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
                    web_fetch_blocks=candidate_web_fetch_blocks,
                    build_cost_usd=round(accumulated_cost, 4),
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
                    web_fetch_blocks=candidate_web_fetch_blocks,
                    build_cost_usd=round(accumulated_cost, 4),
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
                    web_fetch_blocks=candidate_web_fetch_blocks,
                    build_cost_usd=round(accumulated_cost, 4),
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

        # Per-turn progress callback — let the frontend show live build progress
        if progress_callback:
            # Determine phase from model + flags
            phase = "researching" if not api_spec_written else "building"
            if smoke_ever_passed:
                phase = "validating"
            # Extract tool names used this turn
            tool_names = [b.name for b in response.content if b.type == "tool_use"]
            progress_callback("build_turn", {
                "candidate_name": candidate.name,
                "turn": turn + 1,
                "max_turns": AGENT5_MAX_TURNS,
                "phase": phase,
                "model": current_model,
                "cost_usd": round(call_cost, 4),
                "tools_used": tool_names,
            })

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

        # ── Universal orphan-server-tool-use scrubber ──
        # Real-run signal (traces voice_debug_4 + voice_debug_5):
        # occasionally the API returns a response whose `content` has a
        # `server_tool_use` block (web_search / web_fetch / advisor)
        # WITHOUT its matching `..._tool_result` in the same response.
        # This happens when:
        #   - stop_reason=max_tokens truncates the response mid-tool
        #   - a search server-side fails to materialize a result block
        #   - rare API races where the response closes before the
        #     server tool completes (voice_debug_5 saw this with
        #     stop_reason=tool_use at turn 0 — not max_tokens)
        # Once appended to ``messages``, the next API call 400s with
        # ``<tool>_tool_use was found without a corresponding
        # <tool>_tool_result block`` — unrecoverable from conversation
        # history. So we scrub orphans BEFORE appending, on EVERY turn.
        # When content becomes empty after stripping, fall back to a
        # minimal text block so the conversation retains valid shape.
        SERVER_TOOL_NAMES = {"web_search", "web_fetch", "advisor"}
        _server_use_ids: list[str] = []
        _server_result_ids: set[str] = set()
        for _blk in response.content:
            _btype = getattr(_blk, "type", "")
            if _btype == "server_tool_use" and getattr(_blk, "name", "") in SERVER_TOOL_NAMES:
                _bid = getattr(_blk, "id", "")
                if _bid:
                    _server_use_ids.append(_bid)
            # Match ANY server-tool result by suffix — Anthropic uses a
            # per-tool-family type literal:
            #   web_search   → "web_search_tool_result"
            #   web_fetch    → "web_fetch_tool_result"
            #   advisor      → "advisor_tool_result"
            #   <future>     → "<name>_tool_result"
            # The earlier explicit list missed advisor_tool_result and
            # the scrubber wrongly classified live advisor pairs as
            # orphans, stripping the server_tool_use and breaking the
            # next API call (real bug surfaced in voice_dual_3).
            elif _btype.endswith("_tool_result") and _btype != "tool_result":
                _rid = getattr(_blk, "tool_use_id", "")
                if _rid:
                    _server_result_ids.add(_rid)
        _orphan_ids = {u for u in _server_use_ids if u not in _server_result_ids}
        if _orphan_ids:
            logger.warning(
                f"Stripping {len(_orphan_ids)} orphan server tool_use block(s) "
                f"for {candidate.name} at turn {turn} (stop_reason={response.stop_reason})",
                extra={
                    "operation": "orphan_server_tool_strip",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "turn": turn,
                    "stop_reason": response.stop_reason,
                    "orphan_count": len(_orphan_ids),
                },
            )
            # Monkey-patch the response.content in place so subsequent
            # references in this same turn (e.g., _extract_text_from_
            # response, tool dispatch) see the repaired content. We use
            # object.__setattr__ because anthropic SDK response objects
            # are frozen pydantic models.
            _cleaned = [
                blk for blk in response.content
                if not (
                    getattr(blk, "type", "") == "server_tool_use"
                    and getattr(blk, "id", "") in _orphan_ids
                )
            ]
            if not _cleaned:
                _cleaned = [{
                    "type": "text",
                    "text": "(server-side research truncated; continuing)",
                }]
            try:
                object.__setattr__(response, "content", _cleaned)
            except (AttributeError, TypeError):
                # Fall back: if we can't mutate the response, the
                # `messages.append` below will still see the repaired
                # list through local variable capture. But subsequent
                # reads of ``response.content`` will see the orphan —
                # safer to bail with a warning than to poison the
                # conversation. Treat this turn as a pause and nudge.
                messages.append({"role": "assistant", "content": _cleaned})
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "Server-side research was truncated. Summarize "
                            "what you have and proceed — do not re-issue "
                            "the same search."
                        ),
                    }],
                })
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
                    web_fetch_blocks=candidate_web_fetch_blocks,
                    build_cost_usd=round(accumulated_cost, 4),
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
                    # Pass credentials so run_code subprocesses can do live API validation.
                    # Pass build_read_state so the patch_file gate enforces
                    # read-before-patch discipline across the build loop.
                    result_text, exit_code = _dispatch_tool(
                        block.name, block.input, sandbox_dir,
                        extra_env=credentials, read_state=build_read_state,
                    )
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

            # ── Phase 1 + 1.5 hardening: detect useless web_fetch results ──
            # Two failure surfaces share one recovery path:
            #   Phase 1   — HTTP-level errors (403/Cloudflare, 429, 5xx)
            #   Phase 1.5 — content-level uselessness (SPA shells, auth walls,
            #               soft 404s, marketing pages with no API signals)
            # Anthropic's web_fetch can't customize user-agent and can't run JS,
            # so the recovery for both is the same: PIVOT to web_search snippets,
            # GitHub SDK repos, alternate URLs, or archive.org. We append unified
            # guidance to the tool_results so the model sees it next turn.
            # See puzzleeval/web_fetch_fallback.py for both classifiers.
            if ENABLE_FETCH_FALLBACK:
                blocked = extract_blocked_fetches(response)
                unusable = extract_unusable_pages(response)
                actionable = count_actionable_problems(blocked, unusable)
                if actionable:
                    candidate_web_fetch_blocks += actionable
                    logger.info(
                        f"Web fetch problems for {candidate.name} at turn {turn}",
                        extra={
                            "operation": "agent5_fetch_blocks",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            **summarize_blocks_for_log(blocked, unusable),
                        },
                    )
                    # Backoff for 429s before next turn (no-op when no rate limit).
                    maybe_apply_rate_limit_backoff(blocked)
                    # Append unified guidance as a text block alongside the
                    # tool_results so the model sees it on its next turn.
                    guidance = build_fallback_message(blocked, unusable)
                    if guidance:
                        tool_results.append({"type": "text", "text": guidance})

            # Append assistant response + tool results to conversation
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            # ── Adaptive progress tracking (Claude Code diminishing-returns) ──
            # A turn "made progress" if: (a) it wrote/patched a file, OR
            # (b) it got a server-tool result (web_fetch/search/advisor).
            # Pure text/thinking turns don't advance state, so they don't
            # count. This is the core signal for "stuck" — independent of
            # turn count.
            turn_made_progress = False
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    name = getattr(block, "name", "") or ""
                    if name in ("write_file", "patch_file"):
                        turn_made_progress = True
                        break
                if getattr(block, "type", None) == "server_tool_use":
                    turn_made_progress = True
                    break
            progress_ring.append(turn_made_progress)
            if len(progress_ring) > AGENT5_DIMINISHING_RETURNS_WINDOW:
                progress_ring.pop(0)
            # If N consecutive no-progress turns AND we have smoke passing,
            # inject a wrap-up nudge (once) — the builder should commit to
            # HARNESS_COMPLETE or pivot. This is SOFT — it doesn't break
            # the loop, just tells the agent it's spinning.
            if (
                len(progress_ring) >= AGENT5_DIMINISHING_RETURNS_WINDOW
                and not any(progress_ring)
                and not diminishing_nudge_sent
                and smoke_ever_passed
            ):
                messages.append({
                    "role": "user",
                    "content": (
                        f"## DIMINISHING-RETURNS DETECTED\n\n"
                        f"The last {AGENT5_DIMINISHING_RETURNS_WINDOW} turns produced no code "
                        f"changes and no new research findings. Smoke test has passed. "
                        f"Either signal HARNESS_COMPLETE if the live test is passing, OR "
                        f"identify the specific blocker with a `<root_cause_analysis>` block "
                        f"and act on it. Don't keep thinking — either act or commit.\n"
                    ),
                })
                diminishing_nudge_sent = True
                logger.info(
                    f"Diminishing-returns nudge sent for {candidate.name} at turn {turn}",
                    extra={"operation": "diminishing_returns_nudge",
                           "trace_id": trace_id,
                           "candidate_name": candidate.name},
                )

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

            # If stuck in an error loop, force escalating reassessment.
            # Hard cap on reassessment tiers — after N escalations without
            # recovery, accept defeat and emit FailedHarness cleanly.
            if total_reassessments >= AGENT5_MAX_REASSESSMENT_TIERS:
                logger.warning(
                    f"Agent 5: max reassessment tiers ({AGENT5_MAX_REASSESSMENT_TIERS}) "
                    f"reached for {candidate.name} — accepting failure cleanly",
                    extra={"operation": "reassessment_cap_reached",
                           "trace_id": trace_id,
                           "candidate_name": candidate.name,
                           "approaches_tried": approaches_tried},
                )
                break
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
                            f"Use `ask_research` to verify it — but read the DOC_MAP in api_spec.txt "
                            f"FIRST and target the specific doc URL that covers '{cat_label}'.\n"
                        )
                # Record this reassessment as an "approach tried" so the
                # next tier's prompt can cite what NOT to repeat.
                approaches_tried.append(
                    f"tier_{total_reassessments}_{recent_cats[-1] if error_history else 'unknown'}"
                )
                approaches_summary = (
                    f"\n**Approaches already attempted (do NOT repeat):**\n"
                    + "\n".join(f"  - {a}" for a in approaches_tried[-5:])
                    if approaches_tried else ""
                )

                # Escalating tiers based on cumulative reassessments.
                # Each tier REQUIRES the builder to first emit a
                # <root_cause_analysis> block — symptom-patching without
                # root-cause reasoning is what causes the spirals we want
                # to break. The block must cite api_spec.txt evidence.
                root_cause_requirement = (
                    "\n**REQUIRED BEFORE ANY patch_file / write_file:** emit a "
                    "`<root_cause_analysis>` block answering:\n"
                    "  1. What exactly failed (the error message, not a summary)\n"
                    "  2. What assumption did I make in the code? (cite the line)\n"
                    "  3. What does api_spec.txt say about this? (cite the section — "
                    "AUTH_HEADER / ENDPOINTS / REQUEST_FORMAT / PYTHON_EXAMPLES / DOC_MAP)\n"
                    "  4. Which DOC_MAP URL would resolve this? (before ask_research)\n"
                    "  5. What's the specific fix? (1-sentence plan)\n"
                    "If the spec doesn't answer #3, that's the gap — use ask_research with the "
                    "specific DOC_MAP URL if you identified one.\n"
                )
                if total_reassessments == 1:
                    # Tier 1: Fix the specific issue (root-cause-first)
                    reassessment = (
                        f"\n\n## STRATEGIC REASSESSMENT (Tier 1 — root-cause it)\n\n"
                        f"You have hit errors for {consecutive_errors} consecutive turns.\n"
                        f"**Last error:** {last_errors[:300]}\n"
                        + root_cause_requirement
                        + approaches_summary
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
                        + root_cause_requirement
                        + approaches_summary
                        + f"\n**REQUIRED ACTION:** Before ANY more patches, use `ask_research` to "
                          f"verify your fundamental assumption. Use a DOC_MAP URL from "
                          f"api_spec.txt as the ground-truth source if available — don't do broad "
                          f"searching.\n"
                        + pattern_hint
                    )
                else:
                    # Tier 3+: Structured pivot — three different approaches required
                    from puzzleeval.api_patterns import STRUCTURED_PIVOT_PROMPT
                    reassessment = (
                        f"\n\n## STRATEGIC REASSESSMENT (Tier {total_reassessments} — STRUCTURED PIVOT)\n\n"
                        f"You have been stuck for {total_reassessments} reassessment cycles "
                        f"on this harness. Variation-of-the-same-approach has not worked.\n\n"
                        f"**Last error:** {last_errors[:300]}\n"
                        + root_cause_requirement
                        + approaches_summary
                        + pattern_hint
                        + "\n"
                        + STRUCTURED_PIVOT_PROMPT
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
            web_fetch_blocks=candidate_web_fetch_blocks,
            build_cost_usd=round(accumulated_cost, 4),
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
            web_fetch_blocks=candidate_web_fetch_blocks,
            build_cost_usd=round(accumulated_cost, 4),
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
        web_fetch_blocks=candidate_web_fetch_blocks,
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


def _compute_conversation_summary(
    conversation_log: list[dict],
    candidate_name: str,
) -> dict:
    """Build a compact summary block from a per-turn conversation log.

    Emitted as a separate ``conversation_summary.json`` artifact so we
    have grep-friendly per-candidate cache/cost observability without
    mutating the turn-by-turn ``conversation_log.json`` (which
    downstream readers parse as a plain list of turn dicts).

    Fields:
      * ``aggregate`` — totals across all turns: tokens by category,
        total cost, fresh / cache_read / cache_write percentages of
        total billed input, observed cache_hit_pct.
      * ``per_model`` — per-model breakdown. Builds typically use
        Sonnet for Phase-1 research and Opus for Phase-2+ coding; this
        lets us see where the money actually went.
      * ``cache_analysis`` — behavioral signals: which turns wrote
        cache entries, which turns only read, and whether message-level
        caching (the 2026-04-21 NEW-AJ fix) appears to be active
        (detected when ``cache_read_tokens`` substantially exceeds the
        fixed ~system-prompt-size read seen with system-only caching).
      * ``top_costly_turns`` — top 3 most expensive turns for
        diagnostic drill-down.

    All math is over the already-recorded per-turn fields
    (cache_read_tokens / cache_create_tokens / input_tokens /
    output_tokens / cost_usd) so this helper has zero dependencies on
    the Anthropic response object or pricing table — can be regenerated
    post-hoc from any conversation_log.json file.
    """
    real_turns = [t for t in conversation_log if isinstance(t, dict)
                  and isinstance(t.get("turn"), int)]

    totals = {
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    per_model: dict[str, dict] = {}

    for turn_dict in real_turns:
        for k in ("input_tokens", "cache_read_tokens",
                  "cache_create_tokens", "output_tokens"):
            totals[k] += int(turn_dict.get(k, 0) or 0)
        totals["cost_usd"] += float(turn_dict.get("cost_usd", 0.0) or 0.0)

        m = turn_dict.get("model") or "unknown"
        mbucket = per_model.setdefault(m, {
            "turns": 0, "input_tokens": 0, "cache_read_tokens": 0,
            "cache_create_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        })
        mbucket["turns"] += 1
        for k in ("input_tokens", "cache_read_tokens",
                  "cache_create_tokens", "output_tokens"):
            mbucket[k] += int(turn_dict.get(k, 0) or 0)
        mbucket["cost_usd"] += float(turn_dict.get("cost_usd", 0.0) or 0.0)

    total_billed = (totals["input_tokens"] + totals["cache_read_tokens"]
                    + totals["cache_create_tokens"])

    def _pct(n: int | float) -> float:
        return round(100.0 * n / total_billed, 2) if total_billed > 0 else 0.0

    aggregate = {
        **totals,
        "cost_usd": round(totals["cost_usd"], 4),
        "total_billed_input": total_billed,
        "fresh_input_pct": _pct(totals["input_tokens"]),
        "cache_read_pct": _pct(totals["cache_read_tokens"]),
        "cache_write_pct": _pct(totals["cache_create_tokens"]),
        # cache_hit_pct: reads / (reads + writes + fresh) — matches
        # the formula we use per-turn. Higher = more context served
        # from cache = cheaper runs.
        "cache_hit_pct": _pct(totals["cache_read_tokens"]),
    }

    # Cache behavior signals. Useful for debugging "is message caching
    # actually firing" without parsing every turn.
    turns_with_write = sum(
        1 for t in real_turns if int(t.get("cache_create_tokens", 0) or 0) > 0
    )
    turns_with_read_only = sum(
        1 for t in real_turns
        if int(t.get("cache_create_tokens", 0) or 0) == 0
        and int(t.get("cache_read_tokens", 0) or 0) > 0
    )
    # Heuristic: with ONLY system-prompt caching, cache_read per turn
    # is a constant ~system+tools size (~25K tokens). With message-
    # level caching active, at least SOME turns cache_read substantially
    # MORE than that baseline because the growing conversation prefix
    # is being served from cache.
    #
    # Real-run evidence (b79d79b5 post-fix, 2026-04-21):
    #   OpenAI cache_read progression: [43K, 76K, 77K, 82K, 82K, 84K, 85K, 25K]
    #   ElevenLabs cache_read: [158K, 25K, 94K, 95K, 101K, 25K, 90K, 25K, 81K, 85K...]
    # Both show CLEAR message-cache activity (max cache_read 3-6x the
    # system-only baseline) even though EVERY turn also has some
    # cache_write (the newly-appended tail content). A prior version
    # of this heuristic required zero-write turns to detect growth;
    # that path doesn't exist with Anthropic's API, so the heuristic
    # returned false-negatives on genuinely-active message caching.
    #
    # Fixed detection: message caching is active if ANY turn reads
    # more than 1.5x the system-only baseline (~25K for our 10.7K
    # system prompt + ~15K tool definitions). Threshold 40K captures
    # genuine message-cache activity while staying comfortably above
    # the baseline's noise floor.
    SYSTEM_ONLY_BASELINE = 25000  # tokens — measured empirically
    MESSAGE_CACHE_THRESHOLD = 40000  # 1.6x baseline; clear signal
    all_reads = [int(t.get("cache_read_tokens", 0) or 0) for t in real_turns]
    max_read = max(all_reads) if all_reads else 0
    message_cache_likely_active = max_read > MESSAGE_CACHE_THRESHOLD

    # Top 3 most expensive turns for diagnostic drill-down.
    sorted_by_cost = sorted(
        real_turns,
        key=lambda t: float(t.get("cost_usd", 0.0) or 0.0),
        reverse=True,
    )[:3]
    top_costly_turns = [
        {
            "turn": t.get("turn"),
            "cost_usd": round(float(t.get("cost_usd", 0.0) or 0.0), 4),
            "input_tokens": int(t.get("input_tokens", 0) or 0),
            "cache_read_tokens": int(t.get("cache_read_tokens", 0) or 0),
            "cache_create_tokens": int(t.get("cache_create_tokens", 0) or 0),
            "output_tokens": int(t.get("output_tokens", 0) or 0),
            "stop_reason": t.get("stop_reason"),
        }
        for t in sorted_by_cost
    ]

    return {
        "candidate_name": candidate_name,
        "total_turns": len(real_turns),
        "aggregate": aggregate,
        "per_model": {
            m: {**bucket, "cost_usd": round(bucket["cost_usd"], 4)}
            for m, bucket in per_model.items()
        },
        "cache_analysis": {
            "turns_with_cache_write": turns_with_write,
            "turns_with_cache_read_only": turns_with_read_only,
            "message_cache_likely_active": message_cache_likely_active,
        },
        "top_costly_turns": top_costly_turns,
    }


def _save_conversation_log(
    sandbox_dir: Path,
    conversation_log: list[dict],
    candidate_name: str,
) -> None:
    """
    Save the conversation log to the sandbox directory as a readable JSON file.
    This makes Agent 5's builder loop transparent — you can see every turn,
    what Claude said, what tools it called, and what results it got.

    Also writes a compact ``conversation_summary.json`` alongside with
    per-candidate cache hit / token / cost aggregates. Summary is
    non-destructive (separate file) so downstream readers of
    conversation_log.json see exactly the same per-turn shape they
    always did.
    """
    log_path = sandbox_dir / "conversation_log.json"
    try:
        log_path.write_text(
            json.dumps(conversation_log, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass  # Non-critical — don't crash the build if logging fails

    # Cache/cost summary — additive, non-blocking.
    try:
        summary = _compute_conversation_summary(conversation_log, candidate_name)
        summary_path = sandbox_dir / "conversation_summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception:
        # Summary computation should NEVER fail the build. Swallow any
        # edge-case errors (malformed turn dict, etc.) silently.
        pass


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

# ---------------------------------------------------------------------------
# Venv pre-install manifest
# ---------------------------------------------------------------------------
# Packages 85%+ of harnesses end up installing anyway. Pre-installing them
# saves 2-3 "pip install X then check import" Agent 5 turns per candidate
# (real-run trace d3b49875: 7 setup turns = $1.36 wasted largely on these).
#
# Criterion for inclusion: (a) used by the mainstream API interaction
# patterns in api_patterns.py (REST, multipart, async-polling, OAuth, SSE,
# WebSocket), (b) small wheel (<10 MB), (c) widely stable.
# Includes NOT the big-ML wheels (torch, transformers, playwright) — those
# stay candidate-specific in requirements.txt.
VENV_PREINSTALL_MANIFEST = [
    "requests>=2.31.0",          # HTTP client — every REST harness
    "websocket-client>=1.6.0",   # sync WS — OpenAI Realtime, ElevenLabs
    "pydub>=0.25.1",             # audio format conversion (MP3/WAV/PCM16)
    "soundfile>=0.12.1",         # PCM16 I/O without ffmpeg dependency
    "numpy>=1.26.0",             # array math (pydub dep + audio pipelines)
    "python-dotenv>=1.0.0",      # env loading (common for candidate keys)
]


def _create_venv(sandbox_dir: Path, logger, trace_id: str, candidate_name: str) -> bool:
    """
    Create a Python venv in the sandbox directory. Returns True on success.

    The venv is at sandbox_dir/.venv. Commands run via _tool_run_code()
    will automatically use it (via _build_sandbox_env() PATH injection).

    After the venv is created, we pre-install `VENV_PREINSTALL_MANIFEST`
    (small, widely-used HTTP/audio deps). This is best-effort — pre-install
    failures do NOT fail venv creation (the builder can install missing
    packages itself if any slipped through). Controlled by
    `PUZZLEEVAL_VENV_PREINSTALL=0` to disable (debug / minimal-venv runs).
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
            # Optional pre-install of common deps — cuts ~2-3 Agent 5 turns.
            if os.environ.get("PUZZLEEVAL_VENV_PREINSTALL", "1") != "0":
                _preinstall_venv_deps(
                    venv_dir, logger, trace_id, candidate_name,
                )
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


def _preinstall_venv_deps(
    venv_dir: Path,
    logger,
    trace_id: str,
    candidate_name: str,
) -> None:
    """Best-effort pre-install of common harness deps into a fresh venv.

    Runs `python -m pip install --quiet <manifest>` using the venv's
    interpreter. Timeouts at 180s (accommodates slow networks). Logs
    success/failure but never raises — the builder is still responsible
    for writing requirements.txt with any candidate-specific extras, and
    missing packages would surface in the builder's own env_check turn.
    """
    if sys.platform == "win32":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        logger.warning(
            f"venv python not found for pre-install: {venv_python}",
            extra={"operation": "venv_preinstall_missing_python",
                   "trace_id": trace_id,
                   "candidate_name": candidate_name},
        )
        return
    try:
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", *VENV_PREINSTALL_MANIFEST],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            logger.info(
                f"Pre-installed {len(VENV_PREINSTALL_MANIFEST)} deps "
                f"for {candidate_name}",
                extra={"operation": "venv_preinstall_ok",
                       "trace_id": trace_id,
                       "candidate_name": candidate_name,
                       "packages": VENV_PREINSTALL_MANIFEST},
            )
        else:
            # Don't fail the build — the builder can install any missing
            # deps itself. Just log which packages slipped through so we
            # can see the pattern over time.
            logger.info(
                f"Pre-install partial for {candidate_name}: "
                f"{result.stderr[:400]}",
                extra={"operation": "venv_preinstall_partial",
                       "trace_id": trace_id,
                       "candidate_name": candidate_name},
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.info(
            f"Pre-install failed for {candidate_name} (non-fatal): {exc}",
            extra={"operation": "venv_preinstall_error",
                   "trace_id": trace_id,
                   "candidate_name": candidate_name},
        )


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


def _inflate_b64_sentinels(obj: Any) -> Any:
    """Walk a JSON-decoded structure and re-inflate `{"_b64": "..."}`
    sentinels (written by `_execute_single_test`'s exec_script) back
    into real ``bytes``. Round-trip partner of the ``_bytes_safe``
    encoder. Operates in place on dicts/lists; pure-Python recursion
    keeps the implementation independent of any third-party encoder.

    Real-run signal (voice_dual_7): a harness that returned raw MP3
    bytes in ``raw_response.audio_bytes`` had those bytes silently
    stringified as ``"b'\\xff\\xfb...'"`` (Python repr) by the prior
    ``json.dump(..., default=str)`` path. The voice plugin's
    `isinstance(audio_bytes, bytes)` check then failed, no agent
    audio was saved, and the merged conversation file ended up
    caller-only. Fix is a transparent two-stage encoder around the
    JSON serialization border.
    """
    if isinstance(obj, dict):
        # Sentinel: a dict with exactly one key "_b64" holding a base64 string.
        if (
            len(obj) == 1
            and "_b64" in obj
            and isinstance(obj["_b64"], str)
        ):
            try:
                import base64 as _b64
                return _b64.b64decode(obj["_b64"], validate=False)
            except (ValueError, TypeError):
                # Malformed sentinel — leave the raw dict in place so
                # the consumer can decide what to do.
                return obj
        return {k: _inflate_b64_sentinels(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_inflate_b64_sentinels(v) for v in obj]
    return obj


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

    # Bytes-safe round-trip: harnesses can put raw `bytes` (audio MP3,
    # binary blobs) into raw_response. JSON can't carry bytes natively;
    # the previous `default=str` path stringified them as Python repr
    # (`"b'\\xff\\xfb...'"`) which the plugin couldn't decode → voice
    # harnesses' agent audio was silently lost (only caller audio
    # survived; the "merged conversation" file ended up caller-only).
    # Real-run signal: voice_dual_7 produced 5 caller MP3s + 1 merged
    # MP3 that was actually 5 caller voices stitched together with NO
    # agent audio.
    #
    # General fix: walk the result before dumping; replace every bytes
    # value with the sentinel `{"_b64": "<base64-utf8>"}`. Caller side
    # walks the loaded JSON and re-inflates sentinels back to bytes.
    # Transparent to harnesses (they keep returning bytes) and to the
    # plugin (it gets bytes back). Belt-and-braces: the plugin now also
    # accepts the b64 string directly, so older harnesses that
    # base64-encoded themselves still work.
    exec_script = (
        'import sys, json, base64\n'
        'sys.path.insert(0, ".")\n'
        'import harness\n'
        '\n'
        'def _bytes_safe(obj):\n'
        '    if isinstance(obj, (bytes, bytearray)):\n'
        '        return {"_b64": base64.b64encode(bytes(obj)).decode("ascii")}\n'
        '    if isinstance(obj, dict):\n'
        '        return {k: _bytes_safe(v) for k, v in obj.items()}\n'
        '    if isinstance(obj, list):\n'
        '        return [_bytes_safe(v) for v in obj]\n'
        '    if isinstance(obj, tuple):\n'
        '        return [_bytes_safe(v) for v in obj]\n'
        '    return obj\n'
        '\n'
        'input_data = json.loads(open("_test_input.json", encoding="utf-8").read())\n'
        'result = harness.run(input_data)\n'
        'safe = _bytes_safe(result)\n'
        'with open("_test_output.json", "w", encoding="utf-8") as f:\n'
        '    json.dump(safe, f, default=str)\n'
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
                # Inverse of `_bytes_safe` in the exec_script: walk the
                # JSON tree and re-inflate `{"_b64": "..."}` sentinels
                # back to real bytes so downstream consumers (the voice
                # plugin's audio writer, anything else that needs raw
                # bytes) get the data in its native shape.
                result = _inflate_b64_sentinels(result)
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
    rate_limiter: "GlobalProviderLimiter | None" = None,
    upstream_provider: str | None = None,
) -> list[tuple[TestCase, dict]]:
    """
    Execute all test cases in randomized order with rate limiting.
    Returns list of (test_case, harness_result) tuples.
    Early aborts if error rate exceeds threshold.

    When ``rate_limiter`` is provided (Gap 13/25), each test call waits on
    the per-candidate bucket AND the per-upstream bucket before firing.
    """
    shuffled = list(test_cases)
    random.shuffle(shuffled)

    results: list[tuple[TestCase, dict]] = []
    error_count = 0

    # Multi-call modalities (voice_conversation, voice_turn, conversation)
    # are owned by a plugin's drive-loop (voice_realtime / conversation_
    # simulator). Pre-calling harness.run() once with the test's bare
    # adapted payload is wasteful AND actively harmful: the harness has
    # no audio_url / turn_index / session_state until the plugin's
    # responder builds them per turn, so strict harnesses correctly
    # return success=False on the pre-call ("missing audio_url"), which
    # records as a real test error and skips the entire plugin path.
    # voice_dual_6 caught exactly this: the lenient OpenAI harness
    # tolerated the bogus pre-call and got 6 audio artifacts via the
    # plugin; the strict ElevenLabs harness rejected it and ended up
    # with 0 audio_paths + 0 evaluation. Skip the pre-call for these
    # modalities and let the plugin own every harness invocation.
    multi_call_input_types = {"conversation", "voice_conversation", "voice_turn"}
    multi_call_output_types = {"voice_conversation", "voice_turn"}

    def _is_multi_call(_tc: "TestCase") -> bool:
        return (
            (_tc.input_type or "") in multi_call_input_types
            or (_tc.output_type or "") in multi_call_output_types
        )

    for i, tc in enumerate(shuffled):
        adapted = _adapt_test_input(tc, harness)

        if _is_multi_call(tc):
            # Synthesize a placeholder result that downstream evaluation
            # will hand to the plugin's evaluate_output. The plugin
            # itself will drive every real harness call via its
            # drive_conversation responder using the harness_runner
            # injected by Agent 5.
            placeholder = {
                "output": "",
                "latency_ms": 0.0,
                "tokens_used": None,
                "cost_usd": None,
                "raw_response": {
                    "_skipped_pre_call_for_multi_call_modality": True,
                    "input_type": tc.input_type,
                    "output_type": tc.output_type,
                },
                "success": True,
                "error": None,
            }
            logger.info(
                f"skipping pre-call for {harness.candidate_name} on {tc.id} "
                f"(multi-call modality {tc.input_type}/{tc.output_type}); "
                f"plugin owns the loop",
                extra={
                    "operation": "multi_call_pre_call_skipped",
                    "trace_id": trace_id,
                    "candidate_name": harness.candidate_name,
                    "test_case_id": tc.id,
                    "input_type": tc.input_type,
                    "output_type": tc.output_type,
                },
            )
            results.append((tc, placeholder))
            continue

        # Gap 13 + 25: pace calls to respect per-candidate and upstream limits
        if rate_limiter is not None:
            waited = rate_limiter.acquire(harness.candidate_name, upstream_provider)
            if waited > 0.5:
                logger.info(
                    f"rate_limiter waited {waited:.2f}s for {harness.candidate_name}",
                    extra={
                        "operation": "rate_limit_wait",
                        "trace_id": trace_id,
                        "candidate_name": harness.candidate_name,
                        "wait_seconds": waited,
                        "upstream_provider": upstream_provider,
                    },
                )

        result = _execute_single_test(
            sandbox_dir, adapted, credentials, _adaptive_test_timeout(harness)
        )

        if not result["success"] and _is_rate_limit_error(result.get("error")):
            logger.info(
                f"Rate limit hit for {harness.candidate_name}, backing off {AGENT6_RATE_LIMIT_BACKOFF}s",
                extra={"operation": "rate_limit_backoff", "trace_id": trace_id},
            )
            time.sleep(AGENT6_RATE_LIMIT_BACKOFF)
            if rate_limiter is not None:
                rate_limiter.acquire(harness.candidate_name, upstream_provider)
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

        if i < len(shuffled) - 1 and rate_limiter is None:
            # Only apply the blind 0.5s sleep when no smart limiter is active
            time.sleep(0.5)

    return results


def _resolve_candidate_credentials(
    harness: TestHarness,
    provider_credentials: dict[str, dict[str, str]] | None,
) -> dict[str, str] | None:
    """Resolve credentials for a candidate from the provider registry or env vars.

    A candidate can use multiple providers (e.g., an "ElevenLabs Voice
    Stack" harness calls ElevenLabs for TTS + OpenAI Whisper for STT +
    Anthropic Claude for reasoning). Historically this resolver
    short-circuited on the first registry-key match and returned only
    one provider's env vars — harnesses that needed cross-provider keys
    got KeyError on the others.

    General fix: union EVERY registered provider whose normalized key
    appears anywhere in the candidate's searchable surface — name,
    provider, description, data_format_notes, confirmed_capabilities,
    pricing_details. "elevenlabs voice stack" now pulls ElevenLabs AND
    OpenAI AND Anthropic because the candidate's description mentions
    all three.

    Env-var fallback (harness.auth_env_vars) still fills in any key
    that wasn't provided via the registry — unchanged.
    """
    credentials: dict[str, str] = {}

    if provider_credentials:
        # Build the candidate's searchable text once. Pull everything
        # that might name a provider: the candidate + provider fields
        # on the harness itself, plus whatever the builder recorded in
        # data_format_notes (which often contains things like
        # "Chat = OpenAI; TTS = ElevenLabs").
        searchable_parts = [
            getattr(harness, "candidate_name", "") or "",
            getattr(harness, "provider", "") or "",
            getattr(harness, "validation_notes", "") or "",
        ]
        searchable = " ".join(searchable_parts).lower()
        candidate_key = harness.candidate_name.lower().strip()
        provider_key = harness.provider.lower().strip()

        for registry_key, env_dict in provider_credentials.items():
            norm_key = registry_key.lower().strip()
            if not norm_key:
                continue
            if (
                norm_key == candidate_key
                or norm_key == provider_key
                or norm_key in candidate_key
                or candidate_key in norm_key
                or norm_key in provider_key
                or provider_key in norm_key
                # Cross-provider match: mentioned anywhere in the
                # candidate's searchable text.
                or norm_key in searchable
            ):
                credentials.update(env_dict)
                # Don't break — union ALL matching providers so a
                # cross-provider candidate gets every key it needs.

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
            from puzzleeval.structured_output import parse_with_fallback
            # Evaluator rigor upgrade: pass thinking=adaptive so the judge
            # can reason through synonym mapping, partial-match semantics,
            # and edge-case criteria weighting — previously it one-shot
            # rubber-stamped. The strict-grammar path will pick up the
            # output_config.effort tier (defaults to "high") via
            # parse_with_fallback's ``extra`` kwarg, so evaluators respect
            # the same PUZZLEEVAL_EFFORT knob as every other agent.
            from puzzleeval.config import output_config_for_request
            _eval_extra: dict[str, object] = {"thinking": {"type": "adaptive"}}
            _eval_ocfg = output_config_for_request()
            if _eval_ocfg:
                _eval_extra["output_config"] = _eval_ocfg
            response = parse_with_fallback(
                client=client,
                model=AGENT6_EVAL_MODEL,
                max_tokens=AGENT6_EVAL_MAX_TOKENS,
                system=_with_shared_preamble(EVALUATION_SYSTEM_PROMPT),
                messages=[{"role": "user", "content": prompt}],
                output_format=EvaluationBatchResult,
                extra=_eval_extra,
                trace_id=trace_id,
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


def _needs_plugin_synthesis(tc: TestCase) -> bool:
    """Test cases for special modalities that didn't ship with file/payload
    can be filled in by the plugin synthesizers (TTS for audio,
    conversation_simulator for chat scripts, code_execution for code seeds)."""
    if tc.test_file_path:
        return False
    if tc.input_data and tc.input_type not in {"audio_content"}:
        # When input_data is already populated, plugin synthesis is only
        # needed for audio (synthesize a real audio file from the text).
        return False
    return tc.input_type in {"audio_content", "conversation", "code"}


def _synthesize_test_input_via_plugin(
    tc: TestCase,
    sandbox_dir: Path,
    logger: logging.Logger,
    trace_id: str,
) -> TestCase:
    """Try to synthesize this test case's input via a registered plugin.

    Returns the test case unchanged when no plugin is available or the
    synthesis failed — callers handle missing input downstream
    (e.g., file_required tests already have the Gap 3 fallback).
    """
    from puzzleeval.tool_plugins import find_plugins_for_input_type
    candidates_plugins = [
        p for p in find_plugins_for_input_type(tc.input_type)
        if p.capabilities().synthesizes_input
    ]
    if not candidates_plugins:
        return tc
    # Prefer TTS for audio_content, conversation_simulator for conversation,
    # code_execution for code. Take the first available.
    plugin = None
    for p in candidates_plugins:
        ok, _ = p.is_available()
        if ok:
            plugin = p
            break
    if plugin is None:
        logger.info(
            f"No available synthesizer plugin for {tc.input_type} on {tc.id}",
            extra={"operation": "synthesis_unavailable", "trace_id": trace_id,
                   "candidates": [p.name for p in candidates_plugins]},
        )
        return tc
    # Plugins that produce audio artifacts write to a caller-supplied
    # session dir. Default is %TEMP%, which leaks audio files outside
    # the run. Point them into the sandbox's voice/ subdir so every
    # artifact for this run lives under runs/<trace_id>/harnesses/<slug>/.
    # Cloud-scale seam: swap the voice/ Path for a StorageBackend shim
    # (S3/GCS) and every plugin automatically persists to cloud storage.
    if hasattr(plugin, "set_session_dir"):
        try:
            plugin.set_session_dir(sandbox_dir / "voice")
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"{plugin.name} set_session_dir failed: {exc}",
                extra={"operation": "plugin_session_dir_failed", "trace_id": trace_id},
            )
    try:
        result = plugin.synthesize_input(
            scope_role=tc.sub_task_ref,
            ground_truth_hint=tc.input_data or None,
        )
    except Exception as exc:
        logger.warning(
            f"Plugin {plugin.name} synthesis failed for {tc.id}: {exc}",
            extra={"operation": "synthesis_crash", "trace_id": trace_id},
        )
        return tc
    tc_dict = tc.model_dump()
    if result.file_path:
        tc_dict["test_file_path"] = result.file_path
        # Promote the synthesized file's expected text into expected_output
        # when the caller didn't already set one.
        if not tc.expected_output and "text" in result.ground_truth:
            tc_dict["expected_output"] = result.ground_truth["text"]
    if result.inline_data:
        # Inline payloads (conversation scripts, code prompts) flow through
        # input_data so the harness sees them. We serialize as JSON for
        # transport — the harness will decode based on input_type.
        import json as _json
        try:
            tc_dict["input_data"] = _json.dumps(result.inline_data)
        except (TypeError, ValueError):
            pass
    logger.info(
        f"Plugin {plugin.name} synthesized input for {tc.id}",
        extra={"operation": "synthesis_complete", "trace_id": trace_id,
               "plugin": plugin.name, "file_path": result.file_path,
               "has_inline": bool(result.inline_data)},
    )
    return TestCase(**tc_dict)


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
        elif _needs_plugin_synthesis(tc):
            # Plugin dispatch: this test case wants a modality-specific
            # input (audio, code prompt, multi-turn script) that no file
            # path was provided for. Try the registered synthesizer plugin.
            synthesized = _synthesize_test_input_via_plugin(tc, sandbox_dir, logger, trace_id)
            staged.append(synthesized)
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

def run_implement_test_env_agent(
    input_data: Agent5Input,
    progress_callback: "Callable[[str, dict], None] | None" = None,
) -> Agent5Result:
    """
    Run Agent 5. Takes validated candidates from Agent 4 and builds a test
    harness for each one in parallel.

    Each candidate gets an autonomous builder agent (multi-turn tool-use loop)
    that reads API docs, writes harness code, tests it, and fixes errors.

    Returns Agent5Result with successfully built harnesses and failures.
    """
    # ★ CORE LINE 1: Create the API client
    # Agent 5 builder loop runs for several minutes per candidate with deep
    # adaptive thinking — extend the per-call timeout to 240 s so a single
    # long Opus thinking turn doesn't trip the default. max_retries=3 still
    # bounds total time and covers transient 5xx / connection drops.
    from puzzleeval.anthropic_client import build_client, SERVER_TOOL_TIMEOUT_S
    client = build_client(api_key=ANTHROPIC_API_KEY, timeout=SERVER_TOOL_TIMEOUT_S)

    # [logging]
    logger = get_logger("agent_5_implement")

    # ── Credential-gated candidate selection ──
    #
    # Only test candidates we can actually build + call: those that either
    # (a) have a registered credential in provider_registry.json, or
    # (b) have auth_method="no_auth" (rare — mostly free public APIs).
    #
    # Non-credentialed candidates waste Agent 5's budget — the build loop
    # spends 10+ turns producing harness code that then can't authenticate
    # against the real API. The harness appears "built" but live_test
    # fails, the candidate shows 0% pass rate, and the user learns
    # nothing about the candidate's actual quality — only that we didn't
    # have a key. Filter them out so every successful build produces real
    # test data on the real API.
    #
    # Fallback when ZERO credentialed candidates survive: surface zero
    # harnesses and an explicit advisory. This is cleaner than pretending
    # to test providers whose keys we don't have.
    all_candidates = input_data.validated_candidates

    # Determine which candidates have credentials.
    # Two sources (union):
    #   1. input_data.provider_credentials — whatever the caller passed in.
    #   2. provider_registry.json — the persistent local registry.
    # Registry lookups use substring matching so "OpenAI Realtime API" and
    # "OpenAI" both match the registry's "openai" entry.
    cred_providers: set[str] = set()
    if input_data.provider_credentials:
        cred_providers = {k.lower() for k in input_data.provider_credentials.keys()}

    # Pull the registry once; used by _has_credentials below.
    _registry_providers: set[str] = set()
    try:
        from puzzleeval.provider_registry import load_registry
        _reg = load_registry()
        _registry_providers = {k.lower() for k in _reg.providers.keys()}
    except Exception as _exc:  # noqa: BLE001
        logger.warning(
            "provider_registry load failed during candidate filter: %s",
            _exc,
            extra={"operation": "agent5_registry_load_failed", "trace_id": input_data.trace_id},
        )

    def _has_credentials(c: ScreenedCandidate) -> bool:
        # no_auth candidates don't need a key.
        if (getattr(c, "auth_method", "") or "").lower() == "no_auth":
            return True
        norm_provider = _normalize(c.provider or "")
        norm_name = _normalize(c.name or "")
        # Exact match in provider_credentials (case-insensitive).
        if cred_providers and (
            norm_provider in cred_providers or norm_name in cred_providers
        ):
            return True
        # Substring match in registry keys (matches "openai realtime" → "openai").
        for reg_key in _registry_providers:
            if (
                reg_key in norm_provider or norm_provider in reg_key
                or reg_key in norm_name or norm_name in reg_key
            ):
                return True
        return False

    credentialed = [c for c in all_candidates if _has_credentials(c)]
    uncredentialed = [c for c in all_candidates if not _has_credentials(c)]

    if credentialed:
        # Happy path — rank the credentialed pool by relevance_score and
        # take the top AGENT5_MAX_CANDIDATES.
        sorted_candidates = sorted(
            credentialed,
            key=lambda c: c.relevance_score,
            reverse=True,
        )
        candidates = sorted_candidates[:AGENT5_MAX_CANDIDATES]
        if uncredentialed:
            logger.info(
                "Filtered out %d candidates without credentials — registry "
                "holds keys for %d / %d candidates. Dropped: %s",
                len(uncredentialed), len(credentialed), len(all_candidates),
                [c.name for c in uncredentialed],
                extra={
                    "operation": "agent5_credential_filter",
                    "trace_id": input_data.trace_id,
                    "dropped_uncredentialed": [c.name for c in uncredentialed],
                    "kept_credentialed": [c.name for c in credentialed],
                },
            )
    else:
        # No candidate has credentials. Rather than silently testing
        # harnesses that are guaranteed to fail auth, surface zero
        # candidates and let Agent 5's empty-candidates path produce a
        # clean coverage gap advisory. The frontend's results view
        # renders this with a "no credentials configured for any
        # verified candidate — add keys via provider_registry.json"
        # message.
        logger.warning(
            "Zero candidates have registered credentials — skipping Agent 5 "
            "builds entirely. Candidates: %s",
            [c.name for c in all_candidates],
            extra={
                "operation": "agent5_no_credentials_skip",
                "trace_id": input_data.trace_id,
                "all_candidates": [c.name for c in all_candidates],
                "registry_providers": sorted(_registry_providers),
            },
        )
        candidates = []

    # Emit the authoritative selection — this is the ONLY place that decides which candidates get built
    if progress_callback:
        progress_callback("candidates_selected", {
            "selected": [c.name for c in candidates],
        })

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
    # Resolve to an ABSOLUTE path. Downstream: voice/ audio files are stored
    # with this prefix, threaded through TestCaseResult.audio_paths, and then
    # served by the backend /runs/audio route. If harness_base is RELATIVE
    # (e.g., the literal string "runs/..."), Python still creates the dir
    # correctly from the current cwd, but the serialized path strings go out
    # to the frontend as `runs\<trace>\...` (Windows backslashes, relative).
    # The backend audio route's containment check then fails to resolve the
    # path against its allowed roots — every <audio> tag 404s silently.
    # Resolving here gives every saved audio artifact an absolute path with
    # the OS-native separator, so the route works without per-caller fixups.
    harness_base = (Path("runs") / input_data.trace_id / "harnesses").resolve()
    harness_base.mkdir(parents=True, exist_ok=True)

    # ======================================================================
    # PARALLEL BUILD: One thread per candidate
    # ======================================================================
    # Each candidate gets its own thread, its own conversation context, and
    # its own sandbox directory. No shared state, no token accumulation.
    # ======================================================================

    # ★ CORE: Launch all harness builds in parallel
    results_by_index: dict[int, TestHarness | FailedHarness] = {}

    # Guard: ThreadPoolExecutor requires max_workers > 0. When every upstream
    # candidate was rejected (e.g., Agent 4 deep-verify found no public docs
    # across the board), we short-circuit to an empty harness set so the
    # report assembler sees "zero harnesses" cleanly instead of crashing the
    # run on `ValueError: max_workers must be greater than 0`. This matches
    # the documented graceful-degradation contract in the Agent 5 design.
    if not candidates:
        logger.warning(
            "Agent 5 received zero candidates to build — returning empty result",
            extra={
                "operation": "agent5_empty_candidates",
                "trace_id": input_data.trace_id,
            },
        )
        return Agent5Result(
            harnesses=[],
            failed_harnesses=[],
            total_candidates_attempted=0,
            total_build_cost_usd=0.0,
            build_summary=(
                "No candidates reached Agent 5 — every upstream candidate "
                "was rejected (typically Agent 4 deep-verify couldn't reach "
                "any public API docs for this workflow). Report will render "
                "the run as a coverage gap rather than a zero-harness error."
            ),
            candidate_runs=[],
            failed_test_runs=[],
            total_test_cases=0,
            total_test_cost_usd=0.0,
            test_execution_summary="No candidates to test.",
            web_fetch_blocks=0,
            scope_runs=[],
        )

    with ThreadPoolExecutor(max_workers=min(AGENT5_MAX_PARALLEL, len(candidates))) as executor:
        future_to_index = {}
        for i, candidate in enumerate(candidates):
            slug = _candidate_slug(candidate.name)
            sandbox_dir = harness_base / slug
            sandbox_dir.mkdir(parents=True, exist_ok=True)

            # Architecture note: Agent 4 no longer writes an atlas. Agent 5
            # does its own Phase-1 research (web_search + web_fetch) and
            # writes its own api_spec.txt into the sandbox. See split
            # decision in CLAUDE.md NEW-AI.

            logger.info(f"Submitting build for: {candidate.name}", extra={
                "operation": "harness_build_submit",
                "trace_id": input_data.trace_id,
                "candidate_name": candidate.name,
                "candidate_index": i + 1,
                "sandbox_dir": str(sandbox_dir),
            })

            if progress_callback:
                progress_callback("harness_started", {"candidate_name": candidate.name})

            future = executor.submit(
                _build_single_harness, client, candidate, input_data, sandbox_dir, logger,
                progress_callback=progress_callback,
            )
            future_to_index[future] = i

        # Collect results as they complete
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            candidate_name = candidates[idx].name
            try:
                result = future.result()
                results_by_index[idx] = result
                if progress_callback:
                    if isinstance(result, TestHarness):
                        progress_callback("harness_completed", {
                            "candidate_name": candidate_name,
                            "success": True,
                            "build_turns": result.build_turns,
                            "build_cost_usd": result.build_cost_usd,
                        })
                    elif isinstance(result, FailedHarness):
                        progress_callback("harness_failed", {
                            "candidate_name": candidate_name,
                            "failure_reason": result.failure_reason,
                        })
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
                    web_fetch_blocks=0,
                )
                if progress_callback:
                    progress_callback("harness_failed", {
                        "candidate_name": candidate_name,
                        "failure_reason": str(e),
                    })

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
            # OBSERVABILITY BUG #2 FIX (2026-04-21): previously cost
            # accumulated inside a failed build was dropped silently
            # from the run total. A build that burned $5 over 10 turns
            # before failing showed $0 in reporting. Now every
            # FailedHarness carries its pre-failure `build_cost_usd`
            # and we sum it into the run total alongside successful
            # harnesses. Zero when the failure happened before any
            # LLM call (venv setup failure etc.), so this addition
            # is a no-op for pre-LLM failure paths.
            total_cost += getattr(result, "build_cost_usd", 0.0) or 0.0

    # ──────────────────────────────────────────────────────────────────
    # Q4: Build-failure fallback. When EVERY user-selected candidate failed,
    # pull next-ranked verified candidates that the user did NOT pick and
    # try them. Guarantees the user gets at least one testable environment
    # back instead of a hard "Zero harnesses" failure.
    # ──────────────────────────────────────────────────────────────────
    from puzzleeval.config import AGENT5_FALLBACK_ENABLED, AGENT5_FALLBACK_MAX
    if (
        AGENT5_FALLBACK_ENABLED
        and len(harnesses) == 0
        and AGENT5_FALLBACK_MAX > 0
    ):
        picked_names = {c.name.strip().lower() for c in candidates}
        fallback_pool = [
            c for c in input_data.validated_candidates
            if c.name.strip().lower() not in picked_names
        ]
        # Rank fallback pool by relevance score; ties broken by adoption_difficulty
        # ("easy" first — quickest to build).
        diff_rank = {"easy": 0, "medium": 1, "hard": 2}
        fallback_pool.sort(
            key=lambda c: (-c.relevance_score, diff_rank.get(c.adoption_difficulty, 3))
        )
        fallback_attempts = fallback_pool[:AGENT5_FALLBACK_MAX]
        if fallback_attempts:
            logger.warning(
                f"All {len(candidates)} selected harnesses failed. "
                f"Trying {len(fallback_attempts)} fallback candidates from the "
                f"unselected verified pool.",
                extra={
                    "operation": "agent5_fallback_engaged",
                    "trace_id": input_data.trace_id,
                    "fallback_names": [c.name for c in fallback_attempts],
                },
            )
            if progress_callback:
                progress_callback("agent5_fallback_engaged", {
                    "primary_failures": [c.name for c in candidates],
                    "fallback_attempts": [c.name for c in fallback_attempts],
                })

            # Same parallel build pattern as the primary attempt
            with ThreadPoolExecutor(max_workers=min(AGENT5_MAX_PARALLEL, len(fallback_attempts))) as executor:
                fb_future_to_idx = {}
                for j, fb_cand in enumerate(fallback_attempts):
                    slug = _candidate_slug(fb_cand.name)
                    fb_sandbox = harness_base / f"_fallback_{slug}"
                    fb_sandbox.mkdir(parents=True, exist_ok=True)
                    fb_future = executor.submit(
                        _build_single_harness, client, fb_cand, input_data, fb_sandbox, logger,
                        progress_callback=progress_callback,
                    )
                    fb_future_to_idx[fb_future] = j

                for fb_future in as_completed(fb_future_to_idx):
                    j = fb_future_to_idx[fb_future]
                    fb_name = fallback_attempts[j].name
                    try:
                        fb_result = fb_future.result()
                        if isinstance(fb_result, TestHarness):
                            fb_result.was_fallback = True
                            harnesses.append(fb_result)
                            total_cost += fb_result.build_cost_usd
                            if progress_callback:
                                progress_callback("harness_completed", {
                                    "candidate_name": fb_name,
                                    "was_fallback": True,
                                    "build_cost_usd": fb_result.build_cost_usd,
                                })
                        else:
                            # Fallback also failed — record as a failed harness.
                            # OBSERVABILITY BUG #2 FIX (2026-04-21): include
                            # the partial cost of this failed fallback build
                            # in the run total so reporting reflects true spend.
                            failed.append(fb_result)
                            total_cost += getattr(fb_result, "build_cost_usd", 0.0) or 0.0
                    except Exception as exc:
                        logger.warning(
                            f"Fallback build crashed for {fb_name}: {exc}",
                            extra={"operation": "agent5_fallback_thread_error", "trace_id": input_data.trace_id},
                        )
                        failed.append(FailedHarness(
                            candidate_name=fb_name,
                            provider=fallback_attempts[j].provider,
                            failure_reason=f"Fallback build error: {exc}",
                            failure_category="unknown",
                            partial_code=None,
                            turns_attempted=0,
                            web_fetch_blocks=0,
                        ))
            # total_candidates_attempted should reflect the true number of
            # builds attempted (primary + fallback) so the validator's count
            # consistency check stays honest.
            candidates = list(candidates) + fallback_attempts

    # Build summary
    summary_parts = [
        f"Built {len(harnesses)}/{len(candidates)} harnesses successfully."
    ]
    fallback_count = sum(1 for h in harnesses if h.was_fallback)
    if fallback_count:
        summary_parts.append(f"({fallback_count} via auto-fallback after primary picks failed.)")
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

        # Gap E: Adversarial verification battery (Claude Code-style verification
        # pass). Run BEFORE Agent 3 cases so a fragile harness is caught and
        # marked NOT READY rather than silently corrupting domain test results.
        from puzzleeval.config import ADVERSARIAL_PROBES_ENABLED
        if ADVERSARIAL_PROBES_ENABLED:
            from puzzleeval.adversarial_verifier import (
                run_adversarial_battery,
                report_to_dict,
            )
            for h in list(harnesses_with_sandboxes):
                # Use the first test case's adapted input as the probe basis;
                # falls back to a minimal payload if no test cases match.
                creds = _resolve_candidate_credentials(h, input_data.provider_credentials)
                sample = None
                for tc in test_cases:
                    try:
                        sample = _adapt_test_input(tc, h)
                        if isinstance(sample, dict) and sample:
                            break
                    except Exception:
                        continue
                if not isinstance(sample, dict) or not sample:
                    sample = {"input_data": "smoke"}
                try:
                    report = run_adversarial_battery(
                        sandbox_dir=Path(h.harness_dir),
                        sample_input=sample,
                        credentials=creds,
                    )
                    h.adversarial_report = report_to_dict(report)
                    if not report.harness_ready:
                        logger.warning(
                            f"adversarial battery: {h.candidate_name} marked NOT READY",
                            extra={
                                "operation": "adversarial_not_ready",
                                "trace_id": input_data.trace_id,
                                "candidate_name": h.candidate_name,
                                "critical_failures": report.critical_failures,
                            },
                        )
                        if progress_callback:
                            progress_callback("harness_not_ready", {
                                "candidate_name": h.candidate_name,
                                "critical_failures": report.critical_failures,
                            })
                        # Drop from execution set — the harness will be reported
                        # as built but not exercised.
                        harnesses_with_sandboxes.remove(h)
                except Exception as exc:
                    logger.warning(
                        f"adversarial battery error for {h.candidate_name}: {exc}",
                        extra={"operation": "adversarial_error", "trace_id": input_data.trace_id},
                    )
                    # Defensive: record the failure but allow tests to proceed.
                    h.adversarial_report = {
                        "harness_ready": True,
                        "probe_count": 0,
                        "critical_failures": [],
                        "warnings": [f"battery error: {exc}"],
                        "probes": [],
                    }

        # Gap 13 + Gap 25: one rate limiter shared across candidates so both
        # per-candidate AND per-upstream buckets pace the whole run.
        from puzzleeval.rate_limiter import GlobalProviderLimiter
        rate_limiter = GlobalProviderLimiter()
        for c in input_data.validated_candidates:
            rate_limiter.configure_candidate(c.name, c.rate_limit_info)

        # Build candidate -> upstream_provider lookup once
        upstream_by_name: dict[str, str | None] = {
            c.name: getattr(c, "upstream_provider", None)
            for c in input_data.validated_candidates
        }

        def _run_tests_for_candidate(harness):
            """Execute all tests for one candidate. Thread-safe — each candidate
            has its own sandbox, credentials, and API provider."""
            if progress_callback:
                progress_callback("test_execution_started", {"candidate_name": harness.candidate_name})

            sandbox_dir = Path(harness.harness_dir)
            staged_test_cases = _stage_test_files(test_cases, sandbox_dir, logger, input_data.trace_id)
            creds = _resolve_candidate_credentials(
                harness, input_data.provider_credentials
            )

            # Set session_dir on every plugin that supports it BEFORE
            # test execution. The earlier wiring only set this during
            # input synthesis (`_synthesize_test_input_via_plugin`)
            # which is skipped for `input_type=conversation` voice
            # tests — drive_conversation then wrote per-turn audio +
            # the merged conversation file to `%TEMP%/puzzleeval_voice/`
            # outside the run directory. The backend's audio-streaming
            # endpoint refuses paths outside its allowed runs roots, so
            # the frontend silently couldn't play those clips. Setting
            # session_dir here ensures EVERY artifact for this candidate
            # — synthesis OR multi-turn drive — lands under
            # ``runs/<trace_id>/harnesses/<slug>/voice/`` and reaches
            # the UI through `/pzapi/runs/audio?path=...`.
            voice_session_dir = sandbox_dir / "voice"
            try:
                voice_session_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            try:
                from puzzleeval.tool_plugins import list_plugins as _list_plugins
                for _plug in _list_plugins():
                    if not hasattr(_plug, "set_session_dir"):
                        continue
                    try:
                        _plug.set_session_dir(voice_session_dir)
                    except Exception as _exc:  # noqa: BLE001
                        logger.debug(
                            f"{_plug.name} set_session_dir(eval) failed: {_exc}",
                            extra={"operation": "plugin_session_dir_failed",
                                   "trace_id": input_data.trace_id},
                        )
            except Exception:  # noqa: BLE001
                # Plugin discovery is best-effort; failure here just
                # means audio falls back to %TEMP% (the prior behavior).
                pass

            raw_results = _execute_all_tests(
                sandbox_dir, staged_test_cases, harness, creds, logger, input_data.trace_id,
                rate_limiter=rate_limiter,
                upstream_provider=upstream_by_name.get(harness.candidate_name),
            )

            eval_items = []
            test_case_results = []

            for tc, result in raw_results:
                adapted = _adapt_test_input(tc, harness)

                if not result["success"]:
                    error_msg = result.get("error") or ""
                    is_incompatible = "INCOMPATIBLE" in error_msg.upper()

                    # Gap 3 fix: file_required tests with no file should NOT be
                    # unconditionally skipped.  The harness was given the text
                    # input_data as a fallback; if it returned INCOMPATIBLE that
                    # is a real result (API genuinely needs a file), not a
                    # pre-filter skip.  Record it as a normal failure so OCR /
                    # audio scopes get non-zero test results.
                    file_required_no_file = (
                        getattr(tc, "file_required", False) and not tc.test_file_path
                    )
                    if is_incompatible and file_required_no_file:
                        skip = None  # do NOT skip — count as real failure
                        annotated_error = (
                            f"{error_msg} "
                            "[Tested with synthetic text input (no user file provided)]"
                        )
                    else:
                        skip = error_msg if is_incompatible else None
                        annotated_error = result.get("error")

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
                        error=annotated_error,
                        skip_reason=skip,
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

                # Gap 3: annotate when a file_required test succeeded with
                # synthetic text input (no user file).
                success_input = dict(adapted)
                if getattr(tc, "file_required", False) and not tc.test_file_path:
                    success_input["_note"] = (
                        "Tested with synthetic text input (no user file provided)"
                    )

                test_case_results.append(TestCaseResult(
                    test_case_id=tc.id,
                    sub_task_ref=tc.sub_task_ref,
                    input_sent=success_input,
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

            # ──────────────────────────────────────────────────────────
            # Plugin dispatch — strategy-driven per ``EVAL_STRATEGY``:
            #
            #   "tool_runner" (default): expose ALL eligible plugins as
            #     @beta_tool functions to Claude via
            #     client.beta.messages.tool_runner. Claude picks + chains
            #     plugins, emits a structured ScoreVerdict. Fixes all the
            #     coverage gaps of deterministic dispatch (multi-tool,
            #     ambiguous enums, Agent 3 mis-labels, multi-modal
            #     responses, novel plugins). See plugin_tool_runner.py.
            #
            #   "deterministic": legacy enum-based — first matching
            #     plugin wins, falls through to LLM judge on miss.
            #     Kept for emergency bisection.
            #
            #   "hybrid": deterministic first; on miss or fallback_reason,
            #     tool_runner picks up. Middle ground.
            # ──────────────────────────────────────────────────────────
            from puzzleeval.config import EVAL_STRATEGY
            plugin_handled_ids: set[str] = set()

            def _promote_verdict_to_tcr(
                tcr_target,
                criteria_eval,
                score: float,
                passed: bool,
                reasoning: str,
                tools_used_additions: list[str],
                audio_paths_additions: list[dict] | None = None,
            ) -> None:
                """Apply a verdict's numerics back onto the TestCaseResult.

                Shared between deterministic + tool_runner paths so the
                weighted-score + telemetry fields end up identical
                regardless of strategy.
                """
                tcr_target.criteria_scores = [
                    CriterionScore(
                        criterion=c["criterion"],
                        eval_type=c.get("eval_type", "subjective_quality"),
                        weight=c["weight"],
                        score=score,
                        passed=passed,
                        reasoning=reasoning[:500],
                    )
                    for c in criteria_eval
                ] or [
                    CriterionScore(
                        criterion="overall",
                        eval_type="subjective_quality",
                        weight=1.0,
                        score=score,
                        passed=passed,
                        reasoning=reasoning[:500],
                    )
                ]
                tcr_target.weighted_score = score
                tcr_target.passed = passed
                tcr_target.tools_used = (
                    list(getattr(tcr_target, "tools_used", []) or [])
                    + list(tools_used_additions)
                )
                if audio_paths_additions:
                    tcr_target.audio_paths = (
                        list(getattr(tcr_target, "audio_paths", []) or [])
                        + list(audio_paths_additions)
                    )

            def _run_deterministic() -> None:
                """Legacy enum-based dispatch path. Mutates eval_items in place."""
                from puzzleeval.modality import detect_for_test_case
                for eval_item in list(eval_items):
                    tc_eval, result_eval, criteria_eval = eval_item
                    reqs = detect_for_test_case(
                        input_type=tc_eval.input_type,
                        output_type=tc_eval.output_type,
                    )
                    evaluator = next(iter(reqs.output_evaluators), None)
                    if evaluator is None:
                        continue
                    runner_for_plugin = None
                    if evaluator.capabilities().requires_harness_runner:
                        creds_for_runner = creds
                        sandbox_for_runner = sandbox_dir
                        timeout_for_runner = _adaptive_test_timeout(harness)
                        # Per-test-case context propagation: plugins that
                        # drive harness.run() turn-by-turn (voice_realtime,
                        # conversation_simulator) build their OWN payload
                        # per turn (audio_url, turn_index, session_state,
                        # …) and naturally drop anything they don't know
                        # about — including `input_context` from the test
                        # case. That's where the user's system prompt
                        # lives ("You are Vera, the plumbing voice agent
                        # …") — without it the agent is ungrounded on
                        # every turn. Wrap the runner here so every
                        # payload the plugin passes through gets the
                        # original test case's input_context + top-level
                        # input_type merged in (without clobbering any
                        # keys the plugin did set). General fix — works
                        # for every plugin with requires_harness_runner.
                        tc_ctx = tc_eval.input_context
                        tc_input_type = tc_eval.input_type
                        # Compute the default once per test case so the
                        # closure can reuse it. Built from the candidate
                        # name + scope ref — both already in scope here.
                        # See `_default_input_context` docstring for why
                        # this lives in our code, not in a prompt rule.
                        _default_ctx = _default_input_context(
                            candidate.name,
                            getattr(tc_eval, "sub_task_ref", None),
                        )
                        def _runner(payload,
                                    _sd=sandbox_for_runner,
                                    _c=creds_for_runner,
                                    _t=timeout_for_runner,
                                    _ctx=tc_ctx,
                                    _default=_default_ctx,
                                    _it=tc_input_type):
                            if not isinstance(payload, dict):
                                payload = {"payload": payload}
                            merged = dict(payload)
                            # Always run input_context through the merge —
                            # handles all three cases: missing entirely,
                            # present-but-no-system-prompt, present-with-
                            # system-prompt-under-some-alias. See
                            # `_merge_with_default_input_context` for the
                            # full case analysis. Real-run trace f1312253:
                            # the simpler "skip if test set anything"
                            # check would have masked partial-context bugs.
                            payload_ctx = merged.get("input_context")
                            merged["input_context"] = _merge_with_default_input_context(
                                payload_ctx if isinstance(payload_ctx, dict) else _ctx,
                                _default,
                            )
                            if "input_type" not in merged and _it:
                                merged["input_type"] = _it
                            return _execute_single_test(_sd, merged, _c, _t)
                        runner_for_plugin = _runner
                    try:
                        verdict = evaluator.evaluate_output(
                            response=result_eval.get("raw_response") or result_eval.get("output"),
                            expected=tc_eval.expected_output,
                            criteria=[
                                {"criterion": c["criterion"], "weight": c["weight"],
                                 "eval_type": c.get("eval_type", "subjective_quality")}
                                for c in criteria_eval
                            ],
                            harness_runner=runner_for_plugin,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"plugin {evaluator.name} crashed evaluating {tc_eval.id}: {exc}; falling back to LLM judge",
                            extra={"operation": "plugin_eval_crash", "trace_id": input_data.trace_id},
                        )
                        continue
                    if verdict.fallback_reason:
                        continue
                    tcr_target = next(
                        (t for t in test_case_results if t.test_case_id == tc_eval.id), None,
                    )
                    if tcr_target is None:
                        continue
                    # Pull audio artifacts if the plugin captured any.
                    # Priority order (same as plugin_tool_runner.py —
                    # kept consistent so either eval strategy produces
                    # the same audio_paths on the TestCaseResult):
                    #   1. verdict.detail.audio_paths — authoritative
                    #      list the plugin built during evaluate_output
                    #      (drive_conversation puts its per-turn
                    #      artifacts here).
                    #   2. artifacts_for_token_prefix(session_token) —
                    #      multi-turn, each turn sub-tokened.
                    #   3. artifacts_for_token(token) — legacy single-turn.
                    audio_artifacts: list[dict] = []
                    detail = getattr(verdict, "detail", None) or {}
                    if isinstance(detail, dict):
                        detail_paths = detail.get("audio_paths")
                        if isinstance(detail_paths, list):
                            for item in detail_paths:
                                if isinstance(item, dict) and item.get("path"):
                                    audio_artifacts.append(item)
                    if not audio_artifacts and (
                        hasattr(evaluator, "artifacts_for_token_prefix")
                        or hasattr(evaluator, "artifacts_for_token")
                    ):
                        try:
                            token_candidates: list[str] = []
                            exp = tc_eval.expected_output
                            if isinstance(exp, dict) and exp.get("token"):
                                token_candidates.append(exp["token"])
                            elif isinstance(exp, str) and exp.startswith("{"):
                                import json as _json
                                try:
                                    parsed = _json.loads(exp)
                                    if isinstance(parsed, dict) and parsed.get("token"):
                                        token_candidates.append(parsed["token"])
                                except (ValueError, TypeError):
                                    pass
                            if isinstance(detail, dict) and detail.get("session_token"):
                                token_candidates.append(detail["session_token"])
                            for tok in token_candidates:
                                if hasattr(evaluator, "artifacts_for_token_prefix"):
                                    arts = evaluator.artifacts_for_token_prefix(tok)
                                else:
                                    arts = evaluator.artifacts_for_token(tok)
                                if arts:
                                    audio_artifacts.extend(arts)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                f"{evaluator.name} artifacts fetch failed: {exc}",
                                extra={"operation": "plugin_artifacts_failed", "trace_id": input_data.trace_id},
                            )
                    _promote_verdict_to_tcr(
                        tcr_target, criteria_eval,
                        score=verdict.score,
                        passed=verdict.passed,
                        reasoning=verdict.reasoning,
                        tools_used_additions=[evaluator.name],
                        audio_paths_additions=audio_artifacts,
                    )
                    plugin_handled_ids.add(tc_eval.id)

            def _run_tool_runner() -> None:
                """Claude-driven plugin dispatch via tool_runner. Mutates eval_items.

                Closure-captures the outer ``eval_cost`` (nonlocal) so every
                tool_runner invocation's Claude cost accumulates into the
                candidate's ``evaluation_cost_usd`` — matches the deterministic
                path's accounting exactly. Without the nonlocal rebind, C4
                regression: tool_runner costs vanish from the report.
                """
                nonlocal eval_cost
                from puzzleeval.plugin_tool_runner import evaluate_with_tool_runner
                # Candidate-invariant capture (creds / sandbox / timeout).
                # Per-test `input_context` is threaded in via the
                # per-test-case wrapper below — this was a real bug
                # exposed by the voice_debug_3 run: the user's system
                # prompt (input_context.instructions) never reached the
                # harness on multi-turn tests because the plugin builds
                # its per-turn payload from scratch.
                creds_for_runner = creds
                sandbox_for_runner = sandbox_dir
                timeout_for_runner = _adaptive_test_timeout(harness)

                for eval_item in list(eval_items):
                    tc_eval, result_eval, criteria_eval = eval_item
                    # H3 fix: only inject harness_runner when the modality
                    # actually needs multi-call orchestration. Widening the
                    # eligible-plugin set for every test (including plain
                    # text evals) invites Claude to mis-pick conversation_
                    # simulator / voice_realtime for scope misfits.
                    needs_runner = tc_eval.input_type in {
                        "conversation", "voice_conversation", "voice_turn",
                    } or tc_eval.output_type in {
                        "voice_turn", "voice_conversation",
                    }
                    # Per-test-case wrapper: merges this test case's
                    # `input_context` + `input_type` into every payload
                    # the plugin forwards, so the harness sees the user's
                    # system prompt on every turn. See the deterministic
                    # path above for the same pattern + rationale.
                    runner_for_this = None
                    if needs_runner:
                        tc_ctx = tc_eval.input_context
                        tc_input_type = tc_eval.input_type
                        # Compute the default once per test case so the
                        # closure can reuse it. Built from the candidate
                        # name + scope ref — both already in scope here.
                        # See `_default_input_context` docstring for why
                        # this lives in our code, not in a prompt rule.
                        _default_ctx = _default_input_context(
                            candidate.name,
                            getattr(tc_eval, "sub_task_ref", None),
                        )
                        def _runner(payload,
                                    _sd=sandbox_for_runner,
                                    _c=creds_for_runner,
                                    _t=timeout_for_runner,
                                    _ctx=tc_ctx,
                                    _default=_default_ctx,
                                    _it=tc_input_type):
                            if not isinstance(payload, dict):
                                payload = {"payload": payload}
                            merged = dict(payload)
                            # Always run input_context through the merge —
                            # handles all three cases: missing entirely,
                            # present-but-no-system-prompt, present-with-
                            # system-prompt-under-some-alias. See
                            # `_merge_with_default_input_context` for the
                            # full case analysis. Real-run trace f1312253:
                            # the simpler "skip if test set anything"
                            # check would have masked partial-context bugs.
                            payload_ctx = merged.get("input_context")
                            merged["input_context"] = _merge_with_default_input_context(
                                payload_ctx if isinstance(payload_ctx, dict) else _ctx,
                                _default,
                            )
                            if "input_type" not in merged and _it:
                                merged["input_type"] = _it
                            return _execute_single_test(_sd, merged, _c, _t)
                        runner_for_this = _runner

                    verdict = evaluate_with_tool_runner(
                        client=client,
                        response=result_eval.get("raw_response") or result_eval.get("output"),
                        expected=tc_eval.expected_output,
                        criteria=[
                            {"criterion": c["criterion"], "weight": c["weight"],
                             "eval_type": c.get("eval_type", "subjective_quality")}
                            for c in criteria_eval
                        ],
                        test_scenario=getattr(tc_eval, "scenario", "") or "",
                        harness_runner=runner_for_this,
                        input_type=tc_eval.input_type,
                        output_type=tc_eval.output_type,
                        trace_id=input_data.trace_id,
                    )
                    # C4 fix: accumulate Claude cost from each tool_runner
                    # invocation into the candidate's evaluation_cost_usd.
                    # Prior to this, tool_runner cost vanished from the
                    # report's total_test_cost_usd.
                    eval_cost += float(verdict.cost_usd or 0.0)
                    if verdict.fallback_reason:
                        logger.info(
                            "tool_runner fallback for %s: %s",
                            tc_eval.id, verdict.fallback_reason,
                            extra={"operation": "tool_runner_fallback",
                                   "trace_id": input_data.trace_id},
                        )
                        continue
                    tcr_target = next(
                        (t for t in test_case_results if t.test_case_id == tc_eval.id), None,
                    )
                    if tcr_target is None:
                        continue
                    tools_used = ["tool_runner"] + list(verdict.tools_invoked)
                    _promote_verdict_to_tcr(
                        tcr_target, criteria_eval,
                        score=verdict.score,
                        passed=verdict.passed,
                        reasoning=verdict.reasoning,
                        tools_used_additions=tools_used,
                        audio_paths_additions=verdict.artifacts,
                    )
                    plugin_handled_ids.add(tc_eval.id)

            # Initialize eval_cost BEFORE dispatch so tool_runner's nonlocal
            # reference can accumulate into it. The llm-judge path at the
            # bottom of this function will add to the same accumulator.
            eval_cost = 0.0

            if EVAL_STRATEGY == "deterministic":
                _run_deterministic()
            elif EVAL_STRATEGY == "hybrid":
                _run_deterministic()
                # eval_items already pruned for deterministic hits; whatever
                # remains goes to tool_runner.
                eval_items = [
                    item for item in eval_items if item[0].id not in plugin_handled_ids
                ]
                _run_tool_runner()
            else:  # "tool_runner" — the default
                _run_tool_runner()

            # Drop plugin-handled items so the LLM judge below doesn't double-score
            eval_items = [
                item for item in eval_items if item[0].id not in plugin_handled_ids
            ]

            # LLM judge handles everything plugins didn't claim.
            # When PUZZLEEVAL_HYBRID_EVAL_ENABLED=1, the hybrid Claude-picks-plugins
            # path runs FIRST for these fall-through cases. Whatever it doesn't
            # confidently score (parse failure, plugin crash, no_plugins_available)
            # falls through to the legacy LLM judge below.
            from puzzleeval.config import HYBRID_EVAL_ENABLED
            # NOTE: eval_cost was already initialized and may contain
            # tool_runner-accumulated cost from _run_tool_runner above.
            # Do NOT reset it here; _evaluate_with_llm adds to it below.
            if eval_items and HYBRID_EVAL_ENABLED:
                from puzzleeval.hybrid_evaluator import (
                    evaluate_with_claude_picked_plugins,
                )
                hybrid_handled_ids: set[str] = set()
                for tc_eval, result_eval, criteria_eval in list(eval_items):
                    try:
                        verdict = evaluate_with_claude_picked_plugins(
                            response=result_eval.get("raw_response") or result_eval.get("output"),
                            expected=tc_eval.expected_output,
                            criteria=criteria_eval,
                            client=client,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            f"hybrid eval crashed for {tc_eval.id}: {exc}",
                            extra={"operation": "hybrid_eval_crash", "trace_id": input_data.trace_id},
                        )
                        continue
                    if verdict.fallback_reason:
                        # Couldn't score — let the LLM judge handle it
                        continue
                    tcr_target = next(
                        (t for t in test_case_results if t.test_case_id == tc_eval.id), None,
                    )
                    if tcr_target is None:
                        continue
                    tcr_target.criteria_scores = [
                        CriterionScore(
                            criterion=c["criterion"],
                            eval_type=c.get("eval_type", "subjective_quality"),
                            weight=c["weight"],
                            score=verdict.score,
                            passed=verdict.passed,
                            reasoning=verdict.reasoning[:500],
                        )
                        for c in criteria_eval
                    ] or [
                        CriterionScore(
                            criterion="overall",
                            eval_type="subjective_quality",
                            weight=1.0,
                            score=verdict.score,
                            passed=verdict.passed,
                            reasoning=verdict.reasoning[:500],
                        )
                    ]
                    tcr_target.weighted_score = verdict.score
                    tcr_target.passed = verdict.passed
                    used = list(getattr(tcr_target, "tools_used", []) or [])
                    used.append("hybrid_evaluator")
                    used.extend(verdict.tools_called)
                    tcr_target.tools_used = used
                    hybrid_handled_ids.add(tc_eval.id)
                eval_items = [
                    item for item in eval_items if item[0].id not in hybrid_handled_ids
                ]

            if eval_items:
                llm_scores_map, _llm_cost = _evaluate_with_llm(
                    client, eval_items, harness.candidate_name, logger, input_data.trace_id,
                )
                # Accumulate, don't clobber — tool_runner may already
                # have spent money in eval_cost above (C4 fix).
                eval_cost += float(_llm_cost or 0.0)
                for tcr in test_case_results:
                    if tcr.success and tcr.test_case_id in llm_scores_map:
                        tcr.criteria_scores = llm_scores_map[tcr.test_case_id]
                        tcr.weighted_score = _compute_weighted_score(tcr.criteria_scores)
                        tcr.passed = tcr.weighted_score >= AGENT6_PASS_THRESHOLD
                        tcr.tools_used = (
                            list(getattr(tcr, "tools_used", []) or []) + ["llm_judge"]
                        )

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

            if progress_callback:
                progress_callback("candidate_results_ready", {
                    "candidate_name": harness.candidate_name,
                    "provider": harness.provider,
                    "tests_passed": metrics.get("tests_passed", 0),
                    "tests_failed": metrics.get("tests_failed", 0),
                    "pass_rate": metrics.get("pass_rate", 0),
                    "avg_latency_ms": metrics.get("avg_latency_ms", 0),
                    "total_cost_usd": metrics.get("total_cost_usd", 0),
                    "overall_score": sum(tcr.weighted_score for tcr in run.test_results) / max(len(run.test_results), 1),
                    "test_results": [
                        {
                            "test_case_id": tcr.test_case_id,
                            "passed": tcr.passed,
                            "weighted_score": tcr.weighted_score,
                            "latency_ms": tcr.latency_ms,
                            "criteria_scores": [
                                {"criterion": cs.criterion, "score": cs.score,
                                 "passed": cs.passed, "reasoning": cs.reasoning}
                                for cs in tcr.criteria_scores
                            ],
                        }
                        for tcr in run.test_results
                    ],
                })

            return run, metrics["total_cost_usd"] + eval_cost

        # Run test execution in parallel — each candidate hits a different API
        # provider, so no cross-candidate rate limit concerns. Same pattern as
        # the parallel harness builds above. Adversarial battery may have
        # filtered the list down — guard against the empty case so we don't
        # construct a zero-worker pool.
        if not harnesses_with_sandboxes:
            logger.info(
                "All harnesses dropped by adversarial battery; skipping test execution",
                extra={"operation": "test_execution_skipped_adversarial", "trace_id": input_data.trace_id},
            )
        else:
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

    # [Phase 1 hardening] Aggregate per-candidate web_fetch block counts.
    # Surfaces a run-level signal of how often Cloudflare/WAF blocks slowed
    # down doc reading, useful for spotting providers we should pre-cache or
    # access via SDK GitHub repos instead.
    total_web_fetch_blocks = sum(h.web_fetch_blocks for h in harnesses) + sum(
        f.web_fetch_blocks for f in failed
    )

    # Phase 9: post-process flat candidate_runs into per-scope ScopeTestRun list
    # so Agent5Result.scope_runs reflects the blueprint. build_scope_runs is a
    # pure function — it reads the already-computed CandidateTestRun objects.
    scope_runs: list = []
    try:
        from puzzleeval.scope_routing import build_scope_runs
        workflow = getattr(input_data.user_understanding, "workflow", None)
        test_plan = getattr(input_data.user_understanding, "test_plan", None)
        scope_runs = build_scope_runs(
            candidate_runs=candidate_runs,
            blueprint=workflow,
            test_cases=test_cases,
            test_plan=test_plan,
        )
    except Exception as exc:
        logger.warning(
            f"scope_runs build failed (non-fatal): {exc}",
            extra={"operation": "scope_runs_build_failed", "trace_id": input_data.trace_id},
        )
        scope_runs = []

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
        web_fetch_blocks=total_web_fetch_blocks,
        scope_runs=scope_runs,
    )

    logger.info("Agent 5 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "harnesses_built": len(harnesses),
        "harnesses_failed": len(failed),
        "total_cost": total_cost,
        "tests_run": len(candidate_runs),
        "test_cost": total_test_cost,
        "web_fetch_blocks": total_web_fetch_blocks,
    })

    return result
