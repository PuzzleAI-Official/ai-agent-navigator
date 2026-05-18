"""Agent 5 abandon-candidate artifact validation.

Phase 6 gives Agent 5 a truthful early-exit path for candidates that cannot be
repaired by more code patches. The artifact is agent-authored, but validation is
deterministic and evidence-based so "I think it is impossible" is never enough.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ABANDON_CANDIDATE_RELATIVE_PATH = "_agent_state/abandon_candidate.json"

VALID_ABANDON_REASONS = frozenset({
    "provider_blocked",
    "credentials_unavailable",
    "api_incompatible",
    "docs_missing",
    "quota_exhausted",
    "test/fixture_mismatch_unfixable",
})

_LOCAL_EVIDENCE_KEYS = ("artifact", "path", "file")
_EXTERNAL_EVIDENCE_KEYS = (
    "docs_entrypoint",
    "failure_packet",
    "provider_response",
    "research_finding",
    "status_code",
    "url",
)


@dataclass(frozen=True)
class AbandonCandidateValidation:
    ok: bool
    issues: list[str]
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": self.issues, "data": self.data}


def _load_json_object(content: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, [f"abandon_candidate.json must be valid JSON: {exc.msg}"]
    if not isinstance(data, dict):
        return None, ["abandon_candidate.json must be a JSON object"]
    return data, []


def _normalize_rel_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")


def _evidence_has_external_anchor(item: Any, sandbox_dir: Path | None) -> bool:
    if isinstance(item, str):
        rel = _normalize_rel_path(item)
        return bool(rel) and (sandbox_dir is None or (sandbox_dir / rel).exists())
    if not isinstance(item, dict):
        return False
    if any(item.get(key) for key in _EXTERNAL_EVIDENCE_KEYS):
        return True
    for key in _LOCAL_EVIDENCE_KEYS:
        rel = _normalize_rel_path(item.get(key))
        if rel and (sandbox_dir is None or (sandbox_dir / rel).exists()):
            return True
    return False


def validate_abandon_candidate_text(
    content: str,
    *,
    sandbox_dir: Path | None = None,
) -> AbandonCandidateValidation:
    data, issues = _load_json_object(content)
    if data is None:
        return AbandonCandidateValidation(False, issues)

    reason = str(data.get("reason") or "")
    if reason not in VALID_ABANDON_REASONS:
        issues.append(
            "reason must be one of "
            + ", ".join(sorted(VALID_ABANDON_REASONS))
        )

    summary = str(data.get("summary") or data.get("explanation") or "").strip()
    if len(summary) < 20:
        issues.append("summary must explain why more patching is the wrong next action")

    evidence = data.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        issues.append("evidence must be a non-empty list")
    else:
        anchored = [
            item for item in evidence
            if _evidence_has_external_anchor(item, sandbox_dir)
        ]
        if not anchored:
            issues.append(
                "evidence must cite external artifacts or provider/docs facts, "
                "not only Agent 5 self-assertion"
            )

    return AbandonCandidateValidation(not issues, issues, data)


def read_abandon_candidate(sandbox_dir: Path) -> dict[str, Any] | None:
    path = sandbox_dir / ABANDON_CANDIDATE_RELATIVE_PATH
    if not path.exists():
        return None
    verdict = validate_abandon_candidate_text(
        path.read_text(encoding="utf-8"),
        sandbox_dir=sandbox_dir,
    )
    return verdict.data if verdict.ok else None


def failed_harness_category_for_reason(reason: str) -> str:
    return reason if reason in VALID_ABANDON_REASONS else "unknown"


__all__ = [
    "ABANDON_CANDIDATE_RELATIVE_PATH",
    "AbandonCandidateValidation",
    "VALID_ABANDON_REASONS",
    "failed_harness_category_for_reason",
    "read_abandon_candidate",
    "validate_abandon_candidate_text",
]
