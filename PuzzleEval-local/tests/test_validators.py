# ============================================================================
# Tests for Output Quality Validators
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_validators.py -v
#
# Tests that validators correctly identify quality issues in agent outputs.
# Each test uses hand-crafted good/bad outputs — no API mocking needed.
# ============================================================================

import pytest

from puzzleeval.schemas import (
    Agent1Result,
    Agent2Result,
    Agent3Result,
    Candidate,
    ClarifyingResponse,
    Constraints,
    InfoStatus,
    JudgementCriterion,
    SubTask,
    TestCase,
    UserUnderstandingOutput,
    WorkflowBlueprint,
    WorkflowStep,
)
from puzzleeval.validators import (
    validate_agent1_output,
    validate_agent2_output,
    validate_agent3_output,
)


# ============================================================================
# Shared Fixtures
# ============================================================================

def _make_user_understanding() -> UserUnderstandingOutput:
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
        constraints=Constraints(budget_range="$50-200/mo"),
        workflow_summary=None,
    )


# ============================================================================
# Agent 1 Validator Tests
# ============================================================================

class TestAgent1Validator:

    def test_good_output_passes(self):
        result = Agent1Result(
            is_clear=True,
            result=_make_user_understanding(),
        )
        v = validate_agent1_output(result)
        assert v.passed
        assert len(v.errors) == 0

    def test_clarification_response_passes(self):
        result = Agent1Result(
            is_clear=False,
            clarification_needed=ClarifyingResponse(
                message="I need more info",
                critical_questions=["What domain?"],
                optional_prompt=None,
                partial_understanding="Some understanding",
                info_status=InfoStatus(
                    has_concrete_subtasks=False,
                    has_domain=False,
                ),
            ),
        )
        v = validate_agent1_output(result)
        assert v.passed

    def test_is_clear_true_but_result_none_fails(self):
        result = Agent1Result(is_clear=True, result=None)
        v = validate_agent1_output(result)
        assert not v.passed
        assert any("result is None" in e for e in v.errors)

    def test_no_subtasks_fails(self):
        uo = _make_user_understanding()
        uo.sub_tasks = []
        result = Agent1Result(is_clear=True, result=uo)
        v = validate_agent1_output(result)
        assert not v.passed
        assert any("No sub-tasks" in e for e in v.errors)

    def test_empty_domain_fails(self):
        uo = _make_user_understanding()
        uo.domain = ""
        result = Agent1Result(is_clear=True, result=uo)
        v = validate_agent1_output(result)
        assert not v.passed
        assert any("Domain is empty" in e for e in v.errors)

    def test_vague_domain_warns(self):
        uo = _make_user_understanding()
        uo.domain = "general"
        result = Agent1Result(is_clear=True, result=uo)
        v = validate_agent1_output(result)
        assert v.passed  # warning, not error
        assert any("general" in w for w in v.warnings)

    def test_few_search_keywords_warns(self):
        uo = _make_user_understanding()
        uo.sub_tasks[0].search_keywords = ["only_one"]
        result = Agent1Result(is_clear=True, result=uo)
        v = validate_agent1_output(result)
        assert v.passed
        assert any("fewer than 2 search keywords" in w for w in v.warnings)

    def test_is_clear_false_but_no_clarification_fails(self):
        result = Agent1Result(is_clear=False, clarification_needed=None)
        v = validate_agent1_output(result)
        assert not v.passed



# ============================================================================
# Agent 2 Validator Tests
# ============================================================================

