"""
SQL schema extensions for the cognitive architecture.

Defines ALTER TABLE / CREATE INDEX statements for extending existing tables
with confidence components, evidence sets, lifecycle status, and provenance
tracking.  These are stored as strings and applied via :func:`apply_extensions`.

All statements use ``IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS`` so they
are safe to re-run (idempotent).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extension SQL blocks
# ---------------------------------------------------------------------------

SIGNALS_EXTENSIONS: str = """
    -- Confidence breakdown stored as JSONB for composite recomputation
    ALTER TABLE signals ADD COLUMN IF NOT EXISTS confidence_components JSONB DEFAULT '{}';

    -- Full processing provenance (ingestion -> NER -> dedup -> embed -> cluster)
    ALTER TABLE signals ADD COLUMN IF NOT EXISTS provenance JSONB DEFAULT '{}';
"""

FACTS_EXTENSIONS: str = """
    -- Evidence set: array of evidence items supporting this fact
    ALTER TABLE facts ADD COLUMN IF NOT EXISTS evidence_set JSONB DEFAULT '[]';

    -- Back-reference to the fact this one contradicts (if any)
    ALTER TABLE facts ADD COLUMN IF NOT EXISTS contradiction_of UUID;

    -- Confidence components for recomputation
    ALTER TABLE facts ADD COLUMN IF NOT EXISTS confidence_components JSONB DEFAULT '{}';

    -- Index for contradiction lookups
    CREATE INDEX IF NOT EXISTS idx_facts_contradiction_of
        ON facts(contradiction_of) WHERE contradiction_of IS NOT NULL;
"""

EVENTS_EXTENSIONS: str = """
    -- Lifecycle state machine status
    ALTER TABLE events ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'emerging';

    -- When the lifecycle status last changed
    ALTER TABLE events ADD COLUMN IF NOT EXISTS lifecycle_changed_at TIMESTAMPTZ DEFAULT NOW();

    -- Hierarchical events: parent event for sub-event grouping
    ALTER TABLE events ADD COLUMN IF NOT EXISTS parent_event_id UUID;

    -- Velocity tracking for ACTIVE -> EVOLVING transitions
    ALTER TABLE events ADD COLUMN IF NOT EXISTS velocity_change REAL NOT NULL DEFAULT 0.0;

    -- Last signal linkage time for staleness checks
    ALTER TABLE events ADD COLUMN IF NOT EXISTS last_signal_at TIMESTAMPTZ;

    -- Confidence components
    ALTER TABLE events ADD COLUMN IF NOT EXISTS confidence_components JSONB DEFAULT '{}';

    -- Indexes for lifecycle queries
    CREATE INDEX IF NOT EXISTS idx_events_lifecycle_status
        ON events(lifecycle_status);
    CREATE INDEX IF NOT EXISTS idx_events_parent_event
        ON events(parent_event_id) WHERE parent_event_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_events_last_signal_at
        ON events(last_signal_at);
"""

WATCHLIST_EXTENSIONS: str = """
    -- Structured query for programmatic matching (beyond keyword matching)
    ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS structured_query JSONB;
"""

# Ordered list of all extension blocks
_ALL_EXTENSIONS: list[tuple[str, str]] = [
    ("signals",   SIGNALS_EXTENSIONS),
    ("facts",     FACTS_EXTENSIONS),
    ("events",    EVENTS_EXTENSIONS),
    ("watchlist", WATCHLIST_EXTENSIONS),
]


# ---------------------------------------------------------------------------
# Apply / check helpers
# ---------------------------------------------------------------------------

async def apply_extensions(pool) -> dict[str, bool]:
    """Apply all schema extensions.  Returns {table: success} dict.

    Parameters
    ----------
    pool : asyncpg.Pool
        Active connection pool.

    Returns
    -------
    dict[str, bool]
        Mapping of table name to whether the extension was applied
        successfully.
    """
    results: dict[str, bool] = {}
    async with pool.acquire() as conn:
        for table_name, sql in _ALL_EXTENSIONS:
            try:
                await conn.execute(sql)
                results[table_name] = True
                logger.info("Schema extension applied: %s", table_name)
            except Exception as exc:
                logger.error(
                    "Failed to apply schema extension for %s: %s",
                    table_name, exc,
                )
                results[table_name] = False
    return results


async def check_extensions(pool) -> dict[str, bool]:
    """Check which schema extensions have been applied.

    Parameters
    ----------
    pool : asyncpg.Pool
        Active connection pool.

    Returns
    -------
    dict[str, bool]
        Mapping of ``table.column`` to whether the column exists.
    """
    checks: list[tuple[str, str, str]] = [
        # (label, table_name, column_name)
        ("signals.confidence_components",   "signals",   "confidence_components"),
        ("signals.provenance",              "signals",   "provenance"),
        ("facts.evidence_set",              "facts",     "evidence_set"),
        ("facts.contradiction_of",          "facts",     "contradiction_of"),
        ("facts.confidence_components",     "facts",     "confidence_components"),
        ("events.lifecycle_status",         "events",    "lifecycle_status"),
        ("events.lifecycle_changed_at",     "events",    "lifecycle_changed_at"),
        ("events.parent_event_id",          "events",    "parent_event_id"),
        ("events.velocity_change",          "events",    "velocity_change"),
        ("events.last_signal_at",           "events",    "last_signal_at"),
        ("events.confidence_components",    "events",    "confidence_components"),
        ("watchlist.structured_query",      "watchlist", "structured_query"),
    ]

    results: dict[str, bool] = {}
    async with pool.acquire() as conn:
        for label, table_name, column_name in checks:
            try:
                exists = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = $1 AND column_name = $2
                    )
                    """,
                    table_name,
                    column_name,
                )
                results[label] = bool(exists)
            except Exception:
                results[label] = False

    return results
