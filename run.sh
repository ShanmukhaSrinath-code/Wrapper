#!/usr/bin/env bash
# Start the whole application with one command.
#
#   ./run.sh            start everything, apply migrations, print the URLs
#   ./run.sh --stop     stop the stack, keep the data
#   ./run.sh --fresh    wipe volumes and rebuild from scratch
#   ./run.sh --open     also open the main URLs in a browser
#
# Migrations run *inside* the app container, so this needs nothing on your
# machine except Docker -- no Python, no venv, no make.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

COMPOSE=(docker compose -f deploy/docker-compose.yml --profile tracing)
# `--progress quiet` keeps a cached rebuild from burying the output in BuildKit
# layer chatter. Failures still print, because compose exits non-zero.
QUIET=(--progress quiet)
APP=http://localhost:8000
OPEN=0

# Git Bash rewrites paths that look like /app into C:/... before Docker sees
# them, which breaks every `exec` below. This turns that off.
export MSYS_NO_PATHCONV=1

c()  { printf '\033[%sm%s\033[0m' "$1" "$2"; }
ok() { printf '  %s %s\n' "$(c '32' '[OK]')" "$1"; }
no() { printf '  %s %s\n' "$(c '31' '[!!]')" "$1"; }
step(){ printf '\n%s\n' "$(c '1;36' "$1")"; }

case "${1:-}" in
  --stop)
    step "Stopping (data kept)"
    "${COMPOSE[@]}" "${QUIET[@]}" stop
    ok "stopped. ./run.sh to start again."
    exit 0
    ;;
  --fresh)
    step "Removing containers and volumes"
    "${COMPOSE[@]}" "${QUIET[@]}" down -v
    ;;
  --open) OPEN=1 ;;
  "") ;;
  *) echo "unknown option: $1"; sed -n '2,9p' "$0"; exit 2 ;;
esac

# ---------------------------------------------------------------- preflight --
step "1/4  Checking Docker"
if ! docker info >/dev/null 2>&1; then
  no "Docker is not running. Start Docker Desktop and try again."
  exit 1
fi
ok "Docker is running"

# ------------------------------------------------------------------ startup --
step "2/4  Building and starting 10 services"
echo "  building (first run takes a few minutes, later runs are cached)..."
"${COMPOSE[@]}" "${QUIET[@]}" up -d --build
ok "containers up"

# -------------------------------------------------------------------- wait ---
step "3/4  Waiting for the API to become ready"
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$APP/health/ready" || true)
  if [ "$code" = "200" ]; then ok "ready after ${i}s"; break; fi
  if [ "$i" = "90" ]; then
    no "the API did not become ready in 90s"
    echo "     logs:  docker compose -f deploy/docker-compose.yml logs app --tail 40"
    exit 1
  fi
  sleep 1
done

# -------------------------------------------------------------- migrations ---
step "4/4  Applying database migrations"
if out=$("${COMPOSE[@]}" exec -T app alembic upgrade head 2>&1); then
  echo "$out" | grep -E "Running upgrade|already at" | tail -3 || true
  ok "schema up to date"
elif echo "$out" | grep -q "Can't locate revision"; then
  # The volume outlived a branch switch: the database is stamped with a
  # migration that only exists on another branch, so this branch's chain has
  # no path from it. Naming the cause beats printing Alembic's message.
  stuck=$(echo "$out" | grep -oE "revision identified by '[^']+'" | head -1)
  no "the database is at a $stuck this branch does not have"
  cat <<'FIX'

     The volume survived a branch switch. Pick one:

       ./run.sh --fresh                  wipe the volumes and start clean
       git checkout poc/support-tickets  go back to the branch that has it

FIX
  exit 1
else
  no "migrations failed"
  echo "$out" | tail -8
  exit 1
fi

# ------------------------------------------------------------------ report ---
step "Services"
"${COMPOSE[@]}" ps --format '  {{.Name}}\t{{.Status}}' 2>/dev/null | sort || true

step "URLs"
cat <<'URLS'
  API (Swagger)      http://localhost:8000/docs
  API (ReDoc)        http://localhost:8000/redoc
  OpenAPI schema     http://localhost:8000/openapi.json
  Liveness           http://localhost:8000/health/live
  Readiness          http://localhost:8000/health/ready
  Raw metrics        http://localhost:8000/metrics

  Grafana            http://localhost:3001          (anonymous Editor; admin/admin)
  Prometheus         http://localhost:9090
  Loki API           http://localhost:3100
  Tempo API          http://localhost:3200
  MinIO console      http://localhost:9001          (minioadmin/minioadmin)
  MinIO S3 endpoint  http://localhost:9000

  Postgres           localhost:5432                 (appuser/apppassword, db=appdb)
  Redis              localhost:6379

  Architecture UI    docs/architecture.html         (open the file, no server needed)
URLS

if [ "$OPEN" = "1" ]; then
  step "Opening browser tabs"
  for u in "$APP/docs" http://localhost:3001 http://localhost:9090 http://localhost:9001; do
    start "" "$u" >/dev/null 2>&1 || true
  done
fi

step "Next"
cat <<'NEXT'
  ./run.sh --stop     stop, keep the data
  ./run.sh --fresh    wipe volumes and rebuild
  docker compose -f deploy/docker-compose.yml logs -f app     tail the API logs
NEXT
echo
