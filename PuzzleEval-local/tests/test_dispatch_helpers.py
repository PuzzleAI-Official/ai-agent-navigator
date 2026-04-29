"""Helper-level tests for puzzleeval.agents.agent5.dispatch_helpers (Phase 4 Path B Step 2).

These tests pin the contracts of the 7 dispatch-loop pure helpers in
isolation. End-to-end coverage lives in ``test_build_loop_behavior.py``;
this layer catches regressions per-predicate at <1ms each.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from puzzleeval.agents.agent5.dispatch_helpers import (
    ERROR_SIGNATURES,
    PHASE_1_VIOLATION_FILES,
    TRANSITION_FILES_WRITE,
    build_reassessment_message,
    classify_tool_result_error,
    detect_harness_signal,
    detect_phase_transition,
    detect_smoke_pass,
    detect_tool_result_error,
    is_phase_1_code_violation,
    should_inject_reassessment,
)


def _block(btype: str, name: str = "", **input_kwargs) -> SimpleNamespace:
    return SimpleNamespace(type=btype, name=name, input=input_kwargs)


# ---------------------------------------------------------------------------
# detect_smoke_pass
# ---------------------------------------------------------------------------


class TestDetectSmokePass:
    def test_marker_present(self):
        assert detect_smoke_pass("output\nSMOKE TEST PASSED\nmore")

    def test_marker_absent(self):
        assert not detect_smoke_pass("nothing happened")

    def test_case_sensitive_marker(self):
        # Production marker is uppercase — lowercase variant must NOT match.
        # If this changes, downstream telemetry breaks.
        assert not detect_smoke_pass("smoke test passed")

    def test_empty_string(self):
        assert not detect_smoke_pass("")


# ---------------------------------------------------------------------------
# detect_harness_signal
# ---------------------------------------------------------------------------


class TestDetectHarnessSignal:
    def test_complete(self):
        assert detect_harness_signal("done\nHARNESS_COMPLETE") == "complete"

    def test_failed(self):
        assert detect_harness_signal("can't\nHARNESS_FAILED") == "failed"

    def test_neither(self):
        assert detect_harness_signal("just text") is None

    def test_complete_wins_when_both_present(self):
        # Order-of-priority rule. The agent shouldn't emit both, but
        # COMPLETE wins as a defensive default (caller proceeds to
        # verification gate, which is the right path).
        text = "HARNESS_COMPLETE somehow also HARNESS_FAILED"
        assert detect_harness_signal(text) == "complete"

    def test_substring_in_text_does_not_misfire(self):
        # Defensive: the marker is rare enough that substring-anywhere
        # is the right semantics. Confirm it isn't matching too loosely.
        assert detect_harness_signal("the harness is incomplete") is None
        assert detect_harness_signal("the harness has not failed") is None


# ---------------------------------------------------------------------------
# detect_phase_transition
# ---------------------------------------------------------------------------


class TestDetectPhaseTransition:
    def test_write_api_spec_triggers(self):
        block = _block("tool_use", "write_file", filename="api_spec.txt")
        triggered, label = detect_phase_transition(block, api_spec_written=False)
        assert triggered
        assert label == "write_file:api_spec.txt"

    def test_patch_api_spec_triggers(self):
        block = _block("tool_use", "patch_file", filename="api_spec.txt")
        triggered, label = detect_phase_transition(block, api_spec_written=False)
        assert triggered
        assert label == "patch_file:api_spec.txt"

    def test_write_harness_triggers(self):
        block = _block("tool_use", "write_file", filename="harness.py")
        triggered, label = detect_phase_transition(block, api_spec_written=False)
        assert triggered
        assert label == "write_file:harness.py"

    def test_write_requirements_triggers(self):
        block = _block("tool_use", "write_file", filename="requirements.txt")
        triggered, label = detect_phase_transition(block, api_spec_written=False)
        assert triggered
        assert label == "write_file:requirements.txt"

    def test_unrelated_write_does_not_trigger(self):
        block = _block("tool_use", "write_file", filename="foo.py")
        triggered, _ = detect_phase_transition(block, api_spec_written=False)
        assert not triggered

    def test_already_written_no_op(self):
        # If api_spec_written is True, even an api_spec.txt write is
        # a no-op for transition purposes.
        block = _block("tool_use", "write_file", filename="api_spec.txt")
        triggered, _ = detect_phase_transition(block, api_spec_written=True)
        assert not triggered

    def test_non_tool_use_block_no_op(self):
        block = _block("text")
        triggered, _ = detect_phase_transition(block, api_spec_written=False)
        assert not triggered

    def test_patch_other_file_does_not_trigger(self):
        # patch_file ONLY triggers for api_spec.txt
        block = _block("tool_use", "patch_file", filename="harness.py")
        triggered, _ = detect_phase_transition(block, api_spec_written=False)
        assert not triggered

    def test_block_without_input_no_op(self):
        block = SimpleNamespace(type="tool_use", name="write_file", input={})
        triggered, _ = detect_phase_transition(block, api_spec_written=False)
        assert not triggered


class TestIsPhase1CodeViolation:
    def test_harness_py_is_violation(self):
        assert is_phase_1_code_violation("write_file:harness.py")

    def test_requirements_txt_is_violation(self):
        assert is_phase_1_code_violation("write_file:requirements.txt")

    def test_api_spec_is_not_violation(self):
        assert not is_phase_1_code_violation("write_file:api_spec.txt")

    def test_patch_file_is_never_violation(self):
        # patch_file('api_spec.txt') is the canonical augment path
        assert not is_phase_1_code_violation("patch_file:api_spec.txt")

    def test_empty_label_is_not_violation(self):
        assert not is_phase_1_code_violation("")


# ---------------------------------------------------------------------------
# detect_tool_result_error
# ---------------------------------------------------------------------------


class TestDetectToolResultError:
    @pytest.mark.parametrize("text", [
        "Traceback (most recent call last):",
        "AssertionError: foo",
        "ModuleNotFoundError: no module named 'requests'",
        "401 unauthorized",
        "403 forbidden",
        "ConnectionRefusedError: nothing on port",
        "exit code: 1",
        "wrong x-auth-key",
        "API key is invalid",
        "permission denied",
    ])
    def test_known_error_signatures_detected(self, text):
        assert detect_tool_result_error(text)

    @pytest.mark.parametrize("text", [
        "",
        "no errors here",
        # Bare 404 must NOT trigger — it false-positives when fetched_docs
        # mentions HTTP error codes (e.g., "returns 404 for invalid keys").
        "the API returns 404 for invalid keys",
        # Bare "error" must NOT trigger.
        "error handling configured successfully",
        # Bare "fail" must NOT trigger.
        "graceful failure mode enabled",
    ])
    def test_non_errors_not_detected(self, text):
        assert not detect_tool_result_error(text)

    def test_case_insensitive(self):
        # Production matching is case-insensitive against ERROR_SIGNATURES.
        assert detect_tool_result_error("TRACEBACK (MOST RECENT call last)")

    def test_error_signatures_count_locked(self):
        # ERROR_SIGNATURES is a TUPLE for immutability. If the count
        # changes (signatures added or removed), this test signals the
        # need to re-audit the false-positive surface.
        assert len(ERROR_SIGNATURES) == 16


# ---------------------------------------------------------------------------
# classify_tool_result_error
# ---------------------------------------------------------------------------


class TestClassifyToolResultError:
    @pytest.mark.parametrize("text", [
        "401 unauthorized",
        "403 forbidden",
        "wrong x-auth-key",
        "Authentication failed",
        "not authorized",
    ])
    def test_auth_category(self, text):
        # NB: "permission denied" is in ERROR_SIGNATURES (triggers
        # has_error) but is NOT in the auth-category patterns —
        # production classifies it as "other". Preserved as-is during
        # the extraction; if we want better coverage, change in a
        # separate behavior-change PR with new tests.
        assert classify_tool_result_error(text) == "auth"

    @pytest.mark.parametrize("text", [
        "404 not found",
        "ConnectionRefusedError",
        "the endpoint is not found",
    ])
    def test_endpoint_category(self, text):
        assert classify_tool_result_error(text) == "endpoint"

    @pytest.mark.parametrize("text", [
        "400 bad request: missing field",
        "Invalid input: must be string",
        "unsupported audio_format",
    ])
    def test_format_category(self, text):
        assert classify_tool_result_error(text) == "format"

    def test_other_category(self):
        # A traceback for a generic RuntimeError → falls through to "other"
        assert classify_tool_result_error(
            "Traceback: RuntimeError: something unexpected"
        ) == "other"

    def test_priority_auth_over_endpoint(self):
        # If both 401 (auth) AND 404 (endpoint) appear, auth wins.
        # Auth is upstream of endpoint — fix auth first.
        assert classify_tool_result_error(
            "401 unauthorized 404 not found"
        ) == "auth"

    def test_empty_string_is_other(self):
        assert classify_tool_result_error("") == "other"


# ---------------------------------------------------------------------------
# should_inject_reassessment
# ---------------------------------------------------------------------------


class TestShouldInjectReassessment:
    def test_at_threshold_triggers(self):
        assert should_inject_reassessment(consecutive_errors=3, max_consecutive=3)

    def test_above_threshold_triggers(self):
        assert should_inject_reassessment(consecutive_errors=5, max_consecutive=3)

    def test_below_threshold_does_not_trigger(self):
        assert not should_inject_reassessment(
            consecutive_errors=2, max_consecutive=3,
        )

    def test_zero_errors_no_op(self):
        assert not should_inject_reassessment(0, 3)


# ---------------------------------------------------------------------------
# build_reassessment_message
# ---------------------------------------------------------------------------


class TestBuildReassessmentMessage:
    def test_tier_1_includes_root_cause_requirement(self):
        msg = build_reassessment_message(
            consecutive_errors=3,
            last_errors="ConnectionError on POST /v1/foo",
            error_history=[(0, "auth")],
            total_reassessments=1,
            approaches_tried=[],
        )
        assert "Tier 1" in msg
        assert "root-cause it" in msg
        assert "<root_cause_analysis>" in msg
        assert "ConnectionError" in msg

    def test_tier_2_questions_assumptions(self):
        msg = build_reassessment_message(
            consecutive_errors=3,
            last_errors="still 404",
            error_history=[(0, "endpoint"), (1, "endpoint")],
            total_reassessments=2,
            approaches_tried=["tier_1_endpoint"],
        )
        assert "Tier 2" in msg
        assert "question your assumptions" in msg
        assert "ask_research" in msg

    def test_tier_3_structured_pivot(self):
        msg = build_reassessment_message(
            consecutive_errors=3,
            last_errors="repeated failure",
            error_history=[],
            total_reassessments=3,
            approaches_tried=["tier_1_x", "tier_2_x"],
        )
        assert "Tier 3" in msg
        assert "STRUCTURED PIVOT" in msg

    def test_pattern_hint_when_3_same_category(self):
        msg = build_reassessment_message(
            consecutive_errors=3,
            last_errors="auth fail",
            error_history=[(0, "auth"), (1, "auth"), (2, "auth")],
            total_reassessments=1,
            approaches_tried=[],
        )
        assert "PATTERN DETECTED" in msg
        assert "authentication" in msg or "auth header" in msg

    def test_no_pattern_hint_when_categories_mixed(self):
        msg = build_reassessment_message(
            consecutive_errors=3,
            last_errors="x",
            error_history=[(0, "auth"), (1, "format"), (2, "endpoint")],
            total_reassessments=1,
            approaches_tried=[],
        )
        assert "PATTERN DETECTED" not in msg

    def test_no_pattern_hint_when_history_too_short(self):
        msg = build_reassessment_message(
            consecutive_errors=3,
            last_errors="x",
            error_history=[(0, "auth"), (1, "auth")],  # Only 2 entries
            total_reassessments=1,
            approaches_tried=[],
        )
        assert "PATTERN DETECTED" not in msg

    def test_approaches_summary_includes_recent_5(self):
        msg = build_reassessment_message(
            consecutive_errors=3,
            last_errors="x",
            error_history=[],
            total_reassessments=1,
            approaches_tried=[
                "tier_1_a", "tier_2_b", "tier_3_c",
                "tier_4_d", "tier_5_e", "tier_6_f",
            ],
        )
        # Last 5 of 6 should appear; the oldest (tier_1_a) should NOT.
        assert "tier_1_a" not in msg
        for marker in ("tier_2_b", "tier_3_c", "tier_4_d", "tier_5_e", "tier_6_f"):
            assert marker in msg

    def test_endpoint_pattern_label(self):
        msg = build_reassessment_message(
            consecutive_errors=3, last_errors="x",
            error_history=[(0, "endpoint")] * 3,
            total_reassessments=1, approaches_tried=[],
        )
        assert "endpoint URL" in msg

    def test_format_pattern_label(self):
        msg = build_reassessment_message(
            consecutive_errors=3, last_errors="x",
            error_history=[(0, "format")] * 3,
            total_reassessments=1, approaches_tried=[],
        )
        assert "request format" in msg or "format/body" in msg


class TestEnrichResearchQuestion:
    """The enrichment shapes the research sub-agent's input. Pin the
    composition rules so the helper extraction can't silently drop
    fields the sub-agent depends on."""

    def _spec_path_with(self, tmp_path, content):
        p = tmp_path / "api_spec.txt"
        p.write_text(content, encoding="utf-8")
        return p

    def test_basic_question_has_service_block(self, tmp_path):
        from puzzleeval.agents.agent5.dispatch_helpers import (
            enrich_research_question,
        )
        spec = tmp_path / "missing.txt"  # doesn't exist
        out = enrich_research_question(
            question="What auth?",
            candidate_name="Mindee",
            candidate_provider="Mindee SAS",
            candidate_docs_url="https://docs.mindee.com/",
            spec_path=spec,
            prior_results_text="",
            harness_code=None,
        )
        assert "Mindee" in out
        assert "Mindee SAS" in out
        assert "https://docs.mindee.com/" in out
        assert "QUESTION: What auth?" in out

    def test_spec_present_includes_known_block(self, tmp_path):
        from puzzleeval.agents.agent5.dispatch_helpers import (
            enrich_research_question,
        )
        spec = self._spec_path_with(
            tmp_path, "ENDPOINTS:\n  POST /v1/predict\n",
        )
        out = enrich_research_question(
            question="What auth?",
            candidate_name="Mindee", candidate_provider="X",
            candidate_docs_url="x", spec_path=spec,
            prior_results_text="", harness_code=None,
        )
        assert "WHAT WE ALREADY KNOW" in out
        assert "POST /v1/predict" in out
        assert "DO NOT re-research" in out

    def test_spec_doc_map_section_included_when_beyond_2k(self, tmp_path):
        from puzzleeval.agents.agent5.dispatch_helpers import (
            enrich_research_question,
        )
        # Spec where DOC_MAP appears AFTER the 2K boundary so the
        # second-pass section grab is what surfaces it.
        body = "ENDPOINTS:\n  POST /v1/x\n" + "x" * 2200 + "\nDOC_MAP:\n  https://docs.example.com/auth\n"
        spec = self._spec_path_with(tmp_path, body)
        out = enrich_research_question(
            question="x", candidate_name="X", candidate_provider="X",
            candidate_docs_url="x", spec_path=spec,
            prior_results_text="", harness_code=None,
        )
        assert "DOC_MAP:" in out
        assert "https://docs.example.com/auth" in out

    def test_prior_results_included_when_present(self, tmp_path):
        from puzzleeval.agents.agent5.dispatch_helpers import (
            enrich_research_question,
        )
        out = enrich_research_question(
            question="q", candidate_name="X", candidate_provider="X",
            candidate_docs_url="x", spec_path=tmp_path / "missing.txt",
            prior_results_text="Traceback: ConnectionError on POST",
            harness_code=None,
        )
        assert "LAST ERROR CONTEXT" in out
        assert "ConnectionError" in out

    def test_prior_results_omitted_when_empty(self, tmp_path):
        from puzzleeval.agents.agent5.dispatch_helpers import (
            enrich_research_question,
        )
        out = enrich_research_question(
            question="q", candidate_name="X", candidate_provider="X",
            candidate_docs_url="x", spec_path=tmp_path / "missing.txt",
            prior_results_text="",
            harness_code=None,
        )
        assert "LAST ERROR CONTEXT" not in out

    def test_harness_code_included_with_first_40_lines(self, tmp_path):
        from puzzleeval.agents.agent5.dispatch_helpers import (
            enrich_research_question,
        )
        code = "\n".join(f"line {i}" for i in range(60))
        out = enrich_research_question(
            question="q", candidate_name="X", candidate_provider="X",
            candidate_docs_url="x", spec_path=tmp_path / "missing.txt",
            prior_results_text="", harness_code=code,
        )
        assert "CURRENT HARNESS CODE" in out
        assert "line 0" in out
        assert "line 39" in out
        # First 40 lines: line 40 must NOT appear
        assert "line 40" not in out

    def test_harness_code_omitted_when_none(self, tmp_path):
        from puzzleeval.agents.agent5.dispatch_helpers import (
            enrich_research_question,
        )
        out = enrich_research_question(
            question="q", candidate_name="X", candidate_provider="X",
            candidate_docs_url="x", spec_path=tmp_path / "missing.txt",
            prior_results_text="", harness_code=None,
        )
        assert "CURRENT HARNESS CODE" not in out


class TestPhase1ViolationConstants:
    """Constants need explicit guard tests so accidental edits surface."""

    def test_transition_files_includes_three(self):
        assert TRANSITION_FILES_WRITE == frozenset(
            {"api_spec.txt", "harness.py", "requirements.txt"}
        )

    def test_phase_1_violations_excludes_api_spec(self):
        # Writing api_spec.txt is the CORRECT Phase 1 behavior — never
        # a violation. Only code files (harness.py, requirements.txt)
        # are violations when written by Sonnet during Phase 1.
        assert "api_spec.txt" not in PHASE_1_VIOLATION_FILES
        assert "harness.py" in PHASE_1_VIOLATION_FILES
        assert "requirements.txt" in PHASE_1_VIOLATION_FILES
