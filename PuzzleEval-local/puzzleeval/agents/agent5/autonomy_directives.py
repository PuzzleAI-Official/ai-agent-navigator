"""Directive constants for the autonomy artifact layer.

The orchestrator can inject these as user messages at specific trigger
points. Build-plan directives are legacy/experimental and disabled by
default; reflection directives remain load-bearing when the completion gate
needs evidence in ``_agent_state/reflection_phase_3.md``.

Per AD-007, the directives are deterministic Python-side enforcement
of contracts that prompt-only teaching can't reliably enforce. The
constants live in this module so they're greppable + testable + reused
across the build_loop, dispatch_helpers, and tests.

This module owns message wording only. Whether a directive fires is governed
by config flags in ``config.py`` and gate logic in ``build_loop.py``;
``verification.py`` owns acceptance semantics.

These strings are LOAD-BEARING product behavior — changing them
materially alters Agent 5's behavior. Tests pin the exact wording.
"""

from __future__ import annotations


# ============================================================================
# Turn 1 — initialize the planning artifact
# ============================================================================

BUILD_PLAN_INIT_DIRECTIVE = (
    "Optional diagnostic planning is enabled for this run. Before any "
    "scaffolding, write `_agent_state/build_plan.md` as a compact operator "
    "todo list. This file is not build authority; objective.md, "
    "runtime_state.json, research_synthesis.json, and "
    "implementation_plan.json remain the action sources. "
    "Use this exact structure:\n"
    "\n"
    "```markdown\n"
    "# Build plan for <candidate>\n"
    "\n"
    "## Research synthesis [status: in_progress]\n"
    "- [ ] Fetch primary docs: <url from objective.md CONSTRAINTS>\n"
    "- [ ] Write `_agent_state/research_plan.json` for unresolved build-critical gaps\n"
    "- [ ] Consolidate findings in `_agent_state/research_synthesis.json`\n"
    "- [ ] Write `_agent_state/implementation_plan.json` for build-gate validation\n"
    "\n"
    "## Build [status: not_started]\n"
    "- [ ] requirements.txt with pinned versions\n"
    "- [ ] harness.py implementing run(input_data) -> dict\n"
    "- [ ] optional smoke_test.py only if useful for offline mechanical checks\n"
    "- [ ] _forensics.py imports + traced_op wrapping per AD-011\n"
    "\n"
    "## Verify [status: not_started]\n"
    "- [ ] live_test.py with production payload shape\n"
    "- [ ] Live test passes for each modality in test cases\n"
    "\n"
    "## Deliver [status: not_started]\n"
    "- [ ] All SUCCESS CRITERIA evidenced in reflection_phase_3.md\n"
    "- [ ] HARNESS_COMPLETE\n"
    "\n"
    "## Decisions log\n"
    "<append a one-line note when you revise the plan or pivot>\n"
    "```\n"
    "\n"
    "Read `_agent_state/objective.md` first so the plan reflects your "
    "actual SUCCESS CRITERIA + CONSTRAINTS for THIS candidate. The plan is "
    "a LIVING document — update it at these trigger points (NOT every turn): "
    "after implementation_plan.json is accepted; after scaffold writes; after "
    "a failed check/probe or live test; after a pivot; before HARNESS_COMPLETE. "
    "Use `patch_file('_agent_state/build_plan.md', ...)` for updates so the "
    "decisions log accumulates context across turns.\n"
    "\n"
    "Because this optional build-plan directive is enabled, create the plan "
    "once, then continue with the normal research-plan -> synthesis -> "
    "implementation-plan flow."
)


def initial_build_plan_content(candidate_name: str) -> str:
    """Return the seed build plan staged before turn 0.

    The file is agent-writable; this seed guarantees fast-path builds
    have a plan artifact before scaffold writes. Agent 5 should treat it as
    optional orientation unless build-plan directives are explicitly enabled.
    """
    return (
        f"# Build plan for {candidate_name}\n\n"
        "> Orchestrator-seeded before turn 0 so fast-path builds have a "
        "planning artifact before scaffold writes. Read only if useful; "
        "do not maintain unless explicitly directed.\n\n"
        "## Research synthesis [status: in_progress]\n"
        "- [ ] Read _agent_state/objective.md\n"
        "- [ ] Write research_plan.json for unresolved build-critical gaps\n"
        "- [ ] Write research_synthesis.json and implementation_plan.json\n\n"
        "## Build [status: not_started]\n"
        "- [ ] requirements.txt with pinned versions\n"
        "- [ ] harness.py implementing run(input_data) -> dict\n"
        "- [ ] optional smoke_test.py for offline mechanical checks\n"
        "- [ ] live_test.py using production payload shape\n"
        "- [ ] forensics instrumentation per AD-011\n\n"
        "## Verify [status: not_started]\n"
        "- [ ] final harness passes representative probe or records a genuine external block\n"
        "- [ ] live_test.py passes or failure is explained with evidence\n\n"
        "## Deliver [status: not_started]\n"
        "- [ ] reflection_phase_3.md cites evidence for objective.md\n"
        "- [ ] HARNESS_COMPLETE\n\n"
        "## Decisions log\n"
        "- turn 0: system seeded this plan; update at trigger points.\n"
    )


# ============================================================================
# Trigger-based nudge — build_plan.md staleness
# ============================================================================
# Fires only when ``PUZZLEEVAL_AUTONOMY_BUILD_PLAN_DIRECTIVES`` is enabled.
# Soft nudge — does NOT block the build. Disabled by default because recent
# real runs showed plan-maintenance turn cost without behavior improvement.

