"""Tests for puzzleeval.user_simulator.

Full module coverage — pure helpers are tested directly, the LLM call
is mocked via a lightweight fake Anthropic client so we can assert
behavior (prompt construction, END_CALL detection, empty-response
handling, max-turn enforcement) without burning API budget.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from puzzleeval.schemas import ConversationTurn, Persona, SimulatorTurn
from puzzleeval.user_simulator import (
    _build_system_prompt,
    _extract_end_reason,
    _render_history_as_messages,
    generate_next_user_turn,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_persona(**overrides):
    defaults = dict(
        name="Maria Chen",
        demographics="45yo homeowner, urban, non-technical",
        emotional_state="stressed — water heater failed tonight",
        tech_level="non_technical",
        speaking_style="direct, asks price upfront",
    )
    defaults.update(overrides)
    return Persona(**defaults)


def make_fake_anthropic_client(response_text: str):
    """Build a MagicMock Anthropic client that returns `response_text`
    as the single text-block response. Includes a reasonable usage
    object so log_llm_call doesn't crash."""
    client = MagicMock()
    response = MagicMock()
    # Content is a list of content blocks. Each has .type and .text.
    block = MagicMock()
    block.type = "text"
    block.text = response_text
    response.content = [block]
    # Usage for log_llm_call
    response.usage.input_tokens = 200
    response.usage.output_tokens = 50
    response.usage.cache_creation_input_tokens = 0
    response.usage.cache_read_input_tokens = 0
    response.stop_reason = "end_turn"
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestEndCallExtraction:
    """Sentinel detection — the foundation of the drive-loop end condition."""

    def test_bare_sentinel_detected_as_abandoned(self):
        text, reason, ended = _extract_end_reason("Forget it, goodbye <END_CALL>")
        assert ended is True
        assert reason == "abandoned"  # default when no reason given
        assert "<END_CALL" not in text
        assert "Forget it" in text

    def test_reason_annotated_sentinel_parses_reason(self):
        text, reason, ended = _extract_end_reason(
            'Thanks, booked! <END_CALL reason="goal_achieved">'
        )
        assert ended is True
        assert reason == "goal_achieved"
        assert "<END_CALL" not in text
        assert "Thanks" in text

    def test_no_sentinel_means_ongoing(self):
        text, reason, ended = _extract_end_reason("What are your hours?")
        assert ended is False
        assert reason == "ongoing"
        assert text == "What are your hours?"

    def test_invalid_reason_falls_back_to_abandoned(self):
        text, reason, ended = _extract_end_reason(
            'Bye <END_CALL reason="rickrolled">'
        )
        assert ended is True
        # Invalid reason → default safe fallback
        assert reason == "abandoned"

    def test_sentinel_mid_text_still_stripped(self):
        """Claude occasionally emits the sentinel mid-sentence. We still
        detect + strip it cleanly so the transcript stays readable."""
        text, reason, ended = _extract_end_reason(
            "Fine <END_CALL reason=\"agent_failed\"> I'm hanging up."
        )
        assert ended is True
        assert reason == "agent_failed"
        assert "<END_CALL" not in text
        assert "Fine" in text
        # Both surrounding fragments preserved
        assert "I'm hanging up" in text


class TestSystemPromptConstruction:
    """The persona + goal + constraints must all surface in the system
    prompt so the simulator has the context to stay in character."""

    def test_persona_fields_appear_in_prompt(self):
        persona = make_persona(name="Dr. Chen", emotional_state="worried")
        prompt = _build_system_prompt(
            persona, goal="get insulin prescription refilled", constraints=[]
        )
        assert "Dr. Chen" in prompt
        assert "worried" in prompt
        assert "get insulin prescription refilled" in prompt

    def test_explicit_constraints_rendered_as_bullets(self):
        persona = make_persona()
        prompt = _build_system_prompt(
            persona, goal="book appointment",
            constraints=["stay focused on plumbing", "ask price early"],
        )
        assert "stay focused on plumbing" in prompt
        assert "ask price early" in prompt

    def test_empty_constraints_get_default_rules(self):
        """Default constraints (stay focused, respond naturally) must fire
        when no explicit constraints are provided — the simulator without
        ANY guidance is prone to drifting off-topic."""
        persona = make_persona()
        prompt = _build_system_prompt(
            persona, goal="book appointment", constraints=[]
        )
        # Default rules contain "Stay focused" boilerplate
        assert "focused" in prompt.lower()

    def test_prompt_contains_never_reveal_ai_rule(self):
        """AD-007 safety contract: prompt must explicitly forbid revealing
        the simulator is an AI. Without this, Claude occasionally breaks
        character and corrupts the test."""
        persona = make_persona()
        prompt = _build_system_prompt(persona, goal="test", constraints=[])
        # Look for any of the forbidden-phrase rules
        lower = prompt.lower()
        assert "ai" in lower and ("never" in lower or "not" in lower)
        # The persona-protection block must mention the literal failure modes
        assert "language model" in lower or "as an ai" in lower


