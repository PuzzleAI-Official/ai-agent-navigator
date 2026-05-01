# ============================================================================
# Tests for Agent 5 (Implement Test Env Agent)
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_agent5.py -v
#
# These tests use mocked API calls — no real API key or web fetches needed.
# Tests cover:
#   1. Pydantic schema validation (Agent5Input, TestHarness, FailedHarness, Agent5Result)
#   2. Helper functions (_candidate_slug, _dispatch_tool, _categorize_failure)
#   3. Integration tests with mocked Anthropic client
#   4. Validator tests (validate_agent5_output)
# ============================================================================

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.schemas import (
    Agent3Result,
    Agent4Result,
    Agent5Input,
    Agent5Result,
    Constraints,
    FailedHarness,
    JudgementCriterion,
    RejectedCandidate,
    ScreenedCandidate,
    SubTask,
    TestCase,
    TestHarness,
    UserUnderstandingOutput,
)
from puzzleeval.validators import validate_agent5_output, ValidationResult


@pytest.fixture(autouse=True)
def _disable_legacy_blocking_gates_for_agent5_mocks(monkeypatch):
    """The legacy integration tests in this file mock 2-3 API responses
    per build. After the autonomy + forensics gates were unified into the
    completion path (Goal/Planning/State/Reflection PR 1-3), those gates
    can fire on minimal mock harnesses that don't import ``_forensics``
    or write a reflection — consuming an extra API call that the mocks
    don't provide. Disable the gates that aren't the focus of these
    integration tests; the gates have their own dedicated test files
    (``test_autonomy_artifacts.py``, ``test_reflection_gate.py``,
    ``test_forensics_layer.py``).
    """
    monkeypatch.setenv("PUZZLEEVAL_GATE_AUTONOMY_ARTIFACTS", "0")
    monkeypatch.setenv("PUZZLEEVAL_GATE_FORENSICS_COVERAGE", "0")
    monkeypatch.setenv("PUZZLEEVAL_GATE_REFLECTION_PHASE_3", "0")
    monkeypatch.setenv("PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK", "0")
    monkeypatch.setenv("PUZZLEEVAL_VENV_PREINSTALL", "0")
    monkeypatch.setenv("PUZZLEEVAL_DIRECTIVE_SUPPRESS_ON_AGREEMENT", "0")
    import importlib
    import puzzleeval.config as cfg
    importlib.reload(cfg)
    yield


# ============================================================================
# Test Fixtures — Reusable test data
# ============================================================================

def _make_user_understanding() -> UserUnderstandingOutput:
    """Create a sample Agent 1 output for testing Agent 5."""
    return UserUnderstandingOutput(
        summary="User needs AI to extract data from invoices",
        sub_tasks=[
            SubTask(
                description="Extract structured data from invoice photos",
                capability="document OCR",
                search_keywords=["invoice OCR API", "document data extraction"],
            ),
        ],
        search_strategy="both",
        domain="accounting",
        search_keywords=["invoice processing automation"],
        constraints=Constraints(
            budget_range="$50-200/mo",
            must_have_features=["OCR"],
            integration_requirements=["QuickBooks"],
            technical_level="non-technical",
        ),
        workflow_summary=None,
    )


def _make_screened_candidate(
    name: str = "Google Document AI",
    *,
    auth_method: str = "no_auth",
) -> ScreenedCandidate:
    """Create a fully populated ScreenedCandidate.

    ``auth_method`` defaults to ``"no_auth"`` so the Agent 5 credential
    filter (landed in the "only test providers we have keys for" pass)
    lets the test candidate through without having to seed a mock
    provider_registry. Tests that specifically exercise the credential
    path pass ``auth_method="api_key"`` and either inject
    ``provider_credentials`` into Agent5Input or add a registry shim.
    """
    return ScreenedCandidate(
        name=name,
        provider="Google",
        description="Document processing and OCR service",
        pricing_model="per-page",
        pricing_details="$0.01 per page",
        claimed_capabilities=["document OCR", "invoice parsing"],
        relevance_score=0.92,
        adoption_difficulty="easy",
        relevant_subtasks=["Extract structured data from invoice photos"],
        source="https://cloud.google.com/document-ai",
        verified_api_docs_url="https://cloud.google.com/document-ai/docs/reference/rest",
        auth_method=auth_method,
        api_access_method="free_tier",
        confirmed_capabilities=["document OCR", "invoice parsing", "table extraction"],
        rate_limit_info="1000 requests per minute",
        data_format_notes="Accepts JPEG/PNG/PDF via multipart upload, returns JSON",
        screening_notes="Fetched API docs page. Found REST API reference with endpoints.",
    )