BUILD_PLAN_STALENESS_NUDGE = (
    "Heads up: a trigger event just occurred ({trigger}) and "
    "`_agent_state/build_plan.md` hasn't been updated since the last "
    "trigger. Because optional diagnostic planning is enabled, patch "
    "build_plan.md briefly if the trigger changed your todo list; otherwise "
    "continue with the first-class artifact or code action. Do not treat "
    "build_plan.md as build authority."
)


# ============================================================================
# Pre-HARNESS_COMPLETE reflection (load-bearing — PR 2 will gate on this)
# ============================================================================
# Fires when the agent's text contains HARNESS_COMPLETE but no
# reflection_phase_3.md exists yet. The agent is asked to write the
# reflection BEFORE the orchestrator accepts the signal.
#
# The verifier now rejects HARNESS_COMPLETE on missing/vacuous/unsupported
# reflection evidence until the issue-specific retry policy is exhausted.

REFLECTION_PHASE_3_DIRECTIVE = (
    "You signaled HARNESS_COMPLETE. Before that's accepted, write "
    "`_agent_state/reflection_phase_3.md` with EVIDENCE (file references "
    "like `harness.py:42`, test output snippets, forensics events, code "
    "excerpts) for each section below. Self-attestation ('yes, handled') "
    "is not evidence and is rejected by the gate. "
    "Write substantive citations now so this artifact is useful to the "
    "verifier and to operators reviewing the build.\n"
    "\n"
    "```markdown\n"
    "# Reflection: pre-HARNESS_COMPLETE\n"
    "\n"
    "## SUCCESS CRITERIA evidence walk-through\n"
    "For each item in objective.md SUCCESS CRITERIA, cite the evidence\n"
    "(file + line OR test output snippet OR forensics event):\n"
    "\n"
    "<fill in for each criterion>\n"
    "\n"
    "## Adversarial probe robustness\n"
    "For each of the 6 probes (empty_input, max_input, malformed_input,\n"
    "idempotency, concurrency, repeat_call), cite the engineering decision\n"
    "(code reference REQUIRED):\n"
    "\n"
    "<fill in for each probe>\n"
    "\n"
    "## User-fit assessment\n"
    "For each sub-task in objective.md DELIVERABLE, cite the harness code\n"
    "path that addresses it:\n"
    "\n"
    "<fill in for each sub-task>\n"
    "\n"
    "## Code quality assessment\n"
    "For each code-quality criterion in SUCCESS CRITERIA, cite the code\n"
    "reference:\n"
    "\n"
    "- separation of concerns: <harness.py: lines X-Y>\n"
    "- no swallowed exceptions: <error paths cited>\n"
    "- resource cleanup: <cleanup calls cited at all return points>\n"
    "- no shared mutable state: <proof at module level + run() function>\n"
    "\n"
    "## Self-critique\n"
    "What's one thing you'd fix with one more turn? (cite the problematic\n"
    "code path)\n"
    "\n"
    "<fill in>\n"
    "```\n"
    "\n"
    "After writing the reflection, repeat HARNESS_COMPLETE in your next "
    "message. The completion gate will accept it only if the reflection "
    "passes evidence checks."
)


# ============================================================================
# Trigger labels — used for telemetry + the staleness nudge format string
# ============================================================================

TRIGGER_SCAFFOLD_WRITTEN = "scaffold files written"
TRIGGER_SMOKE_PASSED = "smoke test passed"
TRIGGER_SMOKE_FAILED = "smoke test failed"
TRIGGER_LIVE_FAILED = "live test failed"
TRIGGER_PRE_HARNESS_COMPLETE = "pre-HARNESS_COMPLETE"


# ============================================================================
# Telemetry event names
# ============================================================================
# Use these constants when emitting structured logs so consumers can
# pattern-match without typo risk. Each name maps to a `gate_fired` /
# `directive_fired` style log entry the build_loop emits.

EVENT_BUILD_PLAN_INIT_DIRECTIVE_FIRED = "autonomy_build_plan_init_directive_fired"
EVENT_BUILD_PLAN_INIT_SKIPPED = "autonomy_build_plan_init_skipped"
EVENT_BUILD_PLAN_STALE_AT_TRIGGER = "autonomy_build_plan_stale_at_trigger"
EVENT_REFLECTION_PHASE_3_DIRECTIVE_FIRED = "autonomy_reflection_phase_3_directive_fired"
EVENT_REFLECTION_PHASE_3_WRITTEN = "autonomy_reflection_phase_3_written"
EVENT_AGENT_OBSERVATION_RECORDED = "autonomy_agent_observation_recorded"
EVENT_OBJECTIVE_MODIFY_BLOCKED = "autonomy_objective_modify_blocked"
EVENT_RUNTIME_STATE_WRITE_BLOCKED = "autonomy_runtime_state_write_blocked"


__all__ = [
    "BUILD_PLAN_INIT_DIRECTIVE",
    "initial_build_plan_content",
    "BUILD_PLAN_STALENESS_NUDGE",
    "REFLECTION_PHASE_3_DIRECTIVE",
    "TRIGGER_SCAFFOLD_WRITTEN",
    "TRIGGER_SMOKE_PASSED",
    "TRIGGER_SMOKE_FAILED",
    "TRIGGER_LIVE_FAILED",
    "TRIGGER_PRE_HARNESS_COMPLETE",
    "EVENT_BUILD_PLAN_INIT_DIRECTIVE_FIRED",
    "EVENT_BUILD_PLAN_INIT_SKIPPED",
    "EVENT_BUILD_PLAN_STALE_AT_TRIGGER",
    "EVENT_REFLECTION_PHASE_3_DIRECTIVE_FIRED",
    "EVENT_REFLECTION_PHASE_3_WRITTEN",
    "EVENT_AGENT_OBSERVATION_RECORDED",
    "EVENT_OBJECTIVE_MODIFY_BLOCKED",
    "EVENT_RUNTIME_STATE_WRITE_BLOCKED",
]
