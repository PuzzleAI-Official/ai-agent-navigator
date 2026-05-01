"""Directive constants for the autonomy artifact layer.

The orchestrator injects these as user messages at specific trigger
points to teach the agent to maintain ``_agent_state/build_plan.md``,
``_agent_state/agent_observations.json``, and (at HARNESS_COMPLETE)
``_agent_state/reflection_phase_3.md``.

Per AD-007, the directives are deterministic Python-side enforcement
of contracts that prompt-only teaching can't reliably enforce. The
constants live in this module so they're greppable + testable + reused
across the build_loop, dispatch_helpers, and tests.

PR 1 scope: directives fire and the artifacts are written. The
reflection-evidence gate that REJECTS HARNESS_COMPLETE on missing or
vacuous reflection is PR 2 work — keep this module's exports tight to
"what fires" and let verification.py own "what gates."

These strings are LOAD-BEARING product behavior — changing them
materially alters Agent 5's behavior. Tests pin the exact wording.
"""

from __future__ import annotations


# ============================================================================
# Turn 1 — initialize the planning artifact
# ============================================================================

BUILD_PLAN_INIT_DIRECTIVE = (
    "You're at turn 1. Before any research or scaffolding, write "
    "`_agent_state/build_plan.md` — your living todo list for this build. "
    "Use this exact structure:\n"
    "\n"
    "```markdown\n"
    "# Build plan for <candidate>\n"
    "\n"
    "## Phase 1 - Research [status: in_progress]\n"
    "- [ ] Fetch primary docs: <url from objective.md CONSTRAINTS>\n"
    "- [ ] Identify auth method + endpoints + payload shapes\n"
    "- [ ] Write or patch api_spec.txt (this triggers Phase 2 model switch)\n"
    "\n"
    "## Phase 2 - Build [status: not_started]\n"
    "- [ ] requirements.txt with pinned versions\n"
    "- [ ] harness.py implementing run(input_data) -> dict\n"
    "- [ ] smoke_test.py exercising every input shape from objective.md SUCCESS CRITERIA\n"
    "- [ ] _forensics.py imports + traced_op wrapping per AD-011\n"
    "\n"
    "## Phase 3 - Verify [status: not_started]\n"
    "- [ ] live_test.py with production payload shape\n"
    "- [ ] Live test passes for each modality in test cases\n"
    "\n"
    "## Phase 4 - Deliver [status: not_started]\n"
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
    "after api_spec.txt is written or patched; after scaffold writes; after "
    "a failed smoke or live test; after a pivot; before HARNESS_COMPLETE. "
    "Use `patch_file('_agent_state/build_plan.md', ...)` for updates so the "
    "decisions log accumulates context across turns.\n"
    "\n"
    "Write build_plan.md FIRST. Then proceed with normal Phase 1 research."
)


def initial_build_plan_content(candidate_name: str) -> str:
    """Return the seed build plan staged before turn 0.

    The file is agent-writable; this seed guarantees fast-path builds
    have a plan artifact before scaffold writes. Agent 5 should patch or
    replace it when the turn-1 directive or a trigger point asks for an
    update.
    """
    return (
        f"# Build plan for {candidate_name}\n\n"
        "> Orchestrator-seeded before turn 0 so fast-path builds have a "
        "planning artifact before scaffold writes. Agent 5 owns updates.\n\n"
        "## Phase 1 - Research [status: in_progress]\n"
        "- [ ] Read _agent_state/objective.md\n"
        "- [ ] Confirm or write api_spec.txt\n\n"
        "## Phase 2 - Build [status: not_started]\n"
        "- [ ] requirements.txt with pinned versions\n"
        "- [ ] harness.py implementing run(input_data) -> dict\n"
        "- [ ] smoke_test.py covering success + error shapes\n"
        "- [ ] live_test.py using production payload shape\n"
        "- [ ] forensics instrumentation per AD-011\n\n"
        "## Phase 3 - Verify [status: not_started]\n"
        "- [ ] smoke_test.py passes\n"
        "- [ ] live_test.py passes or failure is explained with evidence\n\n"
        "## Phase 4 - Deliver [status: not_started]\n"
        "- [ ] reflection_phase_3.md cites evidence for objective.md\n"
        "- [ ] HARNESS_COMPLETE\n\n"
        "## Decisions log\n"
        "- turn 0: system seeded this plan; update at trigger points.\n"
    )


# ============================================================================
# Trigger-based nudge — build_plan.md staleness
# ============================================================================
# Fires when a meaningful state-change trigger has occurred but the agent
# hasn't updated build_plan.md since. Soft nudge — does NOT block the
# build. PR 2 may upgrade to a harder check based on telemetry data.

BUILD_PLAN_STALENESS_NUDGE = (
    "Heads up: a trigger event just occurred ({trigger}) and "
    "`_agent_state/build_plan.md` hasn't been updated since the last "
    "trigger. Take 10 seconds before your next significant action: "
    "patch_file build_plan.md with checked-off todos and any new ones "
    "the trigger surfaced. Then continue. This keeps your plan operational "
    "rather than ornamental."
)


# ============================================================================
# Pre-HARNESS_COMPLETE reflection (load-bearing — PR 2 will gate on this)
# ============================================================================
# Fires when the agent's text contains HARNESS_COMPLETE but no
# reflection_phase_3.md exists yet. The agent is asked to write the
# reflection BEFORE the orchestrator accepts the signal.
#
# In PR 1 the directive fires + telemetry records what the agent writes,
# but the verifier does NOT reject HARNESS_COMPLETE on missing/vacuous
# reflection. PR 2 ships the actual gate.

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
    "message and the build will accept (PR 1: always; PR 2 onward: only "
    "if the reflection passes evidence checks)."
)


# ============================================================================
# Trigger labels — used for telemetry + the staleness nudge format string
# ============================================================================

TRIGGER_API_SPEC_WRITTEN = "api_spec.txt written"
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
    "TRIGGER_API_SPEC_WRITTEN",
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
