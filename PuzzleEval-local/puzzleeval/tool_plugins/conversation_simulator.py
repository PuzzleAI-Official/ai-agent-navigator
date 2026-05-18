"""Conversation simulator â€” multi-turn test runner for chatbot/inbound agents.

Single-shot request/response harness can't evaluate "does the bot keep
state across 5 turns + ask the right clarifying questions?" or
"does lead qualification correctly branch when the prospect says they
don't have a budget?" The conversation simulator drives a multi-turn
script:

  - Each test case carries a ``conversation`` script: a list of
    ``{"role": "user", "content": "..."}`` turns plus assertions
    about what the agent should say at each point.
  - The plugin replays the script against the candidate's harness,
    feeding each user turn in sequence and capturing the agent's
    response.
  - At each turn (or at the end), assertions are checked: did the agent
    mention required topics? did it stay on intent? did it ask the
    expected clarifying question?

The simulator is harness-agnostic â€” it calls the candidate's
``run(input_data)`` with conversation history in the input. The shape
is whatever the harness expects; common forms:

  ``{"messages": [{"role": "user", "content": "..."}, ...]}``  (OpenAI-style)
  ``{"conversation": [...], "session_id": "X"}``               (session-keyed)
  ``{"user_input": "...", "history": [...]}``                  (legacy)

The plugin picks the shape based on a hint in
``input_context.conversation_format`` (default: ``messages``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from puzzleeval.tool_plugins import (
    EvaluationResult,
    HARNESS_EXECUTION_SINGLE_CALL,
    PluginCapabilities,
    SynthesisResult,
    ToolPlugin,
    register_plugin,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Script shape
# ---------------------------------------------------------------------------


@dataclass
class ConversationAssertion:
    """A single check applied at a specific turn (or at end-of-conversation).

    ``turn_index``: 0-based index of the agent turn this assertion checks.
                    -1 means the FINAL agent turn.
    ``check_type``: ``contains`` / ``not_contains`` / ``regex_match`` are
                    deterministic evidence extractors; ``intent_match`` and
                    production scripted evaluation use semantic review.
    ``value``: the literal/regex/expected intent to evaluate.
    """
    turn_index: int
    check_type: str
    value: str
    weight: float = 1.0


@dataclass
class ConversationScript:
    """A multi-turn test script."""
    user_turns: list[str]
    assertions: list[ConversationAssertion] = field(default_factory=list)
    initial_context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class ConversationSimulatorPlugin(ToolPlugin):
    """Drives a multi-turn conversation against a candidate harness."""

    name = "conversation_simulator"

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            input_types=["conversation"],
            output_types=["free_text", "structured_json"],
            synthesizes_input=True,
            evaluates_output=True,
            requires_credentials=[],
            # Drives the harness: needs a harness_runner to invoke
            # harness.run() once per turn in the conversation script.
            requires_harness_runner=True,
            harness_execution_mode=HARNESS_EXECUTION_SINGLE_CALL,
            notes=(
                "Multi-turn replay. Harness-agnostic: works with any "
                "{messages: [...]} / {conversation: [...]} / "
                "{user_input, history} shape. Format selectable via "
                "input_context.conversation_format."
            ),
        )

    # -- input synthesis --------------------------------------------------

    def synthesize_input(
        self, *, scope_role: str, ground_truth_hint: str | None = None,
        **kwargs: Any,
    ) -> SynthesisResult:
        """Build a domain-neutral 3-turn conversation script.

        Real test cases override this with detailed scripts. The default
        seed exercises: greeting â†’ information request â†’ follow-up.
        """
        script = ConversationScript(
            user_turns=[
                "Hi, I have a question.",
                "Can you tell me about what you can help with?",
                "Thanks, that's all I needed.",
            ],
            assertions=[
                ConversationAssertion(
                    turn_index=0, check_type="not_contains", value="error",
                ),
                ConversationAssertion(
                    turn_index=-1, check_type="not_contains", value="error",
                ),
            ],
        )
        return SynthesisResult(
            inline_data={
                "conversation_script": {
                    "user_turns": script.user_turns,
                    "assertions": [
                        {
                            "turn_index": a.turn_index,
                            "check_type": a.check_type,
                            "value": a.value,
                            "weight": a.weight,
                        }
                        for a in script.assertions
                    ],
                }
            },
            ground_truth={"turn_count": len(script.user_turns)},
            notes="default 3-turn greeting/info/farewell seed",
        )

    # -- output evaluation ------------------------------------------------

    def evaluate_output(
        self, *, response: Any, expected: Any,
        criteria: list[dict] | None = None,
        harness_runner: Callable[[dict], dict] | None = None,
        conversation_format: str = "messages",
        **kwargs: Any,
    ) -> EvaluationResult:
        """Drive a multi-turn chatbot conversation (scripted OR agentic).

        Two modes, selected automatically based on what's in ``expected``:

            **Scripted**: ``expected`` carries a ``conversation_script`` with
            user_turns[] + assertions[]. Each user turn is fed to the harness
            in order; deterministic assertions become evidence signals, and
            production callers may require evidence-grounded semantic review.

            **Agentic** (new): when kwargs contain ``persona`` + ``goal`` +
            ``rubric``, the plugin runs user_simulator to generate each
            user turn reactively and rubric_judge to score the full
            transcript. ``expected`` can be empty in this mode (all
            context comes from the kwargs).

        Mode resolution:
          1. PUZZLEEVAL_CONVERSATION_EVAL_MODE global override (if non-'auto')
          2. kwargs['evaluation_mode'] explicit setting
          3. 'auto' (default): agentic when persona+goal+rubric present; else scripted

        ``harness_runner`` is a callable that takes the per-turn input dict
        and returns a dict matching Agent 5's standard test response shape
        (``{success, output, latency_ms, raw_response, error}``). Same
        contract in both modes.

        ``response`` is unused â€” this plugin DRIVES the harness rather
        than judging an existing response.

        Agentic kwargs:
            persona: Persona
            goal: str
            constraints: list[str]
            rubric: list[RubricCriterion]
            max_turns: int
            evaluation_mode: 'auto' | 'agentic' | 'scripted'
            anthropic_client: optional; built lazily if omitted
            trace_id: correlation ID for logs
        """
        # â”€â”€ Resolve mode â”€â”€
        try:
            from puzzleeval.config import CONVERSATION_EVAL_MODE as _GLOBAL_MODE
        except Exception:  # pragma: no cover
            _GLOBAL_MODE = "auto"
        explicit_mode = kwargs.get("evaluation_mode", "auto")
        effective_mode = (
            _GLOBAL_MODE if _GLOBAL_MODE != "auto" else explicit_mode
        )
        persona = kwargs.get("persona")
        goal = kwargs.get("goal")
        rubric = kwargs.get("rubric")
        # Merge caller-provided input_context into expected so the
        # downstream agent_system_prompt extraction has ONE unified
        # source. Caller's wins on key conflict â€” Agent 5 knows the
        # authoritative TestCase.input_context.
        _caller_ic = kwargs.get("input_context")
        if _caller_ic and isinstance(expected, dict):
            merged_ic = dict(expected.get("input_context") or {})
            merged_ic.update(_caller_ic)
            expected = dict(expected)
            expected["input_context"] = merged_ic
        elif _caller_ic:
            # `expected` wasn't a dict â€” synthesize a minimal wrapper
            # so the input_context reaches the judge.
            expected = {"input_context": dict(_caller_ic)}

        if effective_mode == "auto":
            if persona is not None and goal and rubric:
                effective_mode = "agentic"
            else:
                effective_mode = "scripted"

        if effective_mode == "agentic" and not (
            persona is not None and goal and rubric
        ):
            # Missing pieces â€” fall back to scripted
            logger.warning(
                "conversation_simulator: agentic mode requested but "
                "persona/goal/rubric incomplete â€” falling back to scripted.",
                extra={"operation": "agentic_mode_fallback"},
            )
            effective_mode = "scripted"

        if effective_mode == "agentic":
            if harness_runner is None:
                return EvaluationResult(
                    passed=False, score=0.0,
                    reasoning="no harness_runner provided to drive the conversation",
                    fallback_reason="no_runner",
                )
            # Pull the agent's configured system prompt out of expected's
            # input_context (alias list matches voice_realtime +
            # AD-007 runner-level merge). Rubric judge uses it as
            # ground truth for scope/policy scoring.
            agent_system_prompt = ""
            if isinstance(expected, dict):
                ic = expected.get("input_context")
                if isinstance(ic, dict):
                    for _alias in ("instructions", "system_prompt",
                                   "system", "brief", "agent_prompt"):
                        _val = ic.get(_alias)
                        if isinstance(_val, str) and _val.strip():
                            agent_system_prompt = _val.strip()
                            break
                if not agent_system_prompt:
                    _val = expected.get("instructions")
                    if isinstance(_val, str) and _val.strip():
                        agent_system_prompt = _val.strip()
            return self._evaluate_agentic(
                persona=persona, goal=goal,
                constraints=kwargs.get("constraints") or [],
                rubric=rubric,
                max_turns=kwargs.get("max_turns") or 4,
                harness_runner=harness_runner,
                conversation_format=conversation_format,
                anthropic_client=kwargs.get("anthropic_client"),
                trace_id=kwargs.get("trace_id", "no-trace"),
                evaluation_mode=effective_mode,
                agent_system_prompt=agent_system_prompt,
            )

        # â”€â”€ Scripted path â”€â”€
        script = self._extract_script(expected)
        if script is None:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning="no conversation_script in expected payload",
                fallback_reason="no_script",
            )
        if harness_runner is None:
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning="no harness_runner provided to drive the conversation",
                fallback_reason="no_runner",
            )
        agent_system_prompt = ""
        if isinstance(expected, dict):
            ic = expected.get("input_context")
            if isinstance(ic, dict):
                for _alias in ("instructions", "system_prompt", "system", "brief", "agent_prompt"):
                    _val = ic.get(_alias)
                    if isinstance(_val, str) and _val.strip():
                        agent_system_prompt = _val.strip()
                        break
        return self._evaluate_scripted(
            script=script, harness_runner=harness_runner,
            conversation_format=conversation_format,
            anthropic_client=kwargs.get("anthropic_client"),
            trace_id=kwargs.get("trace_id", "no-trace"),
            agent_system_prompt=agent_system_prompt,
            semantic_review_required=bool(kwargs.get("semantic_review_required")),
        )

    def _evaluate_scripted(
        self,
        *,
        script: "ConversationScript",
        harness_runner: Callable[[dict], dict],
        conversation_format: str,
        anthropic_client=None,
        trace_id: str = "no-trace",
        agent_system_prompt: str = "",
        semantic_review_required: bool = False,
    ) -> EvaluationResult:
        """Script-driven replay with semantic review when available."""
        history: list[dict[str, str]] = []
        transcript: list[dict[str, Any]] = []
        agent_turns: list[str] = []
        per_turn_errors: list[str] = []
        for turn_index, user_turn in enumerate(script.user_turns):
            history.append({"role": "user", "content": user_turn})
            transcript.append({
                "turn_index": len(transcript),
                "role": "user",
                "text": user_turn,
                "meta": {"script_turn_index": turn_index},
            })
            payload = self._payload_for_format(history, conversation_format)
            try:
                turn_result = harness_runner(payload)
            except Exception as exc:
                per_turn_errors.append(f"runner crash: {exc}")
                break
            if not isinstance(turn_result, dict) or not turn_result.get("success"):
                per_turn_errors.append(
                    f"harness failure: {(turn_result or {}).get('error', 'unknown')[:200]}"
                )
                break
            output = turn_result.get("output")
            agent_text = self._extract_agent_text(output)
            agent_turns.append(agent_text)
            history.append({"role": "assistant", "content": agent_text})
            transcript.append({
                "turn_index": len(transcript),
                "role": "agent",
                "text": agent_text,
                "meta": {"script_turn_index": turn_index},
            })

        # Score assertions
        passed_weights = 0.0
        total_weights = 0.0
        assertion_detail: list[dict[str, Any]] = []
        for assertion in script.assertions:
            total_weights += assertion.weight
            idx = assertion.turn_index
            if idx == -1:
                idx = len(agent_turns) - 1
            if idx < 0 or idx >= len(agent_turns):
                assertion_detail.append({
                    "assertion": assertion.value,
                    "passed": False,
                    "reason": "no agent turn at requested index",
                })
                continue
            agent_text = agent_turns[idx]
            ok = self._check(agent_text, assertion)
            assertion_detail.append({
                "assertion": f"{assertion.check_type}: {assertion.value!r}",
                "turn": idx,
                "passed": ok,
            })
            if ok:
                passed_weights += assertion.weight

        score = (passed_weights / total_weights) if total_weights else (
            1.0 if not per_turn_errors else 0.0
        )
        passed = (score >= 0.6) and not per_turn_errors
        semantic_review = None
        if semantic_review_required and not per_turn_errors and script.assertions:
            from puzzleeval.semantic_review import review_scripted_conversation_semantics

            semantic_review = review_scripted_conversation_semantics(
                transcript=transcript,
                assertions=[
                    {
                        "turn_index": assertion.turn_index,
                        "check_type": assertion.check_type,
                        "value": assertion.value,
                        "weight": assertion.weight,
                    }
                    for assertion in script.assertions
                ],
                candidate_role="text conversation harness",
                agent_system_prompt=agent_system_prompt,
                client=anthropic_client,
                trace_id=trace_id,
            )
            if semantic_review.get("available"):
                passed = bool(semantic_review.get("passed"))
                score = float(semantic_review.get("score") or 0.0)
        return EvaluationResult(
            passed=passed,
            score=score,
            reasoning=(
                f"{len(agent_turns)} turn(s) executed; "
                f"{sum(1 for d in assertion_detail if d['passed'])}/"
                f"{len(assertion_detail)} assertions passed"
                + ("; semantic review applied" if semantic_review and semantic_review.get("available") else "")
                + (f"; errors: {per_turn_errors}" if per_turn_errors else "")
            ),
            detail={
                "agent_turns": agent_turns,
                "assertion_results": assertion_detail,
                "per_turn_errors": per_turn_errors,
                "evaluation_mode": "scripted",
                "semantic_review": semantic_review,
            },
        )

    def _evaluate_agentic(
        self,
        *,
        persona,
        goal: str,
        constraints: list[str],
        rubric,
        max_turns: int,
        harness_runner: Callable[[dict], dict],
        conversation_format: str,
        anthropic_client,
        trace_id: str,
        evaluation_mode: str,
        agent_system_prompt: str = "",
    ) -> EvaluationResult:
        """Agentic drive: user_simulator turns + rubric_judge scoring.

        Mirrors voice_realtime._drive_conversation_agentic but for text â€”
        no TTS/STT needed, conversation is pure text from first turn to
        judge. Same contract as scripted path for the harness_runner
        (receives payload dict, returns {success, output, ...}).

        The user text for each turn is produced by ``user_simulator``
        based on the transcript so far; after the loop, ``rubric_judge``
        scores the complete exchange. Both modules are imported lazily
        so tests that only exercise the scripted path don't need their
        dependencies loaded.
        """
        from puzzleeval.config import CONVERSATION_MAX_TURNS_CEILING
        from puzzleeval.user_simulator import generate_next_user_turn
        from puzzleeval.rubric_judge import judge_conversation
        from puzzleeval.schemas import ConversationTurn

        resolved_max = min(max_turns, CONVERSATION_MAX_TURNS_CEILING)
        transcript: list[ConversationTurn] = []
        simulator_cost = 0.0
        sim_end_reason = "ongoing"
        per_turn_errors: list[str] = []

        # message-shape chat history (the payload we feed the harness)
        chat_history: list[dict[str, str]] = []

        turn_idx = 0
        while turn_idx < resolved_max:
            # 1. simulator generates next user turn
            try:
                sim_turn = generate_next_user_turn(
                    history=transcript,
                    persona=persona,
                    goal=goal,
                    constraints=constraints,
                    turn_index=turn_idx,
                    max_turns=resolved_max,
                    client=anthropic_client,
                    trace_id=trace_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "simulator crashed at turn %d: %s", turn_idx, exc,
                    extra={"operation": "simulator_crash",
                           "trace_id": trace_id},
                )
                sim_end_reason = "abandoned"
                break
            simulator_cost += sim_turn.cost_usd

            user_text = (sim_turn.text or "").strip()
            transcript.append(ConversationTurn(
                turn_index=turn_idx * 2,
                role="user",
                text=user_text,
                meta={
                    "end_reason": sim_turn.end_reason,
                    "simulator_cost_usd": sim_turn.cost_usd,
                },
            ))

            if sim_turn.end_conversation:
                sim_end_reason = sim_turn.end_reason
                break
            if not user_text:
                sim_end_reason = "abandoned"
                break

            # 2. feed chat_history + new user message to harness
            chat_history.append({"role": "user", "content": user_text})
            payload = self._payload_for_format(chat_history, conversation_format)
            try:
                turn_result = harness_runner(payload)
            except Exception as exc:
                per_turn_errors.append(f"runner crash: {exc}")
                transcript.append(ConversationTurn(
                    turn_index=turn_idx * 2 + 1,
                    role="agent",
                    text=f"[AGENT ERROR: {exc}]",
                    meta={"error": str(exc)},
                ))
                sim_end_reason = "agent_failed"
                break
            if not isinstance(turn_result, dict) or not turn_result.get("success"):
                err = (turn_result or {}).get("error", "unknown")
                per_turn_errors.append(f"harness failure: {str(err)[:200]}")
                transcript.append(ConversationTurn(
                    turn_index=turn_idx * 2 + 1,
                    role="agent",
                    text=f"[HARNESS FAILURE: {err}]",
                    meta={"error": str(err)},
                ))
                sim_end_reason = "agent_failed"
                break

            output = turn_result.get("output")
            agent_text = self._extract_agent_text(output)
            chat_history.append({"role": "assistant", "content": agent_text})
            transcript.append(ConversationTurn(
                turn_index=turn_idx * 2 + 1,
                role="agent",
                text=agent_text[:2000] if agent_text else "",
                meta={},
            ))
            turn_idx += 1

        if sim_end_reason == "ongoing" and turn_idx >= resolved_max:
            sim_end_reason = "max_turns"

        # 3. judge the full transcript
        judge_cost = 0.0
        rubric_verdict = None
        try:
            rubric_verdict = judge_conversation(
                transcript=transcript,
                persona=persona,
                goal=goal,
                rubric=rubric,
                candidate_role="",  # text chat â€” no scope_role wiring here
                agent_system_prompt=agent_system_prompt,
                # Use rubric_judge's bounded client policy. The simulator may
                # use a broader client for turn generation, but transcript
                # judging has no server tools and should fail this test rather
                # than consume the general API timeout budget.
                client=None,
                trace_id=trace_id,
            )
            judge_cost = rubric_verdict.cost_usd
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "rubric_judge crashed: %s", exc,
                extra={"operation": "rubric_judge_crash",
                       "trace_id": trace_id},
            )

        overall_score = rubric_verdict.overall_score if rubric_verdict else 0.0
        overall_passed = rubric_verdict.passed if rubric_verdict else False

        return EvaluationResult(
            passed=overall_passed,
            score=overall_score,
            reasoning=(
                rubric_verdict.conversation_summary
                if rubric_verdict else
                (
                    f"{len(transcript)} turn(s) executed; "
                    f"end_reason={sim_end_reason}; "
                    f"errors={per_turn_errors or 'none'}"
                )
            ),
            detail={
                "evaluation_mode": evaluation_mode,
                "transcript": [t.model_dump() for t in transcript],
                "rubric_verdict": (
                    rubric_verdict.model_dump() if rubric_verdict else None
                ),
                "simulator_cost_usd": round(simulator_cost, 6),
                "judge_cost_usd": round(judge_cost, 6),
                "sim_end_reason": sim_end_reason,
                "per_turn_errors": per_turn_errors,
                # Legacy-shape compatibility for callers that read agent_turns:
                "agent_turns": [
                    t.text for t in transcript if t.role == "agent"
                ],
            },
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _extract_script(expected: Any) -> ConversationScript | None:
        if isinstance(expected, ConversationScript):
            return expected
        if isinstance(expected, dict):
            raw = expected.get("conversation_script") or expected
            user_turns = raw.get("user_turns") if isinstance(raw, dict) else None
            if not isinstance(user_turns, list) or not user_turns:
                return None
            assertions_raw = raw.get("assertions") or []
            assertions = []
            for a in assertions_raw:
                if isinstance(a, dict):
                    assertions.append(ConversationAssertion(
                        turn_index=int(a.get("turn_index", -1)),
                        check_type=str(a.get("check_type", "contains")),
                        value=str(a.get("value", "")),
                        weight=float(a.get("weight", 1.0)),
                    ))
            return ConversationScript(
                user_turns=[str(t) for t in user_turns],
                assertions=assertions,
                initial_context=raw.get("initial_context", {}) if isinstance(raw, dict) else {},
            )
        return None

    @staticmethod
    def _payload_for_format(
        history: list[dict[str, str]], conversation_format: str,
    ) -> dict[str, Any]:
        fmt = (conversation_format or "messages").lower()
        if fmt == "messages":
            return {"messages": list(history)}
        if fmt == "conversation":
            return {"conversation": list(history)}
        if fmt == "history":
            return {
                "user_input": history[-1]["content"] if history else "",
                "history": list(history[:-1]),
            }
        # Generic fallback â€” pass both
        return {
            "messages": list(history),
            "user_input": history[-1]["content"] if history else "",
            "history": list(history[:-1]),
        }

    @staticmethod
    def _extract_agent_text(output: Any) -> str:
        """Pull the agent's textual reply out of various harness output shapes."""
        if isinstance(output, str):
            return output
        if isinstance(output, dict):
            for key in ("text", "message", "content", "reply", "answer", "output"):
                value = output.get(key)
                if isinstance(value, str):
                    return value
                if isinstance(value, dict):
                    for inner in ("text", "content"):
                        v2 = value.get(inner)
                        if isinstance(v2, str):
                            return v2
        return str(output)[:2000]

    @staticmethod
    def _check(agent_text: str, assertion: ConversationAssertion) -> bool:
        text_lc = (agent_text or "").lower()
        value = assertion.value or ""
        ct = assertion.check_type
        if ct == "contains":
            return value.lower() in text_lc
        if ct == "not_contains":
            return value.lower() not in text_lc
        if ct == "regex_match":
            import re
            try:
                return bool(re.search(value, agent_text or "", re.IGNORECASE))
            except re.error:
                return False
        if ct == "intent_match":
            # Light heuristic: count token overlap between the assertion's
            # intent description and the agent text. >= 30% overlap = match.
            # The Agent 5 LLM judge picks up the nuanced cases when this fails.
            import re
            atoks = set(re.findall(r"[a-z0-9']+", text_lc))
            vtoks = set(re.findall(r"[a-z0-9']+", value.lower()))
            if not vtoks:
                return True
            return (len(atoks & vtoks) / len(vtoks)) >= 0.3
        return False


register_plugin(ConversationSimulatorPlugin())


__all__ = [
    "ConversationAssertion",
    "ConversationScript",
    "ConversationSimulatorPlugin",
]


