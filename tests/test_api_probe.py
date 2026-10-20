"""
Tests for the API comparison experiment: collector/api_probe.py and sql/api_probe_report.sql.
The probe tests use a fake HTTP session (no network). The report test needs PostgreSQL
and is skipped unless TEST_DATABASE_URL is set (as in tests/test_collector.py).
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
from api_probe import probe_api  # noqa: E402


class FakeResponse:
    def __init__(self, status, payload=None, invalid_json=False):
        self.status_code = status
        self._payload = payload
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        return self._payload


class FakeSession:
    """Returns (or raises) the prepared outcome for each station id, in order."""
    def __init__(self, outcomes):
        self.headers = {}
        self.outcomes = list(outcomes)
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_probe_records_success_errors_and_timeouts():
    session = FakeSession([
        FakeResponse(200, {"departures": [{}, {}, {}]}),   # works, 3 departures
        FakeResponse(503),                                  # server error
        requests.exceptions.ReadTimeout("read timed out"),  # no answer at all
        FakeResponse(200, invalid_json=True),               # broken answer
        FakeResponse(200, [{}]),                            # older API format: plain list
    ])
    results = probe_api("http://api", ["A", "B", "C", "D", "E"], session=session, pause=0)

    assert session.urls[0] == "http://api/stops/A/departures"
    station, ok, status, error, seconds, departures = results[0]
    assert (station, ok, status, error, departures) == ("A", True, 200, None, 3)
    assert seconds >= 0
    assert results[1][1:4] == (False, 503, None)
    assert results[2][1:4] == (False, None, "ReadTimeout")
    assert results[3][1:4] == (False, 200, "InvalidJSON")
    assert results[4][1] is True and results[4][5] == 1


# ---------------------------------------------------------------------------
# Report SQL against a real PostgreSQL
# ---------------------------------------------------------------------------
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def conn():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        connection = psycopg.connect(TEST_DATABASE_URL)
    except psycopg.OperationalError:
        pytest.skip("no PostgreSQL reachable")
    with connection.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS api_probe")
        cur.execute((ROOT / "db/migrations/007_api_probe.sql").read_text())
        # 6 rounds, 2 stations each. The public API answers nothing in rounds 2-4,
        # our own API answers everything.
        start = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        for i in range(6):
            t = start + timedelta(minutes=5 * i)
            for station in ("S1", "S2"):
                public_ok = i not in (2, 3, 4)
                cur.execute(
                    "INSERT INTO api_probe (probed_at, api, station_id, ok, http_status, error, seconds, departures)"
                    " VALUES (%s, 'public', %s, %s, %s, %s, %s, %s)",
                    (t, station, public_ok, 200 if public_ok else None, None if public_ok else "ReadTimeout",
                     1.0 if public_ok else 30.0, 20 if public_ok else None),
                )
                cur.execute(
                    "INSERT INTO api_probe (probed_at, api, station_id, ok, http_status, error, seconds, departures)"
                    " VALUES (%s, 'own', %s, TRUE, 200, NULL, 0.5, 20)", (t, station),
                )
    connection.commit()
    yield connection
    connection.close()


def test_api_probe_report(conn):
    queries = [q for q in (ROOT / "sql/api_probe_report.sql").read_text().split(";") if "SELECT" in q]
    assert len(queries) == 4
    results = []
    with conn.cursor() as cur:
        for query in queries:
            cur.execute(query)
            results.append(cur.fetchall())
    reliability, rounds, gaps, problems = results

    own, public = reliability  # ordered by api name
    assert own[:3] == ("own", 12, 100) and public[:3] == ("public", 12, 50)
    assert float(public[3]) == pytest.approx(1.0)              # median response time of successful requests
    assert rounds == [("own", 6, 0, 0), ("public", 6, 3, 50)]
    assert [(api, float(gap)) for api, gap in gaps] == [("own", 5.0), ("public", 20.0)]  # 10:05 -> 10:25
    assert problems == [("public", "ReadTimeout", 6)]
