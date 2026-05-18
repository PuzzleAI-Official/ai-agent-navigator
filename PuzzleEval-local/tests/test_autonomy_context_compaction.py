"""Tests for ``puzzleeval.agents.agent5.context_compaction``.

The compaction fires after the implementation-plan build gate accepts. It
REPLACES ``messages`` with a single canonical state packet pointing at
on-disk artifacts. Direct fix for build-gate narrative-inertia failures.
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
            "- [ ] harness.py imports and exposes run(input_data)\n"
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
                "files_present": [
                    "objective.md",
                    "runtime_state.json",
                    "research_synthesis.json",
                    "implementation_plan.json",
                    "_forensics.py",
                ],
                "files_pending": ["harness.py", "smoke_test.py", "live_test.py", "requirements.txt"],
            }),
            encoding="utf-8",
        )
    (state_dir / "docs_entrypoint.json").write_text(
        json.dumps({
            "evidence_status": "fetched_current_api_docs",
            "primary_docs_entrypoint": "https://docs.example.com/api",
            "prefetched_docs": {
                "https://docs.example.com/api": "_agent_state/fetched_docs_main.md",
            },
        }),
        encoding="utf-8",
    )
    (state_dir / "test_case_manifest.json").write_text(
        json.dumps({
            "input_families": [{"family": "voice_conversation", "count": 2}],
            "representative_cases": [{"id": "tc-1", "why": "multi-turn"}],
        }),
        encoding="utf-8",
    )
    (state_dir / "research_synthesis.json").write_text(
        json.dumps({
            "build_brief": {
                "endpoint_auth": "Bearer token",
                "input_mapping": "caller_audio_url -> provider audio input",
            },
            "unresolved_questions": [],
        }),
        encoding="utf-8",
    )
    (state_dir / "implementation_plan.json").write_text(
        json.dumps({
            "interaction_pattern": "persistent_worker",
            "file_plan": ["harness.py", "smoke_test.py", "live_test.py"],
            "objective_coverage": [{"criterion_id": "OBJ-1", "approach": "smoke"}],
        }),
        encoding="utf-8",
    )
    if with_build_plan:
        (state_dir / "build_plan.md").write_text(
            "# Build plan for TestSvc\n\n"
            "## Phase 1 - Research [status: complete]\n"
            "- [x] research_synthesis.json written\n\n"
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
        assert "harness.py imports and exposes run(input_data)" in section
        assert "live_test.py passes" in section
        # Should NOT include subsequent sections
        assert "Budget" not in section

    def test_returns_empty_when_section_missing(self):
        assert context_compaction._extract_outstanding_success_criteria("") == ""
        assert context_compaction._extract_outstanding_success_criteria("# No section here") == ""


class TestBuildPlanIsNotCompactionAuthority:
    def test_no_phase_2_todo_extractor(self):
        assert not hasattr(context_compaction, "_extract_phase_2_todos")


# ---------------------------------------------------------------------------
# Compose state packet
# ---------------------------------------------------------------------------


class TestComposeCanonicalStatePacket:
    def test_packet_directs_opus_imperatively(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        # Imperative tone — direct fix for narrative-inertia bias.
        assert "You are the builder" in packet
        assert "without ritual rereads" in packet.lower()
        # The packet must point at on-disk artifacts (Codex's "orchestrator
        # owns truth" — the agent reads from disk, not from inherited
        # conversation history).
        assert "_agent_state/research_synthesis.json" in packet
        assert "_agent_state/implementation_plan.json" in packet
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
        assert "harness.py imports and exposes run(input_data)" in packet
        assert "All test cases pass" in packet

    def test_packet_ignores_build_plan_when_plan_present(self, tmp_path: Path):
        _seed_sandbox(tmp_path, with_build_plan=True)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert "Phase 2 todos from build_plan.md" not in packet
        assert "Build plan for TestSvc" not in packet

    def test_packet_omits_plan_section_when_plan_absent(self, tmp_path: Path):
        _seed_sandbox(tmp_path, with_build_plan=False)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert "Phase 2 todos from build_plan.md" not in packet

    def test_packet_handles_missing_runtime_state_gracefully(self, tmp_path: Path):
        _seed_sandbox(tmp_path, with_runtime_state=False)
        # Should still compose — falls back to "(none)" / "?" placeholders.
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert "post-compaction restoration packet" in packet

    def test_packet_restores_bounded_load_bearing_artifacts(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert "fetched_current_api_docs" in packet
        assert "voice_conversation" in packet
        assert "caller_audio_url" in packet
        assert "persistent_worker" in packet
        assert "full fetched docs" in packet
        assert len(packet) <= context_compaction.MAX_RESTORATION_PACKET_CHARS + 80

    def test_packet_writes_post_compaction_snapshot(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        snapshot = json.loads(
            (tmp_path / "_agent_state" / "post_compaction_snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        assert snapshot["event_name"] == context_compaction.COMPACTION_EVENT_NAME
        assert "_agent_state/objective.md" in snapshot["included_artifacts"]
        assert "full research findings" in snapshot["omitted_artifacts"]
        assert snapshot["packet_char_count"] == len(packet)


# ---------------------------------------------------------------------------
# Top-level compaction entry point
# ---------------------------------------------------------------------------


class TestCompactForBuildGate:
    def test_returns_single_user_message(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        msgs = context_compaction.compact_for_build_gate(tmp_path)
        assert isinstance(msgs, list)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert isinstance(msgs[0]["content"], str)

    def test_message_content_is_the_canonical_packet(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        msgs = context_compaction.compact_for_build_gate(tmp_path)
        packet = context_compaction.compose_canonical_state_packet(tmp_path)
        assert msgs[0]["content"] == packet

    def test_old_entrypoint_remains_compatibility_wrapper(self, tmp_path: Path):
        _seed_sandbox(tmp_path)
        assert context_compaction.compact_for_model_transition(tmp_path) == (
            context_compaction.compact_for_build_gate(tmp_path)
        )
