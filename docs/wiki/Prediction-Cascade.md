# Legacy Prediction Keys in V2

The pre-2.0 numeric cascade is superseded. STRATHMARK now runs one Prediction Engine V2
bundle and projects its results into the five keys older consumers expect.

```text
manual   operator-supplied time; authoritative, uncalibrated, not training evidence
llm      None; LLMs cannot produce or adjust numeric predictions
ml       promoted residual correction to V2, if active
baseline authoritative V2 hierarchical core
panel    static broad SB/UH prior for degraded fallback
```

Selection is manual, promoted residual, core, then panel. The 2.0.0 packaged residual
is inactive, so a normal non-manual prediction is `baseline`.

Legacy `results_df`, `ml_model`, `llm_client`, division, tournament, heat, quality, and
field-strength inputs remain accepted where needed for compatibility but do not alter a
V2 number. They must not be described as active evidence.

Every result may include interval, engine/model/calibration versions, cutoff,
provenance, warnings, ignored factors, and degraded state. See [Prediction Engine
V2](Prediction-Engine-V2) for the active mechanism.

`STRATHMARK_PREDICTION_ENGINE=legacy` is a temporary baseline-only rollback. It applies
the cutoff, removes inactive context, and never restores numeric LLM behavior.
