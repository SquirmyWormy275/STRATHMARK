-- Roll back migration 007 only before any shadow receipt or numeric evidence is active.
-- After activation, use forward repair or restore from the durable local ledger.

BEGIN;

-- The mirror RPC writes delivery, header, then child rows.  Lock in that same
-- stable order so no append can cross the activation check.
LOCK TABLE public.shadow_mirror_deliveries IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.shadow_numeric_outcome_revisions IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.shadow_numeric_settlement_revisions IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.shadow_receipt_cores IN ACCESS EXCLUSIVE MODE;
SET LOCAL row_security = off;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.shadow_mirror_deliveries)
       OR EXISTS (SELECT 1 FROM public.shadow_numeric_outcome_revisions)
       OR EXISTS (SELECT 1 FROM public.shadow_numeric_settlement_revisions)
       OR EXISTS (SELECT 1 FROM public.shadow_receipt_cores) THEN
        RAISE EXCEPTION
            'cannot roll back migration 007 while active shadow evidence exists';
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS prediction_ledger_settlements_numeric_authority
    ON public.prediction_ledger_settlements;
DROP FUNCTION IF EXISTS public.reject_legacy_settlement_after_shadow_authority();
DROP FUNCTION IF EXISTS public.append_shadow_mirror_v1(pg_catalog.jsonb);
DROP TABLE IF EXISTS public.shadow_numeric_settlement_revisions;
DROP TABLE IF EXISTS public.shadow_numeric_outcome_revisions;
DROP TABLE IF EXISTS public.shadow_receipt_cores;
DROP TABLE IF EXISTS public.shadow_mirror_deliveries;

COMMIT;
