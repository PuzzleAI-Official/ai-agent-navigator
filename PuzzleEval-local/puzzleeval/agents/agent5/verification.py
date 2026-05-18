"""Verification gate — final structural check before HARNESS_COMPLETE.

When the builder model signals HARNESS_COMPLETE, the build loop calls
this gate to confirm the harness is structurally present (harness.py
exists, is non-empty). The agent already validated compatible input
forms with real test data during Phase 3 — this is just a sanity
check that the file actually exists.

This module is intentionally focused on structural and evidence gates.
The build loop owns smoke/live execution state and only calls this module
when the agent signals HARNESS_COMPLETE; this module verifies the harness
surface, forensics coverage, voice live-test contract, and reflection
evidence that support that completion signal.

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
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from puzzleeval.agents.agent5.sandbox import _read_harness_code
from puzzleeval.config import (
    GATE_SESSION_CONTINUITY_ENABLED,
    GATE_STREAM_KEEPALIVE_DIAGNOSTIC_ENABLED,
)
from puzzleeval.agents.agent5.business_fixture import (
    load_business_fixture,
    validate_structured_claims_against_business_fixture,
)

if TYPE_CHECKING:
    import logging
    import anthropic
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
        credentials: Resolved credentials, accepted for call-site
                     compatibility with the broader completion flow.
        logger: Standard logger for telemetry.
        trace_id: Run trace_id for log correlation.

    Returns:
        ``None`` on success. A short error string when verification
        fails (caller injects this into the build loop's user message
        and asks the model to fix).

    Completion execution state is enforced by the build loop before it
    accepts HARNESS_COMPLETE. This function only checks that the final
    harness file exists and is readable enough for downstream gates.
    Previously this docstring referenced a planned real-test probe; that
    state now lives in the build loop's completion flow.
    """
    if not _read_harness_code(sandbox_dir):
        return "harness.py does not exist or is empty."
    return None


def _distinctive_objective_terms(objective_md: str) -> list[str]:
    """Extract a small set of task-specific terms for live-test sanity checks."""

    tokens = re.findall(r"[A-Za-z][A-Za-z0-9&'-]{3,}", objective_md)
    stop = {
        "objective", "candidate", "deliverable", "success", "criteria",
        "harness", "python", "input", "output", "smoke", "live", "test",
        "tests", "voice", "conversation", "provider", "agent", "system",
        "defined", "must", "should", "with", "from", "that", "this",
        "turn", "turns", "persistent", "runner", "forensics", "coverage",
    }
    seen: set[str] = set()
    terms: list[str] = []
    for token in tokens:
        lower = token.lower().strip("-'")
        if lower in stop or lower in seen or len(lower) < 4:
            continue
        seen.add(lower)
        terms.append(lower)
        if len(terms) >= 12:
            break
    return terms


