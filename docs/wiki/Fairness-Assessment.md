# Fairness Assessment

Monte Carlo simulation produces win-rate numbers. Fairness assessment
turns those numbers into a go / no-go verdict that an official can
sign off on.

## The one metric that matters

**Win-rate spread** — the difference between the highest and lowest
win rate in the simulated field. Lower is better; zero is perfect.

`SimulationConfig` defines five bands:

| Rating       | Spread threshold | Interpretation                                  |
|--------------|------------------|-------------------------------------------------|
| Excellent    | < 2 %            | Handicaps are essentially perfect               |
| Very good    | < 5 %            | Production-ready; no adjustment needed          |
| Good         | < 10 %           | Fair; acceptable for most events                |
| Fair         | < 15 %           | Usable but worth reviewing the input data       |
| Poor         | ≥ 15 %           | Predictions have systematic bias; investigate   |

Bands come from `SimulationConfig.FAIRNESS_THRESHOLD_*` in
`config.py`.

## Calling the assessment

```python
from strathmark import simulate_and_assess_handicaps

assessment = simulate_and_assess_handicaps(
    mark_results,
    num_simulations=500_000,
)
print(assessment["rating"])          # 'Excellent' / 'Very Good' / …
print(assessment["spread_percent"])  # float
print(assessment["narrative"])       # plain-text explanation
```

The narrative includes:

- Summary rating and spread.
- List of competitors flagged for high-variance input data.
- Notes on any win-rate outlier (a competitor more than two standard
  deviations from the mean win rate).

## LLM-based commentary

When Ollama is reachable (or Gemini is configured), extended narrative
is available:

```python
from strathmark import get_ai_assessment_of_handicaps

narrative = get_ai_assessment_of_handicaps(
    mark_results,
    simulation_results,
    event_name="300 mm SB",
)
print(narrative)
```

This produces a multi-paragraph sports-commentary-style analysis that
describes the race dynamics and calls out interesting micro-battles.
It is decorative, not load-bearing — the structured `assessment` is
what officials decide on.

## Championship race analysis

For final-round setups the dedicated role surfaces a six-section
commentary:

```python
from strathmark import get_championship_race_analysis

analysis = get_championship_race_analysis(
    mark_results,
    simulation_results,
    event_name="Grand Final SB",
    round_type="final",
)
```

Sections: pre-race narrative, start-line dynamics, mid-race
developments, finish-line drama, likely winner reasoning, "what would
change this". Each section is 2–4 sentences and is suitable for
announcer cue cards.

## What good looks like

A well-functioning race day typically produces:

- 70–80 % of heats rated **Excellent** (< 2 % spread).
- 15–25 % rated **Very Good** (2–5 %).
- A handful rated **Good** (5–10 %) — usually heats with one or more
  competitors whose historical data is thin.
- Anything **Fair** or **Poor** is a flag to pull the officials into
  the data review.

## What to do when a heat rates Poor

1. Check for input-data outliers. A single bad time in a competitor's
   history (e.g. a DNS mis-recorded as 5 s) corrupts the average and
   pushes the whole heat's spread above 15 %.
2. Verify the wood quality rating. A 5 on a block that is actually a
   7 will shift the cascade's predictions in the wrong direction.
3. Check whether any competitor has zero history. The panel-mark
   fallback at `VERY LOW` confidence is the right answer for
   newcomers, but it produces wide spread by construction.
4. Consider a manual override for any obviously mis-modelled
   competitor — the cascade respects the override at `VERY HIGH`
   confidence.

## Bias detection across events

The cascade tracks systematic bias per competitor across events.
`db.get_competitor_bias()` returns the running mean of `(actual −
predicted)` for a competitor. Use this to spot competitors whose
predictions are consistently 2+ seconds off — the isotonic calibrator
in `predictor.py` will eventually correct this automatically, but the
bias report is the manual fall-back.

## Testing

- `tests/test_fairness.py` — rating band boundaries, narrative text.
- `tests/test_fairness_extended.py` — warning triggers (variance
  imbalance, outlier win rate, insufficient simulations).

Every `assessment["rating"]` value is one of the five band strings;
the test suite asserts `in {'Excellent', 'Very Good', 'Good', 'Fair',
'Poor'}` on every return.
