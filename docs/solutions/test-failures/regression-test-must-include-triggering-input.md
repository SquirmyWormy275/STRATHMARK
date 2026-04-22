---
type: knowledge
problem_type: test_failure
severity: high
tags:
  - "regression-tests"
  - "adversarial-review"
  - "test-coverage"
  - "codex-findings"
confidence: high
created: 2026-04-21
source: "Codex adversarial review of test/comprehensive-test-suite PR (Mar 24 2026)"
---

# Regression Test Must Include the Triggering Input

## Context
A regression test is an audit trail — it's supposed to fail if the original bug is ever reintroduced. If the test omits the exact input that triggered the bug, it will pass whether or not the fix is still in place. That is a false-coverage failure: the test suite grows, the confidence grows, but nothing actually guards the regression.

This pattern was caught twice by Codex adversarial review of the v0.3.1 comprehensive test suite PR and fixed in commit `e8f551f` ("fix: address adversarial review findings in test suite").

## Problem
Two regression tests in `tests/test_regression.py` had green runs that proved nothing:

**Timeout-filter regression** — the original bug was that 180s+ timeout results pulled baseline predictions upward. The test history contained only normal-range times (24–26s); the baseline prediction would stay well under 50s whether or not the filter was in place. The assertion `pred.value < 50.0` was always true.

**97% tournament-weighting regression** — the original bug was mis-weighting when `num_tournament_rounds >= 4` (the 97% path). The test used the default `num_tournament_rounds=1`, which exercises the 65% path. The test was green, but it was testing the wrong code path.

## What Didn't Work
- Adding more assertions to the same shape of test — no amount of tightening catches the issue if the input never reaches the code under test
- Trusting the test's name to signal what it exercises — "test_timeout_filter" said nothing about whether a timeout result was actually in the history

## Solution
Include the triggering input explicitly, and comment it as such.

Fixed form (current `tests/test_regression.py:159-168`):
```python
# Timeout result — should be clipped by robust averaging
HistoricalResult(
    event_code="SB",
    time_seconds=180.0,
    species="S01",
    diameter_mm=300,
    quality=5,
    result_date=today - timedelta(days=180),
),
```

The `# Timeout result — should be clipped` comment is load-bearing: a future refactor that deletes the 180s entry "because it looks like an outlier" would unknowingly defeat the regression. The comment tells the reviewer *why* it's there.

## Why This Works
The regression test now actually exercises the code path under test. If someone removes the outlier-clipping logic in `predict_baseline()`, the assertion `pred.value < 50.0` fails because the 180s result pulls the average above 50.

## Prevention
Writing a regression test, in order:
1. **Reproduce the bug first.** Note the exact input that triggers it — not just the output.
2. **Write the test to include that input explicitly.** Comment it as the triggering input.
3. **Dry-run the fix removal.** Temporarily revert the fix; confirm the test fails. If it still passes, the test doesn't cover the regression.
4. **Re-apply the fix.** The test should go green.
5. **Keep one class per bug** in `tests/test_regression.py`, with a docstring naming the original bug.

A lightweight mental check before commit: *if I revert the fix, does this test go red?* If the answer is "I'm not sure," dry-run it.

## Related
- `tests/test_regression.py` — all regression tests live here, one class per bug
- Commit `e8f551f` — "fix: address adversarial review findings in test suite"
- [`../best-practices/test-isolation-no-prod-db.md`](../best-practices/test-isolation-no-prod-db.md) — tests never touch prod data