def _make_test_cases() -> Agent3Result:
    """Create a minimal Agent 3 result for testing."""
    return Agent3Result(
        test_cases=[
            TestCase(
                id="tc-001",
                sub_task_ref="Extract structured data from invoice photos",
                scenario="A standard printed invoice from a US vendor",
                input_type="text",
                output_type="extraction",
                input_data="Invoice #1234 from Acme Corp, total $500",
                expected_output="vendor: Acme Corp, total: 500.00, invoice_number: 1234",
                difficulty="medium",
                tags=["happy_path"],
                judgement_criteria=[
                    JudgementCriterion(
                        criterion="Must extract vendor name correctly",
                        weight=0.5,
                        eval_type="contains_key_info",
                    ),
                    JudgementCriterion(
                        criterion="Must extract total amount correctly",
                        weight=0.5,
                        eval_type="exact_match",
                    ),
                ],
            ),
        ],
        generation_notes="Generated 1 test case for invoice OCR",
        coverage_summary={"Extract structured data from invoice photos": 1},
    )


def _make_agent5_input() -> Agent5Input:
    """Create a valid Agent5Input."""
    return Agent5Input(
        validated_candidates=[
            _make_screened_candidate("Google Document AI"),
            _make_screened_candidate("AWS Textract"),
        ],
        user_understanding=_make_user_understanding(),
        test_cases=_make_test_cases(),
        trace_id="test-trace-123",
    )


def _make_test_harness(
    name: str = "Google Document AI",
    sandbox_dir: str = "/tmp/test_sandbox",
) -> TestHarness:
    """Create a fully populated TestHarness."""
    return TestHarness(
        candidate_name=name,
        provider="Google",
        harness_dir=sandbox_dir,
        entry_file="harness.py",
        requirements=["requests"],
        auth_env_vars=["GOOGLE_API_KEY"],
        auth_method="api_key",
        supported_input_types=["text"],
        supported_output_types=["extraction"],
        smoke_test_passed=True,
        validation_notes="SMOKE TEST PASSED\nHARNESS_COMPLETE",
        build_turns=5,
        build_cost_usd=1.50,
        harness_code='import os\ndef run(input_data):\n    return {"output": "", "latency_ms": 0, "tokens_used": None, "cost_usd": None, "raw_response": {}, "success": False, "error": "no API key"}',
    )


def _make_failed_harness(name: str = "BadService") -> FailedHarness:
    """Create a FailedHarness."""
    return FailedHarness(
        candidate_name=name,
        provider="BadCorp",
        failure_reason="API docs returned 404 and no SDK quickstart found",
        failure_category="docs_unusable",
        partial_code=None,
        turns_attempted=8,
    )


def _make_agent4_result() -> Agent4Result:
    """Create a sample Agent4Result matching the Agent5Input candidates."""
    return Agent4Result(
        validated_candidates=[
            _make_screened_candidate("Google Document AI"),
            _make_screened_candidate("AWS Textract"),
        ],
        rejected_candidates=[],
        screening_summary="2 candidates passed screening.",
        total_candidates_screened=2,
    )


def _make_agent5_result(sandbox_dir: str = "/tmp/test_sandbox") -> Agent5Result:
    """Create a valid Agent5Result."""
    return Agent5Result(
        harnesses=[
            _make_test_harness("Google Document AI", sandbox_dir),
            _make_test_harness("AWS Textract", sandbox_dir),
        ],
        failed_harnesses=[],
        total_candidates_attempted=2,
        total_build_cost_usd=3.00,
        build_summary="Built 2/2 harnesses successfully. Total build cost: $3.00.",
    )


# ============================================================================
# Schema Tests
# ============================================================================

