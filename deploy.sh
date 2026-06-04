#!/usr/bin/env bash
# Deploy = setup-db + bootstrap + avvio stack.
# ./deploy.sh [local|dev|prod] <admin_uid> [opzioni ansible]   (env default: local)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STAGE=local
if [[ "${1:-}" =~ ^(local|dev|prod)$ ]]; then
  STAGE="$1"
  shift
fi

if [ $# -lt 1 ]; then
  echo "usage: deploy.sh [local|dev|prod] <admin_uid> [ansible options]"
  echo "example: deploy.sh a.gerace            # local"
  echo "example: deploy.sh prod a.gerace"
  echo "example: deploy.sh dev a.gerace -e ozonapp_deploy_build=true"
  exit 1
fi

ADMIN_UID="$1"
shift

export ANSIBLE_CONFIG="${ANSIBLE_CONFIG:-${ROOT_DIR}/ansible.cfg}"
export ANSIBLE_LOCAL_TEMP="${ANSIBLE_LOCAL_TEMP:-${ROOT_DIR}/.ansible/tmp}"
mkdir -p "${ANSIBLE_LOCAL_TEMP}"

INVENTORY="${ANSIBLE_INVENTORY:-${ROOT_DIR}/ansible/inventories/${STAGE}/hosts.yml}"

if [ "${STAGE}" = "local" ]; then
  ansible-playbook -i "${INVENTORY}" "${ROOT_DIR}/ansible/playbooks/deploy.yml" \
    -e "ozonapp_project_root=${ROOT_DIR}" \
    -e "ozonapp_sync_sources=false" \
    -e "ozonapp_admin_uid=${ADMIN_UID}" \
    "$@"
else
  ansible-playbook -i "${INVENTORY}" "${ROOT_DIR}/ansible/playbooks/deploy.yml" \
    -e "ozonapp_admin_uid=${ADMIN_UID}" \
    "$@"
fi
