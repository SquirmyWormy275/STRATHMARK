---
type: knowledge
problem_type: architecture_decision
severity: high
tags:
  - "persistence"
  - "sqlite"
  - "supabase"
  - "mnemex"
  - "offline-first"
  - "controlled-write"
  - "event-deployment"
confidence: high
created: 2026-04-21
last_updated: 2026-08-13
source: "v0.2.0 Supabase integration + Apr 7 deployment-readiness session + 2026-05-04 controlled-write reframe"
---

# Triple Store — SQLite Local + STRATHMARK Supabase Cache + MNEMEX Authority

> **Historical architecture with a V2 addendum.** References below to a prediction
> “cascade” describe the pre-2.0 engine. Prediction Engine V2 snapshots one causal
> bundle and does not query these stores per competitor. Its separate append-only
> ledger and optional mirror are documented in
> [`docs/PREDICTION_ENGINE_V2.md`](../../PREDICTION_ENGINE_V2.md).

## Context (post-2026-05-04)

STRATHMARK persists data across three tiers, each with a distinct lifecycle
role and reliability assumption:

- `store.py` — SQLite at `~/.strathmark/results.db` (local, always available)
- `db.py` — STRATHMARK Supabase at `STRATHMARK_SUPABASE_URL` (hydrated cache + ML state)
- `mnemex.py` — MNEMEX Supabase at `MNEMEX_SUPABASE_URL` (canonical archive)

This is intentionally three layers, not duplication. Each backend serves a
different lifecycle stage. The 2026-05-04 reframe moved STRATHMARK Supabase
from "system of record" to "hydrated cache plus ML state" — MNEMEX took over
the system-of-record role for canonical results across all timbersports
disciplines.

## Pattern

| Backend | Module | Role | Online required at prediction time? |
|---|---|---|---|
| SQLite | `store.py` (`ResultStore`) | Event-day cache + per-competitor history. The cascade reads here when computing marks. | No |
| STRATHMARK Supabase | `db.py` (`pull_results`, `get_competitor_bias`, ML state writers) | Hydrated chopping cache populated by the sync function from MNEMEX. Plus internal ML state (model_versions, calibration_tables, feature_store, predictions, prediction_residuals). | No (cascade falls back to SQLite) |
| MNEMEX Supabase | `mnemex.py` | Canonical archive of every timbersports result across every discipline. STRATHMARK reads it via the sync function and the rewritten `register_competitor()` ONLY. Never on the prediction hot path. | Never on hot path |

### Sync paths (MNEMEX -> STRATHMARK Supabase)

Three paths, all through `strathmark.sync`:

1. `nightly_batch()` — cron at 3am UTC. Pulls scraper output and federation
   backfill from MNEMEX, upserts into the STRATHMARK cache. Filters to
   chopping disciplines and `provisional = false`.
2. `strathex_finalization(event_id)` — STRATHEX finalization webhook fires
   this for sub-minute propagation of newly finalized events.
3. `manual_force_sync(show_name=None)` — admin-driven; used when an operator
   has imported a scorebook batch into MNEMEX and tomorrow's event needs
   the data now.

All three paths log to `sync_log` with the appropriate `sync_path` value.

## Rationale

- **MNEMEX as authority** decouples canonical history from any single
  product. STRATHMARK is one consumer; future timbersports analytics
  products are others. The 1311 chopping rows in STRATHMARK Supabase are
  re-keyed against MNEMEX IDs by `scripts/rekey_against_mnemex.py` (one-shot,
  run after MNEMEX is stood up).
- **STRATHMARK Supabase as hydrated cache** keeps the prediction hot path
  fast. STRATHMARK never queries MNEMEX directly during a prediction; the
  cache is always local enough.
- **SQLite as offline-first edge** keeps event-day operations functional
  when both cloud tiers are unreachable. Documented race-day failure modes
  (DNS lag, paused projects, venue Wi-Fi dropping) all continue to work
  through the local cache.
- **ML state stays in STRATHMARK** because it is internal to STRATHMARK's
  modeling pipeline. Model versions, calibration tables, feature vectors,
  predictions, and residuals are not canonical timbersports data — they
  are STRATHMARK's record of what its model did and when. Carving them
  out from the controlled-write rule keeps the ML lifecycle non-blocking
  on MNEMEX availability.
