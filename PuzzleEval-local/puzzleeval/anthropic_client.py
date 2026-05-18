"""Central Anthropic client factory + model fallback ladder.

Every agent in the pipeline constructs its ``anthropic.Anthropic`` client
via ``build_client()`` so three policies apply uniformly:

  * **Connection timeout.** ``timeout=120 s`` (override via
    ``PUZZLEEVAL_ANTHROPIC_TIMEOUT_S``) — keeps a stalled TCP socket from
    parking the whole pipeline for the SDK's 10-minute default.
  * **Transport retries.** ``max_retries=3`` (override via
    ``PUZZLEEVAL_ANTHROPIC_MAX_RETRIES``) covers transient 5xx + connection
    drops at the SDK layer, so a single blip doesn't kill a run.
  * **Model fallback.** ``call_with_model_fallback(fn, primary_model, ...)``
    wraps a call in a deterministic Opus → Sonnet → Haiku ladder on
    persistent 429. Callers that also use ``structured_output.parse_with_fallback``
    should NOT stack model fallback on top of it — one graceful degradation
    at a time is enough.

Most callers get resilient HTTP + optional model degradation without bespoke
handling per call site. Callers with strict model ownership can disable the
ladder explicitly.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Callable

try:
    import anthropic
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal test envs
    class _MissingAnthropicError(RuntimeError):
        pass

    class _MissingAnthropic:
        class types:
            class Message:  # noqa: D401 - annotation compatibility stub
                pass

        class _BaseError(Exception):
            def __init__(self, *args, **kwargs):
                super().__init__(*args)
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class BadRequestError(_BaseError):
            pass

        class RateLimitError(_BaseError):
            pass

        class InternalServerError(_BaseError):
            pass

        class APIConnectionError(_BaseError):
            pass

        class APIStatusError(_BaseError):
            status_code = None

        class Anthropic:  # noqa: D401 - compatibility stub
            def __init__(self, *args, **kwargs):
                raise _MissingAnthropicError(
                    "The 'anthropic' package is required for real API calls. "
                    "Install PuzzleEval dependencies before running agents."
                )

        @staticmethod
        def beta_tool(func=None, *args, **kwargs):
            if func is None:
                return lambda inner: inner
            return func

    anthropic = _MissingAnthropic()  # type: ignore[assignment]
    sys.modules.setdefault("anthropic", anthropic)  # type: ignore[arg-type]

from puzzleeval.config import require_anthropic_key

logger = logging.getLogger(__name__)


# Default knobs — tuned for an interactive pipeline.
#
# Two timeout tiers because the pipeline has two call shapes with very
# different wall-clocks:
#
#   DEFAULT_TIMEOUT_S — used by most single-shot structured-output calls
#   (Agent 1, Agent 2's STRUCTURE pass, Agent 4's STRUCTURE pass).
#   These are messages.parse() style and typically finish in 10-30
#   seconds. 120 s is ample headroom for those paths.
#
#   AGENT3_GENERATION_TIMEOUT_S — used by Agent 3 / 3F test generation.
#   Voice/conversation test cases with fixtures and weighted rubrics can
#   be much larger than the other single-shot calls. Keep this separate
#   so Agent 3 gets enough room without making every short call slow to
#   fail when the API connection is unhealthy.
#
#   SERVER_TOOL_TIMEOUT_S — used by calls that loop server-side tools
#   (Agent 2's RESEARCH pass with web_search, Agent 4's deep_verify
#   with web_fetch + web_search, Agent 5's builder with tool_runner +
#   code_execution). The server-side loop can genuinely take 2-5
#   minutes: each web_search adds ~5-15 s, each web_fetch ~5-30 s,
#   and Opus reasoning between calls adds more. 120 s collapsed
#   real runs mid-research. Raised to 420 s (7 min) so a legitimate
#   multi-tool call completes; 10-min is still the HARD SDK ceiling
#   before the connection drops anyway.
#
#   max_retries=3 covers transient 5xx + connection drops without
#   flooding when Anthropic is genuinely down.
DEFAULT_TIMEOUT_S = float(os.environ.get("PUZZLEEVAL_ANTHROPIC_TIMEOUT_S", "120"))
SERVER_TOOL_TIMEOUT_S = float(
    os.environ.get("PUZZLEEVAL_ANTHROPIC_SERVER_TOOL_TIMEOUT_S", "420")
)
DEFAULT_MAX_RETRIES = int(os.environ.get("PUZZLEEVAL_ANTHROPIC_MAX_RETRIES", "3"))
AGENT3_GENERATION_TIMEOUT_S = float(
    os.environ.get("PUZZLEEVAL_AGENT3_GENERATION_TIMEOUT_S", "300")
)
AGENT3_GENERATION_MAX_RETRIES = int(
    os.environ.get("PUZZLEEVAL_AGENT3_GENERATION_MAX_RETRIES", "1")
)
AGENT3_TRANSIENT_RETRY_ATTEMPTS = int(
    os.environ.get("PUZZLEEVAL_AGENT3_TRANSIENT_RETRY_ATTEMPTS", "2")
)


_API_KEY_SENTINEL = object()


def build_client(
    *,
    api_key: str | None = _API_KEY_SENTINEL,  # type: ignore[assignment]
    timeout: float | None = None,
    max_retries: int | None = None,
) -> anthropic.Anthropic:
    """Construct an Anthropic client with PuzzleEval's standard defaults.

    Every agent should use this rather than calling ``anthropic.Anthropic()``
    directly — it ensures the timeout + retry policy is consistent across
    Agents 1-5 and plugins.

    ``api_key`` semantics:
      * Omitted → ``require_anthropic_key()`` is called (raises if env var
        unset). This is the production path.
      * Explicitly passed (including ``None``) → forwarded as-is so unit
        tests can build a client object without setting the env var; the
        SDK will only error on the first real network call (which tests
        either mock or never make).
    """
    if api_key is _API_KEY_SENTINEL:
        api_key = require_anthropic_key()
    return anthropic.Anthropic(
        api_key=api_key,
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_S,
        max_retries=max_retries if max_retries is not None else DEFAULT_MAX_RETRIES,
    )


# ---------------------------------------------------------------------------
# Model fallback ladder
# ---------------------------------------------------------------------------

# Default ladder: try the requested model, then degrade to a cheaper /
# less-loaded family on persistent rate-limit. The ladder is keyed by the
# requested model — Opus falls to Sonnet, Sonnet falls to Haiku, Haiku has
# no fallback (cheapest tier already).
_DEFAULT_LADDER: dict[str, tuple[str, ...]] = {
    "claude-opus-4-7": ("claude-sonnet-4-6",),
    "claude-opus-4-5": ("claude-sonnet-4-6",),
    "claude-opus-4-1": ("claude-sonnet-4-5-20250929",),
    "claude-sonnet-4-6": ("claude-haiku-4-5-20251001",),
    "claude-sonnet-4-5-20250929": ("claude-haiku-4-5-20251001",),
}


def model_fallback_chain(model: str) -> tuple[str, ...]:
    """Return ``(model,)`` followed by its fallback ladder, no duplicates."""
    chain: list[str] = [model]
    seen = {model}
    for fallback in _DEFAULT_LADDER.get(model, ()):
        if fallback not in seen:
            chain.append(fallback)
            seen.add(fallback)
    return tuple(chain)


def call_with_model_fallback(
    *,
    fn: Callable[[str], Any],
    primary_model: str,
    trace_id: str = "",
    is_rate_limit: Callable[[Exception], bool] | None = None,
    operation_label: str = "anthropic_call",
    allow_fallbacks: bool = True,
) -> Any:
    """Run ``fn(model)`` against the primary model, fall back on 429.

    ``fn`` is a closure that takes a model name and runs the actual API
    call. We try ``primary_model`` first; on ``RateLimitError`` (or any
    exception ``is_rate_limit`` flags as a rate-limit) we try the next
    model in the ladder. If all models in the ladder rate-limit, the
    last exception is re-raised.

    Set ``allow_fallbacks=False`` for call sites with strict model ownership
    (for example Agent 5 builder turns). In that mode the helper still
    centralizes rate-limit classification but only calls ``primary_model``.

    Non-rate-limit exceptions (BadRequestError, APIConnectionError,
    APIStatusError) propagate immediately — model fallback only addresses
    capacity issues, not request validity.
    """
    if is_rate_limit is None:
        is_rate_limit = _default_is_rate_limit
    chain = model_fallback_chain(primary_model) if allow_fallbacks else (primary_model,)
    last_exc: Exception | None = None
    for idx, model in enumerate(chain):
        try:
            if idx > 0:
                logger.warning(
                    "%s: falling back to %s after rate-limit on %s",
                    operation_label, model, chain[idx - 1],
                    extra={
                        "operation": "model_fallback",
                        "trace_id": trace_id,
                        "from_model": chain[idx - 1],
                        "to_model": model,
                    },
                )
            return fn(model)
        except Exception as exc:  # noqa: BLE001 — broad on purpose; we re-raise non-rate-limit
            if not is_rate_limit(exc):
                raise
            last_exc = exc
            # Continue to next model in the ladder
    # Exhausted the ladder — raise the last 429 so callers can classify it
    assert last_exc is not None
    raise last_exc


def _default_is_rate_limit(exc: Exception) -> bool:
    """Detect rate-limit or capacity-overload errors from the Anthropic SDK."""
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError) and getattr(exc, "status_code", None) in (429, 529):
        return True
    msg = str(exc).lower()
    return any(
        kw in msg for kw in (
            "rate limit", "rate_limit", "overloaded", "too many requests",
        )
    )


# ---------------------------------------------------------------------------
# Transient 5xx retry (beyond the SDK's built-in max_retries)
# ---------------------------------------------------------------------------
#
# The Anthropic SDK's ``max_retries=3`` retries happen rapidly with minimal
# backoff. When their backend has a multi-second outage (observed: a ~90 s
# degraded-mode window where every request returned HTTP 500 "Internal
# server error"), 3 rapid retries finish before the incident clears and
# the caller gets a hard failure. This helper adds a second tier of
# longer-baked retries on top of the SDK tier — seconds-scale instead of
# milliseconds-scale.
#
# Not a substitute for SDK retries; they complement. Use this helper for
# expensive calls whose failure would force the caller to redo prior spend
# (e.g. Agent 2 Step 2 after a $0.25 Step 1, Agent 5's per-candidate
# builder turns). Don't use it blindly for every call — that multiplies
# real outages by the retry count and delays user feedback.

import time as _time


def retry_on_transient_5xx(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 2.0,
    backoff_factor: float = 3.0,
    trace_id: str = "",
    operation_label: str = "anthropic_call",
) -> Any:
    """Run ``fn()``; retry on 500/502/503 with exponential backoff.

    Returns on first success; re-raises any non-5xx exception immediately.
    After ``max_attempts`` 5xx failures the last exception propagates so
    the caller can classify it. Sleeps: ``base_delay_s``,
    ``base_delay_s * backoff_factor``, etc. — default (2, 6, 18) for
    ``max_attempts=3``.

    Rationale: the SDK's ``max_retries`` retries within milliseconds and
    doesn't span a real backend incident. This helper adds a second tier
    at seconds scale so Anthropic has time to stabilise. Only intercepts
    5xx — 4xx and rate-limit errors (handled by ``call_with_model_fallback``)
    pass through unchanged.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except anthropic.InternalServerError as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            wait = base_delay_s * (backoff_factor ** attempt)
            logger.warning(
                "%s: Anthropic 500 (attempt %d/%d); retrying in %.1fs",
                operation_label, attempt + 1, max_attempts, wait,
                extra={
                    "operation": f"{operation_label}_5xx_retry",
                    "trace_id": trace_id,
                    "attempt": attempt + 1,
                    "error": str(exc),
                },
            )
            _time.sleep(wait)
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            if status in (502, 503):
                last_exc = exc
                if attempt + 1 >= max_attempts:
                    break
                wait = base_delay_s * (backoff_factor ** attempt)
                logger.warning(
                    "%s: Anthropic %s (attempt %d/%d); retrying in %.1fs",
                    operation_label, status, attempt + 1, max_attempts, wait,
                    extra={
                        "operation": f"{operation_label}_5xx_retry",
                        "trace_id": trace_id,
                        "attempt": attempt + 1,
                        "status": status,
                        "error": str(exc),
                    },
                )
                _time.sleep(wait)
            else:
                raise
    assert last_exc is not None
    raise last_exc


__all__ = [
    "AGENT3_GENERATION_MAX_RETRIES",
    "AGENT3_GENERATION_TIMEOUT_S",
    "AGENT3_TRANSIENT_RETRY_ATTEMPTS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_S",
    "SERVER_TOOL_TIMEOUT_S",
    "build_client",
    "call_with_model_fallback",
    "model_fallback_chain",
    "retry_on_transient_5xx",
]
