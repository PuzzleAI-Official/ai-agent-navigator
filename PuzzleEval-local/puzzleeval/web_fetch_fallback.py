# ============================================================================
# Web Fetch Fallback — Detection + Recovery for Useless Fetches
# ============================================================================
# A fetch can fail to give the agent useful content in two ways:
#
# (Phase 1) HTTP-level failure — the server returned an error code or
#   Anthropic's tool reported an internal limit:
#   - url_not_accessible: 403 / 404 / 5xx / Cloudflare WAF / IP blocking
#   - too_many_requests: 429 rate limit (per-domain throttle)
#   - unavailable: generic transient outage
#
# (Phase 1.5) Content-level failure — the server returned HTTP 200 but the
#   body is unusable:
#   - script_rendered: SPA shell (Next.js, React, Vue, Angular, Svelte)
#                      that needs JS execution to materialize content
#   - auth_wall: page demands login before showing the docs
#   - not_found_soft: returned 200 with a "page not found" body
#   - unknown_useless: page has no API signals (endpoints / auth / code blocks
#                      / API prose) and we can't classify why
#
# Anthropic does not let callers customize the user-agent web_fetch sends, and
# the tool does not execute JavaScript. So rotating headers and rendering SPAs
# are off the table. Instead we DETECT both classes of useless content in the
# response and PIVOT — guiding the model toward web_search snippets, GitHub
# SDK repos, alternate docs URLs, or archive.org.
#
# Design principle for the content classifier (Phase 1.5):
#   POSITIVE signals identify usable pages — endpoint patterns, auth examples,
#   code blocks, API prose. ANY positive signal → page is usable, regardless
#   of how it was rendered. Only when zero positive signals fire do we run a
#   secondary classification ("why is this empty?") to specialize the recovery
#   message. This avoids hardcoding framework markers as the primary detector;
#   they evolve too fast and miss adjacent failure modes (auth walls,
#   marketing pages) entirely.
#
# Used by:
#   - Agent 4 (screening.py) — counts both HTTP errors and content failures
#                              per candidate (single-shot, observability only)
#   - Agent 5 (implement_test_env.py) — counts both AND injects unified
#                                       fallback guidance into the next turn
#
# Toggle off via PUZZLEEVAL_ENABLE_FETCH_FALLBACK=0 (defaults on).
# ============================================================================

import re
import time
from dataclasses import dataclass
from typing import Any

from puzzleeval.config import (
    ENABLE_FETCH_FALLBACK,
    FETCH_RATE_LIMIT_BACKOFF_SECONDS,
)


# ---------------------------------------------------------------------------
# Anthropic web_fetch error code taxonomy
# ---------------------------------------------------------------------------
# Source: anthropic.types.WebFetchToolResultErrorCode (Literal of 8 codes).
# We classify each one as "blocking" (potentially recoverable via fallback) or
# "non-recoverable" (genuine config / API limit issues).
#
# Blocking codes are what the "Cloudflare blocking" todo item is really about:
# the URL CAN exist but our request is being rejected by a WAF / rate limiter.
# Non-recoverable codes mean retrying the same URL won't help (bad input,
# blocklisted host, exhausted budget).
# ---------------------------------------------------------------------------

BLOCKING_ERROR_CODES = frozenset({
    "url_not_accessible",   # 403 / 404 / 5xx / Cloudflare WAF
    "too_many_requests",    # 429 rate limit
    "unavailable",          # generic transient
})

NON_RECOVERABLE_ERROR_CODES = frozenset({
    "invalid_tool_input",
    "url_too_long",
    "url_not_allowed",
    "unsupported_content_type",
    "max_uses_exceeded",
})


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def is_blocking_error(error_code: str) -> bool:
    """Return True for error codes we can recover from via fallback strategies."""
    return error_code in BLOCKING_ERROR_CODES


