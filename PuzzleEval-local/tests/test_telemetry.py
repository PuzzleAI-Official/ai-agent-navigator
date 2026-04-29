"""Tests for the consolidated telemetry package (Phase 0).

Verifies:
  * Public API surface (TelemetryContext, record_llm_call, track_time, etc.)
  * Cost calculation matches expected values for each model + cache scenario
  * Pricing table lookup
  * Time tracking accuracy
  * TelemetryContext immutability and clone helpers
  * Back-compat shims at legacy import paths
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from puzzleeval import telemetry as obs


# ---------------------------------------------------------------------------
# TelemetryContext
# ---------------------------------------------------------------------------


class TestTelemetryContext:
    def test_construct_with_required_fields(self):
        ctx = obs.TelemetryContext(trace_id="abc", agent_id="agent_5")
        assert ctx.trace_id == "abc"
        assert ctx.agent_id == "agent_5"
        assert ctx.run_id is None
        assert ctx.candidate_id is None

    def test_is_frozen(self):
        ctx = obs.TelemetryContext(trace_id="abc", agent_id="agent_5")
        with pytest.raises((AttributeError, Exception)):
            ctx.trace_id = "xyz"  # type: ignore[misc]

    def test_with_operation_returns_new_instance(self):
        ctx = obs.TelemetryContext(trace_id="abc", agent_id="agent_5")
        ctx2 = ctx.with_operation("build")
        assert ctx is not ctx2
        assert ctx2.operation == "build"
        assert ctx.operation is None  # original unchanged

    def test_with_candidate_clones_and_updates(self):
        ctx = obs.TelemetryContext(trace_id="abc", agent_id="agent_5")
        ctx2 = ctx.with_candidate("OpenAI Realtime")
        assert ctx2.candidate_id == "OpenAI Realtime"
        assert ctx.candidate_id is None

    def test_to_log_extra_drops_none_fields(self):
        ctx = obs.TelemetryContext(trace_id="abc", agent_id="agent_5")
        extra = ctx.to_log_extra()
        assert extra == {"trace_id": "abc"}
        # No None values leak
        assert "run_id" not in extra
        assert "candidate_id" not in extra

    def test_to_log_extra_includes_all_set_fields(self):
        ctx = obs.TelemetryContext(
            trace_id="abc",
            agent_id="agent_5",
            run_id="run-123",
            candidate_id="cand-1",
            operation="build_turn",
        )
        extra = ctx.to_log_extra()
        assert extra["trace_id"] == "abc"
        assert extra["run_id"] == "run-123"
        assert extra["candidate_id"] == "cand-1"
        assert extra["operation"] == "build_turn"

    def test_with_extra_merges(self):
        ctx = obs.TelemetryContext(
            trace_id="abc", agent_id="agent_5", extra={"phase": "build"},
        )
        ctx2 = ctx.with_extra(turn=3)
        assert ctx2.extra == {"phase": "build", "turn": 3}


# ---------------------------------------------------------------------------
# Pricing tables
# ---------------------------------------------------------------------------


class TestPricingTables:
    def test_lookup_known_models(self):
        for model in [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]:
            inp, out = obs.lookup_pricing(model)
            assert inp > 0, f"{model} input price is zero"
            assert out > 0, f"{model} output price is zero"
            assert out > inp, f"{model} output should be more expensive than input"

    def test_lookup_unknown_model_returns_zeros_by_default(self):
        inp, out = obs.lookup_pricing("totally-fake-model")
        assert inp == 0.0
        assert out == 0.0

    def test_lookup_unknown_model_with_default(self):
        defaults = (1.0 / 1_000_000, 5.0 / 1_000_000)
        inp, out = obs.lookup_pricing("totally-fake-model", default=defaults)
        assert (inp, out) == defaults

    def test_cache_multipliers_are_correct(self):
        assert obs.CACHE_WRITE_MULTIPLIER_5M == 1.25
        assert obs.CACHE_WRITE_MULTIPLIER_1H == 2.00
        assert obs.CACHE_READ_MULTIPLIER == 0.10

    def test_web_search_pricing(self):
        assert obs.WEB_SEARCH_PRICE_PER_SEARCH == 0.01

    def test_model_pricing_table_immutable_keys(self):
        # We don't enforce frozen-ness but the table should be stable;
        # this catches accidental mutation in tests.
        opus = obs.MODEL_PRICING.get("claude-opus-4-7")
        assert opus == (5.0 / 1_000_000, 25.0 / 1_000_000)


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


def _fake_response(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
):
    """Build a SimpleNamespace mimicking an Anthropic response.usage."""
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
    return SimpleNamespace(usage=usage, stop_reason="end_turn")


class TestCostCalculation:
    def test_basic_cost_no_cache(self):
        response = _fake_response(input_tokens=1000, output_tokens=500)
        cost = obs.estimate_cost(model="claude-sonnet-4-6", response=response)
        # Sonnet: $3/1M input, $15/1M output
        # 1000 * 3/1M = 0.003; 500 * 15/1M = 0.0075; total = 0.0105
        assert cost == pytest.approx(0.0105, rel=1e-6)

    def test_cost_with_cache_write(self):
        response = _fake_response(
            input_tokens=1000, output_tokens=500, cache_creation=2000,
        )
        cost = obs.estimate_cost(model="claude-sonnet-4-6", response=response)
        # 1000 input @ $3/1M = 0.003
        # 2000 cache_write @ $3/1M * 1.25 = 0.0075
        # 500 output @ $15/1M = 0.0075
        # Total = 0.018
        assert cost == pytest.approx(0.018, rel=1e-6)

    def test_cost_with_cache_read_is_cheap(self):
        response = _fake_response(
            input_tokens=100, output_tokens=500, cache_read=10_000,
        )
        cost = obs.estimate_cost(model="claude-sonnet-4-6", response=response)
        # 100 input @ $3/1M = 0.0003
        # 10000 cache_read @ $3/1M * 0.10 = 0.003
        # 500 output @ $15/1M = 0.0075
        # Total = ~0.0108
        assert cost == pytest.approx(0.0108, rel=1e-6)

    def test_calculate_call_cost_back_compat(self):
        """The legacy entry point still works with the new pricing tables."""
        response = _fake_response(input_tokens=1000, output_tokens=500)
        cost = obs.calculate_call_cost(
            response,
            "claude-sonnet-4-6",
            model_pricing=obs.MODEL_PRICING,
            web_search_price_per_search=obs.WEB_SEARCH_PRICE_PER_SEARCH,
        )
        assert cost == pytest.approx(0.0105, rel=1e-6)


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


class TestTiming:
    def test_track_time_measures_elapsed(self):
        with obs.track_time("test_op") as timer:
            time.sleep(0.02)
        assert timer.elapsed_ms >= 20
        assert timer.elapsed_ms < 200, "elapsed shouldn't be 200x the sleep"
        assert timer.stopped

    def test_track_time_inside_context_returns_partial(self):
        with obs.track_time("test_op") as timer:
            time.sleep(0.005)
            mid = timer.elapsed_ms
            time.sleep(0.005)
        end = timer.elapsed_ms
        # Mid should be ≤ end (timer measures elapsed-so-far inside context)
        assert mid <= end + 1  # +1 to allow rounding noise

    def test_timer_operation_label_preserved(self):
        with obs.track_time("agent_5_build_turn") as timer:
            pass
        assert timer.operation == "agent_5_build_turn"

    def test_elapsed_seconds_matches_ms(self):
        with obs.track_time("test_op") as timer:
            time.sleep(0.02)
        # elapsed_seconds is elapsed_ms / 1000 with independent rounding
        # (elapsed_ms rounds to 2 decimals, elapsed_seconds to 4). Allow
        # 1ms tolerance to absorb the rounding difference.
        assert abs(timer.elapsed_seconds - timer.elapsed_ms / 1000) < 0.001


# ---------------------------------------------------------------------------
# RunBudget — preserved API surface check
# ---------------------------------------------------------------------------


class TestRunBudgetSurface:
    def test_budget_imported_from_telemetry(self):
        budget = obs.RunBudget(cap_usd=10.0)
        budget.spend(1.0, "agent_1")
        snap = budget.snapshot()
        assert snap["spent_usd"] == 1.0
        assert snap["cap_usd"] == 10.0

    def test_budget_exceeded_error(self):
        budget = obs.RunBudget(cap_usd=1.0)
        budget.spend(0.5, "first")
        with pytest.raises(obs.BudgetExceededError):
            budget.spend(1.0, "second")  # cumulative 1.5 > 1.0


# ---------------------------------------------------------------------------
# Logging / record_llm_call
# ---------------------------------------------------------------------------


class TestLogging:
    def test_get_logger_returns_named_logger(self):
        logger = obs.get_logger("test_agent")
        assert logger.name == "test_agent"

    def test_generate_trace_id_returns_uuid_format(self):
        tid = obs.generate_trace_id()
        # UUID4 format: 8-4-4-4-12 hex chars separated by dashes = 36 chars
        assert len(tid) == 36
        assert tid.count("-") == 4

    def test_record_llm_call_returns_cost(self):
        ctx = obs.TelemetryContext(trace_id="abc", agent_id="test_agent")
        response = _fake_response(input_tokens=1000, output_tokens=500)
        cost = obs.record_llm_call(
            ctx,
            model="claude-sonnet-4-6",
            response=response,
            latency_ms=100,
        )
        assert cost == pytest.approx(0.0105, rel=1e-6)

    def test_log_llm_call_legacy_entry_point(self):
        """Legacy entry point preserved for back-compat."""
        logger = obs.get_logger("test_agent")
        response = _fake_response(input_tokens=1000, output_tokens=500)
        cost = obs.log_llm_call(
            logger,
            response,
            "claude-sonnet-4-6",
            "trace-abc",
            time.time() - 0.05,
            "test_op",
        )
        assert cost == pytest.approx(0.0105, rel=1e-6)


# ---------------------------------------------------------------------------
# Back-compat shims
# ---------------------------------------------------------------------------


class TestBackCompatShims:
    def test_legacy_budget_module(self):
        from puzzleeval import budget
        assert budget.RunBudget is obs.RunBudget
        assert budget.BudgetExceededError is obs.BudgetExceededError
        assert budget.DEFAULT_MAX_RUN_COST_USD == obs.DEFAULT_MAX_RUN_COST_USD

    def test_legacy_logging_setup_module(self):
        from puzzleeval import logging_setup
        assert logging_setup.get_logger is obs.get_logger
        assert logging_setup.generate_trace_id is obs.generate_trace_id
        assert logging_setup.log_llm_call is obs.log_llm_call

    def test_legacy_costing_module(self):
        from puzzleeval.agents.agent5 import costing
        assert costing.calculate_call_cost is obs.calculate_call_cost

    def test_config_re_exports_pricing(self):
        from puzzleeval import config as cfg
        assert cfg.MODEL_PRICING is obs.MODEL_PRICING
        assert cfg.CACHE_READ_MULTIPLIER == 0.10
        assert cfg.WEB_SEARCH_PRICE_PER_SEARCH == 0.01
