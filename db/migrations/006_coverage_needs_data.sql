-- Migration 006: a collector run only counts as coverage if it actually received data.
-- Safe to run more than once. Applied by deploy/update.sh (after 002, which it replaces).
--
-- Before: in_coverage was TRUE if ANY collector run started in the 15 minutes before the
-- planned departure. During a VBB API outage the collector still runs and logs a row in
-- collector_runs (with stations_ok = 0), so outages were wrongly counted as covered time.
-- Now: only runs with at least one successful station request count.

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
          AND r.stations_ok > 0                              -- the run received data
    )                                                        AS in_coverage
FROM departures d
JOIN stations s USING (station_id)
WHERE d.planned_time < now();
