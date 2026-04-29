"""Cross-run memory directory.

Modeled on Claude Code's `src/memdir/` pattern: persistent files written by
agents during one run that future runs can recall semantically. Without
this, every Phase 6.5 deep-verify of a provider re-researches the same
docs from scratch — even when yesterday's run already fetched them.

Two concrete use-cases this enables today:

  1. **API spec memos.** Phase 6.5 saves `api_spec.txt` per candidate. Today
     these live under `runs/{trace_id}/harnesses/{slug}/`. By also writing
     them to a stable cross-run location keyed by candidate name + docs URL,
     future runs of the same candidate can SKIP the deep-verify research
     turns entirely (they still verify the cached spec is fresh).

  2. **API quirk notes.** When Agent 5 discovers that "Provider X's response
     uses snake_case keys, not camelCase" or "Provider Y requires multipart
     even though docs imply JSON", that's reusable knowledge. Save it to
     memdir; recall it on next run.

This module is the storage + recall layer. It does NOT decide WHEN to read
or write — agents call ``write_memory()`` and ``recall_memory()`` at their
own discretion.

Storage layout:

  ~/.puzzleeval/memdir/
    api_specs/
      <provider_slug>__<docs_url_hash>.md       # frontmatter + body
    quirks/
      <provider_slug>.md
    INDEX.json                                  # name → list of memory files

Frontmatter fields:
  name        — display name ("Mindee Receipt API spec")
  description — one-line for relevance ranking
  tags        — list of free-text tags
  written_at  — ISO timestamp
  written_by  — agent that produced it
  source_run  — trace_id of the run that wrote this memory
  schema_version — bumped if the memory format changes
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage location — overridable for tests
# ---------------------------------------------------------------------------

_DEFAULT_MEMDIR = Path.home() / ".puzzleeval" / "memdir"


def get_memdir_root() -> Path:
    """Return the active memdir root, honoring the env override.

    Tests set ``PUZZLEEVAL_MEMDIR`` to a tmp path so they don't clobber
    the user's real memory dir.
    """
    raw = os.environ.get("PUZZLEEVAL_MEMDIR")
    if raw:
        return Path(raw)
    return _DEFAULT_MEMDIR


# Whole module is gated. When disabled (rare; off-by-default for tests),
# write_memory and recall_memory become no-ops.
def memory_enabled() -> bool:
    val = os.environ.get("PUZZLEEVAL_MEMORY_ENABLED", "1").lower()
    return val not in {"0", "false", "no"}


# ---------------------------------------------------------------------------
# Slug / hash helpers — keep file names short and deterministic
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(name: str, max_len: int = 40) -> str:
    """Lowercase, strip non-alnum, truncate. Matches the convention Agent 5
    uses for its sandbox dir slugs so cross-references work without a lookup."""
    s = _NON_ALNUM.sub("_", name.strip().lower()).strip("_")
    return s[:max_len] or "unnamed"


def short_hash(value: str, length: int = 10) -> str:
    """Stable short SHA-256 prefix used to disambiguate memos for the same
    candidate at different docs URLs (a vendor with v1 + v2 docs gets two
    distinct memos rather than one overwriting the other)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


# ---------------------------------------------------------------------------
# Memory record dataclass
# ---------------------------------------------------------------------------


@dataclass
class Memory:
    name: str
    description: str
    body: str
    tags: list[str] = field(default_factory=list)
    written_at: str = ""  # ISO 8601, set by write_memory if blank
    written_by: str = ""
    source_run: str = ""
    schema_version: int = 1
    relpath: str = ""  # filled when loaded from disk

    def to_markdown(self) -> str:
        # Minimal frontmatter block — keep it parseable by both this module
        # and any future tooling.
        lines = [
            "---",
            f"name: {_yaml_safe(self.name)}",
            f"description: {_yaml_safe(self.description)}",
            f"tags: {json.dumps(self.tags, ensure_ascii=False)}",
            f"written_at: {self.written_at}",
            f"written_by: {_yaml_safe(self.written_by)}",
            f"source_run: {_yaml_safe(self.source_run)}",
            f"schema_version: {self.schema_version}",
            "---",
            "",
            self.body.rstrip(),
            "",
        ]
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, text: str, relpath: str = "") -> "Memory | None":
        if not text.startswith("---"):
            return None
        try:
            _, frontmatter, body = text.split("---", 2)
        except ValueError:
            return None
        meta: dict[str, Any] = {}
        for line in frontmatter.strip().splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if key == "tags":
                try:
                    meta[key] = json.loads(val)
                except json.JSONDecodeError:
                    meta[key] = []
            elif key == "schema_version":
                try:
                    meta[key] = int(val)
                except ValueError:
                    meta[key] = 1
            else:
                meta[key] = _strip_yaml_quotes(val)
        return cls(
            name=meta.get("name", ""),
            description=meta.get("description", ""),
            body=body.strip(),
            tags=meta.get("tags") or [],
            written_at=meta.get("written_at", ""),
            written_by=meta.get("written_by", ""),
            source_run=meta.get("source_run", ""),
            schema_version=meta.get("schema_version", 1),
            relpath=relpath,
        )


def _yaml_safe(value: str) -> str:
    """Quote a YAML scalar when it contains characters that would change parsing."""
    if value is None:
        return ""
    s = str(value)
    if any(ch in s for ch in (":", "#", "\"", "'", "\n", ",", "[", "]", "{", "}")):
        return json.dumps(s, ensure_ascii=False)
    return s


def _strip_yaml_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_LOCK = threading.Lock()


