"""Confidence calibration tracking — claimed confidence vs actual outcomes.

When hypotheses are CONFIRMED or REFUTED, and when events resolve or stall,
record the claimed confidence at creation time vs the actual outcome.
This builds the dataset needed to detect systematic over/under-confidence.

No LLM/SLM required — purely SQL-based analysis.

Existing schema columns used:
  - hypotheses: id, thesis, counter_thesis, evidence_balance, status, data (JSONB),
                created_at, updated_at
  - signals: id, confidence, created_at
  - events: id, data (JSONB -> lifecycle_status), signal_count, created_at
  - signal_event_links: signal_id, event_id
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger("legba.maintenance.calibration")

# Redis key for tracking which hypotheses have already been recorded
_REDIS_TRACKED_SET = "legba:calibration:tracked_hypotheses"

# Redis key for the calibration data store
_REDIS_CALIBRATION_DATA = "legba:calibration:data"


class CalibrationTracker:
    """Track claimed confidence vs actual outcomes for calibration.

    When hypotheses are CONFIRMED or REFUTED, and when situations are RESOLVED,
    record the claimed confidence at creation time vs the actual outcome.
    This builds the dataset needed to detect systematic over/under-confidence.
    """

    def __init__(self, pg_pool: asyncpg.Pool, metrics_client=None,
                 redis_client=None):
        self.pool = pg_pool
        self.metrics = metrics_client
        self.redis = redis_client

    async def run_all(self) -> dict[str, int | float]:
        """Run all calibration tasks. Returns summary stats."""
        results: dict[str, int | float] = {}

        tracked = await self.track_hypothesis_resolutions()
        results["hypotheses_tracked"] = tracked

        distribution = await self.analyze_confidence_distribution()
        results.update(distribution)

        await self.write_calibration_metrics()

        return results

    # ------------------------------------------------------------------
    # 1. Hypothesis resolution tracking
    # ------------------------------------------------------------------

    async def track_hypothesis_resolutions(self) -> int:
        """Find recently resolved hypotheses and record calibration data.

        Query hypotheses WHERE status IN ('confirmed', 'refuted')
        AND NOT already tracked (use a Redis set to avoid double-counting).

        For each: record evidence_balance at resolution time, the thesis,
        and whether confirmed or refuted.

        TODO: SLM could assess whether the resolution was correct by
        reviewing the evidence chain and current signals.
        """
        tracked_count = 0

        try:
            async with self.pool.acquire() as conn:
                # Get resolved hypotheses
                rows = await conn.fetch("""
                    SELECT id, thesis, counter_thesis, evidence_balance,
                           status, created_at, updated_at,
                           array_length(supporting_signals, 1) AS support_count,
                           array_length(refuting_signals, 1) AS refute_count
                    FROM hypotheses
                    WHERE status IN ('confirmed', 'refuted')
                    ORDER BY updated_at DESC
                    LIMIT 100
                """)

                for row in rows:
                    hypothesis_id = str(row["id"])

                    # Check if already tracked (via Redis if available, else JSONB flag)
                    already_tracked = False
                    if self.redis:
                        try:
                            already_tracked = await self.redis.sismember(
                                _REDIS_TRACKED_SET, hypothesis_id
                            )
                        except Exception:
                            already_tracked = False

                    if already_tracked:
                        continue

                    # Also check the JSONB flag as a fallback
                    calibration_flag = await conn.fetchval("""
                        SELECT data->'calibration_tracked'
                        FROM hypotheses WHERE id = $1
                    """, row["id"])
                    if calibration_flag and str(calibration_flag).strip('"') == "true":
                        continue

                    # Record calibration data
                    calibration_record = {
                        "hypothesis_id": hypothesis_id,
                        "thesis": row["thesis"],
                        "counter_thesis": row["counter_thesis"],
                        "status": row["status"],
                        "evidence_balance": row["evidence_balance"],
                        "support_count": row["support_count"] or 0,
                        "refute_count": row["refute_count"] or 0,
                        "created_at": row["created_at"].isoformat(),
                        "resolved_at": row["updated_at"].isoformat(),
                        "resolution_days": (
                            (row["updated_at"] - row["created_at"]).total_seconds() / 86400
                        ),
                    }

                    # Store to Redis for analysis
                    if self.redis:
                        try:
                            await self.redis.rpush(
                                _REDIS_CALIBRATION_DATA,
                                json.dumps(calibration_record),
                            )
                            await self.redis.sadd(_REDIS_TRACKED_SET, hypothesis_id)
                        except Exception as e:
                            logger.debug("Redis calibration write failed: %s", e)

                    # Mark as tracked in the hypothesis JSONB data
                    try:
                        await conn.execute("""
                            UPDATE hypotheses SET
                                data = COALESCE(data, '{}'::jsonb) ||
                                       '{"calibration_tracked": true}'::jsonb,
                                updated_at = NOW()
                            WHERE id = $1
                        """, row["id"])
                    except Exception as e:
                        logger.debug("Failed to mark hypothesis as tracked: %s", e)

                    tracked_count += 1
                    logger.info(
                        "Calibration: tracked hypothesis %s — %s (balance=%d, %s)",
                        hypothesis_id[:8],
                        row["status"],
                        row["evidence_balance"],
                        row["thesis"][:60],
                    )

        except Exception as e:
            logger.error("Hypothesis resolution tracking failed: %s", e)

        return tracked_count

    # ------------------------------------------------------------------
    # 2. Confidence distribution analysis
    # ------------------------------------------------------------------

    async def analyze_confidence_distribution(self) -> dict[str, float]:
        """Analyze whether confidence scores are calibrated.

        For signals that became part of confirmed events (signal_count > 5,
        lifecycle_status = 'active'), what was their average confidence at ingestion?

        For signals in events that stayed at 'emerging' and were never reinforced,
        what was their average confidence?

        If high-confidence signals end up in dead events as often as low-confidence
        ones, the confidence scoring is not discriminating.

        TODO: SLM could do deeper analysis of which confidence components
        (source reliability, temporal freshness, etc.) are most predictive.
        """
        results: dict[str, float] = {}

        try:
            async with self.pool.acquire() as conn:
                # Avg confidence of signals in ACTIVE events (signal_count > 5)
                active_conf = await conn.fetchval("""
                    SELECT AVG(s.confidence)
                    FROM signals s
                    JOIN signal_event_links sel ON sel.signal_id = s.id
                    JOIN events e ON e.id = sel.event_id
                    WHERE COALESCE(e.data->>'lifecycle_status', 'emerging') = 'active'
                      AND e.signal_count > 5
                """)
                results["avg_confidence_active_events"] = round(
                    float(active_conf), 4
                ) if active_conf is not None else 0.0

                # Avg confidence of signals in RESOLVED/dead events (signal_count < 3)
                dead_conf = await conn.fetchval("""
                    SELECT AVG(s.confidence)
                    FROM signals s
                    JOIN signal_event_links sel ON sel.signal_id = s.id
                    JOIN events e ON e.id = sel.event_id
                    WHERE COALESCE(e.data->>'lifecycle_status', 'emerging') = 'resolved'
                      AND e.signal_count < 3
                """)
                results["avg_confidence_dead_events"] = round(
                    float(dead_conf), 4
                ) if dead_conf is not None else 0.0

                # Avg confidence of signals in EMERGING events (still unresolved)
                emerging_conf = await conn.fetchval("""
                    SELECT AVG(s.confidence)
                    FROM signals s
                    JOIN signal_event_links sel ON sel.signal_id = s.id
                    JOIN events e ON e.id = sel.event_id
                    WHERE COALESCE(e.data->>'lifecycle_status', 'emerging') = 'emerging'
                """)
                results["avg_confidence_emerging_events"] = round(
                    float(emerging_conf), 4
                ) if emerging_conf is not None else 0.0

                # Discrimination score: difference between active and dead confidence
                # Higher is better — means confidence is predictive of event survival
                active = results["avg_confidence_active_events"]
                dead = results["avg_confidence_dead_events"]
                if active > 0 and dead > 0:
                    results["confidence_discrimination"] = round(active - dead, 4)
                else:
                    results["confidence_discrimination"] = 0.0

                # Hypothesis confidence at resolution
                # For confirmed: what was the avg evidence_balance? (should be positive)
                confirmed_balance = await conn.fetchval("""
                    SELECT AVG(evidence_balance) FROM hypotheses
                    WHERE status = 'confirmed'
                """)
                results["avg_balance_confirmed"] = round(
                    float(confirmed_balance), 4
                ) if confirmed_balance is not None else 0.0

                # For refuted: what was the avg evidence_balance? (should be negative)
                refuted_balance = await conn.fetchval("""
                    SELECT AVG(evidence_balance) FROM hypotheses
                    WHERE status = 'refuted'
                """)
                results["avg_balance_refuted"] = round(
                    float(refuted_balance), 4
                ) if refuted_balance is not None else 0.0

                # Count by status for context
                for status_val in ("confirmed", "refuted", "active"):
                    count = await conn.fetchval(
                        "SELECT COUNT(*) FROM hypotheses WHERE status = $1",
                        status_val,
                    )
                    results[f"hypothesis_count_{status_val}"] = float(count or 0)

        except Exception as e:
            logger.error("Confidence distribution analysis failed: %s", e)

        if results:
            logger.info(
                "Calibration analysis: active_conf=%.3f dead_conf=%.3f "
                "discrimination=%.3f confirmed_balance=%.1f refuted_balance=%.1f",
                results.get("avg_confidence_active_events", 0),
                results.get("avg_confidence_dead_events", 0),
                results.get("confidence_discrimination", 0),
                results.get("avg_balance_confirmed", 0),
                results.get("avg_balance_refuted", 0),
            )

        return results

    # ------------------------------------------------------------------
    # 3. Write calibration metrics to TimescaleDB
    # ------------------------------------------------------------------

    async def write_calibration_metrics(self) -> None:
        """Write calibration summary to TimescaleDB for Grafana.

        Metrics written:
        - calibration_high_conf_active: avg confidence of signals in ACTIVE events
        - calibration_high_conf_dead: avg confidence of signals in RESOLVED events (<3 signals)
        - calibration_discrimination: active - dead confidence spread
        - calibration_hypothesis_confirmed: count of confirmed hypotheses
        - calibration_hypothesis_refuted: count of refuted hypotheses
        - calibration_balance_confirmed: avg evidence_balance for confirmed
        - calibration_balance_refuted: avg evidence_balance for refuted
        """
        if not self.metrics or not self.metrics.available:
            return

        try:
            async with self.pool.acquire() as conn:
                # Signal confidence by event lifecycle
                active_conf = await conn.fetchval("""
                    SELECT AVG(s.confidence)
                    FROM signals s
                    JOIN signal_event_links sel ON sel.signal_id = s.id
                    JOIN events e ON e.id = sel.event_id
                    WHERE COALESCE(e.data->>'lifecycle_status', 'emerging') = 'active'
                      AND e.signal_count > 5
                """)

                dead_conf = await conn.fetchval("""
                    SELECT AVG(s.confidence)
                    FROM signals s
                    JOIN signal_event_links sel ON sel.signal_id = s.id
                    JOIN events e ON e.id = sel.event_id
                    WHERE COALESCE(e.data->>'lifecycle_status', 'emerging') = 'resolved'
                      AND e.signal_count < 3
                """)

                # Hypothesis counts and balances
                confirmed_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM hypotheses WHERE status = 'confirmed'"
                ) or 0
                refuted_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM hypotheses WHERE status = 'refuted'"
                ) or 0
                confirmed_balance = await conn.fetchval(
                    "SELECT AVG(evidence_balance) FROM hypotheses WHERE status = 'confirmed'"
                )
                refuted_balance = await conn.fetchval(
                    "SELECT AVG(evidence_balance) FROM hypotheses WHERE status = 'refuted'"
                )

            points: list[tuple[str, str, float]] = []

            if active_conf is not None:
                points.append(("calibration_high_conf_active", "all",
                               float(active_conf)))
            if dead_conf is not None:
                points.append(("calibration_high_conf_dead", "all",
                               float(dead_conf)))
            if active_conf is not None and dead_conf is not None:
                points.append(("calibration_discrimination", "all",
                               float(active_conf) - float(dead_conf)))

            points.append(("calibration_hypothesis_confirmed", "all",
                           float(confirmed_count)))
            points.append(("calibration_hypothesis_refuted", "all",
                           float(refuted_count)))

            if confirmed_balance is not None:
                points.append(("calibration_balance_confirmed", "all",
                               float(confirmed_balance)))
            if refuted_balance is not None:
                points.append(("calibration_balance_refuted", "all",
                               float(refuted_balance)))

            if points:
                await self.metrics.write_batch(points)
                logger.debug("Wrote %d calibration metrics to TimescaleDB",
                             len(points))

        except Exception as e:
            logger.error("Calibration metrics write failed: %s", e)
