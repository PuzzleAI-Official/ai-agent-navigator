# PuzzleEval

**An AI agent evaluation platform.** Describe what you need AI to do; we find the real providers that can do it, build live test harnesses for each, run them against representative test data, and deliver a ranked comparison with per-scope winners, monthly-cost projection, and evidence for every claim.

## What's in this repo

| Directory | Role |
|---|---|
| `PuzzleEval-local/` | Python core — 5-agent pipeline, 8 tool plugins, schemas, validators, resilience infrastructure. `~26.6K LoC`. |
| `puzzleeval-api/` | FastAPI backend — REST endpoints, SSE streaming, run manager, pipeline orchestrator. `~2.3K LoC`. |
| `src/` | React + TypeScript frontend (Vite) — chat UI, workflow diagram, coverage matrix, selection panel, live cost meter, evaluation report renderer. `~12K LoC`. |

**Total production code:** ~40,900 lines. **Test suite:** 885 tests passing across core + API + generalizability bench.

## 30-second quickstart

```bash
# 1. Put your Anthropic key in puzzleeval-api/.env
cp puzzleeval-api/.env.example puzzleeval-api/.env
# edit: ANTHROPIC_API_KEY=sk-ant-api03-...

# 2. Install the Python package (once)
pip install -e PuzzleEval-local/

# 3. Install frontend dependencies (once)
npm install

# 4. Start the backend (terminal 1)
cd puzzleeval-api && python -m uvicorn main:app --host 0.0.0.0 --port 8001

# 5. Start the frontend (terminal 2)
npm run dev   # Vite on http://localhost:8080

# 6. Visit http://localhost:8080/playground
#    Describe your need → pick providers → watch live evaluation → read report.
```

## The pipeline at a glance

```
User describes need (+ optional files)
       │
       ▼
  Agent 1 (user_understanding)   → sub-tasks + workflow blueprint + test plan
       │
       ├──────────────┬──────────────┐
       ▼              ▼              ▼
  Agent 2           Agent 3      Agent 3F
  (research)    (synthetic    (file-based
  finds real      tests)       tests, only
  candidate      generates    when files
  providers       test cases   uploaded)
       │
       ▼
  [ user pauses at SelectionPanel — pick candidates per scope ]
       │
       ▼
  Agent 4 (screening + deep verify)  → scope-by-scope coverage confirmed
       │
       ▼
  Agent 5 ×N (implement_test_env)    → one harness per candidate, runs all tests
       │
       ▼
  EvaluationReport assembler         → winner, per-scope winners, evidence,
                                       monthly cost projection, pros/cons
```

## Documentation index

Start with the first two if you're onboarding; the rest are deep references.

| Doc | What's in it |
|---|---|
| [`PuzzleEval-local/CLAUDE.md`](PuzzleEval-local/CLAUDE.md) | Current state summary + recent session history. Canonical "what changed most recently." |
| [`PuzzleEval-local/ARCHITECTURE.md`](PuzzleEval-local/ARCHITECTURE.md) | Python-layer architecture. Agent-by-agent spec + module index + SSE event catalog + honest gap list. |
| [`puzzleeval-api/BACKEND_ARCHITECTURE.md`](puzzleeval-api/BACKEND_ARCHITECTURE.md) | FastAPI surface. Endpoint reference + SSE events + resilience infrastructure + env-var reference. |
| [`PuzzleEval-local/PLUGIN_KEYS.md`](PuzzleEval-local/PLUGIN_KEYS.md) | Every API key — where it goes, what it enables. |
| [`AGENT_REFINEMENT_ROADMAP.md`](AGENT_REFINEMENT_ROADMAP.md) | Original 10-phase plan (all phases shipped). Preserved as design record. |
| [`PuzzleEval-local/POST_ROADMAP_ENHANCEMENTS.md`](PuzzleEval-local/POST_ROADMAP_ENHANCEMENTS.md) | Everything shipped AFTER the original roadmap — plugin ecosystem, hybrid evaluator, production resilience audit pass. |
| [`PuzzleEval-local/GAP_ANALYSIS.md`](PuzzleEval-local/GAP_ANALYSIS.md) | Older gap list — superseded by the "Known gaps" section in the roadmap + post-roadmap docs. |

## What ships today

### Pipeline capabilities

