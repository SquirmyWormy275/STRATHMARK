-- Migration: Prediction Engine V2 trusted append-only ledger
-- Date:      2026-08-11
-- Author:    Prediction Engine V2 implementation
-- Reversible: yes (DROP removes mirrored ledger data)
-- Idempotent: yes for schema objects and grants
--
-- Local SQLite remains the race-day authority.  These tables are an optional
-- stable-ID-only mirror.  No browser role can write; the single transactional
-- RPC is granted only to service_role.

BEGIN;

CREATE TABLE IF NOT EXISTS prediction_ledger_requests (
    ledger_request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    event_code TEXT NOT NULL CHECK (event_code IN ('SB', 'UH')),
    prediction_as_of DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_ledger_predictions (
    prediction_id TEXT PRIMARY KEY,
    ledger_request_id TEXT NOT NULL
        REFERENCES prediction_ledger_requests(ledger_request_id),
    competitor_id TEXT NOT NULL REFERENCES competitors(competitor_id),
    event_code TEXT NOT NULL CHECK (event_code IN ('SB', 'UH')),
    median_seconds NUMERIC NOT NULL CHECK (median_seconds > 0),
    assigned_mark INTEGER NOT NULL CHECK (assigned_mark >= 3),
    source TEXT NOT NULL,
    training_eligible BOOLEAN NOT NULL,
    engine_version TEXT,
    model_version TEXT,
    calibration_version TEXT,
    evidence_cutoff DATE,
    interval_lower NUMERIC,
    interval_upper NUMERIC,
    interval_coverage NUMERIC,
    interval_state TEXT,
    interval_scope TEXT,
    ignored_factors JSONB NOT NULL DEFAULT '[]'::jsonb,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    optimizer TEXT,
    optimizer_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (ledger_request_id, competitor_id)
);

CREATE TABLE IF NOT EXISTS prediction_ledger_features (
    feature_snapshot_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL
        REFERENCES prediction_ledger_predictions(prediction_id),
    feature_name TEXT NOT NULL,
    numeric_value DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (prediction_id, feature_name)
);

CREATE TABLE IF NOT EXISTS prediction_ledger_settlements (
    settlement_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL
        REFERENCES prediction_ledger_predictions(prediction_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    competitor_id TEXT NOT NULL REFERENCES competitors(competitor_id),
    event_code TEXT NOT NULL CHECK (event_code IN ('SB', 'UH')),
    actual_time NUMERIC NOT NULL CHECK (actual_time > 0),
    residual NUMERIC NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    supersedes_settlement_id TEXT
        REFERENCES prediction_ledger_settlements(settlement_id),
    settled_at TIMESTAMPTZ NOT NULL,
    UNIQUE (prediction_id, revision),
    UNIQUE (prediction_id, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_prediction_ledger_competitor
    ON prediction_ledger_predictions (competitor_id, event_code);
CREATE INDEX IF NOT EXISTS idx_prediction_ledger_settlement_current
    ON prediction_ledger_settlements (prediction_id, revision DESC);

CREATE OR REPLACE FUNCTION reject_prediction_ledger_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'prediction ledger rows are append-only';
END;
$$;

DROP TRIGGER IF EXISTS prediction_ledger_requests_immutable
    ON prediction_ledger_requests;
CREATE TRIGGER prediction_ledger_requests_immutable
BEFORE UPDATE OR DELETE ON prediction_ledger_requests
FOR EACH ROW EXECUTE FUNCTION reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS prediction_ledger_predictions_immutable
    ON prediction_ledger_predictions;
CREATE TRIGGER prediction_ledger_predictions_immutable
BEFORE UPDATE OR DELETE ON prediction_ledger_predictions
FOR EACH ROW EXECUTE FUNCTION reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS prediction_ledger_features_immutable
    ON prediction_ledger_features;
CREATE TRIGGER prediction_ledger_features_immutable
BEFORE UPDATE OR DELETE ON prediction_ledger_features
FOR EACH ROW EXECUTE FUNCTION reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS prediction_ledger_settlements_immutable
    ON prediction_ledger_settlements;
CREATE TRIGGER prediction_ledger_settlements_immutable
BEFORE UPDATE OR DELETE ON prediction_ledger_settlements
FOR EACH ROW EXECUTE FUNCTION reject_prediction_ledger_mutation();

ALTER TABLE prediction_ledger_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE prediction_ledger_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE prediction_ledger_predictions ENABLE ROW LEVEL SECURITY;
ALTER TABLE prediction_ledger_predictions FORCE ROW LEVEL SECURITY;
ALTER TABLE prediction_ledger_features ENABLE ROW LEVEL SECURITY;
ALTER TABLE prediction_ledger_features FORCE ROW LEVEL SECURITY;
ALTER TABLE prediction_ledger_settlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE prediction_ledger_settlements FORCE ROW LEVEL SECURITY;

REVOKE ALL ON prediction_ledger_requests FROM PUBLIC, anon, authenticated;
REVOKE ALL ON prediction_ledger_predictions FROM PUBLIC, anon, authenticated;
REVOKE ALL ON prediction_ledger_features FROM PUBLIC, anon, authenticated;
REVOKE ALL ON prediction_ledger_settlements FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT ON prediction_ledger_requests TO service_role;
GRANT SELECT, INSERT ON prediction_ledger_predictions TO service_role;
GRANT SELECT, INSERT ON prediction_ledger_features TO service_role;
GRANT SELECT, INSERT ON prediction_ledger_settlements TO service_role;

CREATE OR REPLACE FUNCTION append_prediction_ledger_v2(ledger_payload JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    request_row JSONB;
    existing_hash TEXT;
BEGIN
    IF ledger_payload ? 'settlement' THEN
        INSERT INTO prediction_ledger_settlements (
            settlement_id, prediction_id, revision, competitor_id, event_code,
            actual_time, residual, actor, reason, payload_hash,
            supersedes_settlement_id, settled_at
        )
        SELECT
            row.settlement_id, row.prediction_id, row.revision,
            row.competitor_id, row.event_code, row.actual_time, row.residual,
            row.actor, row.reason, row.payload_hash,
            row.supersedes_settlement_id, row.settled_at
        FROM jsonb_to_record(ledger_payload->'settlement') AS row(
            settlement_id TEXT, prediction_id TEXT, revision INTEGER,
            competitor_id TEXT, event_code TEXT, actual_time NUMERIC,
            residual NUMERIC, actor TEXT, reason TEXT, payload_hash TEXT,
            supersedes_settlement_id TEXT, settled_at TIMESTAMPTZ
        )
        ON CONFLICT (prediction_id, payload_hash) DO NOTHING;
        RETURN jsonb_build_object('accepted', TRUE, 'kind', 'settlement');
    END IF;

    request_row := ledger_payload->'request';
    INSERT INTO prediction_ledger_requests (
        ledger_request_id, request_hash, event_code, prediction_as_of, created_at
    ) VALUES (
        request_row->>'ledger_request_id', request_row->>'request_hash',
        request_row->>'event_code', (request_row->>'prediction_as_of')::DATE,
        (request_row->>'created_at')::TIMESTAMPTZ
    ) ON CONFLICT (ledger_request_id) DO NOTHING;

    SELECT request_hash INTO existing_hash
    FROM prediction_ledger_requests
    WHERE ledger_request_id = request_row->>'ledger_request_id';
    IF existing_hash IS DISTINCT FROM request_row->>'request_hash' THEN
        RAISE EXCEPTION 'ledger request hash conflict';
    END IF;

    INSERT INTO prediction_ledger_predictions (
        prediction_id, ledger_request_id, competitor_id, event_code,
        median_seconds, assigned_mark, source, training_eligible,
        engine_version, model_version, calibration_version, evidence_cutoff,
        interval_lower, interval_upper, interval_coverage, interval_state,
        interval_scope, ignored_factors, warnings, optimizer,
        optimizer_metadata, created_at
    )
    SELECT
        row.prediction_id, row.ledger_request_id, row.competitor_id,
        row.event_code, row.median_seconds, row.assigned_mark, row.source,
        row.training_eligible, row.engine_version, row.model_version,
        row.calibration_version, row.evidence_cutoff, row.interval_lower,
        row.interval_upper, row.interval_coverage, row.interval_state,
        row.interval_scope, COALESCE(row.ignored_factors, '[]'::jsonb),
        COALESCE(row.warnings, '[]'::jsonb), row.optimizer,
        COALESCE(row.optimizer_metadata, '{}'::jsonb), row.created_at
    FROM jsonb_to_recordset(ledger_payload->'predictions') AS row(
        prediction_id TEXT, ledger_request_id TEXT, competitor_id TEXT,
        event_code TEXT, median_seconds NUMERIC, assigned_mark INTEGER,
        source TEXT, training_eligible BOOLEAN, engine_version TEXT,
        model_version TEXT, calibration_version TEXT, evidence_cutoff DATE,
        interval_lower NUMERIC, interval_upper NUMERIC,
        interval_coverage NUMERIC, interval_state TEXT, interval_scope TEXT,
        ignored_factors JSONB, warnings JSONB, optimizer TEXT,
        optimizer_metadata JSONB, created_at TIMESTAMPTZ
    )
    ON CONFLICT (prediction_id) DO NOTHING;

    INSERT INTO prediction_ledger_features (
        feature_snapshot_id, prediction_id, feature_name, numeric_value, created_at
    )
    SELECT
        row.feature_snapshot_id, row.prediction_id, row.feature_name,
        row.numeric_value, row.created_at
    FROM jsonb_to_recordset(ledger_payload->'features') AS row(
        feature_snapshot_id TEXT, prediction_id TEXT, feature_name TEXT,
        numeric_value DOUBLE PRECISION, created_at TIMESTAMPTZ
    )
    ON CONFLICT (feature_snapshot_id) DO NOTHING;

    RETURN jsonb_build_object('accepted', TRUE, 'kind', 'field');
END;
$$;

REVOKE ALL ON FUNCTION append_prediction_ledger_v2(JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION append_prediction_ledger_v2(JSONB) TO service_role;

COMMIT;

-- Rollback (destructive to mirrored ledger data; local SQLite remains intact):
-- BEGIN;
-- DROP FUNCTION IF EXISTS append_prediction_ledger_v2(JSONB);
-- DROP FUNCTION IF EXISTS reject_prediction_ledger_mutation();
-- DROP TABLE IF EXISTS prediction_ledger_settlements;
-- DROP TABLE IF EXISTS prediction_ledger_features;
-- DROP TABLE IF EXISTS prediction_ledger_predictions;
-- DROP TABLE IF EXISTS prediction_ledger_requests;
-- COMMIT;
