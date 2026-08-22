<p align="center">
  <img src="https://raw.githubusercontent.com/SquirmyWormy275/STRATHMARK/main/assets/strathmark_logo.png" alt="STRATHMARK" width="480"/>
</p>

# STRATHMARK

STRATHMARK is an installable Python engine for AAA-compatible woodchopping
handicaps. Version 2.0.0 uses a reproducible, prior-only statistical model and a
deterministic joint mark optimizer. It works offline and exposes both Python and REST
interfaces.

## Install

```bash
python -m pip install "strathmark @ git+https://github.com/SquirmyWormy275/STRATHMARK.git@v2.0.0"
python -m pip install "strathmark[api] @ git+https://github.com/SquirmyWormy275/STRATHMARK.git@v2.0.0"
```

STRATHMARK 2.0.0 is published as the immutable `v2.0.0` Git tag and GitHub release;
there is no PyPI distribution for this version. Production consumers may pin the
exact release commit for an even stricter lock. Other optional extras are `ml`, `db`,
`llm`, and `dev`.

The NumPy/Pandas V2 core and its validated JSON artifact ship in the base package. No
network, database, LLM, or native ML library is required to calculate marks.

## Quick start

```python
from datetime import date

from strathmark import HandicapCalculator
from strathmark.predictor import CompetitorRecord, HistoricalResult, PredictionContext, WoodProfile

competitors = [
    CompetitorRecord(
        name="Alice",
        competitor_id="competitor-alice",
        gender="F",
        history=[
            HistoricalResult("SB", 28.4, "Pine", 300, 5, date(2025, 3, 1)),
            HistoricalResult("SB", 27.9, "Pine", 300, 5, date(2024, 11, 15)),
        ],
    ),
    CompetitorRecord(
        name="Bob",
        competitor_id="competitor-bob",
        gender="M",
        history=[HistoricalResult("SB", 35.2, "Pine", 300, 5, date(2025, 3, 1))],
    ),
]

wood = WoodProfile(species="Pine", diameter_mm=300, quality=5)
results = HandicapCalculator().calculate(
    competitors,
    wood,
    event_code="SB",
    context=PredictionContext(prediction_as_of=date(2026, 1, 1)),
)

for result in results:
    print(result.name, result.predicted_time, result.mark, result.interval)
```

`calculate()` returns `MarkResult` objects ordered slowest to fastest. Important fields
include `predicted_time`, `mark`, `method_used`, `interval`, performance `std_dev`,
engine/model/calibration versions, optimizer metadata, warnings, and optional trusted
ledger state.

`quality=5` remains required by the legacy `WoodProfile` constructor, but wood quality
is deliberately a numeric no-op in V2 until provenance-backed quality or moisture data
exists.

## Prediction Engine V2

The core models log finish time with robust hierarchical partial pooling. Verified
inputs are competitor identity and strictly prior history, event, result date and
exclusive UTC cutoff, diameter, species physical properties, and gender including
missingness. Zero- and sparse-history competitors pool toward supported population
priors. Chronological conformal intervals stay separate from race-performance
`std_dev`.

Division, round/heat, venue, lane/stand, run order, exact material identity, wood
quality/moisture, weather, equipment, fatigue, penalty/DNF state, same-tournament
weighting, and field strength are accepted where compatibility requires but cannot
change a V2 number. Numeric LLM prediction is retired; LLMs remain narrative-only.

The five legacy keys remain:

- `manual`: uncalibrated operator override;
- `llm`: always `None` numerically;
- `ml`: optional promoted residual learner, inactive in 2.0.0;
- `baseline`: authoritative V2 core;
- `panel`: labeled broad-prior fallback.

A field uses one immutable model bundle. Marks are chosen from 2,048 deterministic
joint posterior samples, subject to the 3-second floor, effective ceiling, monotonic
ordering, and a rounded-gap fallback.

Read [Prediction Engine V2](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/PREDICTION_ENGINE_V2.md)
for the complete evidence, calibration, optimizer, compatibility, and ledger contract.

## Locked benchmark

