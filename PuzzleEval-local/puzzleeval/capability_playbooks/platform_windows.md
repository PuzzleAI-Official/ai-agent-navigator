---
id: platform_windows
version: 1
title: Windows Platform Shell Rules
category: platform
description: |
  Windows-specific shell command translation guidance for harness builders.
  Required when the build sandbox runs on Windows (sys.platform == "win32").
  Teaches Unix→Windows command equivalents (tail, head, grep, &&, etc.) and
  documents the python -c silent-output trap. Always-on for Agent 5 builds
  on Windows; inert for other platforms.
selectors:
  trigger_platforms:
    - win32
selection_mode: always_on
priority: 200
applies_to_agents:
  - agent_5
paired_gates: []
related_contracts: []
revisit_when:
  - "Windows shell semantics change in a future Python release"
  - "We adopt PowerShell as the default shell instead of cmd"
---

## Windows-specific shell caveats (OS: Windows detected)

Real-run evidence (trace d3b49875): ~7 setup turns / $1.36 were burned
on Unix muscle-memory commands that fail silently on Windows. Your
training data is Unix-heavy — translate BEFORE emitting the command:

| Unix command | Windows equivalent                                       |
|--------------|----------------------------------------------------------|
| `tail -5`    | drop the pipe; write file then read last bytes in Python |
| `head -3`    | drop the pipe; use `findstr /N "."` or Python slicing    |
| `grep PAT`   | `findstr PAT`                                            |
| `A && B`     | `A && B` WORKS in cmd/PowerShell, but `A; B` does NOT    |
| `A & B`      | DON'T use — Windows `&` is a sequential separator not bg |
| backticks    | use `$(...)` in PowerShell or pipe to a temp file        |
| `ls -la`     | `dir` OR `python -c "import os; print(os.listdir('.'))"` |
| `which foo`  | `where foo`                                              |

Guideline: NEVER emit `| tail`, `| head`, `| wc`, `| grep` on Windows.
These return exit 255 and waste a turn. Pre-translate instead.

### Known silent-output trap: `python -c "..."` on Windows

`python -c "print(something)"` occasionally returns exit 0 with NO
visible stdout on Windows (subprocess output capture race). If a
`python -c` command on Windows produces no visible output, DO NOT
retry with another `python -c`. Write a `.py` file via `write_file`
and execute with `python foo.py` — this always captures output.

### ASCII-only in generated Python code

Use plain ASCII in `harness.py` / `smoke_test.py` / `live_test.py` /
`requirements.txt` content on Windows: no unicode dashes (`—`, `–`),
no arrows (`→`, `←`), no smart quotes. Use `--` for dashes and `->`
for arrows. Windows file I/O defaults to cp1252 (or local ANSI), and
emitting Unicode glyphs in source comments produces silent
encoding-error truncation on read-back. Stick to ASCII for code.
