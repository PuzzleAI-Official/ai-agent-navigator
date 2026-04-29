"""Tests for puzzleeval.anthropic_client + puzzleeval.budget.

Pure unit tests — no real API calls. The client factory and budget
module are the chokepoints for HTTP resilience + cost control across
the entire pipeline.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

from puzzleeval.anthropic_client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_S,
    build_client,
    call_with_model_fallback,
    model_fallback_chain,
    retry_on_transient_5xx,
)
from puzzleeval.budget import BudgetExceededError, RunBudget


# ---------------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------------


def test_build_client_uses_default_timeout_and_retries():
    """Sanity: default args produce a client with our standard timeout/retries."""
    client = build_client(api_key="sk-test-explicit")
    assert float(client.timeout) == DEFAULT_TIMEOUT_S
    assert client.max_retries == DEFAULT_MAX_RETRIES


def test_build_client_accepts_overrides():
    client = build_client(api_key="sk-test", timeout=60, max_retries=1)
    assert float(client.timeout) == 60.0
    assert client.max_retries == 1


def test_build_client_accepts_explicit_none_api_key():
    """Production passes the key explicitly. Tests pass None to mock — both
    paths must build a client without raising at construction time."""
    client = build_client(api_key=None)  # may fail on real call, fine for tests
    assert isinstance(client, anthropic.Anthropic)


def test_build_client_omitted_key_calls_require_anthropic_key(monkeypatch):
    """When api_key is omitted (production path), require_anthropic_key fires."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
        build_client()


# ---------------------------------------------------------------------------
# Model fallback ladder
# ---------------------------------------------------------------------------


def test_model_fallback_chain_opus_falls_to_sonnet():
    chain = model_fallback_chain("claude-opus-4-7")
    assert chain[0] == "claude-opus-4-7"
    assert "claude-sonnet-4-6" in chain


def test_model_fallback_chain_haiku_has_no_fallback():
    chain = model_fallback_chain("claude-haiku-4-5-20251001")
    assert chain == ("claude-haiku-4-5-20251001",)


def test_model_fallback_chain_unknown_model_returns_self():
    chain = model_fallback_chain("future-model-x")
    assert chain == ("future-model-x",)


# ---------------------------------------------------------------------------
# call_with_model_fallback
# ---------------------------------------------------------------------------


def _make_429() -> anthropic.RateLimitError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(
        429, request=req,
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "rate limited"}},
    )
    return anthropic.RateLimitError(message="rate limited", response=resp, body=None)


def _make_400() -> anthropic.BadRequestError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(
        400, request=req,
        json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}},
    )
    return anthropic.BadRequestError(message="bad", response=resp, body=None)


def test_first_model_succeeds_returns_immediately():
    fn = MagicMock(return_value="ok")
    out = call_with_model_fallback(
        fn=fn, primary_model="claude-opus-4-7",
    )
    assert out == "ok"
    fn.assert_called_once_with("claude-opus-4-7")


def test_falls_back_to_next_model_on_rate_limit():
    calls = []
    def fn(model):
        calls.append(model)
        if model == "claude-opus-4-7":
            raise _make_429()
        return f"ok-from-{model}"
    out = call_with_model_fallback(
        fn=fn, primary_model="claude-opus-4-7",
    )
    assert out == "ok-from-claude-sonnet-4-6"
    assert calls == ["claude-opus-4-7", "claude-sonnet-4-6"]


def test_non_rate_limit_error_propagates_immediately():
    """A 400 is a request bug, not a capacity issue — model fallback is wrong."""
    fn = MagicMock(side_effect=_make_400())
    with pytest.raises(anthropic.BadRequestError):
        call_with_model_fallback(
            fn=fn, primary_model="claude-opus-4-7",
        )
    fn.assert_called_once()


def test_exhausted_ladder_reraises_last_rate_limit():
    fn = MagicMock(side_effect=_make_429())
    with pytest.raises(anthropic.RateLimitError):
        call_with_model_fallback(
            fn=fn, primary_model="claude-opus-4-7",
        )
    # Tried opus + sonnet (sonnet's fallback is haiku)
    assert fn.call_count >= 2


# ---------------------------------------------------------------------------
# retry_on_transient_5xx — second-tier retry for real backend incidents
# ---------------------------------------------------------------------------


def _make_500(message: str = "Internal server error") -> anthropic.InternalServerError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(
        500, request=req,
        json={"type": "error", "error": {"type": "api_error", "message": message}},
    )
    return anthropic.InternalServerError(message=message, response=resp, body=None)


def _make_502() -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(
        502, request=req,
        json={"type": "error", "error": {"type": "bad_gateway", "message": "bad gateway"}},
    )
    return anthropic.APIStatusError(message="bad gateway", response=resp, body=None)


