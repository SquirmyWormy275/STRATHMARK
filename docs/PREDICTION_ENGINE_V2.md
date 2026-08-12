# Prediction Engine V2

STRATHMARK 2.0.0 replaces the numeric prediction cascade with one reproducible,
prior-only prediction system. It predicts positive finish times, reports calibrated
forecast intervals, assigns integer marks from the field's joint predictive
distributions, and can record trusted predictions in an append-only ledger.

This page is the canonical description of the active mechanism. Older cascade and
same-tournament documents are retained only as historical decisions and are marked
superseded.

## What the engine uses

The numeric model has a closed allowlist. Adding a request field or accepting a legacy
argument does not make that field model evidence.

Active request and history evidence is:

- stable competitor identity, where available;
- the competitor's strictly prior dated history;
- event (`SB` or `UH`);
- result date and one exclusive UTC `prediction_as_of` cutoff;
- target and historical diameter;
- target and historical species;
- six species properties joined by species: Janka hardness, specific gravity, crush
  strength, shear strength, modulus of rupture, and modulus of elasticity;
- immutable competitor gender (`M`, `F`, or explicitly missing).

The model derives prior-only history depth, 730-day exponential recency weight,
same-event residual state, a bounded recent trend, cross-event state, missing/support
flags, and a clamped log-diameter ratio. Unknown species use pooled physical-property
values and a missing-species indicator; they are not relabeled as a known species.

V2 currently ignores these unverified factors as numeric no-ops:

- division;
- round or heat;
- venue;
- lane or stand;
- run order;
- exact log, block, or batch identity;
- wood quality or moisture;
- weather;
- equipment;
- rest or fatigue;
- penalty or DNF status;
- same-tournament result weighting;
- field strength.

These factors remain deferred until tournament-management software records them with
stable provenance and enough out-of-time data exists to validate them. Notes are not
parsed to infer a DNF, penalty, or timeout.

## Causal evidence boundary

Every request resolves `prediction_as_of` once as an exclusive UTC date. Only results
with `result_date < prediction_as_of` are eligible. Same-day, future, invalid-date, and
undated rows are excluded. Time must be positive and finite, event must be `SB` or `UH`,
diameter must be positive and finite, and a stable competitor ID is required for
population training evidence and trusted persistence.

This boundary is shared by training and inference. A backdated request will not load a
model trained at or after its cutoff. If no compatible model snapshot is available, the
request degrades to a labeled broad event prior instead of using future information.

## Statistical mechanism

The dependable core is a robust hierarchical model of log finish time implemented with
NumPy and Pandas. It combines:

- event-specific intercepts and diameter slopes;
- species physical properties and explicit missing indicators;
- gender, including a missing category;
- robust Huber/ridge population fitting;
- exponentially recency-weighted competitor-event state;
- partial pooling that shrinks sparse competitors toward supported population priors;
- bounded trend and learned cross-event borrowing;
- robust residual scale and positive-support lognormal prediction.

Zero-history competitors receive a wide conditional population prior. Sparse histories
are pooled instead of being treated as fully reliable, and the influence of personal
history grows smoothly with support.

Chronological split-conformal calibration produces the central 90% interval. It pools
residuals by event and history-depth band when adequately supported, then event, then
global. The response states the nominal coverage, calibration state, and pooling scope.

The forecast interval answers, "How uncertain is this predicted finish time?" The
existing `std_dev` field answers, "How much might this competitor's race performance
vary in simulation?" They are deliberately separate and must not be substituted for
one another.

## Optional residual learner

CatBoost may be installed through the `ml` extra, but it is not automatically active.
A residual artifact is promoted only after frozen rolling-origin comparisons show at
least 1% improvement in both MAE and RMSE, no material loss of 90% interval calibration,
and no eligible history-depth cohort MAE regression greater than 5%. Eligible cohorts
need at least 30 examples. Ties or incomplete evidence reject promotion.

The 2.0.0 packaged release has no promoted residual candidate. The residual component is
therefore inactive and the hierarchical core is authoritative.

## Manual overrides, LLMs, and compatibility keys

A manual time is an operator instruction, not a calibrated model estimate. It is tagged
`manual`, carries no model interval, and is excluded from training rows.

LLMs may still produce narrative commentary or fairness explanations. They cannot
generate, select, or modify numeric predictions or marks. Passing the legacy
`llm_client` argument is a numeric no-op.

`get_all_predictions()` retains its five keys for compatibility:

| Legacy key | V2 meaning |
| --- | --- |
| `manual` | operator override, when supplied |
| `llm` | always `None` for numeric prediction |
| `ml` | promoted optional residual prediction, otherwise `None` |
| `baseline` | authoritative V2 hierarchical core |
| `panel` | static broad event prior used for degraded fallback |

Selection is `manual`, then promoted `ml`, then `baseline`, then `panel`. This mapping is
not an ensemble of independent opinions: `ml`, when present, is a validated residual
correction to the same core.

## Joint mark optimizer

For a field calculation, STRATHMARK snapshots one immutable model bundle and uses it for
every competitor. It then draws 2,048 deterministic common-random-number samples from
the competitors' positive-support predictive distributions. The coordinate search uses
at most eight fixed passes and compares candidate mark sheets lexicographically:

1. squared deviation from equal model-implied win probability;
2. expected finish spread;
3. departure from the legacy rounded median-gap marks;
4. the input-order mark tuple as the deterministic tie-breaker.

Marks are integers between 3 and the effective ceiling, at least one competitor receives
Mark 3, and a faster median cannot receive an earlier start than a slower median. Equal
medians retain caller input order. A one-competitor field receives Mark 3.

The optimizer result is accepted only when it is not worse than the legacy sheet under
that objective. Invalid posterior data or any search failure uses the bounded fallback:

```text
mark = min(effective_ceiling, max(3, 3 + round(slowest_median - competitor_median)))
```

The result exposes `optimizer` and reproducibility metadata, including sample count,
seed, passes, objectives, and fallback reason.

## Artifact loading and request consistency

The core is a checksummed JSON artifact; native pickle is not accepted. Load precedence
is:

1. `STRATHMARK_PREDICTION_CORE_ARTIFACT`;
2. local `models/prediction_v2_core.json`;
3. local `models/prediction_v2/core.json`;
4. packaged `strathmark/models/prediction_v2_core.json`;
5. labeled broad event prior.

`STRATHMARK_PREDICTION_RESIDUAL_ARTIFACT` can point at an optional residual directory.
The loader checks compatibility, cutoffs, and checksums. A field holds one immutable
`PredictionBundle`; files cannot change the model halfway through a request.

`GET /health` reports core, residual, and calibration availability and versions,
artifact source, cutoff, warnings, and degraded state. Ollama health is reported
separately because it is narrative-only.

## Locked release evidence

The checked-in frozen temporal benchmark used 128 rows dated 2025-07-01 through
2026-02-06. Selection and calibration were frozen first.

| Metric | V2 core | Strict prior-only incumbent | Change |
| --- | ---: | ---: | ---: |
| MAE | 16.1301 s | 20.5172 s | 21.38% lower |
| RMSE | 33.6904 s | 44.4791 s | 24.26% lower |
| 90% interval coverage | 94.53% | not available | descriptive |

This is evidence for this fixed workbook and temporal split, not proof of universal
accuracy or real-world equal finishes. Event and history-depth cohort results have
smaller samples; see `benchmarks/prediction_v2_report.json` for their counts and labels.
The optional residual was inactive for the locked evaluation.

The locked role was opened once. Do not delete the final report or rerun the locked
opening to improve results. Normal verification is safe and does not rescore those rows:

```bash
python train_model.py
```

`python train_model.py --prepare` and `python train_model.py --open-locked-test` are
governance operations for a new, prospectively locked benchmark only. The current tool
refuses them while the published final report exists.

## Trusted prediction ledger

Public `/calculate` and `/predict` are stateless. They do not create trusted training
evidence. Authenticated `POST /ledger/calculate` requires:

- `Authorization: Bearer <STRATHMARK_API_TOKEN>`;
- a caller-supplied `request_id` idempotency key;
- a stable `competitor_id` for every competitor.

The entire field is written atomically to local SQLite after marks are final. The ledger
stores only the canonical calculation hash, stable IDs, engine/model/calibration versions,
cutoff, median, interval, mark, source, ignored factors, numeric allowlisted feature
snapshot, warnings, and optimizer metadata. It does not persist names, narrative notes,
or the raw request payload.

An identical caller/request key returns the original prediction IDs. Reusing it for a
different canonical input or deterministic prediction output is a conflict. `POST
/ledger/predictions/{prediction_id}/settle` verifies prediction, competitor, and event;
an exact retry deduplicates, while a correction requires an actor-attributed reason and
appends a new revision that supersedes the previous settlement. Rows are immutable.
Manual-source rows are not training eligible.

SQLite at `STRATHMARK_DB_PATH` (default `~/.strathmark/results.db`) is the race-day
authority. If cloud
credentials exist, mirroring is best-effort through the service-role-only function from
migration `20260811_005_prediction_v2.sql`. Browser roles have no ledger access and RLS
is forced. Cloud failure is reported in ledger status but never invalidates returned
marks. Local ledger write failure is also non-blocking for calculation; the response
reports that trusted recording failed.

## 2.0 migration and rollback

Existing constructors, function names, result ordering, five prediction keys, and
performance `std_dev` semantics remain compatible. New result fields have defaults.
Legacy context inputs such as division, quality, tournament results, and field strength
remain accepted for one migration release but are numeric no-ops.

To compare or temporarily roll back to the deterministic pre-V2 baseline, set:

```powershell
$env:STRATHMARK_PREDICTION_ENGINE = "legacy"
```

The rollback path still applies the exclusive cutoff, strips inactive inputs, and never
calls an LLM for a number. Remove the variable to return to V2. It is a temporary
one-release compatibility tool, not a long-term alternate engine.

## Related references

- `benchmarks/prediction_v2_report.md` and `.json` — locked evidence
- `strathmark/models/prediction_v2_core.json` — packaged safe artifact
- `strathmark/features.py` — causal evidence boundary
- `strathmark/prediction_v2.py` — core and calibration
- `strathmark/mark_optimizer.py` — deterministic joint marks
- `strathmark/ledger.py` — local trusted ledger
- `strathmark/migrations/20260811_005_prediction_v2.sql` — optional cloud mirror
- `docs/DEPLOYMENT.md` — operator runbook
