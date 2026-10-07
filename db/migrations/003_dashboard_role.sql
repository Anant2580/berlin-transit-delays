-- Migration 003: a read-only database user for the public dashboard.
-- The dashboard can only SELECT, so even a bug in it cannot change or delete data.
-- Run with psql and the password as a variable (deploy/update.sh does this):
--   psql -v dash_pw=... -f 003_dashboard_role.sql

SELECT 'CREATE ROLE dashboard LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard') \gexec

ALTER ROLE dashboard WITH LOGIN PASSWORD :'dash_pw';
ALTER ROLE dashboard SET default_transaction_read_only = on;
ALTER ROLE dashboard SET statement_timeout = '30s';

GRANT USAGE ON SCHEMA public TO dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dashboard;
