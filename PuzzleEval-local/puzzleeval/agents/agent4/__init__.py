"""Agent 4 — Screening Agent (per-candidate API verification).

Phase 7: per-agent package structure. The legacy path
``puzzleeval.agents.screening`` is preserved as a thin re-export shim
for back-compat.

Canonical home: ``puzzleeval.agents.agent4.core``.
"""

from puzzleeval.agents.agent4.core import *  # noqa: F401,F403
from puzzleeval.agents.agent4.core import (
    VERIFICATION_SYSTEM_PROMPT,
    STRUCTURE_SYSTEM_PROMPT,
    run_screening_agent,
)

__all__ = ['VERIFICATION_SYSTEM_PROMPT', 'STRUCTURE_SYSTEM_PROMPT', 'run_screening_agent']
