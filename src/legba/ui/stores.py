"""
Store holder for the Operator Console UI.

Owns instances of all read-only stores needed by the UI.
Uses the same store classes as the agent — no query reimplementation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from qdrant_client import QdrantClient

from ..agent.memory.structured import StructuredStore
from ..agent.memory.graph import GraphStore
from ..agent.memory.registers import RegisterStore
from ..agent.memory.opensearch import OpenSearchStore
from ..shared.config import PostgresConfig, RedisConfig, OpenSearchConfig, QdrantConfig, LLMConfig

logger = logging.getLogger(__name__)


class StoreHolder:
    """Owns connections to all backing stores for the UI."""

    def __init__(
        self,
        pg: PostgresConfig,
        redis_cfg: RedisConfig,
        os_cfg: OpenSearchConfig,
        audit_cfg: OpenSearchConfig,
        qdrant_cfg: QdrantConfig | None = None,
        llm_cfg: LLMConfig | None = None,
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

        # Qdrant (sync client — runs in thread via asyncio.to_thread)
        _qcfg = qdrant_cfg or QdrantConfig.from_env()
        self._qdrant: QdrantClient | None = None
        self._qdrant_host = _qcfg.host
        self._qdrant_port = _qcfg.port
        self._qdrant_available = False

        # Embedding config for semantic search
        _llm = llm_cfg or LLMConfig.from_env()
        self._embedding_api_base = _llm.embedding_api_base or _llm.api_base
        self._embedding_api_key = _llm.embedding_api_key or _llm.api_key
        self._embedding_model = _llm.embedding_model

    async def connect(self) -> None:
        await self.structured.connect()
        await self.graph.connect()
        await self.registers.connect()
        await self.opensearch.connect()
        await self.audit.connect()
        # Qdrant sync client — no async connect needed, just instantiate
        try:
            self._qdrant = QdrantClient(host=self._qdrant_host, port=self._qdrant_port, timeout=10)
            # Verify connectivity with a lightweight call
            self._qdrant.get_collections()
            self._qdrant_available = True
        except Exception:
            logger.warning("Qdrant unavailable at %s:%s", self._qdrant_host, self._qdrant_port)
            self._qdrant_available = False

    async def close(self) -> None:
        await self.structured.close()
        await self.graph.close()
        await self.registers.close()
        await self.opensearch.close()
        await self.audit.close()
        if self._qdrant:
            try:
                self._qdrant.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Qdrant memory helpers
    # ------------------------------------------------------------------

    async def get_memories(
        self, collection: str, limit: int = 50, offset: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Scroll memories from a Qdrant collection.

        Returns (points, next_offset) where next_offset is the page token
        for the next page, or None if no more results.
        """
        import asyncio

        if not self._qdrant_available:
            return [], None

        def _scroll():
            return self._qdrant.scroll(
                collection_name=collection,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )

        try:
            points, next_offset = await asyncio.to_thread(_scroll)
            results = []
            for p in points:
                results.append({"id": p.id, **(p.payload or {})})
            return results, next_offset
        except Exception as exc:
            logger.warning("Qdrant scroll failed: %s", exc)
            return [], None

    async def search_memories(
        self, collection: str, query_text: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Semantic search over a Qdrant collection.

        Embeds the query via the embedding API, then searches Qdrant.
        """
        import asyncio

        if not self._qdrant_available:
            return []

        # Get embedding vector for the query
        vector = await self._embed_text(query_text)
        if not vector:
            return []

        def _search():
            return self._qdrant.query_points(
                collection_name=collection,
                query=vector,
                limit=limit,
                with_payload=True,
            )

        try:
            results = await asyncio.to_thread(_search)
            return [
                {"id": r.id, "score": r.score, **(r.payload or {})}
                for r in results.points
            ]
        except Exception as exc:
            logger.warning("Qdrant search failed: %s", exc)
            return []

    async def count_memories(self, collection: str) -> int:
        """Return the number of points in a Qdrant collection."""
        import asyncio

        if not self._qdrant_available:
            return 0

        def _count():
            info = self._qdrant.get_collection(collection)
            return info.points_count or 0

        try:
            return await asyncio.to_thread(_count)
        except Exception:
            return 0

    async def _embed_text(self, text: str) -> list[float] | None:
        """Call the embedding API to vectorize text."""
        url = f"{self._embedding_api_base}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self._embedding_api_key:
            headers["Authorization"] = f"Bearer {self._embedding_api_key}"

        payload = {
            "model": self._embedding_model,
            "input": text,
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["embedding"]
        except Exception as exc:
            logger.warning("Embedding API call failed: %s", exc)
            return None

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

    async def count_facts(self) -> int:
        if not self.structured._available:
            return 0
        try:
            async with self.structured._pool.acquire() as conn:
                return await conn.fetchval("SELECT count(*) FROM facts")
        except Exception:
            return 0

    async def count_situations(self, statuses: tuple[str, ...] | None = None) -> int:
        if not self.structured._available:
            return 0
        try:
            async with self.structured._pool.acquire() as conn:
                if statuses:
                    placeholders = ", ".join(f"${i+1}" for i in range(len(statuses)))
                    return await conn.fetchval(
                        f"SELECT count(*) FROM situations WHERE status IN ({placeholders})",
                        *statuses,
                    )
                return await conn.fetchval("SELECT count(*) FROM situations")
        except Exception:
            return 0

    async def count_watchlist(self) -> int:
        if not self.structured._available:
            return 0
        try:
            async with self.structured._pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT count(*) FROM watchlist WHERE active = true"
                )
        except Exception:
            return 0

    async def fetch_active_situations(self, limit: int = 5) -> list[dict]:
        """Fetch active/escalating situations for dashboard display."""
        if not self.structured._available:
            return []
        try:
            async with self.structured._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, name, status, category, intensity_score, updated_at "
                    "FROM situations "
                    "WHERE status IN ('active', 'escalating') "
                    "ORDER BY intensity_score DESC, updated_at DESC "
                    "LIMIT $1",
                    limit,
                )
                return [
                    {
                        "id": str(row["id"]),
                        "name": row["name"],
                        "status": row["status"],
                        "category": row["category"],
                        "intensity_score": row["intensity_score"] or 0.0,
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]
        except Exception:
            return []

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
