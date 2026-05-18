from __future__ import annotations

from pathlib import Path

from puzzleeval.agents.agent5.execution import PersistentHarnessSession


def _write_harness(tmp_path: Path, source: str) -> None:
    (tmp_path / "harness.py").write_text(source, encoding="utf-8")


def test_persistent_runner_preserves_worker_state_across_turns(tmp_path):
    _write_harness(
        tmp_path,
        """
def run(input_data):
    state = input_data["session_state"]
    state["count"] = state.get("count", 0) + 1
    return {
        "success": True,
        "output": str(state["count"]),
        "raw_response": {"count": state["count"]},
    }
""",
    )

    with PersistentHarnessSession(tmp_path, None, turn_timeout=5) as session:
        first = session.turn({"turn_index": 0, "session_state": {}})
        second = session.turn({"turn_index": 1, "session_state": {}})

    assert first["raw_response"]["count"] == 1
    assert second["raw_response"]["count"] == 2


def test_persistent_runner_preserves_bytes_round_trip(tmp_path):
    _write_harness(
        tmp_path,
        """
def run(input_data):
    return {
        "success": True,
        "output": "audio",
        "raw_response": {"audio_bytes": b"abc123"},
    }
""",
    )

    with PersistentHarnessSession(tmp_path, None, turn_timeout=5) as session:
        result = session.turn({"turn_index": 0, "session_state": {}})

    assert result["raw_response"]["audio_bytes"] == b"abc123"


def test_persistent_runner_close_signal_cleans_resources(tmp_path):
    _write_harness(
        tmp_path,
        """
from pathlib import Path

class Resource:
    def close(self):
        Path("closed.txt").write_text("closed", encoding="utf-8")

def run(input_data):
    input_data["session_state"].setdefault("resource", Resource())
    return {"success": True, "output": "ok", "raw_response": {}}
""",
    )

    session = PersistentHarnessSession(tmp_path, None, turn_timeout=5)
    session.turn({"turn_index": 0, "session_state": {}})
    session.close()

    assert (tmp_path / "closed.txt").read_text(encoding="utf-8") == "closed"


def test_persistent_runner_timeout_kills_worker(tmp_path):
    _write_harness(
        tmp_path,
        """
import time

def run(input_data):
    time.sleep(5)
    return {"success": True, "output": "late", "raw_response": {}}
""",
    )

    session = PersistentHarnessSession(tmp_path, None, turn_timeout=1)
    result = session.turn({"turn_index": 0, "session_state": {}})

    assert result["success"] is False
    assert result["raw_response"]["error_type"] == "worker_timeout"


def test_persistent_runner_worker_death_is_not_restarted(tmp_path):
    _write_harness(
        tmp_path,
        """
import os

def run(input_data):
    if input_data.get("turn_index") == 1:
        os._exit(3)
    return {"success": True, "output": "alive", "raw_response": {}}
""",
    )

    with PersistentHarnessSession(tmp_path, None, turn_timeout=5) as session:
        first = session.turn({"turn_index": 0, "session_state": {}})
        second = session.turn({"turn_index": 1, "session_state": {}})
        third = session.turn({"turn_index": 2, "session_state": {}})

    assert first["success"] is True
    assert second["success"] is False
    assert second["raw_response"]["error_type"] in {"worker_died", "worker_timeout"}
    assert third["success"] is False
    assert third["raw_response"]["error_type"] in {"worker_died", "worker_closed"}


def test_persistent_runner_conversation_timeout_prevents_runaway(tmp_path):
    _write_harness(
        tmp_path,
        """
def run(input_data):
    return {"success": True, "output": "ok", "raw_response": {}}
""",
    )

    with PersistentHarnessSession(
        tmp_path,
        None,
        turn_timeout=5,
        conversation_timeout=0,
    ) as session:
        result = session.turn({"turn_index": 0, "session_state": {}})

    assert result["success"] is False
    assert result["raw_response"]["error_type"] == "conversation_timeout"
