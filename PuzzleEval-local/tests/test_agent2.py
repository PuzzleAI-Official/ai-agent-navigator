# ============================================================================
# Tests for Agent 2 (Research Agent)
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_agent2.py -v
#
# These tests use mocked API calls — no real API key or web searches needed.
# The mock pattern matches test_agent1.py: we mock the Anthropic client and
# control what it returns, then verify Agent 2 handles it correctly.
# ============================================================================

from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.schemas import (
    Agent2Input,
    Agent2Result,
    Candidate,
    Constraints,
    SubTask,
    UserUnderstandingOutput,
)


# ============================================================================
# Test Fixtures — Reusable test data
# ============================================================================

def _make_user_understanding() -> UserUnderstandingOutput:
    """Create a sample Agent 1 output for testing Agent 2."""
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


def _make_sample_candidates() -> list[Candidate]:
    """Create sample candidates for testing."""
    return [
        Candidate(
            name="Google Document AI",
            provider="Google",
            description="Document processing and OCR service with pre-trained models for invoices",
            api_available=True,
            api_docs_url="https://cloud.google.com/document-ai/docs/reference/rest",
            pricing_model="per-page",
            pricing_details="$0.01 per page for general OCR, $0.10 for specialized invoice parsing",
            claimed_capabilities=["document OCR", "invoice parsing", "table extraction"],
            relevance_score=0.92,
            adoption_difficulty="hard",
            relevant_subtasks=["Extract structured data from invoice photos"],
            source="https://cloud.google.com/document-ai",
        ),
        Candidate(
            name="AWS Textract",
            provider="Amazon Web Services",
            description="ML-based document text and data extraction service",
            api_available=True,
            api_docs_url="https://docs.aws.amazon.com/textract/latest/dg/API_Reference.html",
            pricing_model="per-page",
            pricing_details="$0.015 per page for text detection, $0.065 for invoice/receipt analysis",
            claimed_capabilities=["document OCR", "form extraction", "invoice analysis"],
            relevance_score=0.88,
            adoption_difficulty="hard",
            relevant_subtasks=["Extract structured data from invoice photos"],
            source="https://aws.amazon.com/textract/",
        ),
        Candidate(
            name="Mindee",
            provider="Mindee",
            description="API-first document parsing with specialized invoice and receipt models",
            api_available=True,
            api_docs_url="https://developers.mindee.com/docs",
            pricing_model="freemium",
            pricing_details="Free up to 250 pages/mo, then $0.02/page",
            claimed_capabilities=["invoice parsing", "receipt scanning", "document OCR"],
            relevance_score=0.85,
            adoption_difficulty="easy",
            relevant_subtasks=["Extract structured data from invoice photos"],
            source="https://mindee.com",
        ),
        Candidate(
            name="Rossum",
            provider="Rossum",
            description="AI-powered document understanding platform specializing in invoices",
            api_available=True,
            api_docs_url="https://api.elis.rossum.ai/docs",
            pricing_model="monthly",
            pricing_details="Custom pricing, free trial available",
            claimed_capabilities=["invoice processing", "data extraction", "validation"],
            relevance_score=0.78,
            adoption_difficulty="easy",
            relevant_subtasks=[
                "Extract structured data from invoice photos",
                "Create bill entries in QuickBooks from structured data",
            ],
            source="https://rossum.ai",
        ),
        Candidate(
            name="Veryfi",
            provider="Veryfi",
            description="Real-time OCR API for receipts, invoices, and financial documents",
            api_available=True,
            api_docs_url="https://veryfi.com/api/",
            pricing_model="usage-based",
            pricing_details="$0.05-0.10 per document depending on plan",
            claimed_capabilities=["receipt OCR", "invoice parsing", "expense categorization"],
            relevance_score=0.82,
            adoption_difficulty="easy",
            relevant_subtasks=["Extract structured data from invoice photos"],
            source="https://veryfi.com",
        ),
    ]


def _make_agent2_result() -> Agent2Result:
    """Create a sample Agent2Result for testing."""
    return Agent2Result(
        candidates=_make_sample_candidates(),
        search_approach=(
            "Searched for 'best invoice OCR APIs 2026', 'document data extraction API comparison', "
            "and 'QuickBooks automation AI'. Fetched API docs for 7 candidates, validated 5."
        ),
        coverage_notes=(
            "Document OCR has 5 strong candidates. QuickBooks integration has fewer direct options "
            "— most candidates handle OCR but not the accounting entry step. May need API glue "
            "between an OCR tool and QuickBooks API directly."
        ),
    )


# ============================================================================
# Schema Validation Tests
# ============================================================================

