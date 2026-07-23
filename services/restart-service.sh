#!/usr/bin/env bash
# Implementazione condivisa dai restart.sh dei service.
set -euo pipefail

SERVICES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if (($# != 2)); then
    echo "ERRORE: restart-service.sh deve essere invocato dal restart.sh del service." >&2
    exit 2
fi

SERVICE_DIR="$1"
SERVICE_NAME="$2"

if [[ ! "${SERVICE_NAME}" =~ ^[a-z0-9_]+$ ]]; then
    echo "ERRORE: nome service non valido: ${SERVICE_NAME}" >&2
    exit 2
fi

EXPECTED_DIR="${SERVICES_DIR}/${SERVICE_NAME}"
if [[ ! -d "${SERVICE_DIR}" ]] \
    || [[ "$(cd "${SERVICE_DIR}" && pwd)" != "${EXPECTED_DIR}" ]]; then
    echo "ERRORE: directory non valida per il service ${SERVICE_NAME}." >&2
    exit 2
fi

COMPOSE_FILE="${SERVICE_DIR}/docker-compose.yml"
MANIFEST_FILE="${SERVICE_DIR}/manifest.json"
[[ -f "${COMPOSE_FILE}" ]] || {
    echo "ERRORE: file Compose non trovato: ${COMPOSE_FILE}" >&2
    exit 1
}
[[ -f "${MANIFEST_FILE}" ]] || {
    echo "ERRORE: manifest non trovato: ${MANIFEST_FILE}" >&2
    exit 1
}
command -v docker >/dev/null 2>&1 || {
    echo "ERRORE: docker non trovato nel PATH." >&2
    exit 1
}

# Compose usa gli env-file anche per l'interpolazione del compose. L'ordine
# mantiene il contratto esistente: service.env sovrascrive l'env base.
ENV_ARGS=()
BASE_ENV="${SERVICE_DIR}/../../.env"
SERVICE_ENV="${SERVICE_DIR}/service.env"
[[ -f "${BASE_ENV}" ]] && ENV_ARGS+=(--env-file "${BASE_ENV}")
[[ -f "${SERVICE_ENV}" ]] && ENV_ARGS+=(--env-file "${SERVICE_ENV}")

echo "Riavvio ${SERVICE_NAME} rileggendo la configurazione env..."
(
    cd "${SERVICE_DIR}"
    docker compose "${ENV_ARGS[@]}" up -d --force-recreate --no-deps
)
echo "${SERVICE_NAME} riavviato."
