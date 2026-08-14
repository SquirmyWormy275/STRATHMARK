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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'strathmark_prediction_rpc_owner'
          AND NOT rolinherit
          AND NOT rolsuper
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolreplication
          AND NOT rolcanlogin
          AND NOT rolbypassrls
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_auth_members AS membership
              WHERE membership.member = pg_roles.oid
                 OR membership.roleid = pg_roles.oid
          )
    ) THEN
        RAISE EXCEPTION
            'strathmark_prediction_rpc_owner must be isolated and unprivileged';
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS public.prediction_ledger_requests (
    ledger_request_id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    event_code TEXT NOT NULL CHECK (event_code IN ('SB', 'UH')),
    prediction_as_of DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (caller_id, request_id)
);

CREATE TABLE IF NOT EXISTS public.prediction_ledger_predictions (
    prediction_id TEXT PRIMARY KEY,
    ledger_request_id TEXT NOT NULL
        REFERENCES public.prediction_ledger_requests(ledger_request_id),
    competitor_id TEXT NOT NULL REFERENCES public.competitors(competitor_id),
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

CREATE TABLE IF NOT EXISTS public.prediction_ledger_features (
    feature_snapshot_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL
        REFERENCES public.prediction_ledger_predictions(prediction_id),
    feature_name TEXT NOT NULL,
    numeric_value DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (prediction_id, feature_name)
);

CREATE TABLE IF NOT EXISTS public.prediction_ledger_settlements (
    settlement_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL
        REFERENCES public.prediction_ledger_predictions(prediction_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    competitor_id TEXT NOT NULL REFERENCES public.competitors(competitor_id),
    event_code TEXT NOT NULL CHECK (event_code IN ('SB', 'UH')),
    actual_time NUMERIC NOT NULL CHECK (actual_time > 0),
    residual NUMERIC NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    supersedes_settlement_id TEXT
        REFERENCES public.prediction_ledger_settlements(settlement_id),
    settled_at TIMESTAMPTZ NOT NULL,
    UNIQUE (prediction_id, revision),
    UNIQUE (prediction_id, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_prediction_ledger_competitor
    ON public.prediction_ledger_predictions (competitor_id, event_code);
CREATE INDEX IF NOT EXISTS idx_prediction_ledger_settlement_current
    ON public.prediction_ledger_settlements (prediction_id, revision DESC);

CREATE OR REPLACE FUNCTION public.reject_prediction_ledger_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'prediction ledger rows are append-only';
END;
$$;

DROP TRIGGER IF EXISTS prediction_ledger_requests_immutable
    ON public.prediction_ledger_requests;
CREATE TRIGGER prediction_ledger_requests_immutable
BEFORE UPDATE OR DELETE ON public.prediction_ledger_requests
FOR EACH ROW EXECUTE FUNCTION public.reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS prediction_ledger_predictions_immutable
    ON public.prediction_ledger_predictions;
CREATE TRIGGER prediction_ledger_predictions_immutable
BEFORE UPDATE OR DELETE ON public.prediction_ledger_predictions
FOR EACH ROW EXECUTE FUNCTION public.reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS prediction_ledger_features_immutable
    ON public.prediction_ledger_features;
CREATE TRIGGER prediction_ledger_features_immutable
BEFORE UPDATE OR DELETE ON public.prediction_ledger_features
FOR EACH ROW EXECUTE FUNCTION public.reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS prediction_ledger_settlements_immutable
    ON public.prediction_ledger_settlements;
CREATE TRIGGER prediction_ledger_settlements_immutable
BEFORE UPDATE OR DELETE ON public.prediction_ledger_settlements
FOR EACH ROW EXECUTE FUNCTION public.reject_prediction_ledger_mutation();

ALTER TABLE public.prediction_ledger_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prediction_ledger_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE public.prediction_ledger_predictions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prediction_ledger_predictions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.prediction_ledger_features ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prediction_ledger_features FORCE ROW LEVEL SECURITY;
ALTER TABLE public.prediction_ledger_settlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prediction_ledger_settlements FORCE ROW LEVEL SECURITY;

REVOKE ALL ON public.prediction_ledger_requests FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.prediction_ledger_predictions FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.prediction_ledger_features FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.prediction_ledger_settlements FROM PUBLIC, anon, authenticated;

REVOKE ALL ON public.prediction_ledger_requests FROM service_role;
REVOKE ALL ON public.prediction_ledger_predictions FROM service_role;
REVOKE ALL ON public.prediction_ledger_features FROM service_role;
REVOKE ALL ON public.prediction_ledger_settlements FROM service_role;

GRANT USAGE ON SCHEMA public TO strathmark_prediction_rpc_owner;
GRANT SELECT ON public.competitors TO strathmark_prediction_rpc_owner;
GRANT SELECT, INSERT ON public.prediction_ledger_requests TO strathmark_prediction_rpc_owner;
GRANT SELECT, INSERT ON public.prediction_ledger_predictions TO strathmark_prediction_rpc_owner;
GRANT SELECT, INSERT ON public.prediction_ledger_features TO strathmark_prediction_rpc_owner;
GRANT SELECT, INSERT ON public.prediction_ledger_settlements TO strathmark_prediction_rpc_owner;

DROP POLICY IF EXISTS prediction_ledger_requests_rpc ON public.prediction_ledger_requests;
CREATE POLICY prediction_ledger_requests_rpc ON public.prediction_ledger_requests
    FOR ALL TO strathmark_prediction_rpc_owner
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS prediction_ledger_predictions_rpc ON public.prediction_ledger_predictions;
CREATE POLICY prediction_ledger_predictions_rpc ON public.prediction_ledger_predictions
    FOR ALL TO strathmark_prediction_rpc_owner
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS prediction_ledger_features_rpc ON public.prediction_ledger_features;
CREATE POLICY prediction_ledger_features_rpc ON public.prediction_ledger_features
    FOR ALL TO strathmark_prediction_rpc_owner
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS prediction_ledger_settlements_rpc ON public.prediction_ledger_settlements;
CREATE POLICY prediction_ledger_settlements_rpc ON public.prediction_ledger_settlements
    FOR ALL TO strathmark_prediction_rpc_owner
    USING (true) WITH CHECK (true);

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
    incoming_settlement public.prediction_ledger_settlements%ROWTYPE;
    existing_settlement public.prediction_ledger_settlements%ROWTYPE;
    latest_settlement public.prediction_ledger_settlements%ROWTYPE;
    prediction_median pg_catalog.numeric;
    inserted_request_count pg_catalog.integer := 0;
    existing_request_id pg_catalog.text;
BEGIN
    IF ledger_payload IS NULL
       OR pg_catalog.jsonb_typeof(ledger_payload) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'ledger payload must be an object';
    END IF;
    IF ledger_payload ? 'settlement' THEN
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(ledger_payload) AS key
            WHERE key <> 'settlement'
        ) OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(ledger_payload)
        ) <> 1 THEN
            RAISE EXCEPTION 'ledger payload must contain exactly one operation kind';
        END IF;
        settlement_row := ledger_payload->'settlement';
        IF pg_catalog.jsonb_typeof(settlement_row) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'settlement payload must be a non-null object';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(settlement_row) AS key
            WHERE key NOT IN (
                'settlement_id', 'prediction_id', 'revision', 'competitor_id',
                'event_code', 'actual_time', 'residual', 'actor', 'reason',
                'payload_hash', 'supersedes_settlement_id', 'settled_at'
            )
        ) OR (
            SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(settlement_row)
        ) <> 12 THEN
            RAISE EXCEPTION 'ledger settlement has unknown or missing properties';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_each(settlement_row) AS member
            WHERE CASE
                WHEN member.key IN (
                    'settlement_id', 'prediction_id', 'competitor_id', 'event_code',
                    'actor', 'payload_hash', 'settled_at'
                ) THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'string'
                WHEN member.key IN ('reason', 'supersedes_settlement_id')
                    THEN pg_catalog.jsonb_typeof(member.value) NOT IN ('string', 'null')
                WHEN member.key IN ('actual_time', 'residual')
                    THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'number'
                WHEN member.key = 'revision' THEN CASE
                    WHEN pg_catalog.jsonb_typeof(member.value) = 'number'
                    THEN (member.value#>>'{}')::pg_catalog.numeric < 1
                      OR (member.value#>>'{}')::pg_catalog.numeric > 2147483647
                      OR pg_catalog.floor((member.value#>>'{}')::pg_catalog.numeric)
                         IS DISTINCT FROM (member.value#>>'{}')::pg_catalog.numeric
                    ELSE TRUE
                END
                ELSE TRUE
            END
        ) THEN
            RAISE EXCEPTION 'ledger settlement JSON types are invalid';
        END IF;
        IF NULLIF(settlement_row->>'settlement_id', '') IS NULL
           OR NULLIF(settlement_row->>'prediction_id', '') IS NULL
           OR NULLIF(settlement_row->>'competitor_id', '') IS NULL
           OR NULLIF(settlement_row->>'event_code', '') IS NULL
           OR NULLIF(pg_catalog.btrim(settlement_row->>'actor'), '') IS NULL THEN
            RAISE EXCEPTION 'settlement payload is missing required linkage fields';
        END IF;
        SELECT
            row.settlement_id, row.prediction_id, row.revision,
            row.competitor_id, row.event_code, row.actual_time, row.residual,
            row.actor, row.reason, row.payload_hash,
            row.supersedes_settlement_id, row.settled_at
        INTO incoming_settlement
        FROM pg_catalog.jsonb_to_record(settlement_row) AS row(
            settlement_id TEXT, prediction_id TEXT, revision INTEGER,
            competitor_id TEXT, event_code TEXT, actual_time NUMERIC,
            residual NUMERIC, actor TEXT, reason TEXT, payload_hash TEXT,
            supersedes_settlement_id TEXT, settled_at TIMESTAMPTZ
        );

        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(incoming_settlement.prediction_id, 0)
        );

        SELECT * INTO existing_settlement
        FROM public.prediction_ledger_settlements
        WHERE prediction_id = incoming_settlement.prediction_id
          AND payload_hash = incoming_settlement.payload_hash;
        IF FOUND THEN
            IF ROW(
                existing_settlement.settlement_id,
                existing_settlement.prediction_id,
                existing_settlement.revision,
                existing_settlement.competitor_id,
                existing_settlement.event_code,
                existing_settlement.actual_time,
                existing_settlement.residual,
                existing_settlement.actor,
                existing_settlement.reason,
                existing_settlement.payload_hash,
                existing_settlement.supersedes_settlement_id,
                existing_settlement.settled_at
            ) IS DISTINCT FROM ROW(
                incoming_settlement.settlement_id,
                incoming_settlement.prediction_id,
                incoming_settlement.revision,
                incoming_settlement.competitor_id,
                incoming_settlement.event_code,
                incoming_settlement.actual_time,
                incoming_settlement.residual,
                incoming_settlement.actor,
                incoming_settlement.reason,
                incoming_settlement.payload_hash,
                incoming_settlement.supersedes_settlement_id,
                incoming_settlement.settled_at
            ) THEN
                RAISE EXCEPTION 'ledger settlement payload conflict';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'accepted', TRUE, 'kind', 'settlement'
            );
        END IF;

        IF EXISTS (
            SELECT 1 FROM public.prediction_ledger_settlements
            WHERE settlement_id = incoming_settlement.settlement_id
        ) THEN
            RAISE EXCEPTION 'ledger settlement payload conflict';
        END IF;

        SELECT prediction.median_seconds
        INTO prediction_median
        FROM public.prediction_ledger_predictions AS prediction
        WHERE prediction.prediction_id = incoming_settlement.prediction_id
          AND prediction.competitor_id = incoming_settlement.competitor_id
          AND prediction.event_code = incoming_settlement.event_code;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'settlement prediction linkage mismatch';
        END IF;

        -- The authoritative ledger computes with Python binary floats. Permit
        -- only serialization noise, never caller-chosen residual evidence.
        IF incoming_settlement.residual IS NULL
           OR pg_catalog.abs(
               incoming_settlement.residual
               - (incoming_settlement.actual_time - prediction_median)
           ) > 0.000000001 THEN
            RAISE EXCEPTION 'ledger settlement residual conflict';
        END IF;

        SELECT * INTO latest_settlement
        FROM public.prediction_ledger_settlements
        WHERE prediction_id = incoming_settlement.prediction_id
        ORDER BY revision DESC
        LIMIT 1;
        IF NOT FOUND THEN
            IF incoming_settlement.revision IS DISTINCT FROM 1
               OR incoming_settlement.supersedes_settlement_id IS NOT NULL THEN
                RAISE EXCEPTION 'ledger settlement revision conflict';
            END IF;
        ELSE
            IF incoming_settlement.revision IS DISTINCT FROM latest_settlement.revision + 1
               OR incoming_settlement.supersedes_settlement_id
                  IS DISTINCT FROM latest_settlement.settlement_id THEN
                RAISE EXCEPTION 'ledger settlement revision conflict';
            END IF;
            IF NULLIF(pg_catalog.btrim(incoming_settlement.reason), '') IS NULL THEN
                RAISE EXCEPTION 'ledger settlement correction reason conflict';
            END IF;
        END IF;

        INSERT INTO public.prediction_ledger_settlements (
            settlement_id, prediction_id, revision, competitor_id, event_code,
            actual_time, residual, actor, reason, payload_hash,
            supersedes_settlement_id, settled_at
        )
        VALUES (
            incoming_settlement.settlement_id,
            incoming_settlement.prediction_id,
            incoming_settlement.revision,
            incoming_settlement.competitor_id,
            incoming_settlement.event_code,
            incoming_settlement.actual_time,
            incoming_settlement.residual,
            incoming_settlement.actor,
            incoming_settlement.reason,
            incoming_settlement.payload_hash,
            incoming_settlement.supersedes_settlement_id,
            incoming_settlement.settled_at
        );
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
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(ledger_payload) AS key
        WHERE key NOT IN ('request', 'predictions', 'features')
    ) OR (
        SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(ledger_payload)
    ) <> 3 THEN
        RAISE EXCEPTION 'ledger field payload has unknown or missing properties';
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
        SELECT 1 FROM pg_catalog.jsonb_object_keys(request_row) AS key
        WHERE key NOT IN (
            'ledger_request_id', 'caller_id', 'request_id', 'request_hash',
            'hash_algorithm', 'event_code', 'prediction_as_of', 'created_at'
        )
    ) OR (
        SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(request_row)
    ) NOT IN (7, 8)
       OR NOT request_row ? 'ledger_request_id'
       OR NOT request_row ? 'caller_id'
       OR NOT request_row ? 'request_id'
       OR NOT request_row ? 'request_hash'
       OR NOT request_row ? 'event_code'
       OR NOT request_row ? 'prediction_as_of'
       OR NOT request_row ? 'created_at' THEN
        RAISE EXCEPTION 'ledger request has unknown or missing properties';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_each(request_row) AS member
        WHERE pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'string'
    ) THEN
        RAISE EXCEPTION 'ledger request JSON types are invalid';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(ledger_payload->'predictions') AS item
        WHERE pg_catalog.jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(item) AS key
               WHERE key NOT IN (
                   'prediction_id', 'ledger_request_id', 'competitor_id',
                   'event_code', 'median_seconds', 'assigned_mark', 'source',
                   'training_eligible', 'engine_version', 'model_version',
                   'calibration_version', 'evidence_cutoff', 'interval_lower',
                   'interval_upper', 'interval_coverage', 'interval_state',
                   'interval_scope', 'ignored_factors', 'warnings', 'optimizer',
                   'optimizer_metadata', 'created_at'
               )
           )
           OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(item)) <> 22
    ) THEN
        RAISE EXCEPTION 'ledger prediction has unknown or missing properties';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(ledger_payload->'predictions') AS item
        CROSS JOIN LATERAL pg_catalog.jsonb_each(item) AS member
        WHERE CASE
            WHEN member.key IN (
                'prediction_id', 'ledger_request_id', 'competitor_id',
                'event_code', 'source', 'created_at'
            ) THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'string'
            WHEN member.key = 'median_seconds'
                THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'number'
            WHEN member.key = 'assigned_mark' THEN CASE
                WHEN pg_catalog.jsonb_typeof(member.value) = 'number'
                THEN (member.value#>>'{}')::pg_catalog.numeric < 3
                  OR (member.value#>>'{}')::pg_catalog.numeric > 2147483647
                  OR pg_catalog.floor((member.value#>>'{}')::pg_catalog.numeric)
                     IS DISTINCT FROM (member.value#>>'{}')::pg_catalog.numeric
                ELSE TRUE
            END
            WHEN member.key = 'training_eligible'
                THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'boolean'
            WHEN member.key IN (
                'engine_version', 'model_version', 'calibration_version',
                'evidence_cutoff', 'interval_state', 'interval_scope', 'optimizer'
            ) THEN pg_catalog.jsonb_typeof(member.value) NOT IN ('string', 'null')
            WHEN member.key IN ('interval_lower', 'interval_upper', 'interval_coverage')
                THEN pg_catalog.jsonb_typeof(member.value) NOT IN ('number', 'null')
            WHEN member.key IN ('ignored_factors', 'warnings')
                THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'array'
            WHEN member.key = 'optimizer_metadata'
                THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'object'
            ELSE TRUE
        END
    ) THEN
        RAISE EXCEPTION 'ledger prediction JSON types are invalid';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(ledger_payload->'features') AS item
        WHERE pg_catalog.jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(item) AS key
               WHERE key NOT IN (
                   'feature_snapshot_id', 'prediction_id', 'feature_name',
                   'numeric_value', 'created_at'
               )
           )
           OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(item)) <> 5
    ) THEN
        RAISE EXCEPTION 'ledger feature has unknown or missing properties';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(ledger_payload->'features') AS item
        CROSS JOIN LATERAL pg_catalog.jsonb_each(item) AS member
        WHERE CASE
            WHEN member.key IN (
                'feature_snapshot_id', 'prediction_id', 'feature_name', 'created_at'
            ) THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'string'
            WHEN member.key = 'numeric_value'
                THEN pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'number'
            ELSE TRUE
        END
    ) THEN
        RAISE EXCEPTION 'ledger feature JSON types are invalid';
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
    GET DIAGNOSTICS inserted_request_count = ROW_COUNT;

    SELECT ledger_request_id
    INTO existing_request_id
    FROM public.prediction_ledger_requests
    WHERE caller_id = request_row->>'caller_id'
      AND request_id = request_row->>'request_id';
    IF NOT FOUND OR EXISTS (
        SELECT 1
        FROM public.prediction_ledger_requests AS existing
        WHERE existing.caller_id = request_row->>'caller_id'
          AND existing.request_id = request_row->>'request_id'
          AND ROW(
              existing.ledger_request_id,
              existing.caller_id,
              existing.request_id,
              existing.request_hash,
              existing.event_code,
              existing.prediction_as_of,
              existing.created_at
          ) IS DISTINCT FROM ROW(
              request_row->>'ledger_request_id',
              request_row->>'caller_id',
              request_row->>'request_id',
              request_row->>'request_hash',
              request_row->>'event_code',
              (request_row->>'prediction_as_of')::DATE,
              (request_row->>'created_at')::TIMESTAMPTZ
          )
    ) THEN
        IF EXISTS (
            SELECT 1
            FROM public.prediction_ledger_requests AS existing
            WHERE existing.caller_id = request_row->>'caller_id'
              AND existing.request_id = request_row->>'request_id'
              AND existing.request_hash IS DISTINCT FROM request_row->>'request_hash'
        ) THEN
            RAISE EXCEPTION 'ledger request hash conflict';
        END IF;
        RAISE EXCEPTION 'ledger request projection conflict';
    END IF;

    IF inserted_request_count = 0 THEN
        IF pg_catalog.jsonb_array_length(ledger_payload->'predictions') <>
           (
               SELECT pg_catalog.count(*)
               FROM public.prediction_ledger_predictions
               WHERE ledger_request_id = existing_request_id
           )
           OR EXISTS (
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
               EXCEPT
               SELECT
                   prediction_id, ledger_request_id, competitor_id, event_code,
                   median_seconds, assigned_mark, source, training_eligible,
                   engine_version, model_version, calibration_version, evidence_cutoff,
                   interval_lower, interval_upper, interval_coverage, interval_state,
                   interval_scope, ignored_factors, warnings, optimizer,
                   optimizer_metadata, created_at
               FROM public.prediction_ledger_predictions
               WHERE ledger_request_id = existing_request_id
           )
           OR EXISTS (
               SELECT
                   prediction_id, ledger_request_id, competitor_id, event_code,
                   median_seconds, assigned_mark, source, training_eligible,
                   engine_version, model_version, calibration_version, evidence_cutoff,
                   interval_lower, interval_upper, interval_coverage, interval_state,
                   interval_scope, ignored_factors, warnings, optimizer,
                   optimizer_metadata, created_at
               FROM public.prediction_ledger_predictions
               WHERE ledger_request_id = existing_request_id
               EXCEPT
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
           ) THEN
            RAISE EXCEPTION 'ledger prediction projection conflict';
        END IF;

        IF pg_catalog.jsonb_array_length(ledger_payload->'features') <>
           (
               SELECT pg_catalog.count(*)
               FROM public.prediction_ledger_features AS feature
               JOIN public.prediction_ledger_predictions AS prediction
                 ON prediction.prediction_id = feature.prediction_id
               WHERE prediction.ledger_request_id = existing_request_id
           )
           OR EXISTS (
               SELECT
                   row.feature_snapshot_id, row.prediction_id, row.feature_name,
                   row.numeric_value, row.created_at
               FROM pg_catalog.jsonb_to_recordset(ledger_payload->'features') AS row(
                   feature_snapshot_id TEXT, prediction_id TEXT, feature_name TEXT,
                   numeric_value DOUBLE PRECISION, created_at TIMESTAMPTZ
               )
               EXCEPT
               SELECT
                   feature.feature_snapshot_id, feature.prediction_id,
                   feature.feature_name, feature.numeric_value, feature.created_at
               FROM public.prediction_ledger_features AS feature
               JOIN public.prediction_ledger_predictions AS prediction
                 ON prediction.prediction_id = feature.prediction_id
               WHERE prediction.ledger_request_id = existing_request_id
           )
           OR EXISTS (
               SELECT
                   feature.feature_snapshot_id, feature.prediction_id,
                   feature.feature_name, feature.numeric_value, feature.created_at
               FROM public.prediction_ledger_features AS feature
               JOIN public.prediction_ledger_predictions AS prediction
                 ON prediction.prediction_id = feature.prediction_id
               WHERE prediction.ledger_request_id = existing_request_id
               EXCEPT
               SELECT
                   row.feature_snapshot_id, row.prediction_id, row.feature_name,
                   row.numeric_value, row.created_at
               FROM pg_catalog.jsonb_to_recordset(ledger_payload->'features') AS row(
                   feature_snapshot_id TEXT, prediction_id TEXT, feature_name TEXT,
                   numeric_value DOUBLE PRECISION, created_at TIMESTAMPTZ
               )
           ) THEN
            RAISE EXCEPTION 'ledger feature projection conflict';
        END IF;

        RETURN pg_catalog.jsonb_build_object(
            'accepted', TRUE, 'kind', 'field', 'duplicate', TRUE
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_to_recordset(ledger_payload->'predictions') AS row(
            prediction_id TEXT
        )
        JOIN public.prediction_ledger_predictions AS existing
          ON existing.prediction_id = row.prediction_id
    ) THEN
        RAISE EXCEPTION 'ledger prediction projection conflict';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_to_recordset(ledger_payload->'features') AS row(
            feature_snapshot_id TEXT
        )
        JOIN public.prediction_ledger_features AS existing
          ON existing.feature_snapshot_id = row.feature_snapshot_id
    ) THEN
        RAISE EXCEPTION 'ledger feature projection conflict';
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
    ;

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
    ;

    RETURN pg_catalog.jsonb_build_object('accepted', TRUE, 'kind', 'field');
END;
$$;

ALTER FUNCTION public.append_prediction_ledger_v2(pg_catalog.jsonb)
    OWNER TO strathmark_prediction_rpc_owner;
REVOKE ALL ON FUNCTION public.append_prediction_ledger_v2(pg_catalog.jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.append_prediction_ledger_v2(pg_catalog.jsonb)
    TO service_role;

COMMIT;

-- Rollback (destructive to mirrored ledger data; local SQLite remains intact):
-- BEGIN;
-- DROP FUNCTION IF EXISTS append_prediction_ledger_v2(JSONB);
-- DROP TABLE IF EXISTS public.prediction_ledger_settlements;
-- DROP TABLE IF EXISTS public.prediction_ledger_features;
-- DROP TABLE IF EXISTS public.prediction_ledger_predictions;
-- DROP TABLE IF EXISTS public.prediction_ledger_requests;
-- DROP FUNCTION IF EXISTS public.reject_prediction_ledger_mutation();
-- COMMIT;
