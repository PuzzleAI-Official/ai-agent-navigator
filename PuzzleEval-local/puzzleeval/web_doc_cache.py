"""Shared web-doc cache for Agent 4 â†’ Agent 5 handoff.

Agent 4 and Agent 5 both fetch API documentation via Anthropic's
server-side `web_fetch` tool. Before this module existed, Agent 4 fetched
`verified_api_docs_url`, used the page content ONCE to confirm "yes this
is real docs" (a boolean check), and discarded the 15K-token response.
Agent 5 then re-fetched the same URL as its Phase-1 STEP 1. Two costs:

  1. Double the network roundtrip (~3-5s per candidate).
  2. Double exposure to Cloudflare / WAF / 429 / 5xx. If Agent 4's
     fetch succeeded but Agent 5's second fetch was blocked, Agent 5
     had to search+fetch alternatives from scratch â€” wasting the work
     Agent 4 already did.

The fix: Agent 4 persists only successfully fetched, relevant API-doc
pages to the candidate's future sandbox directory as `fetched_docs_<n>.txt`.
Search snippets and irrelevant/inaccessible pages are discovery audit only.
Agent 5 sees the confirmed fetched files already on disk when its sandbox
opens, reads them via `read_file` (free, instant, CF-immune), and only
reaches for `web_fetch` on genuine gaps.

This module is the single source of truth for two pieces of shared
machinery both agents rely on:

  - The candidate â†’ sandbox path mapping (`candidate_sandbox_dir`) so
    Agent 4 and Agent 5 agree on where to write / read.
  - The web_fetch relevance gate plus discovery audit logic
    (`save_web_fetches_to_sandbox`), previously duplicated inside
    Agent 5's `_extract_and_save_web_content`.

Both are pure functions on the filesystem + Anthropic SDK response
shape â€” no client, no network, no model call. They can be unit-tested
without any API key.

## Why this lives outside agents/

Putting it in `agents/implement_test_env.py` (as it used to be) meant
`agents/screening.py` would have to import from a sibling agent module â€”
a layering inversion. `web_doc_cache` is infrastructure, not agent
logic, so it sits at package root alongside `config.py`,
`anthropic_client.py`, `pipeline.py`.

## Back-compat

Agent 5's original `_extract_and_save_web_content` signature is preserved
as a thin shim that calls this module. Callers in implement_test_env.py
didn't have to change. The shim is intentionally kept so any future
refactor doesn't have to touch the builder-loop call site.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover â€” import avoided at runtime
    import anthropic  # noqa: F401


# ============================================================================
# Candidate â†’ filesystem path routing
# ============================================================================
#
# Agent 4 runs BEFORE Agent 5 creates the per-candidate sandbox. For the
# handoff to work, both agents must compute the SAME path from the
# candidate's name. Centralizing here ensures no drift: if we change the
# slug rules later, both agents pick up the change together.
# ============================================================================

# Cap on saved page size. The web_fetch tool itself is capped at
# `max_content_tokens=15000` (~60K chars) when Agent 5 configures it.
# Agent 4 doesn't cap its fetches, so pages can be larger. 50K chars
# covers the 99th percentile of real API docs; OpenAI's huge reference
# still fits. Pages beyond this are almost always nav + duplicated
# content that Agent 5's context-management will drop anyway.
MAX_SAVED_PAGE_CHARS = 50_000

# Search result snippets are denser per char (titles + URLs + brief
# page_snippets). 30K is enough for the 10-15 results a single search
# typically returns, with budget for 2-3 searches per candidate.
MAX_SAVED_SEARCH_CHARS = 30_000

EXCLUDED_DISCOVERY_FILENAME = "excluded_docs_discovery.json"

_URL_DOC_MARKERS = (
    "/api",
    "api.",
    "/docs",
    "docs.",
    "developer",
    "developers",
    "/reference",
    "reference.",
    "/sdk",
    "/quickstart",
    "/guides",
    "openapi",
    "swagger",
)

_CONTENT_DOC_MARKERS = (
    "api key",
    "authorization",
    "bearer",
    "authentication",
    "endpoint",
    "request",
    "response",
    "sdk",
    "curl",
    "websocket",
    "rest api",
    "openapi",
    "swagger",
    "http ",
    "https://",
)

_HTTP_METHOD_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_.:/?&={}\-]+)",
    re.IGNORECASE,
)


def candidate_slug(name: str) -> str:
    """Convert a candidate name to a filesystem-safe directory name.

    The canonical slug for every disk-routed resource (sandbox dir,
    prefetched docs, harness files, voice artifacts) that belongs to a
    candidate. Single source of truth â€” Agent 4 and Agent 5 must call
    THIS function (not roll their own) so their disk paths align.

    Examples:
        "Google Document AI" â†’ "google_document_ai"
        "AWS Textract (OCR)" â†’ "aws_textract_ocr"
        "ElevenLabs Conversational AI" â†’ "elevenlabs_conversational_ai"

    The 40-char cap is a Windows path-length-safety margin, not a
    semantic choice: long names still disambiguate on their first
    40 chars in practice.
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:40]


