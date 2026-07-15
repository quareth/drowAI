#!/bin/bash
# DrowAI Backend Docker Entrypoint
# Runs database migrations before starting the application

set -e

echo "========================================="
echo "DrowAI Backend Starting..."
echo "========================================="

if [ -n "${DROWAI_CONFIG_DIR:-}" ] || [ -n "${DROWAI_SECRETS_DIR:-}" ]; then
    echo "Loading generated deployment config..."
    eval "$(
        python -m backend.config_bootstrap print-env \
            --profile "${DROWAI_DEPLOYMENT_PROFILE:-single_host}" \
            --docker \
            --postgres-host "${POSTGRES_HOST:-postgres}"
    )"
fi

# Wait for database to be ready (healthcheck should handle this, but be safe)
echo "Checking database connectivity..."
python -c "
import os
import time
from sqlalchemy import create_engine, text

url = os.environ.get('DATABASE_URL', '')
if url.startswith('postgres://'):
    url = url.replace('postgres://', 'postgresql://', 1)

for i in range(30):
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        print('Database is ready!')
        break
    except Exception:
        print(f'Waiting for database... ({i+1}/30)')
        time.sleep(1)
else:
    print('ERROR: Could not connect to database after 30 seconds')
    exit(1)
"

# Run Alembic migrations
echo "Running database migrations..."
cd /app/backend
echo "Applying pending migrations..."
alembic upgrade head

echo "Migrations complete!"
echo "========================================="

# Start the application
echo "Starting uvicorn..."
cd /app

UVICORN_ARGS=(backend.main:app --host 0.0.0.0 --port 8000)
if [ "${DROWAI_UVICORN_RELOAD:-false}" = "true" ]; then
    UVICORN_ARGS+=(--reload)
fi

exec uvicorn "${UVICORN_ARGS[@]}"
