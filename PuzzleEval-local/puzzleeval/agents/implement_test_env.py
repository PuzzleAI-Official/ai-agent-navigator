# ============================================================================
# Agent 5: Implement Test Env Agent
#
# Compatibility module: public imports still point here while cohesive Agent 5
# subsystems move into puzzleeval.agents.agent5 in behavior-preserving slices.

import json
import logging
import os
import random
import re
import subprocess
import sys
import threading  # Used by _execute_all_tests + eval dispatchers for per-candidate parallelism locks (AGENT6_PER_CANDIDATE_PARALLELISM + AGENT6_PER_CANDIDATE_SESSION_PARALLELISM). See threading.Lock() call sites around state_lock + eval_state_lock.
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
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
    AGENT6_SESSION_MAX_RETRIES,
    AGENT6_SESSION_RETRY_BACKOFF_BASE,
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
from puzzleeval.agents.agent5.playbooks import compose_capability_playbooks
from puzzleeval.agents.agent5.prompts import (
    load_builder_system_prompt as _agent5_load_builder_system_prompt,
    render_builder_prompt as _agent5_render_builder_prompt,
    render_builder_prompt_for_os as _agent5_render_builder_prompt_for_os,
)
from puzzleeval.agents.agent5 import tools as _agent5_tools


# ----------------------------------------------------------------------------
# Phase 1 helper — load capability playbook content from packaged markdown
# ----------------------------------------------------------------------------
# After the Phase 0 contract-system migration, the canonical home for prompt-
# teaching content is `puzzleeval/capability_playbooks/*.md`. The constants
# below (_OS_RULES_*, _VOICE_HARNESS_CONTRACT, etc.) are now ONE-LINE loaders
# that read the markdown body via this helper. The names survive for back-compat
# with source-grep tests and external callers; the bodies live in markdown so
# prompt edits are diffable in isolation.
def _load_capability_playbook_body(filename: str) -> str:
    """Read a capability playbook .md file and return its body (frontmatter stripped).

    Pure helper. Used to populate Phase-1 compatibility constants below.
    Implemented here (not imported from agent5/playbooks.py) so it works for
    ANY filename, not just the ones in PLAYBOOK_ORDER.
    """
    from importlib import resources
    from puzzleeval.contracts.loader import parse_frontmatter

    raw = (
        resources.files("puzzleeval")
        .joinpath("capability_playbooks", filename)
        .read_text(encoding="utf-8")
    )
    _, body = parse_frontmatter(raw, source=filename)
    return body if body else raw


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
    """Back-compat shim — use ``agent5.initial_message.with_shared_preamble``."""
    from puzzleeval.agents.agent5.initial_message import with_shared_preamble
    return with_shared_preamble(prompt)


def _format_modality_context_for_builder(input_data: "Agent5Input") -> str:
    """Back-compat shim — use ``agent5.initial_message.format_modality_context_for_builder``."""
    from puzzleeval.agents.agent5.initial_message import format_modality_context_for_builder
    return format_modality_context_for_builder(input_data)

def _format_atlas_context_for_builder(candidate: ScreenedCandidate) -> str:
    """Back-compat shim — use ``agent5.initial_message.format_atlas_context_for_builder``."""
    from puzzleeval.agents.agent5.initial_message import format_atlas_context_for_builder
    return format_atlas_context_for_builder(candidate)



# Template field names the Phase 1 prompt teaches Agent 5 to use when
# calling ask_research. The soft adherence checker counts how many of
# these appear in the question. We don't reject malformed calls — Opus 4
# follows the template reliably enough that hard validation would
# produce false-rejects on benign rephrasings. Per Q3 in the plan:
# instructional default + soft logging-only validator gives us telemetry
# without rigidity.
_ASK_RESEARCH_TEMPLATE_FIELDS: tuple[str, ...] = (
    "CANDIDATE",
    "ENDPOINT",
    "KNOWN",
    "FIELD NEEDED",
    "WHY",
)


def _ask_research_template_adherence(question: str) -> dict[str, object]:
    """Back-compat shim — use ``agent5.research_subagent.ask_research_template_adherence``."""
    from puzzleeval.agents.agent5.research_subagent import ask_research_template_adherence
    return ask_research_template_adherence(question)


def _synthesize_api_spec_from_checklist(candidate: ScreenedCandidate) -> str | None:
    """Pre-render an api_spec.txt skeleton from Agent 4's BuildReadinessChecklist.

    The biggest single Agent 5 wall-clock cost is Sonnet's Phase 1
    research turn — real-run measurement (trace 8ded6706, 2026-04-25):
      - OpenAI Realtime:   T0 Sonnet = 358s, $1.20
      - ElevenLabs ConvAI: T2 Sonnet = 408s, $1.19
    Combined: ~12 minutes wall-clock + $2.40 per voice run, all spent
    on RE-EXTRACTING fields that Agent 4 already confirmed in the
    BuildReadinessChecklist.

    The checklist for both candidates had:
      * endpoint_path: confirmed (full wss URL with template params)
      * auth_method: confirmed (bearer/api_key shape)
      * request_body_shape: confirmed (full JSON event protocol)
      * response_body_shape: confirmed (full event types)

    Pre-rendering an api_spec.txt skeleton from this checklist BEFORE
    the build loop starts means Sonnet sees a starting-point spec on
    Phase A inventory and can either:
      (a) accept it as-is and skip directly to Phase D / Phase 2 build
          (saves the entire 6-min research turn — biggest win)
      (b) augment only the gap fields (much shorter research turn)

    Returns None when the checklist isn't complete enough to draft
    from (missing/unknown non-negotiables) — the build loop falls back
    to legacy full-research behavior unchanged.

    Principle: this is general not modality-specific. Any candidate
    whose Agent 4 checklist confirms all 4 non-negotiables gets a
    pre-rendered spec. Voice / OCR / chatbot / future modalities all
    benefit identically.
    """
    from puzzleeval.schemas import NON_NEGOTIABLE_FIELDS

    checklist = getattr(candidate, "checklist", None)
    if checklist is None:
        return None
    if checklist.populated_by != "agent_4":
        # Sentinel checklist — Agent 4 failed to extract; can't pre-render
        return None

    # ── Eligibility for partial pre-render (NEW-AM v3) ──
    # Pre-NEW-AM v3 we required ALL 4 non-negotiables to be `confirmed`
    # — too strict. If 3-of-4 are confirmed and 1 is inferred, dropping
    # the entire pre-render means Sonnet re-extracts the 3 confirmed
    # fields too (~$0.40 wasted). Better: partial pre-render. Render
    # confirmed fields as AUTHORITATIVE; render inferred fields with an
    # explicit `[INFERRED — verify on first call]` marker; render
    # unknown fields as REQUIRES_AUGMENT. Sonnet's augment scope shrinks
    # accordingly (verify 1 inferred + augment 5 standard sections,
    # rather than re-extract 3 confirmed + verify 1 + augment 5).
    #
    # Threshold: at least the endpoint_path must be confirmed-or-
    # inferred (not unknown). Without an endpoint we have nothing to
    # build against — better to fall through to full research.
    n_confirmed = sum(
        1 for fn in NON_NEGOTIABLE_FIELDS
        if (getattr(checklist, fn, None) is not None
            and getattr(checklist, fn).status == "confirmed")
    )
    ep = checklist.endpoint_path
    if ep is None or ep.status == "unknown":
        return None  # No endpoint → can't pre-render anything useful
    # Even single-confirmed-non-negotiable triggers pre-render now —
    # the partial spec saves redundant re-extraction of THAT field plus
    # tells Sonnet exactly what's missing.
    if n_confirmed == 0:
        return None  # All 4 inferred-or-unknown → not enough scaffolding

    # Build the spec skeleton in api_spec.txt format that Phase 2 expects.
    lines: list[str] = ["API_SPEC_START"]
    lines.append(f"SERVICE: {candidate.name}")
    lines.append(f"PROVIDER: {candidate.provider}")
    lines.append(f"DOCS_URL: {candidate.verified_api_docs_url}")
    lines.append("")
    lines.append("# Pre-rendered from Agent 4's BuildReadinessChecklist.")
    lines.append(f"# {n_confirmed}/{len(NON_NEGOTIABLE_FIELDS)} non-negotiables confirmed; "
                 "inferred fields marked [INFERRED — verify on first call];")
    lines.append("# unknown fields marked [REQUIRES_AUGMENT] — research them targeted.")
    lines.append("")

    # Per-field renderer with status-aware marking. Confirmed → trust,
    # inferred → verify on first call, unknown → augment in research.
    def _render_field(label: str, field, conditional_purpose: str = "") -> None:
        if field is None or field.status == "unknown":
            lines.append(f"{label}: [REQUIRES_AUGMENT — checklist had no value]")
            if conditional_purpose:
                lines.append(f"  why: {conditional_purpose}")
            lines.append("")
            return
        if field.status == "inferred":
            lines.append(f"{label}: [INFERRED — verify on first API call]")
        else:  # confirmed
            lines.append(f"{label}:")
        lines.append(f"  {field.value or '(empty)'}")
        if field.source_url:
            lines.append(f"  source: {field.source_url}")
        lines.append("")

    _render_field("ENDPOINT_PATH", checklist.endpoint_path)
    _render_field("AUTH_METHOD", checklist.auth_method)
    _render_field("REQUEST_BODY_SHAPE", checklist.request_body_shape)
    _render_field("RESPONSE_BODY_SHAPE", checklist.response_body_shape)

    # Optional / conditional fields — render with status tag so the
    # builder knows whether to trust them or fill gaps.
    for opt_field in (
        "auth_refresh", "error_response_schema", "rate_limit_signal",
        "async_pattern", "content_type_quirks", "sandbox_availability",
    ):
        f = getattr(checklist, opt_field, None)
        if f is None:
            continue
        status = f.status
        val = f.value or "(not extracted)"
        # Header label uppercase for grep-ability + status marker
        header_label = opt_field.upper()
        marker = {"confirmed": "[OK]", "inferred": "[INFERRED]", "unknown": "[UNKNOWN]"}.get(status, "[?]")
        lines.append(f"{header_label}: {marker}")
        lines.append(f"  {val}")
        lines.append("")

    # ── Provider surface — alternative endpoints (Gap C + D fix) ──
    # Agent 4 enumerated up to 5 endpoints in provider_surface during
    # deep-verify, with role tags (primary/alternative/unrelated).
    # Render them prominently with a directive: "if the chosen endpoint
    # turns out wrong for your test case input, switch to one of these
    # before re-researching the API from scratch." Without this Sonnet
    # would re-search the docs to discover what alternatives exist.
    surface = checklist.provider_surface or []
    if surface:
        lines.append("PROVIDER_SURFACE — endpoints Agent 4 considered:")
        lines.append("  (If the chosen endpoint above turns out wrong for")
        lines.append("   your test case input shape, try one of the")
        lines.append("   [alternative] endpoints BEFORE re-researching.)")
        # Schema (puzzleeval/schemas.py::EndpointSummary):
        #   .name (e.g. "POST /v1/audio/speech" or "WebSocket /v1/realtime")
        #   .purpose (one-line description)
        #   .relevance_to_use_case ("primary" | "alternative" | "unrelated")
        #   .selection_note (why chosen / rejected)
        # Pre-NEW-AM v3 used getattr(ep, "method"/"path"/"role") which
        # are NOT EndpointSummary fields — every entry rendered as
        # "[?] ? ?" silently dropping the alternatives info. Real-run
        # trace 8ded6706 surfaced this as one of the audit findings.
        for ep in surface[:8]:  # cap for readability
            name = getattr(ep, "name", None) or "?"
            relevance = getattr(ep, "relevance_to_use_case", None) or "?"
            purpose = (getattr(ep, "purpose", None) or "")[:120]
            note = (getattr(ep, "selection_note", None) or "")[:80]
            lines.append(f"  - [{relevance}] {name}  -- {purpose}")
            if note:
                lines.append(f"      note: {note}")
        lines.append("")

    # ── Completeness-confidence note (Gap D fix) ──
    # The checklist's request_body_shape captures THE PRIMARY message
    # shape for the chosen endpoint. APIs commonly have variants:
    #   - WebSocket events: many message types, not just init
    #   - REST endpoints: PATCH/PUT alongside POST with different bodies
    #   - Override variants: conversation_initiation_client_data with
    #     conversation_config_override (real example from ElevenLabs
    #     ConvAI — critical for system prompt injection mid-stream)
    # Without this note, builder might trust REQUEST_BODY_SHAPE as
    # exhaustive and miss the variant it actually needs.
    lines.append("COMPLETENESS_NOTE:")
    lines.append("  REQUEST_BODY_SHAPE + RESPONSE_BODY_SHAPE above capture")
    lines.append("  the PRIMARY message/event shapes for the chosen endpoint,")
    lines.append("  not necessarily ALL variants. Common variants to check")
    lines.append("  for in OVERRIDE_OR_SPECIAL_MESSAGE_SHAPES below:")
    lines.append("    - WebSocket protocols: lifecycle events, error events,")
    lines.append("      ping/pong keepalive, interruption signals")
    lines.append("    - REST endpoints: PATCH/PUT body shapes, query params,")
    lines.append("      header-based variants (e.g., If-Match for idempotency)")
    lines.append("    - Override / context-injection variants needed to pass")
    lines.append("      runtime config (system prompt, tool definitions, etc.)")
    lines.append("  If your test case needs ANY variant beyond what's above,")
    lines.append("  research it via web_fetch BEFORE writing harness code.")
    lines.append("")

    # Selection justification — why THIS endpoint over alternatives
    if checklist.selection_justification:
        lines.append("SELECTION_JUSTIFICATION:")
        for chunk in checklist.selection_justification[:600].splitlines():
            lines.append(f"  {chunk}")
        lines.append("")

    # ── EXPLICIT GAPS SECTION (NEW-AM v2 — per-test-case completion) ──
    # Real-run analysis (trace 8ded6706, 2026-04-25) compared the
    # checklist against what Sonnet's 6-minute research turn actually
    # produced for ElevenLabs ConvAI:
    #   * 60% redundant (re-extracted confirmed checklist fields)
    #   * 40% NEW value the checklist doesn't carry by design:
    #       - Auxiliary endpoint request bodies (POST /agents,
    #         DELETE /agents/{id}, etc.) — only the primary endpoint
    #         is in checklist.request_body_shape
    #       - Override / special-case message shapes (e.g.,
    #         conversation_initiation_client_data with
    #         conversation_config_override for mid-stream system
    #         prompt injection)
    #       - INPUT_COMPATIBILITY (per-test-case mapping: which
    #         test_case input shapes work, which return INCOMPATIBLE)
    #       - ROUTING_TABLE (per-test-case dispatch logic: which
    #         payload shape maps to which endpoint)
    #       - WORKING_EXAMPLE (concrete copy-paste request flow)
    #
    # Pure-skip pre-render would lose this 40% and quality would
    # degrade. The fix: render the AUTHORITATIVE 60% (saving the
    # redundant Sonnet work) but mark the 40% as REQUIRES_AUGMENT
    # so the builder knows what to research/synthesize before
    # building. Net wall-clock saving: ~3-4 min per candidate
    # (vs 6 if everything was redundant) — but quality preserved.
    lines.append("")
    lines.append("# ─────────────────────────────────────────────────────")
    lines.append("# REQUIRES_AUGMENT — fields Sonnet/Opus must fill in")
    lines.append("# ─────────────────────────────────────────────────────")
    lines.append("# The four non-negotiables above are AUTHORITATIVE; do NOT")
    lines.append("# re-research them. The fields below are per-test-case or")
    lines.append("# auxiliary — fill them in only if your test cases need them.")
    lines.append("")
    lines.append("AUXILIARY_ENDPOINTS_REQUEST_BODIES: [REQUIRES_AUGMENT IF NEEDED]")
    lines.append("  If your test case provisions resources (creates agents,")
    lines.append("  deletes them, configures phone numbers, etc.) you must")
    lines.append("  research the request body shape of those auxiliary")
    lines.append("  endpoints. The provider_surface above lists them; the")
    lines.append("  primary endpoint's body shape is in REQUEST_BODY_SHAPE.")
    lines.append("")
    lines.append("OVERRIDE_OR_SPECIAL_MESSAGE_SHAPES: [REQUIRES_AUGMENT IF NEEDED]")
    lines.append("  If the API has special variants (config overrides, mid-")
    lines.append("  stream context injection, dynamic-variable substitution,")
    lines.append("  etc.) needed for your test cases, research and document")
    lines.append("  them here. Common case: passing a system prompt mid-")
    lines.append("  conversation requires the OVERRIDE message variant, not")
    lines.append("  the basic init shape in REQUEST_BODY_SHAPE.")
    lines.append("")
    lines.append("INPUT_COMPATIBILITY: [REQUIRES_AUGMENT — per-test-case]")
    lines.append("  Map each test case input form (text / audio / file_upload /")
    lines.append("  base64 / url) to YES/NO based on what the API actually")
    lines.append("  accepts. Drives ROUTING_TABLE below.")
    lines.append("")
    lines.append("ROUTING_TABLE: [REQUIRES_AUGMENT — per-test-case]")
    lines.append("  For each test_case input shape, document which endpoint")
    lines.append("  to call and how to transform the input. Forms that the")
    lines.append("  API does NOT support → harness returns")
    lines.append("  success=False, error='INCOMPATIBLE: <reason>'.")
    lines.append("")
    lines.append("WORKING_EXAMPLE: [REQUIRES_AUGMENT — synthesize]")
    lines.append("  A complete runnable example (curl / Python / language of")
    lines.append("  your choice) showing the canonical request flow end-to-end.")
    lines.append("  At minimum: the primary endpoint call with auth + body +")
    lines.append("  expected response shape. Multi-step flows: enumerate")
    lines.append("  every step.")
    lines.append("")
    lines.append("ACCEPTED_INPUT_FORMATS:")
    lines.append("  (derive from test case input_type + INPUT_COMPATIBILITY above)")
    lines.append("")
    lines.append("API_SPEC_END")

    return "\n".join(lines)


