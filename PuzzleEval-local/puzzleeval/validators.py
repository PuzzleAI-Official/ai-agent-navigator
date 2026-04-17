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

VALID_INPUT_TYPES = {
    "text", "structured_data", "document_content", "conversation",
    "image_description", "audio_content", "file_reference", "code",
    # Inbound / outbound / voice modalities — wired in by the
    # webhook_receiver, outbound_delivery, voice_realtime plugins.
    "webhook_event", "voice_turn",
}
# `code` and `audio_content` added so Agent 1's TestPlan can declare
# code-generation and voice-agent scopes whose outputs are dispatched to
# the code_execution / transcription tool plugins. Without these, the
# modality detector falls back to LLM judging for those modalities even
# when the right plugin is registered + ready.
#
# `webhook_event`, `voice_turn`, `webhook_callback`, `outbound_message`
# extend the enum so Agent 1 can declare scopes that the webhook_receiver
# / outbound_delivery / voice_realtime plugins handle. Each value maps to
# a real plugin via puzzleeval/test_data_sufficiency.py and modality.py.
VALID_OUTPUT_TYPES = {
    "free_text", "structured_json", "classification", "extraction",
    "action", "media_url", "code", "audio_content",
    "webhook_callback", "outbound_message", "voice_turn",
}
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
# Phase 5: Allowed values for PricingBreakdown enum-like fields
# ============================================================================

VALID_PRICING_CONFIDENCES = {"high", "medium", "low"}
VALID_BILLING_GRANULARITIES = {"monthly", "per_call", "annual_commit", "hybrid"}


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


