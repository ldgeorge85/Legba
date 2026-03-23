"""Differential accumulator for the subconscious service.

Tracks state changes between conscious cycles and writes a JSON
summary to Redis for the agent to consume at the start of each cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg
import redis.asyncio as aioredis

logger = logging.getLogger("legba.subconscious.differential")

# Redis key where the accumulated differential is stored
DIFFERENTIAL_KEY = "legba:subconscious:differential"

# Redis key for the last snapshot timestamp
SNAPSHOT_KEY = "legba:subconscious:last_snapshot"


class DifferentialAccumulator:
    """Accumulates state changes between conscious cycles.

    Tracks:
    - New signals per situation
    - Event lifecycle transitions
    - Entity anomalies
    - Fact changes
    - Hypothesis evidence changes
    - Watchlist matches

    The accumulated diff is written to Redis as JSON. The conscious agent
    reads and clears it at the start of each cycle.
    """

    def __init__(self, pg_pool: asyncpg.Pool, redis_client: aioredis.Redis):
        self._pg = pg_pool
        self._redis = redis_client
        self._last_snapshot: datetime | None = None

    async def initialize(self) -> None:
        """Load last snapshot timestamp from Redis."""
        raw = await self._redis.get(SNAPSHOT_KEY)
        if raw:
            try:
                self._last_snapshot = datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                self._last_snapshot = None

        if self._last_snapshot is None:
            self._last_snapshot = datetime.now(timezone.utc)
            await self._redis.set(
                SNAPSHOT_KEY,
                self._last_snapshot.isoformat(),
            )

        logger.info("Differential accumulator initialized, last snapshot: %s", self._last_snapshot)

    async def accumulate(self) -> dict[str, Any]:
        """Query all state changes since last snapshot and build differential.

        Returns the differential dict that gets written to Redis.
        """
        now = datetime.now(timezone.utc)
        since = self._last_snapshot or now

        # Run all queries concurrently-ish (sequential for safety with pool)
        new_signals = await self._query_new_signals(since)
        event_transitions = await self._query_event_transitions(since)
        entity_anomalies = await self._query_entity_anomalies(since)
        fact_changes = await self._query_fact_changes(since)
        hypothesis_changes = await self._query_hypothesis_changes(since)
        watchlist_matches = await self._query_watchlist_matches(since)

        differential = {
            "since": since.isoformat(),
            "until": now.isoformat(),
            "new_signals": new_signals,
            "event_transitions": event_transitions,
            "entity_anomalies": entity_anomalies,
            "fact_changes": fact_changes,
            "hypothesis_changes": hypothesis_changes,
            "watchlist_matches": watchlist_matches,
            "summary": {
                "new_signal_count": len(new_signals),
                "event_transition_count": len(event_transitions),
                "entity_anomaly_count": len(entity_anomalies),
                "fact_change_count": len(fact_changes),
                "hypothesis_change_count": len(hypothesis_changes),
                "watchlist_match_count": len(watchlist_matches),
            },
        }

        # Write to Redis
        await self._redis.set(DIFFERENTIAL_KEY, json.dumps(differential))

        # Update snapshot
        self._last_snapshot = now
        await self._redis.set(SNAPSHOT_KEY, now.isoformat())

        logger.info(
            "Differential accumulated: %d signals, %d events, %d entities, "
            "%d facts, %d hypotheses, %d watchlist",
            len(new_signals), len(event_transitions), len(entity_anomalies),
            len(fact_changes), len(hypothesis_changes), len(watchlist_matches),
        )

        return differential

    async def _query_new_signals(self, since: datetime) -> list[dict[str, Any]]:
        """Query new signals grouped by situation since last snapshot."""
        try:
            rows = await self._pg.fetch(
                """
                SELECT
                    s.id::text AS signal_id,
                    s.title,
                    s.category,
                    s.confidence,
                    s.created_at::text AS created_at,
                    COALESCE(
                        (SELECT sit.name
                         FROM situation_signals ss
                         JOIN situations sit ON ss.situation_id = sit.id
                         WHERE ss.signal_id = s.id
                         LIMIT 1),
                        'unlinked'
                    ) AS situation_name
                FROM signals s
                WHERE s.created_at > $1
                ORDER BY s.created_at DESC
                LIMIT 200
                """,
                since,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to query new signals: %s", exc)
            return []

    async def _query_event_transitions(self, since: datetime) -> list[dict[str, Any]]:
        """Query events that changed state since last snapshot."""
        try:
            rows = await self._pg.fetch(
                """
                SELECT
                    e.id::text AS event_id,
                    e.title,
                    e.event_type,
                    e.severity,
                    e.signal_count,
                    e.updated_at::text AS updated_at,
                    e.created_at::text AS created_at
                FROM events e
                WHERE e.updated_at > $1
                ORDER BY e.updated_at DESC
                LIMIT 50
                """,
                since,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to query event transitions: %s", exc)
            return []

    async def _query_entity_anomalies(self, since: datetime) -> list[dict[str, Any]]:
        """Query entity profiles with anomalous state changes."""
        try:
            # Find entities updated recently with low completeness or stale verification
            rows = await self._pg.fetch(
                """
                SELECT
                    ep.id::text AS entity_id,
                    ep.canonical_name,
                    ep.entity_type,
                    ep.completeness_score,
                    ep.last_verified_at::text AS last_verified_at,
                    ep.updated_at::text AS updated_at,
                    (SELECT COUNT(*) FROM signal_entity_links sel
                     WHERE sel.entity_id = ep.id
                       AND sel.created_at > $1) AS recent_link_count
                FROM entity_profiles ep
                WHERE ep.updated_at > $1
                  AND (
                    ep.completeness_score < 0.3
                    OR ep.last_verified_at IS NULL
                    OR ep.last_verified_at < NOW() - INTERVAL '7 days'
                  )
                ORDER BY ep.updated_at DESC
                LIMIT 30
                """,
                since,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to query entity anomalies: %s", exc)
            return []

    async def _query_fact_changes(self, since: datetime) -> list[dict[str, Any]]:
        """Query facts that were created, updated, or superseded since last snapshot."""
        try:
            rows = await self._pg.fetch(
                """
                SELECT
                    f.id::text AS fact_id,
                    f.subject,
                    f.predicate,
                    f.value,
                    f.confidence,
                    f.updated_at::text AS updated_at,
                    CASE
                        WHEN f.superseded_by IS NOT NULL THEN 'superseded'
                        WHEN f.created_at > $1 THEN 'created'
                        ELSE 'updated'
                    END AS change_type
                FROM facts f
                WHERE f.updated_at > $1
                   OR f.created_at > $1
                ORDER BY f.updated_at DESC
                LIMIT 50
                """,
                since,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to query fact changes: %s", exc)
            return []

    async def _query_hypothesis_changes(self, since: datetime) -> list[dict[str, Any]]:
        """Query hypotheses with recent evidence changes."""
        try:
            rows = await self._pg.fetch(
                """
                SELECT
                    h.id::text AS hypothesis_id,
                    h.thesis,
                    h.counter_thesis,
                    h.evidence_balance,
                    h.status,
                    h.updated_at::text AS updated_at,
                    array_length(h.supporting_signals, 1) AS supporting_count,
                    array_length(h.refuting_signals, 1) AS refuting_count
                FROM hypotheses h
                WHERE h.updated_at > $1
                ORDER BY h.updated_at DESC
                LIMIT 20
                """,
                since,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to query hypothesis changes: %s", exc)
            return []

    async def _query_watchlist_matches(self, since: datetime) -> list[dict[str, Any]]:
        """Query recent watchlist trigger matches."""
        try:
            rows = await self._pg.fetch(
                """
                SELECT
                    wt.id::text AS trigger_id,
                    wt.watch_name,
                    wt.event_title,
                    wt.priority,
                    wt.triggered_at::text AS triggered_at,
                    wt.match_reasons::text AS match_reasons
                FROM watch_triggers wt
                WHERE wt.triggered_at > $1
                ORDER BY wt.triggered_at DESC
                LIMIT 30
                """,
                since,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to query watchlist matches: %s", exc)
            return []
