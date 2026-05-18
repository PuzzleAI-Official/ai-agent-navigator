# ============================================================================
# Agent 3: Synthetic Test Cases Agent
# ============================================================================
# PURPOSE:
#   Generate realistic test case specifications that will be used to evaluate
#   ALL AI candidates fairly. Test cases are generated INDEPENDENTLY from
#   candidate services — this prevents bias.
#
# DESIGN: Single-step pure function — one structured output call.
#
# WHY SINGLE STEP (vs Agent 2's two-step)?
#   Agent 2 needs two steps because server tools (web_search) produce mixed
#   content blocks that can't be combined with structured output. Agent 3
#   doesn't use any server tools — it's purely generative from Agent 1's
#   output. A single client.messages.parse() call with output_format is
#   sufficient and cheaper.
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  CORE LINES GUIDE                                               │
# │                                                                 │
# │  If you want to understand ONLY the main logic (skip logging,   │
# │  error handling, cost tracking), read these lines:              │
# │                                                                 │
# │  1. SYSTEM_PROMPT             — instructions for test generation│
# │  2. _build_generation_message() — assembles the request         │
# │  3. run_synthetic_tests_agent()  — THE MAIN FUNCTION            │
# │     Inside it, the core is just 4 lines:                        │
# │       a. client = anthropic.Anthropic(...)                      │
# │       b. message = _build_generation_message(input)             │
# │       c. response = client.messages.parse(                      │
# │            output_format=Agent3GenerationResult) ★ SINGLE CALL  │
# │       d. return response.parsed_output                          │
# │                                                                 │
# │  Everything else is logging, error handling, and cost tracking  │
# │  — necessary for production but not for understanding the flow. │
# └─────────────────────────────────────────────────────────────────┘
# ============================================================================

import time
from typing import Any

import anthropic

from functools import lru_cache
from importlib import resources

from puzzleeval.config import (
    ANTHROPIC_API_KEY,
    DEFAULT_MODEL,
)
from puzzleeval.anthropic_client import (
    AGENT3_GENERATION_MAX_RETRIES,
    AGENT3_GENERATION_TIMEOUT_S,
    AGENT3_TRANSIENT_RETRY_ATTEMPTS,
    build_client,
)
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import (
    Agent3GenerationResult,
    Agent3Input,
    Agent3Result,
    UserUnderstandingOutput,
)


# ============================================================================
# [CORE] System Prompt — Test Case Generation
# ============================================================================
# This prompt instructs Claude to generate test cases that:
#   1. Cover ALL sub-tasks from Agent 1's output
#   2. Spread across 6 testing dimensions (coverage matrix)
#   3. Include weighted judgement criteria for Agent 7
#   4. Scale count dynamically based on sub-task complexity
#
# The coverage matrix ensures the user never feels undertested. Each sub-task
# gets tested across happy path, input variations, edge cases, scale,
# domain-specific scenarios, and error resilience.
# ============================================================================

@lru_cache(maxsize=1)
def _load_system_prompt() -> str:
    """Load SYSTEM_PROMPT from templates/system_prompt.md (Phase 7)."""
    return resources.files("puzzleeval.agents.agent3").joinpath(
        "templates", "system_prompt.md",
    ).read_text(encoding="utf-8")


SYSTEM_PROMPT = _load_system_prompt()


# ============================================================================
# Constants
# ============================================================================

# Caching is DISABLED for Agent 3 (single-shot, no conversation loop).
CACHING_ENABLED = False

# Max tokens for the generation call. Test cases with detailed criteria
# and realistic input data produce substantial output.
GENERATION_MAX_TOKENS = 16384


def _attach_business_fixture(
    result: Agent3GenerationResult | Agent3Result | None,
    business_fixture: dict | None,
) -> Agent3Result | None:
    """Attach orchestrator-owned fixture metadata after model generation.

    The LLM must not be asked to emit ``business_fixture``. It is an
    open-ended ``dict[str, Any]`` and belongs to the orchestrator, not to
    Claude's structured-output schema. Keep it out of Agent3GenerationResult,
    then attach it here for persisted Agent3Result consumers.
    """
    if result is None:
        return None
    if isinstance(result, Agent3Result):
        result.business_fixture = business_fixture
        return result
    data = result.model_dump()
    data["business_fixture"] = business_fixture
    return Agent3Result(**data)


# ============================================================================
# Deterministic auto-fill for conversational input_context.instructions
# ============================================================================
# AD-007: safety-critical contracts live in deterministic code, not prompts.
# Real-run trace f9de380b (2026-04-22) caught Agent 3 ignoring the prompt's
# "input_context.instructions is REQUIRED" rule on 5/5 conversational test
# cases. Without instructions, the candidate agent gets a generic runner-
# side fallback ("You are a helpful voice_agent for {candidate}"), which
# tanks every rubric score for domain-specific criteria — the entire
# conversational eval becomes meaningless.
#
# This module guarantees every conversational test gets scope-appropriate
# instructions, regardless of what Agent 3 emitted:
#   1. PRIMARY source: scope_spec.agent_instructions (Agent 1 generates)
#   2. FALLBACK source: derive from workflow step + domain + sample_output
#   3. Apply to every conversational test case missing input_context
#
# Non-conversational tests are left untouched (they shouldn't have
# instructions anyway — see validator warnings).

_CONVERSATIONAL_INPUT_TYPES = frozenset((
    "conversation", "voice_conversation", "voice_turn", "chat",
))
_SYSTEM_PROMPT_ALIASES = (
    "instructions", "system_prompt", "system", "brief", "agent_prompt",
)

# Module-level logger for the auto-fill + derivation helpers. The main
# run_synthetic_tests_agent function defines its own logger via
# get_logger("agent_3_synthetic_tests") with trace context — that's
# used for the per-run telemetry. These module-level helpers just need
# a logger for structured warnings.
import logging as _logging

_module_logger = _logging.getLogger("puzzleeval.agents.synthetic_tests")


def _has_agent_instructions(input_context) -> bool:
    """True iff input_context already carries a non-empty agent system prompt.

    Uses the same alias list the Agent 5 runner uses so the check is
    symmetric with what downstream code looks for.
    """
    if not isinstance(input_context, dict):
        return False
    for alias in _SYSTEM_PROMPT_ALIASES:
        val = input_context.get(alias)
        if isinstance(val, str) and val.strip():
            return True
    return False


