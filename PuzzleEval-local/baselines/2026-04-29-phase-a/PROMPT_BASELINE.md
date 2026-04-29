# Prompt Baseline — captured 2026-04-29-phase-a

This file captures the FREE portion of the Phase 0 baseline: prompt-token counts and cross-render common-prefix size for the merge-gate combos. The Agent-5-build portion (build_cost, turn_count, scores) is captured separately in the same baselines directory.

## Per-combo metrics

| Combo | Platform | Test cases (input → output) | Chars | Tokens | Method |
|---|---|---|---:|---:|:---:|
| `linux_voice` | linux | voice_conversation->voice_conversation | 68,891 | 17,222 | estimate |
| `linux_ocr` | linux | document_content->structured_data | 54,059 | 13,514 | estimate |
| `windows_code` | win32 | text->code | 58,111 | 14,527 | estimate |
| `linux_chatbot_conversation` | linux | conversation->conversation | 58,109 | 14,527 | estimate |
| `linux_audio_content` | linux | audio_content->text | 68,891 | 17,222 | estimate |
| `windows_voice` | win32 | voice_conversation->voice_conversation | 68,893 | 17,223 | estimate |
| `macos_ocr` | darwin | document_content->structured_data | 54,059 | 13,514 | estimate |

Combo descriptions:

- `linux_voice` — Linux + voice_conversation — heavy modality (loads voice + streaming + live_test_voice)
- `linux_ocr` — Linux + OCR/document — no modality playbooks
- `windows_code` — Windows + code-gen — streaming only + Windows playbook
- `linux_chatbot_conversation` — Linux + plain text conversation (per coverage.py: should load streaming only; today over-loads voice + live_test_voice — Phase A fix)
- `linux_audio_content` — Linux + audio_content (OCR-of-audio-style)
- `windows_voice` — Windows + voice (combines all three voice playbooks with Windows playbook)
- `macos_ocr` — macOS + OCR — minimal playbook surface

## Cross-render common prefix (merge-gate metric)

Computed across the 3 combos ('linux_voice', 'linux_ocr', 'windows_code'): **11,890 chars / 2,972 tokens** (method = `estimate`).

This number measures how much of the rendered prompt is stable across OS + modality variations — directly correlated with Anthropic prompt-cache hit potential. Phase D's cache-invariant repair is expected to grow this number materially.

## Acceptance targets (post-Phase E)

- `prompt_tokens` should drop **≥30%** on at least 4 of the 5 baseline candidates' renders. Today's per-combo numbers above are the reference.
- Common-prefix tokens should rise (or stay flat) — never drop below today's 2,972.

## Notes

- Raw rendered prompts are in `runs/baseline-<date>/prompts/` (gitignored — they may contain provider names from playbook content).
- Token counts use Anthropic SDK's free `count_tokens` endpoint when an API key is available, otherwise a 4-chars-per-token estimate (consistent across baseline + post-refactor measurement).
- This file is meant to be re-generated with the same script after Phase E to produce the comparison `POST_REFACTOR.md`.
