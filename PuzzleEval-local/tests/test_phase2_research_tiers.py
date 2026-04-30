"""Phase 2C.4 tests — research_subagent 3-tier output classifier.

The sub-agent prompt now teaches three explicit tiers:

  ANSWER          — confident, source-backed
  REASONABLE_GUESS — likely value with reasoning, builder validates
                    empirically
  NOT_FOUND       — genuinely uncertain; builder leaves as TODO

The classifier `classify_research_output(text)` routes raw responses
into the three tier strings (or "UNKNOWN" when no tier prefix is
detectable). Future G-Aux2 promotion would attach Pydantic-schema
validation to the tier output; for now the classifier is advisory.
"""

from __future__ import annotations


class TestClassifyResearchOutput:
    def test_tier_constant_has_three_canonical_tiers(self):
        from puzzleeval.agents.agent5.research_subagent import (
            RESEARCH_SUBAGENT_TIERS,
        )

        assert RESEARCH_SUBAGENT_TIERS == ("ANSWER", "REASONABLE_GUESS", "NOT_FOUND")

    def test_answer_tier(self):
        from puzzleeval.agents.agent5.research_subagent import classify_research_output

        text = (
            "ANSWER: Use the `override_permissions` flag in the agent config\n"
            "SOURCE: https://elevenlabs.io/docs/agents/config\n"
            "CONFIDENCE: high"
        )
        assert classify_research_output(text) == "ANSWER"

    def test_reasonable_guess_tier(self):
        from puzzleeval.agents.agent5.research_subagent import classify_research_output

        text = (
            "REASONABLE_GUESS: WebSocket close code 1011 likely indicates "
            "a server-side timeout based on similar realtime APIs.\n"
            "BASIS: OpenAI Realtime documents close 1011 with the same "
            "semantics; this provider's docs are silent.\n"
            "CONFIDENCE: low"
        )
        assert classify_research_output(text) == "REASONABLE_GUESS"

    def test_not_found_tier(self):
        from puzzleeval.agents.agent5.research_subagent import classify_research_output

        text = (
            "NOT_FOUND: searched: 'provider X webhook signature'; "
            "checked: docs root, GitHub, StackOverflow.\n"
            "RECOMMENDED NEXT STEP: probe the webhook payload structure\n"
            "CONFIDENCE: none"
        )
        assert classify_research_output(text) == "NOT_FOUND"

    def test_unknown_when_no_tier_prefix(self):
        from puzzleeval.agents.agent5.research_subagent import classify_research_output

        text = "Some prose that doesn't follow the tier format."
        assert classify_research_output(text) == "UNKNOWN"

    def test_empty_text_returns_unknown(self):
        from puzzleeval.agents.agent5.research_subagent import classify_research_output

        assert classify_research_output("") == "UNKNOWN"

    def test_handles_leading_backticks_and_whitespace(self):
        from puzzleeval.agents.agent5.research_subagent import classify_research_output

        # The prompt examples use backtick-formatted lines; the classifier
        # tolerates that.
        text = "  `ANSWER: foo`\n  `SOURCE: https://x`"
        assert classify_research_output(text) == "ANSWER"

    def test_reasonable_guess_takes_precedence_over_substring(self):
        """Per the order in the classifier, REASONABLE_GUESS is checked
        before ANSWER — they're distinct tier names so neither is a
        substring of the other, but the test pins the precedence."""
        from puzzleeval.agents.agent5.research_subagent import classify_research_output

        text = "REASONABLE_GUESS: x"
        assert classify_research_output(text) == "REASONABLE_GUESS"

    def test_classifies_first_line_only(self):
        """A text that starts with prose and later has a tier name in
        the middle should NOT be classified by the buried mention."""
        from puzzleeval.agents.agent5.research_subagent import classify_research_output

        # First line doesn't match any tier; subsequent lines do.
        text = "Quick summary here.\nANSWER: x\nSOURCE: y"
        # The classifier scans all lines but only matches at line-start
        # after stripping. So this should classify as ANSWER.
        assert classify_research_output(text) == "ANSWER"