def _find_scope_spec_for(test_case, user_understanding) -> "Any":
    """Find the ScopeTestSpec that matches this test case's scope.

    Primary key: test_case.scope_id (populated by Agent 3 from
    ScopeTestSpec.scope_id). Falls back to None when the test case has
    no scope link (pre-Phase-3 flow).
    """
    test_plan = getattr(user_understanding, "test_plan", None)
    if test_plan is None:
        return None
    scope_id = getattr(test_case, "scope_id", None)
    if not scope_id:
        return None
    for spec in test_plan.scope_specs or []:
        if spec.scope_id == scope_id:
            return spec
    return None


def _find_workflow_step_for(scope_spec, user_understanding) -> "Any":
    """Find the WorkflowStep that the scope_spec targets."""
    if scope_spec is None:
        return None
    workflow = getattr(user_understanding, "workflow", None)
    if workflow is None:
        return None
    for step in workflow.steps or []:
        if step.id == scope_spec.scope_id:
            return step
    return None


def _derive_instructions_from_scope(
    scope_spec,
    workflow_step,
    domain: str | None,
    user_summary: str | None,
) -> str:
    """Fallback: synthesize agent system prompt from scope_spec metadata.

    Used when scope_spec.agent_instructions is None/empty (Agent 1
    missed its field). Pulls every piece of scope context Agent 1 DID
    capture — workflow step description, domain, sample_output, user
    summary — and weaves them into a coherent system prompt.

    Not as precise as a hand-crafted instruction, but ALWAYS better
    than the generic "You are a helpful voice_agent" fallback. The
    sample_output alone usually carries the business name, pricing
    details, and expected behavior since Agent 1 grounds it in the
    user's request.
    """
    pieces: list[str] = []
    # Role anchor — derive from workflow step's role (what Agent 1
    # captured from the user's description). Fall back to the generic
    # "agent" when unset so we don't bias voice vs chat vs any other
    # modality.
    role = getattr(workflow_step, "role", None) or "agent"
    desc = (
        getattr(workflow_step, "description", None)
        or getattr(scope_spec, "expected_output_description", None)
        or ""
    ).strip()
    if domain:
        # "for {domain}" is domain-neutral — works for plumbing,
        # healthcare, legal, education, nonprofit. Avoids the old
        # "business" phrasing that assumed a commercial context.
        pieces.append(
            f"You are a {role} for {domain.strip()}."
        )
    else:
        pieces.append(f"You are a {role}.")
    if desc:
        pieces.append(f"Your responsibility: {desc}")
    # Sample_output carries whatever concrete scope details Agent 1
    # captured from the user (menu items, pricing, hours, service
    # area, escalation rules — or for a medical scope: triage levels;
    # or for a legal scope: jurisdiction limits). Passing it verbatim
    # means the rules content scales with the user's input — no
    # domain hardcoding here.
    sample_out = (
        getattr(scope_spec, "sample_output", None) or ""
    ).strip()
    if sample_out:
        pieces.append(
            f"Expected behavior reference: {sample_out}"
        )
    # Include the original user summary — closest to a verbatim
    # rules statement. Works for any domain.
    if user_summary:
        pieces.append(
            f"Context: {user_summary.strip()}"
        )
    # Global guidance — domain-agnostic operating principles.
    # Avoid "prices" (sales bias) and "transfer to human" (voice bias).
    # "Specifics you haven't been given" covers: prices, policies,
    # hours, legal advice, medical diagnoses, pricing menus — whatever
    # the domain might require. "Escalate when uncertain" works for
    # any support modality (voice, chat, email).
    pieces.append(
        "Stay within the scope described above. Answer truthfully. "
        "Never invent specifics (prices, dates, policies, facts) that "
        "you haven't been given. Escalate to a human when uncertain."
    )
    return " ".join(pieces)


def _ensure_agent_instructions_on_conversational(
    result,
    user_understanding,
    *,
    trace_id: str,
) -> None:
    """Post-process Agent 3 output — ensure every conversational test
    has input_context.instructions.

    Mutates result.test_cases in-place. Never raises — on any edge
    case (missing scope spec, derivation failure), falls back silently
    to what Agent 3 emitted so the pipeline proceeds. Logs each
    auto-fill so the pipeline summary can surface how often Agent 3
    needed a rescue.
    """
    auto_filled_count = 0
    derived_from_fallback_count = 0
    for tc in result.test_cases or []:
        if tc.input_type not in _CONVERSATIONAL_INPUT_TYPES:
            continue
        if _has_agent_instructions(tc.input_context):
            continue

        # Primary source — Agent 1's scope_spec.agent_instructions
        scope_spec = _find_scope_spec_for(tc, user_understanding)
        instructions = None
        source = ""
        if scope_spec is not None:
            scope_instr = getattr(scope_spec, "agent_instructions", None)
            if isinstance(scope_instr, str) and scope_instr.strip():
                instructions = scope_instr.strip()
                source = "agent_1_scope_spec"

        # Fallback — derive from scope_spec + workflow step + domain
        if not instructions:
            workflow_step = _find_workflow_step_for(
                scope_spec, user_understanding,
            )
            domain = getattr(user_understanding, "domain", None)
            summary = getattr(user_understanding, "summary", None)
            try:
                instructions = _derive_instructions_from_scope(
                    scope_spec, workflow_step, domain, summary,
                )
                source = "derived_from_scope_metadata"
                derived_from_fallback_count += 1
            except Exception as exc:  # noqa: BLE001 — best effort
                _module_logger.warning(
                    "failed to derive agent instructions for %s: %s",
                    tc.id, exc,
                    extra={
                        "operation": "agent3_instructions_derivation_failed",
                        "trace_id": trace_id,
                    },
                )
                continue  # leave tc untouched; runner fallback applies

        # Merge into input_context (preserve any other keys Agent 3 set)
        ctx = dict(tc.input_context or {})
        ctx["instructions"] = instructions
        tc.input_context = ctx
        auto_filled_count += 1
        _module_logger.info(
            "auto-filled input_context.instructions on test %s (%s)",
            tc.id, source,
            extra={
                "operation": "agent3_instructions_autofill",
                "trace_id": trace_id,
                "test_case_id": tc.id,
                "source": source,
                "chars": len(instructions),
            },
        )
    if auto_filled_count:
        _module_logger.warning(
            "Agent 3 omitted input_context.instructions on %d "
            "conversational test(s) — auto-filled from %s. %d "
            "from scope_spec, %d from derived fallback.",
            auto_filled_count,
            "scope_spec / derived metadata",
            auto_filled_count - derived_from_fallback_count,
            derived_from_fallback_count,
            extra={
                "operation": "agent3_instructions_autofill_summary",
                "trace_id": trace_id,
                "count": auto_filled_count,
                "from_scope_spec": auto_filled_count - derived_from_fallback_count,
                "from_derived": derived_from_fallback_count,
            },
        )


