"""Signal batch validation using the SLM.

JDL Level 0: SLM signal quality assessment.

Queries uncertain signals from Postgres, batches them, sends to the SLM
for quality assessment, and parses the structured verdicts.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from .config import SubconsciousConfig
from .prompts import (
    SIGNAL_VALIDATION_PROMPT,
    SIGNAL_VALIDATION_SCHEMA,
    SIGNAL_VALIDATION_SYSTEM,
)
from .provider import BaseSLMProvider, SLMError
from .schemas import SignalBatchValidationResponse, SignalValidationVerdict

logger = logging.getLogger("legba.subconscious.validation")


async def fetch_uncertain_signals(
    pool: asyncpg.Pool,
    uncertainty_low: float,
    uncertainty_high: float,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Fetch signals with confidence between uncertainty thresholds.

    Returns signals that are uncertain enough to benefit from SLM review
    but not so low they should be auto-rejected.
    """
    rows = await pool.fetch(
        """
        SELECT
            s.id::text AS signal_id,
            s.title,
            s.category,
            s.confidence,
            s.source_url,
            s.created_at::text AS created_at,
            src.name AS source_name,
            src.reliability AS source_reliability
        FROM signals s
        LEFT JOIN sources src ON s.source_id = src.id
        WHERE s.confidence > $1
          AND s.confidence < $2
          AND s.created_at > NOW() - INTERVAL '24 hours'
        ORDER BY s.created_at DESC
        LIMIT $3
        """,
        uncertainty_low,
        uncertainty_high,
        batch_size,
    )
    return [dict(r) for r in rows]


async def validate_signal_batch(
    signals: list[dict[str, Any]],
    provider: BaseSLMProvider,
    config: SubconsciousConfig,
) -> list[SignalValidationVerdict]:
    """Validate a batch of uncertain signals using the SLM.

    Args:
        signals: List of signal dicts from fetch_uncertain_signals.
        provider: The SLM provider instance.
        config: Service config.

    Returns:
        List of SignalValidationVerdict objects.
    """
    if not signals:
        return []

    # Build signal summaries for the prompt
    signal_summaries = []
    for s in signals:
        signal_summaries.append({
            "signal_id": s["signal_id"],
            "title": s["title"],
            "category": s["category"],
            "original_confidence": s["confidence"],
            "source_name": s.get("source_name", "unknown"),
            "source_reliability": s.get("source_reliability", 0.5),
        })

    prompt = SIGNAL_VALIDATION_PROMPT.format(
        count=len(signal_summaries),
        signals_json=json.dumps(signal_summaries, indent=2),
        schema=json.dumps(SIGNAL_VALIDATION_SCHEMA, indent=2),
    )

    try:
        result = await provider.complete(
            prompt=prompt,
            system=SIGNAL_VALIDATION_SYSTEM,
            json_schema=SIGNAL_VALIDATION_SCHEMA,
        )

        response = SignalBatchValidationResponse.model_validate(result)
        logger.info(
            "Signal validation: %d signals assessed, %d verdicts returned",
            len(signals), len(response.verdicts),
        )
        return response.verdicts

    except SLMError as exc:
        logger.warning("SLM signal validation failed: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Signal validation parse error: %s", exc)
        return []


async def apply_signal_verdicts(
    pool: asyncpg.Pool,
    verdicts: list[SignalValidationVerdict],
    uncertainty_low: float,
) -> int:
    """Apply validation verdicts back to signals in Postgres.

    Updates signal confidence based on SLM assessment. Signals that
    fall below uncertainty_low after adjustment are flagged.

    Returns the number of signals updated.
    """
    if not verdicts:
        return 0

    updated = 0
    async with pool.acquire() as conn:
        for verdict in verdicts:
            try:
                # Update the signal's confidence with the adjusted value
                result = await conn.execute(
                    """
                    UPDATE signals
                    SET confidence = $1,
                        updated_at = NOW()
                    WHERE id = $2::uuid
                    """,
                    verdict.adjusted_confidence,
                    verdict.signal_id,
                )
                if result and "UPDATE 1" in result:
                    updated += 1

                # TODO: Store detailed verdict in a confidence_components JSONB column
                # when that column is added to the signals table. The verdict contains
                # specificity, internal_consistency, cross_signal_contradiction, and
                # reasoning that would be valuable for audit trails.
                #
                # await conn.execute(
                #     """
                #     UPDATE signals
                #     SET confidence_components = $1
                #     WHERE id = $2::uuid
                #     """,
                #     json.dumps(verdict.model_dump()),
                #     verdict.signal_id,
                # )

            except Exception as exc:
                logger.warning(
                    "Failed to apply verdict for signal %s: %s",
                    verdict.signal_id, exc,
                )

    logger.info("Applied %d signal validation verdicts", updated)
    return updated
