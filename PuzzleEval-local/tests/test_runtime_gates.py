"""Tests for runtime contract gates (Phase 0 — AD-007 enforcement).

Verifies the VoiceHarnessGate and the gate registry pattern. Per AD-007,
gates are Python-only — they don't read configuration from markdown.
This test validates the contract: gates run regardless of what
capability_playbooks/voice.md says about its paired_gates field.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from puzzleeval.contracts import (
    ALWAYS_ON_GATES,
    ContractGate,
    GateReport,
    GateResult,
    TaskContext,
    VoiceHarnessGate,
    gate_by_name,
    gate_names,
    run_gates,
)


def _voice_task() -> TaskContext:
    return TaskContext(
        agent_id="agent_5",
        phase="execute",
        platform="linux",
        test_cases=(
            SimpleNamespace(
                input_type="voice_conversation", output_type="voice_conversation",
            ),
        ),
    )


def _ocr_task() -> TaskContext:
    return TaskContext(
        agent_id="agent_5",
        phase="execute",
        platform="linux",
        test_cases=(
            SimpleNamespace(input_type="document_content", output_type="extraction"),
        ),
    )


# ---------------------------------------------------------------------------
# VoiceHarnessGate.applies_to
# ---------------------------------------------------------------------------


class TestVoiceHarnessGateAppliesTo:
    def test_voice_conversation_triggers_gate(self):
        gate = VoiceHarnessGate()
        assert gate.applies_to(_voice_task())

    def test_voice_turn_triggers_gate(self):
        gate = VoiceHarnessGate()
        task = TaskContext(
            agent_id="agent_5",
            phase="execute",
            platform="linux",
            test_cases=(SimpleNamespace(input_type="voice_turn", output_type="voice_turn"),),
        )
        assert gate.applies_to(task)

    def test_audio_content_triggers_gate(self):
        gate = VoiceHarnessGate()
        task = TaskContext(
            agent_id="agent_5",
            phase="execute",
            platform="linux",
            test_cases=(SimpleNamespace(input_type="audio_content", output_type="audio_content"),),
        )
        assert gate.applies_to(task)

    def test_ocr_does_not_trigger_gate(self):
        gate = VoiceHarnessGate()
        assert not gate.applies_to(_ocr_task())

    def test_text_does_not_trigger_gate(self):
        gate = VoiceHarnessGate()
        task = TaskContext(
            agent_id="agent_5",
            phase="execute",
            platform="linux",
            test_cases=(SimpleNamespace(input_type="text", output_type="free_text"),),
        )
        assert not gate.applies_to(task)


# ---------------------------------------------------------------------------
# VoiceHarnessGate.validate
# ---------------------------------------------------------------------------


class TestVoiceHarnessGateValidate:
    def test_shape_a_audio_bytes_passes(self):
        gate = VoiceHarnessGate()
        state = {
            "raw_response": {
                "audio_bytes": b"fake-audio-data",
                "audio_format": "mp3",
            },
            "success": True,
        }
        result = gate.validate(state, task=_voice_task())
        assert result.passed
        assert result.evidence["shape"] == "A"

    def test_shape_b_audio_path_passes(self):
        gate = VoiceHarnessGate()
        state = {
            "raw_response": {
                "audio_path": "/tmp/response.wav",
            },
            "success": True,
        }
        result = gate.validate(state, task=_voice_task())
        assert result.passed
        assert result.evidence["shape"] == "B"

    def test_both_shapes_fails(self):
        """raw_response with BOTH audio_bytes AND audio_path → fail."""
        gate = VoiceHarnessGate()
        state = {
            "raw_response": {
                "audio_bytes": b"x",
                "audio_path": "/tmp/x.wav",
            },
        }
        result = gate.validate(state, task=_voice_task())
        assert not result.passed
        assert "BOTH" in result.reason

    def test_forbidden_audio_url_key_fails(self):
        gate = VoiceHarnessGate()
        state = {
            "raw_response": {
                "audio_url": "https://example.com/audio.mp3",
            },
        }
        result = gate.validate(state, task=_voice_task())
        assert not result.passed
        assert "audio_url" in result.evidence["forbidden_keys"]

    def test_forbidden_audio_b64_key_fails(self):
        gate = VoiceHarnessGate()
        state = {
            "raw_response": {
                "audio_b64": "ZmFrZQ==",
            },
        }
        result = gate.validate(state, task=_voice_task())
        assert not result.passed
        assert "audio_b64" in result.evidence["forbidden_keys"]

    def test_text_only_response_passes(self):
        """Empty raw_response → text-only fallback path is valid."""
        gate = VoiceHarnessGate()
        state = {
            "raw_response": {},
            "output": "agent text response",
        }
        result = gate.validate(state, task=_voice_task())
        assert result.passed
        assert result.evidence["shape"] == "text"

    def test_no_raw_response_passes(self):
        gate = VoiceHarnessGate()
        state = {"output": "text only"}
        result = gate.validate(state, task=_voice_task())
        assert result.passed

    def test_non_dict_state_fails(self):
        gate = VoiceHarnessGate()
        result = gate.validate("not a dict", task=_voice_task())
        assert not result.passed


# ---------------------------------------------------------------------------
# Registry pattern
# ---------------------------------------------------------------------------


class TestRegistryPattern:
    def test_always_on_gates_includes_voice_harness(self):
        gate_ids = {g.name for g in ALWAYS_ON_GATES}
        assert "voice_harness" in gate_ids

    def test_gate_by_name(self):
        gate = gate_by_name("voice_harness")
        assert gate is not None
        assert isinstance(gate, VoiceHarnessGate)

    def test_gate_by_name_unknown_returns_none(self):
        assert gate_by_name("nonexistent_gate") is None

    def test_gate_names_returns_tuple(self):
        names = gate_names()
        assert isinstance(names, tuple)
        assert "voice_harness" in names

    def test_run_gates_returns_report(self):
        state = {
            "raw_response": {"audio_bytes": b"x"},
            "success": True,
        }
        report = run_gates(state, _voice_task())
        assert isinstance(report, GateReport)
        assert report.all_passed
        assert len(report.results) == 1

    def test_run_gates_skips_inapplicable(self):
        """Non-voice task → voice gate doesn't run."""
        state = {"output": "text"}
        report = run_gates(state, _ocr_task())
        assert isinstance(report, GateReport)
        # No voice gate ran (only voice_harness exists today; for OCR, zero gates)
        assert len(report.results) == 0
        assert report.all_passed  # vacuously

    def test_run_gates_collects_failures(self):
        state = {
            "raw_response": {
                "audio_bytes": b"x",
                "audio_path": "/tmp/x.wav",  # both → fail
            },
        }
        report = run_gates(state, _voice_task())
        assert not report.all_passed
        assert len(report.failures) == 1
        assert report.failures[0].gate_name == "voice_harness"


