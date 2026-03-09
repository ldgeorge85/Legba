#!/bin/bash
set -e

# Initialize / migrate database
airflow db migrate

# Create admin user (skip if already exists)
airflow users create \
    --username "${AIRFLOW_ADMIN_USER:-airflow}" \
    --password "${AIRFLOW_ADMIN_PASSWORD:-airflow}" \
    --role Admin \
    --firstname Legba \
    --lastname Admin \
    --email admin@legba.local 2>/dev/null || true

# Start webserver in background
airflow webserver --port 8080 &

# Start scheduler (foreground — container stays alive)
exec airflow scheduler