def _format_checklist_context_for_builder(candidate: ScreenedCandidate) -> str:
    """Back-compat shim — use ``agent5.initial_message.format_checklist_context_for_builder``."""
    from puzzleeval.agents.agent5.initial_message import format_checklist_context_for_builder
    return format_checklist_context_for_builder(candidate)



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


# ============================================================================
# Sonnet → Opus boundary compaction (PLAN_VOICE_RUN_OPTIMIZATIONS extension)
# ============================================================================
#
# Real-run measurement (trace 73a9d605, 2026-04-25):
#   - ElevenLabs first-Opus turn cache_create: 69,770 tokens → ~$0.44 tax
#   - OpenAI first-Opus turn cache_create:     35,993 tokens → ~$0.22 tax
#   - Combined per-run model-switch tax:       ~$0.66
#
# What's happening: Opus has a separate cache namespace from Sonnet.
# When the model switches at the Phase 1 → Phase 2 boundary, Opus's
# first turn re-caches the entire conversation (including Sonnet's
# raw web_fetch / web_search tool_result blobs — 15-30K chars each)
# into its own namespace. That re-cache is the tax.
#
# Compaction trades the raw-doc blobs (which Opus rarely re-reads
# verbatim) for brief 1-line summaries pointing at:
#   - api_spec.txt (the contract Sonnet synthesized)
#   - DOC_REFERENCES (URL list inside api_spec.txt)
#   - fetched_docs_*.txt (the same content Agent 4 / Sonnet saved
#     to disk via save_web_fetches_to_sandbox — Opus can read_file()
#     any of them on demand)
#
# Safety guarantees (verified before shipping):
#   - Every web_fetch_tool_result content blob is ALSO on disk as
#     fetched_docs_N.txt (web_doc_cache.save_web_fetches_to_sandbox
#     writes both Agent 4's and Agent 5/Sonnet's fetches there).
#   - Real-run audit confirmed Agent 5 prefers read_file over
#     web_fetch when files exist (ElevenLabs: 2 read_file, 0
#     web_fetch); the read_file path is the established happy path.
#   - tool_use_id is preserved on every replaced block so the
#     server-tool pairing invariant (every tool_use has a matching
#     tool_result) is not broken.
#
# Wired at the same point that flips `api_spec_written = True` so
# compaction runs ONCE per build (not per turn).
# ============================================================================

# What we replace `web_fetch_tool_result.content.content.source.data`
# with after compaction. Opus reads this and sees a clear pointer to
# the on-disk artifacts — no ambiguity about where to look if it
# needs the raw page content.
_COMPACTED_FETCH_PLACEHOLDER = (
    "[research artifact COMPACTED — original page content available via "
    "read_file('fetched_docs_N.txt') in this sandbox; the synthesized "
    "contract lives in api_spec.txt. Read those files if you need the "
    "raw doc; the prior conversation history has been compacted to save "
    "cache space at the Sonnet → Opus model boundary.]"
)


def _compact_research_tool_results(messages: list) -> int:
    """Replace web_fetch / web_search tool_result CONTENT BLOBS in the
    conversation history with brief summaries.

    Mutates each message's content list in place — the builder loop
    owns the `messages` list and re-sends it on every API call. After
    compaction, the next API call sees ~5K tokens of message history
    instead of ~60K, eliminating the model-switch cache rebuild tax.

    Preserves the SHAPE of every block (web_fetch_tool_result stays a
    web_fetch_tool_result, just with shorter `content.content.source.data`)
    so the API's "every tool_use has a matching tool_result" invariant
    is not broken. tool_use_id is preserved.

    Idempotent: calling twice has no effect on the second call (the
    compaction marker is in the new content; calling again re-replaces
    the marker with itself).

    Returns: number of blocks compacted (for logging / observability).

    Safe on:
      - Empty messages list (returns 0)
      - Messages with string content (skipped)
      - Messages with no research blocks (no-op)
      - Mix of typed SDK objects + dicts (handled both shapes)
    """
    compacted_count = 0
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            block_type = _block_type(block)
            if block_type == "web_fetch_tool_result":
                content[i] = _compact_web_fetch_block(block)
                compacted_count += 1
            # NOTE: web_search_tool_result is NOT compacted (NEW-AM v3 fix).
            # Real-run trace 32a4b134 (2026-04-25) caught a 400 from the
            # Anthropic API: "Invalid `encrypted_content` in `search_result`
            # block". Our prior _compact_web_search_block set
            # `encrypted_content: ""` (empty string) — Anthropic now
            # rejects that as invalid (the field expects either a real
            # encrypted blob or an omitted entry).
            #
            # Skipping web_search_tool_result compaction is safe AND has
            # negligible cost impact: web_search results are 5-7K tokens
            # each (titles + snippets); web_fetch results are 5-25K (full
            # page content). The 60K-token cache rebuild tax we're
            # eliminating is dominated by web_fetch blobs. Removing
            # web_search from the compaction reduces our savings by
            # <5% but eliminates the API-rejection failure mode.
            #
            # If Anthropic ever publishes a "compacted" sentinel for
            # encrypted_content (or accepts None), revisit and re-add
            # web_search compaction.
    return compacted_count


