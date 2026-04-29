"""Phase 7 — reusable per-agent restructure script.

Run with: python scripts/_phase7_restructure.py <agent_name>
where <agent_name> in {agent1, agent2, agent3, agent3f, agent4}.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def restructure_agent(
    legacy_path: str,
    package_name: str,
    public_names: list,
    prompts: list,
    description: str,
):
    """Per-agent Phase 7 restructure (see Phase 7 plan)."""
    src = open(legacy_path, encoding="utf-8").read()
    pkg_dir = Path(f"puzzleeval/agents/{package_name}")
    templates_dir = pkg_dir / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)

    for const_name, template_filename, _header_marker in prompts:
        # Match the inline `NAME = TRIPLE_QUOTE...content...TRIPLE_QUOTE` block.
        # Use a sentinel for the triple quote to avoid escaping headaches.
        TQ = chr(34) * 3
        pattern_str = f"^{const_name} = {TQ}(.*?)\\n{TQ}"
        pattern = re.compile(pattern_str, re.DOTALL | re.MULTILINE)
        m = pattern.search(src)
        assert m, f"{const_name} not found via pattern"
        body = m.group(1)
        full_match = m.group(0)

        md_path = templates_dir / template_filename
        md_path.write_text(body + "\n", encoding="utf-8")
        print(
            f"  [{package_name}] wrote {md_path}: "
            f"{len(body)} chars / {len(body.splitlines())} lines"
        )

        # Build loader replacement (concat strings to avoid quote-escape issues).
        loader_lines = [
            f"@lru_cache(maxsize=1)",
            f"def _load_{const_name.lower()}() -> str:",
            f'    """Load {const_name} from templates/{template_filename} (Phase 7)."""',
            f'    return resources.files("puzzleeval.agents.{package_name}").joinpath(',
            f'        "templates", "{template_filename}",',
            f'    ).read_text(encoding="utf-8")',
            "",
            "",
            f"{const_name} = _load_{const_name.lower()}()",
        ]
        loader = "\n".join(loader_lines)
        src = src.replace(full_match, loader, 1)

    # Add lru_cache + resources imports — insert at the END of the
    # FIRST contiguous import block at module top. Lazy `import X`
    # inside functions later in the file would otherwise be picked
    # as "last import" and break.
    if "from functools import lru_cache" not in src:
        # Find the first `import`/`from` at column 0.
        first_import = re.search(r"^(?:from|import) .+$", src, re.MULTILINE)
        if first_import:
            # Walk forward line by line; stop at first non-import,
            # non-blank, non-comment, non-continuation line. Treat
            # parenthesized multi-line imports as one unit.
            lines = src.splitlines(keepends=True)
            # Find the line index of first_import.
            idx = src[:first_import.start()].count("\n")
            i = idx
            paren_depth = 0
            last_import_line = i
            while i < len(lines):
                ln = lines[i].rstrip("\n")
                paren_depth += ln.count("(") - ln.count(")")
                stripped = ln.strip()
                if (
                    stripped.startswith("import ")
                    or stripped.startswith("from ")
                    or paren_depth > 0
                    or stripped == ""
                    or stripped.startswith("#")
                ):
                    if (
                        stripped.startswith("import ")
                        or stripped.startswith("from ")
                        or paren_depth > 0
                    ):
                        last_import_line = i
                    i += 1
                    continue
                break
            # Insert after last_import_line.
            insert_at = sum(len(l) for l in lines[: last_import_line + 1])
            insert_block = (
                "\nfrom functools import lru_cache"
                "\nfrom importlib import resources\n"
            )
            src = src[:insert_at] + insert_block + src[insert_at:]

    # Write core.py
    core_path = pkg_dir / "core.py"
    core_path.write_text(src, encoding="utf-8")
    print(f"  [{package_name}] wrote {core_path}: {len(src.splitlines())} LoC")

    # __init__.py
    legacy_module = (
        legacy_path.replace("puzzleeval/", "puzzleeval.")
        .replace("/", ".")
        .replace(".py", "")
    )
    public_names_list = ", ".join(public_names)
    init_lines = [
        f'"""Agent {package_name.replace("agent", "")} — {description}.',
        "",
        "Phase 7: per-agent package structure. The legacy path",
        f"``{legacy_module}`` is preserved as a thin re-export shim",
        "for back-compat.",
        "",
        f"Canonical home: ``puzzleeval.agents.{package_name}.core``.",
        '"""',
        "",
        f"from puzzleeval.agents.{package_name}.core import *  # noqa: F401,F403",
        f"from puzzleeval.agents.{package_name}.core import (",
    ]
    for name in public_names:
        init_lines.append(f"    {name},")
    init_lines += [
        ")",
        "",
        f"__all__ = {list(public_names)!r}",
    ]
    open(pkg_dir / "__init__.py", "w", encoding="utf-8").write(
        "\n".join(init_lines) + "\n",
    )
    print(f"  [{package_name}] wrote __init__.py")

    # Legacy shim.
    shim_lines = [
        f'"""Back-compat shim — moved to ``puzzleeval.agents.{package_name}``.',
        "",
        f"Phase 7: this module re-exports the public surface from the new",
        f"``{package_name}`` package. Existing callers using",
        f"``from {legacy_module} import X`` keep working unchanged.",
        f"New code should prefer ``puzzleeval.agents.{package_name}.core``.",
        '"""',
        "",
        f"from puzzleeval.agents.{package_name}.core import *  # noqa: F401,F403",
        "# Explicit re-export of public names so source-grep tests still find them.",
        f"from puzzleeval.agents.{package_name}.core import (",
    ]
    for name in public_names:
        shim_lines.append(f"    {name},")
    shim_lines += [")"]
    open(legacy_path, "w", encoding="utf-8").write("\n".join(shim_lines) + "\n")
    print(f"  [{package_name}] wrote shim {legacy_path}")


