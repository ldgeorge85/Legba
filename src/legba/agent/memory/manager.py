"""
Memory Manager

Unified interface across all memory layers.
Provides retrieval (for ORIENT) and storage (for PERSIST) operations.
"""

from __future__ import annotations

from typing import Any

from ...shared.config import LegbaConfig
from ...shared.schemas.memory import Episode, Fact
from ...shared.schemas.goals import Goal
from ..log import CycleLogger
from .registers import RegisterStore
from .episodic import EpisodicStore
from .structured import StructuredStore
from .graph import GraphStore


class MemoryManager:
    """
    Unified memory interface.

    Coordinates across Redis (registers), Qdrant (episodic), and
    Postgres (structured) for both retrieval and storage.
    """

    def __init__(self, config: LegbaConfig, logger: CycleLogger):
        self.config = config
        self.logger = logger

        self.registers = RegisterStore(
            host=config.redis.host,
            port=config.redis.port,
            db=config.redis.db,
            password=config.redis.password,
        )
        self.episodic = EpisodicStore(
            host=config.qdrant.host,
            port=config.qdrant.port,
            vector_size=config.llm.embedding_dimensions,
            short_term_collection=config.agent.qdrant_short_term,
            long_term_collection=config.agent.qdrant_long_term,
            facts_collection=config.agent.qdrant_facts,
        )
        self.structured = StructuredStore(
            dsn=config.postgres.dsn,
            pool_min=config.agent.pg_pool_min,
            pool_max=config.agent.pg_pool_max,
        )
        self.graph = GraphStore(dsn=config.postgres.dsn)  # Own pool (AGE needs LOAD + search_path)

    async def connect(self) -> None:
        """Connect to all memory backends. Each degrades gracefully."""
        await self.registers.connect()
        self.logger.log_memory("connect", "redis",
                               fallback=self.registers._using_fallback)

        await self.episodic.connect()
        self.logger.log_memory("connect", "qdrant",
                               available=self.episodic._available)

        await self.structured.connect()
        self.logger.log_memory("connect", "postgres",
                               available=self.structured._available)

        # Graph store gets its own pool (AGE connections need LOAD + search_path)
        await self.graph.connect()
        self.logger.log_memory("connect", "graph",
                               available=self.graph._available)

        # Wire graph into structured store for edge cleanup on fact supersede
        self.structured._graph = self.graph

    async def close(self) -> None:
        await self.registers.close()
        await self.episodic.close()
        await self.graph.close()
        await self.structured.close()

    # --- Retrieval (ORIENT phase) ---

    async def get_cycle_number(self) -> int:
        return await self.registers.get_int("cycle_number", default=0)

    async def increment_cycle(self) -> int:
        return await self.registers.incr("cycle_number")

    async def retrieve_context(
        self,
        query_embedding: list[float] | None = None,
        limit: int = 5,
        current_cycle: int | None = None,
    ) -> dict[str, Any]:
        """
        Retrieve relevant context from all memory layers.

        Returns a dict with keys: registers, episodes, goals, facts.
        """
        context: dict[str, Any] = {}

        # Registers (always available)
        context["registers"] = await self.registers.get_all_registers()

        # Episodic memories (if embedding available)
        if query_embedding:
            episodes = await self.episodic.search_both(
                query_vector=query_embedding,
                limit=limit,
            )
            context["episodes"] = episodes
            self.logger.log_memory("retrieve", "qdrant", count=len(episodes))
        else:
            context["episodes"] = []

        # Active goals
        goals = await self.structured.get_active_goals()
        context["goals"] = [g.model_dump() for g in goals]
        self.logger.log_memory("retrieve", "postgres_goals", count=len(goals))

        # Facts — semantic (Qdrant) + structured (Postgres), merged and deduped
        facts_limit = self.config.agent.facts_retrieval_limit
        semantic_facts: list[dict] = []
        if query_embedding:
            semantic_facts = await self.episodic.search_facts(
                query_vector=query_embedding,
                limit=facts_limit,
            )

        # Filter semantic facts against Postgres to exclude expired/superseded
        # facts that still have Qdrant embeddings.
        if semantic_facts and self.structured._available:
            try:
                from uuid import UUID as _UUID
                sem_ids = []
                for sf in semantic_facts:
                    try:
                        sem_ids.append(_UUID(sf.get("fact_id", "")))
                    except (ValueError, TypeError):
                        pass
                if sem_ids:
                    async with self.structured._pool.acquire() as conn:
                        active_rows = await conn.fetch(
                            "SELECT id FROM facts "
                            "WHERE id = ANY($1) "
                            "  AND superseded_by IS NULL "
                            "  AND (valid_until IS NULL OR valid_until > NOW())",
                            sem_ids,
                        )
                        active_ids = {str(r["id"]) for r in active_rows}
                        semantic_facts = [
                            sf for sf in semantic_facts
                            if sf.get("fact_id", "") in active_ids
                        ]
            except Exception:
                pass  # If check fails, proceed with unfiltered semantic facts

        structured_facts = await self.structured.query_facts(limit=facts_limit)

        # Merge: semantic results first (most relevant), then structured (recent/confident)
        seen_ids: set[str] = set()
        merged: list[dict] = []
        for f in semantic_facts:
            fid = f.get("fact_id", "")
            if fid and fid not in seen_ids:
                seen_ids.add(fid)
                merged.append(f)
        for f in structured_facts:
            fid = str(f.id)
            if fid not in seen_ids:
                seen_ids.add(fid)
                merged.append(f.model_dump())

        # Recent-cycle facts: always include the agent's recent work
        if current_cycle is not None:
            recent_facts = await self.structured.query_facts_recent(
                current_cycle=current_cycle, lookback=5, limit=facts_limit,
            )
            for f in recent_facts:
                fid = str(f.id)
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    merged.append(f.model_dump())
        else:
            recent_facts = []

        # Deduplicate by subject — max 2 facts per subject to prevent crowding
        MAX_FACTS_PER_SUBJECT = 2
        subject_counts: dict[str, int] = {}
        deduped: list[dict] = []
        for f in merged:
            subj = f.get("subject", "")
            cnt = subject_counts.get(subj, 0)
            if cnt < MAX_FACTS_PER_SUBJECT:
                deduped.append(f)
                subject_counts[subj] = cnt + 1

        # Cap total to prevent prompt inflation
        context["facts"] = deduped[:facts_limit * 2]
        self.logger.log_memory("retrieve", "facts",
                               semantic=len(semantic_facts),
                               structured=len(structured_facts),
                               recent=len(recent_facts),
                               merged=len(merged),
                               after_dedup=len(context["facts"]))

        return context

    # --- Storage (PERSIST phase) ---

    async def store_episode(self, episode: Episode) -> bool:
        success = await self.episodic.store_episode(episode)
        self.logger.log_memory("store", "qdrant",
                               episode_type=episode.episode_type.value,
                               success=success)
        return success

    async def store_fact(self, fact: Fact, embedding: list[float] | None = None) -> bool:
        success = await self.structured.store_fact(fact)
        self.logger.log_memory("store", "postgres_fact",
                               subject=fact.subject,
                               success=success)
        # Also store in Qdrant semantic index if embedding is available
        if success and embedding:
            fact_text = f"{fact.subject} {fact.predicate} {fact.value}"
            await self.episodic.store_fact_embedding(
                fact_id=str(fact.id),
                text=fact_text,
                embedding=embedding,
                subject=fact.subject,
                predicate=fact.predicate,
                value=fact.value,
                confidence=fact.confidence,
            )
        return success

    async def save_goal(self, goal: Goal) -> bool:
        success = await self.structured.save_goal(goal)
        self.logger.log_memory("store", "postgres_goal",
                               goal_type=goal.goal_type.value,
                               success=success)
        return success

    async def get_active_goals(self) -> list[Goal]:
        return await self.structured.get_active_goals()
