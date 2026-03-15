"""Source scheduler — determines which sources are due for fetching.

Queries Postgres for active sources whose next_fetch_at has passed,
prioritizing never-fetched sources.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class ScheduledSource:
    """A source ready to be fetched."""

    id: UUID
    name: str
    url: str
    source_type: str
    category: str
    language: str
    fetch_interval_minutes: int
    last_successful_fetch_at: datetime | None
    consecutive_failures: int
    query_template: str
    auth_config: dict  # from data JSONB
    config: dict  # from data JSONB


async def get_due_sources(pool: asyncpg.Pool, limit: int = 10) -> list[ScheduledSource]:
    """Get sources that are due for fetching.

    Priority:
      1. Never-fetched active sources (last_successful_fetch_at IS NULL)
      2. Sources past their next_fetch_at
      3. Sources with no next_fetch_at set (legacy, treat as due)

    Returns up to `limit` sources ordered by priority.
    """
    try:
        rows = await pool.fetch(
            """
            SELECT
                id, name, url, source_type,
                COALESCE(category, '') as category,
                language,
                COALESCE(fetch_interval_minutes, 60) as fetch_interval_minutes,
                last_successful_fetch_at,
                COALESCE(consecutive_failures, 0) as consecutive_failures,
                data
            FROM sources
            WHERE status = 'active'
              AND (
                  next_fetch_at IS NULL
                  OR next_fetch_at <= NOW()
                  OR last_successful_fetch_at IS NULL
              )
            ORDER BY
                -- Never-fetched first
                CASE WHEN last_successful_fetch_at IS NULL THEN 0 ELSE 1 END,
                -- Then by how overdue they are
                COALESCE(next_fetch_at, '1970-01-01'::timestamptz) ASC
            LIMIT $1
            """,
            limit,
        )

        sources = []
        for row in rows:
            data = {}
            try:
                import json
                raw = row["data"]
                if isinstance(raw, str):
                    data = json.loads(raw)
                elif isinstance(raw, dict):
                    data = raw
            except Exception:
                pass

            sources.append(ScheduledSource(
                id=row["id"],
                name=row["name"],
                url=row["url"],
                source_type=row["source_type"],
                category=row["category"],
                language=row["language"] or "en",
                fetch_interval_minutes=row["fetch_interval_minutes"],
                last_successful_fetch_at=row["last_successful_fetch_at"],
                consecutive_failures=row["consecutive_failures"],
                query_template=data.get("query_template", ""),
                auth_config=data.get("auth_config", data.get("config", {})),
                config=data.get("config", {}),
            ))

        return sources

    except Exception as e:
        logger.error("Failed to query due sources: %s", e)
        return []


async def initialize_next_fetch(pool: asyncpg.Pool) -> int:
    """Set next_fetch_at for any active sources that don't have it yet.

    Called at service startup. Returns count of sources updated.
    """
    try:
        result = await pool.execute(
            """
            UPDATE sources SET
                next_fetch_at = NOW()
            WHERE status = 'active'
              AND next_fetch_at IS NULL
            """
        )
        count = int(result.split()[-1]) if result else 0
        if count:
            logger.info("Initialized next_fetch_at for %d sources", count)
        return count
    except Exception as e:
        logger.error("Failed to initialize next_fetch: %s", e)
        return 0
