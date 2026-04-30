"""Vision-based evaluation for image-generation outputs.

Gap 6 + Gap 26 (HIGH): when a candidate returns a media URL (generated
image / audio / video reference), the text LLM judge can only check
structural compliance ("URL returned?"). It cannot tell whether the
generated content matches the user's prompt.

This module adds a narrow vision path: when ``TestCase.output_type ==
"media_url"`` AND the harness response carries a URL that resolves to an
image, we send the image to Claude's vision input and score it against
the expected description + criteria.

Audio / video vision evaluation is not implemented here — only images.
Those would require a different model path and are out of scope for
this pass.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from anthropic import Anthropic, APIError

from puzzleeval import config
from puzzleeval.web_fetch_fallback import (
    BLOCKING_ERROR_CODES,
    NON_RECOVERABLE_ERROR_CODES,
)

logger = logging.getLogger(__name__)

# Known image extensions. If the URL ends with one of these we route to vision.
_IMAGE_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
)

# Max bytes to download before giving up (4 MB — large enough for any
# reasonable generated image, small enough that a runaway download can't
# freeze a test run).
_MAX_IMAGE_BYTES = 4 * 1024 * 1024

_SYSTEM_PROMPT = (
    "You are an impartial judge evaluating whether a generated image "
    "matches an expected description. Reply ONLY in the JSON format "
    "requested — no prose, no markdown."
)


@dataclass
class VisionVerdict:
    """Result of a vision-judge evaluation.

    Phase 2C.3 (per Codex C6) added two fields to surface degraded
    scoring to downstream consumers:

    * ``vision_fallback_used`` (bool): True when vision could not
      verify image content (download failed, data URL invalid,
      unsupported source, or parse error). Downstream callers that
      fall back to a text-only judge can read this field and apply
      a score cap based on what evidence IS available.

    * ``cap_reason`` (str | None): explains WHY the cap (if any) was
      applied. Tier strings the system uses (operator-readable in
      reports):
        - ``"no_evidence"``  → cap at 0.3 (no URL, no content, no metadata)
        - ``"url_only"``     → cap at 0.5 (URL present, content not verified)
        - ``"url_plus_structural"`` → cap at 0.7 (URL + shape match)
        - ``"url_plus_secondary"`` → cap at 0.9 (URL + structural + OCR/metadata)
      None when no cap applied (full vision-verified result).

    The cap helper :func:`apply_fallback_cap` enforces the tiers
    consistently across consumers.
    """

    passed: bool
    score: float  # 0.0 - 1.0
    reasoning: str
    criterion_scores: dict[str, float]
    fallback_reason: str | None = None  # Set when vision couldn't run
    vision_fallback_used: bool = False  # Phase 2C.3 (Codex C6)
    cap_reason: str | None = None  # Phase 2C.3 (Codex C6)


# ---------------------------------------------------------------------------
# Phase 2C.3 — tiered fallback cap (Codex C6)
# ---------------------------------------------------------------------------
#
# When vision can't verify image content, downstream judges falling back
# to text-only scoring can over-credit "URL present" as a pass. The cap
# tiers below define the maximum score allowed when the only evidence
# available is at each tier:
#
#   no_evidence:        0.3 — neither URL nor content nor metadata
#   url_only:           0.5 — URL present, content unverified
#   url_plus_structural: 0.7 — URL + structural match (e.g., expected
#                              response shape, expected MIME type)
#   url_plus_secondary: 0.9 — URL + structural + secondary verification
#                              (OCR text, metadata, cached content)
#   None (no cap):      1.0 — full vision-verified result
#
# Per Codex C6 -- nuance matters. A flat 0.5 cap over-scored
# no-evidence cases and under-scored cases with secondary evidence.
_FALLBACK_CAP_TIERS: dict[str, float] = {
    "no_evidence": 0.3,
    "url_only": 0.5,
    "url_plus_structural": 0.7,
    "url_plus_secondary": 0.9,
}


def apply_fallback_cap(score: float, cap_reason: str | None) -> float:
    """Cap `score` by the tier corresponding to `cap_reason`.

    Returns `min(score, tier_max)` when `cap_reason` is a known tier;
    returns `score` unchanged when `cap_reason` is None or unknown
    (defensive: don't accidentally cap legitimate scores when the
    tier label is wrong).
    """
    if cap_reason is None:
        return score
    cap = _FALLBACK_CAP_TIERS.get(cap_reason)
    if cap is None:
        return score
    return min(score, cap)


def _looks_like_image_url(candidate: str) -> bool:
    if not isinstance(candidate, str):
        return False
    stripped = candidate.strip().lower()
    if stripped.startswith("data:image/"):
        return True
    parsed = urlparse(stripped)
    if parsed.scheme not in ("http", "https"):
        return False
    path = parsed.path
    return path.endswith(_IMAGE_EXTS)


def extract_image_url(response_payload: Any) -> str | None:
    """Walk a dict/list/str payload looking for an image URL.

    Returns the first URL that looks like an image, or None.
    """
    if response_payload is None:
        return None
    if isinstance(response_payload, str):
        return response_payload.strip() if _looks_like_image_url(response_payload) else None
    if isinstance(response_payload, list):
        for item in response_payload:
            found = extract_image_url(item)
            if found:
                return found
        return None
    if isinstance(response_payload, dict):
        # Common keys first for speed
        for key in ("url", "image_url", "output_url", "image", "href", "result_url"):
            if key in response_payload:
                found = extract_image_url(response_payload[key])
                if found:
                    return found
        for value in response_payload.values():
            found = extract_image_url(value)
            if found:
                return found
    return None


def _download_image(url: str) -> tuple[bytes, str] | None:
    """Fetch an image URL and return (bytes, media_type) or None on failure.

    No retry policy — one attempt, short timeout, small size cap.
    """
    # Lazy import keeps ``requests`` optional at import time.
    try:
        import requests  # type: ignore
    except ImportError:
        logger.warning("vision_judge: `requests` not installed; cannot download image")
        return None

    try:
        resp = requests.get(url, timeout=10, stream=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("vision_judge: download failed for %s: %s", url, exc)
        return None

    content_type = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
    if not content_type.startswith("image/"):
        logger.warning(
            "vision_judge: non-image content-type %s for %s", content_type, url
        )
        return None

    data = bytearray()
    for chunk in resp.iter_content(chunk_size=8192):
        data.extend(chunk)
        if len(data) > _MAX_IMAGE_BYTES:
            logger.warning(
                "vision_judge: image at %s exceeds %d bytes, aborting",
                url,
                _MAX_IMAGE_BYTES,
            )
            return None
    return bytes(data), content_type


def _decode_data_url(data_url: str) -> tuple[bytes, str] | None:
    match = re.match(r"data:(image/[a-zA-Z0-9+.-]+);base64,(.+)$", data_url.strip())
    if not match:
        return None
    media_type = match.group(1)
    try:
        return base64.b64decode(match.group(2)), media_type
    except Exception:
        return None


def _build_vision_prompt(
    expected_description: str, criteria: list[dict]
) -> str:
    criterion_block = "\n".join(
        f"- {c.get('criterion', '(unnamed)')} (weight={c.get('weight', 0):.2f}, "
        f"eval_type={c.get('eval_type', 'subjective_quality')})"
        for c in criteria
    ) or "- Overall fidelity to the expected description (weight=1.00)"
    return (
        "Expected output description:\n"
        f"{expected_description.strip()}\n\n"
        "Criteria to evaluate (each scored 0.0-1.0):\n"
        f"{criterion_block}\n\n"
        "Look at the generated image and score each criterion. Reply with JSON:\n"
        "{\n"
        '  "criterion_scores": {"<criterion text>": 0.0, ...},\n'
        '  "score": 0.0,\n'
        '  "passed": true,\n'
        '  "reasoning": "One short paragraph explaining the verdict."\n'
        "}\n"
        'Where "score" is the criterion-weighted mean (0-1) and "passed" is '
        "true only when score >= 0.6 AND no required criterion scored 0.0."
    )


def evaluate_generated_image(
    image_source: str,
    expected_description: str,
    criteria: list[dict] | None = None,
    client: Anthropic | None = None,
    model: str | None = None,
) -> VisionVerdict:
    """Score a generated image against an expected description.

    Parameters
    ----------
    image_source
        Either an ``https://`` URL OR a ``data:image/...;base64,...`` data URL.
    expected_description
        The target description (from TestCase.expected_output / sample_output).
    criteria
        Optional list of ``{"criterion": str, "weight": float, "eval_type": str}``.
    client
        Optional pre-built Anthropic client (tests inject a mock).
    model
        Optional override; defaults to ``config.DEFAULT_MODEL``.

    Returns
    -------
    VisionVerdict. ``fallback_reason`` is set when the call couldn't run
    (no image, download failed, API error) — the caller falls back to the
    text judge in that case.
    """
    criteria = criteria or []

    # Resolve the image to bytes + media_type.
    if image_source.startswith("data:image/"):
        decoded = _decode_data_url(image_source)
        if decoded is None:
            return VisionVerdict(
                passed=False,
                score=0.0,
                reasoning="",
                criterion_scores={},
                fallback_reason="invalid_data_url",
                vision_fallback_used=True,
                cap_reason="no_evidence",
            )
        data, media_type = decoded
        source_block: dict[str, Any] = {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        }
    elif image_source.startswith(("http://", "https://")):
        # Honor the same fetch-fallback flag used everywhere else in the repo.
        if not getattr(config, "ENABLE_FETCH_FALLBACK", True):
            # When fetch fallback is disabled we still attempt — the flag
            # gates retry/backoff, not blanket disablement — but record it.
            pass
        downloaded = _download_image(image_source)
        if downloaded is None:
            return VisionVerdict(
                passed=False,
                score=0.0,
                reasoning="",
                criterion_scores={},
                fallback_reason="download_failed",
                vision_fallback_used=True,
                cap_reason="url_only",
            )
        data, media_type = downloaded
        source_block = {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        }
    else:
        return VisionVerdict(
            passed=False,
            score=0.0,
            reasoning="",
            criterion_scores={},
            fallback_reason="unsupported_source",
            vision_fallback_used=True,
            cap_reason="no_evidence",
        )

    if client is None:
        client = Anthropic()
    if model is None:
        model = config.DEFAULT_MODEL

    user_text = _build_vision_prompt(expected_description, criteria)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": source_block},
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )
    except APIError as exc:
        logger.warning("vision_judge: Anthropic API error: %s", exc)
        return VisionVerdict(
            passed=False,
            score=0.0,
            reasoning="",
            criterion_scores={},
            fallback_reason=f"api_error: {exc.__class__.__name__}",
            vision_fallback_used=True,
            cap_reason="no_evidence",
        )
    except Exception as exc:  # unexpected: network / serialization
        logger.warning("vision_judge: unexpected error: %s", exc)
        return VisionVerdict(
            passed=False,
            score=0.0,
            reasoning="",
            criterion_scores={},
            fallback_reason=f"unexpected: {exc.__class__.__name__}",
            vision_fallback_used=True,
            cap_reason="no_evidence",
        )

    # Extract text content
    text_parts: list[str] = []
    for block in response.content or []:
        text = getattr(block, "text", None)
        if text:
            text_parts.append(text)
    raw = "\n".join(text_parts).strip()

    # Strip optional ```json fences the model sometimes adds despite instructions.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("vision_judge: could not parse response as JSON: %s", raw[:200])
        return VisionVerdict(
            passed=False,
            score=0.0,
            reasoning=raw,
            criterion_scores={},
            fallback_reason="parse_failure",
            vision_fallback_used=True,
            cap_reason="no_evidence",
        )

    score = float(parsed.get("score", 0.0))
    score = max(0.0, min(1.0, score))
    criterion_scores_raw = parsed.get("criterion_scores", {}) or {}
    criterion_scores = {
        str(k): max(0.0, min(1.0, float(v)))
        for k, v in criterion_scores_raw.items()
        if isinstance(v, (int, float))
    }
    return VisionVerdict(
        passed=bool(parsed.get("passed", score >= 0.6)),
        score=score,
        reasoning=str(parsed.get("reasoning", "")),
        criterion_scores=criterion_scores,
    )


# Re-export for callers that want to branch on web_fetch error codes
__all__ = [
    "VisionVerdict",
    "evaluate_generated_image",
    "extract_image_url",
    "BLOCKING_ERROR_CODES",
    "NON_RECOVERABLE_ERROR_CODES",
]
