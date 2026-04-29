"""ContractMetadata — Pydantic model for capability_playbook frontmatter.

Each `.md` file in `puzzleeval/capability_playbooks/` opens with a YAML
frontmatter block. The frontmatter is parsed and validated against this
schema at registry-load time. A schema-invalid frontmatter raises
``ContractLoadError`` with the offending file path — caught early at
startup, never silently degraded.

This is the "data contract" between contract authors and the runtime.
Authoring guide lives at ``puzzleeval/capability_playbooks/AUTHORING.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Allowed values for enum-shaped fields. Update these in lockstep with
# what TaskContext exposes / what CoverageRequirement understands.
# ---------------------------------------------------------------------------
ALLOWED_CATEGORIES = frozenset({
    "modality",        # voice, streaming, code-gen, etc.
    "platform",        # windows, linux, macos
    "interaction",     # websocket, async-polling, sse, multipart
    "safety",          # side-effects, credential isolation
    "meta",            # error-recovery, budget-constraints
})

ALLOWED_SELECTION_MODES = frozenset({
    "always_on",        # loaded if applies_to_agents matches (no test_cases inspection)
    "deterministic",    # Tier 1 — schema-driven enum match
})
# Removed `llm_routed` (2026-04-27, Round 5 refinement): Codex pointed out
# that shipping a Tier-2 LLM-routing framework with NO consumer is
# speculative platform architecture. Today every contract uses
# always_on or deterministic. When/if a real use case for LLM-routed
# selection emerges (e.g., 30+ contracts with overlapping triggers
# where deterministic match is genuinely ambiguous), add the value
# back here AND re-implement Layer 3 with a real router. Until then,
# this enum REJECTS llm_routed at frontmatter-parse time so authors
# can't accidentally ship a contract that points at vapor.

ALLOWED_AGENTS = frozenset({
    "agent_1", "agent_2", "agent_3", "agent_3f", "agent_4", "agent_5",
})


# ---------------------------------------------------------------------------
# Criticality — does downstream behavior DEPEND on this contract?
# ---------------------------------------------------------------------------
# Per Codex pushback A: required contracts must be selected DETERMINISTICALLY.
# The LLM router (Tier 2, when implemented) can suggest OPTIONAL contracts but
# must NOT be capable of skipping a required contract — that would silently
# break correctness.
#
# required: a CoverageRequirement may depend on this contract being selected.
#           Layer 4 will report a coverage gap if it's missing. Cannot be
#           selection_mode=llm_routed.
# optional: nice-to-have guidance. The LLM router may include or skip it.
#           Never appears in CoverageRequirement.required_contract_ids.
# ---------------------------------------------------------------------------
ALLOWED_CRITICALITIES = frozenset({"required", "optional"})


class SelectorMetadata(BaseModel):
    """Frontmatter shape for the ``selectors`` block.

    Mirrors ``SelectorSpec`` but as a Pydantic model so frontmatter parsing
    catches schema errors early.
    """
    trigger_types: list[str] = Field(default_factory=list)
    trigger_platforms: list[str] = Field(default_factory=list)
    trigger_candidate_metadata: dict[str, str] = Field(default_factory=dict)
    require_all: bool = False


class ContractMetadata(BaseModel):
    """Pydantic-validated frontmatter for a contract markdown file.

    Required fields: ``id``, ``description``. Everything else has sensible
    defaults so simple contracts (always-on platform contracts) don't
    need to spell out every field.
    """

    id: str = Field(
        description=(
            "Stable contract identifier. Must be unique across the "
            "registry. Used in selectors, conflict resolution, and "
            "telemetry. Convention: lowercase snake_case matching the "
            "filename minus `.md` (e.g., voice.md → id='voice')."
        ),
    )
    version: int = Field(
        default=1,
        ge=1,
        description=(
            "Contract version. Bump when the body content changes in "
            "breaking ways (would invalidate downstream behavior). "
            "Mostly informational today — used in telemetry to track "
            "which contract version was active per build."
        ),
    )
    title: str = Field(
        default="",
        description="Human-readable title shown in tooling.",
    )
    description: str = Field(
        description=(
            "One-paragraph description of what this contract teaches. "
            "USED BY TIER 2 LLM ROUTER — write this as a short, accurate "
            "summary of when the contract should apply. Keep under 500 "
            "characters; the router reads this verbatim."
        ),
    )
    category: str = Field(
        default="modality",
        description=(
            "Contract category (modality | platform | interaction | "
            "safety | meta). Used for telemetry grouping and CLI tooling. "
            "Selection logic does NOT branch on category — this is "
            "metadata, not behavior."
        ),
    )
    selectors: SelectorMetadata = Field(
        default_factory=SelectorMetadata,
        description=(
            "Predicate against TaskContext. Empty selectors + "
            "selection_mode=deterministic → contract never selects "
            "(intentional: forces explicit always_on or llm_routed)."
        ),
    )
    selection_mode: str = Field(
        default="deterministic",
        description=(
            "How this contract gets selected. always_on = loaded if "
            "applies_to_agents matches. deterministic = schema-driven "
            "enum match against trigger_types/trigger_platforms/"
            "trigger_candidate_metadata (default). The reserved value "
            "'llm_routed' is rejected today — Tier 2 LLM routing was "
            "removed in Round 5 as speculative architecture. When a "
            "real use case emerges, add it back to ALLOWED_SELECTION_MODES."
        ),
    )
    priority: int = Field(
        default=100,
        description=(
            "Composition order. Higher priority injects EARLIER in the "
            "rendered prompt (lower in numbering = injected later, "
            "weighted higher by LLMs since later instructions win). Also "
            "used by Layer 5 conflict resolution: when two mutually-"
            "exclusive contracts both match, the one with HIGHER priority "
            "wins."
        ),
    )
    applies_to_agents: list[str] = Field(
        default_factory=lambda: ["agent_5"],
        description=(
            "Which agent(s) can load this contract. Default agent_5 "
            "since most contracts today are Agent-5-specific. Multi-agent "
            "contracts list each agent explicitly."
        ),
    )
    paired_gates: list[str] = Field(
        default_factory=list,
        description=(
            "DESCRIPTIVE ONLY — names of Python gate classes that enforce "
            "this contract at runtime. The runtime registry "
            "(puzzleeval/contracts/runtime_gates.py) is the source of "
            "truth for which gates run. Editing this field does NOT "
            "change runtime gate execution. Listed here for "
            "documentation + searchability."
        ),
    )
    related_contracts: list[str] = Field(
        default_factory=list,
        description=(
            "Other contract IDs related to this one. Currently used by "
            "tooling (`puzzleeval contract graph` renders the dependency "
            "DAG). No runtime effect."
        ),
    )
    mutually_exclusive_with: list[str] = Field(
        default_factory=list,
        description=(
            "Other contract IDs that cannot co-select with this one. "
            "Used by Layer 5 conflict detection. When both match a task, "
            "Layer 5 drops the lower-priority contract."
        ),
    )
    revisit_when: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form list of triggers that should make a maintainer "
            "review this contract. No runtime effect — purely "
            "documentation."
        ),
    )
    criticality: str = Field(
        default="required",
        description=(
            "Whether downstream behavior DEPENDS on this contract being "
            "selected (required) or merely benefits from it (optional). "
            "Required contracts MUST be selected deterministically — they "
            "cannot use selection_mode=llm_routed. Optional contracts can "
            "use any selection mode including the LLM router. Default is "
            "'required' so legacy + new contracts default to the safe "
            "side; flip to 'optional' explicitly when adding LLM-routable "
            "contracts."
        ),
    )

    @field_validator("category")
    @classmethod
    def _validate_category(cls, v: str) -> str:
        if v not in ALLOWED_CATEGORIES:
            raise ValueError(
                f"category={v!r} not in {sorted(ALLOWED_CATEGORIES)}. "
                "Add the new category to ALLOWED_CATEGORIES if you want "
                "to introduce it."
            )
        return v

    @field_validator("selection_mode")
    @classmethod
    def _validate_selection_mode(cls, v: str) -> str:
        if v not in ALLOWED_SELECTION_MODES:
            raise ValueError(
                f"selection_mode={v!r} not in {sorted(ALLOWED_SELECTION_MODES)}"
            )
        return v

    @field_validator("applies_to_agents")
    @classmethod
    def _validate_agents(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("applies_to_agents must be non-empty")
        unknown = set(v) - ALLOWED_AGENTS
        if unknown:
            raise ValueError(
                f"applies_to_agents contains unknown agents: {sorted(unknown)}. "
                f"Allowed: {sorted(ALLOWED_AGENTS)}"
            )
        return v

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v:
            raise ValueError("id must be non-empty")
        # Convention: lowercase + alphanumerics + underscores only.
        # Catches accidental whitespace, non-ASCII, etc. at load time.
        if not all(c.islower() or c.isdigit() or c == "_" for c in v):
            raise ValueError(
                f"id={v!r} must be lowercase, alphanumeric, "
                "underscores only (snake_case)"
            )
        return v

    @field_validator("criticality")
    @classmethod
    def _validate_criticality(cls, v: str) -> str:
        if v not in ALLOWED_CRITICALITIES:
            raise ValueError(
                f"criticality={v!r} must be one of "
                f"{sorted(ALLOWED_CRITICALITIES)}"
            )
        return v

    @model_validator(mode="after")
    def _validate_combinations(self) -> "ContractMetadata":
        """Cross-field invariants."""
        # selection_mode=deterministic with empty selectors is a bug —
        # the contract would never select.
        if self.selection_mode == "deterministic" and not self.selectors_have_predicate():
            raise ValueError(
                f"Contract {self.id!r} has selection_mode=deterministic "
                "but no selectors declared. Either:\n"
                "  - Set selection_mode=always_on if it should always load.\n"
                "  - Set selection_mode=llm_routed if the LLM should decide.\n"
                "  - Add at least one trigger_types / trigger_platforms / "
                "trigger_candidate_metadata predicate."
            )
        # The required+llm_routed validator from Round 4 is intentionally
        # vestigial after Round 5: ALLOWED_SELECTION_MODES no longer
        # contains "llm_routed", so the field validator rejects it at
        # parse time before this combination check ever fires. The
        # criticality field is preserved for author-intent documentation
        # — required contracts must have a CoverageRequirement entry,
        # optional ones don't (when optional contracts return).
        return self

    def selectors_have_predicate(self) -> bool:
        """True if at least one selection dimension is declared."""
        return bool(
            self.selectors.trigger_types
            or self.selectors.trigger_platforms
            or self.selectors.trigger_candidate_metadata
        )


__all__ = [
    "ALLOWED_AGENTS",
    "ALLOWED_CATEGORIES",
    "ALLOWED_CRITICALITIES",
    "ALLOWED_SELECTION_MODES",
    "ContractMetadata",
    "SelectorMetadata",
]
