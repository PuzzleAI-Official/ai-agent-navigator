"""Agent 3F — file-based synthetic test case generation.

Phase 7: per-agent package structure. The legacy single-file path
``puzzleeval.agents.synthetic_tests_file`` is preserved as a thin
re-export shim for back-compat.

Canonical home: ``puzzleeval.agents.agent3f.core``.
"""

from puzzleeval.agents.agent3f.core import *  # noqa: F401,F403
from puzzleeval.agents.agent3f.core import (
    SYSTEM_PROMPT,
    run_file_tests_agent,
)

__all__ = ["SYSTEM_PROMPT", "run_file_tests_agent"]
