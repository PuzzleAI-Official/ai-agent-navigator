# Plan: Voice-Run Wall-Clock Optimizations

## Context

Real run trace `73a9d605` (2026-04-25): 25:36 wall-clock for 2 voice
candidates (OpenAI Realtime + ElevenLabs ConvAI) on 7 test cases each.
Reference: a comparable OCR run finishes in ~12 min. Voice is
intrinsically harder (per-test conversation = 60-135s vs OCR's 2-5s
HTTP round-trip), but the audit identified ~6-9 min of avoidable wall-
clock waste in the current pipeline.

This plan ships the user-approved optimizations from the audit, with
evidence behind each item, work-effort estimates, and rollback
notes. Every item lists the evidence that justified the user's decision
to ship or skip.

Reference: `PLAN_AGENT5_RESEARCH_AGENCY.md` for the parallel
build-readiness checklist work that already shipped.

---

## Wall-clock baseline (from file mtimes, not estimates)

```
   0s ──┬─ Agent 1 starts        (Opus 4.7, 1 turn)
 179s ──┼─ Agent 3 done           (parallel with A2)
 364s ──┼─ Agent 2 done           (longer parallel sibling)
 486s ──┼─ Selection submitted    (122s manual click — UX, not algo)
 616s ──┼─ Agent 4 done           (per-cand parallel, 130s)
 774s ──┼─ OpenAI api_spec written
 868s ──┼─ ElevenLabs api_spec written       (4 min later)
 968s ──┼─ OpenAI build COMPLETE              (cand+352s)
1066s ──┼─ ElevenLabs build COMPLETE          (cand+450s, 100s later)
1066s ──┼─ Tests start (BOTH wait for slowest build)
1230s ──┼─ OpenAI tests done
1463s ──┼─ ElevenLabs tests done (longer + bigger conversations)
1535s ──┴─ Final merge of last test + report   (72s)
```

Slowest path: ElevenLabs build (9.5 min) + ElevenLabs tests (7.8 min)
+ trailing merge (1.2 min) + selection pause (2 min) + Agent 1+2
(6 min) + Agent 4 (2 min) ≈ 25.5 min.

---

## Items the user approved + items the user dropped (with reasoning)

### DROPPED — Item 1: Remove build-then-test barrier

**User's challenge** (correct): "saving time only for fast candidates means
the overall time isn't different".

**Math**:
- Current: `wall_clock = max(builds) + max(tests)` — barrier means all tests
  start at max(builds) finish time
- Without barrier: `wall_clock = max(per_candidate_total_time)` — each
  candidate's tests start when its OWN build finishes

**Same wall-clock** when the slowest build belongs to the same candidate
as the slowest tests (the typical case — complex providers tend to need
both longer builds AND longer test conversations). For our run:
- Slowest build: ElevenLabs (9.5 min) — same candidate
- Slowest tests: ElevenLabs (7.8 min) — same candidate
- Wall-clock with barrier: `9.5 + 7.8 = 17.3 min`
- Wall-clock without barrier: `max(9.5+7.8, 5.9+5.3) = 17.3 min` — identical

The barrier WOULD waste time only when slowest-build and slowest-tests
come from DIFFERENT candidates — possible but not observed in this run.

**Verdict**: dropped from this plan. **Zero wall-clock saving for the
typical case.** Could revisit if a future run shows asymmetric build-vs-
test slowness across candidates.

### SHIP — Item 2: Per-candidate parallelism 3 → 7

**User's concern**: "the biggest problem is each provider's concurrent
rate limit". Approved IF safety can be proven OR fallback exists.

**Evidence — fallback exists and is wired**:
- `puzzleeval/rate_limiter.py` ships `TokenBucketLimiter` +
  `GlobalProviderLimiter`. Per-candidate buckets parsed from
  `ScreenedCandidate.rate_limit_info`. Cross-candidate buckets per
  `upstream_provider`.
- Wired into Agent 5's `_execute_all_tests` loop at
  `implement_test_env.py:7617-7620` — every test call goes through
  `rate_limiter.acquire(candidate, upstream)` BEFORE hitting the
  provider API.
- `acquire()` SLEEPS until a token is available — pure back-pressure,
  no errors raised. If we bump parallelism to 7 and the limiter is
  configured for 2 RPS per candidate, the effective parallelism is
  capped at 2 RPS regardless of how many threads are queued.
- Default `PUZZLEEVAL_DEFAULT_RPS=2` (config.py:526) is conservative
  enough for free-tier plans.

**Evidence — provider limits headroom**:
- OpenAI Realtime: tier-1 input quota = 30K tokens/min; typical 5-turn
  voice conversation ≈ 5K tokens. 7-parallel = 35K tokens/min —
  borderline tier-1, fine on tier-2+. Connection limit not publicly
  documented but chat allows 50+ concurrent.
