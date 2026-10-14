"""
DAG: daily_quality_and_stats

Every night at 03:15 Berlin time, for the previous day:
  1. quality_checks   - is the data complete enough to trust? Results are stored in the
                        data_quality_checks table; the task fails (red in the UI) if a check fails.
  2. daily_line_stats - aggregates the day into one row per line (departures, on-time %,
                        average and 90th-percentile delay). This table feeds trend charts
                        and the delay-prediction model planned for step 4.

Both tasks are independent, so the statistics are built even when a quality check fails;
the failed check makes the problem visible instead of hiding it.

To (re)process a specific day, trigger the DAG manually with the "day" parameter (YYYY-MM-DD).
"""

import os
from datetime import date, timedelta
from pathlib import Path

import pendulum
import psycopg
from airflow.sdk import Param, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException

BERLIN = pendulum.timezone("Europe/Berlin")
DATABASE_URL = os.environ["DATABASE_URL"]                      # the transit database
SQL_DIR = Path(os.getenv("SQL_DIR", "/opt/airflow/sql")) / "daily"

# check name -> (metric from quality_metrics.sql, comparison, threshold)
QUALITY_CHECKS = {
    "collector_runs":        ("collector_runs", ">=", 250),       # 288 expected (one every 5 min)
    "station_success_pct":   ("station_success_pct", ">=", 95),   # share of station requests that worked
    "longest_gap_min":       ("longest_gap_min", "<=", 15),       # no long time without data (outage)
    "realtime_coverage_pct": ("realtime_coverage_pct", ">=", 85), # departures with live delay data
    "departures":            ("departures", ">=", 10000),         # a normal day has far more
}


def target_day() -> date:
    """The day to process: the 'day' parameter if given, otherwise yesterday in Berlin time."""
    context = get_current_context()
    if context["params"].get("day"):
        return date.fromisoformat(context["params"]["day"])
    run_time = context.get("logical_date") or context["dag_run"].run_after
    return (pendulum.instance(run_time).in_timezone(BERLIN) - timedelta(days=1)).date()


def run_sql_file(conn, name: str, day: date):
    """Run every statement in a .sql file with the day as parameter; return the last result."""
    statements = [s.strip() for s in (SQL_DIR / name).read_text().split(";") if s.strip()]
    rows = None
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement, {"day": day})
            if cur.description:
                columns = [c.name for c in cur.description]
                rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    return rows


@dag(
    dag_id="daily_quality_and_stats",
    description="Nightly data-quality checks and per-line daily statistics",
    schedule="15 3 * * *",
    start_date=pendulum.datetime(2026, 10, 12, tz="Europe/Berlin"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=5)},  # for temporary problems, e.g. DB restarts
    params={"day": Param("", type="string", description="Day to process (YYYY-MM-DD); empty = yesterday")},
    tags=["berlin-transit", "quality", "aggregation"],
)
def daily_quality_and_stats():

    @task
    def quality_checks() -> dict:
        day = target_day()
        with psycopg.connect(DATABASE_URL) as conn:
            metrics = run_sql_file(conn, "quality_metrics.sql", day)[0]
            failed = []
            with conn.cursor() as cur:
                for name, (metric, op, threshold) in QUALITY_CHECKS.items():
                    value = float(metrics[metric] or 0)
                    passed = value >= threshold if op == ">=" else value <= threshold
                    if not passed:
                        failed.append(f"{name}={value:.1f} (expected {op} {threshold})")
                    cur.execute(
                        """
                        INSERT INTO data_quality_checks (service_date, check_name, value, threshold, passed)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (service_date, check_name) DO UPDATE
                        SET value = EXCLUDED.value, threshold = EXCLUDED.threshold,
                            passed = EXCLUDED.passed, checked_at = now()
                        """,
                        (day, name, value, threshold, passed),
                    )
        print(f"Quality metrics for {day}: {metrics}")
        if failed:
            # AirflowFailException = fail without retrying: the data will not change in 5 minutes
            raise AirflowFailException(f"Data-quality checks failed for {day}: " + "; ".join(failed))
        return {"day": day.isoformat(), **{k: float(v or 0) for k, v in metrics.items()}}

    @task
    def daily_line_stats() -> dict:
        day = target_day()
        with psycopg.connect(DATABASE_URL) as conn:
            run_sql_file(conn, "daily_line_stats.sql", day)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM daily_line_stats WHERE service_date = %s", (day,))
                lines = cur.fetchone()[0]
        print(f"Saved statistics for {lines} lines on {day}")
        return {"day": day.isoformat(), "lines": lines}

    # Independent tasks: statistics are built even if a quality check fails
    quality_checks()
    daily_line_stats()


daily_quality_and_stats()
