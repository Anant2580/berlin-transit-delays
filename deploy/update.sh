#!/usr/bin/env bash
# Update and (re)start the whole project with one command.
#
#   sudo ./deploy/update.sh            # local / private: database, collector, dashboard
#   sudo ./deploy/update.sh --public   # cloud server: Airflow runs the collection, dashboard over HTTPS
#
# What it does:
#   1. downloads the latest code from GitHub
#   2. makes sure .env has passwords/keys for the dashboard user and Airflow
#   3. with --public: sets the dashboard address to <server-ip>.sslip.io, turns on HTTPS
#      and lets Airflow schedule the collection instead of the standalone collector
#   4. applies the database migrations in db/migrations (safe to run again)
#   5. rebuilds and restarts the containers
# Collected data is never deleted.

set -euo pipefail
cd "$(dirname "$0")/.."

# Read and write KEY=value lines in .env
get_env() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }
set_env() {
  if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}

# Get the latest code, then restart this script so the NEW version of it runs
# (bash would otherwise keep executing the old copy it already started reading)
if [ -z "${UPDATE_SH_REEXEC:-}" ]; then
  echo "==> Getting the latest code"
  git pull --ff-only
  UPDATE_SH_REEXEC=1 exec "$0" "$@"
fi

[ -f .env ] || cp .env.example .env
chmod 600 .env

# Create secrets once; later runs keep them
ensure_secret() {
  if [ -z "$(get_env "$1")" ]; then
    echo "==> Creating $1"
    set_env "$1" "$2"
  fi
}
ensure_secret DASHBOARD_DB_PASSWORD "$(openssl rand -hex 16)"
ensure_secret AIRFLOW_DB_PASSWORD "$(openssl rand -hex 16)"
ensure_secret AIRFLOW_ADMIN_PASSWORD "$(openssl rand -hex 8)"
ensure_secret AIRFLOW_JWT_SECRET "$(openssl rand -hex 32)"
ensure_secret AIRFLOW_FERNET_KEY "$(openssl rand -base64 32 | tr '+/' '-_')"   # Fernet = url-safe base64 of 32 bytes
# Without a profile only the database and dashboard would start; run the standalone collector by default
[ -n "$(get_env COMPOSE_PROFILES)" ] || set_env COMPOSE_PROFILES collector

if [ "${1:-}" = "--public" ]; then
  ip="$(curl -fsS https://api.ipify.org)"
  host="${ip//./-}.sslip.io"   # sslip.io turns 1-2-3-4.sslip.io into the address 1.2.3.4 (free, no signup)
  echo "==> Public dashboard address: https://$host"
  set_env DASHBOARD_HOST "$host"
  set_env COMPOSE_PROFILES public,airflow
fi

echo "==> Starting the database"
docker compose up -d --wait db

echo "==> Applying database migrations"
for migration in db/migrations/*.sql; do
  echo "    $migration"
  docker compose exec -T -e PGOPTIONS="-c client_min_messages=warning" db psql -q -v ON_ERROR_STOP=1 \
    -v dash_pw="$(get_env DASHBOARD_DB_PASSWORD)" -v airflow_pw="$(get_env AIRFLOW_DB_PASSWORD)" \
    -U "$(get_env POSTGRES_USER)" -d "$(get_env POSTGRES_DB)" < "$migration"
done

echo "==> Building and starting all services"
docker compose up -d --build --remove-orphans

# Only one collector at a time: when Airflow schedules the collection, remove the standalone one
if [[ ",$(get_env COMPOSE_PROFILES)," == *",airflow,"* ]]; then
  docker compose rm -sf collector > /dev/null 2>&1 || true
  echo "==> Airflow schedules the collection. Open the Airflow UI with an SSH tunnel:"
  echo "      ssh -L 8080:localhost:8080 ubuntu@<server-ip>   then http://localhost:8080"
  echo "      user: admin   password: $(get_env AIRFLOW_ADMIN_PASSWORD)"
fi

docker compose ps --format 'table {{.Service}}\t{{.Status}}'
echo "==> Done"