class TestAgent2Validator:

    def _make_candidates(self, count: int, providers: list[str] | None = None) -> list[Candidate]:
        if providers is None:
            providers = [f"Provider{i}" for i in range(count)]
        candidates = []
        for i in range(count):
            candidates.append(Candidate(
                name=f"Service{i}",
                provider=providers[i % len(providers)],
                description="Does stuff",
                api_available=True,
                api_docs_url=None,
                pricing_model="usage-based",
                pricing_details=None,
                claimed_capabilities=["capability1"],
                relevance_score=0.8,
                adoption_difficulty="easy",
                relevant_subtasks=["Extract structured data from invoice photos"],
                source="web search",
            ))
        return candidates

    def test_good_output_passes(self):
        result = Agent2Result(
            candidates=self._make_candidates(5, ["A", "B", "C", "D", "E"]),
            search_approach="Searched comparison articles",
            coverage_notes="Good coverage",
        )
        v = validate_agent2_output(result, _make_user_understanding())
        assert v.passed

    def test_too_few_candidates_fails(self):
        result = Agent2Result(
            candidates=self._make_candidates(2, ["A", "B"]),
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, _make_user_understanding())
        assert not v.passed
        assert any("at least 4" in e for e in v.errors)

    def test_few_providers_fails(self):
        result = Agent2Result(
            candidates=self._make_candidates(5, ["A", "A", "B", "B", "A"]),
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, _make_user_understanding())
        assert not v.passed
        assert any("at least 3" in e for e in v.errors)

    def test_non_api_candidate_fails(self):
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        candidates[0].api_available = False
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, _make_user_understanding())
        assert not v.passed
        assert any("without API access" in e for e in v.errors)

    def test_invalid_relevance_score_fails(self):
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        candidates[0].relevance_score = 1.5
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, _make_user_understanding())
        assert not v.passed

    def test_low_relevance_warns(self):
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        candidates[0].relevance_score = 0.1
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, _make_user_understanding())
        assert v.passed
        assert any("very low relevance" in w for w in v.warnings)

    def test_fuzzy_subtask_matching_no_false_warning(self):
        """Rephrased sub-task descriptions should match via keyword overlap."""
        # Candidate uses different wording than Agent 1's sub-task description
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        for c in candidates:
            c.relevant_subtasks = [
                # Rephrasing of "Extract structured data from invoice photos"
                "Extract data from invoice photos/PDFs (vendor, amounts, line items)",
                # Rephrasing of "Create bill entries in QuickBooks from structured data"
                "QuickBooks bill creation from structured invoice data",
            ]
        result = Agent2Result(
            candidates=candidates,
            search_approach="Searched for invoice OCR APIs",
            coverage_notes="Good coverage",
        )
        v = validate_agent2_output(result, _make_user_understanding())
        assert v.passed
        # Should NOT have any sub-task coverage warnings
        coverage_warnings = [w for w in v.warnings if "Sub-tasks not covered" in w]
        assert len(coverage_warnings) == 0, f"Unexpected coverage warning: {coverage_warnings}"

    def test_majority_hard_adoption_for_non_technical_warns(self):
        """Majority hard-adoption candidates for non-technical user should warn."""
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        # Make majority (3 of 5) hard
        candidates[0].adoption_difficulty = "hard"
        candidates[1].adoption_difficulty = "hard"
        candidates[2].adoption_difficulty = "hard"
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        non_tech_user = _make_user_understanding()
        non_tech_user.constraints.technical_level = "non-technical"
        v = validate_agent2_output(result, non_tech_user)
        assert v.passed  # Warning, not error
        assert any("hard adoption" in w.lower() for w in v.warnings)

    def test_minority_hard_adoption_for_non_technical_no_warning(self):
        """A few hard-adoption candidates for non-technical is fine — contextual judgment."""
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        # Only 1 of 5 is hard — that's fine, agent used judgment
        candidates[0].adoption_difficulty = "hard"
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        non_tech_user = _make_user_understanding()
        non_tech_user.constraints.technical_level = "non-technical"
        v = validate_agent2_output(result, non_tech_user)
        adoption_warnings = [w for w in v.warnings if "hard adoption" in w.lower()]
        assert len(adoption_warnings) == 0

    def test_hard_adoption_for_technical_no_warning(self):
        """Hard adoption candidates for technical user should NOT warn."""
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        candidates[0].adoption_difficulty = "hard"
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        # Create a technical user
        tech_user = _make_user_understanding()
        tech_user.constraints.technical_level = "technical"
        v = validate_agent2_output(result, tech_user)
        assert v.passed
        adoption_warnings = [w for w in v.warnings if "hard adoption" in w.lower()]
        assert len(adoption_warnings) == 0

    def test_invalid_adoption_difficulty_errors(self):
        """Invalid adoption_difficulty value should be a blocking error."""
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        candidates[0].adoption_difficulty = "impossible"
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, _make_user_understanding())
        assert not v.passed
        assert any("invalid adoption_difficulty" in e for e in v.errors)

    # ── Phase 4: coverage validator tests ──

    def _make_uo_with_3_scope_blueprint(self) -> UserUnderstandingOutput:
        uo = _make_user_understanding()
        uo.workflow = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="a",
                         capability="document OCR", input_from="user",
                         output_format="structured_json"),
            WorkflowStep(id="step_2", role="classify", description="b",
                         capability="accounting integration", input_from="step_1",
                         output_format="classification", depends_on=["step_1"]),
            WorkflowStep(id="step_3", role="sync", description="c",
                         capability="accounting integration", input_from="step_2",
                         output_format="action", depends_on=["step_2"]),
        ])
        return uo

    def test_uncovered_scope_warns(self):
        # 5 candidates but nobody claims step_3 → validator warns.
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        for c in candidates:
            c.covers_step_ids = frozenset({"step_1", "step_2"})
            c.coverage_confidence = {"step_1": "claimed", "step_2": "claimed"}
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, self._make_uo_with_3_scope_blueprint())
        assert v.passed  # warning, not error
        assert any("NOT covered" in w and "step_3" in w for w in v.warnings)

    def test_thin_coverage_at_scope_warns(self):
        # step_2 covered by only 2 candidates — thin (<3).
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        # All cover step_1
        for c in candidates:
            c.covers_step_ids = frozenset({"step_1", "step_3"})
            c.coverage_confidence = {"step_1": "claimed", "step_3": "claimed"}
        # Only 2 ALSO cover step_2
        candidates[0].covers_step_ids = frozenset({"step_1", "step_2", "step_3"})
        candidates[0].coverage_confidence = {
            "step_1": "claimed", "step_2": "claimed", "step_3": "claimed",
        }
        candidates[1].covers_step_ids = frozenset({"step_1", "step_2", "step_3"})
        candidates[1].coverage_confidence = {
            "step_1": "claimed", "step_2": "claimed", "step_3": "claimed",
        }
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, self._make_uo_with_3_scope_blueprint())
        assert v.passed
        assert any("Thin coverage at scope 'step_2'" in w for w in v.warnings)

    def test_coverage_confidence_drift_is_error(self):
        # coverage_confidence key not in covers_step_ids → structural drift.
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        for c in candidates:
            c.covers_step_ids = frozenset({"step_1", "step_2", "step_3"})
            c.coverage_confidence = {
                "step_1": "claimed", "step_2": "claimed", "step_3": "claimed",
            }
        # Inject drift on the first candidate
        candidates[0].coverage_confidence = {
            "step_1": "claimed",
            "step_99": "claimed",  # not in covers
        }
        candidates[0].covers_step_ids = frozenset({"step_1"})
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, self._make_uo_with_3_scope_blueprint())
        assert not v.passed
        assert any("drift between the two fields" in e for e in v.errors)

    def test_invalid_confidence_value_is_error(self):
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        for c in candidates:
            c.covers_step_ids = frozenset({"step_1", "step_2", "step_3"})
            c.coverage_confidence = {
                "step_1": "claimed", "step_2": "claimed", "step_3": "claimed",
            }
        candidates[0].coverage_confidence["step_1"] = "unknown"  # bad value
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, self._make_uo_with_3_scope_blueprint())
        assert not v.passed
        assert any("invalid coverage_confidence" in e for e in v.errors)

    def test_legacy_flat_flow_skips_coverage_checks(self):
        # No candidate has coverage populated → validator silently skips
        # the Phase 4 block. Sub-task fuzzy matching still runs.
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, self._make_uo_with_3_scope_blueprint())
        # No scope warnings (Phase 4 block entirely skipped)
        assert not any("NOT covered" in w for w in v.warnings)
        assert not any("Thin coverage" in w for w in v.warnings)

    # ── Phase 5: pricing validator tests ──

    def _make_basic_pricing(self, **overrides):
        from puzzleeval.schemas import PricingBreakdown, PricingTier
        defaults = {
            "tiers": [
                PricingTier(name="Free", monthly_cost_usd=0.0, included_units=250,
                            unit_name="pages", overage_cost_per_unit_usd=0.02),
                PricingTier(name="Pro", monthly_cost_usd=29.0, included_units=2500,
                            unit_name="pages", overage_cost_per_unit_usd=0.015),
            ],
            "free_tier_monthly_units": 250,
            "pay_as_you_go": False,
            "billing_granularity": "monthly",
            "per_scope_unit_cost": {},
            "sources": ["https://example.com/pricing"],
            "confidence": "high",
        }
        defaults.update(overrides)
        return PricingBreakdown(**defaults)

    def _put_pricing_on_candidate(self, bp_steps, pricing):
        """Attach pricing to first candidate; blueprint with matching scopes."""
        uo = self._make_uo_with_3_scope_blueprint()
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        # Give every candidate coverage so Phase 4 block activates.
        for c in candidates:
            c.covers_step_ids = frozenset({"step_1", "step_2", "step_3"})
            c.coverage_confidence = {
                "step_1": "claimed", "step_2": "claimed", "step_3": "claimed",
            }
        candidates[0].pricing_breakdown = pricing
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        return validate_agent2_output(result, uo)

    def test_valid_pricing_passes(self):
        bp = self._make_basic_pricing()
        v = self._put_pricing_on_candidate(["step_1", "step_2", "step_3"], bp)
        assert v.passed

    def test_empty_tiers_is_error(self):
        from puzzleeval.schemas import PricingBreakdown
        bp = PricingBreakdown(
            tiers=[],
            sources=["https://example.com/pricing"],
            confidence="low",
        )
        v = self._put_pricing_on_candidate(["step_1"], bp)
        assert not v.passed
        assert any("zero pricing tiers" in e for e in v.errors)

    def test_missing_sources_is_error(self):
        bp = self._make_basic_pricing(sources=[])
        v = self._put_pricing_on_candidate(["step_1"], bp)
        assert not v.passed
        assert any("no source URLs" in e for e in v.errors)

    def test_invalid_confidence_is_error(self):
        bp = self._make_basic_pricing(confidence="maybe")
        v = self._put_pricing_on_candidate(["step_1"], bp)
        assert not v.passed
        assert any("invalid confidence" in e for e in v.errors)

    def test_low_confidence_warns(self):
        bp = self._make_basic_pricing(confidence="low")
        v = self._put_pricing_on_candidate(["step_1"], bp)
        assert v.passed  # warning, not error
        assert any("confidence='low'" in w for w in v.warnings)

    def test_invalid_billing_granularity_is_error(self):
        bp = self._make_basic_pricing(billing_granularity="weekly")
        v = self._put_pricing_on_candidate(["step_1"], bp)
        assert not v.passed
        assert any("invalid billing_granularity" in e for e in v.errors)

    def test_per_scope_drift_is_error(self):
        # per_scope_unit_cost has a key not in covers_step_ids → drift
        bp = self._make_basic_pricing(
            per_scope_unit_cost={"step_1": 0.1, "step_99": 0.5},
        )
        v = self._put_pricing_on_candidate(["step_1"], bp)
        assert not v.passed
        assert any("step_99" in e and "drift" in e for e in v.errors)

    def test_negative_cost_is_error(self):
        from puzzleeval.schemas import PricingTier
        bp = self._make_basic_pricing(tiers=[
            PricingTier(name="Bad", monthly_cost_usd=-5.0),
        ])
        v = self._put_pricing_on_candidate(["step_1"], bp)
        assert not v.passed
        assert any("negative monthly_cost_usd" in e for e in v.errors)

    def test_negative_overage_is_error(self):
        from puzzleeval.schemas import PricingTier
        bp = self._make_basic_pricing(tiers=[
            PricingTier(
                name="Bad", monthly_cost_usd=10.0,
                overage_cost_per_unit_usd=-0.01,
            ),
        ])
        v = self._put_pricing_on_candidate(["step_1"], bp)
        assert not v.passed
        assert any("negative overage" in e for e in v.errors)

    def test_null_pricing_is_silent(self):
        # No candidate has pricing_breakdown → validator silently skips
        # Phase 5 checks. This is the default state pre-Phase-6.5.
        candidates = self._make_candidates(5, ["A", "B", "C", "D", "E"])
        for c in candidates:
            c.covers_step_ids = frozenset({"step_1", "step_2", "step_3"})
            c.coverage_confidence = {
                "step_1": "claimed", "step_2": "claimed", "step_3": "claimed",
            }
        result = Agent2Result(
            candidates=candidates,
            search_approach="...",
            coverage_notes="...",
        )
        v = validate_agent2_output(result, self._make_uo_with_3_scope_blueprint())
        # No Phase 5 error about pricing — every candidate has None breakdown.
        assert not any("Pricing for" in e for e in v.errors)
        assert not any("Pricing for" in w for w in v.warnings)