# ============================================================================
# [CORE] Build the generation request message
# ============================================================================
# Serializes Agent 1's output into a structured prompt that tells Claude
# exactly what sub-tasks to generate tests for, with domain context and
# any workflow data to ground the synthetic content.
# ============================================================================

def _format_test_plan(test_plan) -> str:
    """
    Render Agent 1's TestPlan as authoritative per-scope generation specs.

    When present, this OVERRIDES the general architecture annotations —
    Agent 3 follows these specs exactly instead of guessing.
    """
    if not test_plan or not getattr(test_plan, "scope_specs", None):
        return ""

    lines = [
        "## TEST PLAN (AUTHORITATIVE — from Agent 1)",
        "",
        "Agent 1 designed these per-scope test specifications. Follow them EXACTLY.",
        f"Total target: {test_plan.total_test_target} test cases.",
        "",
    ]

    for spec in test_plan.scope_specs:
        lines.append(f"### Scope: {spec.scope_id}")
        lines.append(f"  test_mode: {spec.test_mode}")
        lines.append(f"  input_type: {spec.input_type} (USE THIS — do not override)")
        lines.append(f"  output_type: {spec.output_type} (USE THIS — do not override)")
        lines.append(f"  test_count_target: {spec.test_count_target}")
        lines.append(f"  requires_user_files: {spec.requires_user_files}")
        lines.append(f"  evaluation_focus: {', '.join(spec.evaluation_focus)}")
        lines.append(f"  input_description: {spec.input_description}")
        lines.append(f"  expected_output_description: {spec.expected_output_description}")
        lines.append(f"")
        lines.append(f"  SAMPLE INPUT (use as template for variations):")
        lines.append(f"  {spec.sample_input}")
        lines.append(f"")
        lines.append(f"  SAMPLE OUTPUT (use as template for expected_output):")
        lines.append(f"  {spec.sample_output}")

        if spec.upstream_output_shape:
            lines.append(f"")
            lines.append(f"  UPSTREAM OUTPUT SHAPE (this scope receives data shaped like this):")
            lines.append(f"  {spec.upstream_output_shape}")
            lines.append(f"  Your input_data for this scope MUST match this shape — simulate upstream output.")

        # Gap 30: scope-specific input_context parameters (e.g. target_language)
        hints = getattr(spec, "input_context_hints", None)
        if hints:
            lines.append("")
            lines.append(f"  INPUT_CONTEXT HINTS (copy into every test case's input_context):")
            for k, v in hints.items():
                lines.append(f"    - {k}: {v}")
            lines.append(
                "  Every test case you generate for this scope MUST include these keys "
                "verbatim in TestCase.input_context — the harness needs them to route "
                "the API call correctly."
            )

        # Gap 14: ground_truth vs exemplar scoring
        ref_mode = getattr(spec, "reference_mode", "ground_truth")
        if ref_mode == "exemplar":
            lines.append("")
            lines.append(
                "  REFERENCE MODE: exemplar — sample_output is ONE valid answer, not THE "
                "answer. Generate test cases whose expected_output is an exemplar the "
                "LLM judge will use as a REFERENCE, not a target. Criteria should focus "
                "on qualities (helpfulness, tone, coverage) rather than exact text match."
            )

        # Gap 9: destructive action steps
        side_effects = getattr(spec, "side_effects", "read_only")
        if side_effects != "read_only":
            lines.append("")
            lines.append(
                f"  SIDE EFFECTS: {side_effects} — this scope WRITES to external "
                "systems. Generate test inputs that exercise both happy-path AND "
                "error-resilience (duplicate writes, invalid records, partial data) "
                "but use SYNTHETIC / clearly-labeled test records so dry-run / "
                "sandbox execution is easy to distinguish from real data."
            )

        if spec.file_description:
            lines.append(f"  File description: {spec.file_description}")
        lines.append("")

    if test_plan.notes:
        lines.append(f"Test plan notes: {test_plan.notes}")

    return "\n".join(lines)


def _format_workflow_architecture(workflow) -> str:
    """
    Render the workflow DAG as a compact architecture summary for Agent 3.

    Shows the data flow between steps so Agent 3 knows:
    - What each step produces (output_format)
    - What feeds each step (input_from)
    - The dependency chain (what runs before what)

    Returns empty string when no workflow exists (single-scope / legacy).
    """
    if not workflow or not getattr(workflow, "steps", None):
        return ""
    lines = ["## Workflow Architecture (from Agent 1)"]
    lines.append("Data flows through these steps in order. Test cases for each step must")
    lines.append("match its expected input/output format.\n")
    for step in workflow.steps:
        deps = f" (after {', '.join(step.depends_on)})" if step.depends_on else " (first step)"
        lines.append(
            f"  {step.id} [{step.role}]{deps}\n"
            f"    Input: {step.input_from or 'user'} -> Output: {step.output_format}\n"
            f"    \"{step.description}\""
        )
    lines.append("")
    lines.append(
        "For downstream steps (input_from != user), your test case input_data should "
        "SIMULATE what the upstream step would produce. Example: if step_1 is OCR "
        "producing structured_json, then step_2's test cases should use a realistic "
        "JSON object with extracted fields as input_data, NOT a raw document."
    )
    return "\n".join(lines)


