---
type: knowledge
problem_type: architecture_decision
severity: medium
tags:
  - "predictor"
  - "ml"
  - "tournament"
  - "audit"
confidence: high
created: 2026-04-15
source: "TODO-011 audit (eng-review outside voice 2026-03-23)"
---

# Tournament Weighting Double-Counting Audit (TODO-011)

## Context
Eng-review outside voice raised concern that 97%/3% tournament weighting might be double-counted when blending baseline + ML predictions, because baseline already applies tournament weighting and ML features could also include tournament-weighted averages.

## Pattern
**Audit verdict (2026-04-15): no double-counting today.** The concern is valid design guidance for future ensemble work but does not describe a current bug.

Traced flow:
- `tournament_time` / `num_tournament_rounds` live on `CompetitorRecord` ([predictor.py:141](strathmark/predictor.py#L141))
- Only `predict_baseline()` at [predictor.py:1218](strathmark/predictor.py#L1218) applies the weighted blend:
  ```python
  _round_weights = {1: 0.65, 2: 0.80, 3: 0.90, 4: 0.97}
  t_weight = _round_weights.get(min(num_tournament_rounds, 4), 0.97)
  baseline = (tournament_time * t_weight) + (historical_baseline * h_weight)
  ```
- `train_model.py` feature engineering has NO `tournament_time` or `num_tournament_rounds` input. Features are `comp_recent`, `days_since_last`, species/wood scalars, and historical aggregates ([train_model.py:284-303](train_model.py#L284-L303)). Tournament data is NOT present in ML training features.
- The cascade in `select_best_prediction()` ([predictor.py:1917](strathmark/predictor.py#L1917)) selects ONE method per prediction (Manual > LLM > ML > Baseline). No blend layer exists.
- `tournament_weighted` flag propagates into LLM and ML wrappers at [predictor.py:1375](strathmark/predictor.py#L1375) and [predictor.py:1869](strathmark/predictor.py#L1869), but only for confidence upgrading ("VERY HIGH") and a hint to LLM to apply minimal adjustment — NOT for re-applying the 97/3 math.

## Rationale
Double-counting requires BOTH (a) an ensemble blend that sums method outputs, and (b) multiple methods that each apply tournament weighting. Today, only baseline applies the weighting, and methods are selected (not blended). The guardrail therefore lives in the ensemble design, not in current code.

## Examples
Guardrails for future ensemble work (TODO-002, TODO-005):
1. Ensemble blend MUST operate on method outputs, not re-read `tournament_time` — the weighting is already baked into each method's output.
2. If ML is retrained to include `tournament_time` as a feature, the ensemble layer MUST NOT also apply its own tournament-vs-historical split — the ML prediction already reflects tournament context.
3. Any new prediction method that adds tournament weighting MUST document the fact in its `metadata['tournament_weighted']` flag, so the ensemble can detect and avoid stacking.
4. Shadow-evaluate any ensemble change against competitors WITH and WITHOUT `tournament_time` set — a bug that only affects the tournament path will hide in the aggregate MAE.
