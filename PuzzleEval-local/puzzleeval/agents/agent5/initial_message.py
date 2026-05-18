"""Initial-message formatters for the Agent 5 builder.

Owns the family of formatter functions that build sections of the
initial user message Claude sees when the build loop starts. Each
formatter takes structured input (a candidate, a sandbox dir, and
orchestrator-staged artifacts) and returns a markdown-formatted string.

Phase 3.4 of the architecture cleanup — extracted from
``puzzleeval/agents/implement_test_env.py``. Migrated incrementally
in three sub-PRs (3.4.a, 3.4.b, 3.4.c) to keep blast radius small per
Codex's "reduce blast radius" guidance.

Sub-PR boundaries:
  * 3.4.a (this commit) — trivial wrappers: with_shared_preamble,
    with_builder_appendix, usefulness_signal.
  * 3.4.b (next) — schema-coupled: format_modality_context_for_builder,
    format_atlas_context_for_builder.
  * 3.4.c (last) — filesystem-bound: format_sandbox_contents_block,
    format_prefetched_docs_block.

The public shim names (``_with_shared_preamble``, ``_with_builder_appendix``,
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
    evidence sequence.
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
# different questions. Agent-5-owned research_synthesis and
# implementation_plan answer "how to build"; this signal answers "what
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

    Architecture note: Agent 4 now verifies the docs entrypoint and
    lightweight metadata. It does NOT produce an authoritative atlas JSON.
    Agent 5 owns research strategy, research_synthesis.json, and
    implementation_plan.json.

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
                        "live_test.py"):
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
        "or write_file for `_agent_state/research_plan.json`). NOT "
        "`os.listdir` / `ls` / `dir`. That inventory is already above."
    )
    return "\n".join(lines)


def format_prefetched_docs_block(sandbox_dir: Path | None) -> str:
    """Render a ranked inventory of Agent 4's prefetched docs.

    Files are ordered by ``usefulness_signal`` so the builder sees the
    code-heavy / endpoint-heavy pages first when scanning the list. The
    signal is purely RANKING — there are no gates, no tier instructions,
    no "skip to STEP X" branches. docs_entrypoint/research_handoff provide
    routing; this block is background reading material.

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
            "during initial inventory to ground docs_entrypoint; during planned "
            "research as targeted background for the specific gap you're "
            "researching. The docs-entrypoint artifact is the authorization "
            "source; this block is background material. Do not fetch discovery "
            "or source URLs when Agent 4 already gave a verified docs URL or "
            "prefetched mirror; use older source URLs only when tied to a "
            "named unresolved question."
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


# ---------------------------------------------------------------------------
# Autonomy artifacts block (PR 1 — Goal/Planning/State/Reflection)
# ---------------------------------------------------------------------------
# When ``GATE_AUTONOMY_ARTIFACTS_ENABLED`` is on, the orchestrator stages
# ``_agent_state/`` with system-generated objective.md + runtime_state.json
# before the build loop starts. This formatter renders the section of the
# initial user message that teaches the agent the file roles + access
# discipline. Returns empty string when the directory is absent.


