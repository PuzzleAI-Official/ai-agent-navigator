"""Capability playbook loader — parses frontmatter + body, builds Contract objects.

Each ``.md`` file in this directory opens with a YAML frontmatter block:

    ---
    id: voice
    description: ...
    selectors:
      trigger_types: [voice_conversation, ...]
    ---

    # Markdown body (the prompt-teaching content)

This loader:
  1. Walks the directory at first call (memoized via lru_cache).
  2. Parses each `.md` file's frontmatter as YAML.
  3. Validates the frontmatter against ``ContractMetadata`` (Pydantic).
  4. Returns a tuple of ``Contract`` objects (metadata + body).

Failure handling:
  * Malformed YAML → ContractLoadError with file path.
  * Pydantic validation failure → ContractLoadError with file path + reason.
  * Missing file in installed wheel → empty registry (caught by CI guard
    ``tests/test_wheel_packaging.py`` at startup; downstream coverage gate
    will fail loudly when no contracts match).

Caching:
  * lru_cache memoizes the full registry. Subsequent calls are O(1).
  * Hot-reload: setting ``PUZZLEEVAL_PLAYBOOK_HOT_RELOAD=1`` env var
    bypasses the cache so devs can edit `.md` files without restarting.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Iterable

import yaml
from pydantic import ValidationError

from puzzleeval.contracts.errors import ContractLoadError
from puzzleeval.contracts.metadata import ContractMetadata


# ---------------------------------------------------------------------------
# Frontmatter delimiter — three dashes on their own line, per YAML
# frontmatter convention used by Anthropic Skills, Jekyll, Hugo, etc.
# ---------------------------------------------------------------------------
_FRONTMATTER_DELIMITER = "---"


@dataclass(frozen=True)
class Contract:
    """One contract loaded from disk.

    Fields:
        metadata: parsed + validated frontmatter.
        body: the markdown body (everything after the closing ``---``).
        filename: source filename (e.g., ``"voice.md"``). Used for error
                  messages and tooling.
    """

    metadata: ContractMetadata
    body: str
    filename: str

    @property
    def id(self) -> str:
        return self.metadata.id

    def render(self) -> str:
        """Return the contract body — what gets injected into prompts.

        Today this is just the body verbatim. Future: could template the
        body with task-specific values (candidate name, etc.).
        """
        return self.body


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str, *, source: str = "<unknown>") -> tuple[dict, str]:
    """Split a markdown file into (frontmatter dict, body string).

    Expected shape:
      ---
      <yaml>
      ---
      <body>

    When no frontmatter is present, returns ``({}, text)`` — the caller
    decides whether absence of frontmatter is an error.

    Raises ContractLoadError on malformed YAML.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return ({}, text)

    # Find the closing delimiter
    close_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_DELIMITER:
            close_idx = i
            break

    if close_idx is None:
        raise ContractLoadError(
            contract_path=source,
            reason="Frontmatter has opening `---` but no closing `---`.",
        )

    yaml_text = "\n".join(lines[1:close_idx])
    body = "\n".join(lines[close_idx + 1:])
    # Preserve trailing newline if the original file had one. Many
    # downstream consumers (and source-grep tests) compare body content
    # against legacy inline string constants which traditionally end
    # with \n. splitlines() drops the trailing newline, so we restore it.
    if text.endswith("\n") and not body.endswith("\n"):
        body = body + "\n"

    if not yaml_text.strip():
        return ({}, body.lstrip())

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ContractLoadError(
            contract_path=source,
            reason=f"YAML parse failed: {exc}",
        ) from exc

    if parsed is None:
        return ({}, body.lstrip())

    if not isinstance(parsed, dict):
        raise ContractLoadError(
            contract_path=source,
            reason=f"Frontmatter must be a YAML mapping; got {type(parsed).__name__}.",
        )

    return (parsed, body.lstrip())


def _parse_contract_text(text: str, *, filename: str) -> Contract:
    """Parse the raw text of one .md file into a Contract.

    Raises ContractLoadError on any validation failure (caller decides
    whether to fail-fast or skip).
    """
    raw_meta, body = parse_frontmatter(text, source=filename)
    if not raw_meta:
        raise ContractLoadError(
            contract_path=filename,
            reason="Missing frontmatter. Every contract must declare an id, description, and selectors.",
        )
    if not body.strip():
        raise ContractLoadError(
            contract_path=filename,
            reason="Empty contract body. The markdown teaching content cannot be blank.",
        )
    try:
        metadata = ContractMetadata(**raw_meta)
    except ValidationError as exc:
        raise ContractLoadError(
            contract_path=filename,
            reason=f"Frontmatter schema validation failed: {exc}",
        ) from exc
    return Contract(metadata=metadata, body=body, filename=filename)


# ---------------------------------------------------------------------------
# Auto-discovery — read every .md file in the directory.
# ---------------------------------------------------------------------------
# Files starting with `_` (e.g., this loader's own files) and AUTHORING.md
# are skipped — they're loader code or author docs, not contracts.
# ---------------------------------------------------------------------------

def _list_contract_filenames() -> list[str]:
    """List filenames of all contract .md files in capability_playbooks/."""
    out: list[str] = []
    pkg = resources.files("puzzleeval").joinpath("capability_playbooks")
    for entry in pkg.iterdir():
        name = entry.name
        if not name.endswith(".md"):
            continue
        if name.startswith("_"):
            continue
        if name == "AUTHORING.md":
            continue
        out.append(name)
    return sorted(out)


