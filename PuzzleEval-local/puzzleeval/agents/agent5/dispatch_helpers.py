"""Pure dispatch-loop helpers for the Agent 5 build loop.

Phase 4 Path B Step 2 â€” extracts the PURE PREDICATES + PURE FORMATTERS
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
# every turn â€” these helpers are the canonical predicates for both.
SMOKE_PASS_MARKER = "SMOKE TEST PASSED"
HARNESS_COMPLETE_MARKER = "HARNESS_COMPLETE"
HARNESS_FAILED_MARKER = "HARNESS_FAILED"

# Error detection: NARROW pattern set that indicates ACTUAL test/command
# failures â€” NOT broad patterns like "error" or "404" alone (those
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

# Error classification patterns â€” each tuple is the substrings that map
# the error to a category. First match wins (auth â†’ endpoint â†’ format â†’
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
    result content) â€” same predicate works for both.
    """
    return SMOKE_PASS_MARKER in text


def detect_harness_signal(text: str) -> HarnessSignal | None:
    """Returns ``"complete"`` if HARNESS_COMPLETE appears, ``"failed"``
    if HARNESS_FAILED appears, else None.

    NB: COMPLETE wins over FAILED if both appear â€” the agent should
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
# Error detection + classification
# ---------------------------------------------------------------------------


def detect_tool_result_error(text: str) -> bool:
    """True iff text contains an ACTUAL error signature (not a doc
    excerpt mentioning an HTTP code).

    Match is case-insensitive against the ``ERROR_SIGNATURES`` tuple.
    The narrow pattern set is the load-bearing design choice â€” earlier
    versions matched bare ``"error"`` or ``"404"`` which spuriously
    incremented the consecutive_errors counter when fetched_docs.txt
    contained "returns 404 for invalid keys".
    """
    lowered = text.lower()
    return any(sig in lowered for sig in ERROR_SIGNATURES)


def classify_tool_result_error(text: str) -> ErrorCategory:
    """Map an error-bearing text to one of four categories.

    Categories drive the PATTERN DETECTED hint in the reassessment
    message â€” when the same category fires 3+ times in a row, the
    builder's APPROACH (not just the details) is wrong, and the
    reassessment cites the specific category to focus on.

    Order: auth â†’ endpoint â†’ format â†’ other. First match wins.
    The order reflects priority â€” a 401 + 404 in the same text
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
      * Tier 1 â€” root-cause it: emit a <root_cause_analysis> block, fix
        the specific issue.
      * Tier 2 â€” question your assumptions: the APPROACH may be wrong,
        not just the details. ask_research against a planned task or
        concrete debug gap.
      * Tier 3+ â€” STRUCTURED PIVOT: three different approaches required
        (per ``puzzleeval.api_patterns.STRUCTURED_PIVOT_PROMPT``).

    Pattern detection: when ``error_history``'s last 3 entries share
    the same category, append a "PATTERN DETECTED" hint citing the
    specific category label (auth header / endpoint URL / request
    format/body / error). This is the load-bearing escalation signal â€”
    pinned by ``TestErrorClassification`` end-to-end.

    Pure function. No state mutation. Caller appends the returned string
    as a ``{"role": "user", "content": ...}`` message + manages the
    counter resets.
    """
    # Pattern detection â€” same category 3+ times in a row.
    pattern_hint = ""
    if len(error_history) >= 3:
        recent_cats = [cat for _, cat in error_history[-3:]]
        if len(set(recent_cats)) == 1:
            cat = recent_cats[0]
            cat_label = _CATEGORY_LABELS[cat]
            pattern_hint = (
                f"\n**PATTERN DETECTED:** The same '{cat_label}' error has occurred "
                f"3+ times. This strongly suggests your fundamental assumption about "
                f"the {cat_label} is WRONG â€” not the details. "
                f"Use `ask_research` to verify it only as a planned task or "
                f"FIELD NEEDED/WHY debug gap grounded in research_synthesis.json "
                f"or latest_failure_packet.json.\n"
            )

    approaches_summary = (
        f"\n**Approaches already attempted (do NOT repeat):**\n"
        + "\n".join(f"  - {a}" for a in approaches_tried[-5:])
        if approaches_tried else ""
    )

    # Escalating tiers REQUIRE a root_cause_analysis block â€” symptom-
    # patching without root-cause reasoning is what causes spirals.
    root_cause_requirement = (
        "\n**REQUIRED BEFORE ANY patch_file / write_file:** emit a "
        "`<root_cause_analysis>` block answering:\n"
        "  1. What exactly failed (the error message, not a summary)\n"
        "  2. What assumption did I make in the code? (cite the line)\n"
        "  3. What do research_synthesis.json, implementation_plan.json, "
        "and latest_failure_packet.json say about this? (cite the field)\n"
        "  4. Which planned research task or FIELD NEEDED/WHY debug gap "
        "would resolve this? (before ask_research)\n"
        "  5. What's the specific fix? (1-sentence plan)\n"
        "If the durable research artifacts don't answer #3, that's the gap â€” "
        "use ask_research only with a matching task_id or concrete debug gap.\n"
    )

    if total_reassessments == 1:
        return (
            f"\n\n## STRATEGIC REASSESSMENT (Tier 1 â€” root-cause it)\n\n"
            f"You have hit errors for {consecutive_errors} consecutive turns.\n"
            f"**Last error:** {last_errors[:300]}\n"
            + root_cause_requirement
            + approaches_summary
            + pattern_hint
        )
    if total_reassessments == 2:
        return (
            f"\n\n## STRATEGIC REASSESSMENT (Tier 2 â€” question your assumptions)\n\n"
            f"You have been stuck for multiple error cycles. Fixing details is not working.\n"
            f"**Last error:** {last_errors[:300]}\n\n"
            f"Your APPROACH may be wrong â€” not just the details. The endpoint URL, "
            f"API version, or platform may have changed since the research was done.\n\n"
            + root_cause_requirement
            + approaches_summary
            + f"\n**REQUIRED ACTION:** Before ANY more patches, use `ask_research` to "
              f"verify your fundamental assumption through a declared research task "
              f"or FIELD NEEDED/WHY debug gap. Do not do broad searching.\n"
            + pattern_hint
        )
    # Tier 3+: structured pivot
    from puzzleeval.api_patterns import STRUCTURED_PIVOT_PROMPT
    return (
        f"\n\n## STRATEGIC REASSESSMENT (Tier {total_reassessments} â€” STRUCTURED PIVOT)\n\n"
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
# ask_research enrichment (impure: optional local artifact reads)
# ---------------------------------------------------------------------------


def enrich_research_question(
    *,
    question: str,
    candidate_name: str,
    candidate_provider: str,
    candidate_docs_url: str,
    sandbox_dir: Any,  # pathlib.Path
    prior_results_text: str,
    harness_code: str | None,
) -> str:
    """Enrich a bare ask_research question with build-loop context.

    Claude Code pattern: sub-agents get RELEVANT context, not bare
    queries. Generic question â†’ generic answer; question + durable artifacts
    + last error + harness snippet â†’ targeted answer that actually
    helps the builder.

    What gets included (in order):
      1. Service identity (name + provider + docs_url) â€” anchors the
         research agent on the right vendor.
      2. The question itself.
      3. ``WHAT WE ALREADY KNOW`` from docs_entrypoint, research_plan,
         research_synthesis, implementation_plan, failure packets, and findings.
      4. ``LAST ERROR CONTEXT``: first 500 chars of prior tool results
         this turn, if any. Lets the research agent see WHAT FAILED.
      5. ``CURRENT HARNESS CODE``: first 40 lines of harness.py (if
         present). Lets the research agent see WHAT THE BUILDER WROTE
         that's failing.

    Pure-ish: bounded local artifact reads. No mutation, no network. Caller
    passes ``sandbox_dir`` and ``harness_code`` because the build loop already
    has both.
    """
    enriched = f"Service: {candidate_name} ({candidate_provider})\n"
    enriched += f"API Docs URL: {candidate_docs_url}\n\n"
    enriched += f"QUESTION: {question}\n"

    state_dir = sandbox_dir / "_agent_state"
    known_parts: list[str] = []
    for rel, limit in (
        ("docs_entrypoint.json", 1500),
        ("research_plan.json", 2000),
        ("research_synthesis.json", 3000),
        ("implementation_plan.json", 2000),
        ("latest_failure_packet.json", 1500),
    ):
        path = state_dir / rel
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8")
                if text.strip():
                    known_parts.append(f"\n## {rel}\n{text[:limit]}")
        except OSError:
            pass
    findings_dir = state_dir / "research_findings"
    if findings_dir.exists():
        try:
            snippets: list[str] = []
            for path in sorted(findings_dir.glob("*.json"))[:5]:
                snippets.append(f"### {path.name}\n{path.read_text(encoding='utf-8')[:900]}")
            if snippets:
                known_parts.append("\n## research_findings\n" + "\n".join(snippets))
        except OSError:
            pass
    if known_parts:
        enriched += (
            "\nWHAT WE ALREADY KNOW (durable Agent 5 artifacts):\n"
            + "".join(known_parts)
            + "\nDO NOT re-research confirmed facts above. Focus on the missing "
            "or wrong implementation-changing field.\n"
        )

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
# depth, not the only enforcement â€” the builder prompt also teaches the
# canonical file list and the phase-aware write order.

# Exact-match meta-file names (case-insensitive). Narrow allowlist, not a
# regex â€” a regex would block legitimate filenames in future modalities.
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
# only when debugging an error from a real harness.run() failure â€” and
# only AFTER harness.py exists.
_INTROSPECTION_PREFIXES: tuple[str, ...] = (
    "inspect_",
    "check_",
    "explore_",
    "probe_",
)

# Build scaffold files. In the default architecture these are blocked until
# _agent_state/implementation_plan.json is accepted. The legacy
# ``build_gate_accepted`` variable is now a compatibility boolean for "build gate
# satisfied" at the call site.
SCAFFOLD_FILENAMES: frozenset[str] = frozenset({
    "harness.py",
    "smoke_test.py",
    "live_test.py",
    "requirements.txt",
})


def is_forbidden_meta_filename(filename: str) -> bool:
    """Return True iff `filename` is a known meta-file the builder shouldn't write.

    Case-insensitive, exact-match against `FORBIDDEN_META_FILENAMES`. Path
    separators in `filename` are ignored â€” the caller passes the basename.

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
    .py files. The CALLER (tools.py) decides whether to warn â€” typically
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


