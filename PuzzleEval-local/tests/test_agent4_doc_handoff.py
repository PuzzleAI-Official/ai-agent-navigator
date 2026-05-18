"""Agent 4 docs-entrypoint and research-handoff regression guards."""

from __future__ import annotations

import json
from pathlib import Path

from puzzleeval.agents.agent5.initial_message import (
    format_prefetched_docs_block,
    format_research_inputs_block,
)
from puzzleeval.docs_entrypoint import (
    docs_entrypoint_allows_automatic_build,
    read_docs_entrypoint,
    write_docs_entrypoint,
)
from puzzleeval.research_handoff import read_research_handoff, write_research_handoff
from puzzleeval.schemas import ScreenedCandidate


def _candidate(**updates) -> ScreenedCandidate:
    data = {
        "name": "Example Voice",
        "provider": "Example",
        "description": "Voice agent API",
        "pricing_model": "usage-based",
        "pricing_details": "test tier",
        "claimed_capabilities": ["voice"],
        "relevance_score": 0.9,
        "adoption_difficulty": "medium",
        "relevant_subtasks": ["voice conversation"],
        "source": "https://example.test",
        "verified_api_docs_url": "https://docs.example.test/api",
        "auth_method": "api_key",
        "api_access_method": "free_tier",
        "confirmed_capabilities": ["voice conversation"],
        "rate_limit_info": "not_found",
        "data_format_notes": "audio in, audio out",
        "screening_notes": "Fetched official API docs with endpoints and auth.",
    }
    data.update(updates)
    return ScreenedCandidate(**data)


def _write_fetched_docs(tmp_path: Path, url: str = "https://docs.example.test/api") -> None:
    (tmp_path / "fetched_docs_0.txt").write_text(
        f"# Fetched from: {url}\n"
        "# Evidence status: fetched_current_api_docs\n\n"
        "POST /v1/conversation\nAuthorization: Bearer $KEY\n"
        "Request JSON body and response JSON schema.",
        encoding="utf-8",
    )


def test_docs_entrypoint_is_written_from_fetch_verified_candidate_metadata(tmp_path: Path):
    _write_fetched_docs(tmp_path)
    payload = write_docs_entrypoint(_candidate(), tmp_path)

    assert payload["docs_verdict"] == "verified_docs"
    assert payload["evidence_status"] == "fetched_current_api_docs"
    assert payload["primary_docs_entrypoint"] == "https://docs.example.test/api"
    assert payload["prefetched_docs"][0]["filename"] == "fetched_docs_0.txt"
    assert payload["auth_method"] == "api_key"
    assert read_docs_entrypoint(tmp_path) == payload
    assert docs_entrypoint_allows_automatic_build(_candidate(), tmp_path)


def test_docs_entrypoint_blocks_snippet_only_verified_url(tmp_path: Path):
    payload = write_docs_entrypoint(_candidate(), tmp_path)

    assert payload["docs_verdict"] == "no_verified_docs"
    assert payload["evidence_status"] == "missing_fetch_verified_docs"
    assert payload["primary_docs_entrypoint"] == ""
    assert not docs_entrypoint_allows_automatic_build(_candidate(), tmp_path)


def test_docs_entrypoint_blocks_missing_verified_docs(tmp_path: Path):
    candidate = _candidate(verified_api_docs_url="")
    payload = write_docs_entrypoint(candidate, tmp_path)

    assert payload["docs_verdict"] == "no_verified_docs"
    assert not docs_entrypoint_allows_automatic_build(candidate, tmp_path)


def test_research_handoff_contains_compact_routing_index(tmp_path: Path):
    (tmp_path / "fetched_docs_0.txt").write_text(
        "# Fetched from: https://docs.example.test/api\n\nPOST /v1/conversation",
        encoding="utf-8",
    )

    payload = write_research_handoff(_candidate(), tmp_path)

    assert payload["canonical_docs_urls"] == ["https://docs.example.test/api"]
    assert payload["prefetched_doc_files"] == ["fetched_docs_0.txt"]
    assert payload["source"] == "agent_4_research_handoff"
    assert read_research_handoff(tmp_path) == payload


def test_research_inputs_block_prioritizes_current_artifacts(tmp_path: Path):
    _write_fetched_docs(tmp_path)
    write_docs_entrypoint(_candidate(), tmp_path)
    write_research_handoff(_candidate(), tmp_path)
    rendered = format_research_inputs_block(_candidate(), tmp_path)

    assert "docs_entrypoint.json" in rendered
    assert "research_handoff.json" in rendered
    assert "api_spec.txt" not in rendered
    assert "BuildReadinessChecklist" not in rendered


def test_prefetched_docs_block_renders_without_stale_policy(tmp_path: Path):
    (tmp_path / "fetched_docs_0.txt").write_text(
        "# Fetched from: https://docs.example.test/api\n\n"
        "POST /v1/demo\nAuthorization: Bearer",
        encoding="utf-8",
    )

    rendered = format_prefetched_docs_block(tmp_path)

    assert "fetched_docs_0.txt" in rendered
    assert "docs-entrypoint artifact is the authorization source" in rendered
    assert "checklist" not in rendered.lower()


def test_agent4_templates_no_longer_request_retired_checklist():
    verification = Path("puzzleeval/agents/agent4/templates/verification_system.md").read_text(
        encoding="utf-8",
    )
    structure = Path("puzzleeval/agents/agent4/templates/structure_system.md").read_text(
        encoding="utf-8",
    )
    combined = verification + "\n" + structure

    assert "BUILD_READINESS_CHECKLIST" not in combined
    assert "BuildReadinessChecklist" not in combined
    assert "checklist" not in combined.lower()


def test_agent4_templates_do_not_allow_snippet_only_build_eligibility():
    verification = Path("puzzleeval/agents/agent4/templates/verification_system.md").read_text(
        encoding="utf-8",
    )

    assert "search_snippet_only" not in verification
    assert "inaccessible_best_effort" not in verification
    assert "PASS on snippet" not in verification
    assert "Fetch-Verified Determination" in verification


def test_candidate_serialization_ignores_old_checklist_payload():
    payload = json.loads(_candidate().model_dump_json())
    payload["checklist"] = {"old": "payload"}

    parsed = ScreenedCandidate(**payload)

    assert not hasattr(parsed, "checklist")