On the frozen 128-row temporal test set, the V2 core recorded MAE 16.1301 seconds versus
20.5172 for the strict prior-only incumbent (21.38% lower), and RMSE 33.6904 versus
44.4791 (24.26% lower). The central 90% interval covered 94.53% of rows. These figures
describe that fixed workbook and split; they are not a universal accuracy or fairness
claim. Cohort samples are smaller, and the optional residual learner was inactive.

Verify the checked-in report and packaged artifact without reopening the locked data:

```bash
python train_model.py
```

## REST API

```bash
pip install "strathmark[api]"
uvicorn strathmark.api:app --host 127.0.0.1 --port 8000
```

- `GET /health` — core availability and cutoff compatibility, calibration, residual,
  artifact, store, and narrative-LLM health; accepts optional `prediction_as_of`
- `POST /predict` — one stateless prediction and all five compatibility keys
- `POST /calculate` — stateless field prediction and mark assignment
- `POST /simulate` — independent fairness simulation, capped at 250,000 races
- `POST /ledger/calculate` — authenticated, idempotent trusted calculation
- `POST /ledger/predictions/{prediction_id}/settle` — authenticated immutable settlement
- `POST /results` and `GET /results/{competitor_name}` — authenticated local history
- `POST /v1/shadow/calculate` — trusted field calculation or exact receipt recovery
- `POST /v1/shadow/receipts/lookup` — immutable receipt lookup with live status
- `POST /v1/shadow/status` — trust, freshness, mirror, outcome, and drift status
- `POST /v1/shadow/outcomes/apply` — field-atomic settlement or void revision
- `POST /v1/shadow/mirror/replay` — bounded administrator mirror replay
- `POST /v1/shadow/drift` — bounded advisory-only drift report

Public calculation routes do not write trusted training evidence. Ledger and result
routes require `STRATHMARK_API_TOKEN`.

All six `/v1/shadow/*` routes require both a consumer-specific bearer credential and a
short-lived v2 actor attestation. The attestation binds the action, run revision, and
canonical validated request digest. Trusted calculation also requires an attested
`offline-single-writer-durable` topology and a current, integrity-verified local
evidence snapshot. Receipt-bound settlement/void remains available when the current
snapshot is stale or missing so evidence can be corrected safely. See the canonical
[Shadow Consumer Contract](docs/SHADOW_CONSUMER_CONTRACT.md) for roles, request limits,
readiness gates, frozen OpenAPI checksum, and the complete offline lifecycle.

## Stable design rules

- Mark floor: 3 seconds.
- System ceiling: 183 seconds; event configuration may lower it.
- Joint optimizer: deterministic, fixed seed/sample/pass budget, never wall-clock based.
- Fallback mark: `3 + round(slowest - predicted)`, bounded to floor and ceiling.
- Evidence: strictly earlier than one exclusive UTC request cutoff.
- Forecast interval and simulation `std_dev`: different quantities.
- Numeric LLM: prohibited.
- Output: plain text, no terminal-control formatting.

## Documentation

- [Woodchopping Handicap Foundations](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/wiki/Handicap-Mark-Math.md) — mandatory
  domain reading for contributors, agents, and downstream systems
- [Prediction Engine V2](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/PREDICTION_ENGINE_V2.md)
- [Architecture](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/ARCHITECTURE.md)
- [Deployment runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/DEPLOYMENT.md)
- [Shadow Consumer Contract](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/SHADOW_CONSUMER_CONTRACT.md)
- [Wiki source](https://github.com/SquirmyWormy275/STRATHMARK/tree/main/docs/wiki)
- [Contributing](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/CONTRIBUTING.md)
- [Changelog](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/CHANGELOG.md)

## Development

```bash
pip install -e ".[dev,api]"
pytest tests -q
ruff check .
ruff format --check .
python train_model.py
```

Tests use isolated temporary databases. Never point tests at a production database.

## License

Apache License 2.0. See [LICENSE](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/LICENSE).

## Author

Alex Kaper — [SquirmyWormy275](https://github.com/SquirmyWormy275)
