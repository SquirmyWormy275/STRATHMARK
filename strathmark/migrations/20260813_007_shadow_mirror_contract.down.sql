-- Roll back migration 007 only before any shadow receipt or numeric evidence is active.
-- After activation, use forward repair or restore from the durable local ledger.

BEGIN;

DO $$
BEGIN
    IF pg_catalog.to_regclass('public.shadow_mirror_deliveries') IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.shadow_mirror_deliveries) THEN
        RAISE EXCEPTION
            'cannot roll back migration 007 while active shadow evidence exists';
    END IF;
END;
$$;

DROP FUNCTION IF EXISTS public.append_shadow_mirror_v1(pg_catalog.jsonb);
DROP TABLE IF EXISTS public.shadow_numeric_settlement_revisions;
DROP TABLE IF EXISTS public.shadow_numeric_outcome_revisions;
DROP TABLE IF EXISTS public.shadow_receipt_cores;
DROP TABLE IF EXISTS public.shadow_mirror_deliveries;

COMMIT;