AGENTS = {
    "agent1": dict(
        legacy_path="puzzleeval/agents/user_understanding.py",
        package_name="agent1",
        public_names=["SYSTEM_PROMPT", "run_user_understanding_agent"],
        prompts=[("SYSTEM_PROMPT", "system_prompt.md", None)],
        description=(
            "User Understanding Agent (parses user request into sub-tasks "
            "+ workflow blueprint)"
        ),
    ),
    "agent2": dict(
        legacy_path="puzzleeval/agents/research.py",
        package_name="agent2",
        # Public names: prompts + the main entry point + helpers tests use.
        public_names=[
            "RESEARCH_SYSTEM_PROMPT",
            "STRUCTURE_SYSTEM_PROMPT",
            "run_research_agent",
        ],
        prompts=[
            ("RESEARCH_SYSTEM_PROMPT", "research_system.md", None),
            ("STRUCTURE_SYSTEM_PROMPT", "structure_system.md", None),
        ],
        description="Research Agent (web search + candidate scoring)",
    ),
    "agent3": dict(
        legacy_path="puzzleeval/agents/synthetic_tests.py",
        package_name="agent3",
        public_names=["SYSTEM_PROMPT", "run_synthetic_tests_agent"],
        prompts=[("SYSTEM_PROMPT", "system_prompt.md", None)],
        description="Synthetic Tests Agent (text-based test case generation)",
    ),
    "agent3f": dict(
        legacy_path="puzzleeval/agents/synthetic_tests_file.py",
        package_name="agent3f",
        public_names=["SYSTEM_PROMPT", "run_file_tests_agent"],
        prompts=[("SYSTEM_PROMPT", "system_prompt.md", None)],
        description="File-Based Test Cases Agent (uses uploaded files)",
    ),
    "agent4": dict(
        legacy_path="puzzleeval/agents/screening.py",
        package_name="agent4",
        public_names=[
            "VERIFICATION_SYSTEM_PROMPT",
            "STRUCTURE_SYSTEM_PROMPT",
            "run_screening_agent",
        ],
        prompts=[
            ("VERIFICATION_SYSTEM_PROMPT", "verification_system.md", None),
            ("STRUCTURE_SYSTEM_PROMPT", "structure_system.md", None),
        ],
        description="Screening Agent (per-candidate API verification)",
    ),
}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if target not in AGENTS:
        print(f"Usage: python {sys.argv[0]} <{'/'.join(AGENTS.keys())}>")
        sys.exit(1)
    cfg = AGENTS[target]
    restructure_agent(**cfg)
    print(f"Done: {target}")
