---
type: knowledge
problem_type: best_practice
severity: medium
tags:
  - "workflow"
  - "workspace"
  - "multi-project"
confidence: high
created: 2026-04-15
source: "internal knowledge"
---

# Workspace Project Disambiguation

## Context
The user's workspace contains multiple related projects in sibling directories: STRATHEX (legacy), STRATHMARK (this repo — calculation core), KYTHEREX, Missoula Pro-Am Manager. Code and concepts cross-reference between them, and a request like "fix the variance bug" is ambiguous if the agent assumes the wrong project.

## Pattern
Before making any code changes, confirm which project/subdirectory the user is referring to. If the referenced code isn't found in the current working directory, check sibling directories AND check for separate GitHub repos (some projects are only on GitHub, not in the local workspace yet).

When a developer references a business, feature, or prior decision by name ("Pyramid Lumber", "MT/PNW pivot", "Pro-Am Manager"), search the current repo's README, DESIGN.md, and recent git log before claiming ignorance.

## Rationale
- STRATHEX and STRATHMARK share heritage (STRATHMARK migrated from STRATHEX in commit 5416342) — code patterns look similar but diverged
- The Pro-Am Manager is a SEPARATE repo that CONSUMES STRATHMARK — a bug report about "the cascade hanging" could be about either side of the boundary
- Silently making changes in the wrong repo creates merge conflicts and lost work

## Examples
Good: user says "fix the Ollama timeout." Agent checks `cwd` (STRATHMARK), finds `strathmark/llm.py`, confirms `call_ollama` is defined here, proceeds.

Bad: user says "fix the Ollama timeout." Agent assumes STRATHMARK, but the timeout is actually in the Pro-Am Manager's wrapper that SETS `STRATHMARK_OLLAMA_TIMEOUT` before calling STRATHMARK. Agent edits the wrong file, change ships to wrong repo.

When in doubt, ask: "Is this fix needed in STRATHMARK (the engine) or in the calling side (Pro-Am Manager)?"
