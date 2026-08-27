---
title: Tournament Weighting and Decay
status: partially-superseded-by-v2
superseded_date: 2026-08-11
superseded_by: docs/PREDICTION_ENGINE_V2.md
---

# Tournament Weighting and Decay — V2 Status

> V3 supersedes this as future architecture with frozen round epochs, between-round
> updates, capability state, and accuracy-earned credibility. See
> [`../../PREDICTION_ENGINE_V3.md`](../../PREDICTION_ENGINE_V3.md). The rules below remain
> exact for V2, which retains production authority until explicit cutover.

The pre-2.0 graduated 65/80/90/97% same-tournament blend is retired. Existing history
does not prove round/heat identity or same-tournament provenance well enough to use it
as numeric evidence. `tournament_time`, tournament results, heat IDs, and round counts
are compatibility no-ops in V2.

The 730-day exponential recency concept remains active, but only inside the V2
prior-only partially pooled competitor state. Every contributing result must have
`result_date < prediction_as_of`; same-day, future, invalid-date, and undated rows are
excluded.

Future tournament software may capture round and material provenance, but activation
requires a new allowlist/model version and frozen temporal validation. See
[`../../PREDICTION_ENGINE_V2.md`](../../PREDICTION_ENGINE_V2.md).
