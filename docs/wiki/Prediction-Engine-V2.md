# Prediction Engine V2

V2 is the established prediction engine. It estimates positive cutting times from
prior results, then chooses handicap marks for the complete field. Version 2.0.1
preserves the 2.0.0 calculations.

## What affects a prediction?

The model uses stable competitor identity, event, dated cutting times, diameter,
species and the packaged timber properties. Gender is an active category, with a
separate missing category. Personal history is combined with population estimates,
so sparse history does not become an overconfident personal average.

Only valid results **before** `prediction_as_of` are admitted. Same-day, future,
undated and invalid rows are excluded. Earlier results contribute recency, bounded
trend and cross-event information. See [recency](Time-Decay-Weighting).

Quality, moisture, division, heat, lane, weather, equipment, fatigue and same-tournament
weighting do not change V2 numbers. Accepted compatibility fields are disclosed as
ignored rather than presented as measured effects. See [wood and diameter](Wood-and-Diameter-Scaling).

## What comes back?

Each result contains a predicted median time, a calibrated central 90% interval,
a separate performance standard deviation, and the calculated mark. Versions,
cutoff, warnings and degraded state explain which model and evidence were used.

The optional residual correction is inactive in the 2.0.0 release. LLM output cannot
supply numeric predictions. [Legacy keys](Prediction-Cascade) explains the five-key
compatibility view.

## How are marks chosen?

The optimizer compares whole-field sheets using 2,048 fixed simulation samples and
at most eight coordinate passes. It prioritizes equal model-implied winning chances,
then expected finish spread, departure from rounded time-gap marks, and a stable
input-order tie-break.

Marks are bounded integers from 3 to 183, preserve median ordering and include a
Mark 3. If the search fails, V2 returns a bounded rounded-gap sheet and exposes the
fallback. [Simulation](Variance-and-Monte-Carlo) explains the different uncertainty measures.

## Published accuracy evidence

The frozen 128-row temporal test recorded MAE of 16.1301 seconds against 20.5172 for
the strict incumbent; RMSE was 33.6904 against 44.4791. Central 90% interval coverage
was 94.53%. These figures apply to that workbook and split, not every future event.

In the source checkout, `python train_model.py` verifies the published report and
artifact without reopening the locked test. Do not rerun `--open-locked-test` for
that release. Current preview experiments use separate evidence.

## Integration

Public calculation is stateless. Authenticated ledger and shadow routes are separate
facilities for stored receipts and settlements. They are not automatically used by
STRATHEX's direct calculation. See [REST API](REST-API) and the
[full V2 specification](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/PREDICTION_ENGINE_V2.md).
