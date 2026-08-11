# STRATHMARK architecture

This document covers the *why* behind STRATHMARK's structure: which decisions were deliberate, which constraints drove them, and where the library would change with more time. For module-level reference (function signatures, parameter docs, examples), see [`docs/wiki/`](wiki/).

## Library overview

STRATHMARK takes a list of `CompetitorRecord` objects (each carrying a name and a history of past `HistoricalResult` entries), a `WoodProfile` describing the event's wood (species, diameter, quality), and an event code (`SB` for Standing Block, `UH` for Underhand, `HJ` for Hard Hit, etc.), and produces a list of `CalculationResult` objects with a predicted finish time, a gap to the front marker, and a handicap mark in seconds.

The mark is bounded by AAA-sanctioned woodchopping rules: a 3-second floor and a 183-second ceiling (180-second event time limit plus the 3-second minimum mark). Variance modeling is absolute (plus or minus 3 seconds), never proportional to the predicted time. The prediction cascade is ordered Manual > LLM > ML > Panel mark fallback so the most authoritative source available wins.

Consumers depend on STRATHMARK by version pin. The public API is exposed at the package root and re-exported through `strathmark/__init__.py`. Two main consumers exist today: [STRATHEX](https://github.com/SquirmyWormy275/STRATHEX) (the public AI-powered tournament management system that originally contained this code) and multiple private timbersports applications I maintain. STRATHEX is the largest reference implementation and the canonical example of how to compose STRATHMARK calculations into a full tournament workflow.

## Module structure

The package is organized into six functional groups.

**Calculation core** is the part of the library that any consumer imports first. `strathmark/calculator.py` contains `HandicapCalculator`, the entry point that orchestrates prediction, gap arithmetic, and mark assignment. `strathmark/predictor.py` runs the four-tier prediction cascade. `strathmark/variance.py` implements the 500K-race Monte Carlo simulation used to validate that an assigned mark sheet produces the expected sub-second fairness spread. `strathmark/wood.py` handles species properties, diameter scaling, and quality adjustment. `strathmark/decay.py` applies the exponential 2-year half-life weighting to historical results. `strathmark/fallback.py` provides panel marks and event baselines when no other tier in the cascade produces a prediction.

**Configuration** lives in `strathmark/config.py`. Every invariant (mark floor, ceiling, half-life days, simulation sample count, LLM model name, token limits, timeout values) is a frozen dataclass field. Frozen dataclasses make accidental mutation impossible at runtime and keep the design rules in one auditable file.

**Persistence** has two backends. `strathmark/store.py` is the SQLite local result store at `~/.strathmark/results.db`, used for offline-first race-day deployments. `strathmark/db.py` is the Supabase / PostgreSQL backend used for cross-event ingestion and historical analysis. The split is documented in [`solutions/architecture-decisions/dual-store-sqlite-supabase-split.md`](solutions/architecture-decisions/dual-store-sqlite-supabase-split.md).

**LLM integration** spans `strathmark/llm.py` (Ollama and Gemini connection management, fail-fast cascade behavior on unreachable hosts), `strathmark/llm_roles.py` (extended roles: competitor profiles, race commentary, anomaly detection), and `strathmark/fairness.py` (LLM-based fairness assessment composed on top of the Monte Carlo audit).

**I/O and utilities** are grouped: `loader.py` reads Excel workbooks, `utils.py` standardizes column names and scores prediction accuracy, `analytics.py` supports backtesting and competitor profiling, `visualization.py` renders ASCII summaries.

**API surface** is `strathmark/api.py`, a FastAPI server exposing `/calculate`, `/predict`, `/simulate`, and `/results` endpoints for non-Python consumers.

## API design decisions

Three decisions shape the public API.

**AAA rules enforced at the library boundary, not at the consumer level.** A consumer cannot produce a non-compliant mark by accident, because the library will not produce one. The mark floor (3) and ceiling (183) are constants in `config.py`, read by `_assign_marks()` in `calculator.py`, and clamped before any value leaves the library. Variance is constructed in `variance.py` against an absolute model and the proportional-variance API path does not exist. This puts compliance enforcement in exactly one place. Downstream applications inherit it by depending on STRATHMARK; they cannot regress it without changing the library itself. The alternative (each consumer enforces its own version of the rules) is what STRATHEX did before extraction, and the result was that variance handling drifted between the legacy spreadsheet and the in-app calculator over a year. Centralizing the invariants in a library was the durable fix.

**Minimal public surface, generous internal surface.** The `__all__` list in `strathmark/__init__.py` exposes ~30 names: the calculator class, four data types, six prediction functions, four simulation entry points, and the I/O helpers. Internal helpers (the cascade inner loops, the variance model's MAD clipping, the wood scaling exponent calibration) are not re-exported. Consumers can still import them via the module path if they need to, but the package root advertises only the surface that is contract-stable. This means I can refactor `predictor.py`'s internals freely without breaking downstream code.

**Optional dependencies as extras with lazy imports, not hard requirements.** Heavy dependencies (XGBoost, LightGBM, scikit-learn, Supabase, FastAPI, Ollama, Gemini) live in `[ml]`, `[db]`, `[api]`, and `[llm]` extras. Inside the package, those imports happen at function-call time, not module-import time. The cascade falls through gracefully when an extra is not installed: missing `ml` skips the ML tier, missing `llm` skips the LLM tier, and the Panel fallback always works. A scoring laptop at a regional event can run STRATHMARK with only `pandas`, `numpy`, `requests`, and `openpyxl` installed (~30 MB of dependencies) and still produce valid AAA-compliant marks.

## Cross-reference with STRATHEX

STRATHMARK was extracted from STRATHEX in commit `5416342`. The two repositories share heritage and continue to share calculation logic. The mapping below names which STRATHEX file each STRATHMARK module replaced, so a behavior change cannot silently drift across the boundary.

| STRATHEX source | STRATHMARK module |
|---|---|
| `woodchopping/handicaps/calculator.py` | `calculator.py` |
| `woodchopping/predictions/baseline.py` | `predictor.py` + `decay.py` |
| `woodchopping/predictions/prediction_aggregator.py` | `predictor.py` |
| `woodchopping/predictions/ai_predictor.py` | `predictor.py` (LLM tier) + `llm.py` |
| `woodchopping/predictions/ml_model.py` | `predictor.py` (ML tier) |
| `woodchopping/predictions/diameter_scaling.py` | `wood.py` |
| `woodchopping/simulation/monte_carlo.py` | `variance.py` |
| `woodchopping/predictions/calibration.py` | `predictor.py` |
| `woodchopping/data/preprocessing.py` | `predictor.py` (ML feature engineering) |
| `config.py` | `config.py` |

The drift discipline is human-enforced today: when a STRATHMARK module changes, I check the corresponding STRATHEX file in the table to confirm the consumer either bumps its dependency pin or absorbs the change. This works because both repositories are solo-maintained. With external contributors it would not, which is one of the items in the section below.

## Dependency philosophy

STRATHMARK targets heterogeneous deployment environments: laptops at events (Windows, macOS), cloud workers (Linux containers on Railway and Fly.io), and embedded scoring devices that may not have a recent Python toolchain. The dependency strategy is shaped by that constraint, not by raw computational speed.

The base install is pure Python plus four mature scientific libraries (pandas, numpy, requests, openpyxl). XGBoost, LightGBM, and scikit-learn are optional extras because they pull in compiled native code that fails to install cleanly on locked-down event laptops. The LLM tier is optional because Ollama is not always reachable on race day (firewalls, captive portals). Supabase is optional because the local SQLite store is sufficient for single-event use.

The cascade design (Manual > LLM > ML > Panel) means the prediction quality degrades gracefully with available extras. A laptop with only the base install still produces marks; a server with all extras gets the same marks plus better predictions plus historical analytics plus narrative commentary. The base install never fails closed.

## What I would do differently

A few decisions look different in hindsight.

**Versioning after the 1.0.0 release.** STRATHMARK reached 1.0.0 after production use established its public calculator API as stable. Future behavior changes to the documented public surface now require normal semantic-versioning discipline and downstream-consumer review.

**Manual cross-reference table maintenance.** The mapping in *Cross-reference with STRATHEX* lives in this document and in the developer's head. With one maintainer, that is enough. With two or more, it is not. The durable fix would be a script that parses STRATHEX imports, resolves them against STRATHMARK's `__all__`, and reports any STRATHEX file referencing a STRATHMARK module that the table does not list.

**Root-level scripts.** `train_model.py`, `evaluate_llm_prompts.py`, and `import_legacy.py` live at the repository root next to `pyproject.toml` rather than under `scripts/` (which exists and houses other scripts). The placement is a small repo-hygiene drift from when the project was a single-file calculator.

**PyPI publication is manual.** The `publish.yml` workflow runs only on `workflow_dispatch`. A tag-push trigger (`v*`) would automate releases and remove a step from the ship checklist. Trusted publishing is already configured; flipping the trigger is one line of YAML.

**The dual-store split is convention, not enforced.** `store.py` and `db.py` are independent classes with overlapping responsibility. A cleaner design would expose a single `Repository` protocol and let consumers choose the implementation. Today the choice happens by which import you write, which is fine but invites drift.
