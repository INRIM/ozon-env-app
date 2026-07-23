#!/usr/bin/env bash
# Deploy locale senza Ansible: build immagini e avvio dello stack Compose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
DOCKER_NETWORK="ozn-network"

RUN_BUILD=true
BOOTSTRAP_MODE=ask

usage() {
    cat <<'EOF'
Uso: ./deploy.sh [--skip-build] [--bootstrap|--no-bootstrap]

  --skip-build    non esegue build_imges.sh
  --bootstrap     esegue bootstrap.sh dopo l'avvio
  --no-bootstrap  non propone il bootstrap
  -h, --help      mostra questo messaggio
EOF
}

while (($# > 0)); do
    case "$1" in
        --skip-build)
            RUN_BUILD=false
            ;;
        --bootstrap)
            BOOTSTRAP_MODE=run
            ;;
        --no-bootstrap)
            BOOTSTRAP_MODE=skip
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: opzione non supportata: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

command -v docker >/dev/null 2>&1 || {
    echo "ERROR: docker non trovato nel PATH" >&2
    exit 1
}

[[ -f "${COMPOSE_FILE}" ]] || {
    echo "ERROR: file Compose non trovato: ${COMPOSE_FILE}" >&2
    exit 1
}

if [[ ! -f "${ENV_FILE}" ]]; then
    [[ -f "${ENV_EXAMPLE}" ]] || {
        echo "ERROR: ${ENV_FILE} assente e fallback ${ENV_EXAMPLE} non trovato" >&2
        exit 1
    }
    cp "${ENV_EXAMPLE}" "${ENV_FILE}"
    chmod 0600 "${ENV_FILE}"
    echo "Creato .env da .env.example"
fi

if [[ "${RUN_BUILD}" == true ]]; then
    [[ -x "${SCRIPT_DIR}/build_imges.sh" ]] || {
        echo "ERROR: build_imges.sh assente o non eseguibile" >&2
        exit 1
    }
    echo "Build delle immagini ozon-env-app..."
    (cd "${SCRIPT_DIR}" && ./build_imges.sh)
fi

if ! docker network inspect "${DOCKER_NETWORK}" >/dev/null 2>&1; then
    echo "Creazione rete Docker ${DOCKER_NETWORK}..."
    docker network create "${DOCKER_NETWORK}" >/dev/null
fi

echo "Avvio dello stack Docker Compose..."
docker compose -f "${COMPOSE_FILE}" up -d

if [[ "${BOOTSTRAP_MODE}" == ask ]]; then
    if [[ -t 0 ]]; then
        read -r -p "Eseguire bootstrap.sh ora? [y/N]: " answer
        case "${answer}" in
            y|Y|yes|YES) BOOTSTRAP_MODE=run ;;
            *) BOOTSTRAP_MODE=skip ;;
        esac
    else
        echo "Input non interattivo: bootstrap.sh non eseguito."
        BOOTSTRAP_MODE=skip
    fi
fi

if [[ "${BOOTSTRAP_MODE}" == run ]]; then
    [[ -x "${SCRIPT_DIR}/bootstrap.sh" ]] || {
        echo "ERROR: bootstrap.sh assente o non eseguibile" >&2
        exit 1
    }
    "${SCRIPT_DIR}/bootstrap.sh"
fi
