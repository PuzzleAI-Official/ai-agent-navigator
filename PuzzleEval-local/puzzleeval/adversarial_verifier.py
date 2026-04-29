"""Adversarial harness verification.

Modeled on Claude Code's `verificationAgent.ts`: don't confirm the happy
path, *try to break the implementation*. After Agent 5 builds a harness
and signals HARNESS_COMPLETE on smoke + one live call, this module runs
a structured battery of adversarial probes to surface failures before the
real test cases run.

Probes are PRINCIPLE-BASED (not capability-specific). They apply to every
HTTP harness regardless of domain:

  1. **Empty / minimal input** — does the harness gracefully handle the
     smallest valid payload? (Many APIs reject empty multipart parts with
     opaque 400s.)

  2. **Maximum / oversized input** — feed something near the documented
     ceiling (or 10x the smoke-test size if no ceiling). Ensures the
     harness doesn't truncate or buffer-overflow.

  3. **Malformed input** — random non-printable bytes, emoji-heavy text,
     mixed encodings. Ensures the harness sends what was given without
     mangling, and surfaces API errors as ``success=False`` rather than
     crashing.

  4. **Idempotency probe** — call the harness twice with the SAME input.
     For read-only APIs this should return equivalent outputs (modulo
     timestamps). For write APIs, it surfaces whether the harness uses
     dedup keys or creates duplicate records (Gap 9 read-only-vs-writes
     overlap).

  5. **Concurrency probe** — fire 3 simultaneous calls. Surfaces races,
     shared-state bugs, and the API's actual concurrent-request behavior
     (some return 429, some hang, some succeed).

  6. **Auth-error probe** — call once with a deliberately corrupted
     credential. Confirms the harness routes auth failures into
     ``success=False`` instead of crashing or silently returning empty.

These probes do NOT replace Agent 3's domain test cases. They run BEFORE
the domain tests and tell us whether the harness itself is fit for the
test execution phase. A harness that fails the auth-error probe will
silently corrupt ALL of Agent 3's test results.

The probes execute INSIDE the harness's own sandbox via the same Python
subprocess interface the production test runner uses. The probe results
are recorded on ``TestHarness.adversarial_findings`` and surfaced on
``Agent5Result.adversarial_summary`` for the report.
"""

from __future__ import annotations

import json
import logging
import os
import random
import string
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Probe definitions — each yields a tuple (label, payload, expected_outcome)
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    label: str
    passed: bool
    outcome: str  # "graceful_success" | "graceful_failure" | "crash" | "silent_corruption"
    detail: str
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdversarialReport:
    """Compact summary attached to TestHarness post-build.

    ``critical_failures`` are probes whose outcome was crash or
    silent_corruption — they invalidate the harness for production
    test execution. ``warnings`` are graceful_failures that the
    underlying API genuinely cannot handle (acceptable; the harness
    correctly surfaced the failure).
    """
    harness_ready: bool
    probe_results: list[ProbeResult] = field(default_factory=list)
    critical_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Harness invocation — same subprocess shape implement_test_env uses
# ---------------------------------------------------------------------------


