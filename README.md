# Berlin Public Transport Delay Analytics

How punctual is Berlin's public transport really? This project collects live departure data for 25 Berlin stations, stores planned and actual departure times in PostgreSQL, and turns them into KPIs such as on-time percentage, average delay per line and the most delayed stations.

Berlin's GTFS timetable files only contain the *planned* schedule, not real delays. To measure punctuality, this project collects realtime data itself, every few minutes, and builds up its own history.

## How it works

```
VBB realtime API ──► collector (Python, every 5 min) ──► PostgreSQL ──► SQL KPIs ──► dashboard
```

- **Collector** (`collector/collector.py`): every 5 minutes it asks the VBB API for the next 10 minutes of departures at each station. A departure is seen several times while it approaches; each sighting updates the same row, so the stored delay is the latest realtime value before the vehicle leaves.
- **Reliability**: the public API is sometimes slow or down. Requests time out after 30 s and are retried with exponential backoff (2, 4, 8 s). A station that still fails is skipped and retried in the next cycle, and after 3 failed stations in a row the collector assumes the API is down and waits for the next cycle (circuit breaker) instead of crashing. Every run is logged in `collector_runs`, so gaps in the data stay visible.
- **Database** (`db/init/01_schema.sql`): one row per departure (trip × station × planned time), a `stations` table, and a `collector_runs` table that logs every run for data-quality checks.
- **Analysis** (`sql/kpis.sql`): KPI queries built on the view `v_departures_clean`, using CTEs and window functions.

Both services run in Docker, so the only thing you need to install is Docker Desktop.

## KPI definitions

| KPI | Definition |
|---|---|
| On time | Departure between 1 minute early and 3 minutes late |
| Delay | Actual minus planned departure time, from the last realtime update |
| Cancellation rate | Cancelled departures / all departures |
| Realtime coverage | Share of departures that have realtime data |

Departures without realtime data are excluded from delay and on-time KPIs but counted in the data-quality checks.

## Setup (macOS / Windows / Linux)

1. Install and open **Docker Desktop**.
2. Open a terminal in this folder and create your settings file:
   ```bash
   cp .env.example .env
   ```
3. Start the database and the collector:
   ```bash
   docker compose up -d --build
   ```
4. Watch the collector work (Ctrl+C to stop watching, the collector keeps running):
   ```bash
   docker compose logs -f collector
   ```
   Log times are in UTC (Berlin time minus 1 or 2 hours). You should see one line per station (`Station 'S Ostkreuz' -> ...`) and then `Run done: ...` every 5 minutes. Check that each station name matched the stop you meant; if not, edit `collector/stations.txt` and run step 3 again.

**Keep your laptop awake.** The collector only runs while the computer is on and awake. On a Mac, keep it plugged in with the lid open and run this in a second terminal window:
```bash
caffeinate -s
```

## Using the data

Count what you have collected so far:
```bash
docker compose exec db psql -U transit -d transit -c "SELECT COUNT(*) FROM departures;"
```

Run all KPI queries:
```bash
docker compose exec db psql -U transit -d transit -f /sql/kpis.sql
```

Export the clean data to CSV (for Tableau Public, Excel or pandas). The file appears in the `exports/` folder:
```bash
docker compose exec db psql -U transit -d transit -c "\copy (SELECT * FROM v_departures_clean) TO '/exports/departures.csv' WITH CSV HEADER"
```

You can also connect any SQL tool (DBeaver, pgAdmin, Python) to `localhost:5432`, database `transit`, user `transit`, password from your `.env`.

## Stopping

```bash
docker compose stop        # pause, data is kept
docker compose up -d       # continue later
```
`docker compose down -v` deletes the database volume and **all collected data**.

## Roadmap

- [x] Step 1: Collect live departures into PostgreSQL
- [ ] Step 2: Schedule the collection with Apache Airflow
- [ ] Step 3: Interactive dashboard (on-time %, worst lines and stations, delays by hour)
- [ ] Step 4: Predict delays with machine learning and explain the predictions with SHAP

## Data source

Realtime data from VBB (Verkehrsverbund Berlin-Brandenburg), accessed through the open API [v6.vbb.transport.rest](https://v6.vbb.transport.rest). No API key needed; the collector stays well below the 100 requests/minute limit.
