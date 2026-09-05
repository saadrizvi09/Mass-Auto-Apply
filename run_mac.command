#!/bin/bash
# Normal-use AutoApply Cloud launcher (no source-code reload).
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
SAAS_PYTHON="$PROJECT_DIR/.venv-saas/bin/python"
SAAS_ENV_FILE="$PROJECT_DIR/.env.saas.local"
cd "$PROJECT_DIR"

if [[ ! -x "$SAAS_PYTHON" ]]; then
    echo "The AutoApply Cloud environment is not installed; starting setup."
    exec bash "$PROJECT_DIR/START_HERE.command"
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
echo "AutoApply Cloud is starting at http://127.0.0.1:8000"
echo "Keep this window open. Press Control-C here to stop the server."
echo

(sleep 2; open "http://127.0.0.1:8000" >/dev/null 2>&1 || true) &
exec "$SAAS_PYTHON" -m uvicorn app.saas_main:app --host 127.0.0.1 --port 8000
