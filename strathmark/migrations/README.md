# STRATHMARK Migrations

## V2/V3 boundary

V3 is an implemented release candidate whose older rehearsal is stale after current
source changes, but V2 remains the trusted production authority until an explicit
cutover. No production authority has changed.

This directory contains the historical V1/V2 PostgreSQL/Supabase mirror migrations. V3
does not extend these files and does not use PostgreSQL as its race-day authority. Its
dedicated local SQLite event-store migrations are ordered, checksummed, and applied by
`strathmark/v3/migrations/`. V3 imports V2 through a repeatable read-only snapshot and
never makes the two schemas concurrent trusted writers.

Applying a file in this directory cannot activate V3. Applying a V3 local migration
cannot switch a consumer. Existing production PostgreSQL work remains a separately
authorized operation.

This directory holds checked-in SQL migration files for the STRATHMARK Supabase
schema. From 2026-05-04 forward, every schema change goes through a file here.
No more ad-hoc DDL via the dashboard SQL editor without leaving a migration
artifact in the repo.

## Naming convention

```
YYYYMMDD_NNN_short_description.sql
```

- `YYYYMMDD` is the date the migration was authored (UTC).
- `NNN` is a zero-padded 3-digit sequence number for migrations authored on
  the same day (`001`, `002`, ...).
- `short_description` is `lowercase_with_underscores`, less than 50 chars.

Example: `20260504_001_add_source_tracking.sql`

Migrations are ordered lexicographically. The naming convention guarantees
that ordering matches authoring order.

## Content structure

Every migration file MUST have:

1. A header comment block stating the date, author/intent, and whether the
   migration is reversible.
2. The forward DDL statements wrapped in a transaction (`BEGIN; ... COMMIT;`).
3. A "Rollback" section as a comment block with the inverse DDL the operator
   would run to undo the change. Comment-only — do NOT have a separate
   rollback file. If a migration is genuinely irreversible (data loss),
   say so explicitly and document the recovery path.

Each migration must be idempotent where Postgres allows it (`CREATE TABLE IF
NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`). When idempotency is impossible,
the file must say so in the header.

## Application

Production migrations are NOT applied automatically. The accepted production process
today is:

1. The author writes the file and gets it merged.
2. An operator with separately authorized Supabase access applies the forward block to
   the explicitly verified target project. Production identifiers do not belong in this
   repository runbook.
3. The operator records the application timestamp and any notes in
   `docs/migration-log.md` (created on first use; not present yet).

Once the controlled-write reframe lands and the MNEMEX-side sync function
exists, this process will be tightened (likely Supabase CLI based, or a
dedicated migration runner). Until then, the dashboard editor is the
mechanism.

Prediction Engine V2 migrations `20260811_005_prediction_v2.sql`,
`20260813_006_prediction_hash_algorithm.sql`, and
`20260813_007_shadow_mirror_contract.sql` are also operator-applied, not automatic.
Before applying them in order, use a superuser or an account that is a member of
PostgreSQL's `pg_create_role` role to execute and verify
`prerequisites/prediction_rpc_owner.sql`. Do not substitute an application or browser
credential for this role-capable operator step. Then confirm migrations 001-004 are
present, take a schema backup, and verify that existing competitor IDs match the stable
IDs trusted callers will send. Apply the complete transaction as a trusted operator.
Never expose the `service_role` key to a browser or mobile client.

Migration 005 creates four additive append-only mirror tables and the
`append_prediction_ledger_v2(JSONB)` transactional RPC. It forces RLS, revokes `anon`
and `authenticated`, grants the RPC only to `service_role`, and installs UPDATE/DELETE
rejection triggers. The RPC must be owned by the dedicated
`strathmark_prediction_rpc_owner` role with `NOINHERIT`, `NOSUPERUSER`, `NOCREATEDB`,
`NOCREATEROLE`, `NOREPLICATION`, `NOLOGIN`, `NOBYPASSRLS`, and no role memberships,
has an empty search path, and uses fully qualified `public` relations and indexes. The
service role cannot mutate the tables directly.
Local SQLite remains the race-day authority.

Migration 006 adds the request `hash_algorithm` column and replaces the RPC so old
`raw-v1` rows and queued payloads remain replayable while new `active-v2` hashes are
recorded explicitly. It does not rewrite existing evidence.
Its guarded `20260813_006_prediction_hash_algorithm.down.sql` rollback restores the
pre-006 RPC and refuses to drop the version column after any `active-v2` row exists.
It takes an `ACCESS EXCLUSIVE` lock before inspecting or changing the request table and
sets `row_security=off`, so concurrent appends cannot cross the guard and an executor
whose RLS policy could hide evidence fails closed.
Across migration 005, migration 006, and the guarded 006 rollback, idempotency means
an exact typed projection retry: the complete request row and exact prediction and
feature sets (including counts) must match. Changed, missing, extra, or cross-request
child IDs conflict and the whole RPC call rolls back. Settlement retries likewise
compare every stored typed field. New corrections must increment the latest revision,
supersede that exact settlement, carry a non-empty reason, and keep residual within
`1e-9` seconds of `actual_time - median_seconds` to allow only float serialization noise.
Before any JSON-to-record conversion, all three RPC versions require closed request,
prediction, feature, and settlement key sets plus the JSON type expected by every typed
column. The seven original request fields are mandatory; `hash_algorithm` is the sole
optional version field so queued legacy `raw-v1` payloads remain replayable. Migration
005 and the 006 rollback accept only an omitted or explicit `raw-v1` algorithm, while
migration 006 additionally accepts `active-v2`. No other missing, extra, Boolean-as-
integer, fractional-integer, string-as-number, or nested replacement value is accepted.

