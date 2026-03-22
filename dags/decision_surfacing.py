"""
Decision Surfacing DAG — identify merge candidates, stale goals, dormant situations.

Runs periodic checks on data quality and surfaces items needing human or agent attention.
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


def find_stale_goals(**context):
    """Find goals not updated in >7 days."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("""
        SELECT id, data->>'title' as title, updated_at
        FROM goals
        WHERE data->>'status' = 'active'
          AND updated_at < NOW() - INTERVAL '7 days'
        ORDER BY updated_at ASC
    """)
    stale = cur.fetchall()
    for gid, title, updated in stale:
        print(f"Stale goal: {title} (last updated: {updated})")

    cur.close()
    conn.close()
    return len(stale)


def find_dormant_situations(**context):
    """Find active situations with no events in >5 days."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("""
        SELECT s.name, s.event_count, s.last_event_at, s.updated_at
        FROM situations s
        WHERE s.status = 'active'
          AND (s.last_event_at IS NULL OR s.last_event_at < NOW() - INTERVAL '5 days')
          AND s.updated_at < NOW() - INTERVAL '5 days'
        ORDER BY s.updated_at ASC
    """)
    dormant = cur.fetchall()
    for name, count, last_event, updated in dormant:
        print(f"Dormant situation: {name} ({count} events, last: {last_event})")

    # Auto-mark as dormant
    cur.execute("""
        UPDATE situations SET status = 'dormant'
        WHERE status = 'active'
          AND (last_event_at IS NULL OR last_event_at < NOW() - INTERVAL '10 days')
          AND updated_at < NOW() - INTERVAL '10 days'
    """)
    auto_dormant = cur.rowcount
    conn.commit()

    if auto_dormant:
        print(f"Auto-dormant: {auto_dormant} situations (no events in 10+ days)")

    cur.close()
    conn.close()
    return {"dormant_candidates": len(dormant), "auto_dormant": auto_dormant}


def find_entity_merge_candidates(**context):
    """Find entity profiles that look like duplicates (substring match)."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("""
        SELECT a.canonical_name, b.canonical_name, a.entity_type
        FROM entity_profiles a, entity_profiles b
        WHERE a.id < b.id
          AND a.entity_type = b.entity_type
          AND length(a.canonical_name) > 5
          AND length(b.canonical_name) > 5
          AND (a.canonical_name ILIKE '%%' || b.canonical_name || '%%'
               OR b.canonical_name ILIKE '%%' || a.canonical_name || '%%')
        LIMIT 20
    """)
    candidates = cur.fetchall()
    for name_a, name_b, etype in candidates:
        print(f"Merge candidate: '{name_a}' <-> '{name_b}' ({etype})")

    cur.close()
    conn.close()
    return len(candidates)


with DAG(
    dag_id="decision_surfacing",
    default_args=default_args,
    description="Surface stale goals, dormant situations, merge candidates",
    schedule_interval="0 */12 * * *",  # Every 12 hours
    start_date=datetime(2026, 3, 22),
    catchup=False,
    tags=["quality", "surfacing"],
) as dag:

    stale = PythonOperator(
        task_id="find_stale_goals",
        python_callable=find_stale_goals,
    )

    dormant = PythonOperator(
        task_id="find_dormant_situations",
        python_callable=find_dormant_situations,
    )

    merges = PythonOperator(
        task_id="find_entity_merge_candidates",
        python_callable=find_entity_merge_candidates,
    )

    [stale, dormant, merges]
