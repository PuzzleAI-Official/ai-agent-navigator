"""Contract system — public API.

The canonical owner of:
  * Contract loading from `puzzleeval/capability_playbooks/*.md`.
  * Selection (multi-layer: always-on + deterministic).
  * Coverage validation against per-task-type requirements.
  * Conflict detection and resolution.
  * Runtime gates (AD-007 — gates are Python, not markdown-controlled).

Public API:

    from puzzleeval.contracts import (
        TaskContext,
        select_contracts_for_task,
        compose_contract_block,
        run_gates,
    )

    task = TaskContext(
        agent_id="agent_5",
        phase="build",
        platform="win32",
        test_cases=(...),
        candidate=screened_candidate,
    )

    block = compose_contract_block(task)  # → str ready for prompt injection

    # OR full result with telemetry:
    result = select_contracts_for_task(task)
    for contract in result.contracts:
        print(contract.id, contract.metadata.priority)

    # Runtime gate enforcement (separate concern from selection):
    report = run_gates(harness_response, task)
    if not report.all_passed:
        for failure in report.failures:
            log_violation(failure)
"""

from __future__ import annotations

from puzzleeval.contracts.loader import (
    Contract,
    ContractRegistry,
    discover_contracts,
    parse_frontmatter,
    reload_registry,
)
from puzzleeval.contracts.coverage import (
    COVERAGE_REQUIREMENTS,
    ContractCoverageReport,
    CoverageRequirement,
    TaskSignature,
    applicable_requirements,
    validate_coverage,
)
from puzzleeval.contracts.errors import (
    ContractConflictError,
    ContractCoverageError,
    ContractError,
    ContractLoadError,
)
from puzzleeval.contracts.metadata import (
    ALLOWED_AGENTS,
    ALLOWED_CATEGORIES,
    ALLOWED_SELECTION_MODES,
    ContractMetadata,
    SelectorMetadata,
)
from puzzleeval.contracts.runtime_gates import (
    ALWAYS_ON_GATES,
    ContractGate,
    GateReport,
    GateResult,
    VoiceHarnessGate,
    gate_by_name,
    gate_names,
    run_gates,
)
from puzzleeval.contracts.selector import (
    Conflict,
    SelectionResult,
    compose_contract_block,
    select_contracts_for_task,
)
from puzzleeval.contracts.selector_spec import SelectorSpec, matches
from puzzleeval.contracts.task_context import TaskContext

__all__ = [
    # Loader
    "Contract",
    "ContractRegistry",
    "discover_contracts",
    "parse_frontmatter",
    "reload_registry",
    # Metadata
    "ContractMetadata",
    "SelectorMetadata",
    "ALLOWED_AGENTS",
    "ALLOWED_CATEGORIES",
    "ALLOWED_SELECTION_MODES",
    # Task / selector spec
    "TaskContext",
    "SelectorSpec",
    "matches",
    # Coverage
    "COVERAGE_REQUIREMENTS",
    "ContractCoverageReport",
    "CoverageRequirement",
    "TaskSignature",
    "applicable_requirements",
    "validate_coverage",
    # Errors
    "ContractError",
    "ContractLoadError",
    "ContractCoverageError",
    "ContractConflictError",
    # Runtime gates
    "ALWAYS_ON_GATES",
    "ContractGate",
    "GateReport",
    "GateResult",
    "VoiceHarnessGate",
    "gate_by_name",
    "gate_names",
    "run_gates",
    # Selection
    "Conflict",
    "SelectionResult",
    "compose_contract_block",
    "select_contracts_for_task",
]
