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
    WorkflowBlueprint,
    WorkflowStep,
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

    def test_unknown_extension_returns_file_reference(self):
        """Generalized: unknown extensions no longer raise — they return
        a structured file_reference text. Keeps the pipeline domain-agnostic
        so audio/video/archive/binary all flow through one path."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".whatever", delete=False) as f:
            f.write(b"binary data")
            path = f.name
        try:
            result = parse_file(path)
            assert isinstance(result, str)
            assert "path:" in result
            assert path in result
        finally:
            os.unlink(path)

    def test_audio_file_returns_file_reference(self):
        """Audio extension passes through with the right media_type."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"id3 tag bytes")
            path = f.name
        try:
            result = parse_file(path)
            assert "audio/mpeg" in result
            assert path in result
        finally:
            os.unlink(path)

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


# ============================================================================
# Phase 3: WorkflowBlueprint schema
# ============================================================================
# The prompt work that teaches Agent 1 to emit blueprints is covered by
# live behavior in Phase 10 bench runs. These unit tests cover the SCHEMA
# layer — which is where downstream phases plug in — plus backward-compat
# with pre-Phase-3 saved outputs.
# ============================================================================

class TestWorkflowBlueprintSchema:

    def test_single_step_blueprint(self):
        # "I need a chatbot" case — a 1-step blueprint is legitimate.
        bp = WorkflowBlueprint(
            steps=[
                WorkflowStep(
                    id="step_1", role="chatbot",
                    description="Answer customer support questions",
                    capability="customer support chatbot",
                    input_from="user", output_format="free_text",
                    depends_on=[], all_in_one_compatible=True,
                ),
            ],
            architecture_options=["all_in_one"],
            notes="Single-capability request.",
        )
        assert len(bp.steps) == 1
        assert bp.steps[0].depends_on == []
        assert bp.architecture_options == ["all_in_one"]

    def test_two_step_blueprint_depends_on_chain(self):
        # Classic ingestion -> output pattern.
        bp = WorkflowBlueprint(
            steps=[
                WorkflowStep(
                    id="step_1", role="ocr",
                    description="OCR invoice photo",
                    capability="document OCR",
                    input_from="user", output_format="structured_json",
                    depends_on=[],
                ),
                WorkflowStep(
                    id="step_2", role="spreadsheet_sync",
                    description="Append rows to Google Sheets",
                    capability="spreadsheet integration",
                    input_from="step_1", output_format="action",
                    depends_on=["step_1"],
                ),
            ],
        )
        assert bp.steps[1].input_from == "step_1"
        assert bp.steps[1].depends_on == ["step_1"]

    def test_architecture_options_default(self):
        # Default factory should supply both options when none passed.
        bp = WorkflowBlueprint(
            steps=[WorkflowStep(
                id="step_1", role="ocr",
                description="OCR", capability="document OCR",
                input_from="user", output_format="structured_json",
            )],
        )
        assert bp.architecture_options == ["all_in_one", "best_per_step"]

    def test_blueprint_json_round_trip(self):
        bp = WorkflowBlueprint(
            steps=[
                WorkflowStep(
                    id="step_1", role="ocr",
                    description="x", capability="document OCR",
                    input_from="user", output_format="structured_json",
                    depends_on=[],
                ),
                WorkflowStep(
                    id="step_2", role="classify",
                    description="y", capability="classification",
                    input_from="step_1", output_format="classification",
                    depends_on=["step_1"],
                ),
            ],
            notes="round-trip test",
        )
        s = bp.model_dump_json()
        back = WorkflowBlueprint.model_validate_json(s)
        assert len(back.steps) == 2
        assert back.steps[1].depends_on == ["step_1"]
        assert back.notes == "round-trip test"

    # ── DAG expansion: parallel_group + fan-out/fan-in schema support ──

    def test_parallel_group_default_is_none(self):
        # parallel_group is optional and defaults to None for linear steps.
        step = WorkflowStep(
            id="step_1", role="ocr",
            description="OCR", capability="document OCR",
            input_from="user", output_format="structured_json",
        )
        assert step.parallel_group is None

    def test_parallel_group_round_trips(self):
        # Non-null parallel_group must survive JSON serialization so the
        # frontend can read it.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(
                id="step_2a", role="tax_verify",
                description="verify tax ids", capability="tax ID verification",
                input_from="step_1", output_format="structured_json",
                depends_on=["step_1"], parallel_group="enrichment",
            ),
        ])
        s = bp.model_dump_json()
        back = WorkflowBlueprint.model_validate_json(s)
        assert back.steps[0].parallel_group == "enrichment"

    def test_fan_out_fan_in_dag(self):
        # The user-described "OCR AND verify AND classify, then merge"
        # shape: three parallel enrichments after step_1, all feeding step_3.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(
                id="step_1", role="ocr",
                description="OCR invoices", capability="document OCR",
                input_from="user", output_format="structured_json",
                depends_on=[],
            ),
            WorkflowStep(
                id="step_2a", role="tax_verify",
                description="Verify tax IDs", capability="tax ID verification",
                input_from="step_1", output_format="structured_json",
                depends_on=["step_1"], parallel_group="enrichment",
            ),
            WorkflowStep(
                id="step_2b", role="classify",
                description="Categorize expenses", capability="expense classification",
                input_from="step_1", output_format="classification",
                depends_on=["step_1"], parallel_group="enrichment",
            ),
            WorkflowStep(
                id="step_2c", role="line_item_extract",
                description="Extract line items", capability="line item extraction",
                input_from="step_1", output_format="structured_json",
                depends_on=["step_1"], parallel_group="enrichment",
            ),
            WorkflowStep(
                id="step_3", role="bookkeeping_sync",
                description="Sync all to QuickBooks", capability="accounting integration",
                input_from="step_2a", output_format="action",
                depends_on=["step_2a", "step_2b", "step_2c"],
            ),
        ])
        # Shape assertions — the DAG the downstream phases need to see.
        ids = {s.id for s in bp.steps}
        assert ids == {"step_1", "step_2a", "step_2b", "step_2c", "step_3"}
        enrichment = [s for s in bp.steps if s.parallel_group == "enrichment"]
        assert len(enrichment) == 3
        fan_in = next(s for s in bp.steps if s.id == "step_3")
        assert set(fan_in.depends_on) == {"step_2a", "step_2b", "step_2c"}

    def test_two_root_parallel_ingestion(self):
        # Two roots (photo + audio) fan into a single summarize step.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(
                id="step_1a", role="ocr",
                description="OCR receipts", capability="document OCR",
                input_from="user", output_format="structured_json",
                depends_on=[], parallel_group="ingest",
            ),
            WorkflowStep(
                id="step_1b", role="transcribe",
                description="Transcribe audio", capability="audio transcription",
                input_from="user", output_format="free_text",
                depends_on=[], parallel_group="ingest",
            ),
            WorkflowStep(
                id="step_2", role="summarize",
                description="Meeting summary", capability="meeting summarization",
                input_from="step_1a", output_format="free_text",
                depends_on=["step_1a", "step_1b"],
            ),
        ])
        roots = [s for s in bp.steps if not s.depends_on]
        assert len(roots) == 2
        assert all(r.parallel_group == "ingest" for r in roots)
        fan_in = next(s for s in bp.steps if s.id == "step_2")
        assert set(fan_in.depends_on) == {"step_1a", "step_1b"}


