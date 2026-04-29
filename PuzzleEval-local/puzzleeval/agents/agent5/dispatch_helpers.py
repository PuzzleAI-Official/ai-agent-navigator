"""Pure dispatch-loop helpers for the Agent 5 build loop.

Phase 4 Path B Step 2 — extracts the PURE PREDICATES + PURE FORMATTERS
that the dispatch loop in ``_build_single_harness`` uses to make
control-flow decisions. The orchestration spine (the loop itself, the
state mutation) STAYS in ``_build_single_harness``; only the predicate
checks + message building move here.

This is the L7 split: don't extract one giant ``dispatch_loop`` function
with 15 parameters. Extract the small testable units that the loop
composes. Each helper here:
  * Has a single, well-bounded responsibility.
  * Is testable in isolation (no external state, no I/O for predicates).
  * Has a real production user (every helper replaces inline code in
    the dispatch loop).

Helpers:
  * Status signal detection (text-content predicates):
      - ``detect_smoke_pass(text) -> bool``
      - ``detect_harness_signal(text) -> Literal["complete","failed",None]``
  * Phase 1→2 transition detection (block-content predicate):
      - ``detect_phase_transition(block, api_spec_written) -> tuple[bool, str]``
  * Error classification (text-content predicate + classifier):
      - ``detect_tool_result_error(text) -> bool``
      - ``classify_tool_result_error(text) -> Literal[...]``
  * Reassessment trigger + builder:
      - ``should_inject_reassessment(consecutive_errors, max_consecutive) -> bool``
      - ``build_reassessment_message(...) -> str``

AD-007: All predicates are deterministic Python. Markdown contracts
don't gate behavior here.
"""

from __future__ import annotations

from typing import Any, Literal

# Status signals appear in BOTH text content (last_text) AND tool result
# content (all_results_text_raw). The dispatch loop checks both surfaces
# every turn — these helpers are the canonical predicates for both.
SMOKE_PASS_MARKER = "SMOKE TEST PASSED"
HARNESS_COMPLETE_MARKER = "HARNESS_COMPLETE"
HARNESS_FAILED_MARKER = "HARNESS_FAILED"

# Phase 1 → Phase 2 transition triggers. NEW-AM v7 (real-run trace
# a4860e94, 2026-04-25) added ``patch_file('api_spec.txt')`` as the
# canonical post-pre-render augment-completion signal. Without it
# Sonnet's patch goes untriggered and Sonnet ends up writing harness.py
# (low-quality code generation when Opus should have taken over).
TRANSITION_FILES_WRITE: frozenset[str] = frozenset(
    {"api_spec.txt", "harness.py", "requirements.txt"}
)
PHASE_1_VIOLATION_FILES: frozenset[str] = frozenset({"harness.py", "requirements.txt"})

# Error detection: NARROW pattern set that indicates ACTUAL test/command
# failures — NOT broad patterns like "error" or "404" alone (those
# false-positive when docs text mentions HTTP error codes, e.g.,
# "returns 404 for invalid keys" in fetched_docs incremented the error
# counter spuriously). Pinned by ``TestErrorClassification`` in
# ``tests/test_build_loop_behavior.py``.
ERROR_SIGNATURES: tuple[str, ...] = (
    "traceback (most recent",
    "assertionerror",
    "smoke test failed",
    "modulenotfounderror",
    "syntaxerror",
    "indentationerror",
    "connectionrefusederror",
    "connectionerror",
    "401 unauthorized",
    "403 forbidden",
    "exit code: 1",
    "exit code: 2",
    "wrong x-auth-key",
    "api key is invalid",
    "not authorized",
    "permission denied",
)

