"""Regression guards for Sonnet→Opus boundary research compaction.

Real-run trace 73a9d605 (2026-04-25) showed first-Opus turns paying a
~$0.66/run model-switch tax: cache_create of 35-70K tokens because
Opus has a separate cache namespace from Sonnet, so it re-caches the
entire conversation including raw web_fetch / web_search blobs.

The fix replaces those research blobs with brief summaries pointing at
on-disk artifacts (api_spec.txt + fetched_docs_*.txt) at the model-
switch point. Opus gets a much smaller message history → much smaller
cache_create on the first Opus turn.

Safety guarantees this test file locks:
  1. tool_use_id is preserved on every replaced block (the API
     enforces "every server-tool tool_use has a matching tool_result"
     pairing — break it and we get a 400).
  2. Block TYPE is preserved (web_fetch_tool_result stays
     web_fetch_tool_result) so the API recognizes the structure.
  3. Original page content is replaced with a clear pointer to disk
     artifacts; Opus can read_file them on demand (proven by audit:
     real Agent 5 already prefers read_file over web_fetch when files
     exist).
  4. Idempotent: calling twice doesn't double-mutate.
  5. Safe across both SDK-typed and dict block shapes (the API can
     return either depending on continuation vs fresh response).
  6. Mixed content (text + tool_use + tool_result) only touches
     research tool_results; text and tool_use blocks are preserved.
  7. Empty messages / messages with only string content are no-ops.
  8. Wired at the api_spec_written transition point, not on every turn.

Reference: PLAN_VOICE_RUN_OPTIMIZATIONS.md deep-dive item 1.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from puzzleeval.agents.implement_test_env import (
    _COMPACTED_FETCH_PLACEHOLDER,
    _block_attr,
    _block_type,
    _compact_research_tool_results,
    _compact_web_fetch_block,
    _compact_web_search_block,
)


# ============================================================================
# Helpers — minimal block factories for both shapes
# ============================================================================


def _dict_web_fetch_block(*, tool_use_id: str, url: str, data: str) -> dict:
    """Build a dict-shaped web_fetch_tool_result block."""
    return {
        "type": "web_fetch_tool_result",
        "tool_use_id": tool_use_id,
        "content": {
            "type": "web_fetch_result",
            "url": url,
            "content": {
                "type": "document",
                "source": {
                    "type": "text",
                    "data": data,
                },
            },
        },
    }


def _dict_web_search_block(*, tool_use_id: str, results: list[dict]) -> dict:
    """Build a dict-shaped web_search_tool_result block."""
    return {
        "type": "web_search_tool_result",
        "tool_use_id": tool_use_id,
        "content": results,
    }


class _FakeSDKObject:
    """SDK-typed objects use attribute access, not dict access. This
    fake mirrors the shape so we can test both code paths."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _sdk_web_fetch_block(*, tool_use_id: str, url: str, data: str) -> _FakeSDKObject:
    return _FakeSDKObject(
        type="web_fetch_tool_result",
        tool_use_id=tool_use_id,
        content=_FakeSDKObject(
            type="web_fetch_result",
            url=url,
            content=_FakeSDKObject(
                type="document",
                source=_FakeSDKObject(type="text", data=data),
            ),
        ),
    )


# ============================================================================
# 1. Empty / no-op cases
# ============================================================================


class TestNoOpCases:
    """Compaction must be safe to call on any messages list shape —
    empty list, string content, no research blocks. Returns 0 in all
    these cases without mutating anything."""

    def test_empty_messages_returns_zero(self):
        assert _compact_research_tool_results([]) == 0

    def test_messages_with_string_content_skipped(self):
        msgs = [
            {"role": "user", "content": "tell me about your API"},
            {"role": "assistant", "content": "Sure, I'll fetch the docs."},
        ]
        assert _compact_research_tool_results(msgs) == 0
        # No mutation
        assert msgs[1]["content"] == "Sure, I'll fetch the docs."

    def test_messages_without_research_blocks(self):
        msgs = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Just thinking out loud."},
                {"type": "tool_use", "id": "tu_1", "name": "read_file",
                 "input": {"path": "api_spec.txt"}},
            ]},
        ]
        assert _compact_research_tool_results(msgs) == 0
        # No mutation — text + tool_use blocks unchanged
        assert msgs[0]["content"][0]["text"] == "Just thinking out loud."
        assert msgs[0]["content"][1]["name"] == "read_file"

    def test_non_dict_messages_skipped(self):
        # Defensive: should not crash on malformed input
        msgs = ["not_a_dict", 42, None, {"role": "user", "content": []}]
        # Should not raise
        result = _compact_research_tool_results(msgs)
        assert result == 0


