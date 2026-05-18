"""Regression tests for Agent 5 write_file gates B1, B2, B3.

Each gate has the four-test discipline from the Phase B plan:

  1. **Violation triggers gate** â€” the gate fires on a clear violation.
  2. **Near-miss does NOT trigger** â€” the gate does NOT fire on a
     legitimate boundary case (so future modalities aren't blocked).
  3. **Env-var bypass works** â€” when the corresponding flag is set to
     "0", the gate is silent and the call proceeds normally.
  4. **Structured logging fires** â€” `gate_fired` log line is emitted on
     trigger, with the documented JSON-shaped extras.

Gate B4 (pre-build research budget) lives in build_loop.py and is wired
to the API response loop; it's covered separately by an integration
test in `test_build_loop_behavior.py`.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest

from puzzleeval.agents.agent5 import dispatch_helpers
from puzzleeval.agents.agent5 import tools


# ---------------------------------------------------------------------------
# Predicate-level tests (pure functions in dispatch_helpers.py)
# ---------------------------------------------------------------------------


class TestForbiddenMetaFilenamePredicate:
    def test_violations(self):
        for name in ["NOTES.md", "notes.md", "STATUS.txt", "status.txt", "Plan.md"]:
            assert dispatch_helpers.is_forbidden_meta_filename(name), name

    def test_canonical_files_pass(self):
        for name in [
            "_agent_state/research_plan.json",
            "_agent_state/research_synthesis.json",
            "_agent_state/implementation_plan.json",
            "harness.py",
            "smoke_test.py",
            "live_test.py",
            "requirements.txt",
        ]:
            assert not dispatch_helpers.is_forbidden_meta_filename(name), name

    def test_empty_filename_does_not_match(self):
        assert not dispatch_helpers.is_forbidden_meta_filename("")


class TestIntrospectionScriptNamePredicate:
    def test_violations(self):
        for name in [
            "inspect_sdk.py",
            "check_endpoints.py",
            "explore_api.py",
            "probe_response.py",
            "INSPECT_data.py",  # case-insensitive prefix
        ]:
            assert dispatch_helpers.is_introspection_script_name(name), name

    def test_canonical_python_files_pass(self):
        for name in ["harness.py", "smoke_test.py", "live_test.py"]:
            assert not dispatch_helpers.is_introspection_script_name(name), name

    def test_non_py_with_introspection_prefix_does_not_match(self):
        # The pattern is .py-only â€” a docs file named inspect_notes.txt
        # is somebody's notes file, not a probe script.
        assert not dispatch_helpers.is_introspection_script_name("inspect_notes.txt")


class TestPhase1ScaffoldViolationPredicate:
    def test_scaffold_block_before_build_gate(self):
        for name in ["harness.py", "smoke_test.py", "live_test.py", "requirements.txt"]:
            assert dispatch_helpers.is_phase1_scaffold_violation(
                name, build_gate_accepted=False
            ), name

    def test_scaffold_allowed_after_build_gate(self):
        # After implementation_plan.json is accepted, scaffold writes are
        # exactly what's expected.
        for name in ["harness.py", "smoke_test.py", "live_test.py", "requirements.txt"]:
            assert not dispatch_helpers.is_phase1_scaffold_violation(
                name, build_gate_accepted=True
            ), name

    def test_implementation_plan_itself_is_not_a_scaffold_file(self):
        # Writing the build-gate artifact is not a scaffold write.
        assert not dispatch_helpers.is_phase1_scaffold_violation(
            "_agent_state/implementation_plan.json", build_gate_accepted=False
        )

    def test_unrelated_filename_not_blocked_before_build_gate(self):
        # If a future modality writes (say) `voice_seed.wav`, the gate
        # must not block it â€” this gate is narrowly the 4 scaffold names.
        assert not dispatch_helpers.is_phase1_scaffold_violation(
            "voice_seed.wav", build_gate_accepted=False
        )


# ---------------------------------------------------------------------------
# tools.write_file integration tests (gates wired with config flags)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_flags(monkeypatch):
    """Each test starts with all gates ON (default behavior)."""
    monkeypatch.setenv("PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES", "1")
    monkeypatch.setenv("PUZZLEEVAL_GATE_INTROSPECTION_WARN", "1")
    monkeypatch.setenv("PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK", "1")
    # Force a config reload so module-level constants reflect the env.
    import puzzleeval.config as cfg
    importlib.reload(cfg)


def _phase_state(implementation_plan_accepted: bool, slug: str = "test-cand") -> dict:
    return {
        "implementation_plan_accepted": implementation_plan_accepted,
        "build_gate_accepted": implementation_plan_accepted,
        "candidate_slug": slug,
        "trace_id": "test-trace",
    }


class TestGateB1ForbiddenFilenames:
    def test_violation_rejects(self, tmp_path: Path):
        # STATUS.txt has an allowed extension (.txt), so it reaches gate B1
        # rather than getting blocked by the extension allowlist.
        result = tools.write_file(
            {"filename": "STATUS.txt", "content": "ok"},
            tmp_path,
            phase_state=_phase_state(implementation_plan_accepted=True),
        )
        assert result.startswith("Error:"), result
        assert "meta/state-tracking" in result
        assert not (tmp_path / "STATUS.txt").exists(), "rejected file must not have been written"

    def test_near_miss_canonical_file_allowed(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "build_plan.md", "content": "Build notes\n"},
            tmp_path,
            phase_state=_phase_state(implementation_plan_accepted=False),
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "build_plan.md").exists()

    def test_env_bypass_allows_write(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES", "0")
        import puzzleeval.config as cfg
        importlib.reload(cfg)

        result = tools.write_file(
            {"filename": "notes.txt", "content": "operator override"},
            tmp_path,
            phase_state=_phase_state(implementation_plan_accepted=True),
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "notes.txt").exists()

    def test_md_filename_blocked_by_b1_when_in_forbidden_set(self, tmp_path: Path):
        # ``.md`` is now an ALLOWED extension (autonomy artifacts like
        # build_plan.md need it). Forbidden meta-files like NOTES.md are
        # still rejected â€” but by B1 (forbidden-meta-filename), not by the
        # extension allowlist. Defense in depth via filename, not via type.
        result = tools.write_file(
            {"filename": "NOTES.md", "content": "x"},
            tmp_path,
            phase_state=_phase_state(implementation_plan_accepted=True),
        )
        assert result.startswith("Error:"), result
        assert "meta/state-tracking" in result, (
            f"Expected B1 forbidden-meta-filename rejection, got: {result}"
        )

    def test_structured_logging_fires_on_reject(self, tmp_path: Path, caplog):
        with caplog.at_level(logging.WARNING, logger="puzzleeval.agents.agent5.tools"):
            tools.write_file(
                {"filename": "STATUS.txt", "content": "x"},
                tmp_path,
                phase_state=_phase_state(implementation_plan_accepted=True),
            )
        records = [r for r in caplog.records if getattr(r, "operation", "") == "gate_fired"]
        assert any(
            getattr(r, "gate_name", "") == "forbidden_meta_filename" for r in records
        ), "expected a gate_fired log with gate_name=forbidden_meta_filename"


class TestGateB3Phase1ScaffoldBlock:
    def test_violation_rejects_before_build_gate(self, tmp_path: Path):
        result = tools.write_file(
            {"filename": "harness.py", "content": "def run(x): ..."},
            tmp_path,
            phase_state=_phase_state(implementation_plan_accepted=False),
        )
        assert result.startswith("Error:"), result
        assert "implementation_plan.json" in result
        assert not (tmp_path / "harness.py").exists()

    def test_near_miss_after_build_gate_allowed(self, tmp_path: Path):
        # Once implementation_plan.json is accepted, scaffold writes are
        # exactly what's expected.
        result = tools.write_file(
            {"filename": "harness.py", "content": "def run(x): return {}"},
            tmp_path,
            phase_state=_phase_state(implementation_plan_accepted=True),
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "harness.py").exists()

    def test_legacy_caller_without_phase_state_unaffected(self, tmp_path: Path):
        # Existing callers that don't pass phase_state get original behavior.
        result = tools.write_file(
            {"filename": "harness.py", "content": "def run(x): ..."},
            tmp_path,
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "harness.py").exists()

    def test_env_bypass_allows_prebuild_scaffold(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK", "0")
        import puzzleeval.config as cfg
        importlib.reload(cfg)

        result = tools.write_file(
            {"filename": "harness.py", "content": "def run(x): ..."},
            tmp_path,
            phase_state=_phase_state(implementation_plan_accepted=False),
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "harness.py").exists()


class TestGateB2IntrospectionWarn:
    def test_warn_does_not_block_write(self, tmp_path: Path):
        # Pre-harness probe: gate should warn but allow.
        assert not (tmp_path / "harness.py").exists()
        result = tools.write_file(
            {"filename": "inspect_sdk.py", "content": "import some_sdk"},
            tmp_path,
            phase_state=_phase_state(implementation_plan_accepted=True),
        )
        assert not result.startswith("Error:"), result
        assert (tmp_path / "inspect_sdk.py").exists()

    def test_no_warn_when_harness_already_exists(self, tmp_path: Path, caplog):
        # Post-harness debugging probes are legitimate; gate should be silent.
        (tmp_path / "harness.py").write_text("def run(x): return {}", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="puzzleeval.agents.agent5.tools"):
            result = tools.write_file(
                {"filename": "probe_response.py", "content": "import harness"},
                tmp_path,
                phase_state=_phase_state(implementation_plan_accepted=True),
            )
        assert not result.startswith("Error:"), result
        introspection_warns = [
            r for r in caplog.records
            if getattr(r, "operation", "") == "gate_fired"
            and getattr(r, "gate_name", "") == "introspection_warn"
        ]
        assert introspection_warns == [], "warn should not fire when harness.py exists"

    def test_warn_fires_when_harness_missing(self, tmp_path: Path, caplog):
        # Pre-harness probe with no harness.py present: WARN fires.
        with caplog.at_level(logging.WARNING, logger="puzzleeval.agents.agent5.tools"):
            tools.write_file(
                {"filename": "explore_api.py", "content": "x = 1"},
                tmp_path,
                phase_state=_phase_state(implementation_plan_accepted=True),
            )
        warns = [
            r for r in caplog.records
            if getattr(r, "operation", "") == "gate_fired"
            and getattr(r, "gate_name", "") == "introspection_warn"
        ]
        assert warns, "warn should fire when probe is written before harness.py"
        assert getattr(warns[0], "rejected", None) is False, "warn must not reject"

    def test_env_bypass_silences_warn(self, tmp_path: Path, monkeypatch, caplog):
        monkeypatch.setenv("PUZZLEEVAL_GATE_INTROSPECTION_WARN", "0")
        import puzzleeval.config as cfg
        importlib.reload(cfg)

        with caplog.at_level(logging.WARNING, logger="puzzleeval.agents.agent5.tools"):
            tools.write_file(
                {"filename": "inspect_data.py", "content": "x = 1"},
                tmp_path,
                phase_state=_phase_state(implementation_plan_accepted=True),
            )
        warns = [
            r for r in caplog.records
            if getattr(r, "operation", "") == "gate_fired"
            and getattr(r, "gate_name", "") == "introspection_warn"
        ]
        assert warns == [], "warn must be silent when env-var disables it"


# ---------------------------------------------------------------------------
# Gate B4 (pre-build research budget) â€” predicate-level tests
# ---------------------------------------------------------------------------


class _StubServerToolUse:
    """Minimal stub mirroring response.usage.server_tool_use's shape."""

    def __init__(self, web_search_requests: int = 0):
        self.web_search_requests = web_search_requests


