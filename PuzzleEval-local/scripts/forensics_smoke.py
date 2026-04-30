"""End-to-end smoke test for the harness forensics layer.

Stages the auto-injected `_forensics.py` shim into a temp sandbox,
writes a minimal `harness.py` that exercises the universal API + the
auto-instrumentation hooks, runs it as a subprocess, and asserts the
JSONL log file has the expected events.

Run:
    python scripts/forensics_smoke.py

Exit code 0 = all checks passed; non-zero = at least one check failed
(prints which one).

This script is the ground-truth verification that the forensics layer
ACTUALLY works end-to-end. The unit tests in test_forensics_layer.py
cover the building blocks; this script covers the integration.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


SAMPLE_HARNESS = '''
"""Smoke harness — exercises the forensics layer's universal API."""
from _forensics import log, traced_op

# Universal log() — emits a custom event with our standard fields
log("custom_test_event", op="hello", provider="smoke", value=42)

# traced_op() — emits op_start + op_done with timing
with traced_op("noop", provider="smoke"):
    x = sum(range(100))

# traced_op() with an exception — emits op_error and re-raises
try:
    with traced_op("fail_on_purpose", provider="smoke"):
        raise ValueError("expected — testing op_error path")
except ValueError:
    pass

# Confirm credential redaction works
log("test_url", url="https://api.example.com/?token=SECRET&other=keep")
log("test_headers", headers={
    "Authorization": "Bearer SHOULD_NOT_LEAK",
    "Content-Type": "application/json",
})
'''


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    from puzzleeval.agents.agent5.sandbox import stage_forensics_shim  # noqa: E402

    failures: list[str] = []

    def check(condition: bool, msg: str) -> None:
        if not condition:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # Stage shim
        ok = stage_forensics_shim(tmp)
        check(ok, "stage_forensics_shim returned False")
        check((tmp / "_forensics.py").exists(), "_forensics.py not staged")

        # Write + run sample harness
        (tmp / "harness.py").write_text(SAMPLE_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "harness.py"],
            cwd=str(tmp),
            capture_output=True,
            text=True,
            timeout=30,
        )
        check(proc.returncode == 0,
              f"harness exited {proc.returncode}; stderr: {proc.stderr[:500]}")

        # Read JSONL log
        log_path = tmp / "harness_forensics.jsonl"
        check(log_path.exists(), "harness_forensics.jsonl not produced")
        if not log_path.exists():
            print("FAIL: log file missing", file=sys.stderr)
            return 1
        events = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                failures.append(f"non-JSON line in log: {line[:80]} ({exc})")

        # Check events present
        names = [e.get("event") for e in events]
        check("harness_start" in names, "harness_start auto-event missing")
        check("custom_test_event" in names, "custom_test_event missing")
        check("harness_exit" in names, "harness_exit atexit event missing")

        # traced_op events: op_start + op_done for noop; op_error for fail
        op_starts = [e for e in events if e.get("event") == "op_start"]
        op_dones = [e for e in events if e.get("event") == "op_done"]
        op_errors = [e for e in events if e.get("event") == "op_error"]
        check(any(e["op"] == "noop" for e in op_starts), "noop op_start missing")
        check(any(e["op"] == "noop" for e in op_dones), "noop op_done missing")
        noop_done = next((e for e in op_dones if e["op"] == "noop"), None)
        if noop_done:
            check("duration_ms" in noop_done, "op_done missing duration_ms")
        check(any(e["op"] == "fail_on_purpose" for e in op_errors),
              "fail_on_purpose op_error missing")

        # Credential redaction
        log_text = log_path.read_text(encoding="utf-8")
        check("SECRET" not in log_text,
              "URL token leaked into forensics log (?token=SECRET)")
        check("SHOULD_NOT_LEAK" not in log_text,
              "Authorization header leaked into forensics log")
        check("[REDACTED]" in log_text,
              "[REDACTED] sentinel missing — redaction not applied")

        # t_ms field present on every event
        check(all("t_ms" in e for e in events),
              "some events missing t_ms field")

        # Print summary
        print(f"Events recorded: {len(events)}")
        print(f"Event names: {sorted(set(names))}")
        print(f"Log size: {len(log_text)} bytes")

    if failures:
        print("\nFAIL:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\nPASS: forensics layer smoke test green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
