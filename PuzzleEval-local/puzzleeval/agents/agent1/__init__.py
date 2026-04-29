"""Agent 1 — User Understanding Agent (parses user request into sub-tasks + workflow blueprint).

Phase 7: per-agent package structure. The legacy path
``puzzleeval.agents.user_understanding`` is preserved as a thin re-export shim
for back-compat.

Canonical home: ``puzzleeval.agents.agent1.core``.
"""

from puzzleeval.agents.agent1.core import *  # noqa: F401,F403
from puzzleeval.agents.agent1.core import (
    SYSTEM_PROMPT,
    run_user_understanding_agent,
)

__all__ = ['SYSTEM_PROMPT', 'run_user_understanding_agent']
