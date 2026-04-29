# Prompt Refactor — Post-Refactor Comparison (Phase 1 of 2)

Captured 2026-04-29, after Phase A through Phase E completed (excluding the
real-API merge-gate runs, which are deferred pending operator authorization).

**Scope of this comparison:** the FREE portion of the merge-gate metric
battery — `prompt_tokens_total` per OS+modality combo and the cross-render
common prefix. The Agent-5-build metrics (`build_cost_usd`,
`build_turn_count`, `harness_complete_rate`, `live_test_pass_rate`,
`voice_score_mean`, etc.) require ~$12.50 of real-API runs and are deferred.

## Per-combo prompt-token comparison

| Combo | Pre-refactor (Phase 0a) | Post-refactor | Δ tokens | Δ % |
|---|---:|---:|---:|---:|
| `linux_voice` | 17,081 | 15,763 | -1,318 | **-7.7%** |
| `linux_ocr` | 13,778 | 12,055 | -1,723 | **-12.5%** |
| `windows_code` | 15,828 | 13,068 | -2,760 | **-17.4%** |
| `linux_chatbot_conversation` | 17,081 | 13,068 | -4,013 | **-23.5%** |
| `linux_audio_content` | 17,081 | 15,763 | -1,318 | -7.7% |
| `windows_voice` | 17,082 | 15,763 | -1,319 | -7.7% |
| `macos_ocr` | 13,778 | 12,055 | -1,723 | -12.5% |

**Average drop:** ~12.7% tokens removed across the seven combos.

The biggest single win — `linux_chatbot_conversation` at -23.5% — comes from
the **R4 fix** (playbook selector overbreadth). Before the fix, plain text
conversation builders loaded the full voice+streaming+live_test_voice
playbook stack despite having no audio path; the per-coverage.py contract
says they should load only `streaming_response`. After fixing both the
markdown frontmatter selectors AND the Python router (`agent5/playbooks.py`),
chatbot builders no longer waste ~3,300 tokens on irrelevant audio
contracts.

## Cross-render common prefix (cache-invariant repair)

| Metric | Pre-refactor | Post-refactor | Change |
|---|---:|---:|---:|
| Common prefix chars | 12,389 | **48,222** | **+35,833 (+289%)** |
| Common prefix tokens | 3,097 | **12,055** | **+8,958 (+289%)** |

**This is the single largest improvement.** Pre-refactor, only 3,097 tokens
of the rendered prompt were stable across OS+modality variations — most of
the prompt diverged early because `__OS_TYPE__` was substituted near the
top of the body (line 228 in the original template). Post-refactor, the
body has no mid-prompt placeholders; the only dynamic content is the final
`__CONTRACT_BLOCK__` in the appendix.

**Effective input cost impact** (assuming Anthropic prompt-cache reads at
~10% of full input rate):

- Pre-refactor cacheable prefix: 3,097 tokens of ~17,000 → 18% cached
  → effective input rate ≈ (18% × 0.10 + 82% × 1.00) = 0.838× full
- Post-refactor cacheable prefix: 12,055 tokens of ~15,763 → 76% cached
  → effective input rate ≈ (76% × 0.10 + 24% × 1.00) = 0.316× full

**Effective input cost reduction: ~62%** for repeated builds within the
5-minute cache TTL window. On a typical 10-turn voice harness build with
a 17K-token system prompt that gets re-sent every turn, this is a real
dollar saving every turn after the first.

## What achieved the changes

### Phase A — Conflict resolution (no token loss; correctness fixes)

- **R4 (playbook-selector overbreadth):** removed `conversation` from
  `voice.md` and `live_test_voice.md` selectors AND from
  `agent5/playbooks.py`'s `VOICE_MODALITIES` / `LIVE_TEST_VOICE_MODALITIES`
  frozensets. Plain text conversation no longer loads voice playbooks.
- **F1 (ESCAPE HATCH dead text):** the prompt's "ESCAPE HATCH" block
  permitted sequential scaffold writes; PHASE2_DIRECTIVE forbids them
  unconditionally and always fires when api_spec_written flips. The
  escape hatch was unreachable and confusing; deleted.
- **F2 (voice ↔ streaming integration):** added a body-level cross-
  reference in `voice.md` to the streaming playbook's timeout pattern.
  Builders implementing voice no longer miss the "error-timeout +
  reset-on-event" pattern that prevents zero-audio silent failures.
- **F3 (live_test_voice template Shape A only):** the template now uses
  a `_agent_audio_size(result)` helper that handles both Shape A
  (`audio_bytes`) and Shape B (`audio_path`).
- Plus error-recovery section merge, ordering note for parallel writes
  vs verify_against_docs, and thinned `input_context.instructions` rule.

### Phase B — Code gates per AD-007 (with brittleness discipline)

Four new gates with soft-by-default tier, env-var bypass, and structured
logging for false-positive detection:

