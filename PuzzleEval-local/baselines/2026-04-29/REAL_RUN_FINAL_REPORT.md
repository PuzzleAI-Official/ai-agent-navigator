# Real Run — Final Report (2026-04-30)

End-to-end live run of the Phase-2-refactored pipeline against the
user's stated request: **"Compare OpenAI and ElevenLabs voice agents
for picking up calls at my 24/7 plumbing business in Los Angeles."**

- **Run ID:** `6e0c9563`
- **Trace:** `97b0427c-e6da-45dd-a9a8-5d36e077e145`
- **Duration:** ~25 minutes wall-clock
- **Cost:** $6.0830 USD (121.66 credits)
- **Status:** completed; report rendered
- **Outcome:** declared OpenAI Realtime API the winner (the ONLY
  candidate that completed test execution)

---

## Headline finding

**The user wanted a comparison between OpenAI and ElevenLabs. The
report shows OpenAI as the sole candidate and gives no surfaced
explanation that ElevenLabs was built but failed adversarial
verification before tests ran.** A user reading this report would
believe ElevenLabs was never attempted. See Issue #6 below.

The Phase 2 prompt-refactor work itself produced excellent outputs
(workflow blueprint, agent_instructions, test cases, rubric criteria
all on point). The issues below are mostly about reporting / UX /
consistency / a few schema-vs-code gaps.

---

## What worked beautifully

| Stage | Outcome | Quality signal |
|---|---|---|
| Agent 1 — User Understanding | Single voice_agent step + complete plumbing prompt with price menu, service area, escalation policy | F-A1 delegation working; agent_instructions verbatim correct |
| Agent 1 — Explicit candidate capture | Both "OpenAI" and "ElevenLabs" extracted from natural language → `explicit_candidates` | Phase 2A capture rule honored |
| Agent 2 — Research | 6 candidates ranked: OpenAI 95%, ElevenLabs 95%, Synthflow 87%, Retell 84%, Thoughtly 79%, Vapi 54% | Provider diversity; class labeling (Developer primitive vs Packaged product) on every candidate |
| Agent 2 — User-named boost | Activity feed: "Boosted relevance for 2 user-named provider(s)" | Explicit candidates surface to top of selection |
| Agent 3 — Test generation | 5 voice_conversation tests covering happy-path, out-of-area, out-of-scope (AC), context-retention, edge case | Domain-specific rubrics: pricing_accuracy, service_area_policy, scope_adherence, context_retention; all marked `evaluation_mode=agentic`; all populate `input_context.instructions` |
| Selection panel UX | OpenAI + ElevenLabs + Retell auto-selected; adoption-difficulty badges (easy/medium/hard); START TESTING CTA prominent | Per-scope floor of 3 (G-A2) honored |
| Pipeline progress strip | 5-stage with per-stage credit accounting (6.17 / 2.62 / 19.35 / 89.84 credits) | Granular cost surfacing |
| Activity feed | 9 sections, collapsible, granular per-step messages | Excellent operator visibility |
| Stuck-pipeline detector | "No progress for 3m 6s — pipeline may be stuck" banner appeared during long Agent 4 step | Defensive UX present |
| OpenAI build | 9 turns, $2.05 build cost, 85.64% cache-hit rate, adversarial probes 0/6 critical failures | Phase 1 cache-prefix invariant paying off |
| OpenAI live tests | 5 tests run, real TTS-synthesized callers, real provider audio, rubric-judged transcripts, audio MP3s saved per turn | Voice harness multi-turn driver works end-to-end |
| OpenAI rubric scores | tc-001=0.705, tc-002=0.87, tc-003=0.9625, tc-004=0.63, tc-005=0.25; overall 0.6835 | Realistic spread; out-of-scope refusal (tc-003) scored highest |

---

## Issues exposed (8 total, in priority order)

### #6 (CRITICAL) — ElevenLabs silently absent from the final report

The candidate the user named got built, failed the adversarial
verifier, and was excluded from test execution. The report shows it
nowhere — no row, no "build failed" badge, nothing.

