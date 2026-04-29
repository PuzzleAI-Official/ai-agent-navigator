# ============================================================================
# Tests for puzzleeval.web_fetch_fallback (Phase 1: Cloudflare hardening)
# ============================================================================
# Run: ANTHROPIC_API_KEY=dummy python -m pytest tests/test_web_fetch_fallback.py -v
#
# Covers detection helpers, fallback-message construction, rate-limit backoff,
# and the integration with PipelineRun (auto-promote web_fetch_blocks into
# pipeline_summary.json).
#
# Anthropic SDK error objects are duck-typed via SimpleNamespace so we don't
# need real API responses — the helpers only inspect ``type``, ``content``,
# ``error_code``, ``tool_use_id``, ``id``, and ``input`` attributes.
# ============================================================================

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from puzzleeval import web_fetch_fallback as wff
from puzzleeval.pipeline import PipelineRun
from puzzleeval.schemas import (
    Agent1Input,
    Agent1Result,
    Constraints,
    SubTask,
    UserUnderstandingOutput,
)


# ============================================================================
# Helpers — build fake Anthropic response objects with specific block shapes
# ============================================================================

def _make_error_block(error_code: str, tool_use_id: str = "tu_1"):
    """Construct a duck-typed WebFetchToolResultBlock containing an error."""
    return SimpleNamespace(
        type="web_fetch_tool_result",
        tool_use_id=tool_use_id,
        content=SimpleNamespace(
            type="web_fetch_tool_result_error",
            error_code=error_code,
        ),
    )


def _make_success_block(tool_use_id: str = "tu_1", body: str = "API documentation body..."):
    """Construct a duck-typed WebFetchToolResultBlock containing real content.

    ``body`` is the page text the agent will see — defaults to a short stub,
    callers can pass realistic HTML to exercise the content classifier.
    """
    return SimpleNamespace(
        type="web_fetch_tool_result",
        tool_use_id=tool_use_id,
        content=SimpleNamespace(
            type="web_fetch_block",
            url="https://docs.example.com/api",
            content=SimpleNamespace(
                source=SimpleNamespace(data=body),
            ),
        ),
    )


def _make_server_tool_use_block(tool_use_id: str, url: str):
    """Construct a duck-typed server_tool_use block paired with a fetch result."""
    return SimpleNamespace(
        type="server_tool_use",
        id=tool_use_id,
        input={"url": url},
    )


def _make_response(*content_blocks):
    return SimpleNamespace(content=list(content_blocks))


# ============================================================================
# Detection
# ============================================================================

class TestErrorClassification:

    def test_blocking_codes_are_recoverable(self):
        for code in ("url_not_accessible", "too_many_requests", "unavailable"):
            assert wff.is_blocking_error(code), f"expected {code} to be blocking"

    def test_non_recoverable_codes_are_not_blocking(self):
        for code in ("invalid_tool_input", "url_too_long", "url_not_allowed",
                     "unsupported_content_type", "max_uses_exceeded"):
            assert not wff.is_blocking_error(code), f"expected {code} non-recoverable"


