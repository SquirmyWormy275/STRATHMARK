---
type: knowledge
problem_type: architecture_decision
severity: critical
tags:
  - "calculator"
  - "invariants"
  - "marks"
  - "domain-rules"
confidence: high
created: 2026-04-15
source: "internal knowledge"
last_updated: 2026-08-22
---

# Mark Floor and Ceiling Invariants

## Context
STRATHMARK computes handicap marks for woodchopping competitions. The current V2 engine has
load-bearing mark bounds, but their authority differs. The 3-second floor matches the
reviewed AAA/QAA handicap context. The 183-second ceiling is a STRATHMARK safety policy
derived from the 180-second event-duration boundary plus the 3-second displayed base;
it is not stated as a universal rulebook maximum. Read
[`../../wiki/Handicap-Mark-Math.md`](../../wiki/Handicap-Mark-Math.md) for the domain
meaning of a mark and common-offset rebasing.

## Pattern
- **Mark floor: 3 seconds** — the slowest predicted competitor gets Mark 3; no
  competitor receives a mark below 3 in STRATHMARK.
- **Mark ceiling: 183 seconds system-wide** — STRATHMARK's event time limit is 180
  seconds plus the 3-second displayed base. No valid engine mark exceeds 183.
- **Gap logic**: `gap = predicted_time(frontmarker) - predicted_time(competitor); mark = 3 + round(gap); mark = min(mark, 183)`. The frontmarker is the slowest predicted competitor, so a faster competitor receives a larger mark and waits longer. Rounding is standard half-to-even (banker's rounding).
- **V2 variance**: the legacy/current V2 fallback uses an absolute ±3-second rule;
  proportional fallback variance is forbidden within that contract. This is not a
  universal statistical law and does not constrain a separately validated future
  distributional engine.

## Rationale
The 3-second floor is STRATHMARK's reviewed displayed-base policy and leaves a practical
starter-count buffer; it is not a claim that every association must use 3 or that a
smaller number is physically impossible. The ceiling keeps the configured 180-second
raw event-duration boundary within a sheet rebased to the 3-second base. Proportional
fallback variance was prohibited in the V2 design because its available evidence did not
justify assuming uncertainty scaled mechanically with predicted time. A 20-second chop
and a 60-second chop can have similar absolute strike-by-strike variation; any successor
must learn and validate its uncertainty rather than inheriting an unsupported percentage.

## Examples
```python
# Design rules
gap = predicted_time(frontmarker) - predicted_time(competitor)
mark = 3 + round(gap)  # round half-to-even
mark = min(mark, 183)  # system-wide ceiling
```

Integration test `test_all_marks_at_most_183` was changed from 180 to 183 in commit df2fe3a — the 180 value was wrong, the system ceiling is 183.

Prior behavior (pre-v0.3.0): mark computation used `math.ceil(gap)`, which systematically inflated every non-integer-gap mark by ~0.5s on average. The switch to `round()` (half-to-even) landed in v0.3.0 Phase 4 alongside the Monte Carlo tuning. See [`../best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md`](../best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md) for the v0.3.0 hardening narrative.

Any change that introduces proportional variance (for example,
`std = predicted * 0.12`) into the V2 Monte Carlo or mark calculation violates the V2
contract. A successor engine may use heteroscedastic distributions only through its own
versioned, causally validated, calibrated uncertainty contract.
