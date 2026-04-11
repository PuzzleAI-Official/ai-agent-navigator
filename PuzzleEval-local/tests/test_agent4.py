# ============================================================================
# Tests for Agent 4 (Screening Agent)
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_agent4.py -v
#
# These tests use mocked API calls — no real API key or web searches needed.
# The mock pattern matches test_agent2.py: we mock the Anthropic client and
# control what it returns, then verify Agent 4 handles it correctly.
# ============================================================================

from unittest.mock import MagicMock, patch

import pytest

from puzzleeval.schemas import (
    Agent2Input,
    Agent2Result,
    Agent4Input,
    Agent4Result,
    Candidate,
    Constraints,
    RejectedCandidate,
    ScreenedCandidate,
    SubTask,
    UserUnderstandingOutput,
)


# ============================================================================
# Test Fixtures — Reusable test data
# ============================================================================

def _make_user_understanding() -> UserUnderstandingOutput:
    """Create a sample Agent 1 output for testing Agent 4."""
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
            description="Document processing and OCR service",
            api_available=True,
            api_docs_url="https://cloud.google.com/document-ai/docs/reference/rest",
            pricing_model="per-page",
            pricing_details="$0.01 per page for general OCR",
            claimed_capabilities=["document OCR", "invoice parsing"],
            relevance_score=0.92,
            adoption_difficulty="hard",
            relevant_subtasks=["Extract structured data from invoice photos"],
            source="https://cloud.google.com/document-ai",
        ),
        Candidate(
            name="AWS Textract",
            provider="Amazon Web Services",
            description="ML-based document text and data extraction",
            api_available=True,
            api_docs_url="https://docs.aws.amazon.com/textract/",
            pricing_model="per-page",
            pricing_details="$0.015 per page for text detection",
            claimed_capabilities=["document OCR", "form extraction"],
            relevance_score=0.88,
            adoption_difficulty="hard",
            relevant_subtasks=["Extract structured data from invoice photos"],
            source="https://aws.amazon.com/textract/",
        ),
        Candidate(
            name="Mindee",
            provider="Mindee",
            description="API-first document parsing",
            api_available=True,
            api_docs_url="https://developers.mindee.com/docs",
            pricing_model="freemium",
            pricing_details="Free up to 250 pages/mo",
            claimed_capabilities=["invoice parsing", "receipt scanning"],
            relevance_score=0.85,
            adoption_difficulty="easy",
            relevant_subtasks=["Extract structured data from invoice photos"],
            source="https://mindee.com",
        ),
        Candidate(
            name="EnterpriseOnlyOCR",
            provider="BigCorp",
            description="Enterprise document processing",
            api_available=True,
            api_docs_url=None,
            pricing_model="monthly",
            pricing_details="Custom enterprise pricing",
            claimed_capabilities=["document OCR"],
            relevance_score=0.60,
            adoption_difficulty="hard",
            relevant_subtasks=["Extract structured data from invoice photos"],
            source="training knowledge",
        ),
        Candidate(
            name="Veryfi",
            provider="Veryfi",
            description="Real-time OCR API for receipts and invoices",
            api_available=True,
            api_docs_url="https://veryfi.com/api/",
            pricing_model="usage-based",
            pricing_details="$0.05-0.10 per document",
            claimed_capabilities=["receipt OCR", "invoice parsing"],
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
        search_approach="Searched for 'best invoice OCR APIs 2026'",
        coverage_notes="Document OCR has 5 strong candidates.",
    )


def _make_screened_candidate(name: str = "Google Document AI") -> ScreenedCandidate:
    """Create a fully populated ScreenedCandidate."""
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
        auth_method="api_key",
        api_access_method="free_tier",
        confirmed_capabilities=["document OCR", "invoice parsing", "table extraction"],
        rate_limit_info="1000 requests per minute",
        data_format_notes="Accepts JPEG/PNG/PDF via multipart upload, returns JSON",
        screening_notes="Fetched API docs page. Found REST API reference with endpoints.",
    )


