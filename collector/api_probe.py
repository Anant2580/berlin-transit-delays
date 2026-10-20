"""
API comparison probe (experiment).

Question: is our own copy of the VBB API more reliable than the shared public server
v6.vbb.transport.rest? Both run the same open-source code (github.com/derhuerst/vbb-rest)
and return the same JSON, so the collector could switch by changing API_BASE.

probe_api() asks one API for the departures of a few stations: one request per station,
no retries, the same timeout as the collector. The Airflow DAG compare_apis calls it for
both APIs at the same time and stores every result in the api_probe table.
"""

import time

import requests

TIMEOUT_SECONDS = 30          # same as the collector's REQUEST_TIMEOUT_SECONDS
PAUSE_SECONDS = 1.0           # be polite to the public server
PARAMS = {"duration": 10, "remarks": "false", "linesOfStops": "false", "pretty": "false"}


def probe_api(base_url, station_ids, session=None, pause=PAUSE_SECONDS):
    """
    Request /stops/{id}/departures once per station.
    Returns one tuple per station: (station_id, ok, http_status, error, seconds, departures)
      ok          True if the API answered 200 with valid JSON
      http_status None if there was no HTTP answer at all (timeout, connection refused)
      error       exception class name (e.g. "ReadTimeout"), None if there was an HTTP answer
      departures  number of departures returned, None if not ok
    """
    session = session or requests.Session()
    session.headers["User-Agent"] = "berlin-transit-delays (student portfolio project)"
    results = []
    for i, station_id in enumerate(station_ids):
        if i:
            time.sleep(pause)
        started = time.monotonic()
        status = error = departures = None
        try:
            resp = session.get(f"{base_url}/stops/{station_id}/departures", params=PARAMS, timeout=TIMEOUT_SECONDS)
            status = resp.status_code
            if status == 200:
                payload = resp.json()
                # v6 returns {"departures": [...]}, older versions a plain list
                deps = payload.get("departures", []) if isinstance(payload, dict) else payload
                departures = len(deps)
        except requests.exceptions.JSONDecodeError:  # 200 but not valid JSON
            error = "InvalidJSON"
        except requests.RequestException as exc:     # timeout, connection error, ...
            error = exc.__class__.__name__
        seconds = round(time.monotonic() - started, 2)
        results.append((station_id, departures is not None, status, error, seconds, departures))
    return results
