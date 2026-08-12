#!/bin/bash
# Bootstrap AutoApply Cloud on macOS/Apple Silicon. Safe to run more than once.
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
SAAS_ENV_FILE="$PROJECT_DIR/.env.saas.local"
cd "$PROJECT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This installer is for macOS."
    exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
    echo "Warning: this launcher is optimized for Apple Silicon."
fi

# Homebrew's normal Apple Silicon location is not always on PATH in a fresh shell.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

if ! command -v brew >/dev/null 2>&1; then
    echo
    echo "Homebrew is needed to install Python, Node.js, and the Supabase CLI."
    read -r -p "Install Homebrew now using its official installer? [Y/n] " answer
    case "${answer:-Y}" in
        [Yy]*) ;;
        *) echo "Cancelled. Install Homebrew, then run this file again."; exit 1 ;;
    esac
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"
fi

if command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.12)"
else
    echo
    echo "Installing Python 3.12..."
    brew install python@3.12
    PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    echo
    echo "Installing Node.js for the browser bundle..."
    brew install node
fi

if ! command -v supabase >/dev/null 2>&1; then
    echo
    echo "Installing the Supabase CLI..."
    brew install supabase/tap/supabase
fi

if [[ -e ".venv-saas" && ! -x ".venv-saas/bin/python" ]]; then
    echo "The existing .venv-saas is not usable on this Mac. Rename it, then rerun setup."
    exit 1
fi

if [[ ! -x ".venv-saas/bin/python" ]]; then
    echo
    echo "Creating the Python 3.12 environment..."
    "$PYTHON_BIN" -m venv .venv-saas
fi

echo
echo "Installing Python packages..."
"$PROJECT_DIR/.venv-saas/bin/python" -m pip install --upgrade pip setuptools wheel
"$PROJECT_DIR/.venv-saas/bin/python" -m pip install -r requirements-dev.txt

echo
echo "Building the browser assets..."
npm --prefix frontend ci
npm --prefix frontend run build

if [[ ! -f "$SAAS_ENV_FILE" ]]; then
    cp .env.example "$SAAS_ENV_FILE"
    chmod 600 "$SAAS_ENV_FILE"
    echo
    echo "Created .env.saas.local. Add the development Supabase credentials before launching."
fi

chmod 600 "$SAAS_ENV_FILE"
chmod +x START_HERE.command run_mac.command dev.command setup_mac.sh

echo
echo "Checking AutoApply Cloud..."
"$PROJECT_DIR/.venv-saas/bin/python" -c "from app.saas_main import app; print('AutoApply Cloud import check: OK')"

echo
echo "Setup complete. Double-click run_mac.command for a normal launch."
