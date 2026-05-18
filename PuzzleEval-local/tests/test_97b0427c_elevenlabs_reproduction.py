"""Permanent regression test for the 97b0427c ElevenLabs adversarial-failure pattern.

Real-run trace 97b0427c (2026-04-29): ElevenLabs ConvAI harness built
cleanly, passed smoke + live tests, then collapsed against the 6-probe
adversarial battery — 5 critical failures (empty_input, max_input,
malformed_input, idempotency, concurrency timeouts). Reading the
harness.py revealed the engineering gaps:
  * Singleton WebSocket state shared across concurrent calls.
  * 12-second IO blocking with no early exit on missing greeting.
  * Swallowed exceptions in the reader thread.
  * No resource cleanup on early-return error paths.

The PR 2 reflection-evidence gate is the system-level fix: the agent
MUST cite specific code references for each adversarial probe before
HARNESS_COMPLETE is accepted. A vacuous reflection ("yes, all probes
handled") gets rejected; a substantive one with file:line citations
passes. This test pins both directions on a reflection that mirrors
what an agent might write for the original 97b0427c scenario.

The test is permanent — any regression in the reflection-evidence
check that lets the vacuous variant slip through will fail this
regression.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from puzzleeval.agents.agent5 import verification
from puzzleeval.agents.agent5.reflection_evidence_check import (
    ReflectionVerdict,
    compute_evidence,
    evaluate,
)


_LOGGER = logging.getLogger("test_97b0427c")


# ---------------------------------------------------------------------------
# The two reflection variants
# ---------------------------------------------------------------------------
# VACUOUS: what an agent might write if it built the original 97b0427c
# harness (singleton state, no timeouts, swallowed exceptions) and just
# self-attested at the reflection step. This is what the gate must REJECT.
_VACUOUS_ELEVENLABS_REFLECTION = """# Reflection: pre-HARNESS_COMPLETE

## SUCCESS CRITERIA evidence walk-through

Yes, smoke and live tests pass. All test cases are handled.

## Adversarial probe robustness

The harness is robust against all six probes. Empty inputs are
handled, max input is fine, malformed input is rejected, idempotency
is preserved, concurrency works, repeat calls are stable.

## User-fit assessment

The harness solves the user's voice-agent comparison need.

## Code quality assessment

Clean separation of concerns. Errors are handled. Resources cleaned up.
No shared mutable state. Looks good.

## Self-critique

Nothing to fix.
"""

# SUBSTANTIVE: what a properly-engineered ElevenLabs harness reflection
# looks like — concrete file:line citations for every claim, decisions
# that map to specific code paths. This is what the gate must ACCEPT.
_SUBSTANTIVE_ELEVENLABS_REFLECTION = """# Reflection: pre-HARNESS_COMPLETE

## SUCCESS CRITERIA evidence walk-through

- offline smoke check passes: smoke_test.py:42 runs mocked mechanical probes (happy path,
  empty input, malformed input, max input, concurrency); output shows
  "5/5 passed".
- live_test.py passes: live_test.py:18 invokes harness.run() with a
  production-shape audio_url payload; output: "live test ok".
- All 5 test cases pass: harness.py:120 returns the standardized 7-key
  dict for each tc-XXX input; agent_3_test_cases.json verified.
- Forensics coverage: harness.py:1 imports `_forensics` first;
  harness.py:55 wraps the WebSocket session creation in
  `with traced_op('session_create_start', ...)`; stream_event lifecycle
  logged at harness.py:88 + harness.py:95.

## Adversarial probe robustness

- empty_input: harness.py:32-36 short-circuits when input_data['text']
  or input_data['audio_url'] is missing. Returns success=False with
  error='empty input — returning early before WebSocket open'. No hang.
- max_input: harness.py:42-50 caps input length at 100K bytes and
  truncates with explicit logging via traced_op('input_truncated', ...).
  No crash.
- malformed_input: harness.py:52-58 catches JSONDecodeError + returns
  success=False with structured error dict and logs malformed_payload.
- idempotency: harness.py:60-65 builds a fresh `WebSocketClient(self.url)`
  per run() call; no module-level mutable session_state dict. Sequential
  calls each open + close their own connection (verified in smoke test).
- concurrency: harness.py:70-78 uses local variables only. The reader
  thread at harness.py:130-145 has explicit error propagation via a
  per-call queue (NOT a singleton); concurrent run() calls produce
  independent queues. No shared mutable state to race on.
- repeat_call: harness.py:88 logs op_done events showing consistent
  output across 5 sequential calls in smoke_test.py:75.

## User-fit assessment

- Sub-task A (receive inbound call): harness.py:100-110 opens the
  ElevenLabs ConvAI WebSocket session and forwards the caller's audio
  via `conversation_initiation_client_data` per ConvAI docs.
- Sub-task B (respond in natural voice): harness.py:115-122 collects
  the agent_audio chunks via _drain_response and returns them as
  raw_response['audio_bytes'] for the transcription plugin.

## Code quality assessment

- separation of concerns: harness.py:20-30 (auth header build),
  harness.py:35-50 (WebSocket open + protocol), harness.py:60-110
  (run() business logic).
- no swallowed exceptions: every except block at harness.py:35, 52, 75
  logs traced_op('op_error', ...) AND propagates the error via the
  per-call queue or returns success=False with the error message.
