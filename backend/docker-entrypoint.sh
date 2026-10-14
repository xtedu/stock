#!/bin/sh
set -e

echo "running alembic migrations..."
alembic upgrade head

echo "starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
