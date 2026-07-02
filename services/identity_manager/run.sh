#!/usr/bin/env bash
# Avvio auto-gestito di identity_manager.
#
# Layering env:
#   - ../../.env       env generico dello stack (APP_CODE, MONGO_*, ...)
#   - ./service.env    env specifico del service (IDENTITY_MANAGER_*), gitignored
#
# Flusso:
#   1. verifica env base (stack) presente
#   2. param opzionali (interval)
#   3. docker compose up -d --build
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BASE_ENV="../../.env"
SVC_ENV="service.env"

# --- helper -----------------------------------------------------------------
# valore di una chiave da un env file (ultima occorrenza), senza source.
env_get() {
  local f="$1" k="$2"
  [ -f "$f" ] || return 0
  grep -E "^${k}=" "$f" | tail -1 | cut -d= -f2-
}

# upsert chiave=valore in service.env.
env_set() {
  local k="$1" v="$2"
  touch "$SVC_ENV"
  if grep -qE "^${k}=" "$SVC_ENV"; then
    grep -vE "^${k}=" "$SVC_ENV" > "$SVC_ENV.tmp" && mv "$SVC_ENV.tmp" "$SVC_ENV"
  fi
  printf '%s=%s\n' "$k" "$v" >> "$SVC_ENV"
}

have() { [ -n "${1:-}" ]; }

# valore preso da service.env, fallback su env base.
svc_or_base() {
  local v
  v="$(env_get "$SVC_ENV" "$1")"
  [ -n "$v" ] || v="$(env_get "$BASE_ENV" "$1")"
  printf '%s' "$v"
}

# --- 1. env base ------------------------------------------------------------
[ -f "$BASE_ENV" ] || {
  echo "ERRORE: manca env base $BASE_ENV. Configura prima lo stack principale."
  exit 1
}
missing_base=()
for k in APP_CODE MONGO_USER MONGO_PASS MONGO_DB; do
  have "$(env_get "$BASE_ENV" "$k")" || missing_base+=("$k")
done
if [ ${#missing_base[@]} -gt 0 ]; then
  echo "ERRORE: chiavi mancanti in $BASE_ENV: ${missing_base[*]}"
  echo "Configura lo stack principale prima di avviare identity_manager."
  exit 1
fi

# --- 2. param opzionali (default applicati da config.py) --------------------
if ! have "$(env_get "$SVC_ENV" IDENTITY_MANAGER_INTERVAL_MINUTES)" && ! have "$(env_get "$BASE_ENV" IDENTITY_MANAGER_INTERVAL_MINUTES)"; then
  read -rp "IDENTITY_MANAGER_INTERVAL_MINUTES minuti [10]: " itv
  have "$itv" && env_set IDENTITY_MANAGER_INTERVAL_MINUTES "$itv"
fi

# --- 3. up ------------------------------------------------------------------
echo ""
echo "Config pronta ($SVC_ENV). Avvio compose..."
docker compose up -d --build
echo "identity_manager avviato. Log: docker logs -f ozon-env-identity-manager"
