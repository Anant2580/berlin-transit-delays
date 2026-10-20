"""
DAG: compare_apis  (temporary experiment)

Is our own copy of the VBB API (service vbb-api in docker-compose.yml) more reliable than
the shared public server? Every 5 minutes this DAG asks both APIs for the departures of the
same 5 randomly chosen stations, at the same time, one request each and no retries, and
stores every outcome in the api_probe table.

Results: sql/api_probe_report.sql. Pause or delete this DAG once the decision is made.
The real collection (collect_departures) is not affected.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pendulum
import psycopg
from airflow.sdk import dag, task

DATABASE_URL = os.environ["DATABASE_URL"]
COLLECTOR_DIR = os.getenv("COLLECTOR_DIR", "/opt/airflow/collector")
APIS = {
    "public": os.getenv("PUBLIC_API_BASE", "https://v6.vbb.transport.rest"),
    "own": os.getenv("OWN_API_BASE", "http://vbb-api:3000"),
}
STATIONS_PER_ROUND = 5


@dag(
    dag_id="compare_apis",
    description="Experiment: public VBB API vs. our own instance, same requests at the same time",
    schedule="*/5 * * * *",
    start_date=pendulum.datetime(2026, 10, 20, tz="Europe/Berlin"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["berlin-transit", "experiment"],
)
def compare_apis():

    @task(execution_timeout=timedelta(minutes=4))
    def probe() -> dict:
        if COLLECTOR_DIR not in sys.path:
            sys.path.insert(0, COLLECTOR_DIR)
        from api_probe import probe_api  # collector/api_probe.py, mounted into the containers

        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT station_id FROM stations ORDER BY random() LIMIT %s", (STATIONS_PER_ROUND,))
                station_ids = [row[0] for row in cur.fetchall()]

            # Both APIs in parallel, so they are compared under the same conditions
            probed_at = datetime.now(timezone.utc)
            with ThreadPoolExecutor(max_workers=len(APIS)) as pool:
                futures = {name: pool.submit(probe_api, base, station_ids) for name, base in APIS.items()}
                results = {name: future.result() for name, future in futures.items()}

            with conn.cursor() as cur:
                for name, rows in results.items():
                    cur.executemany(
                        "INSERT INTO api_probe"
                        " (probed_at, api, station_id, ok, http_status, error, seconds, departures)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(probed_at, name, *row) for row in rows],
                    )

        # A failing API is a result, not an error: the task stays green either way
        summary = {name: f"{sum(r[1] for r in rows)}/{len(rows)} ok" for name, rows in results.items()}
        print(f"Probe results: {summary}")
        return summary

    probe()


compare_apis()
