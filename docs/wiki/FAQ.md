# FAQ

## Is V2 still an LLM/ML/baseline cascade?

No. One hierarchical V2 core is authoritative. The old five keys remain for API
compatibility; numeric `llm` is always `None`, and `ml` exists only for a promoted
residual correction.

## Which factors affect a number?

Stable identity/prior history, event, strictly prior result dates, diameter, species
physical properties, and gender including missingness. Division, heat, venue, lane,
run order, material identity, quality/moisture, weather, equipment, fatigue,
penalty/DNF, same-tournament weighting, and field strength do not.

## Why did changing quality or division do nothing?

That is intentional. Those compatible fields lack current provenance-backed evidence.
They remain numeric no-ops until a future model validates them.

## What happens with no history?

A compatible core returns a wide conditional population prior. Without a compatible
core artifact, STRATHMARK returns a labeled broad event prior and degraded warning.

## Is the interval the same as std_dev?

No. The interval is calibrated uncertainty about the predicted time. `std_dev` is
performance variability used in race simulation.

## Why is the residual inactive?

No optional candidate was frozen before the 2.0.0 locked test. CatBoost is available as
tooling but cannot activate without passing the prospective promotion gate.

## Can I rerun the locked benchmark?

Use `python train_model.py` to verify it. Do not delete the final report or rerun
`--open-locked-test`; a future model needs a new prospective locked split.

## Why are my marks not exactly 3 + rounded gap?

V2 normally optimizes the full field from joint posterior samples. Rounded gap is the
safe fallback and tie anchor. Inspect `optimizer` and `optimizer_metadata`.

## Does calculation require the cloud?

No. The model and local SQLite work offline. Cloud mirroring is optional and
best-effort.

## How do I record trusted predictions?

Use authenticated `/ledger/calculate` with a durable request ID and stable competitor
IDs. Public `/calculate` and `/predict` are deliberately stateless.

## How do I roll back?

Temporarily set `STRATHMARK_PREDICTION_ENGINE=legacy` and restart. It selects a
deterministic baseline-only path, still excludes non-prior rows, and never uses a
numeric LLM.
