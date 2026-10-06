"""
Tests for the collector.

- Parsing tests run anywhere (no database, no network).
- Database tests run against a real PostgreSQL and are skipped if none is reachable.
  Locally:  TEST_DATABASE_URL=postgresql://transit:transit@localhost:5432/transit_test pytest
  In CI:    GitHub Actions starts a PostgreSQL 16 service container (see .github/workflows/ci.yml).
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
import collector  # noqa: E402

BERLIN = timezone(timedelta(hours=2))
SEEN_AT = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def make_departure(trip_id="T1", planned="2026-10-05T12:39:00+02:00", delay=60, cancelled=None, **extra):
    """Build one departure in the format the VBB API returns (v6.vbb.transport.rest)."""
    planned_dt = datetime.fromisoformat(planned)
    when = None if cancelled or delay is None else (planned_dt + timedelta(seconds=delay)).isoformat()
    dep = {
        "tripId": trip_id,
        "plannedWhen": planned,
        "when": when,
        "delay": delay,
        "cancelled": cancelled,
        "platform": "4",
        "plannedPlatform": "4",
        "direction": "S Wartenberg (Berlin)",
        "line": {"name": "S75", "product": "suburban"},
    }
    dep.update(extra)
    return dep


# ---------------------------------------------------------------------------
# Parsing (no database needed)
# ---------------------------------------------------------------------------
def test_departure_rows_parses_api_payload():
    payload = {"departures": [make_departure()], "realtimeDataUpdatedAt": 0}
    rows = collector.departure_rows("900120003", payload, SEEN_AT)

    assert len(rows) == 1
    row = rows[0]
    assert row["trip_id"] == "T1"
    assert row["station_id"] == "900120003"
    assert row["delay_seconds"] == 60
    assert row["actual_time"] - row["planned_time"] == timedelta(seconds=60)
    assert row["cancelled"] is False  # the API sends null, we store False
    assert row["line_name"] == "S75" and row["line_product"] == "suburban"


def test_departure_rows_removes_duplicates_and_incomplete_rows():
    deps = [
        make_departure("T1"),
        make_departure("T1"),                       # same departure twice in one response
        make_departure(None),                       # no trip id -> cannot be identified
        {"tripId": "T2", "plannedWhen": None},      # no planned time -> cannot be identified
        make_departure("T3", planned="2026-10-05T12:45:00+02:00"),
    ]
    rows = collector.departure_rows("900120003", {"departures": deps}, SEEN_AT)
    assert sorted(r["trip_id"] for r in rows) == ["T1", "T3"]


def test_departure_rows_accepts_plain_list_from_older_api_versions():
    rows = collector.departure_rows("900120003", [make_departure()], SEEN_AT)
    assert len(rows) == 1


def test_cancelled_departure_has_no_actual_time():
    payload = {"departures": [make_departure(delay=None, cancelled=True)]}
    row = collector.departure_rows("900120003", payload, SEEN_AT)[0]
    assert row["cancelled"] is True
    assert row["actual_time"] is None and row["delay_seconds"] is None


# ---------------------------------------------------------------------------
# Database: upsert logic, view and KPI queries (real PostgreSQL)
# ---------------------------------------------------------------------------
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def conn():
    """Fresh schema for every test, created from db/init/01_schema.sql."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
    except psycopg.OperationalError:
        pytest.skip("no PostgreSQL reachable")
    with connection.cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS v_departures_clean")
        cur.execute("DROP TABLE IF EXISTS departures, collector_runs, stations")
        cur.execute((ROOT / "db/init/01_schema.sql").read_text())
        cur.execute("INSERT INTO stations (station_id, name) VALUES ('900120003', 'S Ostkreuz Bhf (Berlin)')")
    connection.commit()
    yield connection
    connection.close()


def save(conn, departure):
    """Run one departure through the same parsing and upsert code the collector uses."""
    rows = collector.departure_rows("900120003", {"departures": [departure]}, datetime.now(timezone.utc))
    with conn.cursor() as cur:
        cur.executemany(collector.UPSERT_SQL, rows)
    conn.commit()


def stored(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT delay_seconds, cancelled, actual_time IS NULL, times_seen FROM departures")
        return cur.fetchone()


def test_upsert_keeps_latest_realtime_delay(conn):
    save(conn, make_departure(delay=60))
    save(conn, make_departure(delay=240))
    assert stored(conn) == (240, False, False, 2)


def test_upsert_keeps_previous_delay_when_realtime_disappears(conn):
    save(conn, make_departure(delay=120))
    save(conn, make_departure(delay=None))
    assert stored(conn) == (120, False, False, 2)


def test_upsert_cancellation_clears_delay(conn):
    save(conn, make_departure(delay=30))
    save(conn, make_departure(delay=None, cancelled=True))
    assert stored(conn) == (None, True, True, 2)


@pytest.mark.parametrize("delay, expected", [(-60, True), (180, True), (-61, False), (181, False)])
def test_view_on_time_boundaries(conn, delay, expected):
    """On time = at most 1 minute early and at most 3 minutes late."""
    planned = (datetime.now(BERLIN) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    save(conn, make_departure(planned=planned, delay=delay))
    with conn.cursor() as cur:
        cur.execute("SELECT on_time FROM v_departures_clean")
        assert cur.fetchone()[0] is expected


def test_kpi_queries_run(conn):
    """Every query in sql/kpis.sql must run without errors on the current schema."""
    planned = (datetime.now(BERLIN) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    save(conn, make_departure(planned=planned, delay=90))
    queries = [q for q in (ROOT / "sql/kpis.sql").read_text().split(";") if "SELECT" in q]
    assert len(queries) == 8
    with conn.cursor() as cur:
        for query in queries:
            cur.execute(query)
            cur.fetchall()
