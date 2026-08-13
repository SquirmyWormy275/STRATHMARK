---
type: knowledge
problem_type: architecture_decision
severity: high
tags:
  - "ensemble"
  - "predictor"
  - "meta-learner"
  - "deferred-scope"
  - "outside-voice-review"
confidence: high
created: 2026-04-21
last_updated: 2026-08-13
source: "CEO + Eng plan-review sessions 2026-03-23; TODOS.md TODO-001..011"
---

# Ensemble Predictor — Design Decisions and Deferred Scope

> **Superseded by Prediction Engine V2.** This document preserves a pre-2.0 proposal
> for historical context. Its cascade, numeric-LLM, name-keyed ledger, and ensemble
> backlog are not active requirements. See
> [`docs/PREDICTION_ENGINE_V2.md`](../../PREDICTION_ENGINE_V2.md) and the current
> [`TODOS.md`](../../../TODOS.md).

## Historical context
The CEO and eng reviews on 2026-03-23 produced a comprehensive design for a future ensemble predictor — a meta-learner that would blend outputs from the Manual/LLM/ML/Baseline tiers using learned weights rather than the current first-valid-wins cascade. The work is tracked in [`TODOS.md`](../../../TODOS.md) as TODO-001..011.

TODOS.md tracks the *tasks*. This doc captures the *design decisions* — the agreed-upon shape of the solution, which alternatives were considered and rejected, and which scope was deliberately deferred. Without this, a future implementer re-opens every debate by re-reading TODOS.md piece by piece.

## Accepted design

**Meta-learner = inverse-MAE weighting, not logistic regression.**
`weight = 1 / method_MAE`, normalized across available methods. Simpler than multinomial logistic regression, no sklearn dependency for inference, statistically sound with <100 data points. The outside-voice reviewer pointed out that logistic regression predicts "which method is best" — the wrong question; the question is "what blend weight produces the best predicted time." Inverse-MAE answers that question directly.

**Ensemble layers on top of `select_best_prediction()`, not replace it.**
[`strathmark/predictor.py`](../../../strathmark/predictor.py) already has `select_best_prediction()` doing expected-error scoring across methods. The ensemble adds blend weighting above it; the existing `_expected_error()` stays as the fallback when ledger data is sparse. Replacing it would create two competing method-selection systems.

**`get_best_prediction()` scores and returns one; the ensemble needs the raw per-method outputs.**
The current hot path uses `select_best_prediction()` to score candidates by `_expected_error` and return the winner — it does not run an all-methods blend. The ensemble's meta-learner needs outputs from every available method to compute the blend. `get_all_predictions()` is already the public surface for this — the ensemble wraps it. Performance impact is real (always paying LLM + ML cost) — gate the all-methods path behind "ensemble has enough data to justify it" (see activation threshold below).

**Activation threshold: 100+ ledger entries spanning 2+ distinct competition dates.**
30 entries all from one competition overfit to that day's conditions. Distinct-date check prevents single-venue overfitting. Track `distinct_count(prediction_date)` in the ledger; require BOTH total ≥100 AND distinct dates ≥2.

**Shadow evaluation: 50+ entries OR a statistical significance test.**
With absolute ±3s variance, 20 samples cannot distinguish real model improvement from noise. Either increase the shadow-eval sample size to 50+ or run a paired t-test / Wilcoxon signed-rank comparing old-vs-new model errors.

**Ledger unique key: `(competitor_name, heat_id, event_code, method, prediction_date)`.**
The current `results` table key `(competitor_name, heat_id, event_code, time_seconds)` is correct as-is for *results*. When the *prediction ledger* is created, its key must include `prediction_date` to prevent cross-competition matching when `heat_id` defaults to empty string.

## Simpler alternative to test first

Before building `ensemble.py`, try data-driven `_expected_error()`:
- `_expected_error()` at [`predictor.py:1954`](../../../strathmark/predictor.py) (inside `select_best_prediction`, line 1913) currently uses hard-coded confidence-to-error mapping.
- Replace with actual per-method MAE from the ledger when available; fall back to the hard-coded values when no ledger data.
- This is ~20 lines. If it closes most of the accuracy gap, the full ensemble is overengineering.
- Estimate the ceiling by running both in parallel for N shows before committing to `ensemble.py`.

This test-first discipline was explicit in the eng review: if a simpler change captures most of the win, the ensemble becomes optional scope.

## Deliberately deferred

**Pairwise competition predictions** — scoped out as "a separate product, not a feature." The outside voice reviewer argued that "predict head-to-head outcomes between two specific competitors" is a different product from "produce a fair start sheet." Keep STRATHMARK focused on marks; pairwise is a downstream tool.

**Automated XGBoost retraining** — manual via `scripts/train_model.py` only. Model versioning, rollback, and shadow-evaluation-before-swap are not solved. Auto-retraining without those guardrails risks shipping a regression during a live event.

**Division-specific method-accuracy priors** — defer until 500+ ledger entries exist across divisions. Division-specific weights require division-specific sample depth; sub-100 per division would overfit.

## Tournament-weighting guardrail (TODO-011)
When the ensemble blends baseline + ML, `tournament_time` must be applied at exactly ONE tier — the blend itself must not re-apply it. Full audit and reasoning in [`tournament-weighting-audit-todo-011.md`](tournament-weighting-audit-todo-011.md).

## When to revisit
- Before writing the first line of `ensemble.py`
- When ledger crosses 100 entries (re-evaluate activation threshold)
- If a new outside-voice review disagrees with a decision above — update this doc with the new Status line

## Related
- [`TODOS.md`](../../../TODOS.md) — the task-level tracker (TODO-001..011)
- [`tournament-weighting-audit-todo-011.md`](tournament-weighting-audit-todo-011.md) — detailed TODO-011 writeup
- [`prediction-cascade-order.md`](prediction-cascade-order.md) — current cascade rules that the ensemble layers above
- [`../workflow-patterns/eng-review-cycle-and-todos-handoff.md`](../workflow-patterns/eng-review-cycle-and-todos-handoff.md) — the review-to-TODOS-to-impl pattern that produced this design
