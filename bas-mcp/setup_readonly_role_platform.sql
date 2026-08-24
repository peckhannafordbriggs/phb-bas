-- =============================================================================
-- A dedicated read-only role for the PLATFORM database (phb_platform).
--
-- The companion to setup_readonly_role.sql, which does the same job for the
-- standalone `bas` database. Two scripts rather than one because the two
-- databases are shaped differently in the one way that matters here.
--
-- WHY A SECOND ROLE, AND NOT THE SAME ONE
--
-- PostgreSQL roles are cluster-wide, so there is exactly one password per role
-- name. `bas_readonly` on the standalone database uses 'bas_readonly_local',
-- which is committed in setup_readonly_role.sql on purpose and which that file
-- warns must never be reused elsewhere. Granting that same role SELECT on the
-- platform database would hand a committed password read access to real
-- building data. So this is a distinct role with a password that is NOT in this
-- file.
--
-- WHY EVERY GRANT IS PER-TABLE
--
-- The standalone database had a schema of its own (`bas`), so
-- "GRANT SELECT ON ALL TABLES IN SCHEMA bas" was precise. The platform keeps its
-- BAS tables in `public` alongside `employees`, `audit_events`, `module_grants`,
-- `draft_locks` and `_prisma_migrations`. A blanket grant on `public` would let
-- a dashboard role read the employee directory. This grants table by table on
-- names matching 'bas\_%', the same shape used for the `bas_collector` role on
-- 24 August 2026.
--
-- ALTER DEFAULT PRIVILEGES IS DELIBERATELY ABSENT. Read this before adding it.
--
-- The standalone script uses it so that objects created by future migrations are
-- covered automatically. That mechanism CANNOT be used here: default privileges
-- apply per schema and per creating role, and there is no way to filter them by
-- table name. `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES`
-- would silently grant this role SELECT on the next table Prisma creates,
-- whether that is `bas_alarms` or `employee_ssn`.
--
-- The cost of leaving it out is that a new bas_* table or view is invisible to
-- Grafana and the MCP server until this script is re-run. That failure is loud
-- (a permissions error in a panel) and the fix is one command. The cost of
-- putting it in is silent and unbounded. See docs/runbook.md in phb-platform,
-- *A new bas_* table is invisible to Grafana until this is re-run*.
--
-- RUN IT (as a superuser, from C:\dev\bas-mcp):
--
--   psql "postgresql://postgres:...@localhost:5432/phb_platform" \
--        -v pw=<a new password, not bas_readonly_local> \
--        -f setup_readonly_role_platform.sql
--
-- Pass the password bare. `:'pw'` below asks psql to quote it as a SQL literal,
-- which is also what escapes a password containing a quote.
-- =============================================================================

\set ON_ERROR_STOP on

\if :{?pw}
\else
\echo 'ERROR: pass the password with  -v pw=yourpassword'
\quit 1
\endif

-- CREATE or ALTER, whichever applies, so the script is re-runnable and is also
-- how the password gets rotated.
--
-- Built as text and run with \gexec rather than in a DO block, because psql does
-- NOT substitute :variables inside dollar-quoted strings - `format(..., :'pw')`
-- inside $$ ... $$ fails with `syntax error at or near ":"`. Measured, not
-- guessed: that is how the first version of this file failed.
SELECT format('CREATE ROLE bas_readonly_platform WITH LOGIN PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bas_readonly_platform')
UNION ALL
SELECT format('ALTER ROLE bas_readonly_platform WITH LOGIN PASSWORD %L', :'pw')
 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bas_readonly_platform');
\gexec

GRANT CONNECT ON DATABASE phb_platform TO bas_readonly_platform;

-- USAGE on the schema is not a read grant. It only makes the schema's contents
-- addressable; without a per-table SELECT there is still nothing to read.
GRANT USAGE ON SCHEMA public TO bas_readonly_platform;

-- --- SELECT, table by table, on bas_* only -----------------------------------
--
-- pg_class rather than pg_tables + pg_views: one pass covers ordinary tables
-- ('r'), views ('v'), partitioned tables ('p') and materialised views ('m'), so
-- a future bas_* object of a different relkind is not quietly skipped.
--
-- The pattern is 'bas\_%' with the underscore escaped. Unescaped, '_' is a
-- single-character wildcard in LIKE and 'bas_%' would also match a table called
-- 'basement_survey'.
DO $$
DECLARE
    r record;
    n int := 0;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        WHERE ns.nspname = 'public'
          AND c.relkind IN ('r', 'v', 'm', 'p')
          AND c.relname LIKE 'bas\_%'
        ORDER BY c.relname
    LOOP
        EXECUTE format('GRANT SELECT ON public.%I TO bas_readonly_platform', r.relname);
        n := n + 1;
    END LOOP;

    -- Refuse to finish having granted nothing. A silent zero here would leave a
    -- role that exists, connects, and can read nothing - which presents as a
    -- broken dashboard rather than as a setup script that did not run.
    IF n = 0 THEN
        RAISE EXCEPTION
            'Granted SELECT on 0 objects. Is this the platform database? '
            'Expected public.bas_* tables to exist.';
    END IF;

    RAISE NOTICE 'Granted SELECT on % bas_* objects.', n;
END
$$;

-- --- withhold everything else ------------------------------------------------
--
-- None of these are granted by default. They are stated so that the intent is
-- unmistakable to whoever reads this next, and so that a future blanket grant
-- run by hand does not survive the next run of this script.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public FROM bas_readonly_platform;
REVOKE CREATE ON SCHEMA public FROM bas_readonly_platform;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM bas_readonly_platform;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM bas_readonly_platform;

-- The platform's own tables, named explicitly. The REVOKE above already covers
-- them, but naming them means a reader can see that the question was asked, and
-- a future audit can grep for the table it cares about.
REVOKE ALL ON public.employees          FROM bas_readonly_platform;
REVOKE ALL ON public.audit_events       FROM bas_readonly_platform;
REVOKE ALL ON public.module_grants      FROM bas_readonly_platform;
REVOKE ALL ON public.modules            FROM bas_readonly_platform;
REVOKE ALL ON public.positions          FROM bas_readonly_platform;
REVOKE ALL ON public.departments        FROM bas_readonly_platform;
REVOKE ALL ON public.draft_locks        FROM bas_readonly_platform;
REVOKE ALL ON public._prisma_migrations FROM bas_readonly_platform;

-- --- role-level settings -----------------------------------------------------
--
-- Same two as the standalone role. Set on the role rather than asked for by the
-- client, so they apply to every session regardless of what connects.
--
-- statement_timeout matters more on the platform database than it did on the
-- standalone one: the collector is writing to this database, and a runaway
-- dashboard query holding locks or burning CPU here delays collection against a
-- 41.7-hour roll horizon.
ALTER ROLE bas_readonly_platform SET statement_timeout = '30s';
ALTER ROLE bas_readonly_platform SET default_transaction_read_only = on;

\echo ''
\echo 'Created/updated role: bas_readonly_platform'
\echo 'Connection string (substitute the password you passed in):'
\echo '  postgresql://bas_readonly_platform:<password>@localhost:5432/phb_platform'
\echo ''
\echo 'Now prove the boundary:'
\echo '  psql "<that string>" -c "SELECT count(*) FROM bas_points"        -- must work'
\echo '  psql "<that string>" -c "SELECT count(*) FROM employees"         -- must be DENIED'
\echo '  psql "<that string>" -c "SELECT count(*) FROM audit_events"      -- must be DENIED'
\echo ''