# ---------------------------------------------------------------------------
# AD-007 invariant
# ---------------------------------------------------------------------------


class TestAD007Invariant:
    """Runtime gates must NOT be configurable via markdown.

    AD-007: contract markdown teaches; Python enforces. Editing voice.md
    cannot disable VoiceHarnessGate. The paired_gates field is descriptive,
    not control.
    """

    def test_voice_md_paired_gates_field_is_descriptive(self):
        """Verify voice.md lists VoiceHarnessGate but the gate runs because
        of the Python registry, not because of this listing."""
        from puzzleeval.contracts import ContractRegistry

        reg = ContractRegistry.cached()
        voice = reg.by_id("voice")
        # The metadata SHOULD reference voice_harness (descriptive)
        assert "voice_harness" in voice.metadata.paired_gates

        # But removing it from metadata wouldn't disable the gate —
        # the gate's existence is determined by ALWAYS_ON_GATES, not
        # by this field. We can't easily test mutation (registry is
        # frozen + cached) but the test_registry_pattern tests above
        # demonstrate that the gate runs based on Python registration.

    def test_paired_gates_references_resolve_to_real_gates(self):
        """Every paired_gates entry must resolve to a real gate class."""
        from puzzleeval.contracts import ContractRegistry

        reg = ContractRegistry.cached()
        for contract in reg:
            for gate_name in contract.metadata.paired_gates:
                gate = gate_by_name(gate_name)
                assert gate is not None, (
                    f"Contract {contract.metadata.id} declares paired gate "
                    f"{gate_name!r} but no such gate is registered. Either "
                    "implement the gate in runtime_gates.py and add to "
                    "ALWAYS_ON_GATES, or remove the metadata reference."
                )
