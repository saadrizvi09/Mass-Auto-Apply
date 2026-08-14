#!/bin/bash
# AutoApply Cloud development launcher with source-code reload.
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
SAAS_PYTHON="$PROJECT_DIR/.venv-saas/bin/python"
SAAS_ENV_FILE="$PROJECT_DIR/.env.saas.local"
cd "$PROJECT_DIR"

if [[ ! -x "$SAAS_PYTHON" ]]; then
    echo "Run START_HERE.command once before using the development launcher."
    read -r -p "Press Return to close..." _
    exit 1
fi

if [[ ! -f "$SAAS_ENV_FILE" ]]; then
    echo "Missing .env.saas.local. Copy .env.example and add the development credentials."
    read -r -p "Press Return to close..." _
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$SAAS_ENV_FILE"
set +a

echo
echo "AutoApply Cloud development server: http://127.0.0.1:8000"
echo "Starting the local discovery/browser worker too."
echo "Press Control-C to stop both processes."
echo

WORKER_PID=""
API_PID=""
CLEANING_UP=0
cleanup() {
    if [[ "$CLEANING_UP" -eq 1 ]]; then
        return
    fi
    CLEANING_UP=1
    for pid in "$API_PID" "$WORKER_PID"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    for pid in "$API_PID" "$WORKER_PID"; do
        if [[ -n "$pid" ]]; then
            wait "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT
trap 'exit 130' INT TERM

"$SAAS_PYTHON" -m worker.main &
WORKER_PID=$!
(sleep 2; open "http://127.0.0.1:8000" >/dev/null 2>&1 || true) &
"$SAAS_PYTHON" -m uvicorn app.saas_main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --reload \
    --reload-dir "$PROJECT_DIR/app" &
API_PID=$!

# Bash 3.2 ships with macOS and has no `wait -n`, so supervise both children
# portably. If either process exits, stop the other instead of leaving an API
# that accepts background work with no worker available to process it.
while true; do
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        if wait "$WORKER_PID"; then WORKER_STATUS=0; else WORKER_STATUS=$?; fi
        echo
        echo "The local worker stopped (status $WORKER_STATUS). Stopping the API so queued work cannot appear stuck."
        exit "$WORKER_STATUS"
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        if wait "$API_PID"; then API_STATUS=0; else API_STATUS=$?; fi
        exit "$API_STATUS"
    fi
    sleep 1
done
