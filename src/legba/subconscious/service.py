"""Legba Subconscious Service — main entry point.

Async service that runs alongside the conscious agent, using a side-channel
SLM (Llama 3.1 8B via vLLM) for continuous validation and enrichment tasks.

Three concurrent loops:
1. NATS consumer — triggered work items from other services
2. Timer loop — periodic tasks on modulo schedule
3. Differential accumulator — continuous state change tracking

Usage:
    python -m legba.subconscious
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import datetime, timezone

import asyncpg
import nats as nats_lib
import redis.asyncio as aioredis

from .classification import (
    apply_classification_verdicts,
    fetch_boundary_signals,
    refine_classifications,
)
from .config import SubconsciousConfig
from .differential import DifferentialAccumulator
from .entity_resolution import (
    apply_entity_verdicts,
    fetch_ambiguous_entities,
    resolve_entity_batch,
)
from .prompts import (
    FACT_REFRESH_PROMPT,
    FACT_REFRESH_SCHEMA,
    FACT_REFRESH_SYSTEM,
    GRAPH_CONSISTENCY_PROMPT,
    GRAPH_CONSISTENCY_SYSTEM,
    RELATIONSHIP_VALIDATION_PROMPT,
    RELATIONSHIP_VALIDATION_SCHEMA,
    RELATIONSHIP_VALIDATION_SYSTEM,
)
from .provider import BaseSLMProvider, SLMError, create_provider
from .schemas import FactRefreshVerdict, RelationshipVerdict
from .situation_detect import detect_situations
from .validation import (
    apply_signal_verdicts,
    fetch_uncertain_signals,
    validate_signal_batch,
)

logger = logging.getLogger("legba.subconscious")


class SubconsciousService:
    """Main subconscious service orchestrator.

    Runs three concurrent async loops:
    - NATS consumer for triggered work
    - Timer loop for periodic tasks
    - Differential accumulator for state tracking
    """

    def __init__(self, config: SubconsciousConfig):
        self.config = config
        self._running = False
        self._pg_pool: asyncpg.Pool | None = None
        self._redis: aioredis.Redis | None = None
        self._nats: nats_lib.NATS | None = None
        self._nats_sub = None
        self._provider: BaseSLMProvider | None = None
        self._differential: DifferentialAccumulator | None = None
        self._tick_count = 0
        self._start_time: datetime | None = None
        self._tasks_completed = 0

    async def start(self) -> None:
        """Initialize connections and start the three concurrent loops."""
        self._start_time = datetime.now(timezone.utc)
        self._running = True

        logger.info("Starting Legba Subconscious Service")
        logger.info(
            "Config: check_interval=%ds, provider=%s, model=%s",
            self.config.check_interval,
            self.config.llm_provider,
            self.config.llm_model,
        )

        # Connect to backing stores and init SLM provider
        await self._connect()

        logger.info("Subconscious service initialized, entering main loops")

        # Start health server in background
        asyncio.create_task(self._health_server())

        # Run three concurrent loops
        try:
            await asyncio.gather(
                self._nats_consumer(),
                self._timer_loop(),
                self._differential_accumulator(),
            )
        except asyncio.CancelledError:
            logger.info("Subconscious service cancelled")
        finally:
            await self._shutdown()

    async def _connect(self) -> None:
        """Establish connections to all backing stores and init SLM provider."""
        cfg = self.config

        # Postgres
        self._pg_pool = await asyncpg.create_pool(
            cfg.postgres.dsn,
            min_size=2,
            max_size=8,
        )
        logger.info("Connected to Postgres at %s", cfg.postgres.host)

        # Redis
        self._redis = aioredis.Redis(
            host=cfg.redis.host,
            port=cfg.redis.port,
            db=cfg.redis.db,
            password=cfg.redis.password,
            decode_responses=True,
        )
        await self._redis.ping()
        logger.info("Connected to Redis at %s", cfg.redis.host)

        # NATS
        try:
            self._nats = await nats_lib.connect(cfg.nats.url)
            logger.info("Connected to NATS at %s", cfg.nats.url)
        except Exception as e:
            logger.warning("NATS not available, triggered tasks disabled: %s", e)
            self._nats = None

        # SLM provider
        self._provider = create_provider(cfg)
        logger.info(
            "SLM provider initialized: %s (%s)",
            cfg.llm_provider, cfg.llm_model,
        )

        # Differential accumulator
        self._differential = DifferentialAccumulator(self._pg_pool, self._redis)
        await self._differential.initialize()

    # ------------------------------------------------------------------
    # Loop 1: NATS consumer — triggered work items
    # ------------------------------------------------------------------

    async def _nats_consumer(self) -> None:
        """Consume triggered work items from NATS subjects."""
        if not self._nats:
            logger.info("NATS not available, consumer loop idle")
            # Stay alive but idle
            while self._running:
                await asyncio.sleep(10)
            return

        subjects = [
            "legba.subconscious.signals",
            "legba.subconscious.entities",
            "legba.subconscious.relationships",
            "legba.subconscious.verdicts",
            "legba.subconscious.briefing",
        ]

        subscriptions = []
        for subject in subjects:
            try:
                sub = await self._nats.subscribe(subject)
                subscriptions.append(sub)
                logger.info("Subscribed to NATS subject: %s", subject)
            except Exception as e:
                logger.warning("Failed to subscribe to %s: %s", subject, e)

        try:
            while self._running:
                for sub in subscriptions:
                    try:
                        msg = await asyncio.wait_for(
                            sub.next_msg(), timeout=1.0,
                        )
                        await self._handle_nats_message(msg.subject, msg.data)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        logger.warning("NATS message handling error: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            for sub in subscriptions:
                try:
                    await sub.unsubscribe()
                except Exception:
                    pass

    async def _handle_nats_message(self, subject: str, data: bytes) -> None:
        """Route a NATS message to the appropriate handler."""
        try:
            payload = json.loads(data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Invalid NATS message on %s: %s", subject, e)
            return

        logger.debug("NATS message on %s: %s", subject, payload)

        if subject == "legba.subconscious.signals":
            await self._handle_signal_validation_trigger(payload)
        elif subject == "legba.subconscious.entities":
            await self._handle_entity_resolution_trigger(payload)
        elif subject == "legba.subconscious.relationships":
            await self._handle_relationship_validation_trigger(payload)
        elif subject == "legba.subconscious.verdicts":
            # Verdict acknowledgment from conscious agent
            logger.info("Verdict acknowledged: %s", payload.get("type", "unknown"))
        elif subject == "legba.subconscious.briefing":
            # Briefing request — trigger differential accumulation
            await self._differential.accumulate()
        else:
            logger.debug("Unhandled NATS subject: %s", subject)

        self._tasks_completed += 1

    async def _handle_signal_validation_trigger(self, payload: dict) -> None:
        """Handle triggered signal validation (e.g., from ingestion batch)."""
        signal_ids = payload.get("signal_ids", [])
        if not signal_ids:
            return

        logger.info("Triggered signal validation for %d signals", len(signal_ids))

        # Fetch the specific signals by ID
        rows = await self._pg_pool.fetch(
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
            WHERE s.id = ANY($1::uuid[])
            """,
            signal_ids,
        )
        signals = [dict(r) for r in rows]

        if signals:
            verdicts = await validate_signal_batch(signals, self._provider, self.config)
            await apply_signal_verdicts(
                self._pg_pool, verdicts, self.config.uncertainty_low,
            )

    async def _handle_entity_resolution_trigger(self, payload: dict) -> None:
        """Handle triggered entity resolution."""
        entity_ids = payload.get("entity_ids", [])
        if not entity_ids:
            return

        logger.info("Triggered entity resolution for %d entities", len(entity_ids))
        # Fetch entities and resolve
        entities = await fetch_ambiguous_entities(
            self._pg_pool, len(entity_ids),
        )
        if entities:
            verdicts = await resolve_entity_batch(
                entities, self._pg_pool, self._provider, self.config,
            )
            await apply_entity_verdicts(self._pg_pool, verdicts)

    async def _handle_relationship_validation_trigger(self, payload: dict) -> None:
        """Handle triggered relationship validation."""
        triples = payload.get("triples", [])
        source_text = payload.get("source_text", "")
        if not triples:
            return

        logger.info("Triggered relationship validation for %d triples", len(triples))

        prompt = RELATIONSHIP_VALIDATION_PROMPT.format(
            source_text=source_text[:1000],
            triples_json=json.dumps(triples, indent=2),
            schema=json.dumps(RELATIONSHIP_VALIDATION_SCHEMA, indent=2),
        )

        try:
            result = await self._provider.complete(
                prompt=prompt,
                system=RELATIONSHIP_VALIDATION_SYSTEM,
                json_schema=RELATIONSHIP_VALIDATION_SCHEMA,
            )
            # Parse as list of RelationshipVerdict
            if isinstance(result, list):
                verdicts = [RelationshipVerdict.model_validate(v) for v in result]
            elif isinstance(result, dict) and "verdicts" in result:
                verdicts = [RelationshipVerdict.model_validate(v) for v in result["verdicts"]]
            else:
                verdicts = [RelationshipVerdict.model_validate(result)]

            valid_count = sum(1 for v in verdicts if v.valid)
            logger.info(
                "Relationship validation: %d/%d triples valid",
                valid_count, len(verdicts),
            )

            await self._apply_relationship_verdicts(verdicts, triples, source_text)

        except (SLMError, Exception) as exc:
            logger.warning("Relationship validation failed: %s", exc)

    async def _apply_relationship_verdicts(
        self,
        verdicts: list[RelationshipVerdict],
        triples: list[dict],
        source_text: str,
    ) -> None:
        """Persist relationship validation verdicts.

        - Valid verdicts: insert into proposed_edges with status='approved'.
        - Reclassified verdicts (corrected_type set): insert with the corrected type.
        - Invalid verdicts: log the rejection.
        """
        if not verdicts or not self._pg_pool:
            return

        stored = 0
        async with self._pg_pool.acquire() as conn:
            for verdict in verdicts:
                idx = verdict.triple_index
                if idx < 0 or idx >= len(triples):
                    logger.warning(
                        "Relationship verdict triple_index %d out of range", idx,
                    )
                    continue

                triple = triples[idx]
                subject = triple.get("subject", "")
                obj = triple.get("object", "")
                rel_type = triple.get("predicate", triple.get("relationship_type", ""))

                if not subject or not obj:
                    continue

                if not verdict.valid and not verdict.corrected_type:
                    # Invalid and no reclassification — just log
                    logger.info(
                        "Relationship rejected: %s -[%s]-> %s — %s",
                        subject, rel_type, obj, verdict.reasoning,
                    )
                    continue

                # Use corrected type if the SLM reclassified the relationship
                effective_type = verdict.corrected_type or rel_type
                if verdict.corrected_type:
                    logger.info(
                        "Relationship reclassified: %s -[%s -> %s]-> %s — %s",
                        subject, rel_type, effective_type, obj,
                        verdict.reasoning,
                    )

                # Reification heuristic: if this triple involves entities
                # with an existing HostileTo relationship and the new edge
                # is SuppliesWeaponsTo or FundedBy, flag for reification
                # as a potential proxy/hostile supply Nexus.
                if effective_type in ("SuppliesWeaponsTo", "FundedBy"):
                    try:
                        hostile_exists = await conn.fetchval(
                            """
                            SELECT EXISTS (
                                SELECT 1 FROM proposed_edges
                                WHERE relationship_type = 'HostileTo'
                                  AND status IN ('pending', 'approved')
                                  AND (
                                      (LOWER(source_entity) = LOWER($1) AND LOWER(target_entity) = LOWER($2))
                                      OR (LOWER(source_entity) = LOWER($2) AND LOWER(target_entity) = LOWER($1))
                                  )
                            )
                            """,
                            subject, obj,
                        )
                        if not hostile_exists:
                            # Also check the AGE graph for committed HostileTo edges
                            try:
                                hostile_exists = await conn.fetchval(
                                    """
                                    SELECT EXISTS (
                                        SELECT 1 FROM nexuses
                                        WHERE nexus_type = 'HostileTo'
                                          AND (
                                              (LOWER(actor_entity) = LOWER($1) AND LOWER(target_entity) = LOWER($2))
                                              OR (LOWER(actor_entity) = LOWER($2) AND LOWER(target_entity) = LOWER($1))
                                          )
                                    )
                                    """,
                                    subject, obj,
                                )
                            except Exception:
                                pass

                        if hostile_exists:
                            logger.warning(
                                "REIFICATION RECOMMENDED: %s -[%s]-> %s — "
                                "entities have HostileTo relationship, "
                                "potentially needs reification as a proxy/hostile supply Nexus",
                                subject, effective_type, obj,
                            )
                    except Exception as exc:
                        logger.debug(
                            "Reification heuristic check failed for %s -[%s]-> %s: %s",
                            subject, effective_type, obj, exc,
                        )

                try:
                    # Check for duplicate before inserting
                    existing = await conn.fetchval(
                        "SELECT id FROM proposed_edges "
                        "WHERE LOWER(source_entity) = LOWER($1) "
                        "AND LOWER(target_entity) = LOWER($2) "
                        "AND relationship_type = $3 "
                        "AND status IN ('pending', 'approved')",
                        subject, obj, effective_type,
                    )
                    if existing:
                        logger.debug(
                            "Relationship already in proposed_edges: %s -[%s]-> %s",
                            subject, effective_type, obj,
                        )
                        continue

                    evidence = source_text[:500] if source_text else ""
                    await conn.execute(
                        "INSERT INTO proposed_edges "
                        "(source_entity, target_entity, relationship_type, "
                        "confidence, evidence_text, source_cycle, status) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        subject,
                        obj,
                        effective_type,
                        0.7,  # Default confidence for subconscious-validated edges
                        evidence,
                        0,    # source_cycle 0 = subconscious origin
                        "pending",
                    )
                    stored += 1
                    logger.info(
                        "Relationship stored as proposed_edge: %s -[%s]-> %s",
                        subject, effective_type, obj,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to store relationship %s -[%s]-> %s: %s",
                        subject, effective_type, obj, exc,
                    )

        if stored:
            logger.info(
                "Relationship verdicts: %d stored as proposed_edges", stored,
            )

    # ------------------------------------------------------------------
    # Loop 2: Timer loop — periodic tasks on modulo schedule
    # ------------------------------------------------------------------

    async def _timer_loop(self) -> None:
        """Run periodic tasks based on tick modulo intervals."""
        while self._running:
            self._tick_count += 1
            tick = self._tick_count

            try:
                # Signal validation — most frequent
                if tick % self.config.signal_validation_interval == 0:
                    await self._periodic_signal_validation()

                # Entity resolution
                if tick % self.config.entity_resolution_interval == 0:
                    await self._periodic_entity_resolution()

                # Classification refinement
                if tick % self.config.classification_interval == 0:
                    await self._periodic_classification()

                # Fact refresh
                if tick % self.config.fact_refresh_interval == 0:
                    await self._periodic_fact_refresh()

                # Situation detection
                if tick % self.config.situation_detect_interval == 0:
                    await self._periodic_situation_detection()

                # Graph consistency check — daily
                if tick % self.config.graph_consistency_interval == 0:
                    await self._periodic_graph_consistency()

                # Source reliability recalc — daily
                if tick % self.config.source_reliability_interval == 0:
                    await self._periodic_source_reliability()

            except Exception as exc:
                logger.error("Timer loop tick %d error: %s", tick, exc)

            await asyncio.sleep(self.config.check_interval)

    async def _periodic_signal_validation(self) -> None:
        """Periodic: validate uncertain signals."""
        logger.info("Periodic signal validation starting")
        signals = await fetch_uncertain_signals(
            self._pg_pool,
            self.config.uncertainty_low,
            self.config.uncertainty_high,
            self.config.signal_batch_size,
        )
        if signals:
            verdicts = await validate_signal_batch(signals, self._provider, self.config)
            await apply_signal_verdicts(
                self._pg_pool, verdicts, self.config.uncertainty_low,
            )
            self._tasks_completed += 1

    async def _periodic_entity_resolution(self) -> None:
        """Periodic: resolve ambiguous entities."""
        logger.info("Periodic entity resolution starting")
        entities = await fetch_ambiguous_entities(
            self._pg_pool, self.config.entity_batch_size,
        )
        if entities:
            verdicts = await resolve_entity_batch(
                entities, self._pg_pool, self._provider, self.config,
            )
            await apply_entity_verdicts(self._pg_pool, verdicts)
            self._tasks_completed += 1

    async def _periodic_classification(self) -> None:
        """Periodic: refine boundary classifications."""
        logger.info("Periodic classification refinement starting")
        signals = await fetch_boundary_signals(
            self._pg_pool, self.config.classification_batch_size,
        )
        if signals:
            verdicts = await refine_classifications(signals, self._provider, self.config)
            await apply_classification_verdicts(self._pg_pool, verdicts)
            self._tasks_completed += 1

    async def _periodic_fact_refresh(self) -> None:
        """Periodic: check fact corroboration against recent signals."""
        logger.info("Periodic fact refresh starting")

        try:
            # Fetch stale facts that haven't been checked recently
            rows = await self._pg_pool.fetch(
                """
                SELECT
                    f.id::text AS fact_id,
                    f.subject,
                    f.predicate,
                    f.value,
                    f.confidence,
                    f.updated_at::text AS updated_at
                FROM facts f
                WHERE f.superseded_by IS NULL
                  AND (f.updated_at < NOW() - INTERVAL '24 hours'
                       OR f.updated_at IS NULL)
                ORDER BY f.confidence ASC, f.updated_at ASC
                LIMIT 10
                """,
            )
            facts = [dict(r) for r in rows]

            for fact in facts:
                # Find recent signals related to this fact's subject
                signal_rows = await self._pg_pool.fetch(
                    """
                    SELECT
                        s.id::text AS signal_id,
                        s.title,
                        s.confidence,
                        s.created_at::text AS created_at
                    FROM signals s
                    WHERE s.title ILIKE $1
                      AND s.created_at > NOW() - INTERVAL '7 days'
                    ORDER BY s.created_at DESC
                    LIMIT 5
                    """,
                    f"%{fact['subject'][:50]}%",
                )
                related_signals = [dict(r) for r in signal_rows]

                if not related_signals:
                    continue  # No recent signals to compare

                prompt = FACT_REFRESH_PROMPT.format(
                    fact_id=fact["fact_id"],
                    subject=fact["subject"],
                    predicate=fact["predicate"],
                    value=fact["value"],
                    confidence=fact["confidence"],
                    updated_at=fact["updated_at"],
                    signals_json=json.dumps(related_signals, indent=2),
                    schema=json.dumps(FACT_REFRESH_SCHEMA, indent=2),
                )

                try:
                    result = await self._provider.complete(
                        prompt=prompt,
                        system=FACT_REFRESH_SYSTEM,
                        json_schema=FACT_REFRESH_SCHEMA,
                    )
                    verdict = FactRefreshVerdict.model_validate(result)

                    if verdict.status == "contradicted":
                        # Lower confidence significantly
                        await self._pg_pool.execute(
                            """
                            UPDATE facts
                            SET confidence = GREATEST(confidence - 0.3, 0.0),
                                updated_at = NOW()
                            WHERE id = $1::uuid
                            """,
                            verdict.fact_id,
                        )
                        logger.info(
                            "Fact %s contradicted: %s",
                            verdict.fact_id, verdict.reasoning,
                        )
                    elif verdict.status == "corroborated":
                        # Boost confidence slightly
                        await self._pg_pool.execute(
                            """
                            UPDATE facts
                            SET confidence = LEAST(confidence + 0.1, 1.0),
                                updated_at = NOW()
                            WHERE id = $1::uuid
                            """,
                            verdict.fact_id,
                        )
                        logger.debug(
                            "Fact %s corroborated: %s",
                            verdict.fact_id, verdict.reasoning,
                        )
                    else:
                        # Stale — just touch updated_at to avoid re-checking
                        await self._pg_pool.execute(
                            "UPDATE facts SET updated_at = NOW() WHERE id = $1::uuid",
                            verdict.fact_id,
                        )

                except (SLMError, Exception) as exc:
                    logger.warning("Fact refresh failed for %s: %s", fact["fact_id"], exc)

            self._tasks_completed += 1

        except Exception as exc:
            logger.warning("Periodic fact refresh failed: %s", exc)

    async def _periodic_situation_detection(self) -> None:
        """Periodic: SLM-based situation detection from event clusters."""
        logger.info("Periodic situation detection starting")
        try:
            proposed = await detect_situations(
                self._pg_pool, self._provider, self.config,
            )
            if proposed:
                logger.info(
                    "Situation detection: %d proposals created", proposed,
                )
            self._tasks_completed += 1
        except Exception as exc:
            logger.warning("Periodic situation detection failed: %s", exc)

    async def _periodic_graph_consistency(self) -> None:
        """Periodic: check graph consistency constraints."""
        logger.info("Periodic graph consistency check starting")

        try:
            # Query for graph anomalies
            anomalies = []

            # Check for orphan entities (no links in 30+ days)
            orphan_rows = await self._pg_pool.fetch(
                """
                SELECT
                    ep.id::text AS entity_id,
                    ep.canonical_name,
                    ep.entity_type,
                    ep.updated_at::text AS updated_at
                FROM entity_profiles ep
                WHERE NOT EXISTS (
                    SELECT 1 FROM signal_entity_links sel
                    WHERE sel.entity_id = ep.id
                      AND sel.created_at > NOW() - INTERVAL '30 days'
                )
                AND ep.created_at < NOW() - INTERVAL '7 days'
                LIMIT 20
                """,
            )
            for row in orphan_rows:
                anomalies.append({
                    "type": "orphan_entity",
                    "entity_id": row["entity_id"],
                    "canonical_name": row["canonical_name"],
                    "entity_type": row["entity_type"],
                    "last_updated": row["updated_at"],
                })

            # Check for stale entities (not verified in 14+ days)
            stale_rows = await self._pg_pool.fetch(
                """
                SELECT
                    ep.id::text AS entity_id,
                    ep.canonical_name,
                    ep.entity_type,
                    ep.last_verified_at::text AS last_verified_at
                FROM entity_profiles ep
                WHERE ep.last_verified_at IS NOT NULL
                  AND ep.last_verified_at < NOW() - INTERVAL '14 days'
                LIMIT 20
                """,
            )
            for row in stale_rows:
                anomalies.append({
                    "type": "stale_entity",
                    "entity_id": row["entity_id"],
                    "canonical_name": row["canonical_name"],
                    "entity_type": row["entity_type"],
                    "last_verified_at": row["last_verified_at"],
                })

            if not anomalies:
                logger.info("Graph consistency: no anomalies found")
                return

            prompt = GRAPH_CONSISTENCY_PROMPT.format(
                anomalies_json=json.dumps(anomalies, indent=2),
            )

            try:
                result = await self._provider.complete(
                    prompt=prompt,
                    system=GRAPH_CONSISTENCY_SYSTEM,
                )
                # Result is a list of verdicts
                if isinstance(result, list):
                    action_count = sum(1 for v in result if v.get("needs_action"))
                    logger.info(
                        "Graph consistency: %d anomalies reviewed, %d need action",
                        len(result), action_count,
                    )
                    # TODO: Write actionable anomalies to a review queue
                    # for the conscious agent to handle

            except (SLMError, Exception) as exc:
                logger.warning("Graph consistency SLM check failed: %s", exc)

            self._tasks_completed += 1

        except Exception as exc:
            logger.warning("Periodic graph consistency failed: %s", exc)

    async def _periodic_source_reliability(self) -> None:
        """Periodic: recalculate source reliability scores."""
        logger.info("Periodic source reliability recalc starting")

        try:
            # Calculate reliability based on success/failure ratios and signal quality
            await self._pg_pool.execute(
                """
                UPDATE sources s
                SET source_quality_score = sub.quality_score,
                    updated_at = NOW()
                FROM (
                    SELECT
                        s2.id,
                        CASE
                            WHEN (s2.fetch_success_count + s2.fetch_failure_count) = 0 THEN 0.5
                            ELSE LEAST(1.0, GREATEST(0.0,
                                s2.fetch_success_count::float
                                / NULLIF(s2.fetch_success_count + s2.fetch_failure_count, 0)
                                * 0.6
                                + COALESCE(
                                    (SELECT AVG(sig.confidence)
                                     FROM signals sig
                                     WHERE sig.source_id = s2.id
                                       AND sig.created_at > NOW() - INTERVAL '7 days'),
                                    0.5
                                ) * 0.4
                            ))
                        END AS quality_score
                    FROM sources s2
                    WHERE s2.status = 'active'
                ) sub
                WHERE s.id = sub.id
                  AND s.status = 'active'
                """,
            )
            logger.info("Source reliability scores recalculated")
            self._tasks_completed += 1

        except Exception as exc:
            logger.warning("Source reliability recalc failed: %s", exc)

    # ------------------------------------------------------------------
    # Loop 3: Differential accumulator
    # ------------------------------------------------------------------

    async def _differential_accumulator(self) -> None:
        """Continuous state change tracking loop.

        Runs at a slower cadence than the timer loop. Accumulates changes
        between conscious agent cycles and writes to Redis.
        """
        # Accumulate every 5 minutes (5 ticks at 60s interval)
        accumulate_interval = 5

        tick = 0
        while self._running:
            tick += 1
            if tick % accumulate_interval == 0:
                try:
                    await self._differential.accumulate()
                except Exception as exc:
                    logger.warning("Differential accumulation failed: %s", exc)

            await asyncio.sleep(self.config.check_interval)

    # ------------------------------------------------------------------
    # Health + lifecycle
    # ------------------------------------------------------------------

    async def _health_server(self) -> None:
        """Simple HTTP health endpoint."""
        from asyncio import start_server

        async def handle(reader, writer):
            data = await reader.read(1024)
            request_line = data.decode().split("\n")[0] if data else ""

            if "/metrics" in request_line:
                body = json.dumps({
                    "ticks": self._tick_count,
                    "tasks_completed": self._tasks_completed,
                    "uptime_seconds": int(
                        (datetime.now(timezone.utc) - self._start_time).total_seconds()
                    ) if self._start_time else 0,
                    "provider": self.config.llm_provider,
                    "model": self.config.llm_model,
                })
            else:
                body = json.dumps({
                    "status": "ok",
                    "service": "subconscious",
                    "uptime_seconds": int(
                        (datetime.now(timezone.utc) - self._start_time).total_seconds()
                    ) if self._start_time else 0,
                    "ticks": self._tick_count,
                    "tasks_completed": self._tasks_completed,
                })

            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"\r\n{body}"
            )
            writer.write(response.encode())
            await writer.drain()
            writer.close()

        try:
            server = await start_server(handle, "0.0.0.0", self.config.health_port)
            logger.info("Health server listening on :%d", self.config.health_port)
            await server.serve_forever()
        except Exception as e:
            logger.warning("Health server failed: %s", e)

    async def _shutdown(self) -> None:
        """Clean shutdown of connections."""
        logger.info("Shutting down subconscious service")
        self._running = False

        if self._provider:
            try:
                await self._provider.close()
            except Exception:
                pass

        if self._nats:
            try:
                await self._nats.close()
            except Exception:
                pass

        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass

        if self._pg_pool:
            try:
                await self._pg_pool.close()
            except Exception:
                pass

        logger.info(
            "Subconscious service stopped. Tasks completed: %d",
            self._tasks_completed,
        )

    def stop(self) -> None:
        """Signal the service to stop."""
        self._running = False


def main() -> None:
    """Entry point for `python -m legba.subconscious`."""
    config = SubconsciousConfig.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    service = SubconsciousService(config)

    # Graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.new_event_loop()

    def _signal_handler():
        logger.info("Received shutdown signal")
        service.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        loop.run_until_complete(service.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
