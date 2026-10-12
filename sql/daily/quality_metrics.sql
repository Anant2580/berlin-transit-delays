-- Data-quality metrics for one service day (Berlin time). Parameter: %(day)s
WITH day_bounds AS (
    SELECT (%(day)s::date)::timestamp AT TIME ZONE 'Europe/Berlin'                       AS day_start,
           ((%(day)s::date) + 1)::timestamp AT TIME ZONE 'Europe/Berlin'                 AS day_end
),
runs AS (
    SELECT r.* FROM collector_runs r, day_bounds b
    WHERE r.started_at >= b.day_start AND r.started_at < b.day_end
),
gaps AS (
    SELECT EXTRACT(EPOCH FROM started_at - LAG(started_at) OVER (ORDER BY started_at)) / 60.0 AS gap_min
    FROM runs
),
deps AS (
    SELECT d.* FROM v_departures_clean d, day_bounds b
    WHERE d.planned_time >= b.day_start AND d.planned_time < b.day_end
)
SELECT
    (SELECT COUNT(*) FROM runs)                                                     AS collector_runs,
    (SELECT 100.0 * SUM(stations_ok) / NULLIF(SUM(stations_ok + stations_failed), 0) FROM runs)
                                                                                    AS station_success_pct,
    (SELECT COALESCE(MAX(gap_min), 0) FROM gaps)                                    AS longest_gap_min,
    (SELECT COUNT(*) FROM deps)                                                     AS departures,
    (SELECT 100.0 * AVG((has_realtime OR cancelled)::int) FROM deps)                AS realtime_coverage_pct;