def extract_blocked_fetches(response: Any) -> list[dict[str, str]]:
    """Scan ``response.content`` for failed web_fetch attempts.

    Inspects each block of type ``web_fetch_tool_result`` and checks whether
    its inner ``content`` is a ``WebFetchToolResultErrorBlock`` (type =
    ``web_fetch_tool_result_error``). For each blocked fetch we return a
    dict with the originating URL (looked up from the matching
    ``server_tool_use`` block), the error code, and the tool_use_id.

    Non-blocking errors (invalid_tool_input, url_too_long, etc.) are
    returned too, so callers can log or surface them — but the
    ``build_fallback_message`` and ``maybe_apply_rate_limit_backoff``
    helpers only act on the blocking subset.
    """
    blocked: list[dict[str, str]] = []
    content_blocks = getattr(response, "content", None) or []

    for block in content_blocks:
        if getattr(block, "type", "") != "web_fetch_tool_result":
            continue
        inner = getattr(block, "content", None)
        if inner is None:
            continue
        if getattr(inner, "type", "") != "web_fetch_tool_result_error":
            continue
        error_code = getattr(inner, "error_code", "unknown")
        tool_use_id = getattr(block, "tool_use_id", "")
        url = _find_url_for_tool_use_id(content_blocks, tool_use_id)
        blocked.append({
            "url": url or "unknown",
            "error_code": error_code,
            "tool_use_id": tool_use_id,
        })

    return blocked


def _find_url_for_tool_use_id(
    content_blocks: list[Any],
    tool_use_id: str,
) -> str | None:
    """Walk the response blocks to find the ``server_tool_use`` block that
    triggered this fetch, and return the URL it requested.

    Anthropic returns server tool calls as ``server_tool_use`` blocks
    immediately before their matching ``web_fetch_tool_result`` block.
    The ``tool_use_id`` ties them together.
    """
    if not tool_use_id:
        return None
    for block in content_blocks:
        if getattr(block, "type", "") != "server_tool_use":
            continue
        if getattr(block, "id", "") != tool_use_id:
            continue
        tool_input = getattr(block, "input", None) or {}
        return tool_input.get("url") if isinstance(tool_input, dict) else None
    return None


def count_blocking_fetches(blocked: list[dict[str, str]]) -> int:
    """Count fetches whose error code is in ``BLOCKING_ERROR_CODES``."""
    return sum(1 for b in blocked if is_blocking_error(b["error_code"]))


def has_rate_limit_error(blocked: list[dict[str, str]]) -> bool:
    return any(b["error_code"] == "too_many_requests" for b in blocked)


# ---------------------------------------------------------------------------
# Content-quality classifier (Phase 1.5)
# ---------------------------------------------------------------------------
# Run on successful web_fetch results to decide whether the returned HTML
# actually contains content the agent can use. Two-stage:
#   1. POSITIVE signals — does the page show signs of API content?
#   2. NEGATIVE classification — when stage 1 fails, why is this page empty?
#
# All deterministic, no LLM call. Runs on every successful fetch in roughly
# constant time (regex + substring scans on truncated 15K-token bodies).
# ---------------------------------------------------------------------------

# Stage 1 — positive signals (any one fires → page is usable)
# An endpoint signature like "POST /v1/documents" or "GET /api/jobs/{id}".
# Case-insensitive; covers the standard 6 HTTP verbs plus path that starts
# with "/" and uses URL-safe chars including {placeholders}.
_ENDPOINT_PATTERN = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH|HEAD)\s+/[\w/{}\-:.~]+",
    re.IGNORECASE,
)

# Auth header markers — substring matches, case-insensitive
_AUTH_HEADER_MARKERS: tuple[str, ...] = (
    "Authorization:",
    "X-API-Key:",
    "X-Auth-Token:",
    "X-Auth-Key:",
    "Bearer ",
    "OAuth ",
    "api_key=",
    "apiKey:",
)

# Code-call patterns showing how to invoke the API
_CODE_CALL_MARKERS: tuple[str, ...] = (
    "curl ",
    "curl -X",
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.delete",
    "requests.patch",
    "fetch(",
    "axios.",
    "http.client",
    "HttpClient",
    "RestClient",
    "$.ajax",
    "urllib.request",
)

