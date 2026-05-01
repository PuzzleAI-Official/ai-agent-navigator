"""Permanent regression test for the 749b09b1 narrative-inertia failure.

Real-run trace 749b09b1 (2026-04-28): Opus inherited Sonnet's exit
narration ("Now I have all the information needed. Let me patch
api_spec.txt...") + the system prompt's "Phase 2 = Opus"
anthropomorphization. Opus emitted 33 turns of variants on "Phase 1
complete — handing off to Phase 2 (Opus)" without realizing IT IS
Opus. cache_read=88,763 stayed identical across T5-T37; ~$1.65 wasted.

The fix (PR 1 of the autonomy plan): at the api_spec_written
transition, REPLACE ``messages`` with a single canonical state packet
that points at on-disk artifacts. This breaks the inheritance — Opus
starts fresh with "you are now Phase 2 builder" + concrete next steps.

This test reproduces the scenario: Sonnet emits exit narration in its
final Phase 1 turn, then we drive the transition. After the transition,
``messages`` must NOT contain the inherited narration text. The
context-compaction code path is the only mechanism that achieves this.

The test is permanent — any change to the transition handling that
re-introduces narrative inheritance will fail this regression.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Reuse fixtures from the existing build-loop behavior tests.
from tests.test_build_loop_behavior import (  # noqa: E402
    _make_input_with_one_candidate,
    _make_mock_response,
    _make_text_block,
    _make_tool_use_block,
)


# Sonnet's exit narration verbatim (paraphrased from the real-run trace).
# Any change to messages handling that lets this string survive into
# Opus's context will fail the regression.
_SONNET_EXIT_NARRATION = (
    "Now I have all the information needed. Let me patch api_spec.txt "
    "and hand off to Opus for Phase 2."
)

# The canonical-state-packet markers the compaction injects. Must be
# present in messages after the transition fires.
_PACKET_MARKER_HEADER = "You are now Phase 2 builder"
_PACKET_MARKER_NO_HANDOFF = "do not narrate"


@pytest.fixture(autouse=True)
def _enable_autonomy_and_compaction(monkeypatch):
    """The 749b09b1 fix lives in the autonomy + compaction code paths.
    Both flags must be ON for the regression to actually verify the fix.
    The legacy build-loop fixture turns them off; this fixture overrides.
    """
    monkeypatch.setenv("PUZZLEEVAL_GATE_AUTONOMY_ARTIFACTS", "1")
    monkeypatch.setenv("PUZZLEEVAL_CONTEXT_COMPACTION_AT_MODEL_TRANSITION", "1")
    # Disable the suppression so the compaction definitely fires (no
    # agent observation present in the mock — would be no_observation
    # → fire — but we lock it explicitly).
    monkeypatch.setenv("PUZZLEEVAL_DIRECTIVE_SUPPRESS_ON_AGREEMENT", "0")
    monkeypatch.setenv("PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK", "0")
    monkeypatch.setenv("PUZZLEEVAL_VENV_PREINSTALL", "0")
    import puzzleeval.config as cfg
    importlib.reload(cfg)
    from puzzleeval.agents import implement_test_env as ite
    from puzzleeval.agents.agent5 import sandbox as agent5_sandbox

    def _fast_create_venv(*args, **kwargs):
        return True

    monkeypatch.setattr(ite, "_create_venv", _fast_create_venv)
    monkeypatch.setattr(agent5_sandbox, "create_venv", _fast_create_venv)
    yield


def _capture_messages_side_effect(responses):
    """Return (side_effect, snapshots) where snapshots[N] is a deep copy
    of the messages list passed into the Nth API call."""
    snapshots: list[list[dict]] = []

    def side_effect(**kwargs):
        # Copy at call-time so subsequent mutations don't change the snapshot.
        msgs = kwargs.get("messages", [])
        snapshots.append([dict(m) for m in msgs])
        if not responses:
            raise StopIteration("no more mock responses")
        return responses.pop(0)

    return side_effect, snapshots


@patch("puzzleeval.agents.implement_test_env.anthropic.Anthropic")
def test_compaction_clears_sonnet_exit_narration_at_transition(mock_anthropic_cls):
    """The canonical 749b09b1 reproduction.

    Setup: Sonnet's Phase 1 response includes the exit narration
    verbatim alongside a patch_file('api_spec.txt') tool_use. The
    orchestrator processes the patch, flips api_spec_written=True, and
    runs the transition handler.

    Expectation: Opus's first API call (after the transition) sees
    messages WITHOUT the Sonnet exit narration. The canonical packet
    must be the only user message.
    """
    from puzzleeval.agents.implement_test_env import run_implement_test_env_agent

    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client

    # Sonnet's Phase 1 turn — exit narration + patch_file
    sonnet_turn = _make_mock_response(
        [
            _make_text_block(_SONNET_EXIT_NARRATION),
            _make_tool_use_block("patch_file", {
                "filename": "api_spec.txt",
                "old_string": "x", "new_string": "y",
            }, "tu_sonnet"),
        ],
        stop_reason="tool_use",
    )

    # Opus's Phase 2 turn — writes harness.py
    opus_turn = _make_mock_response(
        [_make_tool_use_block("write_file", {
            "filename": "harness.py",
            "content": (
                'def run(input_data):\n'
                '    return {"output":"x","latency_ms":1,"tokens_used":None,'
                '"cost_usd":None,"raw_response":{},"success":True,"error":None}\n'
            ),
        }, "tu_opus")],
        stop_reason="tool_use",
    )

    # Final completion signal.
    completion_turn = _make_mock_response(
        [_make_text_block("SMOKE TEST PASSED\nHARNESS_COMPLETE")],
        stop_reason="end_turn",
    )

    side_effect, snapshots = _capture_messages_side_effect([
        sonnet_turn, opus_turn, completion_turn,
    ])
    mock_client.beta.messages.create.side_effect = side_effect

    # Pre-render api_spec.txt so the patch_file call has a target to
    # operate on (mirrors the real-run trace 749b09b1 setup where Agent
    # 4's pre-render landed first).
    input_data = _make_input_with_one_candidate("NarrativeInertia")
    from puzzleeval.web_doc_cache import candidate_sandbox_dir
    sandbox_dir = candidate_sandbox_dir(input_data.trace_id, "NarrativeInertia")
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "api_spec.txt").write_text("x", encoding="utf-8")

    run_implement_test_env_agent(input_data)

    # ── Assertions ──────────────────────────────────────────────────
    # snapshot[0]: Sonnet's first API call (initial message only)
    # snapshot[1]: Opus's first API call (after the transition fired)
    # snapshot[2]: Completion turn
    assert len(snapshots) >= 2, (
        f"Expected at least 2 API calls; got {len(snapshots)}"
    )

    opus_messages = snapshots[1]

    # Flatten message contents for inspection.
    serialized = ""
    for msg in opus_messages:
        content = msg.get("content")
        if isinstance(content, str):
            serialized += content + "\n"
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    serialized += str(block.get("text", "")) + "\n"
                    serialized += str(block.get("content", "")) + "\n"

    # The exit narration must NOT survive into Opus's context.
    assert _SONNET_EXIT_NARRATION not in serialized, (
        "Sonnet's exit narration leaked into Opus's first API call — "
        "the compaction is broken and 749b09b1's narrative-inertia "
        "failure mode can recur. Got messages: "
        f"{serialized[:1000]}"
    )

    # The canonical packet markers must be present.
    assert _PACKET_MARKER_HEADER in serialized, (
        "The canonical state packet's 'You are now Phase 2 builder' "
        "marker is missing from Opus's first API call. The compaction "
        "didn't write the packet."
    )
    assert _PACKET_MARKER_NO_HANDOFF.lower() in serialized.lower(), (
        "The canonical state packet's 'do not narrate' instruction is "
        "missing — Opus might re-emit handoff narration."
    )

    # Compaction means the message list at Opus's first call is short.
    # Sonnet's full conversation should NOT be inherited.
    assert len(opus_messages) <= 2, (
        f"After compaction, messages list should be 1-2 entries (the "
        f"canonical packet + maybe an assistant turn). Got "
        f"{len(opus_messages)} entries."
    )


# Note: PR 3 suppression behavior (when the agent has written a
# matching phase observation to agent_observations.json BEFORE the
# transition, neither compaction nor PHASE2_DIRECTIVE fires) is verified
# at the unit level in tests/test_directive_suppression.py and at the
# integration level by mock_pipeline_direct.py. An integration test
# here would have to deeply hook into stage_agent_state's idempotent
# overwrite of _agent_state/objective.md, which is out of scope for
# the regression-pinning purpose of this file.
