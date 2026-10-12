# Apache Airflow with the one extra library the DAGs need (psycopg 3 for PostgreSQL).
# Versions follow Airflow's official constraints file, so they always match this Airflow release.
FROM apache/airflow:3.3.2-python3.12

RUN pip install --no-cache-dir "apache-airflow==3.3.2" "psycopg[binary]" \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt"
