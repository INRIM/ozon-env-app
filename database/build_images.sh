#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="${IMAGE_TAG:-latest}"
# Deve combaciare col default in Dockerfile-mongo.
MONGO_VERSION="${MONGO_VERSION:-8.2.12}"

docker build \
  --rm \
  "${SCRIPT_DIR}" \
  --no-cache \
  --build-arg TZ="${TZ:-Europe/Rome}" \
  --build-arg MONGO_VERSION="${MONGO_VERSION}" \
  --network host \
  -f "${SCRIPT_DIR}/Dockerfile-mongo" \
  -t "ozonapp.db:${IMAGE_TAG}"