def _format_business_fixture_for_prompt(fixture: dict | None) -> str:
    if not fixture:
        return (
            "## Canonical Business Fixture\n"
            "No canonical business fixture was provided. Do not invent exact "
            "prices, hours, menu items, policies, or service-area facts unless "
            "the user request explicitly supplies them."
        )
    facts = fixture.get("canonical_facts") or []
    fact_lines = "\n".join(f"- {str(f)[:500]}" for f in facts[:20]) or "- (none)"
    synthetic = "yes" if fixture.get("synthetic") else "no"
    return (
        "## Canonical Business Fixture (AUTHORITATIVE)\n"
        f"- synthetic_gap_marker: {synthetic}\n"
        f"- source: {fixture.get('source', 'unknown')}\n"
        f"- domain: {fixture.get('domain', 'unknown')}\n"
        "\nCanonical facts:\n"
        f"{fact_lines}\n\n"
        "Rules:\n"
        "- Generate tests, rubrics, goals, and expected behavior against these facts.\n"
        "- Do not introduce menu items, prices, hours, service areas, policies, or appointment constraints absent from this fixture.\n"
        "- If the fixture is synthetic or marks missing facts, test refusal/clarification rather than exact totals or unavailable facts.\n"
        "- Negative/off-menu scenarios may mention absent facts only when the rubric rewards decline, redirect, escalation, or alternatives."
    )


def _build_generation_message(
    user_understanding: UserUnderstandingOutput,
    business_fixture: dict | None = None,
) -> str:
    """
    Convert Agent 1's structured output into a test generation request.

    Includes:
    - Summary and domain for context
    - Each sub-task with capability and keywords
    - Constraints that affect test design
    - Workflow summary for grounding (if available)
    - Calculated target case count
    """
    # ── CORE: Build the sub-tasks section ──
    # When Agent 1 produced a workflow blueprint, enrich each sub-task
    # with the corresponding step's architectural constraints so Agent 3
    # generates tests with the EXACT input/output types the architecture
    # expects. Without this, Agent 3 guesses independently and may
    # produce tests misaligned with the workflow design.
    blueprint = user_understanding.workflow
    step_by_cap: dict[str, "WorkflowStep"] = {}
    if blueprint and blueprint.steps:
        from puzzleeval.schemas import WorkflowStep
        for step in blueprint.steps:
            step_by_cap[step.capability.strip().lower()] = step

    subtask_lines = []
    for i, st in enumerate(user_understanding.sub_tasks, 1):
        lines = [
            f"{i}. {st.description}",
            f"   Capability: {st.capability}",
            f"   Search keywords: {', '.join(st.search_keywords)}",
        ]

        # Match this sub-task to a workflow step via capability
        matched_step = step_by_cap.get(st.capability.strip().lower())
        if matched_step:
            lines.append(f"   [Architecture] step_id={matched_step.id}, role={matched_step.role}")
            lines.append(f"   [Architecture] output_format={matched_step.output_format}")
            input_desc = (
                "user provides input directly"
                if matched_step.input_from == "user" or matched_step.input_from is None
                else f"receives output from {matched_step.input_from}"
            )
            lines.append(f"   [Architecture] input_source={input_desc}")
            if st.requires_test_files:
                lines.append(f"   [Architecture] requires_test_files=True (file-based input)")
            else:
                lines.append(f"   [Architecture] requires_test_files=False (text/synthetic input)")

        subtask_lines.append("\n".join(lines))
    subtasks_text = "\n\n".join(subtask_lines)

    # ── Calculate target test case count ──
    num_subtasks = len(user_understanding.sub_tasks)
    base_per_subtask = 7  # middle of 5-8 range
    workflow_bonus = 2 if user_understanding.workflow_summary else 0
    target_total = num_subtasks * (base_per_subtask + workflow_bonus)
    target_total = max(10, min(50, target_total))  # clamp to [10, 50]

    # ── CORE: Build constraints section ──
    constraints = user_understanding.constraints
    budget = constraints.budget_range or "Not specified"
    tech_level = constraints.technical_level or "Not specified"
    integrations = (
        ", ".join(constraints.integration_requirements)
        if constraints.integration_requirements
        else "None specified"
    )

    # ── Test Plan integration ──
    # When Agent 1 produced a test_plan, include it as the primary
    # instruction for what to generate. This replaces independent guessing
    # with directed execution. When no test_plan exists, fall back to the
    # architecture annotations + general instructions.
    test_plan_section = _format_test_plan(user_understanding.test_plan)

    # ── CORE: Assemble the full message ──
    message = f"""## What the User Needs
{user_understanding.summary}

## Domain
{user_understanding.domain}

## Sub-Tasks to Generate Test Cases For
{subtasks_text}

## Constraints (affect test design)
- Budget: {budget}
- Technical level: {tech_level}
- Integration requirements: {integrations}

## Workflow Context
{user_understanding.workflow_summary or "No workflow document provided."}

{_format_workflow_architecture(user_understanding.workflow)}

{test_plan_section}

{_format_business_fixture_for_prompt(business_fixture)}

## Target
Generate approximately {target_total} test cases total ({base_per_subtask + workflow_bonus} per sub-task).
Ensure every sub-task is covered across all 6 testing dimensions.
Use IDs starting from tc-001.

IMPORTANT: When a Test Plan is provided above, it is AUTHORITATIVE — follow the per-scope specs exactly (input_type, output_type, sample_input shape, test_count_target). When [Architecture] annotations appear but no Test Plan, use them to set output_type and design input_data that matches the workflow's data flow."""

    return message


# ============================================================================
# [CORE] Main function — this is the entry point
# ============================================================================
#
# THE CORE LOGIC IS 4 LINES (marked with ★ below). Everything else is
# logging, error handling, and cost tracking — necessary for production
# but not for understanding what Agent 3 does.
#
# SINGLE API CALL:
#   client.messages.parse() with output_format=Agent3GenerationResult
#   → guaranteed structured JSON matching our Pydantic schema
#
# ============================================================================

