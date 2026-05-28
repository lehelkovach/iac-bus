#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/venv}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "Missing virtualenv python at ${VENV_DIR}/bin/python"
  exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/watchmedo" ]]; then
  echo "Missing watchmedo at ${VENV_DIR}/bin/watchmedo (install watchdog)"
  exit 1
fi

export BUS_LOG_LEVEL="${BUS_LOG_LEVEL:-DEBUG}"
export PYTHONUNBUFFERED=1
export FLASK_DEBUG="${FLASK_DEBUG:-1}"

WATCH_DIRECTORY="${WATCH_DIRECTORY:-${APP_DIR}}"
WATCH_PATTERNS="${WATCH_PATTERNS:-*.py;*.json;*.md;*.yaml;*.yml}"
WATCH_IGNORE_PATTERNS="${WATCH_IGNORE_PATTERNS:-*/.git/*;*/venv/*;*/.venv/*;*/__pycache__/*}"

exec "${VENV_DIR}/bin/watchmedo" auto-restart \
  --directory "${WATCH_DIRECTORY}" \
  --recursive \
  --patterns "${WATCH_PATTERNS}" \
  --ignore-patterns "${WATCH_IGNORE_PATTERNS}" \
  --signal SIGTERM \
  -- "${VENV_DIR}/bin/python" "${APP_DIR}/server.py"