# Error classification patterns — each tuple is the substrings that map
# the error to a category. First match wins (auth → endpoint → format →
# other). Order matters: 401/403 should classify as auth even when other
# patterns also match.
_AUTH_PATTERNS: tuple[str, ...] = (
    "401", "403", "auth", "unauthorized", "forbidden", "wrong x-auth-key",
)
_ENDPOINT_PATTERNS: tuple[str, ...] = ("404", "not found", "connectionrefused")
_FORMAT_PATTERNS: tuple[str, ...] = (
    "400", "bad request", "invalid input", "unsupported",
)


ErrorCategory = Literal["auth", "endpoint", "format", "other"]
HarnessSignal = Literal["complete", "failed"]


# ---------------------------------------------------------------------------
# Status signal detection
# ---------------------------------------------------------------------------


def detect_smoke_pass(text: str) -> bool:
    """True iff text contains the SMOKE TEST PASSED marker.

    Pure substring match. The dispatch loop checks both ``last_text``
    (model-emitted) and ``all_results_text_raw`` (concatenated tool
    result content) — same predicate works for both.
    """
    return SMOKE_PASS_MARKER in text


def detect_harness_signal(text: str) -> HarnessSignal | None:
    """Returns ``"complete"`` if HARNESS_COMPLETE appears, ``"failed"``
    if HARNESS_FAILED appears, else None.

    NB: COMPLETE wins over FAILED if both appear — the agent should
    never emit both, but guard against it. Tested at the dispatch
    layer via ``TestVerificationGateContract`` (positive) and
    ``TestOutputShapeContract`` (failed signal).
    """
    if HARNESS_COMPLETE_MARKER in text:
        return "complete"
    if HARNESS_FAILED_MARKER in text:
        return "failed"
    return None


# ---------------------------------------------------------------------------
# Phase 1 → Phase 2 transition detection
# ---------------------------------------------------------------------------


def detect_phase_transition(block: Any, api_spec_written: bool) -> tuple[bool, str]:
    """Detect whether THIS dispatched block flips Phase 1 → Phase 2.

    Returns ``(transition_triggered, trigger_label)``:
      * ``transition_triggered``: True when api_spec_written should
        flip from False → True after this block.
      * ``trigger_label``: short string identifying which file caused
        the transition (e.g., ``"write_file:api_spec.txt"``,
        ``"patch_file:api_spec.txt"``). Empty when no transition.

    Triggers (only when ``api_spec_written`` is currently False):
      1. ``write_file('api_spec.txt')`` — Sonnet writing the spec from
         scratch (no pre-render fastpath). Primary trigger.
      2. ``patch_file('api_spec.txt')`` — NEW-AM v7: Sonnet PATCHING a
         pre-rendered spec (the augment path). Without this trigger,
         Sonnet's patch goes undetected and Sonnet ends up writing
         harness.py (low-quality output). Real bug from a4860e94.
      3. ``write_file('harness.py')`` or ``write_file('requirements.txt')``
         — Sonnet skipped the spec entirely. PHASE 1 PROMPT VIOLATION
         (Sonnet should never write code files in Phase 1) — flag the
         transition so model swaps to Opus, but caller should log a
         warning. Pinned by ``TestPhaseTransitionTriggers``.

    Pure function. Block content + flag in → tuple out. No state mutation.
    """
    if api_spec_written:
        return (False, "")

    btype = getattr(block, "type", "")
    if btype != "tool_use":
        return (False, "")

    name = getattr(block, "name", "")
    block_input = getattr(block, "input", {}) or {}
    filename = block_input.get("filename", "")

    if name == "write_file" and filename in TRANSITION_FILES_WRITE:
        return (True, f"write_file:{filename}")

    if name == "patch_file" and filename == "api_spec.txt":
        return (True, "patch_file:api_spec.txt")

    return (False, "")


def is_phase_1_code_violation(trigger_label: str) -> bool:
    """True iff the transition trigger represents a Phase 1 prompt
    violation (Sonnet wrote a code file when it should have only
    written/patched the spec).

    Caller logs a warning for these — model still switches to Opus,
    but the harness was code-generated by Sonnet (lower quality for
    code) instead of Opus.
    """
    if not trigger_label.startswith("write_file:"):
        return False
    filename = trigger_label.removeprefix("write_file:")
    return filename in PHASE_1_VIOLATION_FILES


