#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-199}"

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

exec python -m uvicorn main:app --host "$HOST" --port "$APP_PORT"
