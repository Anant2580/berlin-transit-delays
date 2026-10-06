-- KPI queries for the Berlin public transport delay project.
-- Run them with:  docker compose exec db psql -U transit -d transit -f /sql/kpis.sql
-- or open this file in DBeaver / pgAdmin and run one query at a time.
-- All queries use v_departures_clean (see db/init/01_schema.sql).
-- On time = between 1 min early and 3 min late.


-- 1) Data quality: how much data do we have per day, and how much has realtime info?
SELECT
    service_date,
    COUNT(*)                                                  AS departures,
    ROUND(100.0 * AVG(has_realtime::int), 1)                  AS pct_with_realtime,
    SUM(cancelled::int)                                       AS cancelled
FROM v_departures_clean
GROUP BY service_date
ORDER BY service_date;


-- 2) Collector health: failed station requests and gaps between runs
WITH runs AS (
    SELECT
        started_at,
        stations_failed,
        started_at - LAG(started_at) OVER (ORDER BY started_at) AS gap
    FROM collector_runs
)
SELECT
    DATE(started_at AT TIME ZONE 'Europe/Berlin')             AS day,
    COUNT(*)                                                  AS runs,
    SUM(stations_failed)                                      AS failed_requests,
    MAX(gap)                                                  AS longest_gap   -- big gaps = laptop asleep
FROM runs
GROUP BY 1
ORDER BY 1;


-- 3) Overall KPIs by transport mode
SELECT
    line_product,
    COUNT(*)                                                  AS departures,
    ROUND(100.0 * AVG(on_time::int), 1)                       AS on_time_pct,
    ROUND(AVG(delay_seconds) / 60.0, 2)                       AS avg_delay_min,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY delay_seconds) / 60.0)::numeric, 2) AS median_delay_min,
    ROUND(100.0 * AVG(cancelled::int), 2)                     AS cancelled_pct
FROM v_departures_clean
WHERE has_realtime OR cancelled
GROUP BY line_product
ORDER BY on_time_pct;


-- 4) Average delay by line, worst 15 lines (only lines with enough data)
WITH line_stats AS (
    SELECT
        line_name,
        line_product,
        COUNT(*)                                              AS departures,
        AVG(delay_seconds) / 60.0                             AS avg_delay_min,
        AVG(on_time::int)                                     AS on_time_share
    FROM v_departures_clean
    WHERE has_realtime AND NOT cancelled
    GROUP BY line_name, line_product
    HAVING COUNT(*) >= 50
)
SELECT
    RANK() OVER (ORDER BY avg_delay_min DESC)                 AS rank,
    line_name,
    line_product,
    departures,
    ROUND(avg_delay_min::numeric, 2)                          AS avg_delay_min,
    ROUND(100 * on_time_share::numeric, 1)                    AS on_time_pct
FROM line_stats
ORDER BY rank
LIMIT 15;


-- 5) Worst 10 stations by on-time %
WITH station_stats AS (
    SELECT
        station_name,
        COUNT(*)                                              AS departures,
        AVG(on_time::int)                                     AS on_time_share,
        AVG(delay_seconds) / 60.0                             AS avg_delay_min
    FROM v_departures_clean
    WHERE has_realtime AND NOT cancelled
    GROUP BY station_name
)
SELECT
    station_name,
    departures,
    ROUND(100 * on_time_share::numeric, 1)                    AS on_time_pct,
    ROUND(avg_delay_min::numeric, 2)                          AS avg_delay_min
FROM station_stats
ORDER BY on_time_share
LIMIT 10;


-- 6) Delays by hour of day (rush hours vs. quiet hours)
SELECT
    hour_of_day,
    COUNT(*)                                                  AS departures,
    ROUND(100.0 * AVG(on_time::int), 1)                       AS on_time_pct,
    ROUND(AVG(delay_seconds) / 60.0, 2)                       AS avg_delay_min
FROM v_departures_clean
WHERE has_realtime AND NOT cancelled
GROUP BY hour_of_day
ORDER BY hour_of_day;


-- 7) Weekday vs. weekend by mode
SELECT
    CASE WHEN weekday <= 5 THEN 'Weekday' ELSE 'Weekend' END  AS day_type,
    line_product,
    COUNT(*)                                                  AS departures,
    ROUND(100.0 * AVG(on_time::int), 1)                       AS on_time_pct
FROM v_departures_clean
WHERE has_realtime AND NOT cancelled
GROUP BY 1, 2
ORDER BY 2, 1;


-- 8) Does delay build up along the day? Running average delay per line for the latest day
WITH last_day AS (
    SELECT * FROM v_departures_clean
    WHERE service_date = (SELECT MAX(service_date) FROM v_departures_clean)
      AND has_realtime AND NOT cancelled
)
SELECT
    line_name,
    planned_time AT TIME ZONE 'Europe/Berlin'                 AS planned_local,
    delay_minutes,
    ROUND(AVG(delay_minutes) OVER (
        PARTITION BY line_name
        ORDER BY planned_time
        ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
    ), 2)                                                     AS rolling_avg_delay_10
FROM last_day
WHERE line_name IN ('S7', 'U2', 'M10')   -- pick the lines you want to look at
ORDER BY line_name, planned_time;