class TestUserUnderstandingOutputBackwardCompat:

    def test_output_without_workflow_still_parses(self):
        # Pre-Phase-3 saved artifacts have no `workflow` field. Default=None
        # keeps them parseable; downstream branches on `is not None`.
        uo = UserUnderstandingOutput.model_validate_json(json.dumps({
            "summary": "test",
            "sub_tasks": [{
                "description": "d", "capability": "c", "search_keywords": ["k"],
            }],
            "search_strategy": "both",
            "domain": "d",
            "search_keywords": ["k"],
            "constraints": {},
            "workflow_summary": None,
            # NB: no "workflow" field
        }))
        assert uo.workflow is None  # defaults to None

    def test_output_with_workflow_present(self):
        uo = UserUnderstandingOutput(
            summary="t",
            sub_tasks=[SubTask(description="d", capability="document OCR", search_keywords=["k"])],
            domain="d",
            search_keywords=["k"],
            constraints=Constraints(),
            workflow=WorkflowBlueprint(steps=[
                WorkflowStep(
                    id="step_1", role="ocr",
                    description="OCR docs", capability="document OCR",
                    input_from="user", output_format="structured_json",
                ),
            ]),
        )
        assert uo.workflow is not None
        assert uo.workflow.steps[0].capability == "document OCR"


