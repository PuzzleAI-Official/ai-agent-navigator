"""Render Agents 1-4 templates + auxiliary system prompts and emit
per-prompt token metrics for Phase 2 baseline measurement.

Mirrors `measure_prompt_baseline.py` (which covers Agent 5) but for the
Phase 2 prompt surface: Agents 1-4 templates + the inline system-prompt
strings inside auxiliary Python files (rubric_judge, user_simulator,
research_subagent, vision_judge, evaluation, agent_preamble).

Outputs:
  - PuzzleEval-local/baselines/<date>/PHASE2_BASELINE.md (tracked, sanitized)

Usage:
  python scripts/measure_phase2_baseline.py [--date YYYY-MM-DD]

This is the FREE portion of Phase 2.0 baseline (no real-API runs). The
quality battery (Agent 1 blueprint quality, Agent 2 scope coverage,
Agent 3 test validity + diversity, Agent 4 verification precision)
lives in `quality_battery.py` and requires real-API calls; that script
is run separately.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Phase 2 prompt surface: 7 agent-template files + 7 auxiliary string
# constants. Each entry is (label, file_path, kind), where kind is
# "template" (raw markdown) or "constant:<name>" (Python constant).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptSurface:
    label: str
    kind: str  # "template" or "constant:<NAME>"
    path: Path
    description: str


PHASE2_SURFACES: tuple[PromptSurface, ...] = (
    # Agent 1-4 templates (raw markdown, no placeholder substitution).
    PromptSurface(
        label="agent1_system_prompt",
        kind="template",
        path=ROOT / "puzzleeval" / "agents" / "agent1" / "templates" / "system_prompt.md",
        description="Agent 1 (User Understanding) system prompt — 327 lines",
    ),
    PromptSurface(
        label="agent2_research_system",
        kind="template",
        path=ROOT / "puzzleeval" / "agents" / "agent2" / "templates" / "research_system.md",
        description="Agent 2 (Research) research_system prompt — 100 lines",
    ),
    PromptSurface(
        label="agent2_structure_system",
        kind="template",
        path=ROOT / "puzzleeval" / "agents" / "agent2" / "templates" / "structure_system.md",
        description="Agent 2 (Research) structure_system prompt — 56 lines",
    ),
    PromptSurface(
        label="agent3_system_prompt",
        kind="template",
        path=ROOT / "puzzleeval" / "agents" / "agent3" / "templates" / "system_prompt.md",
        description="Agent 3 (Synthetic Tests, text mode) system prompt — 523 lines (biggest)",
    ),
    PromptSurface(
        label="agent3f_system_prompt",
        kind="template",
        path=ROOT / "puzzleeval" / "agents" / "agent3f" / "templates" / "system_prompt.md",
        description="Agent 3F (Synthetic Tests, file mode) system prompt — 99 lines",
    ),
    PromptSurface(
        label="agent4_verification_system",
        kind="template",
        path=ROOT / "puzzleeval" / "agents" / "agent4" / "templates" / "verification_system.md",
        description="Agent 4 (Screening) verification_system prompt — 206 lines",
    ),
    PromptSurface(
        label="agent4_structure_system",
        kind="template",
        path=ROOT / "puzzleeval" / "agents" / "agent4" / "templates" / "structure_system.md",
        description="Agent 4 (Screening) structure_system prompt — 56 lines",
    ),
    # Auxiliary system-prompt string constants.
    PromptSurface(
        label="agent_preamble",
        kind="constant:SHARED_AGENT_PREAMBLE",
        path=ROOT / "puzzleeval" / "agent_preamble.py",
        description="Shared cross-cutting preamble injected into every agent",
    ),
    PromptSurface(
        label="rubric_judge_template",
        kind="constant:_JUDGE_SYSTEM_TEMPLATE",
        path=ROOT / "puzzleeval" / "rubric_judge.py",
        description="Rubric judge system prompt (stable + per-test composed)",
    ),
    PromptSurface(
        label="user_simulator_template",
        kind="constant:_SIMULATOR_SYSTEM_TEMPLATE",
        path=ROOT / "puzzleeval" / "user_simulator.py",
        description="User simulator system prompt template",
    ),
    PromptSurface(
        label="research_subagent_system",
        kind="constant:TARGETED_RESEARCH_SYSTEM",
        path=ROOT / "puzzleeval" / "agents" / "agent5" / "research_subagent.py",
        description="Phase-2 ask_research sub-agent system prompt",
    ),
    PromptSurface(
        label="vision_judge_system",
        kind="constant:_SYSTEM_PROMPT",
        path=ROOT / "puzzleeval" / "vision_judge.py",
        description="Vision judge system prompt",
    ),
    PromptSurface(
        label="evaluation_system_prompt",
        kind="constant:EVALUATION_SYSTEM_PROMPT",
        path=ROOT / "puzzleeval" / "agents" / "implement_test_env.py",
        description="Agent 5 LLM judge system prompt (test-result evaluation)",
    ),
)


def load_surface_text(surface: PromptSurface) -> str:
    """Return the prompt text for a surface (template body or constant value)."""
    if surface.kind == "template":
        return surface.path.read_text(encoding="utf-8")
    if surface.kind.startswith("constant:"):
        const_name = surface.kind.split(":", 1)[1]
        # Import the module and read the attribute.
        rel = surface.path.relative_to(ROOT).with_suffix("")
        module_name = ".".join(rel.parts)
        import importlib
        mod = importlib.import_module(module_name)
        return getattr(mod, const_name)
    raise ValueError(f"unknown surface kind: {surface.kind!r}")


def count_tokens(text: str, *, model: str = "claude-opus-4-5") -> tuple[int, str]:
    """Return (token_count, method). Tries Anthropic SDK count_tokens; falls
    back to a 4-chars-per-token estimate when no API key is available.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key and api_key != "dummy" and not api_key.startswith("dummy"):
        try:
            from anthropic import Anthropic

            client = Anthropic(api_key=api_key)
            result = client.messages.count_tokens(
                model=model,
                messages=[{"role": "user", "content": "."}],
                system=text,
            )
            return result.input_tokens, "anthropic"
        except Exception as exc:
            sys.stderr.write(
                f"[warn] count_tokens via Anthropic failed: {exc}; falling back to estimate\n"
            )
    return max(1, len(text) // 4), "estimate"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_dt.date.today().isoformat())
    parser.add_argument("--out-tracked", default=str(ROOT / "baselines"))
    parser.add_argument(
        "--label",
        default="PHASE2_BASELINE",
        help="Output filename label (default PHASE2_BASELINE → PHASE2_BASELINE.md)",
    )
    args = parser.parse_args()

    tracked_dir = Path(args.out_tracked) / args.date
    tracked_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    template_total_chars = 0
    template_total_tokens = 0
    aux_total_chars = 0
    aux_total_tokens = 0

    for surface in PHASE2_SURFACES:
        try:
            text = load_surface_text(surface)
        except Exception as exc:
            sys.stderr.write(f"[error] failed to load {surface.label}: {exc}\n")
            continue
        token_count, method = count_tokens(text)
        rows.append(
            {
                "label": surface.label,
                "kind": "template" if surface.kind == "template" else "auxiliary",
                "chars": len(text),
                "tokens": token_count,
                "token_method": method,
                "description": surface.description,
            }
        )
        if surface.kind == "template":
            template_total_chars += len(text)
            template_total_tokens += token_count
        else:
            aux_total_chars += len(text)
            aux_total_tokens += token_count

    # Emit sanitized PHASE2_BASELINE.md (tracked).
    md = []
    md.append(f"# Phase 2 Prompt Baseline — captured {args.date}")
    md.append("")
    md.append(
        "FREE portion of Phase 2.0 baseline: prompt-token counts for "
        "Agents 1-4 templates + 6 auxiliary system-prompt string constants. "
        "The quality battery (Agent N quality metrics requiring real-API "
        "calls) is captured separately by `quality_battery.py`."
    )
    md.append("")
    md.append("## Agent 1-4 templates (raw markdown)")
    md.append("")
    md.append("| Surface | Chars | Tokens | Method | Description |")
    md.append("|---|---:|---:|:---:|---|")
    for r in rows:
        if r["kind"] == "template":
            md.append(
                f"| `{r['label']}` | {r['chars']:,} | {r['tokens']:,} "
                f"| {r['token_method']} | {r['description']} |"
            )
    md.append("")
    md.append(
        f"**Templates total: {template_total_chars:,} chars / "
        f"{template_total_tokens:,} tokens.**"
    )
    md.append("")
    md.append("## Auxiliary system-prompt string constants")
    md.append("")
    md.append("| Surface | Chars | Tokens | Method | Description |")
    md.append("|---|---:|---:|:---:|---|")
    for r in rows:
        if r["kind"] == "auxiliary":
            md.append(
                f"| `{r['label']}` | {r['chars']:,} | {r['tokens']:,} "
                f"| {r['token_method']} | {r['description']} |"
            )
    md.append("")
    md.append(
        f"**Auxiliary total: {aux_total_chars:,} chars / "
        f"{aux_total_tokens:,} tokens.**"
    )
    md.append("")
    md.append("## Phase 2 acceptance targets")
    md.append("")
    md.append(
        "- `prompt_tokens` should drop **≥20%** on at least **4 of 7** agent "
        "templates after Phase 2D (looser than Phase 1's 30% target — "
        "Agents 1-4 prompts are smaller and less patch-heavy)."
    )
    md.append(
        "- Auxiliary system-prompt string lengths drop ≥10% on average."
    )
    md.append(
        "- Quality battery (Agent N quality metrics) at parity or better — "
        "see `quality_battery.py` output."
    )
    md.append("")
    md.append("## Notes")
    md.append("")
    md.append(
        "- Templates are loaded via `read_text(encoding='utf-8')` from "
        "`puzzleeval/agents/agent<N>/templates/`. No placeholder substitution; "
        "raw template tokens are the measurement (production rendering "
        "for Agents 1-4 uses these strings verbatim — they're not parameterized "
        "the way Agent 5's `__CONTRACT_BLOCK__` is)."
    )
    md.append(
        "- Auxiliary constants are imported live (via `importlib`) from "
        "their source files. The imported value is what gets sent to Claude "
        "as the `system` parameter."
    )
    md.append(
        "- Token counts use Anthropic SDK's free `count_tokens` endpoint "
        "when an API key is available; otherwise a 4-chars-per-token estimate "
        "(consistent across baseline + post-refactor measurement)."
    )
    md.append(
        "- This file is meant to be re-generated with the same script after "
        "each Phase 2 sub-phase to produce a comparison."
    )
    md.append("")

    out_file = tracked_dir / f"{args.label}.md"
    out_file.write_text("\n".join(md), encoding="utf-8")
    print(f"[+] {args.label}.md written to {out_file}")
    print()
    print("Templates:")
    for r in rows:
        if r["kind"] == "template":
            print(
                f"  {r['label']:<35}  chars={r['chars']:>7,}  "
                f"tokens={r['tokens']:>5,}  ({r['token_method']})"
            )
    print(
        f"  {'TOTAL':<35}  chars={template_total_chars:>7,}  "
        f"tokens={template_total_tokens:>5,}"
    )
    print()
    print("Auxiliary:")
    for r in rows:
        if r["kind"] == "auxiliary":
            print(
                f"  {r['label']:<35}  chars={r['chars']:>7,}  "
                f"tokens={r['tokens']:>5,}  ({r['token_method']})"
            )
    print(
        f"  {'TOTAL':<35}  chars={aux_total_chars:>7,}  "
        f"tokens={aux_total_tokens:>5,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
