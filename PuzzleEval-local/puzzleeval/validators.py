# ============================================================================
# Output Quality Validators — Catch Silent Failures
# ============================================================================
# Pydantic validates STRUCTURE (is the JSON schema correct?).
# Validators check QUALITY (will this output actually work downstream?).
#
# Every agent gets a validation function that returns ValidationResult:
#   - passed: True if no hard errors (pipeline can continue)
#   - errors: blocking issues (pipeline should stop or flag)
#   - warnings: quality concerns (pipeline continues, but log for review)
#
# Validators that check cross-agent consistency (e.g., "does Agent 3 cover
# all of Agent 1's sub-tasks?") take the upstream agent's output as context.
# ============================================================================

from pydantic import BaseModel, Field

from puzzleeval.schemas import (
    Agent1Result,
    Agent2Result,
    Agent3Result,
    Agent4Result,
    Agent5Result,
    UserUnderstandingOutput,
)


class ValidationResult(BaseModel):
    """Result of a quality validation check."""

    passed: bool = Field(
        description="True if no hard errors — pipeline can continue"
    )

    errors: list[str] = Field(
        default_factory=list,
        description="Blocking issues that indicate the output is unusable"
    )

    warnings: list[str] = Field(
        default_factory=list,
        description="Quality concerns that don't block but should be reviewed"
    )


# ============================================================================
# Allowed values for Agent 3 enum-like fields
# ============================================================================

VALID_INPUT_TYPES = {"text", "structured_data", "document_content", "conversation", "image_description"}
VALID_OUTPUT_TYPES = {"free_text", "structured_json", "classification", "extraction", "action"}
VALID_EVAL_TYPES = {"exact_match", "semantic_similarity", "contains_key_info", "format_compliance", "subjective_quality"}
VALID_DIFFICULTIES = {"easy", "medium", "hard"}

# ============================================================================
# Allowed values for Agent 4 enum-like fields
# ============================================================================

VALID_REJECTION_CATEGORIES = {
    "no_api_access", "no_public_docs", "capability_mismatch",
    "rate_limit_insufficient", "no_free_tier", "enterprise_only",
    "deprecated", "region_restricted",
}

VALID_AUTH_METHODS = {
    "api_key", "oauth2", "bearer_token", "basic_auth", "no_auth", "unknown",
}

VALID_ACCESS_METHODS = {
    "free_signup", "free_tier", "trial", "sandbox", "open", "paid_only",
}


# ============================================================================
# Allowed values for Agent 2 adoption difficulty
# ============================================================================

VALID_ADOPTION_DIFFICULTIES = {"easy", "medium", "hard"}


# ============================================================================
# Shared helpers
# ============================================================================

# Stop words excluded from keyword-overlap matching — these carry no
# semantic signal and inflate overlap scores between unrelated descriptions.
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "to", "for", "from",
    "with", "into", "on", "is", "it", "as", "at", "by", "be", "that",
    "this", "which", "given", "using", "via", "such", "each", "all",
    "can", "must", "should", "will", "also", "etc", "e.g", "i.e",
})


def _significant_words(text: str) -> set[str]:
    """Extract significant lowercase words (3+ chars, not stop words)."""
    import re
    words = set(re.findall(r"[a-z][a-z0-9]+", text.lower()))
    return {w for w in words if len(w) >= 3 and w not in _STOP_WORDS}


def _fuzzy_subtask_match(description: str, candidates: set[str]) -> bool:
    """
    Check if a sub-task description is semantically covered by any candidate
    description using keyword overlap.

    Returns True if any candidate shares >= 40% of the description's
    significant words. This threshold catches rephrasings like:
      "Given a photo of an invoice, extract structured data" vs
      "Extract structured data from invoice photos/PDFs"
    while rejecting genuinely unrelated descriptions.
    """
    desc_words = _significant_words(description)
    if not desc_words:
        return False

    for candidate_text in candidates:
        cand_words = _significant_words(candidate_text)
        if not cand_words:
            continue
        overlap = desc_words & cand_words
        # Check overlap from BOTH directions — the shorter description
        # may share a high % even when the longer one doesn't.
        desc_ratio = len(overlap) / len(desc_words)
        cand_ratio = len(overlap) / len(cand_words)
        if desc_ratio >= 0.4 or cand_ratio >= 0.4:
            return True
    return False