def candidate_sandbox_dir(trace_id: str, candidate_name: str,
                          runs_root: Path | str | None = None) -> Path:
    """Compute the sandbox directory for a candidate in a given run.

    Matches the path Agent 5 uses in `run_implement_test_env_agent`:
    ``runs/<trace_id>/harnesses/<candidate_slug>/``. Resolved to an
    absolute path so the backend's audio-streaming containment check
    (which resolves against allowed roots) works uniformly whether the
    caller was Agent 4 (pre-sandbox-creation) or Agent 5 (post).

    Agent 4 calls this to decide where to write `fetched_docs_*.txt`;
    Agent 5 will later compute the same path and find those files
    already there.

    `runs_root` lets API/server callers route all artifacts into their
    configured run directory. CLI/local callers may omit it to use "runs".
    """
    root = Path(runs_root) if runs_root else Path("runs")
    return (root / trace_id / "harnesses" / candidate_slug(candidate_name)).resolve()


def count_existing_fetched_docs(sandbox_dir: Path) -> int:
    """Count `fetched_docs_<n>.txt` files already in the sandbox.

    Agent 5 uses this when starting a build to find out how many docs
    Agent 4 prefetched. The count is then passed as `existing_count`
    to `save_web_fetches_to_sandbox` so Agent 5's own fetches
    (when it needs any) are indexed after Agent 4's.

    Returns 0 on a missing directory â€” the common case for Agent 4 runs
    where no candidate has been verified yet.
    """
    if not sandbox_dir.exists():
        return 0
    return sum(
        1 for p in sandbox_dir.iterdir()
        if p.is_file()
        and p.name.startswith("fetched_docs_")
        and p.name.endswith(".txt")
    )


# ============================================================================
# Response â†’ disk persistence
# ============================================================================


def _api_doc_relevance(url: str, text: str) -> tuple[bool, str]:
    """Return whether fetched content is useful API docs for Agent 5."""

    normalized_url = (url or "").lower()
    sample = (text or "")[:MAX_SAVED_PAGE_CHARS].lower()
    if not sample.strip():
        return False, "empty_fetch_result"

    url_hits = sum(1 for marker in _URL_DOC_MARKERS if marker in normalized_url)
    content_hits = sum(1 for marker in _CONTENT_DOC_MARKERS if marker in sample)
    method_hit = bool(_HTTP_METHOD_RE.search(text or ""))
    has_code_or_schema = any(
        token in sample
        for token in ("json", "python", "javascript", "curl", "schema", "parameter")
    )
    if content_hits >= 3 and (url_hits >= 1 or method_hit or has_code_or_schema):
        return True, "api_doc_signals"
    if method_hit and content_hits >= 1:
        return True, "http_endpoint_signals"
    return False, "insufficient_api_doc_signals"


