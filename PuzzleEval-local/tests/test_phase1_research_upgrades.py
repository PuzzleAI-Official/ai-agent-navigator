"""Regression guards for the Phase-1 research-pattern upgrades.

Three teachings added together after real-run audit (2026-04-22) of both
voice runs (6608f88c, f9de380b) and OCR runs (045bbd10, 0de34b79):

  Fix 1 — Atlas-first: the builder reads Agent 4's ScreenedCandidate
          fields (verified_api_docs_url, auth_method, data_format_notes,
          interaction_model, sandbox_available) as Step-0 starting
          points BEFORE issuing any web_search / web_fetch. Agent 4
          already verified these in-run; re-discovering wastes fetches.
          (NOT cross-run memdir — that's deferred.)

  Fix 2 — Adaptive search trigger: after the Step-1 fetch of
          verified_api_docs_url, judge page density. If <200 lines of
          substantive content (landed on TOC/nav), issue a site-scoped
          search + parallel-fetch the top results in one turn. Dense
          pages skip search entirely (current fast path preserved).

  Fix 3 — Completeness gate before api_spec.txt: ERRORS section must
          have ≥2 documented error classes, WORKING_EXAMPLE must have
          ≥1 complete runnable example **in ANY language** (Python,
          curl, JS, Go, Ruby, raw HTTP — not Python-only, since many
          APIs ship curl-first or JS-first docs). These sections
          exist to prevent Phase 2 from hitting opaque 4xx/5xx that
          cost 3-5 debug turns to decode. One extra targeted research
          fetch is cheaper than those debug turns.

Principles verified:
  - GENERAL across modalities (voice, OCR, code, webhook, vision, chat)
  - GENERAL across providers (no hardcoded provider names)
  - NO cross-run memory use (memdir / provider_atlases deferred)
  - Preserves existing fast path for dense-doc providers (Step 1 hit
    covers them; Steps 2-3 only fire on thin pages)
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _builder_prompt() -> str:
    from puzzleeval.agents.implement_test_env import BUILDER_SYSTEM_PROMPT
    return BUILDER_SYSTEM_PROMPT


def _phase1_section() -> str:
    prompt = _builder_prompt()
    start = prompt.find("## PHASE 1: RESEARCH")
    end = prompt.find("## PHASE 2: BUILD")
    assert start != -1 and end != -1
    return prompt[start:end]


class TestFix1AtlasFirstTeaching:
    """Agent 5's Phase 1 must teach the builder to consume Agent 4's
    pre-verified output (now the BuildReadinessChecklist) BEFORE
    issuing any web_search or web_fetch.

    Migration note: the original "STEP 0: atlas-first" structure was
    superseded by the five-phase A→B→C→D→E flow in the build-readiness-
    checklist pass. PHASE A (Inventory) is the new "atlas-first" — but
    instead of pointing at ad-hoc atlas fields, it points at the
    structured checklist that enumerates all ten build-readiness
    questions with confirmed/inferred/unknown statuses.
    """

    def test_phase1_has_inventory_phase_first(self):
        phase1 = _phase1_section()
        # The Phase D refactor collapsed PHASE A-E into three canonical
        # steps. "Step 1 — Inventory" is the new "read what Agent 4 gave
        # you" step.
        assert "Step 1 — Inventory" in phase1
        idx_1 = phase1.find("Step 1 — Inventory")
        idx_2 = phase1.find("Step 2 — Gap analysis")
        section = phase1[idx_1:idx_2]
        # Step 1 should reference the checklist (the new atlas).
        assert "checklist" in section.lower() or "BuildReadinessChecklist" in section

    def test_phase1_references_verified_api_docs_url(self):
        """The collapsed three-step prompt no longer name-drops
        `verified_api_docs_url` directly; it points at Agent 4's
        BuildReadinessChecklist + prefetched docs as the inventory.
        Verify Phase 1 still grounds the builder against Agent 4's
        output (which carries verified_api_docs_url)."""
        phase1 = _phase1_section()
        assert (
            "verified_api_docs_url" in phase1
            or "Agent 4" in phase1
            or "BuildReadinessChecklist" in phase1
            or "prefetched" in phase1.lower()
        ), "Phase 1 must ground the builder against Agent 4's output."

    def test_phase1_references_auth_method_field(self):
        phase1 = _phase1_section()
        # auth_method is now a checklist field name — appears throughout
        # the Phase B trigger rules and the spec template
        assert "auth_method" in phase1

    def test_phase1_references_async_pattern_via_checklist(self):
        """Async / webhook / streaming behavior is now expressed via
        the checklist's `async_pattern` field rather than separate
        interaction_model flag names. The Phase B trigger rules name
        async/streaming as triggers for that field."""
        phase1 = _phase1_section()
        assert "async_pattern" in phase1, (
            "Phase 1 must reference the async_pattern checklist field "
            "(replaces the prior interaction_model flag enumeration)."
        )
        # And async/streaming must be named as Phase B triggers
        for trigger in ("async", "streaming"):
            assert trigger in phase1.lower(), (
                f"Phase 1's Phase B trigger rules must name {trigger!r}."
            )

    def test_phase_a_is_before_phase_b(self):
        """Ordering matters — the builder must do Inventory (Step 1)
        BEFORE Gap Analysis (Step 2). Renamed in the Phase D refactor."""
        phase1 = _phase1_section()
        idx_1 = phase1.find("Step 1 — Inventory")
        idx_2 = phase1.find("Step 2 — Gap analysis")
        assert 0 < idx_1 < idx_2

    def test_no_cross_run_memory_references(self):
        """Per user's explicit scope: cross-run memory (memdir /
        provider_atlases) is deferred. Phase 1 teaching must NOT
        reference those — only the current-run ScreenedCandidate."""
        phase1 = _phase1_section()
        # Memdir-style references would signal cross-run memory use
        # We DO allow general mentions of "memory" in colloquial
        # contexts, so check the specific memdir anchors.
        for cross_run_anchor in ("memdir", "provider_atlases",
                                 "cross-run", "previous runs",
                                 "cached from prior"):
            assert cross_run_anchor not in phase1.lower(), (
                f"Phase 1 must not reference cross-run memory "
                f"({cross_run_anchor!r} found) — memdir is deferred."
            )


class TestFix2AdaptiveSearchTeaching:
    """Phase 1 must teach: when Agent 4's checklist has gaps the
    builder needs for THIS test case, trigger targeted research
    (web_search/web_fetch/ask_research). When the checklist already
    answers the test case's needs, skip straight to spec + build.

    Migration note: the prior 'dense vs thin prefetch' density-tier
    distinction was REPLACED by the checklist's per-field status
    (confirmed | inferred | unknown). The five-phase flow encodes the
    same gate behavior: Phase B identifies what's missing, Phase C
    fills it, Phase D writes the spec.
    """

    def test_phase_b_replaces_dense_vs_thin_judgment(self):
        """The 'is the prefetch dense enough?' question is decomposed
        into per-field status checks via the checklist. Step 2 (Gap
        analysis) is where the builder identifies which of the ten
        build-readiness fields are NOT confirmed AND triggered for
        this specific test case."""
        phase1 = _phase1_section()
        idx_2 = phase1.find("Step 2 — Gap analysis")
        idx_3 = phase1.find("Step 3 — Fill the gaps")
        section = phase1[idx_2:idx_3]
        assert "Gap" in section
        # Per-test-case relevance principle preserved.
        assert "test case" in section.lower(), (
            "Step 2 must teach per-test-case relevance."
        )
        # Field-status mechanism preserved (confirmed/inferred/unknown).
        assert (
            "confirmed" in section.lower()
            or "unknown" in section.lower()
            or "inferred" in section.lower()
        ), (
            "Step 2 must reference the checklist's status mechanism "
            "(confirmed/inferred/unknown) for gap identification."
        )

    def test_phase1_defers_completeness_to_checklist_not_density(self):
        """The old 'count 200 substantive lines' manual rule has been
        REPLACED by per-field statuses on the checklist. The model no
        longer counts lines — it reads each field's status."""
        phase1 = _phase1_section()
        assert "200 lines" not in phase1, (
            "The '200 lines' manual threshold has been replaced by the "
            "checklist's per-field status mechanism."
        )
        # And the new mechanism (checklist with confirmed/inferred/unknown)
        # must be referenced
        assert "checklist" in phase1.lower()
        for status in ("confirmed", "inferred", "unknown"):
            assert status in phase1.lower(), (
                f"Phase 1 must reference the {status!r} status "
                f"(replaces the prior density-tier system)."
            )

    def test_phase1_teaches_site_scoped_search_syntax(self):
        # The five-phase prompt no longer has the rigid STEP 2 / STEP 3
        # parallel-fetch routine, but the checklist field reference docs
        # are surfaced via source_url, and the WORKING_EXAMPLE checklist
        # entry still points at site-scoped fallbacks. Verify either
        # the site-scoped pattern remains taught OR the equivalent
        # ask_research / web_search guidance is in place.
        phase1 = _phase1_section()
        assert (
            "site:" in phase1
            or "ask_research" in phase1
            or "web_fetch" in phase1
        ), (
            "Phase 1 must still teach a research mechanism for filling "
            "gaps — site-scoped search, ask_research, or web_fetch."
        )

    def test_phase1_teaches_openapi_hunt(self):
        phase1 = _phase1_section()
        assert "openapi" in phase1.lower() or "swagger" in phase1.lower(), (
            "Phase 1 must teach OpenAPI/Swagger spec hunting (kept from "
            "the prior pass — strictly best research outcome)."
        )

    def test_phase1_teaches_parallel_fetch_in_same_turn(self):
        phase1 = _phase1_section()
        # Parallelism is still encouraged in the WRITE EARLY preamble
        # — multiple web_fetch in ONE response, not iterative click-walking
        assert "PARALLEL" in phase1 or "parallel" in phase1
        assert "same response" in phase1.lower() or "one turn" in phase1.lower() or "ONE turn" in phase1

    def test_phase1_discourages_iterative_research_via_soft_budget(self):
        """The new prompt replaces the rigid 'don't walk one click at a
        time' wording with a soft research budget (2 calls per gap) +
        a behavioral stop test ('can I write the harness without TODO/
        guess/might-need-to'). Both discourage open-ended walking."""
        phase1 = _phase1_section()
        # Soft budget OR hard "TURNS" budget OR stop test — at least one
        # of the three anti-walking mechanisms must be present
        assert (
            "2 calls per gap" in phase1
            or "2 TURNS" in phase1
            or "stop test" in phase1.lower()
            or "without a TODO" in phase1.lower()
            or "without guessing" in phase1.lower()
        ), (
            "Phase 1 must teach an anti-iteration mechanism — soft "
            "research budget, hard turn budget, or behavioral stop test."
        )


