---
type: bug
problem_type: data_integrity
severity: high
symptoms:
  - "All time-decay weights silently defaulted to 1.0 regardless of age"
  - "2-year half-life decay was not being applied to historical results"
  - "Backtest MAE improved from 11.06s to 10.59s after fix"
tags:
  - "decay"
  - "prediction"
  - "datetime"
  - "silent-failure"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Decay Weights Silently Default to 1.0

## Problem
`calculate_performance_weight()` in `decay.py` compared a `datetime.date` against a `pandas.Timestamp` (or `datetime.datetime`), which raised a TypeError. The exception was swallowed by a broad try/except that returned a default weight of 1.0. Every historical result received full weight regardless of age, silently defeating the 2-year half-life decay.

## Root Cause
Mixed datetime types flow through the codebase: Excel loader produces `pandas.Timestamp`, `date.today()` returns `datetime.date`, and SQLite round-trips give strings. Subtracting incompatible types raises TypeError; a defensive try/except hid the bug for months.

## Solution
- `decay.py`: normalize both operands to `datetime.date` before subtraction; narrow the try/except to only catch the specific conversion failure
- `fallback.py`: fix the same pattern in the inline decay function
- `predictor.py`: fix dead-code date delta in `_apply_form_trajectory`
- `predictor.py`: normalize `pandas.Timestamp.date()` before subtraction

## Prevention
- Never use a broad `try/except: return default` around arithmetic on external/mixed types — it converts correctness bugs into silent-default bugs
- When a function has a "default return" fallback, log at WARNING level on the fallback path so silent failures surface in logs
- Backtest MAE on a known-good dataset before and after any change to decay, weighting, or time logic — silent weighting bugs don't show up in unit tests but do show up in MAE
