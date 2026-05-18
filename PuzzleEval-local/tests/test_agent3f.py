# ============================================================================
# Tests for Agent 3F (File-Based Test Cases Agent)
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_agent3f.py -v
#
# Tests use mocked API calls — no real API key needed.
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
# Fixtures
# ============================================================================

def _make_user_understanding() -> UserUnderstandingOutput:
    return UserUnderstandingOutput(
        summary="User needs AI to extract data from invoices",
        sub_tasks=[
            SubTask(
                description="Extract structured data from invoice photos",
                capability="document OCR",
                search_keywords=["invoice OCR API", "document data extraction"],
                requires_test_files=True,
                test_file_description="5-10 sample invoice photos or PDFs",
            ),
        ],
        search_strategy="both",
        domain="accounting",
        search_keywords=["invoice processing automation"],
        constraints=Constraints(),
        workflow_summary=None,
    )


def _make_file_test_cases() -> list[TestCase]:
    return [
        TestCase(
            id="tc-001",
            sub_task_ref="Extract structured data from invoice photos",
            scenario="Standard printed invoice from user's collection",
            input_type="document_content",
            input_data="Invoice #4521 from Acme Corp, 3 line items, total $1,250.00",
            input_context={"document_format": "invoice"},
            test_file_path="/uploads/invoice_001.pdf",
            output_type="extraction",
            expected_output='{"vendor": "Acme Corp", "total": 1250.00}',
            judgement_criteria=[
                JudgementCriterion(criterion="Extract vendor name", weight=0.5, eval_type="exact_match"),
                JudgementCriterion(criterion="Extract total", weight=0.5, eval_type="exact_match"),
            ],
            difficulty="easy",
            tags=["happy_path"],
        ),
    ]


def _make_agent3_result() -> Agent3Result:
    return Agent3Result(
        test_cases=_make_file_test_cases(),
        generation_notes="Generated 1 test case from 1 user-uploaded file",
        coverage_summary={"Extract structured data from invoice photos": 1},
    )


# ============================================================================
# Helper Function Tests
# ============================================================================

class TestHelpers:

    @patch("puzzleeval.agents.agent3f.core.parse_file")
    def test_build_file_message_includes_context(self, mock_parse):
        from puzzleeval.agents.agent3f.core import _build_file_message

        mock_parse.return_value = "Invoice text content here"

        uo = _make_user_understanding()
        blocks = _build_file_message(uo, ["/tmp/test.txt"])

        # Should contain user summary
        full_text = " ".join(
            b["text"] for b in blocks if b.get("type") == "text"
        )
        assert "extract data from invoices" in full_text.lower()
        assert "accounting" in full_text.lower()
        assert "test.txt" in full_text

    @patch("puzzleeval.agents.agent3f.core.parse_file")
    def test_build_file_message_handles_text_files(self, mock_parse):
        from puzzleeval.agents.agent3f.core import _build_file_message

        mock_parse.return_value = "CSV data here"

        blocks = _build_file_message(_make_user_understanding(), ["/tmp/data.csv"])

        text_blocks = [b for b in blocks if b.get("type") == "text"]
        full_text = " ".join(b["text"] for b in text_blocks)
        assert "CSV data here" in full_text

    @patch("puzzleeval.agents.agent3f.core.parse_file")
    def test_build_file_message_handles_binary_files(self, mock_parse):
        from puzzleeval.agents.agent3f.core import _build_file_message

        # Simulate a PDF content block
        mock_parse.return_value = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "abc123"},
        }

        blocks = _build_file_message(_make_user_understanding(), ["/tmp/invoice.pdf"])

        doc_blocks = [b for b in blocks if b.get("type") == "document"]
        assert len(doc_blocks) == 1
        assert doc_blocks[0]["source"]["media_type"] == "application/pdf"

    @patch("puzzleeval.agents.agent3f.core.parse_file")
    def test_build_file_message_handles_parse_failure(self, mock_parse):
        from puzzleeval.agents.agent3f.core import _build_file_message
        from puzzleeval.exceptions import AgentFileParseError

        mock_parse.side_effect = AgentFileParseError("File not found")

        blocks = _build_file_message(_make_user_understanding(), ["/tmp/missing.pdf"])

        full_text = " ".join(
            b.get("text", "") for b in blocks if b.get("type") == "text"
        )
        assert "Could not read file" in full_text