class TestFix3CompletenessChecklistExtended:
    """The Phase-1 completion checklist must require ERRORS + SAMPLE_CODE
    sections to be populated before api_spec.txt is written. These are
    the sections that prevent Phase-2 opaque-4xx debug cycles."""

    def _checklist_section(self) -> str:
        phase1 = _phase1_section()
        start = phase1.find("completion checklist")
        end = phase1.find("### Principle: live validation")
        if end == -1:
            end = phase1.find("### DO NOT")
        assert start != -1
        if end == -1:
            return phase1[start:]
        return phase1[start:end]

    def test_checklist_requires_errors_section(self):
        checklist = self._checklist_section()
        assert "ERRORS:" in checklist or "ERRORS section" in checklist
        # Must specify ≥2 error classes
        assert "≥2" in checklist or "at least 2" in checklist.lower() or "2 common error" in checklist.lower()

    def test_checklist_requires_sample_code(self):
        checklist = self._checklist_section()
        # The checklist item is named WORKING_EXAMPLE (language-agnostic,
        # accepts Python/curl/JS/Go/etc). The older PYTHON_EXAMPLES name
        # was too restrictive — many APIs have curl-only or JS-first docs.
        assert "WORKING_EXAMPLE" in checklist, (
            "Checklist must use the language-agnostic WORKING_EXAMPLE "
            "contract, not the Python-only PYTHON_EXAMPLES name."
        )
        # Must require 1 COMPLETE working example (not snippet)
        assert "≥1" in checklist or "at least 1" in checklist.lower() or "COMPLETE" in checklist

    def test_checklist_accepts_any_language_example(self):
        """PYTHON_EXAMPLES is too strict — forces the builder to
        fabricate Python when docs only have curl/JS/Go. The
        WORKING_EXAMPLE contract must explicitly state ANY language
        is acceptable, with a curl→Python translation cheat-sheet so
        non-Python examples are still actionable."""
        checklist = self._checklist_section()
        # Must explicitly say "any language" (or equivalent)
        any_lang = (
            "ANY language" in checklist
            or "any language" in checklist.lower()
        )
        assert any_lang, (
            "Checklist must state WORKING_EXAMPLE accepts ANY language. "
            "Without this, builders fabricate Python from training data "
            "when docs are curl-only."
        )
        # Must enumerate at least 3 acceptable non-Python forms so the
        # rule isn't just hand-waving
        non_python_forms = sum(
            1 for form in ("curl", "JavaScript", "JS", "Node",
                           "Go", "Ruby", "Java", "raw HTTP",
                           "Swagger")
            if form in checklist or form.lower() in checklist.lower()
        )
        assert non_python_forms >= 3, (
            f"Checklist must enumerate ≥3 non-Python example forms "
            f"(found {non_python_forms}) — without concrete "
            f"alternatives the 'any language' rule is abstract."
        )

    def test_checklist_has_completeness_rule_for_gaps(self):
        checklist = self._checklist_section()
        # When ERRORS or WORKING_EXAMPLE is empty, builder must do
        # ONE more targeted search before writing the spec
        assert "Completeness rule" in checklist or "targeted search" in checklist

    def test_checklist_justifies_cost_tradeoff(self):
        """The checklist must explain WHY the gate matters — without
        rationale, the builder treats it as bureaucratic overhead and
        skips it. The rationale: 1 extra research fetch < 3-5 Phase-2
        debug turns."""
        checklist = self._checklist_section()
        assert "Phase 2" in checklist and (
            "debug" in checklist.lower() or "cost" in checklist.lower()
            or "cheaper" in checklist.lower()
        )


