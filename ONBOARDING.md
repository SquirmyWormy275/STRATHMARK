# Onboarding

STRATHMARK is a pip-installable woodchopping handicap engine. This doc is a routing hub: it points you at the right file for what you're trying to do. It does not re-document things that live elsewhere.

## First five minutes

```bash
pip install -e ".[dev]"
pytest tests/ -v     # core test suite
```

Read in this order:
1. [`README.md`](README.md) — library usage, public API, design rules, package structure
2. [`docs/wiki/Home.md`](docs/wiki/Home.md) — architecture overview, invariants, where STRATHMARK fits vs. STRATHEX / Missoula-Pro-Am-Manager
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module structure and design decisions

The design rules (mark floor=3, ceiling=183, absolute variance only, cascade order, plain-text output) are non-negotiable. They are enforced in code and in review.

## If you're fixing a bug

1. Reproduce it first. Capture the exact input that triggers it.
2. Investigate root cause — use `/investigate` or systematic debugging.
3. Write the regression test **before** the fix — see [`docs/solutions/test-failures/regression-test-must-include-triggering-input.md`](docs/solutions/test-failures/regression-test-must-include-triggering-input.md). A regression test that doesn't include the triggering input passes forever regardless.
4. Ship via `COMMIT PATCH` for single-file patches or `PREPARE FOR COMMIT` for multi-file work — see [`docs/solutions/workflow-patterns/prepare-for-commit-protocol.md`](docs/solutions/workflow-patterns/prepare-for-commit-protocol.md).
5. Document non-obvious fixes in `docs/solutions/` via `/ce:compound`.

## If you're adding a feature

1. Run `/plan-eng-review` (architecture) and/or `/plan-ceo-review` (scope) before writing code.
2. Decisions land in [`TODOS.md`](TODOS.md) as TODO-NNN entries with a stable identifier.
3. Implement against the TODO, prefixing commits with the scope `(eng-review)`, `(qa)`, etc.
4. Get a second opinion via `/codex review`.
5. Ship via `/ship` → PR → CI → merge. Direct-to-main is accepted only for small patches.
6. Document the outcome via `/ce:compound`.

Full pattern: [`docs/solutions/workflow-patterns/eng-review-cycle-and-todos-handoff.md`](docs/solutions/workflow-patterns/eng-review-cycle-and-todos-handoff.md).

## If you're debugging a live deployment

Start at [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — env vars, pre-event checklist, post-event ingestion, troubleshooting.

Most common failure modes and their docs:
- **Ollama unreachable** → cascade skips LLM tier and proceeds Manual → ML → Baseline → Panel. See [`docs/solutions/performance-issues/ollama-cascade-hang-on-unreachable-host.md`](docs/solutions/performance-issues/ollama-cascade-hang-on-unreachable-host.md).
- **Supabase DNS / credentials fail** → local SQLite keeps working; only ingestion is blocked. See [`docs/solutions/architecture-decisions/dual-store-sqlite-supabase-split.md`](docs/solutions/architecture-decisions/dual-store-sqlite-supabase-split.md).
- **Env vars not taking effect** → STRATHMARK reads some env vars at import time. See [`docs/solutions/configuration-issues/env-vars-resolved-at-import-time.md`](docs/solutions/configuration-issues/env-vars-resolved-at-import-time.md).

Always run `python scripts/validate_deployment.py` before an event. If it fails any check, do not start the event.

## Reference map

| File | What it's for |
|---|---|
| [`README.md`](README.md) | Library usage, public API, design rules, package structure, downstream relationships |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev install, lint, CI, test loop |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history, including the 1.0.0 publication release |
| [`TODOS.md`](TODOS.md) | Active design work (ensemble predictor, P1/P2 items) |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Live-event deployment guide |
| [`docs/wiki/`](docs/wiki/) | Architecture reference (19 pages: cascade, variance, wood, decay, etc.) |
| [`docs/solutions/`](docs/solutions/) | Learnings database — organized by category |

## Design-rule cross-reference

Every invariant lives in exactly one place in code:

| Invariant | File | Where |
|---|---|---|
| Mark floor = 3 | `strathmark/calculator.py` | `HandicapCalculator.MARK_FLOOR` |
| Mark ceiling = 183 | `strathmark/calculator.py` | `HandicapCalculator.MARK_CEILING` |
| Gap logic + banker's rounding | `strathmark/calculator.py` | `_assign_marks()` |
| Absolute variance only | `strathmark/variance.py` | module constants + docstring |
| Prediction cascade | `strathmark/predictor.py` | `get_best_prediction()` |
| Time-decay half-life (730 days) | `strathmark/decay.py`, `strathmark/config.py` | `DecayConfig.HALF_LIFE_MODERATE_DAYS` |
| 97% same-tournament weighting | `strathmark/predictor.py` | graduated weighting in `predict_baseline()` |
| Plain-text output | `strathmark/calculator.py` | `StartSheet.render()` |

Bypassing any of these through normal imports is not possible — change them at the one location and review will catch the design implication.

## Downstream consumers

STRATHMARK is a shared calculation core. Changes here propagate to:
- **STRATHEX** — full tournament-management system
- **Missoula-Pro-Am-Manager** — race-day scoring UI

Both depend on STRATHMARK via pinned version. The CI matrix (`.github/workflows/ci.yml`) runs lint + the full test suite across Python 3.10/3.12/3.13 on Ubuntu and Windows, plus a wheel build. Every PR must pass CI before merge.

If a change breaks a downstream project, it breaks every project that depends on STRATHMARK. Treat the public API as a contract — see the "Public vs internal API" section of [`docs/wiki/Architecture-Overview.md`](docs/wiki/Architecture-Overview.md).

## Common first tasks

- **Understand one module** — read the docstring at the top, then its tests. Every module has a `test_<module>.py` and most have a `test_<module>_extended.py` for edge cases.
- **Run the ML or API tests** — `pip install -e ".[dev,ml,api,llm,db]"` then `pytest`.
- **Make a small change** — scope to one file, use `COMMIT PATCH`, ship.
- **Understand why something is the way it is** — check `docs/solutions/` for the category matching your question before opening an issue.

## Questions this doc doesn't answer

- "What does variance scaling look like in practice?" → [`docs/wiki/Variance-and-Monte-Carlo.md`](docs/wiki/Variance-and-Monte-Carlo.md)
- "How does the ML predictor train?" → `scripts/train_model.py` + [`docs/wiki/Prediction-Cascade.md`](docs/wiki/Prediction-Cascade.md)
- "Why this rulebook and not another?" → [`docs/wiki/Rulebook-Comparison.md`](docs/wiki/Rulebook-Comparison.md)
- "What changed in v0.3.0?" → [`CHANGELOG.md`](CHANGELOG.md) and [`docs/solutions/best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md`](docs/solutions/best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md)

If the answer isn't in any of those, it's probably a genuine gap — open an issue or capture it via `/ce:compound` after you solve it.
