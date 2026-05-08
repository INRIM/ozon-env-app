#!/bin/bash
cd database
./buiild_images.sh
cd ..
docker build --rm . --no-cache --build-arg TZ="Europe/Rome" --network host -t ozonapp.app:latest