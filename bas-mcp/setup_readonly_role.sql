-- =============================================================================
-- A dedicated read-only database role for the MCP server.
--
-- The server already refuses non-SELECT queries and opens read-only
-- transactions. This is the third layer, and it is the one that does not depend
-- on my code being correct: even if the validator had a hole, this role has no
-- permission to write anything.
--
-- That matters more here than in most systems. Building history is
-- irreplaceable — the JACE overwrites its own copy within roughly two days, so
-- a bad DELETE destroys data that exists nowhere else. There is no restore from
-- the source.
--
-- Run once:
--   psql "postgresql://bas:bas_local_dev_only@localhost:5432/bas" -f setup_readonly_role.sql
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bas_readonly') THEN
        -- LOCAL DEVELOPMENT DEFAULT. This password is committed on purpose:
        -- the role is read-only, reachable only on localhost, and this script
        -- exists to create it. NEVER reuse this value on a shared or cloud
        -- database. When the platform database gets its own bas_readonly role,
        -- generate a new password and store it in Key Vault.
        CREATE ROLE bas_readonly WITH LOGIN PASSWORD 'bas_readonly_local';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE bas TO bas_readonly;
GRANT USAGE   ON SCHEMA   bas TO bas_readonly;

GRANT SELECT ON ALL TABLES    IN SCHEMA bas TO bas_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA bas TO bas_readonly;

-- Objects created by FUTURE migrations must be covered automatically, or a new
-- table or view silently becomes invisible to Grafana and the MCP server — and
-- the failure looks like a broken dashboard rather than a permissions problem.
--
-- The FOR ROLE clause matters and is easy to get wrong. Default privileges apply
-- only to objects created by the named role. This script is run as a superuser,
-- but migrations run as `bas`, so without FOR ROLE bas the defaults would attach
-- to the wrong creator and never fire. Recreating a view (which migrations do)
-- also drops its existing grants, so this is not a one-off concern.
ALTER DEFAULT PRIVILEGES FOR ROLE bas IN SCHEMA bas
    GRANT SELECT ON TABLES TO bas_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE bas IN SCHEMA bas
    GRANT SELECT ON SEQUENCES TO bas_readonly;

-- And cover anything created by a superuser too, in case a migration is ever run
-- that way.
ALTER DEFAULT PRIVILEGES IN SCHEMA bas GRANT SELECT ON TABLES TO bas_readonly;

-- Explicitly withhold everything else. Belt and braces — these are not granted
-- by default, but stating it makes the intent unmistakable to whoever reads
-- this next.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA bas FROM bas_readonly;
REVOKE CREATE ON SCHEMA bas FROM bas_readonly;
REVOKE ALL ON SCHEMA public FROM bas_readonly;

-- Query timeout at the role level, so it applies regardless of what the client
-- asks for. A runaway query cannot pin the database that the collector is
-- simultaneously trying to write to.
ALTER ROLE bas_readonly SET statement_timeout = '30s';
ALTER ROLE bas_readonly SET default_transaction_read_only = on;

\echo ''
\echo 'Created role: bas_readonly'
\echo 'Connection string for the MCP server:'
\echo '  postgresql://bas_readonly:bas_readonly_local@localhost:5432/bas'
\echo ''
