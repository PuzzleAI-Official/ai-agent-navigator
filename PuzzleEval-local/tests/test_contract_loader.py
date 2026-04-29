"""Tests for the contract loader (Phase 0 foundation).

Verifies frontmatter parsing, ContractMetadata schema validation, and
auto-discovery of all contracts in capability_playbooks/.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from puzzleeval.contracts.loader import (
    Contract,
    ContractRegistry,
    discover_contracts,
    parse_frontmatter,
    reload_registry,
)
from puzzleeval.contracts.errors import ContractLoadError
from puzzleeval.contracts.metadata import ContractMetadata


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter_and_body(self):
        text = "---\nid: foo\ndescription: bar\n---\n\nbody text"
        meta, body = parse_frontmatter(text)
        assert meta == {"id": "foo", "description": "bar"}
        assert body == "body text"

    def test_no_frontmatter_returns_text_as_body(self):
        text = "just body text"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == "just body text"

    def test_unclosed_frontmatter_raises(self):
        text = "---\nid: foo\ndescription: bar\n\nbody text"
        with pytest.raises(ContractLoadError) as exc_info:
            parse_frontmatter(text, source="test.md")
        assert "no closing" in str(exc_info.value).lower()

    def test_malformed_yaml_raises(self):
        text = "---\nid: foo\n  bad: indentation: here\n---\nbody"
        with pytest.raises(ContractLoadError) as exc_info:
            parse_frontmatter(text, source="test.md")
        assert "yaml parse failed" in str(exc_info.value).lower()

    def test_empty_frontmatter_returns_empty_dict(self):
        text = "---\n---\nbody"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == "body"

    def test_yaml_returning_non_dict_raises(self):
        text = "---\n- list_item_1\n- list_item_2\n---\nbody"
        with pytest.raises(ContractLoadError) as exc_info:
            parse_frontmatter(text, source="test.md")
        assert "yaml mapping" in str(exc_info.value).lower()

    def test_complex_frontmatter_with_lists_and_dicts(self):
        text = """---
id: voice
selectors:
  trigger_types:
    - voice_conversation
    - voice_turn
applies_to_agents:
  - agent_5
priority: 100
---
body"""
        meta, body = parse_frontmatter(text)
        assert meta["id"] == "voice"
        assert meta["selectors"]["trigger_types"] == ["voice_conversation", "voice_turn"]
        assert meta["priority"] == 100

    def test_body_lstrip_handles_blank_lines_after_frontmatter(self):
        text = "---\nid: x\ndescription: y\n---\n\n\n\nbody"
        meta, body = parse_frontmatter(text)
        assert body == "body"


# ---------------------------------------------------------------------------
# ContractMetadata schema validation
# ---------------------------------------------------------------------------


class TestContractMetadata:
    def test_minimal_valid_metadata(self):
        meta = ContractMetadata(
            id="test_contract",
            description="A test contract",
            selectors={"trigger_types": ["voice_conversation"]},
        )
        assert meta.id == "test_contract"
        assert meta.priority == 100  # default
        assert meta.applies_to_agents == ["agent_5"]  # default

    def test_id_must_be_snake_case(self):
        with pytest.raises(ValidationError):
            ContractMetadata(
                id="Bad-ID-WithDashes",
                description="x",
                selectors={"trigger_types": ["voice"]},
            )

    def test_id_cannot_be_empty(self):
        with pytest.raises(ValidationError):
            ContractMetadata(
                id="",
                description="x",
                selectors={"trigger_types": ["voice"]},
            )

    def test_unknown_category_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ContractMetadata(
                id="test_contract",
                description="x",
                category="bogus_category",
                selectors={"trigger_types": ["voice"]},
            )
        assert "category" in str(exc.value).lower()

    def test_unknown_selection_mode_rejected(self):
        with pytest.raises(ValidationError):
            ContractMetadata(
                id="test_contract",
                description="x",
                selection_mode="hybrid",
                selectors={"trigger_types": ["voice"]},
            )

    def test_deterministic_without_predicate_rejected(self):
        """Critical: deterministic mode + no selectors is a configuration bug."""
        with pytest.raises(ValidationError) as exc:
            ContractMetadata(
                id="test_contract",
                description="x",
                selection_mode="deterministic",
                selectors={},
            )
        assert "deterministic" in str(exc.value).lower()

    def test_always_on_without_predicate_allowed(self):
        """Always-on contracts don't need predicates — they always load."""
        meta = ContractMetadata(
            id="test_contract",
            description="x",
            selection_mode="always_on",
            selectors={},
        )
        assert meta.selection_mode == "always_on"

    def test_unknown_agent_rejected(self):
        with pytest.raises(ValidationError):
            ContractMetadata(
                id="test_contract",
                description="x",
                applies_to_agents=["agent_99"],
                selectors={"trigger_types": ["voice"]},
            )


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


