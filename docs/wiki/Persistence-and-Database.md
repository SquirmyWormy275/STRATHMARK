# Persistence and Database

STRATHMARK has two storage tiers — a local SQLite store that works
out of the box and a Supabase/PostgreSQL backend for multi-device
tournament operation. The cascade reads from whichever tier is
available and falls through gracefully if neither is populated.

## Local SQLite store (`strathmark/store.py`)

### Default path

```
~/.strathmark/results.db
```

Override with `STRATHMARK_DB_PATH=/some/other/path.db`. Parent
directories are created on first write.

### Schema

Two tables:

- `results` — one row per historical chop. Columns: `competitor_name`,
  `event_code`, `raw_time`, `species`, `size_mm`, `quality`,
  `result_date`, `heat_id`, `field_strength`.
- `competitors` — lightweight roster. Columns: `name`, `country`,
  `state`, `gender`, `division`, `panel_mark`.

Both tables use simple TEXT/REAL types; no foreign keys, no
constraints beyond `NOT NULL` on identity columns. The store is
intentionally permissive — it accepts whatever the cascade emits.

### Typical usage

```python
from strathmark import ResultStore
from datetime import date

store = ResultStore()  # opens ~/.strathmark/results.db
history = store.get_competitor_history("Alice Smith", "SB")
# -> list of HistoricalResult

store.record_result(
    competitor_name="Alice Smith",
    event_code="SB",
    raw_time=28.4,
    species="Pine",
    size_mm=300,
    quality=5,
    result_date=date(2026, 4, 25),
)
```

Duplicate detection: the store silently ignores an insert whose
`(competitor_name, event_code, raw_time, size_mm, result_date)` tuple
matches an existing row.

## Supabase backend (`strathmark/db.py`)

### Env vars

| Variable | Purpose |
|----------|---------|
| `STRATHMARK_SUPABASE_URL` | Supabase project URL |
| `STRATHMARK_SUPABASE_KEY` | Service role or anon key |

Both must be set for any `push_*` or `pull_*` call. If either is
missing, `db.py` raises `RuntimeError` on first use and the cascade
falls through to the SQLite store (if populated) then to panel marks.

### Schema

- `competitors` — roster shared across events. Primary key
  `competitor_id`.
- `results` — historical times. Foreign key to `competitors`.
- `sync_log` — append-only audit trail of ingestion batches.

Full schema in STRATHEX `docs/PROJECT_STRUCTURE.md`.

### Ingestion

```python
from strathmark import push_results_dicts

result = push_results_dicts(
    [
        {
            "competitor_id": "C0042",
            "event_code": "SB",
            "time_seconds": 24.7,
            "size_mm": 275,
            "species_code": "S05",
            "date": "2026-04-25",
        },
    ],
    source="pro-am-manager",
    show_name="Missoula Pro-Am 2026",
)
print(result)
# {'inserted': 1, 'skipped': 0, 'errors': 0, 'dry_run': False}
```

Validation rules:

- Required fields: `competitor_id`, `event_code`, `time_seconds`,
  `size_mm`, `species_code`, `date`.
- `event_code` must be `SB` or `UH`.
- `time_seconds` must be in `[3.0, 180.0]` (the system mark window).
- `competitor_id` must already exist in `competitors`.
- Duplicates (same `(competitor_id, event, time, size, date)`) are
  silently skipped.

No row is ever silently dropped without a corresponding counter.

### Reading

```python
from strathmark import pull_results

df = pull_results()                                  # everything
df = pull_results(competitor_ids=["C0042"])          # filter
df = pull_results(show_name="Missoula Pro-Am 2026")  # by show
```

Returns a Pandas DataFrame with the standardised column names
STRATHMARK's predictor expects. `utils.standardize_results_columns`
handles column aliases when the remote schema drifts.

### Registering new competitors

```python
from strathmark import register_competitor

register_competitor(
    name="New Competitor",
    country="USA",
    state="MT",
    gender="M",
)
```

Generates a new `competitor_id`, inserts into `competitors`, and
returns the new ID. Idempotent — calling twice for the same name
returns the existing ID without duplicating the row.

### Bias tracking

```python
from strathmark import record_prediction_residuals, get_competitor_bias

# After the event is scored, record (predicted, actual) per competitor.
record_prediction_residuals(
    [
        {"competitor_id": "C0042",
         "predicted": 24.5, "actual": 24.7,
         "event_code": "SB", "date": "2026-04-25"},
    ],
    show_name="Missoula Pro-Am 2026",
)

print(get_competitor_bias("C0042"))
# {'mean_residual': 0.12, 'mae': 0.54, 'samples': 27}
```

Bias is `actual − predicted` by convention. Positive means the
competitor systematically cuts slower than the model predicts.

## Data flow across a tournament day

```
  Pro-Am Manager (UI)
         |
         v  writes results via push_results_dicts()
  +----------+       +-----------+       +-----------+
  | Supabase |<----->| STRATHMARK|<----->| Local     |
  | (authority)      |  cascade  |       | SQLite    |
  +----------+       +-----------+       +-----------+
         |                ^                    ^
         +---pull_results-+                    |
                                               |
         offline fallback ----------------------
```

Supabase is the authority. STRATHMARK always tries there first. If
Supabase is unreachable (flaky venue wifi), the cascade reads the
local SQLite store — which should have been seeded by a pre-event
`pull_results()` call and will keep race day running.

## Related CLI scripts

- `scripts/validate_deployment.py` — pre-event read-only check of
  Supabase, prediction, simulation, result ingestion.
- `scripts/ingest_proam_results.py` — post-event bulk ingestion from
  a CSV exported by the Pro-Am Manager.

Both are documented in [Deployment](Deployment).

## Testing

- `tests/test_store.py`, `tests/test_store_extended.py` — SQLite CRUD,
  duplicate detection, column aliases, date handling.
- `tests/test_db.py`, `tests/test_db_extended.py` — Supabase
  push/pull (with mocked HTTP), validation rules, bias tracking.

The Supabase tests use `pytest.importorskip("supabase")` so CI
without the `db` extra still passes.
