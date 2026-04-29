# ============================================================================
# Tests for Agent 3 (Synthetic Test Cases Agent)
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_agent3.py -v
#
# These tests use mocked API calls — no real API key or web searches needed.
# The mock pattern matches test_agent1.py and test_agent2.py: we mock the
# Anthropic client and control what it returns, then verify Agent 3 handles
# it correctly.
# ============================================================================

from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.schemas import (
    Agent3Input,
    Agent3Result,
    Constraints,
    JudgementCriterion,
    SubTask,
    TestCase,
    UserUnderstandingOutput,
)


# ============================================================================
# Test Fixtures — Reusable test data
# ============================================================================

def _make_user_understanding() -> UserUnderstandingOutput:
    """Create a sample Agent 1 output for testing Agent 3."""
    return UserUnderstandingOutput(
        summary="User needs AI to extract data from invoices and enter it into QuickBooks",
        sub_tasks=[
            SubTask(
                description="Extract structured data from invoice photos",
                capability="document OCR",
                search_keywords=["invoice OCR API", "document data extraction"],
            ),
            SubTask(
                description="Create bill entries in QuickBooks from structured data",
                capability="accounting integration",
                search_keywords=["QuickBooks API automation", "accounting data entry"],
            ),
        ],
        search_strategy="both",
        domain="accounting",
        search_keywords=["invoice processing automation", "receipt to accounting AI"],
        constraints=Constraints(
            budget_range="$50-200/mo",
            must_have_features=["OCR"],
            integration_requirements=["QuickBooks"],
            technical_level="non-technical",
        ),
        workflow_summary=None,
    )


def _make_sample_test_cases() -> list[TestCase]:
    """Create sample test cases for testing."""
    return [
        TestCase(
            id="tc-001",
            sub_task_ref="Extract structured data from invoice photos",
            scenario="A standard printed invoice from a US vendor with 3 line items",
            input_type="document_content",
            input_data=(
                "Invoice #4521\nVendor: Acme Corp\nDate: 2026-03-15\n"
                "Line items:\n  Widget A - $250.00 x 2 = $500.00\n"
                "  Widget B - $150.00 x 5 = $750.00\n"
                "Total: $1,250.00"
            ),
            input_context={"language": "en", "document_format": "invoice"},
            test_file_path="/uploads/invoice_001.pdf",
            output_type="extraction",
            expected_output='{"vendor": "Acme Corp", "date": "2026-03-15", "total": 1250.00}',
            judgement_criteria=[
                JudgementCriterion(
                    criterion="Must extract vendor name correctly",
                    weight=0.3,
                    eval_type="exact_match",
                ),
                JudgementCriterion(
                    criterion="Must extract total amount correctly",
                    weight=0.3,
                    eval_type="exact_match",
                ),
                JudgementCriterion(
                    criterion="Must extract all line items",
                    weight=0.3,
                    eval_type="contains_key_info",
                ),
                JudgementCriterion(
                    criterion="Output must be valid JSON",
                    weight=0.1,
                    eval_type="format_compliance",
                ),
            ],
            difficulty="easy",
            tags=["happy_path", "us_vendor"],
        ),
        TestCase(
            id="tc-002",
            sub_task_ref="Extract structured data from invoice photos",
            scenario="A handwritten invoice with poor scan quality",
            input_type="document_content",
            input_data=(
                "Invoice\nFrom: Bob's Plumbing\nTo: Jane Smith\n"
                "Service: Pipe repair - $375.00\nParts: $89.50\n"
                "Total: $464.50"
            ),
            input_context={"language": "en", "document_format": "invoice", "quality": "poor"},
            test_file_path="/uploads/invoice_002.jpg",
            output_type="extraction",
            expected_output='{"vendor": "Bob\'s Plumbing", "total": 464.50}',
            judgement_criteria=[
                JudgementCriterion(
                    criterion="Must extract vendor name",
                    weight=0.4,
                    eval_type="semantic_similarity",
                ),
                JudgementCriterion(
                    criterion="Must extract total amount",
                    weight=0.4,
                    eval_type="exact_match",
                ),
                JudgementCriterion(
                    criterion="Output must be valid JSON",
                    weight=0.2,
                    eval_type="format_compliance",
                ),
            ],
            difficulty="hard",
            tags=["input_variation", "error_resilience"],
        ),
        TestCase(
            id="tc-003",
            sub_task_ref="Create bill entries in QuickBooks from structured data",
            scenario="Standard bill entry from extracted invoice data",
            input_type="structured_data",
            input_data='{"vendor": "Acme Corp", "date": "2026-03-15", "total": 1250.00, "line_items": [{"description": "Widget A", "amount": 500.00}]}',
            input_context=None,
            test_file_path=None,
            output_type="action",
            expected_output='{"action": "create_bill", "vendor": "Acme Corp", "amount": 1250.00, "status": "created"}',
            judgement_criteria=[
                JudgementCriterion(
                    criterion="Must create a bill with correct vendor",
                    weight=0.3,
                    eval_type="exact_match",
                ),
                JudgementCriterion(
                    criterion="Must set correct amount",
                    weight=0.3,
                    eval_type="exact_match",
                ),
                JudgementCriterion(
                    criterion="Must return success status",
                    weight=0.2,
                    eval_type="contains_key_info",
                ),
                JudgementCriterion(
                    criterion="Response must be valid JSON",
                    weight=0.2,
                    eval_type="format_compliance",
                ),
            ],
            difficulty="easy",
            tags=["happy_path"],
        ),
    ]


