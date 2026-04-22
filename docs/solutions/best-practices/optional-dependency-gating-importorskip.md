---
type: knowledge
problem_type: best_practice
severity: high
tags:
  - "packaging"
  - "ci"
  - "optional-extras"
  - "pytest"
  - "pyproject"
confidence: high
created: 2026-04-21
source: "v0.3.1 CI setup session + Apr 6 ModuleNotFoundError incident"
---

# Optional Dependency Gating with `pytest.importorskip`

## Context
STRATHMARK's base install is intentionally lean: `pandas`, `numpy`, `requests`, `openpyxl`. Heavy dependencies (FastAPI, XGBoost, Supabase, Ollama Python client, Gemini SDK) live in opt-in extras so downstream consumers (STRATHEX, Missoula-Pro-Am-Manager, bare-install library users) can pull the calculation core without dragging in ML wheels or web frameworks. This only works if tests for those optional subsystems do not crash CI when the extras aren't installed.

The wrong pattern is what caused the first CI run to fail: `test_api.py` imported FastAPI at module top, so pytest collection aborted with `ModuleNotFoundError: No module named 'fastapi'` before any of the rest of the suite ran. A single unconditional import of an optional dep takes the whole suite down.

## Pattern

### 1. Declare extras by domain in `pyproject.toml`
Split optional deps by purpose — `api`, `llm`, `ml`, `db`, `dev`. Base `dependencies = [...]` holds only what `import strathmark` touches at module load time. See [`pyproject.toml`](../../../pyproject.toml) `[project.optional-dependencies]` for the current split.

### 2. Lazy-import the optional dep inside functions, not at module top
See [`strathmark/llm.py`](../../../strathmark/llm.py) `_call_gemini()` for the canonical pattern — `import google.generativeai` happens inside the function with a one-shot `ImportError` warning flag so a cascade doesn't log the same error once per competitor.

### 3. Guard tests with `pytest.importorskip` — placement matters

**Module-level guard** — when the entire test file needs the optional dep. Place the `importorskip` call BEFORE any `from <optional_dep> import ...` line; otherwise the `from` import runs first and crashes collection:

```python
# tests/test_api.py:1-12
"""Tests for strathmark/api.py — FastAPI REST endpoints."""

import pytest

pytest.importorskip(
    "fastapi",
    reason="FastAPI not installed -- install with: pip install -e '.[api]'",
)

from fastapi.testclient import TestClient  # noqa: E402
from strathmark.api import app  # noqa: E402
```

The `# noqa: E402` is required because imports-after-statements violates the default ruff rule. The ruff config opts out of E402 project-wide for this reason (see `pyproject.toml [tool.ruff.lint]`).

**Function-level guard** — when only some tests in the file need the optional dep. Put the `importorskip` inside the test body:

```python
# tests/test_predictor_extended.py:102-111
def test_train_with_sufficient_data(self):
    pytest.importorskip(
        "xgboost",
        reason="xgboost not installed -- install with: pip install -e '.[ml]'",
    )
    df = _make_training_df(n=250)
    ml = MLModel()
    result = ml.train(df)
    assert result is True
```

## Rationale
- Base `[dev]` install runs ~650 tests without pulling any optional extras. CI on a bare `[dev]` environment stays green.
- Downstream projects can `pip install strathmark` and import `HandicapCalculator` without also installing FastAPI or XGBoost they don't need.
- The module-level-import gotcha is the failure mode that caused the v0.3.1 CI cycle to redo this pattern across multiple files. Documenting it prevents re-stepping on the same rake.

## When to Apply
- Adding a new test file that exercises an optional extra
- Adding a new `[project.optional-dependencies]` entry
- Anytime you see `from <optional_dep> import` at a test's module top without a guard above it

## Examples
Current gated tests:
- [`tests/test_api.py`](../../../tests/test_api.py) — module-level guard for FastAPI
- [`tests/test_predictor_extended.py:104`](../../../tests/test_predictor_extended.py) — function-level guard for XGBoost

Lazy import in source:
- [`strathmark/llm.py`](../../../strathmark/llm.py) — `google.generativeai` lazy-imported; missing package is a graceful no-op (cascade skips Gemini tier)

## Anti-patterns
- `from fastapi.testclient import TestClient` at module top in an FastAPI test — crashes pytest collection when `[api]` is missing
- Catching `ImportError` at module top and setting a sentinel — bloats the test file and loses the pytest skip reason
- Moving optional deps into base `dependencies = [...]` to "make tests simpler" — breaks the lean-install promise for downstream consumers
