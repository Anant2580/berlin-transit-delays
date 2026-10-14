"""
Tests for the SQL run by the nightly Airflow DAG (sql/daily/*.sql) against a real PostgreSQL.
Skipped unless TEST_DATABASE_URL is set (GitHub Actions sets it, see .github/workflows/ci.yml).
"""

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
BERLIN = timezone(timedelta(hours=2))  # CEST; the test day lies in summer time
DAY = date(2026, 10, 7)


def run_sql_file(conn, name, day):
    """Same approach as the DAG: run each statement with the day as parameter, return last result."""
    rows = None
    with conn.cursor() as cur:
        for statement in [s.strip() for s in (ROOT / "sql/daily" / name).read_text().split(";") if s.strip()]:
            cur.execute(statement, {"day": day})
            if cur.description:
                rows = cur.fetchall()
    return rows


@pytest.fixture
def conn():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
    except psycopg.OperationalError:
        pytest.skip("no PostgreSQL reachable")
    with connection.cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS v_departures_clean")
        cur.execute("DROP TABLE IF EXISTS departures, collector_runs, stations, daily_line_stats, data_quality_checks")
        schema_files = ["db/init/01_schema.sql", "db/migrations/002_modes_and_coverage.sql",
                        "db/migrations/004_daily_tables.sql", "db/migrations/006_coverage_needs_data.sql"]
        for f in schema_files:
            cur.execute((ROOT / f).read_text())
        cur.execute("INSERT INTO stations (station_id, name) VALUES ('1', 'S Ostkreuz Bhf (Berlin)')")

        # Collector ran every 5 minutes from 08:00 to 10:00 Berlin time, one run had 2 failed stations
        start = datetime(2026, 10, 7, 8, 0, tzinfo=BERLIN)
        for i in range(25):
            t = start + timedelta(minutes=5 * i)
            ok, failed = (23, 2) if i == 3 else (25, 0)
            cur.execute(
                "INSERT INTO collector_runs"
                " (started_at, finished_at, stations_ok, stations_failed, departures_upserted)"
                " VALUES (%s, %s, %s, %s, 10)", (t, t + timedelta(seconds=40), ok, failed),
            )
        # S7 departures: on time, 2 min late, 6 min late, cancelled; one U2 departure without realtime
        deps = [
            ("A", "S7", "suburban", 9, 0, False),
            ("B", "S7", "suburban", 9, 120, False),
            ("C", "S7", "suburban", 9, 360, False),
            ("D", "S7", "suburban", 9, None, True),
            ("E", "U2", "subway", 9, None, False),
        ]
        for trip, line, product, hour, delay, cancelled in deps:
            planned = datetime(2026, 10, 7, hour, 10, tzinfo=BERLIN)
            cur.execute(
                """INSERT INTO departures (trip_id, station_id, planned_time, actual_time, delay_seconds, cancelled,
                                           line_name, line_product, first_seen_at, last_seen_at)
                   VALUES (%s, '1', %s, %s, %s, %s, %s, %s, %s, %s)""",
                (trip, planned, planned + timedelta(seconds=delay) if delay is not None else None,
                 delay, cancelled, line, product, planned, planned),
            )
    connection.commit()
    yield connection
    connection.close()


def test_daily_line_stats(conn):
    run_sql_file(conn, "daily_line_stats.sql", DAY)
    with conn.cursor() as cur:
        cur.execute("""SELECT line_name, mode, departures, cancelled, on_time_pct, avg_delay_seconds
                       FROM daily_line_stats WHERE service_date = %s ORDER BY line_name""", (DAY,))
        rows = cur.fetchall()
    s7, u2 = rows
    # 3 valid S7 departures (cancelled one counted separately); 2 of 3 on time (0 s and 120 s)
    assert s7[:4] == ("S7", "S-Bahn", 3, 1)
    assert float(s7[4]) == pytest.approx(66.67, abs=0.01)
    assert float(s7[5]) == pytest.approx(160.0)  # (0 + 120 + 360) / 3
    # U2 had no realtime data: kept as a line with 0 valid departures and no on-time value
    assert u2[:4] == ("U2", "U-Bahn", 0, 0) and u2[4] is None


def test_daily_line_stats_rerun_replaces_the_day(conn):
    run_sql_file(conn, "daily_line_stats.sql", DAY)
    run_sql_file(conn, "daily_line_stats.sql", DAY)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM daily_line_stats WHERE service_date = %s", (DAY,))
        assert cur.fetchone()[0] == 2


def test_quality_metrics(conn):
    runs, without_data, success_pct, longest_gap, departures, coverage = run_sql_file(
        conn, "quality_metrics.sql", DAY)[0]
    assert runs == 25 and without_data == 0
    assert float(success_pct) == pytest.approx(100 * (24 * 25 + 23) / (25 * 25))
    assert float(longest_gap) == pytest.approx(5.0)
    assert departures == 5
    assert float(coverage) == pytest.approx(80.0)  # 4 of 5 have realtime data or are cancelled


def test_quality_metrics_treat_api_outage_as_gap(conn):
    """Runs without data (API outage from 08:30 to 08:45) still exist, but the gap is measured."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE collector_runs SET stations_ok = 0, stations_failed = 25 WHERE started_at BETWEEN %s AND %s",
            (datetime(2026, 10, 7, 8, 30, tzinfo=BERLIN), datetime(2026, 10, 7, 8, 45, tzinfo=BERLIN)),
        )
    runs, without_data, _, longest_gap, _, _ = run_sql_file(conn, "quality_metrics.sql", DAY)[0]
    assert runs == 25 and without_data == 4
    assert float(longest_gap) == pytest.approx(25.0)  # last data 08:25, next data 08:50