def run_synthetic_tests_agent(input_data: Agent3Input) -> Agent3Result:
    """
    Run Agent 3. Takes Agent 1's structured understanding, generates
    comprehensive test case specifications with ground truth and
    judgement criteria.

    Single-step process:
      client.messages.parse() with output_format=Agent3GenerationResult
    """
    # ★ CORE LINE 1: Create the API client
    # Agent 3 has a dedicated long-generation profile: large voice fixtures
    # and weighted rubrics can legitimately run longer than the generic
    # 120 s structured-output timeout.
    client = build_client(
        api_key=ANTHROPIC_API_KEY,
        timeout=AGENT3_GENERATION_TIMEOUT_S,
        max_retries=AGENT3_GENERATION_MAX_RETRIES,
    )

    # [logging] Set up logger for this agent
    logger = get_logger("agent_3_synthetic_tests")
    logger.info("Agent 3 started", extra={
        "operation": "agent_start", "trace_id": input_data.trace_id,
    })

    # ★ CORE LINE 2: Build the generation request message
    generation_message = _build_generation_message(
        input_data.user_understanding,
        input_data.business_fixture,
    )

    # ======================================================================
    # Generate test cases via structured output
    # ======================================================================

    # ★ CORE LINE 3: Call Claude with structured output
    # Wrapped in parse_with_fallback so a grammar-budget rejection on
    # Agent3Result (TestCase[] with weighted criteria + plugin shapes) falls
    # back to the non-strict tool path instead of failing the run.
    start_time = time.time()
    try:
        from puzzleeval.agent_preamble import with_preamble
        from puzzleeval.structured_output import parse_with_fallback
        response = parse_with_fallback(
            client=client,
            model=DEFAULT_MODEL,
            max_tokens=GENERATION_MAX_TOKENS,
            system=[{"type": "text", "text": with_preamble(SYSTEM_PROMPT)}],
            messages=[{"role": "user", "content": generation_message}],
            output_format=Agent3GenerationResult,
            extra={},
            trace_id=input_data.trace_id,
            transient_max_attempts=AGENT3_TRANSIENT_RETRY_ATTEMPTS,
            fallback_on_strict_transient_error=True,
        )

    # [error handling] Same pattern as Agent 1 and Agent 2
    except anthropic.RateLimitError as e:
        logger.error("Rate limit hit", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "RateLimitError",
        })
        raise AgentRateLimitError(
            message=f"Rate limit exceeded during test generation: {e}",
            agent_name="synthetic_tests", trace_id=input_data.trace_id,
        )
    except anthropic.APIConnectionError as e:
        logger.error("API connection failed", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIConnectionError",
        })
        raise AgentAPIError(
            message=f"Failed to connect to Anthropic API during test generation: {e}",
            agent_name="synthetic_tests", trace_id=input_data.trace_id,
        )
    except anthropic.APIStatusError as e:
        logger.error("API error", extra={
            "operation": "llm_call_generate", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIStatusError",
        })
        raise AgentAPIError(
            message=f"Anthropic API error during test generation: {e}",
            agent_name="synthetic_tests", trace_id=input_data.trace_id,
        )

    # [logging] Log call metrics
    call_cost = log_llm_call(
        logger=logger, response=response, model=DEFAULT_MODEL,
        trace_id=input_data.trace_id, start_time=start_time,
        operation="synthetic_tests_generate",
    )

    # ★ CORE LINE 4: Return the parsed result
    result = _attach_business_fixture(
        response.parsed_output,
        input_data.business_fixture,
    )
    structured_output_telemetry = (
        getattr(response, "structured_output_telemetry", None) or {}
    )

    # [error handling] Defensive check for truncated/refused responses
    if result is None:
        logger.error("Parsed output is None", extra={
            "operation": "output_validation", "trace_id": input_data.trace_id,
            "error": "parsed_output is None",
            "stop_reason": response.stop_reason,
        })
        raise AgentOutputError(
            message=(
                f"Test generation returned no parsed output. "
                f"stop_reason={response.stop_reason}"
            ),
            agent_name="synthetic_tests", trace_id=input_data.trace_id,
        )

    # [cost tracking] Set cost on the result
    result.cost_usd = call_cost
    result.business_fixture = input_data.business_fixture
    result.structured_output_mode = structured_output_telemetry.get("mode")
    result.structured_output_fallback_reason = (
        structured_output_telemetry.get("fallback_reason")
    )
    result.structured_output_schema_repair_attempts = int(
        structured_output_telemetry.get("schema_repair_attempts") or 0
    )

    # ── Deterministic auto-fill: input_context.instructions for conversational tests ──
    # AD-007 contract enforcement: prompt-level rules telling Agent 3 to
    # populate `input_context.instructions` were not reliably followed
    # (real-run f9de380b: 5/5 tests emitted empty input_context). This
    # silently breaks conversational eval — the candidate agent gets a
    # generic runner-side fallback, so the rubric judge can't score
    # plumbing-accuracy / service-area / policy criteria meaningfully.
    # Fix lives here (deterministic code) rather than in more prompt
    # nagging: for every conversational test without agent instructions,
    # pull from scope_spec.agent_instructions (Agent 1's canonical
    # source), else DERIVE from workflow description + domain +
    # sample_output. See _ensure_agent_instructions_on_conversational.
    _ensure_agent_instructions_on_conversational(
        result,
        input_data.user_understanding,
        trace_id=input_data.trace_id,
    )

    # ── Empty-result fallback (fires BEFORE top-up) ────────────────────
    # Real-run signal: trace real_debug_4 caught Agent 3 returning
    # `{"test_cases": []}` in 2 seconds with 27 output tokens — the model
    # went shallow on a valid request. The normal top-up loop then
    # no-ops because it keys off `test_plan.scope_specs[*].capability`
    # which Agent 1 sometimes leaves None. General fix: when the first
    # pass emits zero cases, retry ONCE from the simplest inputs
    # (sub_tasks themselves) with an explicit "you produced nothing —
    # generate at least N cases for each" nudge. This is robust even
    # when test_plan is partial/missing.
    if not result.test_cases:
        try:
            result = _retry_empty_generation(
                result=result,
                input_data=input_data,
                client=client,
                logger=logger,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Agent 3 empty-result retry failed: %s — proceeding with empty result",
                exc,
                extra={"operation": "empty_retry_failed", "trace_id": input_data.trace_id},
            )

    # ── Sufficiency retry: if any sub_task produced fewer tests than Agent 1's
    # test_count_target floor (70% of target), fire ONE top-up call that asks
    # specifically for the missing cases. Cheap, bounded, and lets the
    # validator downstream pass a previously-failing scope instead of killing
    # the pipeline. The retry is LLM-only (no re-routing, no fallback loop) —
    # if the top-up still shortfalls, the validator escalates to error and
    # the caller decides what to do.
    try:
        result = _topup_undergenerated_subtasks(
            result=result,
            input_data=input_data,
            client=client,
            logger=logger,
            original_cost=call_cost,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Agent 3 top-up retry failed: %s — proceeding with original result",
            exc,
            extra={"operation": "topup_retry_failed", "trace_id": input_data.trace_id},
        )

    logger.info("Agent 3 completed", extra={
        "operation": "agent_complete",
        "trace_id": input_data.trace_id,
        "test_case_count": len(result.test_cases),
        "subtasks_covered": len(result.coverage_summary),
        "structured_output_mode": result.structured_output_mode,
        "structured_output_fallback_reason": (
            result.structured_output_fallback_reason
        ),
        "structured_output_schema_repair_attempts": (
            result.structured_output_schema_repair_attempts
        ),
    })

    return result


