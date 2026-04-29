"""
Phase 9: Per-scope test execution routing.

Pure functions that:
  1. Group test cases by scope (sub_task_ref -> scope_id mapping)
  2. Route test cases to candidates via covers_step_ids
  3. Aggregate CandidateTestRun results into ScopeTestRun
  4. Dedup tools across scopes for build (one harness per unique tool)

These functions operate on Agent 5's output -- they don't call any APIs.
They post-process the existing _execute_all_tests results into the
Phase 9 ScopeTestRun structure.
"""
from __future__ import annotations
from puzzleeval.schemas import (
    CandidateTestRun,
    ScopeTestRun,
    TestPlan,
    WorkflowBlueprint,
    TestCase,
)


def _evaluation_mode_for_scope(
    scope_id: str, test_plan: TestPlan | None
) -> str:
    """Derive ScopeTestRun.evaluation_mode from ScopeTestSpec.reference_mode.

    Gap 21: pass rates from exemplar-mode scopes (chatbot, summarization)
    aren't comparable to ground_truth scopes. The frontend renders this
    distinction as a pill per scope.
    """
    if test_plan is None:
        return "objective"
    for spec in test_plan.scope_specs:
        if spec.scope_id == scope_id:
            return "subjective" if spec.reference_mode == "exemplar" else "objective"
    return "objective"


def group_tests_by_scope(
    test_cases: list[TestCase],
    blueprint: WorkflowBlueprint | None,
) -> dict[str, list[TestCase]]:
    """
    Map test cases to scopes via sub_task_ref -> WorkflowStep.capability.

    Each test case's sub_task_ref matches a SubTask.description which
    maps to a WorkflowStep via the shared capability field. When no
    blueprint exists, all tests go to a single "flat" group.
    """
    if not blueprint or not blueprint.steps:
        return {"_flat": list(test_cases)}

    # Build capability -> step_id mapping from the blueprint.
    # Test cases reference sub_tasks by description (sub_task_ref),
    # and each sub_task has a capability that matches a step's capability.
    # For simplicity: assign test cases to scopes based on the order
    # of steps matching the order of sub_tasks.
    #
    # Practical approach: if the test case's sub_task_ref partially matches
    # a step's description or capability, assign it there. Otherwise
    # assign to the first step (fallback).
    step_ids = [s.id for s in blueprint.steps]
    step_caps = {s.id: s.capability.lower() for s in blueprint.steps}
    step_descs = {s.id: s.description.lower() for s in blueprint.steps}

    grouped: dict[str, list[TestCase]] = {sid: [] for sid in step_ids}

    for tc in test_cases:
        # PRIMARY: deterministic routing via scope_id (from TestPlan)
        if tc.scope_id and tc.scope_id in grouped:
            grouped[tc.scope_id].append(tc)
            continue

        # FALLBACK: fuzzy keyword matching (legacy path)
        ref = tc.sub_task_ref.lower()
        assigned = False
        # Try matching by capability keyword overlap
        for sid in step_ids:
            cap = step_caps[sid]
            desc = step_descs[sid]
            # Check if the test case's sub_task_ref shares significant
            # words with the step's capability or description
            cap_words = set(cap.split())
            desc_words = set(desc.split())
            ref_words = set(ref.split())
            if len(cap_words & ref_words) >= 2 or len(desc_words & ref_words) >= 2:
                grouped[sid].append(tc)
                assigned = True
                break
            # Fallback: substring match
            if cap in ref or ref in cap:
                grouped[sid].append(tc)
                assigned = True
                break
        if not assigned:
            # Default to first step (single-scope fallback)
            grouped[step_ids[0]].append(tc)

    return grouped


