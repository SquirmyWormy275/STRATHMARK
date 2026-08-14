-- Migration: Additive trusted shadow evidence mirror
-- Date:      2026-08-13
-- Reversible: only before any shadow evidence has been mirrored
-- Idempotent: yes for schema objects, policies, triggers, function, and grants
--
-- Local SQLite remains the single-writer authority.  This schema stores only
-- immutable receipt cores, observation fingerprints, eligible numeric settle/
-- void revisions, and delivery metadata.  Missoula operational outcomes,
-- prospective context history, names, notes, and secrets do not belong here.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'strathmark_prediction_rpc_owner'
          AND NOT rolcanlogin
          AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION
            'strathmark_prediction_rpc_owner must exist as NOLOGIN NOBYPASSRLS';
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS public.shadow_mirror_deliveries (
    outbox_id pg_catalog.text PRIMARY KEY,
    schema_version pg_catalog.text NOT NULL
        CHECK (schema_version = 'strathmark.mirror-delivery.v1'),
    entity_kind pg_catalog.text NOT NULL
        CHECK (entity_kind IN ('shadow_receipt', 'numeric_outcome_revision')),
    entity_id pg_catalog.text NOT NULL,
    payload_hash pg_catalog.text NOT NULL
        CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    producer_created_at pg_catalog.timestamptz NOT NULL,
    recorded_at pg_catalog.timestamptz NOT NULL DEFAULT pg_catalog.now(),
    UNIQUE (entity_kind, entity_id)
);

CREATE TABLE IF NOT EXISTS public.shadow_receipt_cores (
    ledger_request_id pg_catalog.text PRIMARY KEY
        REFERENCES public.prediction_ledger_requests(ledger_request_id),
    delivery_outbox_id pg_catalog.text NOT NULL UNIQUE
        REFERENCES public.shadow_mirror_deliveries(outbox_id),
    caller_id pg_catalog.text NOT NULL,
    request_id pg_catalog.text NOT NULL,
    schema_version pg_catalog.text NOT NULL
        CHECK (schema_version = 'strathmark.shadow-receipt-mirror.v1'),
    core_schema_version pg_catalog.text NOT NULL
        CHECK (core_schema_version = 'strathmark.shadow-receipt-core.v1'),
    identity_schema_version pg_catalog.text NOT NULL
        CHECK (identity_schema_version = 'strathmark.namespaced-identity.v1'),
    observation_schema_version pg_catalog.text NOT NULL
        CHECK (observation_schema_version =
               'strathmark.shadow-observation-fingerprint.v1'),
    observation_fingerprint pg_catalog.text NOT NULL
        CHECK (observation_fingerprint ~ '^[0-9a-f]{64}$'),
    core pg_catalog.jsonb NOT NULL
        CHECK (pg_catalog.jsonb_typeof(core) = 'object'),
    created_at pg_catalog.timestamptz NOT NULL,
    UNIQUE (caller_id, request_id)
);

