"""System-generated objective.md for Agent 5 builds.

The orchestrator (Python — NOT an LLM) composes a structured contract
from upstream agent outputs and writes it to ``_agent_state/objective.md``
in the candidate's sandbox. Same inputs always produce the same objective
— deterministic, no model variance, no LLM cost.

Agent 5 reads ``objective.md`` at the start of every turn (taught via the
"Autonomy artifacts" section of the builder system prompt). The file is
system-defined and write-protected. Optional agent-authored notes can live
in ``agent_observations.json`` when useful, but that file is diagnostic
scratch and never authoritative.

This is the GOAL pillar of the Goal/Planning/State/Reflection autonomy
architecture (PR 1 of the four-pillar plan, post-Codex revision). The
contract pattern was Codex's load-bearing critique: orchestrator-owned
truth, not agent-synthesized guess.

Inputs are read-only references; this module mutates nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from puzzleeval.schemas import (
        Agent5Input,
        ScreenedCandidate,
    )


# The 6 adversarial probe categories the harness will face after
# HARNESS_COMPLETE. Surfacing them in objective.md SUCCESS CRITERIA makes
# them load-bearing during the build, not just post-hoc grading.
ADVERSARIAL_PROBES = (
    ("empty_input", "returns success=False with error message; does not hang"),
    ("max_input", "handles oversized input within timeout; no crash"),
    ("malformed_input", "returns success=False with error; does not crash"),
    ("idempotency", "repeat calls don't corrupt state or duplicate side effects"),
    ("concurrency", "parallel calls don't race; no shared mutable state without sync"),
    ("repeat_call", "5 sequential calls produce consistent results"),
)


# Standard out-of-scope items — keep harnesses thin and single-purpose.
# Agent 5 is building a TEST HARNESS, not a production wrapper.
OUT_OF_SCOPE = (
    (
        "State leakage across independent test cases; conversation/session-local "
        "state is allowed only when the evaluator supplies continuity context and "
        "it is observable or cleaned up"
    ),
    "Retry logic at the harness level (the test runner controls retries)",
    "Caching of API responses",
    "Anything beyond the standardized 7-key return contract",
)


@dataclass(frozen=True)
class ObjectiveSourceAudit:
    ok: bool
    issues: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": self.issues}


def _business_fixture_for_objective(input_data: Any) -> dict[str, Any] | None:
    try:
        from puzzleeval.agents.agent5.business_fixture import synthesize_business_fixture

        return synthesize_business_fixture(input_data)
    except Exception:  # noqa: BLE001 - fixture context is advisory
        return None


def synthesize_objective(
    candidate: "ScreenedCandidate",
    input_data: "Agent5Input",
    modality_playbook_ids: list[str],
    effective_max_turns: int,
    effective_max_budget_usd: float,
    platform: str,
) -> str:
    """Build the markdown content for ``_agent_state/objective.md``.

    Pure function. Same inputs → same output. No LLM call.

    Args:
        candidate: The ScreenedCandidate this build targets. Provides
            name, provider, claimed_capabilities, relevant_subtasks,
            verified_api_docs_url, auth_method.
        input_data: The full Agent5Input. Provides user_understanding
            (summary, sub_tasks, domain, constraints) and test_cases
            (count, modality types).
        modality_playbook_ids: The playbook IDs composed for this build
            (e.g., ["voice", "streaming_response", "live_test_voice"]).
            Empty list when no modality contracts apply.
        effective_max_turns: The actual turn budget for this build
            (after voice-modality bump if applicable).
        effective_max_budget_usd: The actual dollar budget.
        platform: One of "windows", "linux", "macos".

    Returns:
        The markdown content as a string. Caller writes it to
        ``sandbox_dir / "_agent_state" / "objective.md"``.
    """
    user = input_data.user_understanding
    test_cases = input_data.test_cases.test_cases
    test_count = len(test_cases)

    # ── DELIVERABLE: one sentence + relevant sub-tasks ──────────────
    relevant = candidate.relevant_subtasks or [
        st.description for st in user.sub_tasks
    ]
    relevant_lines = "\n".join(f"  - {st}" for st in relevant)

    deliverable = (
        f"Build a Python harness that exposes `run(input_data: dict) -> dict` "
        f"for **{candidate.name}** ({candidate.provider}), in service of:\n\n"
        f"> {user.summary}\n\n"
        f"The harness must observably support these sub-tasks this candidate covers:\n"
        f"{relevant_lines}"
    )

    # ── SUCCESS CRITERIA — testable list ────────────────────────────
    sc_lines: list[str] = [
        "- [ ] harness.py exposes the required run(input_data) contract and imports cleanly",
        "- [ ] live_test.py passes against the production payload shape",
        "- [ ] representative_probe_evidence.json passes or records a genuine external provider block",
        f"- [ ] All {test_count} test cases from agent_3_test_cases.json produce success=True with correct output shape",
        "- [ ] Survives the 6-probe adversarial battery (run AFTER HARNESS_COMPLETE):",
    ]
    for probe_name, probe_desc in ADVERSARIAL_PROBES:
        sc_lines.append(f"  - **{probe_name}**: {probe_desc}")
    sc_lines.extend([
        "- [ ] Forensics coverage (AD-011):",
        "  - imports `_forensics` first (before HTTP/WS/SDK libs)",
        "  - SDK calls (openai/anthropic/grpc/elevenlabs/deepgram/etc.) wrapped in `with traced_op(...)`",
        "  - streaming/voice harnesses log session + stream lifecycle markers",
        "- [ ] Code quality:",
        "  - clear separation: auth / transport / business logic",
        "  - no swallowed exceptions (errors surface with context)",
        "  - resources cleaned up on every return path (success AND error)",
        "  - no shared mutable state across run() calls without explicit synchronization",
        "- [ ] User-fit: the harness OBSERVABLY solves the sub-tasks listed in DELIVERABLE",
        "  (not just \"the API call succeeded\" — the harness's output enables comparison",
        "  on the dimensions the user cares about for THIS candidate)",
    ])
    success_criteria = "\n".join(sc_lines)

    # ── CONSTRAINTS — budget + modality + platform ──────────────────
    constraint_lines: list[str] = [
        f"- Budget: max **{effective_max_turns} turns**, max **${effective_max_budget_usd:.2f}**",
        f"- Platform: **{platform}** (see capability_playbooks/platform_{platform}.md)",
        "- Observability contract: AD-011 forensics layer (auto-staged at sandbox creation)",
    ]
    if modality_playbook_ids:
        constraint_lines.append(
            "- Modality contracts (composed into your system prompt): "
            + ", ".join(f"`{pid}`" for pid in modality_playbook_ids)
        )
    if candidate.auth_method:
        constraint_lines.append(
            f"- Authentication: **{candidate.auth_method}** "
            f"(verified during Agent 4 screening)"
        )
    if candidate.verified_api_docs_url:
        constraint_lines.append(
            f"- Verified API docs URL: {candidate.verified_api_docs_url}"
        )
    business_fixture = _business_fixture_for_objective(input_data)
    if business_fixture:
        if business_fixture.get("synthetic"):
            constraint_lines.append(
                "- Business fixture: synthetic gap marker; do not invent exact "
                "prices, hours, menus, or policies unless later evidence supplies them"
            )
        else:
            facts = [
                str(item).strip()
                for item in (business_fixture.get("canonical_facts") or [])
                if str(item).strip()
            ][:3]
            if facts:
                constraint_lines.append(
                    "- Business fixture: reuse these canonical facts in live tests "
                    "and rubrics: " + " | ".join(facts)
                )
    constraints = "\n".join(constraint_lines)

    # ── OUT OF SCOPE — anti-scope-creep ─────────────────────────────
    out_of_scope = "\n".join(f"- {item}" for item in OUT_OF_SCOPE)

    # ── Final assembly ──────────────────────────────────────────────
    # The full objective is system-defined and write-protected. If Agent 5
    # needs optional notes, it can use _agent_state/agent_observations.json.
    return f"""# Objective for {candidate.name}

