# ============================================================================
# Tests for Phase 6: inject_user_candidates + apply_scope_picks
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_research_phase6.py -v
#
# Pure-function tests — no mocking, no API key needed. Each test builds its
# own inline fixtures and exercises the two Phase 6 helpers in research.py.
# ============================================================================

import pytest

from puzzleeval.agents.research import apply_scope_picks, inject_user_candidates
from puzzleeval.schemas import Agent2Result, Candidate, UserAddedCandidate


# ============================================================================
# Helpers — small inline builders
# ============================================================================

def _make_candidate(
    name: str = "Mindee",
    provider: str = "Mindee",
    relevance_score: float = 0.75,
    source: str = "https://example.com",
    covers_step_ids: frozenset | None = None,
    coverage_confidence: dict | None = None,
) -> Candidate:
    """Build a minimal valid Candidate with sensible defaults."""
    covers = covers_step_ids if covers_step_ids is not None else sorted({"step_1"})
    confidence = coverage_confidence if coverage_confidence is not None else {
        sid: "claimed" for sid in covers
    }
    return Candidate(
        name=name,
        provider=provider,
        description=f"{name} — test candidate",
        api_available=True,
        api_docs_url=None,
        pricing_model="usage-based",
        pricing_details=None,
        claimed_capabilities=["ocr"],
        relevance_score=relevance_score,
        adoption_difficulty="easy",
        relevant_subtasks=[],
        source=source,
        covers_step_ids=covers,
        coverage_confidence=confidence,
    )


def _make_agent2_result(candidates: list[Candidate] | None = None) -> Agent2Result:
    """Build a minimal Agent2Result, optionally with provided candidates."""
    return Agent2Result(
        candidates=candidates or [],
        search_approach="test search",
        coverage_notes="test notes",
        cost_usd=0.0,
    )


# ============================================================================
# inject_user_candidates — 4 cases
# ============================================================================

class TestInjectUserCandidates:
    """Pure-function tests for inject_user_candidates."""

    def test_merge_into_empty_pool(self):
        """User-added candidates merge into an empty Agent2Result."""
        result = _make_agent2_result(candidates=[])
        user_adds = [
            UserAddedCandidate(
                name="CustomAPI",
                provider="CustomCo",
                covers_step_ids=["step_1", "step_2"],
            ),
        ]

        updated = inject_user_candidates(result, user_adds)

        assert len(updated.candidates) == 1
        c = updated.candidates[0]
        assert c.name == "CustomAPI"
        assert c.provider == "CustomCo"

    def test_dedup_case_insensitive(self):
        """Adding 'Mindee' when 'mindee' already exists is silently skipped."""
        existing = _make_candidate(name="mindee", provider="Mindee SAS")
        result = _make_agent2_result(candidates=[existing])
        user_adds = [
            UserAddedCandidate(
                name="Mindee",
                provider="Mindee SAS",
                covers_step_ids=["step_1"],
            ),
        ]

        updated = inject_user_candidates(result, user_adds)

        # Pool unchanged — the duplicate was skipped.
        assert len(updated.candidates) == 1
        assert updated.candidates[0].name == "mindee"

    def test_user_added_gets_source_and_score(self):
        """Every user-added candidate gets source='user_provided' and relevance_score=0.99."""
        result = _make_agent2_result(candidates=[])
        user_adds = [
            UserAddedCandidate(
                name="MyOCR",
                provider="MyOCRCo",
                covers_step_ids=["step_1"],
            ),
        ]

        updated = inject_user_candidates(result, user_adds)

        c = updated.candidates[0]
        assert c.source == "user_provided"
        assert c.relevance_score == 0.99

    def test_explicit_covers_step_ids_flow_through(self):
        """User-added with explicit covers_step_ids propagates correctly."""
        result = _make_agent2_result(candidates=[])
        user_adds = [
            UserAddedCandidate(
                name="AcmeDoc",
                provider="Acme",
                covers_step_ids=["step_1", "step_2", "step_3"],
            ),
        ]

        updated = inject_user_candidates(result, user_adds)

        c = updated.candidates[0]
        assert c.covers_step_ids == sorted({"step_1", "step_2", "step_3"})
        assert set(c.coverage_confidence.keys()) == {"step_1", "step_2", "step_3"}
        assert all(v == "claimed" for v in c.coverage_confidence.values())


