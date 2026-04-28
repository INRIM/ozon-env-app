#!/bin/bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv non trovato: installa uv per eseguire i test."
  exit 1
fi

echo "check code (optional)"
if [[ "${RUN_FORMAT_CHECK:-0}" == "1" ]]; then
  uv run black --check app tests
  # uv run flake8 app tests
else
  echo "skip format check (set RUN_FORMAT_CHECK=1 to enable)"
fi

rm -rf tests/models

docker compose down
docker compose up -d

echo "run test"
uv run pytest tests/test_api.py

echo "make project: Done."
