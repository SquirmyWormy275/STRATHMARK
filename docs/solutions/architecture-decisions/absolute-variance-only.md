---
type: knowledge
problem_type: architecture_decision
severity: high
tags:
  - "variance"
  - "monte-carlo"
  - "domain-rules"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Absolute Variance Only

## Context
Woodchopping times are dominated by strike-by-strike noise (axe bite, knot encounter, foot slip) that does NOT scale with total chop time. A 20-second chop and a 60-second chop show similar absolute standard deviations across heats from the same competitor.

## Pattern
- Competitor-level variance MUST be modeled as absolute ±3 seconds when derived from history
- Proportional variance (e.g., `std = predicted * 0.12`) is permitted ONLY as a default-estimate fallback when no history exists, and MUST be clamped via `sim_config.MIN_COMPETITOR_STD_SECONDS` and `sim_config.MAX_COMPETITOR_STD_SECONDS`
- The `DEFAULT_VARIANCE_SCALING_FACTOR = 0.12` constant lives in `SimulationConfig`, not as a magic number in `calculator.py`

## Rationale
Proportional variance would predict that a slow chopper has wildly more variance than a fast chopper of the same competence, which is empirically false. It also compounds badly in Monte Carlo: over 500K simulations, proportional variance makes mark stability dependent on predicted time, which is circular.

The 0.12 default was empirically validated against historical data and represents the mean `std/mean` ratio across all observed competitors. It exists only to bootstrap new competitors with no history.

## Examples
Commit 10a4726 extracted the magic number 0.12 into config and clamped the scaling output:
```python
# Before (calculator.py):
competitor_std = max(1.5, min(prediction.value * 0.12, 15.0))

# After:
competitor_std = max(
    sim_config.MIN_COMPETITOR_STD_SECONDS,
    min(prediction.value * sim_config.DEFAULT_VARIANCE_SCALING_FACTOR,
        sim_config.MAX_COMPETITOR_STD_SECONDS),
)
```

When actual history is present:
```python
raw_std = float(np.std(event_times, ddof=1))
competitor_std = max(1.5, min(raw_std, 15.0))  # absolute, not proportional
```
