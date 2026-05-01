"""Tests for ``puzzleeval.agents.agent5.context_compaction``.

The compaction fires at the Sonnet → Opus transition (api_spec_written
flips True). It REPLACES ``messages`` with a single canonical state
packet pointing at on-disk artifacts. Direct fix for run 749b09b1's
narrative-inertia failure mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from puzzleeval.agents.agent5 import context_compaction


# ---------------------------------------------------------------------------
# Helpers — make a sandbox with the expected on-disk artifacts
# ---------------------------------------------------------------------------


def _seed_sandbox(tmp_path: Path, *, with_objective=True, with_runtime_state=True,
                  with_build_plan=False) -> None:
    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    if with_objective:
        (state_dir / "objective.md").write_text(
            "# Objective for TestSvc\n\n"
            "## DELIVERABLE\nBuild a harness.\n\n"
            "## SUCCESS CRITERIA (system-defined; do not modify)\n"
            "- [ ] smoke_test.py passes\n"
            "- [ ] live_test.py passes\n"
            "- [ ] All test cases pass\n\n"
            "## CONSTRAINTS (system-defined)\n"
            "- Budget: max 40 turns\n\n"
            "## OUT OF SCOPE (system-defined - anti-scope-creep)\n"
            "- Caching\n",
            encoding="utf-8",
        )
    if with_runtime_state:
        (state_dir / "runtime_state.json").write_text(
            json.dumps({
                "current_phase": "phase_2_build",
                "current_turn": 3,
                "files_present": ["objective.md", "runtime_state.json", "api_spec.txt", "_forensics.py"],
                "files_pending": ["harness.py", "smoke_test.py", "live_test.py", "requirements.txt"],
            }),
            encoding="utf-8",
        )
    if with_build_plan:
        (state_dir / "build_plan.md").write_text(
            "# Build plan for TestSvc\n\n"
            "## Phase 1 - Research [status: complete]\n"
            "- [x] api_spec.txt written\n\n"
            "## Phase 2 - Build [status: in_progress]\n"
            "- [ ] requirements.txt\n"
            "- [ ] harness.py\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------


class TestExtractOutstandingSuccessCriteria:
    def test_extracts_section_text(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        objective = (tmp_path / "_agent_state" / "objective.md").read_text()
        section = context_compaction._extract_outstanding_success_criteria(objective)
        assert "smoke_test.py passes" in section
        assert "live_test.py passes" in section
        # Should NOT include subsequent sections
        assert "Budget" not in section

    def test_returns_empty_when_section_missing(self):
        assert context_compaction._extract_outstanding_success_criteria("") == ""
        assert context_compaction._extract_outstanding_success_criteria("# No section here") == ""


class TestExtractPhase2Todos:
    def test_extracts_section_text(self, tmp_path: Path):
        _seed_sandbox(tmp_path, with_build_plan=True)
        plan = (tmp_path / "_agent_state" / "build_plan.md").read_text()
        section = context_compaction._extract_phase_2_todos(plan)
        assert "requirements.txt" in section
        assert "harness.py" in section
        # Should NOT include Phase 1
        assert "api_spec.txt written" not in section

    def test_returns_empty_when_no_phase_2_section(self):
        assert context_compaction._extract_phase_2_todos("") == ""
        assert context_compaction._extract_phase_2_todos(
            "# build plan\n\n## Phase 1\n- [ ] x"
        ) == ""


# ---------------------------------------------------------------------------
# Compose state packet
# ---------------------------------------------------------------------------


class TestComposeCanonicalStatePacket:
    def test_packet_directs_opus_imperatively(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        # Imperative tone — direct fix for narrative-inertia bias.
        assert "You are now Phase 2 builder" in packet
        assert "do not narrate" in packet.lower()
        # The packet must point at on-disk artifacts (Codex's "orchestrator
        # owns truth" — the agent reads from disk, not from inherited
        # conversation history).
        assert "api_spec.txt" in packet
        assert "_agent_state/objective.md" in packet
        assert "_agent_state/runtime_state.json" in packet

    def test_packet_lists_required_next_actions(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        for fn in ("requirements.txt", "harness.py", "smoke_test.py", "live_test.py"):
            assert fn in packet, f"Required scaffold file missing from packet: {fn}"

    def test_packet_includes_success_criteria_when_objective_present(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        # SUCCESS CRITERIA section content should be embedded.
        assert "smoke_test.py passes" in packet
        assert "All test cases pass" in packet

    def test_packet_includes_phase_2_todos_when_plan_present(self, tmp_path: Path):
        _seed_sandbox(tmp_path, with_build_plan=True)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert "Phase 2 todos from build_plan.md" in packet

    def test_packet_omits_plan_section_when_plan_absent(self, tmp_path: Path):
        _seed_sandbox(tmp_path, with_build_plan=False)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert "Phase 2 todos from build_plan.md" not in packet

    def test_packet_handles_missing_runtime_state_gracefully(self, tmp_path: Path):
        _seed_sandbox(tmp_path, with_runtime_state=False)
        # Should still compose — falls back to "(none)" / "?" placeholders.
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert "You are now Phase 2 builder" in packet


# ---------------------------------------------------------------------------
# Top-level compaction entry point
# ---------------------------------------------------------------------------


class TestCompactForModelTransition:
    def test_returns_single_user_message(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        msgs = context_compaction.compact_for_model_transition(tmp_path)
        assert isinstance(msgs, list)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert isinstance(msgs[0]["content"], str)

    def test_message_content_is_the_canonical_packet(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        msgs = context_compaction.compact_for_model_transition(tmp_path)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert msgs[0]["content"] == packet
