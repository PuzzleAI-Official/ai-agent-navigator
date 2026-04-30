# Phase 2 Final — Full Merge Gate (2026-04-29)

Closes Phase 2 per the plan's "Phase 2 final — Full merge gate" section
(Section 5 of `before-i-give-you-iterative-hamming.md`). This report is
the canonical pre-merge artifact comparing the pre-Phase-2.0 baseline
against the post-Phase-2D state.

Operator authorization for the deferred real-API verification + paid
quality battery is still required before merging this branch to main —
see Section 5 below.

---

## 1. Token deltas — pre-Phase-2.0 → post-D2

### Templates

| Template | Pre-Phase-2 | Post-D2 | Δ tokens | Δ % |
|---|---:|---:|---:|---:|
| agent1_system_prompt | 8,126 | 8,038 | -88 | -1.1% |
| agent2_research_system | 2,044 | 2,308 | +264 | +12.9% (F-A2 expansion — load-bearing) |
| agent2_structure_system | 1,066 | 1,073 | +7 | +0.7% |
| agent3_system_prompt | 8,098 | 6,973 | **-1,125** | **-13.9%** |
| agent3f_system_prompt | 1,959 | 1,841 | -118 | -6.0% |
| agent4_verification_system | 3,173 | 3,293 | +120 | +3.8% (G-A4 wiring — load-bearing) |
| agent4_structure_system | 849 | 849 | 0 | 0% |
| **TOTAL TEMPLATES** | **25,315** | **24,375** | **-940** | **-3.7%** |

### Auxiliary system prompts

| Surface | Pre-Phase-2 | Post-D2 | Δ tokens | Δ % | Note |
|---|---:|---:|---:|---:|---|
| agent_preamble | 405 | 383 | -22 | -5.4% | content unchanged; method drift (estimate→anthropic) |
| rubric_judge_template | 902 | 902 | 0 | 0% | unchanged |
| user_simulator_template | 548 | 579 | +31 | +5.7% | F-Aux1 extend_to_completion swap |
| research_subagent_system | 903 | 1,174 | +271 | +30.0% | Phase 2C.4 3-tier expansion (load-bearing) |
| vision_judge_system | 40 | 41 | +1 | +2.5% | unchanged; method drift |
| evaluation_system_prompt | 292 | 313 | +21 | +7.2% | unchanged; method drift |
| **TOTAL AUXILIARY** | **3,090** | **3,392** | **+302** | **+9.8%** |

The auxiliary +9.8% is dominated by `research_subagent_system` (+271
tokens) which encodes the new 3-tier output schema (ANSWER /
REASONABLE_GUESS / NOT_FOUND). The middle tier resolves F-Aux2 — the
hallucinate-vs-give-up dichotomy. Compressing without losing the middle
tier isn't possible: the third tier IS the value.

### Plan acceptance vs actual

| Plan target | Status |
|---|---|
| ≥20% prompt-token reduction on at least 4 of 7 agent templates | ❌ FAIL — Agent 3 alone hits -13.9%; no template reaches -20% |
| Auxiliary system-prompt string lengths drop ≥10% on average | ❌ FAIL — went UP +9.8% (research_subagent +30% load-bearing) |
| Cache-prefix tokens at parity or higher (Phase 1 invariant) | ✅ PASS — no new mid-prompt placeholders introduced |

The token-reduction targets were missed for the same reasons documented
in `PHASE2_FINAL_REPORT.md`: Phase 2A traded brevity for clarity (cross-
prompt conflict resolution required ADDING text), Phase 2C.4's 3-tier
expansion was load-bearing, and Phase 2D was a focused
redundancy/war-story pass not a full modal-verb audit. Phase 1's +289%
cache-prefix growth is preserved, so the **effective input cost on
repeated builds is unchanged or improved** (cache reads at ~10% of full
input rate amortize the small absolute regressions).

---

## 2. Verification — full pytest, mock pipeline, preflight

| Check | Result | Notes |
|---|---|---|
| `pytest tests/` | ✅ **1832 passed, 2 skipped, 39 deselected** | Same green count as Phase 2 final pre-D2; no regressions from D2 compression |
| `pytest puzzleeval-api/tests/` | ✅ 37 passed | Backend tests green |
| `scripts/preflight_check.py --no-api` | ✅ 84 pass / 2 warn / 0 fail | Two warns are unchanged from baseline (pre-existing) |
| `scripts/mock_pipeline_direct.py` | ✅ Pipeline completed; winner=Klippa, 4 candidates | E2E shape preserved |
| `scripts/quality_battery.py --dry-run` | ✅ Fixtures + runners load OK | Free dry-run validation; paid run deferred |

