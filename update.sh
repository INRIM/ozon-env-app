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
if [ -z "${ANSIBLE_LOCAL_TEMP:-}" ]; then
  REPO_ANSIBLE_TMP="${ROOT_DIR}/.ansible/tmp"
  if mkdir -p "${REPO_ANSIBLE_TMP}" 2>/dev/null && [ -w "${REPO_ANSIBLE_TMP}" ]; then
    ANSIBLE_LOCAL_TEMP="${REPO_ANSIBLE_TMP}"
  else
    ANSIBLE_LOCAL_TEMP="$(mktemp -d "${TMPDIR:-/tmp}/ozon-env-app-ansible.XXXXXX")"
    trap 'rm -rf "${ANSIBLE_LOCAL_TEMP}"' EXIT
  fi
  export ANSIBLE_LOCAL_TEMP
else
  export ANSIBLE_LOCAL_TEMP
  mkdir -p "${ANSIBLE_LOCAL_TEMP}"
fi

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
