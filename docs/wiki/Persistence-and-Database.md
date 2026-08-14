# Persistence and Database

STRATHMARK separates result history from trusted prediction evidence.

## Local result store

`ResultStore` keeps application history in SQLite. It supports offline prediction input
and the protected `/results` routes. It is not automatically trusted model-evaluation
evidence.

## Trusted V2 ledger

`PredictionLedger` adds append-only tables to `STRATHMARK_DB_PATH` (default
`~/.strathmark/results.db`). Trusted `/ledger/calculate` requires a stable competitor ID
for every row plus a caller/request idempotency key. A complete field is committed in
one transaction after marks are final.

The ledger retains a canonical calculation hash, stable IDs, cutoff, prediction and artifact
versions, median, interval, source, mark, ignored factors, warnings, numeric allowlisted
features, and optimizer metadata. It does not retain display names, narrative notes, or
the raw request body. Manual, broad-prior, legacy-rollback, and degraded predictions are
not training eligible.

An identical retry returns original prediction IDs. Changed inputs or deterministic
prediction outputs under the same key conflict. Settlements verify prediction/competitor/event, deduplicate exact
retries, and append attributed correction revisions rather than updating rows.

## Cloud mirror

Migration `20260811_005_prediction_v2.sql` defines the optional V2 mirror,
`20260813_006_prediction_hash_algorithm.sql` adds explicit `raw-v1`/`active-v2` request
hash compatibility, and `20260813_007_shadow_mirror_contract.sql` adds the closed
immutable shadow receipt and numeric-revision mirror. Apply 005, then 006, then 007 only
after the exact sequence and guarded rollback/refusal behavior pass against disposable
PostgreSQL and a production change is separately authorized. They force RLS, revoke
browser roles, reject UPDATE/DELETE, and grant the append RPC only to `service_role`.
Their presence in the repository is not proof that a live project has them.

SQLite is authoritative. Cloud mirroring is best-effort and cannot block marks. Keep
the service key server-side. A sanitized local outbox is committed with each trusted
field or settlement and an identical retry replays failed delivery. Public `/predict`
and `/calculate` remain stateless.

The durable outbox is append-only and intentionally uncapped: deleting undelivered
evidence to enforce a hard row limit would violate local authority and nonblocking
mirror behavior. Work per request is still bounded through input cardinality, keyset
scan pages, replay batch limits, concurrency slots, and deadlines. Until a reviewed
archive/compaction and finite-capacity policy exists, monitor pending count and oldest
age, provision disk or disable mirroring, and treat unavailable delivery as
`retryable-failed`; `permanent-failed` is not implemented.

MNEMEX and older Supabase ML-state tables are separate integration/history concerns;
they are not the V2 race-day ledger authority.
