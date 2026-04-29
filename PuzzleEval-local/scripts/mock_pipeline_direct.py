"""Direct in-process mock pipeline drive — bypasses HTTP/TestClient.

FastAPI's TestClient can't reliably progress background asyncio tasks
between requests (a documented limitation). To prove the mock pipeline
actually works end-to-end, we call ``run_pipeline(state)`` directly in
the current event loop and assert it reaches a terminal state.

Exits 0 on clean pipeline_completed/cancelled/failed. Exit 1 if the
pipeline hangs or crashes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT.parent / "puzzleeval-api"
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-for-mock")


async def run():
    from services.run_manager import run_manager
    from services.pipeline_runner import run_pipeline
    from services.pipeline_runner import mock_agent1_turn

    state = run_manager.create_run(
        text="I need an AI for invoice OCR and sync to QuickBooks.",
        agent_modes={
            "agent1": "mock", "agent2": "mock",
            "agent3": "mock", "agent4": "mock", "agent5": "mock",
        },
        plan="enterprise",
    )

    # Use the REAL mock_agent1_turn to produce a schema-valid Agent 1
    # output (with all required fields). Hand-crafting the shape drifts
    # whenever UserUnderstandingOutput's schema evolves.
    await mock_agent1_turn(state, "I need invoice OCR and QuickBooks sync.")
    await mock_agent1_turn(state, "High volume, finance domain.")
    if not state.agent1_result or not state.agent1_result.get("result", {}).get("is_clear"):
        # Some mock variants need a third turn to converge.
        await mock_agent1_turn(state, "I'm technical, ~5000 invoices/month.")
    if not state.agent1_result:
        print("[XX] mock_agent1 did not produce a result")
        sys.exit(1)
    # Disable the Phase 6 selection pause for the direct mock drive —
    # pause requires external POST to /select-candidates which we can't
    # provide in a pure asyncio script. The real run has this enabled.
    os.environ["PUZZLEEVAL_USER_SELECTION_ENABLED"] = "0"
    # Reload the flag so run_pipeline sees it.
    import puzzleeval.config as cfg
    cfg.USER_SELECTION_ENABLED = False

    print(f"[+] Pipeline starting for run {state.run_id}/{state.trace_id}")
    try:
        # Mock Agent 5 has ~90s of UX-simulation sleeps (2.5s + 2.0s +
        # 1.5s + 1.0s per harness × 4 candidates, plus 1.5s per test
        # result × ~28 test results). Give enough margin.
        await asyncio.wait_for(run_pipeline(state), timeout=180.0)
    except asyncio.TimeoutError:
        print(f"[XX] pipeline TIMED OUT at status={state.status!r}")
        # Dump partial state for diagnosis
        print(f"     total_cost_usd={state.total_cost_usd}")
        print(f"     agent2_result={bool(state.agent2_result)}")
        print(f"     agent3_result={bool(state.agent3_result)}")
        print(f"     agent4_result={bool(state.agent4_result)}")
        print(f"     agent5_result={bool(state.agent5_result)}")
        sys.exit(1)
    except Exception as exc:
        import traceback
        print(f"[XX] pipeline CRASHED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)

    print(f"[OK] pipeline finished with status={state.status!r}")
    print(f"     total_cost_usd=${state.total_cost_usd:.4f}")
    print(f"     agent2_result present: {bool(state.agent2_result)}")
    print(f"     agent3_result present: {bool(state.agent3_result)}")
    print(f"     agent4_result present: {bool(state.agent4_result)}")
    print(f"     agent5_result present: {bool(state.agent5_result)}")
    if state.agent5_result:
        scope_runs = state.agent5_result.get("scope_runs") or []
        print(f"     scope_runs: len={len(scope_runs)}")
        harnesses = state.agent5_result.get("harnesses") or []
        print(f"     harnesses: len={len(harnesses)}")

    # Verify the evaluation_report artifact landed.
    from pathlib import Path as P
    report_file = API_ROOT / "runs" / state.trace_id / "evaluation_report.json"
    if report_file.exists():
        import json
        report = json.loads(report_file.read_text(encoding="utf-8"))
        print(f"[OK] evaluation_report.json exists: "
              f"candidates={report.get('candidate_count', 0)}, "
              f"winner={report.get('overall_winner')!r}, "
              f"scope_runs={len(report.get('scope_runs', []))}")
    else:
        print(f"[!!] evaluation_report.json missing at {report_file}")

    if state.status not in ("completed", "completed_with_warnings"):
        print(f"[XX] terminal status is not completed: {state.status}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
