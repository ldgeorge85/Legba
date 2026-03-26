"""Nexus temporal management — confidence decay for stale nexuses.

Nexuses not evidenced (updated) in 30 days get confidence decremented,
mirroring the fact_decay pattern. No LLM required — purely SQL-based.

Schema columns used:
  - nexuses: id, confidence, evidence_count, created_at, valid_from,
    valid_until, nexus_type, actor_entity, target_entity
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger("legba.maintenance.nexus_decay")


class NexusDecayManager:
    """Nexus temporal management — confidence decay for stale nexuses."""

    def __init__(self, pg_pool: asyncpg.Pool):
        self._pool = pg_pool

    async def confidence_decay(self) -> int:
        """Nexuses not evidenced in 30 days get confidence decremented.

        Reduces confidence by 0.05 per maintenance cycle (capped at floor
        of 0.1). Only affects nexuses that:
        - Have confidence > 0.1
        - Haven't had their created_at updated in 30 days
        - Have no valid_until set, or valid_until is still in the future

        Returns the number of nexuses with decayed confidence.
        """
        decayed = 0
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE nexuses SET
                    confidence = GREATEST(confidence - 0.05, 0.1)
                WHERE confidence > 0.1
                  AND created_at < NOW() - INTERVAL '30 days'
                  AND (valid_until IS NULL OR valid_until > NOW())
            """)
            decayed = int(result.split()[-1]) if result else 0

        if decayed:
            logger.info(
                "Nexus confidence decay: %d nexuses had confidence reduced",
                decayed,
            )
        return decayed
