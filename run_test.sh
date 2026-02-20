#!/bin/bash
echo "update system"
poetry update
echo "check code"
poetry run black ozonenv_app/**/*.py
#poetry run flake8 ozonenv_app/**/*.py
rm -rf tests/models
docker-compose down
docker-compose up -d
echo "run test"
docker-compose exec testapp /bin/bash tests.sh
echo "make project: Done."
