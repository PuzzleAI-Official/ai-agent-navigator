# ============================================================================
# Tests for puzzleeval-api/services/billing.py (Phase 2: Service Tier Scaffold)
# ============================================================================
# Run from the puzzleeval-api directory:
#   ANTHROPIC_API_KEY=dummy python -m pytest tests/test_billing.py -v
#
# These tests cover:
#   - Plan/feature matrix lookups (plan_allows, starting_credits_for)
#   - Per-agent credit cost (credit_cost_for_agent)
#   - require_agent_access in BOTH modes (no-op default + enforced)
#   - Gate triggering increments plan_gates_triggered counter
#   - quota_snapshot serializes correctly for RunStateOut
#   - RunState extension carries plan/credits/gates correctly
#
# The BILLING_ENFORCED constant is read at module import time, so we use
# pytest's monkeypatch fixture to flip it for enforcement tests.
# ============================================================================

import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

# Add puzzleeval-api root to path so `from services...` imports work.
API_ROOT = Path(__file__).resolve().parent.parent
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

# Also need PuzzleEval-local on path (RunState's siblings reference it).
PUZZLEEVAL_DIR = API_ROOT.parent / "PuzzleEval-local"
if str(PUZZLEEVAL_DIR) not in sys.path:
    sys.path.insert(0, str(PUZZLEEVAL_DIR))

# Required by puzzleeval.config — the test harness doesn't make real API calls
# but the module raises if it's missing.
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from services import billing  # noqa: E402
from services.run_manager import RunManager  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_manager():
    """Each test gets its own in-memory RunManager so runs don't leak."""
    return RunManager()


# ---------------------------------------------------------------------------
# Plan / feature matrix
# ---------------------------------------------------------------------------

class TestPlanFeatureMatrix:

    def test_free_plan_allows_search_only(self):
        # Free tier: search is free, testing requires upgrade, monitoring enterprise-only.
        assert billing.plan_allows("free", "search") is True
        assert billing.plan_allows("free", "testing") is False
        assert billing.plan_allows("free", "monitoring") is False

    def test_paid_plan_allows_testing(self):
        assert billing.plan_allows("paid", "search") is True
        assert billing.plan_allows("paid", "testing") is True
        assert billing.plan_allows("paid", "monitoring") is False

    def test_enterprise_plan_allows_everything(self):
        assert billing.plan_allows("enterprise", "search") is True
        assert billing.plan_allows("enterprise", "testing") is True
        assert billing.plan_allows("enterprise", "monitoring") is True

    def test_unknown_plan_allows_nothing(self):
        # Defensive: unknown plan must NOT silently grant features.
        assert billing.plan_allows("hacker_tier", "testing") is False

    def test_starting_credits_distinguish_paid_from_unlimited(self):
        # free / enterprise = unlimited (None); paid = a finite balance.
        assert billing.starting_credits_for("free") is None
        assert billing.starting_credits_for("enterprise") is None
        paid_credits = billing.starting_credits_for("paid")
        assert isinstance(paid_credits, int)
        assert paid_credits > 0


# ---------------------------------------------------------------------------
# Per-agent credit cost
# ---------------------------------------------------------------------------

class TestCreditCostPerAgent:

    def test_search_agents_are_free(self):
        for agent in ("agent_1", "agent_2", "agent_3", "agent_3f"):
            assert billing.credit_cost_for_agent(agent) == 0

    def test_testing_agents_have_nonzero_cost(self):
        # Agent 4 is cheap (single-shot), Agent 5 is expensive (multi-candidate build).
        assert billing.credit_cost_for_agent("agent_4") >= 1
        assert billing.credit_cost_for_agent("agent_5") > billing.credit_cost_for_agent("agent_4")

    def test_unknown_agent_costs_zero(self):
        # Unknown agents shouldn't accidentally bill — fail open in no-op mode.
        assert billing.credit_cost_for_agent("agent_99") == 0


# ---------------------------------------------------------------------------
# RunState extension
# ---------------------------------------------------------------------------

class TestRunStateExtensions:

    def test_default_run_is_free_unlimited(self, fresh_manager):
        state = fresh_manager.create_run("test")
        assert state.plan == "free"
        assert state.credits_remaining is None  # unlimited
        assert state.credits_consumed == 0
        assert state.plan_gates_triggered == 0

    def test_paid_run_gets_starting_credit_balance(self, fresh_manager):
        state = fresh_manager.create_run("test", plan="paid")
        assert state.plan == "paid"
        # Paid plan starts with the configured balance from billing.PLAN_STARTING_CREDITS.
        assert state.credits_remaining == billing.starting_credits_for("paid")

    def test_enterprise_run_is_unlimited(self, fresh_manager):
        state = fresh_manager.create_run("test", plan="enterprise")
        assert state.plan == "enterprise"
        assert state.credits_remaining is None


