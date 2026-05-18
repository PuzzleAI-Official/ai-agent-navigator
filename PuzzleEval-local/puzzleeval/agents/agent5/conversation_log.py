"""Conversation log persistence + summary analytics for Agent 5 builds.

Owns four related concerns, all about the build conversation's
on-disk artifacts and post-hoc analytics:

  1. ``compute_conversation_summary`` — pure analytics over a per-turn
     conversation log. Produces aggregate token/cost/cache stats,
     per-model breakdown, cache analysis, top costly turns, and
     build-phase breakdown.

  2. ``save_conversation_log`` — writes ``conversation_log.json`` and
     ``conversation_summary.json`` to the sandbox directory.

  3. ``categorize_failure`` — infers a failure category (auth_blocked,
     docs_unusable, etc.) from a builder failure message string.

  4. ``persist_large_output`` — saves large tool-result outputs to
     disk and returns a head+tail-truncated preview. Inspired by Claude
     Code's tool result persistence pattern.

Phase 3.3 of the architecture cleanup — extracted from
``puzzleeval/agents/implement_test_env.py``. Legacy names
(``_compute_conversation_summary``, ``_save_conversation_log``,
``_categorize_failure``, ``_persist_large_output``) are preserved as
one-line shims in the legacy module for back-compat with source-grep
tests + external callers.
"""

from __future__ import annotations

import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants for persist_large_output
# ---------------------------------------------------------------------------
OUTPUT_PERSIST_THRESHOLD = 5000  # Save tool outputs >5K chars to disk
MAX_PERSISTED_OUTPUT_CHARS = 30000  # Cap persisted output files


def persist_large_output(output: str, sandbox_dir: Path, turn: int) -> str:
    """Save large tool output to a file and return a smart-truncated version.

    Inspired by Claude Code's tool result persistence + EndTruncatingAccumulator.

    Strategy (Claude Code pattern):
    - Errors are ALWAYS at the tail (tracebacks, pip failures, test output)
    - Context/noise is in the middle (successful install lines, verbose logs)
    - Keep head (first 800 chars: command context) + tail (last 3000 chars: errors)
    - Drop the middle (noise)
    - Save full output to disk for read_file() access
    """
    if len(output) <= OUTPUT_PERSIST_THRESHOLD:
        return output

    # Save full output to file
    filename = f"output_turn{turn}.txt"
    filepath = sandbox_dir / filename
    try:
        filepath.write_text(output[:MAX_PERSISTED_OUTPUT_CHARS], encoding="utf-8")
    except OSError:
        pass

    # Smart truncation: head + tail (errors are at the end)
    head_size = 800
    tail_size = 3000
    preview_head = output[:head_size]
    preview_tail = (
        output[-tail_size:] if len(output) > head_size + tail_size else output[head_size:]
    )
    separator = (
        f"\n\n... ({len(output)} chars total — middle truncated, errors preserved below. "
        f"Full output saved to {filename}) ...\n\n"
    )

    return preview_head + separator + preview_tail


def categorize_failure(text: str) -> str:
    """Infer a failure category from the agent's failure message.

    Returns one of: docs_unusable, auth_blocked, api_incompatible,
    dependency_failure, build_timeout, unknown.
    """
    text_lower = text.lower()
    # Dead URL / unreachable resource (check BEFORE "not found" to avoid false match)
    if any(kw in text_lower for kw in [
        "couldn't download", "download file", "url unreachable",
        "url not found", "file from provided url", "file from url",
    ]):
        return "docs_unusable"
    if any(kw in text_lower for kw in ["docs", "documentation"]):
        return "docs_unusable"
    # Auth — only when explicitly about authentication, not just "400"
    if any(kw in text_lower for kw in [
        "401", "403", "unauthorized", "forbidden", "wrong x-auth",
        "invalid api key", "invalid key", "paid", "enterprise", "subscription",
    ]):
        return "auth_blocked"
    if any(kw in text_lower for kw in ["incompatible", "not support", "doesn't support"]):
        return "api_incompatible"
    if any(kw in text_lower for kw in ["install", "pip", "package", "dependency"]):
        return "dependency_failure"
    if any(kw in text_lower for kw in [
        "timeout", "budget", "turns", "credit", "balance",
        "billing", "quota", "exceeded", "rate limit",
    ]):
        return "build_timeout"
    return "unknown"