CREATE TABLE IF NOT EXISTS public.shadow_numeric_outcome_revisions (
    field_revision_id pg_catalog.text PRIMARY KEY,
    outcome_revision_id pg_catalog.text NOT NULL UNIQUE,
    ledger_request_id pg_catalog.text NOT NULL
        REFERENCES public.shadow_receipt_cores(ledger_request_id),
    delivery_outbox_id pg_catalog.text NOT NULL UNIQUE
        REFERENCES public.shadow_mirror_deliveries(outbox_id),
    caller_id pg_catalog.text NOT NULL,
    schema_version pg_catalog.text NOT NULL
        CHECK (schema_version = 'strathmark.shadow-numeric-outcome-mirror.v1'),
    actor pg_catalog.text NOT NULL,
    reason_code pg_catalog.text
        CHECK (reason_code IN (
            'corrected_time',
            'retract_invalid_numeric_evidence',
            'valid_replacement'
        )),
    created_at pg_catalog.timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS public.shadow_numeric_settlement_revisions (
    revision_id pg_catalog.text PRIMARY KEY,
    field_revision_id pg_catalog.text NOT NULL
        REFERENCES public.shadow_numeric_outcome_revisions(field_revision_id),
    prediction_id pg_catalog.text NOT NULL
        REFERENCES public.prediction_ledger_predictions(prediction_id),
    revision pg_catalog.integer NOT NULL CHECK (revision >= 1),
    competitor_id pg_catalog.text NOT NULL
        REFERENCES public.competitors(competitor_id),
    event_code pg_catalog.text NOT NULL CHECK (event_code IN ('SB', 'UH')),
    action pg_catalog.text NOT NULL CHECK (action IN ('settle', 'void')),
    actual_time pg_catalog.numeric,
    residual pg_catalog.numeric,
    supersedes_revision_id pg_catalog.text,
    created_at pg_catalog.timestamptz NOT NULL,
    CHECK (
        (action = 'settle' AND actual_time > 0 AND actual_time <= 300
            AND residual IS NOT NULL)
        OR (action = 'void' AND actual_time IS NULL AND residual IS NULL)
    ),
    UNIQUE (prediction_id, revision),
    UNIQUE (field_revision_id, prediction_id)
);

CREATE INDEX IF NOT EXISTS idx_shadow_receipt_caller_request
    ON public.shadow_receipt_cores(caller_id, request_id);
CREATE INDEX IF NOT EXISTS idx_shadow_numeric_outcome_request
    ON public.shadow_numeric_outcome_revisions(ledger_request_id, created_at);
CREATE INDEX IF NOT EXISTS idx_shadow_numeric_settlement_current
    ON public.shadow_numeric_settlement_revisions(prediction_id, revision DESC);

DROP TRIGGER IF EXISTS shadow_mirror_deliveries_immutable
    ON public.shadow_mirror_deliveries;
CREATE TRIGGER shadow_mirror_deliveries_immutable
BEFORE UPDATE OR DELETE ON public.shadow_mirror_deliveries
FOR EACH ROW EXECUTE FUNCTION public.reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS shadow_receipt_cores_immutable
    ON public.shadow_receipt_cores;
CREATE TRIGGER shadow_receipt_cores_immutable
BEFORE UPDATE OR DELETE ON public.shadow_receipt_cores
FOR EACH ROW EXECUTE FUNCTION public.reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS shadow_numeric_outcome_revisions_immutable
    ON public.shadow_numeric_outcome_revisions;
CREATE TRIGGER shadow_numeric_outcome_revisions_immutable
BEFORE UPDATE OR DELETE ON public.shadow_numeric_outcome_revisions
FOR EACH ROW EXECUTE FUNCTION public.reject_prediction_ledger_mutation();

DROP TRIGGER IF EXISTS shadow_numeric_settlement_revisions_immutable
    ON public.shadow_numeric_settlement_revisions;
CREATE TRIGGER shadow_numeric_settlement_revisions_immutable
BEFORE UPDATE OR DELETE ON public.shadow_numeric_settlement_revisions
FOR EACH ROW EXECUTE FUNCTION public.reject_prediction_ledger_mutation();

ALTER TABLE public.shadow_mirror_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shadow_mirror_deliveries FORCE ROW LEVEL SECURITY;
ALTER TABLE public.shadow_receipt_cores ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shadow_receipt_cores FORCE ROW LEVEL SECURITY;
ALTER TABLE public.shadow_numeric_outcome_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shadow_numeric_outcome_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.shadow_numeric_settlement_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shadow_numeric_settlement_revisions FORCE ROW LEVEL SECURITY;

REVOKE ALL ON public.shadow_mirror_deliveries FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.shadow_receipt_cores FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.shadow_numeric_outcome_revisions FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.shadow_numeric_settlement_revisions FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.shadow_mirror_deliveries FROM service_role;
REVOKE ALL ON public.shadow_receipt_cores FROM service_role;
REVOKE ALL ON public.shadow_numeric_outcome_revisions FROM service_role;
REVOKE ALL ON public.shadow_numeric_settlement_revisions FROM service_role;

GRANT SELECT, INSERT ON public.shadow_mirror_deliveries
    TO strathmark_prediction_rpc_owner;
GRANT SELECT, INSERT ON public.shadow_receipt_cores
    TO strathmark_prediction_rpc_owner;
GRANT SELECT, INSERT ON public.shadow_numeric_outcome_revisions
    TO strathmark_prediction_rpc_owner;
GRANT SELECT, INSERT ON public.shadow_numeric_settlement_revisions
    TO strathmark_prediction_rpc_owner;

DROP POLICY IF EXISTS shadow_mirror_deliveries_rpc
    ON public.shadow_mirror_deliveries;
CREATE POLICY shadow_mirror_deliveries_rpc ON public.shadow_mirror_deliveries
    FOR ALL TO strathmark_prediction_rpc_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS shadow_receipt_cores_rpc ON public.shadow_receipt_cores;
CREATE POLICY shadow_receipt_cores_rpc ON public.shadow_receipt_cores
    FOR ALL TO strathmark_prediction_rpc_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS shadow_numeric_outcome_revisions_rpc
    ON public.shadow_numeric_outcome_revisions;
CREATE POLICY shadow_numeric_outcome_revisions_rpc
    ON public.shadow_numeric_outcome_revisions
    FOR ALL TO strathmark_prediction_rpc_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS shadow_numeric_settlement_revisions_rpc
    ON public.shadow_numeric_settlement_revisions;
CREATE POLICY shadow_numeric_settlement_revisions_rpc
    ON public.shadow_numeric_settlement_revisions
    FOR ALL TO strathmark_prediction_rpc_owner USING (true) WITH CHECK (true);

CREATE OR REPLACE FUNCTION public.append_shadow_mirror_v1(
    mirror_payload pg_catalog.jsonb
)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    delivery_row pg_catalog.jsonb;
    ledger_row pg_catalog.jsonb;
    receipt_row pg_catalog.jsonb;
    outcome_row pg_catalog.jsonb;
    revision_row pg_catalog.jsonb;
    existing_delivery public.shadow_mirror_deliveries%ROWTYPE;
    existing_receipt public.shadow_receipt_cores%ROWTYPE;
    existing_outcome public.shadow_numeric_outcome_revisions%ROWTYPE;
    mirror_kind pg_catalog.text;
    caller_namespace pg_catalog.text;
    delivery_exists pg_catalog.boolean := FALSE;
    prior_revision pg_catalog.integer;
    prior_revision_id pg_catalog.text;
    prior_action pg_catalog.text;
    prediction_median pg_catalog.numeric;
BEGIN
    IF mirror_payload IS NULL
       OR pg_catalog.jsonb_typeof(mirror_payload) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'shadow mirror payload must be an object';
    END IF;
    IF pg_catalog.pg_column_size(mirror_payload) > 1048576 THEN
        RAISE EXCEPTION 'shadow mirror payload exceeds the 1 MiB limit';
    END IF;
    IF mirror_payload->>'schema_version'
       IS DISTINCT FROM 'strathmark.shadow-mirror-envelope.v1' THEN
        RAISE EXCEPTION 'unsupported shadow mirror envelope schema';
    END IF;
    mirror_kind := mirror_payload->>'kind';
    IF mirror_kind NOT IN ('shadow_receipt', 'numeric_outcome_revision') THEN
        RAISE EXCEPTION 'unsupported shadow mirror kind';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(mirror_payload) AS key
        WHERE key NOT IN (
            'schema_version', 'kind', 'delivery', 'ledger', 'receipt',
            'numeric_outcome_revision'
        )
    ) OR (
        SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(mirror_payload)
    ) <> CASE WHEN mirror_kind = 'shadow_receipt' THEN 5 ELSE 4 END THEN
        RAISE EXCEPTION 'shadow mirror envelope has unknown or missing properties';
    END IF;
    IF EXISTS (
        WITH RECURSIVE shadow_nodes(node_key, node_value) AS (
            SELECT NULL::pg_catalog.text, mirror_payload
            UNION ALL
            SELECT child.node_key, child.node_value
            FROM shadow_nodes AS parent
            CROSS JOIN LATERAL (
                SELECT member.key AS node_key, member.value AS node_value
                FROM pg_catalog.jsonb_each(
                    CASE
                        WHEN pg_catalog.jsonb_typeof(parent.node_value) = 'object'
                        THEN parent.node_value
                        ELSE '{}'::pg_catalog.jsonb
                    END
                ) AS member
                UNION ALL
                SELECT NULL::pg_catalog.text, element.value
                FROM pg_catalog.jsonb_array_elements(
                    CASE
                        WHEN pg_catalog.jsonb_typeof(parent.node_value) = 'array'
                        THEN parent.node_value
                        ELSE '[]'::pg_catalog.jsonb
                    END
                ) AS element
            ) AS child
        )
        SELECT 1
        FROM shadow_nodes
        WHERE pg_catalog.lower(node_key) IN (
            'name', 'display_name', 'fatigue', 'fatigue_notes', 'medical',
            'medical_notes', 'weather', 'equipment', 'outcome_history',
            'context_history', 'penalty', 'dnf', 'dq', 'notes', 'secret'
        )
    ) THEN
        RAISE EXCEPTION 'shadow mirror contains prohibited operational or free-text data';
    END IF;

    delivery_row := mirror_payload->'delivery';
    IF pg_catalog.jsonb_typeof(delivery_row) IS DISTINCT FROM 'object'
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(delivery_row) AS key
           WHERE key NOT IN (
               'schema_version', 'outbox_id', 'entity_id', 'created_at', 'payload_hash'
           )
       ) OR (
           SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(delivery_row)
       ) <> 5 THEN
        RAISE EXCEPTION 'shadow mirror delivery has unknown or missing properties';
    END IF;
    IF delivery_row->>'schema_version'
       IS DISTINCT FROM 'strathmark.mirror-delivery.v1'
       OR NULLIF(delivery_row->>'outbox_id', '') IS NULL
       OR pg_catalog.length(delivery_row->>'outbox_id') > 128
       OR NULLIF(delivery_row->>'entity_id', '') IS NULL
       OR pg_catalog.length(delivery_row->>'entity_id') > 128
       OR delivery_row->>'payload_hash' !~ '^[0-9a-f]{64}$'
       OR NULLIF(delivery_row->>'created_at', '') IS NULL THEN
        RAISE EXCEPTION 'shadow mirror delivery metadata is invalid';
    END IF;

    SELECT * INTO existing_delivery
    FROM public.shadow_mirror_deliveries
    WHERE outbox_id = delivery_row->>'outbox_id';
    IF FOUND THEN
        delivery_exists := TRUE;
        IF existing_delivery.schema_version IS DISTINCT FROM delivery_row->>'schema_version'
           OR existing_delivery.entity_kind IS DISTINCT FROM mirror_kind
           OR existing_delivery.entity_id IS DISTINCT FROM delivery_row->>'entity_id'
           OR existing_delivery.payload_hash IS DISTINCT FROM delivery_row->>'payload_hash'
           OR existing_delivery.producer_created_at IS DISTINCT FROM
              (delivery_row->>'created_at')::pg_catalog.timestamptz THEN
            RAISE EXCEPTION 'shadow mirror outbox conflict';
        END IF;
    END IF;

    IF mirror_kind = 'shadow_receipt' THEN
        ledger_row := mirror_payload->'ledger';
        receipt_row := mirror_payload->'receipt';
        IF pg_catalog.jsonb_typeof(ledger_row) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(receipt_row) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'shadow receipt ledger and receipt must be objects';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(ledger_row) AS key
            WHERE key NOT IN ('request', 'predictions', 'features')
        ) OR (
            SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(ledger_row)
        ) <> 3
           OR pg_catalog.jsonb_typeof(ledger_row->'request') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(ledger_row->'predictions') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(ledger_row->'predictions') = 0
           OR pg_catalog.jsonb_array_length(ledger_row->'predictions') > 512
           OR pg_catalog.jsonb_typeof(ledger_row->'features') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(ledger_row->'features') > 16384 THEN
            RAISE EXCEPTION 'shadow receipt ledger projection shape or cardinality is invalid';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(ledger_row->'request') AS key
            WHERE key NOT IN (
                'ledger_request_id', 'caller_id', 'request_id', 'request_hash',
                'hash_algorithm', 'event_code', 'prediction_as_of', 'created_at'
            )
        ) OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(ledger_row->'request')
        ) <> 8
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_each(ledger_row->'request') AS member
               WHERE pg_catalog.jsonb_typeof(member.value) IS DISTINCT FROM 'string'
           )
           OR ledger_row#>>'{request,hash_algorithm}' NOT IN ('raw-v1', 'active-v2')
           OR ledger_row#>>'{request,event_code}' NOT IN ('SB', 'UH') THEN
            RAISE EXCEPTION 'shadow receipt ledger request contract is invalid';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(ledger_row->'predictions') AS item
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
               OR pg_catalog.jsonb_typeof(item->'prediction_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(item->'ledger_request_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(item->'competitor_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(item->'event_code') IS DISTINCT FROM 'string'
               OR item->>'event_code' NOT IN ('SB', 'UH')
               OR pg_catalog.jsonb_typeof(item->'median_seconds') IS DISTINCT FROM 'number'
               OR (item->>'median_seconds')::pg_catalog.numeric <= 0
               OR CASE
                   WHEN pg_catalog.jsonb_typeof(item->'assigned_mark') = 'number'
                   THEN (item->>'assigned_mark')::pg_catalog.numeric < 3
                     OR (item->>'assigned_mark')::pg_catalog.numeric > 2147483647
                     OR pg_catalog.floor((item->>'assigned_mark')::pg_catalog.numeric)
                        IS DISTINCT FROM (item->>'assigned_mark')::pg_catalog.numeric
                   ELSE TRUE
               END
               OR pg_catalog.jsonb_typeof(item->'source') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(item->'training_eligible') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(item->'engine_version') NOT IN ('string', 'null')
               OR pg_catalog.jsonb_typeof(item->'model_version') NOT IN ('string', 'null')
               OR pg_catalog.jsonb_typeof(item->'calibration_version') NOT IN ('string', 'null')
               OR pg_catalog.jsonb_typeof(item->'evidence_cutoff') NOT IN ('string', 'null')
               OR pg_catalog.jsonb_typeof(item->'interval_lower') NOT IN ('number', 'null')
               OR pg_catalog.jsonb_typeof(item->'interval_upper') NOT IN ('number', 'null')
               OR pg_catalog.jsonb_typeof(item->'interval_coverage') NOT IN ('number', 'null')
               OR pg_catalog.jsonb_typeof(item->'interval_state') NOT IN ('string', 'null')
               OR pg_catalog.jsonb_typeof(item->'interval_scope') NOT IN ('string', 'null')
               OR pg_catalog.jsonb_typeof(item->'ignored_factors') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_array_length(item->'ignored_factors') > 128
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_array_elements(item->'ignored_factors') AS value
                   WHERE pg_catalog.jsonb_typeof(value) IS DISTINCT FROM 'string'
               )
               OR pg_catalog.jsonb_typeof(item->'warnings') IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_array_length(item->'warnings') > 128
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_array_elements(item->'warnings') AS value
                   WHERE pg_catalog.jsonb_typeof(value) IS DISTINCT FROM 'string'
               )
               OR pg_catalog.jsonb_typeof(item->'optimizer') NOT IN ('string', 'null')
               OR pg_catalog.jsonb_typeof(item->'optimizer_metadata') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(item->'created_at') IS DISTINCT FROM 'string'
        ) THEN
            RAISE EXCEPTION 'shadow receipt ledger prediction contract is invalid';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(ledger_row->'features') AS item
            WHERE pg_catalog.jsonb_typeof(item) IS DISTINCT FROM 'object'
               OR EXISTS (
                   SELECT 1 FROM pg_catalog.jsonb_object_keys(item) AS key
                   WHERE key NOT IN (
                       'feature_snapshot_id', 'prediction_id', 'feature_name',
                       'numeric_value', 'created_at'
                   )
               )
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(item)) <> 5
               OR pg_catalog.jsonb_typeof(item->'feature_snapshot_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(item->'prediction_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(item->'feature_name') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(item->'numeric_value') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(item->'created_at') IS DISTINCT FROM 'string'
        ) THEN
            RAISE EXCEPTION 'shadow receipt ledger feature contract is invalid';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(receipt_row) AS key
            WHERE key NOT IN (
                'schema_version', 'ledger_request_id', 'caller_id', 'request_id',
                'core_schema_version', 'identity_schema_version',
                'observation_schema_version', 'observation_fingerprint', 'core'
            )
        ) OR (
            SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(receipt_row)
        ) <> 9 THEN
            RAISE EXCEPTION 'shadow receipt has unknown or missing properties';
        END IF;
        IF receipt_row->>'schema_version'
           IS DISTINCT FROM 'strathmark.shadow-receipt-mirror.v1'
           OR receipt_row->>'core_schema_version'
              IS DISTINCT FROM 'strathmark.shadow-receipt-core.v1'
           OR receipt_row->>'identity_schema_version'
              IS DISTINCT FROM 'strathmark.namespaced-identity.v1'
           OR receipt_row->>'observation_schema_version'
              IS DISTINCT FROM 'strathmark.shadow-observation-fingerprint.v1'
           OR receipt_row->>'observation_fingerprint' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(receipt_row->'core') IS DISTINCT FROM 'object'
           OR pg_catalog.pg_column_size(receipt_row->'core') > 786432 THEN
            RAISE EXCEPTION 'shadow receipt schema or size is invalid';
        END IF;
        IF delivery_row->>'entity_id' IS DISTINCT FROM receipt_row->>'ledger_request_id'
           OR ledger_row#>>'{request,ledger_request_id}'
              IS DISTINCT FROM receipt_row->>'ledger_request_id'
           OR ledger_row#>>'{request,caller_id}' IS DISTINCT FROM receipt_row->>'caller_id'
           OR ledger_row#>>'{request,request_id}' IS DISTINCT FROM receipt_row->>'request_id'
           OR receipt_row#>>'{core,schema_version}'
              IS DISTINCT FROM receipt_row->>'core_schema_version'
           OR receipt_row#>>'{core,identity_schema_version}'
              IS DISTINCT FROM receipt_row->>'identity_schema_version'
           OR receipt_row#>>'{core,consumer_id}' IS DISTINCT FROM receipt_row->>'caller_id'
           OR receipt_row#>>'{core,request_id}' IS DISTINCT FROM receipt_row->>'request_id'
           OR receipt_row#>>'{core,observation,schema_version}'
              IS DISTINCT FROM receipt_row->>'observation_schema_version'
           OR receipt_row#>>'{core,observation,fingerprint}'
              IS DISTINCT FROM receipt_row->>'observation_fingerprint' THEN
            RAISE EXCEPTION 'shadow receipt linkage mismatch';
        END IF;
        IF receipt_row->>'caller_id'
              !~ '^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$'
           OR receipt_row->>'request_id'
              !~ '^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$'
           OR pg_catalog.split_part(receipt_row->>'caller_id', ':', 1)
              IS DISTINCT FROM pg_catalog.split_part(receipt_row->>'request_id', ':', 1) THEN
            RAISE EXCEPTION 'shadow receipt identity namespace is invalid';
        END IF;
        IF pg_catalog.jsonb_typeof(receipt_row#>'{core,predictions}')
              IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(receipt_row#>'{core,predictions}') = 0
           OR pg_catalog.jsonb_array_length(receipt_row#>'{core,predictions}') > 512
           OR pg_catalog.jsonb_array_length(receipt_row#>'{core,predictions}')
              IS DISTINCT FROM pg_catalog.jsonb_array_length(ledger_row->'predictions') THEN
            RAISE EXCEPTION 'shadow receipt predictions are incomplete';
        END IF;

        IF delivery_exists THEN
            SELECT * INTO existing_receipt
            FROM public.shadow_receipt_cores
            WHERE delivery_outbox_id = delivery_row->>'outbox_id';
            IF NOT FOUND
               OR existing_receipt.ledger_request_id
                  IS DISTINCT FROM receipt_row->>'ledger_request_id'
               OR existing_receipt.caller_id IS DISTINCT FROM receipt_row->>'caller_id'
               OR existing_receipt.request_id IS DISTINCT FROM receipt_row->>'request_id'
               OR existing_receipt.schema_version
                  IS DISTINCT FROM receipt_row->>'schema_version'
               OR existing_receipt.core_schema_version
                  IS DISTINCT FROM receipt_row->>'core_schema_version'
               OR existing_receipt.identity_schema_version
                  IS DISTINCT FROM receipt_row->>'identity_schema_version'
               OR existing_receipt.observation_schema_version
                  IS DISTINCT FROM receipt_row->>'observation_schema_version'
               OR existing_receipt.observation_fingerprint
                  IS DISTINCT FROM receipt_row->>'observation_fingerprint'
               OR existing_receipt.core IS DISTINCT FROM receipt_row->'core'
               OR existing_receipt.created_at IS DISTINCT FROM
                  (receipt_row#>>'{core,created_at}')::pg_catalog.timestamptz
               OR (
                   SELECT pg_catalog.to_jsonb(stored_request)
                   FROM public.prediction_ledger_requests AS stored_request
                   WHERE stored_request.ledger_request_id = receipt_row->>'ledger_request_id'
               ) IS DISTINCT FROM (
                   SELECT pg_catalog.to_jsonb(incoming_request)
                   FROM pg_catalog.jsonb_to_record(ledger_row->'request') AS incoming_request(
                       ledger_request_id pg_catalog.text, caller_id pg_catalog.text,
                       request_id pg_catalog.text, request_hash pg_catalog.text,
                       hash_algorithm pg_catalog.text, event_code pg_catalog.text,
                       prediction_as_of pg_catalog.date,
                       created_at pg_catalog.timestamptz
                   )
               )
               OR (
                   SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.to_jsonb(stored_prediction)
                           ORDER BY stored_prediction.prediction_id
                       ),
                       '[]'::pg_catalog.jsonb
                   )
                   FROM public.prediction_ledger_predictions AS stored_prediction
                   WHERE stored_prediction.ledger_request_id =
                         receipt_row->>'ledger_request_id'
               ) IS DISTINCT FROM (
                   SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.to_jsonb(incoming_prediction)
                           ORDER BY incoming_prediction.prediction_id
                       ),
                       '[]'::pg_catalog.jsonb
                   )
                   FROM pg_catalog.jsonb_to_recordset(ledger_row->'predictions')
                   AS incoming_prediction(
                       prediction_id pg_catalog.text,
                       ledger_request_id pg_catalog.text,
                       competitor_id pg_catalog.text, event_code pg_catalog.text,
                       median_seconds pg_catalog.numeric,
                       assigned_mark pg_catalog.integer, source pg_catalog.text,
                       training_eligible pg_catalog.boolean,
                       engine_version pg_catalog.text, model_version pg_catalog.text,
                       calibration_version pg_catalog.text,
                       evidence_cutoff pg_catalog.date,
                       interval_lower pg_catalog.numeric,
                       interval_upper pg_catalog.numeric,
                       interval_coverage pg_catalog.numeric,
                       interval_state pg_catalog.text, interval_scope pg_catalog.text,
                       ignored_factors pg_catalog.jsonb, warnings pg_catalog.jsonb,
                       optimizer pg_catalog.text,
                       optimizer_metadata pg_catalog.jsonb,
                       created_at pg_catalog.timestamptz
                   )
               )
               OR (
                   SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.to_jsonb(stored_feature)
                           ORDER BY stored_feature.feature_snapshot_id
                       ),
                       '[]'::pg_catalog.jsonb
                   )
                   FROM public.prediction_ledger_features AS stored_feature
                   JOIN public.prediction_ledger_predictions AS stored_prediction
                     ON stored_prediction.prediction_id = stored_feature.prediction_id
                   WHERE stored_prediction.ledger_request_id =
                         receipt_row->>'ledger_request_id'
               ) IS DISTINCT FROM (
                   SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.to_jsonb(incoming_feature)
                           ORDER BY incoming_feature.feature_snapshot_id
                       ),
                       '[]'::pg_catalog.jsonb
                   )
                   FROM pg_catalog.jsonb_to_recordset(ledger_row->'features')
                   AS incoming_feature(
                       feature_snapshot_id pg_catalog.text,
                       prediction_id pg_catalog.text,
                       feature_name pg_catalog.text,
                       numeric_value pg_catalog.double precision,
                       created_at pg_catalog.timestamptz
                   )
               ) THEN
                RAISE EXCEPTION 'shadow mirror duplicate semantic conflict';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'accepted', TRUE, 'kind', mirror_kind, 'duplicate', TRUE
            );
        END IF;

        PERFORM public.append_prediction_ledger_v2(ledger_row);

        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(receipt_row#>'{core,predictions}') AS item
            WHERE pg_catalog.jsonb_typeof(item) IS DISTINCT FROM 'object'
               OR NOT EXISTS (
                   SELECT 1 FROM public.prediction_ledger_predictions AS prediction
                   WHERE prediction.prediction_id = item->>'prediction_id'
                     AND prediction.ledger_request_id = receipt_row->>'ledger_request_id'
                     AND prediction.competitor_id = item->>'competitor_id'
                     AND prediction.event_code = item->>'event_code'
               )
        ) THEN
            RAISE EXCEPTION 'shadow receipt prediction linkage mismatch';
        END IF;

        INSERT INTO public.shadow_mirror_deliveries (
            outbox_id, schema_version, entity_kind, entity_id, payload_hash,
            producer_created_at
        ) VALUES (
            delivery_row->>'outbox_id', delivery_row->>'schema_version', mirror_kind,
            delivery_row->>'entity_id', delivery_row->>'payload_hash',
            (delivery_row->>'created_at')::pg_catalog.timestamptz
        );
        INSERT INTO public.shadow_receipt_cores (
            ledger_request_id, delivery_outbox_id, caller_id, request_id,
            schema_version, core_schema_version, identity_schema_version,
            observation_schema_version, observation_fingerprint, core, created_at
        ) VALUES (
            receipt_row->>'ledger_request_id', delivery_row->>'outbox_id',
            receipt_row->>'caller_id', receipt_row->>'request_id',
            receipt_row->>'schema_version', receipt_row->>'core_schema_version',
            receipt_row->>'identity_schema_version',
            receipt_row->>'observation_schema_version',
            receipt_row->>'observation_fingerprint', receipt_row->'core',
            (receipt_row#>>'{core,created_at}')::pg_catalog.timestamptz
        );
        RETURN pg_catalog.jsonb_build_object('accepted', TRUE, 'kind', mirror_kind);
    END IF;

    outcome_row := mirror_payload->'numeric_outcome_revision';
    IF pg_catalog.jsonb_typeof(outcome_row) IS DISTINCT FROM 'object'
       OR EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_object_keys(outcome_row) AS key
           WHERE key NOT IN (
               'schema_version', 'field_revision_id', 'outcome_revision_id',
               'ledger_request_id', 'caller_id', 'actor', 'reason_code',
               'created_at', 'revisions'
           )
       ) OR (
           SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(outcome_row)
       ) <> 9 THEN
        RAISE EXCEPTION 'numeric outcome mirror has unknown or missing properties';
    END IF;
    IF outcome_row->>'schema_version'
       IS DISTINCT FROM 'strathmark.shadow-numeric-outcome-mirror.v1'
       OR delivery_row->>'entity_id' IS DISTINCT FROM outcome_row->>'field_revision_id'
       OR pg_catalog.jsonb_typeof(outcome_row->'revisions') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(outcome_row->'revisions') = 0
       OR pg_catalog.jsonb_array_length(outcome_row->'revisions') > 512
       OR outcome_row->>'reason_code' NOT IN (
           'corrected_time', 'retract_invalid_numeric_evidence', 'valid_replacement'
       ) AND outcome_row->>'reason_code' IS NOT NULL THEN
        RAISE EXCEPTION 'numeric outcome mirror schema or cardinality is invalid';
    END IF;
    IF outcome_row->>'caller_id'
          !~ '^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$'
       OR outcome_row->>'outcome_revision_id'
          !~ '^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$'
       OR outcome_row->>'actor'
          !~ '^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$' THEN
        RAISE EXCEPTION 'numeric outcome identity is invalid';
    END IF;
    caller_namespace := pg_catalog.split_part(outcome_row->>'caller_id', ':', 1);
    IF pg_catalog.split_part(outcome_row->>'outcome_revision_id', ':', 1)
          IS DISTINCT FROM caller_namespace
       OR pg_catalog.split_part(outcome_row->>'actor', ':', 1)
          IS DISTINCT FROM caller_namespace THEN
        RAISE EXCEPTION 'numeric outcome identity namespace mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.shadow_receipt_cores AS receipt
        WHERE receipt.ledger_request_id = outcome_row->>'ledger_request_id'
          AND receipt.caller_id = outcome_row->>'caller_id'
    ) THEN
        RAISE EXCEPTION 'numeric outcome receipt linkage mismatch';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(outcome_row->'revisions') AS item
        WHERE CASE
            WHEN pg_catalog.jsonb_typeof(item) = 'object'
             AND pg_catalog.jsonb_typeof(item->'revision') = 'number'
            THEN (item->>'revision')::pg_catalog.numeric < 1
              OR (item->>'revision')::pg_catalog.numeric > 2147483647
              OR pg_catalog.floor((item->>'revision')::pg_catalog.numeric)
                 IS DISTINCT FROM (item->>'revision')::pg_catalog.numeric
            ELSE TRUE
        END
    ) THEN
        RAISE EXCEPTION 'numeric revision must be an exact bounded integer';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(outcome_row->'revisions') AS item
        WHERE pg_catalog.jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_object_keys(item) AS key
               WHERE key NOT IN (
                   'revision_id', 'prediction_id', 'revision', 'competitor_id',
                   'event_code', 'action', 'actual_time', 'residual',
                   'supersedes_revision_id'
               )
           )
           OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(item)) <> 9
           OR NOT EXISTS (
               SELECT 1 FROM public.prediction_ledger_predictions AS prediction
               WHERE prediction.prediction_id = item->>'prediction_id'
                 AND prediction.ledger_request_id = outcome_row->>'ledger_request_id'
                 AND prediction.competitor_id = item->>'competitor_id'
                 AND prediction.event_code = item->>'event_code'
           )
           OR item->>'action' NOT IN ('settle', 'void')
           OR (item->>'action' = 'settle' AND (
               pg_catalog.jsonb_typeof(item->'actual_time') IS DISTINCT FROM 'number'
               OR pg_catalog.jsonb_typeof(item->'residual') IS DISTINCT FROM 'number'
               OR (item->>'actual_time')::pg_catalog.numeric <= 0
               OR (item->>'actual_time')::pg_catalog.numeric > 300
               OR item->>'residual' IS NULL
           ))
           OR (item->>'action' = 'void' AND (
               item->>'actual_time' IS NOT NULL OR item->>'residual' IS NOT NULL
           ))
    ) THEN
        RAISE EXCEPTION 'numeric settlement revision linkage or value is invalid';
    END IF;

    IF delivery_exists THEN
        SELECT * INTO existing_outcome
        FROM public.shadow_numeric_outcome_revisions
        WHERE delivery_outbox_id = delivery_row->>'outbox_id';
        IF NOT FOUND
           OR existing_outcome.field_revision_id
              IS DISTINCT FROM outcome_row->>'field_revision_id'
           OR existing_outcome.outcome_revision_id
              IS DISTINCT FROM outcome_row->>'outcome_revision_id'
           OR existing_outcome.ledger_request_id
              IS DISTINCT FROM outcome_row->>'ledger_request_id'
           OR existing_outcome.caller_id IS DISTINCT FROM outcome_row->>'caller_id'
           OR existing_outcome.schema_version
              IS DISTINCT FROM outcome_row->>'schema_version'
           OR existing_outcome.actor IS DISTINCT FROM outcome_row->>'actor'
           OR existing_outcome.reason_code
              IS DISTINCT FROM NULLIF(outcome_row->>'reason_code', '')
           OR existing_outcome.created_at IS DISTINCT FROM
              (outcome_row->>'created_at')::pg_catalog.timestamptz
           OR (
               SELECT COALESCE(
                   pg_catalog.jsonb_agg(
                       pg_catalog.to_jsonb(stored_revision)
                       ORDER BY stored_revision.prediction_id
                   ),
                   '[]'::pg_catalog.jsonb
               )
               FROM public.shadow_numeric_settlement_revisions AS stored_revision
               WHERE stored_revision.field_revision_id =
                     outcome_row->>'field_revision_id'
           ) IS DISTINCT FROM (
               SELECT COALESCE(
                   pg_catalog.jsonb_agg(
                       pg_catalog.to_jsonb(incoming_revision)
                       || pg_catalog.jsonb_build_object(
                           'field_revision_id', outcome_row->>'field_revision_id',
                           'created_at',
                           (outcome_row->>'created_at')::pg_catalog.timestamptz
                       )
                       ORDER BY incoming_revision.prediction_id
                   ),
                   '[]'::pg_catalog.jsonb
               )
               FROM pg_catalog.jsonb_to_recordset(outcome_row->'revisions')
               AS incoming_revision(
                   revision_id pg_catalog.text, prediction_id pg_catalog.text,
                   revision pg_catalog.integer, competitor_id pg_catalog.text,
                   event_code pg_catalog.text, action pg_catalog.text,
                   actual_time pg_catalog.numeric, residual pg_catalog.numeric,
                   supersedes_revision_id pg_catalog.text
               )
           ) THEN
            RAISE EXCEPTION 'shadow mirror duplicate semantic conflict';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'accepted', TRUE, 'kind', mirror_kind, 'duplicate', TRUE
        );
    END IF;

    FOR revision_row IN
        SELECT value
        FROM pg_catalog.jsonb_array_elements(outcome_row->'revisions') AS items(value)
    LOOP
        SELECT prediction.median_seconds
        INTO prediction_median
        FROM public.prediction_ledger_predictions AS prediction
        WHERE prediction.prediction_id = revision_row->>'prediction_id';

        SELECT candidate.revision, candidate.revision_id, candidate.action
        INTO prior_revision, prior_revision_id, prior_action
        FROM (
            SELECT settlement.revision, settlement.settlement_id AS revision_id,
                   'settle'::pg_catalog.text AS action,
                   0 AS source_priority, settlement.settled_at AS authority_timestamp,
                   settlement.settlement_id AS authority_id
            FROM public.prediction_ledger_settlements AS settlement
            WHERE settlement.prediction_id = revision_row->>'prediction_id'
            UNION ALL
            SELECT settlement.revision, settlement.revision_id, settlement.action,
                   1 AS source_priority,
                   settlement.created_at AS authority_timestamp,
                   settlement.revision_id AS authority_id
            FROM public.shadow_numeric_settlement_revisions AS settlement
            WHERE settlement.prediction_id = revision_row->>'prediction_id'
        ) AS candidate
        ORDER BY candidate.revision DESC, candidate.source_priority DESC,
                 candidate.authority_timestamp DESC, candidate.authority_id DESC
        LIMIT 1;
        prior_revision := COALESCE(prior_revision, 0);
        IF (revision_row->>'revision')::pg_catalog.integer <> prior_revision + 1 THEN
            RAISE EXCEPTION 'numeric settlement revision sequence conflict';
        END IF;
        IF revision_row->>'action' = 'void'
           AND (prior_revision = 0 OR prior_action IS DISTINCT FROM 'settle') THEN
            RAISE EXCEPTION 'numeric void requires an active settlement';
        END IF;
        IF (prior_revision > 0 OR revision_row->>'action' = 'void')
           AND NULLIF(outcome_row->>'reason_code', '') IS NULL THEN
            RAISE EXCEPTION 'numeric correction or void requires a reason_code';
        END IF;
        IF (prior_revision = 0 AND revision_row->>'supersedes_revision_id' IS NOT NULL)
           OR (prior_revision > 0 AND revision_row->>'supersedes_revision_id'
               IS DISTINCT FROM prior_revision_id) THEN
            RAISE EXCEPTION
                'numeric settlement must supersede the exact latest authoritative revision';
        END IF;
        -- Python computes residual from binary floats; accept at most 1e-9 seconds
        -- of serialization noise while rejecting caller-chosen numeric evidence.
        IF revision_row->>'action' = 'settle'
           AND pg_catalog.abs(
               (revision_row->>'residual')::pg_catalog.numeric
               - (
                   (revision_row->>'actual_time')::pg_catalog.numeric
                   - prediction_median
               )
           ) > 0.000000001 THEN
            RAISE EXCEPTION 'numeric residual does not match mirrored prediction';
        END IF;
    END LOOP;

    INSERT INTO public.shadow_mirror_deliveries (
        outbox_id, schema_version, entity_kind, entity_id, payload_hash,
        producer_created_at
    ) VALUES (
        delivery_row->>'outbox_id', delivery_row->>'schema_version', mirror_kind,
        delivery_row->>'entity_id', delivery_row->>'payload_hash',
        (delivery_row->>'created_at')::pg_catalog.timestamptz
    );
    INSERT INTO public.shadow_numeric_outcome_revisions (
        field_revision_id, outcome_revision_id, ledger_request_id,
        delivery_outbox_id, caller_id, schema_version, actor, reason_code, created_at
    ) VALUES (
        outcome_row->>'field_revision_id', outcome_row->>'outcome_revision_id',
        outcome_row->>'ledger_request_id', delivery_row->>'outbox_id',
        outcome_row->>'caller_id', outcome_row->>'schema_version',
        outcome_row->>'actor', NULLIF(outcome_row->>'reason_code', ''),
        (outcome_row->>'created_at')::pg_catalog.timestamptz
    );
    INSERT INTO public.shadow_numeric_settlement_revisions (
        revision_id, field_revision_id, prediction_id, revision, competitor_id,
        event_code, action, actual_time, residual, supersedes_revision_id, created_at
    )
    SELECT
        row.revision_id, outcome_row->>'field_revision_id', row.prediction_id,
        row.revision, row.competitor_id, row.event_code, row.action,
        row.actual_time, row.residual, row.supersedes_revision_id,
        (outcome_row->>'created_at')::pg_catalog.timestamptz
    FROM pg_catalog.jsonb_to_recordset(outcome_row->'revisions') AS row(
        revision_id pg_catalog.text, prediction_id pg_catalog.text,
        revision pg_catalog.integer, competitor_id pg_catalog.text,
        event_code pg_catalog.text, action pg_catalog.text,
        actual_time pg_catalog.numeric, residual pg_catalog.numeric,
        supersedes_revision_id pg_catalog.text
    );
    RETURN pg_catalog.jsonb_build_object('accepted', TRUE, 'kind', mirror_kind);
END;
$$;

ALTER FUNCTION public.append_shadow_mirror_v1(pg_catalog.jsonb)
    OWNER TO strathmark_prediction_rpc_owner;
REVOKE ALL ON FUNCTION public.append_shadow_mirror_v1(pg_catalog.jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.append_shadow_mirror_v1(pg_catalog.jsonb)
    TO service_role;

COMMIT;

-- Forward repair: if active shadow evidence exists, keep these rows and repair
-- additively or restore them from the durable local ledger.  Do not apply the
-- guarded down file to an activated mirror.
