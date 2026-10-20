-- Report for the API comparison experiment (Airflow DAG compare_apis, table api_probe).
-- Run on the server:
--   docker compose exec -T db psql -U transit -d transit -f /sql/api_probe_report.sql


-- 1) Reliability and speed per API
SELECT
    api,
    COUNT(*)                                                                      AS requests,
    ROUND(100.0 * AVG(ok::int), 1)                                                AS success_pct,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY seconds) FILTER (WHERE ok))::numeric, 2)  AS median_s,
    ROUND((PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY seconds) FILTER (WHERE ok))::numeric, 2) AS p95_s,
    ROUND(AVG(departures) FILTER (WHERE ok), 1)                                   AS avg_departures,
    MIN(probed_at) AT TIME ZONE 'Europe/Berlin'                                   AS since
FROM api_probe
GROUP BY api
ORDER BY api;


-- 2) Rounds in which an API answered nothing at all: the collector would have got no data
WITH rounds AS (
    SELECT api, probed_at, BOOL_OR(ok) AS any_ok
    FROM api_probe
    GROUP BY api, probed_at
)
SELECT
    api,
    COUNT(*)                                       AS rounds,
    COUNT(*) FILTER (WHERE NOT any_ok)             AS rounds_without_data,
    ROUND(100.0 * AVG((NOT any_ok)::int), 1)       AS pct_rounds_without_data
FROM rounds
GROUP BY api
ORDER BY api;


-- 3) Longest time without a single successful answer, per API
WITH rounds AS (
    SELECT api, probed_at, BOOL_OR(ok) AS any_ok
    FROM api_probe
    GROUP BY api, probed_at
),
ok_rounds AS (
    SELECT api, probed_at,
           probed_at - LAG(probed_at) OVER (PARTITION BY api ORDER BY probed_at) AS gap
    FROM rounds
    WHERE any_ok
)
SELECT api, ROUND(EXTRACT(EPOCH FROM MAX(gap)) / 60.0, 1) AS longest_gap_min
FROM ok_rounds
GROUP BY api
ORDER BY api;


-- 4) What went wrong, per API
SELECT
    api,
    COALESCE(error, 'HTTP ' || http_status)        AS problem,
    COUNT(*)                                       AS requests
FROM api_probe
WHERE NOT ok
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
