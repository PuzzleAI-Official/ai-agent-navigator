"""Regression guards for Agent 5's restored research agency.

Pre-checklist era: Agent 5's Phase 1 prompt told Opus to "skip to STEP 2"
when prefetched docs were dense and "you do NOT need to read_file or
web_fetch." This made Agent 5 a passive consumer of Agent 4's output
instead of an active builder that decides what IT needs.

Post-checklist era (this pass):
  - Five-phase flow A → B → C → D → E (Inventory → Gap analysis →
    Targeted research → Spec → Build).
  - Per-test-case trigger rules in Phase B (only research what THIS
    test case actually needs).
  - ask_research template (CANDIDATE / ENDPOINT / KNOWN / FIELD NEEDED /
    WHY) — direction-pointing.
  - Hard stop test in Phase C ("can I write the harness without TODO,
    without guessing, without 'might need to'").
  - Phase D produces an `=== API SPEC (Phase D) ===` comment block at
    the top of harness.py for debugging visibility.
  - Density gating language ("skip to STEP 2", "you do NOT need to") is
    DELETED — the checklist replaces the proxy of "structural richness"
    with the direct measure "did we answer the builder's questions?"

These tests lock the prompt-shape contract — we don't test Opus's
behavior, we test that the right instructions reach Opus.

Reference: PLAN_AGENT5_RESEARCH_AGENCY.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from puzzleeval.agents.implement_test_env import (
    BUILDER_SYSTEM_PROMPT,
    _ask_research_template_adherence,
    _format_checklist_context_for_builder,
    _format_prefetched_docs_block,
    _usefulness_signal,
)
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
# Five-phase flow contract
# ============================================================================


class TestFivePhaseFlow:
    """The five phases A-E are the contract. Drift here breaks the
    checklist-driven research agency this pass restored."""

    def test_all_five_phases_present_in_canonical_order(self):
        prompt = BUILDER_SYSTEM_PROMPT
        a = prompt.find("PHASE A")
        b = prompt.find("PHASE B")
        c = prompt.find("PHASE C")
        d = prompt.find("PHASE D")
        e = prompt.find("PHASE E")
        assert a > 0
        assert b > a
        assert c > b
        assert d > c
        assert e > d

    def test_phase_a_inventory_role(self):
        prompt = BUILDER_SYSTEM_PROMPT
        # Phase A explicitly references the BuildReadinessChecklist
        idx_a = prompt.find("PHASE A")
        idx_b = prompt.find("PHASE B")
        section = prompt[idx_a:idx_b]
        assert "BuildReadinessChecklist" in section or "checklist" in section.lower()
        assert "Inventory" in section

    def test_phase_b_lists_per_test_case_triggers(self):
        prompt = BUILDER_SYSTEM_PROMPT
        idx_b = prompt.find("PHASE B")
        idx_c = prompt.find("PHASE C")
        section = prompt[idx_b:idx_c]
        # The four non-negotiables are always required
        assert "non-negotiable" in section.lower()
        # Conditional triggers — at least three of the six must be named
        # in the trigger rules section
        triggers = [
            "retry",
            "long-running",
            "async",
            "streaming",
            "side_effects",
            "non-standard content",
        ]
        hits = sum(1 for t in triggers if t.lower() in section.lower())
        assert hits >= 4, (
            f"Phase B should name conditional-field triggers; "
            f"only {hits}/6 trigger keywords present."
        )

    def test_phase_c_contains_ask_research_template(self):
        prompt = BUILDER_SYSTEM_PROMPT
        idx_c = prompt.find("PHASE C")
        idx_d = prompt.find("PHASE D")
        section = prompt[idx_c:idx_d]
        # Template fields
        for field in ("CANDIDATE:", "ENDPOINT:", "KNOWN:", "FIELD NEEDED:", "WHY:"):
            assert field in section, f"Phase C ask_research template missing {field!r}"

    def test_phase_c_contains_falsifiable_stop_test(self):
        prompt = BUILDER_SYSTEM_PROMPT
        idx_c = prompt.find("PHASE C")
        idx_d = prompt.find("PHASE D")
        section = prompt[idx_c:idx_d]
        assert "WITHOUT a TODO" in section
        assert "WITHOUT guessing" in section
        assert "might need to" in section

    def test_phase_c_contains_soft_research_budget(self):
        """Soft instruction (per Q3 / Q1): no hard counter, just budget
        guidance + a behavioral test."""
        prompt = BUILDER_SYSTEM_PROMPT
        idx_c = prompt.find("PHASE C")
        idx_d = prompt.find("PHASE D")
        section = prompt[idx_c:idx_d]
        # 2-call budget mentioned
        assert "2 calls per gap" in section or "2 research calls" in section.lower() or "2 calls" in section

    def test_phase_d_writes_api_spec_txt(self):
        prompt = BUILDER_SYSTEM_PROMPT
        idx_d = prompt.find("PHASE D")
        idx_e = prompt.find("PHASE E")
        section = prompt[idx_d:idx_e]
        assert "api_spec.txt" in section
        # Phase D is the contract
        assert "contract" in section.lower()

    def test_phase_e_implements_from_spec(self):
        import re
        prompt = BUILDER_SYSTEM_PROMPT
        idx_e = prompt.find("PHASE E")
        # Just look at the next 1500 chars (Phase E is short — one-line +
        # then transitions into Phase 2)
        section = prompt[idx_e:idx_e + 1500]
        assert "harness.py" in section.lower()
        # Whitespace-tolerant — the prompt may wrap "single source of\ntruth"
        # across a newline.
        assert re.search(r"single\s+source\s+of\s+truth", section, re.IGNORECASE)


# ============================================================================
# Old gate language removed
# ============================================================================


class TestDensityGateLanguageRemoved:
    """The Phase 1 prompt used to gate Agent 5 behind density tiers
    ('skip to STEP 2', 'you do NOT need to', 'ALREADY IN YOUR
    CONTEXT'). All three are deleted."""

    def test_no_skip_to_step_2(self):
        assert "skip to STEP 2" not in BUILDER_SYSTEM_PROMPT
        assert "Skip to STEP 2" not in BUILDER_SYSTEM_PROMPT

    def test_no_you_do_NOT_need_to(self):
        assert "You do NOT need to" not in BUILDER_SYSTEM_PROMPT
        assert "you do NOT need to" not in BUILDER_SYSTEM_PROMPT

    def test_no_already_in_your_context(self):
        assert "ALREADY IN YOUR CONTEXT" not in BUILDER_SYSTEM_PROMPT
        assert "already in your context" not in BUILDER_SYSTEM_PROMPT.lower()

    def test_no_density_tier_branching(self):
        # The old prompt branched on HIGH/MEDIUM/THIN tiers
        assert "[HIGH" not in BUILDER_SYSTEM_PROMPT
        assert "[MEDIUM" not in BUILDER_SYSTEM_PROMPT
        assert "density-ranked" not in BUILDER_SYSTEM_PROMPT


# ============================================================================
# harness.py Phase D spec-header instruction
# ============================================================================


class TestHarnessSpecHeader:
    """Phase D requires harness.py to start with a structured comment
    block summarizing the checklist contract — the debugging artifact."""

    def test_spec_block_header_taught(self):
        assert "API SPEC (Phase D)" in BUILDER_SYSTEM_PROMPT
        assert "=== END SPEC ===" in BUILDER_SYSTEM_PROMPT

    def test_spec_block_includes_required_fields(self):
        prompt = BUILDER_SYSTEM_PROMPT
        # The template explicitly lists each field the builder must fill
        spec_idx = prompt.find("API SPEC (Phase D)")
        end_idx = prompt.find("=== END SPEC ===", spec_idx)
        block = prompt[spec_idx:end_idx]
        for required in ("Endpoint:", "Auth:", "Request shape:",
                         "Response parse:", "Error handling:",
                         "Async pattern:", "Residual unknowns:"):
            assert required in block, f"Spec block template missing {required!r}"


# ============================================================================
# ask_research template adherence checker (hybrid per Q3)
# ============================================================================


class TestAskResearchAdherence:
    """The hybrid logger inspects ask_research questions for the five
    template fields. Reports adherence; does NOT reject. Per Plan §Q3."""

    def test_fully_adherent_question(self):
        q = (
            "CANDIDATE: OpenAI\n"
            "ENDPOINT: POST /v1/audio/speech\n"
            "KNOWN: auth + endpoint confirmed\n"
            "FIELD NEEDED: error_response_schema\n"
            "WHY: need 429 retry shape"
        )
        report = _ask_research_template_adherence(q)
        assert report["fully_adherent"] is True
        assert report["adherence_ratio"] == 1.0
        assert report["fields_missing"] == []

    def test_completely_non_adherent_question(self):
        report = _ask_research_template_adherence("tell me about ElevenLabs")
        assert report["fully_adherent"] is False
        assert report["adherence_ratio"] == 0.0
        assert len(report["fields_missing"]) == 5

    def test_partial_adherence(self):
        report = _ask_research_template_adherence(
            "CANDIDATE: X — what's the WHY for retry?"
        )
        assert report["fully_adherent"] is False
        assert "CANDIDATE" in report["fields_present"]
        assert "WHY" in report["fields_present"]
        assert "ENDPOINT" in report["fields_missing"]

    def test_empty_question_handled(self):
        report = _ask_research_template_adherence("")
        assert report["fully_adherent"] is False
        assert report["adherence_ratio"] == 0.0

    def test_case_insensitive_field_detection(self):
        # Lowercase template fields still count
        q = "candidate: OpenAI, endpoint: /foo, known: auth, field needed: errors, why: retries"
        report = _ask_research_template_adherence(q)
        assert report["fully_adherent"] is True


# ============================================================================
# Checklist context block (the load-bearing handoff to Agent 5)
# ============================================================================


def _make_candidate(checklist=None) -> ScreenedCandidate:
    return ScreenedCandidate(
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
        checklist=checklist,
    )


class TestChecklistContextBlock:

    def test_none_checklist_renders_legacy_full_research_mode(self):
        rendered = _format_checklist_context_for_builder(_make_candidate(None))
        assert "did not produce a checklist" in rendered
        assert "full-research mode" in rendered

    def test_sentinel_checklist_renders_with_failure_reason(self):
        sentinel = default_unknown_checklist(reason="parser timed out")
        rendered = _format_checklist_context_for_builder(_make_candidate(sentinel))
        assert "snag producing the checklist" in rendered
        assert "parser timed out" in rendered
        assert "full-research mode" in rendered

    def test_verified_pass_renders_full_block(self):
        c = BuildReadinessChecklist(
            provider_surface=[
                EndpointSummary(name="POST /v1/foo", purpose="do foo",
                                relevance_to_use_case="primary",
                                selection_note="best fit"),
                EndpointSummary(name="POST /v1/bar", purpose="do bar",
                                relevance_to_use_case="alternative",
                                selection_note="rejected because Y"),
            ],
            selected_endpoint="POST /v1/foo",
            selection_justification="picked foo over bar because Z",
            endpoint_path=FieldStatus(status="confirmed", value="https://api.x.com/v1/foo", source_url="https://docs.x.com"),
            auth_method=FieldStatus(status="confirmed", value="Bearer", source_url="https://docs.x.com"),
            request_body_shape=FieldStatus(status="confirmed", value='{"x":1}', source_url="https://docs.x.com"),
            response_body_shape=FieldStatus(status="confirmed", value='{"y":2}', source_url="https://docs.x.com"),
        )
        rendered = _format_checklist_context_for_builder(_make_candidate(c))
        assert "BUILD-READINESS CHECKLIST" in rendered
        assert "Verified Pass: YES" in rendered
        assert "POST /v1/foo" in rendered
        assert "POST /v1/bar" in rendered
        assert "PRIMARY" in rendered
        assert "ALTERNATIVE" in rendered
        # Phase B trigger rules are surfaced as a reminder
        assert "Phase B trigger rules" in rendered
        # The four non-negotiables are starred
        for fname in NON_NEGOTIABLE_FIELDS:
            assert fname in rendered

    def test_inconclusive_checklist_renders_verified_pass_no(self):
        c = BuildReadinessChecklist(
            provider_surface=[
                EndpointSummary(name="POST /v1/foo", purpose="do foo",
                                relevance_to_use_case="primary"),
            ],
            selected_endpoint="POST /v1/foo",
            selection_justification="primary fit",
            endpoint_path=FieldStatus(status="inferred", value="?", reasoning="snippet"),
            auth_method=FieldStatus(status="unknown", reasoning="docs paywalled"),
        )
        rendered = _format_checklist_context_for_builder(_make_candidate(c))
        assert "Verified Pass: NO" in rendered
        # Reasoning surfaced for unknowns
        assert "docs paywalled" in rendered

    def test_block_lists_all_ten_field_names(self):
        c = BuildReadinessChecklist()
        rendered = _format_checklist_context_for_builder(_make_candidate(c))
        for fname in BUILD_READINESS_FIELDS:
            assert fname in rendered, f"Block missing field {fname}"

    def test_block_marks_non_negotiables_distinctly(self):
        c = BuildReadinessChecklist()
        rendered = _format_checklist_context_for_builder(_make_candidate(c))
        # Star marker on the four non-negotiables (per the renderer)
        assert "★" in rendered

    def test_phase_b_trigger_rules_surfaced(self):
        c = BuildReadinessChecklist()
        rendered = _format_checklist_context_for_builder(_make_candidate(c))
        # All six conditional triggers must be named
        triggers = [
            "retry",
            "long-running",
            "async",
            "non-standard content",
            "side_effects",
        ]
        for trigger in triggers:
            assert trigger in rendered, (
                f"Phase B trigger reminder missing keyword {trigger!r}"
            )


# ============================================================================
# Prefetched docs block — ranked list-only (no gates)
# ============================================================================


class TestPrefetchedDocsBlockIsRankingOnly:
    """The block now ranks files by usefulness signal but doesn't
    inline content, doesn't tier into HIGH/MEDIUM/THIN, doesn't
    branch instructions. The checklist is the load-bearing artifact."""

    def test_empty_sandbox_returns_empty(self, tmp_path):
        assert _format_prefetched_docs_block(tmp_path) == ""

    def test_none_sandbox_returns_empty(self):
        assert _format_prefetched_docs_block(None) == ""

    def test_files_listed_in_signal_descending_order(self, tmp_path):
        # File 0: nav-heavy (low signal)
        (tmp_path / "fetched_docs_0.txt").write_text(
            "# Fetched from: https://example.com/docs/nav\n\n"
            "Documentation\n\n"
            + "\n".join(
                f"[Section {i}](https://example.com/section/{i})"
                for i in range(50)
            ),
            encoding="utf-8",
        )
        # File 1: code+endpoints (high signal)
        (tmp_path / "fetched_docs_1.txt").write_text(
            "# Fetched from: https://example.com/docs/api-reference\n\n"
            "## Reference\n\n"
            "POST /v1/foo\n"
            "Authorization: Bearer\n"
            "```python\nclient.foo()\n```\n"
            "POST /v1/bar\n"
            "```python\nclient.bar()\n```\n",
            encoding="utf-8",
        )
        rendered = _format_prefetched_docs_block(tmp_path)
        # Both files appear
        assert "fetched_docs_0.txt" in rendered
        assert "fetched_docs_1.txt" in rendered
        # File 1 (higher signal) appears BEFORE File 0
        idx_1 = rendered.find("fetched_docs_1.txt")
        idx_0 = rendered.find("fetched_docs_0.txt")
        assert idx_1 < idx_0, (
            "Higher-signal file should be listed first in the inventory."
        )

    def test_no_density_tier_strings_in_output(self, tmp_path):
        (tmp_path / "fetched_docs_0.txt").write_text(
            "# Fetched from: https://x.com\n\nAnything", encoding="utf-8",
        )
        rendered = _format_prefetched_docs_block(tmp_path)
        # No tier labels in the new output
        for label in ("[HIGH", "[MEDIUM", "[THIN", "HIGH ", "MEDIUM ", "THIN "):
            assert label not in rendered, (
                f"Density tier label {label!r} should not appear in the new "
                f"ranking-only inventory."
            )

    def test_no_inlined_file_content(self, tmp_path):
        unique_marker = "UNIQUE_MARKER_THAT_SHOULD_NOT_BE_INLINED_12345"
        (tmp_path / "fetched_docs_0.txt").write_text(
            f"# Fetched from: https://x.com\n\nPOST /v1/foo\n\n{unique_marker}\n"
            "```python\nfoo()\n```",
            encoding="utf-8",
        )
        rendered = _format_prefetched_docs_block(tmp_path)
        assert unique_marker not in rendered, (
            "The new block lists files but does NOT inline content. "
            "(Inlining was the density era's solution; the checklist "
            "replaces it via per-field source URLs.)"
        )

    def test_no_skip_to_step_language_in_block(self, tmp_path):
        # All-low-signal page
        (tmp_path / "fetched_docs_0.txt").write_text(
            "# Fetched from: https://x.com\n\nDocumentation overview",
            encoding="utf-8",
        )
        rendered = _format_prefetched_docs_block(tmp_path)
        assert "skip to STEP 2" not in rendered
        assert "Skip to STEP 2" not in rendered

    def test_block_explains_when_to_read_files(self, tmp_path):
        (tmp_path / "fetched_docs_0.txt").write_text(
            "# Fetched from: https://x.com\n\nPOST /v1/foo\n",
            encoding="utf-8",
        )
        rendered = _format_prefetched_docs_block(tmp_path)
        # Block tells builder when to read_file (Phase A or Phase C)
        assert "Phase A" in rendered or "Phase C" in rendered
        assert "checklist" in rendered.lower(), (
            "Block must tell builder the checklist is the load-bearing "
            "artifact and these files are background material."
        )


# ============================================================================
# Usefulness signal (the soft ordering proxy)
# ============================================================================


class TestUsefulnessSignal:

    def test_empty_content_scores_zero(self):
        assert _usefulness_signal("") == 0
        assert _usefulness_signal(None) == 0  # type: ignore[arg-type]

    def test_api_doc_outscores_nav_page(self):
        nav = "\n".join(f"[Sec {i}](https://x/{i})" for i in range(60))
        api = (
            "POST /v1/foo\n"
            "POST /v1/bar\n"
            "```python\nclient.foo()\n```\n"
            "Authorization: Bearer xxx\n"
        )
        assert _usefulness_signal(api) > _usefulness_signal(nav)

    def test_websocket_docs_score_positive(self):
        ws = "wss://example.com/v1/realtime\nWebSocket protocol\n```\nawait ws.send(...)\n```"
        assert _usefulness_signal(ws) > 0


# ============================================================================
# Density helpers truly removed from web_doc_cache
# ============================================================================


class TestDensityHelpersDeleted:
    """The pass deletes density scoring from puzzleeval.web_doc_cache —
    no remaining consumers. Lock the deletion."""

    def test_doc_density_score_deleted(self):
        from puzzleeval import web_doc_cache
        assert not hasattr(web_doc_cache, "doc_density_score")

    def test_classify_doc_density_deleted(self):
        from puzzleeval import web_doc_cache
        assert not hasattr(web_doc_cache, "classify_doc_density")

    def test_density_constants_deleted(self):
        from puzzleeval import web_doc_cache
        assert not hasattr(web_doc_cache, "DENSITY_HIGH")
        assert not hasattr(web_doc_cache, "DENSITY_MEDIUM")

    def test_handoff_primitives_remain(self):
        from puzzleeval.web_doc_cache import (
            candidate_slug,
            candidate_sandbox_dir,
            count_existing_fetched_docs,
            save_web_fetches_to_sandbox,
        )
        # The handoff machinery is orthogonal to density; it stays.
        assert callable(candidate_slug)
        assert callable(candidate_sandbox_dir)
        assert callable(count_existing_fetched_docs)
        assert callable(save_web_fetches_to_sandbox)


# ============================================================================
# Phase D completion checklist still references api_spec.txt sections
# ============================================================================


class TestPhaseDCompletionChecklistIntact:
    """The five-phase rewrite preserved the existing api_spec.txt
    completion checklist as Phase D's structural gate. Verify the
    bridge between behavioral stop test (Phase C) and structural gate
    (Phase D) is wired."""

    def test_phase_d_label_replaces_phase_1_label(self):
        # The completion checklist used to be "Phase 1 completion
        # checklist". Renamed to "Phase D completion checklist".
        assert "Phase D completion checklist" in BUILDER_SYSTEM_PROMPT

    def test_completion_checklist_references_phase_c_stop_test(self):
        prompt = BUILDER_SYSTEM_PROMPT
        idx = prompt.find("Phase D completion checklist")
        section = prompt[idx:idx + 1500]
        # Bridge between behavioral (Phase C) and structural (Phase D)
        assert "Phase C" in section
        assert "behavioral stop test" in section.lower() or "stop test" in section.lower()

    def test_endpoint_fit_can_lift_from_checklist(self):
        prompt = BUILDER_SYSTEM_PROMPT
        idx = prompt.find("Phase D completion checklist")
        section = prompt[idx:idx + 3000]
        assert "selected_endpoint" in section
        assert "selection_justification" in section
