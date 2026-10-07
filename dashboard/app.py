"""
Live dashboard for the Berlin public transport delay project.

Reads directly from PostgreSQL (read-only user) and refreshes itself every 5 minutes,
so it always shows the latest data the collector has saved.

Run locally:   streamlit run dashboard/app.py
In Docker:     docker compose up -d dashboard   ->  http://localhost:8501
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg
import streamlit as st

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://dashboard@localhost:5432/transit")
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "300"))  # the collector runs every 5 minutes
GITHUB_URL = "https://github.com/Anant2580/berlin-transit-delays"

# Colours (validated categorical/sequential palette, readable in light and dark mode)
BLUE = "#2a78d6"            # single-series marks
ORANGE = "#eb6834"          # second series (weekend)
SEQUENTIAL_BLUE = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]  # light = low, dark = high

TIME_WINDOWS = {
    "Last 24 hours": timedelta(hours=24),
    "Last 7 days": timedelta(days=7),
    "Last 30 days": timedelta(days=30),
    "All data": None,
}
MODES = ["U-Bahn", "S-Bahn", "Tram", "Bus", "Replacement bus", "Regional train", "Long-distance", "Ferry"]

st.set_page_config(page_title="Berlin Transit Punctuality", page_icon="🚆", layout="wide")


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
@st.cache_data(ttl=REFRESH_SECONDS - 30, show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a read-only query and return a DataFrame (cached until the next collector run)."""
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [c.name for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=columns)
    # PostgreSQL NUMERIC values arrive as Python Decimal objects; charts need plain floats
    for col in df.columns:
        if any(isinstance(v, Decimal) for v in df[col].head(20)):
            df[col] = df[col].astype(float)
    return df


# Every analysis query starts from the same filtered set of departures:
# only the chosen time window and modes, and only periods when the collector was running.
BASE = """
WITH base AS (
    SELECT * FROM v_departures_clean
    WHERE planned_time >= %s AND mode = ANY(%s) AND in_coverage
),
valid AS (  -- departures with realtime data that were not cancelled
    SELECT * FROM base WHERE has_realtime AND NOT cancelled
)
"""


def kpis(since, modes):
    return query(BASE + """
        SELECT
            (SELECT COUNT(*) FROM base)                                     AS departures,
            (SELECT 100.0 * AVG(on_time::int) FROM valid)                   AS on_time_pct,
            (SELECT AVG(delay_seconds) / 60.0 FROM valid)                   AS avg_delay_min,
            (SELECT 100.0 * AVG(cancelled::int) FROM base)                  AS cancelled_pct,
            (SELECT 100.0 * AVG((has_realtime OR cancelled)::int) FROM base) AS realtime_pct
    """, (since, modes)).iloc[0]


def by_mode(since, modes):
    return query(BASE + """
        SELECT mode, COUNT(*) AS departures,
               100.0 * AVG(on_time::int) AS on_time_pct,
               AVG(delay_seconds) / 60.0 AS avg_delay_min
        FROM valid GROUP BY mode ORDER BY on_time_pct
    """, (since, modes))


def by_hour(since, modes):
    return query(BASE + """
        SELECT hour_of_day,
               CASE WHEN weekday <= 5 THEN 'Weekday' ELSE 'Weekend' END AS day_type,
               COUNT(*) AS departures,
               100.0 * AVG(on_time::int) AS on_time_pct
        FROM valid GROUP BY 1, 2 HAVING COUNT(*) >= 20 ORDER BY 1
    """, (since, modes))


def worst_lines(since, modes, min_departures):
    return query(BASE + """
        SELECT line_name, mode, COUNT(*) AS departures,
               100.0 * AVG(on_time::int) AS on_time_pct,
               AVG(delay_seconds) / 60.0 AS avg_delay_min
        FROM valid GROUP BY line_name, mode
        HAVING COUNT(*) >= %s
        ORDER BY avg_delay_min DESC LIMIT 12
    """, (since, modes, min_departures))


def stations(since, modes):
    return query(BASE + """
        SELECT station_name, latitude, longitude, COUNT(*) AS departures,
               100.0 * AVG(on_time::int) AS on_time_pct,
               AVG(delay_seconds) / 60.0 AS avg_delay_min
        FROM valid GROUP BY station_name, latitude, longitude
    """, (since, modes))


def latest_delays(modes):
    """The most delayed departures of the last hour, regardless of the time filter."""
    return query("""
        SELECT to_char(planned_time AT TIME ZONE 'Europe/Berlin', 'HH24:MI') AS planned,
               line_name AS line, mode, station_name AS station, direction,
               delay_minutes AS delay_min
        FROM v_departures_clean
        WHERE planned_time >= now() - INTERVAL '60 minutes'
          AND mode = ANY(%s) AND has_realtime AND NOT cancelled AND delay_seconds >= 180
        ORDER BY delay_seconds DESC LIMIT 10
    """, (modes,))