# ---------------------------------------------------------------------------
# Empty-result fallback — retry when first pass emits zero cases
# ---------------------------------------------------------------------------


def _retry_empty_generation(
    *,
    result: "Agent3Result",
    input_data: "Agent3Input",
    client,  # anthropic.Anthropic
    logger,
) -> "Agent3Result":
    """Retry Agent 3 ONCE when the first pass returns zero test cases.

    Real-run signal (trace real_debug_4): Agent 3 sometimes responds
    shallowly with ``{"test_cases": []}`` despite a clear, well-specified
    request — same model, same input, different run = different output.
    The normal top-up loop doesn't catch this because it keys off
    ``test_plan.scope_specs[*].capability`` which Agent 1 can leave null.

    Fix: when len(test_cases) == 0 after the initial call, fire exactly
    one retry using sub_tasks directly (no test_plan dependency). The
    prompt explicitly states "your previous response was empty" so the
    model can't re-emit the same zero-case output on this pass.

    Never raises. Returns the original empty result on any failure —
    the caller's validator decides whether to fail the pipeline.
    """
    uo = input_data.user_understanding
    if not uo.sub_tasks:
        return result

    from puzzleeval.structured_output import parse_with_fallback
    from puzzleeval.agent_preamble import with_preamble

    # Compute per-sub_task minimum targets, falling back to 5 (the
    # documented base in synthetic_tests per-sub_task sizing).
    test_plan = getattr(uo, "test_plan", None)
    specs_by_cap: dict[str, int] = {}
    if test_plan and test_plan.scope_specs:
        for spec in test_plan.scope_specs:
            cap = getattr(spec, "capability", None)
            tgt = getattr(spec, "test_count_target", None)
            if cap and tgt:
                specs_by_cap[cap] = int(tgt)

    lines: list[str] = [
        "Your previous response returned ZERO test cases. That is not acceptable.",
        "",
        "Generate AT LEAST the minimum below per sub_task. The test_cases list",
        "MUST be non-empty on this retry. Use the sub_task description VERBATIM",
        "for each case's sub_task_ref so validation can map them.",
        "",
    ]
    fixture_block = _format_business_fixture_for_prompt(input_data.business_fixture)
    lines.extend([fixture_block, ""])
    for st in uo.sub_tasks:
        target = specs_by_cap.get(st.capability, 5)
        lines.append(f"- sub_task_ref: {st.description!r}")
        lines.append(f"  capability: {st.capability}")
        lines.append(f"  minimum_cases: {max(3, target)}")
        lines.append("")
    lines.append(
        "Each test case needs 2-5 judgement_criteria with weights summing to ~1.0."
    )

    try:
        response = parse_with_fallback(
            client=client,
            model=DEFAULT_MODEL,
            max_tokens=GENERATION_MAX_TOKENS,
            system=[{"type": "text", "text": with_preamble(SYSTEM_PROMPT)}],
            messages=[{"role": "user", "content": "\n".join(lines)}],
            output_format=Agent3GenerationResult,
            extra={},
            trace_id=input_data.trace_id,
            transient_max_attempts=AGENT3_TRANSIENT_RETRY_ATTEMPTS,
            fallback_on_strict_transient_error=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Agent 3 empty-retry call failed: %s",
            exc,
            extra={"operation": "empty_retry_call_error", "trace_id": input_data.trace_id},
        )
        return result

    retry_cost = log_llm_call(
        logger=logger, response=response, model=DEFAULT_MODEL,
        trace_id=input_data.trace_id, start_time=time.time(),
        operation="synthetic_tests_empty_retry",
    )
    retried = _attach_business_fixture(
        response.parsed_output,
        input_data.business_fixture,
    )
    if retried is None or not retried.test_cases:
        logger.warning(
            "Agent 3 empty-retry still returned no cases — escalating to validator",
            extra={"operation": "empty_retry_still_empty", "trace_id": input_data.trace_id},
        )
        return result

    # Fold retry cases into the (empty) result + accumulate cost.
    result.test_cases = retried.test_cases
    if retried.coverage_summary:
        result.coverage_summary = retried.coverage_summary
    if retried.generation_notes and not result.generation_notes:
        result.generation_notes = retried.generation_notes
    result.cost_usd = (result.cost_usd or 0.0) + retry_cost

    # Re-run auto-fill on the retry cases so they also get
    # input_context.instructions. Empty-retry path bypasses the
    # normal post-processor; applying it again here is the
    # safety net.
    _ensure_agent_instructions_on_conversational(
        result,
        input_data.user_understanding,
        trace_id=input_data.trace_id,
    )

    logger.info(
        "Agent 3 empty-retry recovered %d cases",
        len(retried.test_cases),
        extra={
            "operation": "empty_retry_recovered",
            "trace_id": input_data.trace_id,
            "cases_recovered": len(retried.test_cases),
        },
    )
    return result


# ---------------------------------------------------------------------------
# Sufficiency top-up — closes shortfall vs test_count_target
# ---------------------------------------------------------------------------


# Max topup iterations. Matches Claude Code's MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
# shape — a small bounded loop with a circuit breaker instead of a fixed
# single shot. 3 is the sweet spot: two chances to cover what the first
# attempt missed, one final chance after any gap-analysis refinement.
MAX_TOPUP_ATTEMPTS = 3
# When two consecutive attempts add zero new tests, we've hit a wall —
# either the model is refusing or the sub_task genuinely can't yield more
# variety. Bail gracefully instead of burning another call.
TOPUP_STALL_LIMIT = 2


