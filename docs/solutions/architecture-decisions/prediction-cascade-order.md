---
title: Prediction Cascade Order
status: superseded-by-v2
superseded_date: 2026-08-11
superseded_by: docs/PREDICTION_ENGINE_V2.md
---

# Prediction Cascade Order — Superseded

This decision records the pre-2.0 Manual > LLM > ML > baseline > panel design. It is not
the active numeric architecture.

V3 is a separate blind formula/ML/LLM ensemble and contract, not a restoration of this
first-valid cascade. See [`../../PREDICTION_ENGINE_V3.md`](../../PREDICTION_ENGINE_V3.md).
V2 remains trusted production authority until explicit cutover.

Prediction Engine V2 uses one prior-only hierarchical core. `get_all_predictions()`
keeps the five keys only for compatibility:

- `manual`: operator override;
- `llm`: always `None` numerically;
- `ml`: promoted residual correction only;
- `baseline`: V2 core;
- `panel`: broad event-prior fallback.

Selection is manual, promoted residual, core, panel. Numeric LLM prediction is retired;
LLMs remain narrative-only. See [`../../PREDICTION_ENGINE_V2.md`](../../PREDICTION_ENGINE_V2.md).
