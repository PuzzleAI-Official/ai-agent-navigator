# Agent 4/5 Rollback Retirement Plan

## Summary

Yes, we can delete the stale Agent 4/5 rollback surfaces, but it should be done as a deliberate retirement, not a search-and-replace. The current production path is now docs-entrypoint -> research plan/findings/brief/synthesis -> implementation plan -> build/debug/evidence -> representative probe. The old `api_spec.txt` / `BuildReadinessChecklist` / `PHASE2_DIRECTIVE` / Sonnet-to-Opus transition path is no longer the backbone and still adds prompt and code complexity.

This plan removes stale rollback behavior from live orchestration, prompts, comments, and tests while preserving only passive historical report parsing where needed. Passive compatibility means old run artifacts can still be displayed, but no current Agent 4 or Agent 5 decision path can use the old architecture.

## Evidence From Code Inventory

### Live rollback flags still exist

- `PuzzleEval-local/puzzleeval/config.py` still defines `AGENT4_DOCS_ENTRYPOINT_ENABLED`, `IMPLEMENTATION_PLAN_GATE_ENABLED`, `checklist_prerender_enabled()`, and accepts `PUZZLEEVAL_CONTEXT_COMPACTION_AT_MODEL_TRANSITION` as an alias.
- `PuzzleEval-local/puzzleeval/cli.py` and `puzzleeval-api/services/pipeline_runner.py` still pass `docs_entrypoint_enabled=AGENT4_DOCS_ENTRYPOINT_ENABLED` into build-list selection.
- `PuzzleEval-local/puzzleeval/selection.py` can bypass docs-entrypoint filtering when `docs_entrypoint_enabled=False`.

### `api_spec.txt` is still wired as an alternate build boundary

- `PuzzleEval-local/puzzleeval/agents/agent5/build_loop.py` still carries `PHASE2_DIRECTIVE`, `api_spec_written`, old `detect_phase_transition()` fallback wiring, old `api_spec.txt` ask-research refusal text, and event names/reasons such as `api_spec_written`.
- `PuzzleEval-local/puzzleeval/agents/agent5/dispatch_helpers.py` still exports `detect_phase_transition()`, `TRANSITION_FILES_WRITE`, and `PHASE_1_VIOLATION_FILES` for the old spec-triggered phase change.
- `PuzzleEval-local/puzzleeval/agents/agent5/tools.py` still has a branch where scaffold writes are allowed by `api_spec_written` when `IMPLEMENTATION_PLAN_GATE_ENABLED` is disabled.
- `PuzzleEval-local/puzzleeval/agents/agent5/runtime_state.py`, `turn_blocks.py`, and `conversation_log.py` still expose `api_spec_written` or `api_spec.txt` as phase/boundary telemetry.

### Checklist pre-render path still exists

- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` still defines `_synthesize_api_spec_from_checklist()` and `_checklist_prerender_enabled()`, writes `api_spec.txt` when the old rollback flag is active, and includes rollback helper instructions in the initial Agent 5 message.
- `PuzzleEval-local/puzzleeval/agents/agent4/templates/verification_system.md` still asks Agent 4 to emit a full `BUILD_READINESS_CHECKLIST`.
- `PuzzleEval-local/puzzleeval/agents/agent4/templates/structure_system.md` still requires the structuring agent to populate `ScreenedCandidate.checklist`.
- `PuzzleEval-local/puzzleeval/agents/agent4/core.py` still parses and attaches `BuildReadinessChecklist` objects after screening.
- `PuzzleEval-local/puzzleeval/schemas.py` still defines `BuildReadinessChecklist`, `FieldStatus`, `EndpointSummary`, `default_unknown_checklist()`, and `ScreenedCandidate.checklist`.

### Prompts and docs still mention stale policy

- `PuzzleEval-local/puzzleeval/agents/agent5/templates/builder_system_prompt.md` still mentions optional `api_spec.txt`, degraded rollback helper notes, and Agent 4 checklist hints.
- `PuzzleEval-local/puzzleeval/agents/agent5/initial_message.py` still formats and surfaces checklist compatibility telemetry.
- `PuzzleEval-local/CLAUDE.md`, `PuzzleEval-local/ARCHITECTURE.md`, `PuzzleEval-local/puzzleeval/agents/README_AGENT5.md`, and `puzzleeval-api/BACKEND_ARCHITECTURE.md` still document the stale path or historical model-transition language.

### Tests still enforce stale behavior

Representative stale tests include:

- `PuzzleEval-local/tests/test_749b09b1_reproduction.py`
- `PuzzleEval-local/tests/test_build_plan_staleness.py`
- `PuzzleEval-local/tests/test_agent5_write_file_gates.py`
- `PuzzleEval-local/tests/test_dispatch_helpers.py`
- `PuzzleEval-local/tests/test_research_compaction.py`
- `PuzzleEval-local/tests/test_build_readiness_checklist.py`
- `PuzzleEval-local/tests/test_agent4_doc_handoff.py`
- Old sections inside `test_build_loop_behavior.py`, `test_prod_audit_fixes.py`, `test_agent5_architecture_recovery.py`, `test_agent5_architecture_cleanup.py`, `test_autonomy_runtime_state.py`, `test_turn_blocks.py`, and `test_phase1_research_upgrades.py`.

## Retirement Policy

### Delete from active architecture

- `api_spec.txt` as a build boundary, readiness artifact, transition trigger, or prompt instruction.
- `PHASE2_DIRECTIVE`.
- `api_spec_written` as a phase-control variable.
- `IMPLEMENTATION_PLAN_GATE_ENABLED` and the flag-off branch that restores the old spec boundary.
- `AGENT4_DOCS_ENTRYPOINT_ENABLED` and the flag-off branch that restores checklist pre-render.
- `checklist_prerender_enabled()` and `_checklist_prerender_enabled()`.
- `_synthesize_api_spec_from_checklist()`.
- `CONTEXT_COMPACTION_AT_MODEL_TRANSITION` alias and all Sonnet-to-Opus/model-transition wording.
- Agent 5 prompt surfaces that tell the builder to read or consult `api_spec.txt` or checklist hints.

### Keep, but not as rollback architecture

- `RESEARCH_WORKERS_ENABLED`: keep only as an operational concurrency toggle. If disabled, it must not re-enable `api_spec.txt`; it should fall back to single-threaded/scoped research behavior.
- `PERSISTENT_WORKER_RUNTIME_ENABLED`: keep as runtime execution toggle, not an old architecture.
- `REPRESENTATIVE_PROBE_GATE_ENABLED`: keep as production-equivalence evidence gate.
- Failure packet and LLM review flags: keep as observability/diagnostic controls.
- `api_spec_path` for OpenAPI fastpath only when it points to a real JSON OpenAPI spec, not `api_spec.txt`.

### Passive historical compatibility

Preserve passive report parsing only where deleting it would make old run artifacts unreadable. Passive compatibility must be isolated from Agent 4/5 prompts and live orchestration. It may appear in report-only code with names like `legacy_report_api_spec`, but not in build-loop gates, tool guidance, runtime phase selection, or candidate selection.

## Implementation Plan

### Slice 1: Remove `api_spec.txt` build-boundary logic

Files:

- `PuzzleEval-local/puzzleeval/config.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/build_loop.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/dispatch_helpers.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/tools.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/runtime_state.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/turn_blocks.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/conversation_log.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/autonomy_directives.py`

Changes:

- Make implementation-plan acceptance the unconditional build gate.
- Rename loop state from `api_spec_written` to `build_gate_accepted` or use `implementation_plan_accepted` directly.
- Delete `PHASE2_DIRECTIVE`; keep only the implementation-plan directive and build-gate artifact compaction.
- Delete `detect_phase_transition()` and old spec transition exports.
- Change scaffold gating to accept only `implementation_plan_accepted`.
- Remove `api_spec.txt` from tracked runtime files and build-state summaries.
- Change build-plan staleness triggers from `api_spec.txt written` to `implementation_plan accepted`.
- Remove ask-research fallback text that mentions `api_spec.txt`.

Acceptance:

- `rg "PHASE2_DIRECTIVE|api_spec_written|api_spec\\.txt written|CONTEXT_COMPACTION_AT_MODEL_TRANSITION" PuzzleEval-local/puzzleeval` returns no live-code hits, excluding historical docs only if intentionally archived.
- Writing or patching `api_spec.txt` does not transition the build loop and does not unblock scaffold writes.

### Slice 2: Remove checklist pre-render and Agent 4 checklist prompting

Files:

- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py`
- `PuzzleEval-local/puzzleeval/agents/agent4/core.py`
- `PuzzleEval-local/puzzleeval/agents/agent4/templates/verification_system.md`
- `PuzzleEval-local/puzzleeval/agents/agent4/templates/structure_system.md`
- `PuzzleEval-local/puzzleeval/docs_entrypoint.py`
- `PuzzleEval-local/puzzleeval/research_handoff.py`
- `PuzzleEval-local/puzzleeval/docs_resolver.py`
- `PuzzleEval-local/puzzleeval/validators.py`
- `PuzzleEval-local/puzzleeval/schemas.py`

