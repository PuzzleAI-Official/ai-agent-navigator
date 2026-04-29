# Agent 5 Architecture Boundary

## Production Target

Agent 5 has two separate responsibilities that must not collapse into one
file again:

- **Backbone:** deterministic orchestration, tool execution, sandbox/container
  setup, model calls, phase transitions, telemetry, retries, and result
  aggregation.
- **Capability playbooks:** prompt guidance for a specific modality or
  response pattern, such as voice return shapes or streaming response
  collection.

The compatibility entry point remains
`puzzleeval.agents.implement_test_env`. New implementation should live in
`puzzleeval.agents.agent5` and be imported by that compatibility module.

## Why Local Playbooks, Not Native Skills

PuzzleEval runs agent builds inside its own service boundary and credential
model. Native Anthropic Skills are not the right abstraction for this first
production cleanup because they shift ownership of capability loading outside
PuzzleEval's deterministic router. Local playbooks keep these properties:

- Selection is deterministic from test-case modality metadata.
- Only relevant guidance enters the builder prompt.
- Safety-critical enforcement stays in Python, not markdown.
- Playbooks package cleanly into containers and serverless workers.

## Module Ownership

- `playbooks.py`: selects and caches local markdown playbooks.
- `prompts.py`: loads the packaged builder template and renders OS and
  capability placeholders.
- `tools.py`: owns local custom-tool execution for builder sandboxes.
- `sandbox.py`: will own workspace, venv, credential, and future container
  execution setup.
- `build_loop.py`: will own the model/tool loop once it is extracted from the
  compatibility module.
- `execution.py`: will own test-case execution and retry policy.
- `evaluation.py`: will own deterministic, plugin, and LLM-judge evaluation.
- `costing.py`: owns token, cache, advisor, and server-tool cost accounting.

## Cloud Migration Rules

- Treat the builder sandbox as a replaceable execution backend. Local venv
  execution is the development backend; production can replace it with a
  container or isolated worker without changing prompt routing.
- Keep module APIs stateless or explicitly pass run/candidate state. Avoid
  hidden process-global mutation except cached immutable package resources.
- Keep API wire shape and public imports stable until a versioned migration is
  planned.
- Prefer behavior tests around module contracts over source-grep tests as
  extraction progresses.

## Refined Cleanup Plan

1. Keep the legacy entry point stable while extracting real implementation.
2. Move capability contracts to local playbooks with cached deterministic
   loading.
3. Extract low-risk deterministic subsystems first: tools, prompts, costing,
   sandbox environment, telemetry helpers.
4. Extract the build loop and phase transitions only after tests cover the
   module contracts directly.
5. Migrate brittle source-grep tests gradually; do not rewrite dozens of tests
   just to satisfy a mechanical file move.