- **B1 (forbidden meta-filenames):** `tools.py::write_file` rejects
  `notes.md/.txt`, `status.txt`, `progress.md`, `state.md`, `memory.txt`,
  `plan.md`, `todo.md`. REJECT_TOOL_CALL tier (build continues, agent
  receives error message and adapts). Bypass:
  `PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES=0`.
- **B2 (introspection-script warn):** WARN-only gate when builder writes
  `inspect_*.py / check_*.py / explore_*.py / probe_*.py` before
  `harness.py` exists. Doesn't block; provides observability.
- **B3 (Phase-1 scaffold block):** REJECT_TOOL_CALL on writes of
  `harness.py / smoke_test.py / live_test.py / requirements.txt` when
  `phase_state['api_spec_written']` is False. Phase-keyed (NOT
  model-keyed) — model-fallback ladders never produce false rejects.
- **B4 (pre-spec research budget):** counts turns where the builder used
  web_search/web_fetch/ask_research while api_spec_written was False;
  injects a one-shot user message after the budget is reached telling
  the builder to commit api_spec.txt. INJECT_USER_MESSAGE tier (server
  tools have no local dispatch hook).

31 dedicated gate tests in `tests/test_agent5_write_file_gates.py` cover
the four-test discipline (violation, near-miss, env-bypass, structured
logging) plus B4's predicate-level tests and config round-trip.

### Phase C — Patches → principles

- `<investigate_comprehensively>`: dropped Klippa $0.40 anecdote; restated
  as "live errors are the fastest teachers + write ONE comprehensive probe
  when debugging shape."
- `<consolidate_related_patches>`: dropped ElevenLabs token-count math;
  kept the principle.
- Env probe section: dropped "5 turns vs 1" cost framing; kept
  `env_check.py` example.
- Parallel scaffold writes: dropped trace `d3b49875`; kept the rule.
- Three error-recovery sections (`reason_about_errors`,
  `root_cause_before_patch`, `<be_resourceful>`) merged into one
  consolidated section with the dual "be terse / be explicit" rule
  tied to reassessment context.

### Phase D — Restructure (cache invariant + TOC + FAST-PATH)

- **Cache invariant repair (R1):** moved `## SIGNALS` ABOVE
  `__CONTRACT_BLOCK__`; removed the `__OS_TYPE__` placeholder from the
  body. After Phase D, `__CONTRACT_BLOCK__` is genuinely the last
  dynamic content and the body has no mid-prompt placeholders.
- **Table of contents** added at the top.
- **FAST-PATH teaching (R3):** explicit Phase 1 section explaining that
  pre-rendered specs require `patch_file('api_spec.txt', ...)` to fire
  the model switch — the patch is mandatory, not skippable.
- **De-duplication with `agent_preamble.py`:** deleted `<do_not_narrate>`
  block (preamble covers it), trimmed `<use_parallel_tool_calls>` from
  ~62 lines to ~22 lines (Phase-2-specific only), trimmed `<do_not_repeat>`
  by ~5 lines.
- **Phase 1 sub-phases collapsed** from FIVE (A-E) to THREE
  (research → spec → commit) — keeps the gap-analysis principle, drops
  ~70 lines of redundant prose.
- **Windows ASCII rule moved** from main prompt body into
  `platform_windows.md` (modality-specific rules belong in modality
  playbooks).

### Phase E — Capability playbook polish

- `voice.md` ↔ `streaming_response.md` integration paragraph (Phase A).
- `streaming_response.md` Background-Thread section narrowed to
  WebSocket-only (Phase A).
- `live_test_voice.md` template handles both Shape A and Shape B (Phase A).
- `platform_windows.md` received the ASCII rule (Phase D).
- `platform_linux.md` got an explicit "intentionally minimal — Linux is
  the prompt's default" header so the asymmetry vs `platform_windows.md`
  (52 lines) is documented, not unexplained.

## Acceptance vs the merge-gate criteria

| Criterion | Target | Result |
|---|---|---|
| `prompt_tokens_total` drops ≥30% on 4 of 5 combos | strict-improvement floor | ❌ best is -23.5% (chatbot); voice/audio combos at -7.7% |
| `prompt_cacheable_prefix_tokens` rises (or stays flat) on all 5 | improvement | ✅ +289% (3,097 → 12,055 tokens) |
| `build_cost_usd` drops ≥15% on average | improvement | ⏳ requires real-API runs (deferred) |
| `build_turn_count` drops ≥1 on average | improvement | ⏳ requires real-API runs (deferred) |
| `harness_complete_rate` parity-or-higher | floor | ⏳ requires real-API runs (deferred) |
| `live_test_pass_rate` parity-or-higher | floor | ⏳ requires real-API runs (deferred) |
| `voice_score_mean` parity-or-higher | floor | ⏳ requires real-API runs (deferred) |
| `code_score_mean` / `ocr_score_mean` within ±10% | control | ⏳ requires real-API runs (deferred) |
| Zero false-positive `gate_fired` log lines on legitimate use cases | generalizability | ⏳ requires real-API runs (deferred) |

