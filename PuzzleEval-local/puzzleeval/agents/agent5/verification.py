"""Verification gate — final structural check before HARNESS_COMPLETE.

When the builder model signals HARNESS_COMPLETE, the build loop calls
this gate to confirm the harness is structurally present (harness.py
exists, is non-empty). The agent already validated compatible input
forms with real test data during Phase 3 — this is just a sanity
check that the file actually exists.

This module is intentionally small. The Phase 4 REAL TEST PROBE work
(per OT-013 in CLAUDE.md) will live here too: an additional check that
the harness was actually exercised against an Agent-3-shaped payload
before HARNESS_COMPLETE is accepted. Today's gate doesn't do that;
the planned probe will.

The forensics-coverage gate (`verify_forensics_coverage`) is separate —
it does AST-based semantic checks on the harness to enforce the
OBSERVABILITY CONTRACT (the harness imports `_forensics`, wraps SDK
calls in `traced_op`, instruments session/stream lifecycle for
streaming harnesses). Both gates run after HARNESS_COMPLETE; both
are soft (one retry, then accept with `gate_fired` telemetry per
AD-007 default).

Phase 3.1 of the architecture cleanup — extracted from
``puzzleeval/agents/implement_test_env.py``. The legacy name
``_run_verification_checks`` is preserved as a one-line shim in
``implement_test_env.py`` for back-compat with source-grep tests +
external callers.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

from puzzleeval.agents.agent5.sandbox import _read_harness_code

if TYPE_CHECKING:
    import logging
    from puzzleeval.schemas import ScreenedCandidate


def run_verification_checks(
    sandbox_dir: Path,
    candidate: "ScreenedCandidate",
    credentials: dict[str, str] | None,
    logger: "logging.Logger",
    trace_id: str,
) -> str | None:
    """Verify the harness exists and is structurally valid.

    Called when Claude signals HARNESS_COMPLETE. Returns a short error
    message string when verification fails; returns None on success.

    Args:
        sandbox_dir: The candidate's sandbox directory.
        candidate: The ScreenedCandidate the harness was built for.
        credentials: Resolved credentials (currently unused by the gate;
                     reserved for the planned Phase 4 REAL TEST PROBE
                     which will need them).
        logger: Standard logger for telemetry.
        trace_id: Run trace_id for log correlation.

    Returns:
        ``None`` on success. A short error string when verification
        fails (caller injects this into the build loop's user message
        and asks the model to fix).

    Future expansion (OT-013, planned Phase 4 REAL TEST PROBE):
        Add a check that loads `agent_3_test_cases.json` from the
        sandbox, picks one test case, builds the production-shape
        payload via `derive_production_payload`, calls
        ``harness.run(payload)``, asserts ``success=True`` with expected
        audio shape, and writes ``phase_4_passed.txt`` on success. The
        verification gate then requires that file's existence — the
        bypass-verification problem documented in OT-013 becomes
        structurally impossible.
    """
    if not _read_harness_code(sandbox_dir):
        return "harness.py does not exist or is empty."
    return None


# ---------------------------------------------------------------------------
# Forensics-coverage gate (OBSERVABILITY CONTRACT enforcement)
# ---------------------------------------------------------------------------

# Network libraries the auto-instrumentation in `_forensics.py` covers.
# Calls to these from harness code don't require explicit `traced_op` wrapping
# — the auto-hooks emit request_done/request_error events automatically.
_AUTO_HOOKED_LIBS: frozenset[str] = frozenset({
    "requests", "httpx", "websocket", "websockets", "aiohttp",
})

# Network libraries the auto-instrumentation does NOT cover. Calls to these
# from harness code MUST be wrapped in `traced_op(...)` so the verifier can
# locate failure boundaries when the harness hangs or crashes.
_SDK_LIBS_REQUIRING_MANUAL_WRAP: frozenset[str] = frozenset({
    "openai", "anthropic", "google.cloud", "google.generativeai",
    "grpc", "grpcio", "elevenlabs", "deepgram", "cohere",
    "boto3", "azure",
})

_ALL_NETWORK_LIBS = _AUTO_HOOKED_LIBS | _SDK_LIBS_REQUIRING_MANUAL_WRAP


def _imports_forensics(tree: ast.AST) -> bool:
    """True if the harness imports anything from `_forensics`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "_forensics":
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "_forensics":
                return True
    return False


