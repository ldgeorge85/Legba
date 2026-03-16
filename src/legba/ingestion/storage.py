"""Storage layer — batch writes to Postgres + OpenSearch.

Handles event persistence, source tracking, ingestion logging, and
metrics publishing to Redis.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg

from legba.shared.schemas.events import Event

logger = logging.getLogger(__name__)


class StorageLayer:
    """Batch event storage with dual-write (Postgres + OpenSearch)."""

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        os_client=None,
        redis_client=None,
    ):
        self._pool = pg_pool
        self._os = os_client
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Event storage
    # ------------------------------------------------------------------

    async def store_event(self, event: Event) -> bool:
        """Store a single event in Postgres + OpenSearch."""
        pg_ok = await self._store_pg(event)
        os_ok = await self._store_os(event) if self._os else True

        if pg_ok:
            await self._increment_counters()
            # Best-effort entity auto-linking
            try:
                event_data = {
                    "actors": event.actors,
                    "locations": event.locations,
                }
                linked = await self._auto_link_entities(
                    str(event.id), event_data,
                )
                if linked:
                    logger.debug(
                        "Auto-linked %d entities to event %s", linked, event.id,
                    )
            except Exception as e:
                logger.debug("Auto-link call failed for %s: %s", event.id, e)

        return pg_ok

    async def store_events_batch(self, events: list[Event]) -> tuple[int, int]:
        """Store a batch of events. Returns (stored_count, failed_count)."""
        stored = 0
        failed = 0
        for event in events:
            ok = await self.store_event(event)
            if ok:
                stored += 1
            else:
                failed += 1
        return stored, failed

    async def _store_pg(self, event: Event) -> bool:
        """Insert event into Postgres."""
        try:
            await self._pool.execute(
                """
                INSERT INTO events (id, data, title, source_id, source_url, category,
                                    event_timestamp, language, confidence, guid, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                ON CONFLICT (id) DO NOTHING
                """,
                event.id,
                event.model_dump_json(),
                event.title,
                event.source_id,
                event.source_url,
                event.category.value,
                event.event_timestamp,
                event.language,
                event.confidence,
                getattr(event, "guid", ""),
                event.created_at,
            )
            return True
        except asyncpg.ForeignKeyViolationError:
            # source_id doesn't exist — retry without it
            try:
                await self._pool.execute(
                    """
                    INSERT INTO events (id, data, title, source_id, source_url, category,
                                        event_timestamp, language, confidence, guid, created_at, updated_at)
                    VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, $9, $10, NOW())
                    ON CONFLICT (id) DO NOTHING
                    """,
                    event.id,
                    event.model_dump_json(),
                    event.title,
                    event.source_url,
                    event.category.value,
                    event.event_timestamp,
                    event.language,
                    event.confidence,
                    getattr(event, "guid", ""),
                    event.created_at,
                )
                return True
            except Exception as e:
                logger.error("Event store retry failed %s: %s", event.id, e)
                return False
        except Exception as e:
            logger.error("Event store failed %s: %s", event.id, e)
            return False

    async def _store_os(self, event: Event) -> bool:
        """Index event in OpenSearch."""
        try:
            now = datetime.now(timezone.utc)
            index_name = f"legba-events-{now.strftime('%Y.%m')}"

            doc = {
                "title": event.title,
                "summary": event.summary,
                "full_content": event.full_content,
                "category": event.category.value,
                "actors": event.actors,
                "locations": event.locations,
                "tags": event.tags,
                "language": event.language,
                "source_id": str(event.source_id) if event.source_id else None,
                "source_url": event.source_url,
                "confidence": event.confidence,
                "event_timestamp": event.event_timestamp.isoformat() if event.event_timestamp else None,
                "created_at": event.created_at.isoformat(),
                "geo_countries": event.geo_countries,
            }

            await self._os.index(
                index=index_name,
                id=str(event.id),
                body=doc,
            )
            return True
        except Exception as e:
            logger.warning("OpenSearch index failed for %s: %s", event.id, e)
            return False

    async def _increment_counters(self) -> None:
        """Increment Redis ingestion counters using sorted sets.

        Each event is recorded as a ZADD entry with timestamp score.
        Old entries are pruned on every call via ZREMRANGEBYSCORE.
        Readers use ZCOUNT(now - window, now) to get accurate counts.
        """
        if not self._redis:
            return
        try:
            now = time.time()
            member = f"{now}:{os.urandom(4).hex()}"
            pipe = self._redis.pipeline()
            # Add entry to both sorted sets
            pipe.zadd("legba:ingest:events_1h", {member: now})
            pipe.zadd("legba:ingest:events_24h", {member: now})
            # Prune entries older than their respective windows
            pipe.zremrangebyscore("legba:ingest:events_1h", "-inf", now - 3600)
            pipe.zremrangebyscore("legba:ingest:events_24h", "-inf", now - 86400)
            await pipe.execute()
        except Exception:
            pass

    async def _auto_link_entities(self, event_id: str, event_data: dict) -> int:
        """Best-effort entity linking for ingested events.

        Two strategies:
        1. Match event actors/locations fields against entity profiles (works for
           structured sources like NWS, USGS, and agent-created events).
        2. Scan event title for known entity names (works for all events — most
           RSS feeds put actor names in titles even though the <author> tag is
           just the journalist byline).

        Returns count of links created.
        """
        linked = 0
        actors = event_data.get("actors") or []
        locations = event_data.get("locations") or []
        title = event_data.get("title") or ""

        # Normalize: actors might be strings or lists
        if isinstance(actors, str):
            actors = [a.strip() for a in actors.split(",") if a.strip()]
        if isinstance(locations, str):
            locations = [loc.strip() for loc in locations.split(",") if loc.strip()]

        names = set()
        for name in actors + locations:
            name = name.strip()
            if name and len(name) >= 3:
                names.add(name)

        try:
            # Strategy 1: match actors/locations fields
            for name in names:
                row = await self._pool.fetchrow(
                    "SELECT id FROM entity_profiles WHERE canonical_name ILIKE $1 LIMIT 1",
                    name,
                )
                if row:
                    await self._pool.execute(
                        "INSERT INTO event_entity_links (event_id, entity_id, role, confidence) "
                        "VALUES ($1, $2, 'mentioned', 0.7) ON CONFLICT DO NOTHING",
                        UUID(event_id), row["id"],
                    )
                    linked += 1

            # Strategy 2: scan title for known high-value entity names
            # Only check entities with 4+ char names to avoid false matches
            if title and len(title) >= 10:
                title_lower = title.lower()
                rows = await self._pool.fetch(
                    "SELECT id, canonical_name FROM entity_profiles "
                    "WHERE length(canonical_name) >= 4 "
                    "AND entity_type IN ('country', 'person', 'organization', 'armed_group', 'international_org') "
                    "ORDER BY length(canonical_name) DESC"
                )
                for row in rows:
                    ename = row["canonical_name"]
                    if ename.lower() in title_lower:
                        await self._pool.execute(
                            "INSERT INTO event_entity_links (event_id, entity_id, role, confidence) "
                            "VALUES ($1, $2, 'mentioned', 0.6) ON CONFLICT DO NOTHING",
                            UUID(event_id), row["id"],
                        )
                        linked += 1
                        if linked >= 5:  # Cap to avoid over-linking
                            break
        except Exception as e:
            logger.debug("Auto-link entities error: %s", e)

        return linked

    # ------------------------------------------------------------------
    # Batch entity linker — periodic post-processing of unlinked events
    # ------------------------------------------------------------------

    async def batch_link_entities(self, limit: int = 200) -> int:
        """Link recently unlinked events to known entities via title matching.

        Runs periodically (called from service tick). Conservative matching:
        - Only matches entity names >= 5 chars (avoids 'US' matching 'bus')
        - Uses word-boundary matching via regex (not substring)
        - Only high-confidence entity types (country, person, org, armed_group)
        - Caps at 5 links per event to prevent noise
        - Confidence 0.6 for title matches (lower than agent-created links at 0.8)

        Returns total number of new links created.
        """
        total_linked = 0
        try:
            # Get recent events with zero entity links
            unlinked = await self._pool.fetch(
                """
                SELECT e.id, e.title
                FROM events e
                LEFT JOIN event_entity_links eel ON e.id = eel.event_id
                WHERE eel.event_id IS NULL
                  AND e.title IS NOT NULL
                  AND length(e.title) >= 10
                  AND e.created_at > NOW() - INTERVAL '48 hours'
                ORDER BY e.created_at DESC
                LIMIT $1
                """,
                limit,
            )

            if not unlinked:
                return 0

            # Load entity names once (cached for the batch)
            entities = await self._pool.fetch(
                "SELECT id, canonical_name FROM entity_profiles "
                "WHERE length(canonical_name) >= 5 "
                "AND entity_type IN ('country', 'person', 'organization', "
                "    'armed_group', 'international_org') "
                "ORDER BY length(canonical_name) DESC"
            )

            if not entities:
                return 0

            # Build word-boundary patterns for safe matching
            import re
            patterns = []
            for ent in entities:
                name = ent["canonical_name"]
                try:
                    pat = re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE)
                    patterns.append((ent["id"], name, pat))
                except re.error:
                    continue

            # Match each unlinked event
            for ev in unlinked:
                title = ev["title"]
                links_for_event = 0
                for ent_id, ent_name, pat in patterns:
                    if pat.search(title):
                        try:
                            await self._pool.execute(
                                "INSERT INTO event_entity_links "
                                "(event_id, entity_id, role, confidence) "
                                "VALUES ($1, $2, 'mentioned', 0.6) "
                                "ON CONFLICT DO NOTHING",
                                ev["id"], ent_id,
                            )
                            links_for_event += 1
                            total_linked += 1
                        except Exception:
                            pass
                    if links_for_event >= 5:
                        break

            if total_linked > 0:
                logger.info(
                    "Batch entity linker: %d links created for %d events",
                    total_linked, len(unlinked),
                )
        except Exception as e:
            logger.warning("Batch entity linker error: %s", e)

        return total_linked

    # ------------------------------------------------------------------
    # Source tracking
    # ------------------------------------------------------------------

    async def record_source_success(
        self, source_id: UUID, events_produced: int = 0,
    ) -> None:
        """Record a successful fetch for a source."""
        try:
            await self._pool.execute(
                """
                UPDATE sources SET
                    fetch_success_count = COALESCE(fetch_success_count, 0) + 1,
                    consecutive_failures = 0,
                    last_successful_fetch_at = NOW(),
                    next_fetch_at = NOW() + (COALESCE(fetch_interval_minutes, 60) * INTERVAL '1 minute'),
                    events_produced_count = COALESCE(events_produced_count, 0) + $2,
                    data = jsonb_set(
                        jsonb_set(
                            jsonb_set(data, '{fetch_success_count}',
                                (COALESCE((data->>'fetch_success_count')::int, 0) + 1)::text::jsonb),
                            '{consecutive_failures}', '0'),
                        '{last_successful_fetch_at}', to_jsonb(NOW()::text)),
                    updated_at = NOW()
                WHERE id = $1
                """,
                source_id,
                events_produced,
            )
        except Exception as e:
            logger.error("record_source_success failed for %s: %s", source_id, e)

    async def record_source_failure(
        self, source_id: UUID, error_msg: str, auto_pause_threshold: int = 10,
    ) -> None:
        """Record a failed fetch. Auto-pauses at threshold consecutive failures."""
        try:
            await self._pool.execute(
                """
                UPDATE sources SET
                    fetch_failure_count = COALESCE(fetch_failure_count, 0) + 1,
                    consecutive_failures = COALESCE(consecutive_failures, 0) + 1,
                    next_fetch_at = NOW() + (
                        LEAST(
                            COALESCE(fetch_interval_minutes, 60) * POWER(2, LEAST(COALESCE(consecutive_failures, 0), 8)),
                            1440
                        ) * INTERVAL '1 minute'
                    ),
                    data = jsonb_set(
                        jsonb_set(data, '{fetch_failure_count}',
                            (COALESCE((data->>'fetch_failure_count')::int, 0) + 1)::text::jsonb),
                        '{consecutive_failures}',
                            (COALESCE((data->>'consecutive_failures')::int, 0) + 1)::text::jsonb),
                    status = CASE
                        WHEN COALESCE(consecutive_failures, 0) + 1 >= $2 THEN 'error'
                        ELSE status
                    END,
                    updated_at = NOW()
                WHERE id = $1
                """,
                source_id,
                auto_pause_threshold,
            )

            # Increment Redis error counter (sorted set)
            if self._redis:
                try:
                    now = time.time()
                    member = f"{now}:{os.urandom(4).hex()}"
                    pipe = self._redis.pipeline()
                    pipe.zadd("legba:ingest:errors_1h", {member: now})
                    pipe.zremrangebyscore("legba:ingest:errors_1h", "-inf", now - 3600)
                    await pipe.execute()
                except Exception:
                    pass
        except Exception as e:
            logger.error("record_source_failure failed for %s: %s", source_id, e)

    # ------------------------------------------------------------------
    # Ingestion log
    # ------------------------------------------------------------------

    async def log_fetch_start(self, source_id: UUID, source_name: str) -> UUID:
        """Log the start of a fetch operation. Returns log entry ID."""
        log_id = uuid4()
        try:
            await self._pool.execute(
                """
                INSERT INTO ingestion_log (id, source_id, source_name, fetch_started_at, status)
                VALUES ($1, $2, $3, NOW(), 'running')
                """,
                log_id,
                source_id,
                source_name,
            )
        except Exception as e:
            logger.warning("Failed to log fetch start: %s", e)
        return log_id

    async def log_fetch_complete(
        self,
        log_id: UUID,
        *,
        status: str = "success",
        events_fetched: int = 0,
        events_stored: int = 0,
        events_deduped: int = 0,
        error_message: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        """Log the completion of a fetch operation."""
        try:
            await self._pool.execute(
                """
                UPDATE ingestion_log SET
                    fetch_completed_at = NOW(),
                    status = $2,
                    events_fetched = $3,
                    events_stored = $4,
                    events_deduped = $5,
                    error_message = $6,
                    duration_ms = $7
                WHERE id = $1
                """,
                log_id,
                status,
                events_fetched,
                events_stored,
                events_deduped,
                error_message,
                duration_ms,
            )
        except Exception as e:
            logger.warning("Failed to log fetch complete: %s", e)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def update_heartbeat(self) -> None:
        """Update Redis heartbeat so other services know we're alive."""
        if not self._redis:
            return
        try:
            await self._redis.set("legba:ingest:heartbeat", "alive", ex=60)
        except Exception:
            pass
