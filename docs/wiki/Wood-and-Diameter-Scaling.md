# Wood and Diameter Scaling

Every prediction in the cascade eventually has to answer three
questions about today's block:

1. How hard is it to cut through this *species*?
2. How much does the *diameter* affect cutting time?
3. How much does the *judged firmness* (quality 1–10) modify both of
   those?

All three live in `strathmark/wood.py` and share a wood-properties
table loaded from the `Wood` sheet of `woodchopping_clean.xlsx`.

## Species properties

The wood table has one row per species. Six properties are used across
the cascade:

| Property            | Symbol           | Use                                           |
|---------------------|------------------|-----------------------------------------------|
| Janka hardness      | `janka_hard`     | ML feature; baseline effective-Janka calc     |
| Specific gravity    | `spec_gravity`   | ML feature; density proxy                     |
| Shear strength      | `shear`          | ML feature (5 % importance)                   |
| Crush strength      | `crush_strength` | ML feature                                    |
| Modulus of rupture  | `MOR`            | ML feature                                    |
| Modulus of elasticity | `MOE`          | ML feature                                    |
| Empirical multiplier | `species_mult`  | Time multiplier in baseline + ML feature      |

Six properties are used simultaneously because testing showed combined
predictive power (r = 0.621) is significantly better than any single
property alone (shear alone: r = 0.523).

Default fallbacks (used when a species is not in the table):

- Janka hardness: 1690 (Eastern White Pine)
- Specific gravity: 0.34 (Eastern White Pine)

## Diameter scaling

Cutting time grows roughly as a power law in diameter:

```
time_ratio = (new_diameter / old_diameter) ^ exponent
```

Event-specific exponents are calibrated from historical data:

| Event            | Default exponent | Calibrated from                            |
|------------------|------------------|--------------------------------------------|
| Standing Block   | 1.8              | n=11 within-competitor same-species pairs, median=1.26, MAE-optimised 1.8 |
| Underhand        | 2.1              | n=26 within-competitor same-species pairs, median=2.09, MAE-optimised 2.1 |
| Unknown event    | 2.0              | midpoint (backwards-compat only)           |

The Underhand exponent is steeper because the competitor cuts the full
cross-section from above; Standing Block is cut from both sides and
scales more shallowly.

When enough multi-diameter data exists for a specific event, the
exponent is re-calibrated via `calibrate_scaling_exponent()` and the
event-specific value wins over the default. When calibration data is
insufficient, the default applies.

### Per-competitor scaling

Elite competitors sometimes scale differently from the crowd — a
chopper with a long axe may scale at 1.6 while the median competitor
scales at 1.9. `CompetitorRecord.personal_scaling_exponent` caches a
per-competitor exponent fitted from their own multi-diameter history
(requires 3+ results across 2+ distinct diameters). When the cache
hits, the competitor's personal exponent wins over the event default.

### Tolerance

Diameter differences smaller than `DIAMETER_TOLERANCE = 10 mm` return
a scaling factor of 1.0 (no adjustment). This preserves numerical
stability — a 297 mm block is indistinguishable from a 303 mm one.

## Quality adjustment

Quality is a 1–10 firmness rating judged on the day:

- 1–3 — Soft or rotten wood, cuts fast. Multiplier ≈ 0.85–0.92.
- 4–6 — Average firmness. Quality 5 is the reference (no adjustment).
- 7–10 — Above average or rock-hard. Multiplier ≈ 1.05–1.15.

### Effective-Janka formula

For baseline and ML tiers, quality is folded into an effective Janka
hardness:

```
effective_janka = base_janka × (1 + (quality − 5) × 0.1)
```

So the Janka value the predictor sees swings from 0.6× to 1.5× of the
species baseline as quality moves from 1 to 10.

Worked example — Eastern White Pine (base Janka 1690):

| Quality | Multiplier | Effective Janka |
|---------|------------|-----------------|
| 2       | 1.3        | 2197            |
| 5       | 1.0        | 1690            |
| 8       | 0.7        | 1183            |

The firm-quality-2 White Pine approaches Ponderosa Pine (base Janka
2050); the soft-quality-8 White Pine drops to something closer to
balsa. That is the design target — real wood varies.

### LLM quality multiplier

The LLM tier takes quality as a direct input and emits a multiplier
in `[0.85, 1.15]` bounded by
`LLMConfig.QUALITY_MULTIPLIER_MIN/MAX`. The multiplier is applied to
the already-scaled baseline:

```
predicted_time = weighted_baseline × species_mult × diameter_factor × llm_multiplier
```

When quality is exactly 5 the tier short-circuits and returns `1.0`
without calling the model — a sanity-preserving optimisation.

## QAA triangular interpolation (STRATHEX port)

The QAA rulebook publishes three hardness-class tables (softwood,
medium, hardwood) and leaves it to the handicapper to pick the right
one. STRATHEX introduced (and STRATHMARK inherits through the baseline
cascade) a *triangular membership* blend that picks a soft / medium /
hard proportion based on effective Janka:

- Softwood peak: ≈ 290 lbf (= 1300 in the internal units).
- Medium peak: ≈ 450 lbf (= 2000).
- Hardwood peak: ≈ 630 lbf (= 2800).
- Transition zones: 700-unit overlap for smooth blending.

Example blends (from STRATHEX `QAA_INTERPOLATION_IMPLEMENTATION.md`):

| Wood              | Effective Janka | Soft | Med  | Hard |
|-------------------|-----------------|------|------|------|
| Soft White Pine   | 1183            | 100% |   0% |   0% |
| Avg White Pine    | 1690            |  44% |  56% |   0% |
| Firm White Pine   | 2197            |   0% |  84% |  16% |
| Avg Ponderosa     | 2050            |   0% | 100% |   0% |
| Firm Ponderosa    | 2665            |   0% |   6% |  94% |
| Avg Alder         | 2620            |   0% |  13% |  87% |

This lets STRATHMARK scale a firm White Pine the way QAA would scale
Ponderosa — the two overlap in effective firmness, and the tables
blend across the transition. Full derivation in the STRATHEX docs.

## Ordering in the baseline cascade

When `predict_baseline()` produces a predicted time for Priority 4 of
the cascade, the adjustments are applied in this order:

1. Decay-weighted historical average for the competitor.
2. Species time multiplier (`get_species_time_multiplier`).
3. Diameter scaling (`calculate_scaling_factor`, event-specific
   exponent or competitor's personal exponent if cached).
4. Effective-Janka adjustment (quality 1–10).
5. Confidence penalty (small additive margin when weighted samples < 5).

Order matters — multipliers don't commute when the baseline is
zero-centred. The order above matches STRATHEX's production
calibration.

## Validation

Tests that cover this module live in:

- `tests/test_wood.py` — species lookups, default fallbacks, basic
  scaling arithmetic.
- `tests/test_wood_extended.py` — species multiplier coverage, Janka
  hardness monotonicity, event scaling exponents.
- `tests/test_wood_boundary.py` — diameter tolerance, extreme quality
  (1 and 10), zero-range species.

Every scaling factor emitted by `wood.py` is in `(0, +∞)`; values
outside `[0.5, 2.0]` raise a sanity-check warning in the log.