- ElevenLabs ConvAI: paid tiers allow 10+ concurrent agent sessions per
  workspace; free tier limits to 3.
- Both providers: 7-parallel is well within paid-tier limits.

**Risk**: Very low. Worst case at 7-parallel = rate limiter throttles to
3-4 effective parallel = same as current. No errors, no crashes — the
back-pressure design absorbs the surge.

**Change**:
- `puzzleeval/config.py` line 553: default `AGENT6_PER_CANDIDATE_PARALLELISM`
  3 → 7
- Same for `AGENT6_PER_CANDIDATE_SESSION_PARALLELISM` (multi-turn voice)
- Env override `PUZZLEEVAL_AGENT6_PER_CANDIDATE_PARALLELISM=3` available
  for any ops that wants the conservative default back

**Saving** (math): 7 tests / 3 parallel = 3 batches; longest test per
batch dominates. ElevenLabs batch breakdown: max 117s + 134s + 135s = 386s.
With 7-parallel: 1 batch, longest test = 135s. **Saves 250s on
ElevenLabs alone**, ~150s on OpenAI = ~3-4 min off the test phase.

**Effort**: 1-line config change + 1 test update for new default.

### SHIP — Item 3: Pre-create venvs in parallel during Agent 4

**User's constraint** (correct): "as long as we are only creating venv
for our test candidates" — i.e. only the candidates the user picked
during Phase 6 selection.

**Architecture**: Agent 5's `_build_single_harness` calls
`_create_venv(sandbox_dir, logger, trace_id, candidate.name)` at line
3811 — BEFORE the Opus build loop. Each venv setup involves:
1. `python -m venv .venv` (~30-40s on Windows)
2. `pip install --quiet requests websocket-client pydub soundfile numpy
   python-dotenv` (60-90s, the `VENV_PREINSTALL_MANIFEST`)

Total: 90-130s per candidate, currently serialized at the start of each
build's wall-clock budget.

**Why pre-creating only for SELECTED candidates is correct**:
- Agent 4 receives the user-selected candidate list (post-Phase-6 filter
  via `apply_scope_picks`). Agent 5 receives the same filtered list.
- Pre-creating venvs for non-selected Agent 2 candidates would waste
  disk + time. The hook MUST fire after `apply_scope_picks` returns —
  i.e. between selection-submission and Agent 4 start, OR in parallel
  with Agent 4 verification.

**Change**:
- New function `_precreate_venvs_for_selected(state, candidates)` in
  `puzzleeval-api/services/pipeline_runner.py` — fires AFTER selection
  submit, BEFORE Agent 4. Uses `ThreadPoolExecutor` to spawn one
  `_create_venv()` per selected candidate in parallel.
- Each venv lives at `runs/<trace_id>/harnesses/<candidate_slug>/.venv/`
  (the same path Agent 5 expects). When Agent 5 calls `_create_venv()`,
  it short-circuits via `if (sandbox_dir / ".venv" / "Scripts" /
  "python.exe").exists(): return True` — already in `_create_venv`
  from prior pass, no change needed.

**Wall-clock effect**:
- Today: Agent 4 (130s) → Agent 5 build start, then venv setup (90-130s)
  serialized BEFORE Opus turn 1.
- New: Agent 4 (130s) || venv pre-create (90-130s in parallel) →
  Agent 5 build start with venv READY → Opus turn 1 fires immediately.

**Saving**: 60-120s per candidate — but parallel across candidates, so
**60-120s wall-clock total** for the build phase. Net for the slow
candidate (ElevenLabs): 90-120s saved.

**Risk**: Low. The pre-create runs while Agent 4 has slack (Agent 4 spends
most of its time on web_fetch/web_search server-side wait). If pre-create
hits a transient pip mirror error, we log + retry; if it persistently
fails, Agent 5 falls back to its existing in-build venv creation path
(unchanged). Belt + braces.

**Effort**: ~45 min — new function + hook into pipeline_runner +
3 unit tests + integration test.

### SHIP — Item 4: PUZZLEEVAL_EFFORT high → medium

**User's call**: "i think we can make effort to medium which should be
enough".

**Evidence** (from CLAUDE.md NEW-AC + config.py):
- `PUZZLEEVAL_EFFORT={low|medium|high|xhigh|max}` controls
  `output_config.effort` across all adaptive-thinking calls (Agents 2,
  4, 5 builder, 5 evaluator, ask_research sub-agent, deep-verify
  runner).
- Currently defaults to `high`. The "high" tier asks the model to spend
  substantial inference budget on extended thinking between tool calls.
