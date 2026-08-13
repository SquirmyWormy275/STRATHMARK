# Quick Start

```python
from datetime import date

from strathmark import HandicapCalculator
from strathmark.predictor import CompetitorRecord, HistoricalResult, PredictionContext, WoodProfile

field = [
    CompetitorRecord(
        name="Alice",
        competitor_id="alice-stable-id",
        gender="F",
        history=[HistoricalResult("SB", 29.4, "Pine", 300, 5, date(2025, 5, 1))],
    ),
    CompetitorRecord(
        name="Bob",
        competitor_id="bob-stable-id",
        gender="M",
        history=[HistoricalResult("SB", 35.0, "Pine", 300, 5, date(2025, 4, 20))],
    ),
]

results = HandicapCalculator().calculate(
    field,
    WoodProfile("Pine", 300, 5),
    "SB",
    context=PredictionContext(prediction_as_of=date(2026, 1, 1)),
)

for row in results:
    print(row.name, row.predicted_time, row.mark, row.interval, row.optimizer)
```

The result list is slowest-to-fastest. The date is exclusive: only results before
2026-01-01 can contribute. `quality=5` is required by the compatible constructor but is
a V2 numeric no-op.

`interval` is calibrated forecast uncertainty. `std_dev` is separate race-performance
variability. Marks come from a deterministic 2,048-sample field optimizer, not by
independently rounding each prediction unless the optimizer falls back.

Stable IDs are strongly recommended and required for trusted ledger calls. A display
name is not a durable identity.
