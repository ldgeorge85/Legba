"""
Episodic Memory (Qdrant)

Stores episodes (actions, observations, lessons, cycle summaries) as
vector embeddings for similarity-based retrieval.

Two collections:
- short_term: recent episodes (hours to days), high granularity
- long_term: significant episodes, consolidated summaries, lessons
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from ...shared.schemas.memory import Episode


class EpisodicStore:
    """Qdrant-backed episodic memory with short-term, long-term, and facts collections."""

    SHORT_TERM = "legba_short_term"
    LONG_TERM = "legba_long_term"
    FACTS = "legba_facts"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6333,
        vector_size: int = 1024,
        short_term_collection: str | None = None,
        long_term_collection: str | None = None,
        facts_collection: str | None = None,
    ):
        self._host = host
        self._port = port
        self._vector_size = vector_size
        if short_term_collection:
            self.SHORT_TERM = short_term_collection
        if long_term_collection:
            self.LONG_TERM = long_term_collection
        if facts_collection:
            self.FACTS = facts_collection
        self._client: AsyncQdrantClient | None = None
        self._available = False

    async def connect(self) -> None:
        try:
            self._client = AsyncQdrantClient(host=self._host, port=self._port)
            # Ensure collections exist
            for name in [self.SHORT_TERM, self.LONG_TERM, self.FACTS]:
                collections = await self._client.get_collections()
                exists = any(c.name == name for c in collections.collections)
                if not exists:
                    await self._client.create_collection(
                        collection_name=name,
                        vectors_config=VectorParams(
                            size=self._vector_size,
                            distance=Distance.COSINE,
                        ),
                    )
            self._available = True
        except Exception:
            self._available = False

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    async def store_episode(
        self,
        episode: Episode,
        collection: str | None = None,
    ) -> bool:
        """Store an episode with its embedding vector."""
        if not self._available or episode.embedding is None:
            return False

        target = collection or self.SHORT_TERM

        try:
            await self._client.upsert(
                collection_name=target,
                points=[
                    PointStruct(
                        id=str(episode.id),
                        vector=episode.embedding,
                        payload={
                            "cycle_number": episode.cycle_number,
                            "episode_type": episode.episode_type.value,
                            "content": episode.content,
                            "significance": episode.significance,
                            "goal_id": str(episode.goal_id) if episode.goal_id else None,
                            "tool_name": episode.tool_name,
                            "tags": episode.tags,
                            "created_at": episode.created_at.isoformat(),
                            **episode.metadata,
                        },
                    )
                ],
            )
            return True
        except Exception:
            return False

    async def search_similar(
        self,
        query_vector: list[float],
        collection: str | None = None,
        limit: int = 5,
        min_score: float = 0.0,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for similar episodes by embedding vector."""
        if not self._available:
            return []

        target = collection or self.SHORT_TERM
        query_filter = None

        if filters:
            conditions = []
            for key, value in filters.items():
                conditions.append(
                    FieldCondition(key=key, match=MatchValue(value=value))
                )
            query_filter = Filter(must=conditions)

        try:
            results = await self._client.query_points(
                collection_name=target,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
                score_threshold=min_score,
            )

            return [
                {
                    "id": r.id,
                    "score": r.score,
                    **r.payload,
                }
                for r in results.points
            ]
        except Exception:
            return []

    async def search_both(
        self,
        query_vector: list[float],
        limit: int = 5,
        decay_hours: float = 168.0,
    ) -> list[dict[str, Any]]:
        """
        Search across both short-term and long-term collections.

        Applies time-based relevance decay: older episodes get their similarity
        score reduced by an exponential decay factor based on age.

        Args:
            decay_hours: Half-life in hours. Score halves every this many hours.
                         Default 168 (1 week). Set to 0 to disable decay.
        """
        short = await self.search_similar(query_vector, self.SHORT_TERM, limit=limit)
        long = await self.search_similar(query_vector, self.LONG_TERM, limit=limit)

        # Merge and apply decay
        combined = short + long
        if decay_hours > 0:
            import math
            now = datetime.now(timezone.utc)
            for ep in combined:
                created_str = ep.get("created_at")
                if created_str:
                    try:
                        created = datetime.fromisoformat(created_str)
                        age_hours = (now - created).total_seconds() / 3600.0
                        decay_factor = math.exp(-0.693 * age_hours / decay_hours)  # ln(2) ≈ 0.693
                        ep["raw_score"] = ep.get("score", 0)
                        ep["score"] = ep.get("score", 0) * decay_factor
                        ep["age_hours"] = round(age_hours, 1)
                    except (ValueError, TypeError):
                        pass

        combined.sort(key=lambda x: x.get("score", 0), reverse=True)
        return combined[:limit]

    async def promote_to_long_term(self, episode_id: str, vector: list[float], payload: dict) -> bool:
        """Move an episode from short-term to long-term."""
        if not self._available:
            return False

        try:
            await self._client.upsert(
                collection_name=self.LONG_TERM,
                points=[PointStruct(id=episode_id, vector=vector, payload=payload)],
            )
            await self._client.delete(
                collection_name=self.SHORT_TERM,
                points_selector=[episode_id],
            )
            return True
        except Exception:
            return False

    # --- Fact semantic index ---

    async def store_fact_embedding(
        self,
        fact_id: str,
        text: str,
        embedding: list[float],
        subject: str,
        predicate: str,
        value: str,
        confidence: float,
    ) -> bool:
        """Store a fact's embedding in Qdrant for semantic retrieval."""
        if not self._available:
            return False

        try:
            await self._client.upsert(
                collection_name=self.FACTS,
                points=[
                    PointStruct(
                        id=fact_id,
                        vector=embedding,
                        payload={
                            "fact_id": fact_id,
                            "subject": subject,
                            "predicate": predicate,
                            "value": value,
                            "text": text,
                            "confidence": confidence,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                ],
            )
            return True
        except Exception:
            return False

    async def search_facts(
        self,
        query_vector: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search facts by semantic similarity. No time decay (facts are knowledge, not events)."""
        if not self._available:
            return []

        try:
            results = await self._client.query_points(
                collection_name=self.FACTS,
                query=query_vector,
                limit=limit,
            )

            return [
                {
                    "fact_id": r.payload.get("fact_id"),
                    "subject": r.payload.get("subject", ""),
                    "predicate": r.payload.get("predicate", ""),
                    "value": r.payload.get("value", ""),
                    "confidence": r.payload.get("confidence", 1.0),
                    "score": r.score,
                    "source": "semantic",
                }
                for r in results.points
            ]
        except Exception:
            return []

    async def remove_fact_embedding(self, fact_id: str) -> bool:
        """Remove a fact from the semantic index (e.g., when superseded)."""
        if not self._available:
            return False

        try:
            await self._client.delete(
                collection_name=self.FACTS,
                points_selector=[fact_id],
            )
            return True
        except Exception:
            return False