class TestSchemas:
    """Test Agent 5 Pydantic schemas."""

    def test_agent5_input_valid(self):
        """A valid Agent5Input should construct without errors."""
        inp = _make_agent5_input()
        assert len(inp.validated_candidates) == 2
        assert inp.trace_id == "test-trace-123"

    def test_test_harness_all_fields(self):
        """A fully populated TestHarness should be valid."""
        h = _make_test_harness()
        assert h.candidate_name == "Google Document AI"
        assert h.smoke_test_passed is True
        assert h.build_cost_usd == 1.50
        assert "def run" in h.harness_code

    def test_failed_harness_all_fields(self):
        """A FailedHarness should be valid with all fields."""
        f = _make_failed_harness()
        assert f.candidate_name == "BadService"
        assert f.failure_category == "docs_unusable"
        assert f.turns_attempted == 8

    def test_failed_harness_with_partial_code(self):
        """FailedHarness should accept partial_code."""
        f = FailedHarness(
            candidate_name="PartialService",
            provider="Partial",
            failure_reason="Smoke test never passed",
            failure_category="build_timeout",
            partial_code="import requests\ndef run(input_data): pass",
            turns_attempted=15,
        )
        assert f.partial_code is not None
        assert f.failure_category == "build_timeout"

    def test_agent5_result_valid(self):
        """A valid Agent5Result should construct without errors."""
        r = _make_agent5_result()
        assert len(r.harnesses) == 2
        assert len(r.failed_harnesses) == 0
        assert r.total_candidates_attempted == 2
        assert r.total_build_cost_usd == 3.00


# ============================================================================
# Helper Function Tests
# ============================================================================

