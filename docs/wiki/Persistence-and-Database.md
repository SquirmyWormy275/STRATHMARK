# Storage and recovery

The files you need depend on the selected runtime. A public calculation response
and a stored official competition receipt are different records.

## V2

`ResultStore` holds historical results. `PredictionLedger` and the authenticated
shadow contract hold their own trusted receipts. Public `/calculate` reads its
request and does not automatically write either ledger.

An optional Supabase mirror is a secondary copy. Local outbox records allow later
replay after a failed mirror write; the mirror is not race-day calculation authority.

## Linux V3

The local runtime stores signed command history, receipts and settlements in SQLite,
with a separate retained head and persistent signing identity. STRATHEX also keeps
its saved workflow and command acknowledgments.

An exact retry returns the original committed result. Reusing an ID with changed
inputs is rejected. Approval does not issue a sheet. Settlement binds the complete
issued field; official corrections append revisions rather than overwrite history.

Preserve these files as a set. Do not edit the database, replace the signing key or
upgrade a saved competition's runtime files in place. See [recovery](Deployment).

## V7 service

The wider service uses an append-only event log, rebuildable projections and
content-addressed evidence. Version checks and idempotent commands reject stale or
changed requests. Settlement reactions must complete before later derivations proceed.
The [architecture specification](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/ARCHITECTURE.md)
describes that storage contract in detail.

Tests use disposable databases and synthetic inputs. Keep operator paths out of
[test and rehearsal environments](Testing).
