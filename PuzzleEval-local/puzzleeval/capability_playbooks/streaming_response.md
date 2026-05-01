---
id: streaming_response
version: 1
title: Streaming Response Collection Contract
category: modality
description: |
  Teaches the harness builder the canonical error-timeout + reset-on-event
  pattern for collecting streaming responses (WebSocket events, SSE chunks,
  audio stream chunks, polled job results). Anti-bandaid: principle-based
  teaching with no provider-specific names or magic timeout numbers.
  Required when test cases have voice_conversation, voice_turn,
  audio_content, conversation, or code modalities (any streaming-shape
  response).
selectors:
  trigger_types:
    - voice_conversation
    - voice_turn
    - audio_content
    - conversation
    - code
selection_mode: deterministic
priority: 80
applies_to_agents:
  - agent_5
paired_gates: []
related_contracts:
  - voice
  - live_test_voice
revisit_when:
  - "New streaming protocol emerges that doesn't fit error-timeout pattern"
---

## Streaming / multi-event response collection — REQUIRED for streaming APIs

Your harness collects a response that arrives OVER TIME (WebSocket events,
SSE chunks, polled job results, audio stream chunks). Use the
**error-timeout + reset-on-event** pattern. DO NOT use "silence threshold"
patterns that try to predict completion from short gaps in the stream —
they confuse INFERENCE LATENCY with completion.

### Why "silence threshold" is wrong

When the harness sends user input and starts collecting, the server is doing:
  1. Process input (transcription, validation, routing)
  2. Run inference (LLM thinking, code generation, search, etc.)
  3. Generate output (TTS, formatting, encoding)
  4. Stream output chunks back

The first response chunk can take 1-15 seconds depending on the API. A
1-2 second silence threshold confuses "still inferring" with "done"
and exits BEFORE the first chunk arrives. The next collection turn then
receives the delayed chunk along with new input → garbled state, wrong
answers, tests fail randomly.

### Correct pattern (general for any streaming response)

```python
def collect_response(connection, error_timeout_s):
    """Collect events until completion signal OR extended idle.

    Reset the timeout on every OUTPUT-BEARING event/message. Transport
    keepalives (ping/pong/heartbeat/metadata-only events) do not prove
    the agent is still answering and must not keep the collector alive
    forever. Exit when no meaningful output arrives for `error_timeout_s`
    OR when an explicit completion event lands.
    """
    last_event_at = time.time()
    collected = []
    while True:
        idle = time.time() - last_event_at
        remaining = error_timeout_s - idle
        if remaining <= 0:
            break  # extended idle = agent done OR connection failed
        try:
            event = recv_with_remaining_budget(connection, remaining)
        except Timeout:
            break
        if is_explicit_completion_event(event):  # provider-specific marker
            process(event)
            break
        process(event)
        if is_output_bearing_event(event):
            last_event_at = time.time()  # reset on real response progress
    return collected
```

### Keepalives are not response progress

WebSocket APIs often send `ping`, `pong`, `heartbeat`, or metadata events
while the agent is silent. A harness should respond to keepalives if the
protocol requires it, but should not reset the response idle timer for
keepalive-only traffic. Otherwise a dead or non-answering stream can run
until the hard timeout, and the forensic log shows a long ping/pong tail
instead of the real failure.

### Sizing `error_timeout_s` (apply judgment based on the API)

  - LLM-backed providers (chat, voice, code-gen): **8-15 seconds.** First
    chunk wait can be several seconds (LLM inference + provider TTS/post-
    processing). Don't go below 8.
  - Async polling APIs (job queues, batch processing): **max_polls ×
    poll_interval.** Don't exit after one empty poll — the job may not
    have started yet.
  - Real-time event streams (audio chunks, SSE token stream): **3-5
    seconds between events.** First-chunk wait may be longer; once
    streaming starts, gaps are typically short.

### Listen for explicit completion signals when the API provides them

Always more reliable than time-based heuristics:
  - WebSocket: `response.done`, `agent_response_finished`, `[DONE]`
  - SSE: `data: [DONE]` token, custom finish event types
  - Polling: `status="completed"` / `"failed"` / `"cancelled"`

When the API has a known completion signal, watch for it and break
on receipt — don't rely solely on idle timeout.

### Background-thread pattern (WEBSOCKET ONLY — skip for SSE / polling / chunked HTTP)

For WebSocket harnesses where you need to send pings/keepalives or
audio frames in parallel with receiving, run `recv` in a background
daemon thread that pushes to a queue. The main loop drains the queue
with the same error-timeout + reset-on-event semantics — separating
producer pacing from response collection.

Don't apply this to non-WebSocket streams: SSE, chunked HTTP, and
polling APIs are single-direction (server → client) and the main loop
already handles them cleanly. Adding a thread there is complexity
without benefit.

### Trailing-input padding (provider-specific, watch for it)

Some providers' VAD requires a small amount of trailing silence on the
input stream to detect end-of-speech (otherwise they keep waiting for
more input and never start responding). When your provider docs mention
VAD or "voice activity detection", append ~1-1.5s of silence to your
audio before flipping to receive mode.
