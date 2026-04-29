---
id: platform_linux
version: 1
title: Linux Platform Shell Rules
category: platform
description: |
  Linux-specific shell guidance for harness builders. Standard POSIX
  utilities are available — minimal translation needed. Always-on for
  Agent 5 builds on Linux; inert for other platforms.
selectors:
  trigger_platforms:
    - linux
selection_mode: always_on
priority: 200
applies_to_agents:
  - agent_5
paired_gates: []
related_contracts: []
revisit_when:
  - "Distribution-specific quirks emerge that warrant teaching"
---

## POSIX shell notes (OS: Linux detected)

Standard POSIX utilities are available (`tail`, `head`, `grep`, `wc`,
`find`, `xargs`, etc.). Use them naturally — no Windows translation
layer needed.

`python -c "..."` captures stdout reliably on Linux. For multi-line
scripts, still prefer `write_file` + `python foo.py` so the code
lives on disk for debugging, but inline is fine for one-liners.
