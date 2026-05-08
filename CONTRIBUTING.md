# Contributing to STRATHMARK

STRATHMARK is the shared handicap calculation core for STRATHEX,
Missoula-Pro-Am-Manager, and any future tournament manager built on the
same engine. Changes here propagate downstream, so the bar for tests and
lint is intentionally strict.

## Development install

```bash
pip install -e ".[dev]"
```

Optional extras you may also want while working on specific subsystems:

- `pip install -e ".[ml]"` — XGBoost / LightGBM / scikit-learn predictor
- `pip install -e ".[db]"` — Supabase backend
- `pip install -e ".[api]"` — FastAPI server
- `pip install -e ".[llm]"` — Ollama Python client

## Running the test suite

```bash
pytest tests/ -v
```

The full suite is 707 tests. Supabase-backed db tests are gated behind
`STRATHMARK_TEST_DB=1` and skip by default. ML predictor paths fall back
to baseline when the `[ml]` extras are not installed, so you do not need
them to get a green run.

## Linting and formatting

```bash
ruff check .
ruff format --check .
```

To auto-fix and apply formatting:

```bash
ruff check . --fix
ruff format .
```

## CI

GitHub Actions runs lint, the full test matrix (Ubuntu + Windows across
Python 3.10 / 3.12 / 3.13), and a wheel build on every push to `main`
and every pull request. **All PRs must pass CI before merge.** The
workflow definitions live under [`.github/workflows/`](.github/workflows/).

## Design rules (non-negotiable)

These are enforced in code and in review. See the README's *Design rules*
section for the full list, but the load-bearing ones are:

- Mark floor: 3 seconds
- Mark ceiling: 183 seconds system-wide
- Variance: absolute +/- 3 seconds only — proportional variance is forbidden
- Prediction cascade: Manual > LLM > ML > Panel mark fallback
- Output: plain text only, no emojis, no ANSI color codes
