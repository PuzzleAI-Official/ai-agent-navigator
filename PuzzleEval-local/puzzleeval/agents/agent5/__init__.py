"""Agent 5 implementation package.

The historical public entry point remains
``puzzleeval.agents.implement_test_env`` for compatibility. New Agent 5
subsystems live here so the large compatibility module can shrink in
behavior-preserving slices.
"""

__all__ = [
    "build_loop",
    "costing",
    "evaluation",
    "execution",
    "playbooks",
    "prompts",
    "sandbox",
    "tools",
]
