"""Regression guards for puzzleeval.web_doc_cache.

This module is the Agent 4 → Agent 5 doc handoff. Tests cover:
  - The slug / path contract both agents depend on (drift here =
    Agent 4 writes to one place, Agent 5 reads from another, handoff
    silently broken).
  - The extract-and-save helper behavior across web_fetch and
    web_search tool result shapes.
  - The counter that lets Agent 5 seed `saved_doc_files` from Agent 4's
    contributions without filename collision.
  - Back-compat of the implement_test_env.py shim.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from puzzleeval.web_doc_cache import (
    MAX_SAVED_PAGE_CHARS,
    MAX_SAVED_SEARCH_CHARS,
    candidate_sandbox_dir,
    candidate_slug,
    count_existing_fetched_docs,
    save_web_fetches_to_sandbox,
)


# ============================================================================
# Slug + path contract
# ============================================================================


class TestCandidateSlugIsCanonical:
    """Both Agent 4 and Agent 5 must compute the same disk path for the
    same candidate. Any change to the slug rules applies to BOTH
    automatically via this shared helper."""

    def test_basic_name_becomes_lowercase_underscored(self):
        assert candidate_slug("Google Document AI") == "google_document_ai"

    def test_special_chars_collapsed_to_underscore(self):
        assert candidate_slug("AWS Textract (OCR)") == "aws_textract_ocr"

    def test_trailing_underscores_stripped(self):
        # Trailing punctuation must not leave a dangling underscore;
        # filesystems on Windows dislike trailing dots/underscores on
        # directory names.
        assert candidate_slug("Foo!!!") == "foo"
        assert candidate_slug("   spaced   ") == "spaced"

    def test_very_long_name_capped_at_40(self):
        long = "a very long candidate name " * 5
        slug = candidate_slug(long)
        assert len(slug) <= 40

    def test_unicode_non_ascii_stripped(self):
        # Unicode letters outside ASCII get collapsed; this matches the
        # old `_candidate_slug` behavior. Change here would cascade —
        # any existing runs with emoji-named candidates would lose their
        # handoff.
        assert candidate_slug("Resumé Café") == "resum_caf"


class TestCandidateSandboxDir:
    """The path returned must align with Agent 5's `harness_base /
    slug` convention. Agent 5 computes the same path at
    implement_test_env.py:~6484 (`sandbox_dir = harness_base / slug`);
    if these diverge, the handoff is silently broken."""

    def test_matches_agent5_path_convention(self, tmp_path):
        d = candidate_sandbox_dir(
            "trace-abc", "Google Document AI", runs_root=tmp_path,
        )
        expected_tail = Path("trace-abc") / "harnesses" / "google_document_ai"
        # Compare as strings to avoid resolve() symlink quirks on Windows
        assert str(d).endswith(str(expected_tail))

    def test_path_is_absolute(self, tmp_path):
        d = candidate_sandbox_dir(
            "trace-xyz", "ElevenLabs", runs_root=tmp_path,
        )
        assert d.is_absolute(), (
            "Backend audio-streaming containment check resolves against "
            "absolute allowed roots; a relative path here would make "
            "every served audio file 404."
        )

    def test_default_runs_root_is_runs_dir(self):
        # Production callers don't pass runs_root — the default must be
        # the CWD's "runs" dir, matching Agent 5's
        # `Path("runs") / trace_id / "harnesses"`.
        d = candidate_sandbox_dir("trace-1", "X")
        assert "runs" in str(d).lower()


# ============================================================================
# save_web_fetches_to_sandbox — the real handoff mechanism
# ============================================================================


def _mock_fetch_response(url: str, text: str):
    """Construct a minimal Anthropic-response-shaped object with one
    web_fetch_tool_result block carrying the given URL + text."""
    source = SimpleNamespace(data=text)
    document = SimpleNamespace(source=source)
    fetch_block = SimpleNamespace(url=url, content=document)
    tool_result = SimpleNamespace(
        type="web_fetch_tool_result", content=fetch_block,
    )
    return SimpleNamespace(content=[tool_result])


def _mock_search_response(entries: list[tuple[str, str, str]]):
    """`entries` is a list of (title, url, page_snippet) triples.
    Shape matches Anthropic's web_search_tool_result.content."""
    search_entries = [
        SimpleNamespace(title=t, url=u, page_snippet=s)
        for (t, u, s) in entries
    ]
    search_block = SimpleNamespace(content=search_entries)
    tool_result = SimpleNamespace(
        type="web_search_tool_result", content=search_block,
    )
    return SimpleNamespace(content=[tool_result])


class TestSaveWebFetchesRoundTrip:

    def test_saves_single_web_fetch_to_disk(self, tmp_path):
        resp = _mock_fetch_response(
            "https://example.com/docs/auth",
            "POST /v1/token\nAuthorization: Bearer $KEY\n" + "detail\n" * 50,
        )
        saved = save_web_fetches_to_sandbox(resp, tmp_path)
        assert saved == ["fetched_docs_0.txt"]
        content = (tmp_path / "fetched_docs_0.txt").read_text(encoding="utf-8")
        assert content.startswith("# Fetched from: https://example.com/docs/auth")
        assert "POST /v1/token" in content
        assert "Authorization: Bearer $KEY" in content

    def test_existing_count_shifts_filename_index(self, tmp_path):
        # Agent 4 writes fetched_docs_0, then Agent 5's first web_fetch
        # should start at fetched_docs_1 — NOT overwrite 0.
        resp = _mock_fetch_response("https://a.com/", "first")
        saved_a = save_web_fetches_to_sandbox(resp, tmp_path, existing_count=0)
        assert saved_a == ["fetched_docs_0.txt"]

        resp2 = _mock_fetch_response("https://b.com/", "second")
        saved_b = save_web_fetches_to_sandbox(resp2, tmp_path, existing_count=1)
        assert saved_b == ["fetched_docs_1.txt"]
        # Both files must still exist; the second call didn't stomp the first
        assert (tmp_path / "fetched_docs_0.txt").exists()
        assert (tmp_path / "fetched_docs_1.txt").exists()
        assert "first" in (tmp_path / "fetched_docs_0.txt").read_text(encoding="utf-8")
        assert "second" in (tmp_path / "fetched_docs_1.txt").read_text(encoding="utf-8")

    def test_mkdirs_sandbox_if_missing(self, tmp_path):
        # Agent 4 runs before Agent 5 creates the sandbox; the helper
        # must create the dir on demand.
        target = tmp_path / "nonexistent" / "sandbox"
        assert not target.exists()
        resp = _mock_fetch_response("https://x.com/", "content")
        save_web_fetches_to_sandbox(resp, target)
        assert target.exists()
        assert (target / "fetched_docs_0.txt").exists()

    def test_returns_empty_list_when_response_has_no_fetches(self, tmp_path):
        # Error responses, advisor-only turns, and plain-text-only turns
        # all have no tool-result blocks. Helper should return [] and
        # not raise.
        empty_resp = SimpleNamespace(content=[])
        saved = save_web_fetches_to_sandbox(empty_resp, tmp_path)
        assert saved == []

    def test_swallows_malformed_blocks(self, tmp_path):
        # Partial/malformed blocks (missing .source, missing .content)
        # are a real production case when web_fetch returns an error
        # shape. Helper must skip silently — screening.py's try/except
        # wraps the call anyway, but robustness here prevents the
        # outer catch from ever firing on partial success.
        bad_block = SimpleNamespace(
            type="web_fetch_tool_result",
            content=SimpleNamespace(url="https://broken"),  # no .content
        )
        good_block = _mock_fetch_response(
            "https://good.com/", "real content"
        ).content[0]
        resp = SimpleNamespace(content=[bad_block, good_block])
        saved = save_web_fetches_to_sandbox(resp, tmp_path)
        # Good block saved, bad block skipped — partial success is the
        # right behavior for a best-effort handoff.
        assert saved == ["fetched_docs_0.txt"]

    def test_caps_page_at_max_saved_chars(self, tmp_path):
        huge = "x" * (MAX_SAVED_PAGE_CHARS + 1000)
        resp = _mock_fetch_response("https://huge.com/", huge)
        save_web_fetches_to_sandbox(resp, tmp_path)
        content = (tmp_path / "fetched_docs_0.txt").read_text(encoding="utf-8")
        # Content + header; header is small, so total content length
        # minus header length should be at most MAX_SAVED_PAGE_CHARS.
        body_len = len(content) - len("# Fetched from: https://huge.com/\n# Saved for reference during build phase\n\n")
        assert body_len <= MAX_SAVED_PAGE_CHARS

    def test_saves_search_results_separately(self, tmp_path):
        resp = _mock_search_response([
            ("Docs Home", "https://ex.com/docs", "Overview of the API"),
            ("Auth Guide", "https://ex.com/docs/auth", "Use Bearer tokens"),
        ])
        saved = save_web_fetches_to_sandbox(resp, tmp_path)
        assert saved == ["fetched_docs_0.txt"]
        content = (tmp_path / "fetched_docs_0.txt").read_text(encoding="utf-8")
        assert "Docs Home" in content
        assert "Auth Guide" in content
        assert "https://ex.com/docs/auth" in content

    def test_caps_search_at_max_saved_search_chars(self, tmp_path):
        # One giant snippet
        resp = _mock_search_response([
            ("T", "https://x.com/", "s" * (MAX_SAVED_SEARCH_CHARS + 5000)),
        ])
        save_web_fetches_to_sandbox(resp, tmp_path)
        content = (tmp_path / "fetched_docs_0.txt").read_text(encoding="utf-8")
        body_len = len(content) - len("# Search results — saved for reference during build phase\n\n")
        assert body_len <= MAX_SAVED_SEARCH_CHARS


# ============================================================================
# count_existing_fetched_docs — used by Agent 5 to seed its counter
# ============================================================================


class TestCountExistingFetchedDocs:

    def test_missing_dir_returns_zero(self, tmp_path):
        assert count_existing_fetched_docs(tmp_path / "does-not-exist") == 0

    def test_empty_dir_returns_zero(self, tmp_path):
        assert count_existing_fetched_docs(tmp_path) == 0

    def test_counts_only_fetched_docs_files(self, tmp_path):
        (tmp_path / "fetched_docs_0.txt").write_text("a")
        (tmp_path / "fetched_docs_1.txt").write_text("b")
        (tmp_path / "fetched_docs_2.txt").write_text("c")
        (tmp_path / "harness.py").write_text("# unrelated")
        (tmp_path / "random.txt").write_text("also unrelated")
        assert count_existing_fetched_docs(tmp_path) == 3

    def test_ignores_subdirectories(self, tmp_path):
        (tmp_path / "fetched_docs_0.txt").write_text("a")
        subdir = tmp_path / "nested"
        subdir.mkdir()
        (subdir / "fetched_docs_99.txt").write_text("nested — not counted")
        assert count_existing_fetched_docs(tmp_path) == 1


# ============================================================================
# Back-compat shim at implement_test_env.py
# ============================================================================


class TestAgent5BackCompatShim:
    """Agent 5's original `_extract_and_save_web_content` was moved into
    web_doc_cache.py. A thin shim preserves the call signature so the
    builder-loop call site at ~line 4003 didn't have to change."""

    def test_shim_still_importable_with_old_name(self):
        from puzzleeval.agents.implement_test_env import _extract_and_save_web_content
        assert callable(_extract_and_save_web_content)

    def test_shim_round_trips_to_shared_helper(self, tmp_path):
        from puzzleeval.agents.implement_test_env import _extract_and_save_web_content
        resp = _mock_fetch_response("https://back-compat.com/", "shim works")
        saved = _extract_and_save_web_content(resp, tmp_path, existing_count=5)
        assert saved == ["fetched_docs_5.txt"]
        content = (tmp_path / "fetched_docs_5.txt").read_text(encoding="utf-8")
        assert "shim works" in content