def _make_agent3_result() -> Agent3Result:
    """Create a sample Agent3Result for testing."""
    return Agent3Result(
        test_cases=_make_sample_test_cases(),
        generation_notes=(
            "Generated 3 test cases across 2 sub-tasks. "
            "Invoice OCR covered with happy_path and error_resilience. "
            "QuickBooks integration covered with happy_path."
        ),
        coverage_summary={
            "Extract structured data from invoice photos": 2,
            "Create bill entries in QuickBooks from structured data": 1,
        },
    )


# ============================================================================
# Schema Validation Tests
# ============================================================================

class TestSchemas:
    """Test that Agent 3 Pydantic schemas validate correctly."""

    def test_judgement_criterion_all_fields(self):
        """A fully populated JudgementCriterion should be valid."""
        criterion = JudgementCriterion(
            criterion="Must extract vendor name",
            weight=0.3,
            eval_type="exact_match",
        )
        assert criterion.criterion == "Must extract vendor name"
        assert criterion.weight == 0.3
        assert criterion.eval_type == "exact_match"

    def test_test_case_with_all_fields(self):
        """A fully populated TestCase should be valid."""
        tc = _make_sample_test_cases()[0]
        assert tc.id == "tc-001"
        assert tc.sub_task_ref == "Extract structured data from invoice photos"
        assert tc.input_type == "document_content"
        assert tc.test_file_path == "/uploads/invoice_001.pdf"
        assert tc.output_type == "extraction"
        assert len(tc.judgement_criteria) == 4
        assert tc.difficulty == "easy"
        assert "happy_path" in tc.tags

    def test_test_case_without_file(self):
        """A TestCase with no test file should be valid."""
        tc = _make_sample_test_cases()[2]  # QuickBooks entry — no file needed
        assert tc.test_file_path is None
        assert tc.input_type == "structured_data"

    def test_test_case_with_file(self):
        """A TestCase with a user-uploaded file should have test_file_path set."""
        tc = _make_sample_test_cases()[0]  # Invoice OCR — has file
        assert tc.test_file_path is not None
        assert tc.test_file_path.endswith(".pdf")

    def test_agent3_result_with_test_cases(self):
        """Agent3Result should hold test cases and coverage summary."""
        result = _make_agent3_result()
        assert len(result.test_cases) == 3
        assert "Extract structured data from invoice photos" in result.coverage_summary
        assert result.coverage_summary["Extract structured data from invoice photos"] == 2

    def test_agent3_input_requires_fields(self):
        """Agent3Input needs user_understanding and trace_id."""
        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-trace-agent3",
        )
        assert input_data.trace_id == "test-trace-agent3"
        assert len(input_data.user_understanding.sub_tasks) == 2

    def test_judgement_criteria_weight_range(self):
        """Weights from our fixtures should be in valid range."""
        for tc in _make_sample_test_cases():
            for criterion in tc.judgement_criteria:
                assert 0.0 <= criterion.weight <= 1.0, (
                    f"Test case {tc.id} has out-of-range weight: {criterion.weight}"
                )

    def test_judgement_criteria_weights_sum_approximately_one(self):
        """Weights for each test case should sum to approximately 1.0."""
        for tc in _make_sample_test_cases():
            total_weight = sum(c.weight for c in tc.judgement_criteria)
            assert 0.9 <= total_weight <= 1.1, (
                f"Test case {tc.id} weights sum to {total_weight}, expected ~1.0"
            )


