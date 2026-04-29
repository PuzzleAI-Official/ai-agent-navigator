"""Tests for puzzleeval.agents.agent5.turn_blocks (Phase 4.1).

Pin the orphan-scrubber's behavior contracts:
  * Detection: identifies server_tool_use blocks lacking a matching
    *_tool_result.
  * Suffix matching: any *_tool_result type counts as a match (catches
    advisor_tool_result, future tool families).
  * In-place mutation when the response object isn't frozen.
  * Frozen-fallback signal when mutation fails.
  * Empty-cleaned-list fallback to a placeholder text block.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from puzzleeval.agents.agent5.turn_blocks import (
    SERVER_TOOL_NAMES,
    _summarize_tool_call,
    build_initial_turn_log,
    detect_orphan_server_tool_uses,
    emit_build_turn_progress,
    strip_orphan_server_tool_uses,
)


def _block(btype: str, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(type=btype, **kwargs)


def _response(content: list) -> SimpleNamespace:
    """Build a fake response with mutable .content (not a frozen pydantic model)."""
    return SimpleNamespace(content=content)


class TestDetectOrphans:
    def test_no_blocks_returns_empty(self):
        assert detect_orphan_server_tool_uses(_response([])) == set()

    def test_paired_web_search_no_orphan(self):
        resp = _response([
            _block("server_tool_use", id="t1", name="web_search"),
            _block("web_search_tool_result", tool_use_id="t1"),
        ])
        assert detect_orphan_server_tool_uses(resp) == set()

    def test_paired_advisor_no_orphan_via_suffix_match(self):
        """voice_dual_3 regression: advisor_tool_result must pair via
        suffix, not explicit allowlist."""
        resp = _response([
            _block("server_tool_use", id="a1", name="advisor"),
            _block("advisor_tool_result", tool_use_id="a1"),
        ])
        assert detect_orphan_server_tool_uses(resp) == set()

    def test_unpaired_web_search_is_orphan(self):
        resp = _response([
            _block("server_tool_use", id="t1", name="web_search"),
        ])
        assert detect_orphan_server_tool_uses(resp) == {"t1"}

    def test_mixed_paired_and_orphan(self):
        resp = _response([
            _block("server_tool_use", id="paired", name="web_search"),
            _block("web_search_tool_result", tool_use_id="paired"),
            _block("server_tool_use", id="orphan", name="web_fetch"),
            _block("text", text="some text"),
        ])
        assert detect_orphan_server_tool_uses(resp) == {"orphan"}

    def test_local_tool_result_is_not_a_server_match(self):
        """The local 'tool_result' (without family prefix) is the
        client-side custom-tool result, NOT a server-tool result.
        It should NOT pair with server_tool_use blocks."""
        resp = _response([
            _block("server_tool_use", id="t1", name="web_search"),
            _block("tool_result", tool_use_id="t1"),  # local, not server
        ])
        # The server_tool_use is still orphaned because plain tool_result
        # doesn't count.
        assert detect_orphan_server_tool_uses(resp) == {"t1"}


class TestStripOrphans:
    def test_no_orphans_returns_unchanged(self):
        content = [
            _block("server_tool_use", id="t1", name="web_search"),
            _block("web_search_tool_result", tool_use_id="t1"),
        ]
        resp = _response(list(content))
        cleaned, orphans, mutated = strip_orphan_server_tool_uses(resp)
        assert orphans == frozenset()
        assert mutated  # no mutation needed = trivially "succeeded"
        assert len(cleaned) == 2

    def test_orphan_removed_from_cleaned(self):
        content = [
            _block("server_tool_use", id="orphan", name="web_search"),
            _block("text", text="hello"),
        ]
        resp = _response(content)
        cleaned, orphans, mutated = strip_orphan_server_tool_uses(resp)
        assert orphans == {"orphan"}
        assert mutated
        assert len(cleaned) == 1
        assert cleaned[0].type == "text"

    def test_in_place_mutation_when_possible(self):
        content = [
            _block("server_tool_use", id="orphan", name="web_search"),
            _block("text", text="hello"),
        ]
        resp = _response(content)
        cleaned, orphans, mutated = strip_orphan_server_tool_uses(resp)
        assert mutated
        # SimpleNamespace.content was replaced
        assert len(resp.content) == 1
        assert resp.content[0].type == "text"

    def test_empty_cleaned_falls_back_to_placeholder(self):
        """When ALL blocks were orphans, return a minimal text block so
        the conversation retains valid shape."""
        content = [
            _block("server_tool_use", id="orphan", name="web_search"),
        ]
        resp = _response(content)
        cleaned, orphans, mutated = strip_orphan_server_tool_uses(resp)
        assert orphans == {"orphan"}
        assert len(cleaned) == 1
        assert cleaned[0].get("type") == "text" if isinstance(cleaned[0], dict) else cleaned[0].type == "text"
        # Verify the placeholder text mentions truncation
        text = cleaned[0]["text"] if isinstance(cleaned[0], dict) else cleaned[0].text
        assert "truncated" in text.lower() or "research" in text.lower()


class TestServerToolNames:
    def test_canonical_set_includes_known_tools(self):
        """The set must include web_search, web_fetch, advisor — the
        three server tools Agent 5 uses today."""
        assert "web_search" in SERVER_TOOL_NAMES
        assert "web_fetch" in SERVER_TOOL_NAMES
        assert "advisor" in SERVER_TOOL_NAMES


# ---------------------------------------------------------------------------
# Phase 4 Path B helper-level tests — direct contracts for the new helpers
# extracted from _build_single_harness. These pin the per-helper API surface
# so future refactors can lean on them without re-running the full mock
# pipeline.
# ---------------------------------------------------------------------------


def _usage(input_tokens=100, output_tokens=50, cache_read=0, cache_create=0,
           iterations=None):
    """Build a SimpleNamespace mimicking response.usage shape."""
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_create,
        iterations=iterations or [],
    )


def _response_for_log(usage, content=None, stop_reason="end_turn"):
    return SimpleNamespace(
        usage=usage,
        content=content or [],
        stop_reason=stop_reason,
    )


class TestBuildInitialTurnLog:
    """The dict shape is the persistent contract — conversation_summary.json
    + the SSE pipeline_runner read these field names. Pin them."""

    def test_canonical_field_set(self):
        resp = _response_for_log(_usage())
        log = build_initial_turn_log(
            resp,
            turn=3,
            current_model="claude-opus-4-7",
            call_cost=0.0123,
            call_latency_ms=1234.5,
        )
        # Required fields the downstream readers depend on
        for field in (
            "turn", "stop_reason", "cost_usd", "model",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_create_tokens", "cache_hit_pct", "latency_ms",
            "text", "tool_calls", "tool_results", "iterations",
        ):
            assert field in log, f"missing field: {field}"

    def test_cost_rounded_to_4_decimal_places(self):
        resp = _response_for_log(_usage())
        log = build_initial_turn_log(
            resp,
            turn=0,
            current_model="m",
            call_cost=0.0123456789,
            call_latency_ms=0.0,
        )
        assert log["cost_usd"] == 0.0123  # round(.., 4)

    def test_cache_hit_pct_when_no_cache(self):
        # Pure miss: input=100, cache_read=0, cache_create=0 → 0%
        resp = _response_for_log(_usage(input_tokens=100))
        log = build_initial_turn_log(
            resp,
            turn=0,
            current_model="m",
            call_cost=0.0,
            call_latency_ms=0.0,
        )
        assert log["cache_hit_pct"] == 0.0

    def test_cache_hit_pct_typical(self):
        # input=100, cache_read=900, cache_create=0 → 900/(100+900+0) = 90.0%
        resp = _response_for_log(_usage(input_tokens=100, cache_read=900))
        log = build_initial_turn_log(
            resp,
            turn=0,
            current_model="m",
            call_cost=0.0,
            call_latency_ms=0.0,
        )
        assert log["cache_hit_pct"] == 90.0

    def test_iterations_captured(self):
        # When usage.iterations is populated (advisor path), each
        # entry's per-iteration fields surface in the log.
        iter_one = SimpleNamespace(
            type="message", model="claude-haiku-4-5-20251001",
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        resp = _response_for_log(_usage(iterations=[iter_one]))
        log = build_initial_turn_log(
            resp,
            turn=0,
            current_model="claude-opus-4-7",
            call_cost=0.0,
            call_latency_ms=0.0,
        )
        assert len(log["iterations"]) == 1
        assert log["iterations"][0]["type"] == "message"
        assert log["iterations"][0]["model"] == "claude-haiku-4-5-20251001"

    def test_iterations_empty_when_no_iterations(self):
        resp = _response_for_log(_usage(iterations=None))
        log = build_initial_turn_log(
            resp,
            turn=0,
            current_model="m",
            call_cost=0.0,
            call_latency_ms=0.0,
        )
        assert log["iterations"] == []

    def test_text_and_tool_lists_start_empty(self):
        # The dispatcher mutates these AFTER build_initial_turn_log
        # constructs them; ensure they start in the documented shape.
        resp = _response_for_log(_usage())
        log = build_initial_turn_log(
            resp,
            turn=0,
            current_model="m",
            call_cost=0.0,
            call_latency_ms=0.0,
        )
        assert log["text"] == ""
        assert log["tool_calls"] == []
        assert log["tool_results"] == []


class TestSummarizeToolCall:
    """One-line summary per tool kind — the operator-facing diagnostic
    surface. Cap at ~200 chars so the SSE payload stays bounded."""

    def test_web_fetch_uses_url(self):
        out = _summarize_tool_call({
            "tool": "web_fetch",
            "input": {"url": "https://api.example.com/v1/docs"},
        })
        assert out == {"tool": "web_fetch", "summary": "https://api.example.com/v1/docs"}

    def test_web_search_uses_query(self):
        out = _summarize_tool_call({
            "tool": "web_search",
            "input": {"query": "stripe webhooks idempotency"},
        })
        assert out == {"tool": "web_search", "summary": "stripe webhooks idempotency"}

    def test_write_file_uses_path_or_file_path(self):
        out_a = _summarize_tool_call({
            "tool": "write_file",
            "input": {"path": "harness.py"},
        })
        assert out_a["summary"] == "harness.py"
        out_b = _summarize_tool_call({
            "tool": "write_file",
            "input": {"file_path": "requirements.txt"},
        })
        assert out_b["summary"] == "requirements.txt"

    def test_run_code_uses_command_or_code(self):
        out_a = _summarize_tool_call({
            "tool": "run_code",
            "input": {"command": "pip install requests"},
        })
        assert out_a["summary"] == "pip install requests"
        out_b = _summarize_tool_call({
            "tool": "run_code",
            "input": {"code": "import requests; print(requests.__version__)"},
        })
        assert "requests" in out_b["summary"]

    def test_ask_research_uses_question(self):
        out = _summarize_tool_call({
            "tool": "ask_research",
            "input": {"question": "Why does ElevenLabs return 401?"},
        })
        assert out["summary"] == "Why does ElevenLabs return 401?"

    def test_summary_truncated_at_200(self):
        long_url = "https://example.com/" + "a" * 500
        out = _summarize_tool_call({
            "tool": "web_fetch",
            "input": {"url": long_url},
        })
        assert len(out["summary"]) <= 200

    def test_unknown_tool_falls_back_to_kv_pairs(self):
        out = _summarize_tool_call({
            "tool": "novel_tool",
            "input": {"alpha": "v1", "beta": "v2", "gamma": "v3"},
        })
        # Shows up to first 2 key/value pairs
        assert "alpha=v1" in out["summary"] or "beta=v2" in out["summary"]
        assert "gamma" not in out["summary"]  # third pair excluded

    def test_string_input_passes_through_truncated(self):
        out = _summarize_tool_call({"tool": "x", "input": "hello world"})
        assert out["summary"] == "hello world"

    def test_missing_input_returns_empty_summary(self):
        out = _summarize_tool_call({"tool": "x"})
        assert out["summary"] == ""

    def test_missing_tool_name_returns_question_mark(self):
        out = _summarize_tool_call({"input": {"url": "https://x.com"}})
        assert out["tool"] == "?"


class TestEmitBuildTurnProgress:
    """The SSE-event emitter — the operator-facing live build telemetry.
    No-op when no callback. Phase computed from boundary-state flags."""

    def test_no_callback_is_noop(self):
        # Should not raise; should not even introspect response when
        # progress_callback is None.
        emit_build_turn_progress(
            None,
            candidate_name="X",
            turn=0,
            max_turns=20,
            api_spec_written=False,
            smoke_ever_passed=False,
            current_model="m",
            call_cost=0.0,
            accumulated_cost=0.0,
            call_latency_ms=0.0,
            cache_read=0,
            cache_create=0,
            response=None,  # not touched when callback is None
            turn_log={"text": "", "tool_calls": []},
        )  # no exception

    def test_phase_researching_when_no_api_spec(self):
        events = []
        resp = SimpleNamespace(content=[], stop_reason="end_turn")
        emit_build_turn_progress(
            lambda evt, payload: events.append((evt, payload)),
            candidate_name="X",
            turn=0,
            max_turns=20,
            api_spec_written=False,
            smoke_ever_passed=False,
            current_model="m",
            call_cost=0.0,
            accumulated_cost=0.0,
            call_latency_ms=0.0,
            cache_read=0,
            cache_create=0,
            response=resp,
            turn_log={"text": "", "tool_calls": []},
        )
        assert events[0][0] == "build_turn"
        assert events[0][1]["phase"] == "researching"

    def test_phase_building_after_api_spec_written(self):
        events = []
        resp = SimpleNamespace(content=[], stop_reason="end_turn")
        emit_build_turn_progress(
            lambda evt, payload: events.append((evt, payload)),
            candidate_name="X",
            turn=0,
            max_turns=20,
            api_spec_written=True,
            smoke_ever_passed=False,
            current_model="m",
            call_cost=0.0,
            accumulated_cost=0.0,
            call_latency_ms=0.0,
            cache_read=0,
            cache_create=0,
            response=resp,
            turn_log={"text": "", "tool_calls": []},
        )
        assert events[0][1]["phase"] == "building"

    def test_phase_validating_after_smoke_passed(self):
        events = []
        resp = SimpleNamespace(content=[], stop_reason="end_turn")
        emit_build_turn_progress(
            lambda evt, payload: events.append((evt, payload)),
            candidate_name="X",
            turn=0,
            max_turns=20,
            api_spec_written=True,
            smoke_ever_passed=True,
            current_model="m",
            call_cost=0.0,
            accumulated_cost=0.0,
            call_latency_ms=0.0,
            cache_read=0,
            cache_create=0,
            response=resp,
            turn_log={"text": "", "tool_calls": []},
        )
        assert events[0][1]["phase"] == "validating"

    def test_payload_contract_carries_diagnostic_fields(self):
        """The reason this helper exists: rich per-turn diagnostics
        surfaced to the SSE consumer. Lock the field set."""
        events = []
        # tool_use block so tools_used isn't empty
        tool_use = SimpleNamespace(type="tool_use", name="web_fetch")
        resp = SimpleNamespace(content=[tool_use], stop_reason="end_turn")
        emit_build_turn_progress(
            lambda evt, payload: events.append((evt, payload)),
            candidate_name="MyCand",
            turn=4,
            max_turns=25,
            api_spec_written=False,
            smoke_ever_passed=False,
            current_model="claude-sonnet-4-6",
            call_cost=0.123,
            accumulated_cost=2.45,
            call_latency_ms=4321.7,
            cache_read=10000,
            cache_create=2000,
            response=resp,
            turn_log={
                "text": "this is what claude said",
                "tool_calls": [
                    {"tool": "web_fetch", "input": {"url": "https://x.com"}},
                ],
            },
        )
        evt, payload = events[0]
        assert evt == "build_turn"
        # All required fields present
        for field in (
            "candidate_name", "turn", "max_turns", "phase", "model",
            "cost_usd", "cumulative_cost_usd", "latency_ms",
            "cache_read_tokens", "cache_create_tokens", "tools_used",
            "tool_calls_detail", "text_preview", "stop_reason",
        ):
            assert field in payload, f"missing field: {field}"
        # Specific values
        assert payload["candidate_name"] == "MyCand"
        assert payload["turn"] == 5  # 1-indexed for the operator
        assert payload["model"] == "claude-sonnet-4-6"
        assert payload["cost_usd"] == 0.123
        assert payload["cumulative_cost_usd"] == 2.45
        assert payload["tools_used"] == ["web_fetch"]
        assert len(payload["tool_calls_detail"]) == 1
        assert payload["tool_calls_detail"][0]["summary"] == "https://x.com"

    def test_text_preview_truncated_at_300(self):
        events = []
        resp = SimpleNamespace(content=[], stop_reason="end_turn")
        emit_build_turn_progress(
            lambda evt, payload: events.append((evt, payload)),
            candidate_name="X",
            turn=0,
            max_turns=20,
            api_spec_written=False,
            smoke_ever_passed=False,
            current_model="m",
            call_cost=0.0,
            accumulated_cost=0.0,
            call_latency_ms=0.0,
            cache_read=0,
            cache_create=0,
            response=resp,
            turn_log={"text": "x" * 600, "tool_calls": []},
        )
        assert len(events[0][1]["text_preview"]) == 300

    def test_turn_is_1_indexed(self):
        """Operators read this as 'turn 5 of 25' — must be 1-indexed."""
        events = []
        resp = SimpleNamespace(content=[], stop_reason="end_turn")
        emit_build_turn_progress(
            lambda evt, payload: events.append((evt, payload)),
            candidate_name="X",
            turn=0,  # zero-indexed in the loop
            max_turns=20,
            api_spec_written=False,
            smoke_ever_passed=False,
            current_model="m",
            call_cost=0.0,
            accumulated_cost=0.0,
            call_latency_ms=0.0,
            cache_read=0,
            cache_create=0,
            response=resp,
            turn_log={"text": "", "tool_calls": []},
        )
        assert events[0][1]["turn"] == 1  # surfaced as 1-indexed
