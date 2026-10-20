-- Migration 007: results of the API comparison experiment (Airflow DAG compare_apis).
-- Safe to run more than once. Applied by deploy/update.sh.
-- One row per request: which API, which station, did it work, how long did it take.

CREATE TABLE IF NOT EXISTS api_probe (
    probe_id     BIGSERIAL    PRIMARY KEY,
    probed_at    TIMESTAMPTZ  NOT NULL,   -- start of the round; same value for both APIs
    api          TEXT         NOT NULL,   -- 'public' (v6.vbb.transport.rest) or 'own' (service vbb-api)
    station_id   TEXT         NOT NULL,
    ok           BOOLEAN      NOT NULL,   -- HTTP 200 with valid JSON
    http_status  INTEGER,                 -- NULL = no HTTP answer at all (timeout, connection refused)
    error        TEXT,                    -- e.g. ReadTimeout, ConnectionError
    seconds      NUMERIC(6, 2) NOT NULL,  -- response time
    departures   INTEGER                  -- departures returned (NULL if not ok)
);

CREATE INDEX IF NOT EXISTS idx_api_probe_api_time ON api_probe (api, probed_at);
