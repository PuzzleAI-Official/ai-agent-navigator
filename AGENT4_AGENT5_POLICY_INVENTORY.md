# Agent 4/5 Policy Inventory

Date: 2026-05-06

## Phase 0 Baseline

`git status --short` shows an already-dirty worktree. Notable existing changes before this inventory:

- Modified Agent 4/5 runtime files, including `puzzleeval/agents/agent4/core.py`, `puzzleeval/agents/agent4/templates/verification_system.md`, `puzzleeval/agents/agent5/build_loop.py`, `puzzleeval/agents/agent5/templates/builder_system_prompt.md`, `puzzleeval/agents/implement_test_env.py`, `puzzleeval/config.py`, multiple tool plugins, reports, validators, and frontend files.
- Untracked recovery-plan-adjacent files, including `AGENT4_AGENT5_ARCHITECTURE_RECOVERY_PLAN.md`, `puzzleeval/docs_resolver.py`, `puzzleeval/research_handoff.py`, `puzzleeval/agents/agent5/failure_packets.py`, `puzzleeval/agents/agent5/research_memory.py`, and several new tests.
- Deleted `.claude/scheduled_tasks.lock`.
- Git emitted warnings reading `C:\Users\Deanh/.config/git/ignore`; no destructive cleanup was performed.

Recent run/baseline evidence available:

- `real_run_state.json`: completed run `4c0b57ae`, trace `749b09b1-49ed-4fa2-bc4d-d9e633ea876f`, total cost `8.776767`.
- `run_pointers.txt`: active trace points to `puzzleeval-api/runs/749b09b1-49ed-4fa2-bc4d-d9e633ea876f`.
- `PuzzleEval-local/baselines/2026-04-29/REAL_RUN_OBSERVATIONS.md`: run `6e0c9563`, trace `97b0427c-e6da-45dd-a9a8-5d36e077e145`; Agent 4 verified three candidates, Agent 5 built two, and voice builds leaned on `ask_research`.
- `PuzzleEval-local/baselines/2026-04-29/PHASE2_BASELINE.md`: Agent 4 verification prompt baseline is 12,695 chars / 3,173 tokens / 206 lines; structure prompt baseline is 3,396 chars / 849 tokens / 56 lines.
- Capability playbook size baseline: `voice.md` 151 lines, `streaming_response.md` 141 lines, `live_test_voice.md` 253 lines.

## Files Inspected

