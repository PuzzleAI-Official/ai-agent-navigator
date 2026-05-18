"""Compact Agent 4 -> Agent 5 research handoff.

Agent 4 already verifies candidates and may persist fetched docs. This
module turns that work into a small JSON artifact Agent 5 can consume
before doing fresh web research. docs_entrypoint.json is the docs
authorization artifact; this file is a compact routing index and should not
be treated as build truth.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from puzzleeval.docs_resolver import resolve_docs_urls


RESEARCH_HANDOFF_FILENAME = "research_handoff.json"


def _agent_state_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / "_agent_state"


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = (item or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _infer_transport(*, endpoint: str, async_notes: str, content_notes: str, data_format: str) -> str:
    text = " ".join([endpoint, async_notes, content_notes, data_format]).lower()
    if "wss://" in text or "websocket" in text or "web socket" in text:
        return "websocket"
    if "grpc" in text:
        return "grpc"
    if "sdk" in text or "client library" in text:
        return "sdk"
    if "http" in text or "rest" in text or endpoint.startswith(("http://", "https://")):
        return "http"
    return "unknown"


def _prefetched_doc_files(sandbox_dir: Path | None) -> list[str]:
    if sandbox_dir is None or not sandbox_dir.exists():
        return []
    try:
        return sorted(
            p.name
            for p in sandbox_dir.iterdir()
            if p.is_file()
            and p.name.startswith("fetched_docs_")
            and p.name.endswith(".txt")
        )
    except OSError:
        return []


def _prefetched_doc_urls(sandbox_dir: Path | None) -> list[str]:
    urls: list[str] = []
    if sandbox_dir is None or not sandbox_dir.exists():
        return urls
    for name in _prefetched_doc_files(sandbox_dir):
        path = sandbox_dir / name
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:8]:
                if line.lower().startswith("# fetched from:"):
                    urls.append(line.split(":", 1)[1].strip())
                    break
        except OSError:
            continue
    return _dedupe(urls)


def synthesize_research_handoff(candidate: Any, sandbox_dir: Path | None = None) -> dict[str, Any]:
    """Return a compact research handoff dict for a screened candidate."""

    raw_discovered_urls: list[str] = [
        str(getattr(candidate, "verified_api_docs_url", "") or ""),
    ]
    confirmed_source_urls: list[str] = []
    endpoints: list[str] = []
    streaming_notes: list[str] = []
    quirks: list[str] = []
    unresolved: list[str] = []
    auth_method = str(getattr(candidate, "auth_method", "") or "unknown")
    endpoint_value = ""
    async_value = ""
    content_value = ""

    verified_docs_url = str(getattr(candidate, "verified_api_docs_url", "") or "")

    data_format = str(getattr(candidate, "data_format_notes", "") or "")
    if data_format:
        quirks.append(f"data_format: {data_format[:240]}")
    screening_notes = str(getattr(candidate, "screening_notes", "") or "")
    if screening_notes:
        quirks.append(f"screening_notes: {screening_notes[:240]}")

    docs_resolution = resolve_docs_urls(
        raw_discovered_urls,
        confirmed_source_urls=confirmed_source_urls,
        prefetched_urls=_prefetched_doc_urls(sandbox_dir),
    )

    return {
        "schema_version": 2,
        "candidate": str(getattr(candidate, "name", "") or ""),
        "generated_t_abs": time.time(),
        "canonical_docs_urls": docs_resolution["canonical_docs_urls"],
        "discovered_docs_urls": docs_resolution["discovered_docs_urls"],
        "docs_resolution": docs_resolution["docs_resolution"],
        "prefetched_doc_files": _prefetched_doc_files(sandbox_dir),
        "auth_method": auth_method or "unknown",
        "primary_endpoints": _dedupe(endpoints),
        "sdk_or_transport": _infer_transport(
            endpoint=endpoint_value,
            async_notes=async_value,
            content_notes=content_value,
            data_format=data_format,
        ),
        "streaming_or_session_notes": _dedupe(streaming_notes),
        "known_provider_quirks": _dedupe(quirks),
        "dead_or_blocked_urls": docs_resolution["dead_or_blocked_urls"],
        "unresolved_questions": _dedupe(unresolved),
        "source": "agent_4_research_handoff",
    }


def write_research_handoff(candidate: Any, sandbox_dir: Path) -> dict[str, Any]:
    """Write ``_agent_state/research_handoff.json`` and return the payload."""

    payload = synthesize_research_handoff(candidate, sandbox_dir=sandbox_dir)
    state_dir = _agent_state_dir(sandbox_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / RESEARCH_HANDOFF_FILENAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return payload


def read_research_handoff(sandbox_dir: Path) -> dict[str, Any] | None:
    path = _agent_state_dir(sandbox_dir) / RESEARCH_HANDOFF_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "RESEARCH_HANDOFF_FILENAME",
    "read_research_handoff",
    "synthesize_research_handoff",
    "write_research_handoff",
]
