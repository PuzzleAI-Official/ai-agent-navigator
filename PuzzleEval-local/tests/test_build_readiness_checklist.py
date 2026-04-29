"""Regression guards for the BuildReadinessChecklist contract.

The checklist is the Agent 4 → Agent 5 handoff artifact. These tests
lock the schema shape, the helper semantics, the deterministic
parser/sentinel paths in Agent 4, and the three-state outcome model.

What the checklist replaces:
  - The density-tier injection block in implement_test_env.py (the
    HIGH/MEDIUM/THIN gating). Density measured surface structure;
    the checklist measures whether each builder question is answered.
  - The "skip to STEP 2" / "you do NOT need to" gate language in the
    Phase 1 prompt that suppressed Agent 5's research agency.

What it adds:
  - Provider-surface survey (catches "wrong endpoint matched for the
    use case" failures).
  - Per-field status with `confirmed | inferred | unknown` so silent
    gaps become explicit knowable unknowns.
  - Three-state rejection model: Verified Pass / Verified Reject /
    Inconclusive — never State 4 (system failure rejects the candidate).

Reference: PLAN_AGENT5_RESEARCH_AGENCY.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from puzzleeval.schemas import (
    BUILD_READINESS_FIELDS,
    CONDITIONAL_FIELDS,
    NON_NEGOTIABLE_FIELDS,
    BuildReadinessChecklist,
    EndpointSummary,
    FieldStatus,
    ScreenedCandidate,
    default_unknown_checklist,
)


# ============================================================================
# Field constants — load-bearing for prompts, validators, and triggers
# ============================================================================


class TestFieldConstants:
    """The four / six / ten split is referenced from prompts, parser,
    and Agent 5 trigger rules. A drift here propagates everywhere."""

    def test_four_non_negotiables(self):
        assert NON_NEGOTIABLE_FIELDS == (
            "endpoint_path",
            "auth_method",
            "request_body_shape",
            "response_body_shape",
        )

    def test_six_conditional_fields(self):
        assert CONDITIONAL_FIELDS == (
            "auth_refresh",
            "error_response_schema",
            "rate_limit_signal",
            "async_pattern",
            "content_type_quirks",
            "sandbox_availability",
        )

    def test_ten_total_fields_in_canonical_order(self):
        assert len(BUILD_READINESS_FIELDS) == 10
        assert BUILD_READINESS_FIELDS == NON_NEGOTIABLE_FIELDS + CONDITIONAL_FIELDS

    def test_no_overlap_between_non_negotiable_and_conditional(self):
        assert not (set(NON_NEGOTIABLE_FIELDS) & set(CONDITIONAL_FIELDS))

    def test_every_field_constant_matches_a_real_schema_field(self):
        """Drift between the constant tuple and the schema is the bug
        we're guarding against."""
        c = BuildReadinessChecklist()
        for fname in BUILD_READINESS_FIELDS:
            assert hasattr(c, fname), (
                f"BUILD_READINESS_FIELDS lists {fname!r} but the schema "
                f"has no such field."
            )
            assert isinstance(getattr(c, fname), FieldStatus)


# ============================================================================
# FieldStatus shape
# ============================================================================


class TestFieldStatusShape:

    def test_all_three_status_values_accepted(self):
        for status in ("confirmed", "inferred", "unknown"):
            fs = FieldStatus(status=status)
            assert fs.status == status

    def test_invalid_status_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            FieldStatus(status="bogus")

    def test_confirmed_carries_value_and_source(self):
        fs = FieldStatus(
            status="confirmed",
            value="https://api.example.com/v1/foo",
            source_url="https://docs.example.com/api",
        )
        assert fs.value
        assert fs.source_url

    def test_unknown_can_carry_only_reasoning(self):
        fs = FieldStatus(status="unknown", reasoning="docs paywalled")
        assert fs.value is None
        assert fs.source_url is None
        assert fs.reasoning


# ============================================================================
# EndpointSummary + provider_surface semantics
# ============================================================================


