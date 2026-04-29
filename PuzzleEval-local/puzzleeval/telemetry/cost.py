"""LLM call cost calculation.

Canonical owner of "given an Anthropic response, how much did it cost?"
Replaces the previous per-agent ``_calculate_call_cost`` duplicates.

Two entry points:

  * ``calculate_call_cost(response, model, *, model_pricing, web_search_price_per_search)``
    — the original Agent 5 entry point. Walks ``response.usage.iterations``
    when present (advisor sub-calls have their own model + pricing), falls
    back to top-level usage when not. Preserved for back-compat with
    Agent 5's existing pricing semantics (advisor uses Opus pricing even
    when the executor is on a different model).

  * ``estimate_cost(*, model, response, cache_metrics=None)`` — the
    canonical NEW entry point. Same math, but the API is keyword-only
    and reads pricing from the canonical table.
"""

from __future__ import annotations

from typing import Any, Mapping

from puzzleeval.telemetry.pricing_tables import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER_5M,
    MODEL_PRICING,
    WEB_SEARCH_PRICE_PER_SEARCH,
    lookup_pricing,
)


# Default per-call pricing tuples — used when the model isn't in the
# pricing table. Conservative (matches Sonnet) so unknown-model cost
# isn't zero (which would mislead operators).
DEFAULT_EXECUTOR_PRICING: tuple[float, float] = (
    3.0 / 1_000_000,
    15.0 / 1_000_000,
)
DEFAULT_ADVISOR_PRICING: tuple[float, float] = (
    5.0 / 1_000_000,
    25.0 / 1_000_000,
)


def calculate_call_cost(
    response: Any,
    model: str,
    *,
    model_pricing: Mapping[str, tuple[float, float]] = MODEL_PRICING,
    web_search_price_per_search: float = WEB_SEARCH_PRICE_PER_SEARCH,
) -> float:
    """Calculate the total USD cost of one Anthropic response.

    Walks ``response.usage.iterations`` when the API returned an iteration
    breakdown (``client.beta.messages.create`` with the advisor or
    ``tool_runner`` betas). Each iteration has its own type
    (``"message"`` for the executor turn, ``"advisor_message"`` for
    advisor sub-calls); advisor iterations price at Opus rates by default
    even when the executor is on Sonnet.

    Falls back to top-level ``response.usage`` when ``iterations`` is
    absent.

    Includes:
      * Input tokens at base price.
      * Output tokens at base output price.
      * Cache-write tokens at 1.25x base input price.
      * Cache-read tokens at 0.10x base input price.
      * Web-search server-tool calls at ``$0.01/search``.

    Args:
        response: Anthropic response object (must have ``.usage``).
        model: Model name string used by the executor turn.
        model_pricing: Pricing table override (defaults to canonical table).
        web_search_price_per_search: USD per web_search call (defaults to canonical).

    Returns:
        Total cost rounded to 6 decimal places (microcents precision).
    """
    usage = response.usage
    iterations = getattr(usage, "iterations", None) or []

    if iterations:
        total_cost = 0.0
        for iteration in iterations:
            iter_type = getattr(iteration, "type", "message")
            iter_in = getattr(iteration, "input_tokens", 0) or 0
            iter_out = getattr(iteration, "output_tokens", 0) or 0
            iter_cache_create = getattr(iteration, "cache_creation_input_tokens", 0) or 0
            iter_cache_read = getattr(iteration, "cache_read_input_tokens", 0) or 0

            if iter_type == "advisor_message":
                iter_model = getattr(iteration, "model", None) or model
                in_price, out_price = model_pricing.get(
                    iter_model,
                    DEFAULT_ADVISOR_PRICING,
                )
            else:
                in_price, out_price = model_pricing.get(
                    model,
                    DEFAULT_EXECUTOR_PRICING,
                )

            total_cost += iter_in * in_price
            total_cost += iter_out * out_price
            total_cost += iter_cache_create * in_price * CACHE_WRITE_MULTIPLIER_5M
            total_cost += iter_cache_read * in_price * CACHE_READ_MULTIPLIER

        server_tool_use = getattr(usage, "server_tool_use", None)
        if server_tool_use:
            searches = getattr(server_tool_use, "web_search_requests", 0) or 0
            total_cost += searches * web_search_price_per_search

        return round(total_cost, 6)

    # No iterations — top-level usage is the whole picture
    input_price, output_price = model_pricing.get(model, DEFAULT_EXECUTOR_PRICING)
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

    cost = (usage.input_tokens * input_price) + (usage.output_tokens * output_price)
    cost += cache_create * input_price * CACHE_WRITE_MULTIPLIER_5M
    cost += cache_read * input_price * CACHE_READ_MULTIPLIER

    server_tool_use = getattr(usage, "server_tool_use", None)
    if server_tool_use:
        searches = getattr(server_tool_use, "web_search_requests", 0) or 0
        cost += searches * web_search_price_per_search

    return round(cost, 6)


def estimate_cost(
    *,
    model: str,
    response: Any,
    web_search_price_per_search: float = WEB_SEARCH_PRICE_PER_SEARCH,
) -> float:
    """Canonical cost-estimation entry point.

    Functionally equivalent to ``calculate_call_cost(response, model)`` —
    the keyword-only API is the new convention for callers that don't
    need to override the pricing table.

    Args:
        model: Model name string.
        response: Anthropic response object.
        web_search_price_per_search: Override for web search pricing.

    Returns:
        Total USD cost.
    """
    return calculate_call_cost(
        response,
        model,
        model_pricing=MODEL_PRICING,
        web_search_price_per_search=web_search_price_per_search,
    )


__all__ = [
    "DEFAULT_ADVISOR_PRICING",
    "DEFAULT_EXECUTOR_PRICING",
    "calculate_call_cost",
    "estimate_cost",
]
