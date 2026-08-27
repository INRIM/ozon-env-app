#!/bin/bash
set -euo pipefail

# IMAGE_TAG: tag delle immagini, deve combaciare con quello usato nei compose
# (default `latest`). Serve per tenere separate le immagini di due istanze
# sullo stesso host.
IMAGE_TAG="${IMAGE_TAG:-latest}"
# MONGO_VERSION: versione del server Mongo dell'immagine ozonapp.db
# (default in database/Dockerfile-mongo).
MONGO_VERSION="${MONGO_VERSION:-}"
export IMAGE_TAG MONGO_VERSION

cd database
./build_images.sh
cd ..
docker build --rm . --no-cache --build-arg TZ="Europe/Rome" --network host \
  -t "ozonapp.app:${IMAGE_TAG}"
