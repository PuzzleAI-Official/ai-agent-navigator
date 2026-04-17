# ============================================================================
# CLI Runner — Test agents from the terminal
# ============================================================================
# Usage:
#   # Agent 1 only (default):
#   python -m puzzleeval.cli --text "I need an AI for customer support"
#
#   # Agent 1 → Agent 2 pipeline:
#   python -m puzzleeval.cli --text "I need an AI for customer support" --agent2
#
#   # Agent 1 → Agent 3 pipeline:
#   python -m puzzleeval.cli --text "I need invoice OCR" --agent3
#
# Output: stdout = agent JSON, stderr = structured logs
#   python -m puzzleeval.cli --text "..." --agent2 > result.json 2> logs.jsonl
#
# Pipeline runs are saved to runs/{trace_id}/ with intermediate outputs,
# validation results, and a pipeline_summary.json for debugging.
# ============================================================================

import argparse
import json
import sys
import time

# Fix Windows stdout encoding for Unicode output (arrow chars, etc.)
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# ----------------------------------------------------------------------
# Auto-load .env so plugin credentials (OpenAI Whisper, Deepgram,
# ElevenLabs, etc.) are available without the user manually exporting
# them. Searches up from CWD for the first .env. Mirrors what FastAPI
# does in puzzleeval-api/main.py — keeps the CLI workflow symmetrical.
# Existing os.environ values WIN over .env (override=False).
# ----------------------------------------------------------------------
def _autoload_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        # python-dotenv not installed — silent skip; users can still export
        # env vars manually. We don't crash on missing optional dep.
        return
    from pathlib import Path
    candidates = []
    cwd = Path.cwd().resolve()
    for parent in [cwd] + list(cwd.parents):
        candidates.append(parent / ".env")
        # Also check sibling puzzleeval-api/.env which is where the
        # FastAPI .env lives in the standard repo layout
        candidates.append(parent / "puzzleeval-api" / ".env")
    for path in candidates:
        if path.exists() and path.is_file():
            load_dotenv(path, override=False)
            break

_autoload_dotenv()

from puzzleeval.logging_setup import generate_trace_id, setup_logging, get_logger
from puzzleeval.pipeline import PipelineRun
from puzzleeval.schemas import (
    Agent1Input, Agent2Input, Agent3Input, Agent4Input, Agent5Input,
)
from puzzleeval.validators import (
    validate_agent1_output,
    validate_agent2_output,
    validate_agent3_output,
    validate_agent4_output,
    validate_agent5_output,
)
from puzzleeval.agents.user_understanding import run_user_understanding_agent
from puzzleeval.agents.research import (
    run_research_agent,
    apply_scope_picks,
)
from puzzleeval.agents.synthetic_tests import run_synthetic_tests_agent
from puzzleeval.agents.synthetic_tests_file import run_file_tests_agent
from puzzleeval.agents.screening import run_screening_agent
from puzzleeval.provider_registry import load_registry, get_all_credentials
from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

# Max conversation turns before forcing a result.
# The agent should decide to stop earlier on its own in most cases.
MAX_TURNS = 4


def _filter_user_understanding(uo, subtasks_to_keep):
    """
    Create a copy of UserUnderstandingOutput with only the specified sub-tasks.

    Used by the mixed-mode test generation to give Agent 3F only the file
    sub-tasks and Agent 3 only the text sub-tasks. Each agent generates
    test cases scoped to its sub-tasks, then results merge.
    """
    from puzzleeval.schemas import UserUnderstandingOutput
    keep_descs = {st.description for st in subtasks_to_keep}
    filtered_subtasks = [st for st in uo.sub_tasks if st.description in keep_descs]
    # Keep everything else intact (summary, domain, keywords, constraints, workflow)
    return UserUnderstandingOutput(
        summary=uo.summary,
        sub_tasks=filtered_subtasks,
        search_strategy=uo.search_strategy,
        domain=uo.domain,
        search_keywords=uo.search_keywords,
        constraints=uo.constraints,
        workflow_summary=uo.workflow_summary,
        workflow=uo.workflow,
    )


def _merge_agent3_results(file_result, text_result):
    """
    Merge Agent 3F (file-based) and Agent 3 (synthetic) results into
    one Agent3Result. Handles cases where either or both are None.
    """
    from puzzleeval.schemas import Agent3Result

    if file_result and text_result:
        merged_cases = file_result.test_cases + text_result.test_cases
        merged_coverage = dict(file_result.coverage_summary)
        merged_coverage.update(text_result.coverage_summary)
        merged_notes = (
            f"Mixed-mode generation: {len(file_result.test_cases)} file-based "
            f"+ {len(text_result.test_cases)} synthetic. "
            f"File: {file_result.generation_notes[:100]}... "
            f"Synthetic: {text_result.generation_notes[:100]}..."
        )
        return Agent3Result(
            test_cases=merged_cases,
            generation_notes=merged_notes,
            coverage_summary=merged_coverage,
            cost_usd=file_result.cost_usd + text_result.cost_usd,
        )
    elif file_result:
        return file_result
    elif text_result:
        return text_result
    else:
        # Should never happen — at least one agent always runs
        return Agent3Result(
            test_cases=[],
            generation_notes="No test cases generated (no sub-tasks).",
            coverage_summary={},
            cost_usd=0.0,
        )


