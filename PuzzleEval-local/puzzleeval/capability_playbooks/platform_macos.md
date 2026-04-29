---
id: platform_macos
version: 1
title: macOS Platform Shell Rules
category: platform
description: |
  macOS-specific shell guidance for harness builders. BSD-style POSIX
  utilities differ from GNU in some flag syntax (sed -i, grep -P).
  Always-on for Agent 5 builds on macOS; inert for other platforms.
selectors:
  trigger_platforms:
    - darwin
selection_mode: always_on
priority: 200
applies_to_agents:
  - agent_5
paired_gates: []
related_contracts: []
revisit_when:
  - "Apple Silicon / arm64-specific build quirks emerge"
---

## POSIX shell notes (OS: macOS detected)

BSD-style POSIX utilities are available but some flag syntax differs
from GNU (e.g., `sed -i` requires an empty string arg: `sed -i '' ...`;
`grep -P` for Perl regex isn't supported — use `grep -E` or `rg`).
If your command fails with a sed/grep flag issue, pivot to writing
a Python script instead — faster than debugging BSD vs GNU.

`python -c "..."` captures stdout reliably on macOS.
