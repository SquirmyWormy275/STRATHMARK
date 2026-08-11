# Persistence and Database

STRATHMARK has three storage tiers under the controlled-write
architecture introduced in 2026-05-04:

1. **MNEMEX Supabase** — the universal archive of record for ALL
   timbersports results across ALL disciplines. Canonical source of
   truth for chopping data. Lives in a separate Supabase project from
   STRATHMARK and is not queried at prediction time.
2. **STRATHMARK Supabase** — a hydrated chopping cache (subset of
   MNEMEX, filtered to chopping disciplines and non-provisional rows)
   plus internal ML state (model versions, calibrations, feature
   vectors, predictions, residuals).
3. **Local SQLite** — event-day cache and per-competitor history. Used
   by the prediction cascade so marks compute regardless of cloud state.

This document describes how the three tiers interact, what writes to
what, and what the failure modes look like.

## Data flow across a tournament day

```
  +----------+      sync function     +-------------+
  |  MNEMEX  | ---------------------> | STRATHMARK  |
  | Supabase |   (authority -> cache) |  Supabase   |
  +----------+                        +-------------+
       ^                                    ^
       |                                    | pull_results()
       | finalization push                  v
  +----------+                        +-------------+         +---------+
  | STRATHEX |                        | STRATHMARK  | <-----> | SQLite  |
  | (events) |                        |  cascade    |         | (local) |
  +----------+                        +-------------+         +---------+
                                              ^                   ^
                                              |                   |
                                         offline fallback ---------
```

MNEMEX is the authority. STRATHMARK Supabase is a downstream consumer:
it never writes canonical results data of its own, only ML state. The
prediction cascade reads STRATHMARK Supabase (or local SQLite if
Supabase is unreachable) and never reaches across to MNEMEX directly
during prediction.

### Sync triggers (three paths)

1. **Nightly batch (3am UTC).** Pulls scraper output and federation
   backfill uploads from MNEMEX, upserts to the STRATHMARK cache.
   See `strathmark.sync.nightly_batch()`.
2. **STRATHEX finalization push.** When STRATHEX finalizes an event,
   it writes to MNEMEX and fires a focused sync for that event's
   chopping rows. Sub-minute latency. Solves the "Friday-into-Saturday
   show" case where new data needs to be available for tomorrow's
   marks. See `strathmark.sync.strathex_finalization(event_id)`.
3. **Manual force-sync.** Admin tool. Two clicks. Used when an
   operator imports a scorebook batch and tomorrow's event needs the
   data immediately. See `strathmark.sync.manual_force_sync(show_name)`.

All three paths route through the same low-level upsert. The
`sync_path` column on `sync_log` records which path produced each
batch.

## STRATHMARK Supabase tables

### Cache (sourced from MNEMEX)

- **`competitors`** — roster (cached). PK `competitor_id`.
  `mnemex_id` column links to canonical MNEMEX record.
- **`results`** — historical times (cached). FK to `competitors`.
  `mnemex_id` column links to canonical MNEMEX record. `source_type`
  records which sync path produced the row (`'legacy'`,
  `'mnemex_sync'`, `'prediction_residual_write'`).
- **`sync_log`** — append-only audit trail of sync runs.
  `sync_path` records which trigger fired.
- **`wood_species`** — reference data (slow-changing, treated as
  read-only).

Writes to these tables originate ONLY in the sync function. Direct
writes from application code are prevented by RLS policies (see
controlled-write enforcement section below).

### ML state (STRATHMARK-internal carve-out)

- **`model_versions`** — every trained model with full provenance.
- **`calibration_tables`** — calibration artifacts per model version.
- **`feature_store`** — per-prediction feature vectors.
- **`predictions`** — every prediction made.
- **`prediction_residuals`** — settled residuals (actual vs predicted).

Writes to these tables originate in STRATHMARK code itself. They are
explicitly carved out from the controlled-write rule because they are
internal to STRATHMARK's ML lifecycle. RLS policies allow writes from
the STRATHMARK service-role key on these tables.

See [`docs/ml-persistence-policy.md`](../ml-persistence-policy.md) for
retraining cadence, model versioning, calibration drift, and the
non-blocking guarantee.

## Local SQLite store (`strathmark/store.py`)

### Default path

```
~/.strathmark/results.db
```

Override with `STRATHMARK_DB_PATH=/some/other/path.db`. Parent
directories are created on first write.

### Role under controlled-write

The local SQLite store is unchanged by the controlled-write reframe.
It remains the offline-first cache the prediction cascade reads from
when STRATHMARK Supabase is unreachable. It is NOT in the sync chain
— it's a consumer of the same hydrated data.

Pre-event seeding: run `pull_results()` from the STRATHMARK Supabase
cache and feed the result into the local store. The store is then
fully operational on event day even if every cloud tier goes dark.

### Schema

Two tables:

- `results` — one row per historical chop. Columns: `competitor_name`,
  `event_code`, `time_seconds`, `species`, `diameter_mm`, `quality`,
  `competition_id`, `result_date`, `heat_id`, and `recorded_at`.
- `competitors` — lightweight roster.

The local store validates event, time, diameter, quality, and required text
before a row can influence future predictions. `competition_id` separates the
same heat label at different shows; new callers should always provide it.

### Typical usage

