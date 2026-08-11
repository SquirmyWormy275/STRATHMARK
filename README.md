<p align="center">
  <img src="https://raw.githubusercontent.com/SquirmyWormy275/STRATHMARK/main/assets/strathmark_logo.png" alt="STRATHMARK" width="480"/>
</p>

# STRATHMARK

Pip-installable Python library for AAA-compliant woodchopping handicap calculation. Production-tested across multiple tournament management consumers and covered by an automated cross-platform test suite.

Originally extracted from [STRATHEX](https://github.com/SquirmyWormy275/STRATHEX), the public AI-powered handicap calculator. STRATHMARK now powers handicap mark generation across STRATHEX and multiple private timbersports applications that need AAA-compliant marks without the full STRATHEX runtime footprint (XGBoost, Ollama, the 500K-race Monte Carlo engine).

## Why this exists

STRATHEX evolved from a tournament-day calculator into a full management system: result ingestion, championship simulation, fairness assessment, an LLM-driven analyst tier. That growth was right for STRATHEX but wrong for downstream consumers. A scoring laptop at a regional event needs the calculation core, not the surrounding 50-MB ML stack. A separate Pro-Am tournament manager needs the same handicap math, not a copy of the STRATHEX pipeline. STRATHMARK was extracted so the calculation logic lives in exactly one place and propagates outward by version pin.

The library enforces AAA rules at the boundary, not at the consumer level. The 3-second mark floor, 183-second ceiling, absolute (not proportional) variance model, and round-half-to-even gap arithmetic are constants in `strathmark/config.py` and are read by every code path that produces a mark. A consumer cannot produce a non-compliant mark even by accident, because the library refuses to produce one. Compliance enforcement at the library layer means downstream applications inherit it for free and cannot regress it without changing the library itself.

## Installation

```bash
pip install strathmark               # core calculation engine
pip install strathmark[api]          # FastAPI REST server
pip install strathmark[ml]           # XGBoost / LightGBM / scikit-learn predictor
pip install strathmark[llm]          # Ollama Python client
pip install strathmark[db]           # Supabase / PostgreSQL backend
pip install strathmark[dev]          # pytest + ruff
```

The base install pulls in only `pandas`, `numpy`, `requests`, and `openpyxl`. Heavy ML and database dependencies are optional extras and lazily imported, so the prediction cascade falls back to the panel/baseline tier when those packages are not present.

## Usage

```python
from datetime import date
from strathmark import HandicapCalculator
from strathmark.predictor import CompetitorRecord, WoodProfile, HistoricalResult

competitors = [
    CompetitorRecord(
        name="Alice",
        history=[
            HistoricalResult("SB", 28.4, "Pine", 300, 5, date(2025, 3, 1)),
            HistoricalResult("SB", 27.9, "Pine", 300, 5, date(2024, 11, 15)),
        ],
    ),
    CompetitorRecord(
        name="Bob",
        history=[HistoricalResult("SB", 35.2, "Pine", 300, 5, date(2025, 3, 1))],
    ),
]

wood = WoodProfile(species="Pine", diameter_mm=300, quality=5)
results = HandicapCalculator().calculate(competitors, wood, event_code="SB")
```

`results` is a list of `MarkResult` objects (defined in `strathmark.calculator`), one per competitor, carrying `name`, `predicted_time`, `mark` (assigned handicap in seconds, AAA-clamped to the 3-to-183 second range), `method_used` (which tier of the cascade produced the prediction), `confidence`, `explanation`, and `std_dev`.

## API reference

The public surface is exposed at the package root:

- `HandicapCalculator`: full mark assignment from competitor history and wood profile
- `process_competition_day`: high-level pipeline that predicts, assigns marks, runs the simulation, and audits the result
- `get_best_prediction` / `get_all_predictions`: direct access to the Manual > LLM > ML > Panel cascade
- `run_monte_carlo_simulation` / `audit_mark_sheet`: 500K-race fairness simulation against a generated mark sheet
- `ResultStore`: local SQLite history at `~/.strathmark/results.db`
- `push_results` / `pull_results`: Supabase sync for tournament-day backends

See `strathmark/__init__.py` for the full `__all__` list and the [wiki](https://github.com/SquirmyWormy275/STRATHMARK/tree/main/docs/wiki) for module-level reference.

## How it relates to STRATHEX

STRATHMARK is the calculation core. STRATHEX is one consumer of that core and the largest public reference implementation. The two repositories share heritage (STRATHMARK was extracted from STRATHEX in commit `5416342`) and stay synchronized through a cross-reference table documented in [`docs/ARCHITECTURE.md`](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/ARCHITECTURE.md). When a STRATHMARK module changes, the table identifies which STRATHEX file consumes it, so a behavior change cannot silently drift across the boundary.

## Production usage

STRATHMARK powers handicap mark generation in production at AAA-sanctioned timbersports events including the Missoula Pro-Am and Mason County Western Qualifier (via STRATHEX), and in multiple private timbersports applications I maintain that need AAA-compliant marks without the full STRATHEX runtime. The downstream STRATHEX system, which composes STRATHMARK calculations with its simulation logic, achieves 0.3-second finish-time spreads on Standing Block and 0.8-second spreads on Underhand against an industry-acceptable target of under 1 second. STRATHMARK provides the calculation primitives that downstream applications compose into systems achieving sub-second fairness spreads at AAA-sanctioned events.

## Testing

`pytest tests/ -v` runs the calculator invariants, variance, predictor cascade, integration, fairness, persistence, LLM, ML, and regression suites. Tests requiring optional extras skip gracefully when those packages are not installed. CI runs the core matrix on Ubuntu and Windows across Python 3.10, 3.12, and 3.13, plus an API contract job with the `api` extra and an isolated built-wheel smoke test.

## Documentation

- [`docs/ARCHITECTURE.md`](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/ARCHITECTURE.md): module structure, design decisions, and the STRATHEX cross-reference table
- [`docs/wiki/`](https://github.com/SquirmyWormy275/STRATHMARK/tree/main/docs/wiki): 19 reference pages covering cascade, variance, wood, decay, fairness, deployment
- [`docs/solutions/`](https://github.com/SquirmyWormy275/STRATHMARK/tree/main/docs/solutions): institutional knowledge organized by category (bugs solved, decisions made, patterns followed)
- [`CONTRIBUTING.md`](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/CONTRIBUTING.md): local development loop, lint, CI
- [`docs/DEPLOYMENT.md`](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/DEPLOYMENT.md): race-day deployment guide

## Design rules

These invariants are enforced in `strathmark/config.py` and read by every mark-producing code path:

- Mark floor: 3 seconds (no exceptions)
- Mark ceiling: 183 seconds (180-second event time limit plus the 3-second minimum mark)
- Variance: absolute plus or minus 3 seconds; proportional variance is forbidden
- Prediction cascade: Manual > LLM > ML > Panel mark fallback
- Time-decay weighting: exponential, 2-year half-life (730 days)
- Tournament weighting: same-tournament results carry 97 percent weight, historical results 3 percent
- Output: plain text only; no emojis, no ANSI color codes
- Gap arithmetic: `mark = 3 + round(gap)` with round-half-to-even, capped at 183

## License

Apache License 2.0. See [LICENSE](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/LICENSE).

The Apache patent grant matters because STRATHMARK has multiple production consumers; the patent protection extends to those consumers (including future external adopters) against patent claims related to the calculation methods.

## About the author

Alex Kaper, MIS senior at the University of Montana, graduating May 2026. I build ML systems that ship, including production timbersports software.

- GitHub: [SquirmyWormy275](https://github.com/SquirmyWormy275)
- Email: <alex.j.kaper@gmail.com>
