"""Legba Ingestion Service — main entry point.

Deterministic source fetcher. No LLM. Runs continuously, fetching
sources on their configured intervals, deduplicating events, storing
to Postgres + OpenSearch, and publishing notifications via NATS.

Usage:
    python -m legba.ingestion.service
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from uuid import UUID

import asyncpg
import nats as nats_lib
import redis.asyncio as aioredis

from .config import IngestionConfig
from .dedup import DedupEngine
from .fetcher import fetch_source
from .normalizer import normalize_entry
from .scheduler import ScheduledSource, get_due_sources, initialize_next_fetch
from .storage import StorageLayer

logger = logging.getLogger("legba.ingestion")


class IngestionService:
    """Main ingestion service orchestrator."""

    def __init__(self, config: IngestionConfig):
        self.config = config
        self._running = False
        self._pg_pool: asyncpg.Pool | None = None
        self._redis: aioredis.Redis | None = None
        self._nats: nats_lib.NATS | None = None
        self._os_client = None
        self._storage: StorageLayer | None = None
        self._dedup: DedupEngine | None = None
        self._tick_count = 0
        self._events_total = 0
        self._start_time: datetime | None = None
        # Semaphore to limit concurrent fetches
        self._fetch_sem: asyncio.Semaphore | None = None

    async def start(self) -> None:
        """Initialize connections and start the fetch loop."""
        self._start_time = datetime.now(timezone.utc)
        self._running = True
        self._fetch_sem = asyncio.Semaphore(self.config.max_workers)

        logger.info("Starting Legba Ingestion Service")
        logger.info(
            "Config: check_interval=%ds, max_workers=%d, dedup_cache=%d",
            self.config.check_interval,
            self.config.max_workers,
            self.config.dedup_cache_size,
        )

        # Connect to backing stores
        await self._connect()

        # Initialize next_fetch_at for sources that don't have it
        await initialize_next_fetch(self._pg_pool)

        # Load dedup cache
        await self._dedup.load_cache()

        # Ensure schema (run same migration as agent)
        await self._ensure_schema()

        logger.info("Ingestion service initialized, entering main loop")

        # Start health server in background
        asyncio.create_task(self._health_server())

        # Main loop
        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(self.config.check_interval)
        except asyncio.CancelledError:
            logger.info("Ingestion service cancelled")
        finally:
            await self._shutdown()

    async def _connect(self) -> None:
        """Establish connections to all backing stores."""
        cfg = self.config

        # Postgres
        self._pg_pool = await asyncpg.create_pool(
            cfg.postgres.dsn,
            min_size=2,
            max_size=cfg.max_workers + 2,
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
            logger.warning("OpenSearch not available, events will only go to Postgres: %s", e)

        # NATS
        try:
            self._nats = await nats_lib.connect(cfg.nats.url)
            logger.info("Connected to NATS at %s", cfg.nats.url)
        except Exception as e:
            logger.warning("NATS not available, notifications disabled: %s", e)
            self._nats = None

        # Initialize storage and dedup
        self._storage = StorageLayer(self._pg_pool, self._os_client, self._redis)
        self._dedup = DedupEngine(self._pg_pool, self.config.dedup_cache_size)

    async def _ensure_schema(self) -> None:
        """Ensure ingestion-specific schema exists."""
        try:
            await self._pg_pool.execute("""
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS fetch_interval_minutes INTEGER DEFAULT 60;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS next_fetch_at TIMESTAMPTZ;
                ALTER TABLE sources ADD COLUMN IF NOT EXISTS category TEXT DEFAULT '';
                CREATE INDEX IF NOT EXISTS idx_sources_next_fetch ON sources(next_fetch_at)
                    WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS ingestion_log (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    source_id UUID REFERENCES sources(id),
                    source_name TEXT NOT NULL DEFAULT '',
                    fetch_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    fetch_completed_at TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'running',
                    events_fetched INTEGER DEFAULT 0,
                    events_stored INTEGER DEFAULT 0,
                    events_deduped INTEGER DEFAULT 0,
                    error_message TEXT,
                    duration_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_ingestion_log_source
                    ON ingestion_log(source_id);
                CREATE INDEX IF NOT EXISTS idx_ingestion_log_time
                    ON ingestion_log(fetch_started_at DESC);
            """)
        except Exception as e:
            logger.warning("Schema ensure failed (may already exist): %s", e)

    async def _tick(self) -> None:
        """One scheduler tick — find and process due sources."""
        self._tick_count += 1
        await self._storage.update_heartbeat()

        # Get sources due for fetching
        sources = await get_due_sources(self._pg_pool, limit=self.config.max_workers)
        if not sources:
            if self._tick_count % 10 == 0:  # Log every ~5 minutes at 30s interval
                logger.debug("No sources due for fetching")
            return

        logger.info("Tick %d: %d sources due for fetching", self._tick_count, len(sources))

        # Fetch concurrently up to max_workers
        tasks = [self._process_source(source) for source in sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                import traceback
                logger.error(
                    "Source processing crashed: %s — %s: %s\n%s",
                    sources[i].name, type(result).__name__, result,
                    "".join(traceback.format_exception(type(result), result, result.__traceback__)),
                )

        # Refresh dedup counter periodically
        if self._tick_count % 20 == 0:  # Every ~10 minutes
            await self._dedup.load_cache()

        # Batch entity linking — process unlinked events every ~30 minutes
        if self._tick_count % 60 == 30:
            try:
                linked = await self._storage.batch_link_entities(limit=200)
                if linked:
                    logger.info("Batch linker: %d entity links created", linked)
            except Exception as e:
                logger.warning("Batch linker failed: %s", e)

    async def _process_source(self, source: ScheduledSource) -> None:
        """Fetch, dedup, store events from a single source."""
        async with self._fetch_sem:
            # Advance next_fetch_at immediately to prevent re-selection while processing
            try:
                await self._pg_pool.execute(
                    "UPDATE sources SET next_fetch_at = NOW() + ($1 || ' minutes')::interval WHERE id = $2",
                    str(source.fetch_interval_minutes), source.id,
                )
            except Exception:
                pass

            log_id = await self._storage.log_fetch_start(source.id, source.name)

            logger.info("Fetching: %s (%s, %s)", source.name, source.source_type, source.url[:80])

            # Resolve auth config from env vars if needed
            auth_config = self._resolve_auth(source.auth_config)

            # Fetch
            result = await fetch_source(
                source.url,
                source_type=source.source_type,
                query_template=source.query_template,
                auth_config=auth_config,
                last_fetch=source.last_successful_fetch_at,
                timeout=self.config.http_timeout,
                limit=self.config.batch_size,
            )

            if not result.success:
                logger.warning(
                    "Fetch failed: %s — %s (HTTP %s)",
                    source.name, result.error, result.http_status,
                )
                await self._storage.record_source_failure(
                    source.id, result.error, self.config.auto_pause_threshold,
                )
                await self._storage.log_fetch_complete(
                    log_id,
                    status="error",
                    error_message=result.error,
                    duration_ms=result.fetch_duration_ms,
                )
                return

            if not result.entries:
                logger.info("Done: %s — 0 entries (%s)", source.name, result.parse_mode)
                await self._storage.record_source_success(source.id, 0)
                await self._storage.log_fetch_complete(
                    log_id,
                    status="success",
                    events_fetched=0,
                    duration_ms=result.fetch_duration_ms,
                )
                return

            # Normalize entries to Events
            events = []
            for entry in result.entries:
                event = normalize_entry(
                    entry,
                    source_id=source.id,
                    source_name=source.name,
                    source_category=source.category,
                    source_language=source.language,
                )
                events.append(event)

            # Batch-internal dedup (within this fetch)
            batch_entries = [(e.guid, e.source_url, e.title) for e in events]
            batch_dupes = await self._dedup.check_batch_internal(batch_entries)

            # Dedup against existing events
            stored = 0
            deduped = 0
            for i, event in enumerate(events):
                if i in batch_dupes:
                    deduped += 1
                    continue

                dup = await self._dedup.check(event.guid, event.source_url, event.title)
                if dup.is_duplicate:
                    deduped += 1
                    continue

                # Geo resolution (best-effort)
                await self._resolve_geo(event)

                # Store
                ok = await self._storage.store_event(event)
                if ok:
                    stored += 1
                    self._events_total += 1
                    self._dedup.add_to_cache(event.title)

            # Record source success
            await self._storage.record_source_success(source.id, stored)

            # Log completion
            await self._storage.log_fetch_complete(
                log_id,
                status="success",
                events_fetched=len(events),
                events_stored=stored,
                events_deduped=deduped,
                duration_ms=result.fetch_duration_ms,
            )

            logger.info(
                "Done: %s — %d fetched, %d stored, %d deduped (%s)",
                source.name, len(events), stored, deduped, result.parse_mode,
            )

            # NATS notification
            if stored > 0:
                await self._notify_nats(source, stored, deduped, events)

    async def _resolve_geo(self, event: Event) -> None:
        """Best-effort geo resolution for event locations."""
        if not event.locations:
            return
        try:
            from legba.agent.tools.builtins.geo import resolve_locations
            geo = resolve_locations(event.locations)
            event.geo_countries = geo.get("countries", [])
            event.geo_regions = geo.get("regions", [])
            event.geo_coordinates = geo.get("coordinates", [])
        except Exception:
            pass

    def _resolve_auth(self, auth_config: dict) -> dict | None:
        """Resolve auth config, substituting env var references."""
        if not auth_config:
            return None

        import os
        resolved = dict(auth_config)

        # Support env var references: {"value": "$ACLED_API_KEY"}
        value = resolved.get("value", "")
        if isinstance(value, str) and value.startswith("$"):
            env_key = value[1:]
            resolved["value"] = os.getenv(env_key, "")
            if not resolved["value"]:
                logger.warning("Auth env var %s not set", env_key)
                return None

        return resolved if resolved.get("value") else None

    async def _notify_nats(
        self,
        source: ScheduledSource,
        stored: int,
        deduped: int,
        events: list,
    ) -> None:
        """Publish ingestion batch notification to NATS."""
        if not self._nats:
            return
        try:
            categories = list(set(e.category.value for e in events if hasattr(e, "category")))
            msg = {
                "source_id": str(source.id),
                "source_name": source.name,
                "events_stored": stored,
                "events_deduped": deduped,
                "categories": categories[:10],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await self._nats.publish(
                "legba.ingest.batch",
                json.dumps(msg).encode(),
            )
        except Exception as e:
            logger.debug("NATS publish failed: %s", e)

    async def _health_server(self) -> None:
        """Simple HTTP health endpoint."""
        from asyncio import start_server

        async def handle(reader, writer):
            data = await reader.read(1024)
            request_line = data.decode().split("\n")[0] if data else ""

            if "/metrics" in request_line:
                body = json.dumps({
                    "ticks": self._tick_count,
                    "events_total": self._events_total,
                    "uptime_seconds": int(
                        (datetime.now(timezone.utc) - self._start_time).total_seconds()
                    ) if self._start_time else 0,
                })
            else:
                body = json.dumps({
                    "status": "ok",
                    "uptime_seconds": int(
                        (datetime.now(timezone.utc) - self._start_time).total_seconds()
                    ) if self._start_time else 0,
                    "ticks": self._tick_count,
                    "events_total": self._events_total,
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
        logger.info("Shutting down ingestion service")
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

        logger.info("Ingestion service stopped. Total events stored: %d", self._events_total)

    def stop(self) -> None:
        """Signal the service to stop."""
        self._running = False


def main() -> None:
    """Entry point for `python -m legba.ingestion.service`."""
    config = IngestionConfig.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    service = IngestionService(config)

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
