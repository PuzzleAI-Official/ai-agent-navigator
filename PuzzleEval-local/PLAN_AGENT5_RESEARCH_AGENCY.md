# Plan v2: Build-Readiness as a First-Class Artifact

## Why v1 was wrong

V1 treated Agent 5's passivity as the root cause and proposed a five-phase prompt rewrite. That fixes a symptom, not the cause.

The root cause is that **the pipeline has no structured artifact that says "have we answered the questions a builder will need to ask?"** We substituted density scoring — a structural proxy (code fences, endpoints, auth headers, WebSocket mentions) — for the actual question. Two pages with identical density can differ wildly in usefulness: one might be dense with pricing tables and navigation, the other dense with request/response schemas. Density was a proxy for a proxy.

A second, deeper gap: no one in the pipeline systematically asks *"what's the full API surface of this provider, and is the endpoint we matched actually the best fit for the user's use case?"* Agent 2 finds candidates. Agent 4 verifies the specific endpoint it was handed. If ElevenLabs has both a TTS endpoint and a ConvAI WebSocket, and Agent 2 matches the wrong one, Agent 4 will happily verify the wrong endpoint. Agent 5 will build a harness that technically works but fails the use case. This is exactly what happened in trace `0c7f085f`.

V1 would not have caught either failure. V1 gave Agent 5 permission to research but didn't give Agent 4 the obligation to verify the right thing or to produce a handoff artifact that Agent 5 could reason over.

## The correct abstraction

Replace density scoring with a **build-readiness checklist** — a small, enumerable Pydantic artifact that flows Agent 2 → Agent 4 → Agent 5, with each stage populating its share and each stage's stopping criterion defined as *"is my portion of the checklist populated to the required bar?"*

The checklist has two sections:

**Section 1 — Provider capability surface.** Answers *"what can this provider do that's potentially relevant to the user's use case?"* Populated by Agent 4 (not Agent 2 — Agent 2's breadth is for candidate discovery, which is wider).

**Section 2 — Build-readiness fields.** Answers *"does the builder have what it needs to write a working harness?"* Populated by Agent 4 to the best of its ability from docs, then closed by Agent 5 during its Phase B/C.

Every build-readiness field has three possible states — `confirmed` (answered from authoritative docs with a source URL), `inferred` (Agent 4's best guess from available context, with reasoning), `unknown` (explicitly flagged, with a reason why the docs didn't answer). State is a first-class citizen, because *knowing what you don't know* is half the value.

---

## Schema

Added to `puzzleeval/schemas.py`:

```python
class EndpointSummary(BaseModel):
    name: str                           # e.g. "POST /v1/audio/speech"
    purpose: str                        # one sentence
    relevance_to_use_case: Literal["primary", "alternative", "unrelated"]
    selection_note: str | None = None   # "chosen because X" or "rejected because Y"


class FieldStatus(BaseModel):
    status: Literal["confirmed", "inferred", "unknown"]
    value: str | None = None            # the actual content when known
    source_url: str | None = None       # authoritative doc URL when confirmed
    reasoning: str | None = None        # why inferred, why unknown, or note on source


class BuildReadinessChecklist(BaseModel):
    # --- Provider capability surface (Agent 4 fills) ---
    provider_surface: list[EndpointSummary]
    selected_endpoint: str
    selection_justification: str        # why THIS endpoint vs alternatives in provider_surface

    # --- Build-readiness fields (Agent 4 fills to best-effort; Agent 5 closes gaps) ---
    endpoint_path: FieldStatus
    auth_method: FieldStatus            # header vs body vs OAuth flow
    auth_refresh: FieldStatus           # how to refresh/rotate if test runs long
    request_body_shape: FieldStatus     # JSON skeleton of a valid request
    response_body_shape: FieldStatus    # JSON skeleton of a success response
    error_response_schema: FieldStatus  # at minimum the common 4xx shape
    rate_limit_signal: FieldStatus      # header name, retry-after format
    async_pattern: FieldStatus          # "sync" | "polling" | "streaming" | "webhook" | "unknown" + protocol details
    content_type_quirks: FieldStatus    # multipart, form, SSE, binary, etc.
    sandbox_availability: FieldStatus   # sandbox URL if side-effects require it

    # --- Meta ---
    populated_by: Literal["agent_4", "agent_5", "agent_5_after_research"]
    last_updated_at: datetime
```

Attached to `ScreenedCandidate`:

```python
class ScreenedCandidate(BaseModel):
    # ... existing fields ...
    checklist: BuildReadinessChecklist | None = None   # populated by Agent 4
```

Ten build-readiness fields. Not arbitrary — each corresponds to a concrete question a harness author must answer to write working code:

| Field | Question it answers |
|---|---|
| `endpoint_path` | Where do I POST? |
| `auth_method` | How do I authenticate the request? |
| `auth_refresh` | How do I stay authenticated if the test runs long? |
| `request_body_shape` | What JSON do I send? |
| `response_body_shape` | How do I extract the output? |
| `error_response_schema` | Which errors retry vs fail? |
| `rate_limit_signal` | How do I know I'm being throttled? |
| `async_pattern` | Do I poll, stream, or wait for webhook? |
| `content_type_quirks` | Is it JSON, multipart, binary, SSE? |
| `sandbox_availability` | Can I test without side-effects? |

If you can fill these, you can build. If you can't, you can't. This is the actual contract between research and build.

---

## Stage-by-stage

### Agent 2 (`research.py`)

**Unchanged.** Agent 2's role is candidate discovery — wide net, short claim per candidate. It does not fill the checklist. Its existing `Candidate` objects remain the input to Agent 4.

**Rationale:** the user said "research agent knows what provider can do" — that knowledge lives in Agent 4 (deep-verify), not Agent 2. Agent 2 is keyword-matched breadth. Making it fill the checklist would either inflate its cost or produce shallow checklist data that Agent 4 has to redo anyway.

### Agent 4 (`screening.py` + `deep_verify_prompt.py`)

**Two new obligations:**

1. **Survey the provider's relevant API surface.** Before confirming the matched endpoint, list the provider's other endpoints that plausibly relate to the user's use case, and justify the selection. This is the "all kinds of APIs they provide" the user asked about. Output populates `provider_surface` and `selection_justification`.

2. **Populate the build-readiness checklist.** For each of the ten fields, find the answer in docs. Mark `confirmed` with source URL when found in authoritative docs. Mark `inferred` with reasoning when the docs hint but don't state explicitly. Mark `unknown` with a reason when the docs simply don't cover it.

**Three-state outcome for each Agent 4 candidate (per Q4 — we never reject for system failure):**

State 1 — **Verified Pass**: Agent 4 ran cleanly, four non-negotiables `confirmed`, `provider_surface` populated, `selection_justification` written. → Pass to Agent 5 with full checklist.

State 2 — **Verified Reject**: Agent 4 ran cleanly AND found definitive evidence the candidate is bad: docs explicitly say "no public API", endpoint returns documented 404, provider has no relevant API for the use case, deprecated with no replacement, etc. → Reject with category + reason. Don't waste Agent 5 time.

State 3 — **Inconclusive**: Agent 4 ran cleanly but couldn't confirm non-negotiables from public docs (paywalled docs, sparse documentation, ambiguous spec) AND found no evidence the candidate is bad. → Pass to Agent 5 with checklist showing fields as `unknown` or `inferred`. Agent 5 attempts to confirm via its own research. If Agent 5 also can't confirm, the test will fail at runtime — and runtime failure is more diagnostic than premature rejection.

State 4 — **System Failure**: Agent 4 crashed, parser failed on malformed JSON, network timeout, rate limit. → NEVER reject the candidate. Pass through with sentinel checklist (all fields `unknown`, `populated_by="system_failure"`). Agent 5 falls back to full research mode. The candidate's eventual fate is determined by Agent 5's runtime test result, not by our infrastructure failure.

**Principle:** rejection is reserved for evidence of badness, not for absence of evidence. The runtime test is the final arbiter for State 3 and State 4.

**Prompt change to `deep_verify_prompt.py`:** add a structured output section that mirrors `BuildReadinessChecklist`. Claude returns the checklist as a JSON block at the end of its analysis. Parser wraps with Pydantic validation and attaches to `ScreenedCandidate.checklist`.

**Size impact:** Agent 4's system prompt grows by ~60 lines (the checklist template + instructions for the three statuses + provider-surface instruction). Agent 4's output grows by ~200 tokens of structured JSON. Acceptable.

### Agent 5 (`implement_test_env.py`)

**Five-phase flow, but now the phases are driven by the checklist, not by density tiers:**

```
PHASE A — Inherit (read the checklist Agent 4 built)
  Read `candidate.checklist`. This is your starting context. It already
  enumerates what's known and what's unknown.
  Also read the inlined source docs; they back the `confirmed` fields with
  URL pointers you can cross-check.

PHASE B — Gap analysis for THIS test case (NOT for every test case)
  Most fields are CONDITIONAL. Apply these triggers to decide what's
  actually needed for the harness you're about to write:

    ALWAYS required (the four non-negotiables):
      endpoint_path, auth_method, request_body_shape, response_body_shape

    Required IF the test case exercises retry / failure paths:
      error_response_schema, rate_limit_signal

    Required IF the test session is long-running (>10 min):
      auth_refresh

    Required IF the API is async or streaming:
      async_pattern (with protocol details)

    Required IF the request uses non-standard content types:
      content_type_quirks (multipart, SSE, binary)

    Required IF the candidate has side_effects=creates_records:
      sandbox_availability

  Output: the list of fields that are (a) not `confirmed` and (b)
  triggered for this test case by the rules above. Empty list is
  acceptable and common — small read-only sync calls only need the
  four non-negotiables, which Agent 4 already confirmed.

PHASE C — Targeted research (fill ONLY the fields Phase B named)
  For each gap from Phase B:
    • Call `web_fetch` when you have a specific URL likely to answer.
    • Call `ask_research` when you need delegation. Use this template:
         CANDIDATE: <name>
         ENDPOINT: <from checklist.selected_endpoint>
         KNOWN: <summarize what Agent 4 already confirmed>
         FIELD NEEDED: <one of the 10 build-readiness fields>
         WHY: <how the answer changes the harness>
    • Update the checklist entry in-place: `unknown` → `confirmed` with
      source URL, or at least `unknown` → `inferred` with explicit reasoning.
  Soft research budget: aim for at most 2 research calls per gap. If a
  third is needed, name why before making it. If a fourth would be
  needed, commit to your best understanding and proceed — record the
  residual uncertainty in the spec.
  Stop test:
    "Is every field in Phase B's list now `confirmed`, OR `inferred` with
     reasoning I'm willing to stand behind in the harness spec? AND have
     I left genuinely irrelevant unknowns (e.g., `auth_refresh` for a
     2-second test) untouched, instead of researching them out of habit?"
  When yes, proceed. The spec can ship with `unknown` fields that don't
  matter — that's the whole point of per-test-case relevance.

PHASE D — Write the spec (commit to a contract)
  Emit the checklist + a code-ready spec as a comment block at the top of
  harness.py:
    • Copy the 10 fields with their final status
    • Add a "Harness plan" paragraph translating confirmed fields into code
      structure: request builder, response parser, error handler, async loop
  If you cannot write the Harness plan without a TODO or a guess, the
  checklist is not done. Return to Phase C.

PHASE E — Build
  Implement harness.py from the spec. The spec is the contract; deviations
  update the spec comment first.
```

**Critical:** the stop test in Phase C and Phase D are **falsifiable** — "can I name each field as confirmed-or-inferred-with-standing-reasoning?" not "do I feel ready?" The model can apply it to itself deterministically.

**Agent 5's `ask_research` calls are now automatically direction-pointing.** The checklist context IS the direction. The agent can't call `ask_research` with "tell me about ElevenLabs" because the template requires `FIELD NEEDED`, which is one of ten enumerable items.

---

## What gets deleted

- `puzzleeval/web_doc_cache.py::doc_density_score` — gone.
- `puzzleeval/web_doc_cache.py::classify_doc_density` — gone.
- Density tier constants (`DENSITY_HIGH`, `DENSITY_MEDIUM`) — gone.
- HIGH/MEDIUM/THIN tagging in `_format_prefetched_docs_block` — gone.
- Case A/B/C branching in the injection block — gone.
- "skip to STEP 2" language — gone.
- "you do NOT need to `read_file` or `web_fetch`" — gone.
- ~150 LoC of density scoring, ranking, and tier-branching.
- Three of the 19 tests in `test_density_aware_prefetch_injection.py` that pinned Case A/B/C behavior — gone. Remaining 16 adapted to test doc-listing order (the ranking survives as an order, not as a gate).

## What stays

- `candidate_sandbox_dir`, `save_web_fetches_to_sandbox`, `count_existing_fetched_docs` — the doc handoff mechanism is orthogonal to the checklist. Still valuable.
- `web_fetch_fallback.py` — unchanged.
- All of Agent 2's discovery logic — unchanged.
- Rate limiting, selection, scope routing — unchanged.
- Agent 4's existing verification (pricing, endpoint reachability) — unchanged, checklist is additive.

## What becomes simpler

Agent 5's Phase 1 prompt goes from:

> *Here are density-ranked files. HIGH density → skip to STEP 2. MEDIUM → inventory then decide. THIN → search first. You do NOT need to `read_file` or `web_fetch`...*

To:

> *Here is the checklist Agent 4 populated for this candidate. Ten fields, each with confirmed/inferred/unknown. Your job: for this specific test case, identify which unknowns block the code path, fill them via targeted research, then write the spec and build.*

Two sentences. One artifact. Unambiguous.

---

## Stopping criteria — table

| Stage | Stopping criterion (explicit, falsifiable) |
|---|---|
| Agent 2 | N candidates found with keyword + basic capability claim (existing; unchanged) |
| Agent 4 | One of three outcomes per candidate: **Verified Pass** (non-negotiables confirmed + surface + justification), **Verified Reject** (definitive bad evidence), or **Inconclusive** (passes through to Agent 5 as State 3). Never State 4 → Reject. |
| Agent 5 Phase B | Per-test-case gap list enumerated using the trigger rules (most lists are empty or 1-2 items) |
| Agent 5 Phase C | Every Phase-B-named field is `confirmed` or `inferred` with standing reasoning. Irrelevant `unknown` fields are deliberately left unresearched. |
| Agent 5 Phase D | Harness plan writable without TODO / guess / "might need to" |
| Agent 5 Phase B | Gap list enumerated (may be empty) |
| Agent 5 Phase C | Every Phase-B-named field is `confirmed` or `inferred` with standing reasoning |
| Agent 5 Phase D | Harness plan writable without TODO / guess / "might need to" |

Each criterion is a boolean, not a judgment call.

---

## Answering the user's three questions, directly

1. *"Does research agent know if we get enough information to know what the api provider can do that are potentially related to our use case (all kinds of apis they provide)?"*
   **Yes.** `provider_surface` enumerates relevant endpoints with purpose + relevance tag. Agent 4's stopping criterion requires at least one `"primary"` entry and a `selection_justification`. If Agent 4 can't write either, it can't pass the candidate.

2. *"Enough information to feed to builder to build the harness?"*
   **Yes.** Ten named build-readiness fields corresponding to the ten questions a harness author must answer. Agent 4's non-negotiables guarantee the four critical ones are `confirmed` before handoff. The other six are explicit about their state — no silent gaps.

3. *"Know when research is enough to stop and write the api spec and then build?"*
   **Yes at Agent 4:** non-negotiables + all-fields-have-status.
   **Yes at Agent 5:** Phase-B-named gaps closed + harness-plan writable without guesses.
   Both criteria are boolean and self-applied.

---

## Migration — concrete steps

### Step 1 — Schema
Add `EndpointSummary`, `FieldStatus`, `BuildReadinessChecklist` to `puzzleeval/schemas.py`. Add `checklist` field to `ScreenedCandidate`. Pydantic validation ensures structural correctness.

### Step 2 — Agent 4 prompt + parser
Extend `deep_verify_prompt.py`:
- Add `PROVIDER_SURFACE_SECTION` with instruction + example
- Add `BUILD_READINESS_CHECKLIST_SECTION` with the 10 fields, 3 statuses, and a worked example showing all three statuses in use
- Add final-output JSON schema requirement

Update `screening.py::_verify_single_candidate` to:
- Parse the structured JSON from Claude's response
- Pydantic-validate into `BuildReadinessChecklist`
- Attach to `ScreenedCandidate.checklist`
- On parse failure: log, attach a sentinel checklist with all fields `unknown` and `populated_by="agent_4"`, continue (do not reject the candidate on parse failure alone; Agent 5 can still handle it as a worst-case "Agent 4 produced nothing useful" scenario).

### Step 3 — Agent 5 prompt rewrite
Rewrite Phase 1 system prompt in `implement_test_env.py`:
- Replace density-tier language with checklist-driven five phases
- Remove Case A/B/C branching in `_format_prefetched_docs_block`
- Keep the doc-listing block as "here's what Agent 4 fetched, in ranking order" — rank is preserved as a triage hint only
- Add the `ask_research` template verbatim

Phase D output: harness.py header comment block with the 10 fields + harness plan paragraph.

### Step 4 — Delete density scoring
Remove `doc_density_score`, `classify_doc_density`, tier constants, tier-branching logic.

### Step 5 — Tests
Add `tests/test_build_readiness_checklist.py` (~25 cases):
- Schema round-trip for all three `FieldStatus` states
- `BuildReadinessChecklist` serializes and validates
- Agent 4 prompt includes checklist template
- Agent 4 parser recovers checklist from well-formed JSON
- Agent 4 parser handles malformed JSON gracefully (sentinel checklist)
- Agent 4 stopping criterion: non-negotiables confirmed
- Agent 5 Phase B gap identification (unknowns filtered by test-case relevance)
- Agent 5 Phase C stop test (falsifiable)
- Agent 5 Phase D harness-plan writability test
- `ask_research` template enforced in Phase C output
- Harness.py header comment contains all 10 fields

Delete most of `tests/test_density_aware_prefetch_injection.py`; keep 4-5 tests for the ranking-as-order behavior. Adapt `tests/test_agent4_doc_handoff.py` to also assert checklist attachment.

Update end-to-end mock pipeline tests to assert `candidate.checklist is not None` after Agent 4.

### Step 6 — Backward compatibility
`ScreenedCandidate.checklist` is optional (`| None = None`). Existing cached candidates without a checklist still load. Agent 5 treats `checklist=None` as "Agent 4 didn't run" and falls back to full research mode — which is the current behavior, so nothing regresses for cached data.

---

## Failure mode analysis

| Failure mode | Behavior |
|---|---|
| Agent 4 returns malformed JSON for checklist | Parser logs, attaches sentinel (all `unknown`), Agent 5 handles as full-research case |
| Agent 4 marks field `confirmed` but Agent 5 finds source URL says otherwise | Agent 5 flips status to `inferred` with note "Agent 4 source did not actually confirm"; proceeds with own research |
| Agent 5 cannot resolve an `unknown` via `web_fetch`/`ask_research` | Phase D spec comment records the residual uncertainty; harness uses best-inferred shape; test result will expose if the inference was wrong |
| Provider surface only has one endpoint, no alternatives | Fine. `selection_justification` states "only relevant endpoint this provider offers" |
| User's use case spans multiple workflow steps, each with different candidates | Each candidate has its own checklist. No cross-checklist coupling needed |
| Cached run (no Agent 4 execution) | `checklist=None`; Agent 5 falls back to current behavior |
| Agent 5 calls `ask_research` with a malformed template (missing FIELD NEEDED) | Instructional-level enforcement only; we don't add runtime validation. If it happens, the research subagent returns generic, user sees lower-quality research — acceptable degradation |

---

## Cost impact

| Line item | Direction |
|---|---|
| Agent 4 prompt | +60 lines ≈ +$0.002 per candidate at sonnet pricing (prompt-cached after first call) |
| Agent 4 output | +200 tokens ≈ +$0.003 per candidate |
| Agent 5 prompt | Slightly smaller (density branching deleted, checklist injected) — net roughly flat |
| Agent 5 research calls | Likely DOWN — Phase B explicitly filters to relevant unknowns, whereas current Agent 5 often re-researches what's already covered |
| Total per candidate | Net: roughly flat to slightly down |

Biggest cost saving is behavioral: when Agent 5 has a checklist that says `auth_method=confirmed`, it doesn't waste a `web_fetch` re-confirming it.

---

## Files touched

| File | Change |
|---|---|
| `puzzleeval/schemas.py` | +3 models: `EndpointSummary`, `FieldStatus`, `BuildReadinessChecklist`; `ScreenedCandidate.checklist` field |
| `puzzleeval/deep_verify_prompt.py` | +provider-surface section, +checklist section, +JSON output requirement |
| `puzzleeval/agents/screening.py` | Parse checklist JSON; attach to `ScreenedCandidate`; sentinel on parse failure |
| `puzzleeval/agents/implement_test_env.py` | Phase 1 prompt rewrite around checklist; delete density branching; harness.py header comment; `ask_research` template |
| `puzzleeval/web_doc_cache.py` | Delete `doc_density_score`, `classify_doc_density`, tier constants. Keep handoff primitives |
| `tests/test_build_readiness_checklist.py` | NEW — 25 cases |
| `tests/test_agent4_doc_handoff.py` | Add checklist-attachment assertions |
| `tests/test_density_aware_prefetch_injection.py` | Delete ~13 cases; keep ~6 ranking-as-order cases; rename to `test_agent4_doc_injection_order.py` |
| `tests/test_agent5_research_agency.py` | NEW — 15 cases covering five-phase flow and `ask_research` template |
| `tests/test_phase6_5_and_7.py` | +3 cases for provider-surface + checklist in Agent 4 prompt |

Net: +1 new file, ~200 LoC added (schema + prompt + parser), ~150 LoC deleted (density scoring), +40 tests, ~13 tests deleted/renamed. **Net smaller code surface than current.**

---

## Out of scope

- Agent 2 changes — its breadth role is unaffected.
- Frontend — the checklist can surface in UI later if useful; not this pass.
- Rate limiting, pricing, selection — untouched.
- Schema migration for already-running cached data — checklist is optional, so no migration needed.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Agent 4's structured output parsing fails for some providers | Sentinel checklist on parse failure, Agent 5 handles it as "full-research" mode. Non-blocking |
| Agent 4 populates checklist but hallucinations sneak into `confirmed` fields | `source_url` is required for `confirmed`; Agent 5 Phase A cross-checks against fetched docs; spot-detect mismatches flip status to `inferred` |
| Ten fields is too few (misses some build need) | Start with ten. If a common unmet need emerges from real runs, add an eleventh field. Schema supports additive change |
| Ten fields is too many (overkill) | Four are non-negotiable, six are explicitly allowed `unknown`. The long tail costs ~60 extra prompt lines once, cached. Minimal |
| Agent 5 disputes Agent 4's `confirmed` field | Status flip to `inferred` with "disputed" note; proceeds. Recorded in harness.py spec comment for post-hoc review |
| Provider surface survey drives Agent 4 cost up for providers with large API catalogs | Cap `provider_surface` at 5 entries; model picks the 5 most relevant to user's use case |

---

## Why this is actually the optimal shape

1. **Enumerable contract.** The ten build-readiness fields are a *complete* list of what a harness author needs. Claims this are strong — but every harness-failure-mode I can think of maps to one of these ten. If a real failure emerges that doesn't, we add a field. The list is empirically refinable.

2. **Status is first-class.** "We don't know X" is a legitimate, recorded answer. Current pipeline treats unknowns as silent gaps. Explicit unknowns are actionable; silent gaps produce mystery test failures.

3. **Each stage's job is bounded.** Agent 4 fills what it can from docs; Agent 5 fills the rest via targeted research. Neither can pretend the other did its job. No silent accumulation of tech debt.

4. **Stopping criteria are falsifiable.** "Are the fields populated?" is boolean. "Is this dense enough?" is a judgment call on a proxy measurement. Booleans scale; proxies don't.

5. **Less code.** The checklist is ~60 lines of Pydantic + ~200 tokens of output. It replaces ~150 LoC of density scoring, tier classification, and branching instructional prompts. Correctly engineered is smaller than over-engineered.

6. **Directly answers the three questions.** Provider-surface for breadth; build-readiness for depth; status enumeration for stopping.

7. **Debuggable.** After a failed real run, you open `harness.py`, read the header comment, and see exactly which fields were `confirmed` vs `inferred` vs `unknown`. If the test failed because of an `inferred` field, you know where to look. Current pipeline after a failure leaves you reading density scores.

---

## Decisions resolved (from sign-off pass)

### Q1 — Ten fields, NOT over-constraining

The ten fields are the complete enumeration of what a harness author needs. They are NOT a stop bar for Agent 5 — Agent 5 only needs the **subset relevant to the test case it's about to run**.

When Agent 5 stops doing research:
1. The four non-negotiables (`endpoint_path`, `auth_method`, `request_body_shape`, `response_body_shape`) are already `confirmed` by Agent 4 before handoff.
2. Agent 5 applies Phase B trigger rules to identify which of the OTHER six fields actually matter for THIS test case (often zero — a 2-second sync read needs none of them).
3. Agent 5 researches only the triggered subset and stops when each is `confirmed` or `inferred-with-standing-reasoning`.
4. Stop test: *"Can I write the harness's request-builder, response-parser, and error-handler without TODO, without guessing, without 'might need to'?"*

A typical small read-only test triggers zero additional fields → Agent 5 does **zero research calls**, writes the spec from Agent 4's checklist, and builds. A test exercising retry behavior on an async streaming endpoint triggers `error_response_schema` + `rate_limit_signal` + `async_pattern` → Agent 5 may make 2-3 targeted research calls.

This is not over-constraining. It's per-test-case relevance: most fields stay `unknown` forever and that's fine because the harness doesn't use them.

### Q2 — Four non-negotiables, agreed

`endpoint_path`, `auth_method`, `request_body_shape`, `response_body_shape`. These guarantee at least one successful API call is possible. Without any of them, the harness can't even attempt a request. Locked in.

### Q3 — `ask_research` template: hybrid (instructional + soft logging validator)

How the two options work differently:

**Pure instructional**
- Template is in the prompt. Agent 5 follows it as a soft norm.
- Pros: simple (zero new code), model-flexible (Opus can adapt template fields if a gap doesn't fit cleanly), easy to evolve wording, no false-rejections of good calls with slightly different format.
- Cons: no telemetry on adherence, can drift silently over time, harder to debug "why was the research vague?"

**Pure runtime validation**
- A wrapper around `ask_research` parses the input and rejects if template fields are missing.
- Pros: forces compliance, gives adherence telemetry, catches malformed calls before they go to the research subagent.
- Cons: brittle (Opus uses slightly different field names → false reject), adds retry logic + latency, more code to maintain, can fail open or closed (both add complexity).

**Hybrid (recommended)**
- Template is in the prompt (instructional default).
- Wrapper LOGS template adherence as warnings but does NOT reject.
- We get telemetry without rigidity. If we see drift in production logs, we tighten the prompt or escalate to validation later.
- Cost: ~20 LoC for the soft validator + a logging counter. No retry logic, no rejection path.

For an Opus 4 system that follows structured templates well, hard validation is overkill. Hybrid gives us the visibility to know if it ever stops working without paying the brittleness tax today.

**Decision: Hybrid.** Template in prompt + soft logging-only validator that tracks adherence rate.

### Q4 — Rejection: three valid reject states, never reject for system failure

The full breakdown is in the "Three-state outcome" section above. Summary:

- **Verified Pass** → pass to Agent 5 with full checklist
- **Verified Reject** → reject (only when Agent 4 found definitive evidence the candidate is bad)
- **Inconclusive** → pass to Agent 5 with `unknown` fields; runtime test is final arbiter
- **System Failure** → NEVER reject; pass through with sentinel checklist; Agent 5 does full research; runtime test is final arbiter

The principle: rejection is for evidence of badness. Absence of evidence → let the candidate run; the runtime test is more diagnostic than premature rejection.

### Q5 — Density: delete entirely, no remaining use

The density score is currently used in exactly one place: ranking Agent 4's saved fetches for Agent 5's injection. Under the checklist design:
- Agent 4 has already extracted the answers into the checklist with source URL pointers.
- Agent 5 reads the checklist, not the raw doc dump.
- When Agent 5 needs to confirm a field, it follows the source URL directly — no ranking needed.

So density truly has no remaining consumer. **Delete.** This includes `doc_density_score`, `classify_doc_density`, tier constants, and the `[INLINED]/[FETCHED]/[THIN]` tagging.

**Your deeper question: do we have a good mechanism to tell our agent when to search and when to fetch?**

Yes — the checklist itself IS that mechanism, applied differently at each stage:

- **Agent 2** (discovery): web_search to find candidates. No fetch — too expensive, too early.
- **Agent 4** (verification): web_search to locate authoritative docs, web_fetch to confirm checklist fields. Each field corresponds to a specific question; fetch is justified when the question can't be answered from search snippets. Stops when checklist non-negotiables are `confirmed`.
- **Agent 5** (build): web_search rarely (only for completely unknown territory). web_fetch when a specific URL is likely to answer a triggered Phase B gap. ask_research for delegated investigation. Stops when the spec is writable without guessing.

Search and fetch are not philosophical choices — they map to "do I need to find a URL?" (search) vs "do I need to read a specific URL's content?" (fetch). The checklist makes the trigger explicit at each stage.

---

## Speed and quality assessment

**Per-candidate Agent 4 cost:** slightly up — +60 prompt lines (cached after first call, ~$0.002 per candidate amortized) + ~200 tokens of structured JSON output (~$0.003). Net: +$0.005/candidate. Negligible.

**Per-candidate Agent 5 cost:** likely down — Phase B's trigger rules eliminate the "research everything just in case" tendency the current density-injection prompts have. Most test cases trigger zero or one additional field, vs current behavior where Agent 5 often re-confirms what's already in the dense block. Estimated savings: 1-3 fewer `web_fetch` / `ask_research` calls per candidate on average.

**Wall-clock per candidate:** roughly flat to slightly faster. Agent 4 spends marginally more time producing structured output; Agent 5 spends substantially less time re-researching what Agent 4 covered. Net: -10% to flat, candidate-dependent.

**Quality:** substantially better, in three concrete ways:
1. **Right endpoint chosen.** `provider_surface` + `selection_justification` catches the "wrong endpoint matched for the use case" failure mode (the `0c7f085f` ElevenLabs TTS-vs-ConvAI bug). Today: silent. Future: explicit.
2. **No silent gaps.** Every field has a status. `unknown` is recorded with reasoning, not silently absent. Mystery test failures become diagnosable from the spec comment alone.
3. **Per-test-case research focus.** Agent 5 stops over-researching irrelevant fields (`auth_refresh` for a 2-second test) and starts under-researching less (Phase B trigger rules force confrontation with what's actually needed).

**Net answer to "slower and better, or faster and better":** **Faster on average and substantially better in quality.** The quality wins dominate; the speed delta is small-and-positive on most runs because Agent 5 stops doing redundant work that the density-injection prompts implicitly encouraged.

The one cost we knowingly take: Agent 4's prompt grows by ~60 lines. Cached after the first call per session. Real cost: ~$0.002 amortized per candidate. Trivial.

---

## Implementation order

1. Schema additions (`EndpointSummary`, `FieldStatus`, `BuildReadinessChecklist`, `ScreenedCandidate.checklist`)
2. Agent 4: extend `deep_verify_prompt.py` with provider-surface + checklist sections; extend `screening.py` parser; implement three-state outcome logic
3. Agent 5: rewrite Phase 1 prompt with five phases + per-test-case trigger rules + `ask_research` template; emit harness.py spec header
4. Soft `ask_research` template adherence logger (~20 LoC, hybrid per Q3)
5. Delete density scoring (`doc_density_score`, `classify_doc_density`, tier constants, A/B/C branching)
6. Tests: 25 new for checklist, 15 for Agent 5 five-phase flow, ~6 surviving density-as-order tests renamed/relocated
7. End-to-end mock verification: drive a single-candidate mock through Agent 4 → 5, confirm checklist round-trips and per-test-case relevance fires correctly
8. End-to-end real verification: one well-documented candidate (OpenAI Chat) and one less-documented candidate to exercise both `confirmed`-heavy and `unknown`-heavy paths

Confirm and I start at step 1.
