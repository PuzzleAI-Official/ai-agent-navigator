"""Initial-message formatters for the Agent 5 builder.

Owns the family of formatter functions that build sections of the
initial user message Claude sees when the build loop starts. Each
formatter takes structured input (a candidate, a sandbox dir, a
checklist) and returns a markdown-formatted string.

Phase 3.4 of the architecture cleanup — extracted from
``puzzleeval/agents/implement_test_env.py``. Migrated incrementally
in three sub-PRs (3.4.a, 3.4.b, 3.4.c) to keep blast radius small per
Codex's "reduce blast radius" guidance.

Sub-PR boundaries:
  * 3.4.a (this commit) — trivial wrappers: with_shared_preamble,
    with_builder_appendix, usefulness_signal.
  * 3.4.b (next) — schema-coupled: format_modality_context_for_builder,
    format_atlas_context_for_builder, format_checklist_context_for_builder.
  * 3.4.c (last) — filesystem-bound: format_sandbox_contents_block,
    format_prefetched_docs_block.

The legacy names (``_with_shared_preamble``, ``_with_builder_appendix``,
``_usefulness_signal``, etc.) are preserved as one-line shims in
``implement_test_env.py`` for back-compat with source-grep tests +
external callers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from puzzleeval.schemas import Agent5Input, ScreenedCandidate, TestCase


# ---------------------------------------------------------------------------
# Universal preamble + appendix wrappers
# ---------------------------------------------------------------------------

def with_shared_preamble(prompt: str) -> str:
    """Wire the cross-cutting agent preamble onto a system prompt.

    The preamble (parallel tool use, no narration, reason about errors,
    verify against source, commit and course-correct) is shared across
    every multi-turn agent. Defined as a thin wrapper around
    ``puzzleeval.agent_preamble.with_preamble`` to centralize the
    integration point.
    """
    from puzzleeval.agent_preamble import with_preamble
    return with_preamble(prompt)


def with_builder_appendix(prompt: str) -> str:
    """Append the API-pattern catalog and live-test battery to the
    builder system prompt.

    The two appendices give the builder explicit knowledge of common
    patterns (so it doesn't rediscover REST + Bearer / multipart /
    async polling on every harness) and a clear pre-HARNESS_COMPLETE
    checklist.
    """
    from puzzleeval.api_patterns import (
        API_PATTERNS_CATALOG,
        LIVE_TEST_BATTERY_PROMPT,
    )
    return prompt + "\n\n---\n" + API_PATTERNS_CATALOG + "\n\n---\n" + LIVE_TEST_BATTERY_PROMPT


# ---------------------------------------------------------------------------
# Usefulness signal — soft ranking for prefetched docs
# ---------------------------------------------------------------------------
# Patterns rank a markdown doc by structural richness. Higher score =
# more useful as a starting read for a harness builder.
#
# This replaces the prior density-tier system which conflated "page has
# structural richness" with "page tells you how to BUILD" — two
# different questions. The BuildReadinessChecklist (NEW-AK) answers
# "how to build" via per-field source URLs; this signal answers "what
# to read first as background." Pure ranking, no thresholds or gates.
# ---------------------------------------------------------------------------
USEFULNESS_PATTERNS = (
    ("code_fences", lambda c: c.count("```") * 3),
    ("endpoints", lambda c: len(re.findall(
        r"^\s*(POST|GET|PUT|DELETE|PATCH)\s+/", c, re.MULTILINE)) * 3),
    ("websocket", lambda c: (
        c.lower().count("wss://") + c.lower().count("websocket")
    ) * 2),
    ("auth_examples", lambda c: (
        c.count("Authorization:") + c.count("Bearer ")
        + c.count("X-API-Key:") + c.lower().count("xi-api-key")
    ) * 2),
    ("substantive", lambda c: 1 if len(c) > 5000 else 0),
    ("nav_penalty", lambda c: -(c.count("](https://") // 10)),
)


def usefulness_signal(content: str) -> int:
    """Soft ordering signal for prefetched docs.

    Higher score = more useful as a starting read for a harness builder.
    Pure ranking signal — no thresholds, no gates, no instructional
    branches downstream.
    """
    if not content:
        return 0
    return sum(scorer(content) for _name, scorer in USEFULNESS_PATTERNS)


# ---------------------------------------------------------------------------
# Schema-coupled formatters (Phase 3.4.b)
# ---------------------------------------------------------------------------

def format_modality_context_for_builder(input_data: "Agent5Input") -> str:
    """Tell the builder which tool plugins will evaluate its harness output.

    When the test cases declare audio_content / code / conversation /
    media_url modalities, the modality detector identifies the plugin
    that will run during evaluation. Naming the plugin in the builder
    prompt makes the builder ACT on the contract — e.g. "your harness
    must return an audio URL or path the transcription plugin can
    download" instead of guessing the response shape.
    """
    try:
        from puzzleeval.modality import detect_for_test_case
        from puzzleeval.tool_plugins import list_plugins
    except Exception:  # noqa: BLE001 — defensive: don't break prompt rendering
        return ""

    pairs: set[tuple[str, str]] = set()
    for tc in input_data.test_cases.test_cases:
        pairs.add((tc.input_type, tc.output_type))
    if not pairs:
        return ""

    available_plugins = ", ".join(p.name for p in list_plugins())
    lines: list[str] = ["", "## Modality plugins active for this run", ""]
    lines.append(f"Available plugins in registry: {available_plugins}.")
    for input_type, output_type in sorted(pairs):
        reqs = detect_for_test_case(input_type=input_type, output_type=output_type)
        synth_names = [p.name for p in reqs.input_synthesizers]
        eval_names = [p.name for p in reqs.output_evaluators]
        lines.append(
            f"- ({input_type} → {output_type}): "
            f"input_synthesizers={synth_names or 'none'}, "
            f"output_evaluators={eval_names or 'none (LLM judge fallback)'}"
        )
        if reqs.unavailable:
            for plugin_name, reason in reqs.unavailable:
                lines.append(f"    UNAVAILABLE — {plugin_name}: {reason}")
    lines.append("")
    lines.append(
        "If your harness returns audio (path or URL), the transcription "
        "plugin will STT it and compare to expected text. If it returns "
        "code, the code_execution plugin will run it. If it returns an "
        "image URL, the vision plugin will judge it. Match your response "
        "shape to the plugin's expected input — that's the contract."
    )
    return "\n".join(lines)


def format_atlas_context_for_builder(candidate: "ScreenedCandidate") -> str:
    """Return structured hints from ScreenedCandidate's enrichment fields.

    Architecture note: Agent 4 now only does a shallow verify (exists/
    blocked). It does NOT produce an atlas JSON. Agent 5 does its own
    Phase-1 research via web_search + web_fetch and writes its own
    api_spec.txt into the sandbox.

    This helper still surfaces any simple enrichment fields Agent 4
    happens to populate (sandbox_available, upstream_provider,
    interaction_model flags on the shallow path). Returns an empty
    string when nothing is populated — the common case today.
    """
    sections: list[str] = []

    interaction = getattr(candidate, "interaction_model", None)
    if interaction is not None:
        active_modes = []
        for flag in ("synchronous", "async_polling", "webhook_callback",
                     "sse_streaming", "batch_file", "event_subscription"):
            if getattr(interaction, flag, False):
                active_modes.append(flag)
        if active_modes:
            sections.append(
                "### Interaction model hints (from Agent 4 shallow verify)\n"
                "- Active delivery modes: " + ", ".join(active_modes) + "\n"
                + (
                    "- This API uses async/long-running operations. Your harness "
                    "MUST implement a polling loop or stream consumer; do not "
                    "treat the first response as the final result. Use a longer "
                    "timeout (5-10 min) for end-to-end test calls.\n"
                    if any(m in active_modes for m in ("async_polling", "batch_file", "sse_streaming"))
                    else ""
                )
                + (
                    f"- Notes: {interaction.notes}\n" if getattr(interaction, "notes", "") else ""
                )
            )

    if getattr(candidate, "sandbox_available", False):
        sandbox_url = getattr(candidate, "sandbox_docs_url", None) or "(sandbox docs not captured)"
        sections.append(
            "### Sandbox available\n"
            f"- This provider has a documented sandbox / test mode at: {sandbox_url}\n"
            "- PREFER the sandbox base URL during testing — write_only/destructive "
            "calls will not touch real customer data. The sandbox usually accepts "
            "the same auth keys (or a separate test-key prefix like sk_test_*).\n"
        )

    upstream = getattr(candidate, "upstream_provider", None)
    if upstream:
        sections.append(
            f"### Upstream provider\n- This API wraps `{upstream}`. Rate-limit "
            "headroom is shared with every other candidate that wraps the same "
            "upstream — keep test request volume modest.\n"
        )

    if not sections:
        return ""
    return "\n---\n## ENRICHMENT HINTS FROM AGENT 4\n\n" + "\n".join(sections)


def format_checklist_context_for_builder(candidate: "ScreenedCandidate") -> str:
    """Surface Agent 4's BuildReadinessChecklist to the Phase 1 builder.

    This is the load-bearing handoff: the checklist enumerates what's
    known/inferred/unknown across the ten build-readiness fields plus
    the provider's relevant API surface. Agent 5 reads it during Phase A
    (Inventory), uses Phase B trigger rules to identify which unknowns
    matter for the test case, fills only those (Phase C), then writes
    the spec (Phase D).

    Three render modes:
      - checklist is None        → cached / legacy candidate; tell
                                    builder to do full research from
                                    scratch.
      - populated_by == "system_failure" → Agent 4 sentinel; warn
                                    builder and route to full research,
                                    surface the failure reason.
      - normal                   → render the surface, the selection,
                                    each of the ten fields with status
                                    + value + source, and the
                                    per-test-case trigger rules.
    """
    from puzzleeval.schemas import BUILD_READINESS_FIELDS, NON_NEGOTIABLE_FIELDS

    checklist = getattr(candidate, "checklist", None)

    # Mode 1: no checklist (cached / legacy)
    if checklist is None:
        return (
            "\n---\n"
            "## BUILD-READINESS CHECKLIST (from Agent 4)\n\n"
            "Agent 4 did not produce a checklist for this candidate "
            "(typically a cached candidate from a run predating the "
            "checklist schema). Treat this as full-research mode: do "
            "Phase A by reading the prefetched docs in your sandbox + "
            f"the `verified_api_docs_url` ({candidate.verified_api_docs_url}), "
            "then proceed through Phases B-E as normal."
        )

    # Mode 2: sentinel (system failure)
    if checklist.populated_by == "system_failure":
        reason = ""
        for n in BUILD_READINESS_FIELDS:
            r = getattr(checklist, n).reasoning
            if r:
                reason = r
                break
        reason_line = f"\n  Reason: {reason}" if reason else ""
        return (
            "\n---\n"
            "## BUILD-READINESS CHECKLIST (from Agent 4)\n\n"
            "Agent 4 hit a snag producing the checklist for this "
            "candidate (parser failure, malformed JSON, or schema "
            f"validation error).{reason_line}\n\n"
            "Treat this as full-research mode: do Phase A by reading "
            "the prefetched docs in your sandbox + the "
            f"`verified_api_docs_url` ({candidate.verified_api_docs_url}), "
            "then proceed through Phases B-E. Don't trust any field "
            "in the checklist; all are flagged unknown for diagnostic "
            "reasons."
        )

    # Mode 3: real checklist — render in full
    lines: list[str] = [
        "",
        "---",
        "## BUILD-READINESS CHECKLIST (from Agent 4 — Phase A inventory)",
        "",
    ]

    confirmed = len(checklist.fields_with_status("confirmed"))
    inferred = len(checklist.fields_with_status("inferred"))
    unknown = len(checklist.fields_with_status("unknown"))
    verified_pass = checklist.is_verified_pass()
    lines.append(
        f"Status: {confirmed}/10 confirmed, {inferred} inferred, "
        f"{unknown} unknown. "
        f"Verified Pass: {'YES' if verified_pass else 'NO'} "
        "(four non-negotiables — endpoint_path, auth_method, "
        "request_body_shape, response_body_shape — all confirmed)."
    )
    lines.append("")

    if checklist.provider_surface:
        lines.append("### Provider surface (endpoints Agent 4 considered)")
        for ep in checklist.provider_surface:
            tag = ep.relevance_to_use_case.upper()
            note = f" — {ep.selection_note}" if ep.selection_note else ""
            lines.append(f"  [{tag:<11}] `{ep.name}` — {ep.purpose}{note}")
        lines.append("")
    if checklist.selected_endpoint:
        lines.append(f"Selected endpoint: `{checklist.selected_endpoint}`")
        if checklist.selection_justification:
            lines.append(f"Justification: {checklist.selection_justification}")
        lines.append("")

    lines.append("### Build-readiness fields (10)")
    lines.append("")
    for fname in BUILD_READINESS_FIELDS:
        fs = getattr(checklist, fname)
        non_neg_marker = "★" if fname in NON_NEGOTIABLE_FIELDS else " "
        lines.append(f"{non_neg_marker} {fname}: [{fs.status.upper()}]")
        if fs.value:
            value_preview = fs.value[:200] + ("…" if len(fs.value) > 200 else "")
            lines.append(f"    value: {value_preview}")
        if fs.source_url:
            lines.append(f"    source: {fs.source_url}")
        if fs.reasoning:
            reasoning_preview = fs.reasoning[:200] + ("…" if len(fs.reasoning) > 200 else "")
            lines.append(f"    reasoning: {reasoning_preview}")
    lines.append("")
    lines.append("(★ = non-negotiable for Verified Pass)")
    lines.append("")

    lines.append("### Phase B trigger rules (which fields matter for THIS test case)")
    lines.append("")
    lines.append(
        "The four non-negotiables are ALWAYS required (Agent 4 should have "
        "confirmed them; if any is `unknown`/`inferred`, you fill them in "
        "Phase C). The six conditional fields are required only when the "
        "test case actually exercises them:"
    )
    lines.append(
        "  - error_response_schema, rate_limit_signal: required IF the test "
        "exercises retry / failure paths"
    )
    lines.append(
        "  - auth_refresh: required IF the test session is long-running (>10 min)"
    )
    lines.append(
        "  - async_pattern: required IF the API is async or streaming"
    )
    lines.append(
        "  - content_type_quirks: required IF non-standard content types "
        "(multipart, SSE, binary)"
    )
    lines.append(
        "  - sandbox_availability: required IF the candidate has side_effects "
        "(creates/modifies/deletes records)"
    )
    lines.append("")
    lines.append(
        "Fields irrelevant to your test case stay `unknown` and that's fine "
        "— don't research them out of habit."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Filesystem-bound formatters (Phase 3.4.c)
# ---------------------------------------------------------------------------

def format_sandbox_contents_block(
    sandbox_dir: Path | None,
    staged_test_cases: "list[TestCase] | None",
) -> str:
    """Render a comprehensive listing of the sandbox starting contents.

    Real-run trace 2b2b9d1f (2026-04-22) showed ALL candidate builds
    started turn 0 with `python -c "import os; print(os.listdir('.'))"`
    at ~$0.10/candidate. That probe is wasteful — the initial message
    can tell the builder exactly what's there, so turn 0 should jump
    straight to research or build.

    This block lists EVERY file the builder needs to know about:
      - Prefetched docs from Agent 4 (if the handoff landed)
      - Staged test inputs from Agent 3 (under test_inputs/)
      - Any other starting files
    Plus an explicit directive not to re-probe.

    Returns empty string when sandbox_dir is None (test fixtures /
    mock runs that don't have a real sandbox yet).
    """
    if sandbox_dir is None or not sandbox_dir.exists():
        return ""
    try:
        entries = sorted(sandbox_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return ""
    if not entries:
        return ""

    lines = [
        "",
        "### Sandbox Starting Contents — DO NOT PROBE",
        "",
        "Your sandbox directory is pre-populated with the files listed "
        "below. This list is AUTHORITATIVE — **do not run "
        "`os.listdir('.')`, `ls`, `dir`, or any other directory-"
        "exploration command on turn 0**. Every candidate build on real "
        "run traces wasted ~$0.10 on that probe before jumping to "
        "research; the probe tells you nothing the list below doesn't.",
        "",
        "```",
    ]

    fetched_docs = []
    test_files = []
    other_files = []
    subdirs = []
    for p in entries:
        if p.is_dir():
            subdirs.append(p.name + "/")
        elif p.name.startswith("fetched_docs_") and p.name.endswith(".txt"):
            fetched_docs.append(p.name)
        elif p.name in ("harness.py", "requirements.txt", "smoke_test.py",
                        "live_test.py", "api_spec.txt"):
            other_files.append(p.name + "  [already exists — patch_file, don't rewrite]")
        else:
            other_files.append(p.name)

    if fetched_docs:
        lines.append("Pre-fetched API documentation (from Agent 4 — read these first):")
        for fn in fetched_docs:
            lines.append(f"  {fn}")
    if subdirs:
        lines.append("Subdirectories:")
        for d in subdirs:
            lines.append(f"  {d}")
    if other_files:
        lines.append("Other files:")
        for fn in other_files:
            lines.append(f"  {fn}")

    if staged_test_cases:
        test_file_paths = []
        for tc in staged_test_cases:
            tfp = getattr(tc, "test_file_path", None)
            if tfp:
                test_file_paths.append(str(tfp))
        if test_file_paths:
            lines.append("")
            lines.append("Test input files (staged for harness.run() calls):")
            for tfp in test_file_paths:
                lines.append(f"  {tfp}")

    lines.append("```")
    lines.append("")
    lines.append(
        "**Turn 0 rule**: your very first tool call should be productive "
        "work (web_fetch for research, read_file for a prefetched doc, "
        "or write_file to start the spec). NOT `os.listdir` / `ls` / "
        "`dir`. That inventory is already above."
    )
    return "\n".join(lines)


def format_prefetched_docs_block(sandbox_dir: Path | None) -> str:
    """Render a ranked inventory of Agent 4's prefetched docs.

    Files are ordered by ``usefulness_signal`` so the builder sees the
    code-heavy / endpoint-heavy pages first when scanning the list. The
    signal is purely RANKING — there are no gates, no tier instructions,
    no "skip to STEP X" branches. The checklist (rendered separately
    above this block) is the load-bearing handoff; this block is
    background reading material.

    Returns empty string when sandbox_dir is None / missing / empty —
    Agent 5 then falls back to web_fetch / web_search.
    """
    if sandbox_dir is None or not sandbox_dir.exists():
        return ""

    from puzzleeval.web_doc_cache import count_existing_fetched_docs

    count = count_existing_fetched_docs(sandbox_dir)
    if count == 0:
        return ""

    docs: list[tuple[int, str, str, int]] = []
    for i in range(count):
        filename = f"fetched_docs_{i}.txt"
        filepath = sandbox_dir / filename
        try:
            content = filepath.read_text(encoding="utf-8")
        except OSError:
            continue
        source_url = ""
        first_line = content.splitlines()[0] if content else ""
        if first_line.startswith("# Fetched from: "):
            source_url = first_line[len("# Fetched from: "):]
        elif first_line.startswith("# Search results"):
            source_url = "<aggregated search snippets>"
        body = content
        if first_line.startswith("#"):
            body_lines = content.splitlines()
            idx = 0
            while idx < len(body_lines) and body_lines[idx].startswith("#"):
                idx += 1
            if idx < len(body_lines) and body_lines[idx].strip() == "":
                idx += 1
            body = "\n".join(body_lines[idx:])
        signal = usefulness_signal(body)
        docs.append((signal, filename, source_url, len(body)))

    if not docs:
        return ""

    docs.sort(key=lambda d: -d[0])

    lines = [
        "",
        "### Prefetched Documentation Inventory (Agent 4 saved these to your sandbox)",
        "",
        (
            "Below are the files Agent 4 fetched while verifying this "
            "candidate, ranked by a usefulness signal (code fences, HTTP "
            "endpoints, auth headers — pages with these tend to be better "
            "starting reads than nav-only pages). The ranking is a HINT, "
            "not a gate."
        ),
        "",
        (
            "When to read these files (`read_file('fetched_docs_<n>.txt')`): "
            "during Phase A to ground the BuildReadinessChecklist's "
            "`source_url` references; during Phase C as targeted background "
            "for the specific gap you're researching. The checklist (above) "
            "is the load-bearing artifact — this block is background "
            "material."
        ),
        "",
    ]
    for signal, filename, source_url, body_len in docs:
        size_kb = body_len // 1024
        lines.append(
            f"  {filename} — {source_url}  "
            f"(signal {signal}, {size_kb}KB)"
        )
    return "\n".join(lines)


__all__ = [
    "USEFULNESS_PATTERNS",
    "format_atlas_context_for_builder",
    "format_checklist_context_for_builder",
    "format_modality_context_for_builder",
    "format_prefetched_docs_block",
    "format_sandbox_contents_block",
    "usefulness_signal",
    "with_builder_appendix",
    "with_shared_preamble",
]