# Keywords whose presence in a >=500-char prose body suggests API documentation
_API_PROSE_KEYWORDS: tuple[str, ...] = (
    "endpoint", "endpoints", "request body", "response body",
    "parameter", "parameters", "authentication", "authorization",
    "rate limit", "rate-limit", "api reference", "api key",
    "request format", "response format",
)

# Stage 2 — secondary classification when no positive signal fires.
# Markers covering the major SPA frameworks circa 2026. Order matters only
# for which name we pick if multiple match — recovery is the same.
_SPA_MARKERS: tuple[str, ...] = (
    "__NEXT_DATA__",
    "NEXT_REDIRECT",
    "self.__next_f",
    "__NUXT__",
    "data-reactroot",
    "data-react-root",
    'ng-version="',
    "ng-app=",
    "data-sveltekit",
    "<router-view",
    "data-vue-meta",
)

# Auth-wall markers — page is gating access via login
_AUTH_WALL_MARKERS: tuple[str, ...] = (
    "sign in to view",
    "log in to continue",
    "log in to view",
    "please log in",
    "you must be logged in",
    "login required",
    'type="password"',
    'name="password"',
)

# Soft-404 markers — page returned 200 but the content says "not found"
_NOT_FOUND_MARKERS: tuple[str, ...] = (
    "page not found",
    "404 not found",
    "couldn't find",
    "we could not find",
    "this page does not exist",
    "the page you requested",
)

# Tag stripping for prose-length floor.
# Drop <script> and <style> blocks ENTIRELY (content + tags) since their
# bodies inflate raw character counts without contributing readable prose.
# Then strip remaining tags as plain markup.
_SCRIPT_BLOCK_PATTERN = re.compile(
    r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE,
)
_STYLE_BLOCK_PATTERN = re.compile(
    r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE,
)
_TAG_PATTERN = re.compile(r"<[^>]+>")


def _strip_to_prose(text: str) -> str:
    """Remove <script>/<style> blocks and remaining HTML tags.

    Returns the readable text body with whitespace collapsed. Used to
    enforce the prose-length floor in stage 1 and the body-length check
    in the soft-404 secondary classification.
    """
    text = _SCRIPT_BLOCK_PATTERN.sub(" ", text)
    text = _STYLE_BLOCK_PATTERN.sub(" ", text)
    text = _TAG_PATTERN.sub(" ", text)
    return " ".join(text.split())


@dataclass
class ContentVerdict:
    """Outcome of content-quality assessment for a single fetched page.

    ``usable`` is the only field callers must check for the proceed/pivot
    decision. ``category`` and ``signal`` exist for telemetry and for
    parameterizing the fallback guidance message.
    """

    usable: bool
    category: str           # "usable" or "script_rendered" / "auth_wall" / etc.
    signal: str = ""        # Which positive or negative marker fired


# Categories that count as "page returned but content unusable" — the
# pivot-on-content-failure case Phase 1.5 hardens against.
UNUSABLE_CATEGORIES: frozenset[str] = frozenset({
    "script_rendered",
    "auth_wall",
    "not_found_soft",
    "unknown_useless",
})


def _has_usable_content(text: str) -> tuple[bool, str]:
    """Stage 1 — return ``(True, signal)`` on the FIRST positive match.

    Order is chosen for cheapness, not importance: regex matches and
    substring scans run in roughly constant time, so the early-out keeps
    the classifier fast on usable pages (the common case).
    """
    if _ENDPOINT_PATTERN.search(text):
        return True, "endpoint_signature"

    lower = text.lower()
    for marker in _AUTH_HEADER_MARKERS:
        if marker.lower() in lower:
            return True, f"auth_header:{marker.strip(': ')}"

    for marker in _CODE_CALL_MARKERS:
        if marker.lower() in lower:
            return True, f"code_call:{marker.strip()}"

    # Prose floor — ≥500 chars of readable body that mentions API concepts.
    # Catches well-written narrative docs that don't include code samples
    # in the truncated 15K-token window we received.
    prose = _strip_to_prose(text)
    if len(prose) >= 500:
        prose_lower = prose.lower()
        if any(kw in prose_lower for kw in _API_PROSE_KEYWORDS):
            return True, "prose_with_api_keywords"

    return False, ""


