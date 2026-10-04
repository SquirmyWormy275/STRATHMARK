# Uncertainty and simulation

A time prediction, its interval and a simulation answer different questions.

| Measure | What it describes |
| --- | --- |
| V2 central 90% prediction interval | Calibrated uncertainty in a future cutting time. |
| Performance `std_dev` | Expected race-to-race variation, in seconds; used by the public simulator. |
| Optimizer samples | Fixed joint draws used internally to compare possible mark sheets. |

Do not turn the interval width directly into race noise. V2 performance spread comes
from event history or bounded defaults. V3 preserves component distributions and
pooled uncertainty under its own contract.

## Two different uses of simulation

The V2 mark optimizer uses 2,048 deterministic common-random samples to select marks,
with at most eight coordinate passes. Identical inputs, cutoff, model and seed give
identical results. Search failure produces the bounded rounded-gap fallback.

The separate `run_monte_carlo_simulation` and `/simulate` endpoint audit marks that
have already been assigned. The REST simulation default/maximum is 250,000 races,
further limited by four million competitor-cells and one concurrent simulation per
process. STRATHEX's local modes have their own limits.

A simulated win rate is conditional on the supplied model and performance spread.
It does not guarantee a competitor's actual winning chance or certify an event's
fairness. See [fairness assessment](Fairness-Assessment).
