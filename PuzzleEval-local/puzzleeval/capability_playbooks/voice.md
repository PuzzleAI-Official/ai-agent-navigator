---
id: voice
version: 1
title: Voice Harness Return Shape Contract
category: modality
description: |
  Teaches the harness builder how to return audio responses for voice/audio
  test cases. Two valid return shapes (Shape A: inline audio_bytes; Shape B:
  on-disk audio_path). Required when test cases have voice_conversation,
  voice_turn, or audio_content modalities. Paired with VoiceHarnessGate
  which validates raw_response shape at runtime.
selectors:
  trigger_types:
    - voice_conversation
    - voice_turn
    - audio_content
    - conversation
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
  - "Audio shape diverges from current 2 supported types (audio_bytes / audio_path)"
  - "Provider-specific quirks emerge that this teaching doesn't cover"
---

## Voice harness return-shape contract (REQUIRED when test has voice / audio / conversation modality)

The voice plugin (`tool_plugins/voice_realtime.py`) drives multi-turn
conversations by calling your harness once per turn and extracting the
agent's audio response from `raw_response`. It understands EXACTLY TWO
return shapes. Any other shape — `audio_url`, `audio_base64_string`,
`audio_data`, a custom schema — will silently fall through, zero agent
audio reaches the report, and every test scores 0.

Pick ONE shape per harness. Do NOT mix. Do NOT invent new keys.

### Shape A — inline audio bytes (preferred for responses under ~5 MB)

```python
def run(input_data):
    ...
    return {
        "output": "agent transcript (optional)",
        "latency_ms": elapsed_ms,
        "tokens_used": None,
        "cost_usd": None,
        "raw_response": {
            "audio_bytes": <bytes>,      # raw audio bytes — the plugin base64-decodes
                                          # automatically if you pass a b64 STRING instead
            "audio_format": "mp3",       # "mp3" | "wav" | "ogg" | "m4a" | "webm" | "flac" | "pcm16"
            "audio_sample_rate": 24000,  # REQUIRED when format="pcm16"; ignored otherwise
            "audio_channels": 1,         # REQUIRED when format="pcm16"; ignored otherwise
            "audio_content_type": "audio/mpeg",  # OPTIONAL; inferred from audio_format when absent
            "transcript": "optional text",
        },
        "success": True,
        "error": None,
    }
```

### Shape B — on-disk audio file path (preferred for large responses OR when you transcode via ffmpeg/pydub and it's already on disk)

```python
def run(input_data):
    ...
    # Write audio to a temp file (tempfile.NamedTemporaryFile, AudioSegment.export, ...)
    temp_path = "/tmp/response_abc.wav"
    return {
        "output": "agent transcript (optional)",
        "latency_ms": elapsed_ms,
        "raw_response": {
            "audio_path": temp_path,  # absolute path to a readable audio file
                                       # Extension determines format: .wav .mp3 .ogg .m4a .webm .flac
            "transcript": "optional text",
        },
        "success": True,
        "error": None,
    }
```

### HARD RULES — the plugin silently breaks otherwise

1. `raw_response` must contain EITHER `audio_bytes` OR `audio_path`. Never both.
2. When you have raw PCM16 samples (no container header — OpenAI Realtime,
   ElevenLabs Realtime, most telephony), use Shape A with
   `audio_format="pcm16"` + sample_rate + channels. The plugin transcodes
   to MP3 / WAV automatically.
3. When you have a known audio container (MP3 / WAV / OGG / M4A), either
   shape works. Shape B is slightly cheaper (no base64 round-trip).
4. DO NOT return `audio_url`, `audio_b64`, `audio_data`, `audio`,
   `audio_file`, or any other key name — they will NOT be parsed.
5. DO NOT put audio under a nested key like `raw_response["data"]["audio"]`.
   The plugin reads `raw_response.audio_bytes` and `raw_response.audio_path`
   only at the top level.
6. When the API returns text-only (no audio — rare but valid for voice
   APIs that can degrade to text), just omit BOTH audio keys. The plugin
   falls back to the text path.
7. For multi-turn sessions, the PLUGIN passes `session_state` from
   turn N → turn N+1. **Nothing more.** THE HARNESS owns provider
   continuity:
     - On turn_index=0: open the WebSocket / get the conversation_id /
       initialize whatever provider-side state your API uses.
       **STORE the connection handle (or conversation_id, or message
       history list) INSIDE `session_state`** — that dict is
       mutated-in-place and survives to the next turn.
     - On turn_index>0: **READ from session_state and REUSE.** Do NOT
       open a fresh WebSocket / start a new conversation / drop the
       message history. The agent depends on your stored state to
       remember turn 0.

   Skipping this causes the **"agent introduces itself every turn"**
   bug — fresh provider session per harness call → agent has zero
   memory of prior turns → every multi-turn test scores near zero
   regardless of agent quality.

   The plugin CANNOT do this for you. It doesn't know whether your
   provider needs a WebSocket handle, a conversation_id header, an
   accumulated messages list, or some custom session token. Only the
   HARNESS knows. Read your API docs for the session lifecycle and
   thread it explicitly.

### What the plugin does with each shape (so you can verify your harness output)

Both shapes route through `_save_audio_blob(turn_token, bytes, ctype)`,
which writes `response_<token>_<hex>.<ext>` to the session dir that
appears as `runs/<trace_id>/harnesses/<slug>/voice/`. Those files are
then served by `/pzapi/runs/audio?path=...` for UI playback and fed
through the transcription plugin for scoring.

If your harness builds correctly but the report shows zero agent audio,
you violated this contract. Re-read the shapes above.
