"""Research dead-end memory for Agent 5 builds."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


TERMINAL_RESEARCH_URLS_FILENAME = "research_terminal_urls.json"


def _agent_state_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / "_agent_state"


def _memory_path(sandbox_dir: Path) -> Path:
    return _agent_state_dir(sandbox_dir) / TERMINAL_RESEARCH_URLS_FILENAME


def _normalize_url(url: str) -> str:
    text = (url or "").strip()
    if text.endswith("/"):
        text = text[:-1]
    return text


def read_terminal_research_urls(sandbox_dir: Path) -> dict[str, Any]:
    path = _memory_path(sandbox_dir)
    if not path.exists():
        return {"schema_version": 1, "urls": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "urls": {}}
    if not isinstance(data, dict):
        return {"schema_version": 1, "urls": {}}
    data.setdefault("schema_version", 1)
    data.setdefault("urls", {})
    if not isinstance(data["urls"], dict):
        data["urls"] = {}
    return data


def mark_terminal_research_url(
    sandbox_dir: Path,
    *,
    url: str,
    reason: str,
    turn: int | None = None,
    status: str | None = None,
    error_code: str | None = None,
    source: str = "web_fetch",
) -> dict[str, Any] | None:
    """Mark a URL as terminal for this build and mirror into handoff."""

    norm = _normalize_url(url)
    if not norm:
        return None
    state_dir = _agent_state_dir(sandbox_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    memory = read_terminal_research_urls(sandbox_dir)
    existing = memory["urls"].get(norm, {})
    attempts = int(existing.get("attempts", 0) or 0) + 1
    entry = {
        "url": norm,
        "reason": reason,
        "status": status,
        "error_code": error_code,
        "source": source,
        "first_turn": existing.get("first_turn", turn),
        "last_turn": turn,
        "attempts": attempts,
        "last_seen_t_abs": time.time(),
    }
    memory["urls"][norm] = entry
    try:
        _memory_path(sandbox_dir).write_text(
            json.dumps(memory, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        return entry

    # Keep the compact research handoff aligned so the agent sees the
    # terminal URL in the same artifact it already reads for research.
    handoff_path = state_dir / "research_handoff.json"
    if handoff_path.exists():
        try:
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            blocked = [
                item for item in (handoff.get("dead_or_blocked_urls") or [])
                if isinstance(item, dict) and _normalize_url(str(item.get("url") or "")) != norm
            ]
            blocked.append({
                "url": norm,
                "reason": reason,
                "status": status,
                "error_code": error_code,
                "turn": turn,
            })
            handoff["dead_or_blocked_urls"] = blocked[-25:]
            handoff_path.write_text(
                json.dumps(handoff, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return entry


def terminal_reason_for_fetch_result(result: dict[str, Any]) -> str | None:
    """Return a terminal reason for a web_fetch result, if any."""

    if not isinstance(result, dict) or result.get("tool") != "web_fetch":
        return None
    status = str(result.get("status") or "").lower()
    code = str(result.get("error_code") or "").lower()
    message = str(result.get("error_message") or "").lower()
    chars = int(result.get("chars_returned") or 0)
    if status == "error":
        if "url_not_allowed" in code or "url_not_allowed" in message:
            return "url_not_allowed"
        if "403" in code or "403" in message or "forbidden" in message or "cloudflare" in message:
            return "403_or_waf"
        if "auth" in message or "login" in message:
            return "auth_wall"
        return code or "fetch_error"
    if status == "empty" or chars == 0:
        return "empty_result"
    preview = str(result.get("preview") or "").lower()
    if "<script" in preview and chars < 1500:
        return "empty_spa_shell"
    return None


def summarize_terminal_research_urls(sandbox_dir: Path) -> dict[str, Any]:
    memory = read_terminal_research_urls(sandbox_dir)
    urls = memory.get("urls", {})
    return {
        "count": len(urls),
        "urls": list(urls.values())[-10:],
    }


__all__ = [
    "TERMINAL_RESEARCH_URLS_FILENAME",
    "mark_terminal_research_url",
    "read_terminal_research_urls",
    "summarize_terminal_research_urls",
    "terminal_reason_for_fetch_result",
]
