# PuzzleEval — Project Context for AI Assistants

> This file provides complete context for any AI assistant working on this codebase.
> Read this FIRST before making any changes.

## What Is PuzzleEval

An AI agent evaluation platform. Users describe what they need AI to do in plain English. We find relevant AI solutions, test them with synthetic data, and return a verdict on **Performance**, **Speed**, **Price** — nothing else.

Target users: SMBs (small/medium businesses) who are overwhelmed by AI options and don't have the technical ability to evaluate them.

## Current State (as of 2026-04-21)

**Production-ready for local hosting. Full voice pipeline verified end-to-end
on real APIs (OpenAI Realtime + ElevenLabs Conversational AI).
907 tests passing (831 core + 37 API + 39 generalizability bench).
Zero regressions. TypeScript + Vite clean.**

> **NEW-AI — Voice end-to-end success + runner-level safety nets + web-tools revert** (this session, documented below). Previous passes' sections (NEW-AH, NEW-AG, etc.) are preserved below in chronological reverse order.

### NEW-AI — Voice end-to-end success + runner safety nets + web-tools revert (2026-04-21)

Four sessions of "agent audio missing" ended here with real agent MP3 files
landing on disk for the first time across every test case. The pattern across
prior sessions was the same symptom (0 agent audio) with shifting root
causes; each session fixed the surface and the next exposed a new one. This
session found the REAL blocker (harness rejecting calls without
`input_context.instructions` — two prompt-level rules that Claude didn't
reliably follow) and moved the fix into deterministic code at the runner
layer. Also reverted the 20260209 web-tools upgrade after real-run traces
exposed multiple failure modes, shipped nine cumulative fixes total, and
captured an honest architectural plan for Phase 4 (Agent 5 as integration
engineer — documented below but NOT yet implemented).

**Final real-run evidence (trace 28cb2648):**
- Both harnesses built ($3.91 OpenAI / $10.55 ElevenLabs)
- **33 agent MP3 files for OpenAI, 21 for ElevenLabs** (first session with
  non-zero `response_*` files on disk across every candidate)
- Per-turn scoring populates `audio_paths` with `role: agent` entries
- 1/8 tests fully passed ElevenLabs, per-test scores 0.25-0.5 (limited by
  OT-001 substring-match quality, NOT by pipeline correctness)
- `conversation_*.mp3` files now play end-to-end with uniform sample rate
  (fixed mid-session after the user reported "only 4 seconds then pauses")

**Capability fix 1 — 20260209 web-tools REVERT (removed everything we did to make them work).**

Earlier in this session we upgraded `web_fetch_20250910` → `web_fetch_20260209`
and `web_search_20250305` → `web_search_20260209` to gain dynamic filtering
(~24% fewer input tokens, code execution free when paired). Real-run traces
exposed multiple failure modes that outweighed the benefit:

- **400 "container_id is required" cascades.** The 20260209 web tools
  auto-enable code_execution; once its sandbox has pending tool uses, every
  subsequent call in the conversation must pass `container=<id>`. Missing it
  on ANY sub-agent (Agent 5 builder, Agent 4 verify, ask_research) produced
  hard 400s. ElevenLabs build died this way in trace d3b49875.
- **Agent 2 silent hang.** Agent 2 uses the non-beta `client.messages.create`
  entrypoint. Passing `container=<id>` there hung the socket silently (8+
  min stall with zero logs). Observed: run 31f1af7c.
- **3-5 min sandbox spin-up latency** per Agent 2 real-research call with
  dynamic filtering enabled — inflated wall-clock with no corresponding
  quality gain on our discovery workload.

Reverted everything:
- `web_fetch` / `web_search` back to `20250910` / `20250305` in
  `research.py`, `screening.py`, `implement_test_env.py` (main + ask_research
  sub-agent)
- Removed `container_id` threading from Agent 5 builder loop, Agent 4
  verify continuations, Agent 2 research continuations, ask_research
  sub-agent
- Restored explicit `code_execution_20260120` declaration in
  `_build_tools_with_programmatic` (basic web tools don't auto-inject, so
  the explicit tool is required for plugin-chain `allowed_callers` to work)
- Regression guard class `TestNoContainerThreadingAfterRevert` asserts NO
  `_kwargs_*["container"] =` patterns in active code and NO
  `"type": "web_fetch_20260209"` in active tool definitions

If anyone ever wants to re-try 20260209, the revert-guard tests point them
at `research.py`'s top-of-file comment block which enumerates the real-run
failure modes and the full migration checklist required.

**Capability fix 2 — Runner-level default input_context (THE actual multi-session blocker).**

The REAL reason agent audio kept missing across prior sessions: the
OpenAI Realtime + ElevenLabs Conversational AI harnesses both hard-fail
when called without `input_context["instructions"]` (or any of the 5
accepted aliases: `instructions`, `system_prompt`, `system`, `brief`,
`agent_prompt`). Root cause chain:

1. Agent 3's prompt rule says voice tests MUST populate
   `input_context.instructions`. In real runs (trace f1312253), Agent 3
   emitted `input_context: null`. Rule violated.
2. Agent 5's builder prompt teaches a `DEFAULT_INSTRUCTIONS` fallback
   inside the harness. In real runs, the OpenAI harness Agent 5 wrote
   contained literally `if not instructions: return _fail("missing
   system prompt in input_context", t0)` with NO fallback. Rule violated.
3. The plugin's `drive_conversation` calls harness.run() with
   `{audio_url, turn_index, session_state}` — no `input_context`.
4. The runner closure DID merge `tc.input_context` from the test case,
   but `tc.input_context` was null → no merge → harness 400s on every turn
   → plugin sees empty audio → zero response files saved.

Two prompt-level rules that both failed adherence. Fix at the runner
layer (our code, deterministic, not dependent on Claude):

- `_SYSTEM_PROMPT_ALIASES = ("instructions", "system_prompt", "system",
  "brief", "agent_prompt")` — mirrors the accepted-key list both
  observed harnesses use. Adding a new alias = 1-line change.
- `_default_input_context(candidate_name, scope_role)` returns a dict
  populating EVERY alias with a candidate-aware default
  ("You are a helpful {scope_role} for {candidate_name}. …").
- `_merge_with_default_input_context(test_case_ctx, default_ctx)` —
  handles three real-world cases cleanly:
  1. `test_case_ctx is None` → full default
  2. Test has other keys but no system-prompt field (e.g.,
     `{"persona_name": "Vera"}`) → preserve original keys + fill all
     system-prompt aliases from default
  3. Test has its own system prompt under one alias → propagate that
     value to every other alias so harnesses reading different names
     all see the user's intent
- BOTH runner closures in `_build_single_harness` now route through
  `_merge_with_default_input_context` — the deterministic gate.

Regression guards: `TestDefaultInputContextFallback` class with 4 tests
covering the alias list, null case, partial-other-keys case,
partial-with-prompt case (with propagation across all aliases).

**Capability fix 3 — Voice plugin handles THREE audio return shapes.**

Each prior session, a different harness chose a different valid return
shape for `raw_response` and the plugin had no handler → silent
fall-through → 0 agent audio. This session consolidated:

- **Shape A (inline bytes):** `raw_response.audio_bytes: <bytes>` — the
  pre-existing path, works unchanged.
- **Shape A-variant (base64 STRING):** `raw_response.audio_bytes: "<b64
  str>"` — what the OpenAI Realtime harness emits for JSON-safe
  subprocess marshaling. pydub silently mishandles strings (accepts at
  construction, fails at `.export()`), wave raises TypeError. Fix:
  decode at the TOP of the pcm16 branch BEFORE pydub/wave touch the
  payload.
- **Shape B (on-disk file path):** `raw_response.audio_path: "/tmp/x.wav"`
  — what a later OpenAI harness chose, saving audio via pydub/ffmpeg to
  a temp file first. Fix: plugin responder reads the file into bytes,
  derives content_type from the extension, continues down the bytes path
  so `_save_audio_blob` sees a uniform contract.

Regression guards: `TestVoicePCM16StringNormalization` (3 tests) +
`test_responder_handles_audio_path_harness_return_shape` — cover both
new shapes with unit-level verification (no real API needed).

**Capability fix 4 — Conversation merger normalizes sample rates via pydub.**

The byte-concat merger (shipped in NEW-AH) produced files that played
the first segment then paused. Root cause: caller TTS produces MP3 at
44.1 kHz; OpenAI Realtime PCM16 → pydub MP3 produces agent files at 24
kHz. Byte-concat writes the FIRST frame's metadata (44.1 kHz) but
subsequent frames have a different sample rate → players halt at the
first inconsistency. User reported "only 4 seconds then pauses."

Fix: `_try_merge_via_pydub(ordered_paths, out_path, ext)` new method.
Decodes each per-turn file into an `AudioSegment`, normalizes every
segment to the FIRST segment's frame_rate + channel count (preserves
caller TTS quality), concatenates via `AudioSegment` `+` operator,
exports as a uniformly-encoded MP3. Byte-concat path stays as fallback
when pydub/ffmpeg unavailable. Regression guards:
`TestConversationMergeNormalizesSampleRate` (3 tests).

Also re-merged 16 existing conversation files from run 28cb2648 in
place to fix the broken playback immediately (no pipeline re-run
needed). All verified uniformly 44.1 kHz mono after repair.

**Capability fix 5 — Voice harness return-shape contract (Option A).**

Formalized the harness-to-plugin contract in the builder prompt so
future harnesses pick from the 2 supported shapes instead of inventing
a 3rd. Injected via the conditional placeholder pattern:

- New constant `_VOICE_HARNESS_CONTRACT` — explicit contract listing
  Shape A (inline bytes) + Shape B (on-disk file path), required fields,
  forbidden keys (`audio_url`, `audio_b64`, `audio_data`, etc.), and the
  hard rule "raw_response MUST contain EITHER audio_bytes OR audio_path,
  never both."
- `_modality_specific_contract(test_cases)` returns the voice contract
  when test cases include voice/conversation/audio modalities, empty
  string otherwise. Non-voice builds don't pay the prompt-token tax.
- `__MODALITY_CONTRACT__` placeholder in `BUILDER_SYSTEM_PROMPT` — same
  injection pattern as `__OS_SPECIFIC_RULES__`.
- `_render_builder_prompt(template, test_cases)` — unified renderer
  that fills all three placeholders (`__OS_TYPE__`,
  `__OS_SPECIFIC_RULES__`, `__MODALITY_CONTRACT__`).

**Forward-compat note:** this injection pattern IS the structural
predecessor of a skills-style per-modality loader (see AD-002 revisit
trigger). When modality count crosses ~8, convert
`_VOICE_HARNESS_CONTRACT` to a `skills/voice.md` file and
`_modality_specific_contract` to a file-reading router — no call-site
changes. Written in the shape we want to migrate to.

Regression guards: `TestModalityContractInjection` class (5 tests) —
contract content, voice-tests inject, non-voice-tests don't,
unknown-platform safe, OS-and-modality compose independently.

**Capability fix 6 — Explicit-candidate boost actually persists.**

