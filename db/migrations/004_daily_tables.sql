-- Migration 004: tables filled by the nightly Airflow DAG (daily_quality_and_stats).
-- Safe to run more than once. Applied by deploy/update.sh.

-- One row per day and line: the input for trend charts and, later, delay prediction
CREATE TABLE IF NOT EXISTS daily_line_stats (
    service_date        DATE    NOT NULL,
    line_name           TEXT    NOT NULL,
    mode                TEXT    NOT NULL,
    departures          INTEGER NOT NULL,   -- with realtime data, not cancelled
    cancelled           INTEGER NOT NULL,
    on_time_pct         NUMERIC(5, 2),
    avg_delay_seconds   NUMERIC(8, 1),
    p90_delay_seconds   NUMERIC(8, 1),
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service_date, line_name, mode)
);

-- Results of the nightly data-quality checks, so problems stay visible, not only in logs
CREATE TABLE IF NOT EXISTS data_quality_checks (
    service_date  DATE    NOT NULL,
    check_name    TEXT    NOT NULL,
    value         NUMERIC,
    threshold     NUMERIC,
    passed        BOOLEAN NOT NULL,
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service_date, check_name)
);

-- The read-only dashboard user may read the new tables (if that user exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard') THEN
        GRANT SELECT ON daily_line_stats, data_quality_checks TO dashboard;
    END IF;
END $$;
