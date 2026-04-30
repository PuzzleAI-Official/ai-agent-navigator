# Real Run — Observations + Issues Exposed (2026-04-30)

Live test of the full Phase-2-refactored pipeline against a real user
prompt: **"Compare OpenAI and ElevenLabs voice agents for picking up
calls at my 24/7 plumbing business in Los Angeles."**

Run ID: `6e0c9563`. Trace: `97b0427c-e6da-45dd-a9a8-5d36e077e145`.

This document captures issues exposed in real time. Numbered for
follow-up.

---

## Setup

- Frontend (Vite, port 8080) and backend (FastAPI uvicorn, port 8001)
  both started via `preview_start`.
- Initial bug: `.claude/launch.json` had `"port": 8080` without
  `autoPort` key; the preview tool flagged port 8080 as in use by the
  Vite I had started manually first. **Fix:** added explicit
  `"autoPort": false` to both `frontend` and `backend` configs (this is
  correct — Vite hardcodes `port: 8080` in `vite.config.ts` and the
  backend's CORS + Vite's `/pzapi` proxy depend on the fixed ports).

## What worked beautifully

1. **Agent 1 → WorkflowBlueprint quality.** Single `voice_conversation`
   step with comprehensive `agent_instructions` (full price menu,
   service area, escalation policy). Persona was realistic ("45yo
   homeowner, water heater leaking tonight"). Rubric criteria spread
   across 6 dimensions including the safety-critical
   `accuracy_no_hallucination` and `service_area_policy`. The Phase 2A
   F-A1 delegation worked — Agent 1 deferred conversational test
   counts to Agent 3's framework instead of duplicating the rules.

2. **Explicit candidate capture (Agent 1 line 60-80 in prompt).** The
   user's "OpenAI and ElevenLabs" was correctly captured into
   `explicit_candidates` and auto-injected at the selection screen.

3. **Agent 2 → relevance scoring.** Both OpenAI Realtime API and
   ElevenLabs Conversational AI scored 95%; Retell AI 84%; Vapi 54%;
   Synthflow 67%; Thoughtly 79%. Six candidates discovered, three
   auto-pre-selected (the named two + Retell to satisfy the per-scope
   floor of 3 per the Phase 2B G-A2 gate). Class labeling worked:
   "ElevenLabs · Developer primitive..."; "OpenAI · Developer
   primitive..."; "Retell · Packaged product...". Adoption-difficulty
   tags rendered correctly (easy/medium/hard).

4. **Agent 3 → 5 voice_conversation test cases.** Test count of 5 is
   in the Agent 3 conversational-scope sweet spot (4-6 per Phase 2A
   F-A1). The test plan's `agent_instructions` was rendered verbatim
   with the full plumbing prompt — Phase 2D's compressed
   "input_context.instructions co-located rule" is being honored.

5. **Selection panel UX.** Pre-checked the 3 highest-relevance
   candidates with adoption-difficulty badges. "START TESTING" CTA
   prominent. "Add custom provider" affordance present.

6. **Pipeline visualization.** Five-stage progress strip (Understand
   → Research → Test Cases → Screen → Build + Test) with per-stage
   credit tracking ("6.17 credits", "2.62 credits"). Active stage has
   blue dot indicator.

7. **Activity feed.** Granular per-step status messages ("Workflow
   designed with 1 scope(s)", "Searching for AI solutions...", etc.)
   with collapsible subsections.

## Issues exposed

### Issue #1 — Anthropic strict-grammar 400 + 503 (transient, but visible to operator)

```
structured_output: strict-grammar path failed, falling back to non-strict tool —
Error code: 400 — 'The compiled grammar is too large, which would cause performance
issues. Simplify your tool schemas or reduce ...'

structured_output.strict_parse: transient Anthropic error (attempt 1/4), retrying in 4.6s —
Error code: 503 — 'Grammar compilation is temporarily unavailable. Please try again.'
```

- **Root cause:** Agent 1's `UserUnderstandingOutput` Pydantic schema
  is too complex for Anthropic's strict-grammar mode (workflow blueprint
  + test plan + sub_tasks + persona-rich nested types). Strict path
  fails with 400; falls back to non-strict tool with retries on 503.
- **Impact today:** No data loss — the `parse_with_fallback` path
  catches it. But operators see scary log lines and Agent 1 takes
  ~30-45s instead of ~10s on first call.
- **Fix direction:** either (a) accept the fallback path silently
  (lower the log level for the 400 to DEBUG), or (b) trim
  `Agent1Result`'s schema (e.g., flatten optional fields) so strict
  grammar fits.

### Issue #2 — "FREE search only" plan badge confusing for first-time eval

The top-right badge says "FREE / search only" and the `/api/runs/{id}`
response shows:
```json
"quota": {
  "plan": "free",
  "tier_features": {"search": true, "testing": false, "monitoring": false},
  "billing_enforced": false
}
```

- The label suggests testing is disabled, but `billing_enforced=false`
  means the gate doesn't actually block. The user clicks START TESTING
  and the pipeline does proceed.
- **Impact:** confusing UX. A new user will assume "search only"
  means they can't run a real eval, when the env-var override is
  already on (PUZZLEEVAL_BILLING_ENFORCED=0 default).
- **Fix direction:** when `billing_enforced=false`, surface "Dev mode
  · all features enabled" instead of "search only".

### Issue #3 — Chat panel "thinking..." indicator stuck after Agent 1 finishes

Chat shows the "..." dots for ~30+ seconds after Agent 1 completes and
the pipeline has already moved past selection into screening. The
chat-panel component doesn't subscribe to the `workflow_blueprint` or
`pipeline_started` SSE events to clear the thinking state.

- **Impact:** non-fatal but creates uncertainty — the user thinks
  Agent 1 is still working when it's actually done. Visible as the
  Activity panel surfaces all the in-flight work BUT the chat panel
  remains in the "thinking" state.
- **Fix direction:** dispatch a "agent_1_done" / "pipeline_started"
  event from the SSE handler that updates chat-panel local state to
  clear the thinking indicator. Confirmed by checking the SSE event
  stream — `workflow_blueprint` event fires correctly with full
  payload but chat UI doesn't react.

### Issue #4 — Backend log emoji corruption ("�" placeholder)

Backend uvicorn logs render unicode em-dashes (—) as `�`. Cosmetic
only, but on Windows console encoding the `cp1252` mismatch produces
mojibake.

- **Fix:** set `PYTHONIOENCODING=utf-8` at uvicorn startup, or strip
  em-dashes from log strings.

### Issue #5 — Agent 3 emitted empty `coverage_summary: {}` despite prompt rule

The Phase 2D prompt says "This field MUST NOT be empty — every
sub-task must appear as a key with its integer count" (Agent 3
system_prompt.md line 456). On this real run Agent 3 emitted:
```json
{"coverage_summary": {}}
```

