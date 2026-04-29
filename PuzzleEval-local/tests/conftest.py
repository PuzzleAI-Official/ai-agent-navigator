"""Test-suite-wide fixtures and defaults.

Disables features that depend on real network / real subprocess execution
when not explicitly enabled by an individual test:

- Adversarial probe battery (Gap E) — runs subprocesses against built
  harnesses. Most agent tests use mocked harnesses with no real venv;
  the probes would correctly flag them as not-ready and break the test.
  Tests that exercise the battery directly (test_claude_code_patterns)
  set their own env explicitly via fixtures.

- Cross-run memory directory (Gap D) — points to a per-test tmp path
  unless the test wants its own; prevents pollution of the user's real
  ~/.puzzleeval/memdir/.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _disable_adversarial_probes_by_default(monkeypatch):
    """Default to OFF for the adversarial battery in unit tests.

    The dedicated battery tests in test_claude_code_patterns set
    PUZZLEEVAL_ADVERSARIAL_PROBES_ENABLED themselves where they need it
    (or invoke the battery functions directly without going through the
    config flag).
    """
    if "PUZZLEEVAL_ADVERSARIAL_PROBES_ENABLED" not in os.environ:
        monkeypatch.setenv("PUZZLEEVAL_ADVERSARIAL_PROBES_ENABLED", "0")
    yield


@pytest.fixture(autouse=True)
def _isolate_memdir(tmp_path_factory, monkeypatch):
    """Point the memdir at a unique tmp path per test session so memos
    don't leak between tests or pollute the developer's real memdir."""
    if "PUZZLEEVAL_MEMDIR" not in os.environ:
        d = tmp_path_factory.mktemp("memdir")
        monkeypatch.setenv("PUZZLEEVAL_MEMDIR", str(d))
    yield


@pytest.fixture(autouse=True)
def _disable_phase1_scaffold_gate_for_legacy_mocks(monkeypatch):
    """Disable gate B3 (Phase-1 scaffold block) for legacy mock-based
    tests by default.

    Legacy tests (test_agent5.py, test_build_loop_behavior.py, etc.) use
    minimal mocks that write harness.py directly without first writing
    api_spec.txt — they predate gate B3 and test OTHER invariants
    (output shape, cost accumulation, conversation log, verification
    gate, retry behavior, integration paths).

    Gate B3's correctness is verified separately in
    `tests/test_agent5_write_file_gates.py`, which explicitly passes
    `phase_state` and asserts both the violation + near-miss cases.
    Disabling B3 globally via the documented env-var bypass lets
    legacy mock flows continue to exercise the build loop's higher-
    level contracts.

    Tests that need gate B3 active (the dedicated gate-suite tests)
    set the env var explicitly via their own fixtures.
    """
    if "PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK" not in os.environ:
        monkeypatch.setenv("PUZZLEEVAL_GATE_PHASE1_SCAFFOLD_BLOCK", "0")
        # Force a config reload so module-level constants reflect the env.
        import importlib

        import puzzleeval.config as cfg

        importlib.reload(cfg)
    yield