# ============================================================================
# apply_scope_picks — 5 cases
# ============================================================================

class TestApplyScopePicks:
    """Pure-function tests for apply_scope_picks."""

    def test_none_scope_picks_passthrough(self):
        """scope_picks=None means pass-through — all candidates survive."""
        c1 = _make_candidate(name="Alpha", provider="A")
        c2 = _make_candidate(name="Beta", provider="B")
        result = _make_agent2_result(candidates=[c1, c2])

        updated = apply_scope_picks(result, scope_picks=None)

        assert len(updated.candidates) == 2
        names = {c.name for c in updated.candidates}
        assert names == {"Alpha", "Beta"}

    def test_valid_picks_filter_unselected(self):
        """Only candidates picked at at least one scope survive."""
        c1 = _make_candidate(name="Alpha", provider="A")
        c2 = _make_candidate(name="Beta", provider="B")
        c3 = _make_candidate(name="Gamma", provider="C")
        result = _make_agent2_result(candidates=[c1, c2, c3])

        scope_picks = {"step_1": ["Alpha", "Gamma"]}
        updated = apply_scope_picks(result, scope_picks=scope_picks)

        names = {c.name for c in updated.candidates}
        assert names == {"Alpha", "Gamma"}
        assert len(updated.candidates) == 2

    def test_covers_step_ids_reduced_to_picked_scopes(self):
        """Candidate picked at 2 of 3 scopes has covers_step_ids reduced to those 2."""
        c = _make_candidate(
            name="AllInOne",
            provider="X",
            covers_step_ids=sorted({"step_1", "step_2", "step_3"}),
            coverage_confidence={"step_1": "claimed", "step_2": "claimed", "step_3": "claimed"},
        )
        result = _make_agent2_result(candidates=[c])

        # User picks AllInOne at step_1 and step_3 only, not step_2.
        scope_picks = {
            "step_1": ["AllInOne"],
            "step_3": ["AllInOne"],
        }
        updated = apply_scope_picks(result, scope_picks=scope_picks)

        assert len(updated.candidates) == 1
        kept = updated.candidates[0]
        assert kept.covers_step_ids == sorted({"step_1", "step_3"})
        assert set(kept.coverage_confidence.keys()) == {"step_1", "step_3"}

    def test_empty_scope_picks_zero_candidates(self):
        """scope_picks={} (empty dict) filters out everything."""
        c1 = _make_candidate(name="Alpha", provider="A")
        result = _make_agent2_result(candidates=[c1])

        updated = apply_scope_picks(result, scope_picks={})

        assert len(updated.candidates) == 0

    def test_user_added_injected_alongside_filtered(self):
        """User-added candidates appear alongside scope-filtered candidates."""
        c1 = _make_candidate(name="Alpha", provider="A")
        c2 = _make_candidate(name="Beta", provider="B")
        result = _make_agent2_result(candidates=[c1, c2])

        scope_picks = {"step_1": ["Alpha"]}
        user_added = [
            UserAddedCandidate(
                name="CustomAPI",
                provider="CustomCo",
                covers_step_ids=["step_1", "step_2"],
            ),
        ]

        updated = apply_scope_picks(result, scope_picks=scope_picks, user_added=user_added)

        names = {c.name for c in updated.candidates}
        assert names == {"Alpha", "CustomAPI"}
        assert len(updated.candidates) == 2

        # Verify the user-added candidate has the expected properties.
        custom = [c for c in updated.candidates if c.name == "CustomAPI"][0]
        assert custom.source == "user_provided"
        assert custom.relevance_score == 0.99
