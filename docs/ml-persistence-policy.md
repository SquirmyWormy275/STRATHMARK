# STRATHMARK ML Persistence Policy

Date: 2026-05-04
Owner: STRATHMARK persistence reframe PR
Status: Policy in force from this PR forward. Implementation lands in this PR
for schema, in follow-on PRs for the runtime behavior changes referenced
below.

This document is the source of truth for how STRATHMARK persists ML state,
when it retrains, how it versions models, how it tracks calibration drift,
and the non-blocking guarantee the prediction hot path lives under.

## 1. Retraining cadence

Retraining is NOT triggered by individual predictions, regardless of how
many residuals have accumulated. The model is treated as a slowly-changing
artifact, not a continuously-updated one.

Two approved triggers:

A. **Scheduled.** A cron-style job retrains on a fixed cadence. Default:
   weekly, on Sunday 03:00 UTC, off-show times. Cadence is operator-tunable
   (weekly vs monthly tradeoff: more frequent retraining captures recent
   competitor trends faster; less frequent retraining reduces churn in
   marks across consecutive shows). The actual scheduler is out of scope
   for this PR.

B. **On-demand.** Operator runs `strathmark train --force` (CLI to be
   added; not in this PR). Used when a major event has injected enough
   new data that the next show benefits from immediate retraining.
   Operator workflow:
   1. Pull latest results into the local cache (`pull_results()` from the
      hydrated cache, or directly from MNEMEX once that path lands).
   2. Run training. The new model is inserted into `model_versions` with
      `is_active=FALSE`.
   3. Operator inspects the new model's holdout MAE / CRPS / 90% coverage
      against the currently-active model.
   4. If the new model is better, run `strathmark activate <ulid>` (CLI
      pending) which calls `set_active_model()`. The old model is
      atomically retired.
   5. Validate by running predictions against a known-good fixture before
      the next live show.

Retraining is never automatic-on-improvement. Activation always requires
an explicit human step. This is by design: a worse model that scores
better on holdout (overfit, distribution shift) must not silently become
the production model.

## 2. Model versioning

Every retraining run creates a new `model_versions` row with a fresh ULID.
Old models are never deleted from the table. Storage is cheap; audit is not.

Invariants:

- One row per training run.
- `is_active=TRUE` on at most one row per `model_type`. Enforced by a
  partial unique index in the DB and by `set_active_model()` at the
  application layer.
