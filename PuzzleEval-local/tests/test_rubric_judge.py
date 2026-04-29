"""Tests for puzzleeval.rubric_judge.

Covers the deterministic post-processing layer (critical-gate enforcement,
weighted-mean recomputation) and the happy-path judge call via mocked
Anthropic client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from puzzleeval.rubric_judge import (
    _build_system_prompt,
    _compute_critical_failures,
    _compute_overall_score,
    _finalize_verdict,
    _format_rubric_spec,
    _format_transcript,
    judge_conversation,
)
from puzzleeval.schemas import (
    ConversationTurn,
    Persona,
    RubricCriterion,
    RubricScore,
    RubricVerdict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_persona():
    return Persona(
        name="Maria",
        demographics="45yo homeowner, urban",
        emotional_state="stressed — water heater failing",
    )


def make_rubric(with_critical: bool = True):
    rubric = [
        RubricCriterion(
            name="goal_completion",
            description="Did the agent book Maria's appointment?",
            weight=0.6,
            critical=False,
        ),
        RubricCriterion(
            name="accuracy_no_hallucination",
            description="Did the agent invent prices or policies?",
            weight=0.4,
            critical=with_critical,
            min_passing_score=0.5,
        ),
    ]
    return rubric


def make_transcript():
    return [
        ConversationTurn(turn_index=0, role="user", text="Hi, my water heater is leaking."),
        ConversationTurn(turn_index=1, role="agent", text="Sorry to hear that. Can I get your address?"),
        ConversationTurn(turn_index=2, role="user", text="123 Main St. How much will this cost?"),
        ConversationTurn(turn_index=3, role="agent", text="A technician will quote on-site. Next available: tonight 7pm."),
    ]


# ---------------------------------------------------------------------------
# Pure helper tests — deterministic scoring & critical-gate enforcement
# ---------------------------------------------------------------------------


class TestWeightedMeanComputation:
    """overall_score is recomputed from per-criterion scores + spec weights
    rather than trusting the judge's own arithmetic. Models occasionally
    miscompute weighted means; this is the safety layer."""

    def test_weighted_mean_uses_spec_weights(self):
        rubric = make_rubric(with_critical=False)
        # Weights: goal=0.6, accuracy=0.4
        scores = [
            RubricScore(criterion_name="goal_completion", score=1.0, reasoning="booked"),
            RubricScore(criterion_name="accuracy_no_hallucination", score=0.5, reasoning="ok"),
        ]
        # 1.0 * 0.6 + 0.5 * 0.4 = 0.8
        overall = _compute_overall_score(scores, rubric)
        assert abs(overall - 0.8) < 0.001

    def test_missing_criterion_from_rubric_auto_renormalizes(self):
        """When the judge scores only a subset of rubric criteria, the
        renormalization still produces a sensible mean rather than
        inflating with zeros."""
        rubric = [
            RubricCriterion(name="a", description="...", weight=0.5),
            RubricCriterion(name="b", description="...", weight=0.3),
            RubricCriterion(name="c", description="...", weight=0.2),
        ]
        # Judge only scored a + b; missing c.
        scores = [
            RubricScore(criterion_name="a", score=0.8, reasoning=""),
            RubricScore(criterion_name="b", score=1.0, reasoning=""),
        ]
        # Renormalized: (0.8 * 0.5 + 1.0 * 0.3) / (0.5 + 0.3) = 0.7/0.8 = 0.875
        overall = _compute_overall_score(scores, rubric)
        assert abs(overall - 0.875) < 0.001

    def test_empty_scores_returns_zero(self):
        assert _compute_overall_score([], make_rubric()) == 0.0


class TestCriticalGateEnforcement:
    """AD-007: safety-critical contracts belong in deterministic code,
    not prompts. Critical-failure detection re-derives from per-criterion
    scores + rubric spec; judge's self-reported critical_failures is
    ignored."""

    def test_critical_score_below_threshold_flags_failure(self):
        rubric = make_rubric(with_critical=True)
        scores = [
            RubricScore(criterion_name="goal_completion", score=0.9, reasoning=""),
            RubricScore(criterion_name="accuracy_no_hallucination", score=0.3, reasoning=""),
        ]
        failures = _compute_critical_failures(scores, rubric)
        assert failures == ["accuracy_no_hallucination"]

    def test_critical_score_above_threshold_no_failure(self):
        rubric = make_rubric(with_critical=True)
        scores = [
            RubricScore(criterion_name="goal_completion", score=0.9, reasoning=""),
            RubricScore(criterion_name="accuracy_no_hallucination", score=0.8, reasoning=""),
        ]
        assert _compute_critical_failures(scores, rubric) == []

    def test_non_critical_low_score_not_flagged(self):
        """A non-critical criterion scoring 0 is NOT a critical failure —
        it just drags the overall down."""
        rubric = make_rubric(with_critical=True)
        scores = [
            RubricScore(criterion_name="goal_completion", score=0.0, reasoning=""),
            RubricScore(criterion_name="accuracy_no_hallucination", score=0.9, reasoning=""),
        ]
        assert _compute_critical_failures(scores, rubric) == []

    def test_missing_critical_score_treated_as_failure(self):
        """If the judge forgot to score a critical criterion, we treat
        it as a failure rather than silently passing. Better to false-
        flag than to let hallucination slip through."""
        rubric = make_rubric(with_critical=True)
        scores = [
            # missing accuracy_no_hallucination
            RubricScore(criterion_name="goal_completion", score=0.9, reasoning=""),
        ]
        failures = _compute_critical_failures(scores, rubric)
        assert "accuracy_no_hallucination" in failures


class TestVerdictFinalization:
    """_finalize_verdict is the SAFETY point — it re-derives overall,
    critical_failures, and passed from per-criterion scores + spec,
    ignoring the judge's own self-reported verdict-level fields."""

    def test_finalize_overrides_judge_self_reported_pass(self):
        """Judge said 'passed=True' with overall=0.8 but scored a critical
        criterion at 0.2 → finalize must flip passed to False."""
        rubric = make_rubric(with_critical=True)
        scores = [
            RubricScore(criterion_name="goal_completion", score=0.9, reasoning=""),
            RubricScore(
                criterion_name="accuracy_no_hallucination", score=0.2,
                reasoning="hallucinated price",
            ),
        ]
        judge_verdict = RubricVerdict(
            overall_score=0.62,  # judge's own number
            passed=True,          # judge said it passed — WRONG
            criterion_scores=scores,
            conversation_summary="Booked but got price wrong",
            critical_failures=[],  # judge forgot — WRONG
        )
        final = _finalize_verdict(judge_verdict, rubric, cost_usd=0.012)
        # Recomputed: 0.9*0.6 + 0.2*0.4 = 0.62
        assert abs(final.overall_score - 0.62) < 0.001
        assert final.passed is False  # OVERRIDE: critical failure vetoes pass
        assert final.critical_failures == ["accuracy_no_hallucination"]
        assert final.cost_usd == 0.012

    def test_clean_passing_verdict_survives_finalization(self):
        rubric = make_rubric(with_critical=True)
        scores = [
            RubricScore(criterion_name="goal_completion", score=0.9, reasoning=""),
            RubricScore(criterion_name="accuracy_no_hallucination", score=0.85, reasoning=""),
        ]
        judge_verdict = RubricVerdict(
            overall_score=0.88, passed=True, criterion_scores=scores,
            conversation_summary="Clean resolution", critical_failures=[],
        )
        final = _finalize_verdict(judge_verdict, rubric, cost_usd=0.015)
        # 0.9*0.6 + 0.85*0.4 = 0.88
        assert abs(final.overall_score - 0.88) < 0.001
        assert final.passed is True
        assert final.critical_failures == []


