"""Source-level guard: Agent 1 wires adaptive thinking + effort.

Like the existing tests/test_thinking_and_tools.py guards for Agents 2/4/5,
this test grep's the source to ensure Agent 1's API call carries
``thinking={"type": "adaptive"}`` and respects ``output_config_for_request()``.
Belt-and-suspenders against a future refactor stripping these out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

AGENT1_PATH = (
    Path(__file__).resolve().parents[1]
    / "puzzleeval" / "agents" / "agent1" / "core.py"
)


@pytest.fixture(scope="module")
def source() -> str:
    return AGENT1_PATH.read_text(encoding="utf-8")


def test_imports_output_config(source):
    assert "output_config_for_request" in source


def test_passes_adaptive_thinking(source):
    # Adaptive thinking dict construction. The dict is built into a local
    # `_extra` mapping then unpacked into the API call so output_config can
    # be conditionally present. We grep for the literal {"type": "adaptive"}.
    assert (
        '"thinking": {"type": "adaptive"}' in source
        or "'thinking': {'type': 'adaptive'}" in source
    ), "Agent 1 should pass thinking={'type':'adaptive'}"


def test_calls_output_config_helper(source):
    assert "output_config_for_request()" in source


def test_request_unpacks_extra_kwargs(source):
    # The request should accept output_config + thinking via **_extra so
    # the helper's None return value produces a no-op (no field sent).
    assert "_extra" in source


def test_default_model_is_opus_in_config():
    """Agent 1's default model is Opus 4.7 (set in config.py)."""
    config_path = (
        Path(__file__).resolve().parents[1] / "puzzleeval" / "config.py"
    )
    text = config_path.read_text(encoding="utf-8")
    assert 'AGENT1_MODEL = os.environ.get("PUZZLEEVAL_AGENT1_MODEL", "claude-opus-4-7")' in text


def test_effort_default_is_medium():
    """PUZZLEEVAL_EFFORT defaults to 'medium' (lowered from 'high' in
    PLAN_VOICE_RUN_OPTIMIZATIONS.md item 4 — high was burning thinking
    budget on routine tool execution turns; medium is a floor that
    auto-tunes UPWARD on complex decisions). Users can bump back to
    high/xhigh/max via PUZZLEEVAL_EFFORT env override."""
    config_path = (
        Path(__file__).resolve().parents[1] / "puzzleeval" / "config.py"
    )
    text = config_path.read_text(encoding="utf-8")
    # The default literal:
    assert 'EFFORT = os.environ.get("PUZZLEEVAL_EFFORT", "medium")' in text


def test_xhigh_is_in_valid_effort_set():
    """Verifies the xhigh tier is wired in — user asked specifically for it."""
    config_path = (
        Path(__file__).resolve().parents[1] / "puzzleeval" / "config.py"
    )
    text = config_path.read_text(encoding="utf-8")
    assert '"xhigh"' in text
    assert '"max"' in text


def test_agent1_uses_parse_with_fallback(source):
    """Agent 1 must route the structured-output call through
    parse_with_fallback so a grammar-budget rejection on Agent1Result
    falls back to the non-strict tool path instead of failing the run."""
    assert "parse_with_fallback(" in source
    assert "from puzzleeval.structured_output import parse_with_fallback" in source


def test_structured_output_module_exists():
    """The shared parse_with_fallback module exists with the required
    public surface."""
    so_path = (
        Path(__file__).resolve().parents[1]
        / "puzzleeval" / "structured_output.py"
    )
    text = so_path.read_text(encoding="utf-8")
    assert "def parse_with_fallback(" in text
    assert "compiled grammar" in text
    assert "Grammar compilation timed out" in text
    # The fallback drops thinking + output_config (incompatible with
    # tool_choice forcing a tool). Guard so a future refactor doesn't
    # accidentally re-add them and break the fallback path.
    assert '"output_config", "thinking"' in text or "'output_config', 'thinking'" in text
    # Defensive unwrap guard against model over-nesting.
    assert "_unwrap_overnested_input" in text or "unwrap_overnested" in text or "schema_required" in text
