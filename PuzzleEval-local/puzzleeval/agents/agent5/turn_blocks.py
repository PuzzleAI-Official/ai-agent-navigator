"""Per-turn response block handlers for the Agent 5 build loop.

Phase 4 of the architecture cleanup — extracts inline block-handling
logic from ``_build_single_harness`` into focused helpers. Each helper
is a PURE function (or near-pure: only logging side effects allowed).
The build loop's control flow stays in the outer function; these
helpers operate on the response content and return the result the
caller acts on.

Module ownership:
  * Detect + strip orphan server_tool_use blocks (``strip_orphan_server_tool_uses``)
  * Future expansion (Phase 4 follow-ups):
    - Per-block-type handlers (text / thinking / tool_use / tool_result / ...)
    - Block dispatcher (free function over BLOCK_HANDLERS registry)

Today this module starts narrow and grows as we extract more helpers.
The goal is NOT to abstract everything at once — it's to make focused,
behavior-preserving moves with clear boundaries.
"""

from __future__ import annotations

from typing import Any


# Server tool names whose `server_tool_use` blocks need pairing with a
# matching `..._tool_result` block in the SAME response. Anthropic 400s
# the next API call if a server_tool_use lacks its result and we append
# it to the conversation history.
SERVER_TOOL_NAMES: frozenset[str] = frozenset({"web_search", "web_fetch", "advisor"})


def detect_orphan_server_tool_uses(response: Any) -> set[str]:
    """Find server_tool_use blocks that lack a matching _tool_result.

    Pure function. Walks ``response.content``, collects every
    server_tool_use id, every ``*_tool_result`` block's tool_use_id,
    and returns the set difference (server_tool_use ids that don't
    have a corresponding result).

    Why this matters: when ``stop_reason=max_tokens`` truncates a
    response mid-tool, the server_tool_use block lands without its
    result. Appending such content to the conversation history causes
    the next API call to 400 with "tool_use was found without a
    corresponding tool_result". Detecting orphans before append lets
    us strip them.

    Implementation note on "endswith _tool_result" matching: Anthropic
    uses per-tool-family type literals (web_search_tool_result,
    web_fetch_tool_result, advisor_tool_result). An earlier explicit
    allowlist missed advisor_tool_result and wrongly classified live
    advisor pairs as orphans (real bug from voice_dual_3 trace). Using
    a suffix match avoids that whole class of bugs.
    """
    server_use_ids: list[str] = []
    server_result_ids: set[str] = set()
    for blk in response.content:
        btype = getattr(blk, "type", "")
        if btype == "server_tool_use" and getattr(blk, "name", "") in SERVER_TOOL_NAMES:
            bid = getattr(blk, "id", "")
            if bid:
                server_use_ids.append(bid)
        elif btype.endswith("_tool_result") and btype != "tool_result":
            rid = getattr(blk, "tool_use_id", "")
            if rid:
                server_result_ids.add(rid)
    return {u for u in server_use_ids if u not in server_result_ids}


def strip_orphan_server_tool_uses(response: Any) -> tuple[list[Any], frozenset[str], bool]:
    """Detect orphans + strip them from response.content. Mutates in-place when possible.

    Returns ``(cleaned_content, orphan_ids, mutation_succeeded)``:
      * ``cleaned_content``: the content list with orphans removed.
        Falls back to a minimal text block if stripping leaves nothing.
      * ``orphan_ids``: frozenset of stripped server_tool_use ids
        (empty when there were no orphans).
      * ``mutation_succeeded``: True if ``response.content`` was
        successfully replaced in-place. False means the response
        object was frozen (rare but possible) — caller decides whether
        to fall back to local-variable usage.

    Pure-ish: side effects limited to a best-effort
    ``object.__setattr__`` on ``response.content``. Logging is the
    caller's responsibility (caller has the logger + context).
    """
    orphan_ids = detect_orphan_server_tool_uses(response)
    if not orphan_ids:
        return (list(response.content), frozenset(), True)

    cleaned: list[Any] = [
        blk for blk in response.content
        if not (
            getattr(blk, "type", "") == "server_tool_use"
            and getattr(blk, "id", "") in orphan_ids
        )
    ]
    if not cleaned:
        cleaned = [{
            "type": "text",
            "text": "(server-side research truncated; continuing)",
        }]

    # Best-effort in-place mutation. Anthropic SDK response objects are
    # frozen pydantic models, but object.__setattr__ usually works on
    # the underlying __dict__.
    try:
        object.__setattr__(response, "content", cleaned)
        return (cleaned, frozenset(orphan_ids), True)
    except (AttributeError, TypeError):
        return (cleaned, frozenset(orphan_ids), False)


# ---------------------------------------------------------------------------
# Turn telemetry — construct the initial turn_log dict for one API turn.
# ---------------------------------------------------------------------------

