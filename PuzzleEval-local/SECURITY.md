# PuzzleEval — Sandbox Security Model

**Status:** local self-hosted — NOT cloud production-ready.

This document is the explicit threat model for the harness sandbox. It exists because Codex's Phase-2 review correctly observed that the cleanup-refactor plan focused on code organization and was silent on runtime safety. This document closes that gap by stating clearly what the sandbox protects against today and what it does NOT protect against.

## Current sandbox model (local self-hosted)

Each candidate's harness runs as a Python subprocess with these constraints:

1. **Working directory:** `runs/<trace_id>/harnesses/<candidate_slug>/`. Each candidate has its own directory, but they share the host filesystem above this point.
2. **Python interpreter:** a per-candidate `.venv/bin/python` (Linux/macOS) or `.venv/Scripts/python.exe` (Windows) created by `puzzleeval/agents/agent5/sandbox.py::create_venv`.
3. **Environment variables:** assembled by `agent5.tools.build_sandbox_env` — host environment minus PuzzleEval-specific vars, plus `PATH` extended with the venv's `bin/Scripts` directory, plus credentials passed via `extra_env`.
4. **Timeout:** soft timeout from `AGENT5_CODE_TIMEOUT` (default 120s, scales to 600s for async APIs). Implemented via `subprocess.run(..., timeout=N)` — sends `SIGTERM`/`TerminateProcess` after N seconds.
5. **Output:** stdout/stderr captured; trimmed to fit context; persisted to disk for debugging.

This is **adequate for local self-hosted operation** where the operator is running PuzzleEval against their own AI evaluation requests. It is **NOT adequate for SaaS / multi-tenant** operation.

## What the current model PROTECTS against

| Threat | Mitigation |
|---|---|
| Accidental infinite loop | `subprocess.run(timeout=N)` |
| Accidental network of unrelated calls | `web_fetch`/`web_search` are server tools (Anthropic API), candidate harness can only do what its API allows |
| Accidental filesystem trashing | `subprocess.run(cwd=sandbox_dir)` constrains relative file writes |
| Cross-candidate venv races | `_VENV_CREATE_LOCKS` (R2 invariant from Phase 2) |
| Credential leakage in logs | Plugins redact known auth fields from telemetry |

## What the current model does NOT protect against (cloud-pivot blockers)

| Threat | Why current model fails | What's needed for cloud |
|---|---|---|
| **Sandbox escape** | A subprocess running attacker-supplied Python can `os.execv`, `pickle.loads`, monkey-patch stdlib, fork, etc. Containment is OS-trust-the-user, not isolation. | Container/Firecracker/E2B isolation with seccomp + read-only rootfs |
| **Secret containment** | Credentials passed via env vars are visible to subprocess + can be printed to stdout, written to disk, or exfiltrated via `requests` | Secrets injection via short-lived per-candidate tokens; egress filter on log output |
| **Egress control** | Candidate harness can hit ANY URL — there's no allowlist. A malicious harness could exfiltrate data, scan internal networks, or hit ransomware-mining proxies. | Egress allowlist scoped to the candidate's documented API endpoints. Block private IP ranges. |
| **CPU/memory/disk limits** | Soft timeout only. A harness can consume all CPU cores, allocate all RAM, fill the disk. | `cgroups` / container resource limits (cpu shares, memory cap, ephemeral storage cap) |
| **Multi-tenant boundaries** | Single process, no tenant model. Every candidate sees `runs/` for the entire process. | Per-tenant filesystem isolation; tenant-tagged audit logs |
| **Artifact storage** | `runs/<trace_id>/` lives on local disk. No retention policy, no encryption at rest, no per-tenant access control. | Object storage (S3/GCS) with per-tenant prefixes + encryption + signed URLs |
| **Queue / idempotency** | No job queue. A second `run_pipeline()` for the same `trace_id` would race the first. | Job queue (SQS/Pub/Sub) with idempotent runner + dedupe key |
| **Cancellation** | `cancel_event` propagates to agent boundaries but not into Agent 5's 25-turn build loop (per OT-013 in CLAUDE.md). | Thread `cancel_event` through the build loop's API call boundary; honor it within ≤30s |
| **Subprocess input validation** | Tool dispatch trusts the LLM's tool_input dict. Malformed input could exploit `shell=True` somewhere downstream. | Schema-validate every tool_input via Pydantic at dispatch time |

## Cloud pivot triggers

This refactor (Phases 0-8) does NOT close any of the cloud-pivot blockers above. It delivers **code-organization production-readiness**, not **runtime-safety production-readiness**. The cloud-isolation work is a separate stream gated on:

1. **PuzzleEval pivots to SaaS** — multi-tenant, untrusted candidate harness execution.
2. **An untrusted-third-party candidate gets accepted into a real run** — today every candidate is one the operator chose to evaluate; tomorrow a marketplace model could submit one.
3. **A security incident** — discovered exfiltration via a candidate harness's API call.

When any of these triggers fires, the response is to spec a **Sandbox Isolation Stream** as a separate project. The minimum-viable delivery would be:
- Move harness execution into Docker / Firecracker / E2B (managed sandbox)
- Egress allowlist per candidate (matched against deep-verified endpoints)
- Resource caps (CPU shares, memory cap, ephemeral disk cap)
- Per-tenant credential injection via short-lived tokens
- Audit log of every harness subprocess invocation

That work is intentionally OUT OF SCOPE for the current architecture cleanup. Conflating them would multiply the regression surface for two unrelated concerns.

## Today's operational guidance

While running PuzzleEval locally:
- Operate in a workspace where the harness subprocess can do anything you can do — equivalent to running someone else's `pip install` + `python their_script.py`
- Don't run PuzzleEval against attacker-controlled candidate descriptions. Today every candidate is something the operator typed in or picked from Agent 2's discovery results.
- Treat `runs/` as containing potentially-sensitive data (real API keys appear in subprocess env vars; transcripts contain user-uploaded content)
- The CI runs in a sandbox that's already isolated (GitHub Actions / your CI provider) — no additional isolation needed there

## Authoritative references

- Sandbox env assembly: [puzzleeval/agents/agent5/tools.py::build_sandbox_env](puzzleeval/agents/agent5/tools.py)
- Venv creation: [puzzleeval/agents/agent5/sandbox.py::create_venv](puzzleeval/agents/agent5/sandbox.py)
- Credential resolution: [puzzleeval/agents/agent5/sandbox.py::resolve_candidate_credentials](puzzleeval/agents/agent5/sandbox.py)
- Tool dispatch: [puzzleeval/agents/agent5/tools.py::dispatch_tool](puzzleeval/agents/agent5/tools.py)
- Subprocess execution: [puzzleeval/agents/agent5/tools.py::run_code](puzzleeval/agents/agent5/tools.py)
- Cancellation propagation gap: see CLAUDE.md OT-013 ("HARNESS_COMPLETE is misleading")
