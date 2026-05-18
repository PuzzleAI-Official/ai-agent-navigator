"""Regression guards for the "Sandbox Starting Contents" block + the
Agent 4 doc-handoff instrumentation.

Real-run trace 2b2b9d1f (2026-04-22) surfaced two waste patterns:

  1. **T0 `os.listdir` probe**: every candidate build's first turn
     ran `python -c "import os; print(os.listdir('.'))"` costing
     ~$0.10 per candidate. Over a 2-candidate run that's $0.20 of
     pure waste — the initial message never told the builder what
     was in the sandbox, so it probed out of reasonable curiosity.

  2. **Silent Agent 4 doc handoff failure**: `_verify_single_candidate`
     in screening.py had doc-handoff code but logged exceptions at
     `debug` level. uvicorn's default INFO cutoff suppressed them.
     Result: we couldn't tell why zero prefetched docs landed in the
     sandbox even though Agent 4 clearly web_fetched.

Fixes:
  - New `_format_sandbox_contents_block` enumerates sandbox contents
    in the initial message AND includes an explicit "DO NOT PROBE"
    rule so the builder skips the os.listdir on turn 0.
  - Screening instrumentation promoted to INFO on both attempt +
    exception paths, logging block types present in each response so
    next run's diagnostics tell us whether Agent 4 had web_fetch
    results to save at all.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMPL_SRC = (ROOT / "puzzleeval" / "agents" / "implement_test_env.py").read_text(encoding="utf-8")
SCREENING_SRC = ((ROOT / "puzzleeval" /"agents" / "agent4" / "core.py").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent4" / "templates" / "verification_system.md").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent4" / "templates" / "structure_system.md").read_text(encoding="utf-8"))


# ============================================================================
# Sandbox contents block
# ============================================================================


class TestSandboxContentsBlockExists:

    def test_helper_function_defined(self):
        assert "_format_sandbox_contents_block" in IMPL_SRC

    def test_helper_called_from_initial_message(self):
        fn_start = IMPL_SRC.find("def _build_initial_message")
        next_fn = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:next_fn if next_fn != -1 else len(IMPL_SRC)]
        assert "_format_sandbox_contents_block" in fn_body, (
            "Initial message must call _format_sandbox_contents_block "
            "so the contents listing actually appears in the prompt."
        )

    def test_helper_listed_before_research_inputs(self):
        """Ordering: the sandbox inventory should appear BEFORE the
        consolidated research-inputs section. Otherwise the builder reads
        the teaching first but doesn't have the inventory to act on."""
        fn_start = IMPL_SRC.find("def _build_initial_message")
        next_fn = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:next_fn if next_fn != -1 else len(IMPL_SRC)]
        contents_pos = fn_body.find("_format_sandbox_contents_block")
        research_inputs_pos = fn_body.find("_format_research_inputs_block")
        assert contents_pos < research_inputs_pos