def build_scope_runs(
    candidate_runs: list[CandidateTestRun],
    blueprint: WorkflowBlueprint | None,
    test_cases: list[TestCase] | None = None,
    test_plan: TestPlan | None = None,
) -> list[ScopeTestRun]:
    """
    Post-process Agent 5's flat candidate_runs into per-scope ScopeTestRun.

    For 1-scope or no-blueprint workflows: wraps all candidate_runs into
    a single ScopeTestRun (backward-compat, same data).

    For N-scope workflows: groups candidate_runs by which scopes they
    cover (using test_results[].sub_task_ref as the routing signal) and
    produces one ScopeTestRun per scope.

    When ``test_plan`` is provided, each returned ScopeTestRun carries
    ``evaluation_mode`` derived from the corresponding ScopeTestSpec's
    ``reference_mode`` (Gap 21).
    """
    if not blueprint or len(blueprint.steps) <= 1:
        # Single-scope or legacy: wrap all runs into one scope
        scope_id = blueprint.steps[0].id if blueprint and blueprint.steps else "step_1"
        scope_role = blueprint.steps[0].role if blueprint and blueprint.steps else "default"
        total_tests = sum(r.total_tests for r in candidate_runs)
        return [ScopeTestRun(
            scope_id=scope_id,
            scope_role=scope_role,
            candidate_results=sorted(candidate_runs, key=lambda r: -(r.pass_rate or 0)),
            test_case_count=total_tests,
            evaluation_mode=_evaluation_mode_for_scope(scope_id, test_plan),
        )]

    # Multi-scope: group test results by scope
    step_caps = {s.id: s.capability.lower() for s in blueprint.steps}
    step_descs = {s.id: s.description.lower() for s in blueprint.steps}
    step_roles = {s.id: s.role for s in blueprint.steps}
    step_ids = [s.id for s in blueprint.steps]

    # Build test_case_id -> scope_id lookup from test_cases (deterministic path)
    tc_scope_map: dict[str, str] = {}
    if test_cases:
        for tc in test_cases:
            if tc.scope_id and tc.scope_id in step_caps:
                tc_scope_map[tc.id] = tc.scope_id

    scope_runs: list[ScopeTestRun] = []
    for scope_id in step_ids:
        cap = step_caps[scope_id]
        desc = step_descs[scope_id]

        # For each candidate, check if they have test results relevant to this scope
        scope_candidate_results: list[CandidateTestRun] = []
        scope_test_count = 0

        for cr in candidate_runs:
            # Filter test results to those matching this scope
            scope_results = []
            for tr in cr.test_results:
                # PRIMARY: deterministic routing via scope_id (from TestPlan)
                mapped_scope = tc_scope_map.get(tr.test_case_id)
                if mapped_scope is not None:
                    if mapped_scope == scope_id:
                        scope_results.append(tr)
                    continue

                # FALLBACK: fuzzy keyword matching (legacy path)
                ref = tr.sub_task_ref.lower()
                ref_words = set(ref.split())
                cap_words = set(cap.split())
                desc_words = set(desc.split())
                if (len(cap_words & ref_words) >= 2 or
                    len(desc_words & ref_words) >= 2 or
                    cap in ref or ref in cap):
                    scope_results.append(tr)

            if scope_results:
                # Build a scope-specific CandidateTestRun with only these results
                passed = sum(1 for r in scope_results if r.passed)
                failed = sum(1 for r in scope_results if not r.passed and r.success)
                errored = sum(1 for r in scope_results if not r.success and not r.skip_reason)
                skipped = sum(1 for r in scope_results if r.skip_reason)
                executed = len(scope_results) - skipped

                scope_cr = CandidateTestRun(
                    candidate_name=cr.candidate_name,
                    provider=cr.provider,
                    harness_dir=cr.harness_dir,
                    status=cr.status,
                    test_results=scope_results,
                    total_tests=len(scope_results),
                    tests_passed=passed,
                    tests_failed=failed,
                    tests_errored=errored,
                    tests_skipped=skipped,
                    success_rate=(executed - errored) / max(executed, 1),
                    pass_rate=passed / max(executed - errored, 1) if executed > errored else 0.0,
                    avg_latency_ms=cr.avg_latency_ms,
                    p95_latency_ms=cr.p95_latency_ms,
                    total_cost_usd=cr.total_cost_usd,
                    evaluation_cost_usd=0.0,
                    execution_duration_ms=0.0,
                )
                scope_candidate_results.append(scope_cr)
                scope_test_count += len(scope_results)

        if scope_candidate_results:
            scope_runs.append(ScopeTestRun(
                scope_id=scope_id,
                scope_role=step_roles[scope_id],
                candidate_results=sorted(
                    scope_candidate_results,
                    key=lambda r: -(r.pass_rate or 0)
                ),
                test_case_count=scope_test_count,
                evaluation_mode=_evaluation_mode_for_scope(scope_id, test_plan),
            ))

    return scope_runs


def dedup_tools_for_build(
    scope_selections: dict[str, list[str]],
) -> list[str]:
    """
    Collect unique tool names across all scopes for harness building.
    A candidate covering M scopes gets ONE build, not M.
    Returns deduplicated list preserving first-seen order.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for candidates in scope_selections.values():
        for name in candidates:
            key = name.strip().lower()
            if key not in seen:
                seen.add(key)
                unique.append(name)
    return unique
