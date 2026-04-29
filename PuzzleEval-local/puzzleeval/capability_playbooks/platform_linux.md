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

Standard GNU coreutils are available (`tail`, `head`, `grep`, `wc`,
`find`, `xargs`, `sed -i`, `grep -P`, etc.). Use them naturally — the
core builder rules cover the rest. This playbook is intentionally
minimal: Linux is the prompt's defaults, so there's nothing to
translate.

`python -c "..."` captures stdout reliably on Linux. For multi-line
scripts, prefer `write_file` + `python foo.py` so the code lives on
disk for debugging; inline is fine for one-liners.

If you find yourself wanting to add a Linux-specific rule, ask first:
"Does this rule conflict with what builders already assume?" If yes,
add it here. If no, the builder doesn't need it.
