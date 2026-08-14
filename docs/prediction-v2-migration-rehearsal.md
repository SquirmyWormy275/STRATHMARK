# Prediction V2 Migration Rehearsal

This is a development and CI proof for migrations 005, 006, and 007. It never applies a
hosted migration and does not authorize production changes.

## Safety boundary

- Use only a PostgreSQL server bound to `127.0.0.1`, `localhost`, or `::1`.
- Use a controller database named `strathmark_rehearsal_*`.
- The controller account must be able to create/drop a database and temporary roles.
- Do not export Supabase or Railway credentials. The runner removes ambient cloud and
  libpq connection variables before launching PostgreSQL clients.
- The runner refuses the known STRATHMARK production project reference and common
  production database names before connecting.

## Run locally

Create an empty controller database and a local-only controller account, then run:

```powershell
$env:STRATHMARK_REHEARSAL_DSN = 'postgresql://rehearsal:LOCAL_ONLY_PASSWORD@127.0.0.1:5432/strathmark_rehearsal_controller'
python -m pytest tests/test_postgres_rehearsal.py -v
Remove-Item Env:STRATHMARK_REHEARSAL_DSN
```

The runner creates a unique child database, bootstraps `anon`, `authenticated`, and
`service_role`, then executes the checked-in production prerequisite
`strathmark/migrations/prerequisites/prediction_rpc_owner.sql`. That prerequisite must
be run before migration 005 by a superuser or a member of PostgreSQL's
`pg_create_role` predefined role; it provisions or validates the dedicated
`strathmark_prediction_rpc_owner NOLOGIN NOBYPASSRLS` role. The runner then creates the
minimal `competitors` table. It always drops the child database and only drops roles it
created itself. The prerequisite is checked in for review; this rehearsal does not
apply it to any hosted database.

## What passes

The matrix executes the real SQL and verifies:

- migration 005 accepts legacy `raw-v1` and rejects `active-v2` atomically;
- malformed, ambiguous, unlinked, missing, and wrong-type RPC payloads are rejected
  before any request, prediction, feature, or settlement mutation;
- forced RLS and grants deny browser roles and service-role table mutation;
- the RPC has a non-login, non-bypass owner, empty search path, qualified objects, and
  service-only execution;
- update/delete triggers keep mirror rows append-only;
- both required query indexes exist in the PostgreSQL catalog;
- migration 006 replays legacy rows, accepts `active-v2`, supports exact retry, and
  rejects changed hashes;
- failed field or settlement/correction foreign keys roll back atomically;
- ordered reapplication is idempotent;
- rollback succeeds before active evidence and refuses after active evidence;
- a temporary object with the same unqualified table name cannot shadow RPC objects.
- migration 007 preserves all 005/006 rows and adds only the versioned receipt,
  numeric settle/void, and delivery evidence tables;
- a complete receipt envelope, exact retry, changed-payload conflict, settle-to-void
  sequence, and migration rerun retain exactly one immutable copy;
- receipt and numeric foreign-key failures roll back the entire envelope transaction;
- browser roles and direct service-role mutation remain denied across all four 007
  tables, which have forced RLS and immutable-row triggers;
- the 007 down file works before activation and refuses after any shadow evidence is
  recorded; active recovery is forward repair or local-ledger restore.

Passing this gate proves PostgreSQL semantics only. A separately authorized, isolated
hosted smoke test is still required before any production migration window.
