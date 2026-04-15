---
type: knowledge
problem_type: workflow_pattern
severity: medium
tags:
  - "workflow"
  - "commit"
  - "release"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# PREPARE FOR COMMIT and COMMIT PATCH Protocols

## Context
STRATHMARK uses two standing orders (defined in `MEMORY.md`) that define the release workflow. Both are triggered by the user typing the exact phrase — they define the ordered steps for shipping a change.

## Pattern

**PREPARE FOR COMMIT** — full release cycle:
1. Scan the entire codebase (source, tests, config)
2. Update docs (CLAUDE.md, DEVELOPMENT.md, README)
3. Scrub `__pycache__`, `.pyc`, temp artifacts — list before deleting
4. Reorganize misplaced files — confirm with user before moving
5. Bump version (patch/minor/major based on scope); record in MEMORY.md
6. Update existing tests for signature changes; add new tests for new functionality; run `pytest`; fix failures until green
7. Draft imperative-style commit message
8. After commit, record session changes under "Completed Work" in MEMORY.md

**COMMIT PATCH** — fast-lane for small fixes:
1. Scrub `__pycache__`, `.pyc`, temp files
2. Bump patch version (e.g., 0.3.0 → 0.3.1); record in MEMORY.md
3. Run `pytest` as-is — do NOT rewrite or add tests; fix only patch-introduced failures
4. Draft commit message with `fix:` or `patch:` prefix, imperative style
5. Append one-liner to the "Patches" list in MEMORY.md (no full session block)

## Rationale
PREPARE FOR COMMIT enforces that every version bump ships with matching docs and full green tests. COMMIT PATCH exists because forcing a full doc sweep on a one-line fix creates friction that discourages small, safe patches. The split makes "small fix, quick ship" cheap while keeping the main release path rigorous.

## Examples
When the user says "COMMIT PATCH" after a one-line bugfix, do NOT update docs or write new tests — just scrub, bump patch, verify existing tests pass, commit. When they say "PREPARE FOR COMMIT" after a multi-file feature, do the full 8-step sequence. Never merge the two — running the patch flow on a feature-sized change leaves docs stale; running the full flow on a one-liner wastes the user's time.
