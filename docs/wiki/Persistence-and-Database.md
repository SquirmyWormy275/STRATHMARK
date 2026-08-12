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

The ledger retains canonical request hash, stable IDs, cutoff, prediction and artifact
versions, median, interval, source, mark, ignored factors, warnings, numeric allowlisted
features, and optimizer metadata. It does not retain display names, narrative notes, or
the raw request body. Manual predictions are not training eligible.

An identical retry returns original prediction IDs. A changed payload under the same
key conflicts. Settlements verify prediction/competitor/event, deduplicate exact
retries, and append attributed correction revisions rather than updating rows.

## Cloud mirror

Migration `20260811_005_prediction_v2.sql` defines an optional Supabase mirror. It
forces RLS, revokes browser roles, rejects UPDATE/DELETE, and grants the append RPC only
to `service_role`. The migration is operator-applied; its presence in the repository is
not proof that a live project has it.

SQLite is authoritative. Cloud mirroring is best-effort and cannot block marks. Keep
the service key server-side. Public `/predict` and `/calculate` remain stateless.

MNEMEX and older Supabase ML-state tables are separate integration/history concerns;
they are not the V2 race-day ledger authority.
