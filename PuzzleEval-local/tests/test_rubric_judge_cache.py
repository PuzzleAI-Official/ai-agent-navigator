"""Regression guards for rubric judge prompt caching.

Item 1b of the PLAN_VOICE_RUN_OPTIMIZATIONS deep-dive: the rubric
judge runs 14 times per voice run (7 tests × 2 candidates). The
~2K-token instructional skeleton is identical across every call. By
splitting the system prompt into a STABLE block (with cache_control:
ephemeral) + a PER-TEST block, the skeleton writes once + reads
~13× → saves ~$0.07 per run.

Real-run data backing the optimization (trace 73a9d605):
  - 14 rubric judge calls
  - $0.704 total rubric judge cost (11.4% of total run cost)
  - $0.05 average per call
  - Stable skeleton ~798 tokens × $3/MTok input = $0.0024 per call wasted
  - Across 14 calls: $0.034 wasted per run on re-sending the skeleton

These tests lock:
  1. `_build_system_prompt_blocks` returns a list-of-blocks (not a string)
  2. Block 0 is the stable skeleton + has cache_control: ephemeral
  3. Block 1 is the per-test content + does NOT have cache_control
  4. Block 0 contains NO per-test data (no leakage)
  5. Block 0 + Block 1 concatenated == old single-string version
     (back-compat guarantee for any caller that wants the full text)
  6. judge_conversation passes the BLOCKS list to the SDK, not the string
  7. The legacy `_build_system_prompt` still works for callers that
     want the string form (e.g., tests grepping prompt content)

Reference: PLAN_VOICE_RUN_OPTIMIZATIONS deep-dive item 1b.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def sample_persona():
    from puzzleeval.schemas import Persona
    return Persona(
        name="Alice Johnson",
        demographics="32, professional, time-constrained",
        emotional_state="mildly anxious about the appointment",
    )


@pytest.fixture
def sample_rubric():
    from puzzleeval.schemas import RubricCriterion
    return [
        RubricCriterion(
            name="goal_completion",
            description="Did the agent help the caller book?",
            weight=0.4,
        ),
        RubricCriterion(
            name="accuracy_no_hallucination",
            description="Did the agent invent prices or services?",
            weight=0.3,
            critical=True,
        ),
    ]


# ============================================================================
# 1. _build_system_prompt_blocks shape
# ============================================================================


class TestBlocksShape:
    """The new function must return a 2-element list of dict blocks
    matching the Anthropic SDK's `system=` shape."""

    def test_returns_list_of_two_blocks(self, sample_persona, sample_rubric):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        blocks = _build_system_prompt_blocks(
            persona=sample_persona, goal="book appointment",
            rubric=sample_rubric, transcript=[],
            candidate_role="dental dispatcher",
        )
        assert isinstance(blocks, list)
        assert len(blocks) == 2

    def test_each_block_is_text_dict(self, sample_persona, sample_rubric):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        blocks = _build_system_prompt_blocks(
            persona=sample_persona, goal="x", rubric=sample_rubric,
            transcript=[], candidate_role="r",
        )
        for block in blocks:
            assert isinstance(block, dict)
            assert block["type"] == "text"
            assert isinstance(block["text"], str)
            assert block["text"]  # non-empty


# ============================================================================
# 2. Block 0 is stable + cached
# ============================================================================


class TestStableBlock:

    def test_block_0_has_cache_control_ephemeral(
        self, sample_persona, sample_rubric,
    ):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        blocks = _build_system_prompt_blocks(
            persona=sample_persona, goal="x", rubric=sample_rubric,
            transcript=[], candidate_role="r",
        )
        assert "cache_control" in blocks[0]
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_block_0_contains_instructional_skeleton(
        self, sample_persona, sample_rubric,
    ):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        blocks = _build_system_prompt_blocks(
            persona=sample_persona, goal="x", rubric=sample_rubric,
            transcript=[], candidate_role="r",
        )
        stable_text = blocks[0]["text"]
        # Key instructional sections must be present
        assert "YOUR TASK" in stable_text
        assert "HOW TO SCORE" in stable_text
        assert "OUTPUT FORMAT" in stable_text

    def test_block_0_is_identical_across_different_per_test_inputs(self):
        """The whole point: the cached block is invariant. Two calls
        with different persona/goal/rubric/transcript must produce the
        IDENTICAL block 0 text."""
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        from puzzleeval.schemas import Persona, RubricCriterion

        blocks_1 = _build_system_prompt_blocks(
            persona=Persona(name="Alice", demographics="a", emotional_state="b"),
            goal="goal A", rubric=[RubricCriterion(name="c1", description="d", weight=1.0)],
            transcript=[], candidate_role="role A",
        )
        blocks_2 = _build_system_prompt_blocks(
            persona=Persona(name="Bob", demographics="x", emotional_state="y"),
            goal="goal B", rubric=[RubricCriterion(name="c2", description="e", weight=2.0)],
            transcript=[], candidate_role="role B",
        )
        # Block 0 must be byte-identical
        assert blocks_1[0]["text"] == blocks_2[0]["text"], (
            "Block 0 (stable cached skeleton) MUST be identical across "
            "different per-test inputs. If it differs, cache_control "
            "won't hit and the optimization breaks."
        )
        # Block 0 cache_control must be identical
        assert blocks_1[0]["cache_control"] == blocks_2[0]["cache_control"]
        # Block 1 differs (per-test content)
        assert blocks_1[1]["text"] != blocks_2[1]["text"]


