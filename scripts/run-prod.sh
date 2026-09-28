#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_root"

if [[ ${DJANGO_DEBUG:-0} != 0 ]]; then
  echo "scripts/run-prod.sh requires DJANGO_DEBUG=0" >&2
  exit 1
fi
export DJANGO_DEBUG=0
export FLEETLINE_REQUIRE_STRONG_SECRET=1

# Import settings before doing build or migration work. Non-debug settings reject
# a missing, placeholder, or weak DJANGO_SECRET_KEY.
(
  cd backend
  ../.venv/bin/python -c 'import fleetops.settings'
)

npm run build
(
  cd backend
  ../.venv/bin/python manage.py migrate --noinput
  ../.venv/bin/python manage.py collectstatic --noinput
)

worker_pid=
cleanup() {
  if [[ -n "$worker_pid" ]]; then
    kill "$worker_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

(cd backend && ../.venv/bin/python manage.py runworker) &
worker_pid=$!
cd backend
exec ../.venv/bin/gunicorn fleetops.wsgi:application --bind "${APP_BIND:-127.0.0.1:8088}" --workers "${WEB_WORKERS:-2}" --access-logfile - --error-logfile -
