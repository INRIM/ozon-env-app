#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker build \
  --rm \
  "${SCRIPT_DIR}" \
  --no-cache \
  --build-arg TZ="${TZ:-Europe/Rome}" \
  --network host \
  -f "${SCRIPT_DIR}/Dockerfile-mongo" \
  -t ozonapp.db:latest