def collector_health():
    return query("""
        WITH runs AS (
            SELECT started_at, stations_ok, stations_failed,
                   started_at - LAG(started_at) OVER (ORDER BY started_at) AS gap
            FROM collector_runs WHERE started_at >= now() - INTERVAL '24 hours'
        )
        SELECT (SELECT MAX(started_at) FROM collector_runs)                AS last_run,
               (SELECT MIN(planned_time) FROM departures)                  AS data_since,
               COUNT(*)                                                     AS runs_24h,
               COALESCE(100.0 * SUM(stations_ok) / NULLIF(SUM(stations_ok + stations_failed), 0), 0)
                                                                            AS success_pct_24h,
               EXTRACT(EPOCH FROM MAX(gap)) / 60.0                          AS longest_gap_min_24h
        FROM runs
    """).iloc[0]


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------
def style(fig: go.Figure, height: int = 340) -> go.Figure:
    """Shared, quiet chart styling: thin grid, no chart junk, hover on every mark."""
    fig.update_layout(
        height=height, margin=dict(l=8, r=16, t=8, b=8),
        hoverlabel=dict(font_size=13), showlegend=False, bargap=0.35,
    )
    fig.update_xaxes(showgrid=True, gridwidth=1, zeroline=False, title=None)
    fig.update_yaxes(showgrid=False, zeroline=False, title=None)
    return fig


def fmt(value, pattern, empty="–"):
    return empty if value is None or pd.isna(value) else pattern.format(value)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.title("How punctual is Berlin's public transport?")
st.caption(
    "Live departures at 25 stations, collected every 5 minutes from the VBB realtime API. "
    "On time = at most 1 minute early and 3 minutes late."
)

# Filters in one row above the charts
f1, f2, _ = st.columns([1, 2, 1])
window_label = f1.selectbox("Time window", list(TIME_WINDOWS), index=1)
selected_modes = f2.multiselect("Transport modes", MODES, default=MODES)