# ============================================================================
# 3. Block 1 is per-test + uncached
# ============================================================================


class TestPerTestBlock:

    def test_block_1_has_no_cache_control(self, sample_persona, sample_rubric):
        """Per-test content has no cache_control — caching it would be
        a NET LOSS (1.25× write penalty without read benefit since
        every test has a unique transcript)."""
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        blocks = _build_system_prompt_blocks(
            persona=sample_persona, goal="x", rubric=sample_rubric,
            transcript=[], candidate_role="r",
        )
        assert "cache_control" not in blocks[1]

    def test_block_1_contains_per_test_content(
        self, sample_persona, sample_rubric,
    ):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        blocks = _build_system_prompt_blocks(
            persona=sample_persona, goal="book a dental appointment",
            rubric=sample_rubric, transcript=[],
            candidate_role="dental dispatcher",
            agent_system_prompt="You are Vera, a dental front desk agent.",
        )
        per_test = blocks[1]["text"]
        # Persona surfaces
        assert "Alice Johnson" in per_test
        assert "professional" in per_test
        # Goal surfaces
        assert "book a dental appointment" in per_test
        # Agent system prompt surfaces (Vera)
        assert "Vera" in per_test
        # Candidate role surfaces
        assert "dental dispatcher" in per_test
        # Rubric criteria surface
        assert "goal_completion" in per_test
        assert "accuracy_no_hallucination" in per_test


# ============================================================================
# 4. NO per-test leakage into the stable block
# ============================================================================


class TestStableBlockHasNoLeakage:
    """If per-test data leaks into the stable block, the cache_control
    is useless (cache key is the FULL block content; any difference =
    cache miss). This is the most critical correctness test."""

    def test_persona_does_not_leak(self):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        from puzzleeval.schemas import Persona, RubricCriterion
        blocks = _build_system_prompt_blocks(
            persona=Persona(
                name="UNIQUE_PERSONA_MARKER_ALPHA",
                demographics="UNIQUE_DEMO_MARKER",
                emotional_state="UNIQUE_STATE_MARKER",
            ),
            goal="book", rubric=[RubricCriterion(name="x", description="y", weight=1.0)],
            transcript=[], candidate_role="role",
        )
        stable = blocks[0]["text"]
        assert "UNIQUE_PERSONA_MARKER_ALPHA" not in stable
        assert "UNIQUE_DEMO_MARKER" not in stable
        assert "UNIQUE_STATE_MARKER" not in stable

    def test_goal_does_not_leak(self):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        from puzzleeval.schemas import Persona, RubricCriterion
        blocks = _build_system_prompt_blocks(
            persona=Persona(name="x", demographics="y", emotional_state="z"),
            goal="UNIQUE_GOAL_MARKER_BETA",
            rubric=[RubricCriterion(name="x", description="y", weight=1.0)],
            transcript=[], candidate_role="role",
        )
        assert "UNIQUE_GOAL_MARKER_BETA" not in blocks[0]["text"]

    def test_rubric_does_not_leak(self):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        from puzzleeval.schemas import Persona, RubricCriterion
        blocks = _build_system_prompt_blocks(
            persona=Persona(name="x", demographics="y", emotional_state="z"),
            goal="g",
            rubric=[
                RubricCriterion(name="UNIQUE_RUBRIC_MARKER_GAMMA",
                                description="d", weight=1.0)
            ],
            transcript=[], candidate_role="role",
        )
        assert "UNIQUE_RUBRIC_MARKER_GAMMA" not in blocks[0]["text"]

    def test_agent_instructions_do_not_leak(self):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        from puzzleeval.schemas import Persona, RubricCriterion
        blocks = _build_system_prompt_blocks(
            persona=Persona(name="x", demographics="y", emotional_state="z"),
            goal="g", rubric=[RubricCriterion(name="x", description="y", weight=1.0)],
            transcript=[], candidate_role="role",
            agent_system_prompt="UNIQUE_AGENT_PROMPT_MARKER_DELTA: act as X",
        )
        assert "UNIQUE_AGENT_PROMPT_MARKER_DELTA" not in blocks[0]["text"]

    def test_candidate_role_does_not_leak(self):
        from puzzleeval.rubric_judge import _build_system_prompt_blocks
        from puzzleeval.schemas import Persona, RubricCriterion
        blocks = _build_system_prompt_blocks(
            persona=Persona(name="x", demographics="y", emotional_state="z"),
            goal="g", rubric=[RubricCriterion(name="x", description="y", weight=1.0)],
            transcript=[], candidate_role="UNIQUE_ROLE_MARKER_EPSILON",
        )
        assert "UNIQUE_ROLE_MARKER_EPSILON" not in blocks[0]["text"]


