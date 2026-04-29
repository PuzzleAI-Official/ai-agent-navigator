# Plugin & Credential Setup

This is the single source of truth for **where every API key goes** in
PuzzleEval. There are three credential surfaces and they hold different
kinds of secrets.

## TL;DR

```bash
# Copy the template and fill in keys you have
cp puzzleeval-api/.env.example puzzleeval-api/.env

# Print the live readiness matrix to see which plugins are READY
python -c "from puzzleeval.plugin_status import snapshot_all, format_text_table; print(format_text_table(snapshot_all()))"

# Run the pipeline — both the FastAPI backend and the CLI auto-load .env
cd puzzleeval-api && python -m uvicorn main:app --host 0.0.0.0 --port 8001
# OR
cd PuzzleEval-local && python -m puzzleeval.cli --text "I need invoice OCR" --agent5
```

## Three credential surfaces

### Surface 1: `puzzleeval-api/.env` — system-level keys

For the **agents themselves** (Anthropic) and the **plugin providers**
(OpenAI Whisper, Deepgram, AssemblyAI, ElevenLabs). These power
PuzzleEval's evaluation infrastructure.

```dotenv
# REQUIRED
ANTHROPIC_API_KEY=sk-ant-api03-...

# OPTIONAL (any subset; missing = LLM judge fallback)
OPENAI_API_KEY=sk-proj-...        # enables Whisper STT + OpenAI TTS
DEEPGRAM_API_KEY=...              # alternative STT (free tier 12k min/yr)
ASSEMBLYAI_API_KEY=...            # alternative STT (free tier 5 hr/mo)
ELEVENLABS_API_KEY=...            # alternative TTS (free tier 10k chars/mo)
```

`puzzleeval-api/.env.example` carries the full template with comments and
pricing notes. Both the FastAPI backend (`uvicorn main:app`) and the CLI
(`python -m puzzleeval.cli`) load this file automatically — no manual
exports required.

### Surface 2: `PuzzleEval-local/provider_registry.json` — candidate API keys

For the **APIs being TESTED** — Mindee, Veryfi, Stripe, Twilio,
in-house APIs, anything Agent 5 needs to call during harness building.
These are conceptually separate from system keys: PuzzleEval doesn't
need them to function; it needs them to test the providers your users
care about.

Schema (excerpt — full schema in the file):

```json
{
  "providers": {
    "mindee": {
      "tier": "free",
      "monthly_limit": 250,
      "env_vars": {"MINDEE_API_KEY": "your_actual_key_here"}
    },
    "stripe_oauth_demo": {
      "tier": "live",
      "oauth": {
        "client_id_env": "STRIPE_CLIENT_ID",
        "client_secret_env": "STRIPE_CLIENT_SECRET",
        "token_url": "https://connect.stripe.com/oauth/token",
        "scope": "read_write"
      }
    }
  }
}
```

OAuth flows reference env vars (e.g. `STRIPE_CLIENT_ID`,
`STRIPE_CLIENT_SECRET`) — those env vars can live in `.env` or be
exported manually. The `provider_registry.json` itself only carries the
NAMES of the env vars + the OAuth metadata (token URL, scope).

### Surface 3: shell `os.environ` — overrides + diagnostic flags

Anything from `.env` can also be set as a shell env var. Shell vars
**override** `.env`. Use this for:

- Per-run overrides (`OPENAI_API_KEY=sk-other-key python -m ...`)
- Diagnostic flags (`PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED=0 python -m ...`)
- CI / containerized deployment (no `.env` file; secrets injected as env)

## Plugin → key mapping

This is the single mapping you'll reference most:

| Plugin | Required key (any one) | What it's used for |
|---|---|---|
| **code_execution** | (none — Python always present) | Runs generated code in sandbox; scores by execution success |
| **vision** | `ANTHROPIC_API_KEY` | Scores image responses via Claude vision |
| **transcription** | `OPENAI_API_KEY` OR `DEEPGRAM_API_KEY` OR `ASSEMBLYAI_API_KEY` | STTs voice agent audio responses; scores against expected transcript |
| **tts** | `OPENAI_API_KEY` OR `ELEVENLABS_API_KEY` | Synthesizes audio test inputs for voice agents |
| **conversation_simulator** | (none) | Drives multi-turn chatbot tests with assertion checking |
| **webhook_receiver** | (none — local) | Captures inbound HTTP callbacks from candidate agents (Slack mention, Intercom widget, Stripe event, generic webhook). Optional `PUZZLEEVAL_TUNNEL_URL` exposes a public URL via ngrok/cloudflared for offsite candidates. |
| **outbound_delivery** | (none — local) | Mock SMTP / channel / SMS receivers that verify outbound messages actually landed. Default ports: SMTP 2525, Slack-like 8766, SMS-like 8767. |
| **voice_realtime** | (none required; STT eval needs `OPENAI_API_KEY` / `DEEPGRAM_API_KEY` / `ASSEMBLYAI_API_KEY`) | Local audio-loopback voice harness — serves a synthesized caller utterance, captures the candidate's TwiML/NCCO/JSON/audio response. Same `PUZZLEEVAL_TUNNEL_URL` trick when needed. |

