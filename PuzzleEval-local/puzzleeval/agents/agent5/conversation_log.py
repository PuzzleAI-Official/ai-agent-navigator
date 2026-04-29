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
      * ``boundary_turns`` — when api_spec was written, when smoke passed,
        when HARNESS_COMPLETE fired.

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

    # Build-phase breakdown — derives Phase 1/2/3 boundaries from turn data.
    api_spec_at = -1
    smoke_passed_at = -1
    harness_complete_at = -1

    for t in real_turns:
        tn = t.get("turn")
        if api_spec_at < 0:
            for tc in (t.get("tool_results") or []):
                if isinstance(tc, dict):
                    wrote = tc.get("wrote_path", "")
                    if wrote in ("api_spec.txt", "harness.py", "requirements.txt"):
                        api_spec_at = tn
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

    def _bucket_phase(tn: int) -> str:
        if api_spec_at >= 0 and tn < api_spec_at:
            return "research"
        if smoke_passed_at >= 0 and tn < smoke_passed_at:
            return "build"
        if harness_complete_at >= 0 and tn < harness_complete_at:
            return "validate"
        if harness_complete_at >= 0 and tn >= harness_complete_at:
            return "post"
        if smoke_passed_at >= 0:
            return "validate"
        if api_spec_at >= 0:
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
        "build_phases": build_phases,
        "boundary_turns": {
            "api_spec_written_at": api_spec_at,
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
