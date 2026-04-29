"""Runtime gates — Python-only enforcement of safety-critical contracts.

THIS MODULE IS THE SOURCE OF TRUTH for which gates run. Per AD-007,
runtime gates live in deterministic Python code, NOT in markdown
metadata. The contract markdown's ``paired_gates`` field is descriptive
documentation only — it doesn't control execution.

Why: prompt-level rules are advisory; Claude can violate them silently.
A runtime gate enforces the contract regardless of prompt adherence,
catching violations at the system boundary.

Public API:
  ``ALWAYS_ON_GATES``: tuple of registered gate instances. Used by the
                       test executor / Agent 5 to validate harness
                       responses against the contracts they're paired with.
  ``ContractGate``: base class. Subclass and add to ALWAYS_ON_GATES to
                    introduce a new gate.
  ``run_gates(state, task)``: convenience function that runs every
                              applicable gate and returns the combined
                              GateReport.

Pattern stolen from Claude Code's permission system: each gate decides
both ``applies_to`` (do I care about this task?) and ``validate``
(is the state valid?). The runtime registry is hardcoded so deletion
of one gate is a code change, not a markdown change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from puzzleeval.contracts.task_context import TaskContext


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict on a state.

    Fields:
        passed: True if the gate passed, False if it failed.
        gate_name: Name of the gate (used in telemetry / error messages).
        reason: When passed=False, human-readable explanation of why.
        evidence: Optional dict of supporting data (raw_response keys,
                  observed values, expected shape, etc.).
    """

    passed: bool
    gate_name: str
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def pass_(cls, gate_name: str, **evidence: Any) -> "GateResult":
        return cls(passed=True, gate_name=gate_name, evidence=dict(evidence))

    @classmethod
    def fail(cls, gate_name: str, reason: str, **evidence: Any) -> "GateResult":
        return cls(
            passed=False,
            gate_name=gate_name,
            reason=reason,
            evidence=dict(evidence),
        )


@dataclass
class GateReport:
    """Aggregate of all gate results for one task.

    Fields:
        results: list of GateResult, one per gate that ran.
        all_passed: True iff every gate that ran passed.
        failures: list of failed GateResult entries.
    """

    results: list[GateResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]


