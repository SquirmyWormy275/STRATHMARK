# Architecture Overview

STRATHMARK is organised around a single public entry point
(`HandicapCalculator`) and a cascade of prediction modules that feed it.
This page walks through the modules in dependency order — reading in
this order is the fastest way to understand the code base.

## Top-level layout

```
strathmark/
    __init__.py         Public API (HandicapCalculator, CompetitorRecord,
                        WoodProfile, ResultStore, and re-exports)
    calculator.py       Mark computation, gap logic, start-sheet rendering
    predictor.py        Prediction cascade orchestration + CompetitorRecord,
                        WoodProfile, HistoricalResult, MLModel classes
    variance.py         Absolute variance model, Monte Carlo simulation
    wood.py             Species hardness lookup, diameter scaling, quality
                        adjustment
    decay.py            Exponential time-decay weighting (2-year half-life)
    fallback.py         Panel marks and event baseline fallbacks
    config.py           All constants as frozen dataclasses
    store.py            SQLite local result store (~/.strathmark/results.db)
    db.py               Supabase/PostgreSQL backend (push/pull results)
    loader.py           Excel workbook loader (woodchopping_clean.xlsx)
    utils.py            Column standardisation, prediction accuracy scoring
    analytics.py        Backtesting, competitor profiling, performance history
    fairness.py         AI-assisted fairness assessment
    visualization.py    Plain-text simulation summaries, ASCII bar charts
    llm.py              Ollama + Gemini connection management and prompt
                        execution
    llm_roles.py        Extended LLM roles (profiles, commentary, anomaly
                        detection)
    api.py              FastAPI REST API (calculate, predict, simulate,
                        results)
```

## Dependency graph

Everything flows into `calculator.py`. The arrows point from the module
that imports to the module being imported.

```
                      +-----------------+
                      |   config.py     |  (imported by everything)
                      +-----------------+
                              ^
                              |
   +------+      +--------+   |   +------------+      +----------+
   | wood | <--  |        |   |   |  decay     | <--  | fallback |
   +------+      |        |   |   +------------+      +----------+
                 |        |---+          ^                  ^
                 | pred-  |              |                  |
                 | ictor  |--------------+                  |
                 |        |-----------------------------------
                 +--------+
                      ^
                      |
             +--------+---------+
             |                  |
   +-----------+         +------------+
   | calculator|-------->| variance   |
   +-----------+         +------------+
       ^                      ^
       |                      |
   +-------+             +-----------+
   | api.py|             | fairness  |
   +-------+             +-----------+
```

- `config.py` is a dependency of nearly every module and imports nothing
  from STRATHMARK itself.
- `predictor.py` is the cascade orchestrator — it pulls from `decay`,
  `fallback`, `wood`, `llm`, and the `MLModel` class it defines.
- `calculator.py` is the public surface. It knows nothing about
  species, decay, LLMs, or the database. It only knows how to turn
  predicted times into marks.
- `variance.py` consumes `MarkResult.to_simulation_dict()` — it only
  needs `predicted_time`, `mark`, and `std_dev` per competitor.

## Lifecycle of a single call

Given one call to `calc.calculate(competitors, wood, event_code)`:

1. `HandicapCalculator.calculate` validates `event_code` is SB or UH.
2. If an ML model has not been trained yet and `results_df` was
   provided, `predictor.MLModel.train()` fires once — all subsequent
   calls reuse the cached model.
3. For each competitor, `predictor.get_best_prediction()` runs the
   cascade:
   - Manual override (from `manual_overrides` dict or
     `record.manual_time_override`).
   - Same-tournament time (graduated 65 / 80 / 90 / 97 % weight).
   - LLM quality adjustment on top of the weighted baseline (Ollama
     first; Gemini cloud as a fail-fast fallback).
   - XGBoost prediction from the 27-feature vector.
   - Weighted baseline from `decay.compute_weighted_average()`.
   - Panel mark fallback from `fallback.get_panel_mark()`.
