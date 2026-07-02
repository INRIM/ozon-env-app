#!/usr/bin/env bash
# Esegue bootstrap.py dentro il container app.
# Chiede l'UID dell'admin base se non passato come argomento.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SELECTED_ITEMS=()

select_items() {
    local label="$1"
    shift
    local items=("$@")
    local choice
    local indexes
    local idx
    local item

    SELECTED_ITEMS=()
    if [[ ${#items[@]} -eq 0 ]]; then
        return 0
    fi

    echo ""
    echo "${label} trovati:"
    idx=1
    for item in "${items[@]}"; do
        echo "  ${idx}) $(basename "$item")"
        idx=$((idx + 1))
    done

    if [[ ! -t 0 ]]; then
        echo "Input non interattivo: nessun ${label} avviato."
        return 0
    fi

    read -rp "Quali ${label} avviare? [all/none/1,3]: " choice
    choice="${choice// /}"
    case "$choice" in
        all|ALL)
            SELECTED_ITEMS=("${items[@]}")
            return 0
            ;;
        ""|none|NONE|no|NO)
            return 0
            ;;
    esac

    IFS=',' read -r -a indexes <<< "$choice"
    for idx in "${indexes[@]}"; do
        if [[ "$idx" =~ ^[0-9]+$ ]] && (( idx >= 1 && idx <= ${#items[@]} )); then
            SELECTED_ITEMS+=("${items[$((idx - 1))]}")
        else
            echo "Selezione ignorata: $idx" >&2
        fi
    done
}

start_services() {
    local services_dir="${SCRIPT_DIR}/services"
    local service_dirs=()
    local service_dir

    [[ -d "$services_dir" ]] || return 0

    while IFS= read -r service_dir; do
        service_dirs+=("$service_dir")
    done < <(find "$services_dir" -mindepth 1 -maxdepth 1 -type d ! -name '.*' | sort)

    select_items "service" "${service_dirs[@]}"
    for service_dir in "${SELECTED_ITEMS[@]}"; do
        if [[ -x "${service_dir}/run.sh" ]]; then
            echo "→ avvio service $(basename "$service_dir")..."
            (cd "$service_dir" && ./run.sh)
        elif [[ -f "${service_dir}/docker-compose.yml" ]]; then
            echo "→ avvio service $(basename "$service_dir") via docker compose..."
            (cd "$service_dir" && docker compose up -d --build)
        else
            echo "WARN: service $(basename "$service_dir") senza run.sh o docker-compose.yml, salto." >&2
        fi
    done
}

scaffold_worker_compose() {
    local worker_dir="$1"
    local worker_name
    local compose_file
    local modules=()
    local py_file
    local module_name
    local service_name

    worker_name="$(basename "$worker_dir")"
    compose_file="${worker_dir}/docker-compose.yml"
    [[ ! -f "$compose_file" ]] || return 0

    while IFS= read -r py_file; do
        modules+=("$(basename "$py_file" .py)")
    done < <(find "$worker_dir" -mindepth 1 -maxdepth 1 -type f -name '*.py' ! -name '__init__.py' | sort)

    if [[ ${#modules[@]} -eq 0 ]]; then
        echo "WARN: worker ${worker_name} senza moduli Python avviabili, scaffold saltato." >&2
        return 1
    fi

    echo "→ creo scaffold docker-compose.yml per worker ${worker_name}..."
    cat > "$compose_file" <<EOF
name: ozon-env-app-${worker_name}

x-worker-base: &worker-base
  image: ozonapp-worker:latest
  build:
    context: ..
    dockerfile: ozon_camunda_worker/Dockerfile
  env_file:
    - path: ../../.env
    - path: service.env
      required: false
  environment:
    PYTHONPATH: /app/workers
    OZON_LOCALEDIR: /tmp/ozon-locale
    OZON_APPLANG: it
  volumes:
    - ../../workers:/app/workers
    - ../../models:/models
  extra_hosts:
    - "host.docker.internal:host-gateway"
  networks:
    - ozn-network
  restart: unless-stopped
  mem_limit: 384m

services:
EOF

    for module_name in "${modules[@]}"; do
        service_name="${module_name//_/-}"
        cat >> "$compose_file" <<EOF
  ${service_name}:
    <<: *worker-base
    command:
      [
        "sh",
        "-c",
        "mkdir -p /tmp/ozon-locale && /app/.venv/bin/python /app/workers/ozon_camunda_worker/wait_for_camunda.py && exec /app/.venv/bin/python -m ${worker_name}.${module_name}",
      ]

EOF
    done

    cat >> "$compose_file" <<EOF
networks:
  ozn-network:
    external: true
    name: ozn-network
EOF
}

start_workers() {
    local workers_dir="${SCRIPT_DIR}/workers"
    local worker_dirs=()
    local worker_dir

    [[ -d "$workers_dir" ]] || return 0

    while IFS= read -r worker_dir; do
        worker_dirs+=("$worker_dir")
    done < <(find "$workers_dir" -mindepth 1 -maxdepth 1 -type d ! -name '.*' ! -name 'ozon_camunda_worker' | sort)

    select_items "worker" "${worker_dirs[@]}"
    for worker_dir in "${SELECTED_ITEMS[@]}"; do
        if [[ -x "${worker_dir}/run.sh" ]]; then
            echo "→ avvio worker $(basename "$worker_dir")..."
            (cd "$worker_dir" && ./run.sh)
            continue
        fi

        scaffold_worker_compose "$worker_dir" || continue
        echo "→ avvio worker $(basename "$worker_dir") via docker compose..."
        (cd "$worker_dir" && docker compose up -d --build)
    done
}

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

start_services
start_workers
