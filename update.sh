#!/usr/bin/env bash
# Update = ricrea un servizio del compose (default: app).
# ./update.sh [local|dev|prod] [service] [opzioni ansible]   (env default: local)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STAGE=local
if [[ "${1:-}" =~ ^(local|dev|prod)$ ]]; then
  STAGE="$1"
  shift
fi

SERVICE=app
if [ $# -ge 1 ] && [[ "${1}" != -* ]]; then
  SERVICE="$1"
  shift
fi

export ANSIBLE_CONFIG="${ANSIBLE_CONFIG:-${ROOT_DIR}/ansible.cfg}"
export ANSIBLE_LOCAL_TEMP="${ANSIBLE_LOCAL_TEMP:-${ROOT_DIR}/.ansible/tmp}"
mkdir -p "${ANSIBLE_LOCAL_TEMP}"

INVENTORY="${ANSIBLE_INVENTORY:-${ROOT_DIR}/ansible/inventories/${STAGE}/hosts.yml}"

if [ "${STAGE}" = "local" ]; then
  ansible-playbook -i "${INVENTORY}" "${ROOT_DIR}/ansible/playbooks/update.yml" \
    -e "ozonapp_project_root=${ROOT_DIR}" \
    -e "ozonapp_sync_sources=false" \
    -e "ozonapp_update_service=${SERVICE}" \
    "$@"
else
  ansible-playbook -i "${INVENTORY}" "${ROOT_DIR}/ansible/playbooks/update.yml" \
    -e "ozonapp_update_service=${SERVICE}" \
    "$@"
fi
