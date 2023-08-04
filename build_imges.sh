#!/bin/bash
docker build --rm . --build-arg TZ="Europe/Rome" --network host -t ozonapp.tests:latest