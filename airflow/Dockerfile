# Apache Airflow plus the extra libraries the DAGs need:
#   psycopg 3            PostgreSQL access
#   lightgbm, scikit-learn  the weekly delay model (DAG weekly_delay_model)
# Versions follow Airflow's official constraints file where it pins them, so they always match this Airflow release.
FROM apache/airflow:3.3.2-python3.12

RUN pip install --no-cache-dir "apache-airflow==3.3.2" "psycopg[binary]" "scikit-learn" "lightgbm==4.7.0" \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt"
