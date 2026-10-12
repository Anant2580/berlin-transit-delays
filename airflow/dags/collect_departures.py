"""
DAG: collect_departures

Every 5 minutes: fetch the next 10 minutes of departures for all tracked stations from the
VBB realtime API and upsert them into PostgreSQL.

The collection logic lives in collector/collector.py and is reused unchanged; Airflow adds
scheduling, a run history, logs per run, timeouts and alerting through failed runs.
"""

import os
import sys
from datetime import timedelta

import pendulum
from airflow.sdk import dag, task

# collector/ is mounted into the Airflow containers (see docker-compose.yml)
COLLECTOR_DIR = os.getenv("COLLECTOR_DIR", "/opt/airflow/collector")


@dag(
    dag_id="collect_departures",
    description="Fetch live departures for 25 Berlin stations and upsert them into PostgreSQL",
    schedule="*/5 * * * *",
    start_date=pendulum.datetime(2026, 10, 12, tz="Europe/Berlin"),
    catchup=False,              # a missed run cannot be repeated later: the API only shows the future
    max_active_runs=1,          # never two collections at the same time
    dagrun_timeout=timedelta(minutes=12),
    default_args={"retries": 0},  # no retry: the next run starts 5 minutes later anyway
    tags=["berlin-transit", "ingestion"],
)
def collect_departures():

    @task(execution_timeout=timedelta(minutes=10))
    def collect() -> dict:
        """Resolve station ids (cached) and save one round of departures."""
        if COLLECTOR_DIR not in sys.path:
            sys.path.insert(0, COLLECTOR_DIR)
        import collector  # imported here so the DAG file itself loads fast

        conn = collector.connect_with_retry()
        try:
            stations, missing = collector.resolve_stations(conn)
            if not stations:
                raise RuntimeError("No stations could be resolved - is the VBB API reachable?")
            if missing:
                print(f"{missing} station name(s) could not be looked up yet; they are retried next run")
            result = collector.run_once(conn, stations)
        finally:
            conn.close()

        # Mark the run as failed (red in the Airflow UI) if nothing could be collected at all
        if result["stations_ok"] == 0:
            raise RuntimeError(f"All station requests failed: {result}")
        return result  # shown in the UI as the task's return value (XCom)

    collect()


collect_departures()
