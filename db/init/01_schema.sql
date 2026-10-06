-- Schema for the Berlin public transport delay project.
-- Runs automatically the first time the PostgreSQL container starts.

-- Stations we track (resolved from names in collector/stations.txt)
CREATE TABLE IF NOT EXISTS stations (
    station_id  TEXT PRIMARY KEY,              -- VBB stop id, e.g. '900100003'
    name        TEXT NOT NULL,
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per departure (trip x station x planned time).
-- The collector sees each departure several times while it approaches;
-- every new sighting updates the row, so we keep the latest realtime value.
CREATE TABLE IF NOT EXISTS departures (
    trip_id           TEXT        NOT NULL,
    station_id        TEXT        NOT NULL REFERENCES stations (station_id),
    planned_time      TIMESTAMPTZ NOT NULL,
    actual_time       TIMESTAMPTZ,             -- NULL if cancelled or no realtime data
    delay_seconds     INTEGER,                 -- NULL if no realtime data
    cancelled         BOOLEAN     NOT NULL DEFAULT FALSE,
    line_name         TEXT,                    -- e.g. 'S7', 'U2', 'M10', '100'
    line_product      TEXT,                    -- suburban, subway, tram, bus, ferry, express, regional
    direction         TEXT,
    platform          TEXT,
    planned_platform  TEXT,
    first_seen_at     TIMESTAMPTZ NOT NULL,
    last_seen_at      TIMESTAMPTZ NOT NULL,
    times_seen        INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (trip_id, station_id, planned_time)
);

CREATE INDEX IF NOT EXISTS idx_departures_planned_time ON departures (planned_time);
CREATE INDEX IF NOT EXISTS idx_departures_line ON departures (line_name);

-- One row per collector run, used to monitor data quality and gaps
CREATE TABLE IF NOT EXISTS collector_runs (
    run_id              BIGSERIAL PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ NOT NULL,
    stations_ok         INTEGER     NOT NULL,
    stations_failed     INTEGER     NOT NULL,
    departures_upserted INTEGER     NOT NULL
);

-- Clean view for analysis and dashboards.
-- Only departures whose planned time has passed, with Berlin local time columns.
-- On time = no more than 1 min early and no more than 3 min late (change it here if you like).
CREATE OR REPLACE VIEW v_departures_clean AS
SELECT
    d.trip_id,
    d.station_id,
    s.name                                                   AS station_name,
    s.latitude,
    s.longitude,
    d.line_name,
    d.line_product,
    d.direction,
    d.planned_time,
    d.actual_time,
    d.delay_seconds,
    ROUND(d.delay_seconds / 60.0, 1)                         AS delay_minutes,
    d.cancelled,
    (d.delay_seconds IS NOT NULL)                            AS has_realtime,
    CASE
        WHEN d.cancelled OR d.delay_seconds IS NULL THEN NULL
        ELSE d.delay_seconds BETWEEN -60 AND 180
    END                                                      AS on_time,
    (d.planned_time AT TIME ZONE 'Europe/Berlin')::date      AS service_date,
    EXTRACT(HOUR FROM d.planned_time AT TIME ZONE 'Europe/Berlin')::int    AS hour_of_day,
    EXTRACT(ISODOW FROM d.planned_time AT TIME ZONE 'Europe/Berlin')::int  AS weekday,  -- 1 = Monday
    COALESCE(d.platform <> d.planned_platform, FALSE)        AS platform_changed
FROM departures d
JOIN stations s USING (station_id)
WHERE d.planned_time < now();
