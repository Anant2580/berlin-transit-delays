"""
Berlin public transport delay collector.

Every POLL_INTERVAL_SECONDS this script asks the VBB API for the next few
minutes of departures at each tracked station and saves planned vs. actual
departure times in PostgreSQL. A departure is seen several times while it
approaches; each sighting updates the same row, so the stored delay is the
latest realtime value before the vehicle leaves.

Data source: VBB realtime data via https://v6.vbb.transport.rest
(no API key needed, limit 100 requests/minute).
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import requests

# ---------------------------------------------------------------------------
# Configuration (all can be overridden with environment variables)
# ---------------------------------------------------------------------------
API_BASE = os.getenv("API_BASE", "https://v6.vbb.transport.rest")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://transit:transit@localhost:5432/transit")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 minutes
LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", "10"))           # departures window per request
REQUEST_PAUSE_SECONDS = float(os.getenv("REQUEST_PAUSE_SECONDS", "1.0"))  # stay far below the rate limit
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))  # the public API can be slow
STATIONS_FILE = Path(os.getenv("STATIONS_FILE", Path(__file__).with_name("stations.txt")))
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() == "true"            # handy for testing
# Circuit breaker: after this many failed stations in a row the API is probably down,
# so stop for this cycle and try again at the next one instead of retrying every station
MAX_FAILS_IN_ROW = int(os.getenv("MAX_FAILS_IN_ROW", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("collector")

session = requests.Session()
session.headers["User-Agent"] = "berlin-transit-delays (student portfolio project)"

# Set to True by SIGTERM/SIGINT so the loop can stop cleanly (e.g. `docker compose stop`)
stop_requested = False


def handle_stop(signum, frame):
    global stop_requested
    stop_requested = True
    log.info("Stop signal received, finishing current run...")


signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def api_get(path, params, retries=4):
    """GET a JSON endpoint, retrying on network errors, 429 and 5xx with backoff."""
    url = f"{API_BASE}{path}"
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries:
                raise
            wait = 2 ** attempt  # 2s, 4s, 8s
            log.warning("Request to %s failed (%s), retry %d in %ds", path, exc, attempt, wait)
            time.sleep(wait)


def parse_time(value):
    """Turn an ISO 8601 string from the API into an aware datetime (or None)."""
    return datetime.fromisoformat(value) if value else None


def resolve_stations(conn):
    """
    Read station names from stations.txt, look up their VBB ids once,
    and store them in the stations table. Returns a list of (id, name).
    Already known stations are not looked up again.
    """
    names = [
        line.strip()
        for line in STATIONS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    with conn.cursor() as cur:
        cur.execute("SELECT station_id, name FROM stations")
        known = {row[1]: row[0] for row in cur.fetchall()}
    # Also remember which query each station came from, so restarts skip the lookup
    cache_path = STATIONS_FILE.with_suffix(".cache.json")
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    stations = []
    fails_in_row = 0
    api_down = False
    for query in names:
        if stop_requested:
            break
        if query in cache and cache[query] in known.values():
            station_id = cache[query]
            stations.append((station_id, next(n for n, i in known.items() if i == station_id)))
            continue
        if api_down:
            continue  # keep collecting known stations from the cache, but no more API lookups

        try:
            results = api_get("/locations", {
                "query": query, "results": 1, "addresses": "false", "poi": "false", "pretty": "false",
            })
        except Exception as exc:  # API slow or down: skip this station for now, retry next cycle
            log.warning("Lookup for '%s' failed (%s), will retry next cycle", query, exc.__class__.__name__)
            fails_in_row += 1
            if fails_in_row >= MAX_FAILS_IN_ROW:
                log.warning("API seems down (%d lookups failed in a row), pausing lookups until next cycle", fails_in_row)
                api_down = True
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue
        fails_in_row = 0
        time.sleep(REQUEST_PAUSE_SECONDS)
        match = next((r for r in results if r.get("type") in ("stop", "station")), None)
        if match is None:
            log.warning("No stop found for '%s', skipping it", query)
            continue

        loc = match.get("location") or {}
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO stations (station_id, name, latitude, longitude)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (station_id) DO UPDATE SET name = EXCLUDED.name
                """,
                (match["id"], match["name"], loc.get("latitude"), loc.get("longitude")),
            )
        conn.commit()
        cache[query] = match["id"]
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
        stations.append((match["id"], match["name"]))
        log.info("Station '%s' -> %s (%s)", query, match["name"], match["id"])

    # The same stop can be matched by two queries; keep each id once
    unique = list(dict.fromkeys(stations))
    missing = len(names) - len(stations)
    return unique, missing


