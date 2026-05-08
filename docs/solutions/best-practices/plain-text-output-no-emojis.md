---
type: knowledge
problem_type: best_practice
severity: medium
tags:
  - "output"
  - "cli"
  - "style"
confidence: high
created: 2026-04-15
source: "internal knowledge"
---

# Plain Text Output Only

## Context
STRATHMARK runs on event-day laptops, Windows terminals (PowerShell/cmd), CI logs, and piped into the Pro-Am Manager. Emojis and ANSI color codes break on at least one of these regularly (cmd.exe shows boxes, log aggregators choke on control bytes, screen readers fail).

## Pattern
- No emojis in any output — CLI, logs, error messages, docstrings shown to users
- No ANSI color codes
- Use `[OK]` / `[FAIL]` / `[WARN]` instead of colored ticks/crosses
- Plain ASCII bar charts in `visualization.py`, not Unicode block characters

## Rationale
The event laptop is Windows; cmd.exe and some PowerShell versions misrender Unicode. A mark sheet that prints correctly on one machine but shows `â–ˆ` blocks on another is unacceptable during a live event. Plain text is the lowest common denominator and loses no information.

## Examples
From `scripts/validate_deployment.py`:
```
Supabase:           [OK]
Predictions (base): [OK]
Predictions (LLM):  [OK]
READY FOR DEPLOYMENT: [YES]
```

NOT:
```
Supabase:           ✅
Predictions (base): ✅
READY FOR DEPLOYMENT: 🚀
```

When writing any new user-facing output, first ask "will this render in cmd.exe on a Windows 10 laptop at a Montana fairground with no admin access to install a better terminal?"