**What actually happened:**
- ElevenLabs Conversational AI: 13 build turns, $2.28 spent
- Smoke test passed (`smoke_test_passed: True`)
- Live validation attempted but `live_validation_passed: None`
- Adversarial probe report:
  - `harness_ready: False`
  - `critical_failure_count: 5`
  - Critical failures:
    1. `empty_input: timeout` — harness hangs on empty input
    2. `max_input: timeout` — harness hangs on max-size input
    3. `malformed_input: timeout` — harness hangs on garbage input
    4. `idempotency: call_a=crash, call_b=crash` — calling twice crashes both times
    5. `concurrency: outcomes=['crash', 'crash', 'crash']` — concurrent calls all crash

The Agent 5 builder produced a WebSocket-based ElevenLabs harness that
hangs on edge inputs (no timeout) and crashes on retry/concurrency
(probably singleton WebSocket state, port reuse, or unclosed file
handle). The adversarial verifier correctly caught this — but the
report assembler did NOT surface it.

**Impact:** the user's stated comparison goal (OpenAI vs ElevenLabs) is
silently incomplete. The report says "OpenAI wins" against a field of
1 candidate, with no acknowledgement that ElevenLabs was attempted and
why it didn't make it.

**Fix direction:** `report.py::assemble_report` should iterate
`agent_5_output.harnesses` (not just `candidate_runs`) and surface a
"Build attempted, failed adversarial validation" row for every harness
that was built but didn't make it to test execution. Include the
critical_failures list verbatim so the user understands what blocked
it. This is also where Retell would surface — it was screened, but
never picked up by the build phase.

### #7 (HIGH) — Pass rate displayed two different ways in the same report

In the **Ranked candidates** card at the top of Results: "5/5 passed
(100%) · score 68%"

In the **Per-scope breakdown** card directly below: "PASS RATE 60% ·
3/5 passed"

In the **Activity feed**: "OpenAI Realtime API: 3/5 tests passed"

The truth is 3/5 — tc-004 and tc-005 failed (their scores 0.63 and
0.25 below the per-criterion critical thresholds). The 5/5 figure
appears to use a different pass criterion (any score > 0?) and is
misleading.

