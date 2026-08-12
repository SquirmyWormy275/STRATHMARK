# Handicap Mark Math

STRATHMARK first predicts one positive finish-time distribution per competitor, then
chooses integer marks for the complete field.

The V2 optimizer compares 2,048 deterministic common-random race samples. Its objective
is lexicographic: equal model-implied win probability, expected finish spread, closeness
to legacy rounded-gap marks, then the input-order mark tuple. It runs at most eight
passes and accepts no sheet worse than the fallback under that objective.

Invariants:

- every mark is an integer from 3 through the effective event ceiling;
- at least one competitor receives Mark 3;
- a faster predicted median cannot start earlier than a slower median;
- equal medians retain input order;
- a one-person field receives Mark 3;
- identical inputs, cutoff, bundle, and seed return identical marks.

If distributions are unavailable or search fails, STRATHMARK uses:

```text
slowest = max(predicted medians)
mark = 3 + round(slowest - competitor median)
mark = clamp(mark, 3, effective ceiling)
```

Python `round` is round-half-to-even. Each result exposes `optimizer` and metadata so an
operator can distinguish `posterior_crn_v2` from `rounded_gap_fallback` and see the
seed, samples, passes, objectives, and reason.
