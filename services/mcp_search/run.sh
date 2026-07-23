#!/usr/bin/env bash
# Avvio auto-gestito di mcp_search.
#
# Nessuna auth M2M da provisionare (a differenza di calendar_scheduler):
# il server non ha una propria identita' verso ozon-env-app, inoltra solo
# l'Authorization bearer del chiamante MCP.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SVC_ENV="service.env"
APP_DEFAULT_URL="http://ozon-env-app:8000"

env_get() { local f="$1" k="$2"; [ -f "$f" ] || return 0; grep -E "^${k}=" "$f" | tail -1 | cut -d= -f2-; }
env_set() {
  local k="$1" v="$2"; touch "$SVC_ENV"
  grep -qE "^${k}=" "$SVC_ENV" && { grep -vE "^${k}=" "$SVC_ENV" > "$SVC_ENV.tmp" && mv "$SVC_ENV.tmp" "$SVC_ENV"; }
  printf '%s=%s\n' "$k" "$v" >> "$SVC_ENV"
}
have() { [ -n "${1:-}" ]; }

[ -f "$SVC_ENV" ] || cp service.env.example "$SVC_ENV"

BASE_URL="$(env_get "$SVC_ENV" MCP_SEARCH_OZON_BASE_URL)"
if ! have "$BASE_URL"; then
  read -rp "MCP_SEARCH_OZON_BASE_URL [$APP_DEFAULT_URL]: " BASE_URL
  have "$BASE_URL" || BASE_URL="$APP_DEFAULT_URL"
  env_set MCP_SEARCH_OZON_BASE_URL "$BASE_URL"
fi

echo ""
echo "Config pronta ($SVC_ENV). Avvio compose..."
docker compose --env-file "$SVC_ENV" up -d --build
echo "mcp_search avviato. Log: docker logs -f ozon-env-mcp-search"
