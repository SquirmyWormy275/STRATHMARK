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
source: "knowledge-seed from CLAUDE.md and git history"
last_updated: 2026-04-21
---

# Mark Floor and Ceiling Invariants

## Context
STRATHMARK computes handicap marks for woodchopping competitions. The mark system has domain-imposed bounds that must never be violated: marks are seconds-of-head-start assigned by a human panel of experts, and the bounds are fixed by competition rules, not by algorithmic choice.

## Pattern
- **Mark floor: 3 seconds** — the slowest predicted competitor gets mark 3; no competitor ever gets a mark below 3. Do not lower this floor under any circumstance.
- **Mark ceiling: 183 seconds system-wide** — the event time limit is 180s plus the 3s minimum mark. No valid mark can exceed 183.
- **Gap logic**: `gap = predicted_time(competitor) - predicted_time(front_marker); mark = 3 + round(gap); mark = min(mark, 183)`. Rounding is standard half-to-even (banker's rounding).
- **Variance**: absolute ±3 seconds only. Proportional variance is FORBIDDEN.

## Rationale
The floor exists because no competitor can reasonably react and start in under ~3 seconds; any mark below 3 is physically meaningless. The ceiling exists because a mark > 183 would mean the competitor starts after the event has already timed out. Proportional variance is forbidden because woodchopping variance is dominated by strike-by-strike noise that does NOT scale with total time — a 20s chop and a 60s chop have similar absolute variance.

## Examples
```python
# From CLAUDE.md Design Rules
gap = predicted_time(competitor) - predicted_time(front_marker)
mark = 3 + round(gap)     # round half-to-even
mark = min(mark, 183)     # system-wide ceiling
```

Integration test `test_all_marks_at_most_183` was changed from 180 to 183 in commit df2fe3a — the 180 value was wrong, the system ceiling is 183.

Prior behavior (pre-v0.3.0): mark computation used `math.ceil(gap)`, which systematically inflated every non-integer-gap mark by ~0.5s on average. The switch to `round()` (half-to-even) landed in v0.3.0 Phase 4 alongside the Monte Carlo tuning. See [`../best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md`](../best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md) for the v0.3.0 hardening narrative.

Any code that introduces proportional variance (e.g., `std = predicted * 0.12`) in a Monte Carlo or mark calculation violates the design rule. Variance scaling exists for the DEFAULT estimate only (when no real history is available) — actual variance from history MUST use absolute std-dev.
