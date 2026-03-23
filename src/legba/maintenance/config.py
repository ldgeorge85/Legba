"""Maintenance daemon configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from legba.shared.config import (
    NatsConfig,
    OpenSearchConfig,
    PostgresConfig,
    QdrantConfig,
    RedisConfig,
)


@dataclass(frozen=True)
class MaintenanceConfig:
    """Maintenance daemon tuning knobs.

    All intervals expressed in ticks (1 tick = check_interval seconds).
    Default check_interval is 60s, so tick counts map to minutes 1:1.
    """

    check_interval: int = 60                # Seconds between scheduler ticks
    health_port: int = 8700                 # Health/metrics HTTP port
    log_level: str = "INFO"

    # Task intervals (in ticks)
    lifecycle_decay_interval: int = 5       # Event lifecycle decay (5 min)
    entity_gc_interval: int = 60            # Entity garbage collection (60 min)
    fact_decay_interval: int = 60           # Fact temporal management (60 min)
    corroboration_interval: int = 10        # Signal corroboration scoring (10 min)
    metrics_interval: int = 5               # Extended metric collection (5 min)
    integrity_interval: int = 720           # Data integrity verification (12h)
    situation_detect_interval: int = 30    # Situation detection (30 min)
    adversarial_detect_interval: int = 30  # Adversarial signal detection (30 min)
    calibration_track_interval: int = 60   # Confidence calibration tracking (60 min)

    # Backing store configs
    postgres: PostgresConfig = PostgresConfig()
    redis: RedisConfig = RedisConfig()
    opensearch: OpenSearchConfig = OpenSearchConfig()
    qdrant: QdrantConfig = QdrantConfig()
    nats: NatsConfig = NatsConfig()

    @classmethod
    def from_env(cls) -> MaintenanceConfig:
        return cls(
            check_interval=int(os.getenv("MAINTENANCE_CHECK_INTERVAL", "60")),
            health_port=int(os.getenv("MAINTENANCE_HEALTH_PORT", "8700")),
            log_level=os.getenv("MAINTENANCE_LOG_LEVEL", "INFO"),
            lifecycle_decay_interval=int(os.getenv("MAINTENANCE_LIFECYCLE_DECAY_INTERVAL", "5")),
            entity_gc_interval=int(os.getenv("MAINTENANCE_ENTITY_GC_INTERVAL", "60")),
            fact_decay_interval=int(os.getenv("MAINTENANCE_FACT_DECAY_INTERVAL", "60")),
            corroboration_interval=int(os.getenv("MAINTENANCE_CORROBORATION_INTERVAL", "10")),
            metrics_interval=int(os.getenv("MAINTENANCE_METRICS_INTERVAL", "5")),
            integrity_interval=int(os.getenv("MAINTENANCE_INTEGRITY_INTERVAL", "720")),
            situation_detect_interval=int(os.getenv("MAINTENANCE_SITUATION_DETECT_INTERVAL", "30")),
            adversarial_detect_interval=int(os.getenv("MAINTENANCE_ADVERSARIAL_DETECT_INTERVAL", "30")),
            calibration_track_interval=int(os.getenv("MAINTENANCE_CALIBRATION_TRACK_INTERVAL", "60")),
            postgres=PostgresConfig.from_env(),
            redis=RedisConfig.from_env(),
            opensearch=OpenSearchConfig.from_env(),
            qdrant=QdrantConfig.from_env(),
            nats=NatsConfig.from_env(),
        )
