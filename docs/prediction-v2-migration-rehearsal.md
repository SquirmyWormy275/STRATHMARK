# Prediction V2 Migration Rehearsal

> **V2-only proof.** V3 uses separate local SQLite migrations and a separate cutover
> boundary. This PostgreSQL rehearsal neither initializes nor authorizes V3.

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
`strathmark_prediction_rpc_owner` role with no login, bypass, inheritance, cluster
administration, replication, or membership capability. The runner deliberately mutates
one role attribute and one membership in turn to prove the prerequisite rejects both,
then restores the isolated role. It then creates the minimal `competitors` table. It
always drops the child database and only drops roles it
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
- migration 006 replays legacy rows, accepts `active-v2`, supports exact typed retry,
  and rejects changed hashes;
- the 005, 006, and 006-down RPCs compare the complete request projection plus exact
  prediction and feature sets/counts; changed, missing, extra, or cross-request child
  IDs conflict without leaving partial request or prediction rows;
- before their first JSON-to-record conversion, those RPCs reject unknown or missing
  request/prediction/feature/settlement keys and values whose JSON types do not match
  the frozen typed projection; `hash_algorithm` remains the only optional request key
  so legacy `raw-v1` outbox rows can replay;
- all three legacy RPC versions compare every typed settlement field on retry and
  reject altered same-hash evidence, skipped/stale revisions, non-latest supersession,
  missing correction reasons, and residuals outside the `1e-9` float-noise allowance;
- failed field or settlement/correction foreign keys roll back atomically;
- ordered reapplication is idempotent;
- rollback succeeds before active evidence and refuses after active evidence;
- the 006 rollback takes an exclusive relation lock, waits for a concurrent append,
  then refuses after the append commits; a NOBYPASS executor cannot silently inspect a
  filtered subset when `row_security=off`;
- a temporary object with the same unqualified table name cannot shadow RPC objects.
- migration 007 preserves all 005/006 rows and adds only the versioned receipt,
  numeric settle/void, and delivery evidence tables;
- a complete receipt envelope, exact retry, changed-payload conflict, settle-to-void
  sequence, and migration rerun retain exactly one immutable copy;
- bounded nonempty evidence diagnostic maps are accepted, while snapshot/per-competitor
  reason maps with more than 128 entries, non-machine-code keys, negative or fractional
  counts, Booleans, numeric strings, arrays, or nested objects are rejected;
- receipt and numeric foreign-key failures roll back the entire envelope transaction;
- browser roles and direct service-role mutation remain denied across all four 007
  tables, which have forced RLS and immutable-row triggers;
- the 007 down file works before activation and refuses after any shadow evidence is
  recorded; active recovery is forward repair or local-ledger restore.
- the 007 down file locks all four mirror relations before its RLS-fail-closed guard, so
  a concurrent receipt cannot cross the guard;
- sorted per-prediction advisory locks serialize legacy and numeric settlement writers:
  a legacy-first race becomes the exact prior numeric revision, while a numeric-first
  race is rejected by the database authority trigger before a second legacy row lands.

Passing this gate proves PostgreSQL semantics only. A separately authorized, isolated
hosted smoke test is still required before any production migration window.