# ============================================================================
# Helper Function Tests
# ============================================================================

class TestHelpers:
    """Test Agent 3 helper functions."""

    def test_build_generation_message_includes_subtasks(self):
        """The generation message should include all sub-tasks and domain."""
        from puzzleeval.agents.agent3.core import _build_generation_message

        user_understanding = _make_user_understanding()
        message = _build_generation_message(user_understanding)

        # Should contain the summary
        assert "extract data from invoices" in message.lower()
        # Should contain sub-task descriptions
        assert "Extract structured data from invoice photos" in message
        assert "Create bill entries in QuickBooks" in message
        # Should contain domain
        assert "accounting" in message
        # Should contain constraints
        assert "$50-200/mo" in message
        assert "non-technical" in message

    def test_build_generation_message_calculates_target_count(self):
        """Should calculate target case count based on sub-task count."""
        from puzzleeval.agents.agent3.core import _build_generation_message

        user_understanding = _make_user_understanding()  # 2 sub-tasks, no workflow
        message = _build_generation_message(user_understanding)

        # 2 sub-tasks × 7 base = 14 total
        assert "14" in message

    def test_build_generation_message_workflow_bonus(self):
        """Should add bonus cases when workflow_summary is present."""
        from puzzleeval.agents.agent3.core import _build_generation_message

        user_understanding = _make_user_understanding()
        user_understanding.workflow_summary = "Invoice processing workflow with 5 steps"
        message = _build_generation_message(user_understanding)

        # 2 sub-tasks × (7 + 2 workflow bonus) = 18 total
        assert "18" in message

    def test_build_generation_message_handles_missing_constraints(self):
        """Should handle missing optional constraints gracefully."""
        from puzzleeval.agents.agent3.core import _build_generation_message

        user_understanding = UserUnderstandingOutput(
            summary="Need AI for document processing",
            sub_tasks=[
                SubTask(
                    description="Parse documents",
                    capability="OCR",
                    search_keywords=["OCR API"],
                ),
            ],
            search_strategy="both",
            domain="general",
            search_keywords=["document processing AI"],
            constraints=Constraints(),
            workflow_summary=None,
        )
        message = _build_generation_message(user_understanding)

        assert "Not specified" in message
        assert "None specified" in message

    def test_build_generation_message_min_clamp(self):
        """Single sub-task should still produce at least 10 target cases."""
        from puzzleeval.agents.agent3.core import _build_generation_message

        user_understanding = UserUnderstandingOutput(
            summary="Need AI chatbot",
            sub_tasks=[
                SubTask(
                    description="Answer customer questions",
                    capability="text generation",
                    search_keywords=["chatbot API"],
                ),
            ],
            search_strategy="both",
            domain="customer support",
            search_keywords=["AI chatbot"],
            constraints=Constraints(),
            workflow_summary=None,
        )
        message = _build_generation_message(user_understanding)

        # 1 sub-task × 7 = 7, but min is 10
        assert "10" in message