def turn_used_prebuild_research(
    response_content: list,
    response_usage: Any,
    *,
    build_gate_artifact_exists: bool = False,
) -> bool:
    """Return True iff this turn used a research-style tool that should
    count toward the pre-build research budget (gate B4).

    Signals:
      * `response.usage.server_tool_use.web_search_requests > 0`
      * `tool_use` block for `ask_research`
      * `server_tool_use` block for `web_fetch` or `web_search`

    ``build_gate_artifact_exists`` is accepted for call-site symmetry; current
    planned-research flow counts any pre-build ask_research attempt as a
    research turn.
    """
    _ = build_gate_artifact_exists
    server_tool_use = getattr(response_usage, "server_tool_use", None)
    if server_tool_use:
        if (getattr(server_tool_use, "web_search_requests", 0) or 0) > 0:
            return True
    for block in response_content or ():
        block_type = getattr(block, "type", "")
        block_name = getattr(block, "name", "")
        if block_type == "server_tool_use" and block_name in ("web_fetch", "web_search"):
            return True
        if block_type == "tool_use" and block_name == "ask_research":
            return True
    return False


def is_phase1_scaffold_violation(filename: str, *, build_gate_accepted: bool) -> bool:
    """Return True iff writing `filename` would violate the build gate.

    Callers pass the active implementation-plan build-gate truth.

    Out-of-distribution risk: if a future flow legitimately needs a
    pre-gate scaffold write, env-var `PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK=0`
    disables this gate.
    """
    if build_gate_accepted:
        return False
    if not filename:
        return False
    return filename.lower() in {name.lower() for name in SCAFFOLD_FILENAMES}