def _import_line_number(tree: ast.Module, module_root: str) -> int | None:
    """Return the line number of the first import of `module_root`, or None.

    `module_root` is the top-level package name (e.g., "requests" matches
    "import requests" AND "from requests.auth import HTTPBasicAuth").
    Only inspects top-level imports (module-level statements).
    """
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root == module_root:
                    return node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".", 1)[0]
                if root == module_root:
                    return node.lineno
    return None


def _detected_network_imports(tree: ast.Module) -> set[str]:
    """Return the set of network library top-level package names imported."""
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _ALL_NETWORK_LIBS:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".", 1)[0]
                if root in _ALL_NETWORK_LIBS:
                    found.add(root)
    return found


def _count_traced_op_blocks(tree: ast.AST) -> int:
    """Count `with traced_op(...)` (or `with X.traced_op(...)`) AST nodes."""
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    func = ctx.func
                    name = None
                    if isinstance(func, ast.Name):
                        name = func.id
                    elif isinstance(func, ast.Attribute):
                        name = func.attr
                    if name == "traced_op":
                        count += 1
    return count


# Heuristic: regex-scan source for `log("event_name"` / `traced_op("op_name"`
# string literals. AST-walk would also work but regex is sufficient for a
# soft gate and tolerates minor formatting variation.
_LOG_EVENT_REGEX = re.compile(
    r"""\b(?:log|traced_op)\s*\(\s*['"]([a-zA-Z_][a-zA-Z0-9_]*)['"]"""
)


def _scan_canonical_events(src: str) -> set[str]:
    """Set of event names referenced in `log("X"` or `traced_op("X"` calls."""
    return set(_LOG_EVENT_REGEX.findall(src))


def _is_streaming_harness(src: str, tree: ast.Module) -> bool:
    """Heuristic: harness uses websocket/websockets AND has a Thread/asyncio
    background reader.

    Streaming/voice harnesses have an inherently more complex lifecycle
    (session establishment + multi-turn message flow + reader threads or
    async tasks). The verifier requires explicit instrumentation for
    these — auto-hooks alone aren't enough.
    """
    imports = _detected_network_imports(tree)
    has_ws = "websocket" in imports or "websockets" in imports
    if not has_ws:
        return False
    has_thread = bool(re.search(r"\bthreading\.Thread\b|\bThread\s*\(", src)) or \
                 bool(re.search(r"\basyncio\b", src))
    return has_thread