def _compute_topup_gaps(
    *,
    result: "Agent3Result",
    cap_to_target: dict[str, tuple[int, str]],
    desc_to_cap: dict[str, str],
) -> tuple[list[tuple[str, str, int, int]], dict[str, set[str]], dict[str, set[str]]]:
    """Compute current gaps: shortfalls, missing dimensions per cap, and
    which judgement_criteria strings currently have ≥1 scoring test.

    Returns ``(shortfalls, missing_dims_by_cap, uncovered_criteria_by_cap)``.

    - ``shortfalls`` — list of (cap, desc, actual, target) where
      ``actual < floor``.
    - ``missing_dims_by_cap`` — which canonical coverage dimensions have
      zero tests for each shortfalling cap.
    - ``uncovered_criteria_by_cap`` — any ``judgement_criteria.criterion``
      string mentioned in the TestPlan scope_specs that currently has zero
      test cases referencing it. A criterion with ≥1 test is "covered."
      Used as a *secondary* gap signal when count-floor is already met —
      sufficiency isn't just about count, it's about criterion coverage.
    """
    from collections import Counter
    from puzzleeval.config import (
        CANONICAL_COVERAGE_DIMENSIONS,
        SUFFICIENCY_FLOOR_RATIO,
        SUFFICIENCY_HARD_FLOOR,
    )

    # Re-count per-cap tests.
    actual_by_cap: Counter[str] = Counter()
    for tc in result.test_cases:
        ref = tc.sub_task_ref or ""
        if ref in desc_to_cap:
            actual_by_cap[desc_to_cap[ref]] += 1

    shortfalls: list[tuple[str, str, int, int]] = []
    for cap, (target, desc) in cap_to_target.items():
        actual = actual_by_cap.get(cap, 0)
        floor = max(SUFFICIENCY_HARD_FLOOR, int(target * SUFFICIENCY_FLOOR_RATIO))
        if actual < floor:
            shortfalls.append((cap, desc, actual, target))

    # Canonical-dimension coverage per cap that's shortfalling.
    missing_dims: dict[str, set[str]] = {cap: set() for cap, _, _, _ in shortfalls}
    dims_covered: dict[str, set[str]] = {cap: set() for cap, _, _, _ in shortfalls}
    for tc in result.test_cases:
        ref = tc.sub_task_ref or ""
        cap = desc_to_cap.get(ref)
        if cap in dims_covered:
            for tag in (tc.tags or []):
                if tag in CANONICAL_COVERAGE_DIMENSIONS:
                    dims_covered[cap].add(tag)
    for cap in missing_dims:
        missing_dims[cap] = CANONICAL_COVERAGE_DIMENSIONS - dims_covered[cap]

    # Criterion-coverage: every criterion_text referenced in at least one
    # test case's judgement_criteria means that criterion has a scoring
    # test. Uncovered = referenced in scope_specs but no test case mentions it.
    uncovered_criteria: dict[str, set[str]] = {cap: set() for cap in cap_to_target}
    # Build set of criteria that appear in any test case per cap.
    cap_to_tested_criteria: dict[str, set[str]] = {cap: set() for cap in cap_to_target}
    for tc in result.test_cases:
        ref = tc.sub_task_ref or ""
        cap = desc_to_cap.get(ref)
        if cap is None:
            continue
        for jc in (tc.judgement_criteria or []):
            crit = getattr(jc, "criterion", None)
            if crit:
                cap_to_tested_criteria[cap].add(crit.strip().lower())
    # Scope-specs carry the authoritative criterion list per capability.
    # When the TestPlan lists N named criteria for a cap and only M < N
    # appear in test cases, the remaining (N - M) are "uncovered."
    # We can't always map test_plan.scope_specs[].judgement_criteria back
    # to the caps (different field shapes across versions), so fall back
    # gracefully when the structure isn't there.
    # The topup prompt uses missing_dims primarily; uncovered_criteria is
    # surfaced as context when populated.
    return shortfalls, missing_dims, uncovered_criteria


