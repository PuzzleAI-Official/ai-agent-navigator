"""Real E2E driver — exercises Agent 4 fast verify + Agent 5 research/build.

Scenario: voice agent comparison so we also verify the conversation audio
merge path. Two candidates max to keep cost bounded (~$3-4 total).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from urllib.error import HTTPError

BASE = "http://127.0.0.1:8001/api"

SCENARIO = (
    "I run a 24/7 customer service line for a small plumbing business. "
    "Callers should hear a friendly voice agent that books appointments "
    "and answers basic pricing questions. Compare OpenAI and ElevenLabs "
    "voice stacks — I want to see which one feels more natural and "
    "handles back-and-forth conversation better. Monthly volume: ~500 "
    "calls. Budget: $50-100/month. Technical level: non-technical."
)


def post(path: str, payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse_sse_line(buffer: str) -> list[tuple[str, dict]]:
    events = []
    current_event = None
    for line in buffer.splitlines():
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current_event:
            payload = line.split(":", 1)[1].strip()
            try:
                events.append((current_event, json.loads(payload)))
            except json.JSONDecodeError:
                events.append((current_event, {}))
            current_event = None
    return events


def stream_until(run_id: str, stop_event_types: set[str], on_event=None, timeout: int = 1800) -> dict:
    """Stream SSE events for run_id until one of stop_event_types fires.

    Returns the last matching event's payload.
    """
    sse_req = urllib.request.Request(
        BASE + f"/runs/{run_id}/events",
        headers={"Accept": "text/event-stream"},
    )
    start = time.time()
    final_payload = {}
    with urllib.request.urlopen(sse_req, timeout=timeout) as sse:
        buffer = ""
        while True:
            if time.time() - start > timeout:
                print(f"  [stream] timeout after {timeout}s")
                return final_payload
            try:
                line = sse.readline()
            except Exception as exc:
                print(f"  [stream] readline error: {exc}")
                return final_payload
            if not line:
                continue
            chunk = line.decode(errors="replace")
            buffer += chunk
            if chunk == "\n":
                events = parse_sse_line(buffer)
                buffer = ""
                for etype, data in events:
                    if on_event:
                        on_event(etype, data)
                    if etype in stop_event_types:
                        final_payload = data
                        return final_payload


def main() -> int:
    # 1. Create run
    print("=" * 72); print("STEP 1 — create run"); print("=" * 72)
    created = post("/runs", {
        "text": SCENARIO,
        "plan": "free",
        "agent_modes": {
            "agent1": "real", "agent2": "real", "agent3": "real",
            "agent4": "real", "agent5": "real",
        },
    }, timeout=30)
    run_id = created["run_id"]
    trace_id = created["trace_id"]
    print(f"  run_id:   {run_id}")
    print(f"  trace_id: {trace_id}")

    # 2. Send initial chat (Agent 1 might take 20-40s, budget 180s)
    print(); print("=" * 72); print("STEP 2 — send initial chat (Agent 1)"); print("=" * 72)
    t0 = time.time()
    reply = post(f"/runs/{run_id}/chat", {"message": SCENARIO}, timeout=180)
    a1_dur = time.time() - t0
    print(f"  Agent 1 turn took {a1_dur:.1f}s")
    print(f"  is_clear: {reply.get('is_clear')}")
    print(f"  pipeline_started: {reply.get('pipeline_started')}")

    # If Agent 1 asks clarifying questions, send a yes-to-all follow-up
    for _ in range(3):
        if reply.get("is_clear"):
            break
        qs = reply.get("clarifying_questions") or []
        if qs:
            print(f"  Agent 1 asked {len(qs)} clarifying Q(s) — answering briefly")
            ans = "Yes to all. Happy with either sandbox or free tier. No strict integration."
            t0 = time.time()
            reply = post(f"/runs/{run_id}/chat", {"message": ans}, timeout=180)
            print(f"  follow-up turn took {time.time() - t0:.1f}s, is_clear={reply.get('is_clear')}")

    if not reply.get("pipeline_started"):
        print("  ERROR: pipeline never started after conversation loop")
        return 2

    # 3. Stream SSE until selection_required
    print(); print("=" * 72); print("STEP 3 — stream SSE (Agents 1→2→4 + pause)"); print("=" * 72)
    start = time.time()
    agent4_start = [None]
    candidates_total = [0]

    def log_event(etype: str, data: dict):
        elapsed = time.time() - start
        d = data.get("data") or data
        if etype == "workflow_blueprint":
            steps = (d.get("workflow") or {}).get("steps") or []
            print(f"  [{elapsed:5.1f}s] workflow_blueprint: {len(steps)} steps")
        elif etype == "candidates_found":
            cands = d.get("candidates") or []
            candidates_total[0] = len(cands)
            print(f"  [{elapsed:5.1f}s] candidates_found: {len(cands)}")
            for c in cands[:8]:
                print(f"             - {c.get('name','?')}")
        elif etype == "agent_started":
            if d.get("agent") == "agent_4":
                agent4_start[0] = elapsed
            print(f"  [{elapsed:5.1f}s] agent_started: {d.get('agent')}")
        elif etype == "agent_completed":
            agent = d.get("agent", "?")
            cost = d.get("cost_usd", 0)
            if agent == "agent_4" and agent4_start[0] is not None:
                print(f"  [{elapsed:5.1f}s] agent_completed: {agent} cost=${cost:.3f} (duration: {elapsed - agent4_start[0]:.1f}s)")
            else:
                print(f"  [{elapsed:5.1f}s] agent_completed: {agent} cost=${cost:.3f}")
        elif etype == "candidate_verified":
            print(f"  [{elapsed:5.1f}s] verified: {d.get('candidate_name')} @ {d.get('scope_id')}")
        elif etype == "candidate_rejected":
            print(f"  [{elapsed:5.1f}s] rejected: {d.get('candidate_name')} reason={d.get('reason')}")
        elif etype == "selection_required":
            print(f"  [{elapsed:5.1f}s] selection_required — will pick candidates")

    sel = stream_until(run_id, {"selection_required", "pipeline_failed"}, on_event=log_event, timeout=900)
    if not sel:
        print("  ERROR: never reached selection_required")
        return 3

    sel_data = sel.get("data") or sel
    per_scope = sel_data.get("per_scope_candidates", {})
    default_picks = sel_data.get("default_picks", {})

    # 4. Pick candidates — prefer OpenAI + ElevenLabs per scope
    print(); print("=" * 72); print("STEP 4 — submit selection"); print("=" * 72)
    scope_picks = {}
    for scope_id, names in per_scope.items():
        preferred = [n for n in names if any(p in n.lower() for p in ("openai", "elevenlabs"))]
        picks = preferred[:2] if preferred else (default_picks.get(scope_id) or names)[:2]
        scope_picks[scope_id] = picks
        print(f"  scope {scope_id}: {picks}")
    resp = post(f"/runs/{run_id}/select-candidates", {
        "scope_picks": scope_picks,
        "user_added_candidates": [],
    }, timeout=30)
    print(f"  selection accepted: {resp.get('status','?')}")

    # 5. Stream SSE until pipeline_completed / failed
    print(); print("=" * 72); print("STEP 5 — watch Agent 5 (Phase 1 research + model transition + build)"); print("=" * 72)
    start = time.time()
    agent5_start = [None]
    hcounts = {"started": 0, "completed": 0, "failed": 0}

    def log_a5(etype: str, data: dict):
        elapsed = time.time() - start
        d = data.get("data") or data
        if etype == "agent_started" and d.get("agent") == "agent_5":
            agent5_start[0] = elapsed
            print(f"  [{elapsed:5.1f}s] agent_started: agent_5")
        elif etype == "harness_started":
            hcounts["started"] += 1
            print(f"  [{elapsed:5.1f}s] harness_started: {d.get('candidate_name')}")
        elif etype == "harness_completed":
            hcounts["completed"] += 1
            print(f"  [{elapsed:5.1f}s] harness_completed: {d.get('candidate_name')} "
                  f"turns={d.get('build_turns')} cost=${d.get('build_cost_usd',0):.2f}")
        elif etype == "harness_failed":
            hcounts["failed"] += 1
            print(f"  [{elapsed:5.1f}s] harness_failed: {d.get('candidate_name')} reason={d.get('reason')}")
        elif etype == "agent_completed" and d.get("agent") == "agent_5":
            cost = d.get("cost_usd", 0)
            dur = elapsed - (agent5_start[0] or 0)
            print(f"  [{elapsed:5.1f}s] agent_completed: agent_5 cost=${cost:.3f} duration={dur:.1f}s")
        elif etype == "evaluation_report":
            print(f"  [{elapsed:5.1f}s] evaluation_report emitted")
        elif etype == "pipeline_completed":
            print(f"  [{elapsed:5.1f}s] pipeline_completed")
        elif etype == "pipeline_failed":
            print(f"  [{elapsed:5.1f}s] pipeline_failed: {d}")

    final = stream_until(run_id, {"pipeline_completed", "pipeline_failed", "done"}, on_event=log_a5, timeout=3600)

    print()
    print("=" * 72)
    print("RESULTS SUMMARY")
    print("=" * 72)
    print(f"  trace_id: {trace_id}")
    print(f"  harnesses: started={hcounts['started']} completed={hcounts['completed']} failed={hcounts['failed']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HTTPError as exc:
        print(f"HTTPError {exc.code}: {exc.read().decode()[:500]}")
        sys.exit(4)
    except Exception as exc:
        print(f"driver error: {type(exc).__name__}: {exc}")
        sys.exit(5)
