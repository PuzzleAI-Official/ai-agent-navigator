"""Evidence-grounded semantic review helpers for scripted conversations."""

from __future__ import annotations

import logging
from typing import Any

from puzzleeval.schemas import ConversationTurn, Persona, RubricCriterion

logger = logging.getLogger(__name__)


def _assertion_text(assertions: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in assertions:
        check_type = str(item.get("check_type") or "contains")
        value = str(item.get("value") or item.get("expected") or "").strip()
        if value:
            parts.append(f"{check_type}: {value}")
    return "; ".join(parts[:12]) or "The agent should respond coherently to the scripted user turns."


def review_scripted_conversation_semantics(
    *,
    transcript: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
    candidate_role: str = "",
    agent_system_prompt: str = "",
    client: Any = None,
    trace_id: str = "no-trace",
) -> dict[str, Any]:
    """Use the bounded rubric judge for scripted semantic equivalence.

    Deterministic scripted checks remain useful as evidence signals, but this
    helper is the semantic verdict path when an Anthropic client/API key is
    available. It returns ``available=False`` on judge setup/runtime failure so
    local no-key tests do not perform network work.
    """

    turns = [
        ConversationTurn(
            turn_index=int(item.get("turn_index", index)),
            role="agent" if str(item.get("role")) == "agent" else "user",
            text=str(item.get("text") or ""),
            meta=dict(item.get("meta") or {}),
        )
        for index, item in enumerate(transcript)
    ]
    if not turns:
        return {
            "available": True,
            "passed": False,
            "score": 0.0,
            "reason": "no transcript to review",
        }

    expectation_summary = _assertion_text(assertions)
    persona = Persona(
        name="Scripted evaluator",
        demographics="Evaluation harness",
        emotional_state="neutral",
    )
    rubric = [
        RubricCriterion(
            name="scripted_expectations",
            description=(
                "Judge whether the agent responses semantically satisfy these "
                f"scripted expectations without requiring exact wording: {expectation_summary}"
            ),
            weight=1.0,
            critical=True,
            min_passing_score=0.5,
        )
    ]
    try:
        from puzzleeval.rubric_judge import judge_conversation

        verdict = judge_conversation(
            transcript=turns,
            persona=persona,
            goal=f"Satisfy scripted evaluation expectations: {expectation_summary}",
            rubric=rubric,
            candidate_role=candidate_role,
            agent_system_prompt=agent_system_prompt,
            client=client,
            trace_id=trace_id,
        )
    except Exception as exc:  # noqa: BLE001 - semantic review must not crash eval
        logger.warning(
            "scripted semantic review unavailable: %s",
            exc,
            extra={"operation": "scripted_semantic_review_unavailable"},
        )
        return {
            "available": False,
            "passed": None,
            "score": None,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    return {
        "available": True,
        "passed": bool(verdict.passed),
        "score": float(verdict.overall_score),
        "reason": verdict.conversation_summary,
        "rubric_verdict": verdict.model_dump(),
    }


__all__ = ["review_scripted_conversation_semantics"]