def _classify_useless_page(text: str) -> str:
    """Stage 2 — name the failure mode for use in the recovery message.

    Order matters: SPA detection is checked first because SPA pages can
    contain "log in" widget markup as decoration even though the actual
    failure is JS-rendering, not auth-walling. Soft-404 is last and
    requires a SHORT prose body to avoid false-positives on pages that
    legitimately discuss "page not found" error responses.
    """
    for marker in _SPA_MARKERS:
        if marker in text:
            return "script_rendered"

    lower = text.lower()
    if any(m in lower for m in _AUTH_WALL_MARKERS):
        return "auth_wall"

    prose = _strip_to_prose(text)
    if len(prose) < 500 and any(m in prose.lower() for m in _NOT_FOUND_MARKERS):
        return "not_found_soft"

    return "unknown_useless"


def assess_content_quality(text: str) -> ContentVerdict:
    """Decide whether fetched HTML contains content the agent can use.

    See module docstring for the design rationale. Two-stage:
      1. POSITIVE signals → ``usable=True`` regardless of how the page
         was rendered. Stripe, Twilio, Mindee, every server-rendered or
         statically-generated doc page passes here.
      2. SECONDARY classification → name the failure mode so the recovery
         message can be specific (SPA shell, auth wall, soft 404, etc.).
    """
    is_usable, signal = _has_usable_content(text)
    if is_usable:
        return ContentVerdict(usable=True, category="usable", signal=signal)
    category = _classify_useless_page(text)
    return ContentVerdict(usable=False, category=category, signal="")


def extract_unusable_pages(response: Any) -> list[dict[str, str]]:
    """Scan a response for SUCCESSFUL web_fetch results whose body is unusable.

    Complementary to ``extract_blocked_fetches``. That function handles
    HTTP-level errors (the fetch itself reported failure). This one handles
    content-level failures (the fetch returned 200 but the page is empty,
    SPA-rendered, login-walled, or a soft 404). Together they cover the
    full surface of "fetches that didn't help the agent."

    Each entry is shaped ``{url, category, tool_use_id}`` so the caller can
    aggregate counts, log details, and build the unified fallback message.
    Error blocks are skipped (avoids double-counting them).
    """
    unusable: list[dict[str, str]] = []
    content_blocks = getattr(response, "content", None) or []

    for block in content_blocks:
        if getattr(block, "type", "") != "web_fetch_tool_result":
            continue
        inner = getattr(block, "content", None)
        if inner is None:
            continue
        # Skip error blocks — those are extract_blocked_fetches' territory.
        if getattr(inner, "type", "") == "web_fetch_tool_result_error":
            continue

        # Successful fetch — pull the page text from the document source.
        page_text = _safe_extract_page_text(inner)
        if page_text is None:
            continue

        verdict = assess_content_quality(page_text)
        if verdict.usable:
            continue

        tool_use_id = getattr(block, "tool_use_id", "")
        url = _find_url_for_tool_use_id(content_blocks, tool_use_id)
        unusable.append({
            "url": url or "unknown",
            "category": verdict.category,
            "tool_use_id": tool_use_id,
        })

    return unusable


def _safe_extract_page_text(inner: Any) -> str | None:
    """Pull the page body text out of a WebFetchBlock without raising.

    Path: ``inner.content.source.data`` per Anthropic's beta types. Several
    AttributeError paths exist (older SDK, partial blocks, missing source) —
    return None and let the caller skip rather than crash the agent loop.
    """
    try:
        return inner.content.source.data
    except AttributeError:
        return None


# ---------------------------------------------------------------------------
# Recovery — fallback guidance and backoff
# ---------------------------------------------------------------------------

