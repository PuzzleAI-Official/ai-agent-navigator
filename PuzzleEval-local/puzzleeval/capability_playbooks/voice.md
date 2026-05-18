---
id: voice
version: 2
title: Voice Harness Outcome Contract
category: modality
description: |
  Outcome contract for voice/audio harnesses. It defines the response evidence
  the evaluator must be able to observe without prescribing provider code,
  event loops, retries, or session implementation details.
selectors:
  trigger_types:
    - voice_conversation
    - voice_turn
    - audio_content
selection_mode: deterministic
priority: 100
applies_to_agents:
  - agent_5
paired_gates:
  - voice_harness
related_contracts:
  - streaming_response
  - live_test_voice
revisit_when:
  - "The evaluator accepts a new audio evidence key."
  - "Runtime primitives change beyond single_call and persistent_worker."
---

## Voice Harness Outcome Contract

This contract defines what must be true about a voice harness result. It does
not define how to talk to any provider.

Required observable outcome:

- Each successful voice result returns `success=True`, an optional text
  transcript in `output`, and audio evidence in `raw_response`.
- Audio evidence uses exactly one supported top-level key:
  `raw_response.audio_bytes` for inline audio or `raw_response.audio_path`
  for a readable local audio file.
- Audio metadata is present when needed to make the bytes playable:
  `audio_format`, plus sample rate and channel count for raw PCM.
- `raw_response.transcript` may provide the agent transcript, but transcript
  text alone is not audio evidence.
- Unsupported aliases such as `audio_url`, `audio_data`, `audio_file`, nested
  audio payloads, or custom audio schemas do not satisfy the contract.

Continuity outcome:

- The implementation plan declares who owns conversational state:
  `provider_server` or `harness_process`.
- When state ownership is `provider_server`, the harness result must include
  enough evidence to show the provider-side conversation identity or equivalent
  server-held context is reused.
- When state ownership is `harness_process`, production evaluation must use
  the `persistent_worker` runtime primitive so in-memory state can survive
  across turns.
- If persistent workers are disabled, voice continuity must be reported as
  degraded rather than claimed as production-equivalent.

Evidence expected by reports and gates:

- Per-turn agent audio is saved or saveable by the evaluator.
- Multi-turn transcripts distinguish caller and agent turns.
- Forensics, when available, show session creation on the first turn and reuse
  or equivalent continuity evidence on later turns.
- A voice harness that produces no agent audio can pass neither live-test
  evidence nor user-facing report truth, even if it returns text.