# ---------------------------------------------------------------------------
# Error detection + classification
# ---------------------------------------------------------------------------


def detect_tool_result_error(text: str) -> bool:
    """True iff text contains an ACTUAL error signature (not a doc
    excerpt mentioning an HTTP code).

    Match is case-insensitive against the ``ERROR_SIGNATURES`` tuple.
    The narrow pattern set is the load-bearing design choice — earlier
    versions matched bare ``"error"`` or ``"404"`` which spuriously
    incremented the consecutive_errors counter when fetched_docs.txt
    contained "returns 404 for invalid keys".
    """
    lowered = text.lower()
    return any(sig in lowered for sig in ERROR_SIGNATURES)


def classify_tool_result_error(text: str) -> ErrorCategory:
    """Map an error-bearing text to one of four categories.

    Categories drive the PATTERN DETECTED hint in the reassessment
    message — when the same category fires 3+ times in a row, the
    builder's APPROACH (not just the details) is wrong, and the
    reassessment cites the specific category to focus on.

    Order: auth → endpoint → format → other. First match wins.
    The order reflects priority — a 401 + 404 in the same text
    should classify as auth (auth is upstream of endpoint).

    Pure function. No I/O. Caller is responsible for first checking
    ``detect_tool_result_error(text)``; calling this on a non-error
    text returns "other" (which is a safe default).
    """
    lowered = text.lower()
    if any(p in lowered for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in lowered for p in _ENDPOINT_PATTERNS):
        return "endpoint"
    if any(p in lowered for p in _FORMAT_PATTERNS):
        return "format"
    return "other"


# ---------------------------------------------------------------------------
# Reassessment trigger + message builder
# ---------------------------------------------------------------------------


def should_inject_reassessment(
    consecutive_errors: int, max_consecutive: int,
) -> bool:
    """True iff the dispatch loop should inject a STRATEGIC REASSESSMENT
    user message before the next API call.

    Pure predicate. The threshold is callable-supplied so tests can
    use a smaller threshold without mocking constants.
    """
    return consecutive_errors >= max_consecutive


# Category label used in the PATTERN DETECTED hint. Pinned by
# ``TestErrorClassification`` end-to-end + helper unit tests.
_CATEGORY_LABELS: dict[ErrorCategory, str] = {
    "auth": "authentication/auth header",
    "endpoint": "endpoint URL",
    "format": "request format/body",
    "other": "error",
}


