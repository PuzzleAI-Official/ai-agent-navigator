"""Exception types for the contract system."""

from __future__ import annotations


class ContractError(Exception):
    """Base class for all contract-related errors."""


class ContractLoadError(ContractError):
    """A contract markdown file failed to parse or validate.

    Raised at registry-load time when:
      * Frontmatter YAML is malformed.
      * Frontmatter fails Pydantic validation against ContractMetadata.
      * The contract body is empty or unreadable.

    Includes the file path so operators can fix the offending file.
    """

    def __init__(self, *, contract_path: str, reason: str) -> None:
        self.contract_path = contract_path
        self.reason = reason
        super().__init__(f"Contract {contract_path!r} failed to load: {reason}")


class ContractCoverageError(ContractError):
    """A task's selected contracts don't cover its required contracts.

    Raised by ``select_contracts_for_task(strict=True)`` when Layer 4's
    coverage validation finds at least one missing required contract.
    Caught by the pipeline runner and converted into a clear user-facing
    ``agent_blocked`` SSE event.

    Includes the task signature + missing contract IDs so operators can
    investigate why the gap appeared (missing CoverageRequirement entry?
    missing contract in registry? missing trigger_types match?).
    """

    def __init__(
        self,
        *,
        task_description: str,
        missing: list[str],
        selected: list[str],
    ) -> None:
        self.task_description = task_description
        self.missing = missing
        self.selected = selected
        super().__init__(
            f"Contract coverage gap for {task_description}: "
            f"missing {missing}; selected {selected}"
        )


class ContractConflictError(ContractError):
    """Two mutually-exclusive contracts both match a task.

    This is raised only when conflict resolution by priority can't pick a
    winner (e.g., equal priority on both sides). In normal operation,
    Layer 5 resolves conflicts silently by dropping the lower-priority
    contract.
    """

    def __init__(self, *, conflicts: list[tuple[str, str]]) -> None:
        self.conflicts = conflicts
        super().__init__(f"Unresolvable contract conflicts: {conflicts}")


__all__ = [
    "ContractError",
    "ContractLoadError",
    "ContractCoverageError",
    "ContractConflictError",
]