# ============================================================================
# 2. web_fetch compaction
# ============================================================================


class TestWebFetchCompaction:
    """The core fix: web_fetch_tool_result blobs (15-30K chars each)
    get replaced with brief pointers to disk artifacts."""

    def test_single_web_fetch_compacted(self):
        large_content = "X" * 30000
        msgs = [{
            "role": "assistant",
            "content": [_dict_web_fetch_block(
                tool_use_id="srvtoolu_abc",
                url="https://example.com/docs",
                data=large_content,
            )],
        }]
        n = _compact_research_tool_results(msgs)
        assert n == 1

        block = msgs[0]["content"][0]
        # SHAPE preserved
        assert block["type"] == "web_fetch_tool_result"
        # tool_use_id preserved (API requires this for pairing)
        assert block["tool_use_id"] == "srvtoolu_abc"
        # URL preserved (diagnostic value)
        assert block["content"]["url"] == "https://example.com/docs"
        # CONTENT replaced with pointer
        new_data = block["content"]["content"]["source"]["data"]
        assert _COMPACTED_FETCH_PLACEHOLDER in new_data
        # Massive size reduction
        assert len(new_data) < 1000
        assert len(new_data) < len(large_content) / 10

    def test_multiple_web_fetches_all_compacted(self):
        msgs = [{
            "role": "assistant",
            "content": [
                _dict_web_fetch_block(tool_use_id="t1", url="a.com", data="A" * 20000),
                _dict_web_fetch_block(tool_use_id="t2", url="b.com", data="B" * 20000),
                _dict_web_fetch_block(tool_use_id="t3", url="c.com", data="C" * 20000),
            ],
        }]
        n = _compact_research_tool_results(msgs)
        assert n == 3
        for block in msgs[0]["content"]:
            new_data = block["content"]["content"]["source"]["data"]
            assert _COMPACTED_FETCH_PLACEHOLDER in new_data
            assert len(new_data) < 1000

    def test_compaction_across_multiple_messages(self):
        msgs = [
            {"role": "assistant", "content": [
                _dict_web_fetch_block(tool_use_id="t1", url="a.com", data="A" * 10000),
            ]},
            {"role": "assistant", "content": [
                _dict_web_fetch_block(tool_use_id="t2", url="b.com", data="B" * 10000),
            ]},
        ]
        n = _compact_research_tool_results(msgs)
        assert n == 2

    def test_pointer_text_explains_disk_alternative(self):
        """The replacement text must clearly tell Opus where to find
        the original content (read_file the on-disk artifacts)."""
        msgs = [{
            "role": "assistant",
            "content": [_dict_web_fetch_block(
                tool_use_id="t1", url="x.com", data="lots of HTML",
            )],
        }]
        _compact_research_tool_results(msgs)
        new_data = msgs[0]["content"][0]["content"]["content"]["source"]["data"]
        assert "fetched_docs_" in new_data, (
            "Pointer text must mention fetched_docs_*.txt so Opus knows "
            "where to read_file for the original content."
        )
        assert "api_spec.txt" in new_data, (
            "Pointer text must reference api_spec.txt so Opus knows "
            "the synthesized contract is there."
        )


# ============================================================================
# 3. web_search compaction
# ============================================================================


class TestWebSearchCompaction:
    """web_search_tool_result blocks are LEFT UNCHANGED (NEW-AM v3 fix).

    Real-run trace 32a4b134 (2026-04-25) caught a 400 from the
    Anthropic API when our compaction set encrypted_content="" on
    web_search results: "Invalid `encrypted_content` in `search_result`
    block". The field expects a real encrypted blob or omitted entry;
    empty string is rejected.

    Skipping web_search compaction is safe + has negligible cost
    impact (web_search results are 5-7K each vs web_fetch's 5-25K).
    """

    def test_search_results_NOT_compacted(self):
        """Web_search blocks must pass through unchanged — the prior
        compaction broke API validation."""
        original_block = _dict_web_search_block(
            tool_use_id="search_xyz",
            results=[
                {"type": "web_search_result", "url": "a.com",
                 "title": "A Doc", "page_age": "2024-01-01",
                 "encrypted_content": "X" * 5000},
                {"type": "web_search_result", "url": "b.com",
                 "title": "B Doc", "page_age": "2024-01-02",
                 "encrypted_content": "Y" * 5000},
            ],
        )
        msgs = [{"role": "assistant", "content": [original_block]}]
        n = _compact_research_tool_results(msgs)
        assert n == 0, (
            "web_search blocks should NOT be compacted — Anthropic API "
            "rejects empty encrypted_content (real-run trace 32a4b134, "
            "2026-04-25)."
        )
        # Block is unchanged — original encrypted_content preserved
        block = msgs[0]["content"][0]
        assert block["type"] == "web_search_tool_result"
        assert block["tool_use_id"] == "search_xyz"
        assert len(block["content"]) == 2
        for entry in block["content"]:
            assert entry["encrypted_content"] in ("X" * 5000, "Y" * 5000), (
                "encrypted_content must be preserved untouched — the API "
                "rejects empty string + we have no valid sentinel value."
            )


