#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose-test.yml"
ENV_FILE="${ROOT_DIR}/tests/camunda_e2e/.env"

APP_CODE="${APP_CODE:-test_request}"
APP_TOKEN="${APP_TOKEN:-}"
SESSION_SECRET="${SESSION_SECRET:-camunda-e2e-dev-only}"
BUILD_IMAGES="${BUILD_IMAGES:-0}"
# NOEXIT=1 (o arg `noexit`): non rimuove lo stack a fine test, cosi puoi
# verificare i log e spegnerlo dopo con `down`.
NOEXIT="${NOEXIT:-0}"

usage() {
  printf '%s\n' \
    "Uso:" \
    "  $0 [run]                         run + teardown (stack rimosso a fine test)" \
    "  $0 noexit                        run SENZA teardown (stack resta su)" \
    "  $0 down                          spegne/rimuove lo stack" \
    "  $0 logs                          segue i log dello stack" \
    "" \
    "Variabili:" \
    "  APP_CODE=test_request            codice app/model, non token" \
    "  APP_TOKEN=...                    OPZIONALE: token statico unico. Di norma" \
    "                                   non serve, i token sono mintati da keycloak" \
    "                                   per-utente (utente/responsible/manager)." \
    "  BUILD_IMAGES=1                   esegue ./build_imges.sh prima del compose" \
    "  NOEXIT=1                         come l'arg noexit: niente teardown" \
    "" \
    "Default: a fine run lo stack viene rimosso (down -v). Con noexit/NOEXIT=1" \
    "resta su per ispezionare i log (poi: $0 logs / $0 down)."
}

write_env_file() {
  umask 077
  {
    printf 'APP_CODE=%s\n' "${APP_CODE}"
    printf 'SESSION_SECRET=%s\n' "${SESSION_SECRET}"
    # APP_TOKEN solo se forzato a mano (override del mint keycloak per-utente).
    if [[ -n "${APP_TOKEN}" ]]; then
      printf 'APP_TOKEN=%s\n' "${APP_TOKEN}"
    fi
  } > "${ENV_FILE}"
}

teardown() {
  # Sparisce tutto a fine run: container, volumi e rete del progetto.
  docker compose -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
}

compose_logs() {
  docker compose -f "${COMPOSE_FILE}" logs --tail 220 \
    app worker-check-user worker-approved worker-refused runner camunda keycloak || true
}

run_stack() {
  write_env_file

  if [[ "${BUILD_IMAGES}" == "1" ]]; then
    "${ROOT_DIR}/build_imges.sh"
  fi

  # Teardown a fine run, SALVO noexit (per ispezionare i log).
  if [[ "${NOEXIT}" != "1" ]]; then
    trap teardown EXIT
  fi

  # Niente porte host: --wait aspetta gli healthcheck (camunda/db/app) e i
  # servizi started; nessun curl dall'host. La readiness di keycloak e' gestita
  # dal retry nel mint token.
  if ! docker compose -f "${COMPOSE_FILE}" up -d --wait \
    camunda keycloak db app \
    worker-check-user worker-approved worker-refused; then
    compose_logs >&2
    exit 1
  fi
  if ! docker compose -f "${COMPOSE_FILE}" run --rm runner; then
    compose_logs >&2
    exit 1
  fi

  if [[ "${NOEXIT}" == "1" ]]; then
    printf '%s\n' \
      "Stack lasciato SU (noexit). Log: $0 logs   Stop: $0 down"
  fi
}

case "${1:-run}" in
  run)
    run_stack
    ;;
  noexit)
    NOEXIT=1
    run_stack
    ;;
  down)
    docker compose -f "${COMPOSE_FILE}" down -v
    ;;
  logs)
    docker compose -f "${COMPOSE_FILE}" logs -f \
      app worker-check-user worker-approved worker-refused runner camunda keycloak
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