def _make_rejected_candidate(name: str = "EnterpriseOnlyOCR") -> RejectedCandidate:
    """Create a rejected candidate."""
    return RejectedCandidate(
        name=name,
        provider="BigCorp",
        rejection_reason="No public API documentation found. Web search returned only marketing pages.",
        rejection_category="enterprise_only",
    )


def _make_agent4_result() -> Agent4Result:
    """Create a sample Agent4Result — 3 validated + 2 rejected."""
    return Agent4Result(
        validated_candidates=[
            _make_screened_candidate("Google Document AI"),
            _make_screened_candidate("AWS Textract"),
            _make_screened_candidate("Mindee"),
        ],
        rejected_candidates=[
            _make_rejected_candidate("EnterpriseOnlyOCR"),
            RejectedCandidate(
                name="Veryfi",
                provider="Veryfi",
                rejection_reason="API docs page returns 404, web search found no developer docs",
                rejection_category="no_public_docs",
            ),
        ],
        screening_summary="Screened 5 candidates. 3 passed with verified API docs. 2 rejected.",
        total_candidates_screened=5,
    )


# ============================================================================
# Schema Validation Tests
# ============================================================================

class TestSchemas:
    """Test that Agent 4 Pydantic schemas validate correctly."""

    def test_screened_candidate_with_all_fields(self):
        """A fully populated ScreenedCandidate should be valid."""
        sc = _make_screened_candidate()
        assert sc.name == "Google Document AI"
        assert sc.verified_api_docs_url.startswith("https://")
        assert sc.auth_method == "api_key"
        assert sc.api_access_method == "free_tier"
        assert len(sc.confirmed_capabilities) >= 1
        assert sc.data_format_notes != ""

    def test_rejected_candidate_with_all_fields(self):
        """A RejectedCandidate should have name, reason, and category."""
        rc = _make_rejected_candidate()
        assert rc.name == "EnterpriseOnlyOCR"
        assert rc.rejection_reason != ""
        assert rc.rejection_category == "enterprise_only"

    def test_agent4_result_with_mixed_candidates(self):
        """Agent4Result should hold validated + rejected lists."""
        result = _make_agent4_result()
        assert len(result.validated_candidates) == 3
        assert len(result.rejected_candidates) == 2
        assert result.total_candidates_screened == 5
        # All validated should have enrichment fields
        for vc in result.validated_candidates:
            assert vc.verified_api_docs_url
            assert vc.auth_method
            assert vc.data_format_notes

    def test_agent4_input_requires_fields(self):
        """Agent4Input needs candidates, user_understanding, and trace_id."""
        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-trace-screening-001",
        )
        assert input_data.trace_id == "test-trace-screening-001"
        assert len(input_data.candidates.candidates) == 5

    def test_screened_candidate_requires_verified_url(self):
        """verified_api_docs_url is non-optional — must be a string."""
        # This should work with a valid URL
        sc = _make_screened_candidate()
        assert isinstance(sc.verified_api_docs_url, str)

        # Pydantic should reject None for a non-optional str field
        with pytest.raises(Exception):
            ScreenedCandidate(
                name="Test",
                provider="Test",
                description="Test",
                pricing_model="per-page",
                pricing_details=None,
                claimed_capabilities=["test"],
                relevance_score=0.5,
                relevant_subtasks=["test"],
                source="test",
                verified_api_docs_url=None,  # type: ignore  # Should fail
                auth_method="api_key",
                api_access_method="free_tier",
                confirmed_capabilities=["test"],
                rate_limit_info=None,
                data_format_notes="JSON",
                screening_notes="test",
            )


# ============================================================================
# Helper Function Tests
# ============================================================================