---

## 3. Quality battery — dry-run only

The quality battery harness is built (`scripts/quality_battery.py`) and
dry-run-validated:

```
[dry-run] Validating fixtures load + harness shape...
[dry-run]   Agent 1: 2 fixtures OK
[dry-run]   Agent 2: 2 fixtures OK
[dry-run]   Agent 3: 1 fixtures OK
[dry-run]   Agent 4: 1 fixtures OK
[dry-run] Agent 1+2 runners import OK; Agent 3+4 runners present
```

The paid run (cost: ~$0.50-$1.00) captures the four real-API metrics:

| Metric | Strict-improvement floor at the merge gate |
|---|---|
| Agent 1 blueprint quality | Sub-task count within ±1 of expected; DAG edges match |
| Agent 2 scope coverage | Avg `covers_step_ids` count per scope at parity or higher |
| Agent 3 test validity | 100% Pydantic pass + Shannon-entropy diversity at parity |
| Agent 4 verification precision | verified / (verified + inconclusive) at parity; known-bad → Verified Reject |

**Deferred:** the paid capture awaits operator authorization. The harness
is ready; the comparison delta will be computed against the pre-Phase-2.0
baseline once the run lands.

---

## 4. Section 3 ledger audit — knowledge preservation

Every entry in the plan's per-deletion accounting ledger has a verified
destination in the current code.

### 4.1 Redundancies (wisdom already lives elsewhere) — 5/5 verified

| Plan entry | Status | Evidence |
|---|:---:|---|
| Agent 1 lines 110-112 — triple-emphasis on parallelism | ✅ | Agent 1 mentions parallelism but only blueprint-DAG-parallelism (rules 6-7, parallel_group field); tool-call parallelism is owned by `agent_preamble.py` (no duplication) |
| Agent 1 lines 226-278 — expanded conversational test-count rules | ✅ | Now line 237 — single delegating bullet pointing at Agent 3's persona/goal/rubric framework |
| Agent 3F lines 18-26 — "Historical note" on prior bug | ✅ | DELETED in Phase 2D extension (commit `bae6d92`); `grep -c "Historical note"` returns 0 |
| Agent 3 + Agent 3F "Coverage summary MUST NOT BE EMPTY" repeated 3× | ✅ | Reduced to 1 reminder per prompt (Agent 3 line 456 + Agent 3F line 97). Each prompt owns its own output schema; both reminders are local to their context |
| Agent 4 line 72-73 — "false pass costs nothing" justification | ✅ | Compressed to one-liner: "When uncertain, ALWAYS PASS with notes. A false pass costs nothing (Agent 5 will catch it). A false reject loses a valid candidate forever." |

### 4.2 Patches → principles (wisdom encoded as rule) — 6/6 verified

| Plan entry | Status | Evidence |
|---|:---:|---|
| Agent 1 lines 52-73 — explicit candidate normalization examples | ✅ | Reframed as principles (lines 67-69): canonical brand name, strip qualifiers, skip generic words |
| Agent 2 SEO-bias "Developer primitive vs Packaged product" | ✅ | Reframed as principle (lines 23-33): "deliberately probe two orthogonal framings" — domain-agnostic, applies to every capability |
| Agent 2 weight tables with "like today" framing | ✅ | Reframed (lines 76-81) as starting-point persona guidance + "State your chosen weights and WHY they fit this user" |
| Agent 3 30%/50%/20% magic spread | ✅ | Reframed with `~` prefix (lines 36-38) signaling starting-point semantics |
| Agent 3 "6 dimensions = 6 baseline tests" conflation | ✅ | Split: dimensions describe WHAT to cover (Agent 3 matrix); count derivation lives in Agent 1 `test_count_target` rule (lines 234-239) |
| Agent 4 PASS-rule 5-bullet carve-outs | ✅ | Simplified to "Evidence-Based Determination" principle: PASS on ANY evidence; REJECT only when zero evidence after exhausting strategies |

### 4.3 Durable homes (new tools / schemas / data) — 9/9 verified

