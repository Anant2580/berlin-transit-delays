"""
Tests for the delay model (ml/): no data leakage in the features, an honest time split,
the full training pipeline on synthetic data, and the database round trip.
The database tests are skipped unless TEST_DATABASE_URL is set (GitHub Actions sets it).
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
import pytest

from ml.features import ALL_FEATURES, add_lag_features, load_from_db, prepare
from ml.train import run, time_split, write_to_db

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 10, 5, 0, 0)   # a Monday, UTC


def make_raw(rows):
    """Build a raw frame like the CSV export from (minutes after START, line, delay_seconds) tuples."""
    return pd.DataFrame([{
        "trip_id": f"trip-{i}", "station_id": "900100003", "line_name": line, "line_product": "bus",
        "mode": "Bus", "direction": "Somewhere", "planned_utc": START + timedelta(minutes=m),
        "delay_seconds": delay, "cancelled": "f", "has_realtime": "t",
    } for i, (m, line, delay) in enumerate(rows)])


def test_prepare_labels_late_and_drops_unusable_departures():
    raw = make_raw([(0, "M1", 0), (5, "M1", 181), (10, "M1", 180)])
    raw.loc[2, "cancelled"] = "t"                       # cancelled: no delay to learn from
    raw.loc[3] = raw.loc[0]
    raw.loc[3, ["trip_id", "has_realtime"]] = ["trip-x", "f"]   # no realtime data
    df = prepare(raw)
    assert list(df["late"]) == [0, 1]                   # > 180 s is late, 180 s exactly is not
    assert df["planned"].dt.tz is not None


def test_lag_features_only_use_departures_at_least_30_minutes_earlier():
    # Line M1: one 20-minute delay at minute 0, then punctual departures every 5 minutes
    raw = make_raw([(0, "M1", 1200)] + [(m, "M1", 0) for m in range(5, 125, 5)])
    df = add_lag_features(prepare(raw)).set_index(
        prepare(raw)["planned"].sub(pd.Timestamp(START, tz="UTC")).dt.total_seconds().div(60).astype(int))

    # 25 minutes later the big delay is still "too recent" to be known for sure: not used
    assert df.loc[25, "line_n"] == 0 and np.isnan(df.loc[25, "line_delay"])
    # 30 minutes later it is used ...
    assert df.loc[30, "line_delay"] > 0
    # ... and 95 minutes later it has dropped out of the 60-minute window again
    assert df.loc[95, "line_delay"] == 0


def test_time_split_keeps_the_last_full_week_for_testing():
    raw = make_raw([(m, "M1", 0) for m in range(0, 60 * 24 * 22, 60)])   # 22 days, hourly
    train, val, test = time_split(prepare(raw))
    assert train["planned"].max() < val["planned"].min()
    assert val["planned"].max() < test["planned"].min()
    assert test["planned"].dt.tz_convert("Europe/Berlin").dt.normalize().nunique() == 7
    assert val["planned"].dt.tz_convert("Europe/Berlin").dt.normalize().nunique() == 7


def test_run_trains_evaluates_and_explains(tmp_path):
    # 25 days, two lines every 5 minutes. M2 is often late in the afternoon, and lateness
    # persists for a while, so both the timetable and the recent-delay features carry signal.
    rng = np.random.default_rng(0)
    rows, state = [], {"M1": 0.0, "M2": 0.0}
    for m in range(0, 60 * 24 * 25, 5):
        hour = (m // 60) % 24
        for line in ("M1", "M2"):
            base = 300 if (line == "M2" and 14 <= hour <= 18) else 0
            state[line] = 0.95 * state[line] + rng.normal(0, 30)
            rows.append((m, line, base + state[line] + rng.normal(0, 60)))
    results = run(make_raw(rows), tmp_path)

    models = results["models"]
    assert set(models) == {"base_rate", "history_line_hour", "lightgbm_schedule", "lightgbm_full"}
    assert models["base_rate"]["roc_auc"] == 0.5
    assert models["lightgbm_full"]["roc_auc"] > 0.75
    assert len(results["importance"]) == len(ALL_FEATURES)
    for name in ("delay_model.txt", "categories.json", "results.json"):
        assert (tmp_path / name).exists()
    assert json.loads((tmp_path / "results.json").read_text())["n_test"] == results["n_test"]


# ---------------------------------------------------------------------------
# Database round trip
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
        cur.execute("DROP VIEW IF EXISTS v_departures_clean")
        cur.execute("DROP TABLE IF EXISTS ml_feature_importance, ml_model_metrics, ml_model_runs, "
                    "departures, collector_runs, stations, daily_line_stats, data_quality_checks")
        for f in ["db/init/01_schema.sql", "db/migrations/002_modes_and_coverage.sql",
                  "db/migrations/006_coverage_needs_data.sql", "db/migrations/008_ml_results.sql"]:
            cur.execute((ROOT / f).read_text())
        cur.execute("INSERT INTO stations (station_id, name) VALUES ('1', 'S Ostkreuz Bhf (Berlin)')")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        planned = now - timedelta(hours=3)
        cur.execute("INSERT INTO collector_runs (started_at, finished_at, stations_ok, stations_failed, "
                    "departures_upserted) VALUES (%s, %s, 25, 0, 2)", (planned - timedelta(minutes=5), planned))
        for trip, delay in (("A", 0), ("B", 240)):
            cur.execute("""INSERT INTO departures (trip_id, station_id, planned_time, actual_time, delay_seconds,
                                cancelled, line_name, line_product, first_seen_at, last_seen_at)
                           VALUES (%s, '1', %s, %s, %s, FALSE, 'S7', 'suburban', %s, %s)""",
                        (trip, planned, planned + timedelta(seconds=delay), delay, planned, planned))
    connection.commit()
    yield connection
    connection.close()


def test_load_from_db_returns_the_export_columns(conn):
    raw = load_from_db(conn, datetime.now(timezone.utc) - timedelta(days=1))
    assert sorted(raw["trip_id"]) == ["A", "B"]
    df = prepare(raw)
    assert list(df.sort_values("trip_id")["late"]) == [0, 1]


def test_write_to_db_stores_metrics_and_importance(conn):
    metrics = {"roc_auc": 0.85, "pr_auc": 0.41, "brier": 0.06, "precision_top10": 0.4, "recall_top10": 0.49}
    results = {
        "trained_at": "2026-11-10T04:30:00+00:00", "train_from": "2026-10-07 00:00:00+00:00",
        "test_from": "2026-11-02 23:00:00+00:00", "test_to": "2026-11-09 22:59:00+00:00",
        "n_train": 100, "n_test": 50, "late_rate_test": 0.083, "best_iteration": 200,
        "models": {"history_line_hour": metrics, "lightgbm_full": metrics},
        "importance": [{"rank": 1, "feature": "line_delay", "label": "Line delay", "mean_abs_shap": 0.46}],
    }
    run_id = write_to_db(conn, results)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ml_model_metrics WHERE run_id = %s", (run_id,))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT feature FROM ml_feature_importance WHERE run_id = %s", (run_id,))
        assert cur.fetchall() == [("line_delay",)]
