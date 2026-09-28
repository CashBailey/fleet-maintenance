#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_root"

suite_mode=complete
if (($# > 0)); then
  if [[ ${FLEETLINE_E2E_PARTIAL:-0} != 1 ]]; then
    echo "Filtered E2E runs are partial evidence. Set FLEETLINE_E2E_PARTIAL=1 explicitly to pass Playwright arguments." >&2
    exit 2
  fi
  suite_mode=partial
elif [[ ${FLEETLINE_E2E_PARTIAL:-0} == 1 ]]; then
  suite_mode=partial
fi

python_bin="$repo_root/.venv/bin/python"
gunicorn_bin="$repo_root/.venv/bin/gunicorn"
playwright_bin="$repo_root/node_modules/.bin/playwright"
for required in "$python_bin" "$gunicorn_bin" "$playwright_bin"; do
  if [[ ! -x "$required" ]]; then
    echo "Missing $required; run make install first." >&2
    exit 2
  fi
done
if ! command -v google-chrome >/dev/null 2>&1; then
  echo "Google Chrome is required for real-browser E2E tests." >&2
  exit 2
fi

mkdir -p "$repo_root/.e2e" "$repo_root/artifacts/e2e"
exec 9>"$repo_root/.e2e/test-e2e.lock"
if ! flock -n 9; then
  echo "Another E2E run is using the shared production build outputs; wait for it to finish." >&2
  exit 2
fi
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
run_dir=$(mktemp -d "$repo_root/.e2e/run.XXXXXX")
artifact_dir=${PLAYWRIGHT_ARTIFACT_DIR:-"$repo_root/artifacts/e2e/$run_id"}
mkdir -p "$artifact_dir/logs"

source "$repo_root/scripts/postgres-test-lib.sh"

free_port() {
  "$python_bin" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

postgres_port=$(free_port)
app_port=$(free_port)
while [[ "$app_port" == "$postgres_port" ]]; do app_port=$(free_port); done
postgres_dir="$run_dir/postgres"
media_dir="$run_dir/media"
app_pid=
worker_pid=
blob_dir="$artifact_dir/blob-reports"
mkdir -p "$blob_dir"

required_specs=(
  accessibility.spec.ts
  api-token-lifecycle.spec.ts
  auth-rbac.spec.ts
  autopi-meter.spec.ts
  barcode.spec.ts
  component-tracking.spec.ts
  concurrency-idempotency.spec.ts
  defect-repair.spec.ts
  document-library.spec.ts
  gatorhub-integration.spec.ts
  immutable-corrections.spec.ts
  inspections.spec.ts
  inventory-count-approval.spec.ts
  inventory-work-order.spec.ts
  offline-sync.spec.ts
  preventive-maintenance.spec.ts
  purchase-requests.spec.ts
  purchasing-receiving.spec.ts
  restart-persistence.setup.spec.ts
  restart-persistence.spec.ts
  search-report-import-export.spec.ts
  smoke.spec.ts
  webhook-administration.spec.ts
  work-order-team-assignment.spec.ts
)

for spec in "${required_specs[@]}"; do
  [[ -f "$repo_root/tests/e2e/$spec" ]] || {
    echo "Required E2E specification is missing: tests/e2e/$spec" >&2
    exit 2
  }
done

if forbidden_markers=$(grep -R -n -E --include='*.spec.ts' \
  '\b(test|describe)(\.[A-Za-z_][A-Za-z0-9_]*)*\.(only|skip|fixme)[[:space:]]*\(' \
  "$repo_root/tests/e2e"); then
  echo "Focused, skipped, or fixme E2E tests are forbidden:" >&2
  echo "$forbidden_markers" >&2
  exit 2
fi

stop_application() {
  if [[ -n "$app_pid" ]]; then
    kill -TERM "$app_pid" 2>/dev/null || true
    wait "$app_pid" 2>/dev/null || true
    app_pid=
  fi
}

start_application() {
  (
    cd backend
    exec "$gunicorn_bin" fleetops.wsgi:application \
      --bind "127.0.0.1:$app_port" --workers 2 --timeout 30 \
      --access-logfile - --error-logfile -
  ) >>"$artifact_dir/logs/application.log" 2>&1 &
  app_pid=$!
}

wait_for_readiness() {
  curl --silent --show-error --fail --retry 60 --retry-delay 1 --retry-connrefused \
    --connect-timeout 1 --retry-max-time 60 --max-time 65 "$E2E_BASE_URL/health/live" >/dev/null
  local deadline=$((SECONDS + 60))
  while ((SECONDS < deadline)); do
    if ! kill -0 "$app_pid" 2>/dev/null || ! kill -0 "$worker_pid" 2>/dev/null; then
      return 1
    fi
    if body=$(curl --silent --show-error --fail --max-time 2 "$E2E_BASE_URL/health/ready" 2>/dev/null) && \
      "$python_bin" -c 'import json,sys; p=json.load(sys.stdin); c=p["checks"]; raise SystemExit(not (p["status"] == "ok" and c.get("database") == c.get("frontend") == c.get("worker") == "ok"))' <<<"$body"; then
      return 0
    fi
  done
  return 1
}

run_phase() {
  local phase=$1
  shift
  PLAYWRIGHT_BLOB_OUTPUT_FILE="$blob_dir/$phase.zip" \
    "$playwright_bin" test --reporter=blob,line "$@"
}

merge_reports() {
  compgen -G "$blob_dir/*.zip" >/dev/null || return 0
  PLAYWRIGHT_HTML_OUTPUT_DIR="$artifact_dir/playwright-report" \
    PLAYWRIGHT_HTML_OPEN=never \
    PLAYWRIGHT_JSON_OUTPUT_FILE="$artifact_dir/results.json" \
    "$playwright_bin" merge-reports --reporter=html,json "$blob_dir" \
    >"$artifact_dir/logs/report-merge.log" 2>&1
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if ! merge_reports; then
    echo "Failed to merge Playwright reports; see $artifact_dir/logs/report-merge.log" >&2
    [[ "$status" == 0 ]] && status=1
  fi
  stop_application
  [[ -n "$worker_pid" ]] && kill -TERM "$worker_pid" 2>/dev/null || true
  [[ -n "$worker_pid" ]] && wait "$worker_pid" 2>/dev/null || true
  if [[ -f "$postgres_dir/postgres.log" ]]; then
    cp "$postgres_dir/postgres.log" "$artifact_dir/logs/postgres.log"
  fi
  fleetline_stop_postgres "$postgres_dir" || true
  case "$run_dir" in
    "$repo_root"/.e2e/run.*) rm -rf -- "$run_dir" ;;
    *) echo "Refusing to remove unexpected run directory: $run_dir" >&2 ;;
  esac
  echo "E2E artifacts: $artifact_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

export DJANGO_SECRET_KEY="e2e-only-secret-key-with-at-least-fifty-characters-000000000000"
export DJANGO_DEBUG=0
export ALLOWED_HOSTS="127.0.0.1,localhost"
export CSRF_TRUSTED_ORIGINS="http://127.0.0.1:$app_port"
export COOKIE_SECURE=0
export DB_CONN_MAX_AGE=0
export MEDIA_ROOT="$media_dir"
export WEBHOOK_ALLOW_HTTP=1
export WEBHOOK_ALLOW_PRIVATE_NETWORKS=1
export E2E_USERNAME=${E2E_USERNAME:-fleet.manager@example.com}
export E2E_PASSWORD=${E2E_PASSWORD:-DemoPass123!}
export E2E_BASE_URL="http://127.0.0.1:$app_port"
export E2E_RESTART_STATE_FILE="$run_dir/restart-persistence.json"
export E2E_RUN_ID="$run_id"
export FLEETLINE_ALLOW_DEMO_SEED=1
export INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD=0.00
export PLAYWRIGHT_ARTIFACT_DIR="$artifact_dir"
export PYTHONUNBUFFERED=1
mkdir -p "$media_dir"

{
  echo "run_id=$run_id"
  echo "suite_mode=$suite_mode"
  echo "base_url=$E2E_BASE_URL"
  if (($# > 0)); then
    printf 'playwright_arguments='
    printf '%q ' "$@"
    printf '\n'
  else
    echo "playwright_arguments=(none)"
  fi
  "$python_bin" --version
  node --version
  "$playwright_bin" --version
  google-chrome --version
} >"$artifact_dir/environment.txt" 2>&1

echo "Discovering the complete mandatory E2E manifest..."
"$playwright_bin" test --list >"$artifact_dir/logs/test-discovery.log" 2>&1
for spec in "${required_specs[@]}"; do
  grep -Fq "$spec:" "$artifact_dir/logs/test-discovery.log" || {
    echo "Required E2E specification was not discovered by Playwright: $spec" >&2
    exit 2
  }
done

echo "Building production frontend..."
npm run build >"$artifact_dir/logs/build.log" 2>&1

echo "Starting isolated PostgreSQL on port $postgres_port..."
fleetline_start_postgres "$postgres_dir" "$postgres_port" fleetline_e2e

echo "Applying migrations and deterministic seed data..."
(
  cd backend
  "$python_bin" manage.py migrate --noinput
  "$python_bin" manage.py seed_demo
  "$python_bin" manage.py collectstatic --noinput
) >"$artifact_dir/logs/setup.log" 2>&1

(
  cd backend
  exec "$python_bin" manage.py runworker --poll 0.1
) >"$artifact_dir/logs/worker.log" 2>&1 &
worker_pid=$!

start_application

echo "Waiting for application, database, frontend, and worker readiness..."
if ! wait_for_readiness; then
  echo "Application did not become ready." >&2
  tail -n 100 "$artifact_dir/logs/application.log" >&2 || true
  tail -n 100 "$artifact_dir/logs/worker.log" >&2 || true
  exit 1
fi

echo "Creating restart-persistence record through the browser..."
run_phase restart-setup tests/e2e/restart-persistence.setup.spec.ts
[[ -s "$E2E_RESTART_STATE_FILE" ]] || { echo "Restart setup did not save its state marker." >&2; exit 1; }

echo "Restarting the production Gunicorn process against the same database..."
stop_application
start_application
if ! wait_for_readiness; then
  echo "Application did not become ready after restart." >&2
  tail -n 100 "$artifact_dir/logs/application.log" >&2 || true
  exit 1
fi

echo "Verifying durable state in a fresh browser context..."
run_phase restart-verification tests/e2e/restart-persistence.spec.ts

echo "Running the $suite_mode remaining Playwright suite against $E2E_BASE_URL..."
run_phase complete-suite --grep-invert @restart-persistence "$@"
kill -0 "$app_pid"
kill -0 "$worker_pid"
