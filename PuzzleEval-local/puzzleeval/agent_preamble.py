"""Shared cross-cutting preamble injected into every PuzzleEval agent.

Modeled on Claude Code's `enhanceSystemPromptWithEnvDetails()` /
`DEFAULT_AGENT_PROMPT` pattern (see `src/constants/prompts.ts` in the
Claude Code source). The same handful of behavioral rules apply to
every agent in our pipeline regardless of its specific job:

  - parallel tool calls when independent
  - no narration / preamble
  - reason about root cause on errors, don't pattern-match recipes
  - verify against the source (docs / code / actual response), not memory
  - commit to an approach, course-correct on signal — don't dither

Centralizing these rules has three benefits:
  1. New agents pick them up automatically — no copy-paste drift.
  2. Tuning the rules is one edit, not five.
  3. Tests can grep one location to verify cross-cutting principles
     are present in every agent's effective prompt.

The preamble is INTENTIONALLY short. Long preambles dilute attention
and crowd out agent-specific guidance. Claude Code's general-purpose
agent has ~30 lines of preamble; we hold to a similar budget.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# The preamble — ONE place, used everywhere
# ---------------------------------------------------------------------------

SHARED_AGENT_PREAMBLE = """## Cross-cutting rules (applies to every PuzzleEval agent)

These rules are about HOW you operate, not WHAT you produce. Your agent-specific job is described after this preamble.

**Tool calls.** When two or more tool calls do not depend on each other, batch them in a single response. Independent web searches, independent file reads, independent API checks — fire them in parallel. Sequential calls when one feeds the next.

**No narration.** Don't preface tool calls with "Let me ...". Don't summarize what you just did unless asked. The trace shows what happened.

**Reason about root cause on errors.** When a tool returns an error, identify what changed about the world that produced the error (wrong endpoint? expired credential? rate limit? schema drift?). Don't blindly retry the same call. Don't pattern-match a recipe from memory — read the actual error and act on its content.

**Verify against the source.** When the question is "does this API return X?" or "does this field exist?", read the actual docs / actual response / actual code. Do not rely on training-data memory of how the API used to work — APIs change. The cost of one fetch is much smaller than the cost of acting on a stale assumption.

**Commit and course-correct.** Don't dither between two approaches. Pick one based on the evidence, follow it, and reverse if signal says it's wrong. Indecision burns turns and budget without producing output.

**Concise output.** Match output length to the task. A yes/no question gets one sentence. A spec extraction gets the spec. Padding the response with context the caller already has is noise.

---
"""


# ---------------------------------------------------------------------------
# Composer — the only public function. Every agent imports this.
# ---------------------------------------------------------------------------


def with_preamble(agent_specific_prompt: str) -> str:
    """Prepend the shared cross-cutting preamble to an agent's system prompt.

    Use at module level when constructing the system prompt constant, OR at
    call time if the prompt is built dynamically. Idempotent: re-applying
    will not double-add (we look for a marker line).
    """
    if "Cross-cutting rules (applies to every PuzzleEval agent)" in agent_specific_prompt:
        return agent_specific_prompt
    return f"{SHARED_AGENT_PREAMBLE}\n{agent_specific_prompt}"


__all__ = ["SHARED_AGENT_PREAMBLE", "with_preamble"]
