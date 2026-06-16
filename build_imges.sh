#!/bin/bash
cd database
./build_images.sh
cd ..
docker build --rm . --no-cache --build-arg TZ="Europe/Rome" --network host -t ozonapp.app:latest
docker build --rm ./services/mail_sender --no-cache --network host -t ozonapp.mail_sender:latest
docker build --rm ./services/calendar_scheduler --no-cache --network host -t ozonapp.calendar_scheduler:latest