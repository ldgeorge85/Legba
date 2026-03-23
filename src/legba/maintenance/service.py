"""Legba Maintenance Service — main entry point.

Deterministic background maintenance daemon. No LLM. Runs continuously,
scheduling housekeeping tasks on configurable tick intervals.

Usage:
    python -m legba.maintenance
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import traceback
from datetime import datetime, timezone

import asyncpg
import nats as nats_lib
import redis.asyncio as aioredis

from .config import MaintenanceConfig
from .lifecycle import LifecycleManager
from .entity_gc import EntityGarbageCollector
from .fact_decay import FactDecayManager
from .corroboration import CorroborationScorer
from .integrity import IntegrityVerifier
from .metrics import MetricCollector
from .situation_detect import SituationDetector
from .adversarial import AdversarialDetector
from .calibration import CalibrationTracker
from ..shared.metrics import MetricsClient

logger = logging.getLogger("legba.maintenance")


class MaintenanceService:
    """Main maintenance daemon orchestrator."""

    def __init__(self, config: MaintenanceConfig):
        self.config = config
        self._running = False
        self._pg_pool: asyncpg.Pool | None = None
        self._redis: aioredis.Redis | None = None
        self._nats: nats_lib.NATS | None = None
        self._os_client = None
        self._qdrant = None
        self._metrics: MetricsClient | None = None
        self._tick_count = 0
        self._start_time: datetime | None = None
        self._task_stats: dict[str, dict] = {}

    async def start(self) -> None:
        """Initialize connections and start the tick loop."""
        self._start_time = datetime.now(timezone.utc)
        self._running = True

        logger.info("Starting Legba Maintenance Daemon")
        logger.info(
            "Config: check_interval=%ds, health_port=%d",
            self.config.check_interval,
            self.config.health_port,
        )
        logger.info(
            "Task intervals (ticks): lifecycle=%d, entity_gc=%d, fact_decay=%d, "
            "corroboration=%d, metrics=%d, integrity=%d, situation_detect=%d, "
            "adversarial=%d, calibration=%d",
            self.config.lifecycle_decay_interval,
            self.config.entity_gc_interval,
            self.config.fact_decay_interval,
            self.config.corroboration_interval,
            self.config.metrics_interval,
            self.config.integrity_interval,
            self.config.situation_detect_interval,
            self.config.adversarial_detect_interval,
            self.config.calibration_track_interval,
        )

        # Connect to backing stores
        await self._connect()

        logger.info("Maintenance daemon initialized, entering main loop")

        # Start health server in background
        asyncio.create_task(self._health_server())

        # Main loop
        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(self.config.check_interval)
        except asyncio.CancelledError:
            logger.info("Maintenance daemon cancelled")
        finally:
            await self._shutdown()

    async def _connect(self) -> None:
        """Establish connections to all backing stores."""
        cfg = self.config

        # Postgres
        self._pg_pool = await asyncpg.create_pool(
            cfg.postgres.dsn,
            min_size=2,
            max_size=5,
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

        # OpenSearch
        try:
            from opensearchpy import AsyncOpenSearch
            os_kwargs = {
                "hosts": [{"host": cfg.opensearch.host, "port": cfg.opensearch.port}],
                "use_ssl": cfg.opensearch.scheme == "https",
                "verify_certs": False,
            }
            if cfg.opensearch.username:
                os_kwargs["http_auth"] = (cfg.opensearch.username, cfg.opensearch.password)
            self._os_client = AsyncOpenSearch(**os_kwargs)
            logger.info("Connected to OpenSearch at %s:%d", cfg.opensearch.host, cfg.opensearch.port)
        except Exception as e:
            logger.warning("OpenSearch not available: %s", e)

        # Qdrant
        try:
            from qdrant_client import AsyncQdrantClient
            self._qdrant = AsyncQdrantClient(host=cfg.qdrant.host, port=cfg.qdrant.port)
            await self._qdrant.get_collections()  # connection test
            logger.info("Connected to Qdrant at %s:%d", cfg.qdrant.host, cfg.qdrant.port)
        except Exception as e:
            logger.warning("Qdrant not available: %s", e)
            self._qdrant = None

        # NATS
        try:
            self._nats = await nats_lib.connect(cfg.nats.url)
            logger.info("Connected to NATS at %s", cfg.nats.url)
        except Exception as e:
            logger.warning("NATS not available, notifications disabled: %s", e)
            self._nats = None

        # Metrics (TimescaleDB, optional)
        self._metrics = MetricsClient()
        await self._metrics.connect()

    async def _tick(self) -> None:
        """One scheduler tick — run due maintenance tasks via modulo scheduling."""
        self._tick_count += 1
        tick = self._tick_count

        # Update heartbeat in Redis
        try:
            await self._redis.hset("legba:maintenance", mapping={
                "last_tick": tick,
                "last_tick_at": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": int(
                    (datetime.now(timezone.utc) - self._start_time).total_seconds()
                ) if self._start_time else 0,
            })
        except Exception as e:
            logger.debug("Heartbeat update failed: %s", e)

        # Lifecycle decay — every lifecycle_decay_interval ticks
        if tick % self.config.lifecycle_decay_interval == 0:
            await self._run_task("lifecycle_decay", self._lifecycle_decay)

        # Corroboration scoring — every corroboration_interval ticks
        if tick % self.config.corroboration_interval == 0:
            await self._run_task("corroboration_scoring", self._corroboration_scoring)

        # Metric collection — every metrics_interval ticks
        if tick % self.config.metrics_interval == 0:
            await self._run_task("metric_collection", self._metric_collection)

        # Entity GC — every entity_gc_interval ticks
        if tick % self.config.entity_gc_interval == 0:
            await self._run_task("entity_gc", self._entity_gc)

        # Fact decay — every fact_decay_interval ticks
        if tick % self.config.fact_decay_interval == 0:
            await self._run_task("fact_decay", self._fact_decay)

        # Integrity verification — every integrity_interval ticks
        if tick % self.config.integrity_interval == 0:
            await self._run_task("integrity_verification", self._integrity_verification)

        # Situation detection — every situation_detect_interval ticks
        if tick % self.config.situation_detect_interval == 0:
            await self._run_task("situation_detection", self._situation_detection)

        # Adversarial signal detection — every adversarial_detect_interval ticks
        if tick % self.config.adversarial_detect_interval == 15:
            await self._run_task("adversarial_detect", self._adversarial_detect)

        # Calibration tracking — every calibration_track_interval ticks
        if tick % self.config.calibration_track_interval == 45:
            await self._run_task("calibration_track", self._calibration_track)

        # Periodic status log
        if tick % 10 == 0:
            logger.info(
                "Maintenance tick %d — tasks run: %s",
                tick,
                ", ".join(
                    f"{name}({s.get('runs', 0)})"
                    for name, s in self._task_stats.items()
                ) or "none yet",
            )

    async def _run_task(self, name: str, coro_fn) -> None:
        """Execute a maintenance task with error handling and stats tracking."""
        started = datetime.now(timezone.utc)
        try:
            await coro_fn()
            elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            stats = self._task_stats.setdefault(name, {"runs": 0, "errors": 0})
            stats["runs"] += 1
            stats["last_run"] = started.isoformat()
            stats["last_duration_ms"] = elapsed_ms
            logger.debug("Task %s completed in %dms", name, elapsed_ms)
        except Exception as e:
            elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            stats = self._task_stats.setdefault(name, {"runs": 0, "errors": 0})
            stats["errors"] += 1
            stats["last_error"] = str(e)
            stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
            logger.error(
                "Task %s failed after %dms: %s\n%s",
                name, elapsed_ms, e,
                traceback.format_exc(),
            )

    # ------------------------------------------------------------------
    # Task delegates
    # ------------------------------------------------------------------

    async def _lifecycle_decay(self) -> None:
        """Event lifecycle transitions and situation decay."""
        mgr = LifecycleManager(self._pg_pool)
        transitions = await mgr.event_lifecycle_decay()
        dormant = await mgr.situation_decay()
        if transitions or dormant:
            logger.info(
                "Lifecycle: %d event transitions, %d situations marked dormant",
                transitions, dormant,
            )
            # Publish notification via NATS
            if self._nats and (transitions or dormant):
                try:
                    msg = {
                        "type": "lifecycle_decay",
                        "event_transitions": transitions,
                        "situations_dormant": dormant,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    await self._nats.publish(
                        "legba.maintenance.lifecycle",
                        json.dumps(msg).encode(),
                    )
                except Exception:
                    pass

    async def _entity_gc(self) -> None:
        """Entity garbage collection and source health."""
        gc = EntityGarbageCollector(self._pg_pool)
        dormant = await gc.entity_gc()
        dupes = await gc.detect_duplicate_entities()
        orphans = await gc.clean_orphan_edges()
        paused = await gc.source_health()
        logger.info(
            "Entity GC: %d dormant, %d duplicate candidates, %d orphan edges removed, %d sources paused",
            dormant, dupes, orphans, paused,
        )

    async def _fact_decay(self) -> None:
        """Fact temporal management."""
        mgr = FactDecayManager(self._pg_pool)
        expired = await mgr.fact_decay()
        decayed = await mgr.confidence_decay()
        logger.info("Fact decay: %d expired, %d confidence-decayed", expired, decayed)

    async def _corroboration_scoring(self) -> None:
        """Signal corroboration scoring."""
        scorer = CorroborationScorer(self._pg_pool)
        scored = await scorer.corroboration_scoring()
        if scored:
            logger.info("Corroboration: %d events scored", scored)

    async def _integrity_verification(self) -> None:
        """Data integrity verification and eval rubrics."""
        verifier = IntegrityVerifier(self._pg_pool, self._metrics)
        issues = await verifier.integrity_verification()
        rubric_results = await verifier.eval_rubrics()
        total_issues = sum(issues.values()) if issues else 0
        logger.info(
            "Integrity: %d issues found, rubrics: %s",
            total_issues,
            ", ".join(f"{k}={v:.2f}" for k, v in rubric_results.items()) if rubric_results else "none",
        )

    async def _metric_collection(self) -> None:
        """Extended metric collection."""
        collector = MetricCollector(self._pg_pool, self._metrics)
        await collector.metric_collection()

    async def _situation_detection(self) -> None:
        """Automated situation detection from event clusters."""
        detector = SituationDetector(self._pg_pool)
        proposed = await detector.detect_situations()
        if proposed:
            logger.info("Situation detection: %d proposals created", proposed)

    async def _adversarial_detect(self) -> None:
        """Adversarial signal detection — coordinated inauthentic behavior."""
        detector = AdversarialDetector(self._pg_pool, self._qdrant)
        results = await detector.run_all()
        total_flags = sum(len(v) for v in results.values())
        if total_flags:
            logger.info(
                "Adversarial: %d flag groups — %s",
                total_flags,
                ", ".join(f"{k}({len(v)})" for k, v in results.items()),
            )
            # Write detection counts to metrics
            if self._metrics and self._metrics.available:
                points = [
                    ("adversarial_flags", flag_type, float(len(flags)))
                    for flag_type, flags in results.items()
                ]
                points.append(("adversarial_flags_total", "all", float(total_flags)))
                await self._metrics.write_batch(points)

    async def _calibration_track(self) -> None:
        """Confidence calibration tracking and analysis."""
        tracker = CalibrationTracker(
            self._pg_pool, self._metrics, self._redis,
        )
        results = await tracker.run_all()
        if results:
            logger.info(
                "Calibration: tracked=%d, discrimination=%.3f",
                results.get("hypotheses_tracked", 0),
                results.get("confidence_discrimination", 0.0),
            )

    # ------------------------------------------------------------------
    # Health server
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
                    "task_stats": self._task_stats,
                    "uptime_seconds": int(
                        (datetime.now(timezone.utc) - self._start_time).total_seconds()
                    ) if self._start_time else 0,
                })
            else:
                body = json.dumps({
                    "status": "ok",
                    "service": "maintenance",
                    "uptime_seconds": int(
                        (datetime.now(timezone.utc) - self._start_time).total_seconds()
                    ) if self._start_time else 0,
                    "ticks": self._tick_count,
                    "task_stats": self._task_stats,
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

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Clean shutdown of connections."""
        logger.info("Shutting down maintenance daemon")
        self._running = False

        if self._nats:
            try:
                await self._nats.close()
            except Exception:
                pass

        if self._os_client:
            try:
                await self._os_client.close()
            except Exception:
                pass

        if self._qdrant:
            try:
                await self._qdrant.close()
            except Exception:
                pass

        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass

        if self._metrics:
            try:
                await self._metrics.close()
            except Exception:
                pass

        if self._pg_pool:
            try:
                await self._pg_pool.close()
            except Exception:
                pass

        logger.info(
            "Maintenance daemon stopped after %d ticks. Task summary: %s",
            self._tick_count,
            json.dumps(self._task_stats, default=str),
        )

    def stop(self) -> None:
        """Signal the service to stop."""
        self._running = False


def main() -> None:
    """Entry point for `python -m legba.maintenance`."""
    config = MaintenanceConfig.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    service = MaintenanceService(config)

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
