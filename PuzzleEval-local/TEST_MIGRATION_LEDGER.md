# Test Migration Ledger

Per Codex pushback Q2: "Which tests will be deleted because their contract is obsolete, and what behavior test replaces each one?"

This document catalogs every test deleted, migrated, or replaced during the architecture cleanup, with a per-test explanation of why and what (if anything) replaces it. The principle: a test is an asset only if it pins a contract that still matters.

## Categories

- **MIGRATED** — test still exists but its source-grep target was updated to point at the new canonical location. Same contract pinned, different file path.
- **STRENGTHENED** — test still exists but its assertions were broadened or replaced with behavior tests that catch a wider class of regressions.
- **DELETED** — test is gone; the contract it pinned no longer exists.
- **REPLACED** — test is gone; a new behavior test pins the same contract from a different angle.

## Phase 0 (telemetry consolidation + contracts foundation)

| Status | Test | Reason | Replacement |
|---|---|---|---|
| MIGRATED | `tests/test_prod_audit_fixes.py::TestAdvisorModelFallbackNotHardcoded::test_advisor_model_defaults_to_executor_model` | Source-grep target was `puzzleeval/agents/agent5/costing.py`; that file became a shim | Same test now reads `puzzleeval/telemetry/cost.py` (the canonical home) |
| ADDED | `tests/test_telemetry.py` (27 cases) | Pin the new public API of `puzzleeval.telemetry` package | New behavior tests for cost calculation, pricing lookup, timing, budget, logging, back-compat |
| ADDED | `tests/test_contract_loader.py` (~30 cases) | Pin frontmatter parsing + Pydantic schema validation + auto-discovery | Property tests for the loader |
| ADDED | `tests/test_contract_selection_properties.py` (~20 cases) | Pin the 4 selection mechanism guarantees (Soundness, Completeness, Consistency, Determinism) | Property tests run against generated TaskContexts |
| ADDED | `tests/test_runtime_gates.py` (~16 cases) | Pin AD-007: gates are Python; markdown is descriptive only | Behavior test of VoiceHarnessGate + registry pattern |
| ADDED | `tests/test_wheel_packaging.py` (~10 cases) | CI guard: contracts ship in installed wheel, not just source tree | Reads each contract via `importlib.resources` (editable install). Wheel-build subclass added in Phase-2 hardening. |
| ADDED | `tests/test_pricing_table_completeness.py` (~5 cases) | CI guard: every claude-* model name in source has a MODEL_PRICING entry | Scans codebase, asserts table coverage |

## Phase 1.A (inline contract migration)

| Status | Test | Reason | Replacement |
|---|---|---|---|
| MIGRATED | `tests/test_prod_audit_fixes.py::TestEfficiencyHardening_RealRun_d3b49875::test_builder_prompt_has_windows_command_translation_table` | Source-grep target was `puzzleeval/agents/implement_test_env.py`; the Windows rules content moved to `capability_playbooks/platform_windows.md` | Same test now reads from the markdown file. Behavior contract preserved (Windows rules still required, still contain "findstr"). |

## Phase 1.B + 1.C (placeholder collapse + unified selector)

