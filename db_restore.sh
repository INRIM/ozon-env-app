#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
DATABASE_DIR="${ROOT_DIR}/database"
DATABASE_SCRIPTS_DIR="${DATABASE_DIR}/scripts"
INIT_SCRIPT="${DATABASE_SCRIPTS_DIR}/init_db.js"
SETUP_SCRIPT="${ROOT_DIR}/setup_db.sh"
RESTORE_SCRIPT="${DATABASE_DIR}/db_restore.sh"
DUMP_DIR="${DATABASE_DIR}/dump"

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
  if [[ ! -d "${DUMP_DIR}" ]]; then
    echo "Directory dump non trovata: ${DUMP_DIR}" >&2
    exit 1
  fi

  if [[ -n "${MONGO_DB:-}" && -d "${DUMP_DIR}/${MONGO_DB}" ]]; then
    printf '%s\n' "${MONGO_DB}"
    return
  fi

  mapfile -t dump_dirs < <(find "${DUMP_DIR}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)

  if [[ ${#dump_dirs[@]} -eq 0 ]]; then
    echo "Nessun dump database trovato in ${DUMP_DIR}" >&2
    exit 1
  fi

  if [[ ${#dump_dirs[@]} -gt 1 ]]; then
    echo "Trovati piu dump in ${DUMP_DIR}: ${dump_dirs[*]}. Allinea il nome a MONGO_DB oppure lascia un solo dump." >&2
    exit 1
  fi

  printf '%s\n' "${dump_dirs[0]}"
}

main() {
  load_env
  require_var "MONGO_DB"

  local dump_name
  dump_name="$(resolve_dump_name)"

  if [[ ! -f "${INIT_SCRIPT}" ]]; then
    echo "File ${INIT_SCRIPT} non trovato. Eseguo setup_db.sh"
    (
      cd "${ROOT_DIR}"
      bash "${SETUP_SCRIPT}"
    )
  fi

  if [[ ! -f "${INIT_SCRIPT}" ]]; then
    echo "Il file ${INIT_SCRIPT} non e stato creato correttamente." >&2
    exit 1
  fi

  (
    cd "${ROOT_DIR}"
    bash "${RESTORE_SCRIPT}" "${dump_name}"
  )
}

main "$@"