def format_research_handoff_block(sandbox_dir: Path | None) -> str:
    """Render the compact Agent 4 -> Agent 5 research handoff."""

    if sandbox_dir is None:
        return ""
    try:
        from puzzleeval.research_handoff import read_research_handoff

        handoff = read_research_handoff(sandbox_dir)
    except Exception:  # noqa: BLE001 - initial message should be robust
        handoff = None
    if not handoff:
        return ""

    def _items(values, limit=5):
        out: list[str] = []
        for item in values or []:
            text = str(item).strip()
            if text:
                out.append(text[:240])
            if len(out) >= limit:
                break
        return out

    lines = [
        "",
        "### Compact Research Handoff (Agent 4 -> Agent 5)",
        "",
        (
            "Start from this handoff before fresh web research. Use fresh "
            "web_search/web_fetch only for unresolved questions or fields "
            "not covered by the handoff or prefetched docs."
        ),
        "",
        f"- Auth / transport: {handoff.get('auth_method', 'unknown')} / {handoff.get('sdk_or_transport', 'unknown')}",
    ]
    docs = _items(handoff.get("canonical_docs_urls"))
    if docs:
        lines.append("- Canonical docs:")
        lines.extend(f"  - {u}" for u in docs)
    discovered = _items(handoff.get("discovered_docs_urls"))
    if discovered:
        lines.append("- Discovered but not verified/current docs (inspect only if canonical docs are insufficient):")
        lines.extend(f"  - {u}" for u in discovered)
    prefetched = _items(handoff.get("prefetched_doc_files"))
    if prefetched:
        lines.append("- Prefetched docs to read first:")
        lines.extend(f"  - `{u}`" for u in prefetched)
    endpoints = _items(handoff.get("primary_endpoints"))
    if endpoints:
        lines.append("- Primary endpoint/API surface hints:")
        lines.extend(f"  - {u}" for u in endpoints)
    notes = _items(handoff.get("streaming_or_session_notes"))
    if notes:
        lines.append("- Streaming/session notes:")
        lines.extend(f"  - {u}" for u in notes)
    unresolved = _items(handoff.get("unresolved_questions"))
    if unresolved:
        lines.append("- Unresolved questions only:")
        lines.extend(f"  - {u}" for u in unresolved)
    blocked = handoff.get("dead_or_blocked_urls") or []
    if blocked:
        lines.append("- Do not retry terminal/blocked URLs unless a later search result justifies it:")
        for item in blocked[:5]:
            if isinstance(item, dict):
                lines.append(f"  - {item.get('url')} ({item.get('reason')})")
    lines.append("- Full JSON: `_agent_state/research_handoff.json`")
    return "\n".join(lines)


