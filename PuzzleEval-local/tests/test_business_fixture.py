"""Tests for canonical business fixture synthesis.

The fixture is a production guard against domain facts drifting between
Agent 3 test generation, Agent 5 live tests, rubrics, and reports.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from puzzleeval.agents.agent5.business_fixture import (
    stage_business_fixture,
    synthesize_business_fixture,
    synthesize_business_fixture_from_user_understanding,
    validate_structured_claims_against_business_fixture,
    validate_text_against_business_fixture,
)


def test_no_fact_sensitive_request_skips_fixture():
    input_data = SimpleNamespace(
        user_understanding=SimpleNamespace(summary="Compare summarization APIs"),
        test_cases=SimpleNamespace(test_cases=[]),
    )
    assert synthesize_business_fixture(input_data) is None


def test_user_supplied_domain_facts_become_canonical_fixture():
    input_data = SimpleNamespace(
        user_understanding=SimpleNamespace(
            summary="Evaluate voice ordering for Bean & Brew. Menu includes latte $4.50 and hours 7am-5pm."
        ),
        test_cases=SimpleNamespace(test_cases=[]),
    )
    fixture = synthesize_business_fixture(input_data)
    assert fixture is not None
    assert fixture["synthetic"] is False
    assert fixture["source"] == "user_or_agent1_context"
    assert "latte" in fixture["canonical_facts"][0].lower()
    assert "4.50" in fixture["canonical_facts"][0]


def test_missing_exact_facts_creates_visible_synthetic_gap_marker(tmp_path):
    input_data = SimpleNamespace(
        user_understanding=SimpleNamespace(summary="Evaluate a cafe voice agent for menu and pricing questions."),
        test_cases=SimpleNamespace(test_cases=[]),
    )
    fixture = stage_business_fixture(tmp_path, input_data)
    assert fixture is not None
    assert fixture["synthetic"] is True
    assert "do not demand exact prices" in " ".join(fixture["notes"]).lower()

    payload = json.loads((tmp_path / "_agent_state" / "business_fixture.json").read_text())
    assert payload["source"] == "synthetic_gap_marker"


def test_fixture_consistency_flags_invented_menu_items_and_hours():
    fixture = {
        "schema_version": 1,
        "synthetic": False,
        "canonical_facts": [
            "Menu: Latte $5.50, Drip Coffee $3.00, Espresso $3.25, Croissant $4.00, Muffin $3.50",
            "Hours: Mon-Sat 7am-7pm, closed Sundays",
        ],
    }
    issues = validate_text_against_business_fixture(
        "The caller wants to order cold brew with oat milk. "
        "Tell them pickup is available Sunday and hours are 6am-7pm daily.",
        fixture,
        source_label="test case",
    )
    joined = " ".join(issues).lower()
    assert "cold brew" in joined
    assert "oat milk" in joined
    assert "closed sundays" in joined or "sundays are closed" in joined
    assert "6:00am" in joined


def test_synthetic_fixture_rejects_exact_fact_demands():
    fixture = {
        "schema_version": 1,
        "synthetic": True,
        "canonical_facts": [
            "Fact-sensitive tests were requested, but exact facts were unavailable."
        ],
    }
    issues = validate_text_against_business_fixture(
        "Expected output: quote the cappuccino price as $4.75 and say Sunday pickup is 9am.",
        fixture,
        source_label="test case",
    )
    assert issues
    assert "synthetic gap marker" in issues[0]


def test_fixture_consistency_allows_negative_off_menu_scenario():
    fixture = {
        "schema_version": 1,
        "synthetic": False,
        "canonical_facts": ["Menu: Latte $5.50, Muffin $3.50"],
    }
    issues = validate_text_against_business_fixture(
        "The caller asks to order cold brew, which is not available. "
        "The agent should refuse and offer a latte alternative.",
        fixture,
        source_label="test case",
    )
    assert issues == []


def test_fixture_time_normalization_allows_colon_variant():
    fixture = {
        "schema_version": 1,
        "synthetic": False,
        "canonical_facts": ["Hours: Mon-Sat 7:00 AM-7:00 PM, closed Sundays"],
    }
    issues = validate_text_against_business_fixture(
        "Expected output: say Saturday hours are 7am-7pm.",
        fixture,
        source_label="structured claim",
    )
    assert issues == []


def test_structured_fixture_validation_ignores_raw_source_comments():
    fixture = {
        "schema_version": 1,
        "synthetic": False,
        "canonical_facts": ["Hours: Mon-Sat 7am-7pm, closed Sundays"],
    }
    issues = validate_structured_claims_against_business_fixture(
        [
            {"utterance": "What time do you open Saturday?"},
            {"expected": "Answer with Saturday hours."},
        ],
        fixture,
        source_label="live-test structured evidence",
    )
    assert issues == []

    contradictory = validate_structured_claims_against_business_fixture(
        [{"expected": "Confirm Sunday pickup is available at 9am."}],
        fixture,
        source_label="live-test structured evidence",
    )
    assert contradictory


def test_agent3_generation_prompt_receives_authoritative_fixture():
    pytest.importorskip("anthropic")
    from puzzleeval.agents.agent3.core import _build_generation_message

    user_understanding = SimpleNamespace(
        summary="Evaluate voice ordering for Bean & Brew.",
        domain="coffee shop",
        sub_tasks=[
            SimpleNamespace(
                description="Answer menu and order questions",
                capability="voice_conversation",
                search_keywords=["coffee", "ordering"],
                requires_test_files=False,
            )
        ],
        workflow=None,
        workflow_summary="",
        constraints=SimpleNamespace(
            budget_range=None,
            technical_level=None,
            integration_requirements=[],
        ),
        test_plan=None,
    )
    fixture = {
        "schema_version": 1,
        "synthetic": False,
        "source": "user_or_agent1_context",
        "domain": "coffee shop",
        "canonical_facts": [
            "Menu: Latte $5.50, Drip Coffee $3.00, Espresso $3.25, Croissant $4.00, Muffin $3.50",
            "Hours: Mon-Sat 7am-7pm, closed Sundays",
        ],
    }

    prompt = _build_generation_message(user_understanding, fixture)
    assert "Canonical Business Fixture (AUTHORITATIVE)" in prompt
    assert "Latte $5.50" in prompt
    assert "Do not introduce menu items" in prompt


def test_explicit_fixture_takes_precedence_over_downstream_test_cases():
    user_understanding = SimpleNamespace(
        summary="Evaluate Bean & Brew. Menu includes latte $5.50.",
        domain="coffee shop",
    )
    fixture = synthesize_business_fixture_from_user_understanding(user_understanding)
    input_data = SimpleNamespace(
        user_understanding=user_understanding,
        business_fixture=fixture,
        test_cases=SimpleNamespace(
            business_fixture=None,
            test_cases=[
                SimpleNamespace(input_data="Caller asks for cold brew $9.00.")
            ],
        ),
    )
    assert synthesize_business_fixture(input_data) == fixture
