"""Prompt rendering helpers for Agent 5.

After Phase 1.B, the canonical placeholder set is:

  * ``__OS_TYPE__``  — the literal "Windows" / "Linux" / "macOS" string
                       (small, host-stable, kept inline near the top of
                       the prompt as a positional cue).
  * ``__CONTRACT_BLOCK__`` — single trailing placeholder that bundles
                              every conditional contract (platform +
                              modality) selected by the contract system.
                              Placed near the END of the template so the
                              cacheable prefix covers ~99% of the prompt.

The legacy placeholders ``__OS_SPECIFIC_RULES__`` and
``__MODALITY_CONTRACT__`` are still recognised by ``render_builder_prompt``
for back-compat with any caller that builds prompts via the legacy path.
On the legacy path they receive their respective inline content; on the
canonical path the unified contract block replaces them with empty
strings (their content lives in ``__CONTRACT_BLOCK__``).
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=1)
def load_builder_system_prompt() -> str:
    """Load the packaged Agent 5 builder prompt template."""

    return (
        resources.files("puzzleeval.agents.agent5")
        .joinpath("templates", "builder_system_prompt.md")
        .read_text(encoding="utf-8")
    )


def os_name_for_platform(platform: str) -> str:
    if platform == "win32":
        return "Windows"
    if platform == "darwin":
        return "macOS"
    if platform.startswith("linux"):
        return "Linux"
    return platform


def os_rules_for_platform(platform: str, os_rules: dict[str, str]) -> str:
    if platform == "win32":
        return os_rules.get("win32", "")
    if platform == "darwin":
        return os_rules.get("darwin", "")
    if platform.startswith("linux"):
        return os_rules.get("linux", "")
    return ""


def render_builder_prompt_for_os(
    prompt_template: str,
    *,
    platform: str,
    os_rules: dict[str, str],
) -> str:
    """LEGACY back-compat — fill __OS_TYPE__ + __OS_SPECIFIC_RULES__.

    Used by callers that haven't migrated to the unified contract block.
    Today's Agent 5 builder uses ``render_builder_prompt`` directly so
    only legacy tests/tools call this entry point.
    """
    rendered = prompt_template.replace("__OS_TYPE__", os_name_for_platform(platform))
    return rendered.replace(
        "__OS_SPECIFIC_RULES__",
        os_rules_for_platform(platform, os_rules),
    )


def render_builder_prompt(
    prompt_template: str,
    *,
    platform: str,
    os_rules: dict[str, str] | None = None,
    modality_contract: str = "",
    contract_block: str | None = None,
) -> str:
    """Render the Agent 5 builder prompt.

    Two modes:

      * **Canonical** (``contract_block`` is not None): single trailing
        placeholder ``__CONTRACT_BLOCK__`` is filled with the unified
        contract block from ``puzzleeval.contracts.compose_contract_block``.
        Legacy placeholders ``__OS_SPECIFIC_RULES__`` and
        ``__MODALITY_CONTRACT__`` are stripped (replaced with empty
        strings) since their content already lives inside the unified
        block.

      * **Legacy** (``contract_block`` is None): fill the dual-placeholder
        layout with separate ``os_rules`` dict + ``modality_contract``
        string. Used by tests + back-compat call sites that haven't yet
        migrated to the unified path.

    The ``__OS_TYPE__`` placeholder always receives the human-readable
    OS name regardless of mode — it's a tiny positional cue near the top
    of the template.
    """
    rendered = prompt_template.replace("__OS_TYPE__", os_name_for_platform(platform))

    if contract_block is not None:
        # Canonical path — strip legacy placeholders, fill unified block.
        rendered = rendered.replace("__OS_SPECIFIC_RULES__", "")
        rendered = rendered.replace("__MODALITY_CONTRACT__", "")
        rendered = rendered.replace("__CONTRACT_BLOCK__", contract_block)
        return rendered

    # Legacy path — keep existing dual-placeholder behavior.
    rendered = rendered.replace(
        "__OS_SPECIFIC_RULES__",
        os_rules_for_platform(platform, os_rules or {}),
    )
    rendered = rendered.replace("__MODALITY_CONTRACT__", modality_contract)
    # Strip the unified placeholder if present (legacy templates may not have it
    # but the canonical template does — we don't want raw __CONTRACT_BLOCK__
    # leaking into the rendered prompt in legacy mode).
    rendered = rendered.replace("__CONTRACT_BLOCK__", "")
    return rendered


__all__ = [
    "load_builder_system_prompt",
    "os_name_for_platform",
    "os_rules_for_platform",
    "render_builder_prompt",
    "render_builder_prompt_for_os",
]
