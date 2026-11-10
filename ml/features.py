"""
Feature engineering for the delay model.

The question the model answers: will this departure be late (more than 3 minutes)?
It may only use information that exists BEFORE the departure:

  schedule features   line, mode, station, direction, hour of day, weekday
  recent delays       how late the same line, and the same station, was in the 60 minutes
                      ending 30 minutes before this departure

Why "ending 30 minutes before": the delay we store is the last realtime value seen, which can
still change until a train has actually left. Departures planned at least 30 minutes earlier
have almost always left already (99% of delays are under 14 minutes), so their delays are
final and using them is not peeking into the future (no data leakage).
"""

import pandas as pd

LATE_THRESHOLD_SECONDS = 180            # "late" = more than 3 minutes, same as "on time" on the dashboard
LAG_OFFSET = pd.Timedelta("30min")      # gap between the newest information used and the departure
LAG_WINDOW = pd.Timedelta("60min")      # how much recent history the lag features summarise

CATEGORICAL = ["line_name", "station_id", "mode", "direction"]
SCHEDULE_FEATURES = CATEGORICAL + ["hour", "weekday"]
LAG_FEATURES = ["line_delay", "line_late", "line_n", "station_delay", "station_late", "station_n"]
ALL_FEATURES = SCHEDULE_FEATURES + LAG_FEATURES

# Readable names for charts and the dashboard
FEATURE_LABELS = {
    "line_name": "Line",
    "station_id": "Station",
    "mode": "Transport mode",
    "direction": "Direction",
    "hour": "Hour of day",
    "weekday": "Weekday",
    "line_delay": "Line: avg. delay 30–90 min before",
    "line_late": "Line: share late 30–90 min before",
    "line_n": "Line: departures 30–90 min before",
    "station_delay": "Station: avg. delay 30–90 min before",
    "station_late": "Station: share late 30–90 min before",
    "station_n": "Station: departures 30–90 min before",
}

# The same columns as the CSV export and load_from_db()
EXPORT_SQL = """
SELECT trip_id, station_id, line_name, line_product, mode, direction,
       planned_time AT TIME ZONE 'UTC' AS planned_utc, delay_seconds, cancelled, has_realtime
FROM v_departures_clean
WHERE in_coverage AND planned_time >= %s AND planned_time < now() - interval '1 hour'
"""


def load_from_db(conn, since) -> pd.DataFrame:
    """
    Read departures since a timestamp from PostgreSQL (same columns as the CSV export).
    Uses COPY ... TO STDOUT: PostgreSQL streams CSV straight into pandas, which needs far less
    memory than millions of Python row tuples (this runs on a 6 GB server).
    """
    import io

    from psycopg import sql

    query = sql.SQL("COPY ({}) TO STDOUT WITH (FORMAT csv, HEADER)").format(
        sql.SQL(EXPORT_SQL.replace("%s", "{since}")).format(since=sql.Literal(since))
    )
    buffer = io.BytesIO()
    with conn.cursor() as cur, cur.copy(query) as copy:
        for block in copy:
            buffer.write(block)
    buffer.seek(0)
    return pd.read_csv(buffer)


def prepare(raw: pd.DataFrame) -> pd.DataFrame:
    """Keep departures with a realtime delay that were not cancelled; add time columns and the label."""
    df = raw.copy()
    # Booleans arrive as 't'/'f' from a CSV export and as True/False from the database
    for col in ("cancelled", "has_realtime"):
        if df[col].dtype != bool:
            df[col] = df[col].astype(str).isin(["t", "true", "True"])
    df = df[df["has_realtime"] & ~df["cancelled"]].copy()

    df["planned"] = pd.to_datetime(df["planned_utc"]).dt.tz_localize("UTC")
    local = df["planned"].dt.tz_convert("Europe/Berlin")
    df["hour"] = local.dt.hour
    df["weekday"] = local.dt.dayofweek                 # 0 = Monday
    df["delay_seconds"] = df["delay_seconds"].astype(float)
    df["late"] = (df["delay_seconds"] > LATE_THRESHOLD_SECONDS).astype(int)
    df["station_id"] = df["station_id"].astype(str)
    df["direction"] = df["direction"].fillna("unknown")
    return df.sort_values("planned").reset_index(drop=True)


def _lag_for(df: pd.DataFrame, key: str, prefix: str) -> pd.DataFrame:
    """
    For every departure: average delay, share late and number of departures of the same `key`
    (line or station) in the LAG_WINDOW that ends LAG_OFFSET before its planned time.
    """
    # 1) Rolling 60-minute statistics per key, evaluated at each departure's planned time
    pieces = []
    for value, group in df[[key, "planned", "delay_seconds", "late"]].groupby(key, sort=False, observed=True):
        g = group.set_index("planned").sort_index()
        rolled = pd.DataFrame({
            f"{prefix}_delay": g["delay_seconds"].rolling(LAG_WINDOW).mean(),
            f"{prefix}_late": g["late"].rolling(LAG_WINDOW).mean(),
            f"{prefix}_n": g["late"].rolling(LAG_WINDOW).count(),
        })
        rolled[key] = value
        # Several departures can share a planned time; the last row already covers all of them
        pieces.append(rolled.reset_index().drop_duplicates("planned", keep="last"))
    history = pd.concat(pieces).rename(columns={"planned": "as_of"}).sort_values("as_of")

    # 2) Look up the newest statistics that existed 30 minutes before each departure.
    #    merge_asof(direction="backward") only ever picks rows from the past.
    lookup = pd.DataFrame({key: df[key], "as_of": df["planned"] - LAG_OFFSET, "row": df.index})
    merged = pd.merge_asof(
        lookup.sort_values("as_of"), history, on="as_of", by=key,
        direction="backward", tolerance=LAG_WINDOW,   # older than one window = no recent information
    )
    out = merged.set_index("row").sort_index()[[f"{prefix}_delay", f"{prefix}_late", f"{prefix}_n"]]
    out[f"{prefix}_n"] = out[f"{prefix}_n"].fillna(0)   # no departures in the window = 0 (delay stays unknown)
    return out


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add the recent-delay features for the line and for the station."""
    return df.join(_lag_for(df, "line_name", "line")).join(_lag_for(df, "station_id", "station"))


def to_model_frame(df: pd.DataFrame, categories: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """
    Feature matrix for LightGBM. Categorical columns get a fixed category list (learned on the
    training data), so the same line always maps to the same code at training and prediction time.
    """
    X = df[ALL_FEATURES].copy()
    categories = categories or {c: sorted(X[c].astype(str).unique()) for c in CATEGORICAL}
    for c in CATEGORICAL:
        X[c] = pd.Categorical(X[c].astype(str), categories=categories[c])   # unseen values -> missing
    return X, categories
