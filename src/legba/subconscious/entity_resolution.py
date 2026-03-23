"""Entity resolution using the SLM.

Resolves ambiguous entity extractions by querying the SLM to match
extracted entity names against existing entity profiles.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from .config import SubconsciousConfig
from .prompts import (
    ENTITY_RESOLUTION_PROMPT,
    ENTITY_RESOLUTION_SCHEMA,
    ENTITY_RESOLUTION_SYSTEM,
)
from .provider import BaseSLMProvider, SLMError
from .schemas import EntityResolutionVerdict

logger = logging.getLogger("legba.subconscious.entity_resolution")


async def fetch_ambiguous_entities(
    pool: asyncpg.Pool,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Fetch recently extracted entities that lack a confident link.

    Finds entity mentions from signal_entity_links where confidence is
    below the threshold, indicating ambiguity in the extraction.
    """
    rows = await pool.fetch(
        """
        SELECT
            sel.entity_id::text AS entity_id,
            ep.canonical_name AS entity_name,
            ep.entity_type,
            sel.confidence AS link_confidence,
            s.title AS signal_title,
            s.id::text AS signal_id
        FROM signal_entity_links sel
        JOIN entity_profiles ep ON sel.entity_id = ep.id
        JOIN signals s ON sel.signal_id = s.id
        WHERE sel.confidence < 0.6
          AND sel.created_at > NOW() - INTERVAL '24 hours'
        ORDER BY sel.confidence ASC
        LIMIT $1
        """,
        batch_size,
    )
    return [dict(r) for r in rows]


async def fetch_entity_candidates(
    pool: asyncpg.Pool,
    entity_name: str,
    entity_type: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fetch potential matching entity profiles for resolution.

    Uses trigram similarity on canonical_name (if pg_trgm is available)
    with a fallback to ILIKE pattern matching.
    """
    # Try trigram similarity first (requires pg_trgm extension)
    try:
        rows = await pool.fetch(
            """
            SELECT
                id::text AS entity_id,
                canonical_name,
                entity_type,
                completeness_score,
                (data->>'description')::text AS description
            FROM entity_profiles
            WHERE entity_type = $1 OR $1 = 'other'
            ORDER BY similarity(canonical_name, $2) DESC
            LIMIT $3
            """,
            entity_type,
            entity_name,
            limit,
        )
        return [dict(r) for r in rows]
    except Exception:
        # Fallback to ILIKE if pg_trgm not available
        pattern = f"%{entity_name.split()[0]}%" if entity_name else "%"
        rows = await pool.fetch(
            """
            SELECT
                id::text AS entity_id,
                canonical_name,
                entity_type,
                completeness_score,
                (data->>'description')::text AS description
            FROM entity_profiles
            WHERE (entity_type = $1 OR $1 = 'other')
              AND canonical_name ILIKE $2
            ORDER BY updated_at DESC
            LIMIT $3
            """,
            entity_type,
            pattern,
            limit,
        )
        return [dict(r) for r in rows]


async def resolve_entity_batch(
    entities: list[dict[str, Any]],
    pool: asyncpg.Pool,
    provider: BaseSLMProvider,
    config: SubconsciousConfig,
) -> list[EntityResolutionVerdict]:
    """Resolve a batch of ambiguous entities using the SLM.

    For each entity, fetches candidates from the DB, builds a prompt,
    and asks the SLM to pick the best match or mark as new.

    Args:
        entities: List of entity dicts from fetch_ambiguous_entities.
        pool: Postgres connection pool.
        provider: The SLM provider instance.
        config: Service config.

    Returns:
        List of EntityResolutionVerdict objects.
    """
    if not entities:
        return []

    verdicts: list[EntityResolutionVerdict] = []

    for entity in entities:
        entity_name = entity["entity_name"]
        entity_type = entity.get("entity_type", "other")
        context = entity.get("signal_title", "")

        # Fetch candidate matches from DB
        candidates = await fetch_entity_candidates(
            pool, entity_name, entity_type, limit=10,
        )

        candidates_json = json.dumps(
            [
                {
                    "entity_id": c["entity_id"],
                    "canonical_name": c["canonical_name"],
                    "entity_type": c["entity_type"],
                    "description": c.get("description", ""),
                }
                for c in candidates
            ],
            indent=2,
        )

        prompt = ENTITY_RESOLUTION_PROMPT.format(
            entity_name=entity_name,
            context=context,
            entity_type=entity_type,
            candidates_json=candidates_json,
            schema=json.dumps(ENTITY_RESOLUTION_SCHEMA, indent=2),
        )

        try:
            result = await provider.complete(
                prompt=prompt,
                system=ENTITY_RESOLUTION_SYSTEM,
                json_schema=ENTITY_RESOLUTION_SCHEMA,
            )
            verdict = EntityResolutionVerdict.model_validate(result)
            verdicts.append(verdict)
        except SLMError as exc:
            logger.warning(
                "SLM entity resolution failed for '%s': %s", entity_name, exc,
            )
        except Exception as exc:
            logger.warning(
                "Entity resolution parse error for '%s': %s", entity_name, exc,
            )

    logger.info(
        "Entity resolution: %d entities processed, %d verdicts",
        len(entities), len(verdicts),
    )
    return verdicts


async def apply_entity_verdicts(
    pool: asyncpg.Pool,
    verdicts: list[EntityResolutionVerdict],
) -> int:
    """Apply entity resolution verdicts.

    For matches: update signal_entity_links confidence.
    For new entities: flag for manual review.

    Returns the number of entities processed.
    """
    if not verdicts:
        return 0

    processed = 0
    async with pool.acquire() as conn:
        for verdict in verdicts:
            try:
                if verdict.is_new_entity:
                    # TODO: Create new entity_profile and update signal_entity_links
                    # to point to the new entity. This requires:
                    # 1. INSERT into entity_profiles
                    # 2. UPDATE signal_entity_links to replace the ambiguous link
                    #
                    # For now, log the recommendation for the conscious agent to handle.
                    logger.info(
                        "Entity resolution: '%s' -> NEW ENTITY (confidence=%.2f): %s",
                        verdict.entity_name, verdict.confidence, verdict.reasoning,
                    )
                elif verdict.matched_entity_id:
                    # Update link confidence for the matched entity
                    # TODO: If the matched_entity_id differs from the current entity_id
                    # in signal_entity_links, we need to update the FK reference.
                    # This requires knowing the original signal_id from the batch.
                    logger.info(
                        "Entity resolution: '%s' -> matched %s (confidence=%.2f): %s",
                        verdict.entity_name, verdict.matched_entity_id,
                        verdict.confidence, verdict.reasoning,
                    )
                processed += 1
            except Exception as exc:
                logger.warning(
                    "Failed to apply entity verdict for '%s': %s",
                    verdict.entity_name, exc,
                )

    logger.info("Applied %d entity resolution verdicts", processed)
    return processed
