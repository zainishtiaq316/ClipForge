#!/usr/bin/env bash
# ClipForge one-click launcher for macOS / Linux.
# First run: creates a Python environment, installs dependencies and builds the UI.
# Later runs: starts immediately and opens your browser.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=$(command -v python3 || command -v python || true)
if [ -z "$PYTHON" ] || ! "$PYTHON" -c "import sys; sys.exit(sys.version_info < (3, 10))"; then
  echo "Python 3.10 or newer is required: https://www.python.org/downloads/"
  exit 1
fi

if [ ! -f backend/.venv/.installed ]; then
  echo "[1/3] Installing the video engine. This happens only once and can take a few minutes..."
  "$PYTHON" -m venv backend/.venv
  backend/.venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
  backend/.venv/bin/python -m pip install --disable-pip-version-check -q -r backend/requirements.txt
  touch backend/.venv/.installed
fi

if [ ! -f frontend/dist/index.html ]; then
  if ! command -v npm >/dev/null; then
    echo "Node.js 18 or newer is required to build the interface the first time: https://nodejs.org/"
    exit 1
  fi
  echo "[2/3] Building the interface. This happens only once..."
  (cd frontend && npm ci --no-audit --no-fund --loglevel=error && npm run build)
fi

echo "[3/3] Starting ClipForge..."
cd backend
exec .venv/bin/python -m app
