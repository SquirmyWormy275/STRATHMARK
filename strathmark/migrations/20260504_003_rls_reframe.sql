-- Migration: RLS reframe for controlled-write enforcement
-- Date:      2026-05-04
-- Author:    persistence-reframe PR (expanded scope: out-of-scope items)
-- Reversible: yes (DROP POLICY + ALTER TABLE DISABLE RLS); see Rollback section
-- Idempotent: yes (DROP POLICY IF EXISTS + CREATE POLICY)
--
-- Purpose
-- -------
-- Replaces "read-only by convention" with PostgreSQL RLS enforcement.
-- After this migration:
--
--   - Cache tables (results, competitors): writes denied for any role
--     except a dedicated mnemex_sync Postgres role. The sync function
--     (strathmark.sync) holds the only key with that role.
--   - ML state tables (model_versions, calibration_tables, feature_store,
--     predictions, prediction_residuals): writes allowed from the
--     STRATHMARK service-role key. This is the explicit ML-state carve-out.
--   - sync_log: writes from the mnemex_sync role only.
--   - wood_species: writes from a dedicated wood_admin role only (rare,
--     reference-data maintenance).
--
-- Reads remain unrestricted across all tables for both anon and
-- service_role keys.
--
-- IMPORTANT pre-application steps (all required before this migration enforces anything)
-- ---------------------------------------------------------------------------------------
-- The Supabase service_role role has the BYPASSRLS attribute by default.
-- ENABLE ROW LEVEL SECURITY alone does NOT constrain it. To make the
-- controlled-write policies actually enforce, all of the following are
-- required, in order:
--
-- 1. Create the mnemex_sync role in the Supabase dashboard's Database
--    Settings -> Roles. Mark it as a member of authenticated. Do NOT
--    grant BYPASSRLS to it. Grant USAGE on schema public; GRANT SELECT,
--    INSERT, UPDATE, DELETE ON results, competitors, sync_log TO
--    mnemex_sync;.
-- 2. Create a wood_admin role similarly for reference-data maintenance.
--    Grant SELECT, INSERT, UPDATE, DELETE ON wood_species TO wood_admin;.
-- 3. Provision a dedicated MNEMEX_SYNC_DB_URL/KEY for the sync function
--    that authenticates AS mnemex_sync (not service_role). Update
--    strathmark.sync to use this client instead of _get_client() from db.py.
-- 4. Remove BYPASSRLS from service_role for the duration this enforcement
--    is desired:
--        ALTER ROLE service_role NOBYPASSRLS;
--    OR rotate STRATHMARK_SUPABASE_KEY to a non-BYPASSRLS role used solely
--    for ML-state writes (preferred -- keeps service_role for emergency
--    operator access).
-- 5. Apply this migration. The FORCE ROW LEVEL SECURITY directives below
--    ensure the policies enforce even on table owners; combined with
--    NOBYPASSRLS on service_role, this closes the loop.
-- 6. Verify by attempting a write to results from the STRATHMARK service-
--    role client and asserting a permission-denied error.
--
-- If steps 1-4 are NOT complete, applying this migration will silently
-- succeed but the policies will be a no-op against any BYPASSRLS role.
-- The migration does not refuse to apply in that state -- the operator
-- is responsible for verifying the role plumbing first.

BEGIN;

-- ---------------------------------------------------------------------------
-- Enable RLS on every table touched by this migration
-- ---------------------------------------------------------------------------

