"""Fact temporal management — expiration and confidence decay.

Deterministic fact lifecycle maintenance. No LLM required.

Schema columns used:
  - facts: id, subject, predicate, value, confidence, source_cycle,
    source_type, data (JSONB), valid_from, valid_until, created_at,
    updated_at, superseded_by, confidence_components (JSONB),
    evidence_set (JSONB)
  - signal_event_links: signal_id, event_id, created_at
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("legba.maintenance.fact_decay")


class FactDecayManager:
    """Fact temporal management — expiration and confidence decay."""

    def __init__(self, pg_pool: asyncpg.Pool):
        self._pool = pg_pool

    async def fact_decay(self) -> int:
        """Walk facts, identify past valid_until, mark stale.

        Two operations:
        1. Facts with explicit valid_until in the past -> mark as expired
           (set superseded_by = NULL to distinguish from replaced facts,
            and record expiry in data JSONB)
        2. Open-ended facts (valid_until IS NULL) with no supporting signals
           in 30 days -> set valid_until = NOW()

        Returns the number of facts expired or closed.
        """
        expired_count = 0
        async with self._pool.acquire() as conn:
            # --- Explicit valid_until expired ---
            result = await conn.execute("""
                UPDATE facts SET
                    data = jsonb_set(
                        COALESCE(data, '{}'::jsonb),
                        '{expired}',
                        '"true"'
                    ),
                    updated_at = NOW()
                WHERE valid_until IS NOT NULL
                  AND valid_until < NOW()
                  AND superseded_by IS NULL
                  AND COALESCE(data->>'expired', 'false') != 'true'
            """)
            count = int(result.split()[-1]) if result else 0
            expired_count += count
            if count:
                logger.info("Fact decay: %d facts with past valid_until marked expired", count)

            # --- Open-ended facts with no recent supporting signals ---
            # Find facts that have valid_until IS NULL, are older than 30 days,
            # and whose evidence_set signals have no recent activity.
            # For facts with evidence_set populated, check those specific signals.
            # For facts without evidence_set, fall back to subject-matching heuristic.
            rows = await conn.fetch("""
                SELECT f.id, f.subject, f.predicate, f.value, f.confidence,
                       f.created_at, f.data, f.evidence_set
                FROM facts f
                WHERE f.valid_until IS NULL
                  AND f.superseded_by IS NULL
                  AND f.created_at < NOW() - INTERVAL '30 days'
                  AND f.updated_at < NOW() - INTERVAL '30 days'
                  AND COALESCE(f.data->>'expired', 'false') != 'true'
                LIMIT 500
            """)

            for row in rows:
                evidence = row["evidence_set"]
                # Parse evidence_set JSONB (stored as JSON array of UUIDs)
                evidence_ids = []
                if evidence:
                    if isinstance(evidence, list):
                        evidence_ids = evidence
                    elif isinstance(evidence, str):
                        try:
                            import json as _json
                            evidence_ids = _json.loads(evidence)
                        except (ValueError, TypeError):
                            pass

                if evidence_ids:
                    # Use evidence_set: check if any linked signals were ingested recently
                    import uuid as _uuid
                    uuids = []
                    for eid in evidence_ids:
                        try:
                            uuids.append(_uuid.UUID(str(eid)) if not isinstance(eid, _uuid.UUID) else eid)
                        except (ValueError, TypeError):
                            continue
                    if uuids:
                        recent_signal = await conn.fetchval("""
                            SELECT EXISTS (
                                SELECT 1 FROM signals s
                                WHERE s.id = ANY($1)
                                  AND s.created_at > NOW() - INTERVAL '30 days'
                                LIMIT 1
                            )
                        """, uuids)
                    else:
                        recent_signal = False
                else:
                    # Fallback: subject-matching heuristic for facts without evidence_set
                    recent_signal = await conn.fetchval("""
                        SELECT EXISTS (
                            SELECT 1 FROM signals s
                            WHERE s.created_at > NOW() - INTERVAL '30 days'
                              AND (
                                  s.title ILIKE '%' || $1 || '%'
                                  OR s.data::text ILIKE '%' || $1 || '%'
                              )
                            LIMIT 1
                        )
                    """, row["subject"])

                if not recent_signal:
                    await conn.execute(
                        """
                        UPDATE facts SET
                            valid_until = NOW(),
                            data = jsonb_set(
                                COALESCE(data, '{}'::jsonb),
                                '{auto_closed_reason}',
                                '"no_supporting_signals_30d"'
                            ),
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        row["id"],
                    )
                    expired_count += 1
                    logger.debug(
                        "Fact auto-closed: %s | %s | %s (no signals in 30d)",
                        row["subject"], row["predicate"], row["value"][:60],
                    )

        if expired_count:
            logger.info("Fact decay: %d total facts expired/closed", expired_count)
        return expired_count

    async def confidence_decay(self) -> int:
        """Facts with no corroboration in 30 days get confidence decremented.

        Reduces confidence by 0.05 per maintenance cycle (capped at floor of 0.1).
        Only affects facts that:
        - Are not already superseded
        - Have confidence > 0.1
        - Haven't been updated in 30 days
        - Are not already expired

        Updates both the scalar confidence and the confidence_components.decay
        field for full audit trail.

        Returns the number of facts with decayed confidence.
        """
        decayed = 0
        async with self._pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE facts SET
                    confidence = GREATEST(confidence - 0.05, 0.1),
                    confidence_components = jsonb_set(
                        COALESCE(confidence_components, '{}'::jsonb),
                        '{decay}',
                        to_jsonb(
                            COALESCE((confidence_components->>'decay')::numeric, 0.0) - 0.05
                        )
                    ),
                    data = jsonb_set(
                        COALESCE(data, '{}'::jsonb),
                        '{last_confidence_decay}',
                        to_jsonb(NOW()::text)
                    ),
                    updated_at = NOW()
                WHERE superseded_by IS NULL
                  AND confidence > 0.1
                  AND updated_at < NOW() - INTERVAL '30 days'
                  AND COALESCE(data->>'expired', 'false') != 'true'
                  AND (valid_until IS NULL OR valid_until > NOW())
            """)
            decayed = int(result.split()[-1]) if result else 0

        if decayed:
            logger.info("Confidence decay: %d facts had confidence reduced", decayed)
        return decayed
