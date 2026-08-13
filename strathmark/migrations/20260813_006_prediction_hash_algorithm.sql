-- Migration: Version Prediction Engine V2 request hashes in the cloud mirror
-- Date:      2026-08-13
-- Reversible: yes (the column may be dropped after replacing the RPC)
-- Idempotent: yes for the column, function, and grants
--
-- Migration 005 predated active-evidence request hashing. Existing mirrored rows
-- are therefore raw-v1. New callers send an explicit active-v2 value, while an
-- omitted value remains raw-v1 so queued pre-006 payloads can still be replayed.

BEGIN;

ALTER TABLE prediction_ledger_requests
    ADD COLUMN IF NOT EXISTS hash_algorithm TEXT NOT NULL DEFAULT 'raw-v1'
        CHECK (hash_algorithm IN ('raw-v1', 'active-v2'));

CREATE OR REPLACE FUNCTION append_prediction_ledger_v2(ledger_payload JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    request_row JSONB;
    incoming_algorithm TEXT;
    existing_hash TEXT;
    existing_algorithm TEXT;
    existing_request_id TEXT;
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
    incoming_algorithm := COALESCE(
        NULLIF(request_row->>'hash_algorithm', ''),
        'raw-v1'
    );
    IF incoming_algorithm NOT IN ('raw-v1', 'active-v2') THEN
        RAISE EXCEPTION 'unsupported ledger request hash algorithm';
    END IF;

    INSERT INTO prediction_ledger_requests (
        ledger_request_id, caller_id, request_id, request_hash, hash_algorithm,
        event_code, prediction_as_of, created_at
    ) VALUES (
        request_row->>'ledger_request_id', request_row->>'caller_id',
        request_row->>'request_id', request_row->>'request_hash',
        incoming_algorithm, request_row->>'event_code',
        (request_row->>'prediction_as_of')::DATE,
        (request_row->>'created_at')::TIMESTAMPTZ
    ) ON CONFLICT (caller_id, request_id) DO NOTHING;

    SELECT ledger_request_id, request_hash, hash_algorithm
    INTO existing_request_id, existing_hash, existing_algorithm
    FROM prediction_ledger_requests
    WHERE caller_id = request_row->>'caller_id'
      AND request_id = request_row->>'request_id';
    IF existing_algorithm IS DISTINCT FROM incoming_algorithm THEN
        RAISE EXCEPTION 'ledger request hash algorithm conflict';
    END IF;
    IF existing_hash IS DISTINCT FROM request_row->>'request_hash' THEN
        RAISE EXCEPTION 'ledger request hash conflict';
    END IF;
    IF existing_request_id IS DISTINCT FROM request_row->>'ledger_request_id' THEN
        RETURN jsonb_build_object('accepted', TRUE, 'kind', 'field', 'duplicate', TRUE);
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

-- Rollback: apply 20260813_006_prediction_hash_algorithm.down.sql only after
-- disabling active-v2 mirroring. It aborts if any active-v2 rows already exist.