# ============================================================================
# 4. Idempotence
# ============================================================================


class TestIdempotence:
    """Calling compaction twice must not break anything. The second
    call sees already-compacted blocks and re-replaces them with the
    same marker (no growth, no shrinkage)."""

    def test_double_compaction_safe(self):
        msgs = [{
            "role": "assistant",
            "content": [_dict_web_fetch_block(
                tool_use_id="t1", url="x.com", data="X" * 10000,
            )],
        }]
        n1 = _compact_research_tool_results(msgs)
        size_after_first = len(
            msgs[0]["content"][0]["content"]["content"]["source"]["data"]
        )
        n2 = _compact_research_tool_results(msgs)
        size_after_second = len(
            msgs[0]["content"][0]["content"]["content"]["source"]["data"]
        )
        assert n1 == 1
        assert n2 == 1  # Still finds the block (just re-replaces marker)
        assert size_after_first == size_after_second
        # tool_use_id still preserved
        assert msgs[0]["content"][0]["tool_use_id"] == "t1"


# ============================================================================
# 5. Mixed-shape support (SDK objects + dicts)
# ============================================================================


class TestMixedShapeSupport:
    """The Anthropic SDK returns typed objects in fresh responses but
    dict shapes after JSON serialization round-trips. Compaction must
    handle both."""

    def test_sdk_object_compacted_to_dict(self):
        sdk_block = _sdk_web_fetch_block(
            tool_use_id="sdk_id", url="https://sdk.example.com",
            data="S" * 25000,
        )
        msgs = [{"role": "assistant", "content": [sdk_block]}]
        n = _compact_research_tool_results(msgs)
        assert n == 1
        # Replaced object is a dict (more portable for re-send)
        new_block = msgs[0]["content"][0]
        assert isinstance(new_block, dict)
        assert new_block["type"] == "web_fetch_tool_result"
        assert new_block["tool_use_id"] == "sdk_id"
        assert new_block["content"]["url"] == "https://sdk.example.com"

    def test_mixed_sdk_and_dict_blocks(self):
        msgs = [{
            "role": "assistant",
            "content": [
                _sdk_web_fetch_block(tool_use_id="sdk_t", url="s.com", data="A" * 5000),
                _dict_web_fetch_block(tool_use_id="dict_t", url="d.com", data="B" * 5000),
            ],
        }]
        n = _compact_research_tool_results(msgs)
        assert n == 2
        # Both replaced as dicts
        for block in msgs[0]["content"]:
            assert isinstance(block, dict)


# ============================================================================
# 6. Mixed-block content (text + tool_use + research tool_result)
# ============================================================================


class TestMixedBlockContent:
    """The compaction must ONLY touch research tool_result blocks.
    Text, tool_use, and other tool_results must be preserved unchanged."""

    def test_text_and_tool_use_preserved(self):
        msgs = [{
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll fetch the docs now."},
                {"type": "tool_use", "id": "srvtoolu_1", "name": "web_fetch",
                 "input": {"url": "https://example.com"}},
                _dict_web_fetch_block(
                    tool_use_id="srvtoolu_1", url="https://example.com",
                    data="huge HTML page" * 1000,
                ),
                {"type": "text", "text": "Now I'll write api_spec.txt"},
            ],
        }]
        n = _compact_research_tool_results(msgs)
        assert n == 1, f"expected 1 compaction, got {n}"

        content = msgs[0]["content"]
        # Text blocks preserved
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "I'll fetch the docs now."
        # tool_use preserved
        assert content[1]["type"] == "tool_use"
        assert content[1]["id"] == "srvtoolu_1"
        assert content[1]["name"] == "web_fetch"
        # tool_result was the one compacted
        assert content[2]["type"] == "web_fetch_tool_result"
        assert _COMPACTED_FETCH_PLACEHOLDER in content[2]["content"]["content"]["source"]["data"]
        # Trailing text preserved
        assert content[3]["type"] == "text"
        assert content[3]["text"] == "Now I'll write api_spec.txt"

    def test_non_research_tool_results_preserved(self):
        """tool_use_result blocks for read_file, write_file, etc. are
        NOT compacted — they're typically small and useful to Opus."""
        msgs = [{
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "write_file",
                 "input": {"filename": "harness.py"}},
            ],
        }, {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1",
                 "content": "Written 5000 chars to harness.py"},
            ],
        }]
        n = _compact_research_tool_results(msgs)
        assert n == 0
        # write_file result preserved verbatim
        assert msgs[1]["content"][0]["content"] == "Written 5000 chars to harness.py"


