# Recency and personal history in V2

V2 uses dated results strictly before the prediction cutoff. Same-day, future,
undated and invalid-date rows are excluded before personal state is calculated.

Older admitted results gradually receive less weight, using a 730-day exponential
recency scale. The model combines personal same-event history with a population
estimate. A competitor with little history therefore stays closer to the broad
prior; stronger personal evidence grows with support.

V2 also uses bounded trend and validated borrowing from earlier cross-event results.
It does not apply the old 65/80/90/97% same-tournament weights. Heat IDs, round counts
and `tournament_time` do not change V2 numbers.

STRATHEX recalculates a V2 final with the event's saved original date cutoff.
Direct library callers must supply the same `PredictionContext.prediction_as_of`
for each calculation; the stateless library does not remember a competition's
previous cutoff. A later cutoff can admit newer dated results.

Linux V3 has a different policy: all fields in a round share frozen evidence,
and valid settled results can contribute at a later-round boundary.

See [V2](Prediction-Engine-V2) and [V3](Prediction-Engine-V3) for their full behavior.