class TestSandboxContentsRenders:
    """Behavioral tests of the helper — does it produce the right
    text for the builder to read?"""

    def _helper(self):
        from puzzleeval.agents.implement_test_env import _format_sandbox_contents_block
        return _format_sandbox_contents_block

    def test_empty_string_when_sandbox_dir_none(self):
        helper = self._helper()
        assert helper(None, []) == ""

    def test_empty_string_for_missing_sandbox(self, tmp_path):
        helper = self._helper()
        missing = tmp_path / "does-not-exist"
        assert helper(missing, []) == ""

    def test_empty_string_for_empty_sandbox(self, tmp_path):
        helper = self._helper()
        assert helper(tmp_path, []) == ""

    def test_lists_prefetched_docs_separately(self, tmp_path):
        helper = self._helper()
        (tmp_path / "fetched_docs_0.txt").write_text("# Fetched from: https://x.com/")
        (tmp_path / "fetched_docs_1.txt").write_text("content")
        rendered = helper(tmp_path, [])
        assert "Pre-fetched API documentation" in rendered
        assert "fetched_docs_0.txt" in rendered
        assert "fetched_docs_1.txt" in rendered

    def test_lists_subdirectories(self, tmp_path):
        helper = self._helper()
        (tmp_path / "test_inputs").mkdir()
        rendered = helper(tmp_path, [])
        assert "test_inputs/" in rendered

    def test_forbids_probing_explicitly(self, tmp_path):
        """The whole point of the block: stop the builder from running
        os.listdir on turn 0. This rule MUST appear in the rendered
        text with all three common probe commands named."""
        helper = self._helper()
        (tmp_path / "fetched_docs_0.txt").write_text("# Fetched from: https://x.com/")
        rendered = helper(tmp_path, [])
        assert "DO NOT PROBE" in rendered or "do not run" in rendered.lower()
        assert "os.listdir" in rendered
        assert "ls" in rendered
        assert "dir" in rendered.lower()

    def test_flags_already_existing_harness_files(self, tmp_path):
        """If harness.py already exists (rerun scenario), the builder
        should be nudged toward patch_file instead of stomping."""
        helper = self._helper()
        (tmp_path / "harness.py").write_text("# old version")
        rendered = helper(tmp_path, [])
        assert "harness.py" in rendered
        assert "already exists" in rendered or "patch_file" in rendered

    def test_lists_staged_test_files(self, tmp_path):
        """Test file paths come from TestCase.test_file_path. The block
        should surface them so the builder knows what the harness will
        actually run against."""
        helper = self._helper()
        (tmp_path / "some_file.txt").write_text("x")
        fake_tc = SimpleNamespace(
            id="tc1", scenario="test",
            test_file_path="/tmp/test_inputs/invoice_001.pdf",
        )
        rendered = helper(tmp_path, [fake_tc])
        assert "invoice_001.pdf" in rendered or "test_inputs/invoice_001.pdf" in rendered

    def test_includes_turn_0_rule(self):
        """The teaching must be EXPLICIT: your first tool call should
        be productive work, not an inventory probe."""
        from puzzleeval.agents.implement_test_env import _format_sandbox_contents_block
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "fetched_docs_0.txt").write_text("# Fetched from: https://x.com/")
            rendered = _format_sandbox_contents_block(p, [])
        # The rule must mention "turn 0" + what's ALLOWED (not just forbidden)
        assert "first tool call" in rendered.lower() or "Turn 0" in rendered
        # And it must name at least one productive alternative
        assert "web_fetch" in rendered or "read_file" in rendered or "write_file" in rendered


# ============================================================================
# Screening instrumentation (promoted to INFO)
# ============================================================================


class TestScreeningDocHandoffLogging:

    def test_attempt_logged_at_info_level(self):
        """Every handoff attempt must log at INFO — tells us whether
        the code path fired at all on a real run, regardless of whether
        it saved anything."""
        fn_start = SCREENING_SRC.find("def _verify_single_candidate")
        next_fn = SCREENING_SRC.find("\ndef ", fn_start + 1)
        fn_body = SCREENING_SRC[fn_start:next_fn]
        # The attempt log must be unconditional (inside try, after save)
        assert "doc handoff attempted" in fn_body.lower(), (
            "Must log on every attempt at INFO — otherwise real-run "
            "diagnostics can't distinguish 'code didn't run' from "
            "'code ran but saved nothing'."
        )

    def test_exception_logged_at_warning_level(self):
        """Promoted from debug → warning. uvicorn's default cutoff is
        INFO, so debug entries were invisible in real-run logs."""
        fn_start = SCREENING_SRC.find("def _verify_single_candidate")
        next_fn = SCREENING_SRC.find("\ndef ", fn_start + 1)
        fn_body = SCREENING_SRC[fn_start:next_fn]
        # Find the handoff's except block
        handoff_start = fn_body.find("candidate_sandbox_dir")
        after_handoff = fn_body[handoff_start:]
        # Within the handoff's try/except, must use logger.warning
        # (not logger.debug) for the exception path
        except_idx = after_handoff.find("except Exception")
        assert except_idx != -1
        except_block = after_handoff[except_idx:except_idx + 800]
        assert "logger.warning" in except_block, (
            "Handoff exception must log at WARNING — debug-level is "
            "filtered by uvicorn's default INFO cutoff."
        )

    def test_logs_block_types_for_diagnostics(self):
        """The attempt log must include the response's block types so
        we can see at a glance whether Agent 4 had web_fetch_tool_result
        blocks (saveable) vs web_search_tool_result only (snippets) vs
        just text (nothing to save)."""
        fn_start = SCREENING_SRC.find("def _verify_single_candidate")
        next_fn = SCREENING_SRC.find("\ndef ", fn_start + 1)
        fn_body = SCREENING_SRC[fn_start:next_fn]
        assert "block_types" in fn_body, (
            "Must enumerate response block types in the handoff log — "
            "otherwise we can't diagnose WHY zero docs landed on disk."
        )
