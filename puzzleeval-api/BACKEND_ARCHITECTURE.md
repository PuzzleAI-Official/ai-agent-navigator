# PuzzleEval API Backend — Complete Architecture Guide

**Purpose:** A comprehensive guide to how the FastAPI backend wraps PuzzleEval's CLI pipeline into a web API that streams real-time progress to the frontend.

**Audience:** Future Claude sessions, future developers, anyone who needs to understand the system without spelunking through 1000+ lines of code.

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [File Structure](#2-file-structure)
3. [Request Flow — What Happens When User Sends a Message](#3-request-flow)
4. [Pipeline Orchestration](#4-pipeline-orchestration)
5. [SSE Event System](#5-sse-event-system)
6. [State Management (RunState)](#6-state-management)
7. [Mock vs Real Mode](#7-mock-vs-real-mode)
8. [Agent Input Construction](#8-agent-input-construction)
9. [Credential Handling](#9-credential-handling)
10. [File Upload Flow](#10-file-upload-flow)
11. [CWD Management for Agent 5](#11-cwd-management)
12. [Progress Callbacks in Agent 5](#12-progress-callbacks)
13. [Observability / Debug Artifacts](#13-observability)
14. [Known Gotchas and Design Decisions](#14-gotchas)
15. [Testing Strategy](#15-testing)
16. [Future Work](#16-future-work)

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       REACT FRONTEND (:8081)                     │
│                                                                   │
│  ┌────────────────┐      ┌─────────────────┐                    │
│  │ Playground.tsx │◄─────┤ usePipelineRun  │                    │
│  │   (UI)         │      │   (hook)         │                    │
│  └────────────────┘      └────────┬────────┘                    │
│                                    │                              │
│                          ┌─────────▼─────────┐                   │
│                          │  services/api.ts  │                   │
│                          │  (HTTP + SSE)     │                   │
│                          └─────────┬─────────┘                   │
└────────────────────────────────────┼─────────────────────────────┘
                                     │ /pzapi/* (proxied by Vite)
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                     FASTAPI BACKEND (:8001)                      │
│                                                                   │
│  ┌───────────────────────────────────────────────────────┐      │
│  │  main.py — loads .env, sets paths, mounts routes      │      │
│  └───────────────────────────────────────────────────────┘      │
│                                                                   │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌───────────┐ │
│  │  /runs     │  │  /chat     │  │  /files    │  │  /events  │ │
│  │  (CRUD)    │  │  (Agent 1) │  │  (uploads) │  │  (SSE)    │ │
│  └────────────┘  └────────────┘  └────────────┘  └───────────┘ │
│                                                                   │
│  ┌───────────────────────────────────────────────────────┐      │
│  │  services/                                             │      │
│  │    - run_manager.py   (in-memory RunState)             │      │
│  │    - event_bus.py     (thread-safe SSE queue)          │      │
│  │    - pipeline_runner.py (orchestrates agents)          │      │
│  └───────────────────────────────────────────────────────┘      │
└────────────────────────────────────┼─────────────────────────────┘
                                     │ imports
                                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                  PUZZLEEVAL PACKAGE (read-only)                  │
│                                                                   │
│  agents/                    schemas.py       provider_registry   │
│  ├── user_understanding.py  (Pydantic)      .json                │
│  ├── research.py                                                  │
│  ├── screening.py                                                 │
│  ├── synthetic_tests*.py                                         │
│  └── implement_test_env.py  (the big one — 3500 lines)           │
└─────────────────────────────────────────────────────────────────┘
```

**Key design principle:** The API is a THIN WRAPPER around PuzzleEval's agent functions. It doesn't reimplement agent logic — it:
1. Receives HTTP requests
2. Constructs agent inputs (Pydantic models)
3. Calls agent functions via `asyncio.to_thread()` (they're synchronous)
4. Emits SSE events as agents progress
5. Saves results for debugging

---

## 2. File Structure

```
puzzleeval-api/
├── .env                          # ANTHROPIC_API_KEY + provider registry path
├── main.py                       # FastAPI app entry point
├── requirements.txt              # fastapi, uvicorn, sse-starlette, dotenv
├── uploads/{run_id}/             # User-uploaded files (created at runtime)
├── runs/{trace_id}/              # Debug artifacts per pipeline run
│   ├── agent_1_conversation.json
│   ├── agent_1_output.json
│   ├── agent_2_output.json
│   ├── agent_3_output.json
│   ├── agent_4_output.json
│   ├── agent_5_output.json
│   ├── pipeline_summary.json
│   └── harnesses/{slug}/         # Agent 5 sandbox dirs
│       ├── harness.py
│       ├── requirements.txt
│       ├── .venv/
│       └── conversation_log.json
│
├── models/
│   └── api_models.py             # Pydantic request/response models
│
├── routes/
│   ├── runs.py                   # POST /runs, GET /runs/{id}, DELETE /runs/{id}
│   ├── chat.py                   # POST /runs/{id}/chat (Agent 1)
│   ├── files.py                  # POST /runs/{id}/files
│   └── events.py                 # GET /runs/{id}/events (SSE stream)
│
└── services/
    ├── run_manager.py            # RunState dataclass + in-memory store
    ├── event_bus.py              # Thread-safe queue.Queue for SSE
    └── pipeline_runner.py        # Heart of the system (600+ lines)
```

---

## 3. Request Flow

### Scenario: User types a message and uploads 2 PDFs

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. Frontend: handleSend(text, [file1, file2])                   │
│    (in usePipelineRun.ts)                                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. POST /api/runs  { text, agent_modes }                         │
│    → run_manager.create_run() creates RunState                   │
│    → Returns { run_id, trace_id }                                │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. POST /api/runs/{run_id}/files  (multipart)                    │
│    → files.py saves bytes to uploads/{run_id}/                   │
│    → Stores absolute paths in state.uploaded_files[]             │
│    → Returns { file_ids }                                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. POST /api/runs/{run_id}/chat  { message, file_ids }           │
│    → chat.py calls real_agent1_turn() or mock_agent1_turn()      │
│    → Agent 1 decides: is_clear=True or False                     │
│    → If True: spawn background task run_pipeline(state)          │
│    → Returns { is_clear, assistant_message, pipeline_started }   │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 5. GET /api/runs/{run_id}/events  (SSE stream, long-lived)       │
│    → events.py subscribes to state.event_bus                     │
│    → Yields events as they arrive from pipeline_runner           │
│    → Frontend updates UI in real-time                            │
└─────────────────────────────────────────────────────────────────┘
```

**Critical timing:** File upload happens BEFORE the chat message is sent. This way, when Agent 1 receives the message, the files are already saved to disk with absolute paths in `state.uploaded_files`.

---

## 4. Pipeline Orchestration

The `run_pipeline()` function in `services/pipeline_runner.py` orchestrates all agents. Here's the exact flow:

```
┌─────────────────────────────────────────────────────────────────┐
│ run_pipeline(state: RunState)                                    │
│                                                                   │
│ 1. Setup:                                                         │
│    - Create PipelineRun for saving artifacts                     │
│    - Save agent_1_conversation.json and agent_1_output.json     │
│    - _get_user_understanding(state) reconstructs Pydantic model │
│                                                                   │
│ 2. Emit "pipeline_started"                                       │
│                                                                   │
│ 3. Parallel execution via asyncio.gather:                        │
│                                                                   │
│    ┌─ Branch A: Research + Screening (sequential within) ──┐    │
│    │                                                         │    │
│    │  • emit "agent_started" for agent_2                    │    │
│    │  • Call _run_real_agent2() or mock                     │    │
│    │  • Apply inject_registry_candidates() shim             │    │
│    │  • emit "candidates_found"                             │    │
│    │  • emit "agent_completed" for agent_2                  │    │
│    │  • Save agent_2_output.json                            │    │
│    │                                                         │    │
│    │  • emit "agent_started" for agent_4                    │    │
│    │  • Call _run_real_agent4() or mock                     │    │
│    │  • emit "candidates_verified"                          │    │
│    │  • emit "agent_completed" for agent_4                  │    │
│    │  • Save agent_4_output.json                            │    │
│    └────────────────────────────────────────────────────────┘    │
│                                                                   │
│    ┌─ Branch B: Test Generation ──────────────────────────┐      │
│    │  • emit "agent_started" for agent_3                   │      │
│    │  • Call _run_real_agent3() or mock                    │      │
│    │    (routes to 3F if test files provided)              │      │
│    │  • emit "test_cases_ready"                            │      │
│    │  • emit "agent_completed" for agent_3                 │      │
│    │  • Save agent_3_output.json                           │      │
│    └────────────────────────────────────────────────────────┘    │
│                                                                   │
│    Both branches run concurrently. Agent 4 can start as soon    │
│    as Agent 2 finishes, WITHOUT waiting for Agent 3 to finish.  │
│                                                                   │
│ 4. After BOTH branches complete:                                  │
│    - Check for cancellation                                       │
│    - emit "agent_started" for agent_5                            │
│    - Call _run_real_agent5() with progress_callback              │
│      - Internally: Agent 5 emits candidates_selected, then:      │
│        - ThreadPoolExecutor builds all candidates in parallel    │
│        - For each: emit build_turn, harness_started/completed    │
│        - Then ThreadPoolExecutor runs tests in parallel          │
│        - For each: emit test_result, candidate_results_ready     │
│    - emit "agent_completed" for agent_5                          │
│    - Save agent_5_output.json                                    │
│                                                                   │
│ 5. Report generation step:                                       │
│    - emit "report_generating"                                     │
│    - await asyncio.sleep(5)                                      │
│                                                                   │
│ 6. emit "pipeline_completed" with final total cost               │
│                                                                   │
│ 7. Cleanup:                                                       │
│    - pipeline_run.finalize() writes pipeline_summary.json        │
│    - emit "done"                                                  │
│    - event_bus.close()                                           │
└─────────────────────────────────────────────────────────────────┘
```

**Why parallel branches matter:** This matches the CLI's `ThreadPoolExecutor(max_workers=2)` pattern. Agent 4 doesn't depend on Agent 3 — making Agent 4 wait for Agent 3 to finish would waste time.

---

## 5. SSE Event System

### Thread Safety (CRITICAL)

**`EventBus` uses `queue.Queue`, NOT `asyncio.Queue`**:

```python
# services/event_bus.py
class EventBus:
    def __init__(self):
        self._queue: queue.Queue[SSEEvent | None] = queue.Queue()
```

**Why:** Agent 5's progress_callback fires from ThreadPoolExecutor threads. `asyncio.Queue` is NOT thread-safe for cross-thread `put_nowait()`. `queue.Queue` IS thread-safe.

The async `subscribe()` method uses `asyncio.to_thread(self._queue.get)` to poll without blocking the event loop.

### Complete Event Catalog

| Event Type | When Fired | Payload | UI Effect |
|-----------|------------|---------|-----------|
| `pipeline_started` | Pipeline begins | `{trace_id}` | Stage→pipeline, Activity tab appears |
| `agent_started` | Each agent begins | `{agent, name}` | Pipeline node pulses, activity entry |
| `agent_activity` | Any agent activity | `{agent, message, status?, candidate_name?}` | Activity feed entry |
| `agent_completed` | Each agent finishes | `{agent, cost_usd}` | Node shows checkmark + cost |
| `candidates_found` | After Agent 2 | `{candidates[]}` | Candidate cards appear |
| `test_cases_ready` | After Agent 3 | `{count}` | (no direct UI, just notifies) |
| `candidates_verified` | After Agent 4 | `{validated[], rejected[]}` | Cards get Verified badge, rejected removed |
| `candidates_selected` | Agent 5 internal selection | `{selected[]}` | Filter cards to only selected |
| `harness_started` | Before building a harness | `{candidate_name}` | Card shows "Building..." |
| `build_turn` | Each Agent 5 turn | `{candidate_name, turn, max_turns, phase, tools_used, cost_usd}` | Per-candidate progress log |
| `harness_completed` | Harness built OK | `{candidate_name, build_turns, build_cost_usd}` | Card shows "Built" |
| `harness_failed` | Harness build failed | `{candidate_name, failure_reason}` | Card shows "Failed" |
| `test_execution_started` | Before running tests | `{candidate_name}` | Card shows "Testing..." |
| `test_result` | Each test case | `{candidate_name, test_case_id, passed, weighted_score, latency_ms, criteria_scores[]}` | Test detail rows populate |
| `candidate_results_ready` | All tests for a candidate done | `{candidate_name, tests_passed, tests_failed, pass_rate, avg_latency_ms, total_cost_usd, overall_score}` | Performance/speed/cost appear |
| `report_generating` | Before final results | `{}` | Activity shows "Generating report..." |
| `pipeline_completed` | All done | `{total_cost_usd, summary}` | Stage→results, comparison view |
| `pipeline_cancelled` | User cancelled | `{cancelled_at_agent}` | Show partial results |
| `pipeline_failed` | Unrecoverable error | `{error}` | Show error message |
| `done` | Stream closing | `{}` | EventSource closes |

### SSE Transport

- Frontend uses native `EventSource` API (browser-built-in)
- Backend uses `sse-starlette` which handles SSE framing
- Each event is JSON: `{type: "event_name", data: {...}}`
- The Vite proxy rewrites `/pzapi/*` → `/api/*` to avoid clash with `/api` React route

---

## 6. State Management

### RunState (services/run_manager.py)

```python
@dataclass
class RunState:
    run_id: str                    # Short UUID for the run
    trace_id: str                  # Full UUID for log correlation
    status: str                    # "created" | "agent1_conversation" | "pipeline_running" | "completed" | "failed" | "cancelled"
    cancel_requested: bool         # Set by DELETE /runs/{id}, checked between agents

    # Per-agent mode configuration
    agent_modes: dict[str, str]    # {"agent1": "mock", "agent2": "real", ...}

    # Agent 1 conversation
    conversation_history: list[dict]  # [{"role": "user"|"assistant", "content": str}]
    agent1_result: dict | None        # Full Agent1Result as dict (after is_clear=True)
    user_text: str
    current_turn: int              # 1-indexed

    # Pipeline results (dicts from .model_dump())
    agent2_result: dict | None
    agent3_result: dict | None
    agent4_result: dict | None
    agent5_result: dict | None

    # Cached Pydantic models (for type-safe downstream input construction)
    # Set dynamically: state._agent1_model, state._agent2_model, state._agent3_model
    # Used by _run_real_agent4/5 when building their inputs

    # File management
    uploaded_files: list[dict]     # [{file_id, filename, path, size}]
    file_id_to_path: dict[str, str]

    # Cost tracking
    total_cost_usd: float

    # SSE
    event_bus: EventBus

    # Background task reference
    pipeline_task: asyncio.Task | None
```

### RunManager (singleton)

```python
# In-memory dict. NOT persisted. Lost on server restart.
# For cloud deployment, replace with Redis.
run_manager = RunManager()

run_manager.create_run(text, agent_modes) → RunState
run_manager.get_run(run_id) → RunState | None
run_manager.cancel_run(run_id) → bool  # Sets cancel_requested flag
```

---

## 7. Mock vs Real Mode

The system has a **per-agent mode toggle**. Each agent independently can be "mock" or "real":

```python
agent_modes = {
    "agent1": "mock",  # Scripted conversation, no API calls
    "agent2": "real",  # Actual web search + research
    "agent3": "mock",  # Load test cases from working_test_6
    "agent4": "real",  # Actual API verification
    "agent5": "mock",  # Replay harness builds from working_test_6
}
```

### Mock Mode

- Reads from `PuzzleEval-local/runs/working_test_6/agent_N_output.json`
- Emits the same SSE events a real run would, with artificial delays
- **Zero API cost** — for testing UI flow
- Mock Agent 1: returns scripted clarifying questions on turn 1, `is_clear=true` on turn 2

### Real Mode

- Calls actual agent functions from `puzzleeval.agents.*`
- Makes real Claude API calls
- Costs real money ($5-7 per full run)
- Uses `asyncio.to_thread()` to run sync agent functions without blocking

### Why This Matters

Enables development and demos without burning money. You can:
- Develop the UI with all agents mocked ($0)
- Test Agent 1 with only agent1=real (~$0.05)
- Progressively test each agent individually
- Only do full real runs when everything is verified

---

## 8. Agent Input Construction

Every agent has specific Pydantic input requirements. Getting these wrong = instant failure. Here's the exact field-by-field construction for each agent:

### Agent 1 Input (user_understanding.py)

```python
Agent1Input(
    user_text=user_message,                    # str, required
    workflow_file_path=None,                   # str|None. CRITICAL: set to None.
                                                # Files go to Agent 3F, NOT Agent 1.
                                                # The CLI never passes --file when using --test-files.
    trace_id=state.trace_id,                   # str, required
    conversation_history=conv_history,         # list[dict] | None
                                                # Excludes current user message (that's in user_text)
)
```

**Why workflow_file_path=None:** Attempting to send PDF/image as `workflow_file_path` causes Agent 1 to put it in the system prompt as a non-text block. `client.messages.parse()` rejects this with "system.1.type: Input should be 'text'" error. The CLI separates workflow context files (`--file`) from test files (`--test-files`). We don't support workflow context files from the web UI — all uploaded files are test files.

### Agent 2 Input (research.py)

```python
Agent2Input(
    user_understanding=user_understanding,     # UserUnderstandingOutput (Pydantic model, NOT dict)
    trace_id=state.trace_id,                   # str
)

# CRITICAL POST-STEP: Apply the registry injection shim
result = inject_registry_candidates(result)
```

**Why the shim:** It injects all providers from `provider_registry.json` into Agent 2's results with `relevance_score=0.99`, guaranteeing they're selected in Agent 5 (where we have credentials for live testing). Without this, Agent 2 might find different providers than the ones we can actually test. The CLI applies this same shim on line 356 of cli.py.

### Agent 3 Input (synthetic_tests.py / synthetic_tests_file.py)

```python
Agent3Input(
    user_understanding=user_understanding,     # UserUnderstandingOutput
    trace_id=state.trace_id,                   # str
    test_file_paths=test_file_paths or None,   # list[str] | None
)

# Routing
if test_file_paths:
    result = run_file_tests_agent(input_data)      # Agent 3F (vision-based)
else:
    result = run_synthetic_tests_agent(input_data) # Agent 3 (text-only)
```

### Agent 4 Input (screening.py)

**MOST COMMON BUG HERE.** The schema says:
```python
class Agent4Input(BaseModel):
    candidates: Agent2Result   # NOT list[Candidate], the FULL Agent2Result
```

Correct construction:
```python
# Reconstruct Agent2Result from cached model or dict
if hasattr(state, "_agent2_model") and state._agent2_model:
    agent2_result_model = state._agent2_model
else:
    agent2_result_model = Agent2Result(**state.agent2_result)

Agent4Input(
    candidates=agent2_result_model,            # FULL Agent2Result object
    user_understanding=user_understanding,
    trace_id=state.trace_id,
)
```

### Agent 5 Input (implement_test_env.py)

```python
# Reconstruct ScreenedCandidate models from dicts
validated_dicts = state.agent4_result.get("validated_candidates", [])
validated_models = [ScreenedCandidate(**c) for c in validated_dicts]

# Reconstruct Agent3Result
agent3_result_model = state._agent3_model or Agent3Result(**state.agent3_result)

# Load credentials — CRITICAL: pass candidates for name matching
registry = load_registry()
provider_creds = get_all_credentials(registry, validated_models)

Agent5Input(
    validated_candidates=validated_models,     # list[ScreenedCandidate]
    user_understanding=user_understanding,     # UserUnderstandingOutput
    test_cases=agent3_result_model,            # FULL Agent3Result
    trace_id=state.trace_id,
    provider_credentials=provider_creds,       # dict | None
)
```

---

## 9. Credential Handling

**The flow from provider_registry.json → Agent 5 subprocess env:**

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. provider_registry.json                                        │
│    {                                                              │
│      "providers": {                                               │
│        "veryfi": {                                                │
│          "env_vars": {                                            │
│            "VERYFI_API_KEY": "...",                               │
│            "VERYFI_CLIENT_ID": "...",                             │
│            ...                                                    │
│          }                                                        │
│        },                                                         │
│        "mindee": {...},                                           │
│        "nanonets": {...},                                         │
│        "klippa": {...}                                            │
│      }                                                            │
│    }                                                              │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼ load_registry()
┌─────────────────────────────────────────────────────────────────┐
│ 2. ProviderRegistry object                                       │
│    registry.providers = {                                        │
│      "veryfi": ProviderEntry(env_vars={...}),                    │
│      ...                                                          │
│    }                                                              │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼ get_all_credentials(registry, validated_models)
┌─────────────────────────────────────────────────────────────────┐
│ 3. Matching function normalizes names and matches:               │
│    - Exact match on candidate name                               │
│    - Exact match on provider name                                │
│    - Substring match                                             │
│    - Fallback to os.environ                                      │
│                                                                   │
│    Returns:                                                       │
│    {                                                              │
│      "veryfi": {VERYFI_API_KEY: "...", ...},                     │
│      "mindee": {MINDEE_API_KEY: "...", ...},                     │
│      "nanonets": {NANONETS_API_KEY: "...", ...},                 │
│      "klippa": {KLIPPA_API_KEY: "..."}                           │
│    }                                                              │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼ Agent5Input.provider_credentials
┌─────────────────────────────────────────────────────────────────┐
│ 4. Inside Agent 5:                                               │
│    - _resolve_credentials(candidate) returns the right dict      │
│    - _build_sandbox_env(sandbox_dir, extra_env=credentials)      │
│      merges credentials INTO os.environ for the subprocess       │
│                                                                   │
│    Used in 3 phases:                                             │
│    a) Phase 2 build: run_code tool executes harness code         │
│    b) Phase 3 live validation: run_code runs live_test.py        │
│    c) Post-loop execution: _execute_single_test runs harness     │
│                                                                   │
│    All 3 use the same _build_sandbox_env pattern.                │
└─────────────────────────────────────────────────────────────────┘
```

**Verified safe:** Credentials are injected into the subprocess environment for every single API call, across all 3 phases of Agent 5.

---

## 10. File Upload Flow

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. User clicks paperclip in chat, selects 3 PDFs                │
│    Frontend stores File objects in local state                   │
│    Files show as chips below input — NOT uploaded yet           │
└───────────────────────────┬─────────────────────────────────────┘
                            │ User clicks Send
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. Frontend: handleSend(text, pendingFiles)                      │
│    a. createRun(text) → gets run_id                              │
│    b. uploadFiles(run_id, files) → POST /api/runs/{id}/files    │
│       Returns { file_ids }                                       │
│    c. sendMessage(run_id, text, file_ids) → POST chat            │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. files.py saves bytes to disk                                  │
│    run_upload_dir = puzzleeval-api/uploads/{run_id}/             │
│    For each file:                                                │
│      dest = run_upload_dir / filename                            │
│      write bytes                                                  │
│      state.uploaded_files.append({                               │
│        file_id, filename,                                        │
│        path: str(dest.resolve()),   # ABSOLUTE path              │
│        size                                                       │
│      })                                                           │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. chat.py calls real_agent1_turn(state, message)                │
│    Agent 1 gets text only — NOT the files                        │
│    (workflow_file_path=None, see Section 8)                      │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 5. Pipeline starts → _run_real_agent3(state)                     │
│    test_file_paths = [                                           │
│      f.get("path") for f in state.uploaded_files                 │
│      if f.get("path")                                            │
│    ]                                                              │
│    Agent 3F receives ALL uploaded files                          │
│    It reads each file via Claude vision (PDFs, images)           │
│    Generates one test case per file with ground truth            │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│ 6. Agent 5 receives test_cases with file paths                   │
│    _stage_test_files() COPIES files from uploads/                │
│    into each sandbox/{candidate}/test_files/                     │
│    Updates test_case.test_file_path to the staged absolute path  │
│    Builder agent sees absolute paths in initial message          │
│    All 3 phases (build, live validation, post-loop) use these   │
└─────────────────────────────────────────────────────────────────┘
```

**Key property:** Files are never lost because:
- Upload happens on user action (click Send), not on file selection
- Paths are absolute throughout
- Agent 3F reads files by absolute path
- Agent 5 stages copies into per-candidate sandboxes
- Subprocess env includes the sandbox's CWD

---

## 11. CWD Management

Agent 5 creates harness sandbox directories using a RELATIVE path:

```python
# inside implement_test_env.py (PuzzleEval package)
harness_base = Path("runs") / input_data.trace_id / "harnesses"
harness_base.mkdir(parents=True, exist_ok=True)
```

This means the location depends on the current working directory when `run_implement_test_env_agent()` is called.

**Problem:** FastAPI server's CWD could be anywhere (wherever uvicorn was started). If CWD is wrong, harnesses get created in the wrong place.

**Solution** (in `_run_real_agent5()`):

```python
API_ROOT = Path(__file__).resolve().parent.parent  # puzzleeval-api/

original_cwd = os.getcwd()
def _run_agent5_with_cwd():
    os.chdir(str(API_ROOT))
    try:
        return run_implement_test_env_agent(agent5_input, progress_callback=...)
    finally:
        os.chdir(original_cwd)

result = await asyncio.to_thread(_run_agent5_with_cwd)
```

This forces CWD to `puzzleeval-api/` before Agent 5 runs, so harnesses end up in `puzzleeval-api/runs/{trace_id}/harnesses/`. The original CWD is restored in `finally`.

**Thread safety caveat:** `os.chdir()` is process-wide. If multiple pipelines run concurrently (future cloud deployment), they'd fight over CWD. For single-user local mode this is fine. For production, Agent 5 would need to accept an absolute `runs_dir` parameter instead.

---

## 12. Progress Callbacks

Agent 5 is a ~3500-line function. Without callbacks, we'd wait 5 minutes with no UI feedback.

### How it works

```python
# Our pipeline_runner.py defines the callback
def _agent5_progress(event_type: str, data: dict):
    # Emits SSE events based on event_type
    ...

# Pass to Agent 5
run_implement_test_env_agent(agent5_input, progress_callback=_agent5_progress)
```

### Where callbacks fire inside Agent 5

| Location (implement_test_env.py) | Event Type | Purpose |
|----------------------------------|-----------|---------|
| After `candidates = sorted_candidates[:AGENT5_MAX_CANDIDATES]` (line 3209) | `candidates_selected` | Authoritative list of which N candidates Agent 5 picked |
| Before `executor.submit()` for each candidate (~line 3240) | `harness_started` | Before each build begins |
| After `turn_log` is built inside build loop (~line 1647) | `build_turn` | Per-turn progress with phase + tools used |
| After `future.result()` returns TestHarness (~line 3257) | `harness_completed` | Build succeeded |
| After `future.result()` returns FailedHarness or raises (~line 3265) | `harness_failed` | Build failed |
| Inside `_run_tests_for_candidate()` before tests (~line 3351) | `test_execution_started` | Starting tests for one candidate |
| After metrics computed, before return (~line 3470) | `candidate_results_ready` | All tests done for one candidate, with full scores |

### Why `candidates_selected` matters

Agent 5 sorts candidates **credentials-first, then by relevance_score**. The top-N it picks depends on the registry. If we predicted the selection from Agent 4's output using only relevance_score, we'd pick the WRONG candidates.

The authoritative source is Agent 5's internal selection, emitted right after it picks. The frontend filters its candidate list to exactly what Agent 5 builds.

---

## 13. Observability

Every pipeline run creates a debug directory:

```
puzzleeval-api/runs/{trace_id}/
├── agent_1_conversation.json   # Full chat history (all turns)
├── agent_1_output.json         # Agent 1's final Agent1Result
├── agent_2_output.json         # Agent2Result with candidates + cost
├── agent_3_output.json         # Agent3Result with test cases
├── agent_4_output.json         # Agent4Result with validated/rejected
├── agent_5_output.json         # Agent5Result with harnesses + test_results (often 1+ MB)
├── pipeline_summary.json       # Overall status, timing, per-agent costs
└── harnesses/                  # Created by Agent 5 itself
    └── {candidate_slug}/
        ├── harness.py                # The generated API client
        ├── requirements.txt
        ├── smoke_test.py
        ├── .venv/                    # Per-harness Python venv
        ├── conversation_log.json     # Full Claude conversation for this build
        ├── api_spec.txt              # Extracted API documentation
        └── fetched_docs_*.txt        # Web-fetched docs
```

**These files are gold for debugging.** If a real run fails or produces bad results:
1. Open `agent_N_output.json` to see what each agent produced
2. Open `harnesses/{slug}/conversation_log.json` to see what Claude did during build
3. Open `harnesses/{slug}/harness.py` to see the generated code
4. Open `pipeline_summary.json` for timing and cost breakdown

The `_save_json()` helper in `pipeline_runner.py` saves these files. It handles both dicts and Pydantic models.

---

## 14. Gotchas

### Gotcha 1: dotenv needs `override=True`
```python
load_dotenv(Path(__file__).parent / ".env", override=True)
```
Without `override=True`, if `ANTHROPIC_API_KEY` is already set to an empty string in the environment, dotenv won't replace it.

### Gotcha 2: Vite proxy and `/api` page collision
The Lovable-generated site has an `/api` page. Vite's proxy was set to forward `/api/*` to FastAPI, making the page unreachable.

**Fix:** Changed proxy path to `/pzapi/*`, with rewrite rule that converts it to `/api/*` when forwarding. Frontend's `API_BASE = "/pzapi"`. Backend endpoints stay at `/api/*`.

### Gotcha 3: Agent 1 and binary files
`client.messages.parse()` rejects non-text blocks in the `system` parameter. Agent 1's `_build_system_blocks()` puts PDF/image content blocks in the system prompt when `workflow_file_path` is set.

**Fix:** We set `workflow_file_path=None`. All uploaded files go to Agent 3F only.

### Gotcha 4: success_rate vs weighted_score
`CandidateTestRun.success_rate` = "tests that executed without errors" (0.0 to 1.0).
`CandidateTestRun.pass_rate` = "tests that passed evaluation" (0.0 to 1.0).
`TestCaseResult.weighted_score` = "quality score from LLM judge" (0.0 to 1.0).

Klippa with 0/3 tests passed had `success_rate=1.0` (all executed) but `pass_rate=0.0`. Using `success_rate` as the performance score would show Klippa as 100/100.

**Fix:** `_compute_avg_weighted_score(candidate_run)` averages `weighted_score` across test_results. This is the real quality metric.

### Gotcha 5: Agent 4 input type
`Agent4Input.candidates` expects the FULL `Agent2Result` Pydantic model, not a list of Candidates. The schema specifies `candidates: Agent2Result`.

### Gotcha 6: Agent 5 candidate selection
Agent 5 sorts candidates with `(has_credentials, relevance_score)` descending — **credentials first**. Predicting this externally requires knowing which candidates have registry matches. Safer: let Agent 5 emit `candidates_selected` itself.

### Gotcha 7: Variable scoping with asyncio.gather
Variables defined inside branch functions aren't accessible after `asyncio.gather()` returns. Must read state back from `state.agent4_result` etc.

### Gotcha 8: model_dump() makes Pydantic models into dicts
When we store `state.agent2_result = result.model_dump()`, the nested Pydantic models become dicts. For downstream agents that need Pydantic types, we cache the original model via `state._agent2_model` for type-safe construction.

### Gotcha 9: pipeline_run.finalize() without save_agent_result()
We bypass `PipelineRun.save_agent_result()` and use `_save_json()` directly. This means `pipeline_summary.json` has less detail than CLI's version, but individual `agent_N_output.json` files are still complete.

### Gotcha 10: EventBus close() must fire
If the pipeline crashes before `event_bus.close()`, the SSE subscriber hangs forever. The `finally` block ensures close always fires.

---

## 15. Testing

### Mock-mode smoke test

```bash
cd puzzleeval-api
python -c "
import asyncio
from dotenv import load_dotenv; from pathlib import Path
load_dotenv(Path('.env'), override=True)
import sys
sys.path.insert(0, str(Path('.').resolve().parent / 'PuzzleEval-local'))

from services.run_manager import run_manager
from services.pipeline_runner import run_pipeline, mock_agent1_turn

state = run_manager.create_run('test')
asyncio.run(mock_agent1_turn(state, 'invoice OCR'))
asyncio.run(mock_agent1_turn(state, 'pdfs'))
asyncio.run(run_pipeline(state))
"
```

Should complete in ~60-90 seconds with 100+ SSE events and all 7 JSON artifacts saved.

### Incremental real testing

1. Set `agent1: "real", rest: "mock"` — tests Agent 1 conversation (~$0.05)
2. Set `agent1,2: "real", rest: "mock"` — tests research (~$0.40)
3. Set `agent1-4: "real", agent5: "mock"` — tests screening (~$1.50)
4. Set all "real" — full pipeline (~$5-7)

---

## 16. Future Work

### For Cloud Scale (1000+ users)

1. **Replace RunManager** in-memory dict with Redis. RunState becomes a hash.
2. **Replace EventBus** queue.Queue with Redis pub/sub so SSE works across multiple FastAPI workers.
3. **Replace uploads/ and runs/** local dirs with S3/GCS + presigned URLs.
4. **Agent 5 CWD fix:** Add `runs_dir: str | None` to `Agent5Input` so we can pass absolute paths and remove `os.chdir()`.
5. **Agent 5 job queue:** Move Agent 5 execution to Celery or SQS workers so long builds don't hold FastAPI request handlers.
6. **Database:** PostgreSQL for run history, user accounts, billing.
7. **Auth:** OAuth (Google, GitHub) or email/password via Supabase.

### For Fault Tolerance

1. **Resumable sessions:** Frontend stores `run_id` in localStorage. On reconnect, call `GET /runs/{id}` and rebuild state from the saved JSON files. Reconnect SSE.
2. **Event history replay:** Store all emitted events in Redis, so reconnecting clients can catch up.

### For UX Polish

1. **Token streaming for Agent 1:** Use `client.messages.stream()` + partial parsing to stream words as they're generated.
2. **Agent 5 in-turn streaming:** Currently we only emit after each turn completes. Could emit thinking tokens within a turn.
3. **Real-time cost cap:** Show a warning when approaching budget limits.

---

## Quick Reference

### Running the backend
```bash
cd puzzleeval-api
python -m uvicorn main:app --port 8001 --host 0.0.0.0
```

### Running the frontend
```bash
cd ai-agent-navigator
npx vite --port 8081
```

### Key endpoints
- `GET /api/health` — backend status
- `POST /api/runs` — create run
- `POST /api/runs/{id}/chat` — Agent 1 turn
- `POST /api/runs/{id}/files` — upload files
- `GET /api/runs/{id}/events` — SSE stream
- `DELETE /api/runs/{id}` — cancel run
- `GET /api/runs/{id}` — get run state (for reconnection)

### Key files to know
- `services/pipeline_runner.py` — everything important is here
- `services/run_manager.py` — RunState dataclass
- `services/event_bus.py` — thread-safe SSE queue
- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — Agent 5 (3500 lines)
- `PuzzleEval-local/puzzleeval/schemas.py` — Pydantic models for all agents