class ContractGate(ABC):
    """Base class for runtime contract gates.

    Subclass this and add to ALWAYS_ON_GATES to introduce a new gate.
    The runtime registry is hardcoded — that's intentional per AD-007.

    Subclasses MUST override ``name``, ``applies_to``, and ``validate``.
    The ``contract_reference`` docstring should point at the markdown
    file that teaches the same pattern (for human cross-referencing).
    """

    name: str = ""
    contract_reference: str = ""  # e.g., "capability_playbooks/voice.md"

    @abstractmethod
    def applies_to(self, task: TaskContext) -> bool:
        """Return True when this gate should run for this task."""
        raise NotImplementedError

    @abstractmethod
    def validate(self, state: Any, *, task: TaskContext) -> GateResult:
        """Validate the state. Return GateResult.

        ``state`` is the thing being validated — typically a harness
        response dict or a SelectionResult. Subclasses define what shape
        they expect.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# VoiceHarnessGate — enforces the voice harness return-shape contract
# (capability_playbooks/voice.md).
# ---------------------------------------------------------------------------
class VoiceHarnessGate(ContractGate):
    """Validates voice harness responses against the return-shape contract.

    See ``puzzleeval/capability_playbooks/voice.md`` for the prompt-level
    teaching of this contract. This gate enforces it at runtime regardless
    of whether Claude followed the prompt.

    Two valid shapes:
      Shape A: ``raw_response.audio_bytes`` (inline audio).
      Shape B: ``raw_response.audio_path`` (on-disk path).

    Hard rules from voice.md (encoded here as gate predicates):
      1. raw_response must contain EITHER audio_bytes OR audio_path. Never
         both. Never neither (unless explicitly text-only path).
      2. Forbidden keys (audio_url, audio_b64, audio_data, audio,
         audio_file) cause silent fall-through in the plugin.
    """

    name = "voice_harness"
    contract_reference = "capability_playbooks/voice.md"

    _FORBIDDEN_KEYS: frozenset[str] = frozenset({
        "audio_url",
        "audio_b64",
        "audio_data",
        "audio",
        "audio_file",
    })

    _VOICE_TYPES: frozenset[str] = frozenset({
        "voice_conversation",
        "voice_turn",
        "audio_content",
    })

    def applies_to(self, task: TaskContext) -> bool:
        """Voice tasks only. Skip non-voice modalities cleanly."""
        all_types = task.input_types | task.output_types
        return bool(self._VOICE_TYPES & all_types)

    def validate(self, state: Any, *, task: TaskContext) -> GateResult:
        """Validate ``state`` (a harness response dict).

        Pass conditions:
          * raw_response has either audio_bytes OR audio_path (not both).
          * No forbidden keys.

        Fail conditions:
          * Both audio_bytes AND audio_path present.
          * Neither present AND a forbidden key is present (silent failure
            mode the plugin can't recover from).

        Pass-through (not a hard fail):
          * Neither audio_bytes nor audio_path AND no forbidden keys —
            the response is text-only, which is valid per voice.md rule 6.
        """
        if not isinstance(state, dict):
            return GateResult.fail(
                self.name,
                "state is not a dict; expected harness response shape",
                state_type=type(state).__name__,
            )

        raw_response = state.get("raw_response")
        if not isinstance(raw_response, dict):
            # Text-only response with no raw_response is valid per
            # voice.md rule 6 (text fallback path).
            return GateResult.pass_(self.name, fallback="no raw_response (text-only)")

        has_bytes = "audio_bytes" in raw_response and raw_response.get("audio_bytes") is not None
        has_path = "audio_path" in raw_response and raw_response.get("audio_path") is not None

        if has_bytes and has_path:
            return GateResult.fail(
                self.name,
                "raw_response has BOTH audio_bytes AND audio_path. Pick ONE shape.",
                has_audio_bytes=True,
                has_audio_path=True,
            )

        # Check for forbidden keys (silent failure mode in the plugin).
        forbidden_present = self._FORBIDDEN_KEYS & set(raw_response.keys())
        if forbidden_present and not (has_bytes or has_path):
            return GateResult.fail(
                self.name,
                (
                    f"raw_response uses forbidden audio key(s) {sorted(forbidden_present)} "
                    "but no audio_bytes or audio_path. The voice plugin "
                    "will not parse these — see voice.md."
                ),
                forbidden_keys=sorted(forbidden_present),
            )

        return GateResult.pass_(
            self.name,
            shape="A" if has_bytes else ("B" if has_path else "text"),
        )


# ---------------------------------------------------------------------------
# Registry — every gate that should run anywhere lives here.
# ---------------------------------------------------------------------------
# Add a new gate by:
#   1. Subclassing ContractGate above.
#   2. Adding an instance to ALWAYS_ON_GATES.
#
# Deletion of a gate is a code change. Editing the markdown's paired_gates
# field is NOT a way to disable a gate (that's the AD-007 invariant).
# ---------------------------------------------------------------------------
ALWAYS_ON_GATES: tuple[ContractGate, ...] = (
    VoiceHarnessGate(),
)


def run_gates(state: Any, task: TaskContext) -> GateReport:
    """Run every applicable gate against ``state`` and aggregate results."""
    report = GateReport()
    for gate in ALWAYS_ON_GATES:
        if gate.applies_to(task):
            result = gate.validate(state, task=task)
            report.results.append(result)
    return report


def gate_by_name(name: str) -> ContractGate | None:
    """Return the gate with this name, or None.

    Used by tooling (`puzzleeval contract validate <file>`) to confirm
    that a contract's `paired_gates` references resolve to real gates.
    """
    for gate in ALWAYS_ON_GATES:
        if gate.name == name:
            return gate
    return None


def gate_names() -> tuple[str, ...]:
    """Return tuple of all registered gate names."""
    return tuple(gate.name for gate in ALWAYS_ON_GATES)


__all__ = [
    "ALWAYS_ON_GATES",
    "ContractGate",
    "GateReport",
    "GateResult",
    "VoiceHarnessGate",
    "gate_by_name",
    "gate_names",
    "run_gates",
]
