# Installation

STRATHMARK is a standard Python package. Python 3.10 or newer is
required. The base install is deliberately lean — only `pandas`,
`numpy`, `requests`, and `openpyxl` are pulled in by default. Heavy
dependencies (ML libraries, database clients, web server) are gated
behind optional extras and only pulled in when you ask for them.

## From PyPI

```bash
pip install strathmark               # core engine only
pip install strathmark[api]          # FastAPI REST server
pip install strathmark[llm]          # Ollama Python client + Gemini fallback
pip install strathmark[ml]           # XGBoost, LightGBM, scikit-learn
pip install strathmark[db]           # Supabase / PostgreSQL backend
pip install strathmark[dev]          # pytest, pytest-cov, ruff
pip install strathmark[api,llm,ml,db,dev]   # everything
```

## From source (editable install)

```bash
git clone https://github.com/SquirmyWormy275/STRATHMARK.git
cd STRATHMARK
pip install -e ".[dev]"
pytest tests/ -v
```

An editable install is what STRATHEX, the Missoula Pro-Am Manager, and
future tournament managers use during development. Any change to
STRATHMARK becomes visible to downstream projects on the next run — no
rebuild, no reinstall.

## Optional-extras matrix

| Extra     | Packages                                    | Unlocks |
|-----------|---------------------------------------------|---------|
| (base)    | pandas, numpy, requests, openpyxl           | Calculator, predictor cascade (baseline + panel tier), variance, Monte Carlo, SQLite store, Excel loader |
| `api`     | fastapi, uvicorn[standard]                  | `uvicorn strathmark.api:app` — REST endpoints for calculate, predict, simulate, results |
| `llm`     | ollama, google-generativeai                 | LLM prediction tier (local Ollama + Gemini cloud fallback); fairness and commentary LLM roles |
| `ml`      | xgboost, lightgbm, scikit-learn             | ML prediction tier (XGBoost trained on historical data with time-decay sample weights) |
| `db`      | supabase                                    | Supabase/PostgreSQL push/pull, multi-device roster sync |
| `dev`     | pytest, pytest-cov, ruff                    | Full test suite, coverage reports, linting |

Every optional tier is lazily imported. Missing extras never crash — the
cascade simply skips the missing level and falls through. An install
with just the base dependencies will run any downstream tournament
manager that accepts a panel-mark fallback, and will still return valid
marks for every competitor.

## Environment variables

Set these in the event laptop's shell before the first `import
strathmark` call. They are read into frozen dataclasses at module-import
time, so changing them later has no effect.

| Variable | Required for | Default |
|----------|--------------|---------|
| `STRATHMARK_SUPABASE_URL` | DB push/pull | unset |
| `STRATHMARK_SUPABASE_KEY` | DB push/pull | unset |
| `STRATHMARK_DB_PATH`      | local SQLite store path | `~/.strathmark/results.db` |
| `STRATHMARK_OLLAMA_URL`   | legacy full-URL override | `http://localhost:11434/api/generate` |
| `OLLAMA_HOST`             | preferred host-only override (read at call time) | `http://localhost:11434` |
| `STRATHMARK_OLLAMA_CONNECT_TIMEOUT` | TCP connect timeout, seconds | `3` |
| `STRATHMARK_OLLAMA_READ_TIMEOUT`    | HTTP read timeout, seconds | `15` |
| `STRATHMARK_OLLAMA_MAX_RETRIES`     | retry attempts for Ollama | `0` |
| `GEMINI_API_KEY`          | cloud LLM fallback | unset |
| `GEMINI_MODEL`            | cloud LLM model | `gemini-2.0-flash-lite` |

Missing Supabase credentials are not fatal: the cascade falls back to
the local SQLite store, then to the panel-mark fallback. Only
ingestion (`push_results_dicts`, `register_competitor`) strictly
requires Supabase.

## Verify the install

```python
>>> from strathmark import HandicapCalculator
>>> HandicapCalculator.MARK_FLOOR
3
>>> HandicapCalculator.MARK_CEILING
183
>>> import strathmark
>>> strathmark.__version__
'0.4.0'
```

If that works, every downstream feature will work — the heavier tiers
just wait for their extras to be installed.
