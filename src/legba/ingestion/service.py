"""Legba Ingestion Service — main entry point.

Deterministic source fetcher. No LLM. Runs continuously, fetching
sources on their configured intervals, deduplicating signals, storing
to Postgres + OpenSearch, and publishing notifications via NATS.

Usage:
    python -m legba.ingestion.service
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
from .models_client import ModelsClient
from .storage import StorageLayer
from ..shared.metrics import MetricsClient

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
        self._signals_total = 0
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
            logger.warning("OpenSearch not available, signals will only go to Postgres: %s", e)

        # Qdrant (for signal embeddings)
        qdrant_client = None
        try:
            qdrant_host = os.getenv("QDRANT_HOST", "localhost")
            qdrant_port = int(os.getenv("QDRANT_PORT", "6333"))
            from qdrant_client import AsyncQdrantClient
            qdrant_client = AsyncQdrantClient(host=qdrant_host, port=qdrant_port)
            await qdrant_client.get_collections()  # connection test
            logger.info("Connected to Qdrant at %s:%d", qdrant_host, qdrant_port)
        except Exception as e:
            logger.warning("Qdrant not available, signal embeddings disabled: %s", e)
            qdrant_client = None

        # Embedding function (uses vLLM OpenAI-compatible endpoint)
        embed_fn = None
        embed_base = os.getenv("EMBEDDING_API_BASE") or os.getenv("OPENAI_BASE_URL", "")
        embed_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        embed_model = os.getenv("MEMORY_EMBEDDING_MODEL", "embedding-inno1")
        if embed_base and qdrant_client:
            try:
                import httpx
                _embed_http = httpx.AsyncClient(
                    base_url=embed_base.rstrip("/"),
                    headers={"Authorization": f"Bearer {embed_key}", "Content-Type": "application/json"},
                    timeout=httpx.Timeout(30),
                )

                async def _generate_embedding(text: str) -> list[float]:
                    resp = await _embed_http.post("/embeddings", json={"model": embed_model, "input": text})
                    resp.raise_for_status()
                    return resp.json()["data"][0]["embedding"]

                embed_fn = _generate_embedding
                logger.info("Embedding enabled: model=%s via %s", embed_model, embed_base)
            except Exception as e:
                logger.warning("Embedding client setup failed: %s", e)

        # NATS
        try:
            self._nats = await nats_lib.connect(cfg.nats.url)
            logger.info("Connected to NATS at %s", cfg.nats.url)
        except Exception as e:
            logger.warning("NATS not available, notifications disabled: %s", e)
            self._nats = None

        # Initialize storage and dedup
        self._storage = StorageLayer(
            self._pg_pool, self._os_client, self._redis,
            qdrant_client=qdrant_client,
            embedding_client=embed_fn,
        )
        await self._storage.ensure_qdrant_collection()
        self._qdrant = qdrant_client
        self._dedup = DedupEngine(self._pg_pool, self.config.dedup_cache_size, qdrant_client=qdrant_client, embed_fn=embed_fn)

        # Models service (optional GPU inference — classification, translation, extraction, summarization)
        self._models = ModelsClient()
        if await self._models.check_health():
            logger.info("Models service connected")
        else:
            logger.info("Models service not available — using regex classification")

        # Metrics (TimescaleDB, optional)
        self._metrics = MetricsClient()
        await self._metrics.connect()

        # Telegram (optional)
        self._telegram = None
        if os.getenv("TELEGRAM_ENABLED", "").lower() in ("true", "1", "yes"):
            from .telegram import TelegramFetcher
            self._telegram = TelegramFetcher()
            if await self._telegram.connect():
                logger.info("Telegram ingestion enabled")
            else:
                self._telegram = None

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

        # Periodic models health check (every ~60s)
        if self._tick_count % 2 == 0:
            await self._models.check_health()

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

        # Batch entity linking — process unlinked signals every ~30 minutes
        if self._tick_count % 60 == 30:
            try:
                linked = await self._storage.batch_link_entities(limit=200)
                if linked:
                    logger.info("Batch linker: %d entity links created", linked)
            except Exception as e:
                logger.warning("Batch linker failed: %s", e)

        # Signal-to-event clustering — every ~20 minutes (offset from entity linker)
        if self._tick_count % 20 == 10:  # Every ~10 minutes
            try:
                from .cluster import SignalClusterer
                from .notifications import NotificationDispatcher
                notifier = NotificationDispatcher()
                clusterer = SignalClusterer(self._pg_pool, qdrant_client=self._qdrant, notifier=notifier)
                events_affected = await clusterer.cluster(window_hours=12, max_signals=1500)
                if events_affected:
                    logger.info("Signal clusterer: %d events created/updated", events_affected)
            except Exception as e:
                logger.warning("Signal clusterer failed: %s", e)

    async def _process_source(self, source: ScheduledSource) -> None:
        """Fetch, dedup, store signals from a single source."""
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

            # Telegram sources use a different fetch path
            if source.source_type == "telegram":
                await self._process_telegram_source(source, log_id)
                return

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
                user_agent=source.config.get("user_agent", ""),
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

            # Normalize entries to Signals
            signals = []
            for entry in result.entries:
                sig = normalize_entry(
                    entry,
                    source_id=source.id,
                    source_name=source.name,
                    source_category=source.category,
                    source_language=source.language,
                )
                signals.append(sig)

            # Model enrichment (if GPU service available)
            if self._models.available:
                for sig in signals:
                    await self._enrich_with_models(sig)

            # Batch-internal dedup (within this fetch)
            batch_entries = [(s.guid, s.source_url, s.title) for s in signals]
            batch_dupes = await self._dedup.check_batch_internal(batch_entries)

            # Batch dedup against existing signals (2 DB queries instead of 2N)
            # Only check entries that survived batch-internal dedup
            candidates = [
                (i, signals[i])
                for i in range(len(signals))
                if i not in batch_dupes
            ]
            if candidates:
                candidate_entries = [
                    (sig.guid, sig.source_url, sig.title) for _, sig in candidates
                ]
                batch_results = await self._dedup.check_batch(candidate_entries)
            else:
                batch_results = []

            stored = 0
            deduped = len(batch_dupes)
            for j, (i, sig) in enumerate(candidates):
                if batch_results[j].is_duplicate:
                    deduped += 1
                    continue

                # Geo resolution (best-effort)
                await self._resolve_geo(sig)

                # Store
                ok = await self._storage.store_signal(sig)
                if ok:
                    stored += 1
                    self._signals_total += 1
                    self._dedup.add_to_cache(sig.title)

            # Record source success
            await self._storage.record_source_success(source.id, stored)

            # Log completion
            await self._storage.log_fetch_complete(
                log_id,
                status="success",
                events_fetched=len(signals),
                events_stored=stored,
                events_deduped=deduped,
                duration_ms=result.fetch_duration_ms,
            )

            logger.info(
                "Done: %s — %d fetched, %d stored, %d deduped (%s)",
                source.name, len(signals), stored, deduped, result.parse_mode,
            )

            # Metrics
            if self._metrics.available:
                await self._metrics.write_batch([
                    ("signals_stored", f"source:{source.name}", stored),
                    ("signals_deduped", f"source:{source.name}", deduped),
                    ("signals_fetched", f"source:{source.name}", len(signals)),
                ])

            # NATS notification
            if stored > 0:
                await self._notify_nats(source, stored, deduped, signals)

    async def _resolve_geo(self, signal: Signal) -> None:
        """Best-effort geo resolution for signal locations."""
        if not signal.locations:
            return
        try:
            from legba.agent.tools.builtins.geo import resolve_locations
            geo = resolve_locations(signal.locations)
            signal.geo_countries = geo.get("countries", [])
            signal.geo_regions = geo.get("regions", [])
            signal.geo_coordinates = geo.get("coordinates", [])
        except Exception:
            pass

    async def _enrich_with_models(self, signal) -> None:
        """Enrich a signal using the GPU models service. Best-effort."""
        try:
            # Translation: if non-English, translate title
            if signal.language and signal.language != "en":
                translated = await self._models.translate(
                    signal.title, source_lang=signal.language,
                )
                if translated:
                    # Store original, replace with translation
                    signal.tags = list(signal.tags) + [f"original_lang:{signal.language}"]
                    signal.title = translated

            # Classification: replace regex-inferred category
            category, confidence = await self._models.classify(signal.title)
            if category and category != "other" and confidence > 0.5:
                signal.category = category

            # NER: spaCy trf on GPU (high-accuracy entity extraction)
            ner_actors, ner_locations = await self._models.ner(signal.title)

            # Entity/relation extraction: REBEL triples (relationship discovery)
            triples = await self._models.extract_triples(signal.title)

            # Merge NER + REBEL + existing spaCy sm results
            actors = set(signal.actors) if signal.actors else set()
            locations = set(signal.locations) if signal.locations else set()

            # Add NER results
            actors.update(a for a in ner_actors if len(a) >= 2)
            locations.update(l for l in ner_locations if len(l) >= 2)

            # Add REBEL subject/object entities
            if triples:
                for t in triples:
                    subj = t.get("subject", "").strip()
                    obj = t.get("object", "").strip()
                    pred = t.get("predicate", "").lower()
                    if subj and len(subj) >= 2:
                        if "location" in pred or "country" in pred or "place" in pred:
                            locations.add(subj)
                        else:
                            actors.add(subj)
                    if obj and len(obj) >= 2:
                        if "location" in pred or "country" in pred or "place" in pred:
                            locations.add(obj)
                        else:
                            actors.add(obj)

            signal.actors = list(actors)[:15]
            signal.locations = list(locations)[:10]

        except Exception as e:
            logger.debug("Model enrichment failed for signal: %s", e)

    async def _process_telegram_source(self, source: ScheduledSource, log_id) -> None:
        """Fetch and process signals from a Telegram channel."""
        if not self._telegram or not self._telegram.available:
            logger.warning("Telegram not available for source %s", source.name)
            await self._storage.log_fetch_complete(
                log_id, status="error", error_message="Telegram client not available",
            )
            return

        try:
            # Extract handle from telegram://@handle URL
            handle = source.url.replace("telegram://", "").lstrip("@")

            messages = await self._telegram.fetch_channel(
                handle,
                since=source.last_successful_fetch_at,
                limit=self.config.batch_size,
            )

            if not messages:
                await self._storage.record_source_success(source.id, 0)
                await self._storage.log_fetch_complete(log_id, status="success", events_fetched=0)
                return

            # Normalize to Signal schema
            from .telegram_normalizer import normalize_telegram_message
            signals = [
                normalize_telegram_message(
                    msg, source_id=source.id, source_name=source.name,
                    source_category=source.category, source_language=source.language,
                )
                for msg in messages
            ]

            # Same dedup + store pipeline as RSS/API signals
            batch_entries = [(s.guid, s.source_url, s.title) for s in signals]
            batch_dupes = await self._dedup.check_batch_internal(batch_entries)

            candidates = [
                (i, signals[i]) for i in range(len(signals)) if i not in batch_dupes
            ]

            if candidates:
                existing = await self._dedup.check_batch(
                    [(s.guid, s.source_url, s.title) for _, s in candidates]
                )
                to_store = [
                    s for (_, s), result in zip(candidates, existing) if not result.is_duplicate
                ]
            else:
                to_store = []

            stored = 0
            for sig in to_store:
                await self._resolve_geo(sig)
                ok = await self._storage.store_signal(sig)
                if ok:
                    stored += 1
                    self._signals_total += 1

            duped = len(signals) - stored
            logger.info(
                "Telegram: %s — %d messages, %d stored, %d deduped",
                source.name, len(messages), stored, duped,
            )

            await self._storage.record_source_success(source.id, stored)
            await self._storage.log_fetch_complete(
                log_id, status="success",
                events_fetched=len(messages), events_stored=stored, events_deduped=duped,
            )

        except Exception as e:
            logger.warning("Telegram processing failed for %s: %s", source.name, e)
            await self._storage.record_source_failure(source.id, str(e), self.config.auto_pause_threshold)
            await self._storage.log_fetch_complete(log_id, status="error", error_message=str(e))

    def _resolve_auth(self, auth_config: dict) -> dict | None:
        """Resolve auth config, substituting env var references."""
        if not auth_config:
            return None

        import os
        resolved = dict(auth_config)
        auth_type = resolved.get("type", "")

        # Bearer / OAuth2: resolve token, client_id, client_secret, username, password env refs.
        # Token exchange itself happens in fetcher._get_oauth_token at fetch time.
        if auth_type == "bearer":
            for key in ("token", "client_id", "client_secret", "username", "password"):
                val = resolved.get(key, "")
                if isinstance(val, str) and val.startswith("$"):
                    env_key = val[1:]
                    resolved[key] = os.getenv(env_key, "")
                    # Don't warn for missing token when token_url is set (OAuth2 flow)
                    if not resolved[key] and key == "token" and resolved.get("token_url"):
                        continue
                    if not resolved[key]:
                        logger.warning("Auth env var %s not set", env_key)
            # Bearer is valid if we have a static token OR a token_url for OAuth2
            if resolved.get("token") or resolved.get("token_url"):
                return resolved
            return None

        # api_key / query_param: resolve single "value" field
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
        signals: list,
    ) -> None:
        """Publish ingestion batch notification to NATS."""
        if not self._nats:
            return
        try:
            categories = list(set(str(s.category.value if hasattr(s.category, 'value') else s.category) for s in signals if hasattr(s, "category")))
            msg = {
                "source_id": str(source.id),
                "source_name": source.name,
                "signals_stored": stored,
                "signals_deduped": deduped,
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
                    "signals_total": self._signals_total,
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
                    "signals_total": self._signals_total,
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

        if self._models:
            try:
                await self._models.close()
            except Exception:
                pass

        if self._telegram:
            try:
                await self._telegram.close()
            except Exception:
                pass

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

        logger.info("Ingestion service stopped. Total signals stored: %d", self._signals_total)

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
