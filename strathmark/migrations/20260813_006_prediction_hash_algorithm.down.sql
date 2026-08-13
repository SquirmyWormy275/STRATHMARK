-- Roll back migration 006 only before any active-v2 request has been mirrored.
-- Disable active-v2 cloud mirroring before applying this file.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM prediction_ledger_requests WHERE hash_algorithm = 'active-v2'
    ) THEN
        RAISE EXCEPTION
            'cannot roll back migration 006 while active-v2 request rows exist';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.append_prediction_ledger_v2(
    ledger_payload pg_catalog.jsonb
)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    request_row pg_catalog.jsonb;
    settlement_row pg_catalog.jsonb;
    existing_hash pg_catalog.text;
    existing_request_id pg_catalog.text;
BEGIN
    IF ledger_payload IS NULL
       OR pg_catalog.jsonb_typeof(ledger_payload) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'ledger payload must be an object';
    END IF;
    IF ledger_payload ? 'settlement' THEN
        IF ledger_payload ? 'request'
           OR ledger_payload ? 'predictions'
           OR ledger_payload ? 'features' THEN
            RAISE EXCEPTION 'ledger payload must contain exactly one operation kind';
        END IF;
        settlement_row := ledger_payload->'settlement';
        IF pg_catalog.jsonb_typeof(settlement_row) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'settlement payload must be a non-null object';
        END IF;
        IF NULLIF(settlement_row->>'settlement_id', '') IS NULL
           OR NULLIF(settlement_row->>'prediction_id', '') IS NULL
           OR NULLIF(settlement_row->>'competitor_id', '') IS NULL
           OR NULLIF(settlement_row->>'event_code', '') IS NULL THEN
            RAISE EXCEPTION 'settlement payload is missing required linkage fields';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.prediction_ledger_predictions AS prediction
            WHERE prediction.prediction_id = settlement_row->>'prediction_id'
              AND prediction.competitor_id = settlement_row->>'competitor_id'
              AND prediction.event_code = settlement_row->>'event_code'
        ) THEN
            RAISE EXCEPTION 'settlement prediction linkage mismatch';
        END IF;
        IF NULLIF(settlement_row->>'supersedes_settlement_id', '') IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM public.prediction_ledger_settlements AS prior
               WHERE prior.settlement_id = settlement_row->>'supersedes_settlement_id'
                 AND prior.prediction_id = settlement_row->>'prediction_id'
           ) THEN
            RAISE EXCEPTION 'settlement prediction linkage mismatch';
        END IF;
        INSERT INTO public.prediction_ledger_settlements (
            settlement_id, prediction_id, revision, competitor_id, event_code,
            actual_time, residual, actor, reason, payload_hash,
            supersedes_settlement_id, settled_at
        )
        SELECT
            row.settlement_id, row.prediction_id, row.revision,
            row.competitor_id, row.event_code, row.actual_time, row.residual,
            row.actor, row.reason, row.payload_hash,
            row.supersedes_settlement_id, row.settled_at
        FROM pg_catalog.jsonb_to_record(ledger_payload->'settlement') AS row(
            settlement_id TEXT, prediction_id TEXT, revision INTEGER,
            competitor_id TEXT, event_code TEXT, actual_time NUMERIC,
            residual NUMERIC, actor TEXT, reason TEXT, payload_hash TEXT,
            supersedes_settlement_id TEXT, settled_at TIMESTAMPTZ
        )
        ON CONFLICT (prediction_id, payload_hash) DO NOTHING;
        RETURN pg_catalog.jsonb_build_object('accepted', TRUE, 'kind', 'settlement');
    END IF;

    IF NOT ledger_payload ? 'request' THEN
        RAISE EXCEPTION 'field request must be a non-null object';
    END IF;
    IF NOT ledger_payload ? 'predictions' THEN
        RAISE EXCEPTION 'field predictions must be a non-empty array';
    END IF;
    IF NOT ledger_payload ? 'features' THEN
        RAISE EXCEPTION 'field features must be an array';
    END IF;
    request_row := ledger_payload->'request';
    IF pg_catalog.jsonb_typeof(request_row) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'field request must be a non-null object';
    END IF;
    IF pg_catalog.jsonb_typeof(ledger_payload->'predictions') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(ledger_payload->'predictions') = 0 THEN
        RAISE EXCEPTION 'field predictions must be a non-empty array';
    END IF;
    IF pg_catalog.jsonb_typeof(ledger_payload->'features') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'field features must be an array';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(ledger_payload->'predictions') AS item
        WHERE pg_catalog.jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR item->>'ledger_request_id' IS DISTINCT FROM request_row->>'ledger_request_id'
           OR item->>'event_code' IS DISTINCT FROM request_row->>'event_code'
    ) THEN
        RAISE EXCEPTION 'prediction request linkage mismatch';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(ledger_payload->'features') AS feature
        WHERE pg_catalog.jsonb_typeof(feature) IS DISTINCT FROM 'object'
           OR NOT EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_array_elements(ledger_payload->'predictions') AS prediction
               WHERE prediction->>'prediction_id' = feature->>'prediction_id'
           )
    ) THEN
        RAISE EXCEPTION 'feature prediction linkage mismatch';
    END IF;
    IF NULLIF(request_row->>'hash_algorithm', '') IS NOT NULL
       AND request_row->>'hash_algorithm' <> 'raw-v1' THEN
        RAISE EXCEPTION 'active-v2 request hashes require migration 006';
    END IF;

    INSERT INTO public.prediction_ledger_requests (
        ledger_request_id, caller_id, request_id, request_hash,
        event_code, prediction_as_of, created_at
    ) VALUES (
        request_row->>'ledger_request_id', request_row->>'caller_id',
        request_row->>'request_id', request_row->>'request_hash',
        request_row->>'event_code', (request_row->>'prediction_as_of')::DATE,
        (request_row->>'created_at')::TIMESTAMPTZ
    ) ON CONFLICT (caller_id, request_id) DO NOTHING;

    SELECT ledger_request_id, request_hash
    INTO existing_request_id, existing_hash
    FROM public.prediction_ledger_requests
    WHERE caller_id = request_row->>'caller_id'
      AND request_id = request_row->>'request_id';
    IF existing_hash IS DISTINCT FROM request_row->>'request_hash' THEN
        RAISE EXCEPTION 'ledger request hash conflict';
    END IF;
    IF existing_request_id IS DISTINCT FROM request_row->>'ledger_request_id' THEN
        RETURN pg_catalog.jsonb_build_object(
            'accepted', TRUE, 'kind', 'field', 'duplicate', TRUE
        );
    END IF;

    INSERT INTO public.prediction_ledger_predictions (
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
    FROM pg_catalog.jsonb_to_recordset(ledger_payload->'predictions') AS row(
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

    INSERT INTO public.prediction_ledger_features (
        feature_snapshot_id, prediction_id, feature_name, numeric_value, created_at
    )
    SELECT
        row.feature_snapshot_id, row.prediction_id, row.feature_name,
        row.numeric_value, row.created_at
    FROM pg_catalog.jsonb_to_recordset(ledger_payload->'features') AS row(
        feature_snapshot_id TEXT, prediction_id TEXT, feature_name TEXT,
        numeric_value DOUBLE PRECISION, created_at TIMESTAMPTZ
    )
    ON CONFLICT (feature_snapshot_id) DO NOTHING;

    RETURN pg_catalog.jsonb_build_object('accepted', TRUE, 'kind', 'field');
END;
$$;

ALTER FUNCTION public.append_prediction_ledger_v2(pg_catalog.jsonb)
    OWNER TO strathmark_prediction_rpc_owner;
REVOKE ALL ON FUNCTION public.append_prediction_ledger_v2(pg_catalog.jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.append_prediction_ledger_v2(pg_catalog.jsonb)
    TO service_role;

ALTER TABLE prediction_ledger_requests DROP COLUMN hash_algorithm;

COMMIT;
