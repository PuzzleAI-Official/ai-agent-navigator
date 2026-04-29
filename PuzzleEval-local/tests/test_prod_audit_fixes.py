"""Regression guards for the four-subagent pre-production audit.

Each section covers one audit finding; these tests lock in the fix so a
future refactor can't silently re-introduce the bug.

Organized by audit agent's report:
  1. Backend + API wiring (pipeline_runner.py)
  2. Prompts / schemas (validators, Agent 3/3F, Agent 5 builder)
  3. E2E trace / tool_runner integrity (plugin_tool_runner.py)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _builder_prompt_text() -> str:
    from puzzleeval.agents.implement_test_env import BUILDER_SYSTEM_PROMPT

    return BUILDER_SYSTEM_PROMPT


def _agent5_combined_source() -> str:
    """Combined source: implement_test_env.py + agent5/api_call.py.

    Phase 4 Path B Step 1 split the API-call boundary (retry, PTL recovery,
    rate-limit backoff, context_management edits, system prompt cache
    block) into ``puzzleeval.agents.agent5.api_call``. Legacy source-grep
    tests need to see the union of both files to find string literals
    that moved across the boundary.

    Phase 8 candidate: tests using this helper are SUPERSEDED by
    behavior tests in ``tests/test_build_loop_behavior.py`` (TestAPICallRetryBehavior,
    TestRateLimitRetryBehavior) + helper tests in ``tests/test_api_call.py``
    (TestIsPtlError, TestContextManagementEdits, TestMessageCacheBreakpoint,
    TestBuilderBetas). When Phase 8 cleanup runs, delete the source-grep
    tests that have a behavior-test replacement.
    """
    impl_path = ROOT / "puzzleeval" / "agents" / "implement_test_env.py"
    agent5 = ROOT / "puzzleeval" / "agents" / "agent5"
    return (
        impl_path.read_text(encoding="utf-8")
        + "\n# === api_call.py boundary ===\n"
        + (agent5 / "api_call.py").read_text(encoding="utf-8")
        + "\n# === dispatch_helpers.py boundary ===\n"
        + (agent5 / "dispatch_helpers.py").read_text(encoding="utf-8")
        + "\n# === build_loop.py boundary ===\n"
        + (agent5 / "build_loop.py").read_text(encoding="utf-8")
        + "\n# === execution.py boundary ===\n"
        + (agent5 / "execution.py").read_text(encoding="utf-8")
        + "\n# === evaluation.py boundary ===\n"
        + (agent5 / "evaluation.py").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 1. Backend + API wiring
# ---------------------------------------------------------------------------


def test_pipeline_runner_gather_has_return_exceptions():
    """Backend CRITICAL #1 — parallel branches must not cancel each other
    when one raises. Without return_exceptions=True, an Agent 3 hiccup
    tears down a running Agent 4."""
    src = (
        ROOT.parent
        / "puzzleeval-api"
        / "services"
        / "pipeline_runner.py"
    ).read_text(encoding="utf-8")
    assert "return_exceptions=True" in src, (
        "pipeline_runner.py's asyncio.gather MUST use return_exceptions=True "
        "so a failure in one branch doesn't cancel the other mid-flight."
    )


def test_pipeline_runner_selection_cancel_emits_correct_status():
    """Backend CRITICAL #2 — cancel during the selection pause must
    identify itself as 'during_selection', not the previous misleading
    'after_screening' label."""
    src = (
        ROOT.parent
        / "puzzleeval-api"
        / "services"
        / "pipeline_runner.py"
    ).read_text(encoding="utf-8")
    assert '"during_selection"' in src, (
        "Selection-phase cancel must emit cancelled_at='during_selection'."
    )


def test_pipeline_runner_no_cost_double_count():
    """Backend HIGH — final EvaluationReport must use state.total_cost_usd
    alone (already includes all agent costs). Previously was
    state.total_cost_usd + total_cost which double-counted the whole run."""
    src = (
        ROOT.parent
        / "puzzleeval-api"
        / "services"
        / "pipeline_runner.py"
    ).read_text(encoding="utf-8")
    # The CALL site — not the explanatory comment block.
    assert "total_cost_usd=state.total_cost_usd + total_cost" not in src, (
        "Cost double-count regressed. assemble_report(total_cost_usd=...) "
        "must pass state.total_cost_usd directly (already includes every "
        "agent's cost via _record_agent_cost_and_emit)."
    )
    assert "total_cost_usd=state.total_cost_usd," in src, (
        "Expected assemble_report to be called with total_cost_usd=state.total_cost_usd."
    )


def test_files_upload_sanitizes_filename():
    """Backend HIGH — ../../etc/passwd-style traversal must be rejected
    OR sanitized to a safe basename. Upload route must not allow
    writing outside run_upload_dir."""
    src = (
        ROOT.parent
        / "puzzleeval-api"
        / "routes"
        / "files.py"
    ).read_text(encoding="utf-8")
    # Must call `.name` or an equivalent basename helper.
    assert "PurePosixPath" in src or "PureWindowsPath" in src or ".name" in src
    # Must include a containment check.
    assert "relative_to" in src or "run_upload_dir" in src
    # Must have the "invalid filename" 400 error response.
    assert "invalid filename" in src


def test_runs_audio_endpoint_exists():
    """Frontend depends on GET /runs/audio?path=... to play back voice
    artifacts. Endpoint must exist with containment check."""
    src = (
        ROOT.parent
        / "puzzleeval-api"
        / "routes"
        / "runs.py"
    ).read_text(encoding="utf-8")
    assert '@router.get("/runs/audio")' in src or "/runs/audio" in src
    assert "FileResponse" in src
    # Containment check.
    assert "relative_to" in src


# ---------------------------------------------------------------------------
# 2. Prompts / schemas
# ---------------------------------------------------------------------------


def test_agent3_input_type_enum_list_is_complete():
    """Agent 3's top-level input_type enum list must match VALID_INPUT_TYPES
    — stale listing of only 5 values was leading Claude astray."""
    from puzzleeval.validators import VALID_INPUT_TYPES
    src = ((ROOT / "puzzleeval" /"agents" / "agent3" / "core.py").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent3" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
    # Slice out the input_type Values section for precise checking.
    start = src.index("## input_type Values")
    end = src.index("## output_type Values")
    section = src[start:end]
    # Every VALID_INPUT_TYPES value must be named somewhere in that section.
    for enum_value in VALID_INPUT_TYPES:
        assert f'"{enum_value}"' in section, (
            f"Agent 3 input_type section missing '{enum_value}' — drift with "
            f"VALID_INPUT_TYPES will mis-guide test case generation."
        )


def test_agent3_output_type_enum_list_is_complete():
    """Agent 3's top-level output_type enum list must match VALID_OUTPUT_TYPES."""
    from puzzleeval.validators import VALID_OUTPUT_TYPES
    src = ((ROOT / "puzzleeval" /"agents" / "agent3" / "core.py").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent3" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
    start = src.index("## output_type Values")
    end = src.index("## Architecture Alignment")
    section = src[start:end]
    for enum_value in VALID_OUTPUT_TYPES:
        assert f'"{enum_value}"' in section, (
            f"Agent 3 output_type section missing '{enum_value}' — drift with "
            f"VALID_OUTPUT_TYPES will mis-guide test case generation."
        )


def test_workflow_step_output_format_description_lists_all_enums():
    """WorkflowStep.output_format field description must mention every
    VALID_OUTPUT_TYPES value so anyone reading schemas.py gets the full enum."""
    from puzzleeval.validators import VALID_OUTPUT_TYPES
    src = (ROOT / "puzzleeval" / "schemas.py").read_text(encoding="utf-8")
    # Isolate the WorkflowStep.output_format field block.
    idx = src.find("    output_format: str = Field(")
    assert idx >= 0
    block_end = src.find("    )", idx) + len("    )")
    block = src[idx:block_end]
    for enum_value in VALID_OUTPUT_TYPES:
        assert f"'{enum_value}'" in block or f'"{enum_value}"' in block, (
            f"WorkflowStep.output_format description missing '{enum_value}'"
        )


def test_agent5_builder_teaches_multi_turn_harness_payload():
    """C3 — Agent 5 builder prompt must describe the {audio_url, turn_index,
    session_state} payload shape so builders don't write single-turn
    harnesses that KeyError on voice_conversation dispatches."""
    src = _agent5_combined_source()
    assert "MULTI-CALL HARNESS CONTRACT" in src
    assert "turn_index" in src
    assert "session_state" in src
    assert "audio_url" in src


def test_agent5_builder_emits_wss_advisory_section():
    """C6 — when atlas or docs_url signal a WebSocket endpoint, the
    builder prompt must carry an advisory telling the builder to NOT
    silently build a REST facsimile."""
    src = _agent5_combined_source()
    assert "WebSocket/Realtime endpoint detected" in src
    assert "websocket_not_supported" in src
    assert "REST facsimile" in src


# ---------------------------------------------------------------------------
# 3. E2E trace / tool_runner integrity
# ---------------------------------------------------------------------------


def test_beta_header_is_the_correct_value():
    """C1 — the most critical bug from the audit. Wrong header kills the
    entire NEW-AA tool_runner path. Must be exactly code-execution-2025-08-25
    per Anthropic docs for the code_execution_20260120 tool."""
    src = (ROOT / "puzzleeval" / "plugin_tool_runner.py").read_text(encoding="utf-8")
    # Correct literal must be present.
    assert '"code-execution-2025-08-25"' in src
    # Old wrong value must be gone.
    assert '"code-execution-2026-01-20"' not in src, (
        "Wrong beta header regressed — every tool_runner call will 400."
    )


def test_tool_runner_accumulates_eval_cost():
    """C4 — ToolRunnerVerdict.cost_usd must accumulate into eval_cost
    so the report's total_test_cost_usd isn't under-counted when the
    tool_runner path runs."""
    src = _agent5_combined_source()
    # Look for the accumulator pattern.
    assert "eval_cost += float(verdict.cost_usd" in src, (
        "ToolRunnerVerdict.cost_usd must be added to eval_cost. "
        "Otherwise tool_runner's Claude cost vanishes from the report."
    )
    # Late nonlocal binding so the closure mutates the outer var.
    assert "nonlocal eval_cost" in src


def test_tool_runner_harness_runner_conditional():
    """H3 — harness_runner must only be injected for multi-call modalities.
    Previously injected for every test, inviting Claude to mis-pick
    conversation_simulator for simple text evals.

    After voice_debug_3, the one-liner ``runner_for_this = _runner if
    needs_runner else None`` was replaced by a full ``if needs_runner:``
    block that builds a per-test-case runner threading the test case's
    ``input_context`` through to the harness. Gate invariants:
    ``needs_runner`` still decides runner injection, and
    ``runner_for_this = None`` remains the non-multi-call branch
    (initialized before the if, ensuring a clean default)."""
    src = _agent5_combined_source()
    # The gate must still exist.
    assert "needs_runner" in src
    # The None default must be preserved.
    assert "runner_for_this = None" in src
    # Multi-call path must construct a runner.
    assert "if needs_runner:" in src


def test_evaluate_with_tool_runner_narrow_widening():
    """plugin_tool_runner.py — runner-driven widening must be gated on
    multi-call modality, not every call."""
    src = (ROOT / "puzzleeval" / "plugin_tool_runner.py").read_text(encoding="utf-8")
    assert "is_multi_call_test" in src
    assert "multi_call_modalities" in src


def test_eligible_plugins_simplified_semantics():
    """plugin_tool_runner.py — the AND-then-OR contradiction is gone.
    Either-side-matches is the documented semantics now."""
    from puzzleeval.plugin_tool_runner import eligible_plugins
    # conversation_simulator claims input_types=["conversation"] and
    # output_types=["free_text", "structured_json"]. A test with both set
    # to matching values should include it.
    out = eligible_plugins(input_type="conversation", output_type="free_text")
    names = {p.name for p in out}
    assert "conversation_simulator" in names


def test_ll_judge_eval_cost_uses_accumulation_not_assignment():
    """Regression: previously `eval_cost = _evaluate_with_llm(...)` wiped
    out tool_runner's accumulated cost. Must use += instead."""
    src = _agent5_combined_source()
    # The pattern must be: destructure to a separate name, then accumulate.
    assert "_llm_cost = _evaluate_with_llm(" in src
    assert "eval_cost += float(_llm_cost" in src


# ---------------------------------------------------------------------------
# 4. EvaluationReport.scope_runs — Phase 9 now surfaces to frontend
# ---------------------------------------------------------------------------


def test_evaluation_report_has_scope_runs_field():
    """Phase 9 per-scope breakdown must surface in the final report so
    the frontend's ResultsComparison can render per-scope tables."""
    from puzzleeval.report import EvaluationReport
    import dataclasses
    fields = {f.name for f in dataclasses.fields(EvaluationReport)}
    assert "scope_runs" in fields


def test_agent5_system_prompt_has_cache_control():
    """D1 — Agent 5's 10.7K-token system prompt must have cache_control
    ephemeral so it caches across the 6-24 turns per candidate. Without
    this, every turn re-pays the full system-prompt input cost.

    Phase 8 candidate: after Phase 4 Path B Step 1 the system block with
    cache_control lives in ``api_call.make_builder_api_call`` while the
    ``_render_builder_prompt`` call lives in
    ``implement_test_env._build_single_harness`` (caller pre-renders,
    helper applies cache_control wrapper). Asserts now check both halves
    independently: the render call site (impl) and the cache_control
    wrapper around system text (api_call).
    """
    src = _agent5_combined_source()
    # 1. The cache_control ephemeral wrapper must exist (in api_call.py).
    assert '"cache_control": {"type": "ephemeral"}' in src
    # 2. The render call site must exist (in implement_test_env.py).
    import re
    m = re.search(
        r"_render_builder_prompt\(\s*BUILDER_SYSTEM_PROMPT",
        src,
    )
    assert m is not None, (
        "BUILDER prompt call site must pass BUILDER_SYSTEM_PROMPT into "
        "_render_builder_prompt so __OS_TYPE__ + __OS_SPECIFIC_RULES__ + "
        "__MODALITY_CONTRACT__ placeholders get filled."
    )
    # 3. The system block in api_call.py wraps the rendered text with
    # cache_control. Anchor on ctx.system_text (the helper's wired param).
    idx_sys = src.find("ctx.system_text")
    assert idx_sys > 0, (
        "make_builder_api_call must pass ctx.system_text into the "
        "system block (so caller-rendered text is what gets cached)."
    )
    window = src[max(0, idx_sys - 200):idx_sys + 200]
    assert '"cache_control"' in window, (
        "Agent 5 BUILDER system block in api_call.py missing "
        "cache_control ephemeral on the system text."
    )

# Phase 8: deleted source-grep test `test_agent5_context_management_shape_matches_anthropic_schema`.
# Behavior covered by: tests/test_api_call.py::TestContextManagementEdits


def test_agent5_ptl_detection_strict():
    """Regression: PTL recovery used to match any 400 containing the word
    "context" — which false-positived on an unrelated
    `context_management.edits.0...` schema error. Each false-positive
    retried instantly, hit the same 400, halved max_tokens, and blew
    through the retry budget in microseconds. Detection must be keyed to
    real prompt-too-long markers only.
    """
    src = _agent5_combined_source()
    # Loose markers that caused the false-positive must be gone from the
    # detection expression.
    bad_expr = '"context" in error_msg'
    assert bad_expr not in src, (
        "PTL detector still matches any error containing 'context' — "
        "this false-positives on schema errors like "
        "'context_management.edits.0.<edit>.trigger'."
    )
    # At least one of the canonical PTL markers must be present.
    assert (
        '"prompt is too long"' in src
        or '"prompt too long"' in src
        or '"maximum context length"' in src
    ), "PTL detector lost its real markers — it will never trigger."


def test_agent2_structure_prompt_no_duplicate_candidate_class_block():
    """D3 — the candidate-class principle was duplicated verbatim across
    RESEARCH + STRUCTURE prompts. Keep it in RESEARCH only; STRUCTURE
    gets a one-liner pointer. ~1800 token saving per Agent 2 run."""
    from puzzleeval.agents.research import STRUCTURE_SYSTEM_PROMPT
    # The RESEARCH prompt's "Examples of the duality" marker should NOT
    # appear in STRUCTURE anymore — it was the biggest duplicated block.
    assert "Examples of the duality" not in STRUCTURE_SYSTEM_PROMPT
    # But the one-line preservation rule IS still there.
    assert "Developer-primitive" in STRUCTURE_SYSTEM_PROMPT or (
        "Developer API that" in STRUCTURE_SYSTEM_PROMPT
    )


def test_agent5_has_turn_budget_nudge():
    """A11 — when builder approaches MAX_TURNS without completing, inject
    a nudge so Claude commits to HARNESS_COMPLETE or HARNESS_FAILED
    instead of starting a new refactor."""
    src = _agent5_combined_source()
    assert "TURN BUDGET ADVISORY" in src
    assert "turn_budget_nudge_sent" in src


def test_candidate_interaction_pattern_hint_has_websocket_and_sse():
    """C4 — Agent 2's hint enum must include 'websocket' and 'sse_streaming'
    so voice/realtime candidates get a pre-signal before Agent 4 deep-verify."""
    from puzzleeval.schemas import Candidate
    field = Candidate.model_fields["api_interaction_pattern_hint"]
    desc = (field.description or "").lower()
    assert "'websocket'" in desc
    assert "'sse_streaming'" in desc


def test_agent5_verify_contradiction_resolved():
    """The 'DO NOT write verification scripts' rule must explicitly
    clarify it's about Phase 1 ad-hoc scripts — NOT the Phase 2
    <verify_against_docs> eyeball check. Audit flagged this as a contradiction."""
    src = _builder_prompt_text()
    # Must mention Phase 1 context explicitly.
    assert "ad-hoc verification scripts" in src or "Phase 1" in src
    # And verify_against_docs must still be the Phase 2 step (not deleted).
    assert "<verify_against_docs>" in src


def test_agent5_phase1_is_research_from_scratch():
    """Agent 5's Phase 1 prompt must do its own research via web_search +
    web_fetch. Agent 4 no longer produces an atlas; the builder is the
    sole researcher.
    """
    src = _builder_prompt_text()
    # Phase 1 header must be RESEARCH, not ATLAS INGEST.
    assert "PHASE 1: RESEARCH" in src
    # Must explicitly use server-side web tools.
    assert "web_search" in src
    assert "web_fetch" in src
    # Must NOT reference the removed atlas mechanism.
    assert "ATLAS INGEST" not in src
    assert "PRE-EXTRACTED ATLAS" not in src


def test_server_tool_timeout_tier_exists():
    """Capability fix: server-side-tool-loop agents (Agent 2 research,
    Agent 4 deep-verify, Agent 5 builder) need a longer timeout than
    simple structured-output calls. Default 120 s timed out mid-research
    on real runs. SERVER_TOOL_TIMEOUT_S must be exported and >= 300 s."""
    from puzzleeval.anthropic_client import (
        DEFAULT_TIMEOUT_S, SERVER_TOOL_TIMEOUT_S,
    )
    assert SERVER_TOOL_TIMEOUT_S >= 300.0, (
        "SERVER_TOOL_TIMEOUT_S must be >= 300s to accommodate multi-search "
        "+ multi-turn server-tool loops"
    )
    assert SERVER_TOOL_TIMEOUT_S > DEFAULT_TIMEOUT_S


def test_agent2_uses_server_tool_timeout():
    """Source-grep: Agent 2's research client must use SERVER_TOOL_TIMEOUT_S.
    Anchor the fix so future refactors can't silently regress it."""
    src = (ROOT / "puzzleeval" / "agents" / "agent2" / "core.py").read_text(
        encoding="utf-8"
    )
    assert "SERVER_TOOL_TIMEOUT_S" in src


def test_agent4_screening_uses_server_tool_timeout():
    """Agent 4 deep-verify does up to 15 turns × web_fetch/web_search.
    Needs the longer timeout."""
    src = (ROOT / "puzzleeval" / "agents" / "agent4" / "core.py").read_text(
        encoding="utf-8"
    )
    assert "SERVER_TOOL_TIMEOUT_S" in src


def test_agent5_uses_server_tool_timeout_constant():
    """Agent 5 previously hardcoded 240s. Now references the constant
    for consistency + single tuning point."""
    src = _agent5_combined_source()
    # The constant reference must be present (not the old literal 240)
    assert "SERVER_TOOL_TIMEOUT_S" in src
    # Defensive: old hardcoded 240 for timeout should be gone from client build
    assert "timeout=240" not in src


def test_agent1_input_has_proceed_with_partial_info_field():
    """--no-interactive must be plumbed through to Agent 1 so it can
    produce a complete result with default values for optional fields
    when critical info is present. Without this field, --no-interactive
    just hangs the pipeline at turn 1."""
    from puzzleeval.schemas import Agent1Input
    fields = Agent1Input.model_fields
    assert "proceed_with_partial_info" in fields, (
        "Agent1Input missing proceed_with_partial_info field — "
        "--no-interactive flag cannot reach Agent 1"
    )
    # Default must be False to preserve multi-turn behavior for chat flows.
    default = fields["proceed_with_partial_info"].default
    assert default is False, f"default should be False, got {default!r}"


def test_agent1_prompt_respects_proceed_with_partial_info():
    """Source-grep: Agent 1's prompt assembly must inject the
    OPERATOR DIRECTIVE when the flag is True so Claude knows to
    populate a full result instead of asking follow-ups."""
    src = (ROOT / "puzzleeval" / "agents" / "agent1" / "core.py").read_text(
        encoding="utf-8"
    )
    assert "proceed_with_partial_info" in src
    assert "OPERATOR DIRECTIVE" in src


def test_cli_passes_no_interactive_to_agent1():
    """The --no-interactive CLI flag must set proceed_with_partial_info
    on the Agent1Input it constructs. Without this, the flag only
    disabled loop follow-up prompts but Agent 1 was still producing
    is_clear=False and stopping the pipeline."""
    src = (ROOT / "puzzleeval" / "cli.py").read_text(encoding="utf-8")
    assert "proceed_with_partial_info=args.no_interactive" in src


def test_provider_registry_has_openai_and_elevenlabs():
    """User's voice scenario needs OpenAI + ElevenLabs available via the
    registry. Substring match must resolve both candidate classes."""
    from puzzleeval.provider_registry import load_registry, get_credentials
    reg = load_registry()
    assert "openai" in reg.providers, (
        "openai entry missing from provider_registry.json"
    )
    assert "elevenlabs" in reg.providers, (
        "elevenlabs entry missing from provider_registry.json"
    )
    # Substring match for representative candidate names
    openai_creds = get_credentials(reg, "OpenAI", "OpenAI Realtime")
    assert openai_creds and "OPENAI_API_KEY" in openai_creds
    elab_creds = get_credentials(reg, "ElevenLabs", "ElevenLabs Conversational AI")
    assert elab_creds and "ELEVENLABS_API_KEY" in elab_creds


def test_provider_registry_sync_to_environ_fills_missing_keys():
    """The registry → os.environ propagation fills empty slots without
    overriding existing values. Core capability: registry becomes a
    first-class credential source on par with .env."""
    import os
    from puzzleeval.provider_registry import (
        ProviderRegistry, ProviderEntry, sync_to_environ,
    )
    reg = ProviderRegistry({
        "foo": ProviderEntry(name="foo", env_vars={
            "SYNC_TEST_KEY_A": "from_registry",
            "SYNC_TEST_KEY_B": "from_registry",
        }),
    })
    # Pre-set one key; empty-string the other (common CI footgun).
    os.environ["SYNC_TEST_KEY_A"] = "from_shell"  # explicit WIN
    os.environ["SYNC_TEST_KEY_B"] = ""            # empty → evicted
    try:
        applied = sync_to_environ(reg)
        assert os.environ["SYNC_TEST_KEY_A"] == "from_shell"  # preserved
        assert os.environ["SYNC_TEST_KEY_B"] == "from_registry"  # filled
        assert "SYNC_TEST_KEY_B" in applied
        assert "SYNC_TEST_KEY_A" not in applied  # not written — already set
    finally:
        os.environ.pop("SYNC_TEST_KEY_A", None)
        os.environ.pop("SYNC_TEST_KEY_B", None)


def test_provider_registry_sync_to_environ_override_mode():
    """override=True mode is reserved for tests that need to forcibly
    swap credentials mid-run. Normal imports use override=False."""
    import os
    from puzzleeval.provider_registry import (
        ProviderRegistry, ProviderEntry, sync_to_environ,
    )
    reg = ProviderRegistry({
        "foo": ProviderEntry(name="foo", env_vars={
            "SYNC_OVERRIDE_KEY": "from_registry",
        }),
    })
    os.environ["SYNC_OVERRIDE_KEY"] = "from_shell"
    try:
        sync_to_environ(reg, override=True)
        assert os.environ["SYNC_OVERRIDE_KEY"] == "from_registry"
    finally:
        os.environ.pop("SYNC_OVERRIDE_KEY", None)


def test_package_autoload_propagates_registry_to_environ():
    """puzzleeval package-level autoload of registry must run at import
    time. After `import puzzleeval`, os.environ contains keys from
    provider_registry.json (not just .env)."""
    import importlib, os
    import puzzleeval
    importlib.reload(puzzleeval)
    # Any key from registry we added should now be in os.environ.
    # MINDEE_API_KEY is present in the committed registry.
    assert os.environ.get("MINDEE_API_KEY"), (
        "MINDEE_API_KEY not in os.environ after package import — "
        "registry autoload failed"
    )


def test_tts_plugin_has_failover_chain():
    """When the primary TTS provider fails (401, 429, rate limit), the
    plugin must fall over to the next credentialed provider instead of
    collapsing to the text-placeholder fallback. This is the real-world
    save for expired ElevenLabs keys during an OpenAI+ElevenLabs voice run."""
    import os
    from unittest.mock import patch
    # Set BOTH provider keys so there are two providers to iterate.
    os.environ["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "dummy-oa")
    os.environ["ELEVENLABS_API_KEY"] = os.environ.get("ELEVENLABS_API_KEY", "dummy-11")
    os.environ["PUZZLEEVAL_TTS_PROVIDER"] = "elevenlabs"  # request elevenlabs first

    from puzzleeval.tool_plugins import tts as tts_mod
    # Force elevenlabs to raise, openai to succeed. If failover works,
    # the result's voice_provider should be openai_tts.
    original_dispatch = dict(tts_mod._PROVIDER_DISPATCH)

    def _fail(text, key, **kw):
        raise RuntimeError("401 Unauthorized")

    def _succeed(text, key, voice="alloy", **kw):
        return b"FAKE_WAV_BYTES" * 100

    tts_mod._PROVIDER_DISPATCH["elevenlabs"] = _fail
    tts_mod._PROVIDER_DISPATCH["openai_tts"] = _succeed
    try:
        result = tts_mod.TTSPlugin().synthesize_input(
            scope_role="voice_caller", ground_truth_hint="Hello",
        )
        assert result.file_path, (
            f"synthesis returned no file despite openai_tts being available: "
            f"{result.notes}"
        )
        assert result.ground_truth.get("voice_provider") == "openai_tts", (
            f"expected failover to openai_tts, got: {result.ground_truth}"
        )
    finally:
        tts_mod._PROVIDER_DISPATCH.clear()
        tts_mod._PROVIDER_DISPATCH.update(original_dispatch)


def test_assemble_report_extracts_scope_runs_from_agent5():
    """_extract_scope_runs handles both Pydantic-model and dict shapes
    that may come from Agent 5's scope_runs output."""
    from puzzleeval.report import _extract_scope_runs
    # Empty / None inputs.
    assert _extract_scope_runs(None) == []
    assert _extract_scope_runs({}) == []
    assert _extract_scope_runs({"scope_runs": []}) == []
    # Dict-shaped entries pass through.
    dict_entry = {
        "scope_id": "step_1",
        "scope_role": "ocr",
        "candidate_results": [],
        "test_case_count": 5,
        "evaluation_mode": "objective",
    }
    out = _extract_scope_runs({"scope_runs": [dict_entry]})
    assert out == [dict_entry]


# ---------------------------------------------------------------------------
# 4. Real-run capability fixes (trace real_debug_3 / real_debug_4)
# ---------------------------------------------------------------------------


def test_builder_prompt_has_error_handling_contract():
    """Regression: trace real_debug_4b revealed the adversarial battery
    dropped every built harness because the harnesses CRASHED on empty /
    malformed / bad-creds probes instead of returning {success: False}.
    The builder prompt's old `Handle ALL errors gracefully` bullet was
    too soft. The new ERROR-HANDLING CONTRACT section spells out the
    six probe modes + the exact return shape the harness must produce.
    """
    from puzzleeval.agents.implement_test_env import BUILDER_SYSTEM_PROMPT
    assert "ERROR-HANDLING CONTRACT" in BUILDER_SYSTEM_PROMPT
    # Each of the six adversarial probes must be named in the contract so
    # the model can't selectively handle only happy-path errors.
    for probe in (
        "Empty / missing input",
        "Malformed input",
        "Oversized input",
        "Bad credentials",
        "Idempotency",
        "Network / SDK exceptions",
    ):
        assert probe in BUILDER_SYSTEM_PROMPT, (
            f"ERROR-HANDLING CONTRACT missing probe: {probe}"
        )
    # The skeleton must include the wrap-everything-in-try/except pattern.
    assert "try:" in BUILDER_SYSTEM_PROMPT
    assert "except Exception as exc" in BUILDER_SYSTEM_PROMPT


def test_smoke_test_template_exercises_adversarial_probes():
    """Regression: the old smoke test only verified structural shape +
    connection-error mock. It never caught contract violations, so
    harnesses reached HARNESS_COMPLETE with happy-path-only error
    handling and then died in the adversarial battery. The new template
    replays the same six probes the battery will run."""
    from puzzleeval.agents.implement_test_env import BUILDER_SYSTEM_PROMPT
    for probe_label in (
        "Probe 2: empty input",
        "Probe 3: malformed input",
        "Probe 4: oversized input",
        "Probe 5: bad credentials",
        "Probe 6: concurrency",
    ):
        assert probe_label in BUILDER_SYSTEM_PROMPT, (
            f"Smoke test template missing {probe_label}"
        )
    # The _assert_clean_failure helper is what enforces the contract.
    assert "_assert_clean_failure" in BUILDER_SYSTEM_PROMPT


def test_agent3_empty_retry_helper_exists():
    """Regression: trace real_debug_4 saw Agent 3 return
    ``{"test_cases": []}`` in 2 seconds. The existing topup loop didn't
    recover because it keys off test_plan.scope_specs capability which
    Agent 1 can leave None. `_retry_empty_generation` fires ONCE when
    the first pass emits zero cases, using sub_tasks directly so it
    doesn't depend on test_plan completeness."""
    from puzzleeval.agents.agent3.core import _retry_empty_generation
    import inspect
    sig = inspect.signature(_retry_empty_generation)
    # Keyword-only, matches run_synthetic_tests_agent's calling site.
    assert set(sig.parameters.keys()) == {
        "result", "input_data", "client", "logger",
    }


def test_agent3_empty_retry_wired_into_generate_function():
    """The empty-retry must actually fire from
    run_synthetic_tests_agent when the first pass returns 0 cases."""
    src = (ROOT / "puzzleeval" / "agents" / "agent3" / "core.py").read_text(
        encoding="utf-8"
    )
    # The call site lives inside run_synthetic_tests_agent, gated on
    # `if not result.test_cases:`.
    assert "if not result.test_cases:" in src
    assert "_retry_empty_generation(" in src
    # Order matters — empty-retry must run BEFORE topup so topup can
    # operate on a non-empty result.
    empty_idx = src.find("_retry_empty_generation(")
    topup_idx = src.find("_topup_undergenerated_subtasks(", empty_idx)
    assert empty_idx < topup_idx, (
        "empty-retry must fire before top-up; top-up depends on a "
        "non-empty starting result for per-sub_task shortfall math."
    )


def test_agent2_validator_suppresses_legacy_warning_on_phase4_coverage():
    """Regression: the fuzzy description-overlap check false-positived in
    real_debug_3 ('Invoice OCR & data extraction' vs a 22-word Agent 1
    description). Phase 4's structured covers_step_ids is authoritative
    when populated on every candidate + every scope."""
    from puzzleeval.validators import validate_agent2_output
    from puzzleeval.schemas import (
        Agent2Result, Candidate, UserUnderstandingOutput, SubTask, InfoStatus,
        WorkflowBlueprint, WorkflowStep, Constraints,
    )

    agent1 = UserUnderstandingOutput(
        is_clear=True,
        summary="s",
        sub_tasks=[SubTask(
            description="Given a PDF invoice, extract the vendor name, "
                        "total amount, and structured line items into JSON",
            capability="document OCR",
            search_keywords=["invoice OCR API"],
        )],
        search_strategy="both",
        domain="accounting",
        search_keywords=["invoice OCR"],
        constraints=Constraints(),
        info_status=InfoStatus(has_concrete_subtasks=True, has_domain=True),
        workflow=WorkflowBlueprint(steps=[
            WorkflowStep(
                id="step_1", role="ocr",
                description="OCR invoice", capability="document OCR",
                input_from="user", output_format="structured_json",
            ),
        ]),
    )
    agent2 = Agent2Result(
        candidates=[
            Candidate(
                name="Mindee", provider="Mindee",
                description="OCR API",
                api_available=True,
                pricing_model="freemium",
                claimed_capabilities=["invoice OCR"],
                adoption_difficulty="easy",
                relevance_score=0.9,
                source="test",
                # Short Agent-2 summary — would fail fuzzy match alone.
                relevant_subtasks=["Invoice OCR & data extraction"],
                # But Phase 4 coverage is populated — this MUST suppress
                # the fuzzy warning.
                covers_step_ids=["step_1"],
                coverage_confidence={"step_1": "claimed"},
            ),
        ],
        search_approach="test",
        coverage_notes="",
    )
    r = validate_agent2_output(agent2, agent1)
    warning_text = "\n".join(r.warnings)
    assert "Sub-tasks not covered by any candidate" not in warning_text, (
        "Phase 4 coverage is complete — legacy fuzzy warning should be suppressed."
    )


def test_agent2_validator_still_warns_when_phase4_coverage_incomplete():
    """Inverse of the above — if covers_step_ids is empty (legacy flow),
    the fuzzy check must still run. Don't silently drop coverage
    detection on pre-Phase-4 pipelines."""
    from puzzleeval.validators import validate_agent2_output
    from puzzleeval.schemas import (
        Agent2Result, Candidate, UserUnderstandingOutput, SubTask, InfoStatus,
        Constraints,
    )

    agent1 = UserUnderstandingOutput(
        is_clear=True,
        summary="s",
        sub_tasks=[SubTask(
            description="A very specific long description that won't fuzzy-match",
            capability="something",
            search_keywords=["kw"],
        )],
        search_strategy="both",
        domain="d",
        search_keywords=["kw"],
        constraints=Constraints(),
        info_status=InfoStatus(has_concrete_subtasks=True, has_domain=True),
        workflow=None,  # No blueprint → pure legacy flow.
    )
    agent2 = Agent2Result(
        candidates=[
            Candidate(
                name="X", provider="X",
                description="X",
                api_available=True,
                pricing_model="free",
                claimed_capabilities=["something"],
                adoption_difficulty="easy",
                relevance_score=0.9,
                source="test",
                relevant_subtasks=["totally unrelated capability summary"],
                # covers_step_ids empty — Phase 4 not active.
            ),
        ],
        search_approach="test",
        coverage_notes="",
    )
    r = validate_agent2_output(agent2, agent1)
    warning_text = "\n".join(r.warnings)
    assert "Sub-tasks not covered by any candidate" in warning_text


def test_adversarial_battery_calls_harness_with_positional_dict():
    """Regression: the adversarial battery's subprocess driver used to
    call ``run(**payload)`` (keyword-splat), but the harness contract is
    ``run(input_data: dict) -> dict`` — a SINGLE positional dict. Every
    probe raised TypeError BEFORE the harness's own code ran, so every
    harness ever built was reported as 'crash' and test execution was
    skipped. This is the direct reason real runs (real_debug_3 through
    real_debug_5) never produced final results. The driver must pass
    payload positionally.
    """
    from puzzleeval import adversarial_verifier
    # The driver source is embedded as a string literal in _invoke_harness.
    # Grep it rather than execute — faster + doesn't need a sandbox.
    import inspect
    src = inspect.getsource(adversarial_verifier._invoke_harness)
    # The active driver line is `"    result = run(payload)\n"` — the
    # leading 4-space indent + the assignment distinguish the live code
    # from any free-text mention of `run(**payload)` that lives in a
    # comment explaining the historical bug.
    assert '"    result = run(payload)\\n"' in src, (
        "Driver must call run(payload), not run(**payload). Re-check the "
        "calling convention after any adversarial_verifier refactor."
    )
    assert '"    result = run(**payload)\\n"' not in src, (
        "The buggy kwargs-splat call is back — every harness will crash."
    )


def test_execute_single_test_round_trips_bytes_via_b64_sentinel():
    """Regression: voice_dual_7 produced 5 caller MP3s + a "merged
    conversation" file that was caller-only — the agent's TTS audio
    silently vanished. Root cause: harness returned raw `bytes` in
    raw_response.audio_bytes; subprocess driver used
    `json.dump(..., default=str)` which stringified bytes as
    `"b'\\xff\\xfb...'"` (Python repr); plugin's
    `isinstance(audio_bytes, bytes)` check then failed. General fix:
    bytes-safe round-trip — exec_script encodes bytes as
    `{"_b64": "..."}` sentinels, Agent 5 inflates them back to bytes
    before returning the result dict."""
    from puzzleeval.agents.implement_test_env import _inflate_b64_sentinels
    import base64
    raw_audio = b"\xff\xfb\x90\x00" + b"A" * 1024  # MP3-ish payload
    encoded = {"_b64": base64.b64encode(raw_audio).decode("ascii")}
    # Round-trip the sentinel — must come back as exact bytes.
    out = _inflate_b64_sentinels({
        "raw_response": {
            "audio_bytes": encoded,
            "audio_format": "mp3",
            "transcription": "hello",  # pure-string field stays a string
        },
        "success": True,
    })
    assert out["raw_response"]["audio_bytes"] == raw_audio
    # Adjacent string fields untouched.
    assert out["raw_response"]["transcription"] == "hello"
    # Malformed sentinels left alone (no crash).
    out2 = _inflate_b64_sentinels({"_b64": 12345})
    assert out2 == {"_b64": 12345}
    # Nested in lists.
    out3 = _inflate_b64_sentinels([encoded, "plain", encoded])
    assert out3[0] == raw_audio and out3[2] == raw_audio and out3[1] == "plain"


def test_exec_script_encodes_bytes_with_b64_sentinel():
    """The subprocess driver script must base64-encode bytes via the
    `_bytes_safe` helper before json.dump. Without this, raw bytes
    fall back to ``default=str`` which produces useless Python repr."""
    src = _agent5_combined_source()
    # The encoder helper must be defined inside the exec_script.
    assert "_bytes_safe" in src
    assert '"_b64": base64.b64encode' in src
    # And applied to the harness result before dumping.
    assert "safe = _bytes_safe(result)" in src
    # The decoder helper must run on the read side.
    # Phase 6.1: helper renamed inflate_b64_sentinels (canonical, no leading _).
    assert (
        "_inflate_b64_sentinels(result)" in src
        or "inflate_b64_sentinels(result)" in src
    )


def test_id3v2_strip_handles_real_openai_tts_header():
    """Regression: voice_v3 merged 4 MP3 segments byte-wise. Players
    only played the first segment because the leading ID3v2 tag
    declared a 3-second duration. The merger must strip ID3v2 tags
    from subsequent segments so the concat stream parses as a
    continuous frame sequence."""
    from puzzleeval.tool_plugins.voice_realtime import (
        _strip_id3v2_header, _strip_id3v1_trailer,
    )
    # Build an ID3v2 tag of size 33 bytes payload (synchsafe-encoded)
    # + 10-byte header = 43 byte total tag, then a fake MP3 frame.
    tag_payload = b"X" * 33
    size_synchsafe = bytes([0, 0, 33 >> 7 & 0x7F, 33 & 0x7F])
    tag_header = b"ID3\x04\x00\x00" + size_synchsafe
    fake_frame = b"\xff\xfb\x90\x00" + b"A" * 100  # MP3 frame
    raw = tag_header + tag_payload + fake_frame
    stripped = _strip_id3v2_header(raw)
    assert stripped == fake_frame, (
        f"ID3v2 strip should leave only frames; got {len(stripped)} "
        f"bytes (expected {len(fake_frame)})"
    )

    # No ID3v2 → pass through.
    no_tag = b"\xff\xfb\x90\x00" + b"B" * 200
    assert _strip_id3v2_header(no_tag) == no_tag

    # Malformed ID3v2 (size byte with high bit set) → leave alone.
    bad_size = b"ID3\x04\x00\x00\xff\xff\xff\xff" + b"C" * 100
    assert _strip_id3v2_header(bad_size) == bad_size

    # ID3v1 trailer (TAG + 125 bytes at end) — strip.
    body_with_v1 = fake_frame + b"TAG" + b"D" * 125
    assert _strip_id3v1_trailer(body_with_v1) == fake_frame
    # No trailer → pass through.
    assert _strip_id3v1_trailer(fake_frame) == fake_frame


def test_voice_plugin_session_dir_is_thread_local():
    """Regression: voice_dual_6 + voice_dual_7 — Agent 5 runs
    candidates in parallel via ThreadPoolExecutor. Each worker called
    `plugin.set_session_dir(<sandbox>/voice/)` for its own candidate.
    The plugin is a module-level singleton, so the LAST set_session_dir
    won for both workers → BOTH candidates' caller+agent audio landed
    in one folder (whichever set it last). Real-run evidence:
    voice_dual_6's audio_paths showed `elevenlabs_voice_stack/voice/`
    for OpenAI Voice Stack's audio. Fix: thread-local session_dir so
    each worker reads its own value."""
    from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
    from pathlib import Path
    import tempfile
    import threading

    plugin = VoiceRealtimePlugin()
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        results: dict[str, Path] = {}
        barrier = threading.Barrier(2)
        def _worker(name, path):
            plugin.set_session_dir(path)
            # Wait so both threads have set their dir before either reads.
            barrier.wait()
            results[name] = plugin._session_dir
        ta = threading.Thread(target=_worker, args=("a", Path(tmp_a)))
        tb = threading.Thread(target=_worker, args=("b", Path(tmp_b)))
        ta.start(); tb.start()
        ta.join(); tb.join()
        assert results["a"] == Path(tmp_a), (
            f"Thread A should see its own dir; got {results['a']}"
        )
        assert results["b"] == Path(tmp_b), (
            f"Thread B should see its own dir; got {results['b']}"
        )
        # Critical invariant: the two threads see DIFFERENT dirs even
        # though they share one plugin instance. Without thread-local,
        # both would see whichever set_session_dir ran last.
        assert results["a"] != results["b"]


def test_voice_plugin_responder_derives_content_type_from_audio_format():
    """Regression: voice harnesses commonly return
    ``raw_response = {"audio_bytes": <bytes>, "audio_format": "mp3"}``
    WITHOUT an explicit ``audio_content_type``. The plugin's
    ``_responder`` used to default to ``"audio/wav"`` which made
    ``_save_audio_blob`` write `.wav` files containing MP3 bytes
    (corrupted playback) AND the merger refused to concat the
    mixed-extension caller(.mp3)+agent(.wav) sets, producing a
    caller-only "merged conversation". General fix: derive
    content_type from audio_format ("mp3" → "audio/mpeg") when not
    explicitly provided."""
    src = (
        ROOT / "puzzleeval" / "tool_plugins" / "voice_realtime.py"
    ).read_text(encoding="utf-8")
    # The mapping must be present and cover the common formats.
    for entry in ('"mp3":  "audio/mpeg"', '"wav":  "audio/wav"',
                  '"webm": "audio/webm"'):
        assert entry in src, f"Format-to-content-type mapping missing: {entry}"
    # And the derivation must run when audio_content_type is absent.
    assert "raw.get(\"audio_format\")" in src


def test_voice_plugin_extracts_audio_from_base64_string():
    """Belt-and-braces: even when a harness explicitly base64-encodes
    its own audio_bytes (older convention), the plugin must still save
    + transcribe the audio. Was previously failing the
    `isinstance(audio_bytes, bytes)` check, dropping the audio
    silently."""
    src = (
        ROOT / "puzzleeval" / "tool_plugins" / "voice_realtime.py"
    ).read_text(encoding="utf-8")
    # The base64 string fast-path branch must exist before the bytes
    # isinstance check.
    assert "isinstance(audio_bytes, str)" in src
    assert "b64decode" in src
    # And the size guard prevents short strings (transcripts) from
    # being mis-decoded as audio.
    assert "len(audio_bytes) >= 100" in src


def test_agent5_skips_pre_call_for_multi_call_modalities():
    """Regression: voice_dual_6 — Agent 5 was pre-calling
    ``harness.run(adapted_input)`` ONCE per test case to seed the
    evaluator's `response` argument. For multi-call modalities (voice
    plugin owns the loop), the adapted input lacks audio_url +
    turn_index + session_state, so strict harnesses (ElevenLabs Voice
    Stack) correctly returned ``success=False``, which got recorded
    as a real test failure and skipped the entire plugin path. Lenient
    harnesses (OpenAI) tolerated the bogus pre-call and got plugin
    eval. Fix: detect multi-call modality (input_type or output_type
    in {conversation, voice_conversation, voice_turn}) and SKIP the
    pre-call — synthesize a placeholder so the plugin's drive_loop
    owns every real harness invocation."""
    src = _agent5_combined_source()
    assert "multi_call_pre_call_skipped" in src, (
        "Agent 5 must skip the pre-call for multi-call modalities; "
        "otherwise strict harnesses fail before the plugin can run."
    )
    # Both input + output multi-call modality sets must be checked.
    assert "multi_call_input_types" in src
    assert "multi_call_output_types" in src


def test_agent5_sets_voice_session_dir_before_evaluation():
    """Regression: voice_dual_4 wrote per-turn caller/agent + merged
    conversation MP3s to ``%TEMP%/puzzleeval_voice/`` instead of
    ``runs/<trace_id>/harnesses/<slug>/voice/``. Reason: the prior
    wiring only set session_dir on the plugin during INPUT synthesis
    (``_synthesize_test_input_via_plugin``), which is skipped for
    ``input_type=conversation`` tests — drive_conversation runs INSIDE
    evaluate_output, with no session_dir set. The backend's audio
    streamer refuses paths outside its allowed runs roots → frontend
    silently can't play those clips. Fix: set session_dir on every
    plugin that supports it BEFORE every candidate's test execution
    so synthesis + multi-turn drive both write into the run dir."""
    src = _agent5_combined_source()
    # The test-execution prep block calls set_session_dir per plugin.
    assert (
        "voice_session_dir" in src
        and "set_session_dir(voice_session_dir)" in src
    ), (
        "Agent 5 must set every plugin's session_dir to "
        "<sandbox>/voice/ before test execution so multi-turn audio "
        "lands under the run directory."
    )


def test_tool_runner_direct_invokes_owner_plugin_for_multi_call_modality():
    """Regression: trace voice_dual_4 — same input, same eligible
    plugins, BUT Claude's tool_runner non-deterministically picked
    voice_realtime for ElevenLabs (5 turns driven, verdict promoted)
    while skipping it for OpenAI (collapsed to llm_judge fallback,
    single-turn). When a plugin OWNS the modality (modality enum match
    + requires_harness_runner=True) and a harness_runner is supplied,
    invoke it DIRECTLY rather than leaving Claude to roll the dice.
    Plugin's drive_conversation IS the authoritative path; tool_picker
    has nothing to add."""
    src = (
        ROOT / "puzzleeval" / "plugin_tool_runner.py"
    ).read_text(encoding="utf-8")
    # The fast-path branch must exist + log so operators can see it
    # firing (and audit with grep when results look weird).
    assert "tool_runner_direct_invoke_owner" in src, (
        "Missing direct-invoke owner fast path; multi-call modalities "
        "will keep flapping between plugin path and llm_judge fallback."
    )
    # Owner detection requires modality enum match + requires_harness_runner.
    assert "requires_harness_runner" in src
    assert (
        "input_type in p.capabilities().input_types" in src
        or "output_type in p.capabilities().output_types" in src
    )


def test_orphan_scrubber_matches_advisor_tool_result_by_suffix():
    """Regression: trace voice_dual_3 — the orphan-server-tool-use
    scrubber matched ``web_search_tool_result``/``web_fetch_tool_result``
    explicitly, but missed ``advisor_tool_result`` because the SDK
    uses a per-tool-family type literal (``<name>_tool_result``). The
    scrubber wrongly classified valid advisor pairs as orphans,
    stripped the server_tool_use, and the next API call 400'd. Fix:
    match by ``*_tool_result`` suffix so any server-tool family lands
    automatically.

    Phase 4.1 migration: detection logic moved to
    puzzleeval/agents/agent5/turn_blocks.py. The suffix-match invariant
    lives in the new canonical location.
    """
    src = (
        ROOT / "puzzleeval" / "agents" / "agent5" / "turn_blocks.py"
    ).read_text(encoding="utf-8")
    assert 'btype.endswith("_tool_result")' in src, (
        "Orphan scrubber should match server-tool result blocks by "
        "suffix, not by an explicit allowlist that skips future tools."
    )
    # Sanity: no explicit two-tool tuple (the regressed pattern).
    assert (
        '("web_search_tool_result", "web_fetch_tool_result",'
        not in src
    ), "Old explicit-list classifier still present — would re-bug."


def test_agent5_builder_scrubs_orphan_server_tool_use_every_turn():
    """Regression: traces voice_debug_4 + voice_debug_5 both showed
    ``<tool>_tool_use was found without a corresponding
    <tool>_tool_result block`` 400 errors on the turn AFTER an
    assistant response contained a server_tool_use (web_search /
    web_fetch / advisor) without its matching result block. This
    happens with any stop_reason (max_tokens, tool_use, end_turn)
    depending on where the server-side execution broke. The fix is a
    UNIVERSAL pre-append scrubber: inventory server_tool_use + result
    ids in response.content, drop orphaned server_tool_use blocks, and
    fall back to a minimal text block when stripping empties the
    content. Runs every turn, not just on max_tokens."""
    src = _agent5_combined_source()
    assert "orphan_server_tool_strip" in src, (
        "Builder loop missing universal orphan-server-tool-use scrubber "
        "(operation label 'orphan_server_tool_strip'). Without it, a "
        "truncated server-tool response poisons the next API call."
    )
    # The fix identifies server_tool_use vs server_tool_result ids.
    assert "server_tool_use" in src
    assert "web_search_tool_result" in src
    # The scrubber names all three server tools we use.
    for tool in ("web_search", "web_fetch", "advisor"):
        assert f'"{tool}"' in src


# (The heavier end-to-end `plugin.evaluate_output(...)` test was dropped
# because instantiating VoiceRealtimePlugin binds a local HTTP port +
# does real TTS, which polluted state for later tests/test_voice_realtime.py
# tests in the same session. The simpler `_extract_conversation_script`
# test above + the real-run voice verification in runs/voice_debug_* cover
# the same invariant without side effects.)


def test_tool_runner_promotes_plugin_verdict_when_claude_skips_score_verdict():
    """Regression: trace voice_debug_6. The voice plugin drove 5 turns
    cleanly, produced passed/score/reasoning, but tool_runner saw
    ``final_parsed is None`` (Claude didn't emit a structured
    ``ScoreVerdict``) and returned fallback_reason='no_structured_verdict',
    which collapsed the run back to single-turn LLM judge. The real
    intelligence (per-turn transcripts + drive_conversation scoring)
    was lost on the floor. General fix: capture every plugin verdict
    in EvalContext and promote the last conclusive one when Claude's
    structured output is missing. Plugin scoring IS authoritative."""
    from puzzleeval.plugin_tool_runner import (
        EvalContext, _CapturedPluginVerdict, ToolRunnerVerdict,
    )
    # Schema guards: the capture dataclass + list field must exist.
    assert hasattr(EvalContext, "__dataclass_fields__")
    assert "plugin_verdicts" in EvalContext.__dataclass_fields__
    # The source must contain the promotion branch.
    src = (
        ROOT / "puzzleeval" / "plugin_tool_runner.py"
    ).read_text(encoding="utf-8")
    assert "promoting plugin" in src, (
        "tool_runner missing promote-plugin-verdict branch; voice plugin "
        "verdicts will keep evaporating into LLM-judge fallback."
    )
    assert "_CapturedPluginVerdict" in src
    # When a plugin returns EvaluationResult(fallback_reason=None) with
    # non-empty reasoning, we promote it — the positive invariant the
    # promotion branch guards.
    assert "fallback_reason is None" in src and "reasoning.strip()" in src


def test_voice_plugin_emits_merged_conversation_audio_path():
    """Regression: trace voice_debug_6 produced 2 per-turn audio files
    and the user wanted to hear the full call as one file. After
    drive_conversation, the plugin merges caller_0/agent_0/caller_1/
    agent_1/… into a single ``conversation_<token>.<ext>`` clip and
    surfaces it as the FIRST artifact with role='conversation'. Guard
    that the merge function exists + is invoked + the merged artifact
    is featured in audio_paths."""
    from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
    plugin = VoiceRealtimePlugin()
    # Function + shape contracts.
    assert hasattr(plugin, "_merge_conversation_audio")
    import inspect
    sig = inspect.signature(plugin._merge_conversation_audio)
    params = set(sig.parameters.keys()) - {"self"}
    assert params == {"session_token", "turns"}, (
        f"merge signature changed; expected (session_token, turns), got {params}"
    )
    # Real MP3 concat works on real bytes.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "caller_turn0.mp3"
        p2 = Path(tmp) / "agent_turn0.mp3"
        p1.write_bytes(b"\xff\xfb\x90\x00" + b"A" * 1024)  # MP3-ish header + body
        p2.write_bytes(b"\xff\xfb\x90\x00" + b"B" * 512)
        turns = [
            {"turn_index": 0, "caller_path": str(p1), "agent_path": str(p2)},
        ]
        merged = plugin._merge_conversation_audio(
            session_token="abc123def456abcd", turns=turns,
        )
        assert merged is not None
        merged_path = Path(merged)
        assert merged_path.exists()
        # Merged file is approximately the sum of inputs.
        assert merged_path.stat().st_size >= 1024 + 512
        # Named after the session and in the same directory.
        assert merged_path.name.startswith("conversation_")
        assert merged_path.suffix == ".mp3"


def test_voice_plugin_merge_returns_none_on_mixed_extensions():
    """The merger refuses mixed extensions — concatenating an mp3 with
    a wav produces garbage. Return None and let callers keep per-turn
    artifacts instead of creating a corrupt file."""
    from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
    import tempfile
    from pathlib import Path
    plugin = VoiceRealtimePlugin()
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "caller_turn0.mp3"
        p2 = Path(tmp) / "agent_turn0.wav"
        p1.write_bytes(b"\xff\xfb" + b"A" * 100)
        p2.write_bytes(b"RIFF" + b"B" * 100)
        turns = [{"turn_index": 0, "caller_path": str(p1), "agent_path": str(p2)}]
        merged = plugin._merge_conversation_audio(
            session_token="token123456789ab", turns=turns,
        )
        assert merged is None


def test_audio_route_allowlist_includes_cli_dev_root():
    """Regression: the backend audio-streaming route used to only serve
    files under ``puzzleeval-api/runs``. CLI-driven runs land under
    ``PuzzleEval-local/runs`` and got 403 — the frontend silently
    couldn't play their audio. The allowlist must include both roots
    plus any extras from ``PUZZLEEVAL_EXTRA_RUNS_ROOTS``."""
    src = (
        ROOT.parent / "puzzleeval-api" / "routes" / "runs.py"
    ).read_text(encoding="utf-8")
    # Both roots are configured.
    assert '"runs"' in src
    assert "PuzzleEval-local" in src, (
        "Audio route missing CLI dev runs root in allowlist."
    )
    # Operator-extensible.
    assert "PUZZLEEVAL_EXTRA_RUNS_ROOTS" in src


def test_credential_resolver_unions_cross_provider_registry_entries():
    """Regression: "ElevenLabs Voice Stack" needs ElevenLabs + OpenAI +
    Anthropic keys because the harness is a cross-provider wrapper.
    The old resolver short-circuited on the first registry match
    (ElevenLabs) and dropped OpenAI/Anthropic keys. General fix: union
    every registered provider whose key is mentioned anywhere in the
    candidate's searchable surface (name/provider/notes)."""
    from puzzleeval.agents.implement_test_env import _resolve_candidate_credentials
    # Build a minimal TestHarness-like stub.
    class _H:
        candidate_name = "ElevenLabs Voice Stack"
        provider = "ElevenLabs"
        validation_notes = (
            "Uses ElevenLabs for TTS, OpenAI Whisper for STT, "
            "Anthropic Claude Haiku for reasoning."
        )
        auth_env_vars: list[str] = []
    provider_credentials = {
        "elevenlabs": {"ELEVENLABS_API_KEY": "el_key"},
        "openai":     {"OPENAI_API_KEY": "oa_key"},
        "mindee":     {"MINDEE_API_KEY": "mi_key"},  # should NOT match
    }
    creds = _resolve_candidate_credentials(_H(), provider_credentials)
    assert creds is not None
    # ElevenLabs matches by name.
    assert creds.get("ELEVENLABS_API_KEY") == "el_key"
    # OpenAI matches because "OpenAI" appears in validation_notes.
    assert creds.get("OPENAI_API_KEY") == "oa_key"
    # Mindee never appears in searchable surface → NOT included.
    assert "MINDEE_API_KEY" not in creds


def test_voice_plugin_extracts_script_from_json_string_dict():
    """Regression: trace voice_debug_3. ``TestCase.expected_output`` is
    typed ``str`` — multi-turn voice tests serialize the script as a
    JSON string. The extractor used to parse ONLY list-shaped strings
    (starting with ``[``), not the natural dict-shaped form
    (``'{"conversation_script": [...]}'``). Every voice test fell
    through to single-turn evaluation → score 0.1 regardless of
    content. Must handle BOTH shapes."""
    from puzzleeval.tool_plugins.voice_realtime import _extract_conversation_script

    # Dict-wrapped JSON string — the shape that real tests use.
    dict_json = (
        '{"conversation_script": ['
        '{"user_text": "hi", "expected_agent_contains": "hello"}'
        ']}'
    )
    out = _extract_conversation_script(dict_json)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["user_text"] == "hi"

    # List-shaped JSON string — legacy shape, still supported.
    list_json = (
        '[{"user_text": "hi", "expected_agent_contains": "hello"},'
        ' {"user_text": "bye", "expected_agent_contains": "goodbye"}]'
    )
    out = _extract_conversation_script(list_json)
    assert isinstance(out, list) and len(out) == 2

    # Garbage stays None (no crash).
    assert _extract_conversation_script("not json at all") is None
    assert _extract_conversation_script("[broken json") is None
    assert _extract_conversation_script("{\"foo\": 1}") is None  # no script key


def test_agent5_harness_runner_threads_input_context_per_test_case():
    """Regression: trace voice_debug_3. The harness runner closure used
    to be hoisted out of the per-test loop for performance — but that
    meant every test case got the SAME runner, so the user's
    per-test-case ``input_context`` (system prompt / persona /
    grounding) never reached the harness on multi-turn plugin-driven
    runs. voice_realtime builds its per-turn payload from scratch
    ({audio_url, turn_index, session_state}) — anything not in the
    payload the plugin forwards disappears. Fix: wrap the runner per
    test case, merging this case's ``input_context`` into every payload
    the plugin forwards. Guard: the source must mention the per-test
    runner wrapping pattern in BOTH the deterministic and tool_runner
    dispatch paths."""
    src = _agent5_combined_source()
    # Both paths must merge input_context into the runner payload. The
    # original literal `merged["input_context"] = _ctx` was generalized
    # in trace f1312253 to route through `_merge_with_default_input_context`
    # which handles the three real-world cases (null / partial-other-keys
    # / partial-with-prompt). Either implementation is acceptable as
    # long as both runner closures inject input_context.
    count_legacy = src.count('merged["input_context"] = _ctx')
    count_new = src.count('_merge_with_default_input_context(')
    # Helper definition (1) + two closure call sites (2) ≥ 3 in the new
    # pattern; OR the legacy pattern appears at least twice.
    assert (count_legacy >= 2) or (count_new >= 3), (
        f"Expected the input_context-merging runner wrapper in BOTH "
        f"dispatch paths; found legacy={count_legacy}, "
        f"merge-helper={count_new}. If neither pattern is wired, "
        f"per-test input_context silently drops on multi-call plugin runs."
    )


def test_builder_prompt_teaches_harness_to_read_input_context_instructions():
    """Regression: trace voice_debug_3 built a harness that looked for
    input_context['system_prompt'] but not input_context['instructions']
    — the canonical key the voice plugin uses. With the runner wrapper
    shipping ``input_context`` on every turn, the builder prompt must
    explicitly teach the harness to read ``input_context['instructions']``
    first, aliased to common synonyms, and refuse to hardcode a
    default persona."""
    from puzzleeval.agents.implement_test_env import _build_initial_message
    # The prompt-extension lives inside the test-case-forms block
    # built by _build_initial_message when multi-call modalities are
    # present. Grep the source directly for the invariant — the
    # MULTI-CALL HARNESS CONTRACT must teach the instructions keys.
    src = _agent5_combined_source()
    # Canonical key is "instructions".
    assert "input_context['instructions']" in src
    # At least TWO of the common aliases must be mentioned so builders
    # handle variant test-case shapes.
    aliases = ["'system_prompt'", "'system'", "'brief'", "'agent_prompt'"]
    mentioned = sum(1 for a in aliases if a in src)
    assert mentioned >= 2, (
        f"Builder prompt should document at least 2 persona-key aliases "
        f"so harnesses don't miss common variants; found {mentioned}."
    )
    # Anti-pattern: DO NOT hardcode DEFAULT_SYSTEM_PROMPT.
    assert "NEVER hardcode the persona" in src


def test_validate_agent5_accepts_none_agent4_output():
    """Regression: trace voice_debug_2 crashed the CLI's Agent-5 replay
    path (hand-crafted Agent-5 input without a prior Agent 4 run) with
    ``AttributeError: 'NoneType' object has no attribute 'validated_candidates'``
    because the cross-agent consistency check dereferenced
    ``agent4_output.validated_candidates`` unconditionally. The validator
    must tolerate ``agent4_output=None`` — the check is observational,
    not a correctness gate."""
    from puzzleeval.validators import validate_agent5_output
    from puzzleeval.schemas import Agent5Result, TestHarness
    # Minimal successful harness — no Agent 4 context available.
    h = TestHarness(
        candidate_name="voice-agent-x",
        provider="OpenAI",
        harness_code="def run(input_data): return {}",
        entry_file="harness.py",
        harness_dir=str(ROOT),  # any existing path
        requirements=[],
        auth_env_vars=[],
        auth_method="api_key",
        supported_input_types=["conversation"],
        supported_output_types=["voice_conversation"],
        smoke_test_passed=True,
        validation_notes="",
        build_turns=5,
        build_cost_usd=0.5,
    )
    result = Agent5Result(
        harnesses=[h],
        failed_harnesses=[],
        total_candidates_attempted=1,
        total_build_cost_usd=0.5,
        build_summary="1/1",
        candidate_runs=[],
        failed_test_runs=[],
        total_test_cases=0,
        total_test_cost_usd=0.0,
        test_execution_summary="0/0",
    )
    # Both (None) and (Agent4Result) paths must work.
    v1 = validate_agent5_output(result, None)
    assert isinstance(v1.errors, list)
    # No "'NoneType' object has no attribute" errors.
    assert not any("NoneType" in e for e in v1.errors), v1.errors


def test_agent3_validator_backfills_empty_coverage_summary():
    """Regression: the validator used to warn 'coverage_summary is empty'
    without computing the truth. The truth is trivially derivable from
    test_cases. Backfill silently; warn only on disagreement."""
    from puzzleeval.validators import validate_agent3_output
    from puzzleeval.schemas import (
        Agent3Result, TestCase, JudgementCriterion,
        UserUnderstandingOutput, SubTask, InfoStatus, Constraints,
    )

    agent1 = UserUnderstandingOutput(
        is_clear=True, summary="s",
        sub_tasks=[SubTask(
            description="foo", capability="cap",
            search_keywords=["k"],
        )],
        search_strategy="both", domain="d",
        search_keywords=["k"],
        constraints=Constraints(),
        info_status=InfoStatus(has_concrete_subtasks=True, has_domain=True),
    )
    # Agent 3 emitted 3 test cases but neglected coverage_summary.
    tc_list = [
        TestCase(
            id=f"t{i}",
            sub_task_ref="foo",
            scenario=f"scenario {i}",
            input_type="text", output_type="free_text",
            input_data="in", expected_output="out",
            difficulty="medium",
            judgement_criteria=[
                JudgementCriterion(criterion="c", weight=1.0,
                                   eval_type="semantic_similarity"),
            ],
            coverage_dimensions=["happy_path"],
            tags=[],
        )
        for i in range(3)
    ]
    agent3 = Agent3Result(
        test_cases=tc_list,
        generation_notes="",
        coverage_summary={},  # The bug — empty.
    )
    r = validate_agent3_output(agent3, agent1)
    # Validator mutates `result.coverage_summary` in place.
    assert agent3.coverage_summary == {"foo": 3}
    # And no spurious warning fires now.
    warning_text = "\n".join(r.warnings)
    assert "coverage_summary is empty" not in warning_text


# ---------------------------------------------------------------------------
# Agent 4 → Agent 5 handoff (deep-verify + atlas staging REMOVED)
# ---------------------------------------------------------------------------
#
# Agent 4 is now shallow verify only (exists / blocked). Agent 5 does its
# own Phase-1 research via web_search + web_fetch and writes its own
# api_spec.txt. No atlas handoff, no staging step.


def test_agent4_is_shallow_verify_not_deep_verify():
    """Regression guard: the deep-verify subsystem must be gone.

    Agent 4 is shallow verify only; Agent 5 does own research.
    """
    puzzleeval_dir = ROOT / "puzzleeval"
    # The deleted modules must NOT be present.
    assert not (puzzleeval_dir / "deep_verify_runner.py").exists()
    assert not (puzzleeval_dir / "deep_verify_prompt.py").exists()
    assert not (puzzleeval_dir / "provider_atlas.py").exists()
    assert not (puzzleeval_dir / "manual_atlas.py").exists()


def test_agent5_no_atlas_staging_block():
    """Agent 5 must NOT have an atlas-staging block that copies Agent 4's
    atlas into the sandbox. That whole machinery was removed when Agent 4
    became shallow-verify-only.
    """
    impl_src = _agent5_combined_source()
    assert "atlas_stage_into_sandbox" not in impl_src
    assert "candidate.api_spec_path = str(atlas_dest)" not in impl_src


# ---------------------------------------------------------------------------
# User selection filter → Agent 4 sync
# ---------------------------------------------------------------------------


def test_agent1_schema_carries_explicit_candidates_list():
    """Regression: Agent 1's output must carry ``explicit_candidates`` so
    downstream auto-injection knows which provider names the user named.

    Previously no schema field captured this, and "Compare OpenAI vs
    ElevenLabs" requests tested random adjacent voice services because
    Agent 2's web search didn't always surface the two explicitly-named
    providers.
    """
    from puzzleeval.schemas import UserUnderstandingOutput, InfoStatus, Constraints
    obj = UserUnderstandingOutput(
        summary="voice",
        sub_tasks=[],
        domain="voice",
        search_keywords=["voice api"],
        constraints=Constraints(),
        explicit_candidates=["OpenAI", "ElevenLabs"],
    )
    assert obj.explicit_candidates == ["OpenAI", "ElevenLabs"]
    # JSON round-trip must preserve the list.
    round_tripped = UserUnderstandingOutput.model_validate_json(obj.model_dump_json())
    assert round_tripped.explicit_candidates == ["OpenAI", "ElevenLabs"]
    # Default is the empty list — no backcompat break for pre-fix saved artifacts.
    obj_default = UserUnderstandingOutput(
        summary="foo",
        sub_tasks=[],
        domain="x",
        search_keywords=["y"],
        constraints=Constraints(),
    )
    assert obj_default.explicit_candidates == []


def test_inject_explicit_candidates_adds_missing_and_skips_present():
    """Auto-inject fills the Agent 2 pool with user-named providers when
    they're missing, and skips when they already appear (by substring
    in either direction so "OpenAI" matches "OpenAI Realtime API" etc.)."""
    from puzzleeval.agents.research import inject_explicit_candidates
    from puzzleeval.schemas import Agent2Result, Candidate

    pool = Agent2Result(
        candidates=[
            Candidate(
                name="OpenAI Realtime API",
                provider="OpenAI",
                description="",
                api_available=True,
                pricing_model="paid",
                claimed_capabilities=[],
                relevance_score=0.7,
                adoption_difficulty="medium",
                relevant_subtasks=[],
                source="search",
                covers_step_ids=["step_1"],
            ),
        ],
        search_approach="web",
        coverage_notes="",
    )
    # "OpenAI" substring-matches; "ElevenLabs" doesn't → inject one.
    result = inject_explicit_candidates(
        pool,
        explicit_names=["OpenAI", "ElevenLabs"],
        blueprint_step_ids=["step_1"],
    )
    names = [c.name for c in result.candidates]
    assert "OpenAI Realtime API" in names  # kept
    assert "ElevenLabs" in names  # injected
    assert len(result.candidates) == 2

    # Source tag must be user_explicit for surfacing priority + audit trail.
    injected = next(c for c in result.candidates if c.name == "ElevenLabs")
    assert injected.source == "user_explicit"
    assert injected.covers_step_ids == ["step_1"]


def test_inject_explicit_candidates_empty_passthrough():
    """No explicit names → identity."""
    from puzzleeval.agents.research import inject_explicit_candidates
    from puzzleeval.schemas import Agent2Result

    pool = Agent2Result(candidates=[], search_approach="x", coverage_notes="")
    assert inject_explicit_candidates(pool, []) is pool


def test_agent5_filters_out_uncredentialed_candidates(tmp_path, monkeypatch):
    """Credential-gated selection: candidates without a key in
    provider_registry.json (and not "no_auth") get filtered out BEFORE
    Agent 5 builds. A build that can't authenticate is a guaranteed waste.
    """
    from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
    from puzzleeval.schemas import (
        Agent5Input, ScreenedCandidate, Agent3Result, TestCase,
        JudgementCriterion, UserUnderstandingOutput, Constraints,
    )
    from unittest.mock import patch

    # Empty registry → nothing is credentialed.
    monkeypatch.setenv("PUZZLEEVAL_REGISTRY_PATH", str(tmp_path / "empty.json"))
    (tmp_path / "empty.json").write_text('{"providers": {}}', encoding="utf-8")

    uncredentialed = ScreenedCandidate(
        name="NoKeyProvider",
        provider="NoKeyProvider",
        description="",
        pricing_model="paid",
        claimed_capabilities=[],
        relevance_score=0.9,
        adoption_difficulty="medium",
        relevant_subtasks=[],
        source="search",
        verified_api_docs_url="https://example.com/docs",
        auth_method="api_key",
        api_access_method="paid_only",
        confirmed_capabilities=[],
        data_format_notes="",
        screening_notes="",
    )
    inp = Agent5Input(
        validated_candidates=[uncredentialed],
        user_understanding=UserUnderstandingOutput(
            summary="x", sub_tasks=[], domain="y",
            search_keywords=["z"], constraints=Constraints(),
        ),
        test_cases=Agent3Result(test_cases=[], generation_notes="", coverage_summary={}),
        trace_id="test-creds",
    )

    with patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic"):
        result = run_implement_test_env_agent(inp)

    # No harnesses built — credential gate filtered the candidate out.
    assert len(result.harnesses) == 0
    assert len(result.failed_harnesses) == 0
    assert result.total_candidates_attempted == 0


# is_pass_decision / _atlas_from_spec_text tests removed — the deep-verify
# runner that provided both functions was deleted when Agent 4 became
# shallow-verify-only.


def test_audio_route_registered_before_run_id_route():
    """Regression: ``/runs/audio`` MUST register before ``/runs/{run_id}``,
    otherwise FastAPI matches the path-parameter route first and the
    audio endpoint returns 404 "Run not found". That was why every
    voice recording silently failed to play in the frontend.

    Source-grep guard — watches for the correct ordering in the
    routes file without standing up the full app.
    """
    runs_py = (
        ROOT.parent / "puzzleeval-api" / "routes" / "runs.py"
    ).read_text(encoding="utf-8")
    audio_pos = runs_py.find('@router.get("/runs/audio")')
    run_id_pos = runs_py.find('@router.get("/runs/{run_id}"')
    assert audio_pos >= 0, "/runs/audio handler must exist in routes/runs.py"
    assert run_id_pos >= 0, "/runs/{run_id} handler must exist in routes/runs.py"
    assert audio_pos < run_id_pos, (
        "/runs/audio must be registered BEFORE /runs/{run_id} or FastAPI "
        f"matches the path-param route first and the audio endpoint 404s. "
        f"Got audio at offset {audio_pos}, run_id at offset {run_id_pos}."
    )


def test_pipeline_runner_syncs_agent2_model_after_selection_filter():
    """Regression for the user-visible bug where selecting 2 candidates
    at the SelectionPanel still resulted in Agent 4 screening all 7.

    Cause: ``apply_scope_picks`` updated ``state.agent2_result`` (the
    dict) but not ``state._agent2_model`` (the Pydantic object). Agent 4
    reads ``_agent2_model`` first (fast path), so it saw the pre-filter
    7-candidate model. Fix: assign both after the filter runs.

    Source grep: both branches of the selection resume logic must
    write back to ``state._agent2_model``.
    """
    runner_src = (
        ROOT.parent / "puzzleeval-api" / "services" / "pipeline_runner.py"
    ).read_text(encoding="utf-8")
    # The user-selection branch AND the auto-picks branch must both sync.
    sync_count = runner_src.count("state._agent2_model = a2_model")
    assert sync_count >= 2, (
        f"Expected at least 2 ``state._agent2_model = a2_model`` assignments "
        f"(user-selection + auto-picks branches), found {sync_count}. "
        "Without this sync, Agent 4 sees the pre-filter candidate list."
    )


# ---------------------------------------------------------------------------
# E2E real-run regressions (session 2026-04-20, trace d3b49875)
# ---------------------------------------------------------------------------


class TestVoicePCM16StringNormalization:
    """Real-run trace d3b49875: the OpenAI Realtime harness returned
    ``raw_response.audio_bytes`` as a base64 STRING (for JSON safety
    across the subprocess border). The voice plugin's multi-turn
    responder tried to call ``_encode_pcm16_to_mp3(<string>, ...)`` and
    ``_wrap_pcm16_as_wav(<string>, ...)``; pydub accepts strings at
    construction but fails at ``.export()`` with ``memoryview:
    a bytes-like object is required, not 'str'``, and wave's
    ``writeframes`` raises TypeError. Both paths caught the exception,
    ``audio_bytes_out`` stayed as the string, ``audio_content_type``
    stayed None, and downstream never saved any agent audio — all 8
    voice tests scored 0/8 with zero ``response_*`` files on disk.

    Fix: base64-decode at the TOP of the PCM16 branch, before pydub
    or wave touch the payload."""

    def test_pcm16_branch_decodes_b64_string_before_encoding(self):
        """Source grep: the decode must appear before
        ``_encode_pcm16_to_mp3`` in the responder."""
        src = (
            ROOT
            / "puzzleeval"
            / "tool_plugins"
            / "voice_realtime.py"
        ).read_text(encoding="utf-8")
        # Find the critical region: the if-block that handles pcm16.
        needle = 'if fmt in ("pcm", "pcm16"'
        idx = src.find(needle)
        assert idx > 0, "pcm16 branch missing"
        # The decode logic must appear BEFORE this if.
        window = src[max(0, idx - 1500):idx]
        assert "b64decode" in window, (
            "Responder must base64-decode audio_bytes_out before the "
            "pcm16 pydub/wave branch — otherwise strings get silently "
            "mishandled and agent audio vanishes."
        )
        assert "isinstance(audio_bytes_out, str)" in window, (
            "The decode must be gated on isinstance(str) so in-process "
            "bytes callers pass through unchanged."
        )

    def test_pcm16_encode_pipeline_accepts_b64_string(self):
        """Live simulation: harness returns b64 string, plugin responder
        must save real agent audio (non-None path)."""
        # This test exercises the actual flow end-to-end without real APIs.
        import base64
        import os
        import tempfile
        from puzzleeval.tool_plugins.voice_realtime import (
            _encode_pcm16_to_mp3,
            _wrap_pcm16_as_wav,
        )
        # 1 second of silence at 24kHz PCM16 mono = 48000 bytes
        raw_pcm = b"\x00\x00" * 24000
        b64_str = base64.b64encode(raw_pcm).decode("ascii")

        # Before fix: passing the STRING into _encode_pcm16_to_mp3 would
        # silently produce garbage or fail at export. Verify that if we
        # decode first, the helper works correctly on bytes.
        decoded = base64.b64decode(b64_str, validate=False)
        assert decoded == raw_pcm, "b64 roundtrip must be lossless"
        encoded_mp3 = _encode_pcm16_to_mp3(
            decoded, sample_rate=24000, channels=1,
        )
        if encoded_mp3 is not None:
            # pydub + ffmpeg available → real MP3 bytes (ID3 or frame sync)
            assert encoded_mp3[:3] == b"ID3" or encoded_mp3[:2] in (
                b"\xff\xfb", b"\xff\xf3", b"\xff\xf2",
            ), "encoded bytes must be valid MP3 framing"
        else:
            # pydub missing → fallback to WAV must work
            wav = _wrap_pcm16_as_wav(decoded, sample_rate=24000, channels=1)
            assert wav[:4] == b"RIFF", "WAV fallback must produce RIFF header"

    def test_responder_handles_audio_path_harness_return_shape(self):
        """Real-run trace abb00832: Agent 5 built an OpenAI Realtime
        harness that saves audio to a temp WAV and returns
        `raw_response["audio_path"]` (instead of the b64-string
        `audio_bytes` pattern). The voice plugin responder only knew
        about audio_bytes/twiml/ncco — the audio_path branch silently
        fell through, zero agent audio saved, scores 0/8.

        Both shapes are reasonable harness choices (some devs prefer
        file paths for small JSON payloads, others prefer inline
        bytes). The plugin must handle both so no harness author is
        accidentally penalized."""
        src = (
            ROOT
            / "puzzleeval"
            / "tool_plugins"
            / "voice_realtime.py"
        ).read_text(encoding="utf-8")
        # Responder must read the audio_path file into bytes and
        # continue down the bytes path (uniform contract).
        assert 'raw.get("audio_path")' in src, (
            "Plugin responder must accept the audio_path return shape. "
            "Real-run trace abb00832 showed OpenAI harnesses choose this "
            "shape, producing 0 agent audio when unhandled."
        )
        # Must read the file + map extension → content_type.
        assert "Harness-on-disk return shape" in src

    def test_responder_audio_content_type_set_after_successful_pcm16_encode(
        self,
    ):
        """Source grep: after _encode_pcm16_to_mp3 succeeds the responder
        sets ``ct = 'audio/mpeg'``, and after _wrap_pcm16_as_wav sets
        ``ct = 'audio/wav'``. Otherwise the downstream saver falls
        through to 'audio/wav' default which mislabels the MP3 we just
        encoded — future tooling that routes by content_type would pick
        the wrong decoder."""
        src = (
            ROOT
            / "puzzleeval"
            / "tool_plugins"
            / "voice_realtime.py"
        ).read_text(encoding="utf-8")
        assert 'ct = "audio/mpeg"' in src
        assert 'ct = "audio/wav"' in src


class TestReadBeforePatchGate:
    """Claude Code parity: FileEditTool refuses edits unless the file
    has been READ (full read, not partial) since its last modification.
    See `src/tools/FileEditTool/FileEditTool.ts:275-287`:

        const readTimestamp = toolUseContext.readFileState.get(fullFilePath)
        if (!readTimestamp || readTimestamp.isPartialView) {
            return { result: false, behavior: 'ask',
                message: 'File has not been read yet. Read it first...',
                errorCode: 6 }
        }

    We mirror this gate in `_tool_patch_file` via a per-build
    `read_state: dict[str, float]` threaded through `_dispatch_tool`.
    Forces the builder to plan patches with current file contents in
    memory — eliminates the iterative-micro-patch waste pattern
    observed in trace 28cb2648 (5 consecutive patches = $1.67 burned)."""

    def test_patch_before_read_returns_error(self, tmp_path):
        from puzzleeval.agents.implement_test_env import _tool_patch_file
        target = tmp_path / "harness.py"
        target.write_text("old content\n", encoding="utf-8")
        read_state = {}  # nothing read yet
        result = _tool_patch_file(
            {"filename": "harness.py", "old_string": "old", "new_string": "new"},
            tmp_path, read_state=read_state,
        )
        assert "has not been read" in result.lower() or "STOP:" in result, (
            f"patch_file without prior read must be refused; got: {result!r}"
        )
        # File should NOT have been modified.
        assert target.read_text(encoding="utf-8") == "old content\n"

    def test_patch_after_read_succeeds(self, tmp_path):
        from puzzleeval.agents.implement_test_env import (
            _tool_patch_file, _tool_read_file,
        )
        target = tmp_path / "harness.py"
        target.write_text("old content\n", encoding="utf-8")
        read_state = {}
        _tool_read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
        result = _tool_patch_file(
            {"filename": "harness.py", "old_string": "old", "new_string": "new"},
            tmp_path, read_state=read_state,
        )
        assert "Patched" in result, f"patch should succeed after read; got: {result!r}"
        assert target.read_text(encoding="utf-8") == "new content\n"

    def test_patch_after_write_succeeds_without_separate_read(self, tmp_path):
        """Write populates read_state — immediate same-turn patch is fine
        because the caller knows what was just written. Matches Claude
        Code's FileWriteTool behavior."""
        from puzzleeval.agents.implement_test_env import (
            _tool_patch_file, _tool_write_file,
        )
        read_state = {}
        _tool_write_file(
            {"filename": "harness.py", "content": "old content\n"},
            tmp_path, read_state=read_state,
        )
        result = _tool_patch_file(
            {"filename": "harness.py", "old_string": "old", "new_string": "new"},
            tmp_path, read_state=read_state,
        )
        assert "Patched" in result

    def test_consecutive_patches_same_turn_ok(self, tmp_path):
        """Two patches in a row to the same file in ONE turn are fine —
        the first patch updates read_state so the second doesn't trip
        the staleness check."""
        from puzzleeval.agents.implement_test_env import (
            _tool_patch_file, _tool_read_file,
        )
        target = tmp_path / "harness.py"
        target.write_text("A\nB\nC\n", encoding="utf-8")
        read_state = {}
        _tool_read_file({"filename": "harness.py"}, tmp_path, read_state=read_state)
        r1 = _tool_patch_file(
            {"filename": "harness.py", "old_string": "A", "new_string": "X"},
            tmp_path, read_state=read_state,
        )
        assert "Patched" in r1
        r2 = _tool_patch_file(
            {"filename": "harness.py", "old_string": "B", "new_string": "Y"},
            tmp_path, read_state=read_state,
        )
        assert "Patched" in r2

    def test_legacy_callers_without_read_state_unaffected(self, tmp_path):
        """When `read_state=None` (tests, direct callers), the gate does
        NOT fire — preserves backward compat for 828+ existing tests."""
        from puzzleeval.agents.implement_test_env import _tool_patch_file
        target = tmp_path / "x.py"
        target.write_text("old\n", encoding="utf-8")
        result = _tool_patch_file(
            {"filename": "x.py", "old_string": "old", "new_string": "new"},
            tmp_path,  # no read_state
        )
        assert "Patched" in result

    def test_dispatch_tool_accepts_read_state_kwarg(self):
        from puzzleeval.agents.implement_test_env import _dispatch_tool
        import inspect
        sig = inspect.signature(_dispatch_tool)
        assert "read_state" in sig.parameters


class TestSpecConformingContextManagement:
    """Anthropic docs (/build-with-claude/context-editing) declare
    `clear_tool_inputs` as a BOOLEAN (default False). An earlier
    configuration passed it as a LIST of tool names, relying on
    undefined API coercion. This suite locks in the spec-conforming
    replacement:

      - ONE consolidated `clear_tool_uses_20250919` edit
      - `clear_tool_inputs: False` (safer default: preserves tool call
        parameters, clears only result bodies)
      - `clear_at_least: {input_tokens: 10000}` — per docs, ensures
        every clearing event saves more tokens than the cache-rewrite
        cost. Without it, small clears can be net-negative with
        message-level caching.
      - `keep: {type: tool_uses, value: 3}` — explicit default; most-
        recent tool interactions preserved.
      - `exclude_tools: [write_file, patch_file, advisor]` — edit-tool
        history and strategic guidance are never cleared.
      - Separate `clear_thinking_20251015: keep: all` edit — preserves
        ALL thinking blocks to maximize cache hits (docs quote:
        "preserve all thinking blocks by setting keep: all").
    """

    def test_clear_tool_inputs_is_boolean_not_list(self):
        """Regression guard: list-shape `clear_tool_inputs` was
        undefined API behavior (silent coercion or ignore)."""
        src = _agent5_combined_source()
        idx = src.find('"clear_tool_inputs":')
        assert idx > 0, "clear_tool_inputs key missing from Agent 5 config"
        # Look at the next 60 chars to see the value
        window = src[idx:idx + 60]
        assert ('"clear_tool_inputs": False' in window
                or '"clear_tool_inputs": True' in window), (
            f"clear_tool_inputs must be a Python boolean per Anthropic "
            f"spec; found: {window!r}. The published schema declares "
            f"this as `boolean` (default false). A list shape is "
            f"undefined behavior."
        )
        # Our preferred value is False — preserves call-parameter
        # context so Claude still sees what it did even after results
        # are cleared.
        assert '"clear_tool_inputs": False' in window, (
            f"Preferred default is False (safer — keeps tool CALL "
            f"parameters visible so Claude can reason about its own "
            f"action history). Found: {window!r}."
        )

    def test_clear_at_least_guard_present(self):
        """Docs quote (/build-with-claude/context-editing):
        'Use the `clear_at_least` parameter to ensure a minimum number
        of tokens is cleared each time. You'll incur cache write costs
        each time content is cleared.'

        Without this guard, small clears fire when triggered even if
        they save less than the cache-rewrite cost. Paired with the
        message-level cache breakpoint, this is the knob that makes
        the combination net-positive."""
        src = _agent5_combined_source()
        assert '"clear_at_least":' in src, (
            "clear_at_least guard missing — context clearing could "
            "fire when savings are less than cache invalidation cost. "
            "Add `clear_at_least: {type: input_tokens, value: N}` to "
            "the clear_tool_uses edit."
        )
        # Must reference the config constant so tuning is in one place.
        assert 'CACHE_CLEAR_AT_LEAST_TOKENS' in src, (
            "clear_at_least value should come from config constant "
            "CACHE_CLEAR_AT_LEAST_TOKENS so PUZZLEEVAL_CACHE_CLEAR_AT_LEAST "
            "can tune it without a code edit."
        )

    def test_exclude_tools_preserves_edit_history(self):
        src = _agent5_combined_source()
        idx = src.find('"exclude_tools":')
        assert idx > 0
        window = src[idx:idx + 400]
        for tool in ("write_file", "patch_file", "advisor"):
            assert f'"{tool}"' in window, (
                f"{tool} must be in exclude_tools — Claude needs to "
                f"see its edit history / strategic guidance to maintain "
                f"file-state awareness across long builds."
            )

    def test_clear_thinking_keep_all_preserves_cache(self):
        """Per docs: 'When thinking blocks are kept in context (not
        cleared), the prompt cache is preserved, enabling cache hits
        and reducing input token costs. ... To maximize cache hits,
        preserve all thinking blocks by setting keep: all.'

        Without this edit, the default ('keep only last turn's
        thinking') would invalidate the message cache at every
        thinking-block boundary — defeating the message-level cache."""
        src = _agent5_combined_source()
        assert '"clear_thinking_20251015"' in src, (
            "Missing clear_thinking_20251015 edit — default thinking "
            "clearing invalidates message cache at every thinking "
            "boundary. Add an edit with keep: 'all' to preserve cache."
        )
        # Check that the edit uses keep: "all" (the cache-maximizing option)
        # Find the clear_thinking edit block and verify
        idx = src.find('"clear_thinking_20251015"')
        assert idx > 0
        window = src[idx:idx + 200]
        assert '"keep": "all"' in window, (
            f"clear_thinking_20251015 must set keep: 'all' per docs "
            f"cache-maximization recommendation. Found: {window!r}."
        )

    # Phase 8: deleted source-grep test `test_clear_thinking_edit_has_no_trigger_field`.
    # Behavior covered by: tests/test_api_call.py::TestContextManagementEdits

    def test_pipeline_runner_records_agents_into_pipeline_summary(self):
        """Real run e21f6077 exposed that `pipeline_summary.json.agents`
        was always `[]` on FastAPI backend runs — the PipelineRun
        object was created and finalized, but `save_agent_result` was
        never called, so the summary had empty agents and
        ``total_cost_usd=0`` even when ``state.total_cost_usd`` showed
        real spend. Fix: `_record_agent_cost_and_emit` now appends
        an AgentRecord alongside the budget update. Regression guard:
        the helper must include the AgentRecord append pattern.
        """
        runner_path = (
            ROOT.parent / "puzzleeval-api" / "services" / "pipeline_runner.py"
        )
        assert runner_path.exists(), (
            f"pipeline_runner.py not found at {runner_path}"
        )
        src = runner_path.read_text(encoding="utf-8")
        # The helper must import AgentRecord and append to pipeline_run.agents.
        assert (
            "pipeline_run.agents.append(AgentRecord(" in src
        ), (
            "`_record_agent_cost_and_emit` must append AgentRecord to "
            "pipeline_run.agents so pipeline_summary.json shows the "
            "per-agent cost breakdown instead of empty agents list."
        )
        # Agent 1 also needs a record — it runs in the /chat phase
        # before _record_agent_cost_and_emit fires for later agents.
        assert 'name="agent_1"' in src, (
            "Agent 1 must also be appended to pipeline_run.agents — "
            "it completes in the /chat conversation phase before the "
            "pipeline worker's _record_agent_cost_and_emit path fires."
        )

    # Phase 8: deleted source-grep test `test_clear_thinking_edit_is_first_in_edits_list`.
    # Behavior covered by: tests/test_api_call.py::TestContextManagementEdits::test_clear_thinking_is_first

    def test_consolidated_single_clear_tool_uses_edit(self):
        """Two separate `clear_tool_uses_20250919` edits with ambiguous
        composition order were collapsed to ONE correctly-typed edit.
        Count active edits to verify there's exactly one."""
        src = _agent5_combined_source()
        # Count occurrences as a dict key (inside an edit, not in a
        # comment). Match `"type": "clear_tool_uses_20250919"` which
        # is unique per edit object.
        import re
        count = len(re.findall(
            r'^\s*"type":\s*"clear_tool_uses_20250919"',
            src, re.MULTILINE,
        ))
        assert count == 1, (
            f"Agent 5 should have exactly ONE clear_tool_uses edit; "
            f"found {count}. Multiple edits of the same type have "
            f"undefined composition semantics."
        )


class TestMessageLevelPromptCaching:
    """Docs-driven: Anthropic's prompt-caching guide recommends caching
    the growing conversation prefix in multi-turn tool-use loops by
    placing `cache_control: {type: ephemeral}` on the last message's
    last content block each turn. Turn N+1 reads turn N's entire prior
    conversation at 0.1x base input cost instead of full price.

    Agent 5's builder loop is exactly this shape: APPENDS tool_results,
    assistant responses, and new user turns each iteration; never
    modifies earlier turns. Ideal cache target. Combined with the
    existing system-block cache (10.7K tokens) that's been in place
    since NEW-AC, this covers both static (system) and growing
    (messages) regions of the prompt.

    Safety: `_apply_message_cache_breakpoint` never raises on malformed
    input, strips prior markers to stay within the 4-breakpoint cap,
    and is a no-op on empty message lists. Cache miss costs exactly
    the pre-change baseline (1x input) so worst case = current cost.
    """

    def test_helper_imports_cleanly(self):
        from puzzleeval.agents.implement_test_env import (
            _apply_message_cache_breakpoint,
        )
        assert callable(_apply_message_cache_breakpoint)

    def test_empty_messages_is_noop(self):
        from puzzleeval.agents.implement_test_env import (
            _apply_message_cache_breakpoint,
        )
        out = _apply_message_cache_breakpoint([])
        assert out == []

    def test_string_content_wrapped_with_cache_control(self):
        from puzzleeval.agents.implement_test_env import (
            _apply_message_cache_breakpoint,
        )
        messages = [{"role": "user", "content": "hello"}]
        _apply_message_cache_breakpoint(messages)
        # String content must be converted to a list of text blocks so
        # cache_control can attach.
        assert isinstance(messages[0]["content"], list)
        assert messages[0]["content"][0]["type"] == "text"
        assert messages[0]["content"][0]["text"] == "hello"
        assert messages[0]["content"][0]["cache_control"] == {
            "type": "ephemeral"
        }

    def test_list_content_last_block_gets_cache_control(self):
        from puzzleeval.agents.implement_test_env import (
            _apply_message_cache_breakpoint,
        )
        messages = [{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                {"type": "text", "text": "continue"},
            ],
        }]
        _apply_message_cache_breakpoint(messages)
        # Marker lands on the LAST block only.
        assert "cache_control" not in messages[0]["content"][0]
        assert messages[0]["content"][-1]["cache_control"] == {
            "type": "ephemeral"
        }

    def test_prior_markers_stripped_to_stay_under_4_breakpoint_cap(self):
        """Over many turns we mustn't accumulate cache_control markers
        beyond the 4-per-request cap. Exactly ONE active message
        marker at any time."""
        from puzzleeval.agents.implement_test_env import (
            _apply_message_cache_breakpoint,
        )
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "turn 1",
                 "cache_control": {"type": "ephemeral"}},
            ]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "response"},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "turn 2"},
            ]},
        ]
        _apply_message_cache_breakpoint(messages)
        # Turn-1 marker stripped
        assert "cache_control" not in messages[0]["content"][0]
        # Turn-2 marker placed
        assert messages[-1]["content"][-1]["cache_control"] == {
            "type": "ephemeral"
        }
        # Count total cache_control markers across all messages
        total = sum(
            1
            for msg in messages
            if isinstance(msg.get("content"), list)
            for block in msg["content"]
            if isinstance(block, dict) and "cache_control" in block
        )
        assert total == 1, (
            f"Exactly one active message cache_control expected, "
            f"found {total}. Over-accumulation blows the "
            f"4-breakpoint-per-request cap."
        )

    def test_idempotent_repeated_calls(self):
        """Calling the helper twice in a row produces the same state."""
        from puzzleeval.agents.implement_test_env import (
            _apply_message_cache_breakpoint,
        )
        messages = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        _apply_message_cache_breakpoint(messages)
        snapshot = json.dumps(messages, sort_keys=True)
        _apply_message_cache_breakpoint(messages)
        assert json.dumps(messages, sort_keys=True) == snapshot

    def test_helper_does_not_crash_on_malformed_message(self):
        """Defensive: int / None / dict-with-no-content shouldn't raise."""
        from puzzleeval.agents.implement_test_env import (
            _apply_message_cache_breakpoint,
        )
        # None content (malformed but the helper must not raise)
        messages = [{"role": "user", "content": None}]
        _apply_message_cache_breakpoint(messages)  # no-op expected
        # int content (malformed; helper should no-op without raising)
        messages = [{"role": "user", "content": 42}]
        _apply_message_cache_breakpoint(messages)
        # list with non-dict tail block
        messages = [{"role": "user", "content": ["raw string not a block"]}]
        _apply_message_cache_breakpoint(messages)  # no-op

    def test_config_flag_exists_and_defaults_on(self):
        """Feature flag exists so a future Anthropic regression can be
        reverted with env var instead of code rollback."""
        # Re-import from env-free state
        import importlib
        import puzzleeval.config as _c
        importlib.reload(_c)
        assert hasattr(_c, "CACHE_MESSAGES_ENABLED")
        assert _c.CACHE_MESSAGES_ENABLED is True, (
            "CACHE_MESSAGES_ENABLED must default to True — the docs-"
            "verified cache savings are sizeable and the worst case "
            "(cache miss) matches pre-change cost."
        )
        assert hasattr(_c, "CACHE_CLEAR_AT_LEAST_TOKENS")
        assert _c.CACHE_CLEAR_AT_LEAST_TOKENS >= 1, (
            "CACHE_CLEAR_AT_LEAST_TOKENS must be positive — the whole "
            "point is to skip small clears that cost more in cache "
            "invalidation than they save."
        )

    def test_config_flag_respects_env_override(self, monkeypatch):
        """PUZZLEEVAL_CACHE_MESSAGES_ENABLED=0 must disable the feature
        cleanly (revert path)."""
        import importlib
        import puzzleeval.config as _c
        monkeypatch.setenv("PUZZLEEVAL_CACHE_MESSAGES_ENABLED", "0")
        importlib.reload(_c)
        assert _c.CACHE_MESSAGES_ENABLED is False
        # Restore for subsequent tests
        monkeypatch.delenv("PUZZLEEVAL_CACHE_MESSAGES_ENABLED")
        importlib.reload(_c)

    def test_agent5_builder_calls_helper_gated_by_flag(self):
        """The builder loop must gate the call behind
        CACHE_MESSAGES_ENABLED so env-flag flip is a clean revert.

        Phase 8 candidate: partially superseded by
        tests/test_api_call.py::TestMessageCacheBreakpoint
        (test_breakpoint_called_when_cache_enabled +
        test_breakpoint_skipped_when_cache_disabled). After Phase 4 Path B
        Step 1, the gate lives inside ``api_call.make_builder_api_call``;
        the call site uses the injected ``ctx.apply_message_cache_breakpoint``
        callable (not the bare ``_apply_message_cache_breakpoint(messages)``
        literal that lived in the loop pre-extraction).
        """
        src = _agent5_combined_source()
        # The injected callback's call site uses the ctx-prefixed form.
        assert "ctx.apply_message_cache_breakpoint(ctx.messages)" in src, (
            "make_builder_api_call must invoke the injected "
            "apply_message_cache_breakpoint callable on ctx.messages."
        )
        assert "CACHE_MESSAGES_ENABLED" in src, (
            "Call site must import/check CACHE_MESSAGES_ENABLED for "
            "the env-flag revert path."
        )
        # The gate must be `if CACHE_MESSAGES_ENABLED:` so flipping
        # the env var fully bypasses the helper.
        idx_call = src.find("ctx.apply_message_cache_breakpoint(ctx.messages)")
        window_before = src[max(0, idx_call - 400):idx_call]
        assert "if CACHE_MESSAGES_ENABLED:" in window_before, (
            f"Helper call must be guarded by `if CACHE_MESSAGES_ENABLED:` "
            f"for clean env-flag revert. Call-site context: {window_before!r}"
        )

    def test_total_cache_breakpoints_within_anthropic_limit(self):
        """Anthropic spec: max 4 cache_control markers per request.
        We use 2 at runtime (system + last message). No further
        breakpoints should land in the builder call without a
        corresponding test update.

        Phase 8 candidate: the system-block cache marker now lives in
        ``api_call.make_builder_api_call``; the test anchor is updated
        to walk to ``primary_model=ctx.current_model`` (Phase 4 Path B
        Step 1 prefixed loop-locals with ``ctx.``).
        """
        src = _agent5_combined_source()
        idx_marker = src.find('"type": "compact_20260112"')
        assert idx_marker > 0, (
            "compact_20260112 marker missing from Agent 5 builder"
        )
        idx_call = src.rfind("client.beta.messages.create(", 0, idx_marker)
        assert idx_call > 0
        # The call closes via the ``ctx.``-prefixed primary_model arg
        # passed to call_with_model_fallback.
        idx_end = src.find("primary_model=ctx.current_model", idx_call)
        assert idx_end > 0, (
            "make_builder_api_call must pass ctx.current_model as "
            "primary_model to call_with_model_fallback"
        )
        body = src[idx_call:idx_end]
        literal_markers = body.count(
            '"cache_control": {"type": "ephemeral"}'
        )
        # In source: exactly 1 (system block). The message-level one
        # is placed at runtime by the injected breakpoint callback.
        assert literal_markers == 1, (
            f"Expected exactly 1 cache_control marker in the builder "
            f"create() body (system block only). Found {literal_markers}."
        )