def _read_contract_file(filename: str) -> str:
    """Read one .md file by filename. Returns raw text."""
    return (
        resources.files("puzzleeval")
        .joinpath("capability_playbooks", filename)
        .read_text(encoding="utf-8")
    )


def _hot_reload_enabled() -> bool:
    return os.environ.get("PUZZLEEVAL_PLAYBOOK_HOT_RELOAD", "0") == "1"


@lru_cache(maxsize=None)
def _discover_contracts_cached() -> tuple[Contract, ...]:
    """Memoized version of discover_contracts.

    Parallel-loads all .md files via ThreadPoolExecutor. Per-file errors
    propagate as ContractLoadError so the operator gets a clear failure
    at startup (not silent partial registry).
    """
    filenames = _list_contract_filenames()
    if not filenames:
        return ()

    contracts: list[Contract] = []
    # Parallel read + parse — Claude Code pattern from commands.ts:258.
    # max_workers=4 is plenty for parsing 3-50 small markdown files.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_load_one, filename): filename for filename in filenames
        }
        for future, filename in zip(futures, futures.values()):
            contract = future.result()  # raises ContractLoadError on failure
            contracts.append(contract)

    # Sort by filename for stable ordering (priority-sort is selector's job)
    contracts.sort(key=lambda c: c.filename)
    return tuple(contracts)


def _load_one(filename: str) -> Contract:
    """Read + parse one contract file. Used by ThreadPoolExecutor."""
    text = _read_contract_file(filename)
    return _parse_contract_text(text, filename=filename)


def discover_contracts() -> tuple[Contract, ...]:
    """Discover and parse every contract markdown file.

    Returns a tuple of Contract objects sorted by filename.

    Raises ContractLoadError on the first malformed contract — this is
    a startup-time guarantee. We'd rather fail loudly than ship with a
    silently-skipped contract.

    Hot-reload: setting PUZZLEEVAL_PLAYBOOK_HOT_RELOAD=1 bypasses the
    cache so devs can edit `.md` files and see effect on next call.
    Off by default (production should use the cache for stability).
    """
    if _hot_reload_enabled():
        # Drop both lru_cache layers (this fn + per-file lru_cache below)
        _discover_contracts_cached.cache_clear()
    return _discover_contracts_cached()


def reload_registry() -> tuple[Contract, ...]:
    """Force-reload the registry from disk.

    Used by tests that mutate the directory and want to see changes
    without setting the hot-reload env var.
    """
    _discover_contracts_cached.cache_clear()
    return _discover_contracts_cached()


# ---------------------------------------------------------------------------
# ContractRegistry — public API for selector.py.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractRegistry:
    """Read-only view of all loaded contracts.

    Wraps ``discover_contracts()`` with convenience methods used by the
    selector (filter by selection_mode, by agent, etc.).
    """

    contracts: tuple[Contract, ...]

    @classmethod
    def cached(cls) -> "ContractRegistry":
        """Build a registry from the cached discovery."""
        return cls(contracts=discover_contracts())

    def __len__(self) -> int:
        return len(self.contracts)

    def __iter__(self):
        return iter(self.contracts)

    def by_id(self, contract_id: str) -> Contract:
        """Look up a contract by id. Raises KeyError if missing."""
        for c in self.contracts:
            if c.metadata.id == contract_id:
                return c
        raise KeyError(f"Unknown contract id: {contract_id!r}")

    def has(self, contract_id: str) -> bool:
        """True if a contract with this id is in the registry."""
        return any(c.metadata.id == contract_id for c in self.contracts)

    def for_agent(self, agent_id: str) -> tuple[Contract, ...]:
        """Contracts that opt into this agent_id."""
        return tuple(
            c for c in self.contracts
            if agent_id in c.metadata.applies_to_agents
        )

    def always_on(self, agent_id: str | None = None) -> tuple[Contract, ...]:
        """Contracts with selection_mode=always_on, optionally agent-filtered."""
        out = [c for c in self.contracts if c.metadata.selection_mode == "always_on"]
        if agent_id is not None:
            out = [c for c in out if agent_id in c.metadata.applies_to_agents]
        return tuple(out)

    def deterministic(self, agent_id: str | None = None) -> tuple[Contract, ...]:
        """Contracts with selection_mode=deterministic."""
        out = [c for c in self.contracts if c.metadata.selection_mode == "deterministic"]
        if agent_id is not None:
            out = [c for c in out if agent_id in c.metadata.applies_to_agents]
        return tuple(out)

    def conflict_graph(self) -> dict[str, frozenset[str]]:
        """Build the undirected conflict graph from mutually_exclusive_with.

        Returns a dict mapping contract_id → frozenset of conflicting
        contract_ids. Used by Layer 5 conflict detection.
        """
        graph: dict[str, set[str]] = {}
        for c in self.contracts:
            for excluded in c.metadata.mutually_exclusive_with:
                graph.setdefault(c.metadata.id, set()).add(excluded)
                graph.setdefault(excluded, set()).add(c.metadata.id)
        return {k: frozenset(v) for k, v in graph.items()}


__all__ = [
    "Contract",
    "ContractRegistry",
    "discover_contracts",
    "parse_frontmatter",
    "reload_registry",
]