class TestHelpers:
    """Test Agent 4 helper functions."""

    def test_build_candidate_message_includes_candidate_info(self):
        """The candidate message should include candidate details and source URL."""
        from puzzleeval.agents.screening import _build_candidate_message

        candidate = _make_sample_candidates()[0]  # Google Document AI
        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-message-001",
        )
        message = _build_candidate_message(candidate, input_data)

        assert "Google Document AI" in message
        assert "Google" in message  # provider
        assert "cloud.google.com" in message  # api_docs_url
        assert "document OCR" in message  # claimed_capabilities
        assert "Source URL" in message  # product website for Strategy 3

    def test_build_candidate_message_includes_subtasks(self):
        """The candidate message should include user sub-tasks for capability matching."""
        from puzzleeval.agents.screening import _build_candidate_message

        candidate = _make_sample_candidates()[0]
        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-message-002",
        )
        message = _build_candidate_message(candidate, input_data)

        assert "Extract structured data from invoice photos" in message
        assert "Create bill entries in QuickBooks" in message

    def test_build_candidate_message_handles_null_api_docs_url(self):
        """Should handle candidates with no api_docs_url gracefully."""
        from puzzleeval.agents.screening import _build_candidate_message

        # EnterpriseOnlyOCR has api_docs_url=None
        candidate = _make_sample_candidates()[3]
        assert candidate.api_docs_url is None

        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-message-003",
        )
        message = _build_candidate_message(candidate, input_data)

        assert "None provided" in message
        assert "EnterpriseOnlyOCR" in message

    def test_extract_text_from_response_mixed_blocks(self):
        """Should extract only text blocks, skipping tool blocks."""
        from puzzleeval.agents.screening import _extract_text_from_response

        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "CANDIDATE: Google Document AI\nDETERMINATION: PASS"

        tool_block = MagicMock()
        tool_block.type = "server_tool_use"

        fetch_result = MagicMock()
        fetch_result.type = "web_fetch_tool_result"

        mock_response.content = [text_block, tool_block, fetch_result]

        result = _extract_text_from_response(mock_response)
        assert "DETERMINATION: PASS" in result
        assert "server_tool_use" not in result


# ============================================================================
# Agent Integration Tests (Mocked API)
# ============================================================================

