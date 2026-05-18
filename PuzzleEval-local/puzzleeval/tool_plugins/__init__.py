"""Tool plugin architecture for cross-modality test generation + evaluation.

Today's pipeline is excellent at HTTP REST APIs that take JSON / files in,
return JSON out, and can be evaluated by an LLM judge reading the response
text. That covers a lot — but not everything PuzzleEval needs to support:

  - **Voice / phone agents**: input is audio, output is audio; can't be
    serialized as text and the LLM judge can't listen.
  - **Code generation**: output is code; quality requires EXECUTION
    (does it compile? does it pass the test suite?), not LLM judging.
  - **Multi-turn chatbots**: a single request/response harness can't
    evaluate "does the bot keep state across 5 turns + ask the right
    clarifying questions?"
  - **Outbound message senders**: the relevant signal is "did the
    message reach the destination correctly," which needs a destination
    simulator (mock SMTP / Slack / Twilio webhook receiver).
  - **Document parsing test data**: when the user has no sample PDFs
    on disk, we should be able to GENERATE representative test PDFs
    rather than fall back to text proxies.

The plugin architecture below lets each modality plug in:

  1. **Test input synthesis** — produce realistic input data for the
     modality (e.g. TTS plugin synthesizes an audio file, code plugin
     generates a code-completion prompt + expected output).
  2. **Test output evaluation** — score the candidate API's response in
     the modality's native form (e.g. transcription plugin STTs the
     audio response back to text and compares to expected; code plugin
     runs the generated code in a sandbox and checks exit code).
  3. **Capability declaration** — each plugin declares which
     `input_type` and `output_type` enum values it can handle. The
     modality detector matches test cases to plugins by these tags.

A plugin is just a class implementing `ToolPlugin` and registered via
`register_plugin()`. New plugins can be added without touching Agent 5.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

HARNESS_EXECUTION_SINGLE_CALL = "single_call"
HARNESS_EXECUTION_PERSISTENT_WORKER = "persistent_worker"
# Phase 5 compatibility shims. Serialized conversation is an interaction
# pattern inside implementation_plan.json, not a runtime primitive; it runs
# through the single-call subprocess primitive unless the plan says the harness
# process owns state. The old names remain importable during rollout.
HARNESS_EXECUTION_MULTI_TURN_SERIALIZED = HARNESS_EXECUTION_SINGLE_CALL
HARNESS_EXECUTION_MULTI_TURN_PERSISTENT = HARNESS_EXECUTION_PERSISTENT_WORKER


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------


@dataclass
class PluginCapabilities:
    """What modalities a plugin claims to handle.

    Each list entry is a string from the schema enums (input_type /
    output_type) — e.g. ``audio_content``, ``free_text``, ``media_url``,
    ``code``. Plugins MAY also declare ``synthesizes_input`` (can
    produce input data when none provided) and ``evaluates_output``
    (can score a response).

    ``requires_harness_runner`` declares that the plugin DRIVES the
    harness rather than scoring an already-produced response — it needs
    a ``harness_runner: Callable[[dict], dict]`` passed to
    ``evaluate_output`` so it can invoke the candidate's harness.run()
    once per turn / once per sub-call. Agent 5's test-execution loop
    uses this flag to decide whether to build + inject a runner closure.
    The process-lifecycle choice is separate: ``harness_execution_mode``
    decides whether those calls use ``single_call`` or ``persistent_worker``.
    Multi-turn serialized/provider-held context is represented by
    implementation_plan.json's interaction pattern, not by another runtime
    primitive.
    Without ``requires_harness_runner``, Agent 5 sends ``response`` +
    ``expected`` as usual and the plugin scores what it's given.

    This is the general replacement for the prior hardcoded
    ``if evaluator.name == 'conversation_simulator'`` check — any plugin
    that needs multi-call orchestration (conversation_simulator for text
    chat, voice_realtime for multi-turn voice, any future plugin that
    needs the same pattern) sets this flag, and Agent 5 dispatches
    uniformly. Zero case-specific branches.
    """
    input_types: list[str] = field(default_factory=list)
    output_types: list[str] = field(default_factory=list)
    synthesizes_input: bool = False
    evaluates_output: bool = False
    requires_credentials: list[str] = field(default_factory=list)
    requires_harness_runner: bool = False
    # How Agent 5 invokes harness.run() for plugins that drive the
    # harness. Runtime primitives are binary: single_call or persistent_worker.
    harness_execution_mode: str = HARNESS_EXECUTION_SINGLE_CALL
    # `provisions_remote_session_per_call` declares that a single
    # ``harness.run()`` call provisions a billable provider-side resource
    # (e.g., ElevenLabs Conversational AI agent, OpenAI Realtime session)
    # that costs money and counts against rate limits. The adversarial
    # verifier reads this flag to skip stateless probes
    # (`idempotency` + `concurrency`) which would otherwise create N
    # billable sessions in seconds and flood provider rate limits — the
    # exact failure mode that dropped ElevenLabs from test execution
    # in real-run trace 6e0c9563. Multi-turn coverage for these
    # harnesses comes from smoke_test + live_test which the builder
    # writes; the skipped probes assume STATELESS semantics that don't
    # apply.
    provisions_remote_session_per_call: bool = False
    notes: str = ""


@dataclass
class SynthesisResult:
    """Output of `synthesize_input()` — file path or inline payload."""
    file_path: str | None = None
    inline_data: dict[str, Any] | None = None
    ground_truth: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass
class EvaluationResult:
    """Output of `evaluate_output()` — passed/failed + scoring detail."""
    passed: bool
    score: float  # 0.0 - 1.0
    reasoning: str
    detail: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str | None = None  # Set when plugin couldn't run; caller may fall back to LLM judge


@dataclass
class ProviderHealthResult:
    """Optional plugin-level provider/account health preflight result."""

    status: str = "unsupported"  # healthy | external_provider_blocked | unsupported
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


class ToolPlugin(ABC):
    """Base class for every tool plugin.

    Subclasses MUST implement at minimum:
      - ``name`` (class attr): short identifier
      - ``capabilities()``: declared input/output types
      - One or both of ``synthesize_input()`` / ``evaluate_output()``
    """

    name: str = ""

    @abstractmethod
    def capabilities(self) -> PluginCapabilities:
        """Return what this plugin can do."""
        raise NotImplementedError

    def health_check(
        self,
        credentials: dict[str, str] | None = None,
        candidate_context: dict[str, Any] | None = None,
    ) -> ProviderHealthResult:
        """Optional provider/account preflight.

        Plugins override this when they can cheaply distinguish harness bugs
        from external account/provider blockers (quota exhaustion, auth denial,
        no output-bearing stream, provider outage). Unsupported means "use the
        normal live-test path."
        """
        return ProviderHealthResult()

    def is_available(self) -> tuple[bool, str]:
        """Self-check: can this plugin actually run in the current env?

        Returns ``(True, "")`` when ready, or ``(False, reason)`` when not
        (missing system dep, missing credential, missing Python package).

        Default implementation checks declared `requires_credentials`
        against `os.environ`. Override for richer checks.
        """
        import os
        missing = [
            v for v in self.capabilities().requires_credentials
            if not os.environ.get(v)
        ]
        if missing:
            return False, f"missing env vars: {', '.join(missing)}"
        return True, ""

    def synthesize_input(
        self, *, scope_role: str, ground_truth_hint: str | None = None,
        **kwargs: Any,
    ) -> SynthesisResult:
        """Generate test input for this modality. Optional override."""
        raise NotImplementedError(
            f"{self.name}: this plugin does not support input synthesis"
        )

    def evaluate_output(
        self, *, response: Any, expected: Any, criteria: list[dict] | None = None,
        **kwargs: Any,
    ) -> EvaluationResult:
        """Score a candidate API's response. Optional override."""
        raise NotImplementedError(
            f"{self.name}: this plugin does not support output evaluation"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, ToolPlugin] = {}