def _check_pricing_breakdown(
    candidate_name: str,
    covers_step_ids: frozenset[str] | set[str],
    breakdown,
    errors: list[str],
    warnings: list[str],
) -> None:
    """
    Phase 5 shared validator — call from Agent 2 / Agent 4 validators when
    a candidate has a non-null `pricing_breakdown`. Silent when breakdown
    is None (the overwhelmingly common case until Phase 6.5 ships).

    Structural checks (errors):
      - `tiers` must have >=1 entry
      - `sources` must have >=1 entry
      - `confidence` must be in VALID_PRICING_CONFIDENCES
      - `billing_granularity` must be in VALID_BILLING_GRANULARITIES
      - `per_scope_unit_cost` keys must be a subset of covers_step_ids
      - each tier's monthly_cost_usd must be >=0
      - each tier's overage_cost_per_unit_usd (when set) must be >=0
    Soft checks (warnings):
      - confidence=low (emitter struggled — ops should review)
      - negative / zero sources after the required >=1 check
    """
    if breakdown is None:
        return

    prefix = f"Pricing for '{candidate_name}'"

    if len(breakdown.tiers) == 0:
        errors.append(
            f"{prefix} has zero pricing tiers — candidates with no accessible "
            "tier should be REJECTED by Phase 6.5, not emitted with an empty "
            "pricing_breakdown"
        )
    if not breakdown.sources:
        errors.append(
            f"{prefix} has no source URLs — every pricing tier must be "
            "backed by at least one source for UI verifiability"
        )
    if breakdown.confidence not in VALID_PRICING_CONFIDENCES:
        errors.append(
            f"{prefix} has invalid confidence='{breakdown.confidence}'. "
            f"Must be one of: {sorted(VALID_PRICING_CONFIDENCES)}"
        )
    elif breakdown.confidence == "low":
        warnings.append(
            f"{prefix} has confidence='low' — pricing page couldn't be "
            "parsed cleanly; numbers may be estimates"
        )

    if breakdown.billing_granularity not in VALID_BILLING_GRANULARITIES:
        errors.append(
            f"{prefix} has invalid billing_granularity="
            f"'{breakdown.billing_granularity}'. Must be one of: "
            f"{sorted(VALID_BILLING_GRANULARITIES)}"
        )

    # per_scope_unit_cost keys must align with covers_step_ids — a scope
    # in pricing that the candidate doesn't claim to cover is structural
    # drift.
    valid_scopes = set(covers_step_ids)
    if breakdown.per_scope_unit_cost and valid_scopes:
        for scope_id in breakdown.per_scope_unit_cost:
            if scope_id not in valid_scopes:
                errors.append(
                    f"{prefix} has per_scope_unit_cost['{scope_id}'] but "
                    "this scope is not in covers_step_ids — drift between "
                    "pricing and coverage fields"
                )

    # Per-tier arithmetic sanity
    for i, tier in enumerate(breakdown.tiers):
        tprefix = f"{prefix} tier[{i}] '{tier.name}'"
        if tier.monthly_cost_usd < 0:
            errors.append(
                f"{tprefix} has negative monthly_cost_usd="
                f"{tier.monthly_cost_usd}"
            )
        if (
            tier.overage_cost_per_unit_usd is not None
            and tier.overage_cost_per_unit_usd < 0
        ):
            errors.append(
                f"{tprefix} has negative overage_cost_per_unit_usd="
                f"{tier.overage_cost_per_unit_usd}"
            )


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

    # ── Phase 3: WorkflowBlueprint consistency ──
    # The blueprint is optional (None is valid — Agent 1 may legitimately fail to
    # produce one, and old saved outputs predate Phase 3). BUT when it IS present,
    # its internal structure must be consistent or downstream phases will break
    # (empty step_id -> KeyError, orphan depends_on -> silent skip, etc.).
    if r.workflow is not None:
        bp = r.workflow
        if len(bp.steps) == 0:
            errors.append("WorkflowBlueprint has zero steps — should be None, not empty")
        else:
            # ID uniqueness — Phase 9 harness chaining keys by id.
            ids_seen = set()
            for i, step in enumerate(bp.steps):
                if not step.id.strip():
                    errors.append(f"WorkflowStep {i} has empty id")
                elif step.id in ids_seen:
                    errors.append(f"Duplicate WorkflowStep id '{step.id}' — ids must be unique")
                else:
                    ids_seen.add(step.id)

            # Orphan depends_on — each ref must point to an existing step.
            valid_ids = ids_seen
            for step in bp.steps:
                for dep in step.depends_on:
                    if dep not in valid_ids:
                        errors.append(
                            f"WorkflowStep '{step.id}' depends_on '{dep}' which is not a known step id"
                        )

            # input_from — must be "user" or a valid step id.
            for step in bp.steps:
                if step.input_from is None:
                    continue
                if step.input_from == "user":
                    continue
                if step.input_from not in valid_ids:
                    errors.append(
                        f"WorkflowStep '{step.id}' input_from='{step.input_from}' is neither 'user' nor a known step id"
                    )

            # ── DAG integrity: cycle detection ──
            # depends_on + steps must form a DAG. A cycle means Phase 9's
            # routing logic would loop forever, so this is a hard error.
            # Only run when there are no orphan depends_on references
            # (we only want to walk edges whose endpoints actually exist).
            step_by_id = {s.id: s for s in bp.steps if s.id.strip()}
            has_bad_refs = any(
                dep not in step_by_id
                for s in bp.steps
                for dep in s.depends_on
            )
            if not has_bad_refs:
                WHITE, GRAY, BLACK = 0, 1, 2
                color: dict[str, int] = {sid: WHITE for sid in step_by_id}
                cycle_nodes: list[str] = []

                def _dfs(node: str, stack: list[str]) -> bool:
                    """Return True if a cycle is found rooted at `node`."""
                    color[node] = GRAY
                    stack.append(node)
                    for dep in step_by_id[node].depends_on:
                        if color[dep] == GRAY:
                            # Back edge — the cycle is the path from `dep`
                            # forward through `stack` plus the edge back.
                            idx = stack.index(dep)
                            cycle_nodes.extend(stack[idx:] + [dep])
                            return True
                        if color[dep] == WHITE and _dfs(dep, stack):
                            return True
                    stack.pop()
                    color[node] = BLACK
                    return False

                for sid in step_by_id:
                    if color[sid] == WHITE and _dfs(sid, []):
                        break

                if cycle_nodes:
                    errors.append(
                        "WorkflowBlueprint has a cycle in depends_on: "
                        + " -> ".join(cycle_nodes)
                        + ". The DAG must be acyclic; Phase 9 routing would loop forever."
                    )

            # ── DAG integrity: unreachable steps ──
            # Walk forward from every root (step with empty depends_on AND
            # input_from in {None, "user"}) and warn about steps that can
            # never be reached. Soft warning, not error — downstream can
            # still execute reachable steps; this just flags authoring drift.
            if not has_bad_refs and step_by_id:
                roots = {
                    s.id for s in bp.steps
                    if not s.depends_on
                    and (s.input_from is None or s.input_from == "user")
                }
                if roots:
                    # Build id → list of downstream ids (dependents).
                    dependents: dict[str, list[str]] = {sid: [] for sid in step_by_id}
                    for s in bp.steps:
                        for dep in s.depends_on:
                            dependents[dep].append(s.id)
                    reachable: set[str] = set()
                    frontier = list(roots)
                    while frontier:
                        node = frontier.pop()
                        if node in reachable:
                            continue
                        reachable.add(node)
                        frontier.extend(dependents[node])
                    unreachable = set(step_by_id) - reachable
                    for sid in sorted(unreachable):
                        warnings.append(
                            f"WorkflowStep '{sid}' is unreachable from any root "
                            f"(no path from user input). Agent 1 likely intended "
                            f"a depends_on link to an earlier step."
                        )
                elif len(step_by_id) > 1:
                    # Multi-step blueprint with zero roots means every step
                    # depends on something — the whole thing is stranded.
                    warnings.append(
                        "WorkflowBlueprint has no root step (every step has a "
                        "non-empty depends_on or a non-'user' input_from). "
                        "Runtime has nowhere to start."
                    )

            # Capability cross-ref — each step.capability should match a SubTask.capability.
            # Warn (not error) because tolerating slight wording drift is safer than blocking.
            subtask_caps = {st.capability.strip().lower() for st in r.sub_tasks}
            for step in bp.steps:
                if step.capability.strip().lower() not in subtask_caps:
                    warnings.append(
                        f"WorkflowStep '{step.id}' capability '{step.capability}' doesn't match any SubTask.capability "
                        "— downstream role-based candidate grouping may lose this step"
                    )

            # Role hygiene — snake_case-ish, non-empty, reasonable length.
            for step in bp.steps:
                if not step.role.strip():
                    errors.append(f"WorkflowStep '{step.id}' has empty role")
                elif len(step.role) > 40:
                    warnings.append(f"WorkflowStep '{step.id}' role '{step.role}' is unusually long — prefer concise snake_case tags")

            # Architecture options sanity
            if not bp.architecture_options:
                warnings.append("WorkflowBlueprint.architecture_options is empty — at least 'all_in_one' is expected")

            # Duplicate capabilities — causes scope routing ambiguity
            # when Agent 3's fuzzy matching assigns tests to the wrong scope.
            cap_counts: dict[str, list[str]] = {}
            for step in bp.steps:
                cap_key = step.capability.strip().lower()
                cap_counts.setdefault(cap_key, []).append(step.id)
            for cap, step_ids_list in cap_counts.items():
                if len(step_ids_list) > 1:
                    warnings.append(
                        f"Multiple steps share capability '{cap}': "
                        f"{step_ids_list}. This causes scope routing "
                        f"ambiguity in Agent 3's test generation. "
                        f"Consider making capabilities unique per step "
                        f"(e.g., 'English to Spanish translation' instead "
                        f"of 'text translation' for all languages)."
                    )

    # ── TestPlan validation ──
    if r.workflow is not None and r.test_plan is None:
        warnings.append(
            "WorkflowBlueprint present but test_plan is None — Agent 3 "
            "will fall back to independent test generation without "
            "per-scope specs (quality may be lower)"
        )

    if r.test_plan is not None:
        tp = r.test_plan
        # Scope spec count must match step count
        if r.workflow is not None:
            step_ids = {s.id for s in r.workflow.steps}
            spec_ids = {s.scope_id for s in tp.scope_specs}
            missing_specs = step_ids - spec_ids
            extra_specs = spec_ids - step_ids
            if missing_specs:
                errors.append(
                    f"TestPlan missing scope_specs for steps: {sorted(missing_specs)}"
                )
            if extra_specs:
                warnings.append(
                    f"TestPlan has scope_specs for non-existent steps: {sorted(extra_specs)}"
                )

        # Total target sum check
        computed_sum = sum(s.test_count_target for s in tp.scope_specs)
        if computed_sum != tp.total_test_target:
            warnings.append(
                f"TestPlan total_test_target={tp.total_test_target} doesn't match "
                f"sum of scope targets={computed_sum}"
            )

        # Valid test_mode values
        valid_modes = {"file_based", "synthetic_text", "synthetic_structured"}
        for spec in tp.scope_specs:
            if spec.test_mode not in valid_modes:
                errors.append(
                    f"ScopeTestSpec '{spec.scope_id}' has invalid test_mode='{spec.test_mode}'"
                )

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

    # ── Phase 4: WorkflowBlueprint scope coverage ──
    # When Agent 1 produced a blueprint, check that every scope has at least
    # one candidate claiming coverage. Thin pools (<3) are warnings; fully
    # uncovered scopes are warnings too — Phase 6.5 still tries, but the
    # scope will end up with fewer verified candidates after deep-verify.
    #
    # Legacy flow (workflow=None or empty covers_step_ids everywhere) is
    # silent — the flat sub-task coverage check above already ran.
    bp = agent1_output.workflow
    if bp and bp.steps:
        any_coverage_populated = any(
            bool(c.covers_step_ids) for c in result.candidates
        )
        if any_coverage_populated:
            per_scope_counts: dict[str, int] = {s.id: 0 for s in bp.steps}
            for c in result.candidates:
                for sid in c.covers_step_ids:
                    if sid in per_scope_counts:
                        per_scope_counts[sid] += 1

            scope_role_by_id = {s.id: s.role for s in bp.steps}
            uncovered_scopes = [
                sid for sid, n in per_scope_counts.items() if n == 0
            ]
            thin_scopes = [
                (sid, n) for sid, n in per_scope_counts.items()
                if 0 < n < 3
            ]

            if uncovered_scopes:
                warnings.append(
                    "Blueprint scopes NOT covered by any candidate "
                    f"(Phase 6.5 will have nothing to deep-verify at these): "
                    + ", ".join(
                        f"{sid} ({scope_role_by_id.get(sid, '?')})"
                        for sid in uncovered_scopes
                    )
                )
            for sid, n in thin_scopes:
                warnings.append(
                    f"Thin coverage at scope '{sid}' "
                    f"(role={scope_role_by_id.get(sid, '?')}): only {n} candidate(s) "
                    "claim coverage. Target is >=3 per scope — expect fewer "
                    "tested candidates here after Phase 6.5 deep-verify."
                )

            # Phase 5: structural checks on pricing_breakdown when present.
            # Agent 2 normally leaves this None; kicks in for candidates
            # enriched after Phase 6.5 ships or for test fixtures that
            # stamp pricing. Null-safe — no effect when breakdown is None.
            for c in result.candidates:
                _check_pricing_breakdown(
                    c.name, c.covers_step_ids, c.pricing_breakdown,
                    errors, warnings,
                )

            # Every coverage_confidence key must align with covers_step_ids,
            # and every value must be 'claimed' or 'verified'. Violations
            # are structural bugs in Agent 2 output — error-level.
            valid_conf = {"claimed", "verified"}
            for c in result.candidates:
                cov_set = c.covers_step_ids
                for sid, conf in c.coverage_confidence.items():
                    if sid not in cov_set:
                        errors.append(
                            f"'{c.name}' has coverage_confidence['{sid}'] "
                            "but this scope is not in covers_step_ids — "
                            "drift between the two fields"
                        )
                    if conf not in valid_conf:
                        errors.append(
                            f"'{c.name}' has invalid coverage_confidence"
                            f"['{sid}']='{conf}' (must be 'claimed' or "
                            "'verified')"
                        )
                for sid in cov_set:
                    if sid not in c.coverage_confidence:
                        warnings.append(
                            f"'{c.name}' covers '{sid}' but has no "
                            "coverage_confidence entry — normalizer should "
                            "have filled this"
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

        # Phase 5: structural checks on pricing_breakdown when present.
        # Until Phase 6.5's 4B extraction ships, pricing_breakdown is
        # always None and this call is a no-op. ScreenedCandidate gains
        # covers_step_ids in Phase 6.5 — for Phase 5a we pass frozenset()
        # so the per_scope drift check is silently skipped (can't drift
        # against an empty scope set).
        vc_covers = getattr(vc, "covers_step_ids", frozenset())
        _check_pricing_breakdown(
            vc.name, vc_covers, vc.pricing_breakdown, errors, warnings,
        )

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