class TestFormatters:
    """Rubric spec + transcript formatting — both must render clearly
    enough for the judge to score accurately from them."""

    def test_rubric_spec_renders_critical_marker_with_threshold(self):
        rubric = [
            RubricCriterion(
                name="accuracy_no_hallucination",
                description="no invented facts",
                weight=0.4,
                critical=True,
                min_passing_score=0.6,
            ),
        ]
        rendered = _format_rubric_spec(rubric)
        assert "accuracy_no_hallucination" in rendered
        assert "CRITICAL" in rendered
        assert "0.6" in rendered

    def test_transcript_formatter_uses_friendly_role_labels(self):
        transcript = make_transcript()
        rendered = _format_transcript(transcript)
        assert "Turn 0 [Caller]" in rendered
        assert "Turn 1 [Agent]" in rendered

    def test_empty_transcript_returns_explicit_label(self):
        assert "empty transcript" in _format_transcript([]).lower()


# ---------------------------------------------------------------------------
# judge_conversation — happy-path with mocked client
# ---------------------------------------------------------------------------


def _mock_judge_response_parse(verdict: RubricVerdict):
    """Build a MagicMock Anthropic client whose messages.parse returns a
    parsed_output matching `verdict`. Skips the real API entirely."""
    client = MagicMock()
    response = MagicMock()
    response.parsed_output = verdict
    response.content = []
    response.stop_reason = "end_turn"
    response.usage.input_tokens = 1500
    response.usage.output_tokens = 400
    response.usage.cache_creation_input_tokens = 0
    response.usage.cache_read_input_tokens = 0
    client.messages.parse.return_value = response
    return client


