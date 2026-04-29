"""Quality battery harness for Phase 2 prompt refactor.

Per the Phase 2 plan (Section 7): token metrics are necessary but not
sufficient. This harness measures behavioral parity using fixed inputs.
Every metric has a strict-improvement floor at the merge gate.

Metrics measured:
  * Agent 1 blueprint quality — sub-task count within expected range +
    declared modality coverage matches expected.
  * Agent 2 scope coverage — avg `covers_step_ids` count per scope.
  * Agent 3 test validity — Pydantic passes for all + Shannon entropy
    of test inputs (diversity proxy).
  * Agent 4 verification precision — verified / (verified + inconclusive)
    ratio on a fixture set with 1 known-bad placeholder.

Modes:
  --dry-run    Validate fixtures load correctly + Pydantic shapes parse.
               No API calls. Free. Used to verify the harness itself
               works before kicking off paid real-API runs.

  (default)    Real-API runs. Calls each agent with each fixture,
               captures response, computes metrics, writes a JSON blob
               to the baselines directory. Cost: ~$0.50-$1.00 for the
               full battery.

Outputs:
  - PuzzleEval-local/baselines/<date>/quality_battery_<label>.json

Usage:
  # Validate fixtures (no API):
  python scripts/quality_battery.py --dry-run

  # Capture pre-Phase-2A baseline (real API, ~$1):
  python scripts/quality_battery.py --label phase2_baseline

  # Capture post-Phase-2A snapshot for comparison:
  python scripts/quality_battery.py --label post_phase2A

  # Compare two snapshots:
  python scripts/quality_battery.py \
      --compare-against baselines/<date>/quality_battery_phase2_baseline.json \
      --label post_phase2A
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures — small, hand-curated inputs with known-expected shapes.
# Start with 2 per agent; expand to 5 once the MVP harness is proven.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Agent1Fixture:
    """A natural-language user request + expected blueprint shape."""

    fixture_id: str
    user_text: str
    # Expected blueprint shape (sanity bounds, not exact equality).
    expected_min_subtasks: int
    expected_max_subtasks: int
    # Modalities that MUST appear in the blueprint's input/output types.
    must_include_modalities: tuple[str, ...] = field(default_factory=tuple)


AGENT1_FIXTURES: tuple[Agent1Fixture, ...] = (
    Agent1Fixture(
        fixture_id="invoice_ocr_quickbooks",
        user_text=(
            "I need an AI for invoice OCR and sync to QuickBooks. "
            "High volume, around 5000 invoices per month. "
            "I'm technically capable, in finance domain."
        ),
        expected_min_subtasks=2,
        expected_max_subtasks=4,
        must_include_modalities=("document_content",),
    ),
    Agent1Fixture(
        fixture_id="voice_dispatcher_plumbing",
        user_text=(
            "I run a plumbing business and need a 24/7 AI voice agent "
            "that takes calls, diagnoses problems, quotes prices from "
            "our menu, and books service appointments."
        ),
        expected_min_subtasks=1,
        expected_max_subtasks=3,
        must_include_modalities=("voice_conversation",),
    ),
)


@dataclass(frozen=True)
class Agent2Fixture:
    """A fixture that exercises Agent 2 starting from a known blueprint.

    The harness loads `agent1_result_path` (JSON file with a saved Agent 1
    output) and calls Agent 2 with that as input. Only loads when a real
    Agent 1 fixture has been captured at least once.
    """

    fixture_id: str
    agent1_fixture_id: str  # Reference back to the Agent 1 fixture
    expected_min_candidates: int
    expected_max_candidates: int
    expected_min_avg_scope_coverage: float


AGENT2_FIXTURES: tuple[Agent2Fixture, ...] = (
    Agent2Fixture(
        fixture_id="invoice_ocr_candidates",
        agent1_fixture_id="invoice_ocr_quickbooks",
        expected_min_candidates=4,
        expected_max_candidates=8,
        expected_min_avg_scope_coverage=0.8,
    ),
    Agent2Fixture(
        fixture_id="voice_dispatcher_candidates",
        agent1_fixture_id="voice_dispatcher_plumbing",
        expected_min_candidates=4,
        expected_max_candidates=8,
        expected_min_avg_scope_coverage=0.8,
    ),
)


@dataclass(frozen=True)
class Agent3Fixture:
    """Fixture for Agent 3 (test generation, text mode)."""

    fixture_id: str
    agent2_fixture_id: str  # Reference back
    expected_min_test_cases: int
    expected_max_test_cases: int
    expected_min_diversity_entropy: float  # Shannon entropy of inputs


AGENT3_FIXTURES: tuple[Agent3Fixture, ...] = (
    Agent3Fixture(
        fixture_id="voice_dispatcher_tests",
        agent2_fixture_id="voice_dispatcher_candidates",
        expected_min_test_cases=4,
        expected_max_test_cases=12,
        expected_min_diversity_entropy=1.5,
    ),
)


@dataclass(frozen=True)
class Agent4Fixture:
    """Fixture for Agent 4 (screening + deep verify) with a known-bad
    placeholder candidate that MUST be Verified Reject."""

    fixture_id: str
    agent2_fixture_id: str
    known_bad_candidate_name: str  # MUST be Verified Reject
    expected_min_precision: float  # verified / (verified + inconclusive)


AGENT4_FIXTURES: tuple[Agent4Fixture, ...] = (
    Agent4Fixture(
        fixture_id="voice_dispatcher_screening",
        agent2_fixture_id="voice_dispatcher_candidates",
        known_bad_candidate_name="DefinitelyNotARealAIService_v1",
        expected_min_precision=0.5,
    ),
)


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def shannon_entropy(items: list[Any]) -> float:
    """Shannon entropy of a list (in bits). Higher = more diverse."""
    if not items:
        return 0.0
    counts = Counter(items)
    total = len(items)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def compute_agent1_metrics(fixture: Agent1Fixture, result: Any) -> dict:
    """Compute Agent 1 blueprint-quality metrics from a real Agent 1 result."""
    metrics: dict[str, Any] = {"fixture_id": fixture.fixture_id}

    # Sub-task count
    sub_tasks = []
    if hasattr(result, "sub_tasks"):
        sub_tasks = list(result.sub_tasks)
    elif isinstance(result, dict) and "sub_tasks" in result:
        sub_tasks = list(result["sub_tasks"])
    metrics["sub_task_count"] = len(sub_tasks)
    metrics["sub_task_count_in_range"] = (
        fixture.expected_min_subtasks <= len(sub_tasks) <= fixture.expected_max_subtasks
    )

    # Modality coverage
    declared_modalities: set[str] = set()
    blueprint = (
        getattr(result, "workflow", None)
        or (result.get("workflow") if isinstance(result, dict) else None)
    )
    if blueprint:
        steps = (
            getattr(blueprint, "steps", None)
            or (blueprint.get("steps") if isinstance(blueprint, dict) else [])
        )
        for step in steps or []:
            for field_name in ("input_type", "output_type"):
                v = (
                    getattr(step, field_name, None)
                    or (step.get(field_name) if isinstance(step, dict) else None)
                )
                if v:
                    declared_modalities.add(v)
    metrics["declared_modalities"] = sorted(declared_modalities)
    metrics["modality_coverage_complete"] = all(
        m in declared_modalities for m in fixture.must_include_modalities
    )

    # Pydantic-level validation: did the result successfully parse?
    metrics["pydantic_passed"] = result is not None
    return metrics


def compute_agent2_metrics(fixture: Agent2Fixture, result: Any) -> dict:
    """Compute Agent 2 scope-coverage metrics."""
    metrics: dict[str, Any] = {"fixture_id": fixture.fixture_id}
    candidates = (
        getattr(result, "candidates", None)
        or (result.get("candidates") if isinstance(result, dict) else [])
        or []
    )
    metrics["candidate_count"] = len(candidates)
    metrics["candidate_count_in_range"] = (
        fixture.expected_min_candidates <= len(candidates) <= fixture.expected_max_candidates
    )

    # Avg scope coverage = mean of len(covers_step_ids) per candidate
    coverages = [
        len(
            getattr(c, "covers_step_ids", None)
            or (c.get("covers_step_ids") if isinstance(c, dict) else [])
            or []
        )
        for c in candidates
    ]
    metrics["avg_scope_coverage"] = (
        sum(coverages) / len(coverages) if coverages else 0.0
    )
    metrics["avg_scope_coverage_meets_target"] = (
        metrics["avg_scope_coverage"] >= fixture.expected_min_avg_scope_coverage
    )
    metrics["pydantic_passed"] = result is not None
    return metrics


def compute_agent3_metrics(fixture: Agent3Fixture, result: Any) -> dict:
    """Compute Agent 3 test-validity + diversity metrics."""
    metrics: dict[str, Any] = {"fixture_id": fixture.fixture_id}
    test_cases = (
        getattr(result, "test_cases", None)
        or (result.get("test_cases") if isinstance(result, dict) else [])
        or []
    )
    metrics["test_case_count"] = len(test_cases)
    metrics["test_case_count_in_range"] = (
        fixture.expected_min_test_cases <= len(test_cases) <= fixture.expected_max_test_cases
    )

    # Shannon entropy of input modalities (proxy for diversity)
    input_types = [
        (
            getattr(tc, "input_type", None)
            or (tc.get("input_type") if isinstance(tc, dict) else "unknown")
        )
        for tc in test_cases
    ]
    metrics["input_type_entropy"] = shannon_entropy(input_types)
    metrics["diversity_meets_target"] = (
        metrics["input_type_entropy"] >= fixture.expected_min_diversity_entropy
    )
    metrics["pydantic_passed"] = result is not None
    return metrics


def compute_agent4_metrics(fixture: Agent4Fixture, result: Any) -> dict:
    """Compute Agent 4 verification-precision metrics."""
    metrics: dict[str, Any] = {"fixture_id": fixture.fixture_id}
    validated = (
        getattr(result, "validated_candidates", None)
        or (result.get("validated_candidates") if isinstance(result, dict) else [])
        or []
    )
    rejected = (
        getattr(result, "rejected_candidates", None)
        or (result.get("rejected_candidates") if isinstance(result, dict) else [])
        or []
    )
    metrics["validated_count"] = len(validated)
    metrics["rejected_count"] = len(rejected)

    # Precision = verified / (verified + inconclusive). Inconclusive isn't
    # a separate bucket here — we proxy it with rejected-with-reason
    # "inconclusive" or similar. Conservative: precision = validated /
    # (validated + rejected) when the known-bad is rejected (good signal).
    total = len(validated) + len(rejected)
    metrics["precision"] = (len(validated) / total) if total else 0.0

    # Known-bad MUST be rejected
    rejected_names = {
        getattr(r, "name", None) or (r.get("name") if isinstance(r, dict) else None)
        for r in rejected
    }
    metrics["known_bad_rejected"] = fixture.known_bad_candidate_name in rejected_names
    metrics["precision_meets_target"] = (
        metrics["precision"] >= fixture.expected_min_precision
    )
    metrics["pydantic_passed"] = result is not None
    return metrics


# ---------------------------------------------------------------------------
# Real-API runners — only called when --dry-run is NOT set.
# ---------------------------------------------------------------------------


def run_agent1(fixture: Agent1Fixture) -> Any:
    """Call Agent 1 with the fixture's user_text. Real API.

    Agent 1's `run_user_understanding_agent` takes an `Agent1Input` with
    `messages` (conversation history). We feed a single user turn.
    """
    from puzzleeval.agents.agent1.core import run_user_understanding_agent
    from puzzleeval.schemas import Agent1Input

    inp = Agent1Input(
        messages=[{"role": "user", "content": fixture.user_text}],
        attachments=[],
    )
    return run_user_understanding_agent(inp)


def run_agent2(fixture: Agent2Fixture, agent1_result: Any) -> Any:
    """Call Agent 2 with a captured Agent 1 result."""
    from puzzleeval.agents.agent2.core import run_research_agent
    from puzzleeval.schemas import Agent2Input

    inp = Agent2Input(user_understanding=agent1_result)
    return run_research_agent(inp)


def run_agent3(fixture: Agent3Fixture, agent2_result: Any, agent1_result: Any) -> Any:
    """Call Agent 3 with captured Agent 1 + 2 results.

    Agent 3's entry point + input shape will be confirmed at first
    real-API run; harness surfaces ImportError if the symbol drifts.
    """
    import importlib

    a3_core = importlib.import_module("puzzleeval.agents.agent3.core")
    runner = getattr(a3_core, "run_synthetic_tests_agent", None) or getattr(
        a3_core, "run_synthetic_tests", None
    )
    if runner is None:
        raise ImportError("Agent 3 runner not found in agent3.core")
    return runner(
        candidates=getattr(agent2_result, "candidates", None)
        or (agent2_result.get("candidates") if isinstance(agent2_result, dict) else []),
        user_understanding=agent1_result,
    )


def run_agent4(fixture: Agent4Fixture, agent2_result: Any, agent1_result: Any) -> Any:
    """Call Agent 4 with captured Agent 1 + 2 results."""
    import importlib

    a4_core = importlib.import_module("puzzleeval.agents.agent4.core")
    runner = getattr(a4_core, "run_screening_agent", None) or getattr(
        a4_core, "run_screening", None
    )
    if runner is None:
        raise ImportError("Agent 4 runner not found in agent4.core")
    return runner(
        candidates=getattr(agent2_result, "candidates", None)
        or (agent2_result.get("candidates") if isinstance(agent2_result, dict) else []),
        user_understanding=agent1_result,
    )


# ---------------------------------------------------------------------------
# Main harness
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate fixtures load + Pydantic shapes parse. No API calls.",
    )
    parser.add_argument("--label", default="quality_battery", help="Output filename label")
    parser.add_argument("--date", default=_dt.date.today().isoformat())
    parser.add_argument("--out-tracked", default=str(ROOT / "baselines"))
    parser.add_argument(
        "--compare-against",
        default=None,
        help="Path to a previous quality_battery JSON to diff against.",
    )
    parser.add_argument(
        "--agents",
        default="1,2,3,4",
        help="Comma-separated agent IDs to run (default: all four).",
    )
    args = parser.parse_args()

    selected_agents = {int(s.strip()) for s in args.agents.split(",") if s.strip()}
    tracked_dir = Path(args.out_tracked) / args.date
    tracked_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        print("[dry-run] Validating fixtures load + harness shape...")
        # Just ensure fixture data is well-formed.
        for fx in AGENT1_FIXTURES:
            assert fx.user_text, fx.fixture_id
            assert fx.expected_min_subtasks <= fx.expected_max_subtasks, fx.fixture_id
        for fx in AGENT2_FIXTURES:
            assert fx.expected_min_candidates <= fx.expected_max_candidates, fx.fixture_id
        for fx in AGENT3_FIXTURES:
            assert fx.expected_min_test_cases <= fx.expected_max_test_cases, fx.fixture_id
        for fx in AGENT4_FIXTURES:
            assert 0 <= fx.expected_min_precision <= 1.0, fx.fixture_id
        print(
            f"[dry-run]   Agent 1: {len(AGENT1_FIXTURES)} fixtures OK\n"
            f"[dry-run]   Agent 2: {len(AGENT2_FIXTURES)} fixtures OK\n"
            f"[dry-run]   Agent 3: {len(AGENT3_FIXTURES)} fixtures OK\n"
            f"[dry-run]   Agent 4: {len(AGENT4_FIXTURES)} fixtures OK"
        )
        # Verify the agent runner functions are importable.
        try:
            from puzzleeval.agents.agent1.core import run_user_understanding_agent  # noqa: F401
            from puzzleeval.agents.agent2.core import run_research_agent  # noqa: F401
            import importlib

            a3 = importlib.import_module("puzzleeval.agents.agent3.core")
            a4 = importlib.import_module("puzzleeval.agents.agent4.core")
            assert hasattr(a3, "run_synthetic_tests_agent") or hasattr(a3, "run_synthetic_tests")
            assert hasattr(a4, "run_screening_agent") or hasattr(a4, "run_screening")
            print("[dry-run] Agent 1+2 runners import OK; Agent 3+4 runners present")
        except (ImportError, AssertionError) as exc:
            print(f"[dry-run] WARN: agent runner import/lookup failed: {exc}")
            print("[dry-run]   Real-API mode will need a runner-name fix.")
        return 0

    # Real-API mode
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or api_key.startswith("dummy"):
        print("[error] real-API mode requires ANTHROPIC_API_KEY. Use --dry-run for offline validation.")
        return 2

    results: dict[str, Any] = {
        "captured": _dt.datetime.utcnow().isoformat() + "Z",
        "agent1": [],
        "agent2": [],
        "agent3": [],
        "agent4": [],
    }
    cached_agent1: dict[str, Any] = {}
    cached_agent2: dict[str, Any] = {}

    if 1 in selected_agents:
        print("[+] Agent 1 fixtures...")
        for fx in AGENT1_FIXTURES:
            try:
                result = run_agent1(fx)
                cached_agent1[fx.fixture_id] = result
                metrics = compute_agent1_metrics(fx, result)
                results["agent1"].append(metrics)
                print(f"  {fx.fixture_id:<35} sub_tasks={metrics['sub_task_count']} pyd={metrics['pydantic_passed']}")
            except Exception as exc:
                results["agent1"].append({"fixture_id": fx.fixture_id, "error": str(exc)})
                print(f"  {fx.fixture_id:<35} ERROR: {exc}")

    if 2 in selected_agents:
        print("[+] Agent 2 fixtures...")
        for fx in AGENT2_FIXTURES:
            a1 = cached_agent1.get(fx.agent1_fixture_id)
            if a1 is None:
                results["agent2"].append({"fixture_id": fx.fixture_id, "skipped": "no agent1 result"})
                continue
            try:
                result = run_agent2(fx, a1)
                cached_agent2[fx.fixture_id] = result
                metrics = compute_agent2_metrics(fx, result)
                results["agent2"].append(metrics)
                print(f"  {fx.fixture_id:<35} candidates={metrics['candidate_count']} avg_cov={metrics['avg_scope_coverage']:.2f}")
            except Exception as exc:
                results["agent2"].append({"fixture_id": fx.fixture_id, "error": str(exc)})

    if 3 in selected_agents:
        print("[+] Agent 3 fixtures...")
        for fx in AGENT3_FIXTURES:
            a2 = cached_agent2.get(fx.agent2_fixture_id)
            if a2 is None:
                results["agent3"].append({"fixture_id": fx.fixture_id, "skipped": "no agent2 result"})
                continue
            a1 = cached_agent1.get(
                next((fx2.agent1_fixture_id for fx2 in AGENT2_FIXTURES if fx2.fixture_id == fx.agent2_fixture_id), "")
            )
            try:
                result = run_agent3(fx, a2, a1)
                metrics = compute_agent3_metrics(fx, result)
                results["agent3"].append(metrics)
                print(f"  {fx.fixture_id:<35} tests={metrics['test_case_count']} entropy={metrics['input_type_entropy']:.2f}")
            except Exception as exc:
                results["agent3"].append({"fixture_id": fx.fixture_id, "error": str(exc)})

    if 4 in selected_agents:
        print("[+] Agent 4 fixtures...")
        for fx in AGENT4_FIXTURES:
            a2 = cached_agent2.get(fx.agent2_fixture_id)
            a1 = cached_agent1.get(
                next((fx2.agent1_fixture_id for fx2 in AGENT2_FIXTURES if fx2.fixture_id == fx.agent2_fixture_id), "")
            )
            if a2 is None:
                results["agent4"].append({"fixture_id": fx.fixture_id, "skipped": "no agent2 result"})
                continue
            try:
                result = run_agent4(fx, a2, a1)
                metrics = compute_agent4_metrics(fx, result)
                results["agent4"].append(metrics)
                print(f"  {fx.fixture_id:<35} precision={metrics['precision']:.2f} known_bad_rejected={metrics['known_bad_rejected']}")
            except Exception as exc:
                results["agent4"].append({"fixture_id": fx.fixture_id, "error": str(exc)})

    # Save JSON
    out_file = tracked_dir / f"{args.label}.json"
    out_file.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print()
    print(f"[+] Quality battery captured to {out_file}")

    # Comparison mode
    if args.compare_against:
        prior = json.loads(Path(args.compare_against).read_text(encoding="utf-8"))
        print()
        print("=== Comparison vs prior battery ===")
        for agent_key in ("agent1", "agent2", "agent3", "agent4"):
            for prior_metric, current_metric in zip(prior.get(agent_key, []), results.get(agent_key, [])):
                fid = prior_metric.get("fixture_id", "?")
                # Compare common keys
                common_keys = (set(prior_metric) & set(current_metric)) - {"fixture_id"}
                changes = []
                for k in sorted(common_keys):
                    if prior_metric[k] != current_metric[k]:
                        changes.append(f"{k}: {prior_metric[k]} -> {current_metric[k]}")
                if changes:
                    print(f"  {agent_key}/{fid}: " + "; ".join(changes))
                else:
                    print(f"  {agent_key}/{fid}: parity")

    return 0


if __name__ == "__main__":
    sys.exit(main())