def format_research_inputs_block(
    candidate: "ScreenedCandidate",
    sandbox_dir: Path | None,
) -> str:
    """Render the canonical Agent 4 -> Agent 5 research input surface.

    This block consolidates the prompt surfaces:
    - docs_entrypoint.json: Agent 4 docs authorization artifact.
    - research_handoff.json: compact routing/dead-URL index.
    - fetched_docs_*.txt: raw evidence cache.

    The initial message should use this single block so
    Agent 5 sees one source-precedence policy rather than three competing
    prompt sections.
    """

    def _items(values, limit=5):
        out: list[str] = []
        for item in values or []:
            text = str(item).strip()
            if text:
                out.append(text[:240])
            if len(out) >= limit:
                break
        return out

    docs_entrypoint = None
    handoff = None
    if sandbox_dir is not None:
        try:
            from puzzleeval.docs_entrypoint import read_docs_entrypoint

            docs_entrypoint = read_docs_entrypoint(sandbox_dir)
        except Exception:  # noqa: BLE001 - initial message should be robust
            docs_entrypoint = None
        try:
            from puzzleeval.research_handoff import read_research_handoff

            handoff = read_research_handoff(sandbox_dir)
        except Exception:  # noqa: BLE001 - initial message should be robust
            handoff = None

    lines: list[str] = [
        "",
        "---",
        "## Research Inputs (Agent 4 -> Agent 5)",
        "",
        "Use these inputs in this order:",
        "1. `_agent_state/docs_entrypoint.json` for verified docs authorization and official starting URL.",
        "2. `_agent_state/research_handoff.json` for source routing, unresolved questions, and dead/blocked URLs.",
        "3. `fetched_docs_*.txt` for raw evidence behind the docs verdict.",
        "4. Fresh web_search/web_fetch only for named unresolved questions or missing build-critical facts.",
        "",
    ]

    if docs_entrypoint:
        lines.append("### Docs entrypoint")
        verdict = docs_entrypoint.get("docs_verdict", "unknown")
        primary = docs_entrypoint.get("primary_docs_entrypoint", "")
        confidence = docs_entrypoint.get("confidence", "unknown")
        lines.append(f"- Verdict: {verdict} (confidence: {confidence})")
        if primary:
            lines.append(f"- Primary official docs entrypoint: {primary}")
        domain = docs_entrypoint.get("official_domain", "")
        if domain:
            lines.append(f"- Official domain: {domain}")
        alternates = _items(docs_entrypoint.get("alternate_entrypoints"))
        if alternates:
            lines.append("- Alternate entrypoints:")
            lines.extend(f"  - {u}" for u in alternates)
        blocked = docs_entrypoint.get("deprecated_or_blocked_urls") or []
        if blocked:
            lines.append("- Do not retry deprecated/blocked URLs unless fresh evidence changes this:")
            for item in blocked[:5]:
                if isinstance(item, dict):
                    lines.append(f"  - {item.get('url')} ({item.get('reason')})")
        lines.append(
            f"- Auth/access/pricing metadata: {docs_entrypoint.get('auth_method', 'unknown')} / "
            f"{docs_entrypoint.get('api_access_method', 'unknown')} / "
            f"{str(docs_entrypoint.get('pricing_summary', '') or 'unknown')[:180]}"
        )
        capabilities = _items(docs_entrypoint.get("capability_hints"))
        if capabilities:
            lines.append("- Capability hints:")
            lines.extend(f"  - {u}" for u in capabilities)
        lines.append("- Full docs-entrypoint JSON: `_agent_state/docs_entrypoint.json`")
        lines.append("")
    else:
        lines.append(
            "No docs-entrypoint JSON is present. This direct-build path must "
            "verify docs before treating any URL as authoritative."
        )
        lines.append("")

    if handoff:
        lines.append("### Compact handoff index")
        lines.append(
            f"- Auth / transport: {handoff.get('auth_method', 'unknown')} / "
            f"{handoff.get('sdk_or_transport', 'unknown')}"
        )
        docs = _items(handoff.get("canonical_docs_urls"))
        if docs:
            lines.append("- Canonical docs:")
            lines.extend(f"  - {u}" for u in docs)
        discovered = _items(handoff.get("discovered_docs_urls"))
        if discovered:
            lines.append("- Discovered but not verified/current docs:")
            lines.extend(f"  - {u}" for u in discovered)
        prefetched = _items(handoff.get("prefetched_doc_files"))
        if prefetched:
            lines.append("- Prefetched docs to read first:")
            lines.extend(f"  - `{u}`" for u in prefetched)
        endpoints = _items(handoff.get("primary_endpoints"))
        if endpoints:
            lines.append("- Primary endpoint/API surface hints:")
            lines.extend(f"  - {u}" for u in endpoints)
        notes = _items(handoff.get("streaming_or_session_notes"))
        if notes:
            lines.append("- Streaming/session notes:")
            lines.extend(f"  - {u}" for u in notes)
        unresolved = _items(handoff.get("unresolved_questions"))
        if unresolved:
            lines.append("- Research only these unresolved questions unless tests reveal a new gap:")
            lines.extend(f"  - {u}" for u in unresolved)
        blocked = handoff.get("dead_or_blocked_urls") or []
        if blocked:
            lines.append("- Do not retry terminal/blocked URLs unless a later search result justifies it:")
            for item in blocked[:5]:
                if isinstance(item, dict):
                    lines.append(f"  - {item.get('url')} ({item.get('reason')})")
        lines.append("- Full handoff JSON: `_agent_state/research_handoff.json`")
        lines.append("")
    else:
        lines.append(
            "No compact handoff JSON is present. Fall back to prefetched docs "
            "and verified_api_docs_url after verifying them."
        )
        lines.append("")

    # Keep the raw evidence inventory compact.
    if sandbox_dir is not None and sandbox_dir.exists():
        try:
            from puzzleeval.web_doc_cache import count_existing_fetched_docs

            count = count_existing_fetched_docs(sandbox_dir)
        except Exception:  # noqa: BLE001 - diagnostics only
            count = 0
        if count:
            lines.append("### Raw evidence files")
            for i in range(min(count, 8)):
                filename = f"fetched_docs_{i}.txt"
                path = sandbox_dir / filename
                source_url = ""
                size_kb = 0
                try:
                    content = path.read_text(encoding="utf-8")
                    size_kb = max(1, len(content) // 1024)
                    first_line = content.splitlines()[0] if content else ""
                    if first_line.startswith("# Fetched from: "):
                        source_url = first_line[len("# Fetched from: "):]
                    elif first_line.startswith("# Search results"):
                        source_url = "<aggregated search snippets>"
                except OSError:
                    pass
                suffix = f" - {source_url}" if source_url else ""
                lines.append(f"- `{filename}` ({size_kb}KB){suffix}")
            if count > 8:
                lines.append(f"- ... {count - 8} more fetched docs available in the sandbox.")
            lines.append("")

    lines.append(
        "Rule: do not fetch discovery/source URLs when the handoff already "
        "gives canonical docs, unless the URL answers a named unresolved question."
    )
    return "\n".join(lines)


def format_autonomy_artifacts_block(sandbox_dir: Path | None) -> str:
    """Tell the agent about the ``_agent_state/`` directory and its contents.

    Returns empty string when the directory doesn't exist. Otherwise returns a structured block
    naming each file, who owns it, and the read-before-act discipline.

    Pure function — reads the filesystem only. No mutation.
    """
    if sandbox_dir is None:
        return ""
    state_dir = sandbox_dir / "_agent_state"
    if not state_dir.exists():
        return ""

    objective_path = state_dir / "objective.md"
    runtime_state_path = state_dir / "runtime_state.json"
    if not (objective_path.exists() and runtime_state_path.exists()):
        return ""

    return (
        "\n---\n"
        "## Autonomy artifacts — `_agent_state/`\n"
        "\n"
        "Your sandbox now contains a `_agent_state/` directory. The "
        "orchestrator-owned files are load-bearing for the build loop. "
        "**Use the restored snapshot or summarize_build_state() at the top "
        "of significant turns.** Read runtime_state.json/objective.md only "
        "when the summary lacks a detail needed for the next action. Durable "
        "artifacts survive context compaction and replace ad-hoc narrative "
        "tracking.\n"
        "\n"
        "| File | Owner | You can... |\n"
        "|------|-------|------------|\n"
        "| `_agent_state/objective.md` | **orchestrator** (read-only to you) | READ to see DELIVERABLE + SUCCESS CRITERIA + CONSTRAINTS + OUT OF SCOPE. Do not write or patch this file. |\n"
        "| `_agent_state/runtime_state.json` | **orchestrator** (read-only to you) | READ to see authoritative state — `current_phase`, `files_present`, `files_pending`, `smoke_test_status`, `directives_fired`, etc. Updated every turn. Trust this OVER the conversation history. |\n"
        "| `_agent_state/test_case_manifest.json` | **orchestrator** (read-only to you) | READ to see the actual Agent 3 test families and representative cases final evaluation will exercise. Use this to avoid generic provider prep that does not change the harness. |\n"
        "| `_agent_state/build_plan.md` | deprecated context aid | Ignore for action selection. Do not spend turns reading or maintaining it unless explicitly asked. |\n"
        "| `_agent_state/research_plan.json` | YOU | WRITE when several independent research gaps remain. Include focused `research_tasks` with question, where_to_look, evidence_required, and why_needed_for_build. Planned workers use this for parallel research when enabled. |\n"
        "| `_agent_state/research_findings/*.json` | orchestrator/research workers (read-only to you) | READ worker evidence and citations. Do not write these files; synthesize them instead. |\n"
        "| `_agent_state/research_build_brief.json` | orchestrator/research workers (read-only to you) | READ first after planned research as an advisory index. It is keyword-bucketed triage, not authoritative synthesis. |\n"
        "| `_agent_state/research_synthesis.json` | YOU | WRITE after reading research findings. This is the durable provider understanding/doc map: provider_doc_map, chosen_api_surface, credential_model, request_response_contract, input_compatibility, routing_table, working_examples, errors_and_limits, sdk_package, lead-authored build_brief, constraints, cited facts, assumptions, risks, and whether to proceed. |\n"
        "| `_agent_state/implementation_plan.json` | YOU | WRITE when you interpret objective.md into a candidate-specific plan. Include objective_coverage entries that reference stable objective IDs (`OBJ-1`, `OBJ-2`, ...), chosen API surface, credential env vars, interaction pattern, live-test strategy, no blocking open questions, and `ready_to_build=true`. This is the default build gate. |\n"
        "| `_agent_state/representative_probe_evidence.json` | orchestrator gate | READ if completion fails. It records representative test cases run through the production evaluator/plugin path; debug from its evidence instead of replacing it with a toy live test. |\n"
        "| `_agent_state/code_diagnostics.json` | orchestrator diagnostics (read-only to you) | READ or summarize when syntax diagnostics are active. It records mechanical Python syntax findings from successful writes/patches. |\n"
        "| `_agent_state/post_compaction_snapshot.json` | orchestrator audit (read-only to you) | Audit trail for the restored compaction packet; not an action source unless debugging compaction itself. |\n"
        "| `_agent_state/business_fixture.json` | orchestrator/context aid | READ for canonical business facts (menu, pricing, hours, service area, policies) when present. Live tests and rubrics should use these same facts. |\n"
        "| `_agent_state/abandon_candidate.json` | YOU | WRITE only for a validated evidence-based early exit when more patching is the wrong next action. Cite docs/provider/failure evidence. |\n"
        "| `_agent_state/agent_observations.json` | optional diagnostic scratch | Do not create this as a ritual. It is never authoritative; `runtime_state.json` is. |\n"
        "| `_agent_state/reflection_phase_3.md` | YOU | WRITE before HARNESS_COMPLETE (orchestrator will direct you). Must cite specific evidence (file:line, test output, forensics events) — not self-attestation. |\n"
        "\n"
        "**Discipline:**\n"
        "- Turn opening: use `summarize_build_state()` or the restored "
        "compaction packet to ground your mental model. Read runtime_state.json "
        "only when you need fields absent from the summary.\n"
        "- Before any major decision: ensure `_agent_state/objective.md` SUCCESS "
        "CRITERIA are represented in context. Read the file when the summary "
        "does not include the needed criterion detail.\n"
        "- Before delegating several initial research gaps: write `_agent_state/research_plan.json`; "
        "broad unplanned ask_research is blocked while planned research workers are enabled, "
        "but one concrete FIELD NEEDED/WHY debug gap is allowed and recorded durably.\n"
        "- Debugging: use `summarize_build_state()`, `summarize_forensics()`, "
        "`read_forensics(last_n)`, and `read_file_range(...)` before writing "
        "custom diagnostic scripts.\n"
        "- Attempts to write `_agent_state/objective.md` or "
        "`_agent_state/runtime_state.json` will be REJECTED by the tool gate. "
        "These are orchestrator-owned. Write to your own files instead.\n"
    )


__all__ = [
    "USEFULNESS_PATTERNS",
    "format_atlas_context_for_builder",
    "format_autonomy_artifacts_block",
    "format_modality_context_for_builder",
    "format_prefetched_docs_block",
    "format_research_inputs_block",
    "format_research_handoff_block",
    "format_sandbox_contents_block",
    "usefulness_signal",
    "with_builder_appendix",
    "with_shared_preamble",
]
