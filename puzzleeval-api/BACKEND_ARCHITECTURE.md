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
│                       REACT FRONTEND (:8080)                     │
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
│  ├── agent1/core.py + templates/   (Pydantic)      .json         │
│  ├── agent2/core.py + templates/                                  │
│  ├── agent3/core.py + templates/                                  │
│  ├── agent3f/core.py + templates/                                 │
│  ├── agent4/core.py + templates/                                  │
│  ├── agent5/  (build_loop, api_call, dispatch_helpers, etc.)      │
│  └── implement_test_env.py  (back-compat shims to agent5/*)       │
│      + 5 legacy single-file shims (research.py, etc.)             │
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
│    a) Build/debug: run_code tool executes harness code           │
│    b) Live/prod-shape validation: run_code runs live_test.py     │
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

Agent 5 creates harness sandbox directories from `Agent5Input.runs_root` when
the API runner provides one, falling back to the CLI-compatible relative
`./runs` path only when the field is omitted:

```python
# inside implement_test_env.py (PuzzleEval package)
harness_base = (
    (Path(input_data.runs_root) if input_data.runs_root else Path("runs"))
    / input_data.trace_id
    / "harnesses"
).resolve()
harness_base.mkdir(parents=True, exist_ok=True)
```

The API runner passes the absolute `puzzleeval-api/runs` directory into Agent 4,
Agent 5, and the background venv pre-create task so docs handoff files, venvs,
harness code, audio artifacts, and reports all land in the same run tree.

**Solution** (in `_run_real_agent5()`):

```python
API_ROOT = Path(__file__).resolve().parent.parent  # puzzleeval-api/

agent5_input = Agent5Input(..., runs_root=str(API_ROOT / "runs"))
result = await asyncio.to_thread(
    run_implement_test_env_agent,
    agent5_input,
    progress_callback=...,
)
```

This avoids process-wide `os.chdir()`, which is unsafe when multiple runs overlap.

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
        ├── _agent_state/             # objective, research plan/synthesis, implementation plan, runtime/failure state
        └── fetched_docs_*.txt        # Web-fetched docs / cached snippets
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
4. **Agent 5 job queue:** Move Agent 5 execution to Celery or SQS workers so long builds don't hold FastAPI request handlers.
5. **Database:** PostgreSQL for run history, user accounts, billing.
6. **Auth:** OAuth (Google, GitHub) or email/password via Supabase.

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
- `POST /api/runs/{id}/chat` — Agent 1 turn (returns HTTP 402 with `reason="budget_exceeded"` when run cap is crossed)
- `POST /api/runs/{id}/files` — upload files (capped at `PUZZLEEVAL_MAX_UPLOAD_BYTES`, default 100 MiB; HTTP 413 on overflow)
- `GET /api/runs/{id}/events` — SSE stream (supports `last_event_id` for reconnect)
- `DELETE /api/runs/{id}` — cancel run
- `GET /api/runs/{id}` — get run state (for reconnection)
- `POST /api/runs/{id}/select-candidates` — submit scope_picks + user_added providers (Phase 6 gate)
- `GET /api/runs/{id}/report` — **NEW** — `EvaluationReport` dict (see §17). Reads persisted `evaluation_report.json` first, falls back to on-demand assembly.

### Key files to know
- `services/pipeline_runner.py` — pipeline orchestration + SSE events + new `_record_agent_cost_and_emit()` helper + `coverage_gap` emit + report assembly
- `services/run_manager.py` — `RunState` + new `budget: RunBudget` field + `record_cost()` helper
- `services/event_bus.py` — thread-safe SSE queue
- `main.py` — **NEW** `lifespan` handler shuts down plugin HTTP/SMTP servers cleanly on uvicorn restart
- `routes/runs.py` — includes new `GET /runs/{id}/report` endpoint
- `routes/files.py` — `_read_with_cap()` stream-reader with size guard
- `routes/chat.py` — catches `BudgetExceededError` → HTTP 402
- `PuzzleEval-local/puzzleeval/agents/implement_test_env.py` — back-compat forwarding shims to `agent5/*` (~4,700 lines, mostly delegation; new code goes in canonical `agent5/<sub-module>.py` per CLAUDE.md AD-010)
- `PuzzleEval-local/puzzleeval/schemas.py` — Pydantic models for all agents
- `PuzzleEval-local/puzzleeval/anthropic_client.py` — **NEW** central client factory (timeout + retries + model fallback ladder)
- `PuzzleEval-local/puzzleeval/budget.py` — **NEW** `RunBudget` + `BudgetExceededError`
- `PuzzleEval-local/puzzleeval/structured_output.py` — **NEW** `parse_with_fallback()` for grammar-budget recovery
- `PuzzleEval-local/puzzleeval/test_data_sufficiency.py` — **NEW** per-scope data-readiness verdict analyzer
- `PuzzleEval-local/puzzleeval/report.py` — **NEW** final `EvaluationReport` assembler

---

## Section 17. Resilience + cost wiring (current session additions)

All three pieces are live chokepoints every agent flows through — not observability gloss.

### Central Anthropic client factory (`puzzleeval/anthropic_client.py`)

Every agent used to construct its own `anthropic.Anthropic(api_key=...)` with bare defaults (10-min timeout, no retries). A flaky TCP socket hung the whole pipeline; a single transient 429/5xx killed runs. Now every agent calls:

```python
from puzzleeval.anthropic_client import build_client
client = build_client(api_key=ANTHROPIC_API_KEY)          # 120 s timeout + max_retries=3
# Agent 5 extends timeout for deep thinking:
client = build_client(api_key=ANTHROPIC_API_KEY, timeout=240)
```

`call_with_model_fallback(fn, primary_model, ...)` wraps a call in an Opus→Sonnet→Haiku ladder on persistent 429. Currently wired into Agent 1's `parse_with_fallback` path; other agents rely on SDK-level retries only (known gap).

### Cost budget circuit-breaker (`puzzleeval/budget.py` + `RunState.record_cost()`)

Every cost-accumulation site calls `state.record_cost(amount_usd, reason)` — drives BOTH `state.total_cost_usd` AND the budget's internal accumulator. When the run crosses `PUZZLEEVAL_MAX_RUN_COST_USD` (default $25), `BudgetExceededError` is raised.

**Outward surfacing:**
- `routes/chat.py` catches it during Agent 1 turns → HTTP 402 with `{reason, spent_usd, cap_usd, message}`.
- `services/pipeline_runner.py` catches it in the outer handler → `pipeline_failed` SSE with `{reason: "budget_exceeded", spent_usd, cap_usd, last_charge_reason, recovery}`.

**Where costs are recorded:**
- Agent 1 chat turns — `real_agent1_turn()` after result parse
- Agents 2/3/4 completion — `_record_agent_cost_and_emit("agent_N", cost)`
- Agent 5 completion — aggregates `total_build_cost_usd + total_test_cost_usd`

Every `_record_agent_cost_and_emit()` call also emits a `cost_update` SSE so the frontend meter updates between agent boundaries (previously it was frozen during multi-minute agents).

### Structured-output grammar fallback (`puzzleeval/structured_output.py`)

Wrapped around ALL 6 structured-output call sites (Agents 1, 2, 3, 3F, 4, and Agent 5's LLM evaluator).

Anthropic's `client.messages.parse(output_format=PydanticModel)` compiles the schema into a token-level grammar — fast and guaranteed valid, but size-capped. Several schemas (`Agent1Result`, `Agent2Result`, `Agent3Result`) sit near the cap. A single field addition can trip 400 errors:
- `"The compiled grammar is too large, which would cause performance issues."`
- `"Grammar compilation timed out."`

`parse_with_fallback(...)` catches those 400s and falls through to `client.messages.create()` with a non-strict tool whose `input_schema` is the same JSON Schema. Post-processes model output: (a) unwraps `{"input": {...}}` over-nesting, (b) coerces Python-repr strings (`"frozenset({'x'})"`) back to arrays before Pydantic validation. Returns a shim object with the same attributes the strict path would have — callers don't branch.

The fallback strips `thinking` + `output_config` (Anthropic forbids those with `tool_choice` forcing a specific tool). Graceful degradation — strict path tried first so normal runs keep adaptive thinking.

### Plugin lifecycle (`puzzleeval-api/main.py:lifespan`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure plugins register (auto-import)
    from puzzleeval.tool_plugins import list_plugins
    logger.info("startup: %d tool plugins registered", len(list_plugins()))
    yield
    # Shutdown: close every plugin's background server
    for plugin in list_plugins():
        shutdown_fn = getattr(plugin, "shutdown", None)
        if callable(shutdown_fn):
            shutdown_fn()
```

The 3 new local plugins (webhook_receiver, outbound_delivery, voice_realtime) spin up HTTP/SMTP servers on 127.0.0.1 lazily. Without this handler, `uvicorn --reload` leaks threads + sockets; the second start falls to ephemeral ports and silently breaks harnesses with hardcoded port references. All `_ThreadedHTTPServer` subclasses set `allow_reuse_address = True` for restart resilience.

### Evaluation report assembler (`puzzleeval/report.py`)

At `pipeline_completed` time, the pipeline runner assembles an `EvaluationReport` from Agents 1/2/4/5 outputs + total cost:

```json
{
  "run_id": "...", "trace_id": "...",
  "user_summary": "...", "domain": "construction",
  "monthly_volume": 100, "total_cost_usd": 4.53,
  "candidate_count": 3, "test_count": 24,
  "coverage": {
    "blueprint_step_ids": ["step_1", "step_2"],
    "covered_step_ids": ["step_1", "step_2"],
    "missing_step_ids": [], "coverage_percent": 1.0
  },
  "winners_by_scope": {"step_1": "Mindee", "step_2": "QuickBooks"},
  "overall_winner": "Mindee",
  "candidate_reports": [
    {
      "name": "Mindee", "provider": "Mindee", "rank": 1,
      "overall_score": 0.92, "pass_rate": 0.875,
      "passed_count": 7, "total_count": 8,
      "avg_latency_ms": 1240, "cost_usd_per_call": 0.012,
      "monthly_cost_projection_usd": 1.20,
      "auth_method": "api_key", "requirements": ["requests"],
      "auth_env_vars": ["MINDEE_API_KEY"], "sandbox_used": false,
      "failure_evidence": [{"test_case_id": "t3", "scenario": "damaged invoice", "passed": false, "score": 0.2, "reasoning_excerpt": "..."}],
      "success_evidence": [...],
      "pros": ["Top-tier overall score (92%)"], "cons": [...]
    }
  ],
  "advisories": []
}
```

Tolerates partial inputs — emits advisories for missing pieces rather than crashing. Data reaches the user via three paths:
- Persisted to `runs/<trace>/evaluation_report.json`
- Emitted as `evaluation_report` SSE event (→ `EvaluationReportCard`)
- Served via `GET /runs/{id}/report` (reads disk first, falls back to on-demand)

### Upload + SMTP DoS caps

- `routes/files.py:_read_with_cap()` — 1 MiB chunk reads with running tally, HTTP 413 on overflow.
- `outbound_delivery.py:_SMTPRequestHandler` — `readline(SMTP_MAX_LINE_BYTES)` (8 KB per RFC 5321) + total DATA cap `SMTP_MAX_DATA_BYTES` (25 MiB). Overflow returns SMTP 552 cleanly.

### `.env` autoload empty-shadow fix (`puzzleeval/__init__.py:_autoload_dotenv`)

Before calling `load_dotenv(override=False)`, walks the keys defined in the `.env` file and evicts any whose `os.environ` value is empty or whitespace-only. Fixes the CI footgun where `ANTHROPIC_API_KEY=` (empty placeholder) silently shadowed the real value. Real non-empty shell values still win (override=False preserved).

---

## Section 18. New SSE events

Frontend handlers in `src/hooks/usePipelineRun.ts`; event type registration in `src/services/api.ts:eventTypes`.

| Event | Trigger | Payload | UI effect |
|---|---|---|---|
| `test_data_sufficiency` | After Agent 1's sub-tasks, before Agent 3F | `{summary, verdicts[{scope_id, action, reason, advisories, request_message, degraded_confidence, plugin_for_augment, file_count}]}` | Chat note — READY / AUGMENT / SYNTHESIZE / REQUEST_MORE / DEGRADE per scope |
| `coverage_gap` | After `candidates_found` if zero candidates OR any scope uncovered | `{candidate_count, blueprint_step_ids, missing_scopes, covered_scopes, user_message}` | Chat warning with broaden-search guidance |
| `cost_update` | After every agent completion + Agent 1 turns | `{source, delta_usd, total_cost_usd, budget: {spent_usd, cap_usd, remaining_usd, utilization}}` | Live cost meter |
| `evaluation_report` | At `pipeline_completed` | Full `EvaluationReport` dict (see §17) | Renders via `EvaluationReportCard` |
| `pipeline_failed` with `reason="budget_exceeded"` | `BudgetExceededError` caught at pipeline boundary | `{error, reason, spent_usd, cap_usd, last_charge_reason, recovery}` | Distinct error path — suggests raising `PUZZLEEVAL_MAX_RUN_COST_USD` |

---

## Section 19. Environment variable reference

Every configurable knob. Defaults shown.

**Required:** `ANTHROPIC_API_KEY`

**Plugin credentials (any-of):** `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ASSEMBLYAI_API_KEY`, `ELEVENLABS_API_KEY`

**Anthropic client:**
- `PUZZLEEVAL_ANTHROPIC_TIMEOUT_S=120`
- `PUZZLEEVAL_ANTHROPIC_MAX_RETRIES=3`
- `PUZZLEEVAL_EFFORT=high` (low / medium / high / xhigh / max)
- `PUZZLEEVAL_MODEL=claude-sonnet-4-6`
- `PUZZLEEVAL_AGENT1_MODEL=claude-opus-4-7`

**Cost budget:** `PUZZLEEVAL_MAX_RUN_COST_USD=25.0`

**Agent 5 feature flags:**
- `PUZZLEEVAL_HYBRID_EVAL_ENABLED=0`
- `PUZZLEEVAL_PROGRAMMATIC_TOOLS_ENABLED=0`
- `PUZZLEEVAL_AGENT5_FALLBACK_ENABLED=1`
- `PUZZLEEVAL_AGENT5_FALLBACK_MAX=3`

**Uploads + sufficiency:**
- `PUZZLEEVAL_MAX_UPLOAD_BYTES=104857600` (100 MiB)
- `PUZZLEEVAL_MIN_FILES_PER_SCOPE=3`
- `PUZZLEEVAL_IDEAL_FILES_PER_SCOPE=6`

**New plugin tuning:**
- `PUZZLEEVAL_WEBHOOK_PORT=8765` / `PUZZLEEVAL_WEBHOOK_BIND=127.0.0.1` / `PUZZLEEVAL_WEBHOOK_MAX_BODY=1048576`
- `PUZZLEEVAL_TUNNEL_URL=` (optional public URL for offsite candidates)
- `PUZZLEEVAL_SMTP_PORT=2525` / `PUZZLEEVAL_SMTP_BIND=127.0.0.1` / `PUZZLEEVAL_SMTP_MAX_LINE_BYTES=8192` / `PUZZLEEVAL_SMTP_MAX_DATA_BYTES=26214400`
- `PUZZLEEVAL_SLACK_MOCK_PORT=8766` / `PUZZLEEVAL_SMS_MOCK_PORT=8767` / `PUZZLEEVAL_OUTBOUND_BIND=127.0.0.1` / `PUZZLEEVAL_OUTBOUND_HTTP_MAX_BODY=1048576`
- `PUZZLEEVAL_VOICE_PORT=` (unset = isolated loopback port; set only for operator-managed tunnels) / `PUZZLEEVAL_VOICE_BIND=127.0.0.1` / `PUZZLEEVAL_VOICE_MAX_AUDIO=26214400`
- `PUZZLEEVAL_TTS_PROVIDER=` (auto / openai_tts / elevenlabs) / `PUZZLEEVAL_ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM`

**Plugin registry strictness:** `PUZZLEEVAL_STRICT_PLUGIN_REGISTRY=0` (set `1` to raise on duplicate names)

**Frontend (Vite):** `VITE_API_BASE=/pzapi` — override to point the SPA at a different backend URL without rebuilding
