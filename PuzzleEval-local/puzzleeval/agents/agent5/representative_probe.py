"""Production-equivalence representative probe gate for Agent 5.

This gate runs one real test case per manifest family through the same harness
runner + plugin evaluation path used by final evaluation. It does not tell the
builder how to implement a modality; it checks whether the current harness can
survive the real payload shape and evidence loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from puzzleeval.config import AGENT6_TEST_TIMEOUT
from puzzleeval.schemas import Agent5Input, ScreenedCandidate, TestCase, TestHarness

from .test_case_manifest import representative_test_cases

REPRESENTATIVE_PROBE_EVIDENCE_RELATIVE_PATH = "_agent_state/representative_probe_evidence.json"

_SYSTEM_PROMPT_ALIASES = (
    "instructions",
    "system_prompt",
    "system",
    "brief",
    "agent_prompt",
)


def representative_probe_evidence_path(sandbox_dir: Path) -> Path:
    return sandbox_dir / REPRESENTATIVE_PROBE_EVIDENCE_RELATIVE_PATH


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_requirements(sandbox_dir: Path) -> list[str]:
    lines = _read_text(sandbox_dir / "requirements.txt").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _hash_probe_inputs(sandbox_dir: Path, test_cases: list[TestCase]) -> str:
    payload = {
        "harness.py": _read_text(sandbox_dir / "harness.py"),
        "requirements.txt": _read_text(sandbox_dir / "requirements.txt"),
        "implementation_plan.json": _read_text(sandbox_dir / "_agent_state" / "implementation_plan.json"),
        "representative_tests": [
            tc.model_dump(mode="json") if hasattr(tc, "model_dump") else dict(tc)
            for tc in test_cases
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_cached_evidence(sandbox_dir: Path, probe_hash: str) -> dict[str, Any] | None:
    path = representative_probe_evidence_path(sandbox_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("probe_hash") == probe_hash:
        return data
    return None


def _write_evidence(sandbox_dir: Path, payload: dict[str, Any]) -> Path:
    path = representative_probe_evidence_path(sandbox_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload.setdefault("schema_version", 1)
    payload.setdefault("written_t_abs", time.time())
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def _needs_credentials(candidate: ScreenedCandidate, credentials: dict[str, str] | None) -> bool:
    auth_method = str(getattr(candidate, "auth_method", "") or "").lower()
    if auth_method in {"", "none", "no_auth", "public"}:
        return False
    return not bool(credentials)


def _input_context_default(candidate_name: str, sub_task_ref: str | None) -> dict[str, str]:
    role = (sub_task_ref or "agent").replace("_", " ").strip() or "agent"
    text = (
        f"You are a helpful {role} for {candidate_name}. "
        "Use the current test payload and conversation context to complete the task."
    )
    return {alias: text for alias in _SYSTEM_PROMPT_ALIASES}


def _merge_input_context(test_context: dict[str, Any] | None, default_context: dict[str, str]) -> dict[str, Any]:
    if not isinstance(test_context, dict):
        return dict(default_context)
    merged: dict[str, Any] = dict(default_context)
    merged.update(test_context)
    user_prompt = next(
        (
            test_context[key]
            for key in _SYSTEM_PROMPT_ALIASES
            if isinstance(test_context.get(key), str) and test_context[key].strip()
        ),
        None,
    )
    if user_prompt:
        for alias in _SYSTEM_PROMPT_ALIASES:
            merged[alias] = user_prompt
    return merged


def _is_multi_call(tc: TestCase) -> bool:
    multi_call = {"conversation", "voice_conversation", "voice_turn"}
    return (tc.input_type in multi_call) or (tc.output_type in multi_call)


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    audio_url = payload.get("audio_url") or payload.get("caller_audio_url")
    history = payload.get("conversation_history")
    session_state = payload.get("session_state")
    context = payload.get("input_context")
    return {
        "keys": sorted(str(key) for key in payload.keys()),
        "has_audio": bool(audio_url),
        "turn_index": payload.get("turn_index"),
        "conversation_history_count": len(history) if isinstance(history, list) else 0,
        "session_state_keys": sorted(session_state.keys()) if isinstance(session_state, dict) else [],
        "input_context_keys": sorted(context.keys()) if isinstance(context, dict) else [],
    }


def _summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    raw = result.get("raw_response")
    return {
        "success": bool(result.get("success")),
        "error": str(result.get("error") or "")[:500],
        "output_chars": len(str(result.get("output") or "")),
        "raw_response_keys": sorted(raw.keys()) if isinstance(raw, dict) else [],
        "audio_path_count": len(result.get("audio_paths") or []),
        "has_merged_audio": bool(result.get("merged_audio_path")),
    }


def _requires_observable_output(tc: TestCase) -> bool:
    output_type = str(getattr(tc, "output_type", "") or "").lower()
    if output_type in {"", "none", "no_output", "side_effect_only"}:
        return bool(getattr(tc, "expected_output", None) or getattr(tc, "judgement_criteria", None))
    return True


def _test_case_probe_summary(tc: TestCase) -> dict[str, Any]:
    return {
        "id": tc.id,
        "scenario": tc.scenario,
        "input_type": tc.input_type,
        "output_type": tc.output_type,
        "evaluation_mode": getattr(tc, "evaluation_mode", "auto"),
        "file_required": bool(getattr(tc, "file_required", False)),
        "has_file": bool(getattr(tc, "test_file_path", None)),
        "max_turns": int(getattr(tc, "max_turns", 1) or 1),
        "requires_observable_output": _requires_observable_output(tc),
    }


def _external_block_reason(text: str) -> str | None:
    lowered = text.lower()
    if any(token in lowered for token in ("401", "403", "unauthorized", "forbidden", "invalid api key", "auth")):
        return "credential_or_auth_block"
    if any(token in lowered for token in ("quota", "rate limit", "429", "insufficient credits", "billing")):
        return "quota_or_billing_block"
    if any(token in lowered for token in ("provider unavailable", "temporarily unavailable", "service unavailable", "503")):
        return "provider_unavailable"
    return None


def _supported_types(test_cases: list[TestCase]) -> tuple[list[str], list[str]]:
    input_types = sorted({str(tc.input_type) for tc in test_cases if getattr(tc, "input_type", None)})
    output_types = sorted({str(tc.output_type) for tc in test_cases if getattr(tc, "output_type", None)})
    return input_types or ["text"], output_types or ["free_text"]


def _make_harness(
    sandbox_dir: Path,
    candidate: ScreenedCandidate,
    test_cases: list[TestCase],
) -> TestHarness:
    from puzzleeval.agents.agent5.sandbox import extract_env_vars_from_code

    code = _read_text(sandbox_dir / "harness.py")
    auth_env_vars = extract_env_vars_from_code(code)
    if not auth_env_vars and str(getattr(candidate, "auth_method", "") or "").lower() not in {"no_auth", "none", "public"}:
        provider_slug = re.sub(r"[^A-Z0-9]+", "_", str(candidate.provider).upper()).strip("_") or "API"
        auth_env_vars = [f"{provider_slug}_API_KEY"]
    input_types, output_types = _supported_types(test_cases)
    return TestHarness(
        candidate_name=candidate.name,
        provider=candidate.provider,
        harness_dir=str(sandbox_dir),
        entry_file="harness.py",
        requirements=_read_requirements(sandbox_dir),
        auth_env_vars=auth_env_vars,
        auth_method=candidate.auth_method or "unknown",
        supported_input_types=input_types,
        supported_output_types=output_types,
        smoke_test_passed=True,
        validation_notes="representative probe candidate harness",
        build_turns=0,
        build_cost_usd=0.0,
        harness_code=code,
    )


def _set_probe_session_dir(sandbox_dir: Path) -> None:
    try:
        from puzzleeval.tool_plugins import list_plugins

        voice_dir = sandbox_dir / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True)
        for plugin in list_plugins():
            if hasattr(plugin, "set_session_dir"):
                try:
                    plugin.set_session_dir(voice_dir)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


def _probe_one(
    *,
    client: Any,
    sandbox_dir: Path,
    candidate: ScreenedCandidate,
    harness: TestHarness,
    tc: TestCase,
    credentials: dict[str, str] | None,
    logger: logging.Logger,
    trace_id: str,
    progress_callback: Callable[[str, dict], None] | None,
) -> dict[str, Any]:
    from puzzleeval.agents.agent5.execution import (
        PersistentHarnessSession,
        adapt_test_input,
        execute_all_tests,
        execute_test_with_session_retry,
    )
    from puzzleeval.agents.agent5.runtime_policy import (
        RUNTIME_PERSISTENT_WORKER,
        select_runtime_primitive,
    )
    from puzzleeval.plugin_tool_runner import evaluate_with_tool_runner

    _set_probe_session_dir(sandbox_dir)
    multi_call = _is_multi_call(tc)
    payloads_seen: list[dict[str, Any]] = []
    release_callbacks: list[Callable[[], None]] = []

    if multi_call:
        raw_result = {
            "output": "",
            "latency_ms": 0.0,
            "tokens_used": None,
            "cost_usd": None,
            "raw_response": {"_representative_probe_multi_call_placeholder": True},
            "success": True,
            "error": None,
        }
    else:
        raw_results = execute_all_tests(
            sandbox_dir,
            [tc],
            harness,
            credentials,
            logger,
            trace_id,
        )
        raw_result = raw_results[0][1] if raw_results else {"success": False, "error": "no result"}

    runner_for_probe = None
    if multi_call:
        persistent_session = None
        decision = select_runtime_primitive(sandbox_dir=sandbox_dir, caps=None)
        if decision.mode == RUNTIME_PERSISTENT_WORKER:
            try:
                persistent_session = PersistentHarnessSession(
                    sandbox_dir,
                    credentials,
                    turn_timeout=AGENT6_TEST_TIMEOUT,
                    logger=logger,
                    trace_id=trace_id,
                    candidate_name=candidate.name,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "representative probe persistent session startup failed: %s",
                    exc,
                    extra={"operation": "representative_probe_session_start_failed", "trace_id": trace_id},
                )
                persistent_session = None

        def _release() -> None:
            if persistent_session is not None:
                persistent_session.close()

        release_callbacks.append(_release)
        default_context = _input_context_default(candidate.name, getattr(tc, "sub_task_ref", None))

        def _runner(payload: Any) -> dict[str, Any]:
            if not isinstance(payload, dict):
                payload = {"payload": payload}
            merged = dict(payload)
            payload_ctx = merged.get("input_context")
            merged["input_context"] = _merge_input_context(
                payload_ctx if isinstance(payload_ctx, dict) else getattr(tc, "input_context", None),
                default_context,
            )
            if "input_type" not in merged and tc.input_type:
                merged["input_type"] = tc.input_type
            payloads_seen.append(_payload_summary(merged))
            if persistent_session is not None:
                return persistent_session.turn(merged)
            return execute_test_with_session_retry(
                sandbox_dir,
                merged,
                credentials,
                AGENT6_TEST_TIMEOUT,
                logger,
                trace_id,
                candidate.name,
            )

        runner_for_probe = _runner

    def _progress(event_type: str, payload: dict[str, Any]) -> None:
        if progress_callback is None:
            return
        data = dict(payload or {})
        data.setdefault("candidate_name", candidate.name)
        data.setdefault("test_case_id", tc.id)
        progress_callback(event_type, data)

    criteria = [
        {
            "criterion": item.criterion,
            "eval_type": item.eval_type,
            "weight": item.weight,
        }
        for item in (getattr(tc, "judgement_criteria", []) or [])
    ]
    try:
        verdict = evaluate_with_tool_runner(
            client=client,
            response=raw_result.get("raw_response") or raw_result.get("output"),
            expected=tc.expected_output,
            criteria=criteria,
            test_scenario=tc.scenario,
            harness_runner=runner_for_probe,
            input_type=tc.input_type,
            output_type=tc.output_type,
            trace_id=trace_id,
            persona=getattr(tc, "persona", None),
            goal=getattr(tc, "goal", None),
            constraints=getattr(tc, "constraints", None) or [],
            rubric=getattr(tc, "rubric", None) or [],
            max_turns=getattr(tc, "max_turns", 4) or 4,
            evaluation_mode=getattr(tc, "evaluation_mode", "auto"),
            input_context=getattr(tc, "input_context", None) or {},
            progress_callback=_progress,
            release_harness_session=release_callbacks[0] if release_callbacks else None,
        )
        raw_summary = _summarize_result(raw_result)
        artifact_count = len(verdict.artifacts or [])
        empty_success_without_evidence = (
            _requires_observable_output(tc)
            and raw_summary["success"]
            and raw_summary["output_chars"] == 0
            and raw_summary["audio_path_count"] == 0
            and not raw_summary["has_merged_audio"]
            and artifact_count == 0
        )
        passed = (
            bool(raw_result.get("success"))
            and bool(verdict.passed)
            and not verdict.fallback_reason
            and not empty_success_without_evidence
        )
        failure_text = "\n".join(
            str(part or "")
            for part in (
                raw_result.get("error"),
                verdict.fallback_reason,
                verdict.reasoning,
                "empty_success_without_output_evidence" if empty_success_without_evidence else "",
            )
        )
        external_block = _external_block_reason(failure_text)
        return {
            "test_case_id": tc.id,
            "family_key": (
                f"input={tc.input_type}|output={tc.output_type}|"
                f"mode={getattr(tc, 'evaluation_mode', 'auto')}"
            ),
            "test_case": _test_case_probe_summary(tc),
            "status": "passed" if passed else ("external_block" if external_block else "failed"),
            "external_block_reason": external_block,
            "raw_result": raw_summary,
            "input_observation": {
                "payloads_seen_count": len(payloads_seen),
                "expected_input_reached_harness": (
                    "observed_payloads"
                    if payloads_seen
                    else ("single_call_adapter_path" if not multi_call else "not_observed")
                ),
                "requires_observable_output": _requires_observable_output(tc),
                "empty_success_without_output_evidence": empty_success_without_evidence,
            },
            "payloads_supplied_to_harness": payloads_seen,
            "verdict": {
                "passed": bool(verdict.passed),
                "score": float(verdict.score or 0.0),
                "reasoning": str(verdict.reasoning or "")[:2000],
                "tools_invoked": list(verdict.tools_invoked or []),
                "fallback_reason": verdict.fallback_reason,
                "artifact_count": len(verdict.artifacts or []),
                "artifacts": verdict.artifacts or [],
                "detail_keys": sorted(verdict.verdict_detail.keys())
                if isinstance(getattr(verdict, "verdict_detail", None), dict)
                else [],
            },
        }
    finally:
        for release in release_callbacks:
            try:
                release()
            except Exception:  # noqa: BLE001
                pass


def run_representative_probe_gate(
    *,
    client: Any,
    sandbox_dir: Path,
    candidate: ScreenedCandidate,
    input_data: Agent5Input,
    staged_test_cases: list[TestCase],
    credentials: dict[str, str] | None,
    logger: logging.Logger,
    trace_id: str,
    progress_callback: Callable[[str, dict], None] | None = None,
) -> str | None:
    """Run or reuse representative probe evidence.

    Returns a completion-gate issue string when the probe exposes a real
    production-path failure. Missing credentials are recorded truthfully but do
    not block offline builds.
    """

    selected = representative_test_cases(sandbox_dir, staged_test_cases)
    if not selected:
        _write_evidence(sandbox_dir, {
            "status": "not_applicable",
            "reason": "No test cases available for representative probe.",
            "probe_hash": "none",
            "selected_test_ids": [],
        })
        return None

    probe_hash = _hash_probe_inputs(sandbox_dir, selected)
    cached = _load_cached_evidence(sandbox_dir, probe_hash)
    if cached and cached.get("status") in {"passed", "skipped_no_credentials", "external_block", "failed"}:
        if cached.get("status") == "failed":
            return str(cached.get("gate_issue") or "representative probe failed")
        return None

    if _needs_credentials(candidate, credentials):
        _write_evidence(sandbox_dir, {
            "status": "skipped_no_credentials",
            "reason": "Provider credentials were not available; production-equivalence probe was recorded as skipped.",
            "probe_hash": probe_hash,
            "selected_test_ids": [tc.id for tc in selected],
        })
        return None

    harness = _make_harness(sandbox_dir, candidate, selected)
    results: list[dict[str, Any]] = []
    if progress_callback:
        progress_callback("representative_probe_started", {
            "candidate_name": candidate.name,
            "selected_test_ids": [tc.id for tc in selected],
        })
    for tc in selected:
        try:
            results.append(_probe_one(
                client=client,
                sandbox_dir=sandbox_dir,
                candidate=candidate,
                harness=harness,
                tc=tc,
                credentials=credentials,
                logger=logger,
                trace_id=trace_id,
                progress_callback=progress_callback,
            ))
        except Exception as exc:  # noqa: BLE001
            results.append({
                "test_case_id": tc.id,
                "status": "failed",
                "exception": type(exc).__name__,
                "reason": str(exc)[:1000],
            })

    failed = [item for item in results if item.get("status") == "failed"]
    external = [item for item in results if item.get("status") == "external_block"]
    if failed:
        gate_issue = (
            "representative_probe: production-equivalence probe failed for "
            f"{', '.join(str(item.get('test_case_id')) for item in failed)}. "
            "The harness must pass a representative real test-case path, or "
            "record a genuine external provider block."
        )
        status = "failed"
    elif external:
        gate_issue = None
        status = "external_block"
    else:
        gate_issue = None
        status = "passed"

    _write_evidence(sandbox_dir, {
        "status": status,
        "gate_issue": gate_issue,
        "probe_hash": probe_hash,
        "trace_id": trace_id,
        "candidate_name": candidate.name,
        "selected_test_ids": [tc.id for tc in selected],
        "results": results,
        "guidance": (
            "This evidence comes from representative Agent 3 test cases run "
            "through the same harness-runner/plugin-evaluator path used by "
            "final evaluation. live_test.py remains the builder's self-check; "
            "this file is the production-equivalence proof."
        ),
    })
    if progress_callback:
        progress_callback("representative_probe_completed", {
            "candidate_name": candidate.name,
            "status": status,
            "selected_test_ids": [tc.id for tc in selected],
        })
    return gate_issue


__all__ = [
    "REPRESENTATIVE_PROBE_EVIDENCE_RELATIVE_PATH",
    "representative_probe_evidence_path",
    "run_representative_probe_gate",
]