class TestConversationMergeNormalizesSampleRate:
    """Real-run trace 28cb2648: caller TTS produced 44.1 kHz MP3, agent
    PCM16 → pydub MP3 produced 24 kHz, byte-concat resulted in playback
    pausing at the first sample-rate transition (~4 seconds in). Fix:
    decode every per-turn file via pydub, resample to a uniform format
    (first segment's rate + channels), concatenate, and export. Byte-
    concat path stays as fallback when pydub/ffmpeg unavailable."""

    def test_merger_via_pydub_normalizes_to_uniform_format(self, tmp_path):
        from pydub import AudioSegment
        from pydub.generators import Sine
        from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin

        # Caller-style: 44.1 kHz mono MP3
        caller = Sine(440).to_audio_segment(duration=1000).set_frame_rate(44100).set_channels(1)
        caller_path = tmp_path / "caller_session-t0.mp3"
        caller.export(str(caller_path), format="mp3")
        # Agent-style: 24 kHz mono MP3 (mismatch)
        agent = Sine(880).to_audio_segment(duration=1500).set_frame_rate(24000).set_channels(1)
        agent_path = tmp_path / "response_session-t0_x.mp3"
        agent.export(str(agent_path), format="mp3")

        p = VoiceRealtimePlugin()
        p.set_session_dir(str(tmp_path))
        out_path = tmp_path / "conversation_session.mp3"
        ok = p._try_merge_via_pydub(
            [caller_path, agent_path], out_path, ".mp3",
        )
        assert ok, "pydub merge must succeed when pydub+ffmpeg are available"
        assert out_path.exists()
        merged = AudioSegment.from_file(str(out_path))
        # Sample rate must match first segment (caller).
        assert merged.frame_rate == 44100, (
            f"Merged file's sample rate must be normalized to first "
            f"segment's rate (44100), got {merged.frame_rate}"
        )
        # Duration must equal sum of inputs (within 100ms tolerance for
        # mp3 frame alignment).
        assert abs(len(merged) - 2500) < 200, (
            f"Merged duration {len(merged)}ms should be ~2500ms (sum of "
            f"1000 + 1500). Mismatch indicates frames were dropped or "
            f"resampling skewed timing."
        )

    def test_merger_falls_back_to_byte_concat_when_pydub_breaks(
        self, monkeypatch, tmp_path,
    ):
        """When pydub raises (ffmpeg missing, corrupt file, etc), the
        byte-concat fallback still produces *some* file rather than None."""
        from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
        # Two empty .mp3 files — pydub will refuse them, so fallback
        # path runs.
        a = tmp_path / "caller_session-t0.mp3"
        b = tmp_path / "response_session-t0_x.mp3"
        a.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        b.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)

        p = VoiceRealtimePlugin()
        p.set_session_dir(str(tmp_path))
        # Force pydub to fail.
        monkeypatch.setattr(p, "_try_merge_via_pydub", lambda *a, **k: False)
        merged = p._merge_conversation_audio(
            session_token="session_token_test",
            turns=[
                {"caller_path": str(a), "agent_path": str(b)},
            ],
        )
        assert merged is not None, (
            "Byte-concat fallback must produce SOME file even when "
            "pydub fails — degraded but not none."
        )

    def test_merger_path_grep_for_pydub_helper(self):
        """Source-grep guard: the pydub normalization path must exist
        AND be invoked from _merge_conversation_audio (not orphaned).
        Without this regression check, a future cleanup could remove
        the pydub path and silently regress to the sample-rate-mismatch
        bug."""
        src = (
            ROOT
            / "puzzleeval"
            / "tool_plugins"
            / "voice_realtime.py"
        ).read_text(encoding="utf-8")
        assert "_try_merge_via_pydub" in src
        # Must be invoked BEFORE the byte-concat block so pydub wins
        # when available.
        idx_helper = src.find("self._try_merge_via_pydub(")
        idx_byte_concat = src.find("merged_via_pydub = self._try_merge_via_pydub")
        assert idx_helper > 0