def build_initial_turn_log(
    response: Any,
    *,
    turn: int,
    current_model: str,
    call_cost: float,
    call_latency_ms: float,
) -> dict:
    """Build the initial turn_log dict from a response's usage stats.

    Pure function — no side effects, no dependencies on the build loop's
    state. Returns a fresh dict that the caller then mutates by iterating
    ``response.content`` (text/tool_use/tool_result blocks add fields).

    Fields populated:
      * Turn-identification: ``turn``, ``stop_reason``, ``model``
      * Cost + tokens: ``cost_usd``, ``input_tokens``, ``output_tokens``,
        ``cache_read_tokens``, ``cache_create_tokens``, ``cache_hit_pct``
      * Latency: ``call_latency_ms`` (per-turn API wall-clock)
      * Per-iteration breakdown: ``iterations`` (advisor / executor split)
      * Empty containers for the caller to fill: ``text``, ``tool_calls``,
        ``tool_results``

    Phase 4 Path B extraction — the inline turn_log construction in
    ``_build_single_harness`` was identical to this function. Moving it
    here gives the dict-construction a clear name + isolated tests +
    a stable contract that downstream readers (conversation_summary,
    SSE telemetry) can rely on.
    """
    iterations_log = []
    for iteration in (getattr(response.usage, "iterations", None) or []):
        iterations_log.append({
            "type": getattr(iteration, "type", "message"),
            "model": getattr(iteration, "model", current_model),
            "input_tokens": getattr(iteration, "input_tokens", 0),
            "output_tokens": getattr(iteration, "output_tokens", 0),
            "cache_read": getattr(iteration, "cache_read_input_tokens", 0),
            "cache_create": getattr(iteration, "cache_creation_input_tokens", 0),
        })

    try:
        cache_read = int(getattr(response.usage, "cache_read_input_tokens", 0) or 0)
        cache_create = int(getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
        total_input = int(response.usage.input_tokens) + cache_read + cache_create
        cache_hit_pct = round(cache_read / total_input * 100, 1) if total_input > 0 else 0.0
    except (TypeError, ValueError):
        cache_read = 0
        cache_create = 0
        cache_hit_pct = 0.0

    return {
        "turn": turn,
        "stop_reason": response.stop_reason,
        "cost_usd": round(call_cost, 4),
        "model": current_model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_tokens": cache_read,
        "cache_create_tokens": cache_create,
        "cache_hit_pct": cache_hit_pct,
        "latency_ms": call_latency_ms,
        "text": "",
        "tool_calls": [],
        "tool_results": [],
        "iterations": iterations_log,
    }


# ---------------------------------------------------------------------------
# Per-turn progress callback — emit a build_turn SSE event with rich detail.
# ---------------------------------------------------------------------------

def _summarize_tool_call(tool_call: dict) -> dict:
    """Build a one-line summary of a single tool_call entry from turn_log.

    Returns ``{"tool": <name>, "summary": <truncated string>}``. Truncation
    keeps each summary under ~200 chars so the SSE payload stays bounded.
    """
    tool_name = tool_call.get("tool", "?")
    tc_input = tool_call.get("input")
    summary = ""
    if isinstance(tc_input, dict):
        if tool_name == "web_fetch":
            summary = tc_input.get("url", "")[:200]
        elif tool_name == "web_search":
            summary = tc_input.get("query", "")[:200]
        elif tool_name in ("write_file", "patch_file", "read_file"):
            summary = str(tc_input.get("path", tc_input.get("file_path", "")))[:200]
        elif tool_name == "run_code":
            cmd = tc_input.get("command") or tc_input.get("code") or ""
            summary = str(cmd)[:200]
        elif tool_name == "ask_research":
            summary = str(tc_input.get("question", ""))[:200]
        else:
            summary = ", ".join(
                f"{k}={str(v)[:60]}" for k, v in list(tc_input.items())[:2]
            )
    elif isinstance(tc_input, str):
        summary = tc_input[:200]
    return {"tool": tool_name, "summary": summary}


def emit_build_turn_progress(
    progress_callback,
    *,
    candidate_name: str,
    turn: int,
    max_turns: int,
    api_spec_written: bool,
    smoke_ever_passed: bool,
    current_model: str,
    call_cost: float,
    accumulated_cost: float,
    call_latency_ms: float,
    cache_read: int,
    cache_create: int,
    response: Any,
    turn_log: dict,
) -> None:
    """Emit a 'build_turn' SSE-style event with rich per-turn detail.

    Computes the build phase ("researching"/"building"/"validating")
    from boundary-state flags (api_spec_written, smoke_ever_passed) and
    summarizes tool calls (URLs, queries, filenames). This is the
    operator-facing diagnostic surface — what's the build doing right now,
    in plain language, with enough context to spot stalls.

    Phase 4 Path B extraction. No-op when ``progress_callback`` is None.
    """
    if progress_callback is None:
        return

    phase = "researching" if not api_spec_written else "building"
    if smoke_ever_passed:
        phase = "validating"

    tool_calls_detail = [
        _summarize_tool_call(tc) for tc in (turn_log.get("tool_calls") or [])
    ]

    text_preview = (turn_log.get("text") or "").strip()[:300]

    progress_callback("build_turn", {
        "candidate_name": candidate_name,
        "turn": turn + 1,
        "max_turns": max_turns,
        "phase": phase,
        "model": current_model,
        "cost_usd": round(call_cost, 4),
        "cumulative_cost_usd": round(accumulated_cost, 4),
        "latency_ms": call_latency_ms,
        "cache_read_tokens": cache_read,
        "cache_create_tokens": cache_create,
        "tools_used": [b.name for b in response.content if b.type == "tool_use"],
        "tool_calls_detail": tool_calls_detail,
        "text_preview": text_preview,
        "stop_reason": response.stop_reason,
    })


__all__ = [
    "SERVER_TOOL_NAMES",
    "build_initial_turn_log",
    "detect_orphan_server_tool_uses",
    "emit_build_turn_progress",
    "strip_orphan_server_tool_uses",
]