# ============================================================================
# Agent 1 Validator
# ============================================================================

def validate_agent1_output(result: Agent1Result) -> ValidationResult:
    """
    Validate Agent 1's output quality.

    Checks (when is_clear=True):
    - At least 1 sub-task with description
    - Each sub-task has at least 2 search keywords
    - Domain is not empty
    - Search keywords exist at top level
    """
    errors = []
    warnings = []

    if not result.is_clear:
        # Clarification response — validate that
        if result.clarification_needed is None:
            errors.append("is_clear=False but clarification_needed is None")
        return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)

    if result.result is None:
        errors.append("is_clear=True but result is None")
        return ValidationResult(passed=False, errors=errors, warnings=warnings)

    r = result.result

    # Sub-tasks
    if len(r.sub_tasks) == 0:
        errors.append("No sub-tasks identified — downstream agents have nothing to work with")
    else:
        for i, st in enumerate(r.sub_tasks):
            if not st.description.strip():
                errors.append(f"Sub-task {i+1} has empty description")
            if len(st.search_keywords) < 2:
                warnings.append(f"Sub-task {i+1} '{st.description[:50]}' has fewer than 2 search keywords — may limit research quality")

    # Domain
    if not r.domain or not r.domain.strip():
        errors.append("Domain is empty — Agent 2 needs domain context for effective search")
    elif r.domain.lower() in ("general", "other", "unknown", "unspecified"):
        warnings.append(f"Domain is '{r.domain}' — vague domain may reduce research quality")

    # Top-level search keywords
    if len(r.search_keywords) < 2:
        warnings.append("Fewer than 2 top-level search keywords — may limit all-in-one solution discovery")

    # Summary
    if not r.summary or len(r.summary.strip()) < 10:
        warnings.append("Summary is very short — may not provide enough context for downstream agents")

    return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)


# ============================================================================
# Agent 2 Validator
# ============================================================================

def validate_agent2_output(
    result: Agent2Result,
    agent1_output: UserUnderstandingOutput,
) -> ValidationResult:
    """
    Validate Agent 2's output quality.

    Checks:
    - Minimum candidate count
    - Provider diversity
    - All candidates have api_available=True
    - Sub-task coverage (every Agent 1 sub-task should appear in at least 1 candidate)
    - Relevance scores in valid range
    """
    errors = []
    warnings = []

    # Candidate count
    if len(result.candidates) < 4:
        errors.append(
            f"Only {len(result.candidates)} candidates found — need at least 4 "
            f"for meaningful comparison after screening"
        )
    elif len(result.candidates) < 5:
        warnings.append(
            f"Only {len(result.candidates)} candidates — target is 5-7 for buffer after screening"
        )

    # Provider diversity
    providers = {c.provider for c in result.candidates}
    if len(providers) < 3:
        errors.append(
            f"Only {len(providers)} unique providers — need at least 3 for diversity"
        )
    elif len(providers) < 4:
        warnings.append(
            f"Only {len(providers)} unique providers — target is 4+ for diversity"
        )

    # API availability (V0 scope)
    non_api = [c.name for c in result.candidates if not c.api_available]
    if non_api:
        errors.append(
            f"Candidates without API access (V0 requires API): {', '.join(non_api)}"
        )

    # Relevance scores
    for c in result.candidates:
        if not (0.0 <= c.relevance_score <= 1.0):
            errors.append(f"'{c.name}' has invalid relevance_score: {c.relevance_score}")
        elif c.relevance_score < 0.3:
            warnings.append(f"'{c.name}' has very low relevance_score ({c.relevance_score}) — may not be a good fit")

    # Adoption difficulty
    for c in result.candidates:
        if c.adoption_difficulty not in VALID_ADOPTION_DIFFICULTIES:
            errors.append(
                f"'{c.name}' has invalid adoption_difficulty: '{c.adoption_difficulty}'. "
                f"Must be one of: {sorted(VALID_ADOPTION_DIFFICULTIES)}"
            )

    # Adoption difficulty vs technical level — informational cross-check
    # This is an observation, not a hard filter. The research agent uses
    # contextual judgment to select candidates. But if most candidates are
    # hard-adoption for a non-technical user, that's worth flagging.
    tech_level = None
    if agent1_output.constraints:
        tech_level = agent1_output.constraints.technical_level
    if tech_level == "non-technical":
        hard_candidates = [
            c.name for c in result.candidates
            if c.adoption_difficulty == "hard"
        ]
        if len(hard_candidates) > len(result.candidates) // 2:
            warnings.append(
                f"User is non-technical but majority of candidates have hard adoption "
                f"difficulty: {hard_candidates}. The research agent may not have found "
                f"enough user-appropriate alternatives."
            )

    # Sub-task coverage
    # Agent 2 candidates describe sub-tasks in their own words (e.g.,
    # "Extract structured data from invoice photos/PDFs") which won't
    # match Agent 1's exact description ("Given a photo or PDF of an
    # invoice, extract structured data..."). We use keyword overlap to
    # detect semantic coverage: if two descriptions share enough
    # significant words, they refer to the same sub-task.
    agent1_subtask_descriptions = {st.description for st in agent1_output.sub_tasks}
    covered_subtasks = set()
    for c in result.candidates:
        for st_ref in c.relevant_subtasks:
            covered_subtasks.add(st_ref)

    uncovered = agent1_subtask_descriptions - covered_subtasks
    if uncovered:
        still_uncovered = []
        for uc in uncovered:
            found = _fuzzy_subtask_match(uc, covered_subtasks)
            if not found:
                still_uncovered.append(uc)

        if still_uncovered:
            warnings.append(
                f"Sub-tasks not covered by any candidate: {still_uncovered}"
            )

    return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)