def compute_conversation_summary(
    conversation_log: list[dict],
    candidate_name: str,
) -> dict:
    """Build a compact summary block from a per-turn conversation log.

    Emitted as a separate ``conversation_summary.json`` artifact so we
    have grep-friendly per-candidate cache/cost observability without
    mutating the turn-by-turn ``conversation_log.json`` (which
    downstream readers parse as a plain list of turn dicts).

    Fields:
      * ``aggregate`` — totals across all turns: tokens by category,
        total cost, fresh / cache_read / cache_write percentages of
        total billed input, observed cache_hit_pct, latency stats.
      * ``per_model`` — per-model breakdown.
      * ``cache_analysis`` — behavioral signals: which turns wrote
        cache entries, which turns only read, and whether message-level
        caching appears to be active.
      * ``top_costly_turns`` — top 3 most expensive turns.
      * ``build_phases`` — turn count + cost + latency per phase
        (research/build/validate/post).
      * ``boundary_turns`` — when the active build gate accepted, when
        smoke passed, and when HARNESS_COMPLETE fired.

    All math is over the already-recorded per-turn fields so this helper
    has zero dependencies on the Anthropic response object or pricing
    table — can be regenerated post-hoc from any conversation_log.json file.
    """
    real_turns = [t for t in conversation_log if isinstance(t, dict)
                  and isinstance(t.get("turn"), int)]

    totals = {
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0.0,
    }
    per_model: dict[str, dict] = {}
    latencies_ms: list[float] = []

    for turn_dict in real_turns:
        for k in ("input_tokens", "cache_read_tokens",
                  "cache_create_tokens", "output_tokens"):
            totals[k] += int(turn_dict.get(k, 0) or 0)
        totals["cost_usd"] += float(turn_dict.get("cost_usd", 0.0) or 0.0)
        turn_latency = float(turn_dict.get("latency_ms", 0.0) or 0.0)
        totals["latency_ms"] += turn_latency
        if turn_latency > 0:
            latencies_ms.append(turn_latency)

        m = turn_dict.get("model") or "unknown"
        mbucket = per_model.setdefault(m, {
            "turns": 0, "input_tokens": 0, "cache_read_tokens": 0,
            "cache_create_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "latency_ms": 0.0,
        })
        mbucket["turns"] += 1
        for k in ("input_tokens", "cache_read_tokens",
                  "cache_create_tokens", "output_tokens"):
            mbucket[k] += int(turn_dict.get(k, 0) or 0)
        mbucket["cost_usd"] += float(turn_dict.get("cost_usd", 0.0) or 0.0)
        mbucket["latency_ms"] += turn_latency

    total_billed = (totals["input_tokens"] + totals["cache_read_tokens"]
                    + totals["cache_create_tokens"])

    def _pct(n: int | float) -> float:
        return round(100.0 * n / total_billed, 2) if total_billed > 0 else 0.0

    if latencies_ms:
        latency_min_ms = round(min(latencies_ms), 2)
        latency_max_ms = round(max(latencies_ms), 2)
        latency_avg_ms = round(sum(latencies_ms) / len(latencies_ms), 2)
    else:
        latency_min_ms = latency_max_ms = latency_avg_ms = 0.0

    aggregate = {
        **totals,
        "cost_usd": round(totals["cost_usd"], 4),
        "latency_ms": round(totals["latency_ms"], 2),
        "total_billed_input": total_billed,
        "fresh_input_pct": _pct(totals["input_tokens"]),
        "cache_read_pct": _pct(totals["cache_read_tokens"]),
        "cache_write_pct": _pct(totals["cache_create_tokens"]),
        "cache_hit_pct": _pct(totals["cache_read_tokens"]),
        "latency_min_ms": latency_min_ms,
        "latency_max_ms": latency_max_ms,
        "latency_avg_ms": latency_avg_ms,
        "turns_with_latency": len(latencies_ms),
    }

    turns_with_write = sum(
        1 for t in real_turns if int(t.get("cache_create_tokens", 0) or 0) > 0
    )
    turns_with_read_only = sum(
        1 for t in real_turns
        if int(t.get("cache_create_tokens", 0) or 0) == 0
        and int(t.get("cache_read_tokens", 0) or 0) > 0
    )

    # Heuristic: with ONLY system-prompt caching, cache_read per turn
    # is a constant ~system+tools size (~25K tokens). With message-
    # level caching active, at least SOME turns cache_read substantially
    # MORE than that baseline.
    SYSTEM_ONLY_BASELINE = 25000
    MESSAGE_CACHE_THRESHOLD = 40000  # 1.6x baseline; clear signal
    all_reads = [int(t.get("cache_read_tokens", 0) or 0) for t in real_turns]
    max_read = max(all_reads) if all_reads else 0
    message_cache_likely_active = max_read > MESSAGE_CACHE_THRESHOLD

    sorted_by_cost = sorted(
        real_turns,
        key=lambda t: float(t.get("cost_usd", 0.0) or 0.0),
        reverse=True,
    )[:3]
    top_costly_turns = [
        {
            "turn": t.get("turn"),
            "cost_usd": round(float(t.get("cost_usd", 0.0) or 0.0), 4),
            "input_tokens": int(t.get("input_tokens", 0) or 0),
            "cache_read_tokens": int(t.get("cache_read_tokens", 0) or 0),
            "cache_create_tokens": int(t.get("cache_create_tokens", 0) or 0),
            "output_tokens": int(t.get("output_tokens", 0) or 0),
            "stop_reason": t.get("stop_reason"),
        }
        for t in sorted_by_cost
    ]

    def _iteration_summary(turn_dict: dict) -> dict:
        existing = turn_dict.get("iteration_summary")
        if isinstance(existing, dict) and existing:
            return existing
        iterations = [
            it for it in (turn_dict.get("iterations") or [])
            if isinstance(it, dict)
        ]
        return {
            "count": len(iterations),
            "advisor_count": sum(1 for it in iterations if it.get("type") == "advisor_message"),
            "message_count": sum(1 for it in iterations if it.get("type") == "message"),
            "models": sorted({str(it.get("model") or "") for it in iterations if it.get("model")}),
            "input_tokens": sum(int(it.get("input_tokens", 0) or 0) for it in iterations),
            "output_tokens": sum(int(it.get("output_tokens", 0) or 0) for it in iterations),
            "cache_read_tokens": sum(int(it.get("cache_read", 0) or 0) for it in iterations),
            "cache_create_tokens": sum(int(it.get("cache_create", 0) or 0) for it in iterations),
        }
    sorted_by_latency = sorted(
        real_turns,
        key=lambda t: float(t.get("latency_ms", 0.0) or 0.0),
        reverse=True,
    )[:5]

    def _compact_tools(turn_dict: dict) -> list[str]:
        out: list[str] = []
        for tc in (turn_dict.get("tool_calls") or [])[:4]:
            if not isinstance(tc, dict):
                continue
            tool = str(tc.get("tool", "?"))
            inp = tc.get("input")
            detail = ""
            if isinstance(inp, dict):
                detail = str(
                    inp.get("filename")
                    or inp.get("path")
                    or inp.get("file_path")
                    or inp.get("command")
                    or inp.get("query")
                    or inp.get("url")
                    or ""
                )[:80]
            out.append(f"{tool}:{detail}" if detail else tool)
        return out

    def _server_result_counts(turn_dict: dict) -> dict:
        counts = {
            "web_fetch_results": 0,
            "web_fetch_empty": 0,
            "web_fetch_errors": 0,
            "web_fetch_nonempty": 0,
            "web_search_results": 0,
            "advisor_results": 0,
        }
        for tr in turn_dict.get("tool_results") or []:
            if not isinstance(tr, dict):
                continue
            tool = tr.get("tool")
            if tool == "web_fetch":
                counts["web_fetch_results"] += 1
                if tr.get("status") == "error" or tr.get("error_code"):
                    counts["web_fetch_errors"] += 1
                if int(tr.get("chars_returned", 0) or 0) > 0:
                    counts["web_fetch_nonempty"] += 1
                else:
                    counts["web_fetch_empty"] += 1
            elif tool == "web_search":
                counts["web_search_results"] += 1
            elif tool == "advisor":
                counts["advisor_results"] += 1
        return counts

    def _research_result_details(turn_dict: dict) -> dict:
        """Compact, URL/query-level research evidence for operator audit.

        The aggregate counts answer "how much research happened"; these
        bounded details answer "what did it try, and what did it get back?"
        This is intentionally small enough for ``conversation_summary.json``.
        Full raw blocks remain in ``conversation_log.json``.
        """
        fetches: list[dict] = []
        searches: list[dict] = []
        for tr in turn_dict.get("tool_results") or []:
            if not isinstance(tr, dict):
                continue
            tool = tr.get("tool")
            if tool == "web_fetch":
                url = str(tr.get("url") or tr.get("requested_url") or "unknown")
                chars = int(tr.get("chars_returned", 0) or 0)
                status = str(tr.get("status") or ("success" if chars > 0 else "empty"))
                fetches.append({
                    "url": url[:240],
                    "status": status,
                    "chars_returned": chars,
                    "error_code": str(tr.get("error_code") or "")[:80],
                })
            elif tool == "web_search":
                top_results = []
                for r in (tr.get("top_results") or [])[:3]:
                    if not isinstance(r, dict):
                        continue
                    top_results.append({
                        "title": str(r.get("title") or "")[:120],
                        "url": str(r.get("url") or "")[:200],
                    })
                searches.append({
                    "query": str(tr.get("query") or "")[:240],
                    "result_count": int(tr.get("result_count", 0) or 0),
                    "top_results": top_results,
                })
        return {"web_fetches": fetches, "web_searches": searches}

    top_slow_turns = [
        {
            "turn": t.get("turn"),
            "latency_ms": round(float(t.get("latency_ms", 0.0) or 0.0), 2),
            "cost_usd": round(float(t.get("cost_usd", 0.0) or 0.0), 4),
            "model": t.get("model"),
            "phase_hint": (
                "research"
                if any(
                    isinstance(tr, dict) and tr.get("tool") in ("web_fetch", "web_search")
                    for tr in (t.get("tool_results") or [])
                )
                else "build_or_verify"
            ),
            "tool_calls": _compact_tools(t),
            "server_result_counts": _server_result_counts(t),
            "research_details": _research_result_details(t),
            "iteration_summary": _iteration_summary(t),
        }
        for t in sorted_by_latency
    ]

    def _tool_path(entry: dict) -> str:
        inp = entry.get("input")
        if isinstance(inp, dict):
            return str(
                inp.get("filename")
                or inp.get("path")
                or inp.get("file_path")
                or inp.get("command")
                or inp.get("query")
                or inp.get("url")
                or ""
            )
        return ""

    def _result_persisted(entry: dict) -> bool:
        if entry.get("is_error"):
            return False
        if entry.get("persisted") is False:
            return False
        return True

    def _result_path(entry: dict) -> str:
        if not _result_persisted(entry):
            return ""
        return str(
            entry.get("wrote_path")
            or entry.get("patch_path")
            or entry.get("path")
            or ""
        )

    def _tool_elapsed_ms(entry: dict) -> float:
        try:
            return float(entry.get("tool_elapsed_ms", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _short_error(entry: dict) -> str:
        text = str(entry.get("result", "") or "")
        text = " ".join(text.strip().split())
        return text[-500:]

    def _turn_signals(turn_dict: dict) -> set[str]:
        calls = [tc for tc in (turn_dict.get("tool_calls") or []) if isinstance(tc, dict)]
        results = [tr for tr in (turn_dict.get("tool_results") or []) if isinstance(tr, dict)]
        tools = {str(tc.get("tool") or "") for tc in calls}
        result_tools = {str(tr.get("tool") or "") for tr in results}
        paths = [_tool_path(tc) for tc in calls]
        result_paths = [_result_path(tr) for tr in results]
        all_paths = [p for p in paths + result_paths if p]
        signals: set[str] = set()

        if any(tr.get("is_error") for tr in results):
            signals.add("tool_error")
        if any("timed out" in str(tr.get("result", "")).lower() for tr in results):
            signals.add("tool_timeout")
        if any("not recognized as an internal or external command" in str(tr.get("result", "")).lower() for tr in results):
            signals.add("platform_command_error")
        if any("STOP:" in str(tr.get("result", "")) for tr in results):
            signals.add("gate_repair")
        if any(p.startswith("_agent_state/") for p in all_paths):
            signals.add("autonomy_artifact")
        if all_paths and all(p.startswith("_agent_state/") for p in all_paths):
            signals.add("artifact_only")
        if any(p in {"harness.py", "requirements.txt", "smoke_test.py", "live_test.py"} for p in all_paths):
            signals.add("scaffold_changed")
        if any(
            Path(p).name.startswith(("tail_", "dump", "show_", "showevt", "show_evt", "inspect_", "probe_"))
            for p in all_paths
        ):
            signals.add("diagnostic_script")
        if "patch_file" in tools:
            signals.add("patch")
        if "write_file" in tools:
            signals.add("write")
        if "read_file" in tools and tools <= {"read_file"}:
            signals.add("read_only")
        if "run_code" in tools:
            signals.add("run_code")
        if "advisor" in tools or "advisor" in result_tools:
            signals.add("advisor")
        if any(tr.get("tool") in {"web_fetch", "web_search"} for tr in results):
            signals.add("server_research")
        if any(tr.get("tool") == "web_fetch" and int(tr.get("chars_returned", 0) or 0) <= 0 for tr in results):
            signals.add("empty_web_fetch")
        if any(tr.get("tool") == "web_fetch" and int(tr.get("chars_returned", 0) or 0) > 0 for tr in results):
            signals.add("nonempty_web_fetch")
        if "ask_research" in tools or "ask_research" in result_tools:
            signals.add("ask_research")
        if not calls and (turn_dict.get("text") or "").strip():
            signals.add("text_only")
        if any(bool(tr.get("deduped_read")) for tr in results):
            signals.add("deduped_read_stub")
        return signals

    def _efficiency_class(signals: set[str]) -> str:
        if (
            "tool_timeout" in signals
            or "platform_command_error" in signals
            or "deduped_read_stub" in signals
        ):
            return "waste"
        if "tool_error" in signals or "gate_repair" in signals:
            return "recovery"
        if "artifact_only" in signals or "text_only" in signals:
            return "overhead"
        if {"scaffold_changed", "run_code", "nonempty_web_fetch"} & signals:
            return "productive"
        if {"server_research", "advisor", "ask_research", "read_only"} & signals:
            return "support"
        return "other"

    turn_efficiency = []
    class_totals: dict[str, dict] = {}
    patch_by_file: dict[str, int] = {}
    read_counts: dict[str, int] = {}
    research_counts = {
        "turns": 0,
        "latency_ms": 0.0,
        "cost_usd": 0.0,
        "web_fetch_results": 0,
        "empty_web_fetch_results": 0,
        "web_fetch_errors": 0,
        "web_search_results": 0,
        "advisor_results": 0,
        "empty_web_fetch_by_url": {},
        "web_fetch_error_codes": {},
        "web_search_queries": [],
        "fetch_attempts": [],
    }
    waste_signals = {
        "artifact_only_turns": 0,
        "text_only_turns": 0,
        "tool_error_turns": 0,
        "tool_timeout_turns": 0,
        "platform_command_error_turns": 0,
        "gate_repair_turns": 0,
        "redundant_read_hints": 0,
        "diagnostic_script_turns": 0,
    }
    scaffold_files = {"requirements.txt", "harness.py", "smoke_test.py", "live_test.py"}
    scaffold_write_turns = 0
    single_file_scaffold_turns = 0
    total_tool_turns = 0
    parallel_tool_turns = 0
    diagnostic_script_writes: list[str] = []
    tool_timing_by_tool: dict[str, dict] = {}
    top_slow_tool_results: list[dict] = []
    total_custom_tool_elapsed_ms = 0.0
    known_tool_timing_results = 0
    patch_churn_by_file: dict[str, dict] = {}
    late_scaffold_changes: list[dict] = []
    large_patch_events: list[dict] = []
    build_gate_accepted_at = -1
    smoke_passed_at = -1
    harness_complete_at = -1

    for t in real_turns:
        tn = t.get("turn")
        if build_gate_accepted_at < 0:
            for tc in (t.get("tool_results") or []):
                if isinstance(tc, dict):
                    wrote = _result_path(tc)
                    if build_gate_accepted_at < 0 and wrote in (
                        "_agent_state/implementation_plan.json",
                    ):
                        build_gate_accepted_at = tn
                    if build_gate_accepted_at >= 0:
                        break
        if smoke_passed_at < 0:
            results_text = " ".join(
                str(tc.get("result", "")) for tc in (t.get("tool_results") or [])
                if isinstance(tc, dict)
            )
            if "SMOKE TEST PASSED" in results_text:
                smoke_passed_at = tn
        if harness_complete_at < 0:
            text = t.get("text", "") or ""
            if "HARNESS_COMPLETE" in text:
                harness_complete_at = tn

    for t in real_turns:
        signals = _turn_signals(t)
        cls = _efficiency_class(signals)
        cost = float(t.get("cost_usd", 0.0) or 0.0)
        latency = float(t.get("latency_ms", 0.0) or 0.0)
        bucket = class_totals.setdefault(cls, {
            "turns": 0,
            "cost_usd": 0.0,
            "latency_ms": 0.0,
        })
        bucket["turns"] += 1
        bucket["cost_usd"] += cost
        bucket["latency_ms"] += latency

        calls = [tc for tc in (t.get("tool_calls") or []) if isinstance(tc, dict)]
        results = [tr for tr in (t.get("tool_results") or []) if isinstance(tr, dict)]
        turn_custom_tool_elapsed_ms = 0.0
        if calls:
            total_tool_turns += 1
            if len(calls) > 1:
                parallel_tool_turns += 1
        scaffold_written_this_turn: set[str] = set()
        for tc in calls:
            path = _tool_path(tc)
            if tc.get("tool") == "patch_file":
                p = path or "unknown"
                patch_by_file[p] = patch_by_file.get(p, 0) + 1
            if tc.get("tool") == "read_file":
                p = path or "unknown"
                read_counts[p] = read_counts.get(p, 0) + 1
            if tc.get("tool") == "write_file" and Path(path).name in scaffold_files:
                scaffold_written_this_turn.add(Path(path).name)
            if tc.get("tool") == "write_file" and Path(path).name.startswith(
                ("tail_", "dump", "show_", "showevt", "show_evt", "inspect_", "probe_")
            ):
                diagnostic_script_writes.append(path or "unknown")
        for tr in results:
            elapsed_ms = _tool_elapsed_ms(tr)
            if elapsed_ms > 0:
                known_tool_timing_results += 1
                total_custom_tool_elapsed_ms += elapsed_ms
                turn_custom_tool_elapsed_ms += elapsed_ms
                tool = str(tr.get("tool") or "unknown")
                bucket = tool_timing_by_tool.setdefault(tool, {
                    "count": 0,
                    "elapsed_ms": 0.0,
                    "errors": 0,
                })
                bucket["count"] += 1
                bucket["elapsed_ms"] += elapsed_ms
                if tr.get("is_error"):
                    bucket["errors"] += 1
                top_slow_tool_results.append({
                    "turn": t.get("turn"),
                    "tool": tool,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "is_error": bool(tr.get("is_error")),
                    "path": _result_path(tr)[:160],
                    "summary": _short_error(tr)[:240],
                })
            if tr.get("tool") == "patch_file":
                path = _result_path(tr) or "unknown"
                diff = int(tr.get("patch_diff_chars", 0) or 0)
                abs_diff = abs(diff)
                churn = patch_churn_by_file.setdefault(path, {
                    "patches": 0,
                    "total_abs_diff_chars": 0,
                    "net_diff_chars": 0,
                    "max_abs_diff_chars": 0,
                    "first_turn": t.get("turn"),
                    "last_turn": t.get("turn"),
                    "late_patch_count": 0,
                })
                churn["patches"] += 1
                churn["total_abs_diff_chars"] += abs_diff
                churn["net_diff_chars"] += diff
                churn["max_abs_diff_chars"] = max(churn["max_abs_diff_chars"], abs_diff)
                churn["last_turn"] = t.get("turn")
                if smoke_passed_at >= 0 and int(t.get("turn", 0) or 0) > smoke_passed_at:
                    churn["late_patch_count"] += 1
                    if Path(path).name in scaffold_files and len(late_scaffold_changes) < 20:
                        late_scaffold_changes.append({
                            "turn": t.get("turn"),
                            "tool": "patch_file",
                            "path": path,
                            "patch_diff_chars": diff,
                        })
                if abs_diff >= 2000 and len(large_patch_events) < 20:
                    large_patch_events.append({
                        "turn": t.get("turn"),
                        "path": path,
                        "patch_diff_chars": diff,
                    })
            if tr.get("tool") == "write_file":
                path = _result_path(tr)
                if smoke_passed_at >= 0 and int(t.get("turn", 0) or 0) > smoke_passed_at:
                    if Path(path).name in scaffold_files and len(late_scaffold_changes) < 20:
                        late_scaffold_changes.append({
                            "turn": t.get("turn"),
                            "tool": "write_file",
                            "path": path,
                            "wrote_chars": int(tr.get("wrote_chars", 0) or 0),
                        })
        if scaffold_written_this_turn:
            scaffold_write_turns += 1
            if len(scaffold_written_this_turn) == 1:
                single_file_scaffold_turns += 1

        if "artifact_only" in signals:
            waste_signals["artifact_only_turns"] += 1
        if "text_only" in signals:
            waste_signals["text_only_turns"] += 1
        if "tool_error" in signals:
            waste_signals["tool_error_turns"] += 1
        if "tool_timeout" in signals:
            waste_signals["tool_timeout_turns"] += 1
        if "platform_command_error" in signals:
            waste_signals["platform_command_error_turns"] += 1
        if "gate_repair" in signals:
            waste_signals["gate_repair_turns"] += 1
        if "deduped_read_stub" in signals:
            waste_signals["redundant_read_hints"] += 1
        if "diagnostic_script" in signals:
            waste_signals["diagnostic_script_turns"] += 1

        result_counts = _server_result_counts(t)
        if "server_research" in signals or "advisor" in signals or "ask_research" in signals:
            research_counts["turns"] += 1
            research_counts["latency_ms"] += latency
            research_counts["cost_usd"] += cost
            research_counts["web_fetch_results"] += result_counts["web_fetch_results"]
            research_counts["empty_web_fetch_results"] += result_counts["web_fetch_empty"]
            research_counts["web_fetch_errors"] += result_counts["web_fetch_errors"]
            research_counts["web_search_results"] += result_counts["web_search_results"]
            research_counts["advisor_results"] += result_counts["advisor_results"]
            details = _research_result_details(t)
            for fetch in details["web_fetches"]:
                if fetch["chars_returned"] <= 0 or fetch["status"] == "error":
                    url = fetch["url"] or "unknown"
                    by_url = research_counts["empty_web_fetch_by_url"]
                    by_url[url] = by_url.get(url, 0) + 1
                if fetch["error_code"]:
                    errors = research_counts["web_fetch_error_codes"]
                    errors[fetch["error_code"]] = errors.get(fetch["error_code"], 0) + 1
                if len(research_counts["fetch_attempts"]) < 25:
                    research_counts["fetch_attempts"].append({
                        "turn": t.get("turn"),
                        **fetch,
                    })
            for search in details["web_searches"]:
                if search["query"] and len(research_counts["web_search_queries"]) < 25:
                    research_counts["web_search_queries"].append({
                        "turn": t.get("turn"),
                        "query": search["query"],
                        "result_count": search["result_count"],
                        "top_urls": [
                            r.get("url", "") for r in search.get("top_results", [])
                            if isinstance(r, dict) and r.get("url")
                        ],
                    })

        turn_efficiency.append({
            "turn": t.get("turn"),
            "class": cls,
            "signals": sorted(signals),
            "cost_usd": round(cost, 4),
            "latency_ms": round(latency, 2),
            "custom_tool_elapsed_ms": round(turn_custom_tool_elapsed_ms, 2),
            "tool_calls": _compact_tools(t),
            "iteration_summary": _iteration_summary(t),
        })

    for bucket in class_totals.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 4)
        bucket["latency_ms"] = round(bucket["latency_ms"], 2)
    research_counts["cost_usd"] = round(research_counts["cost_usd"], 4)
    research_counts["latency_ms"] = round(research_counts["latency_ms"], 2)

    repeated_reads = {
        path: count for path, count in sorted(read_counts.items())
        if count > 1
    }
    parallel_tool_call_rate = (
        round(parallel_tool_turns / total_tool_turns, 4)
        if total_tool_turns else 0.0
    )
    for bucket in tool_timing_by_tool.values():
        bucket["elapsed_ms"] = round(bucket["elapsed_ms"], 2)
        bucket["avg_elapsed_ms"] = (
            round(bucket["elapsed_ms"] / bucket["count"], 2)
            if bucket["count"] else 0.0
        )
    top_slow_tool_results = sorted(
        top_slow_tool_results,
        key=lambda row: float(row.get("elapsed_ms", 0.0) or 0.0),
        reverse=True,
    )[:10]
    patch_churn_by_file = {
        path: {
            **churn,
            "total_abs_diff_chars": int(churn["total_abs_diff_chars"]),
            "net_diff_chars": int(churn["net_diff_chars"]),
            "max_abs_diff_chars": int(churn["max_abs_diff_chars"]),
        }
        for path, churn in sorted(
            patch_churn_by_file.items(),
            key=lambda item: (-int(item[1].get("patches", 0)), item[0]),
        )
    }
    repeated_patch_files = {
        path: churn["patches"]
        for path, churn in patch_churn_by_file.items()
        if int(churn.get("patches", 0) or 0) > 1
    }
    tool_timing = {
        "model_api_latency_ms": round(totals["latency_ms"], 2),
        "custom_tool_elapsed_ms": round(total_custom_tool_elapsed_ms, 2),
        "observed_model_plus_tool_ms": round(totals["latency_ms"] + total_custom_tool_elapsed_ms, 2),
        "known_custom_tool_results": known_tool_timing_results,
        "by_tool": tool_timing_by_tool,
        "top_slow_tool_results": top_slow_tool_results,
    }
    churn_analysis = {
        "patch_by_file": patch_churn_by_file,
        "repeated_patch_files": repeated_patch_files,
        "late_scaffold_changes": late_scaffold_changes,
        "large_patch_events": large_patch_events,
    }

    def _repair_episodes() -> list[dict]:
        episodes: list[dict] = []
        for idx, t in enumerate(real_turns):
            results = [
                tr for tr in (t.get("tool_results") or [])
                if isinstance(tr, dict)
            ]
            if not any(tr.get("is_error") for tr in results):
                continue
            episode = {
                "failure_turn": t.get("turn"),
                "failure_tools": [
                    str(tr.get("tool") or "unknown")
                    for tr in results if tr.get("is_error")
                ][:5],
                "failure_summary": next(
                    (_short_error(tr) for tr in results if tr.get("is_error")),
                    "",
                ),
                "repair_actions": [],
                "next_validation": None,
                "resolved": None,
            }
            for nxt in real_turns[idx + 1: idx + 9]:
                nresults = [
                    tr for tr in (nxt.get("tool_results") or [])
                    if isinstance(tr, dict)
                ]
                for tr in nresults:
                    if tr.get("tool") in {"patch_file", "write_file", "ask_research", "summarize_forensics", "read_forensics"}:
                        if len(episode["repair_actions"]) < 8:
                            episode["repair_actions"].append({
                                "turn": nxt.get("turn"),
                                "tool": tr.get("tool"),
                                "path": _result_path(tr)[:160],
                                "patch_diff_chars": tr.get("patch_diff_chars"),
                                "is_error": bool(tr.get("is_error")),
                            })
                validation_results = [
                    tr for tr in nresults
                    if tr.get("tool") == "run_code"
                ]
                if validation_results:
                    failed = any(tr.get("is_error") for tr in validation_results)
                    episode["next_validation"] = {
                        "turn": nxt.get("turn"),
                        "failed": failed,
                        "commands": [
                            str(tr.get("command") or "")[:160]
                            for tr in validation_results
                        ],
                    }
                    episode["resolved"] = not failed
                    break
            episodes.append(episode)
            if len(episodes) >= 20:
                break
        return episodes

    efficiency_analysis = {
        "class_totals": class_totals,
        "waste_signals": waste_signals,
        "research": research_counts,
        "tool_timing": tool_timing,
        "repair_episodes": _repair_episodes(),
        "churn_analysis": churn_analysis,
        "patch_by_file": patch_by_file,
        "repeated_read_paths": repeated_reads,
        "scaffold_write_turns": scaffold_write_turns,
        "single_file_scaffold_turns": single_file_scaffold_turns,
        "scaffold_serialized_without_reason": single_file_scaffold_turns > 0,
        "parallel_tool_call_rate": parallel_tool_call_rate,
        "parallel_tool_turns": parallel_tool_turns,
        "total_tool_turns": total_tool_turns,
        "diagnostic_script_writes": diagnostic_script_writes[:20],
        "turns": turn_efficiency,
    }

    # Build-phase breakdown — derives research/build/validate buckets from
    # the active build gate, smoke pass, and HARNESS_COMPLETE boundaries.
    def _bucket_phase(tn: int) -> str:
        if build_gate_accepted_at >= 0 and tn < build_gate_accepted_at:
            return "research"
        if smoke_passed_at >= 0 and tn < smoke_passed_at:
            return "build"
        if harness_complete_at >= 0 and tn < harness_complete_at:
            return "validate"
        if harness_complete_at >= 0 and tn >= harness_complete_at:
            return "post"
        if smoke_passed_at >= 0:
            return "validate"
        if build_gate_accepted_at >= 0:
            return "build"
        return "research"

    phase_buckets: dict[str, dict] = {
        p: {"turns": 0, "cost_usd": 0.0, "latency_ms": 0.0, "first_turn": -1, "last_turn": -1}
        for p in ("research", "build", "validate", "post")
    }
    for t in real_turns:
        tn = int(t.get("turn", 0))
        phase = _bucket_phase(tn)
        b = phase_buckets[phase]
        b["turns"] += 1
        b["cost_usd"] += float(t.get("cost_usd", 0.0) or 0.0)
        b["latency_ms"] += float(t.get("latency_ms", 0.0) or 0.0)
        if b["first_turn"] < 0:
            b["first_turn"] = tn
        b["last_turn"] = tn
    build_phases = {
        p: {
            "turns": b["turns"],
            "cost_usd": round(b["cost_usd"], 4),
            "latency_ms": round(b["latency_ms"], 2),
            "first_turn": b["first_turn"],
            "last_turn": b["last_turn"],
        }
        for p, b in phase_buckets.items()
        if b["turns"] > 0
    }

    return {
        "candidate_name": candidate_name,
        "total_turns": len(real_turns),
        "aggregate": aggregate,
        "per_model": {
            m: {**bucket, "cost_usd": round(bucket["cost_usd"], 4)}
            for m, bucket in per_model.items()
        },
        "cache_analysis": {
            "turns_with_cache_write": turns_with_write,
            "turns_with_cache_read_only": turns_with_read_only,
            "message_cache_likely_active": message_cache_likely_active,
        },
        "top_costly_turns": top_costly_turns,
        "top_slow_turns": top_slow_turns,
        "efficiency_analysis": efficiency_analysis,
        "build_phases": build_phases,
        "boundary_turns": {
            "build_gate_accepted_at": build_gate_accepted_at,
            "smoke_passed_at": smoke_passed_at,
            "harness_complete_at": harness_complete_at,
        },
    }


def save_conversation_log(
    sandbox_dir: Path,
    conversation_log: list[dict],
    candidate_name: str,
) -> None:
    """Save conversation_log.json + conversation_summary.json to sandbox.

    Both writes are non-critical — failures are swallowed silently so
    the build doesn't crash on logging hiccups.
    """
    log_path = sandbox_dir / "conversation_log.json"
    try:
        log_path.write_text(
            json.dumps(conversation_log, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass

    try:
        summary = compute_conversation_summary(conversation_log, candidate_name)
        try:
            from puzzleeval.agents.agent5.failure_packets import summarize_failure_packets

            summary["failure_packets"] = summarize_failure_packets(sandbox_dir)
        except Exception:  # noqa: BLE001
            pass
        try:
            from puzzleeval.agents.agent5.research_memory import summarize_terminal_research_urls

            summary["terminal_research_urls"] = summarize_terminal_research_urls(sandbox_dir)
        except Exception:  # noqa: BLE001
            pass
        summary_path = sandbox_dir / "conversation_summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — logging must never fail the build
        pass


__all__ = [
    "OUTPUT_PERSIST_THRESHOLD",
    "MAX_PERSISTED_OUTPUT_CHARS",
    "categorize_failure",
    "compute_conversation_summary",
    "persist_large_output",
    "save_conversation_log",
]
