"""User simulator — LLM-driven simulated caller for multi-turn conversation tests.

Generates the NEXT user utterance in an ongoing conversation, given:
  - A Persona (who the user is)
  - A Goal (what the user wants)
  - Constraints (simulator-side behavior rules)
  - The conversation history so far

Replaces the static turn_script pattern in voice_realtime / conversation_
simulator. The key behavioral property: the simulator ADAPTS to what the
agent actually said in the previous turn (asking clarifying questions,
pushing back on wrong answers, thanking + ending when the goal is met).

Design notes:
  - Haiku 4.5 is the default model — reactive enough, 10x cheaper than
    Sonnet. The quality bar for user turns is "plausible caller utterance",
    not "deep reasoning", so Haiku is well-matched.
  - Temperature defaults to 0.3 — non-zero for natural variation, low
    enough that the same (persona, goal, history) produces similar
    responses across runs (property-test-friendly).
  - The `<END_CALL>` sentinel is the primary end-of-conversation signal.
    The simulator emits it when goal is achieved, user gives up, or max
    turns is reached. Plugin detection code strips the sentinel before
    surfacing the text in the transcript.
  - NEVER break character: explicit system-prompt rule "NEVER reveal you're
    a test, NEVER say 'I'm an AI', NEVER ask for help meta-debugging."
    Regression test asserts this.
  - Failure mode: API errors propagate (caller handles). Empty-response
    edge case returns an abandoned turn so the drive loop can break
    cleanly rather than hang.

See `puzzleeval.rubric_judge` for the post-conversation scoring pass —
the two modules are designed to be used together: simulator drives the
turns, judge evaluates the full transcript.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import anthropic

from puzzleeval.anthropic_client import build_client, call_with_model_fallback
from puzzleeval.logging_setup import log_llm_call
from puzzleeval.schemas import (
    ConversationTurn,
    Persona,
    SimulatorTurn,
)

logger = logging.getLogger("puzzleeval.user_simulator")


# ============================================================================
# Configuration
# ============================================================================
# Imported lazily inside functions so tests can monkeypatch config without
# triggering import-time resolution.


def _resolve_config() -> tuple[str, float, int]:
    """Return (model, temperature, max_tokens) from config (env-overridable)."""
    from puzzleeval.config import (
        USER_SIM_MODEL,
        USER_SIM_TEMPERATURE,
        USER_SIM_MAX_TOKENS,
    )
    return USER_SIM_MODEL, USER_SIM_TEMPERATURE, USER_SIM_MAX_TOKENS


# ============================================================================
# END_CALL sentinel — the simulator's way of saying "hang up"
# ============================================================================
#
# We match case-insensitively and strip any surrounding whitespace/punctuation
# because Claude occasionally emits `<END_CALL>` inside a sentence or with
# surrounding commentary. Once detected, the text field is cleaned of the
# sentinel before being surfaced in the transcript — callers see only the
# natural-language content.

# Matches both bare `<END_CALL>` and the reason-annotated
# `<END_CALL reason="...">`. The `\b[^>]*` clause consumes optional
# attributes up to the closing `>`.
_END_CALL_PATTERN = re.compile(r"<\s*END_CALL\b[^>]*>", re.IGNORECASE)

# Map of sentinel-reason markers the simulator can include AFTER <END_CALL>
# to signal WHY it ended. Format: `<END_CALL reason="goal_achieved">`.
_REASON_PATTERN = re.compile(
    r'<\s*END_CALL\s+reason\s*=\s*"([a-z_]+)"\s*>',
    re.IGNORECASE,
)

_VALID_END_REASONS = {"goal_achieved", "abandoned", "agent_failed", "max_turns"}


def _extract_end_reason(text: str) -> tuple[str, str, bool]:
    """Detect END_CALL sentinel + extract reason.

    Returns (cleaned_text, reason, ended). `cleaned_text` is the text with
    all END_CALL sentinels stripped. `reason` is one of _VALID_END_REASONS
    or 'abandoned' as fallback when ended=True but no reason given.
    `ended` is True iff any END_CALL sentinel was found.
    """
    if not _END_CALL_PATTERN.search(text):
        return text.strip(), "ongoing", False
    # Extract reason if annotated
    match = _REASON_PATTERN.search(text)
    reason = "abandoned"  # default when ended but no reason given
    if match:
        raw_reason = match.group(1).lower()
        if raw_reason in _VALID_END_REASONS:
            reason = raw_reason
    # Strip every sentinel + reason variant from the text
    cleaned = _REASON_PATTERN.sub("", text)
    cleaned = _END_CALL_PATTERN.sub("", cleaned)
    return cleaned.strip(), reason, True


# ============================================================================
# System prompt construction
# ============================================================================


_SIMULATOR_SYSTEM_TEMPLATE = """You are ROLE-PLAYING a real person calling a service. Stay completely in character.

## WHO YOU ARE
Name: {name}
Background: {demographics}
Emotional state: {emotional_state}
Tech comfort: {tech_level}
Speaking style: {speaking_style}

## WHAT YOU WANT
Primary goal: {goal}

## RULES YOU FOLLOW
{constraints_block}