def _cli_user_selection_pause(
    a2_result,
    blueprint,
    no_interactive: bool,
    logger,
    trace_id: str,
):
    """
    Phase 6: CLI-side pause for per-scope candidate picking. Mirrors the
    API's SelectionPanel flow but runs against stdin/stderr.

    Behavior:
      - `--no-interactive` OR `PUZZLEEVAL_USER_SELECTION_ENABLED=0` → pure
        pass-through (every Agent 2 candidate tested at every scope it
        claimed — legacy behavior).
      - No blueprint / 0-scope blueprint → pass-through (nothing to pick
        per-scope).
      - No candidates → pass-through (no picks possible).
      - Otherwise: prints the Agent 2 candidate pool grouped by scope, then
        prompts for each scope:
            [y] keep all   [n] drop all   [i1,i3,i5] keep these indices
        Submits the user's picks via apply_scope_picks.

    Registry-based auto-add is GONE (Phase 6 retires the shim). Users who
    want registry providers in automated runs add them via the API's
    SelectionPanel UI or via a scripted select-candidates POST; the CLI
    now treats every run as "the user has full manual control over which
    candidates get tested."

    Returns a NEW Agent2Result. Never mutates the input.
    """
    from puzzleeval.config import USER_SELECTION_ENABLED

    if not USER_SELECTION_ENABLED or no_interactive:
        logger.info("Phase 6 user selection skipped (flag off or --no-interactive)", extra={
            "operation": "phase_6_skip", "trace_id": trace_id,
        })
        return apply_scope_picks(a2_result)  # pass-through

    if not blueprint or not getattr(blueprint, "steps", None):
        logger.info("Phase 6 skipped — no multi-scope blueprint", extra={
            "operation": "phase_6_skip", "trace_id": trace_id,
        })
        return apply_scope_picks(a2_result)

    if not a2_result.candidates:
        logger.warning("Phase 6 skipped — no Agent 2 candidates to pick from", extra={
            "operation": "phase_6_skip", "trace_id": trace_id,
        })
        return apply_scope_picks(a2_result)

    # Group candidates by scope for display
    print("\n" + "=" * 60, file=sys.stderr)
    print("Phase 6: Candidate Selection (per scope)", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(file=sys.stderr)

    scope_picks: dict[str, list[str]] = {}
    for step in blueprint.steps:
        candidates_here = [
            c for c in a2_result.candidates
            if step.id in c.covers_step_ids
        ]
        print(
            f"  Scope {step.id} ({step.role}): "
            f"{len(candidates_here)} candidate(s) claiming coverage",
            file=sys.stderr,
        )
        if not candidates_here:
            print("    (no candidates cover this scope)", file=sys.stderr)
            scope_picks[step.id] = []
            continue
        for idx, c in enumerate(candidates_here, 1):
            print(
                f"    [{idx}] {c.name:<30} ({c.provider})  "
                f"relevance={c.relevance_score:.2f}",
                file=sys.stderr,
            )
        prompt = (
            f"  Pick for {step.id}: [y]=keep all, [n]=drop all, "
            f"comma-separated indices (e.g. 1,3): "
        )
        try:
            response = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  No input — keeping all candidates at this scope.", file=sys.stderr)
            response = "y"
        scope_picks[step.id] = _parse_scope_response(response, candidates_here)
        print(
            f"  → Kept {len(scope_picks[step.id])} candidate(s) at {step.id}",
            file=sys.stderr,
        )
        print(file=sys.stderr)

    # Summarize the decision
    total_unique_picks = len({
        name for names in scope_picks.values() for name in names
    })
    print(f"Summary: {total_unique_picks} unique candidates across all scopes",
          file=sys.stderr)
    print("=" * 60 + "\n", file=sys.stderr)

    logger.info("Phase 6 user selection applied", extra={
        "operation": "phase_6_applied", "trace_id": trace_id,
        "scope_picks": {k: len(v) for k, v in scope_picks.items()},
        "total_unique_picks": total_unique_picks,
    })

    return apply_scope_picks(a2_result, scope_picks=scope_picks)


def _parse_scope_response(response: str, candidates_here) -> list[str]:
    """Map a CLI response ('y' / 'n' / '1,3,5') to the kept candidate names."""
    if not response or response == "y":
        return [c.name for c in candidates_here]
    if response == "n":
        return []
    try:
        indices = [int(x.strip()) for x in response.split(",") if x.strip()]
    except ValueError:
        # Unparseable — default to keep all so the user doesn't accidentally
        # drop everything with a typo.
        return [c.name for c in candidates_here]
    kept: list[str] = []
    for i in indices:
        if 1 <= i <= len(candidates_here):
            kept.append(candidates_here[i - 1].name)
    return kept


def _print_validation(agent_name: str, validation, logger, trace_id: str):
    """Print validation results to stderr and log them."""
    if validation.errors:
        print(f"\n  !! {agent_name} VALIDATION ERRORS:", file=sys.stderr)
        for err in validation.errors:
            print(f"     - {err}", file=sys.stderr)
        logger.warning(f"{agent_name} validation failed", extra={
            "operation": "validation_failed", "trace_id": trace_id,
            "errors": validation.errors,
        })
    if validation.warnings:
        print(f"\n  >> {agent_name} warnings:", file=sys.stderr)
        for warn in validation.warnings:
            print(f"     - {warn}", file=sys.stderr)
        logger.info(f"{agent_name} validation warnings", extra={
            "operation": "validation_warnings", "trace_id": trace_id,
        })
    if validation.passed and not validation.warnings:
        print(f"  >> {agent_name} validation: PASSED", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="PuzzleEval — AI Agent Evaluation Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Agent 1 only:
  python -m puzzleeval.cli --text "I need an AI for customer support on Shopify"

  # Agent 1 → Agent 2 pipeline:
  python -m puzzleeval.cli --text "I need invoice OCR for my accounting workflow" --agent2

  # Agent 1 → Agent 3 pipeline (text-only test generation):
  python -m puzzleeval.cli --text "I need a chatbot" --agent3

  # Agent 1 → Agent 3/3F pipeline (with test files):
  python -m puzzleeval.cli --text "I need invoice OCR" --agent3 --test-files invoice1.jpg,invoice2.pdf

  # With options:
  python -m puzzleeval.cli --text "..." --file workflow.pdf --pretty --agent2
  python -m puzzleeval.cli --text "..." --agent2 > result.json 2> logs.jsonl
        """,
    )

    parser.add_argument("--text", required=False, default=None, help="Your AI needs description")
    parser.add_argument("--file", default=None, help="Optional: workflow file path")
    parser.add_argument("--trace-id", default=None, help="Optional: custom trace ID")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--no-interactive", action="store_true", help="Skip follow-up questions")
    parser.add_argument(
        "--agent2", action="store_true",
        help="Run Agent 2 (Research) after Agent 1 completes. Searches the web for AI candidates.",
    )
    parser.add_argument(
        "--agent3", action="store_true",
        help="Run Agent 3 (Synthetic Tests) after Agent 1 completes. Generates test case specifications.",
    )
    parser.add_argument(
        "--agent4", action="store_true",
        help="Run Agent 4 (Screening) after Agent 2. Verifies API accessibility for each candidate.",
    )
    parser.add_argument(
        "--agent5", action="store_true",
        help=(
            "Run Agent 5 (Implement Test Env) after Agent 4. Builds test harnesses "
            "for each validated candidate. Implies --agent4 and --agent2. Also runs "
            "Agent 3 for test case format context."
        ),
    )
    parser.add_argument(
        "--test-files", default=None,
        help="Comma-separated file paths for file-based testing (invoices, receipts, etc.)",
    )
    parser.add_argument(
        "--agent5-input", default=None,
        help=(
            "Path to a saved agent_5_input.json file. Skips Agents 1-4 and runs "
            "Agent 5 directly from saved input. Useful for re-testing Agent 5 changes "
            "without re-running the full pipeline. Creates a new run folder."
        ),
    )
    args = parser.parse_args()

    setup_logging()
    logger = get_logger("cli")

    # ---------------------------------------------------------------
    # Fast path: --agent5-input runs Agent 5 directly from saved input
    # ---------------------------------------------------------------
    if args.agent5_input:
        import shutil
        from pathlib import Path

        input_path = Path(args.agent5_input)
        if not input_path.exists():
            print(f"Error: {input_path} does not exist", file=sys.stderr)
            sys.exit(1)

        raw = json.loads(input_path.read_text(encoding="utf-8"))
        old_trace_id = raw.get("trace_id", "unknown")
        trace_id = generate_trace_id()
        raw["trace_id"] = trace_id

        logger.info("CLI started (agent5-input mode)", extra={
            "operation": "cli_start", "trace_id": trace_id,
            "original_trace_id": old_trace_id,
        })

        pipeline_run = PipelineRun(trace_id)

        # Copy upstream outputs from original run
        old_run_dir = input_path.parent
        new_run_dir = Path("runs") / trace_id
        new_run_dir.mkdir(parents=True, exist_ok=True)
        for fname in old_run_dir.iterdir():
            if fname.is_file() and fname.name.startswith("agent_") and not fname.name.startswith("agent_5"):
                shutil.copy2(fname, new_run_dir / fname.name)

        # Refresh credentials from the CURRENT registry (not stale saved ones).
        # The saved agent_5_input.json may have old credentials from a previous run.
        # Always reload from provider_registry.json to pick up any updates.
        agent5_input_obj = Agent5Input(**raw)
        registry = load_registry()
        fresh_creds = get_all_credentials(registry, agent5_input_obj.validated_candidates)
        if fresh_creds:
            raw["provider_credentials"] = fresh_creds
            print("  Credentials refreshed from current provider_registry.json", file=sys.stderr)

        agent5_input = Agent5Input(**raw)
        # Save input file to new run folder (with refreshed credentials)
        (new_run_dir / "agent_5_input.json").write_text(
            json.dumps(raw, indent=2, default=str), encoding="utf-8",
        )

        print(f"\n  Original run: {old_trace_id}", file=sys.stderr)
        print(f"  New trace ID: {trace_id}", file=sys.stderr)
        print(f"  Candidates: {len(agent5_input.validated_candidates)}", file=sys.stderr)
        for c in agent5_input.validated_candidates:
            print(f"    - {c.name} ({c.provider}) [score={c.relevance_score}]", file=sys.stderr)
        cred_count = len(agent5_input.provider_credentials) if agent5_input.provider_credentials else 0
        print(f"  Credentials: {cred_count} providers", file=sys.stderr)
        print(f"  Test cases: {len(agent5_input.test_cases.test_cases)}", file=sys.stderr)

        print("\n" + "=" * 60, file=sys.stderr)
        print("Running Agent 5 (Implement Test Env)...", file=sys.stderr)
        print("Building test harnesses in parallel. This may take 3-8 minutes.", file=sys.stderr)
        print("=" * 60 + "\n", file=sys.stderr)

        agent5_start = time.time()
        agent5_result = run_implement_test_env_agent(agent5_input)
        agent5_duration = round((time.time() - agent5_start) * 1000, 2)

        pipeline_run.save_agent_result(
            "agent_5", agent5_input, agent5_result, duration_ms=agent5_duration,
            cost_usd=agent5_result.total_build_cost_usd,
        )

        # Load Agent 4 output from original run for validation cross-checks
        from puzzleeval.schemas import Agent4Result
        agent4_output_path = old_run_dir / "agent_4_output.json"
        agent4_result_for_validation = None
        if agent4_output_path.exists():
            try:
                agent4_result_for_validation = Agent4Result(
                    **json.loads(agent4_output_path.read_text(encoding="utf-8"))
                )
            except Exception:
                pass
        agent5_validation = validate_agent5_output(agent5_result, agent4_result_for_validation)
        pipeline_run.save_validation("agent_5", agent5_validation)
        _print_validation("Agent 5", agent5_validation, logger, trace_id)

        print(f"\n  Agent 5 summary: {agent5_result.build_summary}", file=sys.stderr)
        print(f"\n  Pipeline run saved to: runs/{trace_id}/", file=sys.stderr)

        indent = 2 if args.pretty else None
        print(agent5_result.model_dump_json(indent=indent))

        pipeline_run.finalize()
        sys.exit(0)

    # --text is required unless --agent5-input is used
    if not args.text:
        print("Error: --text is required (unless using --agent5-input)", file=sys.stderr)
        sys.exit(1)

    trace_id = args.trace_id or generate_trace_id()

    logger.info("CLI started", extra={"operation": "cli_start", "trace_id": trace_id})

    # Initialize pipeline run for output persistence
    pipeline_run = PipelineRun(trace_id)

    try:
        # ---------------------------------------------------------------
        # Conversation loop
        # ---------------------------------------------------------------
        conversation_history: list[dict] = []
        current_text = args.text
        result = None
        agent1_total_cost = 0.0

        agent1_start = time.time()

        for turn in range(1, MAX_TURNS + 1):
            logger.info(f"Conversation turn {turn}", extra={
                "operation": "conversation_turn", "trace_id": trace_id,
            })

            input_data = Agent1Input(
                user_text=current_text,
                workflow_file_path=args.file if turn == 1 else None,
                trace_id=trace_id,
                conversation_history=conversation_history if conversation_history else None,
            )

            result = run_user_understanding_agent(input_data)
            agent1_total_cost += result.cost_usd

            if result.is_clear:
                break

            if args.no_interactive:
                break

            clarification = result.clarification_needed
            print("\n" + "=" * 60, file=sys.stderr)
            print(clarification.message, file=sys.stderr)
            print(file=sys.stderr)
            if clarification.critical_questions:
                for i, q in enumerate(clarification.critical_questions, 1):
                    print(f"  {i}. {q}", file=sys.stderr)
            if clarification.optional_prompt:
                print(file=sys.stderr)
                print(f"  (Optional) {clarification.optional_prompt}", file=sys.stderr)
            print("\n" + "=" * 60, file=sys.stderr)
            print("Your answer:", file=sys.stderr)

            try:
                user_answer = input()
            except EOFError:
                break

            if not user_answer.strip():
                break

            conversation_history.append({"role": "user", "content": current_text})
            conversation_history.append({"role": "assistant", "content": result.model_dump_json()})
            current_text = user_answer

        # Save and validate Agent 1
        agent1_duration = round((time.time() - agent1_start) * 1000, 2)
        result.cost_usd = agent1_total_cost
        pipeline_run.save_agent_result(
            "agent_1", input_data, result, duration_ms=agent1_duration,
            cost_usd=result.cost_usd,
        )
        agent1_validation = validate_agent1_output(result)
        pipeline_run.save_validation("agent_1", agent1_validation)
        _print_validation("Agent 1", agent1_validation, logger, trace_id)

        # ---------------------------------------------------------------
        # Agent 2: Research (optional, triggered by --agent2 flag)
        # ---------------------------------------------------------------

        # --agent5 implies --agent4 implies --agent2
        run_agent2 = (args.agent2 or args.agent4 or args.agent5) and result.is_clear and result.result is not None

        if run_agent2:

            # ---------------------------------------------------------------
            # For --agent5: Run Agent 2→4 IN PARALLEL with Agent 3/3F.
            # Both branches only need Agent 1 output. Agent 5 needs both.
            #
            # Branch A: Agent 2 (research) → Agent 4 (screening) [sequential]
            # Branch B: Agent 3F or Agent 3 (test cases) [independent]
            # ---------------------------------------------------------------
            if args.agent5:
                from concurrent.futures import ThreadPoolExecutor as _TP

                test_file_paths = None
                if args.test_files:
                    test_file_paths = [
                        p.strip() for p in args.test_files.split(",") if p.strip()
                    ]

                def _run_research_and_screening():
                    """Branch A: Agent 2 → Agent 4 (sequential, needs Agent 2 output)."""
                    print("\n" + "=" * 60, file=sys.stderr)
                    print("Agent 1 complete. Starting Agent 2 (Research)...", file=sys.stderr)
                    print("=" * 60 + "\n", file=sys.stderr)

                    agent2_input = Agent2Input(
                        user_understanding=result.result,
                        trace_id=trace_id,
                    )
                    a2_start = time.time()
                    a2_result = run_research_agent(agent2_input)
                    # Phase 6: prompt the user for per-scope candidate picks.
                    # In --no-interactive mode this is a pure pass-through.
                    a2_result = _cli_user_selection_pause(
                        a2_result,
                        blueprint=result.result.workflow,
                        no_interactive=args.no_interactive,
                        logger=logger,
                        trace_id=trace_id,
                    )
                    a2_duration = round((time.time() - a2_start) * 1000, 2)

                    pipeline_run.save_agent_result(
                        "agent_2", agent2_input, a2_result,
                        duration_ms=a2_duration, cost_usd=a2_result.cost_usd,
                    )
                    a2_validation = validate_agent2_output(a2_result, result.result)
                    pipeline_run.save_validation("agent_2", a2_validation)
                    _print_validation("Agent 2", a2_validation, logger, trace_id)

                    # Agent 4
                    print("\n" + "=" * 60, file=sys.stderr)
                    print("Agent 2 complete. Starting Agent 4 (Screening)...", file=sys.stderr)
                    print("=" * 60 + "\n", file=sys.stderr)

                    agent4_input = Agent4Input(
                        candidates=a2_result,
                        user_understanding=result.result,
                        trace_id=trace_id,
                    )
                    a4_start = time.time()
                    a4_result = run_screening_agent(agent4_input)
                    a4_duration = round((time.time() - a4_start) * 1000, 2)

                    pipeline_run.save_agent_result(
                        "agent_4", agent4_input, a4_result,
                        duration_ms=a4_duration, cost_usd=a4_result.cost_usd,
                    )
                    a4_validation = validate_agent4_output(a4_result, a2_result, result.result)
                    pipeline_run.save_validation("agent_4", a4_validation)
                    _print_validation("Agent 4", a4_validation, logger, trace_id)

                    return a2_result, a4_result

                def _run_test_generation():
                    """Branch B: mixed-mode test generation.

                    Routing priority:
                      1. If Agent 1 produced a TestPlan → route by scope_spec.test_mode
                         (file_based → 3F, synthetic_* → Agent 3)
                      2. Otherwise → route by sub_task.requires_test_files
                      3. Both agents produce Agent3Result → merge

                    Both agents produce Agent3Result. When both run, we merge
                    their test_cases + coverage_summary into one Agent3Result.
                    """
                    a3_start = time.time()
                    all_subtasks = result.result.sub_tasks

                    # Determine file vs text sub-tasks.
                    # TestPlan takes priority when available — its per-scope
                    # test_mode is more precise than requires_test_files.
                    test_plan = getattr(result.result, "test_plan", None)
                    if test_plan and test_plan.scope_specs:
                        file_scope_ids = {
                            s.scope_id for s in test_plan.scope_specs
                            if s.test_mode == "file_based"
                        }
                        # Map scope_id back to sub-tasks via capability
                        blueprint = result.result.workflow
                        scope_caps = {}
                        if blueprint:
                            scope_caps = {
                                s.capability.strip().lower(): s.id
                                for s in blueprint.steps
                            }
                        file_subtasks = []
                        text_subtasks = []
                        for st in all_subtasks:
                            scope_id = scope_caps.get(st.capability.strip().lower(), "")
                            if scope_id in file_scope_ids:
                                file_subtasks.append(st)
                            else:
                                text_subtasks.append(st)
                        print(f"  TestPlan routing: {len(file_subtasks)} file-based, {len(text_subtasks)} synthetic", file=sys.stderr)
                    else:
                        file_subtasks = [st for st in all_subtasks if st.requires_test_files]
                        text_subtasks = [st for st in all_subtasks if not st.requires_test_files]

                    a3_file_result = None
                    a3_text_result = None

                    # ── Test data sufficiency check ──
                    # Print a verdict per file-requiring sub-task BEFORE either
                    # Agent 3F or Agent 3 fires, so the user knows up front
                    # whether their data is sufficient and what the pipeline
                    # plans to do (READY / AUGMENT / SYNTHESIZE / REQUEST_MORE
                    # / DEGRADE). Pure observability — does not block the run.
                    try:
                        from puzzleeval.test_data_sufficiency import (
                            assess_sufficiency,
                        )
                        from puzzleeval.tool_plugins import list_plugins
                        avail = {p.name for p in list_plugins() if p.is_available()[0]}
                        for sub in file_subtasks:
                            sub_paths = list(test_file_paths or [])
                            verdict = assess_sufficiency(
                                scope_id=getattr(sub, "id", sub.description[:30]),
                                input_type="document_content",
                                output_type=getattr(sub, "output_format", "structured_json"),
                                requires_test_files=True,
                                file_paths=sub_paths,
                                available_plugin_names=avail,
                            )
                            print(
                                f"  data sufficiency [{verdict.scope_id}]: "
                                f"{verdict.action.upper()} — {verdict.reason}",
                                file=sys.stderr,
                            )
                            for advisory in verdict.advisories:
                                print(f"    advisory: {advisory}", file=sys.stderr)
                            if verdict.request_message:
                                print(f"    user-action: {verdict.request_message}", file=sys.stderr)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  data sufficiency check skipped: {exc}", file=sys.stderr)

                    # ── Agent 3F: file-based sub-tasks (only when files provided) ──
                    if test_file_paths and file_subtasks:
                        print("\n" + "=" * 60, file=sys.stderr)
                        print(f"Starting Agent 3F (File-Based Tests) for {len(file_subtasks)} file sub-task(s)...", file=sys.stderr)
                        print(f"Reading {len(test_file_paths)} user-provided file(s).", file=sys.stderr)
                        print("=" * 60 + "\n", file=sys.stderr)

                        # Build a filtered UserUnderstandingOutput with ONLY file sub-tasks
                        # so Agent 3F focuses on generating ground truth from the files.
                        file_uo = _filter_user_understanding(result.result, file_subtasks)

                        a3f_input = Agent3Input(
                            user_understanding=file_uo,
                            trace_id=trace_id,
                            test_file_paths=test_file_paths,
                        )
                        a3_file_result = run_file_tests_agent(a3f_input)
                        print(f"  Agent 3F: {len(a3_file_result.test_cases)} file-based test cases", file=sys.stderr)

                    # ── Agent 3: synthetic text sub-tasks ──
                    # Always runs when there are text sub-tasks. Also runs for
                    # file sub-tasks when no files were provided (degraded mode).
                    synth_subtasks = list(text_subtasks)
                    if not test_file_paths and file_subtasks:
                        # No files provided but some sub-tasks need them —
                        # Agent 3 generates synthetic text proxies.
                        synth_subtasks.extend(file_subtasks)
                        missing_subs = [st.description for st in file_subtasks]
                        print(
                            f"\n  NOTE: {len(file_subtasks)} sub-task(s) require test files but none provided:\n"
                            + "".join(f"    - {d}\n" for d in missing_subs)
                            + "  Generating synthetic text tests instead. Pass --test-files for better results.\n",
                            file=sys.stderr,
                        )

                    if synth_subtasks:
                        print("\n" + "=" * 60, file=sys.stderr)
                        print(f"Starting Agent 3 (Synthetic Tests) for {len(synth_subtasks)} text sub-task(s)...", file=sys.stderr)
                        print("=" * 60 + "\n", file=sys.stderr)

                        synth_uo = _filter_user_understanding(result.result, synth_subtasks)

                        a3_input = Agent3Input(
                            user_understanding=synth_uo,
                            trace_id=trace_id,
                        )
                        a3_text_result = run_synthetic_tests_agent(a3_input)
                        print(f"  Agent 3: {len(a3_text_result.test_cases)} synthetic test cases", file=sys.stderr)

                        # Wire file_required flag for sub-tasks that need files
                        # but were given synthetic proxies instead.
                        if not test_file_paths and file_subtasks:
                            file_caps = {st.capability.lower() for st in file_subtasks}
                            file_descs = {st.description.lower() for st in file_subtasks}
                            for tc in a3_text_result.test_cases:
                                ref = tc.sub_task_ref.lower()
                                if any(cap in ref or ref in cap for cap in file_caps) or \
                                   any(desc in ref or ref in desc for desc in file_descs):
                                    tc.file_required = True

                    # ── Merge results ──
                    a3_result = _merge_agent3_results(a3_file_result, a3_text_result)

                    a3_duration = round((time.time() - a3_start) * 1000, 2)
                    pipeline_run.save_agent_result(
                        "agent_3", Agent3Input(
                            user_understanding=result.result,
                            trace_id=trace_id,
                            test_file_paths=test_file_paths,
                        ), a3_result,
                        duration_ms=a3_duration, cost_usd=a3_result.cost_usd,
                    )
                    a3_validation = validate_agent3_output(a3_result, result.result)
                    pipeline_run.save_validation("agent_3", a3_validation)
                    _print_validation("Agent 3", a3_validation, logger, trace_id)

                    return a3_result

                # Run both branches in parallel
                print("\n" + "=" * 60, file=sys.stderr)
                print("Running Agent 2→4 (Research→Screening) and Agent 3 (Tests) in PARALLEL...", file=sys.stderr)
                print("=" * 60 + "\n", file=sys.stderr)

                with _TP(max_workers=2) as executor:
                    future_research = executor.submit(_run_research_and_screening)
                    future_tests = executor.submit(_run_test_generation)

                    agent2_result, agent4_result = future_research.result()
                    agent3_result = future_tests.result()

                # Now run Agent 5 (after both parallel branches complete)
                print("\n" + "=" * 60, file=sys.stderr)
                print("Agents 2→4 and 3 complete. Starting Agent 5 (Implement Test Env)...", file=sys.stderr)
                print(
                    f"Building test harnesses for {len(agent4_result.validated_candidates)} "
                    f"validated candidates in parallel.", file=sys.stderr,
                )
                print("This may take 2-5 minutes.", file=sys.stderr)
                print("=" * 60 + "\n", file=sys.stderr)

                # Load provider registry for live validation credentials
                registry = load_registry()
                provider_creds = get_all_credentials(
                    registry, agent4_result.validated_candidates,
                )

                # Show per-candidate credential matching
                from puzzleeval.provider_registry import get_credentials as _get_creds
                for vc in agent4_result.validated_candidates:
                    creds = _get_creds(registry, vc.provider, vc.name)
                    if creds:
                        print(
                            f"  >> {vc.name}: credentials FOUND ({list(creds.keys())})",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"  >> {vc.name}: no credentials (live validation skipped)",
                            file=sys.stderr,
                        )

                if not provider_creds:
                    print(
                        "  No credentials found for any candidate. "
                        "Add keys to provider_registry.json for live validation.",
                        file=sys.stderr,
                    )

                agent5_input = Agent5Input(
                    validated_candidates=agent4_result.validated_candidates,
                    user_understanding=result.result,
                    test_cases=agent3_result,
                    trace_id=trace_id,
                    provider_credentials=provider_creds,
                )

                agent5_start = time.time()
                agent5_result = run_implement_test_env_agent(agent5_input)
                agent5_duration = round((time.time() - agent5_start) * 1000, 2)

                # Save and validate Agent 5
                pipeline_run.save_agent_result(
                    "agent_5", agent5_input, agent5_result, duration_ms=agent5_duration,
                    cost_usd=agent5_result.total_build_cost_usd,
                )
                agent5_validation = validate_agent5_output(agent5_result, agent4_result)
                pipeline_run.save_validation("agent_5", agent5_validation)
                _print_validation("Agent 5", agent5_validation, logger, trace_id)

                print(f"\n  Agent 5 summary: {agent5_result.build_summary}", file=sys.stderr)

                indent = 2 if args.pretty else None
                print(agent5_result.model_dump_json(indent=indent))

            else:
                # --agent2 or --agent4 without --agent5: sequential, no parallel
                print("\n" + "=" * 60, file=sys.stderr)
                print("Agent 1 complete. Starting Agent 2 (Research)...", file=sys.stderr)
                print("=" * 60 + "\n", file=sys.stderr)

                agent2_input = Agent2Input(
                    user_understanding=result.result,
                    trace_id=trace_id,
                )
                agent2_start = time.time()
                agent2_result = run_research_agent(agent2_input)
                # Phase 6: prompt for per-scope picks (no-op in --no-interactive).
                agent2_result = _cli_user_selection_pause(
                    agent2_result,
                    blueprint=result.result.workflow,
                    no_interactive=args.no_interactive,
                    logger=logger,
                    trace_id=trace_id,
                )
                agent2_duration = round((time.time() - agent2_start) * 1000, 2)

                pipeline_run.save_agent_result(
                    "agent_2", agent2_input, agent2_result,
                    duration_ms=agent2_duration, cost_usd=agent2_result.cost_usd,
                )
                agent2_validation = validate_agent2_output(agent2_result, result.result)
                pipeline_run.save_validation("agent_2", agent2_validation)
                _print_validation("Agent 2", agent2_validation, logger, trace_id)

                if args.agent4:
                    print("\n" + "=" * 60, file=sys.stderr)
                    print("Agent 2 complete. Starting Agent 4 (Screening)...", file=sys.stderr)
                    print("=" * 60 + "\n", file=sys.stderr)

                    agent4_input = Agent4Input(
                        candidates=agent2_result,
                        user_understanding=result.result,
                        trace_id=trace_id,
                    )
                    agent4_start = time.time()
                    agent4_result = run_screening_agent(agent4_input)
                    agent4_duration = round((time.time() - agent4_start) * 1000, 2)

                    pipeline_run.save_agent_result(
                        "agent_4", agent4_input, agent4_result,
                        duration_ms=agent4_duration, cost_usd=agent4_result.cost_usd,
                    )
                    agent4_validation = validate_agent4_output(agent4_result, agent2_result, result.result)
                    pipeline_run.save_validation("agent_4", agent4_validation)
                    _print_validation("Agent 4", agent4_validation, logger, trace_id)

                    indent = 2 if args.pretty else None
                    print(agent4_result.model_dump_json(indent=indent))
                else:
                    indent = 2 if args.pretty else None
                    print(agent2_result.model_dump_json(indent=indent))

        elif args.agent3 and result.is_clear and result.result is not None:
            # ---------------------------------------------------------------
            # Agent 3 / 3F: Test Case Generation
            # ---------------------------------------------------------------
            # Routing is deterministic based on sub-task nature:
            #   requires_test_files=false → Agent 3 (synthetic text)
            #   requires_test_files=true  → Agent 3F (files + synthetic)
            # For mixed evaluations, both run and results merge.
            # ---------------------------------------------------------------

            # Parse --test-files if provided
            test_file_paths = None
            if args.test_files:
                test_file_paths = [
                    p.strip() for p in args.test_files.split(",") if p.strip()
                ]

            # Split sub-tasks by nature
            text_subtasks = [st for st in result.result.sub_tasks if not st.requires_test_files]
            file_subtasks = [st for st in result.result.sub_tasks if st.requires_test_files]

            agent3_start = time.time()
            final_result = None

            # ── Mixed-mode routing (same logic as --agent5 path) ──
            a3_file_result = None
            a3_text_result = None

            # Agent 3F: file sub-tasks (only when files provided)
            if file_subtasks and test_file_paths:
                print("\n" + "=" * 60, file=sys.stderr)
                print(f"Starting Agent 3F (File-Based Tests) for {len(file_subtasks)} file sub-task(s)...", file=sys.stderr)
                print(f"Reading {len(test_file_paths)} user-provided file(s).", file=sys.stderr)
                print("=" * 60 + "\n", file=sys.stderr)

                file_uo = _filter_user_understanding(result.result, file_subtasks)
                a3f_input = Agent3Input(
                    user_understanding=file_uo,
                    trace_id=trace_id,
                    test_file_paths=test_file_paths,
                )
                a3_file_result = run_file_tests_agent(a3f_input)

            # Agent 3: synthetic text sub-tasks
            synth_subtasks = list(text_subtasks)
            if not test_file_paths and file_subtasks:
                synth_subtasks.extend(file_subtasks)
                missing_subs = [st.description for st in file_subtasks]
                print(
                    f"\n  NOTE: {len(file_subtasks)} sub-task(s) require test files but none provided:\n"
                    + "".join(f"    - {d}\n" for d in missing_subs)
                    + "  Generating synthetic text tests instead.\n",
                    file=sys.stderr,
                )

            if synth_subtasks:
                print("\n" + "=" * 60, file=sys.stderr)
                print(f"Starting Agent 3 (Synthetic Tests) for {len(synth_subtasks)} text sub-task(s)...", file=sys.stderr)
                print("=" * 60 + "\n", file=sys.stderr)

                synth_uo = _filter_user_understanding(result.result, synth_subtasks)
                a3_input = Agent3Input(
                    user_understanding=synth_uo,
                    trace_id=trace_id,
                )
                a3_text_result = run_synthetic_tests_agent(a3_input)

                # Wire file_required flag for degraded file sub-tasks
                if not test_file_paths and file_subtasks:
                    file_caps = {st.capability.lower() for st in file_subtasks}
                    file_descs = {st.description.lower() for st in file_subtasks}
                    for tc in a3_text_result.test_cases:
                        ref = tc.sub_task_ref.lower()
                        if any(cap in ref or ref in cap for cap in file_caps) or \
                           any(desc in ref or ref in desc for desc in file_descs):
                            tc.file_required = True

            final_result = _merge_agent3_results(a3_file_result, a3_text_result)

            agent3_duration = round((time.time() - agent3_start) * 1000, 2)

            # Save and validate
            agent_label = "agent_3"
            if file_subtasks and test_file_paths:
                agent_label = "agent_3f" if not text_subtasks else "agent_3_merged"
            pipeline_run.save_agent_result(
                agent_label, Agent3Input(
                    user_understanding=result.result,
                    trace_id=trace_id,
                    test_file_paths=test_file_paths,
                ), final_result, duration_ms=agent3_duration,
                cost_usd=final_result.cost_usd,
            )
            agent3_validation = validate_agent3_output(final_result, result.result)
            pipeline_run.save_validation(agent_label, agent3_validation)
            _print_validation("Agent 3", agent3_validation, logger, trace_id)

            indent = 2 if args.pretty else None
            print(final_result.model_dump_json(indent=indent))

        else:
            indent = 2 if args.pretty else None
            print(result.model_dump_json(indent=indent))

        # Finalize pipeline run
        summary = pipeline_run.finalize()
        print(f"\n  Pipeline run saved to: runs/{trace_id}/", file=sys.stderr)
        print(f"  Status: {summary['status']}", file=sys.stderr)

    except KeyboardInterrupt:
        logger.info("CLI interrupted", extra={"operation": "cli_interrupt", "trace_id": trace_id})
        pipeline_run.fail("pipeline", "KeyboardInterrupt")
        pipeline_run.finalize()
        sys.exit(130)

    except Exception as e:
        logger.error(f"Agent failed: {e}", extra={
            "operation": "agent_failure", "trace_id": trace_id,
            "error": str(e), "error_type": type(e).__name__,
        })
        pipeline_run.fail("unknown", str(e))
        pipeline_run.finalize()
        print(json.dumps({
            "error": True, "error_type": type(e).__name__,
            "message": str(e), "trace_id": trace_id,
        }, indent=2 if args.pretty else None))
        sys.exit(1)


if __name__ == "__main__":
    main()
