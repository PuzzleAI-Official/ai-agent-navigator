from puzzleeval.agents.agent5.conversation_log import compute_conversation_summary
from puzzleeval.agents.agent5.build_loop import (
    _start_tool_heartbeat,
    _start_turn_heartbeat,
)


def test_conversation_summary_surfaces_top_slow_research_turns():
    log = [
        {
            "turn": 0,
            "model": "claude-sonnet-4-6",
            "cost_usd": 0.01,
            "latency_ms": 1000,
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_create_tokens": 0,
            "tool_calls": [],
            "tool_results": [],
        },
        {
            "turn": 4,
            "model": "claude-sonnet-4-6",
            "cost_usd": 1.2,
            "latency_ms": 250000,
            "input_tokens": 10,
            "output_tokens": 1000,
            "cache_read_tokens": 20000,
            "cache_create_tokens": 5000,
            "tool_calls": [
                {"tool": "patch_file", "input": {"filename": "api_spec.txt"}},
            ],
            "tool_results": [
                {
                    "tool": "web_fetch",
                    "url": "https://docs.example.com/blocked",
                    "requested_url": "https://docs.example.com/blocked",
                    "chars_returned": 0,
                    "status": "error",
                    "error_code": "url_not_accessible",
                },
                {
                    "tool": "web_search",
                    "query": "example realtime websocket docs",
                    "result_count": 10,
                    "top_results": [
                        {"title": "Realtime Docs", "url": "https://docs.example.com/realtime"},
                    ],
                },
                {"tool": "advisor", "result": ""},
            ],
            "iteration_summary": {"count": 7, "advisor_count": 1},
        },
    ]

    summary = compute_conversation_summary(log, "TestCo")

    slow = summary["top_slow_turns"][0]
    assert slow["turn"] == 4
    assert slow["phase_hint"] == "research"
    assert slow["tool_calls"] == ["patch_file:api_spec.txt"]
    assert slow["server_result_counts"]["web_fetch_empty"] == 1
    assert slow["server_result_counts"]["web_fetch_errors"] == 1
    assert slow["server_result_counts"]["web_search_results"] == 1
    assert slow["research_details"]["web_fetches"][0]["url"] == "https://docs.example.com/blocked"
    assert slow["research_details"]["web_searches"][0]["query"] == "example realtime websocket docs"
    assert slow["iteration_summary"]["count"] == 7


def test_conversation_summary_research_roi_preserves_urls_and_queries():
    log = [
        {
            "turn": 2,
            "model": "claude-sonnet-4-6",
            "cost_usd": 0.5,
            "latency_ms": 120000,
            "input_tokens": 10,
            "output_tokens": 100,
            "cache_read_tokens": 1000,
            "cache_create_tokens": 100,
            "tool_calls": [
                {"tool": "web_fetch", "id": "fetch-1", "input": {"url": "https://bad.example/docs"}},
                {"tool": "web_search", "id": "search-1", "input": {"query": "bad example api"}},
            ],
            "tool_results": [
                {
                    "tool": "web_fetch",
                    "id": "fetch-1",
                    "url": "https://bad.example/docs",
                    "requested_url": "https://bad.example/docs",
                    "chars_returned": 0,
                    "status": "empty",
                },
                {
                    "tool": "web_search",
                    "id": "search-1",
                    "query": "bad example api",
                    "result_count": 3,
                    "top_results": [
                        {"title": "Docs", "url": "https://good.example/docs"},
                    ],
                },
            ],
        },
    ]

    summary = compute_conversation_summary(log, "TestCo")
    research = summary["efficiency_analysis"]["research"]

    assert research["empty_web_fetch_by_url"] == {"https://bad.example/docs": 1}
    assert research["fetch_attempts"] == [
        {
            "turn": 2,
            "url": "https://bad.example/docs",
            "status": "empty",
            "chars_returned": 0,
            "error_code": "",
        }
    ]
    assert research["web_search_queries"] == [
        {
            "turn": 2,
            "query": "bad example api",
            "result_count": 3,
            "top_urls": ["https://good.example/docs"],
        }
    ]


