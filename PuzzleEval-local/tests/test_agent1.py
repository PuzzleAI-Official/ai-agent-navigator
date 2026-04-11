# ============================================================================
# Tests for Agent 1 (User Understanding Agent)
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/ -v
# ============================================================================

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.schemas import (
    Agent1Input,
    Agent1Result,
    ClarifyingResponse,
    Constraints,
    InfoStatus,
    SubTask,
    UserUnderstandingOutput,
)
from puzzleeval.file_parsers import parse_csv, parse_txt, parse_file
from puzzleeval.exceptions import AgentFileParseError


class TestSchemas:
    """Test that Pydantic schemas validate correctly."""

    def test_clear_result_with_subtasks(self):
        """A fully populated clear result with sub-tasks should be valid."""
        result = Agent1Result(
            is_clear=True,
            result=UserUnderstandingOutput(
                summary="User needs AI for invoice OCR and QuickBooks entry",
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
                search_keywords=["invoice processing automation", "receipt to QuickBooks AI"],
                constraints=Constraints(
                    budget_range="$50-200/mo",
                    must_have_features=["OCR"],
                    integration_requirements=["QuickBooks"],
                    technical_level="non-technical",
                ),
                workflow_summary=None,
            ),
            clarification_needed=None,
        )
        assert result.is_clear is True
        assert len(result.result.sub_tasks) == 2
        assert result.result.sub_tasks[0].capability == "document OCR"
        assert result.result.search_strategy == "both"

    def test_clarifying_result_is_valid(self):
        """A clarifying response should have critical questions + info tracking."""
        result = Agent1Result(
            is_clear=False,
            result=None,
            clarification_needed=ClarifyingResponse(
                message="I can see you need AI help — could you share a bit more?",
                critical_questions=["What specific tasks take the most time in your day-to-day?"],
                optional_prompt="If you'd like, you can also share your rough budget and any tools you currently use.",
                partial_understanding="User wants AI for business",
                info_status=InfoStatus(
                    has_concrete_subtasks=False,
                    has_domain=False,
                ),
            ),
        )
        assert result.is_clear is False
        assert len(result.clarification_needed.critical_questions) == 1
        assert result.clarification_needed.info_status.has_concrete_subtasks is False
        assert result.clarification_needed.optional_prompt is not None

    def test_agent1_input_requires_user_text(self):
        """Agent1Input should require user_text and trace_id."""
        input_data = Agent1Input(
            user_text="I need AI for support",
            trace_id="test-trace-123",
        )
        assert input_data.user_text == "I need AI for support"
        assert input_data.workflow_file_path is None
        assert input_data.conversation_history is None


class TestFileParsers:
    """Test file parsing functions."""

    def test_parse_csv_basic(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write("Name,Email,Role\nAlice,alice@test.com,Engineer\nBob,bob@test.com,Designer\n")
            csv_path = f.name
        try:
            result = parse_csv(csv_path)
            assert "Name" in result and "Alice" in result
        finally:
            os.unlink(csv_path)

    def test_parse_csv_truncation(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write("id,value\n")
            for i in range(200):
                f.write(f"{i},data_{i}\n")
            csv_path = f.name
        try:
            result = parse_csv(csv_path, max_rows=10)
            assert "Showing 10 of 200 total rows" in result
        finally:
            os.unlink(csv_path)

    def test_parse_txt(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("Step 1: Receive customer email\nStep 2: Respond")
            txt_path = f.name
        try:
            assert "Step 1" in parse_txt(txt_path)
        finally:
            os.unlink(txt_path)

    def test_unsupported_format_raises_error(self):
        with pytest.raises(AgentFileParseError, match="Unsupported file format"):
            parse_file("document.exe")

    def test_missing_file_raises_error(self):
        with pytest.raises(AgentFileParseError, match="not found"):
            parse_txt("/nonexistent/path/file.txt")


class TestAgent1:
    """Test the user understanding agent with mocked API calls."""

    def _make_mock_response(self, parsed_output: Agent1Result) -> MagicMock:
        mock_response = MagicMock()
        mock_response.parsed_output = parsed_output
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 500
        mock_response.usage.output_tokens = 200
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        return mock_response

    @patch("puzzleeval.agents.user_understanding.anthropic.Anthropic")
    def test_clear_request_returns_result(self, mock_anthropic_class):
        expected_result = Agent1Result(
            is_clear=True,
            result=UserUnderstandingOutput(
                summary="Need AI for Shopify customer support",
                sub_tasks=[
                    SubTask(
                        description="Answer product questions from catalog",
                        capability="customer support chatbot",
                        search_keywords=["customer support chatbot API", "e-commerce AI"],
                    ),
                    SubTask(
                        description="Categorize and route support tickets",
                        capability="text classification",
                        search_keywords=["ticket classification AI", "email triage"],
                    ),
                ],
                search_strategy="both",
                domain="e-commerce",
                search_keywords=["Shopify customer support AI", "e-commerce chatbot"],
                constraints=Constraints(),
                workflow_summary=None,
            ),
            clarification_needed=None,
        )

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.parse.return_value = self._make_mock_response(expected_result)

        input_data = Agent1Input(
            user_text="I need an AI chatbot for my Shopify store customer support",
            trace_id="test-123",
        )

        from puzzleeval.agents.user_understanding import run_user_understanding_agent
        result = run_user_understanding_agent(input_data)

        assert result.is_clear is True
        assert len(result.result.sub_tasks) >= 1
        assert result.result.domain == "e-commerce"

    @patch("puzzleeval.agents.user_understanding.anthropic.Anthropic")
    def test_vague_request_returns_clarification(self, mock_anthropic_class):
        expected_result = Agent1Result(
            is_clear=False,
            result=None,
            clarification_needed=ClarifyingResponse(
                message="I'd love to help — could you share a bit more?",
                critical_questions=["What tasks take the most time?"],
                optional_prompt="Feel free to also mention any tools you use.",
                partial_understanding="User wants AI for business",
                info_status=InfoStatus(
                    has_concrete_subtasks=False,
                    has_domain=False,
                ),
            ),
        )

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.parse.return_value = self._make_mock_response(expected_result)

        input_data = Agent1Input(user_text="I need AI for my business", trace_id="test-456")

        from puzzleeval.agents.user_understanding import run_user_understanding_agent
        result = run_user_understanding_agent(input_data)

        assert result.is_clear is False
        assert len(result.clarification_needed.critical_questions) >= 1


class TestLogging:
    """Test that structured logging produces valid JSON."""

    def test_log_produces_json(self, capsys):
        import logging
        from puzzleeval.logging_setup import StructuredJsonFormatter

        logger = logging.getLogger("test_logger")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredJsonFormatter())
        logger.addHandler(handler)

        logger.info("Test message", extra={"trace_id": "test-trace", "tokens_in": 100})

        captured = capsys.readouterr()
        log_data = json.loads(captured.err.strip())
        assert log_data["level"] == "INFO"
        assert log_data["message"] == "Test message"
        assert log_data["trace_id"] == "test-trace"
        assert log_data["tokens_in"] == 100
