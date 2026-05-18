from puzzleeval.cli import _filter_user_understanding
from puzzleeval.schemas import (
    Constraints,
    ScopeTestSpec,
    SubTask,
    TestPlan,
    UserUnderstandingOutput,
    WorkflowBlueprint,
    WorkflowStep,
)


def _spec(scope_id: str, target: int) -> ScopeTestSpec:
    return ScopeTestSpec(
        scope_id=scope_id,
        test_mode="synthetic_text",
        input_type="text",
        output_type="free_text",
        input_description=f"input for {scope_id}",
        expected_output_description=f"output for {scope_id}",
        sample_input="sample",
        sample_output="sample",
        test_count_target=target,
    )


def test_filter_user_understanding_preserves_matching_test_plan_scopes():
    first = SubTask(description="Answer support calls", capability="voice agent", search_keywords=["voice"])
    second = SubTask(description="Sync CRM updates", capability="crm sync", search_keywords=["crm"])
    uo = UserUnderstandingOutput(
        summary="Need voice support and CRM sync",
        sub_tasks=[first, second],
        domain="support",
        search_keywords=["support ai"],
        constraints=Constraints(),
        workflow_summary=None,
        workflow=WorkflowBlueprint(steps=[
            WorkflowStep(
                id="step_1",
                role="voice",
                description="Answer calls",
                capability="voice agent",
                input_from="user",
                output_format="conversation",
            ),
            WorkflowStep(
                id="step_2",
                role="crm",
                description="Update CRM",
                capability="crm sync",
                input_from="step_1",
                output_format="action",
                depends_on=["step_1"],
            ),
        ]),
        test_plan=TestPlan(
            scope_specs=[_spec("step_1", 6), _spec("step_2", 4)],
            total_test_target=10,
            notes="scope-specific targets",
        ),
    )

    filtered = _filter_user_understanding(uo, [first])

    assert [st.description for st in filtered.sub_tasks] == ["Answer support calls"]
    assert filtered.test_plan is not None
    assert [spec.scope_id for spec in filtered.test_plan.scope_specs] == ["step_1"]
    assert filtered.test_plan.total_test_target == 6