def write_memory(
    category: str,
    key: str,
    name: str,
    description: str,
    body: str,
    tags: list[str] | None = None,
    written_by: str = "",
    source_run: str = "",
) -> Path | None:
    """Write a memory under ``<memdir_root>/<category>/<slug>__<hash>.md``.

    ``key`` is whatever uniquely identifies this memo — typically
    ``"<provider_slug>::<docs_url>"``. Returns the path written (None if
    memory is disabled).
    """
    if not memory_enabled():
        return None
    try:
        cat_dir = get_memdir_root() / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        provider, _, locator = key.partition("::")
        fname = f"{slugify(provider)}__{short_hash(locator or key)}.md"
        path = cat_dir / fname
        memory = Memory(
            name=name,
            description=description,
            body=body,
            tags=tags or [],
            written_at=datetime.now(timezone.utc).isoformat(),
            written_by=written_by,
            source_run=source_run,
        )
        with _LOCK:
            path.write_text(memory.to_markdown(), encoding="utf-8")
            _refresh_index(get_memdir_root())
        logger.info(
            "memory_written",
            extra={"category": category, "key": key, "path": str(path)},
        )
        return path
    except Exception as exc:  # never raise into the agent loop
        logger.warning("memory_write_failed: %s", exc)
        return None


def recall_memory(
    category: str, key: str, max_age_days: int | None = None
) -> Memory | None:
    """Recall the most recent memo for ``category`` + ``key``.

    Returns None when not found, when memory is disabled, or when the only
    memo on disk is older than ``max_age_days`` (None = never expire).
    """
    if not memory_enabled():
        return None
    cat_dir = get_memdir_root() / category
    if not cat_dir.exists():
        return None
    provider, _, locator = key.partition("::")
    fname = f"{slugify(provider)}__{short_hash(locator or key)}.md"
    path = cat_dir / fname
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("memory_read_failed: %s", exc)
        return None
    memo = Memory.from_markdown(text, relpath=str(path.relative_to(get_memdir_root())))
    if memo is None:
        return None
    if max_age_days is not None and memo.written_at:
        try:
            written = datetime.fromisoformat(memo.written_at)
            age_days = (datetime.now(timezone.utc) - written).total_seconds() / 86400.0
            if age_days > max_age_days:
                return None
        except ValueError:
            pass
    return memo


def find_relevant_memories(
    query: str, categories: list[str] | None = None, limit: int = 5
) -> list[Memory]:
    """Find memories whose name / description / tags overlap with the query.

    Pure word-overlap scorer. The Claude Code equivalent uses a small LLM
    to score relevance; we keep it deterministic and zero-cost. The returned
    list is ordered by score desc, then by recency desc.
    """
    if not memory_enabled():
        return []
    root = get_memdir_root()
    if not root.exists():
        return []

    query_words = set(_tokenize(query.lower()))
    if not query_words:
        return []

    scope: list[Path] = []
    cats = categories or [d.name for d in root.iterdir() if d.is_dir()]
    for cat in cats:
        cat_dir = root / cat
        if cat_dir.is_dir():
            scope.extend(p for p in cat_dir.iterdir() if p.suffix == ".md")

    scored: list[tuple[float, str, Memory]] = []
    for path in scope:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        memo = Memory.from_markdown(text, relpath=str(path.relative_to(root)))
        if memo is None:
            continue
        haystack = " ".join([memo.name, memo.description, " ".join(memo.tags)]).lower()
        haystack_words = set(_tokenize(haystack))
        overlap = len(query_words & haystack_words)
        if overlap == 0:
            continue
        score = overlap / max(len(query_words), 1)
        scored.append((score, memo.written_at, memo))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [memo for _, _, memo in scored[:limit]]


def _tokenize(text: str) -> list[str]:
    """Simple word splitter — alphanumeric runs of ≥3 chars."""
    return [w for w in re.findall(r"[a-z0-9]+", text) if len(w) >= 3]


# ---------------------------------------------------------------------------
# Index — for fast cross-run discovery without scanning every file
# ---------------------------------------------------------------------------


def _refresh_index(root: Path) -> None:
    """Rebuild ``INDEX.json`` from disk. Called after every write_memory.

    Cheap (we have at most thousands of memos) and keeps recall_memory's
    O(1) lookup honest even after concurrent writes from parallel runs.
    """
    try:
        index: dict[str, list[str]] = {}
        for cat_dir in root.iterdir():
            if not cat_dir.is_dir():
                continue
            cat = cat_dir.name
            entries: list[str] = []
            for memo_path in cat_dir.iterdir():
                if memo_path.suffix == ".md":
                    entries.append(memo_path.name)
            entries.sort()
            index[cat] = entries
        index_path = root / "INDEX.json"
        index_path.write_text(
            json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "categories": index}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("memory_index_refresh_failed: %s", exc)


def stats() -> dict[str, Any]:
    """Observability: counts per category. Surfaced in pipeline_summary.json."""
    root = get_memdir_root()
    if not root.exists():
        return {"enabled": memory_enabled(), "root": str(root), "categories": {}}
    out: dict[str, int] = {}
    for cat_dir in root.iterdir():
        if cat_dir.is_dir():
            out[cat_dir.name] = sum(1 for p in cat_dir.iterdir() if p.suffix == ".md")
    return {"enabled": memory_enabled(), "root": str(root), "categories": out}


__all__ = [
    "Memory",
    "find_relevant_memories",
    "get_memdir_root",
    "memory_enabled",
    "recall_memory",
    "short_hash",
    "slugify",
    "stats",
    "write_memory",
]
