#!/usr/bin/env bash
# Deploy = build immagini ozon-env-app + docker compose up.
# ./deploy.sh [local|dev|prod] [opzioni ansible]   (env default: local)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STAGE=local
if [[ "${1:-}" =~ ^(local|dev|prod)$ ]]; then
  STAGE="$1"
  shift
fi

export ANSIBLE_CONFIG="${ANSIBLE_CONFIG:-${ROOT_DIR}/ansible.cfg}"
export ANSIBLE_LOCAL_TEMP="${ANSIBLE_LOCAL_TEMP:-${ROOT_DIR}/.ansible/tmp}"
mkdir -p "${ANSIBLE_LOCAL_TEMP}"

INVENTORY="${ANSIBLE_INVENTORY:-${ROOT_DIR}/ansible/inventories/${STAGE}/hosts.yml}"

if [ "${STAGE}" = "local" ]; then
  ansible-playbook -i "${INVENTORY}" "${ROOT_DIR}/ansible/playbooks/deploy.yml" \
    -e "ozonapp_project_root=${ROOT_DIR}" \
    -e "ozonapp_sync_sources=false" \
    "$@"
else
  ansible-playbook -i "${INVENTORY}" "${ROOT_DIR}/ansible/playbooks/deploy.yml" "$@"
fi

if [ -t 0 ]; then
  read -rp "Eseguire bootstrap.sh ora? [y/N]: " RUN_BOOTSTRAP
  case "${RUN_BOOTSTRAP}" in
    y|Y|yes|YES)
      "${ROOT_DIR}/bootstrap.sh"
      ;;
  esac
else
  echo "Input non interattivo: bootstrap.sh non eseguito."
fi