def verify_forensics_coverage(sandbox_dir: Path) -> str | None:
    """Enforce the OBSERVABILITY CONTRACT via semantic AST checks on harness.py.

    Returns ``None`` when the harness satisfies coverage requirements; otherwise
    returns a short repair-request string that the build loop injects into the
    next user message so the builder model can fix the gap.

    Checks (in order; first failure short-circuits):
      1. ``harness.py`` exists and is non-empty
      2. Imports `_forensics` (Import or ImportFrom)
      3. `_forensics` import line precedes every detected network-library
         import line (auto-hooks need to monkey-patch BEFORE clients grab
         function references)
      4. If the harness uses any SDK library not covered by auto-hooks
         (openai, anthropic, google.cloud, grpc, etc.), there must be at
         least one `with traced_op(...)` block
      5. Streaming/voice harnesses (websocket + Thread/asyncio) must have
         session-lifecycle and stream-lifecycle instrumentation visible in
         the source

    Soft gate per AD-007: caller emits this string as a `tool_result`,
    allows ONE retry, then accepts the harness anyway with `gate_fired`
    telemetry. Bypass via env var ``PUZZLEEVAL_GATE_FORENSICS_COVERAGE=0``.
    """
    src = _read_harness_code(sandbox_dir) or ""
    if not src:
        return "harness.py is empty or missing."

    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return (f"harness.py has a syntax error at line {exc.lineno}: "
                f"{exc.msg!r}. Fix the syntax before signaling HARNESS_COMPLETE.")

    # Check 2: imports _forensics
    if not _imports_forensics(tree):
        return (
            "harness.py does not import the auto-injected `_forensics` "
            "module. Add `from _forensics import log, traced_op` as the "
            "FIRST line of harness.py (before any HTTP/WS/SDK client "
            "imports). The forensics shim is auto-staged by sandbox setup; "
            "see the OBSERVABILITY CONTRACT in the system prompt."
        )

    # Check 3: _forensics imported before network libs (so auto-hooks patch first)
    forensics_lineno = _import_line_number(tree, "_forensics")
    if forensics_lineno is None:
        # Defensive: ImportFrom returned True from check 2 but we couldn't
        # locate it at top level (nested import?). Treat as missing.
        return (
            "`_forensics` is imported but not at module top level. Move the "
            "import to the FIRST line of harness.py so the auto-hooks patch "
            "BEFORE clients grab function references."
        )
    for lib in _detected_network_imports(tree):
        net_lineno = _import_line_number(tree, lib)
        if net_lineno is not None and net_lineno < forensics_lineno:
            return (
                f"`_forensics` imported at line {forensics_lineno}, but "
                f"`{lib}` imported earlier at line {net_lineno}. The "
                f"auto-hooks need to monkey-patch `{lib}` BEFORE the harness "
                f"grabs references to its functions. Move "
                f"`from _forensics import log, traced_op` to the FIRST line."
            )

    # Check 4: SDK libs require manual traced_op wrapping
    used = _detected_network_imports(tree)
    sdk_used = used & _SDK_LIBS_REQUIRING_MANUAL_WRAP
    if sdk_used:
        traced_op_count = _count_traced_op_blocks(tree)
        if traced_op_count == 0:
            return (
                f"harness.py uses SDK libraries {sorted(sdk_used)} which the "
                f"auto-hooks do NOT cover (the hooks only intercept "
                f"requests/httpx/websocket-client/websockets/aiohttp/threading). "
                f"Wrap each SDK call site with `with traced_op(\"<op_name>\", "
                f"provider=\"<name>\"):` so the verifier can localize failures. "
                f"The verifier found ZERO `with traced_op(...)` blocks."
            )

    # Check 5: streaming/voice harnesses need session + stream instrumentation
    if _is_streaming_harness(src, tree):
        events = _scan_canonical_events(src)
        # Accept any of these as evidence of session lifecycle:
        session_evidence = (
            "session_create" in events or
            any(e.startswith("session_") for e in events) or
            any("session" in e for e in events)
        )
        # Accept any of these as evidence of stream lifecycle:
        stream_evidence = (
            "stream" in events or
            "stream_start" in events or
            "stream_event" in events or
            any(e.startswith("stream_") for e in events) or
            "drain_response" in events or
            "drain" in events
        )
        missing = []
        if not session_evidence:
            missing.append(
                "session lifecycle (e.g., `traced_op(\"session_create\", "
                "provider=...)` around the WebSocket handshake or session "
                "provisioning call)"
            )
        if not stream_evidence:
            missing.append(
                "stream lifecycle (e.g., `traced_op(\"stream\", provider=...)` "
                "around the receive loop, plus periodic `log(\"stream_event\", "
                "event_type=...)` per provider event)"
            )
        if missing:
            return (
                "streaming/voice harness is missing required instrumentation: "
                + "; ".join(missing)
                + ". See the OBSERVABILITY CONTRACT for the canonical event "
                  "taxonomy. Without these events, the verifier can't show the "
                  "operator WHERE the harness got stuck when it hangs."
            )

    return None


__all__ = [
    "run_verification_checks",
    "verify_forensics_coverage",
]
