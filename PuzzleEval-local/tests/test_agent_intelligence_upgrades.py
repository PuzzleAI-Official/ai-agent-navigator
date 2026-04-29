"""Tests for the 5-agent intelligence pass.

One file per agent would scatter these — but they all share one theme:
"the agent uses MORE of the available context than it did before."
Keeping them together makes the intent legible.

These are source-level + structural tests. Full end-to-end verification
requires real API calls and lives in the marker-gated generalizability
bench. No live calls here.
"""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Agent 5 — user_understanding threading + model fallback
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def agent5_source() -> str:
    """Combined source: implement_test_env.py + agent5/api_call.py.

    Phase 4 Path B Step 1 split the API-call boundary into api_call.py;
    agent5_source is the union so existing source-grep tests find string
    literals on either side of the split (e.g.,
    test_agent5_wires_model_fallback's ``call_with_model_fallback`` +
    ``primary_model=...``). Phase 8 candidates: tests using this fixture
    are partially superseded by tests/test_api_call.py (TestSuccessPath
    pins the model_fallback wiring + primary_model arg).
    """
    impl = (
        ROOT / "puzzleeval" / "agents" / "implement_test_env.py"
    ).read_text(encoding="utf-8")
    api = (
        ROOT / "puzzleeval" / "agents" / "agent5" / "api_call.py"
    ).read_text(encoding="utf-8")
    # Phase 6.2: evaluator's adaptive-thinking wiring lives in evaluation.py.
    evaluation = (
        ROOT / "puzzleeval" / "agents" / "agent5" / "evaluation.py"
    ).read_text(encoding="utf-8")
    return (
        impl
        + "\n# === api_call.py boundary ===\n" + api
        + "\n# === evaluation.py boundary ===\n" + evaluation
    )


def test_agent5_reads_domain_from_user_understanding(agent5_source):
    """Before this pass, Agent 5 only read sub_tasks. Now it reads domain
    so it can make domain-aware endpoint choices (healthcare → HIPAA,
    social → doesn't matter)."""
    # The builder initial message now extracts domain from user_understanding
    assert "uo = input_data.user_understanding" in agent5_source
    assert 'getattr(uo, "domain"' in agent5_source


def test_agent5_reads_technical_level(agent5_source):
    """Non-technical users deserve higher-level SDK wrappers; expert users
    can handle raw REST. Agent 5 now knows which one to pick."""
    assert '"technical_level"' in agent5_source or "technical_level" in agent5_source


def test_agent5_reads_monthly_volume_and_bands_it(agent5_source):
    """100 records/mo → atomic endpoint. 100k/mo → batch endpoint. The
    builder prompt now bands monthly_volume into LOW/MODERATE/HIGH/VERY HIGH
    so Claude has explicit guidance for endpoint selection."""
    assert "monthly_volume" in agent5_source
    # Banding labels are load-bearing — teach the model concrete tiers
    # rather than just passing the raw number through.
    assert "LOW" in agent5_source
    assert "HIGH" in agent5_source


def test_agent5_reads_workflow_step_role(agent5_source):
    """Agent 5 now knows which step within the DAG it's building for — so
    a classify-scope candidate is told its role, a merge-scope candidate
    sees its fan-in. Endpoint selection benefits from this context."""
    assert "scope_role_hints" in agent5_source
    assert "side_effects" in agent5_source

# Phase 8: deleted source-grep test `test_agent5_wires_model_fallback`.
# Behavior covered by: tests/test_api_call.py::TestSuccessPath::test_success_called_with_primary_model_and_betas


def test_agent5_builder_prompt_teaches_atomic_vs_batch(agent5_source):
    """The 'first endpoint that works' problem: prompt now explicitly
    teaches batch vs atomic selection based on monthly_volume."""
    assert "atomic" in agent5_source.lower()
    assert "batch" in agent5_source.lower()