- **Multi-agent orchestration** — Agents 1→5 with parallel branches, user-selection pause, deep scope verification.
- **5-action data sufficiency verdict** per file-requiring scope: READY / AUGMENT / SYNTHESIZE / REQUEST_MORE / DEGRADE.
- **8 tool plugins** covering code execution, vision, transcription, TTS, multi-turn conversation, inbound webhooks, outbound delivery (SMTP / channel / SMS), and voice-realtime audio loopback.
- **Modality-driven deterministic dispatch** by `(input_type, output_type)` schema enums — no hardcoded role branches.
- **Hybrid evaluator** (opt-in) for ambiguous modalities — exposes plugins as Claude-callable tools.
- **Cross-run memory** via `puzzleeval/memdir/` — provider atlas + API-spec cache with 14-day TTL.

### Production infrastructure

- **Central Anthropic client factory** — 120 s timeout, `max_retries=3`, Opus→Sonnet→Haiku fallback ladder.
- **Cost circuit-breaker** — `PUZZLEEVAL_MAX_RUN_COST_USD` (default $25) stops runaway spending via HTTP 402 / `pipeline_failed`.
- **Structured-output grammar fallback** — `parse_with_fallback()` survives Anthropic's compiled-grammar 400s; wrapped around all 6 structured-output call sites.
- **FastAPI lifespan plugin teardown** — HTTP/SMTP servers close cleanly across `uvicorn --reload`.
- **DoS protection** — stream-read upload cap + SMTP per-line + total DATA caps.
- **`.env` autoload with empty-shadow defense** — empty-string placeholders don't silently shadow real values.

### Frontend

- **SSE auto-reconnect** with exponential backoff + `lastEventId` preservation.
- **Live cost meter** driven by `cost_update` events between agent boundaries.
- **Stuck-pipeline banner** when > 2 min silent during the pipeline stage.
- **Structured `EvaluationReportCard`** rendering winner, per-scope winners, coverage %, advisories, ranked candidates with pros/cons + failure evidence.
- **`VITE_API_BASE` env override** to retarget a different backend without rebuilding.

## Running the test suite

```bash
# Core tests
cd PuzzleEval-local && python -m pytest tests/ -q

# API tests
cd puzzleeval-api && python -m pytest tests/ -q

# Generalizability bench (cassette-replay, cheap)
cd PuzzleEval-local && python -m pytest -m generalizability tests/generalizability/ -q
```

Current: **816 + 30 + 39 = 885 tests passing.** Test-to-production ratio: ~34%.

## Running in real mode

Every agent can be toggled `mock` / `real` independently:

```bash
# Mock everything — free, no API calls, exercises the pipeline end-to-end
curl -s -X POST http://localhost:8001/api/runs \
  -H "Content-Type: application/json" \
  -d '{"text": "...", "agent_modes": {"agent1":"mock","agent2":"mock","agent3":"mock","agent4":"mock","agent5":"mock"}}'

# Full real run (~$5-7 per run depending on workload)
curl -s -X POST http://localhost:8001/api/runs \
  -H "Content-Type: application/json" \
  -d '{"text": "...", "agent_modes": {"agent1":"real","agent2":"real","agent3":"real","agent4":"real","agent5":"real"}}'
```

Then POST the user's request to `/api/runs/{run_id}/chat` and subscribe to `/api/runs/{run_id}/events` for the SSE stream.

## Known gaps (honest)

1. Agent 5 mid-turn cancellation (stops at agent boundaries, not inside the 25-turn loop)
2. Agent 5 Opus→Sonnet model fallback (helper exists, wiring pending)
3. Live `agent_thinking` SSE streaming (blocks produced, not surfaced)
4. Incremental token streaming (Agent 5 uses blocking `messages.create`)
5. Agent 2 per-scope parallelism (currently serial)
6. In-run web_fetch URL cache (re-fetch per candidate)
7. Idempotency keys + DRY_RUN propagation on writes
8. Provider-quirk registry (Stripe-Version, OpenAI-Beta headers)
9. AWS SigV4 / OAuth2 auth_code / mTLS auth patterns

Full detail in [`PuzzleEval-local/POST_ROADMAP_ENHANCEMENTS.md`](PuzzleEval-local/POST_ROADMAP_ENHANCEMENTS.md) §22. Closing all 9 is ~1 focused day — none are architectural.

## License

Proprietary — all rights reserved.
