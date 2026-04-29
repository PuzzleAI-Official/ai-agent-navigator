"""Agent 5 build loop — the orchestration spine.

Phase 5 destination — this module owns ``build_single_harness(ctx)``,
the autonomous tool-use loop that drives ONE candidate's harness
build. The legacy entry point ``_build_single_harness`` in
``implement_test_env.py`` becomes a thin shim that constructs
``BuildContext`` and delegates here.

Shape of the move:
  * Step 1 (this file at first commit): ``BuildContext`` +
    ``BuildLoopState`` dataclasses + helper unit tests. The dataclasses
    are unused until Step 3.
  * Step 2: ``_initialize_loop_state(ctx, setup)`` extraction.
  * Step 3: ``build_single_harness(ctx)`` lands here, replacing the
    legacy function body.

AD-007: this module is pure backbone. Markdown contracts don't gate
anything here — retry math, state mutation, dispatch flow are all
deterministic Python.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import anthropic

from puzzleeval.agents.agent5.sandbox import candidate_slug
from puzzleeval.logging_setup import log_llm_call
from puzzleeval.schemas import (
    Agent5Input,
    FailedHarness,
    ScreenedCandidate,
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


# ---------------------------------------------------------------------------
# Phase 1 → Phase 2 forcing directive (AD-007 deterministic enforcement)
# ---------------------------------------------------------------------------
#
# Real-run trace 749b09b1 (2026-04-28) caught a 33-turn narration stall:
# OpenAI Realtime build had Sonnet write exit narration ("Now I have all
# the information needed. Let me patch api_spec.txt...") right before the
# Sonnet→Opus boundary. Opus inherited that narrative arc + the system
# prompt's "Phase 2 = Opus" anthropomorphization, and emitted 33 turns of
# variants on "Phase 1 complete — handing off to Phase 2 (Opus)" without
# realizing IT IS Opus. cache_read=88,763 stayed identical across T5-T37
# (messages list never changed), and the loop just retried.
#
# The narration trigger is candidate-specific and nondeterministic:
# ElevenLabs Sonnet T2 emitted ZERO exit text in the same run, and Opus
# T4 went straight to write_file × 4. We can't rely on prompt language
# alone to keep Opus on track when the inherited context biases narrative.
#
# Fix: deterministically inject this directive as a user message
# immediately after the api_spec_written transition fires. Opus's first
# response after the transition will see this directive as the most
# recent user input — overriding any inherited narrative bias from
# Sonnet's exit text. AD-007: the contract is enforced in Python, not
# in the system prompt where adherence is best-effort.
#
# Cost ceiling per occurrence: ~$0.05 (one extra user message in the
# next API call). Cost saved per occurrence: ~$1.65 (the observed
# narration-stall waste). Net win even if the directive only helps 1
# build in 30.
PHASE2_DIRECTIVE = (
    "api_spec.txt is now written. You are in Phase 2. Your IMMEDIATE next "
    "response MUST call write_file (in parallel) for: requirements.txt, "
    "harness.py, smoke_test.py, live_test.py. Do NOT narrate the "
    "transition. Do NOT acknowledge any handoff. Just call the tools."
)


# ---------------------------------------------------------------------------
# BuildContext — frozen inputs the legacy entry point provides.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildContext:
    """Frozen inputs for ONE candidate's build.

    Constructed once at the entry point (legacy
    ``_build_single_harness(client, candidate, input_data, sandbox_dir,
    logger, progress_callback=None)``); never mutated.

    Derived fields (``trace_id``, ``candidate_label``) are populated in
    ``__post_init__`` so callers don't recompute them from the same
    inputs across helpers.

    Note: post-setup outputs (``credentials``, ``staged_test_cases``,
    ``pre_rendered_spec``, ``provider_slug``) live on
    ``BuildSetupSuccess`` (already extracted in Phase 4.2). They are
    NOT on BuildContext because they aren't available at construction
    time — the caller must run setup first to obtain them. Helpers
    requiring both shapes take ``ctx: BuildContext`` AND
    ``setup: BuildSetupSuccess`` separately.
    """

    # The 6 legacy positional/keyword inputs
    client: Any  # anthropic.Anthropic
    candidate: Any  # ScreenedCandidate
    input_data: Any  # Agent5Input
    sandbox_dir: Path
    logger: Any  # logging.Logger
    progress_callback: Callable[[str, dict], None] | None = None

    # Derived in __post_init__ — read-only after construction
    trace_id: str = field(init=False)
    candidate_label: str = field(init=False)

    def __post_init__(self) -> None:
        # Frozen dataclass — must use object.__setattr__ to populate
        # derived fields. This is the documented escape hatch for
        # __post_init__ on frozen dataclasses.
        object.__setattr__(self, "trace_id", self.input_data.trace_id)
        object.__setattr__(
            self, "candidate_label", candidate_slug(self.candidate.name)
        )


# ---------------------------------------------------------------------------
# BuildLoopState — mutable per-build state. Replaces ~22 loop locals.
# ---------------------------------------------------------------------------


@dataclass
class BuildLoopState:
    """Mutable per-build state — the ~22 locals the loop body reads/writes.

    Each field's default matches the inline ``var = default`` line it
    replaces in ``_build_single_harness``. The loop body uses
    ``state.X = ...`` instead of bare ``X = ...``. Behavior is identical;
    only the access syntax changes.

    NOT included (intentional):
      * Per-iteration short-lived locals (``call_start``,
        ``call_latency_ms``, ``last_errors``, ``pattern_hint``,
        ``current_max_tokens``, etc.). They live + die within one loop
        body iteration; promoting them to BuildLoopState would pollute
        the dataclass.
      * Setup-derived constants (``credentials``, ``staged_test_cases``,
        ``trace_id``, ``candidate_label``, etc.). Those live on
        BuildContext / BuildSetupSuccess.
    """

    # ── Turn counters + accumulators ──────────────────────────────
    turn: int = 0
    accumulated_cost: float = 0.0
    total_web_searches: int = 0
    candidate_web_fetch_blocks: int = 0  # Cumulative recoverable web_fetch blocks across turns

    # ── The conversation ──────────────────────────────────────────
    messages: list = field(default_factory=list)
    conversation_log: list = field(default_factory=list)  # Human-readable per-turn log

    # ── Phase 1 → Phase 2 transition flag ──────────────────────────
    api_spec_written: bool = False  # Flips True when api_spec.txt is written/patched

    # ── Per-turn ephemeral (cleared at top of each iteration) ──────
    last_text: str = ""

    # ── Verification gate state ──────────────────────────────────
    verification_attempts: int = 0
    verification_passed: bool = False

    # ── Smoke test pass tracking (across ALL turns) ──────────────
    smoke_ever_passed: bool = False
    smoke_passed_at_turn: int = -1  # -1 = never passed yet

    # ── Dead-end detection / reassessment ─────────────────────────
    consecutive_errors: int = 0  # Resets on successful turn OR after reassessment
    total_reassessments: int = 0  # Cumulative — never resets (drives escalation tiers)
    error_history: list[tuple[int, str]] = field(default_factory=list)  # (turn, category)

    # ── Progress / nudge tracking ────────────────────────────────
    progress_ring: list[bool] = field(default_factory=list)  # Last N turns' "made progress" signal
    diminishing_nudge_sent: bool = False
    turn_budget_nudge_sent: bool = False
    approaches_tried: list[str] = field(default_factory=list)
    patch_fragmentation_nudged_files: set[str] = field(default_factory=set)

    # ── Tool state ──────────────────────────────────────────────
    build_read_state: dict[str, float] = field(default_factory=dict)  # filename → last_read_mtime
    saved_doc_files: list[str] = field(default_factory=list)  # fetched_docs_N.txt index

    # ── Wall-clock timeout tracking ──────────────────────────────
    build_start_time: float = 0.0  # time.monotonic() at build start; 0.0 means uninitialized


# ---------------------------------------------------------------------------
# Phase 5 Step 2 helpers — _initialize_loop_state + _should_inject_turn_budget_nudge
# ---------------------------------------------------------------------------


def _initialize_loop_state(ctx: BuildContext, setup: Any) -> BuildLoopState:
    """Build the initial BuildLoopState from ctx + setup output.

    Replaces the inline state-init block in
    ``_build_single_harness`` that ran AFTER ``_setup_sandbox_and_credentials``
    returned. Each assignment here matches a corresponding inline
    ``var = default`` line — behavior is identical.

    Args:
        ctx: Frozen BuildContext (the 6 entry-point inputs + derived
            trace_id / candidate_label).
        setup: BuildSetupSuccess-shaped object with ``credentials``,
            ``staged_test_cases``, ``pre_rendered_spec``, etc. (Imported
            lazily to avoid an import cycle — implement_test_env owns
            BuildSetupSuccess today; this helper just reads its
            attributes.)

    Returns:
        BuildLoopState seeded with:
          * The first user message (the build's initial prompt) in
            ``messages``.
          * ``saved_doc_files`` populated from prefetched docs already
            on disk (Agent 4 → Agent 5 handoff).
          * ``build_start_time`` set to ``time.monotonic()``.
          * Pre-render boundary log fired (mirrors inline behavior).
          * Every other field at its dataclass default.

    Pure-ish: imports + filesystem read (counting prefetched docs) +
    one logger call. No mutation of ctx or setup.
    """
    import time

    from puzzleeval.web_doc_cache import count_existing_fetched_docs

    # Lazy import — `_build_initial_message` lives in implement_test_env
    # today (Phase 4 has not yet extracted it). Importing at call time
    # avoids a build_loop → implement_test_env import cycle.
    from puzzleeval.agents.implement_test_env import _build_initial_message

    state = BuildLoopState()

    # Seed messages from the initial-message builder.
    initial_message = _build_initial_message(
        ctx.candidate,
        ctx.input_data,
        credentials=setup.credentials,
        staged_test_cases=setup.staged_test_cases,
        sandbox_dir=ctx.sandbox_dir,
        api_spec_pre_rendered=bool(setup.pre_rendered_spec),
    )
    state.messages.append({"role": "user", "content": initial_message})

    # Seed prefetched-docs index from sandbox dir (Agent 4 → 5 handoff).
    prefetched_count = count_existing_fetched_docs(ctx.sandbox_dir)
    if prefetched_count:
        state.saved_doc_files = [
            f"fetched_docs_{i}.txt" for i in range(prefetched_count)
        ]
        ctx.logger.info(
            f"Seeded Agent 5 with {prefetched_count} prefetched docs from Agent 4",
            extra={
                "operation": "agent5_doc_handoff_seed",
                "trace_id": ctx.trace_id,
                "candidate_name": ctx.candidate.name,
                "prefetched_count": prefetched_count,
                "prefetched_files": state.saved_doc_files,
            },
        )

    # Pre-render boundary log (purely informational — no behavior gate).
    if setup.pre_rendered_spec:
        ctx.logger.info(
            f"Pre-rendered spec landed for {ctx.candidate.name}; "
            "Sonnet will targeted-augment via patch_file (will trigger Opus switch)",
            extra={
                "operation": "checklist_prerender_for_augment",
                "trace_id": ctx.trace_id,
                "candidate_name": ctx.candidate.name,
            },
        )

    # Wall-clock start.
    state.build_start_time = time.monotonic()

    return state


def _should_inject_turn_budget_nudge(
    state: BuildLoopState, max_turns: int,
) -> bool:
    """Predicate for the wrap-up nudge — fires when ≤3 turns remain
    AND smoke hasn't passed AND the conversation has started AND the
    nudge hasn't fired yet.

    Pattern stolen from Claude Code's ``getBudgetContinuationMessage``
    (query/tokenBudget.ts). Without this the builder can get stuck
    polishing on turn 23 and run out without producing a verdict.

    Pure function. Caller is responsible for actually appending the
    nudge message AND setting ``state.turn_budget_nudge_sent = True``
    after injection.
    """
    turns_remaining = max_turns - state.turn
    return (
        turns_remaining <= 3
        and not state.turn_budget_nudge_sent
        and not state.smoke_ever_passed
        and bool(state.messages)
    )


# ---------------------------------------------------------------------------
# Phase 5 Step 3 — build_single_harness (the orchestration spine).
# ---------------------------------------------------------------------------


def build_single_harness(
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

    # Phase 5 Step 3 — lazy imports of helpers that still live in
    # ``puzzleeval.agents.implement_test_env``. Lazy because:
    # implement_test_env imports build_loop at module bottom for the
    # legacy shim; importing back at MODULE TOP here would cycle. By
    # the time build_single_harness is CALLED, implement_test_env has
    # finished loading, so these imports resolve cleanly.
    from puzzleeval.config import (
        AGENT5_BUILDER_MODEL,
        AGENT5_MAX_OUTPUT_TOKENS,
        AGENT5_MAX_TURNS,
        AGENT5_MAX_VERIFICATION_RETRIES,
        ENABLE_FETCH_FALLBACK,
        RESEARCH_MODEL,
    )
    from puzzleeval.agents.implement_test_env import (
        ALL_TOOLS,
        BUILDER_SYSTEM_PROMPT,
        CUSTOM_TOOL_NAMES,
        _apply_message_cache_breakpoint,
        _ask_research_template_adherence,
        _build_initial_message,
        _build_tools_with_programmatic,
        _calculate_call_cost,
        _categorize_failure,
        _compact_research_tool_results,
        _detect_patch_fragmentation_pattern,
        _dispatch_tool,
        _extract_and_save_web_content,
        _extract_text_from_response,
        _finalize_build_result,
        _persist_large_output,
        _read_harness_code,
        _render_builder_prompt,
        _run_targeted_research,
        _run_verification_checks,
        _setup_sandbox_and_credentials,
        _tool_patch_file,
        _with_builder_appendix,
        _with_shared_preamble,
    )

    # Phase 4.2: setup logic extracted to _setup_sandbox_and_credentials.
    # Returns BuildSetupSuccess on success, or TestHarness/FailedHarness
    # for early-return paths (OpenAPI fastpath success, venv failure).
    _setup = _setup_sandbox_and_credentials(
        candidate, input_data, sandbox_dir, logger, progress_callback,
    )
    if isinstance(_setup, (TestHarness, FailedHarness)):
        return _setup
    candidate_label = _setup.candidate_label
    trace_id = _setup.trace_id
    provider_slug = _setup.provider_slug
    staged_test_cases = _setup.staged_test_cases
    credentials = _setup.credentials
    pre_rendered_spec = _setup.pre_rendered_spec

    # ★ CORE: Initialize the conversation with seed knowledge + credential hints
    # No separate research sub-agent — the builder does its own research in Phase 1.
    # This keeps research and coding in ONE context, so the actual fetched API docs
    # are in memory when the code is written. No knowledge-handoff loss.
    initial_message = _build_initial_message(
        candidate, input_data, credentials=credentials,
        staged_test_cases=staged_test_cases, sandbox_dir=sandbox_dir,
        api_spec_pre_rendered=bool(pre_rendered_spec),
    )
    messages = [{"role": "user", "content": initial_message}]
    # Exposed to the per-turn builder loop so `_render_builder_prompt` can
    # inject the right modality-specific contract (voice harness shape,
    # etc.) based on THIS candidate's test-case modalities.
    test_cases_for_builder = staged_test_cases

    accumulated_cost = 0.0
    total_web_searches = 0
    turn = 0
    # ── Phase 1 → Phase 2 transition tracker ──
    # Stays False at build start. The transition fires when the builder
    # writes OR PATCHES api_spec.txt (NEW-AM v7 — added patch_file
    # trigger).
    #
    # Architecture (NEW-AM v3 + v7 combined):
    #   * Pre-render writes api_spec.txt at sandbox setup (NEW-AM v3)
    #   * Sonnet does targeted augment via patch_file('api_spec.txt')
    #     during Phase 1
    #   * patch_file('api_spec.txt') flips this flag (NEW-AM v7)
    #   * Next turn switches to Opus, which writes harness.py + tests
    #
    # Pre-NEW-AM v7 BUG (real-run trace a4860e94, 2026-04-25):
    #   * Pre-render created api_spec.txt
    #   * Sonnet PATCHED it (patch_file → no trigger)
    #   * Sonnet then wrote harness.py (write_file → trigger fires
    #     too late; harness.py is already Sonnet's code)
    #   * Sonnet's harness.py had subtly-wrong session.update shape
    #     and trivial live_test (audio_url=None) → 0/5 real tests
    #
    # NEW-AM v7 fix: trigger ALSO fires on patch_file('api_spec.txt'),
    # so Sonnet's augment patch immediately switches to Opus before
    # Sonnet can write any code files. Plus Phase 1 prompt now hard-
    # forbids Sonnet from writing harness.py / requirements.txt /
    # smoke_test.py / live_test.py.
    api_spec_written = False
    if pre_rendered_spec:
        logger.info(
            f"Pre-rendered spec landed for {candidate.name}; Sonnet will targeted-augment via patch_file (will trigger Opus switch)",
            extra={
                "operation": "checklist_prerender_for_augment",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
            },
        )
    last_text = ""
    verification_attempts = 0
    verification_passed = False
    conversation_log = []  # Human-readable log of every turn
    # Seed saved_doc_files from the sandbox dir — Agent 4's verification
    # turn persists `fetched_docs_*.txt` here before we ever start, so the
    # builder's STEP 1 can `read_file` instead of `web_fetch` the same URL
    # a second time. If Agent 4 found nothing (or the feature flag was
    # off), this is the empty list and Agent 5 behaves exactly as before.
    # See puzzleeval/web_doc_cache.py for the handoff protocol.
    from puzzleeval.web_doc_cache import count_existing_fetched_docs
    _prefetched_count = count_existing_fetched_docs(sandbox_dir)
    saved_doc_files = [
        f"fetched_docs_{i}.txt" for i in range(_prefetched_count)
    ]
    if saved_doc_files:
        logger.info(
            f"Seeded Agent 5 with {_prefetched_count} prefetched docs from Agent 4",
            extra={
                "operation": "agent5_doc_handoff_seed",
                "trace_id": trace_id,
                "candidate_name": candidate.name,
                "prefetched_count": _prefetched_count,
                "prefetched_files": saved_doc_files,
            },
        )
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
    # Gate C — patch-fragmentation nudge history. Each filename is
    # added AFTER the nudge fires for that file, so a single file can
    # be nudged AT MOST once per build. Re-fires on DIFFERENT files
    # (e.g., nibbling on harness.py then nibbling on live_test.py
    # triggers two nudges, which is correct — two distinct lessons).
    patch_fragmentation_nudged_files: set[str] = set()
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
        AGENT5_PATCH_FRAGMENT_NUDGE_ENABLED,
        AGENT5_PATCH_FRAGMENT_TOKEN_CEILING,
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

        # ★ CORE: Call Claude with all tools + server-side context management.
        #
        # Phase 4 Path B Step 1: the API call boundary (retry loop + PTL
        # recovery + rate-limit backoff + APIConnectionError handling) is
        # extracted to ``puzzleeval.agents.agent5.api_call.make_builder_api_call``.
        # This call site provides the loop-local state in a typed
        # ``BuilderAPICallContext`` and pattern-matches on the ``APICallOutcome``
        # tagged union. The orchestration spine (turn counter, accumulated
        # cost, response handling) stays here in the loop.
        #
        # Phase 1 → Phase 2 transition: ``current_model`` is recomputed each
        # iteration. When ``api_spec_written`` flips True after a write/patch
        # of api_spec.txt (see dispatch loop below), the next iteration uses
        # Opus instead of Sonnet — the model swap is observable via the API
        # call kwargs (pinned by ``TestPhaseTransitionTriggers``).
        from puzzleeval.agents.agent5.api_call import (
            APICallFailure,
            BuilderAPICallContext,
            make_builder_api_call,
        )
        from puzzleeval.config import output_config_for_request

        call_start = time.time()
        current_model = AGENT5_BUILDER_MODEL if api_spec_written else RESEARCH_MODEL

        api_outcome = make_builder_api_call(BuilderAPICallContext(
            client=client,
            current_model=current_model,
            current_max_tokens=AGENT5_MAX_OUTPUT_TOKENS,
            messages=messages,
            system_text=_with_shared_preamble(
                _with_builder_appendix(
                    _render_builder_prompt(
                        BUILDER_SYSTEM_PROMPT,
                        test_cases=test_cases_for_builder,
                    )
                )
            ),
            tools=_build_tools_with_programmatic(ALL_TOOLS),
            output_config=output_config_for_request(),
            max_retries=3,
            candidate=candidate,
            candidate_label=candidate_label,
            sandbox_dir=sandbox_dir,
            trace_id=trace_id,
            turn=turn,
            accumulated_cost=accumulated_cost,
            candidate_web_fetch_blocks=candidate_web_fetch_blocks,
            read_harness_code=_read_harness_code,
            apply_message_cache_breakpoint=_apply_message_cache_breakpoint,
            logger=logger,
        ))
        if isinstance(api_outcome, APICallFailure):
            return api_outcome.failed_harness
        response = api_outcome.response

        # [logging] Log this call's metrics
        log_llm_call(
            logger=logger, response=response, model=current_model,
            trace_id=trace_id, start_time=call_start,
            operation=f"harness_build_{candidate_label}_turn{turn}",
        )

        # [observability] Capture per-turn latency so conversation_log
        # has wall-clock data alongside token/cost data. log_llm_call
        # captures latency to stderr structured logs but doesn't write
        # to the persisted turn dict; without this we have no way to
        # correlate "this turn cost $X" with "this turn took Ys" when
        # diagnosing perf issues post-hoc.
        call_latency_ms = round((time.time() - call_start) * 1000, 2)

        # [cost tracking] All costs (executor + advisor + cache + web search)
        # calculated from the iterations array per Anthropic API docs
        call_cost = _calculate_call_cost(response, current_model)
        accumulated_cost += call_cost

        # [tracking] Web search count for logging
        server_tool_use = getattr(response.usage, "server_tool_use", None)
        if server_tool_use:
            total_web_searches += getattr(server_tool_use, "web_search_requests", 0) or 0

        # ── Log this turn for conversation history ──
        # Phase 4 Path B: turn_log construction extracted to
        # agent5.turn_blocks.build_initial_turn_log. The dict is
        # subsequently mutated by the response.content iteration below
        # (text accumulation + tool_calls / tool_results appends).
        from puzzleeval.agents.agent5.turn_blocks import build_initial_turn_log
        turn_log = build_initial_turn_log(
            response,
            turn=turn,
            current_model=current_model,
            call_cost=call_cost,
            call_latency_ms=call_latency_ms,
        )

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
                    # [observability] Capture server-tool inputs (web_fetch URL,
                    # web_search query) instead of dropping them — the URL or
                    # query is exactly the diagnostic info that lets the
                    # operator see WHAT Claude is researching this turn.
                    # Pre-NEW-AM the input was replaced with a generic
                    # placeholder string and the URL/query was thrown away,
                    # making mid-build debugging impossible.
                    raw_input = getattr(block, "input", None) or {}
                    if isinstance(raw_input, dict):
                        tool_entry["input"] = {
                            k: (str(v)[:300] if isinstance(v, str) else v)
                            for k, v in raw_input.items()
                        }
                    else:
                        tool_entry["input"] = "(server tool — non-dict input)"
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
            # [observability] Capture web_fetch / web_search server-tool RESULTS
            # so the operator can see WHAT Claude actually found this turn.
            # Pre-NEW-AM these block types passed through silently — we knew
            # Claude called web_fetch but had no idea what page came back
            # (which is critical when debugging "why did Claude pick the
            # wrong endpoint?"). Truncate aggressively (500 chars per blob)
            # to keep the conversation_log readable; the full fetched docs
            # are persisted separately as fetched_docs_*.txt.
            elif block.type == "web_fetch_tool_result":
                fetched_url = ""
                fetched_preview = ""
                fetched_chars = 0
                content = getattr(block, "content", None)
                if content is not None:
                    inner = getattr(content, "content", None)
                    if inner is not None:
                        fetched_url = getattr(inner, "url", "") or ""
                        source = getattr(inner, "source", None)
                        if source is not None:
                            data = getattr(source, "data", "") or ""
                            fetched_chars = len(data)
                            fetched_preview = data[:500]
                turn_log["tool_results"].append({
                    "tool": "web_fetch",
                    "url": fetched_url,
                    "chars_returned": fetched_chars,
                    "preview": fetched_preview,
                })
            elif block.type == "web_search_tool_result":
                content = getattr(block, "content", None)
                results_summary = []
                result_count = 0
                if isinstance(content, list):
                    result_count = len(content)
                    for r in content[:5]:  # capture top 5 hits
                        title = getattr(r, "title", "") or (r.get("title", "") if isinstance(r, dict) else "")
                        url = getattr(r, "url", "") or (r.get("url", "") if isinstance(r, dict) else "")
                        results_summary.append({"title": title[:120], "url": url[:200]})
                turn_log["tool_results"].append({
                    "tool": "web_search",
                    "result_count": result_count,
                    "top_results": results_summary,
                })
        conversation_log.append(turn_log)

        # [observability] Incremental conversation_log.json save — written
        # after EVERY turn instead of only at end-of-build. Lets the
        # operator `cat conversation_log.json` mid-build to see exactly
        # what Claude did each turn (text emitted, tools called, URLs
        # fetched, search queries). Previously the file only existed
        # AFTER the build finished — useless for debugging a stuck build.
        # Best-effort: failures here MUST NOT break the build loop.
        try:
            (sandbox_dir / "conversation_log.json").write_text(
                json.dumps(conversation_log, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass

        # Per-turn progress callback — let the frontend show live build progress.
        #
        # Phase 4 Path B extraction: rich SSE payload construction
        # (phase computation, tool_calls_detail summarization, text_preview)
        # moved to ``puzzleeval.agents.agent5.turn_blocks.emit_build_turn_progress``.
        # This call site provides the loop-local state (turn, model,
        # accumulators) that the helper composes into the event.
        from puzzleeval.agents.agent5.turn_blocks import emit_build_turn_progress
        emit_build_turn_progress(
            progress_callback,
            candidate_name=candidate.name,
            turn=turn,
            max_turns=AGENT5_MAX_TURNS,
            api_spec_written=api_spec_written,
            smoke_ever_passed=smoke_ever_passed,
            current_model=current_model,
            call_cost=call_cost,
            accumulated_cost=accumulated_cost,
            call_latency_ms=call_latency_ms,
            # cache_read/cache_create live on turn_log under the
            # *_tokens names (build_initial_turn_log populated them).
            cache_read=turn_log.get("cache_read_tokens", 0),
            cache_create=turn_log.get("cache_create_tokens", 0),
            response=response,
            turn_log=turn_log,
        )

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
        # Real-run signal (traces voice_debug_4 + voice_debug_5): the API
        # occasionally returns content with a `server_tool_use` block lacking
        # its matching `_tool_result` (max_tokens truncation, server-side
        # races). Appending to history would 400 the next API call. Strip
        # orphans BEFORE appending, on EVERY turn.
        #
        # Phase 4.1: detection + cleanup logic moved to
        # ``puzzleeval.agents.agent5.turn_blocks.strip_orphan_server_tool_uses``.
        # Logging + control-flow fallback stay here in the loop.
        from puzzleeval.agents.agent5.turn_blocks import strip_orphan_server_tool_uses
        _cleaned, _orphan_ids, _mutation_ok = strip_orphan_server_tool_uses(response)
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
            if not _mutation_ok:
                # Could not mutate response.content (frozen response object).
                # Treat this turn as a pause: append the cleaned content
                # locally + nudge, increment turn, continue loop.
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

        # Also check agent's text for smoke test pass / completion signals.
        # Phase 4 Path B Step 2: detection extracted to dispatch_helpers
        # (pure predicates, see agent5/dispatch_helpers.py).
        from puzzleeval.agents.agent5.dispatch_helpers import (
            detect_harness_signal,
            detect_smoke_pass,
        )
        if detect_smoke_pass(last_text) and not smoke_ever_passed:
            smoke_ever_passed = True
            smoke_passed_at_turn = turn

        # Check for HARNESS_COMPLETE signal in text
        if smoke_ever_passed and detect_harness_signal(last_text) == "complete":
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
        # Phase 4 Path B Step 2: signal detection via dispatch_helpers.
        _signal = detect_harness_signal(last_text)
        if response.stop_reason == "end_turn":
            if _signal == "complete":
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

            elif _signal == "failed":
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
                # Append the assistant response so messages[] actually
                # changes between iterations. Without this, identical
                # prompts cycle and the model is locked into whatever
                # pattern its first response landed on (real-run trace
                # 749b09b1, 2026-04-28: cache_read=88,763 stayed
                # IDENTICAL across 33 turns of Opus narrating "handing
                # off to Phase 2 (Opus)" — $1.65 wasted on
                # cache-replay). Appending the response means at
                # minimum the model now sees its own prior text in
                # history on the next iteration.
                # Pinned by tests/test_build_loop_behavior.py::TestEndTurnNoSignalAppendsAssistant.
                messages.append({"role": "assistant", "content": response.content})
                turn += 1
                continue

        # ── Handle tool_use: dispatch custom tools ──
        if response.stop_reason == "tool_use":
            tool_results = []
            # Capture pre-transition state so we can detect the EXACT turn
            # the Phase 1 → Phase 2 boundary fires and inject the
            # PHASE2_DIRECTIVE forcing message in that same turn's
            # messages.append below. See PHASE2_DIRECTIVE comment above
            # for the real-run trace + diagnosis.
            api_spec_was_written = api_spec_written
            for block in response.content:
                if block.type == "tool_use" and block.name in CUSTOM_TOOL_NAMES:
                    # ── ask_research: spawn targeted research sub-agent ──
                    # Enriches the question with actual context (harness code,
                    # last error) so the research agent can give precise answers
                    # instead of generic API overviews.
                    if block.name == "ask_research":
                        question = block.input.get("question", "")
                        # ── PHASE-1 GATE ──────────────────────────────────────
                        # AD-007 safety contract enforced deterministically:
                        # ask_research is for Phase 2+ debugging — it fills
                        # GAPS in an existing api_spec.txt. Calling it before
                        # api_spec.txt exists defeats the whole context-
                        # inheritance design: the sub-agent's enrichment path
                        # has nothing to enrich with (no spec excerpt, no
                        # harness code, no prior errors), so it does generic
                        # research that the builder should have done itself
                        # via web_search/web_fetch.
                        #
                        # Real-run evidence (trace f9de380b): ElevenLabs build
                        # called ask_research TWICE before api_spec.txt was
                        # written — both calls returned useful-looking text
                        # but the builder then had no structured spec to
                        # anchor against, so subsequent turns re-asked
                        # questions the initial web research should have
                        # answered.
                        #
                        # Gate: if api_spec.txt doesn't exist yet, refuse
                        # the call with a clear message pointing to the
                        # right tools. Deterministic code, not prompt rule.
                        spec_path = sandbox_dir / "api_spec.txt"
                        phase1_block = not spec_path.exists()

                        if phase1_block:
                            result_text = (
                                "ask_research REFUSED: you haven't written "
                                "api_spec.txt yet. ask_research is a tool for "
                                "filling GAPS in an existing spec during "
                                "Phase 2+ debugging — it's NOT a shortcut for "
                                "initial discovery.\n\n"
                                "WHY THIS MATTERS: the sub-agent inherits YOUR "
                                "context (api_spec excerpt, harness code, "
                                "recent errors) to give targeted answers. "
                                "With no api_spec.txt on disk, the sub-agent "
                                "has nothing to inherit — it falls back to "
                                "generic research which YOU should be doing "
                                "yourself in Phase 1 so you develop your own "
                                "understanding of the API.\n\n"
                                "WHAT TO DO INSTEAD — Phase 1 research pattern:\n"
                                "  1. web_search: '<provider> API documentation "
                                "<your specific scope>'\n"
                                "  2. web_fetch: the top result's docs URL\n"
                                "  3. Navigate + fetch related pages (auth, "
                                "endpoints, errors, rate limits)\n"
                                "  4. Synthesize findings into api_spec.txt "
                                "with ENDPOINTS / AUTH / REQUEST_FORMAT / "
                                "RESPONSE_FORMAT / ERRORS / SAMPLE_CODE\n"
                                "  5. ONLY AFTER api_spec.txt exists, if you "
                                "hit a debug question the spec can't answer, "
                                "THEN call ask_research with the specific gap.\n\n"
                                f"Your question was: '{question[:300]}'. "
                                "Convert it into direct web_search/web_fetch "
                                "calls for now. Retry ask_research later when "
                                "you have api_spec.txt + a concrete error/gap."
                            )
                            logger.warning(
                                f"ask_research blocked for {candidate.name} at "
                                f"turn {turn} — api_spec.txt doesn't exist yet "
                                f"(question: {question[:100]!r})",
                                extra={
                                    "operation": "ask_research_phase1_gate",
                                    "trace_id": trace_id,
                                    "candidate_name": candidate.name,
                                    "turn": turn,
                                },
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text[:8000],
                            })
                            if conversation_log:
                                conversation_log[-1]["tool_results"].append({
                                    "tool": "ask_research",
                                    "result": "BLOCKED (Phase 1 — use web_search/web_fetch)",
                                })
                            continue

                        if not question:
                            result_text = "Error: empty question. Ask a specific question about the API."
                        else:
                            # ── SOFT TEMPLATE ADHERENCE LOG (Plan §Q3 hybrid) ──
                            # Inspect the question for the
                            # CANDIDATE/ENDPOINT/KNOWN/FIELD NEEDED/WHY
                            # template fields. We don't reject malformed
                            # calls — Opus follows the template reliably
                            # enough that hard validation would produce
                            # false-rejects on benign rephrasings. Logging
                            # only gives us telemetry to detect drift if
                            # adherence ever degrades in production.
                            adherence = _ask_research_template_adherence(question)
                            if not adherence["fully_adherent"]:
                                logger.info(
                                    f"ask_research template adherence: "
                                    f"{len(adherence['fields_present'])}/5 fields "
                                    f"for {candidate.name} at turn {turn} "
                                    f"(missing: {adherence['fields_missing']})",
                                    extra={
                                        "operation": "ask_research_template_adherence",
                                        "trace_id": trace_id,
                                        "candidate_name": candidate.name,
                                        "turn": turn,
                                        "fully_adherent": False,
                                        "adherence_ratio": adherence["adherence_ratio"],
                                        "fields_present": adherence["fields_present"],
                                        "fields_missing": adherence["fields_missing"],
                                    },
                                )
                            else:
                                logger.info(
                                    f"ask_research fully template-adherent for "
                                    f"{candidate.name} at turn {turn}",
                                    extra={
                                        "operation": "ask_research_template_adherence",
                                        "trace_id": trace_id,
                                        "candidate_name": candidate.name,
                                        "turn": turn,
                                        "fully_adherent": True,
                                        "adherence_ratio": 1.0,
                                    },
                                )

                            # Phase 4 Path B Step 3: enrichment extracted to
                            # dispatch_helpers.enrich_research_question.
                            # Caller still owns: prior_results_text aggregation
                            # (depends on tool_results local), harness code
                            # read, and the actual sub-agent invocation.
                            from puzzleeval.agents.agent5.dispatch_helpers import (
                                enrich_research_question,
                            )
                            prior_results_text = " ".join(
                                r.get("content", "") for r in tool_results
                                if isinstance(r.get("content"), str)
                            )
                            enriched_question = enrich_research_question(
                                question=question,
                                candidate_name=candidate.name,
                                candidate_provider=candidate.provider,
                                candidate_docs_url=candidate.verified_api_docs_url,
                                spec_path=spec_path,
                                prior_results_text=prior_results_text,
                                harness_code=_read_harness_code(sandbox_dir),
                            )
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
                    #
                    # Primary triggers (intended flow):
                    #   * write_file('api_spec.txt') — Sonnet writing
                    #     spec from scratch (no pre-render)
                    #   * patch_file('api_spec.txt') — Sonnet PATCHING
                    #     a pre-rendered spec (NEW-AM v3 augment path)
                    #
                    # Fallback triggers:
                    #   * write_file('harness.py' / 'requirements.txt') —
                    #     Sonnet skipped spec entirely. Mark transition
                    #     but PHASE 1 PROMPT FORBIDS this — Sonnet
                    #     should never write code files. If we hit
                    #     this branch, log a WARNING.
                    #
                    # NEW-AM v7 fix (real-run trace a4860e94, 2026-04-25):
                    # Pre-NEW-AM v7 the trigger only fired on write_file.
                    # With NEW-AM v3 pre-render, api_spec.txt exists at
                    # sandbox setup → Sonnet PATCHES it (patch_file) →
                    # trigger never fires from spec → Sonnet then writes
                    # harness.py (write_file) → trigger fires too late,
                    # harness.py is Sonnet's code (broken: wrong
                    # session.update shape, audio_url=None live test).
                    # Adding patch_file('api_spec.txt') as a trigger
                    # ensures the model switches to Opus right after
                    # Sonnet's augment patch, BEFORE Sonnet can write
                    # any code files.
                    # Phase 4 Path B Step 2: trigger detection extracted
                    # to dispatch_helpers.detect_phase_transition (pure
                    # predicate). The Phase 1 prompt-violation warning
                    # stays here because it owns the logging side effect.
                    from puzzleeval.agents.agent5.dispatch_helpers import (
                        detect_phase_transition,
                        is_phase_1_code_violation,
                    )
                    transition_triggered, trigger = detect_phase_transition(
                        block, api_spec_written,
                    )
                    if transition_triggered and is_phase_1_code_violation(trigger):
                        written_file = trigger.removeprefix("write_file:")
                        logger.warning(
                            f"Sonnet wrote {written_file} during Phase 1 for {candidate.name} — "
                            "this is a Phase 1 prompt violation. Sonnet should ONLY write/patch "
                            "api_spec.txt. The code file is now Sonnet-generated (low quality "
                            "for code) instead of Opus-generated.",
                            extra={
                                "operation": "sonnet_wrote_code_file",
                                "trace_id": trace_id,
                                "candidate_name": candidate.name,
                                "turn": turn,
                                "file": written_file,
                            },
                        )
                    if transition_triggered:
                        api_spec_written = True
                        logger.info(f"Phase 1 complete for {candidate.name}, switching to Opus (trigger: {trigger})", extra={
                            "operation": "phase_transition",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "turn": turn,
                            "trigger_file": trigger,
                        })
                        # ─────────────────────────────────────────
                        # Sonnet → Opus boundary compaction
                        # ─────────────────────────────────────────
                        # Eliminate the model-switch cache rebuild
                        # tax (~$0.66 per run, real-run measurement
                        # in trace 73a9d605). Compact Sonnet's raw
                        # web_fetch / web_search tool_result blobs
                        # to brief summaries; Opus reads them on
                        # disk via read_file when needed.
                        try:
                            compacted = _compact_research_tool_results(
                                messages,
                            )
                            if compacted:
                                logger.info(
                                    f"Compacted {compacted} research tool_result block(s) "
                                    f"at Sonnet→Opus boundary for {candidate.name}",
                                    extra={
                                        "operation": "research_compaction",
                                        "trace_id": trace_id,
                                        "candidate_name": candidate.name,
                                        "turn": turn,
                                        "blocks_compacted": compacted,
                                    },
                                )
                        except Exception as exc:  # noqa: BLE001
                            # Compaction failure is non-fatal — at
                            # worst we pay the original tax. Don't
                            # let a malformed message structure
                            # crash the build.
                            logger.warning(
                                f"Research compaction failed for {candidate.name}: "
                                f"{type(exc).__name__}: {str(exc)[:200]}",
                                extra={
                                    "operation": "research_compaction_failed",
                                    "trace_id": trace_id,
                                    "candidate_name": candidate.name,
                                },
                            )
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
                    # [observability] Log tool result with rich diagnostic
                    # context: exit_code (was dropped on the floor), full
                    # 2000-char tail (so tracebacks fit — 500 chars cut
                    # error messages mid-line), and per-tool metadata
                    # (write_file content size, patch_file diff hint).
                    # Without this, debugging "why did smoke_test.py fail?"
                    # required re-reading sandbox files manually because
                    # the truncated 500-char tool result hid the actual
                    # Python traceback.
                    if conversation_log:
                        result_entry = {
                            "tool": block.name,
                            "exit_code": exit_code,
                            "is_error": has_tool_error,
                            "result": result_text[-2000:],  # Tail, not head — errors at end
                            "result_length": len(result_text),
                        }
                        # Per-tool diagnostic enrichment
                        if block.name == "write_file":
                            content = block.input.get("content", "") if isinstance(block.input, dict) else ""
                            result_entry["wrote_path"] = block.input.get("filename", block.input.get("path", "")) if isinstance(block.input, dict) else ""
                            result_entry["wrote_chars"] = len(content)
                            result_entry["wrote_preview"] = content[:200]
                        elif block.name == "patch_file":
                            old_str = block.input.get("old_string", "") if isinstance(block.input, dict) else ""
                            new_str = block.input.get("new_string", "") if isinstance(block.input, dict) else ""
                            result_entry["patch_path"] = block.input.get("filename", block.input.get("path", "")) if isinstance(block.input, dict) else ""
                            result_entry["patch_diff_chars"] = len(new_str) - len(old_str)
                            result_entry["patch_old_preview"] = old_str[:120]
                            result_entry["patch_new_preview"] = new_str[:120]
                        elif block.name == "run_code":
                            cmd = block.input.get("command") or block.input.get("code") or "" if isinstance(block.input, dict) else ""
                            result_entry["command"] = str(cmd)[:300]
                        conversation_log[-1]["tool_results"].append(result_entry)
                    # (live_test injection removed — agent validates with real test data)

            # ── Detect smoke test passing in tool results ──
            # CRITICAL: Track across ALL turns (not just last_text).
            all_results_text_raw = " ".join(
                r.get("content", "") for r in tool_results if isinstance(r.get("content"), str)
            )
            if detect_smoke_pass(all_results_text_raw) and not smoke_ever_passed:
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
            if smoke_ever_passed and detect_harness_signal(all_results_text_raw) == "complete":
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
            # Phase 4 Path B Step 2: error detection + classification
            # extracted to dispatch_helpers (pure predicates over text).
            # ``all_results_text`` (lowered) preserved as a local — it's
            # used downstream by the reassessment-message builder.
            from puzzleeval.agents.agent5.dispatch_helpers import (
                classify_tool_result_error,
                detect_tool_result_error,
            )
            all_results_text = all_results_text_raw.lower()
            has_error = detect_tool_result_error(all_results_text_raw)

            if has_error:
                consecutive_errors += 1
                error_history.append((turn, classify_tool_result_error(all_results_text_raw)))
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

            # ── Phase 1 → Phase 2 forcing directive ──
            # AD-007 deterministic enforcement: when api_spec_written
            # JUST flipped True this turn, inject PHASE2_DIRECTIVE as
            # the most recent user message so Opus's first response
            # after the boundary cannot inherit Sonnet's narrative arc.
            # Pinned by tests/test_build_loop_behavior.py::TestPhase2Directive.
            if api_spec_written and not api_spec_was_written:
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": PHASE2_DIRECTIVE}],
                })

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

            # ── Gate C — patch-fragmentation runtime nudge ──
            # When the builder serial-nibbles at the same file with two
            # small patches back-to-back, offer (softly) that parallel
            # patches land a multi-edit fix in ONE turn instead of N.
            # NOT a hard stop; genuine iterate-and-verify cycles continue
            # unchanged. At-most-once per file per build (see state set
            # above) so it can't spam the loop on legitimate long debug
            # sessions.
            if AGENT5_PATCH_FRAGMENT_NUDGE_ENABLED and AGENT5_PATCH_FRAGMENT_TOKEN_CEILING > 0:
                fragmented_file = _detect_patch_fragmentation_pattern(
                    conversation_log,
                    patch_fragmentation_nudged_files,
                    output_token_ceiling=AGENT5_PATCH_FRAGMENT_TOKEN_CEILING,
                )
                if fragmented_file:
                    messages.append({
                        "role": "user",
                        "content": (
                            f"## Patch-fragmentation observation\n\n"
                            f"The last 2 patches to `{fragmented_file}` were "
                            f"small and sequential. If you already know 3+ "
                            f"more edits to `{fragmented_file}` for the same "
                            f"underlying bug, emit them as PARALLEL "
                            f"`patch_file` calls in the same turn — each "
                            f"round-trip costs a full conversation replay. "
                            f"If you're genuinely iterating (each patch "
                            f"depends on seeing the last one's effect, "
                            f"e.g., you need to re-run smoke_test between "
                            f"edits), keep going sequentially — this nudge "
                            f"is advisory, not a mandate. This is the only "
                            f"nudge you'll see for `{fragmented_file}` "
                            f"this build.\n"
                        ),
                    })
                    patch_fragmentation_nudged_files.add(fragmented_file)
                    logger.info(
                        f"Patch-fragmentation nudge sent for {candidate.name} "
                        f"on file={fragmented_file} at turn {turn}",
                        extra={
                            "operation": "patch_fragmentation_nudge",
                            "trace_id": trace_id,
                            "candidate_name": candidate.name,
                            "filename": fragmented_file,
                            "turn": turn,
                        },
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
            # Phase 4 Path B Step 2: reassessment trigger predicate +
            # message builder extracted to dispatch_helpers (pure
            # functions). Loop owns the COUNTER MUTATIONS (increment,
            # reset to 0) and the message append; the helpers compute
            # the bool gate + the message string.
            from puzzleeval.agents.agent5.dispatch_helpers import (
                build_reassessment_message,
                should_inject_reassessment,
            )
            if should_inject_reassessment(consecutive_errors, MAX_CONSECUTIVE_ERRORS):
                total_reassessments += 1
                last_errors = all_results_text[:500]
                # Record this reassessment as an "approach tried" so the
                # next tier's prompt can cite what NOT to repeat.
                _last_cat = error_history[-1][1] if error_history else "unknown"
                approaches_tried.append(
                    f"tier_{total_reassessments}_{_last_cat}"
                )
                reassessment = build_reassessment_message(
                    consecutive_errors=consecutive_errors,
                    last_errors=last_errors,
                    error_history=error_history,
                    total_reassessments=total_reassessments,
                    approaches_tried=approaches_tried,
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

    # Phase 4.4: post-loop result assembly extracted to _finalize_build_result.
    return _finalize_build_result(
        candidate,
        input_data,
        sandbox_dir,
        conversation_log=conversation_log,
        credentials=credentials,
        provider_slug=provider_slug,
        turn=turn,
        last_text=last_text,
        accumulated_cost=accumulated_cost,
        candidate_web_fetch_blocks=candidate_web_fetch_blocks,
        smoke_ever_passed=smoke_ever_passed,
        verification_attempts=verification_attempts,
        verification_passed=verification_passed,
    )


__all__ = [
    "BuildContext",
    "BuildLoopState",
    "_initialize_loop_state",
    "_should_inject_turn_budget_nudge",
    "build_single_harness",
]
