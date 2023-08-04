#!/bin/bash
echo "run App"
pip install --upgrade -e .
uvicorn app.main:app --reload --workers 1 --env-file /app/.env --host 0.0.0.0 --port 8000