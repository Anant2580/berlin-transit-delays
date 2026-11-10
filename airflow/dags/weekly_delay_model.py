"""
DAG: weekly_delay_model

Every Monday at 04:30 Berlin time: retrain the delay model on the last 8 weeks, evaluate it on
the most recent full week (never seen in training) against two baselines, compute the SHAP
feature importance, and store everything in PostgreSQL for the dashboard.

The model file itself is kept in the ml_models volume (/opt/airflow/models).
The ML code lives in ml/ (mounted into the Airflow containers, see docker-compose.yml).
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pendulum
from airflow.sdk import dag, task

ML_PARENT = os.getenv("ML_PARENT_DIR", "/opt/airflow")       # folder that contains ml/
MODEL_DIR = Path(os.getenv("MODEL_DIR", "/opt/airflow/models"))
WEEKS_OF_HISTORY = 8


@dag(
    dag_id="weekly_delay_model",
    description="Retrain, evaluate and explain (SHAP) the model that predicts late departures",
    schedule="30 4 * * 1",
    start_date=pendulum.datetime(2026, 11, 9, tz="Europe/Berlin"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=10)},
    tags=["berlin-transit", "ml"],
)
def weekly_delay_model():

    @task(execution_timeout=timedelta(minutes=45))
    def train_and_evaluate() -> dict:
        if ML_PARENT not in sys.path:
            sys.path.insert(0, ML_PARENT)
        import psycopg

        from ml.features import load_from_db
        from ml.train import run, write_to_db

        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            raw = load_from_db(conn, datetime.now(timezone.utc) - timedelta(weeks=WEEKS_OF_HISTORY))
            results = run(raw, MODEL_DIR)
            run_id = write_to_db(conn, results)

        full, history = results["models"]["lightgbm_full"], results["models"]["history_line_hour"]
        print(f"Run {run_id}: ROC-AUC {full['roc_auc']} (history baseline {history['roc_auc']}), "
              f"catches {full['recall_top10']:.0%} of late departures in the riskiest 10%")
        return {"run_id": run_id, **full}

    train_and_evaluate()


weekly_delay_model()