def test_retry_on_transient_5xx_returns_first_success():
    fn = MagicMock(return_value="ok")
    out = retry_on_transient_5xx(fn, base_delay_s=0)
    assert out == "ok"
    fn.assert_called_once()


def test_retry_on_transient_5xx_retries_on_500_then_succeeds():
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _make_500()
        return "recovered"
    out = retry_on_transient_5xx(fn, base_delay_s=0)
    assert out == "recovered"
    assert calls["n"] == 3


def test_retry_on_transient_5xx_reraises_after_exhaust():
    """After `max_attempts` consecutive 5xx failures, the last exc propagates."""
    fn = MagicMock(side_effect=_make_500("persistent outage"))
    with pytest.raises(anthropic.InternalServerError, match="persistent outage"):
        retry_on_transient_5xx(fn, max_attempts=3, base_delay_s=0)
    assert fn.call_count == 3


def test_retry_on_transient_5xx_retries_502_but_not_400():
    """502 is a transient (infra issue), 400 is client-side and propagates."""
    # 502: retried
    calls_502 = {"n": 0}
    def fn_502():
        calls_502["n"] += 1
        if calls_502["n"] == 1:
            raise _make_502()
        return "ok"
    assert retry_on_transient_5xx(fn_502, base_delay_s=0) == "ok"
    assert calls_502["n"] == 2

    # 400: propagates immediately, no retry
    fn_400 = MagicMock(side_effect=_make_400())
    with pytest.raises(anthropic.BadRequestError):
        retry_on_transient_5xx(fn_400, base_delay_s=0)
    fn_400.assert_called_once()


def test_retry_on_transient_5xx_non_anthropic_error_propagates():
    """Regular exceptions (ValueError etc.) aren't treated as transients."""
    fn = MagicMock(side_effect=ValueError("not anthropic"))
    with pytest.raises(ValueError, match="not anthropic"):
        retry_on_transient_5xx(fn, base_delay_s=0)
    fn.assert_called_once()


# ---------------------------------------------------------------------------
# RunBudget
# ---------------------------------------------------------------------------


def test_budget_unlimited_never_raises():
    b = RunBudget.unlimited()
    for _ in range(100):
        b.spend(1_000_000)  # Spend $1M per call, 100 times
    assert b.spent_usd == 100_000_000


def test_budget_within_cap_passes():
    b = RunBudget(cap_usd=10.0)
    b.spend(3.0, "agent_1")
    b.spend(4.0, "agent_2")
    assert b.spent_usd == 7.0
    assert b.remaining() == 3.0


def test_budget_exceeded_raises_budget_error():
    b = RunBudget(cap_usd=5.0)
    b.spend(3.0, "agent_1")
    with pytest.raises(BudgetExceededError) as ei:
        b.spend(3.0, "agent_2")  # 6.0 > 5.0
    assert ei.value.cap == 5.0
    assert ei.value.spent == 6.0
    assert "agent_2" in ei.value.last_reason


def test_budget_negative_amount_clamped_to_zero():
    """Refunds aren't real — defend against an Agent 5 cost calc bug that
    produces a negative value."""
    b = RunBudget(cap_usd=10.0)
    b.spend(-5.0, "buggy")
    assert b.spent_usd == 0


def test_budget_on_spend_callback_fires():
    events = []
    b = RunBudget(cap_usd=10.0, on_spend=lambda amt, reason, cum: events.append((amt, reason, cum)))
    b.spend(2.0, "x")
    b.spend(3.0, "y")
    assert events == [(2.0, "x", 2.0), (3.0, "y", 5.0)]


def test_budget_on_spend_callback_failure_does_not_break_spending():
    """If the SSE emit callback raises, the budget still records the spend."""
    def explode(*a, **kw):
        raise RuntimeError("subscriber crashed")
    b = RunBudget(cap_usd=10.0, on_spend=explode)
    b.spend(1.0, "x")  # should not raise
    assert b.spent_usd == 1.0


def test_budget_snapshot_shape():
    b = RunBudget(cap_usd=10.0)
    b.spend(3.0, "x")
    snap = b.snapshot()
    assert snap["spent_usd"] == 3.0
    assert snap["cap_usd"] == 10.0
    assert snap["remaining_usd"] == 7.0
    assert snap["utilization"] == 0.3
    assert snap["event_count"] == 1


def test_budget_snapshot_unlimited_shows_none_cap():
    snap = RunBudget.unlimited().snapshot()
    assert snap["cap_usd"] is None
    assert snap["remaining_usd"] is None
    assert snap["utilization"] == 0.0


def test_budget_thread_safety_concurrent_spends():
    """Lock-protected: 10 threads each calling spend() 100 times produce
    deterministic total."""
    import threading
    b = RunBudget(cap_usd=10_000.0)
    def worker():
        for _ in range(100):
            b.spend(1.0, "concurrent")
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.spent_usd == 1000.0
    assert len(b.history) == 1000
