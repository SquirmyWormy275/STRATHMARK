# Handicap Mark Math

This page explains the exact formula STRATHMARK uses to turn predicted
times into handicap marks, plus every constraint it enforces. It also
compares the formula side-by-side with the written rules of the
American Lumberjack Association (ALA), Australian Axemen's Association
(AAA), and Queensland Axemen's Association (QAA).

## The formula

```
gap  = slowest_predicted_time - this_competitor_predicted_time
mark = MARK_FLOOR + round(gap)
mark = min(mark, effective_ceiling)
```

- `MARK_FLOOR = 3`
- `MARK_CEILING = 183` system-wide (180 s time limit + 3 s minimum)
- `effective_ceiling` defaults to 183 but can be tightened per event
  (strictly greater than `MARK_FLOOR`, strictly less than or equal to
  `MARK_CEILING`).
- `round()` is Python's built-in — banker's rounding, also called
  round-half-to-even. 7.5 rounds to 8, 8.5 rounds to 8, 9.5 rounds to
  10. The choice is deliberate: see "Rounding choice" below.

## Worked example

Five-competitor SB heat, predicted times after the cascade:

| Competitor | Predicted (s) |
|------------|---------------|
| Sue        | 58.3          |
| Bob        | 52.7          |
| Amy        | 48.2          |
| Joe        | 42.8          |
| Dan        | 38.1          |

Slowest = Sue at 58.3 s, so she gets Mark 3.

| Competitor | Gap    | `round(gap)` | Mark |
|------------|--------|--------------|------|
| Sue        | 0.0 s  | 0            | 3    |
| Bob        | 5.6 s  | 6            | 9    |
| Amy        | 10.1 s | 10           | 13   |
| Joe        | 15.5 s | 16           | 19   |
| Dan        | 20.2 s | 20           | 23   |

(Banker's rounding only affects half-integer gaps. Every non-half
rounds the same as standard rounding.)

If every competitor cuts exactly to prediction, finish times are:

| Competitor | Delay | Cutting | Finish   |
|------------|-------|---------|----------|
| Sue        | 0 s   | 58.3 s  | 58.3 s   |
| Bob        | 6 s   | 52.7 s  | 58.7 s   |
| Amy        | 10 s  | 48.2 s  | 58.2 s   |
| Joe        | 16 s  | 42.8 s  | 58.8 s   |
| Dan        | 20 s  | 38.1 s  | 58.1 s   |

Spread ≈ 0.7 s, well inside the ±3 s variance used in Monte Carlo.

## The floor — Mark 3

The slowest predicted competitor — the *front marker* — always starts
at Mark 3. The floor is 3 for historical and safety reasons:

- It gives the announcer three full seconds of lead-in count
  ("three, two, one, Go") before any axe swings.
- It matches the written rules of every sanctioning body STRATHMARK
  targets. See [Rulebook Comparison](Rulebook-Comparison).
- It prevents a negative mark in any scenario (the cascade is not
  allowed to produce a predicted time slower than the slowest
  competitor's baseline).

The floor is set in one place: `HandicapCalculator.MARK_FLOOR = 3` in
`calculator.py`. No cascade level can override it.

## The ceiling — 183 s

The system-wide ceiling is 183 s = 180 s time limit + 3 s minimum
mark. The time limit matches the `Judge has a discretion to direct a
Competitor … to cease competing` rule in the AAA rulebook (2 minutes
for chopping / sawing, 3 minutes before forced stop), scaled to
STRATHEX's broader target. A mark of 183 means the competitor must wait
180 seconds before starting, which is the maximum a heat can
realistically run.

If an event wants a tighter limit (say 90 s for a hot-saw exhibition),
construct the calculator with `event_ceiling=90`. The constructor
raises `ValueError` if the ceiling is less than or equal to 3, or
greater than 183.

## Rounding choice — banker's rounding

STRATHEX historically rounded *up* (ceiling). STRATHMARK v0.3.0
switched to banker's rounding (Python's built-in `round`), and v0.3.1
preserved that choice. The reason is statistical, not ergonomic:

- Ceiling rounding introduces a systematic upward bias of 0.5 s per
  competitor. Over a heat of 8 competitors, 4 s of extra mark is
  added on average — this widens the predicted field spread.
- Banker's rounding is unbiased on half-integer gaps. Over large
  tournaments the mean rounding error is approximately zero.
- Testing with Monte Carlo fairness simulations showed banker's
  rounding gives spreads 0.3 to 0.8 percentage points tighter than
  ceiling rounding on typical fields.

Both choices preserve the floor-of-3 invariant. This is a modelling
decision; the choice does not affect the sanctioning body rules
because both round to whole seconds.

## Tie rule

If two competitors have identical predicted times (`gap == 0` for
both), they receive the same mark and start together. The cascade does
not need to break ties — the rulebook leaves tie-handling to the
officials. The start sheet displays them in the order they appear in
the input; downstream tournament managers can alpha-sort or use
lane-assignment draws as the rulebook requires.

## Same-tournament round handling

When a competitor has cut earlier rounds of the same event on the same
wood today, those times feed the cascade at Priority 2. The weighting
graduates from 65 % at round 1 to 97 % at round 4+. The mark formula is
unchanged — only the predicted time it operates on moves.

This matters in double-elimination or multi-round formats where the
competitor's *current* form on *today's* wood is far more predictive
than their historical average. See the weighting table in
[Prediction Cascade](Prediction-Cascade#priority-2--same-tournament-weighting-graduated).

## Why the gap is computed against the slowest and not the median

Slowest-referenced marks are simpler to announce (everyone's delay is
"seconds past Mark 3"), and they match every sanctioning body
rulebook. Median-referenced marks are mathematically valid but add a
manual-arithmetic step for officials, which is error-prone on race day.

## Sanity checks enforced on every mark

After `_assign_marks` runs, these invariants are held by construction:

- `result.mark >= 3` — guaranteed by `MARK_FLOOR + round(gap)` with
  `gap >= 0`.
- `result.mark <= effective_ceiling` — guaranteed by the final
  `min()`.
- `result.mark` is a Python `int` — `round()` returns an `int` on
  integer-free input.
- The front marker's mark equals exactly 3 (no off-by-one).

The regression test `tests/test_calculator.py::test_mark_invariants`
covers all four.
