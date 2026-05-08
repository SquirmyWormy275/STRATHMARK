-- Migration: atomic active-model swap function + prediction_residuals dedup
-- Date:      2026-05-08
-- Author:    triage of v0.5.0 informational findings
-- Reversible: yes (DROP FUNCTION + DROP INDEX)
-- Idempotent: yes (CREATE OR REPLACE FUNCTION + CREATE UNIQUE INDEX IF NOT EXISTS)
--
-- Purpose
-- -------
-- 1. Replace the two-step deactivate-then-activate pattern in
--    `strathmark.db.set_active_model()` with a server-side function that
--    flips both updates in a single transaction. This closes the race
--    window where a crash between the two HTTP calls leaves no active
--    model for a model_type, AND the window where two concurrent
--    activations both think they're transitioning from "no active" to
--    "active" and one violates the partial unique index.
--
-- 2. Add a partial unique index on prediction_residuals(prediction_id)
--    that prevents the same prediction from getting double-residualed.
--    Both `settle_prediction` and `record_prediction_residuals` can
--    legitimately write residual rows; the constraint guarantees only
--    one row per prediction_id, so drift detection counts unique events
--    rather than callsite-duplicates.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Atomic active-model swap function
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_active_model_atomic(target_model_version_id TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    target_model_type TEXT;
BEGIN
    SELECT model_type INTO target_model_type
    FROM model_versions
    WHERE model_version_id = target_model_version_id;

    IF target_model_type IS NULL THEN
        RAISE EXCEPTION 'model_version_id not found: %', target_model_version_id;
    END IF;

    -- Both updates run in this function's implicit transaction. The
    -- partial unique index on (model_type) WHERE is_active = TRUE is
    -- evaluated at COMMIT, so the order of the two UPDATEs is internally
    -- consistent.
    UPDATE model_versions
    SET is_active = FALSE,
        retired_at = NOW()
    WHERE model_type = target_model_type
      AND is_active = TRUE
      AND model_version_id <> target_model_version_id;

    UPDATE model_versions
    SET is_active = TRUE,
        retired_at = NULL
    WHERE model_version_id = target_model_version_id;
END;
$$;

-- Allow the standard caller to invoke the function. Keep the grant tight;
-- this function is operator-action infrastructure, not anonymous-callable.
GRANT EXECUTE ON FUNCTION set_active_model_atomic(TEXT) TO service_role;

-- ---------------------------------------------------------------------------
-- 2. prediction_residuals dedup constraint
-- ---------------------------------------------------------------------------
-- One residual row per prediction_id when prediction_id is set. Rows
-- without a prediction_id (legacy ingestion via record_prediction_residuals
-- before predictions existed, or backfills with no link) are unaffected.

CREATE UNIQUE INDEX IF NOT EXISTS prediction_residuals_prediction_id_key
    ON prediction_residuals (prediction_id)
    WHERE prediction_id IS NOT NULL;

COMMIT;

-- ---------------------------------------------------------------------------
-- Rollback
-- ---------------------------------------------------------------------------
--
-- BEGIN;
-- DROP INDEX IF EXISTS prediction_residuals_prediction_id_key;
-- REVOKE EXECUTE ON FUNCTION set_active_model_atomic(TEXT) FROM service_role;
-- DROP FUNCTION IF EXISTS set_active_model_atomic(TEXT);
-- COMMIT;
