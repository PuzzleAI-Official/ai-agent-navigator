"""Regression tests for Phase 2B gates G-A2, G-A3, G-A4.

Each gate ships with the 5-test discipline + a false-positive
regression test (per Codex C8):

  1. **Violation triggers** — gate fires on a clear violation.
  2. **Near-miss does NOT trigger** — gate stays silent on legitimate
     boundary cases (so future modalities aren't blocked).
  3. **Env-var bypass works** — when the flag is "0", the gate is
     silent and parsing proceeds normally.
  4. **Structured logging fires** — `gate_fired` log line emitted
     with the documented JSON shape.
  5. **No-raise** — Pydantic gates never raise an exception, even
     on a violation. They emit a log; the parse succeeds.
  6. **False-positive regression** — capability-predicate gates
     (G-A3) handle a hypothetical new modality cleanly when the
     predicate is updated.

Phase 2B's gate G-Aux2 (research_subagent 3-tier output) lives in
Phase 2C since it's tied to the auxiliary-prompt refactor.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Per-test gate-enable fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _enable_phase2_gates(monkeypatch):
    """Per-file autouse fixture enables all Phase 2B gates by default.
    Tests that exercise the env-var bypass set the flag to "0" themselves.
    """
    monkeypatch.setenv("PUZZLEEVAL_GATE_AGENT2_SCOPE_FLOOR", "1")
    monkeypatch.setenv("PUZZLEEVAL_GATE_TESTCASE_INSTRUCTIONS_ASYMMETRY", "1")
    monkeypatch.setenv("PUZZLEEVAL_GATE_CHECKLIST_VERIFIED_PASS", "1")
    import puzzleeval.config as cfg
    importlib.reload(cfg)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _make_candidate(name: str, covers: list[str]) -> Any:
    """Minimal Candidate stub for Agent2Result tests."""
    from puzzleeval.schemas import Candidate

    return Candidate(
        name=name,
        provider=name,
        description=f"{name} description",
        pricing_model="usage-based",
        claimed_capabilities=["foo"],
        relevance_score=0.7,
        adoption_difficulty="easy",
        covers_step_ids=covers,
        api_available=True,
        api_docs_url=f"https://{name.lower()}.example.com",
        relevant_subtasks=[],
        source="https://example.com",
    )


def _make_agent2_result(*candidates: Any) -> Any:
    from puzzleeval.schemas import Agent2Result

    return Agent2Result(
        candidates=list(candidates),
        search_approach="test",
        coverage_notes="test",
        cost_usd=0.0,
    )


def _make_test_case(*, input_type: str, instructions: str | None = None) -> Any:
    """Minimal TestCase for G-A3 instructions-asymmetry tests."""
    from puzzleeval.schemas import (
        JudgementCriterion,
        Persona,
        RubricCriterion,
        TestCase,
    )

    base: dict[str, Any] = dict(
        id="tc-test",
        sub_task_ref="x",
        scenario="test",
        input_type=input_type,
        input_data="{}",
        output_type="text",
        expected_output="x",
        judgement_criteria=[
            JudgementCriterion(
                criterion="c", eval_type="subjective_quality", weight=1.0,
            )
        ],
        difficulty="medium",
        tags=["happy_path"],
    )
    if instructions is not None:
        base["input_context"] = {"instructions": instructions}
    # Conversational test cases need persona+goal+rubric+evaluation_mode
    if input_type in ("voice_conversation", "voice_turn", "conversation", "chat"):
        base["persona"] = Persona(name="M", demographics="h", emotional_state="s")
        base["goal"] = "test"
        base["rubric"] = [
            RubricCriterion(name="goal_completion", description="g", weight=1.0)
        ]
        base["evaluation_mode"] = "agentic"
    return TestCase(**base)


# ---------------------------------------------------------------------------
# G-A2 — Agent2Result per-scope-floor
# ---------------------------------------------------------------------------

class TestGateA2_PerScopeFloor:
    def test_violation_triggers_warning(self, caplog):
        # 2 candidates both covering scope A, 1 covering scope B → B has <3.
        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_agent2_result(
                _make_candidate("X", ["A", "B"]),
                _make_candidate("Y", ["A"]),
                _make_candidate("Z", ["A"]),
            )
        records = [r for r in caplog.records if getattr(r, "operation", "") == "gate_fired"]
        assert any(
            getattr(r, "gate_name", "") == "agent2_scope_floor" for r in records
        ), "expected a gate_fired log with gate_name=agent2_scope_floor"

    def test_near_miss_does_not_trigger(self, caplog):
        # All scopes have ≥3 covering candidates → no warn.
        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_agent2_result(
                _make_candidate("X", ["A", "B"]),
                _make_candidate("Y", ["A", "B"]),
                _make_candidate("Z", ["A", "B"]),
            )
        records = [
            r for r in caplog.records
            if getattr(r, "operation", "") == "gate_fired"
            and getattr(r, "gate_name", "") == "agent2_scope_floor"
        ]
        assert records == []

    def test_env_bypass_silences(self, monkeypatch, caplog):
        monkeypatch.setenv("PUZZLEEVAL_GATE_AGENT2_SCOPE_FLOOR", "0")
        import puzzleeval.config as cfg
        importlib.reload(cfg)

        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_agent2_result(_make_candidate("X", ["A"]))
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "agent2_scope_floor"
        ]
        assert records == []

    def test_no_raise_on_violation(self):
        # Violation must NOT raise — the validator only logs.
        try:
            result = _make_agent2_result(_make_candidate("X", ["A"]))
        except Exception as exc:
            pytest.fail(f"G-A2 must not raise on violation, got {exc!r}")
        assert result is not None

    def test_structured_logging_payload(self, caplog):
        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_agent2_result(_make_candidate("X", ["A"]))
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "agent2_scope_floor"
        ]
        assert records, "expected gate_fired record"
        rec = records[0]
        assert getattr(rec, "severity", None) == "WARN"
        assert getattr(rec, "rejected", None) is False
        under = getattr(rec, "under_covered_scopes", None)
        assert under and isinstance(under, list)
        assert {"scope_id": "A", "candidate_count": 1} in under


# ---------------------------------------------------------------------------
# G-A3 — TestCase instructions-asymmetry (capability-predicate-driven)
# ---------------------------------------------------------------------------

class TestGateA3_InstructionsAsymmetry:
    def test_violation_non_conversational_with_instructions(self, caplog):
        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_test_case(
                input_type="document_content",
                instructions="You are an OCR agent",  # WRONG: OCR isn't an LLM agent
            )
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "testcase_instructions_asymmetry"
        ]
        assert records, "expected gate_fired for non-conv with instructions"
        assert getattr(records[0], "violation", None) == "instructions_populated_for_non_conversational"

    def test_violation_conversational_without_instructions(self, caplog):
        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_test_case(
                input_type="voice_conversation",
                instructions=None,  # WRONG: conversational MUST have instructions
            )
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "testcase_instructions_asymmetry"
        ]
        assert records, "expected gate_fired for conv without instructions"
        assert getattr(records[0], "violation", None) == "instructions_missing_for_conversational"

    def test_near_miss_conversational_with_instructions(self, caplog):
        # Conversational WITH instructions = correct, no warn.
        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_test_case(
                input_type="voice_conversation",
                instructions="You are Vera, a 24/7 plumbing dispatcher.",
            )
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "testcase_instructions_asymmetry"
        ]
        assert records == []

    def test_near_miss_non_conversational_without_instructions(self, caplog):
        # Non-conversational WITHOUT instructions = correct, no warn.
        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_test_case(
                input_type="document_content",
                instructions=None,
            )
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "testcase_instructions_asymmetry"
        ]
        assert records == []

    def test_env_bypass_silences(self, monkeypatch, caplog):
        monkeypatch.setenv(
            "PUZZLEEVAL_GATE_TESTCASE_INSTRUCTIONS_ASYMMETRY", "0"
        )
        import puzzleeval.config as cfg
        importlib.reload(cfg)

        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_test_case(input_type="document_content", instructions="oops")
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "testcase_instructions_asymmetry"
        ]
        assert records == []

    def test_no_raise_on_violation(self):
        try:
            tc = _make_test_case(
                input_type="document_content", instructions="oops"
            )
        except Exception as exc:
            pytest.fail(f"G-A3 must not raise on violation, got {exc!r}")
        assert tc is not None

    def test_structured_logging_payload(self, caplog):
        with caplog.at_level(logging.WARNING, logger="puzzleeval.schemas"):
            _make_test_case(
                input_type="document_content", instructions="oops"
            )
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "testcase_instructions_asymmetry"
        ]
        assert records
        rec = records[0]
        assert getattr(rec, "severity", None) == "WARN"
        assert getattr(rec, "rejected", None) is False
        assert getattr(rec, "input_type", None) == "document_content"

    def test_false_positive_predicate_extensibility(self):
        """Codex C8 — the capability predicate is the right extension
        point. A hypothetical future modality that needs instructions
        on a non-conversational TestCase would be added to the predicate
        (not the validator); both `supports_user_instructions(new_type)`
        returning True AND the gate not firing for that type are the
        contract.

        This test pins the predicate-as-extension-point shape: existing
        modalities classify correctly, and the predicate is queryable
        without import-cycle issues from the validator.
        """
        from puzzleeval.capability_predicates import supports_user_instructions

        # Existing modalities classify correctly.
        assert supports_user_instructions("voice_conversation") is True
        assert supports_user_instructions("voice_turn") is True
        assert supports_user_instructions("conversation") is True
        assert supports_user_instructions("chat") is True
        assert supports_user_instructions("document_content") is False
        assert supports_user_instructions("audio_content") is False
        assert supports_user_instructions("code") is False
        assert supports_user_instructions(None) is False
        assert supports_user_instructions("") is False


# ---------------------------------------------------------------------------
# G-A4 — BuildReadinessChecklist Verified-Pass-needs-non-negotiables
# ---------------------------------------------------------------------------

def _make_checklist(*, all_confirmed: bool = True) -> Any:
    """Minimal BuildReadinessChecklist stub.

    When `all_confirmed=True` returns a checklist with all 4 non-negotiables
    `confirmed`. When False, returns one with non-negotiables `unknown`.
    """
    from puzzleeval.schemas import (
        BuildReadinessChecklist,
        FieldStatus,
    )

    if all_confirmed:
        confirmed_field = FieldStatus(
            status="confirmed",
            value="POST /v1/foo",
            source_url="https://example.com/docs",
            reasoning=None,
        )
    else:
        confirmed_field = FieldStatus(
            status="unknown",
            value=None,
            source_url=None,
            reasoning="docs paywalled",
        )
    unknown = FieldStatus(
        status="unknown", value=None, source_url=None, reasoning="N/A"
    )
    return BuildReadinessChecklist(
        endpoint_path=confirmed_field,
        auth_method=confirmed_field,
        request_body_shape=confirmed_field,
        response_body_shape=confirmed_field,
        auth_refresh=unknown,
        error_response_schema=unknown,
        rate_limit_signal=unknown,
        async_pattern=unknown,
        content_type_quirks=unknown,
        sandbox_availability=unknown,
        populated_by="agent_4",
        last_updated_at="",
    )


class TestGateA4_VerifiedPassNonNegotiables:
    def test_violation_triggers(self, caplog):
        cl = _make_checklist(all_confirmed=False)
        from puzzleeval.validators import validate_checklist_for_verified_pass

        with caplog.at_level(logging.WARNING, logger="puzzleeval.validators"):
            unknowns = validate_checklist_for_verified_pass(
                cl, candidate_name="TestCand", trace_id="t1"
            )
        assert len(unknowns) == 4, f"expected all 4 non-negotiables flagged, got {unknowns}"
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "checklist_verified_pass_non_negotiables"
        ]
        assert records, "expected gate_fired log"

    def test_near_miss_all_confirmed(self, caplog):
        cl = _make_checklist(all_confirmed=True)
        from puzzleeval.validators import validate_checklist_for_verified_pass

        with caplog.at_level(logging.WARNING, logger="puzzleeval.validators"):
            unknowns = validate_checklist_for_verified_pass(cl)
        assert unknowns == []
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "checklist_verified_pass_non_negotiables"
        ]
        assert records == []

    def test_env_bypass_silences(self, monkeypatch, caplog):
        monkeypatch.setenv("PUZZLEEVAL_GATE_CHECKLIST_VERIFIED_PASS", "0")
        cl = _make_checklist(all_confirmed=False)
        from puzzleeval.validators import validate_checklist_for_verified_pass

        with caplog.at_level(logging.WARNING, logger="puzzleeval.validators"):
            unknowns = validate_checklist_for_verified_pass(cl)
        assert unknowns == []  # bypass returns empty
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "checklist_verified_pass_non_negotiables"
        ]
        assert records == []

    def test_no_raise_on_violation(self):
        cl = _make_checklist(all_confirmed=False)
        from puzzleeval.validators import validate_checklist_for_verified_pass

        try:
            validate_checklist_for_verified_pass(cl)
        except Exception as exc:
            pytest.fail(f"G-A4 must not raise on violation, got {exc!r}")

    def test_structured_logging_payload(self, caplog):
        cl = _make_checklist(all_confirmed=False)
        from puzzleeval.validators import validate_checklist_for_verified_pass

        with caplog.at_level(logging.WARNING, logger="puzzleeval.validators"):
            validate_checklist_for_verified_pass(
                cl, candidate_name="TestCand", trace_id="t1"
            )
        records = [
            r for r in caplog.records
            if getattr(r, "gate_name", "") == "checklist_verified_pass_non_negotiables"
        ]
        assert records
        rec = records[0]
        assert getattr(rec, "severity", None) == "WARN"
        assert getattr(rec, "rejected", None) is False
        unknowns = getattr(rec, "unknown_non_negotiables", None)
        assert isinstance(unknowns, list) and len(unknowns) == 4
        assert getattr(rec, "candidate_name", None) == "TestCand"


# ---------------------------------------------------------------------------
# Config flag round-trip
# ---------------------------------------------------------------------------

class TestPhase2GateConfig:
    def test_all_three_flags_default_on(self, monkeypatch):
        for flag in (
            "PUZZLEEVAL_GATE_AGENT2_SCOPE_FLOOR",
            "PUZZLEEVAL_GATE_TESTCASE_INSTRUCTIONS_ASYMMETRY",
            "PUZZLEEVAL_GATE_CHECKLIST_VERIFIED_PASS",
        ):
            monkeypatch.delenv(flag, raising=False)
        import puzzleeval.config as cfg
        importlib.reload(cfg)
        assert cfg.GATE_AGENT2_SCOPE_FLOOR_ENABLED is True
        assert cfg.GATE_TESTCASE_INSTRUCTIONS_ASYMMETRY_ENABLED is True
        assert cfg.GATE_CHECKLIST_VERIFIED_PASS_ENABLED is True

    def test_flags_can_be_disabled(self, monkeypatch):
        for flag in (
            "PUZZLEEVAL_GATE_AGENT2_SCOPE_FLOOR",
            "PUZZLEEVAL_GATE_TESTCASE_INSTRUCTIONS_ASYMMETRY",
            "PUZZLEEVAL_GATE_CHECKLIST_VERIFIED_PASS",
        ):
            monkeypatch.setenv(flag, "0")
        import puzzleeval.config as cfg
        importlib.reload(cfg)
        assert cfg.GATE_AGENT2_SCOPE_FLOOR_ENABLED is False
        assert cfg.GATE_TESTCASE_INSTRUCTIONS_ASYMMETRY_ENABLED is False
        assert cfg.GATE_CHECKLIST_VERIFIED_PASS_ENABLED is False
