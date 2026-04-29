"""Pre-flight check — run BEFORE a real $$$ pipeline run.

Catches the common failure modes that would waste money debugging on a
live run:

  1. Import errors in any module we touched this session
  2. Config values (EVAL_STRATEGY, EVAL_PROGRAMMATIC_CHAINING_ENABLED, etc.)
     are actually what we think at runtime
  3. Plugin registration + is_available() matches environment reality
  4. Required credentials present (ANTHROPIC_API_KEY + optional plugin keys)
  5. One real but cheap Anthropic call proves network + auth work
  6. SSE event emission set ↔ frontend handler set (matrix match)
  7. Schema round-trip on realistic ScreenedCandidate through report
  8. Disk writable + runs/ exists + plugin ports free
  9. Backend FastAPI boots + lifespan registers plugins

Exit code 0 = ready to go. Exit 1 = at least one blocker.

Usage:
    cd PuzzleEval-local
    python scripts/preflight_check.py
    # or with cheap API call skipped:
    python scripts/preflight_check.py --no-api
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import sys
import traceback
from pathlib import Path
from typing import Any

# Make puzzleeval importable when script is run directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Colors (no external dep)
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


class Check:
    __slots__ = ("name", "status", "detail", "blocker")
    def __init__(self, name: str, status: str, detail: str = "", blocker: bool = False):
        self.name = name
        self.status = status  # "pass" | "warn" | "fail"
        self.detail = detail
        self.blocker = blocker


RESULTS: list[Check] = []


def record(name: str, status: str, detail: str = "", blocker: bool = False):
    RESULTS.append(Check(name, status, detail, blocker))
    # ASCII-safe icons — Windows cp1252 console chokes on Unicode checkmarks.
    icon = {"pass": f"{GREEN}[OK]{RESET}", "warn": f"{YELLOW}[!!]{RESET}",
            "fail": f"{RED}[XX]{RESET}"}[status]
    print(f"  {icon} {name}", flush=True)
    if detail:
        for line in detail.strip().splitlines():
            print(f"      {line}", flush=True)


def section(title: str):
    print(f"\n{BOLD}{CYAN}{title}{RESET}", flush=True)


# -----------------------------------------------------------------------------
# 1. Import check — every module we touched must import cleanly
# -----------------------------------------------------------------------------


def check_imports():
    section("1. Import verification")
    modules = [
        "puzzleeval.config",
        "puzzleeval.schemas",
        "puzzleeval.validators",
        "puzzleeval.plugin_tool_runner",
        "puzzleeval.report",
        "puzzleeval.agents.user_understanding",
        "puzzleeval.agents.research",
        "puzzleeval.agents.synthetic_tests",
        "puzzleeval.agents.synthetic_tests_file",
        "puzzleeval.agents.screening",
        "puzzleeval.agents.implement_test_env",
        "puzzleeval.tool_plugins",
        "puzzleeval.tool_plugins.code_execution",
        "puzzleeval.tool_plugins.vision",
        "puzzleeval.tool_plugins.transcription",
        "puzzleeval.tool_plugins.tts",
        "puzzleeval.tool_plugins.conversation_simulator",
        "puzzleeval.tool_plugins.webhook_receiver",
        "puzzleeval.tool_plugins.outbound_delivery",
        "puzzleeval.tool_plugins.voice_realtime",
    ]
    for mod in modules:
        try:
            importlib.import_module(mod)
            record(mod, "pass")
        except Exception as exc:  # noqa: BLE001
            record(mod, "fail", f"{type(exc).__name__}: {exc}", blocker=True)


# -----------------------------------------------------------------------------
# 2. Config snapshot
# -----------------------------------------------------------------------------


def check_config():
    section("2. Config snapshot")
    try:
        from puzzleeval import config as cfg
    except Exception as exc:
        record("config import", "fail", str(exc), blocker=True)
        return
    keys = [
        "EVAL_STRATEGY", "EVAL_TOOL_SEARCH_THRESHOLD", "EVAL_MAX_ITERATIONS",
        "EVAL_PROGRAMMATIC_CHAINING_ENABLED",
        "AGENT5_MAX_CANDIDATES", "AGENT5_MAX_TURNS", "AGENT5_MAX_BUDGET_TOTAL",
        "AGENT5_MAX_BUDGET_PER_CANDIDATE", "USER_SELECTION_ENABLED",
        "RATE_LIMIT_ENABLED", "RESEARCH_DUAL_SEARCH_ENABLED",
        "AGENT1_MODEL", "DEFAULT_MODEL", "AGENT5_BUILDER_MODEL",
        # Note: CACHING_ENABLED is a per-agent module constant (not in
        # puzzleeval.config). Each agent sets it based on its own needs
        # (Agent 5 uses prompt caching via cache_control; Agents 1-4
        # disable it because conversation-loop overhead dominates the
        # per-turn savings). Don't check here.
    ]
    expected_defaults = {
        "EVAL_STRATEGY": "tool_runner",
        "EVAL_PROGRAMMATIC_CHAINING_ENABLED": True,
        "USER_SELECTION_ENABLED": True,
    }
    for k in keys:
        if not hasattr(cfg, k):
            record(k, "fail", "config attribute missing", blocker=True)
            continue
        val = getattr(cfg, k)
        status = "pass"
        detail = f"value={val!r}"
        if k in expected_defaults and val != expected_defaults[k]:
            status = "warn"
            detail += f"  (expected default {expected_defaults[k]!r})"
        record(k, status, detail)


# -----------------------------------------------------------------------------
# 3. Plugin readiness matrix
# -----------------------------------------------------------------------------


def check_plugins():
    section("3. Plugin readiness matrix")
    try:
        from puzzleeval.tool_plugins import list_plugins
    except Exception as exc:
        record("tool_plugins import", "fail", str(exc), blocker=True)
        return
    plugins = list_plugins()
    if len(plugins) < 7:
        record("plugin count", "warn",
               f"expected 7+ plugins, got {len(plugins)}")
    else:
        record(f"plugin count = {len(plugins)}", "pass")
    for p in plugins:
        try:
            caps = p.capabilities()
            ok, reason = p.is_available()
        except Exception as exc:
            record(p.name, "fail", f"capabilities/is_available crashed: {exc}",
                   blocker=True)
            continue
        # Pass = available. Warn = unavailable with clear reason (not a blocker
        # for generic runs, only for scenarios using this plugin).
        if ok:
            record(
                f"{p.name}",
                "pass",
                f"input={caps.input_types} output={caps.output_types} "
                f"notes={(caps.notes or '')[:60]!r}",
            )
        else:
            record(f"{p.name}", "warn", f"unavailable: {reason}")


# -----------------------------------------------------------------------------
# 4. Credential check
# -----------------------------------------------------------------------------


def check_credentials():
    section("4. Credential check")
    required = ["ANTHROPIC_API_KEY"]
    optional_for_voice = [
        "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
        "DEEPGRAM_API_KEY", "ASSEMBLYAI_API_KEY",
    ]
    for k in required:
        val = os.environ.get(k)
        if not val:
            record(k, "fail", "missing (pipeline will not run)", blocker=True)
        elif len(val) < 20:
            record(k, "warn", f"suspiciously short ({len(val)} chars)")
        else:
            record(k, "pass", f"present ({len(val)} chars)")
    present_optional = [k for k in optional_for_voice if os.environ.get(k)]
    if present_optional:
        record("voice plugin keys", "pass",
               f"present: {', '.join(present_optional)}")
    else:
        record("voice plugin keys", "warn",
               "no OPENAI/ELEVENLABS/DEEPGRAM/ASSEMBLYAI key — voice "
               "evaluations will fall back to text-only paths")


# -----------------------------------------------------------------------------
# 5. Cheap Anthropic API call (confirms key + network)
# -----------------------------------------------------------------------------


def check_anthropic_api(skip: bool):
    section("5. Anthropic API connectivity")
    if skip:
        record("cheap hello-world call", "warn",
               "skipped via --no-api (no $$$ spent)")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        record("cheap hello-world call", "fail",
               "ANTHROPIC_API_KEY not set", blocker=True)
        return
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with: OK"}],
        )
        text = ""
        for block in resp.content or []:
            if hasattr(block, "text"):
                text += block.text
        if "OK" in text.upper():
            record("cheap hello-world call",
                   "pass",
                   f"model responded, tokens_in={resp.usage.input_tokens} "
                   f"tokens_out={resp.usage.output_tokens}")
        else:
            record("cheap hello-world call", "warn",
                   f"model responded but text was {text!r}")
    except Exception as exc:
        record("cheap hello-world call", "fail",
               f"{type(exc).__name__}: {exc}", blocker=True)


# -----------------------------------------------------------------------------
# 6. SSE event coverage matrix — emits vs handlers
# -----------------------------------------------------------------------------


def check_sse_coverage():
    section("6. SSE event emit vs frontend handler")
    import re
    backend_path = (
        ROOT.parent / "puzzleeval-api" / "services" / "pipeline_runner.py"
    )
    frontend_hook = (
        ROOT.parent / "src" / "hooks" / "usePipelineRun.ts"
    )
    if not backend_path.exists():
        record("backend pipeline_runner.py", "fail",
               f"not found at {backend_path}", blocker=True)
        return
    if not frontend_hook.exists():
        record("frontend usePipelineRun.ts", "fail",
               f"not found at {frontend_hook}", blocker=True)
        return
    backend_src = backend_path.read_text(encoding="utf-8")
    frontend_src = frontend_hook.read_text(encoding="utf-8")
    # Extract emitted event names via emit("name", ...)
    emit_pat = re.compile(r'emit\(\s*"([a-z_]+)"')
    emits = sorted(set(emit_pat.findall(backend_src)))
    # Extract frontend case blocks.
    case_pat = re.compile(r'case\s+"([a-z_]+)"\s*:')
    cases = set(case_pat.findall(frontend_src))
    # Extract api.ts subscribed event list too.
    api_path = ROOT.parent / "src" / "services" / "api.ts"
    api_src = api_path.read_text(encoding="utf-8") if api_path.exists() else ""
    subscribed_pat = re.compile(r'"([a-z_]+)"')
    subscribed = set(subscribed_pat.findall(api_src))
    for event in emits:
        in_api = event in subscribed
        has_handler = event in cases
        if has_handler:
            record(event, "pass")
        elif in_api:
            # Subscribed but no handler — silently dropped. Not always fatal
            # (some events are intentionally just notifications), but worth
            # noting.
            record(event, "warn", "subscribed but no case handler")
        else:
            record(event, "warn", "not subscribed on frontend")


# -----------------------------------------------------------------------------
# 7. Schema round-trip
# -----------------------------------------------------------------------------


def check_schema_roundtrip():
    section("7. Schema round-trip — ScreenedCandidate + Report")
    try:
        from puzzleeval.schemas import (
            ScreenedCandidate, InteractionModel, UserSelectableParam,
        )
        from puzzleeval.report import assemble_report, report_to_dict
    except Exception as exc:
        record("import", "fail", str(exc), blocker=True)
        return
    try:
        sc = ScreenedCandidate(
            name="PreflightTester",
            provider="TestCo",
            description="Developer primitive that validates pipeline health",
            pricing_model="freemium",
            pricing_details="$0.01/call beyond free tier",
            claimed_capabilities=["preflight_validation"],
            relevance_score=0.95,
            adoption_difficulty="easy",
            relevant_subtasks=["Validate pipeline health"],
            source="https://example.test/preflight",
            verified_api_docs_url="https://example.test/docs",
            auth_method="api_key",
            api_access_method="free_signup",
            confirmed_capabilities=["preflight_validation"],
            rate_limit_info="100 req/min",
            data_format_notes="JSON body",
            screening_notes="deep_verify (preflight)",
            api_spec_path=None,
            covers_step_ids=["step_1"],
            coverage_confidence={"step_1": "verified"},
            pricing_breakdown=None,
            upstream_provider=None,
            sandbox_available=False,
            sandbox_docs_url=None,
            interaction_model=InteractionModel(synchronous=True),
            user_selectable_params=[],
        )
        _ = sc.model_dump()
        record("ScreenedCandidate build + dump", "pass",
               f"fields OK, {len(sc.model_fields)} fields total")
    except Exception as exc:
        record("ScreenedCandidate build + dump", "fail",
               f"{type(exc).__name__}: {exc}", blocker=True)
        return
    try:
        rep = assemble_report(
            run_id="preflight",
            trace_id="preflight-trace",
            agent1_result=None,
            agent2_result=None,
            agent4_result={"validated_candidates": [sc.model_dump()]},
            agent5_result=None,
            total_cost_usd=0.0,
        )
        d = report_to_dict(rep)
        _ = json.dumps(d, default=str)
        has_scope_runs = "scope_runs" in d
        record("assemble_report + report_to_dict",
               "pass" if has_scope_runs else "warn",
               f"scope_runs field present: {has_scope_runs}")
    except Exception as exc:
        record("assemble_report", "fail",
               f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-300:]}",
               blocker=True)


# -----------------------------------------------------------------------------
# 8. Disk writable + runs dir
# -----------------------------------------------------------------------------


def check_disk():
    section("8. Disk + runs directory")
    runs_root = ROOT.parent / "puzzleeval-api" / "runs"
    try:
        runs_root.mkdir(parents=True, exist_ok=True)
        probe = runs_root / ".preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("puzzleeval-api/runs writable", "pass", str(runs_root))
    except Exception as exc:
        record("puzzleeval-api/runs writable", "fail",
               f"cannot write: {exc}", blocker=True)


# -----------------------------------------------------------------------------
# 9. Plugin ports availability
# -----------------------------------------------------------------------------


def check_ports():
    section("9. Plugin port availability (best-effort)")
    default_ports = {
        "voice_realtime /audio": int(os.environ.get("PUZZLEEVAL_VOICE_PORT", "8768")),
        "webhook_receiver": int(os.environ.get("PUZZLEEVAL_WEBHOOK_PORT", "8765")),
        "outbound SMTP": int(os.environ.get("PUZZLEEVAL_SMTP_PORT", "2525")),
        "outbound Slack mock": int(os.environ.get("PUZZLEEVAL_SLACK_MOCK_PORT", "8766")),
        "outbound SMS mock": int(os.environ.get("PUZZLEEVAL_SMS_MOCK_PORT", "8767")),
    }
    for label, port in default_ports.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            sock.bind(("127.0.0.1", port))
            sock.close()
            record(f"{label} port {port}", "pass", "free")
        except OSError as exc:
            # Already bound — could be a prior run. Plugins auto-fallback to
            # random port when their preferred port is taken, so this is a
            # warning, not a blocker.
            record(f"{label} port {port}", "warn",
                   f"in use (plugin will fall back to random port): {exc}")
        finally:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


# -----------------------------------------------------------------------------
# 10. Backend FastAPI boot (imports only — don't actually start the server)
# -----------------------------------------------------------------------------


def check_backend_boot():
    section("10. Backend FastAPI import + lifespan")
    # Add puzzleeval-api to path.
    api_root = ROOT.parent / "puzzleeval-api"
    sys.path.insert(0, str(api_root))
    try:
        import main as backend_main  # noqa: F401
        record("puzzleeval-api main.py imports", "pass")
    except Exception as exc:
        record("puzzleeval-api main.py imports", "fail",
               f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-400:]}",
               blocker=True)
    finally:
        if str(api_root) in sys.path:
            sys.path.remove(str(api_root))


# -----------------------------------------------------------------------------
# 11. plugin_tool_runner smoke — beta header value + structure
# -----------------------------------------------------------------------------


def check_tool_runner():
    section("11. plugin_tool_runner invariants")
    try:
        from puzzleeval import plugin_tool_runner as ptr
    except Exception as exc:
        record("plugin_tool_runner import", "fail",
               f"{type(exc).__name__}: {exc}", blocker=True)
        return
    src = (ROOT / "puzzleeval" / "plugin_tool_runner.py").read_text(
        encoding="utf-8"
    )
    # Correct beta header literal?
    if '"code-execution-2025-08-25"' in src:
        record("beta header literal", "pass")
    else:
        record("beta header literal", "fail",
               "beta header is not 'code-execution-2025-08-25' — every "
               "tool_runner call will 400", blocker=True)
    # Eligibility semantics sane?
    try:
        plugins = ptr.eligible_plugins(
            input_type="voice_conversation", output_type="voice_conversation",
        )
        names = [p.name for p in plugins]
        if "voice_realtime" in names:
            record("voice_conversation eligibility", "pass",
                   f"matches: {names}")
        else:
            record("voice_conversation eligibility", "warn",
                   f"voice_realtime not in {names} — voice tests will fall "
                   "through to LLM judge")
    except Exception as exc:
        record("eligible_plugins(voice_conversation)", "fail",
               f"{type(exc).__name__}: {exc}", blocker=True)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main():
    skip_api = "--no-api" in sys.argv
    print(f"\n{BOLD}PuzzleEval Pre-Flight Check{RESET}")
    print(f"Working directory: {ROOT}")
    print(f"Skip real API call: {skip_api}\n")

    check_imports()
    check_config()
    check_plugins()
    check_credentials()
    check_anthropic_api(skip_api)
    check_sse_coverage()
    check_schema_roundtrip()
    check_disk()
    check_ports()
    check_backend_boot()
    check_tool_runner()

    # Summary.
    total = len(RESULTS)
    passes = sum(1 for r in RESULTS if r.status == "pass")
    warns = sum(1 for r in RESULTS if r.status == "warn")
    fails = sum(1 for r in RESULTS if r.status == "fail")
    blockers = sum(1 for r in RESULTS if r.blocker)

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}Summary:{RESET} {passes} pass / {warns} warn / {fails} fail")
    print(f"{BOLD}Blockers:{RESET} {blockers}")
    if blockers:
        print(f"\n{RED}{BOLD}BLOCKERS FOUND -- do not start a real run yet:{RESET}")
        for r in RESULTS:
            if r.blocker:
                print(f"  {RED}[XX]{RESET} {r.name}: {r.detail}")
        sys.exit(1)
    elif warns:
        print(f"\n{YELLOW}No blockers. {warns} warnings -- review then proceed.{RESET}")
        sys.exit(0)
    else:
        print(f"\n{GREEN}{BOLD}ALL CHECKS PASSED -- ready to run.{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
