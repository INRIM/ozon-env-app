#!/usr/bin/env bash
# Ferma mcp_search. Opzioni passate a `docker compose down` (es: -v).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

docker compose down "$@"
echo "mcp_search fermato."