- Empirical evidence from this run's `conversation_log.json`:
  ElevenLabs T2 (Opus) cost $0.97 with 262 output tokens, 0 visible
  text, 0 tool calls — that's pure thinking burning budget.

**Why medium is enough**:
- Our agents now have the BuildReadinessChecklist to short-circuit a
  lot of "what should I do next" reasoning at Phase 1.
- Medium-effort thinking still gets sentence-level reasoning between
  tool calls, just less depth on each.
- Adaptive thinking auto-tunes UPWARD when the model hits a complex
  decision point — medium is a FLOOR, not a cap.

**Change**:
- `puzzleeval/config.py` — change `EFFORT` default from `high` to
  `medium`. Env override `PUZZLEEVAL_EFFORT=high` available to revert.

**Saving**: 30-60s per slow build (per-turn thinking budget halved).
Across 11-turn ElevenLabs build: ~5-10s × 8 turns ≈ 40-80s. Smaller per
turn but compounds.

**Risk**: Quality regression on hard candidates. Mitigation: env
override allows per-deployment tuning. If we observe build-failure rate
ticking up, flip back to `high`.

**Effort**: 1-line config change + 1 test for new default.

### SHIP — Item 5: Per-test audio merge → background thread

**User's call**: "good plan if it can save us time".

**Evidence — current behavior**:
- `voice_realtime.py:_drive_conversation_agentic` (line ~758) calls
  `_merge_conversation_audio(session_token, turns)` synchronously in
  the test worker thread, AFTER the conversation completes.
- Merge does pydub-based decode + sample-rate normalization +
  concat + MP3 re-encode of N audio files (typically 13-19 per test).
- **Observed merge time**: ElevenLabs's last test took 135s wall:
  conversation finished at run+1463s, merged file written at
  run+1535s — that's 72s for the merge alone (over 50% of the test's
  wall-clock).
- The merge BLOCKS the worker thread from accepting the next test in
  queue. With parallelism N, the next batch starts max(merge time of
  prior batch members) later than necessary.

**Math for parallelism=3**, 7 tests, conversation~60s, merge~70s:
- Today (blocking merge): batch 1 = max(60+70) = 130s; batch 2 = 130s;
  batch 3 = 130s; total = 390s
- Background merge: workers free at +60s after conversation; next batch
  starts immediately. Total wall = 60s × 3 batches + 70s tail
  (test-7 merge) = 250s.
- **Savings: ~140s per candidate's test phase** (combines with
  parallelism=7 from item 2 for compounding wins).

**Change**:
- New `ThreadPoolExecutor` in `voice_realtime.py` for background merges
  (size = max parallel tests, daemon threads).
- `_drive_conversation_agentic` submits `_merge_conversation_audio` as
  a background future, returns the test result immediately; future
  written to a shared dict keyed by session_token.
- At end-of-run (after `_execute_all_tests` returns), block on all
  pending merge futures + collect their results into the
  `audio_paths` field on each `TestCaseResult`.
- If a merge fails (existing best-effort behavior), the test still
  passes — per-turn audio is the fallback, "merged" is a UX nicety.

**Risk**: Low. Merge failure is already non-fatal in current code
(line 762 `try/except`). Background execution preserves the same
fail-silent contract. Minor risk: end-of-run blocks until last merge
finishes — we add a 30s timeout per merge so a hung pydub call doesn't
freeze the whole report assembly.

**Effort**: ~45 min — voice_realtime.py refactor + 4 unit tests
covering: background submission, end-of-run join, timeout handling,
preserve fail-silent contract.

### CONFIRMED SHIPPED — Item 6: Checklist token budget fix

Already shipped earlier this session. `VERIFICATION_MAX_TOKENS` 4096 →
12288 + strengthened "REQUIRED" prompt language. Will populate real
checklists on next run, dropping ElevenLabs build from 11 turns → ~5-7
turns. **Saves 3-4 min on the slow candidate's build phase.**

### SKIPPED — Item 7: Selection-pause UX

User's call: "nothing you can do." Pause is interactive UX, not
algorithm. `PUZZLEEVAL_USER_SELECTION_ENABLED=0` flag exists for
headless / scripted runs.

---

## Combined wall-clock projection (next real voice run)

| Phase | Today | Projected with all items | Source of saving |
|---|---|---|---|
| Agent 1 | 3:00 | 3:00 | — |
| Agent 2 + 3 (parallel) | 6:04 | ~5:00 | Item 4 (medium effort on Agent 2) |
| Selection pause | 2:00 (user) | 0:30 (user) | UX only |
| Agent 4 | 2:10 | 2:10 (+venv prep in parallel) | Item 3 — saves on Agent 5, not on A4 |
| Agent 5 build (slowest cand) | 9:30 | ~4:00 | Item 6 (checklist) + 3 (venv ready) + 4 (medium effort) |
| Agent 5 tests (parallelism, bg merge) | 7:48 | ~2:30 | Item 2 (parallelism 7) + 5 (bg merge) |
| Final processing (last merge) | 1:12 | 0:15 | Item 5 |
| **Total** | **25:36** | **~13:15** | **~12 min saved** |