class _StubUsage:
    def __init__(self, server_tool_use: _StubServerToolUse | None = None):
        self.server_tool_use = server_tool_use


class _StubBlock:
    """Minimal stub mirroring response.content blocks (server_tool_use / tool_use)."""

    def __init__(self, type_: str, name: str = ""):
        self.type = type_
        self.name = name


class TestPrebuildResearchTurnDetection:
    def test_web_search_via_usage_counts(self):
        usage = _StubUsage(server_tool_use=_StubServerToolUse(web_search_requests=1))
        assert dispatch_helpers.turn_used_prebuild_research(
            [], usage, build_gate_artifact_exists=False,
        ) is True

    def test_web_fetch_block_counts(self):
        content = [_StubBlock("server_tool_use", "web_fetch")]
        assert dispatch_helpers.turn_used_prebuild_research(
            content, _StubUsage(), build_gate_artifact_exists=False,
        ) is True

    def test_ask_research_counts_in_planned_research_flow_before_build_gate(self):
        content = [_StubBlock("tool_use", "ask_research")]
        # Artifact present path: ask_research still counts as a research turn.
        assert dispatch_helpers.turn_used_prebuild_research(
            content, _StubUsage(), build_gate_artifact_exists=True,
        ) is True
        # Current planned-research flow: ask_research before build gate
        # still counts toward the pre-build research budget.
        assert dispatch_helpers.turn_used_prebuild_research(
            content, _StubUsage(), build_gate_artifact_exists=False,
        ) is True

    def test_text_only_turn_does_not_count(self):
        content = [_StubBlock("text")]
        assert dispatch_helpers.turn_used_prebuild_research(
            content, _StubUsage(), build_gate_artifact_exists=True,
        ) is False

    def test_advisor_block_does_not_count(self):
        # The advisor is a separate server tool used for tier-up
        # consultation, not for primary research. It should NOT count
        # against the pre-build research budget.
        content = [_StubBlock("server_tool_use", "advisor")]
        assert dispatch_helpers.turn_used_prebuild_research(
            content, _StubUsage(), build_gate_artifact_exists=False,
        ) is False