def _block_type(block) -> str | None:
    """Return the `.type` of a content block, handling both SDK-typed
    and dict shapes. Returns None when no type is discernible."""
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_attr(block, name: str, default=None):
    """Read an attribute / dict key off a block, dual-shape safe."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _compact_web_fetch_block(block) -> dict:
    """Replace a web_fetch_tool_result block's page content with a
    short summary. Preserves type + tool_use_id + URL.

    Output is a plain dict (not an SDK object) — the Anthropic API
    accepts both shapes for re-sent assistant messages, and dicts
    survive the SDK's serialization round-trip without mutation
    issues if the caller's typed object is frozen.
    """
    tool_use_id = _block_attr(block, "tool_use_id", "")
    inner = _block_attr(block, "content")
    # Try to preserve the URL for diagnostic/traceability value.
    url = ""
    if inner is not None:
        url = _block_attr(inner, "url", "") or ""
    return {
        "type": "web_fetch_tool_result",
        "tool_use_id": tool_use_id,
        "content": {
            "type": "web_fetch_result",
            "url": url,
            "content": {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": _COMPACTED_FETCH_PLACEHOLDER,
                },
            },
        },
    }


def _compact_web_search_block(block) -> dict:
    """Replace a web_search_tool_result block's results list with a
    single brief entry. Preserves type + tool_use_id."""
    tool_use_id = _block_attr(block, "tool_use_id", "")
    return {
        "type": "web_search_tool_result",
        "tool_use_id": tool_use_id,
        "content": [{
            "type": "web_search_result",
            "url": "",
            "title": "[research artifact compacted]",
            "page_age": "",
            "encrypted_content": "",
        }],
    }


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
    """Back-compat shim — use ``agent5.initial_message.with_builder_appendix``."""
    from puzzleeval.agents.agent5.initial_message import with_builder_appendix
    return with_builder_appendix(prompt)


BUILDER_SYSTEM_PROMPT = _agent5_load_builder_system_prompt()


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
        "Your peer integration engineer FOR PHASE 2+ DEBUGGING ONLY. Pass "
        "a specific question about an API you're already integrating; a "
        "research sub-agent searches the web and returns a structured "
        "answer.\n\n"
        "⚠️ PHASE-1 BLOCKED: this tool REFUSES to run until api_spec.txt "
        "exists in your sandbox. Initial API discovery is YOUR job — do "
        "it directly with web_search + web_fetch so you build your own "
        "mental model of the API. The sub-agent inherits YOUR context "
        "(api_spec excerpt, harness code, recent errors) to give targeted "
        "answers; before the spec exists there's nothing to inherit, so "
        "calling ask_research would mean the sub-agent does generic "
        "research that YOU should have done + internalized yourself.\n\n"
        "Phase 1 pattern (do this FIRST with web_search/web_fetch):\n"
        "  1. Find the provider's docs (site-scoped search + targeted fetch)\n"
        "  2. Extract ENDPOINTS / AUTH / REQUEST_FORMAT / RESPONSE_FORMAT / "
        "ERRORS / SAMPLE_CODE\n"
        "  3. Write api_spec.txt capturing that structure\n"
        "Phase 2+ pattern (NOW ask_research is unlocked):\n"
        "  4. Build harness.py from api_spec.txt\n"
        "  5. Hit an error your spec can't explain → call ask_research "
        "with the specific gap\n\n"
        "AUTO-INHERITED CONTEXT (Phase 2+): api_spec excerpt, recent "
        "error output, harness code snippet, and provider details are "
        "automatically attached. You do NOT need to restate any of that "
        "in your question. Just ask the specific thing you're uncertain "
        "about.\n\n"
        "TWO AUTOMATIC MODES:\n"
        "  • Targeted mode (you name a specific flag/endpoint/error code): "
        "the sub-agent does ONE precise search, returns the answer + source "
        "URL, stops.\n"
        "  • Exploratory mode (open-ended question, or thin context): the "
        "sub-agent diversifies across search angles — official docs, "
        "GitHub SDK issues, community forums, archived web — and "
        "synthesizes findings. It will explicitly report what it didn't "
        "find rather than fabricate.\n\n"
        "WHEN TO USE IT IN PHASE 2+ (treat as a normal tool, not a last "
        "resort):\n"
        "  • You suspect a known API quirk behind an error (WebSocket "
        "close codes, undocumented config flags, deprecated endpoints)\n"
        "  • Docs might document a flag/config your api_spec doesn't cover\n"
        "  • You'd Google it yourself in 30 seconds — call ask_research "
        "instead (one call ~$0.15-0.25, cheaper than 2-3 probe scripts)\n"
        "  • The same error surfaces twice and you want to verify "
        "assumptions before another patch attempt\n\n"
        "HONEST FAILURE: if the sub-agent says 'NOT FOUND — searched X, Y, "
        "Z', TRUST IT. Do not re-call with a rephrased question hoping "
        "for a different result. Switch to empirical probing or ask for a "
        "different angle. Research and probing are complementary, not "
        "substitutes.\n\n"
        "Cost: ~$0.15-0.25 per call. Sub-agent uses Sonnet + 3 searches + "
        "3 fetches."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The specific thing you're uncertain about. Focus on "
                    "the UNKNOWN, not the context (context is auto-attached). "
                    "Good: 'What enables override_permissions at agent "
                    "creation for ElevenLabs Conversational AI?' "
                    "Good: 'Why does a ConvAI WebSocket close with code "
                    "1008 immediately after sending first_message override?' "
                    "Avoid: 'What is the ElevenLabs Conversational AI "
                    "endpoint?' (already in your api_spec — don't re-ask "
                    "known info)."
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

# Phase 1 migration: OS rules now live in puzzleeval/capability_playbooks/
# platform_*.md files with YAML frontmatter. The constants below load their
# bodies from disk at module import. Keeping the legacy NAMES preserves
# back-compat with source-grep tests + external callers; the BODIES are
# diffable in markdown.
_OS_RULES_WINDOWS = _load_capability_playbook_body("platform_windows.md")
_OS_RULES_LINUX = _load_capability_playbook_body("platform_linux.md")
_OS_RULES_MACOS = _load_capability_playbook_body("platform_macos.md")


def _os_specific_rules() -> str:
    """Return the prompt rules block for the current host OS. Empty-string
    fallback for unrecognized platforms so the prompt always renders."""
    return _agent5_render_builder_prompt_for_os(
        "__OS_SPECIFIC_RULES__",
        platform=sys.platform,
        os_rules={
            "win32": _OS_RULES_WINDOWS,
            "darwin": _OS_RULES_MACOS,
            "linux": _OS_RULES_LINUX,
        },
    )


def _render_builder_prompt_for_os(prompt_template: str) -> str:
    """Fill __OS_TYPE__ + __OS_SPECIFIC_RULES__ placeholders based on host."""
    return _agent5_render_builder_prompt_for_os(
        prompt_template,
        platform=sys.platform,
        os_rules={
            "win32": _OS_RULES_WINDOWS,
            "darwin": _OS_RULES_MACOS,
            "linux": _OS_RULES_LINUX,
        },
    )


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

_VOICE_HARNESS_CONTRACT = _load_capability_playbook_body("voice.md")


# ─────────────────────────────────────────────────────────────────────────────
# STREAMING RESPONSE COLLECTION CONTRACT (NEW-AM v4)
# ─────────────────────────────────────────────────────────────────────────────
# Real-run trace 4427591c (2026-04-25) caught a prompt-under-specification
# bug: two builds of the SAME ElevenLabs ConvAI provider produced TWO
# DIFFERENT response-collection architectures (60% vs 20% pass rate)
# because the prompt teaches "what to return" + "session_state for multi-turn"
# but says NOTHING about HOW to wait for streaming responses. The LLM
# coin-flips between good and bad patterns based on training-data salience.
#
# This contract teaches the CORRECT abstraction (error-timeout +
# reset-on-event) for any streaming-response harness — voice WebSocket,
# SSE chat, async polling, etc. NOT injected for single-call REST
# harnesses (OCR/vision/inbound webhook/outbound), so they see ZERO
# prompt overhead and ZERO behavior change.
#
# Anti-bandaid checks (regression-tested):
#   * Principle-based content (no provider names like "ElevenLabs", no
#     specific timeout numbers like "1.5s")
#   * Schema-driven trigger (input_type/output_type indicates streaming)
#   * Conditional injection mirrors __OS_SPECIFIC_RULES__ +
#     __MODALITY_CONTRACT__ patterns (no new infrastructure)
_STREAMING_RESPONSE_CONTRACT = _load_capability_playbook_body("streaming_response.md")


def _modality_specific_contract(test_cases: list[Any] | None) -> str:
    """Return the modality-specific harness contracts for this candidate's
    tests. **Composed content** — multiple feature-specific contracts
    merge into a single string in the order they apply.

    NEW-AM v4: this is the consolidation point for ALL conditional
    contract injection. Mirrors the AD-002 skills/playbook architecture
    direction (one loader returning composed per-modality content).
    Adding a new feature contract = adding a new helper that returns
    a string and appending it here. No new placeholders, no new
    renderer wiring.

    Today's feature contracts:
      * ``_voice_harness_contract_for(test_cases)`` — voice return
        shape + session_state continuity (voice/audio modalities only)
      * ``_streaming_response_contract_for(test_cases)`` — error-
        timeout + reset-on-event collection pattern (broader: voice +
        multi-turn chat + code-gen streaming + any streaming response)

    When a future feature lands (e.g., async_polling pattern, batch_
    file submission, OAuth refresh-token loop, multi-modal output
    fanout), add another helper + append it below. When the helper
    count crosses ~8 OR independent ownership becomes valuable,
    migrate the helpers to per-skill markdown files in skills/
    and read them here. Per AD-002 revisit triggers.

    Empty string when no feature contract applies (OCR / vision /
    single-call REST builds see zero injection).
    """
    if not test_cases:
        return ""
    # Compatibility fallbacks keep the historical constants as the runtime
    # safety net while Agent 5 now loads the primary capability content from
    # local PuzzleEval playbooks.
    fallbacks = {
        "voice": _voice_harness_contract_for(test_cases),
        "streaming_response": _streaming_response_contract_for(test_cases),
        "live_test_voice": _live_test_contract_for(test_cases),
    }
    return compose_capability_playbooks(test_cases, fallbacks=fallbacks)


# ─────────────────────────────────────────────────────────────────────────────
# LIVE-TEST CONTRACT (NEW-AM v6 — Phase 3 modality-specific live testing)
# ─────────────────────────────────────────────────────────────────────────────
# Real-run trace a4860e94 (2026-04-25) caught a Phase 3 prompt gap: the
# template teaches OCR-style live testing (file-based, single-call) and
# leaves the builder to improvise for voice/conversation modalities. Two
# harnesses for the SAME prompt produced wildly different live_tests:
#
#   ElevenLabs live_test (162 lines, comprehensive):
#     * Synthesized real caller audio via TTS
#     * Drove 2-turn conversation through harness.run()
#     * Verified full WebSocket + agent-creation + multi-turn flow
#     → Real tests: 4/5 pass (the live test caught real bugs)
#
#   OpenAI live_test (28 lines, trivial):
#     * Called harness.run({"audio_url": None, "turn_index": 0})
#     * Asserted: success=True AND audio_bytes_len > 0
#     → "Test passed" without ever exercising the audio code path
#     → Real tests: 0/5 pass (agent silent every turn)
#
# The audio code path was NEVER VERIFIED for OpenAI. The harness was
# declared "live-test passed" but the production input shape was
# never even sent to the API.
#
# Conditional injection — fires ONLY for streaming/multi-call modalities
# that need rigorous live testing. Single-call REST harnesses (OCR /
# vision / inbound webhook / outbound) keep the existing OCR-style live
# test guidance — no contamination.
_LIVE_TEST_CONTRACT_VOICE = _load_capability_playbook_body("live_test_voice.md")


def _live_test_contract_for(test_cases: list[Any]) -> str:
    """Return the voice/conversation live-test contract when test cases
    include multi-turn streaming response shapes; empty string for
    single-call REST harnesses (which keep the existing OCR-style Phase 3
    live-test guidance).

    Trigger: same shapes as the streaming-response-collection contract
    (voice_conversation, voice_turn, audio_content, conversation, code).
    These all require the production-payload-shape live test pattern.

    Non-streaming shapes get NO injection — OCR/vision/REST harnesses
    keep the existing single-call live-test guidance (test each file
    type, success=True + output_len>100). Zero contamination.
    """
    if not test_cases:
        return ""
    streaming_shapes = {
        "voice_conversation", "voice_turn", "audio_content",
        "conversation",
        "code",
    }
    for tc in test_cases:
        input_type = (
            getattr(tc, "input_type", None)
            or (tc.get("input_type") if isinstance(tc, dict) else None)
        )
        output_type = (
            getattr(tc, "output_type", None)
            or (tc.get("output_type") if isinstance(tc, dict) else None)
        )
        if input_type in streaming_shapes or output_type in streaming_shapes:
            return _LIVE_TEST_CONTRACT_VOICE
    return ""


def _voice_harness_contract_for(test_cases: list[Any]) -> str:
    """Return the voice harness contract when test cases include voice
    modalities; empty string otherwise. Helper consumed by the unified
    `_modality_specific_contract` dispatcher.

    Voice signals (any in candidate's test cases injects):
      * input_type:  ``conversation`` / ``voice_conversation`` / ``voice_turn`` / ``audio_content``
      * output_type: ``voice_conversation`` / ``voice_turn`` / ``audio_content``
    """
    voice_modalities = {
        "conversation", "voice_conversation", "voice_turn", "audio_content",
    }
    for tc in test_cases:
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


def _streaming_response_contract_for(test_cases: list[Any]) -> str:
    """Return the streaming-response collection contract when test cases
    declare streaming-shape input/output types; empty string otherwise.
    Helper consumed by the unified `_modality_specific_contract`
    dispatcher.

    Streaming-response shapes (need the rule):
      * voice_conversation, voice_turn, audio_content
      * conversation (multi-turn chat — events stream)
      * code (code-gen often streams)
    """
    streaming_shapes = {
        "voice_conversation", "voice_turn", "audio_content",
        "conversation",
        "code",
    }
    for tc in test_cases:
        input_type = (
            getattr(tc, "input_type", None)
            or (tc.get("input_type") if isinstance(tc, dict) else None)
        )
        output_type = (
            getattr(tc, "output_type", None)
            or (tc.get("output_type") if isinstance(tc, dict) else None)
        )
        if input_type in streaming_shapes or output_type in streaming_shapes:
            return _STREAMING_RESPONSE_CONTRACT
    return ""


def _streaming_response_contract(test_cases: list[Any] | None) -> str:
    """Back-compat shim — public name retained for tests + external
    callers that grep for it. The dispatch lives in
    `_streaming_response_contract_for`; this just forwards.
    """
    if not test_cases:
        return ""
    return _streaming_response_contract_for(test_cases)


def _render_builder_prompt(
    prompt_template: str,
    test_cases: list[Any] | None = None,
) -> str:
    """Render the Agent 5 builder prompt with unified contract injection.

    Phase 1.B + 1.C: every conditional contract (platform + modality) flows
    through ``puzzleeval.contracts.compose_contract_block(task)`` and lands
    in the single trailing ``__CONTRACT_BLOCK__`` placeholder near the end
    of the template. The legacy split placeholders (``__OS_SPECIFIC_RULES__``
    + ``__MODALITY_CONTRACT__``) are stripped from the rendered output —
    their content already lives inside the unified block.

    Cache-prefix benefit: the cacheable prefix grows from ~234 lines (where
    the old ``__OS_SPECIFIC_RULES__`` placeholder used to live) to ~1080
    lines (everything before ``__CONTRACT_BLOCK__`` near the end). Per the
    NEW-AM cache_create cost analysis (cache_create was 30-48% of build
    cost), this is a measurable cost reduction on multi-turn Agent 5 builds.

    Single-call REST harnesses (OCR / vision / inbound webhook /
    outbound) on a non-platform-specific path see an empty contract
    block — zero prompt overhead, zero behavior change.

    The legacy helpers (``_modality_specific_contract``,
    ``_voice_harness_contract_for``, ``_streaming_response_contract_for``,
    ``_live_test_contract_for``) survive as forwarders for source-grep
    tests + external callers — but the canonical render path no longer
    invokes them.
    """
    from puzzleeval.contracts import TaskContext, compose_contract_block

    task = TaskContext(
        agent_id="agent_5",
        phase="build",
        platform=sys.platform,
        test_cases=tuple(test_cases or ()),
    )
    contract_block = compose_contract_block(task)
    return _agent5_render_builder_prompt(
        prompt_template,
        platform=sys.platform,
        contract_block=contract_block,
    )


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

# Phase 3.3: persist_large_output + constants moved to agent5.conversation_log.
from puzzleeval.agents.agent5.conversation_log import (
    OUTPUT_PERSIST_THRESHOLD,
    MAX_PERSISTED_OUTPUT_CHARS,
    persist_large_output as _persist_large_output_canonical,
)


def _persist_large_output(output: str, sandbox_dir: Path, turn: int) -> str:
    """Back-compat shim — use ``agent5.conversation_log.persist_large_output``."""
    return _persist_large_output_canonical(output, sandbox_dir, turn)


def _candidate_slug(name: str) -> str:
    """Back-compat shim — use ``puzzleeval.agents.agent5.sandbox.candidate_slug``."""
    from puzzleeval.agents.agent5.sandbox import candidate_slug
    return candidate_slug(name)


def _calculate_call_cost(response: anthropic.types.Message, model: str) -> float:
    """Calculate the total cost of a single API call."""
    from puzzleeval.agents.agent5.costing import calculate_call_cost

    return calculate_call_cost(
        response,
        model,
        model_pricing=MODEL_PRICING,
        web_search_price_per_search=WEB_SEARCH_PRICE_PER_SEARCH,
    )



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
    phase_state: dict | None = None,
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

    ``phase_state`` is the optional per-build context for the write_file
    gates (B1, B2, B3). Recognized keys: ``api_spec_written`` (bool),
    ``candidate_slug`` (str), ``trace_id`` (str). Legacy callers omit it
    and get the original behavior.
    """
    return _agent5_tools.dispatch_tool(
        tool_name,
        tool_input,
        sandbox_dir,
        extra_env=extra_env,
        read_state=read_state,
        code_timeout_s=AGENT5_CODE_TIMEOUT,
        allowed_extensions=ALLOWED_EXTENSIONS,
        phase_state=phase_state,
    )


def _tool_write_file(
    tool_input: dict,
    sandbox_dir: Path,
    read_state: dict[str, float] | None = None,
) -> str:
    """Write a file to the sandbox directory with security checks."""
    return _agent5_tools.write_file(
        tool_input,
        sandbox_dir,
        read_state=read_state,
        allowed_extensions=ALLOWED_EXTENSIONS,
    )



def _tool_patch_file(
    tool_input: dict,
    sandbox_dir: Path,
    read_state: dict[str, float] | None = None,
) -> str:
    """Replace a specific string in an existing file."""
    return _agent5_tools.patch_file(
        tool_input,
        sandbox_dir,
        read_state=read_state,
    )



def _build_sandbox_env(sandbox_dir: Path, extra_env: dict[str, str] | None = None) -> dict:
    """Build environment variables for subprocess execution in the sandbox."""
    return _agent5_tools.build_sandbox_env(sandbox_dir, extra_env)



def _tool_run_code(
    tool_input: dict,
    sandbox_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Run a shell command in the sandbox directory with timeout and venv isolation."""
    return _agent5_tools.run_code(
        tool_input,
        sandbox_dir,
        extra_env=extra_env,
        code_timeout_s=AGENT5_CODE_TIMEOUT,
    )



def _tool_read_file(
    tool_input: dict,
    sandbox_dir: Path,
    read_state: dict[str, float] | None = None,
) -> str:
    """Read a file from the sandbox directory."""
    return _agent5_tools.read_file(
        tool_input,
        sandbox_dir,
        read_state=read_state,
    )



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


def _detect_patch_fragmentation_pattern(
    conversation_log: list[dict],
    already_nudged_files: set[str],
    *,
    output_token_ceiling: int = 600,
) -> str | None:
    """
    Gate C — detect small-serial-patch fragmentation on the same file.

    Returns the filename when the last 2 real turns were:
      - Both small (each turn's ``output_tokens`` < ``output_token_ceiling``)
      - Both had EXACTLY ONE ``patch_file`` tool_use
      - Both targeted the SAME ``filename``
      - That filename is NOT already in ``already_nudged_files``

    Returns ``None`` when the pattern doesn't hold, or when the file was
    already nudged this build (we fire at most once per file per build so
    the builder never sees the same nudge twice on the same file).

    Why this signal is safe:
      - ``patch_file`` is the builder's PRIMARY code-fix tool — legitimate
        use is frequent. The serial-same-file-small pattern specifically
        surfaces cases where the builder is nibbling fix-by-fix when a
        wider edit batch would land in one turn.
      - 600 output tokens ≈ a small targeted patch (single string replace
        + a line of rationale). A full rewrite-style patch blows past
        600 easily and won't false-positive.
      - Single tool_use per turn filters out the already-healthy case of
        "two parallel patches to harness.py in one turn".

    Non-real entries (``"save-docs-*"`` / ``"verify-*"`` bookkeeping rows
    appended after web_fetch saves + verification-gate retries) use
    string turn IDs and lack ``output_tokens`` — we skip them when
    walking back, so a save-docs entry between two small patches still
    triggers correctly. This matters: without the skip, the gate would
    silently never fire because real-turn and bookkeeping-turn rows are
    interleaved in practice.
    """
    # Walk back through the log picking REAL turns only (those with a
    # numeric ``turn`` key and an ``output_tokens`` field). We only need
    # the last two.
    real_turns: list[dict] = []
    for entry in reversed(conversation_log):
        if isinstance(entry.get("turn"), int) and "output_tokens" in entry:
            real_turns.append(entry)
            if len(real_turns) >= 2:
                break
    if len(real_turns) < 2:
        return None
    # ``real_turns`` is in reverse order after the walk — the current turn
    # is ``real_turns[0]``, the prior turn is ``real_turns[1]``.
    curr, prev = real_turns[0], real_turns[1]

    # Both turns must be small — guards against rewrite-style patches that
    # legitimately need their own turn to review after landing.
    sentinel = 10**9
    if (
        int(curr.get("output_tokens", sentinel)) >= output_token_ceiling
        or int(prev.get("output_tokens", sentinel)) >= output_token_ceiling
    ):
        return None

    def _single_patch_target(turn_entry: dict) -> str | None:
        """Return the patched filename when this turn has EXACTLY one
        ``patch_file`` tool_use, else None. Multi-tool turns (e.g.,
        parallel patches, or patch + run_code) and zero-patch turns
        both return None — we only want to flag isolated-small patches."""
        tool_calls = turn_entry.get("tool_calls", []) or []
        patches = [
            tc for tc in tool_calls
            if isinstance(tc, dict)
            and tc.get("tool") == "patch_file"
            and isinstance(tc.get("input"), dict)
        ]
        if len(patches) != 1:
            return None
        # ``tool_calls`` as a whole should be just this one patch —
        # allow advisor telemetry rows but disallow mixed code actions
        # (run_code + patch_file signals a genuine iterate-and-verify
        # cycle, not fragmentation).
        non_advisor_tools = [
            tc for tc in tool_calls
            if isinstance(tc, dict) and tc.get("tool") not in ("advisor",)
        ]
        if len(non_advisor_tools) != 1:
            return None
        filename = patches[0]["input"].get("filename", "")
        return filename or None

    curr_file = _single_patch_target(curr)
    prev_file = _single_patch_target(prev)
    if curr_file is None or prev_file is None:
        return None
    if curr_file != prev_file:
        return None
    if curr_file in already_nudged_files:
        return None
    return curr_file


def _extract_and_save_web_content(
    response: anthropic.types.Message,
    sandbox_dir: Path,
    existing_count: int = 0,
) -> list[str]:
    """Back-compat shim over :func:`puzzleeval.web_doc_cache.save_web_fetches_to_sandbox`.

    The original implementation lived inline in this module. It was
    lifted into ``puzzleeval/web_doc_cache.py`` so Agent 4 can use the
    same logic to prefetch docs into the sandbox before Agent 5 builds.
    Keeping this shim avoids touching the Agent 5 call site at line
    ~3991, which is deep inside the builder-loop turn handler.
    """
    from puzzleeval.web_doc_cache import save_web_fetches_to_sandbox
    return save_web_fetches_to_sandbox(response, sandbox_dir, existing_count=existing_count)



# TARGETED_RESEARCH_SYSTEM moved to agent5.research_subagent. Re-exported here.
from puzzleeval.agents.agent5.research_subagent import TARGETED_RESEARCH_SYSTEM


def _run_targeted_research(
    client: anthropic.Anthropic,
    question: str,
    candidate_name: str,
    logger,
    trace_id: str,
) -> tuple[str, float]:
    """Back-compat shim — use ``agent5.research_subagent.run_targeted_research``."""
    from puzzleeval.agents.agent5.research_subagent import run_targeted_research
    return run_targeted_research(client, question, candidate_name, logger, trace_id)


# ============================================================================
# [CORE] Build the initial user message for the builder agent
# ============================================================================

def _build_initial_message(
    candidate: ScreenedCandidate,
    input_data: Agent5Input,
    credentials: dict[str, str] | None = None,
    staged_test_cases: list[TestCase] | None = None,
    sandbox_dir: Path | None = None,
    api_spec_pre_rendered: bool = False,
) -> str:
    """
    Build the first user message for the builder agent. Includes all seed
    knowledge from Agent 4's screening + context from Agent 1/3 + credential hints.

    When `sandbox_dir` is provided, the message enumerates any
    `fetched_docs_*.txt` files Agent 4 prefetched during screening —
    those are the builder's STEP 1 targets for `read_file` instead of
    hitting web_fetch again. See puzzleeval/web_doc_cache.py.

    When `api_spec_pre_rendered=True`, an api_spec.txt skeleton has
    already been written to the sandbox by
    `_synthesize_api_spec_from_checklist` (NEW-AM). The initial message
    surfaces this so Sonnet's Phase 1 can read it as a starting point
    and either skip directly to Phase 2 build (saves the 6-min Sonnet
    research turn) or augment only the gap fields.
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
{_format_checklist_context_for_builder(candidate)}
{_format_modality_context_for_builder(input_data)}
{_format_sandbox_contents_block(sandbox_dir, staged_test_cases)}
{_format_prefetched_docs_block(sandbox_dir)}
{(
    "---\n\n"
    "### ⚡ FAST PATH — api_spec.txt PRE-RENDERED (with cross-check + augment)\n\n"
    "Agent 4's BuildReadinessChecklist provided some-or-all of the "
    "build-readiness fields with evidence-backed source URLs. We have "
    "already pre-rendered `api_spec.txt` into your sandbox — read it "
    "first via `read_file('api_spec.txt')`.\n\n"
    "**Architectural principle:** Agent 4's job is verification (does the "
    "API exist with the 4 non-negotiables findable in docs?). YOUR job "
    "is to OWN the full-picture understanding. The pre-rendered spec is "
    "a HEAD START you can verify+extend, not a contract you must accept "
    "blindly. You always have these fallback paths to discover anything "
    "the checklist missed:\n"
    "  - `verified_api_docs_url` (always available — read it)\n"
    "  - `fetched_docs_*.txt` (prefetched by Agent 4 — read_file them)\n"
    "  - `web_fetch` (max_uses=5; fetch any new URL)\n"
    "  - `web_search` (max_uses=4; discover endpoints)\n"
    "  - `ask_research` (delegate targeted research mid-build)\n\n"
    "**Status semantics — three tiers in the spec:**\n\n"
    "  - **Authoritative** (no marker): Agent 4 confirmed with evidence "
    "URL. **CROSS-CHECK on Phase A**: when you read the prefetched docs, "
    "verify the value matches what the docs show. If they agree, trust "
    "for the rest of the build. If they disagree, downgrade to "
    "[INFERRED] in your mental model and verify on first API call.\n"
    "  - **`[INFERRED — verify on first call]`**: Agent 4 found indirect "
    "evidence. Use the value, but the first live API call will catch "
    "errors fast.\n"
    "  - **`[REQUIRES_AUGMENT]`**: not in checklist (per-test-case, "
    "auxiliary, or unknown). Research these targeted via web_fetch / "
    "web_search / read_file on prefetched docs.\n\n"
    "**Critical completeness check:** the checklist's REQUEST_BODY_SHAPE "
    "+ RESPONSE_BODY_SHAPE capture the PRIMARY message shape, NOT all "
    "variants. The `OVERRIDE_OR_SPECIAL_MESSAGE_SHAPES` section is a "
    "REQUIRES_AUGMENT trigger for variants needed by your test cases "
    "(WebSocket lifecycle events, REST PATCH/PUT bodies, override "
    "messages for config injection, etc.). Real example from prior runs: "
    "ElevenLabs ConvAI's `conversation_initiation_client_data` has an "
    "OVERRIDE variant for mid-stream system prompt injection that the "
    "basic init shape doesn't show — missing it breaks system prompt "
    "passing entirely.\n\n"
    "**Workflow:**\n"
    "  1. `read_file('api_spec.txt')` — understand what's done + what's marked\n"
    "  2. `read_file('fetched_docs_0.txt')` etc — Phase A cross-check: "
    "do the authoritative values in the spec match what the docs show? "
    "If yes, trust them. If no, treat as INFERRED and verify.\n"
    "  3. List the `[REQUIRES_AUGMENT]` and `[INFERRED]` sections explicitly\n"
    "  4. Match against your test cases — which augment sections are "
    "actually needed?\n"
    "  5. Research ONLY those (web_fetch / read_file on prefetched docs / "
    "ask_research). Skip augment sections your test cases don't exercise.\n"
    "  6. Use patch_file or write_file to update api_spec.txt with the "
    "augmented sections (replace [REQUIRES_AUGMENT] markers).\n"
    "  7. The PROVIDER_SURFACE alternatives section lists the other "
    "endpoints Agent 4 considered. If your chosen endpoint turns out "
    "wrong for the test case input shape, switch to one of the "
    "[alternative] endpoints. If even those don't fit, web_search the "
    "docs URL for more endpoints — provider_surface is a head start, "
    "NOT an exhaustive list.\n"
    "  8. Proceed to Phase 2 build.\n\n"
    "Real-run measurement (trace 8ded6706, 2026-04-25): pre-NEW-AM "
    "Sonnet's full research turn took ~6 minutes / $1.20 per candidate, "
    "of which ~60% was re-extracting fields the checklist already had. "
    "Expected with NEW-AM: ~2-3 minutes / $0.50-0.60 per candidate "
    "(cross-check + targeted augment). If your candidate's checklist had "
    "fewer confirmed fields, expect closer to 4-5 min (more augment "
    "scope). Either way better than 6 min.\n"
) if api_spec_pre_rendered else ""}
---

Start with Phase A inventory: read the BuildReadinessChecklist Agent 4
produced (in your candidate's metadata above) and the prefetched docs
listed in your sandbox inventory. The checklist tells you which of the
ten build-readiness fields are confirmed, inferred, or unknown; the
prefetched docs back the confirmed entries with source URLs you can
cross-check. From there, run Phase B (gap analysis for THIS test case)
to identify which unknowns actually matter, Phase C (targeted research
to fill them), Phase D (write api_spec.txt as the contract), then
Phase E (build harness.py).{
    " **Phase D is already partially done — see the FAST PATH section "
    "above; you may be able to skip directly to Phase E if the "
    "pre-rendered spec is sufficient for your test cases.**"
    if api_spec_pre_rendered else ""
}"""

    return message


def _format_sandbox_contents_block(
    sandbox_dir: Path | None,
    staged_test_cases: list[TestCase] | None,
) -> str:
    """Back-compat shim — use ``agent5.initial_message.format_sandbox_contents_block``."""
    from puzzleeval.agents.agent5.initial_message import format_sandbox_contents_block
    return format_sandbox_contents_block(sandbox_dir, staged_test_cases)



# Soft heuristic for ordering prefetched files in the inventory — pages
# with code fences, HTTP endpoints, auth headers, or WebSocket
# protocol mentions tend to be more useful starting reads than nav-link
# pages. This is a RANKING SIGNAL ONLY; nothing is gated on it. The
# builder still decides what to read via Phase A inventory.
#
# Replaces the prior density-tier system (HIGH/MEDIUM/THIN with
# inline-vs-listed branching), which conflated "this page has structural
# richness" with "this page tells you how to BUILD" — two different
# questions. The checklist now answers the second question directly via
# Agent 4's per-field source URLs; the prefetched files are background
# reading material, ordered by a soft proxy for usefulness.
# Phase 3.4.a: USEFULNESS_PATTERNS + usefulness_signal moved to
# agent5.initial_message. Re-exported here as shims.
from puzzleeval.agents.agent5.initial_message import (
    USEFULNESS_PATTERNS as _USEFULNESS_PATTERNS,
    usefulness_signal as _usefulness_signal_canonical,
)


def _usefulness_signal(content: str) -> int:
    """Back-compat shim — use ``agent5.initial_message.usefulness_signal``."""
    return _usefulness_signal_canonical(content)



def _format_prefetched_docs_block(sandbox_dir: Path | None) -> str:
    """Back-compat shim — use ``agent5.initial_message.format_prefetched_docs_block``."""
    from puzzleeval.agents.agent5.initial_message import format_prefetched_docs_block
    return format_prefetched_docs_block(sandbox_dir)



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


# ----------------------------------------------------------------------------
# Phase 4.2: Build setup result type
# ----------------------------------------------------------------------------
# `_setup_sandbox_and_credentials` returns one of three shapes:
#   1. BuildSetupSuccess — full setup data, caller proceeds to the build loop
#   2. TestHarness — OpenAPI fastpath generated a complete harness; return as-is
#   3. FailedHarness — venv creation failed; return as-is
#
# The caller pattern:
#   setup = _setup_sandbox_and_credentials(...)
#   if isinstance(setup, (TestHarness, FailedHarness)):
#       return setup
#   # ... continue with setup.candidate_label, setup.staged_test_cases, etc.
@dataclass(frozen=True)
class BuildSetupSuccess:
    """Successful build setup — all fields populated for the loop to use."""
    candidate_label: str
    trace_id: str
    provider_slug: str
    staged_test_cases: list
    credentials: dict[str, str] | None
    pre_rendered_spec: str | None


def _finalize_build_result(
    candidate: "ScreenedCandidate",
    input_data: "Agent5Input",
    sandbox_dir: Path,
    *,
    conversation_log: list,
    credentials: dict[str, str] | None,
    provider_slug: str,
    turn: int,
    last_text: str,
    accumulated_cost: float,
    candidate_web_fetch_blocks: int,
    smoke_ever_passed: bool,
    verification_attempts: int,
    verification_passed: bool,
) -> "TestHarness | FailedHarness":
    """Post-loop result assembly: read harness.py, decide TestHarness vs FailedHarness.

    Phase 4.4 extraction: pulled out of ``_build_single_harness`` to give
    the post-loop assembly logic a clear name + isolated tests. Caller
    has finished the build loop with state captured in the keyword args
    above.

    Returns:
        FailedHarness when:
          * harness.py does not exist (build never produced output)
          * smoke test never passed AND verification gate didn't pass
        TestHarness in the success case (or partial-success: smoke passed
        but live validation didn't fully complete).
    """
    # Save conversation log for debugging — best-effort, never raises
    _save_conversation_log(sandbox_dir, conversation_log, candidate.name)

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
    smoke_passed = (
        smoke_ever_passed
        or "SMOKE TEST PASSED" in last_text
        or "HARNESS_COMPLETE" in last_text
    )

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
    input_types: set[str] = set()
    output_types: set[str] = set()
    for tc in input_data.test_cases.test_cases:
        for st_name in candidate.relevant_subtasks:
            if st_name in tc.sub_task_ref or tc.sub_task_ref in st_name:
                input_types.add(tc.input_type)
                output_types.add(tc.output_type)
    if not input_types:
        for tc in input_data.test_cases.test_cases:
            input_types.add(tc.input_type)
            output_types.add(tc.output_type)

    validation_parts = [f"Smoke: {'PASS' if smoke_passed else 'INCOMPLETE'}"]
    validation_parts.append(
        f"Verification gate: {verification_attempts} retries, "
        f"{'PASSED' if verification_passed else 'EXHAUSTED'}"
    )

    # Read api_spec.txt as comprehensive API knowledge for downstream consumers
    api_knowledge = None
    api_spec_path = sandbox_dir / "api_spec.txt"
    if api_spec_path.exists():
        try:
            api_knowledge = api_spec_path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 — best-effort read
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


def _setup_sandbox_and_credentials(
    candidate: "ScreenedCandidate",
    input_data: "Agent5Input",
    sandbox_dir: Path,
    logger,
    progress_callback: "Callable[[str, dict], None] | None" = None,
) -> "BuildSetupSuccess | TestHarness | FailedHarness":
    """Stage test files + run OpenAPI fastpath + create venv + resolve credentials + pre-render api_spec.

    Phase 4.2 extraction: pulled out of ``_build_single_harness`` to reduce its
    size and isolate the setup-with-early-returns logic into a focused helper.

    Returns:
        BuildSetupSuccess: when the build loop should proceed.
        TestHarness: when the OpenAPI fastpath generated a complete harness
                     mechanically (no LLM build turns needed). Caller should
                     return this directly.
        FailedHarness: when venv creation failed AND the venv python doesn't
                       exist. Caller should return this directly.
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
    staged_test_cases = _stage_test_files(
        input_data.test_cases.test_cases, sandbox_dir, logger, trace_id,
    )

    # ──────────────────────────────────────────────────────────────────
    # OpenAPI auto-generation fast path. If the candidate's atlas
    # carries an openapi_url, generate the harness mechanically — no LLM
    # build turns. The build loop only runs when this fast path fails.
    # ──────────────────────────────────────────────────────────────────
    openapi_harness_text = _try_openapi_fastpath(candidate, sandbox_dir, logger, trace_id)
    if openapi_harness_text:
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
            # NOTE: this _create_venv call passes only 2 args; the canonical
            # signature requires 4 (sandbox_dir, logger, trace_id, candidate_name).
            # Preserved as-is for behavior parity — if this ever runs in
            # production it would raise TypeError, which the surrounding
            # try/except catches and falls back to LLM build. Real-run
            # traces don't show this firing on the openapi_fastpath success
            # path, so the latent issue stays as documented technical debt
            # rather than a behavior change in this refactor.
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
        except Exception as exc:  # noqa: BLE001 — fastpath fallback is intentional
            logger.warning(
                f"openapi-fastpath: write/setup failed for {candidate.name}: {exc} — "
                f"falling back to LLM build",
                extra={"operation": "openapi_fastpath_fallback", "trace_id": trace_id},
            )

    # ★ Create isolated venv for this candidate
    venv_ok = _create_venv(sandbox_dir, logger, trace_id, candidate.name)
    if not venv_ok:
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

    # ── Pre-render api_spec.txt from BuildReadinessChecklist (NEW-AM) ──
    # When Agent 4's checklist is complete enough, pre-render the api_spec
    # skeleton. Sonnet sees it on Phase A inventory and can skip ~6 min of
    # research. Falls through gracefully when checklist isn't complete.
    pre_rendered_spec = _synthesize_api_spec_from_checklist(candidate)
    if pre_rendered_spec:
        try:
            (sandbox_dir / "api_spec.txt").write_text(
                pre_rendered_spec, encoding="utf-8",
            )
            logger.info(
                f"Pre-rendered api_spec.txt from checklist for {candidate.name}",
                extra={
                    "operation": "checklist_prerender_spec",
                    "trace_id": trace_id,
                    "candidate_name": candidate.name,
                    "spec_chars": len(pre_rendered_spec),
                },
            )
        except OSError as exc:
            logger.warning(
                f"Failed to pre-render api_spec for {candidate.name}: {exc}",
                extra={"operation": "checklist_prerender_failed",
                       "trace_id": trace_id},
            )
            pre_rendered_spec = None

    return BuildSetupSuccess(
        candidate_label=candidate_label,
        trace_id=trace_id,
        provider_slug=provider_slug,
        staged_test_cases=staged_test_cases,
        credentials=credentials,
        pre_rendered_spec=pre_rendered_spec,
    )


def _build_single_harness(
    client: anthropic.Anthropic,
    candidate: ScreenedCandidate,
    input_data: Agent5Input,
    sandbox_dir: Path,
    logger,
    progress_callback: "Callable[[str, dict], None] | None" = None,
) -> TestHarness | FailedHarness:
    """Legacy entry point — Phase 5 Step 3 delegation shim.

    The real implementation lives in
    ``puzzleeval.agents.agent5.build_loop.build_single_harness``.
    Source-grep tests that previously read this function via
    ``inspect.getsource(ite._build_single_harness)`` follow the alias
    transparently because Python's ``inspect`` follows ``__code__``
    via ``__wrapped__``-aware lookups when available; for tests that
    don't, retarget to ``inspect.getsource(build_loop.build_single_harness)``.

    Same call signature as before — callers (the ThreadPoolExecutor
    in ``run_implement_test_env_agent``) need no changes.
    """
    from puzzleeval.agents.agent5.build_loop import build_single_harness
    return build_single_harness(
        client, candidate, input_data, sandbox_dir, logger, progress_callback,
    )

def _resolve_credentials(
    input_data: Agent5Input,
    candidate: ScreenedCandidate,
    provider_slug: str,
) -> dict[str, str] | None:
    """Back-compat shim — use ``puzzleeval.agents.agent5.sandbox.resolve_credentials``."""
    from puzzleeval.agents.agent5.sandbox import resolve_credentials
    return resolve_credentials(input_data, candidate, provider_slug)


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
    """Back-compat shim — use ``puzzleeval.agents.agent5.sandbox.extract_env_vars_from_code``."""
    from puzzleeval.agents.agent5.sandbox import extract_env_vars_from_code
    return extract_env_vars_from_code(code)


def _env_var_similarity(a: str, b: str) -> float:
    """Back-compat shim — use ``agent5.sandbox.env_var_similarity``."""
    from puzzleeval.agents.agent5.sandbox import env_var_similarity
    return env_var_similarity(a, b)


def _compute_conversation_summary(
    conversation_log: list[dict],
    candidate_name: str,
) -> dict:
    """Back-compat shim — use ``agent5.conversation_log.compute_conversation_summary``."""
    from puzzleeval.agents.agent5.conversation_log import compute_conversation_summary
    return compute_conversation_summary(conversation_log, candidate_name)

def _save_conversation_log(
    sandbox_dir: Path,
    conversation_log: list[dict],
    candidate_name: str,
) -> None:
    """Back-compat shim — use ``agent5.conversation_log.save_conversation_log``."""
    from puzzleeval.agents.agent5.conversation_log import save_conversation_log
    return save_conversation_log(sandbox_dir, conversation_log, candidate_name)

def _categorize_failure(text: str) -> str:
    """Back-compat shim — use ``agent5.conversation_log.categorize_failure``."""
    from puzzleeval.agents.agent5.conversation_log import categorize_failure
    return categorize_failure(text)


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
# ============================================================================
# Phase 2: Venv management — moved to puzzleeval.agents.agent5.sandbox
# ============================================================================
# Module-level state (_VENV_CREATE_LOCKS, _VENV_CREATE_LOCKS_GUARD) and the
# venv lifecycle functions live in agent5/sandbox.py. The names below are
# re-exports for back-compat with source-grep tests + external callers
# (notably puzzleeval-api/services/pipeline_runner.py which imports
# precreate_venvs_for_candidates from this module).
#
# Critical R2 invariant: _VENV_CREATE_LOCKS is module-level state. The
# `dict` object below is the SAME OBJECT as agent5.sandbox._VENV_CREATE_LOCKS
# — they alias the same dict. Tests verify this identity.
from puzzleeval.agents.agent5.sandbox import (
    VENV_PREINSTALL_MANIFEST,
    _VENV_CREATE_LOCKS,
    _VENV_CREATE_LOCKS_GUARD,
    create_venv as _create_venv_canonical,
    venv_python_path as _venv_python_path_canonical,
    acquire_venv_lock as _acquire_venv_lock_canonical,
    precreate_venvs_for_candidates as _precreate_venvs_canonical,
    preinstall_venv_deps as _preinstall_venv_deps_canonical,
)


def _venv_lock_for(sandbox_dir: Path) -> threading.Lock:
    """Back-compat shim — use ``agent5.sandbox.acquire_venv_lock``."""
    return _acquire_venv_lock_canonical(sandbox_dir)


def _venv_python_path(venv_dir: Path) -> Path:
    """Back-compat shim — use ``agent5.sandbox.venv_python_path``."""
    return _venv_python_path_canonical(venv_dir)


def _create_venv(sandbox_dir: Path, logger, trace_id: str, candidate_name: str) -> bool:
    """Back-compat shim — use ``agent5.sandbox.create_venv``."""
    return _create_venv_canonical(sandbox_dir, logger, trace_id, candidate_name)


def precreate_venvs_for_candidates(
    candidate_names: list[str],
    trace_id: str,
    logger,
    runs_root: "Path | str | None" = None,
    max_workers: int | None = None,
) -> dict[str, bool]:
    """Back-compat re-export — canonical home is
    ``puzzleeval.agents.agent5.sandbox.precreate_venvs_for_candidates``.
    Used by ``puzzleeval-api/services/pipeline_runner.py``.
    """
    return _precreate_venvs_canonical(
        candidate_names, trace_id, logger,
        runs_root=runs_root, max_workers=max_workers,
    )


def _preinstall_venv_deps(
    venv_dir: Path,
    logger,
    trace_id: str,
    candidate_name: str,
) -> None:
    """Back-compat shim — use ``agent5.sandbox.preinstall_venv_deps``."""
    return _preinstall_venv_deps_canonical(
        venv_dir, logger, trace_id, candidate_name,
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
    """Back-compat shim — use ``puzzleeval.agents.agent5.verification.run_verification_checks``."""
    from puzzleeval.agents.agent5.verification import run_verification_checks
    return run_verification_checks(sandbox_dir, candidate, credentials, logger, trace_id)


# ============================================================================
# Post-Build Test Execution Functions
# ============================================================================
# These functions handle mechanical test execution and evaluation AFTER
# harnesses are built. Agent 5 owns the full lifecycle: build + test.
# ============================================================================

RATE_LIMIT_INDICATORS = {"rate limit", "429", "too many requests", "quota exceeded"}

# ────────────────────────────────────────────────────────────────────
# Concurrent-session cap detection (distinct from rate limits).
#
# Rate limits: "you made too many requests in N seconds, back off for
# a few seconds." Typical reset time: 1-60s.
#
# Concurrent-session caps: "you already have N open sessions, can't
# open another until one closes." Typical release time: 15-30s (a
# full conversation duration). WebSocket voice APIs and chat
# platforms expose this. Free tiers often cap at 1 per key; paid
# tiers at 3-10.
#
# The error texts are provider-specific — this set covers the common
# English phrasings. Additive: if a real run surfaces a new phrasing,
# add the token here. The cost of a false positive (treating a non-
# cap error as a cap error) is one unnecessary sleep+retry; the cost
# of a false negative (missing a cap error) is a test fails when it
# could have waited. Bias slightly toward false positives.
# ────────────────────────────────────────────────────────────────────
CONCURRENT_SESSION_INDICATORS = {
    "concurrent session",
    "concurrent sessions",
    "session limit",
    "max sessions",
    "maximum sessions",
    "too many concurrent",
    "concurrent connections",
    "maximum concurrent",
    "maximum number of concurrent calls",
    "session in use",
    "session already active",
    "session is already active",
    "concurrent call limit",
    "concurrent conversation",
    "active session exists",
}

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



# ============================================================================
# Phase 6.2 re-exports: evaluation symbols moved to agent5/evaluation.py
# Legacy callers (other modules + source-grep tests) keep working through
# these aliases; canonical home is puzzleeval.agents.agent5.evaluation.
# ============================================================================
from puzzleeval.agents.agent5.evaluation import (
    RAW_RESPONSE_MAX_CHARS,
    build_evaluation_prompt as _build_evaluation_prompt,
    compute_weighted_score as _compute_weighted_score,
    evaluate_exact_match as _evaluate_exact_match,
    evaluate_format_compliance as _evaluate_format_compliance,
    evaluate_mechanical as _evaluate_mechanical,
    evaluate_with_llm as _evaluate_with_llm,
    try_parse_number as _try_parse_number,
)

# ============================================================================
# Phase 6.1 re-exports: execution symbols moved to agent5/execution.py
# Legacy callers + source-grep tests keep working through these aliases;
# canonical home is puzzleeval.agents.agent5.execution.
# ============================================================================
from puzzleeval.agents.agent5.execution import (
    adapt_test_input as _adapt_test_input,
    compute_aggregate_metrics as _compute_aggregate_metrics,
    execute_all_tests as _execute_all_tests,
    execute_single_test as _execute_single_test,
    execute_test_with_session_retry as _execute_test_with_session_retry,
    inflate_b64_sentinels as _inflate_b64_sentinels,
    needs_plugin_synthesis as _needs_plugin_synthesis,
    run_single_test_with_rate_limit as _run_single_test_with_rate_limit,
    synthesize_test_input_via_plugin as _synthesize_test_input_via_plugin,
)







def _is_rate_limit_error(error_msg: str | None) -> bool:
    """Check if an error message indicates a rate limit."""
    if not error_msg:
        return False
    lower = error_msg.lower()
    return any(indicator in lower for indicator in RATE_LIMIT_INDICATORS)


def _is_concurrent_session_error(error_msg: str | None) -> bool:
    """Check if an error message indicates the provider's concurrent-
    session cap was exceeded (distinct from rate limits).

    See CONCURRENT_SESSION_INDICATORS for the token list + rationale.
    Case-insensitive substring match. Returns False for None/empty.
    """
    if not error_msg:
        return False
    lower = error_msg.lower()
    return any(indicator in lower for indicator in CONCURRENT_SESSION_INDICATORS)








def _resolve_candidate_credentials(
    harness: TestHarness,
    provider_credentials: dict[str, dict[str, str]] | None,
) -> dict[str, str] | None:
    """Back-compat shim — use ``agent5.sandbox.resolve_candidate_credentials``."""
    from puzzleeval.agents.agent5.sandbox import resolve_candidate_credentials
    return resolve_candidate_credentials(harness, provider_credentials)
























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

            # ─────────────────────────────────────────────────────────
            # Background audio-merge join (item 5 of
            # PLAN_VOICE_RUN_OPTIMIZATIONS.md)
            # ─────────────────────────────────────────────────────────
            # The voice_realtime plugin submits per-test audio merges
            # to a daemon thread pool to free worker threads at
            # conversation-end (instead of conversation-end + 70s
            # merge-time). We now block until every pending merge for
            # this candidate's tests resolves (or times out at
            # PUZZLEEVAL_VOICE_MERGE_TIMEOUT_S, default 60s). For each
            # resolved merge, patch the merged_audio_path + role-
            # 'conversation' audio entry into the matching test result
            # by session_token. No-op when no voice tests ran in this
            # candidate (wait_for_pending_merges returns {}).
            try:
                from puzzleeval.tool_plugins import get_plugin
                voice_plugin = get_plugin("voice_realtime")
            except Exception:  # noqa: BLE001
                voice_plugin = None
            if voice_plugin is not None and hasattr(
                voice_plugin, "wait_for_pending_merges",
            ):
                merge_results = voice_plugin.wait_for_pending_merges()
                if merge_results:
                    for _tc, _r in raw_results:
                        sess = _r.get("session_token") if isinstance(_r, dict) else None
                        if not sess or sess not in merge_results:
                            continue
                        merged = merge_results[sess]
                        if not merged:
                            continue
                        _r["merged_audio_path"] = merged
                        # Prepend the merged conversation entry so UIs
                        # that play "first audio" default to the full
                        # call. Match the role tagging used by
                        # voice_realtime's pre-bg-merge code path.
                        existing = _r.get("audio_paths", []) or []
                        _r["audio_paths"] = [
                            {"role": "conversation",
                             "path": merged,
                             "token": sess}
                        ] + existing

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
                verdict_detail: dict | None = None,
                tool_runner_cost_usd: float = 0.0,
            ) -> None:
                """Apply a verdict's numerics back onto the TestCaseResult.

                Shared between deterministic + tool_runner paths so the
                weighted-score + telemetry fields end up identical
                regardless of strategy.

                ``verdict_detail`` carries the plugin's full detail dict —
                used to extract rubric_verdict + transcript for agentic
                conversational tests. Both fields land on the TCR when
                present so the EvaluationReport assembler + frontend can
                render the rubric breakdown + transcript view.
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

                # ── Per-test cost + latency aggregation (NEW-AM gap fix) ──
                # Real-run audit (2026-04-25, trace 8ded6706) showed every
                # TestCaseResult had `cost_usd: None` and `latency_ms: 0`
                # despite agentic conversational tests genuinely costing
                # ~$0.03-0.10/test (user simulator turns + rubric judge call).
                # Pre-fix: eval_cost was accumulated per-CANDIDATE only; the
                # per-TEST breakdown was lost, so test_cost_usd reported
                # $0.00 in summaries even when ~$0.50/run was actually spent.
                #
                # General fix: when verdict_detail carries the plugin-side
                # cost components (simulator_cost_usd, judge_cost_usd —
                # populated by voice_realtime + conversation_simulator
                # agentic paths), sum them with the harness-side cost
                # (already on tcr_target.cost_usd from harness.run()) and
                # the tool_runner Claude cost. Same shape for latency.
                if isinstance(verdict_detail, dict) or tool_runner_cost_usd:
                    test_cost = float(getattr(tcr_target, "cost_usd", None) or 0.0)
                    if isinstance(verdict_detail, dict):
                        test_cost += float(verdict_detail.get("simulator_cost_usd") or 0.0)
                        test_cost += float(verdict_detail.get("judge_cost_usd") or 0.0)
                    # tool_runner's own Claude cost (from picking + invoking
                    # plugins) — passed in as a separate arg so the per-test
                    # number reflects ALL Anthropic spend on this test, not
                    # just the harness/plugin breakdown.
                    test_cost += float(tool_runner_cost_usd or 0.0)
                    if test_cost > 0:
                        tcr_target.cost_usd = round(test_cost, 6)

                    # Latency: prefer plugin-reported total_duration_s,
                    # fall back to harness latency_ms. Multi-turn voice
                    # conversations are dominated by per-turn latency
                    # accumulation; single-turn tests by the one harness
                    # call. Either way, surfacing wall-clock per test
                    # lets the operator spot slow tests in the report
                    # without parsing transcripts.
                    if isinstance(verdict_detail, dict):
                        plugin_dur_s = verdict_detail.get("total_duration_s")
                        if plugin_dur_s is not None:
                            try:
                                tcr_target.latency_ms = round(float(plugin_dur_s) * 1000, 2)
                            except (TypeError, ValueError):
                                pass

                # ── Agentic conversational eval — rubric_verdict + transcript ──
                # Plugins that ran the agentic path attach these to their
                # verdict.detail dict (see voice_realtime._drive_conversation_
                # agentic + conversation_simulator._evaluate_agentic). We
                # surface BOTH onto the TestCaseResult so:
                #   - report.py's _extract_test_evidence reads them into
                #     TestEvidence (frontend renders the rubric breakdown)
                #   - Downstream analysis can correlate per-criterion
                #     scores with specific turns
                # Non-conversational tests pass verdict_detail={} or without
                # these keys — the getattr chain safely skips.
                if isinstance(verdict_detail, dict):
                    raw_verdict = verdict_detail.get("rubric_verdict")
                    if isinstance(raw_verdict, dict):
                        try:
                            from puzzleeval.schemas import RubricVerdict as _RV
                            tcr_target.rubric_verdict = _RV.model_validate(raw_verdict)
                        except Exception as _exc:  # noqa: BLE001
                            # Malformed verdict dict — log but don't fail
                            # the test; the other signals still flow.
                            logger.debug(
                                "rubric_verdict validation failed: %s", _exc,
                            )
                    raw_transcript = verdict_detail.get("transcript")
                    if isinstance(raw_transcript, list):
                        from puzzleeval.schemas import ConversationTurn as _CT
                        clean_turns = []
                        for t in raw_transcript:
                            if isinstance(t, dict) and "role" in t and "text" in t:
                                try:
                                    clean_turns.append(_CT.model_validate(t))
                                except Exception:  # noqa: BLE001
                                    # Skip malformed entries; partial
                                    # transcript is still useful.
                                    pass
                        if clean_turns:
                            tcr_target.transcript = clean_turns

            # ──────────────────────────────────────────────────────────
            # Shared lock for evaluation phase mutations.
            #
            # Both _run_deterministic and _run_tool_runner now run their
            # per-item work in parallel via ThreadPoolExecutor. The
            # expensive parts (plugin.evaluate_output, evaluate_with_
            # tool_runner — each can take 15-30s for multi-turn
            # conversations) happen OUTSIDE this lock. The lock only
            # guards the fast shared-state mutations that follow:
            #   - eval_cost accumulation (+=)
            #   - plugin_handled_ids.add(...)
            #   - tcr_target = next(...) lookup + _promote_verdict_to_tcr
            #     (which mutates the TCR object in test_case_results)
            #
            # Thread safety reasoning:
            #   - rate_limiter has its own internal lock
            #   - plugin.evaluate_output / evaluate_with_tool_runner are
            #     pure with respect to test_case_results (they return a
            #     verdict; mutation happens AFTER under the lock)
            #   - subprocess execution inside drive_conversation is
            #     thread-safe (each subprocess has its own PID)
            #   - voice_realtime + conversation_simulator plugins are
            #     thread-local on session_dir + locked on _token_to_audio
            #     (already audited per AD-004)
            # ──────────────────────────────────────────────────────────
            eval_state_lock = threading.Lock()

            # Multi-turn vs single-turn split — drives which parallelism
            # knob applies. Multi-turn tests open a separate provider
            # session each (WebSocket / conversation_id / etc.) and run
            # 15-30s of harness.run() calls; we cap their concurrency at
            # SESSION_PARALLELISM (default 2) to stay inside common
            # free-tier session caps. Single-turn tests are short
            # API+LLM-judge round-trips; they get the higher PARALLELISM
            # cap (default 3).
            from puzzleeval.config import (
                AGENT6_PER_CANDIDATE_PARALLELISM,
                AGENT6_PER_CANDIDATE_SESSION_PARALLELISM,
            )
            _multi_turn_input = {"conversation", "voice_conversation", "voice_turn"}
            _multi_turn_output = {"voice_conversation", "voice_turn"}

            def _is_multi_turn_eval(_tc) -> bool:
                return (
                    (getattr(_tc, "input_type", "") or "") in _multi_turn_input
                    or (getattr(_tc, "output_type", "") or "") in _multi_turn_output
                )

            def _dispatch_eval_items_in_parallel(items, worker):
                """Run ``worker(item)`` for every item, parallelized by modality.

                Multi-turn items run at SESSION_PARALLELISM; single-turn
                items run at PARALLELISM. Both buckets dispatch
                concurrently inside themselves but the buckets run
                sequentially to keep log ordering clean — the expensive
                bucket (multi-turn) typically finishes after the
                single-turn bucket, so total wall-clock is near
                max(bucket_a_time, bucket_b_time).

                Thread-local session_dir propagation (AD-004 + real-run
                trace 94271de4 fix): ``set_session_dir`` was called on
                the OUTER candidate-level thread above (line ~7255),
                but voice_realtime stores it in ``threading.local()``
                so the new ThreadPoolExecutor worker threads spawned
                here don't inherit it. Without this re-set, voice
                artifacts wrote to %TEMP%/puzzleeval_voice/ instead of
                ``runs/<trace>/harnesses/<slug>/voice/`` — the backend
                audio-streaming containment check then 404'd every
                playback request from the frontend. The wrapper below
                re-applies set_session_dir on each worker thread's
                first call, restoring AD-004 across the parallelism
                boundary.
                """
                # Capture the session dir from the outer thread so
                # every worker can re-apply it on its own thread-local.
                _worker_voice_session_dir = sandbox_dir / "voice"

                def _worker_with_session_dir(item):
                    # Re-set session_dir on this thread's local. Cheap
                    # (~10 plugins, each is a setattr); idempotent;
                    # safe to call from any thread.
                    try:
                        from puzzleeval.tool_plugins import list_plugins as _lp
                        for _p in _lp():
                            if hasattr(_p, "set_session_dir"):
                                try:
                                    _p.set_session_dir(_worker_voice_session_dir)
                                except Exception:  # noqa: BLE001
                                    pass
                    except Exception:  # noqa: BLE001
                        pass  # plugin discovery failure is best-effort
                    return worker(item)

                multi_turn = [it for it in items if _is_multi_turn_eval(it[0])]
                single_turn = [it for it in items if not _is_multi_turn_eval(it[0])]

                # Single-turn first — usually finishes fast
                if single_turn:
                    workers = max(1, min(
                        AGENT6_PER_CANDIDATE_PARALLELISM, len(single_turn),
                    ))
                    if workers == 1:
                        for it in single_turn:
                            _worker_with_session_dir(it)
                    else:
                        with ThreadPoolExecutor(
                            max_workers=workers,
                            thread_name_prefix=f"puzzleeval-eval-st-{harness.candidate_name[:16]}",
                        ) as ex:
                            futures = [ex.submit(_worker_with_session_dir, it) for it in single_turn]
                            for fut in as_completed(futures):
                                fut.result()  # propagate exceptions

                if multi_turn:
                    workers = max(1, min(
                        AGENT6_PER_CANDIDATE_SESSION_PARALLELISM, len(multi_turn),
                    ))
                    if workers == 1:
                        for it in multi_turn:
                            _worker_with_session_dir(it)
                    else:
                        with ThreadPoolExecutor(
                            max_workers=workers,
                            thread_name_prefix=f"puzzleeval-eval-mt-{harness.candidate_name[:16]}",
                        ) as ex:
                            futures = [ex.submit(_worker_with_session_dir, it) for it in multi_turn]
                            for fut in as_completed(futures):
                                fut.result()

            def _run_deterministic() -> None:
                """Legacy enum-based dispatch path. Mutates eval_items in place.

                Per-item work runs in parallel via _dispatch_eval_items_
                in_parallel — multi-turn evals at SESSION_PARALLELISM,
                single-turn at PARALLELISM. Shared state mutations are
                guarded by eval_state_lock.
                """
                from puzzleeval.modality import detect_for_test_case

                def _process_one(eval_item) -> None:
                    tc_eval, result_eval, criteria_eval = eval_item
                    reqs = detect_for_test_case(
                        input_type=tc_eval.input_type,
                        output_type=tc_eval.output_type,
                    )
                    evaluator = next(iter(reqs.output_evaluators), None)
                    if evaluator is None:
                        return
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
                                    _it=tc_input_type,
                                    _logger=logger,
                                    _trace_id=input_data.trace_id,
                                    _cn=candidate.name):
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
                            # Session-cap retry: if the provider returns
                            # "max concurrent sessions" on this per-turn
                            # call, the wrapper waits for a slot and
                            # retries before returning. See
                            # _execute_test_with_session_retry for the
                            # full semantics. Transparent to the plugin's
                            # drive-loop — it sees a longer latency on
                            # the affected turn but no error.
                            return _execute_test_with_session_retry(
                                _sd, merged, _c, _t,
                                _logger, _trace_id, _cn,
                            )
                        runner_for_plugin = _runner
                    try:
                        # Pass agentic-eval context through to the plugin.
                        # Conversational plugins (voice_realtime,
                        # conversation_simulator) detect persona+goal+rubric
                        # and route to the agentic drive loop. When absent,
                        # plugins fall back to their scripted path. This
                        # is the SINGLE wiring seam that makes
                        # Agent 3's persona/goal/rubric actually reach
                        # evaluation time. See schemas.TestCase for the
                        # per-test override semantics.
                        verdict = evaluator.evaluate_output(
                            response=result_eval.get("raw_response") or result_eval.get("output"),
                            expected=tc_eval.expected_output,
                            criteria=[
                                {"criterion": c["criterion"], "weight": c["weight"],
                                 "eval_type": c.get("eval_type", "subjective_quality")}
                                for c in criteria_eval
                            ],
                            harness_runner=runner_for_plugin,
                            persona=getattr(tc_eval, "persona", None),
                            goal=getattr(tc_eval, "goal", None),
                            constraints=getattr(tc_eval, "constraints", None) or [],
                            rubric=getattr(tc_eval, "rubric", None) or [],
                            max_turns=getattr(tc_eval, "max_turns", 6),
                            evaluation_mode=getattr(tc_eval, "evaluation_mode", "auto"),
                            trace_id=input_data.trace_id,
                            # Pass input_context directly so the plugin
                            # (+ rubric_judge downstream) can read the
                            # agent's system prompt as ground truth for
                            # scope/policy scoring. Without this, the
                            # judge has to guess what "in scope" means.
                            input_context=getattr(tc_eval, "input_context", None) or {},
                        )
                    except Exception as exc:
                        logger.warning(
                            f"plugin {evaluator.name} crashed evaluating {tc_eval.id}: {exc}; falling back to LLM judge",
                            extra={"operation": "plugin_eval_crash", "trace_id": input_data.trace_id},
                        )
                        return  # parallel worker — return, not continue
                    if verdict.fallback_reason:
                        return
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

                    # ── Shared-state mutation: LOCK ──
                    # test_case_results lookup + TCR mutation +
                    # plugin_handled_ids.add must be atomic. Two parallel
                    # workers could otherwise race on the same test_case_id
                    # and double-promote. Fast (<1ms) — no performance
                    # concern from holding the lock here.
                    with eval_state_lock:
                        tcr_target = next(
                            (t for t in test_case_results if t.test_case_id == tc_eval.id), None,
                        )
                        if tcr_target is None:
                            return
                        _promote_verdict_to_tcr(
                            tcr_target, criteria_eval,
                            score=verdict.score,
                            passed=verdict.passed,
                            reasoning=verdict.reasoning,
                            tools_used_additions=[evaluator.name],
                            audio_paths_additions=audio_artifacts,
                            verdict_detail=detail if isinstance(detail, dict) else None,
                        )
                        plugin_handled_ids.add(tc_eval.id)

                # Dispatch every item to the parallel runner. Multi-turn
                # evals run at SESSION_PARALLELISM (2); single-turn at
                # PARALLELISM (3). Both inside this single candidate
                # thread — cross-candidate parallelism is the outer
                # ThreadPoolExecutor in run_implement_test_env_agent.
                _dispatch_eval_items_in_parallel(list(eval_items), _process_one)

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

                def _process_one(eval_item) -> None:
                    nonlocal eval_cost
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
                                    _it=tc_input_type,
                                    _logger=logger,
                                    _trace_id=input_data.trace_id,
                                    _cn=candidate.name):
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
                            # Session-cap retry: if the provider returns
                            # "max concurrent sessions" on this per-turn
                            # call, the wrapper waits for a slot and
                            # retries before returning. See
                            # _execute_test_with_session_retry for the
                            # full semantics. Transparent to the plugin's
                            # drive-loop — it sees a longer latency on
                            # the affected turn but no error.
                            return _execute_test_with_session_retry(
                                _sd, merged, _c, _t,
                                _logger, _trace_id, _cn,
                            )
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
                        # Agentic conversational context — threaded through
                        # to EvalContext → plugin.evaluate_output so
                        # persona/goal/rubric actually reach the plugin's
                        # agentic drive path. Without these, multi-turn
                        # tests silently fall back to scripted mode.
                        persona=getattr(tc_eval, "persona", None),
                        goal=getattr(tc_eval, "goal", None),
                        constraints=getattr(tc_eval, "constraints", None) or [],
                        rubric=getattr(tc_eval, "rubric", None) or [],
                        max_turns=getattr(tc_eval, "max_turns", 6),
                        evaluation_mode=getattr(tc_eval, "evaluation_mode", "auto"),
                        input_context=getattr(tc_eval, "input_context", None) or {},
                    )
                    # ── Shared-state mutation: LOCK ──
                    # eval_cost, plugin_handled_ids, and TCR mutation must
                    # be atomic. Two parallel workers could otherwise
                    # double-count eval_cost or double-promote a TCR.
                    # The expensive work (evaluate_with_tool_runner) ran
                    # OUTSIDE this lock; we're only holding it for the
                    # fast mutations (<1ms).
                    with eval_state_lock:
                        # C4 fix: accumulate Claude cost from each tool_
                        # runner invocation into evaluation_cost_usd.
                        eval_cost += float(verdict.cost_usd or 0.0)
                        if verdict.fallback_reason:
                            logger.info(
                                "tool_runner fallback for %s: %s",
                                tc_eval.id, verdict.fallback_reason,
                                extra={"operation": "tool_runner_fallback",
                                       "trace_id": input_data.trace_id},
                            )
                            return  # parallel worker — return, not continue
                        tcr_target = next(
                            (t for t in test_case_results if t.test_case_id == tc_eval.id), None,
                        )
                        if tcr_target is None:
                            return
                        tools_used = ["tool_runner"] + list(verdict.tools_invoked)
                        _promote_verdict_to_tcr(
                            tcr_target, criteria_eval,
                            score=verdict.score,
                            passed=verdict.passed,
                            reasoning=verdict.reasoning,
                            tools_used_additions=tools_used,
                            audio_paths_additions=verdict.artifacts,
                            # Tool-runner verdicts carry their plugin's detail
                            # dict under `.verdict_detail` (see
                            # plugin_tool_runner.ToolRunnerVerdict fix).
                            verdict_detail=getattr(verdict, "verdict_detail", None),
                            # Pass the tool_runner Claude cost so the per-test
                            # cost_usd reflects ALL Anthropic spend on this
                            # test, not just plugin-side simulator+judge.
                            # Pre-NEW-AM this cost was tracked only at the
                            # candidate-aggregate level (eval_cost), so each
                            # test's cost_usd showed None even when ~$0.10
                            # was genuinely spent.
                            tool_runner_cost_usd=float(verdict.cost_usd or 0.0),
                        )
                        plugin_handled_ids.add(tc_eval.id)

                # Dispatch every item to the parallel runner. Multi-turn
                # evals (WebSocket voice conversations, chat loops) run at
                # SESSION_PARALLELISM (default 2); single-turn evals
                # (OCR, chatbot single-turn, classification) run at
                # PARALLELISM (default 3).
                _dispatch_eval_items_in_parallel(list(eval_items), _process_one)

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
                overall_score=metrics["overall_score"],
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
                    # Read from the same source as the persisted schema
                    # field — previously this was inlined as
                    # `sum(weighted_score)/len` here but NOT persisted,
                    # causing live/refreshed UI inconsistency. Now both
                    # SSE and persisted JSON read the same computed value.
                    "overall_score": metrics.get("overall_score", 0.0),
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