class TestScreeningAgent:
    """Test the screening agent with mocked API calls."""

    def _make_mock_verify_response(
        self, text_content: str, stop_reason: str = "end_turn",
    ) -> MagicMock:
        """Create a mock response for a per-candidate verification call."""
        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = text_content
        mock_response.content = [text_block]
        mock_response.stop_reason = stop_reason
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 5000
        mock_response.usage.output_tokens = 800
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        mock_response.usage.server_tool_use = MagicMock()
        mock_response.usage.server_tool_use.web_search_requests = 1
        return mock_response

    def _make_mock_structure_response(self, parsed_output: Agent4Result) -> MagicMock:
        """Create a mock response for the structuring call."""
        mock_response = MagicMock()
        mock_response.parsed_output = parsed_output
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 2000
        mock_response.usage.output_tokens = 1500
        mock_response.usage.cache_creation_input_tokens = 0
        mock_response.usage.cache_read_input_tokens = 0
        return mock_response

    @patch("puzzleeval.agents.screening.anthropic.Anthropic")
    def test_valid_input_produces_result(self, mock_anthropic_class):
        """Valid input should produce a valid Agent4Result."""
        expected_result = _make_agent4_result()

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # Per-candidate verification calls (5 candidates)
        verify_responses = [
            self._make_mock_verify_response(
                f"CANDIDATE: {c.name}\nDETERMINATION: PASS\nEVIDENCE: Found API docs"
            )
            for c in _make_sample_candidates()
        ]
        mock_client.messages.create.side_effect = verify_responses

        # Final structuring call
        mock_client.messages.parse.return_value = self._make_mock_structure_response(
            expected_result
        )

        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-screening-001",
        )

        from puzzleeval.agents.screening import run_screening_agent
        result = run_screening_agent(input_data)

        assert len(result.validated_candidates) == 3
        assert len(result.rejected_candidates) == 2
        assert result.total_candidates_screened == 5

        # Verify per-candidate calls were made (5 calls for 5 candidates)
        assert mock_client.messages.create.call_count == 5

        # Verify tools include both web_fetch and web_search
        first_call = mock_client.messages.create.call_args_list[0]
        tools = first_call.kwargs.get("tools", [])
        tool_types = {t["type"] for t in tools}
        assert "web_fetch_20250910" in tool_types
        assert "web_search_20250305" in tool_types

    @patch("puzzleeval.agents.screening.anthropic.Anthropic")
    def test_single_candidate_api_error_doesnt_kill_pipeline(self, mock_anthropic_class):
        """If one candidate's verification fails, others should still proceed."""
        import anthropic as anthropic_module

        expected_result = _make_agent4_result()

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # First candidate fails with API error, rest succeed
        candidates = _make_sample_candidates()
        responses = []
        for i, c in enumerate(candidates):
            if i == 0:
                # First candidate causes API error
                responses.append(
                    anthropic_module.APIConnectionError(request=MagicMock())
                )
            else:
                responses.append(
                    self._make_mock_verify_response(
                        f"CANDIDATE: {c.name}\nDETERMINATION: PASS"
                    )
                )

        mock_client.messages.create.side_effect = responses

        # Structuring still works
        mock_client.messages.parse.return_value = self._make_mock_structure_response(
            expected_result
        )

        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-screening-graceful",
        )

        from puzzleeval.agents.screening import run_screening_agent
        # Should NOT raise — graceful degradation
        result = run_screening_agent(input_data)
        assert result is not None

    @patch("puzzleeval.agents.screening.anthropic.Anthropic")
    def test_structure_step_none_output_raises_error(self, mock_anthropic_class):
        """If structuring returns None, should raise AgentOutputError."""
        from puzzleeval.exceptions import AgentOutputError

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # All verification calls succeed
        candidates = _make_sample_candidates()
        mock_client.messages.create.side_effect = [
            self._make_mock_verify_response(f"CANDIDATE: {c.name}\nDETERMINATION: PASS")
            for c in candidates
        ]

        # Structuring returns None
        mock_structure = self._make_mock_structure_response(None)
        mock_structure.parsed_output = None
        mock_client.messages.parse.return_value = mock_structure

        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-screening-none",
        )

        from puzzleeval.agents.screening import run_screening_agent
        with pytest.raises(AgentOutputError, match="no parsed output"):
            run_screening_agent(input_data)

    @patch("puzzleeval.agents.screening.anthropic.Anthropic")
    def test_pause_turn_handled_per_candidate(self, mock_anthropic_class):
        """pause_turn during per-candidate verification should continue."""
        expected_result = _make_agent4_result()

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        candidates = _make_sample_candidates()

        # First candidate: pause_turn then end_turn. Rest: end_turn directly.
        responses = []
        # Candidate 0: pause then complete
        responses.append(self._make_mock_verify_response(
            "Searching...", stop_reason="pause_turn"
        ))
        responses.append(self._make_mock_verify_response(
            "CANDIDATE: Google Document AI\nDETERMINATION: PASS"
        ))
        # Candidates 1-4: direct completion
        for c in candidates[1:]:
            responses.append(self._make_mock_verify_response(
                f"CANDIDATE: {c.name}\nDETERMINATION: PASS"
            ))

        mock_client.messages.create.side_effect = responses
        mock_client.messages.parse.return_value = self._make_mock_structure_response(
            expected_result
        )

        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-screening-pause",
        )

        from puzzleeval.agents.screening import run_screening_agent
        result = run_screening_agent(input_data)
        assert result is not None

        # 6 create calls: candidate 0 (2 calls: pause + continue) + candidates 1-4 (4 calls)
        assert mock_client.messages.create.call_count == 6

    @patch("puzzleeval.agents.screening.anthropic.Anthropic")
    def test_structure_step_api_error_raises(self, mock_anthropic_class):
        """API error in the structuring step should raise AgentAPIError."""
        import anthropic as anthropic_module
        from puzzleeval.exceptions import AgentAPIError

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        # All verifications succeed
        candidates = _make_sample_candidates()
        mock_client.messages.create.side_effect = [
            self._make_mock_verify_response(f"CANDIDATE: {c.name}\nDETERMINATION: PASS")
            for c in candidates
        ]

        # Structuring fails
        mock_client.messages.parse.side_effect = anthropic_module.APIConnectionError(
            request=MagicMock()
        )

        input_data = Agent4Input(
            candidates=_make_agent2_result(),
            user_understanding=_make_user_understanding(),
            trace_id="test-screening-struct-fail",
        )

        from puzzleeval.agents.screening import run_screening_agent
        with pytest.raises(AgentAPIError, match="Failed to connect"):
            run_screening_agent(input_data)

    def test_tool_configuration(self):
        """Verify web_fetch and web_search tool configs are correct."""
        from puzzleeval.agents.screening import WEB_FETCH_TOOL, WEB_SEARCH_TOOL

        assert WEB_FETCH_TOOL["type"] == "web_fetch_20250910"
        assert WEB_FETCH_TOOL["max_uses"] == 3  # docs page + homepage + follow link
        assert WEB_SEARCH_TOOL["type"] == "web_search_20250305"
        assert WEB_SEARCH_TOOL["max_uses"] == 3  # standard + capability + site-scoped


