#!/bin/bash
docker build --rm . --no-cache --build-arg TZ="Europe/Rome" --network host -t ozonapp.api:latest