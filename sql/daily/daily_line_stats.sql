-- Aggregate one service day into daily_line_stats (re-running a day replaces its rows).
-- Parameter: %(day)s  (a date, normally yesterday in Berlin time)
DELETE FROM daily_line_stats WHERE service_date = %(day)s;

INSERT INTO daily_line_stats (
    service_date, line_name, mode, departures, cancelled,
    on_time_pct, avg_delay_seconds, p90_delay_seconds
)
SELECT
    service_date,
    line_name,
    mode,
    COUNT(*) FILTER (WHERE has_realtime AND NOT cancelled)                       AS departures,
    COUNT(*) FILTER (WHERE cancelled)                                            AS cancelled,
    ROUND(100.0 * AVG(on_time::int), 2)                                          AS on_time_pct,
    ROUND(AVG(delay_seconds) FILTER (WHERE NOT cancelled), 1)                    AS avg_delay_seconds,
    ROUND((PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY delay_seconds))::numeric, 1) AS p90_delay_seconds
FROM v_departures_clean
WHERE planned_time >= (%(day)s::date)::timestamp AT TIME ZONE 'Europe/Berlin'      -- index-friendly day range
  AND planned_time <  ((%(day)s::date) + 1)::timestamp AT TIME ZONE 'Europe/Berlin'
  AND in_coverage
  AND line_name IS NOT NULL
GROUP BY service_date, line_name, mode;