# ---------------------------------------------------------------------------
# Config flag round-trip (every gate flag is readable + togglable)
# ---------------------------------------------------------------------------


class TestGateConfigRoundTrip:
    def test_all_four_flags_default_on(self, monkeypatch):
        for flag in (
            "PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES",
            "PUZZLEEVAL_GATE_INTROSPECTION_WARN",
            "PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK",
            "PUZZLEEVAL_GATE_PREBUILD_RESEARCH_BUDGET",
        ):
            monkeypatch.delenv(flag, raising=False)
        import puzzleeval.config as cfg
        importlib.reload(cfg)
        assert cfg.GATE_FORBIDDEN_FILENAMES_ENABLED is True
        assert cfg.GATE_INTROSPECTION_WARN_ENABLED is True
        assert cfg.GATE_PHASE1_SCAFFOLD_BLOCK_ENABLED is True
        assert cfg.GATE_PREBUILD_RESEARCH_BUDGET_ENABLED is True
        assert cfg.GATE_PREBUILD_RESEARCH_BUDGET == 2

    def test_flags_can_be_disabled(self, monkeypatch):
        for flag in (
            "PUZZLEEVAL_GATE_FORBIDDEN_FILENAMES",
            "PUZZLEEVAL_GATE_INTROSPECTION_WARN",
            "PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK",
            "PUZZLEEVAL_GATE_PREBUILD_RESEARCH_BUDGET",
        ):
            monkeypatch.setenv(flag, "0")
        import puzzleeval.config as cfg
        importlib.reload(cfg)
        assert cfg.GATE_FORBIDDEN_FILENAMES_ENABLED is False
        assert cfg.GATE_INTROSPECTION_WARN_ENABLED is False
        assert cfg.GATE_PHASE1_SCAFFOLD_BLOCK_ENABLED is False
        assert cfg.GATE_PREBUILD_RESEARCH_BUDGET_ENABLED is False

    def test_research_budget_count_is_configurable(self, monkeypatch):
        monkeypatch.setenv("PUZZLEEVAL_GATE_PREBUILD_RESEARCH_BUDGET_COUNT", "5")
        import puzzleeval.config as cfg
        importlib.reload(cfg)
        assert cfg.GATE_PREBUILD_RESEARCH_BUDGET == 5

