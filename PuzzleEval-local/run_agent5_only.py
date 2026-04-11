"""
Run Agent 5 ONLY from a saved agent_5_input.json file.

Usage:
    python run_agent5_only.py runs/c059a231-e82a-445c-9021-7b3eb0c22b7a/agent_5_input.json

This loads the saved input, runs Agent 5, and saves the output.
Useful for testing Agent 5 changes without re-running Agents 1-4.
"""

import json
import sys
import time
from pathlib import Path

from puzzleeval.schemas import Agent5Input, Agent5Result
from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
from puzzleeval.logging_setup import setup_logging, get_logger
from puzzleeval.pipeline import PipelineRun
from puzzleeval.validators import validate_agent5_output


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_agent5_only.py <path_to_agent_5_input.json>")
        print("Example: python run_agent5_only.py runs/c059a231-e82a-445c-9021-7b3eb0c22b7a/agent_5_input.json")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"Error: {input_path} does not exist")
        sys.exit(1)

    setup_logging()
    logger = get_logger("run_agent5_only")

    # Load the saved Agent 5 input
    raw = json.loads(input_path.read_text(encoding="utf-8"))

    # Generate a NEW trace_id so we don't overwrite the old run
    from puzzleeval.logging_setup import generate_trace_id
    new_trace_id = generate_trace_id()
    old_trace_id = raw.get("trace_id", "unknown")
    raw["trace_id"] = new_trace_id

    input_data = Agent5Input(**raw)

    trace_id = input_data.trace_id
    print(f"Original run: {old_trace_id}")
    print(f"New trace ID: {trace_id}")
    print(f"Candidates: {len(input_data.validated_candidates)}")
    for c in input_data.validated_candidates:
        print(f"  - {c.name} ({c.provider}) [score={c.relevance_score}]")

    cred_count = 0
    if input_data.provider_credentials:
        cred_count = len(input_data.provider_credentials)
    print(f"Credentials loaded: {cred_count} providers")
    print(f"Test cases: {len(input_data.test_cases.test_cases)}")
    print()

    # Create pipeline run for output persistence
    pipeline = PipelineRun(trace_id)

    # Copy upstream agent outputs (1-4) from the original run into the new folder
    # so the new run folder has the full context for comparison
    import shutil
    old_run_dir = input_path.parent
    new_run_dir = Path("runs") / trace_id
    new_run_dir.mkdir(parents=True, exist_ok=True)

    for fname in old_run_dir.iterdir():
        if fname.name.startswith("agent_") and fname.name != "agent_5_output.json":
            dest = new_run_dir / fname.name
            if not dest.exists():
                shutil.copy2(fname, dest)

    # Save the Agent 5 input (with new trace_id)
    pipeline.save_agent_input(5, raw)

    print(f"\nNew run folder: runs/{trace_id}/")
    print(f"Upstream outputs copied from: {old_run_dir}")
    print()

    # Run Agent 5
    print("=" * 60)
    print("Running Agent 5 (Implement Test Env)...")
    print("Building harnesses for top 4 candidates in parallel.")
    print("This takes 3-8 minutes. Watch agent5_logs.jsonl for live progress.")
    print("=" * 60)
    sys.stdout.flush()
    start = time.time()

    result = run_implement_test_env_agent(input_data)

    elapsed = time.time() - start
    print()
    print("=" * 60)
    print(f"Agent 5 completed in {elapsed:.1f}s")
    print("=" * 60)

    # Save output
    pipeline.save_agent_output(5, result.model_dump())

    # Validate
    validation = validate_agent5_output(result, input_data)
    pipeline.save_validation(5, validation)

    if validation.get("errors"):
        print(f"\n  !! VALIDATION ERRORS:")
        for e in validation["errors"]:
            print(f"     - {e}")

    if validation.get("warnings"):
        print(f"\n  >> Warnings:")
        for w in validation["warnings"]:
            print(f"     - {w}")

    # Print summary
    print(f"\n  Summary: {result.build_summary}")
    print(f"\n  Harnesses built: {len(result.harnesses)}")
    for h in result.harnesses:
        print(f"    ✅ {h.candidate_name} ({h.provider})")
        print(f"       Smoke: {'PASS' if h.smoke_test_passed else 'FAIL'}")
        print(f"       Live:  {h.live_validation_notes[:100] if h.live_validation_notes else 'N/A'}")
        print(f"       Turns: {h.build_turns}, Cost: ${h.build_cost_usd:.2f}")

    print(f"\n  Failed: {len(result.failed_harnesses)}")
    for f in result.failed_harnesses:
        print(f"    ❌ {f.candidate_name} ({f.provider})")
        print(f"       Reason: {f.failure_reason[:150]}")
        print(f"       Category: {f.failure_category}")

    print(f"\n  Total cost: ${result.total_build_cost_usd:.2f}")
    print(f"\n  Output saved to: runs/{trace_id}/agent_5_output.json")

    # Save pipeline summary
    summary = {
        "trace_id": trace_id,
        "original_trace_id": old_trace_id,
        "mode": "agent5_only_rerun",
        "status": "completed" if result.harnesses else "failed",
        "agent_5": {
            "status": "completed" if result.harnesses else "failed",
            "duration_seconds": round(elapsed, 1),
            "harnesses_built": len(result.harnesses),
            "harnesses_failed": len(result.failed_harnesses),
            "total_cost_usd": result.total_build_cost_usd,
            "candidates_attempted": result.total_candidates_attempted,
            "validation": validation,
        },
    }
    (new_run_dir / "pipeline_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8",
    )

    # Also print JSON result to stdout
    print("\n" + "=" * 60)
    print(json.dumps(result.model_dump(), indent=2, default=str))


if __name__ == "__main__":
    main()