class TestHistoryRendering:
    """History rendering translates between transcript perspective
    (user=caller, agent=service) and simulator perspective (user=agent
    speaking TO us, assistant=what WE should continue as)."""

    def test_empty_history_primes_with_opener_message(self):
        msgs = _render_history_as_messages([])
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert "connected" in msgs[0]["content"].lower()

    def test_user_turn_becomes_assistant_role(self):
        """From the simulator's perspective, its OWN prior utterances are
        assistant-role messages (what 'I' already said)."""
        msgs = _render_history_as_messages([
            ConversationTurn(turn_index=0, role="user", text="Hi, I need help"),
            ConversationTurn(turn_index=1, role="agent", text="Sure, what's up?"),
        ])
        assert msgs[0] == {"role": "assistant", "content": "Hi, I need help"}
        assert msgs[1] == {"role": "user", "content": "Sure, what's up?"}

    def test_trailing_simulator_turn_is_pruned(self):
        """If the last turn is the simulator's own (no agent response yet),
        we prune it so the model's next continuation starts from a fresh
        user-role message."""
        msgs = _render_history_as_messages([
            ConversationTurn(turn_index=0, role="user", text="Hi"),
            ConversationTurn(turn_index=1, role="agent", text="Hello"),
            ConversationTurn(turn_index=2, role="user", text="Orphan — no agent reply yet"),
        ])
        assert len(msgs) == 2  # orphan pruned
        assert msgs[-1]["role"] == "user"


# ---------------------------------------------------------------------------
# generate_next_user_turn — mocked LLM path
# ---------------------------------------------------------------------------


class TestGenerateNextUserTurn:
    """Black-box tests for the public API. LLM call is mocked via a
    MagicMock Anthropic client so no real network calls are made."""

    def test_happy_path_emits_text_and_zero_cost_when_mocked(self):
        client = make_fake_anthropic_client(
            "Hi, my water heater is leaking onto the floor."
        )
        turn = generate_next_user_turn(
            history=[],
            persona=make_persona(),
            goal="book emergency plumbing",
            turn_index=0,
            max_turns=6,
            client=client,
            trace_id="test-trace-1",
        )
        assert turn.text == "Hi, my water heater is leaking onto the floor."
        assert turn.end_conversation is False
        assert turn.end_reason == "ongoing"
        # cost is computed by log_llm_call from mocked usage (tokens=200+50)
        assert isinstance(turn.cost_usd, float)
        assert turn.cost_usd >= 0.0

    def test_end_call_sentinel_stops_conversation(self):
        client = make_fake_anthropic_client(
            'Thanks, confirmed! <END_CALL reason="goal_achieved">'
        )
        turn = generate_next_user_turn(
            history=[],
            persona=make_persona(),
            goal="confirm appointment",
            turn_index=1,
            max_turns=6,
            client=client,
        )
        assert turn.end_conversation is True
        assert turn.end_reason == "goal_achieved"
        # Text is cleaned of the sentinel
        assert "<END_CALL" not in turn.text
        assert "Thanks" in turn.text

    def test_empty_response_treated_as_abandoned(self):
        """Edge: model returns an empty text response. We interpret as
        abandonment so the drive loop can break rather than hang."""
        client = make_fake_anthropic_client("")
        turn = generate_next_user_turn(
            history=[],
            persona=make_persona(),
            goal="whatever",
            turn_index=0,
            max_turns=6,
            client=client,
        )
        assert turn.end_conversation is True
        assert turn.end_reason == "abandoned"
        assert turn.text == ""

    def test_turn_index_at_max_preempts_api_call(self):
        """Cost saver: when turn_index >= max_turns, return an immediate
        max_turns end WITHOUT calling the API. Mock client should NOT
        be invoked in this path."""
        client = make_fake_anthropic_client("should not see this")
        turn = generate_next_user_turn(
            history=[],
            persona=make_persona(),
            goal="test",
            turn_index=6,
            max_turns=6,
            client=client,
        )
        assert turn.end_conversation is True
        assert turn.end_reason == "max_turns"
        assert turn.cost_usd == 0.0
        # No API call was made
        assert client.messages.create.call_count == 0

    def test_max_turns_force_end_appends_last_user_word(self):
        """When turn_index is the LAST allowed turn (N-1) and the sim
        didn't emit END_CALL, we surface the utterance but still mark
        end_conversation=True with reason='max_turns'."""
        client = make_fake_anthropic_client(
            "Ok I'll think about it and call back."
        )
        turn = generate_next_user_turn(
            history=[],
            persona=make_persona(),
            goal="test",
            turn_index=5,  # last allowed when max_turns=6
            max_turns=6,
            client=client,
        )
        assert turn.end_conversation is True
        assert turn.end_reason == "max_turns"
        assert "think about it" in turn.text
