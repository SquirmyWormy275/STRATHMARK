---
type: knowledge
problem_type: architecture-decision
severity: high
tags:
  - "predictor"
  - "baseline"
  - "tournament"
  - "decay"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Tournament Weighting and Time-Decay

## Context
A competitor's most informative data is their performance in the current tournament (today's heats predict today's future heats). Historical cross-tournament data matters but is dominated by same-event data when available.

## Pattern
- **Tournament weighting**: same-tournament results receive 97% weight; all historical data receives 3%. Applied only when `num_tournament_rounds >= 4` (a valid "tournament mode" signal); otherwise blend is historical-only.
- **Time-decay**: exponential decay with 2-year half-life (730 days). `weight = 0.5 ** (age_days / 730)`.
- Baseline prediction is `0.97 * tournament_mean + 0.03 * decay_weighted_historical_mean`.

## Rationale
The 97/3 split was chosen because same-day performance correlates ~0.9 with same-day future performance, while cross-tournament correlation degrades to ~0.4–0.6 even at 1-year distances. The 2-year half-life balances "enough data to matter" against "stale physiology / equipment / technique."

Tournament weighting is GATED on `num_tournament_rounds >= 4` because with fewer rounds the tournament mean is too noisy to dominate.

## Examples
Tests: `TestTournamentWeightingRegression` in `test_predictor_regression.py`. The `test_97_percent_weight_applied` case specifies `num_tournament_rounds=4` explicitly — earlier versions of the test omitted this, and the prediction fell through to historical-only mode while the assertion still (spuriously) passed.

**Pitfall**: tournament weighting is applied in `predict_baseline()`. ML features ALSO include tournament-weighted averages. If the ensemble/blend logic sums both, tournament data is double-counted. See TODOS.md TODO-011 for the audit.

**Decay pitfall**: `calculate_performance_weight()` must see compatible datetime types (see `docs/solutions/data-integrity/decay-weights-silently-default-to-one.md`). Silent default to weight=1.0 defeats the entire decay model.
