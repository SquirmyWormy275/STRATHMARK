# Uncertainty, Variance, and Simulation

STRATHMARK exposes three related but distinct concepts:

1. `PredictionInterval`: chronological conformal uncertainty around the V2 median.
2. `std_dev`: competitor race-performance variability used by the public simulator.
3. Joint optimizer samples: 2,048 fixed common-random draws used only to choose marks.

Do not substitute one for another. The calibrated interval reports its nominal
coverage, state, and scope. Performance `std_dev` keeps the existing public meaning and
is estimated from event history or bounded defaults.

The mark optimizer is deterministic for identical inputs, cutoff, bundle, and seed. It
uses at most eight coordinate passes and falls back to bounded rounded median gaps if
posterior sampling/search fails.

The separate `run_monte_carlo_simulation` and `/simulate` path audits an already assigned
mark sheet. The REST default and maximum are 250,000 races, further bounded by a
4,000,000 cell limit and one concurrent simulation per process. A simulation result is
model-implied evidence, not proof that
actual future fields will finish equally.