| Status | Test | Reason | Replacement |
|---|---|---|---|
| STRENGTHENED | `tests/test_agent5_architecture_cleanup.py::test_agent5_builder_prompt_template_is_loaded_from_package` | Originally asserted `__MODALITY_CONTRACT__` placeholder existed | Now asserts the unified `__CONTRACT_BLOCK__` placeholder exists AND the legacy split placeholders (`__MODALITY_CONTRACT__`, `__OS_SPECIFIC_RULES__`) are absent. Strictly broader contract. |
| STRENGTHENED | `tests/test_prod_audit_fixes.py::TestEfficiencyHardening_RealRun_d3b49875::test_os_specific_rules_are_conditional_not_unconditional` | Asserted dual-placeholder layout via `_render_builder_prompt_for_os` | Asserts unified `__CONTRACT_BLOCK__` via `_render_builder_prompt`. Same behavior contract (Windows runs include Windows guidance; Linux runs don't). |
| STRENGTHENED | `tests/test_prod_audit_fixes.py::TestEfficiencyHardening_RealRun_d3b49875::test_os_specific_rules_unknown_platform_safe` | Same as above | Same as above |
| STRENGTHENED | `tests/test_prod_audit_fixes.py::TestConversationSummaryTelemetry::test_streaming_contract_routes_through_unified_dispatcher` | Asserted `_modality_specific_contract` was the unified dispatcher | Asserts `_render_builder_prompt` calls `compose_contract_block(task)` (the new canonical selector). Plus negative-space asserts (no `__MODALITY_CONTRACT__`, no `__OS_SPECIFIC_RULES__`, no `__STREAMING_RESPONSE_CONTRACT__`). |

## Phase 2 (sandbox extraction)

| Status | Test | Reason | Replacement |
|---|---|---|---|
| MIGRATED | `tests/test_venv_precreate.py::TestPreCreateOnlyRunsForSelectedCandidates::*` (4 tests) | `mock.patch` target was `puzzleeval.agents.implement_test_env._create_venv`; that's now a shim. Mocking it doesn't intercept calls from `agent5/sandbox.py::precreate_venvs_for_candidates`. | Patch target updated to `puzzleeval.agents.agent5.sandbox.create_venv` (canonical). |
| MIGRATED | `tests/test_venv_precreate.py::TestPreCreateRunsInParallel::*` (3 tests) | Same as above | Same as above |
| MIGRATED | `tests/test_venv_precreate.py::TestCreateVenvShortCircuits::*` (3 tests) | Same as above | Same as above |
| MIGRATED | `tests/test_prod_audit_fixes.py::TestEfficiencyHardening_RealRun_d3b49875::test_venv_preinstall_toggleable_via_env` | Source-grep target was `implement_test_env.py`; venv code moved to `sandbox.py` | Same test now reads `puzzleeval/agents/agent5/sandbox.py` |
| ADDED | `tests/test_venv_precreate.py::TestVenvLockDictSingletonAcrossModules` (4 tests) | NEW R2 invariant: lock dict identity preserved across `implement_test_env` and `agent5.sandbox` | New behavior tests guard against accidental redeclaration of `_VENV_CREATE_LOCKS` |

## Pushback A response (criticality field)

| Status | Test | Reason | Replacement |
|---|---|---|---|
| ADDED | `tests/test_coverage_completeness.py::TestEnumToCoverageCompleteness` (3 tests) | NEW: every modality enum value must be classified (CoverageRequirement OR allowlisted as no-coverage). Catches "developer added a new enum value but forgot to update CoverageRequirement". | New behavior test. Closes the silent-degradation gap Codex identified in pushback B. |
| ADDED | `tests/test_coverage_completeness.py::TestCriticalityInvariant` (3 tests) | NEW: required contracts cannot use `selection_mode=llm_routed`. Pydantic validator + Layer 3 filter both enforce this. Belt-and-braces for pushback A. | New behavior test |
| ADDED | `tests/test_coverage_completeness.py::TestNarrowedCorrectnessClaim` (1 test) | Make the narrowed correctness claim explicit: "for tasks matching at least one CoverageRequirement, Layer 4 reports gaps". The OLD claim was overstated. | New behavior test |

## Tests planned for Phase 5+ (NOT YET TOUCHED)

These are projected DELETIONS for the Phase 5 (build_loop move) work. Listed here so the deletion isn't a surprise — each is a Category-B source-grep test pinning implementation details that the move will invalidate.

| Status | Test | Reason | Replacement |
|---|---|---|---|
| (planned DELETE in Phase 5) | `tests/test_prod_audit_fixes.py::TestNoContainerThreadingAfterRevert` | Pins specific source patterns from a 2026-04-19 incident-revert that no longer relevant after the build_loop refactor (no container threading is structurally possible) | Behavior tests in `test_build_loop_behavior.py` (added Phase 4) cover the equivalent contract: builder calls don't leak container_id. |
| (planned MIGRATE in Phase 5) | `tests/test_prod_audit_fixes.py:3705-4609` (12+ uses of `inspect.getsource(ite._build_single_harness)`) | After the move, `_build_single_harness` is in `agent5/build_loop.py`, not `implement_test_env.py` | Update each `inspect.getsource` target. Where the asserted string lives in a sub-helper extracted in Phase 4, retarget to that helper. |
| (planned DELETE in Phase 5) | Tests asserting specific line numbers in `implement_test_env.py` | Implementation-detail pins, not behavior contracts | (none — pure deletion) |

## Statistics

- **Phase 0 + 1 + 2 cumulative:** ~14 tests migrated (path updates), ~4 strengthened (broader contracts), ~91 added (new behavior tests + property tests + R2 + criticality invariant), 0 deleted
- **Net:** +109 tests, all behavior-contract tests with low source-coupling
- **Phase 5+ projection:** ~10-15 tests will need migration (path updates), ~3-5 will be deleted (pure implementation pins), several behavior tests will be added in their place

## Open question for the user

Codex's framework: source-grep tests are debt; behavior tests are the asset. The tests added in Phases 0+1+2 lean BEHAVIOR TEST not source-grep. The remaining source-grep tests in `test_prod_audit_fixes.py` are the natural debt to retire in Phase 5+ when the corresponding code moves. None of the existing source-grep tests have been deleted purely to "clean up" — every change has been driven by an actual code move.
