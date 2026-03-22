"""
Metrics Rollup DAG — hourly and daily aggregation in TimescaleDB.

Rolls up raw metrics into hourly/daily buckets for faster Grafana queries
and baseline comparisons.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

METRICS_DSN = "postgresql://legba_metrics:legba_metrics@timescaledb:5432/legba_metrics"

default_args = {
    "owner": "legba",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


def rollup_hourly(**context):
    """Create hourly aggregates for the previous hour."""
    import psycopg2

    conn = psycopg2.connect(METRICS_DSN)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO metrics (time, metric, dimension, value)
        SELECT
            time_bucket('1 hour', time) AS bucket,
            metric || '_hourly_avg' AS metric,
            dimension,
            avg(value) AS value
        FROM metrics
        WHERE time >= NOW() - INTERVAL '2 hours'
          AND time < NOW() - INTERVAL '1 hour'
          AND metric NOT LIKE '%%_hourly_%%'
          AND metric NOT LIKE '%%_daily_%%'
        GROUP BY bucket, metric, dimension
        ON CONFLICT DO NOTHING
    """)

    conn.commit()
    rows = cur.rowcount
    cur.close()
    conn.close()
    print(f"Hourly rollup: {rows} rows")
    return rows


def rollup_daily(**context):
    """Create daily aggregates for the previous day."""
    import psycopg2

    conn = psycopg2.connect(METRICS_DSN)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO metrics (time, metric, dimension, value)
        SELECT
            time_bucket('1 day', time) AS bucket,
            metric || '_daily_avg' AS metric,
            dimension,
            avg(value) AS value
        FROM metrics
        WHERE time >= NOW() - INTERVAL '2 days'
          AND time < NOW() - INTERVAL '1 day'
          AND metric NOT LIKE '%%_hourly_%%'
          AND metric NOT LIKE '%%_daily_%%'
        GROUP BY bucket, metric, dimension
        ON CONFLICT DO NOTHING
    """)

    conn.commit()
    rows = cur.rowcount
    cur.close()
    conn.close()
    print(f"Daily rollup: {rows} rows")
    return rows


with DAG(
    dag_id="metrics_rollup",
    default_args=default_args,
    description="Hourly and daily metric aggregation in TimescaleDB",
    schedule_interval="@hourly",
    start_date=datetime(2026, 3, 22),
    catchup=False,
    tags=["metrics", "timescaledb"],
) as dag:

    hourly = PythonOperator(
        task_id="rollup_hourly",
        python_callable=rollup_hourly,
    )

    daily = PythonOperator(
        task_id="rollup_daily",
        python_callable=rollup_daily,
    )

    hourly >> daily
