"""Render the Agent 5 builder system prompt across representative OS + modality
combinations and emit per-combo + aggregate metrics.

This is the FREE portion of Phase 0 baseline capture (no real-API runs). It
covers:
  - prompt_chars + prompt_tokens (Anthropic count_tokens when available)
  - cross-render common-prefix size — the number of chars/tokens shared by
    the rendered prompts for {Linux+voice, Linux+OCR, Windows+code}, which
    is the merge-gate metric for cacheable prefix.

Outputs:
  - runs/baseline-<date>/prompts/<combo>.txt   (gitignored, the raw render)
  - PuzzleEval-local/baselines/<date>/PROMPT_BASELINE.md  (tracked, sanitized)

Usage:
  python scripts/measure_prompt_baseline.py [--date YYYY-MM-DD]
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

from puzzleeval.agents.agent5.playbooks import compose_capability_playbooks
from puzzleeval.agents.agent5.prompts import (
    load_builder_system_prompt,
    render_builder_prompt,
)


# Representative test-case dicts triggering each modality combo. The router
# in agent5/playbooks.py keys off (input_type, output_type) per test case.
def _tc(input_type: str, output_type: str = "text") -> dict:
    return {"input_type": input_type, "output_type": output_type}


@dataclass(frozen=True)
class Combo:
    name: str
    platform: str  # "win32" | "linux" | "darwin"
    test_cases: tuple[dict, ...]
    description: str


COMBOS: tuple[Combo, ...] = (
    # The 3 merge-gate combos (used for common-prefix calc)
    Combo(
        name="linux_voice",
        platform="linux",
        test_cases=(_tc("voice_conversation", "voice_conversation"),),
        description="Linux + voice_conversation — heavy modality (loads voice + streaming + live_test_voice)",
    ),
    Combo(
        name="linux_ocr",
        platform="linux",
        test_cases=(_tc("document_content", "structured_data"),),
        description="Linux + OCR/document — no modality playbooks",
    ),
    Combo(
        name="windows_code",
        platform="win32",
        test_cases=(_tc("text", "code"),),
        description="Windows + code-gen — streaming only + Windows playbook",
    ),
    # Coverage extras
    Combo(
        name="linux_chatbot_conversation",
        platform="linux",
        test_cases=(_tc("conversation", "conversation"),),
        description="Linux + plain text conversation (per coverage.py: should load streaming only; today over-loads voice + live_test_voice — Phase A fix)",
    ),
    Combo(
        name="linux_audio_content",
        platform="linux",
        test_cases=(_tc("audio_content", "text"),),
        description="Linux + audio_content (OCR-of-audio-style)",
    ),
    Combo(
        name="windows_voice",
        platform="win32",
        test_cases=(_tc("voice_conversation", "voice_conversation"),),
        description="Windows + voice (combines all three voice playbooks with Windows playbook)",
    ),
    Combo(
        name="macos_ocr",
        platform="darwin",
        test_cases=(_tc("document_content", "structured_data"),),
        description="macOS + OCR — minimal playbook surface",
    ),
)

# The subset whose common prefix is the merge-gate metric.
COMMON_PREFIX_COMBOS = ("linux_voice", "linux_ocr", "windows_code")


def render_for_combo(combo: Combo) -> str:
    """Render the builder system prompt for a given combo."""
    template = load_builder_system_prompt()
    contract_block = compose_capability_playbooks(list(combo.test_cases))
    # Real builds also prepend platform playbook content; for baseline we
    # focus on the modality contract block. The platform playbook is loaded
    # by the contract-system selector at render time in production; for an
    # apples-to-apples token measurement we render with just the modality
    # block. (Phase D intent: have one __CONTRACT_BLOCK__ that bundles
    # both — the goal of the cache invariant repair.)
    return render_builder_prompt(
        template,
        platform=combo.platform,
        contract_block=contract_block,
    )


def count_tokens(text: str, *, model: str = "claude-opus-4-5") -> tuple[int, str]:
    """Return (token_count, method). Tries Anthropic SDK count_tokens; falls
    back to a 4-chars-per-token estimate when no API key is available.

    Returns method = "anthropic" or "estimate".
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
            sys.stderr.write(f"[warn] count_tokens via Anthropic failed: {exc}; falling back to estimate\n")
    # Fallback: 4 chars per token is roughly accurate for English prose.
    return max(1, len(text) // 4), "estimate"


def longest_common_prefix(strings: list[str]) -> str:
    """Return the longest common prefix across ``strings``."""
    if not strings:
        return ""
    shortest = min(strings, key=len)
    for i, ch in enumerate(shortest):
        if any(s[i] != ch for s in strings):
            return shortest[:i]
    return shortest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_dt.date.today().isoformat())
    parser.add_argument("--out-tracked", default=str(ROOT / "baselines"))
    parser.add_argument("--out-raw", default=str(ROOT / "runs"))
    args = parser.parse_args()

    tracked_dir = Path(args.out_tracked) / args.date
    raw_dir = Path(args.out_raw) / f"baseline-{args.date}" / "prompts"
    tracked_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Render every combo and capture metrics.
    rendered: dict[str, str] = {}
    rows: list[dict] = []
    for combo in COMBOS:
        text = render_for_combo(combo)
        rendered[combo.name] = text
        (raw_dir / f"{combo.name}.txt").write_text(text, encoding="utf-8")
        token_count, method = count_tokens(text)
        rows.append(
            {
                "combo": combo.name,
                "platform": combo.platform,
                "test_cases": ", ".join(
                    f"{tc['input_type']}->{tc['output_type']}" for tc in combo.test_cases
                ),
                "chars": len(text),
                "tokens": token_count,
                "token_method": method,
                "description": combo.description,
            }
        )

    # Cross-render common prefix on the 3 merge-gate combos.
    prefix_inputs = [rendered[name] for name in COMMON_PREFIX_COMBOS]
    common_prefix = longest_common_prefix(prefix_inputs)
    common_prefix_chars = len(common_prefix)
    common_prefix_tokens, prefix_method = count_tokens(common_prefix) if common_prefix else (0, "n/a")

    # Emit sanitized PROMPT_BASELINE.md (tracked).
    md = []
    md.append(f"# Prompt Baseline — captured {args.date}")
    md.append("")
    md.append("This file captures the FREE portion of the Phase 0 baseline: prompt-token counts and cross-render common-prefix size for the merge-gate combos. The Agent-5-build portion (build_cost, turn_count, scores) is captured separately in the same baselines directory.")
    md.append("")
    md.append("## Per-combo metrics")
    md.append("")
    md.append("| Combo | Platform | Test cases (input → output) | Chars | Tokens | Method |")
    md.append("|---|---|---|---:|---:|:---:|")
    for r in rows:
        md.append(
            f"| `{r['combo']}` | {r['platform']} | {r['test_cases']} | {r['chars']:,} | {r['tokens']:,} | {r['token_method']} |"
        )
    md.append("")
    md.append("Combo descriptions:")
    md.append("")
    for r in rows:
        md.append(f"- `{r['combo']}` — {r['description']}")
    md.append("")
    md.append("## Cross-render common prefix (merge-gate metric)")
    md.append("")
    md.append(
        f"Computed across the 3 combos {COMMON_PREFIX_COMBOS}: "
        f"**{common_prefix_chars:,} chars / {common_prefix_tokens:,} tokens** "
        f"(method = `{prefix_method}`)."
    )
    md.append("")
    md.append("This number measures how much of the rendered prompt is stable across OS + modality variations — directly correlated with Anthropic prompt-cache hit potential. Phase D's cache-invariant repair is expected to grow this number materially.")
    md.append("")
    md.append("## Acceptance targets (post-Phase E)")
    md.append("")
    md.append(f"- `prompt_tokens` should drop **≥30%** on at least 4 of the 5 baseline candidates' renders. Today's per-combo numbers above are the reference.")
    md.append(f"- Common-prefix tokens should rise (or stay flat) — never drop below today's {common_prefix_tokens:,}.")
    md.append("")
    md.append("## Notes")
    md.append("")
    md.append("- Raw rendered prompts are in `runs/baseline-<date>/prompts/` (gitignored — they may contain provider names from playbook content).")
    md.append("- Token counts use Anthropic SDK's free `count_tokens` endpoint when an API key is available, otherwise a 4-chars-per-token estimate (consistent across baseline + post-refactor measurement).")
    md.append("- This file is meant to be re-generated with the same script after Phase E to produce the comparison `POST_REFACTOR.md`.")
    md.append("")

    out_file = tracked_dir / "PROMPT_BASELINE.md"
    out_file.write_text("\n".join(md), encoding="utf-8")
    print(f"[+] PROMPT_BASELINE.md written to {out_file}")
    print(f"[+] Raw renders in {raw_dir}")
    print()
    for r in rows:
        print(
            f"  {r['combo']:<32}  chars={r['chars']:>7,}  tokens={r['tokens']:>6,}  ({r['token_method']})"
        )
    print()
    print(
        f"  COMMON_PREFIX (across {COMMON_PREFIX_COMBOS}): "
        f"chars={common_prefix_chars:,}  tokens={common_prefix_tokens:,}  ({prefix_method})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