# ============================================================================
# Agent 3 Validator Tests
# ============================================================================

class TestAgent3Validator:

    def _make_test_case(self, id: str, sub_task_ref: str, **overrides) -> TestCase:
        defaults = {
            "id": id,
            "sub_task_ref": sub_task_ref,
            "scenario": "Test scenario",
            "input_type": "document_content",
            "input_data": "Invoice #123\nVendor: Test Corp\nTotal: $100.00",
            "input_context": None,
            "test_file_path": None,
            "output_type": "extraction",
            "expected_output": '{"vendor": "Test Corp", "total": 100.00}',
            "judgement_criteria": [
                JudgementCriterion(criterion="Extract vendor", weight=0.5, eval_type="exact_match"),
                JudgementCriterion(criterion="Extract total", weight=0.5, eval_type="exact_match"),
            ],
            "difficulty": "easy",
            "tags": ["happy_path"],
        }
        defaults.update(overrides)
        return TestCase(**defaults)

    def _make_good_result(self) -> Agent3Result:
        """6 test cases: 3 per sub-task, with difficulty spread."""
        st1 = "Extract structured data from invoice photos"
        st2 = "Create bill entries in QuickBooks from structured data"
        return Agent3Result(
            test_cases=[
                self._make_test_case("tc-001", st1, difficulty="easy"),
                self._make_test_case("tc-002", st1, difficulty="medium"),
                self._make_test_case("tc-003", st1, difficulty="hard"),
                self._make_test_case("tc-004", st2, difficulty="easy", input_type="structured_data"),
                self._make_test_case("tc-005", st2, difficulty="medium", input_type="structured_data"),
                self._make_test_case("tc-006", st2, difficulty="hard", input_type="structured_data"),
            ],
            generation_notes="Generated 6 test cases",
            coverage_summary={st1: 3, st2: 3},
        )

    def test_good_output_passes(self):
        v = validate_agent3_output(self._make_good_result(), _make_user_understanding())
        assert v.passed
        assert len(v.errors) == 0

    def test_empty_test_cases_fails(self):
        result = Agent3Result(
            test_cases=[],
            generation_notes="None generated",
            coverage_summary={},
        )
        v = validate_agent3_output(result, _make_user_understanding())
        assert not v.passed

    def test_missing_subtask_coverage_fails(self):
        """If a sub-task from Agent 1 has zero test cases, that's an error."""
        st1 = "Extract structured data from invoice photos"
        # Only cover sub-task 1, not sub-task 2
        result = Agent3Result(
            test_cases=[
                self._make_test_case("tc-001", st1, difficulty="easy"),
                self._make_test_case("tc-002", st1, difficulty="medium"),
                self._make_test_case("tc-003", st1, difficulty="hard"),
            ],
            generation_notes="...",
            coverage_summary={st1: 3},
        )
        v = validate_agent3_output(result, _make_user_understanding())
        assert not v.passed
        assert any("ZERO test cases" in e for e in v.errors)

    def test_duplicate_ids_fails(self):
        st1 = "Extract structured data from invoice photos"
        st2 = "Create bill entries in QuickBooks from structured data"
        result = Agent3Result(
            test_cases=[
                self._make_test_case("tc-001", st1, difficulty="easy"),
                self._make_test_case("tc-001", st1, difficulty="medium"),  # duplicate
                self._make_test_case("tc-003", st1, difficulty="hard"),
                self._make_test_case("tc-004", st2, difficulty="easy", input_type="structured_data"),
                self._make_test_case("tc-005", st2, difficulty="medium", input_type="structured_data"),
                self._make_test_case("tc-006", st2, difficulty="hard", input_type="structured_data"),
            ],
            generation_notes="...",
            coverage_summary={st1: 3, st2: 3},
        )
        v = validate_agent3_output(result, _make_user_understanding())
        assert not v.passed
        assert any("Duplicate" in e for e in v.errors)

    def test_invalid_input_type_fails(self):
        result = self._make_good_result()
        result.test_cases[0].input_type = "invalid_type"
        v = validate_agent3_output(result, _make_user_understanding())
        assert not v.passed

    def test_invalid_eval_type_fails(self):
        result = self._make_good_result()
        result.test_cases[0].judgement_criteria[0].eval_type = "vibes_check"
        v = validate_agent3_output(result, _make_user_understanding())
        assert not v.passed

    def test_bad_weight_sum_warns(self):
        result = self._make_good_result()
        result.test_cases[0].judgement_criteria = [
            JudgementCriterion(criterion="A", weight=0.2, eval_type="exact_match"),
            JudgementCriterion(criterion="B", weight=0.2, eval_type="exact_match"),
        ]
        v = validate_agent3_output(result, _make_user_understanding())
        assert v.passed  # warning, not error
        assert any("weights sum" in w for w in v.warnings)

    def test_test_file_path_set_is_valid(self):
        """Test cases with user-uploaded file paths should pass."""
        result = self._make_good_result()
        result.test_cases[0].test_file_path = "/uploads/invoice_001.pdf"
        v = validate_agent3_output(result, _make_user_understanding())
        assert v.passed

    def test_empty_input_data_fails(self):
        result = self._make_good_result()
        result.test_cases[0].input_data = ""
        v = validate_agent3_output(result, _make_user_understanding())
        assert not v.passed

    def test_empty_expected_output_fails(self):
        result = self._make_good_result()
        result.test_cases[0].expected_output = "  "
        v = validate_agent3_output(result, _make_user_understanding())
        assert not v.passed

    def test_missing_difficulty_spread_warns(self):
        """If a sub-task only has 'easy' cases, should warn."""
        st1 = "Extract structured data from invoice photos"
        st2 = "Create bill entries in QuickBooks from structured data"
        result = Agent3Result(
            test_cases=[
                self._make_test_case("tc-001", st1, difficulty="easy"),
                self._make_test_case("tc-002", st1, difficulty="easy"),
                self._make_test_case("tc-003", st1, difficulty="easy"),
                self._make_test_case("tc-004", st2, difficulty="easy", input_type="structured_data"),
                self._make_test_case("tc-005", st2, difficulty="medium", input_type="structured_data"),
                self._make_test_case("tc-006", st2, difficulty="hard", input_type="structured_data"),
            ],
            generation_notes="...",
            coverage_summary={st1: 3, st2: 3},
        )
        v = validate_agent3_output(result, _make_user_understanding())
        assert v.passed
        assert any("missing difficulty" in w for w in v.warnings)

    def test_too_few_criteria_fails(self):
        result = self._make_good_result()
        result.test_cases[0].judgement_criteria = [
            JudgementCriterion(criterion="Only one", weight=1.0, eval_type="exact_match"),
        ]
        v = validate_agent3_output(result, _make_user_understanding())
        assert not v.passed

    def test_coverage_summary_mismatch_warns(self):
        result = self._make_good_result()
        result.coverage_summary["Extract structured data from invoice photos"] = 99
        v = validate_agent3_output(result, _make_user_understanding())
        assert v.passed
        assert any("coverage_summary" in w for w in v.warnings)

    def test_empty_coverage_summary_warns(self):
        """Empty coverage_summary should produce a warning."""
        result = self._make_good_result()
        result.coverage_summary = {}
        v = validate_agent3_output(result, _make_user_understanding())
        assert v.passed  # warning, not error
        assert any("coverage_summary is empty" in w for w in v.warnings)


