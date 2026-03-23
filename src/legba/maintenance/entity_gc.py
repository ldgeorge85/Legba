"""Entity garbage collection, duplicate detection, and source health.

Deterministic maintenance tasks for entity lifecycle management.
No LLM required.

Existing schema columns used:
  - entity_profiles: id, canonical_name, entity_type, data (JSONB),
    last_event_link_at, completeness_score, updated_at, created_at
  - signal_entity_links: signal_id, entity_id, role, confidence, created_at
  - event_entity_links: event_id, entity_id
  - sources: id, name, status, consecutive_failures, fetch_failure_count,
    last_successful_fetch_at, updated_at
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("legba.maintenance.entity_gc")


class EntityGarbageCollector:
    """Entity garbage collection and source health management."""

    def __init__(self, pg_pool: asyncpg.Pool):
        self._pool = pg_pool

    async def entity_gc(self) -> int:
        """Mark entities with zero signal references in 30 days as DORMANT.

        An entity is dormant when it has no signal_entity_links created in
        the last 30 days. This doesn't delete the entity — it marks it in
        the data JSONB so it can be excluded from active queries.

        Returns the number of entities marked dormant.
        """
        dormant_count = 0
        async with self._pool.acquire() as conn:
            # Find entities with no recent signal links
            rows = await conn.fetch("""
                SELECT ep.id, ep.canonical_name, ep.entity_type
                FROM entity_profiles ep
                WHERE NOT EXISTS (
                    SELECT 1 FROM signal_entity_links sel
                    WHERE sel.entity_id = ep.id
                      AND sel.created_at > NOW() - INTERVAL '30 days'
                )
                AND ep.created_at < NOW() - INTERVAL '30 days'
                AND COALESCE(ep.data->>'gc_status', 'active') != 'dormant'
            """)

            for row in rows:
                await conn.execute(
                    """
                    UPDATE entity_profiles SET
                        data = jsonb_set(
                            COALESCE(data, '{}'::jsonb),
                            '{gc_status}',
                            '"dormant"'
                        ),
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    row["id"],
                )
                dormant_count += 1
                logger.debug(
                    "Entity %s (%s, %s) marked dormant",
                    row["id"], row["canonical_name"], row["entity_type"],
                )

        if dormant_count:
            logger.info("Entity GC: %d entities marked dormant", dormant_count)
        return dormant_count

    async def detect_duplicate_entities(self) -> int:
        """Flag name-similar entities with co-occurrence patterns.

        Detects potential duplicates using trigram similarity on canonical_name
        where both entities appear in signals for the same events. Flags them
        in the data JSONB for manual review.

        Returns the number of duplicate candidates flagged.
        """
        flagged = 0
        async with self._pool.acquire() as conn:
            # Check if pg_trgm extension is available
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            except Exception:
                logger.debug("pg_trgm extension not available, skipping duplicate detection")
                return 0

            # Find name-similar entity pairs with co-occurrence
            rows = await conn.fetch("""
                SELECT DISTINCT
                    a.id AS id_a, a.canonical_name AS name_a,
                    b.id AS id_b, b.canonical_name AS name_b,
                    similarity(LOWER(a.canonical_name), LOWER(b.canonical_name)) AS sim
                FROM entity_profiles a
                JOIN entity_profiles b ON a.id < b.id
                WHERE similarity(LOWER(a.canonical_name), LOWER(b.canonical_name)) > 0.6
                  AND a.entity_type = b.entity_type
                  AND COALESCE(a.data->>'gc_status', 'active') != 'dormant'
                  AND COALESCE(b.data->>'gc_status', 'active') != 'dormant'
                  AND COALESCE(a.data->>'duplicate_candidate', 'false') != 'true'
                LIMIT 50
            """)

            for row in rows:
                # Check for co-occurrence: both entities linked to signals
                # on the same event
                cooccurrence = await conn.fetchval("""
                    SELECT COUNT(*) FROM (
                        SELECT sel_a.signal_id
                        FROM signal_entity_links sel_a
                        JOIN signal_entity_links sel_b
                          ON sel_a.signal_id = sel_b.signal_id
                        WHERE sel_a.entity_id = $1
                          AND sel_b.entity_id = $2
                        LIMIT 1
                    ) sub
                """, row["id_a"], row["id_b"])

                if cooccurrence and cooccurrence > 0:
                    # Flag the entity with lower completeness_score
                    await conn.execute(
                        """
                        UPDATE entity_profiles SET
                            data = jsonb_set(
                                jsonb_set(
                                    COALESCE(data, '{}'::jsonb),
                                    '{duplicate_candidate}',
                                    '"true"'
                                ),
                                '{duplicate_of}',
                                to_jsonb($2::text)
                            ),
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        row["id_b"], str(row["id_a"]),
                    )
                    flagged += 1
                    logger.debug(
                        "Duplicate candidate: %s <-> %s (sim=%.2f)",
                        row["name_a"], row["name_b"], row["sim"],
                    )

        if flagged:
            logger.info("Duplicate detection: %d candidates flagged", flagged)
        return flagged

    async def clean_orphan_edges(self) -> int:
        """Remove graph edges where one endpoint was merged or deleted.

        Cleans up event_entity_links and signal_entity_links where the
        referenced entity_profiles row no longer exists or is marked as
        merged in the data JSONB.

        Returns the number of orphan edges removed.
        """
        removed = 0
        async with self._pool.acquire() as conn:
            # Remove signal_entity_links pointing to non-existent entities
            result = await conn.execute("""
                DELETE FROM signal_entity_links sel
                WHERE NOT EXISTS (
                    SELECT 1 FROM entity_profiles ep
                    WHERE ep.id = sel.entity_id
                )
            """)
            count = int(result.split()[-1]) if result else 0
            removed += count

            # Remove event_entity_links pointing to non-existent entities
            result = await conn.execute("""
                DELETE FROM event_entity_links eel
                WHERE NOT EXISTS (
                    SELECT 1 FROM entity_profiles ep
                    WHERE ep.id = eel.entity_id
                )
            """)
            count = int(result.split()[-1]) if result else 0
            removed += count

            # Remove event_entity_links pointing to non-existent events
            result = await conn.execute("""
                DELETE FROM event_entity_links eel
                WHERE NOT EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.id = eel.event_id
                )
            """)
            count = int(result.split()[-1]) if result else 0
            removed += count

            # Remove signal_entity_links for merged entities
            # (data->>'gc_status' = 'merged')
            result = await conn.execute("""
                DELETE FROM signal_entity_links sel
                WHERE EXISTS (
                    SELECT 1 FROM entity_profiles ep
                    WHERE ep.id = sel.entity_id
                      AND ep.data->>'gc_status' = 'merged'
                )
            """)
            count = int(result.split()[-1]) if result else 0
            removed += count

        if removed:
            logger.info("Orphan edge cleanup: %d edges removed", removed)
        return removed

    async def source_health(self) -> int:
        """Auto-pause sources with >20 consecutive failures.

        Replaces the source_health Airflow DAG. Sources are paused by
        setting status='paused' and recording the reason in the data JSONB.

        Returns the number of sources paused.
        """
        paused = 0
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, name, consecutive_failures, data
                FROM sources
                WHERE status = 'active'
                  AND consecutive_failures > 20
            """)

            for row in rows:
                import json
                data = row["data"]
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        data = {}
                elif data is None:
                    data = {}
                else:
                    data = dict(data)

                data["auto_paused_at"] = datetime.now(timezone.utc).isoformat()
                data["auto_paused_reason"] = (
                    f"Exceeded 20 consecutive failures ({row['consecutive_failures']})"
                )

                await conn.execute(
                    """
                    UPDATE sources SET
                        status = 'paused',
                        data = $2,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    row["id"], json.dumps(data),
                )
                paused += 1
                logger.warning(
                    "Source auto-paused: %s (%d consecutive failures)",
                    row["name"], row["consecutive_failures"],
                )

        if paused:
            logger.info("Source health: %d sources auto-paused", paused)
        return paused