# ============================================================================
# 5. Back-compat: the legacy single-string version still works
# ============================================================================


class TestLegacyStringVersion:
    """The original `_build_system_prompt` returning a string is kept
    for callers (tests, downstream consumers) that grep prompt content."""

    def test_string_version_returns_str(self, sample_persona, sample_rubric):
        from puzzleeval.rubric_judge import _build_system_prompt
        result = _build_system_prompt(
            persona=sample_persona, goal="book", rubric=sample_rubric,
            transcript=[], candidate_role="role",
            agent_system_prompt="agent prompt here",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_string_version_equals_concatenated_blocks(
        self, sample_persona, sample_rubric,
    ):
        """Invariant: the string version must equal blocks[0] +
        '\\n\\n' + blocks[1]. This guarantees back-compat for any
        caller that switches between the two APIs."""
        from puzzleeval.rubric_judge import (
            _build_system_prompt,
            _build_system_prompt_blocks,
        )
        kwargs = dict(
            persona=sample_persona, goal="book some appointment",
            rubric=sample_rubric, transcript=[],
            candidate_role="dental dispatcher",
            agent_system_prompt="You are Vera, a dental dispatcher.",
        )
        as_string = _build_system_prompt(**kwargs)
        as_blocks = _build_system_prompt_blocks(**kwargs)
        concatenated = as_blocks[0]["text"] + "\n\n" + as_blocks[1]["text"]
        assert as_string == concatenated, (
            "String version MUST equal the two blocks concatenated "
            "(with the documented separator). Otherwise back-compat "
            "callers see different content."
        )

    def test_existing_string_grep_test_still_passes(
        self, sample_persona, sample_rubric,
    ):
        """Locks the back-compat surface that
        test_agent_instructions_grounding.py uses (asserts substrings
        in the returned string)."""
        from puzzleeval.rubric_judge import _build_system_prompt
        from puzzleeval.schemas import Persona, RubricCriterion
        sp = _build_system_prompt(
            persona=Persona(name="M", demographics="h",
                            emotional_state="s"),
            goal="book appointment",
            rubric=[RubricCriterion(name="x", description="y", weight=1.0)],
            transcript=[],
            candidate_role="plumbing dispatcher",
            agent_system_prompt="You are Vera, a plumbing dispatcher.",
        )
        assert "AGENT'S OFFICIAL INSTRUCTIONS" in sp
        assert "Vera" in sp


# ============================================================================
# 6. judge_conversation wires the blocks version
# ============================================================================


class TestJudgeConversationWiresBlocks:
    """Source-grep guard that the rubric_judge call site uses
    `_build_system_prompt_blocks` (the cached version), NOT the legacy
    string version."""

    def test_judge_conversation_uses_blocks(self):
        src = (
            Path(__file__).resolve().parents[1]
            / "puzzleeval" / "rubric_judge.py"
        ).read_text(encoding="utf-8")
        idx = src.find("def judge_conversation")
        assert idx != -1
        end = src.find("\ndef ", idx + 1)
        if end == -1:
            end = len(src)
        body = src[idx:end]
        assert "_build_system_prompt_blocks(" in body, (
            "judge_conversation must call _build_system_prompt_blocks "
            "(the cached version), not the legacy string version. "
            "Without this, the cache_control optimization is bypassed."
        )

    def test_judge_conversation_passes_blocks_to_system_kwarg(self):
        src = (
            Path(__file__).resolve().parents[1]
            / "puzzleeval" / "rubric_judge.py"
        ).read_text(encoding="utf-8")
        idx = src.find("def judge_conversation")
        end = src.find("\ndef ", idx + 1)
        if end == -1:
            end = len(src)
        body = src[idx:end]
        # The call site should pass system=system_blocks (the variable
        # name from the new wiring), NOT system=system_prompt (legacy).
        assert "system=system_blocks" in body, (
            "judge_conversation must pass system=system_blocks (the "
            "variable holding the list-of-blocks). If it passes "
            "system=system_prompt instead, the cache_control on block 0 "
            "is silently dropped."
        )
