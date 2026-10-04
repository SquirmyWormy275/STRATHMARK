# Assessing fairness

A useful handicap offsets expected ability differences. Prediction accuracy,
uncertainty calibration and mark fairness are related, but they need separate checks.

## What the reports can show

- **Time accuracy:** average errors and tail errors against actual cutting times.
- **Interval coverage:** how often the issued forecast interval contains the result.
- **Model-implied fairness:** simulated win-probability spread and expected finish spread.
- **Observed outcomes:** actual settled fields, sample sizes, overrides and degraded cases.

Report adequately supported cohorts as well as overall averages. Missing factors and
small samples limit conclusions. Manual overrides should not be counted as model
training successes.

V2 prioritizes equal model-implied win probabilities, then lower expected finish
spread. Its frozen 128-row accuracy comparison is evidence for that dataset and split,
not universal fairness. See [V2](Prediction-Engine-V2).

V3 also retains independent forecasts, disagreement and capability/weighting changes.
In Linux V3 the active forecasts are Formula and ML; the council is unavailable.
See [V3](Prediction-Engine-V3).

## What the reports cannot decide

Simulation does not prove equal real-world outcomes, sanctioning-body compliance or
cheating. A close finish alone does not prove a valid handicap, particularly if the
prediction used information unavailable before the race. Officials still determine
legal outcomes under the governing rules. See [how handicaps work](Handicap-Mark-Math).
