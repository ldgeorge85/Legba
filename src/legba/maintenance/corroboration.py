"""Signal corroboration scoring.

For recently clustered events, count independent sources contributing
signals and update a corroboration score. Independent means different
source_id on signals linked to the same event.

No LLM required — purely SQL-based.

Existing schema columns used:
  - events: id, data (JSONB), signal_count, updated_at
  - signal_event_links: signal_id, event_id, created_at
  - signals: id, source_id, confidence, data (JSONB)

TODO columns needed (not yet in schema):
  - signals.confidence_components JSONB DEFAULT '{}'
    -- Breakdown: {base: float, corroboration: float, source_reliability: float}
    -- corroboration component is what this module writes
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("legba.maintenance.corroboration")


class CorroborationScorer:
    """Compute corroboration scores for recently clustered events."""

    def __init__(self, pg_pool: asyncpg.Pool):
        self._pool = pg_pool

    async def corroboration_scoring(self) -> int:
        """For recently clustered events, count independent sources.

        An event's corroboration score is based on the number of distinct
        source_ids among its linked signals. The score is stored in the
        event's data JSONB.

        Scoring formula:
          - 1 source  -> corroboration = 0.0 (uncorroborated)
          - 2 sources -> corroboration = 0.3
          - 3 sources -> corroboration = 0.5
          - 4 sources -> corroboration = 0.7
          - 5+ sources -> corroboration = 0.9

        Also updates the corroboration component in each linked signal's
        data JSONB (best-effort, since confidence_components column doesn't
        exist yet).

        Returns the number of events scored.
        """
        scored = 0
        async with self._pool.acquire() as conn:
            # Find events that have received new signals in the last window
            # (configurable, default 10 minutes from the corroboration_interval)
            rows = await conn.fetch("""
                SELECT DISTINCT e.id, e.data, e.signal_count
                FROM events e
                JOIN signal_event_links sel ON sel.event_id = e.id
                WHERE sel.created_at > NOW() - INTERVAL '15 minutes'
            """)

            for row in rows:
                event_id = row["id"]

                # Count distinct sources
                source_count = await conn.fetchval("""
                    SELECT COUNT(DISTINCT s.source_id)
                    FROM signals s
                    JOIN signal_event_links sel ON s.id = sel.signal_id
                    WHERE sel.event_id = $1
                      AND s.source_id IS NOT NULL
                """, event_id)

                source_count = source_count or 0

                # Calculate corroboration score
                if source_count <= 1:
                    corroboration = 0.0
                elif source_count == 2:
                    corroboration = 0.3
                elif source_count == 3:
                    corroboration = 0.5
                elif source_count == 4:
                    corroboration = 0.7
                else:
                    corroboration = 0.9

                # Update event data JSONB with corroboration info
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

                data["corroboration_score"] = corroboration
                data["corroboration_sources"] = source_count
                data["corroboration_updated_at"] = datetime.now(timezone.utc).isoformat()

                await conn.execute(
                    "UPDATE events SET data = $2, updated_at = NOW() WHERE id = $1",
                    event_id, json.dumps(data),
                )

                # Update corroboration component on linked signals
                # TODO: When confidence_components JSONB column exists on signals,
                # update signals.confidence_components->'corroboration' directly
                # instead of using signals.data
                signal_ids = await conn.fetch("""
                    SELECT sel.signal_id
                    FROM signal_event_links sel
                    WHERE sel.event_id = $1
                """, event_id)

                for sig_row in signal_ids:
                    try:
                        await conn.execute(
                            """
                            UPDATE signals SET
                                data = jsonb_set(
                                    COALESCE(data, '{}'::jsonb),
                                    '{corroboration}',
                                    $2::jsonb
                                ),
                                updated_at = NOW()
                            WHERE id = $1
                            """,
                            sig_row["signal_id"],
                            json.dumps({
                                "score": corroboration,
                                "independent_sources": source_count,
                                "event_id": str(event_id),
                            }),
                        )
                    except Exception as e:
                        logger.debug(
                            "Failed to update corroboration on signal %s: %s",
                            sig_row["signal_id"], e,
                        )

                scored += 1
                logger.debug(
                    "Event %s: corroboration=%.1f (%d independent sources)",
                    event_id, corroboration, source_count,
                )

        if scored:
            logger.info("Corroboration: %d events scored", scored)
        return scored