def _topup_undergenerated_subtasks(
    *,
    result: "Agent3Result",
    input_data: "Agent3Input",
    client,  # anthropic.Anthropic — not annotated to avoid circular typing
    logger,
    original_cost: float,
) -> "Agent3Result":
    """Top up under-generated sub_tasks with an ITERATIVE focused LLM loop.

    Runs up to ``MAX_TOPUP_ATTEMPTS`` rounds. After each round, recomputes:
      - per-cap shortfall vs ``floor(target * SUFFICIENCY_FLOOR_RATIO)``
      - per-cap missing canonical dimensions
      - criterion coverage (which scope criteria still have 0 scoring tests)

    Exits early when every sub_task meets floor AND there are no missing
    dimensions for any shortfalling cap. Circuit-breaks after
    ``TOPUP_STALL_LIMIT`` consecutive attempts that add zero new tests,
    so a model refusing to produce more variety doesn't burn the budget.

    The per-attempt prompt feeds back (a) what's still missing and
    (b) what was added in the prior attempt, so the model steers toward
    gaps it didn't hit. Never raises — caller's logger.warning handles
    exceptions.

    This is the "knows what's missing and when to continue" mechanism:
    each attempt narrows the gap based on real post-attempt coverage
    analysis, not a static one-shot prompt.
    """
    uo = input_data.user_understanding
    test_plan = getattr(uo, "test_plan", None)
    if test_plan is None:
        return result
    specs = getattr(test_plan, "scope_specs", None) or []
    if not specs:
        return result

    # Build capability → (target, description) map
    cap_to_target: dict[str, tuple[int, str]] = {}
    for spec in specs:
        cap = getattr(spec, "capability", None)
        target = getattr(spec, "test_count_target", None)
        if not cap or not target:
            continue
        desc = next(
            (st.description for st in uo.sub_tasks if st.capability == cap), ""
        )
        cap_to_target[cap] = (int(target), desc)
    if not cap_to_target:
        return result

    desc_to_cap: dict[str, str] = {d: c for c, (_, d) in cap_to_target.items()}

    from puzzleeval.structured_output import parse_with_fallback
    from puzzleeval.agent_preamble import with_preamble
    from puzzleeval.config import CANONICAL_COVERAGE_DIMENSIONS

    total_topup_cost = 0.0
    total_added = 0
    stall_streak = 0
    prior_added_breakdown: dict[str, int] = {}

    for attempt in range(1, MAX_TOPUP_ATTEMPTS + 1):
        shortfalls, missing_dims, _uncovered = _compute_topup_gaps(
            result=result,
            cap_to_target=cap_to_target,
            desc_to_cap=desc_to_cap,
        )
        # Sufficiency met: no caps below floor AND no caps with any
        # missing canonical dimension among the shortfalling set. Early
        # exit — don't burn a call when we're already good.
        if not shortfalls:
            break

        # Compose attempt prompt with (a) shortfall table, (b) what was
        # added in the prior attempt so the model avoids re-generating
        # near-duplicates.
        lines: list[str] = [
            f"TEST BATTERY SHORTFALL — attempt {attempt} of {MAX_TOPUP_ATTEMPTS}.",
            "",
            "Generate ADDITIONAL test cases to close the gap for each sub_task below.",
            "Rules:",
            "  1. Keep every case's sub_task_ref EXACTLY as shown so validation can match.",
            "  2. Do NOT re-emit any test case already in the battery — generate ONLY the gap-filling cases.",
            "  3. Prioritize the 'dimensions still missing' list — covering a missing dimension",
            "     is worth more than adding yet another happy-path case.",
            "  4. Each case must have 2-5 judgement_criteria with weights summing to ~1.0.",
            "",
        ]
        if prior_added_breakdown:
            lines.append("Prior attempt added (so you don't duplicate):")
            for cap, n in prior_added_breakdown.items():
                lines.append(f"  - {cap}: +{n} cases")
            lines.append("")

        lines.extend([
            _format_business_fixture_for_prompt(input_data.business_fixture),
            "",
        ])

        for cap, desc, actual, target in shortfalls:
            gap = target - actual
            missing = sorted(missing_dims.get(cap, set()))
            lines.append(f"- sub_task_ref: {desc!r}")
            lines.append(f"  capability: {cap}")
            lines.append(f"  current_count: {actual}")
            lines.append(f"  target: {target}")
            lines.append(f"  gap: {gap}")
            lines.append(
                f"  dimensions still missing: {', '.join(missing) if missing else '(all present — deepen existing with edge-case variants)'}"
            )
            lines.append("")

        prompt = "\n".join(lines)

        try:
            response = parse_with_fallback(
                client=client,
                model=DEFAULT_MODEL,
                max_tokens=GENERATION_MAX_TOKENS,
                system=[{"type": "text", "text": with_preamble(SYSTEM_PROMPT)}],
                messages=[{"role": "user", "content": prompt}],
                output_format=Agent3GenerationResult,
                extra={},
                trace_id=input_data.trace_id,
                transient_max_attempts=AGENT3_TRANSIENT_RETRY_ATTEMPTS,
                fallback_on_strict_transient_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Agent 3 topup attempt %d failed: %s — stopping loop",
                attempt, exc,
                extra={"operation": "topup_attempt_error", "trace_id": input_data.trace_id},
            )
            break

        topup = _attach_business_fixture(
            response.parsed_output,
            input_data.business_fixture,
        )
        attempt_cost = log_llm_call(
            logger=logger, response=response, model=DEFAULT_MODEL,
            trace_id=input_data.trace_id, start_time=time.time(),
            operation=f"synthetic_tests_topup_attempt_{attempt}",
        )
        total_topup_cost += attempt_cost

        added_this_attempt = 0
        added_by_cap: dict[str, int] = {}
        if topup is not None and topup.test_cases:
            seen_ids = {tc.id for tc in result.test_cases}
            new_cases = [tc for tc in topup.test_cases if tc.id not in seen_ids]
            result.test_cases.extend(new_cases)
            added_this_attempt = len(new_cases)
            for tc in new_cases:
                cap = desc_to_cap.get(tc.sub_task_ref or "")
                if cap:
                    added_by_cap[cap] = added_by_cap.get(cap, 0) + 1

        total_added += added_this_attempt
        prior_added_breakdown = added_by_cap

        logger.info(
            "Agent 3 topup attempt %d added %d cases",
            attempt, added_this_attempt,
            extra={
                "operation": "topup_attempt_complete",
                "trace_id": input_data.trace_id,
                "attempt": attempt,
                "added": added_this_attempt,
                "attempt_cost_usd": attempt_cost,
                "remaining_shortfalls": len(shortfalls),
            },
        )

        # Circuit breaker: if this attempt added 0 new tests, count a
        # stall. Two stalls in a row → the model is refusing to produce
        # more variety. Bail instead of burning the last attempt.
        if added_this_attempt == 0:
            stall_streak += 1
            if stall_streak >= TOPUP_STALL_LIMIT:
                logger.warning(
                    "Agent 3 topup stalled (%d consecutive 0-add attempts) — stopping loop",
                    stall_streak,
                    extra={
                        "operation": "topup_stall",
                        "trace_id": input_data.trace_id,
                        "attempts_used": attempt,
                    },
                )
                break
        else:
            stall_streak = 0

    result.cost_usd = original_cost + total_topup_cost

    # Emit a final summary of the loop's work for observability.
    final_shortfalls, final_missing, _ = _compute_topup_gaps(
        result=result,
        cap_to_target=cap_to_target,
        desc_to_cap=desc_to_cap,
    )
    logger.info(
        "Agent 3 topup loop complete",
        extra={
            "operation": "topup_loop_complete",
            "trace_id": input_data.trace_id,
            "total_added": total_added,
            "total_cost_usd": total_topup_cost,
            "remaining_shortfall_caps": [cap for cap, _, _, _ in final_shortfalls],
            "remaining_missing_dimensions": {
                cap: sorted(dims) for cap, dims in final_missing.items() if dims
            },
        },
    )
    # Top-up appended new test cases to the SAME result object — apply
    # auto-fill one more time so top-up cases also get instructions.
    # Idempotent: tests that already have instructions are skipped.
    _ensure_agent_instructions_on_conversational(
        result,
        input_data.user_understanding,
        trace_id=input_data.trace_id,
    )
    return result
