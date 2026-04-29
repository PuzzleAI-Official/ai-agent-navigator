"""Agent 2 — Research Agent (web search + candidate scoring).

Phase 7: per-agent package structure. The legacy path
``puzzleeval.agents.research`` is preserved as a thin re-export shim
for back-compat.

Canonical home: ``puzzleeval.agents.agent2.core``.
"""

from puzzleeval.agents.agent2.core import *  # noqa: F401,F403
from puzzleeval.agents.agent2.core import (
    RESEARCH_SYSTEM_PROMPT,
    STRUCTURE_SYSTEM_PROMPT,
    run_research_agent,
)

__all__ = ['RESEARCH_SYSTEM_PROMPT', 'STRUCTURE_SYSTEM_PROMPT', 'run_research_agent']