class TestDefaultInputContextFallback:
    """Real-run trace f1312253 (latest run, post-Option-A): the agent
    audio kept disappearing because:
      1. Agent 3's prompt rule says voice tests MUST emit
         `input_context.instructions` — but Agent 3 emitted `null`.
      2. Agent 5's builder prompt teaches a DEFAULT_INSTRUCTIONS
         fallback IN the harness — but the harness Agent 5 wrote
         literally contained `if not instructions: return _fail(
         'missing system prompt in input_context', t0)`.

    Both prompt-level rules failed. Every harness call returned
    success=False, the plugin saw empty audio, every test scored 0.

    Fix: inject a candidate-aware default into the runner closure
    BEFORE the harness sees the payload. The runner is OUR code; we
    can guarantee adherence. The harness's hard-fail check passes
    because we always supply instructions. Test-case input_context
    still wins when present — the default only fires on the explicit
    null path."""

    def test_default_input_context_fills_every_alias(self):
        """The default must populate EVERY system-prompt alias the
        observed harnesses accept — not just `instructions`. ElevenLabs
        + OpenAI both walk the same alias list, but a future provider
        could read any of them first."""
        from puzzleeval.agents.implement_test_env import (
            _default_input_context, _SYSTEM_PROMPT_ALIASES,
        )
        ctx = _default_input_context("OpenAI Realtime API", "voice_agent")
        assert isinstance(ctx, dict)
        for alias in _SYSTEM_PROMPT_ALIASES:
            assert alias in ctx, (
                f"Default must fill alias {alias!r} — both observed voice "
                f"harnesses (OpenAI Realtime + ElevenLabs Conversational AI) "
                f"walk this list in order."
            )
            assert "OpenAI Realtime API" in ctx[alias]
            assert "voice agent" in ctx[alias]

    def test_default_input_context_handles_missing_role(self):
        from puzzleeval.agents.implement_test_env import _default_input_context
        ctx = _default_input_context("Mindee", None)
        assert "instructions" in ctx
        assert "Mindee" in ctx["instructions"]

    def test_merge_handles_three_real_cases(self):
        """Cover the three patterns observed in real Agent 3 outputs:
        null, partial-other-keys, partial-with-system-prompt."""
        from puzzleeval.agents.implement_test_env import (
            _default_input_context, _merge_with_default_input_context,
        )
        default = _default_input_context("Acme Voice", "receptionist")
        # Case 1: Agent 3 emitted null
        out = _merge_with_default_input_context(None, default)
        assert "instructions" in out and "Acme Voice" in out["instructions"]

        # Case 2: Agent 3 emitted other keys but no system-prompt field.
        # Other keys must be preserved AND every system-prompt alias must
        # be filled from the default — otherwise the harness's
        # `_extract_system_prompt` walks the list and returns None.
        out = _merge_with_default_input_context(
            {"persona_name": "Vera", "scenario_id": "tc-001"}, default,
        )
        assert out["persona_name"] == "Vera"
        assert out["scenario_id"] == "tc-001"
        assert "Acme Voice" in out["instructions"]
        assert "Acme Voice" in out["system_prompt"]

        # Case 3: Agent 3 supplied a real system prompt under ONE alias.
        # The user value must propagate to ALL aliases so harnesses
        # checking different names see the user's intent (not stale defaults).
        out = _merge_with_default_input_context(
            {"system_prompt": "You are Vera, a senior plumbing agent."},
            default,
        )
        for alias in ("instructions", "system_prompt", "system", "brief", "agent_prompt"):
            assert "Vera" in out[alias], (
                f"Test-case value must propagate to alias {alias!r} so the "
                f"harness sees consistent intent regardless of which alias it reads."
            )
        # Default's content must be FULLY overridden by the user value.
        assert "Acme Voice" not in out["instructions"]

    def test_runner_closures_use_merge_helper(self):
        """Source grep: both runner closures (eval_strategy in {'tool_runner',
        'hybrid'} branches) must route through the merge helper, not the
        simpler 'skip if test set anything' check that misses partial cases."""
        src = (
            ROOT
            / "puzzleeval"
            / "agents"
            / "implement_test_env.py"
        ).read_text(encoding="utf-8")
        n = src.count('_merge_with_default_input_context(')
        # Helper definition (1) + two closure call sites (2) = 3.
        assert n >= 3, (
            f"Expected the merge helper invoked in both runner closures "
            f"(plus its definition); found {n} occurrences. Runner-level "
            f"merge is what makes the fix general — not the simpler "
            f"presence check."
        )


