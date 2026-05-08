---
type: knowledge
title: Hardening the Prediction Cascade — LLM JSON Schemas, Scaled Variance, Tuned Monte Carlo
date: 2026-04-21
category: docs/solutions/best-practices
module: strathmark.predictor
component: assistant
problem_type: best_practice
severity: high
applies_when:
  - adding a new LLM role that participates in prediction or fairness decisions
  - changing Monte Carlo parameters (sample count, variance caps)
  - modifying rounding or gap logic in the calculator
  - wiring a new prediction source into the cascade
  - updating the default variance model
related_components:
  - service_object
  - testing_framework
  - tooling
tags:
  - llm-integration
  - ollama
  - json-schema
  - monte-carlo
  - prediction-cascade
  - variance-tuning
  - bankers-rounding
  - fairness
related:
  - docs/solutions/architecture-decisions/prediction-cascade-order.md
  - docs/solutions/architecture-decisions/absolute-variance-only.md
  - docs/solutions/architecture-decisions/mark-floor-ceiling-invariants.md
confidence: high
source: "v0.3.0 release — Phase 3 (LLM integration) + Phase 4 (Monte Carlo optimization)"
---

# Hardening the Prediction Cascade — LLM JSON Schemas, Scaled Variance, Tuned Monte Carlo

## Context
v0.3.0 shipped one coordinated hardening package across the prediction pipeline. Four gaps were closed together because they all undermined the same guarantee: that a mark sheet is defensible in a protest hearing. Freeform Ollama responses fell through to ML whenever the model freestyled a field name; regex parsing was the fragile approach they replaced. The LLM tier existed as a shadow path in `predict_with_llm()`, not wired into the canonical cascade. Monte Carlo ran at 2,000,000 samples without vectorization, slow enough that nobody ran it pre-race. Default variance was a flat 3.0s and mark rounding used `math.ceil()`, inflating every non-integer-gap mark by ~0.5s on average.

The three cross-linked architecture-decisions docs describe each resulting rule in isolation. This doc connects them to v0.3.0 and adds the `llm_roles.py` / `fairness.py` context those decision docs don't cover.

## Guidance

### 1. Enforce JSON schemas on every LLM prediction-path call
Ollama's `format` parameter accepts a JSON schema and constrains generation via GBNF grammar logit masking. Pair schema enforcement with `temperature=0.0` so identical inputs produce identical parseable output.

From [strathmark/llm.py:121-219](../../../strathmark/llm.py):
```python
def call_ollama(
    prompt: str,
    model: Optional[str] = None,
    num_predict: Optional[int] = None,
    ollama_url: Optional[str] = None,
    timeout: Optional[int] = None,
    format_schema: Optional[dict] = None,
) -> Optional[str]:
    temperature = 0.0 if format_schema is not None else 0.3
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    if format_schema is not None:
        payload["format"] = format_schema
```

The Gemini fallback added in v0.4.0 mirrors this contract via `response_mime_type="application/json"` + `response_schema`, so the structured-output discipline survives the cloud handoff.

### 2. Define schemas per role in a dedicated module
`strathmark/llm_roles.py` centralizes every schema the system uses. Keeping payload shapes reviewable in one file means downstream `json.loads()` calls rely on a typed contract instead of hoping the prompt worked.

```python
COMPETITOR_PROFILE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "recent_form": {"type": "string"},
        "prediction_confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "watch_factors": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["narrative", "strengths", "recent_form", "prediction_confidence"],
}
```

`fairness.py` follows the same pattern with a `rating` enum `["Excellent", "Very Good", "Good", "Fair", "Poor", "Unacceptable"]` — the handicapper-facing vocabulary lives in the schema, not in post-hoc parsing.

### 3. Wire LLM into the canonical cascade
Before v0.3.0 the LLM path was decorative and the duplicate `_call_ollama()` in predictor.py was a symptom of the tier never having been wired in (session history). `HandicapCalculator.calculate()` now hands a single `llm_client` to `get_best_prediction()`:

```python
llm_client = {
    "url": self._ollama_url,
    "model": llm_config.PREDICTION_MODEL,
    "timeout": llm_config.TIMEOUT_SECONDS,
}
prediction = get_best_prediction(
    effective_record, wood, event_code,
    wood_data_df=self.wood_df,
    results_df=self.results_df,
    ml_model=self._ml_model,
    llm_client=llm_client,
)
```

