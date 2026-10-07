#!/usr/bin/env bash
# Update and (re)start the whole project with one command.
#
#   sudo ./deploy/update.sh            # local / private: database, collector, dashboard
#   sudo ./deploy/update.sh --public   # cloud server: also serves the dashboard over HTTPS
#
# What it does:
#   1. downloads the latest code from GitHub
#   2. makes sure .env has a password for the read-only dashboard user
#   3. with --public: sets the dashboard address to <server-ip>.sslip.io and turns on HTTPS
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

echo "==> Getting the latest code"
git pull --ff-only

[ -f .env ] || cp .env.example .env
chmod 600 .env

if [ -z "$(get_env DASHBOARD_DB_PASSWORD)" ]; then
  echo "==> Creating a password for the read-only dashboard user"
  set_env DASHBOARD_DB_PASSWORD "$(openssl rand -hex 16)"
fi

if [ "${1:-}" = "--public" ]; then
  ip="$(curl -fsS https://api.ipify.org)"
  host="${ip//./-}.sslip.io"   # sslip.io turns 1-2-3-4.sslip.io into the address 1.2.3.4 (free, no signup)
  echo "==> Public dashboard address: https://$host"
  set_env DASHBOARD_HOST "$host"
  set_env COMPOSE_PROFILES public
fi

echo "==> Starting the database"
docker compose up -d --wait db

echo "==> Applying database migrations"
for migration in db/migrations/*.sql; do
  echo "    $migration"
  docker compose exec -T db psql -q -v ON_ERROR_STOP=1 \
    -v dash_pw="$(get_env DASHBOARD_DB_PASSWORD)" \
    -U "$(get_env POSTGRES_USER)" -d "$(get_env POSTGRES_DB)" < "$migration"
done

echo "==> Building and starting all services"
docker compose up -d --build --remove-orphans

docker compose ps --format 'table {{.Service}}\t{{.Status}}'
echo "==> Done"
