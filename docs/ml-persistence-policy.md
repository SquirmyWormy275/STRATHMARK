# Prediction Model and Persistence Policy

Status: active for STRATHMARK V2. V3 has a separate signed automated model factory and
event authority documented in [`PREDICTION_ENGINE_V3.md`](PREDICTION_ENGINE_V3.md).
V2 remains trusted production authority until explicit cutover.

This policy governs V2 artifacts, promotion, drift, trusted prediction evidence, and
failure behavior. The full mechanism is documented in
[`PREDICTION_ENGINE_V2.md`](PREDICTION_ENGINE_V2.md).

## Model lifecycle

Training, validation, calibration, registration, activation, and drift evaluation are
operator actions outside the prediction hot path. A prediction must never trigger
training or mutate an artifact.

The core is a bounded, checksummed JSON artifact. Do not persist or load the core with
pickle. Every artifact carries its engine, feature schema, canonicalization, source
checksum, model, calibration, and training-cutoff metadata. Old versions are retained
for audit and backdated compatibility.

One field request snapshots one immutable core/residual/calibration bundle. Artifact
changes occur between fields and are operationally logged; do not hot-swap a model
inside an active field.

## Evidence and promotion

Only the closed V2 allowlist may be persisted as numeric feature evidence. Names,
notes, raw request payloads, and inactive future factors are not feature-store values.
Unknown or newly accepted columns remain ignored until a versioned model change passes
prior-only temporal validation.

The dependable core must beat the strict prior-only incumbent on the prospectively
locked gate before packaging. The 2.0.0 gate required at least 1% MAE improvement and no
more than 0.5% RMSE worsening; it passed with locked n=128, MAE 16.1301 vs 20.5172 and
RMSE 33.6904 vs 44.4791. Coverage was 94.53% for the nominal 90% interval. These values
apply only to the checked-in split.

An optional CatBoost residual correction is promoted only when common rolling-origin
folds improve both MAE and RMSE by at least 1%, global 90% coverage distance worsens by
no more than two percentage points, and no eligible history-depth cohort (n>=30)
worsens MAE by more than 5%. Paired-bootstrap ties reject promotion. The 2.0.0 residual
is inactive because no candidate was frozen before the locked test.

## Locked-test governance

Normal release verification is:

```bash
python train_model.py
```

It verifies published files without scoring the locked role again. The existing
`--open-locked-test` result is single-use and must not be rerun by deleting the final
report. A later model needs a new prospectively dated manifest, frozen selection and
calibration decisions, and a new untouched locked role.

## Trusted local ledger

`PredictionLedger` is append-only SQLite at `STRATHMARK_DB_PATH` or
`~/.strathmark/results.db`. Trusted field recording requires stable competitor IDs and
a request ID. It stores:

- canonical request hash, caller/key, event, and exclusive cutoff;
- prediction ID and stable competitor ID;
- engine, core, residual, and calibration provenance;
- median, interval, source, assigned mark, ignored factors, and warnings;
- numeric allowlisted feature snapshot and optimizer metadata;
- immutable settlement revisions with actor, reason, residual, and supersession.

The raw canonical request and display names are not stored. Manual, broad-prior,
legacy-rollback, and degraded predictions are marked not training eligible.
`get_training_rows()` returns only the latest settlement revision for explicitly
eligible model-source predictions.

Exact request or settlement retries deduplicate. Reusing a caller/request key for a
changed payload conflicts. A changed settlement requires a reason and appends a new
revision; no evidence row is updated or deleted.

## Cloud mirror and access control

Local SQLite is authoritative for race-day writes. Supabase mirroring is optional and
best-effort. Migration `20260811_005_prediction_v2.sql` creates separate request,
prediction, feature, and settlement tables. Migration
`20260813_006_prediction_hash_algorithm.sql` preserves legacy request hashes as
`raw-v1` and versions new active-evidence hashes as `active-v2`. Together they force
RLS, revoke browser roles, add immutable-row triggers, and grant a single transactional
append function only to `service_role`.

Migration `20260813_007_shadow_mirror_contract.sql` adds a separate, versioned shadow
append RPC. Receipt envelopes include the immutable receipt core, its namespaced stable
identity linkage, observation fingerprint metadata, and the minimum 005/006 ledger rows
needed for foreign keys. Numeric envelopes contain only eligible settle/void revisions.
They never copy Missoula operational outcome/context history, names, free-text notes,
medical data, email addresses, or credentials. Delivery metadata binds SHA-256 to an
explicit JSON body string. The append RPC recomputes that digest with PostgreSQL
built-ins and requires the parsed body to equal the submitted envelope without delivery
metadata; there is no extension or client-only trust dependency. Equivalent retry JSON
may differ lexically, but frozen receipt-core structure, nested identity linkage, and
the exact unique ledger prediction-ID set must remain identical. All four additive
tables force RLS and reject mutation; the cloud remains a non-authoritative copy.

The embedded 005/006 ledger projection is closed before PostgreSQL converts JSON into
typed rows: request, prediction, feature, and settlement objects must contain their
complete versioned allowlists with the correct JSON types. Only the request
`hash_algorithm` key may be absent for queued legacy `raw-v1` replay. Shadow evidence
count maps are limited to 128 lowercase machine-code keys and exact nonnegative
32-bit-signed integers; Boolean, fractional, string, negative, and nested values never
cross the client boundary or pass the direct append RPC.

The service key stays on a trusted server. Public `/calculate` and `/predict` are
stateless. `/ledger/calculate` and settlement routes require the existing Bearer-token
guard. Do not mirror entries without stable competitor IDs.

## Failure policy

- Core artifact missing, invalid, incompatible, or newer than a request: return a
  labeled broad event prior with degraded warnings.
- Residual missing or invalid: use the core and report the residual warning.
- Optimizer failure: return bounded rounded median-gap marks and record the reason.
- Local ledger failure: return marks and set ledger failure state; preserve the request
  for operator recovery.
- Cloud mirror delivery: keep the committed local ledger row, report
  `recorded_cloud_pending`, and deliver from the local outbox asynchronously or with
  `flush_mirror_outbox()`.
- Training, activation, and migration failures: fail visibly to the operator; these are
  not hot-path best-effort operations.

## Drift

Drift checks use settled, stable-ID, model-source predictions and remain advisory. They
may prompt investigation or a new prospectively locked training cycle but never
auto-train, auto-activate, or silently switch engines. Forecast interval coverage and
point residual movement must be reviewed separately from simulation fairness. V2
coverage is direct containment of each settled actual time inside the interval issued
with that prediction, grouped by nominal coverage. Rows without issued bounds are
reported as unavailable rather than assigned reconstructed limits; thresholds remain
active only for the nominal 90 percent cohort.

## Temporary rollback

`STRATHMARK_PREDICTION_ENGINE=legacy` selects the deterministic baseline-only rollback
for one migration release. It does not revive numeric LLM behavior or the retired
same-tournament/quality logic. Rollback usage must be logged and followed by root-cause
analysis; V2 remains the default.