> System-generated by `puzzleeval.agents.agent5.objective_synthesis` at sandbox
> creation. This file is write-protected. Agent 5 cannot modify it. Optional
> candidate-specific notes may go in `_agent_state/agent_observations.json`
> only when useful; do not create that file as a ritual.

## DELIVERABLE

{deliverable}

## SUCCESS CRITERIA (system-defined; do not modify)

{success_criteria}

## CONSTRAINTS (system-defined)

{constraints}

## OUT OF SCOPE (system-defined - anti-scope-creep)

{out_of_scope}

## CANDIDATE NOTES (read-only; optional diagnostic notes)

<!--
This objective is fully orchestrator-owned. Agent 5 should write
candidate-specific assumptions and decisions only when useful to:

  _agent_state/agent_observations.json
-->
"""


def expected_section_headers() -> list[str]:
    """Return the section headers that must be present in objective.md.

    Used by tests to assert the orchestrator wrote a well-formed file
    and by the write-protection check (any write attempt that would
    delete one of these sections is rejected).
    """
    return [
        "## DELIVERABLE",
        "## SUCCESS CRITERIA (system-defined; do not modify)",
        "## CONSTRAINTS (system-defined)",
        "## OUT OF SCOPE (system-defined - anti-scope-creep)",
        "## CANDIDATE NOTES (read-only; optional diagnostic notes)",
    ]


def audit_objective_source_consistency(
    objective_md: str,
    *,
    candidate: "ScreenedCandidate",
    input_data: "Agent5Input",
    modality_playbook_ids: list[str],
    platform: str,
) -> ObjectiveSourceAudit:
    """Deterministically verify objective.md still reflects upstream facts."""

    issues: list[str] = []
    text = objective_md or ""
    user = input_data.user_understanding
    test_cases = input_data.test_cases.test_cases

    for header in expected_section_headers():
        if header not in text:
            issues.append(f"objective.md missing section header: {header}")
    for label, value in (
        ("candidate name", candidate.name),
        ("candidate provider", candidate.provider),
        ("user summary", user.summary),
        ("platform", platform),
    ):
        if value and str(value) not in text:
            issues.append(f"objective.md missing {label}: {value}")
    expected_count = f"All {len(test_cases)} test cases"
    if expected_count not in text:
        issues.append(f"objective.md missing test-case count phrase: {expected_count}")
    for subtask in candidate.relevant_subtasks or [st.description for st in user.sub_tasks]:
        if subtask and str(subtask) not in text:
            issues.append(f"objective.md missing covered subtask: {subtask}")
    for playbook_id in modality_playbook_ids:
        if playbook_id and playbook_id not in text:
            issues.append(f"objective.md missing modality playbook: {playbook_id}")
    if candidate.auth_method and candidate.auth_method not in text:
        issues.append(f"objective.md missing auth method: {candidate.auth_method}")
    if candidate.verified_api_docs_url and candidate.verified_api_docs_url not in text:
        issues.append(
            f"objective.md missing verified docs URL: {candidate.verified_api_docs_url}"
        )

    business_fixture = _business_fixture_for_objective(input_data)
    if business_fixture:
        if "Business fixture:" not in text:
            issues.append("objective.md missing business fixture constraint")
        if business_fixture.get("synthetic"):
            if "synthetic gap marker" not in text:
                issues.append("objective.md missing synthetic business fixture warning")
        else:
            for fact in (business_fixture.get("canonical_facts") or [])[:3]:
                fact_text = str(fact).strip()
                if fact_text and fact_text not in text:
                    issues.append(f"objective.md missing business fixture fact: {fact_text}")

    return ObjectiveSourceAudit(ok=not issues, issues=issues)


__all__ = [
    "ADVERSARIAL_PROBES",
    "ObjectiveSourceAudit",
    "OUT_OF_SCOPE",
    "audit_objective_source_consistency",
    "synthesize_objective",
    "expected_section_headers",
]
