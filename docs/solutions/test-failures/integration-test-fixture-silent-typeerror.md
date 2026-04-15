---
type: bug
problem_type: test_failure
severity: high
symptoms:
  - "Integration tests ran with empty competitor histories"
  - "All integration tests fell through to panel fallback instead of exercising the prediction cascade"
  - "TypeError from wrong kwarg (event= vs event_code=) was swallowed by try/except in fixture"
tags:
  - "testing"
  - "fixtures"
  - "silent-failure"
  - "integration"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Integration Test Fixture Silent TypeError

## Problem
The `six_sb_competitors` fixture built `HistoricalResult` objects with `event=...` instead of `event_code=...`. Each construction raised TypeError, which was caught by a try/except inside the fixture's loop. Every competitor ended up with an empty history, so the full integration suite ran against panel fallbacks and never actually exercised the prediction cascade. Fixing the fixture exposed a downstream bug in `_apply_form_trajectory` where `datetime.date - pandas.Timestamp` raised a second silent TypeError.

## Root Cause
- Parameter rename (`event` → `event_code`) never propagated to the test fixture
- The fixture wrapped each `HistoricalResult(...)` construction in a broad try/except that swallowed TypeError
- Tests still "passed" because they asserted on panel-fallback behavior, which is valid even with empty histories

## Solution
- Fix the kwarg: `event_code=str(row.get('event', 'SB')).upper()`
- Normalize `pandas.Timestamp` to `date` via `.date()` before subtraction in `_apply_form_trajectory`
- Update ceiling assertion from 180 → 183 (system ceiling is 180s time limit + 3s minimum mark)

## Prevention
- Test fixtures MUST NOT use try/except around object construction — a failed fixture should fail loud
- When renaming a parameter, grep tests/ for the old name; kwargs are invisible to most refactor tools
- Integration tests should assert on something the cascade actually produces (a predicted time, a non-panel PredictionResult) — asserting only on invariants like "mark ≥ 3" passes even when the cascade never ran
