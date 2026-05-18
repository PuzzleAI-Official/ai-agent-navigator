"""Canonical domain fixture for conversational Agent 5 builds.

Voice/chat evaluations often need concrete business facts (hours, prices,
service areas, menus, policies). When those facts are scattered across test
cases, live tests, and rubrics, the system can accidentally grade against
contradictory assumptions. This module derives one visible fixture from the
upstream Agent 1/3 contract so Agent 5 can cite a single source of truth.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any


BUSINESS_FIXTURE_FILENAME = "business_fixture.json"

_FACT_KEYWORDS = (
    "price", "pricing", "$", "menu", "hours", "open", "close",
    "service area", "location", "policy", "appointment", "booking",
    "pickup", "delivery", "quote",
)

_NEGATIVE_CONTEXT_MARKERS = (
    "unavailable",
    "not available",
    "sold out",
    "off menu",
    "off-menu",
    "not on the menu",
    "cannot",
    "can't",
    "do not",
    "decline",
    "refuse",
    "closed",
)

_REQUESTED_ITEM_PATTERNS = (
    r"\b(?:order|get|buy|add|want|like|take|purchase)\s+(?P<item>[a-z][a-z '&-]{2,48})",
    r"\b(?:with|add)\s+(?P<item>[a-z][a-z '&-]{2,36})",
    r"\b(?:price|cost)\s+(?:of|for)\s+(?P<item>[a-z][a-z '&-]{2,36})",
)

_ITEM_PREFIX_WORDS = {
    "a", "an", "the", "one", "two", "three", "large", "small", "medium",
    "hot", "iced", "extra", "regular", "please", "some", "my", "me",
}

_ITEM_STOP_PHRASES = (
    " with ", " and ", " for ", " at ", " on ", " by ", " tomorrow", " today", " pickup",
    " delivery", " please", " and then", " with a ",
)


def _looks_fact_sensitive(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in _FACT_KEYWORDS)


def _has_concrete_fact_value(text: str) -> bool:
    """Return True when prose contains actual domain facts, not just needs.

    "Ask about menu and pricing" is fact-sensitive but not a fixture.
    "Latte is $4.50" or "open 7am-5pm" is a reusable fact source.
    """
    lower = text.lower()
    return bool(
        re.search(r"\$\s*\d", text)
        or re.search(r"\b\d{1,2}\s*(am|pm)\b", lower)
        or re.search(r"\b\d{1,2}\s*[:\-]\s*\d{2}\b", lower)
        or re.search(r"\b(open|closed|hours?)\b.*\b\d", lower)
        or re.search(r"\b(menu|includes|serves|offers)\b.*\b\$\s*\d", lower)
        or re.search(r"\b(service area|located|location)\b.*\b[A-Z][a-z]+", text)
    )


def _compact_lines(text: str, *, limit: int = 12) -> list[str]:
    lines: list[str] = []
    for raw in re.split(r"[\n;]", text or ""):
        item = " ".join(raw.strip().split())
        if item:
            lines.append(item[:300])
        if len(lines) >= limit:
            break
    return lines


def _normalize_item_phrase(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9 '&-]+", " ", value or "").lower()
    for stop in _ITEM_STOP_PHRASES:
        idx = text.find(stop)
        if idx >= 0:
            text = text[:idx]
    words = [w for w in text.split() if w]
    while words and words[0] in _ITEM_PREFIX_WORDS:
        words.pop(0)
    return " ".join(words).strip()


def _catalog_items_from_fixture(fixture: dict[str, Any]) -> set[str]:
    """Extract catalog/menu items from canonical fixture facts.

    The parser is intentionally conservative. It only treats phrases adjacent
    to prices as catalog entries, which avoids turning arbitrary prose into a
    brittle ontology.
    """

    facts = "\n".join(str(x) for x in fixture.get("canonical_facts") or [])
    items: set[str] = set()
    for match in re.finditer(
        r"(?P<item>[A-Za-z][A-Za-z '&-]{1,48})\s*\$\s*\d+(?:\.\d+)?",
        facts,
    ):
        raw = match.group("item")
        raw = re.split(r"[,;:]", raw)[-1]
        raw = re.split(r"\b(?:includes|include|offers|serves|menu)\b", raw, flags=re.I)[-1]
        item = _normalize_item_phrase(raw)
        if item and len(item) <= 48:
            items.add(item)
    return items


def _requested_items_from_text(text: str) -> set[str]:
    lower = (text or "").lower()
    requested: set[str] = set()
    for pattern in _REQUESTED_ITEM_PATTERNS:
        for match in re.finditer(pattern, lower):
            phrase = _normalize_item_phrase(match.group("item"))
            if phrase and len(phrase.split()) <= 4:
                requested.add(phrase)
    return requested


def _is_negative_context(text: str, item: str) -> bool:
    lower = (text or "").lower()
    idx = lower.find(item)
    if idx < 0:
        return False
    window = lower[max(0, idx - 80): idx + len(item) + 80]
    return any(marker in window for marker in _NEGATIVE_CONTEXT_MARKERS)


def validate_text_against_business_fixture(
    text: str,
    fixture: dict[str, Any] | None,
    *,
    source_label: str = "text",
) -> list[str]:
    """Return fixture-consistency issues for generated tests/live tests.

    This is not a semantic judge. It catches high-confidence drift between a
    canonical fixture and generated prompts, especially invented menu items or
    hours that caused recent voice runs to grade against contradictory facts.
    """

    if not fixture:
        return []
    if fixture.get("synthetic"):
        if _has_concrete_fact_value(text):
            return [
                f"{source_label} introduces exact menu/pricing/hours/policy facts, "
                "but business_fixture.json is a synthetic gap marker. Ask for "
                "clarification or test refusal/uncertainty instead of exact facts."
            ]
        return []
    lower = (text or "").lower()
    facts_text = "\n".join(str(x) for x in fixture.get("canonical_facts") or []).lower()
    issues: list[str] = []

    catalog_items = _catalog_items_from_fixture(fixture)
    if catalog_items:
        requested = _requested_items_from_text(lower)
        for item in sorted(requested):
            if item in catalog_items:
                continue
            if any(item == allowed or item.endswith(f" {allowed}") for allowed in catalog_items):
                # "large latte" invents a size/add-on when only "latte" exists.
                issues.append(
                    f"{source_label} references '{item}', but the fixture only lists "
                    f"{', '.join(sorted(catalog_items))}."
                )
                continue
            if _is_negative_context(lower, item):
                continue
            issues.append(
                f"{source_label} references catalog item/add-on '{item}' that is absent "
                "from business_fixture.json."
            )

    if "closed sunday" in facts_text or "closed sundays" in facts_text:
        if re.search(r"\b(open|hours?|available)\b[^.\n]{0,80}\bdaily\b", lower):
            issues.append(
                f"{source_label} says the business is open/available daily, "
                "but business_fixture.json says it is closed Sundays."
            )
        for match in re.finditer(r"\bsunday\b", lower):
            window = lower[max(0, match.start() - 80): match.end() + 80]
            if not any(marker in window for marker in ("closed", "unavailable", "not available", "decline", "refuse")):
                issues.append(
                    f"{source_label} uses Sunday as an available service/pickup time, "
                    "but business_fixture.json says Sundays are closed."
                )
                break

    fixture_times = _normalized_times(facts_text)
    live_times = _normalized_times(lower)
    extra_times = live_times - fixture_times
    if fixture_times and extra_times and re.search(r"\b(hours?|open|close|available)\b", lower):
        issues.append(
            f"{source_label} mentions hours/times not present in business_fixture.json: "
            + ", ".join(sorted(extra_times))
        )

    return issues


def _normalized_times(text: str) -> set[str]:
    """Normalize simple AM/PM times for fixture comparisons.

    This is evidence normalization, not semantic judgment: ``7:00 AM``,
    ``7am``, and ``7 AM`` become the same token.
    """

    out: set[str] = set()
    for match in re.finditer(r"\b(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<ampm>am|pm)\b", text or "", re.I):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        ampm = match.group("ampm").lower()
        out.add(f"{hour}:{minute:02d}{ampm}")
    return out


def validate_structured_claims_against_business_fixture(
    claims: list[dict[str, Any]] | list[str],
    fixture: dict[str, Any] | None,
    *,
    source_label: str = "structured evidence",
) -> list[str]:
    """Validate explicit test/runtime claims against the business fixture.

    Callers should pass actual utterances, assertions, expected outputs, or
    runtime transcripts. Do not pass raw source files or comments here; broad
    source-text scanning is too brittle for a semantic gate.
    """

    texts: list[str] = []
    for claim in claims or []:
        if isinstance(claim, dict):
            for key in ("utterance", "text", "expected", "assertion", "transcript", "claim"):
                value = claim.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
        elif isinstance(claim, str) and claim.strip():
            texts.append(claim)
    if not texts:
        return []
    return validate_text_against_business_fixture(
        "\n".join(texts),
        fixture,
        source_label=source_label,
    )


def load_business_fixture(sandbox_dir: Path) -> dict[str, Any] | None:
    path = sandbox_dir / "_agent_state" / BUSINESS_FIXTURE_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def synthesize_business_fixture(input_data: Any) -> dict[str, Any] | None:
    """Return a canonical fixture dict, or None when not needed.

    The function is deterministic and intentionally conservative. It does not
    invent detailed menus/prices; if upstream Agent 1 already generated
    conversational instructions with concrete facts, those become the source.
    If tests require fact-sensitive behavior but upstream facts are absent, the
    fixture marks that gap explicitly so later validators can avoid demanding
    exact prices/hours from nowhere.
    """
    explicit = getattr(input_data, "business_fixture", None)
    if isinstance(explicit, dict) and explicit:
        return explicit
    test_cases_obj = getattr(input_data, "test_cases", None)
    explicit = getattr(test_cases_obj, "business_fixture", None)
    if isinstance(explicit, dict) and explicit:
        return explicit

    user = getattr(input_data, "user_understanding", None)
    if user is None:
        return None

    candidate_texts: list[str] = [
        str(getattr(user, "summary", "") or ""),
        str(getattr(user, "domain", "") or ""),
    ]
    test_plan = getattr(user, "test_plan", None)
    if test_plan is not None:
        for spec in getattr(test_plan, "scope_specs", []) or getattr(test_plan, "scopes", []) or []:
            for attr in ("agent_instructions", "sample_input", "sample_output", "input_description", "expected_output_description"):
                value = getattr(spec, attr, None)
                if value:
                    candidate_texts.append(str(value))

    test_cases = getattr(getattr(input_data, "test_cases", None), "test_cases", []) or []
    for tc in test_cases:
        for attr in ("scenario", "input_data", "expected_output", "rubric"):
            value = getattr(tc, attr, None)
            if value:
                candidate_texts.append(str(value))
        ctx = getattr(tc, "input_context", None)
        if isinstance(ctx, dict):
            for key in ("instructions", "system_prompt", "business_context"):
                if ctx.get(key):
                    candidate_texts.append(str(ctx[key]))

    joined = "\n".join(candidate_texts)
    if not _looks_fact_sensitive(joined):
        return None

    instruction_sources = [
        text for text in candidate_texts
        if _looks_fact_sensitive(text)
        and _has_concrete_fact_value(text)
        and len(text.strip()) > 20
    ]
    source = instruction_sources[0] if instruction_sources else joined
    synthetic = not bool(instruction_sources)

    return {
        "schema_version": 1,
        "source": "user_or_agent1_context" if not synthetic else "synthetic_gap_marker",
        "synthetic": synthetic,
        "domain": str(getattr(user, "domain", "") or "unknown"),
        "summary": str(getattr(user, "summary", "") or "")[:500],
        "canonical_facts": _compact_lines(source),
        "notes": [
            (
                "Concrete business facts were found upstream and should be reused "
                "by Agent 3 tests, Agent 5 live tests, rubrics, and judges."
                if not synthetic else
                "Fact-sensitive tests were requested, but no concrete facts were "
                "available. Do not demand exact prices/hours/menu totals unless a "
                "later fixture supplies them."
            )
        ],
    }


def stage_business_fixture(sandbox_dir: Path, input_data: Any) -> dict[str, Any] | None:
    fixture = synthesize_business_fixture(input_data)
    if fixture is None:
        return None
    state_dir = sandbox_dir / "_agent_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / BUSINESS_FIXTURE_FILENAME).write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return fixture


def synthesize_business_fixture_from_user_understanding(user_understanding: Any) -> dict[str, Any] | None:
    """Generate the upstream fixture before Agent 3 test generation."""

    return synthesize_business_fixture(
        SimpleNamespace(
            user_understanding=user_understanding,
            test_cases=SimpleNamespace(test_cases=[]),
        )
    )


__all__ = [
    "BUSINESS_FIXTURE_FILENAME",
    "load_business_fixture",
    "synthesize_business_fixture",
    "stage_business_fixture",
    "validate_text_against_business_fixture",
    "validate_structured_claims_against_business_fixture",
    "synthesize_business_fixture_from_user_understanding",
]
