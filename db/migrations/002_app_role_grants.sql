-- Least-privilege runtime role for the monitor.
-- The role itself (with its password) is created by scripts/initialize_database.py,
-- which then applies this file. Run with an administrator connection, once.
--
-- The runtime role can read/insert/update monitor tables, delete only lock rows,
-- and has no DDL rights, no access to other schemas, and no BYPASSRLS.

BEGIN;
SET LOCAL search_path TO nyrr_monitor;

REVOKE ALL ON SCHEMA nyrr_monitor FROM PUBLIC;
GRANT USAGE ON SCHEMA nyrr_monitor TO nyrr_monitor_app;

GRANT SELECT, INSERT, UPDATE ON
    events, shifts, monitoring_runs, availability_transitions, notifications,
    app_state, health_incidents, email_test_runs, email_test_cycles, email_test_deliveries
TO nyrr_monitor_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON locks TO nyrr_monitor_app;
GRANT SELECT ON schema_migrations TO nyrr_monitor_app;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['events','shifts','monitoring_runs','availability_transitions',
                             'notifications','app_state','locks','health_incidents',
                             'email_test_runs','email_test_cycles','email_test_deliveries']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS monitor_app_rw ON nyrr_monitor.%I', t);
        EXECUTE format('CREATE POLICY monitor_app_rw ON nyrr_monitor.%I TO nyrr_monitor_app USING (TRUE) WITH CHECK (TRUE)', t);
    END LOOP;
END $$;

ALTER ROLE nyrr_monitor_app SET search_path TO nyrr_monitor;
ALTER ROLE nyrr_monitor_app SET statement_timeout TO '30s';

INSERT INTO schema_migrations (version) VALUES ('002_app_role_grants')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
