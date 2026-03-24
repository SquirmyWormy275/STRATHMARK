# TODOS

## P1 — Must Do Before Ensemble Implementation

### TODO-001: Measure current cascade MAE (BASELINE)
**What:** Run `backtest_predictions()` on existing historical data to measure current cascade MAE, RMSE, and within-3s percentage for competitors with 3+ results.
**Why:** Without a baseline measurement, we don't know if the ensemble is the right approach. If current MAE is 1.2s, bias correction alone might suffice. If it's 4.0s, better individual methods are needed first.
**Context:** Use `backtest_predictions()` in `analytics.py` with leave-one-out methodology. Record SB and UH separately. This determines implementation strategy.
**Effort:** S (human: ~2 hours / CC: ~15 min)
**Priority:** P1
**Depends on:** Nothing
**Source:** CEO review outside voice (2026-03-23)

### TODO-002: Investigate inverse-MAE vs. logistic regression for meta-learner
**What:** Compare simple inverse-MAE weighting (`weight = 1/method_MAE`, normalized) against multinomial logistic regression for ensemble blend weights.
**Why:** Outside voice argued logistic regression predicts "which method is best," not "optimal blend weight." Inverse-MAE is simpler, no sklearn dependency, more statistically sound for <100 data points.
**Context:** Start with inverse-MAE, graduate to learned weights once ledger has 200+ entries. This also eliminates the sklearn ImportError rescue path.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P1
**Depends on:** TODO-001
**Source:** CEO review outside voice (2026-03-23)

### TODO-003: Fix prediction ledger unique key (data integrity)
**What:** Add `prediction_date` to the ledger's unique constraint to prevent cross-competition matching when `heat_id` is empty string.
**Why:** Current design key `(competitor_name, heat_id, event_code, method)` collides when heat_id is '' (the default). Results from different competitions would incorrectly match.
**Context:** Change to `(competitor_name, heat_id, event_code, method, prediction_date)` or add a `competition_id` field.
**Effort:** S (human: ~1 hour / CC: ~10 min)
**Priority:** P1
**Depends on:** Nothing
**Source:** CEO review outside voice (2026-03-23)

### TODO-004: Reconcile with existing select_best_prediction()
**What:** `predictor.py` already has `select_best_prediction()` (~line 1814) doing expected-error scoring across methods. The ensemble must either replace it, wrap it, or reconcile with it.
**Why:** Two competing method-selection systems is a maintenance nightmare.
**Context:** Read and evaluate `select_best_prediction()` before building `ensemble.py`. If it already does competent method selection, the meta-learner may only need to add blend weighting on top.
**Effort:** S (human: ~2 hours / CC: ~15 min)
**Priority:** P1
**Depends on:** Nothing
**Source:** CEO review outside voice (2026-03-23)

### TODO-010: Investigate data-driven _expected_error() as simpler alternative
**What:** Before building ensemble.py, test whether feeding actual per-method MAE data back into the existing `_expected_error()` function in `select_best_prediction()` achieves similar accuracy gains. This is a ~20-line change vs. a new module.
**Why:** The ensemble may be overengineering the solution. If data-driven method selection (without blending) gets close to the same accuracy, the simpler approach wins.
**Context:** `_expected_error()` at predictor.py:1855 uses hard-coded confidence-to-error mapping. Replace with actual MAE from the prediction ledger when available. Falls back to hard-coded values when no ledger data.
**Effort:** S (human: ~2 hours / CC: ~15 min)
**Priority:** P1
**Depends on:** TODO-001 (baseline MAE measurement)
**Source:** Eng review outside voice (2026-03-23)

### TODO-011: Audit tournament weighting in ensemble blend
**What:** Verify that 97%/3% tournament weighting is not double-counted when blending baseline + ML predictions. Baseline already applies tournament weighting; ML features also include tournament-weighted averages.
**Why:** Double-counting would distort predictions for competitors with tournament_time set — overweighting same-tournament data.
**Context:** Trace tournament_time through `predict_baseline()` and ML feature engineering. Document which methods apply tournament weighting and how the blend handles it.
**Effort:** S (human: ~2 hours / CC: ~15 min)
**Priority:** P1
**Depends on:** Nothing
**Source:** Eng review outside voice (2026-03-23)

## P2 — Implementation Notes

### TODO-005: Change get_best_prediction() execution model
**What:** Currently `get_best_prediction()` short-circuits (returns on first valid method). Ensemble requires all methods to run. Must call `get_all_predictions()` internally or restructure.
**Why:** The ensemble blend needs outputs from all available methods, not just the first successful one.
**Context:** Performance impact: always paying LLM + ML cost. Consider gating: only run all methods when ensemble has enough data to justify blending (>100 ledger entries).
**Effort:** M (human: ~1 week / CC: ~30 min)
**Priority:** P2
**Depends on:** TODO-004
**Source:** CEO review outside voice (2026-03-23)

### TODO-006: Add hook in record_result() for ledger updates
**What:** `store.py` `record_result()` is a simple INSERT with no hook mechanism. Needs to also UPDATE matching prediction rows with actual times.
**Why:** The self-improvement loop depends on prediction-vs-actual matching.
**Context:** Extend `record_result()` to run UPDATE on predictions table. Do not change the method signature (backward compatibility with STRATHEX).
**Effort:** S (human: ~2 hours / CC: ~15 min)
**Priority:** P2
**Depends on:** TODO-003 (correct key design)
**Source:** CEO review outside voice (2026-03-23)

### TODO-007: Raise meta-learner activation threshold
**What:** Change from 30 ledger entries to 100+ entries spanning 2+ distinct competition dates.
**Why:** 30 entries mid-first-competition will overfit to single competition's conditions.
**Context:** Track distinct `prediction_date` values in ledger. Require entries from at least 2 distinct dates AND 100+ total entries before activating ensemble.
**Effort:** S (human: ~1 hour / CC: ~10 min)
**Priority:** P2
**Depends on:** Nothing
**Source:** CEO review outside voice (2026-03-23)

### TODO-008: Increase shadow evaluation sample size
**What:** Increase from 20 to 50+ entries for shadow evaluation, or use a statistical significance test.
**Why:** With ±3s variance, 20 samples cannot distinguish model quality from noise.
**Context:** Consider using a paired t-test or Wilcoxon signed-rank test on old-vs-new model errors rather than raw MAE comparison.
**Effort:** S (human: ~2 hours / CC: ~10 min)
**Priority:** P2
**Depends on:** Nothing
**Source:** CEO review outside voice (2026-03-23)

### TODO-009: Re-estimate effort with all accepted scope expansions
**What:** The original Approach C was sized at "L (~1-2 hours CC)." Six scope expansions (confidence intervals, bias correction, accuracy tracking, anomaly flagging, method explanations, pairwise predictions) were accepted without resizing.
**Why:** Pairwise outcome predictions alone is another L. Total scope is now XL.
**Context:** Realistic estimate with all expansions: human ~2-3 months / CC: ~3-5 hours.
**Effort:** S (effort estimation task)
**Priority:** P2
**Depends on:** Nothing
**Source:** CEO review outside voice (2026-03-23)