class TestModalityContractInjection:
    """Option A from the contract-formalization conversation: each
    modality declares its own harness return-shape contract; the right
    one gets injected into the builder system prompt at render time
    based on the candidate's test-case modalities. Voice tests get the
    voice contract; non-voice tests get an empty string so the prompt
    stays lean. This is the structural predecessor of a per-modality
    skills-style loader (see CLAUDE.md AD-002 revisit trigger).

    Real-run trace abb00832: Agent 5 chose to return audio via
    `raw_response.audio_path` (file path) — the plugin only knew about
    `audio_bytes` (b64 string) — silent fall-through, 0 agent audio.
    The voice contract enumerates BOTH valid shapes so the builder
    can't accidentally pick a third."""

    def test_voice_contract_lists_both_supported_shapes(self):
        from puzzleeval.agents.implement_test_env import _VOICE_HARNESS_CONTRACT
        # Both shape names must appear so Claude knows the menu.
        assert "audio_bytes" in _VOICE_HARNESS_CONTRACT
        assert "audio_path" in _VOICE_HARNESS_CONTRACT
        # Hard rule numbering — uniquely identifies the section so future
        # rewrites can't accidentally drop the rule list.
        assert "HARD RULES" in _VOICE_HARNESS_CONTRACT
        # The forbidden-keys callout must enumerate the alternatives we've
        # seen Claude invent so the next harness author doesn't try them.
        for forbidden in ("audio_url", "audio_data"):
            assert forbidden in _VOICE_HARNESS_CONTRACT, (
                f"Contract must explicitly forbid {forbidden} — listed "
                f"as a known anti-pattern in the contract docstring."
            )

    def test_voice_modality_test_cases_inject_contract(self):
        """When test cases involve voice / conversation / audio
        modalities, the renderer must inject the contract."""
        from puzzleeval.agents.implement_test_env import (
            BUILDER_SYSTEM_PROMPT, _render_builder_prompt,
        )
        voice_tests = [
            {"input_type": "voice_conversation", "output_type": "voice_conversation"},
        ]
        rendered = _render_builder_prompt(BUILDER_SYSTEM_PROMPT, test_cases=voice_tests)
        assert "Voice harness return-shape contract" in rendered
        assert "audio_bytes" in rendered
        assert "audio_path" in rendered
        # Placeholder is fully replaced.
        assert "__MODALITY_CONTRACT__" not in rendered

    def test_non_voice_modality_skips_contract(self):
        """OCR / code-gen / chat-text / generic tests do NOT need the
        voice contract — and shouldn't pay the prompt-token tax for it."""
        from puzzleeval.agents.implement_test_env import (
            BUILDER_SYSTEM_PROMPT, _render_builder_prompt,
        )
        for non_voice in (
            [{"input_type": "document_content", "output_type": "structured_json"}],
            [{"input_type": "text", "output_type": "code"}],
            [{"input_type": "text", "output_type": "free_text"}],
        ):
            rendered = _render_builder_prompt(BUILDER_SYSTEM_PROMPT, test_cases=non_voice)
            assert "Voice harness return-shape contract" not in rendered, (
                f"Non-voice test {non_voice} must NOT inject the voice "
                f"contract — wastes ~600 tokens per builder turn."
            )
            # Placeholder still cleanly stripped to empty.
            assert "__MODALITY_CONTRACT__" not in rendered

    def test_no_test_cases_skips_contract(self):
        """Empty / None test_cases (defensive case) must not crash and
        must produce an empty modality block."""
        from puzzleeval.agents.implement_test_env import (
            BUILDER_SYSTEM_PROMPT, _render_builder_prompt,
        )
        for empty in (None, [], ()):
            rendered = _render_builder_prompt(BUILDER_SYSTEM_PROMPT, test_cases=empty)
            assert "__MODALITY_CONTRACT__" not in rendered
            assert "Voice harness return-shape contract" not in rendered

    def test_modality_contract_is_independent_of_os_rules(self):
        """The modality contract and the OS-specific rules are two
        independent injection points — voice tests on Windows should
        get BOTH; voice tests on Linux should get the voice contract +
        Linux POSIX block; OCR tests on Windows should get the Windows
        block but no voice contract. This composability is what makes
        the layout migratable to skills-files later."""
        from puzzleeval.agents.implement_test_env import (
            BUILDER_SYSTEM_PROMPT, _render_builder_prompt,
        )
        import unittest.mock
        voice_tests = [{"input_type": "voice_turn", "output_type": "audio_content"}]
        with unittest.mock.patch("sys.platform", "win32"):
            r = _render_builder_prompt(BUILDER_SYSTEM_PROMPT, test_cases=voice_tests)
            assert "Voice harness return-shape contract" in r
            assert "Windows" in r
        with unittest.mock.patch("sys.platform", "linux"):
            r = _render_builder_prompt(BUILDER_SYSTEM_PROMPT, test_cases=voice_tests)
            assert "Voice harness return-shape contract" in r
            assert "POSIX" in r
        with unittest.mock.patch("sys.platform", "win32"):
            r = _render_builder_prompt(
                BUILDER_SYSTEM_PROMPT,
                test_cases=[{"input_type": "document_content", "output_type": "structured_json"}],
            )
            assert "Voice harness return-shape contract" not in r
            assert "Windows" in r


class TestNoContainerThreadingAfterRevert:
    """We reverted the 20260209 web-tools upgrade after real-run traces
    (d3b49875 + 4068e872 + 5a59acbc) showed the auto-injected
    code_execution sandbox introduced multiple operational failures:
    400 container_id cascades across every sub-agent, 3-5 min sandbox
    spin-up latency on Agent 2, silent hangs on non-beta
    messages.create when the `container` kwarg is passed. The basic
    20250910 / 20250305 web tools don't create a sandbox — no container
    threading needed anywhere. These tests lock in the revert so a
    future 'let's re-upgrade' pass doesn't silently reintroduce the
    container_id failure surface without intentional planning."""

    def test_no_container_kwarg_forwarding_anywhere(self):
        """After the revert, no agent forwards a `container` kwarg."""
        roots = [
            ROOT / "puzzleeval" / "agents" / "implement_test_env.py",
            ROOT / "puzzleeval" / "agents" / "agent4" / "core.py",
            ROOT / "puzzleeval" / "agents" / "agent2" / "core.py",
        ]
        for p in roots:
            src = p.read_text(encoding="utf-8")
            assert '_kwargs_builder["container"]' not in src, p.name
            assert '_kwargs_verify["container"]' not in src, p.name
            assert '_kwargs_research["container"]' not in src, p.name
            assert '_kwargs_research_sub["container"]' not in src, p.name

    def test_web_tool_versions_are_basic_not_20260209(self):
        """Revert guard: if this fails because someone switched back to
        20260209, read research.py's top-of-file comment FIRST and be
        prepared to re-thread container_id through EVERY sub-agent
        (builder, Agent 4 verify, ask_research, Agent 2 — the last
        requires moving to beta.messages.create first)."""
        for agent_file in (
            "implement_test_env.py", "screening.py", "research.py",
        ):
            src = (
                ROOT / "puzzleeval" / "agents" / agent_file
            ).read_text(encoding="utf-8")
            # Check the ACTIVE tool defs — not historical comments.
            # We grep for the literal `"type": "web_fetch_20260209"` pattern
            # that only appears in active code.
            assert '"type": "web_fetch_20260209"' not in src, (
                f"{agent_file} uses web_fetch_20260209 — see research.py "
                f"top-of-file comment for why we reverted."
            )
            assert '"type": "web_search_20260209"' not in src, agent_file


class TestExplicitCandidateRelevanceBoost:
    """Real-run trace d3b49875: user said 'Compare OpenAI and ElevenLabs
    voice stacks', Agent 1 correctly captured explicit_candidates=
    ['OpenAI', 'ElevenLabs'], Agent 2 found both — but scored them
    #6 (0.61) and #7 (0.445) because adoption_difficulty=easy packaged
    products outranked them for a non-technical user. Phase 7's
    default_picks showed ServiceAgent/iVAI/AI Front Desk. User had
    to manually override the selection to test what they asked for.

    Fix: inject_explicit_candidates now also BOOSTS relevance_score
    to ≥0.95 on already-present candidates whose name substring-matches
    an explicit_candidates entry, not just injects new synthetic ones."""

    def test_explicit_boosts_existing_candidate_relevance(self):
        from puzzleeval.agents.research import inject_explicit_candidates
        from puzzleeval.schemas import Agent2Result, Candidate

        # Simulate the trace d3b49875 state: OpenAI + ElevenLabs found
        # naturally by web search but scored low.
        found = Agent2Result(
            candidates=[
                Candidate(
                    name="ServiceAgent", provider="ServiceAgent Inc",
                    description="Packaged voice agent",
                    api_available=True, pricing_model="monthly",
                    claimed_capabilities=["voice"],
                    relevance_score=0.84, adoption_difficulty="easy",
                    relevant_subtasks=[], source="web_search",
                    covers_step_ids=["step_1"],
                    coverage_confidence={"step_1": "claimed"},
                ),
                Candidate(
                    name="ElevenLabs Conversational AI",
                    provider="ElevenLabs",
                    description="Developer TTS primitive",
                    api_available=True, pricing_model="usage-based",
                    claimed_capabilities=["voice"],
                    relevance_score=0.61, adoption_difficulty="medium",
                    relevant_subtasks=[], source="web_search",
                    covers_step_ids=["step_1"],
                    coverage_confidence={"step_1": "claimed"},
                ),
                Candidate(
                    name="OpenAI Realtime API", provider="OpenAI",
                    description="Developer voice primitive",
                    api_available=True, pricing_model="per-token",
                    claimed_capabilities=["voice"],
                    relevance_score=0.445, adoption_difficulty="medium",
                    relevant_subtasks=[], source="web_search",
                    covers_step_ids=["step_1"],
                    coverage_confidence={"step_1": "claimed"},
                ),
            ],
            search_approach="dual", coverage_notes="", cost_usd=0.3,
        )
        result = inject_explicit_candidates(
            found, ["OpenAI", "ElevenLabs"], blueprint_step_ids=["step_1"],
        )
        by_name = {c.name: c for c in result.candidates}
        # Boosted to at least 0.95 — ensures Phase 7 default_picks surfaces
        # them above the 0.84 packaged products.
        assert by_name["OpenAI Realtime API"].relevance_score >= 0.95, (
            "Explicit 'OpenAI' must boost OpenAI Realtime API above 0.95 "
            "so it lands in default_picks."
        )
        assert (
            by_name["ElevenLabs Conversational AI"].relevance_score >= 0.95
        ), "Explicit 'ElevenLabs' must boost ElevenLabs above 0.95."
        # Non-explicit candidate untouched.
        assert by_name["ServiceAgent"].relevance_score == 0.84

    def test_pipeline_saves_boosted_state_even_without_new_candidates(self):
        """Real-run trace 4068e872: user said 'compare OpenAI and
        ElevenLabs voice stacks', Agent 2 found both naturally at
        relevance 0.35 + 0.63. inject_explicit_candidates BOOSTED them
        to 0.95 in memory — but pipeline_runner only saved state when
        NEW candidates were added (len increased). The boost was
        silently discarded, default_picks chose the top packaged
        products, user had to manually override. Fix: save state on
        ANY change (new candidates OR changed relevance_scores)."""
        src = (
            ROOT.parent / "puzzleeval-api" / "services" / "pipeline_runner.py"
        ).read_text(encoding="utf-8")
        # The condition must check EITHER new candidates OR boosted scores.
        assert "boosted_names" in src, (
            "pipeline_runner must track boosted_names to detect the "
            "score-only change case. Otherwise explicit-candidate boost "
            "silently drops."
        )
        # The save block must be gated on a broader condition than
        # just `added > 0`.
        assert "added > 0 or boosted_names" in src, (
            "Save condition must allow score-only changes. Without "
            "this, inject_explicit_candidates's boost is discarded."
        )

    def test_explicit_already_high_score_unchanged(self):
        """Idempotence: if a candidate already scores ≥0.95, we don't
        mess with it."""
        from puzzleeval.agents.research import inject_explicit_candidates
        from puzzleeval.schemas import Agent2Result, Candidate

        found = Agent2Result(
            candidates=[
                Candidate(
                    name="OpenAI", provider="OpenAI",
                    description="x", api_available=True,
                    pricing_model="per-token",
                    claimed_capabilities=[],
                    relevance_score=0.97, adoption_difficulty="easy",
                    relevant_subtasks=[], source="web_search",
                    covers_step_ids=["step_1"],
                    coverage_confidence={"step_1": "claimed"},
                ),
            ],
            search_approach="x", coverage_notes="", cost_usd=0.0,
        )
        result = inject_explicit_candidates(
            found, ["OpenAI"], blueprint_step_ids=["step_1"],
        )
        assert result.candidates[0].relevance_score == 0.97