# ============================================================================
# Agent 3 Validator
# ============================================================================

def validate_agent3_output(
    result: Agent3Result,
    agent1_output: UserUnderstandingOutput,
) -> ValidationResult:
    """
    Validate Agent 3's output quality.

    Checks:
    - Every sub-task has at least 3 test cases
    - Judgement criteria weights sum to ~1.0
    - Difficulty spread (at least 1 easy, 1 medium, 1 hard per sub-task)
    - Valid enum values for input_type, output_type, eval_type, difficulty
    - coverage_summary matches actual test case distribution
    """
    errors = []
    warnings = []

    if len(result.test_cases) == 0:
        errors.append("No test cases generated")
        return ValidationResult(passed=False, errors=errors, warnings=warnings)

    # --- Per-sub-task coverage analysis ---
    agent1_subtask_descriptions = {st.description for st in agent1_output.sub_tasks}
    subtask_cases: dict[str, list] = {desc: [] for desc in agent1_subtask_descriptions}

    for tc in result.test_cases:
        # Try exact match first, then fuzzy
        matched = False
        for desc in agent1_subtask_descriptions:
            if tc.sub_task_ref == desc:
                subtask_cases[desc].append(tc)
                matched = True
                break
        if not matched:
            # Fuzzy match
            for desc in agent1_subtask_descriptions:
                if tc.sub_task_ref.lower()[:30] in desc.lower() or desc.lower()[:30] in tc.sub_task_ref.lower():
                    subtask_cases[desc].append(tc)
                    matched = True
                    break
        if not matched:
            warnings.append(f"Test case {tc.id} references unknown sub-task: '{tc.sub_task_ref[:60]}'")

    # Check minimum coverage per sub-task
    # Check if file-based testing was used (test_file_path set on any test case).
    # When files are provided, some sub-tasks may have zero coverage if they're
    # not testable with the provided files — this is expected, not an error.
    has_file_tests = any(tc.test_file_path for tc in result.test_cases)

    for desc, cases in subtask_cases.items():
        if len(cases) == 0:
            if has_file_tests:
                warnings.append(f"Sub-task not covered by file-based tests (expected if not file-testable): '{desc[:60]}'")
            else:
                errors.append(f"Sub-task has ZERO test cases: '{desc[:60]}'")
        elif len(cases) < 3:
            warnings.append(f"Sub-task has only {len(cases)} test cases (target: 5-8): '{desc[:60]}'")

    # --- Difficulty spread per sub-task ---
    for desc, cases in subtask_cases.items():
        if len(cases) < 3:
            continue  # Already flagged above
        difficulties = {tc.difficulty for tc in cases}
        missing = {"easy", "medium", "hard"} - difficulties
        if missing:
            warnings.append(
                f"Sub-task '{desc[:40]}' missing difficulty levels: {missing}"
            )

    # --- Per-test-case validation ---
    unique_ids = set()
    for tc in result.test_cases:
        # Duplicate IDs
        if tc.id in unique_ids:
            errors.append(f"Duplicate test case ID: {tc.id}")
        unique_ids.add(tc.id)

        # Valid enum values
        if tc.input_type not in VALID_INPUT_TYPES:
            errors.append(f"Test case {tc.id} has invalid input_type: '{tc.input_type}'")
        if tc.output_type not in VALID_OUTPUT_TYPES:
            errors.append(f"Test case {tc.id} has invalid output_type: '{tc.output_type}'")
        if tc.difficulty not in VALID_DIFFICULTIES:
            errors.append(f"Test case {tc.id} has invalid difficulty: '{tc.difficulty}'")

        # Judgement criteria
        if len(tc.judgement_criteria) < 2:
            errors.append(f"Test case {tc.id} has fewer than 2 judgement criteria")
        else:
            total_weight = sum(c.weight for c in tc.judgement_criteria)
            if not (0.8 <= total_weight <= 1.2):
                warnings.append(
                    f"Test case {tc.id} criteria weights sum to {total_weight:.2f} (expected ~1.0)"
                )
            for criterion in tc.judgement_criteria:
                if criterion.eval_type not in VALID_EVAL_TYPES:
                    errors.append(
                        f"Test case {tc.id} has invalid eval_type: '{criterion.eval_type}'"
                    )
                if not (0.0 <= criterion.weight <= 1.0):
                    errors.append(
                        f"Test case {tc.id} has invalid weight: {criterion.weight}"
                    )

        # Input data should not be empty
        if not tc.input_data or not tc.input_data.strip():
            errors.append(f"Test case {tc.id} has empty input_data")

        # Expected output should not be empty
        if not tc.expected_output or not tc.expected_output.strip():
            errors.append(f"Test case {tc.id} has empty expected_output")

    # --- Coverage summary must not be empty ---
    if not result.coverage_summary:
        warnings.append(
            "coverage_summary is empty — should map each sub-task to its test case count"
        )

    # --- Coverage summary consistency ---
    actual_counts = {}
    for tc in result.test_cases:
        actual_counts[tc.sub_task_ref] = actual_counts.get(tc.sub_task_ref, 0) + 1

    for ref, count in result.coverage_summary.items():
        actual = actual_counts.get(ref, 0)
        if actual != count:
            warnings.append(
                f"coverage_summary says '{ref[:40]}' has {count} cases, "
                f"but actual count is {actual}"
            )

    return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)


