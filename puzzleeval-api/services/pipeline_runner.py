import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

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
    state.total_cost_usd += result_dict.get("cost_usd", 0)
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
        """Save any data (dict or Pydantic model) to the run directory."""
        try:
            path = run_dir / filename
            if hasattr(data, "model_dump_json"):
                path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
            else:
                path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass  # Non-critical — don't break pipeline for logging

    # Save conversation history
    _save_json("agent_1_conversation.json", state.conversation_history)
    _save_json("agent_1_output.json", state.agent1_result)

    try:
        emit("pipeline_started", {"trace_id": state.trace_id})

        user_understanding = _get_user_understanding(state)

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
            emit("candidates_found", {"candidates": [
                {
                    "name": c.get("name", ""),
                    "provider": c.get("provider", ""),
                    "description": c.get("description", ""),
                    "relevance_score": c.get("relevance_score", 0),
                    "adoption_difficulty": c.get("adoption_difficulty", "medium"),
                    "claimed_capabilities": c.get("claimed_capabilities", []),
                }
                for c in candidates
            ]})
            emit("agent_activity", {"agent": "agent_2", "message": f"Research complete — {len(candidates)} candidates selected", "status": "success"})
            emit("agent_completed", {"agent": "agent_2", "cost_usd": state.agent2_result.get("cost_usd", 0)})
            _save_json("agent_2_output.json", state.agent2_result)

            if state.cancel_requested:
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
            _save_json("agent_4_output.json", state.agent4_result)

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
            _save_json("agent_3_output.json", state.agent3_result)

        # Run both branches in parallel — Agent 4 starts as soon as Agent 2 finishes
        await asyncio.gather(_branch_a_research_and_screening(), _branch_b_test_generation())

        if state.cancel_requested:
            emit("pipeline_cancelled", {"cancelled_at_agent": "after_screening"})
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

        emit("agent_completed", {"agent": "agent_5", "cost_usd": state.agent5_result.get("total_build_cost_usd", 0) + state.agent5_result.get("total_test_cost_usd", 0)})
        _save_json("agent_5_output.json", state.agent5_result)

        # ------------------------------------------------------------------
        # Report generation step
        # ------------------------------------------------------------------
        emit("report_generating", {})
        emit("agent_activity", {"agent": "report", "message": "Generating evaluation report...", "status": "progress"})
        await asyncio.sleep(5.0)
        emit("agent_activity", {"agent": "report", "message": "Report ready", "status": "success"})

        # ------------------------------------------------------------------
        # Pipeline complete
        # ------------------------------------------------------------------
        total_cost = sum(
            r.get("cost_usd", 0) if r else 0
            for r in [state.agent2_result, state.agent3_result, state.agent4_result]
        ) + state.agent5_result.get("total_build_cost_usd", 0) + state.agent5_result.get("total_test_cost_usd", 0)
        state.total_cost_usd += total_cost

        emit("pipeline_completed", {
            "total_cost_usd": state.total_cost_usd,
            "summary": state.agent5_result.get("test_execution_summary", "Pipeline complete."),
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
    from puzzleeval.agents.research import run_research_agent, inject_registry_candidates

    agent2_input = Agent2Input(
        user_understanding=user_understanding,
        trace_id=state.trace_id,
    )

    result = await asyncio.to_thread(run_research_agent, agent2_input)

    # CRITICAL: Apply the registry candidate injection shim (same as CLI line 356)
    result = inject_registry_candidates(result)

    result_dict = result.model_dump()
    return result_dict, result


async def _run_real_agent3(state: RunState, user_understanding):
    """Run real Agent 3/3F (Test Generation). Returns (dict, model) tuple."""
    from puzzleeval.schemas import Agent3Input
    from puzzleeval.agents.synthetic_tests import run_synthetic_tests_agent
    from puzzleeval.agents.synthetic_tests_file import run_file_tests_agent

    test_file_paths = [f.get("path") for f in state.uploaded_files if f.get("path")]

    agent3_input = Agent3Input(
        user_understanding=user_understanding,
        trace_id=state.trace_id,
        test_file_paths=test_file_paths if test_file_paths else None,
    )

    # Route to file mode if test files provided
    if test_file_paths:
        result = await asyncio.to_thread(run_file_tests_agent, agent3_input)
    else:
        result = await asyncio.to_thread(run_synthetic_tests_agent, agent3_input)

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
