---
id: streaming_response
version: 2
title: Streaming Response Outcome Contract
category: modality
description: |
  Outcome contract for APIs whose answer arrives over multiple events or
  chunks. It defines what completion and progress evidence must be observed
  without prescribing event-loop code, timeout numbers, retries, or providers.
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
  - "A new outcome signal is needed for streaming completion."
---

## Streaming Response Outcome Contract

This contract defines what must be true when a provider emits a response over
time. It does not define how to implement the stream reader.

The implementation plan must identify:

- `interaction_pattern.known_family`, such as `continuous_stream`,
  `serialized_conversation`, `persistent_session`, or another documented
  provider shape.
- `interaction_pattern.state_owner`, either `provider_server` or
  `harness_process`.
- `interaction_pattern.input_clocking`, meaning what event or request marks a
  user turn as submitted.
- `interaction_pattern.output_completion_signal`, meaning what observable event,
  status, payload, or idle condition proves the provider has finished answering.
- Cleanup responsibility for open sessions, workers, files, sockets, or streams.

Required observable outcome:

- Output-bearing events are distinguishable from transport keepalives and
  metadata.
- Completion is proven by an explicit provider completion signal when one
  exists, or by a documented fallback condition when no explicit signal exists.
- Keepalive-only traffic does not count as agent output.
- Partial chunks are assembled into the same result shape that production
  evaluation consumes.
- Failures include enough evidence to tell whether the provider was blocked,
  silent, still processing, or returning malformed output.

Runtime boundary:

- Streaming semantics are not runtime primitives.
- `provider_server` state can run through `single_call` when the provider
  preserves the conversation or job identity.
- `harness_process` state requires `persistent_worker` while the persistent-worker
  runtime flag is enabled.
