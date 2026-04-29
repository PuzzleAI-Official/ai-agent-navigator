---
id: live_test_voice
version: 1
title: Voice Live Test Session Contract
category: modality
description: |
  Teaches the harness builder how to write live_test.py for voice/multi-turn
  conversation harnesses. Live test must drive 2+ turns through the EXACT
  production payload shape (audio_url, turn_index, session_state,
  input_context). Skipping this causes silent production failures that
  smoke tests don't catch. Required when test cases have voice_conversation,
  voice_turn, or conversation modalities.
selectors:
  trigger_types:
    - voice_conversation
    - voice_turn
    - conversation
    - audio_content
selection_mode: deterministic
priority: 60
applies_to_agents:
  - agent_5
paired_gates: []
related_contracts:
  - voice
  - streaming_response
revisit_when:
  - "Production payload shape changes (e.g., new fields beyond audio_url + turn_index + session_state)"
---

## Voice / multi-turn live-test requirements (REQUIRED — Phase 3)

Your harness handles real-time voice or multi-turn conversation. The
plugin's `drive_conversation` will call `harness.run()` MULTIPLE TIMES
per test with this payload shape:

    {
        "audio_url": "<URL of caller's TTS-synthesized audio>",
        "turn_index": int,            # 0, 1, 2, ...
        "session_state": <mutable dict>,  # threading state across turns
        "input_context": {"instructions": "<system prompt>"}
    }

Your live_test.py MUST exercise THIS exact production flow before
HARNESS_COMPLETE. A live test that skips the audio path (e.g.,
`audio_url=None`) only verifies the trivial "agent greets without input"
case. Production tests with real caller audio will fail silently with
ZERO agent response — and you won't catch it.

### Required live_test.py shape (voice/conversation):

```python
"""Live test: drive a 2-turn conversation through the production payload shape."""
import os, json, requests, harness

# 1) Synthesize real caller audio (use any available TTS service)
def synth_caller_audio(text):
    # ... return raw audio bytes (mp3 / wav / pcm16)
    ...

# 2) Serve audio at a fetchable URL (local HTTP server OR upload to a temp store)
turn0_audio_url = serve_audio(synth_caller_audio("Hi, I have a problem with X"))
turn1_audio_url = serve_audio(synth_caller_audio("My name is Test User, phone 555-1234"))

# 3) Drive 2 turns with the EXACT production payload shape
session_state = {}
instructions = "You are a helpful agent. Greet the caller, gather their name + number."

# Turn 0 — fresh session (turn_index=0)
result_t0 = harness.run({
    "audio_url": turn0_audio_url,
    "turn_index": 0,
    "session_state": session_state,
    "input_context": {"instructions": instructions},
})

# Turn 1 — must reuse session_state (multi-turn continuity)
result_t1 = harness.run({
    "audio_url": turn1_audio_url,
    "turn_index": 1,
    "session_state": session_state,  # same dict — MUST persist agent state
    "input_context": {"instructions": instructions},
})

# 4) ASSERTIONS (live test PASSES = ALL true):
assert result_t0["success"] is True, f"Turn 0 failed: {result_t0.get('error')}"
assert result_t1["success"] is True, f"Turn 1 failed: {result_t1.get('error')}"

# Audio in BOTH turns — agent must respond, not just succeed
audio_t0 = result_t0.get("raw_response", {}).get("audio_bytes", b"")
audio_t1 = result_t1.get("raw_response", {}).get("audio_bytes", b"")
assert len(audio_t0) > 1000, "Turn 0 produced no agent audio"
assert len(audio_t1) > 1000, "Turn 1 produced no agent audio"

# Continuity check — turn 1's transcript should NOT restart with greeting
# (if agent says 'Thanks for calling' on turn 1, it's treating each turn
#  as a new conversation — session_state isn't carrying agent context)
transcript_t1 = (result_t0.get("output", "") + " " +
                  result_t1.get("output", "")).lower()
# (Soft check — log if greeting repeats; some providers legitimately
# re-greet, but flag it for review)

print(json.dumps({"turn0_audio_bytes": len(audio_t0),
                   "turn1_audio_bytes": len(audio_t1),
                   "turn0_transcript": result_t0.get("output", "")[:200],
                   "turn1_transcript": result_t1.get("output", "")[:200],
                   "success": True}, indent=2))
```

### Why this matters

The production flow is multi-turn audio. A live test that doesn't send
audio is meaningless for proving the harness works. Real-run trace
a4860e94 OpenAI: live test passed (audio_url=None path), real tests
got 0/5 — agent silent on every turn because the audio path was broken
but never tested.

### What HARNESS_COMPLETE requires for voice/conversation:

[Y] smoke_test.py passes (structural validation — same as before)
[Y] live_test.py drives 2 turns with REAL caller audio via the
    production payload shape `{audio_url, turn_index, session_state,
    input_context}`
[Y] BOTH turns return success=True
[Y] BOTH turns produce non-empty agent audio (> 1000 bytes)
[Y] session_state carries agent provider state across turns (verified
    by turn 1 not re-initializing the WebSocket / not re-creating
    the agent_id / not losing conversation context)

Skipping any of these → harness will silently fail in production tests.
