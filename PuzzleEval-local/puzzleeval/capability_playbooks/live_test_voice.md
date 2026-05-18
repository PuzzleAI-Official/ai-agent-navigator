---
id: live_test_voice
version: 2
title: Voice Live-Test Outcome Contract
category: modality
description: |
  Outcome contract for voice and multi-turn live tests. It defines the evidence
  live_test.py must produce to prove production equivalence and task equivalence
  without prescribing a runner implementation.
selectors:
  trigger_types:
    - voice_conversation
    - voice_turn
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
  - "Production payload shape changes."
  - "Runtime primitives change beyond single_call and persistent_worker."
---

## Voice Live-Test Outcome Contract

`live_test.py` must prove the built harness works under the same observable
conditions production evaluation will use. It should not be a separate toy path.

Production-equivalence evidence:

- The live test uses the same input concepts production uses for voice turns:
  caller audio, turn index, safe input context, and conversation history.
- The live test uses the runtime primitive selected from
  `implementation_plan.json`: `single_call` for provider-held state or
  `persistent_worker` for harness-process-held state.
- If persistent workers are disabled, the live test records that continuity is
  degraded instead of claiming full equivalence.
- The live test proves cleanup or release of any worker/session resources it
  creates.

Task-equivalence evidence:

- Caller audio contains intelligible task speech, not silence or transport-only
  audio.
- The task content matches the objective, test cases, and business fixture when
  present.
- At least two turns are exercised for multi-turn voice or conversation
  objectives.
- Each exercised turn returns `success=True` and observable agent audio using
  the voice outcome contract.
- Later turns prove continuity through transcript, forensics, or provider/server
  identity evidence. A repeated first-turn greeting without continuity evidence
  is a warning.

HARNESS_COMPLETE evidence:

- `smoke_test.py` proves basic structure.
- `live_test.py` proves production equivalence and task equivalence.
- `live_test.py` should write `_agent_state/live_test_evidence.json` when
  practical. Include per-turn input payload shape, caller input provenance,
  success/error, transcript/output, audio artifact path, session/continuity
  evidence, and the task objective/assertion for that turn.
- The report can point to per-turn audio or merged conversation audio.
- Any provider block, missing credential, quota failure, or unsupported runtime
  degradation is explicit in the evidence rather than hidden behind a passing
  toy live test.