- **Schemas differ intentionally**: SQLite `results` table has
  `competitor_name` (human-readable), no `competitor_id` FK. STRATHMARK
  Supabase `results` has `competitor_id` FK + `mnemex_id` cross-ref.
  MNEMEX has its own canonical schema. Conversion happens at the sync
  boundary and at the `pull_results` boundary.

## Controlled-write enforcement

From migration `20260504_003_rls_reframe.sql`:

- `competitors`, `results`: writes denied for any role except a dedicated
  `mnemex_sync` Postgres role. The sync function uses this role; nothing
  else does.
- `model_versions`, `calibration_tables`, `feature_store`, `predictions`,
  `prediction_residuals`: writes allowed from the STRATHMARK service-role
  key. This is the explicit ML-state carve-out.
- `wood_species`, `sync_log`: writes from service-role key (operator
  maintenance for the former, sync function for the latter).

Reads remain unrestricted (anon key sufficient) for all tables.

## When to apply

- Computing marks during an event -> `ResultStore` / SQLite, always
- Recording a new result locally -> `ResultStore.record_result()`
- Recording a prediction for later settlement -> `record_prediction()`
- Settling a prediction with the actual result -> `settle_prediction()`
- Registering a new competitor -> `register_competitor()` (writes to MNEMEX,
  waits for sync propagation)
- Running backtests across multiple events -> `pull_results()` from the
  STRATHMARK cache
- Triggering an out-of-cycle sync -> `manual_force_sync()` or
  `scripts/sync_from_mnemex.py`

## Failure-mode playbook

| Symptom | Diagnosis | Fallback |
|---|---|---|
| `RuntimeError: STRATHMARK_SUPABASE_URL is not set` | Env vars missing | Cascade still works on SQLite. ML state writes degrade to no-op. |
| STRATHMARK Supabase DNS lookup fails | Stale URL or paused project | Run cascade in local mode; resume project in dashboard |
| MNEMEX env vars unset | MNEMEX not yet online (transition state) | Sync function dry-runs; `register_competitor()` rejects with clear message; rest of system unaffected |
| `is_mnemex_configured() == False` after MNEMEX should be live | Env var typo or project paused | Check `MNEMEX_SUPABASE_URL` resolves; check Cloudflare 521 status if origin is paused |
| Sync function reports zero rows pulled | MNEMEX has no new chopping rows since last cursor | Normal; not an error |
| Hot-path bias correction failures | Transient Supabase blip | Circuit breaker absorbs up to 3 failures per 60s window without permanent disable; auto-resets |

## Examples

Canonical call shapes live in code:
- `ResultStore.record_result()` — see [`strathmark/store.py`](../../../strathmark/store.py)
- `record_prediction()` and `settle_prediction()` — see
  [`strathmark/db.py`](../../../strathmark/db.py)
- Sync function paths — see [`strathmark/sync.py`](../../../strathmark/sync.py)
- MNEMEX client — see [`strathmark/mnemex.py`](../../../strathmark/mnemex.py)

## Related

- [`docs/ml-persistence-policy.md`](../../ml-persistence-policy.md) — ML state policy
- [`docs/wiki/Persistence-and-Database.md`](../../wiki/Persistence-and-Database.md) — operator-facing reference
- [`docs/schema-reality-2026-05-04.md`](../../schema-reality-2026-05-04.md) — verified live schema
- [`strathmark/migrations/`](../../../strathmark/migrations/) — migration files
- [`../configuration-issues/env-vars-resolved-at-import-time.md`](../configuration-issues/env-vars-resolved-at-import-time.md) — why env vars must be set before first import

## Historical note

Prior to 2026-05-04, this document described STRATHMARK Supabase as the
"system of record" and Pro-Am Manager wrote results directly via
`push_results_dicts()`. That arrangement is deprecated. All canonical
writes now route through MNEMEX. The 1311 rows present at the time of
the reframe were re-keyed against MNEMEX IDs by `rekey_against_mnemex.py`
once MNEMEX came online.
