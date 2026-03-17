-- Signals/Events Migration
-- Renames events → signals, events_derived → events
-- Run with: docker exec legba-postgres-1 psql -U legba -d legba -f /path/to/migrate_signals_events.sql
--
-- IMPORTANT: Stop ingestion + supervisor BEFORE running this.
-- The migration takes ~1 second on the current dataset.

BEGIN;

-- =========================================================================
-- 1. Core table renames
-- =========================================================================

-- Rename current events table to signals (raw ingested material)
ALTER TABLE events RENAME TO signals;

-- Rename derived events table to events (real-world occurrences)
ALTER TABLE events_derived RENAME TO events;

-- =========================================================================
-- 2. signal_event_links: add FK to signals table
-- =========================================================================

ALTER TABLE signal_event_links
    ADD CONSTRAINT fk_sel_signal FOREIGN KEY (signal_id)
    REFERENCES signals(id) ON DELETE CASCADE;

-- Update FK on event_id to reference new events table (was events_derived)
ALTER TABLE signal_event_links
    DROP CONSTRAINT IF EXISTS signal_event_links_event_id_fkey;
ALTER TABLE signal_event_links
    ADD CONSTRAINT fk_sel_event FOREIGN KEY (event_id)
    REFERENCES events(id) ON DELETE CASCADE;

-- =========================================================================
-- 3. Entity links: event_entity_links → signal_entity_links
-- =========================================================================

ALTER TABLE event_entity_links RENAME TO signal_entity_links;
ALTER TABLE signal_entity_links RENAME COLUMN event_id TO signal_id;

-- New event_entity_links for derived events
CREATE TABLE IF NOT EXISTS event_entity_links (
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id UUID NOT NULL REFERENCES entity_profiles(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'mentioned',
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, entity_id, role)
);
CREATE INDEX IF NOT EXISTS idx_new_eel_entity ON event_entity_links(entity_id);
CREATE INDEX IF NOT EXISTS idx_new_eel_event ON event_entity_links(event_id);

-- =========================================================================
-- 4. Situation links: situation_events → situation_signals
-- =========================================================================

ALTER TABLE situation_events RENAME TO situation_signals;
ALTER TABLE situation_signals RENAME COLUMN event_id TO signal_id;

-- Drop old FK and add new one pointing to signals
ALTER TABLE situation_signals DROP CONSTRAINT IF EXISTS situation_events_event_id_fkey;
ALTER TABLE situation_signals
    ADD CONSTRAINT fk_ss_signal FOREIGN KEY (signal_id)
    REFERENCES signals(id) ON DELETE CASCADE;

-- Drop old PK and recreate
ALTER TABLE situation_signals DROP CONSTRAINT IF EXISTS situation_events_pkey;
ALTER TABLE situation_signals ADD PRIMARY KEY (situation_id, signal_id);

-- New situation_events for derived events
CREATE TABLE IF NOT EXISTS situation_events (
    situation_id UUID NOT NULL REFERENCES situations(id) ON DELETE CASCADE,
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    relevance REAL NOT NULL DEFAULT 1.0,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (situation_id, event_id)
);

-- =========================================================================
-- 5. Watch triggers: rename event_id → signal_id, add event_id for derived events
-- =========================================================================

ALTER TABLE watch_triggers RENAME COLUMN event_id TO signal_id;

-- Drop old FK and add new one
ALTER TABLE watch_triggers DROP CONSTRAINT IF EXISTS watch_triggers_event_id_fkey;
ALTER TABLE watch_triggers
    ADD CONSTRAINT fk_wt_signal FOREIGN KEY (signal_id)
    REFERENCES signals(id) ON DELETE CASCADE;

-- Add event_id column for reinforcement alerts
ALTER TABLE watch_triggers
    ADD COLUMN IF NOT EXISTS event_id UUID REFERENCES events(id) ON DELETE SET NULL;

-- =========================================================================
-- 6. Index renames (cosmetic, for clarity)
-- =========================================================================

ALTER INDEX IF EXISTS idx_events_source RENAME TO idx_signals_source;
ALTER INDEX IF EXISTS idx_events_category RENAME TO idx_signals_category;
ALTER INDEX IF EXISTS idx_events_timestamp RENAME TO idx_signals_timestamp;
ALTER INDEX IF EXISTS idx_events_language RENAME TO idx_signals_language;
ALTER INDEX IF EXISTS idx_events_created RENAME TO idx_signals_created;
ALTER INDEX IF EXISTS idx_events_guid RENAME TO idx_signals_guid;
ALTER INDEX IF EXISTS idx_events_source_url RENAME TO idx_signals_source_url;
ALTER INDEX IF EXISTS idx_events_guid_unique RENAME TO idx_signals_guid_unique;

-- Rename old entity link indexes
ALTER INDEX IF EXISTS idx_eel_entity RENAME TO idx_sel_entity;
ALTER INDEX IF EXISTS idx_eel_event RENAME TO idx_sel_signal_entity;

-- Rename derived event indexes (from Phase 0 names)
ALTER INDEX IF EXISTS idx_derived_events_category RENAME TO idx_events_category;
ALTER INDEX IF EXISTS idx_derived_events_type RENAME TO idx_events_type;
ALTER INDEX IF EXISTS idx_derived_events_severity RENAME TO idx_events_severity;
ALTER INDEX IF EXISTS idx_derived_events_time_start RENAME TO idx_events_time_start;
ALTER INDEX IF EXISTS idx_derived_events_created RENAME TO idx_events_created;

COMMIT;

-- =========================================================================
-- Post-migration verification (not in transaction)
-- =========================================================================

-- Verify tables exist
SELECT 'signals' AS tbl, count(*) FROM signals
UNION ALL SELECT 'events', count(*) FROM events
UNION ALL SELECT 'signal_event_links', count(*) FROM signal_event_links
UNION ALL SELECT 'signal_entity_links', count(*) FROM signal_entity_links
UNION ALL SELECT 'situation_signals', count(*) FROM situation_signals;