4. Per-competitor standard deviation is computed:
   - 3 or more historical times for the event → clamped sample
     std dev.
   - fewer than 3 → `predicted_time * 0.12`, clamped to `[1.5, 15.0]`.
5. `_assign_marks()` runs the gap logic: `mark = 3 + round(slowest −
   predicted_time)`, then `min(mark, effective_ceiling)`.
6. `MarkResult` list is returned, sorted front marker to back marker.

## Public vs internal API

**Public** (stable, re-exported from `strathmark.__init__`):

- `HandicapCalculator`, `process_competition_day`
- `CompetitorRecord`, `HistoricalResult`, `WoodProfile`,
  `PredictionResult`
- `get_best_prediction`, `get_all_predictions`,
  `select_best_prediction`, `predict_baseline`
- `ResultStore`
- `run_monte_carlo_simulation`, `estimate_competitor_std_dev`,
  `audit_mark_sheet`, `quick_fairness_check`
- `generate_simulation_summary`, `visualize_simulation_results`
- `get_ai_assessment_of_handicaps`,
  `get_championship_race_analysis`, `simulate_and_assess_handicaps`
- `call_ollama`, `check_ollama_connection`
- `load_woodchopping_xlsx`, `load_results_for_competitor`
- `push_results`, `push_results_dicts`, `pull_results`,
  `push_competitors`, `pull_competitors`, `register_competitor`,
  `format_proam_results`, `record_prediction_residuals`,
  `get_competitor_bias`
- `score_prediction_accuracy`

**Internal** (underscore-prefixed, may change without notice):

- `_standardize_results_df`
- `_pooled_std_dev_by_event`
- `_env_int`, `_env_str`
- `_assign_marks`

## Design invariants in code

Every invariant from [Home](Home) is enforced at exactly one place, so
bypassing the rule is not possible through normal imports:

| Invariant | File | Where it lives |
|-----------|------|----------------|
| Mark floor = 3 | `calculator.py` | `HandicapCalculator.MARK_FLOOR = 3` |
| Mark ceiling = 183 | `calculator.py` | `HandicapCalculator.MARK_CEILING = 183` |
| Gap logic | `calculator.py` | `_assign_marks()` body |
| Absolute variance only | `variance.py` | module-level constants and doc string |
| Prediction cascade | `predictor.py` | `get_best_prediction()` body |
| Time-decay half-life | `decay.py` / `config.py` | `DecayConfig.HALF_LIFE_MODERATE_DAYS = 730` |
| 97 % same-tournament | `predictor.py` | graduated weighting in `predict_baseline()` |
| Plain text output | `calculator.py` | `StartSheet.render()` uses only ASCII |

## Cross-reference to STRATHEX

STRATHMARK was extracted from STRATHEX module-by-module. The mapping is
captured verbatim in `README.md`:

| STRATHEX file | STRATHMARK module |
|---------------|-------------------|
| `woodchopping/handicaps/calculator.py` | `calculator.py` |
| `woodchopping/predictions/baseline.py` | `predictor.py + decay.py` |
| `woodchopping/predictions/prediction_aggregator.py` | `predictor.py` |
| `woodchopping/predictions/ai_predictor.py` | `predictor.py` (LLM level) + `llm.py` |
| `woodchopping/predictions/ml_model.py` | `predictor.py` (ML level) |
| `woodchopping/predictions/diameter_scaling.py` | `wood.py` |
| `woodchopping/simulation/monte_carlo.py` | `variance.py` |
| `woodchopping/predictions/calibration.py` | `predictor.py` |
| `woodchopping/data/preprocessing.py` | `predictor.py` (ML feature engineering) |
| `config.py` | `config.py` |

Any STRATHEX contributor who knows their way around the old paths can
move to STRATHMARK without relearning the architecture — the file names
were chosen to make the port self-documenting.
