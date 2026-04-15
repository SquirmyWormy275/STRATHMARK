---
type: bug
problem_type: data-integrity
severity: high
symptoms:
  - "Baseline predictions pulled toward 180s by timeout results"
  - "Species affinity and form trajectory included 180s timeouts as normal data"
  - "Competitor predicted times biased upward for anyone with a DNF/timeout in history"
tags:
  - "predictor"
  - "baseline"
  - "outliers"
  - "timeout"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Timeout Results Pollute Baseline

## Problem
Historical results at or above the 180s event time limit (DNFs, timeouts) were included in baseline averages, species affinity calculations, and form trajectory slopes. A single 180s timeout in a competitor's history could drag their predicted time from ~25s toward 50s+.

## Root Cause
Timeout filtering was applied in some code paths but not others — baseline, species affinity, and form trajectory each re-derived the filtered set, and some callers skipped the filter entirely. There was no single chokepoint enforcing "timeouts are not representative performance data."

## Solution
- `fallback.py`: centralize timeout filtering in `_standardize_results_df()` so every downstream consumer sees only valid results
- Add timeout filters in `get_event_baseline()` and historical lookups
- `predictor.py`: filter timeout results (>180s) from baseline calculation, species affinity, and form trajectory adjustments
- Regression test `test_predict_baseline_excludes_extreme_outliers` includes an actual 180s sample and asserts baseline stays <50s

## Prevention
- Any "representative performance" calculation (average, trend, affinity) MUST filter at >180s before aggregation — 180s is the event hard cap, not a real time
- Centralize data cleaning at the load/standardize layer, not in every consumer
- Regression tests for outlier handling MUST include the actual outlier value (a 180s timeout), not just a "close to the limit" value