# ============================================================================
# Agent Integration Tests (Mocked API)
# ============================================================================

class TestSyntheticTestsAgent:
    """Test the synthetic tests agent with mocked API calls."""

    def _make_mock_response(self, parsed_output: Agent3Result) -> MagicMock:
        """Create a mock response for the structured output call."""
        mock_response = MagicMock()
        mock_response.parsed_output = parsed_output
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 1500
        mock_response.usage.output_tokens = 3000
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        return mock_response

    @patch("puzzleeval.agents.agent3.core.anthropic.Anthropic")
    def test_valid_input_produces_result(self, mock_anthropic_class):
        """Valid input should produce a valid Agent3Result."""
        expected_result = _make_agent3_result()

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.parse.return_value = self._make_mock_response(
            expected_result
        )

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-agent3-001",
        )

        from puzzleeval.agents.agent3.core import run_synthetic_tests_agent
        result = run_synthetic_tests_agent(input_data)

        assert len(result.test_cases) == 3
        assert result.test_cases[0].id == "tc-001"
        assert len(result.coverage_summary) == 2

        # Verify messages.parse was called (single step, not two)
        assert mock_client.messages.parse.call_count == 1

    @patch("puzzleeval.agents.agent3.core.anthropic.Anthropic")
    def test_api_connection_error_raises_agent_error(self, mock_anthropic_class):
        """API connection error should raise AgentAPIError."""
        import anthropic as anthropic_module
        from puzzleeval.exceptions import AgentAPIError

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.parse.side_effect = anthropic_module.APIConnectionError(
            request=MagicMock()
        )

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-agent3-002",
        )

        from puzzleeval.agents.agent3.core import run_synthetic_tests_agent

        with pytest.raises(AgentAPIError, match="Failed to connect"):
            run_synthetic_tests_agent(input_data)

    @patch("puzzleeval.agents.agent3.core.anthropic.Anthropic")
    def test_rate_limit_error_raises_rate_limit_error(self, mock_anthropic_class):
        """Rate limit error should raise AgentRateLimitError."""
        import anthropic as anthropic_module
        from puzzleeval.exceptions import AgentRateLimitError

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.parse.side_effect = anthropic_module.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body=None,
        )

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-agent3-003",
        )

        from puzzleeval.agents.agent3.core import run_synthetic_tests_agent

        with pytest.raises(AgentRateLimitError, match="Rate limit"):
            run_synthetic_tests_agent(input_data)

    @patch("puzzleeval.agents.agent3.core.anthropic.Anthropic")
    def test_none_output_raises_output_error(self, mock_anthropic_class):
        """If parsed output is None, should raise AgentOutputError."""
        from puzzleeval.exceptions import AgentOutputError

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        mock_response = self._make_mock_response(None)
        mock_response.parsed_output = None
        mock_client.messages.parse.return_value = mock_response

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-agent3-004",
        )

        from puzzleeval.agents.agent3.core import run_synthetic_tests_agent

        with pytest.raises(AgentOutputError, match="no parsed output"):
            run_synthetic_tests_agent(input_data)

    @patch("puzzleeval.agents.agent3.core.anthropic.Anthropic")
    def test_uses_default_model(self, mock_anthropic_class):
        """Should use DEFAULT_MODEL for the structured output call."""
        expected_result = _make_agent3_result()

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.parse.return_value = self._make_mock_response(
            expected_result
        )

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-agent3-005",
        )

        from puzzleeval.agents.agent3.core import run_synthetic_tests_agent
        from puzzleeval.config import DEFAULT_MODEL

        run_synthetic_tests_agent(input_data)

        parse_call = mock_client.messages.parse.call_args
        assert parse_call.kwargs.get("model") == DEFAULT_MODEL
