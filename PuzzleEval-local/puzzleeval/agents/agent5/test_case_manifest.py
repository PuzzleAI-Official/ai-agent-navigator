"""Compact Agent 3 test-case manifest for Agent 5 builds.

The manifest is derived from the actual test cases that final evaluation will
run. It is not a modality contract database; it is a small map of real input
families and representative cases so Agent 5 can reason against the work it
must actually satisfy.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from puzzleeval.schemas import Agent5Input, TestCase

TEST_CASE_MANIFEST_RELATIVE_PATH = "_agent_state/test_case_manifest.json"


def test_case_manifest_path(sandbox_dir: Path) -> Path:
    return sandbox_dir / TEST_CASE_MANIFEST_RELATIVE_PATH


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_len(value: Any) -> int:
    if isinstance(value, (list, tuple, set, dict, str)):
        return len(value)
    return 0


def _file_family(tc: TestCase) -> str:
    if not getattr(tc, "test_file_path", None):
        return "no_file"
    suffix = Path(str(tc.test_file_path)).suffix.lower().lstrip(".")
    return suffix or "file"


def _family_key(tc: TestCase) -> str:
    parts = [
        f"input={tc.input_type or 'unknown'}",
        f"output={tc.output_type or 'unknown'}",
        f"mode={getattr(tc, 'evaluation_mode', None) or 'auto'}",
        f"file={_file_family(tc)}",
    ]
    if int(getattr(tc, "max_turns", 1) or 1) > 1:
        parts.append("turns=multi")
    return "|".join(parts)


def _test_summary(tc: TestCase) -> dict[str, Any]:
    criteria = list(getattr(tc, "judgement_criteria", []) or [])
    fixture_refs = []
    input_context = getattr(tc, "input_context", None)
    if isinstance(input_context, dict):
        fixture_refs = sorted(
            str(key)
            for key in input_context.keys()
            if any(token in str(key).lower() for token in ("fixture", "policy", "hours", "price", "service"))
        )
    return {
        "id": tc.id,
        "scenario": tc.scenario,
        "sub_task_ref": tc.sub_task_ref,
        "scope_id": getattr(tc, "scope_id", None),
        "input_type": tc.input_type,
        "output_type": tc.output_type,
        "evaluation_mode": getattr(tc, "evaluation_mode", "auto"),
        "file_required": bool(getattr(tc, "file_required", False)),
        "has_file": bool(getattr(tc, "test_file_path", None)),
        "file_family": _file_family(tc),
        "max_turns": int(getattr(tc, "max_turns", 1) or 1),
        "tags": list(getattr(tc, "tags", []) or []),
        "fixture_fact_refs": fixture_refs,
        "judgement_criteria": [
            {
                "criterion": getattr(item, "criterion", ""),
                "eval_type": getattr(item, "eval_type", ""),
                "weight": getattr(item, "weight", None),
            }
            for item in criteria
        ],
    }


def _representative_for_family(test_cases: list[TestCase]) -> TestCase:
    # Prefer a small but real case in the family. The family key already
    # preserves input/output/eval/file shape, so this stays generic.
    return sorted(
        test_cases,
        key=lambda tc: (
            int(getattr(tc, "max_turns", 1) or 1),
            0 if "happy_path" in (getattr(tc, "tags", []) or []) else 1,
            str(getattr(tc, "id", "")),
        ),
    )[0]


def build_test_case_manifest(input_data: Agent5Input | Any) -> dict[str, Any]:
    test_cases = list(getattr(getattr(input_data, "test_cases", None), "test_cases", []) or [])
    families: dict[str, list[TestCase]] = {}
    for tc in test_cases:
        families.setdefault(_family_key(tc), []).append(tc)

    family_payloads: list[dict[str, Any]] = []
    representative_ids: list[str] = []
    for family_key in sorted(families):
        cases = families[family_key]
        representative = _representative_for_family(cases)
        representative_ids.append(representative.id)
        family_payloads.append({
            "family_key": family_key,
            "why_it_matters": (
                "Representative of actual Agent 3 tests with this input/output/"
                "evaluation/file shape; probe this family on the production path."
            ),
            "test_count": len(cases),
            "test_ids": [tc.id for tc in sorted(cases, key=lambda item: item.id)],
            "representative_test_id": representative.id,
        })

    return {
        "schema_version": 1,
        "written_t_abs": time.time(),
        "trace_id": getattr(input_data, "trace_id", None),
        "test_count": len(test_cases),
        "family_count": len(family_payloads),
        "families": family_payloads,
        "representative_test_ids": representative_ids,
        "tests": [_test_summary(tc) for tc in sorted(test_cases, key=lambda item: item.id)],
        "guidance": (
            "This manifest summarizes the real Agent 3 cases. Use it to plan "
            "the harness and to understand which representative production "
            "probe(s) must prove the same execution path final evaluation uses."
        ),
    }


def stage_test_case_manifest(sandbox_dir: Path, input_data: Agent5Input | Any) -> Path:
    path = test_case_manifest_path(sandbox_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_test_case_manifest(input_data), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path


def read_test_case_manifest(sandbox_dir: Path) -> dict[str, Any] | None:
    path = test_case_manifest_path(sandbox_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def representative_test_cases(
    sandbox_dir: Path,
    test_cases: list[TestCase],
) -> list[TestCase]:
    manifest = read_test_case_manifest(sandbox_dir)
    ids = set()
    if isinstance(manifest, dict):
        ids = {str(item) for item in _as_list(manifest.get("representative_test_ids"))}
    if ids:
        selected = [tc for tc in test_cases if tc.id in ids]
        if selected:
            return selected
    by_family: dict[str, list[TestCase]] = {}
    for tc in test_cases:
        by_family.setdefault(_family_key(tc), []).append(tc)
    return [_representative_for_family(items) for _, items in sorted(by_family.items())]


__all__ = [
    "TEST_CASE_MANIFEST_RELATIVE_PATH",
    "build_test_case_manifest",
    "read_test_case_manifest",
    "representative_test_cases",
    "stage_test_case_manifest",
    "test_case_manifest_path",
]