@st.fragment(run_every=REFRESH_SECONDS)
def dashboard(window_label: str, selected_modes: list[str]):
    """Everything inside this function re-runs every 5 minutes, so the page stays live."""
    if not selected_modes:
        st.info("Choose at least one transport mode.")
        return

    window = TIME_WINDOWS[window_label]
    since = datetime.now(timezone.utc) - window if window else datetime(2000, 1, 1, tzinfo=timezone.utc)
    modes = list(selected_modes)

    health = collector_health()
    k = kpis(since, modes)

    # Freshness line: how old is the newest data?
    if pd.notna(health.last_run):
        minutes_ago = (datetime.now(timezone.utc) - health.last_run).total_seconds() / 60
        status = "🟢 Live" if minutes_ago <= 15 else "🟠 Delayed"
        st.markdown(
            f"**{status}** · last collector run {minutes_ago:.0f} min ago · "
            f"data since {health.data_since:%d %b %Y} · page refreshes every {REFRESH_SECONDS // 60} min"
        )

    if not k.departures:
        st.warning("No departures in this time window yet.")
        return

    # KPI row
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Departures analysed", f"{int(k.departures):,}")
    c2.metric("On time", fmt(k.on_time_pct, "{:.1f}%"))
    c3.metric("Average delay", fmt(k.avg_delay_min, "{:.1f} min"))
    c4.metric("Cancelled", fmt(k.cancelled_pct, "{:.1f}%"))
    c5.metric("Realtime coverage", fmt(k.realtime_pct, "{:.0f}%"))

    st.divider()
    left, right = st.columns(2)

    # On-time % by mode (horizontal bars, sorted)
    with left:
        st.subheader("On time by transport mode")
        df = by_mode(since, modes)
        if df.empty:
            st.info("Not enough data yet.")
        else:
            fig = px.bar(
                df, x="on_time_pct", y="mode", orientation="h", text="on_time_pct",
                custom_data=["departures", "avg_delay_min"],
                color_discrete_sequence=[BLUE],
            )
            fig.update_traces(
                texttemplate="%{text:.1f}%", textposition="outside", cliponaxis=False,
                marker_cornerradius=4,
                hovertemplate="<b>%{y}</b><br>On time: %{x:.1f}%<br>"
                              "Avg. delay: %{customdata[1]:.1f} min<br>%{customdata[0]:,} departures<extra></extra>",
            )
            fig.update_xaxes(range=[0, 105], ticksuffix="%")
            st.plotly_chart(style(fig), width="stretch")

    # On-time % by hour of day, weekday vs weekend
    with right:
        st.subheader("On time by hour of day")
        df = by_hour(since, modes)
        if df.empty:
            st.info("Not enough data yet.")
        else:
            # One row per hour 0-23 for each day type, so hours without data show as gaps, not lines
            full = pd.MultiIndex.from_product([range(24), df.day_type.unique()], names=["hour_of_day", "day_type"])
            df = df.set_index(["hour_of_day", "day_type"]).reindex(full).reset_index()
            fig = px.line(
                df, x="hour_of_day", y="on_time_pct", color="day_type", markers=True,
                custom_data=["departures"],
                color_discrete_map={"Weekday": BLUE, "Weekend": ORANGE},
            )
            fig.update_traces(
                line_width=2, marker_size=8, connectgaps=False,
                hovertemplate="<b>%{x}:00</b> · %{fullData.name}<br>On time: %{y:.1f}%<br>"
                              "%{customdata[0]:,} departures<extra></extra>",
            )
            fig.update_xaxes(range=[-0.5, 23.5], dtick=3, ticksuffix=":00")
            fig.update_yaxes(ticksuffix="%", showgrid=True)
            fig = style(fig)
            if df.day_type.nunique() > 1:
                fig.update_layout(showlegend=True, legend=dict(orientation="h", y=1.08, x=0, title=None))
            st.plotly_chart(fig, width="stretch")

    left, right = st.columns(2)

    # Worst lines by average delay
    with left:
        st.subheader("Lines with the highest average delay")
        min_dep = 50 if window_label != "Last 24 hours" else 20
        df = worst_lines(since, modes, min_dep)
        if df.empty:
            st.info("Not enough data yet.")
        else:
            df = df.iloc[::-1]  # largest at the top
            df["label"] = df.line_name + " (" + df["mode"] + ")"
            fig = px.bar(
                df, x="avg_delay_min", y="label", orientation="h", text="avg_delay_min",
                custom_data=["departures", "on_time_pct"], color_discrete_sequence=[BLUE],
            )
            fig.update_traces(
                texttemplate="%{text:.1f} min", textposition="outside", cliponaxis=False,
                marker_cornerradius=4,
                hovertemplate="<b>%{y}</b><br>Avg. delay: %{x:.1f} min<br>On time: %{customdata[1]:.1f}%<br>"
                              "%{customdata[0]:,} departures<extra></extra>",
            )
            fig.update_xaxes(ticksuffix=" min")
            st.plotly_chart(style(fig, height=380), width="stretch")
            st.caption(f"Lines with at least {min_dep} departures in the selected window.")

    # Map of stations, coloured by average delay
    with right:
        st.subheader("Stations")
        df = stations(since, modes)
        if df.empty:
            st.info("Not enough data yet.")
        else:
            fig = px.scatter_map(
                df, lat="latitude", lon="longitude", size="departures", color="avg_delay_min",
                color_continuous_scale=SEQUENTIAL_BLUE, size_max=22, zoom=10.2,
                center={"lat": 52.515, "lon": 13.40}, map_style="carto-positron",
                custom_data=["station_name", "on_time_pct", "avg_delay_min", "departures"],
            )
            fig.update_traces(
                hovertemplate="<b>%{customdata[0]}</b><br>On time: %{customdata[1]:.1f}%<br>"
                              "Avg. delay: %{customdata[2]:.1f} min<br>%{customdata[3]:,} departures<extra></extra>",
            )
            fig.update_layout(
                height=380, margin=dict(l=0, r=0, t=0, b=0),
                coloraxis_colorbar=dict(title="Avg. delay<br>(min)", thickness=12),
            )
            st.plotly_chart(fig, width="stretch")
            st.caption("Bubble size = number of departures; darker = higher average delay.")

    # Live table
    st.subheader("Most delayed departures in the last hour")
    df = latest_delays(modes)
    if df.empty:
        st.caption("No departures delayed by 3 minutes or more in the last hour.")
    else:
        st.dataframe(
            df, hide_index=True, width="stretch",
            column_config={"delay_min": st.column_config.NumberColumn("delay", format="%.0f min")},
        )

    # Data quality
    with st.expander("Data quality and method"):
        q1, q2, q3 = st.columns(3)
        q1.metric("Collector runs (24 h)", f"{int(health.runs_24h)}", help="Expected: 288 (one every 5 minutes)")
        q2.metric("Successful station requests (24 h)", fmt(health.success_pct_24h, "{:.1f}%"))
        q3.metric("Longest gap between runs (24 h)", fmt(health.longest_gap_min_24h, "{:.0f} min"))
        st.markdown(
            "- **Delay** is the last realtime forecast seen 0–10 minutes before departure.\n"
            "- **Replacement buses** (e.g. a bus running as \"S7\") and **long-distance trains** are "
            "shown as their own modes so they don't distort regular bus and regional train figures.\n"
            "- Departures planned while the collector was not running are excluded, because only "
            "late departures would be seen afterwards.\n"
            f"- Source code and documentation: [GitHub]({GITHUB_URL})"
        )


dashboard(window_label, selected_modes)
