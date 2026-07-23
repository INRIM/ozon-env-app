#!/usr/bin/env bash
# Avvio auto-gestito di calendar_scheduler.
#
# Env layering: ../../.env (base) + ./service.env (specifico SCHEDULER_*).
#
# Lo scheduler chiama l'endpoint run dell'app, che verifica un JWT keycloak:
# l'auth e' SOLO M2M keycloak (niente token statico). Se manca, run.sh lancia
# manager/keycloak-manager/run.sh e importa i SCHEDULER_OAUTH_* in service.env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BASE_ENV="../../.env"
SVC_ENV="service.env"
KC_DIR="../../manager/keycloak-manager"
KC_OUT="$KC_DIR/out/kc-env.var"
APP_DEFAULT_URL="http://ozon-env-app:8000"

env_get() { local f="$1" k="$2"; [ -f "$f" ] || return 0; grep -E "^${k}=" "$f" | tail -1 | cut -d= -f2-; }
env_set() {
  local k="$1" v="$2"; touch "$SVC_ENV"
  grep -qE "^${k}=" "$SVC_ENV" && { grep -vE "^${k}=" "$SVC_ENV" > "$SVC_ENV.tmp" && mv "$SVC_ENV.tmp" "$SVC_ENV"; }
  printf '%s=%s\n' "$k" "$v" >> "$SVC_ENV"
}
have() { [ -n "${1:-}" ]; }
svc_or_base() { local v; v="$(env_get "$SVC_ENV" "$1")"; [ -n "$v" ] || v="$(env_get "$BASE_ENV" "$1")"; printf '%s' "$v"; }

# --- 1. env base ------------------------------------------------------------
[ -f "$BASE_ENV" ] || { echo "ERRORE: manca env base $BASE_ENV. Configura prima lo stack."; exit 1; }
missing=()
for k in APP_CODE MONGO_USER MONGO_PASS MONGO_DB; do
  have "$(env_get "$BASE_ENV" "$k")" || missing+=("$k")
done
[ ${#missing[@]} -eq 0 ] || { echo "ERRORE: chiavi mancanti in $BASE_ENV: ${missing[*]}"; exit 1; }

# --- 2. URL endpoint run ----------------------------------------------------
RUN_URL="$(svc_or_base SCHEDULER_RUN_BASE_URL)"
if ! have "$RUN_URL"; then
  read -rp "SCHEDULER_RUN_BASE_URL [$APP_DEFAULT_URL]: " RUN_URL
  have "$RUN_URL" || RUN_URL="$APP_DEFAULT_URL"
  env_set SCHEDULER_RUN_BASE_URL "$RUN_URL"
fi

# --- 3. auth M2M keycloak (obbligatoria) ------------------------------------
OA_URL="$(svc_or_base SCHEDULER_OAUTH_TOKEN_URL)"
OA_ID="$(svc_or_base SCHEDULER_OAUTH_CLIENT_ID)"
OA_SEC="$(svc_or_base SCHEDULER_OAUTH_CLIENT_SECRET)"
if ! { have "$OA_URL" && have "$OA_ID" && have "$OA_SEC"; }; then
  echo ""
  echo "Config M2M keycloak mancante (l'endpoint run verifica il JWT keycloak)."
  echo ">> Lancio keycloak-manager. Quando chiede il PREFISSO env, inserisci: SCHEDULER"
  echo ""
  ( cd "$KC_DIR" && ./run.sh )
  [ -f "$KC_OUT" ] || { echo "ERRORE: $KC_OUT non generato"; exit 1; }
  imported=0
  for key in TOKEN_URL CLIENT_ID CLIENT_SECRET AUDIENCE; do
    val="$(grep -E "OAUTH_${key}=" "$KC_OUT" | tail -1 | cut -d= -f2-)"
    have "$val" && { env_set "SCHEDULER_OAUTH_${key}" "$val"; imported=$((imported + 1)); }
  done
  [ "$imported" -ge 3 ] || { echo "ERRORE: config M2M incompleta in $KC_OUT"; exit 1; }
  echo "Config M2M importata in $SVC_ENV."
fi

# --- 4. up ------------------------------------------------------------------
echo ""
echo "Config pronta ($SVC_ENV). Avvio compose..."
docker compose --env-file "$BASE_ENV" up -d --build
echo "calendar_scheduler avviato. Log: docker logs -f ozon-env-calendar-scheduler"
