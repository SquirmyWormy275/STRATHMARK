# Choose V2 or V3

The choice is made in STRATHEX when you create an event or tournament. V2 and V3
are both available choices when the corresponding runtime is configured.

## What are the choices?

| Choice | Use |
| --- | --- |
| V2 | Established local predictions and handicap calculation. |
| V3 with `LINUX READY` | Configured Linux competition runtime, including review, issue, results and later rounds. |
| V3 with `NUMERIC PREVIEW ONLY` | Older preview profile. Proposed times and marks only; no official issue or results workflow. |
| V3 with `REHEARSAL` | Development service. Does not establish Windows production eligibility. |

Linux V3 uses Formula and trained ML. Its LLM council is unavailable, so affected
fields need explicit degraded or individual review. See [V3](Prediction-Engine-V3).

## When is the choice locked?

A single event chooses during setup. A tournament chooses once at creation;
all of its events, heats and finals inherit that choice. Nothing is selected by
default. The first numeric operation locks the selection.

A later competition can choose a different engine. An existing competition cannot
switch engines to repair a failed calculation. If the selected engine is unavailable,
restore that engine and its original installation before continuing.

## Why does V3 show times before marks?

Seeding comes before exact heats and stands exist. V3 first supplies raw-time
forecasts to sort or group competitors. These forecasts contain no start marks.

Once the actual field and stands exist, V3 calculates the whole field's marks.
Those proposed marks go through review and a separate issue confirmation. A seeding
forecast cannot serve as an official start sheet.

## Resuming an event

Keep the saved competition, original model, signing key and exact runtime together.
Changing the installed source or model can block a saved V3 competition; it does
not silently update its predictions. See [Deployment and recovery](Deployment).

The [Accuracy Preview](Accuracy-Preview) is a separate program, not another engine
choice in the competition selector.
