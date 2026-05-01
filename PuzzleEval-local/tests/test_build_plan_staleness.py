"""Tests for ``detect_build_plan_triggers``.

PR 1 (deferred wiring): the orchestrator pings the agent at meaningful
state-change moments to keep ``_agent_state/build_plan.md`` operational.
This module exercises the pure trigger-detection helper in
``dispatch_helpers.py``. The actual nudge wiring + mtime comparison
live in ``build_loop.py``; the integration is verified end-to-end via
``mock_pipeline_direct.py`` (post-merge monitoring).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from puzzleeval.agents.agent5.dispatch_helpers import detect_build_plan_triggers


def _make_tool_use_block(name: str, filename: str, tool_id: str = "tu_1") -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = {"filename": filename}
    block.id = tool_id
    return block


def _make_text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


# ---------------------------------------------------------------------------
# api_spec.txt transition trigger
# ---------------------------------------------------------------------------


class TestApiSpecTrigger:

    def test_fires_on_transition(self):
        triggers = detect_build_plan_triggers(
            response_content=[],
            api_spec_was_written=False,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        assert "api_spec.txt written" in triggers

    def test_does_not_fire_when_already_written(self):
        triggers = detect_build_plan_triggers(
            response_content=[],
            api_spec_was_written=True,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        assert "api_spec.txt written" not in triggers

    def test_does_not_fire_when_still_phase_1(self):
        triggers = detect_build_plan_triggers(
            response_content=[],
            api_spec_was_written=False,
            api_spec_now_written=False,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        assert "api_spec.txt written" not in triggers


# ---------------------------------------------------------------------------
# Scaffold-write trigger
# ---------------------------------------------------------------------------


class TestScaffoldTrigger:

    @pytest.mark.parametrize("filename", [
        "harness.py",
        "smoke_test.py",
        "live_test.py",
        "requirements.txt",
        "Harness.PY",  # case-insensitive
    ])
    def test_fires_for_canonical_scaffold_filenames(self, filename: str):
        triggers = detect_build_plan_triggers(
            response_content=[_make_tool_use_block("write_file", filename)],
            api_spec_was_written=True,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        assert any(t.startswith("scaffold write") for t in triggers)

    def test_one_scaffold_trigger_per_turn_even_with_multiple_writes(self):
        # Phase 2's canonical pattern is "all four scaffolds in one turn"
        # — we don't want four nudges firing at once.
        blocks = [
            _make_tool_use_block("write_file", "harness.py", "t1"),
            _make_tool_use_block("write_file", "smoke_test.py", "t2"),
            _make_tool_use_block("write_file", "live_test.py", "t3"),
            _make_tool_use_block("write_file", "requirements.txt", "t4"),
        ]
        triggers = detect_build_plan_triggers(
            response_content=blocks,
            api_spec_was_written=True,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        scaffold_count = sum(1 for t in triggers if t.startswith("scaffold write"))
        assert scaffold_count == 1

    def test_patch_file_also_counts_as_scaffold_trigger(self):
        # A patch_file('harness.py', ...) is a meaningful state change,
        # same shape as write_file.
        triggers = detect_build_plan_triggers(
            response_content=[_make_tool_use_block("patch_file", "harness.py")],
            api_spec_was_written=True,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        assert any(t.startswith("scaffold write") for t in triggers)

    def test_non_scaffold_writes_do_not_trigger(self):
        # Writing api_spec.txt is its own trigger (api_spec transition).
        # _agent_state/build_plan.md is the agent updating the artifact
        # itself — definitely not a scaffold write.
        blocks = [
            _make_tool_use_block("write_file", "api_spec.txt", "t1"),
            _make_tool_use_block("write_file", "_agent_state/build_plan.md", "t2"),
            _make_tool_use_block("read_file", "harness.py", "t3"),
        ]
        triggers = detect_build_plan_triggers(
            response_content=blocks,
            api_spec_was_written=True,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        scaffold_triggers = [t for t in triggers if t.startswith("scaffold write")]
        assert scaffold_triggers == []


# ---------------------------------------------------------------------------
# Smoke-pass + pre-HARNESS_COMPLETE triggers
# ---------------------------------------------------------------------------


class TestStateFlagTriggers:

    def test_smoke_passed_fires_trigger(self):
        triggers = detect_build_plan_triggers(
            response_content=[],
            api_spec_was_written=True,
            api_spec_now_written=True,
            smoke_passed_this_turn=True,
            harness_complete_signaled=False,
        )
        assert "smoke test passed" in triggers

    def test_harness_complete_signaled_fires_trigger(self):
        triggers = detect_build_plan_triggers(
            response_content=[],
            api_spec_was_written=True,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=True,
        )
        assert "pre-HARNESS_COMPLETE" in triggers

    def test_no_trigger_when_no_flags_changed(self):
        triggers = detect_build_plan_triggers(
            response_content=[_make_text_block("just thinking")],
            api_spec_was_written=True,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        assert triggers == []


# ---------------------------------------------------------------------------
# Combined / order-deterministic
# ---------------------------------------------------------------------------


class TestTriggerOrdering:

    def test_multiple_triggers_in_canonical_order(self):
        # The detector should produce triggers in the documented order:
        # api_spec → scaffold → smoke pass → pre-HARNESS_COMPLETE.
        triggers = detect_build_plan_triggers(
            response_content=[_make_tool_use_block("write_file", "harness.py")],
            api_spec_was_written=False,
            api_spec_now_written=True,
            smoke_passed_this_turn=True,
            harness_complete_signaled=True,
        )
        assert triggers == [
            "api_spec.txt written",
            "scaffold write: harness.py",
            "smoke test passed",
            "pre-HARNESS_COMPLETE",
        ]

    def test_empty_response_content_handled(self):
        triggers = detect_build_plan_triggers(
            response_content=None,
            api_spec_was_written=False,
            api_spec_now_written=True,
            smoke_passed_this_turn=False,
            harness_complete_signaled=False,
        )
        assert triggers == ["api_spec.txt written"]