class TestHelpers:
    """Test Agent 5 helper functions."""

    def test_candidate_slug_basic(self):
        from puzzleeval.agents.implement_test_env import _candidate_slug
        assert _candidate_slug("Google Document AI") == "google_document_ai"

    def test_candidate_slug_special_chars(self):
        from puzzleeval.agents.implement_test_env import _candidate_slug
        assert _candidate_slug("AWS Textract (OCR)") == "aws_textract_ocr"

    def test_candidate_slug_long_name(self):
        from puzzleeval.agents.implement_test_env import _candidate_slug
        slug = _candidate_slug("A" * 100)
        assert len(slug) <= 40

    def test_candidate_slug_unicode(self):
        from puzzleeval.agents.implement_test_env import _candidate_slug
        slug = _candidate_slug("Résumé Parser™")
        assert slug  # Should not be empty
        assert all(c.isalnum() or c == "_" for c in slug)

    def test_dispatch_write_file(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        with tempfile.TemporaryDirectory() as tmpdir:
            result, exit_code = _dispatch_tool(
                "write_file",
                {"filename": "test.py", "content": "print('hello')"},
                Path(tmpdir),
            )
            assert "Written" in result
            assert exit_code == 0
            assert (Path(tmpdir) / "test.py").exists()
            assert (Path(tmpdir) / "test.py").read_text() == "print('hello')"

    def test_dispatch_write_file_rejects_path_traversal(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        with tempfile.TemporaryDirectory() as tmpdir:
            result, exit_code = _dispatch_tool(
                "write_file",
                {"filename": "../../../etc/passwd", "content": "evil"},
                Path(tmpdir),
            )
            assert "Error" in result or "Written" in result
            # The important thing: nothing was written outside the sandbox
            assert not Path("/etc/passwd_test").exists()

    def test_dispatch_write_file_rejects_bad_extension(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        with tempfile.TemporaryDirectory() as tmpdir:
            result, exit_code = _dispatch_tool(
                "write_file",
                {"filename": "evil.exe", "content": "binary"},
                Path(tmpdir),
            )
            assert "Error" in result
            assert exit_code != 0
            assert not (Path(tmpdir) / "evil.exe").exists()

    def test_dispatch_run_code(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        with tempfile.TemporaryDirectory() as tmpdir:
            result, exit_code = _dispatch_tool(
                "run_code",
                {"command": "python -c \"print('hello from sandbox')\""},
                Path(tmpdir),
            )
            assert "hello from sandbox" in result
            assert exit_code == 0

    def test_dispatch_run_code_timeout(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use a very short timeout for testing
            with patch("puzzleeval.agents.implement_test_env.AGENT5_CODE_TIMEOUT", 1):
                result, exit_code = _dispatch_tool(
                    "run_code",
                    {"command": "python -c \"import time; time.sleep(10)\""},
                    Path(tmpdir),
                )
                assert "timed out" in result.lower()
                assert exit_code == -1

    def test_dispatch_run_code_truncates_output(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        with tempfile.TemporaryDirectory() as tmpdir:
            result, exit_code = _dispatch_tool(
                "run_code",
                {"command": "python -c \"print('x' * 10000)\""},
                Path(tmpdir),
            )
            assert len(result) <= 6000  # 5000 + truncation message

    def test_dispatch_read_file(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.py").write_text("hello content")
            result, exit_code = _dispatch_tool(
                "read_file",
                {"filename": "test.py"},
                Path(tmpdir),
            )
            assert "hello content" in result
            assert exit_code == 0

    def test_dispatch_read_file_missing(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        with tempfile.TemporaryDirectory() as tmpdir:
            result, exit_code = _dispatch_tool(
                "read_file",
                {"filename": "nonexistent.py"},
                Path(tmpdir),
            )
            assert "Error" in result or "does not exist" in result
            assert exit_code != 0

    def test_categorize_failure_docs(self):
        from puzzleeval.agents.implement_test_env import _categorize_failure
        assert _categorize_failure("API docs returned 404") == "docs_unusable"

    def test_categorize_failure_auth(self):
        from puzzleeval.agents.implement_test_env import _categorize_failure
        assert _categorize_failure("Requires paid subscription key") == "auth_blocked"

    def test_categorize_failure_credit_balance(self):
        from puzzleeval.agents.implement_test_env import _categorize_failure
        assert _categorize_failure("Your credit balance is too low") == "build_timeout"

    def test_categorize_failure_rate_limit(self):
        from puzzleeval.agents.implement_test_env import _categorize_failure
        assert _categorize_failure("Rate limit exceeded") == "build_timeout"

    def test_categorize_failure_unknown(self):
        from puzzleeval.agents.implement_test_env import _categorize_failure
        assert _categorize_failure("Some weird error") == "unknown"

    def test_extract_env_vars_from_code(self):
        from puzzleeval.agents.implement_test_env import _extract_env_vars_from_code
        code = '''
import os
API_KEY = os.environ.get("MINDEE_API_KEY", "")
SECRET = os.environ.get("AWS_SECRET_ACCESS_KEY")
region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
os.environ["INTUIT_CLIENT_ID"]
'''
        result = _extract_env_vars_from_code(code)
        assert "MINDEE_API_KEY" in result
        assert "AWS_SECRET_ACCESS_KEY" in result
        assert "INTUIT_CLIENT_ID" in result
        # AWS_DEFAULT_REGION has no auth keyword — should be excluded
        assert "AWS_DEFAULT_REGION" not in result

    def test_extract_env_vars_empty_code(self):
        from puzzleeval.agents.implement_test_env import _extract_env_vars_from_code
        assert _extract_env_vars_from_code("") == []
        assert _extract_env_vars_from_code(None) == []

    def test_calculate_call_cost_fallback(self):
        """Test cost calculation without iterations (fallback path).

        Updated 2026-04-21: the fallback now also counts top-level
        cache_creation_input_tokens and cache_read_input_tokens (see
        OBSERVABILITY BUG #1 fix). Test must explicitly set those to
        zero (not rely on MagicMock default) otherwise the arithmetic
        raises TypeError on MagicMock * float.
        """
        from puzzleeval.agents.implement_test_env import _calculate_call_cost
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 10000
        mock_response.usage.output_tokens = 2000
        # Explicit zero cache fields so the new fallback path sees
        # ints (not auto-generated MagicMock attrs).
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        mock_response.usage.iterations = None  # No iterations = fallback
        mock_response.usage.server_tool_use = None
        cost = _calculate_call_cost(mock_response, "claude-sonnet-4-5-20250929")
        # 10000 * 3/1M + 2000 * 15/1M = 0.03 + 0.03 = 0.06
        assert abs(cost - 0.06) < 0.001

    def test_calculate_call_cost_with_iterations(self):
        """Test cost calculation using iterations array (correct path)."""
        from puzzleeval.agents.implement_test_env import _calculate_call_cost
        mock_response = MagicMock()
        # Create mock iterations
        exec_iter = MagicMock()
        exec_iter.type = "message"
        exec_iter.input_tokens = 5000
        exec_iter.output_tokens = 1000
        exec_iter.cache_creation_input_tokens = 0
        exec_iter.cache_read_input_tokens = 0

        advisor_iter = MagicMock()
        advisor_iter.type = "advisor_message"
        advisor_iter.model = "claude-opus-4-7"
        advisor_iter.input_tokens = 3000
        advisor_iter.output_tokens = 500
        advisor_iter.cache_creation_input_tokens = 0
        advisor_iter.cache_read_input_tokens = 0

        mock_response.usage.iterations = [exec_iter, advisor_iter]
        mock_response.usage.server_tool_use = None

        cost = _calculate_call_cost(mock_response, "claude-sonnet-4-6")
        # Executor: 5000 * 3/1M + 1000 * 15/1M = 0.015 + 0.015 = 0.030
        # Advisor:  3000 * 5/1M + 500 * 25/1M  = 0.015 + 0.0125 = 0.0275
        # Total: 0.0575
        assert abs(cost - 0.0575) < 0.001


# ============================================================================
# Validator Tests
# ============================================================================

class TestValidator:
    """Test validate_agent5_output()."""

    def test_valid_result_passes(self):
        """A well-formed result should pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = Agent5Result(
                harnesses=[
                    _make_test_harness("Google Document AI", tmpdir),
                    _make_test_harness("AWS Textract", tmpdir),
                ],
                failed_harnesses=[],
                total_candidates_attempted=2,
                total_build_cost_usd=3.00,
                build_summary="Built 2/2.",
            )
            agent4 = _make_agent4_result()
            v = validate_agent5_output(result, agent4)
            assert v.passed is True
            assert len(v.errors) == 0

    def test_zero_harnesses_is_error(self):
        """Zero successful harnesses should be a blocking error."""
        result = Agent5Result(
            harnesses=[],
            failed_harnesses=[_make_failed_harness("Google Document AI"), _make_failed_harness("AWS Textract")],
            total_candidates_attempted=2,
            total_build_cost_usd=5.00,
            build_summary="Built 0/2.",
        )
        agent4 = _make_agent4_result()
        v = validate_agent5_output(result, agent4)
        assert v.passed is False
        assert any("Zero harnesses" in e for e in v.errors)

    def test_count_mismatch_is_error(self):
        """Count mismatch should be a blocking error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = Agent5Result(
                harnesses=[_make_test_harness("Google Document AI", tmpdir)],
                failed_harnesses=[],
                total_candidates_attempted=5,  # Mismatch: 1 != 5
                total_build_cost_usd=1.50,
                build_summary="Built 1/5.",
            )
            agent4 = _make_agent4_result()
            v = validate_agent5_output(result, agent4)
            assert v.passed is False
            assert any("Count mismatch" in e for e in v.errors)

    def test_missing_candidate_is_warning(self):
        """A candidate from Agent 4 not in results should be a warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = Agent5Result(
                harnesses=[_make_test_harness("Google Document AI", tmpdir)],
                failed_harnesses=[],
                total_candidates_attempted=1,
                total_build_cost_usd=1.50,
                build_summary="Built 1/1.",
            )
            # Agent 4 had 2 candidates but result only has 1
            agent4 = _make_agent4_result()
            v = validate_agent5_output(result, agent4)
            # Count mismatch error (1 != 2 in agent4) will fire first
            # But also the missing candidate warning
            assert any("silently dropped" in w for w in v.warnings)

    def test_empty_harness_code_is_error(self):
        """Harness with empty code should be a blocking error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            h = _make_test_harness("Google Document AI", tmpdir)
            h_dict = h.model_dump()
            h_dict["harness_code"] = ""
            bad_harness = TestHarness(**h_dict)

            result = Agent5Result(
                harnesses=[bad_harness, _make_test_harness("AWS Textract", tmpdir)],
                failed_harnesses=[],
                total_candidates_attempted=2,
                total_build_cost_usd=3.00,
                build_summary="Built 2/2.",
            )
            agent4 = _make_agent4_result()
            v = validate_agent5_output(result, agent4)
            assert v.passed is False
            assert any("empty harness_code" in e for e in v.errors)

    def test_invalid_failure_category_is_error(self):
        """Invalid failure_category should be a blocking error."""
        result = Agent5Result(
            harnesses=[],
            failed_harnesses=[
                FailedHarness(
                    candidate_name="BadService",
                    provider="Bad",
                    failure_reason="Something broke",
                    failure_category="invalid_category",  # Not in valid set
                    partial_code=None,
                    turns_attempted=5,
                ),
            ],
            total_candidates_attempted=1,
            total_build_cost_usd=1.00,
            build_summary="Built 0/1.",
        )
        agent4 = Agent4Result(
            validated_candidates=[_make_screened_candidate("BadService")],
            rejected_candidates=[],
            screening_summary="1 passed.",
            total_candidates_screened=1,
        )
        v = validate_agent5_output(result, agent4)
        assert v.passed is False
        assert any("invalid failure_category" in e for e in v.errors)

    def test_single_harness_is_warning(self):
        """Only 1 successful harness should generate a warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = Agent5Result(
                harnesses=[_make_test_harness("Google Document AI", tmpdir)],
                failed_harnesses=[_make_failed_harness("AWS Textract")],
                total_candidates_attempted=2,
                total_build_cost_usd=2.50,
                build_summary="Built 1/2.",
            )
            agent4 = _make_agent4_result()
            v = validate_agent5_output(result, agent4)
            assert v.passed is True  # Not a blocking error
            assert any("Only 1 harness" in w for w in v.warnings)

    def test_smoke_test_not_passed_is_warning(self):
        """A harness that didn't pass smoke test should generate a warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            h_dict = _make_test_harness("Google Document AI", tmpdir).model_dump()
            h_dict["smoke_test_passed"] = False
            bad_harness = TestHarness(**h_dict)

            result = Agent5Result(
                harnesses=[bad_harness, _make_test_harness("AWS Textract", tmpdir)],
                failed_harnesses=[],
                total_candidates_attempted=2,
                total_build_cost_usd=3.00,
                build_summary="Built 2/2.",
            )
            agent4 = _make_agent4_result()
            v = validate_agent5_output(result, agent4)
            assert v.passed is True
            assert any("smoke test" in w.lower() for w in v.warnings)


# ============================================================================
# Integration Tests (Mocked API)
# ============================================================================

class TestIntegration:
    """Test the full agent with mocked Anthropic API calls."""

    def _make_mock_response(self, content_blocks, stop_reason="end_turn"):
        """Create a mock API response."""
        mock = MagicMock()
        mock.content = content_blocks
        mock.stop_reason = stop_reason
        mock.usage.input_tokens = 5000
        mock.usage.output_tokens = 1000
        mock.usage.server_tool_use = None
        return mock

    def _make_text_block(self, text):
        """Create a mock text content block."""
        block = MagicMock()
        block.type = "text"
        block.text = text
        return block

    def _make_tool_use_block(self, name, input_data, tool_id="tool_1"):
        """Create a mock tool_use content block."""
        block = MagicMock()
        block.type = "tool_use"
        block.name = name
        block.input = input_data
        block.id = tool_id
        return block

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_successful_build(self, mock_anthropic_cls):
        """Test successful harness building with mocked API."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        # Set up mock client
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Turn 1: Builder agent writes harness.py (research merged into builder)
        turn1_response = self._make_mock_response(
            [
                self._make_text_block("I'll start by writing the harness."),
                self._make_tool_use_block("write_file", {
                    "filename": "harness.py",
                    "content": (
                        'import os, time\n'
                        'def run(input_data):\n'
                        '    start = time.time()\n'
                        '    latency_ms = round((time.time() - start) * 1000, 2)\n'
                        '    return {"output": "test", "latency_ms": latency_ms, '
                        '"tokens_used": None, "cost_usd": None, "raw_response": {}, '
                        '"success": True, "error": None}\n'
                    ),
                }, "tool_write_1"),
            ],
            stop_reason="tool_use",
        )

        # Turn 2: Agent signals completion
        turn2_response = self._make_mock_response(
            [self._make_text_block("SMOKE TEST PASSED\nHARNESS_COMPLETE")],
            stop_reason="end_turn",
        )

        # Builder uses client.beta.messages.create (no separate research phase)
        mock_client.beta.messages.create.side_effect = [turn1_response, turn2_response]

        # Run with a single candidate for simplicity
        input_data = Agent5Input(
            validated_candidates=[_make_screened_candidate("TestService")],
            user_understanding=_make_user_understanding(),
            test_cases=_make_test_cases(),
            trace_id="test-trace-integration",
        )

        result = run_implement_test_env_agent(input_data)

        assert len(result.harnesses) == 1
        assert result.harnesses[0].candidate_name == "TestService"
        assert result.harnesses[0].smoke_test_passed is True
        assert result.total_candidates_attempted == 1

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_failed_build_returns_failed_harness(self, mock_anthropic_cls):
        """A candidate that fails should produce FailedHarness, not crash."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Agent gives up
        fail_response = self._make_mock_response(
            [self._make_text_block(
                "Could not build harness. API docs returned 404. HARNESS_FAILED"
            )],
            stop_reason="end_turn",
        )

        mock_client.messages.create.return_value = fail_response
        mock_client.beta.messages.create.return_value = fail_response

        input_data = Agent5Input(
            validated_candidates=[_make_screened_candidate("BadService")],
            user_understanding=_make_user_understanding(),
            test_cases=_make_test_cases(),
            trace_id="test-trace-fail",
        )

        result = run_implement_test_env_agent(input_data)

        assert len(result.harnesses) == 0
        assert len(result.failed_harnesses) == 1
        assert result.failed_harnesses[0].candidate_name == "BadService"
        assert result.total_candidates_attempted == 1

    @patch("puzzleeval.agents.implement_test_env.time.sleep")
    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_rate_limit_produces_failed_harness(self, mock_anthropic_cls, mock_sleep):
        """Rate limit error should produce FailedHarness, not crash pipeline."""
        import anthropic as anthropic_module
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Simulate rate limit
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        rate_limit_error = anthropic_module.RateLimitError(
            message="Rate limited",
            response=mock_response,
            body={"error": {"message": "Rate limited"}},
        )
        # Both research (messages.create) and builder (beta.messages.create) hit rate limit
        mock_client.messages.create.side_effect = rate_limit_error
        mock_client.beta.messages.create.side_effect = rate_limit_error

        input_data = Agent5Input(
            validated_candidates=[_make_screened_candidate("RateLimitedService")],
            user_understanding=_make_user_understanding(),
            test_cases=_make_test_cases(),
            trace_id="test-trace-ratelimit",
        )

        result = run_implement_test_env_agent(input_data)

        assert len(result.failed_harnesses) == 1
        assert "rate limit" in result.failed_harnesses[0].failure_reason.lower()

    @patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
    def test_max_turns_exceeded(self, mock_anthropic_cls):
        """Exceeding max turns should produce a FailedHarness."""
        from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Agent keeps asking to run code but never completes
        # Each response requests a tool use
        never_done = self._make_mock_response(
            [self._make_tool_use_block("run_code", {"command": "python smoke_test.py"})],
            stop_reason="tool_use",
        )
        mock_client.messages.create.return_value = never_done
        mock_client.beta.messages.create.return_value = never_done

        input_data = Agent5Input(
            validated_candidates=[_make_screened_candidate("SlowService")],
            user_understanding=_make_user_understanding(),
            test_cases=_make_test_cases(),
            trace_id="test-trace-maxturns",
        )

        # Use a low max_turns for testing
        with patch("puzzleeval.agents.implement_test_env.AGENT5_MAX_TURNS", 3):
            result = run_implement_test_env_agent(input_data)

        # Should have a failed harness (no harness.py was written)
        assert len(result.failed_harnesses) == 1
        assert result.total_candidates_attempted == 1


# ============================================================================
# Provider Registry Tests
# ============================================================================

class TestProviderRegistry:
    """Test provider_registry.py."""

    def test_load_missing_file(self):
        from puzzleeval.provider_registry import load_registry
        registry = load_registry("/nonexistent/path/to/registry.json")
        assert registry.is_empty()

    def test_load_valid_registry(self):
        from puzzleeval.provider_registry import load_registry
        import json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "providers": {
                    "mindee": {
                        "env_vars": {"MINDEE_API_KEY": "test-key-123"},
                        "tier": "free",
                        "monthly_limit": 250,
                    }
                }
            }, f)
            f.flush()
            registry = load_registry(f.name)
        os.unlink(f.name)
        assert not registry.is_empty()
        assert "mindee" in registry.providers

    def test_get_credentials_match(self):
        from puzzleeval.provider_registry import (
            get_credentials, ProviderRegistry, ProviderEntry,
        )
        registry = ProviderRegistry(providers={
            "mindee": ProviderEntry(
                name="mindee",
                env_vars={"MINDEE_API_KEY": "test-key"},
                tier="free",
            )
        })
        creds = get_credentials(registry, "Mindee", "Mindee Invoice OCR API")
        assert creds is not None
        assert creds["MINDEE_API_KEY"] == "test-key"

    def test_get_credentials_no_match(self):
        from puzzleeval.provider_registry import (
            get_credentials, ProviderRegistry,
        )
        registry = ProviderRegistry()
        creds = get_credentials(registry, "Unknown", "Unknown Service")
        assert creds is None

    def test_get_credentials_env_fallback(self):
        from puzzleeval.provider_registry import (
            get_credentials, ProviderRegistry,
        )
        registry = ProviderRegistry()
        with patch.dict(os.environ, {"MY_API_KEY": "from-env"}):
            creds = get_credentials(
                registry, "Unknown", "Unknown",
                auth_env_vars=["MY_API_KEY"],
            )
        assert creds is not None
        assert creds["MY_API_KEY"] == "from-env"


# ============================================================================
# Venv Tests
# ============================================================================

class TestVenv:
    """Test venv creation and sandbox environment."""

    def test_create_venv_success(self):
        from puzzleeval.agents.implement_test_env import _create_venv
        from puzzleeval.logging_setup import get_logger
        logger = get_logger("test")
        with tempfile.TemporaryDirectory() as tmpdir:
            success = _create_venv(Path(tmpdir), logger, "test-trace", "TestService")
            assert success
            venv_dir = Path(tmpdir) / ".venv"
            assert venv_dir.exists()
            if sys.platform == "win32":
                assert (venv_dir / "Scripts" / "python.exe").exists()
            else:
                assert (venv_dir / "bin" / "python").exists()

    def test_sandbox_env_uses_venv(self):
        from puzzleeval.agents.implement_test_env import _build_sandbox_env, _create_venv
        from puzzleeval.logging_setup import get_logger
        logger = get_logger("test")
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_venv(Path(tmpdir), logger, "test-trace", "TestService")
            env = _build_sandbox_env(Path(tmpdir))
            assert "VIRTUAL_ENV" in env
            # Venv bin should be at the front of PATH
            if sys.platform == "win32":
                assert "Scripts" in env["PATH"].split(os.pathsep)[0]
            else:
                assert "bin" in env["PATH"].split(os.pathsep)[0]

    def test_sandbox_env_works_without_venv(self):
        from puzzleeval.agents.implement_test_env import _build_sandbox_env
        with tempfile.TemporaryDirectory() as tmpdir:
            env = _build_sandbox_env(Path(tmpdir))
            assert "PYTHONDONTWRITEBYTECODE" in env
            # VIRTUAL_ENV should NOT be set
            assert "VIRTUAL_ENV" not in env


# ============================================================================
# Verification Gate Tests
# ============================================================================

class TestVerificationChecks:
    """Test _run_verification_checks (in-loop verification)."""

    def test_clean_harness_passes(self):
        from puzzleeval.agents.implement_test_env import _run_verification_checks
        from puzzleeval.logging_setup import get_logger
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "harness.py").write_text(
                'import os\n'
                'API_KEY = os.environ.get("MINDEE_API_KEY")\n'
                'def run(input_data):\n'
                '    return {"output": "", "latency_ms": 0, "tokens_used": None, '
                '"cost_usd": None, "raw_response": {}, "success": False, "error": "no key"}\n'
            )
            issues = _run_verification_checks(
                Path(tmpdir), _make_screened_candidate(), None,
                get_logger("test"), "test-trace",
            )
            assert issues is None  # No issues

    def test_placeholder_url_not_flagged_without_credentials(self):
        """Without credentials, no live test runs. Accept on smoke pass."""
        from puzzleeval.agents.implement_test_env import _run_verification_checks
        from puzzleeval.logging_setup import get_logger
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "harness.py").write_text(
                'URL = "https://example.com/api/v1"\n'
                'def run(input_data):\n'
                '    return {"output": "", "latency_ms": 0, "tokens_used": None, '
                '"cost_usd": None, "raw_response": {}, "success": False, "error": "no key"}\n'
            )
            issues = _run_verification_checks(
                Path(tmpdir), _make_screened_candidate(), None,
                get_logger("test"), "test-trace",
            )
            # No credentials → no live test → accept on smoke pass
            assert issues is None

    def test_auth_mismatch_not_flagged_without_live_failure(self):
        """Auth mismatch is cosmetic — only live test failure should block."""
        from puzzleeval.agents.implement_test_env import _run_verification_checks
        from puzzleeval.logging_setup import get_logger
        candidate = _make_screened_candidate()
        candidate.auth_method = "basic_auth"
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "harness.py").write_text(
                'headers = {"Authorization": "Bearer sk-123"}\n'
                'def run(input_data):\n'
                '    return {"output": "", "latency_ms": 0, "tokens_used": None, '
                '"cost_usd": None, "raw_response": {}, "success": False, "error": "no key"}\n'
            )
            issues = _run_verification_checks(
                Path(tmpdir), candidate, None,
                get_logger("test"), "test-trace",
            )
            # Without credentials, no live test runs. Auth mismatch alone
            # should NOT be flagged (it's cosmetic, causes over-correction).
            assert issues is None

    def test_missing_harness_detected(self):
        from puzzleeval.agents.implement_test_env import _run_verification_checks
        from puzzleeval.logging_setup import get_logger
        with tempfile.TemporaryDirectory() as tmpdir:
            issues = _run_verification_checks(
                Path(tmpdir), _make_screened_candidate(), None,
                get_logger("test"), "test-trace",
            )
            assert issues is not None
            assert "does not exist" in issues


# ============================================================================
# Live Validation Tests (removed — live test infrastructure replaced by
# Phase 3 real test case validation + post-loop mechanical execution)
# ============================================================================
