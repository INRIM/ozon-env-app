#!/usr/bin/env bash
# Avvia il keycloak-manager interattivo (pipeline passo-passo).
# Genera ./out/kc-env.var con le env var da incollare nell'.env del consumer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p out
[ -f .env ] || { echo "manca .env (copia da .env.example)"; exit 1; }

docker compose run --rm keycloak-manager
