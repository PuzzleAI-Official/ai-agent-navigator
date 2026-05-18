"""Docs URL verification and canonicalization helpers.

Agent 2 discovers candidates, Agent 4 verifies them, and Agent 5 builds
from the handoff. This module owns the deterministic source-routing layer
between those stages so provider-specific docs migrations do not leak into
builder prompts or completion gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class DocsMigrationRule:
    legacy_host: str
    legacy_path_prefix: str
    replacement_urls: tuple[str, ...]
    reason: str = "legacy_docs_migrated"


# Keep provider-specific migrations as transparent registry data, not as
# Agent 5 prompt hints or pass/fail logic. Each entry maps an old official
# docs surface to current official docs surfaces.
KNOWN_DOCS_MIGRATIONS: tuple[DocsMigrationRule, ...] = (
    DocsMigrationRule(
        legacy_host="platform.openai.com",
        legacy_path_prefix="/docs/guides/realtime",
        replacement_urls=(
            "https://developers.openai.com/api/docs/guides/realtime-websocket",
            "https://developers.openai.com/api/docs/guides/realtime-conversations",
        ),
    ),
)


def _normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _normalize_url(item)
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _matching_migration(url: str) -> DocsMigrationRule | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/").lower()
    for rule in KNOWN_DOCS_MIGRATIONS:
        if host == rule.legacy_host and path.startswith(rule.legacy_path_prefix):
            return rule
    return None


def resolve_docs_urls(
    discovered_urls: list[str],
    *,
    confirmed_source_urls: list[str] | None = None,
    prefetched_urls: list[str] | None = None,
) -> dict[str, object]:
    """Classify discovered docs URLs into canonical, unverified, and dead sets.

    ``confirmed_source_urls`` are URLs Agent 4 explicitly verified as official
    docs. ``prefetched_urls`` are URLs whose page content was actually saved
    to disk. A discovered URL becomes canonical only when it is either
    confirmed by those sources or mapped through a known migration rule.
    Otherwise it remains a discovered/best-effort URL that Agent 5 can inspect
    but should not treat as authoritative.
    """

    confirmed = set(_dedupe(confirmed_source_urls or []))
    prefetched = set(_dedupe(prefetched_urls or []))

    canonical: list[str] = []
    discovered: list[str] = []
    dead_or_blocked: list[dict[str, object]] = []
    resolution: list[dict[str, object]] = []

    for raw in _dedupe(discovered_urls):
        if not _is_http_url(raw):
            continue

        migration = _matching_migration(raw)
        if migration is not None:
            replacements = list(migration.replacement_urls)
            canonical.extend(replacements)
            dead_or_blocked.append({
                "url": raw,
                "reason": migration.reason,
                "replacement_urls": replacements,
            })
            resolution.append({
                "discovered_url": raw,
                "status": "deprecated_replaced",
                "canonical_urls": replacements,
                "reason": migration.reason,
            })
            continue

        if raw in prefetched:
            canonical.append(raw)
            resolution.append({
                "discovered_url": raw,
                "status": "fetched_current",
                "canonical_urls": [raw],
            })
            continue

        if raw in confirmed:
            canonical.append(raw)
            resolution.append({
                "discovered_url": raw,
                "status": "confirmed_source",
                "canonical_urls": [raw],
            })
            continue

        discovered.append(raw)
        resolution.append({
            "discovered_url": raw,
            "status": "discovered_unverified",
            "canonical_urls": [],
            "reason": "found upstream but not fetched or verified as official docs",
        })

    return {
        "canonical_docs_urls": _dedupe(canonical),
        "discovered_docs_urls": _dedupe(discovered),
        "dead_or_blocked_urls": dead_or_blocked,
        "docs_resolution": resolution,
    }


__all__ = [
    "DocsMigrationRule",
    "KNOWN_DOCS_MIGRATIONS",
    "resolve_docs_urls",
]
