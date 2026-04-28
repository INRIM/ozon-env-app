#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
DATABASE_DIR="${ROOT_DIR}/database"
INIT_SCRIPT="${DATABASE_DIR}/scripts/init_db.js"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "File .env non trovato in ${ROOT_DIR}" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${MONGO_USER:?Variabile MONGO_USER non valorizzata}"
: "${MONGO_PASS:?Variabile MONGO_PASS non valorizzata}"
: "${MONGO_DB:?Variabile MONGO_DB non valorizzata}"

mkdir -p "${DATABASE_DIR}/scripts"

(
  cd "${DATABASE_DIR}"
  bash "./build_imges.sh"
)

cat > "${INIT_SCRIPT}" <<EOF
db.createUser({
  user: "${MONGO_USER}",
  pwd: "${MONGO_PASS}",
  roles: [
    { role: "readWrite", db: "${MONGO_DB}" },
    { role: "readWrite", db: "admin" }
  ]
});
EOF
