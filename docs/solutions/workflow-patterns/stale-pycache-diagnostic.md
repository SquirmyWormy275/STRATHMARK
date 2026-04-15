---
type: knowledge
problem_type: workflow-pattern
severity: medium
tags:
  - "debugging"
  - "python"
  - "cache"
  - "windows"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Stale __pycache__ as First Diagnostic Step

## Context
Python caches compiled bytecode in `__pycache__/*.pyc`. When a module is renamed, moved, or a class signature changes, stale `.pyc` files can be loaded instead of the current source. Symptoms look like "the code is correct but Python is running something else" — especially common on Windows where case-insensitive filesystems interact badly with cache invalidation.

## Pattern
When a Python import error, attribute error, or "the fix isn't taking effect" bug appears and the source code looks correct, FIRST clear `__pycache__/` before deeper investigation:

```bash
find . -type d -name __pycache__ -exec rm -rf {} +
```

On Windows Git Bash this is the same command. On PowerShell:
```powershell
Get-ChildItem -Path . -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
```

## Rationale
This is a 5-second check that resolves a non-trivial fraction of "weird Python behavior" reports. Doing it BEFORE a long debugging dive saves hours. The STRATHMARK session-start hook in `.claude/settings.json` automatically clears `__pycache__` on every Claude Code session start for this reason.

## Examples
Symptoms that should trigger a pycache clear first:
- `ImportError: cannot import name X from Y` when grep shows X clearly defined in Y
- Tests passing locally but failing after a rename
- Test runs using an old version of a class signature
- `AttributeError` on an attribute that exists in source
