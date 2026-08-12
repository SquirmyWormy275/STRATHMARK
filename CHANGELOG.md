# Changelog

All notable changes to STRATHMARK will be documented in this file.

## [2.0.0] - 2026-08-11

Prediction Engine V2 replaces the numeric Manual/LLM/ML/baseline cascade with one
reproducible, prior-only prediction system while retaining the five legacy result keys
for compatibility.

### Added

- Robust hierarchical log-time core with event and diameter effects, six species
  physical properties, gender/missingness, recency-weighted competitor state, bounded
  trend, cross-event borrowing, and partial pooling for zero/sparse history.
- Chronological split-conformal 90% prediction intervals with calibration state and
  scope, kept separate from race-performance `std_dev`.
- Safe checksummed JSON core artifact packaged with the wheel and one immutable
  `PredictionBundle` snapshot per request.
- Optional CatBoost residual artifact and strict rolling-origin promotion gate. No
  residual candidate is promoted in the 2.0.0 release.
- Deterministic joint mark optimizer using 2,048 common-random samples and at most eight
  coordinate passes, with a bounded rounded-gap fallback.
- Append-only local SQLite prediction ledger, authenticated REST ledger routes,
  immutable settlement revisions, and optional best-effort Supabase mirroring through
  migration `20260811_005_prediction_v2.sql`.
- Frozen release evidence and a verify-only operator command: `python train_model.py`.

### Changed

- Numeric evidence is now restricted to stable identity/history, event, strictly prior
  dated results, diameter, species physical properties, and gender including missing.
- `get_all_predictions()` maps `baseline` to the V2 core, `ml` to a promoted residual
  only, `panel` to the broad fallback, and `llm` to `None`.
- `HandicapCalculator.calculate()` snapshots the engine once and chooses marks jointly.
- REST responses include interval, engine/model/calibration, warning, optimizer,
  prediction-ID, degradation, and ledger-state fields.
- Public `/calculate` and `/predict` remain stateless. Trusted writes use
  `/ledger/calculate` and require stable IDs, bearer authentication, and `request_id`.

### Retired

- Numeric LLM generation, selection, and adjustment. LLMs remain available only for
  narrative features.
- Numeric use of division, round/heat, venue, lane/stand, run order, exact material
  identity, wood quality/moisture, weather, equipment, fatigue, penalty/DNF state,
  same-tournament weighting, and field strength. Legacy inputs remain accepted as
  documented no-ops for one migration release.

### Validation

- Frozen 128-row temporal test: MAE 16.1301 vs 20.5172 for the strict prior-only
  incumbent (21.38% lower); RMSE 33.6904 vs 44.4791 (24.26% lower); 90% interval
  coverage 94.53%. These results apply only to the checked-in workbook and split;
  cohort samples are smaller and no universal accuracy or fairness claim is made.

### Migration

- Default engine is V2. Set `STRATHMARK_PREDICTION_ENGINE=legacy` for the temporary,
  deterministic baseline-only rollback. The rollback never invokes an LLM numerically.
- See `docs/PREDICTION_ENGINE_V2.md` and `docs/DEPLOYMENT.md` before upgrading a live
  consumer.

## [1.0.0] - 2026-05-08

First public PyPI release. No API changes relative to 0.5.0; the 1.0.0 designation marks publication readiness, not a redesign. The library has been production-tested for multiple seasons across STRATHEX and the Missoula Pro-Am tournament manager.

### Added
- PyPI distribution metadata: `Development Status :: 5 - Production/Stable`, `License :: OSI Approved :: Apache Software License`, `Operating System :: OS Independent`, and `Topic :: Software Development :: Libraries` classifiers.
- `[project.urls]` entries for Homepage, Issues, Documentation, and Changelog (in addition to the existing Source URL).
- Author email field in `[project.authors]`.
- Lower bound on the `ollama` optional dependency (`ollama>=0.3`) in the `[llm]` extra; previously unconstrained.

### Changed
- Version bumped from 0.5.0 to 1.0.0 to mark the first stable PyPI release. `__version__` in `strathmark/__init__.py` updated to match.
- README image and inter-doc links rewritten as absolute GitHub URLs (`https://raw.githubusercontent.com/.../main/...` for the logo, `https://github.com/SquirmyWormy275/STRATHMARK/blob/main/...` and `.../tree/main/...` for documents and directories) so they render correctly on the PyPI project page.
- README test-count figure updated from 708 to 759 to match the current suite.
- `.gitignore` virtual-environment block extended with `.venv*/` so per-purpose venvs (e.g. `.venv-pypi-readiness`) are ignored without relying on Python's venv-internal `.gitignore`.