- resource cleanup: harness.py:130 closes the WebSocket in a finally
  block on every return path. harness.py:150 has a separate cleanup
  for the reader thread (join with timeout=5s). All early-return
  paths at harness.py:32-58 invoke this cleanup.
- no shared mutable state: harness.py has no module-level dicts or
  lists. The session_state arg is passed in by the caller (test
  framework owns it); harness.py:60-65 reads at entry, writes at
  return — never accumulates across calls within harness internals.

## Self-critique

- One thing I'd fix with one more turn: harness.py:42 uses a hard-coded
  100K input cap; would expose that as a config knob via env var like
  ELEVENLABS_MAX_INPUT_BYTES. Would also extract the WebSocket open
  retry logic at harness.py:35-50 into a separate function for
  testability — currently it's a 15-line inline block.
"""


# ---------------------------------------------------------------------------
# Pattern check verdicts
# ---------------------------------------------------------------------------


class TestVacuousReflectionRejected:
    """The 97b0427c-style self-attestation reflection must NOT pass.

    Without this guarantee, a harness with the original failure modes
    (singleton state, no timeouts, swallowed exceptions) could ship
    HARNESS_COMPLETE on a "looks good" reflection.
    """

    def test_vacuous_pattern_check_returns_fail(self):
        ev = compute_evidence(_VACUOUS_ELEVENLABS_REFLECTION)
        assert ev.verdict == ReflectionVerdict.FAIL, (
            "Vacuous reflection slipped through pattern check; reason: "
            f"{ev.reason}"
        )

    def test_vacuous_evaluate_returns_fail_short_circuits_judge(self):
        # Pattern check FAIL is decisive — the LLM-judge isn't invoked
        # (saves cost when the pattern signal is unambiguous).
        client = MagicMock()
        verdict, evidence, judge_reason = evaluate(
            reflection_md=_VACUOUS_ELEVENLABS_REFLECTION,
            objective_md="(objective)",
            client=client,
            llm_judge_enabled=True,
        )
        assert verdict == ReflectionVerdict.FAIL
        assert judge_reason == ""
        client.messages.create.assert_not_called()


class TestSubstantiveReflectionAccepted:
    """A proper engineering reflection — file:line citations for every
    claim, decisions tied to code paths — must pass without invoking
    the LLM-judge."""

    def test_substantive_pattern_check_returns_pass(self):
        ev = compute_evidence(_SUBSTANTIVE_ELEVENLABS_REFLECTION)
        assert ev.verdict == ReflectionVerdict.PASS, ev.reason
        # File-ref count well above the floor — concrete evidence.
        assert ev.total_file_refs >= 20
        assert ev.sections_with_evidence == 5

    def test_substantive_cites_each_adversarial_probe(self):
        # The reflection must explicitly mention the engineering decision
        # for EACH of the 6 probes by name — that's the load-bearing
        # discrimination test.
        for probe in [
            "empty_input", "max_input", "malformed_input",
            "idempotency", "concurrency", "repeat_call",
        ]:
            assert probe in _SUBSTANTIVE_ELEVENLABS_REFLECTION, (
                f"Probe missing from substantive reflection: {probe}"
            )


# ---------------------------------------------------------------------------
# End-to-end gate verdict via verify_reflection_complete
# ---------------------------------------------------------------------------


class TestVerifyReflectionEndToEnd:

    def _seed_state(self, tmp_path: Path, reflection_text: str) -> None:
        state_dir = tmp_path / "_agent_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "objective.md").write_text(
            "# Objective\n\n## SUCCESS CRITERIA\n- [ ] Survives 6 probes\n",
            encoding="utf-8",
        )
        (tmp_path / "harness.py").write_text(
            "\n".join(f"# line {i}" for i in range(1, 181)),
            encoding="utf-8",
        )
        (tmp_path / "smoke_test.py").write_text(
            "\n".join(f"# line {i}" for i in range(1, 101)),
            encoding="utf-8",
        )
        (tmp_path / "live_test.py").write_text(
            "\n".join(f"# line {i}" for i in range(1, 41)),
            encoding="utf-8",
        )
        (state_dir / "reflection_phase_3.md").write_text(
            reflection_text, encoding="utf-8",
        )

    def _make_candidate(self) -> MagicMock:
        cand = MagicMock()
        cand.name = "ElevenLabs ConvAI"
        cand.provider = "ElevenLabs"
        return cand

    def test_vacuous_reflection_returns_error_at_gate(self, tmp_path: Path):
        self._seed_state(tmp_path, _VACUOUS_ELEVENLABS_REFLECTION)
        result = verification.verify_reflection_complete(
            tmp_path, self._make_candidate(),
            client=None, llm_judge_enabled=False,
            logger=_LOGGER, trace_id="t-97b0427c-vacuous",
        )
        assert result is not None, "gate accepted vacuous ElevenLabs reflection"
        # The error message tells the agent what's needed.
        assert "evidence" in result.lower() or "self-attestation" in result.lower()

    def test_substantive_reflection_returns_none_at_gate(self, tmp_path: Path):
        self._seed_state(tmp_path, _SUBSTANTIVE_ELEVENLABS_REFLECTION)
        result = verification.verify_reflection_complete(
            tmp_path, self._make_candidate(),
            client=None, llm_judge_enabled=False,
            logger=_LOGGER, trace_id="t-97b0427c-substantive",
        )
        assert result is None, (
            f"gate rejected substantive ElevenLabs reflection: {result}"
        )