class TestProviderSurface:

    def test_three_relevance_tags_accepted(self):
        for tag in ("primary", "alternative", "unrelated"):
            ep = EndpointSummary(name="x", purpose="y", relevance_to_use_case=tag)
            assert ep.relevance_to_use_case == tag

    def test_invalid_relevance_tag_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            EndpointSummary(name="x", purpose="y", relevance_to_use_case="bogus")

    def test_has_provider_surface_requires_primary_and_justification(self):
        c = BuildReadinessChecklist(
            provider_surface=[
                EndpointSummary(name="x", purpose="y", relevance_to_use_case="alternative"),
            ],
            selected_endpoint="x",
            selection_justification="picked because",
        )
        assert not c.has_provider_surface(), (
            "no primary tagged → has_provider_surface should be False"
        )

        c.provider_surface[0].relevance_to_use_case = "primary"
        assert c.has_provider_surface()

    def test_has_provider_surface_requires_non_empty_justification(self):
        c = BuildReadinessChecklist(
            provider_surface=[
                EndpointSummary(name="x", purpose="y", relevance_to_use_case="primary"),
            ],
            selected_endpoint="x",
            selection_justification="",
        )
        assert not c.has_provider_surface()


# ============================================================================
# is_verified_pass: the four non-negotiables semantic
# ============================================================================


class TestVerifiedPass:

    def _make_with_all(self, status: str) -> BuildReadinessChecklist:
        fs = lambda: FieldStatus(status=status, value="x" if status != "unknown" else None)
        kwargs = {n: fs() for n in BUILD_READINESS_FIELDS}
        return BuildReadinessChecklist(
            provider_surface=[
                EndpointSummary(name="x", purpose="y", relevance_to_use_case="primary"),
            ],
            selected_endpoint="x",
            selection_justification="picked",
            **kwargs,
        )

    def test_all_confirmed_is_verified_pass(self):
        assert self._make_with_all("confirmed").is_verified_pass()

    def test_all_unknown_is_not_verified_pass(self):
        assert not self._make_with_all("unknown").is_verified_pass()

    def test_all_inferred_is_not_verified_pass(self):
        # "inferred" on a non-negotiable is the Inconclusive state — passes
        # to Agent 5 but is NOT Verified Pass (since the runtime test is
        # the final arbiter for inferred non-negotiables).
        assert not self._make_with_all("inferred").is_verified_pass()

    def test_three_of_four_non_negotiables_confirmed_not_pass(self):
        c = self._make_with_all("confirmed")
        c.endpoint_path = FieldStatus(status="unknown", reasoning="missed")
        assert not c.is_verified_pass()

    def test_conditional_unknown_does_not_block_verified_pass(self):
        c = self._make_with_all("confirmed")
        # Knock all six conditionals to unknown — still Verified Pass
        for fname in CONDITIONAL_FIELDS:
            setattr(c, fname, FieldStatus(status="unknown"))
        assert c.is_verified_pass(), (
            "Conditionals unknown is the common case — still Verified Pass."
        )


# ============================================================================
# fields_with_status helper (Phase B uses this to enumerate gaps)
# ============================================================================


class TestFieldsWithStatus:

    def test_partitions_correctly(self):
        c = BuildReadinessChecklist(
            endpoint_path=FieldStatus(status="confirmed", value="x"),
            auth_method=FieldStatus(status="inferred", reasoning="guess"),
            request_body_shape=FieldStatus(status="confirmed", value="{}"),
            response_body_shape=FieldStatus(status="unknown"),
        )
        confirmed = c.fields_with_status("confirmed")
        inferred = c.fields_with_status("inferred")
        unknown = c.fields_with_status("unknown")

        assert "endpoint_path" in confirmed
        assert "request_body_shape" in confirmed
        assert "auth_method" in inferred
        # response_body_shape PLUS all six unset conditionals (default unknown)
        assert "response_body_shape" in unknown
        # All buckets sum to ten
        assert len(confirmed) + len(inferred) + len(unknown) == len(BUILD_READINESS_FIELDS)


# ============================================================================
# Sentinel checklist (default_unknown_checklist factory)
# ============================================================================


class TestSentinelChecklist:

    def test_sentinel_is_all_unknown(self):
        s = default_unknown_checklist()
        for fname in BUILD_READINESS_FIELDS:
            assert getattr(s, fname).status == "unknown"

    def test_sentinel_default_populated_by_is_system_failure(self):
        s = default_unknown_checklist()
        assert s.populated_by == "system_failure"

    def test_sentinel_carries_reason_on_every_field(self):
        s = default_unknown_checklist(reason="Agent 4 timed out")
        for fname in BUILD_READINESS_FIELDS:
            assert "Agent 4 timed out" in (getattr(s, fname).reasoning or "")

    def test_sentinel_is_not_verified_pass(self):
        assert not default_unknown_checklist().is_verified_pass()
        assert not default_unknown_checklist().has_provider_surface()


