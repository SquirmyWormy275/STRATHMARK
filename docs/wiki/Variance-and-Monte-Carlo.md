# Variance and Monte Carlo Simulation

A handicap is only fair if every competitor has an equal chance of
winning. STRATHMARK uses Monte Carlo simulation to validate that claim
numerically — simulate hundreds of thousands of races with realistic
performance variance, count who wins, and check the spread is tight.

## The absolute variance rule

STRATHMARK uses **absolute** ±3-second variance for every competitor,
not a percentage. This is one of the design invariants listed on
[Home](Home) and is enforced at module level in
`strathmark/variance.py`.

**Why absolute, not proportional?** Real-world factors that cause
performance variance — a grain knot, an axe wobble, a slight
mis-strike — cost everyone the same absolute number of seconds. They
do not scale with skill.

Testing this empirically (STRATHEX Monte Carlo sweep, 1 000 000
simulations):

| Variance model       | Win-rate spread |
|----------------------|-----------------|
| Absolute ±3 s        | 6.7 %           |
| Proportional ±5 %    | 31.0 %          |

Proportional variance concentrates the uncertainty on the slow
competitors (their 5 % is many more seconds than the fast
competitors' 5 %) and so systematically advantages the fast ones. It
is forbidden by design and there is no flag to turn it on.

## Variance components

Each simulated race uses three components:

- **Per-competitor std dev** (`competitor_std`) — estimated from
  historical variance, clamped to `[1.5, 15.0]` seconds. Different
  competitors have different consistency ratings.
- **Heat-level shared variance** (`heat_delta`) — a single Normal(0,
  1.0 s) draw applied identically to every competitor in a race.
  Models wind, grain batch, moisture, temperature.
- **Mark (applied as start delay)** — integer, from the calculator.

For each simulated race:

```
finish_time_i = mark_i + predicted_time_i
              + Normal(0, competitor_std_i)
              + heat_delta
```

The race winner is `argmin(finish_time)`. Repeat 500 000 times, count
wins, and convert to probabilities.

## Per-competitor std-dev estimation

`calculator.calculate()` sets `MarkResult.std_dev` per competitor:

- If the competitor has 3+ results for this event, use the clamped
  sample standard deviation — `max(1.5, min(raw_std, 15.0))`.
- If the competitor has fewer than 3 results, fall back to
  `predicted_time × 0.12`, also clamped.

The 0.12 scaling factor is `SimulationConfig.DEFAULT_VARIANCE_SCALING_FACTOR`
— empirically validated against AAA competition data.

## Consistency ratings

The simulation reports each competitor's consistency rating based on
their std dev:

| Rating      | Threshold (s) |
|-------------|---------------|
| Very high   | std dev ≤ 2.5 |
| High        | 2.5 < std dev ≤ 3.0 |
| Moderate    | 3.0 < std dev ≤ 3.5 |
| Low         | std dev > 3.5 |

These ratings surface in the fairness explanation so officials can
see which competitor has the noisiest history, independent of the
handicap quality.

## Simulation count

- **Full run (race day):** `NUM_SIMULATIONS = 500_000`.
- **Quick pre-event check:** `NUM_SIMULATIONS_QUICK = 100_000`.

The quick run is used during pre-event validation
(`scripts/validate_deployment.py`). The full run is for the fairness
report that accompanies each mark sheet.

## Running a simulation

```python
from strathmark import run_monte_carlo_simulation

simulation_entries = [r.to_simulation_dict() for r in mark_results]
sim = run_monte_carlo_simulation(
    simulation_entries,
    num_simulations=500_000,
)

print(sim["summary"])          # plain-text summary
print(sim["win_rates"])        # dict {name: probability}
print(sim["finish_spread_s"])  # scalar (mean finish spread)
```

Every entry in `simulation_entries` must carry `name`, `mark`,
`predicted_time`, and `std_dev`. The `MarkResult.to_simulation_dict()`
method produces the correct shape automatically.

## `quick_fairness_check()`

For the deployment validator and any in-process sanity check:

```python
from strathmark import quick_fairness_check

report = quick_fairness_check(mark_results, num_simulations=100_000)
print(report["rating"])  # 'Excellent' | 'Very Good' | 'Good' | 'Fair' | 'Poor'
print(report["spread"])  # float, difference between highest and lowest win rate
```

This is the fastest path from a list of `MarkResult` to a go / no-go
fairness verdict.

## `audit_mark_sheet()`

Full audit with per-competitor breakdown:

```python
from strathmark import audit_mark_sheet

audit = audit_mark_sheet(mark_results, num_simulations=500_000)
print(audit["variance_ratio"])     # max/min competitor std dev
print(audit["imbalance_warnings"]) # list of plain-text warnings
```

Flags an imbalance warning when the variance ratio exceeds 2.5 — this
means the most-consistent competitor has under 40 % of the noise of
the least-consistent one, which usually indicates data quality issues
in the input rather than a truly unusual field.

## Visualisation

`visualization.py` prints a plain-text bar chart of win rates:

```
Alice     ████████████████████ 20.3%
Bob       ███████████████████  19.5%
Charlie   █████████████████    17.1%
Diana     ██████████████████   18.8%
Ed        ████████████████████ 20.1%

Win rate spread: 3.2 pp  (rating: Very Good)
```

The bar length is `SimulationConfig.VISUALIZATION_BAR_MAX_LENGTH = 40`
characters; no ANSI colour is used (the design rule).

## Testing

- `tests/test_variance.py` — basic invariants: std-dev clamping, heat
  delta seeded reproducibility, absolute-variance enforcement.
- `tests/test_variance_extended.py` — consistency rating thresholds,
  variance-ratio imbalance detection, 500 K-sample determinism.
- `tests/test_integration.py` and `tests/test_integration_extended.py`
  — full pipeline: calculate → simulate → assert Spread < threshold.

The simulation is deterministic given a seed, so regression tests can
assert exact win-rate numbers without flake.
