"""Tests for puzzleeval.test_data_sufficiency.

Covers the action ladder, file inventory, plugin-augment lookup, and
the test_plan walker. No network, no Claude calls.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from puzzleeval.test_data_sufficiency import (
    DEFAULT_MIN_FILES,
    IDEAL_MIN_FILES,
    FileInventory,
    SufficiencyVerdict,
    assess_sufficiency,
    assess_test_plan,
    inventory_files,
    summarize_verdicts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_files(tmp_path: Path):
    """Factory: create N tempfiles with given extensions, return paths."""
    def _make(specs: list[tuple[str, int]]) -> list[str]:
        out: list[str] = []
        for ext, count in specs:
            for i in range(count):
                p = tmp_path / f"sample_{ext.strip('.')}{i}{ext}"
                p.write_bytes(b"x" * 16)
                out.append(str(p))
        return out
    return _make


# ---------------------------------------------------------------------------
# inventory_files
# ---------------------------------------------------------------------------


def test_inventory_empty_returns_zeroed():
    inv = inventory_files([])
    assert inv.total_count == 0
    assert inv.by_extension == {}
    assert inv.suspected_single_source is False


def test_inventory_none_returns_zeroed():
    inv = inventory_files(None)
    assert inv.total_count == 0


def test_inventory_counts_by_extension(tmp_files):
    paths = tmp_files([(".pdf", 3), (".png", 2)])
    inv = inventory_files(paths)
    assert inv.total_count == 5
    assert inv.by_extension == {".pdf": 3, ".png": 2}
    assert inv.extension_count == 2


def test_inventory_records_missing(tmp_path):
    real = tmp_path / "real.pdf"
    real.write_bytes(b"x")
    inv = inventory_files([str(real), str(tmp_path / "fake.pdf")])
    assert inv.total_count == 1
    assert len(inv.missing_paths) == 1


def test_inventory_detects_single_source_prefix(tmp_path):
    for i in range(3):
        (tmp_path / f"invoice_{i}.pdf").write_bytes(b"x")
    paths = sorted(str(p) for p in tmp_path.glob("invoice_*.pdf"))
    inv = inventory_files(paths)
    assert inv.suspected_single_source is True


def test_inventory_does_not_flag_varied_names(tmp_path):
    (tmp_path / "alpha.pdf").write_bytes(b"x")
    (tmp_path / "beta.pdf").write_bytes(b"x")
    (tmp_path / "gamma.pdf").write_bytes(b"x")
    inv = inventory_files([
        str(tmp_path / "alpha.pdf"),
        str(tmp_path / "beta.pdf"),
        str(tmp_path / "gamma.pdf"),
    ])
    assert inv.suspected_single_source is False


# ---------------------------------------------------------------------------
# assess_sufficiency
# ---------------------------------------------------------------------------


def test_no_files_required_returns_ready():
    v = assess_sufficiency(
        scope_id="step_1", input_type="text", output_type="free_text",
        requires_test_files=False, file_paths=None,
    )
    assert v.action == "ready"
    assert "does not require" in v.reason


def test_ideal_count_returns_ready(tmp_files):
    paths = tmp_files([(".pdf", IDEAL_MIN_FILES)])
    v = assess_sufficiency(
        scope_id="step_1", input_type="document_content",
        output_type="structured_json",
        requires_test_files=True, file_paths=paths,
    )
    assert v.action == "ready"
    assert v.degraded_confidence == 1.0


def test_min_count_returns_ready_with_advisory(tmp_files):
    paths = tmp_files([(".pdf", DEFAULT_MIN_FILES)])
    v = assess_sufficiency(
        scope_id="step_1", input_type="document_content",
        output_type="structured_json",
        requires_test_files=True, file_paths=paths,
    )
    assert v.action == "ready"
    assert v.degraded_confidence < 1.0
    assert any("more sample" in a for a in v.advisories)


def test_below_min_returns_request_more(tmp_files):
    paths = tmp_files([(".pdf", 1)])  # 1 < DEFAULT_MIN_FILES (3)
    v = assess_sufficiency(
        scope_id="step_1", input_type="document_content",
        output_type="structured_json",
        requires_test_files=True, file_paths=paths,
    )
    assert v.action == "request_more"
    assert v.request_message
    assert "Please upload" in v.request_message


def test_wrong_extension_returns_request_more(tmp_files):
    paths = tmp_files([(".mp4", 5)])  # mp4 not in structured_json valid exts
    v = assess_sufficiency(
        scope_id="step_1", input_type="document_content",
        output_type="structured_json",
        requires_test_files=True, file_paths=paths,
    )
    assert v.action == "request_more"
    assert ".mp4" in v.request_message


def test_no_files_falls_back_to_synthesize_when_no_plugin():
    v = assess_sufficiency(
        scope_id="step_1", input_type="document_content",
        output_type="structured_json",
        requires_test_files=True, file_paths=[],
        available_plugin_names=set(),
    )
    assert v.action == "synthesize"
    assert v.degraded_confidence < 1.0


def test_no_files_uses_augment_when_plugin_available():
    # output_type=audio_content has TTS plugin in the augment table
    v = assess_sufficiency(
        scope_id="step_1", input_type="audio_content",
        output_type="audio_content",
        requires_test_files=True, file_paths=[],
        available_plugin_names={"tts"},
    )
    assert v.action == "augment"
    assert v.plugin_for_augment == "tts"


def test_augment_for_webhook_callback():
    v = assess_sufficiency(
        scope_id="step_1", input_type="webhook_event",
        output_type="structured_json",
        requires_test_files=True, file_paths=[],
        available_plugin_names={"webhook_receiver"},
    )
    assert v.action == "augment"
    assert v.plugin_for_augment == "webhook_receiver"


def test_augment_for_outbound_action():
    v = assess_sufficiency(
        scope_id="step_1", input_type="text", output_type="action",
        requires_test_files=True, file_paths=[],
        available_plugin_names={"outbound_delivery"},
    )
    assert v.action == "augment"
    assert v.plugin_for_augment == "outbound_delivery"


def test_augment_for_explicit_webhook_callback():
    """The new direct plugin-modality output_type maps 1:1 to its plugin."""
    v = assess_sufficiency(
        scope_id="step_1", input_type="webhook_event",
        output_type="webhook_callback",
        requires_test_files=True, file_paths=[],
        available_plugin_names={"webhook_receiver"},
    )
    assert v.action == "augment"
    assert v.plugin_for_augment == "webhook_receiver"


def test_augment_for_explicit_outbound_message():
    v = assess_sufficiency(
        scope_id="step_1", input_type="text",
        output_type="outbound_message",
        requires_test_files=True, file_paths=[],
        available_plugin_names={"outbound_delivery"},
    )
    assert v.action == "augment"
    assert v.plugin_for_augment == "outbound_delivery"


def test_augment_for_voice_turn():
    """voice_turn → voice_realtime — closes the gap surfaced in audit."""
    v = assess_sufficiency(
        scope_id="step_1", input_type="voice_turn",
        output_type="voice_turn",
        requires_test_files=True, file_paths=[],
        available_plugin_names={"voice_realtime"},
    )
    assert v.action == "augment"
    assert v.plugin_for_augment == "voice_realtime"


def test_single_source_advisory_attached_when_ready(tmp_path):
    for i in range(IDEAL_MIN_FILES):
        (tmp_path / f"invoice_{i}.pdf").write_bytes(b"x")
    paths = [str(tmp_path / f"invoice_{i}.pdf") for i in range(IDEAL_MIN_FILES)]
    v = assess_sufficiency(
        scope_id="step_1", input_type="document_content",
        output_type="structured_json",
        requires_test_files=True, file_paths=paths,
    )
    assert v.action == "ready"
    assert any("common prefix" in a for a in v.advisories)


# ---------------------------------------------------------------------------
# assess_test_plan + summarize_verdicts
# ---------------------------------------------------------------------------


class _FakeSpec:
    def __init__(self, scope_id, input_type, output_type):
        self.scope_id = scope_id
        self.input_type = input_type
        self.output_type = output_type


class _FakePlan:
    def __init__(self, specs):
        self.scope_specs = specs


def test_assess_test_plan_walks_each_scope():
    plan = _FakePlan([
        _FakeSpec("step_1", "document_content", "structured_json"),
        _FakeSpec("step_2", "text", "free_text"),
    ])
    out = assess_test_plan(plan)
    assert set(out.keys()) == {"step_1", "step_2"}
    assert out["step_1"].action in {"synthesize", "request_more", "ready", "augment"}
    # text scope doesn't require files by default
    assert out["step_2"].action == "ready"


def test_assess_test_plan_none_returns_empty():
    assert assess_test_plan(None) == {}


def test_summarize_collects_advisories_and_min_confidence(tmp_files):
    plan = _FakePlan([
        _FakeSpec("a", "document_content", "structured_json"),
        _FakeSpec("b", "document_content", "structured_json"),
    ])
    paths = tmp_files([(".pdf", 1)])
    out = assess_test_plan(
        plan, file_paths_by_scope={"a": paths, "b": []},
    )
    summary = summarize_verdicts(out)
    assert summary["total_scopes"] == 2
    assert summary["min_confidence"] < 1.0
    assert summary["needs_user_action"] is True
    assert any("a" == m["scope_id"] for m in summary["request_messages"])
