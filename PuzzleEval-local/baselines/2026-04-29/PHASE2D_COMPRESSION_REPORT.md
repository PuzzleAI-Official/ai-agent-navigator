# Phase 2D Compression Report — 2026-04-29

Follow-up pass after `PHASE2_FINAL_REPORT.md` to address the deferred
token-reduction lever (Section 11.3 of the original plan): "modal-verb
audit, dedup with `agent_preamble.py`, terminology unification."

This pass focused on the user's actual ask: "any potential compression
that handles redundant, useless, or unclear prompts." Scope-extended
beyond the modal-verb audit to include redundancy + war-story removal.

---

## Token deltas — Phase 2D pass

### Templates (Phase 2 final → post-D2)

| Template | Phase 2 final | Post-D2 | Δ tokens | Δ % |
|---|---:|---:|---:|---:|
| agent1_system_prompt | 8,117 | 8,038 | -79 | **-1.0%** |
| agent2_research_system | 2,308 | 2,308 | 0 | 0% |
| agent2_structure_system | 1,073 | 1,073 | 0 | 0% |
| agent3_system_prompt | 8,116 | 6,973 | **-1,143** | **-14.1%** |
| agent3f_system_prompt | 1,959 | 1,841 | -118 | **-6.0%** |
| agent4_verification_system | 3,293 | 3,293 | 0 | 0% |
| agent4_structure_system | 849 | 849 | 0 | 0% |
| **TOTAL TEMPLATES** | **25,715** | **24,375** | **-1,340** | **-5.2%** |

### Auxiliary system prompts (Phase 2 final → post-D2)

| Surface | Phase 2 final | Post-D2 | Δ tokens | Δ % |
|---|---:|---:|---:|---:|
| agent_preamble | 405 | 383 | -22 | -5.4% |
| rubric_judge_template | 902 | 902 | 0 | 0% |
| user_simulator_template | 548 | 579 | +31 | +5.7% |
| research_subagent_system | 1,174 | 1,174 | 0 | 0% |
| vision_judge_system | 40 | 41 | +1 | +2.5% |
| evaluation_system_prompt | 292 | 313 | +21 | +7.2% |
| **TOTAL AUXILIARY** | **3,361** | **3,392** | **+31** | **+0.9%** |

(Auxiliary deltas are mostly measurement-method drift: Phase 2 final
used `count_tokens` estimate for some, the D2 pass used the Anthropic
SDK API for cached prompts. The CONTENT did not change for
research_subagent / rubric_judge / user_simulator / vision_judge.)

---

## What changed

### Agent 3 — biggest win (-14.1% / -1143 tokens)

1. **voice_conversation section** — was a 75-line full re-explanation
   of conversation fields, now a 30-line delegation: "Same agentic
   pattern as conversation; here are the voice-specific deltas."
   Loss: zero. Voice already inherited every field semantic from the
   conversation section; the duplication just made the prompt longer.

2. **input_context.instructions co-located rule** — compressed from a
   46-line section to a 27-line rule. Removed the bullet that
   restated which modalities populate `instructions` (the matrix is
   the single authority). Trimmed the validator paragraph and the
   derivation sentence at the end. Kept the canonical example JSON
   verbatim.

3. **Conversation section's Required TestCase fields — input_data +
   input_context bullets** — was 21 lines, now 8 lines. Removed the
   warnings duplicated in the matrix above ("Do NOT put the agent's
   system prompt in input_data"; "Use the SAME instructions string
   across all conversational tests"). The persona/goal/constraints/
   rubric/max_turns/evaluation_mode bullets — each teaching a
   distinct field — were preserved.

4. **PER-MODALITY FIELD MATRIX preamble** — merged the separate
   "Plugin-shaped test cases" section into the matrix intro. Same
   teaching point, one paragraph instead of two.

5. **CRITICAL DISTINCTION prose under the matrix** — compressed from
   7 lines to 5. "TWO DIFFERENT LLM system prompts FOR TWO DIFFERENT
   SIDES" → "Two LLM system prompts, two sides — do NOT conflate."
   The semantic teaching is identical.

6. **"Forbidden cross-modality field usage" section** — renamed to
   "Cross-modality field usage notes" and reframed: the matrix is
   authoritative, this section captures only the two clarifications
   the matrix doesn't (test_file_path null on synthetic
   voice_conversation; validator warns on conversational fields on
   non-conversational tests). 14 lines → 12 lines.

