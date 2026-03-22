"""
Source Health DAG — auto-pause dead sources.

Checks sources with high consecutive failure counts and pauses them.
Also reports source health stats.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

DB_DSN = "postgresql://legba:legba@postgres:5432/legba"

default_args = {
    "owner": "legba",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


def check_and_pause_dead_sources(**context):
    """Pause sources with >20 consecutive failures."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    # Find sources with excessive failures
    cur.execute("""
        SELECT id, name, consecutive_failures, last_successful_fetch_at
        FROM sources
        WHERE status = 'active'
          AND consecutive_failures > 20
    """)
    dead = cur.fetchall()

    paused = []
    for src_id, name, failures, last_success in dead:
        cur.execute(
            "UPDATE sources SET status = 'paused' WHERE id = %s",
            (src_id,),
        )
        paused.append(f"{name} ({failures} failures)")
        print(f"Paused: {name} — {failures} consecutive failures, last success: {last_success}")

    conn.commit()

    # Report overall health
    cur.execute("""
        SELECT
            count(*) FILTER (WHERE status = 'active') as active,
            count(*) FILTER (WHERE status = 'paused') as paused,
            count(*) FILTER (WHERE status = 'active' AND consecutive_failures > 5) as struggling,
            count(*) FILTER (WHERE status = 'active' AND consecutive_failures = 0) as healthy
        FROM sources
    """)
    stats = cur.fetchone()
    print(f"Source health: {stats[3]} healthy, {stats[2]} struggling, {stats[0]} active, {stats[1]} paused")

    cur.close()
    conn.close()
    return {"paused": paused, "stats": {"active": stats[0], "paused": stats[1], "struggling": stats[2], "healthy": stats[3]}}


def report_zero_signal_sources(**context):
    """Report active sources that have produced 0 signals."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("""
        SELECT s.name, s.fetch_success_count, s.fetch_failure_count
        FROM sources s
        WHERE s.status = 'active'
          AND s.id NOT IN (SELECT DISTINCT source_id FROM signals WHERE source_id IS NOT NULL)
        ORDER BY s.name
    """)
    zero = cur.fetchall()
    for name, successes, failures in zero:
        print(f"Zero signals: {name} (fetches: {successes} ok, {failures} fail)")

    cur.close()
    conn.close()
    return len(zero)


with DAG(
    dag_id="source_health",
    default_args=default_args,
    description="Monitor source health and auto-pause dead sources",
    schedule_interval="0 */6 * * *",  # Every 6 hours
    start_date=datetime(2026, 3, 22),
    catchup=False,
    tags=["sources", "health"],
) as dag:

    pause_dead = PythonOperator(
        task_id="check_and_pause_dead_sources",
        python_callable=check_and_pause_dead_sources,
    )

    zero_report = PythonOperator(
        task_id="report_zero_signal_sources",
        python_callable=report_zero_signal_sources,
    )

    pause_dead >> zero_report
