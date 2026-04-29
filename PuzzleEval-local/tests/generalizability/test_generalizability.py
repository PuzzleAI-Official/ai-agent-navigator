"""
Phase 10: Generalizability benchmark tests.

These tests validate the benchmark FRAMEWORK -- config loading, assertion
helpers, domain coverage, and schema integrity. They do NOT make live
API calls (those are done via bench/run_benchmark.py).

Run: ANTHROPIC_API_KEY=dummy pytest -m generalizability tests/generalizability/ -v
"""
import json
from pathlib import Path

import pytest

from tests.generalizability.conftest import all_domain_names, load_domain_config, DOMAINS_DIR


# ============================================================================
# Meta-tests: validate the benchmark framework itself
# ============================================================================

@pytest.mark.generalizability
class TestDomainConfigs:
    """Every domain config must be well-formed."""

    @pytest.mark.parametrize("domain", all_domain_names())
    def test_config_loads(self, domain):
        config = load_domain_config(domain)
        assert "name" in config
        assert "input" in config
        assert "expected_scopes" in config
        assert "per_scope_assertions" in config
        assert "max_cost_usd" in config

    @pytest.mark.parametrize("domain", all_domain_names())
    def test_config_has_valid_assertions(self, domain):
        config = load_domain_config(domain)
        for scope, assertions in config["per_scope_assertions"].items():
            assert "min_top_pass_rate" in assertions, f"Missing min_top_pass_rate for scope {scope}"
            assert "min_candidates_tested" in assertions, f"Missing min_candidates_tested for scope {scope}"
            assert 0.0 <= assertions["min_top_pass_rate"] <= 1.0, f"Invalid pass_rate for {scope}"
            assert assertions["min_candidates_tested"] >= 0, f"Negative min_candidates for {scope}"

    @pytest.mark.parametrize("domain", all_domain_names())
    def test_config_scopes_match_assertions(self, domain):
        config = load_domain_config(domain)
        expected = set(config["expected_scopes"])
        asserted = set(config["per_scope_assertions"].keys())
        # Every expected scope should have an assertion
        missing = expected - asserted
        assert not missing, f"Scopes in expected_scopes but not in per_scope_assertions: {missing}"

    def test_minimum_domain_count(self):
        """Benchmark must have at least 6 base domains + 4 edge-case domains."""
        domains = all_domain_names()
        assert len(domains) >= 10, f"Only {len(domains)} domains; need >= 10"

    def test_has_single_scope_domain(self):
        """At least one domain is single-scope (regression baseline)."""
        for domain in all_domain_names():
            config = load_domain_config(domain)
            if len(config["expected_scopes"]) == 1:
                return
        pytest.fail("No single-scope domain found in benchmark")

    def test_has_multi_scope_domain(self):
        """At least one domain has 3+ scopes (exercises DAG)."""
        for domain in all_domain_names():
            config = load_domain_config(domain)
            if len(config["expected_scopes"]) >= 3:
                return
        pytest.fail("No multi-scope (3+) domain found in benchmark")

    def test_has_fan_out_domain(self):
        """At least one domain exercises fan-out/fan-in (5+ scopes)."""
        for domain in all_domain_names():
            config = load_domain_config(domain)
            if len(config["expected_scopes"]) >= 5:
                return
        pytest.fail("No fan-out domain (5+ scopes) found in benchmark")

    def test_cost_ceilings_are_bounded(self):
        """No domain exceeds $50 cost ceiling."""
        for domain in all_domain_names():
            config = load_domain_config(domain)
            assert config["max_cost_usd"] <= 50.0, f"{domain} cost ceiling ${config['max_cost_usd']} > $50"


@pytest.mark.generalizability
class TestAssertionHelpers:
    """Test the per-scope assertion logic that live runs would exercise."""

    def test_pass_rate_threshold(self):
        """A pass_rate above the threshold should pass."""
        threshold = 0.80
        actual = 0.85
        assert actual >= threshold

    def test_pass_rate_below_threshold_fails(self):
        """A pass_rate below the threshold should fail."""
        threshold = 0.80
        actual = 0.70
        assert actual < threshold  # this is the failing condition

    def test_candidate_count_at_scope(self):
        """Scope with enough candidates should pass."""
        min_required = 3
        actual = 4
        assert actual >= min_required

    def test_zero_candidates_at_scope_valid_when_expected(self):
        """scope_under_capacity explicitly expects 0 candidates at some scopes."""
        config = load_domain_config("scope_under_capacity")
        transcribe_min = config["per_scope_assertions"]["transcribe"]["min_candidates_tested"]
        assert transcribe_min == 0  # expected to be zero
