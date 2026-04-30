"""Tests for session-aware adversarial verifier behavior.

Covers the `provisions_remote_session_per_call` capability flag pathway:
when set on a plugin, harnesses for that modality should NOT be probed
with the stateless `idempotency` + `concurrency` probes. Those probes
assume single-call stateless semantics; for harnesses that provision a
billable provider session per call (ElevenLabs ConvAI, OpenAI Realtime,
Twilio call), they create N billable sessions in seconds and hit
provider rate limits — the exact failure mode that dropped ElevenLabs
from test execution on real-run trace 6e0c9563.

Also covers the `_provisions_remote_session` predicate that the Agent 5
caller uses to set the flag from the test_cases' modality routing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Capability flag wiring
# ---------------------------------------------------------------------------


class TestPluginCapabilities:
    def test_voice_realtime_plugin_provisions_remote_session(self):
        """voice_realtime is the canonical example — multi-turn voice
        harnesses provision a billable WebSocket session per call."""
        from puzzleeval.tool_plugins.voice_realtime import VoiceRealtimePlugin
        caps = VoiceRealtimePlugin().capabilities()
        assert caps.provisions_remote_session_per_call is True

    def test_default_plugin_does_not_provision_remote_session(self):
        """The default for unspecified plugins is False — only opt-in
        plugins (those that genuinely create per-call provider resources)
        should set this flag."""
        from puzzleeval.tool_plugins import PluginCapabilities
        caps = PluginCapabilities()
        assert caps.provisions_remote_session_per_call is False

    def test_conversation_simulator_does_not_provision_remote_session(self):
        """conversation_simulator is the SIMULATOR side (drives the
        client); it doesn't provision a provider session itself."""
        from puzzleeval.tool_plugins.conversation_simulator import ConversationSimulatorPlugin
        caps = ConversationSimulatorPlugin().capabilities()
        assert caps.provisions_remote_session_per_call is False


# ---------------------------------------------------------------------------
# run_adversarial_battery — skip behavior
# ---------------------------------------------------------------------------


def _make_minimal_sandbox(tmp: Path) -> Path:
    """Create a minimal sandbox with a passable harness.py for probes."""
    (tmp / "harness.py").write_text(
        "def run(input_data):\n"
        "    return {'success': True, 'output': '', 'latency_ms': 1,\n"
        "            'tokens_used': None, 'cost_usd': None,\n"
        "            'raw_response': {}, 'error': None}\n",
        encoding="utf-8",
    )
    return tmp