When a plugin's required credentials aren't set, the plugin returns a
structured `fallback_reason` and the LLM judge takes over. Pipelines
NEVER crash on missing credentials — they degrade observably. The
`pipeline_summary.json` carries `plugin_advisories` that tell you
exactly which key to add to enable each blocked plugin.

## How to verify your setup

```bash
# Fast check: print the readiness matrix
cd PuzzleEval-local && python -c "
from puzzleeval.plugin_status import snapshot_all, format_text_table, collect_advisories
print(format_text_table(snapshot_all()))
print()
print('ADVISORIES:')
for a in collect_advisories():
    print(' -', a)
"
```

You should see `[READY]` next to every plugin you have credentials for,
and one-line advisories with copy-paste-able instructions for any
`[BLOCKED]` ones. After every pipeline run, the same data appears in
`runs/{trace_id}/pipeline_summary.json` under `plugins` +
`plugin_advisories`.

## What about .env security?

- `.env` is **not committed** — verify by running `git status` after
  editing it. If it shows up as untracked, add `.env` to your
  `.gitignore` immediately.
- Never paste an `.env` file into a chat / issue / Slack message. If
  you've shared one accidentally, rotate the keys at each provider's
  console.
- Production deployments should inject secrets as environment variables
  (Docker secrets, Kubernetes secrets, AWS Secrets Manager) — `.env`
  is for local development only.

## Adding a new plugin's credentials

When a new plugin is added (your own or an external contribution), the
plugin declares `requires_credentials: list[str]` in its
`PluginCapabilities`. The status reporter automatically picks up the
new credential names and the `format_text_table()` output shows them.
Add the keys to your `.env` and re-run — no other config changes
needed.

---

## "If I just put the keys in `.env`, does it just work?"

**Yes**, given:

1. **Dependencies are installed.** Run `pip install -e PuzzleEval-local/`
   once after pulling the latest. The new dependencies (`python-dotenv`,
   `requests`) are required for the auto-loader and the plugin HTTP
   calls. Without them:
   - `python-dotenv` missing → CLI silently skips `.env` and your keys
     won't be visible.
   - `requests` missing → STT / TTS / OpenAPI fetch plugins all crash
     on first network call.

2. **Keys are in the right `.env`.** It must be `puzzleeval-api/.env`
   (the path the FastAPI backend loads, and the path the CLI's
   auto-loader walks up to from any working directory in the repo).

3. **Required key is set.** `ANTHROPIC_API_KEY` is the only mandatory
   one. Plugin keys (Whisper / Deepgram / ElevenLabs) are optional —
   each plugin returns `fallback_reason="no_provider"` and the LLM
   judge takes over when the key isn't there.

After those three, the chain is fully wired:

```
.env loads → plugin.is_available() returns True → modality detector
picks plugin by (input_type, output_type) → Agent 5 evaluator dispatches
to plugin.evaluate_output() → score recorded with tools_used
```

For audio scopes specifically, the chain extends through Agent 3 +
Agent 3F:

```
Agent 1 declares ScopeTestSpec.output_type="audio_content" →
  Agent 3 generates test cases with spoken text in input_data →
    Agent 5 staging calls TTS plugin to synthesize real audio file →
      candidate harness uploads audio →
        candidate API returns audio response →
          transcription plugin STTs response →
            scored against expected text
```

For code scopes:

```
Agent 1 declares output_type="code" →
  Agent 3 generates {expected_function, test_inputs, test_outputs}
  in expected_output (Plugin-shaped section in Agent 3 prompt) →
    Agent 5 evaluator dispatches to code_execution →
      runs in sandboxed subprocess →
        ASSERT_PASS / ASSERT_FAIL parsed back into score
```

For chatbot scopes:

