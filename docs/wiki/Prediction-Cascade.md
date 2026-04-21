# Prediction Cascade

The cascade is the heart of STRATHMARK. Given one competitor, one wood
profile, and one event code, it tries to produce a predicted time by
asking five sources in order of reliability, stopping at the first one
that succeeds.

```
Priority 1 — Manual override             (operator-supplied time)
Priority 2 — LLM quality adjustment      (Ollama + Gemini fallback)
Priority 3 — ML model                    (XGBoost on 27 features)
Priority 4 — Weighted baseline           (time-decay historical average)
Priority 5 — Panel mark fallback         (division-based default)
```

Every level returns a `PredictionResult` with:

- `value` — predicted time in seconds
- `confidence` — `VERY HIGH`, `HIGH`, `MEDIUM`, `LOW`, or `VERY LOW`
- `method` — which cascade level produced the result
- `explanation` — plain-text reasoning shown next to the mark

## Priority 1 — Manual override

Operators can pin a time directly:

```python
calc.calculate(
    competitors,
    wood,
    "SB",
    manual_overrides={"Alice Smith": 28.5},
)
```

Or set `CompetitorRecord.manual_time_override = 28.5` once on the
record. Either path skips every subsequent level. Confidence is set to
`VERY HIGH` and the explanation records that the value is operator-set.

**When to use:** a competitor is visibly injured, the historical data
is known-bad, or there is a local convention (e.g. a guest competitor
assigned a mark by the chief steward).

## Priority 2 — Same-tournament weighting (graduated)

When the competitor has one or more actual times from earlier rounds of
the *same* tournament on the *same* wood, `predictor.predict_baseline`
weights those times against the decay-weighted historical average:

| Completed rounds | Same-tournament weight | Historical weight |
|------------------|------------------------|-------------------|
| 1                | 65 %                   | 35 %              |
| 2                | 80 %                   | 20 %              |
| 3                | 90 %                   | 10 %              |
| 4+               | 97 %                   | 3 %               |

The 97 % case is the design target. After four rounds on identical
wood, the historical data stops mattering — today's wood is what
matters.

Confidence is upgraded to `VERY HIGH` on any cascade level that consumes
the tournament-weighted baseline.

## Priority 3 — LLM quality adjustment

The LLM tier does not predict a time in isolation. It takes the
weighted baseline as input and produces a quality multiplier that
adjusts for today's wood:

- **Input prompt:** species, Janka hardness, specific gravity, shear
  strength, MOR, MOE, diameter, quality rating (1–10), competitor's
  recent performance context.
- **Output:** a float in `[0.85, 1.15]` and a one-line explanation.
- **Applied as:** `predicted_time = weighted_baseline * multiplier`.

Bounds are clamped in `predictor.py`:
`LLMConfig.QUALITY_MULTIPLIER_MIN = 0.85`,
`LLMConfig.QUALITY_MULTIPLIER_MAX = 1.15`.

If quality is exactly 5 (average reference), the tier returns `1.0`
immediately without calling the model — a sanity-preserving short
circuit.

**Race-day fallback chain:**

1. Ollama on the event laptop (target model: `qwen3.5:9b` at Q4\_K\_M,
   ~6.6 GB on disk, fits in 8 GB VRAM).
2. Gemini cloud (`gemini-2.0-flash-lite`), invoked only when
   `GEMINI_API_KEY` is set and Ollama returns `None`.
3. Fall through to Priority 4.

Timeouts are aggressive: 3 s TCP connect, 15 s read, zero retries. This
is a race-day setting — retrying Ollama on a dead laptop wastes an
entire heat.

**When skipped:** `OLLAMA_HOST=""` or `OLLAMA_HOST=disabled` environment
variable, no Ollama reachable, no Gemini key, or the response did not
parse cleanly.

## Priority 4 — ML model (XGBoost)

The ML tier trains lazily on the first call to `calculate()` when
`results_df` is present. Two separate models are trained — one for SB,
one for UH — because the diameter scaling and species multipliers
differ.

### Training gate

- Total records: 100 minimum (`MIN_ML_TRAINING_RECORDS_TOTAL`).
- Per event: 75 minimum (`MIN_ML_TRAINING_RECORDS_PER_EVENT`).
- Sample weights: exponential time-decay (same half-life as the baseline
  — 365 / 730 / 1095 days depending on activity).