- `retired_at` is set when a model loses its active status. Models are
  retired, not deleted. Re-activation is permitted but discouraged
  (typically you'd train a fresh one instead).
- `artifact_storage='supabase_storage'` for binary model blobs (XGBoost
  pickle); `artifact_ref` is the storage path. `artifact_storage='inline_jsonb'`
  for trivially-serializable models (e.g. baseline coefficients) where the
  full model fits in `notes` or a dedicated JSONB column. Decision per
  training run.

### Hot-swap mid-event

Hot-swapping the active model during a live event is permitted but
discouraged. Safety rules if you do it anyway:

1. The new model MUST have been validated against the same fixture as the
   previous active model.
2. The hot-swap window MUST be a heat boundary, never mid-heat. A
   competitor predicted by model A in heat 1 and model B in heat 2 is
   acceptable. A competitor predicted by model A for the start sheet then
   re-predicted by model B for the same heat's mark assignment is NOT.
3. Operator MUST log the hot-swap in `model_versions.notes` on the new
   row, including the show name, the heat number after which the swap
   happened, and the rationale.

If any of these can't be guaranteed, defer the swap to the next training
window.

## 3. Calibration drift detection

Calibration runs immediately after every retraining, against a held-out
slice of the training data. The result is stored in `calibration_tables`
linked by `model_version_id`. This PR establishes the schema; the drift
detection logic itself lands in a follow-on PR.

When implemented, drift detection will:

1. Compare a rolling window of recent residuals (from `prediction_residuals`)
   against the residual distribution captured in `calibration_tables.holdout_residuals`.
2. Surface an alert when:
   - Mean residual shifts by more than 1 second.
   - Variance shifts by more than 30%.
   - Conformal interval coverage at 90% drops below 0.85 or above 0.95.
3. Drift alerts are advisory, not auto-action. They prompt the operator
   to consider an early retraining; they do not retrain or deactivate
   the model automatically.

Calibration methods supported (matches the DB CHECK constraint):
`conformal_prediction`, `platt`, `isotonic`, `uncertainty_toolbox`.
Default for the XGBoost ensemble: `conformal_prediction`.

## 4. Feature store policy

Features are persisted at PREDICTION time, not at training time. Reason:
feature engineering may change between training and prediction (a new
species multiplier added to the wood table, a recalibrated diameter
exponent, an LLM that adjusted the quality multiplier). The
`feature_store` row records what the model actually saw at the moment
of prediction.

Implications:

- A `feature_store` row is created or upserted by `store_features()` on
  every prediction. The UNIQUE constraint on
  `(model_version_id, competitor_id, event_code)` guarantees one row per
  model+competitor+event triple; later predictions for the same triple
  REPLACE the prior feature vector. Rationale: the most recent feature
  vector for a competitor is the most actionable for retrospective
  debugging. Historical feature vectors live in the `predictions` table's
  notes column or in archival snapshots, NOT in `feature_store`.
- During training, `feature_store` is consulted as a sanity check (do
  the features the new model wants to use actually look like what the
  old model was using on production traffic?), not as the training
  source of truth. Training pulls features fresh from the cascade.
- Feature schema changes (adding a feature, removing a feature, renaming
  a feature) are MAJOR events. They require:
  1. A new `model_versions` row (the old model can't consume new features).
  2. Documentation in this policy doc of what changed and why.
  3. Old `feature_store` rows are retained as-is — they describe what the
     old model actually saw and stay accurate by construction.

## 5. Hot-path Supabase read circuit breaker

Today, `predict_baseline()` in `strathmark/predictor.py` wraps its call
to `get_competitor_bias()` in a try/except, and on first failure sets a
module-level flag (`predict_baseline._supabase_bias_unavailable`) that
disables bias correction for the rest of the process. This was the right
behavior when Supabase was a cross-project dependency that might be
genuinely unreachable. Under the controlled-write reframe, where
STRATHMARK Supabase becomes a local-ish hydrated cache, the
session-level disable is too aggressive: a transient network blip
during one prediction shouldn't permanently degrade every later
prediction in the same process.

New policy (implementation in a follow-on PR):

1. On `get_competitor_bias()` failure, log a warning AND retry on the
   next prediction. No session-level disable.
2. After 3 consecutive failures within a 60-second window, surface a
   single warning telemetry event (not log spam) and fall back to
   in-memory bias state for the remainder of the window.
3. After 60 seconds with no further attempts, the circuit resets and
   the next call attempts Supabase again.
4. Failures are counted per-process, not per-session. Process restart
   resets the counter.

Until that PR lands, the existing circuit breaker stays. This document
captures the target behavior so the next implementer doesn't have to
re-derive the policy.

## 6. The non-blocking guarantee

STRATHMARK MUST remain non-blocking on the prediction hot path. A
database outage MUST NEVER affect live show operations. This is a
standing rule that supersedes any other consideration in this document.

Implications for the new ML state writes:

- `record_prediction()`: best-effort. Returns None on Supabase failure;
  does not raise. Caller treats None as "prediction was made but not
  logged" and continues. Caller MUST NOT depend on the returned
  `prediction_id` being non-None for the prediction itself to be valid.
- `settle_prediction()`: best-effort. Returns None on Supabase failure
  or when the prediction row is missing; does not raise.
- `store_features()`: best-effort by convention but currently does raise
  on failure (it's an upsert and the supabase-py error path is opaque).
  Wrap calls in try/except at the caller until this is hardened in a
  follow-on PR.
- `record_calibration()` and `register_model_version()` and
  `set_active_model()`: NOT best-effort. These are off-hot-path
  operator actions. They MUST raise on failure so the operator knows
  the training output didn't persist. The operator can retry.
- `record_prediction_residuals()`: already wraps in try/except;
  preserves best-effort semantics.

Architectural rule: any write to ML state tables called from inside a
prediction code path MUST be best-effort (try/except + None on failure).
Any write called from outside a prediction code path (training, manual
operator action, post-event settlement) MUST raise on failure so the
operator can react.

A future implementation may queue best-effort writes for retry rather
than dropping them. That queueing layer is out of scope here. Current
behavior: drop on failure, log a warning.

## 7. Implementation status (updated 2026-05-04, scope expansion)

The following items were originally listed as deferred. The scope-expansion
PR landed them. Updated status:

- **Drift detection** — IMPLEMENTED. See `strathmark/drift.py`. The detection
  thresholds and rules described in section 3 above are live. Operators
  call `evaluate_drift(model_version_id, lookback_days=30)` to get a
  `DriftReport`. Auto-action is still NOT done — alerts remain advisory.
- **Hot-path circuit breaker** — IMPLEMENTED. See `_BiasCircuitBreaker` in
  `strathmark/predictor.py`. Per-process counter, 60-second sliding window,
  3-strike threshold, auto-reset. Replaces the prior session-level disable.
- **Sync function** — IMPLEMENTED as scaffolding. See `strathmark/sync.py`.
  Three paths (`nightly_batch`, `strathex_finalization`, `manual_force_sync`)
  share one upsert core. Operates in dry-run no-op mode when
  `MNEMEX_SUPABASE_URL`/`MNEMEX_SUPABASE_KEY` are unset; activates the
  moment they are. The cron, webhook, and admin-button wiring around the
  three paths remain operator deployment tasks.
- **RLS reframe** — IMPLEMENTED as a migration. See
  `strathmark/migrations/20260504_003_rls_reframe.sql`. Applies the
  controlled-write rule. Requires the `mnemex_sync` Postgres role to be
  created in the Supabase dashboard before application; the migration
  file documents the pre-flight steps.
- **`register_competitor` rewrite** — IMPLEMENTED. See
  `strathmark/db.py` `register_competitor()`. Routes through MNEMEX when
  configured; falls back to legacy STRATHMARK-local mint with a deprecation
  warning when MNEMEX is not yet configured. Has `wait_for_sync` for
  callers that need the local cache row before returning.
- **Re-keying script** — IMPLEMENTED. See
  `scripts/rekey_against_mnemex.py`. One-shot, idempotent. Targets the 95%
  match rate; orphans below threshold block commit unless `--force`.
- **MNEMEX client** — NEW. See `strathmark/mnemex.py`. The single
  STRATHMARK-side client for MNEMEX reads and competitor registration.

Items still deferred:

- **Specific retraining schedule** — operator decision; configured when
  the scheduler is deployed.
- **Specific calibration method per model** — training pipeline decision,
  not policy.
- **Doc rewrites of `docs/wiki/Persistence-and-Database.md` and
  `docs/solutions/architecture-decisions/dual-store-sqlite-supabase-split.md`**
  — DONE in this same scope-expansion PR.
- **Auto-action on drift alert** — explicitly NOT done. Drift remains
  advisory by design (see section 1).
- **Operational deployment of the sync function as a cron, webhook, or
  admin button** — code is ready; deployment is operator infrastructure
  work outside this repo.

## 8. Cross-references

- Schema reality: [docs/schema-reality-2026-05-04.md](schema-reality-2026-05-04.md)
- Migration files:
  - [strathmark/migrations/20260504_001_add_source_tracking.sql](../strathmark/migrations/20260504_001_add_source_tracking.sql)
  - [strathmark/migrations/20260504_002_ml_state_tables.sql](../strathmark/migrations/20260504_002_ml_state_tables.sql)
- Open ML research questions: [docs/ml-research-questions.md](ml-research-questions.md)
- Cleanup audit: [docs/cleanup-candidates-2026-05-04.md](cleanup-candidates-2026-05-04.md)