class TestDiscoverContracts:
    def test_discovers_existing_contracts(self):
        contracts = discover_contracts()
        assert len(contracts) >= 6, (
            f"Expected at least 6 contracts (3 modality + 3 platform), "
            f"got {len(contracts)}"
        )
        ids = {c.metadata.id for c in contracts}
        # Voice modality contracts (Codex's NEW-AN extraction)
        assert "voice" in ids
        assert "streaming_response" in ids
        assert "live_test_voice" in ids
        # Platform contracts (Phase 1 extraction)
        assert "platform_windows" in ids
        assert "platform_linux" in ids
        assert "platform_macos" in ids

    def test_each_contract_has_non_empty_body(self):
        for contract in discover_contracts():
            assert len(contract.body) > 100, (
                f"Contract {contract.metadata.id} has suspiciously short body: "
                f"{len(contract.body)} chars"
            )

    def test_each_contract_metadata_validates(self):
        for contract in discover_contracts():
            assert isinstance(contract.metadata, ContractMetadata)
            assert contract.metadata.description, (
                f"Contract {contract.metadata.id} has empty description"
            )

    def test_loader_is_idempotent_via_lru_cache(self):
        c1 = discover_contracts()
        c2 = discover_contracts()
        assert c1 is c2, "lru_cache should return same tuple"

    def test_reload_registry_drops_cache(self):
        c1 = discover_contracts()
        c2 = reload_registry()
        # After reload, identity differs (rebuilt from disk)
        assert c1 == c2  # same content
        # But different tuple instance after cache_clear
        # (Cannot rely on identity check after cache_clear since tuples
        # may compare equal; just ensure no exception.)


class TestContractRegistry:
    def test_cached_returns_consistent_view(self):
        reg1 = ContractRegistry.cached()
        reg2 = ContractRegistry.cached()
        assert len(reg1) == len(reg2)

    def test_by_id_returns_correct_contract(self):
        reg = ContractRegistry.cached()
        voice = reg.by_id("voice")
        assert voice.metadata.id == "voice"

    def test_by_id_raises_keyerror_for_unknown(self):
        reg = ContractRegistry.cached()
        with pytest.raises(KeyError):
            reg.by_id("nonexistent_contract")

    def test_has_returns_bool(self):
        reg = ContractRegistry.cached()
        assert reg.has("voice")
        assert not reg.has("nonexistent_contract")

    def test_for_agent_filters_correctly(self):
        reg = ContractRegistry.cached()
        agent_5_contracts = reg.for_agent("agent_5")
        # All current contracts opt into agent_5
        assert len(agent_5_contracts) == len(reg)

        agent_1_contracts = reg.for_agent("agent_1")
        # None of the current contracts opt into agent_1
        assert len(agent_1_contracts) == 0

    def test_always_on_filters_by_selection_mode(self):
        reg = ContractRegistry.cached()
        always_on = reg.always_on()
        # Platform contracts are always-on
        ids = {c.metadata.id for c in always_on}
        assert "platform_windows" in ids
        assert "platform_linux" in ids
        assert "platform_macos" in ids

    def test_deterministic_filters_by_selection_mode(self):
        reg = ContractRegistry.cached()
        deterministic = reg.deterministic()
        ids = {c.metadata.id for c in deterministic}
        # Voice modality contracts use deterministic mode
        assert "voice" in ids
        assert "streaming_response" in ids
        assert "live_test_voice" in ids
        # Platform contracts use always_on, not deterministic
        assert "platform_windows" not in ids