class TestRunAdversarialBatterySkip:
    def test_signature_accepts_kwarg(self):
        """The new keyword arg must exist with default False so all
        existing callers continue to work unchanged."""
        import inspect
        from puzzleeval.adversarial_verifier import run_adversarial_battery
        sig = inspect.signature(run_adversarial_battery)
        params = sig.parameters
        assert "provisions_remote_session_per_call" in params
        param = params["provisions_remote_session_per_call"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is False

    def test_skipped_probes_recorded_in_warnings(self):
        """When provisions_remote_session_per_call=True, idempotency +
        concurrency are skipped AND the skip is logged as a warning so
        the operator can audit the decision."""
        from puzzleeval.adversarial_verifier import run_adversarial_battery
        with tempfile.TemporaryDirectory() as d:
            tmp = _make_minimal_sandbox(Path(d))
            # Use a no-op enabled_probes list to short-circuit the actual
            # subprocess invocations — we only need to verify the skip
            # logic and warning emission.
            with patch(
                "puzzleeval.adversarial_verifier._precheck_sandbox_imports",
                return_value=(True, ""),
            ):
                report = run_adversarial_battery(
                    sandbox_dir=tmp,
                    sample_input={"text": "x"},
                    credentials=None,
                    enabled_probes=["idempotency", "concurrency", "auth_error"],
                    provisions_remote_session_per_call=True,
                )
            # Must surface a warning describing what was skipped + why
            assert any(
                "idempotency" in w and "concurrency" in w
                for w in report.warnings
            ), f"missing skip warning, got: {report.warnings}"
            # Probes that ran: only auth_error (idempotency + concurrency
            # were filtered out before the loop)
            ran_labels = {p.label for p in report.probe_results}
            assert "idempotency" not in ran_labels
            assert "concurrency" not in ran_labels

    def test_default_false_runs_all_probes(self):
        """Default (False) preserves the original probe set — no
        regression for harnesses that don't opt into the skip."""
        from puzzleeval.adversarial_verifier import run_adversarial_battery
        with tempfile.TemporaryDirectory() as d:
            tmp = _make_minimal_sandbox(Path(d))
            with patch(
                "puzzleeval.adversarial_verifier._precheck_sandbox_imports",
                return_value=(True, ""),
            ):
                report = run_adversarial_battery(
                    sandbox_dir=tmp,
                    sample_input={"text": "x"},
                    credentials=None,
                    enabled_probes=["idempotency", "concurrency"],
                )
            ran_labels = {p.label for p in report.probe_results}
            assert "idempotency" in ran_labels
            assert "concurrency" in ran_labels


# ---------------------------------------------------------------------------
# Forensics tail in summary
# ---------------------------------------------------------------------------


class TestSummarizeIncludesForensicsTail:
    def test_forensics_tail_appended_on_timeout(self):
        from puzzleeval.adversarial_verifier import _summarize
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "harness_forensics.jsonl").write_text(
                '{"event":"op_start","op":"x","t_ms":10}\n'
                '{"event":"op_done","op":"x","t_ms":20}\n',
                encoding="utf-8",
            )
            parsed = {"_invocation": "timeout", "duration_ms": 45000}
            result = _summarize(parsed, sandbox_dir=tmp)
            assert "timeout" in result
            assert "last" in result
            assert "forensic events" in result
            assert "op_start" in result

    def test_no_forensics_no_tail_on_timeout(self):
        from puzzleeval.adversarial_verifier import _summarize
        with tempfile.TemporaryDirectory() as d:
            parsed = {"_invocation": "timeout"}
            result = _summarize(parsed, sandbox_dir=Path(d))
            assert "timeout" in result
            # No forensics file → no tail suffix
            assert "forensic events" not in result

    def test_no_sandbox_dir_no_tail(self):
        from puzzleeval.adversarial_verifier import _summarize
        parsed = {"_invocation": "timeout"}
        result = _summarize(parsed, sandbox_dir=None)
        assert "timeout" in result
        assert "forensic events" not in result

    def test_success_result_has_no_tail(self):
        """Tail only appears on crash/timeout/subprocess_error — not on
        clean success."""
        from puzzleeval.adversarial_verifier import _summarize
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "harness_forensics.jsonl").write_text(
                '{"event":"op_start","op":"x"}\n', encoding="utf-8",
            )
            parsed = {"_subprocess_ok": True, "result": {"success": True}}
            result = _summarize(parsed, sandbox_dir=tmp)
            assert "forensic events" not in result


# ---------------------------------------------------------------------------
# Caller-site predicate: detect stateful-session test cases
# ---------------------------------------------------------------------------


class TestCallerSidePredicate:
    def test_voice_test_case_routes_to_stateful_plugin(self):
        """A voice_conversation test case's input_type must route to a
        plugin with provisions_remote_session_per_call=True."""
        from puzzleeval.tool_plugins import find_plugins_for_input_type
        plugins = find_plugins_for_input_type("voice_conversation")
        assert any(
            p.capabilities().provisions_remote_session_per_call
            for p in plugins
        ), "voice_conversation should route to at least one stateful plugin"

    def test_text_modality_does_not_route_to_stateful_plugin(self):
        """A document_content test case must NOT route to any stateful
        plugin — OCR / extraction harnesses are inherently stateless."""
        from puzzleeval.tool_plugins import find_plugins_for_input_type
        plugins = find_plugins_for_input_type("document_content")
        # If any plugins are registered for this type, none should be
        # stateful-session.
        for p in plugins:
            assert p.capabilities().provisions_remote_session_per_call is False, (
                f"{p.name} unexpectedly marked provisions_remote_session_per_call=True"
            )