def test_conversation_summary_efficiency_analysis_counts_waste_and_patch_signals():
    log = [
        {
            "turn": 0,
            "model": "claude-sonnet-4-6",
            "cost_usd": 0.02,
            "latency_ms": 1000,
            "tool_calls": [
                {"tool": "patch_file", "input": {"filename": "api_spec.txt"}},
            ],
            "tool_results": [
                {"tool": "patch_file", "is_error": False, "patch_path": "api_spec.txt"},
                {"tool": "web_fetch", "chars_returned": 1000},
            ],
            "iterations": [],
        },
        {
            "turn": 1,
            "model": "claude-opus-4-7",
            "cost_usd": 0.03,
            "latency_ms": 2000,
            "tool_calls": [
                {"tool": "write_file", "input": {"filename": "_agent_state/build_plan.md"}},
            ],
            "tool_results": [
                {
                    "tool": "write_file",
                    "is_error": False,
                    "wrote_path": "_agent_state/build_plan.md",
                },
            ],
            "iterations": [],
        },
        {
            "turn": 2,
            "model": "claude-opus-4-7",
            "cost_usd": 0.04,
            "latency_ms": 120000,
            "tool_calls": [
                {"tool": "run_code", "input": {"command": "python live_test.py"}},
            ],
            "tool_results": [
                {
                    "tool": "run_code",
                    "is_error": True,
                    "result": "Error: command timed out after 120 seconds",
                },
            ],
            "iterations": [],
        },
    ]

    summary = compute_conversation_summary(log, "TestCo")
    eff = summary["efficiency_analysis"]

    assert eff["class_totals"]["productive"]["turns"] == 1
    assert eff["class_totals"]["overhead"]["turns"] == 1
    assert eff["class_totals"]["waste"]["turns"] == 1
    assert eff["waste_signals"]["artifact_only_turns"] == 1
    assert eff["waste_signals"]["tool_timeout_turns"] == 1
    assert eff["patch_by_file"]["api_spec.txt"] == 1
    assert eff["research"]["web_fetch_results"] == 1
    assert eff["turns"][2]["signals"] == ["run_code", "tool_error", "tool_timeout"]


def test_rejected_artifact_write_is_not_treated_as_persisted_boundary():
    log = [
        {
            "turn": 0,
            "model": "claude-opus-4-7",
            "cost_usd": 0.02,
            "latency_ms": 1000,
            "tool_calls": [
                {"tool": "write_file", "input": {"filename": "_agent_state/implementation_plan.json"}},
            ],
            "tool_results": [
                {
                    "tool": "write_file",
                    "is_error": True,
                    "persisted": False,
                    "attempted_path": "_agent_state/implementation_plan.json",
                    "result": "Error: implementation_plan.json failed implementation-plan validation",
                },
            ],
            "iterations": [],
        },
        {
            "turn": 1,
            "model": "claude-opus-4-7",
            "cost_usd": 0.03,
            "latency_ms": 1000,
            "tool_calls": [
                {"tool": "write_file", "input": {"filename": "_agent_state/implementation_plan.json"}},
            ],
            "tool_results": [
                {
                    "tool": "write_file",
                    "is_error": False,
                    "persisted": True,
                    "wrote_path": "_agent_state/implementation_plan.json",
                    "result": "Written _agent_state/implementation_plan.json",
                },
            ],
            "iterations": [],
        },
    ]

    summary = compute_conversation_summary(log, "TestCo")

    assert summary["boundary_turns"]["build_gate_accepted_at"] == 1
    first_turn_signals = set(summary["efficiency_analysis"]["turns"][0]["signals"])
    assert "tool_error" in first_turn_signals
    assert "write" in first_turn_signals


def test_conversation_summary_splits_model_and_tool_time():
    log = [
        {
            "turn": 0,
            "model": "claude-opus-4-7",
            "cost_usd": 0.02,
            "latency_ms": 1500,
            "tool_calls": [
                {"tool": "run_code", "input": {"command": "python live_test.py"}},
            ],
            "tool_results": [
                {
                    "tool": "run_code",
                    "is_error": False,
                    "result": "ok",
                    "tool_elapsed_ms": 42000,
                    "command": "python live_test.py",
                },
            ],
            "iterations": [],
        },
        {
            "turn": 1,
            "model": "claude-opus-4-7",
            "cost_usd": 0.01,
            "latency_ms": 500,
            "tool_calls": [
                {"tool": "read_file", "input": {"filename": "harness.py"}},
            ],
            "tool_results": [
                {
                    "tool": "read_file",
                    "is_error": False,
                    "result": "code",
                    "tool_elapsed_ms": 100,
                },
            ],
            "iterations": [],
        },
    ]

    summary = compute_conversation_summary(log, "TestCo")
    timing = summary["efficiency_analysis"]["tool_timing"]

    assert timing["model_api_latency_ms"] == 2000
    assert timing["custom_tool_elapsed_ms"] == 42100
    assert timing["observed_model_plus_tool_ms"] == 44100
    assert timing["by_tool"]["run_code"]["elapsed_ms"] == 42000
    assert timing["by_tool"]["read_file"]["avg_elapsed_ms"] == 100
    assert timing["top_slow_tool_results"][0]["tool"] == "run_code"
    assert summary["efficiency_analysis"]["turns"][0]["custom_tool_elapsed_ms"] == 42000