- **Root cause:** prompt-only rule with no schema-level enforcement.
  The model produced 5 valid TestCases but skipped the
  coverage_summary field. Per AD-007, prompts teach but code enforces
  — this rule should be a Pydantic validator on `Agent3Result`.
- **Impact:** the coverage map that downstream UIs / Agent 4 use to
  reason about per-scope coverage is empty. The system continues
  because nothing strictly requires the field, but downstream
  visualizations may degrade silently.
- **Fix direction:** add a `model_validator(mode='after')` on
  `Agent3Result` that auto-populates `coverage_summary` from
  `test_cases` if empty (group by `sub_task_ref`, count). Soft warn
  rather than reject — the data IS reconstructable from test_cases.

### Quality observations on the test cases themselves

The 5 generated tests cover the right surface — Phase 2 prompt
refactor produced excellent diversity:

| ID | Scenario | Critical-gate criteria |
|---|---|---|
| tc-001 | Happy-path emergency water-heater booking | `pricing_accuracy` critical |
| tc-002 | Out-of-service-area Bakersfield caller | `service_area_policy` critical |
| tc-003 | Out-of-scope AC repair request | `accuracy_no_hallucination` critical |
| tc-004 | Multi-turn context-retention drain clog | `context_retention` + `pricing_accuracy` critical |
| tc-005 | (5th — not yet inspected) | — |

Every test has `evaluation_mode: agentic`, `input_context.instructions`
populated with the full plumbing prompt verbatim, and 4-5
domain-specific weighted rubric criteria. **Phase 2A's F-A1 + F-A3 +
Phase 2D's compressed wording all produced the right behavior.**

### Issue #6 — Agent 5 only built 2 of 3 verified candidates

Pipeline state at 31.82 credits used:
- Agent 4 verified all 3 candidates (Retell AI, ElevenLabs
  Conversational AI, OpenAI Realtime API). All 3 had checklist passes.
- Agent 5 built only 2 harnesses: `elevenlabs_conversational_ai/` and
  `openai_realtime_api/`. No `retell_ai/` directory.
- UI "Candidates" section says "2 discovered" instead of "3", which is
  consistent with the 2 harness dirs but inconsistent with Agent 4's
  validated count of 3.

- **Hypothesis:** the "Selection applied — 3 candidates proceeding to
  screening" check happened with the 3 explicit + auto-pre-checked
  selection, but post-screening only the explicit_candidates (OpenAI
  + ElevenLabs) were forwarded to Agent 5. Retell was dropped despite
  being verified.
- Or: parallelism cap of 2 may be in effect; Retell hasn't been
  picked up yet.
- Either way, the user-facing effect is that the system honored
  "explicit_candidates" but silently dropped a verified competitor.
  Confusing if the user checked Retell on the selection panel.
- **Fix direction:** confirm whether `scope_selections: {}` (empty) on
  the Agent 4 output is the cause — that field gates which validated
  candidates get builds. Either populate from `validated_candidates`
  or surface a "Skipped — not in selection" status badge on Retell.

## In progress

- ElevenLabs harness building (Turn 3/40, ~4.4 credits used so far,
  ask_research probing `conversation_initiation_client_data` schema).
- OpenAI Realtime harness building (Turn 2/40, ~2.7 credits, doing
  read_file).
- Monitor `bzo4yd14v` armed for full pipeline lifecycle.
- Awaiting voice agent live tests on synthesized caller audio.