def _literal_dict_keys_from_python(source: str) -> set[str]:
    """Extract literal dict keys from Python source using AST only."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


def _json_objects_from_text(text: str) -> list[dict[str, Any]]:
    """Best-effort extraction of JSON objects from live-test output."""

    objects: list[dict[str, Any]] = []
    start: int | None = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text or ""):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    data = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    start = None
                    continue
                if isinstance(data, dict):
                    objects.append(data)
                start = None
    return objects


def collect_voice_live_evidence(sandbox_dir: Path) -> dict[str, Any]:
    """Collect structured voice-live evidence without semantic source matching."""

    state_dir = sandbox_dir / "_agent_state"
    evidence_path = state_dir / "live_test_evidence.json"
    evidence: dict[str, Any] = {
        "manifest": None,
        "runtime_turns": [],
        "claims": [],
        "last_live_test_output_present": False,
    }
    if evidence_path.exists():
        try:
            manifest = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = None
        if isinstance(manifest, dict):
            evidence["manifest"] = manifest
            turns = manifest.get("turns")
            if isinstance(turns, list):
                evidence["runtime_turns"].extend(
                    item for item in turns if isinstance(item, dict)
                )
            claims = manifest.get("claims") or manifest.get("fixture_claims")
            if isinstance(claims, list):
                evidence["claims"].extend(claims)

    runtime_path = state_dir / "runtime_state.json"
    if runtime_path.exists():
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            runtime = {}
        output = runtime.get("last_live_test_output") if isinstance(runtime, dict) else ""
        if isinstance(output, str) and output.strip():
            evidence["last_live_test_output_present"] = True
            for item in _json_objects_from_text(output):
                if "turn_index" in item or "success" in item:
                    evidence["runtime_turns"].append(item)
                if isinstance(item.get("transcript"), str):
                    evidence["claims"].append({"transcript": item["transcript"]})
    return evidence


def _voice_review_response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


def _voice_review_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    candidates = [text.strip()] if (text or "").strip() else []
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _voice_semantic_review(
    *,
    client: Any,
    model: str,
    live_source: str,
    evidence: dict[str, Any],
) -> dict[str, Any] | None:
    prompt = (
        "Review whether this voice live test proves task-equivalent current-turn "
        "voice behavior. Use the structured runtime evidence first. Source excerpt "
        "is supporting evidence only; do not require implementation recipe phrases.\n\n"
        "Return strict JSON: decision=pass|block|advisory, confidence=low|medium|high, "
        "issues=array, evidence=array, rationale=one sentence.\n\n"
        "Structured evidence:\n"
        + json.dumps(evidence, indent=2, ensure_ascii=False, default=str)[:6000]
        + "\n\nlive_test.py excerpt:\n"
        + live_source[:6000]
    )
    response = client.messages.create(
        model=model,
        max_tokens=700,
        system=(
            "You are a general validator for production-equivalent voice live "
            "test evidence. Block only high-confidence semantic failures."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    data = _voice_review_json(_voice_review_response_text(response))
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "advisory").strip().lower()
    if decision not in {"pass", "block", "advisory"}:
        decision = "advisory"
    confidence = str(data.get("confidence") or "low").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    return {
        "decision": decision,
        "confidence": confidence,
        "issues": [str(x) for x in (data.get("issues") or []) if str(x).strip()][:5],
        "evidence": [str(x) for x in (data.get("evidence") or []) if str(x).strip()][:5],
        "rationale": str(data.get("rationale") or "")[:500],
    }


def verify_voice_live_test_contract(
    sandbox_dir: Path,
    *,
    client: Any | None = None,
    judge_model: str | None = None,
    llm_review_enabled: bool = False,
) -> str | None:
    """Verify voice live_test.py is production-equivalent enough to trust.

    This is a deterministic contract check, not a semantic judge. It
    catches the real failure mode from voice runs: generic in-process live
    tests that pass while production uses a persistent worker and
    task-specific input_context.
    """

    live_path = sandbox_dir / "live_test.py"
    if not live_path.exists():
        return "live_test.py is missing."
    try:
        live = live_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"live_test.py is unreadable: {exc}"
    missing: list[str] = []
    literal_keys = _literal_dict_keys_from_python(live)
    evidence = collect_voice_live_evidence(sandbox_dir)
    runtime_turns = [
        turn for turn in evidence.get("runtime_turns", [])
        if isinstance(turn, dict)
    ]
    successful_turns = [turn for turn in runtime_turns if turn.get("success") is True]
    audio_evidence = [
        turn for turn in runtime_turns
        if (turn.get("audio_path") or (isinstance(turn.get("raw_response"), dict) and turn["raw_response"].get("audio_path")))
    ]

    if "turn_index" not in literal_keys and not runtime_turns:
        missing.append("turn_index")
    if not {"input_context", "instructions"} <= literal_keys and not evidence.get("manifest"):
        missing.append("input_context.instructions")
    if "conversation_history" not in literal_keys and "history" not in literal_keys and not evidence.get("manifest"):
        missing.append("conversation_history/history")
    if len(runtime_turns) < 2 and not evidence.get("manifest"):
        missing.append("structured evidence for at least two live turns")
    if runtime_turns and len(successful_turns) < min(2, len(runtime_turns)):
        missing.append("success=True for each recorded live turn")
    if runtime_turns and not audio_evidence:
        missing.append("agent audio artifact evidence in live-test output")

    fixture_issues = validate_structured_claims_against_business_fixture(
        evidence.get("claims") or [],
        load_business_fixture(sandbox_dir),
        source_label="live-test structured evidence",
    )
    if fixture_issues:
        missing.append("business_fixture consistency: " + "; ".join(fixture_issues[:4]))

    if missing:
        return (
            "voice live_test.py is not production-equivalent: missing or weak "
            + "; ".join(missing)
            + ". For persistent_worker voice providers, live_test.py must prove "
            "the same production runtime payload, include safe "
            "input_context.instructions, preserve conversation_history, and use "
            "at least two task-specific turns."
        )
    if client is not None and llm_review_enabled and judge_model:
        try:
            review = _voice_semantic_review(
                client=client,
                model=judge_model,
                live_source=live,
                evidence=evidence,
            )
        except Exception:  # noqa: BLE001 - semantic review must not break the gate
            review = None
        if (
            isinstance(review, dict)
            and review.get("decision") == "block"
            and review.get("confidence") == "high"
        ):
            details = "; ".join(review.get("issues") or []) or review.get("rationale") or "semantic live-test evidence failed"
            return (
                "voice live_test.py semantic evidence is not production-equivalent: "
                + details
            )
    return None


# ---------------------------------------------------------------------------
# Reflection-evidence gate (PR 2 — Goal/Planning/State/Reflection plan)
# ---------------------------------------------------------------------------
# When the builder signals HARNESS_COMPLETE, the build loop calls
# ``verify_reflection_complete`` after the structural + forensics gates
# pass. The gate REQUIRES ``_agent_state/reflection_phase_3.md`` to
# exist and to cite specific evidence (file:line refs, test output
# snippets, code excerpts). Self-attestation ("yes, handled") is
# rejected.
#
# The gate is evidence-first and re-runs against current files on each
# HARNESS_COMPLETE. Missing, vacuous, invalid-citation, or unsupported
# reflections return a short error string; the caller owns issue-specific
# retry budgeting and final acceptance/rejection. A later valid reflection
# can pass even if an earlier HARNESS_COMPLETE failed for a different
# reflection issue.
#
# The hybrid pattern + LLM-judge check lives in
# ``agent5/reflection_evidence_check.py``. This module owns the gate
# integration and the telemetry plumbing.


REFLECTION_PHASE_3_FILENAME = "reflection_phase_3.md"
"""Canonical filename inside the ``_agent_state/`` directory."""


def verify_reflection_complete(
    sandbox_dir: "Path",
    candidate: "ScreenedCandidate",
    *,
    client: "anthropic.Anthropic | None" = None,
    judge_model: str = "claude-sonnet-4-6",
    llm_judge_enabled: bool = True,
    logger: "logging.Logger | None" = None,
    trace_id: str = "",
) -> str | None:
    """Verify the agent's pre-HARNESS_COMPLETE reflection has evidence.

    Returns ``None`` when the reflection passes (or when the gate is in
    fail-soft mode and the LLM-judge errors). Returns a short error
    string when the reflection is missing or vacuous; the caller treats
    this like other gate retries — append the error to the next user
    message + the directive, then loop.

    Args:
        sandbox_dir: The candidate's sandbox.
        candidate: ScreenedCandidate (used for telemetry).
        client: Optional Anthropic client for the LLM-judge fallback.
            None disables the judge — pattern check alone decides.
        judge_model: Model ID for the judge call. Defaults to Sonnet 4.6.
        llm_judge_enabled: When False, BORDERLINE pattern verdicts
            accept rather than invoking the judge.
        logger: Standard logger for telemetry.
        trace_id: Run trace_id for log correlation.

    Telemetry events:
        * reflection_phase_3_missing - file absent at gate time.
        * reflection_phase_3_pattern_pass / _fail / _borderline - pattern
          check verdicts.
        * reflection_llm_judge_invoked / _accepted / _rejected - judge
          outcomes.
        * reflection_gate_retry - gate returned an error string (caller
          will inject directive).
        * reflection_gate_accepted - gate returned None.
    """
    from puzzleeval.agents.agent5 import reflection_evidence_check as rec

    reflection_path = sandbox_dir / "_agent_state" / REFLECTION_PHASE_3_FILENAME

    log_extra = {
        "trace_id": trace_id,
        "candidate_name": getattr(candidate, "name", ""),
    }

    if not reflection_path.exists():
        if logger is not None:
            logger.info(
                "Reflection phase 3 missing for %s",
                getattr(candidate, "name", ""),
                extra={
                    "operation": "reflection_phase_3_missing",
                    **log_extra,
                },
            )
        return (
            "_agent_state/reflection_phase_3.md is missing. Before "
            "HARNESS_COMPLETE is accepted, write the reflection (the "
            "orchestrator will inject the template). Each section MUST "
            "cite specific evidence (file references like harness.py:42, "
            "test output snippets, code excerpts). Self-attestation will "
            "be rejected."
        )

    try:
        reflection_md = reflection_path.read_text(encoding="utf-8")
    except OSError as exc:
        # Treat read failures as missing — fail open is too lenient when
        # the file IS there but can't be read. Caller retries with the
        # directive.
        if logger is not None:
            logger.warning(
                "Reflection read failed for %s: %s",
                getattr(candidate, "name", ""), exc,
                extra={
                    "operation": "reflection_phase_3_read_error",
                    **log_extra,
                },
            )
        return f"_agent_state/reflection_phase_3.md exists but is unreadable: {exc}"

    # Read upstream context for the LLM-judge.
    objective_path = sandbox_dir / "_agent_state" / "objective.md"
    objective_md = ""
    if objective_path.exists():
        try:
            objective_md = objective_path.read_text(encoding="utf-8")
        except OSError:
            objective_md = ""

    harness_code = _read_harness_code(sandbox_dir)

    final_verdict, evidence, judge_reason = rec.evaluate(
        reflection_md=reflection_md,
        objective_md=objective_md,
        harness_code=harness_code,
        sandbox_dir=sandbox_dir,
        client=client,
        judge_model=judge_model,
        llm_judge_enabled=llm_judge_enabled,
        logger=logger,
        trace_id=trace_id,
        candidate_name=getattr(candidate, "name", ""),
    )

    if logger is not None:
        logger.info(
            "Reflection phase 3 evaluated for %s: pattern=%s final=%s",
            getattr(candidate, "name", ""),
            evidence.verdict.value, final_verdict.value,
            extra={
                "operation": "reflection_phase_3_evaluated",
                "pattern_verdict": evidence.verdict.value,
                "final_verdict": final_verdict.value,
                "total_file_refs": evidence.total_file_refs,
                "total_code_blocks": evidence.total_code_blocks,
                "total_forensics_refs": evidence.total_forensics_refs,
                "word_count": evidence.word_count,
                "sections_with_evidence": evidence.sections_with_evidence,
                "missing_sections_count": len(evidence.missing_section_headers),
                "judge_reason": judge_reason,
                **log_extra,
            },
        )

    if final_verdict == rec.ReflectionVerdict.PASS:
        return None

    # FAIL — produce a directive-prompt-shaped error message.
    detail = evidence.reason
    if judge_reason:
        detail = f"{detail}; {judge_reason}"
    return (
        f"_agent_state/reflection_phase_3.md is present but lacks substantive "
        f"evidence: {detail}. Cite file:line references "
        f"(e.g. harness.py:42), test output snippets, or forensics events "
        f"for each section. The pre-HARNESS_COMPLETE reflection is the "
        f"agent's commitment that the harness meets objective.md SUCCESS "
        f"CRITERIA — self-attestation is not enough."
    )


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


def verify_session_continuity_from_forensics(
    sandbox_dir: Path,
    *,
    session_token: str | None = None,
) -> str | None:
    """WARN-tier runtime check for stateful conversation resets.

    Harnesses should emit canonical ``session_create`` on the first turn
    and ``session_reuse`` on later turns. A later ``session_create`` is
    usually evidence that production is reinitializing provider state on
    every turn. Explicit ``session_reconnect`` events are allowed because
    reconnect after an error is a legitimate recovery path.
    """
    if not GATE_SESSION_CONTINUITY_ENABLED:
        return None

    log_path = sandbox_dir / "harness_forensics.jsonl"
    if not log_path.exists():
        return None

    events: list[dict] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if session_token:
            token = event.get("session_token") or event.get("conversation_id")
            if token and str(token).split("-t", 1)[0] != session_token:
                continue
        events.append(event)

    creates: set[int] = set()
    reconnect_turns: set[int] = set()
    reuse_turns: set[int] = set()
    for event in events:
        event_name = str(event.get("event") or "")
        op_name = str(event.get("op") or "")
        # ``traced_op("session_create", turn_index=N)`` records
        # ``event=op_start/op_done`` and ``op=session_create``. Direct
        # ``log("session_create", ...)`` records the lifecycle in the
        # event field. Normalize both shapes, plus the older
        # session_create_start/session_create_done taxonomy, so the WARN
        # gate measures behavior instead of one logging spelling.
        if op_name.startswith("session_"):
            name = op_name
        else:
            name = event_name
        try:
            turn_index = int(event.get("turn_index"))
        except (TypeError, ValueError):
            continue
        if name == "session_create" or name.startswith("session_create_"):
            creates.add(turn_index)
        elif name == "session_reconnect" or name.startswith("session_reconnect_"):
            reconnect_turns.add(turn_index)
        elif name == "session_reuse" or name.startswith("session_reuse_"):
            reuse_turns.add(turn_index)

    late_creates = [
        turn for turn in creates
        if turn > 0 and turn not in reconnect_turns
    ]
    if late_creates:
        return (
            "session continuity warning: forensics shows session_create on "
            f"later turn(s) {late_creates}. Stateful voice/conversation "
            "harnesses should create the provider session once at turn 0 "
            "and emit session_reuse on later turns, unless a "
            "session_reconnect event explains recovery after an error."
        )
    if creates and max(creates) == 0 and len(creates) == 1 and not reuse_turns:
        return (
            "session continuity warning: forensics shows a single "
            "session_create but no session_reuse events on later turns. "
            "Emit session_reuse when reusing the provider handle so the "
            "operator can verify continuity."
        )
    return None


_KEEPALIVE_STREAM_TYPES = frozenset({
    "ping",
    "pong",
    "heartbeat",
    "keepalive",
    "keep_alive",
    "metadata",
    "metadata_only",
    "metadata-only",
    "rate_limits.updated",
    "session.updated",
    "session_update",
})


def _stream_event_type(event: dict) -> str:
    """Return the provider event type recorded by the forensics shim."""
    for key in ("type", "event_type", "provider_event", "provider_event_type"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _event_matches_session(event: dict, session_token: str | None) -> bool:
    if not session_token:
        return True
    token = event.get("session_token") or event.get("conversation_id")
    if not token:
        return True
    return str(token).split("-t", 1)[0] == session_token


def _is_timeout_like_event(event: dict) -> bool:
    fields = (
        event.get("event"),
        event.get("op"),
        event.get("error_type"),
        event.get("error"),
        event.get("end_reason"),
        event.get("status"),
    )
    return any("timeout" in str(value).lower() for value in fields if value)


def _is_recv_stream_event(event: dict) -> bool:
    event_name = str(event.get("event") or "")
    op_name = str(event.get("op") or "")
    if event_name != "stream_event" and op_name != "stream_event":
        return False
    direction = str(event.get("dir") or event.get("direction") or "").lower()
    return direction in ("recv", "receive", "in", "inbound")


def _is_keepalive_stream_event(event: dict) -> bool:
    typ = _stream_event_type(event)
    if not typ:
        return False
    if typ in _KEEPALIVE_STREAM_TYPES:
        return True
    return any(token in typ for token in ("ping", "pong", "heartbeat", "keepalive"))


def _is_output_bearing_stream_event(event: dict) -> bool:
    typ = _stream_event_type(event)
    if not typ or _is_keepalive_stream_event(event):
        return False
    # Provider event names vary, so classify positive evidence broadly
    # but only after excluding the known transport-maintenance events.
    return any(
        token in typ
        for token in (
            "audio",
            "text",
            "transcript",
            "delta",
            "message",
            "content",
            "chunk",
            "response.output",
            "agent_response",
        )
    )


def verify_stream_keepalive_only_from_forensics(
    sandbox_dir: Path,
    *,
    session_token: str | None = None,
    min_keepalive_tail: int = 3,
) -> str | None:
    """WARN-tier diagnostic for streams kept alive by transport events only.

    A correct streaming collector resets its idle timeout on meaningful
    output, not on ping/pong/heartbeat/metadata events. This diagnostic
    surfaces the failure pattern where a harness receives only keepalive
    traffic until a hard timeout.
    """
    if not GATE_STREAM_KEEPALIVE_DIAGNOSTIC_ENABLED:
        return None

    log_path = sandbox_dir / "harness_forensics.jsonl"
    if not log_path.exists():
        return None

    events: list[dict] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and _event_matches_session(event, session_token):
            events.append(event)

    if not events or not any(_is_timeout_like_event(event) for event in events):
        return None

    recv_stream_events = [
        event for event in events
        if _is_recv_stream_event(event)
    ]
    if not recv_stream_events:
        return None

    last_output_index = -1
    for idx, event in enumerate(recv_stream_events):
        if _is_output_bearing_stream_event(event):
            last_output_index = idx

    tail = recv_stream_events[last_output_index + 1:]
    keepalive_tail = [event for event in tail if _is_keepalive_stream_event(event)]
    if len(keepalive_tail) < min_keepalive_tail or len(keepalive_tail) != len(tail):
        return None

    event_types = [_stream_event_type(event) or "unknown" for event in keepalive_tail[-5:]]
    if last_output_index >= 0:
        return (
            "stream keepalive warning: forensics shows output-bearing stream "
            f"events followed by {len(keepalive_tail)} keepalive/metadata "
            f"event(s) until timeout ({event_types}). Streaming collectors "
            "should reset idle timers only on meaningful response output, "
            "not ping/pong/heartbeat/metadata traffic."
        )
    return (
        "stream keepalive warning: forensics shows keepalive/metadata "
        f"stream traffic until timeout with no output-bearing event "
        f"({event_types}). The harness may be treating transport "
        "keepalives as response progress; reset idle timers only on "
        "meaningful output events."
    )


_EXTERNAL_PROVIDER_BLOCK_PATTERNS = (
    "quota",
    "credit",
    "credits",
    "insufficient balance",
    "billing",
    "payment required",
    "subscription",
    "rate limit",
    "too many requests",
    "429",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "permission denied",
)


def classify_external_provider_block_from_forensics(
    sandbox_dir: Path,
    *,
    session_token: str | None = None,
) -> dict[str, Any] | None:
    """Return provider/account block evidence from forensics, if present.

    This is deliberately provider-agnostic. It does not decide that every
    timeout is an account problem. It only classifies high-signal evidence:
    quota/auth/billing/rate-limit text in forensics, or the streaming
    keepalive-only diagnostic that already requires timeout evidence.
    """
    log_path = sandbox_dir / "harness_forensics.jsonl"
    if not log_path.exists():
        return None

    matched: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or not _event_matches_session(event, session_token):
            continue
        haystack = json.dumps(event, ensure_ascii=False, default=str).lower()
        if any(pattern in haystack for pattern in _EXTERNAL_PROVIDER_BLOCK_PATTERNS):
            matched.append({
                "event": event.get("event") or event.get("op") or "unknown",
                "turn_index": event.get("turn_index"),
                "type": event.get("type") or event.get("event_type"),
                "preview": haystack[:500],
            })
            if len(matched) >= 3:
                break

    if matched:
        return {
            "status": "external_provider_blocked",
            "reason": "provider/account/quota/auth evidence found in forensics",
            "evidence": matched,
        }

    keepalive_warning = verify_stream_keepalive_only_from_forensics(
        sandbox_dir,
        session_token=session_token,
    )
    if keepalive_warning:
        return {
            "status": "external_provider_blocked",
            "reason": keepalive_warning,
            "evidence": [],
        }
    return None


__all__ = [
    "classify_external_provider_block_from_forensics",
    "run_verification_checks",
    "verify_forensics_coverage",
    "verify_session_continuity_from_forensics",
    "verify_stream_keepalive_only_from_forensics",
]
