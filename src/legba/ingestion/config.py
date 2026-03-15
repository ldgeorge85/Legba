"""Ingestion service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from legba.shared.config import (
    NatsConfig,
    OpenSearchConfig,
    PostgresConfig,
    RedisConfig,
)


@dataclass(frozen=True)
class IngestionConfig:
    """Ingestion service tuning knobs."""

    check_interval: int = 30          # Seconds between scheduler ticks
    max_workers: int = 4              # Concurrent source fetches
    http_timeout: int = 30            # Per-source fetch timeout (seconds)
    dedup_cache_size: int = 500       # Recent events kept in memory for Jaccard dedup
    batch_size: int = 50              # Max events per source fetch before store
    health_port: int = 8600           # Health/metrics HTTP port
    auto_pause_threshold: int = 10    # Consecutive failures before auto-pause (error status)
    log_level: str = "INFO"

    # Backing store configs
    postgres: PostgresConfig = PostgresConfig()
    redis: RedisConfig = RedisConfig()
    opensearch: OpenSearchConfig = OpenSearchConfig()
    nats: NatsConfig = NatsConfig()

    @classmethod
    def from_env(cls) -> IngestionConfig:
        return cls(
            check_interval=int(os.getenv("INGESTION_CHECK_INTERVAL", "30")),
            max_workers=int(os.getenv("INGESTION_MAX_WORKERS", "4")),
            http_timeout=int(os.getenv("INGESTION_HTTP_TIMEOUT", "30")),
            dedup_cache_size=int(os.getenv("INGESTION_DEDUP_CACHE_SIZE", "500")),
            batch_size=int(os.getenv("INGESTION_BATCH_SIZE", "50")),
            health_port=int(os.getenv("INGESTION_HEALTH_PORT", "8600")),
            auto_pause_threshold=int(os.getenv("INGESTION_AUTO_PAUSE_THRESHOLD", "10")),
            log_level=os.getenv("INGESTION_LOG_LEVEL", "INFO"),
            postgres=PostgresConfig.from_env(),
            redis=RedisConfig.from_env(),
            opensearch=OpenSearchConfig.from_env(),
            nats=NatsConfig.from_env(),
        )