class TestEfficiencyHardening_RealRun_d3b49875:
    """Real-run trace d3b49875 (OpenAI Realtime, 17 turns, $4.17):
    ~$1.36 wasted on Unix-on-Windows command failures, $1.04 wasted
    on sequential file writes that could have been parallel, $0.55
    wasted on inline `python -c` retries after Windows subprocess
    output issues. Total avoidable: ~$2.44 / 59% of build cost.

    Each fix below targets one waste class via a prompt/venv change
    that cannot regress silently."""

    def test_builder_prompt_has_windows_command_translation_table(self):
        """Fix A: the Windows-specific translation table exists and is
        injected when the host OS is Windows. Linux/macOS runs get their
        own (shorter) blocks — see test_os_specific_rules_*.

        After the Phase 1 contract migration, the Windows rules live in
        capability_playbooks/platform_windows.md. The legacy constant
        _OS_RULES_WINDOWS still exists in implement_test_env.py as a
        one-line loader; the body lives in the markdown file.
        """
        src = _agent5_combined_source()
        assert "_OS_RULES_WINDOWS" in src, (
            "Builder must keep the Windows-specific rules constant — "
            "without it, Windows runs regress to ~$1.36 wasted on "
            "Unix muscle-memory commands."
        )
        # The specific commands the real trace wasted turns on must be
        # in the Windows playbook content (now in markdown).
        playbook_src = (
            ROOT / "puzzleeval" / "capability_playbooks" / "platform_windows.md"
        ).read_text(encoding="utf-8")
        for cmd in ("`tail", "`head", "`grep", "findstr"):
            assert cmd in playbook_src, (
                f"Windows translation table must mention {cmd} — "
                f"real-run trace showed it's a common failure"
            )

    def test_builder_prompt_has_no_python_dash_c_retry_rule(self):
        """Fix C: after a `python -c` command produces no visible
        output on Windows, the agent must NOT retry with another
        `python -c`. Pivot to write_file + python script.py instead.

        The rule lives in the Windows-specific block (silent output is
        a Windows-specific subprocess-capture race)."""
        src = _agent5_combined_source()
        assert "silent-output trap" in src or (
            "python -c" in src and "DO NOT" in src
        ), (
            "Builder prompt must forbid re-trying `python -c` "
            "inline commands after a Windows silent-output failure."
        )

    def test_os_specific_rules_are_conditional_not_unconditional(self):
        """Cross-OS scaling: the Windows translation table MUST NOT be
        injected on Linux/macOS — it's wasted context + potentially
        confusing (e.g., teaching `findstr` on Linux where it doesn't
        exist).

        Phase 1.B + 1.C: the dual placeholders __OS_SPECIFIC_RULES__ +
        __MODALITY_CONTRACT__ collapsed into a single trailing
        __CONTRACT_BLOCK__. The unified contract loader
        (puzzleeval.contracts.compose_contract_block) selects platform
        contracts via always-on selection on platform predicate.

        The behavior contract (Windows runs include Windows-specific
        guidance; Linux runs don't) is preserved — it now flows through
        the unified contract block instead of a dedicated placeholder.
        """
        import puzzleeval.agents.implement_test_env as m
        # The unified placeholder is the canonical way to inject
        # platform + modality content.
        assert "__CONTRACT_BLOCK__" in m.BUILDER_SYSTEM_PROMPT, (
            "Builder prompt must use the __CONTRACT_BLOCK__ placeholder "
            "so platform + modality contracts are conditional."
        )
        # Rendering on Windows includes the Windows block.
        import unittest.mock
        with unittest.mock.patch("sys.platform", "win32"):
            rendered = m._render_builder_prompt(m.BUILDER_SYSTEM_PROMPT)
            assert "Windows translation" in rendered or "findstr" in rendered
            assert "OS: Windows" in rendered
        # Rendering on Linux includes the Linux block, NOT the Windows one.
        with unittest.mock.patch("sys.platform", "linux"):
            rendered = m._render_builder_prompt(m.BUILDER_SYSTEM_PROMPT)
            assert "findstr" not in rendered, (
                "Linux runs MUST NOT carry Windows translation table — "
                "that's wasted context + misleading (findstr doesn't exist)."
            )
            assert "OS: Linux" in rendered
            assert "POSIX" in rendered, (
                "Linux block must at least acknowledge POSIX shell "
                "conventions are available."
            )
        # Rendering on macOS includes the BSD-specific hints.
        with unittest.mock.patch("sys.platform", "darwin"):
            rendered = m._render_builder_prompt(m.BUILDER_SYSTEM_PROMPT)
            assert "OS: macOS" in rendered
            assert ("sed -i" in rendered) or ("BSD" in rendered), (
                "macOS block must warn about BSD vs GNU flag differences."
            )
            # Windows-specific text must not leak in.
            assert "findstr" not in rendered

    def test_os_specific_rules_unknown_platform_safe(self):
        """Unknown platforms (FreeBSD, AIX, etc.) render with empty
        contract block but still produce a valid prompt.

        Phase 1.B + 1.C: with no platform_freebsd.md contract registered,
        the unified __CONTRACT_BLOCK__ comes back empty for unknown
        platforms — the rendered prompt has no platform-specific guidance
        but is otherwise complete and valid.
        """
        import puzzleeval.agents.implement_test_env as m
        import unittest.mock
        with unittest.mock.patch("sys.platform", "freebsd14"):
            rendered = m._render_builder_prompt(m.BUILDER_SYSTEM_PROMPT)
            # Placeholder must be cleanly stripped, not left dangling.
            assert "__CONTRACT_BLOCK__" not in rendered
            assert "__OS_SPECIFIC_RULES__" not in rendered
            assert "__MODALITY_CONTRACT__" not in rendered
            # Reports the actual platform name honestly so Claude can
            # reason about it.
            assert "OS: freebsd14" in rendered

    def test_builder_prompt_has_env_check_consolidation_pattern(self):
        """Fix D: agents must consolidate multi-package probes into
        a single env_check.py file, not one `python -c "import X"`
        command per package."""
        src = _builder_prompt_text()
        assert "env_check.py" in src, (
            "Builder prompt must explicitly teach the env_check.py "
            "consolidation pattern (write once, run once instead of "
            "5 separate import probes)."
        )
        assert "Consolidated env probe" in src

    def test_builder_prompt_has_parallel_write_rule(self):
        """Fix B: when multiple file contents are decided, emit all
        write_file tool_use blocks in ONE assistant response."""
        src = _builder_prompt_text()
        assert (
            "Parallel tool calls" in src and "write_file" in src
        ), (
            "Builder prompt must teach the parallel-write pattern — "
            "sequential writes cost $0.30+ each in Opus input replay."
        )

    def test_venv_preinstall_manifest_exists(self):
        """Fix E: common deps (requests, websocket-client, pydub,
        soundfile, numpy) must be pre-installed into every fresh venv."""
        from puzzleeval.agents.implement_test_env import (
            VENV_PREINSTALL_MANIFEST,
        )
        # The set chosen from trace d3b49875's 2-3 redundant pip
        # install turns on audio/WS harnesses.
        pkg_prefixes = {p.split(">=")[0].split("==")[0] for p in VENV_PREINSTALL_MANIFEST}
        for required in ("requests", "websocket-client", "pydub",
                          "soundfile", "numpy"):
            assert required in pkg_prefixes, (
                f"VENV_PREINSTALL_MANIFEST must include {required} — "
                f"a core dep the real-run Agent 5 wasted turns on."
            )

    def test_venv_preinstall_toggleable_via_env(self):
        """Fix E: operators must be able to disable pre-install for
        debug / minimal-venv runs via PUZZLEEVAL_VENV_PREINSTALL=0.

        After Phase 2, the venv pre-install logic moved to
        puzzleeval/agents/agent5/sandbox.py. The env var gate is checked
        in create_venv() there.
        """
        src = (
            ROOT / "puzzleeval" / "agents" / "agent5" / "sandbox.py"
        ).read_text(encoding="utf-8")
        assert 'PUZZLEEVAL_VENV_PREINSTALL' in src, (
            "Pre-install must be env-var gated for debug runs."
        )

    def test_builder_prompt_advertises_preinstalled_packages(self):
        """The builder must KNOW the pre-installed packages are
        available — otherwise it'll still probe them. Prompt must
        list them explicitly under 'pre-installed'."""
        src = _builder_prompt_text()
        assert "pre-installed" in src, (
            "Builder prompt must advertise the pre-installed package "
            "list so the agent doesn't redundantly verify them."
        )
        # Every manifest package should be named in the prompt so the
        # agent finds the exact `import X` it's about to run.
        for pkg in ("requests", "websocket", "pydub", "soundfile",
                     "numpy", "dotenv"):
            assert pkg in src, (
                f"Builder prompt must mention pre-installed {pkg}"
            )


# ==========================================================================
# 2026-04-21 Observability audit fixes — cost + cache + telemetry
# ==========================================================================
# Background: a forensic audit of the cost / token / cache observability
# surface found three real accounting bugs that under-reported actual
# spend. Every bug had the same shape — a fallback path or missing field
# caused real LLM spend to be silently dropped from reports. Fixes below
# are in-place corrections with regression guards.


class TestCalculateCallCostFallbackIncludesCache:
    """OBSERVABILITY BUG #1: when Anthropic's response.usage.iterations[]
    array is absent (older API versions, non-beta endpoints, or edge
    cases), `_calculate_call_cost` previously fell back to counting
    ONLY input_tokens and output_tokens — silently dropping the top-level
    cache_creation_input_tokens and cache_read_input_tokens which ARE
    available on every response. On cached builds (every turn after the
    first), this under-reported cost by 5-40%.

    The fallback now applies the SAME multipliers as the iterations-
    path: 1.25x for cache write, 0.1x for cache read.
    """

    def _mock_usage(self, input_tokens, output_tokens,
                    cache_create=0, cache_read=0, iterations=None,
                    web_searches=0):
        """Build a minimal usage-like object matching the SDK's shape."""
        from types import SimpleNamespace

        server_tool_use = None
        if web_searches:
            server_tool_use = SimpleNamespace(
                web_search_requests=web_searches)

        return SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_create,
            cache_read_input_tokens=cache_read,
            iterations=iterations,
            server_tool_use=server_tool_use,
        )

    def test_fallback_includes_cache_creation_at_1_25x(self):
        """When iterations is None and cache_create > 0, the cost must
        include cache_create * price * 1.25."""
        from types import SimpleNamespace
        from puzzleeval.agents.implement_test_env import _calculate_call_cost
        usage = self._mock_usage(
            input_tokens=1000, output_tokens=500, cache_create=10000,
        )
        response = SimpleNamespace(usage=usage)
        cost = _calculate_call_cost(response, "claude-sonnet-4-6")
        # Sonnet: input = $3/MTok, output = $15/MTok
        # Expected: 1000*3e-6 + 500*15e-6 + 10000*3e-6*1.25
        #         = 0.003 + 0.0075 + 0.0375 = 0.048
        assert cost == pytest.approx(0.048, rel=1e-3), (
            f"Fallback must include cache_create at 1.25x. "
            f"Expected ~0.048, got {cost}. Missing cache multiplier."
        )

    def test_fallback_includes_cache_read_at_0_1x(self):
        """When iterations is None and cache_read > 0, the cost must
        include cache_read * price * 0.1."""
        from types import SimpleNamespace
        from puzzleeval.agents.implement_test_env import _calculate_call_cost
        usage = self._mock_usage(
            input_tokens=1000, output_tokens=500, cache_read=50000,
        )
        response = SimpleNamespace(usage=usage)
        cost = _calculate_call_cost(response, "claude-sonnet-4-6")
        # Expected: 1000*3e-6 + 500*15e-6 + 50000*3e-6*0.1
        #         = 0.003 + 0.0075 + 0.015 = 0.0255
        assert cost == pytest.approx(0.0255, rel=1e-3), (
            f"Fallback must include cache_read at 0.1x. "
            f"Expected ~0.0255, got {cost}."
        )

    def test_fallback_includes_web_search_cost(self):
        """When iterations is None and server_tool_use reports web
        searches, the fallback must add $0.01 per search."""
        from types import SimpleNamespace
        from puzzleeval.agents.implement_test_env import _calculate_call_cost
        usage = self._mock_usage(
            input_tokens=1000, output_tokens=500, web_searches=3,
        )
        response = SimpleNamespace(usage=usage)
        cost = _calculate_call_cost(response, "claude-sonnet-4-6")
        # Expected: 1000*3e-6 + 500*15e-6 + 3*0.01
        #         = 0.003 + 0.0075 + 0.03 = 0.0405
        assert cost == pytest.approx(0.0405, rel=1e-3)

    def test_fallback_empty_usage_costs_zero(self):
        """When iterations is None and all tokens are zero, cost is 0."""
        from types import SimpleNamespace
        from puzzleeval.agents.implement_test_env import _calculate_call_cost
        usage = self._mock_usage(input_tokens=0, output_tokens=0)
        response = SimpleNamespace(usage=usage)
        assert _calculate_call_cost(response, "claude-sonnet-4-6") == 0.0

    def test_iterations_path_still_used_when_present(self):
        """Regression guard: the iterations[] path (correct pre-fix)
        must still be used when the array is non-empty. Fallback is
        ONLY for the absent-array case."""
        from types import SimpleNamespace
        from puzzleeval.agents.implement_test_env import _calculate_call_cost
        iterations = [SimpleNamespace(
            type="message",
            input_tokens=500,
            output_tokens=100,
            cache_creation_input_tokens=1000,
            cache_read_input_tokens=250,
        )]
        usage = self._mock_usage(
            # Top-level values that should NOT be used because iterations present.
            input_tokens=99999,
            output_tokens=99999,
            cache_create=99999,
            cache_read=99999,
            iterations=iterations,
        )
        response = SimpleNamespace(usage=usage)
        cost = _calculate_call_cost(response, "claude-sonnet-4-6")
        # Iterations only: 500*3e-6 + 100*15e-6 + 1000*3e-6*1.25 + 250*3e-6*0.1
        #                = 0.0015 + 0.0015 + 0.00375 + 0.000075 = 0.006825
        assert cost == pytest.approx(0.006825, rel=1e-2)
        # Top-level values (99999) would have yielded a cost ~1000x bigger.
        assert cost < 1.0, (
            "Iterations-path must NOT sum top-level tokens. "
            "If it did, cost would be ~$1+ from the fake 99999 values."
        )


class TestAdvisorModelFallbackNotHardcoded:
    """OBSERVABILITY BUG #3: the advisor-iteration rate lookup previously
    defaulted to the literal "claude-opus-4-7" when the iteration
    didn't report its model. This would silently mis-price a future
    advisor model. Now uses the executor model as the fallback —
    advisor typically runs same-or-stronger model as the caller, so
    executor-price is a safer floor."""

    def test_advisor_model_defaults_to_executor_model(self):
        """When advisor iteration has no `model` field, use the
        executor model as the fallback (NOT hardcoded opus-4-7).

        After the telemetry consolidation (Phase 0), cost calculation
        moved from puzzleeval/agents/agent5/costing.py to the canonical
        puzzleeval/telemetry/cost.py. The agent5 path is now a shim.
        Source-grep follows the canonical location.
        """
        src = Path(
            "puzzleeval/telemetry/cost.py"
        ).resolve().read_text(encoding="utf-8")
        # The specific hardcoded default must be gone from active code.
        # A historical comment referencing it is acceptable.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Active code line — check for the banned pattern
            assert 'getattr(iteration, "model", "claude-opus-4-7")' not in line, (
                f"Hardcoded `claude-opus-4-7` advisor fallback must be "
                f"replaced with executor-model fallback. Found: {line!r}"
            )
        # The new pattern should be present
        assert 'getattr(iteration, "model", None) or model' in src, (
            "Expected `getattr(iteration, 'model', None) or model` "
            "pattern in advisor iteration rate selection — safer "
            "fallback than a hardcoded version."
        )


class TestFailedHarnessPreservesBuildCost:
    """OBSERVABILITY BUG #2: a build that failed mid-way (rate-limit
    retries exhausted, bad request, API errors) previously lost all
    its accumulated cost. A 10-turn rate-limited ElevenLabs-style
    build could burn $5-8 and report $0 in the run total. Now
    FailedHarness carries a `build_cost_usd` field and every
    mid-build FailedHarness return site passes the running
    `accumulated_cost`. Run-level aggregator sums it alongside
    TestHarness costs."""

    def test_failed_harness_schema_has_build_cost_usd_field(self):
        from puzzleeval.schemas import FailedHarness
        fields = FailedHarness.model_fields
        assert "build_cost_usd" in fields, (
            "FailedHarness must have a `build_cost_usd` field so "
            "partial-failure costs can be reported in run totals."
        )
        # Must default to 0.0 — pre-accumulation failures (venv setup,
        # missing credentials) legitimately have no cost.
        assert fields["build_cost_usd"].default == 0.0

    def test_failed_harness_accepts_partial_cost(self):
        """Construction with a partial cost must round-trip through
        Pydantic cleanly."""
        from puzzleeval.schemas import FailedHarness
        fh = FailedHarness(
            candidate_name="Test",
            provider="TestCo",
            failure_reason="rate limit exhausted after 3 retries",
            failure_category="build_timeout",
            turns_attempted=12,
            build_cost_usd=5.2341,
        )
        assert fh.build_cost_usd == 5.2341

    def test_in_loop_failure_sites_pass_accumulated_cost(self):
        """Every FailedHarness return site INSIDE the build loop OR the
        api_call helper (after accumulated_cost is initialized) must
        pass ``build_cost_usd=round(<accumulated_cost-source>, 4)``.

        Sites outside the loop (venv-failure, crash-handler) correctly
        omit it because accumulated_cost doesn't exist in their scope.

        Phase 8 candidate: partially superseded by
        tests/test_api_call.py::TestPtlRecovery + TestRateLimitRetry +
        TestApiConnectionAndStatusErrors (each verifies that
        APICallFailure carries the accumulated_cost). After Phase 4
        Path B Step 1, the 3 in-loop FailedHarness sites moved to
        api_call.py and use ``ctx.accumulated_cost`` (with the ``ctx.``
        prefix). The remaining sites in implement_test_env.py keep the
        bare ``accumulated_cost`` form.

        Expected combined-source count: 7 total (3 ctx-prefixed in
        api_call + 4 bare in implement_test_env: 3 in-loop sites that
        wrapped the loop's outer logic + 1 TestHarness success path).
        """
        src = _agent5_combined_source()
        import re
        # Both forms count: bare ``accumulated_cost`` in impl, and
        # ``ctx.accumulated_cost`` in api_call.
        bare = len(re.findall(
            r"build_cost_usd=round\(accumulated_cost", src
        ))
        ctx_form = len(re.findall(
            r"build_cost_usd=round\(ctx\.accumulated_cost", src
        ))
        total = bare + ctx_form
        assert total >= 7, (
            f"Expected at least 7 `build_cost_usd=round(<accumulated_cost>)` "
            f"sites across implement_test_env.py + api_call.py; "
            f"found bare={bare} ctx.={ctx_form} total={total}. "
            f"If a new in-loop failure site is added, it must also pass "
            f"the accumulated cost or the run total under-reports spend."
        )
        # FailedHarness specifically: at least 6 sites carry accumulated_cost.
        fh_count = 0
        for m in re.finditer(r"FailedHarness\(", src):
            window = src[m.start():m.start() + 1000]
            if ("build_cost_usd=round(accumulated_cost" in window
                or "build_cost_usd=round(ctx.accumulated_cost" in window):
                fh_count += 1
        assert fh_count >= 6, (
            f"Expected at least 6 FailedHarness constructions carrying "
            f"accumulated_cost; found {fh_count}."
        )

    def test_run_aggregator_sums_failed_harness_costs(self):
        """The run-level total_cost aggregator must sum
        FailedHarness.build_cost_usd alongside TestHarness costs.
        Regression guard: check for the specific pattern added by
        the 2026-04-21 fix."""
        src = Path(
            "puzzleeval/agents/implement_test_env.py"
        ).resolve().read_text(encoding="utf-8")
        # Two aggregation sites (primary + fallback). Both must add
        # failed-harness build_cost_usd.
        assert (
            'getattr(result, "build_cost_usd", 0.0) or 0.0' in src
            and 'getattr(fb_result, "build_cost_usd", 0.0) or 0.0' in src
        ), (
            "Run aggregator must sum failed-harness costs at BOTH "
            "the primary ThreadPoolExecutor loop AND the fallback "
            "loop. Without these, partial-failure costs stay "
            "invisible in the run total."
        )