### Agent 1 — small win (-79 tokens)

1. **Test plan rule #8 (`agent_instructions` REQUIRED)** — was the
   longest rule on the page, ~8 lines of prose with several "EVERY",
   "NEVER", "ALL" emphasis caps. Compressed to ~6 lines without
   losing any of the load-bearing teaching: required for
   conversational scopes, copied verbatim into
   `input_context.instructions`, fill missing details with reasonable
   defaults + note the assumption, leave null for non-conversational.

### Agent 3F — war-story removal (-118 tokens)

1. **"Historical note" paragraph** — explicitly flagged in the Phase
   2 plan (Section 3.1) as "delete the historical justification; keep
   the rule." The rule was already canonically stated above; the
   historical note ("an earlier version of this prompt encouraged
   2-3 tests per medium-variety file...") added nothing for a fresh
   reader and risked re-suggesting the wrong pattern.

---

## What was deliberately NOT compressed

- **research_subagent_system (1,174 tokens, +30% in Phase 2C.4).**
  The growth was the load-bearing 3-tier output schema (ANSWER /
  REASONABLE_GUESS / NOT_FOUND). Compressing the tier teaching
  without losing the middle tier isn't possible — the third tier
  IS the value.

- **agent_preamble (383 tokens).** Already INTENTIONALLY SHORT (file
  docstring states this). Each rule (parallel tools / no narration /
  reason about root cause / verify against source / commit and
  course-correct / concise output) prevents a specific failure mode.

- **vision_judge_system (41 tokens).** 3 lines. Nothing to compress.

- **rubric_judge_template (902 tokens), user_simulator_template
  (579 tokens).** Already well-edited. Each teaching point in
  HOW TO SCORE / WHAT YOU NEVER DO prevents a specific failure mode
  the schema can't catch alone.

- **agent4_verification_system (3,293 tokens).** Decision tree of
  PASS/REJECT/Inconclusive + 4-step evidence-based search strategy +
  Build-Readiness Checklist instructions are all canonical teaching
  artifacts. The 5-test-per-gate discipline traces back to AD-007.

---

## Verification

| Check | Status |
|---|---|
| pytest sweep | 1832 passed, 2 skipped (unchanged) |
| puzzleeval-api tests | 37 passed |
| Source-grep tests in lockstep | 4 updated (test_agent_instructions_grounding.py) |
| Preflight wiring checks | 84 pass / 2 warn / 0 fail |
| Mock pipeline E2E | green — winner='Klippa', 4 candidates |
| Cache-prefix invariant | preserved (no new mid-prompt placeholders) |

---

## Cumulative Phase 2 picture (PRE-Phase-2 baseline → post-D2)

| Template | Pre-Phase-2 | Post-D2 | Δ tokens | Δ % |
|---|---:|---:|---:|---:|
| agent1_system_prompt | 8,126 | 8,038 | -88 | -1.1% |
| agent2_research_system | 2,044 | 2,308 | +264 | +12.9% (F-A2 expansion) |
| agent2_structure_system | 1,066 | 1,073 | +7 | +0.7% |
| agent3_system_prompt | 8,098 | 6,973 | **-1,125** | **-13.9%** |
| agent3f_system_prompt | 1,959 | 1,841 | -118 | -6.0% |
| agent4_verification_system | 3,173 | 3,293 | +120 | +3.8% (G-A4 wiring) |
| agent4_structure_system | 849 | 849 | 0 | 0% |
| **TOTAL** | **25,315** | **24,375** | **-940** | **-3.7%** |

Net Phase 2 + D2: **-3.7% on the agent template surface** while
adding 8 cross-prompt conflict resolutions, 4 new code gates, and
1 new generalizability primitive. Cache-prefix invariant from Phase
1 (+289%) is preserved, so the **effective input cost on cached
builds remains substantially lower than pre-Phase-1** — the cache
amortizes both the small token regressions and the structural gains.

---

## Recommendation

This pass is the natural stopping point for prompt compression — the
remaining surface area is either load-bearing teaching content or
already terse. Further token reduction would start sacrificing
clarity or removing wisdom.

**Next pre-merge step (deferred from Phase 2 final):** the real-API
verification (~$2-$4) and quality battery (~$0.50-$1) operator-
authorized run, before merging this branch to main.
