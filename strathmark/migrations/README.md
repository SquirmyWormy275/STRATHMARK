# STRATHMARK Migrations

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

These migrations are NOT applied automatically. The accepted process today is:

1. The author writes the file and gets it merged.
2. An operator with Supabase dashboard access pastes the forward block into
   the SQL editor for the target project (currently `iordtvxryrdhqvdkfgzf`)
   and runs it.
3. The operator records the application timestamp and any notes in
   `docs/migration-log.md` (created on first use; not present yet).

Once the controlled-write reframe lands and the MNEMEX-side sync function
exists, this process will be tightened (likely Supabase CLI based, or a
dedicated migration runner). Until then, the dashboard editor is the
mechanism.

## What lives here

Schema changes only. Data migrations (backfills, re-keying, data cleanup)
live in `scripts/` as Python files, not as SQL migrations, because they
need branching and validation logic that SQL handles poorly.

## Test coverage

Every migration MUST be accompanied by tests in `tests/` that exercise the
new schema. Tests against a real Supabase use `STRATHMARK_TEST_DB=1`
against an isolated test project, never against the production project ref.
See [`tests/test_db.py`](../../tests/test_db.py) for the pattern.
