"""Classification refinement using the SLM.

JDL Level 4: Classification refinement.

Handles boundary cases where the ML classifier (DeBERTa) is uncertain
between top categories. The SLM provides semantic tiebreaking.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from .config import SubconsciousConfig
from .prompts import (
    CLASSIFICATION_REFINEMENT_PROMPT,
    CLASSIFICATION_REFINEMENT_SCHEMA,
    CLASSIFICATION_REFINEMENT_SYSTEM,
)
from .provider import BaseSLMProvider, SLMError
from .schemas import ClassificationVerdict

logger = logging.getLogger("legba.subconscious.classification")


async def fetch_boundary_signals(
    pool: asyncpg.Pool,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Fetch signals near the DeBERTa classification decision boundary.

    Returns signals where the category assignment may be uncertain.
    Since classification_scores are not yet stored on signals, this
    uses a heuristic: signals with category='other' that have been
    recently ingested are likely boundary cases.

    TODO: When a classification_scores JSONB column is added to the signals
    table, replace this query with one that finds signals where the top-2
    classification scores are within 0.1 of each other:

        SELECT s.id, s.title, s.category, s.classification_scores
        FROM signals s
        WHERE s.classification_scores IS NOT NULL
          AND (
            (s.classification_scores->>
              (SELECT key FROM jsonb_each_text(s.classification_scores)
               ORDER BY value::float DESC LIMIT 1 OFFSET 1))::float
            >
            (s.classification_scores->>
              (SELECT key FROM jsonb_each_text(s.classification_scores)
               ORDER BY value::float DESC LIMIT 1))::float - 0.1
          )
          AND s.created_at > NOW() - INTERVAL '24 hours'
        ORDER BY s.created_at DESC
        LIMIT $1
    """
    rows = await pool.fetch(
        """
        SELECT
            s.id::text AS signal_id,
            s.title,
            s.category,
            s.confidence,
            s.confidence_components,
            s.created_at::text AS created_at
        FROM signals s
        WHERE s.category = 'other'
          AND s.created_at > NOW() - INTERVAL '24 hours'
        ORDER BY s.created_at DESC
        LIMIT $1
        """,
        batch_size,
    )
    results = []
    for r in rows:
        d = dict(r)
        # confidence_components comes back as a string from JSONB; parse it
        cc = d.get("confidence_components")
        if cc and isinstance(cc, str):
            try:
                d["confidence_components"] = json.loads(cc)
            except (json.JSONDecodeError, TypeError):
                d["confidence_components"] = {}
        results.append(d)
    return results


async def refine_classifications(
    signals: list[dict[str, Any]],
    provider: BaseSLMProvider,
    config: SubconsciousConfig,
) -> list[ClassificationVerdict]:
    """Refine classification for boundary-case signals using the SLM.

    Args:
        signals: List of signal dicts from fetch_boundary_signals.
        provider: The SLM provider instance.
        config: Service config.

    Returns:
        List of ClassificationVerdict objects.
    """
    if not signals:
        return []

    verdicts: list[ClassificationVerdict] = []

    for signal in signals:
        # Use confidence_components if available, otherwise build synthetic scores
        scores: dict[str, float] = {}
        cc = signal.get("confidence_components")
        if cc and isinstance(cc, dict) and cc.get("classification"):
            scores = cc["classification"]
        else:
            scores = {signal["category"]: 0.45, "unknown_secondary": 0.40}

        prompt = CLASSIFICATION_REFINEMENT_PROMPT.format(
            signal_id=signal["signal_id"],
            text=signal["title"][:500],  # Truncate for SLM context
            scores_json=json.dumps(scores, indent=2),
            schema=json.dumps(CLASSIFICATION_REFINEMENT_SCHEMA, indent=2),
        )

        try:
            result = await provider.complete(
                prompt=prompt,
                system=CLASSIFICATION_REFINEMENT_SYSTEM,
                json_schema=CLASSIFICATION_REFINEMENT_SCHEMA,
            )
            verdict = ClassificationVerdict.model_validate(result)
            verdicts.append(verdict)
        except SLMError as exc:
            logger.warning(
                "SLM classification failed for signal %s: %s",
                signal["signal_id"], exc,
            )
        except Exception as exc:
            logger.warning(
                "Classification parse error for signal %s: %s",
                signal["signal_id"], exc,
            )

    logger.info(
        "Classification refinement: %d signals processed, %d verdicts",
        len(signals), len(verdicts),
    )
    return verdicts


async def apply_classification_verdicts(
    pool: asyncpg.Pool,
    verdicts: list[ClassificationVerdict],
) -> int:
    """Apply classification verdicts back to signals in Postgres.

    Updates the signal's category to the SLM's corrected classification.

    Returns the number of signals updated.
    """
    if not verdicts:
        return 0

    updated = 0
    async with pool.acquire() as conn:
        for verdict in verdicts:
            if not verdict.corrected_categories:
                continue
            try:
                new_category = verdict.corrected_categories[0]
                # Only update if the SLM suggests something other than 'other'
                if new_category and new_category != "other":
                    # Update category, data JSONB category field, and
                    # confidence_components with the SLM classification confidence
                    classification_cc = json.dumps({
                        "slm_category": new_category,
                        "slm_confidence": verdict.confidence,
                        "slm_all_categories": verdict.corrected_categories,
                    })
                    result = await conn.execute(
                        """
                        UPDATE signals
                        SET category = $1,
                            data = jsonb_set(
                                COALESCE(data, '{}'::jsonb),
                                '{category}',
                                to_jsonb($1::text)
                            ),
                            confidence_components = jsonb_set(
                                COALESCE(confidence_components, '{}'::jsonb),
                                '{classification}',
                                $3::jsonb
                            ),
                            updated_at = NOW()
                        WHERE id = $2::uuid
                          AND category = 'other'
                        """,
                        new_category,
                        verdict.signal_id,
                        classification_cc,
                    )
                    if result and "UPDATE 1" in result:
                        updated += 1
                        logger.info(
                            "Reclassified signal %s: other -> %s "
                            "(confidence=%.2f): %s",
                            verdict.signal_id, new_category,
                            verdict.confidence, verdict.reasoning,
                        )
            except Exception as exc:
                logger.warning(
                    "Failed to apply classification verdict for signal %s: %s",
                    verdict.signal_id, exc,
                )

    logger.info("Applied %d classification verdicts", updated)
    return updated
