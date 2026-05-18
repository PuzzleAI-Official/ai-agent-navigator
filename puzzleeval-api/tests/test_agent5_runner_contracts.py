import inspect

from services import pipeline_runner


def test_agent5_runner_uses_explicit_runs_root_instead_of_chdir():
    source = inspect.getsource(pipeline_runner._run_real_agent5)

    assert "runs_root=str(API_ROOT / \"runs\")" in source
    assert "os.chdir" not in source
    assert "_run_agent5_with_cwd" not in source


def test_agent4_input_owns_runs_root_not_agent1():
    from puzzleeval.schemas import Agent1Input, Agent4Input, Agent5Input

    assert "runs_root" not in Agent1Input.model_fields
    assert "runs_root" in Agent4Input.model_fields
    assert "runs_root" in Agent5Input.model_fields


def test_venv_precreate_receives_same_runs_root_as_agent5():
    source = inspect.getsource(pipeline_runner._kick_off_venv_precreate)

    assert "runs_root: Path | str | None = None" in source
    assert "runs_root=runs_root" in source


def test_agent5_progress_emits_candidates_selected_once():
    source = inspect.getsource(pipeline_runner.run_pipeline)

    assert source.count('emit("candidates_selected", data)') == 1
    structural_start = source.index('if event_type == "candidates_selected":')
    generic_emit = source.index("emit(event_type, data)", structural_start)
    selected_emit = source.index('emit("candidates_selected", data)', structural_start)
    assert selected_emit < generic_emit
