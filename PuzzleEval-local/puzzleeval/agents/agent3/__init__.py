"""Agent 3 — Synthetic Tests Agent (text-based test case generation).

Phase 7: per-agent package structure. The legacy path
``puzzleeval.agents.synthetic_tests`` is preserved as a thin re-export shim
for back-compat.

Canonical home: ``puzzleeval.agents.agent3.core``.
"""

from puzzleeval.agents.agent3.core import *  # noqa: F401,F403
from puzzleeval.agents.agent3.core import (
    SYSTEM_PROMPT,
    run_synthetic_tests_agent,
)

__all__ = ['SYSTEM_PROMPT', 'run_synthetic_tests_agent']