```python
from strathmark import ResultStore
from datetime import date

store = ResultStore()  # opens ~/.strathmark/results.db
history = store.get_competitor_history("Alice Smith", "SB")

store.record_result(
    competitor_name="Alice Smith",
    event_code="SB",
    time_seconds=28.4,
    species="Pine",
    diameter_mm=300,
    quality=5,
    competition_id="missoula-pro-am-2026",
    result_date=date(2026, 4, 25),
)
```

## STRATHMARK Supabase access (`strathmark/db.py`)

### Env vars

| Variable | Purpose |
|----------|---------|
| `STRATHMARK_SUPABASE_URL` | STRATHMARK project URL |
| `STRATHMARK_SUPABASE_KEY` | Service-role key (sync function + ML writes) or anon key (reads) |
| `MNEMEX_SUPABASE_URL` | MNEMEX project URL (set when MNEMEX is online) |
| `MNEMEX_SUPABASE_KEY` | MNEMEX service-role key for sync reads |

If `MNEMEX_SUPABASE_URL` is unset, the sync function operates in
no-op / dry-run mode. It logs what it WOULD do but never writes. This
keeps the rest of the system functional during the period before
MNEMEX is stood up.

### Reading

```python
from strathmark import pull_results

df = pull_results()  # everything
df = pull_results(competitor_ids=["C0042"])  # filter
```

Returns a Pandas DataFrame with the standardised column names
STRATHMARK's predictor expects.

### Recording predictions and residuals (ML state)

```python
from strathmark import record_prediction, settle_prediction

# At prediction time (best-effort; never blocks the prediction return):
pred_id = record_prediction(
    model_version_id=active_model_id,
    competitor_id="C0042",
    event_code="SB",
    show_name="Missoula Pro-Am 2026",
    predicted_time=24.7,
    predicted_variance=4.0,
    cascade_level_used="ml",
)

# When the actual result lands (best-effort):
residual = settle_prediction(
    prediction_id=pred_id,
    result_id=actual_result_id,
    actual_time=24.9,
)
```

### Bias correction read

```python
from strathmark import get_competitor_bias

bias = get_competitor_bias("C0042")
# Returns median residual in seconds, or None if <3 samples.
```

This is the only Supabase read on the prediction hot path. It is
wrapped in a circuit-breaker that allows transient failures without
permanently disabling bias correction. See
[`strathmark/predictor.py`](../../strathmark/predictor.py) for the
implementation.

## Controlled-write enforcement

Effective from migration `20260504_003_rls_reframe.sql`, the cache
tables (`results`, `competitors`) deny INSERT/UPDATE/DELETE from any
role except the dedicated `mnemex_sync` Postgres role. The ML state
tables (`model_versions`, `calibration_tables`, `feature_store`,
`predictions`, `prediction_residuals`) allow writes from the
STRATHMARK service-role key.

The `register_competitor()` function now writes to MNEMEX (not
STRATHMARK) and waits for sync propagation. See
[`strathmark/db.py`](../../strathmark/db.py) and
[`strathmark/mnemex.py`](../../strathmark/mnemex.py).

`scripts/validate_deployment.py --write` is deprecated. The flag
still parses but logs a deprecation warning and refuses to write to
the controlled-write cache tables. Use the sync function for writes.

## MNEMEX access (`strathmark/mnemex.py`)

STRATHMARK reads MNEMEX directly via the MNEMEX service-role key,
ONLY from the sync function and the rewritten `register_competitor()`.
The prediction hot path NEVER reaches MNEMEX.

```python
from strathmark.mnemex import (
    is_mnemex_configured,
    pull_canonical_results,
    register_competitor_in_mnemex,
)

if is_mnemex_configured():
    df = pull_canonical_results(since="2026-04-01")
```

When MNEMEX env vars are unset, all functions in this module return
empty/no-op results without raising. This is intentional: STRATHMARK
must remain functional during the pre-MNEMEX transition.

## Related CLI scripts

- `scripts/validate_deployment.py` — pre-event read-only check.
  `--write` is deprecated.
- `scripts/ingest_proam_results.py` — DEPRECATED. Operators now
  ingest into MNEMEX directly; STRATHMARK pulls via the sync
  function. The script remains for legacy reference but its
  `--commit` mode logs a deprecation warning.
- `scripts/sync_from_mnemex.py` — manual one-shot sync trigger.
  Operator-facing wrapper around `strathmark.sync.manual_force_sync()`.
- `scripts/rekey_against_mnemex.py` — one-shot script that re-keys
  the existing 1311 results against canonical MNEMEX IDs. Run once
  after MNEMEX comes online.

## Testing

- `tests/test_store.py`, `tests/test_store_extended.py` — SQLite CRUD.
- `tests/test_db.py`, `tests/test_db_extended.py` — STRATHMARK Supabase
  schema, validation rules, ML state imports.
- `tests/test_ml_state.py` — ML state lifecycle (live tests gated by
  `STRATHMARK_TEST_DB=1` against an isolated test project).
- `tests/test_mnemex.py` — MNEMEX client (no-op when env unset; live
  tests gated similarly).
- `tests/test_sync.py` — sync function dry-run and live behavior.
- `tests/test_drift.py` — calibration drift detection.

Live tests refuse to run against the production project ref
`iordtvxryrdhqvdkfgzf` even with `STRATHMARK_TEST_DB=1` set.