class TestSchemas:
    """Test that Agent 2 Pydantic schemas validate correctly."""

    def test_candidate_with_all_fields(self):
        """A fully populated candidate should be valid."""
        candidate = _make_sample_candidates()[0]
        assert candidate.name == "Google Document AI"
        assert candidate.api_available is True
        assert 0.0 <= candidate.relevance_score <= 1.0
        assert len(candidate.claimed_capabilities) >= 1

    def test_candidate_with_null_optional_fields(self):
        """Candidate with null optional fields should be valid."""
        candidate = Candidate(
            name="SomeService",
            provider="SomeCompany",
            description="Does stuff",
            api_available=True,
            api_docs_url=None,
            pricing_model="usage-based",
            pricing_details=None,
            claimed_capabilities=["capability1"],
            relevance_score=0.5,
            adoption_difficulty="easy",
            relevant_subtasks=["some sub-task"],
            source="training knowledge",
        )
        assert candidate.api_docs_url is None
        assert candidate.pricing_details is None

    def test_agent2_result_with_candidates(self):
        """Agent2Result should hold a list of candidates."""
        result = _make_agent2_result()
        assert len(result.candidates) == 5
        assert all(c.api_available for c in result.candidates)
        assert "OCR" in result.coverage_notes

    def test_agent2_input_requires_fields(self):
        """Agent2Input needs user_understanding and trace_id."""
        input_data = Agent2Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-trace-789",
        )
        assert input_data.trace_id == "test-trace-789"
        assert len(input_data.user_understanding.sub_tasks) == 2

    def test_relevance_score_bounds(self):
        """Relevance scores from our fixtures should be in 0.0-1.0 range."""
        candidates = _make_sample_candidates()
        for c in candidates:
            assert 0.0 <= c.relevance_score <= 1.0, (
                f"{c.name} has out-of-range relevance_score: {c.relevance_score}"
            )


# ============================================================================
# Helper Function Tests
# ============================================================================

class TestHelpers:
    """Test Agent 2 helper functions."""

    def test_build_research_message_includes_subtasks(self):
        """The research message should include all sub-tasks and keywords."""
        from puzzleeval.agents.research import _build_research_message

        user_understanding = _make_user_understanding()
        message = _build_research_message(user_understanding)

        # Should contain the summary
        assert "extract data from invoices" in message.lower()
        # Should contain sub-task descriptions
        assert "Extract structured data from invoice photos" in message
        assert "Create bill entries in QuickBooks" in message
        # Should contain search keywords
        assert "invoice OCR API" in message
        assert "QuickBooks API automation" in message
        # Should contain domain
        assert "accounting" in message
        # Should contain constraints
        assert "$50-200/mo" in message
        assert "non-technical" in message
        assert "QuickBooks" in message

    def test_build_research_message_handles_missing_constraints(self):
        """Should handle missing optional constraints gracefully."""
        from puzzleeval.agents.research import _build_research_message

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
            constraints=Constraints(),  # All defaults — nothing specified
            workflow_summary=None,
        )
        message = _build_research_message(user_understanding)

        assert "Not specified" in message  # Budget, tech level
        assert "None specified" in message  # Integration requirements

    def test_extract_text_from_response_text_only(self):
        """Should extract text from a response with only text blocks."""
        from puzzleeval.agents.research import _extract_text_from_response

        mock_response = MagicMock()
        block1 = MagicMock()
        block1.type = "text"
        block1.text = "Found 5 candidates."
        block2 = MagicMock()
        block2.type = "text"
        block2.text = "Google Document AI is the best fit."
        mock_response.content = [block1, block2]

        result = _extract_text_from_response(mock_response)
        assert "Found 5 candidates." in result
        assert "Google Document AI" in result

    def test_extract_text_from_response_mixed_blocks(self):
        """Should skip non-text blocks (tool_use, tool_result, etc.)."""
        from puzzleeval.agents.research import _extract_text_from_response

        mock_response = MagicMock()

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Here are my findings."

        tool_use_block = MagicMock()
        tool_use_block.type = "server_tool_use"

        tool_result_block = MagicMock()
        tool_result_block.type = "web_search_tool_result"

        text_block2 = MagicMock()
        text_block2.type = "text"
        text_block2.text = "Candidate 1: Google Document AI"

        mock_response.content = [text_block, tool_use_block, tool_result_block, text_block2]

        result = _extract_text_from_response(mock_response)
        assert "Here are my findings." in result
        assert "Candidate 1: Google Document AI" in result
        # Tool blocks should not appear in text
        assert "server_tool_use" not in result

    def test_extract_text_from_response_empty(self):
        """Should return empty string when no text blocks exist."""
        from puzzleeval.agents.research import _extract_text_from_response

        mock_response = MagicMock()
        tool_block = MagicMock()
        tool_block.type = "server_tool_use"
        mock_response.content = [tool_block]

        result = _extract_text_from_response(mock_response)
        assert result.strip() == ""


# ============================================================================
# Agent Integration Tests (Mocked API)
# ============================================================================