Migration 007 adds four separate shadow-evidence tables and the
`append_shadow_mirror_v1(JSONB)` RPC. A versioned delivery envelope contains either an
immutable receipt core plus its 005/006 ledger projection, or one field-atomic numeric
settle/void revision. It retains the exact caller/request/competitor linkage,
observation schema and fingerprint, and delivery schema/hash metadata. It does not
store Missoula's operational outcome/context history, display names, narrative notes,
email addresses, or secrets. The delivery hash covers an explicit JSON body string;
the RPC recomputes SHA-256 with PostgreSQL core functions and requires that string's
parsed JSON to equal the submitted envelope without delivery metadata. Exact semantic
retries therefore tolerate harmless JSON whitespace without allowing a false digest to
reserve an outbox ID. The RPC also enforces the frozen receipt-core fields, nested
identity linkages, and a unique receipt prediction-ID set equal to the embedded ledger.
Snapshot `diagnostics` and every competitor's `excluded_by_reason` are bounded to 128
lowercase machine-code keys matching `[a-z][a-z0-9_]{0,63}`, with exact nonnegative
integer counts no larger than `2147483647`. Booleans, floats, numeric strings, negative
counts, and nested values fail before any write or duplicate decision.
Existing 005/006 rows are neither updated nor rewritten. Its guarded down file succeeds
only before any shadow delivery is recorded; after activation use a forward repair or
restore from the durable local ledger. Numeric envelopes acquire advisory locks for all
prediction IDs in sorted order before reading legacy or numeric history. A database
constraint trigger takes the same lock and rejects later legacy settlement inserts once
numeric authority exists. The down file locks all four mirror tables in a stable order,
audits with `row_security=off`, removes that trigger, and only then drops mirror objects.

## What lives here

Schema changes only. Data migrations (backfills, re-keying, data cleanup)
live in `scripts/` as Python files, not as SQL migrations, because they
need branching and validation logic that SQL handles poorly.

Current ordered migrations:

1. `20260504_001_add_source_tracking.sql`
2. `20260504_002_ml_state_tables.sql`
3. `20260504_003_rls_reframe.sql`
4. `20260508_004_atomic_model_swap_and_residual_dedup.sql`
5. `20260811_005_prediction_v2.sql`
6. `20260813_006_prediction_hash_algorithm.sql`
7. `20260813_007_shadow_mirror_contract.sql`

Migration 005 rejects explicit `active-v2` payloads, so a deploy window with only 005
cannot silently store an active digest as `raw-v1`; the durable local outbox retries it
after 006 is installed.

Migration 005's rollback drops mirrored ledger data and is therefore destructive to the
cloud copy. Preserve required audit data before rollback. The local SQLite ledger is
not removed.

## Disposable PostgreSQL rehearsal

The executable release gate is `tests/test_postgres_rehearsal.py`. It accepts only an
explicit loopback DSN whose database name starts with `strathmark_rehearsal_`, scrubs
ambient Supabase/Railway/libpq connection variables from child processes, and rejects
the known production project and common production database names before opening a
connection. It creates a uniquely named database, bootstraps the minimum Supabase-shaped
roles, executes the checked-in RPC-owner prerequisite, creates the minimal `competitors`
table, exercises migrations 005/006/007 and both guarded rollback boundaries, and
destroys the database even after a failed check.
The 005/006 portion sends direct-RPC adversaries for partial field retries,
prediction/feature ID collisions, same-hash settlement mutations, stale or skipped
settlement revisions, wrong supersession, missing correction reasons, and inconsistent
residuals. These checks run against the 005 RPC, the 006 RPC, and the restored 006-down
RPC.
The gate also runs bounded two-session races proving that 006/007 rollbacks wait for a
concurrent append, that legacy-first settlement authority becomes the prior numeric
revision, and that numeric-first authority rejects a concurrent legacy append.

CI runs this gate against a PostgreSQL service container. For a local run, provide a
loopback PostgreSQL superuser/controller account that is authorized to create/drop a
database and the four temporary roles:

```powershell
$env:STRATHMARK_REHEARSAL_DSN = 'postgresql://rehearsal:LOCAL_ONLY_PASSWORD@127.0.0.1:5432/strathmark_rehearsal_controller'
python -m pytest tests/test_postgres_rehearsal.py -v
Remove-Item Env:STRATHMARK_REHEARSAL_DSN
```

Never substitute a hosted Supabase URL or a production database. This proves PostgreSQL
schema, role, RLS, trigger, grant, RPC, idempotency, atomicity, and rollback semantics;
it does not claim hosted Supabase Auth/REST parity and does not authorize production
application.

## Test coverage

Every migration MUST be accompanied by tests in `tests/` that exercise the new schema.
The 005/006/007 gate uses disposable loopback PostgreSQL. Legacy integration tests against a
real Supabase remain separately gated by `STRATHMARK_TEST_DB=1` and a non-production
project; they are not part of the migration rehearsal.
