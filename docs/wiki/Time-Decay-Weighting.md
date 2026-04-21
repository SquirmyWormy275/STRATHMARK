# Time-Decay Weighting

Every prediction tier that consumes historical data applies exponential
time-decay so that recent performances carry more weight than old
ones. This is critical for two cases the rulebooks do not describe
numerically:

- **Aging competitors** — a 40-year-old whose results ten years ago
  were faster than today's. A straight average underestimates their
  current mark.
- **Returning competitors** — a chopper back from a three-year break.
  Fresh results should carry essentially all of the weight; old
  results are a weak prior at best.

`strathmark/decay.py` owns the math; `strathmark/predictor.py` applies
it across the cascade.

## Formula

```
weight = 0.5 ^ (days_old / half_life_days)
```

A result from today has weight 1.000. A result from one half-life ago
has weight 0.500. After several half-lives the weight drops
exponentially toward zero.

Standard half-life is **730 days (2 years)**:

| Days old | Weight |
|----------|--------|
| 0        | 1.000  |
| 365      | 0.707  |
| 730      | 0.500  |
| 1095     | 0.354  |
| 1825     | 0.177  |
| 3650     | 0.031  |

## Adaptive half-lives

The half-life adjusts with the competitor's recent activity. The
activity window is 2 years (`ACTIVITY_WINDOW_DAYS = 730`).

| Activity level | Min results in window | Half-life |
|----------------|----------------------|-----------|
| Active         | 5+                   | 365 days  |
| Moderate       | 2 to 4               | 730 days (standard) |
| Inactive       | 0 to 1               | 1095 days |

Rationale:

- **Active competitors** have enough fresh data that old results are
  noise. The 365-day half-life makes anything older than one year
  contribute less than half weight.
- **Inactive competitors** have scarce recent data. Shrinking the
  half-life would throw away their signal. The 1095-day (3-year)
  half-life preserves older data as a weak but non-zero prior.

The classification is done once per competitor per cascade call by
`decay.classify_activity_level()`.

## Where decay is applied

1. **Baseline prediction** — `predict_baseline` calls
   `compute_weighted_average()` which uses per-result decay weights.
2. **ML training sample weights** — `MLModel.train()` passes decay
   weights to XGBoost's `sample_weight` argument so recent examples
   pull harder on the gradient.
3. **Per-competitor std-dev** — `estimate_competitor_std_dev()`
   weights the sample variance the same way.

The LLM tier does *not* apply decay directly — it consumes the
already-weighted baseline and emits a multiplier. Manual overrides are
decay-agnostic by definition.

## Interaction with same-tournament weighting

Same-tournament round times (from today's competition on today's wood)
are **not** decayed. They represent the most-current form possible.
Instead, they are blended against the decayed historical average with
the graduated weights from [Prediction Cascade](Prediction-Cascade):

```
if 1 tournament round completed:
    final = 0.65 × round_time + 0.35 × decayed_historical
if 2 rounds:
    final = 0.80 × mean(rounds) + 0.20 × decayed_historical
if 3 rounds:
    final = 0.90 × mean(rounds) + 0.10 × decayed_historical
if 4+ rounds:
    final = 0.97 × mean(rounds) + 0.03 × decayed_historical
```

The 97 % cap leaves a small safety margin for data-anomaly detection —
if today's four rounds are wildly inconsistent, the weighted historical
anchor prevents the prediction from swinging to an outlier.

## Robust statistics

When the decay-weighted samples are small (< 5) and noisy, a straight
weighted mean can be dominated by a single old outlier. `decay.py`
falls back to a Median Absolute Deviation (MAD) clipping step:

1. Compute the weighted median.
2. Compute MAD = weighted median of `|x_i − median|`.
3. Clip values outside `median ± 3 × MAD / 0.6745`.
4. Recompute the weighted mean on the clipped set.

This is identical to the robust step in STRATHEX's
`baseline.py::predict_baseline_v2_hybrid`. It trades a small amount of
bias for a large amount of variance reduction when the historical set
is noisy.

## Weighted sample count and confidence

The cascade uses `sum(weights)` as an effective sample count. Anything
below a threshold dials confidence down:

- `VERY HIGH` — 10+ weighted samples and residual std dev < 2.5 s
- `HIGH` — 5+ weighted samples and residual std dev < 3.5 s
- `MEDIUM` — 2+ weighted samples
- `LOW` — 1 weighted sample
- `VERY LOW` — 0 (falls through to panel)

These thresholds come from
`BaselineConfig.CONFIDENCE_VERY_HIGH_MIN_WEIGHTED_SAMPLES` etc. in
`config.py`.

## Testing

Coverage lives in:

- `tests/test_decay.py` — formula correctness, half-life precision.
- `tests/test_decay_extended.py` — adaptive-vs-fixed weighting,
  robust MAD clipping, date-type handling.

Every weight emitted by `decay.py` is strictly in `(0, 1]`; a zero
weight is impossible by construction (exponential never reaches
zero), so no result is ever "erased".
