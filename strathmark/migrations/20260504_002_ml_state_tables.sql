-- Migration: ML state schema
-- Date:      2026-05-04
-- Author:    persistence-reframe PR (Phase 4 of the controlled-write migration plan)
-- Reversible: yes (DROP TABLE removes the new tables; data lost on rollback)
-- Idempotent: yes (uses CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)
--
-- Purpose
-- -------
-- Establishes persistence for STRATHMARK's ML state. Today the XGBoost
-- ensemble trains in-memory inside HandicapCalculator and dies with the
-- process. After this migration:
--
--   - Trained models are persisted with full provenance (model_versions).
--   - Calibration artifacts attach to a specific model version
--     (calibration_tables).
--   - Per-prediction feature vectors are stored at prediction time
--     (feature_store) so retraining can audit what the model actually saw.
--   - Every prediction is logged (predictions table) and later settled with
--     the actual result + residual (settle_prediction).
--   - prediction_residuals gains model_version_id and prediction_id columns,
--     wiring the dormant table into the live ML lifecycle.
--
-- ML state tables are STRATHMARK-internal. Writes to these tables originate
-- in STRATHMARK itself, NOT from the future MNEMEX sync function. This is
-- the explicit carve-out from the controlled-write rule. RLS for the
-- carve-out lands in the controlled-write follow-on PR.

BEGIN;

-- ---------------------------------------------------------------------------
-- model_versions: catalog of every trained model
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS model_versions (
    model_version_id      TEXT         PRIMARY KEY,            -- ULID
    model_type            TEXT         NOT NULL,                -- e.g. 'xgboost_lightgbm_ensemble'
    trained_at            TIMESTAMPTZ  NOT NULL,
    training_data_cutoff  TIMESTAMPTZ  NOT NULL,                -- latest result_date in training set
    training_row_count    INTEGER      NOT NULL,
    hyperparameters       JSONB        NOT NULL,                -- Optuna-tuned values, feature list
    artifact_storage      TEXT         NOT NULL,                -- 'supabase_storage' | 'inline_jsonb'
    artifact_ref          TEXT         NOT NULL,                -- storage path or inline blob ID
    artifact_size_bytes   INTEGER      NOT NULL,
    is_active             BOOLEAN      NOT NULL DEFAULT FALSE,
    retired_at            TIMESTAMPTZ,
    notes                 TEXT
);

ALTER TABLE model_versions
    DROP CONSTRAINT IF EXISTS model_versions_artifact_storage_check;
ALTER TABLE model_versions
    ADD CONSTRAINT model_versions_artifact_storage_check
    CHECK (artifact_storage IN ('supabase_storage', 'inline_jsonb'));

-- Only one active model per model_type. Enforced via partial unique index so
-- multiple is_active=FALSE rows of the same type are allowed.
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_versions_one_active_per_type
    ON model_versions (model_type)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_model_versions_trained_at
    ON model_versions (trained_at DESC);

-- ---------------------------------------------------------------------------
-- calibration_tables: calibration artifacts per model version
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS calibration_tables (
    calibration_id      TEXT         PRIMARY KEY,                -- ULID
    model_version_id    TEXT         NOT NULL REFERENCES model_versions(model_version_id),
    calibrated_at       TIMESTAMPTZ  NOT NULL,
    calibration_method  TEXT         NOT NULL,                    -- 'conformal_prediction' | 'platt' | 'isotonic' | 'uncertainty_toolbox'
    calibration_data    JSONB        NOT NULL,                    -- the calibration table itself
    holdout_residuals   JSONB        NOT NULL,                    -- residuals on the calibration holdout
    crps_score          NUMERIC,                                  -- properscoring CRPS, lower better
    coverage_at_90      NUMERIC,                                  -- conformal interval coverage, target ~0.90
    notes               TEXT
);

ALTER TABLE calibration_tables
    DROP CONSTRAINT IF EXISTS calibration_tables_method_check;
ALTER TABLE calibration_tables
    ADD CONSTRAINT calibration_tables_method_check
    CHECK (calibration_method IN ('conformal_prediction', 'platt', 'isotonic', 'uncertainty_toolbox'));

CREATE INDEX IF NOT EXISTS idx_calibration_tables_model_version
    ON calibration_tables (model_version_id);

-- ---------------------------------------------------------------------------
-- feature_store: per-prediction feature vector capture
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS feature_store (
    feature_set_id    TEXT         PRIMARY KEY,                  -- ULID
    model_version_id  TEXT         NOT NULL REFERENCES model_versions(model_version_id),
    competitor_id     TEXT         NOT NULL REFERENCES competitors(competitor_id),
    event_code        TEXT         NOT NULL,
    features_jsonb    JSONB        NOT NULL,                     -- the 27-feature vector
    computed_at       TIMESTAMPTZ  NOT NULL,
    UNIQUE (model_version_id, competitor_id, event_code)
);