def test_conversation_summary_uses_structured_deduped_read_signal():
    log = [
        {
            "turn": 0,
            "model": "claude-opus-4-7",
            "tool_calls": [{"tool": "read_file", "input": {"filename": "harness.py"}}],
            "tool_results": [
                {
                    "tool": "read_file",
                    "deduped_read": True,
                    "result": "File unchanged since last read.",
                },
            ],
            "iterations": [],
        }
    ]

    summary = compute_conversation_summary(log, "TestCo")
    eff = summary["efficiency_analysis"]

    assert eff["waste_signals"]["redundant_read_hints"] == 1
    assert "deduped_read_stub" in eff["turns"][0]["signals"]


def test_conversation_summary_links_repair_episode_and_patch_churn():
    log = [
        {
            "turn": 0,
            "model": "claude-opus-4-7",
            "cost_usd": 0.02,
            "latency_ms": 1000,
            "tool_calls": [
                {"tool": "run_code", "input": {"command": "python smoke_test.py"}},
            ],
            "tool_results": [
                {
                    "tool": "run_code",
                    "is_error": True,
                    "result": "Traceback: missing session reuse",
                    "command": "python smoke_test.py",
                    "tool_elapsed_ms": 2000,
                },
            ],
            "iterations": [],
        },
        {
            "turn": 1,
            "model": "claude-opus-4-7",
            "cost_usd": 0.03,
            "latency_ms": 2000,
            "tool_calls": [
                {"tool": "patch_file", "input": {"filename": "harness.py"}},
            ],
            "tool_results": [
                {
                    "tool": "patch_file",
                    "is_error": False,
                    "result": "patched",
                    "patch_path": "harness.py",
                    "patch_diff_chars": 3100,
                    "tool_elapsed_ms": 50,
                },
            ],
            "iterations": [],
        },
        {
            "turn": 2,
            "model": "claude-opus-4-7",
            "cost_usd": 0.02,
            "latency_ms": 1000,
            "tool_calls": [
                {"tool": "run_code", "input": {"command": "python smoke_test.py"}},
            ],
            "tool_results": [
                {
                    "tool": "run_code",
                    "is_error": False,
                    "result": "SMOKE TEST PASSED",
                    "command": "python smoke_test.py",
                    "tool_elapsed_ms": 2000,
                },
            ],
            "iterations": [],
        },
        {
            "turn": 3,
            "model": "claude-opus-4-7",
            "cost_usd": 0.04,
            "latency_ms": 3000,
            "tool_calls": [
                {"tool": "patch_file", "input": {"filename": "harness.py"}},
            ],
            "tool_results": [
                {
                    "tool": "patch_file",
                    "is_error": False,
                    "result": "patched again",
                    "patch_path": "harness.py",
                    "patch_diff_chars": -40,
                    "tool_elapsed_ms": 40,
                },
            ],
            "iterations": [],
        },
    ]

    summary = compute_conversation_summary(log, "TestCo")
    eff = summary["efficiency_analysis"]

    episode = eff["repair_episodes"][0]
    assert episode["failure_turn"] == 0
    assert episode["failure_tools"] == ["run_code"]
    assert episode["repair_actions"][0]["path"] == "harness.py"
    assert episode["next_validation"]["turn"] == 2
    assert episode["resolved"] is True

    churn = eff["churn_analysis"]
    assert churn["repeated_patch_files"] == {"harness.py": 2}
    assert churn["patch_by_file"]["harness.py"]["total_abs_diff_chars"] == 3140
    assert churn["large_patch_events"][0]["path"] == "harness.py"
    assert churn["late_scaffold_changes"][0]["turn"] == 3


def test_turn_heartbeat_writes_started_progress_event(tmp_path):
    events = []

    stop = _start_turn_heartbeat(
        progress_callback=lambda event_type, payload: events.append((event_type, payload)),
        sandbox_dir=tmp_path,
        candidate_name="OpenAI",
        turn=3,
        max_turns=40,
        phase="researching",
        model="claude-sonnet-4-6",
        interval_seconds=999,
    )
    stop()

    assert events[0][0] == "build_turn_started"
    assert events[0][1]["turn"] == 4
    progress_path = tmp_path / "_agent_state" / "build_progress.jsonl"
    text = progress_path.read_text(encoding="utf-8")
    assert '"event": "build_turn_started"' in text
    assert '"phase": "researching"' in text


def test_tool_heartbeat_writes_run_code_progress_events(tmp_path):
    events = []

    stop, complete = _start_tool_heartbeat(
        progress_callback=lambda event_type, payload: events.append((event_type, payload)),
        sandbox_dir=tmp_path,
        candidate_name="ElevenLabs",
        turn=20,
        phase="validating",
        tool_name="run_code",
        tool_summary="python live_test.py",
        interval_seconds=999,
    )
    stop()
    complete({"exit_code": -1, "is_error": True})

    assert events[0][0] == "build_tool_started"
    assert events[-1][0] == "build_tool_completed"
    assert events[-1][1]["is_error"] is True
    text = (tmp_path / "_agent_state" / "build_progress.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"event": "build_tool_started"' in text
    assert '"event": "build_tool_completed"' in text
    assert "python live_test.py" in text
