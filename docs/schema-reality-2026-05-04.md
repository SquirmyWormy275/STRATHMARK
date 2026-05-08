# Schema Reality Report — 2026-05-04

Status: PHASE 1 COMPLETE for column-level metadata. Indexes / RLS policies / triggers
remain GATED behind pg_catalog (see "Known gaps" section below).

This report is the new ground-truth reference for the live STRATHMARK Supabase schema.
The docstring at the top of `strathmark/db.py` has been updated to match.

## Project under inspection

- Project ref: `iordtvxryrdhqvdkfgzf`
- Org: STRATHEX
- Region: us-east-1
- Status when verified: live (resumed from a paused state earlier in this session)

## How verification was done

PostgREST OpenAPI spec at `/rest/v1/` returned full column metadata (types, nullability,
defaults, PKs, FKs) for the 5 tables in the public schema. Direct PostgREST GETs were
used to count rows, sample row shape, and tally column distributions. No DDL was issued.

`information_schema` is NOT exposed via PostgREST in this project (only `public` and
`graphql_public` are). pg_catalog access for indexes / RLS / triggers would require
either (a) exposing those schemas in the Supabase project settings, (b) creating a
custom RPC that returns pg_catalog rows, or (c) a direct Postgres connection with the
DB password. None of those happened in this session because Phase 1 is constrained to
read-only queries and the DB password was not provided.

## Live schema (verified)

### `competitors`
- `competitor_id` text, PK, NOT NULL
- `name` text, NOT NULL
- `country` text, nullable
- `state_province` text, nullable
- `gender` text, nullable
- `region` text, nullable
- `created_at` timestamptz, default `now()`, nullable

Row count: 85.
ID format observed: `C001` through `C085` (uppercase `C` prefix + 3-digit zero-padded number).

### `results`
- `result_id` integer, PK, NOT NULL
- `competitor_id` text, FK to `competitors(competitor_id)`, nullable
- `event` text, NOT NULL
- `time_seconds` numeric, NOT NULL
- `size_mm` integer, NOT NULL
- `species_code` text, NOT NULL
- `result_date` date, nullable
- `show_name` text, NOT NULL
- `source_app` text, nullable
- `notes` text, nullable
- `created_at` timestamptz, default `now()`, nullable
- `field_strength` numeric, nullable

Row count: 1311. Single insert batch on 2026-03-10 with `source_app='initial-seed'` and
`show_name='Historical'`. Event split SB=591 / UH=720. Species codes span S01, S03-S06,
S08-S10. Date range 2015-2025. `field_strength` is non-null on 0 of 1311 rows (column
exists but has never been populated).

### `sync_log`
- `sync_id` integer, PK, NOT NULL
- `show_name` text, NOT NULL
- `source_app` text, nullable
- `records_written` integer, nullable
- `synced_at` timestamptz, default `now()`, nullable

Row count: 1. Only the initial-seed batch row. No operational writes have ever occurred.

### `prediction_residuals`
- `residual_id` integer, PK, NOT NULL
- `competitor_id` text, FK to `competitors(competitor_id)`, nullable
- `predicted_time` numeric, NOT NULL
- `actual_time` numeric, NOT NULL
- `residual` numeric, NOT NULL
- `show_name` text, NOT NULL
- `event_code` text, NOT NULL
- `result_date` date, nullable
- `created_at` timestamptz, default `now()`, nullable

Row count: 0. Dormant.

### `wood_species`
- `species_id` text, PK, NOT NULL
- `scientific_name` text, nullable
- `common_name` text, NOT NULL
- `janka_hard` integer, nullable
- `spec_gravity` numeric, nullable
- `crush_strength` integer, nullable
- `shear` integer, nullable
- `mor` integer, nullable
- `moe` integer, nullable
- `country` text, nullable
- `region` text, nullable

Row count: 10. Reference data.

## Diff vs. previous docstring (the surprises)

1. **`prediction_residuals` primary key is `residual_id`, NOT `id`.** The previous docstring
   said `id SERIAL PRIMARY KEY`. The live column is `residual_id`. This was never caught
   by tests because `record_prediction_residuals()` lets the SERIAL auto-generate and
   never references the column by name. Material because Phase 4 ML state design adds
   columns to this table; we need to use the correct PK name.

2. **`results.size_mm` is `integer`, not `numeric`.** Docstring said `size_mm NUMERIC`.
   Live is `integer`. The code path in `push_results` does `float(row["size_mm"])` and
   relies on Postgres to coerce. Coercion succeeds for whole-mm values (which is all
   real data), but if a non-integer mm value were ever passed it would be silently
   truncated or rejected. Worth flagging in the docstring; not breaking today.

