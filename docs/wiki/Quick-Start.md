# Quick Start

This page runs a five-competitor Standing Block heat from raw data to a
printed start sheet in under 30 lines of code. Every public type used
below is re-exported from the top-level `strathmark` namespace.

## The three inputs

1. A list of `CompetitorRecord` — one per person in the heat. Each
   record carries the competitor's name, optional division, and a list
   of `HistoricalResult` entries describing their past times on known
   wood.
2. A `WoodProfile` — species, block diameter in millimetres, and a
   quality rating from 1 to 10.
3. An event code — `"SB"` (Standing Block) or `"UH"` (Underhand).

## Minimal example

```python
from datetime import date
from strathmark import HandicapCalculator
from strathmark.predictor import CompetitorRecord, HistoricalResult, WoodProfile

competitors = [
    CompetitorRecord(
        name="Alice",
        history=[
            HistoricalResult("SB", 28.4, "Pine", 300, 5, date(2025, 3, 1)),
            HistoricalResult("SB", 27.9, "Pine", 300, 5, date(2024, 11, 15)),
            HistoricalResult("SB", 29.1, "Pine", 300, 5, date(2024, 6, 20)),
        ],
    ),
    CompetitorRecord(
        name="Bob",
        history=[
            HistoricalResult("SB", 35.2, "Pine", 300, 5, date(2025, 3, 1)),
            HistoricalResult("SB", 36.0, "Pine", 300, 5, date(2024, 11, 15)),
        ],
        division="Open",
    ),
]

wood = WoodProfile(species="Pine", diameter_mm=300, quality=5)

calc = HandicapCalculator()
results = calc.calculate(competitors, wood, event_code="SB")
sheet = calc.build_start_sheet(
    results,
    event_name="300 mm SB",
    event_code="SB",
    wood=wood,
)
print(sheet.render())
```

Output (shape — actual numbers depend on your data):

```
+====================================================================+
|                    START SHEET -- 300 mm SB                        |
|                      Pine  300mm  Quality 5/10                     |
|--------------------------------------------------------------------|
|   MARK    COMPETITOR                      PRED(s)    METHOD   CONF |
|--------------------------------------------------------------------|
|   3       Bob                              35.60s    baseline LOW  |
|   10      Alice                            28.47s    baseline MED  |
|--------------------------------------------------------------------|
|                 Front marker (lowest mark) starts first.           |
+====================================================================+
```

Bob is the front marker (Mark 3, starts first), Alice is the back
marker (Mark 10, waits 7 seconds longer). If both cut to prediction,
they finish simultaneously.

## Loading data from Excel

```python
from strathmark import HandicapCalculator

calc = HandicapCalculator.from_xlsx("woodchopping_clean.xlsx")
# wood_df and results_df are pre-populated; XGBoost trains lazily on the
# first call to .calculate().
```

## Loading data from Supabase

```python
from strathmark import HandicapCalculator, load_woodchopping_xlsx

wood_df, _ = load_woodchopping_xlsx("woodchopping_clean.xlsx")
calc = HandicapCalculator.from_db(wood_df=wood_df)
```

Both constructors accept an optional `event_ceiling` keyword if the
event is capped below 183 s.

## Batch-processing a whole day

```python
from strathmark import process_competition_day, load_woodchopping_xlsx

wood_df, results_df = load_woodchopping_xlsx("woodchopping_clean.xlsx")

events = [
    {
        "event_name": "275 mm SB",
        "event_code": "SB",
        "species": "Pine",
        "diameter_mm": 275,
        "quality": 5,
        "competitors": sb_entries,
        "wood_df": wood_df,
        "results_df": results_df,
    },
    {
        "event_name": "300 mm UH",
        "event_code": "UH",
        "species": "Pine",
        "diameter_mm": 300,
        "quality": 5,
        "competitors": uh_entries,
        "wood_df": wood_df,
        "results_df": results_df,
    },
]

day = process_competition_day(events)
for row in day:
    print(row["start_sheet"].render())
```

## Running a fairness check

```python
from strathmark import run_monte_carlo_simulation

sim = run_monte_carlo_simulation(
    [r.to_simulation_dict() for r in results],
    num_simulations=500_000,
)
print(sim["summary"])
```

Any spread below about 3 percentage points between highest and lowest
win rate means the handicaps are working. See
[Fairness Assessment](Fairness-Assessment) for the thresholds.

## Continue with

- [Architecture Overview](Architecture-Overview) — module-by-module tour
- [Prediction Cascade](Prediction-Cascade) — why ML beat LLM and when it
  falls back to baseline
- [Handicap Mark Math](Handicap-Mark-Math) — the exact `mark = 3 + round(gap)`
  formula, the floor, the ceiling, and the tie rules
