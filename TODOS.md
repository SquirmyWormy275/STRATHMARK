# TODOs

Prediction Engine V2 superseded the pre-2.0 numeric cascade and the proposed
ensemble backlog. The historical design discussion remains available under
`docs/solutions/architecture-decisions/ensemble-predictor-design-decisions.md`,
but TODO-001 through TODO-011 are closed and must not be treated as active work.

## Active follow-up: tournament-management evidence

Division, round/heat, venue, lane/stand, run order, exact material identity, wood
quality/moisture, weather, equipment, rest/fatigue, and penalty/DNF state remain
numeric no-ops. Future tournament-management software may activate them only after
it captures provenance-backed values over multiple seasons and a new model version
passes prospectively frozen temporal validation.

## Active follow-up: operational evidence

- Apply `strathmark/migrations/20260811_005_prediction_v2.sql` followed by
  `strathmark/migrations/20260813_006_prediction_hash_algorithm.sql`, then
  `strathmark/migrations/20260813_007_shadow_mirror_contract.sql` to production only
  through a separately authorized deployment. First rehearse the exact sequence and
  guarded rollback/refusal behavior against disposable PostgreSQL. Local and CI
  validation never apply them to a live database.
- Design and approve a durable mirror-outbox lifecycle before claiming finite storage:
  archive/compaction rules, a capacity policy that cannot discard undelivered evidence,
  and explicit terminal/permanent-failure classification plus operator recovery. The
  current append-only queue has bounded scans, replay batches, and request concurrency,
  but intentionally has no destructive hard cap and reports unavailable delivery as
  retryable failure.
- Accumulate trusted, settled V2 predictions before considering a residual learner,
  new drift thresholds, or changes to the mark objective. Manual, degraded,
  broad-prior, and provenance-incomplete rows are not training evidence.
- A future accuracy claim requires a new dated manifest, pre-lock record, untouched
  future test role, final report, and versioned artifact. The published locked role
  is never reopened for tuning.

## Closed pre-2.0 backlog

- Cascade MAE measurement and inverse-MAE/logistic ensemble experiments: replaced by
  the frozen prior-only V2 comparison and strict residual-promotion gate.
- Name/date-based prediction ledger key: replaced by stable competitor, request, and
  prediction IDs in the append-only V2 ledger.
- `select_best_prediction()` reconciliation and all-method execution: numeric LLM and
  legacy ensemble selection are retired; V2 uses manual override, promoted residual,
  core, then labeled broad fallback.
- Same-tournament weighting audit: closed. The retired 65/80/90/97 percent blend is
  inactive because current data cannot prove round or tournament provenance.
- Result-hook settlement: replaced by explicit settlement using `prediction_id` and
  immutable correction revisions.
- Ensemble activation/shadow sample/effort tasks: superseded by prospective V2
  validation, the trusted ledger, and the no-hot-path-training boundary.
