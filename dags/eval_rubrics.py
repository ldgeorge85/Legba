"""
Eval Rubrics DAG — automated quality checks.

Runs the quantitative eval rubrics from EVALUATION_RUBRICS.md and logs results.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

DB_DSN = "postgresql://legba:legba@postgres:5432/legba"
METRICS_DSN = "postgresql://legba_metrics:legba_metrics@timescaledb:5432/legba_metrics"

default_args = {
    "owner": "legba",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


def eval_event_dedup(**context):
    """Check for duplicate events (exact title match within 7 days)."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("""
        SELECT count(*) FROM (
            SELECT lower(title) FROM events
            WHERE created_at > NOW() - INTERVAL '7 days'
            GROUP BY lower(title) HAVING count(*) > 1
        ) sub
    """)
    dupes = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM events WHERE created_at > NOW() - INTERVAL '7 days'")
    total = cur.fetchone()[0]

    rate = (dupes / total * 100) if total > 0 else 0
    status = "PASS" if rate < 3 else "FAIL"
    print(f"Event dedup: {dupes} duplicate titles / {total} events = {rate:.1f}% [{status}] (target <3%)")

    cur.close()
    conn.close()

    # Write result to TimescaleDB
    _write_eval_metric("eval_event_dedup_rate", rate)
    return {"dupes": dupes, "total": total, "rate": rate, "status": status}


def eval_graph_quality(**context):
    """Check graph RelatedTo edges and isolated nodes."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("LOAD 'age'")
    cur.execute("SET search_path = ag_catalog, public")

    cur.execute("SELECT * FROM cypher('legba_graph', $$ MATCH ()-[r]->() RETURN count(r) $$) AS (cnt agtype)")
    total_edges = int(cur.fetchone()[0])

    cur.execute("SELECT * FROM cypher('legba_graph', $$ MATCH ()-[r:RelatedTo]->() RETURN count(r) $$) AS (cnt agtype)")
    related_to = int(cur.fetchone()[0])

    cur.execute("SELECT * FROM cypher('legba_graph', $$ MATCH (n) RETURN count(n) $$) AS (cnt agtype)")
    total_nodes = int(cur.fetchone()[0])

    cur.execute("SELECT * FROM cypher('legba_graph', $$ MATCH (n) WHERE NOT EXISTS { MATCH (n)-[]-() } RETURN count(n) $$) AS (cnt agtype)")
    isolated = int(cur.fetchone()[0])

    rt_rate = (related_to / total_edges * 100) if total_edges > 0 else 0
    iso_rate = (isolated / total_nodes * 100) if total_nodes > 0 else 0

    print(f"Graph: {total_nodes} nodes, {total_edges} edges")
    print(f"  RelatedTo: {related_to}/{total_edges} = {rt_rate:.1f}% [{'PASS' if rt_rate < 5 else 'FAIL'}]")
    print(f"  Isolated: {isolated}/{total_nodes} = {iso_rate:.1f}% [{'PASS' if iso_rate < 5 else 'FAIL'}]")

    cur.close()
    conn.close()

    _write_eval_metric("eval_graph_related_to_pct", rt_rate)
    _write_eval_metric("eval_graph_isolated_pct", iso_rate)
    return {"nodes": total_nodes, "edges": total_edges, "related_to_pct": rt_rate, "isolated_pct": iso_rate}


def eval_source_health(**context):
    """Check source zero-signal rate."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM sources WHERE status = 'active'")
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT count(*) FROM sources
        WHERE status = 'active'
          AND id NOT IN (SELECT DISTINCT source_id FROM signals WHERE source_id IS NOT NULL)
    """)
    zero = cur.fetchone()[0]

    rate = (zero / total * 100) if total > 0 else 0
    status = "PASS" if rate < 10 else "FAIL"
    print(f"Source health: {zero}/{total} zero-signal sources = {rate:.1f}% [{status}] (target <10%)")

    cur.close()
    conn.close()

    _write_eval_metric("eval_source_zero_signal_pct", rate)
    return {"zero_signal": zero, "total_active": total, "rate": rate, "status": status}


def eval_entity_links(**context):
    """Check event_entity_links population."""
    import psycopg2

    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM event_entity_links")
    links = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM events")
    events = cur.fetchone()[0]

    avg = links / events if events > 0 else 0
    print(f"Entity links: {links} links across {events} events = {avg:.1f} avg links/event")

    cur.close()
    conn.close()

    _write_eval_metric("eval_entity_links_per_event", avg)
    return {"links": links, "events": events, "avg_per_event": avg}


def _write_eval_metric(metric: str, value: float):
    """Write an eval metric to TimescaleDB."""
    try:
        import psycopg2
        conn = psycopg2.connect(METRICS_DSN)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO metrics (time, metric, dimension, value) VALUES (NOW(), %s, 'eval', %s)",
            (metric, value),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Failed to write eval metric: {e}")


with DAG(
    dag_id="eval_rubrics",
    default_args=default_args,
    description="Automated quality evaluation rubrics",
    schedule_interval="0 */8 * * *",  # Every 8 hours
    start_date=datetime(2026, 3, 22),
    catchup=False,
    tags=["eval", "quality"],
) as dag:

    dedup = PythonOperator(
        task_id="eval_event_dedup",
        python_callable=eval_event_dedup,
    )

    graph = PythonOperator(
        task_id="eval_graph_quality",
        python_callable=eval_graph_quality,
    )

    sources = PythonOperator(
        task_id="eval_source_health",
        python_callable=eval_source_health,
    )

    links = PythonOperator(
        task_id="eval_entity_links",
        python_callable=eval_entity_links,
    )

    [dedup, graph, sources, links]