def register_plugin(plugin: ToolPlugin) -> None:
    """Register a plugin instance under its ``name``.

    Plugin authors should call this at module import time (the
    ``puzzleeval.tool_plugins`` package's ``__init__`` imports the
    bundled plugins so they auto-register).

    Re-registering an EXISTING NAME with the SAME instance is a no-op
    (idempotent — common pattern when a plugin module is imported twice
    via different paths in tests). Re-registering with a DIFFERENT
    instance under the same name is a real bug — two plugin modules
    fighting for the same modality slot would silently override each
    other and the wrong one would handle test cases. We log a warning
    so the conflict is visible in normal operation; pass
    ``PUZZLEEVAL_STRICT_PLUGIN_REGISTRY=1`` to make it raise instead.
    """
    if not getattr(plugin, "name", ""):
        raise ValueError("plugin must set a non-empty `name` class attribute")
    existing = _REGISTRY.get(plugin.name)
    if existing is not None and existing is not plugin:
        msg = (
            f"plugin name conflict: '{plugin.name}' is already registered "
            f"by {type(existing).__module__}.{type(existing).__name__}; "
            f"replacing with {type(plugin).__module__}.{type(plugin).__name__}"
        )
        import os as _os
        if _os.environ.get("PUZZLEEVAL_STRICT_PLUGIN_REGISTRY", "").lower() in (
            "1", "true", "yes",
        ):
            raise RuntimeError(msg)
        logger.warning(msg)
    _REGISTRY[plugin.name] = plugin
    logger.debug("registered plugin: %s", plugin.name)