# ---------------------------------------------------------------------------
# Build-plan staleness triggers (PR 1 â€” Goal/Planning/State/Reflection)
# ---------------------------------------------------------------------------
# The orchestrator pings the agent at "trigger" moments to keep
# ``_agent_state/build_plan.md`` operational rather than ornamental. The
# triggers are observable transitions: implementation plan accepted, scaffold
# writes landed, smoke just passed, the agent is about to signal
# HARNESS_COMPLETE. At each point the orchestrator compares the
# build_plan.md mtime against the prior trigger; if the file hasn't been
# updated, a soft nudge fires.

# Filenames whose write/patch counts as a "scaffold trigger" (one nudge
# per turn even if multiple scaffold files land â€” write_file calls in the
# same turn are typically intentional batch writes after build-gate acceptance).
_SCAFFOLD_TRIGGER_FILENAMES: frozenset[str] = frozenset({
    "harness.py",
    "smoke_test.py",
    "live_test.py",
    "requirements.txt",
})


def detect_build_plan_triggers(
    response_content: list,
    *,
    build_gate_was_accepted: bool,
    build_gate_now_accepted: bool,
    smoke_passed_this_turn: bool,
    harness_complete_signaled: bool,
) -> list[str]:
    """Return the trigger labels that fired during this turn.

    Pure function. Inspects the model's response_content for tool_use
    blocks (scaffold writes) and the loop-state booleans
    for state-flag transitions. Used by the build_loop's staleness check
    to inject `BUILD_PLAN_STALENESS_NUDGE` when the agent's
    `_agent_state/build_plan.md` hasn't been updated alongside the
    triggers.

    Trigger labels are short human-readable strings used by the nudge
    template + telemetry. Order is deterministic: build gate â†’ scaffold â†’
    smoke pass â†’ pre-HARNESS_COMPLETE.
    """
    triggers: list[str] = []

    if build_gate_now_accepted and not build_gate_was_accepted:
        triggers.append("implementation plan accepted")

    # One scaffold trigger per turn - break after first match. The
    # canonical post-gate pattern is to write all four scaffolds in a single turn,
    # so multiple matches in one response are intentional.
    for block in response_content or []:
        if getattr(block, "type", "") != "tool_use":
            continue
        name = getattr(block, "name", "")
        if name not in ("write_file", "patch_file"):
            continue
        block_input = getattr(block, "input", None) or {}
        raw_filename = (block_input.get("filename") or "")
        # Strip directory components and lowercase for the comparison.
        filename = raw_filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if filename in _SCAFFOLD_TRIGGER_FILENAMES:
            triggers.append(f"scaffold write: {filename}")
            break

    if smoke_passed_this_turn:
        triggers.append("smoke test passed")

    if harness_complete_signaled:
        triggers.append("pre-HARNESS_COMPLETE")

    return triggers


__all__ = [
    "ERROR_SIGNATURES",
    "ErrorCategory",
    "FORBIDDEN_META_FILENAMES",
    "HARNESS_COMPLETE_MARKER",
    "HARNESS_FAILED_MARKER",
    "HarnessSignal",
    "SCAFFOLD_FILENAMES",
    "SMOKE_PASS_MARKER",
    "build_reassessment_message",
    "classify_tool_result_error",
    "detect_build_plan_triggers",
    "detect_harness_signal",
    "detect_smoke_pass",
    "detect_tool_result_error",
    "enrich_research_question",
    "is_forbidden_meta_filename",
    "is_introspection_script_name",
    "is_phase1_scaffold_violation",
    "turn_used_prebuild_research",
    "should_inject_reassessment",
]

