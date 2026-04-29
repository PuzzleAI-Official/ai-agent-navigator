"""Canonical Anthropic + plugin-provider pricing tables.

This module is the SINGLE SOURCE OF TRUTH for model pricing. Every other
module that needs to compute cost imports from here.

Source: https://platform.claude.com/docs/en/about-claude/pricing
Update when Anthropic changes prices. The CI guard
``tests/test_pricing_table_completeness.py`` asserts every model name
referenced in the codebase has an entry here — adding a new model
requires updating this table.

Cache pricing multipliers (``CACHE_WRITE_MULTIPLIER_*``,
``CACHE_READ_MULTIPLIER``) follow Anthropic's prompt-caching docs:
  * 5-min TTL: 1.25x base input price for writes.
  * 1-hour TTL: 2.00x base input price for writes.
  * Cache reads (hits): 0.10x base input price (same for both TTLs).

Web-tool pricing (``WEB_SEARCH_PRICE_PER_SEARCH``): $10 per 1,000
searches = $0.01/search. Web fetch has no per-call charge — costs
are token-based only.
"""

from __future__ import annotations

from typing import Mapping


# ---------------------------------------------------------------------------
# Model Pricing — USD per token, format: (input_price, output_price)
# ---------------------------------------------------------------------------
MODEL_PRICING: Mapping[str, tuple[float, float]] = {
    # Opus 4.7: $5 / 1M input, $25 / 1M output
    "claude-opus-4-7": (5.0 / 1_000_000, 25.0 / 1_000_000),
    # Opus 4.5: $5 / 1M input, $25 / 1M output
    "claude-opus-4-5": (5.0 / 1_000_000, 25.0 / 1_000_000),
    # Opus 4.1: $15 / 1M input, $75 / 1M output
    "claude-opus-4-1": (15.0 / 1_000_000, 75.0 / 1_000_000),
    # Sonnet 4.6: $3 / 1M input, $15 / 1M output
    "claude-sonnet-4-6": (3.0 / 1_000_000, 15.0 / 1_000_000),
    # Sonnet 4.5: $3 / 1M input, $15 / 1M output
    "claude-sonnet-4-5-20250929": (3.0 / 1_000_000, 15.0 / 1_000_000),
    # Haiku 4.5: $1 / 1M input, $5 / 1M output
    "claude-haiku-4-5-20251001": (1.0 / 1_000_000, 5.0 / 1_000_000),
}


# ---------------------------------------------------------------------------
# Cache Pricing Multipliers
# ---------------------------------------------------------------------------
CACHE_WRITE_MULTIPLIER_5M = 1.25
CACHE_WRITE_MULTIPLIER_1H = 2.00
CACHE_READ_MULTIPLIER = 0.10


# ---------------------------------------------------------------------------
# Server-tool pricing
# ---------------------------------------------------------------------------
# $10 per 1,000 searches → $0.01/search.
# Web fetch has no per-call charge.
WEB_SEARCH_PRICE_PER_SEARCH = 0.01


# ---------------------------------------------------------------------------
# Minimum cacheable token thresholds per model.
# Below this, the API silently ignores cache_control directives.
# ---------------------------------------------------------------------------
MIN_CACHEABLE_TOKENS: Mapping[str, int] = {
    "claude-opus-4-7": 4096,
    "claude-opus-4-5": 4096,
    "claude-opus-4-1": 1024,
    "claude-sonnet-4-6": 2048,
    "claude-sonnet-4-5-20250929": 1024,
    "claude-haiku-4-5-20251001": 4096,
}


def lookup_pricing(model: str, *, default: tuple[float, float] | None = None) -> tuple[float, float]:
    """Return ``(input_price, output_price)`` per token for ``model``.

    Falls back to ``default`` (or zero-pricing) when the model is unknown
    so callers don't crash on unknown-model logging. The CI guard ensures
    every production model is in the table; ``default`` is a safety net,
    not a workaround.

    Args:
        model: Model name (e.g., ``"claude-opus-4-7"``).
        default: Optional fallback when model is missing. None → ``(0.0, 0.0)``.

    Returns:
        Tuple of (input_price_per_token, output_price_per_token).
    """
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    if default is not None:
        return default
    return (0.0, 0.0)


def min_cacheable_tokens_for(model: str, *, default: int = 1024) -> int:
    """Minimum tokens required for cache_control to take effect on ``model``."""
    return MIN_CACHEABLE_TOKENS.get(model, default)


__all__ = [
    "MODEL_PRICING",
    "MIN_CACHEABLE_TOKENS",
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER_1H",
    "CACHE_WRITE_MULTIPLIER_5M",
    "WEB_SEARCH_PRICE_PER_SEARCH",
    "lookup_pricing",
    "min_cacheable_tokens_for",
]
