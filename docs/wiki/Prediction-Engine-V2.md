# Prediction Engine V2

STRATHMARK 2.0.0 uses one robust hierarchical log-time model, not a numeric
LLM/ML/baseline cascade. One exclusive UTC cutoff and one immutable model bundle apply
to every competitor in a field.

## Active evidence

- stable competitor identity and strictly prior dated history;
- event (`SB` or `UH`), result date, and measured positive time;
- target and historical diameter and species;
- Janka hardness, specific gravity, crush strength, shear, MOR, and MOE, with the
  canonical species lookup packaged inside the checksummed core artifact;
- gender (`M`, `F`, or missing).

Derived features include 730-day recency, history depth, partially pooled same-event
state, bounded trend, cross-event state, missing flags, and log-diameter ratio. Unknown
species use pooled property values plus a missing indicator. Same-day, future,
invalid-date, and undated rows are excluded.

## Inactive numeric inputs

Division, round/heat, venue, lane/stand, run order, exact material identity,
quality/moisture, weather, equipment, fatigue, penalty/DNF status, same-tournament
weighting, and field strength are no-ops until provenance-backed capture and temporal
validation support a future model version.

## Outputs and compatibility

The core returns a positive median and chronological split-conformal central 90%
interval. That interval describes the predictive distribution of a future finish;
`std_dev` remains a separate absolute-seconds performance summary for compatibility.
Settled drift coverage is measured against the interval actually issued for each
prediction, not reconstructed from residual quantiles.

The five legacy keys map as follows:

| Key | V2 meaning |
| --- | --- |
| `manual` | uncalibrated operator override |
| `llm` | always `None` numerically |
| `ml` | promoted optional residual, otherwise `None` |
| `baseline` | authoritative V2 core |
| `panel` | broad event-prior fallback |

The optional residual is inactive in 2.0.0. LLMs are narrative-only.

## Marks

The field optimizer uses 2,048 deterministic common-random samples and at most eight
coordinate passes. It minimizes equal-win-probability error, expected finish spread,
departure from rounded median-gap marks, then the input-order mark tuple. Marks are
bounded integers, preserve median ordering, keep equal-median input order, and include
at least one Mark 3. Failure returns the bounded rounded-gap sheet.

## Locked evidence

On the frozen 128-row temporal test: MAE was 16.1301 seconds versus 20.5172 for the
strict incumbent (21.38% lower); RMSE was 33.6904 versus 44.4791 (24.26% lower); 90%
interval coverage was 94.53%. These results apply only to that workbook and split.
Cohort samples are smaller and this is not proof of universal accuracy or real-world
fairness.

Verify published evidence without reopening the locked rows:

```bash
python train_model.py
```

Do not rerun `--open-locked-test` for this release.

## Trusted evidence

Public prediction routes are stateless. Authenticated `/ledger/calculate` requires a
request ID and stable competitor IDs, writes one local append-only SQLite transaction,
and mirrors to Supabase only best-effort through a replayable local outbox. Each ledger
process uses one bounded worker, which reclaims overflowed and restart-surviving rows
from that durable outbox. `GET /health?prediction_as_of=YYYY-MM-DD` evaluates artifact
compatibility for a historical exclusive cutoff.
Settlements are immutable revisions. The ledger stores hashes, stable IDs, versions,
numeric allowlisted features, predictions,
marks, and settlement provenance—not names, notes, or raw bodies. Migrations 005-006
force RLS, restrict the append RPC to `service_role`, and preserve versioned request
hash compatibility without rewriting old evidence.

For the complete source-controlled contract, see
[`docs/PREDICTION_ENGINE_V2.md`](../PREDICTION_ENGINE_V2.md).