- `AGENT4_AGENT5_ARCHITECTURE_RECOVERY_PLAN.md`
- `PuzzleEval-local/puzzleeval/agents/agent4/core.py`
- `PuzzleEval-local/puzzleeval/agents/agent4/templates/verification_system.md`
- `PuzzleEval-local/puzzleeval/agents/agent4/templates/structure_system.md`
- `PuzzleEval-local/puzzleeval/agents/agent5/build_loop.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/dispatch_helpers.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/initial_message.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/autonomy_directives.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/sandbox.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/tools.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/runtime_state.py`
- `PuzzleEval-local/puzzleeval/agents/agent5/verification.py`
- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py`
- `PuzzleEval-local/puzzleeval/research_handoff.py`
- `PuzzleEval-local/puzzleeval/docs_resolver.py`
- `PuzzleEval-local/puzzleeval/tool_plugins/__init__.py`
- `PuzzleEval-local/puzzleeval/tool_plugins/voice_realtime.py`
- `PuzzleEval-local/puzzleeval/config.py`
- Relevant tests under `PuzzleEval-local/tests/`, especially Agent 4 handoff, Agent 5 cleanup, phase gates, voice contracts, completion gates, failure packets, runtime/report truth, and frontend audio/progress tests.

## Current Active Agent 4 Workflow

Agent 4 still acts as a verifier that also emits build-readiness policy.

1. `run_screening_agent()` loads verification and structure prompts from `agent4/templates/`.
2. `_verify_single_candidate()` lets the verifier use `web_fetch`, `web_search`, and `complete_screening`; fetched docs are cached into each candidate sandbox.
3. `verification_system.md` requires every PASS to include a fenced `BUILD_READINESS_CHECKLIST` JSON block with ten fields and four non-negotiable build fields.
4. `_extract_checklist_from_findings()` parses that fenced checklist. Missing or malformed checklists become a sentinel `BuildReadinessChecklist.default_unknown_checklist(...)`.
5. `structure_system.md` instructs the structurer to copy the checklist into `ScreenedCandidate.checklist`.
6. `_attach_checklists_to_result()` overwrites structured candidate checklists from verifier findings and logs verified/sentinel counts.
7. For each validated candidate, `write_research_handoff()` writes `_agent_state/research_handoff.json`. That handoff is still synthesized from `BuildReadinessChecklist`.

Current effect: Agent 4 owns docs verification, but it also owns a detailed build-readiness checklist that is consumed as Agent 5 build input. That conflicts with the recovery plan.

## Current Active Agent 5 Workflow

Agent 5 is split between research, build, and several older phase-control paths.

1. `_setup_sandbox_and_credentials()` stages orchestrator artifacts and may pre-render `api_spec.txt` from `BuildReadinessChecklist` via `_synthesize_api_spec_from_checklist()`.
2. `write_research_handoff()` also stages `_agent_state/research_handoff.json` for direct Agent 5 runs.
3. `BuildLoopState.api_spec_written` drives the phase name, model switch, and scaffold permission gates.
4. `api_spec.txt` writes or patches trigger `detect_phase_transition()` and can inject `PHASE2_DIRECTIVE`.
5. `ask_research` is refused before `api_spec.txt` exists, so research help is subordinate to the old API-spec phase gate.
6. `objective.md` and runtime artifacts are orchestrator-owned through `agent5.tools.ORCHESTRATOR_OWNED_ARTIFACTS`.
7. `build_plan.md` and `agent_observations.json` remain writable optional scratch/context artifacts.
8. Completion is declared by `HARNESS_COMPLETE`, then checked by completion gates, forensics, reflection evidence, voice live-test contract checks, and failure packet writing.
9. Runtime state snapshots still report phase through `api_spec_written`.

Current effect: Agent 5 does own much of the build/debug loop, but `api_spec.txt`, checklist pre-rendering, and phase-specific directives still own important control flow.

## Policy Ownership Map

| Policy / artifact | Current owner | Target owner | Classification | Notes |
| --- | --- | --- | --- | --- |
| `objective.md` | Orchestrator / Agent 5 sandbox staging | Orchestrator | Keep | Already write-protected in `agent5.tools`; Agent 5 can read but not author or weaken it. |
| Agent 4 docs verification | Agent 4 | Agent 4 | Keep/rewrite | Keep verifier role, but narrow output to docs entrypoint and lightweight metadata. |
| `BuildReadinessChecklist` | Agent 4 verifier prompt, schema, handoff, pre-render | Report/debug telemetry only | Demote | Phase 1 should stop using it as primary build truth while preserving compatibility. |
| `_agent_state/research_handoff.json` | Agent 4 and Agent 5 setup | Agent 5 research input compatibility | Rewrite/shim | Useful as compact index now, but it is still checklist-derived. It should become fallback or be fed by docs entrypoint. |
| `_agent_state/docs_entrypoint.json` | Absent | Agent 4/orchestrator-authored docs verifier artifact | Add/keep | Phase 1 replacement artifact. Agent 5 reads it first. |
| `api_spec.txt` | Agent 5 phase gate, model transition, scaffold unlock | Agent 5 optional evidence/helper until implementation plan replaces it | Demote/rewrite | Phase 4 target. Stop new dependencies before deleting authority. |
| `api_spec_written` | Agent 5 state machine | Implementation-plan gate | Rewrite | Active in build loop, dispatch helpers, runtime state, and many tests. |
| `PHASE2_DIRECTIVE` | Agent 5 build loop | None | Delete | It is an implementation recipe after API-spec transition. |
| `ask_research` | Agent 5 tool gated by `api_spec.txt` | Agent 5 research/debug tool | Rewrite | Phase 3 should unblock planned research and prevent endless delegation. |
| `build_plan.md` | Agent 5 optional writable scratch/directive path | None or telemetry only | Demote/delete | Build planning should move to `implementation_plan.json`; current disabled directive still preserves stale architecture. |
| `agent_observations.json` | Agent 5 optional writable scratch | None | Delete | Artifact does not drive acceptance/user evidence enough to justify ongoing surface. |
| `implementation_plan.json` | Absent | Agent 5 | Add/keep | Phase 4 target: research strategy and implementation plan should own build selection. |
| `harness_execution_mode` | Tool plugins and runtime dispatch | Capability metadata using only approved primitives | Rewrite | Current values include `multi_turn_serialized` and `multi_turn_persistent`; target primitives are only `single_call` and `persistent_worker`. |
| `persistent_worker` | Absent as canonical primitive | Agent 5 runtime/capability planner | Add/keep | Current persistent runner behavior exists but is named and routed through stale plugin modes. |
| Voice/live/streaming playbooks | Capability playbook markdown | Outcome contracts only | Rewrite | Current playbooks still include implementation recipes and test script instructions. |
| `ALLOWED_EXTENSIONS` | `agent5.tools`, shimmed in `implement_test_env.py` | `agent5.tools` canonical | Keep/shim | Current canonical location is acceptable; compatibility shim can remain during cleanup. |
| `HARNESS_COMPLETE` | Agent 5 completion signal plus gates | Agent 5 outcome evidence signal | Keep/rewrite | Keep as final signal, but gates should verify outcomes/safety rather than old implementation recipe compliance. |
| Completion gate | Build loop, verification, validators | Outcome/safety validators | Keep/rewrite | Voice contract is partly outcome-oriented but still prescriptive around persistent runner details. |
| `failure_packets` | New untracked Agent 5 module and build-loop hooks | Agent 5 debug/evidence loop | Keep/expand | Promising Phase 6 direction; keep structured failure packets from becoming another planning source. |
| `runtime_state` | Agent 5 runtime phase and snapshot | Orchestrator/Agent 5 telemetry | Keep/rewrite | Needs migration flags and later removal of `api_spec_written` phase authority. |

## Stale Or Conflicting Policy Inventory

- `BuildReadinessChecklist`: demote in Phase 1. It is currently prompt-required, schema-backed, copied into candidates, and used to synthesize `api_spec.txt`.
- `research_handoff`: rewrite/shim. The module says the checklist remains the detailed source of truth, so it conflicts with the docs-entrypoint target.
- `docs_entrypoint`: add in Phase 1. No active code currently writes or reads it.
- `api_spec_written`: rewrite in Phase 4. It still drives build phase, model selection, scaffold permission, ask-research access, and runtime state.
- `api_spec.txt`: demote in Phase 4. It is currently both helper artifact and phase boundary.
- `PHASE2_DIRECTIVE`: delete in Phase 4. It is a hard-coded implementation recipe after API-spec transition.
- `ask_research`: rewrite in Phase 3. It is blocked until `api_spec.txt`, which prevents Agent 5 from owning research strategy.
- `build_plan.md`: demote/delete in Phase 8. It remains staged and optionally directive-bearing.
- `agent_observations.json`: delete in Phase 8. It is optional scratch and adds artifact surface without clear acceptance value.
- `objective.md`: keep. It is correctly orchestrator-owned and write-protected.
- `implementation_plan`: add in Phase 4. It is absent today.
- `harness_execution_mode`: rewrite in Phase 5. Current values are not the target primitive names.
- `persistent_worker`: add in Phase 5. Existing persistent harness support should be re-owned under the canonical primitive.
- Voice/live/streaming playbooks: rewrite in Phase 5. They are too prescriptive and large for outcome-only contracts.
- `ALLOWED_EXTENSIONS`: keep canonical in `agent5.tools`; keep compatibility shim until imports move.
- `HARNESS_COMPLETE`: keep/rewrite. Keep the final completion signal but align gates to outcomes and evidence.
- Completion gate: keep/rewrite. Existing gates are valuable but some still test recipes.
- `failure_packets`: keep/expand. It supports recovery and evidence, but should remain debug telemetry, not a second planner.
- `runtime_state`: keep/rewrite. Add migration flags now; later remove `api_spec_written` phase control.

## Tests Enforcing Stale Behavior

- Agent 4/checklist:
  - `tests/test_agent4_doc_handoff.py` treats checklist as a load-bearing handoff artifact.
  - `tests/test_build_readiness_checklist.py` validates checklist schema and uses it as Agent 4 to Agent 5 handoff.
  - `tests/test_phase2_gates.py` validates checklist non-negotiable gate behavior.
- API-spec phase gates:
  - `tests/test_ask_research_phase_gate.py` asserts `ask_research` is refused before `api_spec.txt`.
  - `tests/test_build_loop_behavior.py` pins `api_spec_written`, `PHASE2_DIRECTIVE`, and model transition behavior.
  - `tests/test_dispatch_helpers.py` pins `api_spec.txt` phase transition and scaffold blocking.
  - `tests/test_agent5_write_file_gates.py` pins Phase 1 scaffold blocking before `api_spec.txt`.
  - `tests/test_autonomy_context_compaction.py` and `tests/test_build_plan_staleness.py` pin build-plan/API-spec staleness behavior.
- Runtime primitives / playbooks:
  - `tests/test_stateful_voice_runtime_contract.py` pins `multi_turn_persistent` and `multi_turn_serialized` modes.
  - `tests/test_prod_audit_fixes.py` has voice/live/streaming contract tests that still assert detailed playbook injection.
- Completion/evidence:
  - `tests/test_completion_gate.py`, `tests/test_failure_packets.py`, `tests/test_report.py`, and runtime/report truth tests enforce current outcome and reporting behavior. These are mostly keep/rewrite, not immediate delete.
- Frontend/audio/progress:
  - Frontend audio/progress tests should be treated as affected only when runtime/report schemas change; Phase 1 should avoid touching them.

## Phase 1 Smallest Safe Slice

Implement only the docs-entrypoint replacement edge and checklist demotion:

1. Add config flag `PUZZLEEVAL_AGENT4_DOCS_ENTRYPOINT_ENABLED`, default enabled.
2. Add `_agent_state/docs_entrypoint.json` as an Agent 4/orchestrator-owned artifact containing docs verdict, primary docs entrypoint, alternate entrypoints, deprecated/blocked URLs, evidence pointers, lightweight auth/access/pricing metadata, capability hints, and confidence.
3. Have Agent 4 write `docs_entrypoint.json` for each validated candidate when the flag is enabled.
4. Have direct Agent 5 setup also stage `docs_entrypoint.json` so tests and bypass paths receive the same artifact.
5. Make Agent 5 initial research input prefer `docs_entrypoint.json` first, with `research_handoff.json` and checklist summary as compatibility context only.
6. Demote checklist-to-`api_spec.txt` pre-render behind the same flag: default enabled docs-entrypoint flow disables pre-render; flag `0` restores degraded legacy checklist-as-build-truth behavior.
7. Add targeted tests for docs-entrypoint synthesis/staging, runtime migration flag visibility, write protection, and pre-render flag behavior.

Do not change `api_spec_written`, `ask_research`, runtime primitive names, playbooks, or completion gates in Phase 1.

## Phase 1 Risks And Rollback Flags

- Main rollback flag: set `PUZZLEEVAL_AGENT4_DOCS_ENTRYPOINT_ENABLED=0` to restore legacy checklist-driven `api_spec.txt` pre-render behavior.
- Risk: docs-entrypoint confidence and access metadata must be synthesized from imperfect existing candidate/checklist fields until Agent 4 prompt/schema is narrowed.
- Risk: existing tests may still assume default checklist pre-render. Phase 1 should update only targeted tests and avoid broad stale gate rewrites.
- Risk: direct Agent 5 build paths bypass Agent 4, so Agent 5 setup must stage the new artifact too.
- Risk: many dirty files pre-exist; all edits must be scoped and avoid reverting unrelated changes.

