"""Data integrity verification and evaluation rubrics.

Verifies evidence chains (events trace to signals, facts have evidence)
and computes quality metrics. Replaces the eval_rubrics Airflow DAG.

No LLM required — purely SQL-based.

Existing schema columns used:
  - events: id, data (JSONB), signal_count, created_at
  - signals: id, source_id, confidence, created_at
  - signal_event_links: signal_id, event_id
  - signal_entity_links: signal_id, entity_id
  - entity_profiles: id, canonical_name
  - facts: id, subject, confidence, superseded_by, valid_until, data
  - sources: id, status, consecutive_failures, fetch_success_count, fetch_failure_count
  - hypotheses: id, status, evidence_balance

TODO columns needed (not yet in schema):
  - facts.evidence_set UUID[] DEFAULT '{}'
    -- Signal IDs supporting the fact; needed for evidence chain verification
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("legba.maintenance.integrity")


class IntegrityVerifier:
    """Data integrity verification and evaluation rubrics."""

    def __init__(self, pg_pool: asyncpg.Pool, metrics_client=None):
        self._pool = pg_pool
        self._metrics = metrics_client

    async def integrity_verification(self) -> dict[str, int]:
        """Verify evidence chains and data consistency.

        Checks:
        1. Events with signal_count > 0 but no signal_event_links
        2. Events with signal_count mismatch vs actual links
        3. signal_event_links pointing to non-existent signals
        4. signal_entity_links pointing to non-existent entities or signals
        5. Facts with superseded_by pointing to non-existent facts
        6. Situations with stale event_count

        Returns a dict of {check_name: issue_count}.
        """
        issues: dict[str, int] = {}
        async with self._pool.acquire() as conn:
            # 1. Events claiming signals but having no links
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM events e
                WHERE e.signal_count > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM signal_event_links sel
                      WHERE sel.event_id = e.id
                  )
            """)
            issues["events_phantom_signal_count"] = count or 0

            # 2. Events with mismatched signal_count
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM (
                    SELECT e.id, e.signal_count,
                           (SELECT COUNT(*) FROM signal_event_links sel
                            WHERE sel.event_id = e.id) AS actual_count
                    FROM events e
                    WHERE e.signal_count > 0
                ) sub
                WHERE sub.signal_count != sub.actual_count
            """)
            issues["events_signal_count_mismatch"] = count or 0

            # Fix signal count mismatches
            if issues["events_signal_count_mismatch"] > 0:
                await conn.execute("""
                    UPDATE events e SET
                        signal_count = (
                            SELECT COUNT(*) FROM signal_event_links sel
                            WHERE sel.event_id = e.id
                        ),
                        updated_at = NOW()
                    WHERE signal_count != (
                        SELECT COUNT(*) FROM signal_event_links sel
                        WHERE sel.event_id = e.id
                    )
                """)
                logger.info(
                    "Fixed %d events with signal_count mismatch",
                    issues["events_signal_count_mismatch"],
                )

            # 3. Orphan signal_event_links (signal deleted)
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM signal_event_links sel
                WHERE NOT EXISTS (
                    SELECT 1 FROM signals s WHERE s.id = sel.signal_id
                )
            """)
            issues["orphan_signal_event_links"] = count or 0

            # Clean orphan signal_event_links
            if count and count > 0:
                await conn.execute("""
                    DELETE FROM signal_event_links sel
                    WHERE NOT EXISTS (
                        SELECT 1 FROM signals s WHERE s.id = sel.signal_id
                    )
                """)
                logger.info("Removed %d orphan signal_event_links", count)

            # 4. Orphan signal_entity_links
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM signal_entity_links sel
                WHERE NOT EXISTS (
                    SELECT 1 FROM signals s WHERE s.id = sel.signal_id
                )
            """)
            issues["orphan_signal_entity_links_signal"] = count or 0

            count = await conn.fetchval("""
                SELECT COUNT(*) FROM signal_entity_links sel
                WHERE NOT EXISTS (
                    SELECT 1 FROM entity_profiles ep WHERE ep.id = sel.entity_id
                )
            """)
            issues["orphan_signal_entity_links_entity"] = count or 0

            # 5. Facts with broken superseded_by references
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM facts f
                WHERE f.superseded_by IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM facts f2 WHERE f2.id = f.superseded_by
                  )
            """)
            issues["facts_broken_superseded_by"] = count or 0

            # Fix broken superseded_by
            if count and count > 0:
                await conn.execute("""
                    UPDATE facts SET superseded_by = NULL, updated_at = NOW()
                    WHERE superseded_by IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM facts f2 WHERE f2.id = facts.superseded_by
                      )
                """)
                logger.info("Fixed %d facts with broken superseded_by", count)

            # 6. Situation event_count drift
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM situations s
                WHERE s.event_count != (
                    SELECT COUNT(*) FROM situation_events se
                    WHERE se.situation_id = s.id
                )
            """)
            issues["situations_event_count_mismatch"] = count or 0

            if count and count > 0:
                await conn.execute("""
                    UPDATE situations s SET
                        event_count = (
                            SELECT COUNT(*) FROM situation_events se
                            WHERE se.situation_id = s.id
                        ),
                        updated_at = NOW()
                    WHERE event_count != (
                        SELECT COUNT(*) FROM situation_events se
                        WHERE se.situation_id = s.id
                    )
                """)
                logger.info("Fixed %d situations with event_count mismatch", count)

        total = sum(issues.values())
        if total > 0:
            logger.info("Integrity verification: %d total issues found — %s", total, issues)

        # Write issues to metrics
        if self._metrics and self._metrics.available:
            for check_name, count in issues.items():
                await self._metrics.write(
                    "maintenance_integrity", check_name, float(count),
                )

        return issues

    async def eval_rubrics(self) -> dict[str, float]:
        """Compute evaluation rubrics — replaces eval_rubrics Airflow DAG.

        Rubrics:
        1. Event dedup rate: fraction of signals that are linked to events
        2. Graph quality: avg entity links per event
        3. Source health: fraction of sources in active/healthy state
        4. Entity link coverage: fraction of signals with entity links
        5. Fact freshness: fraction of non-expired active facts
        6. Hypothesis balance: avg |evidence_balance| across active hypotheses

        Returns a dict of {rubric_name: score (0.0 to 1.0)}.
        """
        rubrics: dict[str, float] = {}
        async with self._pool.acquire() as conn:
            # 1. Event dedup rate (signal clustering coverage)
            total_signals = await conn.fetchval(
                "SELECT COUNT(*) FROM signals WHERE created_at > NOW() - INTERVAL '7 days'"
            ) or 0
            linked_signals = await conn.fetchval("""
                SELECT COUNT(DISTINCT sel.signal_id)
                FROM signal_event_links sel
                JOIN signals s ON s.id = sel.signal_id
                WHERE s.created_at > NOW() - INTERVAL '7 days'
            """) or 0
            rubrics["event_clustering_rate"] = (
                linked_signals / total_signals if total_signals > 0 else 0.0
            )

            # 2. Graph quality (avg entity links per event)
            total_events = await conn.fetchval("SELECT COUNT(*) FROM events") or 0
            total_event_entity_links = await conn.fetchval(
                "SELECT COUNT(*) FROM event_entity_links"
            ) or 0
            avg_links = (
                total_event_entity_links / total_events if total_events > 0 else 0.0
            )
            # Normalize: 3+ links per event = 1.0
            rubrics["graph_quality"] = min(avg_links / 3.0, 1.0)

            # 3. Source health
            total_sources = await conn.fetchval(
                "SELECT COUNT(*) FROM sources"
            ) or 0
            healthy_sources = await conn.fetchval(
                "SELECT COUNT(*) FROM sources WHERE status = 'active' AND consecutive_failures < 5"
            ) or 0
            rubrics["source_health"] = (
                healthy_sources / total_sources if total_sources > 0 else 0.0
            )

            # 4. Entity link coverage (signals with at least one entity link)
            signals_with_entities = await conn.fetchval("""
                SELECT COUNT(DISTINCT sel.signal_id)
                FROM signal_entity_links sel
                JOIN signals s ON s.id = sel.signal_id
                WHERE s.created_at > NOW() - INTERVAL '7 days'
            """) or 0
            rubrics["entity_link_coverage"] = (
                signals_with_entities / total_signals if total_signals > 0 else 0.0
            )

            # 5. Fact freshness
            total_facts = await conn.fetchval(
                "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL"
            ) or 0
            fresh_facts = await conn.fetchval("""
                SELECT COUNT(*) FROM facts
                WHERE superseded_by IS NULL
                  AND COALESCE(data->>'expired', 'false') != 'true'
                  AND (valid_until IS NULL OR valid_until > NOW())
            """) or 0
            rubrics["fact_freshness"] = (
                fresh_facts / total_facts if total_facts > 0 else 0.0
            )

            # 6. Hypothesis balance (lower is better — well-balanced hypotheses)
            avg_balance = await conn.fetchval("""
                SELECT AVG(ABS(evidence_balance))
                FROM hypotheses
                WHERE status = 'active'
            """)
            if avg_balance is not None:
                # Normalize: 0 imbalance = 1.0, 10+ imbalance = 0.0
                rubrics["hypothesis_balance"] = max(0.0, 1.0 - (float(avg_balance) / 10.0))
            else:
                rubrics["hypothesis_balance"] = 0.0

        logger.info(
            "Eval rubrics: %s",
            ", ".join(f"{k}={v:.3f}" for k, v in rubrics.items()),
        )

        # Write rubrics to TimescaleDB
        if self._metrics and self._metrics.available:
            points = [
                ("maintenance_rubric", name, score)
                for name, score in rubrics.items()
            ]
            await self._metrics.write_batch(points)

        return rubrics