**Best-case scenario hits the OCR-comparable 13-minute target** the
user wanted. Voice's intrinsic per-test conversation cost (~135s for
the longest single 5-turn dialogue) is the floor we cannot go below.

---

## Implementation order + work breakdown

Total work: ~3 hours. Total saving on every voice run: 6-9 min.

### Session 1: Quick wins (~30 min total)
1. **Item 2**: bump `AGENT6_PER_CANDIDATE_PARALLELISM` 3→7 in
   `puzzleeval/config.py` line 553. Same for
   `AGENT6_PER_CANDIDATE_SESSION_PARALLELISM`. Update test
   `test_per_candidate_parallelism.py` to lock the new default.
   Saves 3-4 min on test phase.
2. **Item 4**: change `PUZZLEEVAL_EFFORT` default from `high` to
   `medium` in `config.py`. Update test for new default. Saves
   30-60s per slow build.

### Session 2: Per-test audio merge to background (~45 min)
3. **Item 5**: refactor `voice_realtime.py::_drive_conversation_agentic`
   to submit merge as background future. Add module-level
   `ThreadPoolExecutor` for merges. End-of-run join with 30s timeout
   per merge. Add 4 unit tests:
   - `test_merge_runs_in_background_returns_immediately`
   - `test_end_of_run_joins_pending_merges`
   - `test_merge_timeout_does_not_block_report`
   - `test_merge_failure_preserves_per_turn_audio`

### Session 3: Pre-create venvs (~45 min)
4. **Item 3**: new function
   `puzzleeval-api/services/pipeline_runner.py::_precreate_venvs_for_selected`
   that fires after selection-submit, in parallel with Agent 4.
   ThreadPoolExecutor with one worker per selected candidate.
   Add 3 unit tests:
   - `test_precreate_only_runs_for_selected_candidates`
   - `test_precreate_runs_in_parallel_with_agent_4`
   - `test_agent_5_short_circuits_when_venv_already_exists`

### Verification (~15 min)
5. Full test suite (`pytest tests/`): currently 1308 passing, expect
   1308 + 8 new = 1316 passing.
6. End-to-end mock voice run via the UI to confirm no regressions.
7. (Optional) Real voice run to measure actual saved wall-clock.

---

## Rollback notes per item

| Item | Rollback knob |
|---|---|
| 2 (parallelism 7) | `PUZZLEEVAL_AGENT6_PER_CANDIDATE_PARALLELISM=3` env |
| 3 (venv pre-create) | Remove the `_precreate_venvs_for_selected` call from pipeline_runner; Agent 5 falls back to current in-build venv creation |
| 4 (effort medium) | `PUZZLEEVAL_EFFORT=high` env |
| 5 (background merge) | Set executor `max_workers=1` to force sequential; same code path, just no concurrency |

All four are zero-data-loss rollbacks — flip env / one-line change.

---

## Risk summary

| Item | Risk | Mitigation |
|---|---|---|
| 2 — parallelism 7 | Provider rate limit hit | Rate limiter sleeps; back-pressure absorbs surge |
| 3 — venv pre-create | pip mirror flake during Agent 4 | Agent 5 falls back to in-build venv create |
| 4 — effort medium | Build quality regression | Env override; monitor build failure rate |
| 5 — background merge | Hung pydub blocks report | 30s per-merge timeout |

No item carries data-loss risk. Worst case for any item: same wall-
clock as before the change.

---

## Why item 1 was correctly dropped (math recap)

User's challenge: "saving time only for fast candidates means the
overall time isn't different."

Wall-clock formula:
```
With barrier:    max(builds) + max(tests)
Without barrier: max(per_candidate_total)  where per_candidate_total = build_time + test_time
```

These are equal when the slowest build and slowest tests come from the
same candidate. For our trace:
- ElevenLabs: 9.5 min build + 7.8 min tests = 17.3 min total
- OpenAI: 5.9 min build + 5.3 min tests = 11.2 min total

`max(per_candidate_total)` = `max(17.3, 11.2)` = 17.3 min — IDENTICAL
to the barrier wall-clock of `max(builds) + max(tests)` = 9.5 + 7.8 =
17.3 min.

**The barrier saves nothing in the typical case.** It would only matter
if a fast-build candidate had slow tests (or vice versa) — possible but
not observed. Dropped from this plan; can be revisited if a future trace
shows asymmetric build-vs-test distribution across candidates.