def _venv_python(sandbox_dir: Path) -> Path:
    """Find the venv python interpreter inside this candidate's sandbox.

    Mirrors the platform-aware path resolution used by Agent 5's
    _execute_single_test.
    """
    if sys.platform == "win32":
        candidate = sandbox_dir / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = sandbox_dir / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def _invoke_harness(
    sandbox_dir: Path,
    payload: dict[str, Any],
    credentials: dict[str, str] | None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Run harness.py with the given input via a subprocess driver script.

    Returns the harness's parsed JSON result, or a structured error dict
    when the subprocess crashed / timed out / produced non-JSON output.
    """
    # Harness contract (see BUILDER_SYSTEM_PROMPT): `run(input_data: dict) -> dict`
    # — a SINGLE positional dict argument. The driver must pass the probe
    # payload as that dict, not splat it as kwargs.
    #
    # Real-run bug #1 (trace real_debug_4b + real_debug_5): this line used
    # to be `run(**payload)`. Every call became `run(text="...", ...)`
    # which doesn't match the `run(input_data)` signature, so EVERY probe
    # raised TypeError and the battery reported every harness as "crash".
    # Fixed by passing `payload` positionally.
    #
    # Real-run bug #2 (trace real_debug_6): `from harness import run`
    # used to sit OUTSIDE the try/except, so any module-level ImportError
    # (e.g., missing `requests` package if pip install didn't complete)
    # propagated uncaught, subprocess printed the traceback to stderr,
    # stdout was empty, and the battery reported `non_json_output` — a
    # mystery classification that hid the real cause. Moved the import
    # inside the try so import-time errors become structured
    # `_subprocess_ok=False` payloads the battery can reason about.
    driver = (
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "try:\n"
        "    from harness import run\n"
        "    result = run(payload)\n"
        "    print(json.dumps({'_subprocess_ok': True, 'result': result}, default=str))\n"
        "except SystemExit:\n"
        "    raise\n"
        "except Exception as exc:\n"
        "    import traceback\n"
        "    print(json.dumps({'_subprocess_ok': False, 'error_type': type(exc).__name__, 'error': str(exc), 'tb': traceback.format_exc()}, default=str))\n"
    )
    env = os.environ.copy()
    if credentials:
        env.update(credentials)
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [str(_venv_python(sandbox_dir)), "-c", driver],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(sandbox_dir),
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "_invocation": "timeout",
            "duration_ms": (time.perf_counter() - start) * 1000,
        }
    except Exception as exc:
        return {
            "_invocation": "subprocess_error",
            "error": str(exc),
            "duration_ms": (time.perf_counter() - start) * 1000,
        }

    duration_ms = (time.perf_counter() - start) * 1000
    stdout = (proc.stdout or "").strip()
    last_line = stdout.split("\n")[-1] if stdout else ""
    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(last_line)
    except json.JSONDecodeError:
        parsed = {
            "_subprocess_ok": False,
            "error": "non_json_output",
            "stdout_tail": stdout[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        }
    parsed["duration_ms"] = duration_ms
    parsed["_returncode"] = proc.returncode
    return parsed


# ---------------------------------------------------------------------------
# Probes — each is a small pure function that interprets the harness response
# ---------------------------------------------------------------------------


def _classify(parsed: dict[str, Any]) -> str:
    """Map a parsed harness invocation to one of the 4 outcome buckets."""
    if parsed.get("_invocation") == "timeout":
        return "crash"  # hung > timeout = crash for our purposes
    if parsed.get("_invocation") == "subprocess_error":
        return "crash"
    if not parsed.get("_subprocess_ok", True):
        # Subprocess raised an uncaught Python exception — crash.
        return "crash"
    result = parsed.get("result")
    if not isinstance(result, dict):
        return "silent_corruption"
    if "success" not in result:
        return "silent_corruption"
    if result.get("success"):
        return "graceful_success"
    if result.get("error") or result.get("error_message"):
        return "graceful_failure"
    return "silent_corruption"


def _probe_empty_input(sandbox_dir: Path, sample_input: dict[str, Any], creds) -> ProbeResult:
    payload = {k: ("" if isinstance(v, str) else v) for k, v in sample_input.items()}
    parsed = _invoke_harness(sandbox_dir, payload, creds, timeout=45.0)
    outcome = _classify(parsed)
    passed = outcome in {"graceful_success", "graceful_failure"}
    return ProbeResult(
        label="empty_input",
        passed=passed,
        outcome=outcome,
        detail=_summarize(parsed),
        latency_ms=parsed.get("duration_ms", 0.0),
        raw=parsed,
    )


def _probe_max_input(sandbox_dir: Path, sample_input: dict[str, Any], creds) -> ProbeResult:
    """Inflate the largest string field 10x; integer fields stay (avoid
    overflow on APIs that reject silly values). 10x is conservative — we
    want to surface buffer / serialization issues without triggering the
    API's documented 'too large' error path (that's what _probe_malformed is for)."""
    payload = dict(sample_input)
    target = max(
        ((k, v) for k, v in payload.items() if isinstance(v, str)),
        key=lambda kv: len(kv[1]),
        default=None,
    )
    if target is None:
        return ProbeResult(
            label="max_input",
            passed=True,
            outcome="graceful_success",
            detail="No string fields to inflate; probe skipped.",
        )
    k, v = target
    payload[k] = (v or "x") * 10
    parsed = _invoke_harness(sandbox_dir, payload, creds, timeout=60.0)
    outcome = _classify(parsed)
    passed = outcome in {"graceful_success", "graceful_failure"}
    return ProbeResult(
        label="max_input",
        passed=passed,
        outcome=outcome,
        detail=_summarize(parsed),
        latency_ms=parsed.get("duration_ms", 0.0),
        raw=parsed,
    )


def _probe_malformed(sandbox_dir: Path, sample_input: dict[str, Any], creds) -> ProbeResult:
    """Random non-printable + emoji + mixed scripts in the largest string field.

    A correct harness either succeeds (the API accepts it) or surfaces the
    failure as ``success=False``. A crashing harness has a string-encoding
    bug or a fragile JSON serializer.
    """
    payload = dict(sample_input)
    weird = "".join(random.choice(string.printable + "🔥💥αβγ漢字🌍\x00\x01\x7f") for _ in range(64))
    target = next((k for k, v in payload.items() if isinstance(v, str)), None)
    if target is None:
        return ProbeResult(
            label="malformed_input",
            passed=True,
            outcome="graceful_success",
            detail="No string fields to corrupt; probe skipped.",
        )
    payload[target] = weird
    parsed = _invoke_harness(sandbox_dir, payload, creds, timeout=45.0)
    outcome = _classify(parsed)
    passed = outcome in {"graceful_success", "graceful_failure"}
    return ProbeResult(
        label="malformed_input",
        passed=passed,
        outcome=outcome,
        detail=_summarize(parsed),
        latency_ms=parsed.get("duration_ms", 0.0),
        raw=parsed,
    )


def _probe_idempotency(sandbox_dir: Path, sample_input: dict[str, Any], creds) -> ProbeResult:
    """Run the same input twice. For read-only APIs the outputs should be
    structurally similar. We don't enforce equality — just that the second
    call doesn't crash differently than the first."""
    a = _invoke_harness(sandbox_dir, sample_input, creds, timeout=45.0)
    b = _invoke_harness(sandbox_dir, sample_input, creds, timeout=45.0)
    outcome_a = _classify(a)
    outcome_b = _classify(b)
    if outcome_a == "crash" or outcome_b == "crash":
        return ProbeResult(
            label="idempotency",
            passed=False,
            outcome="crash",
            detail=f"call_a={outcome_a}, call_b={outcome_b}; one or both calls crashed",
            latency_ms=(a.get("duration_ms", 0.0) + b.get("duration_ms", 0.0)),
            raw={"a": a, "b": b},
        )
    keys_a = set((a.get("result") or {}).keys()) if isinstance(a.get("result"), dict) else set()
    keys_b = set((b.get("result") or {}).keys()) if isinstance(b.get("result"), dict) else set()
    structural_match = keys_a == keys_b or (not keys_a and not keys_b)
    return ProbeResult(
        label="idempotency",
        passed=structural_match,
        outcome=outcome_b if structural_match else "silent_corruption",
        detail=(
            "Both calls returned same top-level shape."
            if structural_match
            else f"Shape drift between calls: keys_a={sorted(keys_a)}, keys_b={sorted(keys_b)}"
        ),
        latency_ms=(a.get("duration_ms", 0.0) + b.get("duration_ms", 0.0)),
        raw={"a": a, "b": b},
    )


