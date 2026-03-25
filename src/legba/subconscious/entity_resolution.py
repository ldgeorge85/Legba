"""Entity resolution using the SLM.

JDL Level 1: SLM entity disambiguation.

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
                (data->>'description')::text AS description,
                similarity(canonical_name, $2) AS trgm_similarity
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
                (data->>'description')::text AS description,
                0.0::real AS trgm_similarity
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

        # Build a lookup of candidate trigram similarities for cross-validation
        candidate_trgm: dict[str, float] = {}
        for c in candidates:
            candidate_trgm[c["entity_id"]] = c.get("trgm_similarity", 0.0)

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

            # Cross-validation: if SLM matched to an entity, check trigram
            # similarity between extracted name and candidate canonical_name.
            # Catches hallucinated matches where the SLM claims a match but
            # the names are completely dissimilar.
            if (
                verdict.matched_entity_id
                and not verdict.is_new_entity
                and verdict.confidence > 0.8
            ):
                trgm_sim = candidate_trgm.get(verdict.matched_entity_id, 0.0)
                if trgm_sim < 0.3:
                    # Find the canonical name for the log message
                    matched_name = verdict.matched_entity_id
                    for c in candidates:
                        if c["entity_id"] == verdict.matched_entity_id:
                            matched_name = c["canonical_name"]
                            break
                    logger.warning(
                        "Cross-validation downgrade: SLM matched '%s' -> '%s' "
                        "with confidence=%.2f but trigram similarity=%.3f "
                        "(below 0.3 threshold). Downgrading confidence to 0.5.",
                        entity_name, matched_name,
                        verdict.confidence, trgm_sim,
                    )
                    verdict.confidence = 0.5

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

    For high-confidence matches: update signal_entity_links confidence.
    For high-confidence new entities: create entity_profile.
    Below threshold: log the recommendation only.

    Returns the number of entities processed.
    """
    if not verdicts:
        return 0

    processed = 0
    async with pool.acquire() as conn:
        for verdict in verdicts:
            try:
                if verdict.is_new_entity:
                    if verdict.confidence > 0.85:
                        # Create a new entity_profile with the extracted name
                        import uuid as _uuid
                        new_id = _uuid.uuid4()
                        entity_type = "other"  # Default; will be refined later
                        data = json.dumps({
                            "description": "",
                            "source": "subconscious_entity_resolution",
                            "reasoning": verdict.reasoning,
                        })
                        await conn.execute(
                            """
                            INSERT INTO entity_profiles
                                (id, canonical_name, entity_type, data,
                                 completeness_score, created_at, updated_at)
                            VALUES ($1, $2, $3, $4::jsonb, 0.0, NOW(), NOW())
                            ON CONFLICT DO NOTHING
                            """,
                            new_id,
                            verdict.entity_name,
                            entity_type,
                            data,
                        )
                        logger.info(
                            "Entity resolution: '%s' -> CREATED new entity %s "
                            "(confidence=%.2f): %s",
                            verdict.entity_name, str(new_id),
                            verdict.confidence, verdict.reasoning,
                        )
                    else:
                        logger.info(
                            "Entity resolution: '%s' -> new entity recommended "
                            "but confidence=%.2f below 0.85 threshold: %s",
                            verdict.entity_name, verdict.confidence,
                            verdict.reasoning,
                        )
                elif verdict.matched_entity_id:
                    if verdict.confidence > 0.85:
                        # Update signal_entity_links confidence for this entity
                        result = await conn.execute(
                            """
                            UPDATE signal_entity_links
                            SET confidence = $1
                            WHERE entity_id = $2::uuid
                              AND confidence < $1
                            """,
                            verdict.confidence,
                            verdict.matched_entity_id,
                        )
                        # Parse row count from "UPDATE N"
                        rows_updated = 0
                        if result:
                            try:
                                rows_updated = int(result.split()[-1])
                            except (ValueError, IndexError):
                                pass
                        logger.info(
                            "Entity resolution: '%s' -> matched %s "
                            "(confidence=%.2f, %d links updated): %s",
                            verdict.entity_name, verdict.matched_entity_id,
                            verdict.confidence, rows_updated,
                            verdict.reasoning,
                        )
                    else:
                        logger.info(
                            "Entity resolution: '%s' -> matched %s but "
                            "confidence=%.2f below 0.85 threshold: %s",
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