def build_fallback_message(
    blocked: list[dict[str, str]],
    unusable: list[dict[str, str]] | None = None,
) -> str:
    """Construct a user message guiding the model away from useless fetches.

    Handles two failure surfaces:
      * ``blocked`` — HTTP-level failures from ``extract_blocked_fetches``
        (Phase 1: 403 / 429 / unavailable).
      * ``unusable`` — content-level failures from ``extract_unusable_pages``
        (Phase 1.5: SPA shells, auth walls, soft 404s, unknown empty pages).

    Both kinds get the same recovery toolkit (web_search snippets, GitHub
    SDK repos, alternate subdomains, archive.org) but with category-specific
    framing so the model knows WHY the page was useless and what to try next.

    Returns an empty string if neither input contains anything actionable
    (so callers can ``if msg: messages.append(...)`` cleanly).
    """
    unusable = unusable or []
    recoverable_blocks = [b for b in blocked if is_blocking_error(b["error_code"])]
    actionable_unusable = [
        u for u in unusable if u["category"] in UNUSABLE_CATEGORIES
    ]

    if not recoverable_blocks and not actionable_unusable:
        return ""

    lines = ["## Web Fetch Recovery Guidance", ""]

    if recoverable_blocks:
        lines.append("The following fetches failed at the HTTP layer:")
        for entry in recoverable_blocks:
            lines.append(f"  - {entry['url']} → {entry['error_code']}")
        lines.append("")

    if actionable_unusable:
        lines.append(
            "The following fetches returned HTTP 200 but the body was unusable:"
        )
        for entry in actionable_unusable:
            lines.append(f"  - {entry['url']} → {entry['category']}")
        lines.append("")

    lines.append("Recovery strategies (try in order):")

    if has_rate_limit_error(recoverable_blocks):
        lines.append(
            "1. RATE LIMITED (429): wait before fetching this domain again. "
            "Switch to web_search for the next attempt — it does not share "
            "the per-domain fetch rate limit window."
        )

    if any(b["error_code"] == "url_not_accessible" for b in recoverable_blocks):
        lines.append(
            "2. BLOCKED (likely Cloudflare/WAF): the URL is unreachable to "
            "web_fetch. Do NOT retry the same URL. Instead try, in order:"
        )
        lines.append("   a) web_search 'site:DOMAIN TOPIC' (search snippets bypass WAFs)")
        lines.append("   b) web_search 'github SERVICE python SDK' for an SDK repo")
        lines.append("   c) Try docs.SERVICE.com or developer.SERVICE.com")
        lines.append("   d) web_fetch 'https://web.archive.org/web/2026/URL' for a snapshot")

    if any(b["error_code"] == "unavailable" for b in recoverable_blocks):
        lines.append(
            "3. TRANSIENT UNAVAILABLE: the host returned a generic failure. "
            "Retry once with a different path; if still failing, pivot to "
            "web_search for the same information."
        )

    if any(u["category"] == "script_rendered" for u in actionable_unusable):
        lines.append(
            "4. SPA SHELL: the page is rendered client-side by JavaScript "
            "(Next.js / React / Vue / Angular). web_fetch cannot execute JS, "
            "so the body you got was a script bundle, not the docs. Pivot to:"
        )
        lines.append("   a) web_search 'site:DOMAIN docs OR api OR developer' for deep URLs that may be statically generated")
        lines.append("   b) web_search 'site:DOMAIN openapi.json OR swagger.json' for the spec file")
        lines.append("   c) web_search 'github SERVICE SDK' — SDK source code reveals the API surface")

    if any(u["category"] == "auth_wall" for u in actionable_unusable):
        lines.append(
            "5. AUTH WALL: the docs require login before content is shown. "
            "web_fetch cannot authenticate. Pivot to:"
        )
        lines.append("   a) web_search 'site:DOMAIN' — Google may have indexed the content before the wall")
        lines.append("   b) web_search 'github SERVICE python SDK' — third-party code reveals the API shape")
        lines.append("   c) web_fetch 'https://web.archive.org/web/2026/URL' for a pre-wall snapshot")

    if any(u["category"] == "not_found_soft" for u in actionable_unusable):
        lines.append(
            "6. SOFT 404: the URL returned 200 but the body says 'page not "
            "found'. The path is probably wrong. Pivot to:"
        )
        lines.append("   a) web_search 'site:DOMAIN <topic>' to find the correct path")
        lines.append("   b) Inspect any sitemap.xml or robots.txt for valid doc paths")

    if any(u["category"] == "unknown_useless" for u in actionable_unusable):
        lines.append(
            "7. EMPTY/UNCLASSIFIED: the page returned 200 but contains no "
            "API signals (no endpoints, auth examples, or code blocks). It "
            "may be a marketing page or wrong URL. Pivot to web_search "
            "'site:DOMAIN api documentation' for the actual docs URL."
        )

    lines.append("")
    lines.append(
        "Pivot to an alternative source. Repeating the exact same web_fetch "
        "call is wasted budget."
    )
    return "\n".join(lines)