class TestExtractBlockedFetches:

    def test_returns_empty_list_when_no_fetch_results(self):
        # Response with only a text block — no web_fetch activity at all.
        response = _make_response(SimpleNamespace(type="text", text="hi"))
        assert wff.extract_blocked_fetches(response) == []

    def test_returns_empty_list_when_all_fetches_succeeded(self):
        # Successful fetch should not be classified as blocked.
        response = _make_response(_make_success_block())
        assert wff.extract_blocked_fetches(response) == []

    def test_detects_url_not_accessible_block(self):
        # Cloudflare/403/404/5xx all surface as url_not_accessible.
        response = _make_response(_make_error_block("url_not_accessible", "tu_99"))
        blocked = wff.extract_blocked_fetches(response)
        assert len(blocked) == 1
        assert blocked[0]["error_code"] == "url_not_accessible"
        assert blocked[0]["tool_use_id"] == "tu_99"
        # No matching server_tool_use → URL is "unknown".
        assert blocked[0]["url"] == "unknown"

    def test_finds_url_via_matching_server_tool_use(self):
        url = "https://api.cloudflare-protected.example/docs"
        response = _make_response(
            _make_server_tool_use_block("tu_match", url),
            _make_error_block("too_many_requests", "tu_match"),
        )
        blocked = wff.extract_blocked_fetches(response)
        assert blocked == [
            {"url": url, "error_code": "too_many_requests", "tool_use_id": "tu_match"},
        ]

    def test_returns_multiple_blocked_fetches(self):
        response = _make_response(
            _make_error_block("url_not_accessible", "tu_a"),
            _make_success_block("tu_b"),
            _make_error_block("too_many_requests", "tu_c"),
        )
        blocked = wff.extract_blocked_fetches(response)
        assert len(blocked) == 2
        codes = sorted(b["error_code"] for b in blocked)
        assert codes == ["too_many_requests", "url_not_accessible"]

    def test_handles_response_without_content_attribute(self):
        # Defensive — Anthropic responses always have .content but we shouldn't
        # crash on None for partial / corrupted objects either.
        assert wff.extract_blocked_fetches(SimpleNamespace()) == []


class TestCountAndDetect:

    def test_count_blocking_fetches_excludes_non_recoverable(self):
        # Mix of recoverable + non-recoverable; count should only see the 2 recoverable.
        blocked = [
            {"url": "u1", "error_code": "url_not_accessible", "tool_use_id": ""},
            {"url": "u2", "error_code": "invalid_tool_input", "tool_use_id": ""},
            {"url": "u3", "error_code": "too_many_requests", "tool_use_id": ""},
            {"url": "u4", "error_code": "max_uses_exceeded", "tool_use_id": ""},
        ]
        assert wff.count_blocking_fetches(blocked) == 2

    def test_has_rate_limit_error(self):
        assert wff.has_rate_limit_error([
            {"url": "u", "error_code": "too_many_requests", "tool_use_id": ""},
        ]) is True
        assert wff.has_rate_limit_error([
            {"url": "u", "error_code": "url_not_accessible", "tool_use_id": ""},
        ]) is False
        assert wff.has_rate_limit_error([]) is False


# ============================================================================
# Recovery — message construction + backoff
# ============================================================================

class TestBuildFallbackMessage:

    def test_returns_empty_when_only_non_recoverable(self):
        # Non-recoverable errors don't get fallback guidance; caller stays silent.
        msg = wff.build_fallback_message([
            {"url": "u", "error_code": "invalid_tool_input", "tool_use_id": ""},
        ])
        assert msg == ""

    def test_includes_search_alternatives_for_url_not_accessible(self):
        msg = wff.build_fallback_message([
            {"url": "https://docs.x.com/api", "error_code": "url_not_accessible",
             "tool_use_id": ""},
        ])
        # URL + error code shown; recovery strategy mentions web_search snippets.
        assert "https://docs.x.com/api" in msg
        assert "url_not_accessible" in msg
        assert "web_search" in msg
        assert "site:DOMAIN" in msg
        # GitHub SDK fallback recommended.
        assert "github" in msg.lower()
        # Archive fallback recommended.
        assert "web.archive.org" in msg

    def test_mentions_backoff_for_429(self):
        msg = wff.build_fallback_message([
            {"url": "https://api.x.com", "error_code": "too_many_requests",
             "tool_use_id": ""},
        ])
        assert "RATE LIMITED" in msg
        assert "429" in msg
        # Recommend pivoting to web_search rather than retrying same URL.
        assert "web_search" in msg


