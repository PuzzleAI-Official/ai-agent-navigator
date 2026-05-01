"""Tests for ``puzzleeval.agents.agent5.verification.verify_reflection_complete``.

The gate is the post-HARNESS_COMPLETE check (PR 2). It returns:
  * None when the reflection passes pattern check + (optional) judge.
  * A short error string when the reflection is missing or vacuous;
    the build_loop treats it like other gate retries.

These tests cover the gate function in isolation. End-to-end build_loop
behavior (directive injection on retry, accept-after-retry telemetry)
is covered by the existing build_loop_behavior tests + the autonomy
integration tests.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from puzzleeval.agents.agent5 import verification
from puzzleeval.agents.agent5.reflection_evidence_check import (
    ReflectionVerdict,
)


_LOGGER = logging.getLogger("test_reflection_gate")


# Pull in a canonical PASS reflection from the evidence-check tests
from tests.test_reflection_evidence_check import (  # noqa: E402
    _PASS_REFLECTION,
    _FAIL_REFLECTION,
    _BORDERLINE_REFLECTION,
)


def _make_candidate() -> MagicMock:
    cand = MagicMock()
    cand.name = "TestSvc"
    cand.provider = "TestCo"
    return cand


def _seed_state_dir(tmp_path: Path, *, reflection_text: str | None = None,
                    objective_text: str = "# Objective\n\n## SUCCESS CRITERIA\n- [ ] x"):
    state_dir = tmp_path / "_agent_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "objective.md").write_text(objective_text, encoding="utf-8")
    (tmp_path / "harness.py").write_text("\n".join(f"# line {i}" for i in range(1, 181)), encoding="utf-8")
    (tmp_path / "smoke_test.py").write_text("\n".join(f"# line {i}" for i in range(1, 101)), encoding="utf-8")
    (tmp_path / "live_test.py").write_text("\n".join(f"# line {i}" for i in range(1, 41)), encoding="utf-8")
    if reflection_text is not None:
        (state_dir / "reflection_phase_3.md").write_text(reflection_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Missing reflection — gate returns directive-prompt-shaped error
# ---------------------------------------------------------------------------


class TestVerifyReflectionMissing:

    def test_missing_file_returns_directive_message(self, tmp_path: Path):
        _seed_state_dir(tmp_path, reflection_text=None)
        result = verification.verify_reflection_complete(
            tmp_path, _make_candidate(),
            client=None,
            llm_judge_enabled=False,
            logger=_LOGGER,
            trace_id="t",
        )
        assert result is not None
        assert "missing" in result.lower()
        # The error message must teach the agent to cite evidence.
        assert "file references" in result.lower() or "harness.py:" in result.lower()

    def test_missing_state_dir_treated_as_missing(self, tmp_path: Path):
        # No _agent_state/ at all — gate returns missing-message.
        result = verification.verify_reflection_complete(
            tmp_path, _make_candidate(),
            client=None, llm_judge_enabled=False,
            logger=_LOGGER, trace_id="t",
        )
        assert result is not None
        assert "missing" in result.lower()


# ---------------------------------------------------------------------------
# Pattern verdicts — pass / fail without invoking the judge
# ---------------------------------------------------------------------------


class TestVerifyReflectionPatternVerdicts:

    def test_pass_reflection_returns_none(self, tmp_path: Path):
        _seed_state_dir(tmp_path, reflection_text=_PASS_REFLECTION)
        result = verification.verify_reflection_complete(
            tmp_path, _make_candidate(),
            client=None, llm_judge_enabled=False,
            logger=_LOGGER, trace_id="t",
        )
        assert result is None, result

    def test_fail_reflection_returns_evidence_message(self, tmp_path: Path):
        _seed_state_dir(tmp_path, reflection_text=_FAIL_REFLECTION)
        result = verification.verify_reflection_complete(
            tmp_path, _make_candidate(),
            client=None, llm_judge_enabled=False,
            logger=_LOGGER, trace_id="t",
        )
        assert result is not None
        # The error explains WHY (insufficient file refs, missing sections).
        assert "evidence" in result.lower() or "self-attestation" in result.lower()


# ---------------------------------------------------------------------------
# Borderline + LLM-judge fallback
# ---------------------------------------------------------------------------


class TestVerifyReflectionBorderlineWithJudge:

    def _make_judge_client(self, verdict: str, reason: str = "x") -> MagicMock:
        import json
        client = MagicMock()
        response = MagicMock()
        text = MagicMock()
        text.type = "text"
        text.text = json.dumps({"verdict": verdict, "reason": reason})
        response.content = [text]
        client.messages.create.return_value = response
        return client

    def test_borderline_with_judge_pass_returns_none(self, tmp_path: Path):
        _seed_state_dir(tmp_path, reflection_text=_BORDERLINE_REFLECTION)
        client = self._make_judge_client("pass", "ok")
        result = verification.verify_reflection_complete(
            tmp_path, _make_candidate(),
            client=client, llm_judge_enabled=True,
            logger=_LOGGER, trace_id="t",
        )
        assert result is None, result

    def test_borderline_with_judge_fail_returns_error(self, tmp_path: Path):
        _seed_state_dir(tmp_path, reflection_text=_BORDERLINE_REFLECTION)
        client = self._make_judge_client("fail", "claims unsupported")
        result = verification.verify_reflection_complete(
            tmp_path, _make_candidate(),
            client=client, llm_judge_enabled=True,
            logger=_LOGGER, trace_id="t",
        )
        assert result is not None
        assert "evidence" in result.lower()

    def test_borderline_judge_disabled_accepts(self, tmp_path: Path):
        # Judge disabled → pattern check borderline accepts (defensive PASS).
        _seed_state_dir(tmp_path, reflection_text=_BORDERLINE_REFLECTION)
        result = verification.verify_reflection_complete(
            tmp_path, _make_candidate(),
            client=None, llm_judge_enabled=False,
            logger=_LOGGER, trace_id="t",
        )
        assert result is None