# ============================================================================
# Agent Integration Tests (Mocked API)
# ============================================================================

class TestFileTestsAgent:

    def _make_mock_response(self, parsed_output: Agent3Result) -> MagicMock:
        mock_response = MagicMock()
        mock_response.parsed_output = parsed_output
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 5000
        mock_response.usage.output_tokens = 2000
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        return mock_response

    @patch("puzzleeval.agents.agent3f.core.parse_file")
    @patch("puzzleeval.agents.agent3f.core.anthropic.Anthropic")
    def test_valid_input_produces_result(self, mock_anthropic_class, mock_parse):
        expected_result = _make_agent3_result()
        mock_parse.return_value = "Invoice content"

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.parse.return_value = self._make_mock_response(expected_result)

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-3f-001",
            test_file_paths=["/tmp/invoice.pdf"],
        )

        from puzzleeval.agents.agent3f.core import run_file_tests_agent
        result = run_file_tests_agent(input_data)

        assert len(result.test_cases) == 1
        assert result.test_cases[0].test_file_path == "/uploads/invoice_001.pdf"
        from puzzleeval.anthropic_client import (
            AGENT3_GENERATION_MAX_RETRIES,
            AGENT3_GENERATION_TIMEOUT_S,
        )
        client_call = mock_anthropic_class.call_args
        assert client_call.kwargs.get("timeout") == AGENT3_GENERATION_TIMEOUT_S
        assert client_call.kwargs.get("max_retries") == AGENT3_GENERATION_MAX_RETRIES

    @patch("puzzleeval.agents.synthetic_tests.run_synthetic_tests_agent")
    def test_no_files_falls_back_to_text_only_agent3(self, mock_text_agent):
        """Generalized: Agent 3F no longer raises when files are absent.
        It falls back to Agent 3's text-only synthesis so users without
        sample files still get test cases. Downstream test execution
        surfaces 'INCOMPATIBLE: file required' as a real failure when
        the API actually needs a file (Gap 3 fix in implement_test_env)."""
        from puzzleeval.agents.agent3f.core import run_file_tests_agent
        from puzzleeval.schemas import Agent3Result

        sentinel = Agent3Result(
            test_cases=[],
            coverage_summary={},
            generation_notes="text-only fallback",
            generation_duration_ms=10,
            cost_usd=0.0,
        )
        mock_text_agent.return_value = sentinel

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-3f-002",
            test_file_paths=None,
        )
        result = run_file_tests_agent(input_data)
        mock_text_agent.assert_called_once_with(input_data)
        assert result is sentinel

    @patch("puzzleeval.agents.agent3f.core.parse_file")
    @patch("puzzleeval.agents.agent3f.core.anthropic.Anthropic")
    def test_api_error_raises_agent_error(self, mock_anthropic_class, mock_parse):
        import anthropic as anthropic_module
        from puzzleeval.exceptions import AgentAPIError

        mock_parse.return_value = "content"
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.parse.side_effect = anthropic_module.APIConnectionError(
            request=MagicMock()
        )

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-3f-003",
            test_file_paths=["/tmp/test.pdf"],
        )

        from puzzleeval.agents.agent3f.core import run_file_tests_agent

        with pytest.raises(AgentAPIError, match="Failed to connect"):
            run_file_tests_agent(input_data)

    @patch("puzzleeval.agents.agent3f.core.parse_file")
    @patch("puzzleeval.agents.agent3f.core.anthropic.Anthropic")
    def test_none_output_raises_output_error(self, mock_anthropic_class, mock_parse):
        from puzzleeval.exceptions import AgentOutputError

        mock_parse.return_value = "content"
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        mock_response = self._make_mock_response(None)
        mock_response.parsed_output = None
        mock_client.messages.parse.return_value = mock_response

        input_data = Agent3Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-3f-004",
            test_file_paths=["/tmp/test.pdf"],
        )

        from puzzleeval.agents.agent3f.core import run_file_tests_agent

        with pytest.raises(AgentOutputError, match="no parsed output"):
            run_file_tests_agent(input_data)