If training is refused, the cascade silently drops to Priority 5.

### Features (27)

The feature set is in `config.py → MLConfig.FEATURE_NAMES`. Grouped:

- **Competitor ability (temporal, leak-free):** `comp_weighted_avg`,
  `comp_count`, `comp_std`, `comp_best`, `comp_recent`, `comp_trend`,
  `comp_cross_event_avg`, `days_since_last`, `size_deviation`.
- **Event and competitor attributes:** `event_encoded` (0 = SB,
  1 = UH), `gender_encoded` (0 = F, 1 = M).
- **Wood properties:** `janka_hard`, `spec_gravity`, `crush_strength`,
  `shear`, `MOR`, `MOE`, `species_mult`.
- **Block size:** `size_mm`, `size_mm_sq`, `log_size`.
- **Interaction features:** `event_x_size`, `species_mult_x_size`,
  `comp_avg_x_species`, `comp_avg_x_size`.
- **Seasonal:** `month_sin`, `month_cos`.

Feature importance (gain-based, log-target XGBoost):

- `comp_avg_x_species` — 26 %
- `comp_avg_x_size` — 19 %
- `gender` — 9 %
- `comp_weighted_avg` — 7 %
- `species_mult` — 5 %
- `shear` — 5 %

### Hyperparameters (Optuna-tuned, 30 trials, GroupKFold)

- `n_estimators = 292`
- `max_depth = 4`
- `learning_rate = 0.0305`
- `subsample = 0.643`
- `colsample_bytree = 0.508`
- `min_child_weight = 7`
- `reg_alpha = 0.261`
- `reg_lambda = 0.219`

### Calibration

After prediction, an isotonic regression calibrator (fitted per event
type) can correct systematic over- or under-prediction. Calibration is
active when `is_fitted` is true and the event type has a fitted
calibrator.

### Confidence

- `HIGH` — training data ≥ 80 records
- `MEDIUM` — 50 to 79
- `LOW` — 30 to 49
- If below 30 the tier is skipped entirely.

## Priority 5 — Weighted baseline

The pure-statistical fallback. Takes the competitor's historical event
results, applies exponential time-decay weighting (`decay.py`), and
produces:

```
weighted_avg = Σ (weight_i × time_i) / Σ weight_i
```

Then applies wood-based adjustments in order:

1. Species time multiplier (from `wood.get_species_time_multiplier`).
2. Diameter scaling via a power law with event-specific exponent
   (`wood.calculate_scaling_factor`). The default exponents are
   `SB = 1.8` and `UH = 2.1`, both calibrated from within-competitor
   multi-diameter pairs.
3. Quality adjustment (effective Janka `base × (1 + (quality − 5) × 0.1)`).

Confidence scales with weighted sample count:

- `VERY HIGH` — 10+ weighted samples and residual std dev < 2.5 s
- `HIGH` — 5+ weighted samples and residual std dev < 3.5 s
- `MEDIUM` — 2+ weighted samples
- `LOW` — 1 weighted sample
- `VERY LOW` — no historical data (falls through to Priority 6)

## Priority 6 — Panel mark fallback

When nothing else is available, `fallback.get_panel_mark()` returns a
division-based default time:

- Open — experienced reference competitor
- Novice — slower reference
- Junior — slower still
- Veterans — based on veterans panel mark
- Womens — separate reference

The panel mark is the only tier that always succeeds. Confidence is
`VERY LOW` and the explanation records that no historical data was
available.

## Confidence ladder at a glance

| Level     | Typical source                                                   |
|-----------|------------------------------------------------------------------|
| VERY HIGH | manual override, or same-tournament weighted at 80 % or above    |
| HIGH      | ML with 80+ training records, or baseline with 5+ weighted samples and low residual std dev |
| MEDIUM    | ML with 50–79 records, or baseline with 2+ weighted samples      |
| LOW       | ML with 30–49 records, baseline with a single sample             |
| VERY LOW  | panel mark fallback                                              |

## Why this order

Accuracy testing from the STRATHEX V5.2 deployment, replicated in
STRATHMARK:

- ML average error: ±2.1 s (when enough training data is available)
- LLM average error: ±3.4 s
- Baseline average error: ±4.8 s

ML is the best general-purpose tier, but it needs data. LLM fills the
gap when ML cannot train but historical data is still useful for
adjustment. Baseline always works with 3+ historical times. Panel is
the last-resort floor.