class TestRateLimitBackoff:

    def test_sleeps_when_429_present(self):
        slept = []
        result = wff.maybe_apply_rate_limit_backoff(
            [{"url": "u", "error_code": "too_many_requests", "tool_use_id": ""}],
            sleep=slept.append,
        )
        # Returned the same number of seconds it slept.
        assert result == wff.FETCH_RATE_LIMIT_BACKOFF_SECONDS
        assert slept == [wff.FETCH_RATE_LIMIT_BACKOFF_SECONDS]

    def test_does_not_sleep_when_no_rate_limit(self):
        slept = []
        result = wff.maybe_apply_rate_limit_backoff(
            [{"url": "u", "error_code": "url_not_accessible", "tool_use_id": ""}],
            sleep=slept.append,
        )
        assert result == 0
        assert slept == []

    def test_disabled_when_feature_flag_off(self, monkeypatch):
        # Toggling the flag at runtime simulates an operator disabling fallback.
        monkeypatch.setattr(wff, "ENABLE_FETCH_FALLBACK", False)
        slept = []
        result = wff.maybe_apply_rate_limit_backoff(
            [{"url": "u", "error_code": "too_many_requests", "tool_use_id": ""}],
            sleep=slept.append,
        )
        assert result == 0
        assert slept == []


class TestSummarizeBlocksForLog:

    def test_log_dict_includes_count_breakdown_and_urls(self):
        blocked = [
            {"url": "https://a.com/x", "error_code": "url_not_accessible", "tool_use_id": ""},
            {"url": "https://a.com/x", "error_code": "url_not_accessible", "tool_use_id": ""},
            {"url": "https://b.com/y", "error_code": "too_many_requests", "tool_use_id": ""},
            {"url": "unknown", "error_code": "invalid_tool_input", "tool_use_id": ""},
        ]
        summary = wff.summarize_blocks_for_log(blocked)
        assert summary["web_fetch_blocks_total"] == 4
        assert summary["web_fetch_blocks_recoverable"] == 3
        assert summary["web_fetch_blocks_by_code"] == {
            "url_not_accessible": 2,
            "too_many_requests": 1,
            "invalid_tool_input": 1,
        }
        # URL list is deduped, and "unknown" is filtered out.
        assert summary["web_fetch_blocked_urls"] == [
            "https://a.com/x",
            "https://b.com/y",
        ]


# ============================================================================
# Integration with PipelineRun — pipeline_summary.json surface
# ============================================================================

def _make_agent1_input() -> Agent1Input:
    return Agent1Input(
        user_text="test",
        trace_id="test-fetch-fallback-001",
    )


def _make_agent1_result() -> Agent1Result:
    return Agent1Result(
        is_clear=True,
        result=UserUnderstandingOutput(
            summary="x",
            sub_tasks=[
                SubTask(description="d", capability="c", search_keywords=["k"]),
            ],
            search_strategy="both",
            domain="dom",
            search_keywords=["k"],
            constraints=Constraints(),
            workflow_summary=None,
        ),
    )


class _StubResultWithBlocks:
    """Minimal stand-in for an agent result schema carrying web_fetch_blocks.

    PipelineRun calls ``output_data.model_dump_json(...)`` so we wrap a
    real Pydantic ``Agent1Result`` and attach a ``web_fetch_blocks``
    attribute via subclass for the auto-promotion test.
    """