The dict payload is pragmatic for three well-known fields; a `TypedDict` or frozen dataclass would be more Pythonic when adding a fourth cascade source. `analytics.py` backtesting calls the same `get_best_prediction()` so production and evaluation run identical logic. See [`prediction-cascade-order.md`](../architecture-decisions/prediction-cascade-order.md) for the tier-order rationale.

### 4. Scale default variance with predicted time
When fewer than 3 event results exist, scale by `DEFAULT_VARIANCE_SCALING_FACTOR = 0.12` (empirically the mean `std/mean` ratio across observed competitors — session history), clamped to `[MIN_COMPETITOR_STD_SECONDS, MAX_COMPETITOR_STD_SECONDS]`. v0.3.0 also raised `MAX_COMPETITOR_STD_SECONDS` from 6.0 to 15.0 because the lower cap was clipping elite std-devs and triggering false-positive imbalance warnings (session history). The simulator flags `variance_imbalanced` when max/min ratio > 2.0.

Proportional variance is forbidden for history-based competitors — it applies ONLY to the no-history default. See [`absolute-variance-only.md`](../architecture-decisions/absolute-variance-only.md) for the absolute-only rule and the canonical calculator.py diff.

### 5. Use banker's rounding for mark computation
Ceiling rounding biased every non-integer-gap mark upward by ~0.5s on average. Across a 15-competitor field at 500K simulations that bias was measurable in fairness metrics (session history). Python's `round()` is half-to-even — exactly the design rule. See [`mark-floor-ceiling-invariants.md`](../architecture-decisions/mark-floor-ceiling-invariants.md) for the canonical `mark = 3 + round(gap); min(mark, 183)` form.

### 6. Tune Monte Carlo: two tiers + variance caps + quick check
Dropped from 2,000,000 to 500,000 once numpy vectorization landed — same statistical quality at 4x throughput (session history). Two tiers serve different audiences:

```python
NUM_SIMULATIONS: int = 500_000          # production precision
NUM_SIMULATIONS_QUICK: int = 100_000    # pre-competition fast path
MIN_COMPETITOR_STD_SECONDS: float = 1.5
MAX_COMPETITOR_STD_SECONDS: float = 15.0
DEFAULT_VARIANCE_SCALING_FACTOR: float = 0.12
```

`run_monte_carlo_simulation(..., verbose: bool = False)` keeps race-day pipelines quiet. The new `quick_fairness_check` wraps `audit_mark_sheet()` with 100K sims for iterative tuning before publishing marks:

```python
def quick_fairness_check(
    competitors_with_marks: list,
    variance: float = 3.0,
) -> dict:
    return audit_mark_sheet(
        competitors_with_marks=competitors_with_marks,
        num_simulations=100_000,
        variance=variance,
        verbose=False,
    )
```

Caller expects items shaped `{"name": str, "predicted_time": float, "mark": int}`; a `TypedDict` is warranted if this becomes a public API.

## Why This Matters
Fairness is only real if the pipeline is deterministic. Unvalidated LLM JSON silently falls through to ML or panel marks whenever the model freestyles a field name, making the cascade's stated priority order a lie. A flat 3s variance gives a 20s-predicted chopper the same uncertainty budget as a 90s-predicted novice, biasing Monte Carlo win rates away from the back marker. The four fixes together are the difference between a demo pipeline and a race-ready fairness engine.

v0.3.0 wired LLM into the cascade but did NOT add fail-fast timeouts — the Ollama-unreachable-host hang (120s per dead call) was fixed in v0.4.0. See [`performance-issues/ollama-cascade-hang-on-unreachable-host.md`](../performance-issues/ollama-cascade-hang-on-unreachable-host.md).

## Related
- [`architecture-decisions/prediction-cascade-order.md`](../architecture-decisions/prediction-cascade-order.md) — normative rule for Manual > LLM > ML > Baseline > Panel
- [`architecture-decisions/absolute-variance-only.md`](../architecture-decisions/absolute-variance-only.md) — absolute-only variance rule; proportional permitted ONLY as no-history default
- [`architecture-decisions/mark-floor-ceiling-invariants.md`](../architecture-decisions/mark-floor-ceiling-invariants.md) — `mark = 3 + round(gap); min(mark, 183)`
- [`performance-issues/ollama-cascade-hang-on-unreachable-host.md`](../performance-issues/ollama-cascade-hang-on-unreachable-host.md) — v0.4.0 follow-up that added fail-fast timeouts + Gemini fallback
