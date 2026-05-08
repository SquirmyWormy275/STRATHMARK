---
type: knowledge
problem_type: workflow_pattern
severity: high
tags:
  - "workflow"
  - "plan-review"
  - "todos"
  - "code-review"
  - "session-handoff"
confidence: high
created: 2026-04-21
source: "Mar 24 2026 eng-review + CEO-review sessions, Apr 7 deployment session"
---

# Eng-Review Cycle and TODOS.md Handoff

## Context
STRATHMARK uses a multi-session debugging-and-design pipeline where plan reviews, implementation, adversarial review, and documentation each happen in separate sessions. Session context evaporates between runs. Without a persistent handoff artifact, every new session re-discovers what the last one decided.

`TODOS.md` is the handoff artifact. It is not a to-do list in the productivity sense — it is a *narrative of decisions across sessions*.

## Pattern

Stages, each typically a separate session:
1. `/plan-eng-review` or `/plan-ceo-review` — identifies issues, generates TODO-NNN entries in `TODOS.md` (what, why, context, effort, priority, source, status).
2. Implementation — commits prefixed with scope: `(eng-review)`, `(qa)`, `(ci)`, etc.
3. Codex adversarial review — `/codex review` or equivalent, a second-opinion pass.
4. Ship — `/ship` → PR → CI → merge. Direct-to-main acceptable for small patches.
5. Document — `/document-release` or `/ce:compound` → `docs/solutions/` entry.
6. Status update — `TODOS.md` gets a new `Status (YYYY-MM-DD):` line: RESOLVED, DEFERRED, or narrative.

The artifacts that survive between sessions:
- **Git commits** with a scope prefix naming which review surfaced them: `(eng-review)`, `(qa)`, `(ci)`, `(cso)`.
- **TODOS.md** entries with a stable identifier (TODO-NNN) and a `Status (YYYY-MM-DD):` line when the state changes.
- **docs/solutions/** entry with the outcome and cross-links.

## TODO entry template

```markdown
### TODO-NNN: Short action-oriented title
**What:** Concrete change to make.
**Why:** The rationale or argument surfaced in review.
**Context:** Code references, dependencies, non-obvious constraints.
**Status (YYYY-MM-DD):** in-progress / resolved / deferred — with narrative.
**Effort:** S (human: ~X / CC: ~Y)
**Priority:** P1 / P2
**Depends on:** Other TODO-NNN, or Nothing
**Source:** Which review and when (e.g., "Eng review outside voice 2026-03-23")
```

The `Source:` field is load-bearing. Six months later, when someone asks "why are we doing the ensemble this way?", the source pointer lets them re-open the original review.

## Rationale
- **Session context is ephemeral.** Without TODOS.md, the next session asks "what did we decide?" and the answer is somewhere in transcripts that may or may not still exist.
- **TODO-NNN identifiers are stable.** Cross-linking from commits, solution docs, and code comments back to TODO-011 always resolves.
- **Status-over-time is a narrative.** A TODO entry with three `Status (YYYY-MM-DD)` lines tells the story: "surfaced in eng review", "tentatively resolved", "re-opens when ensemble ships". Future readers get the arc, not just the current state.
- **Scope prefixes on commits make archaeology trivial.** `git log --grep='(eng-review)'` surfaces every eng-review-driven change.

## When to Apply
- Any multi-week feature where review happens before implementation
- Any plan review that produces more than 3 discrete action items
- Any adversarial review cycle where the reviewer surfaces multiple findings
- Before deferring work — a TODO entry with `Priority: P2` + `Status: deferred` is cheaper than re-deriving the argument later

## When to skip
- Single-file bug fixes — a commit message is enough
- Workflow changes that land in one session — capture directly in a solution doc
- Documentation edits — the doc itself is the artifact

## Examples
- [`TODOS.md`](../../../TODOS.md) — current file, TODO-001..011 (ensemble predictor design)
- Commit `10a4726` — `refactor(eng-review): extract variance scaling magic number 0.12 to config` — resolves an eng-review finding
- Commit `6951d58` — `fix(eng-review): add threading.Lock to Ollama connection status cache` — resolves a different eng-review finding, captured in [`../runtime-errors/ollama-status-cache-race-condition.md`](../runtime-errors/ollama-status-cache-race-condition.md)
- Commit `df2fe3a` — `fix(eng-review): fix integration test fixture (event→event_code) and Timestamp date subtraction bug` — captured in [`../test-failures/integration-test-fixture-silent-typeerror.md`](../test-failures/integration-test-fixture-silent-typeerror.md)
- TODO-011 — tournament weighting audit — resolved with Status line + cross-link to [`../architecture-decisions/tournament-weighting-audit-todo-011.md`](../architecture-decisions/tournament-weighting-audit-todo-011.md)

## Related
- [`prepare-for-commit-protocol.md`](prepare-for-commit-protocol.md) — the ordered steps that run inside the "Implementation session" stage
- [`../best-practices/workspace-project-disambiguation.md`](../best-practices/workspace-project-disambiguation.md) — confirm the right project before any review