def _probe_concurrency(
    sandbox_dir: Path, sample_input: dict[str, Any], creds, n: int = 3
) -> ProbeResult:
    """Fire ``n`` simultaneous calls. Surfaces races and the API's actual
    concurrent-request behavior (429 / 503 / hang / success). All-crash =
    fail; any-crash = warning; all-non-crash = pass."""
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(_invoke_harness, sandbox_dir, sample_input, creds, 60.0)
            for _ in range(n)
        ]
        outcomes = [_classify(f.result()) for f in as_completed(futures)]
    crash_count = sum(1 for o in outcomes if o == "crash")
    silent = sum(1 for o in outcomes if o == "silent_corruption")
    if crash_count == n or silent == n:
        passed, label_outcome = False, "crash"
    elif crash_count > 0 or silent > 0:
        passed, label_outcome = True, "graceful_failure"  # warning, not block
    else:
        passed, label_outcome = True, "graceful_success"
    return ProbeResult(
        label="concurrency",
        passed=passed,
        outcome=label_outcome,
        detail=f"outcomes={outcomes}",
    )


def _probe_auth_error(sandbox_dir: Path, sample_input: dict[str, Any], creds) -> ProbeResult:
    """Replace each credential with garbage and call once. A correct harness
    routes auth failure into ``success=False``; a broken one crashes or
    returns empty success. This is the single most important probe for
    detecting harnesses that silently corrupt the test result set."""
    if not creds:
        return ProbeResult(
            label="auth_error",
            passed=True,
            outcome="graceful_success",
            detail="No credentials configured; probe skipped (open API).",
        )
    fake = {k: "obviously_invalid_credential_value_xyz" for k in creds}
    parsed = _invoke_harness(sandbox_dir, sample_input, fake, timeout=45.0)
    outcome = _classify(parsed)
    if outcome == "graceful_success":
        # API returned success even with garbage creds — silent corruption upstream.
        return ProbeResult(
            label="auth_error",
            passed=False,
            outcome="silent_corruption",
            detail="Harness returned success with corrupted credentials — auth not verified.",
            raw=parsed,
        )
    if outcome == "graceful_failure":
        return ProbeResult(
            label="auth_error",
            passed=True,
            outcome="graceful_failure",
            detail="Harness correctly surfaced auth failure as success=False.",
            raw=parsed,
        )
    return ProbeResult(
        label="auth_error",
        passed=False,
        outcome="crash",
        detail="Harness crashed on bad credentials instead of returning success=False.",
        raw=parsed,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _precheck_sandbox_imports(
    sandbox_dir: Path, credentials: dict[str, str] | None
) -> tuple[bool, str | None]:
    """Run the harness's module-level imports in its venv; return
    ``(ok, error_summary)``.

    Real-run signal (trace real_debug_6): a sandbox's .venv had `pip`
    installed but NOT `requests`, even though the build-time smoke test
    had passed. The adversarial battery then saw every probe fail with
    the opaque ``non_json_output`` error when what really happened was
    ``ModuleNotFoundError: requests`` at import time. This pre-check
    fires BEFORE the probes so a setup-level failure is reported as
    such (and auto-repaired once) instead of being misattributed to the
    harness.
    """
    parsed = _invoke_harness(sandbox_dir, {}, credentials, timeout=30.0)
    if parsed.get("_subprocess_ok") is False and parsed.get("error_type") in (
        "ModuleNotFoundError", "ImportError"
    ):
        return False, f"{parsed.get('error_type')}: {parsed.get('error')}"
    # Invocation-level failure (timeout, subprocess error) is also a
    # setup signal — the sandbox couldn't launch.
    if parsed.get("_invocation") in ("timeout", "subprocess_error"):
        return False, f"{parsed.get('_invocation')}: {parsed.get('error', '')}"
    return True, None


def _attempt_pip_install_requirements(sandbox_dir: Path) -> bool:
    """One-shot ``pip install -r requirements.txt`` in the sandbox venv.

    Returns True on success (exit 0), False otherwise. Silent — caller
    decides what to do with the result.
    """
    req = sandbox_dir / "requirements.txt"
    if not req.exists():
        return False
    try:
        proc = subprocess.run(
            [str(_venv_python(sandbox_dir)), "-m", "pip", "install",
             "-r", str(req), "--quiet", "--disable-pip-version-check"],
            capture_output=True, text=True, cwd=str(sandbox_dir),
            timeout=180,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def run_adversarial_battery(
    sandbox_dir: Path,
    sample_input: dict[str, Any],
    credentials: dict[str, str] | None,
    enabled_probes: list[str] | None = None,
) -> AdversarialReport:
    """Run all enabled probes against the harness in ``sandbox_dir``.

    ``sample_input`` is a representative input dict to mutate per probe —
    typically the harness's smoke_test input or the first Agent 3 test
    case after adaptation.

    Returns ``AdversarialReport``. ``harness_ready`` is False when ANY
    probe crashed or showed silent_corruption; the production test runner
    should refuse to execute Agent 3 cases against a not-ready harness.

    Before probing, runs a setup pre-check: if the harness can't even
    import in its venv, the battery tries ``pip install -r requirements.txt``
    once and re-checks. If import still fails, marks the sandbox as
    ``sandbox_broken`` rather than blaming the harness — test execution
    will still run (and surface the real setup error cleanly).
    """
    all_probes = {
        "empty_input": _probe_empty_input,
        "max_input": _probe_max_input,
        "malformed_input": _probe_malformed,
        "idempotency": _probe_idempotency,
        "concurrency": _probe_concurrency,
        "auth_error": _probe_auth_error,
    }
    selected = enabled_probes or list(all_probes.keys())
    report = AdversarialReport(harness_ready=True)
    start = time.perf_counter()

    # ── Setup pre-check: venv sane enough to import the harness? ──
    ok, err = _precheck_sandbox_imports(sandbox_dir, credentials)
    if not ok:
        # Try self-heal: reinstall requirements once.
        _attempt_pip_install_requirements(sandbox_dir)
        ok, err = _precheck_sandbox_imports(sandbox_dir, credentials)
    if not ok:
        # Sandbox genuinely broken. Emit a distinct `sandbox_broken`
        # signal — harness_ready STAYS True so test execution proceeds
        # and surfaces the real failure through the normal path (e.g.,
        # the first real test case will fail with the same ImportError
        # in a context the user can action). The warning is recorded
        # for observability but doesn't block.
        report.warnings.append(
            f"sandbox_broken: harness cannot import in its venv ({err}). "
            f"Adversarial probes skipped; test execution will expose the "
            f"real failure mode."
        )
        report.total_duration_ms = (time.perf_counter() - start) * 1000
        return report

    for probe_name in selected:
        probe = all_probes.get(probe_name)
        if probe is None:
            continue
        try:
            result = probe(sandbox_dir, sample_input, credentials)
        except Exception as exc:
            result = ProbeResult(
                label=probe_name,
                passed=False,
                outcome="crash",
                detail=f"probe runner error: {exc}",
            )
        report.probe_results.append(result)
        if result.outcome in {"crash", "silent_corruption"} and not result.passed:
            report.critical_failures.append(f"{result.label}: {result.detail}")
            report.harness_ready = False
        elif not result.passed:
            report.warnings.append(f"{result.label}: {result.detail}")
    report.total_duration_ms = (time.perf_counter() - start) * 1000
    return report


def report_to_dict(report: AdversarialReport) -> dict[str, Any]:
    """Serialize for storage on TestHarness / SSE emission."""
    return {
        "harness_ready": report.harness_ready,
        "probe_count": len(report.probe_results),
        "critical_failure_count": len(report.critical_failures),
        "warning_count": len(report.warnings),
        "critical_failures": report.critical_failures,
        "warnings": report.warnings,
        "total_duration_ms": round(report.total_duration_ms, 2),
        "probes": [
            {
                "label": p.label,
                "passed": p.passed,
                "outcome": p.outcome,
                "detail": p.detail[:500],
                "latency_ms": round(p.latency_ms, 2),
            }
            for p in report.probe_results
        ],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize(parsed: dict[str, Any]) -> str:
    if parsed.get("_invocation") in {"timeout", "subprocess_error"}:
        return f"{parsed.get('_invocation')}: {parsed.get('error', '')[:200]}"
    if not parsed.get("_subprocess_ok", True):
        return f"crash: {parsed.get('error_type', 'Unknown')}: {parsed.get('error', '')[:200]}"
    result = parsed.get("result")
    if isinstance(result, dict):
        ok = result.get("success")
        err = (result.get("error") or "")[:200]
        return f"success={ok}, error={err}" if err else f"success={ok}"
    return f"non-dict result: {str(result)[:200]}"


__all__ = [
    "AdversarialReport",
    "ProbeResult",
    "report_to_dict",
    "run_adversarial_battery",
]