# ============================================================================
# Validator Tests
# ============================================================================

class TestValidator:
    """Test Agent 4 output validation."""

    def test_valid_result_passes(self):
        """A well-formed Agent4Result should pass validation."""
        from puzzleeval.validators import validate_agent4_output

        result = _make_agent4_result()
        agent2_output = _make_agent2_result()
        agent1_output = _make_user_understanding()

        validation = validate_agent4_output(result, agent2_output, agent1_output)
        assert validation.passed is True
        assert len(validation.errors) == 0

    def test_too_few_validated_is_error(self):
        """Fewer than 2 validated candidates should be an error."""
        from puzzleeval.validators import validate_agent4_output

        result = Agent4Result(
            validated_candidates=[_make_screened_candidate()],
            rejected_candidates=[
                _make_rejected_candidate("EnterpriseOnlyOCR"),
                _make_rejected_candidate("Veryfi"),
            ],
            screening_summary="Only 1 passed.",
            total_candidates_screened=3,
        )

        validation = validate_agent4_output(
            result, _make_agent2_result(), _make_user_understanding(),
        )
        assert validation.passed is False
        assert any("at least 2" in e for e in validation.errors)

    def test_empty_verified_url_is_error(self):
        """Validated candidate with empty verified_api_docs_url should error."""
        from puzzleeval.validators import validate_agent4_output

        bad_candidate = _make_screened_candidate("BadCandidate")
        bad_candidate = ScreenedCandidate(
            **{**bad_candidate.model_dump(), "verified_api_docs_url": ""}
        )

        result = Agent4Result(
            validated_candidates=[
                _make_screened_candidate("Google Document AI"),
                _make_screened_candidate("AWS Textract"),
                bad_candidate,
            ],
            rejected_candidates=[_make_rejected_candidate()],
            screening_summary="3 passed, 1 rejected.",
            total_candidates_screened=4,
        )

        validation = validate_agent4_output(
            result, _make_agent2_result(), _make_user_understanding(),
        )
        assert validation.passed is False
        assert any("verified_api_docs_url" in e for e in validation.errors)

    def test_missing_candidate_warns(self):
        """Agent 2 candidate missing from both lists should warn."""
        from puzzleeval.validators import validate_agent4_output

        # Agent2 has 5 candidates, but Agent4 only accounts for 3
        result = Agent4Result(
            validated_candidates=[
                _make_screened_candidate("Google Document AI"),
                _make_screened_candidate("AWS Textract"),
                _make_screened_candidate("Mindee"),
            ],
            rejected_candidates=[],
            screening_summary="3 passed.",
            total_candidates_screened=3,
        )

        validation = validate_agent4_output(
            result, _make_agent2_result(), _make_user_understanding(),
        )
        # Should pass (not an error) but have a warning about missing candidates
        assert any("silently dropped" in w for w in validation.warnings)

    def test_count_mismatch_is_error(self):
        """total_candidates_screened must equal validated + rejected."""
        from puzzleeval.validators import validate_agent4_output

        result = Agent4Result(
            validated_candidates=[
                _make_screened_candidate("Google Document AI"),
                _make_screened_candidate("AWS Textract"),
                _make_screened_candidate("Mindee"),
            ],
            rejected_candidates=[_make_rejected_candidate()],
            screening_summary="Screened candidates.",
            total_candidates_screened=10,  # Wrong! Should be 4
        )

        validation = validate_agent4_output(
            result, _make_agent2_result(), _make_user_understanding(),
        )
        assert validation.passed is False
        assert any("Count mismatch" in e for e in validation.errors)

    def test_invalid_rejection_category_is_error(self):
        """Invalid rejection_category should be caught."""
        from puzzleeval.validators import validate_agent4_output

        result = Agent4Result(
            validated_candidates=[
                _make_screened_candidate("Google Document AI"),
                _make_screened_candidate("AWS Textract"),
                _make_screened_candidate("Mindee"),
            ],
            rejected_candidates=[
                RejectedCandidate(
                    name="BadService",
                    provider="BadCorp",
                    rejection_reason="Not good enough",
                    rejection_category="invalid_category",  # Not in allowed set
                ),
            ],
            screening_summary="Screened candidates.",
            total_candidates_screened=4,
        )

        validation = validate_agent4_output(
            result, _make_agent2_result(), _make_user_understanding(),
        )
        assert validation.passed is False
        assert any("invalid rejection_category" in e for e in validation.errors)

    def test_paid_only_access_warns(self):
        """api_access_method='paid_only' should produce a warning."""
        from puzzleeval.validators import validate_agent4_output

        paid_candidate = ScreenedCandidate(
            **{**_make_screened_candidate("PaidService").model_dump(),
               "api_access_method": "paid_only"},
        )

        result = Agent4Result(
            validated_candidates=[
                _make_screened_candidate("Google Document AI"),
                _make_screened_candidate("AWS Textract"),
                paid_candidate,
            ],
            rejected_candidates=[_make_rejected_candidate()],
            screening_summary="Screened candidates.",
            total_candidates_screened=4,
        )

        validation = validate_agent4_output(
            result, _make_agent2_result(), _make_user_understanding(),
        )
        assert any("paid access" in w for w in validation.warnings)

    def test_unknown_auth_warns(self):
        """auth_method='unknown' should produce a warning."""
        from puzzleeval.validators import validate_agent4_output

        unknown_auth_candidate = ScreenedCandidate(
            **{**_make_screened_candidate("UnknownAuth").model_dump(),
               "auth_method": "unknown"},
        )

        result = Agent4Result(
            validated_candidates=[
                _make_screened_candidate("Google Document AI"),
                _make_screened_candidate("AWS Textract"),
                unknown_auth_candidate,
            ],
            rejected_candidates=[_make_rejected_candidate()],
            screening_summary="Screened candidates.",
            total_candidates_screened=4,
        )

        validation = validate_agent4_output(
            result, _make_agent2_result(), _make_user_understanding(),
        )
        assert any("unknown" in w and "auth" in w for w in validation.warnings)
