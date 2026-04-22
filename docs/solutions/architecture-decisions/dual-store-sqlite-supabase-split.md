---
type: knowledge
problem_type: architecture_decision
severity: high
tags:
  - "persistence"
  - "sqlite"
  - "supabase"
  - "offline-first"
  - "event-deployment"
confidence: high
created: 2026-04-21
source: "v0.2.0 Supabase integration + Apr 7 deployment-readiness session"
---

# Dual Store — SQLite Local + Supabase Cloud

## Context
STRATHMARK persists results in two backends:
- `store.py` — SQLite at `~/.strathmark/results.db` (local, always available)
- `db.py` — Supabase/PostgreSQL at `STRATHMARK_SUPABASE_URL` (cloud, cross-project)

This is not duplication. Each backend serves a different lifecycle stage and a different reliability assumption.

## Pattern

| Backend | Module | Role | Online required? |
|---|---|---|---|
| SQLite | `store.py` (`ResultStore`) | Event-day cache + per-competitor history. The cascade reads from here when computing marks. | No |
| Supabase | `db.py` (`push_results_dicts`, `pull_results`, `register_competitor`) | Cross-event historical data shared across projects (STRATHMARK, STRATHEX, Missoula-Pro-Am-Manager). Ingestion + long-range backtests. | Yes |

Integration points:
- **Pre-event**: optionally `pull_results()` from Supabase into a local DataFrame, feed into `HandicapCalculator` / `ResultStore`.
- **During event**: `ResultStore` only. No cloud calls in the mark-computation hot path.
- **Post-event**: `push_results_dicts()` pushes event results to Supabase so other projects pick them up for future predictions.

## Rationale
- **Event laptops often lack reliable internet** — remote venues, poor WiFi, contested cellular. Offline-first via SQLite means marks are computed regardless.
- **Supabase DNS failure is a plausible race-day failure mode** — documented in `validate_deployment.py`. If `STRATHMARK_SUPABASE_URL` resolves but the Supabase anon key is stale, or the project is paused, the DNS/TLS handshake can fail without a clean error. The cascade must keep working through local SQLite.
- **Missing `STRATHMARK_SUPABASE_*` is not a failure** — the cascade falls through to the local store, baseline prediction, or panel-mark fallback. Only ingestion (`push_results_dicts`, `register_competitor`) strictly requires Supabase credentials.
- **Multiple projects share the same Supabase** — Missoula-Pro-Am-Manager writes results to the same tables STRATHMARK reads. Supabase is the system-of-record for historical data; SQLite is the edge cache.
- **Schemas differ intentionally**: SQLite `results` table has `competitor_name` (human-readable), no `competitor_id` FK. Supabase `results` table has `competitor_id` FK into `competitors`. The SQLite schema is optimized for quick reads from a single competitor; the Supabase schema is normalized for multi-project writes. Conversion happens at the `push`/`pull` boundary.

## When to Apply
- Computing marks during an event → `ResultStore` / SQLite, always
- Recording a new result locally → `ResultStore.record_result()` (idempotent on the unique key)
- Ingesting Pro-Am Manager export after a show → `push_results_dicts()` → Supabase
- Running backtests across multiple events → `pull_results()` from Supabase first, then feed into `ResultStore` or the calculator
- Registering a new competitor → `register_competitor()` → Supabase only (SQLite uses name as key, not a separate ID table)

## Failure-mode playbook
| Symptom | Diagnosis | Fallback |
|---|---|---|
| `RuntimeError: STRATHMARK_SUPABASE_URL is not set` | Env vars missing in current shell | Cascade still works on SQLite. Only ingestion is blocked. |
| Supabase DNS lookup fails | Stale project URL or paused project | Run the cascade in local mode; re-sync after the event |
| `push_results_dicts` reports `"competitor_id not found"` | Pro-Am Manager exported a name with no Supabase match | Re-run with `--interactive` and `register_competitor` the new name, or pre-load via the programmatic API |

## Examples
Canonical call shapes live in the code rather than this doc:
- `ResultStore.record_result()` signature and usage — see [`strathmark/store.py`](../../../strathmark/store.py) and the wired-up call in [`strathmark/api.py`](../../../strathmark/api.py) (the `/results` endpoint).
- `push_results_dicts()` signature, validation rules, and `{inserted, skipped, errors, dry_run}` return — see [`docs/DEPLOYMENT.md`](../../DEPLOYMENT.md) "Result ingestion programmatic API".

## Related
- [`docs/DEPLOYMENT.md`](../../DEPLOYMENT.md) — env var reference, pre-event checklist, troubleshooting
- [`docs/wiki/Persistence-and-Database.md`](../../wiki/Persistence-and-Database.md) — schema reference
- [`../configuration-issues/env-vars-resolved-at-import-time.md`](../configuration-issues/env-vars-resolved-at-import-time.md) — why `STRATHMARK_SUPABASE_*` must be set before first import
