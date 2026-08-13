# Fairness Assessment

V2 optimizes marks toward equal model-implied win probabilities, then lower expected
finish spread. This is a transparent model objective—not proof that people will have
equal real-world outcomes.

The prediction benchmark measures time accuracy and interval coverage. The separate
Monte Carlo simulator audits a chosen mark sheet under assumed performance variability.
Neither substitutes for settled-field outcome evidence.

Responsible assessment should report:

- prediction MAE/RMSE and calibrated coverage overall and by adequately sized cohort;
- optimizer fallback rate and model-implied win-probability spread;
- settled actual finish/residual outcomes, excluding manual overrides from model
  training metrics;
- missing/unknown-factor and degraded-artifact rates;
- sample sizes and confidence limitations.

The 2.0.0 locked n=128 result supports the core's fixed temporal accuracy comparison,
not universal fairness. Gender is an active immutable/missing model category; division
is inactive. Future venue, lane, material, weather, equipment, fatigue, and status
factors require provenance and enough seasons before analysis.

Optional LLM fairness text is narrative-only and must cite numeric simulator/ledger
evidence rather than inventing conclusions.