-- ENABLE + FORCE: FORCE ensures policies apply even to the table owner,
-- which (combined with NOBYPASSRLS on service_role) closes the BYPASSRLS
-- bypass that would otherwise render these policies a no-op.
ALTER TABLE competitors           ENABLE ROW LEVEL SECURITY;
ALTER TABLE competitors           FORCE  ROW LEVEL SECURITY;
ALTER TABLE results               ENABLE ROW LEVEL SECURITY;
ALTER TABLE results               FORCE  ROW LEVEL SECURITY;
ALTER TABLE sync_log              ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_log              FORCE  ROW LEVEL SECURITY;
ALTER TABLE wood_species          ENABLE ROW LEVEL SECURITY;
ALTER TABLE wood_species          FORCE  ROW LEVEL SECURITY;
ALTER TABLE model_versions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_versions        FORCE  ROW LEVEL SECURITY;
ALTER TABLE calibration_tables    ENABLE ROW LEVEL SECURITY;
ALTER TABLE calibration_tables    FORCE  ROW LEVEL SECURITY;
ALTER TABLE feature_store         ENABLE ROW LEVEL SECURITY;
ALTER TABLE feature_store         FORCE  ROW LEVEL SECURITY;
ALTER TABLE predictions           ENABLE ROW LEVEL SECURITY;
ALTER TABLE predictions           FORCE  ROW LEVEL SECURITY;
ALTER TABLE prediction_residuals  ENABLE ROW LEVEL SECURITY;
ALTER TABLE prediction_residuals  FORCE  ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- Universal read policy (anon + authenticated + service_role can SELECT)
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'competitors', 'results', 'sync_log', 'wood_species',
        'model_versions', 'calibration_tables', 'feature_store',
        'predictions', 'prediction_residuals'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I_read ON %I', t, t);
        EXECUTE format(
            'CREATE POLICY %I_read ON %I FOR SELECT USING (true)',
            t, t
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Cache tables (sync function writes only): results, competitors
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS results_write_sync ON results;
CREATE POLICY results_write_sync ON results
    FOR ALL
    USING (current_user = 'mnemex_sync')
    WITH CHECK (current_user = 'mnemex_sync');

DROP POLICY IF EXISTS competitors_write_sync ON competitors;
CREATE POLICY competitors_write_sync ON competitors
    FOR ALL
    USING (current_user = 'mnemex_sync')
    WITH CHECK (current_user = 'mnemex_sync');

-- ---------------------------------------------------------------------------
-- sync_log (sync function writes only)
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS sync_log_write_sync ON sync_log;
CREATE POLICY sync_log_write_sync ON sync_log
    FOR ALL
    USING (current_user = 'mnemex_sync')
    WITH CHECK (current_user = 'mnemex_sync');

-- ---------------------------------------------------------------------------
-- wood_species (operator maintenance only; rare)
-- ---------------------------------------------------------------------------

-- wood_admin only. Header intent (line 19) was 'wood_admin only'; the
-- previous draft also allowed service_role which contradicts that intent.
-- If an operator needs emergency access without the wood_admin role,
-- they can grant the role temporarily rather than relying on a permanent
-- service_role bypass.
DROP POLICY IF EXISTS wood_species_write_admin ON wood_species;
CREATE POLICY wood_species_write_admin ON wood_species
    FOR ALL
    USING (current_user = 'wood_admin')
    WITH CHECK (current_user = 'wood_admin');

-- ---------------------------------------------------------------------------
-- ML state tables (STRATHMARK-internal carve-out)
-- The service_role key writes here. This IS the explicit carve-out.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'model_versions', 'calibration_tables', 'feature_store',
        'predictions', 'prediction_residuals'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I_write_strathmark ON %I', t, t);
        EXECUTE format(
            'CREATE POLICY %I_write_strathmark ON %I FOR ALL '
            'USING (current_user = ''service_role'') '
            'WITH CHECK (current_user = ''service_role'')',
            t, t
        );
    END LOOP;
END $$;

COMMIT;

-- ---------------------------------------------------------------------------
-- Rollback
-- ---------------------------------------------------------------------------
--
-- WARNING: this rollback removes RLS enforcement and reverts to "read-only by
-- convention" — the legacy state. Use only if the controlled-write
-- enforcement is causing operational pain that cannot be fixed by adjusting
-- the role plumbing.
--
-- BEGIN;
--
-- DO $$
-- DECLARE t TEXT;
-- BEGIN
--     FOREACH t IN ARRAY ARRAY[
--         'competitors', 'results', 'sync_log', 'wood_species',
--         'model_versions', 'calibration_tables', 'feature_store',
--         'predictions', 'prediction_residuals'
--     ]
--     LOOP
--         EXECUTE format('DROP POLICY IF EXISTS %I_read ON %I', t, t);
--         EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
--     END LOOP;
-- END $$;
--
-- DROP POLICY IF EXISTS results_write_sync ON results;
-- DROP POLICY IF EXISTS competitors_write_sync ON competitors;
-- DROP POLICY IF EXISTS sync_log_write_sync ON sync_log;
-- DROP POLICY IF EXISTS wood_species_write_admin ON wood_species;
--
-- DO $$
-- DECLARE t TEXT;
-- BEGIN
--     FOREACH t IN ARRAY ARRAY[
--         'model_versions', 'calibration_tables', 'feature_store',
--         'predictions', 'prediction_residuals'
--     ]
--     LOOP
--         EXECUTE format('DROP POLICY IF EXISTS %I_write_strathmark ON %I', t, t);
--     END LOOP;
-- END $$;
--
-- COMMIT;
