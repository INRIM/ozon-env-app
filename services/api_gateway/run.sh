#!/usr/bin/env bash
# Avvio auto-gestito di api_gateway.
#
# Layering env:
#   - ../../.env       env generico dello stack (STACK_NAME, NETWORK, ...)
#   - ./service.env    env specifico del service (S2S + var del config), gitignored
#
# Flusso:
#   1. verifica env base (stack) presente
#   2. API_GATEWAY_S2S_TOKEN: riusa REMOTE_SELECT_S2S_TOKEN del .env dell'app,
#      altrimenti lo genera e ricorda di allinearlo lato app
#   3. valori delle ${VAR} usate in config/gateway.json (PEOPLE_URL, ...)
#   4. docker compose up -d --build
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BASE_ENV="../../.env"
SVC_ENV="service.env"

env_get() {
  local f="$1" k="$2"
  [ -f "$f" ] || return 0
  grep -E "^${k}=" "$f" | tail -1 | cut -d= -f2-
}

env_set() {
  local k="$1" v="$2"
  touch "$SVC_ENV"
  if grep -qE "^${k}=" "$SVC_ENV"; then
    grep -vE "^${k}=" "$SVC_ENV" > "$SVC_ENV.tmp" && mv "$SVC_ENV.tmp" "$SVC_ENV"
  fi
  printf '%s=%s\n' "$k" "$v" >> "$SVC_ENV"
}

have() { [ -n "${1:-}" ]; }

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
have "$(env_get "$BASE_ENV" NETWORK)" || {
  echo "ERRORE: NETWORK mancante in $BASE_ENV."
  exit 1
}

# --- 2. token S2S -----------------------------------------------------------
# La sorgente di verita' e' il .env dell'app: il gateway deve accettare
# esattamente il token che l'app invia.
S2S="$(env_get "$SVC_ENV" API_GATEWAY_S2S_TOKEN)"
APP_S2S="$(env_get "$BASE_ENV" REMOTE_SELECT_S2S_TOKEN)"
if have "$APP_S2S" && [ "$S2S" != "$APP_S2S" ]; then
  env_set API_GATEWAY_S2S_TOKEN "$APP_S2S"
  S2S="$APP_S2S"
  echo "Token S2S allineato a REMOTE_SELECT_S2S_TOKEN di $BASE_ENV."
fi
if ! have "$S2S"; then
  S2S="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  env_set API_GATEWAY_S2S_TOKEN "$S2S"
  echo ""
  echo ">> Token S2S generato. Aggiungi in $BASE_ENV (lato ozon-env-app):"
  echo "   REMOTE_SELECT_S2S_TOKEN=$S2S"
  echo "   REMOTE_SELECT_ALLOWED_HOSTS=api-gateway"
  echo ""
fi

# --- 3. env referenziate dal config -----------------------------------------
# config/gateway.json elenca gli upstream, ciascuno nel suo config/<nome>.json
# (non versionati: si parte da *.json.example). Le credenziali sono ${VAR}:
# qui si chiedono solo quelle mancanti. Il gateway non parte se una var
# referenziata resta vuota.
CONFIG="config/gateway.json"
[ -f "$CONFIG" ] || {
  echo "ERRORE: manca $CONFIG. Parti da config/gateway.json.example e"
  echo "        aggiungi un config/<upstream>.json per API (da config/upstream.json.example)."
  exit 1
}
for var in $(cat config/*.json | grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' | tr -d '${}' | sort -u); do
  val="$(svc_or_base "$var")"
  if ! have "$val"; then
    read -rp "$var (referenziata in config/): " val
    have "$val" || { echo "$var obbligatoria"; exit 1; }
  fi
  env_set "$var" "$val"
done

# --- 4. up ------------------------------------------------------------------
echo ""
echo "Config pronta ($SVC_ENV). Avvio compose..."
docker compose --env-file "$BASE_ENV" up -d --build
echo "api_gateway avviato. Log: docker logs -f \${STACK_NAME:-ozon-env-app}-api-gateway"
