# Testing

STRATHMARK ships with 667 passing tests spread across 40 test files
covering every module, every cascade level, every invariant, and
every regression that has ever been fixed.

## Install and run

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

The base `[dev]` install runs the core suite and gracefully skips any
test that imports a missing optional extra (FastAPI, Ollama, XGBoost,
Supabase). To exercise everything:

```bash
pip install -e ".[dev,api,llm,ml,db]"
pytest tests/ -v
```

Typical runtime: ~30–60 seconds on a modern laptop.

## Test layout

| File | Scope |
|------|-------|
| `test_calculator.py`, `test_calculator_extended.py` | mark floor/ceiling, gap logic, rounding invariants |
| `test_variance.py`, `test_variance_extended.py` | absolute variance, consistency ratings, Monte Carlo determinism |
| `test_integration.py`, `test_integration_extended.py` | full pipeline — loader → cascade → simulate → audit |
| `test_predictor.py`, `test_predictor_extended.py` | cascade priority, manual override precedence, tournament weighting |
| `test_predictor_regression.py` | regressions for every cascade bug ever fixed |
| `test_wood.py`, `test_wood_extended.py`, `test_wood_boundary.py` | species lookup, diameter scaling, quality adjustment |
| `test_decay.py`, `test_decay_extended.py` | time-decay math, adaptive half-life, MAD clipping |
| `test_fallback.py`, `test_fallback_extended.py` | panel marks, baseline fallback |
| `test_fairness.py`, `test_fairness_extended.py` | rating bands, variance-imbalance warnings |
| `test_llm.py`, `test_llm_roles.py`, `test_llm_roles_extended.py` | connection caching, prompt assembly, JSON schema enforcement |
| `test_store.py`, `test_store_extended.py` | SQLite CRUD, duplicates, DataFrame round-trip |
| `test_db.py`, `test_db_extended.py` | Supabase push/pull (mocked), bias tracking |
| `test_loader.py`, `test_loader_extended.py` | Excel workbook parsing, column aliases |
| `test_utils.py`, `test_utils_extended.py` | column standardisation, prediction accuracy scoring |
| `test_analytics.py`, `test_analytics_extended.py` | backtesting, competitor profiling |
| `test_visualization.py`, `test_visualization_extended.py` | ASCII bar chart accuracy, line widths |
| `test_config.py`, `test_config_invariants.py` | frozen dataclass enforcement, threshold ordering |
| `test_deployment_fallbacks.py` | cascade behaviour when Ollama / Supabase / ML are unreachable |
| `test_api.py` | every REST endpoint via FastAPI `TestClient` |
| `test_regression.py`, `test_boundary.py` | catch-all regression + extreme-value boundary cases |

## What the invariant tests guarantee

Running `pytest` and seeing green means:

- **Mark floor and ceiling** are never breached. Front markers always
  get exactly Mark 3; back markers never exceed 183 (or the event
  ceiling if lower).
- **Gap logic** is correct. `mark == 3 + round(slowest − predicted)`
  for every competitor in every tested field.
- **Banker's rounding** is in effect (`round()` on float gap).
- **Absolute variance** is enforced. Monte Carlo entries with a
  proportional-variance shape raise in the test suite.
- **Cascade priority** holds: manual overrides beat LLM beat ML beat
  baseline beat panel. Every permutation is tested in
  `test_predictor.py`.
- **Time-decay half-life** is the configured value (730 days
  standard; 365 / 730 / 1095 adaptive).
- **Tournament weighting** graduates 65 / 80 / 90 / 97 %.
- **Panel fallback** divisions map to the expected default marks.
- **Plain-text output** contains no ANSI escape codes; the start
  sheet is exactly 70 characters wide.

## Running a subset

Quick mark-math check:

```bash
pytest tests/test_calculator.py tests/test_calculator_extended.py -v
```

Cascade regression check:

```bash
pytest tests/test_predictor.py tests/test_predictor_regression.py -v
```

Deployment-fallback smoke:

```bash
pytest tests/test_deployment_fallbacks.py -v
```

## Coverage

```bash
pytest tests/ --cov=strathmark --cov-report=html
open htmlcov/index.html
```

Target coverage is 85 %+. The current suite runs around 90 % line
coverage across the `strathmark` package; optional modules (API, LLM
roles, DB) drop into the 70–85 % band depending on which extras are
installed.

## Lint

```bash
ruff check strathmark tests
ruff format --check strathmark tests
```

Ruff config is in `pyproject.toml`. A few intentional rule overrides:

- `E501` — long lines in docstring tables.
- `E402` — late imports in `predictor.py`, `variance.py`, `wood.py`
  to gate optional ML deps.
- `F841` — test locals bound for clarity without assertion.

## Continuous integration

GitHub Actions workflow `.github/workflows/ci.yml` runs:

- `lint` — ruff check + format.
- `test` matrix — Python 3.10, 3.12, 3.13 across Ubuntu and Windows,
  with coverage.
- `build` — verify the wheel installs cleanly and
  `from strathmark import HandicapCalculator` works.

Every push and every PR runs the full matrix. Downstream projects
(STRATHEX, Pro-Am Manager) depend on the published wheel and the CI
is their safety net.
