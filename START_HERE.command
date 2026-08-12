#!/bin/bash
# Double-click this file on an Apple Silicon Mac for AutoApply Cloud setup and launch.
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$PROJECT_DIR"

pause_on_error() {
    status=$?
    if [[ $status -ne 0 ]]; then
        echo
        echo "Setup did not finish. Read the error above, then run START_HERE.command again."
        read -r -p "Press Return to close this window..." _
    fi
}
trap pause_on_error EXIT

bash "$PROJECT_DIR/setup_mac.sh"
exec bash "$PROJECT_DIR/run_mac.command"
