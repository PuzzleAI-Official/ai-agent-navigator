"""Shared web-doc cache for Agent 4 → Agent 5 handoff.

Agent 4 and Agent 5 both fetch API documentation via Anthropic's
server-side `web_fetch` tool. Before this module existed, Agent 4 fetched
`verified_api_docs_url`, used the page content ONCE to confirm "yes this
is real docs" (a boolean check), and discarded the 15K-token response.
Agent 5 then re-fetched the same URL as its Phase-1 STEP 1. Two costs:

  1. Double the network roundtrip (~3-5s per candidate).
  2. Double exposure to Cloudflare / WAF / 429 / 5xx. If Agent 4's
     fetch succeeded but Agent 5's second fetch was blocked, Agent 5
     had to search+fetch alternatives from scratch — wasting the work
     Agent 4 already did.

The fix: Agent 4 persists every `web_fetch_tool_result` to the candidate's
future sandbox directory as `fetched_docs_<n>.txt`. Agent 5 sees those
files already on disk when its sandbox opens, reads them via `read_file`
(free, instant, CF-immune), and only reaches for `web_fetch` on genuine
gaps.

This module is the single source of truth for two pieces of shared
machinery both agents rely on:

  - The candidate → sandbox path mapping (`candidate_sandbox_dir`) so
    Agent 4 and Agent 5 agree on where to write / read.
  - The web_fetch / web_search extract-and-persist logic
    (`save_web_fetches_to_sandbox`), previously duplicated inside
    Agent 5's `_extract_and_save_web_content`.

Both are pure functions on the filesystem + Anthropic SDK response
shape — no client, no network, no model call. They can be unit-tested
without any API key.

## Why this lives outside agents/

Putting it in `agents/implement_test_env.py` (as it used to be) meant
`agents/screening.py` would have to import from a sibling agent module —
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

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — import avoided at runtime
    import anthropic  # noqa: F401


# ============================================================================
# Candidate → filesystem path routing
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


def candidate_slug(name: str) -> str:
    """Convert a candidate name to a filesystem-safe directory name.

    The canonical slug for every disk-routed resource (sandbox dir,
    prefetched docs, harness files, voice artifacts) that belongs to a
    candidate. Single source of truth — Agent 4 and Agent 5 must call
    THIS function (not roll their own) so their disk paths align.

    Examples:
        "Google Document AI" → "google_document_ai"
        "AWS Textract (OCR)" → "aws_textract_ocr"
        "ElevenLabs Conversational AI" → "elevenlabs_conversational_ai"

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

    `runs_root` is test-override-only; production always uses "runs".
    """
    root = Path(runs_root) if runs_root else Path("runs")
    return (root / trace_id / "harnesses" / candidate_slug(candidate_name)).resolve()


def count_existing_fetched_docs(sandbox_dir: Path) -> int:
    """Count `fetched_docs_<n>.txt` files already in the sandbox.

    Agent 5 uses this when starting a build to find out how many docs
    Agent 4 prefetched. The count is then passed as `existing_count`
    to `save_web_fetches_to_sandbox` so Agent 5's own fetches
    (when it needs any) are indexed after Agent 4's.

    Returns 0 on a missing directory — the common case for Agent 4 runs
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
# Response → disk persistence
# ============================================================================


def save_web_fetches_to_sandbox(
    response: "anthropic.types.Message",
    sandbox_dir: Path,
    existing_count: int = 0,
) -> list[str]:
    """Extract page text from `web_fetch_tool_result` and
    `web_search_tool_result` blocks and save them to disk.

    This is Agent 5's original `_extract_and_save_web_content`, moved
    here so Agent 4 can use it too. Writes to
    ``sandbox_dir/fetched_docs_<n>.txt`` where ``n`` starts at
    ``existing_count``. Returns the list of filenames saved this call.

    Both tool result shapes are handled:
      - web_fetch: ``block.content.content.source.data`` holds the page
        text (PlainTextSource format).
      - web_search: ``block.content`` is a list of search result entries,
        each with title / url / page_snippet. Useful snippets embed
        endpoint URLs, auth examples, and SDK install commands that
        save a follow-up fetch.

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
                # Nav: WebFetchToolResultBlock → WebFetchBlock →
                #      DocumentBlock → PlainTextSource
                fetch_block = block.content
                if hasattr(fetch_block, "content") and hasattr(
                    fetch_block.content, "source"
                ):
                    page_text = fetch_block.content.source.data
                    url = getattr(fetch_block, "url", "unknown")
                    filename = f"fetched_docs_{fetch_count}.txt"
                    filepath = sandbox_dir / filename
                    header = (
                        f"# Fetched from: {url}\n"
                        f"# Saved for reference during build phase\n\n"
                    )
                    filepath.write_text(
                        header + page_text[:MAX_SAVED_PAGE_CHARS],
                        encoding="utf-8",
                    )
                    saved_files.append(filename)
                    fetch_count += 1
            except (AttributeError, OSError):
                # Non-critical — if extraction fails, web content stays
                # in context and the build loop continues.
                pass
        elif block_type == "web_search_tool_result":
            try:
                search_block = block.content
                if hasattr(search_block, "content") and search_block.content:
                    snippets: list[str] = []
                    for entry in search_block.content:
                        if hasattr(entry, "title") and hasattr(entry, "url"):
                            snippets.append(f"## {entry.title}\nURL: {entry.url}")
                        if hasattr(entry, "page_snippet"):
                            snippets.append(entry.page_snippet)
                        elif hasattr(entry, "text"):
                            snippets.append(entry.text)
                    if snippets:
                        filename = f"fetched_docs_{fetch_count}.txt"
                        filepath = sandbox_dir / filename
                        header = (
                            "# Search results — saved for reference during build phase\n\n"
                        )
                        filepath.write_text(
                            header + "\n\n---\n\n".join(snippets)[:MAX_SAVED_SEARCH_CHARS],
                            encoding="utf-8",
                        )
                        saved_files.append(filename)
                        fetch_count += 1
            except (AttributeError, OSError, TypeError):
                pass

    return saved_files


# Density scoring used to live here as a heuristic for ranking
# prefetched docs by structural richness (code fences, endpoints,
# auth headers, WebSocket mentions). It was deleted when Agent 4's
# BuildReadinessChecklist replaced it — the checklist directly
# encodes "what the builder needs" via per-field source URLs,
# making the "what looks dense" proxy obsolete. The same ranking
# math survives as ``_usefulness_signal`` (soft ordering hint, not
# a gate) in ``puzzleeval.agents.agent5.initial_message``.


__all__ = [
    "candidate_slug",
    "candidate_sandbox_dir",
    "count_existing_fetched_docs",
    "save_web_fetches_to_sandbox",
    "MAX_SAVED_PAGE_CHARS",
    "MAX_SAVED_SEARCH_CHARS",
]
