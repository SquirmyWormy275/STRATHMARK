---
title: Tournament Weighting Audit TODO-011
status: closed-superseded-by-v2
closed_date: 2026-08-11
superseded_by: docs/PREDICTION_ENGINE_V2.md
---

# Tournament Weighting Audit TODO-011 — Closed

> This closure remains correct for V2. V3 implements a different round-epoch and
> credibility system; see [`../../PREDICTION_ENGINE_V3.md`](../../PREDICTION_ENGINE_V3.md).
> It does not reopen the retired fixed-percentage blend.

This pre-2.0 audit asked whether fixed same-tournament weighting was double-counted
between baseline and ML paths. Prediction Engine V2 removes that architecture and makes
same-tournament/round inputs numeric no-ops because their provenance cannot currently be
proved.

No active V2 blend can double-count the retired 65/80/90/97% weights. The current model
uses only strictly prior dated history with 730-day recency inside partially pooled
competitor state. Reintroducing tournament context requires provenance-backed capture,
a new allowlist/version, leakage tests, and frozen temporal validation.

See [`../../PREDICTION_ENGINE_V2.md`](../../PREDICTION_ENGINE_V2.md).