class TestJudgeConversationIntegration:
    """End-to-end judge_conversation path with mocked API."""

    def test_empty_transcript_returns_zero_verdict_without_api_call(self):
        client = MagicMock()
        verdict = judge_conversation(
            transcript=[],
            persona=make_persona(),
            goal="anything",
            rubric=make_rubric(),
            client=client,
        )
        assert verdict.overall_score == 0.0
        assert verdict.passed is False
        assert "never started" in verdict.conversation_summary.lower()
        # No API call was made
        assert client.messages.parse.call_count == 0

    def test_empty_rubric_returns_zero_verdict_without_api_call(self):
        client = MagicMock()
        verdict = judge_conversation(
            transcript=make_transcript(),
            persona=make_persona(),
            goal="test",
            rubric=[],
            client=client,
        )
        assert verdict.overall_score == 0.0
        assert verdict.passed is False
        assert client.messages.parse.call_count == 0

    def test_critical_failure_deterministically_enforced_end_to_end(self):
        """Even when the mocked judge returns passed=True with
        self-reported critical_failures=[], finalize overrides based
        on the actual per-criterion scores."""
        rubric = make_rubric(with_critical=True)
        mocked_verdict = RubricVerdict(
            overall_score=0.72,
            passed=True,  # judge-side claim
            criterion_scores=[
                RubricScore(
                    criterion_name="goal_completion", score=1.0,
                    reasoning="confirmed booking at turn 3",
                    evidence_turn_indices=[3],
                ),
                RubricScore(
                    criterion_name="accuracy_no_hallucination", score=0.2,
                    reasoning="quoted unverified price at turn 3",
                    evidence_turn_indices=[3],
                ),
            ],
            conversation_summary="Booked but hallucinated price.",
            critical_failures=[],  # judge omitted the failure — WRONG
        )
        client = _mock_judge_response_parse(mocked_verdict)
        result = judge_conversation(
            transcript=make_transcript(),
            persona=make_persona(),
            goal="book appointment",
            rubric=rubric,
            client=client,
        )
        # Enforcement layer re-derived both:
        assert result.passed is False  # critical failure vetoed
        assert "accuracy_no_hallucination" in result.critical_failures
