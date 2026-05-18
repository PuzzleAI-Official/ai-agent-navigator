"""Agent 4 docs-entrypoint artifact.

Agent 4 verifies whether a candidate has a usable official documentation
entrypoint. Agent 5 starts from this small authorization artifact, then owns
research synthesis, implementation planning, build, debug, and evidence.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from puzzleeval.docs_resolver import resolve_docs_urls
from puzzleeval.web_doc_cache import EXCLUDED_DISCOVERY_FILENAME


DOCS_ENTRYPOINT_FILENAME = "docs_entrypoint.json"

_ALLOWED_AUTH_METHODS = {
    "api_key",
    "oauth",
    "bearer",
    "basic",
    "no_auth",
    "unknown",
}
_ALLOWED_ACCESS_METHODS = {
    "free_signup",
    "free_tier",
    "trial",
    "sandbox",
    "open",
    "paid_only",
    "unknown",
}


def _agent_state_dir(sandbox_dir: Path) -> Path:
    return sandbox_dir / "_agent_state"


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


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


def _prefetched_doc_entries(sandbox_dir: Path | None) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    if sandbox_dir is None or not sandbox_dir.exists():
        return entries
    for name in _prefetched_doc_files(sandbox_dir):
        path = sandbox_dir / name
        source_url = ""
        evidence_status = ""
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:12]:
                lowered = line.lower()
                if lowered.startswith("# fetched from:"):
                    source_url = line.split(":", 1)[1].strip()
                elif lowered.startswith("# evidence status:"):
                    evidence_status = line.split(":", 1)[1].strip()
        except OSError:
            continue
        if source_url:
            entries.append({
                "filename": name,
                "source_url": source_url,
                "evidence_status": evidence_status or "fetched_current_api_docs",
            })
    return entries


def _excluded_discovery_evidence(sandbox_dir: Path | None) -> list[dict[str, Any]]:
    if sandbox_dir is None:
        return []
    path = _agent_state_dir(sandbox_dir) / EXCLUDED_DISCOVERY_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)][:20]


def _normalize_auth_method(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if "bearer" in text:
        return "bearer"
    if "api_key" in text or "x_api_key" in text or "apikey" in text:
        return "api_key"
    if "oauth" in text:
        return "oauth"
    if "basic" in text:
        return "basic"
    if "no_auth" in text or "noauth" in text:
        return "no_auth"
    if text in {"oauth2", "oauth_2", "oauth_token"}:
        return "oauth"
    if text in {"bearer_token", "bearer_auth", "authorization_bearer"}:
        return "bearer"
    if text in {"basic_auth", "http_basic"}:
        return "basic"
    if text in {"api_token", "x_api_key", "api_key_header"}:
        return "api_key"
    if text in {"none", "open", "public", "noauth"}:
        return "no_auth"
    return text if text in _ALLOWED_AUTH_METHODS else "unknown"


def _normalize_access_method(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"free", "self_serve", "self_service"}:
        return "free_signup"
    if text in {"freemium", "free_plan"}:
        return "free_tier"
    if text in {"test_mode", "sandbox_available"}:
        return "sandbox"
    if text in {"paid", "paid_upfront", "enterprise_paid"}:
        return "paid_only"
    return text if text in _ALLOWED_ACCESS_METHODS else "unknown"


def _official_domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower()


def synthesize_docs_entrypoint(candidate: Any, sandbox_dir: Path | None = None) -> dict[str, Any]:
    """Return the Agent 4 docs-entrypoint artifact for a screened candidate."""

    evidence: list[dict[str, str]] = []
    verified_docs_url = str(getattr(candidate, "verified_api_docs_url", "") or "")
    prefetched_entries = _prefetched_doc_entries(sandbox_dir)
    prefetched_urls = _dedupe([entry["source_url"] for entry in prefetched_entries])
    docs_resolution = resolve_docs_urls(
        prefetched_urls,
        confirmed_source_urls=[],
        prefetched_urls=prefetched_urls,
    )
    discovery_resolution = resolve_docs_urls(
        [verified_docs_url] if verified_docs_url else [],
        confirmed_source_urls=[],
        prefetched_urls=[],
    )

    canonical_urls = list(docs_resolution["canonical_docs_urls"])
    discovered_urls = _dedupe([
        *list(docs_resolution["discovered_docs_urls"]),
        *list(discovery_resolution["discovered_docs_urls"]),
    ])
    primary = canonical_urls[0] if canonical_urls else ""
    docs_verdict = "verified_docs" if primary else "no_verified_docs"

    for entry in prefetched_entries:
        evidence.append({
            "source_url": entry["source_url"],
            "claim": f"fetched into {entry['filename']}",
            "evidence_type": "fetched_page",
        })

    pricing_summary = str(getattr(candidate, "pricing_details", "") or "").strip()
    if not pricing_summary:
        pricing_summary = str(getattr(candidate, "pricing_model", "") or "").strip()

    evidence_status = "fetched_current_api_docs" if primary and prefetched_entries else "missing_fetch_verified_docs"
    confidence = "high" if evidence_status == "fetched_current_api_docs" else "low"

    return {
        "schema_version": 1,
        "candidate": str(getattr(candidate, "name", "") or ""),
        "docs_verdict": docs_verdict,
        "evidence_status": evidence_status,
        "verification_method": "web_fetch" if evidence_status == "fetched_current_api_docs" else "none",
        "primary_docs_entrypoint": primary,
        "official_domain": _official_domain(primary) if primary else "",
        "alternate_entrypoints": _dedupe([*canonical_urls[1:], *discovered_urls]),
        "deprecated_or_blocked_urls": [
            *list(docs_resolution["dead_or_blocked_urls"]),
            *list(discovery_resolution["dead_or_blocked_urls"]),
        ],
        "evidence": evidence[:12],
        "prefetched_docs": prefetched_entries[:12],
        "excluded_discovery_evidence": _excluded_discovery_evidence(sandbox_dir),
        "capability_hints": list(getattr(candidate, "confirmed_capabilities", []) or [])[:12],
        "auth_method": _normalize_auth_method(
            str(getattr(candidate, "auth_method", "") or "")
        ),
        "api_access_method": _normalize_access_method(
            str(getattr(candidate, "api_access_method", "") or "")
        ),
        "pricing_summary": pricing_summary,
        "confidence": confidence,
        "generated_t_abs": time.time(),
        "source": "agent_4_docs_entrypoint",
    }


def write_docs_entrypoint(candidate: Any, sandbox_dir: Path) -> dict[str, Any]:
    """Write ``_agent_state/docs_entrypoint.json`` and return the payload."""

    payload = synthesize_docs_entrypoint(candidate, sandbox_dir=sandbox_dir)
    state_dir = _agent_state_dir(sandbox_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / DOCS_ENTRYPOINT_FILENAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return payload


def resolve_docs_entrypoint_for_candidate(
    candidate: Any,
    sandbox_dir: Path | None = None,
) -> dict[str, Any]:
    """Read an existing docs-entrypoint artifact or synthesize one.

    Agent 4 writes the artifact first in the normal pipeline. Direct Agent 5
    test/build paths may not have that file yet, so they synthesize the same
    shape from ScreenedCandidate fields and prefetched docs.
    """

    if sandbox_dir is not None:
        existing = read_docs_entrypoint(sandbox_dir)
        if existing:
            return existing
    return synthesize_docs_entrypoint(candidate, sandbox_dir=sandbox_dir)


def docs_entrypoint_allows_automatic_build(
    candidate: Any,
    sandbox_dir: Path | None = None,
) -> bool:
    payload = resolve_docs_entrypoint_for_candidate(candidate, sandbox_dir=sandbox_dir)
    return (
        payload.get("docs_verdict") == "verified_docs"
        and payload.get("evidence_status") == "fetched_current_api_docs"
        and bool(payload.get("primary_docs_entrypoint"))
    )


def read_docs_entrypoint(sandbox_dir: Path) -> dict[str, Any] | None:
    path = _agent_state_dir(sandbox_dir) / DOCS_ENTRYPOINT_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "DOCS_ENTRYPOINT_FILENAME",
    "docs_entrypoint_allows_automatic_build",
    "read_docs_entrypoint",
    "resolve_docs_entrypoint_for_candidate",
    "synthesize_docs_entrypoint",
    "write_docs_entrypoint",
]
