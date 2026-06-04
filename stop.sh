#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "stop ozon-env-app"
docker compose -f "${ROOT_DIR}/docker-compose.yml" stop
