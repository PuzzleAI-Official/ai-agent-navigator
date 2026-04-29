"""Verification gate — final structural check before HARNESS_COMPLETE.

When the builder model signals HARNESS_COMPLETE, the build loop calls
this gate to confirm the harness is structurally present (harness.py
exists, is non-empty). The agent already validated compatible input
forms with real test data during Phase 3 — this is just a sanity
check that the file actually exists.

This module is intentionally small. The Phase 4 REAL TEST PROBE work
(per OT-013 in CLAUDE.md) will live here too: an additional check that
the harness was actually exercised against an Agent-3-shaped payload
before HARNESS_COMPLETE is accepted. Today's gate doesn't do that;
the planned probe will.

Phase 3.1 of the architecture cleanup — extracted from
``puzzleeval/agents/implement_test_env.py``. The legacy name
``_run_verification_checks`` is preserved as a one-line shim in
``implement_test_env.py`` for back-compat with source-grep tests +
external callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from puzzleeval.agents.agent5.sandbox import _read_harness_code

if TYPE_CHECKING:
    import logging
    from puzzleeval.schemas import ScreenedCandidate


def run_verification_checks(
    sandbox_dir: Path,
    candidate: "ScreenedCandidate",
    credentials: dict[str, str] | None,
    logger: "logging.Logger",
    trace_id: str,
) -> str | None:
    """Verify the harness exists and is structurally valid.

    Called when Claude signals HARNESS_COMPLETE. Returns a short error
    message string when verification fails; returns None on success.

    Args:
        sandbox_dir: The candidate's sandbox directory.
        candidate: The ScreenedCandidate the harness was built for.
        credentials: Resolved credentials (currently unused by the gate;
                     reserved for the planned Phase 4 REAL TEST PROBE
                     which will need them).
        logger: Standard logger for telemetry.
        trace_id: Run trace_id for log correlation.

    Returns:
        ``None`` on success. A short error string when verification
        fails (caller injects this into the build loop's user message
        and asks the model to fix).

    Future expansion (OT-013, planned Phase 4 REAL TEST PROBE):
        Add a check that loads `agent_3_test_cases.json` from the
        sandbox, picks one test case, builds the production-shape
        payload via `derive_production_payload`, calls
        ``harness.run(payload)``, asserts ``success=True`` with expected
        audio shape, and writes ``phase_4_passed.txt`` on success. The
        verification gate then requires that file's existence — the
        bypass-verification problem documented in OT-013 becomes
        structurally impossible.
    """
    if not _read_harness_code(sandbox_dir):
        return "harness.py does not exist or is empty."
    return None


__all__ = ["run_verification_checks"]
