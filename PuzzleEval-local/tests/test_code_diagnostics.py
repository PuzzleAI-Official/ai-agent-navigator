from __future__ import annotations

import json
from pathlib import Path

from puzzleeval.agents.agent5.code_diagnostics import (
    REGISTRY_RELATIVE_PATH,
    completion_blocking_diagnostics,
    format_new_diagnostics_for_tool_result,
    read_code_diagnostics,
    run_python_diagnostics,
    summarize_code_diagnostics,
)


def test_valid_python_write_records_clean_check(tmp_path: Path):
    (tmp_path / "harness.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = run_python_diagnostics(
        tmp_path,
        ["harness.py"],
        turn=1,
        source_tool="write_file",
    )

    assert result.checked_paths == ["harness.py"]
    assert result.new_diagnostics == []
    registry = read_code_diagnostics(tmp_path)
    assert registry["files"]["harness.py"]["active_diagnostics"] == []
    assert (tmp_path / REGISTRY_RELATIVE_PATH).exists()


def test_syntax_error_is_reported_and_deduped(tmp_path: Path):
    (tmp_path / "harness.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    first = run_python_diagnostics(
        tmp_path,
        ["harness.py"],
        turn=2,
        source_tool="write_file",
    )
    second = run_python_diagnostics(
        tmp_path,
        ["harness.py"],
        turn=3,
        source_tool="patch_file",
    )

    assert len(first.new_diagnostics) == 1
    assert first.new_diagnostics[0]["kind"] == "syntax"
    assert "invalid syntax" in first.new_diagnostics[0]["message"]
    assert "[code_diagnostics]" in format_new_diagnostics_for_tool_result(first)
    assert second.new_diagnostics == []
    summary = summarize_code_diagnostics(tmp_path)
    assert summary["active_count"] == 1


def test_fixing_syntax_clears_completion_blocker(tmp_path: Path):
    (tmp_path / "harness.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    run_python_diagnostics(
        tmp_path,
        ["harness.py"],
        turn=1,
        source_tool="write_file",
    )

    assert completion_blocking_diagnostics(tmp_path)

    (tmp_path / "harness.py").write_text("def fixed():\n    return 1\n", encoding="utf-8")
    result = run_python_diagnostics(
        tmp_path,
        ["harness.py"],
        turn=2,
        source_tool="patch_file",
    )

    assert result.cleared_paths == ["harness.py"]
    assert completion_blocking_diagnostics(tmp_path) == []
    registry = json.loads((tmp_path / REGISTRY_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert registry["files"]["harness.py"]["active_diagnostics"] == []


def test_non_python_paths_are_ignored(tmp_path: Path):
    (tmp_path / "notes.md").write_text("def not python(:\n", encoding="utf-8")

    result = run_python_diagnostics(
        tmp_path,
        ["notes.md"],
        turn=1,
        source_tool="write_file",
    )

    assert result.checked_paths == []
    assert result.new_diagnostics == []
