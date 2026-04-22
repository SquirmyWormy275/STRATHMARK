# Changelog

All notable changes to STRATHMARK will be documented in this file.

## [0.4.1] - 2026-04-21

### Added
- `ONBOARDING.md` at repo root — routing hub for new contributors by task (bug fix, feature, deployment debug, AI agent)
- 7 new learning docs in `docs/solutions/`:
  - `best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md` — cross-cutting v0.3.0 hardening narrative
  - `best-practices/optional-dependency-gating-importorskip.md` — `[extras]` split + `pytest.importorskip` placement gotcha
  - `best-practices/config-env-vars-importlib-reload-pattern.md` — test pattern for import-time env vars
  - `architecture-decisions/dual-store-sqlite-supabase-split.md` — offline-first rationale for SQLite + Supabase
  - `architecture-decisions/ensemble-predictor-design-decisions.md` — accepted design + deferred scope for TODO-001..011
  - `test-failures/regression-test-must-include-triggering-input.md` — Codex-caught regression-coverage pattern
  - `workflow-patterns/eng-review-cycle-and-todos-handoff.md` — plan-review → TODOS.md → impl → Codex → ship pattern

### Changed
- `CLAUDE.md` — added ONBOARDING.md pointer and `docs/solutions/` entry in project-structure tree (for agent discoverability)
- `docs/solutions/architecture-decisions/prediction-cascade-order.md` — added v0.3.0 provenance for JSON-schema / temp=0 / quality==5, cross-link to new hardening doc, `last_updated: 2026-04-21`
- `docs/solutions/architecture-decisions/mark-floor-ceiling-invariants.md` — added pre-v0.3.0 ceiling-rounding historical note, cross-link to new hardening doc, `last_updated: 2026-04-21`
- `strathmark/__init__.py` — `__version__` corrected from `0.3.1` (stale) to `0.4.1`
- `README.md` — version string updated to 0.4.1 and onboarding pointer added

## [0.4.0] - 2026-04-07

### Added
- Gemini cloud fallback (`google-generativeai`) in `llm.py` as a race-day fail-through when Ollama is unreachable
- `_env_int`/`_env_str` helpers in `config.py` for safe env-var parsing with sensible fallbacks
- `scripts/validate_deployment.py` — pre-event read-only deployment validation
- `scripts/ingest_proam_results.py` — post-event Pro-Am result ingestion with dry-run / --commit modes
- `push_results_dicts()` and `register_competitor()` public helpers for programmatic ingestion
- `docs/DEPLOYMENT.md` — full deployment guide
- `tests/test_deployment_fallbacks.py` — fail-fast cascade tests for unreachable Ollama / Supabase

### Changed
- Race-day Ollama defaults: `TIMEOUT_SECONDS` and `MAX_RETRIES` now env-overridable via `STRATHMARK_OLLAMA_*`; default `MAX_RETRIES=0` for fail-fast cascade behavior
- `STRATHMARK_OLLAMA_URL` / `STRATHMARK_OLLAMA_TIMEOUT` / `STRATHMARK_OLLAMA_MAX_RETRIES` now documented as first-class operational knobs

### Fixed
- Ollama cascade hang on unreachable host (120s blocking per call) — fail-fast via reduced timeout + zero retries
- Ollama status cache race condition under concurrent FastAPI requests — `threading.Lock` protecting cache reads/writes
- `LLMConfig` env-var resolution now happens at call time (via helpers) for hot-swappable values, while import-time frozen dataclass is retained for stable params

## [0.3.1] - 2026-03-24

### Added
- 221 new tests across 9 test files, bringing total from 446 to 667
- Regression tests for fixed bugs: banker's rounding, decay weights, timeout filtering, absolute variance, mark floor/ceiling enforcement
- Boundary tests for extreme values: 50-competitor fields, diameters 50-600mm, quality clamping, consistency rating thresholds
- Config invariant tests: frozen dataclass enforcement, rules consistency, threshold ordering, ML/LLM config sanity
- Extended decay tests: exponential precision, date type handling, adaptive vs fixed weighting, robust MAD clipping
- Extended store tests: CRUD, duplicate detection, DataFrame round-trips, column aliases, date handling
- Extended visualization tests: ASCII bar chart accuracy, line width, percentage sums, large field rendering
- Predictor regression tests: cascade priority, tournament weighting with num_tournament_rounds, division fallback
- Full pipeline integration tests: predict-calculate-simulate round-trips, multi-event days, store round-trips, simulation determinism
- Wood boundary tests: species multipliers, Janka hardness monotonicity, event scaling exponents
