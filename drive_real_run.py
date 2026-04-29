"""Real-run driver: voice-agent evaluation, OpenAI + ElevenLabs only.

Runs against backend at http://127.0.0.1:8001 with agent_modes=real for all
agents. Listens on SSE, intercepts the selection_required pause, picks ONLY
OpenAI Realtime + ElevenLabs Conversational AI, then waits for pipeline_completed.

Captures event log + final report path so we can inspect post-run.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

API = "http://127.0.0.1:8001/api"
TRANSCRIPT_PATH = "real_run_transcript.jsonl"

VOICE_PROMPT = (
    "I run a small home-services business — about 200 inbound customer calls "
    "per month. Most callers want to schedule a plumber, ask about pricing, "
    "or report an emergency leak. I want to compare voice AI agents that can "
    "answer those calls — specifically OpenAI Realtime and ElevenLabs "
    "Conversational AI. I'm not technical; I just want to know which of those "
    "two handles real calls better, what each costs at our volume, and any "
    "gotchas. The agent should sound natural, capture the caller's name + "
    "address + nature of the problem, and never invent prices it doesn't "
    "actually know."
)


def log(*parts: Any) -> None:
    msg = " ".join(str(p) for p in parts)
    print(msg, flush=True)
    with open(TRANSCRIPT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "msg": msg}) + "\n")


def main() -> int:
    # Wipe transcript
    if os.path.exists(TRANSCRIPT_PATH):
        os.remove(TRANSCRIPT_PATH)

    log("=== Real run starting ===")
    log("Backend:", API)
    log("Voice prompt:", VOICE_PROMPT[:120] + "...")

    with httpx.Client(timeout=httpx.Timeout(60.0, read=None)) as client:
        # 1. Create run with all real agents
        log("\n[1/5] POST /runs (real mode for all agents)")
        resp = client.post(
            f"{API}/runs",
            json={
                "text": VOICE_PROMPT,
                "agent_modes": {
                    "agent1": "real",
                    "agent2": "real",
                    "agent3": "real",
                    "agent4": "real",
                    "agent5": "real",
                },
                "plan": "free",
            },
        )
        resp.raise_for_status()
        run = resp.json()
        run_id = run["run_id"]
        trace_id = run["trace_id"]
        log(f"  run_id={run_id}  trace_id={trace_id}")

        # 2. Send chat message
        log(f"\n[2/5] POST /runs/{run_id}/chat (one-shot clear request)")
        resp = client.post(f"{API}/runs/{run_id}/chat", json={"message": VOICE_PROMPT})
        resp.raise_for_status()
        chat = resp.json()
        log(f"  is_clear={chat['is_clear']}  pipeline_started={chat['pipeline_started']}")
        log(f"  assistant: {chat['assistant_message'][:200]}")
        if not chat["is_clear"]:
            log("WARN: Agent 1 wants more clarification. Sending follow-up to lock the brief.")
            followup = (
                "Yes, exactly those two: OpenAI Realtime and ElevenLabs Conversational AI. "
                "Volume is 200 calls per month. Done — please proceed."
            )
            resp = client.post(f"{API}/runs/{run_id}/chat", json={"message": followup})
            resp.raise_for_status()
            chat = resp.json()
            log(f"  is_clear={chat['is_clear']}  pipeline_started={chat['pipeline_started']}")
            if not chat["is_clear"]:
                log("ERROR: Agent 1 still not clear after 2 turns. Aborting.")
                return 2

        # 3. Subscribe to SSE.  Each event arrives as 2 lines:
        #     event: <name>
        #     data:  {"type": "<name>", "data": {...}}
        #     (blank line)
        log(f"\n[3/5] GET /runs/{run_id}/events (SSE stream)")
        selection_event = None
        last_status = None
        with client.stream("GET", f"{API}/runs/{run_id}/events", timeout=None) as stream:
            for line in stream.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                try:
                    envelope = json.loads(payload_text)
                except Exception:
                    log(f"  SSE [unparseable]: {payload_text[:200]}")
                    continue
                et = envelope.get("type", "?")
                data = envelope.get("data", {})
                log(f"  SSE [{et}]: {json.dumps(data)[:240]}")

                if et == "selection_required":
                    selection_event = data
                    if not _submit_selection(client, run_id, data):
                        log("ERROR: selection submit failed")
                        return 3
                if et in ("pipeline_completed", "pipeline_failed", "pipeline_cancelled"):
                    last_status = et
                    break
                if et == "agent_blocked":
                    log("Agent blocked. Continuing to watch.")

        log(f"\n[4/5] Pipeline ended with: {last_status}")

        # 4. Pull report + state for inspection
        log(f"\n[5/5] GET /runs/{run_id}/report + state")
        try:
            resp = client.get(f"{API}/runs/{run_id}/report")
            if resp.status_code == 200:
                report = resp.json()
                log(f"  report keys: {list(report.keys())[:15]}")
                with open("real_run_report.json", "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2)
                log("  report saved -> real_run_report.json")
            else:
                log(f"  report status: {resp.status_code}")
        except Exception as e:
            log(f"  report fetch error: {e}")

        try:
            resp = client.get(f"{API}/runs/{run_id}")
            state = resp.json()
            with open("real_run_state.json", "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            log("  state saved -> real_run_state.json")
            log(f"  status={state.get('status')}  total_cost_usd={state.get('total_cost_usd')}")
        except Exception as e:
            log(f"  state fetch error: {e}")

    log("\n=== Real run done ===")
    log(f"trace_id: {trace_id}")
    return 0 if last_status == "pipeline_completed" else 1


def _submit_selection(client: httpx.Client, run_id: str, payload: dict) -> bool:
    """Pick only OpenAI Realtime + ElevenLabs Conversational AI per scope.

    If they're not in the discovered candidates, add them via the `add` field
    so they still flow into Agent 4/5.
    """
    log("\n  -> selection_required intercepted")
    # per_scope_candidates is dict[scope_id, list[str]] (candidate NAMES, not dicts)
    per_scope = payload.get("per_scope_candidates", {})
    log(f"  scopes: {list(per_scope.keys())}")

    # Substring tolerance: candidates may surface under variants
    def _matches(name: str) -> bool:
        n = (name or "").lower()
        return ("openai" in n and "realtime" in n) or (
            "elevenlabs" in n and ("conversational" in n or "convai" in n)
        )

    scope_picks: dict[str, list[str]] = {}
    user_added: list[dict] = []
    all_scope_ids = list(per_scope.keys())

    for scope_id, names in per_scope.items():
        matches = [n for n in names if _matches(n)]
        log(f"  scope {scope_id}: {len(names)} candidates, {len(matches)} match target ({matches})")
        scope_picks[scope_id] = matches

    discovered_all = {n for names in per_scope.values() for n in names}
    has_oai = any(_matches(n) and "openai" in n.lower() for n in discovered_all)
    has_eleven = any(_matches(n) and "elevenlabs" in n.lower() for n in discovered_all)
    missing_oai = not has_oai
    missing_eleven = not has_eleven
    if missing_oai:
        user_added.append({
            "name": "OpenAI Realtime",
            "provider": "OpenAI",
            "api_docs_url": "https://platform.openai.com/docs/guides/realtime",
            "notes": "User-explicit voice candidate.",
            "covers_step_ids": all_scope_ids,
        })
        for sid in all_scope_ids:
            scope_picks.setdefault(sid, []).append("OpenAI Realtime")
        log("  ! OpenAI Realtime missing from discovery — added as user-added")
    if missing_eleven:
        user_added.append({
            "name": "ElevenLabs Conversational AI",
            "provider": "ElevenLabs",
            "api_docs_url": "https://elevenlabs.io/docs/conversational-ai/overview",
            "notes": "User-explicit voice candidate.",
            "covers_step_ids": all_scope_ids,
        })
        for sid in all_scope_ids:
            scope_picks.setdefault(sid, []).append("ElevenLabs Conversational AI")
        log("  ! ElevenLabs Conversational AI missing — added as user-added")

    # Strip empty scopes — backend rejects empty picks per scope
    scope_picks = {k: list(dict.fromkeys(v)) for k, v in scope_picks.items() if v}
    if not scope_picks:
        log("  ERROR: no scopes have any picks. Aborting.")
        return False

    body = {"scope_picks": scope_picks, "add": user_added}
    log(f"  POST /runs/{run_id}/select-candidates body: {json.dumps(body)[:400]}")
    resp = client.post(f"{API}/runs/{run_id}/select-candidates", json=body)
    if resp.status_code != 200:
        log(f"  ERROR submit failed: {resp.status_code} {resp.text[:300]}")
        return False
    log(f"  submit OK: {resp.json()}")
    return True


if __name__ == "__main__":
    sys.exit(main())