Real-run trace d3b49875: user said "Compare OpenAI and ElevenLabs voice
stacks", Agent 1 correctly captured `explicit_candidates=["OpenAI",
"ElevenLabs"]`, Agent 2 found both but scored them low
(OpenAI=0.445, ElevenLabs=0.61). Default picks chose 3 higher-ranked
packaged products; user had to manually flip 5 checkboxes to test what
they asked for.

Root cause: `inject_explicit_candidates` in `research.py` only INJECTED
new synthetic candidates when an explicit name wasn't found — when
found, it was a no-op. The function now also BOOSTS existing
candidates' `relevance_score` to ≥ 0.95 on substring match.

Second-half fix: `pipeline_runner.py::_branch_a_research_and_screening`
only saved state to `state.agent2_result` when `len(injected) >
len(original)`. The boost changes SCORES without adding rows, so the
boost ran in memory but was silently discarded. Fix: track
`boosted_names` separately, save state on EITHER new candidates OR
changed relevance_scores.

Regression guards: `TestExplicitCandidateRelevanceBoost` (2 tests) +
`test_pipeline_saves_boosted_state_even_without_new_candidates`.

**Capability fix 7 — Windows OS-conditional rules + venv pre-install.**

Real-run trace d3b49875 showed 7 Agent 5 env-setup turns ($1.36 wasted)
on Unix muscle-memory commands failing on Windows (`tail`, `head`,
piped `grep`, `&`, etc.) plus sequential package-import probes. Two
general fixes:

- `_OS_RULES_WINDOWS` / `_OS_RULES_LINUX` / `_OS_RULES_MACOS` constants
  injected via `__OS_SPECIFIC_RULES__` placeholder based on
  `sys.platform`. Windows gets the full Unix→Windows translation table
  + `python -c` silent-output trap warning; Linux/macOS get shorter
  POSIX acknowledgments; unknown platforms get empty block.
- `VENV_PREINSTALL_MANIFEST = ["requests", "websocket-client", "pydub",
  "soundfile", "numpy", "python-dotenv"]` — 6 packages 85%+ of harnesses
  install anyway. `_preinstall_venv_deps` runs `pip install --quiet
  <manifest>` right after venv creation. Gated behind
  `PUZZLEEVAL_VENV_PREINSTALL=0` for debug runs.
- Builder prompt advertises the pre-installed packages explicitly so
  Claude doesn't redundantly verify them.

Regression guards: `TestEfficiencyHardening_RealRun_d3b49875` class
(7 tests) — OS translation table, no-`python -c`-retry rule,
env_check.py pattern, parallel-write rule, venv manifest, env-var
toggle, prompt advertisement.

**Capability fix 8 — Parallel writes + env_check.py pattern in builder prompt.**

OS-agnostic prompt additions to cut typical build-time sequential writes
(3 separate `write_file` turns for `requirements.txt` + `harness.py` +
`smoke_test.py` = $1.04 wasted) down to 1 parallel turn; and to cut
5-turn package-probe sequences down to 2 (write `env_check.py` + run).
Included escape hatch language ("guideline, not mandate") so the rule
doesn't over-constrain builds where sequential writes are genuinely
needed.

**Architectural decision — Phase 4 / Agent 5 as integration engineer (planned, NOT yet implemented).**

The honest architectural weakness exposed this session: Agent 5's
`HARNESS_COMPLETE` signal is misleading because the builder writes its
OWN live_test.py (with whatever payload shape it likes) rather than
calling the harness with the EXACT payload the plugin's
`drive_conversation` will send in production. That's why every session
found a new production-only bug even though the builder reported
"live test PASSED".

User's proposed fix (agreed as next-session work):

- **Phase 4 — REAL TEST PROBE** inserted between Phase 3 (live API
  validation) and `HARNESS_COMPLETE`
- Agent 5's sandbox gets `agent_3_test_cases.json` staged as a file
- Agent 5's sandbox gets `_production_shape_helper.py` auto-seeded with
  `derive_production_payload(test_case)` + `synth_caller_audio_url(text)`
- Builder MUST: read a real Agent 3 test case, build the production-shape
  payload, call harness, verify success=True with expected audio shape
- If the probe fails, the builder patches the HARNESS (never the test
  data) until it passes
- `_run_verification_checks` gains a deterministic check for
  `phase_4_passed.txt` written by the successful probe — bypass
  impossible

The runner-level default (capability fix 2 above) stays in place as
defense-in-depth even after Phase 4 lands. Belt AND braces.

**Session test growth:** 784 → 831 core tests (+47 regression guards).
API: 37 tests unchanged. Generalizability: 39 unchanged. Zero
regressions across the session.

**Real runs this session:**
- `d3b49875` — first E2E with 20260209, exposed container_id + PCM16 bugs
- `4068e872` — second E2E, exposed audio_path shape + Agent 2 hang
- `5a59acbc` — third E2E after revert, exposed input_context blocker
- `f1312253` — fourth E2E with Option A contract, still 0 audio (revealed
  the DEEPER input_context issue)
- `28cb2648` — **FIFTH AND FINAL** — agent MP3s finally land end-to-end
  after runner-level default + merger fix (total spend ~$60 across the
  session; this one was $15.54 of that)



### NEW-AH — Voice playback fix + thread-local isolation + Agent Skills decision

After NEW-AG shipped the merged conversation feature, a listen-back pass
caught a real playback bug + a dual-provider isolation bug, AND a strategic
decision was made about Agent Skills vs our own playbook system. All three
matter for future sessions; all three are general-purpose fixes, not
scenario-specific.

**Capability fix 1 — ID3-tag-aware MP3 merger.**
The NEW-AG byte-concat merger produced files the OS sized correctly but
most players (Windows Media Player, browser `<audio>`, QuickTime) only
played the FIRST segment — the caller voice — because the leading
ID3v2 tag declared a 3-second duration and players honored it. Real-run
signal: voice_v3 replay produced a 290 KB conversation file that played
as only ~3 seconds of caller audio, cutting off before Vera's response.
General fix: `_strip_id3v2_header` (synchsafe-size decode) +
`_strip_id3v1_trailer` (TAG signature) module-level helpers. The first
segment keeps its ID3v2 (needed for codec init); segments 2…N are
stripped before append. Mid-stream ID3v1 trailers stripped from every
segment to avoid decoder confusion. Pure-Python; no ffmpeg/pydub
dependency. Fallback still drops the merge and keeps per-turn files
if MP3 parsing fails. Tests:
- `test_id3v2_strip_handles_real_openai_tts_header`

**Capability fix 2 — thread-local session_dir for parallel candidates.**
Agent 5 runs candidates in parallel via ThreadPoolExecutor. Each worker
called `plugin.set_session_dir(<sandbox>/voice/)`. But the plugin is a
MODULE-LEVEL SINGLETON — two workers writing to `self._session_dir`
caused last-setter-wins. Real-run signal: voice_dual_6 showed OpenAI
Voice Stack's audio paths pointing to `elevenlabs_voice_stack/voice/`
because ElevenLabs's worker happened to call set_session_dir second.
General fix: `_session_dir` became a property backed by
`threading.local()`. Each worker reads its own value; writes stay
thread-isolated. `set_session_dir()` signature unchanged — call sites
didn't need updating. Tests:
- `test_voice_plugin_session_dir_is_thread_local` (real two-thread race
  with a Barrier asserting each thread reads its own dir)

**Capability fix 3 — bytes-safe round-trip for harness output.**
Voice harness returned raw `bytes` in `raw_response.audio_bytes`. The
subprocess driver's `json.dump(..., default=str)` stringified them as
`"b'\\xff\\xfb...'"` (useless Python repr). Plugin's
`isinstance(audio_bytes, bytes)` check then failed → agent audio
silently lost. General fix at THREE layers:
1. Driver script (`_execute_single_test`) encodes bytes as
   `{"_b64": "<base64>"}` sentinels before `json.dump`.
2. Agent 5's new `_inflate_b64_sentinels` helper walks the loaded
   JSON and re-inflates sentinels to bytes before returning.
3. Voice plugin's `_extract_agent_text_and_path` ALSO accepts a
   base64 string directly (belt-and-braces for older harnesses that
   pre-encoded themselves).
Harnesses keep returning raw bytes; the plugin keeps reading raw bytes;
the JSON border is transparent. Tests:
- `test_execute_single_test_round_trips_bytes_via_b64_sentinel`
- `test_exec_script_encodes_bytes_with_b64_sentinel`
- `test_voice_plugin_extracts_audio_from_base64_string`

**Capability fix 4 — `audio_format` → `audio_content_type` derivation.**
Voice harnesses commonly return `raw_response = {"audio_bytes": <bytes>,
"audio_format": "mp3"}` WITHOUT explicit `audio_content_type`. Plugin's
`_responder` defaulted to `"audio/wav"` → MP3 bytes got saved as `.wav`
files (corrupted playback) AND the merger refused mixed-extension
concat (caller `.mp3` vs agent `.wav`), producing caller-only merges.
General fix: derive content_type from audio_format via a lookup table
(`mp3`/`mpeg`/`mpga` → `audio/mpeg`, etc.). Test:
- `test_voice_plugin_responder_derives_content_type_from_audio_format`

**Architectural decision — Agent Skills vs our own playbook system.**
Strategic investigation (NOT a code change — documented for future-us):
the question was whether the growing Agent 5 builder prompt (~2500
lines, 10+ modalities incoming) should migrate to Anthropic's Agent
Skills feature or keep expanding our monolithic prompt.

Read carefully:
- `platform.claude.com/docs/en/agents-and-tools/agent-skills/*` (overview, quickstart, best-practices, enterprise)
- `platform.claude.com/docs/en/build-with-claude/skills-guide`
- `platform.claude.com/docs/en/agents-and-tools/remote-mcp-servers`
- `platform.claude.com/docs/en/agents-and-tools/mcp-connector`
- `platform.claude.com/cookbook/skills-notebooks-01-skills-introduction`
- `anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills`

**Finding**: Agent Skills via the Messages API REQUIRE (a) `container`
parameter, (b) `code-execution-2025-08-25` beta, (c) `skills-2025-10-02`
beta, (d) `files-api-2025-04-14` beta. Skills execute inside
Anthropic's sandbox, referenced by `skill_id` from Anthropic's
registry (not file paths in our repo). There is NO documented pattern
for loading skill markdown into `system=[...]` without container +
code execution. The cookbook is explicit: "Skills require the code
execution tool to be enabled."

**Blocker for adopting Skills natively**: PuzzleEval's harness
execution model sends user credentials (Mindee keys, ElevenLabs keys,
OpenAI keys, etc.) into local subprocess venvs. Migrating harness
execution to Anthropic's `container` sandbox would require sending
those credentials through Anthropic's infrastructure — incompatible
with our bring-your-own-keys self-hosted security model.

**Decision (three-way tradeoff evaluated)**:

| Path | Status |
|---|---|
| (A) Native Agent Skills via `container` + betas | **Rejected** — incompatible with local-subprocess credential model |
| (B) Our own skill-format playbook files + deterministic router | **Deferred** — format stability risk, migration benefit weaker than initially claimed |
| (C) Extend existing conditional in-prompt gating pattern | **Adopted** — simplest, no new infrastructure, reversible |

**Current pattern (C) already ships**: Agent 5's `_build_initial_message`
already injects the `MULTI-CALL HARNESS CONTRACT` only when
`input_type ∈ {conversation, voice_conversation, voice_turn}`. When
adding new modalities (code-gen, fine-tuning, streaming), extend the
same pattern: `_build_modality_section(input_type, output_type) ->
str | None` — returns the right ~60-line section or None. One prompt
file, clean per-modality boundaries, no loading/routing infrastructure.

**Revisit triggers** (flip to path B or A):

1. **Flip to (B) playbook files + router**: when (i) modality count
   reaches ~8 and one-prompt-with-sections becomes a merge-conflict
   magnet, OR (ii) different engineers need to own different modality
   playbooks independently. The playbook structure: one `.md` file
   per modality with `description:` frontmatter matching Anthropic's
   skill file format (for soft forward-compat). Router is a deterministic
   `input_type → playbook_path` lookup.

2. **Flip to (A) native Skills**: when (i) PuzzleEval pivots to SaaS
   (credentials already flow through our infrastructure so container
   isn't a security regression), OR (ii) Anthropic ships a
   "skills as system-prompt injection" mode that doesn't require
   `container` + code execution.

**Worth watching**: skill format is described by example in the docs,
not formally spec'd. Anthropic hasn't published stability guarantees.
Designing our playbooks in skill format today is NOT as
forward-compatible as I initially argued — there's reverse-engineering
risk. Real decision cost, not zero.

**Tests (this pass):**
- `test_id3v2_strip_handles_real_openai_tts_header`
- `test_voice_plugin_session_dir_is_thread_local`
- `test_execute_single_test_round_trips_bytes_via_b64_sentinel`
- `test_exec_script_encodes_bytes_with_b64_sentinel`
- `test_voice_plugin_extracts_audio_from_base64_string`
- `test_voice_plugin_responder_derives_content_type_from_audio_format`

Net: 967 → 973 core tests. API + bench unchanged.

**Verified end-to-end** (zero additional build spend): replay of
voice_dual_7's OpenAI harness through the fixed pipeline produced
5 audio files (1 merged + 2 caller + 2 agent) all at matching
extension, merged conversation plays correctly with both voices in
dialogue order, score 1.0 (both turns matched expected substrings
once audio actually reached the scorer). The fix chain is:
`harness bytes` → `_bytes_safe` (base64 sentinel) → JSON → Agent 5
`_inflate_b64_sentinels` → bytes → `_extract_agent_text_and_path`
→ `_save_audio_blob` with derived `audio/mpeg` ctype → `.mp3` file →
merger strips ID3 tags → concat → playable "Full call" clip.

### NEW-AG — Voice end-to-end + dual-provider eval + UI/audio merge

Three connected capability fixes after the second voice real-run pass
(traces voice_debug_6 + voice_dual_3). All five upgrades are
general-purpose surfaces, not scenario-specific bandaids:

**Capability fix 1 — tool_runner promotes plugin verdicts.**
`plugin_tool_runner.py::evaluate_with_tool_runner` used to require
Claude to emit a structured `ScoreVerdict` AFTER calling a plugin
tool. When Claude skipped that final structured emission (which
happens regularly with adaptive thinking and a single-tool decision),
the verdict path collapsed to LLM-judge fallback — silently throwing
away the plugin's actual scoring (e.g., voice_realtime's
`drive_conversation` had run all 5 turns, computed per-turn
substring matches, and produced an `overall_score` + per-turn
`reasoning`). Now: every plugin verdict invocation is captured into
`EvalContext.plugin_verdicts` (new dataclass `_CapturedPluginVerdict`).
When `final_parsed is None`, the LAST conclusive plugin verdict (no
fallback_reason, non-empty reasoning) is promoted directly into the
`ToolRunnerVerdict` instead of falling through to the LLM judge.
Plugin scoring IS authoritative for modality-matched evaluations;
the round-trip via Claude's structured output is a rubber stamp the
system shouldn't depend on. Logged via
`operation: tool_runner_promote_plugin_verdict` so promotions are
observable.

**Capability fix 2 — full-conversation audio merge.**
`voice_realtime.drive_conversation` now stitches every per-turn
caller + agent audio file (in dialogue order) into a single
`conversation_<token>.<ext>` clip alongside the per-turn files.
Surfaces as the FIRST artifact with `role="conversation"` so any UI
defaults to playing the whole call. Pure-Python byte-concat (no
ffmpeg/pydub dependency); refuses mixed extensions to avoid
producing corrupt files. Returned in
`drive_conversation()['merged_audio_path']` AND in `audio_paths`.
General across `shape={twilio,vonage,generic}` because it operates
on the final per-turn file list, not the protocol envelope.

**Capability fix 3 — backend audio route serves CLI-driven runs.**
`puzzleeval-api/routes/runs.py::serve_run_audio` containment check
used to allow only `puzzleeval-api/runs/`. Files written by CLI runs
(under `PuzzleEval-local/runs/`) silently 403'd in the browser —
including every voice scenario you'd test from the CLI before
shipping. Now the allowlist auto-includes the sibling
`PuzzleEval-local/runs/` dev root AND any
`PUZZLEEVAL_EXTRA_RUNS_ROOTS` (comma-separated) from env, with the
same containment guarantee applied to each root individually.

**Capability fix 4 — frontend "Full call" badge for merged audio.**
`EvaluationReportCard.tsx::AudioPathsBlock` splits artifacts by role:
`role="conversation"` clips render at the TOP, full-width, with a
violet "Full call" badge; per-turn caller (blue) and agent (emerald)
clips render below at compact size. Adding a future role is a
one-line entry in the `ROLE_STYLE` map — no rerender logic to
touch.

**Capability fix 5 — credential resolver unions cross-provider.**
`_resolve_candidate_credentials` (Agent 5) used to short-circuit on
the first registry-key match and return one provider's env vars.
Cross-provider candidates (e.g., "ElevenLabs Voice Stack" =
ElevenLabs TTS + OpenAI Whisper + Anthropic Claude reasoning) ended
up missing two of the three keys and crashed on the missing-env-var
path. Fix: union EVERY registered provider whose normalized key
appears anywhere in the candidate's searchable surface (name +
provider + validation_notes). The harness gets every key it needs
without requiring schema changes or a per-candidate
`upstream_providers` list. Agent 5's audio-artifact extraction also
prefers `verdict.detail.audio_paths` first (drive_conversation
populates it directly) and falls back to
`artifacts_for_token_prefix` for multi-turn sessions before the
legacy `artifacts_for_token`.

**Capability fix 7 — direct-invoke owner plugin for multi-call modalities.**
voice_dual_4 surfaced non-determinism in tool_runner: same input,
same eligible plugins, but Claude picked voice_realtime for the
ElevenLabs candidate (5 turns driven, 6 audio artifacts including
merged conversation) and skipped it for the OpenAI candidate
(collapsed to llm_judge fallback, single-turn snapshot, 0 audio
artifacts). When a plugin OWNS the modality (modality enum match
+ `requires_harness_runner=True`) AND a harness_runner is supplied,
`evaluate_with_tool_runner` now invokes that plugin DIRECTLY before
spinning up Claude's tool-picking loop. Owner picking is priority-
sorted: most-specific output_type match wins (so voice_realtime
beats conversation_simulator for `output_type=voice_conversation`),
then input_type match, then alphabetical. Falls back to standard
tool_runner only when no "obvious owner" plugin exists. Logged via
`operation: tool_runner_direct_invoke_owner`. Test:
`test_tool_runner_direct_invokes_owner_plugin_for_multi_call_modality`.

**Capability fix 10 — bytes-safe round-trip for harness output.**
voice_dual_7 produced 5 caller MP3s + a "merged conversation" file
that was caller-only — the agent's TTS audio silently vanished.
Root cause chain: harness returned raw `bytes` in
``raw_response.audio_bytes``; subprocess driver used
``json.dump(..., default=str)`` which stringified bytes as
``"b'\\xff\\xfb...'"`` (Python repr); plugin's
``isinstance(audio_bytes, bytes)`` check then failed → no agent
audio saved. General fix: bytes-safe round-trip — exec_script's
new ``_bytes_safe`` helper encodes every bytes value as
``{"_b64": "<base64>"}`` sentinel before json.dump; Agent 5's new
``_inflate_b64_sentinels`` walks the loaded JSON and re-inflates
sentinels to real bytes. Harnesses keep returning raw bytes; the
plugin keeps reading raw bytes; the JSON border is transparent.
Belt-and-braces: voice_realtime's ``_extract_agent_text_and_path``
also accepts a base64 string when the harness happens to pre-encode
(older harness convention). Tests:
- `test_execute_single_test_round_trips_bytes_via_b64_sentinel`
- `test_exec_script_encodes_bytes_with_b64_sentinel`
- `test_voice_plugin_extracts_audio_from_base64_string`

**Capability fix 11 — audio_format → audio_content_type derivation.**
Voice harnesses commonly return ``raw_response = {"audio_bytes":
<bytes>, "audio_format": "mp3"}`` WITHOUT an explicit
``audio_content_type``. The plugin's ``_responder`` defaulted to
``"audio/wav"`` which made ``_save_audio_blob`` write `.wav` files
containing MP3 bytes (corrupted playback) AND the merger refused
to concat the mixed-extension caller(.mp3)+agent(.wav) sets,
producing a caller-only "merged conversation". Fix: derive
``audio_content_type`` from ``audio_format`` (mp3 → audio/mpeg,
ogg → audio/ogg, etc.) when not explicitly provided. Test:
`test_voice_plugin_responder_derives_content_type_from_audio_format`.

End-to-end re-verified by replaying the existing voice_dual_7
OpenAI harness through the fixed pipeline (saved to ``voice_v3/``):
5 audio paths captured (1 merged + 2 caller + 2 agent, all .mp3),
score 1.0 (both turns matched expected substrings — the agent's
real audio reaching the scorer was the missing piece all along).
Merged conversation file is 290 KB ≈ exact byte-sum of the four
per-turn files; plays caller→agent→caller→agent in dialogue
order.

**Capability fix 9 — skip pre-call for multi-call modalities.**
voice_dual_6 caught the residual gap: Agent 5 was pre-calling
`harness.run(adapted_input)` once before evaluator dispatch to
populate the evaluator's `response` argument. For multi-call
modalities (voice_conversation / voice_turn / conversation), the
adapted input has no `audio_url` / `turn_index` / `session_state` —
those are built per turn by the plugin's drive-loop. Strict
harnesses (ElevenLabs Voice Stack) correctly returned
`success=False, error="missing audio_url"` on this synthetic
pre-call, which got recorded as a real test error, skipping the
entire plugin path. Lenient harnesses (OpenAI) tolerated it and
got plugin eval. Fix: detect multi-call modality and SKIP the
pre-call entirely; synthesize a placeholder `success=True` result
so the evaluator dispatcher hands the case to the plugin without
poisoning it. Plugin's drive_conversation then owns every real
harness invocation. Logged via
`operation: multi_call_pre_call_skipped`. Test:
`test_agent5_skips_pre_call_for_multi_call_modalities`.

**Capability fix 8 — voice session_dir set per candidate before eval.**
voice_dual_4's audio artifacts landed in `%TEMP%/puzzleeval_voice/`
because the prior wiring only set `set_session_dir` during INPUT
synthesis (`_synthesize_test_input_via_plugin`) — voice_conversation
tests skip that path and run drive_conversation INSIDE
evaluate_output, with the plugin's default `%TEMP%` dir still in
effect. The backend's audio-streaming endpoint refuses paths outside
its allowed runs roots → frontend silently couldn't play those
clips. Fix: Agent 5 now iterates every plugin with
`set_session_dir` and points it at
`<sandbox>/voice/` BEFORE every candidate's test-execution loop, so
synthesis + multi-turn drive both write into the run directory and
reach the UI through `/pzapi/runs/audio?path=...`. Test:
`test_agent5_sets_voice_session_dir_before_evaluation`.

**Bonus capability fix 6 — orphan-server-tool scrubber matches by suffix.**
The earlier (`NEW-AF`) orphan-stripping scrubber used an explicit
allowlist (`web_search_tool_result`, `web_fetch_tool_result`,
`server_tool_result`) — missing `advisor_tool_result`. When the
builder invoked the advisor, the scrubber saw its
`server_tool_use(name="advisor")` unpaired in its view and wrongly
stripped it, leaving `advisor_tool_result` orphaned on the next
turn → 400. Fix: match any block type ending in `_tool_result`
(excluding the bare local `tool_result` shape). Every future
server-tool family lands automatically without touching this code.

**Tests (`tests/test_prod_audit_fixes.py`, +6):**
- `test_tool_runner_promotes_plugin_verdict_when_claude_skips_score_verdict`
- `test_voice_plugin_emits_merged_conversation_audio_path`
- `test_voice_plugin_merge_returns_none_on_mixed_extensions`
- `test_audio_route_allowlist_includes_cli_dev_root`
- `test_credential_resolver_unions_cross_provider_registry_entries`
- `test_orphan_scrubber_matches_advisor_tool_result_by_suffix`
- `test_tool_runner_direct_invokes_owner_plugin_for_multi_call_modality`
- `test_agent5_sets_voice_session_dir_before_evaluation`
- `test_agent5_skips_pre_call_for_multi_call_modalities`
- `test_execute_single_test_round_trips_bytes_via_b64_sentinel`
- `test_exec_script_encodes_bytes_with_b64_sentinel`
- `test_voice_plugin_extracts_audio_from_base64_string`
- `test_voice_plugin_responder_derives_content_type_from_audio_format`

Net: 958 → 971 core tests. API + bench unchanged.

**Verified end-to-end (real-run trace voice_dual_3):** OpenAI Voice
Stack and ElevenLabs Voice Stack built in parallel by Agent 5,
each driven through the same 5-turn Acme Plumbing script, each
producing per-turn caller+agent MP3s + a merged conversation MP3
ready to play in the browser through `/pzapi/runs/audio?path=...`.

### NEW-AE — Voice-scenario readiness: registry propagation + TTS failover

Before the first real voice-evaluation run, two capability gaps surfaced.
Both exposed the SAME root pattern: credentials/providers were
single-source (one .env, one TTS provider) with no graceful degradation.
Fixed both as general capability improvements, not scenario-specific
bandaids.

**Capability fix 1 — registry as first-class credential source:**

The registry (`provider_registry.json`) was historically CANDIDATE-SCOPED
— only Agent 5 read it to inject keys into harness subprocesses.
System-level plugins (tts, transcription, voice_realtime,
webhook_receiver, outbound_delivery) read `os.environ` directly and were
blind to the registry. Users had to duplicate keys across `.env` AND the
registry — two sources of truth, inevitable drift.

- `ProviderRegistry.iter_env_vars()` — flat union of every provider's
  env_vars + OAuth env vars. Empty values filtered.
- `sync_to_environ(registry, *, override=False)` — propagates registry
  env_vars into `os.environ`. Semantics mirror `load_dotenv(override=
  False)`: explicit shell exports WIN; empty-string placeholders get
  evicted so the registry's real value reaches plugins. Override=True
  is reserved for test scenarios that need to swap creds mid-run.
- `puzzleeval/__init__.py::_autoload_provider_registry()` — runs after
  `_autoload_dotenv()` so .env still has precedence. One line of
  package-level initialization makes the registry auto-active everywhere
  PuzzleEval imports (CLI, FastAPI, pytest, notebooks, preflight scripts).

Now users put credentials in the registry ONCE. Plugins see them,
Agent 5 injects them, .env is optional. Single source of truth.

Committed OpenAI + ElevenLabs entries:
```json
"openai": {
  "env_vars": {"OPENAI_API_KEY": "sk-..."},
  "tier": "paid",
  "notes": "Shared system key: tts (openai_tts), transcription
            (openai_whisper), AND candidate credential for OpenAI
            Realtime / Chat / DALL-E via substring match."
},
"elevenlabs": {
  "env_vars": {"ELEVENLABS_API_KEY": "sk_...",
               "PUZZLEEVAL_ELEVENLABS_VOICE_ID": "..."},
  "tier": "free", "monthly_limit": 10000,
  "notes": "Shared system key: tts (elevenlabs), ConvAI candidate."
}
```

**Capability fix 2 — TTS provider failover chain:**

The real smoke test surfaced: the committed ElevenLabs key was expired
(401 Invalid API key from their /v1/user endpoint). Old TTS plugin
behavior: when the explicit provider failed, it collapsed to a text
placeholder — every downstream voice test scored 0 regardless of
OpenAI TTS being credentialed and working.

- `_iter_tts_providers()` replaces `_select_tts_provider()` as the
  primary selector. Returns ALL credentialed providers in priority
  order: explicit preference first (when `PUZZLEEVAL_TTS_PROVIDER` is
  set), then the rest.
- `synthesize_input` iterates the chain — 401 / 429 / 5xx from one
  provider logs a warning and tries the next. Only returns failure
  after EVERY credentialed provider has errored.
- Result metadata includes `providers_tried` so the caller can see
  which provider actually succeeded + which failed over.
- `_select_tts_provider()` kept as a back-compat shim for tests that
  need a single-provider view.

**Tests (`tests/test_prod_audit_fixes.py`, +5 cases):**
- `test_provider_registry_has_openai_and_elevenlabs` — registry +
  substring match resolves voice-candidate names.
- `test_provider_registry_sync_to_environ_fills_missing_keys` — empty
  slots filled, explicit values preserved.
- `test_provider_registry_sync_to_environ_override_mode` — override=True
  for test-scenario credential swaps.
- `test_package_autoload_propagates_registry_to_environ` — package
  import activates registry propagation.
- `test_tts_plugin_has_failover_chain` — primary provider fails, plugin
  falls over to next credentialed provider and succeeds.

Net: 932 → 937 core tests. API suite + generalizability bench unchanged.

**New developer scripts (both committed; safe to rerun):**

- `scripts/real_api_smoke.py` — 8 real-API smoke tests, total spend
  < $0.01: Anthropic Haiku, OpenAI Chat, provider_registry lookups,
  plugin readiness, OpenAI TTS, ElevenLabs TTS (exercises failover),
  OpenAI Whisper roundtrip, voice_realtime one-turn loopback. Run before
  every real pipeline run.

- `scripts/preflight_check.py` — 89 static/wiring checks (imports,
  config snapshot, plugin readiness matrix, SSE event coverage, schema
  round-trip, ports, backend boot, tool_runner invariants). No API spend
  with `--no-api`; $0.0001 with.

- `scripts/mock_pipeline_direct.py` — full mock pipeline via
  `asyncio.run(run_pipeline(state))`. Bypasses TestClient's known
  background-task limitation. Verifies end-to-end flow reaches
  status=completed with a persisted evaluation_report.json.



### NEW-AD — Agent 4 → Agent 5 atlas handoff (the big redundancy)

The user caught a real architectural gap: Agent 5's Phase 1 prompt
treated the builder as if starting from zero — "research the API from
scratch, then write api_spec.txt" — even though Agent 4 had already
deep-verified every candidate and produced a rich atlas JSON at
`candidate.api_spec_path`.

**The conflict:** the builder saw TWO contradictory signals:
1. The initial message's "Pre-extracted spec available" section
   pointed to the atlas file (good — Agent 4's output IS being passed).
2. Phase 1's prompt header said "Understand the API before writing any
   code. Do NOT skip this phase." with a full research workflow that
   redid everything Agent 4 had already done.

Result: the builder often re-fetched docs Agent 4 already processed,
burning 3-5 turns rediscovering known endpoints. At ~$0.10-0.30 per
turn × 4 candidates = ~$2-4 wasted per run.

**Fixed this pass:**

- Rewrote Phase 1 header in `implement_test_env.py::BUILDER_SYSTEM_PROMPT`
  from "PHASE 1: RESEARCH — Understand the API before writing any code"
  to "PHASE 1: ATLAS INGEST + GAP-FILL — most of the research is
  already done." New 3-step workflow: (1) read_file(atlas), (2)
  gap-fill only fields marked empty or low-confidence, (3) write
  api_spec.txt by copying atlas fields plus any gap research.
- Raised the atlas-visibility section at the TOP of the initial
  message from a passive "Pre-extracted spec available" note to an
  assertive "⚠️ PRE-EXTRACTED ATLAS — YOUR STARTING POINT, NOT A
  REFERENCE" section that explicitly lists what the atlas contains
  (endpoints, auth, interaction_model, pricing, sandbox, doc_page_map,
  completeness/confidence) and instructs the builder to copy-not-
  rediscover.
- Kept the gap-fill escape hatch: when the atlas explicitly marks a
  field empty or its `atlas_completeness.confidence=low` AND the test
  cases need it, targeted web_fetch / web_search is still correct.

**Expected impact:**
- 3-5 fewer turns per candidate on well-verified providers (Mindee,
  Stripe, ElevenLabs HTTP) where the atlas is rich.
- ~$1-3 saved per run.
- Lower false-positive rate on misalignment (builder was occasionally
  picking a different endpoint than the atlas's ROUTING_TABLE
  suggested, because it "re-researched" and landed on a quickstart
  endpoint instead of the scope-appropriate one).

### Python SDK audit — findings

User asked about Python SDK features + Message Batches.

**Useful but deferred (explicit reasoning):**
- `Message Batches API` (`client.messages.batches.*`) — 50% cost
  savings, but up to 24-hour async completion. Not usable for the
  real-time user-facing pipeline. **Candidate for the generalizability
  bench** — running 39 domain configs overnight at half-price would be
  valuable. Deferred until bench scale demands it.
- `count_tokens` (`client.messages.count_tokens`) — preemptive context
  management pattern. Our context_management.edits (server-side) does
  this reactively; client-side preemption adds complexity without clear
  gain. Skipped.
- `aiohttp` backend (`DefaultAioHttpClient`) — only helps if we move to
  a fully async client model. Our ThreadPoolExecutor + sync client
  pattern works well. Skipped.
- `with_streaming_response` — different from `stream=True`; used for
  large response-body streaming. Not our use case.
- Streaming (`stream=True`) — would enable live agent_thinking SSE UI.
  Already tracked as cloud-deferred UX work.

**Already using:**
- `messages.create` + `messages.parse` for structured outputs
- `beta.messages.tool_runner` (NEW-AA)
- Retries (default 2) via `max_retries` through `anthropic_client.py`
- Timeouts (120s sync, 240s Agent 5)
- Auto-pagination (no current consumer)
- Pydantic type-safe request/response

**Tests (`tests/test_prod_audit_fixes.py`, +1 case):**
- `test_agent5_phase1_is_atlas_first_not_research_from_scratch` locks
  the new atlas-first Phase 1 wording.

Plus `test_bandaid_removal.py::test_spec_path_emitted_when_present`
updated to assert the new stronger atlas-first messaging.

Net: 931 → 932 core tests.



### NEW-AC — Performance + Claude Code parity optimization pass

After the NEW-AB production audit, a focused perf/parity audit surfaced
7 high-ROI optimizations (ranked by impact/effort). All shipped this pass.

**Shipped:**

1. **D1 — Agent 5 system-prompt cache_control.** The 10.7K-token builder
   system prompt was rebuilt every turn. Added `"cache_control":
   {"type":"ephemeral"}` to the system block directly (previously only
   request-level, which is unofficial). Block-level is the documented
   reliable path. **~40% input-cost reduction per candidate build.**

2. **D2/A1 — `clear_thinking_20251015` context-management edit.** Added
   as the FIRST edit in Agent 5's context_management.edits (fires at
   40K tokens before clear_tool_uses at 80K). Prunes accumulated
   extended-thinking blocks without invalidating tool-use cache — free
   bytes back that adaptive thinking on every turn would otherwise
   accumulate. Small but strict additive.

3. **D3 — deduplicated Agent 2 STRUCTURE prompt.** The candidate-class
   separation principle (developer primitive vs packaged product) was
   word-for-word duplicated across RESEARCH + STRUCTURE prompts — ~1800
   redundant tokens per run. Replaced the STRUCTURE copy with a
   one-liner preservation rule. RESEARCH retains the full teaching.

4. **D5/C3 — deep-verify `web_fetch` budget 6 → 8.** On providers with
   fragmented docs (OpenAI Realtime + Chat Completions + Audio — split
   across 4+ pages; Stripe, AWS similar), 6 fetches left zero headroom
   and caused false-positive `docs_unreachable` rejections. +2 fetches
   ~= +$0.04 per candidate worst case, far cheaper than the wrong-
   reject downstream cost (rebuild attempt + user confusion).

5. **A11 — turn-budget nudge for Agent 5 builder.** When builder has
   ≤3 turns remaining AND smoke hasn't passed AND there's conversation
   history, inject a single wrap-up message telling Claude to commit:
   either HARNESS_COMPLETE or HARNESS_FAILED, no third refactor. Fired
   once per candidate. Pattern stolen from Claude Code's
   `getBudgetContinuationMessage` in `query/tokenBudget.ts`. Prevents
   "stuck polishing on turn 23" runs that burn the budget without
   producing a verdict.

6. **C4 — `api_interaction_pattern_hint` enum: +`websocket` +
   `sse_streaming`.** Previously only `sync`/`async_polling`/`other`/
   `unknown`. WebSocket-primary APIs (OpenAI Realtime, ElevenLabs
   Conversational AI, phone/voice realtime) had no hint to thread
   through to Agent 4's deep-verify. Agent 5's wss advisory already
   acts on the downstream `event_subscription` atlas flag; adding the
   upstream hint gives Agent 4 a pre-signal to invest deeper in
   WebSocket discovery during 4A.

7. **Agent 5 prompt — verify_against_docs contradiction resolved.**
   Phase 1 rule "DO NOT write verification scripts" and Phase 2 tag
   `<verify_against_docs>` (read_file comparison) could pattern-match
   inconsistently. Tightened: "DO NOT write ad-hoc verification
   **scripts**" clarified as Phase 1-only; the Phase 2 read_file
   eyeball check explicitly preserved. Eliminates a ~10% false
   hesitation rate on spec-comparison.

**Considered, deliberately skipped:**

- **D9 (atlas endpoint filtering)** — already implemented via
  `scope_hints & covered_scopes` filter with `[:12]` cap at
  `implement_test_env.py:322-327`. Not a real gap.

- **A4 (rationalization countermeasures in adversarial_verifier)** —
  `adversarial_verifier.py` is a deterministic probe runner with no
  Claude-driven prompt. Pattern shape doesn't match. Skipped cleanly.

- **A2 (diminishing-returns turn-budget stop)** — good idea but
  overlaps substantially with A11 (turn-budget nudge) and the
  existing wall-clock + dead-end-detection + smoke-pass-N-turn
  force-accept guards. Revisit after live run reveals whether A11
  alone is sufficient.

- **A7 (cache-safe pinning `pinCacheEdits`)** — requires Anthropic
  beta support (`CACHED_MICROCOMPACT`) that isn't GA. Defer until
  the API ships.

**Deferred — cloud-scale architectural pass:**

- **WebSocket harness template** (the ONE remaining gap for full
  ElevenLabs Realtime + OpenAI Realtime comparison). Needs: new
  `api_patterns.py` entry, async harness_runner bridge, atlas schema
  `websocket_primary` field. Scoped for a dedicated session; the
  user explicitly agreed to defer this pass.

**Tests (`tests/test_prod_audit_fixes.py`, +7 cases):** every
optimization gets a source-grep or import-level regression guard.
924 → 931 core tests.

**Cumulative session state:**
- Core: 931 tests
- API: 30 tests
- Generalizability bench: 39 tests
- **Total: 1000 tests passing. Zero regressions since NEW-AA.**



### NEW-AB — Pre-production audit sweep (4 parallel subagent audits + fixes)

Before the first real live-run, four subagents deep-audited the codebase
in parallel (backend wiring, prompts + schemas, frontend rendering, E2E
trace integrity). Consolidated findings + shipped every real fix:

**CRITICAL production-blockers (would have killed the voice run):**

1. **Wrong beta header** — `plugin_tool_runner.py` sent
   `betas=["code-execution-2026-01-20"]` when the correct value per
   Anthropic docs is `code-execution-2025-08-25`. Every tool_runner
   invocation would have 400'd, silently falling through to LLM judge
   for every test. Fixed + test locks the literal.

2. **Tool_runner cost vanished from reports** — `ToolRunnerVerdict.cost_usd`
   was produced but never accumulated. Agent 5's `eval_cost` clobbered
   tool_runner's contribution when the LLM-judge branch ran (`=` vs
   `+=`). Fixed: nonlocal-accumulate in `_run_tool_runner`, `+=` in
   LLM-judge path.

3. **Multi-turn voice harness contract undocumented** — builder prompt
   described only `{text, input_type, input_context, test_file_path}`;
   voice_realtime's multi-turn driver passes
   `{audio_url, turn_index, session_state}`. Every voice test would
   have KeyError'd. Added a "MULTI-CALL HARNESS CONTRACT" section to
   the builder prompt explaining the per-turn payload shape +
   session_state threading requirement.

4. **WebSocket/Realtime endpoints silently tested as REST facsimiles** —
   no wss:// detection anywhere. Would have shown false-positive pass
   rates for OpenAI Realtime. Added a builder advisory that triggers
   when atlas or docs_url indicates WebSocket: either build against a
   REST fallback with explicit NOTES advisory OR signal
   `HARNESS_FAILED` with `websocket_not_supported`. Never silently.

5. **`asyncio.gather` without `return_exceptions=True`** in
   `pipeline_runner.py` — Agent 3 failure would tear down a running
   Agent 4 branch. Fixed + re-raise so pipeline_failed surfaces the
   real exception, not an asyncio wrapper.

6. **Selection-phase cancel emitted wrong status** — user cancel during
   Phase 6 pause labeled as `"after_screening"`. Fixed: detects
   `state.status == "awaiting_candidate_selection"` and emits
   `cancelled_at="during_selection"`.

7. **Cost double-count in final report** — report total was
   `state.total_cost_usd + total_cost` where both already included every
   agent's cost via `_record_agent_cost_and_emit`. Every run's final
   report showed 2× the actual charge. Fixed: pass `state.total_cost_usd`
   directly.

8. **Path traversal in `POST /files`** — no filename sanitization.
   `../../../etc/passwd` got joined into run_upload_dir. Fixed: strip
   directory components via `PurePosixPath/PureWindowsPath.name`, scrub
   shell-metachars, and verify `dest.resolve()` stays inside
   `run_upload_dir.resolve()` before writing.

**HIGH-priority cleanups (silently-broken features):**

- **Plugin eligibility semantics** (`plugin_tool_runner.py`) —
  contradictory AND-then-OR override replaced with clean either-side-
  matches logic. Runner-driven plugin widening now narrowed to
  multi-call modalities (`conversation`, `voice_conversation`, `voice_turn`)
  so Claude isn't invited to mis-pick conversation_simulator for plain
  text tests.

- **Harness runner conditional injection** (`implement_test_env.py`
  `_run_tool_runner`) — `needs_runner` gate based on input/output
  modality. Plus hoisted the closure out of the per-test loop (was
  rebuilding once per test; now once per candidate).

- **Stale Agent 3 / Agent 3F enum lists** — top-of-prompt input_type
  / output_type sections listed only 4-5 of 11-12 valid values.
  Claude reads prompt top-down; first lists it internalized were
  restrictive. Replaced with complete authoritative enum lists, each
  value tagged with its plugin destination.

- **`WorkflowStep.output_format` field description** in `schemas.py`
  listed 8 of 12 enum values. Now complete.

**Frontend wiring gaps (shipped backend features had no UI):**

- **`<audio>` nowhere in src/** — voice_realtime artifacts were saved
  to `runs/<trace>/harnesses/<slug>/voice/` but invisible in the UI.
  Added:
  - `AudioArtifact` type + optional `audio_paths` on `TestResult`,
    `CriterionScore`.
  - `AudioClip` / `AudioPathsBlock` components in
    `EvaluationReportCard.tsx` — HTML5 `<audio controls>` per file,
    color-coded by role (caller vs agent).
  - New backend route `GET /runs/audio?path=...` in `routes/runs.py`
    that streams audio with containment check (rejects paths outside
    the runs root).
  - Rendered in both `failure_evidence` AND `success_evidence` rows.

- **`ScopeTestRun` declared but never consumed** — `<ResultsComparison>`
  was always called without `scopeRuns`, so Phase 9's per-scope table
  view never rendered. Fixed end-to-end:
  - `EvaluationReport.scope_runs: list[dict]` field added to
    `report.py`.
  - `_extract_scope_runs()` helper pulls from Agent 5's output
    (Pydantic or dict shape).
  - `Playground.tsx` extracts `scope_runs` from the evaluation_report
    payload and passes to `ResultsComparison`.

- **Results pane blanked on all-rejected runs** — `stage === "results"
  && candidates.length > 0` hid everything including advisories. Now
  renders unconditionally at `stage === "results"` with an explicit
  empty-state banner for the zero-candidate case. Rejections still
  visible; evaluation report advisories still shown.

- **3 missing SSE handlers** (`usePipelineRun.ts`):
  - `agent_blocked` (billing gate denial surfaces reason + plan)
  - `test_cases_ready` (Agent 3 count progress)
  - `scope_verified_complete` (per-scope verified/rejected aggregate)

- **`default_picks` ignored by SelectionPanel** — Phase 7's smart-
  default top-K was sent in `selection_required` payload but panel
  pre-checked all candidates instead. Now panel accepts `defaultPicks`
  prop, pre-selects the intersection with available candidates, falls
  back to "all checked" when Phase 7 didn't emit picks (legacy
  pipelines).

**Tests** (`tests/test_prod_audit_fixes.py`, 18 new cases): covers all
eight critical bugs + enum drift + frontend audit signals. Plus the
existing tool_runner test's beta-header assertion is now exact-literal,
not substring. 906 → 924 core tests. API suite + generalizability bench
unchanged.

**The one documented limitation from the audit — not fixed in this pass,
cloud-deferred work:**

- WebSocket harness template (OpenAI Realtime / ElevenLabs streams at
  full fidelity) — the advisory above is the bridge. When the user hits
  this in a real run, they see which providers were tested at full
  fidelity vs REST facsimile. The full fix is a new `api_patterns.py`
  entry for "outbound WebSocket" plus an async harness_runner bridge —
  scoped for a dedicated pass (pattern-catalog refactor to tool-search
  model is the prereq).



### NEW-AA — Claude-driven plugin dispatch via `tool_runner`

**The architectural change the prior deterministic-vs-Claude-driven
debate was pointing at.** Fully migrated Agent 5's per-test scoring
from enum-based deterministic dispatch to Anthropic's documented
tool-use pattern. Closes all six coverage gaps of the old path.

**New module: `puzzleeval/plugin_tool_runner.py`**
- `ScoreVerdict` Pydantic model — Claude's final verdict is
  schema-enforced via `output_format=ScoreVerdict`. No regex-parsing.
- `EvalContext` — per-test context closure-captured by every plugin
  tool so Claude doesn't have to re-state test details each call.
- `eligible_plugins(input_type, output_type)` — returns ALL matching
  plugins (not just first), filters unavailable ones. Used to assemble
  the tool list per test case.
- `_build_plugin_tool(plugin, ctx, ...)` — wraps a plugin's
  `evaluate_output` as a `@beta_tool` function. Dynamic function name
  (`score_with_<plugin>`), docstring synthesized from capabilities so
  Claude can decide when to call. Closure-captures context; Claude
  invokes with zero arguments.
- `evaluate_with_tool_runner(...)` — the entry point Agent 5 calls.
  Assembles tools, kicks off `client.beta.messages.tool_runner`,
  iterates until Claude emits a structured `ScoreVerdict`, folds the
  result + telemetry + artifacts into a `ToolRunnerVerdict`.

**Coverage gaps that deterministic had, now closed:**
1. "Deterministic picked 1 plugin, but the response legitimately
   needed 3" — Claude iterates, invoking multiple tools per test.
2. "Agent 3 mis-stamped the modality enum" — Claude reads the actual
   response content, ignoring the label when it doesn't match.
3. "Two plugins both claim the same (input_type, output_type)" —
   Claude picks by description match, not first-registered wins.
4. "Multi-modal response (audio + image + code)" — Claude chains
   transcription + vision + llm reasoning in one test evaluation.
5. "Novel plugin added without modality enum update" — just register
   with a clear description; Claude finds it via tool search (or
   direct listing below the scale threshold).
6. "Scale past 30+ plugins" — `tool_search_tool_bm25_20251119` auto-
   engages at `EVAL_TOOL_SEARCH_THRESHOLD` (default 15 plugins).
   Plugin tools get `defer_loading=True`; Claude discovers them on
   demand. Prompt caching stays intact.

**Scale-ready knobs in `puzzleeval/config.py`:**
- `EVAL_STRATEGY` (`tool_runner` | `deterministic` | `hybrid`) —
  default is `tool_runner`. `deterministic` kept for emergency
  bisection. `hybrid` runs deterministic first, tool_runner fallback.
- `EVAL_TOOL_SEARCH_THRESHOLD=15` — switches to `tool_search_tool` +
  `defer_loading` when plugin count reaches this. Per Anthropic's
  published "selection accuracy degrades past 30-50 tools" guidance,
  we switch conservatively early.
- `EVAL_MAX_ITERATIONS=5` — cap on tool_runner iterations per test.
  Enough for 3-tool chains; raise for pathological multi-modal tests.
- `EVAL_PROGRAMMATIC_CHAINING_ENABLED=0` — when on, plugin tools get
  `allowed_callers=["direct","code_execution_20260120"]` and a
  `code_execution` server tool is added. Claude can write ONE Python
  script that chains multiple plugins in one container; intermediate
  tool results don't enter the model's context. Off by default until
  soaked on real multi-tool runs.

**Agent 5 dispatch rewrite** (`agents/implement_test_env.py`):
- Old: hardcoded `from puzzleeval.modality import detect_for_test_case`
  + "first matching plugin wins" + LLM judge fallback.
- New: strategy-driven dispatch via `EVAL_STRATEGY`. Shared
  `_promote_verdict_to_tcr` helper ensures deterministic and
  tool_runner paths produce identical `TestCaseResult` shapes.
- The prior hardcoded `if evaluator.name == "conversation_simulator"`
  bandaid stays deleted; `capabilities.requires_harness_runner` drives
  runner injection in BOTH paths.

**What stays the same:**
- `modality.py` is still there — `EVAL_STRATEGY=deterministic` uses
  it. Kept as an escape hatch; delete only after tool_runner soaks
  through a live run cycle.
- `hybrid_evaluator.py` is still there — older opt-in path from NEW-X.
  Superseded by `plugin_tool_runner.py` but preserved for backward
  compat with any caller that imported it directly.
- Every plugin's contract is unchanged. `evaluate_output(response,
  expected, criteria, harness_runner=None)` still works the same way.
  Plugins are exposed to Claude via auto-generated `@beta_tool`
  wrappers — no plugin author has to learn the SDK's tool machinery.

**Tests (`tests/test_plugin_tool_runner.py`, 22 new):**
- ScoreVerdict schema validation (2)
- Eligibility filtering (availability, modality match, multi-modal) (3)
- Tool list assembly (plain, tool_search, code_execution, combined) (4)
- @beta_tool closure wiring: happy path, plugin crash, artifact capture (3)
- `evaluate_with_tool_runner`: verdict extraction, no-verdict fallback,
  API error handling, beta header for code_execution, tool_search
  activation under threshold (5)
- Agent 5 source-grep regressions: strategy dispatch, default path (3)
- System prompt content + tool description contract (2)

852 → 906 core tests. +22 from this pass, +32 from prior NEW-Z audio
+ multi-turn pass. API suite and generalizability bench unchanged.



### NEW-Z — Voice testing: runs-dir audio + multi-turn via capability dispatch

Two shipped items plus a bandaid removal forced by an architecture review:

1. **(a) Voice audio persists under runs dir, surfaces in EvaluationReport.**
   - `voice_realtime.set_session_dir()` — Agent 5 redirects audio persistence
     from `%TEMP%` to `runs/<trace_id>/harnesses/<slug>/voice/` via the
     synthesizer-path seam in `_synthesize_test_input_via_plugin`. Every
     caller + agent WAV now lives alongside harness.py / conversation_log.json
     for the run.
   - `voice_realtime.artifacts_for_token()` — returns ordered `{role, path}`
     list. Agent 5 pulls this after evaluator runs and attaches to
     `TestCaseResult.audio_paths` (new field). `TestEvidence.audio_paths` is
     the report-side field; `report.py::_extract_test_evidence` threads it.
   - **Cloud-scale seam:** `set_session_dir` accepts any Path-compatible
     object. Swap in an S3/GCS Path-shim for cloud.
   - This is a pure outlier-removal: `voice_realtime` was the ONLY plugin
     writing to `%TEMP%` — every other plugin already uses the run dir.

2. **(b) Multi-turn voice — via the EXISTING `requires_harness_runner`
   pattern, not a new "modality" agents must learn.**

   The FIRST version of (b) shipped a new `voice_conversation` input_type
   that would have required Agent 3 prompt gymnastics, an Agent 4 atlas
   field (`multi_turn_state_method`), AND an Agent 5 harness template
   branch. That was case-specific coupling dressed up as architecture.

   The user pushed back: "is this a plugin extension or a new modality?"
   The right answer is the former. The codebase already had the correct
   pattern for text chat (`conversation_simulator` sets a flag, Agent 5
   injects a `harness_runner`, the plugin owns the multi-turn loop
   internally). Voice follows the same pattern now:

   - New `PluginCapabilities.requires_harness_runner: bool` — the general
     seam. Any plugin that DRIVES the harness (conversation_simulator,
     voice_realtime, any future multi-call evaluator) sets it true.
   - `voice_realtime.evaluate_output()` auto-detects a conversation script
     in the expected payload (via `_extract_conversation_script` — accepts
     6 shapes so Agent 3 has no rigid contract) and routes to the new
     `drive_conversation` driver.
   - `_evaluate_conversation()` builds a responder closure that wraps the
     Agent-5-provided `harness_runner`: per-turn payload in, harness
     response out, session_state mutated for the next turn. Plugin owns
     the N-turn state machine. The harness stays single-turn — **same
     shape Agent 5 already knows how to build for voice_turn tests**.
   - Agent 5's dispatch loop now reads `capabilities().requires_harness_runner`
     instead of the pre-existing hardcoded `if evaluator.name ==
     "conversation_simulator"` bandaid. **That bandaid is gone.**
   - Agent 3 prompt gets ONE principle-based rule: "when the scope is
     multi-turn voice, emit a `turns` script." Same shape rule as the
     existing chatbot-conversation rule. No brand-specific carveouts.

   **What agents see:** nothing new. Agent 1 still emits `voice_turn` or
   a multi-turn voice scope exactly as before. Agent 2 still discovers
   voice candidates exactly as before. Agent 4 still deep-verifies with
   the existing atlas fields (interaction_model.synchronous /
   async_polling / sse_streaming already capture the state-carrying
   patterns). Agent 5's harness template generates a single-turn harness,
   unchanged. The plugin is the extension point — agents treat it as an
   opaque black box that claims modalities via schema enums.

   **Zero case-specific branches anywhere.** Adding a future multi-call
   plugin (voice over WebSocket once that's infrastructurally possible,
   anything else that needs N harness invocations per test) takes a
   single flag flip plus the plugin's own logic. No agent prompts to
   touch.

**Tests:** +32 cases across `test_second_intelligence_pass.py` covering:
the runs-dir persistence + report surfacing, the capability-flag
dispatch, the conversation-script auto-detection across six shapes, the
runner-bridge execution path, the Agent 3 prompt rule, the Agent 5
source-grep guard that the hardcoded branch is gone. Net: 852 → 884 core
tests. API + bench unchanged.



### NEW-Y — Bandaid audit + Claude Code study + second capability pass

Two parallel audits drove this pass:
- **Bandaid audit** on the prior NEW-X fixes — clean result: all changes
  classified as GENERAL CAPABILITY BOOST or PRINCIPLE-BASED, with 3 flagged
  as ACCEPTABLE BANDAIDs (DRY tech debt, not logic bugs). **Hoisted** to
  `puzzleeval/config.py`:
  - `CANONICAL_COVERAGE_DIMENSIONS` (6-dim set used in prompt + topup + validator)
  - `SUFFICIENCY_FLOOR_RATIO` (0.7) + `SUFFICIENCY_HARD_FLOOR` (3)
  - `MONTHLY_VOLUME_BANDS` + `band_monthly_volume()` helper
  All 3 consumers now import — no more 3-way drift.
- **Claude Code source study** at `C:/Users/Deanh/OneDrive/Desktop/Claude_code/claude-code-source-code/src`. Identified 5 patterns to steal. Shipped 2 of them below; 3 deferred with explicit architectural reasoning (see "Deferred" section at the bottom of this block).

**Agent 3F — variety-aware batcher** — The old prompt hardcoded "exactly
one test per uploaded file, no more no less." Rewards tidiness over
coverage. New prompt teaches a TWO-PHASE process: (Phase 1) content
inventory per file classifying as `matches_scope` / `off_topic` /
`variety_potential=low|medium|high`, (Phase 2) variety-aware batching —
high-variety files produce 3-5 tests covering multiple dimensions, near-
duplicates collapse to one test, off-topic files get excluded with a
user-facing warning.

**Atlas schema v2 — endpoint + provider fields Agent 4 now extracts** —
The old atlas was missing critical fields that Agent 5 rediscovered every
build. Shipped schema v2 with backward-compat loaders:
- **Per-endpoint**: `idempotency_support` ("header:Idempotency-Key" /
  "none" / "auto" / "required"), `oauth_scopes`, `deprecated` (+
  `replaced_by`), `retryable_errors`, `doc_url` (direct link for
  ask_research jumps).
- **Per-provider**: `version_pin_header` (Stripe-Version / OpenAI-Beta /
  anthropic-version), `sandbox_credential_flow` (auto_on_signup /
  manual_request / contact_sales / not_applicable).

v1 atlases load unchanged — all new fields default to empty and stay that
way until the next deep-verify refresh. The extraction prompt teaches
Claude when + how to populate each field (e.g., `deprecated: true` only
when docs explicitly say so, not when the URL contains "v1").

**Atlas v2 → Agent 5 handoff — `_format_atlas_context_for_builder`
expanded** — Previously the deep-verify atlas was written to disk but
Agent 5 read only a handful of fields from `ScreenedCandidate`; atlas v2
fields never reached the builder prompt. Now the formatter loads the
atlas JSON directly and surfaces:
- `version_pin_header` as "MUST include on every request" guidance
- `sandbox_credential_flow` as "how to get sandbox credentials"
- Deprecated endpoints as "DO NOT build against these" (with `replaced_by` pointers)
- Idempotency-required endpoints as "include key on retries"
- `doc_page_map[]` as bookmarks for ask_research (pre-indexed, no re-search)
- Endpoints relevant to this candidate's `covers_step_ids` — grouped +
  captioned with scope_hints + direct doc_url

Claude Code pattern: bookmark-driven research (the bookmarks exist; now
they're USED).

**Agent 5 — ENDPOINT-FIT CHECKPOINT** — Before Phase 2 (writing code),
the builder MUST emit an `<endpoint_fit>` block justifying its endpoint
choice on 4 dimensions: `user_scope_match` (matches workflow role),
`user_volume_fit` (LOW=atomic, HIGH=batch — read from band helper),
`side_effects` (sandbox vs production), `technical_level` (SDK wrapper
vs raw REST for non-technical users). Plus `version_pin_header` and
`idempotency_plan` declared before Phase 2 starts. **This is THE fix for
the "first endpoint that works" problem** — the builder is forced to
reason about fitness BEFORE writing code, not just pick the first one
that accepts the input format.

Picking rules (PRINCIPLE-BASED, not case-specific):
- Batch vs atomic → from monthly_volume band
- Deprecated endpoints → never pick, use `replaced_by` pointer
- Multiple scope_hint matches → pick the one whose `purpose` most closely
  describes the scope's `role`
- `side_effects=creates_records` → always sandbox/DRY_RUN when available
- `technical_level=non-technical` → prefer SDK wrapper over raw REST

**Tests:** new `tests/test_second_intelligence_pass.py` (18 cases) + all
903 prior tests still passing → 921 total.

**Deferred — honest architectural gaps that would destabilize the real run:**
1. **Agent 2 per-scope parallelism** (Claude Code pattern). Refactoring the
   single `messages.create` into N parallel calls + merge pass is a big
   semantic change (dedup-as-you-go vs post-hoc merge). The atlas cache
   preload shipped in NEW-X already handles the dominant redundancy case.
2. **Microcompact with compactable-tools allowlist** (Claude Code pattern).
   Would replace stale tool outputs with sentinels. Existing
   `_persist_large_output` is working well; more aggressive compaction
   risks context-window regression.
3. **LLM-driven memory selector** (Claude Code pattern). Would change which
   memdir entries surface at recall time. Regresses every test that checks
   specific recall paths.

None are wiring — all 3 are architectural and need their own dedicated
pass with live A/B. Documented in `POST_ROADMAP_ENHANCEMENTS.md` §22.



### NEW-X — Per-agent intelligence pass (current session)

Five parallel deep-audit agents (one per pipeline agent) surfaced real
intelligence gaps, not cosmetic issues. Each finding got a concrete fix:

**Agent 1 — modality literacy.** The one-paragraph enum-list for
`output_format` taught only 6 of 11 values with examples; every worked
blueprint was OCR/document. Replaced with a per-enum modality TABLE
(`free_text` / `structured_json` / `classification` / `extraction` /
`action` / `media_url` / `code` / `audio_content` / `webhook_callback` /
`outbound_message` / `voice_turn` — 11 rows × what-it-produces × scoring
plugin) plus 4 new non-OCR worked example blueprints: chatbot + outbound
email, inbound Slack webhook, code generation, voice/phone agent. Agent
1 now has explicit picking rules for every plugin-routing decision.

**Agent 2 — atlas cache preload + kill-the-hallucination.** `research.py`
used to have ZERO references to `memdir` / `provider_atlas`, so every
Agent 2 run rediscovered Mindee/Veryfi/Stripe from scratch via web_search.
New `_preload_cached_provider_hints()` walks the TestPlan's capabilities,
queries the `provider_atlases` memdir category, and injects known-good
providers into the research prompt as pre-screened candidates (Claude
still decides per-user fit). Separately, `_salvage_findings_from_tool_uses`
used to feed raw search queries to the structurer, which HALLUCINATED
5-7 plausible-looking candidates with invented docs_url values when
web_search failed. Now the salvage mode EXPLICITLY instructs the
structurer to emit `candidates=[]` + coverage_notes explaining the
degradation — honest empty result beats fabricated candidates.

**Agent 3 — sufficiency intelligence.** Before: `test_count_target`
from Agent 1's TestPlan was echoed in the prompt but `validators.py`
used a hardcoded `< 3` threshold, silently accepting 3 tests when the
target was 20. Now: validator reads the target per-capability and
enforces `actual >= floor(target * 0.7)` — below floor becomes ERROR,
not warning. New `_topup_undergenerated_subtasks()` fires ONE focused
top-up LLM call that identifies the gap size + missing dimensions per
scope (happy_path / input_variation / edge_case / scale / domain_specific /
error_resilience) and asks Claude to generate only the gap-filling
cases. Merges new cases into the result. Cheap + bounded + closes the
"knows when enough is enough" gap the user specifically called out.

**Agent 4 — real rejection signal + OpenAPI path-param fix.** Previously
EVERY deep-verify rejection surfaced as hardcoded
`rejection_category="docs_inaccessible"` + boilerplate notes — the
model's actual `REJECT_REASON: no_api` / `enterprise_only` / `deprecated` /
`docs_unreachable` got thrown away at `screening.py:657`. New
`parse_rejection_from_spec()` extracts the real category + notes from
the VERIFICATION_COMPLETE block, maps via `_REJECT_REASON_TO_CATEGORY`
to the canonical `VALID_REJECTION_CATEGORIES` enum, and threads the
signal through `telemetry["rejection_details"]` → `screening.py` →
user-visible `RejectedCandidate`. Users now see "enterprise_only" /
"deprecated" / "no_api_access" with the model's real notes instead of
generic text. Separately, `openapi_harness.py:generate_harness_code()`
had a bug where path-parameter URLs like `/users/{id}` emitted literal
`{id}` in the request URL → 404. New `_extract_path_params()` +
runtime substitution from `input_data` + explicit missing-param error.
This alone likely fixes 30-50% of providers where the "mechanical
harness" shortcut was silently failing. Also removed the dead
`code.replace(X, X)` no-op the audit flagged.

**Agent 5 — user context threading + model fallback.** The biggest gap
by impact: `implement_test_env.py` had zero references to
`user_understanding.domain`, `technical_level`, `constraints`, or
`monthly_volume`. A healthcare team at 100k records/mo and a hobbyist
at 5/mo got IDENTICAL harnesses because the builder didn't know who
the user was. New `user_context_block` in `_build_initial_message`
extracts all of those fields, bands `monthly_volume` into LOW /
MODERATE / HIGH / VERY HIGH tiers with explicit endpoint-selection
guidance (batch vs atomic, sandbox vs production, high-level SDK vs
raw REST), and identifies the candidate's specific scope within the
workflow DAG (side_effects, role, description) so the builder knows
which step it's covering. The "first endpoint that works" problem is
now a "right endpoint for this user" problem.

Second Agent 5 upgrade: `call_with_model_fallback` was wired only into
Agent 1 via `parse_with_fallback`. Agent 5's builder loop (the deepest
and most expensive call) hard-failed on persistent Opus 4.7 rate-limits
after 3 SDK retries. Now wrapped — persistent 429 gracefully degrades
Opus → Sonnet → Haiku via the existing ladder in `anthropic_client.py`.

Third Agent 5 upgrade: the evaluator (`_evaluate_with_llm`) used to
rubber-stamp with one Claude call — no adaptive thinking, no
effort-tier propagation. A great harness with a sloppy evaluator
produces misleading scores. Now wired with `thinking={"type":"adaptive"}`
and `output_config_for_request()` so the judge reasons through synonym
mapping + partial-match semantics with the same rigor as the builder.

**Tests:** new `tests/test_agent_intelligence_upgrades.py` (15 cases
covering all 5 agents) + extended `test_will_it_just_work.py` with
two new assertions (all 11 enum values present in Agent 1 prompt + 4
non-OCR worked examples). +17 tests net, 834 core passing.

**Known gaps still open** (all architectural — not wiring):
- Live `agent_thinking` SSE streaming (extended-thinking blocks
  produced but not surfaced to UI)
- Incremental token streaming (Agent 5 uses blocking `messages.create`
  not `stream=True`)
- Agent 2 per-scope parallelism (single serial call vs N parallel)
- In-run web_fetch URL cache within Agent 4's deep-verify loop
- Idempotency keys + DRY_RUN propagation in Agent 5 harness writes
- Atlas schema_version enforcement
- AWS SigV4 / OAuth2 authorization_code / mTLS auth patterns

 Production code: ~40,900
LoC across PuzzleEval Python core (26,600), FastAPI backend (2,344), and
React frontend (12,018). Full documentation set lives at:

- `PuzzleEval-local/ARCHITECTURE.md` — module index + SSE event catalog + plugin ecosystem + honest gap list
- `puzzleeval-api/BACKEND_ARCHITECTURE.md` — endpoints + resilience infrastructure (§17) + new SSE events (§18) + env vars (§19)
- `AGENT_REFINEMENT_ROADMAP.md` — original 10-phase plan (all shipped) + status block at top
- `POST_ROADMAP_ENHANCEMENTS.md` — post-roadmap sections 1-22 incl. plugin pass (§21) + current session resilience pass (§22)
- `PLUGIN_KEYS.md` — credential surfaces + the 8 plugins + production knobs
- `README.md` (root) — 30-second quickstart + documentation index

Latest pass dispatched six parallel deep audits (robustness, bandaids,
Claude-Code-parity, plugin edges, test-data-quality, frontend errors) and
shipped fixes for every production-blocking finding plus high-leverage
capability boosts:

(NEW-L) **`puzzleeval/anthropic_client.py` — central client factory** —
Every agent used to construct its own `anthropic.Anthropic()` with bare
defaults (10-min timeout, no retries). A flaky TCP socket would hang the
entire pipeline for 10 minutes; a single transient 429 / 5xx killed the
run. The new factory wires `timeout=120s` (Agent 5 gets 240s for deep
thinking) and `max_retries=3` (covers transient 5xx + connection drops at
the SDK level). All 6 production agents now build clients through
`build_client(...)`. Includes a `call_with_model_fallback(...)` helper
that does Opus → Sonnet → Haiku degradation on persistent 429 — the
ladder is data-driven so any agent can opt in.

(NEW-M) **`puzzleeval/budget.py` — `RunBudget` cost circuit-breaker** —
Threadsafe per-run cost cap that raises `BudgetExceededError` when a run
exceeds `PUZZLEEVAL_MAX_RUN_COST_USD` (default $25). Prevents pathological
Agent 5 builder loops from running up unbounded $$. Snapshot-friendly for
SSE event surfacing; on-spend callback hook for live cost meters. 11 unit
tests including thread-safety + deadlock-free snapshot.

(NEW-N) **`schemas.py` — `frozenset[str]` → `list[str]`** — The deep
audit caught that `Candidate.covers_step_ids` and
`ScreenedCandidate.covers_step_ids` declared `frozenset[str]`. JSON
Schema has no native frozenset type, so LLM structured output couldn't
emit it correctly; the strict-grammar path always failed (forcing the
non-strict fallback) and the model occasionally emitted `"frozenset({'x'})"`
strings that Pydantic iterated char-by-char. Migrated both fields to
`list[str]` with sorted+deduped caller-side enforcement. Eliminates a
whole class of bugs at the source instead of patching downstream. All
upstream call sites in research.py / deep_verify_runner.py / pipeline.py
/ selection.py updated to use sorted-set list operations.

(NEW-O) **FastAPI lifespan handler + `allow_reuse_address` everywhere** —
The new plugins (webhook_receiver, outbound_delivery, voice_realtime)
spin up background HTTP/SMTP servers. Without explicit teardown, threads
+ sockets leaked across uvicorn restarts — the second start failed to
rebind the configured port and fell to ephemeral, silently breaking any
harness with hardcoded ports. New `lifespan` context manager calls
`shutdown()` on every plugin at app exit; all `_ThreadedHTTPServer`
subclasses now set `allow_reuse_address = True` so port re-bind works
across `uvicorn --reload` cycles.

(NEW-P) **DoS protection** — Three real OOM/exhaustion vectors closed:
  * `routes/files.py:upload_files` now stream-reads with `MAX_UPLOAD_BYTES_PER_FILE`
    cap (default 100 MiB), aborts with HTTP 413 instead of buffering an
    unbounded blob. Previous code did `await upload_file.read()` with no
    check — a 1 GB upload would exhaust server memory before any
    validation ran.
  * `outbound_delivery.py` SMTP handler now caps `_read_line` at
    `SMTP_MAX_LINE_BYTES` (8 KB per RFC 5321) AND total DATA at
    `SMTP_MAX_DATA_BYTES` (25 MiB matching Gmail's ceiling). Previously
    a malicious sender could stream a single line forever.
  * `outbound_delivery.py` HTTP receivers use `HTTP_MAX_BODY_BYTES`
    (1 MiB) instead of a hardcoded literal.

(NEW-Q) **Coverage gap detection** — When Agent 2 returns 0 candidates
OR when one or more workflow scopes have no covering candidates, the
pipeline now emits a `coverage_gap` SSE event with the `missing_scopes`
list and a user-facing message ("No candidates found — try broadening
your description"). Previously the pipeline silently completed with no
results, which the user reads as "the system broke." Frontend renders
the gap as a chat note immediately after research finishes.

(NEW-R) **`puzzleeval/report.py` — `EvaluationReport` assembler** —
Before this pass, the "final report" was the raw `agent_5_output.json`
artifact and a hardcoded recommendation sentence in the React UI. The
new assembler produces a structured `EvaluationReport` with: per-candidate
ranking by overall_score, per-scope winners (best covering candidate),
pass/fail counts, evidence (top 3 failures + top 3 successes per
candidate), monthly cost projection (uses `puzzleeval.pricing` with the
user's stated `monthly_volume`), deterministic pros/cons heuristics,
sandbox-disclosure flags, and coverage-gap advisories. Tolerates
partial inputs — emits informative advisories rather than crashing when
upstream agents returned empty. Persisted to
`runs/<trace>/evaluation_report.json` AND emitted via
`evaluation_report` SSE event AND served via new `GET /runs/{id}/report`
endpoint. 16 unit tests.

(NEW-S) **Plugin registry guard against silent name overrides** —
`register_plugin()` previously did `_REGISTRY[name] = plugin` with no
duplicate check. Two plugins with the same name silently fought for the
modality slot. Now: re-registering the SAME instance is a no-op
(idempotent), but registering a DIFFERENT instance under an existing
name logs a warning (or raises with `PUZZLEEVAL_STRICT_PLUGIN_REGISTRY=1`).

(NEW-T) **Frontend SSE auto-reconnect with backoff** — `subscribeToEvents`
wraps EventSource with exponential backoff (1s → 2s → … → 30s cap) and
preserves `lastEventId` across reconnects. New `onStatusChange` callback
surfaces `connecting | open | reconnecting | closed` so the UI can render
a "reconnecting…" banner instead of silently going dead on a network
blip.

(NEW-U) **Frontend handlers for `cost_update`, `coverage_gap`,
`evaluation_report`** — `cost_update` now drives the live cost meter
(was previously subscribed but never handled). `coverage_gap` renders as
an immediate chat warning. `evaluation_report` populates the new
`evaluationReport` state object that the results panel consumes. The
hook also exposes `sseStatus` and `lastEventAt` (heartbeat) for
"pipeline may be stuck" warnings during long agent phases.

(NEW-V) **`VITE_API_BASE` env override** — Frontend `API_BASE` reads
`import.meta.env.VITE_API_BASE` first, falling back to the dev-proxy
mount `/pzapi`. Deployers can point the SPA at any backend URL without
rebuilding.

(NEW-W) **Anthropic `parse_with_fallback` wired into all 6 structured-
output call sites** — Previously only Agent 1 used it. Now Agents 1, 2,
3, 3F, 4, AND 5 all route through `parse_with_fallback`, so any of them
hitting the compiled-grammar limit gracefully degrades to non-strict
tool-call mode. All 6 also benefit from the central client factory's
timeouts + retries.

Combined: **+33 PuzzleEval tests** (778 → 811). Zero regressions across
existing tests, the 30-case API suite, the 39-case generalizability
bench, or TypeScript. Vite production build clean. Backend boot via
TestClient + lifespan handler verified clean. Plugin readiness still
8/8.

(Earlier, NEW-A through NEW-K) closed a batch of audit-found gaps and
shipped the durable fix for Anthropic's compiled-grammar size limit on
large structured outputs:

(NEW-F) **`puzzleeval/structured_output.py` — `parse_with_fallback()`** —
drop-in replacement for `client.messages.parse(output_format=...)` that
falls back to `messages.create()` with a non-strict tool when the
strict-grammar path returns a 400 ("compiled grammar is too large" /
"Grammar compilation timed out"). Wired into ALL six structured-output
call sites: Agent 1 (user_understanding), Agent 2 (research structuring
step), Agent 3 (synthetic_tests), Agent 3F (synthetic_tests_file),
Agent 4 (screening structuring step), Agent 5 (LLM evaluator). The
fallback path also defends against two model-output quirks that the
strict path's grammar would have prevented: (a) over-nesting of the
result under an extra `input`/`result`/`arguments` key (defensive
unwrap), and (b) emitting Python repr strings like
`"frozenset({'step_1'})"` for fields the schema declares as arrays
(`_coerce_repr_strings_to_lists` walks and converts before Pydantic
validation). 22 tests in `tests/test_structured_output.py`. Verified
end-to-end: real pipeline run with REAL Agent 1 + REAL Agent 2 + REAL
Agent 4 verify reached `status: completed` cleanly with the fallback
firing 4+ times across the run.

(NEW-G) **`puzzleeval-api/services/pipeline_runner.py` — `_save_json` +
`_coerce_coverage` hardening** — the previous serializer used
`json.dumps(data, default=str)` which would call `str(frozenset(...))`
on any frozenset in the dict, producing the literal repr string
`"frozenset({'step_1'})"` on disk. Replaced with `_jsonify` that walks
the dict and converts frozenset/set/tuple to JSON-friendly equivalents.
`_coerce_coverage` extended with defensive string-pattern matching so
any path that did stringify a frozenset upstream still recovers cleanly.

(NEW-H) **`puzzleeval/agents/research.py` — `RESEARCH_MAX_TOKENS` bump
+ `_salvage_findings_from_tool_uses`** — Agent 2's Step 1 web-research
loop ran out of token budget mid-search on real workloads (one observed
run hit `stop_reason=max_tokens` with 8 tool_use blocks and zero text
findings), causing a hard pipeline failure. Bumped from 5000 → 12000
tokens for headroom; added a salvage helper that synthesizes a
findings paragraph from issued search queries when no text block is
present, so the Step 2 structuring pass still gets useful input.

(NEW-I) **Frontend SSE handler for `test_data_sufficiency`** —
`src/services/api.ts` adds the new event to `eventTypes`;
`src/hooks/usePipelineRun.ts` adds a switch case that turns the verdict
payload into a chat-rendered message ("Test data sufficiency check: …"
with READY/AUGMENT/SYNTHESIZE/REQUEST_MORE/DEGRADE per scope and
advisories rolled up). Without this the SSE event was being silently
dropped by the frontend.

(NEW-J) **Schema field descriptions de-bloated** — long Pydantic class
docstrings on `SubTask`, `InfoStatus`, `WorkflowBlueprint`, `TestPlan`
were trimmed to one-liners (the verbose explanations were duplicated
in agent system prompts anyway). Stale enum lists in `ScopeTestSpec` /
`TestCase` / `TestHarness` now reference `puzzleeval.validators` as
the canonical source instead of inlining the full enum, removing the
documentation drift risk surfaced in the audit.

(NEW-K) **Agent 1 wired through `parse_with_fallback`** — the audit
caught that Agent 1 had neither extended thinking nor effort. Now wired
with `thinking={"type":"adaptive"}` + `output_config_for_request()` AND
the grammar fallback. Set `PUZZLEEVAL_EFFORT=xhigh` for the deepest
planning on Opus 4.7. Source-grep regression guards in
`tests/test_agent1_thinking.py` (8 cases).

Combined: **+24 tests** (754 → 778 in core), zero regressions across
existing tests, the 30-case API suite, the 39-case generalizability
bench, or TypeScript. Vite build clean. Real end-to-end pipeline
verified through the FastAPI backend reaches `status: completed` with
real Agent 1 + Agent 2 + Agent 4 hitting the live Anthropic API.

The previous pass closes the
"sub-scenarios our pipeline can't fully test today" gap by shipping three
new local-only plugins, hardens Agent 1 with adaptive thinking + the
xhigh effort tier, and turns "is the user's test data sufficient?" into
a first-class structured verdict surfaced in every run:

(NEW-A) **`puzzleeval/test_data_sufficiency.py`** — the single source of
truth for "do we have enough test files, and what do we do if not." Pure
module (no Claude calls, no network) that returns a structured
`SufficiencyVerdict` per scope with one of five actions: READY, AUGMENT
(plugin synthesizes inputs), SYNTHESIZE (text fallback), REQUEST_MORE
(pause + ask user with concrete copy), DEGRADE (proceed with reduced
confidence). Wired into both the CLI (printed before Agent 3F fires) and
the FastAPI runner (emitted as a `test_data_sufficiency` SSE event with
per-scope verdicts + summary advisories). Detects wrong-extension uploads
("you sent .mp4 but this is OCR"), single-source variety risk (all files
share a 6-char prefix), and below-min counts (< 3 files / < 6 ideal).
12 test cases in `tests/test_test_data_sufficiency.py`.

(NEW-B) **`puzzleeval/tool_plugins/webhook_receiver.py`** — first-class
fix for the inbound sub-scenario (Slack `app_mention`, Intercom widget,
Stripe events, GitHub webhooks, Twilio SMS replies, generic webhooks).
Runs an in-process HTTP server bound to 127.0.0.1 (configurable port via
`PUZZLEEVAL_WEBHOOK_PORT`, default 8765). `synthesize_input()` returns a
unique-token callback URL + a provider-shaped envelope (slack /
intercom / twilio_sms / stripe / github / generic).
`evaluate_output()` inspects captured POSTs, scoring 0.6 for
"received at all" + 0.4 for substring match. Optional
`PUZZLEEVAL_TUNNEL_URL` advertises a public URL when an operator runs
ngrok / cloudflared out-of-band. 16 tests in
`tests/test_webhook_receiver.py`.

(NEW-C) **`puzzleeval/tool_plugins/outbound_delivery.py`** — first-class
fix for the outbound sub-scenario ("did the email actually land in the
inbox?"). Three local mock receivers spun lazily on first request:
SMTP (hand-rolled pure-socket implementation since `smtpd` was removed
in Python 3.12 — supports HELO/EHLO/MAIL/RCPT/DATA/RSET/NOOP/QUIT,
RFC 5321 transparency rule), channel HTTP (Slack-shaped), SMS HTTP
(Twilio-shaped form-encoded). Default ports 2525 / 8766 / 8767;
configurable via `PUZZLEEVAL_SMTP_PORT` / `PUZZLEEVAL_SLACK_MOCK_PORT`
/ `PUZZLEEVAL_SMS_MOCK_PORT`. Each receiver returns 200 + provider-
shaped JSON ack so candidates expecting normal responses stay happy.
Buffers are recipient/channel-keyed for filtered evaluation.
13 tests in `tests/test_outbound_delivery.py` exercise full SMTP send,
form-encoded SMS, JSON Slack messages, and recipient-filtered
verification.

(NEW-D) **`puzzleeval/tool_plugins/voice_realtime.py`** — local
audio-loopback that takes the voice/phone agent scenario as far as it
can go without a public phone number / TURN server. `synthesize_input()`
calls TTS (when available) to produce caller audio, exposes it at
`/audio/<token>`, and gives the candidate harness a `/voice/<token>`
callback URL plus a `/voice/<token>/recording` upload URL. `evaluate_output()`
extracts agent text from TwiML (`<Say>`/`<Play>`), Vonage NCCO (`talk` /
`stream` actions), generic JSON (`response_text`/`text`/`message`/`reply`),
or — when the response is an audio blob — invokes the transcription
plugin to STT it. 18 tests in `tests/test_voice_realtime.py`. Real
WebRTC/SIP fidelity is still cloud-deferred (needs publicly reachable
phone numbers); local loopback covers intent + response shape.

(NEW-E) **Agent 1 model + reasoning hardening** — Agent 1 now wires
`thinking={"type": "adaptive"}` and `output_config_for_request()` (same
pattern as Agents 2/4/5 since the reasoning-knobs pass). Default model
is Opus 4.7 (`AGENT1_MODEL=claude-opus-4-7`); default effort is `high`;
flip to `PUZZLEEVAL_EFFORT=xhigh` for deeper planning on complex
multi-step demands. Validated by source-grep regression guards in
`tests/test_agent1_thinking.py` (7 cases).

Combined: **+71 PuzzleEval tests** (684 → 755). Zero regressions across
existing tests, the 30-case API suite, the 39-case generalizability
bench, or TypeScript. New schema enums (`webhook_event`, `voice_turn`,
`webhook_callback`, `outbound_message`) extend `VALID_INPUT_TYPES` /
`VALID_OUTPUT_TYPES` and are taught to Agent 1's prompt so blueprints
can declare these scopes by name. Modality dispatcher routes them to the
right plugin automatically — no hardcoded `if scope_role == "voice"`
branches anywhere.

What's still cloud-deferred (needs a public endpoint or a real PSTN
number): real WebRTC/SIP bidirectional calls; outbound sequences
spanning multiple days (needs virtual-clock layer); chatbot widgets
clicked in a real browser (needs Playwright wrapper, ~1 day); code
generation in Node/Go/Rust without the host toolchain installed (one-time
user setup). Everything else from the original "what we can't do" gap
list is now testable locally with no extra infrastructure.

The previous pass adds four
performance + reasoning knobs from the Anthropic platform docs:
(1) `PUZZLEEVAL_EFFORT={low|medium|high|xhigh|max}` config knob applies
`output_config.effort` across every adaptive-thinking call (Agent 2
research, Agent 4 verify, Agent 5 builder + ask_research, deep_verify
runner) so users dial reasoning depth per workload; (2) Adaptive
thinking audit — every multi-turn agent now has both
`thinking={"type":"adaptive"}` and `output_config_for_request()` wired,
verified by source-grep regression guard tests; (3)
`puzzleeval/hybrid_evaluator.py` — opt-in via
`PUZZLEEVAL_HYBRID_EVAL_ENABLED=1` — when deterministic dispatch yields
no plugin for a test case, exposes all 5 plugins as Anthropic-callable
tools (via new `puzzleeval/plugin_tools.py`), lets Claude pick + invoke
one if it sharpens the verdict, falls through to LLM judge on plugin
fallback. Records `hybrid_evaluator` + actual plugin name in
`tools_used`. (4) Programmatic tool calling for Agent 5 builder via
`_build_tools_with_programmatic()` adapter — opt-in via
`PUZZLEEVAL_PROGRAMMATIC_TOOLS=1` — adds Anthropic's
`code_execution_20260120` tool and marks all custom builder tools
(write_file/patch_file/run_code/read_file) with
`allowed_callers=["direct", "code_execution_20260120"]`. Server tools
stay direct-only. Claude can write Python that chains many tool calls
in one container, estimated 30-50% cost + latency savings on multi-step
builds. Both new opt-in flags default OFF for behavioral parity until
soaked on real runs. Two follow-ups in this
pass: (1) **`PLUGIN_KEYS.md` + `puzzleeval-api/.env.example`** are the
single source of truth for where every API key goes — `.env` for
system-level keys (Anthropic + plugin providers like Whisper/Deepgram/
ElevenLabs), `provider_registry.json` for candidate API keys (Mindee,
Veryfi, OAuth flows), `os.environ` for overrides + diagnostic flags.
The CLI now auto-loads `.env` so users running `python -m puzzleeval.cli`
get the same credential surface as the FastAPI backend with zero manual
exports. (2) **Agent 3 + Agent 3F now use plugins for synthesis** —
Agent 3's system prompt teaches plugin-shaped test generation (code
tests get structured `{expected_function, test_inputs, test_outputs}`,
conversation tests get `{conversation_script: {user_turns, assertions}}`,
audio tests get exact spoken text as ground truth). Agent 3F handles
audio file uploads via the transcription plugin to extract spoken
content as ground truth, then the LLM generates test cases against
the transcript. §20 closed the
honesty gap on the §19 plugin shipment: plugins are now ACTUALLY
invoked during evaluation (`detect_for_test_case` runs first; plugin
scores or falls back to LLM judge) and during test-input synthesis
(TTS produces real audio files for audio_content scopes;
conversation_simulator generates default scripts; code_execution
generates FizzBuzz seeds). New `puzzleeval/plugin_status.py` is the
single source of truth for "what's wired, what's READY, which
credential to set" — surfaced in the CLI as a readiness matrix and in
`pipeline_summary.json` as `plugins` + `plugin_advisories`. New
`TestCaseResult.tools_used: list[str]` records which plugin (or
"llm_judge") scored each case for end-user observability. Tool
selection is fully deterministic by schema enum (input_type /
output_type) — no model-driven non-determinism in scoring. §19 shipped a
**tool-plugin architecture** for cross-modality test generation +
evaluation that closes the structural gap on the six product
categories: voice/phone agents (TTS plugin synthesizes audio inputs;
transcription plugin STTs audio responses for evaluation), code
generation (code_execution plugin runs generated code in sandboxed
subprocess across Python/JS/TS/Go/Rust/Bash), inbound + chatbot
agents (conversation_simulator plugin replays multi-turn scripts with
assertion checking and adapts to messages/conversation/history payload
shapes), document parsing (existing LLM judge), and image generation
(vision plugin formalizes the existing `vision_judge.py`). New module
`puzzleeval/modality.py` detects required plugins per test case
purely from the schema enums (input_type / output_type) — no `if
capability == X` branches anywhere. Plugins self-register at package
import; new plugins require zero changes to Agent 5. The builder's
initial message names the active plugins and the contract: "match your
response shape to the plugin's expected input." §18 closed the last
seven case-specific bandaids and shipped four capability extensions: (1)
adaptive timeouts that scale to 10 minutes when a candidate's atlas
declares async_polling/batch_file (no more silent OCR-sized timeouts on
video/ML/batch APIs); (2) generic `_role_tokens()` replaces the
hardcoded `_ROLE_KEYWORD_MAP` so any novel role (genome assembly,
music composition, climate modeling) matches OpenAPI operations
correctly; (3) `_format_atlas_context_for_builder()` injects every
populated atlas field (sandbox URL, interaction modes, upstream
provider, user-selectable params, spec path) into Agent 5's initial
message — the builder now ACTS on what Phase 6.5 already extracted;
(4) `file_parsers.py` accepts audio/video/archive/binary via
structured `file_reference` text — no more hard fail on `.mp3`, `.mp4`,
`.zip`; (5) new `puzzleeval/manual_atlas.py` ingests user-supplied
OpenAPI JSON or markdown spec for private/internal/auth-walled APIs
that web_fetch can't reach; (6) `OAuthCredentials` in
`provider_registry.py` supports OAuth client_credentials flow
alongside API-key env vars; (7) Agent 3F falls back to text-only
synthesis when no sample files supplied. New modules:
`puzzleeval/manual_atlas.py`. The codebase is now principle-based
across all 5 agents — no `if provider == X` branches, no
capability-specific prompt carveouts, no hardcoded brand lists. §17 closed the deeper
forms of Q3 + Q4: (Q3-deep) `puzzleeval/provider_atlas.py` extracts the
EXHAUSTIVE per-provider API surface — every endpoint, every request /
response shape, every error code, doc page map, openapi_url, SDKs — so
the cross-run cache lets ANY future user testing ANY scope of the same
provider hit a fully-warm cache. (Q4-deep) Build resilience via four
mechanisms: `puzzleeval/api_patterns.py` injects a 10-pattern catalog
into the builder prompt (REST+Bearer / multipart / async-polling /
OAuth / SSE / etc — copy-paste skeletons so the builder doesn't
rediscover trivial patterns); `puzzleeval/openapi_harness.py` mechanically
generates the harness when an openapi_url exists (zero LLM build turns);
`STRUCTURED_PIVOT_PROMPT` replaces the easy "give up" tier-3 reassessment
with a forced 3-different-approaches structured pivot; `LIVE_TEST_BATTERY_PROMPT`
makes happy/minimal/boundary/invalid_credential probes mandatory before
HARNESS_COMPLETE. New modules: `puzzleeval/provider_atlas.py`,
`puzzleeval/openapi_harness.py`, `puzzleeval/api_patterns.py`. §16 closed four
whole-picture gaps: (Q1) wired the previously-defined-but-unused
`DEEP_VERIFY_SYSTEM_PROMPT` into production via new
`puzzleeval/deep_verify_runner.py` so every new ScreenedCandidate field
(interaction_model, user_selectable_params, upstream_provider, sandbox_*,
api_spec_path) actually gets populated; (Q2) per-scope coverage_confidence
upgraded to "verified" only for scopes the ROUTING_TABLE confirms;
(Q3) provider-URL deduplication + cross-run memdir cache — three
same-URL candidates share ONE deep-verify call, and previously-verified
providers skip research within a 14-day TTL; (Q4) Agent 5 build-failure
fallback that pulls next-ranked verified candidates when all user picks
fail — guarantees a testable environment back instead of a hard "Zero
harnesses" failure. Toggleable via `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED`,
`PUZZLEEVAL_AGENT5_FALLBACK_ENABLED`. Post-roadmap §13 closed
sixteen local-relevant gaps. §14 refactored remaining case-based bandaids
into domain-agnostic principles. §15 cribbed five generalization
mechanisms from Claude Code's source (`src/tools/AgentTool/built-in/*`,
`src/services/compact/microCompact.ts`, `src/memdir/`,
`src/tools/AgentTool/built-in/verificationAgent.ts`):
(A) shared cross-cutting agent preamble injected into every agent;
(B) adaptive thinking on Agents 2 + 4 (multi-turn reasoning loops);
(C) server-side `context_management` on Agent 4's per-candidate verify;
(D) cross-run memory directory (`puzzleeval/memdir.py`) for api_specs
and quirks recall across runs; (E) adversarial verification battery
(`puzzleeval/adversarial_verifier.py`) — six PRINCIPLE-BASED probes
(empty / max / malformed / idempotency / concurrency / auth_error)
that gate harness readiness BEFORE Agent 3 cases run. New modules:
`puzzleeval/rate_limiter.py`, `puzzleeval/vision_judge.py`,
`puzzleeval/memdir.py`, `puzzleeval/agent_preamble.py`,
`puzzleeval/adversarial_verifier.py`. Gaps 4, 5, 17 (full
streaming/webhook enum), 24 remain cloud-deferred because webhook
reception requires a publicly-reachable callback URL.

### Original state (2026-04-11, before the production-ready pass)

**Agents 1, 2, 3, 4, and 5 are fully built and tested. Agent 5 builds thin API client harnesses AND executes all test cases, with LLM-judged evaluation** — harnesses send files/data and return raw API responses. A separate LLM judge compares raw responses against Agent 3F ground truth. Agents 7-9 are not yet built. The CLI supports running Agent 1 → Agent 2 via `--agent2`, Agent 1 → Agent 3 via `--agent3`, Agent 1 → Agent 2 → Agent 4 via `--agent4`, and Agent 1 → Agent 2 → Agent 4 → Agent 3 → Agent 5 via `--agent5`. When `--agent5` is used, Agent 2→4 and Agent 3F run in parallel for faster wall-clock time.

### Temporary Testing Shim: Registry Candidate Injection

For Agent 5 testing, a temporary shim injects all `provider_registry.json` providers into Agent 2's results (after Agent 2 runs) so they always appear as candidates for Agent 4/5. Injected candidates have `source="provider_registry_injection"` and `relevance_score=0.99` (guarantees top-N selection by Agent 5).

**TO REMOVE THIS SHIM** (when Agent 2 is reliable enough or moving to production):
1. Delete the `inject_registry_candidates()` function from `puzzleeval/agents/research.py` (the block marked `TEMPORARY TESTING SHIM`)
2. Delete the 2 shim lines + comments at ~line 222 in `puzzleeval/cli.py`
3. Remove `, inject_registry_candidates` from the import on `puzzleeval/cli.py` line 39

(Historical context: at the time of this original-state snapshot the frontend was being built separately via Lovable, and there was no backend API — agents were tested via CLI only. Both the FastAPI backend and the React/Vite frontend have since shipped and live in `puzzleeval-api/` and `src/` respectively.)

### Phase 1 Refinement: Cloudflare / Web Fetch Hardening (2026-04-14)

Anthropic's server-side `web_fetch_20250910` tool gets blocked ~5–10% of the time
by Cloudflare WAFs, 429 rate limits, or generic 5xx errors. The user-agent that
the tool sends is fixed by the API — we cannot rotate headers — so the fix is
DETECT and PIVOT, not impersonate.

**`puzzleeval/web_fetch_fallback.py`** classifies the eight `WebFetchToolResultErrorCode`
values into recoverable (`url_not_accessible`, `too_many_requests`, `unavailable`)
and non-recoverable (`invalid_tool_input`, `url_too_long`, etc.). For each
recoverable block in a model response we:

- Track the count for observability.
- Apply a 5-second backoff before the next turn if a 429 was seen.
- (Agent 5 only) Inject a fallback guidance text block into the same user
  message as the tool_results, telling the model to try `web_search 'site:DOMAIN TOPIC'`,
  GitHub SDK repos, alternate docs subdomains, or `web.archive.org` snapshots.

**Where wired in:**
- Agent 4 (`screening.py`): per-candidate count returned as the third tuple element from
  `_verify_single_candidate`, summed in `run_screening_agent`, surfaced as `Agent4Result.web_fetch_blocks`.
- Agent 5 (`implement_test_env.py`): `candidate_web_fetch_blocks` accumulated inside
  `_build_single_harness`, set on every `TestHarness` / `FailedHarness`, summed into
  `Agent5Result.web_fetch_blocks`.
- `pipeline.py`: `AgentRecord.metadata` auto-promotes `web_fetch_blocks` from
  output schemas via `_AUTO_METADATA_FIELDS`. `pipeline_summary.json` now carries
  per-agent and run-level totals when blocks are non-zero.
- Agent 2 (`research.py`): no fetches today, no integration needed.

**Toggle:** `PUZZLEEVAL_ENABLE_FETCH_FALLBACK=0` (default `1`/on) — disables detection
and fallback. `PUZZLEEVAL_FETCH_RATE_LIMIT_BACKOFF=N` (default `5`) — backoff seconds.

**Tests:** `tests/test_web_fetch_fallback.py` (21 cases) — covers detection,
fallback-message construction, backoff stub, summary aggregation, and the
`PipelineRun.save_agent_result` / `finalize` integration.

### Phase 1.5 Refinement: Content-Quality Assessment (2026-04-14)

Phase 1 hardens the **HTTP layer** (403, 429, 5xx). Phase 1.5 hardens the
**content layer**: a fetch can return HTTP 200 and still be useless to the
agent — JavaScript SPA shells (Next.js, React, Vue, Angular), login walls,
soft 404s ("page not found" served as 200), or marketing-only landing pages.
Same recovery path as Phase 1; just a broader trigger surface.

**Design principle:** identify usable pages by **positive signals** —
endpoint patterns, auth examples, code blocks, prose with API keywords. ANY
positive signal → page is usable, regardless of how it was rendered. Only
when zero positive signals fire do we run a secondary classification ("why
is this empty?") to specialize the recovery message. This avoids hardcoding
framework markers (Next.js, React) as the primary detector — they evolve
too fast and miss adjacent failure modes (auth walls, marketing pages)
entirely.

**`puzzleeval/web_fetch_fallback.py`** gains:
- `assess_content_quality(text) -> ContentVerdict` — two-stage classifier.
  Stage 1: scan for endpoint regex (`(GET|POST|...)\s+/...`), auth markers
  (`Authorization:`, `Bearer `, `X-API-Key:`), code calls (`curl`,
  `requests.post`, `fetch(`, `axios.`), or ≥500-char prose body containing
  API keywords (`endpoint`, `authentication`, `parameter`, etc.).
  Stage 2: classify why empty as `script_rendered` / `auth_wall` /
  `not_found_soft` / `unknown_useless`.
- `extract_unusable_pages(response)` — complement to
  `extract_blocked_fetches`. Scans successful `web_fetch_tool_result`
  blocks, runs the classifier, returns dicts of `{url, category,
  tool_use_id}` for any page that comes back unusable.
- `build_fallback_message(blocked, unusable)` — extended to include
  category-specific recovery guidance (SPA pivot to deep URLs / openapi
  search / GitHub SDK; auth-wall pivot to archive.org / GitHub SDK; soft
  404 pivot to sitemap / search; unknown pivot to broader search).
- `count_actionable_problems(blocked, unusable)` — sums recoverable HTTP
  errors + unusable pages into one count. Wired into Agent 4/5's
  `web_fetch_blocks` field.

**Where wired in:**
- Agent 4 (`screening.py`): per-turn detection alongside the existing
  block scan, both folded into `candidate_block_count`.
- Agent 5 (`implement_test_env.py`): per-turn detection alongside the
  existing block scan, unified guidance appended to `tool_results`.
- `Agent4Result.web_fetch_blocks` / `Agent5Result.web_fetch_blocks` /
  `TestHarness.web_fetch_blocks` / `FailedHarness.web_fetch_blocks`
  docstrings updated to reflect the expanded semantics ("HTTP error OR
  content-level failure").

**Toggle:** same `PUZZLEEVAL_ENABLE_FETCH_FALLBACK=0` flag as Phase 1
disables both classifiers in one switch.

**Tests:** `tests/test_web_fetch_fallback.py` extended to 43 cases —
adds positive-signal detection (endpoint / auth / code-call / prose),
secondary classification (4 categories), `extract_unusable_pages`
behavior, combined Phase 1 + 1.5 guidance messages, and a Stripe-style
negative-regression case proving rich code-heavy docs do NOT misclassify
as `script_rendered`.

**Open verification:** end-to-end success rate against real SPA-rendered
providers will land via the Phase 10 generalizability bench, which is
spec'd to include 3-4 SPA-rendered vendors (e.g., DocuClipper) so we can
measure actual recovery-vs-baseline.

### Phase 2 Refinement: Service Tier Scaffold (2026-04-14)

User-facing PuzzleAI plans (Free / Paid / Enterprise) wired through the
backend with a NO-OP DEFAULT — when `PUZZLEEVAL_BILLING_ENFORCED=0`
(today), every gate is observability-only; agents run as before. Flipping
the env var to `1` turns gates into hard `HTTPException(402)` blocks
without any other code change.

**`puzzleeval-api/services/billing.py`** is the single point of policy:
- `PLAN_FEATURE_MATRIX` — `free`/`paid`/`enterprise` × `search`/`testing`/`monitoring`
- `CREDIT_COST_PER_AGENT` — Agents 1-3 free, Agent 4 = 1 credit, Agent 5 = 5 credits
- `PLAN_STARTING_CREDITS` — `free`/`enterprise` = unlimited; `paid` = 100
- `require_agent_access(state, agent)` — single gate called from
  `pipeline_runner.py` before Agent 4 and Agent 5 entries. Raises 402 in
  enforced mode, increments `state.plan_gates_triggered` in no-op mode.
- `quota_snapshot(state)` — serializes plan + credits + features for the
  frontend `Quota` model.

**Where wired in:**
- `puzzleeval-api/services/run_manager.py` — `RunState` extended with
  `plan`, `credits_remaining`, `credits_consumed`, `plan_gates_triggered`.
  `RunManager.create_run(plan="...")` initializes the credit balance
  via `billing.starting_credits_for(plan)`.
- `puzzleeval-api/routes/runs.py` — POST `/runs` accepts optional
  `plan` field; GET `/runs/{id}` returns `Quota` block via
  `billing.quota_snapshot(state)`.
- `puzzleeval-api/services/pipeline_runner.py` — Agent 4 and Agent 5
  entries wrapped with `try/except HTTPException` → emit `agent_blocked`
  + `pipeline_failed` SSE events on 402; otherwise pipeline runs
  normally.
- `puzzleeval-api/routes/monitoring.py` — new stub router with two
  enterprise-gated endpoints (`/monitoring/{run_id}/status`,
  `/monitoring/{run_id}/check`). Returns 402 unless `plan == "enterprise"`,
  regardless of `BILLING_ENFORCED`. Real handlers land in a future phase.
- `src/components/playground/QuotaBadge.tsx` — header badge that polls
  `GET /runs/{id}` every 5s while a run is active. Shows `Free · search only`
  / `Paid · 95 credits` / `Enterprise · unlimited`. Goes red when
  `billing_enforced=true` AND `credits_remaining<=0`.
- `src/types/pipeline.ts` — `Plan` and `Quota` types mirror the backend.
- `src/services/api.ts` — `createRun(..., plan)` plumbs the plan through;
  new `getRunState(runId)` call backs the badge polling.

**Toggle:** `PUZZLEEVAL_BILLING_ENFORCED=1` flips on enforcement. Default
`0` keeps every existing call path (CLI + frontend) running unchanged
while still tracking usage for observability.

**Tests:** `puzzleeval-api/tests/test_billing.py` (19 cases) — covers
plan/feature matrix lookups, per-agent credit cost, RunState
extensions, `require_agent_access` in both no-op and enforced modes,
and `quota_snapshot` serialization. Combined with the 211-case
PuzzleEval suite: 230 tests passing.

**Frontend verification:** `bun run dev` (Vite on :8080); navigate to
`/playground`; QuotaBadge renders in the header showing `FREE search only`
when no run is active. The badge's tooltip reads
`Plan: Free · search only · Advisory` (advisory = enforcement is off).

**Phase fingerprint:** `metadata.credits_consumed`,
`metadata.plan_gates_triggered` (will appear in `pipeline_summary.json`
once `pipeline_runner.py` is migrated to call `pipeline_run.save_agent_result`
— pre-existing gap noted in the Phase 1.5 wiring discussion;
fix folded into Phase 2 since we touched these files anyway). Per-call
billing logs include `{plan, agent, credits_after, credits_consumed_total,
trace_id}` via `logger.info("billing_gate_passed", ...)`.

**Diagnostic flag:** `PUZZLEEVAL_BILLING_ENFORCED=0|1`. When 0:
gates record but never raise. When 1: 402 on first failure, pipeline
emits `agent_blocked` + `pipeline_failed` SSE events, run status
becomes `failed`.

### Phase 3 Refinement: WorkflowBlueprint + Agent 1 as Director (2026-04-14)

Agent 1 was a parser — extract sub-tasks from user text. Phase 3 promotes
it to a **director**: decompose the user's demand into an ordered
`WorkflowBlueprint` with step ordering, data flow, role assignment, and
architecture options (all-in-one vs best-per-step). Downstream phases
branch on this structure — Phase 4's dual search groups candidates by
step role, Phase 6's selection UI renders the step chain, Phase 9's
workflow harness wires `step_N.run() → step_N+1.run()`.

**Model promotion.** `AGENT1_MODEL` is now **Opus 4.7** (was Sonnet 4.6).
Override with `PUZZLEEVAL_AGENT1_MODEL=claude-sonnet-4-6` to revert.
Cost impact: ~$0.10 per evaluation (was ~$0.05) — roughly 1% of the
pipeline total; negligible at scale. Opus's stronger planning reasoning
is what lets Agent 1 reliably emit consistent blueprints.

**Schemas (additive).** `puzzleeval/schemas.py` adds:
- `WorkflowStep` — `id`, `role`, `description`, `capability`, `input_from`,
  `output_format`, `depends_on[]`, `all_in_one_compatible`. The
  `capability` field is a string join key matching `SubTask.capability`.
- `WorkflowBlueprint` — `steps[]`, `architecture_options[]`, `notes`.
- `UserUnderstandingOutput.workflow: WorkflowBlueprint | None` — optional,
  default `None`. Pre-Phase-3 saved artifacts still parse; downstream
  phases branch on `workflow is not None`.

**Prompt extension.** `user_understanding.py`'s `SYSTEM_PROMPT` grows a
"Workflow Blueprint" section that teaches the director role:
- Mirror `sub_tasks` (3 sub-tasks → 3 steps, capability strings match).
- Order by data flow (`input_from="user"` first, then `input_from="step_N"`).
- Don't invent structure (single-capability requests → 1-step blueprint).
- `depends_on` is the DAG source of truth; `steps[]` order is presentation.
- Only emit `workflow=None` when the request is truly unstructurable.

**Validator.** `validators.py`'s `validate_agent1_output` now checks:
- ID uniqueness (errors on duplicates — breaks Phase 9 harness keying)
- `depends_on` references (errors on orphans — would silently skip at runtime)
- `input_from` is either `"user"` or a valid step id
- `capability` cross-refs at least one SubTask (warning, not error — tolerates
  slight wording drift)
- Role non-empty; role length reasonable

**Where wired in:**
- `puzzleeval-api/services/pipeline_runner.py` — emits new SSE event
  `workflow_blueprint` after Agent 1 completes. Payload is the blueprint
  dict or `null`. Frontend handler lives in `usePipelineRun.ts`.
- `src/types/pipeline.ts` — `WorkflowStep` and `WorkflowBlueprint` types
  mirror the Python schemas.
- `src/components/playground/WorkflowDiagram.tsx` — new component. Renders
  a horizontal step chain with arrows, role/description cards, architecture
  badges, and Agent 1's notes in an italic tooltip below. Returns null when
  `blueprint === null` (pre-Phase-3 fallback).
- `src/pages/Playground.tsx` — renders `<WorkflowDiagram blueprint={workflow}>`
  above the candidate list when `stage !== "conversation"`.

**Toggle:** none — schema-only. Phase 3 cannot be disabled because
downstream phases (4, 6, 7, 9) consume the blueprint. If Agent 1 fails to
produce a blueprint (`workflow=None`), downstream branches to legacy
flat-sub-tasks behavior automatically.

**Tests:** `tests/test_agent1.py` adds 6 schema cases (single-step,
multi-step, default architecture_options, JSON round-trip, backward-compat
with/without workflow). `tests/test_validators.py` adds 10 validator cases
(valid blueprint, null blueprint, empty steps, duplicate ids, empty id,
orphan depends_on, orphan input_from, capability drift → warning, empty
role, "user" input_from valid). Combined: 227 PuzzleEval + 19 billing =
**246 tests passing**.

**Frontend verification:** end-to-end drive in preview — drove mock
pipeline through 2 conversation turns → `workflow_blueprint` SSE event
fired → `WorkflowDiagram` rendered showing 2-step workflow ("ocr" →
"accounting_sync") with arrow, architecture badges, and Agent 1 notes.
Screenshot captured. TypeScript + Vite production build both clean.

**Phase fingerprint:** `agent_1_output.json.result.workflow.steps` length
`> 0` on new runs; missing/null on pre-Phase-3 artifacts. Frontend reads
via `workflow_blueprint` SSE payload.

**Diagnostic flag:** schema-only — no flag. Revert with
`PUZZLEEVAL_AGENT1_MODEL=claude-sonnet-4-6` if the new Opus prompt
produces worse blueprints than expected (unlikely, but a safety lever).

### Phase 3 DAG Expansion (2026-04-15)

The initial Phase 3 linear form shipped the schema + validator + SSE event +
frontend chain renderer. The DAG expansion completes Phase 3 by teaching
Agent 1 to author workflows with parallelism, adding cycle/unreachable
validation, and rewriting `WorkflowDiagram` for topological layout.

**Agent 1 prompt — DAG authoring.** `user_understanding.py`'s SYSTEM_PROMPT
grows a new rule block:
- "Parallelism is default, not opt-in" — two steps whose `depends_on` lists
  don't reference each other are implicitly parallel. Only serialize when a
  step literally needs its predecessor's OUTPUT as INPUT.
- "Fan-in merge steps are explicit" — when the user says "combine / merge /
  reconcile, then sync," emit a distinct final step that depends_on every
  parallel branch.
- "Acyclic" — never emit A depends_on B AND B depends_on A.
- Two new worked examples: a fan-out+fan-in DAG (OCR → 3 parallel
  enrichments → merge) and a two-root parallel ingestion (photos + audio
  → summary).

**Schema — `parallel_group: str \| None`.** New optional field on
`WorkflowStep`. Purely a layout hint: steps sharing the same non-null tag
cluster visually in the WorkflowDiagram; `depends_on` remains authoritative
for execution semantics. Omit (leave null) for linear chains. Mirrored in
`src/types/pipeline.ts`.

**Validator — cycle + unreachable checks.**
`validate_agent1_output` now runs DFS-based cycle detection on the
`depends_on` graph once orphan refs are clear. A cycle is a hard error
with the cycle path spelled out in the message (`step_1 -> step_2 -> step_1`).
Unreachable steps (no path from any root) are soft warnings; a multi-step
blueprint with zero roots is flagged with a specific "no root" warning.
Cycle check short-circuits when orphan `depends_on` refs exist so we don't
walk broken edges.

**Frontend — topological layer layout.** `WorkflowDiagram.tsx` was
rewritten from a horizontal chain to a per-layer column layout:
- Layer = longest-path-from-roots. Roots sit at layer 0; each downstream
  step is `1 + max(layer of deps)`.
- Within a layer, steps sharing a non-null `parallel_group` render inside
  a dashed border container tagged with the group name; solo steps render
  without chrome.
- Edges are SVG cubic Beziers from each source node's right-middle to the
  target's left-middle, measured via `useLayoutEffect` + ResizeObserver so
  connection lines track real DOM positions as the panel resizes.
- 1-step blueprint still renders as a single card (same visual density as
  before). Multi-step linear chains render as N columns of 1 (same as the
  old chain view). DAGs get proper fan-out/fan-in visuals.
- Header shows "N steps · DAG" when any layer has >1 node, otherwise
  just "N steps". No external graph library — topological layout is
  hand-rolled (~200 lines). Dagre / elkjs remain optional future polish
  if blueprints ever exceed ~10 nodes in practice.

**Tests:** 4 new schema cases in `test_agent1.py` (`parallel_group`
default, round-trip, fan-out+fan-in shape assertions, two-root parallel
ingestion) + 5 new validator cases in `test_validators.py`
(two-step cycle, three-step cycle, fan-out+fan-in passes,
no-root multi-step → cycle error, parallel_group metadata survives
validation). Combined: 236 PuzzleEval + 19 billing = **255 tests passing**.

**Phase fingerprint:** unchanged — `agent_1_output.json.result.workflow.steps`
still the primary signal. For DAG detection specifically, look for
`parallel_group` populated on ≥1 step OR any layer with >1 sibling
(the frontend's "DAG" badge uses the latter rule).

**Diagnostic flag:** none added — the DAG expansion is prompt + validator
+ frontend only. `PUZZLEEVAL_AGENT1_MODEL=claude-sonnet-4-6` remains the
soft revert lever if the new parallelism rules produce worse blueprints
than expected.

### Phase 4 Refinement: Agent 2 Dual Search + Ranked Candidate Pool (2026-04-15)

Agent 2 was a single-pass surveyor — one search strategy, one flat list of
5-7 candidates. Phase 4 turns it into a DUAL surveyor when Agent 1's
blueprint has N ≥ 2 scopes: one all-in-one horizontal search (Zapier /
n8n / Make / Workato) PLUS one per-scope specialist search per step. Every
candidate now carries a `covers_step_ids: frozenset[str]` claim plus a
`coverage_confidence: dict[str, "claimed" | "verified"]` tag. Dedup by
candidate name merges coverage sets when the same tool surfaces in both
passes. Agent 2 **never verifies** — all confidence values are "claimed";
Phase 6.5's Agent 4 deep-verify later upgrades confirmed scopes to
"verified" or removes them entirely.

**Schemas.** `puzzleeval/schemas.py::Candidate` gains:
- `covers_step_ids: frozenset[str] = Field(default_factory=frozenset)` —
  blueprint step IDs this candidate claims to cover. Arbitrary size: a
  provider may cover 1, 2, or all N scopes and competes independently at
  every scope it claims. No more "multi-step vs specialist" classes —
  everything is just a coverage SET. Empty for legacy flat flow.
- `coverage_confidence: dict[str, str] = Field(default_factory=dict)` —
  per-scope `"claimed"` / `"verified"` tag. Keys align with
  `covers_step_ids`; validator rejects drift between the two fields.

**Prompt + tool config.** `puzzleeval/agents/research.py` gets:
- A rewritten `RESEARCH_SYSTEM_PROMPT` that teaches the dual-search
  strategy: survey + per-scope when N≥2, legacy single-pass otherwise.
  Explicitly states: coverage is NOT a scoring dimension (a 5-scope tool
  doesn't outrank a 1-scope specialist at OCR).
- A rewritten `STRUCTURE_SYSTEM_PROMPT` explaining how to populate
  `covers_step_ids` and `coverage_confidence` per candidate.
- `_build_web_search_tool(blueprint)` — dynamic `max_uses`:
  single-scope/no-blueprint → `SINGLE_SEARCH_MAX_USES` (3); multi-scope →
  `N+1`, capped at `DUAL_SEARCH_MAX_USES_CEILING` (8) so cost stays
  bounded for pathological blueprints.
- `_build_research_message` includes the blueprint as a dedicated section
  so Claude sees every step's id/role/capability before planning searches.
- `_normalize_coverage()` — deterministic post-processing: dedup by
  case-insensitive name merging coverage, drop hallucinated step IDs,
  auto-fill coverage on 1-scope blueprints, clamp any bogus "verified"
  value back to "claimed" (Agent 2 can't verify).

**Validator.** `validate_agent2_output` gains Phase 4 checks when Agent 1
produced a blueprint AND at least one candidate has non-empty coverage:
- Warn when a scope has zero candidates claiming coverage (Phase 6.5 has
  nothing to deep-verify there).
- Warn when a scope has 1-2 candidates (thin pool — target is ≥3).
- Error when `coverage_confidence` keys drift from `covers_step_ids`
  (structural bug in Agent 2 output).
- Error when `coverage_confidence` values are anything other than
  `"claimed"` or `"verified"`.
- Legacy flat flow (every candidate has empty coverage) silently skips
  the Phase 4 block.

**Shim update.** `inject_registry_candidates()` now accepts an optional
`blueprint` argument and stamps injected test providers with claimed
coverage over EVERY blueprint scope. Keeps the "test every provider we
have keys for" intent — injected providers surface in every per-scope
top-K list. Call sites in `puzzleeval/cli.py` (twice) and
`puzzleeval-api/services/pipeline_runner.py::_run_real_agent2` all pass
the blueprint through.

**Pipeline observability.** `puzzleeval/pipeline.py` gains a
`_DERIVED_METADATA_EXTRACTORS` registry — extractors receive the output
model and return `(key, value)` tuples to write into
`AgentRecord.metadata`. The first entry is `_agent2_coverage_metadata`
which writes `phase4_dual_search_active`, `phase4_scopes_covered_count`,
and `phase4_coverage_populated_all` when Agent 2 output has any coverage
populated. `finalize()` promotes these to run-level
`pipeline_summary.json:metadata.*` so ops can grep for
`phase4_dual_search_active=true` to answer "did Phase 4 fire on this run?"
without reading the full agent output.

**Backend SSE.** `pipeline_runner.py::_branch_a_research_and_screening`'s
`candidates_found` event payload now includes `covers_step_ids` (as a
sorted list — frozenset → JSON list) and `coverage_confidence` (dict)
per candidate. A `_coerce_coverage` helper handles the frozenset / list
/ tuple / set cases defensively so the shim's output doesn't break SSE
serialization.

**Frontend.** Three files land:
- `src/types/pipeline.ts` — `PipelineCandidate` gains `covers_step_ids:
  string[]` and `coverage_confidence: Record<string, CoverageConfidence>`.
  `CoverageConfidence = "claimed" | "verified"` is exported for reuse.
- `src/components/playground/CoverageBadge.tsx` — new component.
  Renders "covers K/N scopes" plus per-role chips with amber dots
  (claimed) / emerald dots (verified). Has a `compact` variant for
  the early-discovery row. Aggregate status tints the header chip
  ("claimed, awaiting verify" vs "verified").
- `src/components/playground/CoverageMatrix.tsx` — new component.
  Candidates × scopes table. Rows sorted by coverage count desc →
  relevance desc → name (so all-in-ones rise to the top, specialists
  below). Cells: empty for uncovered, amber ⦿ for claimed, emerald ✓
  for verified. Footer row shows per-scope depth (red if zero, amber if
  <3, emerald if ≥3). Sticky left column, horizontally scrollable on
  narrow panels. Renders only when blueprint has ≥2 scopes AND at least
  one candidate has coverage populated — collapses to nothing otherwise.
- `src/pages/Playground.tsx` mounts `<CoverageMatrix>` above the
  candidate list (below `WorkflowDiagram`) and passes `workflow?.steps`
  into `<CandidateCard>` so the badge can render.
- `src/hooks/usePipelineRun.ts` parses `covers_step_ids` +
  `coverage_confidence` out of the `candidates_found` SSE payload,
  normalizing bogus confidence values to `"claimed"`. Default fields
  added to the two fallback `PipelineCandidate` constructors
  (`harness_started`, `candidate_results_ready`) so the interface
  stays exhaustive.

**Mock data backfill.** `PuzzleEval-local/runs/working_test_6/agent_2_output.json`
— used as mock seed by the FastAPI layer — backfilled with coverage:
Veryfi/Mindee/Taggun/Klippa/DocuClipper claim only `step_1`;
Parseur/Parsio/Nanonets claim both `step_1, step_2` (all-in-one
invoice + accounting pattern). Lets the mock-mode pipeline driver
exercise the CoverageMatrix without a real API key.

**Toggle.** `PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED=0` reverts to
single-pass behavior — `_build_web_search_tool` always returns
`max_uses=3`, the prompt still has blueprint context but Claude does
legacy-style search, and `_normalize_coverage` still runs so downstream
sees empty or single-scope coverage depending on the flat-flow path.

**Tests:** 27 new cases across three test files:
- `tests/test_agent2.py` +18: schema defaults, JSON round-trip, dual
  search tool config (1/2/5/15/disabled scopes), research message
  rendering (multi-scope / no-workflow / 1-scope), coverage
  normalization (dedup, case-insensitive merge, hallucinated IDs
  dropped, 1-scope autofill, legacy empty, verified clamp, higher
  relevance kept on dedup).
- `tests/test_validators.py` +5: uncovered-scope warning, thin-scope
  warning, confidence-key drift error, invalid-confidence-value
  error, legacy-flat-flow skip.
- `tests/test_pipeline.py` +4: coverage metadata auto-promoted,
  absent for legacy flow, finalize() rolls metadata to run-level,
  mixed populated → all-flag false.

Combined: **263 PuzzleEval + 19 billing = 282 tests passing**. `tsc`
clean, Vite production build clean (`index-*.js` 728 kB / 226 kB
gzipped — same shape as before).

**Cost delta.** Single-pass (1-scope / no blueprint / flag off): ~$0.35
per Agent 2 run (3 searches + tokens + structure pass — unchanged from
baseline). Dual search on a 3-scope blueprint: ~$0.40 per run (4
searches + slightly more tokens in the survey pass). 5-scope: ~$0.45
(6 searches). Hard ceiling at 8 searches caps Agent 2 at ~$0.50 even
for very large blueprints. Rounding error vs the ~$6 full-pipeline
total — but worth knowing when reading the cost dashboard.

**Phase fingerprint:** primary signal is
`agent_2_output.json.candidates[].covers_step_ids` (non-empty frozenset
per candidate). Run-level rollup lives in
`pipeline_summary.json:metadata.phase4_dual_search_active` (bool),
`.phase4_scopes_covered_count` (int), and
`.phase4_coverage_populated_all` (bool). `CoverageMatrix` mount in the
playground is the frontend-side signal.

**Diagnostic flag:** `PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED=0`. Set
this and every blueprint size takes the single-pass path. Schema fields
stay populated (empty frozenset + empty dict) so no downstream breakage.

### Phase 5a Refinement: Pricing Data Layer (2026-04-15)

Phase 5 splits into two landings by design: Phase 5a (shipped now) lands
the data contracts + helper functions + frontend scaffold so structured
pricing is a null-safe field everywhere; Phase 5b (lands inside Phase
6.5's 4B extraction) populates that field from real docs. Shipping 5a
ahead of 5b means when 6.5 runs, pricing flows through to the UI
automatically — zero new schema/frontend work at 6.5 for pricing.

**What shipped (Phase 5a):**

*Schemas (`puzzleeval/schemas.py`):*
- `PricingTier` — one tier with `name`, `monthly_cost_usd`, optional
  `included_units` / `unit_name` / `overage_cost_per_unit_usd` / `notes`.
- `PricingBreakdown` — `tiers` (cheapest-first), optional
  `free_tier_monthly_units`, `pay_as_you_go` flag, `billing_granularity`
  (`monthly` / `per_call` / `annual_commit` / `hybrid`),
  `per_scope_unit_cost` (dict keyed by step_id for variable pricing),
  `sources` (URLs), `confidence` (`high`/`medium`/`low`), optional `notes`.
- `Candidate.pricing_breakdown: PricingBreakdown | None = None` — Agent 2
  never fills it. When null, downstream falls back to the legacy
  `pricing_model` / `pricing_details` strings.
- `ScreenedCandidate.pricing_breakdown: PricingBreakdown | None = None` —
  Phase 6.5's 4B extraction populates it alongside endpoints.

*Pure helpers (`puzzleeval/pricing.py`):*
- `estimate_monthly_cost(breakdown, monthly_volume) -> float` — walks
  every tier, computes effective cost at that volume (base + overage for
  overflow tiers, $0 for volumes inside included allowance), returns
  the minimum. Handles capped-no-overage tiers, pure PAYG, flat-rate,
  and freemium.
- `per_scope_unit_cost(breakdown, scope_id) -> float | None` — explicit
  `per_scope_unit_cost[scope_id]` wins; falls back to cheapest tier's
  overage; returns None for flat-rate.
- `monthly_cost_for_scope_map(breakdown, scope_volumes) -> float` —
  per-scope summation for CoverageMatrix "what will this actually cost
  me?" view.
- `cheapest_meaningful_tier` — skips $0-no-overage filler tiers when a
  real paid tier exists.
- `format_tier_short` / `format_breakdown_short` — UI string helpers
  with best-effort singularization (`pages` → `page`, `queries` →
  `query`) so "$0.005/call overage" reads natural.

*Validator (`puzzleeval/validators.py`):*
- New shared helper `_check_pricing_breakdown(candidate_name,
  covers_step_ids, breakdown, errors, warnings)`. Silent when breakdown
  is None (overwhelming default today). When populated:
  - Errors: empty tiers, missing sources, invalid confidence /
    billing_granularity, `per_scope_unit_cost` keys outside
    `covers_step_ids`, negative costs / overages.
  - Warnings: `confidence=low` (emitter struggled — ops review).
- Wired into both `validate_agent2_output` and `validate_agent4_output`
  — covers the rare case where Agent 2 sees pricing early (test
  fixtures; future heuristics) and the normal case where Phase 6.5's 4B
  populates on ScreenedCandidate.

*Frontend (`src/types/pipeline.ts`, `src/lib/pricing.ts`, playground
components):*
- Types `PricingTier`, `PricingBreakdown`, `PricingConfidence` mirror
  Python schemas. `PipelineCandidate.pricing_breakdown?:
  PricingBreakdown | null` added.
- `src/lib/pricing.ts` — TypeScript port of `puzzleeval/pricing.py`.
  Kept literal with the Python so behavior stays in sync. All functions
  `null | undefined`-safe.
- `src/components/playground/PricingBlock.tsx` — new. Header line uses
  `formatBreakdownShort` ("From $X/mo" / PAYG variant), confidence dot
  (emerald / amber / red), expandable tier list with `formatTierShort`,
  per-scope chips from `perScopeUnitCost` (only when pricing varies by
  scope), source links at the bottom.
- `src/components/playground/CandidateCard.tsx` — mounts `<PricingBlock>`
  when `c.pricing_breakdown` is non-null. Invisible until Phase 6.5.
- `src/components/playground/CoverageMatrix.tsx` — cells get a per-scope
  unit-cost overlay (below the coverage dot) when
  `perScopeUnitCost(candidate.pricing_breakdown, step.id)` returns a
  value. Null-safe.
- `src/hooks/usePipelineRun.ts` — `pricing_breakdown: null` defaults in
  every `PipelineCandidate` constructor, null-safe parsing on
  `candidates_verified`, and a new `candidate_verified` event handler
  (forward-compat for Phase 6.5's per-candidate verify event).

*Mock data backfill:*
- `runs/working_test_6/agent_2_output.json` — every candidate now has a
  realistic `pricing_breakdown` (OCR specialists with freemium /
  tiered / low-confidence shapes; all-in-ones with per-scope variable
  pricing). Lets the mock-mode pipeline exercise `<PricingBlock>` and
  the CoverageMatrix cost overlay without a real API key.

**What Phase 6.5 will add (Phase 5b):**
- `screening.py` 4B prompt addition: "also extract pricing alongside
  endpoints" — same fetch budget, same docs pages usually cover both.
- `pipeline_runner.py` emits `candidate_verified` SSE per candidate
  carrying the populated `pricing_breakdown` (the frontend already
  parses this event as of Phase 5a).
- 5 pricing extraction cases in `tests/test_agent4.py`.

**Cost delta:** none for Phase 5a — schema fields + pure helpers, no
LLM calls. Phase 5b's extraction piggybacks on the 4B web_fetch budget
(max_uses=6 — already sized in Phase 6.5 spec to accommodate a separate
/pricing page URL when needed).

**Tests:** 42 new cases:
- `tests/test_pricing.py` (new file) +32: `estimate_monthly_cost` across
  freemium / PAYG / flat-rate / capped-no-overage / mixed, `per_scope_unit_cost`
  precedence, `monthly_cost_for_scope_map`, `cheapest_meaningful_tier`,
  formatters (all fields / flat-rate / PAYG), schema round-trip.
- `tests/test_validators.py` +10: valid pricing passes, empty tiers /
  missing sources / invalid confidence / low confidence warn / invalid
  billing_granularity / per_scope drift / negative cost / negative
  overage / null pricing silent.

Combined: **305 PuzzleEval + 19 billing = 324 tests passing**. `tsc`
clean, Vite production build clean.

**Phase fingerprint:** `agent_2_output.json.candidates[].pricing_breakdown`
or `agent_4_output.json.validated_candidates[].pricing_breakdown` non-null
(lands live with Phase 5b / 6.5). UI fingerprint: `<PricingBlock>` mount
under any candidate card.

**Diagnostic flag:** none added at 5a — the whole data layer is
null-safe. Phase 5b's extraction will reuse
`PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` (Phase 6.5's flag) since pricing
extraction lives inside 4B.

### Phase 6 Refinement: User Candidate Picking + Pipeline Pause (2026-04-15)

After Agent 2 emits its ranked pool, the pipeline pauses and the user
picks which candidates to test at each scope. Replaces the old
`inject_registry_candidates()` testing shim (irreversibly retired) with
explicit user-driven selection via `inject_user_candidates()` +
`apply_scope_picks()`.

**Core mechanism — pipeline pause:**
Backend emits `selection_required` SSE event → frontend renders
`SelectionPanel` → user toggles per-scope keep/remove checkboxes +
optionally adds custom providers → POSTs to `/runs/{id}/select-candidates`
→ backend validates picks, signals `selection_ready` asyncio.Event →
pipeline resumes on filtered candidate list. Cancellation during pause
is supported via `cancel_event`. When `PUZZLEEVAL_USER_SELECTION_ENABLED=0`
the pause is skipped entirely (auto-run, backward-compat).

**Shim retirement (irreversible):**
`inject_registry_candidates()` removed from `research.py`, `cli.py`
(both call sites), and `pipeline_runner.py`. Users who want specific
providers add them via SelectionPanel or CLI interactive prompt.

**New helpers (`research.py`):**
- `inject_user_candidates(agent2_result, user_adds)` — deduped
  case-insensitive merge with `source="user_provided"`,
  `relevance_score=0.99`.
- `apply_scope_picks(agent2_result, scope_picks, user_added)` — filters
  to only picked candidates, reduces each candidate's `covers_step_ids`
  to the scopes it was picked at. `scope_picks=None` is pass-through
  (no filtering — used by `--no-interactive`).

**CLI (`cli.py`):**
- New `_cli_user_selection_pause()` — prints per-scope candidate tables
  to stderr, prompts for each scope (`y/n/indices`), applies picks via
  `apply_scope_picks()`.
- `--no-interactive` skips the pause (auto-run with all Agent 2
  candidates). Controlled by `USER_SELECTION_ENABLED` config flag.

**API:**
- `RunState` gains: `selection_ready: asyncio.Event`,
  `cancel_event: asyncio.Event`, `user_scope_picks`,
  `user_added_candidates`, `selection_required_emitted_at`,
  `user_selection_applied`, and `"awaiting_candidate_selection"` status.
- `POST /api/runs/{id}/select-candidates` — validates scope_ids match
  blueprint, candidate names exist in Agent 2 results or user-added list,
  non-empty picks, no double-submit. Sets `selection_ready.set()`.
- `pipeline_runner.py` — pause inserted between Agent 2 completion and
  Agent 4 start. Uses `asyncio.wait` on `selection_ready` + `cancel_event`.
  On resume, applies picks via `apply_scope_picks` and re-emits
  `candidates_found` with filtered list.

**Frontend:**
- `Stage = "conversation" | "pipeline" | "selection" | "results"` —
  new `"selection"` stage.
- `src/services/api.ts` — new `selectCandidates(runId, request)`;
  `selection_required` + `candidate_verified` + `candidate_rejected`
  added to SSE event types.
- `src/hooks/usePipelineRun.ts` — handles `selection_required` →
  sets stage to "selection"; exposes `perScopeCandidates`,
  `submitSelection()`, `isSelectionSubmitting`, `rejections`.
  Forward-compat `candidate_rejected` handler populates `rejections[]`
  for Phase 6.5.
- `src/components/playground/SelectionPanel.tsx` — NEW. Per-scope
  columns with keep/remove checkboxes, "Add custom provider" form with
  multi-scope checkbox selector, Submit button. Opt-out UX (all start
  selected — user unchecks what they don't want).
- `src/components/playground/RejectionSummary.tsx` — NEW null-safe
  scaffold. Renders per-scope rejection list when Phase 6.5's
  `candidate_rejected` events arrive. Collapsed by default; expandable
  with reason labels + attempt notes. Invisible until 6.5 ships.
- `src/pages/Playground.tsx` — mounts `SelectionPanel` when
  `stage === "selection"`, `RejectionSummary` at top of results.
  Stage indicator bar adds "Selection" between "Research" and "Screening".

**Tests:** 20 new cases:
- `tests/test_research_phase6.py` NEW +9: inject_user_candidates
  (merge/dedup/source/covers) + apply_scope_picks
  (pass-through/filter/reduce/empty/user-added).
- `puzzleeval-api/tests/test_routes_select.py` NEW +11: 404 on
  missing run, 400 on wrong status, double-submit rejected, empty
  picks rejected, unknown scope_id, unknown candidate name, user-added
  with unknown scope, valid picks 200 + ready, same candidate at
  multiple scopes, user-added flows through, empty covers_step_ids
  rejected.

Combined: **314 PuzzleEval + 30 billing/API = 344 tests passing**.
`tsc` clean, Vite build clean.

**Phase fingerprint:** `metadata.user_selection_applied=true`,
`metadata.user_added_candidates_count >= 0`,
`metadata.selection_required_emitted_at` (ISO timestamp).

**Diagnostic flag:** `PUZZLEEVAL_USER_SELECTION_ENABLED=0` — skips
the pause entirely, auto-runs with all Agent 2 candidates (backward-
compat). Default is `1` (pause enabled).

### Phase 6.5 + 7 Refinement: Deep Verify Loop + Per-Scope Selection (2026-04-15)

**Phase 6.5 — Agent 4 directed deep-verify loop (4A→4B→4C→4D).**
Agent 4 is redesigned from a single-shot classifier to a production-grade
directed multi-phase loop. For each selected candidate, the loop runs:
4A (Discovery) → 4B (Spec Extraction + api_spec.txt + ROUTING_TABLE +
pricing) → 4C (Scope Coverage Verification: upgrade claimed→verified,
remove unverifiable scopes) → 4D (Decision with anti-false-positive
AND anti-false-negative guardrails). Drop-on-reject: if a candidate
fails, it's dropped from the scope's tested set — no substitution.
Per-run tool cache: a candidate covering M scopes gets deep-verified
ONCE; subsequent scopes reuse the cached spec.

New files:
- `puzzleeval/deep_verify_prompt.py` — `DEEP_VERIFY_SYSTEM_PROMPT`
  (9.8K chars) teaching the 4-phase workflow + `build_deep_verify_message()`
  per-candidate message builder. Mentions tool budget explicitly
  (6 web_fetch + 5 web_search). Includes OpenAPI/Swagger hunting,
  multi-page doc traversal, third-party doc host checks (Postman,
  ReadMe, SwaggerHub), archive.org fallback, and pricing extraction.

Schema additions (`schemas.py`):
- `FailedToVerify` model: `name`, `provider`, `scope_id`, `reason`
  (docs_unreachable / enterprise_only / deprecated / no_api /
  coverage_removed_at_scope / verify_error), `attempt_notes`.
- `ScreenedCandidate` gains: `api_spec_path: str | None`,
  `covers_step_ids: frozenset[str]`, `coverage_confidence: dict[str, str]`.
- `Agent4Result` gains: `failed_to_verify: list[FailedToVerify]`,
  `scope_selections: dict[str, list[str]]`.

Config (`config.py`):
- `AGENT4_DEEP_VERIFY_ENABLED` (default on)
- `AGENT4_DEEP_VERIFY_MAX_TURNS` (15)
- `AGENT4_DEEP_VERIFY_MAX_PARALLEL` (5)

Pipeline (`pipeline_runner.py`):
- SSE events emitted after Agent 4: `candidate_verified` (per candidate ×
  per verified scope), `candidate_rejected` (per FailedToVerify entry),
  `scope_verified_complete` (per scope with verified + rejected counts).
- Frontend already handles all three events (wired in Phase 6).

**Phase 7 — Per-scope top-K selection.**
Replaces the global `sort by (credentials, relevance)` with independent
per-scope rankings via a 5-dimension weighted scorer.

New file: `puzzleeval/selection.py`
- `select_scope_candidate_pairs()` — for each scope, ranks eligible
  candidates by weighted score and returns top K names.
- `_score_at_scope()` — 5 dimensions: user_picked_here (0.40),
  credentials (0.20), relevance_at_scope (0.20), docs_quality (0.10),
  pricing_fit (0.10). Coverage count is NOT a dimension.
- `_pricing_fit()` — heuristic using Phase 5 PricingBreakdown when
  available, falls back to Agent 2's loose pricing_model string.

Config (`config.py`):
- `SCOPE_SELECTION_WEIGHTS` dict (tunable)
- `SCOPE_CANDIDATES_CAP_BY_PLAN` dict (free=3, paid=5, enterprise=10)

Billing (`billing.py`):
- `PLAN_SCOPE_CANDIDATES_CAP` dict
- `scope_candidates_cap(plan)` function

Pipeline integration (`pipeline_runner.py`):
- Phase 7 runs BETWEEN Agent 2 and Phase 6 pause. Computes programmatic
  default picks that the SelectionPanel shows pre-filled.
- When Phase 6 is disabled (`USER_SELECTION_ENABLED=0`), Phase 7's
  top-K is applied directly via `apply_scope_picks()` — the auto-run
  path for scripts. The `selection_required` SSE event includes
  `default_picks` so the frontend can pre-fill checkboxes.

Tests: 15 new cases in `tests/test_phase6_5_and_7.py`:
- Phase 7 (8): specialist scope exclusion, all-in-one multi-scope,
  multi-scope picks, user override, tier cap, credentials boost,
  coverage count not a dimension, empty coverage excluded.
- Phase 6.5 (7): FailedToVerify round-trip, api_spec_path default,
  coverage fields, Agent4Result with failed_to_verify + scope_selections,
  prompt phase markers, ROUTING_TABLE in prompt, message builder.

Combined: **329 PuzzleEval + 30 API = 359 tests passing**.

Phase fingerprints:
- 6.5: `agent_4_output.json` contains `failed_to_verify[]` +
  `scope_selections{}`; SSE events `candidate_verified` /
  `candidate_rejected` / `scope_verified_complete` fire per-scope.
- 7: `selection_required` SSE payload includes `default_picks`;
  `pipeline_summary.json` metadata: `phase7_scope_picks` present.

Diagnostic flags:
- `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED=0` reverts to shallow Agent 4
  (legacy behavior + Agent 5 resumes Phase 1 research).
- `SCOPE_SELECTION_WEIGHTS` / `SCOPE_CANDIDATES_CAP_BY_PLAN` are config
  dicts in `config.py` — tunable without code changes.

### Phase 8 Refinement: API Doc Understanding (2026-04-15)

Surgical improvement to Phase 6.5's 4B extraction turn-phase. Lifts
api_spec.txt completeness for every verified candidate, which in turn
lifts Agent 5's build success rate in Phase 9.

**Prompt changes (`deep_verify_prompt.py::DEEP_VERIFY_SYSTEM_PROMPT`):**
- **OpenAPI/Swagger hunting section** — new "Step 1: Hunt for OpenAPI"
  block before the narrative-docs extraction. Instructs the model to:
  1. Site-scoped search: `site:{domain} openapi.json OR swagger.json OR openapi.yaml`
  2. Try common convention URLs: `/openapi.json`, `/api/docs/openapi.json`,
     `/v1/openapi.json`, `/swagger.json`, `/.well-known/openapi.yaml`, `/api-docs`
  3. When spec found → parse endpoints directly, skip narrative doc fetches,
     save fetch budget for pricing page + Python examples
- **Endpoint completeness enforcement** — "ENDPOINTS section must list
  ALL endpoints, not just the quickstart example. If you see 5+ endpoints
  in a sidebar but only extracted 1-2, MUST fetch the reference page."
- **Step 2 (narrative fallback)** — only executes when no OpenAPI spec
  resolves. Multi-page traversal rule preserved from Phase 6.5.

**New constants:**
- `COMMON_OPENAPI_PATHS` — tuple of 8 common convention paths, ordered
  by empirical frequency. Used by the prompt AND by `build_deep_verify_message()`
  to generate per-candidate URL hints.
- `THIRD_PARTY_DOC_HOSTS` — tuple of 4 known secondary doc platforms
  (Postman, ReadMe, Stoplight, SwaggerHub). Referenced by the 4D
  anti-false-negative guardrails.

**`build_deep_verify_message()` enhancement:**
- When `claimed_docs_url` is provided, the message now includes an
  "OpenAPI Spec URL Hints (Phase 8)" section with 6 concrete URLs
  derived from the base domain. When no docs URL is known, hints are
  omitted (model does its own discovery).

**Tests:** 9 new cases in `test_phase6_5_and_7.py::TestPhase8DocUnderstanding`:
prompt has OpenAPI hunt section, site-scoped search, common URL probing,
skip-narrative instruction, endpoint completeness enforcement;
`COMMON_OPENAPI_PATHS` has ≥6 entries; `THIRD_PARTY_DOC_HOSTS` has ≥3;
message includes hints when URL provided; hints absent when no URL.

Combined: **338 PuzzleEval + 30 API = 368 tests passing**.

**Phase fingerprint:** verified candidates' `api_spec_path` files contain
`OPENAPI_URL: https://...` (non-"not_found") for most candidates. The
`COMMON_OPENAPI_PATHS` constant can be grepped from the prompt output.

**Diagnostic flag:** prompt-only — no flag. Revert by removing the
Phase 8 Step 1 section from `DEEP_VERIFY_SYSTEM_PROMPT`; the legacy
narrative-extraction path in Step 2 still works.

**Rollback:** pure additive prompt changes. Phase 6.5's directed loop
continues working at prior extraction quality if the Phase 8 sections
are removed.

### Phase 9 Refinement: Per-Scope Test Execution (2026-04-15)

For multi-scope workflows, Agent 5 tests top-K candidates **at each scope
independently**. No end-to-end workflow chaining. No combinatorial
explosion. Per-scope data is the deliverable — "Mindee scores 0.95 at
OCR, Klippa 0.87" is directly actionable.

**Schema (`schemas.py`):**
- `ScopeTestRun` — `scope_id`, `scope_role`, `candidate_results:
  list[CandidateTestRun]` (sorted by aggregate score descending),
  `test_case_count`. One entry per scope that had candidates tested.
- `Agent5Result.scope_runs: list[ScopeTestRun]` — empty for legacy
  (pre-Phase-9) outputs; 1-scope workflows produce 1 ScopeTestRun
  wrapping the same data as `candidate_runs`.

**New module (`puzzleeval/scope_routing.py`):**
- `group_tests_by_scope(test_cases, blueprint)` — routes test cases to
  scopes via `sub_task_ref` ↔ `WorkflowStep.capability` keyword overlap.
  No-blueprint → all tests grouped under "_flat".
- `build_scope_runs(candidate_runs, blueprint)` — post-processes Agent
  5's flat `candidate_runs` into per-scope `ScopeTestRun` entries.
  For each candidate, filters `test_results` to those matching each
  scope, recomputes pass_rate/tests_passed/etc per scope.
- `dedup_tools_for_build(scope_selections)` — unique tool names across
  all scopes (a candidate covering M scopes gets ONE build). Preserves
  first-seen order.

**Config:** `SCOPE_TEST_MODE` flag (env `PUZZLEEVAL_SCOPE_TEST_MODE`,
default on). When off, reverts to legacy flat-candidate testing.

**Frontend:**
- `ScopeTestRun` + `ScopeCandidateResult` types in `pipeline.ts`.
- `ResultsComparison.tsx` rewritten with two paths:
  - Phase 9 (scopeRuns available): per-scope sections with candidate
    tables, pass-rate bars, score/latency/cost columns.
  - Legacy (no scopeRuns): original flat card layout preserved.

**Tests:** 15 new in `test_phase9.py`:
- Schema round-trip, backward-compat default, Agent5Result with scope_runs
- group_tests_by_scope: no-blueprint flat, single-scope, multi-scope routing
- build_scope_runs: single-scope wrap, no-blueprint flat, multi-scope split, empty
- dedup_tools_for_build: across scopes, order preserved, empty, case-insensitive
- Config SCOPE_TEST_MODE enabled by default

Combined: **353 PuzzleEval + 30 API = 383 tests passing**.

**Phase fingerprint:** `agent_5_output.json.scope_runs` length equals
number of scopes; each `ScopeTestRun.candidate_results` non-empty for
scopes with tested candidates.

**Diagnostic flag:** `PUZZLEEVAL_SCOPE_TEST_MODE=0` reverts to legacy
flat-candidate testing.

### Phase 10 Refinement: Generalizability Benchmark (2026-04-15)

10-domain benchmark suite that validates the pipeline works across
multiple AI domains — not just the OCR happy path. Regression gate
for everything in Phases 1-9.

**Domain coverage (10 configs):**

| Domain | Scopes | What it exercises |
|--------|--------|-------------------|
| invoice_workflow | 3 | OCR+extract+sync; SPA provider (Phase 1.5) |
| chatbot_simple | 1 | Single-scope regression baseline |
| classification_pipeline | 2 | Classify+route pattern |
| translation_chain | 2 | Translate+format pattern |
| summarization | 1 | Single-scope summarization |
| multi_step_data_extraction | 4 | Longest DAG (4-step chain) |
| rejection_transparency | 3 | Drop-on-reject with niche providers |
| user_added_candidate | 2 | Phase 6 user-added flow |
| long_chain_parallel | 5 | Fan-out/fan-in DAG topology |
| scope_under_capacity | 2 | Scope with 0 tested candidates |

**Files:**
- `tests/generalizability/domains/*.json` — 10 domain config files with
  `input`, `expected_scopes`, `per_scope_assertions` (min pass rate +
  min candidates per scope), `max_cost_usd` ceiling.
- `tests/generalizability/conftest.py` — fixtures + helpers.
- `tests/generalizability/test_generalizability.py` — 39 tests:
  config loading (10), assertion validation (10), scope-assertion
  alignment (10), plus meta-tests (min domain count, single/multi/fan-out
  domain presence, cost ceiling bounds, assertion helpers).
- `bench/run_benchmark.py` — CLI runner with `--list`, `--domain`,
  `--all`, `--live`, `--report` modes.
- `bench/README.md` — usage docs.
- `pyproject.toml` — `addopts = "-m 'not generalizability'"` so
  standard `pytest` skips bench tests; `pytest -m generalizability`
  runs them explicitly.

**How to run:**
```bash
# Standard tests (bench skipped)
ANTHROPIC_API_KEY=dummy pytest tests/

# Generalizability tests only (config validation — no API calls)
ANTHROPIC_API_KEY=dummy pytest -m generalizability tests/generalizability/ -v

# Bench CLI — dry run all domains
python bench/run_benchmark.py --all

# Bench CLI — live run (requires API key, incurs costs)
python bench/run_benchmark.py --domain invoice_workflow --live
```

**Tests:** 39 generalizability tests (skipped by default) + all existing
tests unchanged. Standard suite: **353 passing, 39 deselected**.
With generalizability: **392 total passing**.

**Phase fingerprint:** `bench/results/{domain}_{timestamp}.json` files.
Presence of `tests/generalizability/domains/*.json` with 10+ configs.

**Diagnostic flag:** marker-gated, separate directory — doesn't touch
the core suite. Remove the `addopts` line from `pyproject.toml` to
include bench tests in the default run.

## Architecture Decisions Log (2026-04-18)

Durable decisions that future sessions should treat as load-bearing. If
you find yourself about to contradict one of these, pause and check
whether the conditions that motivated the decision have changed.

### AD-001: Stay unified, no per-modality pipeline fork

**Decision**: One agent pipeline handles every modality (voice, OCR,
vision, code gen, webhook, streaming, chatbot-text). Do NOT fork
into "voice pipeline / vision pipeline / file pipeline" each with its
own Agent 1-5.

**Why**: Agents 1-4 are 100% modality-agnostic (they operate on enum
fields and WorkflowBlueprint shape). Forking would 4× the prompt
maintenance with zero gain for those agents. Real users describe
mixed-modality problems ("OCR invoices THEN call the vendor to
verify") that a forked architecture would require the user to split
themselves — breaking the product's core value. Cross-modality
comparison (which is a product requirement) becomes impossible with
forked pipelines.

**When to revisit**: only if a modality emerges with fundamentally
different `Agent 1 → 2` semantics, different scoring philosophy that
plugins can't carry, or a user contract that differs at the surface
above agents (e.g., HIPAA-audited healthcare). Voice / vision / file
/ webhook / code-gen / streaming / outbound-email are all "plugin
shaped" and don't qualify.

### AD-002: Agent 5 uses conditional in-prompt gating (path C)

**Decision**: Agent 5's builder prompt stays in ONE file
(`implement_test_env.py::BUILDER_SYSTEM_PROMPT`) with
conditional injection for modality-specific sections. Do NOT build a
playbook-file-loading system yet. Do NOT migrate to native Anthropic
Agent Skills yet.

**Why**: Evaluated three options carefully:
- (A) Native Skills: incompatible with local-subprocess credential
  model. Requires `container` + `code-execution-2025-08-25` + two
  other betas. User API keys would have to flow through Anthropic's
  sandbox.
- (B) Playbook files + deterministic router: real option, but adds
  loading/routing infrastructure for a problem we haven't hit yet
  (prompt still manageable at ~2500 lines, well-organized, <5
  modalities). Forward-compat claim to native Skills is weaker
  than expected (skill format not formally spec'd; Anthropic uses
  uploaded-by-ID model, not file-path model).
- (C) Conditional in-prompt gating: extends the existing
  `MULTI-CALL HARNESS CONTRACT` pattern already in
  `_build_initial_message`. Minimal new infrastructure. Token
  savings same as playbooks (irrelevant sections skipped). Simpler
  to iterate.

**Revisit trigger for flipping to (B)**: modality count ≥ 8 AND
prompt becomes a merge-conflict magnet, OR independent engineer
ownership per modality becomes real.

**Revisit trigger for flipping to (A)**: PuzzleEval pivots to SaaS
(credentials flow through our infra anyway), OR Anthropic ships a
"skills as system-prompt injection" mode without `container`.

See NEW-AH above for the full evaluation.

### AD-003: Plugins own modality-specific orchestration

**Decision**: Any modality that needs N harness invocations per test
(multi-turn voice, multi-turn chatbot, streaming with re-subscribe)
is owned by a PLUGIN with `requires_harness_runner=True`. The plugin
drives its own N-turn loop; the harness stays single-turn.

**Why**: Keeps Agent 5's builder prompt + harness template single-shape
across modalities. Adding a new multi-call modality is one plugin file
(~1200 lines, self-contained) — NOT a prompt overhaul. Voice is the
reference implementation (`tool_plugins/voice_realtime.py`).

### AD-004: Audio artifacts live under the run directory, NOT %TEMP%

**Decision**: Every audio file (caller MP3, agent MP3, merged
conversation) lives under `runs/<trace_id>/harnesses/<candidate_slug>/
voice/`. Thread-local `_session_dir` keeps parallel candidates
isolated.

**Why**: Without this, the backend audio-streaming route would 403
(containment check) and the frontend would silently fail. User
expectation is "every run is a self-contained directory I can zip and
share."

**Cloud-scale seam preserved**: `set_session_dir()` accepts any
Path-compatible object. Swap in an S3/GCS Path-shim without touching
call sites.

### AD-005: Skills and MCP are NOT interchangeable

**Decision** (from reading Anthropic's two MCP docs + two Skills
docs + engineering blog): Skills are for PROCEDURAL KNOWLEDGE
(markdown guidance with optional bundled scripts), MCP is for
TOOL/SERVICE INTERFACES (function-call RPCs). They're orthogonal.
When adding a new capability to PuzzleEval, route as follows:

| Need | Answer |
|---|---|
| Teach the agent a contract (how to call X API family) | In-prompt conditional section (AD-002) |
| Expose a custom callable (e.g., `sample_audio_check`) | Custom tool registered at `client.messages.create(tools=[...])` |
| Expose a set of external service tools | MCP connector (remote or stdio) |
| Bundle domain procedural knowledge for Anthropic-hosted agents | Agent Skills (only when AD-002 revisit triggers hit) |

### AD-006: Bytes across the JSON border

**Decision**: Any harness that returns `bytes` in `raw_response.*`
(audio_bytes, binary blobs, whatever) is transparently round-tripped
via `{"_b64": "<base64>"}` sentinels. The encode happens in the
subprocess driver (`_execute_single_test`'s exec_script); the decode
happens in Agent 5 (`_inflate_b64_sentinels`) before the plugin sees
the dict. Harnesses never need to know.

**Why**: The prior `json.dump(..., default=str)` path silently
stringified bytes as Python repr — bug surface that cost us a
real-run cycle before being caught. The sentinel is the stable seam
for every future modality that ships binary data.

### AD-007: Safety-critical contracts live in deterministic code, not prompts

**Decision**: When Claude's adherence to a prompt rule determines
whether a runtime contract holds (vs. being a soft quality hint),
push the enforcement into OUR code where adherence is guaranteed.
Prompts are for teaching patterns; deterministic runtime gates are
for enforcing contracts.

**Why**: Four sessions chased the same symptom (zero agent audio)
with different root causes. Session 4 (NEW-AI) landed when we
stopped trusting prompt-level rules and pushed the default
`input_context` into the runner closure. Both prompt-level rules
(Agent 3 must emit `input_context.instructions`; Agent 5's harness
must have a `DEFAULT_INSTRUCTIONS` fallback) were violated in the
same real run. Moving the fallback to the runner — code we own,
not prompts we hope Claude follows — closed the gap in one session.

**Operational consequence**: when you add a contract that the
product depends on at runtime, ask "can Claude violating the
prompt-level description of this contract produce a silent
failure?" If yes, add a deterministic enforcement point
(verification gate, runner closure, validator). Keep the prompt
rule too — both layers. Defense in depth.

**Examples already applied**:
- Runner-level default `input_context` injection (NEW-AI)
- Pipeline-runner save-on-score-change for explicit-candidate
  boost (NEW-AI, capability fix 6)
- Container-threading revert guards that fail a test if anyone
  silently re-adds the 20260209 patterns (NEW-AI, capability fix 1)

**Examples planned (Phase 4 design)**:
- `_run_verification_checks` will require `phase_4_passed.txt`
  written by a real-test probe before accepting `HARNESS_COMPLETE`

### AD-008: Web-tool version selection prioritizes behavioral stability over features

**Decision**: Stay on `web_fetch_20250910` + `web_search_20250305`
(basic versions) rather than the 20260209 dynamic-filtering pair.
Revisit only if Anthropic:
1. Fixes the non-beta `messages.create` hang on `container` kwarg, OR
2. Documents a clean "use without container threading" mode for
   read-only tool users, OR
3. Ships a workload where the 24% input-token savings from dynamic
   filtering meaningfully outweighs 3-5 min sandbox spin-up latency

**Why**: Real-run evidence (traces d3b49875 + 31f1af7c + 4068e872
accumulated across NEW-AI) showed the 20260209 pair introduces new
failure classes that the basic versions don't have:
- 400 "container_id is required" cascades across every sub-agent
  using the tools
- Agent 2's non-beta endpoint silently hangs on `container` kwarg
- Dynamic filtering's sandbox spin-up adds 3-5 min per real
  research call
- The theoretical 24% token savings doesn't materialize on our
  discovery workload because Agent 2 only runs 2-3 searches per
  request — not enough content to justify filtering overhead

**Revisit procedure** (when we try again): read the regression
tests in `TestNoContainerThreadingAfterRevert` first — they
enumerate every code site that needs container-ID threading if we
re-upgrade. Then implement the full checklist in one pass:
- Thread `container_id` through Agent 5 builder, Agent 4 verify
  continuations, ask_research sub-agent
- Move Agent 2 to `client.beta.messages.create` (the non-beta
  path is the hanging one)
- Audit EVERY new sub-agent added after this for container
  propagation

### AD-009: The modality-contract injection pattern is a skills-loader predecessor

**Decision**: Modality-specific build rules (voice harness
contract, future vision/code-gen/translation contracts) live in
string constants injected via `__MODALITY_CONTRACT__` placeholder
— the same conditional-injection pattern as
`__OS_SPECIFIC_RULES__`. When modality count crosses ~8 (AD-002
revisit trigger), convert to `skills/voice.md` files and
`_modality_specific_contract` becomes a file-reader router.

**Why**: This is the minimum-work predecessor of the skills-style
per-modality organization we want eventually. The structural
shape already matches what a skills loader would look like:
- Placeholder in the template (equivalent to skill metadata)
- Function that picks the right content based on input
  characteristics (equivalent to skill routing)
- Empty-string fallback for non-matching cases (equivalent to
  "no skill applies — use general rules")

The migration is purely where the strings come from: constants
today, disk files tomorrow. Call sites don't change.

**Current reality** (2026-04-21): only voice has a dedicated
contract (`_VOICE_HARNESS_CONTRACT`). Other modalities use the
general builder rules. Adding a new modality contract is a 2-line
change: new constant + branch in `_modality_specific_contract`.

## Open Threads & Known Limitations (2026-04-21)

Honest ledger of what's not fixed, what might need fixing, and what's
deferred with reasoning. Future sessions: read this first to avoid
re-deriving deliberate choices.

### OT-001: Scoring uses substring match, not semantic

**Symptom**: Voice tests' `expected_agent_contains` fields
(`"9"`, `"555-0911"`, `"95"`, `"Los Angeles"`, `"welcome"`) can miss
valid natural-language responses. Agent says "We are open from nine
to five" → substring `"9"` fails to match "nine" (spelled out).
Replay of voice_dual_7 through the fixed pipeline produced
score=1.0 because the agent happened to answer with digits, but
that's luck-of-the-model not robust scoring.

**Impact**: Voice eval reports understate agent quality by ~20-50%
depending on how the agent phrases responses.

**General fix (deferred)**: add a semantic-match mode to
`drive_conversation` — per turn, if substring match fails, escalate
to an LLM judge call with the transcript and the expected intent.
Cost: ~$0.002 per escalation.

**Why deferred**: lower priority than capability-level bugs. The
substring mode is deterministic + free, which is right for
development. Production reports should add the semantic fallback.

### OT-002: No fresh dual-provider real run post-all-fixes

**Symptom**: All the NEW-AH fixes (bytes-safe round-trip, ID3 strip,
thread-local session_dir, audio_format → content_type) were verified
via (a) unit tests + (b) replay of voice_dual_7's existing OpenAI
harness. No fresh Agent 5 build was done against the dual-provider
scenario with ALL fixes simultaneously active.

**Impact**: High confidence the fixes compose correctly (unit tests
exercise the real code paths), but we don't have a single artifact
showing both candidates building AND running AND producing merged
conversations in their own per-candidate folders.

**To do in next session**: `python -m puzzleeval.cli --agent5-input
runs/voice_scenario_dual/agent_5_input.json --trace-id voice_dual_8`
— cost ~$4-5. Expected outcome: two folders
(`openai_voice_stack/voice/`, `elevenlabs_voice_stack/voice/`) each
with 5 caller + 5 agent + 1 merged file. If any file is missing,
the race condition wasn't fully fixed. Real acceptance test.

### OT-003: Agent 5 builder prompt approaching ~2500 lines

**Symptom**: `BUILDER_SYSTEM_PROMPT` at ~2500 lines. Voice added
~60 lines (conditional MULTI-CALL CONTRACT). Each future modality
will add a similar section.

**Trigger for action**: if prompt crosses ~3500 lines OR becomes a
merge-conflict hotspot, execute AD-002's revisit path (flip to
playbook-files + router). Track via `wc -l` in a weekly health
check.

**Not a bug — a monitoring item.**

### OT-004: Audio merge is MP3-only (robust), WAV/OGG/M4A work but untested at depth

**Symptom**: `_merge_conversation_audio` handles `.mp3` via ID3
tag stripping. For other extensions (wav, ogg, m4a, webm, mp4, mpga,
flac), it byte-concats without any format-aware preprocessing.

**Impact**: Works in theory because most formats don't have
problematic mid-stream metadata (WAV has a header at offset 0 but
subsequent frames are raw PCM; OGG has similar page structure). Not
exhaustively tested.

**Fix if needed**: per-format strippers (WAV RIFF header, OGG
container). Cheap to add when a real-run trace exposes a bad
concat.

### OT-005: Agent 3 empty-response retry is ONE attempt

**Symptom**: `_retry_empty_generation` fires once when Agent 3
returns `{"test_cases": []}`. If the retry also returns empty, the
pipeline proceeds with zero tests — validator downstream errors
out.

**Impact**: Rare (only observed once in traces across ~10 full runs).
When it happens, the user sees "Agent 3 produced zero test cases"
and has to manually re-run.

**Deferred**: N-attempt loop with exponential backoff + model
fallback (Sonnet → Haiku → request user to clarify input). Low
probability × low impact × simple current fix.

### OT-006: Adversarial battery auto-skips on "sandbox_broken"

**Symptom**: When a harness's venv is missing packages (pip install
silently failed during build), the adversarial battery can't import
the harness → returns `sandbox_broken` warning → battery skipped →
test execution proceeds. This was the right choice (don't block
valid harnesses on sandbox setup bugs), BUT it means we lose the
"is the harness robust to adversarial inputs?" check for that
candidate.

**Impact**: On runs where pip install fails silently, candidate
scoring trusts test execution without adversarial pre-flight. In
practice test execution catches most issues anyway.

**Fix direction**: tighten the `_create_venv` path to verify
requirements.txt installed cleanly before running the builder.
Orthogonal to current capability; not urgent.

### OT-007: Two CI runs root allowlist (api vs local)

**Symptom**: Backend `/runs/audio` serves from both
`puzzleeval-api/runs/` AND `PuzzleEval-local/runs/` + operator-
provided `PUZZLEEVAL_EXTRA_RUNS_ROOTS`. This is correct but means
two parallel run directories exist. CI / tests touch one; real
backend traffic touches the other.

**To do**: eventually converge on one runs root (probably
`puzzleeval-api/runs/` as the backend serves it). CLI mode can
symlink or env-var into that dir. Not urgent — current separation
is deliberate during dev.

### OT-008: Voice plugin is a module-level singleton

**Symptom**: `tool_plugins/voice_realtime.py` registers one
instance at import time. Thread-local state is the right fix for
per-candidate isolation (AD-004), but if we ever want per-CANDIDATE
plugin configuration (different TTS voice per candidate, different
audio format default), the singleton design becomes awkward.

**Fix direction**: if needed, accept a per-call config param in
`synthesize_input` / `evaluate_output` that overrides defaults.
Current state: no requirement. Logged for awareness.

### OT-009: Skill format is reverse-engineered from Anthropic's examples

**Symptom**: If AD-002's revisit triggers fire (flip to
playbook-files + router), we'll format the playbooks using
Anthropic's Skills file conventions for soft forward-compat. But
Anthropic hasn't published a formal schema — conventions are
example-based in the cookbook / best-practices docs.

**Impact**: If Anthropic formalizes skills-file schema later and our
reverse-engineered shape diverges, we'd need a migration pass. Low
risk but real.

**Watch**: monitor `platform.claude.com/docs/en/agents-and-tools/
agent-skills/*` for a schema announcement.

### OT-010: LLM-judge fallback on tool_runner non-verdict

**Symptom**: The tool_runner verdict-promotion fix (NEW-AG) catches
the case where Claude's tool_runner doesn't emit a final
`ScoreVerdict`. But if a plugin's own verdict is inconclusive
(e.g., voice plugin returned score=0.4 with fallback_reason="no
runner"), we still fall through to LLM judge — and that judge only
sees the first-turn response, not the full conversation.

**General fix (deferred)**: pass `verdict.detail` (containing
per-turn transcripts) into the LLM-judge prompt so it can reason over
the complete conversation even when tool_runner / direct-invoke
didn't emit a structured verdict.

**Why deferred**: rare path. With direct-invoke owner fast path
(NEW-AG), plugins emit conclusive verdicts for ~100% of voice tests
that reach them. The LLM-judge fallback is a safety net, not the
main path.

### OT-011: No per-candidate SaaS isolation story yet

**Symptom**: Current architecture is self-hosted per-user. If
PuzzleEval pivots to multi-tenant SaaS, per-user credential
isolation becomes a cross-cutting concern (AD-001's "different user
contract" criterion). This would ALSO unblock native Skills (AD-002
revisit trigger).

**Not an open bug — a strategic watch item.** If SaaS becomes a
roadmap item, expect to:
1. Move harness execution to a sandboxed container per user (Docker
   / Firecracker / Anthropic's container).
2. Re-evaluate AD-002 (Skills native vs playbook files).
3. Re-evaluate AD-007 (currently implicit: single-user credential
   registry).

### OT-012: Environment-level WebFetch blocking rejects candidates wholesale

**Symptom (observed 2026-04-19 end-to-end test, run `5b4391e8`)**:
An end-to-end voice-scenario run through the FastAPI backend +
React frontend reached the selection stage with 7 voice candidates
(Rosie AI, NextPhone, Marlie.ai, Upfirst, Dialzara, Goodcall, Retell
AI). After picking 3 defaults + sending to Agent 4, ALL 7 rejected
with `rejection_category="no_public_docs"` / `"no_api_access"` and
`web_fetch_blocks=10`. Agent 5 then hit
`ValueError: max_workers must be greater than 0` because
`ThreadPoolExecutor(max_workers=min(AGENT5_MAX_PARALLEL, 0))` is
illegal.

**Two fixes landed this session:**

1. **Agent 5 empty-candidates guard** — when every upstream
   candidate is rejected, Agent 5 now short-circuits to a clean
   empty `Agent5Result` with `build_summary` explaining the
   coverage gap, instead of crashing. Matches the documented
   graceful-degradation contract. Report assembler renders it
   as a zero-candidate run with advisories, not a pipeline
   failure. See `implement_test_env.py` ~line 4677.

2. **`pipeline_runner.py` UnboundLocalError fix** — Phase 6.5's
   coverage-gap detection had `user_understanding = _get_user_
   understanding(state)` shadowing the enclosing-scope binding.
   Python marked `user_understanding` local for the whole
   `_branch_a_research_and_screening` function, which turned the
   earlier `await _run_real_agent2(state, user_understanding)` at
   line 331 into an UnboundLocalError. Removed the redundant
   re-fetch — the enclosing scope's binding is correct.

**Root cause of the rejections (NOT a bug — environmental)**:
The machine running the backend has Anthropic's server-side
`web_fetch` tool blocked by CF/WAF / corporate filtering on ~every
API docs domain. `web_fetch_fallback.py` correctly detected the
blocks and the agents gracefully returned rejections (no false
passes), but the rejection rate was 100% which is operationally
the same as "no products work."

**Mitigation (set in `.env` this session)**:
`PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED=0` reverts Agent 4 to shallow
pass/fail per the documented diagnostic flag matrix. Did NOT
resolve the rejection rate in the observed run because the shallow
path still makes web_fetch calls for evidence-gathering; same
upstream block hit.

**Real fix paths (cloud-deferred)**:
- Move backend to a cloud VM where WebFetch isn't blocked.
- Add a `PUZZLEEVAL_AGENT4_TRUST_CANDIDATES=1` mode that skips
  doc-fetch verification entirely and trusts Agent 2's discovery —
  only safe when Agent 5 does its own Phase-1 research.
- Add a `PUZZLEEVAL_REGISTRY_ONLY_CANDIDATES=1` mode that filters
  Agent 2's pool to only candidates in `provider_registry.json`
  (where we already have keys + known doc URLs).

**Impact**: Full E2E pipeline flow is verified (frontend ↔ backend
↔ SSE ↔ all 5 agents ↔ report). Voice harness + audio merge code
paths could NOT be exercised in this run because zero candidates
reached Agent 5. Voice-specific verification remains OT-002
(replay or cloud run).

**Observability**: `pipeline_summary.json` now shows
`agent_4_output.web_fetch_blocks >= 1` as a leading indicator.
When blocks > candidate_count × 1.5, the environment is likely
blocking WebFetch wholesale.

### OT-013: HARNESS_COMPLETE is misleading — builder tests a different code path than production

**Symptom (observed 2026-04-21 across traces f1312253 + 28cb2648)**:
Agent 5 writes its own `live_test.py` during the build loop, runs
it, sees it pass, and signs `HARNESS_COMPLETE`. But the live test
invokes `harness.run()` with the payload shape the builder CHOSE
— e.g., `{"text": "hello", "input_context": {"instructions": ...},
"turn_index": 0, "session_state": {}}`. Production's plugin calls
with `{"audio_url": "...", "turn_index": 0, "session_state": {}}`
— no `input_context`, different input channel. Every prior session
found a bug here: the builder's happy-path live test didn't
exercise the production call shape.

**Impact**: `HARNESS_COMPLETE` doesn't reliably mean "production
will work." We've been reactive to each new mismatch.

**Current mitigation (NEW-AI capability fix 2)**: the runner-level
default `input_context` injection catches the input_context subclass
of this bug regardless. That's belt-and-braces, not a cure.

**Planned cure (documented in NEW-AI's Phase 4 block, NOT yet
shipped)**: mandate Agent 5 run a "REAL TEST PROBE" — pick one of
Agent 3's actual test cases, derive the production-shape payload
via an auto-seeded helper, call harness, verify success. The
verification gate enforces this deterministically; bypass impossible.
Design is in CLAUDE.md NEW-AI block; implementation deferred to
next session.

**Why deferred**: user prioritized verifying the current runner-level
default before adding more code — standard "change one thing at a
time" debugging. The real run (trace 28cb2648) verified that fix
works; now Phase 4 is the next layer.

### OT-014: Substring assertion matching is too brittle for natural-language voice scoring

**Symptom (observed in trace 28cb2648's candidate_runs)**: even when
agent audio is correctly saved and transcribed, per-turn scoring
matches against Agent 3's `expected_agent_contains` substrings —
a literal string check. Voice agents respond in natural language:
- Test asserts `"150"` → agent says "around one hundred and fifty"
- Test asserts `"Sarah"` → agent says "Can you confirm your name?"
  without re-stating "Sarah" verbatim
- Test asserts `"afternoon"` → agent says "this evening if that works"

**Impact**: per-test scores are 0.25-0.5 even when the agent is
semantically correct. Pass rate understates voice quality by
20-50%.

**Current mitigation**: none — this is the OT-001 class, broader
than just voice.

**Fix direction**: per-turn semantic-match fallback. When substring
match fails, escalate to a one-shot LLM judge call comparing the
transcript against the test's intent. ~$0.002 per escalation.
Estimate ~1 hour of work.

**Why not this session**: structural correctness (agent audio
actually reaching the report) was the higher priority; scoring
quality is a separate problem with clear remediation when we're
ready.

### OT-015: The pydub-merger path requires ffmpeg on PATH — byte-concat fallback is degraded

**Symptom**: if a future run has pydub installed but ffmpeg
missing, `_try_merge_via_pydub` fails at the MP3 decode/export
step, falls back to byte-concat. Byte-concat only produces playable
merged files when all per-turn segments share encoding parameters
(same sample rate + channel count). Mixed-format inputs produce
files that pause at the first transition (the exact bug we just
fixed this session).

**Impact**: only affects environments where ffmpeg isn't installed.
Our venv pre-install manifest includes pydub but NOT ffmpeg (ffmpeg
isn't pip-installable — it's a system binary). Windows boxes
without ffmpeg will hit the degraded path.

**Current mitigation**: the Agent 5 builder prompt recommends
ffmpeg availability checking in Phase 1. The merger doesn't
FAIL without ffmpeg — it falls back to byte-concat — but playback
quality degrades.

**Fix direction**: either document ffmpeg as a hard prerequisite
in README, OR add a system-level install check in the startup
script that warns if ffmpeg is missing.

### OT-016: Explicit-candidate relevance boost can still lose against packaged products at 0.95

**Symptom (observed in trace 28cb2648 selection pause)**: after
the boost, OpenAI and ElevenLabs land at `relevance_score=0.95`
each. But Phase 7's scope-selection uses weighted scoring with
multiple dimensions (user_picked_here, credentials, docs_quality,
pricing_fit) — not just relevance. A packaged product with
`adoption_difficulty=easy` + higher `docs_quality` can still beat
a boosted developer-primitive at the default_picks cutoff.

**Impact**: user named providers are ALWAYS in the pool (injection
works) and ALWAYS above the 0.84 floor, but may still be
unchecked by default if other candidates score higher on the
non-relevance dimensions. User has to manually flip 1-2 checkboxes.

**Workaround today**: the SelectionPanel shows all candidates,
user checks what they want before submit. Friction, not breakage.

**Fix direction**: in `selection.py`, add a hard override — if
a candidate's source is `user_explicit` OR it's substring-
matched against `explicit_candidates`, force it into default_picks
regardless of weighted score. The relevance boost then becomes
informational (shows "user asked for this") rather than the
mechanism that forces selection.

### OT-017: Module-level singleton voice plugin — shared _token_to_audio dict

**Symptom (observed during probe of trace 28cb2648)**: the
`VoiceRealtimePlugin._token_to_audio` dict is a single
thread-shared mapping, while `_session_dir` is thread-local (per
AD-004). For PARALLEL candidate runs this happens to work —
session tokens are unique `secrets.token_hex(16)` per session, so
there's no collision on keys. But a subtle gotcha: if two
candidates in the same run generate audio for the same
turn_token (extremely rare: would require token-hex collision),
the second write wins.

**Impact**: today, zero observed impact. Design smell for
correctness-critical scale-up.

**Fix direction**: make `_token_to_audio` thread-local too.
One-line change (`self._token_to_audio = threading.local()`,
then `self._tl.token_to_audio.setdefault(token, path)` etc.).
Not a priority until we see a real collision.

## Diagnostic Conventions (Phase Fingerprints + Flag Matrix)

We're shipping 10 phases of refinement without per-phase live testing —
the user explicitly chose to do live runs only at the end. To make fault
attribution tractable in that "one big live run" model, every phase from
Phase 1 onward MUST satisfy two diagnostic conventions.

### Convention 1: Phase Fingerprint

Every phase leaves a unique, queryable signal in the run output so an
operator can answer "did Phase N activate on this run?" with `grep`,
not by reading source. Fingerprints live in either:
- `pipeline_summary.json` under `metadata.<key>` (preferred for run-level
  aggregates — auto-promoted from agent results via
  `_AUTO_METADATA_FIELDS` in `puzzleeval/pipeline.py`), or
- the per-agent JSON output (e.g., `agent_2_output.json.candidates_by_step`)
  when the signal is naturally per-agent.

Fingerprint table (filled in as each phase ships):

| Phase | Signal | Where to find it |
|-------|--------|------------------|
| 1 + 1.5 | `web_fetch_blocks` (HTTP errors + content-level failures combined) | `pipeline_summary.json:metadata.web_fetch_blocks`; per-call breakdown in stderr logs as `web_fetch_blocks_by_code` / `web_fetch_unusable_by_category` |
| 2     | `credits_consumed`, `plan_gates_triggered` on `RunState`; per-call logs as `billing_gate_passed` / `billing_gate_triggered`; `Quota.credits_consumed` in `RunStateOut` | `RunState` fields + stderr logs; `pipeline_summary.json` rollup pending the pipeline_runner refactor noted above |
| 3     | `agent_1_output.json.result.workflow.steps[]` length > 0 (missing/null = pre-Phase-3 or unstructurable request); `workflow_blueprint` SSE event fires once after Agent 1 with the full payload | per-agent JSON + live SSE |
| 4     | `agent_2_output.json.candidates[].covers_step_ids` non-empty; `pipeline_summary.json:metadata.phase4_dual_search_active`, `.phase4_scopes_covered_count`, `.phase4_coverage_populated_all` | per-agent JSON + run-level metadata |
| 5     | `agent_2_output.json.candidates[].pricing_breakdown` non-null; `agent_4_output.json.validated_candidates[].pricing_breakdown.confidence` distribution (Phase 5a ships null-safe scaffold; Phase 5b populates inside Phase 6.5's 4B); UI `<PricingBlock>` mount | per-agent JSON + UI |
| 6     | `RunState.user_selection_applied=true`, `selection_required_emitted_at` ISO timestamp, `user_added_candidates` count; `selection_required` SSE event fires once after Agent 2 when pause is enabled | `RunState` fields + SSE stream |
| 6.5   | `agent_4_output.json` contains `failed_to_verify[]` + `scope_selections{}`; SSE events `candidate_verified` / `candidate_rejected` / `scope_verified_complete` per scope | per-agent JSON + SSE stream |
| 7     | `selection_required` SSE payload includes `default_picks`; Phase 7 auto-pick log visible in agent_activity | SSE stream + pipeline logs |
| 8     | `metadata.openapi_specs_found`, `metadata.docs_pages_traversed` | `pipeline_summary.json:metadata.*` (planned) |
| 9     | `agent_5_output.json.workflow_runs` length (0 = single-step legacy path) | per-agent JSON (planned) |
| 10    | bench/results/{timestamp}.json regression diff vs cassette baseline | `bench/` directory (planned) |

When implementing a phase, add its row to this table in the same commit
that adds the signal. If a phase doesn't have a natural fingerprint
(pure schema additions can fall into this trap), invent one — even a
boolean `metadata.phase_N_active=True` is enough.

### Convention 2: Diagnostic Flag Matrix

Every phase ships behind a feature flag so operators can binary-search
by flipping flags off one at a time. Schema-only changes (Phase 3) can't
be flag-disabled because downstream depends on the field existing —
those rows are documented as "schema-only" so the operator knows not
to look for a flag.

Diagnostic flag table (filled in as each phase ships):

| Phase | Env var | Default | Disable behavior |
|-------|---------|---------|-----------------|
| 1 + 1.5 | `PUZZLEEVAL_ENABLE_FETCH_FALLBACK` | `1` | Skip detection; agents see raw web_fetch errors and useless pages with no recovery guidance |
| 1 (sub) | `PUZZLEEVAL_FETCH_RATE_LIMIT_BACKOFF` | `5` (seconds) | Set to `0` to disable backoff sleep on 429 |
| 2     | `PUZZLEEVAL_BILLING_ENFORCED` | `0` | When `0`: track usage but don't block. When `1`: 402 on insufficient credits or feature-not-in-plan |
| 3     | `PUZZLEEVAL_AGENT1_MODEL` (soft) | `claude-opus-4-7` | Revert to `claude-sonnet-4-6` if Opus blueprint quality regresses. Schema itself cannot be disabled — downstream consumes `WorkflowBlueprint`. |
| 4     | `PUZZLEEVAL_RESEARCH_DUAL_SEARCH_ENABLED` | `1` | When `0`: Agent 2 reverts to single-pass search (max_uses=3, every candidate gets empty `covers_step_ids` → downstream flat flow) |
| 5     | (none at 5a — null-safe scaffold; 5b reuses `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` since extraction lives inside Phase 6.5's 4B) | — | Disabling Phase 6.5 disables pricing extraction; schema stays null-safe |
| 6     | `PUZZLEEVAL_USER_SELECTION_ENABLED` | `1` | When `0`: pipeline skips pause, auto-runs all Agent 2 candidates through Agent 4/5 (pre-Phase-6 behavior) |
| 6.5   | `PUZZLEEVAL_AGENT4_DEEP_VERIFY_ENABLED` | `1` | When `0`: Agent 4 reverts to shallow pass/fail; Agent 5 resumes Phase 1 research |
| 7     | `SCOPE_SELECTION_WEIGHTS` + `SCOPE_CANDIDATES_CAP_BY_PLAN` (config dicts) | tunable | Change weights without code changes; adjust per-plan caps |
| 8     | `PUZZLEEVAL_OPENAPI_HUNT_ENABLED` (planned) | `1` | When `0`: Agent 5 uses today's narrative-only research |
| 9     | `PUZZLEEVAL_WORKFLOW_HARNESS_ENABLED` (planned) | `1` | When `0`: per-candidate harness path even with multi-step blueprint |

Standard debugging procedure when something breaks at the end:
1. Check `pipeline_summary.json:metadata` against the fingerprint table —
   which phases activated?
2. Check the per-agent JSON outputs for the per-agent fingerprints
   (workflow blueprint, candidates_by_step, pricing breakdown, etc.).
3. If a fingerprint that should be there is missing → inspect that phase's
   code path; the phase didn't fire when it should have.
4. If all expected fingerprints are present but behavior is wrong → flip
   flags off in reverse phase order (`9 → 8 → 7 ...`) until behavior
   recovers; the last-flipped flag is the culprit.
5. If the behavior is wrong even with all toggleable flags off →
   schema-level change in Phase 3 introduced something downstream
   misinterprets; bisect via git over the schema commits.

This procedure is mechanical for ~80% of failure modes. The remaining
~20% (cross-phase emergent interactions, behavior regressions in agent
reasoning that don't change observable signals) still require AI-assisted
diagnosis from logs + traces. The conventions above don't eliminate that
need — they minimize how often it's the only available tool.

## The 9-Agent Pipeline

See `ARCHITECTURE.md` for the full spec. Summary:

```
User Input → [1. User Understanding] → [2. Research] → [4. Screening]
                                    ↘ [3. Synthetic Tests]    ↓
                                                    [5. Build + Test × N]
                                                    [7. Analyze × N]
                                                    [8. Ranking]
                                                    [9. Report]
```

- Agents 2 and 3 run in PARALLEL (both consume Agent 1's output)
- Agents 5 and 7 each run N instances in parallel (one per candidate)
- Agent 7 instances are ISOLATED (no cross-product context to avoid bias)

## Tech Stack

- **Language:** Python 3.11+
- **LLM:** Claude via Anthropic SDK (`client.messages.parse()` for structured outputs, `client.messages.create()` for server tools like web search)
- **Default model:** `claude-sonnet-4-6` (Sonnet 4.6) for Agents 1-4. Agent 5 uses Sonnet 4.6 for Phase 1 research and Opus 4.7 for Phase 2+ build/debug. Opus is reserved for agents where nuanced judgment matters (Agent 5 build, Agent 7).
- **Schema validation:** Pydantic v2 (also serves as JSON Schema for structured outputs)
- **Logging:** Structured JSON to stderr via Python stdlib logging. Ready for CloudWatch/Datadog with zero migration.
- **Frontend:** React/TypeScript (built separately in Lovable). Communicates via JSON API contracts defined by Pydantic models.
- **Backend API:** Not yet built. Will be FastAPI — thin wrapper around agent functions.

## Project Structure

```
PuzzleEval/
├── ARCHITECTURE.md              # Master reference for all 9 agents (input/output schemas, flow)
├── CLAUDE.md                    # THIS FILE — context for AI assistants
├── pyproject.toml               # Dependencies: anthropic, pydantic, python-docx, pytest
│
├── puzzleeval/
│   ├── __init__.py              # Package marker, version "0.1.0"
│   ├── __main__.py              # python -m puzzleeval entry point
│   ├── config.py                # Env-based config (API key, model, pricing tables, cache multipliers)
│   ├── exceptions.py            # AgentError hierarchy (RateLimit, API, Output, FileParse)
│   ├── logging_setup.py         # Structured JSON logging shared by ALL agents
│   ├── pipeline.py              # PipelineRun — saves intermediate outputs, generates summary
│   ├── validators.py            # Output quality validators per agent (catch silent failures)
│   ├── web_fetch_fallback.py    # Phase 1 hardening: detect Cloudflare/429/blocked fetches, inject fallback guidance for Agent 5
│   ├── schemas.py               # Pydantic models for Agents 1, 2, 3, 4, and 5
│   ├── file_parsers.py          # PDF/image (native Claude), DOCX/CSV/TXT (Python extraction)
│   ├── provider_registry.py     # Centralized API key management for live validation
│   ├── cli.py                   # CLI runner — Agent pipelines with validation + run persistence
│   │
│   └── agents/
│       ├── __init__.py
│       ├── user_understanding.py   # Agent 1 implementation
│       ├── research.py             # Agent 2 implementation (web search + structured output)
│       ├── synthetic_tests.py      # Agent 3 implementation (text-based synthetic test generation)
│       ├── synthetic_tests_file.py # Agent 3F implementation (file-based test generation)
│       ├── screening.py            # Agent 4 implementation (parallel API docs verification)
│       ├── implement_test_env.py   # Agent 5 implementation (autonomous harness builder)
│       ├── AGENT1_SKILL.md         # Prompt tuning reference (for humans, NOT sent to Claude)
│       ├── README_AGENT1.md        # Complete walkthrough of Agent 1 design
│       └── README_AGENT5.md        # Complete walkthrough of Agent 5 design
│
├── runs/                        # Pipeline run outputs (gitignored)
│   └── {trace_id}/             # One directory per run
│       ├── pipeline_summary.json
│       ├── agent_1_input.json
│       ├── agent_1_output.json
│       ├── agent_1_validation.json
│       ├── ...
│       └── harnesses/           # Agent 5 sandbox directories (one per candidate)
│           └── {candidate_slug}/
│               ├── harness.py, requirements.txt, smoke_test.py
│               ├── live_test.py           # When credentials available
│               ├── fetched_docs_*.txt     # Saved API documentation pages
│               ├── conversation_log.json  # Full build conversation log
│               └── .venv/                 # Isolated Python virtual environment
│
└── tests/
    ├── __init__.py
    ├── test_agent1.py           # 11 unit tests (all passing)
    ├── test_agent2.py           # 15 unit tests (all passing)
    ├── test_agent3.py           # 18 unit tests (all passing)
    ├── test_agent3f.py          # 8 unit tests (all passing)
    ├── test_agent4.py           # 23 unit tests (all passing)
    ├── test_agent5.py           # 52 unit tests (all passing)
    ├── test_validators.py       # 33 unit tests (all passing)
    ├── test_pipeline.py         # 10 unit tests (all passing)
    └── fixtures/
        ├── sample_input_clear.json
        └── sample_input_vague.json
```

## Agent 1 — Key Design Decisions

### Architecture Pattern: Stateless Request-Response

The agent is a pure function: `Agent1Input → Agent1Result`. It holds no state between calls. The caller (CLI today, FastAPI API tomorrow, React frontend eventually) manages conversation history and passes it on each call. This is the same pattern used by ChatGPT, Claude.ai, and every major API at scale.

### Structured Outputs

We use `client.messages.parse(output_format=Agent1Result)` which guarantees Claude's response matches our Pydantic schema. No manual JSON parsing anywhere in the codebase.

### Sub-Task Decomposition

Agent 1 breaks user requests into independent capability sub-tasks. Each sub-task gets its own search keywords focused on the CAPABILITY (e.g., "document OCR API"), not the end-to-end workflow (e.g., "invoice QuickBooks automation"). This ensures the Research Agent finds both all-in-one tools AND specialized tools.

`search_strategy` is always `"both"` — we search for all-in-one and modular approaches simultaneously. The user decides which they prefer AFTER seeing results, not before.

### Information Collection: Critical vs Optional

The agent uses a structured `InfoStatus` object to track what's been collected:

**Critical (blocks search without these):**
- `has_concrete_subtasks` — at least 1 testable input→output sub-task
- `has_domain` — business domain/industry

**Optional (improves results, never blocks):**
- `has_budget`, `has_technical_level`, `has_integration_requirements`, `has_workflow_file`

The conversation flow:
1. Missing critical info → ask `critical_questions`
2. Have critical, missing optional → show breakdown + `optional_prompt` inviting user to add more
3. User responds (or says skip) → `is_clear=true`, produce final output
4. Very detailed first request → skip conversation, produce output immediately

### Conversation: Max 4 Turns, Typically 2-3

The CLI runs a conversation loop (max `MAX_TURNS=4`). The agent decides when it has enough info. Typical flow for a vague request is 3 turns: critical questions → breakdown + optional prompt → final output.

### Integration Requirements Are Screening Checks, Not Search Filters

When a user says "must work with QuickBooks," this is recorded but NOT used to filter search results. The Research Agent searches by capability. The Screening Agent (Agent 4) later checks integration compatibility. This prevents missing great tools that don't advertise specific integrations but work via standard APIs.

### Ambiguous References Are Preserved

When users say "my system" or "our platform," Agent 1 records it as-is (e.g., `"user's existing business system (unspecified)"`). Downstream agents see the ambiguity and can handle it.

### File Handling

- **PDFs and images** → sent directly to Claude as native content blocks (base64 encoded). Claude's vision reads them. No external parsing libraries.
- **DOCX** → text extracted via `python-docx`, appended to system prompt
- **CSV** → read via stdlib `csv`, formatted as text table, capped at 100 rows
- **TXT** → read directly

### Prompt Caching: DISABLED for Agent 1

Agent 1 has a human in the loop (5-15 min between turns). The 5-min cache expires before follow-up. The system prompt (~3,500 tokens) is below Opus's 4,096 minimum cacheable threshold anyway. Caching should be ENABLED for Agents 5/7 which make rapid-fire calls with the same context.

Cache infrastructure is fully built (`CACHING_ENABLED` flag, block-level and top-level `cache_control`, cost calculation with cache multipliers in logging). Flip the flag to `True` for future agents.

### Model Choice: Sonnet 4.6 (default)

Agent 1 uses `DEFAULT_MODEL` (`claude-sonnet-4-6`). Parsing/classification task — Sonnet's sweet spot. Override with `PUZZLEEVAL_MODEL=claude-opus-4-7` environment variable.

### Cost Per Evaluation (Agent 1 only, Sonnet 4.6)

- 1-turn (clear request): ~$0.015
- 2-turn: ~$0.033
- 3-turn (vague → optional → final): ~$0.050

### Logging and Observability

Every API call logs to stderr as structured JSON:
```json
{"timestamp": "...", "agent_name": "agent_1_user_understanding", "tokens_in": 4031, "tokens_out": 189, "cost_usd": 0.014928, "latency_ms": 7709, "model": "claude-sonnet-4-5-20250929", "stop_reason": "end_turn", "trace_id": "..."}
```

`trace_id` (UUID) is generated per user request and threaded through all agents for cross-agent log correlation.

Cache metrics (`cache_creation_tokens`, `cache_read_tokens`) are tracked in logs even when caching is disabled — they'll show 0/null but the logging code is ready.

### Cost Tracking in Agent Results

All agents 1-4 now return `cost_usd` in their result schemas. `log_llm_call()` returns cost so callers can accumulate it. Agent 5's cost is tracked per-candidate in `TestHarness.build_cost_usd` and aggregated in `Agent5Result`.

### Error Handling

```
AgentError (base — carries agent_name + trace_id)
├── AgentRateLimitError    → maps to HTTP 429 in future API
├── AgentAPIError          → maps to HTTP 502
├── AgentOutputError       → maps to HTTP 500 (schema mismatch — very rare with structured outputs)
└── AgentFileParseError    → graceful degradation (proceeds without file)
```

## Agent 2 — Key Design Decisions

### Architecture Pattern: Two-Step Stateless Function

Agent 2 is NOT a single API call like Agent 1. It's two sequential calls:

```
Step 1: client.messages.create() + web_search tool
  → Claude searches the web, reads search result content, writes findings as text

Step 2: client.messages.parse() + output_format=Agent2Result
  → Takes the raw text from Step 1, structures it into guaranteed JSON
```

**Why two steps?** Server tools (web_search) require `client.messages.create()` which returns mixed content blocks (text + tool_use + tool_result). Structured outputs require `client.messages.parse()` with `output_format`. These can't be combined cleanly in one call — the tool-use content blocks conflict with structured output format. Step 2 is cheap (~$0.04) since it's just reformatting already-gathered data.

### How Web Search Server Tools Work (Critical Knowledge)

This is the most important thing to understand about Agent 2. Anthropic's web search is a **server tool** — it works differently from regular tool use:

1. You pass `tools=[{"type": "web_search_20250305", "name": "web_search"}]` in the API call
2. Claude decides when to search based on the prompt
3. The API **executes the search server-side** (you don't handle it)
4. Results are injected into the conversation context automatically
5. Claude continues generating, may search again
6. All of this happens inside ONE `client.messages.create()` call

**The token accumulation problem:** Each search adds results (~5-7K tokens of encrypted content per search) to the context. If Claude searches 3 times, the context grows by ~15-20K tokens. This all stays in memory for the duration of that single API call. With web fetch (loading full pages), this explodes to 50-100K+ tokens.

**`pause_turn` stop reason:** When the server-side loop takes too long, the API returns with `stop_reason="pause_turn"` instead of `"end_turn"`. The caller must decide whether to continue (re-send full context as a new call) or stop with partial results. We handle this with `MAX_CONTINUATIONS=1`.

### Search Strategy: "Survey → Score → Rank"

Agent 2 does NOT search for individual tools. It follows a 5-phase process:

1. **Search** — 1-2 searches for comparison/roundup articles ("best [capability] API tools 2026"). These surface 15-30 candidates in one shot.
2. **Collect** — list ALL tools/services mentioned across articles. This is the raw candidate pool.
3. **Score** — evaluate EVERY candidate on 3 dimensions (0-10):
   - **Capability fit**: how well it handles the user's sub-tasks
   - **Adoption fit**: how realistic it is for THIS user to set up (considering their technical level, the service's auth complexity, setup steps, docs quality)
   - **Use case fit**: whether the service is designed for someone like this user in their domain
4. **Weight** — Claude decides dimension weights based on user context. A non-technical construction owner gets Adoption 40% / Use Case 35% / Capability 25%. A senior SWE gets Capability 50% / Use Case 30% / Adoption 20%.
5. **Rank** — composite score = weighted sum → select top 5-7.

**Why scoring, not vibes:** Claude has training data biases (AWS/Google appear in 10x more training docs than Mindee). Without structured scoring, Claude gravitates toward what it knows best, not what fits the user. The scoring framework forces explicit evaluation on dimensions that matter FOR THIS USER.

**Why NOT fetch individual pages:** Web fetch loads full page content (5-25K tokens per page) into context. This was the #1 cause of our token explosion in early testing ($10 per run). Validation of API docs is Agent 4's job, not Agent 2's.

### Candidate Schema: adoption_difficulty

Each candidate carries an `adoption_difficulty` field (easy/medium/hard) derived from the adoption fit dimensional score:
- Adoption 7-10 → `"easy"` (signup → API key → REST calls)
- Adoption 4-6 → `"medium"` (OAuth, SDK config, platform accounts)
- Adoption 1-3 → `"hard"` (cloud accounts, IAM, service provisioning)

This flows through to Agent 4 (ScreenedCandidate), Agent 8 (ranking), and Agent 9 (final report). It's a description, not a filter — the scoring system handles prioritization.

### Web Search Tool Configuration

```python
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",   # Basic version, no dynamic filtering
    "name": "web_search",
    "max_uses": 3,                    # Hard limit: 1 comparison + 1-2 targeted
}
```

**Why basic (20250305) not dynamic filtering (20260209)?** We tested dynamic filtering — it adds ~8 extra server-side code execution iterations to filter search results, costing MORE in overhead than it saves. With only 2-3 searches and no web fetch, there's not enough content to justify filtering. Dynamic filtering pays off when you have heavy content (many searches, web fetch). The code comments document when to switch.

**Why `max_uses=3`?** Each search adds ~5-7K tokens to context. 3 searches = ~15-20K total. This keeps the total API call around 25-30K input tokens. Comparison articles from 1-2 searches already surface 10-15 candidates.

### Model Choice: Sonnet 4.6 for Both Steps

- **Step 1** uses `RESEARCH_MODEL` (Sonnet 4.6, configurable via `PUZZLEEVAL_RESEARCH_MODEL`). Sonnet 4.6 handles web search results better.
- **Step 2** uses `DEFAULT_MODEL` (Sonnet 4.6). Simple formatting task.

### Prompt Caching: DISABLED for Agent 2

Single-shot agent (no conversation loop). No repeated context to cache.

### What Agent 2 Does NOT Do (Agent 4's Job)

Agent 2 **discovers** candidates. It does NOT:
- Fetch API documentation pages (token-expensive, unnecessary for discovery)
- Verify API access or authentication methods
- Confirm pricing accuracy
- Check rate limits or free tier availability
- Validate integration compatibility

All of these are Agent 4 (Screening Agent)'s job. Agent 2 provides the candidate list; Agent 4 validates it.

### Cost Per Research Run (Agent 2 only, Sonnet 4.5/4.6)

- Step 1 (web research): ~$0.25-0.30 (dominated by search result tokens)
- Step 2 (structuring): ~$0.04
- Web searches: ~$0.02-0.03 (2-3 searches × $0.01)
- **Total: ~$0.30-0.40**

The ~70K input tokens in Step 1 are mostly encrypted search result content — this is the baseline cost of using Anthropic web search. Cannot be reduced without reducing search count.

### Cost Per Full Pipeline Run (Agent 1 + Agent 2)

- Agent 1 (3-turn conversation): ~$0.05
- Agent 2 (research + structure): ~$0.35
- **Total: ~$0.40**

### Lessons Learned Building Agent 2 (Read This Before Modifying)

These are hard-won lessons from iterative testing that cost real money:

1. **Web fetch is a token bomb.** Our first implementation used `web_fetch` to load API docs pages. Each page added 5-25K tokens to context, and that content stayed for ALL subsequent iterations. With 8 fetches, context hit 90K+ tokens across 10+ iterations. Cost: $10 per run. **Fix: removed web fetch entirely.**

2. **Dynamic filtering adds overhead for small payloads.** `web_search_20260209` enables Claude to write code that filters search results. But with only 2-3 searches, the code execution iterations cost more than the filtering saves. Tested: basic search = ~$0.30, dynamic filtering = ~$0.38. **Fix: use basic search (20250305).**

3. **`max_uses` is your most important cost control.** The prompt can say "stop after 2 searches" but Claude may ignore it. `max_uses` is a hard API-level cap. Every search added to `max_uses` increases worst-case cost by ~$0.05-0.10 in token accumulation.

4. **`code_execution` tool is auto-injected by 20260209 versions.** If you add `web_search_20260209` or `web_fetch_20260209`, the API auto-injects a `code_execution` tool. If you ALSO pass an explicit `code_execution` tool, you get a 400 error: "conflicting tool names." Don't pass it explicitly.

5. **Search results include substantial content, not just snippets.** The `encrypted_content` field in search results gives Claude real page content to read — enough to identify candidates, their capabilities, and often pricing. You don't need to fetch pages separately.

6. **`pause_turn` must be handled.** When the server-side tool loop takes too long, the API returns with `stop_reason="pause_turn"`. If you don't handle it, you miss the final text output. We allow 1 continuation max (`MAX_CONTINUATIONS=1`).

7. **Agent 2 should discover, not validate.** Early versions tried to validate candidates (fetch API docs, check pricing). This is Agent 4's job. Separating discovery (Agent 2) from validation (Agent 4) keeps Agent 2 fast and cheap.

### Key Files to Read (Agent 2)

To understand Agent 2, read in this order:
1. `puzzleeval/schemas.py` — Agent2Input, Candidate, Agent2Result (at the bottom, after Agent 1 schemas)
2. `puzzleeval/agents/research.py` — the core logic (look for ★ CORE LINE markers, same style as Agent 1)
3. `puzzleeval/config.py` — `RESEARCH_MODEL`, `WEB_SEARCH_PRICE_PER_SEARCH`

### CLI Usage

```bash
# Agent 1 only (unchanged):
python -m puzzleeval.cli --text "I need AI for customer support" --pretty

# Agent 1 → Agent 2 pipeline:
python -m puzzleeval.cli --text "I need invoice OCR" --agent2 --pretty

# Save output + logs:
python -m puzzleeval.cli --text "..." --agent2 --pretty > result.json 2> logs.jsonl
```

When `--agent2` is set, the CLI runs Agent 1's conversation loop first. When Agent 1 finishes (`is_clear=True`), it automatically pipes `UserUnderstandingOutput` into Agent 2. The final JSON output is Agent2Result (not Agent1Result).

## Agent 3 — Key Design Decisions

### Architecture: Two Agents, Same Output

Agent 1 marks each sub-task with `requires_test_files` (true/false). Routing is deterministic:

- **Agent 3** (`synthetic_tests.py`) — handles sub-tasks where `requires_test_files=false`. Generates synthetic text test data. Used for chatbots, classification, text generation, API integrations.
- **Agent 3F** (`synthetic_tests_file.py`) — handles sub-tasks where `requires_test_files=true`. Reads user-uploaded files via Claude vision, generates ground truth. Produces ONE test case per file — no synthetic text generation when files are provided. Simple and predictable.

Both produce `Agent3Result`. For mixed evaluations (some sub-tasks text, some file), both agents run and results merge. When `--agent5` is used, Agent 3F runs in parallel with Agent 2→4 for faster wall-clock time.

### Dynamic Test Case Count (Not Fixed at 20)

The count scales with sub-task complexity:
- **Base: 5-8 per sub-task** (middle target: 7)
- **Workflow bonus: +2 per sub-task** when `workflow_summary` exists
- **Min: 10 total, Max: 50 total**

The target is calculated in `_build_generation_message()` and passed to Claude in the prompt. Claude decides exact allocation within these bounds.

### Coverage Matrix (6 Dimensions)

Each sub-task is tested across 6 dimensions to ensure users never feel undertested:

1. **happy_path** — standard, clean input
2. **input_variation** — different formats/styles
3. **edge_case** — boundary conditions, unusual values
4. **scale** — single vs batch, small vs large
5. **domain_specific** — industry-specific scenarios
6. **error_resilience** — bad, partial, or corrupted input

Each test case is tagged with which dimensions it covers. The prompt requires at least one case per dimension per sub-task.

### Universal Test Format (Works With Any AI Service)

Agent 3 produces test cases in a canonical text format. Key abstractions:

- **`input_type`**: `"text"` | `"structured_data"` | `"document_content"` | `"conversation"` | `"image_description"` — tells the test runner the nature of the input
- **`output_type`**: `"free_text"` | `"structured_json"` | `"classification"` | `"extraction"` | `"action"` — tells the test runner what to expect back
- **`test_file_path`**: path to user-uploaded file (when file-based), or null for synthetic text tests

**Two agents, same output:**
- **Agent 3** (text mode, no files): Generates synthetic text input data. Used for chatbots, classification, text processing.
- **Agent 3F** (file mode, user uploaded files): Reads real files via Claude vision, generates ground truth and criteria. Used for OCR, document processing, image analysis.

Agent 1 sets `requires_test_files` per sub-task to determine which agent to use. Both produce `Agent3Result`.

### Weighted Judgement Criteria

Each test case has 2-5 `JudgementCriterion` objects with:
- **`criterion`**: specific, measurable ("Must extract vendor name correctly")
- **`weight`**: 0.0-1.0, importance (weights sum to ~1.0)
- **`eval_type`**: how to judge — `"exact_match"` | `"semantic_similarity"` | `"contains_key_info"` | `"format_compliance"` | `"subjective_quality"`

This gives Agent 7 precise, reproducible instructions for scoring.

### Cost Per Run (Agent 3 only, Sonnet 4.6)

- Single structured output call: ~$0.04-0.08
- Scales with test case count (more sub-tasks = more output tokens)

### Cost Per Full Pipeline Run (Agent 1 + Agent 2 + Agent 3)

- Agent 1 (3-turn conversation): ~$0.05
- Agent 2 (research + structure): ~$0.35
- Agent 3 (test generation): ~$0.06
- **Total: ~$0.46**

Note: Agents 2 and 3 run in PARALLEL, so wall-clock time is max(Agent 2, Agent 3), not sum.

### Key Files to Read (Agent 3)

1. `puzzleeval/schemas.py` — Agent3Input, TestCase, JudgementCriterion, Agent3Result (bottom of file, after Agent 2 schemas)
2. `puzzleeval/agents/synthetic_tests.py` — text-based synthetic generation (look for ★ CORE LINE markers)
3. `puzzleeval/agents/synthetic_tests_file.py` — file-based generation using user uploads

### CLI Usage

```bash
# Agent 1 → Agent 3 pipeline:
python -m puzzleeval.cli --text "I need AI for customer support" --agent3 --pretty

# Agent 1 → Agent 3 + 3F pipeline (with test files):
python -m puzzleeval.cli --text "I need invoice OCR" --agent3 --test-files invoice1.jpg,invoice2.pdf --pretty

# Save output + logs:
python -m puzzleeval.cli --text "..." --agent3 --pretty > result.json 2> logs.jsonl
```

When `--agent3` is set, the CLI runs Agent 1's conversation loop first. When Agent 1 finishes (`is_clear=True`), it automatically pipes `UserUnderstandingOutput` into Agent 3. The final JSON output is Agent3Result.

## Pipeline Observability

Every CLI run saves intermediate outputs and validation results to `runs/{trace_id}/`. This enables debugging without re-running the pipeline.

### What Gets Saved

```
runs/{trace_id}/
  pipeline_summary.json        # overall status, timing, cost per agent
  agent_1_input.json           # what Agent 1 received
  agent_1_output.json          # what Agent 1 produced
  agent_1_validation.json      # quality check results
  agent_2_input.json           # (if --agent2)
  agent_2_output.json
  agent_2_validation.json
  ...
```

### Output Quality Validators (`puzzleeval/validators.py`)

Pydantic validates structure. Validators check quality — will this output actually work for downstream agents?

- **Agent 1**: has sub-tasks? has domain? search keywords per sub-task?
- **Agent 2**: 4+ candidates? 3+ providers? all api_available=True? covers all sub-tasks?
- **Agent 3**: all sub-tasks covered? weights sum to ~1.0? difficulty spread? file instructions when file format is set? valid enum values?
- **Agent 4**: 2+ validated candidates? count consistency? all enrichment fields populated? valid rejection categories/auth methods/access methods? no silently dropped candidates?

Validators return `{passed, errors, warnings}`. Errors block the pipeline. Warnings are logged but don't block.

### Pipeline Summary (`pipeline_summary.json`)

One JSON report per run with every agent's status, duration, cost, and validation results. Status values:
- `"completed"` — all agents passed, no warnings
- `"completed_with_warnings"` — passed but validators flagged quality issues
- `"failed"` — an agent threw an error
- `"validation_failed"` — an agent's output failed quality checks

### Adding Validators for New Agents

When building Agent 4+, add a `validate_agent{N}_output()` function to `validators.py` and call it from `cli.py` after the agent runs. The pattern is always:
1. Define what "good output" means for downstream consumption
2. Distinguish errors (blocking) from warnings (non-blocking)
3. Cross-reference with upstream agent output when needed

## API Integration Path (historical note — the FastAPI backend shipped)

The FastAPI backend is built and live in `puzzleeval-api/` (2,344 LoC, 30
passing tests — see `puzzleeval-api/BACKEND_ARCHITECTURE.md` for the full
endpoint surface). The sketch below is the original plan for how the five
agent functions could be wrapped; the actual backend landed differently
(single conversational `/chat` endpoint + a pipeline task that streams SSE
events, rather than one endpoint per agent). Kept here for historical
context:

```python
@app.post("/evaluate/understand", response_model=Agent1Result)
def understand(input_data: Agent1Input):
    return run_user_understanding_agent(input_data)

@app.post("/evaluate/research", response_model=Agent2Result)
def research(input_data: Agent2Input):
    return run_research_agent(input_data)

@app.post("/evaluate/test-cases", response_model=Agent3Result)
def generate_tests(input_data: Agent3Input):
    return run_synthetic_tests_agent(input_data)

@app.post("/evaluate/screen", response_model=Agent4Result)
def screen(input_data: Agent4Input):
    return run_screening_agent(input_data)

@app.post("/evaluate/build-harnesses", response_model=Agent5Result)
def build_harnesses(input_data: Agent5Input):
    return run_implement_test_env_agent(input_data)
```

The frontend sends JSON matching the input schema, gets back JSON matching the result schema. Conversation history is managed by the frontend (React state) and sent on each call. The backend is stateless.

Agent 4 and 5's `ThreadPoolExecutor` parallelism works identically whether called from CLI, FastAPI, or a Lambda handler. At scale, the per-candidate `_build_single_harness()` function can be swapped to distributed workers (one Lambda/Cloud Run per candidate) without changing the function signature — it's already a self-contained function that takes a candidate in and returns a TestHarness or FailedHarness out.

## Key Files to Read

**Agent 1:**
1. `puzzleeval/schemas.py` — Agent1Input, Agent1Result, SubTask, etc. (top of file)
2. `puzzleeval/agents/user_understanding.py` — the core logic (look for ★ CORE LINE markers)
3. `puzzleeval/agents/AGENT1_SKILL.md` — prompt tuning guide and known failure modes

**Agent 2:**
1. `puzzleeval/schemas.py` — Agent2Input, Candidate, Agent2Result (middle of file)
2. `puzzleeval/agents/research.py` — two-step logic (look for ★ CORE LINE markers)
3. `puzzleeval/config.py` — RESEARCH_MODEL, WEB_SEARCH_PRICE_PER_SEARCH

**Agent 3 (text) + Agent 3F (file):**
1. `puzzleeval/schemas.py` — Agent3Input, TestCase, JudgementCriterion, Agent3Result (bottom of file)
2. `puzzleeval/agents/synthetic_tests.py` — text-based synthetic generation (look for ★ CORE LINE markers)
3. `puzzleeval/agents/synthetic_tests_file.py` — file-based generation using user uploads

**Agent 4:**
1. `puzzleeval/schemas.py` — Agent4Input, ScreenedCandidate, RejectedCandidate, Agent4Result (after Agent 3 schemas)
2. `puzzleeval/agents/screening.py` — parallel per-candidate verification (look for ★ CORE LINE markers)
3. `puzzleeval/config.py` — SCREENING_MODEL

**Agent 5:**
1. `puzzleeval/schemas.py` — Agent5Input, TestHarness, FailedHarness, Agent5Result (after Agent 4 schemas)
2. `puzzleeval/agents/implement_test_env.py` — autonomous builder loop (look for ★ CORE LINE markers)
3. `puzzleeval/config.py` — AGENT5_* settings

**Observability:**
1. `puzzleeval/validators.py` — output quality validators for all agents
2. `puzzleeval/pipeline.py` — PipelineRun class (saves outputs, generates summary)

## Agent 4 — Key Design Decisions

### Architecture: N Parallel Verification Calls + 1 Structuring Call

Unlike Agents 1-3 which use 1-2 API calls, Agent 4 makes **N parallel API calls** (one per candidate) using `ThreadPoolExecutor`, then one final structuring call. Each candidate gets its own isolated context — no token accumulation across candidates.

```python
def run_screening_agent(input_data: Agent4Input) -> Agent4Result:
    # Step 1: N parallel per-candidate calls (ThreadPoolExecutor)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_VERIFICATIONS) as executor:
        for candidate in candidates:
            executor.submit(_verify_single_candidate, client, candidate, ...)
    # Step 2: One structuring call → Agent4Result
    client.messages.parse(output_format=Agent4Result, ...)
```

### Why Per-Candidate Isolation (Critical Lesson)

Web_fetch loads entire pages into context (~10-140K tokens per page). If we fetched 7 candidates' API docs in one API call, context would accumulate to 70-980K tokens — the exact token explosion that burned us in Agent 2's early implementation ($10/run).

**Solution:** One API call per candidate. Each call has its own context. Candidate A's 140K-token docs page doesn't inflate Candidate B's context. Cost is predictable and linear.

### Search-First, Fetch-Only-When-Ambiguous (Cost Optimization)

The verification prompt follows the same strategy a human uses to find API docs:

1. **Step 1: SEARCH** (always first — cheap, ~5-7K tokens). Search for `"{name} API documentation"`. If search results clearly show real API docs (endpoints, auth, SDK install in snippets) → PASS immediately. Most well-known services (Google, AWS, Mindee) are verified at this step.

2. **Step 2: FETCH** (only if search was ambiguous). Fetch the specific URL to read actual page content and verify it's real API docs, not marketing.

3. **Step 3: HOMEPAGE** (last resort). Fetch the product's main website (from `source` URL) and look for "Docs"/"Developers"/"API" links.

This cut costs dramatically: Veryfi went from $0.45/candidate (fetching full 140K-token API reference) to $0.15/candidate (search results were enough).

### Tool Configuration (Per-Candidate)

```python
WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 3}
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
```

3 searches (standard + capability-specific + site-scoped) and 3 fetches (docs page + homepage + follow promising link). Basic versions only — dynamic filtering overhead exceeds savings at per-candidate scale.

The verification prompt uses a 4-step strategy: (1) standard search, (2) capability-specific search, (3) site-scoped search, (4) progressive fetch with link-following. An evidence-based PASS rule ensures candidates are only rejected when there is genuinely zero evidence of any API — marketing mentions, pricing tiers with "API access", broken docs URLs, or SDK packages all trigger a mandatory PASS with notes for Agent 5.

### ScreenedCandidate: The Contract With Agent 5

`ScreenedCandidate` is a NEW model (not `Candidate` with extra fields). It guarantees Agent 5 has everything it needs to start building test harnesses without guessing:

- `verified_api_docs_url: str` — **non-optional**, confirmed real docs URL (or best candidate URL if evidence exists but page was temporarily inaccessible)
- `auth_method: str` — api_key, oauth2, bearer_token, basic_auth, no_auth, unknown
- `api_access_method: str` — free_signup, free_tier, trial, sandbox, open, paid_only
- `confirmed_capabilities: list[str]` — verified from actual docs, not Agent 2 claims repeated
- `data_format_notes: str` — what input/output formats the API accepts
- `screening_notes: str` — audit trail of how the pass decision was made

### Parallel Execution

```python
MAX_PARALLEL_VERIFICATIONS = int(os.environ.get("PUZZLEEVAL_MAX_PARALLEL_SCREENING", "7"))
```

Default: all candidates verified simultaneously. Reduce if hitting rate limits on a lower API tier.

Latency: ~30-40 seconds for 7 candidates (parallel) vs ~3+ minutes (sequential).

### Graceful Degradation

If one candidate's verification call fails (rate limit, API error), it's marked as REJECT with a "transient failure" note. Other candidates continue. Only structuring step failures are fatal.

### Accuracy Rules (Anti-False-Positive AND Anti-False-Negative)

The prompt is explicit about both error types:
- Found real API docs with endpoints and auth → **PASS. Do NOT reject.**
- Cannot find docs after all 3 strategies → **REJECT.**
- Uncertain → **PASS with notes.** Agent 5 will verify deeper when building the test harness.

### Validator: Structural Checks Only

The validator checks structural correctness (non-empty fields, valid enum values, count consistency). It does NOT check semantic capability matching — that's the agent's job (Claude understands that "document OCR" and "invoice data extraction" mean the same thing; keyword matching doesn't).

### Model Choice

- Per-candidate verification: `SCREENING_MODEL` (defaults to `RESEARCH_MODEL` = Sonnet 4.6). Handles web content well.
- Structuring: `DEFAULT_MODEL` (Sonnet 4.6). Simple formatting task.

### Prompt Caching: DISABLED for Agent 4

Per-candidate calls have different content each time (different candidate). No repeated context to cache.

### Cost Per Screening Run (Agent 4 only, Sonnet 4.6/4.5)

- 7 parallel verification calls: ~$0.80-1.10 (varies by how many need multiple fetches)
- Structuring call: ~$0.10
- Web searches: ~$0.10 (7 × up to 3 × $0.01)
- **Total: ~$1.00-1.35**

### Cost Per Full Pipeline Run (Agent 1 + Agent 2 + Agent 4)

- Agent 1 (3-turn conversation): ~$0.05
- Agent 2 (research + structure): ~$0.19
- Agent 4 (screening + structure): ~$1.15
- **Total: ~$1.40**

### Lessons Learned Building Agent 4 (Read This Before Modifying)

1. **`web_fetch_20250305` does not exist.** The valid basic version is `web_fetch_20250910`. Valid versions: `web_fetch_20250910`, `web_fetch_20260209`, `web_fetch_20260309`. We wasted a full test run discovering this.

2. **STRUCTURE_MAX_TOKENS must be large enough for the full output.** 7 candidates × 16 fields each × rich text = a lot of JSON. 4096 tokens caused truncated JSON ("EOF while parsing"). Set to 16384.

3. **Search-first saves 3-10x on token costs.** First implementation fetched every candidate's API docs (140K tokens for Veryfi). Search results contain enough content to verify well-known services without fetching the full page. Only fetch for ambiguous cases.

4. **Don't do semantic matching in validators.** Keyword matching produces false warnings ("document OCR" vs "invoice OCR from PDF" share no 15-char prefix). The agent (Claude) already does semantic capability matching when deciding PASS/REJECT. Validators should focus on structural checks.

5. **ThreadPoolExecutor is sufficient for parallelism.** The Anthropic sync client is thread-safe for independent API calls. No need for asyncio complexity. Each thread makes its own HTTP request with its own context.

6. **Evidence of API = mandatory PASS.** Early versions rejected candidates when docs URLs returned errors, even when marketing confirmed a REST API exists. This is wrong — a broken URL doesn't mean no API. The rule: if ANY evidence of an API exists (marketing mentions, pricing tiers, broken docs URLs, SDK packages), PASS with notes. Only reject on genuinely zero evidence. Agent 5 has 15 turns with its own web tools to investigate further.

7. **Site-scoped search catches hidden API pages.** Generic searches like "DocuClipper API documentation" miss API pages nested under feature categories. Searching `site:docuclipper.com API` finds them because Google indexes every page on the domain. This is the third search tier after standard and capability-specific queries.

8. **Following links from the homepage is essential.** Many services don't link their API docs from the homepage navigation. But if you fetch the homepage and see a "OCR API" link under Features, you need to FOLLOW that link (a second fetch) to confirm it leads to real docs. The 3-fetch budget enables: docs page + homepage + follow promising link.

### Key Files to Read (Agent 4)

1. `puzzleeval/schemas.py` — Agent4Input, ScreenedCandidate, RejectedCandidate, Agent4Result (after Agent 3 schemas)
2. `puzzleeval/agents/screening.py` — parallel per-candidate verification (look for ★ CORE LINE markers)
3. `puzzleeval/config.py` — SCREENING_MODEL, PUZZLEEVAL_MAX_PARALLEL_SCREENING

### CLI Usage

```bash
# Agent 1 → Agent 2 → Agent 4 pipeline:
python -m puzzleeval.cli --text "I need invoice OCR" --agent4 --pretty

# --agent4 implies --agent2 (Agent 4 requires Agent 2's candidates)

# Save output + logs:
python -m puzzleeval.cli --text "..." --agent4 --pretty > result.json 2> logs.jsonl
```

When `--agent4` is set, the CLI runs Agent 1's conversation loop, then Agent 2 (research), then Agent 4 (screening). The final JSON output is Agent4Result.

## Pricing Reference (Anthropic, as of 2026-04)

| Model | Input | Output | 5m Cache Write | 1h Cache Write | Cache Read |
|---|---|---|---|---|---|
| Opus 4.7 | $5/MTok | $25/MTok | $6.25/MTok | $10/MTok | $0.50/MTok |
| Sonnet 4.6 | $3/MTok | $15/MTok | $3.75/MTok | $6/MTok | $0.30/MTok |
| Sonnet 4.5 | $3/MTok | $15/MTok | $3.75/MTok | $6/MTok | $0.30/MTok |
| Haiku 4.5 | $1/MTok | $5/MTok | $1.25/MTok | $2/MTok | $0.10/MTok |

Minimum cacheable tokens: Opus 4.7 = 4,096; Sonnet 4.6 = 1,024; Sonnet 4.5 = 1,024; Haiku 4.5 = 4,096.

## Agent 5 — Key Design Decisions (Updated 2026-04-11)

> **Status:** 4/4 builds, 3/4 pass evaluation (working_test_6: Veryfi 89%, Mindee 99%, Nanonets 90%). Cost: ~$4.40/run for 4 candidates, 289 seconds, 7-9 turns per candidate.
> Harness is a THIN API CLIENT — sends file, returns raw response. No parsing, no formatting.
> LLM judge evaluates raw API response against Agent 3F ground truth.

### Architecture: N Parallel Autonomous Builders + Post-Loop LLM-Judged Evaluation

Agent 5 builds a thin API client harness for each candidate, validates it with live API calls during the build, then runs ALL test cases post-loop with LLM-judged evaluation. Each candidate runs in parallel via ThreadPoolExecutor.

**Harness objective: THIN API CLIENT.** The harness sends files/data to the API and returns the raw response. No parsing, no formatting, no field extraction. Evaluation is done by a separate LLM judge that compares the raw API response against Agent 3F ground truth.

```python
def _build_single_harness(client, candidate, input_data, sandbox_dir, logger):
    _stage_test_files(sandbox_dir, test_cases)  # Stage BEFORE build loop
    _create_venv(sandbox_dir, ...)
    credentials = _resolve_credentials(...)

    while turn < MAX_TURNS:
        # Phase 1: Sonnet uses web_search/web_fetch for API docs, writes api_spec.txt
        # Phase 2: Opus builds harness.py (thin API client), runs smoke test
        # Phase 3: Opus runs live API validation with real files (credentials injected)
        current_model = OPUS if api_spec_written else SONNET
        response = client.beta.messages.create(
            model=current_model,
            tools=[web_fetch, web_search, advisor, write_file, run_code, read_file, ask_research],
            thinking={"type": "adaptive"},
        )
        if "HARNESS_COMPLETE" in response:
            break

# After all builds complete:
for harness in successful_harnesses:
    results = _execute_all_tests(harness, test_cases, credentials)  # Mechanical execution
    evaluate(results, judgement_criteria)  # ALL criteria → LLM judge (no mechanical eval)
```

### The 4-Phase System Prompt

1. **PHASE 1: RESEARCH** — Use server-side web_search and web_fetch to find API docs. Follow search→navigate→fetch→synthesize pattern. Write api_spec.txt with INPUT_COMPATIBILITY, ROUTING_TABLE, PYTHON_EXAMPLES, DOC_MAP, DOC_REFERENCES, API_LIMITATIONS. The agent knows ALL test case input forms upfront and researches whether each is compatible. Note: ask_research is for Phase 2+ debugging only, NOT for initial research.
2. **PHASE 2: BUILD** — Write harness.py as a THIN API CLIENT. Sends files/data, returns raw API response. No parsing, no formatting, no field extraction. Incompatible forms return `success=False, error="INCOMPATIBLE: reason"`. Smoke test verifies structure.
3. **PHASE 3: VALIDATE** — Run live API calls with real test files (credentials injected via `_dispatch_tool`). Requires real API success (`success=True`, `output_len > 100`) for EACH file type before HARNESS_COMPLETE. Smoke test alone is NOT sufficient. Milestone message injected when smoke test passes to signal Phase 3 transition.
4. **PHASE 4: COMPLETION CHECKLIST** — Verify all compatible forms work with live API, all incompatible forms return clean errors, signal HARNESS_COMPLETE.

### Post-Loop Test Execution and LLM-Judged Evaluation

After all harnesses are built, Python infrastructure runs ALL Agent 3 test cases through each harness using `_execute_all_tests()` and `_compute_aggregate_metrics()`. **ALL evaluation criteria go to a single LLM judge** — no mechanical eval (no exact_match, no format_compliance). The LLM judge receives the raw API response (truncated at 15K chars), the expected output from Agent 3F ground truth, and the judgement criteria. Uses `response.parsed_output` (not `.parsed`) for structured evaluation results. No adaptive thinking on eval calls. Results are stored in `Agent5Result.candidate_runs`.

### The Verification Gate

When Claude signals HARNESS_COMPLETE, `_run_verification_checks()` confirms harness.py exists. The agent already validated with real test data in Phase 3 — no additional programmatic checks needed. No cosmetic code review (caused over-correction in earlier iterations).
### Model Strategy: Sonnet for Research, Opus for Build

- **Phase 1 (research):** Sonnet 4.6 — uses server-side web_search and web_fetch (NOT ask_research) to find API docs. Follows search→navigate→fetch→synthesize pattern. I/O-heavy, doesn't need Opus reasoning. Cost: ~$0.10-0.30/candidate.
- **Phase 2+ (build/verify):** Opus 4.7 — planning, coding, debugging need strong reasoning. Transition detected when api_spec.txt is written.
- **Opus Advisor:** Available in all phases. Sonnet/Opus can call `advisor()` for strategic guidance. Typically called once per candidate before writing code. Uses `advisor-tool-2026-03-01` beta.
- **ask_research:** Sonnet 4.6 — targeted web search for Phase 2+ debugging ONLY, not for initial research.

### Eight Tools Available to the Builder Agent

| Tool | Type | Purpose |
|------|------|---------|
| `web_fetch` | Server (Anthropic API) | Read API docs (max_content_tokens: 15000) |
| `web_search` | Server (Anthropic API) | Search for SDK docs, examples, tutorials |
| `advisor` | Server (Anthropic API) | Consult Opus 4.7 for strategic guidance |
| `write_file` | Custom (local dispatch) | Write NEW files (harness.py, requirements.txt, smoke_test.py) |
| `patch_file` | Custom (local dispatch) | String-replace editing on EXISTING files |
| `run_code` | Custom (local dispatch) | Run shell commands (120s timeout) |
| `read_file` | Custom (local dispatch) | Read files from sandbox |
| `ask_research` | Custom (spawns sub-agent) | Targeted web research for debugging |

### Context Engineering

- **`max_content_tokens: 15000`** on web_fetch — prevents context explosion. All real API docs fit in 15K tokens; the extra is HTML noise.
- **Server-side context management:** `clear_tool_uses_20250919` at 80K tokens + `compact_20260112` at 150K. No manual context resets.
- **Automatic prompt caching:** `cache_control={"type": "ephemeral"}` at request level. Caches growing conversation prefix. 83-86% cache hit rate in production runs.
- **Accurate cost tracking:** Uses `response.usage.iterations[]` array to track executor vs advisor costs separately.
- **Credentials injected during build:** `_dispatch_tool()` passes credentials to `run_code` so the builder agent can do live API validation during Phase 3. Previously credentials were only available in post-loop execution.
- **Test files staged before build:** `_stage_test_files()` runs BEFORE the builder loop (not just during post-loop). Absolute file paths shown in the initial message so the agent can find and use real test files during Phase 3 validation.

### Configuration Settings

- `AGENT5_BUILDER_MODEL = Opus 4.7` — strong reasoning for coding/debugging
- `RESEARCH_MODEL = Sonnet 4.6` — for Phase 1 research (web_search/web_fetch) + ask_research in Phase 2+
- `AGENT5_MAX_TURNS = 25` — typical successful build: 7-9 turns
- `MAX_TURNS_AFTER_SMOKE = 15` — turns allowed for live API validation after smoke test
- `AGENT5_MAX_PARALLEL = 5` — max concurrent candidate builds
- `AGENT5_CODE_TIMEOUT = 120s` — timeout for run_code (increased from 30s for async APIs that need polling)
- `AGENT5_MAX_OUTPUT_TOKENS = 8192` — code generation needs more than default 4096
- `AGENT5_MAX_CANDIDATES = 4` — top N by user-fit score
- `MAX_BUILD_TIME_SECONDS = 480` — 8-minute wall-clock per candidate

### Behavioral Instructions (Claude Code-Inspired)

The system prompt uses behavioral tags that shape how the model works:
- `<use_parallel_tool_calls>` — batch independent tool calls in one turn
- `<do_not_narrate>` — act, don't explain each step
- `<do_not_re_read>` — don't re-read unchanged files
- `<investigate_comprehensively>` — one script that gets all info, not five separate ones
- `<think_before_acting>` — verify unknowns before writing code
- `<verify_against_docs>` — read code back and compare to api_spec
- `<reason_about_errors>` — reason about root cause, don't follow recipes
- `<be_resourceful>` — create local files when URLs fail
- `<commit_and_course_correct>` — commit to approach, course-correct on failure

### api_spec.txt Format

Phase 1 research produces a structured spec with these sections:
- `SERVICE`, `BASE_URL`, `ENDPOINTS`, `AUTH_HEADER`, `REQUEST_FORMAT`, `RESPONSE_FORMAT`
- `SDK_PACKAGE`, `ACCEPTED_INPUT_FORMATS`, `SAMPLE_TEST_URL`
- `PYTHON_EXAMPLES` — code snippets from docs (multiple, for builder to copy)
- `DOC_REFERENCES` — bookmark URLs for debugging (grows during build)
- `DOC_MAP` — all doc pages discovered (even unfetched ones)
- `INPUT_COMPATIBILITY` — YES/NO per input type (file, URL, text, base64)
- `API_LIMITATIONS` — what the API cannot do
- `ROUTING_TABLE` — input scenario to endpoint mapping

The 5-min TTL works because turns within a single candidate's build happen rapidly (seconds apart). Different candidates (running in parallel threads) each get their own cache entry.

### Rate Limit Retry with Exponential Backoff

Parallel builds across candidates can hit the per-minute token limit. Instead of immediately returning `FailedHarness`, Agent 5 retries with exponential backoff:

- Retry 1: wait 15 seconds
- Retry 2: wait 30 seconds
- Retry 3: wait 60 seconds
- After 3 retries: return `FailedHarness` with `build_timeout` category

This is implemented per-candidate inside `_build_single_harness()`. Other candidates continue building while one waits.

### Conversation Log Per Candidate

Every candidate's build saves `conversation_log.json` to its sandbox directory. This records every turn: Claude's text, tool calls made, tool results received, verification gate outcomes, context resets, and cost per turn. Invaluable for debugging build failures without re-running the pipeline.

### Dead-End Detection

Tracks consecutive error turns via a `consecutive_errors` counter. When tool results contain error signals (error, traceback, 401, 404, etc.) for 3 consecutive turns, the loop injects a STRATEGIC REASSESSMENT message forcing Claude to either pivot to a different approach or signal HARNESS_FAILED. This prevents 7-turn debugging spirals on unsolvable problems (wrong SDK version, deprecated endpoint, requires manual account setup). The counter resets after the reassessment.

### Phase 1 Research (Server-Side Web Tools)

Phase 1 research uses server-side web_search and web_fetch directly (NOT the ask_research sub-agent). The agent follows a search→navigate→fetch→synthesize pattern: search for API docs, navigate promising results, fetch documentation pages, and synthesize findings into api_spec.txt.

**Why server-side tools, not ask_research:** ask_research spawns a separate sub-agent with its own context, which loses the accumulated knowledge from previous searches. For initial research, the builder agent needs to iteratively search, read results, and search again based on what it finds. The server-side tools keep all this in one context. ask_research is reserved for Phase 2+ debugging when the builder hits an error it can't solve from saved docs.

If Phase 1 research fails (docs behind auth), the builder can still fall back to ask_research for targeted questions.

### Patch File Tool (Efficient Bug Fixing)

The builder agent has a `patch_file` tool for string-replace editing (same pattern as Claude Code's FileEditTool). When fixing a bug, the agent sends only the diff (~50 tokens) instead of rewriting the entire file (~2K tokens). This saves ~6-10K tokens per candidate across 3-5 fix iterations.

### Mid-Loop Targeted Research (ask_research Tool)

When the builder hits an error it can't solve from saved docs, it can invoke `ask_research` with a specific question. This spawns a fresh research sub-agent (separate context, 2 searches + 2 fetches) that searches the web and returns findings as a tool result. The answer is also saved to `research_turnN.txt` for future reference.

**Reference chain (cheapest first):** PLAN notes → read_file(saved docs) → ask_research → web_search → web_fetch

**Why:** The upfront research sub-agent can't anticipate every question the builder will have (e.g., "What's the pagination format?", "How do I handle async job polling?"). Mid-loop research provides answers without consuming the builder's own web tool budget. Cost: ~$0.10-0.15 per invocation.

### In-Loop Context Management (Auto-Compact)

Inspired by Claude Code's 3-tier auto-compact system. Before each API call, estimates total message characters. When approaching the context limit (`CONTEXT_CHARS_LIMIT = 600K chars ≈ 150K tokens`), compacts older messages — keeping only the initial message + last 4 message pairs. This prevents context overflow during long builds with multiple web fetches.

### Prompt-Too-Long (PTL) Recovery

When the API returns a "prompt too long" error (BadRequestError), the loop:
1. Compacts the conversation context
2. Reduces `max_tokens` by half (minimum 4096)
3. Retries the API call

This is the same pattern Claude Code uses for `max_tokens` overflow recovery. Without this, a PTL error would crash the entire harness build.

### Large Output Persistence

Tool outputs exceeding 5K chars are saved to disk (`output_turnN.txt`) and the model receives a truncated preview (first 1K + last 2K chars) with a file path reference. The model can use `read_file()` to access the full output on demand.

**Why:** Errors are usually at the END of long outputs (pip install, test runs). Simple truncation at 5K chars cuts off the error message. The head+tail preview ensures Claude sees both the beginning (context) and end (error) of large outputs.

### Wall-Clock Timeout

Each candidate build has an 8-minute wall-clock limit (`MAX_BUILD_TIME_SECONDS = 480`). Prevents runaway builds from blocking the pipeline. The budget cap ($3) is the primary limit; the wall-clock timeout catches edge cases where the agent loops on cheap operations.

### OS Detection

The system prompt dynamically includes `OS: Windows` or `OS: Linux` based on `sys.platform`. This tells the builder agent to use cross-platform commands (`python -c "import os; print(os.listdir('.'))"` instead of `ls`) and `os.path` instead of hardcoded path separators.

### Candidate Selection (Top N by User-Fit Score)

Not all validated candidates get harnesses built. `run_implement_test_env_agent()` sorts candidates by `relevance_score` (the user-fit composite score from Agent 2) and takes the top `AGENT5_MAX_CANDIDATES` (default 4). This avoids building 6 harnesses at $0.50 each when we only need 3 for comparison. The 4th is buffer for build failures.

### Incomplete Harness Detection

If the builder loop exits without the smoke test ever passing AND the verification gate never ran, the result is a `FailedHarness` (not a broken `TestHarness`). This prevents silently passing incomplete harnesses to downstream agents.

### Sandbox File Artifacts

Each candidate's sandbox directory contains:
- `harness.py` — the built harness code
- `requirements.txt` — pip dependencies
- `smoke_test.py` — structural validation test
- `live_test.py` — live API validation (if credentials available)
- `fetched_docs_0.txt`, `fetched_docs_1.txt`, ... — saved API documentation pages
- `conversation_log.json` — full build conversation log
- `.venv/` — isolated Python virtual environment

### Tool Configuration

```python
WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 5}
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}
```

Increased from 3/2 to 5/4 after analyzing run failures. Complex APIs (docs behind auth, multi-page docs) need more fetches. Cost increase (~$0.10-0.15/candidate) is negligible vs cost of a failed build ($0).

### Cost Per Build Run (Agent 5 only, Sonnet 4.6 + Opus 4.7 with adaptive thinking)

- Research + build per candidate (7-9 turns): ~$0.80-1.30
- Targeted research calls (0-1 per candidate): ~$0.05-0.15
- Post-loop LLM evaluation: ~$0.10-0.20
- **Per candidate total: ~$0.95-1.65**
- **4 candidates total: ~$4.00-5.00**

Benchmark (working_test_6): 4/4 builds, $4.40 total, 289 seconds, 7-9 turns per candidate.

### Cost Per Full Pipeline Run (Agent 1 + Agent 2 + Agent 3 + Agent 4 + Agent 5)

- Agent 1 (3-turn conversation): ~$0.05
- Agent 2 (research + structure): ~$0.35
- Agent 3 (test generation): ~$0.06
- Agent 4 (screening + structure, with max_content_tokens=15000): ~$0.80
- Agent 5 (research + harness building + LLM eval, 4 candidates): ~$4.50
- **Total: ~$5.50-6.50**

Note: With `--agent5`, Agent 2→4 and Agent 3F run in parallel, so wall-clock time is faster than sequential.

### Graceful Degradation

If a harness can't be built:
- Builder agent signals "HARNESS_FAILED" → `FailedHarness` with categorized reason
- Rate limit error → `FailedHarness`, other candidates continue
- Budget exceeded → `FailedHarness` with `build_timeout`
- Unexpected exception → caught by ThreadPoolExecutor, `FailedHarness` with `unknown`
- Zero harnesses → validator blocks pipeline (no test execution possible)

### Cloud Scaling Path

1. **Stage 1 (now):** Local CLI. ThreadPoolExecutor, venv-isolated sandbox dirs under `runs/{trace_id}/harnesses/`
2. **Stage 2:** Docker containers for code execution. Swap `_create_venv()` with container creation — `_build_sandbox_env()` is the designed seam. Agent loop stays on server (stateless API calls), `run_code` dispatches to Docker per candidate.
3. **Stage 3:** Cloud Run / Serverless. Each `_build_single_harness()` as a Cloud Run job. Harness artifacts to GCS/S3. Provider registry moves to Secrets Manager.
4. **Stage 4:** Managed sandboxes (E2B, Modal, Firecracker). Sub-second spin-up, pre-built images, network isolation.

### Lessons Learned Building Agent 5 (Read This Before Modifying)

1. **Custom tools and server tools mix cleanly.** The API handles server tools (web_fetch, web_search) internally. Custom tools (write_file, run_code, read_file) return with `stop_reason="tool_use"` for local dispatch. Both can coexist in the same `tools` list.

2. **The 4-phase system prompt makes the agent loop predictable.** Claude follows Research → Build → Verify → Checklist. Without explicit phases, Claude would sometimes skip verification or write code from memory instead of fetched docs.

3. **Structural validation catches most issues without API keys.** Mocking the HTTP layer lets the smoke test verify imports, function signatures, return types, and error handling — all without needing the candidate's API key.

4. **The verification gate catches bugs the smoke test misses.** Smoke tests validate structure (does `run()` exist? does it return the right keys?). The verification gate validates correctness (is the endpoint URL real? does the auth header match the docs?). Moving verification inside the loop (not post-loop) lets Claude fix issues while it still has context.

5. **`max_tokens=8192` is necessary for code generation.** The default 4096 truncates harness code mid-function. Full harness.py + requirements.txt + smoke_test.py needs ~2-3K tokens of output.

6. **`_candidate_slug()` must handle edge cases.** Candidate names can contain parentheses, trademark symbols, and unicode. The slug function strips everything non-alphanumeric and truncates to 40 chars.

7. **Live test injection before the loop is better than post-loop live validation.** Early design ran live validation only after the loop. The current design injects `live_test.py` before the loop starts, so Claude discovers and runs it during Phase 3. If the live test fails, Claude fixes the harness while it still has the API docs in context.

8. **Context compression is the single biggest cost lever.** Web_fetch content accumulates in context across turns. Extracting it to files and resetting the conversation after the research phase cut per-candidate cost by ~50%. The key insight: Claude only needs the full docs during research. During build/verify, it can `read_file()` specific sections on demand.

9. **"PLAN:" detection is a reliable phase boundary.** Claude reliably writes "PLAN:" before transitioning from research to build (the system prompt requires it). This makes it a safe trigger for the context reset. If PLAN is never written (rare), no reset happens and the loop runs at the old cost — safe degradation.

10. **Rate limit retries prevent unnecessary failures.** Parallel builds across candidates regularly hit per-minute token limits. Exponential backoff (15s, 30s, 60s) lets the rate limit window reset. Most rate limits are resolved by the first retry.

11. **PLAN + tool_use in the same turn needs special handling.** When Claude writes PLAN text AND calls write_file in the same response, the context reset must dispatch the tools silently WITHOUT appending `response.content` back to messages — that would re-add the web content we are trying to drop. The tools are dispatched separately and their results summarized in the fresh message chain.

12. **Re-inject live_test.py every time harness.py is written.** The initial injection (before the loop) does not have harness.py yet, so it cannot map env var names. After each write_file("harness.py"), `_inject_live_test_script()` re-runs with fuzzy env var name mapping (`_env_var_similarity()`) to handle mismatches like VERYFI_INC_API_KEY vs VERYFI_API_KEY.

13. **Dead-end detection prevents debugging spirals.** Without it, Claude could spend 7 turns trying to fix an unfixable problem (deprecated endpoint, requires manual dashboard setup). The consecutive error counter + strategic reassessment message forces Claude to either pivot or fail gracefully after 2 consecutive error turns (reduced from 3).

14. **Research sub-agents eliminate the research-vs-build tradeoff.** DocuClipper failed because research consumed 10/15 turns, leaving no time to build. By isolating research into a separate API call (Claude Code's s04 sub-agent pattern), the builder always starts with complete knowledge. Cost: ~$0.15-0.25 per candidate. Savings: 3-5 fewer builder turns + fewer fix iterations from correct endpoints.

15. **patch_file saves significant tokens during fix iterations.** The original design only had write_file — every bug fix rewrote the entire harness.py (~2K tokens). With patch_file (string-replace editing), fixes send ~50 tokens. Over 3-5 fix iterations per candidate, this saves 6-10K output tokens.

16. **Verification gate must not loop.** The original verification gate ran `_run_verification_checks()` redundantly when retries were exhausted (Lido bug). Fix: when retries are exhausted, accept the harness without re-checking. The builder already tried to fix the issues.

17. **Wall-clock timeout catches edge cases.** Budget cap is the primary limit, but cheap operations (run_code, read_file) can loop without hitting budget. 5-minute wall-clock timeout prevents this.

18. **In-loop context management prevents silent failures.** Without auto-compact, context can grow past the window limit after the PLAN-based reset. The API returns truncated or empty responses, which look like agent confusion — not context overflow. Estimating message size before each call and compacting when needed prevents this entire failure class.

19. **PTL recovery is free insurance.** BadRequestError for "prompt too long" is a 1-line check + compact + retry. Without it, the harness fails. With it, the conversation is trimmed and continues. Zero cost when not triggered.

20. **Large output persistence keeps errors visible.** pip install, test runs, and tracebacks regularly exceed 5K chars. Simple truncation cuts off the error message (always at the end). Head+tail preview (first 800 + last 3K) with disk persistence ensures Claude sees both context and error. This alone fixes many "Claude can't see what went wrong" failures.

21. **Exit code interpretation prevents false error detection.** Non-zero exit codes aren't always errors — grep returns 1 for "no matches," not failure. Claude Code's commandSemantics pattern adds semantic context to exit codes. Without this, the dead-end detector sees "exit code 1" as an error signal and triggers unnecessary reassessments.

22. **Microcompact before autocompact saves messages.** Claude Code uses a two-stage compaction: first clear old tool result content in-place (keeping message structure), then drop entire messages only if still over limit. Microcompact preserves conversation flow while freeing tokens. Autocompact is the nuclear option.

23. **Research sub-agents need context to be useful.** A bare question like "What auth does Parseur use?" gets generic answers. Enriching with the actual error, harness code snippet, and service details makes research targeted. The difference: generic → "Parseur uses token auth" vs targeted → "Your auth header uses 'Bearer' but Parseur expects 'Token' — change line 15."

24. **Dynamic max_tokens prevents context overflow.** When context is large (estimated from message chars), reduce max_tokens proactively instead of waiting for the API to reject the request. Cost savings: avoids wasted API call + retry cycle on PTL errors.

25. **Smoke test pass must be tracked across ALL turns.** The original code checked `last_text` (final turn only). In run c059a231, all 4 candidates passed smoke tests at turns 12-16 but the agent kept verifying until turn 24. `last_text` at turn 24 was empty → `smoke_passed=False` → FailedHarness. Fix: `smoke_ever_passed` boolean tracked across every turn's tool results.

26. **Completion nudge prevents verification spirals.** When the smoke test passes, inject a message telling the agent to wrap up. Without this, the agent enters Phase 3 (self-review) and loops forever trying to re-read/re-verify code that already works. The nudge says: "Smoke test passed. Signal HARNESS_COMPLETE now."

27. **Force-accept after smoke + N turns.** If smoke passed N turns ago and the agent hasn't signaled HARNESS_COMPLETE, force-break and accept the harness. This is the safety net for agents that get stuck in verification loops. `MAX_TURNS_AFTER_SMOKE = 5`.

28. **Windows output suppression detection.** `python -c "print(...)"` on Windows sometimes returns exit 0 with empty stdout. Without detection, the agent retries the same command 10+ times in a diagnostic spiral. Fix: detect the pattern and suggest writing a .py file instead.

29. **Two-tier validation gate: smoke → live.** Smoke test proves code structure. Live test proves API connectivity + auth with real files. Phase 3 requires real API success (`success=True`, `output_len > 100`) for EACH file type — smoke test alone is NOT sufficient for HARNESS_COMPLETE.

30. **Live test failure blocks test execution.** If live API validation fails, the harness returns as FailedHarness, not TestHarness. The post-loop test runner never receives broken harnesses.

31. **Research agent must find ALL endpoints, not just one.** The spec template uses `ENDPOINTS:` (plural) with format per endpoint. The #1 failure cause was research finding ONE endpoint (file upload) and the coding agent guessing the format for other endpoints (URL submission). With all endpoints documented, the coding agent builds correct code on the first try.

32. **Research agent must search for OpenAPI/Swagger specs.** The OpenAPI spec (`openapi.json`) is the ground truth for all endpoints, request formats, and response structures. Blog posts and quickstart guides show ONE simple example and miss everything else. The research prompt now explicitly searches for `"{service} openapi.json OR swagger.json"`.

33. **Request format per endpoint is critical.** Different endpoints on the SAME API often use DIFFERENT request formats (e.g., file upload = `files=`, URL submission = `data=`, not `json=`). The spec must document the Python `requests` parameter (`json=`, `data=`, `files=`, `params=`) for EACH endpoint. Getting this wrong causes "missing field" errors even when the code sends the right data.

34. **Adaptive thinking (interleaved reasoning) is the single biggest performance lever.** With `thinking={"type": "adaptive"}`, the model reasons BETWEEN tool calls within a single turn. A debug cycle that took 5 turns (see error → read file → read spec → patch → rerun) now takes 1-2 turns. Reduced typical build from 25+ turns to 18-22 turns.

35. **Credentials injected during build enable live validation.** `_dispatch_tool()` passes credentials to `run_code` so the agent can validate with real API calls during Phase 3. Previously credentials were only available post-loop, so the agent couldn't test live during the build. Test files are staged before the loop via `_stage_test_files()` with absolute paths in the initial message.

36. **Escalating error recovery prevents debugging spirals.** Three tiers: (1) fix specific issue, (2) question fundamental assumptions via ask_research, (3) try completely different approach or fail. Error category tracking detects when the same type of error (auth/endpoint/format) repeats 3+ times and tells the agent its APPROACH is wrong, not the details.

37. **Credential error detection saves turns.** When the API returns "API key is invalid" or "service not enabled," this is unfixable by code changes. The system detects these immediately and returns FailedHarness instead of burning 15 turns trying different auth formats.

38. **Server-side context management (beta API) replaces manual compaction.** Uses `context_management` parameter with `clear_tool_uses_20250919` (clears old tool results at 80K tokens) and `compact_20260112` (Claude-powered summarization at 150K tokens). Strictly better than manual character-based estimation.

39. **Exit-code-based `is_error` replaces string matching.** `_dispatch_tool()` returns `(result, exit_code)` tuple. `is_error` is set based on exit code, not string pattern matching for "error"/"traceback". This eliminates false positives where successful output contains the word "error" (e.g., "error handling configured successfully").

40. **Phase 1 research uses server-side web tools, not ask_research.** ask_research spawns a sub-agent that loses accumulated context. For initial research, the builder agent needs iterative search→navigate→fetch→synthesize, all in one context. ask_research is reserved for Phase 2+ debugging.

41. **Harness as thin API client eliminates parsing bugs.** Earlier harnesses tried to parse API responses, extract fields, and format output. This was the #1 source of build failures — every API has a different response structure. The thin client approach (send file, return raw response) means harness.py is simpler and the LLM judge handles response interpretation.

42. **LLM judge eliminates mechanical eval brittleness.** Mechanical eval (exact_match, format_compliance) produced false failures: "vendor_name" vs "Vendor Name", JSON keys in different order, extra whitespace. ALL criteria now go to one LLM judge that compares raw API response against Agent 3F ground truth. Raw response truncated at 15K chars. Uses `response.parsed_output` (not `.parsed`). No adaptive thinking on eval calls.

43. **Milestone messages prevent premature HARNESS_COMPLETE.** When the smoke test passes, a milestone message is injected telling the agent it must now run live API validation (Phase 3) before signaling HARNESS_COMPLETE. Without this, agents would signal completion after smoke test without ever calling the real API.

44. **Test files staged before build loop.** `_stage_test_files()` copies test files to the sandbox BEFORE the builder loop starts. Absolute paths are shown in the initial message. This lets the agent find and use real test files during Phase 3 live validation. Previously files were only staged during post-loop execution, so the agent had to download test files from the internet.

45. **run_code timeout must be 120s for async APIs.** Some APIs (Nanonets, Klippa) return a job ID and require polling. The original 30s timeout killed these calls before they completed. 120s accommodates: upload (5-10s) + processing (30-60s) + polling (10-30s).

### Key Files to Read (Agent 5)

1. `puzzleeval/schemas.py` — Agent5Input, TestHarness, FailedHarness, Agent5Result (after Agent 4 schemas)
2. `puzzleeval/agents/implement_test_env.py` — autonomous builder loop with verification gate (look for CORE LINE markers)
3. `puzzleeval/config.py` — AGENT5_* settings, PROVIDER_REGISTRY_PATH
4. `puzzleeval/provider_registry.py` — centralized API key management
5. `puzzleeval/agents/README_AGENT5.md` — complete walkthrough of Agent 5 design

### CLI Usage

```bash
# Full pipeline (Agent 1 → 2 → 4 → 3 → 5):
python -m puzzleeval.cli --text "I need invoice OCR" --agent5 --no-interactive --pretty

# Re-run Agent 5 only from a saved input (skips Agents 1-4, refreshes credentials):
python -m puzzleeval.cli --agent5-input runs/{trace-id}/agent_5_input.json --pretty

# Save output + logs:
python -m puzzleeval.cli --text "..." --agent5 --pretty > result.json 2> logs.jsonl
```

The `--agent5-input` flag loads a saved Agent 5 input file and re-runs ONLY Agent 5. It automatically refreshes credentials from the current `provider_registry.json` — not the stale credentials baked into the saved input.

When `--agent5` is set, the CLI runs Agent 1's conversation loop, then Agent 2→4 (research + screening) and Agent 3F (file-based test generation) in parallel, then loads the provider registry, then Agent 5 (harness building + LLM evaluation). The final JSON output is Agent5Result.

## What's Next

The original "build Agent 7 / 8 / 9" roadmap has been superseded. Agent 5
now produces per-candidate test results AND cross-candidate analysis; a
dedicated `puzzleeval/report.py` assembles the final `EvaluationReport`
with deterministic ranking, per-scope winners, evidence, and monthly cost
projection. The 9 Claude-Code-parity gaps documented in
`POST_ROADMAP_ENHANCEMENTS.md` §22 are the current open items (all are
wiring work, none architectural).

### How to Build a New Agent (Checklist)

1. **Add schemas** to `puzzleeval/schemas.py`
2. **Create agent** at `puzzleeval/agents/`
3. **Add validator** to `puzzleeval/validators.py`
4. **Add CLI flag** to `puzzleeval/cli.py`
5. **Write tests** at `tests/`
6. **Update CLAUDE.md** — document key design decisions
7. Run `ANTHROPIC_API_KEY=dummy python -m pytest tests/ -v` — all tests must pass (currently 816 + 39 generalizability deselected)


Cancellation propagation into Agent 5 builder loop: real fix requires threading a cancel_event through 4 nested function signatures + ~30 turn-loop iterations. Will land in a focused next pass — current state.cancel_requested works at agent boundaries which is most of the user-visible value.
Idempotency keys / DRY_RUN propagation / cleanup-after-write: requires Agent 5 builder prompt redesign + per-candidate teardown protocol. Real fix; not a bandaid candidate. Defer until you've actually run a real Stripe / Slack write workflow and felt the pain.
Agent 2 per-scope parallel research: real wall-clock win but requires restructuring research.py's single-call pattern. Defer until you have concrete latency complaints.
Streaming agent text + agent_thinking SSE: requires switching messages.create() to stream=True + delta extraction. Real UX win but ~1 day of focused work that's better tackled standalone.
AWS SigV4 / mTLS / WebSocket patterns in api_patterns.py: rare provider auth methods; defer until a real candidate needs them so we test against a real API contract.


Cancellation doesn't propagate into Agent 5's builder loop. The cancel button works at agent boundaries. In the middle of an 8-minute Agent 5 build, it's ignored. Fixing requires threading cancel_event through ~4 function signatures + ~30 loop iterations. I deferred this explicitly.
No idempotency keys on write operations. If Agent 5 retries a Stripe/Slack write on a 5xx, you could get duplicate records in the real provider's account.
No DRY_RUN propagation into harness generation. side_effects=creates_records scopes could leak test data into real accounts unless the candidate provider happens to have a sandbox URL the builder uses.
No sub-agent parallelism in Agent 2. For a 3-scope blueprint, research runs serially instead of 3 parallel calls. Wall-clock cost, not correctness.
No agent_thinking SSE streaming. Extended thinking blocks exist but aren't surfaced — you see spinners during Opus planning, not live reasoning.
No Agent 5 builder model fallback. I added call_with_model_fallback() as a reusable helper but only Agent 1 currently uses it (via parse_with_fallback). Agent 5 still hard-fails on persistent 429 at Opus 4.7.
Schema grammar size. The Agent1Result schema still sits near the compiled-grammar limit. The fallback path catches it, but every Opus call on Agent 1/2/3/4/5 pays the "try strict, fail, retry non-strict" tax. The real fix is splitting ScreenedCandidate into base + enrichment delta — deferred.

Stream Agent 5's messages.create() calls and emit agent_thinking deltas (~4 hours, biggest UX win)
Wire call_with_model_fallback into Agent 5 (~1 hour, biggest reliability win)
Thread cancel_event through _build_single_harness's 25-turn loop (~2 hours, real user-visible UX)
In-memory web_fetch cache in _verify_single_candidate (~1 hour, small $$ win)
Parallelize Agent 2 per-scope specialist searches (~3 hours, wall-clock win on multi-scope requests)
Emit ThinkingBlock content as SSE (~1 hour, lets users SEE reasoning during long phases)