| Plan entry | Status | File:symbol |
|---|:---:|---|
| Agent 3 `input_context.instructions` asymmetry | ✅ | `puzzleeval/capability_predicates.py::supports_user_instructions` (line 32) + `schemas.py::TestCase._gate_a3_instructions_asymmetry` (line 2204) |
| Agent 4 4-non-negotiables → standalone validator | ✅ | `validators.py::validate_checklist_for_verified_pass` (line 1373) — G-A4 |
| Agent 2 per-scope floor | ✅ | `schemas.py::Agent2Result._gate_a2_per_scope_floor` (line 1454) — G-A2 |
| research_subagent NEVER-fabricate vs builder pragmatism | ✅ | `research_subagent.py::classify_research_output` (line 197) + 3-tier output schema (ANSWER / REASONABLE_GUESS / NOT_FOUND) |
| rubric_judge ↔ user_simulator goal-completion | ✅ | `schemas.py::SimulatorConfig` (line 1654) with `extend_to_completion` + `max_extension_turns`; user_simulator prompt swap |
| vision_judge fallback over-scoring | ✅ | `vision_judge.py::apply_fallback_cap` (line 122) — tiered cap (0.3/0.5/0.7/0.9) + `cap_reason` field |
| evaluation field-name aliases | ✅ | `puzzleeval/field_aliases_invoices.py` — domain-scoped alias dict + `INVOICE_FALSE_POSITIVE_PAIRS` (line 62) discipline + regression test |
| Agent 2 + Agent 4 `relevant_subtasks` back-compat | ✅ | `schemas.py::read_subtask_or_scope_refs` (line 1359) helper + once-per-process `deprecated_field_read` structured telemetry (CI-safe) |
| Agent 1 line 230 + Agent 3 lines 487-503 — kept as prose with delegation | ✅ | Agent 1 keeps the 4-6 conversational test sanity-check; Agent 3's existing validator (lines 509-511 originally) enforces the empty-fields contract for non-conversational |

**Audit result:** 20/20 entries verified (5 redundancies + 6 principles
+ 9 durable homes). No production wisdom was lost in the refactor.

---

## 5. Outstanding items deferred to operator authorization

1. **Real-API verification** (~$2-$4) at the sub-phase merge gates per
   the plan (Codex C5 made these mandatory). Recommend running before
   merging this branch to main:
   - 2 candidates through Agents 1-4 (~$0.50-$1.00) — verifies prompt
     outputs haven't regressed
   - 2 voice + 2 chatbot candidates through agentic mode (~$1-$2) —
     verifies rubric scoring + vision_cap surfaces correctly
   - 1-2 candidates with a Phase 2D prompt to verify cost-per-build
     parity (~$0.50)

2. **Quality battery measurement run** (~$0.50-$1.00). Harness ready
   (`scripts/quality_battery.py`); paid capture against the 5 fixtures
   produces the post-D2 metric snapshot to compare against the
   pre-Phase-2.0 baseline.

3. **G-A4 caller wiring in `agent4/core.py`.** The standalone validator
   function exists (`validate_checklist_for_verified_pass`); calling it
   at the verdict-assignment boundary in `agent4/core.py` is the
   missing wire-up. Without the call, the gate is "ready but not
   active." Low-risk one-line change. Tracked in
   `PHASE2_FINAL_REPORT.md` Section "Outstanding items".

4. **G-Aux2 promotion path.** The `classify_research_output` classifier
   is advisory today. Promotion to Pydantic-schema enforcement on the
   research_subagent output requires schema + retry-on-violation
   wiring. Reserved for a future commit when 2 release cycles of zero
   false positives accumulate.

---

## 6. Summary

**Phase 2 net delivered:**

- 8 cross-prompt conflicts resolved (F-A1 through F-A5, F-Aux1, F-Aux2, F-Aux3)
- 4 new soft code gates (G-A2 / G-A3 / G-A4 / G-Aux2) per AD-007
- 1 new generalizability primitive (`capability_predicates.py`)
- 1 new domain-scoped data file with false-positive discipline
- 1 new schema sub-field (`SimulatorConfig.extend_to_completion`)
- 1 new structured-output cap helper (`apply_fallback_cap`)
- 37 new tests (all 5 categories — violation / near-miss / env-bypass / structured-logging / no-raise / false-positive)
- TOC + dedup + war-story removal across Agents 1, 3, 3F (Phase 2D)
- 100% knowledge-preservation ledger verified (20/20)
- Cache-prefix invariant from Phase 1 preserved

**Verification status:** all four free gates green (pytest 1832/0/2,
api-tests 37/0, preflight 84/0/0, mock pipeline E2E green, quality
battery dry-run OK).

**Recommendation:** Phase 2 is structurally complete. The deferred
real-API + quality battery runs (~$2.50-$5 total) close the merge gate
formally. Once those land green, this branch is ready to merge to main.
