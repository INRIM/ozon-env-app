#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"
SERVICE_NAME="ozonenv_app_db"
INIT_SCRIPT="${SCRIPT_DIR}/scripts/init_db.js"
IMAGE_NAME="ozonapp.db:latest"
BUILD_SCRIPT="${SCRIPT_DIR}/build_imges.sh"

load_env() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "File .env non trovato in ${ROOT_DIR}" >&2
    exit 1
  fi

  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Variabile obbligatoria non valorizzata: ${name}" >&2
    exit 1
  fi
}

resolve_dump_name() {
  local requested_dump="${1:-}"
  local dump_root="${SCRIPT_DIR}/dump"

  if [[ ! -d "${dump_root}" ]]; then
    echo "Directory dump non trovata: ${dump_root}" >&2
    exit 1
  fi

  if [[ -n "${requested_dump}" ]]; then
    if [[ ! -d "${dump_root}/${requested_dump}" ]]; then
      echo "Dump richiesto non trovato: ${dump_root}/${requested_dump}" >&2
      exit 1
    fi
    printf '%s\n' "${requested_dump}"
    return
  fi

  if [[ -n "${MONGO_DB:-}" && -d "${dump_root}/${MONGO_DB}" ]]; then
    printf '%s\n' "${MONGO_DB}"
    return
  fi

  mapfile -t dump_dirs < <(find "${dump_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)

  if [[ ${#dump_dirs[@]} -eq 0 ]]; then
    echo "Nessun dump database trovato in ${dump_root}" >&2
    exit 1
  fi

  if [[ ${#dump_dirs[@]} -gt 1 ]]; then
    echo "Trovati piu dump in ${dump_root}: ${dump_dirs[*]}. Specifica quale usare." >&2
    exit 1
  fi

  printf '%s\n' "${dump_dirs[0]}"
}

wait_for_mongo() {
  local max_attempts=30
  local sleep_seconds=2
  local attempt

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if docker compose -f "${COMPOSE_FILE}" exec -T "${SERVICE_NAME}" \
      mongosh --quiet \
      --username "${MONGO_USER}" \
      --password "${MONGO_PASS}" \
      --authenticationDatabase admin \
      --eval "db.adminCommand({ ping: 1 })" >/dev/null 2>&1; then
      return 0
    fi

    sleep "${sleep_seconds}"
  done

  echo "MongoDB non e diventato disponibile in tempo utile." >&2
  exit 1
}

ensure_db_image() {
  if docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1; then
    return 0
  fi

  echo "Immagine ${IMAGE_NAME} non trovata in locale. Eseguo la build."
  bash "${BUILD_SCRIPT}"
}

main() {
  load_env
  require_var "MONGO_USER"
  require_var "MONGO_PASS"
  require_var "MONGO_DB"

  local dump_name
  dump_name="$(resolve_dump_name "${1:-}")"

  if [[ ! -f "${INIT_SCRIPT}" ]]; then
    echo "File ${INIT_SCRIPT} non trovato. Esegui prima ${ROOT_DIR}/db_restore.sh o ${ROOT_DIR}/setup_db.sh." >&2
    exit 1
  fi

  ensure_db_image

  echo "Avvio del servizio database ${SERVICE_NAME}"
  docker compose -f "${COMPOSE_FILE}" up -d "${SERVICE_NAME}"

  echo "Attendo la disponibilita di MongoDB"
  wait_for_mongo

  echo "Ripristino dump ${dump_name} nel database ${MONGO_DB}"
  docker compose -f "${COMPOSE_FILE}" exec -T "${SERVICE_NAME}" \
    mongorestore \
    --username "${MONGO_USER}" \
    --password "${MONGO_PASS}" \
    --authenticationDatabase admin \
    --drop \
    --db "${MONGO_DB}" \
    "/dump/${dump_name}"

  echo "Restore completato."
}

main "$@"
