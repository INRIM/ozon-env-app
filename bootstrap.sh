#!/usr/bin/env bash
# Esegue bootstrap.py dentro il container app.
# Chiede l'UID dell'admin base se non passato come argomento.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ADMIN_UID="${1:-}"
if [[ -z "$ADMIN_UID" ]]; then
    read -rp "Admin UID: " ADMIN_UID
fi

if [[ -z "$ADMIN_UID" ]]; then
    echo "ERROR: admin UID richiesto" >&2
    exit 1
fi

echo "→ preparo rete docker ozn-network..."
docker network inspect ozn-network >/dev/null 2>&1 || docker network create ozn-network >/dev/null

echo "→ avvio DB se non attivo..."
docker compose up -d ozonenv_app_db

echo "→ attendo che il DB sia pronto..."
until docker compose exec ozonenv_app_db mongosh --quiet --eval "db.adminCommand('ping')" > /dev/null 2>&1; do
    sleep 2
done
echo "→ DB pronto"

docker compose run --rm \
    -v "${SCRIPT_DIR}/bootstrap.py:/app/bootstrap.py:ro" \
    app \
    uv run python bootstrap.py --admin "$ADMIN_UID" "${@:2}"
