"""
Memory tools: memory_store, memory_query, memory_promote, memory_supersede

These provide the LLM with explicit tools to store and query memories,
separate from the automatic memory consolidation in the REFLECT phase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.manager import MemoryManager
    from ...llm.client import LLMClient
    from ....shared.schemas.cycle import CycleState
    from ...log import CycleLogger
    from ..registry import ToolRegistry


def get_definitions() -> list[tuple[ToolDefinition, Any]]:
    return [
        (MEMORY_STORE_DEF, memory_store),
        (MEMORY_QUERY_DEF, memory_query),
        (STORE_FACT_DEF, store_fact),
    ]


def register(
    registry: ToolRegistry,
    *,
    memory: MemoryManager,
    llm: LLMClient,
    state: CycleState,
    logger: CycleLogger,
) -> None:
    """Register memory tools wired to the live memory manager."""

    async def memory_store_handler(args: dict) -> str:
        content = args.get("content", "")
        category = args.get("category", "observation")
        tags_str = args.get("tags", "")
        significance = float(args.get("significance", 0.5))
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]

        from ....shared.schemas.memory import Episode, EpisodeType
        type_map = {
            "fact": EpisodeType.OBSERVATION,
            "lesson": EpisodeType.LESSON,
            "observation": EpisodeType.OBSERVATION,
            "note": EpisodeType.REASONING,
        }
        ep = Episode(
            cycle_number=state.cycle_number,
            episode_type=type_map.get(category, EpisodeType.OBSERVATION),
            content=content,
            significance=significance,
            tags=tags,
        )
        try:
            ep.embedding = await llm.generate_embedding(content[:500])
        except Exception:
            pass

        success = await memory.store_episode(ep)

        # Also store as a fact if category is "fact"
        if category == "fact":
            from ....shared.schemas.memory import Fact
            fact = Fact(
                subject=tags[0] if tags else "general",
                predicate="noted",
                value=content[:500],
                confidence=significance,
                source_cycle=state.cycle_number,
            )
            fact_embedding = None
            try:
                fact_text = f"{fact.subject} {fact.predicate} {fact.value}"
                fact_embedding = await llm.generate_embedding(fact_text[:500])
            except Exception:
                pass
            await memory.store_fact(fact, embedding=fact_embedding)

        return f"Stored in memory (success={success}, tags={tags})"

    async def memory_query_handler(args: dict) -> str:
        query = args.get("query", "")
        limit = int(args.get("limit", 5))

        try:
            embedding = await llm.generate_embedding(query[:500])
            episodes = await memory.episodic.search_both(
                query_vector=embedding, limit=limit,
            )
        except Exception:
            episodes = []

        facts = await memory.structured.query_facts(subject=query)

        results = []
        for ep in episodes[:limit]:
            results.append(f"[episode id={ep.get('id', '')} score={ep.get('score', 0):.2f}] {ep.get('content', '')[:200]}")
        for f in facts[:3]:
            results.append(f"[fact] {f.subject} {f.predicate} {f.value}")

        if not results:
            return "No relevant memories found."
        return "\n".join(results)

    async def memory_promote_handler(args: dict) -> str:
        episode_id = args.get("episode_id", "")
        if not episode_id:
            return "Error: episode_id is required."

        try:
            client = memory.episodic._client
            if not client:
                return "Error: Qdrant not available."

            points = await client.retrieve(
                collection_name=memory.episodic.SHORT_TERM,
                ids=[episode_id],
                with_vectors=True,
                with_payload=True,
            )
            if not points:
                return f"Error: episode {episode_id} not found in short-term memory."

            point = points[0]
            success = await memory.episodic.promote_to_long_term(
                episode_id=episode_id,
                vector=point.vector,
                payload=point.payload,
            )
            if success:
                reason = args.get("reason", "")
                logger.log("memory_promoted", episode_id=episode_id, reason=reason)
                return f"Episode {episode_id} promoted to long-term memory."
            return "Error: promotion failed."
        except Exception as e:
            return f"Error promoting episode: {e}"

    async def memory_supersede_handler(args: dict) -> str:
        from uuid import UUID as _UUID
        from ....shared.schemas.memory import Fact

        old_id_str = args.get("old_fact_id", "")
        if not old_id_str:
            return "Error: old_fact_id is required."

        try:
            old_id = _UUID(old_id_str)
        except ValueError:
            return f"Error: invalid old_fact_id '{old_id_str}'"

        new_subject = args.get("new_subject", "")
        new_predicate = args.get("new_predicate", "")
        new_value = args.get("new_value", "")
        if not all([new_subject, new_predicate, new_value]):
            return "Error: new_subject, new_predicate, and new_value are all required."

        confidence = float(args.get("confidence", 1.0))

        new_fact = Fact(
            subject=new_subject,
            predicate=new_predicate,
            value=new_value,
            confidence=confidence,
            source_cycle=state.cycle_number,
        )

        success = await memory.structured.supersede_fact(old_id, new_fact)
        if success:
            await memory.episodic.remove_fact_embedding(old_id_str)
            try:
                fact_text = f"{new_fact.subject} {new_fact.predicate} {new_fact.value}"
                emb = await llm.generate_embedding(fact_text[:500])
                await memory.episodic.store_fact_embedding(
                    fact_id=str(new_fact.id), text=fact_text, embedding=emb,
                    subject=new_fact.subject, predicate=new_fact.predicate,
                    value=new_fact.value, confidence=new_fact.confidence,
                )
            except Exception:
                pass
            logger.log("fact_superseded", old_id=old_id_str, new_id=str(new_fact.id))
            return f"Fact {old_id_str} superseded by {new_fact.id}."
        return "Error: supersede failed (old fact not found or DB error)."

    async def store_fact_handler(args: dict) -> str:
        """Store a structured fact triple (subject/predicate/value) with optional
        temporal bounds. Normalizes predicates, auto-supersedes volatile facts,
        and stores an embedding for semantic retrieval."""
        from datetime import datetime as _dt, timezone as _tz
        from ....shared.schemas.memory import Fact

        subject = (args.get("subject") or "").strip()
        predicate = (args.get("predicate") or "").strip()
        value = (args.get("value") or "").strip()
        if not all([subject, predicate, value]):
            return "Error: subject, predicate, and value are all required."

        confidence = float(args.get("confidence", 0.7))
        evidence_id = (args.get("evidence_id") or "").strip()

        # Parse optional temporal bounds
        valid_from = None
        valid_until = None
        vf_str = (args.get("valid_from") or "").strip()
        vu_str = (args.get("valid_until") or "").strip()
        if vf_str:
            try:
                valid_from = _dt.fromisoformat(vf_str.replace("Z", "+00:00"))
            except ValueError:
                return f"Error: invalid valid_from format '{vf_str}'. Use ISO 8601 (e.g. '2025-01-20T00:00:00Z')."
        if vu_str:
            try:
                valid_until = _dt.fromisoformat(vu_str.replace("Z", "+00:00"))
            except ValueError:
                return f"Error: invalid valid_until format '{vu_str}'. Use ISO 8601 (e.g. '2025-01-20T00:00:00Z')."

        evidence = []
        if evidence_id:
            evidence = [{"id": evidence_id}]

        fact = Fact(
            subject=subject,
            predicate=predicate,
            value=value,
            confidence=confidence,
            source_cycle=state.cycle_number,
            valid_from=valid_from,
            valid_until=valid_until,
        )

        success = await memory.structured.store_fact(fact, evidence=evidence)
        if not success:
            return f"Error: Failed to store fact (predicate may not be canonical, check logs)."

        # Store embedding for semantic retrieval
        try:
            fact_text = f"{subject} {predicate} {value}"
            emb = await llm.generate_embedding(fact_text[:500])
            await memory.episodic.store_fact_embedding(
                fact_id=str(fact.id), text=fact_text, embedding=emb,
                subject=subject, predicate=predicate,
                value=value, confidence=confidence,
            )
        except Exception:
            pass  # Embedding is best-effort

        logger.log("fact_stored", fact_id=str(fact.id),
                    subject=subject, predicate=predicate, value=value[:60])

        result = {
            "status": "stored",
            "fact_id": str(fact.id),
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "confidence": confidence,
        }
        if valid_from:
            result["valid_from"] = valid_from.isoformat()
        if valid_until:
            result["valid_until"] = valid_until.isoformat()
        import json as _json
        return _json.dumps(result, indent=2)

    registry.register(MEMORY_STORE_DEF, memory_store_handler)
    registry.register(MEMORY_QUERY_DEF, memory_query_handler)
    registry.register(MEMORY_PROMOTE_DEF, memory_promote_handler)
    registry.register(MEMORY_SUPERSEDE_DEF, memory_supersede_handler)
    registry.register(STORE_FACT_DEF, store_fact_handler)


MEMORY_STORE_DEF = ToolDefinition(
    name="memory_store",
    description="Store a piece of information in memory for future retrieval",
    parameters=[
        ToolParameter(name="content", type="string", description="The information to store"),
        ToolParameter(name="category", type="string", description="Category: fact, lesson, observation, note", required=False),
        ToolParameter(name="tags", type="string", description="Comma-separated tags for retrieval", required=False),
        ToolParameter(name="significance", type="number", description="Importance 0.0-1.0 (default 0.5)", required=False),
    ],
)

MEMORY_QUERY_DEF = ToolDefinition(
    name="memory_query",
    description="Search memories by semantic similarity or structured query",
    parameters=[
        ToolParameter(name="query", type="string", description="Natural language search query"),
        ToolParameter(name="category", type="string", description="Filter by category", required=False),
        ToolParameter(name="limit", type="number", description="Maximum results (default 5)", required=False),
    ],
)


MEMORY_PROMOTE_DEF = ToolDefinition(
    name="memory_promote",
    description=(
        "Promote an episode from short-term to long-term memory. Use for significant "
        "memories that should persist beyond the short-term window. Requires the episode ID."
    ),
    parameters=[
        ToolParameter(name="episode_id", type="string",
                      description="UUID of the episode to promote"),
        ToolParameter(name="reason", type="string",
                      description="Why this memory is worth preserving long-term",
                      required=False),
    ],
)


MEMORY_SUPERSEDE_DEF = ToolDefinition(
    name="memory_supersede",
    description=(
        "Replace an outdated fact with a corrected or updated version. "
        "The old fact is marked as superseded and excluded from future queries."
    ),
    parameters=[
        ToolParameter(name="old_fact_id", type="string",
                      description="UUID of the fact to supersede"),
        ToolParameter(name="new_subject", type="string",
                      description="Subject of the replacement fact"),
        ToolParameter(name="new_predicate", type="string",
                      description="Predicate/relationship of the replacement fact"),
        ToolParameter(name="new_value", type="string",
                      description="Updated value"),
        ToolParameter(name="confidence", type="number",
                      description="Confidence 0.0-1.0 (default 1.0)",
                      required=False),
    ],
)

STORE_FACT_DEF = ToolDefinition(
    name="store_fact",
    description=(
        "Store a structured knowledge triple (subject/predicate/value) in the facts database. "
        "Facts persist across cycles and are used for context injection, hypothesis evaluation, "
        "and situation briefs. For time-bounded assertions (leadership, treaty status, GDP figures), "
        "set valid_from and valid_until to enforce temporal boundaries. Facts with the same "
        "subject+predicate but a different value auto-supersede the old fact for single-value "
        "predicates (Capital, Population, GDP, LeaderOf, etc.)."
    ),
    parameters=[
        ToolParameter(name="subject", type="string",
                      description="Entity the fact is about (e.g. 'Russia', 'Vladimir Putin')"),
        ToolParameter(name="predicate", type="string",
                      description="Relationship or property. Must be a canonical predicate: "
                                  "LeaderOf, AlliedWith, HostileTo, MemberOf, OperatesIn, "
                                  "LocatedIn, Capital, Population, GDP, etc."),
        ToolParameter(name="value", type="string",
                      description="The value or target of the relationship"),
        ToolParameter(name="confidence", type="number",
                      description="Confidence 0.0-1.0 (default 0.7). Calibration: "
                                  "0.3-0.4 single source, 0.5-0.6 multiple sources, "
                                  "0.7-0.8 strong corroboration, 0.9+ independently verified.",
                      required=False),
        ToolParameter(name="evidence_id", type="string",
                      description="UUID of signal or event that supports this fact (REQUIRED for verifiability)",
                      required=False),
        ToolParameter(name="valid_from", type="string",
                      description="ISO 8601 timestamp when this fact became true "
                                  "(e.g. '2025-01-20T00:00:00Z'). Defaults to now.",
                      required=False),
        ToolParameter(name="valid_until", type="string",
                      description="ISO 8601 timestamp when this fact stops being true "
                                  "(e.g. '2029-01-20T00:00:00Z'). NULL = open-ended. "
                                  "ALWAYS set this for leadership positions, treaty terms, "
                                  "or any assertion with a known end date.",
                      required=False),
    ],
)


# Stubs — only used if register() is not called (e.g. testing tool definitions in isolation)

async def memory_store(args: dict) -> str:
    return "Error: Memory store not initialized. This tool requires the memory manager to be connected."


async def memory_query(args: dict) -> str:
    return "Error: Memory query not initialized. This tool requires the memory manager to be connected."


async def memory_promote(args: dict) -> str:
    return "Error: Memory promote not initialized."


async def memory_supersede(args: dict) -> str:
    return "Error: Memory supersede not initialized."


async def store_fact(args: dict) -> str:
    return "Error: store_fact not initialized. This tool requires the memory manager to be connected."
