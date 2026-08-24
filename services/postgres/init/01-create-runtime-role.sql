-- Bootstrap the least-privilege role the application connects as.
--
-- Postgres runs everything in /docker-entrypoint-initdb.d exactly once, on a
-- brand-new data directory. This script only creates the ROLE, so a fresh
-- local stack has it before migrations run.
--
-- The GRANTs are deliberately NOT here. They belong to the Alembic migration
-- (migrations/versions/*_harden_audit_immutability.py), because grants are
-- per-table and have to stay in step with the schema -- and because a
-- production database is not created by this script at all. The migration
-- creates the role too, idempotently, so an existing volume or a managed
-- Postgres reaches the same state.
--
-- In short: this file is a local convenience; the migration is authoritative.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'appruntime') THEN
        CREATE ROLE appruntime LOGIN PASSWORD 'appruntimepassword';
    END IF;
END
$$;
