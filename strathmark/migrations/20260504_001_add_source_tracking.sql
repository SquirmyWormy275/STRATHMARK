-- Migration: add source-tracking and provenance fields
-- Date:      2026-05-04
-- Author:    persistence-reframe PR (Phase 3 of the controlled-write migration plan)
-- Reversible: yes (DROP COLUMN restores the prior shape; data in dropped columns is lost)
-- Idempotent: yes (uses ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS where needed)
--
-- Purpose
-- -------
-- Reframes STRATHMARK Supabase from "read-only by convention" toward
-- "controlled-write by enforcement". This migration adds the columns the
-- future MNEMEX sync function needs to track provenance, sync state, and
-- which sync path produced each row. RLS enforcement and the sync function
-- itself land in follow-on PRs once MNEMEX Supabase exists.
--
-- All additions are nullable on existing rows. Existing 1311 results rows,
-- 85 competitors rows, and 1 sync_log row receive the column defaults
-- specified below.

BEGIN;

-- ---------------------------------------------------------------------------
-- results: source-tracking columns
-- ---------------------------------------------------------------------------

ALTER TABLE results
    ADD COLUMN IF NOT EXISTS mnemex_id TEXT,
    ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;

-- mnemex_id will be populated by the future sync function. Existing rows have
-- no MNEMEX counterpart yet and stay NULL until re-keying lands in the
-- controlled-write follow-on PR.
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_mnemex_id
    ON results (mnemex_id)
    WHERE mnemex_id IS NOT NULL;

-- source_type taxonomy (enforced by check constraint, not enum, so it's easy
-- to extend without a Postgres ALTER TYPE):
ALTER TABLE results
    DROP CONSTRAINT IF EXISTS results_source_type_check;
ALTER TABLE results
    ADD CONSTRAINT results_source_type_check
    CHECK (source_type IN ('legacy', 'mnemex_sync', 'prediction_residual_write'));

-- ---------------------------------------------------------------------------
-- competitors: MNEMEX cross-ref column
-- ---------------------------------------------------------------------------

ALTER TABLE competitors
    ADD COLUMN IF NOT EXISTS mnemex_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_competitors_mnemex_id
    ON competitors (mnemex_id)
    WHERE mnemex_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- sync_log: operational telemetry for the future sync function
-- ---------------------------------------------------------------------------

ALTER TABLE sync_log
    ADD COLUMN IF NOT EXISTS sync_path TEXT NOT NULL DEFAULT 'legacy_unknown',
    ADD COLUMN IF NOT EXISTS mnemex_cursor TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rows_pulled INTEGER,
    ADD COLUMN IF NOT EXISTS rows_upserted INTEGER,
    ADD COLUMN IF NOT EXISTS errors_jsonb JSONB;

ALTER TABLE sync_log
    DROP CONSTRAINT IF EXISTS sync_log_sync_path_check;
ALTER TABLE sync_log
    ADD CONSTRAINT sync_log_sync_path_check
    CHECK (sync_path IN ('legacy_unknown', 'nightly_batch', 'strathex_finalization', 'manual_force_sync'));

COMMIT;

-- ---------------------------------------------------------------------------
-- Rollback (run in psql or the Supabase SQL editor to undo this migration)
-- ---------------------------------------------------------------------------
--
-- BEGIN;
--
-- ALTER TABLE results
--     DROP CONSTRAINT IF EXISTS results_source_type_check,
--     DROP COLUMN IF EXISTS mnemex_id,
--     DROP COLUMN IF EXISTS source_type,
--     DROP COLUMN IF EXISTS last_synced_at;
-- DROP INDEX IF EXISTS idx_results_mnemex_id;
--
-- ALTER TABLE competitors
--     DROP COLUMN IF EXISTS mnemex_id;
-- DROP INDEX IF EXISTS idx_competitors_mnemex_id;
--
-- ALTER TABLE sync_log
--     DROP CONSTRAINT IF EXISTS sync_log_sync_path_check,
--     DROP COLUMN IF EXISTS sync_path,
--     DROP COLUMN IF EXISTS mnemex_cursor,
--     DROP COLUMN IF EXISTS rows_pulled,
--     DROP COLUMN IF EXISTS rows_upserted,
--     DROP COLUMN IF EXISTS errors_jsonb;
--
-- COMMIT;