# ============================================================================
# Agent 4 Validator
# ============================================================================

def validate_agent4_output(
    result: Agent4Result,
    agent2_output: Agent2Result,
    agent1_output: UserUnderstandingOutput,
) -> ValidationResult:
    """
    Validate Agent 4's output quality.

    Checks:
    - Minimum validated candidate count
    - Count consistency (validated + rejected = total_candidates_screened)
    - All validated candidates have required enrichment fields populated
    - All rejected candidates have valid rejection categories
    - Every Agent 2 candidate appears in either validated or rejected
    - Capability overlap between confirmed_capabilities and user sub-tasks
    """
    errors = []
    warnings = []

    # --- Candidate counts ---
    validated_count = len(result.validated_candidates)
    rejected_count = len(result.rejected_candidates)
    total = validated_count + rejected_count

    if validated_count < 2:
        errors.append(
            f"Only {validated_count} candidates passed screening — need at least 2 "
            f"for meaningful comparison in testing"
        )
    elif validated_count < 3:
        warnings.append(
            f"Only {validated_count} candidates passed screening — target is 3-5"
        )

    # Count consistency
    if total != result.total_candidates_screened:
        errors.append(
            f"Count mismatch: {validated_count} validated + {rejected_count} rejected "
            f"= {total}, but total_candidates_screened = {result.total_candidates_screened}"
        )

    # --- Validated candidate field checks ---
    for vc in result.validated_candidates:
        prefix = f"Validated '{vc.name}'"

        if not vc.verified_api_docs_url or not vc.verified_api_docs_url.strip():
            errors.append(f"{prefix} has empty verified_api_docs_url — Agent 5 needs this")

        if not vc.auth_method or not vc.auth_method.strip():
            errors.append(f"{prefix} has empty auth_method")
        elif vc.auth_method not in VALID_AUTH_METHODS:
            warnings.append(f"{prefix} has non-standard auth_method: '{vc.auth_method}'")
        elif vc.auth_method == "unknown":
            warnings.append(f"{prefix} has auth_method='unknown' — Agent 5 may struggle with authentication")

        if not vc.api_access_method or not vc.api_access_method.strip():
            errors.append(f"{prefix} has empty api_access_method")
        elif vc.api_access_method not in VALID_ACCESS_METHODS:
            warnings.append(f"{prefix} has non-standard api_access_method: '{vc.api_access_method}'")
        elif vc.api_access_method == "paid_only":
            warnings.append(f"{prefix} requires paid access — may block Agent 5 from building test harness")

        if not vc.confirmed_capabilities:
            errors.append(f"{prefix} has empty confirmed_capabilities — nothing was verified from docs")

        if not vc.data_format_notes or not vc.data_format_notes.strip():
            errors.append(f"{prefix} has empty data_format_notes — Agent 5 needs format info")

        # NOTE: Capability overlap check (do confirmed_capabilities match user
        # sub-tasks?) is intentionally NOT done here. Semantic capability
        # matching requires understanding that "document OCR" and "invoice
        # data extraction" mean the same thing — keyword matching produces
        # too many false warnings. The agent itself (Claude) already does
        # this semantic matching when it determines PASS/REJECT. The
        # validator focuses on structural checks that don't need LLM judgment.

    # --- Rejected candidate field checks ---
    for rc in result.rejected_candidates:
        prefix = f"Rejected '{rc.name}'"

        if not rc.rejection_reason or not rc.rejection_reason.strip():
            errors.append(f"{prefix} has empty rejection_reason")

        if rc.rejection_category not in VALID_REJECTION_CATEGORIES:
            errors.append(
                f"{prefix} has invalid rejection_category: '{rc.rejection_category}'. "
                f"Must be one of: {sorted(VALID_REJECTION_CATEGORIES)}"
            )

    # --- Cross-agent consistency: every Agent 2 candidate accounted for ---
    agent2_names = {c.name for c in agent2_output.candidates}
    validated_names = {c.name for c in result.validated_candidates}
    rejected_names = {c.name for c in result.rejected_candidates}
    accounted_names = validated_names | rejected_names

    missing = agent2_names - accounted_names
    if missing:
        warnings.append(
            f"Agent 2 candidates not found in screening results (silently dropped): "
            f"{sorted(missing)}"
        )

    return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)