### Fixed
- README "Usage" prose claimed `HandicapCalculator().calculate()` returns `CalculationResult` objects with a `gap` attribute. The actual return type is `MarkResult` (defined in `strathmark.calculator`) with fields `name`, `mark`, `predicted_time`, `method_used`, `confidence`, `explanation`, `std_dev`. README rewritten to match the real API. Discovered by running the README example against the built wheel.

### Removed
- Placeholder LinkedIn line in the README "About the author" section.

## [0.5.0] - 2026-05-08

### Added
- MNEMEX integration: STRATHMARK Supabase becomes a hydrated cache of canonical results held in a separate MNEMEX project. Sync runs in dry-run mode until `MNEMEX_SUPABASE_URL`/`MNEMEX_SUPABASE_KEY` are configured.
- `strathmark/mnemex.py` — read-only MNEMEX client plus competitor registration helper. Reads canonical results, looks up canonical competitors, mints new ones via ULID.
- `strathmark/sync.py` — three sync paths sharing one upsert core: `nightly_batch()` (cron at 03:00 UTC), `strathex_finalization(event_id)` (webhook), `manual_force_sync(show_name=None, since=None)` (admin button / CLI). Failures raise after writing an audit-trail row to `sync_log`.
- `strathmark/drift.py` — calibration drift detection. `evaluate_drift(model_version_id, lookback_days=30)` returns a `DriftReport` with mean-shift, variance-ratio, and empirical-coverage alerts. Advisory only; never auto-deactivates a model.
- `strathmark/db.py` ML state writes: `register_model_version()`, `set_active_model()`, `record_calibration()`, `store_features()`, `record_prediction()`, `settle_prediction()`. Best-effort vs raise-on-failure discipline matches the policy doc.
- `_BiasCircuitBreaker` in `strathmark/predictor.py` — 60-second sliding window, 3-strike threshold, auto-reset. Replaces the prior session-level disable so transient Supabase blips don't permanently degrade bias correction.
- Three SQL migrations under `strathmark/migrations/`: `001` adds source-tracking columns (`source_type`, `mnemex_id`), `002` creates ML state tables (`model_versions`, `calibration_tables`, `feature_store`, `predictions`, `prediction_residuals` extensions), `003` reframes RLS for controlled-write enforcement (FORCE ROW LEVEL SECURITY plus a dedicated `mnemex_sync` role plumbing checklist).
- `scripts/rekey_against_mnemex.py` — idempotent re-keying script that fills in `mnemex_id` on existing STRATHMARK competitor rows from MNEMEX. Refuses to commit below the 95% match-rate threshold without `--force`.
- `docs/ml-persistence-policy.md` — policy doc covering retraining cadence, model versioning, calibration drift, feature store, hot-path circuit breaker, non-blocking guarantee.
- `docs/schema-reality-2026-05-04.md`, `docs/cleanup-candidates-2026-05-04.md`, `docs/ml-research-questions.md` — Phase 1 schema verification plus a cleanup audit confirming zero stray validation rows.
- 65 new tests across `tests/test_mnemex.py`, `tests/test_sync.py`, `tests/test_drift.py`, `tests/test_circuit_breaker.py`, `tests/test_ml_state.py`, plus 277 lines of additions to `tests/test_db.py`.

### Changed
- `register_competitor()` rewrite: routes through MNEMEX when configured, falls back to STRATHMARK-local mint with a deprecation warning when MNEMEX is unset. Default `wait_for_sync=False`; opting in to blocking requires explicit kwarg.
- `get_competitor_bias()` no longer swallows DB exceptions internally. Callers on the prediction hot path wrap in `_BiasCircuitBreaker`; the policy is documented in `docs/ml-persistence-policy.md` section 5.
- `_do_sync` writes a failure sync_log row before re-raising on Supabase upsert error, so the audit trail is preserved AND callers see a non-zero exit (matches the module docstring).
- Added `ulid-py>=1.1` to base dependencies.

### Fixed
- Cascade naming mismatch: `record_prediction` validator and the `predictions_cascade_level_check` constraint now accept `'panel'` instead of `'panel_fallback'`, matching the canonical emitter strings in `strathmark/predictor.py`.
- Drift detection's coverage rule now compares empirical coverage of recent residuals against a 90% prediction interval derived from baseline residual quantiles, replacing a check that compared the static calibration-time `coverage_at_90` value against a fixed band.
- RLS migration `003` now adds `FORCE ROW LEVEL SECURITY` to all governed tables, closing the BYPASSRLS bypass on `service_role`. Pre-application checklist expanded to require role plumbing AND `NOBYPASSRLS` rotation. The `wood_species_write_admin` policy no longer ORs in `service_role` (contradicted the header's "wood_admin only" intent).

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