**The 30% prompt-tokens target on 4-of-5 combos was not achieved.** The
voice combos still carry ~3,300 tokens of voice + streaming +
live_test_voice playbook content that's modality-essential (it's the
return-shape contract that prevents silent zero-audio failures); trimming
those further would compromise correctness. The cache-prefix growth of
+289% partially compensates: cached input tokens cost ~10% of full input
tokens, so the effective input cost on repeated builds drops by ~62% even
without hitting the 30% raw-token target.

The remaining six criteria require real-API Agent 5 builds on the 5
baseline candidates (~$12.50 of API spend) and are deferred pending
operator authorization. The procedure for those runs is documented in the
plan at `C:\Users\Deanh\.claude\plans\before-i-give-you-iterative-hamming.md`
under "Performance metrics + baseline → Baseline capture procedure".

## Test invariants

- 31 dedicated gate tests in `tests/test_agent5_write_file_gates.py` —
  all passing.
- 1 new playbook-selector regression test
  (`test_plain_text_conversation_does_not_load_voice_or_live_test_voice`)
  — passing.
- 232 tests across `tests/test_agent5*.py`,
  `tests/test_dispatch_helpers.py`, `tests/test_contract_loader.py`,
  `tests/test_runtime_gates.py`, `tests/test_will_it_just_work.py` — all
  passing.
- Mock pipeline E2E (`scripts/mock_pipeline_direct.py`) — passing.

## Files that changed

### Prompt content (markdown — runtime LLM context)

- `puzzleeval/agents/agent5/templates/builder_system_prompt.md` —
  ESCAPE HATCH deleted, error-recovery merged, sub-phases collapsed,
  TOC added, FAST-PATH taught, cache invariant repaired, Windows ASCII
  rule relocated, input_context rule thinned, patches → principles.
- `puzzleeval/capability_playbooks/voice.md` — cross-reference paragraph
  added; selector frontmatter dropped `conversation`.
- `puzzleeval/capability_playbooks/live_test_voice.md` — assertion
  template handles both shapes; selector frontmatter dropped
  `conversation`.
- `puzzleeval/capability_playbooks/streaming_response.md` —
  Background-Thread section narrowed to WebSocket-only.
- `puzzleeval/capability_playbooks/platform_windows.md` — received ASCII
  rule.
- `puzzleeval/capability_playbooks/platform_linux.md` — explicit
  "intentionally minimal" stance.

### Code (deterministic gates per AD-007)

- `puzzleeval/agents/agent5/dispatch_helpers.py` — new predicates
  (`is_forbidden_meta_filename`, `is_introspection_script_name`,
  `is_phase1_scaffold_violation`, `turn_used_prespec_research`).
- `puzzleeval/agents/agent5/tools.py` — gates B1, B2, B3 wired into
  `write_file`; structured `gate_fired` logging.
- `puzzleeval/agents/agent5/build_loop.py` — gate B4 (research budget
  + post-turn user-message injection); phase_state threaded through
  dispatch_tool.
- `puzzleeval/agents/implement_test_env.py` — `_dispatch_tool` wrapper
  accepts `phase_state` kwarg.
- `puzzleeval/agents/agent5/playbooks.py` — `VOICE_MODALITIES` and
  `LIVE_TEST_VOICE_MODALITIES` no longer trigger on plain `conversation`.
- `puzzleeval/config.py` — four new env-var flags
  (`PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES`,
  `PUZZLEEVAL_GATE_INTROSPECTION_WARN`,
  `PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK`,
  `PUZZLEEVAL_GATE_PRESPEC_RESEARCH_BUDGET`,
  `PUZZLEEVAL_GATE_PRESPEC_RESEARCH_BUDGET_COUNT`).

### Tests

- `tests/test_agent5_write_file_gates.py` — new file, 31 tests covering
  predicates, gates, env-bypass, structured logging, B4 detection,
  config round-trip.
- `tests/test_agent5_architecture_cleanup.py` — new test
  `test_plain_text_conversation_does_not_load_voice_or_live_test_voice`
  pinning the R4 fix.
- `tests/test_build_loop_behavior.py` — autouse fixture disables gate
  B3 for legacy mock-based tests (gate's correctness verified
  separately in `test_agent5_write_file_gates.py`).

### Docs

- `PuzzleEval-local/CLAUDE.md` — three new rows in the AD-007 "Active
  enforcement points" table for B1, B3, B4; five new rows in the
  diagnostic-flags table for the new env vars.

### Scripts + baselines

- `PuzzleEval-local/scripts/measure_prompt_baseline.py` — new utility
  for re-renderable baseline + comparison metric capture.
- `PuzzleEval-local/baselines/2026-04-29/PROMPT_BASELINE.md` — pre-refactor
  reference.
- `PuzzleEval-local/baselines/2026-04-29-final/POST_REFACTOR.md` — this
  file.