def build_reassessment_message(
    *,
    consecutive_errors: int,
    last_errors: str,
    error_history: list[tuple[int, ErrorCategory]],
    total_reassessments: int,
    approaches_tried: list[str],
) -> str:
    """Construct the STRATEGIC REASSESSMENT user message.

    Three escalating tiers based on how many reassessments have already
    fired in this build:
      * Tier 1 — root-cause it: emit a <root_cause_analysis> block, fix
        the specific issue.
      * Tier 2 — question your assumptions: the APPROACH may be wrong,
        not just the details. ask_research with a DOC_MAP URL.
      * Tier 3+ — STRUCTURED PIVOT: three different approaches required
        (per ``puzzleeval.api_patterns.STRUCTURED_PIVOT_PROMPT``).

    Pattern detection: when ``error_history``'s last 3 entries share
    the same category, append a "PATTERN DETECTED" hint citing the
    specific category label (auth header / endpoint URL / request
    format/body / error). This is the load-bearing escalation signal —
    pinned by ``TestErrorClassification`` end-to-end.

    Pure function. No state mutation. Caller appends the returned string
    as a ``{"role": "user", "content": ...}`` message + manages the
    counter resets.
    """
    # Pattern detection — same category 3+ times in a row.
    pattern_hint = ""
    if len(error_history) >= 3:
        recent_cats = [cat for _, cat in error_history[-3:]]
        if len(set(recent_cats)) == 1:
            cat = recent_cats[0]
            cat_label = _CATEGORY_LABELS[cat]
            pattern_hint = (
                f"\n**PATTERN DETECTED:** The same '{cat_label}' error has occurred "
                f"3+ times. This strongly suggests your fundamental assumption about "
                f"the {cat_label} is WRONG — not the details. "
                f"Use `ask_research` to verify it — but read the DOC_MAP in api_spec.txt "
                f"FIRST and target the specific doc URL that covers '{cat_label}'.\n"
            )

    approaches_summary = (
        f"\n**Approaches already attempted (do NOT repeat):**\n"
        + "\n".join(f"  - {a}" for a in approaches_tried[-5:])
        if approaches_tried else ""
    )

    # Escalating tiers REQUIRE a root_cause_analysis block — symptom-
    # patching without root-cause reasoning is what causes spirals.
    root_cause_requirement = (
        "\n**REQUIRED BEFORE ANY patch_file / write_file:** emit a "
        "`<root_cause_analysis>` block answering:\n"
        "  1. What exactly failed (the error message, not a summary)\n"
        "  2. What assumption did I make in the code? (cite the line)\n"
        "  3. What does api_spec.txt say about this? (cite the section — "
        "AUTH_HEADER / ENDPOINTS / REQUEST_FORMAT / WORKING_EXAMPLE / DOC_MAP)\n"
        "  4. Which DOC_MAP URL would resolve this? (before ask_research)\n"
        "  5. What's the specific fix? (1-sentence plan)\n"
        "If the spec doesn't answer #3, that's the gap — use ask_research with the "
        "specific DOC_MAP URL if you identified one.\n"
    )

    if total_reassessments == 1:
        return (
            f"\n\n## STRATEGIC REASSESSMENT (Tier 1 — root-cause it)\n\n"
            f"You have hit errors for {consecutive_errors} consecutive turns.\n"
            f"**Last error:** {last_errors[:300]}\n"
            + root_cause_requirement
            + approaches_summary
            + pattern_hint
        )
    if total_reassessments == 2:
        return (
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
    # Tier 3+: structured pivot
    from puzzleeval.api_patterns import STRUCTURED_PIVOT_PROMPT
    return (
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


# ---------------------------------------------------------------------------
# ask_research enrichment (impure: one optional file read on api_spec.txt)
# ---------------------------------------------------------------------------


def enrich_research_question(
    *,
    question: str,
    candidate_name: str,
    candidate_provider: str,
    candidate_docs_url: str,
    spec_path: Any,  # pathlib.Path
    prior_results_text: str,
    harness_code: str | None,
) -> str:
    """Enrich a bare ask_research question with build-loop context.

    Claude Code pattern: sub-agents get RELEVANT context, not bare
    queries. Generic question → generic answer; question + spec excerpt
    + last error + harness snippet → targeted answer that actually
    helps the builder.

    What gets included (in order):
      1. Service identity (name + provider + docs_url) — anchors the
         research agent on the right vendor.
      2. The question itself.
      3. ``WHAT WE ALREADY KNOW`` block: first 2K of api_spec.txt
         PLUS DOC_REFERENCES + DOC_MAP sections (when present beyond
         the first 2K). The DOC_MAP is the load-bearing part — it
         gives the research agent URL hints for targeted lookups
         instead of broad searching.
      4. ``LAST ERROR CONTEXT``: first 500 chars of prior tool results
         this turn, if any. Lets the research agent see WHAT FAILED.
      5. ``CURRENT HARNESS CODE``: first 40 lines of harness.py (if
         present). Lets the research agent see WHAT THE BUILDER WROTE
         that's failing.

    Pure-ish: ONE file read (``spec_path.read_text``). No mutation, no
    network. Caller passes ``spec_path`` (so the helper doesn't need
    to know the sandbox structure) and ``harness_code`` (caller has
    already invoked ``_read_harness_code``).
    """
    enriched = f"Service: {candidate_name} ({candidate_provider})\n"
    enriched += f"API Docs URL: {candidate_docs_url}\n\n"
    enriched += f"QUESTION: {question}\n"

    # Include what we already know (so research doesn't re-find it)
    try:
        if spec_path.exists():
            full_spec = spec_path.read_text(encoding="utf-8")
            spec_summary = full_spec[:2000]
            # Also include DOC_REFERENCES and DOC_MAP if they exist
            for section in ("DOC_REFERENCES:", "DOC_MAP:"):
                idx = full_spec.find(section)
                if idx > 2000:  # Only add if not already in the first 2K
                    section_text = full_spec[idx:idx + 1000]
                    spec_summary += f"\n\n{section_text}"
            enriched += (
                f"\nWHAT WE ALREADY KNOW (from api_spec.txt):\n{spec_summary}\n"
                "DO NOT re-research info already in the spec above. "
                "Focus on what's MISSING or WRONG.\n"
            )
    except OSError:
        # Best-effort: a missing or unreadable spec is non-fatal —
        # the enriched question still helps via the harness/error context.
        pass

    if prior_results_text:
        enriched += f"\nLAST ERROR CONTEXT: {prior_results_text[:500]}\n"

    if harness_code:
        lines = harness_code.split("\n")
        relevant = "\n".join(lines[:40])
        enriched += (
            f"\nCURRENT HARNESS CODE (first 40 lines):\n"
            f"```python\n{relevant}\n```"
        )

    return enriched


# ---------------------------------------------------------------------------
# write_file gate predicates (Phase B: B1, B2, B3)
# ---------------------------------------------------------------------------
#
# These are PURE PREDICATES used by tools.py::write_file to decide whether
# to allow / warn / reject a write_file call. Each predicate is testable
# in isolation; the soft-vs-hard tier and the env-var bypass live at the
# call site (tools.py + config.py).
#
# Per AD-007: code enforces, prompts teach. These gates are defense in
# depth, not the only enforcement — the builder prompt also teaches the
# canonical file list and the phase-aware write order.

# Exact-match meta-file names (case-insensitive). Narrow allowlist, not a
# regex — a regex would block legitimate filenames in future modalities.
# When in doubt, keep the gate's coverage tight.
FORBIDDEN_META_FILENAMES: frozenset[str] = frozenset({
    "notes.md",
    "notes.txt",
    "status.txt",
    "progress.md",
    "state.md",
    "memory.txt",
    "plan.md",
    "todo.md",
})

# Prefix patterns for introspection scripts. Builders should write these
# only when debugging an error from a real harness.run() failure — and
# only AFTER harness.py exists.
_INTROSPECTION_PREFIXES: tuple[str, ...] = (
    "inspect_",
    "check_",
    "explore_",
    "probe_",
)

# Phase 2 scaffold files. The builder MUST patch api_spec.txt (so
# api_spec_written flips True and the model switch fires) BEFORE writing
# any of these. Pre-spec writes produce code generated by Sonnet, which
# is empirically lower-quality than Opus on harness implementation.
SCAFFOLD_FILENAMES: frozenset[str] = frozenset({
    "harness.py",
    "smoke_test.py",
    "live_test.py",
    "requirements.txt",
})


def is_forbidden_meta_filename(filename: str) -> bool:
    """Return True iff `filename` is a known meta-file the builder shouldn't write.

    Case-insensitive, exact-match against `FORBIDDEN_META_FILENAMES`. Path
    separators in `filename` are ignored — the caller passes the basename.

    Out-of-distribution risk: future modalities might want a `notes.md`-style
    file legitimately. Recovery: env-var `PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES=0`
    disables this gate. Builder also adapts via the rejection message.
    """
    if not filename:
        return False
    return filename.lower() in FORBIDDEN_META_FILENAMES


def is_introspection_script_name(filename: str) -> bool:
    """Return True iff `filename` looks like an introspection probe script.

    Matches common prefixes (`inspect_`, `check_`, `explore_`, `probe_`) on
    .py files. The CALLER (tools.py) decides whether to warn — typically
    only when `harness.py` does not yet exist in the sandbox, since post-
    harness probes are legitimate during debugging.

    Out-of-distribution risk: a future modality might legitimately need a
    pre-harness probe script. This gate is WARN-only (log + allow), so OOD
    impact is observability noise, not blocked work.
    """
    if not filename or not filename.endswith(".py"):
        return False
    name_lower = filename.lower()
    return any(name_lower.startswith(prefix) for prefix in _INTROSPECTION_PREFIXES)


def turn_used_prespec_research(
    response_content: list,
    response_usage: Any,
    *,
    api_spec_exists: bool,
) -> bool:
    """Return True iff this turn used a research-style tool that should
    count toward the pre-spec research budget (gate B4).

    Signals:
      * `response.usage.server_tool_use.web_search_requests > 0`
      * `tool_use` block for `ask_research` (BUT only when api_spec.txt
        already exists — the existing Phase-1 ask_research gate refuses
        the call when no spec is present, so blocked calls don't count)
      * `server_tool_use` block for `web_fetch` or `web_search`

    `api_spec_exists` mirrors the existing Phase-1 ask_research gate: when
    no spec file is on disk, ask_research is refused and shouldn't count
    against the budget. When a pre-rendered spec is on disk (api_spec_written
    may still be False), ask_research is allowed and DOES count.
    """
    server_tool_use = getattr(response_usage, "server_tool_use", None)
    if server_tool_use:
        if (getattr(server_tool_use, "web_search_requests", 0) or 0) > 0:
            return True
    for block in response_content or ():
        block_type = getattr(block, "type", "")
        block_name = getattr(block, "name", "")
        if block_type == "server_tool_use" and block_name in ("web_fetch", "web_search"):
            return True
        if block_type == "tool_use" and block_name == "ask_research" and api_spec_exists:
            return True
    return False


def is_phase1_scaffold_violation(filename: str, *, api_spec_written: bool) -> bool:
    """Return True iff writing `filename` would violate Phase 1 → Phase 2 ordering.

    Phase-keyed (NOT model-keyed) per the architectural contract: "no
    scaffold writes while `api_spec_written` is False." Model-keyed checks
    would falsely reject when the model fallback ladder (Opus → Sonnet →
    Haiku on rate-limit / 5xx) puts a non-Opus model in a Phase 2 context.

    Out-of-distribution risk: if a future flow legitimately needs a
    pre-spec scaffold write, env-var `PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK=0`
    disables this gate. The current pipeline shape always has Phase 1
    spec-writing before Phase 2 code-writing, so the OOD risk is low.
    """
    if api_spec_written:
        return False
    if not filename:
        return False
    return filename.lower() in {name.lower() for name in SCAFFOLD_FILENAMES}


__all__ = [
    "ERROR_SIGNATURES",
    "ErrorCategory",
    "FORBIDDEN_META_FILENAMES",
    "HARNESS_COMPLETE_MARKER",
    "HARNESS_FAILED_MARKER",
    "HarnessSignal",
    "PHASE_1_VIOLATION_FILES",
    "SCAFFOLD_FILENAMES",
    "SMOKE_PASS_MARKER",
    "TRANSITION_FILES_WRITE",
    "build_reassessment_message",
    "classify_tool_result_error",
    "detect_harness_signal",
    "detect_phase_transition",
    "detect_smoke_pass",
    "detect_tool_result_error",
    "enrich_research_question",
    "is_forbidden_meta_filename",
    "is_introspection_script_name",
    "is_phase1_scaffold_violation",
    "turn_used_prespec_research",
    "is_phase_1_code_violation",
    "should_inject_reassessment",
]