def test_agent5_evaluator_uses_adaptive_thinking(agent5_source):
    """Previously the evaluator rubber-stamped with one Claude call.
    Now it uses thinking=adaptive so synonym + partial-match reasoning
    matches the rigor of the builder."""
    # Phase 6.2: function renamed evaluate_with_llm (no leading _);
    # combined source covers both names.
    idx = agent5_source.find("def evaluate_with_llm")
    if idx == -1:
        idx = agent5_source.find("def _evaluate_with_llm")
    assert idx != -1, "evaluate_with_llm function must exist"
    evaluator_block = agent5_source[idx:idx + 5000]
    assert 'thinking' in evaluator_block and 'adaptive' in evaluator_block


# ---------------------------------------------------------------------------
# Agent 3 — test_count_target enforcement + top-up retry
# ---------------------------------------------------------------------------


def test_agent3_has_topup_function():
    """Before: Agent 3 silently accepted shortfall. Now: it detects
    under-generation and fires ONE focused top-up call."""
    source = ((ROOT / "puzzleeval" /"agents" / "agent3" / "core.py").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent3" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
    assert "_topup_undergenerated_subtasks" in source
    assert "floor" in source  # the 70% floor calc


def test_agent3_topup_identifies_missing_dimensions():
    """Top-up should tell the follow-up prompt which dimensions are missing
    so Claude fills the right gaps (not more happy_path tests)."""
    source = ((ROOT / "puzzleeval" /"agents" / "agent3" / "core.py").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent3" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
    # All 6 dimensions named in the set the top-up checks against
    assert "happy_path" in source
    assert "input_variation" in source
    assert "edge_case" in source
    assert "scale" in source
    assert "domain_specific" in source
    assert "error_resilience" in source


def test_agent3_validator_enforces_test_count_target():
    """Historical threshold was hardcoded < 3, ignoring Agent 1's
    test_count_target. Now the validator uses floor(target * 0.7) and
    emits an ERROR (not just warning) when under-generated."""
    source = (ROOT / "puzzleeval" / "validators.py").read_text(encoding="utf-8")
    assert "test_count_target" in source
    assert "target * 0.7" in source or "int(target * 0.7)" in source
    # The under-target case must emit an error, not silent pass
    assert "under-generated" in source.lower() or "Sub-task under-generated" in source


# ---------------------------------------------------------------------------
# Agent 2 — no hallucination quota-fill (atlas cache preload removed)
# ---------------------------------------------------------------------------


def test_agent2_no_hallucination_on_degraded_results():
    """Before: `_salvage_findings_from_tool_uses` fed raw queries to the
    structurer, which fabricated 5-7 candidates with invented docs URLs.
    Now: degraded mode explicitly instructs the structurer to emit
    candidates=[] with a coverage_notes explanation."""
    source = ((ROOT / "puzzleeval" /"agents" / "agent2" / "core.py").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent2" / "templates" / "research_system.md").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent2" / "templates" / "structure_system.md").read_text(encoding="utf-8"))
    assert "DEGRADED MODE" in source
    assert "candidates=[]" in source
    # The instruction MUST forbid fabrication — text is split across string
    # literals in source; just verify both halves are present and close enough.
    assert "MUST NOT" in source
    assert "fabricate" in source
    assert "degraded_no_results" in source


# ---------------------------------------------------------------------------
# Agent 4 — rejection signal preservation + OpenAPI path params
# ---------------------------------------------------------------------------


def test_openapi_harness_substitutes_path_parameters():
    """Before: GET /users/{id} emitted a URL with literal `{id}` → 404.
    Now: _extract_path_params + runtime substitution from input_data."""
    source = (ROOT / "puzzleeval" / "openapi_harness.py").read_text(encoding="utf-8")
    assert "_extract_path_params" in source
    assert "url.replace" in source
    # Error when test case is missing a path param — silent fail is worse
    # than a clear error message.
    assert "Missing path params" in source


def test_openapi_harness_extract_path_params_works():
    """Regression: the actual parser should return the right names."""
    from puzzleeval.openapi_harness import _extract_path_params
    assert _extract_path_params("/users/{id}") == ["id"]
    assert _extract_path_params("/users/{id}/posts/{post_id}") == ["id", "post_id"]
    assert _extract_path_params("/health") == []
    assert _extract_path_params("") == []


# ---------------------------------------------------------------------------
# Agent 1 — modality table coverage is already asserted in
# test_will_it_just_work.py; this file is the agent-intelligence bundle.
# ---------------------------------------------------------------------------
