"""
Store holder for the Operator Console UI.

Owns instances of all read-only stores needed by the UI.
Uses the same store classes as the agent — no query reimplementation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from ..agent.memory.structured import StructuredStore
from ..agent.memory.graph import GraphStore
from ..agent.memory.registers import RegisterStore
from ..agent.memory.opensearch import OpenSearchStore
from ..shared.config import PostgresConfig, RedisConfig, OpenSearchConfig


class StoreHolder:
    """Owns connections to all backing stores for the UI."""

    def __init__(
        self,
        pg: PostgresConfig,
        redis_cfg: RedisConfig,
        os_cfg: OpenSearchConfig,
        audit_cfg: OpenSearchConfig,
    ):
        self.structured = StructuredStore(pg.dsn, pool_min=1, pool_max=3)
        self.graph = GraphStore(pg.dsn)
        self.registers = RegisterStore(
            host=redis_cfg.host,
            port=redis_cfg.port,
            db=redis_cfg.db,
            password=redis_cfg.password,
        )
        self.opensearch = OpenSearchStore(os_cfg)
        self.audit = OpenSearchStore(audit_cfg)

    async def connect(self) -> None:
        await self.structured.connect()
        await self.graph.connect()
        await self.registers.connect()
        await self.opensearch.connect()
        await self.audit.connect()

    async def close(self) -> None:
        await self.structured.close()
        await self.graph.close()
        await self.registers.close()
        await self.opensearch.close()
        await self.audit.close()

    # ------------------------------------------------------------------
    # Read-only helpers (queries not in existing stores)
    # ------------------------------------------------------------------

    async def count_entities(self) -> int:
        if not self.structured._available:
            return 0
        try:
            async with self.structured._pool.acquire() as conn:
                return await conn.fetchval("SELECT count(*) FROM entity_profiles")
        except Exception:
            return 0

    async def count_events(self) -> int:
        if not self.structured._available:
            return 0
        try:
            async with self.structured._pool.acquire() as conn:
                return await conn.fetchval("SELECT count(*) FROM events")
        except Exception:
            return 0

    async def count_sources(self) -> int:
        if not self.structured._available:
            return 0
        try:
            async with self.structured._pool.acquire() as conn:
                return await conn.fetchval("SELECT count(*) FROM sources WHERE status = 'active'")
        except Exception:
            return 0

    async def count_goals(self) -> int:
        if not self.structured._available:
            return 0
        try:
            async with self.structured._pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT count(*) FROM goals WHERE status = 'active'"
                )
        except Exception:
            return 0

    async def count_relationships(self) -> int:
        if not self.graph.available:
            return 0
        try:
            results = await self.graph.execute_cypher(
                "MATCH ()-[r]->() RETURN count(r) AS cnt"
            )
            if results and "cnt" in results[0]:
                val = results[0]["cnt"]
                return int(val) if val is not None else 0
            return 0
        except Exception:
            return 0

    async def get_event(self, event_id: UUID) -> Any:
        if not self.structured._available:
            return None
        try:
            from ..shared.schemas.events import Event
            async with self.structured._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM events WHERE id = $1", event_id
                )
                if row:
                    return Event.model_validate_json(row["data"])
            return None
        except Exception:
            return None

    async def list_entity_versions(self, entity_id: UUID) -> list[dict]:
        if not self.structured._available:
            return []
        try:
            async with self.structured._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT version, cycle_number, created_at "
                    "FROM entity_profile_versions "
                    "WHERE entity_id = $1 "
                    "ORDER BY version DESC",
                    entity_id,
                )
                return [
                    {
                        "version": row["version"],
                        "cycle_number": row["cycle_number"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
        except Exception:
            return []