def _append_excluded_discovery(
    sandbox_dir: Path,
    *,
    source_url: str,
    block_type: str,
    reason: str,
    title: str = "",
) -> None:
    """Best-effort audit of discovery material hidden from Agent 5 context."""

    try:
        state_dir = sandbox_dir / "_agent_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / EXCLUDED_DISCOVERY_FILENAME
        payload: list[dict[str, object]] = []
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    payload = [item for item in loaded if isinstance(item, dict)]
            except (OSError, json.JSONDecodeError):
                payload = []
        payload.append({
            "source_url": source_url,
            "title": title,
            "block_type": block_type,
            "reason": reason,
            "t_abs": time.time(),
        })
        path.write_text(
            json.dumps(payload[-50:], indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        return


def save_web_fetches_to_sandbox(
    response: "anthropic.types.Message",
    sandbox_dir: Path,
    existing_count: int = 0,
) -> list[str]:
    """Extract relevant fetched API-doc pages and audit discovery-only hits.

    This is Agent 5's original `_extract_and_save_web_content`, moved
    here so Agent 4 can use it too. Writes to
    ``sandbox_dir/fetched_docs_<n>.txt`` where ``n`` starts at
    ``existing_count``. Returns the list of filenames saved this call.

    Both tool result shapes are handled:
      - web_fetch: page text is saved only when it has API-doc signals.
      - web_search: result titles/URLs/snippets are recorded as audit-only
        discovery evidence and are not exposed as Agent 5 build context.

    The function swallows per-block extraction errors silently
    (non-critical: if extraction fails, content stays in conversation
    history and the loop continues normally). This matches Agent 5's
    prior behavior.

    Args:
        response: The Anthropic message response. Iterated for
            tool-result content blocks.
        sandbox_dir: Where to write the `fetched_docs_*.txt` files.
            Created if it doesn't exist.
        existing_count: Starting index for the filename suffix.
            Callers who've already saved files from prior turns (or
            from Agent 4's earlier calls) pass the count so new
            filenames don't collide with existing ones.

    Returns:
        List of filenames saved (e.g., ["fetched_docs_3.txt",
        "fetched_docs_4.txt"]). Empty list when no extractable
        content was found or all extractions failed.
    """
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    saved_files: list[str] = []
    fetch_count = existing_count

    for block in response.content:
        block_type = getattr(block, "type", "")
        if block_type == "web_fetch_tool_result":
            try:
                # Nav: WebFetchToolResultBlock â†’ WebFetchBlock â†’
                #      DocumentBlock â†’ PlainTextSource
                fetch_block = block.content
                if hasattr(fetch_block, "content") and hasattr(
                    fetch_block.content, "source"
                ):
                    page_text = fetch_block.content.source.data
                    url = getattr(fetch_block, "url", "unknown")
                    relevant, reason = _api_doc_relevance(str(url), str(page_text))
                    if not relevant:
                        _append_excluded_discovery(
                            sandbox_dir,
                            source_url=str(url),
                            block_type="web_fetch_tool_result",
                            reason=reason,
                        )
                        continue
                    filename = f"fetched_docs_{fetch_count}.txt"
                    filepath = sandbox_dir / filename
                    header = (
                        f"# Fetched from: {url}\n"
                        f"# Evidence status: fetched_current_api_docs\n"
                        f"# Saved for Agent 5 build context\n\n"
                    )
                    filepath.write_text(
                        header + page_text[:MAX_SAVED_PAGE_CHARS],
                        encoding="utf-8",
                    )
                    saved_files.append(filename)
                    fetch_count += 1
            except (AttributeError, OSError):
                # Non-critical â€” if extraction fails, web content stays
                # in context and the build loop continues.
                pass
        elif block_type == "web_search_tool_result":
            try:
                search_block = block.content
                if hasattr(search_block, "content") and search_block.content:
                    for entry in search_block.content:
                        _append_excluded_discovery(
                            sandbox_dir,
                            source_url=str(getattr(entry, "url", "") or ""),
                            title=str(getattr(entry, "title", "") or ""),
                            block_type="web_search_tool_result",
                            reason="search_result_discovery_only",
                        )
            except (AttributeError, OSError, TypeError):
                pass
            continue

    return saved_files


# Density scoring used to live here as a heuristic for ranking prefetched docs
# by structural richness (code fences, endpoints, auth headers, WebSocket
# mentions). The same ranking math survives as ``_usefulness_signal`` (soft
# ordering hint, not a gate) in ``puzzleeval.agents.agent5.initial_message``.


__all__ = [
    "candidate_slug",
    "candidate_sandbox_dir",
    "count_existing_fetched_docs",
    "save_web_fetches_to_sandbox",
    "EXCLUDED_DISCOVERY_FILENAME",
    "MAX_SAVED_PAGE_CHARS",
    "MAX_SAVED_SEARCH_CHARS",
]
