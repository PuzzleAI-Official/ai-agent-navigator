"""[DEPRECATED] Density-aware prefetch injection has been removed.

The density-tier system (HIGH / MEDIUM / THIN with inline-vs-listed
branching, "skip to STEP 2" gates, "you do NOT need to read_file"
language) was deleted in the build-readiness-checklist pass. The
checklist (Agent 4 → Agent 5 handoff artifact) directly encodes "what
the harness builder needs" via per-field source URLs, replacing the
proxy of "structural richness."

Migration:
  - File ranking (the one bit of the density system that remained
    useful) is now done by `_usefulness_signal` in
    `puzzleeval/agents/implement_test_env.py` — same math, no tiers,
    no gating, just an inventory ordering hint.
  - `puzzleeval.web_doc_cache.doc_density_score` /
    `classify_doc_density` / `DENSITY_HIGH` / `DENSITY_MEDIUM` are
    DELETED. Locked by `tests/test_agent5_research_agency.py::
    TestDensityHelpersDeleted`.
  - The Phase 1 prompt's "skip to STEP 2" / "you do NOT need to" gate
    language is DELETED. Locked by `tests/test_agent5_research_agency.py::
    TestDensityGateLanguageRemoved`.
  - Ranking-as-order behavior (densest-looking files first in the
    inventory) is locked by `tests/test_agent5_research_agency.py::
    TestPrefetchedDocsBlockIsRankingOnly`.
  - The usefulness-signal heuristic itself is locked by
    `tests/test_agent5_research_agency.py::TestUsefulnessSignal`.

This file remains as a marker so test-discovery doesn't surface a
stale test name. All cases live in `test_agent5_research_agency.py`.

Reference: PLAN_AGENT5_RESEARCH_AGENCY.md.
"""
from __future__ import annotations


def test_density_system_removed_pointer_to_replacement():
    """Marker test: confirms the density-removal migration has landed.

    The actual coverage moved to test_agent5_research_agency.py. This
    test exists so anyone running `pytest tests/test_density_*` doesn't
    see "0 tests collected" and assume something broke.
    """
    # The replacements are independently locked; this test asserts only
    # that the pointer exists.
    from pathlib import Path
    here = Path(__file__).parent
    successor = here / "test_agent5_research_agency.py"
    assert successor.exists(), (
        "Successor test file `test_agent5_research_agency.py` is missing — "
        "density tests were retired in favor of checklist-driven coverage; "
        "the new file should exist."
    )
