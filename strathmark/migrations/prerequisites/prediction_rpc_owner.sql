-- Prediction ledger RPC owner prerequisite (run before migration 005).
--
-- Run this as a database role that is either a superuser or a member of the
-- PostgreSQL predefined pg_create_role role.  This step is intentionally
-- separate from the application migration because ordinary migration roles
-- must not silently gain cluster-wide role-management authority.

DO $$
BEGIN
    IF current_setting('is_superuser') <> 'on'
       AND NOT pg_catalog.pg_has_role(current_user, 'pg_create_role', 'MEMBER') THEN
        RAISE EXCEPTION
            'prediction RPC owner prerequisite requires a superuser or pg_create_role member';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'strathmark_prediction_rpc_owner'
    ) THEN
        CREATE ROLE strathmark_prediction_rpc_owner NOLOGIN NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'strathmark_prediction_rpc_owner'
          AND NOT rolcanlogin
          AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION
            'strathmark_prediction_rpc_owner must be NOLOGIN NOBYPASSRLS';
    END IF;
END;
$$;