def maybe_apply_rate_limit_backoff(
    blocked: list[dict[str, str]],
    sleep: Any = time.sleep,
) -> int:
    """Sleep for ``FETCH_RATE_LIMIT_BACKOFF_SECONDS`` if a 429 was detected.

    Returns the number of seconds slept (0 if no rate limit, or if the
    feature flag is off). The ``sleep`` parameter is exposed so tests can
    inject a no-op stub.
    """
    if not ENABLE_FETCH_FALLBACK:
        return 0
    if not has_rate_limit_error(blocked):
        return 0
    sleep(FETCH_RATE_LIMIT_BACKOFF_SECONDS)
    return FETCH_RATE_LIMIT_BACKOFF_SECONDS


# ---------------------------------------------------------------------------
# Logging payload
# ---------------------------------------------------------------------------

def summarize_blocks_for_log(
    blocked: list[dict[str, str]],
    unusable: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Compact dict suitable for ``logger.info(..., extra={...})``.

    Combines HTTP-level blocks (Phase 1) and content-level unusable pages
    (Phase 1.5) into one telemetry payload. Includes per-error-code and
    per-unusable-category breakdowns plus the unique URLs (capped at 5
    each to keep log lines bounded).
    """
    unusable = unusable or []
    by_code: dict[str, int] = {}
    blocked_urls: list[str] = []
    for entry in blocked:
        by_code[entry["error_code"]] = by_code.get(entry["error_code"], 0) + 1
        if entry["url"] != "unknown" and entry["url"] not in blocked_urls:
            blocked_urls.append(entry["url"])

    by_category: dict[str, int] = {}
    unusable_urls: list[str] = []
    for entry in unusable:
        by_category[entry["category"]] = by_category.get(entry["category"], 0) + 1
        if entry["url"] != "unknown" and entry["url"] not in unusable_urls:
            unusable_urls.append(entry["url"])

    summary: dict[str, Any] = {
        "web_fetch_blocks_total": len(blocked),
        "web_fetch_blocks_recoverable": count_blocking_fetches(blocked),
        "web_fetch_blocks_by_code": by_code,
        "web_fetch_blocked_urls": blocked_urls[:5],
    }
    if unusable:
        summary.update({
            "web_fetch_unusable_total": len(unusable),
            "web_fetch_unusable_by_category": by_category,
            "web_fetch_unusable_urls": unusable_urls[:5],
        })
    return summary


def count_actionable_problems(
    blocked: list[dict[str, str]],
    unusable: list[dict[str, str]] | None = None,
) -> int:
    """Total count of fetches that produced no usable content this turn.

    Combines HTTP-level recoverable blocks (Phase 1) and content-level
    unusable pages (Phase 1.5). This is the number Agent 4/5 sum into the
    ``web_fetch_blocks`` field on their result schemas.
    """
    unusable = unusable or []
    actionable_unusable = sum(
        1 for u in unusable if u["category"] in UNUSABLE_CATEGORIES
    )
    return count_blocking_fetches(blocked) + actionable_unusable