def departure_rows(station_id, payload, seen_at):
    """Convert one /departures response into rows for the departures table."""
    # v6 returns {"departures": [...], "realtimeDataUpdatedAt": ...}; older versions a plain list
    deps = payload.get("departures", []) if isinstance(payload, dict) else payload

    rows = {}
    for d in deps:
        planned = parse_time(d.get("plannedWhen"))
        trip_id = d.get("tripId")
        if not trip_id or planned is None:
            continue  # cannot identify this departure, ignore it
        line = d.get("line") or {}
        key = (trip_id, station_id, planned)
        # A dict keyed by the primary key removes duplicates within one response
        rows[key] = {
            "trip_id": trip_id,
            "station_id": station_id,
            "planned_time": planned,
            "actual_time": parse_time(d.get("when")),
            "delay_seconds": d.get("delay"),
            "cancelled": bool(d.get("cancelled", False)),
            "line_name": line.get("name"),
            "line_product": line.get("product"),
            "direction": d.get("direction"),
            "platform": d.get("platform"),
            "planned_platform": d.get("plannedPlatform"),
            "seen_at": seen_at,
        }
    return list(rows.values())


UPSERT_SQL = """
INSERT INTO departures (
    trip_id, station_id, planned_time, actual_time, delay_seconds, cancelled,
    line_name, line_product, direction, platform, planned_platform,
    first_seen_at, last_seen_at, times_seen
) VALUES (
    %(trip_id)s, %(station_id)s, %(planned_time)s, %(actual_time)s, %(delay_seconds)s, %(cancelled)s,
    %(line_name)s, %(line_product)s, %(direction)s, %(platform)s, %(planned_platform)s,
    %(seen_at)s, %(seen_at)s, 1
)
ON CONFLICT (trip_id, station_id, planned_time) DO UPDATE SET
    -- keep the last known realtime value if the newest sighting has none;
    -- a cancelled departure has no actual time and no delay
    actual_time   = CASE
                        WHEN EXCLUDED.cancelled THEN NULL
                        WHEN EXCLUDED.delay_seconds IS NOT NULL THEN EXCLUDED.actual_time
                        ELSE departures.actual_time
                    END,
    delay_seconds = CASE
                        WHEN EXCLUDED.cancelled THEN NULL
                        ELSE COALESCE(EXCLUDED.delay_seconds, departures.delay_seconds)
                    END,
    cancelled     = EXCLUDED.cancelled,
    platform      = COALESCE(EXCLUDED.platform, departures.platform),
    last_seen_at  = EXCLUDED.last_seen_at,
    times_seen    = departures.times_seen + 1
"""


def run_once(conn, stations):
    """Fetch departures for every station once and save them."""
    started = datetime.now(timezone.utc)
    ok = failed = upserted = 0
    fails_in_row = 0

    for station_id, name in stations:
        if stop_requested:
            break
        try:
            payload = api_get(f"/stops/{station_id}/departures", {
                "duration": LOOKAHEAD_MINUTES,
                "remarks": "false",
                "linesOfStops": "false",
                "pretty": "false",
            })
            rows = departure_rows(station_id, payload, datetime.now(timezone.utc))
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, rows)
            conn.commit()
            ok += 1
            upserted += len(rows)
            fails_in_row = 0
        except Exception as exc:  # one bad station must not stop the whole run
            conn.rollback()
            failed += 1
            fails_in_row += 1
            log.warning("Station %s (%s) failed: %s", name, station_id, exc.__class__.__name__)
            if fails_in_row >= MAX_FAILS_IN_ROW:
                log.warning("API seems down (%d stations failed in a row), skipping the rest of this run", fails_in_row)
                break
        time.sleep(REQUEST_PAUSE_SECONDS)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collector_runs (started_at, finished_at, stations_ok, stations_failed, departures_upserted)"
            " VALUES (%s, %s, %s, %s, %s)",
            (started, datetime.now(timezone.utc), ok, failed, upserted),
        )
    conn.commit()
    log.info("Run done: %d stations ok, %d failed, %d departures saved", ok, failed, upserted)


def connect_with_retry():
    """The database container may need a few seconds to start, so keep trying."""
    for attempt in range(1, 31):
        try:
            return psycopg.connect(DATABASE_URL)
        except psycopg.OperationalError as exc:
            log.info("Waiting for database (%s)...", exc.__class__.__name__)
            time.sleep(2)
    raise SystemExit("Could not connect to the database")


def main():
    conn = connect_with_retry()
    stations, missing = [], None

    while not stop_requested:
        cycle_start = time.monotonic()
        try:
            # Look up station ids until every name in stations.txt is resolved.
            # Already resolved stations come from the cache, so this costs no API calls.
            if missing is None or missing > 0:
                stations, missing = resolve_stations(conn)
                log.info("Tracking %d stations (%d still to look up)", len(stations), missing)
            if stations:
                run_once(conn, stations)
            else:
                log.warning("No stations resolved yet (API unreachable?), trying again next cycle")
        except psycopg.OperationalError as exc:
            # Lost the database connection (e.g. container restart): reconnect and carry on
            log.warning("Database connection lost (%s), reconnecting", exc)
            conn = connect_with_retry()
        if RUN_ONCE:
            break
        # Sleep until the next cycle, waking up early if a stop is requested
        while not stop_requested and time.monotonic() - cycle_start < POLL_INTERVAL_SECONDS:
            time.sleep(1)

    conn.close()
    log.info("Collector stopped")


if __name__ == "__main__":
    main()
