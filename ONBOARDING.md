# Onboarding

STRATHMARK 2.0.0 is an offline-capable woodchopping prediction and handicap-mark
engine. Start with the current mechanism, not the historical cascade documents.

## First five minutes

```bash
pip install -e ".[dev,api]"
pytest tests -q
python train_model.py
```

The last command verifies the published V2 report and packaged model checksum. It does
not retrain or reopen the locked test.

Read in this order:

1. [`README.md`](README.md) — install and public usage.
2. [`docs/PREDICTION_ENGINE_V2.md`](docs/PREDICTION_ENGINE_V2.md) — active evidence,
   model, uncertainty, optimizer, ledger, and migration contract.
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module and request data flow.
4. [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — race-day runbook and rollback.
5. [`docs/SHADOW_CONSUMER_CONTRACT.md`](docs/SHADOW_CONSUMER_CONTRACT.md) — the
   frozen six-route trusted-shadow boundary, dual authentication, readiness, and
   recovery contract.

## Mental model

One request captures one exclusive UTC cutoff and one immutable model bundle. V2 uses
only stable identity/history, event, dated strictly prior results, diameter, species
physical properties, and gender including missingness. It predicts a positive finish
time and calibrated interval. A deterministic 2,048-sample joint optimizer then assigns
marks for the whole field.

Manual time overrides are operator instructions and have no calibrated interval. The
numeric LLM tier is retired. The compatibility key `llm` remains but is always `None`;
LLM modules are narrative-only.

## Non-negotiable rules

- Mark floor 3 and system ceiling 183; an event may use a lower ceiling.
- Same-day, future, invalid-date, and undated rows never enter V2 evidence.
- Forecast intervals are not race-performance `std_dev`.
- Inactive context fields are numeric no-ops; never infer or backfill them.
- One bundle snapshot serves the whole field.
- Trusted prediction writes require stable competitor IDs and an idempotency key.
- Public `/calculate` and `/predict` remain stateless.
- Database or mirror failure must not block a valid race-day calculation.
- Trusted `/v1/shadow/calculate` requires a durable single-writer topology and a
  current verified local evidence snapshot; never claim either from an ephemeral
  deployment.
- Every `/v1/shadow/*` request uses both a scoped service bearer credential and a
  short-lived v2 actor attestation bound to the exact canonical request digest.
- Receipt-bound settlement and void remain available when current evidence becomes
  stale or unavailable, so incorrect numeric evidence can still be retracted.
- Tests must use temporary, isolated databases and never production.

## Code map

| Responsibility | Source | Primary tests |
| --- | --- | --- |
| Prior-only allowlist and exclusions | `strathmark/features.py` | `tests/test_features.py` |
| Hierarchical core and calibration | `strathmark/prediction_v2.py` | `tests/test_prediction_v2.py`, `tests/test_validation.py` |
| Optional residual gate | `strathmark/residual.py` | `tests/test_residual.py` |
| Compatibility projection/provider | `strathmark/predictor.py` | `tests/test_predictor*.py` |
| Field orchestration | `strathmark/calculator.py` | `tests/test_calculator*.py` |
| Joint mark optimizer | `strathmark/mark_optimizer.py` | `tests/test_mark_optimizer.py` |
| Trusted append-only ledger | `strathmark/ledger.py` | `tests/test_ledger.py` |
| REST routes and auth | `strathmark/api.py` | `tests/test_api.py` |
| Trusted shadow receipts and status | `strathmark/shadow.py`, `strathmark/auth.py` | `tests/test_shadow_api.py`, `tests/test_shadow_consumer_contract.py` |
| Release validation | `scripts/validate_v2.py` | `tests/test_validate_v2.py` |
| Packaged artifact | `strathmark/models/prediction_v2_core.json` | artifact and wheel tests |

## Common changes

### Fix a prediction bug

Reproduce it with a test. If the result looks impossible despite matching source, clear
stale `__pycache__` directories before deeper diagnosis. Preserve the cutoff and active
feature allowlist; a fix must not make an inactive legacy field numerically active.

### Add a factor

Do not add a coefficient directly. A factor needs provenance-backed capture, a versioned
allowlist/schema change, prior-only folding, leakage tests, frozen temporal comparison,
calibration review, docs, and a new artifact. Division, heat, venue, lane, run order,
material identity, quality/moisture, weather, equipment, fatigue, and penalty/DNF are
specifically deferred.

### Change the optimizer

Preserve determinism, 2,048 common-random samples, bounded integer marks, monotonicity,
input-order ties, at least one Mark 3, and the "not worse than rounded-gap" guard unless
a new versioned contract and benchmark explicitly replaces them.

### Change persistence

Local SQLite is the trusted race-day authority. Keep ledger rows append-only, retain
idempotency and stable-ID checks, and use a checked-in migration for Supabase changes.
Public stateless routes must not acquire writes.

## Release checks

```bash
pytest tests -q
ruff check .
ruff format --check .
python train_model.py
python -m build
```

The optional-ML CI job installs CatBoost and tests safe residual artifacts. An installed
library is not enough to activate the residual model; promotion evidence is required.

Do not run `python train_model.py --open-locked-test` for the existing 2.0.0 benchmark.
The role has already been opened once, and the checked-in final report intentionally
prevents a rerun.

## Operations

- Deployment and recovery: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- API examples: [`docs/wiki/REST-API.md`](docs/wiki/REST-API.md)
- Frozen shadow boundary: [`docs/SHADOW_CONSUMER_CONTRACT.md`](docs/SHADOW_CONSUMER_CONTRACT.md)
- Persistence and privacy: [`docs/wiki/Persistence-and-Database.md`](docs/wiki/Persistence-and-Database.md)
- Historical decisions: [`docs/solutions/`](docs/solutions/) — check each page's status;
  the old numeric cascade and same-tournament weighting are superseded.