# ============================================================================
# JSON round-trip — schema must serialize/deserialize cleanly for SSE +
# pipeline persistence
# ============================================================================


class TestJSONRoundTrip:

    def _full_checklist(self) -> BuildReadinessChecklist:
        return BuildReadinessChecklist(
            provider_surface=[
                EndpointSummary(
                    name="POST /v1/foo",
                    purpose="do foo",
                    relevance_to_use_case="primary",
                    selection_note="matches use case",
                ),
                EndpointSummary(
                    name="POST /v1/bar",
                    purpose="do bar",
                    relevance_to_use_case="alternative",
                    selection_note="rejected because",
                ),
            ],
            selected_endpoint="POST /v1/foo",
            selection_justification="picked over /bar because X",
            endpoint_path=FieldStatus(status="confirmed", value="https://api.example.com/v1/foo", source_url="https://docs.example.com/foo"),
            auth_method=FieldStatus(status="confirmed", value="Bearer token", source_url="https://docs.example.com/auth"),
            request_body_shape=FieldStatus(status="confirmed", value='{"x":1}', source_url="https://docs.example.com/foo"),
            response_body_shape=FieldStatus(status="confirmed", value='{"y":2}', source_url="https://docs.example.com/foo"),
            auth_refresh=FieldStatus(status="unknown", reasoning="not in docs"),
            error_response_schema=FieldStatus(status="inferred", value="{}", reasoning="one example"),
            populated_by="agent_4",
            last_updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def test_round_trip_via_model_dump_json(self):
        c = self._full_checklist()
        j = c.model_dump_json()
        c2 = BuildReadinessChecklist.model_validate_json(j)
        assert c2.is_verified_pass()
        assert c2.has_provider_surface()
        assert c2.selected_endpoint == "POST /v1/foo"
        assert c2.endpoint_path.source_url == "https://docs.example.com/foo"

    def test_round_trip_via_dict(self):
        c = self._full_checklist()
        d = c.model_dump()
        c2 = BuildReadinessChecklist.model_validate(d)
        assert c2.is_verified_pass()


# ============================================================================
# ScreenedCandidate.checklist — back-compat + happy path
# ============================================================================


class TestScreenedCandidateChecklistField:

    def _minimal_candidate(self, **overrides) -> ScreenedCandidate:
        defaults = dict(
            name="TestCo",
            provider="TestProvider",
            description="d",
            pricing_model="per-token",
            claimed_capabilities=["foo"],
            relevance_score=0.9,
            adoption_difficulty="easy",
            relevant_subtasks=["foo"],
            source="https://x.com",
            verified_api_docs_url="https://docs.x.com",
            auth_method="bearer_token",
            api_access_method="free_signup",
            confirmed_capabilities=["foo"],
            data_format_notes="JSON",
            screening_notes="ok",
        )
        defaults.update(overrides)
        return ScreenedCandidate(**defaults)

    def test_checklist_field_optional(self):
        sc = self._minimal_candidate()
        assert sc.checklist is None

    def test_checklist_field_accepts_full_object(self):
        c = BuildReadinessChecklist(
            endpoint_path=FieldStatus(status="confirmed", value="x"),
        )
        sc = self._minimal_candidate(checklist=c)
        assert sc.checklist is c
        # round-trip via JSON
        j = sc.model_dump_json()
        sc2 = ScreenedCandidate.model_validate_json(j)
        assert sc2.checklist is not None
        assert sc2.checklist.endpoint_path.status == "confirmed"

    def test_legacy_candidate_without_checklist_loads_clean(self):
        """Cached agent_4_output.json from runs predating this schema
        must still parse."""
        legacy_json = self._minimal_candidate().model_dump()
        legacy_json.pop("checklist", None)
        sc = ScreenedCandidate.model_validate(legacy_json)
        assert sc.checklist is None


# ============================================================================
# Deterministic checklist parser in screening.py
# ============================================================================


class TestExtractChecklistFromFindings:
    """The parser is the safety-critical contract per AD-007.
    Structuring LLM might drop the nested JSON; parser doesn't."""

    def _findings_with_checklist(self, checklist_json: str) -> str:
        return (
            "CANDIDATE: TestCo\n"
            "DETERMINATION: PASS\n"
            "EVIDENCE: docs found\n\n"
            "```json BUILD_READINESS_CHECKLIST\n"
            f"{checklist_json}\n"
            "```\n"
        )

    def _well_formed_checklist_json(self) -> str:
        return json.dumps({
            "provider_surface": [
                {"name": "POST /v1/foo", "purpose": "do foo", "relevance_to_use_case": "primary"},
            ],
            "selected_endpoint": "POST /v1/foo",
            "selection_justification": "picked",
            "endpoint_path": {"status": "confirmed", "value": "https://api.x.com/v1/foo", "source_url": "https://docs.x.com"},
            "auth_method": {"status": "confirmed", "value": "Bearer", "source_url": "https://docs.x.com"},
            "request_body_shape": {"status": "confirmed", "value": '{"x":1}', "source_url": "https://docs.x.com"},
            "response_body_shape": {"status": "confirmed", "value": '{"y":2}', "source_url": "https://docs.x.com"},
            "auth_refresh": {"status": "unknown"},
            "error_response_schema": {"status": "unknown"},
            "rate_limit_signal": {"status": "unknown"},
            "async_pattern": {"status": "confirmed", "value": "sync"},
            "content_type_quirks": {"status": "confirmed", "value": "json"},
            "sandbox_availability": {"status": "unknown"},
        })

    def test_parses_well_formed_block(self):
        from puzzleeval.agents.agent4.core import _extract_checklist_from_findings
        findings = self._findings_with_checklist(self._well_formed_checklist_json())
        c = _extract_checklist_from_findings(findings, "TestCo")
        assert c.populated_by == "agent_4"
        assert c.is_verified_pass()
        assert c.has_provider_surface()
        assert c.last_updated_at, "parser should auto-stamp timestamp"

    def test_returns_sentinel_when_block_missing(self):
        from puzzleeval.agents.agent4.core import _extract_checklist_from_findings
        findings = "CANDIDATE: TestCo\nDETERMINATION: PASS\nEVIDENCE: some\n"
        c = _extract_checklist_from_findings(findings, "TestCo")
        assert c.populated_by == "system_failure"
        assert "no BUILD_READINESS_CHECKLIST block" in (c.endpoint_path.reasoning or "")

    def test_returns_sentinel_on_malformed_json(self):
        from puzzleeval.agents.agent4.core import _extract_checklist_from_findings
        findings = "```json BUILD_READINESS_CHECKLIST\n{this is not json}\n```"
        c = _extract_checklist_from_findings(findings, "TestCo")
        assert c.populated_by == "system_failure"
        assert "failed to parse" in (c.endpoint_path.reasoning or "")

    def test_returns_sentinel_on_schema_validation_failure(self):
        from puzzleeval.agents.agent4.core import _extract_checklist_from_findings
        invalid = json.dumps({
            "provider_surface": [{"name": "x"}],  # missing required fields
            "endpoint_path": {"status": "BOGUS"},
        })
        findings = self._findings_with_checklist(invalid)
        c = _extract_checklist_from_findings(findings, "TestCo")
        assert c.populated_by == "system_failure"
        assert "schema validation" in (c.endpoint_path.reasoning or "")

    def test_handles_label_without_json_language_hint(self):
        """Some emissions use ```BUILD_READINESS_CHECKLIST without the
        json hint; parser should still find them."""
        from puzzleeval.agents.agent4.core import _extract_checklist_from_findings
        findings = (
            "```BUILD_READINESS_CHECKLIST\n"
            f"{self._well_formed_checklist_json()}\n"
            "```\n"
        )
        c = _extract_checklist_from_findings(findings, "TestCo")
        assert c.populated_by == "agent_4"
        assert c.is_verified_pass()


# ============================================================================
# Three-state outcome model (Plan §Q4)
# ============================================================================


class TestThreeStateOutcome:
    """Verified Pass / Verified Reject / Inconclusive / System Failure.
    System failure NEVER rejects the candidate; runtime test is the
    final arbiter for Inconclusive + System Failure."""

    def test_verified_pass_when_all_non_negotiables_confirmed(self):
        c = BuildReadinessChecklist(
            provider_surface=[
                EndpointSummary(name="x", purpose="y", relevance_to_use_case="primary"),
            ],
            selected_endpoint="x",
            selection_justification="ok",
            endpoint_path=FieldStatus(status="confirmed", value="https://x"),
            auth_method=FieldStatus(status="confirmed", value="Bearer"),
            request_body_shape=FieldStatus(status="confirmed", value="{}"),
            response_body_shape=FieldStatus(status="confirmed", value="{}"),
        )
        assert c.is_verified_pass()

    def test_inconclusive_when_non_negotiable_unknown_or_inferred(self):
        c = BuildReadinessChecklist(
            provider_surface=[
                EndpointSummary(name="x", purpose="y", relevance_to_use_case="primary"),
            ],
            selected_endpoint="x",
            selection_justification="ok",
            endpoint_path=FieldStatus(status="confirmed", value="https://x"),
            auth_method=FieldStatus(status="confirmed", value="Bearer"),
            request_body_shape=FieldStatus(status="inferred", value="{}", reasoning="from snippet"),
            response_body_shape=FieldStatus(status="unknown", reasoning="not in docs"),
        )
        # Inconclusive: not a Verified Pass, but populated_by stays
        # "agent_4" — the candidate still flows downstream. (Per the
        # plan: rejecting State 3 is forbidden; the runtime test is
        # the final arbiter.)
        assert not c.is_verified_pass()
        assert c.populated_by == "agent_4"

    def test_system_failure_never_rejects_candidate(self):
        """The sentinel must indicate failure clearly; the candidate
        still propagates to Agent 5 (Agent 5 falls back to full
        research mode). Per Plan §Q4: rejection is reserved for
        evidence of badness, not absence of evidence."""
        s = default_unknown_checklist(reason="agent 4 crashed")
        assert s.populated_by == "system_failure"
        # The sentinel can still be attached to a ScreenedCandidate
        # (validates), and downstream consumers can inspect populated_by
        # to switch to full-research mode.
        sc = ScreenedCandidate(
            name="X", provider="Y", description="z",
            pricing_model="per-token", claimed_capabilities=[],
            relevance_score=0.5, adoption_difficulty="easy",
            relevant_subtasks=[], source="https://x.com",
            verified_api_docs_url="https://docs.x.com",
            auth_method="unknown", api_access_method="unknown",
            confirmed_capabilities=[], data_format_notes="",
            screening_notes="agent 4 crashed mid-verify",
            checklist=s,
        )
        assert sc.checklist is s
        assert sc.checklist.populated_by == "system_failure"


# ============================================================================
# Agent 4 prompts contain expected new sections (source-grep guards)
# ============================================================================


class TestAgent4PromptHasChecklistSections:

    def test_verification_prompt_teaches_BUILD_READINESS_CHECKLIST(self):
        from puzzleeval.agents.screening import VERIFICATION_SYSTEM_PROMPT
        assert "BUILD_READINESS_CHECKLIST" in VERIFICATION_SYSTEM_PROMPT
        assert "NON-NEGOTIABLE" in VERIFICATION_SYSTEM_PROMPT
        # Mentions all four non-negotiable field names
        for fname in NON_NEGOTIABLE_FIELDS:
            assert fname in VERIFICATION_SYSTEM_PROMPT

    def test_verification_prompt_teaches_three_status_values(self):
        from puzzleeval.agents.screening import VERIFICATION_SYSTEM_PROMPT
        for status in ("confirmed", "inferred", "unknown"):
            assert status in VERIFICATION_SYSTEM_PROMPT

    def test_verification_prompt_teaches_three_relevance_tags(self):
        from puzzleeval.agents.screening import VERIFICATION_SYSTEM_PROMPT
        for tag in ("primary", "alternative", "unrelated"):
            assert tag in VERIFICATION_SYSTEM_PROMPT

    def test_verification_prompt_teaches_three_state_rejection_model(self):
        from puzzleeval.agents.screening import VERIFICATION_SYSTEM_PROMPT
        assert "Verified Pass" in VERIFICATION_SYSTEM_PROMPT
        assert "Verified Reject" in VERIFICATION_SYSTEM_PROMPT
        assert "Inconclusive" in VERIFICATION_SYSTEM_PROMPT
        assert "NEVER reject a candidate for" in VERIFICATION_SYSTEM_PROMPT

    def test_structuring_prompt_teaches_checklist_transcription(self):
        from puzzleeval.agents.screening import STRUCTURE_SYSTEM_PROMPT
        assert "BuildReadinessChecklist" in STRUCTURE_SYSTEM_PROMPT
        assert "system_failure" in STRUCTURE_SYSTEM_PROMPT
        assert "BUILD_READINESS_CHECKLIST" in STRUCTURE_SYSTEM_PROMPT

    def test_verification_prompt_has_provider_surface_section(self):
        from puzzleeval.agents.screening import VERIFICATION_SYSTEM_PROMPT
        assert "provider_surface" in VERIFICATION_SYSTEM_PROMPT
        assert "selection_justification" in VERIFICATION_SYSTEM_PROMPT
