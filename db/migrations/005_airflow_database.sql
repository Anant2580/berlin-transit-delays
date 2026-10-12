-- Migration 005: a separate database and user for Apache Airflow's own metadata
-- (DAG runs, task states, users). Keeps Airflow's tables apart from the transit data.
-- Run with psql and the password as a variable (deploy/update.sh does this):
--   psql -v airflow_pw=... -f 005_airflow_database.sql

SELECT 'CREATE ROLE airflow LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'airflow') \gexec
ALTER ROLE airflow WITH LOGIN PASSWORD :'airflow_pw';

SELECT 'CREATE DATABASE airflow OWNER airflow'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'airflow') \gexec