class TestTestPlanSchema:
    """Test Plan schema from Agent 1 as test director."""

    def test_test_plan_round_trip(self):
        from puzzleeval.schemas import TestPlan, ScopeTestSpec
        plan = TestPlan(
            scope_specs=[
                ScopeTestSpec(
                    scope_id="step_1", test_mode="file_based",
                    input_type="document_content", output_type="structured_json",
                    input_description="Invoice photo", expected_output_description="JSON",
                    sample_input="Invoice data", sample_output='{"vendor": "X"}',
                    test_count_target=8, requires_user_files=True,
                    file_description="Invoice photos",
                    evaluation_focus=["accuracy", "completeness"],
                ),
                ScopeTestSpec(
                    scope_id="step_2", test_mode="synthetic_structured",
                    input_type="structured_data", output_type="action",
                    input_description="JSON from OCR", expected_output_description="Confirmation",
                    sample_input='{"vendor": "X"}', sample_output='{"status": "ok"}',
                    test_count_target=6, requires_user_files=False,
                    upstream_output_shape='{"vendor": "...", "total": 0}',
                    evaluation_focus=["accuracy", "error_handling"],
                ),
            ],
            total_test_target=14,
            notes="OCR needs files; sync uses simulated output.",
        )
        j = plan.model_dump_json()
        back = TestPlan.model_validate_json(j)
        assert len(back.scope_specs) == 2
        assert back.scope_specs[0].scope_id == "step_1"
        assert back.scope_specs[0].test_mode == "file_based"
        assert back.scope_specs[1].upstream_output_shape is not None
        assert back.total_test_target == 14

    def test_test_plan_on_user_understanding(self):
        from puzzleeval.schemas import TestPlan, ScopeTestSpec
        uo = UserUnderstandingOutput(
            summary="test",
            sub_tasks=[SubTask(description="d", capability="c", search_keywords=["k"])],
            domain="d", search_keywords=["k"], constraints=Constraints(),
            test_plan=TestPlan(
                scope_specs=[
                    ScopeTestSpec(
                        scope_id="step_1", test_mode="synthetic_text",
                        input_type="text", output_type="free_text",
                        input_description="chat msg", expected_output_description="response",
                        sample_input="Hello", sample_output="Hi there!",
                    ),
                ],
                total_test_target=7,
            ),
        )
        assert uo.test_plan is not None
        assert uo.test_plan.scope_specs[0].scope_id == "step_1"

    def test_test_plan_default_none(self):
        uo = UserUnderstandingOutput(
            summary="t", sub_tasks=[SubTask(description="d", capability="c", search_keywords=["k"])],
            domain="d", search_keywords=["k"], constraints=Constraints(),
        )
        assert uo.test_plan is None

    def test_test_plan_backward_compat_json(self):
        """Pre-TestPlan saved artifacts parse with test_plan=None."""
        import json
        uo = UserUnderstandingOutput.model_validate_json(json.dumps({
            "summary": "t", "sub_tasks": [{"description": "d", "capability": "c", "search_keywords": ["k"]}],
            "search_strategy": "both", "domain": "d", "search_keywords": ["k"],
            "constraints": {},
        }))
        assert uo.test_plan is None