Changes:

- Remove `_synthesize_api_spec_from_checklist()` and all setup-time `api_spec.txt` writes.
- Remove `BUILD_READINESS_CHECKLIST` from Agent 4 verification and structuring prompts.
- Remove deterministic checklist parsing/attachment from Agent 4.
- Make docs-entrypoint synthesis rely on current `ScreenedCandidate` fields, prefetched docs, and verified docs URL, not checklist fields.
- Make research handoff rely on docs-entrypoint, prefetched docs, candidate metadata, and confirmed capability notes, not checklist fields.
- Remove `validate_checklist_for_verified_pass()` and its config flag if it is no longer called.
- Remove `BuildReadinessChecklist`, `default_unknown_checklist()`, and `ScreenedCandidate.checklist` from live schemas unless a report-only compatibility model is needed.

Acceptance:

- Agent 4 prompt asks only for docs-entrypoint evidence and lightweight metadata.
- Agent 4 output schema no longer forces a deep checklist object.
- Agent 5 initial context contains no checklist block.
- Docs-entrypoint and research handoff still populate from verified docs URL, fetched docs, auth/access/pricing metadata, and capability hints.

### Slice 3: Clean Agent 5 prompts and initial context

Files:

- `PuzzleEval-local/puzzleeval/agents/agent5/templates/builder_system_prompt.md`
- `PuzzleEval-local/puzzleeval/agents/agent5/initial_message.py`
- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/context_compaction.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/research_subagent.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/dispatch_helpers.py`

Changes:

- Replace checklist/api-spec guidance with this current path:
  `objective.md`, `docs_entrypoint.json`, `test_case_manifest.json`, `research_plan.json`, `research_findings`, `research_build_brief.json`, `research_synthesis.json`, `implementation_plan.json`, runtime/failure evidence, representative probe evidence.
- Remove default-path references to degraded rollback, model transition, first-turn Sonnet, and `api_spec.txt`.
- Keep prompt policy outcome-oriented: evidence quality, scoped research gaps, vertical slice, representative production-equivalence proof.

Acceptance:

- `rg "api_spec\\.txt|BuildReadinessChecklist|degraded rollback|legacy rollback|Sonnet.?→.?Opus|model transition|first turn Sonnet" PuzzleEval-local/puzzleeval/agents/agent5` returns no default prompt or context hits.

### Slice 4: Make candidate/docs selection unconditional

Files:

- `PuzzleEval-local/puzzleeval/selection.py`
- `PuzzleEval-local/puzzleeval/cli.py`
- `puzzleeval-api/services/pipeline_runner.py`
- `puzzleeval-api/tests/test_agent5_runner_contracts.py`
- Relevant selection tests.

Changes:

- Remove `docs_entrypoint_enabled` from `select_agent5_build_candidates()`.
- Always require `docs_entrypoint_allows_automatic_build()`.
- Always stage docs-entrypoint before Agent 5 setup.
- Remove `AGENT4_DOCS_ENTRYPOINT_ENABLED` imports and migration flag snapshot entry.

Acceptance:

- Agent 5 receives only orchestrator-selected, docs-ready, credential-ready candidates.
- There is no flag that lets Agent 5 build candidates missing docs-entrypoint approval.

### Slice 5: Rewrite stale tests instead of carrying old behavior

Delete or rewrite tests that enforce stale policy:

- Delete `test_build_readiness_checklist.py`.
- Rewrite `test_agent4_doc_handoff.py` around docs-entrypoint and lightweight metadata.
- Rewrite `test_749b09b1_reproduction.py` around implementation-plan build-gate compaction, not api-spec transition.
- Rewrite `test_build_plan_staleness.py` triggers to implementation-plan accepted, scaffold write, smoke pass, completion signal.
- Rewrite `test_agent5_write_file_gates.py` so `api_spec.txt` is not a special allowed phase-1 write.
- Rewrite `test_dispatch_helpers.py` to remove spec transition tests and cover scaffold gate helpers.
- Rewrite compaction/runtime/turn-block tests to use build-gate terminology.
- Update architecture cleanup/recovery tests to assert no rollback path remains.

Add static cleanup tests:

- No default prompts mention `api_spec.txt`, `BuildReadinessChecklist`, `PHASE2_DIRECTIVE`, Sonnet-to-Opus, model transition, or degraded rollback.
- No live source defines `IMPLEMENTATION_PLAN_GATE_ENABLED`, `AGENT4_DOCS_ENTRYPOINT_ENABLED`, `checklist_prerender_enabled`, or `_synthesize_api_spec_from_checklist`.
- Any remaining `api_spec_path` references must be OpenAPI JSON fastpath or report-only compatibility.

### Slice 6: Clean docs/comments

Files:

- `PuzzleEval-local/ARCHITECTURE.md`
- `PuzzleEval-local/CLAUDE.md`
- `PuzzleEval-local/puzzleeval/agents/README_AGENT5.md`
- `puzzleeval-api/BACKEND_ARCHITECTURE.md`
- Source comments found by stale-policy grep.

Changes:

- Replace old workflow diagrams with current workflow.
- Remove migration/rollback tables for retired flags.
- Keep only current operational toggles and evidence gates.

Acceptance:

- Source and docs describe one architecture, not two.

## Current Architecture After Retirement

1. Agent 4 verifies that each candidate has a plausible official docs entrypoint and lightweight auth/access/pricing metadata.
2. The orchestrator writes `docs_entrypoint.json` and selects Agent 5 build candidates deterministically.
3. Agent 5 starts as Opus lead, reads orchestrator-owned objective/test manifest/docs-entrypoint state, and plans scoped research.
4. Research workers answer scoped gaps; findings are indexed into `research_build_brief.json`.
5. Agent 5 writes `research_synthesis.json` and accepted `implementation_plan.json`.
6. Build-gate artifact compaction grounds context in durable artifacts.
7. Agent 5 writes independent scaffold files in parallel when possible, runs smoke/live/self-checks, debugs from failure packets and forensics, and calls scoped research again only for durable knowledge gaps.
8. The orchestrator runs representative production-equivalence probes through the same evaluator/plugin adapter used by final evaluation.
9. `HARNESS_COMPLETE` is accepted only when smoke, truthful live/self-check status, representative probe evidence or external-block evidence, and reflection evidence are satisfied.

## Risks

- Removing `ScreenedCandidate.checklist` is a schema-breaking change for old run JSONs if any loader uses strict validation. Prefer report-only compatibility if historical replay matters.
- Many tests currently encode old phase names. The cleanup should rewrite behavior tests, not simply delete coverage.
- Some occurrences of `api_spec` are legitimate OpenAPI terminology. Do not remove `openapi_harness.fetch_openapi_spec()` or `ScreenedCandidate.api_spec_path` if it points to external OpenAPI JSON and not Agent 5's old `api_spec.txt`.
- `RESEARCH_WORKERS_ENABLED=0` must not resurrect the old architecture. Its disabled behavior should degrade only research parallelism, not build ownership.

## Recommended Execution Order

1. Implement Slice 1 and run Agent 5 gate/compaction/runtime tests.
2. Implement Slice 2 and run Agent 4 docs-entrypoint/selection tests.
3. Implement Slice 3 and run prompt/static stale-policy tests.
4. Implement Slice 4 and run CLI/API selection tests.
5. Implement Slice 5 and run architecture recovery/cleanup tests.
6. Implement Slice 6 and run full stale-policy grep.
7. Run targeted suites:
   - `python -m pytest tests/test_agent5_architecture_cleanup.py tests/test_agent5_architecture_recovery.py tests/test_agent5_write_file_gates.py tests/test_dispatch_helpers.py tests/test_autonomy_runtime_state.py tests/test_turn_blocks.py -q`
   - `python -m pytest tests/test_agent4_doc_handoff.py tests/test_generalist_probe_efficiency_loop.py tests/test_build_loop_behavior.py -q`
   - `python -m pytest tests/test_ask_research_phase_gate.py tests/test_research_planner_phase3.py tests/test_runtime_primitives_phase5.py tests/test_phase6_debug_loop.py -q`
8. Run full Python tests from `PuzzleEval-local` if targeted suites pass.