**Fix direction:** `report.py` and `EvaluationReportCard.tsx` must use
ONE pass-criterion. Either weighted-overall ≥ 0.5 + no critical
failures (the rubric's stated rule) or per-criterion average — but not
both inconsistently.

### #5 (MEDIUM) — Agent 3 emitted empty `coverage_summary: {}` despite prompt rule

The Phase 2D prompt says "This field MUST NOT be empty — every
sub-task must appear as a key with its integer count." On this real
run, Agent 3 emitted `{}`.

**Root cause:** prompt-only rule with no schema enforcement. Per
AD-007, prompts teach but code enforces — this rule should auto-
populate from `test_cases` (group by `sub_task_ref`).

**Fix:** `model_validator(mode='after')` on `Agent3Result` that
auto-fills `coverage_summary` if empty.

### #1 (MEDIUM) — Anthropic strict-grammar 400 + 503 (transient, but visible)

```
structured_output: strict-grammar path failed, falling back to non-strict tool —
Error code: 400 — 'compiled grammar is too large...'
structured_output.strict_parse: transient Anthropic error (attempt 1/4) —
Error code: 503 — 'Grammar compilation is temporarily unavailable.'
```

`Agent1Result`'s schema is too complex for Anthropic's strict-grammar
mode. Falls back to non-strict tool with retries. Operator sees scary
log lines; Agent 1 takes ~30-45s on first call instead of ~10s.

**Fix:** lower the 400 log to DEBUG (it's expected for our schema) or
simplify `Agent1Result` (flatten optional nested types).

### #2 (MEDIUM) — "FREE / search only" badge confusing when billing_enforced=false

The top-right badge says "FREE / search only" while the API exposes
`tier_features: {testing: false}` AND `billing_enforced: false`. Users
think testing is disabled but the START TESTING button works fine.

**Fix:** when `billing_enforced=false`, surface "Dev mode · all
features enabled" instead of "search only".

### #3 (LOW) — Chat panel "thinking..." indicator stuck after Agent 1 finishes

The chat panel keeps the "..." dots for 30+ seconds after Agent 1
completes and the pipeline has moved past selection. Confirmed via
SSE: `workflow_blueprint` event fires correctly with full payload but
the chat panel doesn't subscribe to clear its thinking state.

**Fix:** add an SSE handler in the chat hook that clears the thinking
state on `workflow_blueprint` or `pipeline_started` events.

### #4 (COSMETIC) — Windows console mojibake on em-dash log lines

Backend uvicorn logs render unicode em-dashes (`—`) as `�` on
Windows cp1252 console.

**Fix:** set `PYTHONIOENCODING=utf-8` at uvicorn startup, or strip
em-dashes from log strings.

### #8 (NEW — design question) — `agent_5_output.candidate_runs[].test_results[].score` is None even though scores exist

The `evaluation_report.json::success_evidence` has per-test scores
(0.705, 0.87, 0.9625, 0.63, 0.25) but the upstream
`agent_5_output.candidate_runs[0].test_results[].score` is `None` for
every test. The report assembler is computing scores from a different
source than the per-candidate run record.

**Fix direction:** when persisting `test_results[]` in the candidate
run record, fill in the rubric-judge `score` and `reasoning_excerpt`
fields so `agent_5_output.json` is self-contained for debugging. This
is the file an operator reads when investigating "why did this test
fail" — having empty score fields forces an extra hop into the
evaluation report.

---

## Per-test breakdown for OpenAI Realtime API

| Test | Scenario | Score | Result (per success_evidence) | Result (per candidate_runs) |
|---|---|---:|---|---|
| tc-001 | Happy-path emergency water-heater booking | 0.705 | passed | passed |
| tc-002 | Out-of-area Bakersfield caller | 0.870 | passed | passed |
| tc-003 | Out-of-scope AC repair (must redirect) | 0.9625 | passed | passed |
| tc-004 | Multi-turn drain-clog with context retention | 0.630 | passed | **failed** |
| tc-005 | (5th scenario) | 0.250 | passed | **failed** |

The Phase 2 refactor's domain-specific rubric criteria worked — the
out-of-scope test (tc-003) scored highest (0.9625), suggesting the
agent correctly recognized "AC isn't in scope" and redirected, hitting
`scope_adherence` and `accuracy_no_hallucination` cleanly. The
context-retention test (tc-004) scored 0.63 (mid) and the lowest test
(tc-005) was 0.25 — these are the spots where the agent likely
hallucinated or lost state.

Per-turn audio MP3s are saved under
`puzzleeval-api/runs/97b0427c-e6da-45dd-a9a8-5d36e077e145/harnesses/openai_realtime_api/voice/`
and the playback links surface in the candidate's success_evidence
block.

---

## Cost breakdown ($6.08 total)

| Stage | Cost (USD) | Notes |
|---|---:|---|
| Agent 1 (User Understanding) | ~$0.18 | Single Sonnet call w/ structured output fallback |
| Agent 2 (Research) | ~$0.31 | 4 web_searches, 6-candidate rank |
| Agent 3 (Test cases) | ~$0.13 | One Sonnet call producing 5 voice tests |
| Agent 4 (Screening, all 3 candidates) | ~$0.97 | 3 verifications with web_search/web_fetch |
| Agent 5 build — ElevenLabs | $2.28 | 13 turns, harness built but failed adversarial |
| Agent 5 build — OpenAI | $2.05 | 9 turns, harness passed adversarial 0/6 failures |
| Agent 5 test execution — OpenAI | included | 5 voice tests with TTS + STT + rubric judge |
| **Total** | **$6.08** | within the $25 cap (24% utilization) |

Cache-hit rate during builds: 83-85% (Phase 1 prompt-cache invariant
working as designed — saves ~75% of input token cost on repeated
builder turns).

---

## Recommendation for the user

The system DID validate the user's primary goal (OpenAI works for the
plumbing voice agent use case at 0.68 overall, 3/5 passed). The
ElevenLabs side of the comparison is what's missing.

Before re-running:
1. **Fix Issue #6** so the next run surfaces ElevenLabs's adversarial
   failures in the report. Without that, every subsequent eval that
   has a failed-build candidate will look incomplete.
2. **Fix Issue #7** so pass-rate displays consistently.
3. **Fix Issue #5** so `coverage_summary` is auto-populated.

Then re-run the same prompt — the Phase 2A rubric quality + Phase 1
cache-prefix savings should hold, and you'll see a real two-candidate
comparison with ElevenLabs's failure surfaced clearly (along with the
critical_failures detail so the user can decide whether to retry the
build or accept the gap).
