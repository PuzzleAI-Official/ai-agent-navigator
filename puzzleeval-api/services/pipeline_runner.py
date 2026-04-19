import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("puzzleeval.pipeline_runner")

from fastapi import HTTPException

from datetime import datetime, timezone

from services.billing import require_agent_access
from services.run_manager import RunState

# Path to working_test_6 mock data
MOCK_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "PuzzleEval-local" / "runs" / "working_test_6"

# PuzzleEval package location
PUZZLEEVAL_DIR = Path(__file__).resolve().parent.parent.parent / "PuzzleEval-local"
if str(PUZZLEEVAL_DIR) not in sys.path:
    sys.path.insert(0, str(PUZZLEEVAL_DIR))

# API server root (for runs directory)
API_ROOT = Path(__file__).resolve().parent.parent


def _compute_avg_weighted_score(candidate_run: dict) -> float:
    """Compute average weighted_score across test results. This is the real quality metric."""
    test_results = candidate_run.get("test_results", [])
    if not test_results:
        return 0.0
    scores = [tr.get("weighted_score", 0) for tr in test_results]
    return sum(scores) / len(scores) if scores else 0.0


def _load_mock(filename: str) -> dict:
    path = MOCK_DATA_DIR / filename
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================================
# Mock Agent 1 — scripted conversation, no API calls
# ============================================================================

MOCK_AGENT1_CLARIFICATIONS = [
    "What types of documents do you typically need to process? (PDFs, photos, scanned images, etc.)",
    "What specific data fields do you need extracted from these documents?",
    "What is your approximate monthly document volume?",
]


async def mock_agent1_turn(state: RunState, user_message: str) -> dict:
    await asyncio.sleep(0.8)
    state.current_turn += 1
    state.conversation_history.append({"role": "user", "content": user_message})

    if state.current_turn < 2:
        response = {
            "is_clear": False,
            "assistant_message": "Thanks for that context. I have a few questions to make sure I find the best solutions for you:",
            "clarifying_questions": MOCK_AGENT1_CLARIFICATIONS,
        }
    else:
        mock_output = _load_mock("agent_1_output.json")
        summary = mock_output.get("result", {}).get("summary", "I understand your needs.")
        response = {
            "is_clear": True,
            "assistant_message": f"{summary}\n\nStarting the evaluation pipeline now...",
            "clarifying_questions": [],
        }
        state.agent1_result = mock_output

    state.conversation_history.append({"role": "assistant", "content": response["assistant_message"]})
    return response


# ============================================================================
# Real Agent 1 — calls Claude API
# ============================================================================

async def real_agent1_turn(state: RunState, user_message: str) -> dict:
    from puzzleeval.schemas import Agent1Input
    from puzzleeval.agents.user_understanding import run_user_understanding_agent

    state.current_turn += 1
    state.conversation_history.append({"role": "user", "content": user_message})

    # Build conversation history for Agent 1 (exclude current message)
    conv_history = None
    if state.current_turn > 1:
        conv_history = [
            {"role": m["role"], "content": m["content"]}
            for m in state.conversation_history[:-1]
        ]

    agent1_input = Agent1Input(
        user_text=user_message,
        workflow_file_path=None,  # Files go to Agent 3F for test generation, not Agent 1
        trace_id=state.trace_id,
        conversation_history=conv_history,
    )

    result = await asyncio.to_thread(run_user_understanding_agent, agent1_input)
    result_dict = result.model_dump()

    if result.is_clear:
        summary = result.result.summary if result.result else "I understand your requirements."
        response = {
            "is_clear": True,
            "assistant_message": f"{summary}\n\nStarting the evaluation pipeline now...",
            "clarifying_questions": [],
        }
        # Store BOTH dict (for mock compat) and raw result for real pipeline
        state.agent1_result = result_dict
        state._agent1_model = result  # Keep Pydantic model for downstream agents
    else:
        clarification = result_dict.get("clarification_needed", {})


        if isinstance(clarification, dict):
            message = clarification.get("message", "Could you provide more details?")
            questions = clarification.get("critical_questions", [])
            optional_prompt = clarification.get("optional_prompt", "")
        else:
            message = str(clarification or "Could you provide more details?")
            questions = []
            optional_prompt = ""

        # Build full assistant message: main message + questions + optional prompt
        full_message = message
        if questions:
            full_message += "\n\n" + "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
        if optional_prompt:
            full_message += f"\n\n{optional_prompt}"

        response = {
            "is_clear": False,
            "assistant_message": full_message,
            "clarifying_questions": questions,
        }

    # Append assistant response to conversation history (critical for multi-turn)
    state.conversation_history.append({"role": "assistant", "content": json.dumps(result_dict)})
    # record_cost drives both the plain total AND the budget circuit-breaker.
    # Raises BudgetExceededError if the run blew through the cap — we let it
    # propagate; chat.py's outer handler catches and surfaces the clear error.
    state.record_cost(result_dict.get("cost_usd", 0) or 0, reason="agent_1_turn")
    state.event_bus.emit("cost_update", {
        "trace_id": state.trace_id,
        "total_cost_usd": state.total_cost_usd,
        "source": "agent_1_turn",
        "budget": state.budget.snapshot(),
    })
    return response


# ============================================================================
# Pipeline Runner — orchestrates Agents 2-5 with mock/real per agent
# ============================================================================

def _get_user_understanding(state: RunState):
    """Get UserUnderstandingOutput — Pydantic model for real agents, dict for mock."""
    # If we have the raw model from real Agent 1, use it
    if hasattr(state, "_agent1_model") and state._agent1_model and state._agent1_model.result:
        return state._agent1_model.result
    # Otherwise reconstruct from dict
    from puzzleeval.schemas import UserUnderstandingOutput
    return UserUnderstandingOutput(**state.agent1_result["result"])


