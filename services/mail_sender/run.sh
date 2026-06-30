#!/usr/bin/env bash
# Avvio mail_sender.
#
# Env: usa l'env base dello stack (../../.env). I parametri SMTP NON stanno qui:
# il worker legge mail_server_out dal DB. Override opzionali via ./service.env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BASE_ENV="../../.env"

env_get() {
  local f="$1" k="$2"
  [ -f "$f" ] || return 0
  grep -E "^${k}=" "$f" | tail -1 | cut -d= -f2-
}
have() { [ -n "${1:-}" ]; }

[ -f "$BASE_ENV" ] || {
  echo "ERRORE: manca env base $BASE_ENV. Configura prima lo stack principale."
  exit 1
}

missing=()
for k in APP_CODE MONGO_USER MONGO_PASS MONGO_DB; do
  have "$(env_get "$BASE_ENV" "$k")" || missing+=("$k")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "ERRORE: chiavi mancanti in $BASE_ENV: ${missing[*]}"
  exit 1
fi

echo "Avvio mail_sender..."
docker compose up -d --build
echo "mail_sender avviato. Log: docker logs -f ozon-env-mail-sender"
