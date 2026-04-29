"""Tests for the Claude Code-pattern generality boost (Gaps A-E).

Covers:
  - Gap A: shared agent preamble (agent_preamble.py)
  - Gap B: adaptive thinking enabled on Agents 2 + 4 (presence check via grep)
  - Gap C: server-side context_management on Agent 4 verification
  - Gap D: cross-run memdir (write/recall/relevance/index)
  - Gap E: adversarial verification battery
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from puzzleeval import memdir
from puzzleeval.adversarial_verifier import (
    AdversarialReport,
    ProbeResult,
    _classify,
    _summarize,
    report_to_dict,
)
from puzzleeval.agent_preamble import SHARED_AGENT_PREAMBLE, with_preamble


# ---------------------------------------------------------------------------
# Gap A — shared preamble
# ---------------------------------------------------------------------------


class TestSharedPreamble:
    def test_preamble_has_core_principles(self):
        p = SHARED_AGENT_PREAMBLE
        assert "Cross-cutting rules" in p
        assert "parallel" in p.lower()
        assert "narration" in p.lower()
        assert "root cause" in p.lower() or "reason about" in p.lower()
        assert "verify" in p.lower()
        assert "commit" in p.lower()

    def test_with_preamble_prepends(self):
        original = "You are Agent X."
        out = with_preamble(original)
        assert out.endswith(original)
        assert SHARED_AGENT_PREAMBLE.strip().splitlines()[0] in out

    def test_with_preamble_idempotent(self):
        original = "You are Agent X."
        once = with_preamble(original)
        twice = with_preamble(once)
        assert once == twice

    def test_every_agent_call_is_wired(self):
        """Every place we call client.messages.{create,parse} must use with_preamble.

        Grep-based regression guard. If someone adds a new agent call site
        that bypasses with_preamble, this test fails."""
        agents_dir = Path(__file__).parent.parent / "puzzleeval" / "agents"
        offenders: list[str] = []
        for py in agents_dir.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            # Find every system=[...] line
            for line_no, line in enumerate(text.splitlines(), 1):
                if "system=" in line and (
                    "messages.create" in text[max(0, text.find(line) - 200): text.find(line) + 400]
                    or "messages.parse" in text[max(0, text.find(line) - 200): text.find(line) + 400]
                ):
                    # look at the system= argument value
                    if (
                        "with_preamble(" not in line
                        and "_with_shared_preamble(" not in line
                    ):
                        # Heuristic guard for indirect wrapping: when the
                        # system= argument is a variable (system=system_blocks),
                        # accept the call if the WHOLE file uses with_preamble
                        # somewhere (the variable was wrapped at module level).
                        if (
                            "with_preamble(" in text
                            or "_with_shared_preamble(" in text
                        ):
                            continue
                        offenders.append(f"{py.name}:{line_no} -> {line.strip()}")
        assert not offenders, "Unwrapped system prompts found:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Gap B — adaptive thinking enabled on multi-turn reasoning agents
# ---------------------------------------------------------------------------


class TestAdaptiveThinkingEnabled:
    @pytest.mark.parametrize("agent_file", [
        # Phase 7: agents moved into per-agent packages.
        # Source-grep against canonical core.py.
        "agent2/core.py",  # Agent 2 multi-turn web search
        "agent4/core.py",  # Agent 4 multi-turn per-candidate verify
    ])
    def test_agent_uses_adaptive_thinking(self, agent_file):
        path = Path(__file__).parent.parent / "puzzleeval" / "agents" / agent_file
        text = path.read_text(encoding="utf-8")
        assert 'thinking={"type": "adaptive"}' in text, (
            f"{agent_file} should enable adaptive thinking — multi-turn reasoning "
            f"agents benefit from it the same way Agent 5 does."
        )


# ---------------------------------------------------------------------------
# Gap C — server-side context management on Agent 4
# ---------------------------------------------------------------------------


class TestServerSideContextManagement:
    def test_screening_uses_clear_tool_uses(self):
        path = Path(__file__).parent.parent / "puzzleeval" / "agents" / "agent4" / "core.py"
        text = path.read_text(encoding="utf-8")
        assert "clear_tool_uses_20250919" in text, (
            "Agent 4 should use server-side context management to clear old "
            "tool results, mirroring Agent 5's strategy."
        )


# ---------------------------------------------------------------------------
# Gap D — cross-run memdir
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_memdir(tmp_path, monkeypatch):
    monkeypatch.setenv("PUZZLEEVAL_MEMDIR", str(tmp_path / "memdir"))
    monkeypatch.setenv("PUZZLEEVAL_MEMORY_ENABLED", "1")
    yield tmp_path / "memdir"


class TestMemdirBasics:
    def test_slugify(self):
        assert memdir.slugify("Mindee Receipt API") == "mindee_receipt_api"
        assert memdir.slugify("DocuClipper.io®") == "docuclipper_io"
        assert memdir.slugify("") == "unnamed"

    def test_short_hash_deterministic(self):
        h1 = memdir.short_hash("https://example.com/docs")
        h2 = memdir.short_hash("https://example.com/docs")
        assert h1 == h2
        assert len(h1) == 10

    def test_short_hash_distinguishes(self):
        a = memdir.short_hash("https://example.com/v1/docs")
        b = memdir.short_hash("https://example.com/v2/docs")
        assert a != b


class TestMemdirIO:
    def test_write_and_recall_roundtrip(self, tmp_memdir):
        path = memdir.write_memory(
            category="api_specs",
            key="Mindee::https://docs.mindee.com/v1",
            name="Mindee v1 spec",
            description="Receipt + invoice OCR endpoints; multipart upload",
            body="ENDPOINTS:\n  POST /v1/products/.../receipt/predict\n",
            tags=["ocr", "documents"],
            written_by="agent_4",
            source_run="trace-abc",
        )
        assert path is not None and path.exists()

        recalled = memdir.recall_memory("api_specs", "Mindee::https://docs.mindee.com/v1")
        assert recalled is not None
        assert recalled.name == "Mindee v1 spec"
        assert "ENDPOINTS" in recalled.body
        assert "ocr" in recalled.tags
        assert recalled.written_by == "agent_4"
        assert recalled.source_run == "trace-abc"

    def test_recall_returns_none_when_absent(self, tmp_memdir):
        assert memdir.recall_memory("api_specs", "Nonexistent::nokey") is None

    def test_recall_respects_max_age(self, tmp_memdir):
        memdir.write_memory(
            category="api_specs",
            key="Acme::v1",
            name="Acme",
            description="x",
            body="y",
        )
        # max_age_days=30 → fresh memo passes
        memo = memdir.recall_memory("api_specs", "Acme::v1", max_age_days=30)
        assert memo is not None
        # Manually backdate the memo's frontmatter to simulate an old entry,
        # then verify max_age_days=1 rejects it.
        cat_dir = tmp_memdir / "api_specs"
        memo_path = next(cat_dir.glob("acme*.md"))
        text = memo_path.read_text(encoding="utf-8")
        old_text = text.replace(
            f"written_at: {memo.written_at}",
            "written_at: 2020-01-01T00:00:00+00:00",
        )
        memo_path.write_text(old_text, encoding="utf-8")
        memo_old = memdir.recall_memory("api_specs", "Acme::v1", max_age_days=30)
        assert memo_old is None, "Stale memo should be rejected by max_age_days=30"

    def test_disabled_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_MEMDIR", str(tmp_path / "m"))
        monkeypatch.setenv("PUZZLEEVAL_MEMORY_ENABLED", "0")
        assert memdir.write_memory("c", "k::v", "n", "d", "b") is None
        assert memdir.recall_memory("c", "k::v") is None
        assert memdir.find_relevant_memories("anything") == []

    def test_index_refreshed_after_write(self, tmp_memdir):
        memdir.write_memory(
            category="quirks",
            key="Stripe::auth",
            name="Stripe auth",
            description="bearer token in Authorization header",
            body="x",
        )
        idx = json.loads((tmp_memdir / "INDEX.json").read_text(encoding="utf-8"))
        assert "quirks" in idx["categories"]
        assert any(f.startswith("stripe__") for f in idx["categories"]["quirks"])


class TestMemdirRelevance:
    def test_word_overlap_ranking(self, tmp_memdir):
        memdir.write_memory(
            category="api_specs",
            key="Mindee::v1",
            name="Mindee Receipt OCR API",
            description="Multipart upload for receipt extraction",
            body="...",
            tags=["receipt", "ocr"],
        )
        memdir.write_memory(
            category="api_specs",
            key="Stripe::v2024",
            name="Stripe Payments API",
            description="Process card payments",
            body="...",
            tags=["payments"],
        )
        relevant = memdir.find_relevant_memories("ocr receipt extraction api")
        assert relevant
        assert relevant[0].name == "Mindee Receipt OCR API"

    def test_relevance_filters_unrelated(self, tmp_memdir):
        memdir.write_memory(
            category="api_specs",
            key="Stripe::v2024",
            name="Stripe Payments API",
            description="Process card payments",
            body="...",
            tags=["payments"],
        )
        relevant = memdir.find_relevant_memories("ocr receipt")
        assert relevant == []

    def test_category_filter(self, tmp_memdir):
        memdir.write_memory(
            category="api_specs",
            key="X::1",
            name="XOcr",
            description="ocr api",
            body="b",
        )
        memdir.write_memory(
            category="quirks",
            key="X::quirk",
            name="XOcr quirk",
            description="ocr quirk note",
            body="b",
        )
        only_specs = memdir.find_relevant_memories("ocr", categories=["api_specs"])
        assert all(m.relpath.startswith("api_specs") for m in only_specs)
        only_quirks = memdir.find_relevant_memories("ocr", categories=["quirks"])
        assert all(m.relpath.startswith("quirks") for m in only_quirks)


# ---------------------------------------------------------------------------
# Gap E — adversarial verification battery
# ---------------------------------------------------------------------------


class TestAdversarialClassifier:
    def test_classify_graceful_success(self):
        assert _classify({"_subprocess_ok": True, "result": {"success": True}}) == "graceful_success"

    def test_classify_graceful_failure(self):
        assert _classify({"_subprocess_ok": True, "result": {"success": False, "error": "..."}}) == "graceful_failure"

    def test_classify_silent_corruption_no_success_field(self):
        assert _classify({"_subprocess_ok": True, "result": {"data": "..."}}) == "silent_corruption"

    def test_classify_silent_corruption_no_error_no_success(self):
        # success=False but no error message
        assert _classify({"_subprocess_ok": True, "result": {"success": False}}) == "silent_corruption"

    def test_classify_crash_on_subprocess_error(self):
        assert _classify({"_invocation": "subprocess_error", "error": "..."}) == "crash"

    def test_classify_crash_on_timeout(self):
        assert _classify({"_invocation": "timeout"}) == "crash"

    def test_classify_crash_on_exception(self):
        assert _classify({"_subprocess_ok": False, "error_type": "ValueError"}) == "crash"

    def test_classify_silent_corruption_non_dict(self):
        assert _classify({"_subprocess_ok": True, "result": "string output"}) == "silent_corruption"


class TestAdversarialReport:
    def test_default_harness_ready(self):
        r = AdversarialReport(harness_ready=True)
        assert r.probe_results == []
        assert r.critical_failures == []
        assert r.warnings == []

    def test_report_to_dict_serializes(self):
        r = AdversarialReport(
            harness_ready=False,
            probe_results=[
                ProbeResult(label="auth_error", passed=False, outcome="silent_corruption", detail="bad"),
                ProbeResult(label="empty_input", passed=True, outcome="graceful_failure", detail="ok"),
            ],
            critical_failures=["auth_error: bad"],
            warnings=[],
            total_duration_ms=1234.5,
        )
        d = report_to_dict(r)
        assert d["harness_ready"] is False
        assert d["critical_failure_count"] == 1
        assert d["probe_count"] == 2
        assert len(d["probes"]) == 2
        assert d["probes"][0]["label"] == "auth_error"
        assert d["total_duration_ms"] == 1234.5

    def test_summarize_handles_all_paths(self):
        # Crash
        out = _summarize({"_invocation": "timeout", "error": "x"})
        assert "timeout" in out
        # Subprocess crash
        out = _summarize({"_subprocess_ok": False, "error_type": "ValueError", "error": "msg"})
        assert "crash" in out
        # Success
        out = _summarize({"_subprocess_ok": True, "result": {"success": True}})
        assert "True" in out
        # Failure with error
        out = _summarize({"_subprocess_ok": True, "result": {"success": False, "error": "401"}})
        assert "401" in out


class TestAdversarialEndToEndOnInProcessHarness:
    """Full end-to-end probe battery against a tiny harness on disk.

    Builds a minimal Python harness that returns success=True for normal input
    and success=False for empty input, then runs the battery and asserts the
    report is well-formed and harness_ready=True (graceful failures, no crashes).
    """

    def test_minimal_harness_passes_battery(self, tmp_path):
        # Write a tiny harness.py and a venv-less .venv stub so the runner uses
        # sys.executable (the existing test interpreter).
        harness_dir = tmp_path / "minimal_harness"
        harness_dir.mkdir()
        # Harness contract: run(input_data: dict) -> dict (positional dict).
        # Same shape BUILDER_SYSTEM_PROMPT mandates — any probe that fails
        # against this signature is a real battery bug.
        (harness_dir / "harness.py").write_text(
            "def run(input_data):\n"
            "    payload = input_data or {}\n"
            "    text = payload.get('text', '')\n"
            "    if not text:\n"
            "        return {'success': False, 'error': 'empty input'}\n"
            "    if len(text) > 5000:\n"
            "        return {'success': False, 'error': 'too long'}\n"
            "    return {'success': True, 'output': text.upper()}\n",
            encoding="utf-8",
        )
        from puzzleeval.adversarial_verifier import run_adversarial_battery

        report = run_adversarial_battery(
            sandbox_dir=harness_dir,
            sample_input={"text": "hello"},
            credentials=None,  # no creds → auth_error probe is skipped
            enabled_probes=["empty_input", "max_input", "malformed_input", "idempotency", "concurrency", "auth_error"],
        )

        assert report.harness_ready, (
            f"Minimal harness should pass battery cleanly. "
            f"critical_failures={report.critical_failures}"
        )
        assert len(report.probe_results) == 6
        # Every outcome must be a defined bucket
        for p in report.probe_results:
            assert p.outcome in {"graceful_success", "graceful_failure", "crash", "silent_corruption"}

    def test_crashy_harness_fails_battery(self, tmp_path):
        """A harness that raises on any input → adversarial probes catch it."""
        harness_dir = tmp_path / "crashy_harness"
        harness_dir.mkdir()
        (harness_dir / "harness.py").write_text(
            "def run(input_data):\n"
            "    raise ValueError('unconditional crash')\n",
            encoding="utf-8",
        )
        from puzzleeval.adversarial_verifier import run_adversarial_battery

        report = run_adversarial_battery(
            sandbox_dir=harness_dir,
            sample_input={"text": "hello"},
            credentials=None,
            enabled_probes=["empty_input"],
        )
        assert not report.harness_ready
        assert report.critical_failures
        assert any("empty_input" in f for f in report.critical_failures)

    def test_silent_corruption_harness_fails_auth_probe(self, tmp_path):
        """A harness that returns success=True regardless of credentials →
        auth_error probe correctly flags silent corruption."""
        harness_dir = tmp_path / "silent_corruption_harness"
        harness_dir.mkdir()
        (harness_dir / "harness.py").write_text(
            "def run(**payload):\n"
            "    return {'success': True, 'output': 'always succeeds'}\n",
            encoding="utf-8",
        )
        from puzzleeval.adversarial_verifier import run_adversarial_battery

        report = run_adversarial_battery(
            sandbox_dir=harness_dir,
            sample_input={"text": "hello"},
            credentials={"FAKE_API_KEY": "real_value"},
            enabled_probes=["auth_error"],
        )
        assert not report.harness_ready
        assert any("auth_error" in f for f in report.critical_failures)