# ============================================================================
# 7. Source-grep wiring guard
# ============================================================================


class TestWiredAtModelSwitch:
    """The compaction must fire AT the api_spec_written transition,
    not on every turn. If a refactor moves the call, this test catches it."""

    def test_compaction_called_when_api_spec_written(self):
        # Phase 5 Step 3: _compact_research_tool_results call site moved
        # to build_loop.py with the rest of _build_single_harness body.
        # The function DEFINITION still lives in implement_test_env.py;
        # its CALL SITE is in build_loop.py. Combined source finds both.
        root = Path(__file__).resolve().parents[1]
        impl = (root / "puzzleeval" / "agents" / "implement_test_env.py").read_text(encoding="utf-8")
        build_loop = (root / "puzzleeval" / "agents" / "agent5" / "build_loop.py").read_text(encoding="utf-8")
        src = impl + "\n# === build_loop.py ===\n" + build_loop
        # Find the api_spec_written = True flip location
        idx = src.find("api_spec_written = True")
        assert idx != -1, "api_spec_written flip site moved"
        # Compaction call must appear within the next ~3000 chars (in
        # the same conditional block that flipped the flag)
        nearby = src[idx:idx + 3000]
        assert "_compact_research_tool_results" in nearby, (
            "Compaction must be wired at the api_spec_written transition "
            "so it fires ONCE per build (not on every turn)."
        )

    def test_compaction_wrapped_in_try_except(self):
        """A malformed messages structure must NOT crash the build —
        compaction is a pure optimization, fail-silent on errors.

        rfind locates the LAST occurrence (the call site). The first
        occurrence is the function definition (`def _compact_...`).
        """
        # Phase 5 Step 3: _compact_research_tool_results call site moved
        # to build_loop.py with the rest of _build_single_harness body.
        # The function DEFINITION still lives in implement_test_env.py;
        # its CALL SITE is in build_loop.py. Combined source finds both.
        root = Path(__file__).resolve().parents[1]
        impl = (root / "puzzleeval" / "agents" / "implement_test_env.py").read_text(encoding="utf-8")
        build_loop = (root / "puzzleeval" / "agents" / "agent5" / "build_loop.py").read_text(encoding="utf-8")
        src = impl + "\n# === build_loop.py ===\n" + build_loop
        # Use rfind to skip past the def and find the call site
        idx = src.rfind("_compact_research_tool_results(")
        assert idx != -1
        # Walk back ~500 chars and verify there's a `try:` before the call
        preceding = src[max(0, idx - 500):idx]
        assert "try:" in preceding, (
            "Research compaction call must be wrapped in try/except — "
            "compaction is a pure optimization; failures must not crash "
            "the build."
        )

    def test_compaction_only_runs_in_phase_transition_branch(self):
        """The call must be inside the `if not api_spec_written and
        block.name == 'write_file'` branch, NOT on every turn."""
        # Phase 5 Step 3: _compact_research_tool_results call site moved
        # to build_loop.py with the rest of _build_single_harness body.
        # The function DEFINITION still lives in implement_test_env.py;
        # its CALL SITE is in build_loop.py. Combined source finds both.
        root = Path(__file__).resolve().parents[1]
        impl = (root / "puzzleeval" / "agents" / "implement_test_env.py").read_text(encoding="utf-8")
        build_loop = (root / "puzzleeval" / "agents" / "agent5" / "build_loop.py").read_text(encoding="utf-8")
        src = impl + "\n# === build_loop.py ===\n" + build_loop
        # Find ALL occurrences of _compact_research_tool_results in source
        positions = []
        i = 0
        while True:
            pos = src.find("_compact_research_tool_results(", i)
            if pos == -1: break
            positions.append(pos)
            i = pos + 1
        # Should be exactly 1 call site (plus the def itself)
        # Definitions: `def _compact_research_tool_results(`
        def_count = src.count("def _compact_research_tool_results(")
        call_count = len(positions) - def_count
        assert call_count == 1, (
            f"Expected exactly 1 call site for "
            f"_compact_research_tool_results, found {call_count}. "
            "Multiple call sites suggest it's being called per-turn "
            "(wasted work) instead of once at the model-switch boundary."
        )
