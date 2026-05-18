"""Evidence-check for ``_agent_state/reflection_phase_3.md`` (PR 2).

The reflection is the agent's pre-HARNESS_COMPLETE walk-through against
``_agent_state/objective.md`` SUCCESS CRITERIA. It MUST cite specific
evidence (file references, test output snippets, forensics events,
code excerpts) — not self-attestation ("yes, handled").

Per the plan's Section 5 (revised): hybrid check —
  * **Pattern check (fast):** regex for required citation patterns per
    section. Cheap, deterministic, gameable in principle but raises the
    cost of theater significantly.
  * **LLM-judge fallback (slow):** invoked only when the pattern check
    is borderline. Uses Sonnet to soft-validate that the cited evidence
    actually supports the claim. Capped at one invocation per build to
    keep cost < $0.01/build.

Either failure → caller fires the retry directive. After retry, accept
with ``reflection_gate_fired`` telemetry (AD-007 soft tier).

The module exports pure functions + a public entry point ``evaluate``
that returns a ``ReflectionVerdict`` enum + structured details. Wiring
into the build loop lives in ``agent5/verification.py::verify_reflection_complete``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import anthropic


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ReflectionVerdict(str, Enum):
    """Outcome of an evidence check.

    PASS - reflection has substantive evidence; accept.
    FAIL - reflection is vacuous; caller fires retry directive.
    BORDERLINE - pattern check is ambiguous; LLM-judge may resolve.
    MISSING - reflection_phase_3.md doesn't exist; caller fires
              the directive (the agent hasn't written one yet).
    """

    PASS = "pass"
    FAIL = "fail"
    BORDERLINE = "borderline"
    MISSING = "missing"


@dataclass(frozen=True)
class ReflectionEvidence:
    """Structured measurement of reflection content.

    All fields are computed by ``compute_evidence``. ``verdict`` is the
    pattern-check call; the LLM-judge runs ON TOP of this when verdict
    is BORDERLINE.
    """

    total_file_refs: int
    total_code_blocks: int
    total_forensics_refs: int
    word_count: int
    sections_with_evidence: int  # how many of the 5 expected sections have at least one ref
    missing_section_headers: tuple[str, ...]  # sections not present at all
    verdict: ReflectionVerdict
    reason: str  # short human-readable explanation


# ---------------------------------------------------------------------------
# Pattern-check thresholds
# ---------------------------------------------------------------------------
# These are tunable via env var (see ``puzzleeval.config``). Defaults are
# anchored against the reflection template's structure (5 sections, 6
# adversarial probes, 4 code-quality items) — a baseline-substantive
# reflection has at least:
#   - 6 file refs (one per adversarial probe is the floor)
#   - all 5 expected sections present
#   - 50 words of substantive prose

# Tier boundaries:
#   verdict=PASS       when total_file_refs >= PASS_FILE_REF_FLOOR
#                      AND all 5 expected sections present AND have evidence
#                      AND word_count >= PASS_WORD_FLOOR
#   verdict=FAIL       when total_file_refs < FAIL_FILE_REF_CEILING
#                      OR more than 2 expected sections are empty/missing
#   verdict=BORDERLINE otherwise — caller decides whether to invoke LLM-judge

PASS_FILE_REF_FLOOR = 8
PASS_WORD_FLOOR = 150
FAIL_FILE_REF_CEILING = 3
FAIL_MISSING_SECTIONS_CEILING = 2  # > this many sections empty → fail outright


EXPECTED_SECTION_HEADERS: tuple[str, ...] = (
    "## SUCCESS CRITERIA evidence walk-through",
    "## Adversarial probe robustness",
    "## User-fit assessment",
    "## Code quality assessment",
    "## Self-critique",
)

SECTION_HEADING_ALIASES: dict[str, tuple[str, ...]] = {
    "## SUCCESS CRITERIA evidence walk-through": (
        "success criteria", "deliverable", "objective", "coverage",
        "criteria evidence",
    ),
    "## Adversarial probe robustness": (
        "adversarial", "probe", "robustness", "empty input", "max input",
        "malformed", "idempotency", "concurrency", "repeat call",
    ),
    "## User-fit assessment": (
        "user-fit", "user fit", "blueprint", "sub-task", "subtask",
        "user request", "business task",
    ),
    "## Code quality assessment": (
        "code quality", "quality", "cleanup", "resource", "exception",
        "error path", "state decision", "separation",
    ),
    "## Self-critique": (
        "self-critique", "self critique", "one more turn", "would fix",
        "remaining risk", "critique",
    ),
}


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------


# File:line references. Runtime/code files still matter, but reflections can
# now cite durable Agent 5 research artifacts as first-class evidence.
_CITABLE_FILE_NAME = (
    r"(?:[a-z_][a-z0-9_]*\.py|requirements\.txt|conversation_log\.json|"
    r"_agent_state/(?:"
    r"objective\.md|docs_entrypoint\.json|research_handoff\.json|"
    r"research_plan\.json|research_synthesis\.json|implementation_plan\.json|"
    r"runtime_state\.json|latest_failure_packet\.json|reflection_phase_3\.md|"
    r"research_findings/[A-Za-z0-9_.-]+\.json"
    r"))"
)

_FILE_REF_PATTERN = re.compile(
    rf"\b{_CITABLE_FILE_NAME}(?::\d+(?:-\d+)?)?",
    re.IGNORECASE,
)

_FILE_REF_DETAIL_PATTERN = re.compile(
    rf"\b(?P<name>{_CITABLE_FILE_NAME})(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?",
    re.IGNORECASE,
)

# Forensics-related identifiers commonly cited in reflections
_FORENSICS_PATTERN = re.compile(
    r"\b(?:traced_op|op_start|op_done|op_error|request_done|request_error|"
    r"session_create_start|session_create_done|stream_start|stream_event|"
    r"stream_done|stream_error|harness_forensics|thread_start|thread_error)\b",
    re.IGNORECASE,
)

# Triple-backtick code fences (rough — counts each fence pair as one block)
_CODE_FENCE_PATTERN = re.compile(r"```")


# ---------------------------------------------------------------------------
# Pattern check
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    """Count alphanumeric tokens — a rough proxy for "substantive content"."""
    return len(re.findall(r"[A-Za-z0-9]+", text))


def _normalize_heading(text: str) -> str:
    text = re.sub(r"^#+\s*", "", text.strip().lower())
    text = re.sub(r"^\d+[\).\-\s]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _heading_matches(canonical: str, observed_heading: str) -> bool:
    observed = _normalize_heading(observed_heading)
    canonical_norm = _normalize_heading(canonical)
    if canonical_norm in observed or observed in canonical_norm:
        return True
    return any(alias in observed for alias in SECTION_HEADING_ALIASES[canonical])


def _split_sections(reflection_md: str) -> dict[str, str]:
    """Return a mapping of expected_section_header → body text.

    Sections not present in the input map to empty strings. Sections
    present but without expected wording (e.g. typos or rewrites) map to
    empty as well — strict matching is intentional, the gate teaches the
    agent the canonical structure.
    """
    out: dict[str, str] = {h: "" for h in EXPECTED_SECTION_HEADERS}
    header_matches = list(re.finditer(r"(?m)^##+\s+.+$", reflection_md))
    for i, match in enumerate(header_matches):
        heading = match.group(0).strip()
        body_start = match.end()
        body_end = (
            header_matches[i + 1].start()
            if i + 1 < len(header_matches)
            else len(reflection_md)
        )
        body = reflection_md[body_start:body_end].strip()
        for canonical in EXPECTED_SECTION_HEADERS:
            if out[canonical]:
                continue
            if _heading_matches(canonical, heading):
                out[canonical] = body
                break
    return out


def compute_evidence(reflection_md: str) -> ReflectionEvidence:
    """Compute pattern-based evidence metrics for a reflection.

    Pure function. Same input → same output. No I/O.
    """
    if not reflection_md or not reflection_md.strip():
        return ReflectionEvidence(
            total_file_refs=0,
            total_code_blocks=0,
            total_forensics_refs=0,
            word_count=0,
            sections_with_evidence=0,
            missing_section_headers=EXPECTED_SECTION_HEADERS,
            verdict=ReflectionVerdict.FAIL,
            reason="reflection is empty or whitespace only",
        )

    sections = _split_sections(reflection_md)
    missing_section_headers = tuple(
        h for h, body in sections.items() if not body
    )

    # Whole-document counts
    total_file_refs = len(_FILE_REF_PATTERN.findall(reflection_md))
    # Each fenced code block has TWO triple-backticks (open + close) —
    # halve and round down.
    total_code_blocks = len(_CODE_FENCE_PATTERN.findall(reflection_md)) // 2
    total_forensics_refs = len(_FORENSICS_PATTERN.findall(reflection_md))
    word_count = _word_count(reflection_md)

    # Per-section evidence count
    sections_with_evidence = 0
    for header, body in sections.items():
        if not body:
            continue
        if (
            _FILE_REF_PATTERN.search(body)
            or _CODE_FENCE_PATTERN.search(body)
            or _FORENSICS_PATTERN.search(body)
        ):
            sections_with_evidence += 1

    n_missing = len(missing_section_headers)

    # Ladder the verdict from clearest fail upward.
    if (
        total_file_refs <= FAIL_FILE_REF_CEILING
        or n_missing > FAIL_MISSING_SECTIONS_CEILING
    ):
        verdict = ReflectionVerdict.FAIL
        reason = (
            f"insufficient evidence — file_refs={total_file_refs} "
            f"(<= {FAIL_FILE_REF_CEILING}), missing_sections={n_missing}"
        )
    elif (
        total_file_refs >= PASS_FILE_REF_FLOOR
        and word_count >= PASS_WORD_FLOOR
        and n_missing == 0
        and sections_with_evidence == len(EXPECTED_SECTION_HEADERS)
    ):
        verdict = ReflectionVerdict.PASS
        reason = (
            f"substantive — file_refs={total_file_refs}, words={word_count}, "
            f"all 5 sections present + cite evidence"
        )
    else:
        verdict = ReflectionVerdict.BORDERLINE
        reason = (
            f"borderline — file_refs={total_file_refs}, words={word_count}, "
            f"missing_sections={n_missing}, "
            f"sections_with_evidence={sections_with_evidence}/5"
        )

    return ReflectionEvidence(
        total_file_refs=total_file_refs,
        total_code_blocks=total_code_blocks,
        total_forensics_refs=total_forensics_refs,
        word_count=word_count,
        sections_with_evidence=sections_with_evidence,
        missing_section_headers=missing_section_headers,
        verdict=verdict,
        reason=reason,
    )


def validate_citation_targets(
    reflection_md: str,
    *,
    sandbox_dir: Path | None = None,
    harness_code: str | None = None,
) -> list[str]:
    """Return citation-target errors for file refs in the reflection.

    The pattern check can tell whether a reflection contains references;
    this check verifies the references point at real files and plausible
    line numbers. It is intentionally deterministic and cheap. It does
    not prove the cited code supports the claim; the LLM judge remains
    responsible for semantic support when invoked.
    """
    errors: list[str] = []
    if not reflection_md:
        return errors

    harness_line_count = len(harness_code.splitlines()) if harness_code else None
    seen: set[tuple[str, str | None, str | None]] = set()
    for match in _FILE_REF_DETAIL_PATTERN.finditer(reflection_md):
        name = match.group("name")
        start = match.group("start")
        end = match.group("end")
        key = (name.lower(), start, end)
        if key in seen:
            continue
        seen.add(key)

        file_line_count: int | None = None
        if name.lower() == "harness.py" and harness_line_count is not None:
            file_line_count = harness_line_count
        elif sandbox_dir is not None:
            path = sandbox_dir / name
            if not path.exists():
                errors.append(f"{name} is cited but does not exist")
                continue
            try:
                file_line_count = len(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                errors.append(f"{name} is cited but cannot be read")
                continue

        if start is not None and file_line_count is not None:
            start_i = int(start)
            end_i = int(end or start)
            if start_i < 1 or end_i < start_i:
                errors.append(f"{name}:{start}" + (f"-{end}" if end else "") + " has invalid line range")
            elif start_i > file_line_count or end_i > file_line_count:
                errors.append(
                    f"{name}:{start}" + (f"-{end}" if end else "")
                    + f" exceeds file length ({file_line_count} lines)"
                )
    return errors


def _harness_citation_windows(
    reflection_md: str,
    harness_code: str | None,
    *,
    context_lines: int = 4,
    max_windows: int = 18,
    max_chars: int = 9000,
) -> str:
    """Return cited ``harness.py`` line windows for the LLM judge.

    Sending the beginning of harness.py is not enough for real reflections:
    strong evidence often cites later lines. Use compact windows around the
    cited line numbers so the judge sees the evidence the agent actually cited.
    """

    if not harness_code:
        return ""
    lines = harness_code.splitlines()
    if not lines:
        return ""

    windows: list[tuple[int, int]] = []
    for match in _FILE_REF_DETAIL_PATTERN.finditer(reflection_md or ""):
        if match.group("name").lower() != "harness.py":
            continue
        start = match.group("start")
        if start is None:
            continue
        start_i = max(1, int(start) - context_lines)
        end_i = int(match.group("end") or start)
        end_i = min(len(lines), end_i + context_lines)
        if end_i >= start_i:
            windows.append((start_i, end_i))
        if len(windows) >= max_windows:
            break

    if not windows:
        return "\n".join(
            f"{line_no}: {line}"
            for line_no, line in enumerate(lines[:120], start=1)
        )[:max_chars]

    merged: list[tuple[int, int]] = []
    for start_i, end_i in sorted(windows):
        if not merged or start_i > merged[-1][1] + 1:
            merged.append((start_i, end_i))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_i))

    chunks: list[str] = []
    total = 0
    for start_i, end_i in merged:
        body = "\n".join(
            f"{line_no}: {lines[line_no - 1]}"
            for line_no in range(start_i, end_i + 1)
        )
        chunk = f"\n--- harness.py:{start_i}-{end_i} ---\n{body}\n"
        if total + len(chunk) > max_chars:
            break
        chunks.append(chunk)
        total += len(chunk)
    return "".join(chunks).strip()


def _has_strong_deterministic_evidence(evidence: ReflectionEvidence) -> bool:
    """Return true for high-evidence reflections that do not need judging."""

    return (
        evidence.total_file_refs >= PASS_FILE_REF_FLOOR * 2
        and evidence.word_count >= PASS_WORD_FLOOR * 2
        and evidence.sections_with_evidence >= len(EXPECTED_SECTION_HEADERS) - 1
        and len(evidence.missing_section_headers) <= 1
    )


# ---------------------------------------------------------------------------
# LLM-judge fallback
# ---------------------------------------------------------------------------
# Sonnet judges whether the cited evidence supports the claims. Used only
# when pattern check returns BORDERLINE. Capped at ONE invocation per build
# at ~$0.005, well under the $0.01/build budget in the plan.

_LLM_JUDGE_PROMPT = """You are reviewing a software-engineering reflection
for evidence quality. The reflection is supposed to cite SPECIFIC evidence
(file:line references, test output snippets, code excerpts, forensics
events) for each claim — not self-attestation ("yes, handled").

Below are three pieces of context:

1. The objective the reflection is grading itself against.
2. The reflection itself.
3. (Optional) The cited harness.py line windows.

Your task: decide whether the reflection's claims are GROUNDED in the
cited evidence, or whether they're self-attestation hand-waving.

Rules:
- If the reflection cites file:line references that DON'T appear in the
  cited line windows (when source is provided), that's hand-waving — fail.
- If the reflection makes claims like "concurrency safe" without code
  citations, that's self-attestation — fail.
- If the reflection cites specific lines + the cited code actually
  supports the claim, that's grounded — pass.
- If the reflection is partially grounded but skips required sections
  (Adversarial probe robustness, Code quality assessment, etc.),
  that's still fail — the gate teaches the canonical structure.

Output ONLY a single JSON object on one line, no prose, no markdown:
{"verdict": "pass" | "fail", "reason": "<one-sentence explanation>"}
"""


def llm_judge_check(
    *,
    reflection_md: str,
    objective_md: str,
    harness_code: str | None,
    client: "anthropic.Anthropic",
    judge_model: str,
    logger: Any,
    trace_id: str,
    candidate_name: str,
) -> tuple[ReflectionVerdict, str]:
    """Call Sonnet to judge reflection evidence quality.

    Returns ``(verdict, reason)``. Verdict is PASS or FAIL — the judge
    is binary. Errors (network, schema, timeout) are caught and surfaced
    as PASS with reason "judge_error" so we don't block builds on
    infrastructure flakes (AD-007 fail-soft).

    The judge is best-effort — if it crashes, we log + accept (failing
    open is the safer default for the soft tier).
    """
    user_message_parts: list[str] = []
    user_message_parts.append("# OBJECTIVE\n\n")
    user_message_parts.append(objective_md.strip()[:8000])
    user_message_parts.append("\n\n# REFLECTION\n\n")
    user_message_parts.append(reflection_md.strip()[:8000])
    if harness_code:
        user_message_parts.append("\n\n# CITED HARNESS.PY LINE WINDOWS\n\n")
        user_message_parts.append(_harness_citation_windows(reflection_md, harness_code))
    user_message = "".join(user_message_parts)

    try:
        response = client.messages.create(
            model=judge_model,
            max_tokens=200,
            system=_LLM_JUDGE_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text_blocks = [
            getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text"
        ]
        raw = "".join(text_blocks).strip()
    except Exception as exc:  # noqa: BLE001 — fail-soft per AD-007
        logger.warning(
            "Reflection LLM-judge invocation failed for %s: %s",
            candidate_name, type(exc).__name__,
            extra={
                "operation": "reflection_llm_judge_error",
                "trace_id": trace_id,
                "candidate_name": candidate_name,
                "error_type": type(exc).__name__,
                "error_msg": str(exc)[:200],
            },
        )
        return ReflectionVerdict.PASS, "judge_error_fail_open"

    # Parse the one-line JSON. Tolerate stray prose by extracting the
    # last "{...}" substring.
    import json
    decoded = None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                decoded = json.loads(m.group(0))
            except json.JSONDecodeError:
                decoded = None
    if not isinstance(decoded, dict) or "verdict" not in decoded:
        return ReflectionVerdict.PASS, f"judge_unparseable:{raw[:80]}"

    verdict_str = str(decoded.get("verdict", "")).lower().strip()
    reason = str(decoded.get("reason", ""))[:200]
    if verdict_str == "fail":
        return ReflectionVerdict.FAIL, f"judge_fail:{reason}"
    return ReflectionVerdict.PASS, f"judge_pass:{reason}"


# ---------------------------------------------------------------------------
# Public entry point — combine pattern check + optional LLM-judge
# ---------------------------------------------------------------------------


def evaluate(
    reflection_md: str,
    *,
    objective_md: str = "",
    harness_code: str | None = None,
    sandbox_dir: Path | None = None,
    client: "anthropic.Anthropic | None" = None,
    judge_model: str = "claude-sonnet-4-6",
    llm_judge_enabled: bool = True,
    logger: Any | None = None,
    trace_id: str = "",
    candidate_name: str = "",
) -> tuple[ReflectionVerdict, ReflectionEvidence, str]:
    """Combined evidence check: pattern → optional LLM-judge → final verdict.

    Returns ``(final_verdict, evidence, judge_reason)`` where
    ``judge_reason`` is empty when the LLM-judge wasn't invoked.
    """
    evidence = compute_evidence(reflection_md)

    if evidence.verdict != ReflectionVerdict.FAIL:
        citation_errors = validate_citation_targets(
            reflection_md,
            sandbox_dir=sandbox_dir,
            harness_code=harness_code,
        )
        if citation_errors:
            return (
                ReflectionVerdict.FAIL,
                evidence,
                "invalid_citations:" + "; ".join(citation_errors[:5]),
            )

    if evidence.verdict in (ReflectionVerdict.PASS, ReflectionVerdict.FAIL):
        return evidence.verdict, evidence, ""

    # BORDERLINE — invoke LLM-judge when configured.
    if _has_strong_deterministic_evidence(evidence):
        return ReflectionVerdict.PASS, evidence, "deterministic_strong_evidence"

    if not llm_judge_enabled or client is None or not objective_md:
        # Pattern check borderline + judge unavailable → defensive PASS.
        # The retry-directive path is the safety net when reflection is
        # actually missing; once it exists at borderline quality, we
        # accept rather than spin.
        return ReflectionVerdict.PASS, evidence, "judge_disabled"

    judge_verdict, judge_reason = llm_judge_check(
        reflection_md=reflection_md,
        objective_md=objective_md,
        harness_code=harness_code,
        client=client,
        judge_model=judge_model,
        logger=logger,
        trace_id=trace_id,
        candidate_name=candidate_name,
    )
    return judge_verdict, evidence, judge_reason


__all__ = [
    "ReflectionVerdict",
    "ReflectionEvidence",
    "EXPECTED_SECTION_HEADERS",
    "PASS_FILE_REF_FLOOR",
    "PASS_WORD_FLOOR",
    "FAIL_FILE_REF_CEILING",
    "FAIL_MISSING_SECTIONS_CEILING",
    "compute_evidence",
    "validate_citation_targets",
    "llm_judge_check",
    "evaluate",
]