class TestResearchAgent:
    """Test the research agent with mocked API calls."""

    def _make_mock_research_response(
        self, text_content: str, stop_reason: str = "end_turn",
    ) -> MagicMock:
        """Create a mock response for Step 1 (web research)."""
        mock_response = MagicMock()

        # Create text content block
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = text_content
        mock_response.content = [text_block]

        mock_response.stop_reason = stop_reason
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 3000
        mock_response.usage.output_tokens = 2000
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0

        # Server tool usage metrics
        mock_response.usage.server_tool_use = MagicMock()
        mock_response.usage.server_tool_use.web_search_requests = 2
        return mock_response

    def _make_mock_structure_response(self, parsed_output: Agent2Result) -> MagicMock:
        """Create a mock response for Step 2 (structuring)."""
        mock_response = MagicMock()
        mock_response.parsed_output = parsed_output
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 800
        mock_response.usage.output_tokens = 1200
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        return mock_response

    @patch("puzzleeval.agents.research.anthropic.Anthropic")
    def test_valid_input_produces_result(self, mock_anthropic_class):
        """Valid input should produce a valid Agent2Result."""
        expected_result = _make_agent2_result()

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # Step 1: messages.create() returns research findings
        mock_client.messages.create.return_value = self._make_mock_research_response(
            "Found 5 candidates: Google Document AI, AWS Textract, Mindee, Rossum, Veryfi..."
        )
        # Step 2: messages.parse() returns structured result
        mock_client.messages.parse.return_value = self._make_mock_structure_response(
            expected_result
        )

        input_data = Agent2Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-research-001",
        )

        from puzzleeval.agents.research import run_research_agent
        result = run_research_agent(input_data)

        assert len(result.candidates) == 5
        assert result.candidates[0].name == "Google Document AI"
        assert all(c.api_available for c in result.candidates)

        # Verify Step 1 was called with web search only (no fetch)
        create_call = mock_client.messages.create.call_args
        tools = create_call.kwargs.get("tools", [])
        tool_types = [t["type"] for t in tools]
        assert "web_search_20250305" in tool_types
        assert len(tools) == 1  # Only web search, no fetch

    @patch("puzzleeval.agents.research.anthropic.Anthropic")
    def test_step1_api_error_raises_agent_error(self, mock_anthropic_class):
        """API error in Step 1 should raise AgentAPIError."""
        import anthropic as anthropic_module
        from puzzleeval.exceptions import AgentAPIError

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # Step 1 raises an API error
        mock_client.messages.create.side_effect = anthropic_module.APIConnectionError(
            request=MagicMock()
        )

        input_data = Agent2Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-research-002",
        )

        from puzzleeval.agents.research import run_research_agent

        with pytest.raises(AgentAPIError, match="Failed to connect"):
            run_research_agent(input_data)

    @patch("puzzleeval.agents.research.anthropic.Anthropic")
    def test_step2_none_output_raises_output_error(self, mock_anthropic_class):
        """If Step 2 returns None parsed output, should raise AgentOutputError."""
        from puzzleeval.exceptions import AgentOutputError

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # Step 1 succeeds
        mock_client.messages.create.return_value = self._make_mock_research_response(
            "Found candidates: Google Document AI, AWS Textract..."
        )

        # Step 2 returns None parsed output
        mock_structure = self._make_mock_structure_response(None)
        mock_structure.parsed_output = None
        mock_client.messages.parse.return_value = mock_structure

        input_data = Agent2Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-research-003",
        )

        from puzzleeval.agents.research import run_research_agent

        with pytest.raises(AgentOutputError, match="no parsed output"):
            run_research_agent(input_data)

    @patch("puzzleeval.agents.research.anthropic.Anthropic")
    def test_pause_turn_continues_then_finishes(self, mock_anthropic_class):
        """If Step 1 returns pause_turn, should continue and then finish."""
        expected_result = _make_agent2_result()

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # Step 1: first call returns pause_turn, second call returns end_turn
        pause_response = self._make_mock_research_response(
            "Searching for candidates...", stop_reason="pause_turn"
        )
        final_response = self._make_mock_research_response(
            "Found 5 candidates: Google Document AI, AWS Textract..."
        )
        mock_client.messages.create.side_effect = [pause_response, final_response]

        # Step 2: returns structured result
        mock_client.messages.parse.return_value = self._make_mock_structure_response(
            expected_result
        )

        input_data = Agent2Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-research-pause",
        )

        from puzzleeval.agents.research import run_research_agent
        result = run_research_agent(input_data)

        assert len(result.candidates) == 5
        # Verify messages.create was called twice (initial + 1 continuation)
        assert mock_client.messages.create.call_count == 2

    @patch("puzzleeval.agents.research.anthropic.Anthropic")
    def test_empty_research_raises_output_error(self, mock_anthropic_class):
        """If Step 1 returns no text content, should raise AgentOutputError."""
        from puzzleeval.exceptions import AgentOutputError

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # Step 1 returns response with only tool blocks, no text
        mock_response = MagicMock()
        tool_block = MagicMock()
        tool_block.type = "server_tool_use"
        mock_response.content = [tool_block]
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 1000
        mock_response.usage.output_tokens = 0
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        mock_response.usage.server_tool_use = None
        mock_client.messages.create.return_value = mock_response

        input_data = Agent2Input(
            user_understanding=_make_user_understanding(),
            trace_id="test-research-004",
        )

        from puzzleeval.agents.research import run_research_agent

        with pytest.raises(AgentOutputError, match="no text findings"):
            run_research_agent(input_data)