3. **`results.field_strength` exists in the live schema, was missing from the docstring.**
   Recon flagged this as a probable divergence; confirmed. Type is `numeric`, nullable,
   100% null in current data.

4. **`wood_species` columns are `mor` and `moe` (lowercase), not `MOR` and `MOE`.**
   Postgres folds unquoted identifiers to lowercase. Docstring used uppercase. The code
   that reads these columns lowercases via PostgREST naturally, so this never broke.

5. **`competitors.competitor_id` format observed in live data is `C` + 3 digits
   (`C001`..`C085`).** The `register_competitor()` function in `db.py:774` mints new IDs
   via `f"C{max_n + 1:04d}"` — that produces 4 digits zero-padded (e.g., `C0086`). Result:
   if `register_competitor()` ever ran against this database it would generate IDs in a
   format different from the existing 85 rows. Has never happened (sync_log only has the
   initial-seed entry), but is a latent bug. **Out of scope for this PR** (locked
   decision: register_competitor rewrite to write to MNEMEX is a follow-on PR).

6. **No operational writes have ever happened to this database.** The `sync_log` table
   has exactly one row, the initial-seed batch on 2026-03-10. Every code path that
   purportedly writes to Supabase (`push_results`, `push_competitors`, `push_results_dicts`,
   `register_competitor`, `validate_deployment.py --write`) has never run in production.
   The "read-only by convention" rule has been de-facto enforced by nobody actually
   exercising the write paths. This MATERIALLY de-risks the controlled-write reframe:
   we are not closing a barn door after horses have left.

## Phase 2 preview (no cleanup needed)

The recon assessment flagged a risk that `validate_deployment.py --write` may have left
stray test rows. **Reality: zero stray rows.** No row in `results` has `source_app`
suggesting a test source, no row has "DO NOT keep" in notes, and the only `source_app`
value present is `'initial-seed'`. Phase 2's audit deliverable is therefore short.

## Known gaps (still GATED, not blocking this PR)

1. **Indexes**: not enumerable via PostgREST. Additive columns cannot break existing
   indexes (those only constrain present columns), so this PR can proceed without the
   index list. Index recommendations for new columns will be specified in the migration
   files based on expected query patterns; the migration files apply via the Supabase
   SQL editor where the operator can also inspect existing indexes.
2. **RLS policies**: not enumerable via PostgREST. RLS reframe is explicitly OUT OF
   SCOPE for this PR (locked decision). RLS audit happens in the controlled-write PR.
3. **Triggers**: not enumerable via PostgREST. **Mild risk:** if a row-level trigger
   exists on `results`, `competitors`, or `prediction_residuals`, `ALTER TABLE ADD
   COLUMN` operations may interact with it (typically a non-issue for additive nullable
   columns; could matter for NOT NULL columns with computed defaults). Recommend
   inspecting via the Supabase dashboard SQL editor before applying the Phase 3
   migration: `SELECT event_object_table, trigger_name, action_timing, event_manipulation
   FROM information_schema.triggers WHERE trigger_schema = 'public';`
4. **Exact PG types beyond OpenAPI's coarse categorization**: OpenAPI calls everything
   `text`, `integer`, `numeric`, `date`, or `timestamp with time zone`. It does not
   distinguish e.g. `varchar(N)` from `text`, or `int4` from `int8`, or `numeric(p,s)`
   from unconstrained `numeric`. For schema additions this rarely matters; we'll use
   `TEXT`, `INTEGER`, `NUMERIC`, `DATE`, `TIMESTAMPTZ`, `JSONB`, `BOOLEAN` consistently.

## Decisions made

- **Will NOT create an introspection RPC** to bypass the pg_catalog gap. The Phase 1
  constraint says "Read-only queries only", and creating a function is a write even if
  the function itself is read-only. The user can run pg_catalog queries via the
  dashboard SQL editor if those reads are needed later.
- **Will NOT alter the `register_competitor()` ID format** in this PR. The function is
  scheduled for rewrite to write to MNEMEX in a follow-on PR. Touching it now would be
  scope creep.
- **Will use schema-additive migrations only** (ALTER TABLE ADD COLUMN), no destructive
  changes, no column renames, no constraint additions on existing data. This keeps
  rollback trivial (DROP COLUMN if needed).
- **Will document the docstring inversions in the docstring itself**, not silently fix
  them and pretend the divergence never happened. The docstring becomes the audit log.