class TestConversationSummaryTelemetry:
    """2026-04-21 observability feature: emit a
    `conversation_summary.json` alongside `conversation_log.json` for
    every candidate build. Provides per-candidate cache hit rate, cost
    breakdown, per-model spend, and top-3 costliest turns — without
    mutating the turn-by-turn conversation_log.json schema (downstream
    readers stay unaffected).
    """

    def test_summary_helper_imports(self):
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        assert callable(_compute_conversation_summary)

    def test_summary_aggregates_tokens_and_cost(self):
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        # Mimic two real turns (as stored in conversation_log.json).
        conversation_log = [
            {
                "turn": 0, "model": "claude-sonnet-4-6",
                "input_tokens": 6000, "output_tokens": 1000,
                "cache_read_tokens": 37000, "cache_create_tokens": 50000,
                "cost_usd": 0.41, "stop_reason": "tool_use",
            },
            {
                "turn": 1, "model": "claude-opus-4-7",
                "input_tokens": 54000, "output_tokens": 4000,
                "cache_read_tokens": 25000, "cache_create_tokens": 80000,
                "cost_usd": 1.45, "stop_reason": "tool_use",
            },
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        agg = s["aggregate"]
        assert s["total_turns"] == 2
        assert agg["input_tokens"] == 60000
        assert agg["cache_read_tokens"] == 62000
        assert agg["cache_create_tokens"] == 130000
        assert agg["output_tokens"] == 5000
        assert agg["cost_usd"] == pytest.approx(1.86, rel=1e-2)
        assert agg["total_billed_input"] == 60000 + 62000 + 130000
        # Percentage breakdown sums to ~100%
        assert agg["fresh_input_pct"] + agg["cache_read_pct"] + agg["cache_write_pct"] == pytest.approx(
            100.0, abs=0.1
        )

    def test_summary_has_per_model_breakdown(self):
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        conversation_log = [
            {"turn": 0, "model": "claude-sonnet-4-6",
             "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.01},
            {"turn": 1, "model": "claude-opus-4-7",
             "input_tokens": 200, "output_tokens": 20,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.05},
            {"turn": 2, "model": "claude-opus-4-7",
             "input_tokens": 300, "output_tokens": 30,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.08},
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        pm = s["per_model"]
        assert set(pm.keys()) == {"claude-sonnet-4-6", "claude-opus-4-7"}
        assert pm["claude-opus-4-7"]["turns"] == 2
        assert pm["claude-opus-4-7"]["input_tokens"] == 500
        assert pm["claude-opus-4-7"]["cost_usd"] == pytest.approx(0.13, rel=1e-2)
        assert pm["claude-sonnet-4-6"]["turns"] == 1

    def test_summary_detects_message_cache_active(self):
        """Heuristic: with message-level caching active, at least
        some turns read substantially more than the system-only
        baseline (~25K). Threshold: 40K.

        Updated 2026-04-21 after real run b79d79b5 showed the old
        zero-write-turn heuristic produced false negatives (Anthropic
        writes the new tail content on every turn, so pure read-only
        turns don't exist). New heuristic just checks max cache_read.
        """
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        # Growing cache_read with concurrent small writes (the real
        # pattern observed on post-fix runs). Each turn writes ~5K of
        # new content but reads the full prior prefix.
        conversation_log = [
            {"turn": i, "model": "claude-opus-4-7",
             "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 40000 + i * 15000,  # 40K, 55K, 70K, 85K
             "cache_create_tokens": 5000, "cost_usd": 0.1}
            for i in range(4)
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        assert s["cache_analysis"]["message_cache_likely_active"] is True

    def test_summary_detects_system_only_caching(self):
        """Constant cache_read at ~25K per turn (the system prompt +
        tools baseline) is the signature of pre-2026-04-21 system-
        only caching. No turn exceeds the 40K threshold ⇒ heuristic
        correctly reports message cache inactive."""
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        conversation_log = [
            {"turn": i, "model": "claude-opus-4-7",
             "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 25705,  # constant system-only
             "cache_create_tokens": 0, "cost_usd": 0.1}
            for i in range(5)
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        assert s["cache_analysis"]["message_cache_likely_active"] is False

    def test_summary_heuristic_handles_every_turn_writes_pattern(self):
        """Real-run b79d79b5 regression: every turn has some cache_write
        (Anthropic writes the new tail content on EVERY turn even when
        the bulk of input comes from cache). The prior heuristic
        required at least 3 zero-write turns to detect growth — that
        path never fires in practice, producing false negatives. New
        heuristic ignores cache_write entirely and just checks max
        cache_read against the baseline."""
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        # Matches OpenAI's actual pattern from b79d79b5: every turn
        # has some cache_write, but cache_read peaks far above baseline.
        conversation_log = [
            {"turn": 0, "model": "m", "input_tokens": 282, "output_tokens": 11554,
             "cache_read_tokens": 43318, "cache_create_tokens": 48777, "cost_usd": 0.41},
            {"turn": 1, "model": "m", "input_tokens": 163, "output_tokens": 4835,
             "cache_read_tokens": 76885, "cache_create_tokens": 77751, "cost_usd": 1.20},
            {"turn": 2, "model": "m", "input_tokens": 1, "output_tokens": 93,
             "cache_read_tokens": 77751, "cache_create_tokens": 4816, "cost_usd": 0.08},
            {"turn": 3, "model": "m", "input_tokens": 1, "output_tokens": 1522,
             "cache_read_tokens": 82567, "cache_create_tokens": 119, "cost_usd": 0.06},
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        # 0 read-only turns — every turn has cache_write
        assert s["cache_analysis"]["turns_with_cache_read_only"] == 0
        # But message cache is OBVIOUSLY active (max cache_read 82K >> 25K baseline)
        assert s["cache_analysis"]["message_cache_likely_active"] is True, (
            "Heuristic must detect message caching even when every "
            "turn also has cache_writes. Regression guard from real "
            "run b79d79b5."
        )

    def test_summary_top_costly_turns_sorted_desc(self):
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        conversation_log = [
            {"turn": 0, "model": "m", "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 0.1},
            {"turn": 1, "model": "m", "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 1.5},
            {"turn": 2, "model": "m", "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 0.5},
            {"turn": 3, "model": "m", "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 2.5},
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        tops = s["top_costly_turns"]
        assert len(tops) == 3
        assert tops[0]["turn"] == 3
        assert tops[0]["cost_usd"] == 2.5
        assert tops[1]["turn"] == 1
        assert tops[2]["turn"] == 2

    def test_summary_ignores_non_integer_turn_entries(self):
        """Defensive: if conversation_log ever contains metadata
        entries with string `turn` values (like 'save-docs-9'), they
        must be skipped silently instead of crashing the summary."""
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        conversation_log = [
            {"turn": 0, "model": "m", "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 0.01},
            {"turn": "save-docs-0"},  # Non-integer — must be ignored
            {"turn": 1, "model": "m", "input_tokens": 200, "output_tokens": 20,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 0.02},
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        assert s["total_turns"] == 2  # Not 3
        assert s["aggregate"]["input_tokens"] == 300

    def test_save_writes_summary_alongside_log(self, tmp_path):
        from puzzleeval.agents.implement_test_env import (
            _save_conversation_log,
        )
        conversation_log = [
            {"turn": 0, "model": "claude-sonnet-4-6",
             "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.01},
        ]
        _save_conversation_log(tmp_path, conversation_log, "TestCo")
        # Both files must exist
        log_path = tmp_path / "conversation_log.json"
        summary_path = tmp_path / "conversation_summary.json"
        assert log_path.exists(), "conversation_log.json must be written"
        assert summary_path.exists(), (
            "conversation_summary.json must be written alongside the "
            "full log so per-candidate cost/cache observability is "
            "grep-able without parsing every turn."
        )
        # Summary is valid JSON with expected top-level keys.
        # Use subset semantics — additive enrichments (build_phases,
        # boundary_turns added in NEW-AM) must not break this contract.
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        required_keys = {
            "candidate_name", "total_turns", "aggregate",
            "per_model", "cache_analysis", "top_costly_turns",
        }
        assert required_keys.issubset(summary.keys()), (
            f"summary missing required keys: {required_keys - summary.keys()}"
        )
        # conversation_log.json unchanged shape — still a list
        log_data = json.loads(log_path.read_text(encoding="utf-8"))
        assert isinstance(log_data, list)

    def test_save_summary_failure_does_not_break_log_write(self, tmp_path,
                                                           monkeypatch):
        """Summary computation must never fail the build. If
        compute_conversation_summary raises, conversation_log.json
        must still be written.

        Phase 3.3: the canonical compute_conversation_summary lives in
        puzzleeval/agents/agent5/conversation_log.py. The patch target
        is the canonical path, not the legacy shim, since save_conversation_log
        (also in that module) calls it via local-scope resolution.
        """
        from puzzleeval.agents import implement_test_env as ite
        import puzzleeval.agents.agent5.conversation_log as agent5_log

        def boom(*args, **kwargs):
            raise ValueError("summary computation broken")
        monkeypatch.setattr(agent5_log, "compute_conversation_summary", boom)

        conversation_log = [
            {"turn": 0, "model": "m", "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 0.01},
        ]
        # Must not raise
        ite._save_conversation_log(tmp_path, conversation_log, "TestCo")
        # conversation_log.json still written
        assert (tmp_path / "conversation_log.json").exists()
        # conversation_summary.json NOT written (summary failed)
        assert not (tmp_path / "conversation_summary.json").exists()

    def test_summary_aggregates_latency_ms(self):
        """Real-run audit (2026-04-25) found turn_log was capturing
        cost + cache + tokens but discarding per-turn latency. Without
        latency in turn_log, post-hoc analysis can't correlate
        "this turn cost $X" with "this turn took Ys". Lock the fix:
        latency_ms aggregation in the summary, plus min/max/avg
        distribution stats."""
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        conversation_log = [
            {"turn": 0, "model": "claude-sonnet-4-6",
             "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.01, "latency_ms": 5000.0},
            {"turn": 1, "model": "claude-opus-4-7",
             "input_tokens": 200, "output_tokens": 20,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.05, "latency_ms": 15000.0},
            {"turn": 2, "model": "claude-opus-4-7",
             "input_tokens": 300, "output_tokens": 30,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.07, "latency_ms": 10000.0},
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        agg = s["aggregate"]
        assert agg["latency_ms"] == 30000.0  # 5 + 15 + 10 seconds
        assert agg["latency_min_ms"] == 5000.0
        assert agg["latency_max_ms"] == 15000.0
        assert agg["latency_avg_ms"] == 10000.0
        assert agg["turns_with_latency"] == 3
        # Per-model breakdown also surfaces latency
        opus = s["per_model"]["claude-opus-4-7"]
        assert opus["latency_ms"] == 25000.0  # 15 + 10

    def test_summary_handles_missing_latency_back_compat(self):
        """Pre-NEW-AL conversation_log entries don't have latency_ms.
        The summary must treat missing as zero (not crash) so historical
        runs can still be re-summarized for cost/cache analysis."""
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        conversation_log = [
            # No latency_ms field — pre-fix shape
            {"turn": 0, "model": "m", "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 0.01},
            {"turn": 1, "model": "m", "input_tokens": 200, "output_tokens": 20,
             "cache_read_tokens": 0, "cache_create_tokens": 0, "cost_usd": 0.02},
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        agg = s["aggregate"]
        assert agg["latency_ms"] == 0.0
        assert agg["latency_min_ms"] == 0.0
        assert agg["latency_max_ms"] == 0.0
        assert agg["latency_avg_ms"] == 0.0
        # Critical: turns_with_latency tracks how many turns actually had
        # a positive latency. Lets the summary distinguish "old log,
        # no data" from "new log, every turn was instant" — both look
        # identical in latency_ms otherwise.
        assert agg["turns_with_latency"] == 0
        # Other fields still aggregate normally
        assert agg["input_tokens"] == 300

    def test_turn_log_includes_latency_ms_field(self):
        """Source-grep guard: the turn_log dict must persist `latency_ms`
        so conversation_summary.json can aggregate wall-clock stats.

        Phase 4 Path B note: dict construction moved to
        ``puzzleeval.agents.agent5.turn_blocks.build_initial_turn_log``;
        the wall-clock measurement (``call_latency_ms = round(...)``)
        stays in ``_build_single_harness`` because it captures the API
        roundtrip in the loop's local timing scope. Both halves of the
        contract must hold at their canonical owners.
        """
        import inspect
        from puzzleeval.agents import implement_test_env as ite
        from puzzleeval.agents.agent5 import turn_blocks

        # Dict-construction owner: latency_ms must be persisted with
        # call_latency_ms as the value source.
        turn_log_src = inspect.getsource(turn_blocks.build_initial_turn_log)
        assert '"latency_ms": call_latency_ms' in turn_log_src, (
            "build_initial_turn_log must persist call_latency_ms as the "
            "latency_ms field — conversation_summary.json reads it."
        )

        # Loop owner: the wall-clock variable must still be computed
        # from call_start at the START of the API call.
        loop_src = _agent5_combined_source()
        assert "call_latency_ms = round((time.time() - call_start)" in loop_src, (
            "call_latency_ms must be computed from call_start (the "
            "wall-clock at the start of the API call) — recomputing "
            "later loses the actual API roundtrip duration."
        )

    def test_build_turn_callback_fires_with_rich_diagnostic_payload(self):
        """Real-run audit (2026-04-25): the user observed Agent 5 stuck
        with no per-turn visibility — generic 'Researching API docs'
        messages told us nothing about WHICH page Claude was reading or
        WHICH search query it ran. Lock the enrichment: progress_callback
        must fire AFTER block iteration with tool_calls_detail capturing
        URLs / queries / files Claude touched, plus text_preview and
        cumulative_cost so the operator can `tail` the SSE stream and
        actually understand what Claude is doing each turn.

        Phase 4 Path B note: the rich-payload construction moved to
        ``puzzleeval.agents.agent5.turn_blocks.emit_build_turn_progress``.
        The loop owner (_build_single_harness) MUST NOT silently drop
        the URL/query (negative assertion preserved against the loop
        source); the diagnostic fields are asserted against the helper
        owner where they live now.
        """
        import inspect
        from puzzleeval.agents import implement_test_env as ite
        from puzzleeval.agents.agent5 import turn_blocks

        loop_src = _agent5_combined_source()
        helper_src = inspect.getsource(turn_blocks.emit_build_turn_progress)

        # Server-tool inputs MUST be captured (not dropped). Negative
        # assertion still applies to the loop source — the placeholder
        # literal must never reappear there.
        assert '"(server tool -- handled by API)"' not in loop_src, (
            "Server-tool inputs must be captured (URL for web_fetch, "
            "query for web_search). Dropping them blinds mid-build "
            "diagnostics."
        )

        # build_turn payload's rich diagnostic fields live in the helper.
        for field in ('"tool_calls_detail"', '"text_preview"',
                      '"cumulative_cost_usd"', '"latency_ms"'):
            assert field in helper_src, (
                f"emit_build_turn_progress payload missing {field}. The "
                "whole point of the enrichment is to surface these "
                "fields to the SSE consumer."
            )

        # Incremental conversation_log.json save — tail-able mid-build.
        # This stays in the loop owner; without it the file only exists
        # post-build, useless for debugging a stuck builder.
        assert 'sandbox_dir / "conversation_log.json"' in loop_src, (
            "conversation_log.json must be saved incrementally after "
            "each turn so operators can `cat` it mid-build."
        )

    def test_summary_includes_build_phases_breakdown(self):
        """Real-run audit (2026-04-25): the user couldn't tell from the
        summary whether Agent 5 spent its time researching, building, or
        validating. build_phases derives phase boundaries from turn data
        (api_spec.txt write → research-end, "SMOKE TEST PASSED" in tool
        results → build-end, "HARNESS_COMPLETE" in text → validate-end)
        and reports per-phase turn count, cost, latency."""
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        # Simulated build: 2 research turns, 3 build turns (smoke passes
        # at turn 4), 1 validate turn (HARNESS_COMPLETE at turn 5).
        conversation_log = [
            {"turn": 0, "model": "claude-sonnet-4-6",
             "input_tokens": 1000, "output_tokens": 500,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.30, "latency_ms": 30000.0,
             "tool_results": [{"tool": "web_fetch", "url": "https://example.com"}]},
            {"turn": 1, "model": "claude-sonnet-4-6",
             "input_tokens": 1000, "output_tokens": 500,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.20, "latency_ms": 15000.0,
             "tool_results": [{"tool": "web_fetch", "url": "https://example.com/api"}]},
            {"turn": 2, "model": "claude-opus-4-7",
             "input_tokens": 2000, "output_tokens": 800,
             "cache_read_tokens": 5000, "cache_create_tokens": 1000,
             "cost_usd": 0.05, "latency_ms": 8000.0,
             "tool_results": [{"tool": "write_file", "wrote_path": "api_spec.txt", "wrote_chars": 5000}]},
            {"turn": 3, "model": "claude-opus-4-7",
             "input_tokens": 2000, "output_tokens": 800,
             "cache_read_tokens": 5000, "cache_create_tokens": 1000,
             "cost_usd": 0.05, "latency_ms": 6000.0,
             "tool_results": [{"tool": "write_file", "wrote_path": "harness.py", "wrote_chars": 3000}]},
            {"turn": 4, "model": "claude-opus-4-7",
             "input_tokens": 2000, "output_tokens": 800,
             "cache_read_tokens": 5000, "cache_create_tokens": 1000,
             "cost_usd": 0.05, "latency_ms": 4000.0,
             "tool_results": [{"tool": "run_code", "result": "SMOKE TEST PASSED — all checks ok"}]},
            {"turn": 5, "model": "claude-opus-4-7",
             "input_tokens": 2000, "output_tokens": 800,
             "cache_read_tokens": 5000, "cache_create_tokens": 1000,
             "cost_usd": 0.10, "latency_ms": 12000.0,
             "text": "Both turns succeeded. HARNESS_COMPLETE",
             "tool_results": []},
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")

        # boundary_turns enumerate phase transitions
        bt = s["boundary_turns"]
        assert bt["api_spec_written_at"] == 2
        assert bt["smoke_passed_at"] == 4
        assert bt["harness_complete_at"] == 5

        # build_phases breakdown — research / build / validate buckets
        bp = s["build_phases"]
        assert "research" in bp
        assert bp["research"]["turns"] == 2  # turns 0, 1
        assert bp["research"]["first_turn"] == 0
        assert bp["research"]["last_turn"] == 1
        assert bp["research"]["cost_usd"] == 0.50  # 0.30 + 0.20

        assert "build" in bp
        assert bp["build"]["turns"] == 2  # turns 2, 3
        assert bp["build"]["first_turn"] == 2
        assert bp["build"]["last_turn"] == 3

        assert "validate" in bp
        assert bp["validate"]["turns"] == 1  # turn 4 (smoke passed at 4 → validate starts at 5)
        # Actually with our bucketing: tn < smoke_passed_at (4) → build, tn < harness_complete_at (5) → validate
        # So turn 4 = validate (since 4 < 5), turn 5 = post (since 5 >= 5)

        # post phase = HARNESS_COMPLETE turn
        assert "post" in bp
        assert bp["post"]["turns"] == 1
        assert bp["post"]["first_turn"] == 5

    def test_summary_handles_unfinished_build(self):
        """If a build is killed mid-research (no api_spec.txt yet), all
        turns must bucket as 'research' and boundary_turns must report
        -1 for unreached phases."""
        from puzzleeval.agents.implement_test_env import (
            _compute_conversation_summary,
        )
        conversation_log = [
            {"turn": 0, "model": "m", "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.01, "latency_ms": 1000.0, "tool_results": []},
            {"turn": 1, "model": "m", "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "cost_usd": 0.01, "latency_ms": 1000.0, "tool_results": []},
        ]
        s = _compute_conversation_summary(conversation_log, "TestCo")
        assert s["boundary_turns"] == {
            "api_spec_written_at": -1,
            "smoke_passed_at": -1,
            "harness_complete_at": -1,
        }
        # All turns bucketed as research
        assert s["build_phases"]["research"]["turns"] == 2
        # No other phases populated
        assert "build" not in s["build_phases"]
        assert "validate" not in s["build_phases"]

    def test_web_fetch_and_search_results_captured_in_block_iteration(self):
        """Source-grep guard: the block iteration loop must handle
        web_fetch_tool_result and web_search_tool_result block types
        (not just text/thinking/tool_use/advisor*). Without these
        handlers, web fetch/search results pass through silently and
        we lose visibility into WHAT Claude actually found vs what it
        searched/fetched."""
        import inspect
        from puzzleeval.agents import implement_test_env as ite

        src = _agent5_combined_source()
        assert 'block.type == "web_fetch_tool_result"' in src
        assert 'block.type == "web_search_tool_result"' in src
        # Both must contribute to turn_log["tool_results"]
        assert '"chars_returned"' in src
        assert '"top_results"' in src

    def test_synthesize_api_spec_skips_when_checklist_incomplete(self):
        """Real-run audit (2026-04-25, trace 8ded6706): Sonnet's Phase 1
        research turn took 358-408 seconds per candidate ($1.20 each)
        re-extracting fields the BuildReadinessChecklist already had.
        The pre-render fast path generates an api_spec.txt skeleton from
        the checklist when all 4 non-negotiables are confirmed — but
        MUST return None (fall through to legacy full-research) when
        the checklist is missing, sentinel, or has any non-negotiable
        not at confirmed status. Otherwise we'd pre-render specs from
        unverified data and the harness would target wrong endpoints."""
        from puzzleeval.agents.implement_test_env import (
            _synthesize_api_spec_from_checklist,
        )
        from puzzleeval.schemas import (
            ScreenedCandidate, BuildReadinessChecklist, FieldStatus,
            default_unknown_checklist,
        )

        # Helper: build a minimal candidate (covers all required fields)
        def _make_candidate(checklist):
            return ScreenedCandidate(
                name="TestCo", provider="TestCo Inc",
                description="Test", relevance_score=0.9,
                adoption_difficulty="easy",
                claimed_capabilities=["x"],
                relevant_subtasks=["sub_1"],
                source="research_agent",
                confirmed_capabilities=["x"], auth_method="api_key",
                api_access_method="free_tier",
                verified_api_docs_url="https://example.com/docs",
                pricing_model="freemium", pricing_details="",
                rate_limit_info="", data_format_notes="",
                screening_notes="", checklist=checklist,
            )

        # Case 1: checklist=None → no pre-render
        c = _make_candidate(None)
        assert _synthesize_api_spec_from_checklist(c) is None

        # Case 2: sentinel checklist → no pre-render
        c = _make_candidate(default_unknown_checklist("test failure"))
        assert _synthesize_api_spec_from_checklist(c) is None

        # Case 3: real checklist with one INFERRED non-negotiable → partial pre-render
        # NEW-AM v3 lifted the "all 4 confirmed" requirement. Now we render
        # confirmed fields as authoritative and inferred fields with a
        # [INFERRED — verify on first call] marker. Cuts redundant Sonnet
        # research on the confirmed fields while keeping a clear signal
        # about which to verify.
        partial = BuildReadinessChecklist(
            populated_by="agent_4",
            endpoint_path=FieldStatus(status="confirmed", value="https://api.x/v1"),
            auth_method=FieldStatus(status="inferred", value="bearer"),  # inferred — must verify
            request_body_shape=FieldStatus(status="confirmed", value='{"x": "y"}'),
            response_body_shape=FieldStatus(status="confirmed", value='{"ok": true}'),
        )
        c = _make_candidate(partial)
        spec = _synthesize_api_spec_from_checklist(c)
        assert spec is not None, (
            "NEW-AM v3 partial pre-render: 3-of-4 confirmed should still "
            "produce a spec; the inferred field gets [INFERRED] marker."
        )
        assert "[INFERRED" in spec, "inferred field must be flagged for verification"
        # The confirmed values must still appear authoritatively
        assert "https://api.x/v1" in spec
        assert '{"x": "y"}' in spec

        # Case 3b: zero confirmed non-negotiables → fall through (not enough scaffolding)
        all_inferred = BuildReadinessChecklist(
            populated_by="agent_4",
            endpoint_path=FieldStatus(status="inferred", value="https://api.x/v1"),
            auth_method=FieldStatus(status="inferred", value="bearer"),
            request_body_shape=FieldStatus(status="inferred", value='{"x": "y"}'),
            response_body_shape=FieldStatus(status="inferred", value='{"ok": true}'),
        )
        c = _make_candidate(all_inferred)
        assert _synthesize_api_spec_from_checklist(c) is None, (
            "All-inferred non-negotiables: not enough scaffolding to "
            "pre-render meaningfully; fall through to legacy full research."
        )

        # Case 3c: endpoint unknown → fall through (can't build without endpoint)
        no_endpoint = BuildReadinessChecklist(
            populated_by="agent_4",
            endpoint_path=FieldStatus(status="unknown"),  # missing endpoint
            auth_method=FieldStatus(status="confirmed", value="bearer"),
            request_body_shape=FieldStatus(status="confirmed", value='{"x": "y"}'),
            response_body_shape=FieldStatus(status="confirmed", value='{"ok": true}'),
        )
        c = _make_candidate(no_endpoint)
        assert _synthesize_api_spec_from_checklist(c) is None, (
            "Without endpoint, can't pre-render anything useful — fall through."
        )

        # Case 4: all 4 non-negotiables CONFIRMED → real pre-render
        full = BuildReadinessChecklist(
            populated_by="agent_4",
            endpoint_path=FieldStatus(status="confirmed",
                                       value="https://api.x/v1/widgets",
                                       source_url="https://docs.x/api"),
            auth_method=FieldStatus(status="confirmed",
                                     value="Bearer in Authorization header"),
            request_body_shape=FieldStatus(status="confirmed",
                                            value='{"name": "string"}'),
            response_body_shape=FieldStatus(status="confirmed",
                                             value='{"id": "uuid"}'),
        )
        c = _make_candidate(full)
        spec = _synthesize_api_spec_from_checklist(c)
        assert spec is not None
        assert spec.startswith("API_SPEC_START")
        assert "API_SPEC_END" in spec
        # Non-negotiable values must appear in the rendered spec
        assert "https://api.x/v1/widgets" in spec
        assert "Bearer in Authorization header" in spec
        assert '{"name": "string"}' in spec
        assert '{"id": "uuid"}' in spec
        # Source URL preserved
        assert "https://docs.x/api" in spec
        # Service name + provider preserved for grep-ability
        assert "TestCo" in spec
        # NEW-AM v2: the 40% additive sections must be marked as
        # REQUIRES_AUGMENT so Sonnet does targeted research, not skip.
        # Pre-rendering ONLY the confirmed 60% would lose harness-
        # critical info (override message shapes, auxiliary endpoint
        # bodies, per-test-case INPUT_COMPATIBILITY/ROUTING_TABLE).
        assert "[REQUIRES_AUGMENT" in spec, (
            "Pre-rendered spec must mark per-test-case + auxiliary "
            "fields as REQUIRES_AUGMENT so Sonnet does targeted "
            "research instead of skipping (which would drop harness "
            "quality on override message shapes and per-test-case "
            "input mappings)."
        )
        for required_augment in (
            "AUXILIARY_ENDPOINTS_REQUEST_BODIES",
            "OVERRIDE_OR_SPECIAL_MESSAGE_SHAPES",
            "INPUT_COMPATIBILITY",
            "ROUTING_TABLE",
            "WORKING_EXAMPLE",
        ):
            assert required_augment in spec, (
                f"Pre-rendered spec must include section {required_augment!r} "
                "(marked REQUIRES_AUGMENT) — without it, Sonnet/Opus has "
                "no checklist of what augment work remains."
            )
        # NEW-AM v3 — completeness-confidence note must be in the spec
        # to warn against trusting REQUEST_BODY_SHAPE as exhaustive.
        assert "COMPLETENESS_NOTE" in spec, (
            "Pre-rendered spec must include COMPLETENESS_NOTE warning "
            "the builder that REQUEST_BODY_SHAPE captures the PRIMARY "
            "shape, not all variants. Without this Opus might trust the "
            "spec as exhaustive and miss override / lifecycle / variant "
            "messages needed by the test cases."
        )

    def test_streaming_response_contract_injects_for_voice_tests(self):
        """NEW-AM v4: real-run trace 4427591c (2026-04-25) caught two
        builds of the SAME ElevenLabs ConvAI provider producing 60% vs
        20% pass rates because the prompt didn't teach response-collection
        patterns. The new streaming-response contract teaches the
        error-timeout + reset-on-event pattern that should land in
        any harness collecting a streaming response.

        Voice tests must trigger the injection."""
        from puzzleeval.agents.implement_test_env import (
            _streaming_response_contract,
        )
        # Voice conversation test (multi-turn WebSocket)
        voice_tcs = [{"input_type": "voice_conversation",
                      "output_type": "voice_conversation"}]
        result = _streaming_response_contract(voice_tcs)
        assert result, "voice_conversation tests must inject the streaming contract"
        assert "error-timeout" in result.lower()
        assert "reset" in result.lower()
        assert "completion" in result.lower()

        # Voice turn test (single-turn but TTS streams)
        result = _streaming_response_contract([{"output_type": "voice_turn"}])
        assert result

        # Multi-turn chat (text streaming events)
        result = _streaming_response_contract([{"input_type": "conversation"}])
        assert result

    def test_streaming_response_contract_does_NOT_inject_for_rest_tests(self):
        """Anti-contamination guard: OCR / vision / single-call REST
        builds must see EMPTY injection. The contract is for streaming
        response collection only — adding it to OCR builds would be a
        bandaid that pollutes unrelated harness builds."""
        from puzzleeval.agents.implement_test_env import (
            _streaming_response_contract,
        )

        # OCR (file in, structured JSON out — single blocking REST call)
        ocr_tcs = [{"input_type": "file", "output_type": "structured_json"}]
        assert _streaming_response_contract(ocr_tcs) == "", (
            "OCR/REST builds must NOT receive streaming contract — "
            "single-call APIs don't need it and the rule would be "
            "noise. Anti-bandaid guard."
        )

        # Vision classification (image in, classification out)
        vision_tcs = [{"input_type": "image_description",
                       "output_type": "classification"}]
        assert _streaming_response_contract(vision_tcs) == ""

        # Single-turn chatbot (text in, free_text out — REST)
        chat_tcs = [{"input_type": "text", "output_type": "free_text"}]
        assert _streaming_response_contract(chat_tcs) == ""

        # Inbound webhook
        wh_tcs = [{"input_type": "webhook_event",
                   "output_type": "structured_json"}]
        assert _streaming_response_contract(wh_tcs) == ""

        # Outbound message (fire-and-forget)
        out_tcs = [{"output_type": "outbound_message"}]
        assert _streaming_response_contract(out_tcs) == ""

        # Empty / None
        assert _streaming_response_contract([]) == ""
        assert _streaming_response_contract(None) == ""

    def test_streaming_contract_is_principle_based_not_bandaid(self):
        """The contract content must teach the ABSTRACTION (error-timeout
        + reset-on-event), not specific provider quirks. No provider
        names or specific timeout numbers from one provider's docs.
        Anti-bandaid invariant — keeps the rule general across providers."""
        from puzzleeval.agents.implement_test_env import (
            _STREAMING_RESPONSE_CONTRACT,
        )
        # Provider-name guard — no specific provider should be hardcoded
        # in the contract (would be a bandaid for that provider)
        for forbidden_name in (
            "ElevenLabs", "elevenlabs",
            "OpenAI Realtime", "openai realtime",
            "Vapi", "vapi",
            "Retell", "retell",
            "Bland",
        ):
            assert forbidden_name not in _STREAMING_RESPONSE_CONTRACT, (
                f"Streaming contract mentions {forbidden_name!r} — that's a "
                "bandaid for one provider. Use principle-based language "
                "(LLM-backed providers, async polling APIs, etc.)."
            )
        # Specific-magic-number guard — the actual broken pattern from
        # trace 4427591c was `silence_after_secs=1.5`. The contract
        # MUST NOT teach that specific bad value. Other "1.5" occurrences
        # in general guidance (e.g., "1-1.5s VAD trailing silence") are
        # principle-based ranges, not bandaids — those are fine.
        assert "silence_after_secs=1.5" not in _STREAMING_RESPONSE_CONTRACT
        assert "silence_after_secs = 1.5" not in _STREAMING_RESPONSE_CONTRACT
        # Anti-bandaid: don't teach "use 1.5 seconds" as a recommendation
        for forbidden_phrase in ("1.5 seconds for", "1.5s for"):
            assert forbidden_phrase not in _STREAMING_RESPONSE_CONTRACT, (
                f"Contract recommends specific timeout {forbidden_phrase!r} "
                "— use ranges instead (8-15 seconds for LLM-backed)."
            )
        # Must teach the actual pattern
        assert "reset" in _STREAMING_RESPONSE_CONTRACT.lower()
        assert "error-timeout" in _STREAMING_RESPONSE_CONTRACT.lower() or \
               "error_timeout" in _STREAMING_RESPONSE_CONTRACT.lower()

    def test_streaming_contract_routes_through_unified_dispatcher(self):
        """Phase 1.C consolidation: every conditional contract (platform +
        modality, including streaming response) flows through the unified
        ``puzzleeval.contracts.compose_contract_block(task)`` selector.
        Single trailing placeholder ``__CONTRACT_BLOCK__`` for cache-
        prefix optimization. No per-modality placeholders.
        """
        import inspect
        from puzzleeval.agents import implement_test_env as ite

        # Anti-proliferation: no separate streaming placeholder, no
        # legacy split placeholders.
        assert "__STREAMING_RESPONSE_CONTRACT__" not in ite.BUILDER_SYSTEM_PROMPT, (
            "Streaming contract must NOT have its own placeholder."
        )
        assert "__MODALITY_CONTRACT__" not in ite.BUILDER_SYSTEM_PROMPT, (
            "Phase 1.B collapsed __MODALITY_CONTRACT__ into "
            "__CONTRACT_BLOCK__."
        )
        assert "__OS_SPECIFIC_RULES__" not in ite.BUILDER_SYSTEM_PROMPT, (
            "Phase 1.B collapsed __OS_SPECIFIC_RULES__ into "
            "__CONTRACT_BLOCK__."
        )
        assert "__CONTRACT_BLOCK__" in ite.BUILDER_SYSTEM_PROMPT, (
            "Builder prompt must use the unified __CONTRACT_BLOCK__ "
            "placeholder (Phase 1.B + 1.C)."
        )
        # Legacy helpers still exist (back-compat for source-grep tests +
        # external callers) but the canonical render path now uses the
        # contracts package directly.
        assert callable(ite._voice_harness_contract_for)
        assert callable(ite._streaming_response_contract_for)
        assert callable(ite._modality_specific_contract)
        # Canonical renderer wires through compose_contract_block.
        rsrc = inspect.getsource(ite._render_builder_prompt)
        assert "compose_contract_block" in rsrc, (
            "_render_builder_prompt must invoke the unified selector "
            "(compose_contract_block) — that's the Phase 1.C canonical "
            "path."
        )
        assert "TaskContext" in rsrc

    def test_voice_test_cases_get_BOTH_contracts_composed(self):
        """Voice test cases need BOTH voice return-shape contract AND
        streaming response collection contract. The unified dispatcher
        must compose both — not pick one or the other."""
        from puzzleeval.agents.implement_test_env import (
            _modality_specific_contract,
            _VOICE_HARNESS_CONTRACT,
            _STREAMING_RESPONSE_CONTRACT,
        )
        voice_tcs = [{"input_type": "voice_conversation",
                      "output_type": "voice_conversation"}]
        result = _modality_specific_contract(voice_tcs)
        assert _VOICE_HARNESS_CONTRACT in result, (
            "Voice tests must get the voice return-shape contract"
        )
        assert _STREAMING_RESPONSE_CONTRACT in result, (
            "Voice tests must ALSO get the streaming-collection contract — "
            "voice WebSocket needs both"
        )

    def test_code_test_cases_get_streaming_only_no_voice(self):
        """Code-gen streaming tests need the streaming contract but
        NOT the voice contract. The unified dispatcher must select
        feature-by-feature, not as one big block."""
        from puzzleeval.agents.implement_test_env import (
            _modality_specific_contract,
            _VOICE_HARNESS_CONTRACT,
            _STREAMING_RESPONSE_CONTRACT,
        )
        code_tcs = [{"input_type": "text", "output_type": "code"}]
        result = _modality_specific_contract(code_tcs)
        assert _STREAMING_RESPONSE_CONTRACT in result, (
            "Code-gen tests must get streaming-collection contract"
        )
        assert _VOICE_HARNESS_CONTRACT not in result, (
            "Code-gen tests must NOT get voice contract (no voice "
            "modality involved)"
        )

    def test_rest_test_cases_get_empty_unified_contract(self):
        """OCR/vision/REST tests must see EMPTY composed contract."""
        from puzzleeval.agents.implement_test_env import (
            _modality_specific_contract,
        )
        ocr_tcs = [{"input_type": "file", "output_type": "structured_json"}]
        assert _modality_specific_contract(ocr_tcs) == "", (
            "OCR/REST tests must see empty unified contract — zero "
            "prompt overhead for non-streaming builds"
        )

    # Phase 8: deleted source-grep test `test_phase_transition_fires_on_patch_api_spec`.
    # Behavior covered by: tests/test_dispatch_helpers.py::TestDetectPhaseTransition + tests/test_build_loop_behavior.py::TestPhaseTransitionTriggers

    def test_phase1_prompt_forbids_sonnet_writing_code_files(self):
        """NEW-AM v7: Sonnet (research model) must NOT write harness.py,
        requirements.txt, smoke_test.py, or live_test.py during Phase 1.
        These are Phase 2 (Opus) responsibilities. Without this hard
        constraint, Sonnet's parallel-writes optimization bypasses
        the model-switch architecture and produces broken code files.
        """
        from puzzleeval.agents.implement_test_env import BUILDER_SYSTEM_PROMPT

        prompt = BUILDER_SYSTEM_PROMPT
        # Must explicitly forbid Sonnet from writing code files
        assert "PHASE 1 SONNET FORBIDDEN" in prompt or "Phase 1 Sonnet forbidden" in prompt or \
               "Sonnet (the\nresearch model) MUST NOT write these files" in prompt or \
               "Sonnet (the research model) MUST NOT write these files" in prompt
        # Must list the specific forbidden files
        for forbidden in ("harness.py", "smoke_test.py", "live_test.py", "requirements.txt"):
            # Look for explicit mention of forbidden write_file pattern
            # (these names appear elsewhere in the prompt for general
            # guidance; the constraint is a specific block teaching
            # which model owns them)
            assert forbidden in prompt
        # Must explain the WHY (real-run evidence)
        assert "trace a4860e94" in prompt or "Real-run evidence" in prompt or "REAL-RUN EVIDENCE" in prompt
        # Must keep Phase 2 parallel-writes encouraged (only Phase 1 restricted)
        assert "Phase 2" in prompt or "PHASE 2" in prompt

    def test_live_test_contract_injects_for_voice_tests(self):
        """NEW-AM v6 live-test contract (real-run trace a4860e94, 2026-04-25):
        voice/conversation tests must get the rigorous live-test
        guidance — Phase 3's OCR-style live-test pattern doesn't apply
        and the builder improvises (sometimes badly: trace a4860e94's
        OpenAI live_test passed with audio_url=None, real tests got 0/5).
        """
        from puzzleeval.agents.implement_test_env import (
            _live_test_contract_for,
            _LIVE_TEST_CONTRACT_VOICE,
        )
        # Voice conversation test (multi-turn WebSocket)
        voice_tcs = [{"input_type": "voice_conversation",
                      "output_type": "voice_conversation"}]
        result = _live_test_contract_for(voice_tcs)
        assert result == _LIVE_TEST_CONTRACT_VOICE
        # Single-turn voice
        result = _live_test_contract_for([{"output_type": "voice_turn"}])
        assert result == _LIVE_TEST_CONTRACT_VOICE
        # Multi-turn text chat
        result = _live_test_contract_for([{"input_type": "conversation"}])
        assert result == _LIVE_TEST_CONTRACT_VOICE

    def test_live_test_contract_does_NOT_inject_for_rest_tests(self):
        """Anti-contamination guard: OCR/vision/single-call REST builds
        must see EMPTY live-test contract injection. The OCR-style
        Phase 3 live test (test each file type, success+output_len>100)
        still works for these — the new contract is voice-specific
        guidance that would just be noise."""
        from puzzleeval.agents.implement_test_env import (
            _live_test_contract_for,
        )
        ocr_tcs = [{"input_type": "file", "output_type": "structured_json"}]
        assert _live_test_contract_for(ocr_tcs) == ""
        vision_tcs = [{"input_type": "image_description",
                       "output_type": "classification"}]
        assert _live_test_contract_for(vision_tcs) == ""
        chat_tcs = [{"input_type": "text", "output_type": "free_text"}]
        assert _live_test_contract_for(chat_tcs) == ""
        wh_tcs = [{"input_type": "webhook_event",
                   "output_type": "structured_json"}]
        assert _live_test_contract_for(wh_tcs) == ""
        out_tcs = [{"output_type": "outbound_message"}]
        assert _live_test_contract_for(out_tcs) == ""
        assert _live_test_contract_for([]) == ""
        assert _live_test_contract_for(None) == ""

    def test_live_test_contract_principle_based_not_provider_specific(self):
        """The contract content must teach the PATTERN (real audio +
        multi-turn + production payload shape + audio assertions),
        not specific provider quirks. Anti-bandaid invariant."""
        from puzzleeval.agents.implement_test_env import (
            _LIVE_TEST_CONTRACT_VOICE,
        )
        # No provider names hardcoded
        for forbidden in ("ElevenLabs", "elevenlabs", "OpenAI Realtime",
                          "openai realtime", "Vapi", "vapi", "Retell",
                          "retell"):
            assert forbidden not in _LIVE_TEST_CONTRACT_VOICE, (
                f"Live-test contract mentions {forbidden!r} — bandaid risk. "
                "Use principle-based language."
            )
        # Must teach the production payload shape
        for required in ('"audio_url"', '"turn_index"', '"session_state"',
                         '"input_context"'):
            assert required in _LIVE_TEST_CONTRACT_VOICE, (
                f"Live-test contract missing required field {required} "
                "from the production payload shape."
            )
        # Must teach 2-turn minimum
        assert "2-turn" in _LIVE_TEST_CONTRACT_VOICE.lower() or "2 turns" in _LIVE_TEST_CONTRACT_VOICE.lower()
        # Must teach audio assertion (not just success=True)
        assert "audio_bytes" in _LIVE_TEST_CONTRACT_VOICE

    def test_live_test_contract_routes_through_unified_dispatcher(self):
        """NEW-AM v6 consolidation: live-test contract composes through
        `_modality_specific_contract` (single placeholder, single
        dispatcher) — no separate __LIVE_TEST_CONTRACT__ placeholder
        which would proliferate injection points."""
        import inspect
        from puzzleeval.agents import implement_test_env as ite

        # Anti-proliferation: no separate placeholder
        assert "__LIVE_TEST_CONTRACT__" not in ite.BUILDER_SYSTEM_PROMPT, (
            "Live-test contract must compose through the unified "
            "_modality_specific_contract dispatcher (same pattern as "
            "voice + streaming contracts)."
        )
        # The unified dispatcher composes the live-test helper
        src = inspect.getsource(ite._modality_specific_contract)
        assert "_live_test_contract_for" in src
        # Helper is callable
        assert callable(ite._live_test_contract_for)

    def test_voice_test_cases_get_THREE_contracts_composed(self):
        """Voice test cases need ALL THREE contracts composed:
          1. voice harness return-shape contract
          2. streaming response collection contract
          3. live-test rigor contract
        The unified dispatcher must combine all three."""
        from puzzleeval.agents.implement_test_env import (
            _modality_specific_contract,
            _VOICE_HARNESS_CONTRACT,
            _STREAMING_RESPONSE_CONTRACT,
            _LIVE_TEST_CONTRACT_VOICE,
        )
        voice_tcs = [{"input_type": "voice_conversation",
                      "output_type": "voice_conversation"}]
        result = _modality_specific_contract(voice_tcs)
        assert _VOICE_HARNESS_CONTRACT in result
        assert _STREAMING_RESPONSE_CONTRACT in result
        assert _LIVE_TEST_CONTRACT_VOICE in result, (
            "Voice tests must get the live-test rigor contract — "
            "without it, builder improvises (often badly)."
        )

    def test_execute_single_test_uses_per_call_unique_filenames(self):
        """NEW-AM v6 race-condition fix (real-run trace a4860e94,
        2026-04-25): pre-fix used SHARED `_test_input.json` and
        `_test_output.json` filenames in the candidate's sandbox. With
        AGENT6_PER_CANDIDATE_SESSION_PARALLELISM=6, multiple test
        workers running in parallel raced on the same files →
        cross-test contamination.

        Real evidence from trace a4860e94 ElevenLabs tc-001:
            persona = James Whitfield, phone 555-204-7381
            transcript[3] agent: 'Thanks for calling Acme Plumbing,
                                  Maria. ... 555-918-4422'
                                  ↑ tc-002's name + phone leaked in

        Lock the per-call unique filename pattern:
          * `_test_input_{call_id}.json` (call_id = secrets.token_hex(8))
          * `_test_output_{call_id}.json`
        Subprocess reads/writes its OWN file → no race.
        """
        import inspect
        from puzzleeval.agents import implement_test_env as ite

        src = inspect.getsource(ite._execute_single_test)

        # Must NOT use the shared filenames anymore
        assert '"_test_input.json"' not in src, (
            "_execute_single_test must NOT use the shared filename "
            "_test_input.json — it caused cross-test bleed in trace "
            "a4860e94. Use per-call unique filenames instead."
        )
        assert '"_test_output.json"' not in src, (
            "_execute_single_test must NOT use the shared filename "
            "_test_output.json — same race issue."
        )
        # Must use the per-call unique pattern via secrets.token_hex
        assert "secrets.token_hex(8)" in src, (
            "Per-call unique filenames must use secrets.token_hex(8) "
            "so 6 parallel test subprocesses can't race on the same "
            "input/output file."
        )
        # Filenames must include the call_id in the pattern
        assert "_test_input_" in src
        assert "_test_output_" in src

    def test_verification_max_tokens_at_or_above_16k(self):
        """Real-run trace 4427591c (2026-04-25) caught Agent 4's
        BUILD_READINESS_CHECKLIST JSON block being truncated by
        max_tokens despite the prior NEW-AK bump from 4096 to 12288.
        Rich-docs candidates (OpenAI Realtime SIP had 9 capabilities,
        5 user_selectable_params, full interaction_model, detailed
        pricing) consumed the 12K budget on findings + structured
        fields, leaving the fenced JSON block truncated → sentinel
        checklist → no pre-render fast path → Agent 5 had to do full
        Sonnet research.

        16384 is the new floor: Sonnet output rate $15/MTok × 4K extra
        tokens = $0.06 worst case per candidate when actually used,
        $0 otherwise. Still well below Sonnet's 64K per-call cap.
        """
        from puzzleeval.agents.screening import VERIFICATION_MAX_TOKENS
        assert VERIFICATION_MAX_TOKENS >= 16384, (
            f"VERIFICATION_MAX_TOKENS={VERIFICATION_MAX_TOKENS} is below "
            "the 16K floor. Rich-docs candidates need this headroom for "
            "the BUILD_READINESS_CHECKLIST fenced JSON block to land "
            "after findings + structured fields + adaptive thinking. "
            "Without it, Agent 4 returns sentinel checklists and "
            "Agent 5 loses the pre-render fast path."
        )

    def test_provider_surface_uses_correct_endpointsummary_fields(self):
        """Real-run audit (trace 8ded6706, 2026-04-25) found the
        synthesizer was reading non-existent EndpointSummary fields
        (`method`, `path`, `role`) instead of the actual schema fields
        (`name`, `relevance_to_use_case`, `selection_note`). Result:
        every provider_surface entry rendered as '[?] ? ?' silently
        dropping the alternatives info — defeating the whole purpose
        of surfacing alternatives. Lock the correct field reads."""
        from puzzleeval.agents.implement_test_env import (
            _synthesize_api_spec_from_checklist,
        )
        from puzzleeval.schemas import (
            ScreenedCandidate, BuildReadinessChecklist, FieldStatus,
            EndpointSummary,
        )

        cl = BuildReadinessChecklist(
            populated_by="agent_4",
            endpoint_path=FieldStatus(status="confirmed", value="wss://api.x/v1/realtime"),
            auth_method=FieldStatus(status="confirmed", value="Bearer"),
            request_body_shape=FieldStatus(status="confirmed", value='{"type":"init"}'),
            response_body_shape=FieldStatus(status="confirmed", value='{"type":"audio"}'),
            provider_surface=[
                EndpointSummary(
                    name="WebSocket /v1/realtime",
                    purpose="Bidirectional voice conversation",
                    relevance_to_use_case="primary",
                    selection_note="matches voice_agent scope",
                ),
                EndpointSummary(
                    name="POST /v1/audio/transcriptions",
                    purpose="Standalone STT, single audio file",
                    relevance_to_use_case="alternative",
                    selection_note="rejected — not real-time",
                ),
                EndpointSummary(
                    name="POST /v1/audio/speech",
                    purpose="Standalone TTS",
                    relevance_to_use_case="unrelated",
                ),
            ],
        )
        c = ScreenedCandidate(
            name="OpenAI Realtime", provider="OpenAI",
            description="Voice", relevance_score=0.95,
            adoption_difficulty="easy",
            claimed_capabilities=["voice"],
            relevant_subtasks=["sub_1"],
            source="research_agent",
            confirmed_capabilities=["voice"], auth_method="bearer_token",
            api_access_method="free_signup",
            verified_api_docs_url="https://platform.openai.com/docs/realtime",
            pricing_model="usage", pricing_details="",
            rate_limit_info="", data_format_notes="",
            screening_notes="", checklist=cl,
        )
        spec = _synthesize_api_spec_from_checklist(c)
        assert spec is not None
        # Real endpoint NAMES must appear (not '?')
        assert "WebSocket /v1/realtime" in spec
        assert "POST /v1/audio/transcriptions" in spec
        assert "POST /v1/audio/speech" in spec
        # Real RELEVANCE labels must appear (not '?')
        assert "[primary]" in spec
        assert "[alternative]" in spec
        assert "[unrelated]" in spec
        # Selection notes surfaced when present
        assert "rejected — not real-time" in spec or "rejected" in spec
        # No bare '[?]' or 'method=?' from old field-name bug
        assert "[?]" not in spec, (
            "Provider surface entries should not render as '[?]' — that "
            "indicates the synthesizer is reading wrong EndpointSummary "
            "field names (the original 'method'/'path'/'role' bug)."
        )

    def test_pre_rendered_spec_keeps_sonnet_for_targeted_augment(self):
        """Real-run analysis (trace 8ded6706, 2026-04-25) showed
        Sonnet's 6-min research turn was ~60% redundant (re-extracting
        confirmed checklist fields) but ~40% added real value the
        checklist doesn't carry by design (auxiliary endpoints,
        override message shapes, per-test-case INPUT_COMPATIBILITY +
        ROUTING_TABLE, WORKING_EXAMPLE). NEW-AM v2 design decision:
        pre-render only the AUTHORITATIVE 60% with explicit
        [REQUIRES_AUGMENT] markers; Sonnet still runs Phase 1 to do
        targeted-augment research (~2-3 min instead of 6) on the
        remaining 40%. This is cheaper than routing the augment work
        through Opus (Opus output tokens cost ~1.7x more than Sonnet)
        AND it preserves harness quality.
        """
        import inspect
        from puzzleeval.agents import implement_test_env as ite

        # Phase 4.2: pre-render logic moved from _build_single_harness
        # into _setup_sandbox_and_credentials. The contract checks now
        # target both the setup helper (synthesizer call + write) AND
        # the build loop (state initialization).
        setup_src = inspect.getsource(ite._setup_sandbox_and_credentials)
        loop_src = _agent5_combined_source()

        # Pre-render call site must exist in setup
        assert "_synthesize_api_spec_from_checklist(candidate)" in setup_src, (
            "_setup_sandbox_and_credentials must call the synthesizer "
            "before returning to the build loop."
        )
        # Must write to api_spec.txt in setup
        assert '(sandbox_dir / "api_spec.txt").write_text' in setup_src

        # api_spec_written must NOT be seeded True from pre-render.
        # Sonnet still owns Phase 1 — does targeted augment, writes
        # the final spec, which triggers the normal Phase 1→2 transition.
        assert "api_spec_written = False\n    if pre_rendered_spec:" in loop_src, (
            "api_spec_written should stay False at build start so Sonnet "
            "does targeted-augment Phase 1; the normal write_file trigger "
            "flips it to True after augment."
        )

    def test_promote_verdict_aggregates_test_cost_from_components(self):
        """Real-run audit (2026-04-25): every TestCaseResult had
        cost_usd=None despite agentic conversational tests genuinely
        spending $0.03-0.10 each (user simulator turns + rubric judge
        + tool_runner picking). Lock the aggregation: per-test cost
        must sum harness + simulator + judge + tool_runner."""
        import inspect
        from puzzleeval.agents import implement_test_env as ite

        # _promote_verdict_to_tcr is nested inside run_implement_test_env_agent
        src = inspect.getsource(ite.run_implement_test_env_agent)

        # The function signature includes the new tool_runner_cost_usd arg
        assert "tool_runner_cost_usd: float = 0.0" in src

        # The aggregation logic reads simulator + judge from verdict_detail
        assert 'verdict_detail.get("simulator_cost_usd")' in src
        assert 'verdict_detail.get("judge_cost_usd")' in src

        # Tool runner cost is added (passed through arg, not from detail)
        assert "tool_runner_cost_usd or 0.0" in src

        # Latency from plugin's total_duration_s
        assert 'verdict_detail.get("total_duration_s")' in src

        # Call site must pass verdict.cost_usd as tool_runner_cost_usd
        assert "tool_runner_cost_usd=float(verdict.cost_usd or 0.0)" in src

    def test_custom_tool_results_capture_exit_code_and_metadata(self):
        """Source-grep guard: the custom-tool dispatch path must capture
        exit_code, full 2000-char traceback tail, and per-tool
        metadata (write_file path/chars/preview, patch_file diff hint,
        run_code command). Pre-NEW-AM the result was truncated to 500
        chars (cut off tracebacks) and exit_code was dropped."""
        import inspect
        from puzzleeval.agents import implement_test_env as ite

        src = _agent5_combined_source()
        # Capture must include exit_code + tail-truncation + content-len fields
        for marker in ('"exit_code"', '"is_error"', '"result_length"',
                       '"wrote_path"', '"wrote_chars"', '"wrote_preview"',
                       '"patch_diff_chars"', '"command"',
                       'result_text[-2000:]'):
            assert marker in src, (
                f"custom-tool dispatch path missing diagnostic field "
                f"{marker!r} — the whole point of NEW-AM enrichment is "
                f"to surface these on every tool call."
            )

    def test_pipeline_runner_emits_url_and_query_in_per_turn_message(self):
        """Source-grep guard on the API layer: the SSE agent_activity
        message for build_turn events must surface URL/query/file
        descriptors, not just generic phase labels. Lock the friendly
        formatters."""
        from pathlib import Path
        import os

        # Locate puzzleeval-api/services/pipeline_runner.py from this repo
        repo_root = Path(__file__).resolve().parent.parent.parent
        runner_path = repo_root / "puzzleeval-api" / "services" / "pipeline_runner.py"
        if not runner_path.exists():
            import pytest
            pytest.skip(f"pipeline_runner.py not at expected path {runner_path}")

        src = runner_path.read_text(encoding="utf-8")

        # The build_turn handler must read the rich payload, not just tools_used.
        assert "tool_calls_detail" in src, (
            "pipeline_runner build_turn handler must read tool_calls_detail "
            "to surface URLs/queries in agent_activity messages."
        )

        # Friendly formatters for each tool family
        for keyword in ('"fetch ', '"search ', '"write ', '"run `'):
            assert keyword in src, (
                f"pipeline_runner must format {keyword!r} as a tool-action "
                "label so operators see WHAT each tool did, not just that "
                "a tool was called."
            )

        # Cumulative cost + latency surfaced in the human-readable message
        assert "cumulative_cost_usd" in src
        assert "build_turn_detail" in src, (
            "build_turn_detail structured payload must propagate to SSE "
            "so frontend can render a richer per-turn detail panel."
        )


# ==========================================================================
# Real-run 045bbd10 (OCR) exposed two bugs on 2026-04-21
# ==========================================================================


class TestAgent6EvalMaxTokensBump:
    """Real run 045bbd10 (OCR) silently reported 0% score on all
    candidates because the LLM evaluator's output was truncated
    mid-JSON. Evidence from backend log:

      "Invalid JSON: EOF while parsing a string at line 1 column 5471"
      "LLM evaluation failed after retries for Klippa DocHorizon"

    Root cause: `AGENT6_EVAL_MAX_TOKENS = 4096` with adaptive thinking
    enabled. Thinking burns 1-3K tokens (subset of max_tokens per
    Anthropic docs), leaving only 1-3K for the batch JSON output.
    8-test-case batch needs 3-5K tokens for JSON alone. Output
    truncates → parse fails → retry also truncates → terminal fail
    → all tests scored 0. Fix: bump to 16000 (matches AGENT5 pattern).
    """

    def test_max_tokens_default_is_sufficient_for_adaptive_thinking_plus_batch_json(self):
        import importlib
        import puzzleeval.config as _c
        importlib.reload(_c)
        # 16000 gives ~3K for thinking, ~13K for batch JSON output.
        # Previous 4096 budget was insufficient when both features were
        # active; proven by real run 045bbd10.
        assert _c.AGENT6_EVAL_MAX_TOKENS >= 16000, (
            f"AGENT6_EVAL_MAX_TOKENS must be >= 16000 to accommodate "
            f"adaptive thinking (which is a subset of max_tokens per "
            f"Anthropic docs) PLUS batch JSON for ~8 test cases. "
            f"Got {_c.AGENT6_EVAL_MAX_TOKENS}. Prior 4096 caused "
            f"silent JSON truncation on real run 045bbd10 → all "
            f"tests scored 0/100."
        )

    def test_max_tokens_respects_env_override(self, monkeypatch):
        """Users can tune down if their evals are simpler."""
        import importlib
        import puzzleeval.config as _c
        monkeypatch.setenv("PUZZLEEVAL_AGENT6_EVAL_MAX_TOKENS", "8192")
        importlib.reload(_c)
        assert _c.AGENT6_EVAL_MAX_TOKENS == 8192
        # Restore
        monkeypatch.delenv("PUZZLEEVAL_AGENT6_EVAL_MAX_TOKENS")
        importlib.reload(_c)


class TestAgent3FOneTestPerFile:
    """Real run 045bbd10 (OCR) generated 8 test cases from 3 user
    files — 2 tests on AC-Repair.png, 3 tests on Roof-Repair.png,
    3 tests on water-damage.pdf. The staging layer dutifully copied
    each file with a _tc-NNN suffix, then ran the candidate API
    separately for each test. 5 of the 8 API calls were against
    byte-identical inputs and produced byte-identical responses —
    wasted 62% of the candidate-API budget for zero extra information.

    Root cause: Agent 3F's prompt explicitly told Claude to emit
    'Medium-variety file → 2-3 test cases on the SAME source.' The
    mental model was 'each test is a distinct API call' — which
    made sense for text-mode synthetic inputs but is wrong for
    file-mode where the API is deterministic.

    Fix: rewrite Phase 2 rule to 'ONE test per unique file with
    multi-dimensional criteria list'. Criteria already support
    weight + distinct eval_types, so multi-dimensional grading
    happens on one shared API response instead of N separate
    API calls.
    """

    def test_prompt_emits_one_test_per_unique_input_rule(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent3f" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent3f" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # Core new rule must be present in its general form (one-per-input,
        # not one-per-file — the general principle applies to every modality).
        assert "EMIT EXACTLY ONE TestCase PER UNIQUE INPUT" in src, (
            "Agent 3F prompt must state the one-per-input rule as a hard "
            "requirement. Without it, Claude reverts to multi-tests-per-input "
            "and burns redundant candidate-provider API calls. The rule is "
            "phrased generally (PER UNIQUE INPUT) rather than PER FILE so "
            "the principle applies to every modality that reads this prompt, "
            "not just file-based OCR."
        )

    def test_prompt_explains_why_multi_tests_per_input_add_no_info(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent3f" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent3f" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # Generalization guard: the "why" must cover BOTH deterministic and
        # non-deterministic APIs. Earlier version said only "deterministic"
        # which left a loophole for Claude to justify multi-tests on
        # generative APIs. Real reason: PuzzleEval's architecture evaluates
        # criteria against the single response from each test call, so
        # running the same input twice doesn't sample variance into scoring
        # regardless of API determinism.
        assert "Deterministic APIs" in src and "Non-deterministic APIs" in src, (
            "Prompt must explicitly address BOTH deterministic and "
            "non-deterministic API modalities in its justification. "
            "Earlier wording only named determinism, which left a "
            "loophole for non-deterministic APIs to re-justify "
            "multi-tests-per-input. Generalize the reasoning: the "
            "architecture doesn't sample variance into criterion "
            "scoring in any mode, so multi-tests-per-input wastes "
            "API calls regardless of the upstream API's determinism."
        )

    def test_prompt_retains_criteria_multi_dimensional_grading(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent3f" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent3f" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # The replacement for multi-tests is multi-criteria in one test.
        # Prompt must explicitly tell Claude the criteria list sizes.
        assert (
            "4-6 criteria" in src
            and "3+ dimensions visible" in src
        ), (
            "Prompt must guide Claude to use more criteria per test "
            "(not more tests) for rich files. Otherwise the cost-"
            "savings of one-test-per-file come at the cost of losing "
            "multi-dimensional grading."
        )

    def test_prompt_teaches_gap_reporting_for_cross_agent_coverage(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent3f" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent3f" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # When files don't cover all dimensions, Agent 3F must flag
        # the gap so text-mode Agent 3 can generate synthetic fillers.
        assert (
            "coverage_summary.gaps" in src
            and "synthetic" in src.lower()
        ), (
            "Prompt must teach Agent 3F to report coverage gaps "
            "honestly so text-mode Agent 3 knows which dimensions "
            "need synthetic tests. Without this, a run with sparse "
            "user files silently under-covers the dimension matrix."
        )

    def test_prompt_drops_old_multi_tests_per_file_rule(self):
        """Regression guard: the old rule text must not reappear."""
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent3f" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent3f" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # The old "Medium-variety file → 2-3 test cases" rule should be gone.
        assert "Medium-variety file \u2192 2-3 test cases" not in src, (
            "Old multi-tests-per-file rule re-introduced — this "
            "wastes deterministic-API calls. See real run 045bbd10."
        )
        # And the aim: 1.5-2x file count
        assert "1.5-2x the file count" not in src, (
            "Old 1.5-2x count target reintroduced. New target is "
            "`unique_file_count - off_topic_files`."
        )

    def test_prompt_teaches_near_duplicate_collapse(self):
        """When files are near-duplicates, emit one TestCase, not N."""
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent3f" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent3f" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        assert "near_duplicates" in src or "near-duplicate" in src.lower(), (
            "Prompt must tell Claude how to handle near-duplicate "
            "user files (collapse to one test + report duplicate set)."
        )


class TestBuilderTurnEfficiencyRules:
    """Audit of conversation_log.json across 5 real builds (runs
    b79d79b5 + 045bbd10) found three recurring turn-waste patterns:

      1. Sequential scaffold writes (requirements.txt then harness.py
         then smoke_test.py etc.) when they're independent files that
         could all land in one parallel-write turn. Waste: ~3 turns per
         build × ~$0.08 = ~$0.24 per build.

      2. Fragmented probe scripts during debugging (Klippa wrote 5
         small probes across 8 turns instead of 1-2 comprehensive
         probes). Waste: ~3 turns × $0.07 = $0.21 on complex APIs.

      3. Tiny consecutive patches to the same file addressing one
         logical bug (ElevenLabs turns 12-14 were three serial patches
         to harness.py, one logical fix). Waste: 2 turns × $0.07 = $0.14.

    Veryfi (5 turns / $0.87) is the gold-standard proof that
    parallel-write scaffolding is achievable; the others averaged
    12-19 turns for similar-complexity APIs due to these patterns.

    These regression tests lock each rule into the builder prompt so
    future Claude instances reliably follow them.
    """

    def _read_builder_prompt(self) -> str:
        return _builder_prompt_text()

    def test_scaffold_phase_parallelism_is_soft_guidance(self):
        """Rule 1 (softened per plan §4.1): requirements.txt + harness.py +
        smoke_test.py + live_test.py SHOULD land as parallel write_file
        calls in one turn during scaffold phase, but the rule is now
        guidance with an explicit escape hatch — NOT a hard mandate.
        Real-run evidence-backed nudge, not a policy.

        NEW-AM v7 update (2026-04-25): the rule was HARDENED for
        Phase 1 (Sonnet must NOT write code files) but stays SOFT
        for Phase 2 (Opus parallel-writes are encouraged). The
        update was triggered by trace a4860e94 evidence: when
        Sonnet wrote harness.py via parallel-writes, the harness
        had subtly-wrong session.update body shape and a trivial
        live_test → 0/5 real tests. The Phase 1 hard constraint
        ensures Opus always writes code files."""
        src = self._read_builder_prompt()
        parallel_section = src.split("<use_parallel_tool_calls>")[1].split(
            "</use_parallel_tool_calls>")[0]
        # NEW-AM v7: rule split into Phase 1 (hard constraint) +
        # Phase 2 (parallelism encouraged). Lock both halves.
        assert "PHASE 1 SONNET FORBIDDEN" in parallel_section, (
            "Phase 1 must explicitly FORBID Sonnet from writing code "
            "files (harness.py / smoke_test.py / live_test.py / "
            "requirements.txt). Without this, Sonnet's parallel-writes "
            "optimization bypasses the model-switch architecture and "
            "produces broken code (real-run evidence: trace a4860e94)."
        )
        assert "PHASE 2 OPUS PARALLEL-WRITES" in parallel_section, (
            "Phase 2 (Opus) must still encourage parallel-writes for "
            "the scaffold (requirements + harness + smoke + live in "
            "one turn). The parallel-writes optimization is correct "
            "for the model that actually owns code generation."
        )
        # Phase 1 rule is now a HARD CONSTRAINT (not soft guidance)
        assert "HARD CONSTRAINT" in parallel_section, (
            "Phase 1 rule must be labeled HARD CONSTRAINT to prevent "
            "Sonnet from making 'soft guidance' interpretation that "
            "leads to writing harness.py."
        )
        # Real-run evidence cited
        assert "trace a4860e94" in parallel_section
        # Escape hatch must be explicit so Claude knows sequential is
        # acceptable when genuinely needed. Without it the softening
        # is ambiguous and models may default to parallel anyway.
        assert "ESCAPE HATCH" in parallel_section, (
            "Softened scaffold rule must include an explicit ESCAPE "
            "HATCH clause documenting when sequential writes are "
            "acceptable. Without this, 'guidance' reads as 'mandate "
            "with softer words' and Claude ignores its own reasoning."
        )
        # Must still name the specific files so Claude can pattern-match.
        for filename in ("requirements.txt", "harness.py",
                         "smoke_test.py", "live_test.py"):
            assert filename in parallel_section, (
                f"Scaffold parallelism rule must still explicitly name "
                f"{filename!r} in the preferred-parallel list even "
                f"after softening — the file list is the concrete "
                f"pattern Claude recognizes."
            )

    def test_scaffold_rule_has_concrete_real_run_evidence(self):
        """Rule 1 must cite real-run evidence (Veryfi vs others) so
        Claude sees the pattern isn't theoretical. Concrete numbers
        anchor the rule far better than abstract efficiency advice.
        Survives the Gate A softening — the evidence is what sells
        the guidance when it's no longer a mandate."""
        src = self._read_builder_prompt()
        # The rule should reference Veryfi's 5-turn / $0.87 achievement.
        assert "Veryfi" in src and "5 turns" in src, (
            "Scaffold-phase rule must cite the Veryfi real-run "
            "evidence (5 turns / $0.87) as proof that parallelism "
            "is achievable. Abstract advice isn't sticky; a concrete "
            "success case is — especially after softening from "
            "mandate to guidance."
        )

    def test_probe_script_consolidation_rule_present(self):
        """Rule 2: one comprehensive probe, not N fragmented probes.
        Hard budget of 2 probe scripts per candidate."""
        src = self._read_builder_prompt()
        inv_section = src.split(
            "<investigate_comprehensively>"
        )[1].split("</investigate_comprehensively>")[0]
        assert "Probe-script consolidation (MANDATORY" in inv_section, (
            "<investigate_comprehensively> must contain a MANDATORY "
            "probe-consolidation sub-rule. Generic 'one script not "
            "five' advice was present in the old prompt; Claude "
            "still wrote 5 probe scripts on Klippa. The new rule "
            "must be explicitly MANDATORY and budget-bound."
        )
        # Must state the numerical budget so Claude self-enforces.
        assert "at most TWO probe scripts" in inv_section, (
            "Probe-consolidation rule must state a HARD NUMERICAL "
            "BUDGET (at most 2 probes per candidate). Soft language "
            "like 'prefer one' produced 5-probe sprawls in real runs."
        )

    def test_probe_rule_has_concrete_real_run_evidence(self):
        """Rule 2 must cite the Klippa 5-probe real-run waste so
        Claude doesn't dismiss the rule as abstract over-caution."""
        src = self._read_builder_prompt()
        inv_section = src.split(
            "<investigate_comprehensively>"
        )[1].split("</investigate_comprehensively>")[0]
        assert "Klippa" in inv_section and "5 probe scripts" in inv_section, (
            "Probe-consolidation rule must cite the Klippa real-run "
            "waste (5 probe scripts, $0.40 wasted) so Claude sees "
            "the failure mode is real, not theoretical."
        )

    def test_patch_consolidation_section_exists(self):
        """Rule 3: new <consolidate_related_patches> section."""
        src = self._read_builder_prompt()
        assert "<consolidate_related_patches>" in src, (
            "Builder prompt must contain a new "
            "<consolidate_related_patches> section addressing the "
            "fragmented-patch waste pattern observed on ElevenLabs "
            "turns 12-14 (3 serial patches, same file, one logical fix)."
        )
        # Section must mandate bundling KNOWN related patches in one turn
        # but NOT deferring the first patch waiting for hypothetical edits.
        sec = src.split("<consolidate_related_patches>")[1].split(
            "</consolidate_related_patches>")[0]
        assert "ask" in sec.lower() and "already know" in sec.lower(), (
            "Patch-consolidation rule must include the decision rule: "
            "after first patch, ask if other related edits are "
            "ALREADY KNOWN (not hypothetical). Without this, Claude "
            "could over-apply the rule and defer legitimate first "
            "patches waiting for future patches that never materialize."
        )

    def test_patch_consolidation_warns_against_over_deferring(self):
        """Rule 3 must be risk-free: it must NOT tell Claude to defer
        an obvious first patch waiting for hypothetical future patches.
        The rule applies to KNOWN related edits, not speculative ones."""
        src = self._read_builder_prompt()
        sec = src.split("<consolidate_related_patches>")[1].split(
            "</consolidate_related_patches>")[0]
        # Key risk mitigation: explicitly state what the rule does NOT ask.
        assert "does NOT ask" in sec and "DEFER" in sec, (
            "Patch-consolidation rule must include an explicit "
            "'what this does NOT ask' clause to prevent Claude from "
            "over-applying it by deferring legitimate first patches. "
            "Without the guard, the rule becomes a performance risk."
        )

    def test_patch_consolidation_has_concrete_real_run_evidence(self):
        """Rule 3 must cite the ElevenLabs turn 12-14 real-run waste."""
        src = self._read_builder_prompt()
        sec = src.split("<consolidate_related_patches>")[1].split(
            "</consolidate_related_patches>")[0]
        assert "ElevenLabs" in sec and ("turns 12-14" in sec or "12-14" in sec), (
            "Patch-consolidation rule must cite the ElevenLabs "
            "real-run waste (turns 12-14: three serial patches, one "
            "logical fix) as concrete evidence."
        )

    def test_all_three_rules_are_general_not_modality_specific(self):
        """Regression guard: rules must apply broadly, not be scoped
        to voice/OCR/etc. Check they don't accidentally carve out
        exceptions for specific modalities."""
        src = self._read_builder_prompt()
        # Pull each rule section
        sections = [
            src.split("<use_parallel_tool_calls>")[1].split(
                "</use_parallel_tool_calls>")[0],
            src.split("<investigate_comprehensively>")[1].split(
                "</investigate_comprehensively>")[0],
            src.split("<consolidate_related_patches>")[1].split(
                "</consolidate_related_patches>")[0],
        ]
        # No rule should carve out modality-specific exceptions like
        # "except for voice" or "skip for OCR" which would defeat
        # the generalization intent.
        banned = [
            "except for voice", "except for OCR", "skip for voice",
            "skip for OCR", "only for voice", "only for OCR",
        ]
        for section in sections:
            for b in banned:
                assert b.lower() not in section.lower(), (
                    f"Rule contains modality-specific carve-out: "
                    f"{b!r}. Rules must be general — specific "
                    f"failure modes can be cited as evidence but "
                    f"the RULE itself must apply broadly."
                )


class TestGateCRuntimePatchFragmentationNudge:
    """Gate C — runtime nudge for patch fragmentation (plan §4.2).

    The prompt-side <consolidate_related_patches> rule (already tested
    in ``TestBuilderTurnEfficiencyRules``) teaches the pattern
    statically. Gate C adds a RUNTIME component: when the builder
    demonstrably nibbles the same file with two small consecutive
    patches, inject a soft one-time nudge suggesting parallel edits
    for the next bug on that file.

    Runtime enforcement > prompt-only teaching because Claude may
    violate a prompt rule under cognitive load; the runtime detector
    catches the behavior deterministically and surfaces it with
    concrete filename context.

    These tests lock in:
      - Detection logic (last 2 real turns, same file, small, exactly-one
        patch each) returns the right filename.
      - Per-file one-shot behavior (nudge fires at most once per file
        per build).
      - Bookkeeping entries (save-docs, verify-N) don't poison detection.
      - Config flag + token-ceiling knob exist.
      - Nudge text is soft (advisory, not a mandate) — preserves the
        Gate A softening philosophy.
      - Injection is runtime, not prompt-static.
    """

    def _import_detector(self):
        from puzzleeval.agents.implement_test_env import (
            _detect_patch_fragmentation_pattern,
        )
        return _detect_patch_fragmentation_pattern

    def _patch_turn(self, turn_idx: int, filename: str,
                    output_tokens: int = 200) -> dict:
        """Build a minimal turn_log entry that looks like a small
        single-patch turn to the detector."""
        return {
            "turn": turn_idx,
            "stop_reason": "tool_use",
            "output_tokens": output_tokens,
            "tool_calls": [
                {
                    "tool": "patch_file",
                    "id": f"toolu_{turn_idx}",
                    "input": {
                        "filename": filename,
                        "old_string": "foo = 1",
                        "new_string": "foo = 2",
                    },
                }
            ],
            "tool_results": [],
            "text": "",
        }

    def test_detects_two_small_same_file_patches(self):
        """Happy path: last 2 real turns both patched harness.py with
        < 600-token outputs → detector returns 'harness.py'."""
        detect = self._import_detector()
        conv_log = [
            self._patch_turn(5, "harness.py", output_tokens=180),
            self._patch_turn(6, "harness.py", output_tokens=200),
        ]
        result = detect(conv_log, already_nudged_files=set(),
                        output_token_ceiling=600)
        assert result == "harness.py", (
            "Two consecutive small patches to the same file must be "
            "detected and return the filename. Otherwise the runtime "
            "nudge never fires and the plan §4.2 intent is lost."
        )

    def test_ignores_already_nudged_files(self):
        """One-shot-per-file guarantee: if harness.py was already
        nudged, a second fragmentation run on the same file returns
        None. Prevents nudge spam on legitimately long debug sessions
        (the second run is just the builder legitimately still
        working on the file; nudging again adds no info)."""
        detect = self._import_detector()
        conv_log = [
            self._patch_turn(7, "harness.py"),
            self._patch_turn(8, "harness.py"),
        ]
        result = detect(conv_log, already_nudged_files={"harness.py"},
                        output_token_ceiling=600)
        assert result is None, (
            "Files already in already_nudged_files must NOT trigger "
            "the nudge again — at-most-once per file per build is "
            "the whole point."
        )

    def test_ignores_large_turns(self):
        """Rewrite-style patches naturally produce larger outputs (the
        builder writes the full new section + rationale). Those
        aren't 'fragmentation' — they're legitimate single-commit
        work. Detector must not flag them."""
        detect = self._import_detector()
        conv_log = [
            self._patch_turn(3, "harness.py", output_tokens=180),
            self._patch_turn(4, "harness.py", output_tokens=900),
        ]
        result = detect(conv_log, already_nudged_files=set(),
                        output_token_ceiling=600)
        assert result is None, (
            "A large output in either of the last 2 turns means the "
            "patch was substantial; fragmentation detection must "
            "require BOTH turns small so rewrite-style commits are "
            "not false-flagged."
        )

    def test_ignores_different_files(self):
        """Two small patches to DIFFERENT files are exactly what we
        want (parallel-ish progress across the codebase). Detector
        must only fire when the same file is being nibbled."""
        detect = self._import_detector()
        conv_log = [
            self._patch_turn(1, "harness.py", output_tokens=200),
            self._patch_turn(2, "live_test.py", output_tokens=200),
        ]
        result = detect(conv_log, already_nudged_files=set(),
                        output_token_ceiling=600)
        assert result is None, (
            "Patches to DIFFERENT files are not fragmentation — "
            "that's healthy distributed progress. Detector must "
            "require same-file."
        )

    def test_skips_bookkeeping_entries_between_real_turns(self):
        """conversation_log contains bookkeeping rows like
        'save-docs-N' and 'verify-N' that lack output_tokens and
        use string turn IDs. Those must not break detection when
        they appear BETWEEN two real small-patch turns."""
        detect = self._import_detector()
        conv_log = [
            self._patch_turn(10, "harness.py", output_tokens=190),
            # Bookkeeping entry as the API would produce it
            {
                "turn": "save-docs-10",
                "stop_reason": "docs_saved",
                "text": "Saved fetched docs",
                "tool_calls": [],
                "tool_results": [],
            },
            self._patch_turn(11, "harness.py", output_tokens=210),
        ]
        result = detect(conv_log, already_nudged_files=set(),
                        output_token_ceiling=600)
        assert result == "harness.py", (
            "Bookkeeping rows (save-docs-N, verify-N) must be "
            "transparently skipped — they're not 'real turns'. "
            "Without this, the detector silently never fires "
            "because real and bookkeeping entries interleave in "
            "production conversation logs."
        )

    def test_ignores_turn_with_multiple_tool_calls(self):
        """A turn that patches harness.py AND runs run_code is genuine
        iterate-and-verify — the agent is checking the patch effect
        immediately. Detector must NOT flag that as fragmentation.
        Nudging healthy iterate-and-verify would be counterproductive."""
        detect = self._import_detector()
        mixed_turn = self._patch_turn(20, "harness.py", output_tokens=200)
        mixed_turn["tool_calls"].append({
            "tool": "run_code",
            "id": "toolu_21",
            "input": {"command": "python smoke_test.py"},
        })
        conv_log = [
            self._patch_turn(19, "harness.py", output_tokens=200),
            mixed_turn,
        ]
        result = detect(conv_log, already_nudged_files=set(),
                        output_token_ceiling=600)
        assert result is None, (
            "Mixed-tool turns (patch + run_code) are iterate-and-"
            "verify cycles, not fragmentation. Detector must "
            "require EACH flagged turn to have exactly one "
            "patch_file call as the sole non-advisor tool."
        )

    def test_config_flag_and_ceiling_exist(self):
        """The runtime gate must be flag-able for A/B testing. Two
        knobs are required: an enable flag and a token ceiling."""
        from puzzleeval.config import (
            AGENT5_PATCH_FRAGMENT_NUDGE_ENABLED,
            AGENT5_PATCH_FRAGMENT_TOKEN_CEILING,
        )
        assert isinstance(AGENT5_PATCH_FRAGMENT_NUDGE_ENABLED, bool)
        assert isinstance(AGENT5_PATCH_FRAGMENT_TOKEN_CEILING, int)
        assert AGENT5_PATCH_FRAGMENT_TOKEN_CEILING >= 0, (
            "Token ceiling must be >= 0; 0 effectively disables "
            "while keeping the plumbing hot for A/B testing."
        )

    def test_nudge_is_runtime_not_prompt_static(self):
        """The nudge MUST be injected at runtime (per plan §4.2) —
        NOT statically embedded in BUILDER_SYSTEM_PROMPT where it
        would fire on every turn regardless of conversation shape.
        Source-grep guard: the operation tag must appear in
        implement_test_env.py and the nudge text must be built
        inline from the detected filename, not present in a
        constant block."""
        src = _agent5_combined_source()
        assert '"operation": "patch_fragmentation_nudge"' in src, (
            "Gate C nudge must log via "
            "operation='patch_fragmentation_nudge' so operators can "
            "grep runs for nudge events and measure effectiveness."
        )
        # Must call the detector from the per-turn loop, not anywhere
        # else (e.g., not in a prompt builder — that would make it
        # prompt-static again).
        assert "_detect_patch_fragmentation_pattern(" in src, (
            "Runtime detector must be invoked from the builder loop "
            "for Gate C to be runtime-driven. A static reference in "
            "the prompt would defeat the point."
        )


class TestAskResearchContextInheritance:
    """ask_research overhaul (plan §4.3): the sub-agent used to be a
    last-resort escape hatch with a thin generic prompt. It has been
    repositioned as a PEER INTEGRATION ENGINEER with:

      1. Auto-inherited context (api_spec excerpt, recent errors,
         harness code, provider details) — already wired at dispatch.
      2. A two-regime prompt: targeted (specific question) vs
         exploratory (open-ended / thin context).
      3. Budget bump 2+2 → 3+3 to support exploratory mode's
         multi-angle search.
      4. Tool description that invites normal use (not last resort)
         and explicitly trusts NOT-FOUND failures.

    These tests lock in the four changes so a future prompt refactor
    can't silently degrade the capability back to 'last resort only'.

    Six cases:
      (a) Dispatch enriches the question with api_spec excerpt
      (b) Dispatch includes recent tool-result errors
      (c) Dispatch attaches harness code snippet (first 40 lines)
      (d) Sub-agent prompt teaches Regime A (targeted) explicitly
      (e) Sub-agent prompt teaches Regime B (exploratory) explicitly
      (f) Tool description repositions from last-resort to normal use
    """

    def _read_agent5_src(self) -> str:
        return _agent5_combined_source()

    def test_dispatch_injects_api_spec_excerpt(self):
        """The ask_research dispatch path reads api_spec.txt (if
        present) and threads the first 2K chars + DOC_REFERENCES +
        DOC_MAP sections into the question. Without this the
        sub-agent re-derives what's already known and wastes budget."""
        src = self._read_agent5_src()
        # The enrichment block must reference the spec file and the
        # full_spec slice. Allow either the exact comment phrase or
        # the code that reads + slices the file.
        assert 'api_spec.txt' in src and 'full_spec = spec_path.read_text' in src, (
            "ask_research dispatch must read api_spec.txt and pass "
            "excerpts to the sub-agent (plan §4.3.1). Without this "
            "the sub-agent gets a bare question and re-searches "
            "for info already in the spec."
        )
        # Must slice to first 2K chars so the context stays bounded
        # (large spec files would blow the sub-agent's budget).
        assert "spec_summary = full_spec[:2000]" in src, (
            "Spec excerpt must be capped at 2000 chars so the "
            "sub-agent's context stays bounded. Without a cap, a "
            "10K+ char api_spec would dominate the sub-agent's "
            "input and crowd out the actual question."
        )

    def test_dispatch_captures_recent_error_context(self):
        """The dispatch path injects ``LAST ERROR CONTEXT: ...`` when
        prior tool results this turn contain error output. The
        sub-agent uses this to target its search."""
        src = self._read_agent5_src()
        assert "LAST ERROR CONTEXT:" in src, (
            "ask_research dispatch must surface the last error "
            "text to the sub-agent. A bare question without error "
            "context produces generic answers instead of targeted "
            "debugging help."
        )

    def test_dispatch_attaches_harness_code_snippet(self):
        """The dispatch path reads the current harness.py and
        includes the first 40 lines so the sub-agent can ground its
        answer against real code, not invent hypothetical signatures."""
        src = self._read_agent5_src()
        assert "CURRENT HARNESS CODE (first 40 lines)" in src, (
            "ask_research dispatch must attach the first 40 lines "
            "of harness.py so the sub-agent sees the real code "
            "shape. Without it, answers tend to cite idealized API "
            "signatures that don't match the builder's live code."
        )
        # The dispatch should use _read_harness_code to load from disk
        # rather than trusting the accumulated conversation state.
        assert "_read_harness_code(sandbox_dir)" in src, (
            "Dispatch must call _read_harness_code(sandbox_dir) — "
            "reading from disk is the ground truth; conversation "
            "state may lag."
        )

    def test_sub_agent_prompt_teaches_regime_a_targeted(self):
        """The TARGETED_RESEARCH_SYSTEM prompt must explicitly name
        'Regime A' or equivalent signal for targeted questions with
        a specific feature/flag/endpoint. Without named regimes,
        the sub-agent can't self-classify and defaults to one
        search pattern for every query shape."""
        from puzzleeval.agents.implement_test_env import (
            TARGETED_RESEARCH_SYSTEM,
        )
        assert "REGIME A" in TARGETED_RESEARCH_SYSTEM.upper(), (
            "Two-regime prompt must name REGIME A explicitly so the "
            "sub-agent self-classifies. Plan §4.3.2 hinges on this."
        )
        # Regime A strategy: one precise search, stop.
        assert "ONE" in TARGETED_RESEARCH_SYSTEM, (
            "Regime A strategy must teach ONE precise search (not "
            "budget-blowing multi-search). Targeted mode is the "
            "cost-saver half of the two-regime design."
        )

    def test_sub_agent_prompt_teaches_regime_b_exploratory(self):
        """The prompt must explicitly teach the exploratory regime
        for open-ended questions or thin context. Cover the multi-
        angle strategy (docs / GitHub / community / archive) so the
        sub-agent spreads its budget instead of repeating the same
        search."""
        from puzzleeval.agents.implement_test_env import (
            TARGETED_RESEARCH_SYSTEM,
        )
        assert "REGIME B" in TARGETED_RESEARCH_SYSTEM.upper(), (
            "Two-regime prompt must name REGIME B for exploratory "
            "mode (plan §4.3.2). Without it, thin-context questions "
            "get the same single-search treatment as targeted ones "
            "and come back with poor answers."
        )
        # Regime B should reference multi-angle sources explicitly —
        # the prompt teaches the Google-like spread (docs + GitHub +
        # community + archive).
        for source in ("github", "stackoverflow", "archive"):
            assert source.lower() in TARGETED_RESEARCH_SYSTEM.lower(), (
                f"Regime B multi-angle strategy must include "
                f"{source!r} as a search channel. Missing channels "
                f"mean the sub-agent repeats the official-docs "
                f"search and returns duplicate results."
            )
        # Must also teach the honest-failure output format so
        # exploratory mode doesn't fabricate an answer when
        # research genuinely turned up nothing.
        assert "NOT FOUND" in TARGETED_RESEARCH_SYSTEM, (
            "Prompt must explicitly authorize 'NOT FOUND' output "
            "when research turns up nothing. Without it, the "
            "sub-agent hallucinates confident-sounding answers to "
            "avoid looking useless — exactly what we don't want."
        )

    def test_tool_description_repositions_from_last_resort(self):
        """The ASK_RESEARCH_TOOL description used to frame the tool
        as a last resort. The overhaul repositions it as a normal
        peer-engineer tool to be used whenever a specific question
        would otherwise be googled. Lock the new framing."""
        from puzzleeval.agents.implement_test_env import ASK_RESEARCH_TOOL
        description = ASK_RESEARCH_TOOL["description"]
        # The peer-engineer framing + 'not a last resort' guidance.
        assert "peer integration engineer" in description.lower(), (
            "Tool description must use peer-engineer framing (plan "
            "§4.3.4). Prior 'last resort' framing caused underuse — "
            "the builder suffered through Google-able questions "
            "because the tool read as forbidden unless desperate."
        )
        assert "not a last resort" in description.lower(), (
            "Tool description must explicitly negate the last-"
            "resort framing. Without a direct negation the "
            "implicit framing from earlier versions of Agent 5's "
            "prompt history bleeds through."
        )
        # Cost transparency — the builder should know the price so
        # it doesn't under/over-use.
        assert "$0." in description, (
            "Tool description must quote the per-call cost so the "
            "builder can compare against probe-script alternatives."
        )
        # Budget tightened 3→2 (NEW-AM v5, post real-run trace
        # a4860e94 deep-dive). Real-run measurement: ElevenLabs
        # build's Sonnet T2 fired ask_research and the sub-agent took
        # 487 SECONDS (~8 min) — 60% of build's wall-clock — researching
        # one auxiliary endpoint shape. With max_uses=3+3=6 server
        # tools, sub-agent had headroom for exhaustive multi-angle
        # exploration. Capping to 2+2 forces shorter focused answers;
        # build agent can re-call ask_research with refined question
        # if first answer didn't suffice (cheaper than one massive
        # 8-min call).
        # Phase 3.2 migration: _run_targeted_research moved to
        # puzzleeval/agents/agent5/research_subagent.py. The tool budget
        # invariant lives in the new canonical location.
        src = (
            ROOT / "puzzleeval" / "agents" / "agent5" / "research_subagent.py"
        ).read_text(encoding="utf-8")
        assert '"max_uses": 2' in src, (
            "Sub-agent tool budget must be 2 (NEW-AM v5 — capped "
            "to prevent the 8-min ask_research stalls observed in "
            "real-run trace a4860e94). If this regresses to 3 the "
            "exhaustive exploration mode comes back."
        )
        # Anti-regression — must NOT have the prior 3+3 budget for
        # ask_research's web tools
        # (other places in the file may have max_uses=3 for the
        # builder's OWN web tools — that's fine; what we're locking
        # is the sub-agent's tools at the targeted_research call site)
        # Find the sub-agent's tool block (LAST `max_content_tokens=10000`
        # — the FIRST is WEB_FETCH_TOOL constant for the builder's own
        # web tools; the SECOND is inside the ask_research sub-agent's
        # tools=[] list).
        sub_agent_block_idx = src.rfind('"max_content_tokens": 10000')
        assert sub_agent_block_idx > 0, "ask_research sub-agent block not found"
        block_start = src.rfind("tools=[", 0, sub_agent_block_idx)
        block_end = src.find("]", sub_agent_block_idx)
        sub_agent_block = src[block_start:block_end]
        assert '"max_uses": 2' in sub_agent_block, (
            "ask_research sub-agent tool block must use max_uses=2 "
            "(see NEW-AM v5 above)"
        )
        assert '"max_uses": 3' not in sub_agent_block, (
            "ask_research sub-agent tool block must NOT use max_uses=3 "
            "(reverting to prior wide budget brings back 8-min stalls)"
        )


class TestAgent1TestCountTargetDerivation:
    """Real runs b79d79b5 (voice) and 045bbd10 (OCR) both produced
    exactly 8 test cases, even though the first had no user files
    and the second had 3 user files. The '8' was hardcoded in two
    places: the Agent 1 worked example ("test_count_target": 8 for
    the OCR scope) and the rule ("default 7, increase for complex
    scopes"). Claude anchored on the worked-example number and
    produced 8 regardless of actual input signals.

    Fix: rewrite Agent 1's test_count_target rule so Claude DERIVES
    the count from real signals — file count (file mode), coverage-
    dimension count (text mode) — and REQUIRE it to show derivation
    in notes. Also change the worked example to demonstrate
    derivation rather than anchor a number.
    """

    def test_rule_mandates_derivation_not_gut_feel(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent1" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent1" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # Rule must explicitly forbid gut-feel numbers.
        assert "DO NOT pick a gut-feel number" in src, (
            "Agent 1 prompt must explicitly prohibit gut-feel "
            "test_count_target values. Without the prohibition, "
            "Claude anchors on remembered example numbers (8 in "
            "prior runs) regardless of actual signals."
        )

    def test_file_mode_rule_ties_count_to_file_count(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent1" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent1" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # File-mode rule must tie to file count.
        assert "len(attached_files)" in src, (
            "File-mode test_count_target rule must explicitly state "
            "`test_count_target = len(attached_files)` so Agent 1 "
            "syncs with Agent 3F's one-test-per-unique-input rule. "
            "Without this alignment, Agent 1 asks for more tests "
            "than files exist and the validator flags "
            "'undergenerated' on every file-mode run."
        )

    def test_text_mode_rule_ties_count_to_coverage_dimensions(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent1" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent1" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # Text-mode rule must anchor to coverage dimensions, not a number.
        assert "canonical 6 dimensions" in src, (
            "Text-mode test_count_target rule must anchor to the "
            "6 canonical coverage dimensions (happy_path, "
            "input_variation, edge_case, scale, domain_specific, "
            "error_resilience) — a principled floor of ~6 — rather "
            "than a remembered number. Named dimensions give Claude "
            "a mental model to reason from."
        )

    def test_rule_requires_derivation_shown_in_notes(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent1" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent1" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        assert "Always show your derivation in `notes`" in src, (
            "Agent 1 prompt must require Claude to SHOW its "
            "test_count_target derivation in the notes field. "
            "Without this transparency, the rule is easy to ignore "
            "silently — we can't verify from output whether Claude "
            "derived or anchored."
        )

    def test_worked_example_notes_explain_derivation(self):
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent1" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent1" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        # The worked example's notes must demonstrate derivation logic.
        assert "derived from file_description" in src, (
            "Worked example's `notes` must explain the derivation "
            "of each scope's test_count_target (file_description "
            "midpoint OR coverage-dimension count). The notes teach "
            "Claude the HOW, not just the result."
        )
        assert "derived from the 6 canonical coverage dimensions" in src, (
            "Worked example must demonstrate the text-mode "
            "derivation (one test per canonical dimension) in "
            "notes, not just assert a number."
        )

    def test_worked_example_dropped_the_test_count_target_8_anchor(self):
        """Regression guard: the number 8 must not appear as a
        test_count_target in the worked example. Claude anchored
        on this number across at least two real runs."""
        src = ((
            ROOT / "puzzleeval" /"agents" / "agent1" / "core.py").read_text(encoding="utf-8") + chr(10) + (
            ROOT / "puzzleeval" /"agents" / "agent1" / "templates" / "system_prompt.md").read_text(encoding="utf-8"))
        assert '"test_count_target": 8' not in src, (
            "Worked example must not show test_count_target=8. "
            "This number was the anchor that caused both real "
            "runs (b79d79b5 voice, 045bbd10 OCR) to produce "
            "exactly 8 tests regardless of actual signals."
        )

    def test_schema_description_explains_derivation_semantics(self):
        """Schema field description must match the new prompt rule."""
        from puzzleeval.schemas import ScopeTestSpec
        desc = ScopeTestSpec.model_fields["test_count_target"].description
        assert "DERIVES" in desc or "derived" in desc.lower(), (
            "ScopeTestSpec.test_count_target description must "
            "state that Agent 1 DERIVES the count from signals. "
            "Schema descriptions are the second layer of prompt "
            "(structured-output tool schemas are shown to Claude) "
            "so they must match the Agent 1 prompt's semantics."
        )


class TestCandidateTestRunOverallScorePersisted:
    """Real run 3eb3196a (2026-04-22) exposed that `overall_score` was
    computed inline in the SSE `candidate_results_ready` event callback
    but never persisted to `CandidateTestRun`. Consequences:

    1. Live UI during a run: reads SSE event → shows correct score ✓
    2. After refresh / reopen: reads agent_5_output.json or
       evaluation_report.json → overall_score missing from
       CandidateTestRun → `report.py::_safe_get(run, "overall_score",
       None) or 0.0` → **every candidate shows 0.0** regardless of
       actual test performance.

    Universal bug — same for voice AND OCR since both flows produce
    CandidateTestRun through `_compute_aggregate_metrics`. Frontend
    reads `overall_score` in CandidateCard.tsx, ResultsComparison.tsx,
    EvaluationReportCard.tsx — all broken on refresh.

    Fix: add `overall_score: float = Field(default=0.0, ...)` to the
    schema + compute in `_compute_aggregate_metrics` + pass through
    the constructor. SSE callback now reads the same computed value
    so live and persisted paths are a single source of truth.
    """

    def test_schema_has_overall_score_field(self):
        from puzzleeval.schemas import CandidateTestRun
        assert "overall_score" in CandidateTestRun.model_fields, (
            "CandidateTestRun must have `overall_score` field so "
            "the persisted JSON artifact carries it. Without this, "
            "frontend refresh / reopen shows 0.0 for every candidate."
        )
        field = CandidateTestRun.model_fields["overall_score"]
        assert field.default == 0.0, (
            "overall_score should default to 0.0 so legacy runs "
            "without this field load cleanly via Pydantic's "
            "backward-compat deserialization."
        )

    def test_compute_aggregate_metrics_includes_overall_score(self):
        """Empty-test-results path must include overall_score=0.0;
        populated path must compute mean of weighted_score."""
        from puzzleeval.agents.implement_test_env import _compute_aggregate_metrics
        # Empty case
        empty = _compute_aggregate_metrics([])
        assert "overall_score" in empty
        assert empty["overall_score"] == 0.0
        # Populated case — mock TestCaseResult-like objects
        from types import SimpleNamespace
        results = [
            SimpleNamespace(passed=True, success=True, skip_reason=None,
                            latency_ms=100, cost_usd=0.0, tokens_used=None,
                            weighted_score=0.9),
            SimpleNamespace(passed=False, success=True, skip_reason=None,
                            latency_ms=200, cost_usd=0.0, tokens_used=None,
                            weighted_score=0.3),
            SimpleNamespace(passed=True, success=True, skip_reason=None,
                            latency_ms=150, cost_usd=0.0, tokens_used=None,
                            weighted_score=0.75),
        ]
        metrics = _compute_aggregate_metrics(results)
        # Expected overall_score = (0.9 + 0.3 + 0.75) / 3 = 0.65
        assert abs(metrics["overall_score"] - 0.65) < 1e-3, (
            f"overall_score should be mean of weighted_score; "
            f"expected 0.65, got {metrics['overall_score']}."
        )

    def test_sse_callback_reads_from_metrics_not_recomputes(self):
        """Regression guard: the SSE `candidate_results_ready` event's
        `overall_score` must come from the same `metrics` dict that
        builds the persisted CandidateTestRun. If someone re-introduces
        an inline `sum(...) / len(...)` recomputation, the live and
        persisted values could drift again — same class of bug that
        caused real run 3eb3196a to show 0.0 on refresh."""
        src = _agent5_combined_source()
        # The banned inline pattern
        banned = 'sum(tcr.weighted_score for tcr in run.test_results) / max(len(run.test_results), 1)'
        assert banned not in src, (
            "SSE callback must not re-compute overall_score inline. "
            "Read from metrics['overall_score'] so live and persisted "
            "values come from one source. See real run 3eb3196a."
        )
        # The required pattern
        assert 'metrics.get("overall_score"' in src or 'metrics["overall_score"]' in src, (
            "SSE callback must read overall_score from metrics dict."
        )

    def test_candidate_test_run_constructor_passes_overall_score(self):
        """Regression guard: the CandidateTestRun constructor site in
        Agent 5's test-execution loop must pass overall_score from
        metrics. Without this, the schema field gets its default 0.0
        and persisted state stays broken even with the schema field
        added."""
        src = _agent5_combined_source()
        # Scan the CandidateTestRun(...) construction block in Agent 5
        idx = src.find("run = CandidateTestRun(")
        assert idx > 0
        # Look at the next ~1000 chars for the field assignment
        block = src[idx:idx + 1500]
        assert 'overall_score=metrics["overall_score"]' in block, (
            "CandidateTestRun constructor in Agent 5 must pass "
            "overall_score=metrics['overall_score']. Without this, "
            "the new schema field defaults to 0.0 and the frontend "
            "sees 0 on every refresh — same user-visible bug."
        )