# ---------------------------------------------------------------------------
# require_agent_access — no-op mode (BILLING_ENFORCED=False, the default)
# ---------------------------------------------------------------------------

class TestRequireAgentAccessNoOp:
    """In default mode, gates track usage but never raise."""

    def test_search_agent_passes_for_free_plan(self, fresh_manager):
        state = fresh_manager.create_run("test", plan="free")
        # Should not raise.
        billing.require_agent_access(state, "agent_2")
        assert state.plan_gates_triggered == 0
        assert state.credits_consumed == 0  # search is free

    def test_testing_agent_records_gate_for_free_plan_but_does_not_raise(
        self, fresh_manager, monkeypatch
    ):
        monkeypatch.setattr(billing, "BILLING_ENFORCED", False)
        state = fresh_manager.create_run("test", plan="free")
        # Free plan can't access testing, but in no-op mode we just record.
        billing.require_agent_access(state, "agent_4")
        assert state.plan_gates_triggered == 1
        assert state.credits_consumed == 0  # gate was hit before consumption

    def test_paid_plan_pays_credits_for_testing_agents(
        self, fresh_manager, monkeypatch
    ):
        monkeypatch.setattr(billing, "BILLING_ENFORCED", False)
        state = fresh_manager.create_run("test", plan="paid")
        starting = state.credits_remaining
        cost = billing.credit_cost_for_agent("agent_4")
        billing.require_agent_access(state, "agent_4")
        assert state.credits_consumed == cost
        assert state.credits_remaining == starting - cost
        assert state.plan_gates_triggered == 0  # gate passed cleanly


# ---------------------------------------------------------------------------
# require_agent_access — enforced mode (BILLING_ENFORCED=True)
# ---------------------------------------------------------------------------

class TestRequireAgentAccessEnforced:
    """When enforced, gate failures raise HTTPException(402)."""

    def test_free_plan_blocks_testing_agent(self, fresh_manager, monkeypatch):
        monkeypatch.setattr(billing, "BILLING_ENFORCED", True)
        state = fresh_manager.create_run("test", plan="free")
        with pytest.raises(HTTPException) as exc_info:
            billing.require_agent_access(state, "agent_5")
        assert exc_info.value.status_code == 402
        assert "testing" in str(exc_info.value.detail).lower()
        # Gate counter still bumped for observability.
        assert state.plan_gates_triggered == 1

    def test_paid_plan_blocks_when_credits_exhausted(self, fresh_manager, monkeypatch):
        monkeypatch.setattr(billing, "BILLING_ENFORCED", True)
        state = fresh_manager.create_run("test", plan="paid")
        # Drain credits so the next agent_5 call must fail.
        state.credits_remaining = 0
        with pytest.raises(HTTPException) as exc_info:
            billing.require_agent_access(state, "agent_5")
        assert exc_info.value.status_code == 402
        assert "insufficient" in str(exc_info.value.detail).lower()

    def test_enterprise_passes_everything(self, fresh_manager, monkeypatch):
        monkeypatch.setattr(billing, "BILLING_ENFORCED", True)
        state = fresh_manager.create_run("test", plan="enterprise")
        # Should not raise on either testing or monitoring.
        billing.require_agent_access(state, "agent_5")
        # credits_remaining stays None (unlimited) so no decrement happens.
        assert state.credits_remaining is None


# ---------------------------------------------------------------------------
# quota_snapshot — what the API surfaces to the frontend
# ---------------------------------------------------------------------------

class TestQuotaSnapshot:

    def test_snapshot_includes_all_quota_fields(self, fresh_manager):
        state = fresh_manager.create_run("test", plan="paid")
        snap = billing.quota_snapshot(state)
        assert snap["plan"] == "paid"
        assert snap["credits_remaining"] == billing.starting_credits_for("paid")
        assert snap["credits_consumed"] == 0
        assert "search" in snap["tier_features"]
        assert "testing" in snap["tier_features"]
        assert snap["tier_features"]["testing"] is True
        assert isinstance(snap["billing_enforced"], bool)

    def test_snapshot_reflects_consumption(self, fresh_manager, monkeypatch):
        monkeypatch.setattr(billing, "BILLING_ENFORCED", False)
        state = fresh_manager.create_run("test", plan="paid")
        billing.require_agent_access(state, "agent_4")
        snap = billing.quota_snapshot(state)
        assert snap["credits_consumed"] == billing.credit_cost_for_agent("agent_4")
        assert snap["credits_remaining"] == (
            billing.starting_credits_for("paid")
            - billing.credit_cost_for_agent("agent_4")
        )
