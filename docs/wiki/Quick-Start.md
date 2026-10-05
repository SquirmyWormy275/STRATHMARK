# Calculate a sample field

This example uses the V2 Python API and invented competitors. Install the
[V2 library](Installation) first, save the following as `example.py`, then run
`python example.py`.

```python
from datetime import date

from strathmark import HandicapCalculator
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    PredictionContext,
    WoodProfile,
)

field = [
    CompetitorRecord(
        name="Alice",
        competitor_id="example-alice",
        gender="F",
        history=[HistoricalResult("SB", 29.4, "S01", 300, 5, date(2025, 5, 1))],
    ),
    CompetitorRecord(
        name="Bob",
        competitor_id="example-bob",
        gender="M",
        history=[HistoricalResult("SB", 35.0, "S01", 300, 5, date(2025, 4, 20))],
    ),
]

results = HandicapCalculator().calculate(
    field,
    WoodProfile(species="S01", diameter_mm=300, quality=5),
    event_code="SB",
    context=PredictionContext(prediction_as_of=date(2026, 9, 30)),
)

for row in results:
    print(row.name, row.predicted_time, row.mark, row.interval)
```

## Read the result

- `predicted_time` is raw cutting time in seconds.
- `mark` is the starter's count at which the competitor begins.
- `interval` describes uncertainty in the predicted cutting time.

Results are ordered slowest to fastest. Faster expected competitors normally have
larger marks and start later. [The handicap guide](Handicap-Mark-Math) explains why.

The cutoff is exclusive: only dated results before 30 September 2026 are used.
`quality=5` is accepted for compatibility but does not change a V2 prediction.
Keep stable competitor IDs when connecting real data; names can change or collide.

For an event application with review, results and saves, use
[STRATHEX](STRATHEX-Consumer). For Linux V3, use the
[competition setup guide](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md).
