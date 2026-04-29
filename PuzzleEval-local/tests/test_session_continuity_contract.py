"""Regression guards for the multi-turn session-continuity contract in
`_VOICE_HARNESS_CONTRACT`.

Real-run trace 94271de4 (2026-04-22): ElevenLabs harness scored 0/7 on
all multi-turn voice tests because every turn opened a fresh WebSocket
connection — the agent had no memory of prior turns, repeating its
intro greeting on every user message:

    [user] I have a burst pipe at my house in Anaheim, can you send a plumber?
    [agent] Hello, thanks for calling. How can I help you today?
    [user] Like I just said, I have a burst pipe in Anaheim...
    [agent] Hello, thanks for calling. How can I help you today?

Root cause: HARD RULE #7 in `_VOICE_HARNESS_CONTRACT` was misleadingly
written:

    "The plugin handles session_state threading — you just read
    input_data.get('session_state', {}) and mutate it in-place for
    continuity."

A reasonable Opus reads "plugin handles threading — you just read it"
and concludes the heavy lifting isn't its job. Hence: fresh WebSocket
every turn.

The fix rewrites Rule #7 to be EXPLICIT about ownership:
  - PLUGIN passes session_state through turns. Nothing more.
  - HARNESS owns provider continuity (open WebSocket once on turn 0,
    store handle in session_state, REUSE on turn 1+).
  - Names the failure mode by name ("agent introduces itself every
    turn") so the model recognizes it.

These tests lock the new wording so any future "simplification" that
re-introduces the misleading framing fails CI.
"""

from __future__ import annotations

import pytest


def _contract() -> str:
    from puzzleeval.agents.implement_test_env import _VOICE_HARNESS_CONTRACT
    return _VOICE_HARNESS_CONTRACT


class TestRule7AssignsOwnershipExplicitly:
    """The new Rule #7 must clearly assign session-state ownership to
    the harness — not the plugin. The 'plugin handles it' phrasing is
    the exact bug we're fixing."""

    def test_explicitly_states_harness_owns_continuity(self):
        contract = _contract()
        # Ownership statement — flexibly matched across whitespace so
        # line wrapping in the prompt doesn't break the test.
        import re
        normalized = re.sub(r"\s+", " ", contract)
        assert "HARNESS owns provider continuity" in normalized, (
            "Rule #7 must explicitly state THE HARNESS owns provider "
            "continuity. Without this, the model assumes the plugin "
            "handles WebSocket reuse."
        )

    def test_does_not_say_plugin_handles_threading(self):
        contract = _contract()
        # The OLD wording that caused the bug
        bad_phrase = "plugin handles session_state threading"
        assert bad_phrase not in contract, (
            f"The misleading phrase {bad_phrase!r} must NOT appear — "
            "it's what made Opus think the plugin would manage the "
            "WebSocket lifecycle. Rule #7 must say the plugin only "
            "PASSES session_state through."
        )

    def test_explicitly_states_plugin_only_passes_state(self):
        contract = _contract()
        # Must clarify the plugin's actual minimal role
        passes_phrases = [
            "plugin passes",  # capitalization-flexible
            "plugin only passes",
            "doesn't know whether your provider",
        ]
        assert any(p.lower() in contract.lower() for p in passes_phrases), (
            "Rule #7 must explicitly state the plugin's role is just "
            "to pass session_state between turns — nothing more."
        )


class TestRule7TeachesTurnZeroVsTurnNPlusOne:
    """The contract must walk through both turn-0 (initialize + store)
    and turn-N+1 (read + reuse) so the model sees the full lifecycle."""

    def test_teaches_turn_zero_initialization(self):
        contract = _contract()
        # Must mention turn_index=0 and the action it requires
        assert "turn_index=0" in contract or "turn 0" in contract.lower()
        assert "STORE" in contract or "store" in contract.lower()

    def test_teaches_turn_n_reuse(self):
        contract = _contract()
        # Must mention turn_index>0 and the action (read + reuse)
        assert "turn_index>0" in contract or "turn 1" in contract.lower() or "turn n" in contract.lower()
        assert "REUSE" in contract or "read from session_state" in contract.lower()

    def test_warns_against_re_opening_websocket(self):
        contract = _contract()
        # Must explicitly forbid the bug pattern
        assert "Do NOT" in contract or "do not" in contract.lower()
        # Must name fresh-session reopening as the bug
        bug_indicators = ["fresh WebSocket", "open a fresh", "start a new conversation",
                          "fresh provider session"]
        assert any(b.lower() in contract.lower() for b in bug_indicators), (
            "Rule #7 must explicitly forbid opening a fresh WebSocket "
            "/ new conversation per turn — that's the failure mode."
        )


class TestRule7NamesTheFailureModeByName:
    """Naming the bug pattern in the prompt is load-bearing — it
    gives the model a concrete pattern to recognize and avoid."""

    def test_names_the_intro_repeating_bug(self):
        contract = _contract()
        # The exact phrase the user uses to describe this bug. Naming
        # it in the prompt is what lets Opus map "I just opened a
        # fresh WebSocket" to "oh, that's the bug — fix it."
        assert "introduces itself every turn" in contract.lower(), (
            "Rule #7 must name the 'agent introduces itself every "
            "turn' bug by name. Without a concrete failure-mode "
            "label, the model can't pattern-match its own code "
            "against the pitfall."
        )

    def test_explains_why_score_drops_to_zero(self):
        contract = _contract()
        # Must connect the bug to the scoring outcome so the model
        # understands the cost of getting it wrong
        score_phrases = ["zero", "0", "score near zero", "every multi-turn test"]
        assert any(p in contract.lower() for p in score_phrases), (
            "Rule #7 must explain that the bug causes near-zero "
            "scores — without that context, the model treats it as "
            "a minor lint issue."
        )


class TestRule7CoversAllProviderShapes:
    """The contract must acknowledge that provider session state
    takes different shapes (WebSocket / conversation_id / message
    history) — the harness must inspect its own API to know which."""

    def test_acknowledges_websocket_shape(self):
        contract = _contract()
        assert "WebSocket" in contract or "websocket" in contract

    def test_acknowledges_conversation_id_shape(self):
        contract = _contract()
        assert "conversation_id" in contract.lower()

    def test_acknowledges_message_history_shape(self):
        contract = _contract()
        history_phrases = ["message history", "messages list", "accumulated messages"]
        assert any(p.lower() in contract.lower() for p in history_phrases), (
            "Rule #7 must list message-history accumulation as one of "
            "the shapes — covers REST chat APIs without conversation_id."
        )

    def test_tells_harness_to_read_its_own_docs(self):
        contract = _contract()
        # Final escape hatch: the contract acknowledges it can't cover
        # every API shape, and tells the harness to consult docs.
        assert "API docs" in contract or "session lifecycle" in contract.lower()