# ============================================================================
# Agent 5 Validator — Test Harness Quality
# ============================================================================
# Validates that Agent 5 produced usable test harnesses for Agent 5 test execution.
#
# Structural checks only — no semantic matching (same philosophy as Agent 4).
# The builder agent (Claude) already did semantic work when reading docs and
# writing code. Validators catch structural bugs.
# ============================================================================

VALID_FAILURE_CATEGORIES = {
    "docs_unusable", "auth_blocked", "api_incompatible",
    "build_timeout", "dependency_failure", "unknown",
}


def validate_agent5_output(
    result: Agent5Result,
    agent4_output: Agent4Result,
) -> ValidationResult:
    """
    Validate Agent 5's output quality.

    Checks:
    - At least 1 harness succeeded (error if zero — pipeline can't continue)
    - Count consistency (harnesses + failed = total_candidates_attempted)
    - Every Agent 4 validated candidate appears in either harnesses or failed
    - Each harness has non-empty harness_code, entry_file, harness_dir
    - Each harness harness_dir exists on disk
    - Each failed harness has a valid failure_category
    - Budget not exceeded
    """
    errors = []
    warnings = []

    # --- Count consistency ---
    harness_count = len(result.harnesses)
    failed_count = len(result.failed_harnesses)
    total = harness_count + failed_count

    if total != result.total_candidates_attempted:
        errors.append(
            f"Count mismatch: {harness_count} harnesses + {failed_count} failed "
            f"= {total}, but total_candidates_attempted = {result.total_candidates_attempted}"
        )

    # --- Minimum harness count ---
    if harness_count == 0:
        errors.append(
            "Zero harnesses built — pipeline cannot continue to Agent 5 test execution. "
            "All candidates failed to produce a working test harness."
        )
    elif harness_count == 1:
        warnings.append(
            "Only 1 harness built — need at least 2 for meaningful comparison"
        )

    # --- Per-harness checks ---
    from pathlib import Path

    for h in result.harnesses:
        prefix = f"Harness '{h.candidate_name}'"

        if not h.harness_code or not h.harness_code.strip():
            errors.append(f"{prefix} has empty harness_code")

        if not h.entry_file or not h.entry_file.strip():
            errors.append(f"{prefix} has empty entry_file")

        if not h.harness_dir or not h.harness_dir.strip():
            errors.append(f"{prefix} has empty harness_dir")
        elif not Path(h.harness_dir).exists():
            warnings.append(
                f"{prefix} harness_dir does not exist on disk: {h.harness_dir}"
            )

        if not h.auth_env_vars:
            warnings.append(f"{prefix} has no auth_env_vars — Agent 5 test execution won't know how to authenticate")

        if not h.smoke_test_passed:
            warnings.append(
                f"{prefix} smoke test did not pass — harness may not work correctly"
            )

        if not h.requirements:
            warnings.append(f"{prefix} has no requirements — may be missing dependencies")

        # Live validation checks
        if h.live_validation_attempted and h.live_validation_passed is False:
            warnings.append(
                f"{prefix} live API validation FAILED — harness may not work with real API: "
                f"{(h.live_validation_notes or '')[:200]}"
            )

    # --- Live validation coverage ---
    live_attempted_count = sum(1 for h in result.harnesses if h.live_validation_attempted)
    if result.harnesses and live_attempted_count == 0:
        warnings.append(
            "No harnesses were live-validated — no provider credentials available. "
            "Consider adding credentials to provider_registry.json for more reliable harnesses."
        )

    # --- Per-failed-harness checks ---
    for fh in result.failed_harnesses:
        prefix = f"Failed '{fh.candidate_name}'"

        if not fh.failure_reason or not fh.failure_reason.strip():
            errors.append(f"{prefix} has empty failure_reason")

        if fh.failure_category not in VALID_FAILURE_CATEGORIES:
            errors.append(
                f"{prefix} has invalid failure_category: '{fh.failure_category}'. "
                f"Must be one of: {sorted(VALID_FAILURE_CATEGORIES)}"
            )

    # --- Cross-agent consistency: every Agent 4 candidate accounted for ---
    agent4_names = {c.name for c in agent4_output.validated_candidates}
    harness_names = {h.candidate_name for h in result.harnesses}
    failed_names = {f.candidate_name for f in result.failed_harnesses}
    accounted_names = harness_names | failed_names

    missing = agent4_names - accounted_names
    if missing:
        warnings.append(
            f"Agent 4 validated candidates not found in Agent 5 results "
            f"(silently dropped): {sorted(missing)}"
        )

    # --- Budget check ---
    from puzzleeval.config import AGENT5_MAX_BUDGET_TOTAL
    if result.total_build_cost_usd > AGENT5_MAX_BUDGET_TOTAL:
        warnings.append(
            f"Total build cost ${result.total_build_cost_usd:.2f} exceeds "
            f"budget cap ${AGENT5_MAX_BUDGET_TOTAL:.2f}"
        )

    # --- Per-candidate test run checks (if test execution happened) ---
    for run in result.candidate_runs:
        prefix = f"TestRun '{run.candidate_name}'"
        total_tests = run.tests_passed + run.tests_failed + run.tests_errored + run.tests_skipped
        if total_tests == 0:
            warnings.append(f"{prefix} has zero test results")

        # High error rate is concerning, but skipped tests are legitimate
        executed = total_tests - run.tests_skipped
        if executed > 0 and run.tests_errored > (executed * 0.5):
            warnings.append(
                f"{prefix} has high error rate: {run.tests_errored}/{executed} "
                f"executed tests errored"
            )

        # Informational: note when many tests were skipped
        if run.tests_skipped > 0:
            warnings.append(
                f"{prefix} skipped {run.tests_skipped}/{total_tests} tests "
                f"(INCOMPATIBLE input type or missing files — not errors)"
            )

    return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)