async def run_pipeline(state: RunState):
    """Run the full pipeline (Agents 2-5) after Agent 1 is done."""
    emit = state.event_bus.emit
    state.status = "pipeline_running"

    # Create PipelineRun for observability (runs saved to puzzleeval-api/runs/)
    from puzzleeval.pipeline import PipelineRun
    runs_dir = API_ROOT / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    pipeline_run = PipelineRun(state.trace_id, output_dir=str(runs_dir))
    run_dir = runs_dir / state.trace_id

    def _save_json(filename: str, data):
        """Save any data (dict or Pydantic model) to the run directory.

        Uses Pydantic's mode='json' for nested dumps so non-JSON-native types
        (frozenset, set, datetime, UUID) round-trip as proper JSON values
        (frozenset → list, etc.). Without this, ``json.dumps(default=str)``
        would call ``str(frozenset(...))`` producing the literal Python repr
        ``"frozenset({'x'})"`` — silently breaking any downstream consumer
        that re-loads the file and tries to validate it through Pydantic
        again.
        """
        try:
            path = run_dir / filename
            if hasattr(data, "model_dump_json"):
                path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
                return
            # If data is a dict that came from `.model_dump()`, it may carry
            # native Python objects (frozenset, set) that json doesn't know
            # how to serialize. Walk and coerce them.
            path.write_text(
                json.dumps(_jsonify(data), indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass  # Non-critical — don't break pipeline for logging

    def _jsonify(node):
        """Coerce non-JSON-native Python types to JSON-friendly equivalents."""
        if isinstance(node, (frozenset, set)):
            return sorted(node) if all(isinstance(x, str) for x in node) else list(node)
        if isinstance(node, dict):
            return {k: _jsonify(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_jsonify(v) for v in node]
        if isinstance(node, tuple):
            return [_jsonify(v) for v in node]
        return node

    def _record_agent_cost_and_emit(agent_name: str, cost_usd: float) -> None:
        """Record agent cost through the budget circuit-breaker AND emit a
        live ``cost_update`` SSE so the UI meter updates between agent
        boundaries. Previously cost was only surfaced at agent_completed
        time and the meter appeared frozen during multi-minute agents.

        Raises ``BudgetExceededError`` when the cap is crossed — caller
        catches at the pipeline boundary and surfaces a clear failure.
        """
        if cost_usd and cost_usd > 0:
            state.record_cost(float(cost_usd), reason=agent_name)
        emit("cost_update", {
            "trace_id": state.trace_id,
            "source": agent_name,
            "delta_usd": float(cost_usd or 0),
            "total_cost_usd": state.total_cost_usd,
            "budget": state.budget.snapshot(),
        })

    # Save conversation history
    _save_json("agent_1_conversation.json", state.conversation_history)
    _save_json("agent_1_output.json", state.agent1_result)

    try:
        emit("pipeline_started", {"trace_id": state.trace_id})

        user_understanding = _get_user_understanding(state)

        # Surface the WorkflowBlueprint so the frontend can render the step
        # diagram before Agents 2-5 start producing candidates. Emitted
        # unconditionally — payload is `None` when the mock Agent 1 fixture
        # has no blueprint, so the frontend knows to skip the diagram.
        workflow_payload = None
        test_plan_payload = None
        try:
            raw_result = (state.agent1_result or {}).get("result", {})
            workflow_payload = raw_result.get("workflow")
            test_plan_payload = raw_result.get("test_plan")
        except AttributeError:
            workflow_payload = None
            test_plan_payload = None
        _workflow_payload = {
            "workflow": workflow_payload,
            "test_plan": test_plan_payload,
        }
        emit("workflow_blueprint", _workflow_payload)
        # Cache for late-connect replay — see RunState docstrings.
        state.cached_workflow_blueprint = _workflow_payload

        # ── Architecture summary in chat ──
        # After Agent 1 finishes, show a brief summary of the designed
        # architecture + test plan in the chat so the user sees "here's
        # what I'm about to build and test" before research starts.
        # This is a non-blocking UX improvement — not a pause.
        if workflow_payload:
            steps = workflow_payload.get("steps", [])
            arch_lines = []
            for s in steps:
                role = s.get("role", "?")
                desc = s.get("description", "")
                arch_lines.append(f"**{s.get('id', '?')}** ({role}): {desc}")
            arch_summary = "\n".join(arch_lines)

            test_plan_summary = ""
            if test_plan_payload:
                specs = test_plan_payload.get("scope_specs", [])
                tp_lines = []
                for spec in specs:
                    mode = spec.get("test_mode", "?")
                    count = spec.get("test_count_target", "?")
                    tp_lines.append(f"- {spec.get('scope_id', '?')}: {mode}, {count} tests")
                test_plan_summary = "\n\n**Test Plan:**\n" + "\n".join(tp_lines)

            emit("agent_activity", {
                "agent": "agent_1",
                "message": (
                    f"Workflow designed with {len(steps)} scope(s):\n{arch_summary}"
                    f"{test_plan_summary}"
                ),
                "status": "success",
            })

        # ------------------------------------------------------------------
        # Branch A: Agent 2 → Agent 4 (sequential)
        # Branch B: Agent 3 (independent)
        # Both branches run in PARALLEL — matches CLI design
        # ------------------------------------------------------------------
        emit("agent_started", {"agent": "agent_2", "name": "Research Agent"})
        emit("agent_started", {"agent": "agent_3", "name": "Test Case Generator"})

        async def _branch_a_research_and_screening():
            """Agent 2 → Agent 4 (sequential). Agent 4 starts as soon as Agent 2 finishes."""
            # --- Agent 2 ---
            if state.agent_modes.get("agent2") == "mock":
                a1_result = state.agent1_result or {}
                sub_tasks = a1_result.get("result", {}).get("sub_tasks", [])
                keywords = sub_tasks[0].get("search_keywords", ["AI tools"]) if sub_tasks else ["AI tools"]
                emit("agent_activity", {"agent": "agent_2", "message": f'Searching for "{keywords[0]}"...'})
                await asyncio.sleep(2.0)
                emit("agent_activity", {"agent": "agent_2", "message": "Reading comparison articles and roundup lists..."})
                await asyncio.sleep(2.5)
                state.agent2_result = _load_mock("agent_2_output.json")
                num_cands = len(state.agent2_result.get("candidates", []))
                emit("agent_activity", {"agent": "agent_2", "message": f"Found {num_cands} candidates from comparison articles"})
                await asyncio.sleep(1.5)
                emit("agent_activity", {"agent": "agent_2", "message": "Scoring on capability x adoption x use case fit..."})
                await asyncio.sleep(1.5)
            else:
                emit("agent_activity", {"agent": "agent_2", "message": "Searching for AI solutions..."})
                state.agent2_result, state._agent2_model = await _run_real_agent2(state, user_understanding)

            # Emit Agent 2 completion
            candidates = state.agent2_result.get("candidates", [])
            # Phase 4: dual-search coverage fields travel with every
            # candidate payload so the frontend can build the per-scope
            # CandidateCard view and the CoverageMatrix. Frozenset
            # serializes as a list via Pydantic's model_dump; cast defensively
            # in case the registry shim passes through a plain list/set.
            def _coerce_coverage(raw) -> list[str]:
                if raw is None:
                    return []
                if isinstance(raw, (list, tuple)):
                    return sorted({str(x) for x in raw})
                if isinstance(raw, (set, frozenset)):
                    return sorted({str(x) for x in raw})
                # Defense-in-depth: when an upstream path stringifies a
                # frozenset / set with json.dumps(default=str), we get
                # "frozenset({'step_1'})" or "set({'step_1'})" or "{'step_1'}".
                # Recover the items so consumers downstream don't break.
                if isinstance(raw, str):
                    s = raw.strip()
                    for prefix, suffix in (("frozenset(", ")"), ("set(", ")")):
                        if s.startswith(prefix) and s.endswith(suffix):
                            inner = s[len(prefix):-len(suffix)].strip()
                            if inner.startswith("{") and inner.endswith("}"):
                                inner = inner[1:-1]
                            return sorted({
                                p.strip().strip("'").strip('"')
                                for p in inner.split(",") if p.strip()
                            })
                    if s in ("frozenset()", "set()"):
                        return []
                return []

            _candidates_payload = {"candidates": [
                {
                    "name": c.get("name", ""),
                    "provider": c.get("provider", ""),
                    "description": c.get("description", ""),
                    "relevance_score": c.get("relevance_score", 0),
                    "adoption_difficulty": c.get("adoption_difficulty", "medium"),
                    "claimed_capabilities": c.get("claimed_capabilities", []),
                    "covers_step_ids": _coerce_coverage(c.get("covers_step_ids")),
                    "coverage_confidence": c.get("coverage_confidence") or {},
                }
                for c in candidates
            ]}
            emit("candidates_found", _candidates_payload)
            # Cache for late-connect replay — SelectionPanel needs
            # `candidates` state hydrated even if the frontend missed
            # the initial emit (page reload during pause, SSE reconnect).
            state.cached_candidates_payload = _candidates_payload
            emit("agent_activity", {"agent": "agent_2", "message": f"Research complete — {len(candidates)} candidates selected", "status": "success"})
            emit("agent_completed", {"agent": "agent_2", "cost_usd": state.agent2_result.get("cost_usd", 0)})
            _record_agent_cost_and_emit("agent_2", state.agent2_result.get("cost_usd", 0))
            _save_json("agent_2_output.json", state.agent2_result)

            # ── Coverage gap detection ──
            # Tell the user immediately when (a) Agent 2 found ZERO candidates
            # at all, or (b) every candidate is missing a workflow scope. The
            # previous behavior silently let the pipeline run to "completed"
            # with no actual results, which the user reads as "the system
            # broke" rather than "your niche capability has no public APIs."
            try:
                # `user_understanding` is bound by the enclosing `run_pipeline`
                # scope at line 251; re-fetching here would shadow the closure
                # and turn the earlier read at line 331 into an UnboundLocalError
                # (Python marks a name local for the whole function if any
                # assignment to it exists anywhere in the function body).
                workflow = getattr(user_understanding, "workflow", None)
                blueprint_steps = (
                    [s.id for s in workflow.steps]
                    if workflow and getattr(workflow, "steps", None)
                    else []
                )
                covered_scopes: set[str] = set()
                for c in candidates:
                    for sid in (c.get("covers_step_ids") or []):
                        covered_scopes.add(sid)
                missing_scopes = [s for s in blueprint_steps if s not in covered_scopes]
                if not candidates or missing_scopes:
                    emit("coverage_gap", {
                        "trace_id": state.trace_id,
                        "candidate_count": len(candidates),
                        "blueprint_step_ids": blueprint_steps,
                        "missing_scopes": missing_scopes,
                        "covered_scopes": sorted(covered_scopes),
                        "user_message": (
                            "No candidates found for this request — try broadening "
                            "your description (different keywords, larger industry "
                            "scope) or add specific providers via the SelectionPanel."
                            if not candidates
                            else (
                                f"Found {len(candidates)} candidate(s) but "
                                f"{len(missing_scopes)} scope(s) have no coverage: "
                                f"{', '.join(missing_scopes)}. The pipeline will "
                                f"only test the covered scopes."
                            )
                        ),
                    })
            except Exception as exc:  # noqa: BLE001
                # Don't kill the pipeline if blueprint introspection fails.
                logger.exception("coverage_gap emit failed: %s", exc)

            if state.cancel_requested:
                return

            # ── Phase 7: compute programmatic per-scope top-K defaults ──
            # This provides the DEFAULT picks the SelectionPanel shows.
            # User picks (Phase 6) OVERRIDE these. When Phase 6 is
            # disabled (--no-interactive / flag off), these ARE the final
            # picks that go to deep-verify.
            from puzzleeval.selection import select_scope_candidate_pairs
            from puzzleeval.schemas import Candidate as CandidateModel
            from services.billing import scope_candidates_cap

            bp_steps_for_selection = []
            try:
                raw_result = (state.agent1_result or {}).get("result", {})
                raw_workflow = raw_result.get("workflow")
                if raw_workflow:
                    bp_steps_for_selection = [s.get("id", "") for s in raw_workflow.get("steps", [])]
            except (AttributeError, TypeError):
                pass

            if bp_steps_for_selection and candidates:
                # Build Candidate models for scoring
                scoring_candidates = []
                for c_dict in candidates:
                    try:
                        scoring_candidates.append(CandidateModel(**c_dict))
                    except Exception:
                        continue

                programmatic_picks = select_scope_candidate_pairs(
                    candidates=scoring_candidates,
                    blueprint_step_ids=bp_steps_for_selection,
                    cap_per_scope=scope_candidates_cap(state.plan),
                )
                emit("agent_activity", {
                    "agent": "pipeline",
                    "message": f"Phase 7: computed default per-scope selections ({sum(len(v) for v in programmatic_picks.values())} total picks across {len(programmatic_picks)} scopes)",
                    "status": "info",
                })
            else:
                programmatic_picks = {}

            # ── Phase 6: pause for user candidate selection ──
            # After Agent 2 emits its ranked pool and BEFORE Agent 4 screens,
            # the pipeline pauses and waits for the user to:
            #   1. Pick which candidates to test at each scope (keep/remove)
            #   2. Optionally add custom providers with explicit covers_step_ids
            #
            # The pause emits `selection_required` SSE → frontend shows
            # SelectionPanel → user POSTs to /runs/{id}/select-candidates →
            # route handler sets state.selection_ready → we resume here.
            #
            # When PUZZLEEVAL_USER_SELECTION_ENABLED=0, the pause is skipped
            # and all Agent 2 candidates proceed to Agent 4 (today's behavior).
            from puzzleeval.config import USER_SELECTION_ENABLED
            # Group candidates by scope for the SelectionPanel
            bp_steps = []
            try:
                raw_result = (state.agent1_result or {}).get("result", {})
                raw_workflow = raw_result.get("workflow")
                if raw_workflow:
                    bp_steps = raw_workflow.get("steps", [])
            except (AttributeError, TypeError):
                pass

            # Gate the pause on (a) flag enabled, (b) have candidates,
            # (c) have scopes to pick for. Without scopes the SelectionPanel
            # has nothing to render and the pipeline would hang forever
            # waiting for /select-candidates that the user can't submit.
            # This path fires when Agent 1's workflow is None (unstructurable
            # request) or when USER_SELECTION_ENABLED=1 but the flow is a
            # pure single-scope request whose blueprint was collapsed.
            if USER_SELECTION_ENABLED and candidates and bp_steps:
                per_scope_candidates: dict[str, list[str]] = {}
                for step in bp_steps:
                    step_id = step.get("id", "")
                    per_scope_candidates[step_id] = [
                        c.get("name", "")
                        for c in candidates
                        if step_id in (_coerce_coverage(c.get("covers_step_ids")))
                    ]

                _selection_payload = {
                    "per_scope_candidates": per_scope_candidates,
                    "default_picks": programmatic_picks,
                    "total_candidates": len(candidates),
                }
                emit("selection_required", _selection_payload)
                # Cache the payload on state so the /events endpoint can
                # replay it to any subscriber that connects AFTER the
                # emit — the EventBus queue itself has no replay. See
                # RunState.pending_selection_payload.
                state.pending_selection_payload = _selection_payload
                state.status = "awaiting_candidate_selection"
                state.selection_required_emitted_at = datetime.now(timezone.utc).isoformat()
                emit("agent_activity", {
                    "agent": "pipeline",
                    "message": "Waiting for candidate selection...",
                    "status": "progress",
                })

                # Await selection OR cancellation — whichever fires first.
                selection_task = asyncio.ensure_future(state.selection_ready.wait())
                cancel_task = asyncio.ensure_future(state.cancel_event.wait())
                done, pending = await asyncio.wait(
                    [selection_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()

                if state.cancel_requested:
                    return

                # Apply the user's per-scope picks + user-added candidates.
                from puzzleeval.agents.research import apply_scope_picks
                from puzzleeval.schemas import Agent2Result, UserAddedCandidate

                a2_model = Agent2Result(**state.agent2_result)
                user_added = [
                    UserAddedCandidate(**ua) for ua in state.user_added_candidates
                ] if state.user_added_candidates else None

                a2_model = apply_scope_picks(
                    a2_model,
                    scope_picks=state.user_scope_picks,
                    user_added=user_added,
                )
                state.agent2_result = a2_model.model_dump()
                state.user_selection_applied = True
                state.status = "pipeline_running"
                # Clear the cached selection payload — we're past the
                # pause and a late subscriber should NOT receive a stale
                # synthetic selection_required replay after this point.
                state.pending_selection_payload = None

                # Re-emit candidates_found with the filtered set so the
                # frontend updates the candidate list to reflect picks.
                filtered_candidates = state.agent2_result.get("candidates", [])
                emit("candidates_found", {"candidates": [
                    {
                        "name": c.get("name", ""),
                        "provider": c.get("provider", ""),
                        "description": c.get("description", ""),
                        "relevance_score": c.get("relevance_score", 0),
                        "adoption_difficulty": c.get("adoption_difficulty", "medium"),
                        "claimed_capabilities": c.get("claimed_capabilities", []),
                        "covers_step_ids": _coerce_coverage(c.get("covers_step_ids")),
                        "coverage_confidence": c.get("coverage_confidence") or {},
                    }
                    for c in filtered_candidates
                ]})
                emit("agent_activity", {
                    "agent": "pipeline",
                    "message": f"Selection applied — {len(filtered_candidates)} candidates proceeding to screening",
                    "status": "success",
                })
            elif programmatic_picks and bp_steps_for_selection:
                # Phase 6 disabled but Phase 7 computed picks → apply
                # programmatic top-K as the selection (auto-run path).
                from puzzleeval.agents.research import apply_scope_picks
                from puzzleeval.schemas import Agent2Result as A2R
                a2_model = A2R(**state.agent2_result)
                a2_model = apply_scope_picks(a2_model, scope_picks=programmatic_picks)
                state.agent2_result = a2_model.model_dump()
                emit("agent_activity", {
                    "agent": "pipeline",
                    "message": f"Phase 7 auto-selection — {len(a2_model.candidates)} candidates proceeding",
                    "status": "info",
                })

            # Billing gate: Agent 4 requires the "testing" feature. In default
            # mode (PUZZLEEVAL_BILLING_ENFORCED=0) this is a no-op that just
            # bumps state.plan_gates_triggered for observability. In enforced
            # mode it raises HTTPException(402) which we catch below and
            # translate into an SSE failure event.
            try:
                require_agent_access(state, "agent_4")
            except HTTPException as e:
                emit("agent_blocked", {
                    "agent": "agent_4",
                    "reason": e.detail,
                    "plan": state.plan,
                    "credits_remaining": state.credits_remaining,
                })
                emit("pipeline_failed", {"error": e.detail, "blocked_at": "agent_4"})
                state.status = "failed"
                return

            # --- Agent 4 (starts immediately after Agent 2, doesn't wait for Agent 3) ---
            emit("agent_started", {"agent": "agent_4", "name": "Screening Agent"})

            if state.agent_modes.get("agent4") == "mock":
                state.agent4_result = _load_mock("agent_4_output.json")
                validated = state.agent4_result.get("validated_candidates", [])
                rejected = state.agent4_result.get("rejected_candidates", [])
                all_names = [c.get("name", "") for c in validated] + [c.get("name", "") for c in rejected]
                for name in all_names:
                    emit("agent_activity", {"agent": "agent_4", "message": f"Verifying {name} API...", "candidate_name": name})
                    await asyncio.sleep(1.5)
                    is_validated = any(c.get("name") == name for c in validated)
                    if is_validated:
                        match = next((c for c in validated if c.get("name") == name), {})
                        auth = match.get("auth_method", "unknown")
                        emit("agent_activity", {"agent": "agent_4", "message": f"{name}: API docs confirmed — {auth} auth", "candidate_name": name, "status": "success"})
                    else:
                        match = next((c for c in rejected if c.get("name") == name), {})
                        reason = match.get("rejection_reason", "no API found")
                        emit("agent_activity", {"agent": "agent_4", "message": f"{name}: Rejected — {reason[:60]}", "candidate_name": name, "status": "failure"})
                    await asyncio.sleep(0.8)
            else:
                emit("agent_activity", {"agent": "agent_4", "message": "Verifying API access for all candidates..."})
                state.agent4_result, state._agent4_model = await _run_real_agent4(state, user_understanding)
                validated = state.agent4_result.get("validated_candidates", [])
                rejected = state.agent4_result.get("rejected_candidates", [])
                for c in validated:
                    emit("agent_activity", {"agent": "agent_4", "message": f"{c.get('name','')}: Verified — {c.get('auth_method','unknown')} auth", "candidate_name": c.get("name",""), "status": "success"})
                for c in rejected:
                    emit("agent_activity", {"agent": "agent_4", "message": f"{c.get('name','')}: Rejected — {c.get('rejection_reason','')[:60]}", "candidate_name": c.get("name",""), "status": "failure"})

            validated = state.agent4_result.get("validated_candidates", [])
            rejected = state.agent4_result.get("rejected_candidates", [])
            emit("candidates_verified", {
                "validated": [
                    {
                        "name": c.get("name", ""),
                        "provider": c.get("provider", ""),
                        "description": c.get("description", ""),
                        "relevance_score": c.get("relevance_score", 0),
                        "adoption_difficulty": c.get("adoption_difficulty", "medium"),
                        "confirmed_capabilities": c.get("confirmed_capabilities", []),
                        "auth_method": c.get("auth_method"),
                        "api_access_method": c.get("api_access_method"),
                        "verified_api_docs_url": c.get("verified_api_docs_url"),
                    }
                    for c in validated
                ],
                "rejected": [c.get("name", "") for c in rejected],
            })
            emit("agent_completed", {"agent": "agent_4", "cost_usd": state.agent4_result.get("cost_usd", 0)})
            _record_agent_cost_and_emit("agent_4", state.agent4_result.get("cost_usd", 0))
            _save_json("agent_4_output.json", state.agent4_result)

            # ── Phase 6.5: emit per-candidate deep-verify results ──
            # These SSE events are consumed by the frontend's
            # RejectionSummary (candidate_rejected) and CoverageBadge
            # upgrade (candidate_verified). The events carry per-scope
            # detail so the UI can render precisely which scopes passed
            # and which were dropped.
            from puzzleeval.config import AGENT4_DEEP_VERIFY_ENABLED
            if AGENT4_DEEP_VERIFY_ENABLED:
                for vc in validated:
                    verified_scopes = list(vc.get("covers_step_ids", []))
                    for sid in verified_scopes:
                        emit("candidate_verified", {
                            "candidate_name": vc.get("name", ""),
                            "scope_id": sid,
                            "provider": vc.get("provider", ""),
                            "pricing_breakdown": vc.get("pricing_breakdown"),
                        })

                failed_list = state.agent4_result.get("failed_to_verify", [])
                for ftv in failed_list:
                    emit("candidate_rejected", {
                        "candidate_name": ftv.get("name", ""),
                        "scope_id": ftv.get("scope_id", ""),
                        "reason": ftv.get("reason", "verify_error"),
                        "provider": ftv.get("provider", ""),
                        "attempt_notes": ftv.get("attempt_notes", ""),
                    })

                # Scope-level summary
                scope_verified: dict[str, int] = {}
                scope_rejected: dict[str, int] = {}
                for vc in validated:
                    for sid in vc.get("covers_step_ids", []):
                        scope_verified[sid] = scope_verified.get(sid, 0) + 1
                for ftv in failed_list:
                    sid = ftv.get("scope_id", "")
                    if sid:
                        scope_rejected[sid] = scope_rejected.get(sid, 0) + 1
                all_scope_ids = set(scope_verified) | set(scope_rejected)
                for sid in sorted(all_scope_ids):
                    emit("scope_verified_complete", {
                        "scope_id": sid,
                        "verified_count": scope_verified.get(sid, 0),
                        "rejected_count": scope_rejected.get(sid, 0),
                    })

        async def _branch_b_test_generation():
            """Agent 3/3F — runs independently of Agent 2→4."""
            if state.agent_modes.get("agent3") == "mock":
                emit("agent_activity", {"agent": "agent_3", "message": "Generating test cases from uploaded files..."})
                await asyncio.sleep(2.5)
                state.agent3_result = _load_mock("agent_3_output.json")
                num_tc = len(state.agent3_result.get("test_cases", []))
                emit("agent_activity", {"agent": "agent_3", "message": f"Generated {num_tc} test cases with ground truth"})
            else:
                emit("agent_activity", {"agent": "agent_3", "message": "Generating test cases..."})
                state.agent3_result, state._agent3_model = await _run_real_agent3(state, user_understanding)

            test_cases = state.agent3_result.get("test_cases", [])
            emit("test_cases_ready", {"count": len(test_cases)})
            emit("agent_activity", {"agent": "agent_3", "message": f"Test generation complete — {len(test_cases)} cases", "status": "success"})
            emit("agent_completed", {"agent": "agent_3", "cost_usd": state.agent3_result.get("cost_usd", 0)})
            _record_agent_cost_and_emit("agent_3", state.agent3_result.get("cost_usd", 0))
            _save_json("agent_3_output.json", state.agent3_result)

        # Run both branches in parallel — Agent 4 starts as soon as Agent 2 finishes.
        # return_exceptions=True is CRITICAL: without it, a failure in one branch
        # (e.g. Agent 3 hits a 429) cancels the other branch mid-flight, tearing
        # down a potentially completed Agent 4 run. The retrieved exception is
        # re-raised below so the outer try/except still surfaces a clean
        # pipeline_failed event.
        _results = await asyncio.gather(
            _branch_a_research_and_screening(),
            _branch_b_test_generation(),
            return_exceptions=True,
        )
        for _idx, _r in enumerate(_results):
            if isinstance(_r, BaseException):
                _branch_name = "research_and_screening" if _idx == 0 else "test_generation"
                logger.exception(
                    "parallel branch %s failed: %s", _branch_name, _r,
                )
                # Surface the error via the outer try/except so pipeline_failed
                # carries the original exception type + message, not an
                # asyncio.gather wrapper.
                raise _r

        if state.cancel_requested:
            # Disambiguate cancel-after-screening vs cancel-during-selection.
            # The selection pause sets `state.status = "awaiting_candidate_selection"`
            # and clears it on resume. If the status is still "awaiting..." we
            # know the user cancelled mid-pause.
            cancelled_at = (
                "during_selection"
                if state.status == "awaiting_candidate_selection"
                else "after_screening"
            )
            emit("pipeline_cancelled", {"cancelled_at_agent": cancelled_at})
            state.status = "cancelled"
            emit("done", {})
            state.event_bus.close()
            return

        # ------------------------------------------------------------------
        # Agent 5 (Build + Test)
        # ------------------------------------------------------------------
        if state.cancel_requested:
            emit("pipeline_cancelled", {"cancelled_at_agent": "before_build"})
            state.status = "cancelled"
            emit("done", {})
            state.event_bus.close()
            return

        # --- Phase 2 billing gate: Agent 5 needs the "testing" feature ──
        # Costs 5 credits in enforced mode. Same enforcement / no-op semantics
        # as the Agent 4 gate above.
        try:
            require_agent_access(state, "agent_5")
        except HTTPException as e:
            emit("agent_blocked", {
                "agent": "agent_5",
                "reason": e.detail,
                "plan": state.plan,
                "credits_remaining": state.credits_remaining,
            })
            emit("pipeline_failed", {"error": e.detail, "blocked_at": "agent_5"})
            state.status = "failed"
            emit("done", {})
            state.event_bus.close()
            return

        emit("agent_started", {"agent": "agent_5", "name": "Build + Test Agent"})

        if state.agent_modes.get("agent5") == "mock":
            state.agent5_result = _load_mock("agent_5_output.json")
            selected_names = [h.get("candidate_name", "") for h in state.agent5_result.get("harnesses", [])]
            selected_names += [h.get("candidate_name", "") for h in state.agent5_result.get("failed_harnesses", [])]
            emit("candidates_selected", {"selected": selected_names})
            emit("agent_activity", {"agent": "agent_5", "message": f"Selected top {len(selected_names)} candidates for testing", "status": "info"})
            await asyncio.sleep(0.5)
            await _emit_mock_agent5_progress(state, emit)
        else:
            emit("agent_activity", {"agent": "agent_5", "message": "Building test harnesses and running evaluations..."})
            # DON'T predict which candidates Agent 5 selects — it sorts by
            # credentials first, then relevance_score (different from our sort).
            # Instead, we'll emit candidates_selected AFTER Agent 5 finishes,
            # based on which candidates actually got harnesses built.
            # The harness_started callbacks will show progress in real-time.

            # Define progress callback that emits SSE events
            def _agent5_progress(event_type: str, data: dict):
                name = data.get("candidate_name", "")

                if event_type == "build_turn":
                    # Per-turn progress inside the build loop
                    turn_num = data.get("turn", 0)
                    max_turns = data.get("max_turns", 25)
                    phase = data.get("phase", "building")
                    tools = data.get("tools_used", [])
                    cost = data.get("cost_usd", 0)

                    phase_labels = {
                        "researching": "Researching API docs",
                        "building": "Writing & testing code",
                        "validating": "Live API validation",
                    }
                    phase_label = phase_labels.get(phase, phase)

                    # Describe what tools were used
                    tool_desc = ""
                    if "web_search" in tools:
                        tool_desc = " — searching web"
                    elif "web_fetch" in tools:
                        tool_desc = " — reading docs"
                    elif "write_file" in tools:
                        tool_desc = " — writing code"
                    elif "run_code" in tools:
                        tool_desc = " — running tests"
                    elif "advisor" in tools:
                        tool_desc = " — consulting advisor"
                    elif "ask_research" in tools:
                        tool_desc = " — researching"

                    emit("agent_activity", {
                        "agent": "agent_5",
                        "message": f"{name}: Turn {turn_num}/{max_turns} — {phase_label}{tool_desc} ({cost * 20:.2f} credits)",
                        "candidate_name": name,
                    })
                    return

                # Structural events — emit both the typed event AND activity
                emit(event_type, data)

                if event_type == "candidates_selected":
                    # Authoritative selection from Agent 5's internal logic
                    emit("candidates_selected", data)
                    selected = data.get("selected", [])
                    emit("agent_activity", {"agent": "agent_5", "message": f"Selected {len(selected)} candidates: {', '.join(selected)}", "status": "info"})
                    return
                elif event_type == "harness_started":
                    emit("agent_activity", {"agent": "agent_5", "message": f"Building harness for {name}...", "candidate_name": name})
                elif event_type == "harness_completed":
                    turns = data.get("build_turns", 0)
                    cost = data.get("build_cost_usd", 0)
                    emit("agent_activity", {"agent": "agent_5", "message": f"Harness built for {name} ({turns} turns, {cost * 20:.2f} credits)", "candidate_name": name, "status": "success"})
                elif event_type == "harness_failed":
                    reason = data.get("failure_reason", "")[:80]
                    emit("agent_activity", {"agent": "agent_5", "message": f"Build failed for {name}: {reason}", "candidate_name": name, "status": "failure"})
                elif event_type == "test_execution_started":
                    emit("agent_activity", {"agent": "agent_5", "message": f"Running tests for {name}...", "candidate_name": name})
                elif event_type == "candidate_results_ready":
                    passed = data.get("tests_passed", 0)
                    total = passed + data.get("tests_failed", 0)
                    emit("agent_activity", {"agent": "agent_5", "message": f"{name}: {passed}/{total} tests passed", "candidate_name": name, "status": "success" if passed == total else "info"})
                    for tr in data.get("test_results", []):
                        emit("test_result", {"candidate_name": name, **tr})

            state.agent5_result = await _run_real_agent5(state, user_understanding, _agent5_progress)

        _agent5_cost = state.agent5_result.get("total_build_cost_usd", 0) + state.agent5_result.get("total_test_cost_usd", 0)
        emit("agent_completed", {"agent": "agent_5", "cost_usd": _agent5_cost})
        _record_agent_cost_and_emit("agent_5", _agent5_cost)
        _save_json("agent_5_output.json", state.agent5_result)

        # ------------------------------------------------------------------
        # Report generation step — assemble structured EvaluationReport
        # ------------------------------------------------------------------
        emit("report_generating", {})
        emit("agent_activity", {"agent": "report", "message": "Generating evaluation report...", "status": "progress"})
        try:
            from puzzleeval.report import assemble_report, report_to_dict
            # `state.total_cost_usd` already includes every agent's cost —
            # each agent's completion path calls `_record_agent_cost_and_emit`
            # which increments it. Using `state.total_cost_usd + total_cost`
            # here would DOUBLE-COUNT the run. Previously total_cost was
            # summed locally and added again; delete the local sum.
            report = assemble_report(
                run_id=state.run_id,
                trace_id=state.trace_id,
                agent1_result=state.agent1_result,
                agent2_result=state.agent2_result,
                agent4_result=state.agent4_result,
                agent5_result=state.agent5_result,
                total_cost_usd=state.total_cost_usd,
            )
            report_dict = report_to_dict(report)
            _save_json("evaluation_report.json", report_dict)
            emit("evaluation_report", report_dict)
            emit("agent_activity", {
                "agent": "report",
                "message": (
                    f"Report ready — {report.candidate_count} candidate(s) ranked, "
                    f"winner: {report.overall_winner or 'none'}"
                ),
                "status": "success",
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("report assembly failed: %s", exc)
            emit("agent_activity", {
                "agent": "report",
                "message": f"Report assembly failed: {exc}",
                "status": "error",
            })
            total_cost = sum(
                r.get("cost_usd", 0) if r else 0
                for r in [state.agent2_result, state.agent3_result, state.agent4_result]
            ) + (state.agent5_result.get("total_build_cost_usd", 0) if state.agent5_result else 0) \
              + (state.agent5_result.get("total_test_cost_usd", 0) if state.agent5_result else 0)

        # ------------------------------------------------------------------
        # Pipeline complete. Per-agent costs were already recorded through
        # ``_record_agent_cost_and_emit`` as each agent finished, so
        # ``state.total_cost_usd`` and ``state.budget.spent_usd`` are already
        # the real total — NO second aggregate record here (that would
        # double-count). The ``total_cost`` local is kept only for back-compat
        # with callers that dump it into pipeline_summary.json metadata.
        # ------------------------------------------------------------------
        emit("pipeline_completed", {
            "total_cost_usd": state.total_cost_usd,
            "budget": state.budget.snapshot(),
            "summary": (
                state.agent5_result.get("test_execution_summary", "Pipeline complete.")
                if state.agent5_result else "Pipeline complete."
            ),
        })
        state.status = "completed"

        # Save pipeline summary
        try:
            pipeline_run.finalize()
        except Exception:
            pass  # Non-critical

    except Exception as e:
        import traceback
        traceback.print_exc()
        # Budget exhaustion is a first-class failure category — surface it
        # with a clear reason code so the frontend can render a different
        # banner than generic "pipeline_failed" (e.g. "Cost cap reached —
        # raise PUZZLEEVAL_MAX_RUN_COST_USD and retry").
        from puzzleeval.budget import BudgetExceededError
        if isinstance(e, BudgetExceededError):
            emit("pipeline_failed", {
                "error": str(e),
                "reason": "budget_exceeded",
                "spent_usd": e.spent,
                "cap_usd": e.cap,
                "last_charge_reason": e.last_reason,
                "recovery": (
                    "Raise PUZZLEEVAL_MAX_RUN_COST_USD in the backend env "
                    "(currently "
                    f"${e.cap:.2f}) and retry. The run stopped cleanly — "
                    "partial results are preserved in the run directory."
                ),
            })
        else:
            emit("pipeline_failed", {"error": str(e)})
        state.status = "failed"

    finally:
        emit("done", {})
        state.event_bus.close()


async def _emit_mock_agent5_progress(state: RunState, emit):
    """Emit granular progress events from mock Agent 5 data with realistic delays."""
    harnesses = state.agent5_result.get("harnesses", [])
    candidate_runs = state.agent5_result.get("candidate_runs", [])

    for h in harnesses:
        name = h.get("candidate_name", "")
        if state.cancel_requested:
            return
        emit("harness_started", {"candidate_name": name})
        emit("agent_activity", {"agent": "agent_5", "message": f"Researching {name} API documentation...", "candidate_name": name})
        await asyncio.sleep(2.5)
        emit("agent_activity", {"agent": "agent_5", "message": f"Reading endpoint specifications for {name}...", "candidate_name": name})
        await asyncio.sleep(2.0)
        emit("agent_activity", {"agent": "agent_5", "message": f"Writing harness.py — thin API client for {name}", "candidate_name": name})
        await asyncio.sleep(1.5)

        smoke = h.get("smoke_test_passed", True)
        live = h.get("live_validation_passed", True)
        emit("agent_activity", {
            "agent": "agent_5",
            "message": f"Smoke test {'passed' if smoke else 'failed'} | Live validation {'passed' if live else 'failed'}",
            "candidate_name": name,
            "status": "success" if (smoke and live) else "failure",
        })
        await asyncio.sleep(1.0)

        turns = h.get("build_turns", 0)
        cost = h.get("build_cost_usd", 0)
        emit("harness_completed", {"candidate_name": name, "success": True, "build_turns": turns, "build_cost_usd": cost})
        emit("agent_activity", {"agent": "agent_5", "message": f"Harness built for {name} ({turns} turns, {cost * 20:.2f} credits)", "candidate_name": name, "status": "success"})
        await asyncio.sleep(0.3)

    for fh in state.agent5_result.get("failed_harnesses", []):
        name = fh.get("candidate_name", "")
        reason = fh.get("failure_reason", "unknown")
        emit("harness_failed", {"candidate_name": name, "failure_reason": reason})
        emit("agent_activity", {"agent": "agent_5", "message": f"Build failed for {name}: {reason[:80]}", "candidate_name": name, "status": "failure"})

    for cr in candidate_runs:
        name = cr.get("candidate_name", "")
        if state.cancel_requested:
            return
        emit("test_execution_started", {"candidate_name": name})
        emit("agent_activity", {"agent": "agent_5", "message": f"Running tests for {name}...", "candidate_name": name})
        await asyncio.sleep(0.3)

        for tr in cr.get("test_results", []):
            if state.cancel_requested:
                return
            tc_id = tr.get("test_case_id", "")
            passed = tr.get("passed", False)
            score = tr.get("weighted_score", 0)
            latency = tr.get("latency_ms", 0)

            emit("test_result", {
                "candidate_name": name, "test_case_id": tc_id, "passed": passed,
                "weighted_score": score, "latency_ms": latency,
                "criteria_scores": [
                    {"criterion": cs.get("criterion", ""), "score": cs.get("score", 0),
                     "passed": cs.get("passed", False), "reasoning": cs.get("reasoning", "")}
                    for cs in tr.get("criteria_scores", [])
                ],
            })
            score_pct = round(score * 100)
            emit("agent_activity", {"agent": "agent_5", "message": f"{tc_id}: {'passed' if passed else 'failed'} — {score_pct}/100 ({latency:.0f}ms)", "candidate_name": name, "status": "success" if passed else "failure"})
            await asyncio.sleep(1.5)

        tests_passed = cr.get("tests_passed", 0)
        tests_total = tests_passed + cr.get("tests_failed", 0)
        emit("candidate_results_ready", {
            "candidate_name": name, "provider": cr.get("provider", ""),
            "status": cr.get("status", ""), "tests_passed": tests_passed,
            "tests_failed": cr.get("tests_failed", 0), "pass_rate": cr.get("pass_rate", 0),
            "avg_latency_ms": cr.get("avg_latency_ms", 0), "total_cost_usd": cr.get("total_cost_usd", 0),
            "overall_score": _compute_avg_weighted_score(cr),
        })
        emit("agent_activity", {"agent": "agent_5", "message": f"{name}: {tests_passed}/{tests_total} tests passed", "candidate_name": name, "status": "success" if tests_passed == tests_total else "info"})
        await asyncio.sleep(0.3)


def _emit_agent5_results(state: RunState, emit):
    """Emit Agent 5 results after real execution (coarse — all at once)."""
    result = state.agent5_result

    for h in result.get("harnesses", []):
        name = h.get("candidate_name", "")
        emit("harness_started", {"candidate_name": name})
        emit("harness_completed", {"candidate_name": name, "success": True, "build_turns": h.get("build_turns", 0), "build_cost_usd": h.get("build_cost_usd", 0)})
        emit("agent_activity", {"agent": "agent_5", "message": f"Harness built for {name}", "candidate_name": name, "status": "success"})

    for fh in result.get("failed_harnesses", []):
        name = fh.get("candidate_name", "")
        emit("harness_failed", {"candidate_name": name, "failure_reason": fh.get("failure_reason", "")})
        emit("agent_activity", {"agent": "agent_5", "message": f"Build failed for {name}", "candidate_name": name, "status": "failure"})

    for cr in result.get("candidate_runs", []):
        name = cr.get("candidate_name", "")
        emit("test_execution_started", {"candidate_name": name})
        for tr in cr.get("test_results", []):
            emit("test_result", {
                "candidate_name": name, "test_case_id": tr.get("test_case_id", ""),
                "passed": tr.get("passed", False), "weighted_score": tr.get("weighted_score", 0),
                "latency_ms": tr.get("latency_ms", 0),
                "criteria_scores": [
                    {"criterion": cs.get("criterion", ""), "score": cs.get("score", 0),
                     "passed": cs.get("passed", False), "reasoning": cs.get("reasoning", "")}
                    for cs in tr.get("criteria_scores", [])
                ],
            })
        emit("candidate_results_ready", {
            "candidate_name": name, "provider": cr.get("provider", ""),
            "status": cr.get("status", ""), "tests_passed": cr.get("tests_passed", 0),
            "tests_failed": cr.get("tests_failed", 0), "pass_rate": cr.get("pass_rate", 0),
            "avg_latency_ms": cr.get("avg_latency_ms", 0), "total_cost_usd": cr.get("total_cost_usd", 0),
            "overall_score": _compute_avg_weighted_score(cr),
        })


# ============================================================================
# Real Agent Wrappers — fixed to match CLI's exact input construction
# ============================================================================

async def _run_real_agent2(state: RunState, user_understanding):
    """Run real Agent 2 (Research). Returns (dict, model) tuple."""
    from puzzleeval.schemas import Agent2Input
    from puzzleeval.agents.research import run_research_agent

    agent2_input = Agent2Input(
        user_understanding=user_understanding,
        trace_id=state.trace_id,
    )

    result = await asyncio.to_thread(run_research_agent, agent2_input)

    # Users add specific providers via the SelectionPanel (flowing through
    # inject_user_candidates) or via the CLI interactive prompt. Automated
    # runs that need specific providers POST to /runs/{id}/select-candidates.
    result_dict = result.model_dump()
    return result_dict, result


async def _run_real_agent3(state: RunState, user_understanding):
    """Run real Agent 3/3F (Test Generation). Returns (dict, model) tuple.

    Mixed-mode routing:
      1. If Agent 1 produced a TestPlan → route by scope_spec.test_mode
      2. Otherwise → route by sub_task.requires_test_files
      3. Both agents produce Agent3Result → merge
    """
    from puzzleeval.schemas import (
        Agent3Input, Agent3Result, UserUnderstandingOutput,
    )
    from puzzleeval.agents.synthetic_tests import run_synthetic_tests_agent
    from puzzleeval.agents.synthetic_tests_file import run_file_tests_agent

    test_file_paths = [f.get("path") for f in state.uploaded_files if f.get("path")]
    has_files = bool(test_file_paths)

    # Determine file vs text sub-tasks using TestPlan or requires_test_files
    all_subtasks = user_understanding.sub_tasks
    test_plan = getattr(user_understanding, "test_plan", None)

    file_subtasks = []
    text_subtasks = []

    if test_plan and test_plan.scope_specs:
        # TestPlan routing — more precise than requires_test_files
        file_scope_ids = {
            s.scope_id for s in test_plan.scope_specs
            if s.test_mode == "file_based"
        }
        blueprint = getattr(user_understanding, "workflow", None)
        scope_caps = {}
        if blueprint and blueprint.steps:
            scope_caps = {
                s.capability.strip().lower(): s.id
                for s in blueprint.steps
            }
        for st in all_subtasks:
            scope_id = scope_caps.get(st.capability.strip().lower(), "")
            if scope_id in file_scope_ids:
                file_subtasks.append(st)
            else:
                text_subtasks.append(st)
    else:
        file_subtasks = [st for st in all_subtasks if st.requires_test_files]
        text_subtasks = [st for st in all_subtasks if not st.requires_test_files]

    # Helper to build filtered UserUnderstandingOutput
    def _filter_uo(subtasks_to_keep):
        keep_descs = {st.description for st in subtasks_to_keep}
        filtered = [st for st in all_subtasks if st.description in keep_descs]
        return UserUnderstandingOutput(
            summary=user_understanding.summary,
            sub_tasks=filtered,
            search_strategy=getattr(user_understanding, "search_strategy", "both"),
            domain=user_understanding.domain,
            search_keywords=user_understanding.search_keywords,
            constraints=user_understanding.constraints,
            workflow_summary=getattr(user_understanding, "workflow_summary", None),
            workflow=getattr(user_understanding, "workflow", None),
            test_plan=test_plan,
        )

    # ── Test data sufficiency check ──
    # Per-scope verdict (READY / AUGMENT / SYNTHESIZE / REQUEST_MORE / DEGRADE)
    # surfaced as a single SSE event so the frontend can render advisories +
    # request_more prompts before Agent 3F or Agent 3 fires. Pure observability;
    # never blocks the run.
    try:
        from puzzleeval.test_data_sufficiency import (
            assess_sufficiency, summarize_verdicts,
        )
        from puzzleeval.tool_plugins import list_plugins as _list_plugins
        avail_plugins = {p.name for p in _list_plugins() if p.is_available()[0]}
        verdicts: dict[str, Any] = {}
        for sub in file_subtasks:
            scope_id = getattr(sub, "id", None) or sub.description[:30]
            verdicts[scope_id] = assess_sufficiency(
                scope_id=scope_id,
                input_type="document_content",
                output_type=getattr(sub, "output_format", "structured_json"),
                requires_test_files=True,
                file_paths=list(test_file_paths),
                available_plugin_names=avail_plugins,
            )
        if verdicts:
            summary = summarize_verdicts(verdicts)
            state.event_bus.emit("test_data_sufficiency", {
                "trace_id": state.trace_id,
                "summary": summary,
                "verdicts": [
                    {
                        "scope_id": v.scope_id,
                        "action": v.action,
                        "reason": v.reason,
                        "advisories": v.advisories,
                        "request_message": v.request_message,
                        "degraded_confidence": v.degraded_confidence,
                        "plugin_for_augment": v.plugin_for_augment,
                        "file_count": v.inventory.total_count,
                    }
                    for v in verdicts.values()
                ],
            })
    except Exception as exc:  # noqa: BLE001
        logger.exception("test data sufficiency check failed: %s", exc)

    file_result = None
    text_result = None

    # Agent 3F: file-based sub-tasks (only when files provided)
    if file_subtasks and has_files:
        file_uo = _filter_uo(file_subtasks)
        a3f_input = Agent3Input(
            user_understanding=file_uo,
            trace_id=state.trace_id,
            test_file_paths=test_file_paths,
        )
        file_result = await asyncio.to_thread(run_file_tests_agent, a3f_input)

    # Agent 3: synthetic sub-tasks (+ file sub-tasks when no files provided)
    synth_subtasks = list(text_subtasks)
    if not has_files and file_subtasks:
        synth_subtasks.extend(file_subtasks)

    if synth_subtasks:
        synth_uo = _filter_uo(synth_subtasks)
        a3_input = Agent3Input(
            user_understanding=synth_uo,
            trace_id=state.trace_id,
        )
        text_result = await asyncio.to_thread(run_synthetic_tests_agent, a3_input)

        # Wire file_required flag for degraded file sub-tasks
        if not has_files and file_subtasks and text_result:
            file_caps = {st.capability.lower() for st in file_subtasks}
            for tc in text_result.test_cases:
                ref = tc.sub_task_ref.lower()
                if any(cap in ref or ref in cap for cap in file_caps):
                    tc.file_required = True

    # Merge results
    if file_result and text_result:
        merged_cases = file_result.test_cases + text_result.test_cases
        merged_coverage = dict(file_result.coverage_summary)
        merged_coverage.update(text_result.coverage_summary)
        result = Agent3Result(
            test_cases=merged_cases,
            generation_notes=(
                f"Mixed-mode: {len(file_result.test_cases)} file-based + "
                f"{len(text_result.test_cases)} synthetic."
            ),
            coverage_summary=merged_coverage,
            cost_usd=file_result.cost_usd + text_result.cost_usd,
        )
    elif file_result:
        result = file_result
    elif text_result:
        result = text_result
    else:
        result = Agent3Result(
            test_cases=[], generation_notes="No test cases generated.",
            coverage_summary={}, cost_usd=0.0,
        )

    result_dict = result.model_dump()
    return result_dict, result


async def _run_real_agent4(state: RunState, user_understanding):
    """Run real Agent 4 (Screening). Returns (dict, model) tuple.

    CRITICAL: Agent4Input.candidates expects Agent2Result (not list).
    """
    from puzzleeval.schemas import Agent4Input, Agent2Result

    # Reconstruct Agent2Result from dict if needed
    if hasattr(state, "_agent2_model") and state._agent2_model:
        agent2_result_model = state._agent2_model
    else:
        agent2_result_model = Agent2Result(**state.agent2_result)

    from puzzleeval.agents.screening import run_screening_agent

    agent4_input = Agent4Input(
        candidates=agent2_result_model,  # FULL Agent2Result, NOT .candidates list
        user_understanding=user_understanding,
        trace_id=state.trace_id,
    )

    result = await asyncio.to_thread(run_screening_agent, agent4_input)
    result_dict = result.model_dump()
    return result_dict, result


async def _run_real_agent5(state: RunState, user_understanding, progress_callback=None):
    """Run real Agent 5 (Build + Test). Returns dict.

    CRITICAL points:
    - CWD must be set so runs/{trace_id}/harnesses/ resolves correctly
    - provider_credentials must be loaded from registry with candidate matching
    - test_file_paths must be absolute
    """
    from puzzleeval.schemas import Agent5Input, Agent3Result, ScreenedCandidate
    from puzzleeval.agents.implement_test_env import run_implement_test_env_agent
    from puzzleeval.provider_registry import load_registry, get_all_credentials

    # Reconstruct Pydantic models from dicts for Agent5Input type safety
    validated_dicts = state.agent4_result.get("validated_candidates", [])
    validated_models = [ScreenedCandidate(**c) for c in validated_dicts]

    if hasattr(state, "_agent3_model") and state._agent3_model:
        agent3_result_model = state._agent3_model
    else:
        agent3_result_model = Agent3Result(**state.agent3_result)

    # Load credentials — MUST pass candidates for name matching
    registry = load_registry()
    provider_creds = get_all_credentials(registry, validated_models)

    agent5_input = Agent5Input(
        validated_candidates=validated_models,
        user_understanding=user_understanding,
        test_cases=agent3_result_model,
        trace_id=state.trace_id,
        provider_credentials=provider_creds,
    )

    # Agent 5 creates runs/{trace_id}/harnesses/ relative to CWD
    # Set CWD to API root so harnesses go to puzzleeval-api/runs/
    original_cwd = os.getcwd()

    def _run_agent5_with_cwd():
        os.chdir(str(API_ROOT))
        try:
            return run_implement_test_env_agent(agent5_input, progress_callback=progress_callback)
        finally:
            os.chdir(original_cwd)

    result = await asyncio.to_thread(_run_agent5_with_cwd)
    result_dict = result.model_dump()
    return result_dict
