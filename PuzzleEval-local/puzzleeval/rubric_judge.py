"""Rubric judge — LLM-powered quality scoring for conversation transcripts.

Takes a FULL multi-turn conversation and scores it against a rubric of
weighted criteria (goal_completion, accuracy_no_hallucination,
policy_compliance, etc). Returns a RubricVerdict with per-criterion
scores, reasoning, evidence turn indices, critical-gate failures, and
an overall weighted score.

Complementary to ``puzzleeval.user_simulator`` — simulator drives the
turns, judge evaluates the outcome. Both are used by voice_realtime and
conversation_simulator plugins' agentic-mode paths.

Design notes:
  - Sonnet 4.6 is the default model — higher quality than Haiku, which
    matters for this task. Judge mistakes directly corrupt the score
    surfaced to the user, so we don't skimp here.
  - ``parse_with_fallback`` handles the grammar-compilation edge. The
    judge's output is structured (RubricVerdict schema); any failure
    degrades to non-strict tool mode automatically.
  - Adaptive thinking is opt-in. Default judging uses a rubric-specific
    timeout/retry budget because voice runs should not keep provider
    sessions open while the judge thinks.
  - Critical-gate semantics are enforced CODE-SIDE, not just in the
    prompt. The judge emits per-criterion scores + critical_failures;
    this module post-processes to compute `passed` deterministically:
    `passed = overall_score >= 0.5 AND no critical_failures`. That
    way a confused judge can't accidentally let a hallucinating
    agent pass by marking overall=0.6 while scoring accuracy=0.2.
  - Cost per call: ~$0.015 for a 6-turn transcript on Sonnet 4.6.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import anthropic

from puzzleeval.anthropic_client import build_client
from puzzleeval.logging_setup import log_llm_call
from puzzleeval.schemas import (
    ConversationTurn,
    Persona,
    RubricCriterion,
    RubricScore,
    RubricVerdict,
)
from puzzleeval.structured_output import parse_with_fallback

logger = logging.getLogger("puzzleeval.rubric_judge")


# ============================================================================
# Configuration
# ============================================================================


def _resolve_config() -> tuple[str, int]:
    """Return (model, max_tokens) from config (env-overridable)."""
    from puzzleeval.config import (
        RUBRIC_JUDGE_MODEL,
        RUBRIC_JUDGE_MAX_TOKENS,
    )
    return RUBRIC_JUDGE_MODEL, RUBRIC_JUDGE_MAX_TOKENS


def _build_bounded_judge_client() -> anthropic.Anthropic:
    """Build the judge client with the rubric-specific timeout policy.

    The general Anthropic client allows longer server-tool calls used by
    research/build agents. Transcript judging is different: it has no server
    tools and should fail at the test level rather than occupy an evaluation
    worker for many minutes. Keeping this helper here makes the timeout policy
    a single rubric-judge contract instead of a voice-plugin special case.
    """

    from puzzleeval.config import (
        RUBRIC_JUDGE_MAX_RETRIES,
        RUBRIC_JUDGE_TIMEOUT_S,
    )

    return build_client(
        timeout=RUBRIC_JUDGE_TIMEOUT_S,
        max_retries=RUBRIC_JUDGE_MAX_RETRIES,
    )


# ============================================================================
# System prompt construction
# ============================================================================
#
# The system prompt is split into TWO blocks for prompt-cache efficiency:
#
#   Block 1 (STABLE — eligible for cache_control: ephemeral):
#     Instructional skeleton that's IDENTICAL across every rubric judge
#     call within a run: "## YOUR TASK", "## HOW TO SCORE — CRITICAL RULES",
#     "## OUTPUT FORMAT". ~2K tokens. With 14 rubric judge calls per run
#     (7 tests × 2 candidates) and Sonnet 5-min cache TTL refreshed on
#     every read, this block writes ONCE per 5-min window then reads on
#     every subsequent call. Saves ~$0.07/run vs sending fresh each time.
#
#   Block 2 (PER-TEST):
#     The CONVERSATION-SPECIFIC content: caller persona, goal, agent's
#     official instructions, rubric, transcript. Different on every call,
#     so caching it would be a NET LOSS (1.25× cache write penalty
#     without the read benefit). Sent fresh.
#
# Per-test content lives in the SYSTEM (not the user message) because
# the model produces better critical-gate decisions when the rubric is
# in its instructional context, not its turn-by-turn input.
# ============================================================================


_JUDGE_SYSTEM_STABLE = """You are an expert evaluator scoring a recorded conversation between a caller and an AI agent. The per-test details (caller persona, goal, agent's official instructions, rubric criteria, and transcript) are provided in the second system block below.

## YOUR TASK

Read the full transcript and score the agent's performance across the rubric criteria. For EACH criterion:
  1. Assign a score from 0.0 (completely fails) to 1.0 (fully satisfies)
  2. Write 1-3 sentences explaining the score, citing specific turn indices where possible (e.g., "Agent gave correct price in turn 2 but contradicted it in turn 4.")
  3. List the turn indices (0-based) that support your score, if any

Then compute overall_score as the weighted mean of criterion scores, using the weights from the rubric.

Finally, write a 2-3 sentence conversation_summary that captures: what the user wanted, what the agent did, whether it landed.

## HOW TO SCORE — CRITICAL RULES

**Score CONTENT and BEHAVIOR, not fluency.** A terse but correct answer is 1.0. A verbose but wrong answer is 0. Eloquence is never the test.

**"I don't know" is often 1.0.** An agent that CORRECTLY declines to answer (e.g., "I don't have that information but can transfer you") scores HIGHER on accuracy than one that confidently fabricates.

**Hallucination is a hard failure — check against THE AGENT'S OFFICIAL INSTRUCTIONS in the per-test block.** When scoring `accuracy_no_hallucination` / `policy_compliance` / `scope_adherence`, the agent's instructions are ground truth:
  - If the instructions list a pricing menu, any price the agent quotes OUTSIDE that menu is a hallucination (score accuracy low).
  - If the instructions specify a service area, anything offered outside that area is scope violation (score scope_adherence low).
  - If the instructions define escalation policies, failing to escalate when required is policy failure.
  - If the instructions ARE EMPTY or ABSENT, judge generically: did the agent stay consistent with itself + stay plausible for the domain? Don't penalize the agent for violating rules that were never given.

**Goal completion is BINARY at the logical level.** Did the caller leave with what they came for? Partial ≤ 0.5; genuinely mid-flow ≤ 0.7; clear resolution (booked, got the answer, received the confirmation) ≥ 0.85.

**Critical-gate criteria override overall pass.** Any criterion the rubric flags as `critical=True` that you score below its `min_passing_score` (default 0.5) — add its name to `critical_failures`. The pipeline will fail the test regardless of overall_score.

**Conversation continuity is core behavior.** For voice/phone or chatbot
rubrics that include continuity, repeated first-turn behavior after turn
0 is a serious failure: do not treat repeated greetings, repeated
self-introductions, or asking again for already-provided facts as minor
style issues. Score continuity low unless the agent is explicitly
confirming information.

**Do NOT inflate scores.** An agent that was "mostly okay" is 0.6, not 0.8. Reserve 0.85+ for agents that would genuinely satisfy this specific caller in a real interaction.

## OUTPUT FORMAT

Emit a structured RubricVerdict with:
  - overall_score: weighted mean of criterion scores, computed by you
  - passed: set based on your judgment (the pipeline will re-compute deterministically)
  - criterion_scores: one entry per rubric criterion, IN THE SAME ORDER as the rubric spec
  - conversation_summary: 2-3 sentences
  - critical_failures: list of criterion names (strings) that triggered critical-gate failure
"""


_JUDGE_SYSTEM_PER_TEST_TEMPLATE = """## CANDIDATE ROLE
{candidate_role}

## WHO THE CALLER IS
Name: {persona_name}
Background: {persona_demographics}
Emotional state: {persona_emotional_state}

## WHAT THE CALLER WANTED
{goal}

## THE AGENT'S OFFICIAL INSTRUCTIONS (ground truth for scope + policy)
{agent_instructions_block}

{rubric_block}

## THE TRANSCRIPT

Below is the conversation, turn-by-turn. Turn indices are 0-based.

{transcript_block}
"""


# Back-compat: the original template (single-string) is preserved for any
# external caller that imports it directly. Internal flow now uses the
# two-block split via `_build_system_prompt_blocks` which `judge_conversation`
# calls.
_JUDGE_SYSTEM_TEMPLATE = _JUDGE_SYSTEM_STABLE + "\n\n" + _JUDGE_SYSTEM_PER_TEST_TEMPLATE


def _format_rubric_spec(rubric: list[RubricCriterion]) -> str:
    """Format the rubric criteria as a readable spec block for the judge."""
    if not rubric:
        return "(no rubric provided — score on general helpfulness + accuracy)"
    lines = ["## RUBRIC (score each criterion with reasoning)"]
    for i, crit in enumerate(rubric):
        critical_marker = ""
        if crit.critical:
            threshold = crit.min_passing_score if crit.min_passing_score is not None else 0.5
            critical_marker = f" [CRITICAL — veto pass if score < {threshold}]"
        lines.append(
            f"\n{i + 1}. **{crit.name}** (weight {crit.weight}){critical_marker}"
        )
        lines.append(f"   {crit.description.strip()}")
    return "\n".join(lines)


def _format_transcript(turns: list[ConversationTurn]) -> str:
    """Format transcript turns as 'Turn N [role]: text'."""
    if not turns:
        return "(empty transcript — conversation never started)"
    lines = []
    for turn in turns:
        role_label = "Caller" if turn.role == "user" else "Agent"
        text = turn.text.strip() or "(silence / empty turn)"
        lines.append(f"Turn {turn.turn_index} [{role_label}]:\n{text}\n")
    return "\n".join(lines)


def _format_agent_instructions_block(agent_system_prompt: str | None) -> str:
    """Render the "agent's official instructions" block for the judge.

    When `agent_system_prompt` is a non-empty string, it's quoted verbatim
    — the judge treats it as ground truth for scope/policy. When empty
    or None, we emit a neutral "no instructions configured" line so the
    judge scores generically instead of penalizing the agent for
    violating rules that were never given.

    AD-007 alignment: the judge must KNOW what the agent was told so
    rubric scoring is grounded in contract, not guesswork. Prior version
    forced the judge to guess what "in scope" meant; we've seen cases
    where the judge penalizes an agent for staying on-topic because it
    guessed the wrong topic.
    """
    if agent_system_prompt and agent_system_prompt.strip():
        return (
            "```\n"
            + agent_system_prompt.strip()
            + "\n```\n\n"
            "Score `scope_adherence`, `policy_compliance`, and any "
            "price/fact-accuracy criteria against these instructions "
            "as the canonical contract. If the instructions are silent "
            "on a topic, don't invent rules — score that dimension "
            "against general plausibility for the domain instead."
        )
    return (
        "(No system prompt was provided to the agent for this test. "
        "Judge the agent's behavior against general plausibility and "
        "internal consistency — do NOT penalize scope violations that "
        "weren't defined by any instruction.)"
    )


def _build_system_prompt(
    *,
    persona: Persona,
    goal: str,
    rubric: list[RubricCriterion],
    transcript: list[ConversationTurn],
    candidate_role: str,
    agent_system_prompt: str | None = None,
) -> str:
    """Back-compat string assembly of the full judge system prompt.

    Concatenates the stable instructional skeleton + the per-test
    context into one string. Kept for tests / downstream callers that
    expect a string. Internal flow uses
    `_build_system_prompt_blocks` which returns the cache-aware
    two-block list passed to client.beta.messages.create.
    """
    per_test = _build_per_test_block(
        persona=persona,
        goal=goal,
        rubric=rubric,
        transcript=transcript,
        candidate_role=candidate_role,
        agent_system_prompt=agent_system_prompt,
    )
    return _JUDGE_SYSTEM_STABLE + "\n\n" + per_test


def _build_per_test_block(
    *,
    persona: Persona,
    goal: str,
    rubric: list[RubricCriterion],
    transcript: list[ConversationTurn],
    candidate_role: str,
    agent_system_prompt: str | None = None,
) -> str:
    """Render the per-test variable content of the judge system prompt.

    Excludes the stable instructional skeleton — that lives in
    `_JUDGE_SYSTEM_STABLE` and is sent as a separate (cache-eligible)
    block. Used by `_build_system_prompt_blocks` to assemble the
    cache-aware two-block list.
    """
    return _JUDGE_SYSTEM_PER_TEST_TEMPLATE.format(
        candidate_role=candidate_role or "voice/chat",
        persona_name=persona.name,
        persona_demographics=persona.demographics,
        persona_emotional_state=persona.emotional_state,
        goal=goal.strip(),
        agent_instructions_block=_format_agent_instructions_block(agent_system_prompt),
        rubric_block=_format_rubric_spec(rubric),
        transcript_block=_format_transcript(transcript),
    )


def _build_system_prompt_blocks(
    *,
    persona: Persona,
    goal: str,
    rubric: list[RubricCriterion],
    transcript: list[ConversationTurn],
    candidate_role: str,
    agent_system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Build the cache-aware two-block system prompt for judge_conversation.

    Block 0: stable instructional skeleton with cache_control: ephemeral.
             Identical across every rubric judge call within a 5-min
             window — caches once, reads cheap on every subsequent call.

    Block 1: per-test context (persona, goal, agent instructions, rubric,
             transcript). Different on every call — sent fresh.

    Caller passes the returned list directly as `system=` to the
    Anthropic SDK. The 5-min cache TTL is refreshed on every read,
    so as long as rubric judge calls within a single pipeline run
    happen within 5 min of each other (they do), the stable block
    stays warm.

    Per-run saving: ~$0.07 on Sonnet 4.6 across 14 rubric judge calls.
    Tiny per-call but real and free.
    """
    per_test = _build_per_test_block(
        persona=persona,
        goal=goal,
        rubric=rubric,
        transcript=transcript,
        candidate_role=candidate_role,
        agent_system_prompt=agent_system_prompt,
    )
    return [
        {
            "type": "text",
            "text": _JUDGE_SYSTEM_STABLE,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": per_test,
        },
    ]


# ============================================================================
# Verdict post-processing (deterministic critical-gate enforcement)
# ============================================================================


def _compute_overall_score(
    scores: list[RubricScore],
    rubric: list[RubricCriterion],
) -> float:
    """Weighted mean of criterion scores using the rubric spec's weights.

    Re-computed deterministically rather than trusting the judge's own
    overall_score — models occasionally miscompute the weighted mean.
    When a criterion is present in the rubric but missing from the
    judge's scores (rare), we skip it — weights auto-renormalize.
    """
    if not scores:
        return 0.0
    # Index rubric weights by criterion name for lookup
    weights_by_name = {c.name: c.weight for c in rubric}
    total_weight = 0.0
    weighted_sum = 0.0
    for score in scores:
        weight = weights_by_name.get(score.criterion_name, 1.0 / max(len(scores), 1))
        weighted_sum += score.score * weight
        total_weight += weight
    if total_weight == 0.0:
        return 0.0
    return round(weighted_sum / total_weight, 4)


def _compute_critical_failures(
    scores: list[RubricScore],
    rubric: list[RubricCriterion],
) -> list[str]:
    """Determine which critical-gate criteria scored below their threshold.

    This is the SAFETY CONTRACT enforcement point — we don't trust the
    judge's self-reported critical_failures list. We re-derive from the
    score values + rubric spec. Matches AD-007: safety contracts in
    deterministic code, not prompts.
    """
    failures: list[str] = []
    scores_by_name = {s.criterion_name: s.score for s in scores}
    for crit in rubric:
        if not crit.critical:
            continue
        threshold = crit.min_passing_score if crit.min_passing_score is not None else 0.5
        score = scores_by_name.get(crit.name)
        if score is None:
            # Judge forgot to score a critical criterion — treat as failure.
            # Better to false-flag than silently pass.
            failures.append(crit.name)
            continue
        if score < threshold:
            failures.append(crit.name)
    return failures


def _finalize_verdict(
    judge_verdict: RubricVerdict,
    rubric: list[RubricCriterion],
    cost_usd: float,
) -> RubricVerdict:
    """Post-process the judge's verdict for deterministic correctness.

    Three fixups:
      1. Recompute overall_score from per-criterion scores + spec weights
         (don't trust the model's arithmetic).
      2. Recompute critical_failures from per-criterion scores + spec
         critical/min_passing_score flags (don't trust the model's
         self-assessment on safety gates).
      3. Recompute passed = (overall_score >= 0.5 AND no critical_failures).
         The model's self-reported `passed` field is ignored.
      4. Set the cost_usd that was tracked by the caller.
    """
    overall = _compute_overall_score(judge_verdict.criterion_scores, rubric)
    criticals = _compute_critical_failures(judge_verdict.criterion_scores, rubric)
    passed = overall >= 0.5 and len(criticals) == 0

    return RubricVerdict(
        overall_score=overall,
        passed=passed,
        criterion_scores=judge_verdict.criterion_scores,
        conversation_summary=judge_verdict.conversation_summary,
        critical_failures=criticals,
        cost_usd=round(cost_usd, 6),
    )


# ============================================================================
# Public API
# ============================================================================


def judge_conversation(
    *,
    transcript: list[ConversationTurn],
    persona: Persona,
    goal: str,
    rubric: list[RubricCriterion],
    candidate_role: str = "",
    agent_system_prompt: str | None = None,
    client: anthropic.Anthropic | None = None,
    trace_id: str = "no-trace",
    model: str | None = None,
) -> RubricVerdict:
    """Score a conversation transcript against a rubric.

    Args:
        transcript: Full turn-by-turn conversation as ConversationTurn list.
            Empty transcript returns a 0.0 verdict with reason.
        persona: Who the simulated caller was (for context — the judge
            needs to understand the user's perspective to judge well).
        goal: What the caller wanted. Drives goal_completion scoring.
        rubric: Weighted criteria to score. Empty rubric returns a 0.0
            verdict with reason.
        candidate_role: Human-readable role description from the scope
            (e.g., "plumbing dispatch voice agent"). Grounds the judge.
        agent_system_prompt: The system prompt the candidate agent was
            ACTUALLY given during the conversation (from
            TestCase.input_context.instructions). When present, the
            judge treats it as ground truth for scope/policy scoring:
            quoted prices must match; service area limits must be
            honored; escalation rules must be followed. When None or
            empty, the judge falls back to general plausibility (no
            rule penalties for rules that weren't defined). Passing
            this closes the blind-spot that caused judges to penalize
            on guessed rules. See AD-007.
        client: Anthropic client (built via build_client() when None).
        trace_id: Correlation ID for structured logs.
        model: Override default RUBRIC_JUDGE_MODEL.

    Returns:
        RubricVerdict with deterministically-computed overall_score +
        critical_failures (see _finalize_verdict for enforcement logic).

    Raises:
        anthropic.APIError: on persistent failure after SDK retries.
            Callers should catch + mark test as skipped with eval_error
            rather than letting it crash the whole run.
    """
    # Empty-input fast paths — don't burn an API call on degenerate inputs.
    if not transcript:
        return RubricVerdict(
            overall_score=0.0, passed=False,
            criterion_scores=[],
            conversation_summary="Conversation never started — no transcript to evaluate.",
            critical_failures=[c.name for c in rubric if c.critical],
            cost_usd=0.0,
        )
    if not rubric:
        return RubricVerdict(
            overall_score=0.0, passed=False,
            criterion_scores=[],
            conversation_summary="No rubric provided — nothing to score against.",
            critical_failures=[],
            cost_usd=0.0,
        )

    agent_turns = [t for t in transcript if t.role == "agent"]
    if not any((t.text or "").strip() and not (t.text or "").startswith("[AGENT ERROR:") for t in agent_turns):
        return RubricVerdict(
            overall_score=0.0,
            passed=False,
            criterion_scores=[
                RubricScore(
                    criterion_name=c.name,
                    score=0.0,
                    reasoning=(
                        "Deterministic precheck: no substantive agent "
                        "response was present."
                    ),
                    evidence_turn_indices=[
                        t.turn_index for t in transcript if t.role == "agent"
                    ],
                )
                for c in rubric
            ],
            conversation_summary=(
                "Deterministic precheck failed: the transcript contains "
                "no substantive agent response to judge."
            ),
            critical_failures=[c.name for c in rubric if c.critical],
            cost_usd=0.0,
        )

    model_to_use, max_tokens = _resolve_config()
    if model is not None:
        model_to_use = model

    # Cache-aware two-block system prompt (item 1b of
    # PLAN_VOICE_RUN_OPTIMIZATIONS deep-dive). Block 0 is the stable
    # instructional skeleton with cache_control: ephemeral; Block 1 is
    # the per-test variable content (persona/goal/rubric/transcript).
    # Across 14 rubric judge calls per run, the stable block writes
    # once + reads 13× — saves ~$0.07/run vs the prior single-string
    # approach that re-sent the skeleton fresh on every call.
    system_blocks = _build_system_prompt_blocks(
        persona=persona, goal=goal, rubric=rubric,
        transcript=transcript, candidate_role=candidate_role,
        agent_system_prompt=agent_system_prompt,
    )

    if client is None:
        client = _build_bounded_judge_client()

    start = time.time()

    # Keep transcript judging bounded by default. Adaptive thinking remains
    # opt-in for difficult rubrics.
    try:
        from puzzleeval.config import (
            RUBRIC_JUDGE_ADAPTIVE_THINKING_ENABLED,
            output_config_for_request,
        )
        ocfg = output_config_for_request()
    except Exception:  # pragma: no cover — config edge
        ocfg = None
        RUBRIC_JUDGE_ADAPTIVE_THINKING_ENABLED = False

    extra: dict[str, Any] = {}
    if RUBRIC_JUDGE_ADAPTIVE_THINKING_ENABLED:
        extra["thinking"] = {"type": "adaptive"}
    if ocfg:
        extra["output_config"] = ocfg

    # Build the "user message" — a short prompt that anchors the task.
    # The heavy content (persona, rubric, transcript) is in the system.
    user_msg = (
        "Score this conversation against the rubric. Output a "
        "RubricVerdict. Recompute overall_score as the weighted mean. "
        "List critical_failures for any critical criterion you scored "
        "below its threshold."
    )

    response = parse_with_fallback(
        client=client,
        model=model_to_use,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user_msg}],
        output_format=RubricVerdict,
        extra=extra,
        trace_id=trace_id,
        fallback_tool_name="emit_rubric_verdict",
        transient_max_attempts=1,
    )

    cost_usd = log_llm_call(
        logger, response, model_to_use, trace_id, start,
        operation="rubric_judge",
    )

    # JSON-truncation tolerance: when the judge's response hits max_tokens
    # mid-string, `response.parsed_output` raises a pydantic validation
    # error ("Invalid JSON: EOF while parsing a string at line 1 column
    # X"). Real-run trace 0c7f085f (2026-04-23) hit this twice, crashing
    # those test cases' rubric verdicts entirely.
    #
    # Defensive general contract: if the judge's output is unparseable
    # (truncation, malformed JSON, any schema validation error), return
    # a zero-score verdict with a clear failure reason rather than
    # crashing the whole test. The caller logs the verdict and moves
    # on to the next test. This is a TEST-LEVEL safety net — the
    # first line of defense is the max_tokens bump in config.py.
    try:
        judge_verdict: RubricVerdict = response.parsed_output
    except Exception as parse_exc:  # noqa: BLE001
        stop_reason = getattr(response, "stop_reason", "unknown")
        raw_text = ""
        try:
            # Best-effort extraction of whatever text we did get, for
            # logging — helps ops diagnose whether the judge was close
            # to valid JSON or garbled.
            for block in getattr(response, "content", []) or []:
                if getattr(block, "type", "") == "text":
                    raw_text += getattr(block, "text", "")
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "rubric_judge output unparseable — returning zero-score "
            "fallback verdict (stop_reason=%s, error=%s)",
            stop_reason, parse_exc,
            extra={
                "operation": "rubric_judge_parse_fallback",
                "trace_id": trace_id,
                "stop_reason": stop_reason,
                "raw_text_preview": raw_text[:300],
                "error_type": type(parse_exc).__name__,
            },
        )
        truncation_hint = (
            " (response hit max_tokens cap)"
            if stop_reason == "max_tokens"
            else ""
        )
        return RubricVerdict(
            overall_score=0.0,
            passed=False,
            criterion_scores=[],
            conversation_summary=(
                f"Rubric judge output was unparseable{truncation_hint}. "
                f"{type(parse_exc).__name__}: {str(parse_exc)[:150]}"
            ),
            critical_failures=[c.name for c in rubric if c.critical],
            cost_usd=cost_usd,
        )

    # Deterministic post-processing — don't trust the judge's own overall
    # score or critical-failure self-assessment.
    return _finalize_verdict(judge_verdict, rubric, cost_usd)


__all__ = (
    "judge_conversation",
    # exposed for tests
    "_compute_overall_score",
    "_compute_critical_failures",
    "_finalize_verdict",
    "_format_rubric_spec",
    "_format_transcript",
    "_build_system_prompt",
)