def get_plugin(name: str) -> ToolPlugin | None:
    """Look up a plugin by name. Returns None when not registered."""
    return _REGISTRY.get(name)


def list_plugins() -> list[ToolPlugin]:
    """All registered plugins (in registration order)."""
    return list(_REGISTRY.values())


def find_plugins_for_input_type(input_type: str) -> list[ToolPlugin]:
    """All plugins claiming to handle the given input_type."""
    out = []
    for plugin in _REGISTRY.values():
        caps = plugin.capabilities()
        if input_type in caps.input_types:
            out.append(plugin)
    return out


def find_plugins_for_output_type(output_type: str) -> list[ToolPlugin]:
    """All plugins claiming to handle the given output_type."""
    out = []
    for plugin in _REGISTRY.values():
        caps = plugin.capabilities()
        if output_type in caps.output_types:
            out.append(plugin)
    return out


# ---------------------------------------------------------------------------
# Auto-import bundled plugins so they register on import
# ---------------------------------------------------------------------------
# Each module instantiates its plugin and calls register_plugin() at import
# time. Importing the package below side-effects the registration. A failure
# to import any single plugin (missing optional dependency) is logged but
# does not crash the import chain — other plugins still register.

def _safe_import(module_name: str) -> None:
    try:
        __import__(module_name)
    except Exception as exc:  # pragma: no cover - defensive bootstrap
        logger.warning("plugin import failed for %s: %s", module_name, exc)


for _name in (
    "puzzleeval.tool_plugins.code_execution",
    "puzzleeval.tool_plugins.vision",
    "puzzleeval.tool_plugins.transcription",
    "puzzleeval.tool_plugins.tts",
    "puzzleeval.tool_plugins.conversation_simulator",
    "puzzleeval.tool_plugins.webhook_receiver",
    "puzzleeval.tool_plugins.outbound_delivery",
    "puzzleeval.tool_plugins.voice_realtime",
):
    _safe_import(_name)


__all__ = [
    "EvaluationResult",
    "HARNESS_EXECUTION_MULTI_TURN_PERSISTENT",
    "HARNESS_EXECUTION_MULTI_TURN_SERIALIZED",
    "HARNESS_EXECUTION_PERSISTENT_WORKER",
    "HARNESS_EXECUTION_SINGLE_CALL",
    "PluginCapabilities",
    "ProviderHealthResult",
    "SynthesisResult",
    "ToolPlugin",
    "find_plugins_for_input_type",
    "find_plugins_for_output_type",
    "get_plugin",
    "list_plugins",
    "register_plugin",
]
