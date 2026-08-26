# Prior-Only Recency and Competitor State

> **V2-specific behavior.** The V3 release candidate uses historical cutoffs plus frozen
> tournament/round epochs and a different capability mechanism. Its checked-in evidence
> is rehearsal-tier; V2 remains production authority until explicit cutover.

V2 applies 730-day exponential recency weighting only to results with
`result_date < prediction_as_of`. Same-day, future, invalid-date, and undated results
are excluded before any state is calculated.

Recency is one part of a partially pooled competitor-event state. Sparse personal
history is shrunk strongly toward the event/population prior; support grows smoothly as
prior evidence accumulates. The core also uses a bounded trend and validated cross-event
borrowing from earlier rows.

The retired 65/80/90/97% same-tournament weighting is not active. Round identity and
same-tournament provenance cannot currently be proved, so `tournament_time`, round
counts, and heat IDs are numeric no-ops in V2.

This rule prevents leakage in both training folds and live backdated requests. See
[Prediction Engine V2](Prediction-Engine-V2).
