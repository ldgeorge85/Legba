"""Extended metric collection for Grafana dashboards.

Collects operational metrics beyond what the ingestion service and agent
produce. Written to TimescaleDB via the shared MetricsClient.

No LLM required — purely SQL-based.

Existing schema columns used:
  - signals: id, source_id, category, confidence, created_at
  - events: id, data (JSONB), signal_count, category, severity, created_at, updated_at
  - entity_profiles: id, entity_type, completeness_score
  - entity_profile_versions: id, entity_id, version
  - signal_entity_links: signal_id, entity_id
  - event_entity_links: event_id, entity_id
  - facts: id, confidence, superseded_by, valid_until, data (JSONB)
  - hypotheses: id, status, evidence_balance, updated_at
  - sources: id, status, reliability
  - situations: id, status, event_count, intensity_score
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("legba.maintenance.metrics")


class MetricCollector:
    """Extended metric collection for TimescaleDB / Grafana."""

    def __init__(self, pg_pool: asyncpg.Pool, metrics_client=None):
        self._pool = pg_pool
        self._metrics = metrics_client

    async def metric_collection(self) -> None:
        """Collect and write extended metrics to TimescaleDB.

        Metrics collected:
        1. Signal velocity per source (signals/hour in last 24h)
        2. Signal velocity per category
        3. Event lifecycle distribution
        4. Entity graph size and growth
        5. Hypothesis stability
        6. Fact confidence distribution
        7. Situation activity
        """
        if not self._metrics or not self._metrics.available:
            logger.debug("Metrics client not available, skipping collection")
            return

        points: list[tuple[str, str, float]] = []

        async with self._pool.acquire() as conn:
            # 1. Signal velocity per source (signals/hour, last 24h)
            rows = await conn.fetch("""
                SELECT src.name, COUNT(s.id)::float / 24.0 AS velocity
                FROM signals s
                JOIN sources src ON s.source_id = src.id
                WHERE s.created_at > NOW() - INTERVAL '24 hours'
                GROUP BY src.name
                ORDER BY velocity DESC
                LIMIT 50
            """)
            for row in rows:
                points.append(("signal_velocity_source", row["name"], row["velocity"]))

            # 2. Signal velocity per category
            rows = await conn.fetch("""
                SELECT category, COUNT(*)::float / 24.0 AS velocity
                FROM signals
                WHERE created_at > NOW() - INTERVAL '24 hours'
                GROUP BY category
            """)
            for row in rows:
                points.append(("signal_velocity_category", row["category"], row["velocity"]))

            # 3. Event lifecycle distribution
            # Using data->>'lifecycle_status' since the dedicated column doesn't exist yet
            rows = await conn.fetch("""
                SELECT COALESCE(data->>'lifecycle_status', 'emerging') AS status,
                       COUNT(*) AS count
                FROM events
                GROUP BY status
            """)
            for row in rows:
                points.append(("event_lifecycle_distribution", row["status"], float(row["count"])))

            # Total events
            total_events = await conn.fetchval("SELECT COUNT(*) FROM events") or 0
            points.append(("events_total", "all", float(total_events)))

            # Events created in last 24h
            recent_events = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE created_at > NOW() - INTERVAL '24 hours'"
            ) or 0
            points.append(("events_created_24h", "all", float(recent_events)))

            # 4. Entity graph size
            entity_count = await conn.fetchval("SELECT COUNT(*) FROM entity_profiles") or 0
            points.append(("entity_count", "all", float(entity_count)))

            # Entity count by type
            rows = await conn.fetch("""
                SELECT entity_type, COUNT(*) AS count
                FROM entity_profiles
                GROUP BY entity_type
            """)
            for row in rows:
                points.append(("entity_count_by_type", row["entity_type"], float(row["count"])))

            # Entity links total
            sel_count = await conn.fetchval("SELECT COUNT(*) FROM signal_entity_links") or 0
            points.append(("signal_entity_links_total", "all", float(sel_count)))

            eel_count = await conn.fetchval("SELECT COUNT(*) FROM event_entity_links") or 0
            points.append(("event_entity_links_total", "all", float(eel_count)))

            # Entity versions (profile evolution activity)
            recent_versions = await conn.fetchval("""
                SELECT COUNT(*) FROM entity_profile_versions
                WHERE created_at > NOW() - INTERVAL '24 hours'
            """) or 0
            points.append(("entity_versions_24h", "all", float(recent_versions)))

            # 5. Hypothesis stability
            rows = await conn.fetch("""
                SELECT status, COUNT(*) AS count, AVG(ABS(evidence_balance)) AS avg_balance
                FROM hypotheses
                GROUP BY status
            """)
            for row in rows:
                points.append(("hypothesis_count", row["status"], float(row["count"])))
                if row["avg_balance"] is not None:
                    points.append(("hypothesis_avg_balance", row["status"], float(row["avg_balance"])))

            # Hypotheses evaluated in last 24h
            recently_evaluated = await conn.fetchval("""
                SELECT COUNT(*) FROM hypotheses
                WHERE updated_at > NOW() - INTERVAL '24 hours'
            """) or 0
            points.append(("hypotheses_evaluated_24h", "all", float(recently_evaluated)))

            # 6. Fact confidence distribution (buckets)
            for low, high, label in [
                (0.0, 0.25, "very_low"),
                (0.25, 0.5, "low"),
                (0.5, 0.75, "medium"),
                (0.75, 1.01, "high"),
            ]:
                count = await conn.fetchval("""
                    SELECT COUNT(*) FROM facts
                    WHERE superseded_by IS NULL
                      AND COALESCE(data->>'expired', 'false') != 'true'
                      AND confidence >= $1 AND confidence < $2
                """, low, high) or 0
                points.append(("fact_confidence_distribution", label, float(count)))

            # Total active facts
            total_facts = await conn.fetchval("""
                SELECT COUNT(*) FROM facts
                WHERE superseded_by IS NULL
                  AND COALESCE(data->>'expired', 'false') != 'true'
            """) or 0
            points.append(("facts_active_total", "all", float(total_facts)))

            # 7. Situation activity
            rows = await conn.fetch("""
                SELECT status, COUNT(*) AS count,
                       AVG(event_count) AS avg_events,
                       AVG(intensity_score) AS avg_intensity
                FROM situations
                GROUP BY status
            """)
            for row in rows:
                points.append(("situation_count", row["status"], float(row["count"])))
                if row["avg_events"] is not None:
                    points.append(("situation_avg_events", row["status"], float(row["avg_events"])))
                if row["avg_intensity"] is not None:
                    points.append(("situation_avg_intensity", row["status"], float(row["avg_intensity"])))

            # Signal totals
            total_signals = await conn.fetchval("SELECT COUNT(*) FROM signals") or 0
            points.append(("signals_total", "all", float(total_signals)))

            signals_24h = await conn.fetchval(
                "SELECT COUNT(*) FROM signals WHERE created_at > NOW() - INTERVAL '24 hours'"
            ) or 0
            points.append(("signals_24h", "all", float(signals_24h)))

            # Source stats
            rows = await conn.fetch("""
                SELECT status, COUNT(*) AS count
                FROM sources
                GROUP BY status
            """)
            for row in rows:
                points.append(("source_count", row["status"], float(row["count"])))

        # Write all metrics in one batch
        if points:
            await self._metrics.write_batch(points)
            logger.debug("Wrote %d metric points to TimescaleDB", len(points))
