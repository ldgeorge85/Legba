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

from ...shared.schemas.goals import Goal
from ...shared.schemas.memory import Fact


class StructuredStore:
    """PostgreSQL-backed structured knowledge store."""

    def __init__(self, dsn: str, pool_min: int = 1, pool_max: int = 5):
        self._dsn = dsn
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: asyncpg.Pool | None = None
        self._available = False
        self._graph = None  # Set after construction if graph cleanup on supersede is needed

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
                    source_type TEXT NOT NULL DEFAULT 'agent',
                    data JSONB,
                    valid_from TIMESTAMPTZ,
                    valid_until TIMESTAMPTZ,
                    geo_lat DOUBLE PRECISION,
                    geo_lon DOUBLE PRECISION,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    superseded_by UUID REFERENCES facts(id)
                );

                CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);
                CREATE INDEX IF NOT EXISTS idx_facts_predicate ON facts(predicate);
                CREATE INDEX IF NOT EXISTS idx_facts_source_cycle ON facts(source_cycle);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_temporal_triple
                    ON facts (lower(subject), lower(predicate), lower(value), COALESCE(valid_from, '1970-01-01'::timestamptz));

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

                CREATE TABLE IF NOT EXISTS signals (
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

                ALTER TABLE signals ADD COLUMN IF NOT EXISTS guid TEXT NOT NULL DEFAULT '';

                CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source_id);
                CREATE INDEX IF NOT EXISTS idx_signals_category ON signals(category);
                CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(event_timestamp);
                CREATE INDEX IF NOT EXISTS idx_signals_language ON signals(language);
                CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
                CREATE INDEX IF NOT EXISTS idx_signals_guid ON signals(guid) WHERE guid != '';

                CREATE TABLE IF NOT EXISTS entity_profiles (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    canonical_name TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT 'other',
                    version INTEGER NOT NULL DEFAULT 1,
                    completeness_score REAL NOT NULL DEFAULT 0.0,
                    last_event_link_at TIMESTAMPTZ,
                    geo_lat DOUBLE PRECISION,
                    geo_lon DOUBLE PRECISION,
                    geo_country TEXT,
                    geo_region TEXT,
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

                CREATE TABLE IF NOT EXISTS signal_entity_links (
                    signal_id UUID NOT NULL REFERENCES signals(id),
                    entity_id UUID NOT NULL REFERENCES entity_profiles(id),
                    role TEXT NOT NULL DEFAULT 'mentioned',
                    confidence REAL NOT NULL DEFAULT 0.8,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (signal_id, entity_id, role)
                );

                CREATE INDEX IF NOT EXISTS idx_sel_entity
                    ON signal_entity_links (entity_id);
                CREATE INDEX IF NOT EXISTS idx_sel_signal_entity
                    ON signal_entity_links (signal_id);

                -- Events table (created early so watch_triggers FK works)
                CREATE TABLE IF NOT EXISTS events (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'other',
                    event_type TEXT NOT NULL DEFAULT 'incident',
                    severity TEXT NOT NULL DEFAULT 'medium',
                    time_start TIMESTAMPTZ,
                    time_end TIMESTAMPTZ,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    signal_count INTEGER NOT NULL DEFAULT 0,
                    source_method TEXT NOT NULL DEFAULT 'auto',
                    source_cycle INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            # --- Additive migrations (safe to re-run) ---
            await conn.execute("""
                -- Source reliability tracking columns
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS fetch_success_count INTEGER DEFAULT 0;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS fetch_failure_count INTEGER DEFAULT 0;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS events_produced_count INTEGER DEFAULT 0;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_successful_fetch_at TIMESTAMPTZ;

                -- Signal source_url index for fast dedup
                CREATE INDEX IF NOT EXISTS idx_signals_source_url
                    ON signals(source_url) WHERE source_url != '';

                -- GUID unique constraint (catch racing stores)
                CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_guid_unique
                    ON signals(guid) WHERE guid IS NOT NULL AND guid != '';

                -- Watchlist table
                CREATE TABLE IF NOT EXISTS watchlist (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    name TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    active BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_triggered_at TIMESTAMPTZ,
                    trigger_count INTEGER NOT NULL DEFAULT 0
                );

                -- Watch triggers log
                CREATE TABLE IF NOT EXISTS watch_triggers (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    watch_id UUID NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
                    signal_id UUID REFERENCES signals(id) ON DELETE CASCADE,
                    event_id UUID REFERENCES events(id) ON DELETE SET NULL,
                    watch_name TEXT NOT NULL DEFAULT '',
                    event_title TEXT NOT NULL DEFAULT '',
                    match_reasons JSONB NOT NULL DEFAULT '[]',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_watch_triggers_time
                    ON watch_triggers(triggered_at DESC);

                -- Situations table
                CREATE TABLE IF NOT EXISTS situations (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    category TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_event_at TIMESTAMPTZ,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    intensity_score REAL NOT NULL DEFAULT 0.0
                );
                CREATE INDEX IF NOT EXISTS idx_situations_status ON situations(status);

                -- Situation-signal links (legacy, renamed from situation_events)
                CREATE TABLE IF NOT EXISTS situation_signals (
                    situation_id UUID NOT NULL REFERENCES situations(id) ON DELETE CASCADE,
                    signal_id UUID NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
                    relevance REAL NOT NULL DEFAULT 1.0,
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (situation_id, signal_id)
                );

                -- Situation-event links (derived events)
                CREATE TABLE IF NOT EXISTS situation_events (
                    situation_id UUID NOT NULL REFERENCES situations(id) ON DELETE CASCADE,
                    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    relevance REAL NOT NULL DEFAULT 1.0,
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (situation_id, event_id)
                );

                -- Ingestion service columns on sources
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS fetch_interval_minutes INTEGER DEFAULT 60;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS next_fetch_at TIMESTAMPTZ;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS category TEXT DEFAULT '';
                CREATE INDEX IF NOT EXISTS idx_sources_next_fetch ON sources(next_fetch_at)
                    WHERE status = 'active';

                -- Ingestion log table
                CREATE TABLE IF NOT EXISTS ingestion_log (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    source_id UUID REFERENCES sources(id),
                    source_name TEXT NOT NULL DEFAULT '',
                    fetch_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    fetch_completed_at TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'running',
                    events_fetched INTEGER DEFAULT 0,
                    events_stored INTEGER DEFAULT 0,
                    events_deduped INTEGER DEFAULT 0,
                    error_message TEXT,
                    duration_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_ingestion_log_source
                    ON ingestion_log(source_id);
                CREATE INDEX IF NOT EXISTS idx_ingestion_log_time
                    ON ingestion_log(fetch_started_at DESC);

                -- Predictions / hypothesis tracking
                CREATE TABLE IF NOT EXISTS predictions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    hypothesis TEXT NOT NULL,
                    source_cycle INTEGER NOT NULL,
                    source_type TEXT DEFAULT 'report',
                    category TEXT DEFAULT '',
                    region TEXT DEFAULT '',
                    status TEXT DEFAULT 'open',
                    confidence REAL DEFAULT 0.5,
                    evidence_for TEXT[] DEFAULT '{}',
                    evidence_against TEXT[] DEFAULT '{}',
                    resolution_cycle INTEGER,
                    resolution_note TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);

                -- Entity freshness tracking (for confidence propagation)
                ALTER TABLE entity_profiles ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;

                -- Source credibility tracking (per-source event quality)
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS source_quality_score REAL DEFAULT 0.0;

                -- Proposed edges queue (relationship inference from reports)
                CREATE TABLE IF NOT EXISTS proposed_edges (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    source_entity TEXT NOT NULL,
                    target_entity TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_text TEXT NOT NULL DEFAULT '',
                    source_cycle INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_proposed_edges_status ON proposed_edges(status);

                -- Derived events: real-world occurrences from signal clustering
                CREATE TABLE IF NOT EXISTS events (
                    id UUID PRIMARY KEY,
                    data JSONB NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'other',
                    event_type TEXT NOT NULL DEFAULT 'incident',
                    severity TEXT NOT NULL DEFAULT 'medium',
                    time_start TIMESTAMPTZ,
                    time_end TIMESTAMPTZ,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    signal_count INTEGER NOT NULL DEFAULT 0,
                    source_method TEXT NOT NULL DEFAULT 'auto',
                    source_cycle INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_derived_events_category ON events(category);
                CREATE INDEX IF NOT EXISTS idx_derived_events_type ON events(event_type);
                CREATE INDEX IF NOT EXISTS idx_derived_events_severity ON events(severity);
                CREATE INDEX IF NOT EXISTS idx_derived_events_time_start ON events(time_start);
                CREATE INDEX IF NOT EXISTS idx_derived_events_created ON events(created_at);

                -- Notification audit log
                CREATE TABLE IF NOT EXISTS notifications (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    type TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_type ON notifications(type);
                CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);

                -- Signal-event junction (many-to-many)
                CREATE TABLE IF NOT EXISTS signal_event_links (
                    signal_id UUID NOT NULL,
                    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    relevance REAL NOT NULL DEFAULT 1.0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (signal_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sel_event ON signal_event_links(event_id);
                CREATE INDEX IF NOT EXISTS idx_sel_signal ON signal_event_links(signal_id);

                -- Event-entity links (auto-propagated from signal_entity_links)
                CREATE TABLE IF NOT EXISTS event_entity_links (
                    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    entity_id UUID NOT NULL REFERENCES entity_profiles(id) ON DELETE CASCADE,
                    role TEXT NOT NULL DEFAULT 'mentioned',
                    confidence REAL NOT NULL DEFAULT 0.7,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (event_id, entity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_eel_entity ON event_entity_links(entity_id);
                CREATE INDEX IF NOT EXISTS idx_eel_event ON event_entity_links(event_id);

                -- Hypotheses: competing analytical theories (ACH)
                CREATE TABLE IF NOT EXISTS hypotheses (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    situation_id UUID REFERENCES situations(id),
                    thesis TEXT NOT NULL,
                    counter_thesis TEXT NOT NULL DEFAULT '',
                    diagnostic_evidence JSONB NOT NULL DEFAULT '[]',
                    supporting_signals UUID[] NOT NULL DEFAULT '{}',
                    refuting_signals UUID[] NOT NULL DEFAULT '{}',
                    evidence_balance INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_cycle INTEGER,
                    last_evaluated_cycle INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON hypotheses(status);
                CREATE INDEX IF NOT EXISTS idx_hypotheses_situation ON hypotheses(situation_id);

                -- Discovered URLs (potential new sources from signal content)
                CREATE TABLE IF NOT EXISTS discovered_urls (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    base_domain TEXT NOT NULL,
                    full_url TEXT NOT NULL,
                    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
                    seen_count INT DEFAULT 1,
                    source_signal_id UUID,
                    status TEXT DEFAULT 'new',
                    reviewed_at TIMESTAMPTZ,
                    notes TEXT,
                    UNIQUE(base_domain)
                );
                CREATE INDEX IF NOT EXISTS idx_discovered_urls_status
                    ON discovered_urls (status, seen_count DESC);

                -- Operator correction tracking
                CREATE TABLE IF NOT EXISTS operator_corrections (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    entity_type TEXT NOT NULL,
                    entity_id UUID,
                    action TEXT NOT NULL,
                    old_value JSONB,
                    new_value JSONB,
                    corrected_by TEXT DEFAULT 'operator',
                    corrected_at TIMESTAMPTZ DEFAULT NOW(),
                    notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_corrections_time
                    ON operator_corrections (corrected_at DESC);

                -- Users table (Phase 8: authentication)
                CREATE TABLE IF NOT EXISTS users (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_login TIMESTAMPTZ
                );

                -- Reified relationships: Nexuses table
                -- Nexuses are first-class nodes in the AGE graph, but we
                -- keep a Postgres table as the queryable store because AGE's
                -- Cypher property filtering is limited.
                CREATE TABLE IF NOT EXISTS nexuses (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    nexus_type TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'direct',
                    intent TEXT NOT NULL DEFAULT 'neutral',
                    description TEXT DEFAULT '',
                    actor_entity TEXT NOT NULL,
                    target_entity TEXT NOT NULL,
                    via_entity TEXT,
                    confidence REAL DEFAULT 0.5,
                    evidence_count INT DEFAULT 1,
                    valid_from TIMESTAMPTZ,
                    valid_until TIMESTAMPTZ,
                    source_cycle INT DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_nexuses_actor ON nexuses (actor_entity);
                CREATE INDEX IF NOT EXISTS idx_nexuses_target ON nexuses (target_entity);
                CREATE INDEX IF NOT EXISTS idx_nexuses_type ON nexuses (nexus_type);
            """)

            # --- Cognitive architecture schema extensions ---
            # Uses ADD COLUMN IF NOT EXISTS so safe on both fresh + existing DBs
            try:
                from ...shared.schema_extensions import apply_extensions
                ext_results = await apply_extensions(self._pool)
                for tbl, ok in ext_results.items():
                    if not ok:
                        logger.warning("Schema extension failed for table: %s", tbl)
            except Exception as e:
                logger.warning("Schema extensions could not be applied: %s", e)

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

    # Predicates where only one value should be active per subject or per value.
    # E.g., only one person can be LeaderOf a given country at a time.
    _VOLATILE_PREDICATES = frozenset({
        "LeaderOf", "HeadOfState", "HeadOfGovernment", "President",
        "PrimeMinister", "SupremeLeader", "Monarch",
    })

    # Broader set: predicates where storing a new value for the same subject
    # should auto-supersede the old value (even if not leadership-specific).
    _SINGLE_VALUE_PREDICATES = frozenset({
        "LeaderOf", "HeadOfState", "HeadOfGovernment", "President",
        "PrimeMinister", "SupremeLeader", "Monarch",
        "Capital", "Population", "GDP", "Area", "Currency",
        "GovernmentType", "OfficialLanguage", "SignatoryTo",
    })

    async def store_fact(self, fact: Fact, evidence: list[dict] | None = None, embedding=None) -> bool:
        if not self._available:
            return False
        try:
            # Normalize predicate and value before storage
            from .fact_normalize import (
                normalize_fact_predicate, normalize_fact_value,
                CANONICAL_FACT_PREDICATES,
            )
            fact.predicate = normalize_fact_predicate(fact.predicate)
            fact.value = normalize_fact_value(fact.value)

            # Reject vague "noted" predicate — agent must use a canonical form
            if fact.predicate.lower() == "noted":
                logger.warning(
                    "Rejected fact with predicate 'noted' (subject=%s). "
                    "Use a canonical predicate: 'is', 'has', 'located_in', "
                    "'member_of', 'operates_in'.",
                    fact.subject,
                )
                return False

            # Reject non-canonical predicates with helpful suggestions
            if fact.predicate not in CANONICAL_FACT_PREDICATES:
                from difflib import SequenceMatcher
                scored = sorted(
                    CANONICAL_FACT_PREDICATES,
                    key=lambda c: SequenceMatcher(
                        None, fact.predicate.lower(), c.lower()
                    ).ratio(),
                    reverse=True,
                )
                suggestions = scored[:5]
                logger.warning(
                    "Rejected fact with non-canonical predicate '%s' "
                    "(subject=%s). Did you mean: %s?",
                    fact.predicate,
                    fact.subject,
                    ", ".join(suggestions),
                )
                return False

            async with self._pool.acquire() as conn:
                # --- BUG 7 fix: Dedup hardening ---
                # Check for existing active fact with same triple regardless of valid_from
                existing = await conn.fetchrow("""
                    SELECT id, confidence FROM facts
                    WHERE LOWER(subject) = LOWER($1)
                    AND LOWER(predicate) = LOWER($2)
                    AND LOWER(value) = LOWER($3)
                    AND superseded_by IS NULL
                    AND valid_until IS NULL
                    LIMIT 1
                """, fact.subject, fact.predicate, fact.value)
                if existing:
                    # Update confidence if new is higher, skip insert
                    if fact.confidence > existing['confidence']:
                        await conn.execute(
                            "UPDATE facts SET confidence = $1, source_cycle = $2, updated_at = NOW() WHERE id = $3",
                            fact.confidence, fact.source_cycle, existing['id']
                        )
                    return True  # Fact already exists

                # --- Contradiction detection ---
                contradiction_id = None
                try:
                    from ...shared.contradictions import detect_contradiction, should_auto_create_hypothesis
                    existing_facts = await conn.fetch("""
                        SELECT id, subject, predicate, value, confidence FROM facts
                        WHERE LOWER(subject) = LOWER($1) AND superseded_by IS NULL AND valid_until IS NULL
                    """, fact.subject)
                    contradictions = detect_contradiction(
                        fact.subject, fact.predicate, fact.value,
                        [dict(r) for r in existing_facts]
                    )
                    if contradictions:
                        contradiction_id = contradictions[0]['id']
                        # Auto-create hypothesis if warranted
                        new_fact_dict = {
                            "subject": fact.subject,
                            "predicate": fact.predicate,
                            "value": fact.value,
                            "confidence": fact.confidence,
                        }
                        # Count signal references for involved entities
                        signal_ref_count = 0
                        try:
                            signal_ref_count = await conn.fetchval("""
                                SELECT COUNT(DISTINCT sel.signal_id)
                                FROM signal_entity_links sel
                                JOIN entity_profiles ep ON ep.id = sel.entity_id
                                WHERE LOWER(ep.canonical_name) = LOWER($1)
                            """, fact.subject) or 0
                        except Exception:
                            pass
                        if should_auto_create_hypothesis(
                            contradictions[0], new_fact_dict,
                            min_signal_refs=2, signal_ref_count=signal_ref_count,
                        ):
                            try:
                                await conn.execute("""
                                    INSERT INTO hypotheses (thesis, counter_thesis, status, created_cycle, last_evaluated_cycle)
                                    VALUES ($1, $2, 'active', $3, $3)
                                """,
                                    f"{fact.subject} {fact.predicate} {fact.value}",
                                    f"{contradictions[0]['subject']} {contradictions[0]['predicate']} {contradictions[0]['value']}",
                                    fact.source_cycle,
                                )
                                logger.info(
                                    "Auto-created hypothesis from contradiction: %s %s %s vs %s %s %s",
                                    fact.subject, fact.predicate, fact.value,
                                    contradictions[0]['subject'], contradictions[0]['predicate'], contradictions[0]['value'],
                                )
                            except Exception as he:
                                logger.debug("Auto-hypothesis creation failed: %s", he)
                except Exception as ce:
                    logger.debug("Contradiction detection failed: %s", ce)

                # Auto-supersede for volatile predicates (leadership, etc.)
                # "A LeaderOf B" should supersede any "X LeaderOf B" where X != A
                # Also supersede "B LeaderOf X" (wrong direction) for same entity
                if fact.predicate in self._VOLATILE_PREDICATES:
                    # Find facts being superseded so we can clean graph edges
                    superseded_rows = await conn.fetch(
                        """
                        SELECT subject, predicate, value FROM facts
                        WHERE predicate = $1
                        AND superseded_by IS NULL
                        AND (
                            (lower(value) = lower($2) AND lower(subject) != lower($3))
                            OR (lower(subject) = lower($2) AND lower(value) != lower($3))
                        )
                        """,
                        fact.predicate, fact.value, fact.subject,
                    )

                    await conn.execute(
                        """
                        UPDATE facts SET superseded_by = $1, valid_until = NOW(), updated_at = NOW()
                        WHERE predicate = $2
                        AND superseded_by IS NULL
                        AND valid_until IS NULL
                        AND (
                            (lower(value) = lower($3) AND lower(subject) != lower($4))
                            OR (lower(subject) = lower($3) AND lower(value) != lower($4))
                        )
                        """,
                        fact.id, fact.predicate, fact.value, fact.subject,
                    )

                    # Clean corresponding graph edges for superseded facts
                    if superseded_rows and self._graph and self._graph.available:
                        for row in superseded_rows:
                            try:
                                await self._graph.remove_relationship(
                                    row["subject"], row["value"], row["predicate"],
                                )
                            except Exception:
                                pass  # Best-effort graph cleanup

                # Auto-supersede for single-value predicates (same subject+predicate,
                # different value). E.g., "France Capital Paris" supersedes
                # "France Capital Vichy" if one already exists.
                # _VOLATILE_PREDICATES are a subset handled above with cross-subject
                # logic; this covers the broader set.
                if (fact.predicate in self._SINGLE_VALUE_PREDICATES
                        and fact.predicate not in self._VOLATILE_PREDICATES):
                    sv_superseded = await conn.fetch(
                        """
                        SELECT id, subject, predicate, value FROM facts
                        WHERE LOWER(subject) = LOWER($1)
                          AND LOWER(predicate) = LOWER($2)
                          AND LOWER(value) != LOWER($3)
                          AND superseded_by IS NULL
                          AND (valid_until IS NULL OR valid_until > NOW())
                        """,
                        fact.subject, fact.predicate, fact.value,
                    )
                    if sv_superseded:
                        await conn.execute(
                            """
                            UPDATE facts SET superseded_by = $1, valid_until = NOW(), updated_at = NOW()
                            WHERE LOWER(subject) = LOWER($2)
                              AND LOWER(predicate) = LOWER($3)
                              AND LOWER(value) != LOWER($4)
                              AND superseded_by IS NULL
                              AND (valid_until IS NULL OR valid_until > NOW())
                            """,
                            fact.id, fact.subject, fact.predicate, fact.value,
                        )
                        logger.info(
                            "Auto-superseded %d facts for %s %s (new value: %s)",
                            len(sv_superseded), fact.subject, fact.predicate,
                            fact.value[:60],
                        )

                # Resolve geo from subject/value
                geo_lat, geo_lon = None, None
                try:
                    from ..tools.builtins.geo import resolve_locations
                    geo = resolve_locations([fact.subject, fact.value])
                    if geo.get("coordinates"):
                        coord = geo["coordinates"][0]
                        geo_lat = coord["lat"]
                        geo_lon = coord["lon"]
                except Exception:
                    pass

                # Evidence set JSON
                evidence_json = json.dumps(evidence or [])

                # Resolve temporal bounds from Fact model (agent-provided or defaults)
                _valid_from = fact.valid_from  # None means DB default NOW()
                _valid_until = fact.valid_until  # None means open-ended

                # Build INSERT with optional new columns (evidence_set, contradiction_of)
                # Use try/except to gracefully handle missing columns on older schemas
                try:
                    await conn.execute(
                        """
                        INSERT INTO facts (id, subject, predicate, value, confidence, source_cycle,
                                          data, created_at, valid_from, valid_until, geo_lat, geo_lon,
                                          evidence_set, contradiction_of)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, NOW()), $10, $11, $12, $13::jsonb, $14)
                        ON CONFLICT (lower(subject), lower(predicate), lower(value), COALESCE(valid_from, '1970-01-01'::timestamptz)) DO UPDATE SET
                            confidence = GREATEST(facts.confidence, EXCLUDED.confidence),
                            source_cycle = EXCLUDED.source_cycle,
                            data = EXCLUDED.data,
                            valid_until = COALESCE(EXCLUDED.valid_until, facts.valid_until),
                            geo_lat = COALESCE(EXCLUDED.geo_lat, facts.geo_lat),
                            geo_lon = COALESCE(EXCLUDED.geo_lon, facts.geo_lon),
                            evidence_set = EXCLUDED.evidence_set,
                            contradiction_of = COALESCE(EXCLUDED.contradiction_of, facts.contradiction_of),
                            updated_at = NOW()
                        """,
                        fact.id, fact.subject, fact.predicate, fact.value,
                        fact.confidence, fact.source_cycle, fact.model_dump_json(),
                        fact.created_at, _valid_from, _valid_until,
                        geo_lat, geo_lon,
                        evidence_json, contradiction_id,
                    )
                except Exception:
                    # Fallback: columns may not exist yet on older schema
                    await conn.execute(
                        """
                        INSERT INTO facts (id, subject, predicate, value, confidence, source_cycle, data, created_at, valid_from, valid_until, geo_lat, geo_lon)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, NOW()), $10, $11, $12)
                        ON CONFLICT (lower(subject), lower(predicate), lower(value), COALESCE(valid_from, '1970-01-01'::timestamptz)) DO UPDATE SET
                            confidence = GREATEST(facts.confidence, EXCLUDED.confidence),
                            source_cycle = EXCLUDED.source_cycle,
                            data = EXCLUDED.data,
                            valid_until = COALESCE(EXCLUDED.valid_until, facts.valid_until),
                            geo_lat = COALESCE(EXCLUDED.geo_lat, facts.geo_lat),
                            geo_lon = COALESCE(EXCLUDED.geo_lon, facts.geo_lon),
                            updated_at = NOW()
                        """,
                        fact.id, fact.subject, fact.predicate, fact.value,
                        fact.confidence, fact.source_cycle, fact.model_dump_json(),
                        fact.created_at, _valid_from, _valid_until,
                        geo_lat, geo_lon,
                    )
            return True
        except Exception:
            return False

    async def query_facts(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        limit: int = 20,
        include_expired: bool = False,
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

            if not include_expired:
                # Only current facts: valid_until is NULL (open-ended) or still in the future
                conditions.append("(valid_until IS NULL OR valid_until > NOW())")
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
        """Get non-superseded, temporally current facts from the last N cycles.

        Guarantees the agent sees its own recent work regardless of
        confidence ranking or semantic similarity.  Excludes facts whose
        valid_until is in the past.
        """
        if not self._available:
            return []
        try:
            min_cycle = max(1, current_cycle - lookback)
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT data FROM facts "
                    "WHERE superseded_by IS NULL "
                    "  AND (valid_until IS NULL OR valid_until > NOW()) "
                    "  AND source_cycle >= $1 "
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

    # --- Signal operations (raw ingested material, formerly "events") ---

    async def save_signal(self, signal) -> bool:
        """Upsert a Signal into the signals table."""
        if not self._available:
            return False
        try:
            from ...shared.schemas.signals import Signal
            if not isinstance(signal, Signal):
                return False
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO signals (id, data, title, source_id, source_url, category,
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
                    signal.id,
                    signal.model_dump_json(),
                    signal.title,
                    signal.source_id,
                    signal.source_url,
                    str(signal.category.value if hasattr(signal.category, 'value') else signal.category),
                    signal.event_timestamp,
                    signal.language,
                    signal.confidence,
                    getattr(signal, 'guid', ''),
                    signal.created_at,
                )
            return True
        except asyncpg.ForeignKeyViolationError:
            logger.warning(
                "save_signal FK violation for signal %s (source_id=%s), retrying with source_id=NULL",
                signal.id, signal.source_id,
            )
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO signals (id, data, title, source_id, source_url, category,
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
                        signal.id,
                        signal.model_dump_json(),
                        signal.title,
                        signal.source_url,
                        str(signal.category.value if hasattr(signal.category, 'value') else signal.category),
                        signal.event_timestamp,
                        signal.language,
                        signal.confidence,
                        getattr(signal, 'guid', ''),
                        signal.created_at,
                    )
                return True
            except Exception as e2:
                logger.error("save_signal retry failed for signal %s: %s", signal.id, e2)
                return False
        except Exception as e:
            logger.error("save_signal failed for signal %s: %s", signal.id, e)
            return False

    # Backward-compat alias
    save_event = save_signal

    async def check_signal_guid(self, guid: str) -> dict | None:
        """Check if a signal with this GUID already exists. Returns {id, title} or None."""
        if not self._available or not guid:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, title FROM signals WHERE guid = $1 LIMIT 1",
                    guid,
                )
                if row:
                    return {"id": row["id"], "title": row["title"]}
        except Exception:
            pass
        return None

    # Backward-compat alias
    check_event_guid = check_signal_guid

    async def check_signal_source_url(self, source_url: str) -> dict | None:
        """Check if a signal with this source_url already exists."""
        if not self._available or not source_url:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, title FROM signals WHERE source_url = $1 LIMIT 1",
                    source_url,
                )
                if row:
                    return {"id": row["id"], "title": row["title"]}
        except Exception:
            pass
        return None

    # Backward-compat alias
    check_event_source_url = check_signal_source_url

    async def get_recent_signals_for_dedup(self, limit: int = 100) -> list:
        """Get recent signals (by created_at) for title-similarity dedup."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.signals import Signal
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT data FROM signals ORDER BY created_at DESC LIMIT $1",
                    limit,
                )
            return [Signal.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    # Backward-compat alias
    get_recent_events_for_dedup = get_recent_signals_for_dedup

    async def query_signals(
        self,
        category: str | None = None,
        source_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> list:
        """Query signals with optional filters."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.signals import Signal
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
                    f"SELECT data FROM signals {where} "
                    f"ORDER BY event_timestamp DESC NULLS LAST, created_at DESC LIMIT ${idx}",
                    *params,
                )
                return [Signal.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    # Backward-compat alias
    query_events = query_signals

    # --- Derived Event operations (real-world occurrences from signals) ---

    async def save_derived_event(self, event, lifecycle_status: str | None = None) -> bool:
        """Insert or update a derived event in events."""
        if not self._available:
            return False
        try:
            from ...shared.schemas.derived_events import DerivedEvent
            if not isinstance(event, DerivedEvent):
                return False
            async with self._pool.acquire() as conn:
                # Try with lifecycle columns first (cognitive architecture schema)
                try:
                    ls = lifecycle_status or "emerging"
                    await conn.execute(
                        """
                        INSERT INTO events (id, data, title, summary, category,
                                                    event_type, severity, time_start, time_end,
                                                    confidence, signal_count, source_method,
                                                    source_cycle, created_at, updated_at,
                                                    lifecycle_status, lifecycle_changed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, NOW(), $15, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            data = EXCLUDED.data,
                            title = EXCLUDED.title,
                            summary = EXCLUDED.summary,
                            category = EXCLUDED.category,
                            event_type = EXCLUDED.event_type,
                            severity = EXCLUDED.severity,
                            time_start = EXCLUDED.time_start,
                            time_end = EXCLUDED.time_end,
                            confidence = EXCLUDED.confidence,
                            signal_count = EXCLUDED.signal_count,
                            source_method = EXCLUDED.source_method,
                            source_cycle = EXCLUDED.source_cycle,
                            updated_at = NOW()
                        """,
                        event.id,
                        event.model_dump_json(),
                        event.title,
                        event.summary,
                        str(event.category.value if hasattr(event.category, 'value') else event.category),
                        event.event_type.value,
                        event.severity.value,
                        event.time_start,
                        event.time_end,
                        event.confidence,
                        event.signal_count,
                        event.source_method,
                        event.source_cycle,
                        event.created_at,
                        ls,
                    )
                except Exception:
                    # Fallback: lifecycle columns may not exist yet
                    await conn.execute(
                        """
                        INSERT INTO events (id, data, title, summary, category,
                                                    event_type, severity, time_start, time_end,
                                                    confidence, signal_count, source_method,
                                                    source_cycle, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            data = EXCLUDED.data,
                            title = EXCLUDED.title,
                            summary = EXCLUDED.summary,
                            category = EXCLUDED.category,
                            event_type = EXCLUDED.event_type,
                            severity = EXCLUDED.severity,
                            time_start = EXCLUDED.time_start,
                            time_end = EXCLUDED.time_end,
                            confidence = EXCLUDED.confidence,
                            signal_count = EXCLUDED.signal_count,
                            source_method = EXCLUDED.source_method,
                            source_cycle = EXCLUDED.source_cycle,
                            updated_at = NOW()
                        """,
                        event.id,
                        event.model_dump_json(),
                        event.title,
                        event.summary,
                        str(event.category.value if hasattr(event.category, 'value') else event.category),
                        event.event_type.value,
                        event.severity.value,
                        event.time_start,
                        event.time_end,
                        event.confidence,
                        event.signal_count,
                        event.source_method,
                        event.source_cycle,
                        event.created_at,
                    )
            return True
        except Exception as e:
            logger.error("save_derived_event failed for %s: %s", event.id, e)
            return False

    async def link_signal_to_event(
        self, signal_id: UUID, event_id: UUID, relevance: float = 1.0,
    ) -> bool:
        """Create a signal-event link and propagate entity links to the event."""
        if not self._available:
            return False
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO signal_event_links (signal_id, event_id, relevance) "
                    "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    signal_id, event_id, relevance,
                )
                # Auto-propagate: copy signal_entity_links → event_entity_links
                await conn.execute(
                    """
                    INSERT INTO event_entity_links (event_id, entity_id, role, confidence)
                    SELECT $1, entity_id, role, confidence
                    FROM signal_entity_links WHERE signal_id = $2
                    ON CONFLICT (event_id, entity_id) DO NOTHING
                    """,
                    event_id, signal_id,
                )
            return True
        except Exception as e:
            logger.debug("link_signal_to_event failed: %s", e)
            return False

    async def get_event_signals(self, event_id: UUID) -> list:
        """Get signals linked to a derived event."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.signals import Signal
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.data, sel.relevance
                    FROM signals s
                    JOIN signal_event_links sel ON s.id = sel.signal_id
                    WHERE sel.event_id = $1
                    ORDER BY sel.relevance DESC, s.event_timestamp DESC NULLS LAST
                    """,
                    event_id,
                )
            return [
                {"signal": Signal.model_validate_json(r["data"]), "relevance": r["relevance"]}
                for r in rows
            ]
        except Exception:
            return []

    async def get_signal_events(self, signal_id: UUID) -> list:
        """Get derived events that a signal is linked to."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.derived_events import DerivedEvent
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT ed.data, sel.relevance
                    FROM events ed
                    JOIN signal_event_links sel ON ed.id = sel.event_id
                    WHERE sel.signal_id = $1
                    ORDER BY sel.relevance DESC
                    """,
                    signal_id,
                )
            return [
                {"event": DerivedEvent.model_validate_json(r["data"]), "relevance": r["relevance"]}
                for r in rows
            ]
        except Exception:
            return []

    async def query_derived_events(
        self,
        category: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        min_signal_count: int | None = None,
        source_method: str | None = None,
        limit: int = 20,
    ) -> list:
        """Query derived events with optional filters."""
        if not self._available:
            return []
        try:
            from ...shared.schemas.derived_events import DerivedEvent
            conditions: list[str] = []
            params: list = []
            idx = 1

            if category:
                conditions.append(f"category = ${idx}")
                params.append(category)
                idx += 1
            if event_type:
                conditions.append(f"event_type = ${idx}")
                params.append(event_type)
                idx += 1
            if severity:
                conditions.append(f"severity = ${idx}")
                params.append(severity)
                idx += 1
            if since:
                conditions.append(f"time_start >= ${idx}")
                params.append(since)
                idx += 1
            if until:
                conditions.append(f"time_start <= ${idx}")
                params.append(until)
                idx += 1
            if min_signal_count is not None:
                conditions.append(f"signal_count >= ${idx}")
                params.append(min_signal_count)
                idx += 1
            if source_method:
                conditions.append(f"source_method = ${idx}")
                params.append(source_method)
                idx += 1

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT data FROM events {where} "
                    f"ORDER BY time_start DESC NULLS LAST, created_at DESC LIMIT ${idx}",
                    *params,
                )
                return [DerivedEvent.model_validate_json(row["data"]) for row in rows]
        except Exception:
            return []

    async def find_overlapping_events(
        self,
        actors: list[str],
        locations: list[str],
        time_start: datetime | None,
        time_end: datetime | None,
        category: str | None = None,
    ) -> list:
        """Find existing derived events that overlap with given actors/locations/time.

        Used by the clustering algorithm to merge signals into existing events
        rather than creating duplicates.
        """
        if not self._available:
            return []
        try:
            from ...shared.schemas.derived_events import DerivedEvent
            conditions = ["1=1"]
            params: list = []
            idx = 1

            # Time overlap: event's window overlaps with our window
            if time_start:
                conditions.append(f"(time_end IS NULL OR time_end >= ${idx})")
                params.append(time_start - __import__("datetime").timedelta(hours=24))
                idx += 1
            if time_end:
                conditions.append(f"(time_start IS NULL OR time_start <= ${idx})")
                params.append(time_end + __import__("datetime").timedelta(hours=24))
                idx += 1
            if category:
                conditions.append(f"category = ${idx}")
                params.append(category)
                idx += 1

            where = "WHERE " + " AND ".join(conditions)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT data FROM events {where} "
                    f"ORDER BY created_at DESC LIMIT 50",
                    *params,
                )

            # Filter by entity overlap in Python (JSONB queries for array overlap are clunky)
            our_entities = set(a.lower() for a in actors + locations if a)
            if not our_entities:
                return []

            results = []
            for row in rows:
                ev = DerivedEvent.model_validate_json(row["data"])
                ev_entities = set(
                    a.lower() for a in (ev.actors + ev.locations) if a
                )
                if not ev_entities:
                    continue
                overlap = len(our_entities & ev_entities) / len(our_entities | ev_entities)
                if overlap >= 0.3:
                    results.append({"event": ev, "overlap": overlap})

            return sorted(results, key=lambda x: x["overlap"], reverse=True)
        except Exception:
            return []

    async def count_derived_events(self) -> int:
        """Count total derived events."""
        if not self._available:
            return 0
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM events")
                return row["cnt"] if row else 0
        except Exception:
            return 0

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

                    # Resolve geo from canonical name
                    geo_lat, geo_lon, geo_country, geo_region = None, None, None, None
                    try:
                        from ..tools.builtins.geo import resolve_locations
                        geo = resolve_locations([profile.canonical_name])
                        if geo.get("coordinates"):
                            coord = geo["coordinates"][0]
                            geo_lat = coord["lat"]
                            geo_lon = coord["lon"]
                        if geo.get("countries"):
                            geo_country = geo["countries"][0]
                        if geo.get("regions"):
                            geo_region = geo["regions"][0]
                    except Exception:
                        pass

                    await conn.execute(
                        """
                        INSERT INTO entity_profiles (id, data, canonical_name, entity_type,
                                                     version, completeness_score, last_event_link_at,
                                                     geo_lat, geo_lon, geo_country, geo_region,
                                                     created_at, updated_at, last_verified_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW(), NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            data = EXCLUDED.data,
                            canonical_name = EXCLUDED.canonical_name,
                            entity_type = EXCLUDED.entity_type,
                            version = EXCLUDED.version,
                            completeness_score = EXCLUDED.completeness_score,
                            last_event_link_at = EXCLUDED.last_event_link_at,
                            geo_lat = COALESCE(EXCLUDED.geo_lat, entity_profiles.geo_lat),
                            geo_lon = COALESCE(EXCLUDED.geo_lon, entity_profiles.geo_lon),
                            geo_country = COALESCE(EXCLUDED.geo_country, entity_profiles.geo_country),
                            geo_region = COALESCE(EXCLUDED.geo_region, entity_profiles.geo_region),
                            updated_at = NOW(),
                            last_verified_at = NOW()
                        """,
                        profile.id,
                        profile.model_dump_json(),
                        profile.canonical_name,
                        profile.entity_type.value,
                        profile.version,
                        profile.completeness_score,
                        profile.last_event_link_at,
                        geo_lat, geo_lon, geo_country, geo_region,
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
                    ON CONFLICT (event_id, entity_id) DO UPDATE SET
                        confidence = EXCLUDED.confidence,
                        role = EXCLUDED.role
                    """,
                    link.event_id,
                    link.entity_id,
                    link.role,
                    link.confidence,
                )
            return True
        except Exception:
            return False

    async def get_entity_signals(self, entity_id: UUID, limit: int = 20) -> list[dict]:
        """Get signals linked to an entity."""
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT s.data AS signal_data, l.role, l.confidence
                    FROM signal_entity_links l
                    JOIN signals s ON s.id = l.signal_id
                    WHERE l.entity_id = $1
                    ORDER BY s.event_timestamp DESC NULLS LAST, s.created_at DESC
                    LIMIT $2
                    """,
                    entity_id, limit,
                )
                return [
                    {
                        "event": json.loads(row["signal_data"]),  # key kept as "event" for compat
                        "role": row["role"],
                        "confidence": row["confidence"],
                    }
                    for row in rows
                ]
        except Exception:
            return []

    # Backward-compat alias
    get_entity_events = get_entity_signals

    async def get_signal_entities(self, signal_id: UUID) -> list[dict]:
        """Get entities linked to a signal."""
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT p.data AS profile_data, l.role, l.confidence
                    FROM signal_entity_links l
                    JOIN entity_profiles p ON p.id = l.entity_id
                    WHERE l.signal_id = $1
                    """,
                    signal_id,
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

    # Backward-compat alias
    get_event_entities = get_signal_entities

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

    # --- Entity merge ---

    async def merge_entities(
        self,
        keep_id: str,
        remove_id: str,
        preview: bool = False,
        graph_store=None,
    ) -> dict:
        """Merge remove_id entity into keep_id entity.

        Consolidates event links, facts, profile versions, and optionally
        graph vertices/edges.  When preview=True returns counts of what
        would change without modifying data.

        Args:
            keep_id:     UUID string of the entity to keep.
            remove_id:   UUID string of the entity to absorb and delete.
            preview:     If True, only return counts -- no mutations.
            graph_store:  Optional GraphStore instance for AGE edge remapping.

        Returns dict with keys:
            keep_name, remove_name, events_moved, facts_moved,
            links_dupes_skipped, versions_deleted, graph_edges_moved,
            success, error
        """
        if not self._available:
            return {"success": False, "error": "Structured store not available"}

        from uuid import UUID as _UUID
        try:
            keep_uuid = _UUID(keep_id)
            remove_uuid = _UUID(remove_id)
        except (ValueError, AttributeError) as exc:
            return {"success": False, "error": f"Invalid UUID: {exc}"}

        if keep_uuid == remove_uuid:
            return {"success": False, "error": "keep_id and remove_id must be different"}

        try:
            async with self._pool.acquire() as conn:
                # Verify both entities exist
                keep_row = await conn.fetchrow(
                    "SELECT id, canonical_name FROM entity_profiles WHERE id = $1",
                    keep_uuid,
                )
                remove_row = await conn.fetchrow(
                    "SELECT id, canonical_name FROM entity_profiles WHERE id = $1",
                    remove_uuid,
                )
                if not keep_row:
                    return {"success": False, "error": f"Keep entity {keep_id} not found"}
                if not remove_row:
                    return {"success": False, "error": f"Remove entity {remove_id} not found"}

                keep_name = keep_row["canonical_name"]
                remove_name = remove_row["canonical_name"]

                # --- Count affected rows ---
                events_to_move = await conn.fetchval(
                    "SELECT count(*) FROM signal_entity_links WHERE entity_id = $1",
                    remove_uuid,
                )
                # Links that would be dupes (already linked to keep entity for same event+role)
                dupe_links = await conn.fetchval(
                    """SELECT count(*) FROM signal_entity_links r
                       WHERE r.entity_id = $1
                         AND EXISTS (
                           SELECT 1 FROM signal_entity_links k
                           WHERE k.entity_id = $2
                             AND k.event_id = r.event_id
                             AND k.role = r.role
                         )""",
                    remove_uuid, keep_uuid,
                )
                facts_to_move = await conn.fetchval(
                    "SELECT count(*) FROM facts WHERE LOWER(subject) = LOWER($1)",
                    remove_name,
                )
                # Facts that would collide (same triple already exists for keep)
                dupe_facts = await conn.fetchval(
                    """SELECT count(*) FROM facts f
                       WHERE LOWER(f.subject) = LOWER($1)
                         AND EXISTS (
                           SELECT 1 FROM facts k
                           WHERE LOWER(k.subject) = LOWER($2)
                             AND LOWER(k.predicate) = LOWER(f.predicate)
                             AND LOWER(k.value) = LOWER(f.value)
                         )""",
                    remove_name, keep_name,
                )
                versions_to_delete = await conn.fetchval(
                    "SELECT count(*) FROM entity_profile_versions WHERE entity_id = $1",
                    remove_uuid,
                )

                # Graph edge count (if graph store available)
                graph_edges_to_move = 0
                if graph_store and graph_store.available:
                    try:
                        async with graph_store._pool.acquire() as gconn:
                            remove_esc = graph_store._escape(remove_name)
                            # Outgoing edges
                            out_rows = await graph_store._cypher(gconn, f"""
                                MATCH (n {{name: '{remove_esc}'}})-[r]->()
                                RETURN count(r) AS cnt
                            """, cols="cnt agtype")
                            # Incoming edges
                            in_rows = await graph_store._cypher(gconn, f"""
                                MATCH ()-[r]->(n {{name: '{remove_esc}'}})
                                RETURN count(r) AS cnt
                            """, cols="cnt agtype")
                            out_cnt = int(out_rows[0]["cnt"]) if out_rows else 0
                            in_cnt = int(in_rows[0]["cnt"]) if in_rows else 0
                            graph_edges_to_move = out_cnt + in_cnt
                    except Exception as ge:
                        logger.warning("Graph edge count failed: %s", ge)

                result = {
                    "keep_name": keep_name,
                    "remove_name": remove_name,
                    "events_to_move": events_to_move,
                    "events_moved": events_to_move - dupe_links,
                    "links_dupes_skipped": dupe_links,
                    "facts_to_move": facts_to_move,
                    "facts_moved": facts_to_move - dupe_facts,
                    "facts_dupes_skipped": dupe_facts,
                    "versions_deleted": versions_to_delete,
                    "graph_edges_to_move": graph_edges_to_move,
                    "preview": preview,
                    "success": True,
                }

                if preview:
                    return result

            # --- Execute merge in a transaction ---
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # 1. Move signal_entity_links: reassign to keep, skip dupes
                    await conn.execute(
                        """UPDATE signal_entity_links
                           SET entity_id = $1
                           WHERE entity_id = $2
                             AND NOT EXISTS (
                               SELECT 1 FROM signal_entity_links k
                               WHERE k.entity_id = $1
                                 AND k.signal_id = signal_entity_links.signal_id
                                 AND k.role = signal_entity_links.role
                             )""",
                        keep_uuid, remove_uuid,
                    )

                    # 2. Delete remaining signal_entity_links for remove (dupes)
                    await conn.execute(
                        "DELETE FROM signal_entity_links WHERE entity_id = $1",
                        remove_uuid,
                    )

                    # 3. Move facts: update subject to keep_name, skip dupes
                    #    The unique index on (lower(subject), lower(predicate), lower(value))
                    #    would block conflicting updates, so we exclude them.
                    await conn.execute(
                        """UPDATE facts
                           SET subject = $1, updated_at = NOW()
                           WHERE LOWER(subject) = LOWER($2)
                             AND NOT EXISTS (
                               SELECT 1 FROM facts k
                               WHERE LOWER(k.subject) = LOWER($1)
                                 AND LOWER(k.predicate) = LOWER(facts.predicate)
                                 AND LOWER(k.value) = LOWER(facts.value)
                             )""",
                        keep_name, remove_name,
                    )

                    # 4. Delete remaining dupe facts (subject still matches remove)
                    await conn.execute(
                        "DELETE FROM facts WHERE LOWER(subject) = LOWER($1)",
                        remove_name,
                    )

                    # 5. Delete entity_profile_versions for remove
                    await conn.execute(
                        "DELETE FROM entity_profile_versions WHERE entity_id = $1",
                        remove_uuid,
                    )

                    # 6. Merge aliases: add remove_name as alias on keep profile
                    keep_data_row = await conn.fetchrow(
                        "SELECT data FROM entity_profiles WHERE id = $1", keep_uuid,
                    )
                    if keep_data_row:
                        keep_data = (
                            json.loads(keep_data_row["data"])
                            if isinstance(keep_data_row["data"], str)
                            else keep_data_row["data"]
                        )
                        aliases = keep_data.get("aliases", [])
                        if remove_name not in aliases:
                            aliases.append(remove_name)
                            keep_data["aliases"] = aliases
                            await conn.execute(
                                "UPDATE entity_profiles SET data = $1::jsonb, updated_at = NOW() WHERE id = $2",
                                json.dumps(keep_data, default=str), keep_uuid,
                            )

                    # 7. Delete the old entity profile
                    await conn.execute(
                        "DELETE FROM entity_profiles WHERE id = $1",
                        remove_uuid,
                    )

                logger.info(
                    "Entity merge complete: %s (%s) merged into %s (%s)",
                    remove_name, remove_id, keep_name, keep_id,
                )

            # --- Graph merge (outside Postgres transaction) ---
            graph_edges_moved = 0
            if graph_store and graph_store.available:
                try:
                    graph_edges_moved = await self._merge_graph_vertices(
                        graph_store, keep_name, remove_name,
                    )
                except Exception as ge:
                    logger.warning("Graph merge failed (Postgres merge succeeded): %s", ge)
                    result["graph_error"] = str(ge)

            result["graph_edges_moved"] = graph_edges_moved
            return result

        except Exception as exc:
            logger.error("Entity merge failed: %s", exc)
            return {"success": False, "error": str(exc)}

    @staticmethod
    async def _merge_graph_vertices(
        graph_store, keep_name: str, remove_name: str,
    ) -> int:
        """Remap all edges from remove vertex to keep vertex, then delete remove.

        Returns the number of edges remapped.
        """
        edges_moved = 0
        keep_esc = graph_store._escape(keep_name)
        remove_esc = graph_store._escape(remove_name)

        async with graph_store._pool.acquire() as conn:
            # Get all outgoing edges from remove vertex
            out_edges = await graph_store._cypher(conn, f"""
                MATCH (old {{name: '{remove_esc}'}})-[r]->(target)
                RETURN type(r) AS rel_type, target.name AS target_name
            """, cols="rel_type agtype, target_name agtype")

            for edge in out_edges:
                target_name = edge.get("target_name", "")
                rel_type = edge.get("rel_type", "")
                if not target_name or not rel_type or target_name == keep_name:
                    continue
                rel_label = graph_store._sanitize_label(rel_type)
                target_esc = graph_store._escape(str(target_name))
                # Create edge from keep to target (MERGE to avoid dupes)
                await graph_store._cypher(conn, f"""
                    MATCH (k {{name: '{keep_esc}'}}), (t {{name: '{target_esc}'}})
                    MERGE (k)-[r:{rel_label}]->(t)
                    RETURN r
                """, cols="r agtype")
                edges_moved += 1

            # Get all incoming edges to remove vertex
            in_edges = await graph_store._cypher(conn, f"""
                MATCH (source)-[r]->(old {{name: '{remove_esc}'}})
                RETURN source.name AS source_name, type(r) AS rel_type
            """, cols="source_name agtype, rel_type agtype")

            for edge in in_edges:
                source_name = edge.get("source_name", "")
                rel_type = edge.get("rel_type", "")
                if not source_name or not rel_type or source_name == keep_name:
                    continue
                rel_label = graph_store._sanitize_label(rel_type)
                source_esc = graph_store._escape(str(source_name))
                # Create edge from source to keep (MERGE to avoid dupes)
                await graph_store._cypher(conn, f"""
                    MATCH (s {{name: '{source_esc}'}}), (k {{name: '{keep_esc}'}})
                    MERGE (s)-[r:{rel_label}]->(k)
                    RETURN r
                """, cols="r agtype")
                edges_moved += 1

            # Delete all edges connected to remove vertex, then delete vertex
            try:
                await graph_store._cypher(conn, f"""
                    MATCH (old {{name: '{remove_esc}'}})-[r]-()
                    DELETE r
                    RETURN count(r) AS cnt
                """, cols="cnt agtype")
            except Exception:
                pass  # No edges left is fine

            try:
                await graph_store._cypher(conn, f"""
                    MATCH (old {{name: '{remove_esc}'}})
                    DELETE old
                    RETURN count(old) AS cnt
                """, cols="cnt agtype")
            except Exception:
                pass  # Vertex may not exist in graph

        return edges_moved

    # --- Prediction / hypothesis tracking ---

    async def create_prediction(self, hypothesis: str, source_cycle: int,
                                category: str = "", region: str = "",
                                confidence: float = 0.5, source_type: str = "report") -> str:
        """Create a new prediction/hypothesis. Returns ID."""
        if not self._available:
            return ""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO predictions (hypothesis, source_cycle, source_type,
                                             category, region, confidence)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                    """,
                    hypothesis, source_cycle, source_type,
                    category, region, confidence,
                )
                return str(row["id"])
        except Exception as e:
            logger.error("Failed to create prediction: %s", e)
            return ""

    async def update_prediction(self, prediction_id: str, status: str = None,
                                evidence_for: str = None, evidence_against: str = None,
                                confidence: float = None, resolution_note: str = None,
                                resolution_cycle: int = None) -> bool:
        """Update a prediction. Append evidence, change status."""
        if not self._available:
            return False
        try:
            pid = UUID(prediction_id)
        except (ValueError, TypeError):
            return False
        try:
            async with self._pool.acquire() as conn:
                # Verify prediction exists
                row = await conn.fetchrow("SELECT id FROM predictions WHERE id = $1", pid)
                if not row:
                    return False

                if evidence_for:
                    await conn.execute(
                        "UPDATE predictions SET evidence_for = array_append(evidence_for, $1), "
                        "updated_at = NOW() WHERE id = $2",
                        evidence_for, pid,
                    )
                if evidence_against:
                    await conn.execute(
                        "UPDATE predictions SET evidence_against = array_append(evidence_against, $1), "
                        "updated_at = NOW() WHERE id = $2",
                        evidence_against, pid,
                    )
                if status:
                    await conn.execute(
                        "UPDATE predictions SET status = $1, updated_at = NOW() WHERE id = $2",
                        status, pid,
                    )
                if confidence is not None:
                    await conn.execute(
                        "UPDATE predictions SET confidence = $1, updated_at = NOW() WHERE id = $2",
                        confidence, pid,
                    )
                if resolution_note:
                    await conn.execute(
                        "UPDATE predictions SET resolution_note = $1, updated_at = NOW() WHERE id = $2",
                        resolution_note, pid,
                    )
                if resolution_cycle is not None:
                    await conn.execute(
                        "UPDATE predictions SET resolution_cycle = $1, updated_at = NOW() WHERE id = $2",
                        resolution_cycle, pid,
                    )
            return True
        except Exception as e:
            logger.error("Failed to update prediction %s: %s", prediction_id, e)
            return False

    async def list_predictions(self, status: str = None, limit: int = 50) -> list[dict]:
        """List predictions, optionally filtered by status."""
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                if status:
                    rows = await conn.fetch(
                        "SELECT id, hypothesis, source_cycle, source_type, category, region, "
                        "status, confidence, evidence_for, evidence_against, "
                        "resolution_cycle, resolution_note, created_at, updated_at "
                        "FROM predictions WHERE status = $1 "
                        "ORDER BY created_at DESC LIMIT $2",
                        status, limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT id, hypothesis, source_cycle, source_type, category, region, "
                        "status, confidence, evidence_for, evidence_against, "
                        "resolution_cycle, resolution_note, created_at, updated_at "
                        "FROM predictions ORDER BY created_at DESC LIMIT $1",
                        limit,
                    )
            return [
                {
                    "id": str(r["id"]),
                    "hypothesis": r["hypothesis"],
                    "source_cycle": r["source_cycle"],
                    "source_type": r["source_type"] or "report",
                    "category": r["category"] or "",
                    "region": r["region"] or "",
                    "status": r["status"] or "open",
                    "confidence": r["confidence"],
                    "evidence_for": list(r["evidence_for"] or []),
                    "evidence_against": list(r["evidence_against"] or []),
                    "resolution_cycle": r["resolution_cycle"],
                    "resolution_note": r["resolution_note"] or "",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ]
        except Exception as e:
            logger.error("Failed to list predictions: %s", e)
            return []

    async def get_prediction(self, prediction_id: str) -> dict | None:
        """Get a single prediction by ID."""
        if not self._available:
            return None
        try:
            pid = UUID(prediction_id)
        except (ValueError, TypeError):
            return None
        try:
            async with self._pool.acquire() as conn:
                r = await conn.fetchrow(
                    "SELECT id, hypothesis, source_cycle, source_type, category, region, "
                    "status, confidence, evidence_for, evidence_against, "
                    "resolution_cycle, resolution_note, created_at, updated_at "
                    "FROM predictions WHERE id = $1",
                    pid,
                )
                if not r:
                    return None
                return {
                    "id": str(r["id"]),
                    "hypothesis": r["hypothesis"],
                    "source_cycle": r["source_cycle"],
                    "source_type": r["source_type"] or "report",
                    "category": r["category"] or "",
                    "region": r["region"] or "",
                    "status": r["status"] or "open",
                    "confidence": r["confidence"],
                    "evidence_for": list(r["evidence_for"] or []),
                    "evidence_against": list(r["evidence_against"] or []),
                    "resolution_cycle": r["resolution_cycle"],
                    "resolution_note": r["resolution_note"] or "",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
        except Exception as e:
            logger.error("Failed to get prediction %s: %s", prediction_id, e)
            return None

    # --- Source credibility tracking ---

    async def compute_source_quality_scores(self) -> int:
        """Recompute source_quality_score for all active sources.

        Quality score (0.0-1.0) = weighted average of:
        - entity_link_rate: fraction of source's events that have entity links (50%)
        - event_yield: events_produced / fetch_success (normalized, 30%)
        - reliability: inverse of failure rate (20%)

        Returns number of sources updated.
        """
        if not self._available:
            return 0
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        s.id,
                        s.events_produced_count,
                        s.fetch_success_count,
                        s.fetch_failure_count,
                        COUNT(DISTINCT eel.signal_id) AS linked_signals,
                        COUNT(DISTINCT e.id) AS total_signals
                    FROM sources s
                    LEFT JOIN signals e ON e.source_id = s.id
                    LEFT JOIN signal_entity_links eel ON eel.signal_id = e.id
                    WHERE s.status = 'active'
                    GROUP BY s.id, s.events_produced_count,
                             s.fetch_success_count, s.fetch_failure_count
                """)

                updated = 0
                for r in rows:
                    total = r["total_events"] or 0
                    linked = r["linked_events"] or 0
                    successes = r["fetch_success_count"] or 0
                    failures = r["fetch_failure_count"] or 0
                    produced = r["events_produced_count"] or 0

                    # Entity link rate (0-1): what fraction of events got entity links
                    link_rate = linked / max(total, 1)

                    # Event yield (0-1): events produced per successful fetch, normalized
                    yield_per_fetch = produced / max(successes, 1)
                    # Normalize: 5+ events/fetch = 1.0
                    event_yield = min(yield_per_fetch / 5.0, 1.0)

                    # Reliability (0-1): success rate
                    total_fetches = successes + failures
                    reliability = successes / max(total_fetches, 1)

                    # Weighted composite
                    score = round(
                        0.5 * link_rate + 0.3 * event_yield + 0.2 * reliability,
                        3,
                    )

                    await conn.execute(
                        "UPDATE sources SET source_quality_score = $1 WHERE id = $2",
                        score, r["id"],
                    )
                    updated += 1

                return updated
        except Exception as e:
            logger.error("Failed to compute source quality scores: %s", e)
            return 0

    async def get_source_quality_summary(self, limit: int = 10) -> dict:
        """Get top and bottom sources by quality score for ORIENT injection."""
        if not self._available:
            return {}
        try:
            async with self._pool.acquire() as conn:
                top = await conn.fetch(
                    "SELECT name, source_quality_score FROM sources "
                    "WHERE status = 'active' AND source_quality_score > 0 "
                    "ORDER BY source_quality_score DESC LIMIT $1",
                    limit,
                )
                bottom = await conn.fetch(
                    "SELECT name, source_quality_score FROM sources "
                    "WHERE status = 'active' AND events_produced_count > 0 "
                    "ORDER BY source_quality_score ASC LIMIT $1",
                    limit,
                )
                return {
                    "top": [{"name": r["name"], "score": r["source_quality_score"]} for r in top],
                    "bottom": [{"name": r["name"], "score": r["source_quality_score"]} for r in bottom],
                }
        except Exception as e:
            logger.error("Failed to get source quality summary: %s", e)
            return {}

    # --- Proposed edges (relationship inference queue) ---

    async def list_proposed_edges(
        self, status: str = "pending", limit: int = 50
    ) -> list[dict]:
        """List proposed edges filtered by status."""
        if not self._available:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, source_entity, target_entity, relationship_type, "
                    "confidence, evidence_text, source_cycle, status, "
                    "reviewed_at, created_at "
                    "FROM proposed_edges WHERE status = $1 "
                    "ORDER BY created_at DESC LIMIT $2",
                    status, limit,
                )
                return [
                    {
                        "id": str(r["id"]),
                        "source_entity": r["source_entity"],
                        "target_entity": r["target_entity"],
                        "relationship_type": r["relationship_type"],
                        "confidence": r["confidence"],
                        "evidence_text": r["evidence_text"] or "",
                        "source_cycle": r["source_cycle"],
                        "status": r["status"],
                        "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("Failed to list proposed edges: %s", e)
            return []

    async def review_proposed_edge(
        self, edge_id: str, action: str
    ) -> bool:
        """Approve or reject a proposed edge. Returns True on success."""
        if not self._available or action not in ("approved", "rejected"):
            return False
        try:
            from uuid import UUID as _UUID
            eid = _UUID(edge_id)
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE proposed_edges SET status = $1, reviewed_at = NOW() "
                    "WHERE id = $2 AND status = 'pending'",
                    action, eid,
                )
                return "UPDATE 1" in result
        except Exception as e:
            logger.error("Failed to review proposed edge %s: %s", edge_id, e)
            return False

    # --- Hypothesis (ACH) operations ---

    async def create_hypothesis(
        self,
        thesis: str,
        counter_thesis: str,
        created_cycle: int,
        situation_id: str | None = None,
        diagnostic_evidence: list[dict] | None = None,
    ) -> str | None:
        """Create a competing hypothesis pair. Returns hypothesis ID."""
        if not self._available:
            return None
        try:
            import json as _json
            from uuid import UUID as _UUID
            sit_id = _UUID(situation_id) if situation_id else None
            diag = _json.dumps(diagnostic_evidence or [])
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """INSERT INTO hypotheses
                       (situation_id, thesis, counter_thesis, diagnostic_evidence,
                        created_cycle, last_evaluated_cycle)
                       VALUES ($1, $2, $3, $4::jsonb, $5, $5)
                       RETURNING id""",
                    sit_id, thesis, counter_thesis, diag, created_cycle,
                )
                return str(row["id"]) if row else None
        except Exception as e:
            logger.error("Failed to create hypothesis: %s", e)
            return None

    async def evaluate_hypothesis(
        self,
        hypothesis_id: str,
        supporting_signal: str | None = None,
        refuting_signal: str | None = None,
        status: str | None = None,
        evaluated_cycle: int | None = None,
        diagnostic_update: dict | None = None,
    ) -> bool:
        """Add evidence to or update status of a hypothesis."""
        if not self._available:
            return False
        try:
            from uuid import UUID as _UUID
            hid = _UUID(hypothesis_id)
            async with self._pool.acquire() as conn:
                updates = ["updated_at = NOW()"]
                params = []
                idx = 1

                balance_delta = 0
                if supporting_signal:
                    sid = _UUID(supporting_signal)
                    updates.append(f"supporting_signals = array_append(supporting_signals, ${idx}::uuid)")
                    balance_delta += 1
                    params.append(sid)
                    idx += 1

                if refuting_signal:
                    rid = _UUID(refuting_signal)
                    updates.append(f"refuting_signals = array_append(refuting_signals, ${idx}::uuid)")
                    balance_delta -= 1
                    params.append(rid)
                    idx += 1

                if balance_delta != 0:
                    sign = "+" if balance_delta > 0 else "-"
                    updates.append(f"evidence_balance = evidence_balance {sign} {abs(balance_delta)}")

                if status:
                    updates.append(f"status = ${idx}")
                    params.append(status)
                    idx += 1

                if evaluated_cycle is not None:
                    updates.append(f"last_evaluated_cycle = ${idx}")
                    params.append(evaluated_cycle)
                    idx += 1

                if diagnostic_update:
                    import json as _json
                    updates.append(f"diagnostic_evidence = ${idx}::jsonb")
                    params.append(_json.dumps(diagnostic_update))
                    idx += 1

                params.append(hid)
                sql = f"UPDATE hypotheses SET {', '.join(updates)} WHERE id = ${idx}"
                result = await conn.execute(sql, *params)
                return "UPDATE 1" in result
        except Exception as e:
            logger.error("Failed to evaluate hypothesis %s: %s", hypothesis_id, e)
            return False

    async def list_hypotheses(
        self, status: str | None = None, situation_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        """List hypotheses, optionally filtered by status or situation."""
        if not self._available:
            return []
        try:
            from uuid import UUID as _UUID
            conditions = []
            params = []
            idx = 1

            if status:
                conditions.append(f"h.status = ${idx}")
                params.append(status)
                idx += 1

            if situation_id:
                conditions.append(f"h.situation_id = ${idx}")
                params.append(_UUID(situation_id))
                idx += 1

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(limit)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(f"""
                    SELECT h.id, h.situation_id, h.thesis, h.counter_thesis,
                           h.diagnostic_evidence, h.evidence_balance, h.status,
                           h.created_cycle, h.last_evaluated_cycle,
                           array_length(h.supporting_signals, 1) as support_count,
                           array_length(h.refuting_signals, 1) as refute_count,
                           s.name as situation_name
                    FROM hypotheses h
                    LEFT JOIN situations s ON s.id = h.situation_id
                    {where}
                    ORDER BY h.updated_at DESC
                    LIMIT ${idx}
                """, *params)

                results = []
                for r in rows:
                    d = dict(r)
                    # Convert UUIDs to strings for JSON serialization
                    for k, v in d.items():
                        if hasattr(v, 'hex'):  # UUID
                            d[k] = str(v)
                    # Ensure JSONB fields are parsed (asyncpg may return as string)
                    de = d.get("diagnostic_evidence")
                    if isinstance(de, str):
                        import json as _json
                        try:
                            d["diagnostic_evidence"] = _json.loads(de)
                        except Exception:
                            d["diagnostic_evidence"] = []
                    results.append(d)
                return results
        except Exception as e:
            logger.error("Failed to list hypotheses: %s", e)
            return []