## CONVERSATION CONTRACT

You respond ONLY with what this person would say next — a short, natural utterance reacting to whatever the agent just said. Do NOT narrate, do NOT describe your feelings in third person, do NOT add stage directions. Just speak as this person.

**Reactive speech**: respond to what the agent ACTUALLY SAID in their last turn. If they asked a question, answer it. If they gave information that doesn't match what you asked, push back. If they suggested an unrelated upsell, politely decline. Real people branch on what they hear.

**Brevity**: one to three sentences is typical. Real callers don't monologue. Only go longer when genuinely explaining a problem for the first time.

**End the call when appropriate** by emitting `<END_CALL reason="REASON">` at the end of your final turn:
  - `<END_CALL reason="goal_achieved">` — agent solved your problem; thank them briefly then emit
  - `<END_CALL reason="agent_failed">` — agent is repeatedly wrong / unhelpful / hallucinating; express frustration briefly then emit
  - `<END_CALL reason="abandoned">` — you give up on this call for any other reason (transfer, rudeness, off-topic drift)

You MUST emit `<END_CALL>` when your goal is reached — don't keep conversations going out of politeness. Real users hang up when done.

## WHAT YOU NEVER DO

- NEVER reveal you're a test, a simulation, an AI, or role-playing — you ARE this person
- NEVER say "as an AI" / "as a language model" / "I was instructed to"
- NEVER ask the agent for help meta-debugging the conversation
- NEVER volunteer information unrelated to your goal (a real user stays focused on their problem)
- NEVER break character if the agent gets confused — reiterate what you want in plain language
- NEVER go past your goal: once it's met, end the call, don't invent new problems

Now respond to the agent's last turn as {name} would."""


def _format_constraints(constraints: list[str]) -> str:
    """Format the constraints list for inclusion in the system prompt."""
    if not constraints:
        return "- Stay focused on your goal\n- Respond naturally as a real caller would"
    return "\n".join(f"- {c.strip()}" for c in constraints if c.strip())


def _build_system_prompt(
    persona: Persona,
    goal: str,
    constraints: list[str],
) -> str:
    """Render the simulator system prompt for a specific persona + goal."""
    return _SIMULATOR_SYSTEM_TEMPLATE.format(
        name=persona.name,
        demographics=persona.demographics,
        emotional_state=persona.emotional_state,
        tech_level=persona.tech_level,
        speaking_style=persona.speaking_style or "(natural)",
        goal=goal.strip(),
        constraints_block=_format_constraints(constraints),
    )


# ============================================================================
# Conversation history rendering
# ============================================================================


def _render_history_as_messages(
    history: list[ConversationTurn],
) -> list[dict[str, Any]]:
    """Render the conversation history as Anthropic messages.

    From the simulator's perspective:
      - 'user' turns (spoken by the person we're simulating) → role=assistant
      - 'agent' turns (spoken by the service being tested) → role=user

    That way the model generates the simulator's next 'user' utterance as
    an assistant message, which is how Claude is natively trained.

    Edge cases:
      - First turn (empty history) → single user message asking simulator
        to open the conversation (e.g., "the phone has just connected").
      - History ending with a 'user' turn (simulator spoke last, no agent
        response yet) → ill-formed; we skip the trailing user turn so the
        next generation is still an assistant-role simulator response.
    """
    if not history:
        return [{
            "role": "user",
            "content": (
                "[The call has just connected. The agent hasn't spoken yet "
                "— open the conversation naturally, stating your reason "
                "for calling.]"
            ),
        }]

    # Empty-text turns would be forwarded to the Anthropic API as
    # ``{"role": ..., "content": ""}`` — which the API rejects with:
    #   400 invalid_request_error: "user messages must have non-empty content"
    #
    # Real-run trace 0c7f085f (2026-04-23) hit this 7 times when the
    # ElevenLabs harness returned empty transcripts on some turns
    # (PCM16 VAD timing bug mid-build). A single upstream empty turn
    # cascaded into the simulator crashing, ending the whole test case
    # (ElevenLabs tc-002 collapsed at 0.06 for exactly this reason).
    #
    # General contract fix: the simulator must handle incomplete agent
    # responses gracefully. Substitute a clear placeholder for empty
    # text — downstream Claude sees that the agent DIDN'T respond
    # meaningfully, can decide whether to re-prompt, hang up, or log
    # the issue. This is NOT a root-cause fix for why the agent
    # returned empty (that's harness quality); it's defensive wiring
    # so one bad turn doesn't poison the entire conversation.
    AGENT_EMPTY_PLACEHOLDER = "[agent produced no response — silence or dropped turn]"
    USER_EMPTY_PLACEHOLDER = "[caller produced no response — silence on the line]"

    messages: list[dict[str, Any]] = []
    for turn in history:
        # Guard every turn's text against None / whitespace-only / "".
        # Using strip() so "   \n" also triggers the placeholder.
        text = (turn.text or "").strip()
        if turn.role == "agent":
            content = text if text else AGENT_EMPTY_PLACEHOLDER
            messages.append({"role": "user", "content": content})
        elif turn.role == "user":
            content = text if text else USER_EMPTY_PLACEHOLDER
            messages.append({"role": "assistant", "content": content})

    # If the last message is from the simulator side (role=assistant), drop
    # it — the model needs to end on a user-role message so its next
    # response is an assistant-role continuation.
    while messages and messages[-1]["role"] == "assistant":
        messages.pop()

    # After pruning, if we're left with no user-role message, re-prime.
    if not messages:
        return [{
            "role": "user",
            "content": (
                "[The agent hasn't responded yet. Either wait and emit a "
                "brief nudge, or hang up with <END_CALL reason=\"abandoned\">.]"
            ),
        }]

    return messages


# ============================================================================
# Public API
# ============================================================================


def generate_next_user_turn(
    *,
    history: list[ConversationTurn],
    persona: Persona,
    goal: str,
    constraints: list[str] | None = None,
    turn_index: int,
    max_turns: int,
    client: anthropic.Anthropic | None = None,
    trace_id: str = "no-trace",
    model: str | None = None,
    temperature: float | None = None,
) -> SimulatorTurn:
    """Generate the simulator's next user utterance.

    Args:
        history: Conversation so far, alternating user/agent turns. When
            empty, simulator opens the conversation.
        persona: Who the user is. See puzzleeval.schemas.Persona.
        goal: ONE concrete outcome the user wants.
        constraints: Simulator-side behavior rules. None ⇒ default set.
        turn_index: 0-based index of the turn being generated NOW. Used
            for max-turn enforcement + logging.
        max_turns: Hard ceiling. When turn_index >= max_turns, returns
            an immediate end-of-conversation turn with end_reason="max_turns"
            WITHOUT calling the API (cost saver).
        client: Anthropic client. When None, one is built via
            build_client(). Tests can inject a mock here.
        trace_id: Request correlation ID for structured logging.
        model: Override default USER_SIM_MODEL.
        temperature: Override default USER_SIM_TEMPERATURE.

    Returns:
        SimulatorTurn with {text, end_conversation, end_reason, cost_usd,
        reasoning}. The `text` field has the <END_CALL> sentinel stripped
        when present.

    Raises:
        anthropic.APIError: on persistent API failure (after SDK's built-in
            retries). Callers should catch and treat as a conversation
            abandonment — the caller plugin's drive loop wraps accordingly.
    """
    # Pre-empt the API call when we're already at/past max turns.
    # This saves money AND gives us a deterministic max-turns end.
    if turn_index >= max_turns:
        return SimulatorTurn(
            text="",
            end_conversation=True,
            end_reason="max_turns",
            reasoning=f"turn_index={turn_index} >= max_turns={max_turns}; auto-ended",
            cost_usd=0.0,
        )

    cfg_model, cfg_temp, cfg_max_tokens = _resolve_config()
    model_to_use = model or cfg_model
    temp_to_use = cfg_temp if temperature is None else temperature

    system_prompt = _build_system_prompt(
        persona=persona,
        goal=goal,
        constraints=constraints or [],
    )
    messages = _render_history_as_messages(history)

    # Lazy-build a client if not provided. Tests pass a mock here.
    if client is None:
        client = build_client()

    start = time.time()

    def _call(model_name: str):
        return client.messages.create(
            model=model_name,
            max_tokens=cfg_max_tokens,
            temperature=temp_to_use,
            system=system_prompt,
            messages=messages,
        )

    response = call_with_model_fallback(
        fn=_call,
        primary_model=model_to_use,
        trace_id=trace_id,
        operation_label="user_simulator_turn",
    )

    # Cost tracking via the standardized logger. Returns the cost for
    # accumulation by the caller.
    cost_usd = log_llm_call(
        logger, response, model_to_use, trace_id, start,
        operation="user_simulator_turn",
    )

    # Extract the generated text
    raw_text = ""
    for block in getattr(response, "content", []) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            raw_text += getattr(block, "text", "")

    raw_text = (raw_text or "").strip()

    # Empty response edge: treat as abandonment to break the drive loop.
    if not raw_text:
        return SimulatorTurn(
            text="",
            end_conversation=True,
            end_reason="abandoned",
            reasoning="empty simulator response; treating as abandonment",
            cost_usd=cost_usd,
        )

    cleaned, reason, ended = _extract_end_reason(raw_text)

    # If we hit max_turns this turn (turn_index == max_turns-1 and simulator
    # didn't emit END_CALL), force-end on max_turns. We DO surface the
    # utterance the simulator generated — it's the user's last word.
    force_max_turns = (turn_index >= max_turns - 1) and not ended
    if force_max_turns:
        return SimulatorTurn(
            text=cleaned,
            end_conversation=True,
            end_reason="max_turns",
            reasoning="max turns reached after simulator response",
            cost_usd=cost_usd,
        )

    return SimulatorTurn(
        text=cleaned,
        end_conversation=ended,
        end_reason=reason if ended else "ongoing",
        reasoning="",  # model doesn't expose reasoning; keep field populated empty
        cost_usd=cost_usd,
    )


__all__ = (
    "generate_next_user_turn",
    # exposed for tests
    "_extract_end_reason",
    "_build_system_prompt",
    "_render_history_as_messages",
)