CREATE INDEX IF NOT EXISTS idx_feature_store_competitor
    ON feature_store (competitor_id, event_code);

CREATE INDEX IF NOT EXISTS idx_feature_store_model_version
    ON feature_store (model_version_id);

-- ---------------------------------------------------------------------------
-- predictions: every prediction logged for later settlement
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id        TEXT         PRIMARY KEY,                  -- ULID
    model_version_id     TEXT         NOT NULL REFERENCES model_versions(model_version_id),
    competitor_id        TEXT         NOT NULL REFERENCES competitors(competitor_id),
    event_code           TEXT         NOT NULL,
    show_name            TEXT         NOT NULL,
    predicted_time       NUMERIC      NOT NULL,
    predicted_variance   NUMERIC      NOT NULL,
    cascade_level_used   TEXT         NOT NULL,                      -- which cascade tier produced this prediction
    predicted_at         TIMESTAMPTZ  NOT NULL,
    result_id            INTEGER      REFERENCES results(result_id), -- set when actual result lands
    residual             NUMERIC,                                    -- set when actual result lands
    notes                TEXT
);

ALTER TABLE predictions
    DROP CONSTRAINT IF EXISTS predictions_cascade_level_check;
ALTER TABLE predictions
    ADD CONSTRAINT predictions_cascade_level_check
    CHECK (cascade_level_used IN ('manual', 'llm', 'ml', 'baseline', 'panel_fallback'));

CREATE INDEX IF NOT EXISTS idx_predictions_competitor_event
    ON predictions (competitor_id, event_code);

CREATE INDEX IF NOT EXISTS idx_predictions_model_version
    ON predictions (model_version_id);

CREATE INDEX IF NOT EXISTS idx_predictions_unsettled
    ON predictions (predicted_at)
    WHERE result_id IS NULL;

-- ---------------------------------------------------------------------------
-- prediction_residuals: extend with model linkage
-- ---------------------------------------------------------------------------
--
-- The table existed before this migration (see migration 0 baseline state in
-- docs/schema-reality-2026-05-04.md). Previously dormant: 0 rows. We add
-- model_version_id and prediction_id columns so that every residual is
-- traceable to a specific model and a specific prediction.
--
-- Both new columns are NULLABLE here because the existing zero rows
-- technically have no model linkage. Application code MUST require these
-- fields going forward (validated in record_prediction_residuals); we do
-- not enforce NOT NULL at the DB layer because that would break the
-- migration if rows ever existed.

ALTER TABLE prediction_residuals
    ADD COLUMN IF NOT EXISTS model_version_id TEXT,
    ADD COLUMN IF NOT EXISTS prediction_id    TEXT;

-- FK constraints, separately so we can reference newly created tables.
ALTER TABLE prediction_residuals
    DROP CONSTRAINT IF EXISTS prediction_residuals_model_version_fkey;
ALTER TABLE prediction_residuals
    ADD CONSTRAINT prediction_residuals_model_version_fkey
    FOREIGN KEY (model_version_id) REFERENCES model_versions(model_version_id);

ALTER TABLE prediction_residuals
    DROP CONSTRAINT IF EXISTS prediction_residuals_prediction_fkey;
ALTER TABLE prediction_residuals
    ADD CONSTRAINT prediction_residuals_prediction_fkey
    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id);

CREATE INDEX IF NOT EXISTS idx_prediction_residuals_model_version
    ON prediction_residuals (model_version_id);

CREATE INDEX IF NOT EXISTS idx_prediction_residuals_competitor
    ON prediction_residuals (competitor_id);

COMMIT;

-- ---------------------------------------------------------------------------
-- Rollback (run in psql or the Supabase SQL editor to undo this migration)
-- ---------------------------------------------------------------------------
--
-- WARNING: rollback drops all rows in the four new tables and removes the
-- model linkage columns from prediction_residuals. ML state will be lost.
--
-- BEGIN;
--
-- ALTER TABLE prediction_residuals
--     DROP CONSTRAINT IF EXISTS prediction_residuals_model_version_fkey,
--     DROP CONSTRAINT IF EXISTS prediction_residuals_prediction_fkey,
--     DROP COLUMN IF EXISTS model_version_id,
--     DROP COLUMN IF EXISTS prediction_id;
-- DROP INDEX IF EXISTS idx_prediction_residuals_model_version;
-- DROP INDEX IF EXISTS idx_prediction_residuals_competitor;
--
-- DROP TABLE IF EXISTS predictions;
-- DROP TABLE IF EXISTS feature_store;
-- DROP TABLE IF EXISTS calibration_tables;
-- DROP TABLE IF EXISTS model_versions;
--
-- COMMIT;