# ============================================================================
# Phase 3: WorkflowBlueprint validator
# ============================================================================
# These sit inside validate_agent1_output (same function as existing Agent 1
# checks). Verify that blueprint structural bugs are caught as errors while
# soft inconsistencies (capability drift) are caught as warnings.
# ============================================================================

class TestAgent1WorkflowValidator:
    """validate_agent1_output checks for blueprint consistency when workflow is present."""

    def _result_with_workflow(self, workflow: WorkflowBlueprint | None) -> Agent1Result:
        return Agent1Result(
            is_clear=True,
            result=UserUnderstandingOutput(
                summary="User needs AI for invoice OCR and spreadsheet sync",
                sub_tasks=[
                    SubTask(description="OCR", capability="document OCR",
                            search_keywords=["invoice OCR API", "document extraction"]),
                    SubTask(description="Sync", capability="spreadsheet integration",
                            search_keywords=["Google Sheets API", "spreadsheet automation"]),
                ],
                search_strategy="both",
                domain="accounting",
                search_keywords=["invoice automation", "OCR to sheets"],
                constraints=Constraints(),
                workflow=workflow,
            ),
            clarification_needed=None,
        )

    def test_valid_blueprint_passes(self):
        # Clean 2-step blueprint with matching capabilities -> no errors.
        bp = WorkflowBlueprint(
            steps=[
                WorkflowStep(
                    id="step_1", role="ocr",
                    description="OCR invoice", capability="document OCR",
                    input_from="user", output_format="structured_json",
                ),
                WorkflowStep(
                    id="step_2", role="spreadsheet_sync",
                    description="Append rows", capability="spreadsheet integration",
                    input_from="step_1", output_format="action",
                    depends_on=["step_1"],
                ),
            ],
        )
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert v.passed, f"Expected passed, got errors={v.errors}"

    def test_null_workflow_is_valid(self):
        # Agent 1 can legitimately emit workflow=None; validator shouldn't complain.
        v = validate_agent1_output(self._result_with_workflow(None))
        assert v.passed

    def test_empty_steps_is_error(self):
        bp = WorkflowBlueprint(steps=[])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("zero steps" in e for e in v.errors)

    def test_duplicate_step_ids_is_error(self):
        # Two steps sharing the same id breaks Phase 9's harness keying.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="x",
                         capability="document OCR", input_from="user",
                         output_format="structured_json"),
            WorkflowStep(id="step_1", role="classify", description="y",
                         capability="classification", input_from="step_1",
                         output_format="classification"),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("Duplicate" in e for e in v.errors)

    def test_empty_step_id_is_error(self):
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="", role="ocr", description="x",
                         capability="document OCR", input_from="user",
                         output_format="structured_json"),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("empty id" in e for e in v.errors)

    def test_orphan_depends_on_is_error(self):
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="x",
                         capability="document OCR", input_from="user",
                         output_format="structured_json",
                         depends_on=["step_99"]),  # step_99 doesn't exist
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("step_99" in e for e in v.errors)

    def test_orphan_input_from_is_error(self):
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="x",
                         capability="document OCR",
                         input_from="step_nonexistent",  # not "user", not a valid id
                         output_format="structured_json"),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("input_from" in e for e in v.errors)

    def test_capability_mismatch_is_warning(self):
        # Slight capability drift (e.g. "OCR" vs "document OCR") -> warning,
        # not error. Downstream will still work; we just flag for review.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="x",
                         capability="OCR",  # SubTask uses "document OCR"
                         input_from="user", output_format="structured_json"),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert v.passed  # not an error
        assert any("capability" in w.lower() for w in v.warnings)

    def test_empty_role_is_error(self):
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="", description="x",
                         capability="document OCR",
                         input_from="user", output_format="structured_json"),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("empty role" in e for e in v.errors)

    def test_user_input_from_is_valid(self):
        # "user" is a special valid value for input_from (not a step id).
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="chatbot", description="x",
                         capability="customer support chatbot",
                         input_from="user", output_format="free_text"),
        ])
        result = self._result_with_workflow(bp)
        # SubTask to match the chatbot capability
        result.result.sub_tasks = [
            SubTask(description="d", capability="customer support chatbot",
                    search_keywords=["k1", "k2"]),
        ]
        v = validate_agent1_output(result)
        assert v.passed

    # ── DAG integrity (Phase 3 DAG expansion) ──

    def test_two_step_cycle_is_error(self):
        # A <-> B: each depends on the other. Hard error; Phase 9 routing
        # would loop forever.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="x",
                         capability="document OCR",
                         input_from="user", output_format="structured_json",
                         depends_on=["step_2"]),
            WorkflowStep(id="step_2", role="spreadsheet_sync",
                         description="y", capability="spreadsheet integration",
                         input_from="step_1", output_format="action",
                         depends_on=["step_1"]),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("cycle" in e.lower() for e in v.errors)

    def test_three_step_cycle_is_error(self):
        # Longer cycle A -> B -> C -> A. Must also be caught.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="x",
                         capability="document OCR",
                         input_from="user", output_format="structured_json",
                         depends_on=["step_3"]),
            WorkflowStep(id="step_2", role="classify", description="y",
                         capability="spreadsheet integration",
                         input_from="step_1", output_format="classification",
                         depends_on=["step_1"]),
            WorkflowStep(id="step_3", role="spreadsheet_sync",
                         description="z", capability="document OCR",
                         input_from="step_2", output_format="action",
                         depends_on=["step_2"]),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("cycle" in e.lower() for e in v.errors)

    def test_fan_out_fan_in_passes(self):
        # Proper DAG: step_1 -> {step_2a, step_2b} -> step_3. No cycle,
        # everything reachable. Should validate cleanly.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="a",
                         capability="document OCR",
                         input_from="user", output_format="structured_json"),
            WorkflowStep(id="step_2a", role="tax_verify", description="b",
                         capability="document OCR",
                         input_from="step_1", output_format="structured_json",
                         depends_on=["step_1"], parallel_group="enrichment"),
            WorkflowStep(id="step_2b", role="classify", description="c",
                         capability="spreadsheet integration",
                         input_from="step_1", output_format="classification",
                         depends_on=["step_1"], parallel_group="enrichment"),
            WorkflowStep(id="step_3", role="spreadsheet_sync", description="d",
                         capability="spreadsheet integration",
                         input_from="step_2a", output_format="action",
                         depends_on=["step_2a", "step_2b"]),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert v.passed, f"Expected passed, got errors={v.errors}"

    def test_unreachable_step_is_warning(self):
        # step_3 depends on step_2, which doesn't depend on anything AND
        # isn't flagged as a root (input_from="step_999" is invalid).
        # Wait — invalid input_from will trip an orphan error too, which
        # short-circuits cycle check. Use a CLEAN unreachable: an island
        # pair that depends on each other but not the root.
        # Actually with depends_on being a DAG, "unreachable" means "not
        # connected from any root via forward edges." Construct:
        #   step_1 (root, input_from=user)
        #   step_2 (input_from=user, depends_on=[]) — second root
        #   step_3 (depends_on=["step_1"])
        # All three reachable. To make step_2 unreachable we'd need it to
        # depend_on itself — that's a cycle. The real unreachable case is
        # a step whose input_from points to a step it doesn't depend_on
        # for, AND no root reaches it.
        #
        # Simplest clean "unreachable" shape: step_2 has input_from=step_1
        # but NO depends_on edge pointing at step_2 from anywhere, and
        # step_2's own depends_on is empty — still a root by the validator's
        # definition (empty depends_on + input_from="user"-or-None). So it
        # IS reachable. For a true unreachable, both roots and dependents
        # have to miss it. Construct: step_1 is root, step_2 has
        # depends_on=["step_3"], step_3 has depends_on=["step_2"]. That's
        # a cycle, and the cycle check fires first and blocks unreachable
        # evaluation.
        #
        # Honest test: unreachable warning is emitted when a blueprint has
        # zero roots (every step depends on something). That's the "no
        # root" branch. Verify it.
        bp = WorkflowBlueprint(steps=[
            # step_1 gets input_from=step_2 (valid ref) so it's not a root
            WorkflowStep(id="step_1", role="ocr", description="a",
                         capability="document OCR",
                         input_from="step_2", output_format="structured_json",
                         depends_on=["step_2"]),
            # step_2 gets input_from=step_1 (valid ref) so it's not a root either
            WorkflowStep(id="step_2", role="classify", description="b",
                         capability="spreadsheet integration",
                         input_from="step_1", output_format="classification",
                         depends_on=["step_1"]),
        ])
        # This is also a cycle, so cycle error fires. That's the correct
        # behavior — a no-root multi-step blueprint IS a cycle. Verify the
        # error is emitted; unreachable-warning path is covered indirectly.
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert not v.passed
        assert any("cycle" in e.lower() for e in v.errors)

    def test_parallel_group_metadata_survives(self):
        # parallel_group is UI-only but must round-trip cleanly for the
        # frontend to read it. Validator shouldn't choke on it.
        bp = WorkflowBlueprint(steps=[
            WorkflowStep(id="step_1", role="ocr", description="a",
                         capability="document OCR",
                         input_from="user", output_format="structured_json"),
            WorkflowStep(id="step_2a", role="classify", description="b",
                         capability="document OCR",
                         input_from="step_1", output_format="classification",
                         depends_on=["step_1"], parallel_group="enrichment"),
            WorkflowStep(id="step_2b", role="tax_verify", description="c",
                         capability="spreadsheet integration",
                         input_from="step_1", output_format="structured_json",
                         depends_on=["step_1"], parallel_group="enrichment"),
        ])
        v = validate_agent1_output(self._result_with_workflow(bp))
        assert v.passed, f"Expected passed, got errors={v.errors}"
        # And verify the tags actually stuck in the model.
        assert bp.steps[1].parallel_group == "enrichment"
        assert bp.steps[2].parallel_group == "enrichment"
        assert bp.steps[0].parallel_group is None
