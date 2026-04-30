# Phase 2 Prompt Baseline — captured 2026-04-29

FREE portion of Phase 2.0 baseline: prompt-token counts for Agents 1-4 templates + 6 auxiliary system-prompt string constants. The quality battery (Agent N quality metrics requiring real-API calls) is captured separately by `quality_battery.py`.

## Agent 1-4 templates (raw markdown)

| Surface | Chars | Tokens | Method | Description |
|---|---:|---:|:---:|---|
| `agent1_system_prompt` | 32,470 | 8,117 | estimate | Agent 1 (User Understanding) system prompt — 327 lines |
| `agent2_research_system` | 9,234 | 2,308 | estimate | Agent 2 (Research) research_system prompt — 100 lines |
| `agent2_structure_system` | 4,295 | 1,073 | estimate | Agent 2 (Research) structure_system prompt — 56 lines |
| `agent3_system_prompt` | 32,464 | 8,116 | estimate | Agent 3 (Synthetic Tests, text mode) system prompt — 523 lines (biggest) |
| `agent3f_system_prompt` | 7,838 | 1,959 | estimate | Agent 3F (Synthetic Tests, file mode) system prompt — 99 lines |
| `agent4_verification_system` | 13,173 | 3,293 | estimate | Agent 4 (Screening) verification_system prompt — 206 lines |
| `agent4_structure_system` | 3,396 | 849 | estimate | Agent 4 (Screening) structure_system prompt — 56 lines |

**Templates total: 102,870 chars / 25,715 tokens.**

## Auxiliary system-prompt string constants

| Surface | Chars | Tokens | Method | Description |
|---|---:|---:|:---:|---|
| `agent_preamble` | 1,622 | 405 | estimate | Shared cross-cutting preamble injected into every agent |
| `rubric_judge_template` | 3,609 | 902 | estimate | Rubric judge system prompt (stable + per-test composed) |
| `user_simulator_template` | 2,193 | 548 | estimate | User simulator system prompt template |
| `research_subagent_system` | 4,697 | 1,174 | estimate | Phase-2 ask_research sub-agent system prompt |
| `vision_judge_system` | 161 | 40 | estimate | Vision judge system prompt |
| `evaluation_system_prompt` | 1,168 | 292 | estimate | Agent 5 LLM judge system prompt (test-result evaluation) |

**Auxiliary total: 13,450 chars / 3,361 tokens.**

## Phase 2 acceptance targets

- `prompt_tokens` should drop **≥20%** on at least **4 of 7** agent templates after Phase 2D (looser than Phase 1's 30% target — Agents 1-4 prompts are smaller and less patch-heavy).
- Auxiliary system-prompt string lengths drop ≥10% on average.
- Quality battery (Agent N quality metrics) at parity or better — see `quality_battery.py` output.

## Notes

- Templates are loaded via `read_text(encoding='utf-8')` from `puzzleeval/agents/agent<N>/templates/`. No placeholder substitution; raw template tokens are the measurement (production rendering for Agents 1-4 uses these strings verbatim — they're not parameterized the way Agent 5's `__CONTRACT_BLOCK__` is).
- Auxiliary constants are imported live (via `importlib`) from their source files. The imported value is what gets sent to Claude as the `system` parameter.
- Token counts use Anthropic SDK's free `count_tokens` endpoint when an API key is available; otherwise a 4-chars-per-token estimate (consistent across baseline + post-refactor measurement).
- This file is meant to be re-generated with the same script after each Phase 2 sub-phase to produce a comparison.
