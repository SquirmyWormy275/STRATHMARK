# Legacy prediction keys in V2

Older consumers expect five result keys. V2 keeps those keys as a compatibility view
of its prediction engine; it does not run the old numeric selection cascade.

| Key | Meaning |
| --- | --- |
| `manual` | Explicit operator time override; uncalibrated and excluded from model-training evidence. |
| `llm` | Always `None` numerically. |
| `ml` | Optional promoted residual correction; inactive in the 2.0.0 release. |
| `baseline` | The V2 core prediction. |
| `panel` | Broad Standing Block/Underhand prior used for degraded fallback. |

Selection order is manual override, active residual, core, then panel. A normal
non-manual 2.0.0 prediction therefore uses `baseline`.

Compatibility inputs such as quality, heat, division or tournament context may still
be accepted, but inactive factors do not change the prediction. Read the returned
warnings, versions, cutoff and degraded state. See [V2](Prediction-Engine-V2).

`STRATHMARK_PREDICTION_ENGINE=legacy` is a temporary baseline-only rollback mode.
It still applies the cutoff and does not restore numeric LLM behavior. These keys
and that mode are V2-specific; [V3](Prediction-Engine-V3) has a separate contract.
