"""
Structured Knowledge Store (PostgreSQL)

Stores goals, facts, and structured knowledge that requires
relational queries rather than vector similarity.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

from ...shared.schemas.goals import Goal, GoalStatus
from ...shared.schemas.memory import Fact


class StructuredStore:
    """PostgreSQL-backed structured knowledge store."""

    def __init__(self, dsn: str, pool_min: int = 1, pool_max: int = 5):
        self._dsn = dsn
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: asyncpg.Pool | None = None
        self._available = False

    async def connect(self) -> None:
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=self._pool_min, max_size=self._pool_max)
            await self._ensure_tables()
            self._available = True
        except Exception:
            self._available = False

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def _ensure_tables(self) -> None:
        """Create tables if they don't exist."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS goals (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    goal_type TEXT NOT NULL DEFAULT 'goal',
                    priority INTEGER NOT NULL DEFAULT 5,
                    parent_id UUID REFERENCES goals(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
                CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id);

                CREATE TABLE IF NOT EXISTS facts (
                    id UUID PRIMARY KEY,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_cycle INTEGER,
                    data JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    superseded_by UUID REFERENCES facts(id)
                );

                CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);
                CREATE INDEX IF NOT EXISTS idx_facts_predicate ON facts(predicate);
                CREATE INDEX IF NOT EXISTS idx_facts_source_cycle ON facts(source_cycle);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_triple
                    ON facts (lower(subject), lower(predicate), lower(value));

                CREATE TABLE IF NOT EXISTS modifications (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    cycle_number INTEGER NOT NULL,
                    file_path TEXT,
                    status TEXT NOT NULL DEFAULT 'applied',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_mods_cycle ON modifications(cycle_number);

                CREATE TABLE IF NOT EXISTS sources (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL DEFAULT 'rss',
                    status TEXT NOT NULL DEFAULT 'active',
                    geo_origin TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT 'en',
                    reliability REAL NOT NULL DEFAULT 0.5,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
                CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(source_type);
                CREATE INDEX IF NOT EXISTS idx_sources_geo ON sources(geo_origin);

                CREATE TABLE IF NOT EXISTS events (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    title TEXT NOT NULL,
                    source_id UUID REFERENCES sources(id),
                    source_url TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'other',
                    event_timestamp TIMESTAMPTZ,
                    language TEXT NOT NULL DEFAULT 'en',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                ALTER TABLE events ADD COLUMN IF NOT EXISTS guid TEXT NOT NULL DEFAULT '';

                CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id);
                CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(event_timestamp);
                CREATE INDEX IF NOT EXISTS idx_events_language ON events(language);
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
                CREATE INDEX IF NOT EXISTS idx_events_guid ON events(guid) WHERE guid != '';

                CREATE TABLE IF NOT EXISTS entity_profiles (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    canonical_name TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT 'other',
                    version INTEGER NOT NULL DEFAULT 1,
                    completeness_score REAL NOT NULL DEFAULT 0.0,
                    last_event_link_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_profiles_name
                    ON entity_profiles (LOWER(canonical_name));
                CREATE INDEX IF NOT EXISTS idx_entity_profiles_type
                    ON entity_profiles (entity_type);
                CREATE INDEX IF NOT EXISTS idx_entity_profiles_completeness
                    ON entity_profiles (completeness_score);
                CREATE INDEX IF NOT EXISTS idx_entity_profiles_updated
                    ON entity_profiles (updated_at);

                CREATE TABLE IF NOT EXISTS entity_profile_versions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    entity_id UUID NOT NULL REFERENCES entity_profiles(id),
                    version INTEGER NOT NULL,
                    data JSONB NOT NULL,
                    cycle_number INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_epv_entity
                    ON entity_profile_versions (entity_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_epv_created
                    ON entity_profile_versions (entity_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS event_entity_links (
                    event_id UUID NOT NULL REFERENCES events(id),
                    entity_id UUID NOT NULL REFERENCES entity_profiles(id),
                    role TEXT NOT NULL DEFAULT 'mentioned',
                    confidence REAL NOT NULL DEFAULT 0.8,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (event_id, entity_id, role)
                );

                CREATE INDEX IF NOT EXISTS idx_eel_entity
                    ON event_entity_links (entity_id);
                CREATE INDEX IF NOT EXISTS idx_eel_event
                    ON event_entity_links (event_id);
            """)
            # --- Additive migrations (safe to re-run) ---
            await conn.execute("""
                -- Source reliability tracking columns
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS fetch_success_count INTEGER DEFAULT 0;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS fetch_failure_count INTEGER DEFAULT 0;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS events_produced_count INTEGER DEFAULT 0;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_successful_fetch_at TIMESTAMPTZ;
            """)

    # --- Goal operations ---

    async def save_goal(self, goal: Goal) -> bool:
        if not self._available:
            return False
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO goals (id, data, status, goal_type, priority, parent_id, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        data = EXCLUDED.data,
                        status = EXCLUDED.status,
                        priority = EXCLUDED.priority,
                        updated_at = NOW()
                    """,
                    goal.id,
                    goal.model_dump_json(),
                    goal.status.value,
                    goal.goal_type.value,
                    goal.priority,
                    goal.parent_id,
                    goal.created_at,
                )
            return True
        except Exception:
            return False

    async def get_goal(self, goal_id: UUID) -> Goal | None:
        if not self._available:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM goals WHERE id = $1", goal_id)
                if row:
                    return Goal.model_validate_json(row["data"])
            return None
        except Exception:
            return None

    async def get_active_goals(self) -> list[Goal]:
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT data FROM goals WHERE status = 'active' ORDER BY priority ASC"
                )
                return [Goal.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    async def get_all_goals(self) -> list[Goal]:
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("SELECT data FROM goals ORDER BY priority ASC, created_at ASC")
                return [Goal.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    async def get_deferred_goals(self, current_cycle: int) -> list[Goal]:
        """Get deferred goals whose revisit cycle has arrived."""
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT data FROM goals WHERE status = 'deferred' ORDER BY priority ASC"
                )
                goals = [Goal.model_validate_json(row["data"]) for row in rows]
                return [
                    g for g in goals
                    if g.deferred_until_cycle is not None and g.deferred_until_cycle <= current_cycle
                ]
        except Exception:
            return []

    # --- Fact operations ---

    async def store_fact(self, fact: Fact) -> bool:
        if not self._available:
            return False
        try:
            # Normalize predicate and value before storage
            from .fact_normalize import normalize_fact_predicate, normalize_fact_value
            fact.predicate = normalize_fact_predicate(fact.predicate)
            fact.value = normalize_fact_value(fact.value)

            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO facts (id, subject, predicate, value, confidence, source_cycle, data, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (lower(subject), lower(predicate), lower(value)) DO UPDATE SET
                        confidence = GREATEST(facts.confidence, EXCLUDED.confidence),
                        source_cycle = EXCLUDED.source_cycle,
                        data = EXCLUDED.data,
                        updated_at = NOW()
                    """,
                    fact.id,
                    fact.subject,
                    fact.predicate,
                    fact.value,
                    fact.confidence,
                    fact.source_cycle,
                    fact.model_dump_json(),
                    fact.created_at,
                )
            return True
        except Exception:
            return False

    async def query_facts(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        limit: int = 20,
    ) -> list[Fact]:
        if not self._available:
            return []
        try:
            conditions = ["superseded_by IS NULL"]
            params: list[Any] = []
            idx = 1

            if subject:
                conditions.append(f"subject ILIKE ${idx}")
                params.append(f"%{subject}%")
                idx += 1
            if predicate:
                conditions.append(f"predicate ILIKE ${idx}")
                params.append(f"%{predicate}%")
                idx += 1

            where = " AND ".join(conditions)
            params.append(limit)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT data FROM facts WHERE {where} ORDER BY confidence DESC, created_at DESC LIMIT ${idx}",
                    *params,
                )
                return [Fact.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    async def query_facts_recent(
        self,
        current_cycle: int,
        lookback: int = 5,
        limit: int = 10,
    ) -> list[Fact]:
        """Get non-superseded facts from the last N cycles.

        Guarantees the agent sees its own recent work regardless of
        confidence ranking or semantic similarity.
        """
        if not self._available:
            return []
        try:
            min_cycle = max(1, current_cycle - lookback)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT data FROM facts "
                    "WHERE superseded_by IS NULL AND source_cycle >= $1 "
                    "ORDER BY source_cycle DESC, created_at DESC LIMIT $2",
                    min_cycle, limit,
                )
                return [Fact.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    async def supersede_fact(self, old_fact_id: UUID, new_fact: Fact) -> bool:
        """Mark an existing fact as superseded by a new one."""
        if not self._available:
            return False
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # Store the new fact
                    await conn.execute(
                        """
                        INSERT INTO facts (id, subject, predicate, value, confidence, source_cycle, data, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (id) DO UPDATE SET
                            value = EXCLUDED.value,
                            confidence = EXCLUDED.confidence,
                            updated_at = NOW()
                        """,
                        new_fact.id,
                        new_fact.subject,
                        new_fact.predicate,
                        new_fact.value,
                        new_fact.confidence,
                        new_fact.source_cycle,
                        new_fact.model_dump_json(),
                        new_fact.created_at,
                    )
                    # Mark the old fact as superseded
                    await conn.execute(
                        "UPDATE facts SET superseded_by = $1, updated_at = NOW() WHERE id = $2",
                        new_fact.id,
                        old_fact_id,
                    )
            return True
        except Exception:
            return False

    async def get_fact(self, fact_id: UUID) -> Fact | None:
        """Get a single fact by ID."""
        if not self._available:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM facts WHERE id = $1", fact_id)
                if row:
                    return Fact.model_validate_json(row["data"])
            return None
        except Exception:
            return None

    # --- Modification tracking ---

    async def store_modification(self, modification_data: dict, cycle_number: int) -> bool:
        if not self._available:
            return False
        try:
            mod_id = modification_data.get("id") or modification_data.get("proposal_id")
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO modifications (id, data, cycle_number, file_path, status)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    mod_id,
                    json.dumps(modification_data, default=str),
                    cycle_number,
                    modification_data.get("file_path"),
                    modification_data.get("status", "applied"),
                )
            return True
        except Exception:
            return False

    # --- Source operations ---

    async def save_source(self, source) -> bool:
        """Upsert a Source into the sources table."""
        if not self._available:
            return False
        try:
            from ...shared.schemas.sources import Source
            if not isinstance(source, Source):
                return False
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO sources (id, data, name, url, source_type, status, geo_origin,
                                         language, reliability, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                    ON CONFLICT (url) DO UPDATE SET
                        data = EXCLUDED.data,
                        name = EXCLUDED.name,
                        source_type = EXCLUDED.source_type,
                        status = EXCLUDED.status,
                        geo_origin = EXCLUDED.geo_origin,
                        language = EXCLUDED.language,
                        reliability = EXCLUDED.reliability,
                        updated_at = NOW()
                    """,
                    source.id,
                    source.model_dump_json(),
                    source.name,
                    source.url,
                    source.source_type.value,
                    source.status.value,
                    source.geo_origin,
                    source.language,
                    source.reliability,
                    source.created_at,
                )
            return True
        except Exception:
            return False

    async def get_source(self, source_id: UUID) -> Any:
        """Get a single source by ID."""
        if not self._available:
            return None
        try:
            from ...shared.schemas.sources import Source
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM sources WHERE id = $1", source_id)
                if row:
                    return Source.model_validate_json(row["data"])
            return None
        except Exception:
            return None

    async def get_sources(
        self,
        status: str | None = None,
        source_type: str | None = None,
        limit: int = 50,
    ) -> list:
        """Query sources with optional filters."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.sources import Source
            conditions = []
            params: list[Any] = []
            idx = 1

            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1
            if source_type:
                conditions.append(f"source_type = ${idx}")
                params.append(source_type)
                idx += 1

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT data FROM sources {where} ORDER BY name ASC LIMIT ${idx}",
                    *params,
                )
                results = []
                for row in rows:
                    try:
                        results.append(Source.model_validate_json(row["data"]))
                    except Exception:
                        pass  # skip unparseable rows
                return results
        except Exception:
            return []

    async def delete_source(self, source_id: UUID) -> bool:
        """Delete a source by ID."""
        if not self._available:
            return False
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute("DELETE FROM sources WHERE id = $1", source_id)
                return result == "DELETE 1"
        except Exception:
            return False

    # --- Source reliability tracking ---

    async def record_source_fetch(self, source_id: UUID, *, success: bool) -> None:
        """Record a fetch attempt result for a source. Auto-pauses after 5 consecutive failures."""
        if not self._available:
            return
        try:
            async with self._pool.acquire() as conn:
                if success:
                    await conn.execute("""
                        UPDATE sources SET
                            fetch_success_count = COALESCE(fetch_success_count, 0) + 1,
                            consecutive_failures = 0,
                            last_successful_fetch_at = NOW(),
                            data = jsonb_set(
                                jsonb_set(
                                    jsonb_set(data, '{fetch_success_count}',
                                        (COALESCE((data->>'fetch_success_count')::int, 0) + 1)::text::jsonb),
                                    '{consecutive_failures}', '0'),
                                '{last_successful_fetch_at}', to_jsonb(NOW()::text)),
                            updated_at = NOW()
                        WHERE id = $1
                    """, source_id)
                else:
                    # Increment failures, auto-pause at 5 consecutive
                    await conn.execute("""
                        UPDATE sources SET
                            fetch_failure_count = COALESCE(fetch_failure_count, 0) + 1,
                            consecutive_failures = COALESCE(consecutive_failures, 0) + 1,
                            data = jsonb_set(
                                jsonb_set(data, '{fetch_failure_count}',
                                    (COALESCE((data->>'fetch_failure_count')::int, 0) + 1)::text::jsonb),
                                '{consecutive_failures}',
                                    (COALESCE((data->>'consecutive_failures')::int, 0) + 1)::text::jsonb),
                            status = CASE
                                WHEN COALESCE(consecutive_failures, 0) + 1 >= 5 THEN 'error'
                                ELSE status
                            END,
                            updated_at = NOW()
                        WHERE id = $1
                    """, source_id)
        except Exception:
            pass

    async def increment_source_event_count(self, source_id: UUID) -> None:
        """Increment events_produced_count for a source."""
        if not self._available:
            return
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute("""
                    UPDATE sources SET
                        events_produced_count = COALESCE(events_produced_count, 0) + 1,
                        data = jsonb_set(data, '{events_produced_count}',
                            (COALESCE((data->>'events_produced_count')::int, 0) + 1)::text::jsonb),
                        updated_at = NOW()
                    WHERE id = $1
                """, source_id)
                if result == "UPDATE 0":
                    logger.warning(
                        "increment_source_event_count: no source found for id=%s", source_id
                    )
        except Exception as e:
            logger.error("increment_source_event_count failed for source %s: %s", source_id, e)

    # --- Event operations ---

    async def save_event(self, event) -> bool:
        """Upsert an Event into the events table."""
        if not self._available:
            return False
        try:
            from ...shared.schemas.events import Event
            if not isinstance(event, Event):
                return False
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO events (id, data, title, source_id, source_url, category,
                                        event_timestamp, language, confidence, guid, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        data = EXCLUDED.data,
                        title = EXCLUDED.title,
                        category = EXCLUDED.category,
                        event_timestamp = EXCLUDED.event_timestamp,
                        confidence = EXCLUDED.confidence,
                        updated_at = NOW()
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
                    getattr(event, 'guid', ''),
                    event.created_at,
                )
            return True
        except asyncpg.ForeignKeyViolationError:
            # source_id references a non-existent source — retry without it
            logger.warning(
                "save_event FK violation for event %s (source_id=%s), retrying with source_id=NULL",
                event.id, event.source_id,
            )
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO events (id, data, title, source_id, source_url, category,
                                            event_timestamp, language, confidence, guid, created_at, updated_at)
                        VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, $9, $10, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            data = EXCLUDED.data,
                            title = EXCLUDED.title,
                            category = EXCLUDED.category,
                            event_timestamp = EXCLUDED.event_timestamp,
                            confidence = EXCLUDED.confidence,
                            updated_at = NOW()
                        """,
                        event.id,
                        event.model_dump_json(),
                        event.title,
                        event.source_url,
                        event.category.value,
                        event.event_timestamp,
                        event.language,
                        event.confidence,
                        getattr(event, 'guid', ''),
                        event.created_at,
                    )
                return True
            except Exception as e2:
                logger.error("save_event retry failed for event %s: %s", event.id, e2)
                return False
        except Exception as e:
            logger.error("save_event failed for event %s: %s", event.id, e)
            return False

    async def check_event_guid(self, guid: str) -> dict | None:
        """Check if an event with this GUID already exists. Returns {id, title} or None."""
        if not self._available or not guid:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, title FROM events WHERE guid = $1 LIMIT 1",
                    guid,
                )
                if row:
                    return {"id": row["id"], "title": row["title"]}
        except Exception:
            pass
        return None

    async def get_recent_events_for_dedup(self, limit: int = 100) -> list:
        """Get recent events (by created_at) for title-similarity dedup."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.events import Event
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT data FROM events ORDER BY created_at DESC LIMIT $1",
                    limit,
                )
            return [Event.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    async def query_events(
        self,
        category: str | None = None,
        source_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> list:
        """Query events with optional filters."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.events import Event
            conditions: list[str] = []
            params: list[Any] = []
            idx = 1

            if category:
                conditions.append(f"category = ${idx}")
                params.append(category)
                idx += 1
            if source_id:
                conditions.append(f"source_id = ${idx}")
                params.append(source_id)
                idx += 1
            if since:
                conditions.append(f"event_timestamp >= ${idx}")
                params.append(since)
                idx += 1
            if until:
                conditions.append(f"event_timestamp <= ${idx}")
                params.append(until)
                idx += 1
            if language:
                conditions.append(f"language = ${idx}")
                params.append(language)
                idx += 1

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT data FROM events {where} "
                    f"ORDER BY event_timestamp DESC NULLS LAST, created_at DESC LIMIT ${idx}",
                    *params,
                )
                return [Event.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    # --- Entity Profile operations ---

    async def save_entity_profile(self, profile, cycle_number: int | None = None) -> bool:
        """Upsert an EntityProfile and create a version snapshot."""
        if not self._available:
            return False
        try:
            from ...shared.schemas.entity_profiles import EntityProfile
            if not isinstance(profile, EntityProfile):
                return False

            profile.completeness_score = profile.compute_completeness()
            profile.updated_at = datetime.now(timezone.utc)

            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # Check if exists to determine version bump
                    existing = await conn.fetchrow(
                        "SELECT version FROM entity_profiles WHERE id = $1",
                        profile.id,
                    )
                    if existing:
                        profile.version = existing["version"] + 1

                    await conn.execute(
                        """
                        INSERT INTO entity_profiles (id, data, canonical_name, entity_type,
                                                     version, completeness_score, last_event_link_at,
                                                     created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            data = EXCLUDED.data,
                            canonical_name = EXCLUDED.canonical_name,
                            entity_type = EXCLUDED.entity_type,
                            version = EXCLUDED.version,
                            completeness_score = EXCLUDED.completeness_score,
                            last_event_link_at = EXCLUDED.last_event_link_at,
                            updated_at = NOW()
                        """,
                        profile.id,
                        profile.model_dump_json(),
                        profile.canonical_name,
                        profile.entity_type.value,
                        profile.version,
                        profile.completeness_score,
                        profile.last_event_link_at,
                        profile.created_at,
                    )

                    # Save version snapshot
                    await conn.execute(
                        """
                        INSERT INTO entity_profile_versions (entity_id, version, data, cycle_number)
                        VALUES ($1, $2, $3, $4)
                        """,
                        profile.id,
                        profile.version,
                        profile.model_dump_json(),
                        cycle_number,
                    )
            return True
        except Exception:
            return False

    async def get_entity_profile(self, entity_id: UUID) -> Any:
        """Get current entity profile by ID."""
        if not self._available:
            return None
        try:
            from ...shared.schemas.entity_profiles import EntityProfile
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM entity_profiles WHERE id = $1", entity_id,
                )
                if row:
                    return EntityProfile.model_validate_json(row["data"])
            return None
        except Exception:
            return None

    async def get_entity_profile_by_name(self, name: str) -> Any:
        """Get profile by canonical_name (case-insensitive exact match)."""
        if not self._available:
            return None
        try:
            from ...shared.schemas.entity_profiles import EntityProfile
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM entity_profiles WHERE LOWER(canonical_name) = LOWER($1)",
                    name,
                )
                if row:
                    return EntityProfile.model_validate_json(row["data"])
            return None
        except Exception:
            return None

    async def resolve_entity_name(self, name: str) -> Any:
        """Resolve a name through: canonical_name -> aliases -> fuzzy match.

        Returns the matching EntityProfile or None.
        """
        if not self._available:
            return None
        try:
            from ...shared.schemas.entity_profiles import EntityProfile
            from difflib import SequenceMatcher
            import re

            # 1. Exact canonical name match
            profile = await self.get_entity_profile_by_name(name)
            if profile:
                return profile

            # 2. Search aliases in JSONB
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT data FROM entity_profiles
                    WHERE data->'aliases' @> to_jsonb($1::text)
                    LIMIT 1
                    """,
                    name,
                )
                if rows:
                    return EntityProfile.model_validate_json(rows[0]["data"])

                # 3. Case-insensitive alias search
                rows = await conn.fetch(
                    """
                    SELECT data FROM entity_profiles
                    WHERE EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(data->'aliases') AS alias
                        WHERE LOWER(alias) = LOWER($1)
                    )
                    LIMIT 1
                    """,
                    name,
                )
                if rows:
                    return EntityProfile.model_validate_json(rows[0]["data"])

                # 4. Fuzzy match against all canonical names
                all_rows = await conn.fetch(
                    "SELECT canonical_name, data FROM entity_profiles LIMIT 500"
                )

            def _norm(n: str) -> str:
                return re.sub(r'[-_/\\\s]+', '', n.lower())

            name_norm = _norm(name)
            best_profile = None
            best_ratio = 0.0

            for row in all_rows:
                canon = row["canonical_name"]
                canon_norm = _norm(canon)
                if name_norm == canon_norm:
                    return EntityProfile.model_validate_json(row["data"])
                ratio = SequenceMatcher(None, name_norm, canon_norm).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_profile = row["data"]

            if best_ratio >= 0.85 and best_profile:
                return EntityProfile.model_validate_json(best_profile)

            return None
        except Exception:
            return None

    async def search_entity_profiles(
        self,
        query: str | None = None,
        entity_type: str | None = None,
        min_completeness: float | None = None,
        stale_days: float | None = None,
        limit: int = 20,
    ) -> list:
        """Search entity profiles with filters."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.entity_profiles import EntityProfile
            conditions: list[str] = []
            params: list[Any] = []
            idx = 1

            if query:
                conditions.append(f"LOWER(canonical_name) LIKE LOWER(${idx})")
                params.append(f"%{query}%")
                idx += 1
            if entity_type:
                conditions.append(f"entity_type = ${idx}")
                params.append(entity_type)
                idx += 1
            if min_completeness is not None:
                conditions.append(f"completeness_score >= ${idx}")
                params.append(min_completeness)
                idx += 1
            if stale_days is not None:
                conditions.append(f"updated_at < NOW() - INTERVAL '1 day' * ${idx}")
                params.append(stale_days)
                idx += 1

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT data FROM entity_profiles {where} "
                    f"ORDER BY updated_at DESC LIMIT ${idx}",
                    *params,
                )
                return [EntityProfile.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    async def get_entity_profile_version(
        self,
        entity_id: UUID,
        as_of: datetime | None = None,
        version: int | None = None,
    ) -> dict | None:
        """Get a historical version of an entity profile."""
        if not self._available:
            return None
        try:
            async with self._pool.acquire() as conn:
                if version is not None:
                    row = await conn.fetchrow(
                        "SELECT data, version, cycle_number, created_at "
                        "FROM entity_profile_versions "
                        "WHERE entity_id = $1 AND version = $2",
                        entity_id, version,
                    )
                elif as_of is not None:
                    row = await conn.fetchrow(
                        "SELECT data, version, cycle_number, created_at "
                        "FROM entity_profile_versions "
                        "WHERE entity_id = $1 AND created_at <= $2 "
                        "ORDER BY created_at DESC LIMIT 1",
                        entity_id, as_of,
                    )
                else:
                    return None

                if row:
                    return {
                        "data": json.loads(row["data"]),
                        "version": row["version"],
                        "cycle_number": row["cycle_number"],
                        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    }
            return None
        except Exception:
            return None

    async def save_event_entity_link(self, link) -> bool:
        """Create a link between an event and an entity profile."""
        if not self._available:
            return False
        try:
            from ...shared.schemas.entity_profiles import EventEntityLink
            if not isinstance(link, EventEntityLink):
                return False
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO event_entity_links (event_id, entity_id, role, confidence)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (event_id, entity_id, role) DO UPDATE SET
                        confidence = EXCLUDED.confidence
                    """,
                    link.event_id,
                    link.entity_id,
                    link.role,
                    link.confidence,
                )
            return True
        except Exception:
            return False

    async def get_entity_events(self, entity_id: UUID, limit: int = 20) -> list[dict]:
        """Get events linked to an entity."""
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT e.data AS event_data, l.role, l.confidence
                    FROM event_entity_links l
                    JOIN events e ON e.id = l.event_id
                    WHERE l.entity_id = $1
                    ORDER BY e.event_timestamp DESC NULLS LAST, e.created_at DESC
                    LIMIT $2
                    """,
                    entity_id, limit,
                )
                return [
                    {
                        "event": json.loads(row["event_data"]),
                        "role": row["role"],
                        "confidence": row["confidence"],
                    }
                    for row in rows
                ]
        except Exception:
            return []

    async def get_event_entities(self, event_id: UUID) -> list[dict]:
        """Get entities linked to an event."""
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT p.data AS profile_data, l.role, l.confidence
                    FROM event_entity_links l
                    JOIN entity_profiles p ON p.id = l.entity_id
                    WHERE l.event_id = $1
                    """,
                    event_id,
                )
                return [
                    {
                        "profile": json.loads(row["profile_data"]),
                        "role": row["role"],
                        "confidence": row["confidence"],
                    }
                    for row in rows
                ]
        except Exception:
            return []

    async def get_stale_entities(self, stale_days: float = 7.0, limit: int = 20) -> list:
        """Get entity profiles not updated in N days."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.entity_profiles import EntityProfile
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT data FROM entity_profiles
                    WHERE updated_at < NOW() - INTERVAL '1 day' * $1
                    ORDER BY updated_at ASC
                    LIMIT $2
                    """,
                    stale_days, limit,
                )
                return [EntityProfile.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    async def get_incomplete_entities(self, max_completeness: float = 0.5, limit: int = 20) -> list:
        """Get entity profiles below a completeness threshold."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.entity_profiles import EntityProfile
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT data FROM entity_profiles
                    WHERE completeness_score <= $1
                    ORDER BY completeness_score ASC, updated_at ASC
                    LIMIT $2
                    """,
                    max_completeness, limit,
                )
                return [EntityProfile.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []
