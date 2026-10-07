-- Migration 002: cleaner transport modes and a coverage flag for analysis.
-- Safe to run more than once. Applied by deploy/update.sh.
--
-- mode:        rail replacement buses (e.g. a bus named "S7") and long-distance trains
--              get their own category instead of distorting "Bus" and "Regional train".
-- in_coverage: TRUE when the collector ran in the 15 minutes before the planned departure.
--              Departures planned while the collector was down (laptop asleep, API outage)
--              are only seen afterwards if they were late, so they would bias delays upwards.

CREATE INDEX IF NOT EXISTS idx_collector_runs_started_at ON collector_runs (started_at);

-- New columns are appended at the end so CREATE OR REPLACE VIEW keeps working.
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
    COALESCE(d.platform <> d.planned_platform, FALSE)        AS platform_changed,
    CASE
        WHEN d.line_product = 'bus' AND d.line_name ~* '^(SEV ?)?[SU] ?[0-9]' THEN 'Replacement bus'
        WHEN d.line_product = 'suburban' THEN 'S-Bahn'
        WHEN d.line_product = 'subway'   THEN 'U-Bahn'
        WHEN d.line_product = 'tram'     THEN 'Tram'
        WHEN d.line_product = 'bus'      THEN 'Bus'
        WHEN d.line_product = 'regional' THEN 'Regional train'
        WHEN d.line_product = 'express'  THEN 'Long-distance'
        WHEN d.line_product = 'ferry'    THEN 'Ferry'
        ELSE 'Other'
    END                                                      AS mode,
    EXISTS (
        SELECT 1 FROM collector_runs r
        WHERE r.started_at BETWEEN d.planned_time - INTERVAL '15 minutes' AND d.planned_time
    )                                                        AS in_coverage
FROM departures d
JOIN stations s USING (station_id)
WHERE d.planned_time < now();