```
Agent 1 declares input_type="conversation" →
  Agent 3 generates {conversation_script: {user_turns, assertions}} →
    Agent 5 evaluator dispatches to conversation_simulator →
      replays each turn through the candidate harness →
        per-turn assertions checked, weighted score
```

If anything goes wrong:

```bash
# Check the readiness matrix
python -c "from puzzleeval.plugin_status import snapshot_all, format_text_table; print(format_text_table(snapshot_all()))"

# After a run, the summary carries advisories
cat puzzleeval-api/runs/{trace_id}/pipeline_summary.json | grep -A 5 plugin_advisories
```

Both surface the exact env var to set to enable any blocked plugin.

### What doesn't auto-magically work

These still require manual setup beyond just adding keys:

- **Provider candidate keys** (Mindee, Veryfi, Stripe etc.) go in
  `provider_registry.json`, not `.env`. They're for the things being
  TESTED, not for PuzzleEval's tools.
- **OAuth flows** require both the registry entry (with `oauth.token_url`)
  AND the `client_id_env` / `client_secret_env` env vars set somewhere
  (`.env` is fine).
- **Long-running operations** (video encoding > 10 min) need
  `PUZZLEEVAL_AGENT5_CODE_TIMEOUT_LONG` and `PUZZLEEVAL_AGENT6_TEST_TIMEOUT_LONG`
  bumped up if the API SLA exceeds 10 minutes.
- **Code execution for languages other than Python** needs the host
  toolchain installed (`node`, `tsx`, `go`, `rustc`, `bash`). Plugin
  reports `missing_toolchain_<lang>` when absent.

---

## Performance + reasoning knobs (optional)

Four toggles to tune cost / latency / quality. Default values shipped
are safe; flip them as your workload demands.

```dotenv
# Effort (low/medium/high/xhigh/max) — applies to every adaptive-thinking
# call. Soft guidance to Claude on how much to think per request.
# PUZZLEEVAL_EFFORT=high

# Hybrid evaluator — for AMBIGUOUS-modality test cases (output_type=
# free_text but response is code, etc.), run a second-look pass with
# all 5 plugins exposed as Claude-callable tools + adaptive thinking.
# Modality-clear cases stay deterministic.
# PUZZLEEVAL_HYBRID_EVAL_ENABLED=0

# Programmatic tool calling — Agent 5 builder writes Python that
# chains tool calls in one container. Estimated 30-50% savings on
# builder cost + latency for multi-step builds.
# PUZZLEEVAL_PROGRAMMATIC_TOOLS=0
```

**When to flip each:**

- **`EFFORT=low`** — fast iteration loops where latency matters more than precision
- **`EFFORT=xhigh` or `max`** — final pre-launch eval runs where quality dominates
- **`HYBRID_EVAL_ENABLED=1`** — when test responses don't match their declared `output_type` cleanly and you want plugin assistance
- **`PROGRAMMATIC_TOOLS=1`** — once Agent 5 builder runs cleanly in your environment, opt in to the faster path

---

## Cost, resilience, and DoS knobs (production)

These environmental defaults are already sane for local development. Raise / lower them per workload. The canonical reference with every knob's default value is `puzzleeval-api/BACKEND_ARCHITECTURE.md` §19.

```dotenv
# Hard USD cap per run — circuit-breaker trips to HTTP 402 / pipeline_failed
# with reason="budget_exceeded" when crossed. Raise for real Agent 5 runs
# against expensive providers; lower during development.
# PUZZLEEVAL_MAX_RUN_COST_USD=25.0

# Anthropic client tuning. Default 120 s covers deep thinking without hanging
# the pipeline for 10 min on a stalled TCP socket. max_retries handles
# transient 5xx / connection drops at the SDK level.
# PUZZLEEVAL_ANTHROPIC_TIMEOUT_S=120
# PUZZLEEVAL_ANTHROPIC_MAX_RETRIES=3

# Upload cap — per-file HTTP 413 threshold. 100 MiB is generous for real
# invoice/document PDFs; lower for public-facing deploys to reduce DoS risk.
# PUZZLEEVAL_MAX_UPLOAD_BYTES=104857600

# Plugin port overrides — change only if the default ports conflict with
# something else on your host. See BACKEND_ARCHITECTURE.md §19 for the full
# list (webhook 8765, smtp 2525, slack mock 8766, sms mock 8767, voice 8768).

# Frontend: point at a different backend URL without rebuilding the SPA
# VITE_API_BASE=http://localhost:8001/api
```