class TestApiSpecTemplateHasErrorsSection:
    """The api_spec.txt template that the builder copies from must
    show ERRORS as a first-class section with ≥2 expected entries."""

    def test_template_shows_errors_section(self):
        phase1 = _phase1_section()
        # Template is inside a ``` block — look for ERRORS: as a
        # top-level key
        template_start = phase1.find("API_SPEC_START")
        template_end = phase1.find("API_SPEC_END")
        assert template_start != -1 and template_end != -1
        template = phase1[template_start:template_end]
        assert "ERRORS:" in template, (
            "api_spec.txt template must include ERRORS as a first-class "
            "section so the builder has a slot to populate."
        )

    def test_template_shows_error_examples(self):
        """Template should list common error classes (401/429/etc) so
        the builder knows what's expected."""
        phase1 = _phase1_section()
        template_start = phase1.find("API_SPEC_START")
        template_end = phase1.find("API_SPEC_END")
        template = phase1[template_start:template_end]
        # At least 2 common error classes should be shown
        errors_shown = sum(1 for code in ("401", "403", "429", "500", "4xx", "5xx") if code in template)
        assert errors_shown >= 2, (
            f"Template must show ≥2 example error classes (got "
            f"{errors_shown}) so the builder has concrete targets."
        )


class TestGeneralityAcrossModalitiesAndProviders:
    """The three fixes must be general — no provider hardcoding, no
    modality carve-outs. OCR / voice / code / vision / webhook /
    chat tests all benefit uniformly."""

    def test_no_provider_hardcoding_in_phase1(self):
        phase1 = _phase1_section()
        # Distinguish illustrative examples ("like OpenAI, Stripe,
        # Anthropic" — 3+ names in a list, clearly examples) from
        # provider-specific BRANCHING RULES ("for Twilio, do X"). The
        # former is fine — concrete examples anchor abstract rules.
        # The latter is a bandaid.
        #
        # Heuristic: look for patterns that signal branching — single
        # provider name followed by a verb, "for <Provider>:", etc.
        # These are provider-as-rule-target, not provider-as-example.
        import re
        research_start = phase1.find("atlas-first")
        checklist_end = phase1.find("### Principle: live validation")
        if checklist_end == -1:
            checklist_end = len(phase1)
        research_block = phase1[research_start:checklist_end]

        # Patterns that would indicate provider-specific branching:
        branching_patterns = [
            r"for Twilio[,:\s]",     # "for Twilio, do X"
            r"for Mindee[,:\s]",
            r"for Veryfi[,:\s]",
            r"for Klippa[,:\s]",
            r"if.{0,20}Twilio",       # "if using Twilio"
            r"if.{0,20}Mindee",
            r"Twilio uses",           # "Twilio uses a different pattern"
            r"Mindee uses",
            r"Klippa uses",
            r"Veryfi uses",
        ]
        for pattern in branching_patterns:
            matches = re.findall(pattern, research_block)
            assert not matches, (
                f"Phase 1 has provider-specific branching "
                f"(pattern {pattern!r} matched): research must teach "
                f"general patterns, not per-provider rules."
            )

    def test_illustrative_examples_are_grouped(self):
        """Sanity: when provider names appear as examples, they appear
        in groups (≥2 together), not as standalone special cases.
        'Like X, Y, and Z' = examples; 'X does this differently' =
        bandaid."""
        phase1 = _phase1_section()
        # If "Twilio" appears it should be near another provider name
        # in a "like X, Y, Z" list — but Twilio should ideally not
        # appear at all in Phase 1 teaching. Just spot-check OpenAI
        # (canonical dense-doc example): if present, should be in an
        # enumeration.
        if "OpenAI" in phase1:
            # Check OpenAI appears within 120 chars of another
            # provider name (grouped example)
            idx = phase1.find("OpenAI")
            window = phase1[max(0, idx - 60): idx + 60]
            other_providers = ("Stripe", "Anthropic", "GitHub",
                               "Twilio", "ElevenLabs")
            has_companion = any(p in window for p in other_providers)
            assert has_companion, (
                f"OpenAI appears alone in Phase 1 (context: "
                f"{window!r}) — provider names should appear as "
                f"grouped examples, not standalone rules."
            )

    def test_no_modality_specific_carveouts(self):
        phase1 = _phase1_section()
        phase1_lower = phase1.lower()
        for carveout in (
            "only for voice", "only for ocr", "only for code",
            "only for vision", "only for chat", "only for webhook",
            "except for voice", "except for ocr",
            "skip this for voice", "skip this for ocr",
        ):
            assert carveout not in phase1_lower, (
                f"Phase 1 has modality carve-out {carveout!r} — "
                f"must apply uniformly to all modalities."
            )

    def test_research_pattern_applies_general_statement(self):
        """The Phase 1 research flow must be modality-agnostic — no
        provider-specific carve-outs. After the prompt-refactor, the
        verbose modality enumeration ('voice, OCR, code-gen, webhook...')
        was dropped in favor of principle-based teaching; verify that no
        modality-specific or provider-specific branching has crept in.
        """
        phase1 = _phase1_section()
        # The current Phase 1 should not carry per-modality branches
        # like "if voice modality, do X; if code modality, do Y".
        # We check for absence of the anti-pattern.
        bad_phrases = [
            "if voice",
            "if code modality",
            "if OCR",
            "if webhook",
            "for voice APIs:",
            "for OCR APIs:",
        ]
        for bad in bad_phrases:
            assert bad.lower() not in phase1.lower(), (
                f"Phase 1 must stay modality-agnostic; found provider/"
                f"modality-specific branch: {bad!r}"
            )
        # Per-test-case relevance principle is the modern equivalent
        # of "this pattern is general"; verify it's there.
        assert "test case" in phase1.lower(), (
            "Phase 1 must frame research around per-test-case relevance "
            "(not per-provider type), which IS the generality property."
        )


class TestBudgetAlignsWithParallelFetchPattern:
    """The research budget must accommodate the parallel-fetch pattern.
    The old '3 tool calls' budget conflicts with 'search + 2-3 parallel
    fetches' (4 calls). New budget: 2 research TURNS with parallelism
    inside each turn."""

    def test_budget_is_turn_based_not_call_based(self):
        phase1 = _phase1_section()
        # Must now say "2 turns" or similar turn-based phrasing, not
        # "3 tool calls" which conflicted with parallelism.
        assert "2 TURNS" in phase1 or "2 turns" in phase1.lower(), (
            "Research budget must be turn-based (2 turns) so the "
            "builder can parallel-fetch aggressively within a turn. "
            "Old 3-call limit conflicted with search + 2-3 parallel "
            "fetches = 4 calls."
        )

    def test_budget_preserves_write_early_bias(self):
        phase1 = _phase1_section()
        # WRITE EARLY bias is load-bearing — don't let research
        # perfectionism block progression to Phase 2
        assert "WRITE EARLY" in phase1 or "write_file" in phase1
        assert "commit" in phase1.lower() or "patch" in phase1.lower()
