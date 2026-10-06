# Berlin Public Transport Delay Analytics

[![CI](https://github.com/Anant2580/berlin-transit-delays/actions/workflows/ci.yml/badge.svg)](https://github.com/Anant2580/berlin-transit-delays/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)

**How punctual is Berlin's public transport really?** This project collects live departure data for 25 Berlin stations every 5 minutes, stores planned and actual departure times in PostgreSQL, and turns them into KPIs such as on-time percentage, average delay per line and the most delayed stations.

Berlin's GTFS timetable files only contain the *planned* schedule, not real delays. To measure punctuality, the project collects realtime data itself and builds up its own history.

## Highlights

- **End-to-end data pipeline**: realtime REST API → Python collector → PostgreSQL → SQL analytics, running continuously in Docker.
- **Idempotent loading**: each departure is seen several times while it approaches; an `INSERT … ON CONFLICT` upsert updates one row per departure instead of creating duplicates.
- **Fault tolerance**: timeouts, retries with exponential backoff, and a circuit breaker keep the collector running when the public API is slow or down.
- **Data quality built in**: every collector run is logged, so gaps, failed requests and realtime coverage can be measured.
- **SQL analytics**: KPI queries with CTEs, window functions (`LAG`, `RANK`, rolling averages) and percentiles.
- **Tested**: unit tests for parsing, plus tests of the upsert logic, view and KPI queries against a real PostgreSQL in GitHub Actions.

## Architecture

```mermaid
flowchart LR
    API["VBB realtime API<br/>v6.vbb.transport.rest"] -->|every 5 min, 25 stations| C["Collector<br/>Python 3.12"]
    C -->|upsert| DB[("PostgreSQL 16<br/>departures · stations · collector_runs")]
    DB --> V["View v_departures_clean"]
    V --> K["SQL KPIs<br/>sql/kpis.sql"]
    V --> E["CSV export<br/>Tableau / pandas"]
    K -.-> D["Dashboard<br/>(planned)"]
```

- **Collector** (`collector/collector.py`): every 5 minutes it asks the API for the next 10 minutes of departures at each station. Each sighting updates the same row, so the stored delay is the latest realtime value before the vehicle leaves.
- **Reliability**: requests time out after 30 s and are retried with exponential backoff (2, 4, 8 s). A station that still fails is skipped and retried in the next cycle. After 3 failed stations in a row the collector assumes the API is down and waits for the next cycle (circuit breaker) instead of crashing.
- **Database** (`db/init/01_schema.sql`): one row per departure (trip × station × planned time), a `stations` table, and a `collector_runs` table that logs every run.
- **Analysis** (`sql/kpis.sql`): KPI queries built on the view `v_departures_clean`.

## First results (preliminary)

First day of data: **7,064 departures** at 25 stations, 5–6 October 2026 (afternoon and night only). 91% of departures had realtime data and 3.5% were cancelled.

| Mode | Departures* | On time | Avg. delay |
|---|---:|---:|---:|
| U-Bahn | 1,217 | 97.5% | 0.3 min |
| S-Bahn | 1,879 | 94.3% | 0.8 min |
| Tram | 486 | 87.0% | 0.4 min |
| Bus | 2,684 | 80.7% | 1.0 min |
| Regional train | 162 | 74.1% | 3.1 min |

\*With realtime data, not cancelled. On time = between 1 minute early and 3 minutes late.

First observations:
- Rail-bound modes with their own tracks (U-Bahn, S-Bahn) are the most punctual; buses in mixed traffic the least.
- The least punctual "line" was a **rail replacement bus for the S7** (38% on time). Replacement buses need to be analysed separately from regular bus lines.
- Long-distance trains (ICE/IC) appear at some stations with delays of up to 84 minutes. They are few but distort averages, so they should be reported separately.

These numbers come from less than one day of data and will be updated once a few weeks of continuous data are collected.

## KPI definitions

| KPI | Definition |
|---|---|
| On time | Departure between 1 minute early and 3 minutes late |
| Delay | Actual minus planned departure time, from the last realtime update |
| Cancellation rate | Cancelled departures / all departures |
| Realtime coverage | Share of departures that have realtime data |

Departures without realtime data are excluded from delay and on-time KPIs but counted in the data-quality checks.

## Design decisions and limitations

- **Delay = last realtime forecast.** The collector sees a departure up to 10 minutes before it leaves, so the stored delay is the last forecast before departure, not a measured final delay.
- **Collecting it myself instead of using GTFS.** Public GTFS feeds only have planned times; realtime history is not published, so it has to be recorded.
- **Gaps are expected and visible.** When the computer is off or the API is down, nothing is collected. `collector_runs` makes these gaps measurable (KPI query 2), so they can be excluded from analysis rather than silently biasing it.
- **Station selection.** 25 stations chosen to cover hubs, U-Bahn, tram and outer areas; the list is easy to change in `collector/stations.txt`.

## Tech stack

Python 3.12 (requests, psycopg 3) · PostgreSQL 16 · Docker Compose · SQL (CTEs, window functions) · pytest · ruff · GitHub Actions

## Run it yourself

The only thing you need is **Docker Desktop**.

1. Create your settings file:
   ```bash
   cp .env.example .env
   ```
2. Start the database and the collector:
   ```bash
   docker compose up -d --build
   ```
3. Watch the collector work (Ctrl+C stops watching, the collector keeps running):
   ```bash
   docker compose logs -f collector
   ```
   Log times are in UTC. You should see one line per station (`Station 'S Ostkreuz' -> ...`) and then `Run done: ...` every 5 minutes.

The collector only runs while the computer is awake. On a Mac, keep it plugged in and run `caffeinate -s` in a second terminal.

### Using the data

```bash
# Count collected departures
docker compose exec db psql -U transit -d transit -c "SELECT COUNT(*) FROM departures;"

# Run all KPI queries
docker compose exec db psql -U transit -d transit -f /sql/kpis.sql

# Export clean data to exports/departures.csv (for Tableau, Excel or pandas)
docker compose exec db psql -U transit -d transit -c "\copy (SELECT * FROM v_departures_clean) TO '/exports/departures.csv' WITH CSV HEADER"
```

You can also connect any SQL tool (DBeaver, pgAdmin, Python) to `localhost:5432`, database `transit`, user `transit`, password from your `.env`.

### Stopping

```bash
docker compose stop        # pause, data is kept
docker compose up -d       # continue later
```
`docker compose down -v` deletes the database volume and **all collected data**.

### Tests

```bash
pip install -r collector/requirements.txt pytest ruff
ruff check .
pytest                     # database tests are skipped unless TEST_DATABASE_URL is set
```

## Project structure

```
├── collector/
│   ├── collector.py        # fetches departures and upserts them into PostgreSQL
│   ├── stations.txt        # the 25 stations to track
│   ├── requirements.txt
│   └── Dockerfile
├── db/init/01_schema.sql   # tables, indexes and the analysis view
├── sql/kpis.sql            # KPI queries
├── tests/                  # pytest: parsing, upsert logic, view, KPI queries
├── .github/workflows/      # CI: lint + tests against PostgreSQL 16
└── docker-compose.yml
```

## Roadmap

- [x] Step 1: Collect live departures into PostgreSQL
- [ ] Step 2: Schedule the collection with Apache Airflow
- [ ] Step 3: Interactive dashboard (on-time %, worst lines and stations, delays by hour)
- [ ] Step 4: Predict delays with machine learning and explain the predictions with SHAP

## Data source

Realtime data from VBB (Verkehrsverbund Berlin-Brandenburg), accessed through the open API [v6.vbb.transport.rest](https://v6.vbb.transport.rest). No API key needed; the collector stays well below the 100 requests/minute limit.

## Author

**Anant Sharma** – M.Sc. Data & Knowledge Engineering student at Otto-von-Guericke-Universität Magdeburg, interested in data engineering, machine learning and AI.

Released under the [MIT License](LICENSE).
