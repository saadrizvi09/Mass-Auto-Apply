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
echo "Press Control-C to stop it."
echo
(sleep 2; open "http://127.0.0.1:8000" >/dev/null 2>&1 || true) &
exec "$SAAS_PYTHON" -m uvicorn app.saas_main:app --host 127.0.0.1 --port 8000 --reload