class TestPipelineMetadataPromotion:

    def test_save_agent_result_promotes_web_fetch_blocks_into_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-pipe-meta-001", output_dir=tmpdir)
            result = _make_agent1_result()
            # Stash web_fetch_blocks on the result. Pydantic v2 ignores
            # unknown attributes during dump, but getattr() in pipeline.py
            # picks it up before serialization.
            object.__setattr__(result, "web_fetch_blocks", 7)
            run.save_agent_result(
                "agent_4",
                _make_agent1_input(),
                result,
                duration_ms=100,
                cost_usd=0.5,
            )
            assert run.agents[-1].metadata == {"web_fetch_blocks": 7}

    def test_save_agent_result_omits_metadata_when_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-pipe-meta-002", output_dir=tmpdir)
            result = _make_agent1_result()
            # Default 0 should not pollute metadata.
            object.__setattr__(result, "web_fetch_blocks", 0)
            run.save_agent_result(
                "agent_4",
                _make_agent1_input(),
                result,
                duration_ms=100,
                cost_usd=0.5,
            )
            assert run.agents[-1].metadata == {}

    def test_finalize_aggregates_metadata_to_run_level(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-pipe-meta-003", output_dir=tmpdir)
            for agent_name, blocks in (("agent_4", 3), ("agent_5", 5)):
                result = _make_agent1_result()
                object.__setattr__(result, "web_fetch_blocks", blocks)
                run.save_agent_result(
                    agent_name,
                    _make_agent1_input(),
                    result,
                    duration_ms=10,
                    cost_usd=0.1,
                )
            summary = run.finalize()
            assert summary["metadata"]["web_fetch_blocks"] == 8

            # On-disk JSON also contains the metadata block.
            on_disk = json.loads(
                (Path(tmpdir) / "test-pipe-meta-003" / "pipeline_summary.json")
                .read_text(encoding="utf-8")
            )
            assert on_disk["metadata"]["web_fetch_blocks"] == 8

    def test_finalize_omits_run_metadata_when_no_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run = PipelineRun("test-pipe-meta-004", output_dir=tmpdir)
            run.save_agent_result(
                "agent_1",
                _make_agent1_input(),
                _make_agent1_result(),
                duration_ms=10,
                cost_usd=0.1,
            )
            summary = run.finalize()
            assert "metadata" not in summary


# ============================================================================
# Phase 1.5 — content-quality assessment
# ============================================================================
# Two-stage classifier: stage 1 identifies USABLE pages by positive signals
# (endpoint patterns, auth examples, code blocks, prose with API keywords);
# stage 2 classifies the failure mode (script_rendered, auth_wall,
# not_found_soft, unknown_useless) so the recovery message can be specific.
# ============================================================================


class TestAssessContentQualityPositiveSignals:
    """Stage 1: any positive signal → usable, regardless of how page rendered."""

    def test_usable_via_endpoint_signature(self):
        # Most reliable signal — a documented HTTP method + path.
        verdict = wff.assess_content_quality(
            "<html><body><h1>API</h1><p>POST /v1/documents — create a document</p></body></html>"
        )
        assert verdict.usable is True
        assert verdict.category == "usable"
        assert "endpoint_signature" in verdict.signal

    def test_usable_via_auth_marker(self):
        # Auth header examples are a strong signal even without endpoint regex.
        verdict = wff.assess_content_quality(
            "<html><body>Send <code>Authorization: Bearer YOUR_KEY</code></body></html>"
        )
        assert verdict.usable is True
        assert "auth_header" in verdict.signal

    def test_usable_via_code_call_marker(self):
        # Python requests example proves the page documents an API.
        verdict = wff.assess_content_quality(
            "<html><body><pre><code>requests.post('/api/jobs', json={...})</code></pre></body></html>"
        )
        assert verdict.usable is True
        assert "code_call" in verdict.signal

    def test_usable_via_prose_with_api_keywords(self):
        # Long narrative docs without code examples still pass via prose floor.
        body = "<html><body><h1>Authentication Guide</h1>" + (
            "<p>This endpoint requires an authentication token sent in the request header. "
            "Each parameter is documented below. The response body returns the standard JSON payload. "
            "API reference: see related endpoints for rate limit details. </p>" * 4
        ) + "</body></html>"
        verdict = wff.assess_content_quality(body)
        assert verdict.usable is True
        assert verdict.signal == "prose_with_api_keywords"

    def test_stripe_style_rich_doc_passes(self):
        # Negative regression: a code-heavy page with both prose and code
        # samples MUST classify as usable — this is the false-positive case
        # that an over-eager "script-heavy" detector would miss.
        body = """
<html>
  <body>
    <h1>Charges API</h1>
    <h2>Create a charge</h2>
    <p>Use this endpoint to create a new charge for a customer.</p>
    <pre><code>curl https://api.stripe.com/v1/charges \\
  -u sk_test_xxx: \\
  -d amount=2000 \\
  -d currency=usd</code></pre>
    <pre><code>requests.post('https://api.stripe.com/v1/charges',
  auth=('sk_test_xxx', ''),
  data={'amount': 2000, 'currency': 'usd'})</code></pre>
    <h3>Parameters</h3>
    <p>amount (required) — integer cents</p>
    <p>currency (required) — three-letter ISO currency code</p>
    <script src="/analytics.js"></script>
  </body>
</html>
"""
        verdict = wff.assess_content_quality(body)
        assert verdict.usable is True


class TestAssessContentQualitySecondaryClassification:
    """Stage 2: when stage 1 fails, name the failure mode for the recovery message."""

    def test_classifies_spa_shell_via_next_data(self):
        # DocuClipper-style Next.js page: empty body, NEXT_DATA in script.
        body = """
<html>
  <body>
    <div id="__next"></div>
    <script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>
    <script src="/_next/static/chunks/main.js"></script>
  </body>
</html>
"""
        verdict = wff.assess_content_quality(body)
        assert verdict.usable is False
        assert verdict.category == "script_rendered"

    def test_classifies_spa_shell_via_react_root(self):
        # React app with data-reactroot attribute and no rendered content.
        body = '<html><body><div data-reactroot></div><script src="/app.js"></script></body></html>'
        verdict = wff.assess_content_quality(body)
        assert verdict.category == "script_rendered"

    def test_classifies_auth_wall(self):
        # Login wall — detect via login keywords + password input.
        body = """
<html>
  <body>
    <h1>Sign In</h1>
    <p>Please log in to continue.</p>
    <form><input type="password" name="password"></form>
  </body>
</html>
"""
        verdict = wff.assess_content_quality(body)
        assert verdict.usable is False
        assert verdict.category == "auth_wall"

    def test_classifies_soft_404(self):
        # Page returned 200 but body says "page not found" — common SPA pattern.
        body = "<html><body><h1>404</h1><p>This page does not exist.</p></body></html>"
        verdict = wff.assess_content_quality(body)
        assert verdict.usable is False
        assert verdict.category == "not_found_soft"

    def test_classifies_unknown_useless(self):
        # Empty 200 with no markers — fallback bucket; still triggers pivot.
        body = "<html><body><div>Welcome to our company.</div></body></html>"
        verdict = wff.assess_content_quality(body)
        assert verdict.usable is False
        assert verdict.category == "unknown_useless"


class TestExtractUnusablePages:

    def test_skips_error_blocks(self):
        # Error blocks belong to extract_blocked_fetches; double-counting them
        # would inflate web_fetch_blocks and trigger redundant guidance.
        response = _make_response(
            _make_error_block("url_not_accessible", "tu_err"),
            _make_success_block("tu_spa", body='<div id="__next"></div><script>__NEXT_DATA__</script>'),
        )
        unusable = wff.extract_unusable_pages(response)
        assert len(unusable) == 1
        assert unusable[0]["category"] == "script_rendered"
        assert unusable[0]["tool_use_id"] == "tu_spa"

    def test_skips_usable_pages(self):
        # A successful fetch with real API content should not be flagged.
        response = _make_response(
            _make_success_block("tu_ok", body="POST /v1/documents — upload file"),
        )
        assert wff.extract_unusable_pages(response) == []

    def test_finds_url_via_server_tool_use(self):
        # URL lookup chains the same tool_use_id mechanism as extract_blocked_fetches.
        url = "https://www.docuclipper.com/api-docs/"
        response = _make_response(
            _make_server_tool_use_block("tu_match", url),
            _make_success_block(
                "tu_match",
                body='<html><body><div id="__next"></div><script>__NEXT_DATA__={}</script></body></html>',
            ),
        )
        unusable = wff.extract_unusable_pages(response)
        assert len(unusable) == 1
        assert unusable[0]["url"] == url
        assert unusable[0]["category"] == "script_rendered"

    def test_handles_response_without_content(self):
        # Defensive — same shape check as extract_blocked_fetches.
        assert wff.extract_unusable_pages(SimpleNamespace()) == []


class TestBuildFallbackMessageWithContentVerdicts:
    """Recovery message should mention content-level failure modes alongside HTTP errors."""

    def test_includes_spa_pivot_guidance(self):
        msg = wff.build_fallback_message(
            blocked=[],
            unusable=[{"url": "https://x.com/api-docs", "category": "script_rendered", "tool_use_id": ""}],
        )
        assert "SPA SHELL" in msg
        # Should recommend search for deep URLs and OpenAPI spec.
        assert "openapi.json" in msg
        assert "site:DOMAIN" in msg

    def test_includes_auth_wall_guidance(self):
        msg = wff.build_fallback_message(
            blocked=[],
            unusable=[{"url": "https://x.com/docs", "category": "auth_wall", "tool_use_id": ""}],
        )
        assert "AUTH WALL" in msg
        # Should recommend GitHub SDK + archive snapshot.
        assert "github" in msg.lower()
        assert "archive.org" in msg

    def test_combines_http_block_and_content_failure(self):
        # Mixed turn: one Cloudflare block + one SPA shell. Both should appear
        # in the same unified guidance message under their own headers.
        msg = wff.build_fallback_message(
            blocked=[{"url": "https://x.com/v1", "error_code": "url_not_accessible", "tool_use_id": ""}],
            unusable=[{"url": "https://x.com/docs", "category": "script_rendered", "tool_use_id": ""}],
        )
        assert "BLOCKED" in msg
        assert "SPA SHELL" in msg
        assert "url_not_accessible" in msg
        assert "script_rendered" in msg

    def test_returns_empty_when_neither_actionable(self):
        # Non-recoverable HTTP error + no content failures → no message.
        msg = wff.build_fallback_message(
            blocked=[{"url": "u", "error_code": "invalid_tool_input", "tool_use_id": ""}],
            unusable=[],
        )
        assert msg == ""


class TestSummarizeBlocksWithUnusable:

    def test_includes_unusable_breakdown(self):
        summary = wff.summarize_blocks_for_log(
            blocked=[{"url": "https://a.com", "error_code": "url_not_accessible", "tool_use_id": ""}],
            unusable=[
                {"url": "https://b.com", "category": "script_rendered", "tool_use_id": ""},
                {"url": "https://c.com", "category": "auth_wall", "tool_use_id": ""},
            ],
        )
        assert summary["web_fetch_blocks_total"] == 1
        assert summary["web_fetch_unusable_total"] == 2
        assert summary["web_fetch_unusable_by_category"] == {
            "script_rendered": 1,
            "auth_wall": 1,
        }

    def test_omits_unusable_keys_when_none(self):
        # Backward compat: callers that only pass blocked still work.
        summary = wff.summarize_blocks_for_log(
            blocked=[{"url": "u", "error_code": "url_not_accessible", "tool_use_id": ""}],
        )
        assert "web_fetch_unusable_total" not in summary


class TestCountActionableProblems:

    def test_sums_blocking_and_unusable(self):
        total = wff.count_actionable_problems(
            blocked=[
                {"url": "u1", "error_code": "url_not_accessible", "tool_use_id": ""},
                {"url": "u2", "error_code": "invalid_tool_input", "tool_use_id": ""},  # not recoverable
            ],
            unusable=[
                {"url": "u3", "category": "script_rendered", "tool_use_id": ""},
                {"url": "u4", "category": "auth_wall", "tool_use_id": ""},
            ],
        )
        # 1 recoverable block + 2 unusable = 3 actionable.
        assert total == 3

    def test_handles_empty_inputs(self):
        assert wff.count_actionable_problems([], []) == 0
        assert wff.count_actionable_problems([]) == 0
